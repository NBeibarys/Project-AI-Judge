"""Synchronous batch-facing wrapper around the asynchronous ADK workflow.

All programs use the separate-head architecture:
  1. Run the analyst->grader VERIFY loop ONCE (max 3 iterations). The grader
     does not score; it only approves or rejects the evidence.
  2. If the evidence is approved, run the Head scorer N_SAMPLES times
     (default 3) at temp=0. Each Head run independently scores the SAME
     approved evidence.
  3. Average the criterion scores across the N_SAMPLES Head runs.
  4. Select the rationale from the Head run whose final_score is closest to
     the averaged final score (closest-rationale selection).
  5. Final score = averaged; rationale = selected from closest run.

Multi-sample averaging (research gap #5, arXiv:2606.26185): temp=0 does NOT
fully eliminate variance in LLM judges due to floating-point non-determinism,
batching, and backend routing. Averaging the Head's criterion scores across
multiple runs reduces run-to-run jitter and yields a more stable score.
Only the Head is re-run (cheap), not the whole analyst->grader loop (expensive).

If a Head run fails (API error, schema validation), it is excluded from the
average. If ALL Head runs fail, the row escalates to human review. If the
verify loop itself fails to reach approval (3 rejections), the row escalates
to human review without scoring.
"""
import asyncio
import json
import os
import threading
import uuid

import certifi
from google.adk.agents import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agent import build_head_agent, build_root_agent
from ..programs import ProgramConfig

APP_NAME = "fellowship_review"

# One persistent event loop per worker thread, reused across every row that
# thread processes — not a new asyncio.run() per row. pipeline.py's
# run_batch() processes many rows concurrently via ThreadPoolExecutor;
# asyncio.run() per row creates and destroys a whole event loop per call.
# ADK's Gemini.api_client is a @cached_property (see google/adk/models/
# google_llm.py) — its cached async HTTP client outlives that per-row loop,
# so the next row on the same thread reuses a client still bound to an
# already-closed loop, raising "RuntimeError: Event loop is closed" during
# cleanup (confirmed live: 44 of 69 failures in a 100-row batch run at
# MAX_CONCURRENCY=8). Keeping one loop alive for the thread's whole
# lifetime — closed only when the thread pool shuts down — means cached
# clients stay valid across rows instead of outliving their loop.
_thread_local = threading.local()


def _thread_event_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


# Kill-in-flight-grading support: every row currently inside invoke() is
# registered here as (loop, task) keyed by row_id, guarded by _active_lock.
# cancel_all_active() is the near-instant kill path for the Streamlit Stop
# button — it schedules task.cancel() on each row's OWN loop via
# call_soon_threadsafe (task.cancel() itself is not thread-safe to call
# directly from another thread; call_soon_threadsafe is), which raises
# asyncio.CancelledError inside the row's in-flight Gemini/ADK call almost
# immediately, rather than waiting for that call to finish naturally.
_active_tasks: dict[str, tuple[asyncio.AbstractEventLoop, "asyncio.Task"]] = {}
_active_lock = threading.Lock()


def cancel_all_active() -> None:
    """Cancel every row currently inside AdkReviewWorkflow.invoke().

    Safe to call from any thread (e.g. the Streamlit script thread reacting
    to a Stop click) — each task is cancelled on its own row's event loop via
    call_soon_threadsafe, never by touching the task directly from this
    thread. Clears the registry afterward; invoke()'s own `finally` also
    unregisters each row as it exits, so this is a belt-and-suspenders
    sweep for whatever is still in flight at the moment Stop is clicked.
    """
    with _active_lock:
        entries = list(_active_tasks.values())
        _active_tasks.clear()
    for loop, task in entries:
        loop.call_soon_threadsafe(task.cancel)


# Cap LLM calls per sample:
#   Verify loop: analyst + grader × ProgramConfig.max_verify_iterations —
#   4 worst case for every program now (2 iterations each; R2B, Alchemist,
#   and Fellowship V2 all lowered this since the analyst/grader re-process
#   the full video/deck every iteration). 20 gives headroom without being
#   unbounded.
#   Head: 1 call per run × N_SAMPLES.
MAX_LLM_CALLS_R2B_VERIFY = 20
MAX_LLM_CALLS_HEAD = 2


class AdkReviewWorkflow:
    def __init__(
        self,
        analyzer_model: str,
        grader_model: str,
        program_config: ProgramConfig,
        head_model: str | None = None,
        # Head samples to run and average. The literature (arXiv:2606.26185,
        # Perea multi-judge playbook) recommends 2-3; the batch pipeline
        # always passes config.n_samples explicitly, so this default only
        # applies to direct construction.
        n_samples: int = 3,
    ):
        # certifi provides a consistent trust store across deployments.
        os.environ["SSL_CERT_FILE"] = certifi.where()
        # Keep only immutable configuration: batch threads must not share agent state.
        self.analyzer_model = analyzer_model
        self.grader_model = grader_model
        self.program_config = program_config
        # Head model defaults to the grader model (same model, temp=0).
        self.head_model = head_model or grader_model
        self.n_samples = max(1, n_samples)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_parts(self, state: dict, include_media: bool = True) -> list:
        """Build the multimodal input parts for an agent run.

        Video/pitch-deck media arrives one of two ways (see ResolvedVideo in
        video_urls.py): in-memory bytes (state["*_data"] — Tier-2 downloads
        on Vertex AI, no Files API there) → Part.from_bytes, or a URI Gemini
        can fetch itself (state["*_url"] — a plain public URL for YouTube/
        direct HTTPS video, or a Gemini Files API URI on the Developer API)
        → Part.from_uri. Both "*_data" and "*_url" are checked, not just
        "*_url" truthiness: a Vertex in-memory result leaves "*_url" as ""
        (falsy), so checking only "*_url" would silently drop real media.

        include_media=False skips the video/deck Parts entirely (used by
        the Head for programs with head_include_media=False — see
        ProgramConfig) — only the text Part (raw_row_text + any error/
        chart-text notes) is returned. This is what keeps N_SAMPLES
        concurrent Head calls lightweight enough to parallelize safely;
        see workflow.py's _run_r2b for the concurrency history.
        """
        parts = []
        if include_media:
            video_data = state.get("video_data")
            video_url = state.get("video_url")
            if video_data or video_url:
                mime_type = state.get("video_mime_type") or "video/mp4"
                if video_data:
                    parts.append(types.Part.from_bytes(
                        data=video_data, mime_type=mime_type,
                    ))
                else:
                    # Virtual clip: with segment offsets in state
                    # (operator-typed Segment Start/End cells), Gemini
                    # fetches ONLY that span of the URL server-side —
                    # the agents receive what looks like an individual
                    # clip. Without offsets this builds the same
                    # file_data Part from_uri always built.
                    part_kwargs = {}
                    if state.get("video_segment_start_s") is not None:
                        part_kwargs["video_metadata"] = types.VideoMetadata(
                            start_offset=f"{state['video_segment_start_s']}s",
                            end_offset=f"{state['video_segment_end_s']}s",
                        )
                    parts.append(types.Part(
                        file_data=types.FileData(
                            file_uri=video_url, mime_type=mime_type,
                        ),
                        **part_kwargs,
                    ))
            # Alchemist: pitch deck PDF as a multimodal Part (required source).
            deck_data = state.get("pitch_deck_data")
            deck_url = state.get("pitch_deck_url")
            if deck_data or deck_url:
                deck_mime = state.get("pitch_deck_mime_type", "application/pdf")
                if deck_data:
                    parts.append(types.Part.from_bytes(
                        data=deck_data, mime_type=deck_mime,
                    ))
                else:
                    parts.append(types.Part.from_uri(
                        file_uri=deck_url, mime_type=deck_mime,
                    ))
        text = state.get("raw_row_text", "")
        # Pre-extracted text readout of image-only deck slides (charts,
        # financial tables — see video_ingestion.py's _read_chart_images).
        # Attaching the raw chart images themselves as separate Parts was
        # tried first and confirmed NOT to reliably help (the model reads
        # them correctly in isolation but not consistently once competing
        # with the full deck/video/4-criteria task in the same call — a
        # documented multimodal "needle in a haystack" weakness,
        # arXiv:2406.11230). Converting to text up front sidesteps that:
        # text-based cross-checking in a large context is reliable, so this
        # gives the analyst/grader/head already-read numbers to compare
        # against, instead of asking them to re-read the image themselves.
        if state.get("pitch_deck_chart_text"):
            text += (
                "\n\nPRE-EXTRACTED DATA FROM DECK CHART/IMAGE SLIDES "
                "(already read for you — use this to cross-check against "
                "other sources, same as any other evidence):\n"
                f"{state['pitch_deck_chart_text']}"
            )
        if state.get("video_error"):
            text += f"\n\nVIDEO UNAVAILABLE: {state['video_error']}"
        parts.append(types.Part(text=text))
        return parts

    # ------------------------------------------------------------------
    # Verify loop (analyst -> grader, max 3 iterations)
    # ------------------------------------------------------------------

    async def _run_r2b_verify_loop(self, state: dict) -> dict:
        """Run the analyst->grader verify loop ONCE.

        Returns the session state, which includes ``evidence_approved``
        (True if the grader approved the evidence) and ``analyst_report``
        (the approved evidence the Head will score). If the loop exhausted
        3 attempts without approval, ``human_review_flag`` is True and
        ``final_result`` carries the escalation message.
        """
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(
                self.analyzer_model,
                self.grader_model,
                self.program_config,
            ),
            session_service=session_service,
        )
        session_id = uuid.uuid4().hex
        user_id = state.get("row_id", "batch-user")
        initial_state = {
            **state,
            "grader_feedback": "",
            "attempt": 1,
            "human_review_flag": False,
            "evidence_approved": False,
        }
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state=initial_state,
        )
        parts = self._build_parts(state)
        async for _event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=parts),
            run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS_R2B_VERIFY),
        ):
            pass
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise RuntimeError("ADK session disappeared before result collection.")
        return dict(session.state)

    async def _run_head_once(
        self, state: dict, approved_evidence: dict, sample_idx: int,
    ) -> dict:
        """Run the Head scorer once on the approved evidence.

        Returns the Head's output as a dict
        (criterion_scores, criterion_rationale, final_score, confidence, ...).
        Raises on failure (caller catches and excludes from average).
        """
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=build_head_agent(self.head_model, self.program_config),
            session_service=session_service,
        )
        session_id = uuid.uuid4().hex
        user_id = f"{state.get('row_id', 'batch-user')}_head_{sample_idx}"
        # The Head sees the SAME approved evidence each run. We inject the
        # analyst_report into the session state so the Head's instruction
        # template ({analyst_report}) resolves to the approved evidence.
        initial_state: dict = {"analyst_report": approved_evidence}
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state=initial_state,
        )
        # The Head's instruction references {analyst_report}, which ADK
        # substitutes from session state. Whether the user message also
        # carries the raw video/deck Parts (same as the analyst saw) is
        # program-specific — see ProgramConfig.head_include_media. When
        # False (R2B), the Head scores from analyst_report text alone; the
        # grader (which still sees full media) already verified it.
        parts = self._build_parts(state, include_media=self.program_config.head_include_media)
        async for _event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=parts),
            run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS_HEAD),
        ):
            pass
        session = await session_service.get_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise RuntimeError("Head session disappeared before result collection.")
        head_output = session.state.get("head_score")
        if head_output is None:
            raise RuntimeError("Head agent produced no output.")
        result = _as_dict(head_output)

        # Convert flat/named schema outputs (Fellowship V2, Alchemist, R2B)
        # to the criterion_scores / criterion_rationale dict format that
        # _average_head_samples expects.
        if self.program_config.program == "fellowship_v2" and "criterion_scores" not in result:
            result = self._convert_fellowship_v2_head(result)
        elif self.program_config.program == "alchemist" and "criterion_scores" not in result:
            result = self._convert_alchemist_head(result)
        elif self.program_config.program == "r2b" and "criterion_scores" not in result:
            result = self._convert_r2b_head(result)

        return result

    def _convert_fellowship_v2_head(self, flat: dict) -> dict:
        """Convert FellowshipV2HeadScore flat fields to dict format."""
        criteria_map = {
            "Originality": "Originality",
            "Approach": "Approach",
            "Personal_connection": "Personal connection",
            "Concreteness": "Concreteness",
            "Credibility_in_context": "Credibility in context",
            "Trajectory": "Trajectory",
            "Program_fit": "Program fit",
            "Regional_relevance": "Regional relevance",
            "Communication_quality": "Communication quality",
        }
        criterion_scores = {}
        criterion_rationale = {}
        for field_name, criterion_name in criteria_map.items():
            criterion_scores[criterion_name] = flat.get(field_name, 5)
            criterion_rationale[criterion_name] = flat.get(f"{field_name}_rationale", "")
        return {
            "criterion_scores": criterion_scores,
            "criterion_rationale": criterion_rationale,
            "final_score": flat.get("final_score", 5.0),
            "confidence": flat.get("confidence", "medium"),
            "contradiction_found": flat.get("contradiction_found", False),
            "contradiction_reason": flat.get("contradiction_reason", ""),
            "disqualifying_issue_found": flat.get("disqualifying_issue_found", False),
            "disqualifying_issue_type": flat.get("disqualifying_issue_type", "none"),
            "disqualifying_issue_reason": flat.get("disqualifying_issue_reason", ""),
        }

    def _convert_alchemist_head(self, flat: dict) -> dict:
        """Convert AlchemistHeadScore flat fields to dict format."""
        criteria_map = {
            "Product_MVP_Innovation": "Product/MVP & Innovation",
            "Market_Potential": "Market Potential",
            "Scalability_US_Market": "Scalability & Readiness for the U.S. Market",
            "Team_Strength": "Team Strength",
        }
        criterion_scores = {}
        criterion_rationale = {}
        for field_name, criterion_name in criteria_map.items():
            criterion_scores[criterion_name] = flat.get(field_name, 5)
            criterion_rationale[criterion_name] = flat.get(f"{field_name}_rationale", "")
        return {
            "criterion_scores": criterion_scores,
            "criterion_rationale": criterion_rationale,
            "final_score": flat.get("final_score", 5.0),
            "confidence": flat.get("confidence", "medium"),
            "contradiction_found": flat.get("contradiction_found", False),
            "contradiction_reason": flat.get("contradiction_reason", ""),
            "disqualifying_issue_found": flat.get("disqualifying_issue_found", False),
            "disqualifying_issue_type": flat.get("disqualifying_issue_type", "none"),
            "disqualifying_issue_reason": flat.get("disqualifying_issue_reason", ""),
        }

    def _convert_r2b_head(self, flat: dict) -> dict:
        """Convert R2BHeadScoreNamed flat fields to dict format."""
        criteria_map = {
            "Problem_Solution": "Problem & Solution",
            "Market_Potential": "Market Potential",
            "Product_MVP_Innovation": "Product/MVP & Innovation",
            "Team_Strength": "Team Strength",
            "Business_Model": "Business Model",
            "Presentation_Clarity": "Presentation & Clarity",
        }
        criterion_scores = {}
        criterion_rationale = {}
        for field_name, criterion_name in criteria_map.items():
            criterion_scores[criterion_name] = flat.get(field_name, 5)
            criterion_rationale[criterion_name] = flat.get(f"{field_name}_rationale", "")
        return {
            "criterion_scores": criterion_scores,
            "criterion_rationale": criterion_rationale,
            "final_score": flat.get("final_score", 5.0),
            "confidence": flat.get("confidence", "medium"),
            "contradiction_found": flat.get("contradiction_found", False),
            "contradiction_reason": flat.get("contradiction_reason", ""),
            "disqualifying_issue_found": flat.get("disqualifying_issue_found", False),
            "disqualifying_issue_type": flat.get("disqualifying_issue_type", "none"),
            "disqualifying_issue_reason": flat.get("disqualifying_issue_reason", ""),
        }

    def _average_head_samples(self, samples: list[dict]) -> dict:
        """Average criterion scores across N_SAMPLES Head runs.

        Per spec:
        - Average the 6 criterion scores across runs.
        - Final score = average of averaged criterion scores (equivalent to
          averaging all criterion scores, since equal weights).
        - Select rationale from the run whose final_score is closest to the
          averaged final score (closest-rationale selection).
        - Confidence: most conservative across samples.

        Failed runs (exceptions, schema errors) are excluded. If ALL runs
        fail, escalate to human review.
        """
        valid = [s for s in samples if s and s.get("criterion_scores")]
        if not valid:
            return {
                "score": None,
                "reasoning": (
                    "All Head samples failed to produce scores. "
                    "Manual review required."
                ),
                "confidence": "n/a",
                "human_review_flag": True,
            }

        # A confirmed, material contradiction (deck vs video vs website vs
        # application text disagreeing on a real claim), OR a broader
        # disqualifying issue the Head itself noticed (fraud, a lie,
        # a materially suspicious application) even though the grader had
        # already approved the evidence — either overrides the whole
        # application's score to 0, regardless of the averaged criterion
        # scores. Any single valid sample flagging either is enough — a
        # false negative here is worse than a false positive, and a human
        # reviews the flagged reason before it's treated as final.
        contradiction_samples = [s for s in valid if s.get("contradiction_found")]
        disqualified_samples = [s for s in valid if s.get("disqualifying_issue_found")]
        # Deduplicated: at temp=0 all N samples typically flag the SAME
        # issue verbatim, and the old plain join wrote it 3x into the sheet.
        flag_reasons = list(dict.fromkeys(
            r for r in (
                [s.get("contradiction_reason", "").strip() for s in contradiction_samples]
                + [
                    f"{s.get('disqualifying_issue_type', 'issue')}: "
                    f"{s.get('disqualifying_issue_reason', '').strip()}"
                    for s in disqualified_samples
                    if s.get("disqualifying_issue_reason", "").strip()
                ]
            ) if r
        ))
        if (contradiction_samples or disqualified_samples) and self.program_config.contradiction_auto_zero:
            all_reasons = "; ".join(flag_reasons)
            return {
                "score": 0,
                "reasoning": json.dumps(
                    {
                        "criterion_scores": {c: 0 for c in self.program_config.rubric_criteria},
                        "criterion_rationale": {
                            c: f"Application scored 0: confirmed issue — {all_reasons}"
                            for c in self.program_config.rubric_criteria
                        },
                        "n_samples": len(samples),
                        "n_valid": len(valid),
                        "n_failed": len(samples) - len(valid),
                        "contradiction_flagged_by": len(contradiction_samples),
                        "disqualifying_issue_flagged_by": len(disqualified_samples),
                    },
                    ensure_ascii=False,
                ),
                "confidence": "high",
                "human_review_flag": True,
            }

        # Average each criterion score across runs.
        criteria = self.program_config.rubric_criteria
        avg_criterion_scores: dict[str, float] = {}
        for criterion in criteria:
            vals = [
                s["criterion_scores"][criterion]
                for s in valid
                if criterion in s["criterion_scores"]
            ]
            if vals:
                avg_criterion_scores[criterion] = round(sum(vals) / len(vals), 2)

        avg_final = round(
            sum(avg_criterion_scores.values()) / len(avg_criterion_scores)
        )

        # Closest-rationale selection: pick the run whose final_score is
        # closest to the averaged final score. Use that run's rationale.
        closest = min(
            valid,
            key=lambda s: abs(
                s.get("final_score", avg_final) - avg_final
            ),
        )
        selected_rationale = closest.get("criterion_rationale", {})

        # Conservative confidence: pick the lowest across all valid samples.
        rank = {"low": 0, "medium": 1, "high": 2, "n/a": -1}
        confs = [s.get("confidence", "medium") for s in valid]
        consensus_conf = min(confs, key=lambda c: rank.get(c, 1)) if confs else "medium"

        payload = {
            "criterion_scores": avg_criterion_scores,
            "criterion_rationale": selected_rationale,
            "n_samples": len(samples),
            "n_valid": len(valid),
            "n_failed": len(samples) - len(valid),
            "sample_scores": [s.get("final_score") for s in valid],
            "selected_from_sample": valid.index(closest) + 1,
        }
        # contradiction_auto_zero=False (R2B): an inconsistency was flagged
        # but does NOT zero the application — the Head already priced it
        # into the relevant criterion scores (see R2B_HEAD_INSTRUCTION).
        # Surface the flagged reason(s) in the notes and mark the row for
        # human review; disqualification is the human reviewer's decision.
        if flag_reasons:
            payload["inconsistency_flags"] = flag_reasons
        return {
            "score": avg_final,
            "reasoning": json.dumps(payload, ensure_ascii=False),
            "confidence": consensus_conf,
            "human_review_flag": bool(flag_reasons),
        }

    async def _run_r2b(self, state: dict) -> dict:
        """Verify loop once, then Head N_SAMPLES times, average."""
        # Step 1: verify loop (analyst -> grader, max 3 iterations).
        verify_state = await self._run_r2b_verify_loop(state)

        # If the verify loop exhausted without approval, it already set
        # final_result + human_review_flag. Return as-is.
        if verify_state.get("human_review_flag") or not verify_state.get(
            "evidence_approved"
        ):
            final_result = verify_state.get("final_result", {
                "score": None,
                "reasoning": "Evidence was not approved by the grader.",
                "confidence": "n/a",
            })
            return {
                "final_result": final_result,
                "human_review_flag": True,
                "n_samples": 0,
                "n_valid": 0,
            }

        # Step 2: run the Head N_SAMPLES times on the approved evidence.
        approved_evidence = verify_state.get("analyst_report")
        if not approved_evidence:
            return {
                "final_result": {
                    "score": None,
                    "reasoning": (
                        "Evidence was approved but no analyst_report was found "
                        "in session state. Manual review required."
                    ),
                    "confidence": "n/a",
                },
                "human_review_flag": True,
                "n_samples": 0,
                "n_valid": 0,
            }

        # First attempt at this ran all N_SAMPLES Head calls concurrently
        # via asyncio.gather unconditionally (no shared mutable state
        # between them, so it looked safe). Confirmed live it wasn't when
        # media was attached: R2B embedded video inline as raw bytes
        # (Part.from_bytes, up to VERTEX_INLINE_VIDEO_MAX_BYTES=95MB per
        # call — see video_ingestion.py), so N_SAMPLES concurrent calls
        # meant up to N_SAMPLES x that payload in flight at once through
        # the same shared API client — all 3 samples failed simultaneously
        # with `400 INVALID_ARGUMENT`. head_include_media=False fixes this
        # at the root instead of working around it: the Head no longer
        # receives the video/deck Part at all (see _build_parts's
        # include_media), so each call's payload is just text —
        # parallelizing it is safe by construction, not by luck. R2B,
        # Alchemist, and Fellowship V2 (2026-07-21 session) all set this
        # False now, so every program takes the parallel path below; a
        # future program that sets head_include_media=True would fall
        # back to the sequential branch, which stays here for that case.
        async def _run_sample(i: int) -> dict:
            try:
                return await self._run_head_once(state, approved_evidence, i)
            except Exception:
                # A single Head sample failing (API error, schema validation)
                # doesn't kill the row — average whatever succeeded.
                return {}

        if self.program_config.head_include_media:
            samples = []
            for i in range(self.n_samples):
                samples.append(await _run_sample(i))
        else:
            samples = list(await asyncio.gather(
                *(_run_sample(i) for i in range(self.n_samples))
            ))

        # Step 3: average + closest-rationale selection.
        averaged = self._average_head_samples(samples)
        return {
            "final_result": {
                "score": averaged["score"],
                "reasoning": averaged["reasoning"],
                "confidence": averaged["confidence"],
            },
            "human_review_flag": averaged["human_review_flag"],
            "n_samples": len(samples),
            "n_valid": len([s for s in samples if s and s.get("criterion_scores")]),
        }

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def _invoke_async(self, state: dict) -> dict:
        """Run the review workflow and return final state with final_result."""
        return await self._run_r2b(state)

    def invoke(self, state: dict) -> dict:
        loop = _thread_event_loop()
        task = loop.create_task(self._invoke_async(state))
        row_id = state.get("row_id") or f"anon_{id(task)}"
        with _active_lock:
            _active_tasks[row_id] = (loop, task)
        try:
            return loop.run_until_complete(task)
        finally:
            with _active_lock:
                _active_tasks.pop(row_id, None)


def _as_dict(value) -> dict:
    """Coerce ADK structured output (Pydantic, dict, or JSON str) to dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")

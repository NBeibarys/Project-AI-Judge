"""Synchronous batch-facing wrapper around the asynchronous ADK workflow.

All programs use the separate-head architecture:
  1. Run the analyst->grader VERIFY loop ONCE (max 3 iterations). The grader
     does not score; it only approves or rejects the evidence.
  2. If the evidence is approved, run the Head scorer N_SAMPLES times
     (default 5) at temp=0. Each Head run independently scores the SAME
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
import uuid

import certifi
from google.adk.agents import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agent import build_head_agent, build_root_agent
from ..programs import ProgramConfig

APP_NAME = "fellowship_review"

# Default number of Head samples to run and average. The literature
# (arXiv:2606.26185, Perea multi-judge playbook) recommends 2-3 samples;
# 5 is the sweet spot for variance reduction without excessive cost on
# every row.
DEFAULT_N_SAMPLES = 5
# Cap LLM calls per sample:
#   Verify loop (R2B/Alchemist): analyst + grader × 3 iterations = 6 worst case.
#   Verify loop (Fellowship V2): analyst + web_verifier + grader × 3 = 9
#   worst case, plus google_search rounds inside web_verifier. 20 gives
#   headroom for the search tool calls without being unbounded.
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
        n_samples: int = DEFAULT_N_SAMPLES,
    ):
        # certifi provides a consistent trust store across deployments.
        os.environ["SSL_CERT_FILE"] = certifi.where()
        # Keep only immutable configuration: batch threads must not share agent state.
        self.analyzer_model = analyzer_model
        self.grader_model = grader_model
        self.program_config = program_config
        # Head model defaults to the grader model (same Gemini 3.5-flash, temp=0).
        self.head_model = head_model or grader_model
        self.n_samples = max(1, n_samples)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_parts(self, state: dict) -> list:
        """Build the multimodal input parts for an agent run.

        Handles two URI schemes:
        - https://generativelanguage.googleapis.com/... (Files API, Developer API)
          → Part.from_uri
        - file:///local/path (Vertex AI inline, avoids GCS)
          → Part.from_bytes (read file content into memory)
        """
        parts = []
        if state.get("video_url") and not state.get("video_requires_url_context"):
            mime_type = state.get("video_mime_type", "video/mp4")
            uri = state["video_url"]
            if uri.startswith("file://"):
                # Vertex AI: read local file as inline bytes.
                local_path = uri.replace("file://", "", 1)
                with open(local_path, "rb") as f:
                    parts.append(types.Part.from_bytes(
                        data=f.read(), mime_type=mime_type,
                    ))
            else:
                parts.append(types.Part.from_uri(
                    file_uri=uri, mime_type=mime_type,
                ))
        # Alchemist: pitch deck PDF as a multimodal Part (required source).
        if state.get("pitch_deck_url"):
            deck_mime = state.get("pitch_deck_mime_type", "application/pdf")
            deck_uri = state["pitch_deck_url"]
            if deck_uri.startswith("file://"):
                local_path = deck_uri.replace("file://", "", 1)
                with open(local_path, "rb") as f:
                    parts.append(types.Part.from_bytes(
                        data=f.read(), mime_type=deck_mime,
                    ))
            else:
                parts.append(types.Part.from_uri(
                    file_uri=deck_uri, mime_type=deck_mime,
                ))
        text = state.get("raw_row_text", "")
        if state.get("video_requires_url_context"):
            text += (
                "\n\nVIDEO WEBPAGE REQUIRES URL CONTEXT: "
                f"{state['video_url']}"
            )
        if state.get("video_error"):
            text += f"\n\nVIDEO UNAVAILABLE: {state['video_error']}"
        if state.get("pitch_deck_error"):
            text += f"\n\nPITCH DECK UNAVAILABLE: {state['pitch_deck_error']}"
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
        web_verification_report: dict | None = None,
    ) -> dict:
        """Run the Head scorer once on the approved evidence.

        Returns the Head's R2BHeadScore output as a dict
        (criterion_scores, criterion_rationale, final_score, confidence, ...).
        Raises on failure (caller catches and excludes from average).

        web_verification_report is the output of the web_verifier agent
        (Fellowship V2 only). When provided, it is injected into session
        state so the Head's instruction template ({web_verification_report})
        resolves. When None (R2B/Alchemist), the Head instruction's
        {web_verification_report} placeholder is not referenced.
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
        # For Fellowship V2, we also inject web_verification_report so the
        # Head can use verification tags (verified/unverified/contradicted).
        initial_state: dict = {
            "analyst_report": approved_evidence,
            "sample_idx": sample_idx,
        }
        if web_verification_report is not None:
            initial_state["web_verification_report"] = web_verification_report
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state=initial_state,
        )
        # The Head's instruction references {analyst_report}, which ADK
        # substitutes from session state. The user message carries the
        # video/text parts (same as the analyst saw) so the Head can ground
        # its rationale in the source, not just the analyst's summary.
        parts = self._build_parts(state)
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

        # Convert flat schema (Fellowship V2, Alchemist) to the
        # criterion_scores / criterion_rationale dict format that
        # _average_head_samples expects.
        if self.program_config.program == "fellowship_v2" and "criterion_scores" not in result:
            result = self._convert_fellowship_v2_head(result)
        elif self.program_config.program == "alchemist" and "criterion_scores" not in result:
            result = self._convert_alchemist_head(result)

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
            "override": flat.get("override", 0.0),
            "override_reasoning": flat.get("override_reasoning", ""),
            "confidence": flat.get("confidence", "medium"),
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
            "override": flat.get("override", 0.0),
            "override_reasoning": flat.get("override_reasoning", ""),
            "confidence": flat.get("confidence", "medium"),
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
        selected_confidence = closest.get("confidence", "medium")

        # Conservative confidence: pick the lowest across all valid samples.
        rank = {"low": 0, "medium": 1, "high": 2, "n/a": -1}
        confs = [s.get("confidence", "medium") for s in valid]
        consensus_conf = min(confs, key=lambda c: rank.get(c, 1)) if confs else "medium"

        return {
            "score": avg_final,
            "reasoning": json.dumps(
                {
                    "criterion_scores": avg_criterion_scores,
                    "criterion_rationale": selected_rationale,
                    "n_samples": len(samples),
                    "n_valid": len(valid),
                    "n_failed": len(samples) - len(valid),
                    "sample_scores": [s.get("final_score") for s in valid],
                    "selected_from_sample": valid.index(closest) + 1,
                },
                ensure_ascii=False,
            ),
            "confidence": consensus_conf or selected_confidence,
            "human_review_flag": False,
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

        # Web verifier was removed (3-agent pipeline). Set to None so
        # _run_head_once skips injecting it into session state.
        web_verification_report = None

        samples = []
        for i in range(self.n_samples):
            try:
                sample = await self._run_head_once(
                    state, approved_evidence, i, web_verification_report
                )
                samples.append(sample)
            except Exception:
                # A single Head sample failing (API error, schema validation)
                # doesn't kill the row — average whatever succeeded.
                samples.append({})

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
        return asyncio.run(self._invoke_async(state))


def _as_dict(value) -> dict:
    """Coerce ADK structured output (Pydantic, dict, or JSON str) to dict."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")

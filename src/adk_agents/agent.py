"""Google ADK agents for the review workflow.

All programs use the separate-head architecture with 3 distinct roles:
  analyst -> grader -> R2BApprovalGate, in a LoopAgent (max 3 iterations).
    - analyst (LLM): extracts evidence per criterion. Does NOT score.
    - grader (LLM, temp=0): verifies analyst evidence only. Does NOT score.
      approve -> gate exits the loop; reject -> analyst revises.
  head (LLM, temp=0): a SEPARATE agent run AFTER the verify loop exits with
    approval. Scores approved evidence 1-10 per criterion, writes rationale
    BEFORE score (rationale-before-score CoT), averages criterion scores
    into a final score. The Head is re-run N_SAMPLES times by the workflow
    and its criterion scores are averaged, with rationale selected from the
    run closest to the average.

The Head is NOT part of the LoopAgent — it runs after the loop, on the
approved evidence, so multi-sample averaging only re-runs the Head (cheap),
not the whole analyst->grader loop (expensive).
"""
import json
import os
import re
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent, InvocationContext, LoopAgent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.models.google_llm import Gemini
from google.genai import types

from ..programs import ProgramConfig, get_program_config
from .schemas import (
    AlchemistAnalystReport,
    AlchemistHeadScore,
    AnalystReport,
    FellowshipV2AnalystReport,
    FellowshipV2HeadScore,
    R2BAnalystReport,
    R2BGraderVerdict,
    R2BHeadScore,
    R2BHeadScoreNamed,
)

# Grader/Head/Analyst temperature: the LLM-as-judge literature
# (arXiv:2603.28304, arXiv:2606.26185) recommends low temperature for both
# evidence extraction and scoring. temp=0 does NOT fully eliminate variance
# (hence multi-sample averaging of the Head), but it reduces run-to-run
# jitter. Per spec all three agents run at temp=0.
GRADER_TEMPERATURE = 0.0

# Determinism seed: Gemini supports a seed parameter so that the same input
# produces the same output across runs. This makes the multi-sample Head
# averaging meaningful (residual variance comes only from backend routing,
# not from the model's own sampling) and makes re-runs reproducible for
# audit. Same seed for all three agents (analyst, grader, head).
DETERMINISM_SEED = 7524

# On the Developer API (API key), the ADK routes output_schema through the
# SetModelResponseTool (function calling) path because
# can_use_output_schema_with_tools() returns False for non-Vertex. When a
# built-in tool (url_context, google_search) is combined with that function
# declaration, the Developer API requires
# tool_config.include_server_side_tool_invocations=True, otherwise it rejects
# the request with 400: "Please enable tool_config
# .include_server_side_tool_invocations to use Built-in tools with Function
# calling." That config is needed on Gemini 3.x, but 2.x models reject it
# with 400: "Tool call context circulation is not enabled for models/gemini-
# 2.x". So the tool config must be model-aware.
_GEMINI_3_PLUS_RE = re.compile(r"^gemini-(\d+)\.")


def _supports_server_side_tool_invocations(model: str) -> bool:
    """True for Gemini 3.x+ (Developer API requires this flag for built-in
    tools + function calling). False for 2.x, which rejects it."""
    match = _GEMINI_3_PLUS_RE.match(model)
    return bool(match and int(match.group(1)) >= 3)


def _build_tool_config(model: str) -> types.ToolConfig | None:
    """Build the ToolConfig appropriate for the model and API mode.

    Gemini 3.x on the Developer API needs include_server_side_tool_invocations
    to combine built-in tools with function calling. Gemini 2.x does not
    support that flag and rejects it, so None is returned.

    Vertex AI does NOT support include_server_side_tool_invocations at all
    (it's a Developer API-only parameter). On Vertex, return None regardless
    of model version — Vertex handles built-in tools + function calling
    without the flag.
    """
    # Vertex AI mode: never use this flag (Enterprise Agent Platform rejects it).
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return None
    if _supports_server_side_tool_invocations(model):
        return types.ToolConfig(
            include_server_side_tool_invocations=True,
        )
    return None


def _as_dict(value) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Gate: verify-only grader, no scoring here. The Head scores after.
# ---------------------------------------------------------------------------

def r2b_gate_decision(verdict: dict, attempt: int, max_iterations: int) -> tuple[bool, bool]:
    """Return (approved, exhausted) for the analyst->grader verify-loop routing.

    Shared by all programs (despite the r2b-specific name — see
    R2BApprovalGate). approve=true means evidence is grounded and complete;
    the gate exits the loop so the Head can score. max_iterations comes from
    ProgramConfig.max_verify_iterations, passed in by the caller rather than
    hardcoded here — the loop's own max_iterations and this exhaustion
    check used to be two separately-hardcoded "3"s that could drift apart
    (lowering one without the other leaves the gate's exhaustion path unable
    to ever trigger via attempt count, since the loop hard-stops first). A
    confirmed disqualifying issue (see ``disqualifying_issue_found`` in
    R2BGraderVerdict) always counts as exhausted immediately, regardless of
    attempt count — a lie can't be fixed by another analyst revision.
    """
    approved = bool(verdict.get("approved"))
    disqualified = bool(verdict.get("disqualifying_issue_found"))
    exhausted = disqualified or ((not approved) and attempt >= max_iterations)
    return approved, exhausted


class R2BApprovalGate(BaseAgent):
    """Zero-model routing step for the analyst->grader verify loop (all programs).

    Reads the grader's verify verdict. If approved, marks the evidence as
    approved and exits the loop (the Head scores separately, after). If
    rejected, sends feedback back to the analyst for revision. After
    max_iterations attempts without approval, escalates to human review.

    This gate does NOT produce a final_result with a score — that's the
    Head's job. It only sets ``evidence_approved=true`` on approval, or
    ``human_review_flag=true`` on exhaustion.
    """

    # Mirrors ProgramConfig.contradiction_auto_zero. When False (R2B), a
    # grader-side disqualifying flag no longer short-circuits the loop to
    # a definitive 0 — the inconsistency is folded into the feedback so
    # the analyst records it as evidence, and the Head prices it into the
    # relevant criterion scores; zeroing is the human reviewer's call.
    contradiction_auto_zero: bool = True
    # Mirrors ProgramConfig.max_verify_iterations — must match the same
    # LoopAgent's max_iterations (see build_root_agent) or the exhaustion
    # path below can never trigger via attempt count. No default on
    # purpose: a stale default here is exactly the drift this mirroring
    # exists to prevent, so the caller must state the program's cap.
    max_iterations: int

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        attempt = int(state.get("attempt", 1))
        verdict = _as_dict(state.get("grader_verdict", {}))
        approved, exhausted = r2b_gate_decision(verdict, attempt, self.max_iterations)
        disqualified = (
            bool(verdict.get("disqualifying_issue_found"))
            and self.contradiction_auto_zero
        )
        if verdict.get("disqualifying_issue_found") and not self.contradiction_auto_zero:
            # Keep the observation, drop the verdict: make sure the issue
            # text reaches the analyst's revision (or simply the record)
            # via feedback instead of ending the review with a 0.
            issue_note = (
                f"[INCONSISTENCY NOTED — record as evidence, do not treat "
                f"as disqualifying] {verdict.get('disqualifying_issue_reason', '')}"
            )
            verdict = {
                **verdict,
                "feedback": "\n".join(
                    part for part in (verdict.get("feedback", ""), issue_note) if part
                ),
            }

        delta = {
            "grader_feedback": verdict.get("feedback", ""),
            "attempt": attempt if approved else attempt + 1,
        }
        if approved:
            delta["evidence_approved"] = True
            delta["human_review_flag"] = False
        elif disqualified:
            # The grader confirmed the applicant (not the analyst) made a
            # false/fabricated/materially misleading claim. This can't be
            # fixed by another analyst revision, so it exits the loop
            # immediately (even on attempt 1) with a definitive 0 — not the
            # generic "rejected 3 times, score: None" escalation, which
            # left these rows stuck in human-review limbo with no score.
            issue_type = verdict.get("disqualifying_issue_type", "none")
            issue_reason = verdict.get("disqualifying_issue_reason", "")
            delta["evidence_approved"] = False
            delta["final_result"] = {
                "score": 0,
                "reasoning": (
                    f"Application scored 0: confirmed {issue_type} — "
                    f"{issue_reason}"
                ),
                "confidence": "high",
            }
            delta["human_review_flag"] = True
        elif exhausted:
            delta["evidence_approved"] = False
            delta["final_result"] = {
                "score": None,
                "reasoning": (
                    "Analyst evidence was rejected 3 times. Last grader "
                    f"feedback: {verdict.get('feedback', '')}"
                ),
                "confidence": "n/a",
            }
            delta["human_review_flag"] = True

        message = delta.get("final_result") or {
            "status": "revision_requested",
            "feedback": delta["grader_feedback"],
        }
        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=json.dumps(message))],
            ),
            actions=EventActions(
                state_delta=delta,
                escalate=approved or exhausted,
            ),
        )


# ---------------------------------------------------------------------------
# Agent builders.
# ---------------------------------------------------------------------------

def build_root_agent(
    analyzer_model: str,
    grader_model: str,
    program_config: ProgramConfig,
) -> LoopAgent:
    """Build the analyst->grader review loop.

    The grader VERIFIES ONLY (R2BGraderVerdict schema, no score). The
    R2BApprovalGate exits the loop on approval without scoring — the Head
    scores separately (see build_head_agent) after the loop, so it can be
    re-run for multi-sample averaging without re-running the expensive
    analyst->grader loop.
    """
    program_config.apply_active_criteria()

    # Use explicit schema with named criteria fields for Fellowship V2 and
    # Alchemist so Gemini knows exactly what keys to fill. Generic
    # dict[str, ...] produces empty results because the model doesn't know
    # the keys.
    if program_config.program == "fellowship_v2":
        analyst_schema = FellowshipV2AnalystReport
    elif program_config.program == "alchemist":
        analyst_schema = AlchemistAnalystReport
    elif program_config.program == "r2b":
        analyst_schema = R2BAnalystReport
    else:
        analyst_schema = AnalystReport

    analyst = Agent(
        name="analyst",
        description="Extracts grounded rubric evidence from application text and video.",
        model=Gemini(
            model=analyzer_model,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                exp_base=2,
                initial_delay=2,
                http_status_codes=[429],
            ),
        ),
        # Spec: all three agents (analyst, grader, head) run at temp=0.
        # The LLM-as-judge literature (arXiv:2603.28304, arXiv:2606.26185)
        # recommends low temperature for both evidence extraction and
        # scoring; multi-sample averaging of the Head handles the residual
        # variance that temp=0 cannot fully eliminate.
        generate_content_config=types.GenerateContentConfig(
            temperature=GRADER_TEMPERATURE,
            seed=DETERMINISM_SEED,
            # Explicit per-program thinking level (Gemini 3.x;
            # ProgramConfig.analyst_thinking_level) rather than the model's
            # own dynamic/auto reasoning-budget choice. Dynamic thinking can
            # allocate a different reasoning budget to the same input across
            # separate runs, which was a likely contributor to observed
            # run-to-run flakiness in whether the verify loop converges
            # (same row, same seed, inconsistent outcomes). Every program
            # currently sets HIGH, but this is now a knob, not an assumption.
            thinking_config=types.ThinkingConfig(
                thinking_level=getattr(
                    types.ThinkingLevel, program_config.analyst_thinking_level
                ),
            ),
            # media_resolution intentionally left unspecified: a live A/B
            # test (LOW/MEDIUM/HIGH on the same real R2B row) showed HIGH
            # costs ~2-3x the analyst/grader latency but is NOT a clean
            # quality win — it caught one visual detail LOW/MEDIUM missed
            # (a product prototype shown on a slide) but also DROPPED the
            # founders' names that LOW/MEDIUM both caught. Run-to-run
            # extraction variance looks at least as large as any resolution
            # effect. Unspecified (Gemini's own default, empirically same
            # cost as LOW/MEDIUM for video) is the settled choice until a
            # multi-run variance test justifies paying HIGH's latency cost.
            # response_mime_type is intentionally omitted: the ADK's
            # set_output_schema sets it when the model supports response_schema
            # directly (Vertex AI). On the Developer API (API key), the ADK
            # uses the SetModelResponseTool (function calling) path instead,
            # and response_mime_type=application/json CONFLICTS with function
            # calling, causing 400 INVALID_ARGUMENT.
            tool_config=_build_tool_config(analyzer_model),
        ),
        instruction=program_config.analyst_instruction,
        output_schema=analyst_schema,
        output_key="analyst_report",
        # google_search was removed from the analyst because it conflicts
        # with output_schema - the model does tool calls instead of producing
        # structured JSON, causing all evidence to be rejected after 3 iterations.
        # Web research will be implemented as a separate pre-processing step.
        # url_context was also removed: it existed only for the "webpage with
        # no discoverable direct video" fallback, which now raises instead of
        # handing the analyst a page to interpret live (see video_urls.py) —
        # it also caused a real production failure (a 400 from its own ~15MB
        # fetch cap on a webpage source).
        tools=[],
        timeout=240,
    )
    # Grader verifies only, does not score.
    grader = Agent(
        name="grader",
        description="Verifies analyst evidence only. Does not score.",
        model=Gemini(
            model=grader_model,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                exp_base=2,
                initial_delay=2,
                http_status_codes=[429],
            ),
        ),
        generate_content_config=types.GenerateContentConfig(
            temperature=GRADER_TEMPERATURE,
            seed=DETERMINISM_SEED,
            # Explicit per-program level (ProgramConfig.grader_thinking_level)
            # rather than hardcoded — every program currently sets HIGH, but
            # this is now a knob, not an assumption.
            thinking_config=types.ThinkingConfig(
                thinking_level=getattr(
                    types.ThinkingLevel, program_config.grader_thinking_level
                ),
            ),
            # media_resolution intentionally left unspecified — see the
            # analyst's config above for the A/B test that settled this.
            tool_config=_build_tool_config(grader_model),
        ),
        instruction=program_config.grader_instruction,
        output_schema=R2BGraderVerdict,
        output_key="grader_verdict",
        # url_context removed — see analyst's tools comment above.
        tools=[],
        timeout=240,
    )
    gate = R2BApprovalGate(
        name="approval_gate",
        contradiction_auto_zero=program_config.contradiction_auto_zero,
        max_iterations=program_config.max_verify_iterations,
    )
    max_iter = program_config.max_verify_iterations
    # Web verifier was removed: the 3-agent pipeline (analyst -> grader -> gate)
    # is reliable and the rubric already handles unverified claims via the
    # "each level up requires MORE EVIDENCE" rule. See git history for the
    # web_verifier experiment if it needs to be revived.
    sub_agents = [analyst, grader, gate]
    return LoopAgent(
        name=f"{program_config.program}_review",
        description="Analyst and grader review loop.",
        sub_agents=sub_agents,
        max_iterations=max_iter,
    )


def build_head_agent(
    head_model: str,
    program_config: ProgramConfig,
) -> Agent:
    """Build the separate Head scorer.

    The Head scores APPROVED evidence — it does not verify. It runs after the
    analyst->grader loop exits with approval, on the same analyst_report. The
    workflow re-runs this Head N_SAMPLES times and averages the criterion
    scores, selecting rationale from the run closest to the average.
    """
    program_config.apply_active_criteria()

    # Use explicit schema with named criteria fields for Fellowship V2 and
    # Alchemist. The generic R2BHeadScore dict schema produces empty results
    # because the model doesn't know the criterion names.
    if program_config.program == "fellowship_v2":
        head_schema = FellowshipV2HeadScore
    elif program_config.program == "alchemist":
        head_schema = AlchemistHeadScore
    elif program_config.program == "r2b":
        head_schema = R2BHeadScoreNamed
    else:
        head_schema = R2BHeadScore

    return Agent(
        name="head",
        description="Scores approved evidence only. Does not verify.",
        model=Gemini(
            model=head_model,
            retry_options=types.HttpRetryOptions(
                attempts=5,
                exp_base=2,
                initial_delay=2,
                http_status_codes=[429],
            ),
        ),
        generate_content_config=types.GenerateContentConfig(
            temperature=GRADER_TEMPERATURE,
            seed=DETERMINISM_SEED,
            # Explicit per-program level (ProgramConfig.head_thinking_level),
            # not derived from head_include_media — the two used to be
            # coupled here, but they're independent knobs now (a program
            # could in principle want head_include_media=False with HIGH
            # thinking, or vice versa). R2B/Alchemist set LOW — VALIDATED
            # live for R2B, in two stages. Stage 1: LOW alone (without the
            # Head's instruction pointing at the analyst's video_notes
            # scratchpad field) showed a systematic severity bias; pairing
            # LOW with an explicit video_notes reference in
            # R2B_HEAD_INSTRUCTION recovered baseline quality across 3 rows
            # (every criterion within ±1 of the HIGH+video baseline; one
            # disqualification row matched exactly). Stage 2: restoring the
            # video to the LOW Head was also tested (twice, same row, same
            # config) and REJECTED — it was unstable across runs (total
            # 5.67 vs 4.67, single criteria swinging 3 points between
            # identical invocations, with all 3 within-run samples agreeing
            # exactly, so N_SAMPLES averaging gives no protection) and its
            # rationale showed the Head slipping back into re-verifying
            # grader-approved evidence ("no demo or visual proof was
            # provided to substantiate") instead of scoring it. LOW +
            # text-only is the settled config for R2B: fastest (~4s/sample
            # vs ~150s at HIGH), closest to baseline, and behaviorally
            # correct for a score-only role. Alchemist adopts the same LOW
            # setting but hasn't been separately live-tested this way yet.
            thinking_config=types.ThinkingConfig(
                thinking_level=getattr(
                    types.ThinkingLevel, program_config.head_thinking_level
                ),
            ),
            # media_resolution intentionally left unspecified — see the
            # analyst's config above for the A/B test that settled this.
            # response_mime_type is intentionally omitted: the ADK's
            # set_output_schema sets it when the model supports response_schema
            # directly (Vertex AI). On the Developer API (API key), the ADK
            # uses the SetModelResponseTool (function calling) path instead,
            # and response_mime_type=application/json CONFLICTS with function
            # calling, causing 400 INVALID_ARGUMENT.
            tool_config=_build_tool_config(head_model),
        ),
        instruction=program_config.head_instruction,
        output_schema=head_schema,
        output_key="head_score",
        # url_context removed — see the analyst's tools comment above.
        tools=[],
        timeout=240,
    )


# Module-level root_agent/app for ADK CLI discovery (adk web / adk run)
# ONLY — the real batch pipeline never references root_agent/app/
# _default_program_config; it builds its own agents via config's real
# values in workflow.py. The "fellowship_v2" fallback below is NOT the
# app's default program (there is no such thing — see main.py, which
# requires PROGRAM explicitly and raises if it's unset). It exists purely
# so this module can be imported without crashing when ANALYZER_MODEL/
# GRADER_MODEL/PROGRAM aren't set yet (see below); it has zero effect on
# what actually gets graded.
#
# No hardcoded model-name fallback here, matching config.py: this used to
# default to a specific Gemini tier independently of config.py's own
# default, so the two could silently disagree about which model runs.
#
# This module executes at import time as a side effect of `workflow.py`
# importing build_root_agent/build_head_agent from this module — i.e. on
# every real batch run, not just standalone ADK CLI usage. So root_agent/app
# must degrade to None rather than raise when ANALYZER_MODEL/GRADER_MODEL
# aren't set yet, otherwise merely importing the batch pipeline would crash
# before Config.from_env() ever gets a chance to give its clearer error.
# Only `adk web`/`adk run` actually touch root_agent/app; the batch pipeline
# never references them (it builds its own agents via config's real values).
_default_program_config = get_program_config(
    os.environ.get("PROGRAM", "fellowship_v2")
)
_cli_analyzer_model = os.environ.get("ANALYZER_MODEL")
_cli_grader_model = os.environ.get("GRADER_MODEL")
if _cli_analyzer_model and _cli_grader_model:
    root_agent = build_root_agent(
        _cli_analyzer_model, _cli_grader_model, _default_program_config,
    )
    app = App(name="adk_agents", root_agent=root_agent)
else:
    root_agent = None
    app = None

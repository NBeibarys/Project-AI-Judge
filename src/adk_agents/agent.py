"""Google ADK agents for fellowship and R2B review workflows.

Two architectures, selected by ProgramConfig:

  Fellowship (uses_separate_head=False) — UNCHANGED from original:
    analyst -> grader_head -> ApprovalGate, in a LoopAgent (max 2 iterations).
    The grader both verifies AND scores in one step (GraderVerdict schema).
    Single run; no multi-sample averaging.

  R2B (uses_separate_head=True) — per spec, 3 distinct roles:
    analyst -> grader -> ApprovalGate, in a LoopAgent (max 3 iterations).
      - analyst (LLM): extracts evidence per criterion from the video. Does
        NOT score.
      - grader (LLM, temp=0): verifies analyst evidence only. Does NOT score.
        approve -> gate exits the loop; reject -> analyst revises.
    head (LLM, temp=0): a SEPARATE agent run AFTER the verify loop exits with
      approval. Scores approved evidence 1-10 per criterion, writes rationale
      BEFORE score (rationale-before-score CoT), averages 6 criterion scores
      into a final score. The Head is re-run N_SAMPLES times by the workflow
      and its criterion scores are averaged, with rationale selected from the
      run closest to the average.

The Head is NOT part of the LoopAgent — it runs after the loop, on the
approved evidence, so multi-sample averaging only re-runs the Head (cheap),
not the whole analyst->grader loop (expensive).
"""
import json
import os
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent, InvocationContext, LoopAgent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.models.google_llm import Gemini
from google.adk.tools import url_context
from google.genai import types

from ..programs import FELLOWSHIP_CONFIG, ProgramConfig
from .schemas import AnalystReport, GraderVerdict, R2BGraderVerdict, R2BHeadScore

# Grader/Head/Analyst temperature: the LLM-as-judge literature
# (arXiv:2603.28304, arXiv:2606.26185) recommends low temperature for both
# evidence extraction and scoring. temp=0 does NOT fully eliminate variance
# (hence multi-sample averaging of the Head), but it reduces run-to-run
# jitter. Per spec all three agents run at temp=0.
GRADER_TEMPERATURE = 0.0


def _as_dict(value) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Fellowship gate (original, unchanged): single-grader approve/reject.
# ---------------------------------------------------------------------------

def gate_decision(verdict: dict, attempt: int) -> tuple[bool, bool]:
    """Return (approved, exhausted) for deterministic loop routing."""
    approved = bool(verdict.get("approved"))
    return approved, (not approved and attempt >= 2)


class ApprovalGate(BaseAgent):
    """Zero-model routing step that exits the loop on approval/exhaustion.

    Fellowship variant: single grader. If approved, publishes final_result
    from the grader's score/reasoning. If rejected, sends feedback back to
    the analyst for one revision. After 2 attempts without approval,
    escalates to human review.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        attempt = int(state.get("attempt", 1))
        verdict = _as_dict(state.get("grader_verdict", {}))
        approved, exhausted = gate_decision(verdict, attempt)

        delta = {
            "grader_feedback": verdict.get("feedback", ""),
            "attempt": attempt if approved else attempt + 1,
        }
        if approved:
            delta["final_result"] = {
                "score": verdict.get("score"),
                "reasoning": verdict.get("reasoning", ""),
                "confidence": verdict.get("confidence", "medium"),
            }
            delta["human_review_flag"] = False
        elif exhausted:
            delta["final_result"] = {
                "score": None,
                "reasoning": (
                    "Analyst evidence was rejected twice. Last grader feedback: "
                    f"{verdict.get('feedback', '')}"
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
# R2B gate: verify-only grader, no scoring here. The Head scores after.
# ---------------------------------------------------------------------------

def r2b_gate_decision(verdict: dict, attempt: int) -> tuple[bool, bool]:
    """Return (approved, exhausted) for R2B verify-loop routing.

    The R2B grader only verifies (no score). approve=true means evidence is
    grounded and complete; the gate exits the loop so the Head can score.
    Max 3 iterations per spec.
    """
    approved = bool(verdict.get("approved"))
    exhausted = (not approved) and attempt >= 3
    return approved, exhausted


class R2BApprovalGate(BaseAgent):
    """Zero-model routing step for the R2B analyst->grader verify loop.

    Reads the grader's verify verdict. If approved, marks the evidence as
    approved and exits the loop (the Head scores separately, after). If
    rejected, sends feedback back to the analyst for revision. After 3
    attempts without approval, escalates to human review.

    This gate does NOT produce a final_result with a score — that's the
    Head's job. It only sets ``evidence_approved=true`` on approval, or
    ``human_review_flag=true`` on exhaustion.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        attempt = int(state.get("attempt", 1))
        verdict = _as_dict(state.get("grader_verdict", {}))
        approved, exhausted = r2b_gate_decision(verdict, attempt)

        delta = {
            "grader_feedback": verdict.get("feedback", ""),
            "attempt": attempt if approved else attempt + 1,
        }
        if approved:
            delta["evidence_approved"] = True
            delta["human_review_flag"] = False
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
    program_config: ProgramConfig = FELLOWSHIP_CONFIG,
) -> LoopAgent:
    """Build the analyst->grader review loop.

    For fellowship (uses_separate_head=False): the grader verifies AND scores
    (GraderVerdict schema). The ApprovalGate publishes the final score.

    For R2B (uses_separate_head=True): the grader VERIFIES ONLY
    (R2BGraderVerdict schema, no score). The R2BApprovalGate exits the loop
    on approval without scoring — the Head scores separately (see
    build_head_agent) after the loop, so it can be re-run for multi-sample
    averaging without re-running the expensive analyst->grader loop.
    """
    program_config.apply_active_criteria()
    analyst = Agent(
        name="analyst",
        description="Extracts grounded rubric evidence from application text and video.",
        model=Gemini(
            model=analyzer_model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        # Spec: all three agents (analyst, grader, head) run at temp=0.
        # The LLM-as-judge literature (arXiv:2603.28304, arXiv:2606.26185)
        # recommends low temperature for both evidence extraction and
        # scoring; multi-sample averaging of the Head handles the residual
        # variance that temp=0 cannot fully eliminate.
        generate_content_config=types.GenerateContentConfig(
            temperature=GRADER_TEMPERATURE,
        ),
        instruction=program_config.analyst_instruction,
        output_schema=AnalystReport,
        output_key="analyst_report",
        tools=[url_context],
        timeout=240,
    )
    if program_config.uses_separate_head:
        # R2B: grader verifies only, does not score.
        grader = Agent(
            name="grader",
            description="Verifies analyst evidence only. Does not score.",
            model=Gemini(
                model=grader_model,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
            generate_content_config=types.GenerateContentConfig(
                temperature=GRADER_TEMPERATURE,
            ),
            instruction=program_config.grader_instruction,
            output_schema=R2BGraderVerdict,
            output_key="grader_verdict",
            tools=[url_context],
            timeout=240,
        )
        gate = R2BApprovalGate(name="approval_gate")
        max_iter = 3
    else:
        # Fellowship: grader verifies and scores in one step.
        grader = Agent(
            name="grader_head",
            description="Independently verifies evidence and publishes the final report.",
            model=Gemini(
                model=grader_model,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
            instruction=program_config.grader_instruction,
            output_schema=GraderVerdict,
            output_key="grader_verdict",
            tools=[url_context],
            timeout=240,
        )
        gate = ApprovalGate(name="approval_gate")
        max_iter = 2
    return LoopAgent(
        name=f"{program_config.program}_review",
        description="Analyst and grader review loop.",
        sub_agents=[analyst, grader, gate],
        max_iterations=max_iter,
    )


def build_head_agent(
    head_model: str,
    program_config: ProgramConfig,
) -> Agent:
    """Build the separate Head scorer (R2B only).

    The Head scores APPROVED evidence — it does not verify. It runs after the
    analyst->grader loop exits with approval, on the same analyst_report. The
    workflow re-runs this Head N_SAMPLES times and averages the criterion
    scores, selecting rationale from the run closest to the average.
    """
    program_config.apply_active_criteria()
    return Agent(
        name="head",
        description="Scores approved evidence only. Does not verify.",
        model=Gemini(
            model=head_model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        generate_content_config=types.GenerateContentConfig(
            temperature=GRADER_TEMPERATURE,
        ),
        instruction=program_config.head_instruction,
        output_schema=R2BHeadScore,
        output_key="head_score",
        tools=[url_context],
        timeout=240,
    )


root_agent = build_root_agent(
    os.environ.get("ANALYZER_MODEL", "gemini-3.5-flash"),
    os.environ.get("GRADER_MODEL", "gemini-3.5-flash"),
)
app = App(name="adk_agents", root_agent=root_agent)

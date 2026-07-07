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
from google.genai import Client as GenAIClient
from google.adk.tools import google_search, url_context
from google.genai import types

from ..programs import ProgramConfig, get_program_config
from .schemas import (
    AlchemistAnalystReport,
    AlchemistHeadScore,
    AnalystReport,
    FellowshipV2AnalystReport,
    FellowshipV2HeadScore,
    FellowshipV2WebVerificationReport,
    R2BGraderVerdict,
    R2BHeadScore,
)

# Grader/Head/Analyst temperature: the LLM-as-judge literature
# (arXiv:2603.28304, arXiv:2606.26185) recommends low temperature for both
# evidence extraction and scoring. temp=0 does NOT fully eliminate variance
# (hence multi-sample averaging of the Head), but it reduces run-to-run
# jitter. Per spec all three agents run at temp=0.
GRADER_TEMPERATURE = 0.0

# Override Gemini's api_client to use the Developer API (API key) instead
# of Vertex AI. When GOOGLE_API_KEY is set, all ADK Gemini calls route
# through the Developer API, avoiding GCP billing entirely.
_DEVELOPER_CLIENT = None
if os.environ.get("GOOGLE_API_KEY"):
    _DEVELOPER_CLIENT = GenAIClient(api_key=os.environ["GOOGLE_API_KEY"])

class DeveloperGemini(Gemini):
    """Gemini subclass that uses the Developer API via API key.

    Falls back to the default Gemini (Vertex AI) when GOOGLE_API_KEY is
    not set, preserving backward compatibility with Vertex deployments.
    """

    @property
    def api_client(self):
        if _DEVELOPER_CLIENT is not None:
            return _DEVELOPER_CLIENT
        return super().api_client

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

def build_web_verifier_agent(
    model: str,
    program_config: ProgramConfig,
) -> Agent:
    """Build the web verification agent (Fellowship V2 only).

    Runs between the analyst and the grader. Takes the analyst_report as
    input (via the {analyst_report} template variable in its instruction)
    and produces a FellowshipV2WebVerificationReport. The report is stored
    under the output_key 'web_verification_report' in session state, so the
    grader and head can reference it via {web_verification_report}.

    This agent CAN use google_search: its output_schema is a simple flat
    schema (FellowshipV2WebVerificationReport), not the complex
    AnalystReport. The output_schema/google_search conflict that forced
    google_search off the analyst does not apply here.
    """
    return Agent(
        name="web_verifier",
        description="Verifies analyst evidence via web search.",
        model=Gemini(
            model=model,
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
            # response_mime_type is intentionally omitted: the ADK's
            # set_output_schema sets it when the model supports response_schema
            # directly (Vertex AI). On the Developer API (API key), the ADK
            # uses the SetModelResponseTool (function calling) path instead,
            # and response_mime_type=application/json CONFLICTS with function
            # calling, causing 400 INVALID_ARGUMENT.
            tool_config=_build_tool_config(model),
        ),
        instruction=program_config.web_verifier_instruction,
        output_schema=FellowshipV2WebVerificationReport,
        output_key="web_verification_report",
        # output_key kept for backward compat but web verifier is no longer
        # in the LoopAgent. See git history for the experiment.
        tools=[google_search],
        timeout=240,
    )


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
    else:
        analyst_schema = AnalystReport

    # Note: google_search was removed from the analyst because it conflicts
    # with output_schema - the model does tool calls instead of producing
    # structured JSON, causing all evidence to be rejected after 3 iterations.
    # Web research will be implemented as a separate pre-processing step.
    analyst_tools = [url_context]

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
            # response_mime_type omitted — see web_verifier comment above.
            tool_config=_build_tool_config(analyzer_model),
        ),
        instruction=program_config.analyst_instruction,
        output_schema=analyst_schema,
        output_key="analyst_report",
        tools=analyst_tools,
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
            tool_config=_build_tool_config(grader_model),
        ),
        instruction=program_config.grader_instruction,
        output_schema=R2BGraderVerdict,
        output_key="grader_verdict",
        tools=[url_context],
        timeout=240,
    )
    gate = R2BApprovalGate(name="approval_gate")
    max_iter = 3
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
            # response_mime_type omitted — see web_verifier comment above.
            tool_config=_build_tool_config(head_model),
        ),
        instruction=program_config.head_instruction,
        output_schema=head_schema,
        output_key="head_score",
        tools=[url_context],
        timeout=240,
    )


# Module-level root_agent/app for ADK CLI discovery. The active program is
# resolved from the PROGRAM env var (default fellowship_v2), mirroring
# main.py. Config.from_env is not called here (no sheet/validation) — that
# happens at batch run time via run_batch.
_default_program_config = get_program_config(
    os.environ.get("PROGRAM", "fellowship_v2")
)
root_agent = build_root_agent(
    os.environ.get("ANALYZER_MODEL", "gemini-3.5-flash"),
    os.environ.get("GRADER_MODEL", "gemini-3.5-flash"),
    _default_program_config,
)
app = App(name="adk_agents", root_agent=root_agent)

"""Google ADK analyst -> grader-head workflow with one optional revision."""
import json
import os
from collections.abc import AsyncGenerator

from google.adk.agents import Agent, BaseAgent, InvocationContext, LoopAgent
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.models.google_llm import Gemini
from google.genai import types

from .prompts import ANALYST_INSTRUCTION, GRADER_HEAD_INSTRUCTION
from .schemas import AnalystReport, GraderVerdict


def _as_dict(value) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise ValueError(f"Unsupported structured agent output: {type(value).__name__}")


def gate_decision(verdict: dict, attempt: int) -> tuple[bool, bool]:
    """Return (approved, exhausted) for deterministic loop routing."""
    approved = bool(verdict.get("approved"))
    return approved, (not approved and attempt >= 2)


class ApprovalGate(BaseAgent):
    """Zero-model routing step that exits the loop on approval/exhaustion."""

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


def build_root_agent(analyzer_model: str, grader_model: str) -> LoopAgent:
    analyst = Agent(
        name="analyst",
        description="Extracts grounded rubric evidence from application text and video.",
        model=Gemini(
            model=analyzer_model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        instruction=ANALYST_INSTRUCTION,
        output_schema=AnalystReport,
        output_key="analyst_report",
        timeout=240,
    )
    grader_head = Agent(
        name="grader_head",
        description="Independently verifies evidence and publishes the final report.",
        model=Gemini(
            model=grader_model,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        instruction=GRADER_HEAD_INSTRUCTION,
        output_schema=GraderVerdict,
        output_key="grader_verdict",
        timeout=240,
    )
    gate = ApprovalGate(name="approval_gate")
    return LoopAgent(
        name="fellowship_review",
        description="Analyst and independent grader-head review loop.",
        sub_agents=[analyst, grader_head, gate],
        max_iterations=2,
    )


root_agent = build_root_agent(
    os.environ.get("ANALYZER_MODEL", "gemini-2.5-flash"),
    os.environ.get("GRADER_MODEL", "gemini-2.5-flash"),
)
app = App(name="adk_agents", root_agent=root_agent)

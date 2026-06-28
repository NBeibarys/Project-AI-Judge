"""Synchronous batch-facing wrapper around the asynchronous ADK workflow."""
import asyncio
import os
import uuid

import certifi
from google.adk.agents import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .agent import build_root_agent

APP_NAME = "fellowship_review"


class AdkReviewWorkflow:
    def __init__(self, analyzer_model: str, grader_model: str):
        # certifi provides a consistent trust store across Windows deployments.
        os.environ["SSL_CERT_FILE"] = certifi.where()
        # Keep only immutable configuration: batch threads must not share agent state.
        self.analyzer_model = analyzer_model
        self.grader_model = grader_model

    async def _invoke_async(self, state: dict) -> dict:
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(self.analyzer_model, self.grader_model),
            session_service=session_service,
        )
        session_id = uuid.uuid4().hex
        user_id = state.get("row_id", "batch-user")
        initial_state = {
            **state,
            "grader_feedback": "",
            "attempt": 1,
            "human_review_flag": False,
        }
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
            state=initial_state,
        )

        parts = []
        if state.get("video_url") and not state.get("video_requires_url_context"):
            mime_type = state.get("video_mime_type")
            if mime_type:
                parts.append(
                    types.Part.from_uri(
                        file_uri=state["video_url"],
                        mime_type=mime_type,
                    )
                )
            else:
                # Native YouTube input is valid without MIME, but Part.from_uri()
                # attempts local extension inference; construct FileData directly.
                parts.append(
                    types.Part(
                        file_data=types.FileData(file_uri=state["video_url"])
                    )
                )
        text = state.get("raw_row_text", "")
        if state.get("video_requires_url_context"):
            text += (
                "\n\nVIDEO WEBPAGE REQUIRES URL CONTEXT: "
                f"{state['video_url']}"
            )
        if state.get("video_error"):
            text += f"\n\nVIDEO UNAVAILABLE: {state['video_error']}"
        parts.append(types.Part(text=text))

        async for _event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=parts),
            run_config=RunConfig(max_llm_calls=4),
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

    def invoke(self, state: dict) -> dict:
        return asyncio.run(self._invoke_async(state))

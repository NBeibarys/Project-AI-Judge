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
        os.environ["SSL_CERT_FILE"] = certifi.where()
        self.root_agent = build_root_agent(analyzer_model, grader_model)

    async def _invoke_async(self, state: dict) -> dict:
        session_service = InMemorySessionService()
        runner = Runner(
            app_name=APP_NAME,
            agent=self.root_agent,
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
        if state.get("video_url"):
            parts.append(
                types.Part.from_uri(
                    file_uri=state["video_url"],
                    mime_type=state.get("video_mime_type", "video/mp4"),
                )
            )
        text = state.get("raw_row_text", "")
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

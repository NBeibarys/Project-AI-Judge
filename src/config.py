"""
Central config, env-driven only — no hardcoded secrets or sheet IDs.
Fail fast at startup (not mid-batch) if required env vars are missing,
since a 100+ row run that dies on row 50 from a bad API key wastes
real API spend.

Gemini-only for now (no Claude credit on this account) — see project
memory for the multi-provider history if Claude support needs reviving.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_range: str
    header_row: int
    top_label_row: int
    service_account_path: str
    analyzer_model: str
    grader_model: str
    max_concurrency: int
    checkpoint_path: str

    @classmethod
    def from_env(cls) -> "Config":
        sheet_id = os.environ.get("FELLOWSHIP_SHEET_ID", "")
        sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "")

        if not sheet_id:
            raise RuntimeError("FELLOWSHIP_SHEET_ID not set")
        if not sa_path or not os.path.isfile(sa_path):
            raise RuntimeError(f"GOOGLE_SERVICE_ACCOUNT_PATH invalid: {sa_path}")
        use_vertex = os.environ.get(
            "GOOGLE_GENAI_USE_VERTEXAI",
            "FALSE",
        ).upper() == "TRUE"
        if use_vertex:
            if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
                raise RuntimeError("GOOGLE_CLOUD_PROJECT not set for Vertex AI")
            if not os.environ.get("GOOGLE_CLOUD_LOCATION"):
                raise RuntimeError("GOOGLE_CLOUD_LOCATION not set for Vertex AI")
        elif not os.environ.get("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY not set for Gemini Developer API")

        return cls(
            sheet_id=sheet_id,
            # Defaults point at "Grading Final" (this project's real sheet),
            # not the raw form-response tab — it already has all input
            # columns duplicated plus the AI/AI Reasoning columns to write
            # to. header_row=2 because that sheet has a merged top label
            # row (reviewer names like "Reviewer A") above the real per-column
            # header row.
            sheet_range=os.environ.get("FELLOWSHIP_SHEET_RANGE", "Grading Final"),
            header_row=int(os.environ.get("FELLOWSHIP_HEADER_ROW", "2")),
            # "AI" / "AI Reasoning" are named on this merged top label row,
            # not the per-column header row — pipeline.py reads it
            # separately and looks up the names there. Letter overrides
            # are only a fallback if a future sheet lacks those names.
            top_label_row=int(os.environ.get("FELLOWSHIP_TOP_LABEL_ROW", "1")),
            service_account_path=sa_path,
            analyzer_model=os.environ.get("ANALYZER_MODEL", "gemini-3.5-flash"),
            grader_model=os.environ.get("GRADER_MODEL", "gemini-3.5-flash"),
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "4")),
            checkpoint_path=os.environ.get("CHECKPOINT_PATH", "checkpoint.json"),
        )

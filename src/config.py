"""
Central config, env-driven only — no hardcoded secrets or sheet IDs.
Fail fast at startup (not mid-batch) if required env vars are missing,
since a 100+ row run that dies on row 50 from a bad API key wastes
real API spend.

Two programs coexist: Fellowship (default) and R2B. The active program is
selected via the ``PROGRAM`` env var (default "fellowship"); each program
owns its own sheet-geometry env-var names so the two sheets never collide.
Fellowship callers who never set PROGRAM get byte-identical behavior to
the original hardcoded config: same env vars (FELLOWSHIP_*), same defaults,
same validation. A single run_batch call is exactly one program — all rows
in a batch share the same criteria set, so the module-level active-criteria
slot set in schemas.py never races across programs within a batch.

Gemini-only for now (no Claude credit on this account) — see project
memory for the multi-provider history if Claude support needs reviving.
"""
import os
from dataclasses import dataclass

from .programs import ProgramConfig, get_program_config


@dataclass(frozen=True)
class Config:
    sheet_id: str
    sheet_range: str
    header_row: int
    top_label_row: int
    service_account_path: str
    analyzer_model: str
    grader_model: str
    head_model: str
    n_samples: int
    max_concurrency: int
    checkpoint_path: str
    program_config: ProgramConfig

    @classmethod
    def from_env(cls, program: str = "fellowship") -> "Config":
        program_config = get_program_config(program)
        sheet_id = os.environ.get(program_config.sheet_id_env, "")
        sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "")

        if not sheet_id:
            raise RuntimeError(f"{program_config.sheet_id_env} not set")
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
            sheet_range=os.environ.get(
                program_config.sheet_range_env, "Grading Final",
            ),
            header_row=int(os.environ.get(
                program_config.header_row_env, str(program_config.default_header_row),
            )),
            top_label_row=int(os.environ.get(
                program_config.top_label_row_env, str(program_config.default_top_label_row),
            )),
            service_account_path=sa_path,
            analyzer_model=os.environ.get("ANALYZER_MODEL", "gemini-3.5-flash"),
            grader_model=os.environ.get("GRADER_MODEL", "gemini-3.5-flash"),
            head_model=os.environ.get("HEAD_MODEL", os.environ.get("GRADER_MODEL", "gemini-3.5-flash")),
            n_samples=int(os.environ.get("N_SAMPLES", "5")),
            max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "8")),
            checkpoint_path=os.environ.get("CHECKPOINT_PATH", "checkpoint.json"),
            program_config=program_config,
        )

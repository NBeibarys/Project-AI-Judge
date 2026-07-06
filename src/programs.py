"""Program configuration: per-program rubric, prompts, and sheet geometry.

Fellowship and R2B coexist. The pipeline selects a ProgramConfig at startup
(via the ``PROGRAM`` env var, default ``fellowship``) and threads it through
the agent builder and workflow so the same LoopAgent core grades with the
right rubric, instructions, and sheet columns.

Architecture differs by program:
  - Fellowship (uses_separate_head=False): analyst + grader (verify+score
    combined in one agent). Single run, no multi-sample averaging.
  - R2B (uses_separate_head=True): analyst + grader (verify only) in a loop,
    then a separate Head agent (score only, after approval). The Head is
    re-run N_SAMPLES times and its criterion scores are averaged, with the
    rationale selected from the run closest to the average (per spec).

Existing fellowship behavior is untouched: the default program is
``fellowship``, and the original env var names (FELLOWSHIP_SHEET_ID, etc.)
still work exactly as before.
"""
from dataclasses import dataclass
from typing import Literal

from .adk_agents.prompts import (
    ANALYST_INSTRUCTION,
    GRADER_HEAD_INSTRUCTION,
    R2B_ANALYST_INSTRUCTION,
    R2B_GRADER_INSTRUCTION,
    R2B_HEAD_INSTRUCTION,
    R2B_RUBRIC_TEXT,
    RUBRIC_TEXT,
)
from .adk_agents.schemas import (
    RUBRIC_CRITERIA,
    RUBRIC_CRITERIA_R2B,
    RUBRIC_WEIGHTS,
    RUBRIC_WEIGHTS_R2B,
    set_active_criteria,
)

ProgramName = Literal["fellowship", "r2b"]


@dataclass(frozen=True)
class ProgramConfig:
    """All per-program knobs the agent builder and pipeline need."""

    program: ProgramName
    rubric_criteria: tuple[str, ...]
    rubric_weights: dict[str, float]
    rubric_text: str
    analyst_instruction: str
    grader_instruction: str
    source_priority: Literal["text_primary", "video_primary"]
    # Whether this program uses a separate Head scorer (R2B) vs. a combined
    # grader-verifies-and-scores agent (fellowship). When True, the workflow
    # runs the analyst->grader verify loop, then a separate Head agent that
    # scores approved evidence; multi-sample averaging re-runs the Head only.
    uses_separate_head: bool
    # Sheet geometry — env var names this program reads.
    sheet_id_env: str
    sheet_range_env: str
    header_row_env: str
    top_label_row_env: str
    # Output columns written back to the sheet.
    score_column_name: str
    reasoning_column_name: str
    # Head scorer instruction (only used when uses_separate_head=True).
    head_instruction: str = ""

    def apply_active_criteria(self) -> None:
        """Set the module-level active criteria the Pydantic validators read.
        Must be called before building agents so the output_schema validators
        enforce the right criteria set."""
        set_active_criteria(self.rubric_criteria)


# --- Fellowship (default; mirrors the original hardcoded config) -----------

FELLOWSHIP_CONFIG = ProgramConfig(
    program="fellowship",
    rubric_criteria=RUBRIC_CRITERIA,
    rubric_weights=RUBRIC_WEIGHTS,
    rubric_text=RUBRIC_TEXT,
    analyst_instruction=ANALYST_INSTRUCTION,
    grader_instruction=GRADER_HEAD_INSTRUCTION,
    source_priority="text_primary",
    uses_separate_head=False,
    sheet_id_env="FELLOWSHIP_SHEET_ID",
    sheet_range_env="FELLOWSHIP_SHEET_RANGE",
    header_row_env="FELLOWSHIP_HEADER_ROW",
    top_label_row_env="FELLOWSHIP_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI Reasoning",
)


# --- R2B (6 criteria, 3-band, video-primary, 3 distinct roles) --------------

R2B_CONFIG = ProgramConfig(
    program="r2b",
    rubric_criteria=RUBRIC_CRITERIA_R2B,
    rubric_weights=RUBRIC_WEIGHTS_R2B,
    rubric_text=R2B_RUBRIC_TEXT,
    analyst_instruction=R2B_ANALYST_INSTRUCTION,
    grader_instruction=R2B_GRADER_INSTRUCTION,
    source_priority="video_primary",
    uses_separate_head=True,
    head_instruction=R2B_HEAD_INSTRUCTION,
    sheet_id_env="R2B_SHEET_ID",
    sheet_range_env="R2B_SHEET_RANGE",
    header_row_env="R2B_HEADER_ROW",
    top_label_row_env="R2B_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI Reasoning",
)

_PROGRAMS: dict[str, ProgramConfig] = {
    "fellowship": FELLOWSHIP_CONFIG,
    "r2b": R2B_CONFIG,
}


def get_program_config(name: str) -> ProgramConfig:
    """Resolve a program config by name. Raises if unknown so a typo
    fails loudly at startup rather than silently grading with the wrong
    rubric."""
    key = (name or "").strip().lower()
    if key not in _PROGRAMS:
        raise ValueError(
            f"Unknown program '{name}'. Known programs: {sorted(_PROGRAMS)}"
        )
    return _PROGRAMS[key]

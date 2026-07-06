"""Program configuration: per-program rubric, prompts, and sheet geometry.

The pipeline selects a ProgramConfig at startup (via the ``PROGRAM`` env var,
default ``fellowship_v2``) and threads it through the agent builder and
workflow so the same LoopAgent core grades with the right rubric,
instructions, and sheet columns.

All programs use the separate-head architecture: analyst + grader (verify
only) in a loop, then a separate Head agent (score only, after approval). The
Head is re-run N_SAMPLES times and its criterion scores are averaged, with
the rationale selected from the run closest to the average (per spec).
"""
from dataclasses import dataclass
from typing import Literal

from .adk_agents.prompts import (
    ALCHEMIST_ANALYST_INSTRUCTION,
    ALCHEMIST_GRADER_INSTRUCTION,
    ALCHEMIST_HEAD_INSTRUCTION,
    ALCHEMIST_RUBRIC_TEXT,
    FELLOWSHIP_V2_ANALYST_INSTRUCTION,
    FELLOWSHIP_V2_GRADER_INSTRUCTION,
    FELLOWSHIP_V2_HEAD_INSTRUCTION,
    FELLOWSHIP_V2_RUBRIC_TEXT,
    FELLOWSHIP_V2_WEB_VERIFIER_INSTRUCTION,
    R2B_ANALYST_INSTRUCTION,
    R2B_GRADER_INSTRUCTION,
    R2B_HEAD_INSTRUCTION,
    R2B_RUBRIC_TEXT,
)
from .adk_agents.schemas import (
    RUBRIC_CRITERIA,
    RUBRIC_CRITERIA_ALCHEMIST,
    RUBRIC_CRITERIA_R2B,
    RUBRIC_WEIGHTS,
    RUBRIC_WEIGHTS_ALCHEMIST,
    RUBRIC_WEIGHTS_R2B,
    set_active_criteria,
)

ProgramName = Literal["fellowship_v2", "r2b", "alchemist"]


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
    # Whether this program runs a dedicated web_verifier agent between the
    # analyst and the grader. When True, the workflow runs
    # analyst -> web_verifier -> grader (max 3 iterations), and the head
    # receives the web_verification_report alongside the analyst_report.
    # Fellowship V2 enables this; R2B and Alchemist do not.
    uses_web_verification: bool = False
    # Web verifier instruction (only used when uses_web_verification=True).
    web_verifier_instruction: str = ""
    # Per-program sheet-geometry defaults, used by Config.from_env when the
    # corresponding env var is unset. Fellowship and R2B sheets have a merged
    # top-label row above the per-column header row (header_row=2,
    # top_label_row=1). Fellowship V2's sheet has column names on row 1 and
    # sub-headers on row 2 (header_row=1, top_label_row=0 — no separate label
    # row, so output columns are resolved from the header row itself).
    default_header_row: int = 2
    default_top_label_row: int = 1
    # Number of rows AFTER the header row to skip before real data begins.
    # Fellowship/R2B: 0 (data starts immediately after the header row).
    # Fellowship V2: 1 (row 2 is a sub-header row; real data starts at row 3).
    data_start_offset: int = 0
    # Per-program header exclusions for _build_raw_row_text: columns whose
    # header name is in excluded_header_names, OR whose lowercased header
    # contains any substring in excluded_header_substrings, are dropped from
    # the text the analyzer sees. Each program owns its own list so e.g. the
    # Alchemist's PII columns (timestamp/email/phone/...) don't leak into
    # Fellowship's analyzer input (or vice versa). "ception" matches this
    # sheet's "General Pereception" columns (the real header has a typo —
    # an extra "e" after "Per" — "ception" is the common suffix of both the
    # correct and the typo'd spelling, so it survives that) without
    # hardcoding reviewer names, which change every cohort. Excluding these
    # isn't just tidiness: feeding the AI a human's already-given score
    # would anchor its judgment instead of producing an independent one.
    excluded_header_substrings: tuple = ()
    excluded_header_names: frozenset = frozenset({'', 'AI', 'AI_Reasoning', 'AI Reasoning', 'Total'})

    def apply_active_criteria(self) -> None:
        """Set the module-level active criteria the Pydantic validators read.
        Must be called before building agents so the output_schema validators
        enforce the right criteria set."""
        set_active_criteria(self.rubric_criteria)


# --- Fellowship V2 (9 criteria, 5-band 1-10, new sheet) --------------------

FELLOWSHIP_V2_CONFIG = ProgramConfig(
    program="fellowship_v2",
    rubric_criteria=RUBRIC_CRITERIA,
    rubric_weights=RUBRIC_WEIGHTS,
    rubric_text=FELLOWSHIP_V2_RUBRIC_TEXT,
    analyst_instruction=FELLOWSHIP_V2_ANALYST_INSTRUCTION,
    grader_instruction=FELLOWSHIP_V2_GRADER_INSTRUCTION,
    source_priority="video_primary",
    uses_separate_head=True,
    head_instruction=FELLOWSHIP_V2_HEAD_INSTRUCTION,
    uses_web_verification=True,
    web_verifier_instruction=FELLOWSHIP_V2_WEB_VERIFIER_INSTRUCTION,
    sheet_id_env="FELLOWSHIP_V2_SHEET_ID",
    sheet_range_env="FELLOWSHIP_V2_SHEET_RANGE",
    header_row_env="FELLOWSHIP_V2_HEADER_ROW",
    top_label_row_env="FELLOWSHIP_V2_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI_Reasoning",
    # Fellowship V2 sheet: row 1 = reviewer names (AI, AI_Reasoning, Total),
    # row 2 = actual column headers (question text), row 3+ = applicant data.
    # header_row=2 so column names come from row 2.
    # top_label_row=1 so output column lookup finds AI/AI_Reasoning on row 1.
    # data_start_offset=0 because data starts immediately after header row 2.
    default_header_row=2,
    default_top_label_row=1,
    data_start_offset=0,
    excluded_header_substrings=("ception",),
    excluded_header_names=frozenset({"", "AI", "AI_Reasoning", "AI Reasoning", "Total"}),
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
    excluded_header_substrings=(),
    excluded_header_names=frozenset({"", "AI", "AI_Reasoning"}),
)


# --- Alchemist (4 criteria, 5-band 1-10, pitch deck + text primary) --------

ALCHEMIST_CONFIG = ProgramConfig(
    program="alchemist",
    rubric_criteria=RUBRIC_CRITERIA_ALCHEMIST,
    rubric_weights=RUBRIC_WEIGHTS_ALCHEMIST,
    rubric_text=ALCHEMIST_RUBRIC_TEXT,
    analyst_instruction=ALCHEMIST_ANALYST_INSTRUCTION,
    grader_instruction=ALCHEMIST_GRADER_INSTRUCTION,
    source_priority="text_primary",
    uses_separate_head=True,
    head_instruction=ALCHEMIST_HEAD_INSTRUCTION,
    sheet_id_env="ALCHEMIST_SHEET_ID",
    sheet_range_env="ALCHEMIST_SHEET_RANGE",
    header_row_env="ALCHEMIST_HEADER_ROW",
    top_label_row_env="ALCHEMIST_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI_Reasoning",
    # Sheet geometry left as defaults — the user will configure the actual
    # Alchemist sheet layout (header_row, top_label_row, input/output columns)
    # via the ALCHEMIST_* env vars after the sheet is created.
    default_header_row=2,
    default_top_label_row=1,
    data_start_offset=0,
    excluded_header_substrings=(
        "timestamp", "email", "phone", "telegram", "whatsapp",
        "ceo", "visa", "delaware", "incorporated", "registered",
    ),
    excluded_header_names=frozenset(
        {"", "AI", "AI_Reasoning", "AI Reasoning", "Total", "Score"}
    ),
)


_PROGRAMS: dict[str, ProgramConfig] = {
    "fellowship_v2": FELLOWSHIP_V2_CONFIG,
    "r2b": R2B_CONFIG,
    "alchemist": ALCHEMIST_CONFIG,
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

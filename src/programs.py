"""Program configuration: per-program rubric, prompts, and sheet geometry.

The pipeline selects a ProgramConfig at startup (via the ``PROGRAM`` env
var — required, no default; see main.py) and threads it through the agent
builder and workflow so the same LoopAgent core grades with the right
rubric, instructions, and sheet columns.

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
    # Multi-column output (R2B's video-only round): when set, one score
    # column per rubric criterion, a Total Score column, and a
    # Comments/Notes column are written instead of score_column_name/
    # reasoning_column_name. None (default) keeps every other program on
    # the existing single score+reasoning pair — this is purely additive.
    criterion_column_names: dict[str, str] | None = None
    total_score_column_name: str | None = None
    notes_column_name: str | None = None
    # Whether a confirmed contradiction/disqualifying flag automatically
    # zeroes the whole application. True preserves Alchemist/Fellowship
    # V2's existing behavior (cross-source contradictions between
    # independent artifacts carry real fraud signal there). R2B sets
    # False after 7 out of 7 live auto-zeros proved to be false positives
    # (currency conversion, rounding, MRR-vs-sales, roadmap-vs-status,
    # "250+" vs "300+", conflicting GOALS...) — per the user's decision,
    # an internal inconsistency in a single live pitch video now
    # penalizes the relevant rubric criterion/criteria and flags the row
    # for human review with the inconsistency spelled out in the notes;
    # zeroing is the human reviewer's call, never automatic.
    contradiction_auto_zero: bool = True
    # Max analyst<->grader verify-loop iterations before escalating to
    # human review (see agent.py's build_root_agent/R2BApprovalGate/
    # r2b_gate_decision — shared across all programs despite the r2b-
    # specific naming). Default 3 preserves existing Alchemist/Fellowship
    # V2 behavior. R2B sets 2: most rows converge in 1-2 rounds, the third
    # iteration was the tail case, and both analyst and grader re-process
    # the full pitch video every iteration — capping this directly bounds
    # R2B's worst-case per-row latency, which is video-processing-bound.
    max_verify_iterations: int = 3
    # Whether the Head sees raw media (video/deck Parts) at all, vs. only
    # the analyst's text evidence report (which the grader already
    # approved). The grader is UNAFFECTED by this flag — it always sees
    # media, since it's the one doing verification. Default True preserves
    # the ORIGINAL behavior of every program before this flag existed
    # (Alchemist's Head used to run its own redundant fraud/contradiction
    # check against the actual sources, and Fellowship V2's Head used to
    # score Communication_quality partly from direct audio/visual cues).
    # R2B sets this False: the Head "scores only," never re-verifies
    # (that's the grader's job, which still has full video access) — see
    # R2B_HEAD_INSTRUCTION's "Do not re-verify; assume the evidence is
    # approved and grounded." Alchemist and Fellowship V2 (2026-07-21
    # session) now also set this False, aligned with R2B: in both cases the
    # analyst's written evidence already reports the cues the Head used to
    # need media for directly (Alchemist's cross-source contradiction check,
    # Fellowship's teleprompter/dubbing/eye-contact observations for
    # Communication_quality), so a redundant media-reading Head pass adds
    # cost without adding signal. The Head is also the one re-run N_SAMPLES
    # times per row, so this is where the video-payload-duplication cost
    # (and the concurrency failure it caused) actually lives. Confirmed via
    # git blame that "Head sees raw media" was never a deliberate Alchemist
    # choice — it was introduced in R2B's original commit and inherited by
    # every uses_separate_head=True program through the shared
    # _run_head_once function.
    head_include_media: bool = True
    # Thinking level for the grader and Head, set explicitly per program
    # rather than derived from head_include_media or hardcoded. These used
    # to be coupled (Head's level was inferred from head_include_media;
    # the grader had no per-program override at all, always HIGH) — now
    # each program states its own value directly, so head_include_media
    # only controls whether the Head sees media, nothing else.
    # Analyst default HIGH preserves existing behavior for every program —
    # was hardcoded in agent.py until this field existed; now explicit like
    # the grader and Head.
    analyst_thinking_level: Literal["LOW", "HIGH"] = "HIGH"
    # Grader default HIGH preserves existing behavior for every program.
    grader_thinking_level: Literal["LOW", "HIGH"] = "HIGH"
    # Head default HIGH preserves Fellowship V2/original Alchemist
    # behavior; R2B and (now) Alchemist explicitly set LOW below — see
    # R2B_HEAD_INSTRUCTION/agent.py's build_head_agent for the live A/B
    # test that validated LOW for a media-less, text-only Head.
    head_thinking_level: Literal["LOW", "HIGH"] = "HIGH"
    # Head scorer instruction (only used when uses_separate_head=True).
    head_instruction: str = ""
    # Whether a pitch deck PDF is required for this program. When True,
    # the pipeline ingests the pitch deck (Google Slides / Drive PDF) as
    # a multimodal Part alongside the video. Missing pitch deck penalizes
    # the relevant criteria (e.g., cap Product/MVP at 4 for Alchemist).
    requires_pitch_deck: bool = False
    # Per-program sheet-geometry defaults, used by Config.from_env when the
    # corresponding env var is unset. The Fellowship V2 and R2B sheets carry
    # a merged top-label row above the per-column header row (header_row=2,
    # top_label_row=1, so applicant data starts on row 3). Alchemist's tab
    # has the column names on row 1 with no separate label row
    # (header_row=1, top_label_row=0 — output columns are then resolved
    # from the header row itself).
    default_header_row: int = 2
    default_top_label_row: int = 1
    # Optional override for the sheet column that holds the applicant/
    # startup's name — used by pipeline.py for no-show detection (a human
    # types a marker like "won't pitch" directly into this cell), R2B's
    # wrong-video-segment tripwire note, and duplicate-email
    # disambiguation (see _resolve_name_column_index/_find_name_column_index
    # and _derive_row_id). Default None preserves today's behavior: the
    # pipeline GUESSES this column via a substring hint list
    # (_NAME_COLUMN_HINTS = "startup name"/"company name"/"team name"/
    # "project name") that assumes every program's sheet names applicants
    # after their startup/company/team. That assumption doesn't hold for
    # every program — Fellowship V2's own sheet, as of this session, uses
    # a "participant name"-style header the hint list has never covered.
    # Rather than hardcode yet another guess string per program, this is a
    # SELECTABLE column, same UI pattern as score_column_name/
    # reasoning_column_name/criterion_column_names above (see app.py's
    # "Column mapping" sidebar section and Config.from_env's
    # name_column_override) — settable per run without editing this file.
    # None here (the default for every program below) means "keep
    # guessing via the hint list," unchanged from today.
    name_column_name: str | None = None
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
    # Aligned with R2B/Alchemist (2026-07-21 session): the Head scores from
    # the analyst's approved text evidence only, not the raw video — the
    # grader (which still sees full media) already verified it. The
    # Communication_quality cues that used to justify head_include_media=True
    # (teleprompter, eyes off-screen, dubbing mismatch) are already captured
    # in the analyst's written evidence (see FELLOWSHIP_V2_ANALYST_INSTRUCTION's
    # "For Communication quality, describe what you actually observe..."), so
    # the Head can score from that description alone, exactly like R2B's Head
    # already does for its Presentation & Clarity criterion. Not yet
    # separately live-tested for Fellowship's own rubric; worth validating
    # against a few real rows, same as Alchemist's own note on this tradeoff.
    head_include_media=False,
    # Explicit, not derived from head_include_media (see ProgramConfig's
    # field comment) — set to LOW to match R2B's validated media-less-Head
    # config; watch the first several real rows given the tradeoff noted
    # above hasn't been separately tested for this rubric.
    analyst_thinking_level="HIGH",
    grader_thinking_level="HIGH",
    head_thinking_level="LOW",
    # Aligned with R2B/Alchemist (2026-07-21 session): a confirmed
    # contradiction no longer auto-zeroes the score. It penalizes the
    # relevant criterion/criteria instead and flags the row for human
    # review — zeroing is the human reviewer's call, not automatic.
    # Previously True (Fellowship V2's original behavior); revisit if this
    # proves too lenient in practice, same as R2B's own tuning history for
    # this flag.
    contradiction_auto_zero=False,
    # Aligned with R2B/Alchemist: same latency-driven cap (most rows
    # converge in 1-2 rounds for R2B; the third iteration was the tail
    # case there). Not yet separately live-tested for Fellowship V2's own
    # convergence behavior — revisit if Fellowship rows start needing the
    # third round more often than R2B's/Alchemist's did.
    max_verify_iterations=2,
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
    default_header_row=2,
    default_top_label_row=1,
    excluded_header_substrings=("ception",),
    excluded_header_names=frozenset({"", "AI", "AI_Reasoning", "AI Reasoning", "Total"}),
)


# --- R2B (6 criteria, 3-band, video-only, 3 distinct roles) ----------------

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
    max_verify_iterations=2,
    sheet_id_env="R2B_SHEET_ID",
    sheet_range_env="R2B_SHEET_RANGE",
    header_row_env="R2B_HEADER_ROW",
    top_label_row_env="R2B_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI Reasoning",
    # This R2B round's "AI" tab has one column per rubric criterion instead
    # of a combined score+reasoning pair (score_column_name/
    # reasoning_column_name above are kept for backward compatibility but
    # unused whenever criterion_column_names is set — see process_row).
    criterion_column_names={
        "Problem & Solution": "Problem & Solution (10 pts max)",
        "Market Potential": "Market Potential (10 pts max)",
        "Product/MVP & Innovation": "Product/MVP & Innovation (10 pts max)",
        "Team Strength": "Team Strength (10 pts max)",
        "Business Model": "Business Model (10 pts max)",
        "Presentation & Clarity": "Presentation & Clarity (10 pts)",
    },
    total_score_column_name="Total Score",
    notes_column_name="Comments / Notes",
    head_include_media=False,
    analyst_thinking_level="HIGH",
    grader_thinking_level="HIGH",
    head_thinking_level="LOW",
    contradiction_auto_zero=False,
    excluded_header_substrings=(),
    # This round's own output columns must be excluded from what the AI
    # sees as application text, same as every other program already
    # excludes its output columns (AI/AI_Reasoning/Total/Score) — without
    # this, a re-run (retry after failure, force re-grade) would feed the
    # AI's own prior scores/notes back to it as if they were part of the
    # applicant's submission, directly undermining the "video is the ONLY
    # source" instructions this round's prompts rely on.
    excluded_header_names=frozenset({
        "", "AI", "AI_Reasoning",
        "Problem & Solution (10 pts max)", "Market Potential (10 pts max)",
        "Product/MVP & Innovation (10 pts max)", "Team Strength (10 pts max)",
        "Business Model (10 pts max)", "Presentation & Clarity (10 pts)",
        "Total Score", "Comments / Notes",
        "Segment Start", "Segment End",
    }),
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
    requires_pitch_deck=True,
    head_instruction=ALCHEMIST_HEAD_INSTRUCTION,
    # Aligned with R2B: same latency-driven cap (most rows converge in
    # 1-2 rounds; the third iteration was the tail case there). Not yet
    # separately live-tested for Alchemist's own convergence behavior —
    # revisit if Alchemist rows start needing the third round more often
    # than R2B's did.
    max_verify_iterations=2,
    # Aligned with R2B (2026-07-20 session): a confirmed contradiction no
    # longer auto-zeroes the score. It penalizes the relevant criterion/
    # criteria instead and flags the row for human review — zeroing is the
    # human reviewer's call, not automatic. Previously True (Alchemist's
    # original behavior); revisit if this proves too lenient in practice,
    # same as R2B's own tuning history for this flag.
    contradiction_auto_zero=False,
    # Aligned with R2B: the Head scores from the analyst's approved text
    # evidence only, not the raw deck/video — the grader (which still sees
    # full media) already verified it. Removes the Head's own redundant
    # fraud/contradiction re-check against the actual deck — unlike R2B,
    # this specific tradeoff hasn't been separately live-tested for
    # Alchemist's rubric; worth validating against a few real rows.
    head_include_media=False,
    # Explicit, not derived from head_include_media (see ProgramConfig's
    # field comment) — set to LOW to match R2B's validated media-less-Head
    # config; watch the first several real rows given the tradeoff noted
    # above hasn't been separately tested for this rubric.
    analyst_thinking_level="HIGH",
    grader_thinking_level="HIGH",
    head_thinking_level="LOW",
    sheet_id_env="ALCHEMIST_SHEET_ID",
    sheet_range_env="ALCHEMIST_SHEET_RANGE",
    header_row_env="ALCHEMIST_HEADER_ROW",
    top_label_row_env="ALCHEMIST_TOP_LABEL_ROW",
    score_column_name="AI",
    reasoning_column_name="AI_Reasoning",
    # CRM_Form Responses 1+ updated links tab has headers on row 1,
    # no top label row. Data starts from row 2.
    default_header_row=1,
    default_top_label_row=0,
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

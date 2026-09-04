"""
Batch driver: reads the sheet, skips already-checkpointed rows, runs the
ADK analyst/grader workflow per applicant (in parallel and bounded by
max_concurrency), then writes approved or human-review results to the sheet.

Video URL normalization happens here before ADK receives the application as
native multimodal input. The agents own analysis, verification, retry routing,
and final report generation.

Rows are processed concurrently because they're fully independent — no
shared state between applicants — so a ThreadPoolExecutor is sufficient;
no need for asyncio's added complexity for what's mostly I/O-bound work
(API calls) anyway.
"""
import asyncio
import json
import re
import threading
import unicodedata
from collections import Counter
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlparse

from .checkpoint import Checkpoint
from .config import Config
from .adk_agents import AdkReviewWorkflow
from .google_clients import (
    fetch_sheet_row,
    get_sheets_service,
    read_sheet_rows,
    resolve_multi_output_columns,
    resolve_output_columns,
    write_multi_notes_only,
    write_multi_row_result,
    write_reasoning_only,
    write_row_result,
)
from .video_urls import ResolvedMedia, VideoResolutionError, resolve_video_url
from .video_ingestion import ingest_pitch_deck, ingest_video
from .round_segments import (
    SEGMENT_END_COLUMN,
    SEGMENT_START_COLUMN,
    SegmentValidationError,
    parse_timestamp,
)

# Header exclusion lists are program-specific — see ProgramConfig fields
# excluded_header_substrings and excluded_header_names in src/programs.py.

# After this many consecutive failures on the same row, stop retrying
# blindly and escalate to human review instead. 3 keeps a genuinely
# transient failure (download timeout, API rate limit) retryable on the
# next run while bounding forever-retry on a row that fails the same way
# every time (a structurally broken source URL, a file too large to ever
# process within quota). Independent of the verify loop's iteration cap,
# which is ProgramConfig.max_verify_iterations (2 for every program).
FAILURE_ESCALATION_THRESHOLD = 3

# How often run_batch's completion loop re-checks cancel_event when nothing
# has finished yet. Only affects how quickly a Stop click is noticed while
# every in-flight row is still running — a genuinely completed future is
# always noticed immediately regardless of this value (see the
# concurrent.futures.wait() call in run_batch).
CANCEL_CHECK_INTERVAL_SECONDS = 0.5


class RowCancelled(Exception):
    """Raised by process_row when cancel_event is set mid-row.

    A deliberate stop (the Streamlit Stop button), not a scored failure —
    run_batch's completion handling must treat this the same as the
    existing no-show "deliberate skip" path: no checkpoint entry, no sheet
    write, not counted as an error.
    """


def _normalize_for_match(text: str) -> str:
    """Strip accents — used only for email-column detection, where ASCII
    matching is fine either way but consistency with the rest of the
    codebase's normalization habit costs nothing.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


# Markers a human adds directly into the Startup Name cell for applicants
# who didn't actually show up to pitch (patterns seen on production
# sheets; the examples below use invented names): "Acme Robotics -
# won't pitch", "Northwind Labs no response", "Bluepeak Systems -
# didn't respond". Substring match on the normalized (lowercased,
# accent-stripped) name; these rows are skipped entirely before any
# LLM call.
_NO_SHOW_MARKERS = (
    "no response",
    "not pitching",
    "won't pitch",
    "wont pitch",
    "didn't respond",
    "didnt respond",
    "did not respond",
    "didn't come",
    "didnt come",
    "did not come",
)
_NO_SHOW_COMPACT_MARKERS = tuple(
    re.sub(r"[^a-z0-9]+", "", marker) for marker in _NO_SHOW_MARKERS
)


def _is_no_show(startup_name: str) -> bool:
    name = _normalize_for_match(startup_name)
    compact_name = re.sub(r"[^a-z0-9]+", "", name)
    return (
        any(marker in name for marker in _NO_SHOW_MARKERS)
        or any(marker in compact_name for marker in _NO_SHOW_COMPACT_MARKERS)
    )


def _find_duplicate_emails(header: list, rows: list) -> frozenset:
    """Emails that appear on more than one row — confirmed live: the same
    email is sometimes reused across multiple genuinely different
    applications (one person submitting more than one startup). Since
    checkpointing keys by email, a plain email key would collide: the
    first such row gets graded and checkpointed, and checkpoint.is_done()
    then silently skips every later row sharing that email forever,
    leaving it permanently blank with zero explanation (confirmed: 5 real
    collisions found in one sheet). _derive_row_id disambiguates only
    these confirmed-colliding emails, leaving the plain-email key
    unchanged for the (much more common) non-colliding case — so this
    fix doesn't invalidate checkpoint entries for every already-graded row.
    """
    emails = []
    for row in rows:
        for i, col in enumerate(header):
            if "email" in _normalize_for_match(col):
                if i < len(row) and row[i].strip():
                    emails.append(row[i].strip().lower())
                break
    counts = Counter(emails)
    return frozenset(email for email, n in counts.items() if n > 1)


_NAME_COLUMN_HINTS = ("startup name", "company name", "team name", "project name")


def _find_name_column_index(header: list) -> int | None:
    """Exact match first, substring fallback second — a column whose text
    only CONTAINS a hint (e.g. "Full Team Name List") must never win over
    an earlier or later column that IS one of the hints exactly (e.g.
    "Startup Name"). Verified real-sheet behavior is unaffected (R2B's
    actual header has no such collision today); this just closes a latent
    divergence found when the no-show/segment-tripwire lookups were
    consolidated onto this function instead of their own separate
    exact-match logic.
    """
    normalized = [_normalize_for_match(col) for col in header]
    for i, col in enumerate(normalized):
        if col in _NAME_COLUMN_HINTS:
            return i
    for i, col in enumerate(normalized):
        if any(hint in col for hint in _NAME_COLUMN_HINTS):
            return i
    return None


def _resolve_name_column_index(header: list, program_config) -> int | None:
    """Resolve the applicant/startup-name column index for this program.

    If program_config.name_column_name is set — an operator explicitly
    picked it via app.py's "Column mapping" sidebar / Config.from_env's
    name_column_override, same pattern as score_column_name/
    reasoning_column_name/criterion_column_names — find it via a
    case/whitespace-tolerant EXACT match against header (reusing
    _normalize_for_match, this file's existing normalization convention,
    plus a .strip() since an explicit selection should tolerate a
    trailing-space header like the sheet quirks already documented
    elsewhere in this file). This is deliberately an exact match, not a
    substring hint match: the operator named one specific column, so
    matching a different column that merely contains that text would be
    surprising. Returns None if the configured name doesn't actually
    exist in this sheet's header (e.g. a stale mapping left over from
    switching sheets) — callers must treat that exactly like "no name
    column found" today: gracefully, not a crash.

    Falls back to the existing hint-based _find_name_column_index when
    program_config.name_column_name is None — every program's default,
    preserving today's guess-based behavior unchanged.
    """
    configured_name = getattr(program_config, "name_column_name", None)
    if configured_name is None:
        return _find_name_column_index(header)
    target = _normalize_for_match(configured_name.strip())
    for i, col in enumerate(header):
        if _normalize_for_match(col.strip()) == target:
            return i
    return None


def _segment_needs_human_review(header: list, row: list) -> bool:
    """True when indexing exhausted its review loop for this row."""
    lookup = {col.strip().lower(): i for i, col in enumerate(header)}
    for column in (SEGMENT_START_COLUMN, SEGMENT_END_COLUMN):
        index = lookup.get(column.lower())
        value = row[index].strip() if index is not None and index < len(row) else ""
        if value.upper().startswith("NEEDS HUMAN REVIEW:"):
            return True
    return False


def _read_segment_bounds(header: list, row: list) -> tuple[int, int] | None:
    """(start_s, end_s) from the row's segment cells, or None.

    None (-> today's full-video behavior) whenever the columns are absent,
    either cell is blank, carries the indexer's 'NEEDS CHECK' marker, is
    unparseable, or the bounds are inverted — a bad segment must never
    break grading, only opt out of clipping. Mixed rounds (some rows with
    founder-submitted individual clips) therefore need no special casing.
    """
    lookup = {}
    for i, col in enumerate(header):
        lookup.setdefault(col.strip().lower(), i)
    start_idx = lookup.get(SEGMENT_START_COLUMN.lower())
    end_idx = lookup.get(SEGMENT_END_COLUMN.lower())
    if start_idx is None or end_idx is None:
        return None
    raw_start = (row[start_idx] if len(row) > start_idx else "").strip()
    raw_end = (row[end_idx] if len(row) > end_idx else "").strip()
    if not raw_start or not raw_end:
        return None
    try:
        start_s, end_s = parse_timestamp(raw_start), parse_timestamp(raw_end)
    except SegmentValidationError:
        return None
    if end_s <= start_s:
        return None
    return start_s, end_s


def _derive_row_id(
    header: list,
    row: list,
    sheet_row_number: int,
    duplicate_emails: frozenset = frozenset(),
    program_config=None,
) -> str:
    """Prefer an email column as the stable applicant ID (survives sheet
    re-sorts); fall back to the sheet row number if no email column is
    found, since *some* stable key is required for checkpointing.

    See _find_duplicate_emails: an email in duplicate_emails is
    disambiguated with the startup/company/team/project name column when
    one exists and is non-blank for this row (still survives a resort,
    unlike the row-number fallback used when no such column is found or
    it's blank here).

    program_config, when given, is threaded into _resolve_name_column_index
    so an operator-selected name_column_name (Config.from_env's
    name_column_override) is honored here too, not just for no-show
    detection and R2B's segment tripwire note. None (the default, kept for
    any caller not yet updated to pass it) falls back to the original
    unconditional _find_name_column_index(header) call — unchanged
    behavior.
    """
    for i, col in enumerate(header):
        if "email" in _normalize_for_match(col):
            if i < len(row) and row[i].strip():
                email = row[i].strip().lower()
                if email in duplicate_emails:
                    name_idx = (
                        _resolve_name_column_index(header, program_config)
                        if program_config is not None
                        else _find_name_column_index(header)
                    )
                    if name_idx is not None and name_idx < len(row) and row[name_idx].strip():
                        return f"{email}|{row[name_idx].strip().lower()}"
                    return f"{email}|row_{sheet_row_number}"
                return email
            break
    return f"row_{sheet_row_number}"


def _build_raw_row_text(
    header: list,
    row: list,
    *,
    excluded_header_names: frozenset,
    excluded_header_substrings: tuple,
) -> str:
    """Every eligible question/answer labeled as plain text for both agents.

    Columns whose header name is in excluded_header_names, or whose
    lowercased header contains any substring in excluded_header_substrings,
    are skipped. Both lists are program-specific (see ProgramConfig).
    """
    lines = []
    for i, col in enumerate(header):
        if col in excluded_header_names or any(sub in col.lower() for sub in excluded_header_substrings):
            continue
        value = row[i] if i < len(row) else ""
        lines.append(f"{col}: {value}")
    return "\n".join(lines)


_URL_PATTERN = re.compile(r"https?://[^\s]+")


def _extract_first_url(cell_value: str) -> str:
    """Extract the first URL from a cell that may contain extra prose.

    Applicants sometimes paste a URL followed by instructions like
    'If audio doesn't play in browser, please download the video'.
    Return the first whitespace-delimited URL, or empty string if no
    URL is found.
    """
    if not cell_value:
        return ""
    match = _URL_PATTERN.search(cell_value)
    return match.group(0) if match else ""


def _submitted_video_url(header: list, row: list) -> str:
    for i, col in enumerate(header):
        if "video" in _normalize_for_match(col):
            if i < len(row):
                return _extract_first_url(row[i])
            return ""
    return ""


# Hosts that the Tier-1 resolver cannot fetch directly (Drive serves an HTML
# interstitial or requires auth, so resolve_video_url raises). These must go
# through Tier-2 (Drive API download → Gemini Files API upload) so the
# analyst receives the video as a native multimodal Part.
_DRIVE_HOSTS = frozenset({"drive.google.com", "drive.usercontent.google.com"})


def _is_drive_url(url: str) -> bool:
    """Detect Google Drive links that require Tier-2 ingestion.

    Tier-1 (resolve_video_url) cannot handle Drive: the link resolves to an
    HTML interstitial or an auth-walled download endpoint, so Tier-1 raises
    and the video Part is never created. Detecting the host explicitly lets
    us short-circuit to Tier-2 without paying for a metadata round-trip that
    we already know will not yield a fetchable URI.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in _DRIVE_HOSTS


def _submitted_pitch_deck_url(header: list, row: list) -> str:
    """Find the pitch deck link column. Matches headers containing
    'pitch deck' or 'presentation' (case-insensitive)."""
    for i, col in enumerate(header):
        col_lower = _normalize_for_match(col)
        if "pitch deck" in col_lower or "presentation" in col_lower:
            if i < len(row):
                return _extract_first_url(row[i])
            return ""
    return ""


def _row_has_video(state: dict) -> bool:
    return bool(state.get("video_data") or state.get("video_url"))


def _retry_without_video(workflow, initial_state: dict, exc: Exception) -> dict:
    """The row had a video attached (as downloaded bytes or a direct URI)
    and workflow.invoke failed. Confirmed live across three different failure
    signatures on video-heavy rows — a 429 RESOURCE_EXHAUSTED from an
    oversized video payload, a 400 from Vertex's url_context fetch hitting
    its own ~15MB size cap on a webpage video source (max_bytes_fetched:
    15728640), and an unexplained 400 INVALID_ARGUMENT specific to one
    video/deck combination during Head scoring — that trying to enumerate
    and special-case each exact error is a losing game; the common thread
    is always "the video, somehow". Retry once without it instead: for
    Alchemist, the deck is the required source and is usually fine on its
    own, so a degraded-but-real score beats an automatic human-review
    escalation over a video-only problem.

    Only escalates to human review (by letting a second failure propagate
    to run_batch's 3-strikes safety net) if grading without the video also
    fails for some other reason.
    """
    no_video_state = dict(initial_state)
    video_size = no_video_state.pop("video_original_size_bytes", None)
    no_video_state.pop("video_data", None)
    no_video_state.pop("video_url", None)
    no_video_state.pop("video_mime_type", None)
    size_note = f" ({video_size / 1e6:.0f}MB)" if video_size else ""
    no_video_state["video_error"] = (
        f"Video{size_note} excluded from analysis — processing failed "
        f"({type(exc).__name__}). Scored on pitch deck and application "
        "text only."
    )
    return workflow.invoke(no_video_state)


def _build_workflow(config: Config) -> AdkReviewWorkflow:
    """Construct the workflow with program-aware models and sample count.

    Every program uses the separate-head architecture: verify loop once,
    then the Head re-run n_samples times and averaged. n_samples comes
    from config for all programs alike.
    """
    return AdkReviewWorkflow(
        config.analyzer_model,
        config.grader_model,
        config.program_config,
        head_model=config.head_model,
        n_samples=config.n_samples,
    )


def process_row(
    config: Config,
    workflow,
    header: list,
    row: list,
    sheet_row_number: int,
    checkpoint: Checkpoint,
    *,
    force: bool = False,
    duplicate_emails: frozenset = frozenset(),
    cancel_event: "threading.Event | None" = None,
):
    def _raise_if_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RowCancelled("Grading was cancelled before this row completed.")

    _raise_if_cancelled()
    row_id = _derive_row_id(
        header, row, sheet_row_number, duplicate_emails, program_config=config.program_config,
    )
    if checkpoint.is_done(row_id) and not force:
        return row_id, None  # already graded in a prior run, nothing to write
    if _segment_needs_human_review(header, row):
        # The indexer deliberately left this row unresolved. Do not fall back
        # to grading the full round video; a human must correct the boundaries.
        return row_id, None

    name_col_idx = _resolve_name_column_index(header, config.program_config)
    if name_col_idx is not None and name_col_idx < len(row) and _is_no_show(row[name_col_idx]):
        # Didn't show up to pitch — nothing to grade. No checkpoint entry
        # either, so removing the marker text later makes the row eligible
        # again on the next run.
        return row_id, None

    if config.program_config.program == "r2b" and not _submitted_video_url(header, row):
        # R2B is video-only (no deck/text fallback) — a row with no video
        # link at all has nothing to grade, same as a no-show. Skip it the
        # same way (no checkpoint entry, no LLM cost) rather than running
        # the full pipeline just to auto-score "no video submitted" — it
        # becomes eligible again once a video link is added.
        return row_id, None

    initial_state = {
        "row_id": row_id,
        "raw_row_text": _build_raw_row_text(
            header,
            row,
            excluded_header_names=config.program_config.excluded_header_names,
            excluded_header_substrings=config.program_config.excluded_header_substrings,
        ),
    }

    _raise_if_cancelled()

    # Alchemist: pitch deck is required and is checked FIRST, before any
    # video resolution is even attempted. If no pitch deck URL is submitted,
    # or the deck link is deterministically unfetchable, this is a human-
    # review case regardless of video — score all criteria as 0 and skip
    # the LLM pipeline entirely (saves API cost, avoids attempting a
    # possibly-expensive video download for a row that's already a known
    # dead end). Only once the deck is confirmed accessible does the video
    # block below even run; there, an unresolvable video is fine — video is
    # optional for this program and must not force human review or be
    # penalized (see ALCHEMIST_RUBRIC_TEXT: "Video is optional. Missing
    # video should NOT penalize any criterion.").
    if config.program_config.requires_pitch_deck:
        submitted_pitch_deck_url = _submitted_pitch_deck_url(header, row)
        if not submitted_pitch_deck_url:
            # No pitch deck = automatic 0 on all criteria, no API calls.
            return row_id, {
                "score": 0,
                "reasoning": json.dumps({
                    "criterion_scores": {c: 0 for c in config.program_config.rubric_criteria},
                    "criterion_rationale": {
                        c: "No pitch deck submitted. Pitch deck is required for Alchemist evaluation."
                        for c in config.program_config.rubric_criteria
                    },
                }),
                "human_review_flag": True,
            }
        try:
            resolved_deck = ingest_pitch_deck(
                submitted_pitch_deck_url,
                config.service_account_path,
                config.analyzer_model,
            )
            initial_state["pitch_deck_url"] = resolved_deck.uri
            initial_state["pitch_deck_data"] = resolved_deck.data
            initial_state["pitch_deck_mime_type"] = resolved_deck.mime_type
            initial_state["pitch_deck_chart_text"] = resolved_deck.chart_text
        except VideoResolutionError as exc:
            # A deck link was submitted but is deterministically unfetchable
            # (Drive folder instead of a file, Canva/Cloudflare-blocked page,
            # a JS-rendered site with no direct export, etc.) — this is the
            # same outcome as "no deck submitted" from the grader's
            # perspective (the required primary source is unavailable), so
            # short-circuit the same way: a deterministic score of 0, no LLM
            # call. Running the full analyst->grader->head pipeline just to
            # have it echo back "deck unavailable" wastes API calls/quota on
            # a row that's already a known dead end, and risks a random
            # infra failure (429/500) turning a clean, honest outcome into a
            # silently-stuck "failed" checkpoint entry instead.
            return row_id, {
                "score": 0,
                "reasoning": json.dumps({
                    "criterion_scores": {c: 0 for c in config.program_config.rubric_criteria},
                    "criterion_rationale": {
                        c: f"Pitch deck could not be accessed: {exc}"
                        for c in config.program_config.rubric_criteria
                    },
                }),
                "human_review_flag": True,
            }

    _raise_if_cancelled()

    submitted_video_url = _submitted_video_url(header, row)
    if submitted_video_url:
        # Video resolution is source_priority-agnostic so that Drive videos
        # are ingested as native multimodal Parts for every program, not just
        # R2B. The path is:
        #   1. Tier-1 (resolve_video_url) — cheap, no download; covers
        #      YouTube natively, direct HTTPS videos ≤100MB, and webpages
        #      with discoverable video metadata (og:video / <video> / JSON-LD).
        #   2. If Tier-1 fails outright, or the URL is a Google Drive link,
        #      fall back to Tier-2 (ingest_video) which downloads via
        #      the Drive API and uploads to Files API / inline bytes so the
        #      analyst receives the video as a native Part.
        #   3. If BOTH fail — not YouTube, not Drive, no direct video file
        #      found anywhere — this is a genuine resolution failure, not a
        #      fallback source. A submitted-but-unresolvable link might be a
        #      real video the applicant just hosted somewhere Gemini can't
        #      reach (a Canva/Loom share page, a JS-rendered site); auto-
        #      scoring it as "no video submitted" would unfairly penalize
        #      that, so this short-circuits to human review below instead
        #      of entering the LLM pipeline at all.
        resolved_video: ResolvedMedia | None = None
        try:
            resolved_video = resolve_video_url(submitted_video_url)
        except VideoResolutionError as exc:
            initial_state["video_error"] = str(exc)

        needs_tier2 = (
            resolved_video is None
            or _is_drive_url(submitted_video_url)
            or (
                # Any other directly-fetchable, non-YouTube URL also needs
                # to go through Tier-2 now — not to force a download (Tier-2
                # itself only downloads when the file is actually over
                # Vertex's 15MB URI-fetch limit; see ingest_video),
                # but because Tier-1 alone has no way to make that
                # size-aware decision.
                "youtube" not in resolved_video.uri
                and "youtu.be" not in resolved_video.uri
            )
        )
        if needs_tier2:
            try:
                resolved_video = ingest_video(
                    submitted_video_url,
                    config.service_account_path,
                )
            except VideoResolutionError as exc:
                # Preserve the Tier-1 error when Tier-2 also fails so the
                # human reviewer sees the most informative message.
                initial_state["video_error"] = str(exc)
                resolved_video = None

        if resolved_video is not None:
            initial_state["video_url"] = resolved_video.uri
            initial_state["video_data"] = resolved_video.data
            initial_state["video_mime_type"] = resolved_video.mime_type
            initial_state["video_original_size_bytes"] = (
                resolved_video.original_size_bytes
            )
            # Tier-2 succeeded: clear any stale error left by a Tier-1
            # failure. Without this, the workflow would both build the video
            # Part (video_url is set) AND append "VIDEO UNAVAILABLE: ..."
            # (video_error is set) — a contradictory state for the analyst.
            initial_state.pop("video_error", None)
        elif not config.program_config.requires_pitch_deck:
            # A video link was submitted but could not be resolved to a
            # real, playable video by either tier, AND this program has no
            # alternate required source (R2B/Fellowship V2 are video-only/
            # video-primary) — route to human review instead of entering
            # the LLM pipeline (saves the API cost too).
            #
            # Programs with a required pitch deck (Alchemist) do NOT take
            # this branch. By this point the deck has already been checked
            # (see the requires_pitch_deck block earlier in this function,
            # which runs and returns BEFORE video is ever touched) — so an
            # unresolvable video here is a program with a confirmed-good
            # deck and a merely-optional video that happens to be broken.
            # video is optional there ("Video is optional. Missing video
            # should NOT penalize any criterion." — ALCHEMIST_RUBRIC_TEXT),
            # so this must not force human review or skip the pipeline.
            # video_error is already set above; execution falls through and
            # grading proceeds on the deck/text alone.
            unresolvable_note = (
                f"Video link could not be resolved to a playable video "
                f"(not YouTube, not Drive, no direct video file found): "
                f"{initial_state.get('video_error', 'unknown reason')}"
            )
            if config.program_config.criterion_column_names:
                # Multi-column programs (R2B) need this shape, not
                # score/reasoning — see the same branch below for the
                # normal (post-LLM) case this mirrors.
                return row_id, {
                    "criterion_scores": {},
                    "total_score": None,
                    "notes": unresolvable_note,
                    "human_review_flag": True,
                }
            return row_id, {
                "score": None,
                "reasoning": unresolvable_note,
                "human_review_flag": True,
            }

    # Virtual clip: a round video URL plus operator-typed Segment Start/End
    # cells means Gemini should watch only this startup's span — clipped
    # server-side, no cut files. Segments are entered by hand; there is no
    # automatic proposer (see round_segments.py's module docstring).
    segment_bounds = _read_segment_bounds(header, row)
    if (
        segment_bounds
        and initial_state.get("video_data")
        and not initial_state.get("video_url")
    ):
        # Tier-2 on Vertex returns inline bytes with no URI, and
        # server-side clipping needs a URI. Silently grading the full
        # round recording would grade every startup in the round, not
        # this applicant, so escalate instead of widening the evidence.
        note = (
            "Segment Start/End are set, but this video was downloaded "
            "inline (Vertex Tier-2), where server-side clipping is "
            "unavailable. Needs a human: clip locally or replace the "
            "link with a YouTube URL."
        )
        if config.program_config.criterion_column_names:
            return row_id, {
                "criterion_scores": {}, "total_score": None,
                "notes": note, "human_review_flag": True,
            }
        return row_id, {
            "score": None, "reasoning": note, "human_review_flag": True,
        }

    if segment_bounds and initial_state.get("video_url"):
        initial_state["video_segment_start_s"] = segment_bounds[0]
        initial_state["video_segment_end_s"] = segment_bounds[1]
        # Wrong-segment tripwire — lives in row text, NOT in any agent
        # prompt: if the timestamps point at a different startup's pitch,
        # the analyst says so, the grader loop escalates via the existing
        # human-review path, and no score is written.
        startup_name = ""
        name_idx = _resolve_name_column_index(header, config.program_config)
        if name_idx is not None and len(row) > name_idx:
            startup_name = row[name_idx].strip()
        if startup_name:
            initial_state["raw_row_text"] += (
                f"\n\nVIDEO SEGMENT NOTE: this video segment should be the "
                f"pitch by {startup_name} — if the founders are clearly "
                f"pitching a different company, state that explicitly in "
                f"your evidence instead of extracting evidence for "
                f"{startup_name}."
            )

    # R2B is video-primary and criterion 6 (Presentation & Clarity) requires
    # video evidence. The R2B prompts instruct the grader to score criterion 6
    # as 1 with rationale "No video submitted" when no video is available.
    # Make the no-video state unambiguous to the analyst by setting a
    # video_error even when no URL was submitted at all — the workflow
    # appends this to the analyst's text input as "VIDEO UNAVAILABLE: ...".
    # Checks both video_url and video_data: a Tier-2 in-memory download on
    # Vertex AI leaves video_url as "" (no Files API URI on that backend),
    # so video_url alone would wrongly read as "no video" and overwrite a
    # real result with this error.
    if (
        config.program_config.source_priority == "video_primary"
        and not initial_state.get("video_url")
        and not initial_state.get("video_data")
    ):
        initial_state.setdefault(
            "video_error",
            "No video URL submitted or video could not be resolved.",
        )

    _raise_if_cancelled()

    no_fallback_without_video = bool(config.program_config.criterion_column_names)
    try:
        final_state = workflow.invoke(initial_state)
    except Exception as exc:
        if no_fallback_without_video:
            # R2B (video-only, no deck/text fallback): nothing left to
            # grade on if the analyst's structured output fails, so this
            # row just fails and falls through to run_batch's
            # FAILURE_ESCALATION_THRESHOLD-gated escalation across
            # separate runs, same as any other program's genuine error.
            raise
        elif _row_has_video(initial_state):
            final_state = _retry_without_video(workflow, initial_state, exc)
        else:
            raise

    # One last check before handing back a completed result: run_batch
    # writes to the sheet and marks the checkpoint immediately after this
    # function returns, so this is the last point cancellation can still
    # stop that write/mark from happening for this row.
    _raise_if_cancelled()

    final_result = final_state.get("final_result", {})
    score = final_result.get("score")
    raw_reasoning = final_result.get("reasoning", "")
    reasoning = raw_reasoning
    human_review_flag = final_state.get("human_review_flag", False)
    if human_review_flag:
        # No dedicated flag column exists on the real sheet — fold the
        # signal into the reasoning text itself rather than mutating the
        # sheet's structure (which has formulas referencing specific
        # columns already).
        reasoning = f"[NEEDS HUMAN REVIEW] {reasoning}"
        # score stays as it is, including None. A confirmed disqualification
        # (lie/fraud/contradiction) is a real, deliberate 0 and must reach
        # the sheet; an unknown score (evidence never approved / all Head
        # samples failed) stays None, and run_batch then writes the note
        # WITHOUT touching the score cell, so a re-graded row keeps whatever
        # grade it already had.

    # No GCS cleanup is needed: Tier-2 now uploads exclusively to the Gemini
    # Files API, which auto-expires objects after 48 hours. The previous
    # Vertex AI / GCS upload path (and its gs:// cleanup) has been removed
    # along with all GCP billing dependencies.

    if config.program_config.criterion_column_names:
        # R2B's video-only round: the sheet has one column per rubric
        # criterion instead of a combined score+reasoning pair.
        # raw_reasoning is the JSON blob _average_head_samples already
        # produces (criterion_scores/criterion_rationale) — parse it back
        # out rather than writing raw JSON into a single cell. Any path
        # that couldn't produce real per-criterion scores (evidence never
        # approved, all Head samples failed, video dropped and retried)
        # writes plain text there instead, so json.loads legitimately fails
        # and is handled, not an error case.
        try:
            parsed = json.loads(raw_reasoning)
            criterion_scores = parsed.get("criterion_scores", {})
            criterion_rationale = parsed.get("criterion_rationale", {})
        except (json.JSONDecodeError, TypeError):
            parsed = {}
            criterion_scores = {}
            criterion_rationale = {}
            if score == 0:
                # Grader-side disqualification (agent.py's ApprovalGate,
                # triggered by disqualifying_issue_found on the grader's
                # verdict) sets reasoning to plain text — "Application
                # scored 0: confirmed contradiction — ..." — unlike
                # Head-side disqualification, which produces the JSON blob
                # parsed above. Confirmed via code trace: both are a real,
                # deliberate 0, not "couldn't produce a score", so both
                # must land on the sheet the same way regardless of which
                # stage caught it, or Total Score silently shows blank
                # instead of 0 for the more likely of the two paths (the
                # grader runs first and can exit as early as attempt 1).
                criterion_scores = {c: 0 for c in config.program_config.rubric_criteria}
                criterion_rationale = {
                    c: reasoning for c in config.program_config.rubric_criteria
                }
                parsed = {
                    "criterion_scores": criterion_scores,
                    "criterion_rationale": criterion_rationale,
                }

        if criterion_scores:
            total_score = round(sum(criterion_scores.values()) / len(criterion_scores), 2)
            # Same raw JSON shape Alchemist writes to its single "AI
            # Reasoning" column (criterion_scores/criterion_rationale/
            # n_samples/sample_scores/selected_from_sample) — confidence
            # added on top, since neither program surfaced it to the sheet
            # before even though _average_head_samples always computed it.
            parsed["confidence"] = final_result.get("confidence", "")
            notes = json.dumps(parsed, ensure_ascii=False)
            # Flagged-but-scored rows (an inconsistency was priced into the
            # criterion scores instead of zeroing — contradiction_auto_zero
            # False) still need the human-review marker the app's display
            # logic looks for; plain-text paths get it via `reasoning`
            # above, but this JSON path is built from raw_reasoning.
            if human_review_flag:
                notes = f"[NEEDS HUMAN REVIEW] {notes}"
        else:
            total_score = None
            notes = reasoning  # the plain-text (possibly [NEEDS HUMAN REVIEW]-prefixed) message

        # The caller owns completion because only it observes the Sheets commit.
        return row_id, {
            "criterion_scores": criterion_scores,
            "total_score": total_score,
            "notes": notes,
            "human_review_flag": human_review_flag,
        }

    # The caller owns completion because only it observes the Sheets commit.
    return row_id, {
        "score": score,
        "reasoning": reasoning,
        "human_review_flag": human_review_flag,
    }


def run_batch(
    config: Config,
    force: bool = False,
    limit: int | None = None,
    on_progress=None,
    target_row_number: int | None = None,
    cancel_event: "threading.Event | None" = None,
):
    """Run the batch. on_progress(done, total, row_id, ok), if given, is
    called synchronously on the calling thread right after each row
    finishes (success or failure) — safe for a caller like a Streamlit
    script to update a progress bar without needing its own thread.

    target_row_number, when given, restricts the run to exactly that sheet
    row (1-indexed as it appears in the actual Google Sheet) and always
    re-grades it regardless of checkpoint state — for testing a single
    known row (e.g. after a prompt/config change) without touching the
    rest of the sheet or needing "re-grade already-graded rows" turned on
    for the whole batch. Applies uniformly to every program (no
    program-specific gating) since row-number addressing is generic sheet
    geometry, not a program-specific concept.

    cancel_event, when given, is threaded through to every process_row call
    so an in-progress row can cooperatively bail out (see RowCancelled).
    Rows already cancelled this way (or cancelled at the asyncio level via
    adk_agents.workflow.cancel_all_active()) are treated as a deliberate
    skip in the completion loop below, same as a no-show — not a scored
    failure. Once cancel_event is observed set, the executor is shut down
    with cancel_futures=True so any row not yet started is dropped instead
    of still being launched.
    """
    sheets_service = get_sheets_service(config.service_account_path)
    checkpoint = Checkpoint(config.checkpoint_path)
    workflow = _build_workflow(config)

    header, rows = read_sheet_rows(sheets_service, config.sheet_id, config.sheet_range, config.header_row)
    sheet_name = config.sheet_range.split("!")[0]

    # "AI" / "AI Reasoning" are named on the merged TOP label row, not the
    # per-column header row `header` holds — fetch that row separately
    # rather than requiring a letter override for every sheet shaped this way.
    # When there is no separate label row (top_label_row=0, Fellowship V2),
    # output columns live on the header row itself.
    if config.top_label_row > 0:
        top_label_header = fetch_sheet_row(sheets_service, config.sheet_id, sheet_name, config.top_label_row)
    else:
        top_label_header = header
    if config.program_config.criterion_column_names:
        col_map = resolve_multi_output_columns(
            top_label_header,
            config.program_config.criterion_column_names,
            config.program_config.total_score_column_name,
            config.program_config.notes_column_name,
        )
    else:
        col_map = resolve_output_columns(
            top_label_header,
            score_column_name=config.program_config.score_column_name,
            reasoning_column_name=config.program_config.reasoning_column_name,
        )

    results = {}
    errors = {}
    duplicate_emails = _find_duplicate_emails(header, rows)

    # max_workers bounded by config rather than len(rows) — uncapped
    # concurrency against the Gemini API at 100+ rows risks hitting
    # rate limits and burning retries on 429s instead of real work.
    #
    # Explicit lifecycle instead of `with ThreadPoolExecutor(...) as pool:`:
    # that context manager's default teardown (shutdown(wait=True)) blocks
    # on exit until every already-submitted future finishes — including
    # ones not yet even started — which would make a kill signal wait out
    # the whole remaining queue anyway. Managing shutdown() ourselves lets
    # a kill signal drop not-yet-started futures immediately instead (see
    # cancel_futures=True below); the `finally` still guarantees the pool
    # is shut down on every path (success, exception, or cancellation) so
    # nothing leaks.
    pool = ThreadPoolExecutor(max_workers=config.max_concurrency)
    pool_shutdown_for_cancel = False
    try:
        futures = {}
        submitted = 0
        for i, row in enumerate(rows):
            sheet_row_number = i + config.header_row + 1  # header row + 1-indexing
            if target_row_number is not None and sheet_row_number != target_row_number:
                continue
            if limit is not None and submitted >= limit:
                break
            row_id_preview = _derive_row_id(
                header, row, sheet_row_number, duplicate_emails, program_config=config.program_config,
            )
            effective_force = force or target_row_number is not None
            if checkpoint.is_done(row_id_preview) and not effective_force:
                continue
            submitted += 1
            future = pool.submit(
                process_row,
                config,
                workflow,
                header,
                row,
                sheet_row_number,
                checkpoint,
                force=effective_force,
                duplicate_emails=duplicate_emails,
                cancel_event=cancel_event,
            )
            futures[future] = (row_id_preview, sheet_row_number)

        done_count = 0

        def _handle_completed_future(future, row_id, sheet_row_number):
            # future is guaranteed done() here (by every caller below), so
            # every branch of this call is non-blocking.
            nonlocal done_count
            ok = False
            skipped = False
            try:
                _, result = future.result()
                if result is None:
                    # Deliberate skip (no-show, or already graded in a
                    # prior run) — not a failure. Reported to on_progress
                    # as ok=None so the UI doesn't count it as one:
                    # confirmed live, a batch with 11 no-shows displayed
                    # "13 failed" when only 2 rows genuinely failed.
                    skipped = True
                else:
                    # A result with no score is an escalation, not a grade:
                    # write only the notes/reasoning cell so re-grading a
                    # row that already has scores cannot erase them.
                    if config.program_config.criterion_column_names:
                        if result["criterion_scores"]:
                            write_multi_row_result(
                                sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                                col_map, result["criterion_scores"], result["total_score"],
                                result["notes"],
                            )
                        else:
                            write_multi_notes_only(
                                sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                                col_map, result["notes"],
                            )
                    elif result["score"] is not None:
                        write_row_result(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, result["score"], result["reasoning"],
                        )
                    else:
                        write_reasoning_only(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, result["reasoning"],
                        )
                    # Failed writes remain retryable rather than becoming lost grades.
                    checkpoint.mark_done(
                        row_id,
                        result["human_review_flag"],
                    )
                    results[row_id] = result
                    ok = True
            except (RowCancelled, asyncio.CancelledError, FutureCancelledError):
                # Deliberate cancellation (the Streamlit Stop button) — the
                # same "deliberate skip" bucket as a no-show, not a scored
                # failure: no checkpoint entry, no sheet write. Covers all
                # three cancellation shapes that can reach here: process_row
                # itself raising RowCancelled, asyncio.CancelledError
                # escaping workflow.invoke (cancel_all_active() cancelled
                # the in-flight asyncio Task), and concurrent.futures' own
                # CancelledError — raised by future.result() itself,
                # synchronously and without blocking, for a future dropped
                # by the cancel_futures=True shutdown below before it ever
                # started (Future.result() checks the CANCELLED state
                # directly rather than going through the waiter machinery
                # that as_completed()/wait() rely on — see the long
                # comment on the completion loop below for why that
                # distinction matters here).
                skipped = True
            except Exception as exc:  # noqa: BLE001 — isolate each applicant failure
                # Provider messages may echo submitted PII, so persist only its type.
                attempts = checkpoint.mark_failed(row_id, type(exc).__name__)
                errors[row_id] = str(exc)
                # A row that keeps failing the same way run after run (an
                # oversized file, a structurally broken source URL) isn't a
                # transient blip — retrying it forever just wastes API calls
                # while leaving the sheet blank with zero explanation. After
                # FAILURE_ESCALATION_THRESHOLD attempts, stop retrying and
                # write an explicit human-review result instead, matching
                # how every other terminal outcome is surfaced.
                if attempts >= FAILURE_ESCALATION_THRESHOLD:
                    reasoning = (
                        f"[NEEDS HUMAN REVIEW] AI processing failed {attempts} times "
                        f"in a row (most recent error: {type(exc).__name__}). This "
                        "usually means an oversized or unreachable source file. "
                        "Manual review required."
                    )
                    if config.program_config.criterion_column_names:
                        write_multi_notes_only(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, reasoning,
                        )
                    else:
                        write_reasoning_only(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, reasoning,
                        )
                    checkpoint.mark_done(row_id, True)
            done_count += 1
            if on_progress is not None:
                on_progress(
                    done_count, submitted, row_id,
                    None if skipped else ok,
                )

        # Not `for future in as_completed(futures):` — a future that is
        # still QUEUED (submitted but not yet handed to a worker thread)
        # when pool.shutdown(cancel_futures=True) below cancels it can
        # structurally never be yielded by as_completed(), and the same is
        # true of concurrent.futures.wait() used the "obvious" way. Root
        # cause, from concurrent.futures/_base.py: shutdown()'s cancel
        # loop calls future.cancel() directly on each still-queued work
        # item, which only sets Future state to CANCELLED and runs
        # add_done_callback callbacks — it does NOT reach
        # set_running_or_notify_cancel(), the only method that both
        # transitions a future to CANCELLED_AND_NOTIFIED *and* notifies
        # any waiter registered on it (that method is called exclusively
        # from inside _WorkItem.run(), which a work item pulled straight
        # off the queue by shutdown() never reaches). as_completed() and
        # wait() both key off CANCELLED_AND_NOTIFIED/FINISHED — never
        # plain CANCELLED — so a waiter registered on one of these futures
        # simply never fires; as_completed()'s internal `while pending:`
        # loop then blocks forever. Future.done()/.cancelled(), by
        # contrast, correctly check for CANCELLED too, and future.result()
        # checks CANCELLED directly and raises immediately without
        # blocking — so this loop uses wait() only as a bounded-timeout
        # "has anything happened" signal, and always re-verifies via
        # .done() before deciding a future still needs waiting on.
        pending = set(futures)
        while pending:
            if (
                not pool_shutdown_for_cancel
                and cancel_event is not None
                and cancel_event.is_set()
            ):
                # Kill signal observed: drop every future that hasn't
                # started yet instead of still launching it. Futures
                # already running keep going briefly — they're cancelled
                # cooperatively via cancel_event/cancel_all_active(), not
                # by this call — but nothing new starts after this point.
                pool.shutdown(wait=False, cancel_futures=True)
                pool_shutdown_for_cancel = True

            # wait() is given a bounded timeout so it always returns
            # (rather than potentially blocking forever per the comment
            # above) and so cancel_event is re-checked promptly even when
            # nothing has finished yet. A future that completes normally
            # is still noticed immediately — FIRST_COMPLETED wakes wait()
            # as soon as anything finishes, the timeout only bounds the
            # "nothing has happened yet" case.
            done_now, not_done_now = wait(
                pending, timeout=CANCEL_CHECK_INTERVAL_SECONDS, return_when=FIRST_COMPLETED,
            )
            # wait()'s own bookkeeping mislabels cancelled-while-queued
            # futures as "not done" (see the long comment above) — recheck
            # each of those directly via .done(), which is accurate.
            truly_done = done_now | {f for f in not_done_now if f.done()}
            if not truly_done:
                continue
            for future in truly_done:
                pending.discard(future)
                row_id, sheet_row_number = futures[future]
                _handle_completed_future(future, row_id, sheet_row_number)
    finally:
        # Always shut down — wait=True (block until whatever's still
        # running finishes) unless a kill signal already triggered the
        # wait=False/cancel_futures=True shutdown above, in which case
        # calling shutdown() again is a safe no-op (ThreadPoolExecutor
        # tolerates repeated shutdown() calls).
        pool.shutdown(wait=not pool_shutdown_for_cancel)

    return {"graded": results, "errors": errors}

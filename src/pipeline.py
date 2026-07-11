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
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    write_multi_row_result,
    write_row_result,
)
from .video_urls import ResolvedVideo, VideoResolutionError, resolve_video_url
from .video_ingestion import ingest_pitch_deck, ingest_video_for_r2b

# Header exclusion lists are program-specific — see ProgramConfig fields
# excluded_header_substrings and excluded_header_names in src/programs.py.

# After this many consecutive failures on the same row, stop retrying
# blindly and escalate to human review instead — matches the "3 attempts"
# convention used elsewhere in this codebase (e.g. R2B's verify loop).
FAILURE_ESCALATION_THRESHOLD = 3


def _normalize_for_match(text: str) -> str:
    """Strip accents — used only for email-column detection, where ASCII
    matching is fine either way but consistency with the rest of the
    codebase's normalization habit costs nothing.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


# Markers a human adds directly into the Startup Name cell for applicants
# who didn't actually show up to pitch (confirmed real examples: "Example One -
# won't pitch", "Example Two no response", "Example Co - didn't
# respond"). Substring match on the normalized (lowercased, accent-
# stripped) name — these rows are skipped entirely before any LLM call.
_NO_SHOW_MARKERS = (
    "no response",
    "won't pitch",
    "wont pitch",
    "didn't respond",
    "didnt respond",
    "did not respond",
    "didn't come",
    "didnt come",
    "did not come",
)


def _is_no_show(startup_name: str) -> bool:
    name = _normalize_for_match(startup_name)
    return any(marker in name for marker in _NO_SHOW_MARKERS)


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
    from collections import Counter
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
    for i, col in enumerate(header):
        if any(hint in _normalize_for_match(col) for hint in _NAME_COLUMN_HINTS):
            return i
    return None


def _derive_row_id(
    header: list,
    row: list,
    sheet_row_number: int,
    duplicate_emails: frozenset = frozenset(),
) -> str:
    """Prefer an email column as the stable applicant ID (survives sheet
    re-sorts); fall back to the sheet row number if no email column is
    found, since *some* stable key is required for checkpointing.

    See _find_duplicate_emails: an email in duplicate_emails is
    disambiguated with the startup/company/team/project name column when
    one exists and is non-blank for this row (still survives a resort,
    unlike the row-number fallback used when no such column is found or
    it's blank here).
    """
    for i, col in enumerate(header):
        if "email" in _normalize_for_match(col):
            if i < len(row) and row[i].strip():
                email = row[i].strip().lower()
                if email in duplicate_emails:
                    name_idx = _find_name_column_index(header)
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
# interstitial or requires auth, so resolve_video_url returns a webpage with
# requires_url_context=True). These must go through Tier-2 (Drive API download
# → GCS upload) so the analyst receives the video as a native multimodal Part.
_DRIVE_HOSTS = frozenset({"drive.google.com", "drive.usercontent.google.com"})


def _is_drive_url(url: str) -> bool:
    """Detect Google Drive links that require Tier-2 ingestion.

    Tier-1 (resolve_video_url) cannot handle Drive: the link resolves to an
    HTML interstitial or an auth-walled download endpoint, so Tier-1 returns
    requires_url_context=True and the video Part is never created. Detecting
    the host explicitly lets us short-circuit to Tier-2 without paying for a
    metadata round-trip that we already know will not yield a fetchable URI.
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
    return bool(
        state.get("video_data")
        or state.get("video_url")
        or state.get("video_requires_url_context")
    )


def _retry_without_video(workflow, initial_state: dict, exc: Exception) -> dict:
    """The row had a video attached (as downloaded bytes, a direct URI, or
    a webpage for the analyst's url_context tool to read) and
    workflow.invoke failed. Confirmed live across three different failure
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
    no_video_state.pop("video_source", None)
    no_video_state["video_requires_url_context"] = False
    size_note = f" ({video_size / 1e6:.0f}MB)" if video_size else ""
    no_video_state["video_error"] = (
        f"Video{size_note} excluded from analysis — processing failed "
        f"({type(exc).__name__}). Scored on pitch deck and application "
        "text only."
    )
    return workflow.invoke(no_video_state)


def _build_workflow(config: Config) -> AdkReviewWorkflow:
    """Construct the workflow with program-aware models and sample count.

    Fellowship: single run (no multi-sample averaging).
    R2B: verify loop once, then Head re-run n_samples times and averaged.
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
):
    row_id = _derive_row_id(header, row, sheet_row_number, duplicate_emails)
    if checkpoint.is_done(row_id) and not force:
        return row_id, None  # already graded in a prior run, nothing to write

    for i, col in enumerate(header):
        if _normalize_for_match(col) in ("startup name", "company name", "team name"):
            if i < len(row) and _is_no_show(row[i]):
                # Didn't show up to pitch — nothing to grade. No checkpoint
                # entry either, so removing the marker text later makes the
                # row eligible again on the next run.
                return row_id, None
            break

    initial_state = {
        "row_id": row_id,
        "raw_row_text": _build_raw_row_text(
            header,
            row,
            excluded_header_names=config.program_config.excluded_header_names,
            excluded_header_substrings=config.program_config.excluded_header_substrings,
        ),
        "retry_count": 0,
    }
    submitted_video_url = _submitted_video_url(header, row)
    if submitted_video_url:
        initial_state["submitted_video_url"] = submitted_video_url
        # Video resolution is source_priority-agnostic so that Drive videos
        # are ingested as native multimodal Parts for every program, not just
        # R2B. The path is:
        #   1. Tier-1 (resolve_video_url) — cheap, no download; covers
        #      YouTube natively, direct HTTPS videos ≤100MB, and webpages
        #      with discoverable video metadata (og:video / <video> / JSON-LD).
        #   2. If Tier-1 fails outright, or the URL is a Google Drive link,
        #      fall back to Tier-2 (ingest_video_for_r2b) which downloads via
        #      the Drive API and uploads to Files API / inline bytes so the
        #      analyst receives the video as a native Part.
        #   3. If Tier-1 resolves to requires_url_context=True (a webpage with
        #      no discoverable direct video — e.g. Canva, Loom share pages),
        #      that result is used as-is: the analyst's url_context tool reads
        #      the page live. Tier-2 must NOT download and re-upload that
        #      webpage — it isn't video data, and disguising HTML as a video
        #      Part sends garbage input to Gemini instead of an honest
        #      "can't resolve this video" (see video_ingestion.py docstring).
        #   4. If Tier-2 also fails, record video_error; the workflow surfaces
        #      "VIDEO UNAVAILABLE: ..." to the analyst.
        resolved_video: ResolvedVideo | None = None
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
                # Vertex's 15MB URI-fetch limit; see ingest_video_for_r2b),
                # but because Tier-1 alone has no way to make that
                # size-aware decision.
                not resolved_video.requires_url_context
                and "youtube" not in resolved_video.uri
                and "youtu.be" not in resolved_video.uri
            )
        )
        if needs_tier2:
            try:
                resolved_video = ingest_video_for_r2b(
                    submitted_video_url,
                    config.service_account_path,
                )
            except VideoResolutionError as exc:
                # Preserve the Tier-1 error when Tier-2 also fails so the
                # analyst sees the most informative message.
                initial_state["video_error"] = str(exc)
                resolved_video = None

        if resolved_video is not None:
            initial_state["video_url"] = resolved_video.uri
            initial_state["video_data"] = resolved_video.data
            initial_state["video_mime_type"] = resolved_video.mime_type
            initial_state["video_source"] = resolved_video.source
            initial_state["video_requires_url_context"] = (
                resolved_video.requires_url_context
            )
            initial_state["video_original_size_bytes"] = (
                resolved_video.original_size_bytes
            )
            # Tier-2 succeeded: clear any stale error left by a Tier-1
            # failure. Without this, the workflow would both build the video
            # Part (video_url is set) AND append "VIDEO UNAVAILABLE: ..."
            # (video_error is set) — a contradictory state for the analyst.
            initial_state.pop("video_error", None)

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
        and not initial_state.get("video_requires_url_context")
    ):
        initial_state.setdefault(
            "video_error",
            "No video URL submitted or video could not be resolved.",
        )

    # Alchemist: pitch deck is required. If no pitch deck URL is submitted,
    # score all criteria as 0 and skip the LLM pipeline entirely — this saves
    # API costs and enforces the requirement.
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
                "skipped_no_pitch_deck": True,
            }
        initial_state["submitted_pitch_deck_url"] = submitted_pitch_deck_url
        try:
            resolved_deck = ingest_pitch_deck(
                submitted_pitch_deck_url,
                config.service_account_path,
                config.analyzer_model,
            )
            initial_state["pitch_deck_url"] = resolved_deck.uri
            initial_state["pitch_deck_data"] = resolved_deck.data
            initial_state["pitch_deck_mime_type"] = resolved_deck.mime_type
            initial_state["pitch_deck_source"] = resolved_deck.source
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
                "skipped_no_pitch_deck": True,
            }

    try:
        final_state = workflow.invoke(initial_state)
    except Exception as exc:
        if _row_has_video(initial_state):
            final_state = _retry_without_video(workflow, initial_state, exc)
        else:
            raise
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
        # Only blank the score when it's genuinely unknown (evidence never
        # approved / all Head samples failed). A confirmed disqualification
        # (lie/fraud/contradiction) already decided the score is 0 — that's
        # a real, deliberate result, not an unresolved one, and blanking it
        # here was silently erasing every disqualification override before
        # it ever reached the sheet.
        if score is None:
            score = ""

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
            criterion_scores = {}
            criterion_rationale = {}

        if criterion_scores:
            total_score = sum(criterion_scores.values()) / len(criterion_scores)
            notes = "\n".join(
                f"{criterion}: {criterion_rationale.get(criterion, '')}"
                for criterion in config.program_config.rubric_criteria
            )
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


def run_one(config: Config, row_index: int = 0, *, force: bool = False) -> dict:
    """Run the complete ADK workflow for one sheet row and write its result."""
    sheets_service = get_sheets_service(config.service_account_path)
    checkpoint = Checkpoint(config.checkpoint_path)
    workflow = _build_workflow(config)

    header, rows = read_sheet_rows(
        sheets_service,
        config.sheet_id,
        config.sheet_range,
        config.header_row,
    )
    # Skip non-data rows that sit between the header and the first applicant
    # row (e.g. Fellowship V2's sub-header row 2). data_start_offset is 0 for
    # Fellowship/R2B (data starts right after the header), so this is a no-op
    # for them and preserves their existing behavior exactly.
    offset = config.program_config.data_start_offset
    rows = rows[offset:]
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(f"Applicant row index {row_index} is out of range for {len(rows)} rows.")

    sheet_name = config.sheet_range.split("!")[0]
    sheet_row_number = config.header_row + row_index + 1 + offset
    # When there is no separate merged top-label row (top_label_row=0, as on
    # the Fellowship V2 sheet), output columns live on the header row itself —
    # resolve them from `header` instead of fetching a nonexistent row 0.
    if config.top_label_row > 0:
        top_label_header = fetch_sheet_row(
            sheets_service,
            config.sheet_id,
            sheet_name,
            config.top_label_row,
        )
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

    duplicate_emails = _find_duplicate_emails(header, rows)
    row_id, result = process_row(
        config,
        workflow,
        header,
        rows[row_index],
        sheet_row_number,
        checkpoint,
        force=force,
        duplicate_emails=duplicate_emails,
    )
    if result is None:
        return {"row_id": row_id, "skipped": True}

    if config.program_config.criterion_column_names:
        write_multi_row_result(
            sheets_service,
            config.sheet_id,
            sheet_name,
            sheet_row_number,
            col_map,
            result["criterion_scores"],
            result["total_score"],
            result["notes"],
        )
    else:
        write_row_result(
            sheets_service,
            config.sheet_id,
            sheet_name,
            sheet_row_number,
            col_map,
            result["score"],
            result["reasoning"],
        )
    # Checkpoint only after the authoritative external write succeeds.
    checkpoint.mark_done(row_id, result["human_review_flag"])
    return {"row_id": row_id, **result}


def run_batch(
    config: Config,
    force: bool = False,
    limit: int | None = None,
    on_progress=None,
):
    """Run the batch. on_progress(done, total, row_id, ok), if given, is
    called synchronously on the calling thread right after each row
    finishes (success or failure) — safe for a caller like a Streamlit
    script to update a progress bar without needing its own thread.
    """
    sheets_service = get_sheets_service(config.service_account_path)
    checkpoint = Checkpoint(config.checkpoint_path)
    workflow = _build_workflow(config)

    header, rows = read_sheet_rows(sheets_service, config.sheet_id, config.sheet_range, config.header_row)
    sheet_name = config.sheet_range.split("!")[0]

    # Skip non-data rows between the header and the first applicant row
    # (Fellowship V2's sub-header row 2). No-op for Fellowship/R2B where
    # data_start_offset=0.
    offset = config.program_config.data_start_offset
    rows = rows[offset:]

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
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        futures = {}
        submitted = 0
        for i, row in enumerate(rows):
            if limit is not None and submitted >= limit:
                break
            sheet_row_number = i + config.header_row + 1 + offset  # header_row + sub-header skip + 1-indexing
            row_id_preview = _derive_row_id(header, row, sheet_row_number, duplicate_emails)
            if checkpoint.is_done(row_id_preview) and not force:
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
                force=force,
                duplicate_emails=duplicate_emails,
            )
            futures[future] = (row_id_preview, sheet_row_number)

        done_count = 0
        for future in as_completed(futures):
            row_id, sheet_row_number = futures[future]
            ok = False
            try:
                _, result = future.result()
                if result is None:
                    continue
                if config.program_config.criterion_column_names:
                    write_multi_row_result(
                        sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                        col_map, result["criterion_scores"], result["total_score"],
                        result["notes"],
                    )
                else:
                    write_row_result(
                        sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                        col_map, result["score"], result["reasoning"],
                    )
                # Failed writes remain retryable rather than becoming lost grades.
                checkpoint.mark_done(
                    row_id,
                    result["human_review_flag"],
                )
                results[row_id] = result
                ok = True
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
                        write_multi_row_result(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, {}, None, reasoning,
                        )
                    else:
                        write_row_result(
                            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                            col_map, "", reasoning,
                        )
                    checkpoint.mark_done(row_id, True)
            finally:
                done_count += 1
                if on_progress is not None:
                    on_progress(done_count, submitted, row_id, ok)

    return {"graded": results, "errors": errors}

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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from .checkpoint import Checkpoint
from .config import Config
from .adk_agents import AdkReviewWorkflow
from .google_clients import (
    fetch_sheet_row,
    get_sheets_service,
    read_sheet_rows,
    resolve_output_columns,
    write_row_result,
)
from .video_urls import VideoResolutionError, resolve_video_url

# Excluded from what the analyzer sees — output columns themselves (would
# be circular), and human-given score columns. "ception" matches this
# sheet's "General Pereception" columns (the real header has a typo —
# an extra "e" after "Per" — "ception" is the common suffix of both the
# correct and the typo'd spelling, so it survives that) without hardcoding
# reviewer names, which change every cohort. Excluding these isn't just
# tidiness: feeding the AI a human's already-given score would anchor its
# judgment instead of producing an independent one.
EXCLUDED_HEADER_SUBSTRINGS = ("ception",)
EXCLUDED_HEADER_NAMES = {"", "AI", "AI Reasoning", "Total"}


def _normalize_for_match(text: str) -> str:
    """Strip accents — used only for email-column detection, where ASCII
    matching is fine either way but consistency with the rest of the
    codebase's normalization habit costs nothing.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).lower()


def _derive_row_id(header: list, row: list, sheet_row_number: int) -> str:
    """Prefer an email column as the stable applicant ID (survives sheet
    re-sorts); fall back to the sheet row number if no email column is
    found, since *some* stable key is required for checkpointing.
    """
    for i, col in enumerate(header):
        if "email" in _normalize_for_match(col):
            if i < len(row) and row[i].strip():
                return row[i].strip().lower()
            break
    return f"row_{sheet_row_number}"


def _build_raw_row_text(header: list, row: list) -> str:
    """Every eligible question/answer labeled as plain text for both agents."""
    lines = []
    for i, col in enumerate(header):
        if col in EXCLUDED_HEADER_NAMES or any(sub in col.lower() for sub in EXCLUDED_HEADER_SUBSTRINGS):
            continue
        value = row[i] if i < len(row) else ""
        lines.append(f"{col}: {value}")
    return "\n".join(lines)


def _submitted_video_url(header: list, row: list) -> str:
    for i, col in enumerate(header):
        if "video" in _normalize_for_match(col):
            return row[i].strip() if i < len(row) else ""
    return ""


def process_row(
    config: Config,
    workflow,
    header: list,
    row: list,
    sheet_row_number: int,
    checkpoint: Checkpoint,
    *,
    force: bool = False,
):
    row_id = _derive_row_id(header, row, sheet_row_number)
    if checkpoint.is_done(row_id) and not force:
        return row_id, None  # already graded in a prior run, nothing to write

    initial_state = {
        "row_id": row_id,
        "raw_row_text": _build_raw_row_text(header, row),
        "retry_count": 0,
    }
    submitted_video_url = _submitted_video_url(header, row)
    if submitted_video_url:
        initial_state["submitted_video_url"] = submitted_video_url
        try:
            resolved_video = resolve_video_url(submitted_video_url)
            initial_state["video_url"] = resolved_video.uri
            initial_state["video_mime_type"] = resolved_video.mime_type
            initial_state["video_source"] = resolved_video.source
            initial_state["video_requires_url_context"] = (
                resolved_video.requires_url_context
            )
        except VideoResolutionError as exc:
            initial_state["video_error"] = str(exc)

    final_state = workflow.invoke(initial_state)
    final_result = final_state.get("final_result", {})
    score = final_result.get("score")
    reasoning = final_result.get("reasoning", "")
    human_review_flag = final_state.get("human_review_flag", False)
    if human_review_flag:
        # No dedicated flag column exists on the real sheet — fold the
        # signal into the reasoning text itself rather than mutating the
        # sheet's structure (which has formulas referencing specific
        # columns already).
        reasoning = f"[NEEDS HUMAN REVIEW] {reasoning}"
        score = ""

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
    workflow = AdkReviewWorkflow(config.analyzer_model, config.grader_model)

    header, rows = read_sheet_rows(
        sheets_service,
        config.sheet_id,
        config.sheet_range,
        config.header_row,
    )
    if row_index < 0 or row_index >= len(rows):
        raise IndexError(f"Applicant row index {row_index} is out of range for {len(rows)} rows.")

    sheet_name = config.sheet_range.split("!")[0]
    sheet_row_number = config.header_row + row_index + 1
    top_label_header = fetch_sheet_row(
        sheets_service,
        config.sheet_id,
        sheet_name,
        config.top_label_row,
    )
    col_map = resolve_output_columns(
        top_label_header,
        score_column_letter=config.ai_score_column,
        reasoning_column_letter=config.ai_reasoning_column,
    )

    row_id, result = process_row(
        config,
        workflow,
        header,
        rows[row_index],
        sheet_row_number,
        checkpoint,
        force=force,
    )
    if result is None:
        return {"row_id": row_id, "skipped": True}

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


def run_batch(config: Config):
    sheets_service = get_sheets_service(config.service_account_path)
    checkpoint = Checkpoint(config.checkpoint_path)
    workflow = AdkReviewWorkflow(config.analyzer_model, config.grader_model)

    header, rows = read_sheet_rows(sheets_service, config.sheet_id, config.sheet_range, config.header_row)
    sheet_name = config.sheet_range.split("!")[0]

    # "AI" / "AI Reasoning" are named on the merged TOP label row, not the
    # per-column header row `header` holds — fetch that row separately
    # rather than requiring a letter override for every sheet shaped this way.
    top_label_header = fetch_sheet_row(sheets_service, config.sheet_id, sheet_name, config.top_label_row)
    col_map = resolve_output_columns(
        top_label_header,
        score_column_letter=config.ai_score_column,
        reasoning_column_letter=config.ai_reasoning_column,
    )

    results = {}
    errors = {}

    # max_workers bounded by config rather than len(rows) — uncapped
    # concurrency against the Gemini API at 100+ rows risks hitting
    # rate limits and burning retries on 429s instead of real work.
    with ThreadPoolExecutor(max_workers=config.max_concurrency) as pool:
        futures = {}
        for i, row in enumerate(rows):
            sheet_row_number = i + config.header_row + 1  # header_row itself, then 1-indexing
            row_id_preview = _derive_row_id(header, row, sheet_row_number)
            if checkpoint.is_done(row_id_preview):
                continue
            future = pool.submit(
                process_row,
                config,
                workflow,
                header,
                row,
                sheet_row_number,
                checkpoint,
            )
            futures[future] = (row_id_preview, sheet_row_number)

        for future in as_completed(futures):
            row_id, sheet_row_number = futures[future]
            try:
                _, result = future.result()
                if result is None:
                    continue
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
            except Exception as exc:  # noqa: BLE001 — isolate each applicant failure
                # Provider messages may echo submitted PII, so persist only its type.
                checkpoint.mark_failed(row_id, type(exc).__name__)
                errors[row_id] = str(exc)

    return {"graded": results, "errors": errors}

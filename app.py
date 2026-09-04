"""
Streamlit dashboard for the grading agent (Alchemist, Fellowship V2, R2B).

Run from the repo root:
    streamlit run app.py

Sidebar controls: pick which program to run, then which sheet/tab/header
row to target (defaults to that program's .env values, but overridable —
needed for R2B specifically, which runs multiple competition rounds, each
its own separate Google Sheet). Below that: how many rows to grade this
run, whether to re-grade already-graded rows, and checkpoint reset. The
main area shows current sheet grading state and the results of the most
recent run.
"""
import json
import os
import sys
import threading
import time

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from streamlit.runtime.scriptrunner_utils.exceptions import ScriptControlException

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_REPO_DIR, ".env"), override=True)
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from src.adk_agents import cancel_all_active
from src.checkpoint import Checkpoint
from src.config import Config
from src.pipeline import (
    run_batch,
    _derive_row_id,
    _find_duplicate_emails,
    _find_name_column_index,
    _resolve_name_column_index,
)
from src.programs import DEFAULT_SHEET_RANGE, get_program_config
from src.google_clients import get_sheets_service, read_sheet_rows

PROGRAM_LABELS = {
    "alchemist": "Alchemist",
    "fellowship_v2": "Fellowship V2",
    "r2b": "R2B",
}

st.set_page_config(page_title="Grading Agent", layout="wide")

with st.sidebar:
    st.header("Program & sheet")
    program = st.selectbox(
        "Program",
        options=list(PROGRAM_LABELS.keys()),
        format_func=lambda p: PROGRAM_LABELS[p],
    )
    _program_config = get_program_config(program)
    _default_sheet_id = os.environ.get(_program_config.sheet_id_env, "")
    _default_sheet_range = os.environ.get(
        _program_config.sheet_range_env, DEFAULT_SHEET_RANGE,
    )
    _default_header_row = int(os.environ.get(
        _program_config.header_row_env, str(_program_config.default_header_row),
    ))
    _default_top_label_row = int(os.environ.get(
        _program_config.top_label_row_env, str(_program_config.default_top_label_row),
    ))

    sheet_id_input = st.text_input(
        "Sheet ID",
        value=_default_sheet_id,
        help="From the sheet's URL. Defaults to this program's .env value — "
        "override for a different sheet, e.g. a different R2B competition round.",
    )
    sheet_range_input = st.text_input(
        "Sheet tab / range",
        value=_default_sheet_range,
        help="Tab name (e.g. 'Grading Final'), or an A1 range starting "
        "at row 1.",
    )
    header_row_input = st.number_input(
        "Header row",
        min_value=1, max_value=100, value=_default_header_row, step=1,
    )
    top_label_row_input = st.number_input(
        "Top label row",
        min_value=0, max_value=100, value=_default_top_label_row, step=1,
        help="Row number of a merged group-header row ABOVE the real "
        "column headers (e.g. reviewer names spanning several sub-"
        "columns). Set to 0 if this sheet has no such row — output "
        "columns are then resolved from the header row itself.",
    )

    # Column mapping: sheet layouts aren't assumed fixed across programs/
    # sheets, so let the operator map columns per connection instead of
    # relying on ProgramConfig's hardcoded score_column_name/
    # reasoning_column_name (or, for R2B's multi-column shape,
    # criterion_column_names/total_score_column_name/notes_column_name)
    # and excluded_header_*. A lightweight header-only read (no full row
    # fetch) populates the dropdowns; this can error before a valid sheet
    # ID+tab are both entered, so it's wrapped and silently skipped rather
    # than shown as an error — the "Config error" below already covers a
    # genuinely bad sheet ID/tab once the user finishes typing.
    score_column_override = None
    reasoning_column_override = None
    name_column_override = None
    ignored_columns_override = None
    criterion_column_overrides = None
    total_score_column_override = None
    notes_column_override = None
    if sheet_id_input and sheet_range_input:
        try:
            _preview_service = get_sheets_service(
                os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "")
            )
            _preview_header, _ = read_sheet_rows(
                _preview_service, sheet_id_input, sheet_range_input,
                int(header_row_input),
            )
        except Exception:
            _preview_header = []

        if _preview_header:
            st.divider()
            st.subheader("Column mapping")

            if _program_config.criterion_column_names is not None:
                # Multi-column shape (R2B today): one sheet column per
                # rubric criterion, plus Total Score and Notes/Comments
                # columns, instead of a single score+reasoning pair.
                criterion_column_overrides = {}
                for _criterion in _program_config.rubric_criteria:
                    _hardcoded = _program_config.criterion_column_names.get(_criterion, "")
                    if _hardcoded in _preview_header:
                        _default_idx = _preview_header.index(_hardcoded)
                    else:
                        _default_idx = None
                        st.warning(
                            f"Default '{_hardcoded}' column not found in this sheet — "
                            f"please select the correct '{_criterion}' column."
                        )
                    _criterion_selection = st.selectbox(
                        f"{_criterion} column",
                        options=_preview_header, index=_default_idx,
                        placeholder="Select a column…",
                        help=f"Column this run writes the '{_criterion}' score to.",
                    )
                    # Only record a real (non-None) selection here — Config.
                    # from_env's criterion_column_overrides dict gets blindly
                    # dict.update()-ed into the hardcoded defaults, so a
                    # {criterion: None} entry from a left-unselected dropdown
                    # would overwrite (and destroy) an otherwise-valid
                    # hardcoded default for THAT criterion instead of leaving
                    # it alone, unlike the scalar overrides below which each
                    # get their own `if x:` None-safe check.
                    if _criterion_selection is not None:
                        criterion_column_overrides[_criterion] = _criterion_selection
                if _program_config.total_score_column_name in _preview_header:
                    _default_total_idx = _preview_header.index(
                        _program_config.total_score_column_name
                    )
                else:
                    _default_total_idx = None
                    st.warning(
                        f"Default '{_program_config.total_score_column_name}' column not "
                        "found in this sheet — please select the correct Total Score column."
                    )
                total_score_column_override = st.selectbox(
                    "Total Score column", options=_preview_header, index=_default_total_idx,
                    placeholder="Select a column…",
                    help="Column this run writes the total score to.",
                )
                if _program_config.notes_column_name in _preview_header:
                    _default_notes_idx = _preview_header.index(
                        _program_config.notes_column_name
                    )
                else:
                    _default_notes_idx = None
                    st.warning(
                        f"Default '{_program_config.notes_column_name}' column not found "
                        "in this sheet — please select the correct Notes / Comments column."
                    )
                notes_column_override = st.selectbox(
                    "Notes / Comments column", options=_preview_header, index=_default_notes_idx,
                    placeholder="Select a column…",
                    help="Column this run writes the AI's reasoning/notes to.",
                )
                _mapped_output_columns = (
                    set(criterion_column_overrides.values())
                    | {total_score_column_override, notes_column_override}
                ) - {None}
            else:
                if _program_config.score_column_name in _preview_header:
                    _default_score_idx = _preview_header.index(
                        _program_config.score_column_name
                    )
                else:
                    _default_score_idx = None
                    st.warning(
                        f"Default '{_program_config.score_column_name}' column not found "
                        "in this sheet — please select the correct Score column."
                    )
                if _program_config.reasoning_column_name in _preview_header:
                    _default_reasoning_idx = _preview_header.index(
                        _program_config.reasoning_column_name
                    )
                else:
                    _default_reasoning_idx = None
                    st.warning(
                        f"Default '{_program_config.reasoning_column_name}' column not "
                        "found in this sheet — please select the correct Reasoning column."
                    )
                score_column_override = st.selectbox(
                    "Score column", options=_preview_header, index=_default_score_idx,
                    placeholder="Select a column…",
                    help="Column this run writes the numeric score to.",
                )
                reasoning_column_override = st.selectbox(
                    "Reasoning column", options=_preview_header, index=_default_reasoning_idx,
                    placeholder="Select a column…",
                    help="Column this run writes the AI's reasoning to.",
                )
                _mapped_output_columns = (
                    {score_column_override, reasoning_column_override} - {None}
                )

            # Name column: which sheet column identifies the applicant/
            # startup by name — used by pipeline.py for no-show detection,
            # R2B's wrong-segment tripwire note, and duplicate-email
            # disambiguation (see src.pipeline._resolve_name_column_index).
            # Independent of the single-column vs multi-column split above
            # (every program has applicants with names regardless of
            # output-column shape), so this executes exactly once
            # regardless of which branch ran, rather than being duplicated
            # in both. Defaults to the existing hint-based auto-detect
            # (_find_name_column_index — the same "startup/company/team/
            # project name" guess ProgramConfig.name_column_name=None keeps
            # using) so a sheet with a conventional header still
            # pre-selects the right column; leaves the dropdown unselected
            # (index=None, forcing a conscious operator choice instead of a
            # silent wrong guess) when no hint matches, e.g. Fellowship V2's
            # "Participant Name"-style header.
            _default_name_idx = _find_name_column_index(_preview_header)
            if _default_name_idx is None:
                st.warning(
                    "Could not auto-detect a Name column in this sheet — "
                    "please select the correct column identifying the "
                    "applicant/startup by name."
                )
            name_column_override = st.selectbox(
                "Name column",
                options=_preview_header, index=_default_name_idx,
                placeholder="Select a column…",
                help="Column identifying the applicant/startup by name — "
                "used for no-show detection, R2B's wrong-segment tripwire "
                "note, and duplicate-email disambiguation.",
            )

            # Pre-check whatever the hardcoded defaults would have excluded
            # (PII columns, output columns) so switching to a new sheet
            # doesn't silently feed those to the AI just because nobody
            # re-picked them here. Also pre-check the columns just mapped
            # above — redundant with the always-applied exclusion in
            # config.py, but harmless and keeps the multiselect honest
            # about what's actually hidden from the AI.
            _default_ignored = [
                c for c in _preview_header
                if c in _program_config.excluded_header_names
                or any(s in c.lower() for s in _program_config.excluded_header_substrings)
                or c in _mapped_output_columns
            ]
            ignored_columns_override = st.multiselect(
                "Columns to ignore (hidden from the AI)",
                options=_preview_header, default=_default_ignored,
            )

st.title(f"{PROGRAM_LABELS[program]} Grading Agent")

try:
    config = Config.from_env(
        program,
        sheet_id_override=sheet_id_input or None,
        sheet_range_override=sheet_range_input or None,
        header_row_override=int(header_row_input),
        top_label_row_override=int(top_label_row_input),
        score_column_override=score_column_override,
        reasoning_column_override=reasoning_column_override,
        name_column_override=name_column_override,
        ignored_columns_override=(
            tuple(ignored_columns_override) if ignored_columns_override else None
        ),
        criterion_column_overrides=criterion_column_overrides,
        total_score_column_override=total_score_column_override,
        notes_column_override=notes_column_override,
    )
except RuntimeError as exc:
    st.error(f"Config error: {exc}")
    st.stop()

st.caption(
    f"Sheet: `{config.sheet_id}` · Range: `{config.sheet_range}` · "
    f"Models: {config.analyzer_model} / {config.grader_model} / {config.head_model} · "
    f"N_SAMPLES={config.n_samples} · MAX_CONCURRENCY={config.max_concurrency} · "
    f"Checkpoint: `{config.checkpoint_path}`"
)

with st.sidebar:
    st.header("Run settings")
    force = st.checkbox(
        "Re-grade already-graded rows",
        value=False,
        help="Off: only grade rows with no score yet. On: re-grade every "
        "row in range, overwriting existing scores.",
    )
    limit = st.number_input(
        "Max rows to process this run",
        min_value=1, max_value=1000, value=10, step=1,
    )
    run_clicked = st.button("Run grading", type="primary", use_container_width=True)

    st.divider()
    st.header("Checkpoint")
    checkpoint_exists = os.path.isfile(config.checkpoint_path)
    if checkpoint_exists:
        with open(config.checkpoint_path) as f:
            checkpoint_data = json.load(f)
        status_counts = {"done": 0, "human_review": 0, "failed": 0}
        for entry in checkpoint_data.values():
            status = entry.get("status")
            if status in status_counts:
                status_counts[status] += 1
        # done + human_review are both a completed grade (a score, or a
        # definitive human-review outcome) — only "failed" is a genuine
        # error, so that's the only split worth showing here.
        graded = status_counts["done"] + status_counts["human_review"]
        st.caption(
            f"{graded} row(s) graded (will be skipped next run) · "
            f"{status_counts['failed']} row(s) failed — genuine errors, "
            "will retry next run."
        )
    else:
        st.caption("No checkpoint file yet.")

    confirm_reset = st.checkbox("Confirm reset")
    if st.button(
        "Reset checkpoint",
        disabled=not confirm_reset,
        use_container_width=True,
        help="Clears all done/failed tracking. Existing scores already "
        "written to the sheet are NOT deleted — only the pipeline's memory "
        "of what it has graded is cleared, so the next run will re-grade "
        "everything unless you also set a limit.",
    ):
        with open(config.checkpoint_path, "w") as f:
            f.write("{}")
        st.success("Checkpoint reset.")
        st.rerun()

    st.divider()
    st.header("Test a single row")
    test_row_number = st.number_input(
        "Sheet row number",
        min_value=1, max_value=100000,
        value=config.header_row + 1, step=1,
        help="Grade exactly this row (numbered as it appears in the actual "
        "Google Sheet), bypassing the checkpoint — always re-runs even if "
        "already graded. Useful for testing a prompt/config change on one "
        "known row before running a full batch.",
    )
    test_row_clicked = st.button(
        "Grade this row only", use_container_width=True,
    )

if run_clicked or test_row_clicked:
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    # The progress bar/text below only update once a ROW FINISHES (via
    # on_progress, called from run_batch's per-future completion handler)
    # — for a single row that can take 1-3+ minutes (video download,
    # verify loop, multi-sample Head scoring), that's several minutes of
    # a blank progress area with zero feedback that anything is happening
    # at all, easily read as "stuck". Show something immediately instead.
    status_text.text("Starting — resolving sheet rows and launching grading…")

    # run_batch runs on a background thread instead of blocking this script
    # thread directly. Why: Streamlit's Stop button is purely cooperative —
    # a pending stop request is only checked and raised (StopException) when
    # the script itself makes an st.* call (each is the only kind of
    # "yield point" the script runner checks). A direct, blocking run_batch
    # call makes zero st.* calls of its own, so Stop could never fire at
    # all for a single-row test grade, and for a multi-row batch could only
    # fire between completed rows. Running it on its own thread lets this
    # (main, script) thread poll on a short interval and call real st.*
    # methods every ~0.5s — genuine yield points — while the batch is still
    # in flight, so Stop actually has somewhere to interrupt.
    #
    # cancel_event/`shared` cross the thread boundary deliberately: the
    # background thread NEVER calls any st.* function itself (unsupported/
    # unsafe off the script thread) — it only ever writes into `shared`
    # under `shared_lock`; only this main thread's poll loop below reads
    # `shared` and makes the actual st.* calls.
    cancel_event = threading.Event()
    shared_lock = threading.Lock()
    shared = {
        "done": 0,
        "total": 0,
        "ok_count": 0,
        "fail_count": 0,
        "skip_count": 0,
        "result": None,
        "exc": None,
        "finished": False,
    }

    def _on_progress(done, total, row_id, ok):
        # Called synchronously on the background thread by run_batch —
        # must not touch any st.* call here (see note above).
        with shared_lock:
            shared["done"] = done
            shared["total"] = total
            # ok=None means deliberately skipped (no-show, already graded,
            # or cancelled) — neither a grade nor a failure. Without the
            # separate bucket, a batch with 11 no-shows displayed
            # "13 failed" when only 2 rows genuinely failed (confirmed live).
            if ok is None:
                shared["skip_count"] += 1
            elif ok:
                shared["ok_count"] += 1
            else:
                shared["fail_count"] += 1

    def _run_batch_worker():
        try:
            if test_row_clicked:
                result = run_batch(
                    config, force=True, limit=1, on_progress=_on_progress,
                    target_row_number=int(test_row_number),
                    cancel_event=cancel_event,
                )
            else:
                result = run_batch(
                    config, force=force, limit=int(limit), on_progress=_on_progress,
                    cancel_event=cancel_event,
                )
            with shared_lock:
                shared["result"] = result
        except Exception as exc:  # noqa: BLE001 — surfaced on the main thread below
            with shared_lock:
                shared["exc"] = exc
        finally:
            with shared_lock:
                shared["finished"] = True

    worker_thread = threading.Thread(target=_run_batch_worker, daemon=True)
    worker_thread.start()

    try:
        while True:
            with shared_lock:
                done = shared["done"]
                total = shared["total"]
                ok_count = shared["ok_count"]
                skip_count = shared["skip_count"]
                fail_count = shared["fail_count"]
                finished = shared["finished"]
            # Each of these two calls is a real st.* yield point — this is
            # what makes Stop actually checkable while run_batch is still
            # running on the background thread.
            progress_bar.progress(done / total if total else 0.0)
            if total:
                status_text.text(
                    f"{done}/{total} processed — {ok_count} graded, "
                    f"{skip_count} skipped (no-show / already graded), "
                    f"{fail_count} failed"
                )
            else:
                status_text.text("Starting — resolving sheet rows and launching grading…")
            if finished:
                break
            time.sleep(0.5)
        worker_thread.join(timeout=5)
    except ScriptControlException:
        # Stop or a rerun (a widget interaction, or Streamlit's "Rerun"):
        # StopException and RerunException both subclass this, and both
        # abandon this poll loop, leaving the batch running with nobody
        # reading `shared` — so both must cancel the run rather than
        # orphan it. Set the cooperative flag every process_row call
        # checks, and cancel every in-flight Gemini/ADK asyncio Task
        # directly (the near-instant path — a row could otherwise be
        # minutes deep into a single Gemini call with no other checkpoint
        # to notice cancel_event). Then wait briefly for the background
        # thread to unwind before reporting back.
        cancel_event.set()
        cancel_all_active()
        worker_thread.join(timeout=5)
        with shared_lock:
            ok_count = shared["ok_count"]
        progress_bar.empty()
        status_text.empty()
        st.warning(
            f"⛔ API request killed — grading stopped ({ok_count} graded "
            "before cancellation)."
        )
        raise

    progress_bar.empty()
    status_text.empty()

    with shared_lock:
        result = shared["result"]
        exc = shared["exc"]

    if exc is not None:
        # Re-raise on the main thread so Streamlit's normal uncaught-
        # exception display handles it exactly as it would have before
        # run_batch moved to a background thread (an exception raised
        # directly on the script thread).
        raise exc

    st.success(f"Graded: {len(result['graded'])} | Errors: {len(result['errors'])}")
    if test_row_clicked and not result["graded"] and not result["errors"]:
        st.warning(
            f"Row {int(test_row_number)} was not graded — it may not exist "
            "in the current sheet range, or was deliberately skipped "
            "(no-show, missing required source, etc)."
        )
    if result["errors"]:
        with st.expander(f"{len(result['errors'])} error(s)"):
            for row_id, err in result["errors"].items():
                st.text(f"{row_id}: {err[:300]}")

st.subheader("Current sheet state")
if st.button("🔄 Refresh data"):
    st.rerun()

sheets_service = get_sheets_service(config.service_account_path)
try:
    header, rows = read_sheet_rows(
        sheets_service, config.sheet_id, config.sheet_range, config.header_row
    )
except Exception as exc:  # noqa: BLE001 — operator-facing surface
    # A wrong tab name used to crash the WHOLE app with an uncaught
    # traceback ("Unable to parse range: ...") — confirmed live when a
    # Sheet ID was switched while the tab field still held the previous
    # sheet's tab name. Fail with guidance instead.
    st.error(
        f"Could not read the sheet: {exc}\n\n"
        "Most common cause: the 'Sheet tab / range' field doesn't match "
        "any tab on the selected Sheet ID — tab names differ per sheet "
        "(check the tabs at the bottom of the Google Sheet and update "
        "the sidebar field)."
    )
    st.stop()

# Whitespace/case-tolerant column lookup: the real R2B sheet has headers
# like "Startup Name" (capital N) and "Total Score " (trailing space) —
# exact header.index() matching silently found NOTHING for R2B (score
# column "AI" doesn't exist in multi-column mode at all), so this whole
# section showed Graded=0 and an empty table on a fully graded sheet.
_col_lookup = {}
for _i, _col in enumerate(header):
    _col_lookup.setdefault(_col.strip().lower(), _i)

if config.program_config.criterion_column_names:
    # Multi-column program (R2B): "graded" means the Total Score column
    # is filled; the human-review marker lives in Comments/Notes.
    score_col = config.program_config.total_score_column_name
    reasoning_col = config.program_config.notes_column_name
else:
    score_col = config.program_config.score_column_name
    reasoning_col = config.program_config.reasoning_column_name
score_idx = _col_lookup.get(score_col.strip().lower())
reasoning_idx = _col_lookup.get(reasoning_col.strip().lower())
# Was a hardcoded _col_lookup.get("startup name") — silently blank for any
# program/sheet not using that exact header (e.g. Fellowship V2's
# "Participant Name"-style header). Uses the same resolver as no-show
# detection/duplicate-email disambiguation so this display column honors
# an operator-selected Name column override too, and otherwise falls back
# to the same hint-based auto-detect used everywhere else.
name_idx = _resolve_name_column_index(header, config.program_config)
# Label the table column with whatever the actual sheet header says
# (e.g. "Participant Name" for Fellowship V2, "Startup Name" for R2B/
# Alchemist by default) instead of a hardcoded "Startup" — this is
# generic across all 3 programs, driven entirely by the resolved Name
# column, not a per-program assumption.
name_column_label = (
    header[name_idx].strip()
    if name_idx is not None and len(header) > name_idx and header[name_idx].strip()
    else "Name"
)

# A blank-score row with a human_review checkpoint status is still a
# completed grade — the AI used up the verify loop's iteration cap and
# correctly escalated (e.g. pitch deck inaccessible), it just has no
# number. Only a
# "failed" checkpoint status (a real technical error) counts as a mistake;
# a row with no checkpoint entry yet just hasn't been attempted.
duplicate_emails = _find_duplicate_emails(header, rows)
checkpoint_statuses = {}
if os.path.isfile(config.checkpoint_path):
    with open(config.checkpoint_path) as f:
        checkpoint_statuses = {k: v.get("status") for k, v in json.load(f).items()}

table_rows = []
graded_count = 0
mistake_count = 0
for i, row in enumerate(rows):
    name = row[name_idx] if name_idx is not None and len(row) > name_idx else ""
    score = row[score_idx] if score_idx is not None and len(row) > score_idx else ""
    reasoning_raw = (
        row[reasoning_idx] if reasoning_idx is not None and len(row) > reasoning_idx else ""
    )
    human_review = ""
    if reasoning_raw.strip().startswith("[NEEDS HUMAN REVIEW]"):
        human_review = "yes"

    if score.strip() != "":
        graded_count += 1
    else:
        sheet_row_number = config.header_row + i + 1
        row_id = _derive_row_id(
            header, row, sheet_row_number, duplicate_emails, program_config=config.program_config,
        )
        row_key = Checkpoint.key_for(row_id)
        status = checkpoint_statuses.get(row_key)
        if status == "human_review":
            graded_count += 1
        elif status == "failed":
            mistake_count += 1
        # else: no checkpoint entry yet — not attempted, not a mistake.

    table_rows.append({
        name_column_label: name,
        "Score": score,
        "Human review": human_review,
    })

df = pd.DataFrame(table_rows)
total = len(df)

col1, col2, col3 = st.columns(3)
col1.metric("Total rows", total)
col2.metric("Graded", graded_count, help="Has a score, or a completed human-review outcome.")
col3.metric("Mistakes", mistake_count, help="Genuine technical errors — will retry on the next run.")

st.dataframe(df, use_container_width=True, height=500)

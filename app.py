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
import hashlib
import json
import os
import sys

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_REPO_DIR, ".env"))
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from src.config import Config
from src.pipeline import run_batch, _derive_row_id, _find_duplicate_emails
from src.programs import get_program_config
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
    _default_sheet_range = os.environ.get(_program_config.sheet_range_env, "Grading Final")
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
        help="Tab name (e.g. 'Grading Final'), or a full A1 range.",
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

st.title(f"{PROGRAM_LABELS[program]} Grading Agent")

try:
    config = Config.from_env(
        program,
        sheet_id_override=sheet_id_input or None,
        sheet_range_override=sheet_range_input or None,
        header_row_override=int(header_row_input),
        top_label_row_override=int(top_label_row_input),
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
    st.header("Round video indexing")
    round_url = st.text_input(
        "Round video (YouTube URL)",
        value="",
        help="One recording per round. Indexing watches it once, writes "
        "each startup's Segment Start/End to the sheet for your review, "
        "and fills the Video column with this URL. Add the two segment "
        "header columns to the tab once before first use.",
    )
    if st.button(
        "Index round",
        disabled=not round_url.strip(),
        use_container_width=True,
        help="Runs the indexing pass (a few minutes for an hour-long "
        "recording). Grading is a separate step — review the timestamps "
        "first.",
    ):
        from src.round_indexer import run_round_indexing

        with st.spinner("Indexing round — watching the recording…"):
            try:
                summary = run_round_indexing(config, round_url.strip())
            except Exception as exc:  # noqa: BLE001 — operator-facing surface
                st.error(f"Indexing failed: {exc}")
            else:
                st.success(
                    f"Indexed {summary['indexed']} startups "
                    f"({summary['needs_check']} marked NEEDS CHECK, "
                    f"{summary.get('no_pitch', 0)} with no gradeable pitch). "
                    "Review the Segment columns in the sheet, then run grading."
                )
                st.dataframe([
                    {
                        "Startup": s.startup_name,
                        "Start": s.start,
                        "End": s.end,
                        "Status": s.pitch_status,
                        "Verified": "yes" if s.verified else "NEEDS CHECK",
                    }
                    for s in summary["segments"]
                ])

if run_clicked:
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    # The progress bar/text below only update once a ROW FINISHES (via
    # on_progress, called from run_batch's per-future completion handler)
    # — for a single row that can take 1-3+ minutes (video download,
    # verify loop, multi-sample Head scoring), that's several minutes of
    # a blank progress area with zero feedback that anything is happening
    # at all, easily read as "stuck". Show something immediately instead.
    status_text.text("Starting — resolving sheet rows and launching grading…")
    ok_count = 0
    fail_count = 0
    skip_count = 0

    def _on_progress(done, total, row_id, ok):
        global ok_count, fail_count, skip_count
        # ok=None means deliberately skipped (no-show, or already graded) —
        # neither a grade nor a failure. Without the separate bucket, a
        # batch with 11 no-shows displayed "13 failed" when only 2 rows
        # genuinely failed (confirmed live).
        if ok is None:
            skip_count += 1
        elif ok:
            ok_count += 1
        else:
            fail_count += 1
        progress_bar.progress(done / total if total else 1.0)
        status_text.text(
            f"{done}/{total} processed — {ok_count} graded, "
            f"{skip_count} skipped (no-show / already graded), "
            f"{fail_count} failed"
        )

    result = run_batch(
        config, force=force, limit=int(limit), on_progress=_on_progress
    )
    progress_bar.empty()
    status_text.empty()
    st.success(f"Graded: {len(result['graded'])} | Errors: {len(result['errors'])}")
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
name_idx = _col_lookup.get("startup name")

# A blank-score row with a human_review checkpoint status is still a
# completed grade — the AI finished its 3 review attempts and correctly
# escalated (e.g. pitch deck inaccessible), it just has no number. Only a
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
        row_id = _derive_row_id(header, row, sheet_row_number, duplicate_emails)
        row_key = hashlib.sha256(row_id.encode("utf-8")).hexdigest()
        status = checkpoint_statuses.get(row_key)
        if status == "human_review":
            graded_count += 1
        elif status == "failed":
            mistake_count += 1
        # else: no checkpoint entry yet — not attempted, not a mistake.

    table_rows.append({
        "Startup": name,
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

"""
Streamlit dashboard for the Alchemist grading agent.

Run from the repo root:
    streamlit run app.py

Sidebar controls let the user pick how many rows to grade this run, whether
to re-grade already-graded rows or only ungraded ones, and reset the
checkpoint entirely. The main area shows current sheet grading state and
the results of the most recent run.

Alchemist only for now — one dedicated app per program is the plan, this is
the first (and currently only) one. Local use only, not deployed.
"""
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
from src.pipeline import run_batch
from src.google_clients import get_sheets_service, read_sheet_rows

PROGRAM = "alchemist"

st.set_page_config(page_title="Alchemist Grading Agent", layout="wide")
st.title("Alchemist Grading Agent")

try:
    config = Config.from_env(PROGRAM)
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
            done_count = len(json.load(f))
        st.caption(f"{done_count} row(s) marked done.")
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

if run_clicked:
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    ok_count = 0
    fail_count = 0

    def _on_progress(done, total, row_id, ok):
        global ok_count, fail_count
        if ok:
            ok_count += 1
        else:
            fail_count += 1
        progress_bar.progress(done / total if total else 1.0)
        status_text.text(
            f"{done}/{total} processed — {ok_count} graded, {fail_count} failed"
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
sheets_service = get_sheets_service(config.service_account_path)
header, rows = read_sheet_rows(
    sheets_service, config.sheet_id, config.sheet_range, config.header_row
)

score_col = config.program_config.score_column_name
reasoning_col = config.program_config.reasoning_column_name
score_idx = header.index(score_col) if score_col in header else None
reasoning_idx = header.index(reasoning_col) if reasoning_col in header else None
name_idx = header.index("Startup name") if "Startup name" in header else None

table_rows = []
for row in rows:
    name = row[name_idx] if name_idx is not None and len(row) > name_idx else ""
    score = row[score_idx] if score_idx is not None and len(row) > score_idx else ""
    reasoning_raw = (
        row[reasoning_idx] if reasoning_idx is not None and len(row) > reasoning_idx else ""
    )
    human_review = ""
    if reasoning_raw.strip().startswith("[NEEDS HUMAN REVIEW]"):
        human_review = "yes"
    table_rows.append({
        "Startup": name,
        "Score": score,
        "Human review": human_review,
    })

df = pd.DataFrame(table_rows)
graded_mask = df["Score"].astype(str).str.strip() != ""
total = len(df)
graded_count = int(graded_mask.sum())

col1, col2, col3 = st.columns(3)
col1.metric("Total rows", total)
col2.metric("Graded", graded_count)
col3.metric("Ungraded", total - graded_count)

st.dataframe(df, use_container_width=True, height=500)

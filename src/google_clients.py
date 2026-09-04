"""
Google Sheets access via a single service account.
Service account (not OAuth) chosen because a 100+ row batch run must be
non-interactive — no browser consent prompt mid-run. The account's email
needs Editor access on the Sheet.
"""

import re
from urllib.parse import urlparse

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Hosts whose links may be handed to the Drive API. The ID patterns below
# match on any host, so without this list https://evil.tld/d/<ID> would
# make the service account fetch a Drive file of the submitter's choosing
# (a confused deputy: the file need only be readable by the account, not
# by the applicant).
_DRIVE_ALLOWED_HOSTS = frozenset(
    {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}
)

# Matches both Drive URL shapes seen in form responses:
#   .../file/d/<ID>/view   and   .../open?id=<ID>
_DRIVE_ID_PATTERNS = [
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
]

# A Drive FOLDER link (drive.google.com/drive/folders/<ID>) matches neither
# pattern above — extract_drive_file_id correctly returns None for it — but
# a caller that then falls back to treating the raw URL as a downloadable
# file (e.g. an HTTPS GET expecting a PDF) ends up trying to fetch a Drive
# folder-listing page, which fails slowly and non-deterministically instead
# of with a clear, immediate error. Detecting this shape explicitly lets
# callers fail fast with an honest "this is a folder, not a file" message.
_DRIVE_FOLDER_PATTERN = re.compile(r"drive\.google\.com/drive/folders/")


def is_drive_folder_url(url: str) -> bool:
    return bool(url) and bool(_DRIVE_FOLDER_PATTERN.search(url))


def _range_start_row(sheet_range: str) -> int | None:
    """Row the range's start anchor refers to, or None when the range is
    a bare tab name or has no row anchor (both start at row 1).

    The anchor is only read when the value actually is A1 notation — it
    has a "!" or a ":". A bare "Round2" is a tab name, not column A row
    2, and rejecting it would break sheets whose tabs are named that way.
    """
    if "!" not in sheet_range and ":" not in sheet_range:
        return None
    start = sheet_range.split("!")[-1].split(":")[0]
    match = re.fullmatch(r"[A-Za-z]+(\d+)?", start)
    return int(match.group(1)) if match and match.group(1) else None


def _credentials(service_account_path: str):
    return service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )


def get_sheets_service(service_account_path: str):
    creds = _credentials(service_account_path)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def extract_drive_file_id(url: str) -> str | None:
    """Pull the file ID out of a Drive share link.

    Form respondents paste whatever URL Drive's share dialog gives them —
    we can't assume one fixed shape, so try each known pattern in order.
    """
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if host not in _DRIVE_ALLOWED_HOSTS:
        return None
    for pattern in _DRIVE_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def fetch_sheet_row(sheets_service, sheet_id: str, sheet_name: str, row_number: int) -> list:
    """Fetch one raw row by 1-indexed row number — used to read the merged
    top-label row (e.g. "AI" / "AI Reasoning" group headers) separately
    from the per-column header row read_sheet_rows uses, since on this
    project's real sheet those two kinds of header live on different rows.
    """
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{sheet_name}!{row_number}:{row_number}")
        .execute(num_retries=5)
    )
    values = result.get("values", [])
    return values[0] if values else []


def read_sheet_rows(sheets_service, sheet_id: str, sheet_range: str, header_row: int = 1):
    """Return (header, rows). Row N in `rows` corresponds to sheet row
    N + header_row + 1 (sheet rows are 1-indexed) — callers need that
    offset to write results back to the correct row.

    header_row defaults to 1 (the common case: first row is the header).
    Some sheets — e.g. this project's default grading tab — have a merged
    top label row (group headers like reviewer names) ABOVE the real
    per-column header row; set header_row=2 for those, so column-name
    lookups land on the actual question text, not a merged group label.
    """
    start_row = _range_start_row(sheet_range)
    if start_row not in (None, 1):
        raise ValueError(
            f"Range '{sheet_range}' starts at row {start_row}; row-anchored "
            "ranges are not supported (row math assumes the range starts "
            "at row 1, so grades would be written to the wrong "
            "applicants' rows). Use the tab name, or a range starting at "
            "row 1."
        )
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=sheet_range)
        .execute(num_retries=5)
    )
    values = result.get("values", [])
    if len(values) < header_row:
        return [], []
    header, rows = values[header_row - 1], values[header_row:]
    # Sheets API drops trailing empty cells per row — pad so every row
    # lines up positionally with `header`, or later index lookups
    # silently grab the wrong column for short rows.
    width = len(header)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return header, rows


def resolve_output_columns(
    header: list,
    score_column_name: str = "AI",
    reasoning_column_name: str = "AI Reasoning",
) -> dict:
    """Find the 0-indexed score/reasoning columns to write to.

    Deliberately does NOT auto-append missing columns (unlike an earlier
    version of this function) — on a real production sheet with a
    pre-existing structure (merged header rows, formulas referencing
    specific column letters, named per-reviewer columns), silently
    inserting new columns risks shifting everything and breaking those
    formulas. If the columns aren't where expected, fail loudly instead.

    The caller supplies the merged top-label row where these output names live,
    so deployment-specific column letters are unnecessary.
    """
    col_map = {}
    if score_column_name in header:
        col_map["score"] = header.index(score_column_name)
    else:
        raise RuntimeError(f"Score column '{score_column_name}' not found.")

    if reasoning_column_name in header:
        col_map["reasoning"] = header.index(reasoning_column_name)
    else:
        raise RuntimeError(f"Reasoning column '{reasoning_column_name}' not found.")
    return col_map


def resolve_multi_output_columns(
    header: list,
    criterion_column_names: dict,
    total_score_column_name: str,
    notes_column_name: str,
) -> dict:
    """Find the 0-indexed columns for R2B's multi-column output: one per
    rubric criterion, plus Total Score and Comments/Notes.

    Same fail-loud philosophy as resolve_output_columns: a missing column
    means the sheet doesn't match what this program expects, and silently
    skipping it would mean scores go nowhere without anyone noticing.

    Matches on stripped text — confirmed live: a real sheet header had
    "Total Score " (trailing space) where the configured name was "Total
    Score", which would otherwise fail loudly on an incidental formatting
    difference rather than a genuine missing-column problem.
    """
    stripped_index = {}
    for i, col in enumerate(header):
        stripped_index.setdefault(col.strip(), i)

    def _find(name: str, description: str) -> int:
        idx = stripped_index.get(name.strip())
        if idx is None:
            raise RuntimeError(f"{description} column '{name}' not found.")
        return idx

    criteria_cols = {
        criterion: _find(column_header, f"Criterion (for '{criterion}')")
        for criterion, column_header in criterion_column_names.items()
    }

    return {
        "criteria": criteria_cols,
        "total_score": _find(total_score_column_name, "Total score"),
        "notes": _find(notes_column_name, "Notes"),
    }


def write_row_result(
    sheets_service,
    sheet_id: str,
    sheet_name: str,
    sheet_row_number: int,
    col_map: dict,
    score,
    reasoning: str,
):
    """Write the score + reasoning columns for one row.

    Single batched range-update when the two columns are adjacent (the
    common case); otherwise two separate cell writes. No third
    "human review flag" column — that signal is folded into the
    reasoning text itself by the caller (pipeline.py), since this
    project's real sheet has no dedicated column for it and adding one
    would mean mutating a structure that already has formulas
    referencing specific columns.
    """
    score_idx, reasoning_idx = col_map["score"], col_map["reasoning"]
    if abs(score_idx - reasoning_idx) == 1:
        start_idx, end_idx = sorted([score_idx, reasoning_idx])
        start_col, end_col = _col_letter(start_idx + 1), _col_letter(end_idx + 1)
        ordered_values = [None, None]
        ordered_values[score_idx - start_idx] = str(score) if score is not None else ""
        ordered_values[reasoning_idx - start_idx] = reasoning
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{start_col}{sheet_row_number}:{end_col}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [ordered_values]},
        ).execute(num_retries=5)
        return
    for idx, value in ((score_idx, score), (reasoning_idx, reasoning)):
        letter = _col_letter(idx + 1)
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{letter}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute(num_retries=5)


def write_multi_row_result(
    sheets_service,
    sheet_id: str,
    sheet_name: str,
    sheet_row_number: int,
    col_map: dict,
    criterion_scores: dict,
    total_score,
    notes: str,
):
    """Write R2B's multi-column output: one cell per rubric criterion, plus
    Total Score and Comments/Notes.

    Writes each cell individually rather than batching a range update —
    unlike write_row_result's adjacent-pair case, these 8 target columns
    aren't guaranteed to be contiguous with each other if the sheet is
    ever restructured, and per-row grading already involves several LLM
    calls that dominate cost/time, so 8 small Sheets API calls here is
    negligible.
    """
    for criterion, col_idx in col_map["criteria"].items():
        value = criterion_scores.get(criterion, "")
        letter = _col_letter(col_idx + 1)
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{letter}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute(num_retries=5)

    total_letter = _col_letter(col_map["total_score"] + 1)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{total_letter}{sheet_row_number}",
        valueInputOption="RAW",
        body={"values": [[total_score if total_score is not None else ""]]},
    ).execute(num_retries=5)

    notes_letter = _col_letter(col_map["notes"] + 1)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{notes_letter}{sheet_row_number}",
        valueInputOption="RAW",
        body={"values": [[notes]]},
    ).execute(num_retries=5)


def write_reasoning_only(
    sheets_service,
    sheet_id: str,
    sheet_name: str,
    sheet_row_number: int,
    col_map: dict,
    reasoning: str,
):
    """Write only the reasoning column, leaving the score cell as it is.

    Used for human-review escalations so a row that already carries scores
    keeps them: write_row_result blanks the score cell for a score of
    None, which erased a real grade whenever an already-graded row was
    re-graded (or force-graded) and then escalated.
    """
    letter = _col_letter(col_map["reasoning"] + 1)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{letter}{sheet_row_number}",
        valueInputOption="RAW",
        body={"values": [[reasoning]]},
    ).execute(num_retries=5)


def write_multi_notes_only(
    sheets_service,
    sheet_id: str,
    sheet_name: str,
    sheet_row_number: int,
    col_map: dict,
    notes: str,
):
    """Multi-column equivalent of write_reasoning_only: only the Comments/
    Notes cell is written, so an escalation leaves every criterion score
    and the Total Score exactly as the previous run left them.
    """
    letter = _col_letter(col_map["notes"] + 1)
    sheets_service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{sheet_name}!{letter}{sheet_row_number}",
        valueInputOption="RAW",
        body={"values": [[notes]]},
    ).execute(num_retries=5)


def _col_letter(col_1_indexed: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA. Sheets API ranges use letters, not indices."""
    letters = ""
    n = col_1_indexed
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

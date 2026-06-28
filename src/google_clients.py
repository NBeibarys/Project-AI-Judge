"""
Google Sheets access via a single service account.
Service account (not OAuth) chosen because a 100+ row batch run must be
non-interactive — no browser consent prompt mid-run. The account's email
needs Editor access on the Sheet.
"""
import re
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

# Matches both Drive URL shapes seen in form responses:
#   .../file/d/<ID>/view   and   .../open?id=<ID>
_DRIVE_ID_PATTERNS = [
    re.compile(r"/d/([a-zA-Z0-9_-]{10,})"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]{10,})"),
]


def _credentials(service_account_path: str):
    return service_account.Credentials.from_service_account_file(
        service_account_path, scopes=SCOPES
    )


def get_sheets_service(service_account_path: str):
    creds = _credentials(service_account_path)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def extract_drive_file_id(url: str) -> Optional[str]:
    """Pull the file ID out of a Drive share link.

    Form respondents paste whatever URL Drive's share dialog gives them —
    we can't assume one fixed shape, so try each known pattern in order.
    """
    if not url:
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
        .execute()
    )
    values = result.get("values", [])
    return values[0] if values else []


def read_sheet_rows(sheets_service, sheet_id: str, sheet_range: str, header_row: int = 1):
    """Return (header, rows). Row N in `rows` corresponds to sheet row
    N + header_row + 1 (sheet rows are 1-indexed) — callers need that
    offset to write results back to the correct row.

    header_row defaults to 1 (the common case: first row is the header).
    Some sheets — e.g. this project's "Grading Final" — have a merged
    top label row (group headers like reviewer names) ABOVE the real
    per-column header row; set header_row=2 for those, so column-name
    lookups land on the actual question text, not a merged group label.
    """
    result = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=sheet_range)
        .execute()
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
        ordered_values[score_idx - start_idx] = score
        ordered_values[reasoning_idx - start_idx] = reasoning
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{start_col}{sheet_row_number}:{end_col}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [ordered_values]},
        ).execute()
        return
    for idx, value in ((score_idx, score), (reasoning_idx, reasoning)):
        letter = _col_letter(idx + 1)
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{letter}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute()


def _col_letter(col_1_indexed: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA. Sheets API ranges use letters, not indices."""
    letters = ""
    n = col_1_indexed
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

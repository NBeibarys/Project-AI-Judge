"""Round-video indexing system — SEPARATE from the grading agents.

Two single-turn Gemini calls (direct google-genai client, not ADK — there
is no loop/session state to manage here):
  1. Segmenter: full round recording (LOW media resolution; the boundary
     signal is the emcee's spoken announcements) + the sheet's startup
     names -> structured per-startup timestamps.
  2. Segment verifier: per proposed segment, sees ONLY that clipped span
     (server-side video_metadata offsets) and names the startup pitching —
     the same never-trust-one-generation philosophy as the grading verify
     loop, applied to indexing.

The grading multi-agent system is not touched by anything in this module.
Spec: docs/superpowers/specs/2026-07-14-round-video-auto-indexing-design.md
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .google_clients import _col_letter, get_sheets_service, read_sheet_rows
from .pipeline import _is_no_show
from .round_segments import (
    SEGMENT_END_COLUMN,
    SEGMENT_START_COLUMN,
    parse_timestamp,
    seconds_to_timestamp,
    validate_segments,
)

# One retry after a short pause for the big segmenter call — a single
# transient failure on an hour-long-video call shouldn't force the operator
# to re-click, but we don't loop beyond that (fail loudly instead).
SEGMENTER_ATTEMPTS = 2

# Deliberately mirrors the grading pipeline's determinism choices.
INDEXER_TEMPERATURE = 0.0
INDEXER_SEED = 7524

# Refinement/verification calls are independent per segment (each hits its
# own clipped span) and carry no payload — the video is fetched server-side
# from YouTube, so the inline-bytes concurrency hazard that bit the grading
# Head's parallelization does not exist here. Bounded to stay well inside
# the Vertex request-rate budget alongside any concurrently-running grading.
INDEXER_CONCURRENCY = 3


class RoundSegment(BaseModel):
    startup_name: str = Field(min_length=1)
    start: str = Field(min_length=3, description="Segment start, m:ss or h:mm:ss")
    end: str = Field(min_length=3, description="Segment end, m:ss or h:mm:ss")
    # What actually happened at this startup's announced slot — a real
    # distinction observed at live competitions: the host announces a
    # startup and (a) they pitch, (b) nobody responds, or (c) they respond
    # but can't continue (technical trouble, defer). Only "pitched" slots
    # are gradeable; the other two are surfaced to the operator instead.
    pitch_status: Literal["pitched", "announced_no_response", "aborted"] = "pitched"
    verified: bool = True


class RoundSegmentList(BaseModel):
    segments: list[RoundSegment]


SEGMENTER_PROMPT = """
You are indexing a startup-competition round recording. The video contains
several startup pitches back-to-back. An emcee announces each startup by
name before its pitch, and each pitch is followed by a Q&A with judges.

Find the segment for EVERY startup listed below — no more, no fewer. A
startup's segment starts when the emcee announces it (include the
announcement) and ends when its Q&A finishes, just before the next
announcement (or when the recording's closing remarks begin, for the last
startup).

When a startup is announced, one of three things happens — set
pitch_status accordingly for each startup:
- "pitched": the founders respond and deliver their pitch (the normal
  case). The segment spans announcement -> end of their Q&A. If they
  answer but ask/are told to pitch later, or an early attempt is
  interrupted, and the startup returns LATER in the recording to pitch
  properly, use ONLY the COMPLETED later pitch's span.
- "announced_no_response": the host announces the startup but nobody
  responds or appears, and the host moves on. The segment is just that
  short announcement moment.
- "aborted": the founders respond but cannot continue (technical
  problems, they defer, something goes wrong) and never complete a pitch
  anywhere in this recording. The segment spans whatever actually
  happened at their slot.

Startups to locate (use these EXACT names in your answer):
{startup_names}

Answer with one segment per startup, timestamps as m:ss or h:mm:ss.
""".strip()


VERIFIER_PROMPT = """
This is a clip from a startup-competition recording. Which single startup
is being pitched in this clip? Answer with JSON: {"startup_name": "<name>"}.
If several startups appear, name the one whose pitch takes up most of the
clip. Use the startup's name as announced/shown, as closely as possible.
""".strip()


def _client() -> genai.Client:
    # Env-driven: with GOOGLE_GENAI_USE_VERTEXAI/GOOGLE_CLOUD_PROJECT/
    # GOOGLE_CLOUD_LOCATION set (this deployment), Client() talks to
    # Vertex — same backend the grading pipeline uses.
    return genai.Client()


def _youtube_part(url: str, start_s: int | None = None, end_s: int | None = None) -> types.Part:
    kwargs = {}
    if start_s is not None and end_s is not None:
        kwargs["video_metadata"] = types.VideoMetadata(
            start_offset=f"{start_s}s", end_offset=f"{end_s}s",
        )
    return types.Part(
        file_data=types.FileData(file_uri=url, mime_type="video/mp4"),
        **kwargs,
    )


def _call_segmenter(youtube_url: str, startup_names: list[str], model: str) -> RoundSegmentList:
    prompt = SEGMENTER_PROMPT.format(startup_names="\n".join(f"- {n}" for n in startup_names))
    last_exc: Exception | None = None
    for attempt in range(SEGMENTER_ATTEMPTS):
        try:
            # The client MUST be held in a local for the call's duration:
            # `_client().models.generate_content(...)` frees the Client
            # temporary right after `.models` is read (CPython refcount),
            # which closes its underlying HTTP session — every request then
            # fails with "Cannot send a request, as the client has been
            # closed" (confirmed live on the first real app run).
            client = _client()
            response = client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=[
                    _youtube_part(youtube_url),
                    types.Part(text=prompt),
                ])],
                config=types.GenerateContentConfig(
                    temperature=INDEXER_TEMPERATURE,
                    seed=INDEXER_SEED,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                    response_mime_type="application/json",
                    response_schema=RoundSegmentList,
                ),
            )
            return RoundSegmentList.model_validate_json(response.text)
        except Exception as exc:  # noqa: BLE001 — surface after retry budget
            last_exc = exc
            if attempt + 1 < SEGMENTER_ATTEMPTS:
                time.sleep(5)
    raise RuntimeError(f"Segmenter failed after {SEGMENTER_ATTEMPTS} attempts") from last_exc


def _call_segment_verifier(youtube_url: str, start_s: int, end_s: int, model: str) -> str:
    """Return the startup name the verifier sees pitching in this span."""
    # Held in a local, same as _call_segmenter — see the comment there.
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[
            _youtube_part(youtube_url, start_s, end_s),
            types.Part(text=VERIFIER_PROMPT),
        ])],
        config=types.GenerateContentConfig(
            temperature=INDEXER_TEMPERATURE,
            seed=INDEXER_SEED,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            response_mime_type="application/json",
        ),
    )
    return str(json.loads(response.text).get("startup_name", "")).strip()


REFINE_WINDOW_SECONDS = 90

REFINE_PROMPT = """
In this clip from a startup-competition recording, find the host
announcement that starts the FULL gradeable pitch for startup
"{startup_name}". If "{startup_name}" was announced earlier but the
founders did not pitch then, ignore that earlier aborted/no-response
announcement and use the later announcement where their actual pitch and
Q&A begin.

At what timestamp does the gradeable-pitch announcement begin? Answer with
JSON: {{"announce_time": "<m:ss or h:mm:ss>"}}. If the gradeable-pitch
announcement for "{startup_name}" does not occur anywhere in this clip,
answer {{"announce_time": null}}.
""".strip()


def _refine_start(youtube_url: str, coarse_start_s: int, startup_name: str, model: str) -> int | None:
    """Pin a segment's exact start with a focused window around the coarse one.

    Coarse-to-fine: the hour-long segmenter pass localizes boundaries only
    to within a minute or two (confirmed live — a startup's assigned start
    landed 105s inside the previous startup's Q&A), while a focused ±90s
    clip pins the announcement to the second. Clipped calls report
    timestamps in the ORIGINAL video's timeline (confirmed live), so the
    answer is validated by range — anything outside the shown window (or
    unparseable) returns None and the coarse value stays.
    """
    window_start = max(0, coarse_start_s - REFINE_WINDOW_SECONDS)
    window_end = coarse_start_s + REFINE_WINDOW_SECONDS
    client = _client()
    try:
        response = client.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[
                _youtube_part(youtube_url, window_start, window_end),
                types.Part(text=REFINE_PROMPT.format(startup_name=startup_name)),
            ])],
            config=types.GenerateContentConfig(
                temperature=INDEXER_TEMPERATURE,
                seed=INDEXER_SEED,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                response_mime_type="application/json",
            ),
        )
        raw = json.loads(response.text).get("announce_time")
        if not raw:
            return None
        refined = parse_timestamp(str(raw))
    except Exception:  # noqa: BLE001 — refinement is best-effort, coarse stays
        return None
    if not window_start <= refined <= window_end:
        return None
    return refined


def _names_match(expected: str, seen: str) -> bool:
    """Forgiving comparison: emcee pronunciation/casing/punctuation drift is
    normal; a genuinely different startup won't survive containment checks."""
    a = "".join(ch for ch in expected.lower() if ch.isalnum())
    b = "".join(ch for ch in seen.lower() if ch.isalnum())
    return bool(a) and bool(b) and (a in b or b in a)


def index_round(youtube_url: str, startup_names: list[str], model: str) -> list[RoundSegment]:
    """Segment one round recording and verify every proposed segment.

    Returns segments in video order; .verified=False marks spans where the
    verifier saw a different startup than proposed (written to the sheet
    with a NEEDS CHECK marker — never silently trusted).
    Raises SegmentValidationError if the segmenter's output is structurally
    wrong (missing/extra startups, overlaps, implausible lengths).
    """
    proposal = _call_segmenter(youtube_url, startup_names, model)
    # The segmenter sometimes returns segments in SHEET order rather than
    # time order (confirmed live) — sort by start; names carry identity,
    # list order is only presentation.
    segments = sorted(proposal.segments, key=lambda s: parse_timestamp(s.start))
    triples = [
        (seg.startup_name, parse_timestamp(seg.start), parse_timestamp(seg.end))
        for seg in segments
    ]
    short_ok = frozenset(
        seg.startup_name for seg in segments if seg.pitch_status != "pitched"
    )
    validate_segments(triples, startup_names, short_ok)

    # Coarse-to-fine: pin each PITCHED segment's start with a focused ±90s
    # window call, then derive each end from the NEXT segment's refined
    # start — the recording is contiguous (pitch -> Q&A -> next
    # announcement), so ends carry no independent signal of their own. The
    # last segment keeps its coarse end. Refinement is best-effort per
    # boundary: a failed/out-of-window answer keeps the coarse value.
    # Refinement and verification each run in a bounded thread pool — the
    # per-segment calls are fully independent and payload-free (YouTube is
    # fetched server-side), so parallelizing them is safe; see
    # INDEXER_CONCURRENCY.
    def _refined_start_for(i: int) -> int:
        name, start_s, _ = triples[i]
        if segments[i].pitch_status != "pitched":
            return start_s
        refined = _refine_start(youtube_url, start_s, name, model)
        return refined if refined is not None else start_s

    with ThreadPoolExecutor(max_workers=INDEXER_CONCURRENCY) as pool:
        starts = list(pool.map(_refined_start_for, range(len(triples))))
    refined_triples = []
    for i, (name, _, end_s) in enumerate(triples):
        end = (starts[i + 1] - 1) if i + 1 < len(triples) else end_s
        refined_triples.append((name, starts[i], end))
    validate_segments(refined_triples, startup_names, short_ok)

    # Verify pitched segments only: a no-response/aborted slot's clip is
    # mostly the announcement itself — flagging it NEEDS CHECK on top of
    # its NO PITCH marker would be noise; the operator reviews it anyway.
    def _verified_for(i: int) -> bool:
        name, start_s, end_s = refined_triples[i]
        if segments[i].pitch_status != "pitched":
            return True
        seen = _call_segment_verifier(youtube_url, start_s, end_s, model)
        return _names_match(name, seen)

    with ThreadPoolExecutor(max_workers=INDEXER_CONCURRENCY) as pool:
        verified_flags = list(pool.map(_verified_for, range(len(refined_triples))))

    results: list[RoundSegment] = []
    for i, (name, start_s, end_s) in enumerate(refined_triples):
        results.append(RoundSegment(
            startup_name=name,
            start=seconds_to_timestamp(start_s),
            end=seconds_to_timestamp(end_s),
            pitch_status=segments[i].pitch_status,
            verified=verified_flags[i],
        ))
    return results


def _write_row_cells(sheets_service, sheet_id: str, sheet_name: str,
                     sheet_row_number: int, values_by_header: dict,
                     col_lookup: dict) -> None:
    """Write named cells on one row, one update per cell (same per-cell
    style as write_multi_row_result — these columns aren't contiguous)."""
    for header_name, value in values_by_header.items():
        idx = col_lookup[header_name.strip().lower()]
        letter = _col_letter(idx + 1)
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{sheet_name}!{letter}{sheet_row_number}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ).execute(num_retries=5)


def run_round_indexing(config, youtube_url: str) -> dict:
    """Index one round recording and write results to the sheet.

    Reads the round tab via `config` (same sheet geometry the grading run
    uses), excludes no-show rows, runs index_round, and writes: the round
    URL into each indexed row's Video cell, and start/end into the two
    segment columns (prefixed 'NEEDS CHECK: ' when the verifier saw a
    different startup). Returns a summary dict for the app UI:
    {"indexed": n, "needs_check": n, "segments": [RoundSegment, ...]}.
    Fails loudly if the operator hasn't added the two segment columns or a
    Startup Name/Video column is missing.
    """
    sheets_service = get_sheets_service(config.service_account_path)
    header, rows = read_sheet_rows(
        sheets_service, config.sheet_id, config.sheet_range, config.header_row,
    )
    col_lookup = {}
    for i, col in enumerate(header):
        col_lookup.setdefault(col.strip().lower(), i)
    for required in ("startup name", "video",
                     SEGMENT_START_COLUMN.lower(), SEGMENT_END_COLUMN.lower()):
        if required not in col_lookup:
            raise RuntimeError(
                f"Column {required!r} not found on the tab — add the "
                f"'{SEGMENT_START_COLUMN}' and '{SEGMENT_END_COLUMN}' header "
                "cells once per round tab (see design doc)."
            )

    name_idx = col_lookup["startup name"]
    candidates: list[tuple[int, str]] = []  # (sheet_row_number, name)
    for i, row in enumerate(rows):
        name = (row[name_idx] if len(row) > name_idx else "").strip()
        if name and not _is_no_show(name):
            candidates.append((config.header_row + i + 1, name))

    segments = index_round(youtube_url, [n for _, n in candidates], config.analyzer_model)
    by_name = {s.startup_name: s for s in segments}

    sheet_name = config.sheet_range.split("!")[0].strip("'")
    needs_check = 0
    no_pitch = 0
    for sheet_row_number, name in candidates:
        seg = by_name[name]
        if seg.pitch_status != "pitched":
            # Announced but no gradeable pitch happened (nobody responded,
            # or the founders couldn't continue). Clear any stale Video URL
            # and write an explicit non-timestamp marker; otherwise grading
            # could fall back to feeding the FULL round recording to the
            # analyst for this row. The operator decides what to do with the
            # row (e.g. mark no-show).
            no_pitch += 1
            label = (
                "no response" if seg.pitch_status == "announced_no_response"
                else "aborted"
            )
            marker = f"NO PITCH ({label})"
            _write_row_cells(
                sheets_service, config.sheet_id, sheet_name, sheet_row_number,
                {
                    "Video": "",
                    SEGMENT_START_COLUMN: marker,
                    SEGMENT_END_COLUMN: marker,
                },
                col_lookup,
            )
            continue
        prefix = "" if seg.verified else "NEEDS CHECK: "
        needs_check += 0 if seg.verified else 1
        _write_row_cells(
            sheets_service, config.sheet_id, sheet_name, sheet_row_number,
            {
                "Video": youtube_url,
                SEGMENT_START_COLUMN: f"{prefix}{seg.start}",
                SEGMENT_END_COLUMN: f"{prefix}{seg.end}",
            },
            col_lookup,
        )
    return {
        "indexed": len(segments),
        "needs_check": needs_check,
        "no_pitch": no_pitch,
        "segments": segments,
    }

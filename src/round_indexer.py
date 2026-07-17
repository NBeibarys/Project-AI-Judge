"""Round-video indexing system — SEPARATE from the grading agents.

Three-role Gemini workflow (direct google-genai calls, not ADK sessions):
  1. Analyst (HIGH thinking): watches the full round and proposes every row.
  2. Verifier (HIGH thinking): checks each row in a focused clip. Rejected
     rows return to the Analyst for full-video revision, up to three trials.
  3. Writer (LOW thinking): receives only the fully approved segment list and
     emits the final structured payload once. Python then validates it and
     sends the whole round to Sheets in one batch request.

The grading multi-agent system is not touched by anything in this module.
Spec: docs/superpowers/specs/2026-07-14-round-video-auto-indexing-design.md
"""
import json
import time
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
    validate_segments,
)
from .video_urls import canonicalize_youtube_url

# One retry after a short pause for the big segmenter call — a single
# transient failure on an hour-long-video call shouldn't force the operator
# to re-click, but we don't loop beyond that (fail loudly instead).
SEGMENTER_ATTEMPTS = 2

# Deliberately mirrors the grading pipeline's determinism choices.
INDEXER_TEMPERATURE = 0.0
INDEXER_SEED = 7524

# Strictly one model invocation at a time: one full-video Analyst and one
# sequential per-row Verifier. This avoids duplicate full-video revisions.
INDEXER_CONCURRENCY = 1
INDEXER_REVIEW_ATTEMPTS = 3
VERIFIER_CONTEXT_SECONDS = 90


def _indexer_generation_config(
    thinking_level: types.ThinkingLevel,
    response_schema: type[BaseModel] | None = None,
) -> types.GenerateContentConfig:
    """Keep visual cost low while making boundary reasoning deliberate."""
    return types.GenerateContentConfig(
        temperature=INDEXER_TEMPERATURE,
        seed=INDEXER_SEED,
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        response_mime_type="application/json",
        response_schema=response_schema,
    )


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


class SegmentVerification(BaseModel):
    startup_name_seen: str = Field(min_length=1)
    pitch_status_seen: Literal["pitched", "announced_no_response", "aborted"]
    start_precise: bool
    end_precise: bool
    approved: bool
    feedback: str = ""


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
You are the per-startup verifier for a startup-competition round index.
The expected startup is "{startup_name}". The Analyst proposed:
- start: {start}
- end: {end}
- pitch_status: {pitch_status}

The clip includes context before and after the proposed span. Independently
check all four requirements:
1. startup_name_seen is the startup actually occupying the proposed span.
2. pitch_status_seen correctly distinguishes a completed pitch, an announced
   startup with no response, or an attempt that aborts and never resumes.
3. start_precise is true only when the proposed start is the host announcement
   beginning the FULL gradeable pitch. Ignore an earlier no-response, deferred,
   or interrupted attempt when a completed pitch happens later.
4. end_precise is true only when the proposed end includes the full Q&A and
   stops immediately before the next startup announcement, or after closing
   remarks begin for the final startup. For no-response/aborted slots, verify
   the short event itself is fully captured.

Set approved=true only if identity, status, start, and end are all correct.
Otherwise set approved=false and give exact correction instructions for the
Analyst in feedback. Do not approve an approximately or evenly divided span.
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
                config=_indexer_generation_config(
                    types.ThinkingLevel.HIGH,
                    RoundSegmentList,
                ),
            )
            return RoundSegmentList.model_validate_json(response.text)
        except Exception as exc:  # noqa: BLE001 — surface after retry budget
            last_exc = exc
            if attempt + 1 < SEGMENTER_ATTEMPTS:
                time.sleep(5)
    raise RuntimeError(f"Segmenter failed after {SEGMENTER_ATTEMPTS} attempts") from last_exc


def _call_segment_verifier(
    youtube_url: str,
    segment: RoundSegment,
    model: str,
) -> SegmentVerification:
    """Independently verify one row's identity, status, and both boundaries."""
    start_s = parse_timestamp(segment.start)
    end_s = parse_timestamp(segment.end)
    clip_start = max(0, start_s - VERIFIER_CONTEXT_SECONDS)
    clip_end = end_s + VERIFIER_CONTEXT_SECONDS
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[
            _youtube_part(youtube_url, clip_start, clip_end),
            types.Part(text=VERIFIER_PROMPT.format(
                startup_name=segment.startup_name,
                start=segment.start,
                end=segment.end,
                pitch_status=segment.pitch_status,
            )),
        ])],
        config=_indexer_generation_config(
            types.ThinkingLevel.HIGH,
            SegmentVerification,
        ),
    )
    return SegmentVerification.model_validate_json(response.text)


ANALYST_REVISION_PROMPT = """
You are revising a startup-competition round index after the per-row Verifier
rejected one or more rows. Watch the FULL recording once and return the entire
segment list, with exactly one row for every startup in the current proposal.

Current full proposal:
{segments_json}

Verifier feedback by startup:
{feedback_json}

Revise every startup named in the feedback. Preserve every row absent from the
feedback exactly. A start must be the host announcement beginning the full
gradeable pitch; an end must include the complete Q&A and stop before the next
announcement. If an earlier announcement was deferred, interrupted, or got no
response but the startup pitched later, use only the completed later pitch.
Use absolute timestamps from the original recording as m:ss or h:mm:ss.
""".strip()


def _call_round_revision(
    youtube_url: str,
    segments: list[RoundSegment],
    feedback_by_name: dict[str, str],
    model: str,
) -> list[RoundSegment]:
    """Run one full-video Analyst revision for all rejected rows."""
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[
            _youtube_part(youtube_url),
            types.Part(text=ANALYST_REVISION_PROMPT.format(
                segments_json=json.dumps(
                    [segment.model_dump() for segment in segments],
                    ensure_ascii=False,
                ),
                feedback_json=json.dumps(feedback_by_name, ensure_ascii=False),
            )),
        ])],
        config=_indexer_generation_config(
            types.ThinkingLevel.HIGH,
            RoundSegmentList,
        ),
    )
    revised = RoundSegmentList.model_validate_json(response.text).segments
    expected_names = {segment.startup_name for segment in segments}
    revised_names = {segment.startup_name for segment in revised}
    if revised_names != expected_names or len(revised) != len(segments):
        raise RuntimeError("Analyst revision changed the round's startup set")

    original_by_name = {segment.startup_name: segment for segment in segments}
    for segment in revised:
        if segment.startup_name in feedback_by_name:
            continue
        original = original_by_name[segment.startup_name]
        if (
            segment.start != original.start
            or segment.end != original.end
            or segment.pitch_status != original.pitch_status
        ):
            raise RuntimeError(
                f"Analyst changed already-approved row {segment.startup_name!r}"
            )
    return sorted(revised, key=lambda segment: parse_timestamp(segment.start))


WRITER_PROMPT = """
You are the final writer for a completed startup-round review. Rows with
verified=true were approved; rows with verified=false reached the three-round
limit and require human review. Copy all rows into the required structured
response, preserving every startup name, timestamp, pitch_status, and verified
value exactly. Sort rows by timestamp. Do not reinterpret, correct, estimate,
or omit anything.

Reviewed rows:
{segments_json}
""".strip()


def _call_writer(segments: list[RoundSegment], model: str) -> list[RoundSegment]:
    """Run the LOW-thinking writer once after all row reviews finish."""
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=WRITER_PROMPT.format(
            segments_json=json.dumps(
                [segment.model_dump() for segment in segments],
                ensure_ascii=False,
            ),
        ),
        config=_indexer_generation_config(
            types.ThinkingLevel.LOW,
            RoundSegmentList,
        ),
    )
    written = RoundSegmentList.model_validate_json(response.text).segments
    expected = {
        segment.startup_name: segment.model_dump() for segment in segments
    }
    actual = {
        segment.startup_name: segment.model_dump() for segment in written
    }
    if actual != expected:
        raise RuntimeError("Writer changed verified segment data")
    return sorted(written, key=lambda segment: parse_timestamp(segment.start))


def _names_match(expected: str, seen: str) -> bool:
    """Forgiving comparison: emcee pronunciation/casing/punctuation drift is
    normal; a genuinely different startup won't survive containment checks."""
    a = "".join(ch for ch in expected.lower() if ch.isalnum())
    b = "".join(ch for ch in seen.lower() if ch.isalnum())
    return bool(a) and bool(b) and (a in b or b in a)


def _verification_feedback(
    segment: RoundSegment,
    verdict: SegmentVerification,
) -> str:
    reasons = []
    if not _names_match(segment.startup_name, verdict.startup_name_seen):
        reasons.append(
            f"Expected {segment.startup_name!r}, saw "
            f"{verdict.startup_name_seen!r}."
        )
    if verdict.pitch_status_seen != segment.pitch_status:
        reasons.append(
            f"Expected status {segment.pitch_status!r}, verifier saw "
            f"{verdict.pitch_status_seen!r}."
        )
    if not verdict.start_precise:
        reasons.append("Start timestamp is not the precise gradeable announcement.")
    if not verdict.end_precise:
        reasons.append("End timestamp does not precisely capture the full Q&A boundary.")
    if verdict.feedback.strip():
        reasons.append(verdict.feedback.strip())
    return " ".join(reasons) or "Verifier rejected the segment without details."


def _verify_segment(
    youtube_url: str,
    segment: RoundSegment,
    verifier_model: str,
) -> tuple[bool, str]:
    verdict = _call_segment_verifier(youtube_url, segment, verifier_model)
    approved = (
        verdict.approved
        and _names_match(segment.startup_name, verdict.startup_name_seen)
        and verdict.pitch_status_seen == segment.pitch_status
        and verdict.start_precise
        and verdict.end_precise
    )
    return approved, "" if approved else _verification_feedback(segment, verdict)


def _validated_triples(
    segments: list[RoundSegment],
    startup_names: list[str],
) -> list[tuple[str, int, int]]:
    triples = [
        (
            segment.startup_name,
            parse_timestamp(segment.start),
            parse_timestamp(segment.end),
        )
        for segment in segments
    ]
    short_ok = frozenset(
        segment.startup_name
        for segment in segments
        if segment.pitch_status != "pitched"
    )
    validate_segments(triples, startup_names, short_ok)
    return triples


def index_round(
    youtube_url: str,
    startup_names: list[str],
    analyst_model: str,
    verifier_model: str | None = None,
    writer_model: str | None = None,
) -> list[RoundSegment]:
    """Analyze globally, verify sequentially, and revise globally."""
    verifier_model = verifier_model or analyst_model
    writer_model = writer_model or analyst_model

    proposal = _call_segmenter(youtube_url, startup_names, analyst_model)
    segments = sorted(
        proposal.segments,
        key=lambda segment: parse_timestamp(segment.start),
    )
    _validated_triples(segments, startup_names)

    names_to_verify = set(startup_names)
    unresolved_names: set[str] = set()

    for review_round in range(INDEXER_REVIEW_ATTEMPTS):
        feedback_by_name: dict[str, str] = {}
        for segment in segments:
            if segment.startup_name not in names_to_verify:
                continue
            approved, feedback = _verify_segment(
                youtube_url,
                segment,
                verifier_model,
            )
            if not approved:
                feedback_by_name[segment.startup_name] = feedback

        if not feedback_by_name:
            unresolved_names = set()
            break
        if review_round + 1 == INDEXER_REVIEW_ATTEMPTS:
            unresolved_names = set(feedback_by_name)
            break

        segments = _call_round_revision(
            youtube_url,
            segments,
            feedback_by_name,
            analyst_model,
        )
        _validated_triples(segments, startup_names)
        names_to_verify = set(feedback_by_name)

    reviewed = [
        segment.model_copy(update={
            "verified": segment.startup_name not in unresolved_names,
        })
        for segment in segments
    ]
    _validated_triples(reviewed, startup_names)

    written = _call_writer(reviewed, writer_model)
    _validated_triples(written, startup_names)
    return written


def _write_round_cells(
    sheets_service,
    sheet_id: str,
    sheet_name: str,
    row_updates: list[tuple[int, dict[str, str]]],
    col_lookup: dict[str, int],
) -> None:
    """Write the complete reviewed round in one Sheets API request."""
    data = []
    for sheet_row_number, values_by_header in row_updates:
        for header_name, value in values_by_header.items():
            idx = col_lookup[header_name.strip().lower()]
            letter = _col_letter(idx + 1)
            data.append({
                "range": f"{sheet_name}!{letter}{sheet_row_number}",
                "values": [[value]],
            })
    if not data:
        return
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute(num_retries=5)


def run_round_indexing(config, youtube_url: str) -> dict:
    """Index one round recording and write results to the sheet.

    Reads the round tab via `config` (same sheet geometry the grading run
    uses), excludes no-show rows, runs index_round, and writes: the round
    URL into each indexed row's Video cell, and start/end into the two
    segment columns. Rows unresolved after three rounds receive a human-review
    marker; technical failures still write nothing. Returns a summary:
    {"indexed": n, "needs_check": n, "segments": [RoundSegment, ...]}.
    Fails loudly if the operator hasn't added the two segment columns or a
    Startup Name/Video column is missing.
    """
    youtube_url = canonicalize_youtube_url(youtube_url)
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

    segments = index_round(
        youtube_url,
        [name for _, name in candidates],
        analyst_model=config.analyzer_model,
        verifier_model=config.grader_model,
        writer_model=config.head_model,
    )
    by_name = {s.startup_name: s for s in segments}

    sheet_name = config.sheet_range.split("!")[0].strip("'")
    needs_check = 0
    no_pitch = 0
    row_updates: list[tuple[int, dict[str, str]]] = []
    for sheet_row_number, name in candidates:
        seg = by_name[name]
        if not seg.verified:
            needs_check += 1
            marker = "NEEDS HUMAN REVIEW: "
            row_updates.append((
                sheet_row_number,
                {
                    "Video": youtube_url,
                    SEGMENT_START_COLUMN: f"{marker}{seg.start}",
                    SEGMENT_END_COLUMN: f"{marker}{seg.end}",
                },
            ))
            continue
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
            row_updates.append((
                sheet_row_number,
                {
                    "Video": "",
                    SEGMENT_START_COLUMN: marker,
                    SEGMENT_END_COLUMN: marker,
                },
            ))
            continue
        row_updates.append((
            sheet_row_number,
            {
                "Video": youtube_url,
                SEGMENT_START_COLUMN: seg.start,
                SEGMENT_END_COLUMN: seg.end,
            },
        ))
    _write_round_cells(
        sheets_service,
        config.sheet_id,
        sheet_name,
        row_updates,
        col_lookup,
    )
    return {
        "indexed": len(segments),
        "needs_check": needs_check,
        "no_pitch": no_pitch,
        "segments": segments,
    }

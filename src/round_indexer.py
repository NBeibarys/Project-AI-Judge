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

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from .round_segments import (
    SEGMENT_END_COLUMN,
    SEGMENT_START_COLUMN,
    parse_timestamp,
    validate_segments,
)

# One retry after a short pause for the big segmenter call — a single
# transient failure on an hour-long-video call shouldn't force the operator
# to re-click, but we don't loop beyond that (fail loudly instead).
SEGMENTER_ATTEMPTS = 2

# Deliberately mirrors the grading pipeline's determinism choices.
INDEXER_TEMPERATURE = 0.0
INDEXER_SEED = 7524


class RoundSegment(BaseModel):
    startup_name: str = Field(min_length=1)
    start: str = Field(min_length=3, description="Segment start, m:ss or h:mm:ss")
    end: str = Field(min_length=3, description="Segment end, m:ss or h:mm:ss")
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
            response = _client().models.generate_content(
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
    response = _client().models.generate_content(
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
    triples = [
        (seg.startup_name, parse_timestamp(seg.start), parse_timestamp(seg.end))
        for seg in proposal.segments
    ]
    validate_segments(triples, startup_names)

    results: list[RoundSegment] = []
    for seg, (_, start_s, end_s) in zip(proposal.segments, triples):
        seen = _call_segment_verifier(youtube_url, start_s, end_s, model)
        results.append(RoundSegment(
            startup_name=seg.startup_name,
            start=seg.start,
            end=seg.end,
            verified=_names_match(seg.startup_name, seen),
        ))
    return results

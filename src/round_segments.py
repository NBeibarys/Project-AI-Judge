"""Pure segment logic for operator-entered round-video segment timestamps.

No I/O here — timestamp parsing and structural validation of a
segment list. An operator types each startup's Segment Start/End into the
sheet by hand; the grading pipeline's virtual-clip input assembly
(src/pipeline.py's _read_segment_bounds) reads them from there. There is
no automatic proposer — an earlier LLM-based auto-indexer that watched the
full round recording and proposed these timestamps was removed (it
produced unreliable boundaries on complex rounds, confusing the model
about which span belonged to which startup).
"""

# Sheet column headers the operator adds once per round tab. Resolved by
# name (whitespace/case-tolerant) everywhere — never by position.
SEGMENT_START_COLUMN = "Segment Start"
SEGMENT_END_COLUMN = "Segment End"


class SegmentValidationError(ValueError):
    """A timestamp failed parsing."""


def parse_timestamp(text: str) -> int:
    """'3:12' -> 192; '1:02:48' -> 3768; '61:03' -> 3663.

    In the two-part form, minutes are UNBOUNDED: past the hour mark both
    video players and operators naturally write '61:03' for 1h01m03s
    (confirmed live — a real round recording's segments failed parsing on
    exactly this). Seconds must always be 0-59; the three-part h:mm:ss form
    keeps minutes 0-59 too. Raises SegmentValidationError.
    """
    parts = [p for p in (text or "").strip().split(":")]
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() and p != "" for p in parts):
        raise SegmentValidationError(f"Unparseable timestamp: {text!r}")
    numbers = [int(p) for p in parts]
    hours, minutes, seconds = ([0] + numbers) if len(numbers) == 2 else numbers
    if seconds > 59 or (len(numbers) == 3 and minutes > 59):
        raise SegmentValidationError(f"Out-of-range minutes/seconds: {text!r}")
    return hours * 3600 + minutes * 60 + seconds

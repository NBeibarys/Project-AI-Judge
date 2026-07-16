"""Pure segment logic for round-video auto-indexing.

No I/O here — timestamp parsing/formatting and structural validation of a
proposed segment list, shared by the indexer (src/round_indexer.py) and the
grading pipeline's virtual-clip input assembly (src/pipeline.py).
Spec: docs/superpowers/specs/2026-07-14-round-video-auto-indexing-design.md
"""

# Sheet column headers the operator adds once per round tab. Resolved by
# name (whitespace/case-tolerant) everywhere — never by position.
SEGMENT_START_COLUMN = "Segment Start"
SEGMENT_END_COLUMN = "Segment End"

# A pitch+Q&A slot shorter than a minute or longer than half an hour is
# far outside how these competitions run — almost certainly a hallucinated
# or misread boundary, so fail closed and let the operator decide.
MIN_SEGMENT_SECONDS = 60
MAX_SEGMENT_SECONDS = 30 * 60


class SegmentValidationError(ValueError):
    """A proposed segment list (or single timestamp) failed validation."""


def parse_timestamp(text: str) -> int:
    """'3:12' -> 192; '1:02:48' -> 3768; '61:03' -> 3663.

    In the two-part form, minutes are UNBOUNDED: past the hour mark both
    the segmenter model and video players naturally write '61:03' for
    1h01m03s (confirmed live — a real round recording's segments failed
    parsing on exactly this). Seconds must always be 0-59; the three-part
    h:mm:ss form keeps minutes 0-59 too. Raises SegmentValidationError.
    """
    parts = [p for p in (text or "").strip().split(":")]
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() and p != "" for p in parts):
        raise SegmentValidationError(f"Unparseable timestamp: {text!r}")
    numbers = [int(p) for p in parts]
    hours, minutes, seconds = ([0] + numbers) if len(numbers) == 2 else numbers
    if seconds > 59 or (len(numbers) == 3 and minutes > 59):
        raise SegmentValidationError(f"Out-of-range minutes/seconds: {text!r}")
    return hours * 3600 + minutes * 60 + seconds


def seconds_to_timestamp(total: int) -> str:
    """192 -> '3:12'; 3768 -> '1:02:48' (inverse of parse_timestamp)."""
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def validate_segments(
    segments: list[tuple[str, int, int]],
    expected_names: list[str],
    short_ok_names: frozenset[str] = frozenset(),
) -> None:
    """Fail closed on any structural problem with a proposed segment list.

    segments: [(startup_name, start_seconds, end_seconds), ...] in video
    order (callers sort by start first — the segmenter sometimes returns
    sheet order instead of time order, confirmed live). expected_names:
    the sheet tab's startup names (no-shows already excluded). Checks:
    exact name set match, end > start, plausible length, strictly ordered
    and non-overlapping.

    short_ok_names: startups exempt from the MINIMUM length check — an
    announced-but-didn't-pitch slot (nobody responds, or the founder can't
    continue) is legitimately just a 10-30s announcement span, not a
    hallucinated boundary. The maximum-length and ordering checks still
    apply to them.
    """
    got = [name for name, _, _ in segments]
    if sorted(got) != sorted(expected_names):
        missing = sorted(set(expected_names) - set(got))
        unexpected = sorted(set(got) - set(expected_names))
        raise SegmentValidationError(
            f"Segment names must match the sheet exactly; "
            f"missing={missing}, unexpected={unexpected}"
        )
    previous_end = -1
    for name, start, end in segments:
        if end <= start:
            raise SegmentValidationError(f"{name}: end ({end}s) <= start ({start}s)")
        length = end - start
        min_length = 1 if name in short_ok_names else MIN_SEGMENT_SECONDS
        if not min_length <= length <= MAX_SEGMENT_SECONDS:
            raise SegmentValidationError(
                f"{name}: implausible segment length {length}s "
                f"(allowed {min_length}-{MAX_SEGMENT_SECONDS}s)"
            )
        if start < previous_end:
            raise SegmentValidationError(
                f"{name}: starts at {start}s, before the previous segment "
                f"ends at {previous_end}s (segments must not overlap)"
            )
        previous_end = end

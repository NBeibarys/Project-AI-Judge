import unittest

from src.round_segments import (
    SegmentValidationError,
    parse_timestamp,
    seconds_to_timestamp,
    validate_segments,
)


class ParseTimestampTests(unittest.TestCase):
    def test_mm_ss(self) -> None:
        self.assertEqual(parse_timestamp("3:12"), 192)

    def test_h_mm_ss(self) -> None:
        self.assertEqual(parse_timestamp("1:02:48"), 3768)

    def test_strips_whitespace(self) -> None:
        self.assertEqual(parse_timestamp(" 12:05 "), 725)

    def test_rejects_garbage(self) -> None:
        for bad in ("", "12", "1:2:3:4", "3:75", "abc", "3:1x"):
            with self.assertRaises(SegmentValidationError, msg=bad):
                parse_timestamp(bad)


class SecondsToTimestampTests(unittest.TestCase):
    def test_under_an_hour(self) -> None:
        self.assertEqual(seconds_to_timestamp(192), "3:12")

    def test_over_an_hour(self) -> None:
        self.assertEqual(seconds_to_timestamp(3768), "1:02:48")


class ValidateSegmentsTests(unittest.TestCase):
    def _ok_segments(self):
        return [("Alpha", 60, 400), ("Beta", 410, 800), ("Gamma", 805, 1200)]

    def test_accepts_valid(self) -> None:
        validate_segments(self._ok_segments(), ["Alpha", "Beta", "Gamma"])

    def test_rejects_missing_startup(self) -> None:
        with self.assertRaises(SegmentValidationError):
            validate_segments(self._ok_segments()[:2], ["Alpha", "Beta", "Gamma"])

    def test_rejects_unexpected_startup(self) -> None:
        segs = self._ok_segments() + [("Delta", 1210, 1500)]
        with self.assertRaises(SegmentValidationError):
            validate_segments(segs, ["Alpha", "Beta", "Gamma"])

    def test_rejects_overlap(self) -> None:
        segs = [("Alpha", 60, 500), ("Beta", 410, 800), ("Gamma", 805, 1200)]
        with self.assertRaises(SegmentValidationError):
            validate_segments(segs, ["Alpha", "Beta", "Gamma"])

    def test_rejects_end_before_start(self) -> None:
        segs = [("Alpha", 400, 60), ("Beta", 410, 800), ("Gamma", 805, 1200)]
        with self.assertRaises(SegmentValidationError):
            validate_segments(segs, ["Alpha", "Beta", "Gamma"])

    def test_rejects_implausible_length(self) -> None:
        too_short = [("Alpha", 60, 90), ("Beta", 410, 800), ("Gamma", 805, 1200)]
        with self.assertRaises(SegmentValidationError):
            validate_segments(too_short, ["Alpha", "Beta", "Gamma"])
        too_long = [("Alpha", 60, 60 + 31 * 60), ("Beta", 2000, 2400), ("Gamma", 2405, 2800)]
        with self.assertRaises(SegmentValidationError):
            validate_segments(too_long, ["Alpha", "Beta", "Gamma"])


if __name__ == "__main__":
    unittest.main()

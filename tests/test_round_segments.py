import unittest

from src.pipeline import _read_segment_bounds, _segment_needs_human_review
from src.round_segments import SegmentValidationError, parse_timestamp


class ParseTimestampTests(unittest.TestCase):
    def test_mm_ss(self) -> None:
        self.assertEqual(parse_timestamp("3:12"), 192)

    def test_h_mm_ss(self) -> None:
        self.assertEqual(parse_timestamp("1:02:48"), 3768)

    def test_strips_whitespace(self) -> None:
        self.assertEqual(parse_timestamp(" 12:05 "), 725)

    def test_minutes_beyond_hour_in_two_part_form(self) -> None:
        # Past the hour mark, "61:03" (= 1:01:03) is how models and video
        # players naturally write it — confirmed live on a real recording.
        self.assertEqual(parse_timestamp("61:03"), 3663)

    def test_rejects_garbage(self) -> None:
        for bad in ("", "12", "1:2:3:4", "3:75", "1:61:03", "abc", "3:1x"):
            with self.assertRaises(SegmentValidationError, msg=bad):
                parse_timestamp(bad)


class ReadSegmentBoundsTests(unittest.TestCase):
    HEADER = ["Startup Name", "Video", "Segment Start", "Segment End"]

    def test_parses_clean_cells(self) -> None:
        row = ["Alpha", "https://youtube.com/watch?v=x", "3:12", "9:48"]
        self.assertEqual(_read_segment_bounds(self.HEADER, row), (192, 588))

    def test_blank_cells_mean_no_segment(self) -> None:
        row = ["Alpha", "https://youtube.com/watch?v=x", "", ""]
        self.assertIsNone(_read_segment_bounds(self.HEADER, row))

    def test_needs_check_marker_means_no_segment(self) -> None:
        row = ["Alpha", "url", "NEEDS CHECK: 3:12", "NEEDS CHECK: 9:48"]
        self.assertIsNone(_read_segment_bounds(self.HEADER, row))

    def test_human_review_marker_blocks_full_video_fallback(self) -> None:
        row = [
            "Alpha",
            "url",
            "NEEDS HUMAN REVIEW: 3:12",
            "NEEDS HUMAN REVIEW: 9:48",
        ]
        self.assertTrue(_segment_needs_human_review(self.HEADER, row))
        self.assertIsNone(_read_segment_bounds(self.HEADER, row))

    def test_malformed_cells_mean_no_segment(self) -> None:
        row = ["Alpha", "url", "3:12", "oops"]
        self.assertIsNone(_read_segment_bounds(self.HEADER, row))

    def test_missing_columns_mean_no_segment(self) -> None:
        self.assertIsNone(_read_segment_bounds(["Startup Name", "Video"], ["Alpha", "url"]))

    def test_inverted_bounds_mean_no_segment(self) -> None:
        row = ["Alpha", "url", "9:48", "3:12"]
        self.assertIsNone(_read_segment_bounds(self.HEADER, row))


if __name__ == "__main__":
    unittest.main()

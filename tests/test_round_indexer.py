import unittest
from unittest.mock import MagicMock, patch

from src.round_indexer import (
    RoundSegment,
    RoundSegmentList,
    index_round,
    run_round_indexing,
)
from src.round_segments import SegmentValidationError


def _fake_llm_segments():
    return RoundSegmentList(segments=[
        RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        RoundSegment(startup_name="Beta", start="9:40", end="16:00"),
    ])


class IndexRoundTests(unittest.TestCase):
    @patch("src.round_indexer._refine_start", return_value=None)
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_happy_path_returns_verified_segments(self, mock_seg, mock_verify, mock_refine) -> None:
        mock_seg.return_value = _fake_llm_segments()
        mock_verify.side_effect = ["Alpha", "Beta"]  # verifier echoes correct names
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        # Ends derive from the NEXT segment's start minus 1s (contiguous
        # recording); the last segment keeps its coarse end.
        self.assertEqual(
            [(r.startup_name, r.start, r.end, r.verified) for r in result],
            [("Alpha", "3:00", "9:39", True), ("Beta", "9:40", "16:00", True)],
        )

    @patch("src.round_indexer._refine_start", return_value=None)
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_verifier_mismatch_marks_unverified(self, mock_seg, mock_verify, mock_refine) -> None:
        mock_seg.return_value = _fake_llm_segments()
        mock_verify.side_effect = ["Alpha", "Gamma"]  # wrong pitch in Beta's span
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertTrue(result[0].verified)
        self.assertFalse(result[1].verified)

    @patch("src.round_indexer._refine_start")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_refined_starts_shift_boundaries(self, mock_seg, mock_verify, mock_refine) -> None:
        mock_seg.return_value = _fake_llm_segments()
        # Alpha's start pinned 10s later; Beta's pinned 20s later — Alpha's
        # end must follow Beta's REFINED start, not the coarse one.
        mock_refine.side_effect = [190, 600]
        mock_verify.side_effect = ["Alpha", "Beta"]
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertEqual(
            [(r.startup_name, r.start, r.end) for r in result],
            [("Alpha", "3:10", "9:59"), ("Beta", "10:00", "16:00")],
        )

    @patch("src.round_indexer._call_segmenter")
    def test_structural_failure_raises(self, mock_seg) -> None:
        mock_seg.return_value = RoundSegmentList(segments=[
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        ])  # Beta missing
        with self.assertRaises(SegmentValidationError):
            index_round("https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m")


class RunRoundIndexingTests(unittest.TestCase):
    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_row_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_writes_segments_and_video_url(
        self, mock_svc, mock_read, mock_write, mock_index,
    ) -> None:
        header = ["Startup Name", "Video", "Segment Start", "Segment End"]
        rows = [["Alpha", "", "", ""], ["Beta - didn't respond", "", "", ""], ["Gamma", "", "", ""]]
        mock_read.return_value = (header, rows)
        mock_index.return_value = [
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30", verified=True),
            RoundSegment(startup_name="Gamma", start="9:40", end="16:00", verified=False),
        ]
        config = MagicMock()
        config.header_row = 2
        config.analyzer_model = "m"
        config.sheet_range = "AI"
        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        # no-show row excluded from indexing input
        mock_index.assert_called_once()
        self.assertEqual(mock_index.call_args[0][1], ["Alpha", "Gamma"])
        # Alpha (sheet row 3): url + clean timestamps
        # Gamma (sheet row 5): NEEDS CHECK marker on unverified segment
        written = {call.args[3]: call.args[4] for call in mock_write.call_args_list}
        self.assertEqual(
            written[3],
            {"Video": "https://youtube.com/watch?v=x", "Segment Start": "3:00", "Segment End": "9:30"},
        )
        self.assertEqual(
            written[5],
            {"Video": "https://youtube.com/watch?v=x",
             "Segment Start": "NEEDS CHECK: 9:40", "Segment End": "NEEDS CHECK: 16:00"},
        )
        self.assertEqual(summary["indexed"], 2)
        self.assertEqual(summary["needs_check"], 1)


if __name__ == "__main__":
    unittest.main()

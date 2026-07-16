import unittest
from collections.abc import Callable
from unittest.mock import MagicMock, patch

from src.round_indexer import (
    RoundSegment,
    RoundSegmentList,
    index_round,
    run_round_indexing,
)
from src.round_segments import SegmentValidationError


def _fake_llm_segments() -> RoundSegmentList:
    return RoundSegmentList(segments=[
        RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        RoundSegment(startup_name="Beta", start="9:40", end="16:00"),
    ])


def _verifier_by_start(mapping: dict[int, str]) -> Callable[[str, int, int, str], str]:
    """Refinement/verification run in a thread pool — mock side_effect
    LISTS are consumed in racy order there, so key fakes by call args."""
    def fake(url: str, start_s: int, end_s: int, model: str) -> str:
        return mapping[start_s]
    return fake


class IndexRoundTests(unittest.TestCase):
    @patch("src.round_indexer._refine_start", return_value=None)
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_happy_path_returns_verified_segments(self, mock_seg, mock_verify, mock_refine) -> None:
        mock_seg.return_value = _fake_llm_segments()
        mock_verify.side_effect = _verifier_by_start({180: "Alpha", 580: "Beta"})
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
        mock_verify.side_effect = _verifier_by_start({180: "Alpha", 580: "Gamma"})
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
        # end must follow Beta's REFINED start, not the coarse one. Keyed
        # by startup name (thread pool order is not deterministic).
        mock_refine.side_effect = (
            lambda url, coarse_s, name, model: {"Alpha": 190, "Beta": 600}[name]
        )
        mock_verify.side_effect = _verifier_by_start({190: "Alpha", 600: "Beta"})
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertEqual(
            [(r.startup_name, r.start, r.end) for r in result],
            [("Alpha", "3:10", "9:59"), ("Beta", "10:00", "16:00")],
        )

    @patch("src.round_indexer._refine_start", return_value=None)
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_sheet_order_output_is_sorted_by_time(self, mock_seg, mock_verify, mock_refine) -> None:
        # The segmenter sometimes returns segments in sheet order rather
        # than time order (confirmed live) — index_round must sort instead
        # of failing the overlap check.
        mock_seg.return_value = RoundSegmentList(segments=[
            RoundSegment(startup_name="Beta", start="9:40", end="16:00"),
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        ])
        mock_verify.side_effect = _verifier_by_start({180: "Alpha", 580: "Beta"})
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertEqual([r.startup_name for r in result], ["Alpha", "Beta"])

    @patch("src.round_indexer._refine_start")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_no_pitch_slot_skips_refinement_and_verification(self, mock_seg, mock_verify, mock_refine) -> None:
        # An announced-but-no-response slot is legitimately short (below
        # the normal 60s minimum), is never refined or verified, and keeps
        # its status on the result.
        mock_seg.return_value = RoundSegmentList(segments=[
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
            RoundSegment(startup_name="Beta", start="9:40", end="10:00",
                         pitch_status="announced_no_response"),
        ])
        mock_refine.side_effect = (
            lambda url, coarse_s, name, model: {"Alpha": 180}[name]
        )
        mock_verify.side_effect = _verifier_by_start({180: "Alpha"})
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertEqual(result[1].pitch_status, "announced_no_response")
        self.assertTrue(result[1].verified)
        mock_refine.assert_called_once()
        mock_verify.assert_called_once()

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
        self.assertEqual(summary["no_pitch"], 0)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_row_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_no_pitch_writes_no_frame_marker_and_clears_video(
        self, mock_svc, mock_read, mock_write, mock_index,
    ) -> None:
        header = ["Startup Name", "Video", "Segment Start", "Segment End"]
        rows = [["Alpha", "https://youtube.com/watch?v=old", "1:00", "5:00"]]
        mock_read.return_value = (header, rows)
        mock_index.return_value = [
            RoundSegment(
                startup_name="Alpha",
                start="3:00",
                end="3:20",
                pitch_status="announced_no_response",
            ),
        ]
        config = MagicMock()
        config.header_row = 2
        config.analyzer_model = "m"
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        mock_write.assert_called_once()
        self.assertEqual(
            mock_write.call_args.args[4],
            {
                "Video": "",
                "Segment Start": "NO PITCH (no response)",
                "Segment End": "NO PITCH (no response)",
            },
        )
        self.assertEqual(summary["indexed"], 1)
        self.assertEqual(summary["needs_check"], 0)
        self.assertEqual(summary["no_pitch"], 1)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_row_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_aborted_slot_writes_no_frame_marker(
        self, mock_svc, mock_read, mock_write, mock_index,
    ) -> None:
        header = ["Startup Name", "Video", "Segment Start", "Segment End"]
        rows = [["Alpha", "https://youtube.com/watch?v=old", "1:00", "5:00"]]
        mock_read.return_value = (header, rows)
        mock_index.return_value = [
            RoundSegment(
                startup_name="Alpha",
                start="3:00",
                end="3:45",
                pitch_status="aborted",
            ),
        ]
        config = MagicMock()
        config.header_row = 2
        config.analyzer_model = "m"
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        self.assertEqual(
            mock_write.call_args.args[4],
            {
                "Video": "",
                "Segment Start": "NO PITCH (aborted)",
                "Segment End": "NO PITCH (aborted)",
            },
        )
        self.assertEqual(summary["no_pitch"], 1)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from src.round_indexer import (
    RoundSegment,
    RoundSegmentList,
    index_round,
)
from src.round_segments import SegmentValidationError


def _fake_llm_segments():
    return RoundSegmentList(segments=[
        RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        RoundSegment(startup_name="Beta", start="9:40", end="16:00"),
    ])


class IndexRoundTests(unittest.TestCase):
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_happy_path_returns_verified_segments(self, mock_seg, mock_verify) -> None:
        mock_seg.return_value = _fake_llm_segments()
        mock_verify.side_effect = ["Alpha", "Beta"]  # verifier echoes correct names
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertEqual(
            [(r.startup_name, r.start, r.end, r.verified) for r in result],
            [("Alpha", "3:00", "9:30", True), ("Beta", "9:40", "16:00", True)],
        )

    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_verifier_mismatch_marks_unverified(self, mock_seg, mock_verify) -> None:
        mock_seg.return_value = _fake_llm_segments()
        mock_verify.side_effect = ["Alpha", "Gamma"]  # wrong pitch in Beta's span
        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m",
        )
        self.assertTrue(result[0].verified)
        self.assertFalse(result[1].verified)

    @patch("src.round_indexer._call_segmenter")
    def test_structural_failure_raises(self, mock_seg) -> None:
        mock_seg.return_value = RoundSegmentList(segments=[
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        ])  # Beta missing
        with self.assertRaises(SegmentValidationError):
            index_round("https://youtube.com/watch?v=x", ["Alpha", "Beta"], model="m")


if __name__ == "__main__":
    unittest.main()

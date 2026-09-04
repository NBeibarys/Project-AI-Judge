import unittest
from unittest.mock import MagicMock, patch

from src.pipeline import process_row
from src.video_urls import ResolvedMedia, VideoResolutionError

HEADER = ["Startup Name", "Video", "Segment Start", "Segment End"]
ROW = [
    "Alpha",
    "https://drive.google.com/file/d/1a2b3c4d5e6f7g8h9i/view",
    "3:12",
    "9:48",
]


def _r2b_config() -> MagicMock:
    config = MagicMock()
    config.service_account_path = "dummy-service-account.json"
    config.program_config.program = "r2b"
    config.program_config.criterion_column_names = {"Problem & Solution": "B"}
    config.program_config.requires_pitch_deck = False
    config.program_config.name_column_name = None
    config.program_config.excluded_header_names = frozenset()
    config.program_config.excluded_header_substrings = ()
    return config


class InlineVideoSegmentTests(unittest.TestCase):
    """Server-side clipping needs a URI. A Tier-2 Vertex download has only
    inline bytes, so a segmented row must escalate rather than be graded on
    the whole round recording (every other startup's pitch included)."""

    @patch("src.pipeline.ingest_video")
    @patch("src.pipeline.resolve_video_url")
    def test_segmented_inline_video_escalates_without_grading(
        self, mock_resolve: MagicMock, mock_ingest: MagicMock
    ) -> None:
        mock_resolve.side_effect = VideoResolutionError("no direct video")
        mock_ingest.return_value = ResolvedMedia(
            uri="", mime_type="video/mp4", source="drive_upload", data=b"x",
        )
        checkpoint = MagicMock()
        checkpoint.is_done.return_value = False
        workflow = MagicMock()

        _row_id, result = process_row(
            _r2b_config(), workflow, HEADER, ROW, 3, checkpoint,
        )

        self.assertTrue(result["human_review_flag"])
        self.assertEqual(result["criterion_scores"], {})
        self.assertIsNone(result["total_score"])
        self.assertIn("clip locally", result["notes"])
        workflow.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()

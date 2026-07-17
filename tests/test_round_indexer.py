import unittest
from unittest.mock import MagicMock, patch

from google.genai import types

from src.round_indexer import (
    INDEXER_CONCURRENCY,
    RoundSegment,
    RoundSegmentList,
    SegmentVerification,
    _call_round_revision,
    _call_segmenter,
    _call_segment_verifier,
    _call_writer,
    _write_round_cells,
    index_round,
    run_round_indexing,
)
from src.round_segments import SegmentValidationError


def _fake_segments() -> RoundSegmentList:
    return RoundSegmentList(segments=[
        RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        RoundSegment(startup_name="Beta", start="9:40", end="16:00"),
    ])


def _approved(segment: RoundSegment) -> SegmentVerification:
    return SegmentVerification(
        startup_name_seen=segment.startup_name,
        pitch_status_seen=segment.pitch_status,
        start_precise=True,
        end_precise=True,
        approved=True,
    )


def _rejected(segment: RoundSegment) -> SegmentVerification:
    return SegmentVerification(
        startup_name_seen=segment.startup_name,
        pitch_status_seen=segment.pitch_status,
        start_precise=False,
        end_precise=False,
        approved=False,
        feedback="Correct both boundaries.",
    )


class IndexRoundTests(unittest.TestCase):
    @patch("src.round_indexer._call_writer")
    @patch("src.round_indexer._call_round_revision")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_one_full_analyst_sequential_verification_and_one_writer(
        self, mock_segmenter, mock_verifier, mock_revision, mock_writer,
    ) -> None:
        mock_segmenter.return_value = _fake_segments()
        mock_verifier.side_effect = lambda url, segment, model: _approved(segment)
        mock_writer.side_effect = lambda segments, model: segments

        result = index_round(
            "https://youtube.com/watch?v=x",
            ["Alpha", "Beta"],
            analyst_model="analyst-m",
            verifier_model="verifier-m",
            writer_model="writer-m",
        )

        self.assertEqual(INDEXER_CONCURRENCY, 1)
        mock_segmenter.assert_called_once_with(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], "analyst-m",
        )
        self.assertEqual(mock_verifier.call_count, 2)
        mock_revision.assert_not_called()
        mock_writer.assert_called_once()
        self.assertEqual(mock_writer.call_args.args[1], "writer-m")
        self.assertTrue(all(segment.verified for segment in result))

    @patch("src.round_indexer._call_writer")
    @patch("src.round_indexer._call_round_revision")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_all_rejected_feedback_uses_one_global_analyst_revision(
        self, mock_segmenter, mock_verifier, mock_revision, mock_writer,
    ) -> None:
        original = _fake_segments().segments
        revised = [
            RoundSegment(startup_name="Alpha", start="3:12", end="9:35"),
            RoundSegment(startup_name="Beta", start="9:45", end="16:10"),
        ]
        mock_segmenter.return_value = RoundSegmentList(segments=original)
        mock_verifier.side_effect = (
            lambda url, segment, model:
            _rejected(segment) if segment.start in {"3:00", "9:40"} else _approved(segment)
        )
        mock_revision.return_value = revised
        mock_writer.side_effect = lambda segments, model: segments

        result = index_round(
            "https://youtube.com/watch?v=x",
            ["Alpha", "Beta"],
            analyst_model="analyst-m",
            verifier_model="verifier-m",
            writer_model="writer-m",
        )

        mock_revision.assert_called_once()
        self.assertEqual(
            set(mock_revision.call_args.args[2]),
            {"Alpha", "Beta"},
        )
        self.assertEqual(mock_revision.call_args.args[3], "analyst-m")
        self.assertEqual(mock_verifier.call_count, 4)
        self.assertEqual([segment.start for segment in result], ["3:12", "9:45"])
        self.assertTrue(all(segment.verified for segment in result))

    @patch("src.round_indexer._call_writer")
    @patch("src.round_indexer._call_round_revision")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_third_rejection_becomes_human_review_and_still_reaches_writer(
        self, mock_segmenter, mock_verifier, mock_revision, mock_writer,
    ) -> None:
        segment = RoundSegment(startup_name="Alpha", start="3:00", end="9:30")
        mock_segmenter.return_value = RoundSegmentList(segments=[segment])
        mock_verifier.side_effect = lambda url, current, model: _rejected(current)
        mock_revision.return_value = [segment]
        mock_writer.side_effect = lambda segments, model: segments

        result = index_round(
            "https://youtube.com/watch?v=x",
            ["Alpha"],
            analyst_model="analyst-m",
            verifier_model="verifier-m",
            writer_model="writer-m",
        )

        self.assertEqual(mock_verifier.call_count, 3)
        self.assertEqual(mock_revision.call_count, 2)
        mock_writer.assert_called_once()
        self.assertFalse(result[0].verified)

    @patch("src.round_indexer._call_writer")
    @patch("src.round_indexer._call_round_revision")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_no_pitch_status_is_also_verified(
        self, mock_segmenter, mock_verifier, mock_revision, mock_writer,
    ) -> None:
        segment = RoundSegment(
            startup_name="Alpha",
            start="3:00",
            end="3:20",
            pitch_status="announced_no_response",
        )
        mock_segmenter.return_value = RoundSegmentList(segments=[segment])
        mock_verifier.return_value = _approved(segment)
        mock_writer.side_effect = lambda segments, model: segments

        result = index_round(
            "https://youtube.com/watch?v=x", ["Alpha"], analyst_model="m",
        )

        mock_verifier.assert_called_once()
        mock_revision.assert_not_called()
        self.assertEqual(result[0].pitch_status, "announced_no_response")

    @patch("src.round_indexer._call_writer")
    @patch("src.round_indexer._call_segment_verifier")
    @patch("src.round_indexer._call_segmenter")
    def test_structural_failure_stops_before_verification(
        self, mock_segmenter, mock_verifier, mock_writer,
    ) -> None:
        mock_segmenter.return_value = RoundSegmentList(segments=[
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        ])

        with self.assertRaises(SegmentValidationError):
            index_round(
                "https://youtube.com/watch?v=x",
                ["Alpha", "Beta"],
                analyst_model="m",
            )

        mock_verifier.assert_not_called()
        mock_writer.assert_not_called()

    @patch("src.round_indexer._client")
    def test_analysts_and_verifier_use_high_writer_uses_low(self, mock_client) -> None:
        model_api = mock_client.return_value.models.generate_content
        segments = _fake_segments().segments

        model_api.return_value.text = RoundSegmentList(segments=segments).model_dump_json()
        _call_segmenter(
            "https://youtube.com/watch?v=x", ["Alpha", "Beta"], "m",
        )
        initial_analyst_config = model_api.call_args.kwargs["config"]

        model_api.return_value.text = RoundSegmentList(segments=segments).model_dump_json()
        _call_round_revision(
            "https://youtube.com/watch?v=x",
            segments,
            {"Alpha": "Fix boundaries."},
            "m",
        )
        revision_analyst_config = model_api.call_args.kwargs["config"]

        segment = segments[0]
        model_api.return_value.text = _approved(segment).model_dump_json()
        _call_segment_verifier("https://youtube.com/watch?v=x", segment, "m")
        verifier_config = model_api.call_args.kwargs["config"]

        model_api.return_value.text = RoundSegmentList(segments=[segment]).model_dump_json()
        _call_writer([segment], "m")
        writer_config = model_api.call_args.kwargs["config"]

        for config in (initial_analyst_config, revision_analyst_config, verifier_config):
            self.assertEqual(
                config.thinking_config.thinking_level,
                types.ThinkingLevel.HIGH,
            )
        self.assertEqual(
            writer_config.thinking_config.thinking_level,
            types.ThinkingLevel.LOW,
        )
        for config in (
            initial_analyst_config,
            revision_analyst_config,
            verifier_config,
            writer_config,
        ):
            self.assertEqual(
                config.media_resolution,
                types.MediaResolution.MEDIA_RESOLUTION_LOW,
            )


class RunRoundIndexingTests(unittest.TestCase):
    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_writes_all_rows_in_one_final_batch(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [
                ["Alpha", "", "", ""],
                ["Beta - didn't respond", "", "", ""],
                ["Gamma", "", "", ""],
            ],
        )
        mock_index.return_value = [
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
            RoundSegment(startup_name="Gamma", start="9:40", end="16:00"),
        ]
        config = MagicMock()
        config.header_row = 2
        config.analyzer_model = "analyst-m"
        config.grader_model = "verifier-m"
        config.head_model = "writer-m"
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        mock_index.assert_called_once_with(
            "https://youtube.com/watch?v=x",
            ["Alpha", "Gamma"],
            analyst_model="analyst-m",
            verifier_model="verifier-m",
            writer_model="writer-m",
        )
        mock_write.assert_called_once()
        written = dict(mock_write.call_args.args[3])
        self.assertEqual(written[3]["Segment Start"], "3:00")
        self.assertEqual(written[5]["Segment End"], "16:00")
        self.assertEqual(summary["indexed"], 2)
        self.assertEqual(summary["needs_check"], 0)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_canonicalizes_youtube_url_before_analysis_and_writing(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [["Alpha", "", "", ""]],
        )
        mock_index.return_value = [
            RoundSegment(startup_name="Alpha", start="3:00", end="9:30"),
        ]
        config = MagicMock()
        config.header_row = 2
        config.analyzer_model = "analyst-m"
        config.grader_model = "verifier-m"
        config.head_model = "writer-m"
        config.sheet_range = "AI"

        run_round_indexing(
            config,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t",
        )

        canonical_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(mock_index.call_args.args[0], canonical_url)
        written = dict(mock_write.call_args.args[3])
        self.assertEqual(written[3]["Video"], canonical_url)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_technical_failure_writes_nothing(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [["Alpha", "", "", ""]],
        )
        mock_index.side_effect = RuntimeError("model unavailable")
        config = MagicMock()
        config.header_row = 2
        config.sheet_range = "AI"

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            run_round_indexing(config, "https://youtube.com/watch?v=x")

        mock_write.assert_not_called()

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_unresolved_row_writes_human_review_marker(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [["Alpha", "", "", ""]],
        )
        mock_index.return_value = [
            RoundSegment(
                startup_name="Alpha",
                start="3:00",
                end="9:30",
                verified=False,
            ),
        ]
        config = MagicMock()
        config.header_row = 2
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        written = dict(mock_write.call_args.args[3])
        self.assertEqual(written[3]["Segment Start"], "NEEDS HUMAN REVIEW: 3:00")
        self.assertEqual(written[3]["Segment End"], "NEEDS HUMAN REVIEW: 9:30")
        self.assertEqual(summary["needs_check"], 1)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_unresolved_no_pitch_also_writes_human_review_marker(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [["Alpha", "old-url", "1:00", "5:00"]],
        )
        mock_index.return_value = [
            RoundSegment(
                startup_name="Alpha",
                start="3:00",
                end="3:20",
                pitch_status="announced_no_response",
                verified=False,
            ),
        ]
        config = MagicMock()
        config.header_row = 2
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        written = dict(mock_write.call_args.args[3])
        self.assertEqual(written[3]["Segment Start"], "NEEDS HUMAN REVIEW: 3:00")
        self.assertEqual(written[3]["Video"], "https://youtube.com/watch?v=x")
        self.assertEqual(summary["needs_check"], 1)
        self.assertEqual(summary["no_pitch"], 0)

    @patch("src.round_indexer.index_round")
    @patch("src.round_indexer._write_round_cells")
    @patch("src.round_indexer.read_sheet_rows")
    @patch("src.round_indexer.get_sheets_service")
    def test_approved_no_pitch_writes_marker_and_clears_video(
        self, mock_service, mock_read, mock_write, mock_index,
    ) -> None:
        mock_read.return_value = (
            ["Startup Name", "Video", "Segment Start", "Segment End"],
            [["Alpha", "old-url", "1:00", "5:00"]],
        )
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
        config.sheet_range = "AI"

        summary = run_round_indexing(config, "https://youtube.com/watch?v=x")

        written = dict(mock_write.call_args.args[3])
        self.assertEqual(
            written[3],
            {
                "Video": "",
                "Segment Start": "NO PITCH (no response)",
                "Segment End": "NO PITCH (no response)",
            },
        )
        self.assertEqual(summary["no_pitch"], 1)

    def test_round_cells_use_one_sheets_batch_request(self) -> None:
        sheets_service = MagicMock()
        values_service = sheets_service.spreadsheets.return_value.values.return_value
        request = values_service.batchUpdate.return_value

        _write_round_cells(
            sheets_service,
            "sheet-id",
            "AI",
            [
                (3, {"Video": "url", "Segment Start": "1:00", "Segment End": "4:00"}),
                (4, {"Video": "url", "Segment Start": "4:01", "Segment End": "8:00"}),
            ],
            {"video": 1, "segment start": 2, "segment end": 3},
        )

        values_service.batchUpdate.assert_called_once()
        body = values_service.batchUpdate.call_args.kwargs["body"]
        self.assertEqual(body["valueInputOption"], "RAW")
        self.assertEqual(len(body["data"]), 6)
        request.execute.assert_called_once_with(num_retries=5)


if __name__ == "__main__":
    unittest.main()

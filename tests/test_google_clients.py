import unittest
from unittest.mock import MagicMock

from src.google_clients import (
    extract_drive_file_id,
    read_sheet_rows,
    write_multi_notes_only,
    write_reasoning_only,
)
from src.video_ingestion import _is_google_slides_url


class DriveLinkHostTests(unittest.TestCase):
    """Drive IDs are only honoured on Google's own hosts: the ID patterns
    themselves match any host, and the service account would happily fetch
    a file it can read but the applicant cannot."""

    def test_rejects_drive_id_on_a_foreign_host(self) -> None:
        self.assertIsNone(extract_drive_file_id("https://evil.tld/d/1a2b3c4d5e6f7g8h9i"))

    def test_accepts_drive_file_link(self) -> None:
        self.assertEqual(
            extract_drive_file_id("https://drive.google.com/file/d/1a2b3c4d5e6f7g8h9i/view"),
            "1a2b3c4d5e6f7g8h9i",
        )

    def test_accepts_drive_open_link(self) -> None:
        self.assertEqual(
            extract_drive_file_id("https://drive.google.com/open?id=1a2b3c4d5e6f7g8h9i"),
            "1a2b3c4d5e6f7g8h9i",
        )


class SlidesUrlTests(unittest.TestCase):
    def test_rejects_slides_address_outside_the_host(self) -> None:
        self.assertFalse(
            _is_google_slides_url("https://evil.tld/#docs.google.com/presentation/d/XYZ")
        )

    def test_accepts_real_slides_url(self) -> None:
        self.assertTrue(
            _is_google_slides_url("https://docs.google.com/presentation/d/1a2b3c4d/edit")
        )


class SheetRangeAnchorTests(unittest.TestCase):
    """A row-anchored range shifts every row the batch writes back, so it
    is rejected instead of silently grading the wrong applicants' rows."""

    @staticmethod
    def _service_returning_rows() -> MagicMock:
        service = MagicMock()
        service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["Email"], ["applicant@example.com"]]
        }
        return service

    def test_accepts_a_bare_tab_name(self) -> None:
        # "Round2" is a tab name, not cell A-column row 2.
        for tab in ("Grading Final", "Round2"):
            with self.subTest(tab=tab):
                header, rows = read_sheet_rows(
                    self._service_returning_rows(),
                    "sheet-id",
                    tab,
                )

                self.assertEqual(header, ["Email"])
                self.assertEqual(rows, [["applicant@example.com"]])

    def test_accepts_a_range_starting_at_row_one(self) -> None:
        header, _ = read_sheet_rows(
            self._service_returning_rows(),
            "sheet-id",
            "A1:Z",
        )

        self.assertEqual(header, ["Email"])

    def test_rejects_a_row_anchored_range_without_calling_sheets(self) -> None:
        service = MagicMock()

        with self.assertRaises(ValueError):
            read_sheet_rows(service, "sheet-id", "Sheet1!A2:Z")

        service.spreadsheets.assert_not_called()

    def test_rejects_a_row_only_range(self) -> None:
        with self.assertRaises(ValueError):
            read_sheet_rows(MagicMock(), "sheet-id", "Sheet1!2:5")


class EscalationWriteTests(unittest.TestCase):
    """A human-review escalation must not blank a grade the row already
    carries: re-grading and "grade this row only" both force a re-run, and
    a transient failure would otherwise wipe real scores off the sheet."""

    @staticmethod
    def _updates(service: MagicMock) -> list:
        return service.spreadsheets.return_value.values.return_value.update.call_args_list

    def test_reasoning_only_write_touches_no_other_cell(self) -> None:
        service = MagicMock()

        write_reasoning_only(
            service,
            "sheet-id",
            "Grading Final",
            7,
            {"score": 3, "reasoning": 4},
            "[NEEDS HUMAN REVIEW] failed",
        )

        updates = self._updates(service)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].kwargs["range"], "Grading Final!E7")
        self.assertEqual(
            updates[0].kwargs["body"],
            {"values": [["[NEEDS HUMAN REVIEW] failed"]]},
        )

    def test_multi_notes_only_write_touches_no_other_cell(self) -> None:
        service = MagicMock()

        write_multi_notes_only(
            service,
            "sheet-id",
            "AI",
            7,
            {"criteria": {"Problem & Solution": 3}, "total_score": 9, "notes": 10},
            "[NEEDS HUMAN REVIEW] failed",
        )

        updates = self._updates(service)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].kwargs["range"], "AI!K7")
        self.assertEqual(
            updates[0].kwargs["body"],
            {"values": [["[NEEDS HUMAN REVIEW] failed"]]},
        )


if __name__ == "__main__":
    unittest.main()

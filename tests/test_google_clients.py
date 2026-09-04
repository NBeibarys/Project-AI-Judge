import unittest

from src.google_clients import extract_drive_file_id
from src.video_ingestion import _is_google_slides_url


class DriveLinkHostTests(unittest.TestCase):
    """Drive IDs are only honoured on Google's own hosts: the ID patterns
    themselves match any host, and the service account would happily fetch
    a file it can read but the applicant cannot."""

    def test_rejects_drive_id_on_a_foreign_host(self) -> None:
        self.assertIsNone(
            extract_drive_file_id("https://evil.tld/d/1a2b3c4d5e6f7g8h9i")
        )

    def test_accepts_drive_file_link(self) -> None:
        self.assertEqual(
            extract_drive_file_id(
                "https://drive.google.com/file/d/1a2b3c4d5e6f7g8h9i/view"
            ),
            "1a2b3c4d5e6f7g8h9i",
        )

    def test_accepts_drive_open_link(self) -> None:
        self.assertEqual(
            extract_drive_file_id(
                "https://drive.google.com/open?id=1a2b3c4d5e6f7g8h9i"
            ),
            "1a2b3c4d5e6f7g8h9i",
        )


class SlidesUrlTests(unittest.TestCase):
    def test_rejects_slides_address_outside_the_host(self) -> None:
        self.assertFalse(
            _is_google_slides_url(
                "https://evil.tld/#docs.google.com/presentation/d/XYZ"
            )
        )

    def test_accepts_real_slides_url(self) -> None:
        self.assertTrue(
            _is_google_slides_url(
                "https://docs.google.com/presentation/d/1a2b3c4d/edit"
            )
        )


if __name__ == "__main__":
    unittest.main()

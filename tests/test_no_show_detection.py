import unittest

from src.pipeline import _is_no_show


class NoShowDetectionTests(unittest.TestCase):
    def test_no_response_variants_are_no_shows(self) -> None:
        for startup_name in (
            "Alpha no response",
            "Alpha noresponse",
            "Alpha no-response",
            "Alpha no_response",
            "Alpha NO.RESPONSE",
            "Alpha not pitching",
        ):
            with self.subTest(startup_name=startup_name):
                self.assertTrue(_is_no_show(startup_name))

    def test_normal_startup_name_is_not_a_no_show(self) -> None:
        self.assertFalse(_is_no_show("Alpha AI"))


if __name__ == "__main__":
    unittest.main()

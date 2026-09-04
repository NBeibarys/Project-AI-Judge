import os
import tempfile
import unittest
from unittest.mock import patch

from src.config import Config


def _base_env(service_account_path: str) -> dict:
    """The minimum env Config.from_env accepts for an R2B Vertex run."""
    return {
        "GOOGLE_SERVICE_ACCOUNT_PATH": service_account_path,
        "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
        "GOOGLE_CLOUD_PROJECT": "dummy-project",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "ANALYZER_MODEL": "gemini-dummy-analyzer",
        "GRADER_MODEL": "gemini-dummy-grader",
    }


class ConfigEnvTests(unittest.TestCase):
    """Set-but-empty env vars are how .env.example ships its placeholders,
    so they must read as unset rather than as an empty value."""

    def setUp(self) -> None:
        # A real file: from_env rejects a service-account path that is not
        # one, and no Google client is built here.
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        self.service_account_path = handle.name
        self.addCleanup(os.unlink, self.service_account_path)
        self.env = _base_env(self.service_account_path)

    def test_empty_header_row_var_falls_back_to_the_program_default(self) -> None:
        self.env["R2B_HEADER_ROW"] = ""

        with patch.dict(os.environ, self.env, clear=True):
            config = Config.from_env("r2b", sheet_id_override="x")

        self.assertEqual(config.header_row, 2)

    def test_empty_n_samples_falls_back_to_three(self) -> None:
        self.env["N_SAMPLES"] = ""

        with patch.dict(os.environ, self.env, clear=True):
            config = Config.from_env("r2b", sheet_id_override="x")

        self.assertEqual(config.n_samples, 3)

    def test_checkpoint_path_uses_the_normalized_program_name(self) -> None:
        # "R2B" and "r2b" must not produce two checkpoint files for one sheet.
        with patch.dict(os.environ, self.env, clear=True):
            config = Config.from_env("R2B", sheet_id_override="x")

        self.assertIn("checkpoint_r2b_", config.checkpoint_path)


if __name__ == "__main__":
    unittest.main()

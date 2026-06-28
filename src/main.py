"""CLI entry point for the checkpointed ADK batch review."""
import os
import sys

from .config import Config
from .pipeline import run_batch


def _load_dotenv(path: str = ".env"):
    """Load simple KEY=VALUE settings without overriding shell exports."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if value and key not in os.environ:
                os.environ[key] = value


def main():
    _load_dotenv()
    config = Config.from_env()
    print(f"Running ADK batch against sheet {config.sheet_id}")

    result = run_batch(config)
    graded = result["graded"]
    errors = result["errors"]

    print(f"Graded: {len(graded)} | Errors: {len(errors)}")
    for row_id, err in errors.items():
        print(f"  FAILED {row_id}: {err}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

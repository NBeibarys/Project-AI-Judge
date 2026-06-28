"""CLI entry point for the checkpointed ADK batch review."""
import sys

from dotenv import load_dotenv

from .config import Config
from .pipeline import run_batch


def main():
    # The maintained parser correctly handles quoting, escapes, and interpolation.
    load_dotenv(override=False)
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

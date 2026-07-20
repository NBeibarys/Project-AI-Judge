"""CLI entry point for the checkpointed ADK batch review."""
import os
import sys

from dotenv import load_dotenv

from .config import Config
from .pipeline import run_batch


def main():
    # The maintained parser correctly handles quoting, escapes, and interpolation.
    load_dotenv(override=False)
    # No fallback: an unset PROGRAM must fail loudly here, not silently grade
    # whichever program happened to be the old default (a real source of
    # confusion — an operator or reviewer expecting one program's sheet
    # while PROGRAM was actually unset/stale would get another program's
    # results with no signal anything was wrong).
    program = os.environ.get("PROGRAM", "")
    if not program:
        raise RuntimeError(
            "PROGRAM not set. Set PROGRAM to one of: fellowship_v2, r2b, "
            "alchemist — there is no default."
        )
    config = Config.from_env(program)
    print(f"Running ADK batch [program={program}] against sheet {config.sheet_id}")

    # FORCE=1 regrades every row even if the checkpoint marks it done.
    force = os.environ.get("FORCE", "").lower() in {"1", "true", "yes"}
    result = run_batch(config, force=force)
    graded = result["graded"]
    errors = result["errors"]

    print(f"Graded: {len(graded)} | Errors: {len(errors)} (force={force})")
    for row_id, err in errors.items():
        print(f"  FAILED {row_id}: {err}")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

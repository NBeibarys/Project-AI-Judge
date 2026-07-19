# AI Fellowship Agent — Project Rules

## CODING STANDARDS

Code as a senior data scientist. Every line you write should be production-quality.

- **Explanatory comments**: Explain WHY, not WHAT. Future readers (human or AI) should understand the reasoning behind non-obvious decisions.
- **Best state-of-art techniques**: Use official SDKs, type hints, dataclasses, Pydantic schemas. No deprecated APIs.
- **No redundancy**: Don't repeat logic. DRY. If a pattern appears 3 times, abstract it.
- **Prefer top-level imports**: Import at module top by default. A local `import X` inside a function is acceptable only when justified by a comment explaining why (e.g. avoiding an expensive or optional dependency on a code path that doesn't need it) — see `video_ingestion.py` for examples of this codebase's justified exceptions.
- **No hardcoded values**: Config-driven via env vars or dataclasses. Sheet IDs, model names, credentials all come from environment.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch specific exceptions, not bare `except:`. Log with context.
- **Testing**: Verify with `py_compile` before committing. Run actual tests, not just "it should work."

# AI Fellowship Agent — Project Rules

## CODING STANDARDS

Code as a senior data scientist. Every line you write should be production-quality.

- **Explanatory comments**: Explain WHY, not WHAT. Future readers (human or AI) should understand the reasoning behind non-obvious decisions.
- **Best state-of-art techniques**: Use official SDKs, type hints, dataclasses, Pydantic schemas. No deprecated APIs.
- **No redundancy**: Don't repeat logic. DRY. If a pattern appears 3 times, abstract it.
- **No local imports**: NEVER use `import X` inside a function. It causes `UnboundLocalError` in Python because the variable becomes local-scoped. Always import at module top.
- **No hardcoded values**: Config-driven via env vars or dataclasses. Sheet IDs, model names, credentials all come from environment.
- **Type hints**: All function signatures should have type hints.
- **Error handling**: Catch specific exceptions, not bare `except:`. Log with context.
- **Testing**: Verify with `py_compile` before committing. Run actual tests, not just "it should work."

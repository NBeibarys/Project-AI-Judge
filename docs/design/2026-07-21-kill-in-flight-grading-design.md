# Kill button for in-flight grading

**Date:** 2026-07-21

> **Status: shipped, with the completion loop redesigned during
> implementation.** This is the design as written on 2026-07-21, kept
> unedited as a record of the reasoning. Where the code and this document
> disagree, the code is correct.
>
> - **The completion loop does not use `as_completed()`** as Design 2
>   assumes. It cannot: `pool.shutdown(cancel_futures=True)` calls
>   `future.cancel()` directly on still-queued work items, which sets the
>   future to CANCELLED but never CANCELLED_AND_NOTIFIED, because only
>   `set_running_or_notify_cancel()` makes that transition and it is called
>   exclusively from inside a dispatched `_WorkItem.run()`. Both
>   `as_completed()` and `wait()` key off CANCELLED_AND_NOTIFIED, so no
>   waiter ever fires and the loop blocks forever. The shipped loop uses
>   `wait(..., timeout, FIRST_COMPLETED)` purely as a bounded "has anything
>   happened" signal and re-verifies each future with `.done()`, which does
>   recognise plain CANCELLED. See the long comment in `src/pipeline.py`
>   and the regression test
>   `tests/test_cancellation.py::RunBatchCancelWhileQueuedTests`.
> - **The cancellation handler catches one more exception than designed**:
>   `concurrent.futures.CancelledError`, raised by `future.result()`
>   itself for work items dropped by `cancel_futures=True` before they
>   ever started. The design listed only `RowCancelled` and
>   `asyncio.CancelledError`.
> - **Everything else shipped as designed**: the lock-guarded task registry
>   and `cancel_all_active()` using `loop.call_soon_threadsafe(task.cancel)`,
>   the `cancel_event` checked at five phase boundaries in `process_row`,
>   the background-thread plus 0.5s polling loop in `app.py`, and the
>   previously unbounded `_download_drive_file` gaining a per-chunk timeout.
> - **The test-count reference is stale.** The suite was 22 tests when this
>   was written and has grown since.

## Problem

Streamlit's built-in "Stop" control does nothing useful today. Confirmed against the installed Streamlit 1.58.0 source (`runtime/scriptrunner/script_runner.py`): Stop is purely cooperative — a pending stop request is only checked and raised (`StopException`, a `BaseException` subclass) when the script itself calls an `st.*` command (each such call enqueues a ForwardMsg, which is the only checked "yield point"). `run_batch()` (`src/pipeline.py`) makes zero `st.*` calls internally; the only yield point during a run is `_on_progress`'s `status_text.text(...)`, which fires once **per completed row**. For a single-row test grade (`target_row_number` set, `limit=1`), there are zero yield points before the row is already done — Stop is 100% inert. For a multi-row batch, Stop can only act *between* completed rows; anything already dispatched to the `ThreadPoolExecutor` keeps running to completion regardless (the `with` block's default `shutdown(wait=True)` blocks on exit anyway).

Separately: even where Gemini/ADK calls are concerned, there is no cancellation handle at all — `AdkReviewWorkflow.invoke()` drives the call via `loop.run_until_complete(coro)`, which never exposes the underlying `asyncio.Task`.

## Goal

Make the *existing* Streamlit Stop button actually interrupt an in-progress grading run — no new custom button — across all 3 programs (fellowship_v2, r2b, alchemist), reusing the shared `run_batch`/`process_row` pipeline and the one `app.py` dashboard. Report "API request killed, grading stopped" (or equivalent) back in the UI when it fires.

## Non-goals

- Instant interruption of blocking synchronous network calls (video/deck downloads, ffmpeg transcode, Drive API polling) mid-syscall — not achievable in pure Python without a process-based rewrite, which is disproportionate here (see brainstorm discussion). These are bounded by their own existing timeouts instead (see Fix below for the one unbounded case).
- A full process-based worker rewrite (`ProcessPoolExecutor`/subprocess-per-row).

## Design

### 1. Cancellation plumbing (`src/adk_agents/workflow.py`)

`AdkReviewWorkflow.invoke()` currently does `loop.run_until_complete(self._invoke_async(state))`. Change to:
- `task = loop.create_task(self._invoke_async(state))`
- Register `(loop, task)` in a module-level `dict` keyed by `row_id`, guarded by a `threading.Lock`, before calling `loop.run_until_complete(task)`; unregister in a `finally`.
- Add a module-level function `cancel_all_active()` that iterates the dict and calls `loop.call_soon_threadsafe(task.cancel)` for every entry — this is the near-instant kill path for any row currently inside a Gemini/ADK call.

`asyncio.CancelledError` is a `BaseException` — it will not be silently swallowed by existing `except Exception` handlers in `process_row`; it must propagate and be treated as "cancelled," not a scored failure, by `run_batch`'s completion handling.

### 2. Cooperative cancellation (`src/pipeline.py`)

- `process_row` and `run_batch` gain a `cancel_event: threading.Event | None = None` parameter (default `None` — behavior unchanged when absent, same pattern as `target_row_number`).
- `process_row` checks `cancel_event.is_set()` at the top and before each phase boundary (before pitch-deck ingestion, before video resolution, before `workflow.invoke`, before writing the result, before marking the checkpoint) and raises a small `RowCancelled` exception when set.
- `run_batch`'s `as_completed` loop treats both `RowCancelled` and `asyncio.CancelledError` as a deliberate skip (same `ok=None` bucket already used for no-shows), not a failure — no checkpoint entry, no sheet write for that row.
- `run_batch` no longer relies on `with ThreadPoolExecutor(...) as pool:`'s default blocking shutdown; on a kill signal it calls `pool.shutdown(wait=False, cancel_futures=True)` so not-yet-started futures are dropped immediately instead of still being launched.
- Bonus fix while touching this area: `video_ingestion.py`'s `_download_drive_file` currently has no timeout at all (the one genuinely-unbounded blocking call found in research) — give it a reasonable timeout matching the existing `FILES_API_POLL_TIMEOUT_SECONDS`-style constants in that file.

### 3. Making the native Stop button reachable (`app.py`)

Both the "Run grading" batch flow and the "Grade this row only" single-row flow currently call `run_batch(...)` directly on the script thread. Both move to:
- Launch `run_batch` on a `threading.Thread`, passing a `cancel_event` created fresh for this run.
- The script thread runs a short poll loop instead (e.g. every ~0.5s: read shared progress state, call `progress_bar.progress(...)`/`status_text.text(...)` — each call is a real `st.*` yield point) while the background thread is alive.
- Wrap the poll loop in `try/except StopException`: on catch, set `cancel_event`, call `workflow.cancel_all_active()`, join the background thread with a short bounded timeout, show "⛔ API request killed — grading stopped (N graded before cancellation)", then re-raise `StopException` so Streamlit's own stop UI state resolves normally.
- Progress/results need to cross the thread boundary safely — use a small shared mutable container (e.g. a dict guarded by a lock, or Streamlit's `session_state`) that the background thread updates and the poll loop reads; do not call `st.*` from the background thread itself (not the script thread — unsafe/unsupported).

### Testing

- Existing 22 unit tests must keep passing unchanged.
- Add a mocked test proving: (a) setting `cancel_event` before a row starts causes `process_row` to raise/return a cancelled result without calling `workflow.invoke` or writing to the sheet, (b) `cancel_all_active()` calls `task.cancel()` via `call_soon_threadsafe` for every registered task and clears the registry.
- `app.py` can only be verified via AST/syntax check plus careful manual review (Streamlit apps aren't unit-testable headlessly here), consistent with how the single-row feature was verified.

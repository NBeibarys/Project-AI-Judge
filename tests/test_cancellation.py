"""Kill-in-flight-grading: cooperative cancellation (src/pipeline.py) and
the asyncio-level cancel path (src/adk_agents/workflow.py).

See docs/superpowers/specs/2026-07-21-kill-in-flight-grading-design.md.
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from src.adk_agents import workflow as workflow_module
from src.pipeline import RowCancelled, process_row, run_batch


class ProcessRowCancellationTests(unittest.TestCase):
    def test_cancel_event_set_before_start_raises_without_invoking_workflow(self) -> None:
        cancel_event = threading.Event()
        cancel_event.set()  # already cancelled before process_row is even called

        workflow = MagicMock()
        checkpoint = MagicMock()
        config = MagicMock()

        with self.assertRaises(RowCancelled):
            process_row(
                config,
                workflow,
                header=["Startup Name", "Email"],
                row=["Acme", "a@example.com"],
                sheet_row_number=2,
                checkpoint=checkpoint,
                cancel_event=cancel_event,
            )

        # The whole point: a row cancelled before it starts must never
        # reach the (expensive) Gemini/ADK call, and never touch the
        # checkpoint or sheet-write path (checkpoint.is_done would be the
        # first thing process_row normally does after deriving row_id).
        workflow.invoke.assert_not_called()
        checkpoint.is_done.assert_not_called()
        checkpoint.mark_done.assert_not_called()

    def test_cancel_event_none_does_not_raise_row_cancelled(self) -> None:
        # Default behavior (no cancel_event passed) must be completely
        # unaffected — this is what every existing caller (main.py, the
        # pre-existing app.py flows) relies on.
        cancel_event = threading.Event()
        # Not set — process_row should proceed past the cancellation check
        # (it will still fail/short-circuit for other legitimate reasons
        # given all-mock inputs, but must NOT raise RowCancelled).
        try:
            process_row(
                MagicMock(program_config=MagicMock(
                    requires_pitch_deck=False,
                    source_priority="video_primary",
                    criterion_column_names=None,
                )),
                MagicMock(),
                header=["Startup Name"],
                row=["Acme"],
                sheet_row_number=2,
                checkpoint=MagicMock(is_done=MagicMock(return_value=False)),
                cancel_event=cancel_event,
            )
        except RowCancelled:
            self.fail("RowCancelled raised even though cancel_event was never set")
        except Exception:
            # Any other exception is fine/expected given fully-mocked
            # config internals — this test only asserts RowCancelled
            # specifically does NOT fire.
            pass


class CancelAllActiveTests(unittest.TestCase):
    def setUp(self) -> None:
        # Isolate from any other test/run's state — cancel_all_active
        # mutates a module-level dict.
        with workflow_module._active_lock:
            workflow_module._active_tasks.clear()

    def tearDown(self) -> None:
        with workflow_module._active_lock:
            workflow_module._active_tasks.clear()

    def test_cancel_all_active_cancels_every_registered_task_and_clears_registry(self) -> None:
        loop1, task1 = MagicMock(), MagicMock()
        loop2, task2 = MagicMock(), MagicMock()
        with workflow_module._active_lock:
            workflow_module._active_tasks["row_a"] = (loop1, task1)
            workflow_module._active_tasks["row_b"] = (loop2, task2)

        workflow_module.cancel_all_active()

        # Each row's task must be cancelled via call_soon_threadsafe on its
        # OWN loop (task.cancel() is not safe to call directly from another
        # thread) — not via any other mechanism.
        loop1.call_soon_threadsafe.assert_called_once_with(task1.cancel)
        loop2.call_soon_threadsafe.assert_called_once_with(task2.cancel)
        task1.cancel.assert_not_called()  # only scheduled, never invoked directly here
        task2.cancel.assert_not_called()

        with workflow_module._active_lock:
            self.assertEqual(workflow_module._active_tasks, {})

    def test_cancel_all_active_survives_a_closed_loop(self) -> None:
        # A worker thread that already exited can leave a closed loop in the
        # registry; cancelling it raises RuntimeError, which must not stop
        # the sweep from reaching the rows that are still running.
        closed_loop, closed_task = MagicMock(), MagicMock()
        closed_loop.call_soon_threadsafe.side_effect = RuntimeError(
            "Event loop is closed"
        )
        live_loop, live_task = MagicMock(), MagicMock()
        with workflow_module._active_lock:
            workflow_module._active_tasks["row_closed"] = (closed_loop, closed_task)
            workflow_module._active_tasks["row_live"] = (live_loop, live_task)

        workflow_module.cancel_all_active()

        live_loop.call_soon_threadsafe.assert_called_once_with(live_task.cancel)
        with workflow_module._active_lock:
            self.assertEqual(workflow_module._active_tasks, {})

    def test_cancel_all_active_is_a_no_op_when_nothing_registered(self) -> None:
        # Should not raise even with an empty registry.
        workflow_module.cancel_all_active()
        with workflow_module._active_lock:
            self.assertEqual(workflow_module._active_tasks, {})


class RunBatchCancelWhileQueuedTests(unittest.TestCase):
    """Regression test for the kill-in-flight-grading hang: run_batch's
    completion loop used to iterate `as_completed(futures)` even after
    calling `pool.shutdown(wait=False, cancel_futures=True)`. A future
    still QUEUED (not yet handed to a worker thread) at that moment gets
    `.cancel()`ed directly by the executor's own shutdown() code, which
    (per concurrent.futures/_base.py) sets Future state to CANCELLED but
    never CANCELLED_AND_NOTIFIED — only set_running_or_notify_cancel(),
    called exclusively from inside a dispatched _WorkItem.run(), makes
    that further transition and wakes any as_completed()/wait() waiter.
    A future cancelled straight off the queue never reaches run(), so
    as_completed() can never yield it, and its `while pending:` loop
    blocks forever. This test submits more rows than max_concurrency,
    triggers cancel_event mid-run from a separate thread, and asserts
    run_batch actually returns instead of hanging.
    """

    def test_cancel_mid_run_returns_instead_of_hanging(self) -> None:
        header = ["Email"]
        n_rows = 8
        max_concurrency = 2
        row_sleep_seconds = 0.4
        rows = [[f"applicant{i}@example.com"] for i in range(n_rows)]

        config = MagicMock()
        config.service_account_path = "dummy-service-account.json"
        config.checkpoint_path = "dummy-checkpoint.json"
        config.sheet_id = "dummy-sheet-id"
        config.sheet_range = "Sheet1!A1:Z"
        config.header_row = 1
        config.top_label_row = 0
        config.max_concurrency = max_concurrency
        config.program_config.criterion_column_names = None
        config.program_config.score_column_name = "Score"
        config.program_config.reasoning_column_name = "Reasoning"

        def fake_process_row(
            config, workflow, header, row, sheet_row_number, checkpoint,
            force=False, duplicate_emails=None, cancel_event=None,
        ):
            # Simulates a slow (e.g. Gemini API) row that does NOT itself
            # check cancel_event — the point of this test is the executor
            # dropping rows that never even got dispatched to a worker,
            # not process_row's own cooperative cancellation (covered by
            # ProcessRowCancellationTests above).
            time.sleep(row_sleep_seconds)
            return row[0], {"score": 5, "reasoning": "ok", "human_review_flag": False}

        checkpoint_instance = MagicMock()
        checkpoint_instance.is_done.return_value = False
        checkpoint_instance.mark_failed.return_value = 1

        progress_calls = []

        def on_progress(done, total, row_id, ok):
            progress_calls.append((done, total, row_id, ok))

        cancel_event = threading.Event()
        outcome = {}

        with patch("src.pipeline.get_sheets_service", return_value=MagicMock()), \
             patch("src.pipeline.read_sheet_rows", return_value=(header, rows)), \
             patch("src.pipeline.Checkpoint", return_value=checkpoint_instance), \
             patch("src.pipeline._build_workflow", return_value=MagicMock()), \
             patch("src.pipeline.resolve_output_columns", return_value={}), \
             patch("src.pipeline.write_row_result", return_value=None), \
             patch("src.pipeline.process_row", side_effect=fake_process_row), \
             patch("src.pipeline.CANCEL_CHECK_INTERVAL_SECONDS", 0.02):
            # A short poll interval here (vs. the real 0.5s default) makes
            # this test deterministic: the completion loop re-checks
            # cancel_event roughly every 0.02s, so shutdown(cancel_futures=
            # True) fires well before t=0.1+row_sleep_seconds — i.e. before
            # either already-dispatched row could finish and let a worker
            # thread grab a third one — pinning the "already dispatched
            # vs. still queued" split to exactly max_concurrency instead of
            # leaving it to scheduling variance.

            def run():
                try:
                    outcome["result"] = run_batch(config, on_progress=on_progress, cancel_event=cancel_event)
                except Exception as exc:  # noqa: BLE001 — surfaced via assertion below
                    outcome["exception"] = exc

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            # Fires well before any row (0.4s sleep) can complete, so
            # every not-yet-dispatched row is still sitting in the
            # executor's queue when cancel_event is observed —
            # guaranteeing the cancelled-while-queued path this test
            # targets actually gets exercised.
            time.sleep(0.1)
            cancel_event.set()
            worker.join(timeout=8)

            self.assertFalse(
                worker.is_alive(),
                "run_batch did not return within the bounded timeout — this is "
                "the as_completed()/cancel_futures hang regression.",
            )

        self.assertNotIn("exception", outcome, f"run_batch raised: {outcome.get('exception')}")
        self.assertIn("result", outcome)

        # Every submitted row must get exactly one on_progress call, whether
        # it completed normally (the max_concurrency=2 rows already
        # dispatched before the shutdown) or was dropped cancelled-while-
        # queued (everything else) — none should simply vanish into a hang.
        self.assertEqual(len(progress_calls), n_rows)
        ok_values = [ok for _, _, _, ok in progress_calls]
        # The two rows already handed to a worker thread before shutdown
        # ran complete normally (ok=True); everything still queued at that
        # moment is dropped as a deliberate skip (ok=None), never scored
        # as a failure.
        self.assertEqual(ok_values.count(True), max_concurrency)
        self.assertEqual(ok_values.count(None), n_rows - max_concurrency)
        self.assertEqual(ok_values.count(False), 0)


if __name__ == "__main__":
    unittest.main()

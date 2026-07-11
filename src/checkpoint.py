"""
Row-level resumable checkpoint — flat JSON keyed by row_id.
Deliberately not LangGraph's own checkpointer: each row's graph run is
a few seconds end-to-end, so there's no value in resuming mid-graph.
What matters at 100+ rows is skipping rows already graded on rerun
(after a crash, rate-limit, or manual interrupt).
"""
import json
import os
import threading
import hashlib


class Checkpoint:
    @staticmethod
    def _key(row_id: str) -> str:
        """Pseudonymize applicant identifiers before writing local state."""
        return hashlib.sha256(row_id.encode("utf-8")).hexdigest()

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()  # writes happen from a ThreadPoolExecutor
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _flush(self):
        # Write-to-temp-then-replace so a crash mid-write never corrupts
        # the checkpoint file an earlier successful run depended on.
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp_path, self.path)

    def is_done(self, row_id: str) -> bool:
        with self._lock:
            entry = self._data.get(self._key(row_id))
            return bool(entry and entry.get("status") in ("done", "human_review"))

    def mark_done(self, row_id: str, human_review_flag: bool):
        with self._lock:
            # Resumption needs only routing state; grades remain in Sheets.
            self._data[self._key(row_id)] = {
                "status": "human_review" if human_review_flag else "done",
            }
            self._flush()

    def mark_failed(self, row_id: str, error: str) -> int:
        # Failed rows are NOT treated as done — rerunning the batch
        # should retry them, since the failure is usually transient
        # (download timeout, API rate limit) rather than a content issue.
        # attempts accumulates across calls (unlike status/error, which are
        # just the latest) so a caller can tell a genuinely transient blip
        # apart from a row that fails the same way every single run (e.g.
        # a structurally broken source URL, or a file too large to ever
        # process within quota) and escalate instead of retrying forever.
        with self._lock:
            key = self._key(row_id)
            attempts = self._data.get(key, {}).get("attempts", 0) + 1
            self._data[key] = {"status": "failed", "error": error, "attempts": attempts}
            self._flush()
            return attempts

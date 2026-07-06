"""Minimal HTTP server that wraps the batch CLI for Cloud Run.

Cloud Run requires a container that listens on a port. This module exposes:
  GET  /health  -> 200 "ok"   (liveness/readiness, no model calls)
  POST /run     -> 200 JSON   (triggers one batch run via src.main.main)

The batch is synchronous: a request blocks until the sheet is fully
processed. Cloud Run's request timeout (configurable, default 15m) must be
set high enough for a 100+ row run. Concurrency is set to 1 at deploy time
so two triggers never race on the same checkpoint file.
"""
import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv

load_dotenv(override=False)

# Single-flight guard: the batch writes to a shared checkpoint file, so two
# concurrent runs would corrupt it. Cloud Run concurrency=1 makes this
# belt-and-suspenders rather than load-bearing.
_RUN_LOCK = threading.Lock()
_RUN_IN_PROGRESS = {"value": False}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, content_type: str = "application/json"):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, json.dumps({"status": "ok", "running": _RUN_IN_PROGRESS["value"]}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/run":
            self._send(404, json.dumps({"error": "not found"}))
            return

        if not _RUN_LOCK.acquire(blocking=False):
            self._send(409, json.dumps({"error": "a batch is already running"}))
            return

        _RUN_IN_PROGRESS["value"] = True
        try:
            # Import lazily so /health stays cheap and startup is fast.
            from .main import main as batch_main

            try:
                batch_main()
            except SystemExit as exc:
                # main() calls sys.exit(1) on row errors; surface as a 500
                # with a structured body rather than letting the process die
                # (which would crash the server and drop the response).
                code = exc.code if isinstance(exc.code, int) else 1
                if code == 0:
                    self._send(200, json.dumps({"status": "ok"}))
                else:
                    self._send(
                        500,
                        json.dumps({"status": "error", "exit_code": code}),
                    )
                return
            self._send(200, json.dumps({"status": "ok"}))
        except Exception:
            self._send(
                500,
                json.dumps(
                    {
                        "status": "error",
                        "error": traceback.format_exc(),
                    }
                ),
            )
        finally:
            _RUN_IN_PROGRESS["value"] = False
            _RUN_LOCK.release()

    def log_message(self, format, *args):  # noqa: A002 — matches stdlib signature
        # Cloud Run captures stdout/stderr; suppress the default stderr
        # access log to avoid double-logging.
        pass


def main():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

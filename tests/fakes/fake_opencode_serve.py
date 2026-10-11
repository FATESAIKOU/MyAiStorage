#!/usr/bin/env python3
"""Fake `opencode serve` for OpenCodeDriver tests (task 3.2).

Speaks just the three HTTP calls the driver makes:

  GET  /session/status           -> the JSON in $FAKE_HOME/fake-oc-status.json
                                    ({} when missing)
  POST /session                  -> {"id": $FAKE_OC_SESSION or a default}
  POST /session/<id>/message     -> sleeps $FAKE_OC_ROUND_SECONDS, then {"ok": true}
  POST /session/<id>/abort       -> {}

Every request is appended to $FAKE_HOME/fake-oc-calls.log (JSON per line).
The argument/env knobs: FAKE_OC_NO_LISTEN makes it never bind the port;
FAKE_OC_LISTEN_DELAY sleeps before binding.  SIGTERM exits 0.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

argv = sys.argv[1:]
home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-oc-serve"))
home.mkdir(parents=True, exist_ok=True)
with open(home / "fake-oc-args.log", "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
names = sorted(
    key for key in os.environ
    if key.startswith(("ANTHROPIC_", "OPENAI_", "OPENCODE_", "FAKE_", "AGORA_"))
)
with open(home / "fake-oc-env-names.log", "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "env": names}) + "\n")

calls = home / "fake-oc-calls.log"
status_file = home / "fake-oc-status.json"


def log_call(entry: dict) -> None:
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-opencode"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        log_call({"method": "GET", "path": self.path})
        if self.path.rstrip("/").endswith("/session/status"):
            try:
                payload = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            self._json(200, payload)
            return
        self._json(404, {})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        path = self.path.rstrip("/")
        log_call({"method": "POST", "path": self.path, "body": body})
        if path.endswith("/session"):
            self._json(200, {"id": os.environ.get("FAKE_OC_SESSION", "ses_createdfake00001")})
        elif path.endswith("/message"):
            time.sleep(float(os.environ.get("FAKE_OC_ROUND_SECONDS", "0.05")))
            self._json(200, {"ok": True})
        elif path.endswith("/abort"):
            self._json(200, {})
        else:
            self._json(404, {})


if os.environ.get("FAKE_OC_NO_LISTEN"):
    while True:  # never bind the port; the driver must give up on its own
        time.sleep(0.2)

if os.environ.get("FAKE_OC_LISTEN_DELAY"):
    time.sleep(float(os.environ["FAKE_OC_LISTEN_DELAY"]))

port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
server.serve_forever()

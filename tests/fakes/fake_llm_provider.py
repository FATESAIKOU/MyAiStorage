"""A loopback fake LLM provider for the real-binary env checks (task 3.6.4).

It records only *booleans* per request - whether the credential it saw equals
the subscription key, and whether the parent process's key leaked - never the
values themselves.  `messages` answers an Anthropic-shaped reply (for Claude
Code), anything else an OpenAI-shaped chat completion (for opencode).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-llm"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _record(self) -> None:
        server: FakeProvider = self.server.provider  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        seen = []
        if self.headers.get("x-api-key"):
            seen.append(self.headers["x-api-key"])
        authorization = self.headers.get("Authorization") or ""
        if authorization.startswith("Bearer "):
            seen.append(authorization[len("Bearer "):])
        with server.lock:
            server.requests.append({
                "path": self.path,
                "x_api_key": bool(self.headers.get("x-api-key")),
                "authorization": bool(authorization),
                # booleans only: never the values
                "saw_subscription_key": server.subscription_key in seen,
                "saw_parent_key": bool(server.parent_key) and server.parent_key in seen,
            })
            server.event.set()

    def do_POST(self) -> None:  # noqa: N802
        self._record()
        if "messages" in self.path:
            payload = {
                "id": "msg_fake", "type": "message", "role": "assistant",
                "model": "fake-model",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        else:
            payload = {
                "id": "chatcmpl-fake", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeProvider:
    """One loopback provider; `wait()` blocks until a request arrives."""

    def __init__(self, subscription_key: str, parent_key: str | None) -> None:
        self.subscription_key = subscription_key
        self.parent_key = parent_key
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.provider = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def wait(self, timeout: float = 60.0) -> dict[str, Any]:
        if not self.event.wait(timeout):
            raise AssertionError(f"the provider was never called; got {self.requests!r}")
        with self.lock:
            return self.requests[-1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

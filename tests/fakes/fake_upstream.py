#!/usr/bin/env python3
"""A local, offline stand-in for a model provider (change agora-opencode-v2, 1.1).

opencode talks to models through OpenAI-compatible chat-completions endpoints, so
this is a few hundred lines of `http.server` that answer exactly that call: no
network beyond 127.0.0.1, no credentials, no model, and a deterministic reply - one
sentence, and a tool call when the test wants the shape of a tool turn.

Why it exists: every opencode-facing test and the v2 fixture used to need a real
(free, rate-limited) model, and a skip is not a red. With this, "the reading version
shows the tool line" and "the import carries the tool call" are testable on any
machine, offline, including the break-it checks that must fail loudly rather than
skip (review M6).

Usage
-----
As a library (tests):

    from fake_upstream import start
    upstream = start()                 # an ephemeral port on 127.0.0.1
    config = {"providers": {"fakeup": {... baseURL: upstream.url ...}}}

As a process (fixture generation, acceptance):

    python3 tests/fakes/fake_upstream.py --port 0 --config /tmp/xdg/opencode/opencode.json
    # prints the port it got on stdout, then serves until SIGTERM

Environment:
    FAKE_UPSTREAM_MODE       tool-then-text (default) | text | tool
                             `tool-then-text` calls the tool first, then answers a
                             sentence once opencode sends the tool's result back
    FAKE_UPSTREAM_TEXT       the sentence to answer (default below)
    FAKE_UPSTREAM_TOOL       tool name to call (default "read")
    FAKE_UPSTREAM_TOOL_ARGS  JSON arguments for that call (default: read the file
                             `fake-note.txt` in the project directory)
    FAKE_UPSTREAM_LOG        append one JSON line per request (default
                             $FAKE_HOME/upstream-calls.log, else $HOME/upstream-calls.log)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

#: Deliberately not a real model's wording, and fixed: tests assert on it.
DEFAULT_TEXT = "ZZFAKEUP 這是一句話的回答。"
DEFAULT_TOOL = "read"
DEFAULT_TOOL_ARGS = '{"path": "fake-note.txt"}'

#: The provider name and model id tests put in opencode's config and pass to `-m`.
PROVIDER = "fakeup"
MODEL = "fake-model"


def opencode_config(base_url: str, provider: str = PROVIDER, model: str = MODEL) -> dict:
    """The opencode 2.x config that points `provider` at `base_url`.

    v2 spells this `providers` / `package` / `settings`; v1's `provider` / `npm` /
    `options` is migrated on load, but the fixture should be written the v2 way.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "providers": {
            provider: {
                "name": "Fake upstream",
                "package": "aisdk:@ai-sdk/openai-compatible",
                "settings": {"baseURL": base_url, "apiKey": "fake-upstream"},
                "models": {model: {"name": "Fake Model"}},
            }
        },
    }


def write_config(path: str | Path, base_url: str, **kwargs) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(opencode_config(base_url, **kwargs), indent=2) + "\n",
                      encoding="utf-8")
    return target


def _log_path() -> Path:
    explicit = os.environ.get("FAKE_UPSTREAM_LOG")
    if explicit:
        return Path(explicit)
    home = os.environ.get("FAKE_HOME") or os.environ.get("HOME") or "."
    return Path(home) / "upstream-calls.log"


def _mode() -> str:
    mode = os.environ.get("FAKE_UPSTREAM_MODE", "tool-then-text")
    if mode not in ("tool-then-text", "text", "tool"):
        raise SystemExit(f"[fake_upstream] 不認得的 FAKE_UPSTREAM_MODE：{mode}")
    return mode


def _is_tool_result(messages: list) -> bool:
    """Whether opencode has already run our tool call and sent its result back."""
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "tool-result"
                for item in content):
            return True
    return False


def _sse(handler: BaseHTTPRequestHandler, chunks: list[dict], model: str) -> None:
    """One SSE stream in the shape the AI SDK's openai-compatible provider reads."""
    created = int(time.time())
    for n, chunk in enumerate(chunks):
        if n == 0:
            delta = {"role": "assistant", **chunk.pop("delta", {})} if "delta" in chunk else \
                {"role": "assistant"}
            chunk = {"delta": delta, **chunk}
        event = {"id": "chatcmpl-fakeup", "object": "chat.completion.chunk",
                 "created": created, "model": model, "choices": [chunk]}
        handler.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def _completion(chunks: list[dict], model: str) -> dict:
    created = int(time.time())
    message: dict = {"role": "assistant"}
    for chunk in chunks:
        delta = chunk.get("delta") or {}
        for key, value in delta.items():
            if key == "tool_calls":
                message.setdefault("tool_calls", []).extend(value)
            elif key == "content":
                message["content"] = (message.get("content") or "") + value
    return {"id": "chatcmpl-fakeup", "object": "chat.completion", "created": created,
            "model": model,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeUpstream/1"

    def log_message(self, fmt, *args):   # quiet: the calls log is the record
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {"raw": body.decode("utf-8", "replace")}
        with open(self.server.log, "a") as f:
            f.write(json.dumps({"path": self.path, "request": request},
                               ensure_ascii=False) + "\n")
        if not self.path.rstrip("/").endswith("chat/completions"):
            self.send_error(404, "only /chat/completions is faked")
            return
        self._answer(request)

    def _answer(self, request: dict) -> None:
        model = request.get("model") or MODEL
        messages = request.get("messages") or []
        mode = self.server.mode
        want_tool = mode == "tool" or (mode == "tool-then-text"
                                       and not _is_tool_result(messages))
        chunks: list[dict]
        if want_tool:
            chunks = [{"delta": {"tool_calls": [{
                "index": 0, "id": "call_fakeup_1", "type": "function",
                "function": {"name": self.server.tool,
                             "arguments": self.server.tool_args}}]},
                "finish_reason": None},
                {"delta": {}, "finish_reason": "tool_calls"}]
        else:
            chunks = [{"delta": {"content": self.server.text}, "finish_reason": None},
                      {"delta": {}, "finish_reason": "stop"}]
        if request.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            _sse(self, chunks, model)
        else:
            payload = json.dumps(_completion(chunks, model), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health"):
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"fake upstream\n")
            return
        self.send_error(404)


def start(port: int = 0, *, text: str | None = None, tool: str | None = None,
          tool_args: str | None = None, mode: str | None = None,
          log: str | Path | None = None) -> SimpleNamespace:
    """Serve on 127.0.0.1:`port` (0 = an ephemeral one) until `stop()`.

    Returns an object with `port`, `url`, `log` and `stop()`.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.mode = mode or _mode()
    server.text = text or os.environ.get("FAKE_UPSTREAM_TEXT", DEFAULT_TEXT)
    server.tool = tool or os.environ.get("FAKE_UPSTREAM_TOOL", DEFAULT_TOOL)
    server.tool_args = tool_args or os.environ.get("FAKE_UPSTREAM_TOOL_ARGS", DEFAULT_TOOL_ARGS)
    server.log = str(log or _log_path())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return SimpleNamespace(port=server.server_address[1],
                            url=f"http://127.0.0.1:{server.server_address[1]}",
                            log=server.log,
                            calls=lambda: [json.loads(line) for line in
                                           Path(server.log).read_text().splitlines()],
                            stop=lambda: (server.shutdown(), server.server_close(),
                                          thread.join(timeout=5)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local fake model provider")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--config", help="write the opencode config pointing here to this path")
    parser.add_argument("--provider", default=PROVIDER)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)

    upstream = start(args.port)
    if args.config:
        write_config(args.config, upstream.url, provider=args.provider, model=args.model)
    print(f"{upstream.port}", flush=True)

    def _stop(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())

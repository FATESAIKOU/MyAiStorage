"""The real `lgw.client.Client` over a loopback HTTP server (review H1).

This is the test that keeps `LLMGatewaySource` honest about lgw's actual
`Result` shape.  It skips unless the `gateway` extra is installed, and it
never leaves the machine: a stdlib HTTP server on 127.0.0.1 answers the
registry endpoints, and the connect step is a stub that yields that address
(no SSH).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sidecar.protocols import SourceError
from sidecar.sources import LLMGatewaySource

lgw_connect = pytest.importorskip("lgw.connect")
lgw_client = pytest.importorskip("lgw.client", reason="the gateway extra is not installed")

ROW = {
    "schema": 1,
    "id": "claude-personal",
    "provider": "anthropic",
    "kind": "claude-subscription",
    "agents": ["claude-code"],
    "priority": 10,
    "models": {"allow": ["*"], "default": None},
    "endpoint": None,
    "cooldown": {"5h": None, "1w": None},
    "available": True,
}
NO_KEY_ROW = {k: v for k, v in ROW.items() if k != "key"}


class _Handler(BaseHTTPRequestHandler):
    """Answers the registry endpoints; the paths only need their suffixes.

    list: any GET ending in `subscriptions`; get: any other GET (the row,
    flat, with key); cooldown: any POST ending in `cooldown` -> 204.  A path
    naming `denied` or `agent=opencode` answers 403 with lgw's error code.
    """

    server_version = "fake-lgw"

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
        path = self.path.split("?", 1)[0].rstrip("/")
        if "denied" in path or "agent=opencode" in self.path:
            self._json(403, {"error": "agent_not_allowed"})
        elif path.endswith("subscriptions"):
            self._json(200, {"schema": 1, "subscriptions": [NO_KEY_ROW]})
        else:
            self._json(200, dict(ROW, key="loopback-secret"))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path.split("?", 1)[0].rstrip("/").endswith("cooldown"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(200, {})


class _LoopbackConnect:
    """lgw.connect-shaped stub: no SSH, just yields the loopback URL."""

    ConfigError = lgw_connect.ConfigError

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def load_config(self, path):
        return {"schema": 1}

    @contextmanager
    def connect(self, cfg):
        yield self.base_url


@pytest.fixture
def loopback(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connect_file = tmp_path / "connect.json"
        connect_file.write_text(json.dumps({"schema": 1}), encoding="utf-8")
        base = f"http://127.0.0.1:{server.server_address[1]}"
        yield LLMGatewaySource(
            connect_file,
            connect=_LoopbackConnect(base),
            client_factory=lgw_client.Client,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_lgw_client_shapes(loopback):
    rows = loopback.list()
    assert isinstance(rows, list) and [row.id for row in rows] == ["claude-personal"]
    assert rows[0].key is None

    sub = loopback.get("claude-personal", "claude-code")
    assert sub.key == "loopback-secret"  # get's data is the flat row

    assert loopback.cooldown("claude-personal", "5h", None) is None  # 204 = ok


def test_real_lgw_client_refusal_maps_to_source_error(loopback):
    assert loopback.get("denied", "claude-code") == SourceError("agent_not_allowed")

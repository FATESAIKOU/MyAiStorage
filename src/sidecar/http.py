"""The HTTP control plane (design D1/D8): /agents and /jobs, loopback only.

Four actions over /agents plus the three job forms over /jobs; no
authentication (the user's choice, Q5).  Health is GET /agents answering.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from .jobs import parse_duration, parse_time
from .protocols import (
    BadRequestError,
    JobNotFoundError,
    NotFoundError,
    PayloadTooLargeError,
    SidecarError,
)

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
MAX_BODY_BYTES = 1024 * 1024  # request bodies over 1 MB are a 413 (L4)


class ControlServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], core: Any) -> None:
        super().__init__(address, ControlHandler)
        self.core = core


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "sidecar"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # --- dispatch ---------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
        except SidecarError as e:
            self._send_json(e.status, {"error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # never kill a thread silently
            logger.exception("unhandled control-plane error")
            self._send_json(
                500, {"error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}}
            )
        else:
            self._send_json(status, payload)

    def _route(self, method: str) -> tuple[int, Any]:
        core = self.server.core
        path = urlparse(self.path).path
        parts = [unquote(p) for p in path.split("/") if p]

        if parts == ["agents"]:
            if method == "GET":
                return 200, {"agents": [rec.to_dict() for rec in core.list_agents()]}
            if method == "POST":
                body = self._read_json()
                rec = core.open(
                    id=body.get("id"),
                    agent=body.get("agent"),
                    workdir=body.get("workdir"),
                    session=body.get("session"),
                    subscription=body.get("subscription"),
                    input=body.get("input"),
                )
                return 201, rec.to_dict()
            raise NotFoundError("no such route")

        if len(parts) == 2 and parts[0] == "agents" and method == "GET":
            return 200, core.get(parts[1]).to_dict()

        if len(parts) == 3 and parts[0] == "agents" and method == "POST":
            if parts[2] == "close":
                return 200, core.close(parts[1]).to_dict()
            if parts[2] == "poke":
                body = self._read_json()
                rec = core.poke(
                    parts[1], body.get("text"), bool(body.get("interrupt", False))
                )
                return 200, rec.to_dict()
            raise NotFoundError("no such route")

        if parts == ["jobs"]:
            if method == "GET":
                return 200, {"jobs": [job.to_dict() for job in core.list_jobs()]}
            if method == "POST":
                body = self._read_json()
                job = core.add_job(
                    body.get("action"),
                    body.get("args", {}),
                    at=parse_time(body["at"]) if body.get("at") is not None else None,
                    every=parse_duration(body["every"]) if body.get("every") is not None else None,
                    until=parse_time(body["until"]) if body.get("until") is not None else None,
                )
                return 201, job.to_dict()
            raise NotFoundError("no such route")

        if len(parts) == 2 and parts[0] == "jobs" and method == "DELETE":
            if not core.delete_job(parts[1]):
                raise JobNotFoundError(f"unknown job {parts[1]!r}")
            return 204, None

        raise NotFoundError("no such route")

    # --- plumbing ---------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError as e:
            raise BadRequestError(f"bad Content-Length: {raw_length!r}") from e
        if length > MAX_BODY_BYTES:
            # Reject by the declared size, before pulling the body in (L4).
            raise PayloadTooLargeError(
                f"request body is larger than {MAX_BODY_BYTES} bytes"
            )
        try:
            raw = self.rfile.read(length)
        except (ValueError, OSError) as e:
            raise BadRequestError(f"cannot read the request body: {e}") from e
        if not raw:
            raise BadRequestError("request body must be a JSON object")
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise BadRequestError(f"request body is not JSON: {e}") from e
        if not isinstance(body, dict):
            raise BadRequestError("request body must be a JSON object")
        return body

    def _send_json(self, status: int, payload: Any) -> None:
        if payload is None:
            data = b""
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s %s", self.address_string(), fmt % args)


def make_server(core: Any, host: str = HOST, port: int = 8471) -> ControlServer:
    """Bind the control plane; the host defaults to loopback (design D1)."""
    return ControlServer((host, port), core)

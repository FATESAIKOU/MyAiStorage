"""Fakes for the aicomm tests: a loopback fake sidecar, a sidecar-link dir, fake `angareion`.

aicomm talks to the `angareion` command and to a sidecar over loopback HTTP; when the local
sidecar does not know a recipient it also reads the sidecar-link directory (`heartbeat` and
`ports`). All of that is replaced here, so a test never reaches the real channel, the real
sidecar, or the real `~/.mylinuxpool/sidecar-link`. `run_aicomm` refuses to run unless
`AICOMM_SIDECAR` is set and the sidecar-link directory is one a test owns: either
`AICOMM_SIDECAR_LINK` points somewhere, or the default directory under HOME (which the
`isolated_home` fixture points at a tmp dir) does not exist. So a test that forgets its
fakes fails instead of knocking on the real sidecar or reading a real link.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import io
import json
import os
import re
import shutil
import socket
import stat
import sys
import threading
import time
import urllib.parse
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SENDER = "demo-impl-t1-1"
FAKE_ID = "01FAKEMSG0000000000000000A"
WAKE_TEXT = "有新訊息：請執行 aicomm inbox。"

_FAKE_ANGAREION = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_ANG_LOG"], "a", encoding="utf-8") as f:
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin}}) + "\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "send":
    raw = os.environ.get("FAKE_ANG_SEND_RAW")  # stdout verbatim (JSON string): no added newline
    out = os.environ.get("FAKE_ANG_SEND_OUT", "{fake_id}")
    if raw is not None:
        sys.stdout.write(json.loads(raw))
    elif out:
        print(out)
    sys.exit(int(os.environ.get("FAKE_ANG_SEND_EXIT", "0")))
if cmd == "wait":
    sys.exit(int(os.environ.get("FAKE_ANG_WAIT_EXIT", "0")))
if cmd == "read":
    sys.stdout.write(os.environ.get("FAKE_ANG_READ_OUT", ""))
    sys.exit(int(os.environ.get("FAKE_ANG_READ_EXIT", "0")))
if cmd == "log":
    if "--bogus" in sys.argv[1:]:
        sys.stderr.write("angareion: error: unrecognized arguments: --bogus\\n")
        sys.exit(1)
    sys.stdout.write(os.environ.get("FAKE_ANG_LOG_OUT", ""))
    sys.exit(int(os.environ.get("FAKE_ANG_LOG_EXIT", "0")))
sys.exit(64)
'''


def install_fake_angareion(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "angareion"
    path.write_text(_FAKE_ANGAREION.format(python=sys.executable, fake_id=FAKE_ID), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def angareion_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


class LinkDir:
    """A fake sidecar-link directory: the two files, written outright by a test.

    `fresh(*ports)` is the common case - a heartbeat naming this process and that port
    list. The other writers exist for the boundary cases: an old or future mtime, a dead
    or malformed pid, a missing file, a list that is not what the reader expects.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    @property
    def heartbeat(self) -> Path:
        return self.root / "heartbeat"

    @property
    def ports(self) -> Path:
        return self.root / "ports"

    def _touch(self, path: Path, age: float) -> None:
        if age:
            when = time.time() - age
            os.utime(path, (when, when))

    def beat(self, content: object = None, *, age: float = 0.0) -> None:
        """Write the heartbeat; the default content is this (live) process's pid."""
        self.heartbeat.write_text(
            str(os.getpid() if content is None else content), encoding="utf-8")
        self._touch(self.heartbeat, age)

    def no_beat(self) -> None:
        self.heartbeat.unlink(missing_ok=True)

    def ports_text(self, text: str, *, age: float = 0.0) -> None:
        self.ports.write_text(text, encoding="utf-8")
        self._touch(self.ports, age)

    def ports_bytes(self, data: bytes) -> None:
        self.ports.write_bytes(data)

    def ports_list(self, *values: object, age: float = 0.0) -> None:
        """Write the list: one value per line, in the order given."""
        self.ports_text("".join(f"{value}\n" for value in values), age=age)

    def no_ports(self) -> None:
        self.ports.unlink(missing_ok=True)

    def fresh(self, *ports: object) -> None:
        """A healthy link: a live heartbeat plus that port list (an empty one by default)."""
        self.beat()
        self.ports_list(*ports)


class FakeSidecar:
    """A loopback HTTP server that answers GET /agents/{id} and POST /agents/{id}/poke.

    `agents[name] = {"state": ..., ...}` is served as 200 JSON; a name that is not there
    is 404. `raw[name] = (status, bytes)` overrides the GET answer verbatim (broken JSON).
    `poke_status` / `poke_body` / `poke_delay` shape the poke answer; `poke_bodies[name] =
    (status, bytes)` overrides the poke answer for one agent, so several recipients in one
    send can get different answers (a `poke_id`/`poke_outcome` body, a nested error body).
    Everything it receives is in `requests` as `{"method", "path", "body"}`.
    """

    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.raw: dict[str, tuple[int, bytes]] = {}
        self.poke_status = 200
        self.poke_body = b"{}"
        self.poke_bodies: dict[str, tuple[int, bytes]] = {}
        self.poke_delay = 0.0
        self.get_delay = 0.0
        self.requests: list[dict] = []
        self._release = threading.Event()
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence the test output
                pass

            def _reply(self, status: int, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                sidecar.requests.append({"method": "GET", "path": self.path, "body": None})
                if sidecar.get_delay:
                    sidecar._release.wait(sidecar.get_delay)
                m = re.fullmatch(r"/agents/([^/]+)", self.path)
                if not m:
                    return self._reply(400, b"{}")
                name = urllib.parse.unquote(m.group(1))  # the real sidecar decodes the path
                if name in sidecar.raw:
                    status, payload = sidecar.raw[name]
                    return self._reply(status, payload)
                if name not in sidecar.agents:
                    return self._reply(404, b'{"error": "agent_not_found"}')
                self._reply(200, json.dumps(sidecar.agents[name]).encode())

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")
                sidecar.requests.append({"method": "POST", "path": self.path, "body": body})
                if sidecar.poke_delay:
                    sidecar._release.wait(sidecar.poke_delay)
                m = re.fullmatch(r"/agents/([^/]+)/poke", self.path)
                if not m:
                    return self._reply(400, b"{}")
                status, payload = sidecar.poke_bodies.get(
                    urllib.parse.unquote(m.group(1)), (sidecar.poke_status, sidecar.poke_body)
                )
                self._reply(status, payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def pokes(self) -> list[dict]:
        """The decoded poke bodies, in order."""
        return [json.loads(r["body"]) for r in self.requests if r["method"] == "POST"]

    def foreign_requests(self) -> list[dict]:
        """Requests that are neither 'look up one agent' nor 'poke one agent'."""
        bad = []
        for r in self.requests:
            ok = (r["method"] == "GET" and re.fullmatch(r"/agents/[^/]+", r["path"])) or (
                r["method"] == "POST" and re.fullmatch(r"/agents/[^/]+/poke", r["path"])
            )
            if not ok:
                bad.append(r)
        return bad


class RawSidecar:
    """A loopback socket that answers the n-th connection with the n-th canned byte string.

    For answers http.server cannot produce (a broken status line, a truncated body). The
    last answer repeats when there are more connections than answers.
    """

    def __init__(self, answers: list[bytes]) -> None:
        self.answers = answers
        self.connections = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._sock.getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                data = b""
                try:
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    answer = self.answers[min(self.connections, len(self.answers) - 1)]
                    self.connections += 1
                    conn.sendall(answer)
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


def _link_is_test_only() -> bool:
    """True when the sidecar-link directory aicomm could read is one a test owns.

    An explicit `AICOMM_SIDECAR_LINK` is the test's own (the fixture always sets one). With
    it unset, aicomm would read `~/.mylinuxpool/sidecar-link` under HOME, which is allowed
    only when that directory does not exist - `isolated_home` points HOME at a tmp dir, so
    the real one a worker has is out of reach either way.
    """
    if os.environ.get("AICOMM_SIDECAR_LINK"):
        return True
    try:
        return not (Path.home() / ".mylinuxpool" / "sidecar-link").exists()
    except (OSError, RuntimeError):  # no home to look under: nothing to reach
        return True


def run_aicomm(argv: list[str], monkeypatch, capsys, stdin: str | None = None) -> Result:
    """Run `aicomm.cli.main` in-process. Fails when the fake sidecar/link are not set (guard)."""
    assert os.environ.get("AICOMM_SIDECAR"), (
        "AICOMM_SIDECAR is not set: a test must point aicomm at its own fake sidecar"
    )
    assert _link_is_test_only(), (
        "AICOMM_SIDECAR_LINK is not set and the default sidecar-link directory under HOME "
        "exists: a test must point aicomm at its own fake directory - never at the real "
        "`~/.mylinuxpool/sidecar-link` a worker has"
    )
    from aicomm import cli

    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


@pytest.fixture
def aicomm_world(tmp_path, monkeypatch):
    """A fake `angareion` first on PATH, a fake loopback sidecar and a fake sidecar-link dir.

    The environment is cleaned first, so nothing from the shell (a real AICOMM_SIDECAR, a
    real AICOMM_SIDECAR_LINK, a real identity, FAKE_ANG_* leftovers) leaks into a test. The
    link starts healthy with an empty port list: a recipient the local sidecar does not
    know is `peer-unavailable`, the same as a machine without the link. `add_peer()` starts
    one loopback peer sidecar and publishes its port; no test ever reads a real link. The
    test modules import this as `world`.
    """
    for name in list(os.environ):
        if name.startswith(("FAKE_ANG_", "AICOMM_")):
            monkeypatch.delenv(name)
    bin_dir = tmp_path / "bin"
    install_fake_angareion(bin_dir)
    log = tmp_path / "angareion-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ANG_LOG", str(log))
    monkeypatch.setenv("ANGAREION_IDENTITY", SENDER)
    # `uv run` puts the venv's bin first on PATH; never run the real angareion by mistake.
    assert shutil.which("angareion") == str(bin_dir / "angareion")
    sidecar = FakeSidecar()
    monkeypatch.setenv("AICOMM_SIDECAR", sidecar.url)
    link = LinkDir(tmp_path / "link")
    monkeypatch.setenv("AICOMM_SIDECAR_LINK", str(link.root))
    link.fresh()  # a live heartbeat and no ports to try
    peers: list[FakeSidecar] = []

    def add_peer(state: str = "idle", name: str | None = "demo-pm-t1") -> FakeSidecar:
        """A peer sidecar that knows `name`, with its port published in the link's list."""
        peer = FakeSidecar()
        if name is not None:
            peer.agents[name] = {"id": name, "state": state}
        peers.append(peer)
        link.fresh(*[peer.port for peer in peers])
        return peer
    try:
        yield SimpleNamespace(
            sidecar=sidecar,
            link=link,
            calls=lambda: angareion_calls(log),
            sender=SENDER,
            setenv=monkeypatch.setenv,
            delenv=monkeypatch.delenv,
            add_peer=add_peer,
            peer_ports=lambda: [one.port for one in peers],
        )
    finally:
        sidecar.close()
        for peer in peers:
            peer.close()
    # The local sidecar is only ever looked up or poked, whatever the test did.
    assert sidecar.foreign_requests() == []
    # And every peer sidecar saw nothing but those same two requests.
    for peer in peers:
        assert peer.foreign_requests() == []

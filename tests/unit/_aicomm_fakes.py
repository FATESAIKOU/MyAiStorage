"""Fakes for the aicomm tests: a loopback fake sidecar and fake `angareion`/`pool-peer`.

aicomm talks to three things: the `angareion` command and MyLinuxPool's `pool-peer`
command (both found on PATH or given by an environment variable), and the local sidecar
(HTTP on loopback). All three are replaced here, so a test never reaches the real channel,
the real pool or the real sidecar. `run_aicomm` refuses to run unless `AICOMM_SIDECAR` is
set and the peer is a test one too: either `AICOMM_POOL_PEER` set, or - when a PATH-fallback
test leaves it unset - PATH with no `pool-peer` at all, or only a wrapper around the fake
one. So a test that forgets its fakes fails instead of knocking on the real sidecar or on
a real `pool-peer` (a worker has one on PATH).
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
    out = os.environ.get("FAKE_ANG_SEND_OUT", "{fake_id}")
    if out:
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


# `pool-peer` (MyLinuxPool) is the other command aicomm runs (#368). The fake answers
# from the scenario file `FAKE_PEER_SCENARIO` points at: `{"find": {rc, stdout, delay,
# stderr, stdout_b64}}` and `{"call": {"<port>": {rc, stdout, delay, stderr, stdout_b64}}}`;
# a scenario that names nobody (`find` rc 1) is the default. `stdout_b64` is base64 raw
# bytes the peer prints on stdout, for answers that are not UTF-8. `find` may instead be
# `{"by_id": {"<id>": {...}}}` so several recipients in one send get different answers.
# Every call is appended to `FAKE_PEER_LOG` as one JSON line `{"argv", "stdin"}` before it
# sleeps, so a call aicomm killed on its own timeout is still recorded.
_FAKE_POOL_PEER = r'''#!{python}
import base64, json, os, sys, time
argv = sys.argv[1:]
stdin = sys.stdin.read() if argv[:1] == ["call"] else ""
with open(os.environ["FAKE_PEER_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": argv, "stdin": stdin}}) + "\n")
path = os.environ.get("FAKE_PEER_SCENARIO", "")
scenario = {{}}
if path and os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        scenario = json.load(f)
if argv[:1] == ["find"]:
    answer = scenario.get("find") or {{"rc": 1}}
    by_id = answer.get("by_id")
    if isinstance(by_id, dict):
        answer = by_id.get(argv[1] if len(argv) > 1 else "") or {{"rc": 1}}
elif argv[:1] == ["call"]:
    answer = (scenario.get("call") or {{}}).get(argv[1] if len(argv) > 1 else "") or {{}}
else:
    answer = {{}}
delay = float(answer.get("delay") or 0)
if delay:
    time.sleep(delay)
raw = answer.get("stdout_b64")
if raw:
    sys.stdout.buffer.write(base64.b64decode(raw))
else:
    sys.stdout.write(answer.get("stdout") or "")
sys.stderr.write(answer.get("stderr") or "")
sys.exit(int(answer.get("rc") or 0))
'''


def install_fake_pool_peer(path: Path) -> None:
    """Write the fake `pool-peer` to `path` and make it executable."""
    path.write_text(_FAKE_POOL_PEER.format(python=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def angareion_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def pool_peer_calls(log: Path) -> list[dict]:
    """Every argv (and stdin) the fake `pool-peer` was given, in order."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def assert_only_pool_peer_calls(log: Path) -> None:
    """`pool-peer` is only ever asked the two things design §2 allows (#368).

    `find <id> --json --timeout 10` and `call <port> POST /agents/<id>/poke --timeout 5`;
    nothing else, and no `list`.
    """
    for call in pool_peer_calls(log):
        argv = call["argv"]
        what = f"unexpected pool-peer argv: {argv}"
        if argv[:1] == ["find"]:
            assert len(argv) == 5 and argv[2:] == ["--json", "--timeout", "10"], what
        elif argv[:1] == ["call"]:
            assert len(argv) == 6 and argv[2] == "POST", what
            assert argv[4:] == ["--timeout", "5"], what
            assert re.fullmatch(r"[1-9][0-9]*", argv[1]), what
            assert re.fullmatch(r"/agents/[^/]+/poke", argv[3]), what
        else:
            raise AssertionError(what)


class FakeSidecar:
    """A loopback HTTP server that answers GET /agents/{id} and POST /agents/{id}/poke.

    `agents[name] = {"state": ..., ...}` is served as 200 JSON; a name that is not there
    is 404. `raw[name] = (status, bytes)` overrides the GET answer verbatim (broken JSON).
    `poke_status` / `poke_body` / `poke_delay` shape the poke answer. Everything it
    receives is in `requests` as `{"method", "path", "body"}`.
    """

    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.raw: dict[str, tuple[int, bytes]] = {}
        self.poke_status = 200
        self.poke_body = b"{}"
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
                name = m.group(1)
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
                if not re.fullmatch(r"/agents/[^/]+/poke", self.path):
                    return self._reply(400, b"{}")
                self._reply(sidecar.poke_status, sidecar.poke_body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

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
    def url(self) -> str:
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}"

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


def _path_peer_is_test_only() -> bool:
    """True when a `pool-peer` on PATH is one a test may run: none at all, or our fake's.

    With `AICOMM_POOL_PEER` unset, aicomm falls back to `shutil.which("pool-peer")`. One
    test family runs that fallback for real; that is allowed only when PATH has no
    `pool-peer` at all, or `which` resolves to a wrapper that runs the fake script -
    never to the real `pool-peer` a worker may have on PATH.
    """
    script = os.environ.get("FAKE_PEER_SCRIPT")
    if not script:
        return False
    found = shutil.which("pool-peer")
    if found is None:
        return True
    try:
        text = Path(found).read_text(encoding="utf-8")
    except OSError:
        return False
    return script in text


def run_aicomm(argv: list[str], monkeypatch, capsys, stdin: str | None = None) -> Result:
    """Run `aicomm.cli.main` in-process. Fails when the fake sidecar/peer is not set (guard)."""
    assert os.environ.get("AICOMM_SIDECAR"), (
        "AICOMM_SIDECAR is not set: a test must point aicomm at its own fake sidecar"
    )
    assert os.environ.get("AICOMM_POOL_PEER") or _path_peer_is_test_only(), (
        "AICOMM_POOL_PEER is not set and PATH has no test `pool-peer` either: a test must "
        "point aicomm at its own fake `pool-peer` (the hook, or a wrapper that runs the "
        "fake script, which is what the PATH-fallback family does) - never at the real "
        "one a worker has on PATH"
    )
    from aicomm import cli

    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


@pytest.fixture
def aicomm_world(tmp_path, monkeypatch):
    """A fake `angareion` first on PATH plus a fake loopback sidecar; both are ours alone.

    The environment is cleaned first, so nothing from the shell (a real AICOMM_SIDECAR, a
    real identity, FAKE_ANG_*/FAKE_PEER_* leftovers) leaks into a test. The fake
    `pool-peer` is there too, but `AICOMM_POOL_PEER` points at a path that does not exist
    until a test calls `use_peer()`: the peer fallback is then off. The PATH-fallback
    family instead deletes the hook and puts a wrapper that runs the fake script on PATH;
    the `run_aicomm` guard allows that wrapper (and nothing else) as the peer. No test
    ever runs a real `pool-peer` from PATH. The test modules import this as `world`.
    """
    for name in list(os.environ):
        if name.startswith(("FAKE_ANG_", "FAKE_PEER_", "AICOMM_")):
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
    fake_peer = tmp_path / "fake-pool-peer"
    install_fake_pool_peer(fake_peer)
    peer_log = tmp_path / "pool-peer-calls.jsonl"
    peer_scenario = tmp_path / "pool-peer-scenario.json"
    monkeypatch.setenv("FAKE_PEER_LOG", str(peer_log))
    monkeypatch.setenv("FAKE_PEER_SCENARIO", str(peer_scenario))
    # What the PATH-fallback guard checks a wrapper against (its content names this script).
    monkeypatch.setenv("FAKE_PEER_SCRIPT", str(fake_peer))
    monkeypatch.setenv("AICOMM_POOL_PEER", "/nonexistent/pool-peer")
    try:
        yield SimpleNamespace(
            sidecar=sidecar,
            calls=lambda: angareion_calls(log),
            sender=SENDER,
            setenv=monkeypatch.setenv,
            peer_calls=lambda: pool_peer_calls(peer_log),
            peers=lambda scenario: peer_scenario.write_text(json.dumps(scenario), encoding="utf-8"),
            use_fake_peer=lambda: monkeypatch.setenv("AICOMM_POOL_PEER", str(fake_peer)),
            fake_peer=fake_peer,
        )
    finally:
        sidecar.close()
    # The sidecar is only ever looked up or poked, whatever the test did.
    assert sidecar.foreign_requests() == []
    # And `pool-peer` was only ever asked to find one id or to poke one sidecar.
    assert_only_pool_peer_calls(peer_log)

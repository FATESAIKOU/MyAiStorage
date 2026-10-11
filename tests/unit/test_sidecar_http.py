"""The HTTP control plane (change sidecar-v0, tasks 2.1/2.6).

The server binds 127.0.0.1 only and speaks the four actions plus /jobs; the
agents behind it are the in-memory fakes.  Errors answer
{"error": {"code", "message"}} with the D1/D4 codes.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import threading
import urllib.error
import urllib.request

import pytest

import _sidecar_fakes as F
from sidecar.core import SidecarCore
from sidecar.http import make_server


def call(base, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, raw.decode("utf-8", errors="replace")


def _serve(core):
    server = make_server(core, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def control(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    clock = F.FakeClock(1000.0)
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource(F.default_subscriptions())
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    server, thread = _serve(core)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, server, core, driver, store, source, clock, str(workdir)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _open_args(workdir, **extra):
    args = {"id": "t1", "agent": "claude-code", "workdir": workdir, "session": {"mode": "new"}}
    args.update(extra)
    return args


def test_binds_loopback_only(control):
    _base, server, _core, _driver, _store, _source, _clock, _workdir = control
    assert server.server_address[0] == "127.0.0.1"


def test_open_status_poke_close_roundtrip(control):
    base, _server, _core, driver, store, _source, _clock, workdir = control

    status, body = call(
        base, "POST", "/agents", _open_args(workdir, input="hello")
    )
    assert status == 201
    assert body["id"] == "t1"
    assert body["state"] == "running"
    assert body["subscription"] == "claude-personal"
    assert body["session"] is None  # `new` has no agora id yet (D5)
    assert body["reopen_at"] is None
    assert body["exit"] is None

    status, body = call(base, "GET", "/agents")
    assert status == 200
    assert [agent["id"] for agent in body["agents"]] == ["t1"]

    status, body = call(base, "GET", "/agents/t1")
    assert status == 200 and body["id"] == "t1"

    status, _body = call(base, "POST", "/agents/t1/poke", {"text": "停下來", "interrupt": True})
    assert status == 200
    assert driver.handles[0].interrupts == 1
    assert driver.handles[0].inbox == ["hello", "停下來"]

    status, body = call(base, "POST", "/agents/t1/close")
    assert status == 200
    assert body["state"] == "exited"
    assert driver.stop_calls  # the agent was stopped before the answer


def test_duplicate_id_is_a_409(control):
    base, _server, _core, driver, _store, _source, _clock, workdir = control
    assert call(base, "POST", "/agents", _open_args(workdir))[0] == 201
    status, body = call(base, "POST", "/agents", _open_args(workdir))
    assert status == 409
    assert body["error"]["code"] == "id_conflict"
    assert len(driver.launches) == 1


def test_missing_and_bad_requests_use_the_named_codes(control):
    base, _server, _core, driver, _store, _source, _clock, workdir = control

    status, body = call(base, "GET", "/agents/nope")
    assert status == 404 and body["error"]["code"] == "agent_not_found"

    status, body = call(base, "POST", "/agents", _open_args("/no/such/dir"))
    assert status == 400 and body["error"]["code"] == "workdir_not_found"

    status, body = call(base, "POST", "/agents", {"id": "t1"})
    assert status == 400 and body["error"]["code"] == "invalid_request"

    status, body = call(
        base, "POST", "/agents", _open_args(workdir, subscription={"id": "nope"})
    )
    assert status == 400 and body["error"]["code"] == "subscription_not_found"

    _source.get_errors["claude-personal"] = "agent_not_allowed"
    status, body = call(
        base, "POST", "/agents", _open_args(workdir, subscription={"id": "claude-personal"})
    )
    assert status == 400 and body["error"]["code"] == "subscription_not_allowed"

    _source.get_errors["claude-personal"] = "not_deployed"
    status, body = call(
        base, "POST", "/agents", _open_args(workdir, subscription={"id": "claude-personal"})
    )
    assert status == 409 and body["error"]["code"] == "not_deployed"

    _source.get_errors.clear()
    _source.list_unreachable = "tunnel down"
    status, body = call(base, "POST", "/agents", _open_args(workdir))
    assert status == 503 and body["error"]["code"] == "gateway_unreachable"
    assert driver.launches == []  # nothing was opened

    request = urllib.request.Request(base + "/agents", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=5)
    assert excinfo.value.code == 400

    status, _body = call(base, "PUT", "/agents")
    assert status == 501  # no PUT handler in stdlib's BaseHTTPRequestHandler


def test_poke_on_an_exited_agent_is_a_409(control):
    base, _server, _core, _driver, _store, _source, _clock, workdir = control
    call(base, "POST", "/agents", _open_args(workdir))
    call(base, "POST", "/agents/t1/close")
    status, body = call(base, "POST", "/agents/t1/poke", {"text": "hi"})
    assert status == 409 and body["error"]["code"] == "agent_not_running"
    status, body = call(base, "POST", "/agents/nope/poke", {"text": "hi"})
    assert status == 404 and body["error"]["code"] == "agent_not_found"


def test_jobs_crud_and_at_execution(control):
    base, _server, core, _driver, _store, _source, clock, workdir = control
    status, job = call(
        base, "POST", "/jobs",
        {"action": "open", "args": _open_args(workdir), "at": clock.t + 60},
    )
    assert status == 201
    assert job["auto"] is False
    assert job["at"] == "1970-01-01T00:17:40Z"  # 1060 epoch
    assert job["last_run"] is None and job["last_error"] is None

    status, body = call(base, "GET", "/jobs")
    assert status == 200 and [j["id"] for j in body["jobs"]] == [job["id"]]

    status, _body = call(base, "DELETE", f"/jobs/{job['id']}")
    assert status == 204
    status, body = call(base, "DELETE", f"/jobs/{job['id']}")
    assert status == 404 and body["error"]["code"] == "job_not_found"

    # And an at job really opens the agent when its moment comes.
    status, _job = call(
        base, "POST", "/jobs",
        {"action": "open", "args": _open_args(workdir), "at": clock.t + 60},
    )
    assert status == 201
    clock.advance(60)
    core.tick()
    status, body = call(base, "GET", "/agents")
    assert [agent["id"] for agent in body["agents"]] == ["t1"]


def test_job_every_with_until_and_iso_at(control):
    base, _server, _core, _driver, _store, _source, clock, workdir = control
    status, job = call(
        base, "POST", "/jobs",
        {"action": "poke", "args": {"id": "t1", "text": "x"}, "every": "5m",
         "until": "2030-01-01T00:00:00Z"},
    )
    assert status == 201
    assert job["every"] == 300.0

    status, job = call(
        base, "POST", "/jobs",
        {"action": "open", "args": _open_args(workdir), "at": "1970-01-01T00:16:40Z"},
    )
    assert status == 201
    assert job["next_run_at"] == "1970-01-01T00:16:40Z"


def test_job_validation_is_a_400(control):
    base, _server, _core, _driver, _store, _source, clock, workdir = control
    args = _open_args(workdir)
    assert call(base, "POST", "/jobs", {"action": "pause", "args": {}})[0] == 400
    assert call(base, "POST", "/jobs", {"action": "open", "args": args, "at": 1, "every": 60})[0] == 400
    assert call(base, "POST", "/jobs", {"action": "open", "args": args})[0] == 400
    assert call(base, "POST", "/jobs", {"action": "poke", "args": {"id": "t1"}, "every": 60})[0] == 400
    assert call(base, "POST", "/jobs", {"action": "open", "args": args, "at": "tomorrow"})[0] == 400
    # at in the past is refused (D1)
    status, body = call(base, "POST", "/jobs", {"action": "open", "args": args, "at": clock.t - 1})
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_failed_job_keeps_last_run_and_last_error(control):
    base, _server, core, _driver, _store, _source, clock, workdir = control
    call(base, "POST", "/agents", _open_args(workdir))  # t1 is running
    status, job = call(
        base, "POST", "/jobs",
        {"action": "open", "args": _open_args(workdir), "every": 60},
    )
    assert status == 201

    clock.advance(60)
    core.tick()

    status, body = call(base, "GET", "/jobs")
    row = body["jobs"][0]
    assert row["id"] == job["id"]
    assert row["last_run"] == "1970-01-01T00:17:40Z"  # 1060 epoch
    assert row["last_error"] == {
        "status": 409,
        "code": "id_conflict",
        "message": "agent 't1' already exists in state idle",
    }
    assert "key" not in json.dumps(row)


def test_no_response_ever_carries_the_subscription_key(tmp_path):
    secret = "HTTP-SECRET-KEY"
    workdir = tmp_path / "work"
    workdir.mkdir()
    clock = F.FakeClock(1000.0)
    driver = F.FakeAgentDriver("claude-code")
    store = F.FakeSessionStore()
    source = F.FakeSubscriptionSource([F.claude_subscription("claude-personal", key=secret)])
    core = SidecarCore(
        drivers={"claude-code": driver}, store=store, subscriptions=source, now=clock
    )
    server, thread = _serve(core)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        seen = []
        status, body = call(
            base, "POST", "/agents",
            {"id": "t1", "agent": "claude-code", "workdir": str(workdir),
             "session": {"mode": "new"}, "subscription": {"id": "claude-personal"},
             "input": "go"},
        )
        seen.append(json.dumps(body))
        assert status == 201
        for method, path in (("GET", "/agents"), ("GET", "/agents/t1"), ("GET", "/jobs")):
            _status, body = call(base, method, path)
            seen.append(json.dumps(body))
        _status, body = call(base, "POST", "/agents/t1/close")
        seen.append(json.dumps(body))
        assert secret not in "\n".join(seen)
        assert body["subscription"] == "claude-personal"  # the id is fine to show
        # and the secret is in the child env the fake driver received
        assert driver.launches[0][1] == {"FAKE_AGENT_KEY": secret}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

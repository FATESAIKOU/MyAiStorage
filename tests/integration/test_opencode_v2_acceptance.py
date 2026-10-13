"""The whole v2 flow on a real opencode 2.x, isolated (change agora-opencode-v2, 5.1/5.2).

Run:
    uv run pytest -q -m integration tests/integration/test_opencode_v2_acceptance.py

Everything real happens inside this test's tmp dirs: `HOME` and the four XDG
directories point there, the model is two local `tests/fakes/fake_upstream.py`
servers on 127.0.0.1 (one that calls a read tool and then answers, one that only
answers with a merge section), Drive is `tests/fakes/fake_rclone.py`, and the
managed service's port is set to a free one in the isolated `XDG_CONFIG_HOME` so
even a call that forgot `--standalone` cannot reach the machine-wide service
(review H2). The machine-wide default port is only looked at, before and after,
with `lsof` - never connected to, never stopped.

`test_the_whole_v2_flow_over_a_real_opencode` (5.1):
    two self-made v2 sessions (one multi-turn with a tool call) -> `agora import`
    -> `continue --headless --sync-period 3` (a periodic write-back really saves a
    turn that arrives while the agent runs) -> SIGTERM (gentle stop, final
    write-back) -> `agora show` (reading version complete) -> `agora merge` the
    two -> `continue --headless` the merged one.

`test_an_imported_session_is_visible_through_the_managed_service` (5.2):
    the managed service running on the isolated free port; a session imported
    with `--standalone` is then listed and answered for through that service.
    The real TUI (`opencode --session <id>` with a terminal) stays on the manual
    acceptance list in docs/spike/opencode.md.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import fake_upstream  # noqa: E402  (needs the path above)
import opencode_noninteractive as agent  # noqa: E402

from agora import cli, store

pytestmark = pytest.mark.integration

REAL_OPENCODE = agent.real_opencode()
DEFAULT_PORT = 49374
FAKE_MODEL = f"{fake_upstream.PROVIDER}/{fake_upstream.MODEL}"
#: The summarizer's answer must be a merge section; this is what the "text only"
#: upstream says, and it is self-made filler.
SECTION = {"purpose": "把 CSV 轉成 Markdown 表格",
           "decisions": [{"decision": "先列三個步驟", "reason": "使用者要求"}],
           "progress": "兩個來源都讀過了",
           "open_questions": []}
FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _shared_port() -> str | None:
    """What listens on the machine-wide default port, as `lsof` sees it.

    A look, never a knock (review H2); None when lsof is not on this machine.
    """
    try:
        proc = subprocess.run(["lsof", "-nP", f"-iTCP:{DEFAULT_PORT}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip()


def _opencode(state, *argv: str, timeout: int = 180) -> subprocess.CompletedProcess:
    """The real opencode inside the isolated environment, PWD and all."""
    proc = subprocess.run([REAL_OPENCODE, *argv], cwd=str(state["proj"]),
                          env={**os.environ, "PWD": str(state["proj"])},
                          capture_output=True, text=True, timeout=timeout)
    assert proc.returncode == 0, f"opencode {argv[:2]} 失敗：{proc.stderr[-400:]}"
    return proc


def _session_id_of(proc: subprocess.CompletedProcess, state) -> str:
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    session_id = next(event["sessionID"] for event in events if "sessionID" in event)
    state["ses"].append(session_id)
    return session_id


def _new_session(state, *args: str) -> str:
    """One `opencode run` (a turn) in the project; returns the session id."""
    return _session_id_of(_opencode(state, "run", "--standalone", "-m", FAKE_MODEL,
                                    "--format", "json", *args), state)


def _agora(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _wait_for(what, timeout: float = 45.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = what()
        if value:
            return value
        time.sleep(step)
    return None


def _stored_body(paths: store.Paths, agora_id: str) -> str:
    return (paths.mirror / agora_id.split(":", 1)[1] / "session.md").read_text(encoding="utf-8")


class _Continue:
    """A `continue --headless` subprocess and its stderr lines, read as they come."""

    def __init__(self, state, tmp_path, agora_id: str, *, sync_period: str = "0.05"):
        # --sync-period is minutes: 0.05 is one write-back round every three seconds
        launcher = tmp_path / "run_agora.py"
        launcher.write_text("import sys\nfrom agora.cli import main\nsys.exit(main(sys.argv[1:]))\n",
                            encoding="utf-8")
        self.port = _free_port()
        self.argv = [sys.executable, str(launcher), "continue", "session", agora_id,
                     "--agent", "opencode", "--dir", str(state["proj"]),
                     "--headless", "--headless-port", str(self.port),
                     "--sync-period", sync_period]
        self.proc = subprocess.Popen(self.argv, cwd=str(state["proj"]), env=dict(os.environ),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lines: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stderr:
            self.lines.append(line.rstrip("\n"))

    def three_lines(self, agora_id: str, timeout: float = 120.0) -> list[str]:
        """Wait for the fixed lines the session-continue spec promises."""
        got = _wait_for(lambda: self.lines if any(
            line.startswith("agora-agent-session ") for line in self.lines) else None, timeout)
        assert got is not None, f"continue 沒有印出三行：{self.lines}"
        first = got.index(next(line for line in got if line.startswith("agora-session ")))
        three = got[first:first + 3]
        assert three[0] == f"agora-session {agora_id}", three
        assert three[1] == f"agora-port {self.port}", three
        assert three[2].startswith("agora-agent-session ses_"), three
        return three

    def sigterm(self, timeout: float = 120.0) -> int:
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=timeout)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """The isolated everything: HOME/XDG, models, Drive, service port, project."""
    if REAL_OPENCODE is None:
        pytest.skip("這台機器沒有 opencode（AGORA_REAL_OPENCODE 或 PATH）")
    for name in [k for k in os.environ if k.startswith("OPENCODE_")]:
        monkeypatch.delenv(name)
    xdg = {var: tmp_path / var[4:-5].lower().replace("_", "-") for var in (
        "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")}
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var, path in xdg.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
    if not agent.session_export_supported():
        pytest.skip("這台機器沒有 opencode 2.x（session export）")

    # the two model stands-in: one calls a tool and then answers, one answers with
    # a merge section (the summarizer's output has to validate)
    tool_upstream = fake_upstream.start(mode="tool-then-text", log=tmp_path / "upstream-tool.log")
    text_upstream = fake_upstream.start(mode="text", text=json.dumps(SECTION, ensure_ascii=False),
                                        log=tmp_path / "upstream-text.log")
    config = fake_upstream.opencode_config(tool_upstream.url)
    config["providers"].update(fake_upstream.opencode_config(
        text_upstream.url, provider="faketext")["providers"])
    target = xdg["XDG_CONFIG_HOME"] / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # the managed service's port, inside the isolated config only (H2)
    service_port = _free_port()
    subprocess.run([REAL_OPENCODE, "service", "set", "port", str(service_port)],
                   env={**os.environ}, capture_output=True, timeout=120, check=True)

    # Drive: the fake rclone
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("FAKE_REMOTE", str(tmp_path / "Drive"))
    monkeypatch.setenv("AGORA_UPLOAD", "inline")

    monkeypatch.setenv("AGORA_OPENCODE_CMD", REAL_OPENCODE)
    monkeypatch.setenv("AGORA_OPENCODE_MODEL", "faketext/fake-model")

    proj = tmp_path / "proj"                       # a project of its own, never the repo
    proj.mkdir()
    (proj / "fake-note.txt").write_text("ZZNOTE 這是一張紙條。\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)

    state = {"proj": proj, "paths": store.Paths.from_env(), "ses": [],
             "tool_upstream": tool_upstream, "text_upstream": text_upstream}
    try:
        yield state
    finally:
        # every opencode session this test made goes, one id at a time, even
        # though the whole database is this test's own directory
        for session_id in sorted(set(state["ses"])):
            subprocess.run([REAL_OPENCODE, "session", "delete", session_id, "--standalone"],
                           cwd=str(proj), env={**os.environ, "PWD": str(proj)},
                           capture_output=True)
        tool_upstream.stop()
        text_upstream.stop()


def test_the_whole_v2_flow_over_a_real_opencode(lab, capsys, tmp_path):
    """5.1: import -> headless continue (periodic + final write-back) -> show ->
    merge -> continue the merged session, with the real opencode and no model
    beyond the local fake."""
    state, paths = lab, lab["paths"]
    before = _shared_port()

    # two self-made v2 sessions in the real (isolated) store; A is multi-turn and
    # its first turn calls the read tool
    first = _new_session(state, "--title", "驗收 A", "第一輪：先讀 fake-note.txt 再回答。")
    _new_session(state, "-s", first, "第二輪：ZZACCEPT2 這是接續的一輪。")
    second = _new_session(state, "--title", "驗收 B", "ZZACCEPTB 另一顆要一起 merge 的。")

    code, out, err = _agora(capsys, "import", "session", "--external-session-id", first,
                            "--agent", "opencode", "--header", "title=驗收 A")
    assert code == 0, err[-400:]
    id_a = out.split()[0]
    assert id_a.startswith("agora:")
    code, out, err = _agora(capsys, "import", "session", "--external-session-id", second,
                            "--agent", "opencode", "--header", "title=驗收 B")
    assert code == 0, err[-400:]
    id_b = out.split()[0]

    # continue A headless; the three fixed lines come before the agent starts
    run = _Continue(state, tmp_path, id_a)
    three = run.three_lines(id_a)
    agent_session = three[2].split()[1]
    state["ses"].append(agent_session)

    # a turn while the agent runs: the periodic round really writes it back
    _new_session(state, "-s", agent_session, "第三輪：ZZPERIODIC 定期寫回要看到這句。")
    assert _wait_for(lambda: "ZZPERIODIC" in _stored_body(paths, id_a)), \
        f"定期寫回沒有把新的一輪寫進 store：{run.lines[-6:]}"

    # a last turn right after that round, then SIGTERM inside the same interval:
    # the final write-back is what saves it, and the stop is gentle
    _new_session(state, "-s", agent_session, "第四輪：ZZFINAL 收尾前說的。")
    assert run.sigterm() == 0, "continue --headless 沒有溫和停下"
    assert _wait_for(lambda: "ZZFINAL" in _stored_body(paths, id_a), 30), \
        "最後一次寫回沒有存到收尾前的那一輪"
    assert "定期寫回失敗" not in "\n".join(run.lines), run.lines[-5:]

    # show: the reading version is complete - both rounds, the tool line, the text
    code, out, err = _agora(capsys, "show", "session", id_a)
    assert code == 0, err[-400:]
    assert out.count("## user") >= 3 and out.count("## assistant") >= 3
    assert "[tool] read" in out
    assert "ZZPERIODIC" in out and "ZZFINAL" in out
    assert "ZZNOTE" not in out, "工具輸出不是對話，不該出現在閱讀版"

    # merge the two, then continue the merged session headless
    code, out, err = _agora(capsys, "merge", "session", id_a, id_b, "--agent", "opencode")
    assert code == 0, err[-400:]
    merged = out.split()[0]
    assert merged.startswith("agora:") and merged not in (id_a, id_b)
    assert lab["text_upstream"].calls(), "merge 沒有走假 upstream 寫要約"
    code, out, err = _agora(capsys, "show", "session", merged)
    assert code == 0 and "合併 2 個 Session" in out and "原本是 opencode" in out

    run2 = _Continue(state, tmp_path, merged)
    three = run2.three_lines(merged)
    merged_agent_session = three[2].split()[1]
    state["ses"].append(merged_agent_session)
    _new_session(state, "-s", merged_agent_session, "ZZMERGED 接續 merged 之後的一句。")
    assert _wait_for(lambda: "ZZMERGED" in _stored_body(paths, merged), 45), \
        "merged 的接續沒有寫回"
    assert run2.sigterm() == 0
    body = _stored_body(paths, merged)
    assert "## user" in body and "以下是由" in body, "接續 merged 之後 store 裡不是那場對話"

    assert _shared_port() == before, f"預設埠 {DEFAULT_PORT} 的服務被動到了"


def test_an_imported_session_is_visible_through_the_managed_service(lab):
    """5.2: the running managed service (isolated free port) sees a session that
    was imported with --standalone, and can answer in it. The real TUI stays on
    the manual list."""
    state, paths = lab, lab["paths"]
    before = _shared_port()
    env = {**os.environ, "PWD": str(state["proj"])}

    # a fresh id from the fixture, imported by the real 2.x CLI into the isolated store
    payload = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
                          / "oc-v2-basic.json").read_text(encoding="utf-8"))
    payload["info"]["id"] = "ses_ZZSERVICE00000000001"
    payload["info"]["location"]["directory"] = str(state["proj"])
    raw = state["proj"] / "import-me.json"
    raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    state["ses"].append(payload["info"]["id"])
    subprocess.run([REAL_OPENCODE, "session", "import", str(raw), "--standalone",
                    "--directory", str(state["proj"])],
                   cwd=str(state["proj"]), env=env, capture_output=True, check=True)

    # the managed service comes up on the isolated port...
    started = subprocess.run([REAL_OPENCODE, "service", "start"], cwd=str(state["proj"]),
                             env=env, capture_output=True, text=True, timeout=120)
    assert started.returncode == 0, started.stderr
    try:
        # ...and a call without --standalone goes through it: the session is listed
        listed = subprocess.run([REAL_OPENCODE, "session", "list", "--format", "json"],
                                cwd=str(state["proj"]), env=env, capture_output=True,
                                text=True, timeout=120)
        assert listed.returncode == 0, listed.stderr
        rows = json.loads(listed.stdout or "[]")
        assert payload["info"]["id"] in {row["id"] for row in rows}
        # and the service can answer in it, still through the local fake model
        answered = subprocess.run([REAL_OPENCODE, "run", "-m", FAKE_MODEL, "--format", "json",
                                   "-s", payload["info"]["id"],
                                   "ZZSERVICE 用一句話回答。"],
                                  cwd=str(state["proj"]), env=env, capture_output=True,
                                  text=True, timeout=180)
        assert answered.returncode == 0, answered.stderr[-400:]
        assert fake_upstream.DEFAULT_TEXT in answered.stdout
    finally:
        stopped = subprocess.run([REAL_OPENCODE, "service", "stop"], cwd=str(state["proj"]),
                                 env=env, capture_output=True, text=True, timeout=120)
        assert stopped.returncode == 0, stopped.stderr
    assert _shared_port() == before, f"預設埠 {DEFAULT_PORT} 的服務被動到了"

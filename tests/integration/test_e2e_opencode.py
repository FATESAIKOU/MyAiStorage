"""End to end through agora.cli.main with opencode: import -> search -> continue
(native) -> continue (cross agent, converted), against real Drive
(agora-test/), real opencode and real claude.

Continue writes back into the session it continued (design 5.4, the user's call):
there is no second agora id, `agora.source` becomes the agent session it just
used, the one it came from is remembered in `agora.previous_sources` - so the
import tab stops listing it - and a continued merge stops being a merge.

Mirrors tests/integration/test_e2e_cli.py (which does the same with claude):
the config is a temp dir whose rclone.conf symlinks to ~/.config/agora/rclone.conf
(never read, only referenced by path), and every command goes through
`agora.cli.main` so the assertions read like a user's session.

Two shims make the interactive parts testable:

* AGORA_OPENCODE_CMD -> tests/fakes/opencode_noninteractive.py, which turns the
  TUI form `opencode --session <id>` into `opencode run -s <id> -m <model>
  <fixed question>`. Invoked through `sys.executable`, so no shebang roulette.
* AGORA_CLAUDE_CMD -> tests/fakes/claude_noninteractive.py (impl2's), which adds
  `--disallowedTools Bash Read Glob Grep Edit Write WebFetch WebSearch Task` and
  a fixed self-made prompt.

The project directory is /tmp/agora-it-e2e-oc/p_專案.v2 on purpose: an underscore,
Chinese and a dot in one path, which is what exercises opencode's project id and
claude's directory encoding. opencode scopes sessions to the project directory,
so running it here cannot reach the user's own sessions.

Cleanup (ids are recorded the moment they exist, so a mid-test failure still
tidies up): Drive sessions/<ULID> purged, own opencode session ids deleted one by
one, own claude jsonl uuids deleted, then the project tree. Nothing is deleted in
bulk and nothing outside agora-test/ is touched.

Run: uv run pytest -q -m integration tests/integration/test_e2e_opencode.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import uuid
import warnings

import pytest

from agora import cli, store
from agora.agents import claude as C

# The model chain lives in the fake the wrapper runs, so both integration tests
# try the same models in the same order (tests/fakes is not a package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import opencode_noninteractive as agent  # noqa: E402  (needs the path above)

pytestmark = pytest.mark.integration

# pwd, not HOME: the sandbox replaces HOME per test.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
OPENCODE_WRAPPER = Path(__file__).parent.parent / "fakes" / "opencode_noninteractive.py"
CLAUDE_WRAPPER = Path(__file__).parent.parent / "fakes" / "claude_noninteractive.py"
ASK = "把 CSV 轉成 Markdown 表格，先列三個步驟就好，不要真的動手做。"

ROOT = Path("/tmp/agora-it-e2e-oc")
PROJ = ROOT / "p_專案.v2"   # underscore + Chinese + dot, on purpose


@pytest.fixture()
def e2e(tmp_path, monkeypatch, capsys):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    if shutil.which("opencode") is None:
        pytest.skip("opencode not found")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found")

    config = tmp_path / "config"
    config.mkdir()
    (config / "rclone.conf").symlink_to(REAL_CONF)
    monkeypatch.setenv("AGORA_CONFIG", str(config))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # This file is about the cross-agent flow, not about the background uploader: in
    # the foreground the upload is done when the command returns, which is what these
    # assertions have always meant. The detached uploader has its own tests
    # (tests/integration/test_background_writes.py, tests/unit/test_background.py).
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    # opencode is left on the sandboxed HOME that conftest sets, so this test
    # never writes to the real store; the free model needs no auth. (An earlier
    # version forced HOME back to the real one and then cleaned up with the
    # sandboxed HOME, so the teardown was deleting from a database the test had
    # never written to.) Only the wrapper honours AGORA_REAL_HOME, and it is
    # deliberately left unset here.
    monkeypatch.delenv("AGORA_REAL_HOME", raising=False)
    monkeypatch.delenv("AGORA_OPENCODE_CMD", raising=False)

    for src, var in ((OPENCODE_WRAPPER, "AGORA_OPENCODE_CMD"),
                     (CLAUDE_WRAPPER, "AGORA_CLAUDE_CMD")):
        shim = tmp_path / src.stem  # exec through sys.executable: no shebang lottery
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} {src} \"$@\"\n")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv(var, str(shim))

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))

    if ROOT.exists():
        shutil.rmtree(ROOT)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=PROJ, check=True)

    paths = store.Paths.from_env()
    # ids are recorded the moment they exist, not at the end (E3)
    created = {"ulids": [], "ses": [], "uuids": [], "printed": []}
    state = {"proj": PROJ.resolve(), "paths": paths, "created": created,
             "fake_home": fake_home, "capsys": capsys}
    yield state

    _cleanup(state)


def _cleanup(state) -> None:
    created = state["created"]
    proj, paths = state["proj"], state["paths"]

    # opencode resolves the project from $PWD, so every call below has to carry
    # it: cleanup that only sets cwd would look in the wrong project and leave
    # the session behind (measured).
    env = {**os.environ, "PWD": str(proj)}

    # ids are taken from the wrapper log too, so a launch that happened right
    # before a crash is still cleaned up.
    log = state["fake_home"] / "opencode-e2e-args.log"
    if log.exists():
        for line in log.read_text().splitlines():
            argv = json.loads(line)["argv"]
            for flag in ("--session", "-s"):
                if flag in argv and argv[argv.index(flag) + 1].startswith("ses_"):
                    created["ses"].append(argv[argv.index(flag) + 1])

    # opencode: one id at a time, never a pattern or a bulk delete
    for session_id in sorted(set(created["ses"])):
        proc = subprocess.run(["opencode", "session", "delete", session_id],
                              cwd=str(proj), env=env, capture_output=True)
        if proc.returncode != 0:
            warnings.warn(f"delete {session_id} failed: {proc.stderr.decode()[-200:]}")

    # P1 (D-1): this test's own printed ids only. `agora-test/` also holds other
    # people's sessions, and a sync has already listed them into this cache, so
    # purging "everything I know about" would delete their data.
    ulids = {u.split(":", 1)[1] for u in created["printed"]}
    ulids.update(created["ulids"])
    drive = store.Drive(paths)
    for ulid in sorted(ulids):
        proc = subprocess.run(
            ["rclone", "--config", str(REAL_CONF),
             "--drive-root-folder-id", drive.folder_id(),
             "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
        if proc.returncode != 0:
            warnings.warn(f"purge sessions/{ulid} failed: {proc.stderr.decode()[-200:]}")

    # P2 (E3/D-2): the wrapper logs the uuid before the agent starts, so a failed
    # continue still gets cleaned up. Paths are exact - never a glob over the real
    # ~/.claude/projects/*/ or todos/, which lists other people's filenames.
    claude_log = state["fake_home"] / "e2e-args.log"
    if claude_log.exists():
        for line in claude_log.read_text().splitlines():
            argv = json.loads(line)
            for flag in ("--resume", "--session-id"):
                if flag in argv:
                    created["uuids"].append(argv[argv.index(flag) + 1])
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for session_id in sorted(set(created["uuids"])):
        jsonl = basedir / f"{session_id}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        shutil.rmtree(basedir / session_id, ignore_errors=True)
        for extra in (cfgdir / "session-env" / session_id,
                      cfgdir / "file-history" / session_id):
            shutil.rmtree(extra, ignore_errors=True)
    # claude leaves the project directory (and a memory/ inside it) behind even
    # when every session file is gone. Only this exact name, and only if empty.
    for stale in (basedir / "memory", basedir):
        try:
            stale.rmdir()
        except OSError:
            pass   # something of someone else's is still in there: leave it
    shutil.rmtree(ROOT, ignore_errors=True)


def run_main(state, *argv: str) -> str:
    rc = cli.main(list(argv))
    out = state["capsys"].readouterr().out
    if argv and argv[0] in ("import", "continue", "merge") and \
            len(argv) > 1 and argv[1] == "session":
        # P1 (D-1): record our own new id the moment it is printed, even if the
        # command then fails. Never collect from the index: agora-test/ is shared,
        # and a sync has already pulled everyone else's sessions into this cache.
        for token in out.split():
            if token.startswith("agora:") and len(token) == len("agora:") + 26:
                if token not in state["created"]["printed"]:
                    state["created"]["printed"].append(token)
                break
    assert rc == 0, out[-500:]
    return out


def header_of(state, agora_id: str) -> dict:
    """The stored OKF header, read back the way the next command would see it.

    There is no `agora sync` any more: every command pushes the outbox and pulls
    Drive itself, so this calls the same store.sync the commands call.
    """
    store.sync(state["paths"])
    hdr = store.Index(state["paths"]).header(agora_id.split(":", 1)[1])
    assert hdr is not None, f"{agora_id} 不在索引裡"
    return hdr


def session_md(e2e, agora_id: str) -> str:
    """The session's own document on Drive, the way the next command sees it."""
    store.sync(e2e["paths"])
    ulid = agora_id.split(":", 1)[1]
    return (e2e["paths"].mirror / ulid / "session.md").read_text(encoding="utf-8")


def model_of(payload: dict) -> str | None:
    """The model the session last used, read straight out of the export."""
    for message in reversed(payload.get("messages") or []):
        info = message.get("info") or {}
        if info.get("role") == "assistant":
            return info.get("modelID") or None
    return None


def opencode_run(state, *args: str, timeout: int = 300) -> str:
    """`opencode run` in the project directory; returns stdout.

    Tries each candidate model in turn (see tests/fakes/opencode_noninteractive.py)
    and only skips when none of them answered.

    PWD is set explicitly: opencode picks the project from $PWD rather than the
    process working directory, so a caller that only sets `cwd=` would file the
    session under whatever directory the parent shell was in.
    """
    env = {**os.environ, "PWD": str(state["proj"])}
    proc, model, failures = agent.ask(list(args), cwd=str(state["proj"]),
                                      timeout=timeout, env=env)
    if proc is None:
        pytest.skip(agent.skip_reason(failures))
    print(f"[{os.path.basename(__file__)}] 來源 Session 用到模型：{model}", file=sys.stderr)
    return proc.stdout


def export_bytes(state, session_id: str) -> bytes:
    out = Path("/tmp/agora-it-e2e-oc/export.json")
    with open(out, "wb") as handle:
        proc = subprocess.run(["opencode", "export", session_id], cwd=str(state["proj"]),
                              stdout=handle, stderr=subprocess.DEVNULL,
                              env={**os.environ, "PWD": str(state["proj"])})
    assert proc.returncode == 0
    return out.read_bytes()


def test_import_search_continue_native_then_cross_agent(e2e):
    proj = e2e["proj"]
    created = e2e["created"]
    assert proj.name == "p_專案.v2" and proj.is_dir()

    # 1. self-made short dialogue with the free model, then import it.
    events = [json.loads(line) for line in
              opencode_run(e2e, "--format", "json", "--title", "e2e-oc", ASK).splitlines()
              if line.strip()]
    source_id = next(e["sessionID"] for e in events if "sessionID" in e)
    created["ses"].append(source_id)
    source_raw = export_bytes(e2e, source_id)
    source_before = len(json.loads(source_raw)["messages"])
    source_model = model_of(json.loads(source_raw))

    out = run_main(e2e, "import", "session", "--external-session-id", source_id,
                   "--agent", "opencode", "--header", "title=e2e 表格")
    id1 = out.split()[0]
    assert id1.startswith("agora:")
    created["ulids"].append(id1.split(":", 1)[1])

    # The OKF top level: type, the user's title, and who/what made it.
    hdr1 = header_of(e2e, id1)
    assert hdr1["type"] == "Session"
    assert hdr1["title"] == "e2e 表格" and "CSV" in hdr1["description"]
    assert source_model, "來源的 export 沒有 modelID，測試資料不對"
    assert hdr1["generated"]["by"] == f"opencode/{source_model}"
    assert hdr1["generated"]["at"].endswith("Z")
    assert hdr1["sources"][0]["id"] == f"opencode:{source_id}"
    assert hdr1["sources"][0]["author"] == f"opencode/{source_model}"

    # Everything the system owns lives under `agora` (design v4 3.4).
    sysblock = hdr1["agora"]
    assert sysblock["header"] == 2
    assert sysblock["relation"] == "import" and sysblock["parents"] == []
    assert sysblock["source"]["agent"] == "opencode"
    assert sysblock["source"]["session_id"] == source_id
    assert sysblock["source"]["dir"] == str(proj)   # N9: from the export, not the name
    assert sysblock["source"]["agent_version"][0].isdigit()
    assert sysblock["raw"]["md5"] and sysblock["raw"]["file"].startswith("raw-")
    body1 = session_md(e2e, id1)
    assert "CSV" in body1                            # Drive really has it

    # 2. search finds it through the index; the agora id comes first.
    out = run_main(e2e, "search", "session", "--filter", "text~=表格")
    assert any(line.split()[0] == id1 for line in out.splitlines() if line.split())
    out = run_main(e2e, "search", "session", "--filter", "agent=opencode",
                   "--filter", f"generated.by~=opencode/{source_model}")
    assert any(line.split()[0] == id1 for line in out.splitlines() if line.split())

    # 3. continue with opencode: native load (import with new ids), then the
    #    wrapper's headless run on that new session. The result goes back into the
    #    same agora session - the printed id is the one that was continued.
    out = run_main(e2e, "continue", "session", id1, "--agent", "opencode",
                   "--dir", str(proj))
    id2 = out.split()[0]
    assert id2 == id1, f"continue 應該寫回原本那個 session，卻印了 {id2}"

    hdr2 = header_of(e2e, id1)
    # it is still the imported session it was - only a continued *merge* becomes
    # "continue" - and it has no parent, because nothing branched
    assert hdr2["agora"]["relation"] == "import"
    assert hdr2["agora"]["parents"] == []
    forked = hdr2["agora"]["source"]["session_id"]
    assert forked.startswith("ses_") and forked != source_id
    created["ses"].append(forked)                  # teardown deletes it by id
    assert hdr2["agora"]["source"]["agent"] == "opencode"
    assert hdr2["agora"]["source"]["dir"] == str(proj)
    # the session it came from is remembered, so the import tab leaves it out
    assert hdr2["agora"]["previous_sources"] == [f"opencode:{source_id}"]
    assert hdr2["sources"][0]["id"] == f"opencode:{forked}"
    assert hdr2["generated"]["by"].startswith("opencode/")

    # the session we continued from is untouched on the agent side (spike V1a)
    assert len(json.loads(export_bytes(e2e, source_id))["messages"]) == source_before

    # the new agent session really carries the old transcript plus the new turn,
    # and it belongs to the project directory agora imported it into (spike V5)
    grown = json.loads(export_bytes(e2e, forked))
    assert len(grown["messages"]) >= source_before + 2
    assert grown["info"]["directory"].endswith("p_專案.v2")

    # and Drive's session.md is that longer conversation now
    body2 = session_md(e2e, id1)
    assert agent.QUESTION in body2, "session.md 裡沒有接續時問的那句"
    assert len(body2) > len(body1)

    # 4. cross agent: the same session continued by claude, again in place. The
    #    opencode raw is converted into a Claude jsonl and resumed (design v5),
    #    and the opencode session it replaces is remembered.
    out = run_main(e2e, "continue", "session", id1, "--agent", "claude",
                   "--dir", str(proj))
    id3 = out.split()[0]
    assert id3 == id1, f"換 agent 接續也該寫回同一個 session，卻印了 {id3}"

    hdr3 = header_of(e2e, id1)
    assert hdr3["agora"]["relation"] == "import"
    assert hdr3["agora"]["parents"] == []
    assert hdr3["agora"]["source"]["agent"] == "claude"
    assert hdr3["agora"]["previous_sources"] == [f"opencode:{source_id}",
                                                 f"opencode:{forked}"]
    assert hdr3["generated"]["by"].startswith("claude-code/")
    uuid3 = hdr3["agora"]["source"]["session_id"]
    uuid.UUID(uuid3)
    assert uuid3 in {json.loads(l)[json.loads(l).index("--resume") + 1]
                     for l in (e2e["fake_home"] / "e2e-args.log").read_text().splitlines()
                     if "--resume" in json.loads(l)}, \
        "wrapper 沒有記到這個 uuid，teardown 會漏掉它的 jsonl"
    assert cli.CONVERTED_NOTE in run_main(e2e, "show", "session", id1, "--raw")
    body3 = session_md(e2e, id1)
    assert len(body3) > len(body2), "claude 接續之後 session.md 沒有變長"

    # 5. what each agent was asked to do, from the wrappers' logs.
    oc_log = [json.loads(line) for line in
              (e2e["fake_home"] / "opencode-e2e-args.log").read_text().splitlines()]
    launches = [row for row in oc_log if "--session" in row["argv"]]
    assert [row["argv"] for row in launches] == [["--session", forked]]
    assert launches[0]["model"], "wrapper log 沒有記錄到用哪個模型"
    cl_launches = [json.loads(line) for line in
                   (e2e["fake_home"] / "e2e-args.log").read_text().splitlines()
                   if "--resume" in line or "--session-id" in line]
    assert len(cl_launches) == 1 and "--resume" in cl_launches[0]

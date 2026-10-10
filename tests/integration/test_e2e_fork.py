"""End to end for `agora continue session <id> --agent ... --fork`, through
agora.cli.main against real Drive (agora-test/) and the real agents.

Four tests, one per shape the fork has to hold:

1. claude, same agent: seed a self-made claude dialogue, import it as A, fork
   it with claude. B is a session of its own (title A plus -fork, relation
   fork, parents pointing at A); A's session.md on Drive is byte-identical
   afterwards; B's text carries the fork note and the new answer.
2. opencode, same agent: the same flow with opencode (free model chain).
3. cross agent: A comes from opencode, the fork runs under claude.
4. open and leave: the agent opens and says nothing. B is still on Drive and
   stdout prints B's id (exit 0).

Real calls per test: (1) and (2) cost two each - one to seed the source
dialogue, one for the fork's own run. (3) costs one opencode run plus one
`claude -p`. (4) costs one `claude -p` to seed; the fork's launch is a shim
that exits without speaking, so no model is asked.

Config pattern follows tests/integration/test_e2e_opencode.py: AGORA_CONFIG is
a temp dir whose rclone.conf symlinks to ~/.config/agora/rclone.conf (never
read, only referenced by path). opencode runs on a HOME that belongs to this
file (tests/integration/test_fork_real.py's oc_store pattern) with the XDG
variables unset, so its sessions never reach the user's own store; claude runs
on the person's real login through AGORA_CLAUDE_HOME, like the other e2e
files. Continues run through the committed non-interactive wrappers
(tests/fakes/*_noninteractive.py), except test 4's silent shim.

Cleanup (ids are recorded the moment they exist, so a mid-test failure still
tidies up): Drive sessions/<ULID> purged, own opencode session ids deleted
one by one, own claude jsonl uuids deleted, then the project tree. Only ids
this file printed or launched are touched: agora-test/ also holds other
people's sessions.

Write-only: this file is written, not run, here (issue #29 tasks 1.8).
Run: uv run pytest -q -m integration tests/integration/test_e2e_fork.py
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

from agora import cli, header as h, store
from agora.agents import claude as C

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import opencode_noninteractive as oc_shim  # noqa: E402  (needs the path above)
import claude_noninteractive as cl_shim  # noqa: E402  (its fixed question)

pytestmark = pytest.mark.integration

# pwd, not HOME: the opencode sandbox replaces HOME per test.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
OPENCODE_WRAPPER = Path(__file__).parent.parent / "fakes" / "opencode_noninteractive.py"
CLAUDE_WRAPPER = Path(__file__).parent.parent / "fakes" / "claude_noninteractive.py"
#: opencode answers through this model first (the PM's call); the wrapper's own
#: chain is the fallback when it does not.
MODEL = os.environ.get("AGORA_TEST_MODEL", oc_shim.PRIMARY)
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"
TIMEOUT = int(os.environ.get("AGORA_TEST_TIMEOUT", "300"))

ROOT = Path("/tmp/agora-it-e2e-fork")
PROJ = ROOT / "proj"

SEED_CLAUDE = "請用繁體中文說一句關於表格排版的話。只要文字回答，不要呼叫任何工具、不要寫檔案。"
SEED_OPENCODE = "把 CSV 轉成 Markdown 表格，先說第一步就好，不要真的動手做。"
TITLE = "e2e fork源"


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
    # Foreground uploads: the upload is done when the command returns, which is
    # what these assertions mean (the detached uploader has its own tests).
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    # opencode on a HOME of this file's own (test_fork_real.py's oc_store): its
    # sessions never reach the user's store; claude is unaffected, it resolves
    # everything through AGORA_CLAUDE_HOME.
    oc_home = tmp_path / "oc-home"
    oc_home.mkdir()
    monkeypatch.setenv("HOME", str(oc_home))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
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
    created = {"ulids": [], "ses": [], "uuids": [], "printed": []}
    state = {"proj": PROJ.resolve(), "paths": paths, "created": created,
             "fake_home": fake_home, "capsys": capsys, "tmp": tmp_path,
             "monkeypatch": monkeypatch}
    yield state

    _cleanup(state)


def _cleanup(state) -> None:
    created = state["created"]
    proj, paths = state["proj"], state["paths"]
    # opencode resolves the project from $PWD (measured): cleanup that only
    # sets cwd would look in the wrong project and leave sessions behind.
    env = {**os.environ, "PWD": str(proj)}

    log = state["fake_home"] / "opencode-e2e-args.log"
    if log.exists():
        for line in log.read_text().splitlines():
            argv = json.loads(line)["argv"]
            for flag in ("--session", "-s"):
                if flag in argv and argv[argv.index(flag) + 1].startswith("ses_"):
                    created["ses"].append(argv[argv.index(flag) + 1])

    for session_id in sorted(set(created["ses"])):  # one id at a time, never a pattern
        proc = subprocess.run(["opencode", "session", "delete", session_id],
                              cwd=str(proj), env=env, capture_output=True)
        if proc.returncode != 0:
            warnings.warn(f"delete {session_id} failed: {proc.stderr.decode()[-200:]}")

    # Only this file's own printed ids: agora-test/ is shared, and a sync has
    # already listed other people's sessions into this cache.
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

    claude_log = state["fake_home"] / "e2e-args.log"
    if claude_log.exists():
        for line in claude_log.read_text().splitlines():
            argv = json.loads(line)
            for flag in ("--resume", "--session-id"):
                if flag in argv:
                    created["uuids"].append(argv[argv.index(flag) + 1])
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for session_id in sorted(set(created["uuids"])):  # own jsonl uuids, one by one
        jsonl = basedir / f"{session_id}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        shutil.rmtree(basedir / session_id, ignore_errors=True)
        for extra in (cfgdir / "session-env" / session_id,
                      cfgdir / "file-history" / session_id):
            shutil.rmtree(extra, ignore_errors=True)
    for stale in (basedir / "memory", basedir):
        try:
            stale.rmdir()  # only this exact name, and only if empty
        except OSError:
            pass
    shutil.rmtree(ROOT, ignore_errors=True)


def run_main(state, *argv: str) -> str:
    rc = cli.main(list(argv))
    out = state["capsys"].readouterr().out
    if argv and argv[0] in ("import", "continue", "merge") and \
            len(argv) > 1 and argv[1] == "session":
        # Record our own new id the moment it is printed, even if the command
        # then fails. Never collect from the index: agora-test/ is shared.
        for token in out.split():
            if token.startswith("agora:") and len(token) == len("agora:") + 26:
                if token not in state["created"]["printed"]:
                    state["created"]["printed"].append(token)
                break
    assert rc == 0, out[-500:]
    return out


def header_of(state, agora_id: str) -> dict:
    """The stored OKF header, read back the way the next command would see it."""
    store.sync(state["paths"])
    hdr = store.Index(state["paths"]).header(agora_id.split(":", 1)[1])
    assert hdr is not None, f"{agora_id} 不在索引裡"
    return hdr


def session_md(state, agora_id: str) -> str:
    """The session's own document on Drive, the way the next command sees it."""
    store.sync(state["paths"])
    ulid = agora_id.split(":", 1)[1]
    return (state["paths"].mirror / ulid / "session.md").read_text(encoding="utf-8")


def drive_md5(state, agora_id: str) -> str:
    """The Drive md5 of one session's session.md (content check, not a listing)."""
    drive = store.Drive(state["paths"])
    files = drive.list_one(agora_id.split(":", 1)[1])
    return files.get("session.md", "")


def drive_session_names(state) -> set[str]:
    """The session folder names currently on Drive (names only, no contents)."""
    drive = store.Drive(state["paths"])
    out = drive._run("lsjson", "gdrive:sessions", "--dirs-only")
    return {e["Name"] for e in json.loads(out or "[]")}


def seed_claude(state, prompt: str) -> str:
    """One self-made short dialogue through the real CLI; returns its uuid."""
    sid = str(uuid.uuid4())
    state["created"]["uuids"].append(sid)
    env = {**os.environ, "HOME": str(REAL_HOME), "AGORA_CLAUDE_HOME": str(REAL_HOME)}
    proc = subprocess.run(
        ["claude", "--disallowedTools", DISALLOW, "--model", "haiku",
         "-p", "--session-id", sid, prompt],
        cwd=str(state["proj"]), env=env, capture_output=True, text=True,
        timeout=TIMEOUT)
    assert proc.returncode == 0, proc.stderr[-500:]
    return sid


def seed_opencode(state, ask_text: str, timeout: int = TIMEOUT) -> str:
    """One self-made short dialogue; returns its session id.

    AGORA_TEST_MODEL first, then the wrapper's own free-model chain. A skip
    that does not say why is a test nobody trusts, so every attempt is kept.
    """
    proj = state["proj"]
    env = {**os.environ, "PWD": str(proj),
           "OPENCODE_PERMISSION": oc_shim.PERMISSION}
    real = oc_shim.real_opencode()
    assert real is not None, "opencode 不在 PATH 上"
    attempts = [["-m", MODEL]] + [f for f in oc_shim.model_chain() if f[1] != MODEL]
    failures = []
    for flags in attempts:
        proc = subprocess.run(
            [real, "run", *flags, "--format", "json", "--title", "e2e-fork",
             ask_text],
            cwd=str(proj), env=env, capture_output=True, text=True,
            timeout=timeout)
        events = [json.loads(line) for line in (proc.stdout or "").splitlines()
                  if line.strip().startswith("{")]
        sids = [e["sessionID"] for e in events if "sessionID" in e]
        if proc.returncode == 0 and sids:
            state["created"]["ses"].append(sids[0])
            print(f"[{Path(__file__).name}] 來源 Session 用到模型：{flags[1]}",
                  file=sys.stderr)
            return sids[0]
        failures.append(f"{flags[1]}: rc={proc.returncode} "
                        f"{(proc.stderr or '').strip()[-120:]}")
    pytest.skip(oc_shim.skip_reason(failures))


def import_session(state, external_id: str, agent: str, title: str) -> str:
    out = run_main(state, "import", "session", "--external-session-id", external_id,
                   "--agent", agent, "--header", f"title={title}")
    agora_id = out.split()[0]
    assert agora_id.startswith("agora:")
    state["created"]["ulids"].append(agora_id.split(":", 1)[1])
    return agora_id


def claude_launches(state) -> list[list[str]]:
    """The wrapper's launch log: every --resume/--session-id the agent ran."""
    log = state["fake_home"] / "e2e-args.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()
            if "--resume" in line or "--session-id" in line]


def test_fork_same_agent_claude(e2e):
    proj = e2e["proj"]
    uuid_a = seed_claude(e2e, SEED_CLAUDE)
    id_a = import_session(e2e, uuid_a, "claude", TITLE)
    md5_a = drive_md5(e2e, id_a)
    body_a = session_md(e2e, id_a)

    out = run_main(e2e, "continue", "session", id_a, "--agent", "claude",
                   "--dir", str(proj), "--fork")
    id_b = out.split()[0]
    assert id_b.startswith("agora:") and id_b != id_a, f"fork 該是新的 Session，卻印了 {id_b}"

    hdr_a, hdr_b = header_of(e2e, id_a), header_of(e2e, id_b)
    assert hdr_b["title"] == hdr_a["title"] + "-fork"
    assert h.agora_of(hdr_b)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr_b)["parents"]] == [id_a]
    uuid_b = h.agora_of(hdr_b)["source"]["session_id"]
    uuid.UUID(uuid_b)
    assert uuid_b != uuid_a
    e2e["created"]["uuids"].append(uuid_b)   # the agent-side session this created
    assert any(s.get("id") == id_a for s in hdr_b.get("sources") or [])

    body_b = session_md(e2e, id_b)
    assert f"從 {id_a}" in body_b, "B 的內文沒有 fork 說明"
    assert len(body_b) > len(body_a), "B 的內文沒有新的回答"
    assert cl_shim.P_NATIVE.split("？")[0] in body_b, "B 的內文沒有接續時問的那句"
    assert drive_md5(e2e, id_a) == md5_a, "A 在 Drive 上被改動了"
    assert header_of(e2e, id_a)["title"] == TITLE
    assert uuid_b in {a[a.index("--resume") + 1] for a in claude_launches(e2e)
                      if "--resume" in a}, "fork 的那次啟動沒有跑起來"


def test_fork_same_agent_opencode(e2e):
    proj = e2e["proj"]
    sid_a = seed_opencode(e2e, SEED_OPENCODE)
    id_a = import_session(e2e, sid_a, "opencode", TITLE)
    md5_a = drive_md5(e2e, id_a)
    body_a = session_md(e2e, id_a)

    out = run_main(e2e, "continue", "session", id_a, "--agent", "opencode",
                   "--dir", str(proj), "--fork")
    id_b = out.split()[0]
    assert id_b.startswith("agora:") and id_b != id_a, f"fork 該是新的 Session，卻印了 {id_b}"

    hdr_a, hdr_b = header_of(e2e, id_a), header_of(e2e, id_b)
    assert hdr_b["title"] == hdr_a["title"] + "-fork"
    assert h.agora_of(hdr_b)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr_b)["parents"]] == [id_a]
    sid_b = h.agora_of(hdr_b)["source"]["session_id"]
    assert sid_b.startswith("ses_") and sid_b != sid_a
    e2e["created"]["ses"].append(sid_b)      # teardown deletes it by id
    assert h.agora_of(hdr_b)["source"]["agent"] == "opencode"
    assert h.agora_of(hdr_b)["source"]["dir"] == str(proj)
    assert hdr_b["generated"]["by"].startswith("opencode/")

    body_b = session_md(e2e, id_b)
    assert f"從 {id_a}" in body_b, "B 的內文沒有 fork 說明"
    assert oc_shim.QUESTION in body_b, "B 的內文沒有接續時問的那句"
    assert len(body_b) > len(body_a), "B 的內文沒有新的回答"
    assert drive_md5(e2e, id_a) == md5_a, "A 在 Drive 上被改動了"
    assert header_of(e2e, id_a)["title"] == TITLE


def test_fork_cross_agent_opencode_to_claude(e2e):
    proj = e2e["proj"]
    sid_a = seed_opencode(e2e, SEED_OPENCODE)
    id_a = import_session(e2e, sid_a, "opencode", TITLE)
    md5_a = drive_md5(e2e, id_a)
    body_a = session_md(e2e, id_a)

    out = run_main(e2e, "continue", "session", id_a, "--agent", "claude",
                   "--dir", str(proj), "--fork")
    id_b = out.split()[0]
    assert id_b.startswith("agora:") and id_b != id_a, f"fork 該是新的 Session，卻印了 {id_b}"

    hdr_b = header_of(e2e, id_b)
    assert hdr_b["title"] == TITLE + "-fork"
    assert h.agora_of(hdr_b)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr_b)["parents"]] == [id_a]
    assert h.agora_of(hdr_b)["source"]["agent"] == "claude"
    uuid_b = h.agora_of(hdr_b)["source"]["session_id"]
    uuid.UUID(uuid_b)
    e2e["created"]["uuids"].append(uuid_b)

    raw_b = run_main(e2e, "show", "session", id_b, "--raw")
    assert cli.CONVERTED_NOTE in raw_b, "跨 agent 的前文沒有經過轉換"
    body_b = session_md(e2e, id_b)
    assert f"從 {id_a}" in body_b, "B 的內文沒有 fork 說明"
    assert len(body_b) > len(body_a), "B 的內文沒有新的回答"
    assert drive_md5(e2e, id_a) == md5_a, "A 在 Drive 上被改動了"


SILENT_SHIM = """\
#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
from pathlib import Path

def main(argv):
    fake_home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-home"))
    fake_home.mkdir(parents=True, exist_ok=True)
    with open(fake_home / "e2e-args.log", "a") as f:
        f.write(json.dumps(argv) + "\\n")
    if "--resume" in argv:
        return 0  # the agent opened and left without a word
    real = shutil.which("claude")
    proc = subprocess.run([real, *argv], capture_output=True, text=True,
                          timeout=300)
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    return proc.returncode

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


def test_fork_open_and_leave_keeps_the_fork(e2e):
    """The agent opens and says nothing: B stays on Drive, stdout prints B."""
    proj = e2e["proj"]
    uuid_a = seed_claude(e2e, SEED_CLAUDE)
    id_a = import_session(e2e, uuid_a, "claude", TITLE)
    md5_a = drive_md5(e2e, id_a)

    shim = e2e["tmp"] / "claude-silent"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {e2e['tmp'] / 'silent_shim.py'} \"$@\"\n")
    (e2e["tmp"] / "silent_shim.py").write_text(SILENT_SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    e2e["monkeypatch"].setenv("AGORA_CLAUDE_CMD", str(shim))

    out = run_main(e2e, "continue", "session", id_a, "--agent", "claude",
                   "--dir", str(proj), "--fork")
    id_b = out.split()[0]
    assert id_b.startswith("agora:") and id_b != id_a

    hdr_b = header_of(e2e, id_b)
    assert h.agora_of(hdr_b)["relation"] == "fork"
    assert [p["id"] for p in h.agora_of(hdr_b)["parents"]] == [id_a]
    assert id_b.split(":", 1)[1] in drive_session_names(e2e), "B 沒有上 Drive"
    assert f"從 {id_a}" in session_md(e2e, id_b), "B 的內文沒有 fork 說明"
    assert drive_md5(e2e, id_a) == md5_a, "A 在 Drive 上被改動了"
    assert not (e2e["paths"].state / "unsaved-launches").exists(), \
        "fork 不寫 unsaved-launches：B 的來源就是這次啟動"

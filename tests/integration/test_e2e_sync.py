"""End to end for the periodic write-back (`agora continue --sync-period`),
through `python -m agora.cli` in a subprocess against real Drive (agora-test/)
and the scripted fake claude.

Five tests, one per spec Scenario this level can show without a real model:

1. 執行中看得到進度: the fake says the first sentence, waits for one round,
   says the second, then waits out several more rounds. While the run is still
   going, session.md on Drive already carries the first sentence; when the run
   ends it carries both. The id never changes.
2. 沒有新內容不寫: one sentence, then two idle rounds. Drive's session.md
   ModTime taken after the first upload lands is still the same at the end -
   nothing was staged, nothing re-uploaded - and no second session appears.
3. 途中被刪: after the second sentence lands on Drive, the test purges A's
   folder (playing the other machine; only this test's A). Exactly one rescued
   session appears, its parents name A, and the third sentence - said after the
   purge - lands in it. stderr explains the rescue.
4. --sync-period 0: `-1` and `abc` die with exit 1 before any agent opens;
   with 0 there is no periodic round (continue.log stays empty) and only the
   final write-back lands the sentence.
5. 不弄亂畫面 (spec Scenario + tasks 1.9): stdout and stderr go to one
   O_APPEND file; between FAKE-AGENT-START and FAKE-AGENT-END there is no
   agora output, while <state>/continue.log holds the rounds.

Harness: AGORA_CONFIG/CACHE/STATE live in tmp dirs, rclone.conf is only
symlinked (never read); AGORA_UPLOAD stays detached so uploads really happen
mid-run; AGORA_CLAUDE_HOME is sandboxed too - the fake needs no login, so
nothing real is touched except Drive's agora-test/. Cleanup records every id
the moment it exists (printed ids are harvested from the run's own output
file, rescues from the local index): Drive folders purged, own jsonl uuids
deleted one by one, then the project tree. A run that is still alive at
teardown is killed first, and the purge waits for the uploader to drain, so a
later command's recovery cannot resurrect a purged session after the purge.

Timing: --sync-period 0.01 is 0.6 s a round (design floor 0.1 s); every
periodic round appends exactly one line to continue.log - written or skipped -
which is what the fake's wait-sync paces on. A wait-sync releases on the
*staged* line, while Drive follows seconds later, so tests that need the agent
alive at a Drive landing hold it open with trailing waits - that is the
spec's「不等 agent 結束」.

Write-only: written, not run, here (issue #28 tasks 1.10).
Run: uv run pytest -q -m integration tests/integration/test_e2e_sync.py
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
import time
import uuid
import warnings

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C

pytestmark = pytest.mark.integration

# pwd, not HOME: nothing here needs the person's login (the agent is fake), but
# rclone's own config still lives under it.
REAL_CONF = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "agora" / "rclone.conf"
CLAUDE_FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_claude.py"

#: Minutes per round; 0.01 is 0.6 s, safely above the 0.1 s floor, and keeps
#: every run a matter of seconds (design「等待迴圈」: tests use tiny fractions).
PERIOD = "0.01"
PROC_TIMEOUT = 300.0     # a whole continue run, Drive latency included
POLL_TIMEOUT = 180.0     # one Drive landing
SETTLE_TIMEOUT = 180.0   # the detached uploader draining
#: How long test 2 watches Drive after the first upload lands: every upload the
#: run ever kicks lands inside it, so silence there means no upload happened.
IDLE_WATCH = 5.0

ROOT = Path("/tmp/agora-it-sync")
PROJ = ROOT / "proj"
TITLE = "e2e sync源"

#: Self-made filler. No ASCII comma anywhere: the fake splits its script on ",".
SEED_USER = "ZZSYNC 種子句：表格先對齊再輸出。"
SEED_ASST = "ZZSYNC 種子回答：好。"
FIRST = "ZZSYNC 第一句：逗號用引號包起來。"
SECOND = "ZZSYNC 第二句：表頭列只寫一次。"
THIRD = "ZZSYNC 第三句：存檔用 UTF-8。"


@pytest.fixture()
def sync(tmp_path, monkeypatch, capsys):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    if shutil.which("rclone") is None:
        pytest.fail("需要 rclone（上傳與 purge 都走它）")

    config = tmp_path / "config"
    config.mkdir()
    (config / "rclone.conf").symlink_to(REAL_CONF)
    monkeypatch.setenv("AGORA_CONFIG", str(config))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    # Detached uploads: the point of these tests is what lands mid-run, which
    # only happens when the command returns before the upload does.
    monkeypatch.delenv("AGORA_UPLOAD", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Sandboxed claude home: the fake needs no login, and agora resolves
    # everything through AGORA_CLAUDE_HOME, so the person's sessions are out
    # of reach by construction (no uuid of theirs is ever named).
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)

    shim = tmp_path / "claude-fake"  # exec through sys.executable: no shebang lottery
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} {CLAUDE_FAKE} \"$@\"\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(shim))

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
    created: dict = {"ulids": [], "uuids": [], "printed": [], "proc": None}
    state = {"proj": PROJ.resolve(), "paths": paths, "created": created,
             "fake_home": fake_home, "capsys": capsys, "tmp": tmp_path,
             "monkeypatch": monkeypatch}
    yield state

    _cleanup(state)


def _cleanup(state) -> None:
    created, proj, paths = state["created"], state["proj"], state["paths"]
    # A run still going would keep staging and uploading while the purge below
    # runs; kill it first so what follows is the run's end state.
    proc = created.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait()
    # A test that died mid-run still leaves its printed ids in the output file:
    # harvest them before anything is purged, or a rescue would go unpurged.
    out = created.get("out")
    if out is not None and Path(out).is_file():
        _harvest_ids(state, Path(out).read_text(encoding="utf-8", errors="replace"))
    # Drain the uploader before purging: purging under an active upload
    # resurrects the folder after the purge (T7 F4), and an orphaned stage
    # still uploads - under an id this file already recorded.
    try:
        wait_settled(paths, timeout=120.0)
    except AssertionError as e:
        warnings.warn(str(e))

    log = state["fake_home"] / "claude-args.log"  # the fake logs every launch
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                argv = json.loads(line)
            except ValueError:
                continue
            if "--resume" in argv:
                created["uuids"].append(argv[argv.index("--resume") + 1])
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for sid in sorted(set(created["uuids"])):  # own jsonl uuids, one by one
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        shutil.rmtree(basedir / sid, ignore_errors=True)
        for extra in (cfgdir / "session-env" / sid, cfgdir / "file-history" / sid):
            shutil.rmtree(extra, ignore_errors=True)
    for stale in (basedir / "memory", basedir):
        try:
            stale.rmdir()  # only this exact name, and only if empty
        except OSError:
            pass

    # Only sessions this run owns: what it printed, what it imported, and what
    # the local index shows as rescued out of those (a rescue whose id never
    # got printed is still ours - its parents say so).
    ulids = {u.split(":", 1)[1] for u in created["printed"]}
    ulids.update(created["ulids"])
    try:
        index = store.Index(paths)
        for ulid in list(ulids):
            ulids.update(index.children(ulid))
    except OSError:
        pass
    drive = store.Drive(paths)
    for ulid in sorted(ulids):
        proc = subprocess.run(
            ["rclone", "--config", str(REAL_CONF),
             "--drive-root-folder-id", drive.folder_id(),
             "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
        if proc.returncode != 0:
            warnings.warn(f"purge sessions/{ulid} failed: {proc.stderr.decode()[-200:]}")
    shutil.rmtree(ROOT, ignore_errors=True)


def wait_settled(paths: store.Paths, timeout: float = SETTLE_TIMEOUT) -> None:
    """The detached uploader is through: lock free, outbox and trash empty.

    Twice in a row - a run that has been started but has not taken the lock yet
    looks exactly like nobody working (review T3-it I4). Nudged like the next
    command would (review J1/J3); loud when it never settles.
    """
    deadline = time.monotonic() + timeout
    quiet, nudged = 0, 0.0
    while time.monotonic() < deadline:
        held = store.uploader_is_running(paths)
        waiting = bool(store.outbox_ulids(paths) or store.queued_for_trash(paths))
        if not held and waiting and time.monotonic() - nudged > 5.0:
            store.kick_uploader(paths)
            nudged = time.monotonic()
        quiet = quiet + 1 if (not held and not waiting) else 0
        if quiet >= 2:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"背景上傳器 {timeout:.0f} 秒還沒做完："
        f"鎖={store.uploader_is_running(paths)}"
        f" outbox={sorted(store.outbox_ulids(paths))}"
        f" 垃圾桶佇列={sorted(store.queued_for_trash(paths))}")


def poll_until(desc: str, timeout: float, fn, step: float = 2.0):
    """fn() until it returns something true; None past the deadline (no fail).

    Callers assert, so a timeout reads as what never happened rather than a
    bare expired loop.
    """
    end = time.monotonic() + timeout
    while True:
        if got := fn():
            return got
        if time.monotonic() >= end:
            return None
        time.sleep(step)


def run_cli(state, *argv: str) -> tuple[int, str, str]:
    """cli.main in this process (import, invalid input): fast, no agent."""
    code = cli.main(list(argv))
    out = state["capsys"].readouterr()
    return code, out.out.strip(), out.err


def start_continue(state, *argv: str) -> tuple[subprocess.Popen, Path]:
    """`python -m agora.cli` the way tasks 1.9 runs it: detached, and stdout
    plus stderr to one O_APPEND file the test reads back.

    A continue that is still going when the test wants Drive answers is why
    this is a subprocess: the mid-run purge and the mid-run reads happen here
    while agora runs there (cli.main would block this thread, and it must stay
    in the main thread for SIGINT handling).
    """
    out = state["tmp"] / f"continue-{uuid.uuid4().hex}.log"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agora.cli", *argv],
            cwd=str(state["proj"]), stdout=fd, stderr=fd,
            env={**os.environ, "PWD": str(state["proj"])},
            start_new_session=True)
    finally:
        os.close(fd)
    state["created"]["proc"] = proc
    state["created"]["out"] = out     # teardown harvests printed ids even on failure
    return proc, out


def _harvest_ids(state, text: str) -> None:
    """Record the agora ids the run printed (never the index: shared folder)."""
    for token in text.split():
        if token.startswith("agora:") and len(token) == len("agora:") + 26:
            if token not in state["created"]["printed"]:
                state["created"]["printed"].append(token)


def finish_proc(state, proc: subprocess.Popen, out: Path,
                timeout: float = PROC_TIMEOUT) -> tuple[int, str]:
    """Reap the run; harvest the agora ids it printed for teardown."""
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(f"continue {timeout:g} 秒還沒結束（輸出見 {out}）")
    text = out.read_text(encoding="utf-8", errors="replace")
    _harvest_ids(state, text)
    state["created"]["proc"] = None
    return rc, text


def last_id_of(text: str) -> str:
    """The last agora id the run printed: where the work ended up."""
    ids = [t for t in text.split()
           if t.startswith("agora:") and len(t) == len("agora:") + 26]
    assert ids, "這次接續沒有印出任何 agora id"
    return ids[-1]


def seed_jsonl(state, user_text: str, assistant_text: str) -> str:
    """A hand-written claude session under the sandboxed projects dir."""
    sid = str(uuid.uuid4())
    state["created"]["uuids"].append(sid)  # ours from the moment its file exists
    proj = state["proj"]
    stamp = "2026-10-10T00:00:00.000Z"
    user = {"type": "user", "sessionId": sid, "uuid": str(uuid.uuid4()),
            "parentUuid": None, "timestamp": stamp, "cwd": str(proj),
            "version": "9.9.9", "isSidechain": False, "userType": "external",
            "message": {"role": "user", "content": user_text}}
    assistant = {"type": "assistant", "sessionId": sid, "uuid": str(uuid.uuid4()),
                 "parentUuid": user["uuid"], "timestamp": stamp, "cwd": str(proj),
                 "version": "9.9.9", "requestId": "req_seed",
                 "isSidechain": False, "userType": "external",
                 "message": {"role": "assistant", "model": "zz-seed",
                             "content": [{"type": "text", "text": assistant_text}]}}
    path = C.projects_dir() / C.encode_project_dir(proj) / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(o, ensure_ascii=False)
                               for o in (user, assistant)) + "\n", encoding="utf-8")
    return sid


def import_session(state, sid: str, title: str) -> str:
    """Import the seed and wait until Drive really has it: continue refuses a
    session the cloud never confirmed, and every baseline below assumes the
    upload landed."""
    code, out, err = run_cli(state, "import", "session", "--external-session-id", sid,
                             "--agent", "claude", "--header", f"title={title}")
    assert code == 0, err[-500:]
    agora_id = out.split()[0]
    assert agora_id.startswith("agora:"), out[-200:]
    state["created"]["ulids"].append(agora_id.split(":", 1)[1])
    wait_settled(state["paths"])
    assert drive_md5(state, agora_id), "import 的上傳沒上 Drive"
    return agora_id


def drive_md5(state, agora_id: str) -> str:
    """The Drive md5 of one session's session.md (content check, not a listing)."""
    drive = store.Drive(state["paths"])
    return drive.list_one(agora_id.split(":", 1)[1]).get("session.md", "")


def drive_cat(state, ulid: str) -> str | None:
    """session.md straight off Drive; None while it is not there.

    rclone cat, not the mirror: the mirror is staged before the upload, so only
    Drive says what the periodic rounds have actually landed.
    """
    try:
        return store.Drive(state["paths"])._run(
            "cat", f"gdrive:sessions/{ulid}/session.md")
    except store.StoreError:
        return None


def drive_modtime(state, ulid: str) -> str | None:
    """session.md's ModTime on Drive; None while it is not there.

    A re-uploaded file gets the staged copy's fresh mtime, so an unchanged
    ModTime means no upload happened - an unchanged md5 alone would not say
    that, since identical bytes hash the same.
    """
    try:
        out = store.Drive(state["paths"])._run(
            "lsjson", f"gdrive:sessions/{ulid}", "--files-only")
    except store.StoreError:
        return None
    for entry in json.loads(out or "[]"):
        if entry.get("Name") == "session.md":
            return entry.get("ModTime")
    return None


def drive_names(state) -> set[str]:
    """The session folder names currently on Drive (names only, no contents)."""
    drive = store.Drive(state["paths"])
    out = drive._run("lsjson", "gdrive:sessions", "--dirs-only")
    return {e["Name"] for e in json.loads(out or "[]")}


def purge_ulid(state, ulid: str) -> None:
    """Delete one session folder off Drive (playing the other machine).

    Only a ulid this file recorded: the shared folder's neighbours are never
    named here. Missing already counts as gone (a purge and an upload racing
    is exactly what the mid-run test provokes on purpose).
    """
    drive = store.Drive(state["paths"])
    proc = subprocess.run(
        ["rclone", "--config", str(REAL_CONF),
         "--drive-root-folder-id", drive.folder_id(),
         "purge", f"gdrive:sessions/{ulid}"], capture_output=True, text=True)
    if proc.returncode != 0 and "not found" not in (proc.stderr or "").lower() \
            and "directory not found" not in (proc.stderr or ""):
        pytest.fail(f"purge sessions/{ulid} 失敗：{proc.stderr.strip()[-300:]}")


def header_of(state, agora_id: str) -> dict:
    """The stored OKF header, read back the way the next command would see it."""
    store.sync(state["paths"])
    hdr = store.Index(state["paths"]).header(agora_id.split(":", 1)[1])
    assert hdr is not None, f"{agora_id} 不在索引裡"
    return hdr


def session_md(state, agora_id: str) -> str:
    """The session's own document, through the mirror the next command reads."""
    store.sync(state["paths"])
    ulid = agora_id.split(":", 1)[1]
    return (state["paths"].mirror / ulid / "session.md").read_text(encoding="utf-8")


def script_env(state, *steps: str) -> None:
    """The fake's plan for this run; the signal is the log the rounds write."""
    state["monkeypatch"].setenv("FAKE_AGENT_SCRIPT", ",".join(steps))
    state["monkeypatch"].setenv(
        "FAKE_SYNC_SIGNAL", str(state["paths"].state / "continue.log"))


def test_progress_is_visible_while_the_run_is_still_going(sync):
    """spec「執行中看得到進度」: one round in, Drive already has the first
    sentence; at the end it has both, under the same id.

    The trailing waits hold the agent open past the upload latency: a wait-sync
    releases on the staged line while Drive follows seconds later, so without
    them the run would always end before anything lands (and the mid-run read
    below would be vacuous).
    """
    id_a = import_session(sync, seed_jsonl(sync, SEED_USER, SEED_ASST), TITLE)
    ulid_a = id_a.split(":", 1)[1]
    names_before = drive_names(sync)
    assert ulid_a in names_before

    script_env(sync, f"append:{FIRST}", "wait-sync", f"append:{SECOND}",
               *["wait-sync"] * 20, "exit")
    proc, out = start_continue(sync, "continue", "session", id_a,
                               "--agent", "claude", "--dir", str(sync["proj"]),
                               "--sync-period", PERIOD)
    # Mid-run, and the run still going: the test fails here when the first
    # upload only lands after the agent is already gone.
    landed = poll_until("Drive 上的 A 出現第一句", POLL_TIMEOUT,
                        lambda: (drive_cat(sync, ulid_a) or "").find(FIRST) >= 0
                        and proc.poll() is None)
    assert landed, "定期寫回沒在執行中把第一句傳上 Drive（接續結束才看得到的話，進度就沒意義）"

    rc, text = finish_proc(sync, proc, out)
    assert rc == 0, text[-2000:]
    wait_settled(sync["paths"])   # the last round's upload may still be in flight
    body = drive_cat(sync, ulid_a)
    assert body is not None and FIRST in body and SECOND in body, \
        "結束時 Drive 上的 A 該有兩句"
    assert last_id_of(text) == id_a, "同一個 Session 原地更新，不該另存"
    assert not (drive_names(sync) - names_before), \
        "除了 A，不該多出任何 Session（agora-test 是共用的，只看多出來的）"


def test_no_new_content_means_no_write(sync):
    """spec「沒有新內容不寫」: one sentence, then many idle rounds, then out.

    Every round logs exactly one line whether it wrote or skipped, so the
    trailing waits are what keeps the agent alive through the silence; what the
    run ever uploads all lands inside the watch below, so an unchanged ModTime
    at the end means no second upload - not a watch that ended early.
    """
    id_a = import_session(sync, seed_jsonl(sync, SEED_USER, SEED_ASST), TITLE)
    ulid_a = id_a.split(":", 1)[1]
    names_before = drive_names(sync)

    script_env(sync, f"append:{FIRST}", "wait-sync",
               *["wait-sync"] * 30, "exit")
    proc, out = start_continue(sync, "continue", "session", id_a,
                               "--agent", "claude", "--dir", str(sync["proj"]),
                               "--sync-period", PERIOD)
    assert poll_until("第一句上 Drive", POLL_TIMEOUT,
                      lambda: FIRST in (drive_cat(sync, ulid_a) or "")), \
        "第一句一直沒上 Drive，後面的「沒有再傳」量不到"
    md5_0, mod_0 = drive_md5(sync, id_a), drive_modtime(sync, ulid_a)
    assert mod_0, "讀不到 session.md 的 ModTime"
    print(f"[test_e2e_sync] 第一句上傳後：md5={md5_0} modtime={mod_0}")

    time.sleep(IDLE_WATCH)  # many rounds go by; the script speaks no more
    md5_1, mod_1 = drive_md5(sync, id_a), drive_modtime(sync, ulid_a)
    print(f"[test_e2e_sync] 靜默 {IDLE_WATCH:g} 秒後：md5={md5_1} modtime={mod_1}")
    assert mod_1 == mod_0, \
        f"沒有新內容卻重傳了 session.md（{mod_0} -> {mod_1}）"

    rc, text = finish_proc(sync, proc, out)
    assert rc == 0, text[-2000:]
    body = drive_cat(sync, ulid_a)
    assert body is not None and FIRST in body
    assert not (drive_names(sync) - names_before), \
        "不該多出任何 Session（agora-test 是共用的，只看多出來的）"
    assert drive_modtime(sync, ulid_a) == mod_0, "最後一次寫回也不該重傳"


def test_a_session_deleted_mid_run_is_rescued_exactly_once(sync):
    """spec「途中被刪」: purged after the second sentence lands, the work comes
    back as exactly one new session with A in its parents, and what is said
    afterwards lands in it.

    The purge goes out after the upload it replaces has landed, and is retried
    while the folder keeps coming back: a stage kicked before the purge can
    still recreate it once, but the content goes quiet after the third sentence
    (no new content, no more stages), so the loop converges instead of hiding a
    real second rescue.
    """
    id_a = import_session(sync, seed_jsonl(sync, SEED_USER, SEED_ASST), TITLE)
    ulid_a = id_a.split(":", 1)[1]
    names_before = drive_names(sync)
    assert ulid_a in names_before

    script_env(sync, f"append:{FIRST}", "wait-sync", f"append:{SECOND}", "wait-sync",
               f"append:{THIRD}", *["wait-sync"] * 12, "exit")
    proc, out = start_continue(sync, "continue", "session", id_a,
                               "--agent", "claude", "--dir", str(sync["proj"]),
                               "--sync-period", PERIOD)
    assert poll_until("第二句上 Drive", POLL_TIMEOUT,
                      lambda: SECOND in (drive_cat(sync, ulid_a) or "")), \
        "第二句一直沒上 Drive，刪掉就不是「途中」了"

    gone = False
    for _ in range(3):
        purge_ulid(sync, ulid_a)
        if poll_until(f"{id_a} 從 Drive 消失", 30.0,
                      lambda: ulid_a not in drive_names(sync), step=1.0):
            gone = True
            break
        time.sleep(2.0)
    assert gone, "purge 三次 A 還在 Drive 上，後面的另存量不到"

    rc, text = finish_proc(sync, proc, out)
    assert rc == 0, text[-2000:]
    wait_settled(sync["paths"])   # the rescue's upload has to land before we look

    names_after = drive_names(sync)
    assert ulid_a not in names_after, "A 被刪掉後又被傳回去了"
    new = names_after - names_before
    assert len(new) == 1, f"該只多出一個另存的 Session，卻多了 {sorted(new)}"
    id_b = f"agora:{next(iter(new))}"
    assert [p["id"] for p in h.agora_of(header_of(sync, id_b))["parents"]] == [id_a], \
        "另存的 parents 該指回 A"
    body_b = session_md(sync, id_b)
    assert THIRD in body_b, "刪掉之後說的第三句該寫到另存的那個"
    assert last_id_of(text) == id_b, "最後該印另存的 id"
    assert "另存" in text, "結束時該印出另存的說明"


def test_sync_period_zero_means_only_the_final_write(sync):
    """spec「不自動同步」(+「不合法的間隔」): bad values die with exit 1 before
    any agent opens; with 0 no periodic round runs and only the final
    write-back lands the sentence."""
    id_a = import_session(sync, seed_jsonl(sync, SEED_USER, SEED_ASST), TITLE)

    for bad in ("-1", "abc"):
        code, _out, err = run_cli(sync, "continue", "session", id_a,
                                  "--agent", "claude", "--dir", str(sync["proj"]),
                                  "--sync-period", bad)
        assert code == 1, f"--sync-period {bad} 該 exit 1，卻是 {code}：{err[-300:]}"
    log = sync["fake_home"] / "claude-args.log"
    launches = [line for line in log.read_text().splitlines()] if log.exists() else []
    assert not any("--resume" in line for line in launches), \
        "間隔不合法時不該開 agent"

    script_env(sync, f"append:{FIRST}", "exit")  # no wait-sync: nothing periodic to wait for
    code, out, err = run_cli(sync, "continue", "session", id_a,
                             "--agent", "claude", "--dir", str(sync["proj"]),
                             "--sync-period", "0")
    assert code == 0, err[-500:]
    assert out.split()[0] == id_a

    continue_log = sync["paths"].state / "continue.log"
    assert not continue_log.exists() or continue_log.stat().st_size == 0, \
        "sync-period 0 不該有任何定期寫回的記錄"
    wait_settled(sync["paths"])   # 結束時的寫回是背景上傳的，等它落地再看 Drive
    body = session_md(sync, id_a)
    assert FIRST in body, "結束時的寫回該把第一句存進 A"
    assert drive_md5(sync, id_a), "結束時的寫回該上傳"


def test_the_terminal_stays_quiet_while_the_agent_runs(sync):
    """spec「不弄亂畫面」(tasks 1.9 同形): stdout and stderr share one O_APPEND
    file; between the agent's own START and END marks there is no agora output,
    while continue.log holds the rounds."""
    id_a = import_session(sync, seed_jsonl(sync, SEED_USER, SEED_ASST), TITLE)

    script_env(sync, f"append:{FIRST}", "wait-sync", f"append:{SECOND}", "exit")
    proc, out = start_continue(sync, "continue", "session", id_a,
                               "--agent", "claude", "--dir", str(sync["proj"]),
                               "--sync-period", PERIOD)
    rc, text = finish_proc(sync, proc, out)
    assert rc == 0, text[-2000:]

    lines = text.splitlines()
    start = next((n for n, line in enumerate(lines) if line.strip() == "FAKE-AGENT-START"), None)
    end = next((n for n, line in enumerate(lines) if line.strip() == "FAKE-AGENT-END"), None)
    assert start is not None and end is not None and start < end, \
        "假 agent 的起止標記該在輸出裡（前後各一個）"
    between = [line for line in lines[start + 1:end] if line.strip()]
    assert not any(line.lstrip().startswith("[agora]") for line in between), \
        f"agent 執行中終端機出現了 agora 的輸出：{between[:5]}"
    assert between == [], \
        f"起止標記之間該只有假 agent 自己的標記，卻多了：{between[:5]}"

    continue_log = sync["paths"].state / "continue.log"
    assert continue_log.is_file() and continue_log.stat().st_size > 0, \
        "定期寫回的記錄該在 continue.log，不在終端機"
    wait_settled(sync["paths"])   # the last stage's upload may still be in flight
    body = session_md(sync, id_a)
    assert FIRST in body and SECOND in body, "這次接續的兩句該都存進 A"

"""Integration: measure, do not assert - what the real agents do while nobody is
talking (continue-periodic-sync tasks 1.1, design P10).

The periodic write-back reads the agent's raw every five minutes and stages it
when it differs from the last one it wrote. Before that promise can be made,
three things have to be measured rather than assumed, and none of them has been:

* `opencode export` while the session is idle - is it byte-stable? design「一次寫回」
  P6 leans on "nothing changed" meaning the bytes are the same, and a timestamp or
  a token counter that moves on its own would make every five minutes look like a
  change.
* `opencode export` while the same session is being written to - does it fail, block,
  or return a half-written payload? (design P10)
* Claude, idle: does the jsonl keep growing? A line that is not a message (cost
  state, mode, last-prompt) still changes the raw, so "no new content" and "no
  change on disk" are not the same thing (design P10, P6's cheap pre-check).

Every number this file prints is an observation, and the assertions are only the
ones that must hold for the measurement to mean anything (a session exists, an
export succeeded, an answer came back). Run it with `-s` or read the log: the
output is the deliverable.

Run (never automatically - these call the real agents, and the claude one sleeps):
    uv run pytest -q -s -rA -m integration tests/integration/test_sync_measure.py

    AGORA_MEASURE_IDLE_SECONDS=20   how long to leave claude idle (default 20)
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import pwd
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from agora.agents import claude as C

pytestmark = pytest.mark.integration


# --- opencode 2.x: the 1.x command line (opencode export/import) is gone and the
# 2.x one needs a server context per call; these tests are rewritten in step 5 of
# change agora-opencode-v2 and skip until then (tasks 1.11). Without this they do
# not skip on a 2.x machine, they run and fail - or reach for the shared managed
# service (port 49374) while doing it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import opencode_noninteractive as _oc_probe  # noqa: E402  (needs the path above)

if _oc_probe.session_export_supported():
    pytest.skip("opencode 2.x：整合測試還在用 1.x 的指令，第 5 步更新（tasks 1.11）",
                allow_module_level=True)

CLAUDE = shutil.which("claude")
OPENCODE = shutil.which("opencode")
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"
MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")
TIMEOUT = int(os.environ.get("AGORA_TEST_TIMEOUT", "300"))
#: Long enough that claude would have written anything it is going to write, short
#: enough to sit through. Overridable so a PM can watch a longer idle window.
IDLE_SECONDS = float(os.environ.get("AGORA_MEASURE_IDLE_SECONDS", "20"))

OC_PROJ = Path("/tmp/agora-it-sync/oc_proj")
CL_PROJ = Path("/tmp/agora-it-sync/cl_proj")
PROMPT = "用一句話回答：CSV 的表頭列怎麼寫？不要呼叫任何工具、不要寫檔案。"
FOLLOWUP = "再說一次同樣的答案，請用不同的一句話。不要呼叫任何工具、不要寫檔案。"


def say(text: str) -> None:
    print(f"[measure] {text}", file=sys.stderr)


def git_repo(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=agora@example.invalid",
                    "-c", "user.name=agora", "commit", "-q", "--allow-empty",
                    "-m", "init"], cwd=path, check=True)
    return path.resolve()


def differing_fields(left: bytes, right: bytes) -> list[str]:
    """Which top-level JSON fields differ between two exports, by name only.

    Names, never values: a session's text is not something to print into a log
    (design 7). Unparseable on either side falls back to "不是 JSON", which is
    itself the answer worth seeing.
    """
    try:
        one, two = json.loads(left), json.loads(right)
    except (ValueError, UnicodeDecodeError):
        return ["<不是 JSON>"]
    if not isinstance(one, dict) or not isinstance(two, dict):
        return ["<最外層不是物件>"]
    names = sorted(set(one) | set(two))
    changed = [name for name in names if one.get(name) != two.get(name)]
    # info is one object with a dozen fields; naming the field inside it is the
    # useful part (a moving time.updated is what would break the md5 pre-check).
    if "info" in changed and isinstance(one.get("info"), dict) and isinstance(two.get("info"), dict):
        inside = sorted(k for k in set(one["info"]) | set(two["info"])
                        if one["info"].get(k) != two["info"].get(k))
        changed = [name for name in changed if name != "info"] + \
                  [f"info.{k}" for k in inside]
    if "messages" in changed and isinstance(one.get("messages"), list) \
            and isinstance(two.get("messages"), list):
        changed = [name for name in changed if name != "messages"] + \
                  [f"messages[{n}]" for n in
                   range(max(len(one["messages"]), len(two["messages"])))
                   if _norm(one["messages"], n) != _norm(two["messages"], n)]
    return changed


def _norm(messages: list, index: int) -> str:
    """One message as text, or a marker for "this side has no message here"."""
    return json.dumps(messages[index], sort_keys=True, ensure_ascii=False) \
        if index < len(messages) else "<沒有>"


# --- opencode ---------------------------------------------------------------

@pytest.fixture(scope="module")
def oc_home(tmp_path_factory):
    """A HOME that belongs to this module, with an empty opencode database.

    test_opencode_real.py OC4: these sessions must not land in the user's own
    opencode store, and teardown deletes exactly the ids this run recorded.
    """
    return tmp_path_factory.mktemp("sync-measure-home")


@pytest.fixture
def oc_env(oc_home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(oc_home))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    scratch = tmp_path / "exports"
    scratch.mkdir()
    return {"home": str(oc_home), "proj": git_repo(OC_PROJ), "scratch": scratch}


@pytest.fixture
def oc_trash(oc_env):
    """Delete the ids this run made, one at a time - never a pattern, never a list."""
    if OPENCODE is None:
        pytest.skip("opencode not installed")
    recorded: list[str] = []
    yield recorded
    for session_id in sorted(set(recorded)):
        subprocess.run([OPENCODE, "session", "delete", session_id],
                       cwd=str(OC_PROJ), env={**os.environ, "PWD": str(OC_PROJ)},
                       capture_output=True)
    shutil.rmtree(OC_PROJ, ignore_errors=True)


def opencode_run(env: dict, *args: str) -> subprocess.CompletedProcess:
    """`opencode run` in the sandboxed HOME, in the throwaway project.

    PWD as well as cwd: opencode resolves its project from $PWD, and without it
    the session lands under whatever directory the test runner started in.
    """
    return subprocess.run([OPENCODE, "run", "-m", MODEL, *args],
                          cwd=str(env["proj"]), env={**os.environ, "PWD": str(env["proj"]),
                                                     "HOME": env["home"],
                                                     "OPENCODE_PERMISSION": '{"*":"deny"}'},
                          capture_output=True, text=True, timeout=TIMEOUT)


def export_bytes(session_id: str, env: dict) -> bytes:
    """`opencode export` through a file, timed - never a pipe (spike V1).

    The file lands outside the project: it is a measurement artefact, and opencode
    files the session under `$PWD`, so a stray json there would be indistinguishable
    from something the session said.
    """
    target = env["scratch"] / f"export-{uuid.uuid4().hex}.json"
    started = time.monotonic()
    with open(target, "wb") as out:
        proc = subprocess.run([OPENCODE, "export", session_id], cwd=str(env["proj"]),
                              stdout=out, stderr=subprocess.PIPE,
                              env={**os.environ, "PWD": str(env["proj"]), "HOME": env["home"]},
                              timeout=60)
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        target.unlink(missing_ok=True)
        raise AssertionError(f"opencode export {session_id} 失敗（{elapsed:.2f} 秒）：{detail}")
    blob = target.read_bytes()
    target.unlink()
    return blob


def make_opencode_session(env: dict, trash: list[str]) -> str:
    proc = opencode_run(env, "--format", "json", PROMPT)
    assert proc.returncode == 0, proc.stderr[-400:]
    session_id = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("sessionID"):
            session_id = event["sessionID"]     # recorded before anything can fail
    assert session_id, "這次 run 的事件裡沒有 session id"
    trash.append(session_id)
    return session_id


def test_opencode_export_is_byte_stable_while_the_session_sits_idle(oc_env, oc_trash):
    """design「一次寫回」P6: the write-back skips staging when the raw has not
    changed. For opencode that means comparing two exports of an idle session.

    If they differ, the difference is in a field that moves on its own (a time
    stamp, a token count) and the md5 pre-check would stage a copy every round
    for nothing - which is the 2 GB of Drive trash in design's Risks.
    """
    env = oc_env
    session_id = make_opencode_session(env, oc_trash)

    first = export_bytes(session_id, env)
    say(f"opencode 匯出大小：{len(first)} bytes（session {session_id}）")
    time.sleep(2)
    second = export_bytes(session_id, env)

    same = first == second
    say(f"閒置時兩次 export 是否 byte 相同：{same}"
        + ("" if same else f"；不同的欄位：{differing_fields(first, second)}"))
    assert first and second, "export 是空的，量到的東西沒有意義"
    # The measurement, not the promise: P6's "nothing changed" is only safe if
    # this holds, and the answer belongs in the design rather than in an assert.


def test_opencode_export_while_the_session_is_being_written_to(oc_env, oc_trash):
    """design P10: the write-back exports while the agent is running, not only
    between turns.

    What matters is whether the export fails, blocks, or hands back a payload
    that is halfway through a message - the last would be a corrupt raw on Drive.
    So the export is run repeatedly while a real turn is in flight, and every one
    of them is parsed and counted.
    """
    env = oc_env
    session_id = make_opencode_session(env, oc_trash)
    say(f"匯出大小（動手前）：{len(export_bytes(session_id, env))} bytes")

    running = subprocess.Popen(
        [OPENCODE, "run", "-m", MODEL, "-s", session_id, "--format", "json", FOLLOWUP],
        cwd=str(env["proj"]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PWD": str(env["proj"]), "HOME": env["home"],
             "OPENCODE_PERMISSION": '{"*":"deny"}'})
    started = time.monotonic()
    attempts: list[str] = []
    try:
        while running.poll() is None and time.monotonic() - started < TIMEOUT:
            mark = time.monotonic()
            try:
                blob = export_bytes(session_id, env)
                messages = json.loads(blob)["messages"]
                attempts.append(f"{time.monotonic() - mark:.2f}s "
                                f"{len(messages)} 則／{len(blob)} bytes")
            except AssertionError as e:      # a refusal is the answer, not a crash
                attempts.append(f"{time.monotonic() - mark:.2f}s 失敗：{e}")
            except (ValueError, KeyError, TypeError) as e:   # half-written payload
                attempts.append(f"{time.monotonic() - mark:.2f}s 解析不了：{e}")
            time.sleep(1)
    finally:
        try:
            running.wait(timeout=30)
        except subprocess.TimeoutExpired:
            running.kill()
            running.wait()

    say(f"另一輪對話進行中的 {len(attempts)} 次 export：")
    for line in attempts:
        say(f"    {line}")
    broken = [line for line in attempts if "失敗" in line or "解析不了" in line]
    # Not an assert: whether a parallel export fails, blocks or returns a payload
    # that is halfway through a message is the measurement design P10 asks for,
    # and the answer belongs in the design. Only a run with no samples at all
    # means nothing was measured.
    say(f"opencode export 與寫入並行：{len(attempts)} 次，失敗或讀不出來 {len(broken)} 次"
        + ("（設計要照這個結果決定怎麼處理）" if broken else "（看來並行是安全的）"))
    assert attempts, "對話太快結束，一次 export 都沒排到，什麼都沒量到"
    say(f"匯出大小（動手後）：{len(export_bytes(session_id, env))} bytes")


# --- claude -----------------------------------------------------------------

@pytest.fixture
def cl_env(monkeypatch):
    """The real login for the real claude, a /tmp project for its conversation."""
    if CLAUDE is None:
        pytest.skip("claude CLI not found")
    home = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("AGORA_CLAUDE_HOME", home)
    proj = git_repo(CL_PROJ)
    created: list[str] = []
    yield {"proj": proj, "env": {**os.environ, "HOME": home, "AGORA_CLAUDE_HOME": home},
           "created": created}
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for sid in created:                     # only our own uuids, one by one
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = basedir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        for extra in [cfgdir / "session-env" / sid, cfgdir / "file-history" / sid]:
            if extra.is_dir():
                shutil.rmtree(extra)
    for leftover in [basedir / "memory", basedir]:
        try:
            leftover.rmdir()
        except OSError:
            pass
    shutil.rmtree(CL_PROJ.parent, ignore_errors=True)


def claude_path(env: dict, session_id: str) -> Path:
    return C.projects_dir() / C.encode_project_dir(env["proj"]) / f"{session_id}.jsonl"


def _parses(line: str) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False      # a half-written line is itself a finding, so it is counted below


def test_claude_jsonl_does_not_grow_while_the_session_sits_idle(cl_env):
    """design P10: does an idle claude append lines that are not messages?

    `cost-state`, `mode`, `last-prompt` and friends would change the raw without
    anyone having spoken, so a raw md5 comparison has to know about them: either
    they are ignored, or P6's cheap pre-check (size + mtime) is not enough.
    """
    env = cl_env
    session_id = str(uuid.uuid4())
    env["created"].append(session_id)
    proc = subprocess.run([CLAUDE, "--disallowedTools", DISALLOW, "--model", "haiku",
                           "-p", "--session-id", session_id, PROMPT],
                          cwd=env["proj"], env=env["env"],
                          capture_output=True, text=True, timeout=TIMEOUT)
    assert proc.returncode == 0, proc.stderr[-500:]

    path = claude_path(env, session_id)
    before = path.stat()
    lines_before = path.read_text(encoding="utf-8").splitlines()
    say(f"claude jsonl 大小：{before.st_size} bytes／{len(lines_before)} 行"
        f"（session {session_id}）")

    time.sleep(IDLE_SECONDS)

    after = path.stat()
    lines_after = path.read_text(encoding="utf-8").splitlines()
    new_lines = [line for line in lines_after[len(lines_before):] if line.strip()]
    appended = [json.loads(line).get("type", "?") for line in new_lines if _parses(line)]
    unreadable = len(new_lines) - len(appended)
    say(f"claude 閒置 {IDLE_SECONDS:g} 秒："
        f"大小 {before.st_size} -> {after.st_size} bytes、"
        f"行數 {len(lines_before)} -> {len(lines_after)}、"
        f"mtime 變了：{after.st_mtime_ns != before.st_mtime_ns}、"
        f"新增的行：{appended or '（沒有）'}"
        + (f"，其中 {unreadable} 行解析不了" if unreadable else ""))
    say("（cost-state / mode / last-prompt 這類不是訊息的行也算：raw 的 md5 會變，"
        "P6 的預檢得知道這件事；raw 大小見上一行）")

    # The file has to stay readable, or the periodic export is reading a half-written
    # line rather than a finished conversation (design P10, `_split_lines`).
    assert C._read_lines(path) is not None, "閒置之後這個 jsonl 讀不了"
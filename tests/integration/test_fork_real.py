"""Integration: `append_native` - a same-agent fork - against the real agents.

continue-fork tasks 1.7 and review C2, C6, C7: the three things the unit tests
cannot answer, because the question is what the *real* agent does with what we
hand it.

* claude, C2: A ends on a `tool_use` that never got its `tool_result` - a run
  killed mid-tool. `append_native` puts one stand-in assistant turn (NO_REPLY) in
  front of the fork's two turns; whether the API accepts the unpaired call is
  something only a real `claude --resume` can say. The second shape review C2
  named is measured too: two calls in parallel, the *second* one answered, the
  first not - which is the case the detector misses.
* claude, C6: A's last line carrying a uuid is a `system` or an `attachment`
  rather than a message (real files end like that). `_leaf_uuid` anchors the
  fork's chain there; if claude does not put those lines in the conversation
  chain the agent opens with no history at all and says nothing about it. So the
  fork is asked what A's first sentence was, and the answer has to carry a word
  that only exists in that sentence.
* opencode, C7: what happens to the part an undo pointer names - first ours
  (`_apply_revert`), then opencode's own cleanup, measured by importing a payload
  that still carries the pointer and letting the session be spoken to again.

Every session here is written by this file: a hand-made claude jsonl under a
/tmp project (in the real ~/.claude, which is the only place the real claude will
look, cleaned up by uuid afterwards), and a hand-made opencode export imported
into a throwaway git repo under /tmp with an isolated opencode database. Prompt
text is self-authored filler; nothing here reads a real transcript.

Where the real agent refuses, the message is printed (credentials scrubbed) and
the test is marked xfail with what would have to change - an xfail that says why,
not a red test nobody can read.

Run (never automatically - these call the real agents):
    uv run pytest -q -s -rA -m integration tests/integration/test_fork_real.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import functools
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agora import cli
from agora.agents import claude as C
from agora.agents import opencode as oc
from agora.agents.base import NO_REPLY

pytestmark = pytest.mark.integration

CLAUDE = shutil.which("claude")
OPENCODE = shutil.which("opencode")
# E6, same list test_claude_real.py uses: the fork note is plain text, so no tool
# is ever needed, and none of them may reach the file system.
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"
TIMEOUT = int(os.environ.get("AGORA_TEST_TIMEOUT", "300"))

PROJ = Path("/tmp/agora-it-fork/p_專案.v2")
MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")

#: A source id only ever printed into the fork note this file hands the agent.
SOURCE_ID = "agora:01JQ8Z5M7XR2N4V6P8T0Y2B4C6"
FORK = cli._fork_turns(SOURCE_ID, {"title": "表格排版"})
NOTE, READY = cli.FORK_NOTE.format(id=SOURCE_ID, header="title: 表格排版"), cli.FORK_READY

#: The word only A's first sentence carries, asked about again in the C6 test.
MARK = "ZZMANGO7411"


def say(name: str, text: str) -> None:
    print(f"[{name}] {text}", file=sys.stderr)


# --- the three answers, printed -------------------------------------------

#: The public repo keeps no secrets and neither does a terminal: the refusals we
#: print go to a PM's terminal and into a CI log.
_CREDENTIAL = re.compile(r"sk-[A-Za-z0-9_-]{6,}|Bearer\s+\S+|[A-Za-z0-9_-]{40,}")


def safe(detail: str, keep: int = 1200) -> str:
    """The tail of an agent's error message, with anything key-shaped taken out."""
    return _CREDENTIAL.sub("<redacted>", detail or "").strip()[-keep:]


#: What an API refusal of an unpaired tool_use looks like (review C2).
REFUSAL_MARKERS = ("tool_use", "tool_result", "invalid_request_error", "400")


def refusal(proc: subprocess.CompletedProcess) -> str | None:
    """The refusal text when the agent turned this fork down, else None.

    Anything else - a network error, an expired login - is not what this file
    measures, so it stays an ordinary failure with its own message.
    """
    if proc.returncode == 0:
        return None
    detail = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    return detail if any(marker in detail for marker in REFUSAL_MARKERS) else None


# --- claude: a session written by hand -------------------------------------

FIRST = f"{MARK} 是這個專案的代號，請記住。"
QUESTION_A = "只回答 OK 這兩個字，不要呼叫任何工具、不要寫檔案。"
QUESTION_B = "這段對話的第一句話提到一個專案代號，那個代號是什麼？只回答代號本身。"


@functools.lru_cache(maxsize=1)
def claude_version() -> str:
    """The real CLI's version, carried on the hand-written lines for realism.

    It is metadata claude does not check; read it rather than pin a number here
    that goes stale.
    """
    proc = subprocess.run([CLAUDE, "--version"], capture_output=True, text=True, timeout=60)
    return (proc.stdout or "").strip().splitlines()[0] if proc.returncode == 0 else "unknown"


class Maker:
    """Hand-written claude jsonl lines for one session under one project.

    The field set is cl-basic.jsonl's: what the real file carries, nothing the
    adapter invents, because `--resume` reads these lines rather than anything we
    say about them.
    """

    def __init__(self, proj: Path, session_id: str) -> None:
        self.proj = proj
        self.session_id = session_id
        self.version = claude_version()
        self.at = datetime(2026, 10, 9, 3, 0, 0, tzinfo=timezone.utc)
        self.lines: list[dict] = []

    def _stamp(self, seconds: int) -> str:
        return (self.at + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def said(self, kind: str, uuid_: str, parent: str | None, content, seconds: int) -> dict:
        line = {"type": kind, "sessionId": self.session_id, "uuid": uuid_,
                "parentUuid": parent, "timestamp": self._stamp(seconds),
                "cwd": str(self.proj), "version": self.version,
                "isSidechain": False, "userType": "external",
                "entrypoint": "cli", "gitBranch": "main"}
        if kind == "assistant":
            line["requestId"] = f"req_{uuid_[:8]}"
        line["message"] = {"role": kind, "content": content}
        self.lines.append(line)
        return line

    def plain(self, uuid_: str, parent: str | None, seconds: int, **extra) -> dict:
        """A line that is not part of the conversation: system, attachment, ..."""
        line = {"type": extra.pop("type"), "sessionId": self.session_id, "uuid": uuid_,
                "parentUuid": parent, "timestamp": self._stamp(seconds),
                "isSidechain": False, "userType": "external", "entrypoint": "cli"}
        self.lines.append(line)
        line.update(extra)
        return line

    def write(self) -> str:
        path = C.projects_dir() / C.encode_project_dir(self.proj) / f"{self.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in self.lines) + "\n",
                        encoding="utf-8")
        return str(path)

    def raw(self) -> bytes:
        """A's own raw, read back through the adapter - the fork's input.

        Goes through `export` rather than `_pack_raw` so the fork is handed the
        same bytes the rest of agora would have.
        """
        self.write()
        return C.ADAPTER.export(self.session_id, hint_dir=self.proj).raw


def fork_and_load(adapter, raw: bytes, proj: Path, trash: list[str]):
    """`append_native` -> `start_native`, the two steps `continue --fork` runs."""
    forked = adapter.append_native(raw, FORK)
    launch = adapter.start_native(forked, proj)
    trash.append(launch.agent_session_id)   # recorded now, cleaned even if the run below fails
    return forked, launch


def run_claude(live, *argv: str) -> subprocess.CompletedProcess:
    argv = ["--disallowedTools", DISALLOW, *argv]
    return subprocess.run([CLAUDE, *argv], cwd=live["proj"], env=live["env"],
                          capture_output=True, text=True, timeout=TIMEOUT)


@pytest.fixture()
def live(monkeypatch):
    """The real login for the real claude, a /tmp project for its conversation.

    HOME follows pwd, not the sandbox's HOME (E7): the credentials belong to the
    person, and only the jsonl we write is ours.
    """
    if CLAUDE is None:
        pytest.skip("claude CLI not found")
    home = pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("AGORA_CLAUDE_HOME", home)
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=PROJ, check=True)
    proj = PROJ.resolve()   # /tmp -> /private/tmp on macOS
    env = {**os.environ, "HOME": home, "AGORA_CLAUDE_HOME": home}
    created: list[str] = []
    yield {"proj": proj, "env": env, "created": created}
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for sid in created:            # only our own uuids, one by one
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = basedir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        for extra in [cfgdir / "session-env" / sid, cfgdir / "file-history" / sid]:
            if extra.is_dir():     # E4: per-uuid side files only, never listed
                shutil.rmtree(extra)
        # D-2: no glob over the shared todos/ dir (it lists real filenames)
    for leftover in [basedir / "memory", basedir]:
        try:
            leftover.rmdir()
        except OSError:
            pass
    shutil.rmtree(PROJ.parent, ignore_errors=True)


# --- claude, C2: A ends on a tool_use nobody answered ----------------------

def test_claude_resumes_a_fork_of_a_session_that_ends_on_an_unanswered_tool_use(live):
    """design「接在原生 raw 後面」: A was killed mid-tool.

    The adapter answers with one assistant line of text where the tool_result
    should be. Whether the API takes that is the question - a refusal here means
    `append_native` has to write a user `tool_result` (is_error) instead, which is
    exactly what review C2 said to measure before changing anything.
    """
    proj, created = live["proj"], live["created"]
    sid = str(uuid.uuid4())
    created.append(sid)          # A is ours from the moment its file exists
    maker = Maker(proj, sid)
    maker.said("user", "a1", None, FIRST, 0)
    maker.said("assistant", "a2", "a1",
               [{"type": "text", "text": "我查一下檔案。"},
                {"type": "tool_use", "id": "toolu_fork_a", "name": "Read",
                 "input": {"filePath": "/tmp/agora-it-fork/步驟.md"}}], 5)
    raw = maker.raw()
    assert C._asked_without_an_answer([json.loads(line) for line in json.loads(raw)["main"]]), \
        "這個 A 不是以沒有回覆的 tool_use 結尾，測的東西就不對了"

    forked, launch = fork_and_load(C.ADAPTER, raw, proj, created)
    assert C.ADAPTER.turns(forked) == C.ADAPTER.turns(raw) + [("assistant", [NO_REPLY])] + FORK

    proc = run_claude(live, "--resume", launch.agent_session_id, "-p", QUESTION_A)
    if (detail := refusal(proc)) is not None:
        say("claude 拒絕了以 tool_use 結尾的 fork", safe(detail))
        pytest.xfail("API 拒絕沒有 tool_result 的 tool_use：append_native 要改成補一則 user "
                     "tool_result（is_error），review C2／design F4")
    assert proc.returncode == 0, safe(f"{proc.stderr}\n{proc.stdout}")
    assert proc.stdout.strip(), "claude 沒有回覆"
    say("以 tool_use 結尾的 fork", f"rc=0，回覆：{proc.stdout.strip()[:120]}")


def test_claude_resumes_a_fork_when_only_the_second_of_two_parallel_calls_came_back(live):
    """Review C2, second half: claude writes one line per content block, so a turn
    that calls two tools is two lines and the line after them can hold the *second*
    call's result. The first call is still waiting.

    Whether the stand-in reply is added at all is printed rather than asserted: it
    is the detector's answer, and this file's job is to report what the real agent
    does with the result either way.
    """
    proj, created = live["proj"], live["created"]
    sid = str(uuid.uuid4())
    created.append(sid)
    maker = Maker(proj, sid)
    maker.said("user", "b1", None, "幫我看兩個檔案。", 0)
    maker.said("assistant", "b2", "b1",
               [{"type": "tool_use", "id": "toolu_fork_b1", "name": "Read",
                 "input": {"filePath": "/tmp/agora-it-fork/一.md"}}], 3)
    maker.said("assistant", "b3", "b2",
               [{"type": "tool_use", "id": "toolu_fork_b2", "name": "Read",
                 "input": {"filePath": "/tmp/agora-it-fork/二.md"}}], 4)
    maker.said("user", "b4", "b3",
               [{"type": "tool_result", "tool_use_id": "toolu_fork_b2",
                 "content": "二.md 有三行"}], 5)
    raw = maker.raw()

    forked, launch = fork_and_load(C.ADAPTER, raw, proj, created)
    stand_in = C.ADAPTER.turns(forked) == C.ADAPTER.turns(raw) + [("assistant", [NO_REPLY])] + FORK
    say("平行 tool_use、只有第二個有結果",
        f"補了 stand-in 回答：{stand_in}（review C2 說這個形狀偵測不到）")

    proc = run_claude(live, "--resume", launch.agent_session_id, "-p", QUESTION_A)
    if (detail := refusal(proc)) is not None:
        say("claude 拒絕了平行 tool_use 的 fork", safe(detail))
        pytest.xfail("API 拒絕沒有 tool_result 的 tool_use（平行呼叫的第一個）："
                     "偵測要改成收集最後一則 user 文字之後的所有 tool_use，review C2")
    assert proc.returncode == 0, safe(f"{proc.stderr}\n{proc.stdout}")
    assert proc.stdout.strip(), "claude 沒有回覆"
    say("平行 tool_use 的 fork", f"rc=0，回覆：{proc.stdout.strip()[:120]}")


# --- claude, C6: the anchor line is not a message --------------------------

@pytest.mark.parametrize("tail", ["system", "attachment"])
def test_claude_still_sees_a_when_the_last_uuid_line_is_not_a_message(live, tail):
    """C6: `_leaf_uuid` takes any line with a uuid that is not a sidechain, and a
    real session file's last such line is often `system` (turn_duration) or
    `attachment`. If claude keeps those out of the conversation chain, the fork
    hangs off a line the chain does not contain - the agent opens with none of A
    and reports nothing wrong. Only asking it can show that.
    """
    proj, created = live["proj"], live["created"]
    sid = str(uuid.uuid4())
    created.append(sid)
    maker = Maker(proj, sid)
    maker.said("user", "c1", None, FIRST, 0)
    maker.said("assistant", "c2", "c1",
               [{"type": "text", "text": f"記下了：{MARK}。"}], 5)
    if tail == "system":
        maker.plain("c3", "c2", 6, type="system", subtype="turn_duration",
                    durationMs=1234, content="api")
    else:
        maker.plain("c3", "c2", 6, type="attachment",
                    attachment={"type": "selected_lines", "snapshot": "x"},
                    rendered=["ZZRENDERED 一行"], renderedRole="user")
    raw = maker.raw()
    anchor = C._leaf_uuid(json.loads(raw)["main"])
    assert anchor == "c3", f"錨點不是最後那個 uuid 行（{anchor}），測的東西就不對了"

    _forked, launch = fork_and_load(C.ADAPTER, raw, proj, created)
    proc = run_claude(live, "--resume", launch.agent_session_id, "-p", QUESTION_B)
    assert proc.returncode == 0, safe(f"{proc.stderr}\n{proc.stdout}")
    answer = proc.stdout.strip()
    assert MARK in answer, (
        f"以 {tail} 行收尾的 A：fork 看不到 A 的第一句（回答：{answer[:200]}）。"
        f"錨點掛在 conversation chain 之外，agent 眼前是空的。")
    say(f"最後一個 uuid 行是 {tail}", f"看得到 A 的第一句：{answer[:120]}")


# --- opencode, C7: the part an undo pointer names --------------------------

OC_SID = "ses_agoraforkrevert01"
OC_PROVIDER, _, OC_MODEL = MODEL.partition("/")
#: In the tool call that the undo pointer names, so its fate can be read back by
#: name rather than by an id the import renames.
TOOL_MARK = "ZZTOOLFILE"
UNDONE_MARK = "ZZUNDO"       # a message after the pointer: gone once it is applied


def text_part(part_id: str, message_id: str, text: str) -> dict:
    return {"id": part_id, "sessionID": OC_SID, "messageID": message_id,
            "type": "text", "text": text}


def oc_message(message_id: str, role: str, *, at: int, parts: list[dict],
               parent: str | None = None) -> dict:
    """One message in the shape `opencode import` accepts (oc-basic.json).

    Every field import insists on is here, because it refuses the whole payload
    when one is missing - so a hand-made session has to be complete, not minimal.
    """
    info: dict = {"id": message_id, "sessionID": OC_SID, "role": role,
                  "time": {"created": at}, "agent": "build"}
    if role == "user":
        info["model"] = {"id": OC_MODEL, "providerID": OC_PROVIDER}
        info["summary"] = {}
    else:
        info.update({"parentID": parent, "mode": "build", "finish": "stop",
                     "providerID": OC_PROVIDER, "modelID": OC_MODEL, "cost": 0,
                     "path": {"cwd": "", "root": ""},
                     "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}}})
        info["time"]["completed"] = at + 5
    return {"info": info, "parts": parts}


def reverted_export() -> dict:
    """An export with the user's undo pointer still on it (oc-basic's shape).

    msg 2 is the one the pointer names and `partID` points at its last part, and
    two messages come after it - so applying the undo has both kinds of work to
    do, and the tail is what a fork would otherwise lose.
    """
    base = 1790214000000
    msg1, msg2 = f"msg_{OC_SID}a1", f"msg_{OC_SID}a2"
    msg3, msg4 = f"msg_{OC_SID}a3", f"msg_{OC_SID}a4"
    named = f"prt_{OC_SID}b2"        # the part the pointer names
    return {
        "info": {"id": OC_SID, "slug": "fork-revert", "projectID": "",
                 "directory": "", "path": "", "title": "把 CSV 轉成 Markdown 表格",
                 "agent": "build", "version": "", "cost": 0, "permission": [],
                 "model": {"id": OC_MODEL, "providerID": OC_PROVIDER, "variant": "default"},
                 "summary": {"additions": 0, "deletions": 0, "files": 0},
                 "tokens": {"input": 0, "output": 0, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}},
                 "time": {"created": base, "updated": base + 400000},
                 "revert": {"messageID": msg2, "partID": named,
                            "reason": "ZZREVERT 使用者按了 undo"}},
        "messages": [
            oc_message(msg1, "user", at=base,
                       parts=[text_part(f"prt_{OC_SID}a1", msg1,
                                        "把 CSV 轉成 Markdown 表格，先列三個步驟就好。")]),
            oc_message(msg2, "assistant", at=base + 1000, parent=msg1, parts=[
                {"id": f"prt_{OC_SID}b0", "sessionID": OC_SID, "messageID": msg2,
                 "type": "step-start", "snapshot": "0f1e2d3"},
                text_part(f"prt_{OC_SID}b1", msg2, "我先讀一下步驟檔。"),
                {"id": named, "sessionID": OC_SID, "messageID": msg2, "type": "tool",
                 "callID": f"call_{OC_SID}b2", "tool": "read",
                 "time": {"start": base + 1300, "end": base + 1400},
                 "state": {"status": "completed",
                           "input": {"filePath": f"/tmp/agora-it-fork/{TOOL_MARK} 步驟.md"},
                           "output": "ZZTOOLOUT 三個步驟"}}]),
            oc_message(msg3, "user", at=base + 2000,
                       parts=[text_part(f"prt_{OC_SID}c1", msg3,
                                        f"{UNDONE_MARK} 這一句會被 undo 掉。")]),
            oc_message(msg4, "assistant", at=base + 3000, parent=msg3,
                       parts=[text_part(f"prt_{OC_SID}d1", msg4, "好，這句不說了。")]),
        ],
    }


@pytest.fixture(scope="module")
def oc_home(tmp_path_factory):
    """A HOME that belongs to this module, with an empty opencode database.

    test_opencode_real.py OC4: the sandboxed HOME is what keeps these sessions out
    of the user's own opencode store, and teardown deletes exactly the ids this
    run recorded.
    """
    return tmp_path_factory.mktemp("fork-opencode-home")


@pytest.fixture
def oc_store(oc_home, monkeypatch):
    monkeypatch.setenv("HOME", str(oc_home))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return oc_home


@pytest.fixture
def oc_project(oc_store):
    if OPENCODE is None:
        pytest.skip("opencode not installed")
    if PROJ.exists():
        shutil.rmtree(PROJ)
    PROJ.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=PROJ, check=True)
    subprocess.run(["git", "-c", "user.email=agora@example.invalid",
                    "-c", "user.name=agora", "commit", "-q", "--allow-empty",
                    "-m", "init"], cwd=PROJ, check=True)
    return PROJ.resolve()


@pytest.fixture
def oc_trash(oc_project, oc_store):
    """Delete the ids this run made, one at a time, and nothing else."""
    recorded: list[str] = []
    yield recorded
    for session_id in sorted(set(recorded)):
        subprocess.run([OPENCODE, "session", "delete", session_id], cwd=str(PROJ),
                       env={**os.environ, "PWD": str(PROJ)}, capture_output=True)


def test_opencode_fork_applies_the_undo_before_adding_the_fork_turns(oc_project, oc_store,
                                                                     oc_trash):
    """design「接在原生 raw 後面」, OC6: while the pointer is there opencode hides
    everything after it and deletes it when the user first speaks - the fork's two
    turns would be exactly there.

    So the copy carries the undo applied and no pointer, the undone tail is gone,
    and the two fork turns are the last thing said. What happens to the part the
    pointer names (review C7) is printed, because the docstring and the code used
    to disagree about it and only a real opencode settles it.
    """
    proj = oc_project
    raw = json.dumps(reverted_export(), ensure_ascii=False).encode("utf-8")
    forked, launch = fork_and_load(oc.ADAPTER, raw, proj, oc_trash)

    exported = oc.ADAPTER.export(launch.agent_session_id)
    landed = exported.raw.decode("utf-8")
    assert "revert" not in json.loads(landed)["info"], "指標沒清掉"
    assert UNDONE_MARK not in landed, "被 undo 的訊息還在 fork 裡"
    assert NOTE in landed and READY in landed, "fork 的兩則沒有落在最後"
    assert launch.before_count == len(json.loads(forked)["messages"])

    # C7, our side: the part the pointer names goes with it (`del parts[cut:]`).
    # The other half - what opencode itself does - is the test below.
    say("revert 指向的那個 part（append_native）",
        f"{'刪掉' if TOOL_MARK not in forked.decode('utf-8') else '留著'}")

    # the reading version is what the user would see, and it has to read as a fork
    body = oc.ADAPTER.turns(exported.raw)
    say("fork 讀起來", f"最後兩則：{[(role, lines[0][:40]) for role, lines in body[-2:]]}")


def test_opencode_own_cleanup_and_the_part_the_pointer_names(oc_project, oc_store, oc_trash):
    """Review C7, the half `_apply_revert` is guessing at: opencode's own
    `SessionRevert.cleanup`, on a payload imported with the pointer still on it.

    The unit tests fixed our reading of opencode's behaviour from the binary's
    shape; nothing had run opencode. Importing the pointer and then speaking to
    the session makes opencode do its own cleanup, and the export afterwards says
    whether the part the pointer names is kept or deleted.

    Nothing is asserted about the answer - that is the measurement. What must hold
    is only that the session survived the round trip and still exports.
    """
    proj = oc_project
    raw = json.dumps(reverted_export(), ensure_ascii=False).encode("utf-8")
    launch = oc.ADAPTER.start_native(raw, proj)     # no append_native: the pointer stays
    oc_trash.append(launch.agent_session_id)

    proc = subprocess.run([OPENCODE, "run", "-s", launch.agent_session_id, "-m", MODEL,
                           "--format", "json", "只回答 OK，不要呼叫任何工具。"],
                          cwd=str(proj), env={**os.environ, "PWD": str(proj),
                                             "OPENCODE_PERMISSION": '{"*":"deny"}'},
                          capture_output=True, text=True, timeout=TIMEOUT)
    if proc.returncode != 0:
        say("opencode 自己的 cleanup", f"量不到：rc={proc.returncode} {safe(proc.stderr, 300)}")
        return
    landed = oc.ADAPTER.export(launch.agent_session_id).raw.decode("utf-8")
    say("opencode 自己的 cleanup（指標還在就匯入，再讓這段對話被說一句話）", "; ".join([
        f"指標：{'留著' if 'revert' in json.loads(landed)['info'] else '清掉了'}",
        f"指標之後的訊息：{'留著' if UNDONE_MARK in landed else '刪掉了'}",
        f"指標指到的 part（{TOOL_MARK}）：{'留著' if TOOL_MARK in landed else '刪掉了'}",
        f"匯出大小：{len(landed)} bytes",
    ]))
    assert json.loads(landed)["messages"], "匯出回來是空的"
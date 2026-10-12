"""The opencode adapter (design.md 5.2 and 5.4, v5).

    import session --agent opencode -> export()
    continue: the CLI hands over raws -> turns() / native() -> start_native()
    continue --fork: the CLI hands over A's own raw + two turns -> append_native()

Three directions (design v5, plus F4): `turns` splits this format into the shared
`[(role, lines)]`; `native` builds this format back from them, so a merged or
other-agent session still opens with its history on screen; `append_native` adds
turns to a session *in this format* - a same-agent fork, where A's session is loaded
as it is and a rebuild would lose every tool call. The reading version is
`base.reading(agent, raw)` — this module has nothing for it.

Loading a session means writing a new one: re-identify every id, `opencode
import` in the target directory, then `opencode --session <new id>`. Five things
about that are not negotiable, and all five were measured rather than assumed —
see docs/spike/opencode.md (traps 1-6) for the evidence:

1. import silently drops rows whose id already exists, so every id is new
   (id order and uniqueness: see `reidentify`).
2. import overwrites the session's directory with the working directory, and
   `run --session` hangs silently anywhere else, so one cwd does both.
3. import is not transactional, so both load paths read the session back and
   count messages *and* parts, deleting the wreckage before failing.
4. export's progress line is on stderr; `2>&1` would corrupt the JSON.
5. every call to opencode has a deadline, because the CLI collects on every
   command and one wedged process would wedge all of them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from agora.store import normalize

from .base import (AgentError, Exported, Launch, Listed, NO_REPLY, Turns, agent_cmd, env_seconds,
                   iso_utc, merge_turns, summarize_timeout, tool_line, warn)

# Id layout. Fixed width: a truncated ordinal is what made phase 1 import eight
# messages and get one, silently.
_TIME_HEX = 12      # time.created is milliseconds
_MESSAGE_HEX = 6    # the message's position in the export: unique by construction
_PART_HEX = 4       # the part's position inside its message
_SALT_HEX = 6       # different imports (retry, 1->n, n->1) get different ids

#: The only places an id is rewritten: structural positions, never a tool's state
#: or metadata, where `sessionId` can name a subagent's session or the user's own
#: data (OC2).
_MESSAGE_REFERENCES = ("parentID",)
_PART_REFERENCES = ("messageID",)

#: Part types that produce no line at all (D6: no tool results, no thinking).
_SILENT_PARTS = {"reasoning", "step-start", "step-finish", "step-start-finish",
                 "snapshot", "patch", "agent", "compaction", "subtask"}

#: What a session built by `native()` is called; the CLI gives it a real title in
#: the header, and opencode overwrites this one on the next continue anyway.
NATIVE_TITLE = "Agora 接續"

#: Ids `native()` writes as placeholders. start_native re-identifies every one of
#: them before importing, so they only have to be unique within the payload - but
#: they keep opencode's prefixes, because a payload whose ids do not start with
#: `ses`/`msg`/`prt` is refused outright ("Expected a string starting with msg"),
#: and a raw from `native()` should not be a trap for whoever debugs it.
PLACEHOLDER_ID = "ses_agora_pending0"

#: Every call gets a deadline: the CLI collects on every command, so one wedged
#: opencode would wedge all of them (OC7). Read per call so a test can shorten it.
DEFAULT_CLI_TIMEOUT = 60

#: The whole prompt goes into a file next to the session and only a short message
#: travels in argv: a merge prompt carries every source's reading version, and
#: argv tops out at ARG_MAX (1 MB here). `-f` inlines the file into the message,
#: so it reaches the model without any tool - verified with every tool denied.
MATERIAL_PREFIX = "material-"
MATERIAL_MESSAGE = "附件是完整的指示與材料。請依照附件的指示作答，只輸出它要求的結果。"

#: A summarize session is written down here before it is deleted, so a run that
#: dies mid-way leaves a record a later run can finish (Y6).
PENDING_PREFIX = "pending-"

#: A merge prompt must not be able to reach for a file, so every tool is denied -
#: a prompt asking nicely is not a boundary (measured in docs/spike/opencode.md).
DENY_TOOLS = '{"*":"deny"}'


#: opencode keeps everything in one SQLite file. `opencode session list` only
#: shows the current project's sessions and has no flag for the rest, so the
#: interactive mode reads that file itself - read-only, and only the four columns
#: the listing needs.
_DB_DIR = ("opencode", "opencode.db")
_LIST_COLUMNS = ("id", "directory", "title", "time_updated")
_LIST_SQL = "select id, directory, title, time_updated from session order by time_updated desc, id desc"
#: How many of the newest messages to look at for a preview. Reading the whole
#: transcript to show its last line is what T15 rules out.
_LAST_SQL = "select id, data from message where session_id=? order by time_created desc, id desc limit ?"
_LAST_SCAN = 20
_PARTS_SQL = "select data from part where message_id=? order by time_created, id"
_PREVIEW_CHARS = 2000
_SEARCH_LIKE_SQL = "select session_id, data from part where data like ? escape '\\'"
_SEARCH_ALL_SQL = "select session_id, data from part"

_warned_schema = False
_list_cache: tuple[tuple, list[Listed]] | None = None


def _data_home() -> Path:
    """Where opencode keeps its data: XDG_DATA_HOME, else ~/.local/share."""
    return Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()


def _db_path() -> Path:
    return _data_home().joinpath(*_DB_DIR)


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    """The database, opened so that nothing can be written through it."""
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)   # as_uri escapes ? # % (U2)
    except sqlite3.Error:
        return None


def _err(proc: subprocess.CompletedProcess, n: int = 300) -> str:
    """The tail of a failed command's stderr - the part that says what went wrong."""
    return (proc.stderr or b"").decode("utf-8", "replace").strip()[-n:]


def _warn_schema() -> None:
    """One line, once: an opencode we do not recognise is not an error to raise."""
    global _warned_schema
    if not _warned_schema:
        _warned_schema = True
        warn("這個 opencode 的資料庫結構認不出來，略過未匯入的清單")


def _field(blob: str, key: str):
    """One field out of a JSON blob, or None when it is not the JSON we expect."""
    try:
        value = json.loads(blob)
    except (TypeError, json.JSONDecodeError):
        return None
    return value.get(key) if isinstance(value, dict) else None


def _like_pattern(keyword: str) -> str:
    """`%keyword%`, with LIKE's own wildcards escaped so they stay literal."""
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@contextmanager
def _db(path: Path | None = None) -> Iterator[sqlite3.Connection | None]:
    """The database, read-only, for as long as the block needs it.

    Yields None when there is nothing to read - no file, or not the one we know.
    A sqlite error inside the block is reported once and ends the block: an opencode
    we do not recognise is not an error to raise (review O2). Generators (search)
    live inside it happily; they just stop there.
    """
    connection = _open_readonly(path or _db_path())
    if connection is None:
        yield None
        return
    try:
        yield connection
    except sqlite3.Error:
        _warn_schema()
    finally:
        connection.close()


def _session_directory(session_id: str) -> str | None:
    """The project directory the database records for one session, if it exists."""
    with _db() as connection:
        if connection is None:
            return None
        row = connection.execute("select directory from session where id=?",
                                (session_id,)).fetchone()
    directory = row[0] if row else None
    return directory if isinstance(directory, str) and Path(directory).is_dir() else None


def _event(line: bytes) -> dict | None:
    """One JSON object out of `opencode run --format json`, or None if that is not
    what this line is."""
    text = line.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None


def _event_session_id(line: bytes) -> str | None:
    """The session id one event carries, if any.

    The first events have it, minutes before the run ends - which is what lets the
    record go down while the model is still working (review V5).
    """
    session_id = (_event(line) or {}).get("sessionID")
    return session_id if isinstance(session_id, str) and session_id.strip() else None


def _remember_summary_session(workdir: Path, session_id: str) -> Path:
    """The record saying `workdir`'s summarize session still has to be deleted."""
    record = workdir / f"{PENDING_PREFIX}{session_id}"
    record.write_text("", encoding="utf-8")
    return record


def _last_reply(stdout: bytes) -> tuple[str | None, str | None]:
    """(session id, last assistant message's text) out of `opencode run --format json`.

    One JSON event per line. The session id comes from the first event that
    carries one; the reply is every `text` part of the last message that had
    one, in order, since a long reply can arrive as several parts.
    """
    session_id, parts, last = None, {}, None
    for line in stdout.splitlines():
        event = _event(line.strip())
        if event is None:
            continue
        session_id = session_id or event.get("sessionID")
        part = event.get("part") or {}
        if event.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
            last = part.get("messageID")
            parts.setdefault(last, []).append(part["text"])
    return session_id, ("\n\n".join(parts[last]) if parts else None)


def _session_info(created: int) -> dict:
    """The smallest `info` opencode will import *and* export again.

    Import rejects the whole payload when a field is missing, and export needs
    all of them, so a session that cannot be exported cannot be collected after
    the agent has worked in it. The id is a placeholder (start_native re-identifies
    the whole payload), and so are `directory` / `projectID`: import rewrites both
    from the working directory (spike V5).
    """
    stamp = {"created": created, "updated": created}
    return {
        "id": PLACEHOLDER_ID, "slug": "agora-handoff", "title": NATIVE_TITLE,
        "directory": "", "projectID": "", "path": "",
        "agent": "build", "version": "", "cost": 0, "permission": [],
        "model": {"id": "", "providerID": "opencode", "variant": "default"},
        "summary": {"additions": 0, "deletions": 0, "files": 0},
        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                   "cache": {"read": 0, "write": 0}},
        "time": stamp,
    }


def _session_id() -> str:
    """A fresh opencode-shaped session id: `ses_` + 16 characters."""
    from agora.header import new_ulid

    return "ses_" + new_ulid()[-16:]


def _id(prefix: str, *positions: tuple[int, int], time_ms: int, salt: str) -> str:
    """`prefix` + time + each position at its own width + salt.

    Positions, not the time, are what make the id unique: opencode orders parts by
    (message_id, id), so inside one message only the part position may vary, and
    across messages the message position decides (OC1). The assert keeps an
    overflow from quietly turning one character wider and breaking that order.
    """
    assert all(p < 1 << (4 * width) for p, width in positions), positions
    stamp = f"{max(int(time_ms), 0) & ((1 << (4 * _TIME_HEX)) - 1):0{_TIME_HEX}x}"
    return prefix + stamp + "".join(f"{p:0{w}x}" for p, w in positions) + salt


def _salt_for(session_id: str) -> str:
    """Derived from the new session id, not random: re-running the same import
    produces the same ids, so a retry is a no-op instead of a duplicate."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_SALT_HEX]


def _created_ms(info: dict, fallback: int = 0) -> int:
    created = (info.get("time") or {}).get("created")
    return created if isinstance(created, int) else fallback


def _remap(info: dict, keys: tuple[str, ...], mapping: dict[str, str]) -> None:
    """Rewrite `keys` in place through `mapping`; unknown targets stay as they are.

    A `parentID` pointing outside the imported range is left alone: import does not
    check it, and nulling it would make opencode reject the whole assistant
    message.
    """
    for key in keys:
        value = info.get(key)
        if isinstance(value, str):
            info[key] = mapping.get(value, value)


def reidentify(payload: dict, *, session_id: str, salt: str | None = None) -> dict:
    """Return `payload` re-identified as a brand-new session.

    Everything except the ids stays byte-for-byte identical, so what gets
    imported is the original transcript, not a paraphrase of it. The input is
    never modified.
    """
    messages = _payload_messages(payload)
    out = json.loads(json.dumps(payload))  # deep copy; never mutate the caller's
    salt = salt or _salt_for(session_id)

    # Pass 1: hand out the new ids. Doing this before touching references is what
    # lets a parentID that points forwards resolve.
    message_map: dict[str, str] = {}
    part_map: dict[str, str] = {}
    stamp = 0
    for position, message in enumerate(out["messages"]):
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise AgentError(f"第 {position} 則訊息形狀不對（缺 info 物件）")
        info = message["info"]
        stamp = _created_ms(info, stamp)   # a message with no time keeps the order
        new_message_id = _id("msg_", time_ms=stamp, salt=salt,
                             *((position, _MESSAGE_HEX),))
        message_map[str(info.get("id"))] = new_message_id
        info["id"] = new_message_id
        for part_position, part in enumerate(message.get("parts") or []):
            if isinstance(part, dict):
                part_map[str(part.get("id"))] = _id(
                    "prt_", time_ms=stamp, salt=salt,
                    *((position, _MESSAGE_HEX), (part_position, _PART_HEX)))
                part["id"] = part_map[str(part.get("id"))]

    # Pass 2: the structural references, and nothing else (OC2, N14).
    out["info"]["id"] = session_id
    for message in out["messages"]:
        info = message["info"]
        info["sessionID"] = session_id
        _remap(info, _MESSAGE_REFERENCES, message_map)
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            part["sessionID"] = session_id
            _remap(part, _PART_REFERENCES, message_map)
    revert = out["info"].get("revert")   # a reverted session's undo pointer (OC6)
    if isinstance(revert, dict):
        _remap(revert, _MESSAGE_REFERENCES + _PART_REFERENCES + ("partID",),
               {**message_map, **part_map})
    return out


def _iso(ms: int | None) -> str | None:
    return iso_utc(ms / 1000) if isinstance(ms, int) else None


def _apply_revert(payload: dict) -> None:
    """Apply `info.revert` and clear it: what opencode does before its next prompt.

    Read off the 1.18.34 binary (`SessionRevert.cleanup`), which does this in that
    order: the messages from `messageID` on are dropped - from the one *after* it
    when there is a `partID` - then, when there is a `partID`, that message's own
    parts are cut at it, so the part it names goes too and only what came before it
    stays; then the pointer is cleared. A `messageID` that is not in the export
    removes nothing; only the pointer goes. **tasks 1.7** is to confirm this against
    a real session, and to change this function if the real thing differs - the
    wording here and the code below are kept in step on purpose.

    Why the fork's copy has to have it applied before anything is added (F4): while
    the pointer is there opencode hides everything after it, and the first thing the
    user says deletes those messages for good. The two turns that say what this
    session is a fork of would be sitting exactly there - invisible when the agent
    opens, gone after the first reply, leaving a fork whose last exchange was cut
    off. A itself is not touched: this is the copy that gets imported, and in A the
    user can still undo, or un-undo, as they please.
    """
    info = payload.get("info")
    revert = info.get("revert") if isinstance(info, dict) else None
    if not isinstance(revert, dict):
        return
    messages = _payload_messages(payload)
    found = next((n for n, message in enumerate(messages)
                  if str((message.get("info") or {}).get("id")) == str(revert.get("messageID"))), -1)
    if found >= 0:
        part_id = revert.get("partID")
        del messages[found + (1 if part_id else 0):]
        if part_id and isinstance(messages[found].get("parts"), list):
            parts = messages[found]["parts"]
            cut = next((n for n, part in enumerate(parts)
                        if isinstance(part, dict) and part.get("id") == part_id), -1)
            if cut >= 0:
                del parts[cut:]
    info.pop("revert", None)   # the copy has nothing left to undo


def _role_fields(payload: dict) -> dict[str, dict]:
    """Per role, the fields a new message of that role has to copy: `agent`, the
    model, and (for an assistant) the mode it ran in. A user message nests the model
    (`model`), an assistant message carries it beside the message (`providerID` /
    `modelID`) - that split is opencode's, so each role reads its own shape off its
    own last message (F4).

    Without this the fork's assistant answers with an empty model: `_last_model()`
    stops at the last assistant message, so the Session would record no model at all,
    and opencode could come back on a model this session never used. `mode` comes
    along for the same reason: an assistant that says `agent: plan` and `mode: build`
    is a shape opencode has no reason to have been given.
    """
    fields: dict[str, dict] = {"user": {}, "assistant": {}}
    for message in reversed(_payload_messages(payload)):
        info = message.get("info")
        if not isinstance(info, dict):
            continue
        role = info.get("role")
        if role not in fields or fields[role]:
            continue
        picked = fields[role]
        for key in ("agent", "mode"):
            if isinstance(info.get(key), str) and info[key]:
                picked[key] = info[key]
        if role == "user":
            model = info.get("model")
            if isinstance(model, dict):
                picked["model"] = json.loads(json.dumps(model))   # a copy, not the original
        else:
            for key in ("providerID", "modelID"):
                if isinstance(info.get(key), str) and info[key]:
                    picked[key] = info[key]
    return fields


def _unused_stem(messages: list[dict]) -> str:
    """An id stem nothing in this payload uses.

    `reidentify` maps every id to a new one, so two rows sharing an id would share
    the new one too and one of them would come out pointing at the other.
    """
    taken = {str((message.get("info") or {}).get("id")) for message in messages}
    taken |= {str(part.get("id")) for message in messages
              for part in (message.get("parts") or []) if isinstance(part, dict)}
    stem = "agora"
    while any(value.startswith(("msg_", "prt_")) and stem in value for value in taken):
        stem += "a"
    return stem


def _said(blob: str) -> str | None:
    """The text one part blob holds, if it is text the session said.

    `synthetic` parts are opencode's own inlined attachments: something the session
    was given, not something it said (review O3).
    """
    if _field(blob, "type") != "text" or _field(blob, "synthetic") is True:
        return None
    text = _field(blob, "text")
    return text.strip() if isinstance(text, str) and text.strip() else None


def _payload_messages(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise AgentError("opencode 的匯出檔形狀不對（缺 messages 清單）")
    return payload["messages"]


def _payload_format(payload: object) -> str:
    """Which opencode wrote this export: "v2" (flat rows) or "v1" (nested).

    v2 flattened the rows - `{…, type: "user"|"assistant"|"idle"}`, the assistant's
    content in `content` - and stopped writing `directory`/`version`, so the shape
    of the payload is the fact, not the version of the CLI that is installed now
    (the store still holds raws from earlier versions).

    Every row has to agree (review L1): one v1 row appended to a v2 export by an
    older `append_native` is a broken payload, and calling it v2 would import it
    as one. An empty message list falls back to `info`, and a payload that is
    neither is an error with a reason, never an empty reading version.
    """
    messages = _payload_messages(payload)
    seen: set[str] = set()
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            raise AgentError(f"opencode 的匯出檔第 {position} 則訊息不是物件")
        has_type = isinstance(message.get("type"), str)
        has_info = isinstance(message.get("info"), dict)
        if has_type and not has_info:
            seen.add("v2")
        elif has_info and not has_type:
            seen.add("v1")
        else:
            raise AgentError(f"opencode 的匯出檔第 {position} 則訊息認不出形狀"
                             "（要嘛有 type，要嘛有 info）")
    if len(seen) == 1:
        return seen.pop()
    if seen:
        raise AgentError("opencode 的匯出檔每一列的形狀不一致（v1 與 v2 混在一起）")
    info = payload.get("info") if isinstance(payload, dict) else None
    info = info if isinstance(info, dict) else {}
    if isinstance(info.get("location"), dict):
        return "v2"
    if isinstance(info.get("directory"), str) or isinstance(info.get("slug"), str):
        return "v1"
    raise AgentError("opencode 的匯出檔認不出格式（沒有 location，也沒有 directory／slug）")


def _last_model(payload: dict) -> str | None:
    """The model the session used most recently.

    opencode records the bare model id on assistant messages (`modelID`, with
    `providerID` beside it); user messages carry a nested `model` object instead,
    so the last assistant turn is the one that answers "which model was this".
    A session with no assistant turn yet has none.
    """
    for message in reversed(_payload_messages(payload)):
        info = message.get("info") or {}
        if info.get("role") == "assistant":
            model = info.get("modelID")
            return model if isinstance(model, str) and model else None
    return None


def _lines_of(parts: list[dict]) -> list[str]:
    """One reading-version turn's worth of lines, D6 rules applied."""
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if part.get("synthetic") is True:
            continue                     # opencode's own inlined attachment (D6)
        if kind == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
        elif kind == "tool":
            state = part.get("state") or {}
            lines.append(tool_line(str(part.get("tool", "?")), state.get("input", {})))
        elif kind in _SILENT_PARTS:
            continue
        else:
            lines.append(f"[skip {kind}]")  # visible, and never fatal
    return lines


def _lines_of_v2(content: list) -> list[str]:
    """The same rules for one v2 assistant row's `content` (design 5).

    The row is flat, so the content items are named `text` / `tool` and the tool
    call's name and arguments sit in `name` and `state.input` - everything else
    (attachments, steps, reasoning, a shape a later opencode adds) follows the v1
    rules: silent when it is not conversation, `[skip <type>]` when it is unknown.
    """
    lines: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if item.get("synthetic") is True:
            continue
        if kind == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
        elif kind == "tool":
            state = item.get("state") or {}
            lines.append(tool_line(str(item.get("name", "?")), state.get("input", {})))
        elif kind in _SILENT_PARTS:
            continue
        else:
            lines.append(f"[skip {kind}]")
    return lines


def _user_text(text: object) -> str | None:
    """The words of a v2 user row, if it said anything in writing.

    v2's *first* user row stores the prompt JSON-encoded (`"hello"`), later ones
    plain (measured 2.0.26) - the CLI wraps its positional argument and nothing
    else does. Decoding when it is a JSON string keeps the reading version showing
    the user's words instead of their escaping.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return text.strip()
    if isinstance(decoded, str) and decoded.strip():
        return decoded.strip()
    return text.strip()


class OpencodeAgent:
    """Speaks to the `opencode` executable; owns nothing else."""

    name = "opencode"

    # -- running opencode ---------------------------------------------------

    def _run(self, argv: list[str], cwd: Path | str | None = None,
             stdout: int | None = None,
             env: dict | None = None,
             stdin: int | None = None) -> subprocess.CompletedProcess:
        """Every call gets a deadline (OC7) and reports it as an AgentError, so a
        wedged opencode becomes a retryable failure instead of a hung command.

        `stdin` is DEVNULL wherever a child of ours could otherwise reach for the
        terminal - the periodic write-back runs while the agent owns it (design
        「不弄亂 agent 的畫面」). Left alone it inherits our stdin, and a read there
        eats the user's next line.
        """
        seconds = env_seconds("AGORA_OPENCODE_TIMEOUT", DEFAULT_CLI_TIMEOUT)
        child_env = {**os.environ, **env} if env else None
        try:
            return subprocess.run(argv, cwd=str(cwd) if cwd else None, timeout=seconds,
                                  env=child_env,
                                  **({"stdin": stdin} if stdin is not None else {}),
                                  **({"stdout": stdout, "stderr": subprocess.PIPE}
                                     if stdout is not None else {"capture_output": True}))
        except subprocess.TimeoutExpired:
            raise AgentError(f"opencode 逾時（{seconds:g} 秒）："
                             f"{argv[1] if len(argv) > 1 else argv[0]}") from None

    def _export_bytes(self, session_id: str, cwd: Path | str | None = None) -> bytes:
        """`opencode export <id>` with stdout going to a file, stderr apart.

        `cwd` is not needed for this to work - opencode finds any session from any
        directory (measured, including when the session's own project directory no
        longer exists) - but when it is given, $PWD goes with it, because opencode
        reads the project from $PWD.

        stdin is /dev/null: an export is ours, not the user's, and during a periodic
        write-back the terminal belongs to the agent - a child that read there would
        take the line the user is typing (design「不弄亂 agent 的畫面」).
        """
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            target = Path(tmp) / "export.json"
            with open(target, "wb") as out:
                proc = self._run([agent_cmd(self.name), "export", session_id],
                                 cwd=cwd, stdout=out, stdin=subprocess.DEVNULL,
                                 env={"PWD": str(cwd)} if cwd else None)
            if proc.returncode != 0:
                message = _err(proc)
                if "Session not found" in message:
                    raise AgentError(f"opencode 找不到 Session {session_id}")
                raise AgentError(f"opencode export {session_id} 失敗：{message}")
            return target.read_bytes()

    def _import(self, payload: dict, cwd: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            path = Path(tmp) / "import.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            proc = self._run([agent_cmd(self.name), "import", str(path)], cwd=cwd)
        if proc.returncode != 0:
            raise AgentError(f"opencode import 失敗：{_err(proc)}")

    def _delete(self, session_id: str, cwd: Path, n: int = 200) -> str | None:
        """`opencode session delete <id>` - by id, never a pattern and never a listing.

        None when it worked, otherwise what to tell the user (review O5).
        """
        proc = self._run([agent_cmd(self.name), "session", "delete", session_id], cwd=cwd)
        return None if proc.returncode == 0 else _err(proc, n)

    def _discard(self, session_id: str, cwd: Path) -> str:
        """Delete the wreckage, one id at a time, and say what to tell the user."""
        detail = self._delete(session_id, cwd)
        if detail is None:
            return f"已經刪掉 {session_id}"
        return (f"而且刪不掉 {session_id}，請手動 "
                f"`opencode session delete {session_id}`：{detail}")

    def _import_verified(self, payload: dict, cwd: Path) -> tuple[str, int]:
        """Import, read it back, and prove nothing was dropped (rules 2 and 4).

        Returns (session id, messages landed). Messages *and* parts are compared:
        a colliding part id leaves the message count intact, so counting only
        messages would wave through a session that quietly lost content.
        """
        session_id = str((payload.get("info") or {}).get("id") or "")
        if not session_id:
            raise AgentError("匯出檔的 info.id 不見了，無法決定新 session 的 id")
        messages = _payload_messages(payload)
        parts = sum(len(m.get("parts") or []) for m in messages)
        self._import(payload, cwd)
        landed = _payload_messages(self._read(self._export_bytes(session_id, cwd=cwd)))
        landed_parts = sum(len(m.get("parts") or []) for m in landed)
        if len(landed) != len(messages) or landed_parts != parts:
            tail = self._discard(session_id, cwd)
            raise AgentError(
                f"opencode import 後數量不對（來源 {len(messages)} 則／{parts} 個 part，"
                f"匯入後 {len(landed)} 則／{landed_parts} 個），{tail}；"
                "多半是 id 撞到 opencode 裡既有的 Session")
        return session_id, len(landed)

    def _read(self, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise AgentError(f"opencode 的匯出檔不是 JSON：{e}")
        if not isinstance(payload, dict):
            raise AgentError("opencode 的匯出檔最外層不是 JSON 物件")
        return payload

    # -- the Agent protocol ------------------------------------------------

    def export(self, session_id: str) -> Exported:
        try:
            raw = self._export_bytes(session_id)
        except AgentError:
            # opencode 1.18 exports from anywhere, so this is the belt to that
            # braces: if a version ever scopes export to the current project, the
            # directory is one read-only lookup away. Retrying there beats losing
            # the row the interactive mode is showing.
            elsewhere = _session_directory(session_id)
            if not elsewhere:
                raise
            raw = self._export_bytes(session_id, cwd=Path(elsewhere))
        payload = self._read(raw)
        info = payload.get("info") or {}
        directory = info.get("directory")
        version = info.get("version")   # the version that produced *this* session
        return Exported(
            session_id=str(info.get("id") or session_id),
            raw=raw,
            dir=directory if isinstance(directory, str) and directory else None,
            title=info.get("title") if isinstance(info.get("title"), str) else None,
            created_at=_iso(_created_ms(info)),
            agent_version=str(version) if version else None,
            message_count=len(_payload_messages(payload)),
            model=_last_model(payload),
        )

    def turns(self, raw: bytes) -> Turns:
        """One entry per user/assistant message: its text, plus a line per tool
        call - the reading version's rules (design 4.4), so `native(turns(raw))`
        can rebuild a session whose history reads the same way.

        Both export formats read the same way; `idle` rows and attachment files are
        not conversation and produce no turn (H1: a v2 import must not come out as
        an empty body).
        """
        payload = self._read(raw)
        out: Turns = []
        if _payload_format(payload) == "v2":
            for row in _payload_messages(payload):
                kind = row.get("type")
                if kind == "user":
                    text = _user_text(row.get("text"))
                    out.append(("user", [text] if text else []))
                elif kind == "assistant":
                    out.append(("assistant", _lines_of_v2(row.get("content") or [])))
            return out
        for message in _payload_messages(payload):
            role = (message.get("info") or {}).get("role")
            if role in ("user", "assistant"):
                out.append((role, _lines_of(message.get("parts") or [])))
        return out

    def native(self, turns: Turns) -> bytes:
        """Turns as an opencode export, ready for `start_native` to load.

        The caller (cli._converted_turns) hands over what design v5 promises:
        starts with a user turn, strictly alternating, no empty turn, no
        `[skip …]` line. So this only has to compose them in order - no merging,
        no padding.

        Plain, visible text parts - no synthetic, no attachment: whatever a
        session says has to be on screen when the agent opens. One part per line,
        the way opencode stores a multi-paragraph reply, so `turns(native(
        turns(raw)))` is the same list of lines it started from.
        """
        created = int(time.time() * 1000)
        messages: list[dict] = []
        previous = ""
        for role, lines in turns:
            # the contract says both are true already; the check keeps a caller
            # that breaks it from producing a message opencode would reject
            body = [line for line in lines if line.strip()]
            if role not in ("user", "assistant") or not body:
                continue
            at = created + len(messages)   # times move forward: opencode sorts by them
            message_id = f"msg_agora{len(messages)}"
            info = {"id": message_id, "sessionID": PLACEHOLDER_ID, "role": role,
                    "time": {"created": at}, "agent": "build",
                    "model": {"providerID": "opencode", "modelID": ""}}
            if role == "assistant":
                # import rejects an assistant message whose parentID is null, so
                # it has to point at the message it answers; the rest of these
                # fields are what its schema insists on (measured: each missing
                # one is "Missing key at [...]" and the whole payload is refused)
                info.update({"parentID": previous, "mode": "build", "finish": "stop",
                             "providerID": "opencode", "modelID": "", "cost": 0,
                             "path": {"cwd": "", "root": ""},
                             "tokens": {"total": 0, "input": 0, "output": 0,
                                        "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}}})
                info["time"]["completed"] = at
            else:
                info["summary"] = {"diffs": []}
            messages.append({"info": info, "parts": [
                {"id": f"prt_agora{len(messages)}-{n}", "sessionID": PLACEHOLDER_ID,
                 "messageID": message_id, "type": "text", "text": line}
                for n, line in enumerate(body)]})
            previous = message_id
        payload = {"info": _session_info(created), "messages": messages}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        """A's own export with `turns` added as new messages (F4).

        A same-agent fork loads A's session as it is, so the fork's two turns cannot
        go through `native()`: that rebuilds the conversation out of its reading
        version and loses every tool call. So this is A's payload, copied, with the
        undo pointer applied first (so the fork does not inherit a tail opencode
        would hide and then delete) and the turns appended as ordinary messages -
        chained to A's last one, carrying the model and mode A was using, and visible
        plain text like everything else `native()` writes.

        The one place this is not `raw` plus `turns`: applying the undo *removes*
        messages, so for a session the user undid something in, the result reads as
        that session as the user sees it now - the undone exchange is not in the
        fork, and neither is it in A (design「接在原生 raw 後面」).
        """
        payload = self._read(raw)
        _apply_revert(payload)
        messages = _payload_messages(payload)
        body = [(role, [line for line in lines if line.strip()]) for role, lines in turns
                if role in ("user", "assistant") and any(line.strip() for line in lines)]
        said = [m for m in messages
                if (m.get("info") or {}).get("role") in ("user", "assistant")]
        last_question = next((m for m in reversed(said)
                              if (m.get("info") or {}).get("role") == "user"), None)
        # W1, on the same rule the claude adapter and cli._converted_turns use: what
        # the reading version is about to show, once its `[skip …]` lines are gone.
        # Not the last message - an interrupted run or a provider error leaves an
        # assistant with nothing in it (only step-start, or a retry part this version
        # does not know), which the reading version drops, and the turn before it is
        # still the user's open question.
        spoken = merge_turns([(role, [line for line in lines if not line.startswith("[skip ")])
                              for role, lines in self.turns(
                                  json.dumps(payload, ensure_ascii=False).encode("utf-8"))])
        if body and spoken and spoken[-1][0] == "user":
            body.insert(0, ("assistant", [NO_REPLY]))
        fields = _role_fields(payload)
        session_id = str((payload.get("info") or {}).get("id") or PLACEHOLDER_ID)
        stem = _unused_stem(messages)
        # the stand-in answers the question, so it hangs off that message: the one
        # before it may be the empty assistant, which nothing should continue
        previous = str((last_question or {}).get("info", {}).get("id") or "") if last_question else ""
        first = max(int(time.time() * 1000),
                    1 + max((_created_ms(m.get("info") or {}) for m in messages), default=0))
        for position, (role, lines) in enumerate(body):
            at = first + position
            message_id = f"msg_{stem}{position}"
            copied = fields.get(role) or {}
            info = {"id": message_id, "sessionID": session_id, "role": role,
                    "time": {"created": at}, "agent": copied.get("agent", "build"),
                    "model": copied.get("model") or {"providerID": "opencode", "modelID": ""}}
            if role == "assistant":
                # the same fields native() writes, which import insists on (each one
                # missing is "Missing key at [...]" and the payload is refused)
                info.update({"parentID": previous,
                             "mode": copied.get("mode") or copied.get("agent") or "build",
                             "finish": "stop",
                             "providerID": copied.get("providerID", "opencode"),
                             "modelID": copied.get("modelID", ""), "cost": 0,
                             "path": {"cwd": "", "root": ""},
                             "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0,
                                        "cache": {"read": 0, "write": 0}}})
                info["time"]["completed"] = at
            else:
                info["summary"] = {"diffs": []}
            messages.append({"info": info, "parts": [
                {"id": f"prt_{stem}{position}-{n}", "sessionID": session_id,
                 "messageID": message_id, "type": "text", "text": line}
                for n, line in enumerate(lines)]})
            if role == "user":
                previous = message_id
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def list_sessions(self) -> list[Listed]:
        """Every session on this machine, across all projects (design 5.9).

        Read-only, and never `opencode session list`: that only reports the
        current project. Only the listing columns are read - no transcripts - and
        the answer is memoised per (path, mtime, size), because the interactive
        mode asks again on every refresh. Unreadable or unrecognised: empty list.
        """
        global _list_cache
        path = _db_path()
        try:
            info = path.stat()
        except OSError:
            return []
        try:   # with WAL, new rows land in -wal first and the main file may not change
            wal = path.with_name(path.name + "-wal").stat()
            wal_key = (wal.st_mtime_ns, wal.st_size)
        except OSError:
            wal_key = None
        key = (str(path), info.st_mtime_ns, info.st_size, wal_key)
        if _list_cache and _list_cache[0] == key:
            return list(_list_cache[1])
        rows = self._read_sessions(path)
        _list_cache = (key, rows)
        return list(rows)

    def _read_sessions(self, path: Path) -> list[Listed]:
        with _db(path) as connection:
            if connection is None:
                return []
            columns = {row[1] for row in connection.execute("pragma table_info(session)")}
            if not set(_LIST_COLUMNS) <= columns:
                _warn_schema()
                return []
            return [Listed(session_id=session_id, dir=directory or None,
                           title=title or None, updated_at=_iso(time_updated))
                    for session_id, directory, title, time_updated
                    in connection.execute(_LIST_SQL)]
        return []

    def search_text(self, keyword: str, only: set[str] | None = None) -> Iterator[str]:
        """Session ids whose conversation mentions `keyword`, one at a time.

        Case-insensitive and compared after NFKC, across every project, read-only,
        and streamed: the caller runs this in a thread and puts each id on screen as
        it arrives, so the first match must not wait for the whole store. Hence the
        two passes - `part.data LIKE %keyword%` is cheap and hands over the common
        case in milliseconds, then a full scan checks what LIKE cannot see (LIKE
        folds ASCII case only, and normalises nothing, so full-width "Ｈｅｌｌｏ"
        must still match "hello"). Python decides every match; SQL only chooses
        what to look at. Nothing is exported - that would read whole transcripts to
        answer a question about text.
        """
        keyword = keyword.strip()
        wanted = normalize(keyword)
        if not wanted:
            return
        seen: set[str] = set()
        with _db() as connection:
            if connection is None:
                return
            passes = ((_SEARCH_LIKE_SQL, (_like_pattern(keyword),)), (_SEARCH_ALL_SQL, ()))
            for statement, arguments in passes:
                for session_id, blob in connection.execute(statement, arguments):
                    if session_id in seen or (only is not None and session_id not in only):
                        continue
                    text = _said(blob)
                    if text is not None and wanted in normalize(text):
                        seen.add(session_id)
                        yield session_id

    def last_message(self, session_id: str) -> tuple[str, str] | None:
        """(role, text) of the newest plain-text turn, as stored; None if there is none.

        Reads the last few messages only, and caps the text at 2,000 characters -
        the preview pane shows one turn, not a transcript. `synthetic` parts are
        skipped: they are attachments, not something the session said.
        """
        with _db() as connection:
            if connection is None:
                return None
            for message_id, data in connection.execute(_LAST_SQL, (session_id, _LAST_SCAN)):
                role = _field(data, "role")
                if role not in ("user", "assistant"):
                    continue
                texts = []   # a long reply is several text parts: all of them, in order
                for (blob,) in connection.execute(_PARTS_SQL, (message_id,)):
                    if text := _said(blob):
                        texts.append(text)
                if texts:
                    return role, "\n\n".join(texts)[:_PREVIEW_CHARS]
        return None

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        """One headless turn with every tool denied; returns (text, model).

        The prompt and its materials go into a file inside `workdir` and only a
        short message travels in argv: a merge prompt carries every source's
        reading version, and argv stops at ARG_MAX (1 MB here). `-f` inlines the
        file into the message, so the material reaches the model with no tool
        available - measured: with `OPENCODE_PERMISSION={"*":"deny"}` the model
        still answered with a word that only existed in the attachment. In this
        version the message has to come *before* `-f`; after it, opencode reads
        the message as one more filename and fails.

        The session this run creates is recorded in `workdir/pending-<id>` as soon
        as its id appears in the event stream - not when the run ends, because the
        run is exactly when an interruption happens and a record written after it
        describes nothing (review V5) - and the record only goes away once the
        delete succeeded, so an interrupted run leaves something the next one can
        finish (Y6).

        AGORA_OPENCODE_MODEL picks the model; without it opencode uses whatever
        the user's own default is.
        """
        workdir.mkdir(parents=True, exist_ok=True)
        self._sweep_pending(workdir)
        unique = _session_id()[-16:]
        material = workdir / f"{MATERIAL_PREFIX}{unique}.md"
        argv = [agent_cmd(self.name), "run", MATERIAL_MESSAGE, "-f", str(material),
                "--format", "json"]
        model = os.environ.get("AGORA_OPENCODE_MODEL")
        if model:
            argv += ["-m", model]
        material.write_text(prompt, encoding="utf-8")
        seconds = summarize_timeout()
        try:
            code, events, errors = self._summarizing(argv, workdir, seconds)
        finally:
            material.unlink(missing_ok=True)
        session_id, text = _last_reply(events)
        # the run's JSON events carry no model name, so the session we are about
        # to delete is where we read it from
        answered = self._model_of(session_id, workdir) if session_id else None
        self._drop_summary_session(session_id, workdir)
        if code != 0:
            raise AgentError(f"opencode 寫要約失敗："
                             f"{errors.decode('utf-8', 'replace').strip()[-200:]}")
        if not (text or "").strip():
            raise AgentError("opencode 沒有寫出要約（空回覆）")
        return text, answered

    def _summarizing(self, argv: list[str], workdir: Path, seconds: int) -> tuple[int, bytes, bytes]:
        """`opencode run … --format json`, read while it runs.

        Each pipe gets its own thread, so neither can fill up and wedge the run,
        and the session id is recorded the moment an event carries it (review V5):
        Esc in the interactive mode interrupts the run, and a record written after
        the run would leave the session it created with nothing pointing at it.
        The deadline is the one every other call has (OC7).
        """
        try:
            proc = subprocess.Popen(
                argv, cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={**os.environ, "OPENCODE_PERMISSION": DENY_TOOLS, "PWD": str(workdir)})
        except OSError as e:
            raise AgentError(f"叫不起 opencode（{argv[0]}）：{e}") from None
        events: list[bytes] = []
        errors: list[bytes] = []
        remembered: list[str] = []

        def read_events() -> None:
            for line in iter(proc.stdout.readline, b""):
                events.append(line)
                session_id = _event_session_id(line)
                if session_id and session_id not in remembered:
                    remembered.append(session_id)
                    _remember_summary_session(workdir, session_id)

        readers = (threading.Thread(target=read_events, daemon=True),
                   threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True))
        for reader in readers:
            reader.start()
        try:
            proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise AgentError(f"opencode 寫要約逾時（{seconds:g} 秒）") from None
        except BaseException:      # Esc, a closed terminal: kill opencode now (review M5)
            # The pending record is already written, so killing it is safe - and the
            # alternative is waiting on a stdout that never reaches EOF, until
            # something stronger than SIGINT arrives.
            with contextlib.suppress(Exception):
                proc.kill()
                proc.wait()
            raise
        finally:
            for reader in readers:
                reader.join(timeout=5)     # the pipes close as the child goes, so this returns
            proc.stdout.close()
            proc.stderr.close()
        return proc.returncode, b"".join(events), b"".join(errors)

    def _sweep_pending(self, workdir: Path) -> None:
        """Finish what an interrupted run left: one id at a time, then drop the record.

        The material files go too: one holds the whole conversation being summarised,
        and a run killed by SIGTERM or SIGKILL never reaches its own cleanup. The
        directory is agora's own (design 5.3), so what is in it is ours to remove.
        """
        for record in sorted(workdir.glob(f"{PENDING_PREFIX}*")):
            self._drop_summary_session(record.name[len(PENDING_PREFIX):], workdir)
        for material in workdir.glob(f"{MATERIAL_PREFIX}*"):
            material.unlink(missing_ok=True)

    def _drop_summary_session(self, session_id: str | None, workdir: Path) -> None:
        """Delete the one session a summarize run made - by id, never a pattern and
        never by listing. The record in `workdir` is removed only once that
        worked, so a failure leaves the next run something to finish."""
        if not session_id:
            return
        record = _remember_summary_session(workdir, session_id)   # already there if the run streamed it
        detail = self._delete(session_id, workdir, n=160)
        if detail is not None:
            warn(f"寫要約用掉的 {session_id} 沒刪掉（記錄留在 {record}）：{detail}")
            return
        record.unlink(missing_ok=True)

    def _model_of(self, session_id: str, workdir: Path) -> str | None:
        """Which model answered, read from the session this run just made."""
        try:
            payload = self._read(self._export_bytes(session_id, cwd=workdir))
        except AgentError:
            return None
        if _last_model(payload):
            return _last_model(payload)
        name = ((payload.get("info") or {}).get("model") or {}).get("id")
        return name if isinstance(name, str) and name else None

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None) -> Launch:
        session_id, landed = self._import_verified(
            reidentify(self._read(raw), session_id=_session_id()), workdir)
        if headless:
            # The server mode from the spike (design D6): the caller allocated
            # the port (sidecar passes --headless-port) and talks HTTP to it;
            # the session the conversation was imported into is `session_id`.
            if not headless_port:
                raise AgentError("opencode 的 --headless 需要 --headless-port（由呼叫方給一個空的埠）")
            argv = [agent_cmd(self.name), "serve",
                    "--hostname", "127.0.0.1", "--port", str(headless_port)]
        else:
            argv = [agent_cmd(self.name), "--session", session_id]
        return Launch(argv=argv, cwd=str(workdir), agent_session_id=session_id,
                      before_count=landed)

    def collect(self, launch: Launch) -> Exported | None:
        """What the agent produced, or None if it said nothing new (S9).

        Read back like an import, so `model` is the one the agent just used and
        not the one the session started with.
        """
        if not launch.agent_session_id:
            # OC10: claude raises here too. A pending record always carries the
            # id, so its absence means the record is broken; returning None would
            # make the CLI delete the pending and lose the session for good.
            raise AgentError("沒有 opencode session id，無法收尾")
        exported = self.export(launch.agent_session_id)
        if exported.message_count <= launch.before_count:
            return None
        return exported


ADAPTER = OpencodeAgent()
"""The opencode adapter (design.md 5.2 and 5.4).

    agora import --format opencode --session-id ses_…        -> export()
    agora continue-session <id> --agent opencode             -> start_native()
    agora continue-session <id> --agent opencode  (跨 agent)  -> start_injected()

Loading someone else's session means writing a new one, because opencode has no
"attach to this transcript" flag. The recipe is fixed (spike V1):

    export to a file -> re-identify every id -> opencode import in the target
    directory -> opencode --session <new id>

Five rules that are not optional:

1. **Every id is re-identified, and every field that refers to one.** opencode's
   import silently drops rows whose id already exists (`onConflictDoNothing`):
   importing the same export twice yields a session with **zero** messages and
   rc=0, with no warning (spike V1b). So the session id, every message id and
   every part id are new, and `sessionID` / `messageID` / `parentID` are
   rewritten to match (S4, N14).
2. **Ids keep opencode's shape and the original order, and they cannot
   collide.** The hard requirement is the `ses` / `msg` / `prt` prefix — a wrong
   prefix is rejected outright, and there is no length limit (spike V1). The
   order matters because opencode sorts messages by `time.created` then id and
   sends them to the model in that order, so the new ids must sort exactly like
   the old ones (N3). Uniqueness is the other half, and time alone does not give
   it: two messages created in the same millisecond, or one message with no
   `time.created` at all, would give their first parts the same id — and a
   dropped part is invisible afterwards (opencode drops colliding rows without a
   word, and the message count still matches). So both ids carry the message's
   position, which is unique by construction:

       msg_ = msg_ + time(12) + message#(6)          + salt(6)
       prt_ = prt_ + time(12) + message#(6) + part#(4) + salt(6)

   Fixed width, so nothing can be truncated into a collision either.
3. **Import into the target directory.** opencode rewrites `directory`,
   `projectID` and `path` from the current working directory; whatever the export
   file claims is ignored (spike V5). And `opencode run --session <id>` **hangs
   with no output at all** in any other directory, so `cwd` must stay the same
   for the import and for the agent (H1).
4. **Read the session back and count — messages *and* parts.** Import is not
   transactional: a rejected message leaves a half-written session behind, and an
   id collision leaves an empty one (spike V1, trap 4). Comparing only the
   message count would miss a lost part, which is exactly what rule 2's collision
   produces. Both load paths (native and injected) go through the same check, and
   a mismatch deletes the new session before failing, so the id is free again.
5. **stdout of `export` goes to a file, stderr somewhere else.** opencode writes a
   progress line to stderr; merging the two streams (`2>&1`) puts that line in
   front of the JSON and the file no longer parses (spike V1).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import time

from .base import AgentError, Exported, Launch, agent_cmd, format_reading, tool_line

#: Id layout (rule 2). Fixed width on purpose: a truncated ordinal is what made
#: phase 1 import 8 messages and get 1, silently.
_TIME_HEX = 12         # time.created is milliseconds; 48 bits
_MESSAGE_HEX = 6       # position of the message in the export; unique by construction
_PART_HEX = 4          # position of the part inside its message
_SALT_HEX = 6          # 24 bits: different imports (retry, 1->n, n->1) differ

#: The prefix opencode insists on. Nothing else about an id is checked.
PREFIXES = {"session": "ses_", "message": "msg_", "part": "prt_"}

#: The only places an id is rewritten (OC2). A task tool's
#: `state.metadata.sessionId` names a *subagent's* session, and a tool's input or
#: output may hold a user's own `sessionId`; rewriting those would either point a
#: subagent at its parent or corrupt the user's data. So the walk is explicit,
#: not a recursive hunt for keys that look like ids.
_MESSAGE_REFERENCES = ("sessionID", "parentID")
_PART_REFERENCES = ("sessionID", "messageID")

#: Part types that are deliberately silent (D6: no tool results, no thinking).
_SILENT_PARTS = {"reasoning", "step-start", "step-finish", "step-start-finish",
                 "snapshot", "patch", "agent", "compaction", "subtask"}

#: Marks the part agora itself injected. opencode uses `synthetic` for file
#: contents it inlined, which D6 also says not to reproduce, but the two need
#: different lines: ours is worth one line of explanation, its own is noise.
INJECTED_MARK = "agora-injected"
INJECTED_LINE = "[注入的閱讀版]"

#: Every call to opencode gets a deadline. The CLI runs recover_pending at the
#: start of every command, and that calls export, so one wedged opencode would
#: otherwise wedge every agora command after it (OC7). Read per call, not at
#: import, so a test can shorten it.
DEFAULT_CLI_TIMEOUT = 60


def _timeout() -> int:
    try:
        return int(os.environ.get("AGORA_OPENCODE_TIMEOUT", "") or DEFAULT_CLI_TIMEOUT)
    except ValueError:
        return DEFAULT_CLI_TIMEOUT

#: What start_injected asks the agent to do before anything else.
INJECT_PREAMBLE = "以下是之前一個 Session 的閱讀版，請先讀完，再等我的指示。\n\n"

#: The `info` a session needs before opencode will export it again. Import
#: rejects the whole payload when one is missing, and export needs all of them,
#: so a session that cannot be exported cannot be collected after the agent has
#: worked in it (this is how the injected path first lost its result).
#: `directory` / `projectID` are rewritten by import; they are here so the
#: payload is complete on its own.
def _injected_info(session_id: str, title: str, created: int, workdir: Path) -> dict:
    return {
        "id": session_id,
        "slug": "agora-handoff",
        "projectID": "",
        "directory": str(workdir),
        "path": "",
        "title": title,
        "agent": "build",
        "model": {"id": "", "providerID": "opencode", "variant": "default"},
        "version": "",
        "summary": {"additions": 0, "deletions": 0, "files": 0},
        "cost": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        "permission": [],
        "time": {"created": created, "updated": created},
    }


def _session_id() -> str:
    """A fresh opencode-shaped session id: `ses_` + 16 characters."""
    from agora.header import new_ulid

    return PREFIXES["session"] + new_ulid()[-16:]


def _stamp(time_ms: int) -> str:
    return f"{max(int(time_ms), 0) & ((1 << (4 * _TIME_HEX)) - 1):0{_TIME_HEX}x}"


def _message_id(*, time_ms: int, position: int, salt: str) -> str:
    """`msg_` + time + the message's position in the export + salt."""
    return f"{PREFIXES['message']}{_stamp(time_ms)}{position:0{_MESSAGE_HEX}x}{salt}"


def _part_id(*, time_ms: int, message_position: int, part_position: int,
             salt: str) -> str:
    """`prt_` + time + the message's position + the part's position + salt.

    The message position is what makes this unique: opencode orders parts by
    (message_id, id), so inside one message the part position decides, and across
    messages the message position does (OC1).
    """
    return (f"{PREFIXES['part']}{_stamp(time_ms)}"
            f"{message_position:0{_MESSAGE_HEX}x}{part_position:0{_PART_HEX}x}{salt}")


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
    if not isinstance(payload, dict):
        raise AgentError("匯出檔的最外層不是 JSON 物件")
    if not isinstance(payload.get("messages"), list):
        raise AgentError("匯出檔缺少 messages 清單")

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
        new_message_id = _message_id(time_ms=stamp, position=position, salt=salt)
        message_map[str(info.get("id"))] = new_message_id
        info["id"] = new_message_id
        for part_position, part in enumerate(message.get("parts") or []):
            if isinstance(part, dict):
                part_map[str(part.get("id"))] = _part_id(
                    time_ms=stamp, message_position=position,
                    part_position=part_position, salt=salt)
                part["id"] = part_map[str(part.get("id"))]

    # Pass 2: the structural references, and nothing else (OC2, N14).
    out["info"]["id"] = session_id
    for message in out["messages"]:
        info = message["info"]
        info["sessionID"] = session_id
        _remap(info, ("parentID",), message_map)
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            part["sessionID"] = session_id
            _remap(part, ("messageID",), message_map)
    revert = out["info"].get("revert")   # a reverted session's undo pointer (OC6)
    if isinstance(revert, dict):
        _remap(revert, ("messageID",), message_map)
        _remap(revert, ("partID",), part_map)
    return out


def _iso(ms: int | None) -> str | None:
    if not isinstance(ms, int):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload_messages(payload: object) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise AgentError("opencode 的匯出檔形狀不對（缺 messages 清單）")
    return payload["messages"]


def _lines_of(parts: list[dict]) -> list[str]:
    """One reading-version turn's worth of lines, D6 rules applied."""
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        metadata = part.get("metadata")
        if isinstance(metadata, dict) and metadata.get("agora") == INJECTED_MARK:
            lines.append(INJECTED_LINE)   # one line, not the whole reading version
            continue
        if part.get("synthetic") is True:
            continue                     # opencode's own inlined attachment (OC5)
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


def reading_of(payload: dict) -> str:
    """The reading version: user/assistant text plus one line per tool call."""
    turns: list[tuple[str, list[str]]] = []
    for message in _payload_messages(payload):
        info = message.get("info") or {}
        role = info.get("role")
        if role not in ("user", "assistant"):
            continue
        turns.append((role, _lines_of(message.get("parts") or [])))
    return format_reading(turns)


def _injected_payload(session_id: str, body: str, title: str, workdir: Path) -> dict:
    """One user message carrying the whole reading version (N4)."""
    created = int(time.time() * 1000)
    salt = _salt_for(session_id)
    message_id = _message_id(time_ms=created, position=0, salt=salt)
    part_id = _part_id(time_ms=created, message_position=0, part_position=0, salt=salt)
    return {
        "info": _injected_info(session_id, title, created, workdir),
        "messages": [{
            "info": {"id": message_id, "sessionID": session_id, "role": "user",
                     "time": {"created": created}, "agent": "build",
                     "model": {"providerID": "opencode", "modelID": ""},
                     "summary": {"diffs": []}},
            "parts": [{"id": part_id, "sessionID": session_id,
                       "messageID": message_id, "type": "text",
                       "synthetic": True, "metadata": {"agora": INJECTED_MARK},
                       "text": INJECT_PREAMBLE + body}],
        }],
    }


class OpencodeAgent:
    """Speaks to the `opencode` executable; owns nothing else."""

    name = "opencode"

    # -- reading and writing the agent's own files --------------------------

    def _run(self, argv: list[str], cwd: Path | str | None = None) -> subprocess.CompletedProcess:
        """Every call gets a deadline (OC7) and reports it as an AgentError, so a
        wedged opencode becomes a retryable failure instead of a hung command."""
        seconds = _timeout()
        try:
            return subprocess.run(argv, cwd=str(cwd) if cwd else None,
                                  capture_output=True, timeout=seconds)
        except subprocess.TimeoutExpired:
            raise AgentError(f"opencode 逾時（{seconds} 秒）：{argv[1] if len(argv) > 1 else argv[0]}") from None

    def _export_bytes(self, session_id: str, cwd: Path | str | None = None) -> bytes:
        """`opencode export <id>` with stdout going to a file (rule 5)."""
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            target = Path(tmp) / "export.json"
            seconds = _timeout()
            try:
                with open(target, "wb") as out:
                    proc = subprocess.run([agent_cmd(self.name), "export", session_id],
                                          stdout=out, stderr=subprocess.PIPE,
                                          cwd=str(cwd) if cwd else None,
                                          timeout=seconds)
            except subprocess.TimeoutExpired:
                raise AgentError(f"opencode export {session_id} 逾時（{seconds} 秒）") from None
            if proc.returncode != 0:
                message = (proc.stderr or b"").decode("utf-8", "replace").strip()
                if "Session not found" in message:
                    raise AgentError(f"opencode 找不到 Session {session_id}")
                raise AgentError(f"opencode export {session_id} 失敗：{message[-300:]}")
            return target.read_bytes()

    def _cli_version(self) -> str | None:
        """Only a fallback: the version that matters is the one in the export."""
        proc = self._run([agent_cmd(self.name), "--version"])
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip() or None

    def _import(self, payload: dict, cwd: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            path = Path(tmp) / "import.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            proc = self._run([agent_cmd(self.name), "import", str(path)], cwd=cwd)
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise AgentError(f"opencode import 失敗：{message[-300:]}")

    def _delete(self, session_id: str, cwd: Path) -> None:
        """Remove a session we just created. One id at a time, never a pattern."""
        proc = self._run([agent_cmd(self.name), "session", "delete", session_id], cwd=cwd)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-200:]
            raise AgentError(
                f"刪除 {session_id} 也失敗了，請手動 "
                f"`opencode session delete {session_id}`：{detail}")

    def _discard(self, session_id: str, cwd: Path) -> str:
        """Delete the wreckage and say what to put in the message (OC9)."""
        try:
            self._delete(session_id, cwd)
        except AgentError as e:
            return f"而且刪不掉 {session_id}：{e}"
        return f"已經刪掉 {session_id}"

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
        raw = self._export_bytes(session_id)
        payload = self._read(raw)
        info = payload.get("info") or {}
        directory = info.get("directory")
        # OC8: info.version is the version that produced *this* session; the CLI
        # is only asked when the export does not say.
        version = info.get("version")
        return Exported(
            session_id=str(info.get("id") or session_id),
            raw=raw,
            dir=directory if isinstance(directory, str) and directory else None,
            title=info.get("title") if isinstance(info.get("title"), str) else None,
            created_at=_iso(_created_ms(info)),
            agent_version=str(version) if version else self._cli_version(),
            message_count=len(_payload_messages(payload)),
        )

    def reading(self, raw: bytes) -> str:
        return reading_of(self._read(raw))

    def start_native(self, raw: bytes, workdir: Path) -> Launch:
        session_id, landed = self._import_verified(
            reidentify(self._read(raw), session_id=_session_id()), workdir)
        return Launch(argv=[agent_cmd(self.name), "--session", session_id],
                      cwd=str(workdir), agent_session_id=session_id,
                      before_count=landed)

    def start_injected(self, reading_file: Path, workdir: Path) -> Launch:
        body = Path(reading_file).read_text(encoding="utf-8")
        # Same verification as the native path (OC3): an injected session that
        # imported half-way leaves an agent staring at an empty transcript that
        # looks fine, and nothing would say so.
        session_id, landed = self._import_verified(
            _injected_payload(_session_id(), body, "Agora 接續（閱讀版）", workdir), workdir)
        return Launch(argv=[agent_cmd(self.name), "--session", session_id],
                      cwd=str(workdir), agent_session_id=session_id, before_count=landed)

    def collect(self, launch: Launch) -> Exported | None:
        """What the agent produced, or None if it said nothing new (S9)."""
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
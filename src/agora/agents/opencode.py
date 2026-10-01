"""The opencode adapter (design.md 5.2 and 5.4).

    import --format opencode      -> export()
    continue-session --agent opencode            -> start_native()
    continue-session --agent opencode (跨 agent)  -> start_injected()

Loading a session from another machine means writing a new one: export to a
file, re-identify every id, `opencode import` in the target directory, then
`opencode --session <new id>`. Five things about that are not negotiable, and
all five were measured rather than assumed — see docs/spike/opencode.md
(traps 1-6) for the evidence:

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

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import time

from .base import AgentError, Exported, Launch, agent_cmd, format_reading, tool_line

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

#: Marks our own injected part; opencode's own `synthetic` parts get no line (OC5).
INJECTED_MARK = "agora-injected"
INJECTED_LINE = "[注入的閱讀版]"
INJECT_PREAMBLE = "以下是之前一個 Session 的閱讀版，請先讀完，再等我的指示。\n\n"

#: Every call gets a deadline: the CLI collects on every command, so one wedged
#: opencode would wedge all of them (OC7). Read per call so a test can shorten it.
DEFAULT_CLI_TIMEOUT = 60


def _timeout() -> int:
    try:
        return int(os.environ.get("AGORA_OPENCODE_TIMEOUT", "") or DEFAULT_CLI_TIMEOUT)
    except ValueError:
        return DEFAULT_CLI_TIMEOUT


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
    """One user message carrying the whole reading version (N4).

    Ids are placeholders; `reidentify` gives out the real ones, so the id rules
    live in exactly one place. `synthetic` plus the agora mark is what lets the
    reading version of *this* session be one line instead of a nested copy of the
    previous one (OC5).
    """
    created = int(time.time() * 1000)
    payload = {
        "info": _injected_info(session_id, title, created, workdir),
        "messages": [{
            "info": {"id": "tmp-msg", "sessionID": session_id, "role": "user",
                     "time": {"created": created}, "agent": "build",
                     "model": {"providerID": "opencode", "modelID": ""},
                     "summary": {"diffs": []}},
            "parts": [{"id": "tmp-prt", "sessionID": session_id,
                       "messageID": "tmp-msg", "type": "text",
                       "synthetic": True, "metadata": {"agora": INJECTED_MARK},
                       "text": INJECT_PREAMBLE + body}],
        }],
    }
    return reidentify(payload, session_id=session_id)


class OpencodeAgent:
    """Speaks to the `opencode` executable; owns nothing else."""

    name = "opencode"

    # -- reading and writing the agent's own files --------------------------

    def _run(self, argv: list[str], cwd: Path | str | None = None,
             stdout: int | None = None) -> subprocess.CompletedProcess:
        """Every call gets a deadline (OC7) and reports it as an AgentError, so a
        wedged opencode becomes a retryable failure instead of a hung command."""
        seconds = _timeout()
        try:
            return subprocess.run(argv, cwd=str(cwd) if cwd else None, timeout=seconds,
                                  **({"stdout": stdout, "stderr": subprocess.PIPE}
                                     if stdout is not None else {"capture_output": True}))
        except subprocess.TimeoutExpired:
            raise AgentError(f"opencode 逾時（{seconds} 秒）：{argv[1] if len(argv) > 1 else argv[0]}") from None

    def _export_bytes(self, session_id: str, cwd: Path | str | None = None) -> bytes:
        """`opencode export <id>` with stdout going to a file, stderr apart."""
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            target = Path(tmp) / "export.json"
            with open(target, "wb") as out:
                proc = self._run([agent_cmd(self.name), "export", session_id],
                                 cwd=cwd, stdout=out)
            if proc.returncode != 0:
                message = (proc.stderr or b"").decode("utf-8", "replace").strip()
                if "Session not found" in message:
                    raise AgentError(f"opencode 找不到 Session {session_id}")
                raise AgentError(f"opencode export {session_id} 失敗：{message[-300:]}")
            return target.read_bytes()

    def _import(self, payload: dict, cwd: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            path = Path(tmp) / "import.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            proc = self._run([agent_cmd(self.name), "import", str(path)], cwd=cwd)
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise AgentError(f"opencode import 失敗：{message[-300:]}")

    def _discard(self, session_id: str, cwd: Path) -> str:
        """Delete the wreckage, one id at a time, and say what to tell the user."""
        proc = self._run([agent_cmd(self.name), "session", "delete", session_id], cwd=cwd)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-200:]
            return (f"而且刪不掉 {session_id}，請手動 "
                    f"`opencode session delete {session_id}`：{detail}")
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
        version = info.get("version")   # the version that produced *this* session
        return Exported(
            session_id=str(info.get("id") or session_id),
            raw=raw,
            dir=directory if isinstance(directory, str) and directory else None,
            title=info.get("title") if isinstance(info.get("title"), str) else None,
            created_at=_iso(_created_ms(info)),
            agent_version=str(version) if version else None,
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
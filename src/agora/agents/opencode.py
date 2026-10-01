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
2. **Ids keep opencode's shape and the original order.** The hard requirement is
   the `ses` / `msg` / `prt` prefix — a wrong prefix is rejected outright, and
   there is no length limit (spike V1). The order matters because opencode sorts
   messages by `time.created` then id and sends them to the model in that order,
   so the new ids must sort exactly like the old ones (N3). They are therefore
   fixed width: 12 hex of time, 6 hex of position, 6 hex of salt = 28 characters.
   Nothing here can be truncated into a collision.
3. **Import into the target directory.** opencode rewrites `directory`,
   `projectID` and `path` from the current working directory; whatever the export
   file claims is ignored (spike V5). And `opencode run --session <id>` **hangs
   with no output at all** in any other directory, so `cwd` must stay the same
   for the import and for the agent (H1).
4. **Read the session back and count.** Import is not transactional: a rejected
   message leaves a half-written session behind, and an id collision leaves an
   empty one (spike V1, trap 4). So after importing we export again, compare the
   message count, and on a mismatch delete the new session and fail loudly.
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
_TIME_HEX = 12    # time.created is milliseconds; 48 bits
_ORDINAL_HEX = 6  # up to 16,777,216 messages per import, parts per message
_SALT_HEX = 6     # 24 bits: different imports (retry, 1->n, n->1) differ

#: The prefix opencode insists on. Nothing else about an id is checked.
PREFIXES = {"session": "ses_", "message": "msg_", "part": "prt_"}

#: Keys whose value refers to a session, message or part id (N14). Matched
#: case-insensitively so opencode's `sessionID` and any `sessionId` both count.
_REFERENCE_KEYS = {"sessionid", "messageid", "parentid"}

#: Part types that are deliberately silent (D6: no tool results, no thinking).
_SILENT_PARTS = {"reasoning", "step-start", "step-finish", "step-start-finish",
                 "snapshot", "patch", "agent"}

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


def _make_id(kind: str, *, time_ms: int, ordinal: int, salt: str) -> str:
    stamp = f"{max(int(time_ms), 0) & ((1 << (4 * _TIME_HEX)) - 1):0{_TIME_HEX}x}"
    return f"{PREFIXES[kind]}{stamp}{ordinal:0{_ORDINAL_HEX}x}{salt}"


def _salt_for(session_id: str) -> str:
    """Derived from the new session id, not random: re-running the same import
    produces the same ids, so a retry is a no-op instead of a duplicate."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_SALT_HEX]


def _created_ms(info: dict, fallback: int = 0) -> int:
    created = (info.get("time") or {}).get("created")
    return created if isinstance(created, int) else fallback


def _rewrite_references(node: object, *, session_id: str,
                        message_map: dict[str, str]) -> object:
    """Point every reference at the new ids, wherever it sits in the payload."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            name = key.lower()
            if name == "sessionid" and isinstance(value, str):
                out[key] = session_id
            elif name in _REFERENCE_KEYS and isinstance(value, str):
                # A parent outside the imported range is left as it is: import
                # does not check it, and nulling it would make opencode reject
                # the whole assistant message.
                out[key] = message_map.get(value, value)
            else:
                out[key] = _rewrite_references(value, session_id=session_id,
                                               message_map=message_map)
        return out
    if isinstance(node, list):
        return [_rewrite_references(v, session_id=session_id,
                                    message_map=message_map) for v in node]
    return node


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

    # Pass 1: hand out every new id. Doing this before touching references is
    # what lets a parentID pointing forwards resolve.
    message_map: dict[str, str] = {}
    stamp = 0
    for position, message in enumerate(out["messages"]):
        if not isinstance(message, dict) or not isinstance(message.get("info"), dict):
            raise AgentError(f"第 {position} 則訊息形狀不對（缺 info 物件）")
        info = message["info"]
        stamp = _created_ms(info, stamp)
        new_message_id = _make_id("message", time_ms=stamp, ordinal=position, salt=salt)
        message_map[str(info.get("id"))] = new_message_id
        info["id"] = new_message_id
        for part_position, part in enumerate(message.get("parts") or []):
            if isinstance(part, dict):
                part["id"] = _make_id("part", time_ms=stamp,
                                      ordinal=part_position, salt=salt)

    # Pass 2: every reference in the whole payload, at any depth (N14).
    out = _rewrite_references(out, session_id=session_id, message_map=message_map)
    out["info"]["id"] = session_id
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
    message_id = _make_id("message", time_ms=created, ordinal=0, salt=salt)
    part_id = _make_id("part", time_ms=created, ordinal=0, salt=salt)
    return {
        "info": _injected_info(session_id, title, created, workdir),
        "messages": [{
            "info": {"id": message_id, "sessionID": session_id, "role": "user",
                     "time": {"created": created}, "agent": "build",
                     "model": {"providerID": "opencode", "modelID": ""},
                     "summary": {"diffs": []}},
            "parts": [{"id": part_id, "sessionID": session_id,
                       "messageID": message_id, "type": "text",
                       "text": INJECT_PREAMBLE + body}],
        }],
    }


class OpencodeAgent:
    """Speaks to the `opencode` executable; owns nothing else."""

    name = "opencode"

    # -- reading and writing the agent's own files --------------------------

    def _run(self, argv: list[str], cwd: Path | str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=False)

    def _export_bytes(self, session_id: str, cwd: Path | str | None = None) -> bytes:
        """`opencode export <id>` with stdout going to a file (rule 5)."""
        with tempfile.TemporaryDirectory(prefix="agora-opencode-") as tmp:
            target = Path(tmp) / "export.json"
            with open(target, "wb") as out:
                proc = subprocess.run([agent_cmd(self.name), "export", session_id],
                                      stdout=out, stderr=subprocess.PIPE,
                                      cwd=str(cwd) if cwd else None)
            if proc.returncode != 0:
                message = (proc.stderr or b"").decode("utf-8", "replace").strip()
                if "Session not found" in message:
                    raise AgentError(f"opencode 找不到 Session {session_id}")
                raise AgentError(f"opencode export {session_id} 失敗：{message[-300:]}")
            return target.read_bytes()

    def _version(self) -> str | None:
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
        self._run([agent_cmd(self.name), "session", "delete", session_id], cwd=cwd)

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
        return Exported(
            session_id=str(info.get("id") or session_id),
            raw=raw,
            dir=directory if isinstance(directory, str) and directory else None,
            title=info.get("title") if isinstance(info.get("title"), str) else None,
            created_at=_iso(_created_ms(info)),
            agent_version=self._version(),
            message_count=len(_payload_messages(payload)),
        )

    def reading(self, raw: bytes) -> str:
        return reading_of(self._read(raw))

    def start_native(self, raw: bytes, workdir: Path) -> Launch:
        payload = self._read(raw)
        messages = _payload_messages(payload)
        session_id = _session_id()
        rebuilt = reidentify(payload, session_id=session_id)
        self._import(rebuilt, workdir)
        landed = len(_payload_messages(self._read(
            self._export_bytes(session_id, cwd=workdir))))
        if landed != len(messages):
            # Rule 4. Import is not transactional, so the id is now taken by a
            # broken session: drop it before reporting, or the retry hits the
            # collision path and silently produces an empty session.
            self._delete(session_id, workdir)
            raise AgentError(
                f"opencode import 後訊息數不對（來源 {len(messages)} 則，"
                f"匯入後 {landed} 則），已經刪掉 {session_id}；"
                "多半是 id 撞到 opencode 裡既有的 Session")
        return Launch(argv=[agent_cmd(self.name), "--session", session_id],
                      cwd=str(workdir), agent_session_id=session_id,
                      before_count=landed)

    def start_injected(self, reading_file: Path, workdir: Path) -> Launch:
        body = Path(reading_file).read_text(encoding="utf-8")
        session_id = _session_id()
        self._import(_injected_payload(session_id, body, "Agora 接續（閱讀版）", workdir), workdir)
        return Launch(argv=[agent_cmd(self.name), "--session", session_id],
                      cwd=str(workdir), agent_session_id=session_id, before_count=1)

    def collect(self, launch: Launch) -> Exported | None:
        """What the agent produced, or None if it said nothing new (S9)."""
        if not launch.agent_session_id:
            return None
        exported = self.export(launch.agent_session_id)
        if exported.message_count <= launch.before_count:
            return None
        return exported


ADAPTER = OpencodeAgent()
"""Claude Code adapter (design.md 5.2 and 5.4; review N9, N15, S9, S10).

A session is `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`, one JSON
object per line, plus an optional `<uuid>/` sidecar directory (subagent
transcripts, review S9). The raw export packs both (review N15)::

    {"format": "claude-jsonl/1", "main": [<each line, original string>],
     "aux": {"<path relative to <uuid>/>": <utf-8 text or {"$base64": ...}>}}

All storage goes under $AGORA_CLAUDE_HOME (default ~); unit tests point it
at a temp dir, so the real ~/.claude is never touched.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from agora.agents.base import (
    AgentError,
    Exported,
    Launch,
    agent_cmd,
    format_reading,
    tool_line,
)

FORMAT = "claude-jsonl/1"
TITLE_MAX = 60

# Top-level line types that exist but carry nothing for the reading version
# (test-plan 0.1: known-but-skipped types produce no output at all).
_SILENT_TYPES = frozenset({
    "attachment", "queue-operation", "atis-latch", "last-prompt",
    "cost-state", "mode", "summary", "meta", "file-history",
})
# Content-block types inside user/assistant messages, same rule.
_SILENT_BLOCKS = frozenset({"tool_result", "thinking", "redacted_thinking"})


def claude_home() -> Path:
    """$AGORA_CLAUDE_HOME, defaulting to ~ (never hard-code ~/.claude)."""
    return Path(os.environ.get("AGORA_CLAUDE_HOME") or str(Path.home()))


def projects_dir() -> Path:
    return claude_home() / ".claude" / "projects"


def encode_project_dir(workdir: str | Path) -> str:
    """Encode an absolute path the way Claude Code names its project dir.

    Observed on 2.1.286 (spike V2/V5): every '/' becomes '-', '.' too
    (review N9), e.g. /tmp/my-proj.v2 -> -tmp-my-proj-v2 (the leading
    '/' becomes the leading '-').
    """
    abs_path = str(workdir) if os.path.isabs(str(workdir)) else os.path.abspath(workdir)
    return abs_path.replace("/", "-").replace(".", "-")


def find_jsonl(session_id: str) -> Path:
    """Locate <session_id>.jsonl under projects/*/; AgentError if missing."""
    matches = sorted(projects_dir().glob(f"*/{session_id}.jsonl"))
    if not matches:
        raise AgentError(f"[agora] 找不到 Claude session：{session_id}")
    return matches[0]


def _read_lines(path: Path) -> list[str]:
    """File lines as original strings; a half-written last line is dropped
    with a warning (review S10). A broken line anywhere else is an error."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for i, line in enumerate(lines):
        try:
            json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                print(f"[agora] 警告：{path.name} 最後一行不完整，已丟掉", file=sys.stderr)
                return lines[:i]
            raise AgentError(f"[agora] {path.name} 第 {i + 1} 行解析失敗")
    return lines


def _parse_all(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines]


def _count_messages(objs: list[dict]) -> int:
    return sum(1 for o in objs if o.get("type") in ("user", "assistant"))


def _pack_aux(sidecar: Path) -> dict:
    """{relative-posix-path: text} for everything under <uuid>/ (review N15)."""
    aux: dict = {}
    if not sidecar.is_dir():
        return aux
    for path in sorted(sidecar.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            aux[path.relative_to(sidecar).as_posix()] = raw.decode("utf-8")
        except UnicodeDecodeError:
            aux[path.relative_to(sidecar).as_posix()] = {"$base64": base64.b64encode(raw).decode()}
    return aux


def _unpack_aux(aux: dict, sidecar: Path) -> None:
    for rel, content in aux.items():
        dest = sidecar / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and "$base64" in content:
            dest.write_bytes(base64.b64decode(content["$base64"]))
        else:
            dest.write_text(content, encoding="utf-8")


def _pack_raw(main: list[str], aux: dict) -> bytes:
    return json.dumps({"format": FORMAT, "main": main, "aux": aux},
                      ensure_ascii=False).encode("utf-8")


def _unpack_raw(raw: bytes) -> tuple[list[str], dict]:
    try:
        doc = json.loads(raw.decode("utf-8"))
        return list(doc["main"]), dict(doc.get("aux") or {})
    except (ValueError, KeyError, AttributeError) as e:
        raise AgentError(f"[agora] Claude raw 解析失敗：{e}")


def _agent_version() -> str | None:
    """`claude --version` -> '2.1.286'; None when the CLI is missing/broken."""
    try:
        proc = subprocess.run([agent_cmd("claude"), "--version"],
                              capture_output=True, text=True, timeout=15)
        out = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"(\d+\.\d+\.\d+)", out)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _session_dir(objs: list[dict]) -> str | None:
    """source.dir comes from the jsonl's own cwd field (review N9)."""
    for o in objs:
        if isinstance(o.get("cwd"), str):
            return o["cwd"]
    return None


def _user_text(message: object) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _session_title(objs: list[dict]) -> str | None:
    for o in objs:
        if o.get("type") == "summary" and isinstance(o.get("summary"), str):
            return o["summary"][:TITLE_MAX] or None
    for o in objs:
        if o.get("type") == "user":
            text = _user_text(o.get("message")).strip()
            if text:
                return text[:TITLE_MAX]
    return None


def _exported(session_id: str, main: list[str], sidecar: Path) -> Exported:
    objs = _parse_all(main)
    created = objs[0].get("timestamp") if objs and isinstance(objs[0].get("timestamp"), str) else None
    return Exported(
        session_id=session_id,
        raw=_pack_raw(main, _pack_aux(sidecar)),
        dir=_session_dir(objs),
        title=_session_title(objs),
        created_at=created,
        agent_version=_agent_version(),
        message_count=_count_messages(objs),
    )


def _rewrite_lines(lines: list[str], new_id: str, workdir: str) -> list[str]:
    """Point every line at the new session: sessionId always, cwd when present
    (spike V5: fork normalises both; resume keeps appending to the same file)."""
    out = []
    for line in lines:
        if not line.strip():
            continue
        o = json.loads(line)
        o["sessionId"] = new_id
        o["cwd"] = workdir
        out.append(json.dumps(o))
    return out


def _reading_turns(objs: list[dict]) -> list[tuple[str, list[str]]]:
    turns: list[tuple[str, list[str]]] = []
    for o in objs:
        otype = o.get("type")
        if otype == "user":
            lines = _user_lines(o)
            if lines is not None:
                turns.append(("user", lines))
        elif otype == "assistant":
            lines = _assistant_lines(o)
            if lines is not None:
                turns.append(("assistant", lines))
        elif otype in _SILENT_TYPES:
            continue
        else:
            turns.append(("user", [f"[skip {otype}]"]))
    return turns


def _user_lines(o: dict) -> list[str] | None:
    message = o.get("message")
    if isinstance(message, str):
        return [message] if message.strip() else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else None
    if not isinstance(content, list):
        return None
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            lines.append(str(block["text"]))
        elif btype in _SILENT_BLOCKS:
            continue
        else:
            lines.append(f"[skip {btype}]")
    return lines or None


def _assistant_lines(o: dict) -> list[str] | None:
    message = o.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            lines.append(str(block["text"]))
        elif btype == "tool_use":
            lines.append(tool_line(str(block.get("name", "?")), block.get("input")))
        elif btype in _SILENT_BLOCKS:
            continue
        else:
            lines.append(f"[skip {btype}]")
    return lines or None


class ClaudeAgent:
    name = "claude"

    def export(self, session_id: str) -> Exported:
        path = find_jsonl(session_id)
        main = _read_lines(path)
        return _exported(session_id, main, path.parent / session_id)

    def reading(self, raw: bytes) -> str:
        main, _aux = _unpack_raw(raw)
        return format_reading(_reading_turns(_parse_all(main)))

    def start_native(self, raw: bytes, workdir: Path) -> Launch:
        main, aux = _unpack_raw(raw)
        new_id = str(uuid.uuid4())
        workdir_str = str(workdir)
        subdir = projects_dir() / encode_project_dir(workdir)
        subdir.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_lines(main, new_id, workdir_str)
        (subdir / f"{new_id}.jsonl").write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        sidecar = subdir / new_id
        if aux:
            restored = {}
            for rel, content in aux.items():
                if rel.endswith(".jsonl") and isinstance(content, str):
                    restored[rel] = "\n".join(
                        _rewrite_lines(content.split("\n"), new_id, workdir_str))
                else:
                    restored[rel] = content
            _unpack_aux(restored, sidecar)
        return Launch(argv=[agent_cmd("claude"), "--resume", new_id],
                      cwd=workdir_str, agent_session_id=new_id,
                      before_count=_count_messages(_parse_all(rewritten)))

    def start_injected(self, reading_file: Path, workdir: Path) -> Launch:
        new_id = str(uuid.uuid4())
        prompt = (f"@{reading_file.resolve()} 這是之前一個 Session 的閱讀版。"
                  "請先讀完，用兩三句話說明你理解的進度，然後等我的指示。")
        return Launch(argv=[agent_cmd("claude"), "--session-id", new_id, prompt],
                      cwd=str(workdir), agent_session_id=new_id, before_count=0)

    def collect(self, launch: Launch) -> Exported | None:
        if not launch.agent_session_id:
            raise AgentError("[agora] 沒有 agent session id，無法收尾")
        session_id = launch.agent_session_id
        path = projects_dir() / encode_project_dir(launch.cwd) / f"{session_id}.jsonl"
        if not path.is_file():
            path = find_jsonl(session_id)
        main = _read_lines(path)
        if _count_messages(_parse_all(main)) <= launch.before_count:
            return None
        return _exported(session_id, main, path.parent / session_id)


ADAPTER = ClaudeAgent()

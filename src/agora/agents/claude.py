"""Claude Code adapter (design.md 5.2 and 5.4; review N9, N15, S9, S10).

A session is `<config>/projects/<encoded-cwd>/<uuid>.jsonl`, one JSON
object per line, plus an optional `<uuid>/` sidecar directory (subagent
transcripts, review S9). The raw export packs both (review N15)::

    {"format": "claude-jsonl/1", "main": [<each line, original string>],
     "aux": {"<path relative to <uuid>/>": <utf-8 text or {"$base64": ...}>}}

Config location priority (review CL4):
`$AGORA_CLAUDE_HOME/.claude` > `$CLAUDE_CONFIG_DIR` > `~/.claude`.
Unit tests point AGORA_CLAUDE_HOME at a temp dir, so the real config is
never touched.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath

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
    "cost-state", "mode", "summary", "meta", "file-history", "system",
})
# Content-block types inside user/assistant messages, same rule.
_SILENT_BLOCKS = frozenset({"tool_result", "thinking", "redacted_thinking"})
# Local-command bookkeeping inside user lines (review CL2).
_LOCAL_MARKERS = ("<command-name>", "<local-command-stdout>", "<local-command-caveat>")


def claude_home() -> Path:
    """$AGORA_CLAUDE_HOME, defaulting to ~ (never hard-code ~/.claude)."""
    return Path(os.environ.get("AGORA_CLAUDE_HOME") or str(Path.home()))


def config_dir() -> Path:
    """Where Claude keeps projects/ (review CL4)."""
    if os.environ.get("AGORA_CLAUDE_HOME"):
        return claude_home() / ".claude"
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"])
    return Path.home() / ".claude"


def projects_dir() -> Path:
    return config_dir() / "projects"


def encode_project_dir(workdir: str | Path) -> str:
    """Encode an absolute path the way Claude Code names its project dir.

    Measured on 2.1.286 (review CL1): every non-alphanumeric character
    becomes one '-', e.g. /tmp/a_b 專案.v2 -> -tmp-a-b----v2.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(workdir))


def find_jsonl(session_id: str, hint_dir: str | Path | None = None) -> Path:
    """Locate <session_id>.jsonl: try the hinted project dir first (review
    CL12, so tests never scan real directory names), fall back to glob."""
    if hint_dir is not None:
        direct = projects_dir() / encode_project_dir(hint_dir) / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
    matches = sorted(projects_dir().glob(f"*/{session_id}.jsonl"))
    if not matches:
        raise AgentError(f"找不到 Claude session：{session_id}")
    return matches[0]


def _split_lines(text: str, label: str) -> list[str]:
    """Original strings, minus blanks. A half-written last line is dropped
    with a warning (review S10); a broken line anywhere else is an error."""
    raw = text.split("\n")
    if raw and raw[-1] == "":
        raw.pop()
    lines = [line for line in raw if line.strip()]  # CL8: skip blank lines
    out = []
    for i, line in enumerate(lines):
        try:
            json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                print(f"[agora] 警告：{label} 最後一行不完整，已丟掉", file=sys.stderr)
                continue
            raise AgentError(f"{label} 第 {i + 1} 行解析失敗")
        out.append(line)
    return out


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise AgentError(f"{path.name} 不是 UTF-8：{e}")
    return _split_lines(text, path.name)


def _parse_all(lines: list[str]) -> list[dict]:
    return [json.loads(line) for line in lines]


def _is_noise(o: dict) -> bool:
    """Local-command bookkeeping and meta lines (review CL2): not messages,
    not reading material. isCompactSummary lines are kept: they hold the
    post-compact context."""
    if o.get("isCompactSummary"):
        return False
    if o.get("isMeta"):
        return True
    return (o.get("type") == "user"
            and _user_text(o).lstrip().startswith(_LOCAL_MARKERS))


def _count_messages(objs: list[dict]) -> int:
    return sum(1 for o in objs
               if o.get("type") in ("user", "assistant") and not _is_noise(o))


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


def _unpack_aux(aux: dict, sidecar: Path, rewrite=None) -> None:
    """Restore aux files; rewrite(text, rel) transforms .jsonl ones (P12)."""
    for rel, content in aux.items():
        parts = PurePosixPath(rel).parts
        if PurePosixPath(rel).is_absolute() or ".." in parts:  # CL14
            raise AgentError(f"aux 路徑超出範圍：{rel}")
        if (rewrite is not None and rel.endswith(".jsonl")
                and isinstance(content, str)):
            content = rewrite(content, rel)
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
        raise AgentError(f"Claude raw 解析失敗：{e}")


def _session_dir(objs: list[dict]) -> str | None:
    """source.dir comes from the jsonl's own cwd field (review N9)."""
    for o in objs:
        if isinstance(o.get("cwd"), str):
            return o["cwd"]
    return None


def _user_text(o: dict) -> str:
    """All text of a user line joined (P2: reuses _user_lines)."""
    return "".join(line for line in (_user_lines(o) or [])
                   if not line.startswith("[skip "))


def _session_title(objs: list[dict]) -> str | None:
    for o in objs:
        if o.get("type") == "summary" and isinstance(o.get("summary"), str):
            return o["summary"][:TITLE_MAX] or None
    for o in objs:
        if o.get("type") == "user" and not _is_noise(o):
            text = _user_text(o).strip()
            if text:
                return text[:TITLE_MAX]
    return None


def _exported(session_id: str, main: list[str], sidecar: Path) -> Exported:
    objs = _parse_all(main)
    created = next((o["timestamp"] for o in objs  # CL11: first present timestamp
                    if isinstance(o.get("timestamp"), str)), None)
    version = next((o["version"] for o in reversed(objs)  # P3: from the jsonl
                    if isinstance(o.get("version"), str)), None)
    return Exported(
        session_id=session_id,
        raw=_pack_raw(main, _pack_aux(sidecar)),
        dir=_session_dir(objs),
        title=_session_title(objs),
        created_at=created,
        agent_version=version,
        message_count=_count_messages(objs),
    )


def _rewrite_lines(lines: list[str], new_id: str, workdir: str) -> list[str]:
    """Point every line at the new session: sessionId always; cwd only where
    one already exists (review CL7, spike V5: fork only rewrites existing cwd)."""
    out = []
    for line in lines:
        if not line.strip():
            continue
        o = json.loads(line)
        o["sessionId"] = new_id
        if "cwd" in o:
            o["cwd"] = workdir
        out.append(json.dumps(o, ensure_ascii=False))
    return out


def _rewrite_aux_jsonl(content: str, rel: str, new_id: str, workdir: str) -> str:
    """Same S10 rule as main files (review CL6), keeping a trailing newline."""
    text = "\n".join(_rewrite_lines(_split_lines(content, rel), new_id, workdir))
    if text and content.endswith("\n"):
        text += "\n"
    return text


def _reading_turns(objs: list[dict]) -> list[tuple[str, list[str]]]:
    handlers = {"user": _user_lines, "assistant": _assistant_lines}  # P13
    turns: list[tuple[str, list[str]]] = []
    for o in objs:
        otype = o.get("type")
        if otype in handlers:
            if _is_noise(o):
                continue
            lines = handlers[otype](o)
            if lines is not None:
                turns.append((otype, lines))
        elif otype in _SILENT_TYPES:
            continue
        else:
            turns.append(("user", [f"[skip {otype}]"]))
    return turns


def _block_lines(content: list, *, tools: bool) -> list[str] | None:
    """Shared content-block loop (P1): text kept, *_tool_result silent,
    unknown blocks marked. Only assistant lines summarise tool calls."""
    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            lines.append(str(block["text"]))
        elif tools and (btype == "tool_use" or btype.endswith("_tool_use")):  # CL10
            lines.append(tool_line(str(block.get("name", "?")), block.get("input")))
        elif btype in _SILENT_BLOCKS or btype.endswith("_tool_result"):
            continue
        else:
            lines.append(f"[skip {btype}]")
    return lines or None


def _user_lines(o: dict) -> list[str] | None:
    message = o.get("message")
    if isinstance(message, str):
        return [message] if message.strip() else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return [content] if content.strip() else None
    if isinstance(content, list):
        return _block_lines(content, tools=False)
    return None


def _assistant_lines(o: dict) -> list[str] | None:
    message = o.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        return _block_lines(message["content"], tools=True)
    return None


class ClaudeAgent:
    name = "claude"

    def export(self, session_id: str, hint_dir: str | Path | None = None) -> Exported:
        path = find_jsonl(session_id, hint_dir)
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
            _unpack_aux(aux, sidecar, rewrite=lambda text, rel: _rewrite_aux_jsonl(
                text, rel, new_id, workdir_str))
        return Launch(argv=[agent_cmd("claude"), "--resume", new_id],
                      cwd=workdir_str, agent_session_id=new_id,
                      before_count=_count_messages(_parse_all(rewritten)))

    def start_injected(self, reading_file: Path, workdir: Path) -> Launch:
        new_id = str(uuid.uuid4())
        prompt = (f"@{reading_file.resolve()} 這是之前一個 Session 的閱讀版。"
                  "請先讀完，用兩三句話說明你理解的進度，然後等我的指示。")
        return Launch(argv=[agent_cmd("claude"), "--session-id", new_id, prompt],
                      cwd=str(workdir), agent_session_id=new_id, before_count=2)

    def collect(self, launch: Launch) -> Exported | None:
        if not launch.agent_session_id:
            raise AgentError("沒有 agent session id，無法收尾")
        session_id = launch.agent_session_id
        path = find_jsonl(session_id, launch.cwd)
        main = _read_lines(path)
        if _count_messages(_parse_all(main)) <= launch.before_count:
            return None
        return _exported(session_id, main, path.parent / session_id)


ADAPTER = ClaudeAgent()

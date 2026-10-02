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
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from agora.agents.base import AgentError, Exported, Launch, Listed, Turns, agent_cmd, tool_line
from agora.store import normalize

FORMAT = "claude-jsonl/1"
TITLE_MAX = 60
SUMMARIZE_TIMEOUT_S = 600

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

# Cheap field readers for list_sessions: they scan every session file on the
# machine, so we look at raw text and only json.loads the few lines we need.
_TYPE_RE = re.compile(r'"type"\s*:\s*"([A-Za-z_]+)"')
_CWD_RE = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')
_STAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]*)"')
TITLE_HEAD_LINES = 40        # how far into a session the title is taken from (T15)
PREVIEW_MAX = 2000           # last_message text cap (T15)
TAIL_BYTES = 1 << 20         # at most this much of the end of a file is read
_LIST_CACHE: dict = {}       # (path, mtime, size) -> (title, dir, has_text)


def _forget_other_keys(keep: set) -> None:
    """Keep the cache to the files we just walked, so it cannot grow forever."""
    for key in [k for k in _LIST_CACHE if k not in keep]:
        del _LIST_CACHE[key]


def _peek_session(path: Path) -> tuple[str | None, str | None, bool]:
    """(title, cwd, has_text) from the head of a session file; no full parse."""
    title = directory = None
    has_text = False
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n >= TITLE_HEAD_LINES:
                    break
                kind = _TYPE_RE.search(line)
                if not kind or kind.group(1) not in ("user", "assistant", "summary"):
                    continue
                if directory is None:
                    directory = _json_str(_CWD_RE.search(line))
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if _is_noise(o):
                    continue
                if kind.group(1) == "summary" and title is None \
                        and isinstance(o.get("summary"), str) and o["summary"]:
                    title = o["summary"][:TITLE_MAX]
                elif kind.group(1) == "user" and title is None:
                    text = _line_text(o)
                    if text:
                        title, has_text = text[:TITLE_MAX], True
                elif kind.group(1) == "assistant":
                    has_text = has_text or bool(_line_text(o))
    except (OSError, ValueError, TypeError, AttributeError):   # one odd file must not break the list (review U1)
        return None, None, False
    return title, directory, has_text


def _tail_lines(path: Path) -> list[str]:
    """Whole lines from the end of a file, last first, reading at most TAIL_BYTES.

    No budget on line length: a tool result line can be far longer than the
    message we are after, and stopping at it would hide the message.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            blob = f.read()
    except OSError:
        return []
    lines = blob.decode("utf-8", errors="replace").split("\n")
    if size > TAIL_BYTES and len(lines) > 1:
        lines = lines[1:]          # the first one may be cut in half
    return list(reversed(lines))


def _json_str(match: re.Match | None) -> str | None:
    return json.loads(match.group(1)) if match else None


def _line_text(o: dict) -> str | None:
    """The plain text of a user/assistant line, or None if there is none."""
    lines = _user_lines(o) if o.get("type") == "user" else _assistant_lines(o)
    if not lines:
        return None
    return "\n".join(line for line in lines if not line.startswith("[skip ")).strip() or None


def config_dir() -> Path:
    """Where Claude keeps projects/ (review CL4):
    $AGORA_CLAUDE_HOME/.claude, then $CLAUDE_CONFIG_DIR, then ~/.claude.
    Claude Code does not read XDG_DATA_HOME, so neither do we."""
    if os.environ.get("AGORA_CLAUDE_HOME"):
        return Path(os.environ["AGORA_CLAUDE_HOME"]) / ".claude"
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
    # source.dir comes from the jsonl's own cwd field, not the folder name (N9).
    directory = next((o["cwd"] for o in objs if isinstance(o.get("cwd"), str)), None)
    # The model of the most recent assistant line (design v4 source.model).
    model = next((m["model"] for o in reversed(objs) if o.get("type") == "assistant"
                  and isinstance(m := o.get("message"), dict)
                  and isinstance(m.get("model"), str) and m["model"]), None)
    return Exported(
        session_id=session_id,
        raw=_pack_raw(main, _pack_aux(sidecar)),
        dir=directory,
        title=_session_title(objs),
        created_at=created,
        agent_version=version,
        message_count=_count_messages(objs),
        model=model,
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


def _turns(objs: list[dict]) -> list[tuple[str, list[str]]]:
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
        btype = block.get("type") or ""  # CL10: typeless blocks must not crash
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


PLACEHOLDER_ID = "SESSION-PLACEHOLDER"   # start_native replaces it
PLACEHOLDER_CWD = "CWD-PLACEHOLDER"


def _native_lines(turns: Turns) -> list[str]:
    """Turns → Claude jsonl lines, one line per turn, in the order given.

    cli hands us turns that already start with the user, strictly alternate
    user/assistant, and have no empty or `[skip …]` lines, so this just writes
    them out. Each line's uuid/parentUuid chain is what lets `claude --resume`
    pick the conversation up (spike V2); sessionId and cwd are placeholders
    that start_native rewrites to the new session's.
    """
    base = datetime.now(timezone.utc).replace(microsecond=0)
    lines, parent = [], None
    for i, (role, texts) in enumerate(turns):
        uuid_ = str(uuid.uuid4())
        lines.append(json.dumps({
            "type": role, "sessionId": PLACEHOLDER_ID, "uuid": uuid_,
            "parentUuid": parent,
            "timestamp": (base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "cwd": PLACEHOLDER_CWD, "isSidechain": False, "userType": "external",
            "entrypoint": "cli", "gitBranch": "",
            "message": {"role": role, "content": [{"type": "text", "text": t} for t in texts]},
        }, ensure_ascii=False))
        parent = uuid_
    return lines


class ClaudeAgent:
    name = "claude"

    def export(self, session_id: str, hint_dir: str | Path | None = None) -> Exported:
        path = find_jsonl(session_id, hint_dir)
        main = _read_lines(path)
        return _exported(session_id, main, path.parent / session_id)

    def turns(self, raw: bytes) -> Turns:
        main, _aux = _unpack_raw(raw)
        return _turns(_parse_all(main))

    def native(self, turns: Turns) -> bytes:
        """Turns (from any agent, or a merge of several) → a Claude jsonl raw
        that start_native can load directly (design v5 5.4)."""
        return _pack_raw(_native_lines(turns), {})

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

    def list_sessions(self) -> list[Listed]:
        """Every session under projects/, across all projects (design 5.9).

        Read-only, bounded and cached (review T15): the title comes from the
        first few lines, the timestamp from the file, and a file already seen
        with the same (mtime, size) is not read again - a project can hold
        hundreds of MB of jsonl. A file with no real user/assistant text (what
        a post-/clear session leaves behind) is skipped. Unreadable files are
        skipped, never raised.
        """
        found: list[Listed] = []
        root = projects_dir()
        if not root.is_dir():
            return found
        try:
            paths = [p for p in root.glob("*/*.jsonl") if p.is_file() and not p.is_symlink()]
        except OSError:
            return found
        seen: set = set()
        for path in sorted(paths):
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (str(path), stat.st_mtime, stat.st_size)
            seen.add(key)
            cached = _LIST_CACHE.get(key)
            if cached is None:
                cached = _LIST_CACHE[key] = _peek_session(path)
            title, directory, has_text = cached
            if not has_text:
                continue
            found.append(Listed(
                session_id=path.stem, dir=directory, title=title,
                updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")))
        _forget_other_keys(seen)
        found.sort(key=lambda item: item.updated_at or "", reverse=True)
        return found

    def last_message(self, session_id: str) -> tuple[str, str] | None:
        """(role, text) of the last real message, at most PREVIEW_MAX chars.

        Reads only the tail of the file (review T15/T12): local commands and
        isMeta lines are skipped, a half-written last line is ignored, and a
        session we cannot read gives None instead of raising.
        """
        try:
            path = find_jsonl(session_id)
        except (AgentError, OSError):
            return None
        for line in _tail_lines(path):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or o.get("type") not in ("user", "assistant") or _is_noise(o):
                continue
            text = _line_text(o)
            if text:
                return o["type"], text[-PREVIEW_MAX:]
        return None

    def search_text(self, keyword: str) -> Iterator[str]:
        """Session ids whose conversation text contains keyword, yielded as found.

        Streams every project file line by line - never loads one whole - and
        yields on the first hit in a file, so the caller gets results early.
        Only user/assistant text counts (tool results and local-command noise
        do not); matching is NFKC-normalised and case-insensitive, the same way
        the search index compares. Unreadable files are skipped.
        """
        needle = normalize(keyword)
        if not needle:
            return
        try:
            paths = sorted(projects_dir().glob("*/*.jsonl"))
        except OSError:
            return
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as f:
                    for line in f:
                        kind = _TYPE_RE.search(line)
                        if not kind or kind.group(1) not in ("user", "assistant"):
                            continue
                        try:
                            o = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(o, dict) or _is_noise(o):   # one odd line must not end the search
                            continue
                        text = _line_text(o)
                        if text and needle in normalize(text):
                            yield path.stem
                            break
            except (OSError, ValueError, TypeError, AttributeError):
                continue

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        """One headless `claude -p` that cannot use any tool (design 5.3, review Y3).

        `--tools ""` turns off every built-in tool (and any tool added later),
        `--strict-mcp-config` keeps the user's MCP servers out, and
        `--setting-sources ""` skips the user's hooks and settings. The prompt
        goes in on stdin, never after a variable-length flag (Y1).
        `--no-session-persistence` means nothing is written under
        projects/, so there is no session to delete afterwards (Y3).
        """
        argv = [agent_cmd("claude"), "-p", "--tools", "", "--strict-mcp-config",
                "--setting-sources", "", "--no-session-persistence",
                "--output-format", "json"]
        try:
            proc = subprocess.run(argv, input=prompt, cwd=str(workdir), env=_child_env(),
                                  capture_output=True, text=True, timeout=_summarize_timeout())
        except (OSError, subprocess.SubprocessError) as e:
            raise AgentError(f"Claude 要約失敗：{e}")
        if proc.returncode != 0:
            raise AgentError(f"Claude 要約失敗（rc={proc.returncode}）：{(proc.stderr or proc.stdout).strip()[-300:]}")
        try:
            doc = json.loads(proc.stdout or "{}")
        except ValueError:
            doc = {"result": proc.stdout}
        text = str(doc.get("result") or "").strip()
        if not text:
            raise AgentError("Claude 沒有回覆")
        return text, _used_model(doc)

    def collect(self, launch: Launch) -> Exported | None:
        if not launch.agent_session_id:
            raise AgentError("沒有 agent session id，無法收尾")
        session_id = launch.agent_session_id
        path = find_jsonl(session_id, launch.cwd)
        _warn_cleared(path, session_id)
        main = _read_lines(path)
        if _count_messages(_parse_all(main)) <= launch.before_count:
            return None
        return _exported(session_id, main, path.parent / session_id)


def _used_model(doc: dict) -> str | None:
    """The model claude reports for this run (--output-format json has no
    plain `model` field, only the modelUsage breakdown)."""
    if isinstance(doc.get("model"), str):
        return doc["model"]
    usage = doc.get("modelUsage")
    if isinstance(usage, dict) and usage:   # the model that wrote the most, not a helper model
        return max(usage, key=lambda m: (usage[m] or {}).get("outputTokens", 0) if isinstance(usage[m], dict) else 0)
    return None


def _child_env() -> dict:
    """HOME follows AGORA_CLAUDE_HOME when tests set it; otherwise the user's own environment."""
    if os.environ.get("AGORA_CLAUDE_HOME"):
        return {**os.environ, "HOME": os.environ["AGORA_CLAUDE_HOME"]}
    return dict(os.environ)


def _summarize_timeout() -> float:
    return float(os.environ.get("AGORA_SUMMARIZE_TIMEOUT", SUMMARIZE_TIMEOUT_S))


def _warn_cleared(path: Path, session_id: str) -> None:
    """/clear moves the conversation to a new uuid in the same project dir.

    Verified on 2.1.286 (CL5): interactive --resume keeps appending to the
    same file, but after /clear the new file only points back through a
    `session_id` field on its attachment lines. We do not stitch the two
    together; we say which session holds the rest so it can be imported.
    """
    since = path.stat().st_ctime
    for other in path.parent.glob("*.jsonl"):
        if other == path or other.stat().st_mtime < since:
            continue
        try:
            if session_id in other.read_text(encoding="utf-8", errors="replace"):
                print(f"[agora] 這次接續中用過 /clear，之後的對話在 Claude session {other.stem}；"
                      f"要存進 Agora 請另外執行：agora import session --external-session-id {other.stem} --agent claude",
                      file=sys.stderr)
        except OSError:
            continue


ADAPTER = ClaudeAgent()

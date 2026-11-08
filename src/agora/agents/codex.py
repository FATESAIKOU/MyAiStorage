"""Read-only Codex rollout adapter (#443). Only sessions/ is inspected."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from agora.agents.base import AgentError, Exported, Listed, iso_utc, tool_line, warn

CODEX_CONTINUE_LATER = "codex 第一版只支援 import／讀取（#443）；接續 codex 匯入的 Session 請用 claude 或 opencode（跨 agent，會寫回同一個 Session）"
CODEX_MERGE_LATER = "codex 第一版不寫要約；請用 opencode 或 claude"
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _root():
    return Path(os.environ.get("AGORA_CODEX_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"


def _rows(raw):
    lines = raw.splitlines()
    rows = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError) as exc:
            # A wholly invalid file is an error, even if it consists of one line.
            if rows and i == len(lines) - 1 and not raw.endswith(b"\n"):
                warn("codex rollout 最後一行不完整，已略過")
                break
            raise AgentError("codex rollout 不是有效 JSONL") from exc
        rows.append(row)
    return rows


def _parse(rows):
    turns, texts = [], []
    for row in rows:
        kind, p = row.get("type"), row.get("payload", {})
        if not isinstance(p, dict):
            turns.append(("assistant", [f"[skip {kind}]"]))
            continue
        if kind == "response_item":
            t = p.get("type")
            if t == "message":
                role = p.get("role")
                if role in ("user", "assistant"):
                    lines = [c["text"] for c in p.get("content", [])
                             if isinstance(c, dict) and c.get("type") in ("input_text", "output_text")
                             and isinstance(c.get("text"), str) and c["text"].strip()]
                    if lines:
                        turns.append((role, lines))
                        texts.append((role, "\n".join(lines)))
            elif t == "custom_tool_call":
                turns.append(("assistant", [tool_line(p.get("name", "?"), p.get("input", ""))]))
            elif t not in ("custom_tool_call_output", "reasoning", "additional_tools"):
                turns.append(("assistant", [f"[skip {t}]"]))
        elif kind not in ("session_meta", "turn_context", "event_msg", "world_state", "token_usage_record"):
            turns.append(("assistant", [f"[skip {kind}]"]))
    return turns, texts


def _read(path):
    try:
        for _ in range(2):
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
                break
        return raw
    except OSError as exc:
        raise AgentError(f"讀不到 codex rollout：{path}") from exc


def _files():
    root = _root()
    if not root.exists():
        return []
    try:
        return sorted(root.rglob("rollout-*.jsonl"))
    except OSError as exc:
        raise AgentError("讀不到 codex sessions") from exc


def _meta(rows):
    return next((r.get("payload", {}) for r in rows if r.get("type") == "session_meta"), {})


def _id(path, rows):
    meta = _meta(rows)
    return meta.get("id") or meta.get("session_id") or next(iter(_UUID.findall(path.name)), None)


class Codex:
    name = "codex"

    def export(self, session_id):
        if not _UUID.fullmatch(session_id):
            raise AgentError("codex session id 不合法")
        for path in _files():
            # Filenames normally contain the id; metadata is authoritative and
            # also supports renamed authorized samples.
            raw = _read(path)
            rows = _rows(raw)
            if _id(path, rows) != session_id:
                continue
            turns, texts = _parse(rows)
            meta = _meta(rows)
            model = next((r.get("payload", {}).get("model") for r in reversed(rows)
                          if r.get("type") == "turn_context"), None)
            return Exported(session_id, raw=raw, turns=turns,
                            message_count=len(texts), dir=meta.get("cwd"),
                            title=next((t[:80] for role, t in texts if role == "user"), None),
                            created_at=meta.get("timestamp"), agent_version=meta.get("cli_version"), model=model)
        raise AgentError(f"找不到 codex session：{session_id}")

    def turns(self, raw):
        return _parse(_rows(raw))[0]

    def list_sessions(self):
        listed = []
        for path in _files():
            rows = _rows(_read(path))
            sid = _id(path, rows)
            if sid:
                texts = _parse(rows)[1]
                try:
                    updated = iso_utc(path.stat().st_mtime)
                except OSError as exc:
                    raise AgentError("讀不到 codex rollout 時間") from exc
                listed.append(Listed(sid, _meta(rows).get("cwd"),
                                     next((t[:80] for r, t in texts if r == "user"), None), updated))
        return listed

    def last_message(self, session_id):
        # Preview is optional: inspect only the header and a bounded tail, never
        # load a large rollout just because the TUI cursor moved onto it.
        if not _UUID.fullmatch(session_id):
            return None
        for path in _files():
            try:
                with path.open("rb") as stream:
                    first = stream.readline()
                    try:
                        header = json.loads(first) if first.strip() else {}
                    except (ValueError, UnicodeDecodeError):
                        header = {}
                    if _id(path, [header] if isinstance(header, dict) else []) != session_id:
                        continue
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(max(0, size - 65536))
                    lines = stream.read().splitlines()
                if size > 65536:
                    lines = lines[1:]  # the first line may be cut in half
                for line in reversed(lines):
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(row, dict):
                        texts = _parse([row])[1]
                        if texts:
                            return texts[-1]
                return None
            except OSError:
                return None
        return None

    def search_text(self, keyword, only=None):
        from agora.store import normalize
        needle = normalize(keyword)
        if not needle:
            return
        for item in self.list_sessions():
            if only is None or item.session_id in only:
                texts = _parse(_rows(self.export(item.session_id).raw))[1]
                if any(needle in normalize(text) for _, text in texts):
                    yield item.session_id

    def native(self, turns):
        raise AgentError(CODEX_CONTINUE_LATER)

    def append_native(self, raw, turns):
        raise AgentError(CODEX_CONTINUE_LATER)

    def summarize(self, prompt, workdir):
        raise AgentError(CODEX_MERGE_LATER)

    def start_native(self, raw, workdir, *, headless=False, headless_port=None,
                     harness_file=None, extra_args=()):
        raise AgentError(CODEX_CONTINUE_LATER)

    def collect(self, launch):
        raise AgentError(CODEX_CONTINUE_LATER)


ADAPTER = Codex()

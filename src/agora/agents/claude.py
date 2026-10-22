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
import hashlib
import io
import json
import os
import re
import subprocess
import uuid
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from agora.agents.base import (AgentError, Exported, Increment, Launch, Listed, NO_REPLY, Turns, agent_cmd,
                               iso_utc, merge_turns, summarize_timeout, tool_line, warn)
from agora.private_tmp import PrivateTmpDir
from agora.store import normalize

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

# Cheap field readers for list_sessions: they scan every session file on the
# machine, so we look at raw text and only json.loads the few lines we need.
_TYPE_RE = re.compile(r'"type"\s*:\s*"([A-Za-z_]+)"')
_CWD_RE = re.compile(r'"cwd"\s*:\s*("(?:[^"\\]|\\.)*")')
_STAMP_RE = re.compile(r'"timestamp"\s*:\s*"([^"]*)"')
TITLE_HEAD_LINES = 40        # how far into a session the title is taken from (T15)
PREVIEW_MAX = 2000           # last_message text cap (T15)
TAIL_BYTES = 1 << 20         # at most this much of the end of a file is read
_LIST_CACHE: dict = {}       # (path, mtime, size) -> (title, dir, has_text)


def _said(line: str, kinds: tuple[str, ...]) -> tuple[str, dict, str | None] | None:
    """(kind, object, text) for a line that says something, or None for one that does not.

    The three readers - the listing's head peek, the tail for a preview, the search -
    each want the same four things and each had its own copy of them (review C1): is
    this a kind we care about, does it parse, is it bookkeeping rather than a message,
    and what text does it hold.

    Which kind it is comes from the *parsed* line, not from the first `"type"` the
    regex sees: an assistant line can carry a nested `message.type` ahead of its own,
    and reading that one skipped the line and showed an older message instead
    (review K2). The regex is only the cheap question "is it worth parsing at all".
    """
    if not set(kinds).intersection(_TYPE_RE.findall(line)):
        return None
    try:
        o = json.loads(line)
    except ValueError:
        return None
    kind = o.get("type") if isinstance(o, dict) else None
    if kind not in kinds or _is_noise(o):
        return None
    return kind, o, _line_text(o)


def _title_of(o: dict) -> str | None:
    """The title one line offers: its summary, or a real user turn's first words.

    A local command is not a message, so it never offers one - without this the
    export named a session `<command-name>/model</command-name>` while the listing
    showed what the user actually said (review K1).
    """
    if o.get("type") == "summary" and isinstance(o.get("summary"), str) and o["summary"]:
        return o["summary"][:TITLE_MAX]
    if o.get("type") == "user" and not _is_noise(o) and (text := _line_text(o)):
        return text[:TITLE_MAX]
    return None


def _peek_session(path: Path) -> tuple[str | None, str | None, bool]:
    """(title, cwd, has_text) from the head of a session file; no full parse."""
    title = directory = None
    has_text = False
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n >= TITLE_HEAD_LINES:
                    break
                said = _said(line, ("user", "assistant", "summary"))
                if said is None:
                    continue
                kind, o, text = said
                if directory is None:
                    match = _CWD_RE.search(line)
                    directory = json.loads(match.group(1)) if match else None
                if title is None and (offered := _title_of(o)):
                    title, has_text = offered, has_text or kind == "user"
                elif kind == "assistant":
                    has_text = has_text or bool(text)
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
                warn(f"警告：{label} 最後一行不完整，已丟掉")
                continue
            raise AgentError(f"{label} 第 {i + 1} 行解析失敗")
        out.append(line)
    return out


def _iter_lines(path: Path) -> Iterator[str]:
    """The session's non-blank lines, one at a time, never the whole file (B1a).

    The file is never held as one string plus its `split("\\n")` copy: each line
    is yielded as it is read. Validation, the one parse per line and the packed
    write all happen in the caller's single pass (`_stream_raw`, design B.2).
    """
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                line = raw[:-1] if raw.endswith("\n") else raw
                if line.strip():                 # CL8: skip blank lines
                    yield line
    except UnicodeDecodeError as e:
        # The streaming decoder reports a position inside its read buffer, while
        # HEAD's whole-file read reported a file position (and that position is in
        # the user-facing message). Reproduce the old message on this failing path
        # only; a valid session never pays for the second read.
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError as whole:
            e = whole
        raise AgentError(f"{path.name} 不是 UTF-8：{e}")


def _read_lines(path: Path) -> list[str]:
    """All non-blank lines as a list.

    Not on the export path any more (it streams, `_stream_raw`); measurements and
    the idle check (`tests/perf`, `tests/integration`) still ask for the lines.
    """
    return list(_iter_lines(path))


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
            and (_line_text(o) or "").lstrip().startswith(_LOCAL_MARKERS))


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
    # C4 (impl-mem 2a): json.dump straight into a byte buffer - json.dumps's
    # whole-document str plus its encode would hold two copies of the raw at
    # the peak. The chunked writer produces the same bytes as dumps did.
    # newline="": no os.linesep translation of the "\n" json.dump may emit (the
    # packed output has no bare newline today - json escapes the ones inside
    # strings - but spelling it out keeps that true if anyone adds an indent).
    buf = io.BytesIO()
    writer = io.TextIOWrapper(buf, encoding="utf-8", newline="", write_through=True)
    try:
        json.dump({"format": FORMAT, "main": main, "aux": aux}, writer, ensure_ascii=False)
        writer.flush()   # clarity only; the detach below flushes the wrapper and encoder anyway (review 低3)
    finally:
        writer.detach()           # flush the encoder without closing buf
    return buf.getvalue()


#: The envelope's fixed bytes (design B.2); `_stream_raw` writes them as it reads.
_RAW_HEAD = b'{"format": "claude-jsonl/1", "main": ['
_AUX_SEP = b'], "aux": '


@dataclass
class PackedRaw:
    """What the one streaming pass knows about the raw it wrote (design B.2).

    These are the offsets and digests R3's cursor is built from (`append_from` /
    `seal_upto`, the three sealed values) and they stay in the adapter: they are
    not part of `Exported` (design B.2 point 3).
    """

    path: Path
    md5: str                  # md5 of the whole raw file
    size: int
    main_end: int             # byte offset where `], "aux": ` starts (tail begins here)
    main_count: int           # main elements written (the non-blank, valid lines)
    main_md5: str             # md5 of raw[0:main_end]
    stable_at: array          # stable_at[n] = offset after n elements; len = main_count + 1
    _stable_md5: dict[int, str] = field(default_factory=dict, repr=False, compare=False)

    def stable_md5(self, n: int) -> str | None:
        """md5 of raw[0:stable_at(n)], when this pass captured that boundary.

        The caller names the one boundary it needs (`capture=`); copying an md5
        state for every element would be one live state per line, and R3 only
        ever asks about the cursor's own `sealed_line_count`.
        """
        return self._stable_md5.get(n)


def _unpack_raw(raw: bytes) -> tuple[list[str], dict]:
    try:
        doc = json.loads(raw.decode("utf-8"))
        return list(doc["main"]), dict(doc.get("aux") or {})
    except (ValueError, KeyError, AttributeError) as e:
        raise AgentError(f"Claude raw 解析失敗：{e}")


_MISSING = object()


@dataclass
class _Facts:
    """The small facts one streaming pass keeps from a session's lines (B1a).

    These are what `_exported` used to collect in six scans over a list of every
    parsed object; the objects themselves are dropped line by line now.
    """

    created_at: str | None = None
    agent_version: str | None = None
    directory: str | None = None
    model: str | None = None
    summary_seen: bool = False      # a summary line with a string `summary` exists
    summary: str | None = None      # ... and its value, even when it is empty
    first_title: str | None = None  # first user text, used only when no summary matched
    message_count: int = 0
    turns: Turns = field(default_factory=list)

    @property
    def title(self) -> str | None:
        """A summary anywhere wins, else the first real user turn (review K4)."""
        return self.summary if self.summary_seen else self.first_title


def _take_turn(turns: Turns, o: dict) -> None:
    """The `_turns` mapping for one line, kept as the line goes by (P13)."""
    handlers = {"user": _user_lines, "assistant": _assistant_lines}
    otype = o.get("type")
    if otype in handlers:
        if _is_noise(o):
            return
        lines = handlers[otype](o)
        if lines is not None:
            turns.append((otype, lines))
    elif otype in _SILENT_TYPES:
        return
    else:
        turns.append(("user", [f"[skip {otype}]"]))


def _accumulate(facts: _Facts, o: dict) -> None:
    """Fold one parsed line into the export's fields, in HEAD's own rules."""
    if facts.created_at is None and isinstance(o.get("timestamp"), str):
        facts.created_at = o["timestamp"]              # CL11: first present timestamp
    if isinstance(o.get("version"), str):
        facts.agent_version = o["version"]             # P3: the last one in the file
    if facts.directory is None and isinstance(o.get("cwd"), str):
        facts.directory = o["cwd"]                     # N9: from the jsonl, not the folder
    message = o.get("message")
    if (o.get("type") == "assistant" and isinstance(message, dict)
            and isinstance(message.get("model"), str) and message["model"]):
        facts.model = message["model"]                 # design v4: the most recent assistant
    if (not facts.summary_seen and o.get("type") == "summary"
            and isinstance(o.get("summary"), str)):
        facts.summary_seen = True
        facts.summary = o["summary"][:TITLE_MAX] or None
    if not facts.summary_seen and facts.first_title is None:
        facts.first_title = _title_of(o)
    if o.get("type") in ("user", "assistant") and not _is_noise(o):
        facts.message_count += 1
    _take_turn(facts.turns, o)


def _stream_raw(path: Path, sidecar: Path, out_path: Path, *,
                label: str = "session",
                capture: "Iterable[int] | bool" = ()) -> tuple[_Facts, PackedRaw]:
    """Read, validate, fold and pack the session in one streaming pass (B.2).

    This is `_read_lines` + `_scan` + `_pack_raw_file` as one loop: each line is
    read, parsed once, folded into `_Facts` and - once it is known not to be the
    last one - written to the raw file. Only **one line** is buffered, which is
    what makes a half-written last line droppable without holding the file: the
    line being read now proves the buffered one is not last; at EOF the buffered
    one is decided on its own.

    The observable rules are HEAD's, unchanged:
      - blank lines are skipped before they are counted, so the "line N" in an
        error counts non-blank lines only (CL8);
      - a line that is not JSON and is *not* last is `AgentError` with its
        non-blank line number; the *last* non-blank line, if it is not JSON, is
        dropped with a warning (a writer still going, review S10);
      - a valid-JSON non-dict line is remembered and raised as the same
        `AttributeError` at the end, after validation has had its say;
      - a post-parse error from `_accumulate` is kept as the first one and
        re-raised only after the whole file validated, so a syntax error below
        it still wins.

    One difference from HEAD is intended (#158): HEAD decoded the whole file before it looked at a
    single line, so a file with a syntax error early *and* non-UTF-8 bytes later said "not UTF-8".
    The stream reaches the early line first and says "line N does not parse"; the encoding error
    only comes first when the bad bytes sit within the first read buffer (a short file). Both
    refuse the file with an `AgentError`, and only the message differs.
    Nothing is written for a session that fails validation except the partial
    file in the caller's private temp dir, which the caller removes.

    `capture` names main-element boundaries whose md5 the pass should snapshot
    (`True` = every boundary, for the dual-run test). `PackedRaw` keeps the
    offsets and `main_md5` for R3's cursor; `Exported` gets none of it.
    """
    facts = _Facts()
    digest = hashlib.md5()
    size = 0
    offsets = array("q", [0])       # 8 bytes per element, not one Python int each
    captured: dict[int, str] = {}
    capture_all = capture is True
    wanted = set() if capture_all else set(capture)
    non_dict: object = _MISSING
    first_error: Exception | None = None
    written = 0

    with out_path.open("wb") as f:
        def write(piece: bytes) -> None:
            nonlocal size
            f.write(piece)
            digest.update(piece)
            size += len(piece)

        def emit(line: str, parsed: object) -> None:
            """Fold one valid line into the facts and write it to the raw."""
            nonlocal written, non_dict, first_error
            if not isinstance(parsed, dict):
                # HEAD's first scan (`created`) called `.get` on every object, so any
                # non-dict anywhere wins over the later scans' errors.
                if non_dict is _MISSING:
                    non_dict = parsed
            elif first_error is None:
                try:
                    _accumulate(facts, parsed)
                except Exception as e:        # noqa: BLE001 - re-raised after validation
                    # Drop the traceback: it would keep this line's dict (possibly a
                    # large tool_result) alive until the end of the file.
                    first_error = e.with_traceback(None)
            if written:
                write(b", ")
            write(json.dumps(line, ensure_ascii=False).encode("utf-8"))
            written += 1
            offsets.append(size)
            if capture_all or written in wanted:
                captured[written] = digest.copy().hexdigest()

        if capture_all or 0 in wanted:      # the empty prefix's md5 (R3: sealed at line 0)
            captured[0] = digest.copy().hexdigest()
        write(_RAW_HEAD)
        pending: str | None = None
        for line in _iter_lines(path):
            if pending is not None:
                try:
                    parsed = json.loads(pending)
                except json.JSONDecodeError:
                    # `written` non-blank lines came before this one.
                    raise AgentError(f"{label} 第 {written + 1} 行解析失敗") from None
                emit(pending, parsed)
            pending = line
        if pending is not None:
            try:
                parsed = json.loads(pending)
            except json.JSONDecodeError:
                warn(f"警告：{label} 最後一行不完整，已丟掉")
            else:
                emit(pending, parsed)
        if non_dict is not _MISSING:
            raise AttributeError(f"'{type(non_dict).__name__}' object has no attribute 'get'")
        if first_error is not None:
            raise first_error
        aux = _pack_aux(sidecar)
        main_end = size
        main_md5 = digest.copy().hexdigest()
        write(_AUX_SEP)
        write(json.dumps(aux, ensure_ascii=False).encode("utf-8"))
        write(b"}")
        f.flush()
        os.fsync(f.fileno())

    packed = PackedRaw(path=out_path, md5=digest.hexdigest(), size=size,
                       main_end=main_end, main_count=written, main_md5=main_md5,
                       stable_at=offsets, _stable_md5=captured)
    return facts, packed


def _exported(session_id: str, path: Path, sidecar: Path, *,
              label: str = "session",
              capture: "Iterable[int] | bool" = ()) -> tuple[Exported, PackedRaw]:
    """The export and the streaming facts R3's cursor is built from (B.2)."""
    tmpdir = PrivateTmpDir("agora-raw")
    raw_path = Path(tmpdir.name) / "raw.json"
    try:
        facts, packed = _stream_raw(path, sidecar, raw_path, label=label, capture=capture)
    except BaseException:
        tmpdir.cleanup()
        raise
    exported = Exported(
        session_id=session_id,
        raw_file=raw_path,
        raw_md5=packed.md5,
        raw_size=packed.size,
        _raw_tmpdir=tmpdir,
        dir=facts.directory,
        title=facts.title,
        created_at=facts.created_at,
        agent_version=facts.agent_version,
        message_count=facts.message_count,
        model=facts.model,
        turns=facts.turns,
    )
    return exported, packed


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


def _native_lines(turns: Turns, parent: str | None = None) -> list[str]:
    """Turns → Claude jsonl lines, one line per turn, in the order given.

    cli hands us turns that already start with the user, strictly alternate
    user/assistant, and have no empty or `[skip …]` lines, so this just writes
    them out. Each line's uuid/parentUuid chain is what lets `claude --resume`
    pick the conversation up (spike V2); sessionId and cwd are placeholders
    that start_native rewrites to the new session's.

    `parent` is the uuid the first line hangs off - None for a conversation built
    from scratch, the last real line's uuid when appending to one (F4).
    """
    base = datetime.now(timezone.utc).replace(microsecond=0)
    lines = []
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


def _leaf_uuid(main: list[str]) -> str | None:
    """The uuid a new line has to hang off: the last line that has one and is not
    a sidechain (F4).

    Not the last line. A session file ends with bookkeeping - `summary`,
    `queue-operation`, `last-prompt`, `cost-state`, `mode` - and those carry no
    uuid, so the tail of the file is not where the conversation is. `claude
    --resume` walks parentUuid back from the newest leaf, so a chain hung off
    the wrong line leaves the agent looking at a conversation that has none of A
    in it, and nothing says so. Sidechain lines are a subagent's own branch
    (`isSidechain: true`): the main thread never continues them.
    """
    for line in reversed(main):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or o.get("isSidechain") is True:
            continue
        if isinstance(o.get("uuid"), str) and o["uuid"].strip():
            return o["uuid"]
    return None


def _tool_use_id(block: object) -> str | None:
    """The tool this block calls, or None for anything that is not a call.

    `tool_use` and the server-side `…_tool_use` both count (CL10's shapes); the
    matching `tool_result` / `…_tool_result` names it through `tool_use_id`.
    """
    if not isinstance(block, dict):
        return None
    kind = str(block.get("type") or "")
    return block.get("id") if kind == "tool_use" or kind.endswith("_tool_use") else None


def _tool_result_id(block: object) -> str | None:
    """The tool this block answers, or None for anything that is not an answer."""
    if not isinstance(block, dict):
        return None
    kind = str(block.get("type") or "")
    return block.get("tool_use_id") if kind == "tool_result" or kind.endswith("_tool_result") else None


def _asked_without_an_answer(objs: list[dict]) -> bool:
    """Whether the session ends on a question nobody answered (design 5.4 W1).

    One rule, the same one `cli._converted_turns` uses for another agent's session:
    take the reading version's turns, drop the `[skip …]` lines, and look at what is
    last. A user turn last means the question is still open, and the fork's first
    line would land on it and read as its answer.

    Deciding it on the reading version rather than on the last line is what keeps the
    two adapters and the cross-agent path agreeing. Three shapes the reading version
    has already collapsed, all of which mean the same thing here:

    - a line type `turns()` does not know becomes `[skip widget]` - the bookkeeping
      a real session file ends with (`progress`, `custom-title`) lands this way, and
      it is not the user speaking again;
    - a user line that only carries tool results says nothing, so an assistant that
      answered is still the last thing said;
    - an assistant line with only a thinking block is dropped entirely, so a run that
      was cut short mid-thought leaves the user's question as the last turn.

    The other shape is a `tool_use` that never got its `tool_result` (F4). Parallel
    calls are why that looks further back than one line: claude writes a line per
    content block, so a turn that calls two tools is two lines and the line after
    them holds whichever result arrived first - so every `tool_use` from the last
    question on is collected, and the ones with a matching `tool_result` are taken
    back out.

    What goes in is a stand-in reply, not a `tool_result`. Measured on a real session
    that ends on an unanswered `tool_use` (tasks 1.7): the fork resumes on the
    stand-in alone, and the API does not refuse the unpaired call.
    """
    said = merge_turns([(role, [line for line in lines if not line.startswith("[skip ")])
                        for role, lines in _turns(objs)])
    if said and said[-1][0] == "user":
        return True
    return _left_a_tool_waiting(objs)


def _left_a_tool_waiting(objs: list[dict]) -> bool:
    """Whether the last exchange asked for a tool call that never came back.

    Only the last exchange: an unanswered `tool_use` from three exchanges ago is not
    something the fork has to answer - the conversation has moved on since.
    """
    answered = set()
    asked = set()
    for o in _after_the_last_question(objs):
        message = o.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if _tool_result_id(block) is not None:
                answered.add(_tool_result_id(block))
            elif _tool_use_id(block) is not None:
                asked.add(_tool_use_id(block))
    return bool(asked - answered)


def _after_the_last_question(objs: list[dict]) -> list[dict]:
    """The lines that follow the last thing the user said - the whole file when there
    is none.

    Starting at the question and not at the last line that said anything is what a
    parallel tool call needs: claude writes one line per content block, so a turn
    that calls two tools is two lines, and the results come back in whichever order
    they finish. Starting at the last line would begin at the *second* tool_use and
    never see the first one, so a turn whose first call never came back would look
    finished.

    "Said" is the reading version's own word for it - text, not a `[skip …]` line,
    not noise, and not a user line that carries nothing but tool results (that is the
    results coming home, not the user asking again).
    """
    def said(o: dict) -> bool:
        lines = _user_lines(o) if o.get("type") == "user" else _assistant_lines(o)
        return bool(lines) and any(not line.startswith("[skip ") for line in lines)

    for index in reversed(range(len(objs))):
        if objs[index].get("type") == "user" and said(objs[index]):
            return objs[index + 1:]
    return objs


#: md5 of the empty prefix: what a cursor that has sealed nothing yet records.
EMPTY_MD5 = hashlib.md5(b"").hexdigest()

_CURSOR_INTS = ("source_size", "sealed_line_count", "raw_stable", "main_line_count")
_CURSOR_MD5S = ("sealed_md5", "base_md5", "main_md5")


def _cursor_ok(cursor: object) -> bool:
    """A cursor this version wrote and can use; anything else is "no cursor" (fail-safe)."""
    if not isinstance(cursor, dict) or cursor.get("version") != 1:
        return False
    if not isinstance(cursor.get("agent_session_id"), str):
        return False
    for key in _CURSOR_INTS:
        value = cursor.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    return all(isinstance(cursor.get(k), str) and re.fullmatch(r"[0-9a-f]{32}", cursor[k])
               for k in _CURSOR_MD5S)


class ClaudeAgent:
    name = "claude"

    def export(self, session_id: str, hint_dir: str | Path | None = None) -> Exported:
        path = find_jsonl(session_id, hint_dir)
        exported, _streamed = _exported(session_id, path, path.parent / session_id,
                                        label=path.name)
        return exported

    def export_increment(self, session_id: str, hint_dir: str | Path | None = None,
                         cursor: dict | None = None) -> Increment:
        """`export`, plus where the new bytes start (design B.2).

        The same single pass; it also snapshots the md5 at the cursor's sealed line, so the
        sealed prefix can be checked against what was sealed last time. The prefix is the
        same bytes at the same offset -> `append_from = cursor.raw_stable`; anything else
        (no cursor, another agent session, a shorter or rewritten source) -> None, the whole
        raw is cut again. `seal_upto` is where the main content ends.
        """
        path = find_jsonl(session_id, hint_dir)
        good = _cursor_ok(cursor) and cursor["agent_session_id"] == session_id
        sealed = cursor["sealed_line_count"] if good else None
        exported, packed = _exported(session_id, path, path.parent / session_id,
                                     label=path.name,
                                     capture=(sealed,) if sealed is not None else ())
        append_from = None
        if (good and packed.main_count >= sealed
                and packed.stable_at[sealed] == cursor["raw_stable"]
                and packed.stable_md5(sealed) == cursor["sealed_md5"]):
            append_from = cursor["raw_stable"]
        try:
            source_size = path.stat().st_size
        except OSError:
            source_size = 0
        out = {"version": 1, "agent_session_id": session_id, "path": str(path),
               "source_size": source_size,
               # what is sealed: the old values when this is an append, nothing otherwise
               "sealed_line_count": cursor["sealed_line_count"] if append_from is not None else 0,
               "sealed_md5": cursor["sealed_md5"] if append_from is not None else EMPTY_MD5,
               "raw_stable": append_from if append_from is not None else 0,
               "base_md5": exported.raw_md5,
               "main_line_count": packed.main_count, "main_md5": packed.main_md5}
        return Increment(exported, append_from, packed.main_end, out)

    def collect_increment(self, launch: Launch, cursor: dict | None) -> "Increment | None":
        if not launch.agent_session_id:
            raise AgentError("沒有 agent session id，無法收尾")
        session_id = launch.agent_session_id
        _warn_cleared(find_jsonl(session_id, launch.cwd), session_id)
        inc = self.export_increment(session_id, launch.cwd, cursor)
        if inc.exported.message_count <= launch.before_count:
            return None
        return inc

    def turns(self, raw: bytes) -> Turns:
        main, _aux = _unpack_raw(raw)
        return _turns(_parse_all(main))

    def native(self, turns: Turns) -> bytes:
        """Turns (from any agent, or a merge of several) → a Claude jsonl raw
        that start_native can load directly (design v5 5.4)."""
        return _pack_raw(_native_lines(turns), {})

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        """A's own jsonl with `turns` chained to the end of it (F4).

        A same-agent fork loads A's file as it is - every tool call, every prefix -
        so the two turns that say what this session is fork of cannot go through
        `native()`: that would rebuild the conversation and drop what a rebuild
        cannot express. So A's lines are kept exactly as they are (they are already
        strings, not objects - nothing is re-encoded, not even the whitespace) and
        new ones are written after the last one that belongs to the main thread.
        `sessionId` and `cwd` on the new lines are the same placeholders `native()`
        writes, so `start_native` rewrites them by the same rule (review CL7) when
        it gives the fork its own session id and directory.
        """
        main, aux = _unpack_raw(raw)
        extra = list(turns)
        if _asked_without_an_answer(_parse_all(main)):
            extra.insert(0, ("assistant", [NO_REPLY]))
        return _pack_raw(main + _native_lines(extra, parent=_leaf_uuid(main)), aux)

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None,
                     harness_file: Path | None = None) -> Launch:
        main, aux = _unpack_raw(raw)
        new_id = str(uuid.uuid4())
        workdir_str = str(workdir)
        subdir = projects_dir() / encode_project_dir(workdir)
        subdir.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_lines(main, new_id, workdir_str)
        (subdir / f"{new_id}.jsonl").write_text("\n".join(rewritten) + "\n", encoding="utf-8")
        sidecar = subdir / new_id
        if aux:
            def rewrite(text: str, rel: str) -> str:   # same S10 rule as main (review CL6)
                joined = "\n".join(_rewrite_lines(_split_lines(text, rel), new_id, workdir_str))
                return joined + "\n" if joined and text.endswith("\n") else joined
            _unpack_aux(aux, sidecar, rewrite=rewrite)
        if headless:
            # The programmatic mode the spike pinned (design D6): stream-json in
            # and out, verbose is what makes the stream usable, and the fixed
            # setting keeps claude from auto-continuing past a usage limit by
            # itself (sidecar decides the swap).
            argv = [agent_cmd("claude"), "-p",
                    "--input-format", "stream-json", "--output-format", "stream-json",
                    "--verbose", "--resume", new_id,
                    "--settings", '{"autoContinueAtUsageLimit": false}']
        else:
            argv = [agent_cmd("claude"), "--resume", new_id]
        if harness_file is not None:
            argv.extend(["--append-system-prompt-file", str(harness_file.resolve()),
                         "--system-prompt-snapshot", "off"])
        return Launch(argv=argv, cwd=workdir_str, agent_session_id=new_id,
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
            found.append(Listed(session_id=path.stem, dir=directory, title=title,
                                updated_at=iso_utc(stat.st_mtime)))
        for stale in [k for k in _LIST_CACHE if k not in seen]:
            del _LIST_CACHE[stale]          # the cache is only as big as the last walk
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
            said = _said(line, ("user", "assistant"))
            if said is None or not said[2]:
                continue
            return said[0], said[2][-PREVIEW_MAX:]
        return None

    def search_text(self, keyword: str, only: set[str] | None = None) -> Iterator[str]:
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
            if path.is_symlink() or not path.is_file() or (only is not None and path.stem not in only):
                continue
            try:
                with path.open(encoding="utf-8", errors="replace") as f:
                    for line in f:
                        said = _said(line, ("user", "assistant"))
                        # one odd line must not end the search
                        if said is not None and said[2] and needle in normalize(said[2]):
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
                                  capture_output=True, text=True, timeout=summarize_timeout())
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
        _warn_cleared(find_jsonl(session_id, launch.cwd), session_id)
        exported = self.export(session_id, launch.cwd)
        if exported.message_count <= launch.before_count:
            return None
        return exported


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
                warn(f"這次接續中用過 /clear，之後的對話在 Claude session {other.stem}；"
                     f"要存進 Agora 請另外執行：agora import session "
                     f"--external-session-id {other.stem} --agent claude")
        except OSError:
            continue


ADAPTER = ClaudeAgent()

"""The agy (Antigravity CLI) adapter - read only, first version (#26 PR1).

A conversation is one SQLite database at
`$AGORA_AGY_HOME/conversations/<conversation-uuid>.db` (WAL mode, page size
32768), and the list/titles live in `conversation_summaries.db` next to that
directory. `$AGORA_AGY_HOME` defaults to `~/.gemini/antigravity-cli/` and is
read in *every* place this module touches the agent's files, so a test can
point it at a temp directory and the user's real home is never opened.

What PR1 supports - import and reading only:

    export()        snapshot -> JSON envelope with the whole db base64'd
    turns()         user (step_type 14) / model text and tool calls (15) from
                    the protobuf blobs; tool steps (132) are known and silent
    list_sessions() title/preview/step_count from conversation_summaries.db
    last_message()  one snapshot of that conversation, role from step_type
    search_text()   title + preview only (agy's own table has no body text)

`native` / `append_native` / `summarize` / `start_native` / `collect` are
refused with `AGY_READ_ONLY`; `cli.cmd_continue` and `cli.cmd_merge` refuse
`--agent agy` before doing any work, so a refusal is never mistaken for a bad
model answer. They also refuse a *source* session that came from agy: the
first version supports neither direction of a cross-agent move. Continuing is
PR2, fork/merge are later (#26).

Snapshot (docs/agy-storage.md §1.2, §8): an idle agy database has no `-wal`,
and copying just the `.db` then opening it with `immutable=1` is a consistent
read. While a turn is running the `-wal` holds the new rows and the main file
does not, so both are copied - never `-shm`, SQLite rebuilds it - and the copy
is opened normally in a temp dir, which replays the wal, then `VACUUM INTO`
collapses it to one file. The copy is checked before/after with
`(size, mtime_ns)` and retried when a checkpoint moved the files under it;
a file that vanished mid-copy is the same race, not an error. Nothing here
ever opens a file inside `AGORA_AGY_HOME` writable, and every temp directory
is a `TemporaryDirectory` cleaned on the way out (also on the failing path).

The raw envelope::

    {"format": "agy-conversation/1", "agy_version": ..., "trajectory_id": ...,
     "scan_text": [<plain strings from the conversation steps>], "db_b64": <base64 db>}

`db_b64` is base64 so `store.copy_atomic(utf8=True)` can take it (the mirror
only carries UTF-8). `scan_text` is a separate extraction, not the reading
version: every UTF-8 string of at least 4 bytes found in any length-delimited
field of a step's `step_payload` / `render_info` / `metadata`, untruncated and
deduplicated. That is what a credential scan needs - tool output and tool
arguments are in there in full, while the reading version keeps tool results
out (design 4.4). It does not cover `gen_metadata` / `executor_metadata` /
`trajectory_metadata_blob`: those are settings and system prompts, and they
stay only inside the base64 db (a documented gap, not a silent one).

The db keeps every row - gen_metadata and all - because PR2 restores it and
PR0's R1 asks for no rewriting: the idle path exports the source file byte for
byte, and the live path exports the (row-preserving) `VACUUM INTO` collapse of
the replayed snapshot.

Since PR2a the file is split (chunked verbatim, behaviour unchanged): the
directory/database access layer and the summaries reading live in
`agy_store.py`, the protobuf wire format and the step decoding in
`agy_wire.py`; this module keeps the envelope and `AgyAgent`/`ADAPTER`, and
re-exports the names the commands import (`FORMAT`, `AGY_READ_ONLY`,
`SEARCH_SCOPE`) so the import paths stay the same.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from agora.store import normalize

from .agy_store import (
    _immutable, _rows, _summary_for, _summary_rows, _trajectory_id, snapshot)
from .agy_wire import (
    STEP_MODEL, STEP_USER, _one, _one_line, _read_turns, _scan_text, _texts)
from .base import AgentError, Exported, Launch, Listed, Turns

FORMAT = "agy-conversation/1"

PREVIEW_MAX = 2000         # last_message text cap, the same as claude's
B64_CHUNK = 3 * 32768      # base64 input chunk: a multiple of 3, so every
                           # chunk ends on a 4-character boundary

#: One message for every place that refuses agy: the command boundary
#: (`cli.cmd_merge` / `cli.cmd_continue`) and the adapter's own stubs.
AGY_READ_ONLY = ("agy 第一版只支援 import／讀取；接續在 PR2、fork／merge 為後續（#26）")

#: What `search_text` can see. Written into `agora --help` and the TUI's
#: content-search note, because "no result" must not look like "not there".
SEARCH_SCOPE = "agy 只搜標題與 preview（不搜內文）"

_TITLE_MAX = 60


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _pack_raw_file(path: Path, db: Path, trajectory_id: str | None,
                   scan_text: list[str]) -> tuple[str, int]:
    """Stream the envelope to `path`; return (md5, size) without reading it back.

    base64 is written in 3-byte multiples and the db is read chunk by chunk, so
    the whole raw is never in memory (the same shape claude's `_pack_raw_file`
    has). The byte order is fixed, which is what makes two exports of the same
    unchanged snapshot produce the same md5.
    """
    digest = hashlib.md5()
    size = 0
    with path.open("wb") as out:
        def write(piece: bytes) -> None:
            nonlocal size
            out.write(piece)
            digest.update(piece)
            size += len(piece)

        write(b'{"format": "agy-conversation/1", "agy_version": null, "trajectory_id": ')
        write(json.dumps(trajectory_id, ensure_ascii=False).encode("utf-8"))
        write(b', "scan_text": ')
        write(json.dumps(scan_text, ensure_ascii=False).encode("utf-8"))
        write(b', "db_b64": "')
        with open(db, "rb") as source:
            while chunk := source.read(B64_CHUNK):
                write(base64.b64encode(chunk))
        write(b'"}')
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


def _envelope(raw: bytes) -> dict:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise AgentError(f"agy raw 不是 JSON 信封：{e}")
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise AgentError("agy raw 的信封格式認不出來（要 agy-conversation/1）")
    if not isinstance(document.get("db_b64"), str):
        raise AgentError("agy raw 沒有 db_b64")
    return document


def _decode_db(document: dict, path: Path) -> None:
    """base64 → a temp db file, chunk by chunk (a chunk is a multiple of 4)."""
    encoded = document["db_b64"]
    with path.open("wb") as out:
        for start in range(0, len(encoded), B64_CHUNK):
            try:
                out.write(base64.b64decode(encoded[start:start + B64_CHUNK]))
            except ValueError as e:
                raise AgentError(f"agy raw 的 db_b64 壞了：{e}")


class AgyAgent:
    """Reads agy's own files; never runs the CLI and never writes its home."""

    name = "agy"

    # -- the Agent protocol ------------------------------------------------

    def export(self, session_id: str) -> Exported:
        with snapshot(session_id) as db:
            turns, message_count = _read_turns(_rows(db))
            scan_text = _scan_text(db)
            trajectory_id = _trajectory_id(db)
            summary = _summary_for(session_id)
            tmpdir = tempfile.TemporaryDirectory(prefix="agora-agy-raw-")
            raw_path = Path(tmpdir.name) / "raw.json"
            md5, size = _pack_raw_file(raw_path, db, trajectory_id, scan_text)
        return Exported(
            session_id=session_id,
            raw_file=raw_path,
            raw_md5=md5,
            raw_size=size,
            _raw_tmpdir=tmpdir,
            dir=summary.workspace if summary else None,
            title=summary.title if summary else None,
            created_at=None,          # summaries has no created time (unknown, §10)
            agent_version=None,       # no version field was found in the db (unknown)
            message_count=message_count,
            model=None,               # no model name was found in the steps (unknown)
            turns=turns,
        )

    def turns(self, raw: bytes) -> Turns:
        """The envelope's db, decoded to a temp file and read step by step."""
        document = _envelope(raw)
        with tempfile.TemporaryDirectory(prefix="agora-agy-raw-") as tmp:
            db = Path(tmp) / "raw.db"
            _decode_db(document, db)
            return _read_turns(_rows(db))[0]

    def list_sessions(self) -> list[Listed]:
        """Every conversation the summaries table knows, newest first.

        Read-only and cached (see `_summary_rows`); an unreadable database
        raises `AgentError` - the TUI prints that per agent and carries on,
        while an empty list would quietly claim there are no conversations.
        """
        found = []
        for row in _summary_rows():
            title = (row.title or "").strip() or None
            if title is None and row.preview:
                title = _one_line(row.preview)[:_TITLE_MAX] or None
            found.append(Listed(session_id=row.session_id, dir=row.workspace,
                                title=title, updated_at=row.updated_at))
        found.sort(key=lambda item: item.updated_at or "", reverse=True)
        return found

    def last_message(self, session_id: str) -> tuple[str, str] | None:
        """(role, text) of the last text step; role comes from step_type.

        The summaries table has no role column, so this takes one snapshot when
        the row is picked and reads the last 14/15 step. A tool-call model step
        (no 20.1) is not something the user or the model said as prose, so it
        is passed over; an unreadable session gives None, never a raise.
        """
        try:
            with snapshot(session_id) as db:
                with closing(_immutable(db)) as connection:
                    for step_type, payload in connection.execute(
                            "SELECT step_type, step_payload FROM steps ORDER BY idx DESC"):
                        try:
                            if step_type == STEP_USER:
                                text = "\n".join(_texts(_one(payload or b"", 19), 2))
                                role = "user"
                            elif step_type == STEP_MODEL:
                                text = "\n".join(_texts(_one(payload or b"", 20), 1))
                                role = "assistant"
                            else:
                                continue
                        except (ValueError, IndexError, UnicodeDecodeError):
                            continue
                        if text.strip():
                            return role, text[-PREVIEW_MAX:]
        except (AgentError, sqlite3.Error, OSError):
            return None
        return None

    def search_text(self, keyword: str, only: "set[str] | None" = None) -> Iterator[str]:
        """Ids whose title or preview contains `keyword` - agy's body is not searchable."""
        needle = normalize(keyword)
        if not needle:
            return
        for row in _summary_rows():
            if only is not None and row.session_id not in only:
                continue
            haystack = normalize("\n".join(part for part in (row.title, row.preview) if part))
            if needle in haystack:
                yield row.session_id

    # -- refused until PR2 / later (#26) -----------------------------------

    def native(self, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        raise AgentError(AGY_READ_ONLY)

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None,
                     harness_file: Path | None = None) -> Launch:
        raise AgentError(AGY_READ_ONLY)

    def collect(self, launch: Launch) -> Exported | None:
        raise AgentError(AGY_READ_ONLY)


ADAPTER = AgyAgent()

"""The agy (Antigravity CLI) adapter - import／讀取與同 agent 的互動接續（#26）.

A conversation is one SQLite database at
`$AGORA_AGY_HOME/conversations/<conversation-uuid>.db` (WAL mode, page size
32768), and the list/titles live in `conversation_summaries.db` next to that
directory. `$AGORA_AGY_HOME` defaults to `~/.gemini/antigravity-cli/` and is
read in *every* place this module touches the agent's files, so a test can
point it at a temp directory and the user's real home is never opened.

What PR2b supports - import, reading, and the same-agent interactive
continue:

    export()        snapshot -> JSON envelope with the whole db base64'd
    turns()         user (step_type 14) / model text and tool calls (15) from
                    the protobuf blobs; tool steps (132) are known and silent
    list_sessions() title/preview/step_count from conversation_summaries.db
    last_message()  one snapshot of that conversation, role from step_type
    search_text()   title + preview only (agy's own table has no body text)
    start_native()  `agy_restore.prepare_continue` (restore + the summaries row
                    agy loads conversations by) then `agy --conversation`.
    collect()       one export; None while nothing newer than `before_count`.

`native` / `append_native` / `summarize` stay refused with `AGY_READ_ONLY`,
and the command boundary (`cli.cmd_continue`) still refuses fork / headless /
either cross-agent direction (`AGY_HEADLESS_LATER`／`AGY_CROSS_AGENT` also
live here); merge keeps refusing agy in `cli.cmd_merge`. Continuing is PR2b,
fork/merge are later (#26). No agy flag is ever invented for the launch: the
spike showed plain `agy --conversation <id>` opens with the restored content.

Snapshot (docs/agy-storage.md §1.2, §8): an idle agy database has no `-wal`,
and copying just the `.db` then opening it with `immutable=1` is a consistent
read. While a turn is running the `-wal` holds the new rows and the main file
does not, so both are copied - never `-shm`, SQLite rebuilds it - and the copy
is opened normally in a temp dir, which replays the wal, then `VACUUM INTO`
collapses it to one file. The copy is checked before/after with
`(size, mtime_ns)` and retried when a checkpoint moved the files under it;
a file that vanished mid-copy is the same race, not an error. Nothing here
ever opens a file inside `AGORA_AGY_HOME` writable, and every temp directory
is a `PrivateTmpDir` (under agora's own state, 0700) cleaned on the way out
(also on the failing path); a SIGKILL's leftover is swept by the next round,
continue or export.
The continue half writes the conversation and the summaries row under
`$AGORA_AGY_HOME` (restore temp file inside `conversations/`, `os.replace`,
one summaries insert) and, before replacing a live db, one backup under
agora's own state (`agy-backups/`, private, atomic; the user's original agy
version kept, the newest three later ones kept - review W3); `agy_restore`'s
docstring spells that out, and every test runs it on a fake home.
`restores_in_place = True` is the explicit protocol that makes the cli hand
this adapter the Session's recorded source id (review W2).

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

Since PR2a the file is split (chunked verbatim, behaviour unchanged), and the
split widened in PR2b: the directory/database access layer and the raw envelope
live in `agy_store.py`, the summaries list and the row a continue inserts in
`agy_summaries.py`, the continue half (`restore` / `prepare_continue`) in
`agy_restore.py`, and the protobuf wire format and step decoding in
`agy_wire.py`; this module keeps `AgyAgent`/`ADAPTER` and the boundary messages
(`AGY_READ_ONLY`, `AGY_HEADLESS_LATER`, `AGY_CROSS_AGENT`, `SEARCH_SCOPE`),
re-exporting the names the commands and tests import (`FORMAT`,
`PREVIEW_MAX`) so the import paths stay the same. The dependency direction is
one-way: store → summaries → restore (review W5).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from agora.private_tmp import PrivateTmpDir
from agora.store import normalize

from .agy_restore import prepare_continue
from .agy_store import (FORMAT, PREVIEW_MAX, db_path, decode_db, envelope, immutable,
                        pack_raw_file, rows, scan_text, snapshot, step_fingerprint,
                        trajectory_id)
from .agy_summaries import summary_for, summary_rows
from .agy_wire import STEP_MODEL, STEP_USER, one, one_line, read_turns, texts
from .base import AgentError, Exported, Launch, Listed, Turns, agent_cmd, warn

#: One message for every place that stays read-only: the adapter's own stubs,
#: `cli.cmd_merge`'s early refusal and the fork part of `cmd_continue`'s
#: boundary.
AGY_READ_ONLY = ("agy 第一版只支援 import／讀取與同 agent 的互動接續；"
                 "fork／headless／跨 agent／merge 為後續（#26）")

#: `start_native`'s headless refusal, and `cmd_continue`'s same refusal: agy's
#: interactive launch is what PR2b supports, a headless child is later (#26).
AGY_HEADLESS_LATER = "agy 的 headless 在後續（#26：先支援同 agent 的互動接續）"

#: `cmd_continue`'s refusal for both cross directions (a claude/opencode
#: source continued by agy, an agy source continued by claude/opencode);
#: same-agent only (#26). merge keeps its own `AGY_READ_ONLY`.
AGY_CROSS_AGENT = "跨 agent 的 continue 在後續（#26）：接續只支援同 agent"

#: What `search_text` can see. Written into `agora --help` and the TUI's
#: content-search note, because "no result" must not look like "not there".
SEARCH_SCOPE = "agy 只搜標題與 preview（不搜內文）"

_TITLE_MAX = 60


def _cross_check(path: Path, session_id: str) -> None:
    """Compare the snapshot's step count with conversation_summaries.db's.

    A mismatch is only ever a warning: the summaries table is written by agy
    itself and may lag a checkpoint, and PR1 must still read the conversation.
    A summaries file we cannot read is not evidence about this snapshot either,
    so it is skipped rather than allowed to fail the read. It lives here, the
    caller of `snapshot`, so `agy_store` never imports the layer above it
    (review N3).
    """
    count, _ = step_fingerprint(path)
    try:
        listed = summary_rows()
    except Exception:                       # broken/unreadable list: no cross-check
        return
    for row in listed:
        if row.session_id == session_id and row.step_count is not None and row.step_count != count:
            warn(f"agy：{session_id} 的快照有 {count} 步，summaries 說 {row.step_count} 步；"
                 "以快照為準")
            return


# ---------------------------------------------------------------------------
# AgyAgent
# ---------------------------------------------------------------------------


class AgyAgent:
    """Reads agy's own files; the continue half restores through `agy_store`."""

    name = "agy"

    #: The one adapter whose `start_native` restores into the agent's *own*
    #: store (`agy_restore`), so the cli hands it the Session's recorded source
    #: id (`expected_id`) and the restore can refuse a raw naming another
    #: conversation. An explicit attribute, not the presence of a parameter in
    #: `inspect.signature` (review W2): every other adapter lacks it and gets
    #: exactly the arguments it always got.
    restores_in_place = True

    # -- the Agent protocol ------------------------------------------------

    def export(self, session_id: str) -> Exported:
        """The session as `agy-conversation/1`, packed by `pack_raw_file`.

        The raw is written straight from the db snapshot (streamed; R1 changed
        nothing here) and the turns are carried on the export, so the reading
        version never re-reads the raw. `scan_text` is still O(text) - design K5.
        """
        with snapshot(session_id) as db:
            _cross_check(db, session_id)   # N3: the check lives at this caller, not in store
            turns, message_count = read_turns(rows(db))
            strings = scan_text(db)
            inner_id = trajectory_id(db)
            summary = summary_for(session_id)
            tmpdir = PrivateTmpDir("agora-agy-raw")
            raw_path = Path(tmpdir.name) / "raw.json"
            md5, size = pack_raw_file(raw_path, db, inner_id, strings)
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
        document = envelope(raw)
        with PrivateTmpDir("agora-agy-raw") as tmp:
            db = Path(tmp) / "raw.db"
            decode_db(document, db)
            return read_turns(rows(db))[0]

    def list_sessions(self) -> list[Listed]:
        """Every conversation the summaries table knows, newest first.

        Read-only and cached (see `summary_rows`); an unreadable database
        raises `AgentError` - the TUI prints that per agent and carries on,
        while an empty list would quietly claim there are no conversations.
        """
        found = []
        for row in summary_rows():
            title = (row.title or "").strip() or None
            if title is None and row.preview:
                title = one_line(row.preview)[:_TITLE_MAX] or None
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
                with closing(immutable(db)) as connection:
                    for step_type, payload in connection.execute(
                            "SELECT step_type, step_payload FROM steps ORDER BY idx DESC"):
                        try:
                            if step_type == STEP_USER:
                                text = "\n".join(texts(one(payload or b"", 19), 2))
                                role = "user"
                            elif step_type == STEP_MODEL:
                                text = "\n".join(texts(one(payload or b"", 20), 1))
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
        for row in summary_rows():
            if only is not None and row.session_id not in only:
                continue
            haystack = normalize("\n".join(part for part in (row.title, row.preview) if part))
            if needle in haystack:
                yield row.session_id

    # -- still refused (fork/merge/headless are later, #26) ------------------

    def native(self, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        raise AgentError(AGY_READ_ONLY)

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        raise AgentError(AGY_READ_ONLY)

    # -- the same-agent interactive continue (PR2b) --------------------------

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None,
                     harness_file: Path | None = None,
                     expected_id: str | None = None) -> Launch:
        """Restore the conversation, then hand back the `agy --conversation` launch.

        `agy_restore.prepare_continue` does the whole before-half: restore the
        conversation file under `$AGORA_AGY_HOME/conversations/` (content
        adjudicated, never by mtime) and make sure
        `conversation_summaries.db` carries the row agy loads the
        conversation by - without it agy answers
        `conversation "<id>" not found` and opens an empty one (the PR2b
        spike). The reply names no agy flag (「不要加任何 agy 旗標）: the id
        is the restored file's own `trajectory_meta.cascade_id`, the same id
        the summaries row was written for. `before_count` is the 14/15 turn
        count the conversation already carries, so a round's collect can
        tell real speech from what was already there. headless is refused
        before anything runs (`AGY_HEADLESS_LATER`), and a `--harness-file`
        is not a thing agy takes either.

        `expected_id` is the Session's own record of which conversation this
        raw came from (`agora.source.session_id`; `cli._load_for_launch` passes
        it because this adapter declares `restores_in_place`). Restore refuses
        a Session that records no source id, and a raw whose cascade names a
        different conversation, before writing anything (review W2／W3 - the
        guard is against accidents and conflicts, not a malicious writer).
        """
        if headless:
            raise AgentError(AGY_HEADLESS_LATER)
        if harness_file is not None:
            raise AgentError("agy 不支援 --harness-file（#26：先支援同 agent 的互動接續）")
        conversation_id, before = prepare_continue(raw, workdir=Path(workdir),
                                                   expected_id=expected_id)
        # Restore's `used_live` already warned on stderr when the disk copy was
        # the newer one; the launch is the same either way: the id is the file
        # agy will open.
        return Launch(argv=[agent_cmd("agy"), "--conversation", conversation_id],
                      cwd=str(workdir), agent_session_id=conversation_id,
                      before_count=before)

    def collect(self, launch: Launch) -> Exported | None:
        """One read-back; None while the agent has not said anything new.

        The conversation db under `$AGORA_AGY_HOME` is what gets read
        (`launch.agent_session_id`), one snapshot per collect. A db that is
        gone entirely is an out-loud error, not an empty reading: agy may
        have renamed the conversation (review #26 finding 7), and quietly
        answering `None` would end the round as if nothing was ever said.
        """
        if not launch.agent_session_id:
            raise AgentError("沒有 agent session id，無法收尾")
        session_id = launch.agent_session_id
        if not db_path(session_id).is_file():
            raise AgentError(f"agy 對話 db 不見了：{session_id}；"
                             "agy 可能換了對話 id（#26 review finding 7）")
        exported = self.export(session_id)
        if exported.message_count <= launch.before_count:
            return None
        return exported


ADAPTER = AgyAgent()

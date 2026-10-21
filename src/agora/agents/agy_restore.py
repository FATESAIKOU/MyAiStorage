"""The agy continue's restore half: put the conversation file agy opens in place.

`restore` decodes a raw envelope into the conversation db under
`$AGORA_AGY_HOME/conversations/<cascade_id>.db` (the only agy file it writes,
see `agy_store`), adjudicates it against the live copy by `(count, max idx)`
and the summaries stamp, and `prepare_continue` adds the one summaries row agy
loads a conversation by. A live copy that is about to be overwritten is first
copied into agora's own state (`store.Paths.from_env().state/agy-backups/`),
never into agy's home, and the overwrite says so on stderr (review N1).

Dependency direction (review W5): `agy_store` (paths, snapshot, envelope) →
`agy_summaries` (the list and the row) → this file. Nothing here is imported by
the two lower layers.

What stays unverified (docs/agy-storage.md §12): whether an idle agy leaves an
empty `-wal`／`-shm` behind (so any sidecar still refuses), the summaries
stamp's real unit, and the TOCTOU window between that check and the replace.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agora import store
from agora.agents.agy_store import (PREVIEW_MAX, cascade_id, conversations_dir, db_path,
                                    decode_db, envelope, home, immutable, quick_check, rows,
                                    step_fingerprint)
from agora.agents.agy_summaries import (ensure_summary_row, epoch_seconds, summary_rows,
                                        summaries_path, summary_table)
from agora.agents.agy_wire import fields, one, read_turns
from agora.agents.base import AgentError, warn

#: A restore's temp file inside `conversations/`, removed even on failure.
RESTORE_TMP_PREFIX = ".restore-"

#: How old a leftover `.restore-*.tmp` must be before a later restore deletes it
#: (a SIGKILL mid-restore leaves a whole conversation behind; an hour is long
#: past any write a live process is still doing).
STALE_RESTORE_SECONDS = 3600

#: How many timestamped backups one conversation keeps, not counting the first
#: backup (`<id>.db`, the user's original agy version, which is never deleted;
#: review W3).
BACKUPS_KEEP = 3

TITLE_MAX = 60             # the summaries row's title: the first user text's head

#: A Session whose header lost `agora.source.session_id` cannot say which
#: conversation its raw came from, and agy's restore is the one path that
#: writes the user's real directory: "unknown" must not mean "any
#: conversation" (review W2, the fail-open side of W3).
NO_SOURCE_ID = "agy：這個 Session 沒有記來源 id，不還原（不猜是哪個對話；#26 W2）"


@dataclass(frozen=True)
class RestoreResult:
    """What one restore decided, for the summaries row and the launch.

    `used_live` says the conversation on disk already carried equal-or-newer
    work and was left alone; agy opens it as it is. `last_modified` is the
    latest step's start stamp as epoch seconds - the value the summaries row
    carries, in whatever shape the table's own rows use (None when every step
    stamp was unreadable; `prepare_continue` falls back to `store.now()`).
    """

    conversation_id: str          # the cascade id, the file name agy opens it by
    used_live: bool
    step_count: int               # count(*) of the conversation agy will open
    messages: int                 # 14/15 turns with content - the number for before_count
    title: str | None             # the first user text, at most TITLE_MAX chars
    preview: str | None           # the last real text, truncated like last_message
    wrote: bool                   # this call actually replaced the file
    last_modified: float | None = None   # epoch seconds; None when stamps unread


def _step_seconds(path: Path) -> float | None:
    """The latest step's start stamp (steps.5.1 seconds + 5.2 nanos), epoch seconds.

    It is only ever a comparison input for restore's "is the live copy newer"
    verdict - the summaries column's real unit is not verified (docs/agy-
    storage.md), so nothing here claims to know what agy writes into that list.
    """
    best = None
    try:
        with closing(immutable(path)) as connection:
            for (payload,) in connection.execute("SELECT step_payload FROM steps"):
                try:
                    ctx = one(payload, 5)
                    stamp = one(ctx, 1) if ctx else None
                    if not stamp:
                        continue
                    seconds = None
                    nanos = 0
                    for number, wire, value in fields(stamp):
                        if number == 1 and wire == 0:
                            seconds = int(value)
                        elif number == 2 and wire == 0:
                            nanos = int(value)
                    if seconds is None:
                        continue
                    this = seconds + nanos / 1e9
                    best = this if best is None else max(best, this)
                except ValueError:
                    continue                  # one unreadable step: the rest still tell
    except (sqlite3.Error, ValueError):
        return None
    return best


def _display_facts(turns: list[tuple[str, list[str]]]) -> tuple[str | None, str | None]:
    """(title, preview) for a summaries row, from the reading turns.

    The title is the first user text's first TITLE_MAX characters; the preview
    is the last real text, capped like `last_message`. `[skip …]` markers are
    bookkeeping, not conversation, so neither points at one.
    """
    first_user = last_text = None
    for role, lines in turns:
        text = "\n".join(line for line in lines
                         if line.strip() and not line.startswith("[skip "))
        if not text:
            continue
        if role == "user" and first_user is None:
            first_user = text
        last_text = text
    return (first_user[:TITLE_MAX] if first_user else None,
            last_text[-PREVIEW_MAX:] if last_text else None)


def _conversation_facts(path: Path) -> tuple[int, int, str | None, str | None, float | None]:
    """(step_count, 14/15 message count, title, preview, last-step seconds)."""
    count = step_fingerprint(path)[0]
    turns, messages = read_turns(rows(path))
    title, preview = _display_facts(turns)
    return count, messages, title, preview, _step_seconds(path)


def _live_stamp(conversation_id: str) -> float | None:
    """conversation_summaries.db's own last_modified_time for this id, in seconds.

    Restore's verdict quotes the *summaries* stamp (the PMO rule「step_count 較大
    或 summaries 的 last_modified_time 較晚」), so the live side reads the list
    agy itself keeps; without a readable row for this id the step counts decide
    alone. Its real unit is unverified - `epoch_seconds` normalises what looks
    plausible, and an unusable one is None, not a guess. The tradeoff of taking
    a later summaries stamp at face value (N2b): if agy refreshes that value
    when it merely opens or renames a conversation, a live copy with *fewer*
    steps would win and the raw's newer steps would not be restored this time;
    the stderr line prints both counts so the user can see that.
    """
    try:
        for row in summary_rows():
            if row.session_id == conversation_id:
                return epoch_seconds(row.stamp)
    except Exception:                        # an unreadable list is no evidence
        return None
    return None


def _fsync_dir(path: Path) -> None:
    """Make a rename durable: the directory entry is a write of its own."""
    try:
        fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                  # a filesystem that will not sync a directory
    finally:
        os.close(fd)


def _backup_name(directory: Path, conversation_id: str) -> Path:
    """`<id>.db` for the first backup, `<id>.<UTC stamp>.db` for later ones.

    The first overwrite is the one with the user's real agy version at risk, so
    that copy keeps the plain name and is never replaced; later ones get their
    own name (review W3, E7b). The stamp is microseconds so two overwrites in
    the same second cannot share a file.
    """
    first = directory / f"{conversation_id}.db"
    if not first.exists():
        return first
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = directory / f"{conversation_id}.{stamp}.db"
        if not candidate.exists():
            return candidate


_STAMP = re.compile(r"\d{8}T\d{12}Z")        # the UTC microsecond stamp `_backup_name` writes


def _prune_backups(directory: Path, conversation_id: str) -> None:
    """Delete all but the newest `BACKUPS_KEEP` timestamped backups of one id.

    Only `<id>.<stamp>.db` names are candidates: the plain `<id>.db` is the
    first, original backup and stays; a `.tmp` from a failed copy is not a
    backup either. Newest is by name, which the fixed-width UTC stamp orders.
    """
    prefix = f"{conversation_id}."
    stamped: list[Path] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for path in entries:
        name = path.name
        if not (name.startswith(prefix) and name.endswith(".db")):
            continue
        middle = name[len(prefix):-len(".db")]
        if _STAMP.fullmatch(middle):          # a whole stamp, not just digits: `abc.123.db` is another id's
            stamped.append(path)
    stamped.sort(key=lambda path: path.name)
    for old in stamped[:-BACKUPS_KEEP]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            continue


def _backup_live(target: Path, conversation_id: str) -> Path:
    """The live db's bytes before agora replaces it, in agora's own state.

    Not in agy's home: agy's directory only ever gets the conversation the
    launch needs, and a backup there would be a file agy could list. The copy
    is atomic and private (review W3): `<id>.db.tmp` in the same directory,
    fsync'd, then `os.replace`d - a copy that fails midway leaves the previous
    backup exactly as it was and never truncates it, and the backup directory
    is 0700 with each file 0600 rather than whatever the umask says.

    Retention (review W3, E7b): the first backup is the user's original agy
    version and keeps the plain `<id>.db` name forever; every later overwrite
    writes `<id>.<UTC stamp>.db`, and only the newest `BACKUPS_KEEP` of those
    survive. A copy that cannot be written raises before anything is replaced -
    no backup, no destructive step (review N1).
    """
    directory = store.Paths.from_env().state / "agy-backups"
    backup: Path | None = None
    tmp: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)              # explicit, not the umask's answer
        backup = _backup_name(directory, conversation_id)
        tmp = backup.with_name(backup.name + ".tmp")
        tmp.unlink(missing_ok=True)             # a SIGKILL's leftover is never appended to
        # 0600 from the first byte the copy writes, not after it.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        shutil.copyfile(target, tmp)
        os.chmod(tmp, 0o600)
        with tmp.open("rb") as written:
            os.fsync(written.fileno())
        os.replace(tmp, backup)                 # only now can the old backup be gone
        _fsync_dir(directory)
        tmp = None
        _prune_backups(directory, conversation_id)
    except OSError as e:
        raise AgentError(f"agy：覆蓋前備份失敗，不動活體：{e}") from e
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return backup


def _prune_stale_temps(directory: Path) -> None:
    """Delete `.restore-*.tmp` files a SIGKILL left behind more than an hour ago.

    The temp file has to sit in `conversations/` for `os.replace` to be atomic,
    so a crash leaves a whole conversation copy where agy can see it. A recent
    one may still belong to a restore that is writing; only the old ones go
    (review N9).
    """
    cutoff = store.now() - STALE_RESTORE_SECONDS
    try:
        candidates = list(directory.glob(f"{RESTORE_TMP_PREFIX}*.tmp"))
    except OSError:
        return
    for stale in candidates:
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink(missing_ok=True)
        except OSError:
            continue


def _precheck_summaries() -> None:
    """Read-only check that the summaries row can be added after the restore.

    `restore` replaces the conversation file for real; `ensure_summary_row`
    runs afterwards and raises when the list is missing or unrecognised.
    Checking the same two conditions first (no write) means a failed continue
    never leaves the directory half-changed (review N5). The connection is the
    same immutable single-file kind everything else here reads with: it never
    creates a `-shm` next to agy's file. `immutable=1` on a file agy is
    currently writing (the table only in an uncheckpointed `-wal`) can report
    「結構認不出來」 by mistake (review N1, E9): rare - only around the first
    table creation - and the message says to try again later.
    """
    path = summaries_path()
    if not path.is_file():
        raise AgentError("agy conversation_summaries.db 不存在，不補列（#26：不建表）")
    try:
        with closing(immutable(path)) as connection:
            found = summary_table(connection)
    except sqlite3.Error as e:
        raise AgentError(f"agy summaries 開不起來：{e}") from e
    if found is None:
        raise AgentError(
            "agy：conversation_summaries.db 的結構認不出來，不補列"
            "（#26 PR2b；不是「沒有 session」）；agy 正在寫 WAL 時可能誤報，稍後再試")


def restore(raw, *, workdir: Path, expected_id: str | None = None) -> RestoreResult:
    """Restore a raw envelope as the conversation db a continuation opens.

    只寫 agy 這裡的 `$AGORA_AGY_HOME/conversations/<cascade_id>.db`（模組
    docstring 的第一行；唯一的例外是覆蓋前的備份，寫在 agora 自己的 state，
    見下）：the envelope's bytes are decoded straight into a
    temp file there (`.restore-<random>.tmp`, fsync'd, `quick_check`ed **before**
    any replace, then `os.replace`), the file's name comes from the db's own
    `trajectory_meta.cascade_id` - the file name is the cascade id, not the
    envelope's inner `trajectory_id`, and envelopes that lack the id tolerate
    that for free. `expected_id` is the Session's own record of where this raw
    came from (`agora.source.session_id`, passed by `cli._load_for_launch` when
    the adapter says `restores_in_place`); a raw whose cascade names a
    different conversation is refused before anything is written (`AgentError`,
    review W3), and so is a Session that records no source id at all (review W2
    - unknown must not mean any conversation). The guard is against accidents
    and conflicts (two writers on one Drive, a raw pasted into the wrong
    conversation), **not against a malicious writer**: the header and the raw
    sit in the same Drive, so whoever can rewrite one can rewrite both (md5
    included).

    The content verdict is by the `(count, max idx)` fingerprints and the
    summaries stamp, never by mtime:

    - the target is missing, or its fingerprint equals the raw's → the bytes
      are written / left as they are (`wrote` says which). **The same
      fingerprint means the same step count and highest idx, not the same
      content** (review N2a): two diverged conversations with equal counts are
      a no-op and the live copy stays;
    - the target reads *newer* - more steps, or conversation_summaries.db puts
      its `last_modified_time` after the raw's latest step stamp (a tradeoff
      whose unit is unverified, see `_live_stamp`) - nothing is restored and
      `used_live` comes back: agy opens its own newer file directly (interface
      note §2「活體較新」, PMO/approved), and the stderr line prints both step
      counts (N2b);
    - only when the live copy is *older* do the raw's bytes overwrite it. That
      is the one destructive step: the live db is first copied to agora's own
      state (`agy-backups/`, 0700; the first backup as `<id>.db` and never
      replaced, later ones as `<id>.<UTC stamp>.db`, the newest three kept -
      review W3) and the overwrite says the backup's path on stderr; a failed
      backup means no overwrite (N1).

    Refused with `AgentError`: the temp file's `quick_check` failed (the live
    file is never replaced - review W1), a home that does not exist (a typo'd
    `AGORA_AGY_HOME` must not be fabricated), a Session with no recorded source
    id (W2), a live target that cannot be read (「讀不了…請手動移走再接續」,
    N4), a raw naming another conversation (W3), and a target that still has
    `-wal`/`-shm`. Whether an idle agy leaves an empty `-wal`／`-shm` is
    unverified, so any sidecar still refuses - but the message follows the
    `-wal`'s content (W1): an empty (or absent) one can be deleted while agy is
    not running; one with bytes holds committed turns not yet checkpointed, so
    it must not be deleted - let agy finish (or let sqlite3 open the db once to
    checkpoint) and retry. Both name the path and size (N5).

    TOCTOU: between the `-wal`/`-shm` check and `os.replace`, agy may start and
    open the same db, and the replace would then move the file under its open
    connection. It cannot be removed without a lock agy shares - only narrowed
    - and that limit is documented (docs/agy-storage.md §12).

    A failed decode, adjudication or replace leaves no temp file; a restore
    that starts also deletes `.restore-*.tmp` leftovers older than an hour
    (N9). `workdir` is the continue's project directory; only the fresh
    summaries row's `workspace_uris` wants it (see `ensure_summary_row`), and
    restore itself reads nothing from what it names.
    """
    if not home().is_dir():
        raise AgentError(f"agy 的 home 不存在，不還原：{home()}；"
                         "先確認 AGORA_AGY_HOME（真的 agy 會建自己的 home）")
    if expected_id is None:                  # W2: unknown is not "any conversation"
        raise AgentError(NO_SOURCE_ID)
    document = envelope(raw) if isinstance(raw, (bytes, bytearray)) else raw
    directory = conversations_dir()
    directory.mkdir(exist_ok=True)
    _prune_stale_temps(directory)
    tmp = directory / f"{RESTORE_TMP_PREFIX}{uuid.uuid4().hex}.tmp"
    try:
        decode_db(document, tmp)
        with tmp.open("r+b") as written:
            os.fsync(written.fileno())
        # W1: the temp file is what gets checked - if it is broken, the healthy
        # live db must not have been replaced yet (previously the check ran on
        # the target after the replace, with no way back).
        quick_check(tmp)
        cascade = cascade_id(tmp)
        if not cascade:
            raise AgentError("agy raw 的 db 裡沒有 cascade_id（trajectory_meta），"
                             "無法命名對話檔（#26 PR2b）")
        if cascade != expected_id:
            raise AgentError(
                f"agy raw 自報的對話是 {cascade}，不是這個 Session 的來源 {expected_id}；"
                "不還原（#26 W3：同步衝突或貼錯對話，擋意外不防惡意）")
        target = db_path(cascade)
        wal = target.with_name(target.name + "-wal")
        shm = target.with_name(target.name + "-shm")
        if wal.exists() or shm.exists():
            try:
                wal_size = wal.stat().st_size if wal.exists() else 0
            except OSError:
                wal_size = 0
            # W1: a `-wal` with bytes holds committed turns SQLite has not
            # checkpointed yet; deleting it loses them, while restore can only
            # see the main file. Only an empty (or missing) wal may be removed.
            if wal_size:
                raise AgentError(
                    f"agy 的 {target} 旁有 -wal／-shm（-wal {wal_size} 位元組：{wal}）；"
                    "不要手動刪：裡面是已 commit、還沒 checkpoint 的回合；先讓 agy 正常"
                    "結束一次（或用 sqlite3 開一次該 db 讓它自己 checkpoint），再重試")
            raise AgentError(
                f"agy 的 {target} 旁有 -wal／-shm（-wal {wal_size} 位元組：{wal}）；"
                "若 agy 沒在跑，可手動刪掉這兩個檔再重試；若在跑，等它結束")
        raw_fp = step_fingerprint(tmp)
        raw_seconds = _step_seconds(tmp)
        try:
            live_fp = step_fingerprint(target) if target.is_file() else None
        except AgentError as e:
            raise AgentError(f"agy 的 {target} 讀不了（{e}）；"
                             "若確定壞了請手動移走再接續") from e
        live_seconds = (None if live_fp is None else _live_stamp(cascade))
        wrote = used_live = False
        if live_fp is None:
            os.replace(tmp, target)
            _fsync_dir(target.parent)
            wrote = True
        elif live_fp == raw_fp:
            pass                      # same count and last idx - not the same bytes
        elif live_fp > raw_fp:
            used_live = True
        elif (live_seconds is not None and raw_seconds is not None
                and live_seconds > raw_seconds):
            used_live = True          # agy's own list says its copy is the later state
        elif live_fp < raw_fp:
            backup = _backup_live(target, cascade)
            warn(f"將以 agora 的版本（{raw_fp[0]} 步）覆蓋 agy 較舊的版本"
                 f"（{live_fp[0]} 步），備份在 {backup}")
            os.replace(tmp, target)
            _fsync_dir(target.parent)
            wrote = True              # a later export replaces the older live copy
        if used_live:
            warn(f"agy：{cascade} 這邊已有較新的對話（活體 {live_fp[0]} 步、"
                 f"agora 的 raw {raw_fp[0]} 步），不還原，直接以 agy 開活體"
                 "（#26 內容裁決）")
        count, messages, title, preview, seconds = _conversation_facts(target)
        return RestoreResult(conversation_id=cascade, used_live=used_live,
                             step_count=count, messages=messages,
                             title=title, preview=preview,
                             last_modified=(seconds if seconds is not None
                                            else raw_seconds),
                             wrote=wrote)
    finally:
        tmp.unlink(missing_ok=True)


def prepare_continue(raw: bytes, *, workdir: Path,
                     expected_id: str | None = None) -> tuple[str, int]:
    """Everything an agy continue needs before `agy --conversation <id>` runs.

    Check the summaries list can be written (read-only, N5), restore/adjudicate
    the conversation file, then add the one row the PR2b spike showed agy
    additionally loads conversations *by*: a `conversation_summaries.db` row
    for the id (`ensure_summary_row`), whose stamps stay in `_stamp_like`'s
    unverified shape.

    It answers `(conversation_id, message_count)`. The message count is the
    14/15 turns the conversation agy opens carries - the number that becomes
    `Launch.before_count`, so a round's collect can tell real speech from what
    was already there (the ticket's third slot reads `step_count`; the launch
    needs the message count, and the row's `step_count` column already carries
    the raw count - the deviation is the report's to note). Restore's
    `used_live`／`wrote` stay on its own result: the launch does not branch on
    them (review N7).

    `expected_id` is the Session's own source id; a Session without one is
    refused before anything is written, and so is a raw naming a different
    conversation (review W2／W3, `restore`).
    """
    if expected_id is None:
        raise AgentError(NO_SOURCE_ID)
    _precheck_summaries()
    result = restore(raw, workdir=Path(workdir), expected_id=expected_id)
    ensure_summary_row(
        result.conversation_id,
        title=result.title, preview=result.preview, step_count=result.step_count,
        last_modified=result.last_modified if result.last_modified is not None else store.now(),
        workdir=Path(workdir) if workdir is not None else None)
    return result.conversation_id, result.messages

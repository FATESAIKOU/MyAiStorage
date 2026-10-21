"""The agy same-agent interactive continue (#26 PR2b).

Everything here runs against a /tmp fake `AGORA_AGY_HOME` (fixture-made
conversation dbs, a fake `conversation_summaries.db`) and the fake agy in
`tests/fakes/fake_agy.py` - the real agy binary is never launched and the
real home is never read or written. Ticket order, each ticket line at least
one test:

restore      target missing / same fingerprint noop / live newer by steps /
             live newer by the summaries stamp / live older overwrite (with a
             backup and a stderr line) / `-wal` and `-shm` refused with the
             remedy / temp never left behind / broken raw never replaces the
             live file / broken live file says to move it away / a raw naming
             another conversation refused / missing home / stale restore temps
             / cascade (not the envelope id) names the file
summaries    only a recognised table, only a missing id, one transaction,
             existing rows' stamp shape, unknown columns by declared type,
             another connection's -wal/-shm left alone
start_native argv exactly `[agy, --conversation, id]`, no flags, refusals
collect      None while nothing new / Exported / a gone db says the id moved
fp           db + wal move marks (cli._fingerprint's agy branch)
boundary     `continue --agent agy` reaches the fake agent end to end and
             writes back; the refusals that stay live in test_agent_agy.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import base64
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agora import cli, store
from agora.agents import agy, agy_restore, agy_store, agy_summaries, claude
from agora.agents.base import AgentError, Launch
from _round_seam import in_process_round_child, in_process_start_child
from test_agent_agy import (TURNS, count_steps, make_conversation, make_summaries,
                            run_cli)
from test_agent_agy import MUT as MUT  # the fixture module (make_agy_db.py)

FAKES = Path(__file__).resolve().parents[1] / "fakes"
FAKE_AGY = FAKES / "fake_agy.py"
FAKE_RCLONE = FAKES / "fake_rclone.py"


@pytest.fixture(autouse=True)
def fresh_module_caches(monkeypatch):
    """The module's caches and warn-once flags must not leak between tests."""
    monkeypatch.setenv("AGORA_AGY_CONTINUE", "1")        # the opt-in; the gate has its own tests below
    monkeypatch.setattr(agy_summaries, "_summaries_cache", None)
    monkeypatch.setattr(agy_summaries, "_warned_summary_for", False)
    monkeypatch.setattr(agy_summaries, "_warned_stamp", False)


@pytest.fixture
def agy_home(tmp_path, monkeypatch):
    home = tmp_path / "antigravity-cli"
    (home / "conversations").mkdir(parents=True)
    monkeypatch.setenv("AGORA_AGY_HOME", str(home))
    return home


@pytest.fixture
def fake_agy_cmd(tmp_path, monkeypatch):
    """The fake agy binary; `fake_agy_run` calls it the way a launch does."""
    monkeypatch.setenv("FAKE_HOME", str(tmp_path / "fh"))
    cmd = tmp_path / "agy"
    cmd.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_AGY} \"$@\"\n")
    cmd.chmod(0o755)
    monkeypatch.setenv("AGORA_AGY_CMD", str(cmd))
    return cmd


def fake_agy_run(argv: list[str]) -> None:
    out = subprocess.run([os.environ["AGORA_AGY_CMD"], *argv], env=dict(os.environ),
                         capture_output=True, text=True)
    if out.returncode != 0:
        pytest.fail(f"fake agy run failed: {out.stderr.strip()}")


@pytest.fixture
def env(tmp_path, monkeypatch, fake_agy_cmd):
    """Fake Drive + the fake agy binary + the continue seams, in-process."""
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setattr(cli, "load_agent", lambda name: agy.ADAPTER)
    monkeypatch.setattr(cli, "_round_child", in_process_round_child)
    monkeypatch.setattr(cli, "_start_child", in_process_start_child)


def raw_bytes_of(session_id: str) -> bytes:
    """The whole envelope bytes of a conversation, via the real export."""
    exported = agy.ADAPTER.export(session_id)            # owns the raw tmpdir while read
    return exported.raw_file.read_bytes()


def _tmp_names(home: Path) -> list[str]:
    conversations = home / "conversations"
    return sorted(p.name for p in conversations.iterdir() if p.name.startswith(".restore-"))


def _db_bytes_of(raw: bytes) -> bytes:
    return base64.b64decode(json.loads(raw)["db_b64"])


#: The real agy 1.4.4 `conversation_summaries` schema, copied from
#: `MyAiStorage-team/fixtures-real/agy-summaries-schema.sql` (#136: schema only,
#: no rows). The PR2b helper `make_summaries` is deliberately looser - its
#: table has none of these `NOT NULL` constraints, which is how 117 tests
#: missed the acceptance failure on `last_user_input_time`.
REAL_SUMMARIES_SCHEMA = """\
CREATE TABLE `conversation_summaries` (`conversation_id` text,`title` text NOT NULL DEFAULT "",`preview` text NOT NULL DEFAULT "",`step_count` integer NOT NULL DEFAULT 0,`last_modified_time` datetime NOT NULL,`workspace_uris` text NOT NULL,`status` text NOT NULL DEFAULT "",`source` text NOT NULL DEFAULT "",`project_id` text NOT NULL DEFAULT "",`agent_name` text NOT NULL DEFAULT "",`parent_conversation_id` text NOT NULL DEFAULT "",`nesting_depth` integer NOT NULL DEFAULT 0,`battle_id` text NOT NULL DEFAULT "",`winning_conversation_id` text NOT NULL DEFAULT "",`not_fully_idle` numeric NOT NULL DEFAULT false,`killed` numeric NOT NULL DEFAULT false,`last_user_input_time` datetime NOT NULL,`last_user_input_step_index` integer NOT NULL DEFAULT -1,`app_data_dir` text NOT NULL DEFAULT "",`raw_summary` blob,`group_id` text NOT NULL DEFAULT "",PRIMARY KEY (`conversation_id`));
CREATE INDEX `idx_conversation_summaries_last_user_input_time` ON `conversation_summaries`(`last_user_input_time`);
CREATE INDEX `idx_conversation_summaries_last_modified_time` ON `conversation_summaries`(`last_modified_time`);
"""

#: One row shaped the way the real agy writes its own (test conversation, fake
#: text), per the acceptance run in #136.
REAL_SAMPLE_ROW = {
    "conversation_id": "sample-conv", "title": "Sample title",
    "preview": "sample preview", "step_count": 3,
    "last_modified_time": "2026-10-21 08:41:06.65668+00:00",
    "workspace_uris": '["file:///private/tmp/agora-acc/proj"]',
    "status": "CASCADE_RUN_STATUS_IDLE", "source": "",
    "project_id": "default-cli-project", "agent_name": "",
    "parent_conversation_id": "", "nesting_depth": 0, "battle_id": "",
    "winning_conversation_id": "", "not_fully_idle": 0, "killed": 0,
    "last_user_input_time": "2026-10-21 08:41:06.65668+00:00",
    "last_user_input_step_index": -1, "app_data_dir": "antigravity-cli",
    "raw_summary": None, "group_id": "",
}


def make_real_summaries(home: Path, rows: list[dict] | None = None) -> Path:
    """The summaries db with the real agy's columns and constraints (#136)."""
    path = home / "conversation_summaries.db"
    con = sqlite3.connect(path)
    try:
        con.executescript(REAL_SUMMARIES_SCHEMA)
        for row in rows or []:
            names = ", ".join(f'"{name}"' for name in row)
            marks = ", ".join("?" for _ in row)
            con.execute(f"INSERT INTO conversation_summaries ({names}) VALUES ({marks})",
                        tuple(row.values()))
        con.commit()
    finally:
        con.close()
    return path


def summary_row(home: Path, conversation_id: str) -> dict[str, object]:
    """One summaries row as a dict, every column of the (real or loose) table."""
    con = sqlite3.connect(home / "conversation_summaries.db")
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM conversation_summaries WHERE conversation_id = ?",
                          (conversation_id,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"{conversation_id} has no summaries row"
    return dict(row)



# ---------------------------------------------------------------------------
# restore: what it writes, when it refuses, and what it never leaves behind
# ---------------------------------------------------------------------------


def test_restore_writes_the_conversation_when_the_target_is_missing(agy_home, tmp_path):
    session_id, meta = make_conversation(agy_home, TURNS, seed="restore-new")
    raw = raw_bytes_of(session_id)
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert result.conversation_id == session_id          # named by the db's own meta
    assert result.wrote and not result.used_live
    assert result.step_count == count_steps(agy_home, session_id)
    assert result.messages == 5                          # the 14/15 count for before_count
    assert result.title == "hello probe" and result.preview == "OK"
    assert result.last_modified == pytest.approx(MUT.EPOCH + 5)
    assert _db_bytes_of(raw) == (agy_home / "conversations" / f"{session_id}.db").read_bytes()
    assert not _tmp_names(agy_home)
    for suffix in ("-wal", "-shm"):
        assert not (agy_home / "conversations" / f"{session_id}.db{suffix}").exists()


def test_restore_of_the_same_fingerprint_is_a_noop(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-same")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote is False   # it IS the raw's twin
    first = (target.stat().st_size, target.stat().st_mtime_ns)
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert not result.wrote and not result.used_live
    assert (target.stat().st_size, target.stat().st_mtime_ns) == first


def test_restore_writes_an_absent_target_only_once(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-cycle")
    raw = raw_bytes_of(session_id)
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    second = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert not second.wrote and not second.used_live


def test_restore_declines_a_newer_live_copy(agy_home, tmp_path, capsys, fake_agy_cmd):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-live")
    raw = raw_bytes_of(session_id)                       # the envelope BEFORE the live turn
    fake_agy_run(["--conversation", session_id])         # one more turn on agy's side
    target = agy_home / "conversations" / f"{session_id}.db"
    live_bytes = target.read_bytes()
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert result.used_live and not result.wrote
    assert result.step_count == count_steps(agy_home, session_id)
    assert result.messages == 7 and result.title == "hello probe"
    assert target.read_bytes() == live_bytes             # not one byte is touched
    assert "直接以 agy 開活體" in capsys.readouterr().err


def test_restore_declines_when_only_the_summaries_stamp_is_later(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-stamp")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "small-same-cascade.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="restore-stamp-small")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())             # live is OLDER than the raw...
    make_summaries(agy_home, [{"id": session_id, "title": "x",
                               "last_modified_time": MUT.EPOCH + 60}])   # ...but listed later
    live_bytes = target.read_bytes()
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert result.used_live and not result.wrote
    assert target.read_bytes() == live_bytes


def test_restore_overwrites_an_older_live_copy(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-old")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "small-same-cascade.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="restore-old-small")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert result.wrote and not result.used_live
    assert count_steps(agy_home, session_id) == 6
    assert target.read_bytes() == _db_bytes_of(raw)
    assert not _tmp_names(agy_home)


def test_overwriting_an_older_live_copy_keeps_a_backup_and_says_so(agy_home, tmp_path,
                                                                 capsys):
    """N1: the one destructive step leaves the old live db in agora's own state."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="backup-old")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())
    old_bytes = target.read_bytes()
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    backup = store.Paths.from_env().state / "agy-backups" / f"{session_id}.db"
    assert backup.is_file() and backup.read_bytes() == old_bytes
    err = capsys.readouterr().err
    assert "將以 agora 的版本（6 步）覆蓋 agy 較舊的版本（2 步）" in err
    assert str(backup) in err
    assert not (agy_home / "agy-backups").exists()       # never a backup inside agy's home


def test_the_backup_is_private(agy_home, tmp_path):
    """W3: the backup carries a whole conversation - 0600 in a 0700 directory."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup-mode")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="backup-mode-old")
    (agy_home / "conversations" / f"{session_id}.db").write_bytes(sibling.read_bytes())
    agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    directory = store.Paths.from_env().state / "agy-backups"
    backup = directory / f"{session_id}.db"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_a_copy_that_dies_midway_leaves_the_previous_backup_alone(agy_home, tmp_path,
                                                                 monkeypatch):
    """W3/E7d: an atomic backup never truncates the good one it is replacing."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup-atomic")
    raw = raw_bytes_of(session_id)
    directory = agy_home / "conversations"
    target = directory / f"{session_id}.db"
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="backup-atomic-old")
    target.write_bytes(sibling.read_bytes())
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    backup = store.Paths.from_env().state / "agy-backups" / f"{session_id}.db"
    kept = backup.read_bytes()
    assert kept == sibling.read_bytes()
    before_others = {p.name for p in backup.parent.iterdir() if p.name != backup.name}

    target.write_bytes(sibling.read_bytes())            # live is older again
    old_live = target.read_bytes()

    def dies_midway(src, dst):
        with open(dst, "wb") as out:
            out.write(b"par")
        raise OSError("no space left")

    monkeypatch.setattr(agy_restore.shutil, "copyfile", dies_midway)
    with pytest.raises(AgentError, match="備份失敗"):
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert backup.read_bytes() == kept                   # the previous backup is intact
    assert {p.name for p in backup.parent.iterdir()
            if p.name != backup.name} == before_others   # no half-written copy appeared either
    assert target.read_bytes() == old_live               # and nothing replaced the live db
    assert not list(backup.parent.glob("*.tmp"))         # no half-written temp left behind
    assert not _tmp_names(agy_home)


def test_the_second_overwrite_keeps_the_original_backup(agy_home, tmp_path):
    """W3/E7b: the first backup is the user's agy version; later ones get a stamp."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup-keep")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    directory = store.Paths.from_env().state / "agy-backups"
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="backup-keep-old")
    target.write_bytes(sibling.read_bytes())
    original = target.read_bytes()
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    assert (directory / f"{session_id}.db").read_bytes() == original

    target.write_bytes(sibling.read_bytes())            # another older version
    second_live = target.read_bytes()
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    assert (directory / f"{session_id}.db").read_bytes() == original
    stamped = [p for p in directory.iterdir() if p.name != f"{session_id}.db"]
    assert len(stamped) == 1
    assert stamped[0].name.startswith(f"{session_id}.") and stamped[0].name.endswith(".db")
    assert stamped[0].read_bytes() == second_live


def test_five_overwrites_keep_only_three_stamped_backups(agy_home, tmp_path):
    """W3: the plain `<id>.db` is forever; only the newest three stamped ones stay."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup-retention")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    sibling = tmp_path / "older.db"
    old_versions = []
    for round_ in range(5):
        MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id,
                    seed=f"backup-retention-old-{round_}")
        target.write_bytes(sibling.read_bytes())
        old_versions.append(target.read_bytes())
        assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).wrote
    directory = store.Paths.from_env().state / "agy-backups"
    assert (directory / f"{session_id}.db").read_bytes() == old_versions[0]
    stamped = sorted(p for p in directory.iterdir() if p.name != f"{session_id}.db")
    assert len(stamped) == 3
    assert {p.read_bytes() for p in stamped} == set(old_versions[2:])


def test_a_failed_backup_stops_the_overwrite(agy_home, tmp_path, monkeypatch):
    """N1: no backup, no destructive step."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="backup-fails")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="backup-fails-old")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())
    old_bytes = target.read_bytes()

    def no_space(src, dst):
        raise OSError("no space left")

    monkeypatch.setattr(agy_restore.shutil, "copyfile", no_space)
    with pytest.raises(AgentError, match="備份失敗"):
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert target.read_bytes() == old_bytes
    assert not _tmp_names(agy_home)


def test_the_live_newer_warning_names_both_step_counts(agy_home, tmp_path, capsys):
    """N2b: the user has to see what was not restored, not just the live count."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="both-counts")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "small.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="both-counts-small")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())
    make_summaries(agy_home, [{"id": session_id, "title": "x",
                               "last_modified_time": MUT.EPOCH + 60}])
    assert agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id).used_live
    err = capsys.readouterr().err
    assert "活體 2 步" in err and "agora 的 raw 6 步" in err


def test_restore_refuses_a_target_under_agy(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-busy")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    target_before = target.read_bytes()
    for sidecar in ("-wal", "-shm"):
        (agy_home / "conversations" / f"{session_id}.db{sidecar}").write_bytes(b"occupied")
    with pytest.raises(AgentError) as caught:            # W4/W1: the remedy is in the message
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    message = str(caught.value)
    assert str(target) in message                       # the full path agy would open
    assert "旁有 -wal／-shm" in message
    assert "不要手動刪" in message                       # this wal has bytes
    assert target.read_bytes() == target_before
    assert not _tmp_names(agy_home)                      # the temp file is gone again


def test_a_zero_byte_wal_says_it_can_be_deleted(agy_home, tmp_path):
    """W1: 0 bytes is what SQLite leaves after the last connection closes."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-idle")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    wal = target.with_name(target.name + "-wal")
    wal.write_bytes(b"")
    target.with_name(target.name + "-shm").write_bytes(b"")
    with pytest.raises(AgentError) as caught:
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    message = str(caught.value)
    assert "可手動刪掉" in message and "若在跑，等它結束" in message
    assert str(wal) in message and "0 位元組" in message  # N5: path and size


def test_a_wal_with_content_says_not_to_delete_it(agy_home, tmp_path):
    """W1: those are committed turns SQLite has not checkpointed yet."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-pending")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    wal = target.with_name(target.name + "-wal")
    pending = b"uncheckpointed turns"
    wal.write_bytes(pending)
    with pytest.raises(AgentError) as caught:
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    message = str(caught.value)
    assert "不要手動刪" in message and "checkpoint" in message
    assert str(wal) in message and f"{len(pending)} 位元組" in message   # N5


def test_a_broken_raw_never_replaces_the_healthy_live_copy(agy_home, tmp_path):
    """W1: the check is on the temp file, before any `os.replace`.

    A raw whose header lies about its freelist fails `quick_check` while its
    steps are still readable - exactly the shape that used to replace a healthy
    live db first and only then complain.
    """
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-broken-raw")
    raw = raw_bytes_of(session_id)                       # the healthy newer envelope
    sibling = tmp_path / "healthy-older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="restore-old-live")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())            # a healthy OLDER live copy
    live_bytes = target.read_bytes()
    broken = bytearray(_db_bytes_of(raw))
    broken[36:40] = (7).to_bytes(4, "big")              # header freelist count
    probe = tmp_path / "broken.db"
    probe.write_bytes(bytes(broken))
    con = sqlite3.connect(f"file:{probe}?immutable=1", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM steps").fetchone()[0] == 6  # readable
    finally:
        con.close()
    with pytest.raises(AgentError, match="quick_check 沒過"):
        agy_restore.restore(
            json.dumps({"format": agy.FORMAT, "agy_version": None, "trajectory_id": None,
                        "scan_text": [], "db_b64": base64.b64encode(bytes(broken)).decode()
                        }).encode("utf-8"),
            workdir=tmp_path, expected_id=session_id)
    assert target.read_bytes() == live_bytes            # the live file was never replaced
    assert not _tmp_names(agy_home)


def test_a_failed_restore_leaves_the_directory_clean(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-junk")
    raw = json.dumps({"format": agy.FORMAT, "agy_version": None, "trajectory_id": None,
                      "scan_text": [], "db_b64": "!!not-base64!!"}).encode("utf-8")
    with pytest.raises(AgentError, match="db_b64 壞了"):
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert not _tmp_names(agy_home)


def test_restore_refuses_a_home_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("AGORA_AGY_HOME", str(tmp_path / "not-there"))
    with pytest.raises(AgentError, match="home 不存在"):
        agy_restore.restore(b'{"format": "agy-conversation/1", "db_b64": ""}',
                            workdir=tmp_path)
    assert not (tmp_path / "not-there").exists()         # a typo'd home is not fabricated


def test_restore_refuses_a_raw_that_names_another_conversation(agy_home, tmp_path):
    """W3: the raw's own cascade must be the Session's recorded source id.

    A Drive copy can be edited to point at a different (older, shorter)
    conversation; with only the step-count rule it would win and overwrite it.
    """
    source_id, _ = make_conversation(agy_home, TURNS, seed="w3-source")
    victim_id, _ = make_conversation(agy_home, TURNS[:1], seed="w3-victim")
    victim = agy_home / "conversations" / f"{victim_id}.db"
    victim_bytes = victim.read_bytes()
    stolen = tmp_path / "stolen.db"
    MUT.make_db(str(stolen), TURNS, cascade_id=victim_id, seed="w3-stolen")
    raw = json.dumps({"format": agy.FORMAT, "agy_version": None, "trajectory_id": None,
                      "scan_text": [],
                      "db_b64": base64.b64encode(stolen.read_bytes()).decode()}).encode()
    with pytest.raises(AgentError, match="不是這個 Session 的來源"):
        agy_restore.restore(raw, workdir=tmp_path, expected_id=source_id)
    assert victim.read_bytes() == victim_bytes           # not one byte of the victim
    assert not _tmp_names(agy_home)


def test_restore_without_a_recorded_source_id_refuses(agy_home, tmp_path):
    """W2/E4b: `expected_id=None` used to skip the check and overwrite the victim."""
    victim_id, _ = make_conversation(agy_home, TURNS[:1], seed="w2-victim")
    victim = agy_home / "conversations" / f"{victim_id}.db"
    victim_bytes = victim.read_bytes()
    stolen = tmp_path / "stolen.db"
    MUT.make_db(str(stolen), TURNS, cascade_id=victim_id, seed="w2-stolen")
    raw = json.dumps({"format": agy.FORMAT, "agy_version": None, "trajectory_id": None,
                      "scan_text": [],
                      "db_b64": base64.b64encode(stolen.read_bytes()).decode()}).encode()
    with pytest.raises(AgentError, match="沒有記來源 id"):
        agy_restore.restore(raw, workdir=tmp_path)
    assert victim.read_bytes() == victim_bytes           # not one byte of the victim
    assert not _tmp_names(agy_home)
    with pytest.raises(AgentError, match="沒有記來源 id"):
        agy_restore.prepare_continue(raw, workdir=tmp_path)   # refused before any write


def test_restore_of_the_sessions_own_cascade_is_not_refused(agy_home, tmp_path):
    """W3, the matching side: the recorded id and the raw agree, so it restores."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="w3-match")
    raw = raw_bytes_of(session_id)
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    result = agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert result.wrote and result.conversation_id == session_id
    assert count_steps(agy_home, session_id) == 6


def test_a_live_db_that_cannot_be_read_says_what_to_do(agy_home, tmp_path):
    """N4: the broken file is the live one, so name it and say to move it away."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="live-broken")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    junk = b"not a database" * 100
    target.write_bytes(junk)
    with pytest.raises(AgentError) as caught:
        agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    message = str(caught.value)
    assert str(target) in message and "讀不了" in message
    assert "手動移走再接續" in message
    assert target.read_bytes() == junk                   # a read failure changes nothing


def test_a_restore_cleans_old_restore_temps_but_not_recent_ones(agy_home, tmp_path):
    """N9: a SIGKILL leaves `.restore-*.tmp` in agy's directory; only old ones go."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="restore-stale")
    raw = raw_bytes_of(session_id)
    directory = agy_home / "conversations"
    old = directory / ".restore-old.tmp"
    new = directory / ".restore-new.tmp"
    old.write_bytes(b"a SIGKILL left this")
    new.write_bytes(b"another restore is writing this")
    old_time = store.now() - 7200
    os.utime(old, (old_time, old_time))
    agy_restore.restore(raw, workdir=tmp_path, expected_id=session_id)
    assert not old.exists() and new.exists()


def test_prepare_continue_names_the_cascade_not_the_envelope_id(agy_home, tmp_path):
    """#26 spike: the file name is the db's own meta (`cascade_id`); the envelope
    only carries the inner `trajectory_id`, and the restore never confuses them."""
    session_id, meta = make_conversation(agy_home, TURNS[:1], seed="restore-meta")
    make_summaries(agy_home, [])
    raw = raw_bytes_of(session_id)
    assert meta["trajectory_id"] != session_id
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    cascade, messages = agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert cascade == session_id and messages == 2


# ---------------------------------------------------------------------------
# ensure_summary_row: the row agy loads a conversation by (the PR2b spike)
# ---------------------------------------------------------------------------


def test_ensure_summary_row_inserts_only_a_missing_id(agy_home, tmp_path):
    make_summaries(agy_home, [{"id": "other", "title": "T", "step_count": 1,
                               "last_modified_time": MUT.EPOCH}])
    assert agy_summaries.ensure_summary_row(
        "0123-conv", title="hello probe", preview="OK", step_count=6,
        last_modified=1767225600.0, workdir=tmp_path) is True
    assert agy_summaries.ensure_summary_row(
        "0123-conv", title="另一個", preview="另", step_count=9,
        last_modified=1767225700.0, workdir=tmp_path) is False   # already listed
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        rows_ = con.execute("SELECT conversation_id, title, step_count"
                            " FROM conversation_summaries ORDER BY conversation_id").fetchall()
    finally:
        con.close()
    assert rows_ == [("0123-conv", "hello probe", 6), ("other", "T", 1)]


@pytest.mark.parametrize("shape, sample, fresh", [
    ("int-seconds", MUT.EPOCH, 1767225600),
    ("int-milliseconds", MUT.EPOCH * 1000, 1767225600000),
    ("float-seconds", float(MUT.EPOCH), 1767225600.0),
    ("iso-string", "2025-06-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    ("empty-table", None, "2026-01-01T00:00:00Z"),
])
def test_the_fresh_stamp_keeps_the_table_s_own_shape(agy_home, tmp_path, shape, sample, fresh):
    rows_ = ([] if shape == "empty-table" else
             [{"id": "s", "title": "x", "preview": "y", "step_count": 1,
               "last_modified_time": sample}])
    make_summaries(agy_home, rows_)
    assert agy_summaries.ensure_summary_row(
        "shape-conv", title="t", preview="p", step_count=1,
        last_modified=1767225600.0, workdir=tmp_path) is True
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        value = con.execute("SELECT last_modified_time FROM conversation_summaries"
                            " WHERE conversation_id='shape-conv'").fetchone()[0]
    finally:
        con.close()
    assert value == fresh


def test_ensure_summary_row_fits_the_real_agy_schema(agy_home, tmp_path):
    """#136: every NOT NULL column of the real table gets a value, in its shape."""
    make_real_summaries(agy_home, [REAL_SAMPLE_ROW])     # one row the real agy wrote
    assert agy_summaries.ensure_summary_row(
        "fresh-conv", title="fresh", preview="p", step_count=4,
        last_modified=1767225600.0, workdir=tmp_path) is True
    row = summary_row(agy_home, "fresh-conv")
    # both datetimes carry the same moment, written the way the sample row is:
    # a space, five fraction digits, +00:00 - not ISO T…Z.
    assert row["last_modified_time"] == row["last_user_input_time"] == (
        "2026-01-01 00:00:00.00000+00:00")
    assert row["workspace_uris"] == f'["file://{tmp_path}"]'
    assert row["status"] == "CASCADE_RUN_STATUS_IDLE"
    assert row["project_id"] == "default-cli-project"
    assert row["app_data_dir"] == "antigravity-cli"
    assert row["title"] == "fresh" and row["preview"] == "p"
    assert row["step_count"] == 4
    assert row["source"] == row["agent_name"] == row["parent_conversation_id"] == ""
    assert row["battle_id"] == row["winning_conversation_id"] == row["group_id"] == ""
    assert row["nesting_depth"] == row["not_fully_idle"] == row["killed"] == 0
    assert row["last_user_input_step_index"] == -1
    assert row["raw_summary"] is None               # a nullable blob stays NULL
    assert summary_row(agy_home, "sample-conv")["title"] == "Sample title"


def test_ensure_summary_row_on_an_empty_real_schema_falls_back(agy_home, tmp_path):
    """#136/(b): no row to copy → the shapes this adapter always wrote."""
    make_real_summaries(agy_home)                    # the real constraints, no rows
    assert agy_summaries.ensure_summary_row(
        "fresh-conv", title="fresh", preview="p", step_count=4,
        last_modified=1767225600.0, workdir=tmp_path) is True
    row = summary_row(agy_home, "fresh-conv")
    assert row["last_modified_time"] == row["last_user_input_time"] == (
        "2026-01-01T00:00:00Z")                     # the 未驗 ISO fallback
    assert row["workspace_uris"] == f"file://{tmp_path}"      # plain, as before
    assert row["status"] == row["project_id"] == row["app_data_dir"] == ""


def test_the_fresh_workspace_uris_keeps_a_plain_sample_plain(agy_home, tmp_path):
    """#136: the JSON-array text is copied because the rows use it, not hardcoded."""
    make_real_summaries(agy_home, [dict(REAL_SAMPLE_ROW, workspace_uris="/tmp/proj")])
    assert agy_summaries.ensure_summary_row(
        "fresh-conv", title="fresh", preview="p", step_count=4,
        last_modified=1767225600.0, workdir=tmp_path) is True
    assert summary_row(agy_home, "fresh-conv")["workspace_uris"] == f"file://{tmp_path}"


def test_ensure_summary_row_fills_unknown_columns_by_declared_type(agy_home, tmp_path):
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TABLE conversation_summaries ("
                    "conversation_id text PRIMARY KEY, title text, preview text, "
                    "step_count integer, last_modified_time integer, workspace_uris text, "
                    "status text, source text, project_id text, agent_name text, "
                    "parent_conversation_id text, group_id text, "
                    "extra_count integer, raw_summary blob)")
        con.commit()
    finally:
        con.close()
    assert agy_summaries.ensure_summary_row(
        "declared-conv", title="t", preview="p", step_count=3,
        last_modified=1767225600.0, workdir=tmp_path) is True
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        row = con.execute(
            "SELECT status, source, project_id, agent_name, parent_conversation_id,"
            " group_id, extra_count, raw_summary, workspace_uris, last_modified_time"
            " FROM conversation_summaries WHERE conversation_id='declared-conv'").fetchone()
    finally:
        con.close()
    assert row[:6] == ("", "", "", "", "", "")
    assert row[6] == 0 and row[7] is None                # a blob stays NULL, nothing invented
    assert row[8] == f"file://{tmp_path}"
    assert row[9] == "2026-01-01T00:00:00Z"              # no existing sample: the ISO 未驗 shape


def test_ensure_summary_row_refuses_a_structure_it_does_not_know(agy_home):
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TABLE unrelated (x text)")
        con.commit()
    finally:
        con.close()
    with pytest.raises(AgentError, match="結構認不出來"):
        agy_summaries.ensure_summary_row("x", title="t", preview="p", step_count=1,
                                         last_modified=1767225600.0, workdir=None)


def test_ensure_summary_row_refuses_to_create_the_db(agy_home, tmp_path):
    with pytest.raises(AgentError, match="不存在，不補列"):
        agy_summaries.ensure_summary_row(
            "none", title="t", preview="p", step_count=1, last_modified=1e9,
            workdir=tmp_path)
    assert not (agy_home / "conversation_summaries.db").exists()


def test_a_failing_insert_rolls_back_and_a_later_one_can_work(agy_home, tmp_path):
    make_summaries(agy_home, [])
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        con.execute("CREATE TRIGGER refuse_insert BEFORE INSERT ON conversation_summaries"
                    " BEGIN SELECT RAISE(ABORT, 'forced'); END;")
        con.commit()
    finally:
        con.close()
    with pytest.raises(AgentError, match="寫不進去"):
        agy_summaries.ensure_summary_row(
            "rollback-conv", title="t", preview="p", step_count=1,
            last_modified=1767225600.0, workdir=tmp_path)
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        assert con.execute("SELECT count(*) FROM conversation_summaries").fetchone()[0] == 0
        con.execute("DROP TRIGGER refuse_insert")
        con.commit()
    finally:
        con.close()
    assert agy_summaries.ensure_summary_row(
        "rollback-conv", title="t", preview="p", step_count=1,
        last_modified=1767225600.0, workdir=tmp_path) is True


def test_an_existing_row_leaves_a_live_connection_s_wal_and_shm_alone(agy_home, tmp_path):
    """W2: agy may have the summaries db open while a second continue runs.

    Deleting `-wal`／`-shm` under that connection would hand later connections
    a different shm - the adapter has no business touching them, and SQLite
    removes them itself when the last connection closes.
    """
    path = make_summaries(agy_home, [{"id": "busy-conv", "title": "t", "preview": "p",
                                     "step_count": 1, "last_modified_time": MUT.EPOCH}])
    other = sqlite3.connect(path, isolation_level=None)      # the running agy
    try:
        other.execute("PRAGMA journal_mode=WAL").fetchone()
        assert other.execute("SELECT count(*) FROM conversation_summaries").fetchone()[0] == 1
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        assert wal.exists() and shm.exists()                 # the live WAL shape
        assert agy_summaries.ensure_summary_row(
            "busy-conv", title="other", preview="other", step_count=9,
            last_modified=1767225700.0, workdir=tmp_path) is False   # already listed
        assert wal.exists() and shm.exists()                 # nothing was unlinked
        assert other.execute("SELECT title FROM conversation_summaries"
                             " WHERE conversation_id='busy-conv'").fetchone() == ("t",)
    finally:
        other.close()


# ---------------------------------------------------------------------------
# prepare_continue / start_native / collect / fingerprint
# ---------------------------------------------------------------------------


def test_a_missing_summaries_list_stops_before_touching_the_conversation(agy_home, tmp_path):
    """N5/N1: the restore writes for real, so the summaries list is checked first.

    The live copy is deliberately OLDER than the raw (a restore would overwrite
    it), so "nothing in `conversations/` moved" actually proves the precheck ran
    first; with a same-fingerprint live copy the restore is a no-op and the
    test could not tell (review N1).
    """
    session_id, _ = make_conversation(agy_home, TURNS, seed="precheck")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="precheck-old")
    directory = agy_home / "conversations"
    target = directory / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())            # older: a restore would replace it
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in directory.iterdir()}
    with pytest.raises(AgentError, match="不存在，不補列"):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in directory.iterdir()}
    assert after == before and target.is_file()          # no file was restored


def test_the_precheck_refuses_a_row_the_real_schema_cannot_take(agy_home, tmp_path):
    """#136: a NOT NULL column the fill rules cannot satisfy stops it first.

    The real schema plus one future-looking column (`mystery blob NOT NULL`,
    the ticket's example): the restore would replace the older live copy, so
    «nothing in `conversations/` moved» proves the dry run ran before it.
    """
    session_id, _ = make_conversation(agy_home, TURNS, seed="precheck-row")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="precheck-row-old")
    directory = agy_home / "conversations"
    target = directory / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())            # older: a restore would replace it
    path = make_real_summaries(agy_home)                # empty, so the ADD COLUMN is allowed
    con = sqlite3.connect(path)
    try:
        con.execute("ALTER TABLE conversation_summaries ADD COLUMN mystery blob NOT NULL")
        con.commit()
    finally:
        con.close()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in directory.iterdir()}
    with pytest.raises(AgentError, match="mystery 是 NOT NULL"):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in directory.iterdir()}
    assert after == before and target.is_file()          # not one byte was written
    assert not _tmp_names(agy_home)


def test_a_failed_summaries_insert_takes_the_restored_file_back(agy_home, tmp_path,
                                                                monkeypatch):
    """#136: a conversation file must not survive without its summaries row.

    The target was missing, so the restore filled an empty slot; the failed
    insert has to take exactly that file away again.
    """
    session_id, _ = make_conversation(agy_home, TURNS, seed="undo-missing")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    target.unlink()
    make_real_summaries(agy_home)                        # the precheck passes

    def refuse(*args, **kwargs):
        raise AgentError("forced: 鎖逾時")

    monkeypatch.setattr(agy_restore, "ensure_summary_row", refuse)
    with pytest.raises(AgentError, match="已撤回剛還原的對話檔"):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert not target.exists()                          # no row-less conversation left
    assert not _tmp_names(agy_home)


def test_an_interrupt_during_the_summaries_insert_also_takes_the_restored_file_back(
        agy_home, tmp_path, monkeypatch):
    """#136 review W1: Ctrl-C while waiting for agy's lock is an interrupt, not an
    Exception - it still must not leave a conversation file without its row, and it
    still arrives as the interrupt (not wrapped)."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="undo-interrupt")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    target.unlink()
    make_real_summaries(agy_home)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(agy_restore, "ensure_summary_row", interrupted)
    with pytest.raises(KeyboardInterrupt):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert not target.exists()
    assert not _tmp_names(agy_home)


def test_a_failed_undo_still_says_why_the_row_failed(agy_home, tmp_path, monkeypatch):
    """#136 review N2: when taking the file back fails too, the message carries the
    original reason (a lock timeout, a constraint) as well as the undo failure."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="undo-fails")
    raw = raw_bytes_of(session_id)
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    make_real_summaries(agy_home)

    def refuse(*args, **kwargs):
        raise AgentError("forced: 鎖逾時")

    def cannot_undo(result):
        raise AgentError("撤回失敗（forced）")

    monkeypatch.setattr(agy_restore, "ensure_summary_row", refuse)
    monkeypatch.setattr(agy_restore, "_withdraw_restore", cannot_undo)
    with pytest.raises(AgentError) as err:
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert "撤回失敗" in str(err.value) and "鎖逾時" in str(err.value)


def test_a_column_default_the_table_declares_is_used_not_the_type_default(agy_home):
    """#136 review N1: `last_user_input_step_index` is declared DEFAULT -1; a row we add
    carries what agy itself writes, not the integer type's 0."""
    from agora.agents import agy_summaries
    for text, want in (("-1", (True, -1)), ("0", (True, 0)), ('""', (True, "")),
                       ("'x'", (True, "x")), ("false", (True, 0)), ("true", (True, 1)),
                       ("1.5", (True, 1.5)), ("CURRENT_TIMESTAMP", (False, None)),
                       ("NULL", (False, None)), (None, (False, None))):
        assert agy_summaries._declared_default(text) == want, text


def test_a_failed_summaries_insert_puts_the_overwritten_live_copy_back(
        agy_home, tmp_path, monkeypatch):
    """#136: an overwritten live copy comes back byte for byte, not just gone."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="undo-live")
    raw = raw_bytes_of(session_id)
    sibling = tmp_path / "older.db"
    MUT.make_db(str(sibling), TURNS[:1], cascade_id=session_id, seed="undo-live-old")
    target = agy_home / "conversations" / f"{session_id}.db"
    target.write_bytes(sibling.read_bytes())
    live_bytes = target.read_bytes()
    make_real_summaries(agy_home)

    def refuse(*args, **kwargs):
        raise AgentError("forced: 約束")

    monkeypatch.setattr(agy_restore, "ensure_summary_row", refuse)
    with pytest.raises(AgentError, match="已撤回剛還原的對話檔"):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert target.read_bytes() == live_bytes             # the old live copy is back
    backup = store.Paths.from_env().state / "agy-backups" / f"{session_id}.db"
    assert backup.read_bytes() == live_bytes
    assert not _tmp_names(agy_home)


def test_a_failed_summaries_insert_leaves_a_file_the_restore_only_read(
        agy_home, tmp_path, monkeypatch):
    """#136: only the file this restore wrote is withdrawn - never agy's own.

    The target has the raw's own fingerprint, so `restore` wrote nothing; a
    failure in the row insert must leave that live copy where it is.
    """
    session_id, _ = make_conversation(agy_home, TURNS, seed="undo-noop")
    raw = raw_bytes_of(session_id)
    target = agy_home / "conversations" / f"{session_id}.db"
    live_bytes = target.read_bytes()
    make_real_summaries(agy_home)

    def refuse(*args, **kwargs):
        raise AgentError("forced: 磁碟")

    monkeypatch.setattr(agy_restore, "ensure_summary_row", refuse)
    with pytest.raises(AgentError, match="活體本來就沒動"):
        agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert target.read_bytes() == live_bytes
    assert not _tmp_names(agy_home)


def test_prepare_continue_answers_the_launch_facts(agy_home, tmp_path, capsys):
    session_id, _ = make_conversation(agy_home, TURNS, seed="prepare")
    raw = raw_bytes_of(session_id)
    make_summaries(agy_home, [])                         # the row is what gets inserted
    cascade, before = agy_restore.prepare_continue(raw, workdir=tmp_path, expected_id=session_id)
    assert cascade == session_id and before == 5
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        row = con.execute("SELECT step_count, title, workspace_uris"
                          " FROM conversation_summaries").fetchone()
    finally:
        con.close()
    assert row == (count_steps(agy_home, session_id), "hello probe", f"file://{tmp_path}")
    assert "summaries 說" not in capsys.readouterr().err   # count(*) is never spurious


def test_start_native_launches_the_plain_conversation(agy_home, tmp_path, monkeypatch):
    session_id, _ = make_conversation(agy_home, TURNS, seed="launch")
    make_summaries(agy_home, [])
    raw = raw_bytes_of(session_id)
    monkeypatch.setenv("AGORA_AGY_CMD", "AGY-FAKE-CMD")
    launch = agy.ADAPTER.start_native(raw, tmp_path, expected_id=session_id)
    assert launch.argv == ["AGY-FAKE-CMD", "--conversation", session_id]
    assert launch.cwd == str(tmp_path)
    assert launch.agent_session_id == session_id
    assert launch.before_count == 5                      # and not one agy flag is added


def test_start_native_refuses_headless_and_harness(agy_home, tmp_path):
    session_id, _ = make_conversation(agy_home, TURNS, seed="launch-refuse")
    make_summaries(agy_home, [])
    raw = raw_bytes_of(session_id)
    with pytest.raises(AgentError, match="agy 的 headless 在後續"):
        agy.ADAPTER.start_native(raw, tmp_path, headless=True)
    with pytest.raises(AgentError, match="--harness-file"):
        agy.ADAPTER.start_native(raw, tmp_path, harness_file=tmp_path / "h")


def test_start_native_refuses_a_raw_that_names_another_conversation(agy_home, tmp_path):
    """W3 through the adapter entry the cli uses."""
    session_id, _ = make_conversation(agy_home, TURNS, seed="launch-w3")
    make_summaries(agy_home, [])
    raw = raw_bytes_of(session_id)
    with pytest.raises(AgentError, match="不是這個 Session 的來源"):
        agy.ADAPTER.start_native(raw, tmp_path, expected_id="a-different-conversation")


def test_collect_returns_none_until_something_new_is_said(agy_home, fake_agy_cmd):
    session_id, _ = make_conversation(agy_home, TURNS[:1], seed="collect")
    make_summaries(agy_home, [{"id": session_id, "title": "t", "step_count": 2,
                               "last_modified_time": MUT.EPOCH}])
    launch = Launch(argv=[], cwd=".", agent_session_id=session_id, before_count=2)
    assert agy.ADAPTER.collect(launch) is None           # nothing newer yet
    fake_agy_run(["--conversation", session_id])         # the fake appends one turn
    exported = agy.ADAPTER.collect(launch)
    assert exported is not None and exported.message_count == 4
    assert [line for _role, lines in exported.turns for line in lines][-1] == "ZZSAY 好的"


def test_collect_of_a_gone_db_says_the_id_may_have_changed(agy_home):
    session_id, _ = make_conversation(agy_home, TURNS[:1], seed="collect-gone")
    make_summaries(agy_home, [{"id": session_id, "step_count": 2,
                               "last_modified_time": MUT.EPOCH}])
    (agy_home / "conversations" / f"{session_id}.db").unlink()
    with pytest.raises(AgentError, match="agy 可能換了對話 id（#26 review finding 7）"):
        agy.ADAPTER.collect(Launch(argv=[], cwd=".", agent_session_id=session_id,
                                   before_count=2))


def test_collect_without_a_session_id_refuses():
    with pytest.raises(AgentError, match="沒有 agent session id"):
        agy.ADAPTER.collect(Launch(argv=[], cwd=".", agent_session_id=None))


def test_move_fingerprint_tracks_the_db_and_the_wal(agy_home):
    session_id, meta = make_conversation(agy_home, with_wal=True, seed="fp")
    db = agy_home / "conversations" / f"{session_id}.db"
    live = agy_store.move_fingerprint(db)
    assert live is not None
    assert live[:2] == (db.stat().st_size, db.stat().st_mtime_ns)
    assert live[2] > 0                                   # the wal is part of the mark
    meta["connection"].close()                           # agy's own cleanup: no wal, no shm
    idle = agy_store.move_fingerprint(db)
    assert idle != live and idle[2:] == (0, 0)


def test_the_fingerprint_of_a_missing_db_is_none(agy_home):
    assert agy_store.move_fingerprint(agy_home / "conversations" / "nope.db") is None


def test_cli_fingerprint_serves_the_agy_record(agy_home):
    session_id, meta = make_conversation(agy_home, with_wal=True, seed="cli-fp")
    record = {"agent_session_id": session_id, "dir": None, "agent": "agy"}
    before = cli._fingerprint(record, agy.ADAPTER)
    assert before is not None and before[2] > 0
    meta["connection"].close()
    after = cli._fingerprint(record, agy.ADAPTER)
    assert after != before and after[2:] == (0, 0)
    assert cli._fingerprint({"agent_session_id": "not-there"}, agy.ADAPTER) is None
    assert cli._fingerprint({"agent_session_id": "../walk-out"}, agy.ADAPTER) is None
    assert cli._fingerprint(record, claude.ADAPTER) is None   # other agents keep their rules


# ---------------------------------------------------------------------------
# the boundary: same agent continue with the fake agy, end to end
# ---------------------------------------------------------------------------


def _import_session(agy_home, capsys, seed: str, *, with_row: bool = True):
    """(cascade id, agora id) of one imported fixture conversation."""
    session_id, _ = make_conversation(agy_home, TURNS, seed=seed)
    if with_row:
        make_summaries(agy_home, [{"id": session_id, "title": "Echo test",
                                   "preview": "hello probe",
                                   "step_count": count_steps(agy_home, session_id),
                                   "last_modified_time": MUT.EPOCH,
                                   "workspace_uris": '["file:///tmp/agy-fixture-ws"]'}])
    code, out, err = run_cli(capsys, "import", "session",
                             "--external-session-id", session_id, "--agent", "agy")
    assert code == 0, err
    return session_id, out.splitlines()[-1]


def test_same_agent_continue_runs_the_fake_agy_and_writes_back(agy_home, env,
                                                               tmp_path, capsys):
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-y")
    work = tmp_path / "proj"
    work.mkdir()
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    fh = tmp_path / "fh"
    launches = [json.loads(line) for line in (fh / "agy-args.log").read_text().splitlines()]
    assert launches[-1] == ["--conversation", cascade]
    assert (fh / "agy-cwd.log").read_text().splitlines()[-1] == str(work)
    document = (store.Paths.from_env().mirror / agora_id.split(":")[1] / "session.md").read_text()
    assert "hello probe" in document and "[tool] run_command" in document
    assert "ZZSAY 好的" in document                       # the fake's new round came back
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        row = con.execute("SELECT workspace_uris, step_count FROM conversation_summaries"
                          " WHERE conversation_id = ?", (cascade,)).fetchone()
    finally:
        con.close()
    assert row == ('["file:///tmp/agy-fixture-ws"]', 6)   # already listed at load: untouched
    assert count_steps(agy_home, cascade) == 8            # while the conversation grew
    assert not _tmp_names(agy_home)
    for suffix in ("-wal", "-shm"):
        assert not (agy_home / f"conversation_summaries.db{suffix}").exists()
        assert not (agy_home / "conversations" / f"{cascade}.db{suffix}").exists()


def test_continue_inserts_the_missing_summaries_row_itself(agy_home, env, tmp_path, capsys):
    """The spike (#26): agy loads by the row and the db file alone is not enough -
    so the continue inserts the row (the import happened without one)."""
    make_summaries(agy_home, [])                         # agy's list exists; this id is not in it
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-spike", with_row=False)
    work = tmp_path / "proj"
    work.mkdir()
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    assert "可能換了對話 id" not in err
    con = sqlite3.connect(agy_home / "conversation_summaries.db")
    try:
        row = con.execute("SELECT title, step_count, workspace_uris"
                          " FROM conversation_summaries WHERE conversation_id = ?",
                          (cascade,)).fetchone()
    finally:
        con.close()
    assert row is not None
    assert row[0] == "hello probe" and row[1] == 6       # the row says what the launch opened
    assert count_steps(agy_home, cascade) == 8           # while the conversation itself grew
    assert row[2] == f"file://{work}"
    document = (store.Paths.from_env().mirror / agora_id.split(":")[1] / "session.md").read_text()
    assert "ZZSAY 好的" in document                       # a row is all the fake agent needed


def test_the_continue_writes_a_row_into_the_real_schema(agy_home, env, tmp_path, capsys):
    """#136: the whole `continue --agent agy` against the real table's constraints."""
    make_real_summaries(agy_home)                       # a fresh agy: no rows yet
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-real-schema", with_row=False)
    work = tmp_path / "proj"
    work.mkdir()
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    row = summary_row(agy_home, cascade)
    assert row["last_modified_time"] == row["last_user_input_time"]   # both NOT NULL, one moment
    assert row["workspace_uris"] == f"file://{work}"
    assert row["status"] == row["project_id"] == row["app_data_dir"] == ""
    document = (store.Paths.from_env().mirror / agora_id.split(":")[1] / "session.md").read_text()
    assert "ZZSAY 好的" in document
    assert not _tmp_names(agy_home)


def test_the_continue_hands_the_session_s_own_source_id_to_the_restore(
        agy_home, env, tmp_path, capsys, monkeypatch):
    """W3/W2 wiring: `cli._load_for_launch` goes by `restores_in_place`."""
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-expected")
    work = tmp_path / "proj"
    work.mkdir()
    seen: dict = {}
    real = agy.ADAPTER.start_native

    def spy(raw, workdir, *, headless=False, headless_port=None, harness_file=None,
            expected_id=None):
        seen["expected_id"] = expected_id
        return real(raw, workdir, headless=headless, headless_port=headless_port,
                    harness_file=harness_file, expected_id=expected_id)

    monkeypatch.setattr(agy.ADAPTER, "start_native", spy)
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    assert seen["expected_id"] == cascade               # the Session's recorded source id
    # The protocol the cli goes by: an explicit attribute, not a signature guess.
    assert agy.ADAPTER.restores_in_place is True
    assert getattr(claude.ADAPTER, "restores_in_place", False) is False


def test_continue_never_reaches_for_the_real_home(agy_home, env, tmp_path, capsys,
                                                  monkeypatch):
    """#26 的 guard：`HOME` 指到哨兵目錄，整個 continue 跑完那裡仍然是空的.

    The old guard swapped `Path.home` out; `expanduser()` does not call it, and
    `AGORA_AGY_HOME` short-circuits the `or`, so it could not catch a `home()`
    that stopped honouring the environment (review N8). A sentinel `HOME` and
    "`home()` is under `tmp_path`" do.
    """
    sentinel = tmp_path / "real-home-sentinel"
    sentinel.mkdir()
    monkeypatch.setenv("HOME", str(sentinel))
    make_summaries(agy_home, [])
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-home", with_row=False)
    work = tmp_path / "proj"
    work.mkdir()
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    assert list(sentinel.iterdir()) == []               # nothing was written under ~
    assert agy_store.home().is_relative_to(tmp_path)    # and it is the fake home
    document = (store.Paths.from_env().mirror / agora_id.split(":")[1] / "session.md").read_text()
    assert "ZZSAY 好的" in document


def test_a_noop_agy_writes_no_new_version(agy_home, env, tmp_path, capsys, monkeypatch):
    """The baseline「沒有新內容」: nothing said, nothing saved, no error."""
    cascade, agora_id = _import_session(agy_home, capsys, "e2e-noop")
    work = tmp_path / "proj"
    work.mkdir()
    monkeypatch.setenv("FAKE_AGENT_MODE", "noop")
    code, out, err = run_cli(capsys, "continue", "session", agora_id, "--agent", "agy",
                             "--dir", str(work), "--sync-period", "0")
    assert code == 0, err
    assert "沒有新內容" in err
    document = (store.Paths.from_env().mirror / agora_id.split(":")[1] / "session.md").read_text()
    assert "ZZSAY" not in document
    launches = [json.loads(line) for line in (tmp_path / "fh" / "agy-args.log")
                .read_text().splitlines()]
    assert launches[-1] == ["--conversation", cascade]    # the launch itself still ran


def test_pruning_backups_never_touches_another_ids_files(tmp_path):
    """r3 N1: `abc.123.db` has digits between the dots but is not a stamped backup of `abc`."""
    from agora.agents import agy_restore
    directory = tmp_path / "agy-backups"
    directory.mkdir()
    stamped = [f"abc.2025010{n}T000000000000Z.db" for n in range(1, 6)]
    bystanders = ["abc.db", "abc.123.db", "abc.1.db", "abc.def.2025010", "abcd.db", "abc.123T4Z.db"]
    for name in stamped + bystanders:
        (directory / name).write_bytes(b"x")
    agy_restore._prune_backups(directory, "abc")
    left = sorted(p.name for p in directory.iterdir())
    assert [n for n in left if n in stamped] == stamped[-3:], "only the three newest stamped backups stay"
    for name in bystanders:
        assert name in left, f"{name} is not a stamped backup of abc and must stay"


def test_the_agy_continue_is_off_unless_the_user_opts_in(agy_home, monkeypatch, capsys):
    """The restore writes the real agy directory and real agy is unverified: no opt-in, no write."""
    from agora import cli
    from agora.agents import agy
    monkeypatch.delenv("AGORA_AGY_CONTINUE", raising=False)
    before = sorted(p.name for p in agy_home.rglob("*"))
    with pytest.raises(agy.AgentError) as err:
        agy.ADAPTER.start_native(b"{}", agy_home.parent, expected_id="x")
    assert "備份" in str(err.value) and "AGORA_AGY_CONTINUE=1" in str(err.value)
    for value in ("", "0", "true", "yes"):
        monkeypatch.setenv("AGORA_AGY_CONTINUE", value)
        assert not agy.restore_enabled(), value
    assert sorted(p.name for p in agy_home.rglob("*")) == before, "nothing was written"
    monkeypatch.setenv("AGORA_AGY_CONTINUE", "1")
    assert agy.restore_enabled()


def test_cli_continue_agy_is_refused_before_anything_without_the_opt_in(agy_home, tmp_path, monkeypatch, capsys):
    from agora import cli
    monkeypatch.delenv("AGORA_AGY_CONTINUE", raising=False)
    before = sorted(p.name for p in agy_home.rglob("*"))
    code = cli.main(["continue", "session", "agora:01TEST00000000000000000000", "--agent", "agy",
                     "--dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert code == 1, err
    assert "備份" in err and "AGORA_AGY_CONTINUE=1" in err
    assert sorted(p.name for p in agy_home.rglob("*")) == before

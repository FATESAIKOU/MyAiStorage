import _guard

import fcntl
import json
from pathlib import Path
import sys

import pytest

from agora import store

FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    return remote


def calls(remote: Path) -> list[list[str]]:
    log = remote.parent / "calls.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_folder_id_lock_covers_mkdir_and_write(remote, monkeypatch):
    """T-1: Verify config.json.lock is held during both mkdir and write_atomic (C-3)."""
    paths = store.Paths.from_env()
    lock_file = paths.config / "config.json.lock"

    mkdir_probed = False
    write_probed = False

    orig_run = store.Drive._run
    orig_write = store.write_atomic

    def is_locked(path: Path) -> bool:
        if not path.exists():
            return False
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            else:
                fcntl.flock(f, fcntl.LOCK_UN)
                return False

    def patched_run(self, *args, **kwargs):
        nonlocal mkdir_probed
        if args and args[0] == "mkdir":
            assert is_locked(lock_file), "config.json.lock must be held during mkdir"
            mkdir_probed = True
        return orig_run(self, *args, **kwargs)

    def patched_write(path, text):
        nonlocal write_probed
        if Path(path) == paths.config / "config.json":
            assert is_locked(lock_file), "config.json.lock must be held during write_atomic"
            write_probed = True
        return orig_write(path, text)

    monkeypatch.setattr(store.Drive, "_run", patched_run)
    monkeypatch.setattr(store, "write_atomic", patched_write)

    folder_id = store.Drive(paths).folder_id()
    assert folder_id
    assert mkdir_probed, "Drive._run('mkdir') was not probed"
    assert write_probed, "store.write_atomic was not probed"


def test_folder_id_rereads_config_under_lock(remote):
    """T-2: In-lock re-read adopts newly created folder ID without extra Drive calls (C-4)."""
    paths = store.Paths.from_env()
    a = store.Drive(paths)
    assert a.settings == {}

    b = store.Drive(paths)
    b_id = b.folder_id()
    assert b_id

    before = len(calls(remote))
    a_id = a.folder_id()

    assert a_id == b_id
    assert a.settings.get("folders", {}).get("agora") == b_id
    assert len(calls(remote)) == before


def test_folder_id_merges_without_lost_update(remote, monkeypatch):
    """T-3: Write-back preserves folder IDs created under different names (C-5)."""
    paths = store.Paths.from_env()
    a = store.Drive(paths)
    assert a.settings == {}

    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    test_id = store.Drive(paths).folder_id()
    assert test_id

    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora")
    agora_id = a.folder_id()
    assert agora_id

    data = json.loads((paths.config / "config.json").read_text())
    assert data.get("folders") == {"agora-test": test_id, "agora": agora_id}


def test_folder_id_cache_hit_does_not_create_lock(remote):
    """T-4: Cache hit returns immediately without creating config.json.lock (C-2)."""
    paths = store.Paths.from_env()
    lock_file = paths.config / "config.json.lock"

    # Pre-populated config.json cache hit
    paths.config.mkdir(parents=True, exist_ok=True)
    store.write_atomic(
        paths.config / "config.json",
        json.dumps({"folders": {"agora": "cached-folder-id"}}, indent=2),
    )

    d = store.Drive(paths)
    assert d.folder_id() == "cached-folder-id"
    assert not lock_file.exists(), "config.json.lock must not exist on pre-cached folder_id call"

    # In-memory cache hit
    lock_file.touch()
    assert lock_file.exists()
    lock_file.unlink()
    assert not lock_file.exists()

    assert d.folder_id() == "cached-folder-id"
    assert not lock_file.exists(), "config.json.lock must not be recreated on in-memory cache hit"

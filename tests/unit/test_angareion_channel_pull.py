"""Unit tests for the config pull policy (v5 section 1.3).

`git fetch` + FETCH_HEAD freshness, the 20s timeout, the flock, the
event-driven fetch on a lookup miss and the landing poll of `channel create`.
All through the injected git runner and clocks.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import threading
import time

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion.channel_config import (
    BACKENDS_PATH,
    CHANNELS_PATH,
    GIT_TIMEOUT_SECONDS,
    LANDING_POLL_INTERVAL_SECONDS,
    LANDING_POLL_TIMEOUT_SECONDS,
    MIN_EVENT_FETCH_INTERVAL_SECONDS,
    ChannelStore,
    FileLock,
    GitError,
)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _store(tmp_path, files=None, *, mtime=1000.0, now=1000.0, interval=900, lock=None, warnings=None):
    git = FakeGit(files, fetch_head_mtime=mtime)
    clock = FakeClock(now)
    store = ChannelStore(
        tmp_path,
        git=git,
        interval=interval,
        warn=(warnings if warnings is not None else []).append,
        now=lambda: now,
        clock=lambda: clock.now,
        sleeper=clock.sleep,
        fetch_head_mtime=lambda: mtime,
        lock=lock,
    )
    return store, git, clock


def test_ensure_fresh_fetches_when_never_fetched(tmp_path):
    store, git, _ = _store(tmp_path, mtime=None)
    store.ensure_fresh()
    assert git.fetch_calls == 1
    # The clone is refreshed with a fixed 20-second timeout (design section 11).
    assert git.fetch_timeouts == [GIT_TIMEOUT_SECONDS]
    assert GIT_TIMEOUT_SECONDS == 20.0


def test_ensure_fresh_fetches_only_when_the_copy_is_stale(tmp_path):
    store, git, _ = _store(tmp_path, mtime=1000.0, now=1000.0)
    store.ensure_fresh()
    assert git.fetch_calls == 0  # fresh enough

    store, git, _ = _store(tmp_path, mtime=0.0, now=901.0)
    store.ensure_fresh()
    assert git.fetch_calls == 1  # older than the 900s interval

    store, git, _ = _store(tmp_path, mtime=0.0, now=899.0)
    store.ensure_fresh()
    assert git.fetch_calls == 0


def test_interval_zero_disables_the_periodic_pull_but_not_the_event_fetch(tmp_path):
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({}),
        f"origin/main:{BACKENDS_PATH}": backends_json({}),
    }
    store, git, _ = _store(tmp_path, files, mtime=None, interval=0)
    store.ensure_fresh()
    assert git.fetch_calls == 0  # no periodic pull

    # A lookup miss still asks for fresher data.
    store.resolve("nobody")
    assert git.fetch_calls == 1


def test_event_fetch_skips_when_the_copy_is_only_seconds_old(tmp_path):
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({}),
        f"origin/main:{BACKENDS_PATH}": backends_json({}),
    }
    store, git, _ = _store(tmp_path, files, mtime=1000.0, now=1000.0 + MIN_EVENT_FETCH_INTERVAL_SECONDS - 1)
    assert store.resolve("nobody") is None
    assert git.fetch_calls == 0

    # At the 30-second boundary it fetches again.
    store, git, _ = _store(tmp_path, files, mtime=1000.0, now=1000.0 + MIN_EVENT_FETCH_INTERVAL_SECONDS)
    store.resolve("nobody")
    assert git.fetch_calls == 1


def test_lookup_miss_fetches_and_then_finds_the_channel(tmp_path):
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({}),
        f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
    }
    store, git, _ = _store(tmp_path, files, mtime=0.0, now=1000.0)

    # The mapping only exists after the fetch: another teammate just created it.
    git.on_fetch = lambda: git.set_file(
        CHANNELS_PATH, channels_json({"fresh": ("github-main", 42)})
    )
    entity = store.entity_for("fresh")
    assert git.fetch_calls == 1
    assert (entity.channel, entity.backend, entity.repo, entity.issue) == (
        "fresh",
        "github-main",
        "owner/repo",
        42,
    )


def test_fetch_failure_warns_and_keeps_the_current_copy(tmp_path):
    warnings: list[str] = []
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({"general": ("github-main", 1)}),
        f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
    }
    git = FakeGit(files, raise_on_fetch=GitError("git fetch timed out after 20s"))
    store = ChannelStore(tmp_path, git=git, interval=900, warn=warnings.append, fetch_head_mtime=lambda: None)

    store.ensure_fresh()  # does not raise
    assert git.fetch_calls == 1
    assert len(warnings) == 1 and "timed out" in warnings[0]
    # The command keeps working off the existing copy.
    assert store.resolve("general").issue == 1


def test_entities_lists_every_channel(tmp_path):
    store, _, _ = _store(
        tmp_path,
        {
            f"origin/main:{CHANNELS_PATH}": channels_json({"general": ("github-main", 1), "dev": ("github-2", 7)}),
            f"origin/main:{BACKENDS_PATH}": backends_json(
                {"github-main": ("github", "owner/repo"), "github-2": ("github", "owner/repo2")}
            ),
        },
    )
    entities = store.entities()
    assert sorted((e.channel, e.repo, e.issue) for e in entities) == [
        ("dev", "owner/repo2", 7),
        ("general", "owner/repo", 1),
    ]


def test_poll_for_channel_waits_for_the_mapping_to_land(tmp_path):
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({}),
        f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
    }
    store, git, clock = _store(tmp_path, files)

    def land() -> None:
        if git.fetch_calls >= 3:  # the workflow's commit appears on the third fetch
            git.set_file(CHANNELS_PATH, channels_json({"fresh": ("github-main", 42)}))

    git.on_fetch = land
    assert store.poll_for_channel("fresh") is True
    assert git.fetch_calls == 3
    # The landing wait sleeps in bounded steps, and those bounds come from the clock.
    assert clock.sleeps == [LANDING_POLL_INTERVAL_SECONDS, LANDING_POLL_INTERVAL_SECONDS]


def test_poll_for_channel_gives_up_at_the_two_minute_limit(tmp_path):
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json({}),
        f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")}),
    }
    store, git, clock = _store(tmp_path, files)
    assert store.poll_for_channel("never") is False
    assert clock.now == 1000.0 + LANDING_POLL_TIMEOUT_SECONDS
    assert sum(clock.sleeps) == LANDING_POLL_TIMEOUT_SECONDS
    assert LANDING_POLL_TIMEOUT_SECONDS == 120.0
    # One fetch per wake-up, no more: 120s / 5s = 24 sleeps, 25 fetches.
    assert git.fetch_calls == int(LANDING_POLL_TIMEOUT_SECONDS / LANDING_POLL_INTERVAL_SECONDS) + 1


def test_fetch_is_serialized_with_a_lock(tmp_path):
    events: list[str] = []

    class RecordingLock:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *exc_info):
            events.append("exit")

    git = FakeGit()
    git.on_fetch = lambda: events.append("fetch")
    store = ChannelStore(tmp_path, git=git, warn=lambda message: None, fetch_head_mtime=lambda: None)
    store._lock = lambda: RecordingLock()  # type: ignore[assignment]

    store.fetch()
    assert events == ["enter", "fetch", "exit"]


def test_file_lock_serializes_two_holders(tmp_path):
    lock_path = tmp_path / ".git" / "angareion-pull.lock"
    order: list[str] = []

    def hold_first() -> None:
        with FileLock(lock_path):
            order.append("first-enter")
            time.sleep(0.05)
            order.append("first-exit")

    thread = threading.Thread(target=hold_first)
    thread.start()
    time.sleep(0.01)
    with FileLock(lock_path):
        order.append("second-enter")
    thread.join()

    assert order == ["first-enter", "first-exit", "second-enter"]
    assert lock_path.is_file()

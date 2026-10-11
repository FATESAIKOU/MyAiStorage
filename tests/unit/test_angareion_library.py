"""Unit tests for the library layer: wait polls check, read moves the cursor (D2/D4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

from angareion.backend import Cursor, FakeBackend, Target
from angareion.client import POLL_INTERVAL_SECONDS, Angareion
from angareion.messages import Message
from angareion.state import CursorStore

IDENTITY = {"repo": "owner-org/worker", "role": "worker"}


def _make_msg(
    channel: str = "general",
    msg_id: str = "01AAAA",
    at: str = "2026-10-10T12:00:00+00:00",
    to: dict | None = None,
) -> Message:
    return Message(
        from_={"repo": "owner-org/team", "role": "PM"},
        to=to or IDENTITY,
        channel=channel,
        content="hello",
        id=msg_id,
        at=at,
    )


class CountingBackend(FakeBackend):
    """FakeBackend that counts check calls and records the timeout budgets."""

    def __init__(self):
        super().__init__()
        self.check_calls = 0
        self.check_timeouts: list[float | None] = []

    def check(self, target, cursor, *, timeout=None):
        self.check_calls += 1
        self.check_timeouts.append(timeout)
        return super().check(target, cursor, timeout=timeout)


def _app(tmp_path, backend=None):
    store = CursorStore(tmp_path, IDENTITY)
    return Angareion(backend or FakeBackend(), store)


def test_wait_timeout_zero_checks_once_and_returns_false_when_empty(tmp_path):
    backend = CountingBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    def forbidden(*_args):
        raise AssertionError("timeout 0 must neither sleep nor consult the clock")

    assert app.wait(Target(channel="general"), 0, sleeper=forbidden, clock=forbidden) is False
    assert backend.check_calls == 1
    # Non-blocking: one check with the backend's own default timeout (30s), not a tiny one.
    assert backend.check_timeouts == [None]


def test_wait_caps_each_network_budget_at_thirty_seconds(tmp_path):
    """A long --timeout must not hand the backend an hour-long request budget (W1)."""
    from angareion.client import CHECK_TIMEOUT_CAP_SECONDS, CHECK_TIMEOUT_FLOOR_SECONDS

    backend = CountingBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    assert app.wait(
        Target(channel="general"),
        600,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    ) is False
    assert CHECK_TIMEOUT_CAP_SECONDS == 30.0
    assert CHECK_TIMEOUT_FLOOR_SECONDS == 5.0
    assert backend.check_timeouts[0] == 30.0
    assert all(budget is None or budget <= 30.0 for budget in backend.check_timeouts)


def test_wait_gives_a_short_remaining_time_five_seconds(tmp_path):
    backend = CountingBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    assert app.wait(
        Target(channel="general"),
        2.0,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    ) is False
    assert backend.check_timeouts == [5.0, 5.0]  # remaining 2s and 0s both floor at 5
    assert sleeps == [2.0]


def test_wait_timeout_zero_returns_true_with_new_message(tmp_path):
    backend = CountingBackend()
    backend.create_channel("general", [])
    backend.send("general", _make_msg())
    app = _app(tmp_path, backend)

    assert app.wait(Target(channel="general"), 0) is True
    assert backend.check_calls == 1


def test_wait_timeout_zero_does_not_create_a_cursor_file(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    app.wait(Target(channel="general"), 0)
    assert not app.cursors.path.exists()


def test_wait_polls_until_a_message_arrives(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) == 2:
            backend.send("general", _make_msg())

    result = app.wait(
        Target(channel="general"),
        600,
        poll_interval=15.0,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )

    assert result is True
    assert sleeps == [15.0, 15.0]
    assert not app.cursors.path.exists()  # waiting must not write the cursor


def test_wait_times_out_without_sleeping_past_the_deadline(tmp_path):
    backend = CountingBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    result = app.wait(
        Target(channel="general"),
        31.0,
        poll_interval=15.0,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )

    assert result is False
    assert clock["now"] == 1031.0  # not a second longer
    assert sleeps == [15.0, 15.0, 1.0]
    # Checks at 0s, 15s, 30s and once more at the deadline itself.
    assert backend.check_calls == 4
    # Each check's network budget is min(30s, max(5s, remaining)) (W1).
    assert backend.check_timeouts == [30.0, 16.0, 5.0, 5.0]
    assert not app.cursors.path.exists()


def test_wait_without_timeout_blocks_until_a_message_arrives(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    app = _app(tmp_path, backend)

    clock = {"now": 1000.0}
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds
        if len(sleeps) == 3:
            backend.send("general", _make_msg())

    assert app.wait(
        Target(channel="general"),
        None,
        poll_interval=15.0,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    ) is True
    assert sleeps == [15.0, 15.0, 15.0]


def test_default_poll_interval_is_fifteen_seconds():
    assert POLL_INTERVAL_SECONDS == 15.0


def test_read_uses_the_local_cursor_and_advances_it(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    first = _make_msg(msg_id="01AAAA")
    second = _make_msg(msg_id="01BBBB")
    backend.deliver("general", first, comment_id=1, created_at="2026-10-10T12:00:00Z")
    backend.deliver("general", second, comment_id=2, created_at="2026-10-10T12:00:01Z")
    app = _app(tmp_path, backend)
    target = Target(channel="general")

    batch = app.read(target)
    assert [m.id for m in batch.messages] == ["01AAAA", "01BBBB"]
    batch.commit()
    stored = json.loads(app.cursors.path.read_text(encoding="utf-8"))
    entry = stored["channel:general"]
    assert set(entry) == {"position", "at", "seen"}
    assert entry["position"] == "2"
    assert entry["at"] == "2026-10-10T12:00:01Z"
    assert entry["seen"] == ["1", "2"]

    # Nothing new the second time.
    assert app.read(target).messages == []

    # And it stays that way while nothing arrives.
    assert app.check(target) is False


def test_read_only_advances_the_cursor_when_commit_is_called(tmp_path):
    """Printing happens between read and commit: a broken stdout must not skip messages (L2)."""
    backend = FakeBackend()
    backend.create_channel("general", [])
    backend.send("general", _make_msg(msg_id="01AAAA"))
    app = _app(tmp_path, backend)
    target = Target(channel="general")

    batch = app.read(target)
    assert [m.id for m in batch.messages] == ["01AAAA"]
    assert not app.cursors.path.exists()  # not committed yet: the caller still has to print

    batch.commit()
    assert app.cursors.path.exists()


def test_read_with_since_is_inclusive_on_the_backend_time(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    backend.deliver("general", _make_msg(msg_id="01AAAA"), comment_id=1, created_at="2026-10-10T12:00:00Z")
    backend.deliver("general", _make_msg(msg_id="01BBBB"), comment_id=2, created_at="2026-10-10T12:00:01Z")
    app = _app(tmp_path, backend)
    target = Target(channel="general")

    # Inclusive: the message recorded exactly at the given time comes back.
    batch = app.read(target, since="2026-10-10T12:00:00Z")
    assert [m.id for m in batch.messages] == ["01AAAA", "01BBBB"]
    batch.commit()
    assert not app.cursors.path.exists()  # `--since` never touches the local cursor

    # A later boundary excludes the earlier message.
    assert [m.id for m in app.read(target, since="2026-10-10T12:00:01Z").messages] == ["01BBBB"]


def test_read_to_me_spans_channels_with_its_own_cursor(tmp_path):
    backend = FakeBackend()
    backend.create_channel("general", [])
    backend.create_channel("other", [])
    backend.deliver("general", _make_msg(channel="general", msg_id="01AAAA"), comment_id=1, created_at="2026-10-10T10:00:00Z")
    backend.deliver("other", _make_msg(channel="other", msg_id="01BBBB"), comment_id=2, created_at="2026-10-10T10:01:00Z")
    app = _app(tmp_path, backend)
    target = Target(recipient=IDENTITY)

    batch = app.read(target)
    assert [m.id for m in batch.messages] == ["01AAAA", "01BBBB"]
    batch.commit()
    stored = json.loads(app.cursors.path.read_text(encoding="utf-8"))
    assert set(stored) == {"to:owner-org_worker__worker"}
    assert stored["to:owner-org_worker__worker"]["position"] == "2"

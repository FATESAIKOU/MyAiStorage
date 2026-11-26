import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

"""Contract tests of design 542 PR-3b: `PageWriter.write` (the W table, C-16),
`PageWriter.ensure_successor` (the S table, C-17) and the race between two
writers (C-18), on the Fake backend and the in-memory registry. No network, no
sleeps: the races are gated with `threading.Barrier(2, timeout=5)` and
`FakeRegistryClient.before_post`. Plus the two guards the 3a review asked for:
the registry's id race and the PAGE_FILL_LIMIT boundary."""

import json
import re
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

from _angareion_fakes import StaticResolver
from angareion.backend import (
    BackendError,
    BackendRef,
    ChannelFullError,
    Cursor,
    Entity,
    RateLimitError,
    RetiredPage,
    WriteRefusedError,
    parse_backend_time,
)
from angareion.fake_backend import FakeBackend
from angareion.messages import Message
from angareion.registry import (
    Action,
    FakeRegistryClient,
    parse_comment,
    register_comment,
    rollover_comment,
)
from angareion.registry_rules import rebuild_channels
from angareion.rollover import (
    MAX_RESTARTS,
    MISMATCH,
    OPEN_FAILED,
    REASONS,
    REJECTED,
    REGISTRY_UNREADABLE,
    UNREGISTERED,
    UNVERIFIED,
    MemoryPendingPages,
    PageWriter,
    SuccessorError,
    filling_warning,
    prepare_failed_warning,
    stuck_message,
)
from angareion.state import PendingPageStore

REPO = "owner/repo"
CHANNEL = "general"
ME = "mylinuxpool-impl-w1-1"
BACKEND = BackendRef(name="github-main", type="github", repo=REPO)
OTHER_BACKEND = BackendRef(name="github-2", type="github", repo="owner/repo2")
P = 10  # the current page
R = 9  # the page retired before P
NEW = 11  # what the next open_page gets: the fake's counter is past both P and R
CLOCK = "2026-10-10T12:01:00Z"
NOW = datetime(2026, 10, 10, 12, 1, tzinfo=timezone.utc)
UNTIL = "2026-10-10T12:00:00Z"  # when R was retired (in the local config)
IN_WINDOW = "2026-10-10T13:00:00Z"  # the backend's Date: an hour after UNTIL
OUT_WINDOW = "2026-10-11T13:00:00Z"  # 25 hours after UNTIL: past the fill window


def backend_time(hours: float) -> datetime:
    return parse_backend_time(UNTIL) + timedelta(hours=hours)


def message(content: str = "hello") -> Message:
    return Message(from_=ME, to=None, channel=CHANNEL, content=content)


def local(issue: int = P, *retired: RetiredPage, backend: str = "github-main") -> Entity:
    return Entity(channel=CHANNEL, backend=backend, repo=REPO, issue=issue, retired=tuple(retired))


def retired_page(issue: int = R, until: str = UNTIL, side: bool = False) -> RetiredPage:
    return RetiredPage(issue=issue, until=until, side=side)


def pages(rig: "Rig") -> list[int]:
    """The page each message of the channel landed on, in arrival order (FakeComment.page)."""
    return [c.page[1] for c in rig.backend._comments if c.message.channel == CHANNEL]


def rollover_actions(registry: FakeRegistryClient) -> list[Action]:
    parsed = [parse_comment(c["body"]) for c in registry.all_comments()]
    return [p for p in parsed if isinstance(p, Action) and p.action == "rollover"]


def count_calls(monkeypatch, obj, name: str) -> list:
    """Wrap a method on one instance and record its calls; the original still runs."""
    calls: list = []
    original = getattr(obj, name)

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(obj, name, counted)
    return calls


def assert_bare_message(error: BaseException) -> None:
    """Failures never show an issue number or the repo (the spec's interface rule)."""
    text = str(error)
    assert not re.search(r"#\d+", text)
    assert REPO not in text


class Rig:
    """One Fake backend (current page P, retired page R), one registry and one writer, wired as `cli` wires them."""

    def __init__(
        self,
        *,
        association: str = "OWNER",
        date: str | None = IN_WINDOW,
        now: datetime = NOW,
        register: bool = True,
        current: int = P,
        refresh_registers: bool = False,
    ):
        self.now = now
        self.backend = FakeBackend(StaticResolver({}), clock=lambda: CLOCK, page_date=lambda: date)
        self.backend.add_entity(CHANNEL, number=P)
        self.backend.add_entity(CHANNEL, number=R)
        self.registry = FakeRegistryClient(post_association=association)
        if register:
            self.registry.add_comment(register_comment(CHANNEL, "github-main", current), author_association="OWNER")
        self.warnings: list[str] = []
        self.refreshes: list[str] = []
        self._refresh_registers = refresh_registers
        self._backends = lambda: {"github-main": BACKEND, "github-2": OTHER_BACKEND}
        self.pending = MemoryPendingPages()
        self.writer = self.make_writer(pending=self.pending)

    def make_writer(self, *, backend=None, registry=None, pending=None, backends=None) -> PageWriter:
        return PageWriter(
            self.backend if backend is None else backend,
            self.registry if registry is None else registry,
            pending=MemoryPendingPages() if pending is None else pending,
            backends=self._backends if backends is None else backends,
            refresh=self._refresh,
            warn=self.warnings.append,
            now=lambda: self.now,
            sleeper=lambda seconds: None,
        )

    def _refresh(self, channel: str) -> Entity:
        """The local config re-read: it may now know the channel the registry already has."""
        self.refreshes.append(channel)
        if self._refresh_registers:
            self.registry.add_comment(register_comment(CHANNEL, "github-main", P), author_association="OWNER")
        return local(P)

    def ensure(self, current: int = P, *, repost: bool = False, at: Entity | None = None) -> Entity:
        return self.writer.ensure_successor(CHANNEL, current, local(current) if at is None else at, repost=repost)


# -- C-16: PageWriter.write (the W table) -------------------------------------


def test_c16_w1_writes_the_retired_page_inside_the_fill_window():
    rig = Rig()
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    msg = message()
    assert rig.writer.write(local(P, retired_page()), msg) == msg.id
    assert pages(rig) == [R]
    assert rig.backend.size_calls == [R]
    assert rig.backend.opened == []


def test_c16_w1_a_retired_page_with_2489_messages_still_takes_writes():
    rig = Rig()
    rig.backend.set_page_size(R, 2489)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [R]


def test_c16_w1_a_retired_page_with_2490_messages_is_skipped_for_the_current_page():
    rig = Rig()
    rig.backend.set_page_size(R, 2490)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]


def test_c16_w1_a_retired_page_exactly_24h_after_until_is_outside_the_fill_window():
    rig = Rig(date="2026-10-11T12:00:00Z")  # Date - until == PAGE_FILL_WINDOW: not inside the window
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]
    assert rig.backend.size_calls == [R, P]


def test_c16_w1_only_the_last_retired_page_is_read_when_there_are_two():
    rig = Rig()
    rig.backend.add_entity(CHANNEL, number=8)
    rig.backend.set_page_size(8, 100)  # the older retired page: never looked at
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page(8), retired_page(R)), message())
    assert pages(rig) == [R]
    assert rig.backend.size_calls == [R]


def test_c16_w2_an_unreadable_backend_date_is_outside_the_window_and_raises_nothing():
    rig = Rig(date="garbage")
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    msg = message()
    assert rig.writer.write(local(P, retired_page()), msg) == msg.id
    assert pages(rig) == [P]
    assert rig.warnings == []


def test_c16_w2_a_retired_page_outside_the_fill_window_is_skipped():
    rig = Rig(date=OUT_WINDOW)
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]
    assert rig.backend.size_calls == [R, P]


def test_c16_w2_without_a_backend_date_the_retired_page_is_skipped():
    rig = Rig(date=None)
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]


def test_c16_w3_a_failed_size_read_of_the_retired_page_goes_to_the_current_page():
    rig = Rig()
    rig.backend.next_error = BackendError("temporarily unavailable")  # consumed by the first size read: R's

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]
    assert rig.backend.size_calls == [R, P]


def test_c16_w4_a_full_retired_page_falls_through_to_the_current_page():
    rig = Rig()
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)
    rig.backend.set_send_error(R, ChannelFullError(CHANNEL, 2500, presumed=False))

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]


def test_c16_w4_a_retired_page_that_refuses_the_write_falls_through_to_the_current_page():
    rig = Rig()
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)
    rig.backend.set_send_error(R, WriteRefusedError("refused", status=403))

    rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == [P]


def test_c16_w5_another_error_writing_the_retired_page_is_raised_and_nothing_is_written():
    rig = Rig()
    rig.backend.set_page_size(R, 100)
    rig.backend.set_send_error(R, RateLimitError("slow down", kind="secondary"))

    with pytest.raises(RateLimitError):
        rig.writer.write(local(P, retired_page()), message())
    assert pages(rig) == []


def test_c16_w6_a_current_page_below_the_rollover_band_takes_the_write_with_the_filling_warning():
    rig = Rig()
    rig.backend.set_page_size(P, 2399)

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert pages(rig) == [P]
    assert rig.warnings == [filling_warning(CHANNEL, 2399)]


def test_c16_w6_a_page_below_the_warn_level_gets_no_warning():
    rig = Rig()
    rig.backend.set_page_size(P, 1999)

    rig.writer.write(local(P), message())
    assert pages(rig) == [P]
    assert rig.warnings == []


def test_c16_w6_a_page_at_exactly_the_warn_level_gets_the_filling_warning():
    rig = Rig()
    rig.backend.set_page_size(P, 2000)

    rig.writer.write(local(P), message())
    assert pages(rig) == [P]
    assert rig.warnings == [filling_warning(CHANNEL, 2000)]


def test_c16_w7_a_failed_size_read_of_the_current_page_writes_it_anyway():
    rig = Rig()
    rig.backend.next_error = BackendError("temporarily unavailable")  # consumed by the only size read: P's

    rig.writer.write(local(P), message())
    assert pages(rig) == [P]
    assert rig.warnings == []


def test_c16_w8_a_failed_preparation_writes_the_current_page_with_a_warning():
    rig = Rig()
    rig.backend.set_page_size(P, 2450)
    rig.backend.next_open_error = BackendError("could not open")

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert pages(rig) == [P]
    assert rig.warnings == [
        prepare_failed_warning(CHANNEL, REASONS[OPEN_FAILED]),
        filling_warning(CHANNEL, 2450),
    ]


def test_c16_w8_a_page_at_exactly_the_rollover_level_starts_the_successor():
    rig = Rig(date=OUT_WINDOW)  # the old page is outside its window: the write goes to the new page
    rig.backend.set_page_size(P, 2400)

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert rig.backend.opened == [NEW]
    assert pages(rig) == [NEW]


def test_c16_w8_success_then_the_retired_page_inside_the_window_takes_the_write():
    rig = Rig()
    rig.backend.set_page_size(P, 2450)

    rig.writer.write(local(P), message())
    assert rig.backend.opened == [NEW]
    assert pages(rig) == [P]  # the old page is still under 2490 and inside its window
    assert rig.warnings == []


def test_c16_w9_a_full_current_page_sends_the_message_to_the_new_page():
    rig = Rig()
    rig.backend.set_page_size(P, 2500)

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert rig.backend.opened == [NEW]
    assert pages(rig) == [NEW]
    assert rig.warnings == []


def test_c16_w9_a_mismatch_refreshes_the_local_config_once_and_retries():
    rig = Rig(register=False, refresh_registers=True)
    rig.backend.set_page_size(P, 2500)

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert rig.refreshes == [CHANNEL]
    assert rig.backend.opened == [NEW]
    assert pages(rig) == [NEW]


def test_c16_w9_a_second_mismatch_after_the_refresh_is_an_error_with_the_c4_line_first():
    rig = Rig(register=False)
    rig.backend.set_page_size(P, 2500)

    with pytest.raises(SuccessorError) as info:
        rig.writer.write(local(P), message())
    assert info.value.kind == MISMATCH
    assert "is full" in str(info.value)
    assert rig.refreshes == [CHANNEL]  # the refresh happened once; no second one
    assert rig.warnings == [filling_warning(CHANNEL, 2500)]  # the C-4 line comes before the refusal
    assert rig.backend.opened == []
    assert pages(rig) == []
    assert_bare_message(info.value)


def test_c16_w8_a_mismatch_does_not_refresh_the_local_config():
    rig = Rig(register=False)
    rig.backend.set_page_size(P, 2450)

    rig.writer.write(local(P), message())
    assert rig.refreshes == []
    assert pages(rig) == [P]


def test_c16_w10_a_refused_current_page_rolls_over_like_a_full_one():
    rig = Rig()
    rig.backend.set_page_size(P, 100)
    rig.backend.set_send_error(P, ChannelFullError(CHANNEL, 100, presumed=True))

    msg = message()
    assert rig.writer.write(local(P), msg) == msg.id
    assert rig.backend.opened == [NEW]
    assert pages(rig) == [NEW]


def test_c16_w10_after_a_failed_preparation_does_not_prepare_again():
    rig = Rig()
    rig.backend.set_page_size(P, 2450)
    rig.backend.next_open_error = BackendError("could not open")  # only the first open fails
    rig.backend.set_send_error(P, ChannelFullError(CHANNEL, 2450, presumed=True))

    with pytest.raises(SuccessorError) as info:
        rig.writer.write(local(P), message())
    assert info.value.kind == OPEN_FAILED
    assert rig.backend.opened == []  # a second ensure_successor would have opened NEW
    assert rig.warnings == [
        prepare_failed_warning(CHANNEL, REASONS[OPEN_FAILED]),
        filling_warning(CHANNEL, 2450),
    ]
    assert_bare_message(info.value)


def test_c16_w11_a_refused_current_page_is_raised_without_preparing_or_falling_back():
    rig = Rig()
    rig.backend.set_send_error(P, WriteRefusedError("refused", status=403))

    with pytest.raises(WriteRefusedError):
        rig.writer.write(local(P), message())
    assert rig.backend.opened == []
    assert rig.registry.posted == []
    assert pages(rig) == []


def test_c16_w11_another_error_writing_the_current_page_is_raised_without_preparing():
    rig = Rig()
    rig.backend.set_send_error(P, RateLimitError("slow down", kind="primary"))

    with pytest.raises(RateLimitError):
        rig.writer.write(local(P), message())
    assert rig.backend.opened == []
    assert rig.registry.posted == []


def test_c16_more_than_three_restarts_is_a_backend_error():
    rig = Rig()
    for page in (P, NEW, 12, 13):
        rig.backend.set_page_size(page, 2500)  # each new page is full too, so every start-over restarts again

    with pytest.raises(BackendError, match="could not settle which page to write") as info:
        rig.writer.write(local(P), message())
    assert not isinstance(info.value, SuccessorError)
    assert MAX_RESTARTS == 3
    assert rig.backend.opened == [11, 12, 13, 14]  # three restarts succeeded; the fourth gave up
    assert pages(rig) == []
    assert_bare_message(info.value)
    assert "#" not in str(info.value)  # no issue number in any form


def test_c16_w8_successful_preparations_count_as_restarts_and_the_fourth_gives_up():
    rig = Rig()
    for page in (P, NEW, 12, 13, 14):
        rig.backend.set_page_size(page, 2495)  # in the rollover band; at 2490 the retired pages take no writes

    with pytest.raises(BackendError, match="could not settle which page to write") as info:
        rig.writer.write(local(P), message())
    assert not isinstance(info.value, SuccessorError)
    assert rig.backend.opened == [11, 12, 13, 14]
    assert pages(rig) == []


def test_c16_a_refresh_and_the_successors_after_it_all_count_as_restarts():
    rig = Rig()  # the registry has P as current and no retired page
    rig.backend.set_page_size(P, 2500)
    rig.backend.set_page_size(R, 2495)  # the stale local config still lists R: it must take no writes
    rig.backend.set_page_size(NEW, 2495)
    rig.backend.set_page_size(12, 2495)
    stale = local(P, retired_page(R))  # R is not in the registry's mapping: a mismatch, so the write refreshes

    with pytest.raises(BackendError, match="could not settle which page to write") as info:
        rig.writer.write(stale, message())
    assert not isinstance(info.value, SuccessorError)
    # refresh (restart 1), NEW (2), 12 (3), 13 (4: gives up)
    assert rig.refreshes == [CHANNEL]
    assert rig.backend.opened == [11, 12, 13]
    assert pages(rig) == []


def test_c16_i8_a_stale_local_config_writes_the_new_page_without_opening_one():
    rig = Rig(date=OUT_WINDOW)
    rig.backend.add_entity(CHANNEL, number=NEW)  # the page the registry already has
    rig.registry.add_comment(rollover_comment(CHANNEL, "github-main", NEW, P), author_association="OWNER")
    rig.backend.set_page_size(P, 2450)

    rig.writer.write(local(P), message())  # the local config still says P is current
    assert rig.backend.opened == []  # the registry already has NEW: nothing to open
    assert pages(rig) == [NEW]


def test_c16_l2_past_the_grace_the_retired_page_is_not_read_at_all():
    rig = Rig(now=backend_time(49))
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert rig.backend.size_calls == [P]
    assert pages(rig) == [P]


def test_c16_l2_inside_the_grace_the_retired_page_is_read_as_usual():
    rig = Rig(now=backend_time(47))
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert rig.backend.size_calls == [R]
    assert pages(rig) == [R]


def test_c16_l2_exactly_48h_after_until_the_retired_page_is_still_read():
    rig = Rig(now=backend_time(48))  # the grace is "now <= until + 48h": the last instant still reads it
    rig.backend.set_page_size(R, 100)
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page()), message())
    assert rig.backend.size_calls == [R]
    assert pages(rig) == [R]


def test_c16_low3_an_unreadable_until_counts_as_outside_the_window_and_raises_nothing():
    rig = Rig()
    rig.backend.set_page_size(P, 100)

    rig.writer.write(local(P, retired_page(until="2026-10-10T12:00:60Z")), message())
    assert rig.backend.size_calls == [P]
    assert pages(rig) == [P]


def test_c16_a_backend_that_is_not_a_page_backend_gets_a_plain_send():
    class SendOnly:
        def __init__(self):
            self.sent: list[tuple[Entity, Message]] = []

        def send(self, target, msg):
            self.sent.append((target, msg))
            return msg.id

    rig = Rig()
    plain = SendOnly()
    writer = rig.make_writer(backend=plain, registry=object())  # any registry call would fail: none is made
    msg = message()

    assert writer.write(local(P), msg) == msg.id
    assert [sent[0].issue for sent in plain.sent] == [P]


def test_c16_the_warning_and_stuck_texts_are_fixed():
    assert filling_warning("general", 2000) == "channel 'general' has 2000 of 2500 messages on its current page"
    assert prepare_failed_warning("general", "x") == (
        "channel 'general' is nearly full and its next page could not be prepared (x); "
        "the message was written to the current page"
    )
    assert stuck_message("general", "x") == (
        "channel 'general' is full and its next page could not be prepared (x); "
        "try again later, or run 'angareion channel rollover general'"
    )


# -- C-17: PageWriter.ensure_successor (the S table) -------------------------


def test_c17_s1_an_unreadable_registry_opens_nothing():
    rig = Rig()
    rig.registry.fail_comments_times = 1

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REGISTRY_UNREADABLE
    assert rig.backend.opened == []
    assert_bare_message(info.value)


def test_c17_s1_a_registry_without_a_listing_is_unreadable():
    class PosterOnly:
        def post_comment(self, body):
            raise AssertionError("nothing may be posted")

    rig = Rig()
    writer = rig.make_writer(registry=PosterOnly())
    with pytest.raises(SuccessorError) as info:
        writer.ensure_successor(CHANNEL, P, local(P))
    assert info.value.kind == REGISTRY_UNREADABLE
    assert rig.backend.opened == []


def test_c17_s2_a_channel_missing_from_the_registry_is_a_mismatch_and_opens_nothing():
    rig = Rig(register=False)

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == MISMATCH
    assert rig.backend.opened == []
    assert_bare_message(info.value)


def test_c17_s2_a_backend_that_differs_from_the_registry_is_a_mismatch_and_opens_nothing():
    rig = Rig()

    with pytest.raises(SuccessorError) as info:
        rig.ensure(at=local(P, backend="github-2"))
    assert info.value.kind == MISMATCH
    assert rig.backend.opened == []


def test_c17_s2_a_current_page_that_is_neither_current_nor_retired_is_a_mismatch_and_opens_nothing():
    rig = Rig(current=12)  # the registry says 12; the local config says P (10), which it never retired

    with pytest.raises(SuccessorError) as info:
        rig.ensure(P)
    assert info.value.kind == MISMATCH
    assert rig.backend.opened == []


def test_c17_s2_a_local_retired_page_the_registry_does_not_have_is_a_mismatch_and_opens_nothing():
    rig = Rig()

    with pytest.raises(SuccessorError) as info:
        rig.ensure(at=local(P, retired_page(R)))
    assert info.value.kind == MISMATCH
    assert rig.backend.opened == []


def test_c17_s2_a_failing_backend_list_is_a_mismatch_and_opens_nothing():
    rig = Rig()

    def broken():
        raise RuntimeError("no configuration")

    writer = rig.make_writer(backends=broken)
    with pytest.raises(SuccessorError) as info:
        writer.ensure_successor(CHANNEL, P, local(P))
    assert info.value.kind == MISMATCH
    assert rig.backend.opened == []


def test_c17_s3_another_writer_already_moved_the_channel_returns_the_new_page_and_drops_the_record():
    rig = Rig()
    rig.registry.add_comment(rollover_comment(CHANNEL, "github-main", NEW, P), author_association="OWNER")
    rig.pending.put(CHANNEL, "github-main", 99, P)

    result = rig.ensure()
    assert result.issue == NEW
    assert [page.issue for page in result.retired] == [P]
    assert rig.backend.opened == []
    assert rig.pending.get(CHANNEL) is None


def test_c17_s4_opens_the_next_page_registers_it_and_returns_the_channel_as_the_registry_has_it():
    rig = Rig()

    result = rig.ensure()
    assert result.issue == NEW
    assert result.backend == "github-main"
    assert [page.issue for page in result.retired] == [P]
    assert rig.backend.opened == [NEW]
    assert [a.issue for a in rollover_actions(rig.registry)] == [NEW]
    assert rig.pending.get(CHANNEL) is None


def test_c17_s4_a_recorded_page_is_used_instead_of_opening_a_new_one():
    rig = Rig()
    rig.backend.add_entity(CHANNEL, number=NEW)  # the page an earlier run opened
    rig.pending.put(CHANNEL, "github-main", NEW, P)

    result = rig.ensure()
    assert result.issue == NEW
    assert rig.backend.opened == []
    assert [a.issue for a in rollover_actions(rig.registry)] == [NEW]


@pytest.mark.parametrize("record", [("github-2", 99, P), ("github-main", 99, R)])
def test_c17_s4_a_record_for_another_backend_or_page_is_ignored_and_overwritten(record):
    rig = Rig()
    rig.pending.put(CHANNEL, *record)

    result = rig.ensure()
    assert result.issue == NEW
    assert rig.backend.opened == [NEW]


def test_c17_s5_a_failed_open_is_open_failed_and_records_nothing():
    rig = Rig()
    rig.backend.next_open_error = BackendError("could not open")

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == OPEN_FAILED
    assert rig.pending.get(CHANNEL) is None
    assert_bare_message(info.value)


def test_c17_s5_a_backend_that_cannot_open_pages_is_open_failed():
    rig = Rig()
    writer = rig.make_writer(backend=types.SimpleNamespace())  # not a PageOpener

    with pytest.raises(SuccessorError) as info:
        writer.ensure_successor(CHANNEL, P, local(P))
    assert info.value.kind == OPEN_FAILED


def test_c17_s6_a_page_that_does_not_verify_is_dropped_and_the_next_try_opens_another():
    rig = Rig()
    rig.backend.add_entity("other", number=NEW)  # a page with the wrong title does not verify
    rig.pending.put(CHANNEL, "github-main", NEW, P)

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == UNVERIFIED
    assert rig.pending.get(CHANNEL) is None
    assert_bare_message(info.value)

    result = rig.ensure()
    assert result.issue == 12
    assert rig.backend.opened == [12]


def test_c17_s6_prime_keeps_the_pending_record():
    rig = Rig()
    rig.backend.add_entity(CHANNEL, number=NEW)
    rig.pending.put(CHANNEL, "github-main", NEW, P)
    rig.backend.next_error = BackendError("connection reset")  # the verify call fails: not an answer

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == UNVERIFIED
    assert rig.pending.get(CHANNEL) == ("github-main", NEW, P)
    assert rig.registry.posted == []

    result = rig.ensure()  # the same page, verified this time
    assert result.issue == NEW
    assert rig.backend.opened == []


def test_c17_s7_a_registration_that_fails_keeps_the_page_recorded_and_empty():
    rig = Rig()
    rig.registry.fail_posts_times = 3  # all three attempts of post_with_retries

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == UNREGISTERED
    assert rig.pending.get(CHANNEL) == ("github-main", NEW, P)
    assert pages(rig) == []  # nothing is written to the new page before it is registered
    assert_bare_message(info.value)

    rig.registry.fail_posts_times = 0
    result = rig.ensure()
    assert result.issue == NEW
    assert rig.backend.opened == [NEW]


def test_c17_s8_prime_keeps_the_record_and_the_next_try_does_not_open_again():
    rig = Rig(association="NONE")  # the registry does not accept the writer's comment

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REJECTED
    assert rig.pending.get(CHANNEL) == ("github-main", NEW, P)
    assert_bare_message(info.value)

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REJECTED  # S-4′: the comment is listed already, so nothing is posted again
    assert rig.backend.opened == [NEW]
    assert rig.registry.comment_count() == 2  # the register comment and one rollover


def test_c17_s4_prime_a_listed_rollover_is_not_verified_or_posted_again(monkeypatch):
    rig = Rig(association="NONE")
    with pytest.raises(SuccessorError):
        rig.ensure()
    verified = count_calls(monkeypatch, rig.backend, "verify_channel")
    before = rig.registry.comment_count()

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REJECTED
    assert verified == []
    assert rig.registry.comment_count() == before


def test_c17_s4_prime_three_more_writes_open_nothing_and_post_nothing(monkeypatch):
    rig = Rig(association="NONE")
    rig.backend.set_page_size(P, 2450)
    rig.writer.write(local(P), message("first"))  # the one rollover this identity posts
    verified = count_calls(monkeypatch, rig.backend, "verify_channel")
    before = rig.registry.comment_count()
    rig.warnings.clear()

    for content in ("second", "third", "fourth"):
        rig.writer.write(local(P), message(content))

    assert rig.backend.opened == [NEW]
    assert verified == []
    assert rig.registry.comment_count() == before
    assert rig.warnings == [
        warning
        for _ in range(3)
        for warning in (
            prepare_failed_warning(CHANNEL, REASONS[REJECTED]),
            filling_warning(CHANNEL, 2450),
        )
    ]
    assert pages(rig) == [P, P, P, P]


def test_c17_s4_prime_matches_a_hand_written_body_with_other_key_order_and_spacing(monkeypatch):
    rig = Rig()
    handwritten = json.dumps(
        {"from": P, "issue": NEW, "backend": "github-main", "channel": CHANNEL, "action": "rollover", "schema": 1},
        separators=(",", ":"),
    )
    rig.registry.add_comment(handwritten, author_association="NONE")
    rig.backend.add_entity(CHANNEL, number=NEW)
    rig.pending.put(CHANNEL, "github-main", NEW, P)
    verified = count_calls(monkeypatch, rig.backend, "verify_channel")
    before = rig.registry.comment_count()

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REJECTED
    assert verified == []
    assert rig.registry.comment_count() == before


def test_c17_s4_prime_a_listed_rollover_from_another_page_does_not_count(monkeypatch):
    rig = Rig(association="NONE")
    rig.registry.add_comment(rollover_comment(CHANNEL, "github-main", NEW, R), author_association="NONE")
    rig.backend.add_entity(CHANNEL, number=NEW)
    rig.pending.put(CHANNEL, "github-main", NEW, P)
    verified = count_calls(monkeypatch, rig.backend, "verify_channel")
    before = rig.registry.comment_count()

    with pytest.raises(SuccessorError) as info:
        rig.ensure()
    assert info.value.kind == REJECTED  # not authorized, so the registry ignores it: the write is still refused
    assert len(verified) == 1
    assert rig.registry.comment_count() == before + 1


def test_c17_s4_prime_repost_verifies_and_posts_again_even_when_listed(monkeypatch):
    rig = Rig(association="NONE")
    with pytest.raises(SuccessorError):
        rig.ensure()
    verified = count_calls(monkeypatch, rig.backend, "verify_channel")
    before = rig.registry.comment_count()

    with pytest.raises(SuccessorError) as info:
        rig.ensure(repost=True)
    assert info.value.kind == REJECTED  # still not authorized
    assert len(verified) == 1
    assert rig.registry.comment_count() == before + 1


def test_c17_s4_prime_repost_after_the_cause_is_fixed_accepts_the_recorded_page():
    rig = Rig(association="NONE")
    with pytest.raises(SuccessorError):
        rig.ensure()

    rig.registry.post_association = "OWNER"  # the writer's identity is fixed; the old comment stays unauthorized
    result = rig.ensure(repost=True)
    assert result.issue == NEW
    assert rig.pending.get(CHANNEL) is None


def test_c17_s7_then_recovery_opens_one_page_and_posts_it_once(monkeypatch):
    rig = Rig()
    rig.backend.set_page_size(P, 2450)
    rig.registry.fail_posts_times = 9  # three writes, three attempts each, all refused
    for content in ("one", "two", "three"):
        rig.writer.write(local(P), message(content))  # each one: registration fails, the write goes to P

    assert rig.backend.opened == [NEW]
    assert pages(rig) == [P, P, P]
    assert rig.warnings.count(prepare_failed_warning(CHANNEL, REASONS[UNREGISTERED])) == 3

    rig.registry.fail_posts_times = 0
    result = rig.ensure()
    assert result.issue == NEW
    assert rig.backend.opened == [NEW]  # the same page, not a new one
    assert [a.issue for a in rollover_actions(rig.registry)] == [NEW]
    assert rig.pending.get(CHANNEL) is None


def test_c17_s8_the_listing_after_posting_decides_and_an_unlisted_own_comment_is_merged_back():
    rig = Rig()

    def hide_own_comment(body):
        rig.registry.hidden_ids.add(rig.registry.comment_count() + 1)  # the id this post will get
        rig.registry.before_post = None

    rig.registry.before_post = hide_own_comment
    result = rig.ensure()
    assert result.issue == NEW
    assert rig.pending.get(CHANNEL) is None


def test_c17_s8_when_another_rollover_won_the_channel_it_returns_the_winner():
    rig = Rig()

    def competitor(body):
        rig.registry.add_comment(rollover_comment(CHANNEL, "github-main", 12, P), author_association="OWNER")
        rig.registry.before_post = None

    rig.registry.before_post = competitor
    result = rig.ensure()
    assert result.issue == 12  # ours is the side page now
    assert rig.backend.opened == [NEW]
    assert pages(rig) == []
    assert rig.pending.get(CHANNEL) is None


def test_c17_s8_a_registration_reply_without_an_id_is_read_back_from_the_listing(monkeypatch):
    rig = Rig()
    post = rig.registry.post_comment

    def reply_without_id(body):
        post(body)
        return {"body": body}  # the reply has no id: the listing is the only proof of the registration

    monkeypatch.setattr(rig.registry, "post_comment", reply_without_id)
    result = rig.ensure()
    assert result.issue == NEW
    assert [a.issue for a in rollover_actions(rig.registry)] == [NEW]
    assert rig.pending.get(CHANNEL) is None


def test_c17_s9_an_unreadable_listing_after_the_post_uses_the_own_comment():
    rig = Rig()

    def unreadable_after_post(body):
        rig.registry.fail_comments_times = 1  # the listing S-8 makes fails once
        rig.registry.before_post = None

    rig.registry.before_post = unreadable_after_post
    result = rig.ensure()
    assert result.issue == NEW
    assert rig.pending.get(CHANNEL) is None


def test_c17_a_corrupt_record_file_reads_as_no_record(tmp_path):
    path = tmp_path / "pending-pages.json"
    path.write_text("{not json", encoding="utf-8")
    rig = Rig()
    writer = rig.make_writer(pending=PendingPageStore(path=path))

    result = writer.ensure_successor(CHANNEL, P, local(P))
    assert result.issue == NEW
    assert rig.backend.opened == [NEW]


def test_c17_the_reasons_are_fixed_strings_without_numbers():
    for kind, reason in REASONS.items():
        error = SuccessorError(kind)
        assert error.kind == kind
        assert error.reason == reason
        assert str(error) == reason
        assert_bare_message(error)


# -- C-18: two writers, a gate between their registrations -----------------------


def entity_of(mapping: dict) -> Entity:
    return Entity(
        channel=CHANNEL,
        backend="github-main",
        repo=REPO,
        issue=mapping["issue"],
        retired=tuple(
            RetiredPage(issue=p["issue"], until=p["until"], side=bool(p.get("side", False)))
            for p in mapping.get("retired", [])
        ),
    )


def read_once(rig: Rig, channel_entity: Entity) -> list[str]:
    """The ids a reader gets from a fresh cursor (what Angareion.read's backend call returns)."""
    return [m.id for m in rig.backend.read(channel_entity, Cursor()).messages]


def run_writers(jobs) -> tuple[list[BaseException], dict[int, str], list[bool]]:
    errors: list[BaseException] = []
    results: dict[int, str] = {}

    def run(index, writer, msg):
        try:
            results[index] = writer.write(local(P), msg)
        except BaseException as error:  # collected here, asserted in the main thread
            errors.append(error)

    threads = [threading.Thread(target=run, args=(i, w, m)) for i, (w, m) in enumerate(jobs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return errors, results, [thread.is_alive() for thread in threads]


def test_c18_two_writers_that_open_pages_at_once_register_one_and_side_the_other():
    rig = Rig()
    rig.backend.set_page_size(P, 2495)  # at 2490 and up the old page takes no writes, so both land on the winner's page
    other = rig.make_writer()
    barrier = threading.Barrier(2, timeout=5)
    rig.registry.before_post = lambda body: barrier.wait()  # both have checked and opened; both post together
    try:
        errors, results, alive = run_writers([(rig.writer, message("from a")), (other, message("from b"))])
    finally:
        rig.registry.before_post = None

    assert errors == []
    assert alive == [False, False]
    assert len(rig.backend.opened) == 2
    assert [a.from_issue for a in rollover_actions(rig.registry)] == [P, P]

    mapping = rebuild_channels(rig.registry.all_comments(), {"github-main"})[0][CHANNEL]
    sides = [p["issue"] for p in mapping["retired"] if p.get("side")]
    retired = [p["issue"] for p in mapping["retired"] if not p.get("side")]
    assert len(sides) == 1
    assert retired == [P]
    assert sides[0] != mapping["issue"]
    assert sides[0] not in pages(rig)  # the side page is empty

    ids = read_once(rig, entity_of(mapping))
    assert sorted(ids) == sorted(results.values())
    assert len(ids) == 2


def test_c18_a_writer_that_starts_after_the_rollover_opens_no_page():
    rig = Rig()
    rig.backend.set_page_size(P, 2450)
    first = message("from a")
    assert rig.writer.write(local(P), first) == first.id

    other = rig.make_writer()
    second = message("from b")
    assert other.write(local(P), second) == second.id
    assert rig.backend.opened == [NEW]


def test_c18_a_writer_that_cannot_see_the_rollover_opens_a_side_page_and_readers_get_both_once():
    rig = Rig()
    rig.backend.set_page_size(P, 2495)  # inside the W-8 band, and at least 2490: the old page takes no more writes
    first = message("from a")
    assert rig.writer.write(local(P), first) == first.id
    assert pages(rig) == [NEW]

    a_rollover_ids = [
        c["id"]
        for c in rig.registry.all_comments()
        if isinstance(parse_comment(c["body"]), Action) and parse_comment(c["body"]).action == "rollover"
    ]
    assert len(a_rollover_ids) == 1
    rig.registry.hidden_ids = set(a_rollover_ids)  # B's listings do not show A's registration (I-2)
    other = rig.make_writer()
    second = message("from b")
    assert other.write(local(P), second) == second.id
    assert rig.backend.opened == [NEW, 12]

    rig.registry.hidden_ids = set()
    mapping = rebuild_channels(rig.registry.all_comments(), {"github-main"})[0][CHANNEL]
    assert mapping["issue"] == NEW
    assert [p for p in mapping["retired"] if p.get("side")][0]["issue"] == 12

    reader_before = entity_of({"issue": NEW, "retired": []})  # a config that only knows the current page
    first_read = rig.backend.read(reader_before, Cursor())
    assert [m.id for m in first_read.messages] == [first.id]

    reader_after = rig.backend.read(entity_of(mapping), first_read.cursor)  # the config with the side page
    assert [m.id for m in reader_after.messages] == [second.id]
    again = rig.backend.read(entity_of(mapping), reader_after.cursor)
    assert again.messages == []


def test_c18_guard_the_fake_registry_gives_comments_posted_at_once_different_ids():
    registry = FakeRegistryClient()
    second = threading.Thread(target=lambda: registry.add_comment("second"))

    def gap():
        # Between computing an id and appending the comment. With the lock the second thread waits here
        # until this one has appended, so the join times out; without it the second thread takes the same id.
        registry.before_append = None
        second.start()
        second.join(timeout=0.5)

    registry.before_append = gap
    registry.add_comment("first")
    second.join(timeout=5)

    assert not second.is_alive()
    ids = [c["id"] for c in registry.all_comments()]
    assert len(ids) == 2
    assert len(set(ids)) == 2

"""Unit tests for the page layer of #542 PR-2a: retired pages, page cursors, Fake page storage.

Contracts C-6 (channels.json `retired`), C-9 and C-10 (the pure page functions),
C-15 (the Fake keeps comments per page) and C-23 (the `pages` part of the cursor
file). Design: docs/design-notes/542-channel-rollover.md §2.1, §6.3 (2a).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from datetime import datetime, timedelta, timezone

import pytest

from _angareion_fakes import FakeGit, StaticResolver, backends_json, entity
from angareion.backend import (
    Cursor,
    Entity,
    RetiredPage,
    advance_cursor,
    cursor_after_read,
    format_backend_time,
    page_base,
    page_entity,
    pages_after_read,
    pages_to_read,
)
from angareion.fake_backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelConfigError, ChannelRef, ChannelStore
from angareion.messages import Message
from angareion.state import CursorStore

PM = "mypmo-pm-m1"
ME = "mylinuxpool-impl-w1-1"
U = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)  # the latest rollover in the C-9 cases
AT0 = "2026-11-25T09:00:00Z"
AT_09_30 = "2026-11-25T09:30:00Z"
AT_09_45 = "2026-11-25T09:45:00Z"
AT_09_50 = "2026-11-25T09:50:00Z"
AT = "2026-11-25T10:00:00Z"


def _iso(moment: datetime) -> str:
    return format_backend_time(moment)


def _entity_with(retired: list[RetiredPage], current: int = 20) -> Entity:
    return Entity(
        channel="general", backend="github-main", repo="owner/repo", issue=current, retired=tuple(retired)
    )


def _issues(entities) -> list[int]:
    return [item.issue for item in entities]


def _msg(n: int, content: str, *, to: str | None = ME) -> Message:
    return Message(from_=PM, to=to, channel="general", content=content, id=f"01MSG{n:021d}", at=AT)


# -- C-9: which pages a read covers -------------------------------------------


def test_c9_new_cursor_reads_only_pages_retired_near_the_latest_rollover():
    old = RetiredPage(issue=11, until=_iso(U - timedelta(days=10)))
    recent = RetiredPage(issue=12, until=_iso(U - timedelta(hours=47)))
    latest = RetiredPage(issue=13, until=_iso(U), side=True)
    entity_ = _entity_with([old, recent, latest])

    assert _issues(pages_to_read(entity_, Cursor())) == [12, 13, 20]


def test_c9_new_cursor_grace_boundary_is_exclusive():
    # until + 48h == U is not "> U": that page is outside the new cursor's reading.
    edge = RetiredPage(issue=12, until=_iso(U - timedelta(hours=48)))
    latest = RetiredPage(issue=13, until=_iso(U))

    assert _issues(pages_to_read(_entity_with([edge, latest]), Cursor())) == [13, 20]


def test_c9_cursor_with_a_time_reads_retired_pages_by_its_own_grace():
    # Same channel, read by a cursor that has a time: the old page is still in its grace period.
    old = RetiredPage(issue=11, until=_iso(U - timedelta(days=11)))
    latest = RetiredPage(issue=13, until=_iso(U))
    entity_ = _entity_with([old, latest])
    cursor = Cursor(position="5", at=_iso(U - timedelta(days=10)))

    assert _issues(pages_to_read(entity_, cursor)) == [11, 13, 20]
    assert _issues(pages_to_read(entity_, Cursor())) == [13, 20]


def test_c9_cursor_time_grace_boundary_is_exclusive():
    retired = RetiredPage(issue=12, until=_iso(U))
    entity_ = _entity_with([retired])
    inside = Cursor(position="5", at=_iso(U + timedelta(hours=47, minutes=59)))
    edge = Cursor(position="5", at=_iso(U + timedelta(hours=48)))

    assert _issues(pages_to_read(entity_, inside)) == [12, 20]
    assert _issues(pages_to_read(entity_, edge)) == [20]


def test_c9_no_retired_pages_reads_only_the_current_page():
    assert _issues(pages_to_read(_entity_with([]), Cursor())) == [20]


def test_c9_each_page_is_a_single_page_entity_and_the_current_page_is_last():
    retired = RetiredPage(issue=12, until=_iso(U))
    pages = pages_to_read(_entity_with([retired]), Cursor())

    assert all(page.retired == () for page in pages)
    assert pages[-1] == page_entity(_entity_with([retired]), 20)
    assert pages[-1].issue == 20


# -- C-10: per-page cursors (the pure functions) -------------------------------


def _c(position, at=None, seen=(), pages=None) -> Cursor:
    return Cursor(position=position, at=at, seen=list(seen), pages=pages)


def test_c10_page_base_uses_the_pages_own_cursor():
    prev = _c("9", AT, ["9"], pages={5: _c("3", AT0, ["3"])})

    base = page_base(prev, 5)

    assert base == _c("3", AT0, ["3"])
    assert base.pages is None


def test_c10_page_base_for_a_page_not_in_pages_starts_empty():
    prev = _c("9", AT, ["9"], pages={5: _c("3", AT0, ["3"])})

    assert page_base(prev, 7) == Cursor()


def test_c10_page_base_without_pages_is_the_main_cursor():
    prev = _c("9", AT, ["9"])

    assert page_base(prev, 5) == _c("9", AT, ["9"])
    assert page_base(prev, 5).pages is None


def test_c10_pages_after_read_advances_a_page_that_was_read():
    prev = _c("9", AT, [], pages={5: _c("3", AT0, [])})

    result = pages_after_read(prev, [5, 7], {5: [(4, AT_09_30)]})

    assert result == {5: _c("4", AT_09_30, ["4"]), 7: Cursor()}


def test_c10_pages_after_read_keeps_a_skipped_page_where_it_was():
    prev = _c("9", AT, [], pages={5: _c("3", AT0, [])})

    assert pages_after_read(prev, [5], {}) == {5: _c("3", AT0, [])}


def test_c10_pages_after_read_skipped_page_without_pages_keeps_the_main_cursor():
    prev = _c("3", AT0, [])

    assert pages_after_read(prev, [5], {}) == {5: _c("3", AT0, [])}


def test_c10_pages_after_read_result_has_exactly_the_pages_read():
    prev = _c("9", AT, [], pages={5: _c("3", AT0, []), 6: _c("2", AT0, [])})

    assert set(pages_after_read(prev, [5], {5: [(4, AT_09_30)]})) == {5}


def test_c10_cursor_after_read_main_advances_on_the_pages_read_together():
    # The newest id is on page 5, not on the current page 20: the main cursor must follow it.
    prev = _c("3", AT0, [])

    result = cursor_after_read(prev, [5, 20], {5: [(9, AT_09_50)], 20: [(8, AT_09_45)]})

    assert result == _c(
        "9",
        AT_09_50,
        ["9"],
        pages={5: _c("9", AT_09_50, ["9"]), 20: _c("8", AT_09_45, ["8"])},
    )


def test_c10_cursor_after_read_with_no_entries_keeps_the_main_cursor():
    prev = _c("3", AT0, ["3"], pages={5: _c("3", AT0, ["3"])})

    result = cursor_after_read(prev, [5], {})

    assert result == _c("3", AT0, ["3"], pages={5: _c("3", AT0, ["3"])})
    assert advance_cursor(_c("3", AT0, ["3"]), []) == _c("3", AT0, ["3"])


# -- C-6: channels.json `retired` ----------------------------------------------


def _store_with(tmp_path, channels: dict) -> ChannelStore:
    git = FakeGit()
    git.set_file(CHANNELS_PATH, json.dumps({"channels": channels, "version": 1}))
    git.set_file(BACKENDS_PATH, backends_json({"github-main": ("github", "owner/repo")}))
    return ChannelStore(tmp_path, git=git, interval=0)


def test_c6_retired_parses_in_file_order(tmp_path):
    store = _store_with(
        tmp_path,
        {
            "general": {
                "backend": "github-main",
                "issue": 41,
                "retired": [
                    {"issue": 28, "until": "2026-11-26T01:02:03Z"},
                    {"issue": 42, "until": "2026-11-26T01:02:05+09:00", "side": True},
                ],
            }
        },
    )

    assert store.channels()["general"].retired == (
        RetiredPage(issue=28, until="2026-11-26T01:02:03Z"),
        RetiredPage(issue=42, until="2026-11-26T01:02:05+09:00", side=True),
    )


def test_c6_missing_retired_key_is_no_retired_pages(tmp_path):
    store = _store_with(tmp_path, {"general": {"backend": "github-main", "issue": 41}})

    assert store.channels()["general"] == ChannelRef(name="general", backend="github-main", issue=41)
    assert store.entity_for("general").retired == ()


def test_c6_entity_carries_the_retired_pages(tmp_path):
    store = _store_with(
        tmp_path,
        {
            "general": {
                "backend": "github-main",
                "issue": 41,
                "retired": [{"issue": 28, "until": "2026-11-26T01:02:03Z"}],
            }
        },
    )
    expected = (RetiredPage(issue=28, until="2026-11-26T01:02:03Z"),)

    assert store.entity_for("general").retired == expected
    assert store.entities()[0].retired == expected
    assert store.entity_for("general").issue == 41


@pytest.mark.parametrize(
    "retired",
    [
        {},  # not a list
        None,  # present but not a list
        ["28"],  # an entry that is not an object
        [{"issue": 0, "until": "2026-11-26T01:02:03Z"}],  # not a positive number
        [{"issue": True, "until": "2026-11-26T01:02:03Z"}],  # bool is not a number
        [{"issue": "28", "until": "2026-11-26T01:02:03Z"}],  # a string number
        [{"issue": 28}],  # no until
        [{"issue": 28, "until": "2026-11-26T01:02:03"}],  # no time zone
        [{"issue": 28, "until": 1}],  # until is not a string
        [{"issue": 28, "until": "2026-11-26T01:02:03Z", "side": False}],  # side must be true when present
        [{"issue": 28, "until": "2026-11-26T01:02:03Z", "side": "true"}],  # side is not the boolean true
        [{"issue": 41, "until": "2026-11-26T01:02:03Z"}],  # the same as the current page
        [
            {"issue": 28, "until": "2026-11-26T01:02:03Z"},
            {"issue": 28, "until": "2026-11-26T01:02:04Z", "side": True},
        ],  # repeated among the retired pages
    ],
)
def test_c6_bad_retired_shape_is_rejected(tmp_path, retired):
    store = _store_with(tmp_path, {"general": {"backend": "github-main", "issue": 41, "retired": retired}})

    with pytest.raises(ChannelConfigError):
        store.channels()


# -- C-15: the Fake keeps comments per page ----------------------------------


def test_c15_deliver_lands_on_the_entity_page_and_is_read_only_there():
    fake = FakeBackend(StaticResolver([]))
    current = entity(issue=20)
    retired_page = entity(issue=12)
    fake.deliver(current, _msg(1, "on current"))
    fake.deliver(retired_page, _msg(2, "on retired"))

    assert [m.content for m in fake.read(current, Cursor()).messages] == ["on current"]
    assert [m.content for m in fake.read(retired_page, Cursor()).messages] == ["on retired"]


def test_c15_deliver_with_page_posts_on_that_page_of_the_same_repo():
    fake = FakeBackend(StaticResolver([]))
    current = entity(issue=20)
    fake.deliver(current, _msg(1, "on current"))
    fake.deliver(current, _msg(2, "on page 12"), page=12)

    assert [m.content for m in fake.read(current, Cursor()).messages] == ["on current"]
    assert [m.content for m in fake.read(entity(issue=12), Cursor()).messages] == ["on page 12"]


def test_c15_check_page_size_and_listing_see_only_their_page():
    fake = FakeBackend(StaticResolver([]))
    current = entity(issue=20)
    empty_page = entity(issue=30)
    fake.deliver(current, _msg(1, "only here"))

    assert fake.check(current, Cursor()) is True
    assert fake.check(empty_page, Cursor()) is False
    assert fake.page_size(current) == 1
    assert fake.page_size(empty_page) == 0
    items, _, _ = fake.enumerate_conditional(empty_page)
    assert items == []


def test_c15_a_page_of_another_repo_is_a_different_page():
    fake = FakeBackend(StaticResolver([]))
    here = entity(issue=20, repo="owner/repo")
    there = entity(issue=20, repo="owner/other")
    fake.deliver(here, _msg(1, "in repo"))

    assert fake.page_size(there) == 0


# -- C-23: the `pages` part of the cursor file ------------------------------


def _cursor_file(tmp_path) -> CursorStore:
    return CursorStore(path=tmp_path / "cursor.json")


def test_c23_pages_round_trip_and_are_written_in_numeric_order(tmp_path):
    store = _cursor_file(tmp_path)
    cursor = _c("9", AT, ["9"], pages={28: _c("3", AT0, ["3"]), 5: _c(None, None, [])})

    store.save("channel:general", cursor)

    assert store.cursor("channel:general") == cursor
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(raw["channel:general"]["pages"]) == ["5", "28"]


def test_c23_no_pages_writes_no_pages_key_and_reads_back_as_none(tmp_path):
    store = _cursor_file(tmp_path)

    store.save("channel:general", _c("9", AT, ["9"]))

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert "pages" not in raw["channel:general"]
    assert store.cursor("channel:general").pages is None


def test_c23_old_file_without_pages_reads_as_none(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({"channel:general": {"position": "9", "at": AT, "seen": []}}), encoding="utf-8")

    assert CursorStore(path=path).cursor("channel:general") == _c("9", AT, [])


def test_c23_pages_that_is_not_an_object_reads_as_none(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps({"channel:general": {"position": "9", "at": AT, "seen": [], "pages": ["5"]}}),
        encoding="utf-8",
    )

    assert CursorStore(path=path).cursor("channel:general").pages is None


def test_c23_bad_page_keys_are_dropped_and_the_rest_kept(tmp_path):
    path = tmp_path / "cursor.json"
    good = {"position": "3", "at": AT0, "seen": []}
    path.write_text(
        json.dumps(
            {
                "channel:general": {
                    "position": "9",
                    "at": AT,
                    "seen": [],
                    "pages": {"x": good, "0": good, "028": good, "5": good},
                }
            }
        ),
        encoding="utf-8",
    )

    assert set(CursorStore(path=path).cursor("channel:general").pages) == {5}


def test_c23_page_value_that_is_not_an_object_takes_the_main_columns(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps(
            {"channel:general": {"position": "9", "at": AT, "seen": ["9"], "pages": {"5": 7}}}
        ),
        encoding="utf-8",
    )

    assert CursorStore(path=path).cursor("channel:general").pages == {5: _c("9", AT, ["9"])}


def test_c23_bad_seen_in_a_page_is_read_defensively(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text(
        json.dumps(
            {
                "channel:general": {
                    "position": "9",
                    "at": AT,
                    "seen": [],
                    "pages": {"5": {"position": "3", "at": AT0, "seen": "bad"}, "7": {"position": "2", "at": AT0, "seen": ["2", 4]}},
                }
            }
        ),
        encoding="utf-8",
    )

    pages = CursorStore(path=path).cursor("channel:general").pages

    assert pages == {5: _c("3", AT0, []), 7: _c("2", AT0, ["2"])}


def test_c23_save_keeps_the_pages_of_another_key(tmp_path):
    store = _cursor_file(tmp_path)
    a = _c("9", AT, ["9"], pages={5: _c("3", AT0, ["3"])})
    b = _c("4", AT0, ["4"], pages={6: _c("2", AT0, [])})
    store.save_many({"channel:a": a, "channel:b": b})

    store.save("channel:b", _c("8", AT, ["8"], pages={6: _c("7", AT, [])}))

    assert store.cursor("channel:a") == a


def test_c23_save_many_keeps_the_pages_of_another_key(tmp_path):
    store = _cursor_file(tmp_path)
    a = _c("9", AT, ["9"], pages={5: _c("3", AT0, ["3"])})
    store.save_many({"channel:a": a, "channel:b": _c("4", AT0, ["4"])})

    store.save_many({"channel:b": _c("5", AT, ["5"])})

    assert store.cursor("channel:a") == a

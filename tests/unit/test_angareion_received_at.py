import _guard

from datetime import datetime, timezone

from angareion.messages import parse_received_at, received_at_key


def test_510_c5_no_time_first_then_instants_then_unreadable_last():
    values = [
        "not a time",
        "2026-01-01T01:00:00Z",
        None,
        "2026-01-01T00:00:00",
        "another",
        "2026-01-01T09:30:00+09:00",
    ]
    ordered = sorted(values, key=received_at_key)
    # No time first; zoned times by instant (09:30+09:00 is 00:30Z, before 01:00Z);
    # unreadable or naive times last, ordered by their original text.
    assert ordered == [
        None,
        "2026-01-01T09:30:00+09:00",
        "2026-01-01T01:00:00Z",
        "2026-01-01T00:00:00",
        "another",
        "not a time",
    ]


def test_510_c5_no_time_sorts_before_any_zoned_time():
    assert received_at_key(None) < received_at_key("0001-01-01T00:00:00+00:00")


def test_510_c5_zoned_times_compare_by_instant_across_zones():
    # 09:30+09:00 is 00:30Z, which is before 01:00Z, which is before 10:00:00.5+00:00.
    early = "2026-01-01T09:30:00+09:00"
    middle = "2026-01-01T01:00:00Z"
    late = "2026-01-01T10:00:00.5+00:00"
    assert received_at_key(early) < received_at_key(middle) < received_at_key(late)


def test_510_c5_same_instant_written_differently_compares_equal():
    spellings = [
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:00+00:00",
        "2026-01-01T19:00:00+09:00",
        "2026-01-01T05:00:00-05:00",
    ]
    keys = {received_at_key(s) for s in spellings}
    assert len(keys) == 1


def test_510_c5_unreadable_and_naive_times_sort_last_by_original_text():
    zoned = received_at_key("2026-12-31T23:59:59Z")
    assert zoned < received_at_key("aaa") < received_at_key("bbb")
    assert zoned < received_at_key("2026-01-01T00:00:00")
    assert received_at_key("2026-01-01T00:00:00") < received_at_key("aaa")


def test_510_c5_extreme_offset_does_not_raise_and_orders_by_instant():
    # 0001-01-01 00:00 at +01:00 is an hour before 0001-01-01 00:00Z; converting to UTC would overflow.
    earlier = received_at_key("0001-01-01T00:00:00+01:00")
    later = received_at_key("0001-01-01T00:00:00+00:00")
    assert earlier < later


def test_510_c5_parse_received_at_reads_z_and_offsets_as_aware():
    moment = parse_received_at("2026-01-01T10:00:00Z")
    assert moment == datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert moment.tzinfo is not None
    assert parse_received_at("2026-01-01T19:00:00+09:00") == moment


def test_510_c5_parse_received_at_returns_none_for_naive_or_unreadable():
    assert parse_received_at("2026-01-01T00:00:00") is None
    assert parse_received_at("not a time") is None
    assert parse_received_at("") is None

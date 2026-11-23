"""`timeutil.received_at_key` 的直接測試（#522）。

鍵的形狀：沒有時刻的最前、讀得出的依時刻（aware datetime 直接比較，不轉 UTC）、
讀不出的最後且彼此依原字串；沒有時區的值照 `parse_ts` 的既有約定當 UTC。
"""

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

from pmo.timeutil import parse_ts, received_at_key


def test_no_time_sorts_first():
    first = received_at_key(None)
    assert first == received_at_key("")
    assert first == received_at_key("   ")
    assert first < received_at_key("0001-01-01T00:00:00+00:00")
    assert first < received_at_key("not a time")


def test_mixed_spellings_sort_by_the_moment():
    values = [
        "2026-01-01T01:00:00Z",
        "2026-01-01T10:00:00.5+00:00",
        "2026-01-01T09:30:00+09:00",  # 00:30Z
    ]
    assert sorted(values, key=received_at_key) == [
        "2026-01-01T09:30:00+09:00",
        "2026-01-01T01:00:00Z",
        "2026-01-01T10:00:00.5+00:00",
    ]


def test_the_same_moment_written_differently_has_one_key():
    spellings = [
        "2026-01-01T10:00:00Z",
        "2026-01-01T10:00:00+00:00",
        "2026-01-01T19:00:00+09:00",
        "2026-01-01T05:00:00-05:00",
    ]
    assert len({received_at_key(value) for value in spellings}) == 1


def test_a_value_without_a_zone_is_utc_like_parse_ts_reads_it():
    assert received_at_key("2026-01-01T10:00:00") == received_at_key("2026-01-01T10:00:00Z")
    assert parse_ts("2026-01-01T10:00:00").tzinfo is not None


def test_extreme_offsets_do_not_raise_and_order_by_instant():
    # 0001-01-01T00:00:00+01:00 是 edge 的前一小時；轉成 UTC 會溢位（不轉就不會）。
    earliest = received_at_key("0001-01-01T00:00:00+01:00")
    edge = received_at_key("0001-01-01T00:00:00+00:00")
    latest = received_at_key("9999-12-31T23:59:59-01:00")
    assert earliest < edge < latest


def test_unreadable_values_sort_last_and_by_their_own_text():
    zoned = received_at_key("2026-12-31T23:59:59Z")
    assert zoned < received_at_key("aaa") < received_at_key("bbb")


def test_the_key_combines_with_other_fields():
    # 回傳 tuple：同一時刻時由接在後面的欄位決定。
    first = {"received_at": "2026-01-01T19:00:00+09:00", "id": "A"}
    second = {"received_at": "2026-01-01T10:00:00Z", "id": "B"}

    def key(item: dict) -> tuple:
        return (received_at_key(item["received_at"]), item["id"])

    assert sorted([second, first], key=key) == [first, second]

"""Shared header (test-plan U-HDR)."""

from __future__ import annotations

import pytest

from agora import header as h


def _agora_header(**extra):
    base = {
        "header": 1, "entity": "agora", "type": "session", "id": "agora:01K6TEST",
        "title": "CSV 轉 Markdown 的規劃", "created_at": "2026-10-02T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z", "refs": [], "case": None,
        "note": "第一行\n---\n第三行", "tags": [],
    }
    base.update(extra)
    return base


def test_round_trip_keeps_fields_and_body_dashes():  # U-HDR-01
    hdr = _agora_header()
    body = "## user\n表格\n---\n## assistant\nok\n"
    back, back_body = h.split_document(h.dump_document(hdr, body))
    assert back == hdr
    assert back_body == body


def test_header_args_set_fields_in_order():  # U-HDR-02
    updates = h.parse_header_args([
        "title=規劃", "case=mybrain:案件/x", "refs=mybrain:a.md", "refs=foundry:b", "tags=csv",
    ])
    assert updates == {
        "title": "規劃", "case": "mybrain:案件/x",
        "refs": ["mybrain:a.md", "foundry:b"], "tags": ["csv"],
    }


def test_free_text_goes_to_note():  # U-HDR-03
    assert h.parse_header_args(["注意: 先做讀取"]) == {"note": "注意: 先做讀取"}
    assert h.parse_header_args(["一些 meta 資訊", "note=第二段"]) == {"note": "一些 meta 資訊\n第二段"}


def test_unknown_key_becomes_note_with_warning(capsys):  # N7
    assert h.parse_header_args(["x=1 先試"]) == {"note": "x=1 先試"}
    assert "note" in capsys.readouterr().err


def test_search_filter_rejects_unknown_key():  # U-HDR-04
    with pytest.raises(h.HeaderError, match="agent"):
        h.parse_search_filters(["foo=bar"])
    assert h.parse_search_filters(["agent=claude"]) == [(("source", "agent"), "claude")]


def test_unknown_version_and_fields_survive_rewrite():  # U-HDR-05
    hdr = _agora_header(header=99, zz_future={"a": 1})
    warnings = h.validate(hdr)
    assert warnings and "99" in warnings[0]
    back, _ = h.split_document(h.dump_document(hdr, ""))
    back["title"] = "新標題"
    again, _ = h.split_document(h.dump_document(back, ""))
    assert again["zz_future"] == {"a": 1}


def test_parse_ref_rules():  # U-HDR-06
    assert h.parse_ref("mybrain:a:b/c.md") == h.Ref("mybrain", "a:b/c.md")
    assert h.parse_ref("atelier:職務@v2") == h.Ref("atelier", "職務", rev="v2")
    assert h.parse_ref("foundry:x#sec") == h.Ref("foundry", "x", fragment="sec")
    with pytest.raises(h.HeaderError):
        h.parse_ref("nobody:x")


def test_ref_round_trips_through_str():
    ref = h.Ref("mybrain", "技術/動手做/AiStorage.md", rev="r1", fragment="s2")
    assert h.parse_ref(str(ref)) == ref


def test_case_must_be_a_ref():  # U-HDR-07
    with pytest.raises(h.HeaderError):
        h.validate(_agora_header(case="xyz"))
    h.validate(_agora_header(case=None))
    h.validate(_agora_header(case="mybrain:案件/x"))
    with pytest.raises(h.HeaderError):
        h.parse_header_args(["case=xyz"])


def test_id_must_carry_entity_prefix():
    with pytest.raises(h.HeaderError):
        h.validate(_agora_header(id="01K6TEST"))


def test_ulid_shape_and_order():
    a = h.new_ulid(1_700_000_000_000)
    b = h.new_ulid(1_700_000_000_001)
    assert len(a) == 26 and set(a) <= set(h._CROCKFORD)
    assert a < b

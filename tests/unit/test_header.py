"""Shared OKF header (design 3; test-plan U-HDR)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from agora import header as h


def _session(**extra):
    base = {
        "type": "Session", "title": "CSV 轉 Markdown 的規劃", "description": "第一行\n---\n第三行",
        "tags": [], "generated": {"by": "opencode/space-bunny-free", "at": "2026-10-02T00:00:00Z"},
        "sources": [{"id": "opencode:ses_a", "title": "opencode session"}],
        "id": "agora:01K6TEST", "refs": [], "case": None,
        "agora": {"header": 2, "created_at": "2026-10-02T00:00:00Z", "relation": "import", "parents": []},
    }
    base.update(extra)
    return base


def test_round_trip_keeps_fields_and_body_dashes():  # U-HDR-01
    hdr = _session()
    body = "## user\n表格\n---\n## assistant\nok\n"
    back, back_body = h.split_document(h.dump_document(hdr, body))
    assert back == hdr and back_body == body


def test_header_args_use_dotted_keys_and_yaml_values():  # 3.5
    updates = h.parse_header_args([
        "title=規劃", "tags=[csv, 表格]", "generated.by=human:fatesaikou",
        "sources=[{id: x, title: y}]", "description=把 CSV=轉換", "title=第二次",
    ])
    assert updates == {
        "title": "第二次", "tags": ["csv", "表格"], "generated": {"by": "human:fatesaikou"},
        "sources": [{"id": "x", "title": "y"}], "description": "把 CSV=轉換",
    }


def test_header_args_keep_numbers_as_written():
    assert h.parse_header_args(["title=01"])["title"] == "01"


def test_header_args_without_equals_is_an_error():
    with pytest.raises(h.HeaderError, match="key=value"):
        h.parse_header_args(["一些 meta 資訊"])


def test_overlay_merges_mappings_and_replaces_lists():
    base = {"generated": {"by": "a", "at": "t"}, "tags": ["x"], "title": "old"}
    assert h.overlay(base, {"generated": {"by": "b"}, "tags": ["y"]}) == {
        "generated": {"by": "b", "at": "t"}, "tags": ["y"], "title": "old"}


def test_header_file_then_args_same_key_wins(tmp_path):
    f = tmp_path / "h.yaml"
    f.write_text("---\ntitle: 從檔案\ntags: [a]\nstatus: draft\n---\n")
    assert h.user_updates(str(f), ["title=從參數"]) == {"title": "從參數", "tags": ["a"], "status": "draft"}


def test_single_value_for_a_list_field_becomes_a_list():
    assert h.user_updates(None, ["tags=csv"]) == {"tags": ["csv"]}


@pytest.mark.parametrize("arg", ["id=agora:x", "agora.relation=merge", "type=Note", "case=xyz", "refs=nobody:x"])
def test_user_updates_reject_system_and_bad_fields(arg):
    with pytest.raises(h.HeaderError):
        h.user_updates(None, [arg])


def test_unknown_version_and_fields_survive_rewrite():  # U-HDR-05
    hdr = _session(agora={"header": 99}, zz_future={"a": 1})
    warnings = h.validate(hdr)
    assert warnings and "99" in warnings[0]
    back, _ = h.split_document(h.dump_document(hdr, ""))
    assert back["zz_future"] == {"a": 1}


def test_validate_requires_type_and_prefixed_id():
    with pytest.raises(h.HeaderError):
        h.validate(_session(type=None))
    with pytest.raises(h.HeaderError):
        h.validate(_session(id="01K6TEST"))


def test_check_ref_rules():  # U-HDR-06
    # Nothing reads the parts back, so check_ref only has to accept or raise:
    # a locator with colons, an @rev, and a #fragment all pass.
    for good in ("mybrain:a:b/c.md", "atelier:職務@v2", "foundry:x#sec",
                 "agora:01K6X", "mybrain:案件/檔名.md#某段"):
        assert h.check_ref(good) is None
    for bad in ("nobody:x", "no-colon", "mybrain:", "mybrain:", 42):
        with pytest.raises(h.HeaderError):
            h.check_ref(bad)


def test_case_must_be_a_ref():  # U-HDR-07
    with pytest.raises(h.HeaderError):
        h.validate(_session(case="xyz"))
    h.validate(_session(case="mybrain:案件/x"))


def test_unknown_ref_entity_only_warns_when_not_strict():  # R7
    assert h.validate(_session(refs=["future:x"]), strict_refs=False)


def test_filters_equal_contains_and_aliases():  # 5.1
    assert h.parse_filters(["text~=表格", "agent=claude", "tags=csv", "generated.by~=opencode"]) == [
        (("text",), "~=", "表格"),
        (("agora", "source", "agent"), "=", "claude"),
        (("tags",), "=", "csv"),
        (("generated", "by"), "~=", "opencode"),
    ]


def test_filter_needs_an_operator_and_text_needs_contains():
    with pytest.raises(h.HeaderError):
        h.parse_filters(["表格"])
    with pytest.raises(h.HeaderError, match="text~="):
        h.parse_filters(["text=表格"])


def test_ulid_shape_and_order():
    a = h.new_ulid(1_700_000_000_000)
    b = h.new_ulid(1_700_000_000_001)
    assert len(a) == 26 and set(a) <= set(h._CROCKFORD)
    assert a < b

"""Smoke tests for converters module (OpencodeConverter and registry)."""

import json
from pathlib import Path
import pytest

from aistorage.converters import CONVERTERS, Converter, SessionFacts, get_converter
from aistorage.converters.opencode import OpencodeConverter
from aistorage.reading import validate_reading

DATA_DIR = Path(__file__).parent / "data" / "converters" / "opencode"


def test_converter_registry_smoke():
    conv = get_converter("opencode")
    assert isinstance(conv, Converter)
    assert isinstance(conv, OpencodeConverter)
    assert conv.source == "opencode"

    with pytest.raises(KeyError, match="未註冊"):
        get_converter("nonexistent_source")


def test_opencode_converter_basic_golden_sample():
    raw_path = DATA_DIR / "basic.json"
    expected_path = DATA_DIR / "basic.reading.json"

    assert raw_path.is_file(), f"找不到黃金樣本輸入: {raw_path}"
    assert expected_path.is_file(), f"找不到黃金樣本預期輸出: {expected_path}"

    conv = get_converter("opencode")

    # 1. 測試 facts
    facts = conv.facts(raw_path)
    assert isinstance(facts, SessionFacts)
    assert facts.title == "opencode 轉換器黃金測試樣本"
    assert facts.created_at == "2026-09-26T10:53:20Z"
    assert facts.updated_at == "2026-09-26T11:01:40Z"
    assert facts.archived_at == "2026-09-26T11:03:20Z"
    assert facts.last_message_at == "2026-09-26T10:58:20Z"
    assert facts.in_progress is False
    assert facts.message_ids == ("msg_001", "msg_002", "msg_003", "msg_004")

    # 2. 測試 child_session_ids
    children = conv.child_session_ids(raw_path)
    assert children == ("opencode:ses_sub_001",)

    # 3. 測試 convert
    converted = conv.convert(
        raw_path,
        session_id="opencode:ses_opencode_basic_001",
        snapshot_sha256="4a7d1ed414474e4033ac29ccb8653d9b110e11894d7c0f16599b4f6cf4c93540",
        parent_id=None,
    )

    # 驗證閱讀版結構符合規範
    errs = validate_reading(converted)
    assert errs == []

    # 4. 比對與預期黃金輸出完全一致
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    assert converted == expected

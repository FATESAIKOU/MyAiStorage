"""讀取視圖 model 與 schema 的冒煙測試（實作方撰寫；驗收由測試方另寫）。

範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import json

import pytest

from aistorage.errors import MismatchError
from aistorage.readview.model import (
    ELEMENT_AGORA,
    MANIFEST_FILE_NAME,
    READVIEW_FORMAT,
    FileRef,
    Manifest,
    ReadingRef,
    initial_manifest,
    next_manifest,
    parse_manifest,
    serialize_manifest,
    trusted_ids,
)
from aistorage.readview.naming import index_name, manifest_name, reading_name

T0 = "2026-09-27T08:00:00.000Z"


def _ref(tag: str) -> FileRef:
    """以十六進位字元組出假 sha256。"""
    digit = {"i": "1", "r": "2", "o": "3", "g": "4"}[tag[0]]
    return FileRef(id=f"f-{tag}", sha256=digit * 64, size=10)


def _manifest(**kw) -> Manifest:
    base = dict(
        format=READVIEW_FORMAT,
        element=ELEMENT_AGORA,
        generation=3,
        published_at=T0,
        agora_main_sha="main-sha-abc",
        converter_versions={"opencode": "1"},
        index=_ref("i"),
        files=("f-i", "f-r1"),
        retired=(("f-old", 2),),
    )
    base.update(kw)
    return Manifest(**base)  # type: ignore[arg-type]


def test_initial_manifest_is_valid_and_recognised():
    m = initial_manifest(published_at=T0)
    assert m.is_initial and m.generation == 0 and m.index is None
    assert parse_manifest(serialize_manifest(m)) == m


def test_serialize_is_deterministic_and_roundtrips():
    m = _manifest()
    b1 = serialize_manifest(m)
    b2 = serialize_manifest(_manifest())
    assert b1 == b2
    assert b1.endswith(b"\n")
    assert json.loads(b1)["generation"] == 3
    assert parse_manifest(b1) == m


def test_next_manifest_retires_then_deletes_one_generation_later():
    prev = _manifest(generation=4, retired=(("f-gone", 2),))
    nxt = next_manifest(
        prev,
        index=_ref("i2"),
        files=("f-i2",),
        reading_refs=(),
        retire_now=("f-old",),
        delete_now=("f-gone",),  # 退役世代 2 < 新世代 5 − 1
        published_at=T0,
        agora_main_sha="main-sha-def",
        converter_versions={"opencode": "1"},
    )
    assert nxt.generation == 5
    assert ("f-old", 5) in nxt.retired
    assert "f-gone" not in {fid for fid, _ in nxt.retired}
    assert parse_manifest(serialize_manifest(nxt)) == nxt


def test_trusted_ids_covers_manifest_files_pending_retired():
    m = _manifest(
        pending=(
            ReadingRef("opencode:ses_1", "a" * 64, "f-pending", "b" * 64, 1, True),
        )
    )
    ids = trusted_ids(m, "manifest-file-id")
    assert ids == frozenset(
        {"manifest-file-id", "f-i", "f-r1", "f-old", "f-pending"}
    )


def test_trusted_ids_is_exactly_the_sweep_trusted_set():
    """清扫只認 manifest：不在集合內的檔案一律隔離。"""
    m = _manifest()
    assert trusted_ids(m, "manifest-file-id") == frozenset(
        {"manifest-file-id", "f-i", "f-r1", "f-old"}
    )


def test_file_ref_from_drive_duck_typing():
    class F:
        id = "f-1"
        sha256 = "C" * 64
        size = 42

    r = FileRef.from_drive(F())
    assert r == FileRef(id="f-1", sha256="c" * 64, size=42)


def test_reading_ref_key_and_file_ref():
    r = ReadingRef("opencode:ses_1", "A" * 64, "f-1", "b" * 64, 3, False)
    assert r.key == ("opencode:ses_1", "a" * 64)
    assert r.file_ref == FileRef(id="f-1", sha256="b" * 64, size=3)
    assert ReadingRef.from_dict(r.to_dict()) == r


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="aistorage.readview/v2"),
        lambda d: d.update(generation=-1),
        lambda d: d.update(index={"id": "x", "sha256": "zz", "size": 1}),
        lambda d: d.update(published_at="2026-09-27 08:00:00"),
        lambda d: d.update(agora_main_sha=""),
        lambda d: d.update(retired=[["only-one"]]),
        lambda d: d.update(unexpected_field=1),
    ],
)
def test_parse_manifest_rejects_invalid(mutate):
    d = json.loads(serialize_manifest(_manifest()).decode("utf-8"))
    mutate(d)
    with pytest.raises(MismatchError):
        parse_manifest(json.dumps(d).encode("utf-8"))


def test_parse_manifest_rejects_non_json():
    with pytest.raises(MismatchError):
        parse_manifest(b"not json at all")


def test_naming_is_content_addressed_and_informational():
    assert manifest_name() == MANIFEST_FILE_NAME
    n1 = reading_name("opencode:ses_1", "a" * 64)
    assert n1 == reading_name("opencode:ses_1", "a" * 64)
    assert n1 != reading_name("opencode:ses_2", "a" * 64)
    assert n1.startswith("reading-") and n1.endswith(".json")
    assert index_name(7, "c" * 64).startswith("index-g7-")
    with pytest.raises(ValueError):
        reading_name("", "a" * 64)

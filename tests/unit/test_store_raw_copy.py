"""The raw copy in `store.remember` is a byte copy, not a text round trip (B1b-1).

`remember` used to do `write_atomic(target, wanted.read_text())`: the raw was already
on disk as bytes, and that held a whole second copy of it as a `str` - the 5.5x round
peak the B1a report measured. These tests keep the copy byte-for-byte equal to the old
text path for the shapes a raw really has, keep the old failure behaviour for a raw no
`read_text` would accept, and guard the memory that made the change worth doing.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import gc
import hashlib
import json
import shutil
import tracemalloc
from pathlib import Path

import pytest

from agora import header as h
from agora import store


def _header(title="raw 複製") -> dict:
    ulid = h.new_ulid()
    return {"type": "Session", "title": title, "tags": [], "id": f"agora:{ulid}", "refs": [],
            "case": None,
            "agora": {"header": 2, "created_at": "2026-10-20T00:00:00Z",
                      "updated_at": "2026-10-20T00:00:00Z", "relation": "import",
                      "parents": [],
                      "source": {"agent": "claude", "session_id": "ses_raw",
                                 "created_at": "2026-10-20T00:00:00Z"}}}


def _remember_ready(raw: bytes) -> tuple[store.Paths, Path, str]:
    """Stage a session whose raw is `raw`; return its paths, outbox folder and raw name."""
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", raw)
    return paths, folder, h.agora_of(hdr)["raw"]["file"]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _raw_shapes() -> dict[str, bytes]:
    tool_result = ("步驟 {n}：讀取 /Users/dev/proj/module.py 完成，exit code 0\n" * 12_000)
    return {
        "cjk": json.dumps({"type": "user", "text": "表格轉換を変換するテーブル　ＣＳＶ" * 2_000},
                          ensure_ascii=False).encode("utf-8"),
        "big-tool-result": json.dumps({"type": "user", "toolUseResult": {"content": tool_result}},
                                      ensure_ascii=False).encode("utf-8"),
        "escaped-newlines": json.dumps(
            {"type": "assistant", "text": "第一行\n第二行\t制表\n\"引號\"\\反斜線"},
            ensure_ascii=False).encode("utf-8"),
        "emoji": json.dumps({"text": "🙂🚀" * 5_000}, ensure_ascii=False).encode("utf-8"),
    }


@pytest.mark.parametrize("shape", sorted(_raw_shapes()))
def test_copy_atomic_matches_the_text_copy_it_replaces(tmp_path, shape):
    """The old path is the reference: `write_atomic(target, src.read_text())`."""
    raw = _raw_shapes()[shape]
    src = tmp_path / "raw.json"
    src.write_bytes(raw)
    old = tmp_path / "old.json"
    store.write_atomic(old, src.read_text(encoding="utf-8"))     # the code B1b-1 replaced
    new = tmp_path / "new.json"
    store.copy_atomic(src, new, utf8=True)

    assert new.read_bytes() == raw, shape
    assert store.md5_file(new) == store.md5_file(old) == _md5(raw), shape


def test_remember_copies_the_raw_byte_for_byte():
    raw = _raw_shapes()["cjk"]
    paths, folder, name = _remember_ready(raw)
    store.remember(paths, folder)

    mirrored = paths.mirror / folder.name / name
    assert mirrored.read_bytes() == raw
    assert mirrored.read_bytes() == (folder / name).read_bytes()


def test_remember_leaves_a_same_name_same_size_raw_alone():
    """sync calls `remember` for every outbox entry on every run; a raw already there
    with the same name and size is not rewritten."""
    paths, folder, name = _remember_ready(b'{"x": 1}')
    store.remember(paths, folder)
    target = paths.mirror / folder.name / name
    target.write_bytes(b'{"x": 2}')          # same size, different bytes: a sentinel
    store.remember(paths, folder)
    assert target.read_bytes() == b'{"x": 2}'


def test_remember_rewrites_a_same_name_raw_of_another_size():
    """The other side of the skip (review 中3): same name but a different size is not
    "already there" - the mirror gets the outbox's bytes again. Without this, a skip
    that only looked at whether the file exists would pass the test above."""
    raw = b'{"x": 1}'
    paths, folder, name = _remember_ready(raw)
    store.remember(paths, folder)
    target = paths.mirror / folder.name / name
    target.write_bytes(b'{"x": 123456}')     # another size: stale
    store.remember(paths, folder)
    assert target.read_bytes() == raw


def test_remember_clears_the_raws_the_new_header_replaced():
    paths, folder, name = _remember_ready(b'{"x": 1}')
    mirror = paths.mirror / folder.name
    mirror.mkdir(parents=True)
    old = mirror / "raw-000000000000.json"
    old.write_bytes(b"old")
    store.remember(paths, folder)

    assert not old.exists()
    assert [p.name for p in mirror.glob("raw-*")] == [name]


def test_a_copy_that_fails_midway_leaves_no_half_file(tmp_path, monkeypatch):
    """A reader must see the old file or the new one, never the bytes a failed copy
    happened to get out first."""
    src = tmp_path / "raw.json"
    src.write_bytes(b"a" * 100_000)
    dst = tmp_path / "raw-copy.json"
    dst.write_bytes(b"the previous version")

    def dies_after_a_chunk(fileobj, out, length=0):
        out.write(b"half")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfileobj", dies_after_a_chunk)
    with pytest.raises(OSError):
        store.copy_atomic(src, dst, utf8=True)

    assert dst.read_bytes() == b"the previous version"
    assert list(tmp_path.glob("raw-copy.json*.tmp")) == [], "a leftover staging file"


def test_a_copy_that_fails_with_no_target_leaves_nothing(tmp_path, monkeypatch):
    src = tmp_path / "raw.json"
    src.write_bytes(b"a" * 100_000)
    dst = tmp_path / "never-there.json"

    def boom(fileobj, out, length=0):
        raise OSError("boom")

    monkeypatch.setattr(shutil, "copyfileobj", boom)
    with pytest.raises(OSError):
        store.copy_atomic(src, dst)

    assert not dst.exists()
    assert list(tmp_path.glob("never-there.json*.tmp")) == []


def test_remember_still_rejects_a_raw_read_text_would_have_rejected():
    """`read_text` raised UnicodeDecodeError before anything was written, and
    `_index_outbox` catches it to skip the session. The byte copy has to keep that
    answer (`copy_atomic(..., utf8=True)`), or a corrupt raw would start being indexed."""
    raw = b'{"x": "\xff not utf-8"}'
    paths, folder, name = _remember_ready(raw)
    with pytest.raises(UnicodeDecodeError):
        store.remember(paths, folder)

    mirror = paths.mirror / folder.name
    assert not (mirror / name).exists()
    assert list(mirror.glob("*.tmp")) == []


def test_remember_rejects_a_raw_cut_off_inside_a_character_at_eof():
    """Review 高2: the invalid byte above fails on the first chunk; this one is valid
    until the file ends in the middle of a two-byte character, which only the
    decoder's `final=True` at EOF notices. `read_text` rejected it too."""
    raw = b'{"x": "\xc3'
    paths, folder, name = _remember_ready(raw)
    with pytest.raises(UnicodeDecodeError):
        store.remember(paths, folder)

    mirror = paths.mirror / folder.name
    assert not (mirror / name).exists()
    assert list(mirror.glob("*.tmp")) == []


def test_remember_does_not_hold_a_second_copy_of_the_raw():
    """The guard B1b-1 exists for: a 24 MB raw must not become a 24 MB string.

    Measured as the traced peak over the baseline, so the raw and the session lists
    already alive in the caller do not count. The old text path peaks at >= 24 MB
    (and more for CJK); streaming with a 1 MiB chunk stays around 2 MB.
    """
    size = 24 << 20
    raw = b'{"text":"' + b"a" * (size - 11) + b'"}'
    assert len(raw) == size
    paths, folder, _name = _remember_ready(raw)
    del raw                       # only the file on disk is the input now
    gc.collect()

    tracemalloc.start()
    try:
        gc.collect()
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        store.remember(paths, folder)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    grew = peak - baseline
    assert grew < size // 3, f"remember held {grew / (1 << 20):.1f} MB for a {size >> 20} MB raw"

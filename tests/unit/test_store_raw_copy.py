"""The raw copy in `store.remember` is a byte copy, not a text round trip (B1b-1);
`store.stage` also accepts a file path and streams the raw into the outbox (B1b-2).

`remember` used to do `write_atomic(target, wanted.read_text())`: the raw was already
on disk as bytes, and that held a whole second copy of it as a `str` - the 5.5x round
peak the B1a report measured. These tests keep the copy byte-for-byte equal to the old
text path for the shapes a raw really has, keep the old failure behaviour for a raw no
`read_text` would accept, guard the memory that made the change worth doing, and pin
that staging a file-backed raw does it without a second in-memory copy and cleans up on
failure.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import gc
import hashlib
import json
import os
import shutil
import sys
import tracemalloc
from pathlib import Path

import pytest

from agora import cli, header as h
from agora import store
from agora.agents.base import Exported
from agora.agents.claude import ClaudeAgent, encode_project_dir


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
    return paths, folder, store.raw_name(h.agora_of(hdr)["raw"])


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


def test_stage_from_file_streams_bytes_and_header_fields(tmp_path):
    """A file-backed raw is staged byte-for-byte without being read into memory."""
    raw = _raw_shapes()["big-tool-result"]
    src = tmp_path / "raw.json"
    src.write_bytes(raw)
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", src)

    name = store.raw_name(hdr["agora"]["raw"])
    assert (folder / name).read_bytes() == raw
    assert hdr["agora"]["raw"]["md5"] == hashlib.md5(raw).hexdigest()
    assert hdr["agora"]["raw"]["size"] == len(raw)


@pytest.mark.parametrize("link_works", [True, False], ids=["hard-link", "copy"])
def test_stage_from_file_failure_leaves_no_tmp_folder(tmp_path, monkeypatch, link_works):
    """A mid-read crash must not leave the `.tmp-<ulid>` staging folder behind - whether the
    raw was hard-linked in or copied (a different volume)."""
    raw = b"a" * 3_000_000
    src = tmp_path / "raw.json"
    src.write_bytes(raw)
    paths = store.Paths.from_env()
    hdr = _header()
    ulid = hdr["id"].split(":", 1)[1]
    if not link_works:
        monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("EXDEV")))
    real_feed, calls = store._Chunker.feed, []

    def dies_after_a_chunk(self, data):
        calls.append(len(data))
        if len(calls) >= 2:
            raise OSError("disk full")
        return real_feed(self, data)

    monkeypatch.setattr(store._Chunker, "feed", dies_after_a_chunk)
    with pytest.raises(OSError):
        store.stage(paths, hdr, "## user\nCSV\n", src)

    assert not (paths.outbox / f".tmp-{ulid}").exists()
    assert not (paths.outbox / ulid).exists()


def test_stage_from_file_links_instead_of_copying_when_it_can(tmp_path):
    """Design B.4: same volume -> one hard link, not a second whole-raw write."""
    src = tmp_path / "raw.json"
    src.write_bytes(b'{"x": 1}')
    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", src)
    staged = folder / store.raw_name(hdr["agora"]["raw"])
    assert os.path.samefile(src, staged) or tmp_path.stat().st_dev != paths.outbox.stat().st_dev


def test_claude_export_stage_does_not_hold_raw_bytes(tmp_path, monkeypatch):
    """Export + stage with a ~35 MB CJK jsonl peaks below 2.2x and never needs `.raw`.

    The streaming pack writes the raw to disk, and the file-backed stage copies it
    to the outbox without reading it back. The only large memory still held is the
    line list and the carried turns (B1a), so the ratio is well under the old 5.6x.
    """
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pdir = home / ".claude" / "projects" / encode_project_dir(workdir)
    pdir.mkdir(parents=True)
    sid = "11111111-2222-4333-8444-555555555555"
    path = pdir / f"{sid}.jsonl"

    # ~35 MB of non-ASCII jsonl so the line list pays the CJK str overhead.
    content = "中文" * 5000
    line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"
    target = 35 << 20
    with path.open("wb") as f:
        while f.tell() < target:
            f.write(line.encode("utf-8"))
    main_bytes = path.stat().st_size

    agent = ClaudeAgent()
    gc.collect()
    tracemalloc.start()
    try:
        gc.collect()
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        exported = agent.export(sid, hint_dir=workdir)
        paths = store.Paths.from_env()
        store.stage(paths, _header(), "## user\nCSV\n", exported.raw_file)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    grew = peak - baseline
    assert grew < 2.2 * main_bytes, (
        f"export+stage peak {grew / (1 << 20):.1f} MB for {main_bytes / (1 << 20):.1f} MB file")
    # The lazy property reads the file correctly when something does need bytes.
    assert exported.raw_md5 == hashlib.md5(exported.raw).hexdigest()
    assert exported.raw_size == len(exported.raw)


def test_write_one_uses_raw_md5_without_loading_raw(tmp_path, monkeypatch):
    """`_write_one` must stage the raw through `raw_file`/`raw_md5`, never `.raw`."""
    raw = b'{"format": "claude-jsonl/1", "main": [], "aux": {}}'
    src = tmp_path / "raw.json"
    src.write_bytes(raw)
    exported = Exported(session_id="s", raw_file=src,
                        raw_md5=hashlib.md5(raw).hexdigest(), raw_size=len(raw),
                        turns=[], title="t")
    accesses: list = []
    real_getter = Exported.raw.fget

    def tracked_raw(self):
        accesses.append(1)
        return real_getter(self)

    monkeypatch.setattr(Exported, "raw", property(tracked_raw))

    paths = store.Paths.from_env()
    hdr = _header()
    folder = store.stage(paths, hdr, "## user\nCSV\n", raw)
    target_id = hdr["id"]
    record = {"agora_id": target_id, "agent": "claude", "agent_session_id": "s",
              "dir": str(tmp_path), "parent": {"id": target_id, "raw_md5": "0" * 32},
              "parent_headers": [], "title": "t", "before_count": 0,
              "started_at": "2026-10-20T00:00:00Z", "header_updates": {},
              "add_tags": [], "saved_md5": "0" * 32}
    run = cli._Run(paths, record)

    saved, _where, _note = cli._write_one(paths, run, exported, ClaudeAgent(),
                                          check_drive=False)
    assert saved is not None
    assert not accesses, "_write_one loaded exported.raw instead of using raw_md5"


def _old_aux(sidecar: Path) -> bytes:
    """The aux bytes as `_pack_aux` + `json.dumps(..., ensure_ascii=False)` spelled them before #240."""
    import base64
    aux: dict = {}
    if sidecar.is_dir():
        for p in sorted(sidecar.rglob("*")):
            if not p.is_file():
                continue
            raw = p.read_bytes()
            try:
                aux[p.relative_to(sidecar).as_posix()] = raw.decode("utf-8")
            except UnicodeDecodeError:
                aux[p.relative_to(sidecar).as_posix()] = {"$base64": base64.b64encode(raw).decode()}
    return json.dumps(aux, ensure_ascii=False).encode("utf-8")


def test_iter_aux_bytes_equal_the_old_dumps(tmp_path):
    """Streamed aux == json.dumps of the whole dict: key order, ensure_ascii=False, `$base64`, empty, absent."""
    from agora.agents import claude
    side = tmp_path / "side"
    assert b"".join(claude._iter_aux(side)) == _old_aux(side) == b"{}"       # no sidecar
    side.mkdir()
    assert b"".join(claude._iter_aux(side)) == _old_aux(side) == b"{}"       # empty sidecar
    (side / "sub").mkdir()
    (side / "b.txt").write_text("中文 \"quote\" \\ \n\t\x01   😀", encoding="utf-8")
    (side / "a.txt").write_bytes(b"")                                          # empty file
    (side / "sub" / "x.bin").write_bytes(b"\xff\xfe\x00bad")                   # -> $base64
    (side / "sub" / "中.jsonl").write_bytes("\ufeff".encode("utf-8") + b'{"k": "v"}\n')
    big = ("中😀a" * 700_000).encode("utf-8")                                   # > 1 chunk, splits mid-char
    (side / "sub" / "big.txt").write_bytes(big)
    (side / "sub" / "big.bin").write_bytes(big + b"\xff")                     # invalid only at the very end
    (side / "sub" / "cut.txt").write_bytes(b"ok" + "中".encode("utf-8")[:2])  # truncated tail char
    assert b"".join(claude._iter_aux(side)) == _old_aux(side)


def test_claude_export_with_big_aux_does_not_hold_the_sidecar(tmp_path):
    """A ~20 MB sidecar next to a small session peaks under 1.5x the session size (#240)."""
    home = Path(os.environ["AGORA_CLAUDE_HOME"])
    workdir = tmp_path / "proj"
    workdir.mkdir()
    pdir = home / ".claude" / "projects" / encode_project_dir(workdir)
    pdir.mkdir(parents=True)
    sid = "11111111-2222-4333-8444-666666666666"
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n"
    (pdir / f"{sid}.jsonl").write_text(line * 100, encoding="utf-8")
    side = pdir / sid / "subagents"
    side.mkdir(parents=True)
    row = (json.dumps({"type": "assistant", "text": "中文" * 2000}, ensure_ascii=False) + "\n").encode("utf-8")
    with (side / "agent-1.jsonl").open("wb") as f:
        while f.tell() < (20 << 20):
            f.write(row)
    (side / "blob.bin").write_bytes(b"\xff" * (1 << 20))
    total = (pdir / f"{sid}.jsonl").stat().st_size + sum(p.stat().st_size for p in side.iterdir())

    agent = ClaudeAgent()
    gc.collect()
    tracemalloc.start()
    try:
        gc.collect()
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        exported = agent.export(sid, hint_dir=workdir)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert exported.raw_file is not None
    grew = peak - baseline
    assert grew < 1.5 * total, f"export peak {grew / (1 << 20):.1f} MB for {total / (1 << 20):.1f} MB session"

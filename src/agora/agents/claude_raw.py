"""Raw packet format for Claude Code sessions (design.md 5.2, 5.4; #342).

Defines the raw envelope format, writers, readers, streaming markers,
and aux stream encoding/decoding:

    {"format": "claude-jsonl/1", "main": [<each line, original string>],
     "aux": {"<path relative to <uuid>/>": <utf-8 text or {"$base64": ...}>}}

Does not import claude or store.
"""

from __future__ import annotations

import base64
import codecs
import io
import json
from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agora.agents.base import AgentError

# --- moved verbatim (#342) ---
FORMAT = "claude-jsonl/1"
_AUX_CHUNK = 3 << 18   # 768 KiB; a multiple of 3 so base64 pieces concatenate without padding


def _read_chunks(path: Path, limit: int) -> Iterator[bytes]:
    """The first `limit` bytes of `path`, one chunk at a time."""
    with path.open("rb") as f:
        while limit > 0:
            chunk = f.read(min(_AUX_CHUNK, limit))
            if not chunk:
                return
            limit -= len(chunk)
            yield chunk


def _is_utf8(path: Path, size: int) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        for chunk in _read_chunks(path, size):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _iter_aux(sidecar: Path) -> Iterator[bytes]:
    """The aux object as `json.dumps({rel: text | {"$base64": ...}}, ensure_ascii=False)`
    would spell it (review N15), in pieces: one file's chunk is all that is held (#240).

    A file is read twice - once to learn whether it is UTF-8 (the choice between a text
    value and `$base64` has to come before the first byte of the value), once to emit it.
    The second pass stops at the size the first one saw, so a file that grows in between
    cannot make the value longer than what was checked.

    A file may change between the two passes; the value's opening bytes (a `"`
    for text, `{"$base64": "` for binary) have already been written, so the kind
    cannot be switched mid-stream - a file that was text when checked but became
    binary (or vice versa) cannot be re-classified here. The caller retries the
    whole round next time. `AgentError` is raised in these cases:
    - text branch: a `UnicodeDecodeError` on the second pass (the file stopped being
      valid UTF-8, or ended with an incomplete trailing multibyte sequence -
      `decoder.decode(b"", final=True)` is called at the end to catch the latter);
      or reading fewer bytes than `size` (the file was truncated).
    - `$base64` branch: reading fewer bytes than `size` (the file was truncated).
    A file that keeps the same size but is rewritten to another valid UTF-8 text is
    not an error: the new content is emitted and the value stays self-consistent.
    A file that disappears between the two passes, or becomes a directory, is not
    converted: the original `OSError` propagates."""
    files = [] if not sidecar.is_dir() else [p for p in sorted(sidecar.rglob("*")) if p.is_file()]
    yield b"{"
    for i, path in enumerate(files):
        size = path.stat().st_size
        rel = path.relative_to(sidecar).as_posix()
        key = json.dumps(rel, ensure_ascii=False)
        yield (", " if i else "").encode("utf-8") + key.encode("utf-8") + b": "
        if _is_utf8(path, size):
            decoder = codecs.getincrementaldecoder("utf-8")()
            yield b'"'
            total = 0
            try:
                for chunk in _read_chunks(path, size):
                    total += len(chunk)
                    text = decoder.decode(chunk)
                    yield json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")
                if total < size:
                    raise AgentError(
                        f"{rel}：讀的時候被截短（讀到 {total} 位元組，檢查時是 {size}），"
                        f"檔案在兩遍讀取之間被改了，下一輪會重試")
                decoder.decode(b"", final=True)
            except UnicodeDecodeError as e:
                raise AgentError(
                    f"{rel}：讀的時候不是有效的 UTF-8 或結尾不完整（檔案在兩遍讀取之間被改了），"
                    f"下一輪會重試") from e
            yield b'"'
        else:
            yield b'{"$base64": "'
            total = 0
            for chunk in _read_chunks(path, size):
                total += len(chunk)
                yield base64.b64encode(chunk)
            if total < size:
                raise AgentError(
                    f"{rel}：讀的時候被截短（讀到 {total} 位元組，檢查時是 {size}），"
                    f"檔案在兩遍讀取之間被改了，下一輪會重試")
            yield b'"}'
    yield b"}"


def _aux_files(aux: dict, rewrite=None) -> Iterator[tuple[str, bytes]]:
    """(relative path, the bytes it is restored as); rewrite(text, rel) transforms .jsonl ones (P12)."""
    for rel, content in aux.items():
        parts = PurePosixPath(rel).parts
        if PurePosixPath(rel).is_absolute() or ".." in parts:  # CL14
            raise AgentError(f"aux 路徑超出範圍：{rel}")
        if (rewrite is not None and rel.endswith(".jsonl")
                and isinstance(content, str)):
            content = rewrite(content, rel)
        if isinstance(content, dict) and "$base64" in content:
            yield rel, base64.b64decode(content["$base64"])
        else:
            yield rel, content.encode("utf-8")


def _pack_raw(main: list[str], aux: dict) -> bytes:
    # C4 (impl-mem 2a): json.dump straight into a byte buffer - json.dumps's
    # whole-document str plus its encode would hold two copies of the raw at
    # the peak. The chunked writer produces the same bytes as dumps did.
    # newline="": no os.linesep translation of the "\n" json.dump may emit (the
    # packed output has no bare newline today - json escapes the ones inside
    # strings - but spelling it out keeps that true if anyone adds an indent).
    buf = io.BytesIO()
    writer = io.TextIOWrapper(buf, encoding="utf-8", newline="", write_through=True)
    try:
        json.dump({"format": FORMAT, "main": main, "aux": aux}, writer, ensure_ascii=False)
        writer.flush()   # clarity only; the detach below flushes the wrapper and encoder anyway (review 低3)
    finally:
        writer.detach()           # flush the encoder without closing buf
    return buf.getvalue()


#: The envelope's fixed bytes (design B.2); `_stream_raw` writes them as it reads.
_RAW_HEAD = b'{"format": ' + json.dumps(FORMAT).encode() + b', "main": ['
_AUX_SEP = b'], "aux": '


@dataclass
class PackedRaw:
    """What the one streaming pass knows about the raw it wrote (design B.2).

    These are the offsets and digests R3's cursor is built from (`append_from` /
    `seal_upto`, the three sealed values) and they stay in the adapter: they are
    not part of `Exported` (design B.2 point 3).
    """

    path: Path
    md5: str                  # md5 of the whole raw file
    size: int
    main_end: int             # byte offset where `], "aux": ` starts (tail begins here)
    main_count: int           # main elements written (the non-blank, valid lines)
    main_md5: str             # md5 of raw[0:main_end]
    stable_at: array          # stable_at[n] = offset after n elements; len = main_count + 1
    _stable_md5: dict[int, str] = field(default_factory=dict, repr=False, compare=False)

    def stable_md5(self, n: int) -> str | None:
        """md5 of raw[0:stable_at(n)], when this pass captured that boundary.

        The caller names the one boundary it needs (`capture=`); copying an md5
        state for every element would be one live state per line, and R3 only
        ever asks about the cursor's own `sealed_line_count`.
        """
        return self._stable_md5.get(n)


def _unpack_raw(raw: bytes) -> tuple[list[str], dict]:
    try:
        doc = json.loads(raw.decode("utf-8"))
        return list(doc["main"]), dict(doc.get("aux") or {})
    except (ValueError, KeyError, AttributeError) as e:
        raise AgentError(f"Claude raw 解析失敗：{e}")

"""agy 的 protobuf 線格式與步驟解碼（#26 PR2a 自 agy.py 拆出；PR2b 整理：掃描面
搬去 agy_store，這一層只依賴 base 的純函式）。

The protobuf wire format (docs/agy-storage.md §3)：`varint`／`fields`／`_all`／
`one`／`texts`／`_text`／`one_line`／`_args`、閱讀版的 `_user_turn`／
`_model_turn`／`_turn`／`read_turns`，以及 step_type 常數。每個函式只吃
bytes，不開檔、不碰 AGORA_AGY_HOME；需要打開快照檔的 `scan_text`（與遞迴
收集字串的 `scan_strings`）在 `agy_store`（依賴方向單向：agy_store → 這裡）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from .base import Turns, tool_line, warn

#: step_type values observed on fake conversations (docs/agy-storage.md §3).
STEP_USER = 14
STEP_MODEL = 15
STEP_TOOL = 132


def varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("varint 被截斷")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint 太長")


def fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value) for one message; groups are not used."""
    i, end = 0, len(buf)
    while i < end:
        key, i = varint(buf, i)
        number, wire = key >> 3, key & 0x07
        if not number:
            raise ValueError("欄位編號 0")
        if wire == 0:                       # varint
            value, i = varint(buf, i)
        elif wire == 1:                    # 64-bit
            if i + 8 > end:
                raise ValueError("64-bit 欄位被截斷")
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:                    # length-delimited
            length, i = varint(buf, i)
            if i + length > end:
                raise ValueError("長度超過訊息")
            value, i = buf[i:i + length], i + length
        elif wire == 5:                    # 32-bit
            if i + 4 > end:
                raise ValueError("32-bit 欄位被截斷")
            value, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"不支援的 wire type {wire}")
        yield number, wire, value


def _all(buf: bytes | None, number: int) -> list[bytes]:
    if not buf:
        return []
    return [value for field, wire, value in fields(buf)
            if field == number and wire == 2]


def one(buf: bytes | None, number: int) -> bytes | None:
    hits = _all(buf, number)
    return hits[0] if hits else None


def texts(buf: bytes | None, number: int) -> list[str]:
    return [value.decode("utf-8", "replace") for value in _all(buf, number)]


def _text(buf: bytes | None, number: int) -> str | None:
    hits = texts(buf, number)
    return hits[0] if hits else None


def one_line(text: str) -> str:
    """Text flattened to one line, so a reading turn stays line-shaped."""
    return " ".join(text.replace("\r\n", "\n").replace("\r", "\n").split())


def _args(value: str | None) -> object:
    """A tool's argument JSON as an object, or the raw string when it is not JSON."""
    if value is None:
        return ""
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _user_turn(payload: bytes) -> Turns:
    lines = [text for text in texts(one(payload, 19), 2) if text.strip()]
    return [("user", lines)] if lines else []


def _model_turn(payload: bytes) -> Turns:
    message = one(payload, 20)
    lines = [text for text in texts(message, 1) if text.strip()]
    for part in _all(message, 7):
        name = _text(part, 2) or "?"
        lines.append(tool_line(name, _args(_text(part, 3))))
    return [("assistant", lines)] if lines else []


def _turn(step_type: int, payload: bytes) -> Turns:
    if step_type == STEP_USER:
        return _user_turn(payload)
    if step_type == STEP_MODEL:
        return _model_turn(payload)
    return []                                  # unknown: the caller marks it


def read_turns(rows: Iterator[tuple[int, int, bytes]]) -> tuple[Turns, int]:
    """(reading turns, message count) for the steps of one conversation.

    Step type 132 is the tool *execution* step: it is recognised, and it has no
    reading text of its own (the call was already said by the model's 20.7 part
    and its output belongs to `scan_text`, not to session.md - design 4.4). So
    it is silent, not `[skip …]`. Anything not observed yet is visible as
    `[skip agy-step-<idx>/<type>]`; a step whose protobuf cannot be read is the
    same marker for that step only. The message count is the 14/15 turns that
    produced reading content - `[skip …]` lines are not conversation (N4).
    """
    turns: Turns = []
    messages = 0
    unknown = broken = 0
    for idx, step_type, payload in rows:
        if step_type == STEP_TOOL:
            continue
        if step_type not in (STEP_USER, STEP_MODEL):
            unknown += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
            continue
        try:
            part = _turn(step_type, payload)
        except (ValueError, IndexError, UnicodeDecodeError):
            broken += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
            continue
        if part:
            turns.extend(part)
            messages += 1
        else:
            unknown += 1
            turns.append(("user", [f"[skip agy-step-{idx}/{step_type}]"]))
    if unknown or broken:
        said = []
        if unknown:
            said.append(f"{unknown} 個步驟不認得")
        if broken:
            said.append(f"{broken} 個步驟解不開")
        warn("agy：" + "、".join(said) + "，已標成 [skip …]，其餘照常讀取")
    return turns, messages

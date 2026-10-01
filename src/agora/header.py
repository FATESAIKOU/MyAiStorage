"""The shared header (design.md section 3).

Every item in MyBrain, Agora, Foundry and Atelier carries the same kind of
YAML front matter. Entities point at each other only through it, so this
module knows the reserved top-level fields and the ref syntax, and leaves
every other field alone.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote, unquote

import yaml

HEADER_VERSION = 1
ENTITIES = ("mybrain", "agora", "foundry", "atelier")

# Top-level fields shared by all four entities. Anything else is
# entity-specific and is kept as-is on read and write.
RESERVED = (
    "header", "entity", "type", "id", "title", "created_at", "updated_at",
    "refs", "case", "note", "tags",
)

# Keys a user may set with `--header key=value` on import and merge.
SETTABLE = ("title", "case", "refs", "tags", "note")
MULTI = ("refs", "tags")

# Flat aliases `search --header key=value` accepts, mapped to header paths.
SEARCH_ALIASES = {
    "agent": ("source", "agent"),
    "relation": ("relation",),
    "case": ("case",),
    "tag": ("tags",),
    "ref": ("refs",),
    "title": ("title",),
}


class HeaderError(ValueError):
    """A header, ref or --header argument that breaks the rules."""


@dataclass(frozen=True)
class Ref:
    entity: str
    locator: str
    rev: str | None = None
    fragment: str | None = None

    def __str__(self) -> str:
        text = f"{self.entity}:{quote(self.locator, safe='/:._-~ ')}"
        if self.rev:
            text += f"@{self.rev}"
        if self.fragment:
            text += f"#{self.fragment}"
        return text


def parse_ref(text: str) -> Ref:
    """`entity ":" locator ["@" rev] ["#" fragment]`, split on the first colon."""
    if not isinstance(text, str) or ":" not in text:
        raise HeaderError(f"ref 要寫成 <entity>:<locator>：{text!r}")
    entity, rest = text.split(":", 1)
    if entity not in ENTITIES:
        raise HeaderError(f"不認得的 entity {entity!r}（可用：{', '.join(ENTITIES)}）")
    fragment = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
    rev = None
    if "@" in rest:
        rest, rev = rest.rsplit("@", 1)
    if not rest:
        raise HeaderError(f"ref 缺少 locator：{text!r}")
    return Ref(entity, unquote(rest), rev or None, fragment or None)


def split_document(text: str) -> tuple[dict, str]:
    """Return (header, body). Only the first `---` block is the header."""
    if not text.startswith("---\n"):
        raise HeaderError("檔案開頭不是 header（---）")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise HeaderError("header 沒有結尾的 ---")
    try:
        header = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as e:
        raise HeaderError(f"header 的 YAML 壞了：{e}") from None
    if not isinstance(header, dict):
        raise HeaderError("header 不是 key: value 的形式")
    return header, text[end + 5:]


def dump_document(header: dict, body: str) -> str:
    """Serialise with safe_dump so titles and notes with newlines or `---` stay intact."""
    text = yaml.safe_dump(header, allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{text}---\n{body}"


def validate(header: dict) -> list[str]:
    """Check the shared fields; return warnings, raise on hard errors."""
    warnings = []
    version = header.get("header")
    if version != HEADER_VERSION:
        warnings.append(f"header 版本 {version!r} 不是 {HEADER_VERSION}，只讀共通欄位")
    entity = header.get("entity")
    if entity not in ENTITIES:
        raise HeaderError(f"entity 必須是 {', '.join(ENTITIES)} 之一：{entity!r}")
    item_id = header.get("id")
    if not isinstance(item_id, str) or not item_id.startswith(f"{entity}:"):
        raise HeaderError(f"id 必須以 {entity}: 開頭：{item_id!r}")
    if header.get("case") is not None:
        parse_ref(header["case"])
    for ref in header.get("refs") or []:
        parse_ref(ref)
    return warnings


def parse_header_args(args: list[str]) -> dict:
    """Turn `--header` values into field updates.

    `key=value` sets a settable field (refs and tags accumulate); text
    without `=` is free text and goes into `note`, so the original
    `--header '一些 meta 資訊'` usage keeps working.
    """
    updates: dict = {}
    notes: list[str] = []
    for arg in args:
        key, sep, value = arg.partition("=")
        key = key.strip()
        if not sep or key not in SETTABLE:
            if sep and key.isidentifier():
                print(f"[agora] --header 的 {key!r} 不是可設定的欄位，整段當成 note", file=sys.stderr)
            notes.append(arg)
            continue
        if key in MULTI:
            updates.setdefault(key, []).append(value)
        elif key == "note":
            notes.append(value)
        else:
            updates[key] = value
    if "case" in updates:
        parse_ref(updates["case"])
    for ref in updates.get("refs", []):
        parse_ref(ref)
    if notes:
        updates["note"] = "\n".join(notes)
    return updates


def parse_search_filters(args: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """`search --header key=value` → [(header path, value)]; unknown keys are an error."""
    filters = []
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep or key not in SEARCH_ALIASES:
            raise HeaderError(
                f"search 的 --header 要寫成 key=value，key 只能是 {', '.join(SEARCH_ALIASES)}：{arg!r}")
        filters.append((SEARCH_ALIASES[key], value))
    return filters


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """A ULID: 48-bit millisecond time + 80 random bits, Crockford base32."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))

"""The shared header (design.md section 3).

Every item in MyBrain, Agora, Foundry and Atelier carries OKF v0.2
frontmatter: `type` is required, every other field is optional, and
unknown fields are kept. Entities point at each other through `id`,
`refs` and `case`; Agora's own system fields live under `agora`.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import yaml

HEADER_VERSION = 2
ENTITIES = ("mybrain", "agora", "foundry", "atelier")
SESSION_TYPE = "Session"
SYSTEM_KEYS = ("id", "agora")          # never set by the user (design 3.5)

# search --filter shorthands
FILTER_ALIASES = {"agent": ("agora", "source", "agent")}
TEXT_KEY = "text"                      # the reading version plus the header's text fields


class HeaderError(ValueError):
    """A header, ref, --header or --filter argument that breaks the rules."""


@dataclass(frozen=True)
class Ref:
    entity: str
    locator: str
    rev: str | None = None
    fragment: str | None = None


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
    header = upgrade(_load_yaml(text[4:end]))
    return header, text[end + 5:]


_OLD_AGORA_KEYS = ("relation", "parents", "source", "raw", "created_at", "updated_at")


def upgrade(header: dict) -> dict:
    """Read a version-1 header (before OKF, v4 design) as the current shape (V1).

    Old Agora headers kept relation, parents, source, raw and timestamps at
    the top level, used `type: session`, `entity` and `note`. Move them into
    the agora block so every reader sees one shape; the next save writes it.
    """
    if "agora" in header or not str(header.get("id", "")).startswith("agora:"):
        return header
    out = {k: v for k, v in header.items() if k not in _OLD_AGORA_KEYS + ("header", "entity", "note")}
    out["type"] = SESSION_TYPE
    if header.get("note") and not out.get("description"):
        out["description"] = header["note"]
    out["agora"] = {"header": HEADER_VERSION, **{k: header[k] for k in _OLD_AGORA_KEYS if k in header}}
    return out


def dump_document(header: dict, body: str) -> str:
    """Serialise with safe_dump so titles and descriptions with newlines or `---` stay intact."""
    return f"---\n{dump_header(header)}---\n{body}"


def dump_header(header: dict) -> str:
    return yaml.safe_dump(header, allow_unicode=True, sort_keys=False, width=1000)


def _load_yaml(text: str) -> dict:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise HeaderError(f"header 的 YAML 壞了：{e}") from None
    if not isinstance(data, dict):
        raise HeaderError("header 不是 key: value 的形式")
    return data


def agora_of(header: dict) -> dict:
    """The Agora system block (relation, parents, source, raw, timestamps)."""
    block = header.get("agora")
    return block if isinstance(block, dict) else {}


def validate(header: dict, *, strict_refs: bool = True) -> list[str]:
    """Check the shared fields; return warnings, raise on hard errors.

    With strict_refs=False a bad ref or case (say, an entity this version
    does not know yet) is only a warning, so the item is still read (R7).
    """
    warnings = []
    if not header.get("type"):
        raise HeaderError("OKF 的 type 是必填欄位")
    item_id = header.get("id")
    if not isinstance(item_id, str) or ":" not in item_id or item_id.split(":", 1)[0] not in ENTITIES:
        raise HeaderError(f"id 必須是 <entity>:<id>：{item_id!r}")
    if item_id.startswith("agora:") and agora_of(header).get("header") != HEADER_VERSION:
        warnings.append(f"agora 標頭版本 {agora_of(header).get('header')!r} 不是 {HEADER_VERSION}，只讀共通欄位")
    refs = header.get("refs") or []
    if not isinstance(refs, list):
        raise HeaderError("refs 必須是清單")
    for ref in ([header["case"]] if header.get("case") is not None else []) + list(refs):
        try:
            parse_ref(ref)
        except HeaderError as e:
            if strict_refs:
                raise
            warnings.append(str(e))
    return warnings


# ---------------------------------------------------------------------------
# User-supplied headers: auto fields, then --header-file, then --header (3.5)
# ---------------------------------------------------------------------------


def overlay(base: dict, updates: dict) -> dict:
    """Merge updates into base: mappings merge recursively, lists and scalars replace."""
    out = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = overlay(out[key], value)
        else:
            out[key] = value
    return out


def parse_header_args(args: list[str]) -> dict:
    """`--header a.b=value` → {"a": {"b": value}}; the value is parsed as YAML.

    So `tags=[csv, 表格]`, `generated.by=human:fatesaikou` and
    `sources=[{id: x, title: y}]` all work. A later --header for the same
    key wins.
    """
    updates: dict = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        key = key.strip()
        if not sep or not key or any(not part for part in key.split(".")):
            raise HeaderError(f"--header 要寫成 key=value（例如 description=…）：{arg!r}")
        # Only lists and mappings are parsed as YAML; everything else stays the
        # text the user typed, so "把 CSV: 轉成表格", "no" or a date stay strings (V2, V3).
        parsed: object = value
        if value.strip().startswith(("[", "{")):
            try:
                parsed = _plain(yaml.safe_load(value))
            except yaml.YAMLError as e:
                raise HeaderError(f"--header {key} 的清單／物件寫法有誤：{e}") from None
        node = updates
        parts = key.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if child is None:
                child = node[part] = {}
            elif not isinstance(child, dict):
                raise HeaderError(f"--header 的 {key!r} 和前面的值衝突")
            node = child
        node[parts[-1]] = parsed
    return updates


def _plain(value: object) -> object:
    """YAML dates and datetimes as ISO 8601 strings (OKF uses …Z for UTC), recursively."""
    import datetime as dt
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def load_header_file(path: str | None) -> dict:
    """A YAML header file, with or without surrounding `---` lines."""
    if not path:
        return {}
    text = Path(path).read_text(encoding="utf-8")
    text = text.strip()
    if text.startswith("---"):
        text = text[3:]
        if text.rstrip().endswith("---"):
            text = text.rstrip()[:-3]
    return _plain(_load_yaml(text))


LIST_KEYS = ("tags", "refs", "sources", "verified")   # OKF list fields; a single value becomes [value]


def user_updates(header_file: str | None, header_args: list[str]) -> dict:
    """--header-file first, then each --header in order; reject system fields."""
    updates = overlay(load_header_file(header_file), parse_header_args(header_args))
    for key in LIST_KEYS:
        if key in updates and updates[key] is not None and not isinstance(updates[key], list):
            updates[key] = [updates[key]]
    blocked = [k for k in SYSTEM_KEYS if k in updates]
    if blocked:
        raise HeaderError(f"{', '.join(blocked)} 是系統欄位，不能用 --header 改")
    if "type" in updates and updates["type"] != SESSION_TYPE:
        raise HeaderError(f"Agora 的 type 只能是 {SESSION_TYPE}")
    for ref in ([updates["case"]] if updates.get("case") is not None else []) + list(updates.get("refs") or []):
        parse_ref(ref)
    return updates


# ---------------------------------------------------------------------------
# search --filter KEY=VALUE (equal) and KEY~=TEXT (contains)
# ---------------------------------------------------------------------------


def parse_filters(args: list[str]) -> list[tuple[tuple[str, ...], str, str]]:
    """→ [(header path, op, value)]; op is "=" or "~=", path ("text",) means full text."""
    filters = []
    for arg in args:
        eq = arg.find("=")
        if eq < 0:
            raise HeaderError(f"--filter 要寫成 KEY=VALUE（全等）或 KEY~=TEXT（包含）：{arg!r}")
        contains = eq > 0 and arg[eq - 1] == "~"
        key, op, value = (arg[:eq - 1], "~=", arg[eq + 1:]) if contains else (arg[:eq], "=", arg[eq + 1:])
        key = key.strip()
        if not key:
            raise HeaderError(f"--filter 少了 KEY：{arg!r}")
        path = FILTER_ALIASES.get(key) or tuple(key.split("."))
        if path == (TEXT_KEY,) and op != "~=":
            raise HeaderError("全文只能用包含：--filter text~=關鍵字")
        filters.append((path, op, value))
    return filters


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """A ULID: 48-bit millisecond time + 80 random bits, Crockford base32."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))

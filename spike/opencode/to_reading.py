#!/usr/bin/env python3
"""把 `opencode export` 的 JSON 轉成Agora 的閱讀版 Markdown（spike V3）。

閱讀版是兩件事的同一份東西：**給人看的**（`agora show`）、**跨 agent 注入用的**
（`docs/design.md` 5.4 的「閱讀版注入」）。所以形狀必須是「一個 agent 的第一則
訊息讀完就能接著做」——意即：header（誰、從哪來、接續自誰）＋ 逐輪的
user／assistant 文字。

只用 `text` 與 `reasoning` 兩種 part：閱讀版不重播工具呼叫（raw.json 已經原封
不動留著原生工具呼叫），但**保留 reasoning 之外的使用者輸入與助手的可見輸出**。
另外把 step-finish 的 token/cost 帶成一行統計，讓人知道這段花了多少。

用法：
    to_reading.py --in exported.json --out session.md --agora-id agora:01ABC \\
                  [--agent opencode] [--title '...'] [--relation import] \\
                  [--parent agora:01DEF] [--tag x --tag y] [--note '...']
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

_PART_KEEP = ("text", "reasoning")


def _iso(ms: int | None) -> str:
    if not isinstance(ms, int):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _text_of(parts: list[dict]) -> str:
    out: list[str] = []
    for p in parts:
        if p.get("type") not in _PART_KEEP:
            continue
        body = p.get("text")
        if isinstance(body, str) and body.strip():
            label = "（思考）" if p.get("type") == "reasoning" else ""
            out.append(f"{label}{body.strip()}")
    return "\n\n".join(out)


def _usage(info: dict) -> str:
    tokens = info.get("tokens") or {}
    if not isinstance(tokens, dict):
        return ""
    parts = []
    for key, label in (("input", "in"), ("output", "out"),
                       ("reasoning", "think"), ("cache", "cache")):
        value = tokens.get(key)
        if isinstance(value, int):
            parts.append(f"{label}={value}")
    cost = info.get("cost")
    if isinstance(cost, (int, float)) and cost:
        parts.append(f"cost={cost:.4f}")
    return " ".join(parts)


def yaml_list(key: str, values: list[str]) -> str:
    """空清單寫成 `key: []`，非空寫成 `key:` 換行再縮排兩格。

    不能寫成 `key: - a\\n  - b`：`key:   - spike` 這種同行開頭的序列不是合法
    YAML，會讓整份 front matter 讀不出來。
    """
    if not values:
        return f"{key}: []"
    return f"{key}:\n" + "\n".join(f"  - {v}" for v in values)


def to_reading(payload: dict, *, agora_id: str, agent: str = "opencode",
               title: str | None = None, relation: str = "import",
               parents: list[str] | None = None, refs: list[str] | None = None,
               tags: list[str] | None = None, note: str = "",
               case: str | None = None) -> str:
    info = payload.get("info") or {}
    source = info.get("id")
    created = (info.get("time") or {}).get("created")
    updated = (info.get("time") or {}).get("updated")
    parents = parents or []
    refs = refs or []
    tags = tags or []

    lines = [
        "---",
        "entity: agora",
        "type: session",
        f"id: {agora_id}",
        f"title: {json.dumps(title or info.get('title') or '', ensure_ascii=False)}",
        f"created_at: {_iso(created)}",
        f"updated_at: {_iso(updated)}",
        "source:",
        f"  agent: {agent}",
        f"  session_id: {source}",
        f"relation: {relation}",
        yaml_list("parents", parents),
        yaml_list("refs", refs),
        f"case: {json.dumps(case, ensure_ascii=False) if case else 'null'}",
        f"note: {json.dumps(note, ensure_ascii=False)}",
        yaml_list("tags", tags),
        "---",
        "",
        "## 這是什麼",
        "",
        f"這是 Agora Session `{agora_id}` 的閱讀版。它由 `{agent}` 的 session "
        f"`{source}` 匯出而來，關係是 `{relation}`。",
        "**要接著做，先讀完下面每一輪**，然後從最後一輪繼續；前面的討論是脈絡。",
        "",
    ]

    messages = payload.get("messages") or []
    for i, m in enumerate(messages, start=1):
        m_info = m.get("info") or {}
        role = m_info.get("role")
        body = _text_of(m.get("parts") or [])
        head = f"## {i}. {role}"
        usage = _usage(m_info)
        if usage:
            head += f"　（{usage}）"
        lines.append(head)
        lines.append("")
        lines.append(body if body.strip() else "_（這則沒有文字輸出）_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", required=True, type=Path)
    ap.add_argument("--out", dest="dst", required=True, type=Path)
    ap.add_argument("--agora-id", required=True)
    ap.add_argument("--agent", default="opencode")
    ap.add_argument("--title", default=None)
    ap.add_argument("--relation", default="import")
    ap.add_argument("--parent", action="append", default=[])
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--note", default="")
    ap.add_argument("--case", default=None)
    args = ap.parse_args(argv)

    if not args.agora_id.startswith("agora:"):
        print(f"agora id 必須帶實體前綴 agora:（{args.agora_id}）", file=sys.stderr)
        return 2
    payload = json.loads(args.src.read_text(encoding="utf-8"))
    text = to_reading(payload, agora_id=args.agora_id, agent=args.agent,
                      title=args.title, relation=args.relation,
                      parents=list(args.parent), refs=list(args.ref),
                      tags=list(args.tag), note=args.note, case=args.case)
    args.dst.write_text(text, encoding="utf-8")
    turns = len(payload.get("messages") or [])
    print(f"{args.dst} agora_id={args.agora_id} turns={turns} "
          f"bytes={len(text.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
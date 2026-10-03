#!/usr/bin/env python3
"""把 `opencode export` 的 JSON 轉成 Agora 的閱讀版 Markdown（spike V3）。

**形狀照 design.md §4.4（D6）**：只收 user／assistant 的文字，加上每次工具呼叫
一行摘要；**不收工具結果、不收 thinking／reasoning**。認不得的 part 輸出
`[skip <型態>]` 而不是讓程式失敗。`step-start`／`step-finish` 是 opencode 的
記帳用 part（不是任何一種輸出），直接略過、不留下痕跡。

檔案本體是 design.md §4 說的 `session.md`：**header ＋ 閱讀版**。header 的欄位
照 §3.4（`header: 1`、`source.dir`、`raw: {file, md5, size}`…）；閱讀版本體的
產生方式與 `src/agora/agents/base.py` 的 `format_reading` 相同，這支腳本是為了
在還沒有 agent adapter 時先把 V3 的注入測試跑起來。

用法：
    to_reading.py --in exported.json --out session.md --agora-id agora:01ABC \\
        --raw-file raw-abc123def456.json --raw-md5 <md5> --raw-size <bytes> \\
        [--source-dir /Users/me/proj] [--relation import] [--parent agora:01DEF] \\
        [--ref mybrain:…] [--tag x] [--note '…']
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import yaml

#: §4.4：工具呼叫摘要最多 200 字，超過標「…」
TOOL_SUMMARY_MAX = 200

#: opencode 的記帳 part，不是任何一種輸出
_BOOKKEEPING = ("step-start", "step-finish")


class ReadingError(RuntimeError):
    pass


def _iso(ms: int | None) -> str:
    if not isinstance(ms, int):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def tool_line(name: str, args: object) -> str:
    """`[tool] <名稱> <參數摘要>`，換行壓成空白，超過 200 字標「…」。"""
    try:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(args)
    text = text.replace("\n", " ")
    if len(text) > TOOL_SUMMARY_MAX:
        text = text[:TOOL_SUMMARY_MAX] + "…"
    return f"[tool] {name} {text}".rstrip()


def part_lines(part: dict) -> list[str]:
    """一個 part 變成閱讀版裡的一行（或零行）。只看外觀，不看內容語意。"""
    kind = part.get("type")
    if kind in _BOOKKEEPING:
        return []
    if kind == "text":
        body = part.get("text")
        return [body.strip()] if isinstance(body, str) and body.strip() else []
    if kind == "tool":
        state = part.get("state") or {}
        return [tool_line(str(part.get("tool", "?")), state.get("input", {}))]
    # reasoning／thinking 依 D4.4 不收；其他型態要留下可見的痕跡
    if kind == "reasoning":
        return []
    return [f"[skip {kind}]"]


def usage_line(info: dict) -> str:
    tokens = info.get("tokens") or {}
    if not isinstance(tokens, dict):
        return ""
    parts = [f"{label}={tokens[key]}" for key, label in
             (("input", "in"), ("output", "out"), ("reasoning", "think"),
              ("cache", "cache"))
             if isinstance(tokens.get(key), int)]
    cost = info.get("cost")
    if isinstance(cost, (int, float)) and cost:
        parts.append(f"cost={cost:.4f}")
    return " ".join(parts)


def to_reading(payload: dict, *, agora_id: str, source_dir: str | None = None,
               relation: str = "import", parents: list[str] | None = None,
               refs: list[str] | None = None, tags: list[str] | None = None,
               note: str = "", case: str | None = None, title: str | None = None,
               host: str = "unknown", agent_version: str | None = None,
               raw_file: str | None = None, raw_md5: str | None = None,
               raw_size: int | None = None, include_stats: bool = True) -> str:
    """回傳 `session.md` 的完整內容（header ＋ 閱讀版）。"""
    info = payload.get("info") or {}
    source = info.get("id")
    if not source:
        raise ReadingError("匯出檔的 info.id 不見了")
    created = (info.get("time") or {}).get("created")
    updated = (info.get("time") or {}).get("updated")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ReadingError("匯出檔缺少 messages 清單")

    source: dict = {"agent": "opencode", "session_id": source}
    if source_dir:
        source["dir"] = source_dir
    if host and host != "unknown":
        source["host"] = host
    if info.get("version"):
        source["agent_version"] = str(info["version"])
    if isinstance(created, int):
        source["created_at"] = _iso(created)

    header = {
        "header": 1,
        "entity": "agora",
        "type": "session",
        "id": agora_id,
        "title": title if title is not None else (info.get("title") or ""),
        "created_at": _iso(created),
        "updated_at": _iso(updated),
        "refs": refs or [],
        "case": case,
        "note": note,
        "tags": tags or [],
        "source": source,
        "relation": relation,
        "parents": [{"id": p} for p in (parents or [])],
    }
    if raw_file:
        raw: dict = {"file": raw_file}
        if raw_md5:
            raw["md5"] = raw_md5
        if isinstance(raw_size, int):
            raw["size"] = raw_size
        header["raw"] = raw

    # yaml.safe_dump（§3.4：不用字串拼接）。allow_unicode 讓中文維持中文，
    # sort_keys=False 讓欄位順序跟設計文件一樣。
    front = yaml.safe_dump(header, allow_unicode=True, sort_keys=False,
                           default_flow_style=False).rstrip()

    out = [f"---\n{front}\n---\n"]
    for m in messages:
        m_info = m.get("info") or {}
        role = m_info.get("role")
        if role not in ("user", "assistant"):
            continue
        lines: list[str] = []
        for part in m.get("parts") or []:
            lines.extend(part_lines(part))
        if not [line for line in lines if line.strip()]:
            continue
        if include_stats:
            stats = usage_line(m_info)
            if stats:
                lines.append(f"（{stats}）")
        out.append(f"## {role}\n" + "\n".join(lines) + "\n")

    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", required=True, type=Path)
    ap.add_argument("--out", dest="dst", required=True, type=Path)
    ap.add_argument("--agora-id", required=True)
    ap.add_argument("--source-dir", default=None, help="來源 session 所屬的專案目錄")
    ap.add_argument("--relation", default="import")
    ap.add_argument("--parent", action="append", default=[])
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--note", default="")
    ap.add_argument("--case", default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--host", default="unknown")
    ap.add_argument("--raw-file", default=None)
    ap.add_argument("--raw-md5", default=None)
    ap.add_argument("--raw-size", type=int, default=None)
    args = ap.parse_args(argv)

    if not args.agora_id.startswith("agora:"):
        print(f"agora id 必須帶實體前綴 agora:（{args.agora_id}）", file=sys.stderr)
        return 2
    payload = json.loads(args.src.read_text(encoding="utf-8"))
    text = to_reading(payload, agora_id=args.agora_id, source_dir=args.source_dir,
                      relation=args.relation, parents=list(args.parent),
                      refs=list(args.ref), tags=list(args.tag), note=args.note,
                      case=args.case, title=args.title, host=args.host,
                      raw_file=args.raw_file, raw_md5=args.raw_md5,
                      raw_size=args.raw_size)
    args.dst.write_text(text, encoding="utf-8")
    turns = sum(1 for m in payload["messages"]
                if (m.get("info") or {}).get("role") in ("user", "assistant"))
    print(f"{args.dst} agora_id={args.agora_id} turns={turns} "
          f"bytes={len(text.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
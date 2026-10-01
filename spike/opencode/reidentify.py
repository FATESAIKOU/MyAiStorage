#!/usr/bin/env python3
"""把 `opencode export` 的 JSON 重編成新 session 的匯出檔（spike V1）。

做法沿用期 1（`docs/spike/session-import.md`、main 的
`src/aistorage/adapters/opencode/loader.py`）：**session／message／part 三種 id
全部重編成固定寬度**，其餘欄位一個位元組都不動。

為什麼三種都要改：`opencode import` 對已存在的 id 是 onConflictDoNothing
**靜默丟棄**（rc=0、不報錯）。所以沿用原 id 匯入第二次得到空 session；而自己
編的 id 若會被截斷到不唯一，同一批訊息就會拿到同一個 id，一樣被靜默丟棄。
固定寬度（12 碼時間 + 6 碼序號 + 6 碼鹽 = 28 個字元，後綴不長過 `_ID_MAX = 30`）
讓「唯一」不再靠運氣。

id 的字典序必須與匯出檔的順序一致：opencode 匯出訊息是
`ORDER BY time_created, id`、part 是 `ORDER BY message_id, id`，`time.created`
相同時順序由 id 決定；送給模型的上下文也照這個順序。

用法：
    reidentify.py --in orig.json --out new.json --session-id ses_xxxxxxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

#: opencode 自己產生的 id 是 29～30 個字元（期 1 Q6）；超過就報錯而不是截斷。
_ID_MAX = 30
_TIME_HEX = 12  # time.created 是毫秒，12 碼 16 進位 = 48 bits
_ORDINAL_HEX = 6  # 單一匯入最多 2^24 則訊息／單一訊息最多 2^24 個 part
_SALT_HEX = 6  # 24 bits：不同匯入（重跑、1→n、n→1）拿到不同 id


class ReidentifyError(RuntimeError):
    pass


def salt_for(session_id: str) -> str:
    """由新 session id 推導鹽（不隨機）：同一個來源重跑算出一模一樣的 id，
    匯入因此是 no-op，不會把同一段歷史複製一份。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_SALT_HEX]


def _make_id(prefix: str, *, time_ms: int, ordinal: int, salt: str) -> str:
    stamp = f"{max(int(time_ms), 0) & ((1 << (4 * _TIME_HEX)) - 1):0{_TIME_HEX}x}"
    ident = f"{prefix}_{stamp}{ordinal:0{_ORDINAL_HEX}x}{salt}"
    if len(ident) > _ID_MAX:
        raise ReidentifyError(f"重編出來的 id 有 {len(ident)} 個字元，超過 {_ID_MAX}")
    return ident


def _created_ms(info: dict, fallback: int) -> int:
    created = (info.get("time") or {}).get("created")
    return created if isinstance(created, int) else fallback


def reidentify(payload: dict, *, session_id: str, salt: str | None = None,
               only_session_id: bool = False) -> dict:
    """重編整份匯出檔。`only_session_id=True` 是 V1(b) 的對照組：
    **只換 session id**，message／part 的 id 原樣留著——用來實測 `import`
    對重複 id 的反應。"""
    out = json.loads(json.dumps(payload))  # 深拷貝，不動輸入
    salt = salt or salt_for(session_id)

    info = dict(out.get("info") or {})
    info["id"] = session_id
    info["title"] = f"{info.get('title') or 'agora'} (imported)"
    out["info"] = info

    messages = out.get("messages")
    if not isinstance(messages, list):
        raise ReidentifyError("匯出檔缺少 messages 清單")

    seen: set[str] = set()
    message_map: dict[str, str] = {}
    previous_time = 0
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not isinstance(m.get("info"), dict):
            raise ReidentifyError(f"第 {i} 則訊息形狀不對（缺 info 物件）")
        m_info = dict(m["info"])
        old_id = str(m_info.get("id"))
        previous_time = _created_ms(m_info, previous_time)
        if only_session_id:
            # V1(b) 對照組：id 原樣留著，只換 sessionID。
            message_map[old_id] = old_id
            new_id = old_id
        else:
            new_id = _make_id("msg", time_ms=previous_time, ordinal=i, salt=salt)
        if new_id in seen:  # pragma: no cover - 固定寬度 + 遞增序號不可能撞
            raise ReidentifyError(f"重編出重複 id：{new_id}")
        seen.add(new_id)
        message_map[old_id] = new_id
        m_info["id"] = new_id
        m_info["sessionID"] = session_id
        parent = m_info.get("parentID")
        if isinstance(parent, str) and parent in message_map:
            m_info["parentID"] = message_map[parent]
        m["info"] = m_info
        part_ids: set[str] = set()
        for j, p in enumerate(m.get("parts") or []):
            new_part_id = (
                str(p.get("id")) if only_session_id
                else _make_id("prt", time_ms=previous_time, ordinal=j, salt=salt))
            if new_part_id in part_ids:  # pragma: no cover
                raise ReidentifyError(f"同一則訊息裡重複的 part id：{new_part_id}")
            part_ids.add(new_part_id)
            p["id"] = new_part_id
            p["sessionID"] = session_id
            p["messageID"] = new_id
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", required=True, type=Path)
    ap.add_argument("--out", dest="dst", required=True, type=Path)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--salt", default=None,
                    help="固定寬度 16 進位鹽；預設由 --session-id 推導")
    ap.add_argument("--only-session-id", action="store_true",
                    help="V1(b) 對照組：只換 session id，message／part id 不動")
    args = ap.parse_args(argv)

    if not args.session_id.startswith("ses_"):
        print(f"session id 必須以 ses_ 開頭（opencode import 會擋）：{args.session_id}",
              file=sys.stderr)
        return 2
    payload = json.loads(args.src.read_text(encoding="utf-8"))
    out = reidentify(payload, session_id=args.session_id, salt=args.salt,
                     only_session_id=args.only_session_id)
    args.dst.write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    first_part_id = next((p["id"] for m in out["messages"]
                          for p in (m.get("parts") or [])), "-")
    print(f"session={args.session_id} messages={len(out['messages'])} "
          f"parts={sum(len(m.get('parts') or []) for m in out['messages'])} "
          f"msg_id_len={len(next(m['info']['id'] for m in out['messages']))} "
          f"part_id_len={len(first_part_id)} -> {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
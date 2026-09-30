"""把 opencode export JSON 截斷成可匯入的新 session（id 全部重編）。

只讀自己造的測試 session，不碰真實 Session。

用法：
  python3 make_import.py SEED.json OUT.json \
    --session-id ses_XXX --title T --take 2 --tag IMPA

規則（opencode 1.18.32 實測，見 docs/spike/session-import.md）：
  - 只取前 --take 則 message；session / message / part id 全部換新
    （沿用 ses_/msg_/prt_ 前綴）。
  - parentID 與 part.messageID 按對應表改寫；其餘內容位元組不動。
  - 同一份截斷內容匯入 n 次，每次都要用不同的 --tag 跑一次
    （沿用原 id 的第二次匯入會被靜默丟棄，新 session 是空的）。
"""

import argparse
import copy
import json


def build(seed, session_id, title, take, tag):
    d = copy.deepcopy(seed)
    msgs = d["messages"][:take]
    if len(msgs) < take:
        raise SystemExit(f"take={take} 超過 seed 訊息數 {len(d['messages'])}")
    d["info"]["id"] = session_id
    d["info"]["title"] = title
    msgmap = {}
    for i, m in enumerate(msgs):
        old = m["info"]["id"]
        new = f"msg_{tag}{i:020d}"[:30]
        msgmap[old] = new
        m["info"]["id"] = new
        m["info"]["sessionID"] = session_id
        if m["info"].get("parentID") in msgmap:
            m["info"]["parentID"] = msgmap[m["info"]["parentID"]]
        for j, p in enumerate(m["parts"]):
            p["id"] = f"prt_{tag}{i:010d}{j:06d}"[:30]
            p["sessionID"] = session_id
            p["messageID"] = new
    d["messages"] = msgs
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed")
    ap.add_argument("out")
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--take", type=int, required=True)
    ap.add_argument("--tag", default="IMPS1")
    args = ap.parse_args()
    seed = json.load(open(args.seed))
    out = build(seed, args.session_id, args.title, args.take, args.tag)
    json.dump(out, open(args.out, "w"))
    print(f"wrote {args.out}: {len(out['messages'])} msgs, session {args.session_id}")


if __name__ == "__main__":
    main()

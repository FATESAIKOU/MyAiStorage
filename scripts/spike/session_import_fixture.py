"""造一份**形狀真實**的 opencode 匯出檔（`{info, messages}`），內容自編。

給兩個地方用：

- `docs/spike/session-import.md` 的匯入／重播驗證（spike 原本用手造的 SEED／SMOKE）；
- `tests/integration/test_opencode_load_roundtrip.py`：起點包裡的原始紀錄。

**為什麼要專門造一份**：opencode 1.18.32 的 `import` 會用 zod 驗每一則訊息與
每一個 part（`decodeUnknownSync`），缺 `slug`／`agent`／`model`／`step-start` 之類
的欄位就直接拒絕整份匯入——所以「形狀對得上」不是可選項，測試才不會測到假的東西。
這份樣本涵蓋匯入驗證會碰到的每一種 part：`text`、`reasoning`、`tool`（含
`callID` 與 `state`）、`step-start`、`step-finish`，而且是多回合 assistant
（其中一則有兩個工具呼叫），正是 9.1 掉到「只剩 1 則」時該被擋下的形狀。

用法：`python3 session_import_fixture.py <輸出檔> [session id]`
（預設 9 則訊息、30 個 part；`--take N` 可只取前 N 則當接續點用的快照）

不碰任何真實 Session：id、時間、內容都由固定種子產生。
"""
import json
import random
import sys

NATIVE = "ses_impl2fixture01"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def rid(rng, prefix, t):
    tail = "".join(rng.choice(ALPHABET) for _ in range(13))
    return f"{prefix}_{t:012x}{tail}"


def text_part(rng, sid, mid, t, text):
    return {"type": "text", "time": {"start": t, "end": t + 40},
            "id": rid(rng, "prt", t), "sessionID": sid, "messageID": mid,
            "text": text}


def tool_part(rng, sid, mid, t, name, args, output):
    call = rid(rng, "x", t)[4:16]
    return {"type": "tool", "callID": f"call_function_{call}_{rng.randrange(9)}",
            "tool": name, "state": {
                "status": "completed", "input": args, "output": output,
                "metadata": {"output": len(output)},
                "title": f"{name} 執行完畢",
                "time": {"start": t, "end": t + 120}},
            "id": rid(rng, "prt", t), "sessionID": sid, "messageID": mid}


def build(session_id: str = NATIVE):
    # 種子連 session id 一起算：同一個 id 每次都產生**一樣**的位元組（可重現），
    # 但兩個不同的 id 拿到不同的 message／part id——opencode 的 id 是 DB 的全域
    # 主鍵，撞了匯入會被靜默丟棄（Q6），同一個 DB 裡不能造兩份一樣的 id。
    rng = random.Random(20260930 + sum(ord(c) for c in session_id))
    sid = session_id
    t0 = 1790700000000
    messages = []

    def user(text, t):
        mid = rid(rng, "msg", t)
        messages.append({
            "info": {"id": mid, "sessionID": sid, "role": "user",
                     "time": {"created": t},
                     "agent": "build",
                     "model": {"providerID": "opencode",
                               "modelID": "space-bunny-free"},
                     "summary": {"diffs": []}},
            "parts": [text_part(rng, sid, mid, t, text)],
        })
        return mid

    def assistant(make_parts, t, *, reasoning=True, tokens=1000):
        """`make_parts(mid, t)` 在訊息 id 確定**之後**才組 part（messageID 要
        指向自己，opencode 匯出時是依 messageID _group_ 的）。"""
        mid = rid(rng, "msg", t)
        seq = [ {"type": "step-start", "id": rid(rng, "prt", t), "sessionID": sid,
                 "messageID": mid} ]
        if reasoning:
            seq.append({"type": "reasoning", "time": {"start": t + 10, "end": t + 60},
                        "id": rid(rng, "prt", t + 20), "sessionID": sid,
                        "messageID": mid, "text": "先想一下：這一步要做的是確認檔案內容。"})
        seq.extend(make_parts(mid, t))
        seq.append({"type": "step-finish", "reason": "stop",
                    "tokens": {"total": tokens, "input": tokens - 20,
                               "output": 20, "reasoning": 0,
                               "cache": {"read": tokens - 100, "write": 0}},
                    "cost": 0, "id": rid(rng, "prt", t + 900), "sessionID": sid,
                    "messageID": mid})
        messages.append({
            "info": {"id": mid, "sessionID": sid, "role": "assistant",
                     "parentID": messages[-1]["info"]["id"],
                     "time": {"created": t, "completed": t + 950},
                     "mode": "build", "agent": "build",
                     "path": {"cwd": "/work", "root": "/"},
                     "cost": 0,
                     "tokens": {"total": tokens, "input": tokens - 20,
                                "output": 20, "reasoning": 0,
                                "cache": {"read": tokens - 100, "write": 0}},
                     "modelID": "space-bunny-free", "providerID": "opencode",
                     "finish": "stop"},
            "parts": seq,
        })
        return mid

    t = t0
    user("幫我讀 /work/fixture.txt，然後把第一行回報給我。", t)
    mid = messages[-1]["info"]["id"]
    t += 500
    assistant(lambda mid, t: [text_part(rng, sid, mid, t + 100, "我先讀檔。"),
                              tool_part(rng, sid, mid, t + 200, "read",
                                        {"filePath": "/work/fixture.txt"},
                                        "ALPHA-123 是這份 fixture 的第一行。\n")], t)
    t += 1000
    assistant(lambda mid, t: [text_part(rng, sid, mid, t + 100,
                                        "第一行是 ALPHA-123，我已經讀到了。")], t)
    t += 1000
    user("再跑一次 `echo TOOL-CHECK-789` 讓我確認工具可以用。", t)
    mid = messages[-1]["info"]["id"]
    t += 500
    assistant(lambda mid, t: [tool_part(rng, sid, mid, t + 200, "bash",
                                        {"command": "echo TOOL-CHECK-789"},
                                        "TOOL-CHECK-789\n")], t)
    t += 1000
    assistant(lambda mid, t: [text_part(rng, sid, mid, t + 100,
                                        "TOOL-CHECK-789 印出來了，工具鏈正常。")], t)
    t += 1000
    user("最後一件事：把兩個檔案都列出來。", t)
    mid = messages[-1]["info"]["id"]
    t += 500
    assistant(lambda mid, t: [
        tool_part(rng, sid, mid, t + 150, "bash", {"command": "ls -1 /work"},
                  "a.txt\nb.txt\n"),
        tool_part(rng, sid, mid, t + 250, "bash", {"command": "wc -l /work/a.txt"},
                  "3 /work/a.txt\n"),
        text_part(rng, sid, mid, t + 350, "兩個檔案都在，a.txt 有 3 行。"),
    ], t)
    t += 1000
    assistant(lambda mid, t: [text_part(rng, sid, mid, t + 100,
                                        "都確認完了，沒有其他事。")], t)

    return {
        "info": {"id": sid, "slug": "impl2-fixture", "projectID": "global",
                 "directory": "/work", "path": "work",
                 "title": f"{sid} 的匯入測試", "agent": "build",
                 "model": {"id": "space-bunny-free", "providerID": "opencode",
                           "variant": "default"},
                 "version": "1.18.32",
                 "summary": {"additions": 0, "deletions": 0, "files": 0},
                 "cost": 0,
                 "tokens": {"input": 1200, "output": 300, "reasoning": 0,
                            "cache": {"read": 900, "write": 0}},
                 "time": {"created": t0, "updated": t}},
        "messages": messages,
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "fixture.json"
    native = sys.argv[2] if len(sys.argv) > 2 else NATIVE
    doc = build(session_id=native)
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(json.dumps({"session_id": native,
                      "messages": len(doc["messages"]),
                      "parts": sum(len(m["parts"]) for m in doc["messages"]),
                      "bytes": len(text.encode())}))

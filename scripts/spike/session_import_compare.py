"""比較兩次 stub 抓到的模型請求：是否位元組相同、共同前綴有多長。

用法：
  python3 session_import_compare.py A.json B.json [--skill-prefix P1 P2 ...]
--skill-prefix：把 system prompt 裡會因重名 skill 解析而翻轉的路徑前綴
正規化掉（只用於診斷環境噪音；正式判定以原始位位元組為準）。

另外提供 `export_prefix_bytes()`：不呼叫模型，直接把一份 opencode 匯出檔的
訊息序列重播成「送給模型的 messages 陣列」並序列化。這是**位元組相同這個量的
決定性來源**——spike Q2／Q3 實測只要這個位元組相同，opencode import 出去之後
送模型的開頭就位元組相同（歷史重播保留原 `tool_call id` 與工具結果）。
整合測試用它在沒有真的 opencode 行程的情況下驗同一件事。
"""

import argparse
import json
import sys


def export_prefix_bytes(export, tool_call_id_key="id"):
    """把 opencode 匯出檔重播成送給模型的 messages 陣列（bytes）。

    形狀照 Q2 抓到的 OpenAI-compatible chat completions：system 與 tools 不在
    這個陣列裡。

    `tool_call_id_key`：
    - `"id"`（預設）用 tool part 自己的 `callID`（**真實的 opencode 匯出檔有這個
      欄位**，值就是 `call_function_…`）。Q2 實測 opencode 重播時保留原值、
      不重編，所以兩邊算出來的一樣——這是位元組相同能成立的原因。
    - 傳 None 則**略過** id 欄位。給沒有 `callID` 的合成樣本用（例如
      `tests/unit/data/converters/opencode/basic.json`）：那種匯出檔的
      tool_call id 是 opencode 依訊息 id 現算出來的，重編 id 之後本來就會不同，
      拿它當「位元組相同」的判準會量到無關的東西。
    - 沒有 `callID` 也沒有指定 key 時，退回 `call_<message id>_<序號>`。
    """
    out = []
    for message in export.get("messages", []):
        info = message.get("info") or {}
        role = info.get("role")
        parts = message.get("parts") or []
        texts = [p.get("text") for p in parts
                 if p.get("type") == "text" and p.get("text")]
        tools = [p for p in parts if p.get("type") == "tool"]
        if role == "assistant" and tools:
            for index, part in enumerate(tools):
                state = part.get("state") or {}
                call_id = part.get("callID") or "call_%s_%d" % (
                    info.get("id"), index)
                call = {
                    "type": "function",
                    "function": {
                        "name": part.get("tool"),
                        "arguments": json.dumps(state.get("input"),
                                                ensure_ascii=False,
                                                sort_keys=True),
                    },
                }
                if tool_call_id_key:
                    call[tool_call_id_key] = call_id
                if index == 0 and texts:
                    out.append({"role": "assistant", "content": texts[0],
                                "tool_calls": [call]})
                else:
                    out.append({"role": "assistant", "content": None,
                                "tool_calls": [call]})
                tool_result = {
                    "role": "tool",
                    "content": json.dumps(
                        state.get("output") if state.get("status") != "error"
                        else {"error": state.get("error")},
                        ensure_ascii=False, sort_keys=True),
                }
                if tool_call_id_key:
                    tool_result["tool_call_id"] = call_id
                out.append(tool_result)
            rest = texts[1:] if texts else []
            if rest:
                out.append({"role": "assistant", "content": "\n".join(rest)})
        else:
            out.append({"role": role, "content": "\n".join(texts)})
    return json.dumps(out, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--skill-prefix", nargs="*", default=[])
    args = ap.parse_args()
    ra = open(args.a, "rb").read()
    rb = open(args.b, "rb").read()
    print(f"A={args.a} {len(ra)} bytes")
    print(f"B={args.b} {len(rb)} bytes")
    print("FULL byte-identical:", ra == rb)
    if ra != rb:
        i = next((i for i, (x, y) in enumerate(zip(ra, rb)) if x != y), min(len(ra), len(rb)))
        print(f"first diff at byte {i} of {len(ra)}/{len(rb)}")
        print("A:", ra[max(0, i - 120) : i + 200])
        print("B:", rb[max(0, i - 120) : i + 200])
    if args.skill_prefix:
        sa, sb = ra.decode(), rb.decode()
        for p in args.skill_prefix:
            sa = sa.replace(p, "SK/")
            sb = sb.replace(p, "SK/")
        print("normalized-identical:", sa == sb)


if __name__ == "__main__":
    main()

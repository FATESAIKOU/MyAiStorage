"""9.1 分裂的**前半段**（S1 呼叫 agora_handoff → 同步器上傳 → 提交流程）。

`test_split.py` 的完整 9.1 還需要 S2／S3 的容器、`agora checkout` 與
`agora-opencode load`，那些不在這裡。這個檔只驗到「S1 真的呼叫了工具、交接單真的
進了收件匣、提交流程把它收進 Agora」為止，並把提交流程卡在哪裡印出來。
"""

from __future__ import annotations

import json

import pytest

from aistorage.schema import generate_ulid

from .conftest import assert_tool_called


@pytest.mark.e2e
def test_9_1_split_front_half(resident_pool, run_committer, e2e_settings, e2e_reader):
    canary = f"CANARY-91-{generate_ulid()}"

    # 1. S1 容器；第一句只是識別碼（必須落在接續點之前且已完成）
    c1 = resident_pool("e2e-s1")
    s1_id = c1.create_session()
    c1.send(s1_id, f"請只回覆 ACK-1，不要呼叫任何工具。識別碼：{canary}")

    # 2. 让 AI 用 agora_handoff 寫兩張交接單。
    #    容器裡沒有 gh-pat-actions.txt，所以工具不會觸發 workflow，會「等讀取介面」
    #    （ADR 0007）——這正是我們要驗的：等待期間由測試端的提交流程收進去。
    #    指令不要假設容器裡有專案：實測 space-bunny-free 會因為「沒有程式碼」
    #    而拒絕捏造架構，然後整個不呼叫工具。
    s1_id, _resp = c1.prompt_with_commits(
        "請用 agora_handoff 把接下來兩件工作交出去，交給兩個不同的 Session 各做一件"
        "（tasks 給兩個物件，各要有 title、summary、next_steps）："
        "1. 核對 /work/schemas 底下 6 份 JSON Schema 的欄位，列出跟 readview-manifest.json 不一致的地方。"
        "2. 為 aistorage skill 的工具各寫一段中文使用說明，說明參數與回傳。"
        "完成後請回報交接單的 handoff_id。",
        run_committer,
        session_id=s1_id,
    )

    # 3. AI 真的呼叫了工具嗎（從 opencode export 確認，不信任模型的文字）
    parts = assert_tool_called(c1, s1_id, "agora_handoff", count=1)
    output = parts[0].get("output")
    print("[9.1 前半段] 工具輸出：", json.dumps(output, ensure_ascii=False)[:400])


    # 4. 同步器與提交流程：prompt_with_commits 期間已經跑過，這裡再確認一次現況
    sync = c1.sync_once([s1_id])
    print("[9.1 前半段] 同步器：", sync.stdout.strip()[:400], sync.stderr.strip()[:200])
    report = run_committer()
    print("[9.1 前半段] 提交流程：", report.stdout.strip().splitlines()[-1:])

    # 5. 讀取端看得到 S1 與那兩張交接單了嗎
    view = e2e_reader.get_session(f"opencode:{s1_id}")
    print("[9.1 前半段] 讀取端 session status：", view.value.session.status)
    assert view.value.session.session_id == f"opencode:{s1_id}"

    author = f"opencode:{s1_id}"
    mine = [
        h for h in e2e_reader.list_open_handoffs().value
        if h.author_session_id == author
    ]
    titles = [json.loads(h.body_json or "{}").get("title") for h in mine]
    print("[9.1 前半段] 讀取端看得到的交接單：", titles)
    assert len(mine) == 2, f"預期 2 張交接單，實際 {len(mine)}：{titles}"

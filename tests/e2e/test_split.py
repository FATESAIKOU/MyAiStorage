"""Task 9.1 End-to-End Acceptance Test: Split (1 -> n).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.1
- Design D10 (Split via Handoff and Pinned Continuation Points)
- S1 in independent resident container discusses topics, calls aistorage_split with 2 handoffs, and syncs
- Committer runs locally to commit Agora true store and publish read view
- S2 and S3 start in separate resident containers, discover handoffs exclusively via reader interface
- S2 and S3 claim one handoff each; reader confirms link established
- Continuation points strictly bound to pinned snapshot
- S1 continues running independently; subsequent /undo and edits in S1 do not alter S2's pinned continuation
"""

from __future__ import annotations

import pytest

from aistorage.reader import AgoraReader, Query


@pytest.mark.e2e
def test_9_1_split_1_to_n(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.1 分裂（1→n）：S1 建立兩張交接單，S2、S3 各自認領，S1 /undo 不影響接續點。"""
    # 1. 在獨立容器啟動 S1
    c1 = resident_pool("e2e-s1")
    s1_resp = c1.prompt(
        "我們即將開始專案設計。請簡述後端架構與前端架構，"
        "並使用工具 aistorage_split 分裂成兩項子任務交接單："
        "1. 後端架構實作 (Backend Implementation)"
        "2. 前端介面設計 (Frontend Design)。"
        "完成後請回報交接單摘要。"
    )
    assert s1_resp is not None
    s1_sessions = c1.list_sessions()
    assert len(s1_sessions) >= 1
    s1_id = s1_sessions[0]["id"]

    # 2. 執行提交流程，將 S1 的原始紀錄與兩張交接單收進真本並發布讀取視圖
    run_committer()

    # 3. 讀取介面是 S2、S3 的唯一資訊來源：列出待認領交接單
    open_handoffs_res = e2e_reader.list_open_handoffs()
    open_handoffs = open_handoffs_res.value
    # 必須包含 S1 產生的兩張交接單
    s1_handoffs = [h for h in open_handoffs if h.author_session_id == s1_id]
    assert len(s1_handoffs) == 2, f"預期 S1 產生 2 張交接單，實際找到 {len(s1_handoffs)}"
    h1, h2 = s1_handoffs[0], s1_handoffs[1]

    # 4. 在獨立容器中啟動 S2 與 S3，各自認領一張交接單
    c2 = resident_pool("e2e-s2")
    c3 = resident_pool("e2e-s3")

    # S2 認領 h1
    c2_resp = c2.prompt(
        f"你是接續 Session S2。請使用 aistorage_claim 工具認領交接單 '{h1.handoff_id}'，"
        "並確認取得接續內容。"
    )
    assert c2_resp is not None
    s2_id = c2.list_sessions()[0]["id"]

    # S3 認領 h2
    c3_resp = c3.prompt(
        f"你是接續 Session S3。請使用 aistorage_claim 工具認領交接單 '{h2.handoff_id}'，"
        "並確認取得接續內容。"
    )
    assert c3_resp is not None
    s3_id = c3.list_sessions()[0]["id"]

    # 5. 提交流程處理認領單並建立接續 Link
    run_committer()

    # 6. 讀取端驗證交接單已成功認領，且 S2 讀取接續內容
    continuation_s2_res = e2e_reader.get_continuation(h1.handoff_id)
    assert continuation_s2_res is not None
    cont_s2 = continuation_s2_res.value
    assert cont_s2.handoff.claimed_by_session_id == s2_id
    # 接續點快照必須包含 S1 分裂前的訊息
    assert len(cont_s2.messages_before) > 0

    continuation_s3_res = e2e_reader.get_continuation(h2.handoff_id)
    assert continuation_s3_res is not None
    cont_s3 = continuation_s3_res.value
    assert cont_s3.handoff.claimed_by_session_id == s3_id

    # 7. S1 照常繼續執行不受影響
    s1_continue_resp = c1.prompt("請繼續說明資料庫 migration 計畫。")
    assert s1_continue_resp is not None
    run_committer()

    # 8. 對 S1 進行 /undo 並重新輸入，驗證 S2 承接的內容不變（接續點釘在快照上，D10）
    c1.prompt("/undo")
    c1.prompt("重新輸入：改用 NoSQL 資料庫設計。")
    run_committer()

    # 重新透過讀取介面檢視 S2 的接續內容：快照雜湊與訊息清單依然保持原樣
    cont_s2_after_undo = e2e_reader.get_continuation(h1.handoff_id).value
    assert cont_s2_after_undo.handoff.snapshot_sha256 == cont_s2.handoff.snapshot_sha256
    assert [m["message_id"] for m in cont_s2_after_undo.messages_before] == [
        m["message_id"] for m in cont_s2.messages_before
    ]

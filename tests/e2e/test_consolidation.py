"""Task 9.2 End-to-End Acceptance Test: Consolidation (n -> 1).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.2
- Design D10 (Consolidation: one session claiming multiple handoffs)
- S2 and S3 in independent resident containers yield ends via aistorage_handoff_end
- Committer runs to commit handoffs to Agora true store
- S4 in separate resident container claims both handoffs (consolidation)
- S4 reads continuation contents of both S2 and S3 before their continuation points
- S4 generates a report registered to Foundry (marked xfail pending Stream A Foundry wiring)
- Foundry catalog queryable for S4's artifact and blob content is retrievable
"""

from __future__ import annotations

import pytest

from aistorage.reader import AgoraReader


@pytest.mark.e2e
def test_9_2_consolidation_n_to_1(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.2 統合（n→1）：S2、S3 各自交出末端，S4 認領兩張交接單，產出登錄 Foundry。"""
    # 1. 啟動 S2 與 S3 兩個工作容器
    c2 = resident_pool("e2e-s2-worker")
    c3 = resident_pool("e2e-s3-worker")

    # S2 完成後端實作並交出末端
    c2.prompt("後端實作已完成，包含 RESTful API 與認證模組。請使用 aistorage_handoff_end 交出末端。")
    s2_id = c2.list_sessions()[0]["id"]

    # S3 完成前端實作並交出末端
    c3.prompt("前端設計已完成，包含響應式介面與狀態管理。請使用 aistorage_handoff_end 交出末端。")
    s3_id = c3.list_sessions()[0]["id"]

    # 2. 提交流程將兩份末端交接單收進真本並發布讀取視圖
    run_committer()

    # 3. 讀取介面查詢待認領交接單
    open_res = e2e_reader.list_open_handoffs().value
    h_s2 = next(h for h in open_res if h.author_session_id == s2_id)
    h_s3 = next(h for h in open_res if h.author_session_id == s3_id)
    assert h_s2 is not None
    assert h_s3 is not None

    # 4. 啟動獨立容器 S4 進行統合：認領全部交接單
    c4 = resident_pool("e2e-s4-consolidator")
    claim_prompt = (
        f"你是統合 Session S4。請使用 aistorage_claim 同時認領交接單 '{h_s2.handoff_id}' "
        f"與 '{h_s3.handoff_id}'，完成架構統合工作。"
    )
    c4.prompt(claim_prompt)
    s4_id = c4.list_sessions()[0]["id"]

    # 5. 提交流程處理統合認領
    run_committer()

    # 6. 讀取端驗證 S4 成功統合兩條 Link
    cont_s2 = e2e_reader.get_continuation(h_s2.handoff_id).value
    cont_s3 = e2e_reader.get_continuation(h_s3.handoff_id).value
    assert cont_s2.handoff.claimed_by_session_id == s4_id
    assert cont_s3.handoff.claimed_by_session_id == s4_id

    # 7. S4 讀取 S2、S3 接續點之前的完整閱讀版
    assert len(cont_s2.messages_before) > 0
    assert len(cont_s3.messages_before) > 0

    # 8. S4 產出一份報告登錄進 Foundry
    # PM 指示：9.2 的 Foundry 產出登錄可以先標 xfail（等 A 線接好 Foundry）
    _assert_foundry_artifact_registered(c4, s4_id, run_committer, e2e_reader)


@pytest.mark.xfail(reason="等 A 線接好 Foundry 產出登錄與第 12 步發佈流程", strict=False)
def _assert_foundry_artifact_registered(c4, s4_id: str, run_committer, e2e_reader: AgoraReader):
    """驗證 S4 登錄 Foundry 產出，目錄可查、記錄產出者為 S4 且本體可取得。"""
    c4.prompt(
        "請根據統合結果產生 'architecture-summary.pdf'，並呼叫 aistorage 工具登錄至 Foundry 產出目錄。"
    )
    run_committer()

    # 透過 Foundry 讀取介面查詢產出
    from aistorage.reader.foundry import FoundryCatalogQuery, query_catalog

    # 查詢 S4 產出的產出紀錄
    manifest = e2e_reader._client.manifest()
    db = e2e_reader._client.index()
    try:
        q = FoundryCatalogQuery(produced_by=s4_id)
        entries = query_catalog(db, q)
        assert len(entries) >= 1
        artifact = entries[0]
        assert artifact.produced_by_session_id == s4_id
        assert artifact.name == "architecture-summary.pdf"
        assert artifact.annex_key is not None
    finally:
        db.close()

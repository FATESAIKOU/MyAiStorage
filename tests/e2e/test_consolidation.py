"""Task 9.2 End-to-End Acceptance Test: Consolidation (n -> 1).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.2
- Design D10 (Consolidation: one session claiming multiple handoffs)
- S2 and S3 in independent resident containers yield ends via aistorage_handoff_end
- Committer runs to commit handoffs to Agora true store
- S4 in separate resident container claims both handoffs (consolidation)
- S4 reads continuation contents of both S2 and S3 before their continuation points
"""

from __future__ import annotations

import pytest

from aistorage.reader import AgoraReader

from .conftest import (
    agora_session_id,
    assert_tool_called,
    poll,
)


def _open_handoff_for(reader: AgoraReader, session_id: str):
    hits = [h for h in reader.list_open_handoffs().value if h.author_session_id == session_id]
    return hits[0] if hits else None


def _claimed_ids_from_parts(parts: list[dict]) -> set[str]:
    """從 claim 工具呼叫的輸入取出它認領了哪些交接單（不信任模型的文字）。"""
    out: set[str] = set()
    for part in parts:
        payload = part.get("input")
        if not isinstance(payload, dict):
            continue
        ids = payload.get("handoff_ids")
        if isinstance(ids, list):
            out.update(str(i) for i in ids if isinstance(i, str))
        elif isinstance(ids, str):
            out.add(ids)
    return out


@pytest.mark.e2e
def test_9_2_consolidation_n_to_1(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.2 統合（n→1）：S2、S3 各自交出末端，S4 認領兩張交接單並讀到兩者內容。"""
    # 1. 啟動 S2 與 S3 兩個工作容器，各自交出末端（handoff_end 內部同步並提交）
    c2 = resident_pool("e2e-s2-worker")
    s2_id, _ = c2.prompt_with_commits(
        "後端實作已完成，包含 RESTful API 與認證模組。"
        "請使用 aistorage_handoff_end 交出末端，summary 說明後端成果。",
        run_committer,
    )
    assert_tool_called(c2, s2_id, "aistorage_handoff_end")

    c3 = resident_pool("e2e-s3-worker")
    s3_id, _ = c3.prompt_with_commits(
        "前端設計已完成，包含響應式介面與狀態管理。"
        "請使用 aistorage_handoff_end 交出末端，summary 說明前端成果。",
        run_committer,
    )
    assert_tool_called(c3, s3_id, "aistorage_handoff_end")

    s2_agora = agora_session_id(s2_id)
    s3_agora = agora_session_id(s3_id)

    # 2. 讀取介面查詢待認領交接單（確認 S2、S3 都交出了）
    h_s2 = poll(lambda: _open_handoff_for(e2e_reader, s2_agora), what="S2 的交接單出現")
    h_s3 = poll(lambda: _open_handoff_for(e2e_reader, s3_agora), what="S3 的交接單出現")
    assert h_s2.handoff_id != h_s3.handoff_id

    # 3. 啟動獨立容器 S4 統合：自己透過讀取介面找兩張交接單並全部認領
    c4 = resident_pool("e2e-s4-consolidator")
    s4_id, _ = c4.prompt_with_commits(
        "你是統合 Session S4。請使用 aistorage_list_handoffs 找出目前所有尚未認領的交接單，"
        "然後用 aistorage_claim 一次認領全部（統合），並讀取兩者的接續內容。",
        run_committer,
    )
    claim_parts = assert_tool_called(c4, s4_id, "aistorage_claim")
    claimed_ids = _claimed_ids_from_parts(claim_parts)
    assert {h_s2.handoff_id, h_s3.handoff_id} <= claimed_ids, (
        f"S4 必須在一次呼叫裡認領兩張交接單（統合），工具輸入：{claimed_ids}"
    )
    # 交接單的 id 不是測試餵的：必須來自 S4 自己的 list_handoffs
    list_parts = assert_tool_called(c4, s4_id, "aistorage_list_handoffs")
    listed = "\n".join(str(p.get("output") or "") for p in list_parts)
    for hid in (h_s2.handoff_id, h_s3.handoff_id):
        assert hid in listed, f"S4 認領的 {hid} 必須出現在它自己的 list_handoffs 輸出裡"
    s4_agora = agora_session_id(s4_id)

    # 4. 讀取端驗證 S4 成功統合兩條 Link
    def _both_claimed():
        cont_s2 = e2e_reader.get_continuation(h_s2.handoff_id).value
        cont_s3 = e2e_reader.get_continuation(h_s3.handoff_id).value
        if (
            cont_s2.handoff.claimed_by_session_id == s4_agora
            and cont_s3.handoff.claimed_by_session_id == s4_agora
        ):
            return cont_s2, cont_s3
        return None

    cont_s2, cont_s3 = poll(_both_claimed, what="S4 認領兩張交接單")

    # 5. S4 讀到 S2、S3 接續點之前的內容（非空）
    assert cont_s2.messages, "S2 的接續內容不得為空"
    assert cont_s3.messages, "S3 的接續內容不得為空"

    # S4 的視圖必須有兩條指向 S2、S3 的接續 Link
    s4_view = e2e_reader.get_session(s4_agora).value
    to_sessions = {
        link.to_session_id
        for link in s4_view.links_out
        if link.kind == "continuation"
    }
    assert {s2_agora, s3_agora} <= to_sessions, (
        f"S4 必須有兩條接續 Link 指向 S2、S3，實際 {to_sessions}"
    )

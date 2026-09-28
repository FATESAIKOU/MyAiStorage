"""Task 9.1 End-to-End Acceptance Test: Split (1 -> n).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.1
- Design D10 (Split via Handoff and Pinned Continuation Points)
- S1 in independent resident container discusses topics, calls aistorage_split with 2 handoffs, and syncs
- Committer runs locally to commit Agora true store and publish read view
- S2 and S3 start in separate resident containers, discover handoffs exclusively via reader interface
- S2 and S3 claim one handoff each; reader confirms link established
- Continuation points strictly bound to pinned snapshot
- S1 continues running independently; a real revert (/undo) plus re-edit in S1 does not alter S2's pinned continuation

每一個「要求 AI 呼叫工具」的步驟都用 `assert_tool_called` 從 opencode export 確認
工具真的被呼叫且成功；模型沒照做時以 `ModelDidNotComply` 立即失敗（review：「把
『模型沒有照做』與『系統錯誤』區分開來」）。
一般對話的內容不會自動上傳（同步器每 10 分鐘才跑一次），所以每一步都明確在
容器內執行 `python -m aistorage.syncer opencode once`，再由測試執行本機提交流程；
工具內部的「同步並提交」則由 `prompt_with_commits` 在本機輪流觸發提交流程配合。
"""

from __future__ import annotations

import json

import pytest

from aistorage.reader import AgoraReader
from aistorage.schema import generate_ulid

from .conftest import (
    agora_session_id,
    assert_tool_called,
    poll,
)


def _message_text(message: dict) -> str:
    """把一則閱讀版訊息的所有 part 攤平成文字（比對 canary 用）。"""
    return json.dumps(message, ensure_ascii=False)


def _latest_reading(reader: AgoraReader, session_id: str) -> dict:
    return reader.get_reading(session_id).value


def _message_ids(reading: dict) -> list[str]:
    return [str(m.get("message_id")) for m in reading.get("messages", [])]


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
def test_9_1_split_1_to_n(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.1 分裂（1→n）：S1 建立兩張交接單，S2、S3 各自認領，S1 真的 undo 不影響接續點。"""
    canary = f"CANARY-SPLIT-{generate_ulid()}"

    # 1. 在獨立容器啟動 S1；先送 canary，確認它已完成再送出分裂指令
    #    （canary 必須落在接續點之前、且是已完成的訊息；分兩步最穩定）
    c1 = resident_pool("e2e-s1")
    s1_id = c1.create_session()
    c1.send(s1_id, "請只回覆 ACK-1，不要呼叫任何工具。這句是後續的識別碼：" + canary)
    s1_id, _ = c1.prompt_with_commits(
        # 不要假設容器裡有專案：實測 space-bunny-free 會因為「沒有程式碼」
        # 而拒絕捏造架構，然後整個不呼叫工具。
        "請呼叫 aistorage_split 把接下來兩件工作分裂成交接單，交給兩個不同的 Session 各做一件："
        "1. 核對 /work/schemas 底下 6 份 JSON Schema 的欄位，列出跟 readview-manifest.json 不一致的地方。"
        "2. 為 aistorage skill 的工具各寫一段中文使用說明，說明參數與回傳。"
        "parts 給一個含兩個物件的清單，每個物件要有 title、summary、next_steps。"
        "完成後請回報交接單摘要。",
        run_committer,
        session_id=s1_id,
    )
    s1_agora = agora_session_id(s1_id)
    assert_tool_called(c1, s1_id, "aistorage_split")

    # 2. 讀取介面是 S2、S3 的唯一資訊來源：列出待認領交接單
    def _s1_handoffs():
        found = [
            h
            for h in e2e_reader.list_open_handoffs().value
            if h.author_session_id == s1_agora
        ]
        return found if len(found) == 2 else None

    s1_handoffs = poll(_s1_handoffs, what="S1 的兩張交接單出現在讀取視圖")
    h1, h2 = s1_handoffs[0], s1_handoffs[1]
    handoff_ids = {h1.handoff_id, h2.handoff_id}

    # 3. 接續點包含 canary：釘住的快照裡接續點之前（含接續點）必須有它
    cont_h1_before = e2e_reader.get_continuation(h1.handoff_id).value
    assert cont_h1_before.messages, "接續點之前必須有訊息"
    assert any(canary in _message_text(m) for m in cont_h1_before.messages), (
        "接續點之前的訊息必須包含 canary"
    )
    assert cont_h1_before.handoff.message_id in [
        m["message_id"] for m in cont_h1_before.messages
    ], "接續點必須落在 messages_before 的最後一則"

    # 4. 依序啟動 S2、S3（確定性：等 S2 的認領在讀取視圖可見後再開 S3，
    #    這樣「尚未認領」清單裡只剩另一張，S3 不可能搶到同一張）
    c2 = resident_pool("e2e-s2")
    s2_id, _ = c2.prompt_with_commits(
        "你是接續 Session S2。請先使用 aistorage_list_handoffs 找出尚未認領的交接單，"
        "選其中一張使用 aistorage_claim 認領，並確認取得接續內容。",
        run_committer,
    )
    claim_parts = assert_tool_called(c2, s2_id, "aistorage_claim")
    s2_claimed = _claimed_ids_from_parts(claim_parts)
    assert len(s2_claimed) == 1 and s2_claimed <= handoff_ids, (
        f"S2 必須認領清單中的一張交接單，工具輸入：{s2_claimed}"
    )
    # 交接單的 id 不是測試餵給模型的：它必須來自 aistorage_list_handoffs 的輸出
    list_parts_s2 = assert_tool_called(c2, s2_id, "aistorage_list_handoffs")
    listed_s2 = "\n".join(str(p.get("output") or "") for p in list_parts_s2)
    for hid in s2_claimed:
        assert hid in listed_s2, (
            f"S2 認領的 {hid} 必須出現在它自己的 list_handoffs 輸出裡（唯一資訊來源是讀取介面）"
        )
    poll(
        lambda: len(e2e_reader.list_open_handoffs().value) == 1,
        what="S2 認領後只剩一張待認領",
    )

    c3 = resident_pool("e2e-s3")
    s3_id, _ = c3.prompt_with_commits(
        "你是接續 Session S3。請先使用 aistorage_list_handoffs 找出尚未認領的交接單，"
        "選另一張使用 aistorage_claim 認領，並確認取得接續內容。",
        run_committer,
    )
    claim_parts_s3 = assert_tool_called(c3, s3_id, "aistorage_claim")
    s3_claimed = _claimed_ids_from_parts(claim_parts_s3)
    assert len(s3_claimed) == 1 and s3_claimed <= handoff_ids, (
        f"S3 必須認領清單中的一張交接單，工具輸入：{s3_claimed}"
    )
    assert s2_claimed != s3_claimed, "S2 與 S3 必須各認領不同的交接單"

    s2_agora = agora_session_id(s2_id)
    s3_agora = agora_session_id(s3_id)
    assert s2_agora != s3_agora, "S2 與 S3 必須是不同 Session"

    # 5. 兩張交接單都被認領，而且是 S2、S3 各認領一張；接續點讀得到 canary
    def _claims() -> dict[str, str | None] | None:
        rows = {
            h.handoff_id: e2e_reader.get_continuation(h.handoff_id).value.handoff
            for h in (h1, h2)
        }
        if all(row.claimed_by_session_id for row in rows.values()):
            return {k: v.claimed_by_session_id for k, v in rows.items()}
        return None

    claims = poll(_claims, what="兩張交接單都被認領")
    assert set(claims.values()) == {s2_agora, s3_agora}, (
        f"預期 S2、S3 各認領一張，實際 claimed_by={claims}"
    )
    for hid in handoff_ids:
        cont = e2e_reader.get_continuation(hid).value
        assert cont.messages, f"{hid} 的接續點之前必須有訊息"
        assert any(canary in _message_text(m) for m in cont.messages)

    cont_s2_before = e2e_reader.get_continuation(h1.handoff_id).value
    cont_s3_before = e2e_reader.get_continuation(h2.handoff_id).value

    # 6. S1 照常繼續、不受影響：明確同步→提交，確認最新快照改變且不是 stopped
    view_s1_before = e2e_reader.get_session(s1_agora).value
    sha_before = view_s1_before.session.raw_sha256
    c1.prompt("請繼續說明資料庫 migration 計畫。", session_id=s1_id)
    c1.sync_once([s1_id])
    run_committer()
    view_s1_after = poll(
        lambda: (
            v
            if (v := e2e_reader.get_session(s1_agora).value).session.raw_sha256
            != sha_before
            else None
        ),
        what="S1 的新內容已提交（raw_sha256 改變）",
    )
    assert view_s1_after.session.status != "stopped"
    assert not [
        link for link in view_s1_after.links_out if link.kind == "continuation"
    ], "S1 不應該自己發出接續 Link"

    # 7. 對 S1 做真正的 /undo：API revert 到**接續點那一則訊息**，再重新輸入
    #    （1.7c：等效 /undo；revert 之後重送會刪掉該訊息之後的版本，
    #     連接續點本身都被刪掉——但接續點釘在更早的快照上，不受影響）
    export = c1.export(s1_id)
    all_ids = {
        str(m["info"]["id"])
        for m in export["messages"]
        if isinstance(m.get("info"), dict) and m["info"].get("id")
    }
    pinned_message_id = cont_s2_before.handoff.message_id
    assert pinned_message_id in all_ids, (
        f"接續點訊息 {pinned_message_id} 不在 S1 的匯出裡（現有：{sorted(all_ids)}）"
    )
    c1.revert(s1_id, pinned_message_id)
    c1.prompt("重新輸入：改用 NoSQL 資料庫設計。", session_id=s1_id)

    # 8. 最新閱讀版不再含有接續點那則訊息，並出現重新輸入的內容
    c1.sync_once([s1_id])
    run_committer()

    def _reading_without_pinned() -> dict | None:
        reading = _latest_reading(e2e_reader, s1_agora)
        if pinned_message_id in _message_ids(reading):
            return None
        return reading

    reading_after = poll(
        _reading_without_pinned, what="S1 最新閱讀版不再含有被 undo 的訊息"
    )
    assert any("NoSQL" in _message_text(m) for m in reading_after["messages"]), (
        "重新輸入的內容必須出現在最新閱讀版"
    )
    view_s1_final = e2e_reader.get_session(s1_agora).value
    assert view_s1_final.session.raw_sha256 != sha_before, "undo 之後 S1 必須是新快照"

    # 9. S2、S3 的接續內容不變：同一份釘住的快照、同一批訊息、canary 還在
    cont_s2_after = e2e_reader.get_continuation(h1.handoff_id).value
    cont_s3_after = e2e_reader.get_continuation(h2.handoff_id).value
    assert cont_s2_after.handoff.snapshot_sha256 == cont_s2_before.handoff.snapshot_sha256, (
        "接續點必須釘在被釘住的快照上，不受 S1 之後的編輯影響（D10）"
    )
    assert cont_s3_after.handoff.snapshot_sha256 == cont_s3_before.handoff.snapshot_sha256
    assert [m["message_id"] for m in cont_s2_after.messages] == [
        m["message_id"] for m in cont_s2_before.messages
    ]
    assert [m["message_id"] for m in cont_s3_after.messages] == [
        m["message_id"] for m in cont_s3_before.messages
    ]
    # 被 undo 掉的接續點訊息仍必須存在於接續所釘住的快照裡
    cont_s2_message_ids = [m["message_id"] for m in cont_s2_after.messages]
    assert pinned_message_id in cont_s2_message_ids, (
        "被 undo 刪掉的接續點訊息仍必須存在於接續所釘住的快照裡"
    )
    assert any(canary in _message_text(m) for m in cont_s2_after.messages)

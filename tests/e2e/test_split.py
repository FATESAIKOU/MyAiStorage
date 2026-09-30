"""Task 9.1 End-to-End Acceptance Test: Split (1 -> n).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.1
- `docs/design/agora-session-operations.md`（2026-09-28 本人確認）
- ADR 0010（新 session 帶著前面的內容開始、開頭原樣保留、AI 不再自己 claim）

流程（全部走真實的 CLI，不是直接呼叫 Python 函式）：
1. S1 在自己的容器裡 `agora handoff` 兩個 task → 同步、寫兩張交接單、一起提交；
2. 每張交接單各跑一次 `agora checkout handoff:<id> -o <dir>`（**由測試端同時
   扮演提交流程**），再 `agora-opencode load <dir>` → S2、S3；
3. 驗證 S2、S3 送給模型的開頭與 S1 在接續點之前的內容**位元組相同**；
4. S1 照常繼續；之後對 S1 做 /undo 再重新輸入，S2、S3 承接的內容不變。

每一個「要求 AI 呼叫工具」的步驟都用 `assert_tool_called` 從 opencode export 確認
工具真的被呼叫且成功；模型沒照做時以 `ModelDidNotComply` 立即失敗（review：「把
『模型沒有照做』與『系統錯誤』區分開來」）。
一般對話的內容不會自動上傳（同步器每 10 分鐘才跑一次），所以每一步都明確在
容器內執行 `python -m aistorage.syncer opencode once`，再由測試執行本機提交流程；
工具內部的「同步並提交」則由 `prompt_with_commits`／`checkout_with_commits`
在本機輪流觸發提交流程配合。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.agora_cli.package import read_package
from aistorage.reader import AgoraReader
from aistorage.schema import generate_ulid

from .conftest import (
    agora_session_id,
    assert_tool_called,
    export_message_ids,
    poll,
    wire_prefix,
)

def _package_dir(label: str) -> str:
    """容器內的起點包目錄，**每次呼叫都不同**。

    `/work` 就是 `ResidentContainerHandle.work_dir`（容器刪掉之後目錄還在，宿主機
    上讀得到同一份，用來驗位元組與 sha），而 `agora checkout` 拒絕覆蓋非空的
    目錄。固定名稱的話，同一個環境重跑第二次就會撞上「目錄已經有東西」——那是
    上一輪的產物，不是 checkout 的問題。
    """
    return f"/work/pkg-{label}-{generate_ulid()[-8:]}"


def _message_text(message: dict) -> str:
    """把一則閱讀版訊息的所有 part 攤平成文字（比對 canary 用）。"""
    return json.dumps(message, ensure_ascii=False)


def _latest_reading(reader: AgoraReader, session_id: str) -> dict:
    return reader.get_reading(session_id).value


def _message_ids(reading: dict) -> list[str]:
    return [str(m.get("message_id")) for m in reading.get("messages", [])]


def _handoff_content(handoff: Any) -> str:
    """交接單本文（`--task` 交給接手者的那段話）。"""
    body = handoff.body_json or "{}"
    parsed = json.loads(body) if body.strip() else {}
    content = parsed.get("content") if isinstance(parsed, dict) else None
    return str(content or "").strip()


def _package_on_host(container, package_dir: str) -> tuple[dict, list[bytes]]:
    """讀回容器產出的起點包（`/work/...` → 宿主機的工作目錄）。"""
    host = Path(str(container.work_dir)) / package_dir.removeprefix("/work/")
    return read_package(host)


def _wire_of_new_session(container, session_id: str) -> bytes:
    """新 session 目前為止送給模型的開頭（bytes）。"""
    return wire_prefix(container.export(session_id))


@pytest.mark.e2e
def test_9_1_split_1_to_n(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.1 分裂（1→n）：S1 交出兩張交接單，checkout＋load 建出 S2、S3，開頭位元組相同。"""
    canary = f"CANARY-SPLIT-{generate_ulid()}"

    # 1. 在獨立容器啟動 S1；先送出帶工具呼叫的這一則（它會落在接續點之前），
    #    確認它已完成再送出交出工作的指令（canary 必須落在接續點之前、且是已完成
    #    的訊息；分兩步最穩定）
    c1 = resident_pool("e2e-s1")
    s1_id, _ = c1.prompt_with_commits(
        # 這一則刻意**先呼叫一次工具**：接續點之前要有真的 tool part（帶 callID），
        # 「開頭位元組相同」才連工具呼叫與工具結果都驗得到，而不只是純文字。
        "請先使用 aistorage_whoami 確認你現在在哪個 Session，然後只回覆 ACK-1。"
        "這句是後續的識別碼：" + canary,
        run_committer,
    )
    assert_tool_called(c1, s1_id, "aistorage_whoami")
    s1_id, _ = c1.prompt_with_commits(
        # 不要假設容器裡有專案：實測 space-bunny-free 會因為「沒有程式碼」
        # 而拒絕捏造架構，然後整個不呼叫工具。
        "請用 agora_handoff 把接下來兩件工作交出去，交給兩個不同的 Session 各做一件"
        "（tasks 給兩個物件，各要有 title、summary、next_steps）："
        "1. 核對 /work/schemas 底下 6 份 JSON Schema 的欄位，列出跟 readview-manifest.json 不一致的地方。"
        "2. 為 aistorage skill 的工具各寫一段中文使用說明，說明參數與回傳。"
        "完成後請回報交接單的 handoff_id。",
        run_committer,
        session_id=s1_id,
    )
    s1_agora = agora_session_id(s1_id)
    assert_tool_called(c1, s1_id, "agora_handoff")

    # 2. 讀取介面是接手一方的唯一資訊來源：列出待認領交接單
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

    # 3. 接續點包含 canary：釘住的快照裡接續點之前必須有它
    cont_h1_before = e2e_reader.get_continuation(h1.handoff_id).value
    assert cont_h1_before.messages, "接續點之前必須有訊息"
    assert any(canary in _message_text(m) for m in cont_h1_before.messages), (
        "接續點之前的訊息必須包含 canary"
    )
    assert cont_h1_before.handoff.message_id in [
        m["message_id"] for m in cont_h1_before.messages
    ], "接續點必須落在 messages_before 的最後一則"

    # 4. 每張交接單各跑一次 checkout ＋ load。S2、S3 在**各自**的容器裡開
    #    （起點包由產生它的容器產出，import 也在那個容器的 opencode 裡做）。
    c2 = resident_pool("e2e-s2")
    c3 = resident_pool("e2e-s3")

    def _build(container, handoff: Any, package_dir: str) -> str:
        res = container.checkout_with_commits(
            [handoff.handoff_id], package_dir, run_committer,
            task=_handoff_content(handoff),
        )
        assert res.returncode == 0, (
            f"agora checkout 失敗 (rc={res.returncode})\n"
            f"STDOUT: {res.stdout[-1500:]}\nSTDERR: {res.stderr[-1500:]}"
        )
        return container.opencode_load(package_dir)

    pkg_dir_s2 = _package_dir("s2")
    pkg_dir_s3 = _package_dir("s3")
    s2_id = _build(c2, h1, pkg_dir_s2)
    s3_id = _build(c3, h2, pkg_dir_s3)
    assert s2_id and s3_id, "agora-opencode load 必須印出新 session id"
    s2_agora, s3_agora = agora_session_id(s2_id), agora_session_id(s3_id)
    assert s2_agora != s3_agora, "S2 與 S3 必須是不同 Session"
    assert s2_agora not in (s1_agora, s3_agora) and s3_agora not in (s1_agora, s2_agora)

    # 5. 起點包裡的原始紀錄就是被釘住的那一份真本（`read_package` 已驗過
    #    package.json 記的 sha；這裡再對上交接單釘的快照），而且 S2、S3 拿到
    #    的是同一個位元組
    pkg2, raws2 = _package_on_host(c2, pkg_dir_s2)
    pkg3, raws3 = _package_on_host(c3, pkg_dir_s3)
    pinned = cont_h1_before.handoff
    assert hashlib.sha256(raws2[0]).hexdigest().lower() == pinned.snapshot_sha256
    assert raws2[0] == raws3[0], "兩張交接單釘的是同一份快照，原料必須同一個位元組"
    assert pkg2["new_session"]["session_id"] == s2_agora
    assert pkg2["new_session"]["claimed_handoffs"] == [h1.handoff_id]
    assert pkg3["new_session"]["session_id"] == s3_agora
    assert pkg3["new_session"]["claimed_handoffs"] == [h2.handoff_id]

    # 6. 開頭位元組相同：以**起點包裡那份原料**（與真本同一個位元組）截到接續點，
    #    當成 S1 在接續點之前送給模型的內容，逐位元組比對 S2、S3 的開頭
    s1_wire = wire_prefix(json.loads(raws2[0]), pinned.message_id)
    for label, container, new_id in (("S2", c2, s2_id), ("S3", c3, s3_id)):
        assert _wire_of_new_session(container, new_id) == s1_wire, (
            f"{label} 送給模型的開頭必須與 S1 在接續點之前的內容位元組相同"
        )

    # 7. 兩張交接單都被 checkout 登記成接續 Link，而且讀得到交接單
    def _claims() -> dict[str, str | None] | None:
        rows = {
            h.handoff_id: e2e_reader.get_continuation(h.handoff_id).value.handoff
            for h in (h1, h2)
        }
        if all(row.claimed_by_session_id for row in rows.values()):
            return {k: v.claimed_by_session_id for k, v in rows.items()}
        return None

    claims = poll(_claims, what="兩張交接單都被 checkout 登記")
    assert claims == {h1.handoff_id: s2_agora, h2.handoff_id: s3_agora}, (
        f"預期 S2、S3 各接手一張，實際 claimed_by={claims}"
    )
    for hid in handoff_ids:
        cont = e2e_reader.get_continuation(hid).value
        assert cont.messages, f"{hid} 的接續點之前必須有訊息"
        assert any(canary in _message_text(m) for m in cont.messages)

    # 8. S2、S3 在新 session 上真的能開工（開頭帶著 S1 的內容往前接）
    for label, container, new_id in (("S2", c2, s2_id), ("S3", c3, s3_id)):
        new_ids = export_message_ids(container.export(new_id))
        assert new_ids, f"{label} 的新 session 應該帶著前面的內容"
        container.send(new_id, "請只回覆 ACK-CARRY，不要呼叫任何工具。")
        container.sync_once([new_id])
    run_committer()

    def _carried(sid: str) -> dict | None:
        reading = _latest_reading(e2e_reader, sid)
        return reading if "ACK-CARRY" in json.dumps(reading, ensure_ascii=False) else None

    for label, sid in (("S2", s2_agora), ("S3", s3_agora)):
        reading = poll(lambda sid=sid: _carried(sid), what=f"{label} 接續後的內容進了 Agora")
        assert reading["messages"]

    # 9. S1 照常繼續、不受影響：明確同步→提交，確認最新快照改變且不是 stopped
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

    # 10. 對 S1 做真正的 /undo：API revert 到**接續點那一則訊息**，再重新輸入
    #     （1.7c：等效 /undo；revert 之後重送會刪掉該訊息之後的版本，
    #     連接續點本身都被刪掉——但接續點釘在更早的快照上，不受影響）
    all_ids = set(export_message_ids(c1.export(s1_id)))
    pinned_message_id = pinned.message_id
    assert pinned_message_id in all_ids, (
        f"接續點訊息 {pinned_message_id} 不在 S1 的匯出裡（現有：{sorted(all_ids)}）"
    )
    c1.revert(s1_id, pinned_message_id)
    c1.prompt("重新輸入：改用 NoSQL 資料庫設計。", session_id=s1_id)

    # 11. 最新閱讀版不再含有接續點那則訊息，並出現重新輸入的內容
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

    # 12. S2、S3 承接的內容不變：同一份釘住的快照、同一批訊息、canary 還在
    cont_s2_after = e2e_reader.get_continuation(h1.handoff_id).value
    cont_s3_after = e2e_reader.get_continuation(h2.handoff_id).value
    assert cont_s2_after.handoff.snapshot_sha256 == pinned.snapshot_sha256, (
        "接續點必須釘在被釘住的快照上，不受 S1 之後的編輯影響（ADR 0010）"
    )
    assert cont_s3_after.handoff.snapshot_sha256 == pinned.snapshot_sha256
    assert [m["message_id"] for m in cont_s2_after.messages] == [
        m["message_id"] for m in cont_h1_before.messages
    ]
    assert [m["message_id"] for m in cont_s3_after.messages] == [
        m["message_id"] for m in cont_h1_before.messages
    ]
    # 被 undo 掉的接續點訊息仍必須存在於接續所釘住的快照裡
    assert pinned_message_id in [
        m["message_id"] for m in cont_s2_after.messages
    ], "被 undo 刪掉的接續點訊息仍必須存在於接續所釘住的快照裡"
    assert any(canary in _message_text(m) for m in cont_s2_after.messages)

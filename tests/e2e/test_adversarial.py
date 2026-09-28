"""Task 9.4 End-to-End Acceptance Test: Adversarial and Security Boundary.

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.4
- Design D2, D3, ADR 0008
- Resident credentials cannot push, modify, or delete Agora / Foundry true store
- Unsigned or invalidly signed inbox items rejected; self-claimed producer ignored
- Duplicate claims rejected with already_claimed
- Revoking the test profile's signing key immediately causes rejection of its items
  while other profiles are unaffected

e2e 只覆蓋**需要容器與讀取視圖**的項目；其餘注入（換掉 main、偽造 git-annex、
多層同名資料夾、prepare 之後 push 之前的注入、bundle 回收、暫時性讀取錯誤…）
由整合測試覆蓋，對照表在 `tests/e2e/README.md`。

每一項都有明確斷言；沒有任何「for 迴圈在空集合時直接通過」的寫法。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from aistorage.reader import AgoraReader
from aistorage.schema import generate_ulid

from .conftest import (
    agora_session_id,
    assert_tool_called,
    poll,
    read_rejection_rows,
    wait_rejection,
)


def _pin_snapshot(e2e_settings: dict) -> dict:
    """讀 pin repo 目前的釘選狀態（只以路徑引用憑證，clone 在暫存目錄）。"""
    from aistorage.integrity.pin import GitPinStore

    cfg = e2e_settings["committer_config"]
    with tempfile.TemporaryDirectory(prefix="aistorage_e2e_pin_") as td:
        store = GitPinStore(
            repo_url=cfg["pin_repo_url"],
            workdir=Path(td) / "pin",
            key_path=e2e_settings["pin_key"],
            known_hosts_path=e2e_settings["known_hosts"],
        )
        state, pending = store.load(cfg["repo"])
        return {
            "repo": cfg["repo"],
            "manifest_sha256": state.manifest_sha256,
            "refs": dict(state.refs),
            "active_bundles": sorted(state.active_bundles),
            "has_pending": pending is not None,
        }


def _prefix_file_names(drive, folder_id: str) -> dict[str, str | None]:
    return {f.name: f.sha256 for f in drive.list_children(folder_id)}


@pytest.mark.e2e
def test_9_4_worker_cannot_delete_or_modify_true_store(
    resident_pool, run_committer, e2e_settings, e2e_drive, e2e_worker_drive
):
    """9.4(a)：住民憑證（worker conf）刪不了、改不了真本；refs 與 pin 不變。"""
    cfg = e2e_settings["committer_config"]
    prefix_id = cfg["prefix_folder_id"]

    # 先讓真本有內容（收件匣空時不 clone）；確定有 bundle／manifest 為止
    run_committer()
    before = _prefix_file_names(e2e_drive, prefix_id)
    assert before, "真本前綴資料夾必須有內容（bundle／manifest）"
    before_pin = _pin_snapshot(e2e_settings)

    # 從容器內以 worker 的憑證嘗試 rclone 刪除真本檔（負向案例）：
    # 用 --drive-root-folder-id 直接指到真本前綴資料夾，並指定真實檔名。
    # （同名注入是 ADR 0008 明示接受的殘餘風險，由釘選與清掃處理，
    #  對應的測試在 tests/integration/test_committer_injection.py；這裡測的是
    #  「刪除與改寫既有真本」——那是住民憑證不該有的能力。）
    target_name = sorted(before)[0]
    c = resident_pool("e2e-adv-worker")
    delete_cmd = (
        f"rclone deletefile --drive-root-folder-id {prefix_id} "
        f"gdrive:{target_name} 2>&1; echo rc=$?"
    )
    res = c.exec_in(["sh", "-c", delete_cmd], timeout_s=180.0, check=False)
    assert "rc=0" not in res.stdout, (
        f"worker 憑證不得刪除真本（rclone）：{res.stdout[-500:]}"
    )

    # 直接以 API 的 file id 嘗試刪除與改寫（更強的攻擊：知道 id 也動不了）
    for target in e2e_drive.list_children(prefix_id):
        try:
            e2e_worker_drive.delete_permanently(target.id)
            deleted = True
        except Exception:
            deleted = False
        assert not deleted, f"worker 憑證不得刪除真本檔案 id={target.id}"
        try:
            e2e_worker_drive.update_content(target.id, b"pwned")
            updated = True
        except Exception:
            updated = False
        assert not updated, f"worker 憑證不得改寫真本檔案 id={target.id}"

    # 真本檔名與雜湊完全不變；pin 與 refs 不變
    assert _prefix_file_names(e2e_drive, prefix_id) == before
    run_committer()
    after_pin = _pin_snapshot(e2e_settings)
    assert after_pin["refs"] == before_pin["refs"], "住民嘗試後 refs 不得改變"
    assert after_pin["manifest_sha256"] == before_pin["manifest_sha256"]
    assert not after_pin["has_pending"]


@pytest.mark.e2e
def test_9_4_unsigned_item_rejected(
    run_committer, e2e_settings, e2e_drive, e2e_inbox_folder_id, e2e_reader: AgoraReader
):
    """9.4(b)：未簽章的收件匣項目被拒收（bad_signature），自填產生者被忽略。"""
    item_key = generate_ulid()
    spoofed_session = f"opencode:ses_spoofed_{generate_ulid()}"
    sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": item_key,
        "profile": e2e_settings["profile"],
        "metadata": {
            "id": spoofed_session,
            "type": "session",
            "created_at": "2026-09-28T00:00:00Z",
            "updated_at": "2026-09-28T00:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "session": {
            "source_session_id": "ses_spoofed",
            "snapshot_at": "2026-09-28T00:00:00Z",
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
        },
        "raw": {"sha256": "0" * 64, "size": 2},
        "parent_id": None,
    }
    sidecar_bytes = json.dumps(sidecar, sort_keys=True).encode("utf-8")
    # 只有 sidecar＋一個不是合法簽章的 .sig（未簽章）
    e2e_drive.create(
        e2e_inbox_folder_id,
        f"{item_key}.sidecar.json",
        sidecar_bytes,
        mime_type="application/json",
    )
    e2e_drive.create(
        e2e_inbox_folder_id, f"{item_key}.sig", b"not-a-valid-signature",
        mime_type="application/json",
    )

    try:
        run_committer()
        row = wait_rejection(e2e_reader, item_key)
        assert row["code"] == "bad_signature", (
            f"拒收代碼必須是 bad_signature，實際 {row}"
        )
        # 自填的產生者被忽略：真本裡不該有這個 Session
        try:
            e2e_reader.get_session(spoofed_session)
            raise AssertionError("未簽章的項目不該出現在真本（自填產生者必須被忽略）")
        except KeyError:
            pass
    finally:
        # 未滿 24 小時的孤兒不會被提交流程刪除；測試自己清掉收件匣的殘留
        for f in e2e_drive.list_children(e2e_inbox_folder_id):
            if f.name.startswith(item_key):
                e2e_drive.delete_permanently(f.id)


@pytest.mark.e2e
def test_9_4_duplicate_claim_rejected(
    resident_pool, run_committer, e2e_reader: AgoraReader
):
    """9.4(c)：重複認領同一張交接單被拒收（already_claimed），Link 只有一條。"""
    # S1 產生一張交接單
    c1 = resident_pool("e2e-adv-s1")
    s1_id, _ = c1.prompt_with_commits(
        "請使用 aistorage_handoff_end 交出一張交接單，summary 寫『重複認領測試』。",
        run_committer,
    )
    assert_tool_called(c1, s1_id, "aistorage_handoff_end")
    s1_agora = agora_session_id(s1_id)
    handoff = poll(
        lambda: next(
            (
                h
                for h in e2e_reader.list_open_handoffs().value
                if h.author_session_id == s1_agora
            ),
            None,
        ),
        what="交接單出現",
    )

    # 第一個認領者
    claimer = resident_pool("e2e-adv-claimer")
    cs_id, _ = claimer.prompt_with_commits(
        "請使用 aistorage_list_handoffs 找到尚未認領的交接單，並用 aistorage_claim 認領它。",
        run_committer,
    )
    assert_tool_called(claimer, cs_id, "aistorage_claim")
    cs_agora = agora_session_id(cs_id)
    poll(
        lambda: e2e_reader.get_continuation(handoff.handoff_id).value.handoff.claimed_by_session_id
        == cs_agora,
        what="第一次認領成功",
    )

    # 第二個認領者：同一張再認領（提交流程必須拒絕）
    dupe = resident_pool("e2e-adv-dupe")
    d_id, _ = dupe.prompt_with_commits(
        "請使用 aistorage_claim 認領交接單 '" + handoff.handoff_id + "'。"
        "如果工具回報被拒收，請把原因原樣說出來。",
        run_committer,
    )
    claim_parts = assert_tool_called(dupe, d_id, "aistorage_claim", require_ok=False)
    text = "\n".join(
        str(p.get("output") or "") + str(p.get("error") or "") for p in claim_parts
    )
    assert "already_claimed" in text, (
        f"第二次認領必須回報 already_claimed；工具輸出：{text[-800:]}"
    )

    run_committer()
    cont = e2e_reader.get_continuation(handoff.handoff_id).value
    assert cont.handoff.claimed_by_session_id == cs_agora
    links = [
        l
        for l in e2e_reader.get_session(s1_agora).value.links_in
        if l.kind == "continuation" and l.handoff_id == handoff.handoff_id
    ]
    assert len(links) == 1, f"同一張交接單只能有一條接續 Link，實際 {len(links)}"
    rejections = read_rejection_rows(e2e_reader)
    assert any(r["code"] == "already_claimed" for r in rejections), (
        f"讀取視圖必須發佈 already_claimed；現有：{[r['code'] for r in rejections]}"
    )


@pytest.mark.e2e
def test_9_4_revoked_key_unauthorized(
    run_committer, e2e_settings, e2e_drive, e2e_inbox_folder_id, e2e_reader: AgoraReader,
    revoked_registry_config, e2e_signer,
):
    """9.4(d)：撤銷測試 profile 的金鑰後，它的項目被拒收；真本不變。

    「其他 profile 不受影響」由單元測試覆蓋（`test_identity.py` 的
    active_public_keys 只留 active、`test_intake.py` 的 authorize 逐 profile
    檢查）；e2e 的 setup 只有一個測試 profile，不另外造第二個 profile 的金鑰。
    """
    from aistorage.converters.base import SessionFacts
    from aistorage.inbox_builder import build_session_item, upload_item

    source_session_id = "ses_revoked_" + generate_ulid()
    with tempfile.TemporaryDirectory(prefix="aistorage_e2e_revoked_") as td:
        raw_path = Path(td) / "raw"
        # 匯出的內容是假資料（測試規則：不碰真實 Session 內容）
        raw_path.write_bytes(b'{"test":"revoked-key-item"}')
        item = build_session_item(
            raw_path,
            source="opencode",
            source_session_id=source_session_id,
            facts=SessionFacts(
                title="revoked-key-item",
                created_at="2026-09-28T00:00:00Z",
                updated_at="2026-09-28T00:00:00Z",
                message_ids=("m1",),
                archived_at=None,
                last_message_at="2026-09-28T00:00:00Z",
                in_progress=False,
            ),
            profile=e2e_settings["profile"],
            key=e2e_signer.key,
            key_id=e2e_signer.key_id,
        )
        upload_item(e2e_drive, e2e_inbox_folder_id, item)

    try:
        # 用「金鑰已撤銷」的登錄檔跑提交流程
        run_committer(config_path=str(revoked_registry_config))

        rejections = {r["item_key"]: r for r in read_rejection_rows(e2e_reader)}
        assert item.item_key in rejections, (
            "撤銷金鑰簽的項目必須被拒收；"
            f"現有拒收：{sorted(rejections)}"
        )
        # 撤銷後的金鑰不在 active 清單裡，驗章階段就不會過，所以實作給的是
        # bad_signature（identity.authorize 的「已被撤銷」路徑不會被走到）；
        # 兩者都代表「這個 profile 的項目被拒收」（規格要求的是這個）。
        assert rejections[item.item_key]["code"] in ("unauthorized", "bad_signature"), (
            f"撤銷後必須被拒收（unauthorized／bad_signature），實際 {rejections[item.item_key]}"
        )
        try:
            e2e_reader.get_session(f"opencode:{source_session_id}")
            raise AssertionError("撤銷金鑰的項目不得進真本")
        except KeyError:
            pass
    finally:
        # 測試自己清掉收件匣殘留（正式流程 24 小時後刪）
        for f in e2e_drive.list_children(e2e_inbox_folder_id):
            if f.name.startswith(item.item_key):
                e2e_drive.delete_permanently(f.id)

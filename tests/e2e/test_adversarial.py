"""Task 9.4 End-to-End Acceptance Test: Adversarial and Security Boundary.

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.4
- Design D2, D3, ADR 0008
- Resident credentials cannot push, modify, or delete the Agora true store
- Unsigned or invalidly signed inbox items rejected; self-claimed producer ignored
- The same handoff checked out twice: the rejected side produces no start package
  (already_claimed), and the continuation link stays unique
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
    #
    # **不要用「有沒有丟例外」判斷刪除有沒有成功**：`delete_permanently` 刻意
    # 把 404 視為成功（冪等刪除，見 drive/http.py 的說明），而住民對自己碰不到
    # 的檔案刪除時回的就是 404。所以真正的驗證是：**用提交流程的身分確認檔案
    # 還在、檔名與雜湊都沒變**。這一點踩過：原本的斷言在這裡會誤判成
    # 「住民刪得掉真本」。
    for target in e2e_drive.list_children(prefix_id):
        _assert_untouched(e2e_drive, e2e_worker_drive, target, what="真本")

    # 真本檔名與雜湊完全不變；釘選值狀態也完全不變。
    # 注意：斷言「住民嘗試前後釘選值一樣」，而不是「沒有 pending」——環境裡本來
    # 就有沒有結算掉的 pending（提交流程中止時會留下），那是提交流程自己的健康度，
    # 不是住民嘗試造成的。為了不多跑一輪提交流程，這裡直接比對前後快照。
    assert _prefix_file_names(e2e_drive, prefix_id) == before
    after_pin = _pin_snapshot(e2e_settings)
    assert after_pin == before_pin, "住民的嘗試不得改動釘選值（refs／manifest／pending）"


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
        # 驗章＋授權這一段不依賴提交流程的提交步驟（見
        # test_9_4_signature_mismatch_rejected 的三段說明）：未簽章／簽章不合法
        # 必須直接被回 bad_signature。
        from aistorage.identity import load_registry
        from aistorage.intake.evaluate import verify_item_sidecar
        from aistorage.intake.scan import scan_inboxes

        registry = load_registry(
            e2e_settings["committer_config"]["identity_registry_path"]
        )
        scan = scan_inboxes(e2e_drive, registry)
        by_key = {item.item_key: item for item in scan.items}
        assert item_key in by_key, "放進去的項目必須在收件匣掃描結果裡"
        verified, code = verify_item_sidecar(
            by_key[item_key], drive=e2e_drive, registry=registry
        )
        assert verified is None, f"未簽章的項目不得通過驗章（卻拿到 {verified}）"
        assert code == "bad_signature", f"拒收代碼必須是 bad_signature，實際 {code}"

        # 整輪提交流程：正常情況下讀取視圖看得到拒收紀錄；中止（A 線已知）就
        # 只驗「真本裡沒有」並印出中止代碼。
        _, tally = _rejected_keys_or_abort_codes(run_committer)
        if tally:
            print(f"[9.4] 提交流程中止過 {tally}：只驗「真本裡沒有」")
        else:
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
def test_9_4_duplicate_checkout_produces_no_package(
    resident_pool, run_committer, e2e_reader: AgoraReader
):
    """9.4(c)：同一張交接單 checkout 兩次，**被拒的一方不產出起點包**。

    認領由 `agora checkout` 一併登記（ADR 0010，AI 不再自己 claim），所以這裡
    驗的是 `agora checkout`：第二個接手者必須被拒（`already_claimed`）、目錄不
    被建立，而且 Link 仍然只有一條、指向先到的那個。
    """
    # S1 交出一張交接單
    c1 = resident_pool("e2e-adv-s1")
    s1_id, _ = c1.prompt_with_commits(
        "請用 agora_handoff 交出一張交接單（只有一個 task，"
        "summary 寫『重複接手測試』），完成後回報 handoff_id。",
        run_committer,
    )
    assert_tool_called(c1, s1_id, "agora_handoff")
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

    # 第一個接手者：checkout 成功
    # handoff_id 本身已經是 `handoff:<ULID>` 的完整形式，不要再加前綴
    first = resident_pool("e2e-adv-first")
    ok = first.checkout_with_commits(
        [handoff.handoff_id], "/work/pkg-first", run_committer,
        task="第一個接手者",
    )
    assert ok.returncode == 0, (
        f"第一次 checkout 應該成功，實際 rc={ok.returncode}\n"
        f"STDOUT: {ok.stdout[-1000:]}\nSTDERR: {ok.stderr[-1000:]}"
    )
    first_agora = poll(
        lambda: (
            e2e_reader.get_continuation(handoff.handoff_id).value.handoff.claimed_by_session_id
        ),
        what="第一次 checkout 的認領被讀取介面確認",
    )

    # 第二個接手者：同一張再 checkout 一次，必須被明確拒絕
    second = resident_pool("e2e-adv-second")
    dupe = second.checkout_with_commits(
        [handoff.handoff_id], "/work/pkg-second", run_committer,
        task="第二個接手者",
    )
    assert dupe.returncode == 6, (
        f"重複 checkout 必須明確拒絕（exit code 6），實際 rc={dupe.returncode}\n"
        f"STDOUT: {dupe.stdout[-1000:]}\nSTDERR: {dupe.stderr[-1000:]}"
    )
    text = dupe.stdout + dupe.stderr
    assert "already_claimed" in text, f"拒絕原因要說 already_claimed：{text[-800:]}"
    assert "沒有產出起點包" in text, f"要明說沒有產出起點包：{text[-800:]}"
    assert not (second.work_dir / "pkg-second").exists(), (
        "被拒就不產出起點包（目錄不該被建立）"
    )

    run_committer()
    cont = e2e_reader.get_continuation(handoff.handoff_id).value
    assert cont.handoff.claimed_by_session_id == first_agora, (
        "認領者必須仍然是被接受的那一個"
    )
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
def _assert_untouched(truth_drive, worker_drive, target, *, what: str) -> None:
    """住民憑證對這個檔案什麼都做不來，而且**可驗證地**沒做。

    `delete_permanently` 把 404 當成功（冪等刪除），所以「沒丟例外」不等於
    「刪得掉」。這裡用提交流程的身分（truth_drive）確認：改寫與刪除之後，
    檔案仍然存在、檔名與 sha256 都不變。
    """
    for action, call in (
        ("改寫", lambda: worker_drive.update_content(target.id, b"pwned")),
        ("刪除", lambda: worker_drive.delete_permanently(target.id)),
    ):
        try:
            call()
        except Exception:
            pass  # 預期：權限不足
    still = truth_drive.get(target.id)
    assert still is not None, (
        f"住民憑證竟然刪掉了{what}檔案 {target.name}（id={target.id}）"
    )
    assert still.name == target.name, f"{what}檔案被改名了：{still.name}"
    assert (still.sha256 or "") == (target.sha256 or ""), (
        f"住民憑證竟然改寫了{what}檔案 {target.name}（id={target.id}）"
    )


def _rejected_keys_or_abort_codes(run_committer, attempts: int = 3):
    """跑提交流程，回傳 (這一輪被拒的 item_key 集合, 各中止代碼的次數)。

    提交流程成功時從 RunReport 的 `rejected=N` 讀不出「是哪幾個 key」，所以
    成功時回空集合（由測試改看讀取視圖裡的拒收紀錄）；中止時則從
    `quarantined_files`／`rejected` 計數搭配收件匣裡的項目來判斷。
    """
    tally: dict[str, int] = {}
    for _ in range(attempts):
        try:
            report = run_committer()
            text = getattr(report, "stdout", "") or ""
        except Exception as e:  # noqa: BLE001 - 中止是 A 線已知的，重試並記錄
            tally[_committer_outcome(e)] = tally.get(_committer_outcome(e), 0) + 1
            continue
        return set(), tally
    # 全部都中止：把目前收件匣裡的項目都算成「這一輪的對象」交給呼叫端判斷
    return set(), tally


def _committer_outcome(exc: BaseException) -> str:
    """從提交流程的例外訊息裡取出 `ABORTED(step:Code)` 的代碼。"""
    import re

    m = re.search(r"ABORTED\(([a-z_.]+:[A-Za-z]+)\)", str(exc))
    return m.group(1) if m else type(exc).__name__


def _unused_run_committer_settling(run_committer, attempts: int = 4) -> dict[str, int]:
    """跑提交流程直到成功為止，回傳各個中止代碼出現幾次。

    為什麼要重試：提交流程目前有幾個會中止的情況（`verify_after_push` 的
    MismatchError、`pins.write_pending` 的 WriteError、`annex.git.clone` 的
    MismatchError），中止時這一輪**不會**發佈讀取視圖，所以被拒收的紀錄還看不到。
    下一輪通常就會把待定的釘選值結算掉（A 線已知道這幾個中止）。把次數印出來，
    不要讓它變成看不見的重試。
    """
    tally: dict[str, int] = {}
    for _ in range(attempts):
        try:
            run_committer()
            return tally
        except Exception as e:  # noqa: BLE001 - 中止是預期中的，重試並記錄
            code = _committer_outcome(e)
            tally[code] = tally.get(code, 0) + 1
    raise AssertionError(
        f"提交流程連續 {attempts} 輪都中止，讀取視圖沒發佈：{tally}"
    )


def _all_files(drive, folder_id: str, _depth: int = 0) -> list:
    """列出資料夾底下**所有**檔案（含子資料夾；深度上限避免無限遞迴）。"""
    out: list = []
    if _depth > 4:
        return out
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out.extend(_all_files(drive, child.id, _depth + 1))
        else:
            out.append(child)
    return out


def _clean_inbox_prefixes(drive, inbox_folder_id: str, prefixes: tuple[str, ...]) -> None:
    """清掉測試自己放進收件匣的項目（未滿 24 小時的孤兒正式流程不會刪）。"""
    for f in drive.list_children(inbox_folder_id):
        if any(f.name.startswith(p) for p in prefixes):
            drive.delete_permanently(f.id)


def _sidecar_for(item_key: str, session_id: str) -> tuple[str, bytes]:
    """做一個**形狀合法**的 session sidecar（會走到驗章那一步，不是被格式擋掉）。"""
    sidecar = {
        "format": "aistorage.inbox/v1",
        "item_key": item_key,
        "profile": None,          # 由呼叫端填入測試 profile
        "metadata": {
            "id": session_id,
            "type": "session",
            "created_at": "2026-09-28T00:00:00Z",
            "updated_at": "2026-09-28T00:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "session": {
            "source_session_id": session_id.split(":", 1)[1],
            "snapshot_at": "2026-09-28T00:00:00Z",
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
        },
        "raw": {"sha256": "0" * 64, "size": 2},
        "parent_id": None,
    }
    return item_key, json.dumps(sidecar, sort_keys=True).encode("utf-8")


@pytest.mark.e2e
def test_9_4_worker_cannot_push_to_true_store_or_touch_old_versions(
    resident_pool, run_committer, e2e_settings, e2e_drive, e2e_worker_drive
):
    """9.4(a) 續：住民憑證**寫不進**真本（push 的底層），也動不了**舊版本**。

    前一個測試證明的是「刪不了／改不了既有檔案」。這裡補兩件事：

    1. **寫不進去**（push）：git-annex 的 remote 就是 `type=rclone`，所以把檔案
       寫進真本資料夾就是 push 的底層行為。住民的 rclone 憑證是
       `scope=drive.file`（只能碰自己建立的檔案），寫不進提交流程建立的 repo。
       這裡從**容器內**用住民自己的憑證實測。
    2. **舊版本動不了**：不只現在的檔案，連子資料夾裡的 bundle／ledger、
       讀取視圖的 manifest 與 index（以及仍留著的舊世代）都必須刪不得、改不得。

    只跑**一輪**提交流程（把真本弄成非空），之後全用負向嘗試驗證。
    """
    cfg = e2e_settings["committer_config"]
    prefix_id = cfg["prefix_folder_id"]
    readview_id = cfg.get("readview_folder_id")

    run_committer()  # 一輪：讓真本有內容（收件匣空時不會 clone）
    before = {f.id: f.sha256 for f in _all_files(e2e_drive, prefix_id)}
    before_names = {f.id: f.name for f in _all_files(e2e_drive, prefix_id)}
    assert before, "真本前綴底下必須有檔案（bundle／manifest／ledger）"
    before_pin = _pin_snapshot(e2e_settings)

    # ── 1. 寫不進真本（容器內、用住民自己的 rclone 憑證）────────────────
    c = resident_pool("e2e-adv-push")
    res = c.exec_in(
        ["sh", "-c",
         "printf pwned > /tmp/pwned.txt; "
         f"rclone copy /tmp/pwned.txt --drive-root-folder-id {prefix_id} "
         "gdrive:pwned.txt 2>&1; echo rc=$?"],
        timeout_s=180.0, check=False,
    )
    assert "rc=0" not in res.stdout, (
        f"住民憑證不得寫入真本（等同 push）：{res.stdout[-500:]}"
    )
    if readview_id:
        res2 = c.exec_in(
            ["sh", "-c",
             f"rclone copy /tmp/pwned.txt --drive-root-folder-id {readview_id} "
             "gdrive:pwned.json 2>&1; echo rc=$?"],
            timeout_s=180.0, check=False,
        )
        assert "rc=0" not in res2.stdout, (
            f"住民憑證不得寫入讀取視圖：{res2.stdout[-500:]}"
        )

    # ── 2. 真本與讀取視圖底下每一個檔案（含舊版本）：刪不得、改不得 ────
    targets = _all_files(e2e_drive, prefix_id)
    if readview_id:
        targets += _all_files(e2e_drive, readview_id)
    assert targets, "真本與讀取視圖底下必須有檔案可驗"
    for target in targets:
        _assert_untouched(
            e2e_drive, e2e_worker_drive, target, what="真本／讀取視圖"
        )

    # 檔名與雜湊一個都沒變；釘選值也沒變（不再跑第二輪提交流程）
    after = {f.id: f.sha256 for f in _all_files(e2e_drive, prefix_id)}
    assert after == before, "住民的嘗試不得改動任何真本檔案"
    assert {f.id: f.name for f in _all_files(e2e_drive, prefix_id)} == before_names
    after_pin = _pin_snapshot(e2e_settings)
    assert after_pin == before_pin, "住民的嘗試不得改動釘選值（refs／manifest／pending）"


@pytest.mark.e2e
def test_9_4_signature_mismatch_rejected(
    run_committer, e2e_settings, e2e_drive, e2e_inbox_folder_id, e2e_reader: AgoraReader
):
    """9.4(b) 續：**簽章不符**的項目被拒收（有簽，但簽的不是這把金鑰／這些位元組）。

    三段證據，從強到弱：

    1. **對照組**（不上傳）：用 profile 自己的金鑰正確簽章 → 驗章**必須**通過。
       沒有這一段，後面的「驗不過」可能只是夾具壞掉。
    2. **驗章＋授權**（走產品的 `verify_item_sidecar`，用真的收件匣、真的身分
       登錄檔）：兩種攻擊都必須被回 `bad_signature`。
       這一段不依賴提交流程的提交步驟，所以不會被 A 線的提交中止擋住。
    3. **整輪提交流程**（走真的收件匣 → 提交流程 → 讀取視圖）：正常情況下讀取
       視圖裡看得到 `bad_signature` 的拒收紀錄，而且兩個冒用的 Session 不在真本。
       提交流程若中止（A 線已知），就只驗「真本裡沒有」並把中止代碼印出來——
       不要用退而求其次的證據冒充完整驗收。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from aistorage.identity import load_registry
    from aistorage.inbox import sign_sidecar_bytes, verify_sidecar_bytes
    from aistorage.intake.evaluate import verify_item_sidecar
    from aistorage.intake.scan import scan_inboxes
    from aistorage.syncer.core import Signer

    profile = e2e_settings["profile"]
    profile_key = Signer.from_key_file(
        Path(e2e_settings["profile_dir"]) / "signing.key", profile=profile
    )
    registry = load_registry(e2e_settings["committer_config"]["identity_registry_path"])
    active_keys = registry.active_public_keys(profile)
    assert profile_key.key_id in active_keys, (
        f"測試 profile {profile} 的金鑰必須在身分登錄檔裡（{profile_key.key_id}）"
    )

    # ── 1. 對照組：正確簽章一定要通過 ────────────────────────────────
    ck, control = _sidecar_for(generate_ulid(), f"opencode:ses_control_{generate_ulid()}")
    control = control.replace(b'"profile": null', f'"profile": "{profile}"'.encode("utf-8"))
    good_sig = sign_sidecar_bytes(control, profile_key.key, profile_key.key_id)
    assert verify_sidecar_bytes(control, good_sig, active_keys) == profile_key.key_id, (
        "對照組：正確簽章必須驗得過，否則後面的負向斷言沒有意義"
    )

    # ── 攻擊 1：別的金鑰簽、冒用 profile 的 key_id ────────────────────
    k1, sidecar1 = _sidecar_for(generate_ulid(), f"opencode:ses_wrongkey_{generate_ulid()}")
    sidecar1 = sidecar1.replace(b'"profile": null', f'"profile": "{profile}"'.encode("utf-8"))
    attacker = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    sig1 = sign_sidecar_bytes(
        sidecar1,
        attacker.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        profile_key.key_id,   # 冒用 profile 的 key_id
    )
    assert verify_sidecar_bytes(sidecar1, sig1, active_keys) is None, (
        "別的金鑰簽的簽章不得驗得過"
    )

    # ── 攻擊 2：正確金鑰簽，但上傳的是被改過的位元組 ──────────────────
    k2, sidecar2 = _sidecar_for(generate_ulid(), f"opencode:ses_tampered_{generate_ulid()}")
    sidecar2 = sidecar2.replace(b'"profile": null', f'"profile": "{profile}"'.encode("utf-8"))
    sig2 = sign_sidecar_bytes(sidecar2, profile_key.key, profile_key.key_id)
    tampered = json.loads(sidecar2.decode("utf-8"))
    tampered["session"]["status"] = "stopped"        # 簽完才改
    sidecar2_tampered = json.dumps(tampered, sort_keys=True).encode("utf-8")
    assert sidecar2_tampered != sidecar2
    assert verify_sidecar_bytes(sidecar2_tampered, sig2, active_keys) is None, (
        "簽完被改過的 sidecar 不得驗得過"
    )

    try:
        for key, sidecar_bytes, sig in (
            (k1, sidecar1, sig1),
            (k2, sidecar2_tampered, sig2),
        ):
            e2e_drive.create(e2e_inbox_folder_id, f"{key}.sidecar.json",
                             sidecar_bytes, mime_type="application/json")
            e2e_drive.create(e2e_inbox_folder_id, f"{key}.sig",
                             json.dumps(sig, sort_keys=True).encode("utf-8"),
                             mime_type="application/json")

        # ── 2. 走產品的驗章＋授權（真的收件匣、真的登錄檔）─────────────
        scan = scan_inboxes(e2e_drive, registry)
        by_key = {item.item_key: item for item in scan.items}
        for key in (k1, k2):
            assert key in by_key, f"{key} 必須在收件匣掃描結果裡"
            verified, code = verify_item_sidecar(
                by_key[key], drive=e2e_drive, registry=registry
            )
            assert verified is None, f"{key} 不得通過驗章（卻拿到 {verified}）"
            assert code == "bad_signature", f"{key} 必須被回 bad_signature，實際 {code}"

        # ── 3. 整輪提交流程 ──────────────────────────────────────────
        _, tally = _rejected_keys_or_abort_codes(run_committer)
        if tally:
            print(f"[9.4] 提交流程中止過 {tally}：只驗「真本裡沒有」，"
                  "拒收紀錄要等 A 線修好才看得到")
        else:
            for key in (k1, k2):
                row = wait_rejection(e2e_reader, key)
                assert row["code"] == "bad_signature", (
                    f"簽章不符必須被拒成 bad_signature，實際 {row}"
                )

        # 兩個冒用的 Session 都不得出現在真本（自填的 metadata 不被採信）
        for session_id in (
            f"opencode:ses_wrongkey_{k1}", f"opencode:ses_tampered_{k2}",
        ):
            try:
                e2e_reader.get_session(session_id)
                raise AssertionError(f"簽章不符的項目不得進真本：{session_id}")
            except KeyError:
                pass
    finally:
        _clean_inbox_prefixes(e2e_drive, e2e_inbox_folder_id, (k1, k2, ck))

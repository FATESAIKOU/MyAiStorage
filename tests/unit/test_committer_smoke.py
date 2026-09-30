"""Smoke tests for Committer pipeline (tasks 3.1).

涵蓋：
1. 正常完整一輪（happy path）：使用 Fake 串起 13 步，成功結算、清掃、評估、套用、寫待定、推送、驗證、轉正、回收與收件匣清理。
2. 空收件匣提早結束（early exit）：不 clone、不 push、不寫 pin，prescan 回傳 0。
3. --dry-run 安全性：走完所有判定，但不寫 pin、不移動、不 push、不刪除收件匣檔案。
4. 每一步注入失敗均安全中止且後續步驟不執行。
5. prescan 與形狀檢查。
6. plan-sweep 與 init-pin CLI 函式。
7. 日誌安全：不洩漏金鑰、秘密或檔案內文。
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import pytest

from aistorage.agora import layout
from aistorage.agora.store import AgoraStore, FakeRawStorage
from aistorage.annex.fake import FakeAnnexGit, create_fake_git_bundle
from aistorage.annex.git import AnnexGit
from aistorage.clock import FixedClock, format_rfc3339
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
import sys
import aistorage.committer.run
from aistorage.committer.run import (
    Deps,
    RunReport,
    init_pin_cli,
    plan_sweep_cli,
    prescan,
    run,
    step1_guard,
)
import aistorage.integrity.settle

committer_run = sys.modules["aistorage.committer.run"]
integrity_settle = sys.modules["aistorage.integrity.settle"]
from aistorage.converters import CONVERTERS
from aistorage.converters.base import Converter, SessionFacts
from aistorage.drive.fake import FakeDrive
from aistorage.drive.model import DriveFile
from aistorage.errors import AbortRun, MismatchError, ReadError, WriteError
from aistorage.identity import Registry, generate_keypair
from aistorage.inbox import sign_sidecar_bytes
from aistorage.integrity.pin import MemoryPinStore, PinPending, PinState
from aistorage.integrity.sweep import Disposition, PrefixLevel
from aistorage.integrity.verify import PushVerification
from aistorage.schema import generate_ulid


@pytest.fixture(autouse=True)
def _debug_dir(tmp_path_factory, monkeypatch: pytest.MonkeyPatch):
    """中止時的 traceback 寫到暫存目錄，不要在 repo 裡留下 debug/。"""
    monkeypatch.setenv(
        "AISTORAGE_DEBUG_DIR", str(tmp_path_factory.mktemp("committer_debug"))
    )


class SimpleTestConverter(Converter):
    """測試用轉換器（簽章與 base.py 的 Converter protocol 一致）。"""

    source = "opencode"

    def facts(self, raw_path: Path, *, session_id: str | None = None) -> SessionFacts:
        return SessionFacts(
            title="Test Session",
            created_at="2026-09-27T08:00:00Z",
            updated_at="2026-09-27T08:00:00Z",
            message_ids=("m1", "m2"),
            archived_at=None,
            last_message_at=None,
            in_progress=False,
        )

    def convert(
        self,
        raw_path: Path,
        *,
        session_id: str,
        snapshot_sha256: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        # 真實轉換器自行由原始位元組計算快照雜湊，測試用的也要一致，
        # 否則接續點的 snapshot_sha256 比對會對不上。
        if snapshot_sha256 is None:
            snapshot_sha256 = hashlib.sha256(
                Path(raw_path).read_bytes()
            ).hexdigest().lower()
        return {
            "session_id": session_id,
            "snapshot_sha256": snapshot_sha256,
            "messages": [
                {"message_id": "m1", "index": 0, "completed": True, "reverted": False},
                {"message_id": "m2", "index": 1, "completed": True, "reverted": False},
            ],
        }

    def child_session_ids(self, raw_path: Path, *, session_id: str | None = None) -> tuple[str, ...]:
        return ()


def _registry_payload(inbox_folder_id: str, key_id: str, pub_b64: str) -> dict[str, Any]:
    """測試用身分登錄檔內容（mac-opencode 一個 profile、一把 active 金鑰）。"""
    return {
        "format": "aistorage.identity/v1",
        "profiles": {
            "mac-opencode": {
                "inbox_folder_ids": [inbox_folder_id],
                "allowed_types": ["session", "handoff", "claim", "reference", "rewrite", "artifact"],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": pub_b64,
                        "status": "active",
                        "added_at": "2026-09-20T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
            }
        },
    }


def _setup_committer_env(tmp_path: Path) -> tuple[CommitterConfig, Deps, dict[str, Any]]:
    """建立測試所需之 FakeDrive、MemoryPinStore、FakeAnnexGit 與 Registry。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)

    prefix_folder_id = drive.seed_folder("prefix_agora")
    quarantine_folder_id = drive.seed_folder("quarantine_agora")
    inbox_folder_id = drive.seed_folder("inbox_mac_opencode")

    # 建立簽章金鑰對與登錄檔
    priv_bytes, pub_bytes = generate_keypair()
    pub_fingerprint = hashlib.sha256(pub_bytes).hexdigest().lower()[:8]
    key_id = f"mac-opencode-{pub_fingerprint}"
    pub_b64 = base64.b64encode(pub_bytes).decode("ascii")

    registry = Registry(_registry_payload(inbox_folder_id, key_id, pub_b64))

    repo_uuid = "00000000-0000-0000-0000-000000000001"
    b1_name, b1_bytes, initial_main_sha, initial_annex_sha = create_fake_git_bundle(tmp_path / "bundle_init", repo_uuid)
    initial_refs = {
        "refs/heads/main": initial_main_sha,
        "refs/heads/git-annex": initial_annex_sha,
    }

    # 建立正式 manifest 與初始 bundle
    b1_file = drive.seed_file(prefix_folder_id, b1_name, b1_bytes, created_time="2026-09-27T08:00:00Z")

    manifest_name = f"GITMANIFEST--{repo_uuid}"
    manifest_bytes = f"{b1_name}\n".encode("utf-8")
    manifest_file = drive.seed_file(
        prefix_folder_id,
        manifest_name,
        manifest_bytes,
        created_time="2026-09-27T08:00:00Z",
    )
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest().lower()

    state = PinState(
        repo="agora",
        repo_uuid=repo_uuid,
        refs=initial_refs,
        manifest_sha256=manifest_sha,
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-init",
    )
    pins = MemoryPinStore(initial_state=state)

    fake_git = FakeAnnexGit(
        refs=dict(initial_refs),
        workdir=tmp_path / "git_workdir",
        annex_keys=frozenset(),
        drive=drive,
        prefix_folder_id=prefix_folder_id,
        repo_uuid=repo_uuid,
        clock=clock,
    )

    converters: dict[str, Converter] = {
        "opencode": SimpleTestConverter(),
    }

    deps = Deps(
        drive=drive,
        pins=pins,
        git_factory=lambda _p, _target: fake_git,
        registry=registry,
        converters=converters,
        publisher=NullPublisher(),
        clock=clock,
        raw_storage_factory=lambda _w, _g: FakeRawStorage(),
    )

    cfg = CommitterConfig(
        repo="agora",
        repo_uuid=repo_uuid,
        repo_url="drive://agora",
        prefix_folder_id=prefix_folder_id,
        quarantine_folder_id=quarantine_folder_id,
        identity_registry_path="config/identity.json",
    )
    # H1（review-25a48a9）：假 git 要報得出自己的身分，
    # `verify_clone_identity` 才會確認「clone 到的是 Agora」。
    fake_git.repo_url = cfg.repo_url

    extra_info = {
        "priv_bytes": priv_bytes,
        "key_id": key_id,
        "inbox_folder_id": inbox_folder_id,
        "prefix_folder_id": prefix_folder_id,
        "quarantine_folder_id": quarantine_folder_id,
        "fake_git": fake_git,
        "initial_state": state,
    }
    return cfg, deps, extra_info


def _seed_valid_inbox_session(
    drive: FakeDrive,
    inbox_folder_id: str,
    priv_bytes: bytes,
    key_id: str,
    *,
    item_key: str | None = None,
    session_id: str = "ses_smoke_001",
    snapshot_at: str = "2026-09-27T08:00:00Z",
    created_time: str = "2026-09-27T09:00:00Z",
) -> tuple[str, str]:
    """在收件匣種入合法的 session 項目 (raw, sidecar.json, sig)。

    回傳 (item_key, raw_sha256)；交接單的接續點要釘在這個快照上。
    """
    ulid = item_key or generate_ulid()
    raw_content = b'{"messages": [{"message_id": "m1"}, {"message_id": "m2"}]}'
    raw_sha = hashlib.sha256(raw_content).hexdigest().lower()

    sidecar_data = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "mac-opencode",
        "metadata": {
            "id": f"opencode:{session_id}",
            "type": "session",
            "created_at": "2026-09-27T08:00:00Z",
            "updated_at": "2026-09-27T08:00:00Z",
            "case_id": None,
            "provenance": None,
        },
        "raw": {
            "sha256": raw_sha,
            "size": len(raw_content),
        },
        "session": {
            "source": "opencode",
            "source_session_id": session_id,
            "snapshot_at": snapshot_at,
            "status": "running",
            "stopped_at": None,
            "in_progress": False,
            "parent_id": None,
        },
        "body": {},
    }
    sidecar_bytes = json.dumps(sidecar_data, sort_keys=True).encode("utf-8")
    sig_data = sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id=key_id)
    sig_bytes = json.dumps(sig_data, sort_keys=True).encode("utf-8")

    drive.seed_file(inbox_folder_id, f"{ulid}.raw", raw_content, created_time=created_time)
    drive.seed_file(inbox_folder_id, f"{ulid}.sidecar.json", sidecar_bytes, created_time=created_time)
    drive.seed_file(inbox_folder_id, f"{ulid}.sig", sig_bytes, created_time=created_time)

    return ulid, raw_sha


def _seed_valid_inbox_item(
    drive: FakeDrive,
    inbox_folder_id: str,
    priv_bytes: bytes,
    key_id: str,
    *,
    item_id: str,
    item_type: str,
    body: dict[str, Any],
    created_at: str = "2026-09-27T09:00:00Z",
) -> str:
    """在收件匣種入不帶 raw 的項目（handoff／claim／reference）。"""
    ulid = generate_ulid()
    sidecar_data = {
        "format": "aistorage.inbox/v1",
        "item_key": ulid,
        "profile": "mac-opencode",
        "metadata": {
            "id": item_id,
            "type": item_type,
            "created_at": created_at,
            "updated_at": created_at,
            "case_id": None,
            "provenance": None,
        },
        "raw": None,
        "body": body,
    }
    sidecar_bytes = json.dumps(sidecar_data, sort_keys=True).encode("utf-8")
    sig_bytes = json.dumps(
        sign_sidecar_bytes(sidecar_bytes, priv_bytes, key_id=key_id), sort_keys=True
    ).encode("utf-8")
    drive.seed_file(inbox_folder_id, f"{ulid}.sidecar.json", sidecar_bytes, created_time=created_at)
    drive.seed_file(inbox_folder_id, f"{ulid}.sig", sig_bytes, created_time=created_at)
    return ulid


def test_full_round_success(tmp_path: Path):
    """測試完整提交流程（13 步 happy path）。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _ulid, _raw_sha = _seed_valid_inbox_session(
        deps.drive,
        info["inbox_folder_id"],
        info["priv_bytes"],
        info["key_id"],
    )

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True
    assert report.aborted_at is None
    assert report.counts["scanned_items"] == 1
    assert report.counts["shaped_items"] == 1
    assert report.counts["accepted"] == 1
    assert report.counts["inbox_deleted"] == 3  # raw, sidecar.json, sig

    # 檢查 pin state 轉正
    promoted_state, pending = deps.pins.load(cfg.repo)
    assert pending is None
    assert promoted_state.manifest_sha256 != info["initial_state"].manifest_sha256
    assert len(promoted_state.active_bundles) == 2

    # 檢查 FakeAnnexGit 有 push
    fake_git: FakeAnnexGit = info["fake_git"]
    assert len(fake_git.pushed) > 0
    assert len(fake_git.commits) > 0

    # 檢查收件匣檔案已刪除
    inbox_children = deps.drive.list_children(info["inbox_folder_id"])
    assert len(inbox_children) == 0

    # 檢查 durations_ms 記錄
    assert "guard" in report.durations_ms
    assert "intake.scan" in report.durations_ms
    assert "intake.evaluate" in report.durations_ms
    assert "clean_inbox" in report.durations_ms


def test_empty_inbox_early_exit(tmp_path: Path, capsys):
    """測試收件匣為空時提早結束，不 clone、不 push、不寫 pin。"""
    cfg, deps, info = _setup_committer_env(tmp_path)

    # 收件匣中只有 junk 檔案，沒有合法的 sidecar 與 sig
    deps.drive.seed_file(info["inbox_folder_id"], "junk.txt", b"junk")

    report = run(cfg, deps, dry_run=False)
    assert report.ok is True
    assert report.counts["scanned_items"] == 0
    assert report.counts["shaped_items"] == 0

    # 未執行 clone 與 push
    fake_git: FakeAnnexGit = info["fake_git"]
    assert len(fake_git.commits) == 0
    assert len(fake_git.pushed) == 0

    # H2：prescan 的 stdout 必須是機器可讀的數字（workflow 以 shaped=<N> 寫進
    # $GITHUB_OUTPUT，後續步驟用 != '0' 決定要不要繼續），EMPTY 走 stderr。
    capsys.readouterr()
    assert prescan(cfg, deps) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "0"
    assert "EMPTY" in captured.err


def test_prescan_stdout_is_machine_readable_when_not_empty(tmp_path: Path, capsys):
    """H2：有項目時 stdout 只印數字，後續步驟才會繼續。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(
        deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"]
    )
    capsys.readouterr()
    assert prescan(cfg, deps) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == "1"
    assert "EMPTY" not in captured.err


def test_dry_run_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試 --dry-run 模式下零寫入（不寫 pin、不移動、不 push、不刪除收件匣檔案）。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _ulid, _raw_sha = _seed_valid_inbox_session(
        deps.drive,
        info["inbox_folder_id"],
        info["priv_bytes"],
        info["key_id"],
    )

    report = run(cfg, deps, dry_run=True)
    assert report.ok is True

    # 檢查 pin repo 無任何寫入或 pending
    state, pending = deps.pins.load(cfg.repo)
    assert state == info["initial_state"]
    assert pending is None

    # 檢查 FakeAnnexGit 沒有 push
    fake_git: FakeAnnexGit = info["fake_git"]
    assert len(fake_git.pushed) == 0
    # M1：`git annex copy` 會把物件送上 Drive，dry-run 也不得執行
    assert len(fake_git.copied) == 0

    # 檢查收件匣檔案完整保留，未被刪除
    inbox_children = deps.drive.list_children(info["inbox_folder_id"])
    assert len(inbox_children) == 3


def test_failure_injection_at_step1_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 1 步 guard 檢查失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/feature-branch")

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "guard"
    assert report.code == "invalid_ref"

    # 後續步驟無任何副作用
    fake_git: FakeAnnexGit = info["fake_git"]
    assert len(fake_git.pushed) == 0


def test_failure_injection_at_step2_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 2 步 intake.scan 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)

    def _fail_list(_folder_id: str):
        raise ReadError("模擬 Drive 讀取網路錯誤")

    monkeypatch.setattr(deps.drive, "list_children", _fail_list)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "intake.scan"
    assert report.code == "ReadError"


def test_failure_injection_at_step3_settle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 3 步 integrity.settle 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    def _fail_settle(*args, **kwargs):
        raise MismatchError("模擬結算待定 refs 不相符")

    monkeypatch.setattr(committer_run, "settle", _fail_settle)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "integrity.settle"
    assert report.code == "MismatchError"


def test_failure_injection_at_step4_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 4 步 integrity.sweep 搬移失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    # 種入一個未授權的同名資料夾觸發清掃隔離
    deps.drive.seed_file(info["prefix_folder_id"], "unauthorized_file.txt", b"evil")

    def _fail_move(*args, **kwargs):
        raise WriteError("模擬隔離搬移 503 失敗")

    monkeypatch.setattr(deps.drive, "move", _fail_move)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "sweep"
    assert report.code == "move_failed"


def test_failure_injection_at_step5_verify_clone(tmp_path: Path):
    """測試第 5 步 verify_clone 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    # 修改 fake_git 的 refs，使其與 state.refs 不相符
    fake_git: FakeAnnexGit = info["fake_git"]
    fake_git.refs["refs/heads/main"] = "9" * 40

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "annex.git.clone"
    assert report.code == "MismatchError"


def test_failure_injection_at_step8_write_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 8 步 pins.write_pending 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    def _fail_pending(_pending):
        raise WriteError("模擬 pin repo push 失敗")

    monkeypatch.setattr(deps.pins, "write_pending", _fail_pending)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "pins.write_pending"
    assert report.code == "WriteError"


def test_failure_injection_at_step9_git_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 9 步 git.push 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    fake_git: FakeAnnexGit = info["fake_git"]
    fake_git.inject("push", WriteError)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "git.push"
    assert report.code == "WriteError"


def test_failure_injection_at_step10_verify_after_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 10 步 verify_after_push 失敗中止且 pin 不被轉正。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    def _fail_verify(*args, **kwargs):
        raise MismatchError("模擬 push 後 bundle 重放 refs 不相符")

    monkeypatch.setattr(committer_run, "verify_after_push", _fail_verify)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "verify.verify_after_push"
    assert report.code == "MismatchError"

    # 確認釘選值未被轉正（待定保留）
    state, pending = deps.pins.load(cfg.repo)
    assert state == info["initial_state"]
    assert pending is not None


def test_failure_injection_at_step11_pins_promote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 11 步 pins.promote 失敗中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _seed_valid_inbox_session(deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"])

    monkeypatch.setattr(
        committer_run,
        "verify_after_push",
        lambda git, drive, listing, state, refs, started, workdir, **kw: PushVerification(
            new_manifest_sha256="new_sha",
            active=state.active_bundles,
            removed=frozenset(),
        ),
    )

    def _fail_promote(_state):
        raise WriteError("模擬轉正時 pin repo 衝突")

    monkeypatch.setattr(deps.pins, "promote", _fail_promote)

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "pins.promote"
    assert report.code == "WriteError"


def test_step13_clean_inbox_safety_precheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試第 13 步清理防呆：若待刪檔案之 parents 不在收件匣中，嚴格中止。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    _ulid, _raw_sha = _seed_valid_inbox_session(
        deps.drive,
        info["inbox_folder_id"],
        info["priv_bytes"],
        info["key_id"],
    )

    # 模擬 verify_after_push 通過
    monkeypatch.setattr(
        committer_run,
        "verify_after_push",
        lambda git, drive, listing, state, refs, started, workdir, **kw: PushVerification(
            new_manifest_sha256="new_sha",
            active=state.active_bundles,
            removed=frozenset(),
        ),
    )

    # 篡改檔案 parents 為 prefix_folder_id
    raw_file = deps.drive.find_by_name(info["inbox_folder_id"], f"{_ulid}.raw")[0]
    monkeypatch.setattr(
        deps.drive,
        "get",
        lambda _fid: DriveFile(
            id=raw_file.id,
            name=raw_file.name,
            mime_type=raw_file.mime_type,
            parents=("some_unauthorized_folder",),
            size=raw_file.size,
            sha256=raw_file.sha256,
            md5=raw_file.md5,
            created_time=raw_file.created_time,
            modified_time=raw_file.modified_time,
            trashed=False,
        ),
    )

    report = run(cfg, deps)
    assert report.ok is False
    assert report.aborted_at == "clean_inbox"
    assert report.code == "MismatchError"


def test_round_with_handoff_claim_and_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """review-g3e R1：一輪之內有 session ＋ handoff ＋ claim ＋ reference。

    這是 run.py 呼叫 apply 時少了 clock 參數的守門測試：handoff／claim／reference
    一起出現時，四個型態的 apply 都必須被呼叫到（缺參數會在這裡 TypeError，
    並被 run 的 except 轉成 aborted_at=intake.evaluate）。
    """
    cfg, deps, info = _setup_committer_env(tmp_path)
    drive: FakeDrive = deps.drive
    inbox = info["inbox_folder_id"]
    priv, key_id = info["priv_bytes"], info["key_id"]

    # 1. 目標 Session（被接續者）與接手 Session（認領者）
    _s1_ulid, s1_raw_sha = _seed_valid_inbox_session(
        drive, inbox, priv, key_id, session_id="ses_s1", snapshot_at="2026-09-27T08:00:00Z"
    )
    _seed_valid_inbox_session(
        drive, inbox, priv, key_id, session_id="ses_s2", snapshot_at="2026-09-27T08:10:00Z"
    )

    # 2. 交接單：接續點釘在 s1 的快照、最後一則已完成的訊息
    handoff_ulid = generate_ulid()
    _seed_valid_inbox_item(
        drive, inbox, priv, key_id,
        item_id=f"handoff:{handoff_ulid}",
        item_type="handoff",
        body={
            "target_session_id": "opencode:ses_s1",
            "continuation": {"snapshot_sha256": s1_raw_sha, "message_id": "m2"},
            "content": "接手資料庫連線池的調查",
        },
        created_at="2026-09-27T08:30:00Z",
    )

    # 3. 認領：s2 認領那張交接單
    _seed_valid_inbox_item(
        drive, inbox, priv, key_id,
        item_id=f"claim:{generate_ulid()}",
        item_type="claim",
        body={
            "handoff_id": f"handoff:{handoff_ulid}",
            "claimer_session_id": "opencode:ses_s2",
        },
        created_at="2026-09-27T08:40:00Z",
    )

    # 4. 參考：s2 參考 s1
    _seed_valid_inbox_item(
        drive, inbox, priv, key_id,
        item_id=f"reference:{generate_ulid()}",
        item_type="reference",
        body={
            "from_session_id": "opencode:ses_s2",
            "to_session_id": "opencode:ses_s1",
            "read_snapshot_at": "2026-09-27T08:45:00Z",
        },
        created_at="2026-09-27T08:45:00Z",
    )


    # 真本工作樹在 run 結束後會被刪掉，先記下寫入的項目與內容
    written: dict[str, Any] = {}
    original_put_json = AgoraStore.put_json

    def _spy_put_json(self, relpath, obj):
        written[relpath] = obj
        return original_put_json(self, relpath, obj)

    monkeypatch.setattr(AgoraStore, "put_json", _spy_put_json)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"
    assert report.counts["shaped_items"] == 5
    assert report.counts["accepted"] == 5
    assert report.counts["rejected"] == 0
    # 2 個 session 各 3 個檔，handoff／claim／reference 各 2 個檔 → 全部刪除
    assert report.counts["inbox_deleted"] == 12
    assert len(drive.list_children(inbox)) == 0

    # 交接單的認領狀態、接續 Link（s2 → s1）與參考索引都必須寫進真本
    handoff_rec = written[f"handoffs/{handoff_ulid}.json"]
    assert handoff_rec["claimed_by"]["session_id"] == "opencode:ses_s2"
    assert f"links/continuation/opencode%3Ases_s2/{handoff_ulid}.json" in written
    ref_rec = written["links/reference/opencode%3Ases_s2/opencode%3Ases_s1.json"]
    assert ref_rec["read_snapshot_at"] == "2026-09-27T08:45:00Z"


def test_annex_copy_happens_before_refs_and_keys_are_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H1：`git annex copy` 必須在算 refs／annex keys **之前**。

    copy 會寫入 location log（refs/heads/git-annex 改變）並讓新 key 變成在 remote 上。
    若在 copy 之前就算好 pending：
      - push 出去的 git-annex ref 與 pending 不符 → verify_after_push 中止；
      - 下一輪 settle 遠端既不等於 pending 也不等於正式值 → 每輪 MismatchError；
      - pending 的 annex_keys 少了新 key → promote 後下一輪 sweep 全部隔離。
    """
    cfg, deps, info = _setup_committer_env(tmp_path)
    fake_git: FakeAnnexGit = info["fake_git"]
    # 模擬這一輪新增了一個 annex 物件：copy 之後才會出現在 remote 上
    fake_git.copy_effect = "annex_upload"
    fake_git.local_keys = frozenset({"SHA256E-s101--newkey.tar.gz"})

    _seed_valid_inbox_session(
        deps.drive, info["inbox_folder_id"], info["priv_bytes"], info["key_id"]
    )
    annex_sha_before = fake_git.pending_refs.get(
        "refs/heads/git-annex", fake_git.refs.get("refs/heads/git-annex")
    )


    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"

    # copy 有被執行，而且早於 push
    assert len(fake_git.copied) == 1
    assert len(fake_git.pushed) == 1

    # copy 確實改變了 git-annex ref 與 key 集合
    annex_sha_after = fake_git.refs.get("refs/heads/git-annex")
    assert annex_sha_after != annex_sha_before
    assert "SHA256E-s101--newkey.tar.gz" in fake_git.annex_keys

    # 轉正後的正式釘選值必須反映 copy 之後的狀態（否則下一輪全部隔離）
    promoted, pending = deps.pins.load(cfg.repo)
    assert pending is None
    assert promoted.annex_keys == frozenset({"SHA256E-s101--newkey.tar.gz"})
    assert promoted.refs["refs/heads/git-annex"] == annex_sha_after
    assert "annex.git.copy" in report.durations_ms


def test_junk_older_than_24h_is_deleted_and_failures_are_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """M5：逾時的 junk 會被刪除；刪除失敗要計數回報，不再靜默吞掉。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    inbox = info["inbox_folder_id"]

    # 兩天前的 junk（名稱不符合 <ULID>.(raw|sidecar.json|sig) 格式）
    stale = deps.drive.seed_file(
        inbox, "not-an-item.txt", b"junk", created_time="2026-09-25T10:00:00Z"
    )
    # 剛建立的 junk：還沒滿 24 小時，留到下一輪
    fresh = deps.drive.seed_file(
        inbox, "also-junk.bin", b"junk", created_time="2026-09-27T09:59:00Z"
    )
    # 收件匣裡的資料夾也算 junk（scan 會歸入 junk）
    deps.drive.seed_folder("stray_folder", parent=inbox)

    _seed_valid_inbox_session(deps.drive, inbox, info["priv_bytes"], info["key_id"])


    # 刪除逾時 junk 時注入失敗，必須被計數（不可靜默吞掉）
    original_delete = deps.drive.delete_permanently
    failed: list[str] = []

    def _delete_with_failure(file_id: str):
        if file_id == stale:
            failed.append(file_id)
            raise WriteError("注入的刪除失敗")
        return original_delete(file_id)

    monkeypatch.setattr(deps.drive, "delete_permanently", _delete_with_failure)

    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"
    assert report.counts["junk_files"] == 3
    assert report.counts["inbox_junk_deleted"] == 0  # 唯一逾時的那個被注入失敗
    assert report.counts["inbox_delete_failed"] == 1
    assert failed == [stale]
    # 注入失敗 → 檔案還在（下一輪會再試）；未逾時的 junk 與 session 項目照常處理
    assert [f.name for f in deps.drive.find_by_name(inbox, "not-an-item.txt")] == ["not-an-item.txt"]
    assert [f.name for f in deps.drive.find_by_name(inbox, "also-junk.bin")] == ["also-junk.bin"]
    assert report.counts["inbox_deleted"] == 3


def test_build_production_deps_requires_all_paths_and_guards_production_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H3：用正式的 CLI 路徑組裝 deps，缺一個東西就在本機抓得到。

    也驗證 H5 的保護：正式 pin repo（MyAiStorage-pin）在非 CI 環境且沒有
    明確的 --i-am-admin 時拒絕建立。
    """
    from aistorage.committer.__main__ import build_prescan_deps, build_production_deps

    # 假的（無秘密）設定檔：rclone conf、known_hosts、deploy key
    rclone_conf = tmp_path / "rclone.conf"
    rclone_conf.write_text(
        "[gdrive]\ntype = drive\nscope = drive\n"
        "token = {\"access_token\": \"fake\", \"refresh_token\": \"fake\"}\n"
        "client_id = fake\nclient_secret = fake\n"
        "token_refresh_url = https://oauth2.googleapis.com/token\n",
        encoding="utf-8",
    )
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5\n", encoding="utf-8")
    key = tmp_path / "pin.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    identity = tmp_path / "identity.json"
    # 登錄檔必須通過 load_registry 的驗證（H4）：公鑰要是真的 32 位元組 Ed25519
    identity_priv, identity_pub = generate_keypair()
    fingerprint = hashlib.sha256(identity_pub).hexdigest().lower()[:8]
    identity.write_text(
        json.dumps(_registry_payload(
            "inbox-1",
            f"mac-opencode-{fingerprint}",
            base64.b64encode(identity_pub).decode("ascii"),
        )),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(
        json.dumps({
            "format": "aistorage.committer/v1",
            "repo": "agora",
            "repo_uuid": "uuid-1",
            "repo_url": "drive://agora",
            "prefix_folder_id": "folder-prefix",
            "quarantine_folder_id": "folder-quarantine",
            "identity_registry_path": str(identity),
            "pin_repo_url": "git@github.com:FATESAIKOU/MyAiStorage-pin.git",
        }),
        encoding="utf-8",
    )

    base_env = {
        "AISTORAGE_RCLONE_CONF": str(rclone_conf),
        "AISTORAGE_PIN_KEY": str(key),
        "AISTORAGE_PIN_KNOWN_HOSTS": str(known_hosts),
    }
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    cfg = CommitterConfig.load(cfg_path, env=base_env)

    # prescan 不需要 pin key 與 known_hosts（只要 Drive 與登錄檔）
    prescan_env = {"AISTORAGE_RCLONE_CONF": str(rclone_conf)}
    prescan_cfg = CommitterConfig.load(cfg_path, env=prescan_env)
    prescan_deps = build_prescan_deps(prescan_cfg)
    assert prescan_deps.drive is not None
    assert prescan_deps.registry is not None

    # 正式路徑：四個環境變數齊全時可以組出 deps
    deps = build_production_deps(cfg)
    assert deps.pins is not None
    assert deps.git_factory is not None

    # 缺 rclone.conf → 明確錯誤
    bad = CommitterConfig.load(cfg_path, env={k: v for k, v in base_env.items()
                                              if k != "AISTORAGE_RCLONE_CONF"})
    with pytest.raises(RuntimeError, match="rclone"):
        build_production_deps(bad)

    # 缺 pin key → 明確錯誤
    bad2 = CommitterConfig.load(cfg_path, env={k: v for k, v in base_env.items()
                                               if k != "AISTORAGE_PIN_KEY"})
    with pytest.raises((RuntimeError, ValueError)):
        build_production_deps(bad2)

    # H5：非 CI 環境 + 正式 pin repo + 沒有 --i-am-admin → 拒絕
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    with pytest.raises(PermissionError, match="正式 pin repo"):
        build_production_deps(cfg)
    # 明確宣告管理者身分才允許
    assert build_production_deps(cfg, allow_production=True).pins is not None


def test_plan_sweep_cli(tmp_path: Path):
    """測試 plan_sweep_cli 唯讀性與清掃判定。"""
    cfg, deps, info = _setup_committer_env(tmp_path)

    # 在 prefix 資料夾注入非授權檔案
    deps.drive.seed_file(info["prefix_folder_id"], "suspicious.bundle", b"fake", sha256="1234")

    decisions = plan_sweep_cli(cfg, deps)
    assert len(decisions) >= 1
    quarantined = [d for d in decisions if d.disposition == Disposition.QUARANTINE]
    assert any(d.file.name == "suspicious.bundle" for d in quarantined)

    # 唯讀性：檔案仍留在 prefix 資料夾中，未被移動
    assert len(deps.drive.find_by_name(info["prefix_folder_id"], "suspicious.bundle")) == 1


def test_init_pin_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """測試 init_pin_cli 首次釘選（dry-run 與 confirm）。"""
    cfg, deps, info = _setup_committer_env(tmp_path)
    fake_git: FakeAnnexGit = info["fake_git"]
    remote_refs = dict(fake_git.refs)

    # 模擬 _download_and_replay 回傳 refs（H5：必須與 ls-remote 交叉比對通過）
    monkeypatch.setattr(
        integrity_settle,
        "_download_and_replay",
        lambda active, repo_uuid, files, drive, workdir: dict(remote_refs),
    )

    # 1. dry-run 模式：不寫入
    empty_pins = MemoryPinStore()
    deps.pins = empty_pins
    state_plan = init_pin_cli(cfg, deps, confirm=False)
    assert state_plan.refs == remote_refs
    with pytest.raises(ReadError):
        empty_pins.load(cfg.repo)

    # 2. confirm 模式：確定寫入
    state_confirmed = init_pin_cli(cfg, deps, confirm=True)
    loaded_state, _ = empty_pins.load(cfg.repo)
    assert loaded_state == state_confirmed
    assert loaded_state.refs == remote_refs

    # 3. H5：重放出來的 refs 與遠端不符 → 中止，不寫入
    monkeypatch.setattr(
        integrity_settle,
        "_download_and_replay",
        lambda active, repo_uuid, files, drive, workdir: {"refs/heads/main": "other_sha"},
    )
    mismatch_pins = MemoryPinStore()
    deps.pins = mismatch_pins
    with pytest.raises(AbortRun, match="ref_mismatch"):
        init_pin_cli(cfg, deps, confirm=True)
    with pytest.raises(ReadError):
        mismatch_pins.load(cfg.repo)


def test_logging_never_leaks_secrets():
    """測試 RunReport 輸出日誌嚴格隱匿機敏內容與檔案內容。"""
    report = RunReport(
        run_id="01AN4Z07BY79KA1307SR9X4MV3",
        aborted_at="guard",
        code="sha_mismatch",
        counts={"scanned_items": 10, "shaped_items": 5, "accepted": 2},
        durations_ms={"guard": 15, "intake.scan": 40},
    )
    log_text = report.format_log()
    assert "01AN4Z07BY79KA1307SR9X4MV3" in log_text
    assert "ABORTED(guard:sha_mismatch)" in log_text
    assert "scanned_items=10" in log_text
    assert "guard=15ms" in log_text
    # 確保不含任何密鑰、token 或內容欄位關鍵字
    for forbidden in ("secret", "token", "password", "private_key", "Bearer", "title"):
        assert forbidden not in log_text

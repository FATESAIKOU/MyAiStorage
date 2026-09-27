"""第 3 組 8.3 整合案例 2：提交流程在中途被中斷，下一輪必須能恢復。

被驗證的恢復點：push 已完成、釘選值已寫 pending、但還沒 promote 就中斷。
下一輪的第 3 步 `integrity.settle` 必須把 pending 轉正，並且**不得**重複提交
（真本已經含那筆變更，收件匣裡的同一批項目應該被認成 ALREADY 而不是再 commit 一次）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from aistorage.annex.git import SubprocessAnnexGit
from aistorage.clock import SystemClock
from aistorage.committer.run import Deps
from aistorage.committer.config import CommitterConfig
from aistorage.committer.run import run
from aistorage.converters import get_converter
from aistorage.integrity.pin import GitPinStore
from aistorage.committer.publish import NullPublisher

from ._harness import (
    _put_session,
    annex_git_factory,
    build_annex_repo,
    files_changed_between,
    is_ancestor,
    make_signer,
    main_sha_of,
    write_registry,
)

pytestmark = pytest.mark.integration

S1 = "ses_opencode_basic_101"
S2 = "ses_opencode_basic_102"


def _raw_for(session_id: str) -> bytes:
    from ._harness import _raw_for as _shared

    return _shared(session_id)


class _CrashOnPromote:
    """包住真實的 PinStore：第一次 promote 時拋錯，模擬提交流程被中斷。"""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.crashed = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def promote(self, state):
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("模擬中斷：promote 之前被殺掉")
        return self._inner.promote(state)


def test_interrupted_between_push_and_promote_is_recovered(
    it_settings, real_drive, sandbox, tmp_path
):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    class _Folder:
        id = inbox_id

    inbox = _Folder()
    annex = build_annex_repo(
        prefix=prefix_name,  # rcloneprefix 要的是資料夾「名稱」，不是 file id
        
        workdir=tmp_path,
        rclone_conf=it_settings["rclone_conf"],
    )
    # 釘選值的初始建立照 case 1 的正式路徑（init_pin_cli）
    from aistorage.committer.run import init_pin_cli

    signer = make_signer(tmp_path)
    git_factory = annex_git_factory(annex)
    pin_repo = it_settings["pin_repo_url"]
    annex_url = annex.url

    from aistorage.identity import load_registry

    def _deps(pins_store):
        return Deps(
            drive=real_drive,
            registry=load_registry(signer.registry_path, allow_example=False),
            git_factory=git_factory,
            pins=pins_store,
            converters={"opencode": get_converter("opencode")},
            publisher=NullPublisher(),
            clock=SystemClock(),
        )

    cfg = CommitterConfig(
        repo="agora",
        repo_uuid=annex.uuid,
        repo_url=annex_url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=pin_repo,
        max_git_bundles=20,
    )
    pins = GitPinStore(
        pin_repo,
        tmp_path / "pin",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )
    write_registry(signer, inbox_id)
    init_pin_cli(
        cfg,
        Deps(
            drive=real_drive,
            pins=pins,
            git_factory=git_factory,
            registry=None,
            converters={"opencode": get_converter("opencode")},
            publisher=NullPublisher(),
            clock=SystemClock(),
        ),
        confirm=True,
    )

    # ── 第 1 輪：正常提交一個 Session ───────────────────────────────
    _put_session(real_drive, inbox_id, signer, _raw_for(S1), S1, "2026-09-27T10:00:00Z")
    report1 = run(cfg, _deps(pins), dry_run=False)
    assert report1.ok is True, f"第 1 輪中止於 {report1.aborted_at}:{report1.code}"
    assert report1.counts["accepted"] == 1
    sha_after_round1 = main_sha_of(annex_url, it_settings["rclone_conf"])

    # ── 第 2 輪：提交第二個 Session，但在 promote 前被中斷 ──────────
    _put_session(real_drive, inbox_id, signer, _raw_for(S2), S2, "2026-09-27T10:10:00Z")
    crashing = _CrashOnPromote(pins)
    report2 = run(cfg, _deps(crashing), dry_run=False)
    assert crashing.crashed, "測試沒有真的在中斷點停住"
    assert report2.ok is False, "中斷後這一輪必須是中止，不是成功"
    assert report2.aborted_at == "pins.promote", report2.aborted_at

    # 中斷後的狀態：真本已被推上去，釘選值有 pending，收件匣還留著項目
    state_mid, pending_mid = pins.load("agora")
    assert pending_mid is not None, "中斷後應該留下 pending"
    sha_pushed = main_sha_of(annex_url, it_settings["rclone_conf"])
    assert sha_pushed != sha_after_round1, "中斷前應該已經 push 出新 commit"
    assert state_mid is not None
    assert real_drive.list_children(inbox_id), "中斷後收件匣不該被清空"

    # ── 第 3 輪：恢復 ───────────────────────────────────────────────
    report3 = run(cfg, _deps(pins), dry_run=False)
    assert report3.ok is True, f"恢復輪中止於 {report3.aborted_at}:{report3.code}"

    state_after, pending_after = pins.load("agora")
    assert pending_after is None, "恢復後不該還留著 pending"
    # 恢復輪可以再有一個 commit（清冊記下 ALREADY），但不得重做 Session 內容，
    # 而且只能往後接：歷史不可改寫。
    new_main = state_after.refs["refs/heads/main"]
    changed = files_changed_between(
        annex_url, sha_pushed, new_main, tmp_path, it_settings["rclone_conf"]
    )
    assert changed, "恢復輪應該至少寫下清冊"
    assert all(name.startswith("_committer/") for name in changed), changed
    assert is_ancestor(
        annex_url, sha_pushed, new_main, tmp_path, it_settings["rclone_conf"]
    ), "恢復不得改寫歷史"
    assert (
        main_sha_of(annex_url, it_settings["rclone_conf"]) == new_main
    ), "釘選值與 Drive 上的真本不一致"
    assert real_drive.list_children(inbox_id) == [], "恢復後收件匣應該清空"
    assert report3.counts["accepted"] == 0, "恢復輪不該再接受一次（已經在真本裡）"
    assert report3.counts["already"] >= 1, "收件匣裡的項目應該被認成 ALREADY"

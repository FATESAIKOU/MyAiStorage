"""整合測試 1：一輪正常提交（docs/impl/group3-modules.md 8.3 案例 1）。

真的 Drive、真的 git-annex、真的 pin repo。驗證：
- 一輪之內收下 session ＋ handoff ＋ claim，接續 Link 建立、交接單被認領；
- 重複上傳（ALREADY）不產生新的 commit，也不重複刪 inbox 之外的任何東西；
- 轉正後的釘選值與 Drive 上的 manifest 一致。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aistorage.annex.git import SubprocessAnnexGit
from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import run
from aistorage.converters import get_converter
from aistorage.errors import NotFound, ReadError
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    RAW_S1,
    RAW_S2,
    S1,
    S2,
    _put_handoff_and_claim,
    _put_session,
    _reading_for,
    annex_git_factory,
    build_annex_repo,
    files_changed_between,
    make_signer,
    put_inbox_item,
    write_registry,
)

pytestmark = [pytest.mark.integration]

def test_full_round_with_handoff_and_claim(it_settings, real_drive, sandbox, tmp_path):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    class _Folder:
        id = inbox_id
    inbox = _Folder()
    signer = make_signer(tmp_path)
    write_registry(signer, inbox.id)

    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=20,
        annex_object_sizes=(300,),
    )

    pins = GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]),
        workdir=tmp_path / "pin",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )

    git_factory = annex_git_factory(annex)

    # 先用正式的 init-pin 路徑建立釘選值（讀 manifest、重放 bundle、寫 pin repo）
    from aistorage.committer.run import Deps, init_pin_cli

    cfg = CommitterConfig(
        repo=sandbox.pin_repo_name(),
        repo_uuid=annex.uuid,
        repo_url=annex.url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=20,
    )
    deps = Deps(
        drive=real_drive,
        pins=pins,
        git_factory=git_factory,
        registry=None,  # init-pin 不用
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(),
        clock=SystemClock(),
    )
    sandbox.register_pin_store(pins)
    state = init_pin_cli(cfg, deps, confirm=True)
    assert state.repo_uuid == annex.uuid
    assert state.refs["refs/heads/main"] == annex.main_sha
    assert state.annex_keys, "init-pin 必須讀到 annex key 集合（H5：讀不到要 fail-closed）"

    # 收件匣：兩個 session ＋ handoff ＋ claim（接續點釘在 S1 的最後一則訊息）
    reading_s1 = _reading_for(RAW_S1, f"opencode:{S1}")
    # 接續點必須是最後一則「已完成且未被撤銷」的訊息（apply 的 M5），
    # 黃金樣本最後兩則是 reverted，不能拿來當接續點。
    continuable = [
        m for m in reading_s1["messages"] if m["completed"] and not m["reverted"]
    ]
    last_message_id = continuable[-1]["message_id"]
    _put_session(real_drive, inbox.id, signer, RAW_S1, S1, "2026-09-27T08:00:00Z")
    _put_session(real_drive, inbox.id, signer, RAW_S2, S2, "2026-09-27T08:10:00Z")
    _put_handoff_and_claim(
        real_drive, inbox.id, signer,
        target_session_id=f"opencode:{S1}",
        claimer_session_id=f"opencode:{S2}",
        target_raw_sha=reading_s1["snapshot_sha256"],
        last_message_id=last_message_id,
    )

    deps = Deps(
        drive=real_drive,
        pins=pins,
        git_factory=git_factory,
        registry=__import__("aistorage.identity", fromlist=["load_registry"]).load_registry(
            signer.registry_path, allow_example=False
        ),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(),
        clock=SystemClock(),
    )
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"中止於 {report.aborted_at}:{report.code}"
    assert report.counts["scanned_items"] == 4
    assert report.counts["accepted"] == 4  # 兩個 session ＋ handoff ＋ claim
    assert report.counts["rejected"] == 0

    # 收件匣清空（2 個 session 各 3 檔、handoff/claim 各 2 檔）
    assert real_drive.list_children(inbox.id) == []

    # 釘選值已轉正，且 pending 不再存在
    promoted, pending = pins.load(cfg.repo)
    assert pending is None
    # 釘選值必須記到「真的」每一個 annex key：閱讀版／meta.json 也都在 annex 裡。
    # （曾經因為 git annex find --format 不換行，key 全被串成一行，pin 只記到一個
    #   垃圾字串，下一輪 sweep 就把這些物件全隔離了。）
    assert len(promoted.annex_keys) >= 2, promoted.annex_keys
    assert all(k.startswith("SHA256E-") and len(k) > 20 for k in promoted.annex_keys), \
        promoted.annex_keys
    # Drive 上真的 annex 物件都要在釘選值裡，否則下一輪會被隔離
    on_drive = {
        f.name for f in real_drive.list_children(prefix_id) if f.name.startswith("SHA256E-")
    }
    assert on_drive <= set(promoted.annex_keys), sorted(on_drive - set(promoted.annex_keys))
    assert promoted.manifest_sha256 != state.manifest_sha256
    assert promoted.refs["refs/heads/main"] != annex.main_sha  # 這一輪有新 commit

    # 收件匣再上傳同一份 → ALREADY，不產生新的 commit
    _put_session(real_drive, inbox.id, signer, RAW_S1, S1, "2026-09-27T08:00:00Z")
    report2 = run(cfg, deps, dry_run=False)
    assert report2.ok is True, f"第二輪中止於 {report2.aborted_at}:{report2.code}"
    assert report2.counts["already"] == 1
    assert report2.counts["accepted"] == 0
    assert real_drive.list_children(inbox.id) == []
    promoted2, pending2 = pins.load(cfg.repo)
    assert pending2 is None
    # ALREADY 不產生新的 Session 內容：兩輪之間只有 _committer/（清冊）被改動。
    # （refs 仍可能變：`git annex copy` 會寫 location log，H1 之後這是預期行為。）
    changed = files_changed_between(
        annex.url, promoted.refs["refs/heads/main"], promoted2.refs["refs/heads/main"],
        tmp_path, it_settings["rclone_conf"], annex.workdir,
    )
    assert changed, "第二輪應該只有清冊被寫入"
    assert all(name.startswith("_committer/") for name in changed), changed

    # Drive 上沒有留下隔離的東西
    assert real_drive.list_children(quarantine_id) == []

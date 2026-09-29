"""第 3 組 8.3 整合案例 3：注入（真 main＋偽造物、上一版 manifest 冒充、未引用的 annex 物件、上層同名資料夾）。

驗證的是 design D2／`plan_sweep` 的規則在**真的 Drive** 上也成立：
- 前綴裡被塞進去的東西（上一版 manifest 改名冒充、未被引用的 annex 物件、同名資料夾、
  名字像 bundle 但內容雜湊對不上的假檔）一律被隔離，不得留在真本裡；
- 隔離是「搬到隔離資料夾」，不是刪除，隔離資料夾本身留在原地；
- 下一輪恢復正常：真本只剩 manifest＋bundle，釘選值與 Drive 一致，收件匣照常清空。
"""

from __future__ import annotations

import hashlib

import pytest

from aistorage.annex.git import SubprocessAnnexGit
from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    _put_session,
    _raw_for,
    annex_git_factory,
    build_annex_repo,
    main_sha_of,
    make_signer,
    write_registry,
)

pytestmark = pytest.mark.integration

S1 = "ses_opencode_basic_201"
S2 = "ses_opencode_basic_202"


def _names_under(drive, folder_id: str) -> set[str]:
    """資料夾底下（遞迴）所有檔案與資料夾的名稱；隔離會依日期分層。"""
    names: set[str] = set()
    stack = [folder_id]
    while stack:
        current = stack.pop()
        for child in drive.list_children(current):
            names.add(child.name)
            if child.is_folder:
                stack.append(child.id)
    return names


def test_injected_artifacts_are_quarantined_and_next_round_recovers(
    it_settings, real_drive, sandbox, tmp_path
):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    class _Folder:
        id = inbox_id

    inbox = _Folder()
    annex = build_annex_repo(
        prefix=prefix_name,
        workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=20,
        annex_object_sizes=(300,),
    )
    git_factory = annex_git_factory(annex)
    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    pin_repo = it_settings["pin_repo_url"]
    pins = GitPinStore(
        pin_repo, tmp_path / "pin",
        key_path=it_settings["pin_key"], known_hosts_path=it_settings["known_hosts"],
    )
    cfg = CommitterConfig(
        repo=sandbox.pin_repo_name(),
        repo_uuid=annex.uuid,
        repo_url=annex.url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=pin_repo,
        max_git_bundles=20,
    )
    deps = Deps(
        drive=real_drive,
        pins=pins,
        git_factory=git_factory,
        registry=load_registry(signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(),
        clock=SystemClock(),
    )
    sandbox.register_pin_store(pins)
    init_pin_cli(cfg, deps, confirm=True)
    state, _ = pins.load(cfg.repo)

    # ── 注入五種不該存在的東西（都用真的 Drive 寫入）──────────────
    manifest_name = f"GITMANIFEST--{annex.uuid}"
    real_manifest = next(
        f for f in real_drive.list_children(prefix_id) if f.name == manifest_name
    )
    manifest_bytes = real_drive.download_bytes(real_manifest.id, max_bytes=1 << 20)
    injected: list[str] = []

    # (1) 上一版 manifest 冒充：拿初始 manifest 的內容，改成主 manifest 的名字再放一份
    real_drive.create(prefix_id, real_manifest.name, manifest_bytes)
    injected.append("舊 manifest 冒充")

    # (1b) review-1926cd3 H1：**內容完全合法**的第二份主 manifest（多一個換行，
    #      解析得過、引用的 bundle 也都在）。這是舊規則最嚴重的洞：它會被 HOLD，
    #      於是 `verify_clone` 的「主 manifest 恰好一份」每一輪都失敗，提交流程
    #      永久停擺，而任何一個住民都做得到（讀取身分可以讀整個前綴）。
    real_drive.create(prefix_id, real_manifest.name, manifest_bytes + b"\n")
    injected.append("合法的第二份主 manifest")

    # (2) 名字像 bundle、但內容雜湊對不上（夾帶 payload）
    fake_bundle = f"GITBUNDLE-{annex.uuid[:8]}-{'0' * 64}"
    real_drive.create(prefix_id, fake_bundle, b"not a real bundle", mime_type="application/octet-stream")
    injected.append("假 bundle")

    # (3) 未被引用的 annex 物件（key 不在釘選值裡）
    orphan = f"SHA256E-s300--{'a' * 64}"
    real_drive.create(prefix_id, orphan, b"x" * 307200, mime_type="application/octet-stream")
    injected.append("未引用的 annex 物件")

    # (4) 上層同名資料夾：repo 佈局必須是平的
    sub = real_drive.create(
        prefix_id, f"GITBUNDLE-{annex.uuid[:8]}-decoy", b"",
        mime_type="application/vnd.google-apps.folder",
    )
    real_drive.create(sub.id, "payload.txt", b"decoy", mime_type="text/plain")
    injected.append("同名子資料夾")

    before = {f.name for f in real_drive.list_children(prefix_id)}
    assert manifest_name in before

    # ── 注入之後照常跑一輪 ───────────────────────────────────────
    _put_session(real_drive, inbox_id, signer, _raw_for(S1), S1, "2026-09-27T11:00:00Z")
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"注入後這一輪應該要成功，中止於 {report.aborted_at}:{report.code}"
    assert report.counts["rejected"] == 0
    assert report.counts["accepted"] == 1
    assert report.counts["quarantined_files"] >= 5, report.counts
    assert report.counts["held_files"] == 0, (
        f"注入物不該被 HOLD（等於讓它留在真本裡）: {report.held_files}"
    )
    assert report.counts["need_admin_files"] == 0, (
        f"注入物不該變成 NEED_ADMIN（等於沒有人處理的假訊號）: "
        f"{report.need_admin_files}"
    )

    # 隔離是搬走，不是刪除：隔離資料夾裡（依日期分層）應該找得到那幾樣東西
    quarantined = _names_under(real_drive, quarantine_id)
    assert fake_bundle in quarantined, quarantined
    assert orphan in quarantined, quarantined
    assert sub.name in quarantined, quarantined
    # 冒充的 manifest 有兩份被搬走（另一份是合法的主 manifest，仍留在前綴）
    assert real_manifest.name in quarantined, quarantined

    # 真本裡不再有注入物，只剩 git-remote-annex 自己的東西（manifest／bundle／annex 物件）
    after = {f.name for f in real_drive.list_children(prefix_id)}
    assert fake_bundle not in after, "假 bundle 還留在真本裡"
    assert orphan not in after, "未引用的 annex 物件還留在真本裡"
    assert sub.name not in after, "子資料夾還留在真本裡"
    assert manifest_name in after, "合法的主 manifest 不該被搬走"
    # H1：主 manifest 恰好一份（`verify_clone` 就是這樣檢查的）
    main_manifests = [
        f for f in real_drive.list_children(prefix_id) if f.name == manifest_name
    ]
    assert len(main_manifests) == 1, (
        f"主 manifest 必須恰好一份（否則 verify_clone 每一輪都中止）: "
        f"{[f.id for f in main_manifests]}"
    )
    promoted, pending_now = pins.load(cfg.repo)
    assert pending_now is None
    assert main_manifests[0].sha256 == promoted.manifest_sha256, (
        "留下來的那份必須是這一輪轉正後的正式 manifest（不是任何一份注入物）"
    )
    assert {n for n in after if n.startswith("GITMANIFEST--")} <= {
        manifest_name, f"{manifest_name}.bak"
    }, after
    assert after == {
        n for n in after
        if n.startswith(("GITBUNDLE-", "SHA256E-", "GITMANIFEST--"))
    }, after

    # ── 下一輪恢復：隔離乾淨之後照常提交，不再隔離任何東西 ───────────
    # （收件匣為空的一輪會在第 2 步提早結束、不做清掃，所以這裡放第二個 session，
    #   讓恢復輪真的走完 clone → settle → sweep → push 的完整路徑。）
    _put_session(real_drive, inbox_id, signer, _raw_for(S2), S2, "2026-09-27T11:30:00Z")
    empty_report = run(cfg, deps, dry_run=False)
    assert empty_report.ok is True, f"恢復輪中止於 {empty_report.aborted_at}:{empty_report.code}"
    assert empty_report.counts["accepted"] == 1
    assert empty_report.counts["quarantined_files"] == 0, "恢復輪不該再隔離任何東西"

    state_after, pending = pins.load(cfg.repo)
    assert pending is None
    assert state_after.manifest_sha256 != state.manifest_sha256, "這一輪應該推進了 manifest"
    assert (
        main_sha_of(annex, it_settings["rclone_conf"]) == state_after.refs["refs/heads/main"]
    ), "釘選值與 Drive 上的真本不一致"
    assert real_drive.list_children(inbox_id) == [], "恢復輪應該把收件匣清空"

"""第 3 組 8.3 整合案例 4：bundle 回收的容忍度（design D2 明文要求在 3.2 驗證）。

把 `annex.max-git-bundles` 設得很小，逼 git-remote-annex 在 push 時 consolidate：
舊 bundle 進 manifest 的 removed 清單、新的 consolidated bundle 出現。驗證：
- 回收後（removed 的 bundle 被永久刪除）仍然 clone 得到完整 refs；
- 回收之後還能繼續正常 push、再回收一次也正常；
- 每一輪結束時 manifest 的 active bundle 都還在 Drive 上（可重放），
  釘選值與真本一致。
"""

from __future__ import annotations

import pytest

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

#: 設成 2：push 兩次就會 consolidate，removed 清單一定不是空的
MAX_BUNDLES = 2


def test_bundle_consolidation_then_gc_then_keep_working(
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
        max_git_bundles=MAX_BUNDLES,
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
        repo="agora",
        repo_uuid=annex.uuid,
        repo_url=annex.url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=pin_repo,
        max_git_bundles=MAX_BUNDLES,
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
    init_pin_cli(cfg, deps, confirm=True)

    def _bundle_names() -> set[str]:
        return {f.name for f in real_drive.list_children(prefix_id) if f.name.startswith("GITBUNDLE-")}

    def _manifest():
        from aistorage.annex.manifest import parse_manifest

        name = f"GITMANIFEST--{annex.uuid}"
        f = next(x for x in real_drive.list_children(prefix_id) if x.name == name)
        return parse_manifest(
            real_drive.download_bytes(f.id, max_bytes=1 << 20), repo_uuid=annex.uuid
        )

    # ── 連續三輪：每一輪都 push，前兩輪就會觸發 consolidate ────────
    reports = []
    for round_idx in range(3):
        _put_session(
            real_drive, inbox_id, signer,
            _raw_for(f"ses_opencode_basic_3{round_idx:02d}"),
            f"ses_opencode_basic_3{round_idx:02d}",
            f"2026-09-27T12:{round_idx:02d}:00Z",
        )
        report = run(cfg, deps, dry_run=False)
        assert report.ok is True, f"第 {round_idx + 1} 輪中止於 {report.aborted_at}:{report.code}"
        assert report.counts["accepted"] == 1
        reports.append(report)

    # consolidate 有發生，而且 removed 的 bundle 已被回收（gc_deleted > 0）
    # （回收發生在被 consolidate 的那一輪，不是最後一輪）
    manifest = _manifest()
    assert sum(r.counts["gc_deleted"] for r in reports) >= 1, [r.counts for r in reports]
    assert manifest.removed, "設定的 max-git-bundles 太小，應該會 consolidate"
    # Drive 上不該再有被回收的 bundle
    remaining = _bundle_names()
    assert not (remaining & set(manifest.removed)), (remaining & set(manifest.removed))

    # ── 回收之後：manifest 的 active bundle 都還在，而且 refs 完整 ───
    assert remaining >= set(manifest.active), (remaining, manifest.active)
    state, pending = pins.load("agora")
    assert pending is None
    assert (
        main_sha_of(annex, it_settings["rclone_conf"]) == state.refs["refs/heads/main"]
    ), "釘選值與 Drive 上的真本不一致"

    # ── 回收之後還能繼續 push（第四輪），並且再回收一次也正常 ────────
    _put_session(
        real_drive, inbox_id, signer,
        _raw_for("ses_opencode_basic_399"), "ses_opencode_basic_399",
        "2026-09-27T12:39:00Z",
    )
    report4 = run(cfg, deps, dry_run=False)
    assert report4.ok is True, f"回收後的下一輪中止於 {report4.aborted_at}:{report4.code}"
    assert report4.counts["accepted"] == 1

    manifest_after = _manifest()
    state_after, pending_after = pins.load("agora")
    assert pending_after is None
    assert _bundle_names() >= set(manifest_after.active), "active 的 bundle 必須都還在"
    assert (
        main_sha_of(annex, it_settings["rclone_conf"])
        == state_after.refs["refs/heads/main"]
    ), "釘選值與 Drive 上的真本不一致"
    assert real_drive.list_children(inbox_id) == [], "收件匣應該清空"

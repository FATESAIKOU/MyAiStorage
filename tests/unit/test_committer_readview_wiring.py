"""4.1 接線到提交流程的單元測試（run.py 第 4 步與第 13 步、config 新欄位、4.4 的 created_time）。

用 FakeDrive 走真實的 run()，驗證：
- 第 4 步：讀取視圖的可信集合來自 manifest（trusted_ids），不在集合內的檔案被隔離；
  沒有 manifest 設定／還是初始 manifest 時跳過（不隔離任何東西）；
- 第 13 步：設了讀取視圖就用 DriveReadViewPublisher；發佈失敗只標記 publish_failed，
  真本與收件匣照常（第 12 步已轉正、第 14 步照清）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.annex.fake import FakeAnnexGit, create_fake_git_bundle
from aistorage.clock import FixedClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, run
from aistorage.drive.fake import FakeDrive
from aistorage.errors import ReadError
from aistorage.identity import Registry
from aistorage.integrity.pin import MemoryPinStore, PinState
from aistorage.intake.scan import scan_inboxes

from test_committer_smoke import (
    SimpleTestConverter,
    _registry_payload,
    _seed_valid_inbox_session,
)


def _readview_manifest(
    *,
    generation: int = 1,
    files: tuple[str, ...] = (),
    index_id: str | None = None,
) -> bytes:
    """組出符合 schemas/readview-manifest.schema.json 的 manifest 位元組。"""
    return json.dumps(
        {
            "format": "aistorage.readview/v1",
            "element": "agora",
            "generation": generation,
            "published_at": "2026-09-27T09:00:00Z",
            "agora_main_sha": "0" * 40,
            "converter_versions": {"opencode": "1"},
            "rebuild_epoch": 0,
            "index": (
                {"name": "index.jsonl", "file_id": index_id, "sha256": "1" * 64, "size": 10}
                if index_id
                else None
            ),
            "files": list(files),
            "pending": [],
            "retired": [],
        },
        sort_keys=True,
    ).encode("utf-8")


def _env(
    tmp_path: Path,
    *,
    readview_folder_id: str | None = None,
    readview_manifest_file_id: str | None = None,
    publisher=None,
    seed_session: bool = True,
):
    """組出跑一輪完整 run() 所需的 Fake 環境。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)

    prefix_folder_id = drive.seed_folder("prefix_agora")
    quarantine_folder_id = drive.seed_folder("quarantine_agora")
    inbox_folder_id = drive.seed_folder("inbox_mac_opencode")

    import base64

    from aistorage.identity import generate_keypair

    priv_bytes, pub_bytes = generate_keypair()
    key_id = f"mac-opencode-{hashlib.sha256(pub_bytes).hexdigest().lower()[:8]}"
    registry = Registry(
        _registry_payload(inbox_folder_id, key_id, base64.b64encode(pub_bytes).decode("ascii"))
    )

    repo_uuid = "00000000-0000-0000-0000-000000000001"
    b1_name, b1_bytes, main_sha, annex_sha = create_fake_git_bundle(
        tmp_path / "bundle_init", repo_uuid
    )
    drive.seed_file(prefix_folder_id, b1_name, b1_bytes, created_time="2026-09-27T08:00:00Z")
    manifest_bytes = f"{b1_name}\n".encode("utf-8")
    drive.seed_file(
        prefix_folder_id, f"GITMANIFEST--{repo_uuid}", manifest_bytes,
        created_time="2026-09-27T08:00:00Z",
    )
    state = PinState(
        repo="agora",
        repo_uuid=repo_uuid,
        refs={"refs/heads/main": main_sha, "refs/heads/git-annex": annex_sha},
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest().lower(),
        prev_manifest_sha256=None,
        active_bundles=(b1_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-init",
    )
    pins = MemoryPinStore(initial_state=state)
    fake_git = FakeAnnexGit(
        refs={"refs/heads/main": main_sha, "refs/heads/git-annex": annex_sha},
        workdir=tmp_path / "git_workdir",
        annex_keys=frozenset(),
        drive=drive,
        prefix_folder_id=prefix_folder_id,
        repo_uuid=repo_uuid,
        clock=clock,
    )
    deps = Deps(
        drive=drive,
        pins=pins,
        git_factory=lambda _p: fake_git,
        registry=registry,
        converters={"opencode": SimpleTestConverter()},
        publisher=publisher or NullPublisher(),
        clock=clock,
    )
    cfg = CommitterConfig(
        repo="agora",
        repo_uuid=repo_uuid,
        repo_url="drive://agora",
        prefix_folder_id=prefix_folder_id,
        quarantine_folder_id=quarantine_folder_id,
        identity_registry_path="config/identity.json",
        readview_folder_id=readview_folder_id,
        readview_manifest_file_id=readview_manifest_file_id,
    )
    extra = {
        "drive": drive,
        "inbox_folder_id": inbox_folder_id,
        "priv_bytes": priv_bytes,
        "key_id": key_id,
        "prefix_folder_id": prefix_folder_id,
        "quarantine_folder_id": quarantine_folder_id,
    }
    if seed_session:
        _seed_valid_inbox_session(drive, inbox_folder_id, priv_bytes, key_id)
    return cfg, deps, extra


# ---------------------------------------------------------------------
# 第 4 步：讀取視圖清掃的可信集合
# ---------------------------------------------------------------------


def test_readview_sweep_skipped_without_manifest_config(tmp_path: Path) -> None:
    """沒有設定 manifest → 跳過讀取視圖清掃（不隔離任何東西）。"""
    clock = FixedClock("2026-09-27T10:00:00Z")
    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    extra["drive"].seed_file(rv_id, "stray.json", b"{}", created_time="2026-09-27T09:00:00Z")

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, report.aborted_at
    assert report.readview_sweep == "skipped_no_manifest"
    # 讀取視圖裡的檔案沒被動（沒有可信集合就不清掃）
    assert [f.name for f in deps.drive.list_children(rv_id)] == ["stray.json"]


def test_readview_sweep_skipped_when_manifest_is_initial(tmp_path: Path) -> None:
    """manifest 還是 generation=0（管理者剛建立）→ 跳過清掃。"""
    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    deps.drive.seed_file(rv_id, "stray.json", b"{}", created_time="2026-09-27T09:00:00Z")
    mv_id = deps.drive.seed_file(
        rv_id, "readview-manifest.json", _readview_manifest(generation=0),
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = dataclasses.replace(cfg, readview_manifest_file_id=mv_id)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, report.aborted_at
    assert report.readview_sweep == "skipped_initial"
    # manifest 與它旁邊的檔案都沒被動（還沒有可信集合就隔離 = 資料全毀）
    assert {f.name for f in deps.drive.list_children(rv_id)} == {
        "readview-manifest.json", "stray.json",
    }


def test_readview_sweep_keeps_trusted_and_quarantines_the_rest(tmp_path: Path) -> None:
    """可信集合＝manifest 記載的 file id：集合內保留、集合外隔離。"""
    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    trusted = deps.drive.seed_file(
        rv_id, "sessions-opencode-ses_trusted.json", b'{"session_id":"opencode:ses_trusted"}',
        created_time="2026-09-27T08:40:00Z",
    )
    deps.drive.seed_file(rv_id, "not-in-manifest.json", b"{}", created_time="2026-09-27T08:41:00Z")
    mv_id = deps.drive.seed_file(
        rv_id, "readview-manifest.json", _readview_manifest(generation=3, files=(trusted,)),
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = dataclasses.replace(cfg, readview_manifest_file_id=mv_id)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, report.aborted_at
    assert report.readview_sweep == "swept"
    # manifest 自己與被引用的檔案留在讀取視圖；其餘被搬走
    assert {f.name for f in deps.drive.list_children(rv_id)} == {
        "sessions-opencode-ses_trusted.json",
        "readview-manifest.json",
    }


def test_readview_sweep_fails_closed_when_manifest_corrupted(tmp_path: Path) -> None:
    """manifest 讀不到／損毀 → 整輪中止（不猜可信集合）。"""
    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    mv_id = deps.drive.seed_file(
        rv_id, "readview-manifest.json", b"{ not json",
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = dataclasses.replace(cfg, readview_manifest_file_id=mv_id)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.aborted_at == "integrity.sweep"
    # 隔離資料夾不該多出東西（中止發生在套用清掃之前）
    assert deps.drive.list_children(cfg.quarantine_folder_id) == []


# ---------------------------------------------------------------------
# 第 13 步：DriveReadViewPublisher
# ---------------------------------------------------------------------


def test_publish_uses_readview_publisher_when_configured(tmp_path: Path) -> None:
    """設定了讀取視圖就用 DriveReadViewPublisher（不沿用 deps.publisher）。"""
    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    mv_id = deps.drive.seed_file(
        rv_id, "readview-manifest.json", _readview_manifest(generation=0),
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = dataclasses.replace(cfg, readview_manifest_file_id=mv_id)

    called: dict[str, Any] = {}

    class _SpyPublisher(NullPublisher):
        def publish(self, store, *, dry_run: bool = False, **kwargs: Any) -> None:
            called["used"] = True
            return None

    deps = dataclasses.replace(deps, publisher=_SpyPublisher())
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, report.aborted_at
    # 有讀取視圖設定 → 走 DriveReadViewPublisher，注入的 publisher 不該被用到
    assert "used" not in called, "有讀取視圖設定時不該走 deps.publisher"
    assert report.readview_publish not in (None, "skipped_no_readview"), report.readview_publish


def test_publish_failure_does_not_abort_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """發佈失敗只標記 publish_failed：真本已轉正、收件匣照清、這一輪仍算成功。"""
    import aistorage.publish.publisher as publisher_mod

    rv_id = "rv-folder"
    cfg, deps, extra = _env(tmp_path, readview_folder_id=rv_id)
    deps.drive.seed_folder(rv_id)
    mv_id = deps.drive.seed_file(
        rv_id, "readview-manifest.json", _readview_manifest(generation=0),
        created_time="2026-09-27T08:30:00Z",
    )
    cfg = dataclasses.replace(cfg, readview_manifest_file_id=mv_id)

    def _boom(self, store, **kwargs: Any):
        raise ReadError("模擬發佈失敗")

    monkeypatch.setattr(publisher_mod.DriveReadViewPublisher, "publish", _boom)

    report = run(cfg, deps, dry_run=False)

    assert report.readview_publish == "publish_failed"
    assert report.publish_error == "ReadError"
    assert report.ok is True, f"發佈失敗不得中止這一輪：{report.aborted_at}:{report.code}"
    # 收件匣照常清空
    assert not scan_inboxes(deps.drive, deps.registry).items


# ---------------------------------------------------------------------
# config 新欄位
# ---------------------------------------------------------------------


def test_config_reads_readview_fields(tmp_path: Path) -> None:
    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(
        json.dumps(
            {
                "format": "aistorage.committer/v1",
                "repo": "agora",
                "repo_uuid": "uuid-1",
                "repo_url": "drive://agora",
                "prefix_folder_id": "pf",
                "quarantine_folder_id": "qf",
                "identity_registry_path": "config/identity.json",
                "readview_folder_id": "rv",
                "readview_manifest_file_id": "rv-manifest",
                "readview_rebuild_epoch": 2,
            }
        ),
        encoding="utf-8",
    )
    cfg = CommitterConfig.load(cfg_path, env={})

    assert cfg.readview_folder_id == "rv"
    assert cfg.readview_manifest_file_id == "rv-manifest"
    assert cfg.readview_rebuild_epoch == 2


def test_config_rejects_bad_rebuild_epoch(tmp_path: Path) -> None:
    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(
        json.dumps(
            {
                "format": "aistorage.committer/v1",
                "repo": "agora",
                "repo_uuid": "uuid-1",
                "repo_url": "drive://agora",
                "prefix_folder_id": "pf",
                "quarantine_folder_id": "qf",
                "identity_registry_path": "config/identity.json",
                "readview_rebuild_epoch": -1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="readview_rebuild_epoch"):
        CommitterConfig.load(cfg_path, env={})


def test_config_defaults_readview_fields_to_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(
        json.dumps(
            {
                "format": "aistorage.committer/v1",
                "repo": "agora",
                "repo_uuid": "uuid-1",
                "repo_url": "drive://agora",
                "prefix_folder_id": "pf",
                "quarantine_folder_id": "qf",
                "identity_registry_path": "config/identity.json",
            }
        ),
        encoding="utf-8",
    )
    cfg = CommitterConfig.load(cfg_path, env={})

    assert cfg.readview_manifest_file_id is None
    assert cfg.readview_rebuild_epoch == 0


# ---------------------------------------------------------------------
# 4.4：驗章前拒收的 deletable_after 以檔案 created_time 起算
# ---------------------------------------------------------------------


def test_pre_auth_reject_uses_file_created_time(tmp_path: Path) -> None:
    """bad_signature 拒收的 deletable_after 由項目的 created_time 決定，不是「當下」。

    4.4（PM 決定 4）：驗章前的拒收每一輪都會重新評估，若用「當下」當 rejected_at，
    deletable_after 永遠到不了，驗章前被拒的檔案永遠留在收件匣。
    """
    from aistorage.intake.evaluate import evaluate

    clock = FixedClock("2026-09-27T10:00:00Z")
    drive = FakeDrive(clock=clock)
    inbox_id = drive.seed_folder("inbox_bad_sig")
    prefix_id = drive.seed_folder("prefix_agora")
    # 項目躺進收件匣的時間：兩天前
    drive.seed_file(inbox_id, "01AAAAAAAAAAAAAAAAAAAAAAAA.sig", b'{"bad": true}',
                    created_time="2026-09-25T08:00:00Z")
    drive.seed_file(inbox_id, "01AAAAAAAAAAAAAAAAAAAAAAAA.raw", b'{"x": 1}',
                    created_time="2026-09-25T08:00:00Z")
    drive.seed_file(inbox_id, "01AAAAAAAAAAAAAAAAAAAAAAAA.sidecar.json", b'{"format": "x"}',
                    created_time="2026-09-25T08:00:00Z")

    import base64

    from aistorage.identity import generate_keypair

    priv_bytes, pub_bytes = generate_keypair()
    key_id = f"mac-opencode-{hashlib.sha256(pub_bytes).hexdigest().lower()[:8]}"
    registry = Registry(
        _registry_payload(inbox_id, key_id, base64.b64encode(pub_bytes).decode("ascii"))
    )
    from aistorage.agora.store import AgoraStore, FakeRawStorage
    from aistorage.intake.ledger import Ledger

    store = AgoraStore(tmp_path / "wt", FakeRawStorage())
    ledger = Ledger(store)

    scan = scan_inboxes(drive, registry)
    assert len(scan.items) == 1
    decision = evaluate(
        scan.items[0],
        drive=drive,
        registry=registry,
        store=store,
        ledger=ledger,
        clock=clock,
        workdir=tmp_path / "work",
    )

    assert decision.kind.value == "reject"
    assert decision.rejected_at is not None
    assert decision.rejected_at.startswith("2026-09-25T08:00:00"), decision.rejected_at
    assert decision.deletable_after is not None
    # 2026-09-25T08:00 ＋ 24h = 2026-09-26T08:00，早於「現在」，所以這一輪就可刪
    assert clock.now() >= decision.deletable_after

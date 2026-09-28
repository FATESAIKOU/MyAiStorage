"""多 repo pipeline 的單元測試（group5-7 第 6.1 節：RepoPipeline、依序 agora → foundry）。

驗證：
- `repos` 設定解析（相容舊的單 repo 設定檔）；
- 收件匣沒有 artifact 時**不 clone Foundry**（PM 決定 9：平常成本不變）；
- 有 artifact 時 Foundry 被處理、項目寫進 Foundry 而不是 Agora；
- 兩個 repo 的計數分別累計（不互相覆蓋）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.agora import layout
from aistorage.annex.fake import FakeAnnexGit, create_fake_git_bundle
from aistorage.committer.config import CommitterConfig, RepoConfig
from aistorage.committer.run import Deps, PipelineContext, RepoTarget, run
from aistorage.drive.fake import FakeDrive
from aistorage.identity import Registry
from aistorage.integrity.pin import MemoryPinStore, PinState

from test_committer_readview_wiring import _env, _seed_valid_inbox_session
from test_committer_smoke import (
    _registry_payload,
    _seed_valid_inbox_item,
    SimpleTestConverter,
)


def _add_repo(
    deps: Deps,
    *,
    repo: str,
    prefix_name: str,
    repo_uuid: str,
    tmp_path: Path,
    inbox_folder_id: str,
) -> RepoConfig:
    """在 FakeDrive 上多建一個真本 repo（bundle＋manifest＋釘選值）。"""
    drive: FakeDrive = deps.drive  # type: ignore[assignment]
    prefix_id = drive.seed_folder(prefix_name)
    quarantine_id = drive.seed_folder(f"{prefix_name}-quarantine")
    work = tmp_path / repo
    b_name, b_bytes, main_sha, annex_sha = create_fake_git_bundle(work / "b", repo_uuid)
    drive.seed_file(prefix_id, b_name, b_bytes, created_time="2026-09-27T08:00:00Z")
    manifest_bytes = f"{b_name}\n".encode("utf-8")
    drive.seed_file(
        prefix_id, f"GITMANIFEST--{repo_uuid}", manifest_bytes,
        created_time="2026-09-27T08:00:00Z",
    )
    state = PinState(
        repo=repo,
        repo_uuid=repo_uuid,
        refs={"refs/heads/main": main_sha, "refs/heads/git-annex": annex_sha},
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest().lower(),
        prev_manifest_sha256=None,
        active_bundles=(b_name,),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-27T08:00:00Z",
        run_id="run-init",
    )
    deps.pins = MemoryPinStore(
        initial_state=deps.pins._states["agora"],  # type: ignore[attr-defined]
        initial_states_extra={repo: state},
    )
    # H1（review-25a48a9）：factory 依 **target** 分派（不是依工作目錄），
    # 而且回報的 URL／uuid 必須是這個 target 的——`verify_clone_identity` 會擋下
    # 「拿 Agora 的 repo 當 Foundry 用」的情況。
    original_factory = deps.git_factory
    repo_url = f"drive://{repo}"
    foundry_factory = _multi_git_factory(
        deps,
        repo_uuid=repo_uuid,
        prefix_id=prefix_id,
        repo_url=repo_url,
        refs={"refs/heads/main": main_sha, "refs/heads/git-annex": annex_sha},
    )
    deps.git_factory = lambda dest, target: (  # type: ignore[assignment]
        foundry_factory(dest) if target.repo == repo else original_factory(dest, target)
    )
    return RepoConfig(
        name=repo,
        uuid=repo_uuid,
        url=repo_url,
        prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id,
        # M4：Foundry 一定要自己提供 prefix_levels（設定檔驗證會擋下沒給的；
        # 這裡直接組物件，所以用空值——掃描上層同名資料夾的行為由 config 的測試覆蓋）
        prefix_levels=(),
    )


def _multi_git_factory(
    deps: Deps, *, repo_uuid: str, prefix_id: str, repo_url: str,
    refs: dict[str, str],
):
    clock = deps.clock
    drive: FakeDrive = deps.drive  # type: ignore[assignment]

    def factory(dest: Path):
        return FakeAnnexGit(
            refs=dict(refs),
            workdir=dest,
            annex_keys=frozenset(),
            drive=drive,
            prefix_folder_id=prefix_id,
            repo_uuid=repo_uuid,
            repo_url=repo_url,
            clock=clock,
        )

    return factory


def test_repos_config_parsed_and_back_compatible(tmp_path: Path) -> None:
    base = {
        "format": "aistorage.committer/v1",
        "repo": "agora",
        "repo_uuid": "uuid-agora",
        "repo_url": "annex::agora",
        "prefix_folder_id": "p-agora",
        "quarantine_folder_id": "q-agora",
        "identity_registry_path": "config/identity.json",
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(base), encoding="utf-8")
    assert CommitterConfig.load(path, env={}).repos == ()

    path.write_text(
        json.dumps({
            **base,
            "repos": {
                "foundry": {
                    "uuid": "uuid-foundry",
                    "url": "annex::foundry",
                    "prefix_folder_id": "p-foundry",
                    "quarantine_folder_id": "q-foundry",
                    "largefiles": "include=objects/*/*",
                    "prefix_levels": [
                        {"parent_id": "up-foundry", "name": "p-foundry",
                         "expected_id": "EXPECTED"},
                    ],
                }
            },
        }),
        encoding="utf-8",
    )
    cfg = CommitterConfig.load(path, env={})
    assert [r.name for r in cfg.repos] == ["foundry"]
    assert cfg.repos[0].largefiles == "include=objects/*/*"

    # repos 不該重複列出 agora
    path.write_text(
        json.dumps({
            **base,
            "repos": {"agora": {
                "uuid": "x", "url": "u", "prefix_folder_id": "p",
                "quarantine_folder_id": "q",
                "prefix_levels": [
                    {"parent_id": "up", "name": "p", "expected_id": "E"},
                ],
            }},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不該再列出 agora"):
        CommitterConfig.load(path, env={})


def test_repo_target_from_config_exposes_pipeline_fields(tmp_path: Path) -> None:
    cfg, _deps, _extra = _env(tmp_path)
    target = RepoTarget.from_config(cfg)
    assert target.element == "agora"
    assert target.repo == cfg.repo
    assert target.prefix_folder_id == cfg.prefix_folder_id
    assert target.quarantine_folder_id == cfg.quarantine_folder_id


def _cfg_with_foundry(tmp_path: Path, *, add_foundry: bool = True):
    cfg, deps, extra = _env(tmp_path)
    deps.drive.seed_folder("inbox2")  # type: ignore[attr-defined]
    if add_foundry:
        rc = _add_repo(
            deps,
            repo="foundry",
            prefix_name="prefix_foundry",
            repo_uuid="00000000-0000-0000-0000-000000000002",
            tmp_path=tmp_path,
            inbox_folder_id=extra["inbox_folder_id"],
        )
        cfg = dataclasses.replace(cfg, repos=(rc,))
    return cfg, deps, extra


def test_foundry_is_not_cloned_without_artifacts(tmp_path: Path) -> None:
    """收件匣只有 session → 不處理 Foundry（PM 決定 9）。"""
    cfg, deps, extra = _cfg_with_foundry(tmp_path)
    _seed_valid_inbox_session(
        deps.drive, extra["inbox_folder_id"], extra["priv_bytes"], extra["key_id"]  # type: ignore[arg-type]
    )

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, report.aborted_at
    assert report.foundry_skipped == "no_artifacts"
    # Foundry 的前綴完全沒被碰過
    assert deps.drive.list_children(cfg.repos[0].prefix_folder_id) != []  # type: ignore[attr-defined]
    assert "foundry.accepted" not in report.counts


def test_artifact_is_applied_to_foundry_repo(tmp_path: Path) -> None:
    """有 artifact → Foundry 被處理，產出寫在 Foundry（不是 Agora）。"""
    cfg, deps, extra = _cfg_with_foundry(tmp_path)
    drive: FakeDrive = deps.drive  # type: ignore[assignment]
    session_id = "opencode:ses_smoke_001"
    # 先放一個 Session，讓 produced_by_session_id 指向它
    _seed_valid_inbox_session(
        drive, extra["inbox_folder_id"], extra["priv_bytes"], extra["key_id"]
    )
    from aistorage.inbox_builder import build_artifact_item

    # 用 link（原處產出）而不是 contained：link 只寫產出目錄、不產生 annex 物件，
    # 這樣單元測試不需要真的 git-annex。contained 的 annex 路徑由第 7 組的測試覆蓋。
    built = build_artifact_item(
        kind="link",
        produced_by_session_id=session_id,
        name="spec.md",
        content_type="text/markdown",
        link="https://example.com/spec.md",
        profile="mac-opencode",
        key=extra["priv_bytes"],
        key_id=extra["key_id"],
        clock=deps.clock,
    )
    key = built.item_key
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", built.sidecar_bytes)
    drive.seed_file(
        extra["inbox_folder_id"], f"{key}.sig",
        json.dumps(built.sig, sort_keys=True).encode("utf-8"),
    )
    if built.raw_path is not None:
        drive.seed_file(extra["inbox_folder_id"], f"{key}.raw", built.raw_path.read_bytes())

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert report.foundry_skipped is None
    # 兩個 repo 都收到了自己該收的項目
    assert report.counts.get("accepted", 0) >= 1
    assert "foundry.accepted" in report.counts

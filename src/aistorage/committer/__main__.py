"""AiStorage 提交流程 CLI 命令列進入點。

用法：
  python -m aistorage.committer prescan [--config CONFIG]
  python -m aistorage.committer run [--config CONFIG] [--dry-run]
  python -m aistorage.committer plan-sweep [--config CONFIG]
  python -m aistorage.committer init-pin [--config CONFIG] [--dry-run | --confirm]
  python -m aistorage.committer rebuild-readview --repo <repo> [--verify]

依據規格：docs/impl/group3-modules.md 第 7.2 節
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from aistorage.annex.git import DEFAULT_LARGEFILES, SubprocessAnnexGit
from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import (
    Deps,
    init_pin_cli,
    plan_sweep_cli,
    prescan,
    run,
)
from aistorage.converters import CONVERTERS
from aistorage.drive.auth import RcloneConfToken
from aistorage.drive.http import HttpDriveClient
from aistorage.identity import Registry, load_registry
from aistorage.integrity.pin import GitPinStore


def build_prescan_deps(cfg: CommitterConfig) -> Deps:
    """自 CommitterConfig 建立僅供 prescan 使用的輕量相依元件（僅需 Drive 與身分登錄檔，H2）。"""
    clock = SystemClock()

    if not cfg.rclone_conf_path or not Path(cfg.rclone_conf_path).is_file():
        raise RuntimeError(
            f"找不到 rclone.conf 設定檔，請確認環境變數 AISTORAGE_RCLONE_CONF (目前: {cfg.rclone_conf_path})"
        )
    token_src = RcloneConfToken(cfg.rclone_conf_path)
    drive = HttpDriveClient(token_src)

    # H4: 使用 load_registry 並強制 allow_example=False
    try:
        registry = load_registry(cfg.identity_registry_path, allow_example=False)
    except Exception as e:
        raise RuntimeError(f"載入身分登錄檔失敗 ({cfg.identity_registry_path}): {e}") from e

    return Deps(
        drive=drive,
        pins=None,  # type: ignore[arg-type]
        git_factory=None,  # type: ignore[arg-type]
        registry=registry,
        converters=CONVERTERS,
        publisher=NullPublisher(),
        clock=clock,
    )


def build_production_deps(cfg: CommitterConfig, *, allow_production: bool = False) -> Deps:
    """自 CommitterConfig 建立生產環境相依元件聚合。"""
    clock = SystemClock()

    if not cfg.rclone_conf_path or not Path(cfg.rclone_conf_path).is_file():
        raise RuntimeError(
            f"找不到 rclone.conf 設定檔，請確認環境變數 AISTORAGE_RCLONE_CONF (目前: {cfg.rclone_conf_path})"
        )
    token_src = RcloneConfToken(cfg.rclone_conf_path)
    drive = HttpDriveClient(token_src)

    pin_temp = Path(tempfile.mkdtemp(prefix="aistorage_pin_store_"))
    pins = GitPinStore(
        repo_url=cfg.pin_repo_url,
        workdir=pin_temp,
        key_path=cfg.pin_key_path,
        known_hosts_path=cfg.pin_known_hosts_path,
        allow_production=allow_production,
    )

    # H1（review-25a48a9）：URL 與 annex 規則**都從這一份設定來**。
    # 舊的 factory 只收 `dest`，URL 寫死成某個 repo 的 repo_url，於是換一份設定
    # （ADR 0009：另一個實體的閘門）時會 clone 到錯的 repo——最壞的情況是把它
    # 的內容寫進這個 repo 並 push。`largefiles` 用 agora 的單一來源規則。
    git_factory = lambda dest, cfg: SubprocessAnnexGit.clone_for_commit(
        cfg.repo_url,
        dest,
        max_git_bundles=cfg.max_git_bundles,
        largefiles=DEFAULT_LARGEFILES,
    )

    # H4: 使用 load_registry 並強制 allow_example=False
    try:
        registry = load_registry(cfg.identity_registry_path, allow_example=False)
    except Exception as e:
        raise RuntimeError(f"載入身分登錄檔失敗 ({cfg.identity_registry_path}): {e}") from e

    publisher = NullPublisher()

    return Deps(
        drive=drive,
        pins=pins,
        git_factory=git_factory,
        registry=registry,
        converters=CONVERTERS,
        publisher=publisher,
        clock=clock,
    )


def _rebuild_argv(args: argparse.Namespace) -> list[str]:
    """把 `rebuild-readview` 的子命令參數轉成 `committer.rebuild.main` 吃的 argv。"""
    argv: list[str] = []
    if args.repo:
        argv += ["--repo", args.repo]
    if args.verify:
        argv.append("--verify")
    if args.reader_config:
        argv += ["--reader-config", args.reader_config]
    if args.manifest_file_id:
        argv += ["--manifest-file-id", args.manifest_file_id]
    if args.out:
        argv += ["--out", args.out]
    if args.json:
        argv.append("--json")
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aistorage.committer",
        description="AiStorage 提交流程主體 CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # prescan
    p_prescan = subparsers.add_parser("prescan", help="預先掃描收件匣項目數")
    p_prescan.add_argument("--config", "-c", help="設定檔路徑", default=None)

    # run
    p_run = subparsers.add_parser("run", help="執行 13 步提交流程")
    p_run.add_argument("--config", "-c", help="設定檔路徑", default=None)
    p_run.add_argument("--dry-run", action="store_true", help="乾跑模式（不寫入、不推送到遠端）")

    # plan-sweep
    p_sweep = subparsers.add_parser("plan-sweep", help="計算並印出清掃計畫（唯讀）")
    p_sweep.add_argument("--config", "-c", help="設定檔路徑", default=None)

    # init-pin
    p_init = subparsers.add_parser("init-pin", help="初始化正式釘選值")
    p_init.add_argument("--config", "-c", help="設定檔路徑", default=None)
    p_init.add_argument("--i-am-admin", action="store_true", help="管理者模式，允許在非 CI 環境下存取正式 pin repo")
    init_mode = p_init.add_mutually_exclusive_group()
    init_mode.add_argument("--dry-run", action="store_true", default=True, help="僅列印計畫（預設）")
    init_mode.add_argument("--confirm", action="store_true", help="確定寫入正式釘選值至 pin repo")

    # rebuild-readview（4.5：唯讀，可在本機跑，不需寫入身分）
    p_rebuild = subparsers.add_parser(
        "rebuild-readview",
        help="從真本重建讀取視圖並（選用）與已發佈的讀取視圖比對（唯讀）",
    )
    p_rebuild.add_argument("--repo", default=None, help="真本 repo（路徑或 annex url）")
    p_rebuild.add_argument("--verify", action="store_true", help="與已發佈的讀取視圖比對")
    p_rebuild.add_argument("--reader-config", default=None, help="讀者設定檔路徑")
    p_rebuild.add_argument("--manifest-file-id", default=None, help="覆寫讀者設定裡的 manifest id")
    p_rebuild.add_argument("--out", default=None, help="重建產物的輸出目錄（預設暫存目錄）")
    p_rebuild.add_argument("--json", action="store_true", help="以 JSON 輸出報告")

    args = parser.parse_args(argv)

    # rebuild-readview 完全唯讀：不需要提交流程設定檔，所以要提早分派
    if args.command == "rebuild-readview":
        from aistorage.committer import rebuild

        return rebuild.main(_rebuild_argv(args))

    cfg = CommitterConfig.load(args.config)

    if args.command == "prescan":
        deps = build_prescan_deps(cfg)
        prescan(cfg, deps)
        return 0
    elif args.command == "run":
        deps = build_production_deps(cfg)
        report = run(cfg, deps, dry_run=args.dry_run)
        return 0 if report.ok else 1
    elif args.command == "plan-sweep":
        deps = build_production_deps(cfg)
        decisions = plan_sweep_cli(cfg, deps)
        for d in decisions:
            print(f"[{d.disposition.value.upper()}] {d.file.name} (id={d.file.id}): {d.reason}")
        return 0
    elif args.command == "init-pin":
        deps = build_production_deps(cfg, allow_production=args.i_am_admin)
        if not args.confirm:
            init_pin_cli(cfg, deps, confirm=False)
            return 0
        # 6.5：重建釘選值也是會改動真可信狀態的管理操作，照樣要走錯開。
        # 已經有維護旗標時（中途中止的收尾流程）就直接在既有的鎖裡做，
        # 沒有旗標就自己上鎖（停用 workflow、等沒有執行中的 run、重讀遠端 manifest）。
        _init_pin_under_lock(cfg, deps)
        return 0

    return 0


def _init_pin_under_lock(cfg: CommitterConfig, deps: Deps) -> None:
    """`init-pin --confirm` 的錯開外層（6.5）。

    鎖與 precheck 針對的永遠是**這份設定檔描述的那個**實體（期 1 是 Agora）：
    換一份設定就是換一個實體（ADR 0009），不會在同一輪裡動到別的。
    """
    from aistorage.admin.lock import GitHubAdmin, GitPinFiles, admin_lock_if_needed
    from aistorage.admin.remote import read_remote_manifest_sha256

    def _precheck() -> None:
        # 寬鬆版：首次初始化時 pin repo 還是空的，遠端也可能正是我們要重建的對象；
        # 這裡只擋「多個主 manifest」與「判不出有沒有被動過」這兩種狀態不明。
        read_remote_manifest_sha256(
            deps.drive, cfg.prefix_folder_id, cfg.repo_uuid, allow_missing=True)

    gh_repo = cfg.github_repository or ""
    if not gh_repo:
        # M4：不再預設 FATESAIKOU/MyAiStorage（見 admin/__main__._admin_lock）。
        raise RuntimeError(
            "設定檔缺少 github_repository：重建釘選值要停用的 workflow 在哪個 "
            "repo 上必須明確設定")
    pins = GitPinFiles(
        cfg.pin_repo_url, Path(tempfile.mkdtemp(prefix="init_pin_lock_")),
        key_path=cfg.pin_key_path)
    gh = GitHubAdmin(gh_repo)
    with admin_lock_if_needed(
        repo=cfg.repo, pins=pins, gh=gh, workflow=cfg.committer_workflow,
        reason="init-pin", precheck=_precheck,
    ):
        init_pin_cli(cfg, deps, confirm=True)


if __name__ == "__main__":
    sys.exit(main())

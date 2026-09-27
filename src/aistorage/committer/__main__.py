"""AiStorage 提交流程 CLI 命令列進入點。

用法：
  python -m aistorage.committer prescan [--config CONFIG]
  python -m aistorage.committer run [--config CONFIG] [--dry-run]
  python -m aistorage.committer plan-sweep [--config CONFIG]
  python -m aistorage.committer init-pin [--config CONFIG] [--dry-run | --confirm]

依據規格：docs/impl/group3-modules.md 第 7.2 節
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from aistorage.annex.git import SubprocessAnnexGit
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

    git_factory = lambda dest: SubprocessAnnexGit.clone_for_commit(
        cfg.repo_url,
        dest,
        max_git_bundles=cfg.max_git_bundles,
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

    args = parser.parse_args(argv)

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
        init_pin_cli(cfg, deps, confirm=args.confirm)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

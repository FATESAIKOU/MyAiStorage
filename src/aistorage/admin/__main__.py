"""管理操作 CLI（第 6 組，只在 Mac 上、以管理憑證執行）。

子命令：

- `lock-status`／`unlock`：看與手動解除維護旗標（`unlock --confirm`）。
- `create-repo`：建真本 repo 前綴＋git-annex 遠端（tasks 7.1；預設 dry-run）。
- `erase`：抹除。**預設 dry-run**，只列 id、計數與雜湊；
  `--confirm <plan-hash>` 才執行（計畫一變就拒絕）。
- `rollback`：列出可回滾的快照／把某 Session 回滾到舊快照。
  流程是 AdminLock → clone（暫存目錄）→ 回滾 → commit → push → 驗證 →
  重建 pin → 讀取視圖重建世代。
- `swap-finish`：管理操作中途中止後，用既有管理 clone 收尾（重推 → 驗證 →
  重建 pin → 讀取視圖重建世代）。
- `health`：健康檢查（自行 collect 資料後判定；`--data-json` 只給除錯用）。
- `recover`：復原計畫（唯讀：偵測遠端狀態並列步驟）。

erase／rollback 尚未在本機以外接上真實 Drive 憑證時，會明確報
`not_wired` 並指向 runbook；不會假裝成功。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from aistorage.admin import AdminError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aistorage.admin", description="AiStorage 管理操作（Mac、管理憑證）")
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock-status", help="查看維護旗標")
    lock.add_argument("--pin-repo", required=True, help="pin repo URL（file:// 可本機用）")
    lock.add_argument("--key", default=None, help="deploy key（非本機 URL 必填）")
    lock.add_argument("--repo", default="agora")
    lock.add_argument("--workdir", default=None)

    unlock = sub.add_parser("unlock", help="手動解除維護旗標（管理操作失敗後用）")
    unlock.add_argument("--pin-repo", required=True)
    unlock.add_argument("--key", default=None)
    unlock.add_argument("--repo", default="agora")
    unlock.add_argument("--workdir", default=None)
    unlock.add_argument("--gh-repo", default="")
    unlock.add_argument("--workflow", default=None)
    unlock.add_argument("--confirm", action="store_true",
                        help="確認已經處理完遠端與 pin 的不一致")

    erase = sub.add_parser("erase", help="抹除（預設 dry-run 列計畫）")
    erase.add_argument("--config", default="config/committer.json")
    erase.add_argument("--session", action="append", default=[])
    erase.add_argument("--segment", action="append", default=[],
                       help="SESSION_ID:MID[,MID...]（可重複）")
    erase.add_argument("--annex-key", action="append", default=[])
    erase.add_argument("--canary", required=True,
                       help="只存在於要抹除內容裡的字串；後置條件靠它確認")
    erase.add_argument("--why", default="")
    erase.add_argument("--confirm", default=None, help="計畫雜湊，確認執行")
    erase.add_argument("--workflow", default=None,
                       help="要停用的提交流程 workflow（預設取設定檔 committer_workflow）")

    rollback = sub.add_parser("rollback", help="回滾 Session 到舊快照")
    rollback.add_argument("--config", default="config/committer.json")
    rollback.add_argument("--repo-dir", default=None,
                          help="管理 clone（省略則在暫存目錄新建一個）")
    rollback.add_argument("--session", required=True)
    rollback.add_argument("--list", action="store_true", help="列出可回滾的快照")
    rollback.add_argument("--to", default=None, help="目標 snapshot_sha256")
    rollback.add_argument("--reason", default="")
    rollback.add_argument("--confirm", default=None, help="必須等於 --to")
    rollback.add_argument("--workflow", default=None,
                          help="要停用的提交流程 workflow（預設取設定檔 committer_workflow）")

    rv = sub.add_parser(
        "init-readview",
        help="初始化讀取視圖（建立 generation 0 的空 manifest、分享給 SA）")
    rv.add_argument("--folder-id", required=True, help="讀取視圖資料夾 id")
    rv.add_argument("--element", default="agora", choices=("agora", "foundry"),
                    help="讀取視圖屬於哪個要素（Foundry 用 foundry）")
    rv.add_argument("--config", default="config/committer.json",
                    help="提交流程設定檔（提供管理憑證的路徑）")
    rv.add_argument("--sa-email", default=None,
                    help="讀取用 service account 的 email；給了才會分享資料夾")
    init_mode = rv.add_mutually_exclusive_group()
    init_mode.add_argument("--dry-run", action="store_true",
                           help="只印計畫（預設）")
    init_mode.add_argument("--confirm", action="store_true",
                           help="確定建立（已存在會拒絕覆寫）")

    swap = sub.add_parser("swap-finish", help="中途中止後用管理 clone 收尾")
    swap.add_argument("--config", default="config/committer.json")
    swap.add_argument("--repo-dir", required=True)
    swap.add_argument("--force-push", action="store_true")
    swap.add_argument("--workflow", default=None,
                      help="要停用的提交流程 workflow（預設取設定檔 committer_workflow）")

    health = sub.add_parser("health", help="健康檢查（collect 資料後判定）")
    health.add_argument("--config", default="config/committer.json")
    health.add_argument("--data-json", default=None, help="除錯用：改用這份資料判定")
    health.add_argument("--notify", action="store_true",
                        help="fail 時 macOS 通知（只在 Mac）")
    health.add_argument("--plist", action="store_true",
                        help="印出 launchd plist 而不檢查")
    health.add_argument("--dump-data", default=None, help="把 collect 結果寫到這個檔")

    recover = sub.add_parser("recover", help="復原計畫（唯讀：偵測＋列步驟）")
    recover.add_argument("--config", default="config/committer.json")
    recover.add_argument("--check", action="store_true")
    recover.add_argument("--new-prefix", default=None)

    cr = sub.add_parser(
        "create-repo",
        help="建真本 repo 前綴＋git-annex 遠端（tasks 7.1；只建 repo，不做 pin 與讀取視圖）")
    cr.add_argument("--element", default="foundry", choices=("agora", "foundry"),
                    help="要建哪個要素的 repo（預設 foundry）")
    cr.add_argument("--prefix-name", required=True,
                    help="前綴資料夾名稱（3〜64 字元小寫英文／數字／連字號；rcloneprefix 用它）")
    cr.add_argument("--test-root-id", required=True,
                    help="測試資料夾 id（只在它底下建東西）")
    cr.add_argument("--rclone-conf", required=True,
                    help="rclone 設定檔路徑（只以路徑引用，不讀內容）")
    cr.add_argument("--rclone-remote", default="gdrive")
    cr.add_argument("--max-git-bundles", type=int, default=10)
    cr.add_argument("--work-parent", default=None,
                    help="git 工作區的父目錄（省略用暫存目錄；必須不在專案 repo 內）")
    create_mode = cr.add_mutually_exclusive_group()
    create_mode.add_argument("--dry-run", action="store_true",
                             help="只印計畫（預設）")
    create_mode.add_argument("--confirm", action="store_true",
                             help="真的建立（前綴已存在會拒絕）")
    return parser


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _not_wired(what: str, runbook: str) -> int:
    print(json.dumps(
        {"error": "not_wired",
         "message": f"{what} 需要管理 Drive 憑證（--config 的 rclone conf）與 "
                    f"GitHub 登入；目前只能透過程式 API 使用，見 {runbook}"},
        ensure_ascii=False), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lock-status":
            return _cmd_lock_status(args)
        if args.command == "unlock":
            return _cmd_unlock(args)
        if args.command == "erase":
            return _cmd_erase(args)
        if args.command == "rollback":
            return _cmd_rollback(args)
        if args.command == "init-readview":
            return _cmd_init_readview(args)
        if args.command == "swap-finish":
            return _cmd_swap_finish(args)
        if args.command == "health":
            return _cmd_health(args)
        if args.command == "recover":
            return _cmd_recover(args)
        if args.command == "create-repo":
            return _cmd_create_repo(args)
    except AdminError as e:
        payload: dict[str, Any] = {"error": "admin_error", "message": str(e)}
        report = getattr(e, "report", None)
        if report is not None:
            payload["swap"] = report.to_dict()
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return 1
    raise AssertionError(f"未知子命令: {args.command}")


def _pin_files(args: argparse.Namespace):
    from aistorage.admin.lock import GitPinFiles

    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="admin_pin_"))
    return GitPinFiles(args.pin_repo, workdir,
                       key_path=args.key if args.key else None)


def _cmd_lock_status(args: argparse.Namespace) -> int:
    from aistorage.admin.lock import read_maintenance

    flag = read_maintenance(_pin_files(args), args.repo)
    if flag is None:
        _emit_json({"maintenance": False})
    else:
        _emit_json({"maintenance": True, "reason": flag.reason,
                    "at": flag.at, "by": flag.by})
    return 0


def _cmd_unlock(args: argparse.Namespace) -> int:
    from aistorage.admin.lock import (
        DEFAULT_WORKFLOW, GitHubAdmin, maintenance_relpath,
    )

    if not args.confirm:
        raise AdminError(
            "解除維護旗標前要先確認遠端與 pin 已經一致；"
            "確定後加 --confirm（見 docs/runbooks/erase.md 的中止處理）")
    if not args.gh_repo:
        # M4：不再預設 FATESAIKOU/MyAiStorage——設定檔少了欄位時停用另一個
        # repo 的 workflow，比不做更糟。缺了就直接講。
        raise AdminError(
            "unlock 需要 --gh-repo <owner/repo>：要重新啟用的 workflow 在哪個 "
            "repo 上必須明確指定，不再預設")
    gh = GitHubAdmin(args.gh_repo)
    pins = _pin_files(args)
    workflow = args.workflow or DEFAULT_WORKFLOW
    raw = pins.read_text(maintenance_relpath(args.repo))
    if raw is not None:
        pins.delete(maintenance_relpath(args.repo),
                    "maintenance off (admin unlock --confirm)")
    gh.set_workflow_enabled(workflow, True)
    _emit_json({"unlocked": True, "repo": args.repo, "workflow": workflow,
                "had_flag": raw is not None})
    return 0


def _load_config(path: str):
    from aistorage.committer.config import CommitterConfig

    try:
        return CommitterConfig.load(path)
    except Exception as e:
        raise AdminError(f"設定檔 {path} 無法使用：{type(e).__name__}: {e}") from None


def _build_deps(cfg: Any, *, allow_production: bool = True):
    from aistorage.clock import SystemClock
    from aistorage.committer.publish import NullPublisher
    from aistorage.committer.run import Deps
    from aistorage.converters import CONVERTERS
    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient
    from aistorage.identity import Registry, load_registry
    from aistorage.integrity.pin import GitPinStore

    if not cfg.rclone_conf_path or not Path(cfg.rclone_conf_path).is_file():
        raise AdminError(
            f"找不到 rclone.conf（{cfg.rclone_conf_path}）："
            "管理操作需要專用管理憑證（AISTORAGE_RCLONE_CONF）")
    drive = HttpDriveClient(RcloneConfToken(cfg.rclone_conf_path))
    pins = GitPinStore(
        repo_url=cfg.pin_repo_url,
        workdir=Path(tempfile.mkdtemp(prefix="admin_pin_store_")),
        key_path=cfg.pin_key_path,
        known_hosts_path=cfg.pin_known_hosts_path,
        allow_production=allow_production,
    )
    # H1（review-25a48a9）：factory 收 target，URL 與 annex 規則都從 target 來
    # （admin 的每一條路徑都只操作 Agora，所以 target 就是 cfg 自己）。
    def git_factory(dest, target):
        return _annex_git(target, dest)
    try:
        registry = load_registry(cfg.identity_registry_path, allow_example=False)
    except Exception:
        registry = Registry({})
    return Deps(drive=drive, pins=pins, git_factory=git_factory, registry=registry,
                converters=CONVERTERS, publisher=NullPublisher(), clock=SystemClock())


def _annex_git(target: Any, dest: Path):
    from aistorage.annex.git import SubprocessAnnexGit

    return SubprocessAnnexGit.clone_for_commit(
        target.repo_url, dest,
        max_git_bundles=target.max_git_bundles,
        largefiles=target.largefiles_rule)


def _admin_deps(cfg: Any, deps: Any, drive: Any):
    from aistorage.admin import AdminDeps

    return AdminDeps(
        drive=drive, clock=deps.clock,
        workdir=Path(tempfile.mkdtemp(prefix="admin_work_")),
        repo=cfg.repo, repo_uuid=cfg.repo_uuid,
        prefix_folder_id=cfg.prefix_folder_id,
        quarantine_folder_id=cfg.quarantine_folder_id,
        readview_folder_id=cfg.readview_folder_id,
        repo_uuids=(cfg.repo_uuid,),
    )


def _manifest_precheck(cfg: Any, deps: Any, *, strict: bool) -> Any:
    """6.5 的預檢：重讀遠端主 manifest，確認它等於正式釘選值（設計 §5.1 步驟 4）。

    `strict=False` 只給 `swap-finish` 用：它的前提本來就是「遠端已經不一致」
    （前一次操作中途中止留下的狀態），所以不能要求遠端等於釘選值；仍然要擋的是
    「多個主 manifest」與「判不出有沒有被動過」這兩種狀態不明。
    """
    from aistorage.admin.remote import read_remote_manifest_sha256

    def _check() -> None:
        remote = read_remote_manifest_sha256(
            deps.drive, cfg.prefix_folder_id, cfg.repo_uuid, allow_missing=not strict)
        if not strict:
            return
        state, _pending = deps.pins.load(cfg.repo)
        if remote != state.manifest_sha256:
            raise AdminError(
                f"預檢失敗：遠端主 manifest 雜湊（{remote}）與正式釘選值"
                f"（{state.manifest_sha256}）不符；遠端在管理操作之外被動過，"
                "先查清楚再重來（不要解除維護旗標）")

    return _check


def _admin_lock(args: argparse.Namespace, cfg: Any, deps: Any, *, reason: str,
                strict_precheck: bool) -> Any:
    """6.5：會改動遠端真本的管理操作一律從這裡上鎖。

    停用的 workflow 名稱取自設定檔（`committer_workflow`），不再散落硬編碼——
    名字不對時 `gh workflow disable` 會直接失敗，整個管理操作就起不來。
    """
    from aistorage.admin.lock import AdminLock, GitHubAdmin, GitPinFiles

    pins = GitPinFiles(cfg.pin_repo_url,
                       Path(tempfile.mkdtemp(prefix="admin_pin_")),
                       key_path=cfg.pin_key_path)
    gh_repo = getattr(cfg, "github_repository", "") or ""
    if not gh_repo:
        # M4：不再預設 FATESAIKOU/MyAiStorage。名字不對時 `gh workflow disable`
        # 會停用別的 repo 的 workflow，所以缺了就直接拒絕，不要猜。
        raise AdminError(
            "設定檔缺少 github_repository：管理操作要停用的 workflow 在哪個 "
            "repo 上必須明確設定")
    gh = GitHubAdmin(gh_repo)
    workflow = getattr(args, "workflow", None) or cfg.committer_workflow
    return AdminLock(
        repo=cfg.repo, pins=pins, gh=gh, workflow=workflow, reason=reason,
        precheck=_manifest_precheck(cfg, deps, strict=strict_precheck))


def _cmd_erase(args: argparse.Namespace) -> int:
    from aistorage.admin.erase import (
        EraseTarget, apply_erase, plan_erase, plan_hash,
    )
    from aistorage.agora.store import AgoraStore, GitRawStorage
    from aistorage.committer.run import RepoTarget

    targets: list[EraseTarget] = []
    for sid in args.session:
        targets.append(EraseTarget(kind="session", session_id=sid))
    for spec in args.segment:
        sid, _, mids = spec.partition(":")
        targets.append(EraseTarget(kind="segment", session_id=sid,
                                   message_ids=tuple(m for m in mids.split(",") if m)))
    for key in args.annex_key:
        targets.append(EraseTarget(kind="annex_key", key=key))
    if not targets:
        raise AdminError("抹除必須指定目標（--session／--segment／--annex-key）")

    cfg = _load_config(args.config)
    try:
        deps = _build_deps(cfg)
    except AdminError:
        raise
    except Exception as e:
        return _not_wired(f"erase（{type(e).__name__}）", "docs/runbooks/erase.md")

    clone_dir = Path(tempfile.mkdtemp(prefix="admin_erase_")) / "repo"
    git = deps.git_factory(clone_dir, RepoTarget.from_config(cfg))
    store = AgoraStore(clone_dir, GitRawStorage(clone_dir), git=git,
                       temp_dir=Path(tempfile.mkdtemp(prefix="admin_erase_tmp_")))
    admin = _admin_deps(cfg, deps, deps.drive)

    plan = plan_erase(targets, admin=admin, store=store)
    ph = plan_hash(plan)
    _emit_json({"dry_run": True, "plan_hash": ph,
                "delete_file_ids": len(plan.delete_file_ids),
                "readview_file_ids": len(plan.readview_file_ids),
                "inbox_file_ids": len(plan.inbox_file_ids),
                "quarantine_file_ids": len(plan.quarantine_file_ids),
                "condemned_keys": len(plan.condemned_keys),
                "next": f"python -m aistorage.admin erase --confirm {ph} …"})
    if args.confirm != ph:
        return 0
    if not args.why.strip():
        raise AdminError("執行抹除必須說明原因（--why）")

    with _admin_lock(args, cfg, deps, reason="erase", strict_precheck=True):
        report = apply_erase(
            plan, confirm=ph, admin=admin, cfg=cfg, deps=deps, store=store,
            repo_dir=clone_dir, git=git, why=args.why, canary=args.canary,
            config_path=args.config)
    _emit_json({"dry_run": False, "erasure_record_id": report.erasure_record_id,
                "commit_sha": report.commit_sha,
                "deleted": len(report.deleted_file_ids),
                "verify": dict(report.verify),
                "rebuild_epoch": report.swap.rebuild_epoch if report.swap else None})
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    from aistorage.admin.remote import swap_remote
    from aistorage.admin.rollback import list_rollback_points, rollback_session
    from aistorage.agora.store import AgoraStore, GitRawStorage
    from aistorage.clock import SystemClock
    from aistorage.annex.git import SubprocessAnnexGit

    cfg = _load_config(args.config)

    clone_dir = Path(args.repo_dir) if args.repo_dir else (
        Path(tempfile.mkdtemp(prefix="admin_rollback_")) / "repo")
    if args.repo_dir is None:
        git = _annex_git(cfg, clone_dir)
    else:
        from aistorage.admin.remote import check_repo_dir

        # M4：既有目錄必須是 annex:: 遠端（clone 目的地才不要求）
        clone_dir = check_repo_dir(clone_dir, purpose="rollback 工作目錄")
        git = SubprocessAnnexGit(clone_dir, require_annex_remote=True)
    store = AgoraStore(clone_dir, GitRawStorage(clone_dir), git=git,
                       temp_dir=Path(tempfile.mkdtemp(prefix="admin_rollback_tmp_")))

    if args.list or not args.to:
        points = list_rollback_points(store, args.session)
        _emit_json([{"snapshot_sha256": p.snapshot_sha256,
                     "snapshot_at": p.snapshot_at, "via": p.via,
                     "is_current": p.is_current} for p in points])
        return 0
    if not args.reason.strip():
        raise AdminError("回滾必須說明原因（--reason）")
    if args.confirm != args.to:
        raise AdminError("執行需要 --confirm <目標 snapshot_sha256>（與 --to 一致）")

    try:
        deps = _build_deps(cfg)
    except Exception as e:
        return _not_wired(f"rollback（{type(e).__name__}）", "docs/runbooks/rollback.md")

    with _admin_lock(args, cfg, deps, reason="rollback", strict_precheck=True):
        result = rollback_session(
            store=store, session_id=args.session,
            target_snapshot_sha256=args.to, reason=args.reason,
            clock=SystemClock())
        swap = swap_remote(
            admin=_admin_deps(cfg, deps, deps.drive), cfg=cfg, deps=deps, git=git,
            repo_dir=clone_dir, delete_groups=(), force_push=False,
            config_path=args.config,
            resume_hint="回滾已 commit 在本機；遠端尚未一致，保留維護旗標")
    _emit_json({"session_id": result.session_id, "from": result.from_sha256,
                "to": result.to_sha256, "record_id": result.record_id,
                "was_running": result.was_running, "note": result.note,
                "rebuild_epoch": swap.rebuild_epoch})
    return 0


def _cmd_init_readview(args: argparse.Namespace) -> int:
    """讀取視圖初始化（部署手冊步驟 5）。"""
    from aistorage.admin.init_readview import InitReadviewPlan, init_readview

    try:
        cfg = _load_config(args.config)
    except AdminError as e:
        print(json.dumps({"error": "admin_error", "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        deps = _build_deps(cfg)
    except AdminError:
        raise
    except Exception as e:
        return _not_wired(
            f"init-readview（{type(e).__name__}）", "docs/runbooks/deploy.md 步驟 5")
    result = init_readview(
        deps.drive, args.folder_id, sa_email=args.sa_email,
        confirm=bool(args.confirm), clock=deps.clock,
        element=args.element)
    if isinstance(result, InitReadviewPlan):
        payload = result.to_dict()
        payload["dry_run"] = True
        payload["next"] = (
            f"python -m aistorage.admin init-readview --folder-id {args.folder_id}"
            + (f" --element {args.element}" if args.element != "agora" else "")
            + (f" --sa-email {args.sa_email}" if args.sa_email else "")
            + " --confirm")
        _emit_json(payload)
        return 1 if result.blocked else 0
    _emit_json({"dry_run": False, **result.to_dict(),
                "next": "把 manifest_file_id 填進 config/committer.json 的 "
                        "readview_manifest_file_id 與 reader.json 的 manifest_file_id"})
    return 0


def _cmd_swap_finish(args: argparse.Namespace) -> int:
    from aistorage.admin.remote import swap_remote

    cfg = _load_config(args.config)
    deps = _build_deps(cfg)
    from aistorage.annex.git import SubprocessAnnexGit

    # 6.5：swap-finish 也走同一把鎖。預檢是寬鬆版的——它的前提就是遠端已經不一致。
    with _admin_lock(args, cfg, deps, reason="swap-finish", strict_precheck=False):
        report = swap_remote(
            admin=_admin_deps(cfg, deps, deps.drive), cfg=cfg, deps=deps,
            # M4：swap-finish 操作的是**既有**管理 clone，必須是 annex:: 遠端
            git=SubprocessAnnexGit(Path(args.repo_dir), require_annex_remote=True),
            repo_dir=Path(args.repo_dir), delete_groups=(),
            force_push=args.force_push, config_path=args.config)
    _emit_json(report.to_dict())
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    import platform
    import subprocess
    from datetime import datetime, timezone

    if args.plist:
        from aistorage.admin.health import launchd_plist
        print(launchd_plist())
        return 0

    from aistorage.admin.health import HealthData, run_health, summarize

    now = datetime.now(timezone.utc)
    data: HealthData
    if args.data_json:
        try:
            data = HealthData(**json.loads(
                Path(args.data_json).read_text(encoding="utf-8")))
        except OSError as e:
            raise AdminError(f"讀不到健康資料檔: {args.data_json}: {e}") from None
        except ValueError as e:
            raise AdminError(f"健康資料檔不是合法 JSON: {e}") from None
    else:
        data = _collect_from_config(args.config)
        if args.dump_data:
            from aistorage.admin.health import dump_health_data

            _emit_json({"data_json": str(dump_health_data(data, Path(args.dump_data)))})

    checks = run_health(data, now=now)
    _emit_json([{"name": c.name, "status": c.status, "value": c.value,
                 "hint": c.hint} for c in checks])
    overall = summarize(checks)
    if overall == "fail":
        if args.notify and platform.system() == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 'display notification "AiStorage 健康檢查 fail" with title "AiStorage"'],
                capture_output=True, timeout=30)
        return 1
    return 0


def _collect_from_config(config_path: str):
    from aistorage.admin.health import CollectSources, collect_health
    from aistorage.clock import SystemClock
    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient
    from aistorage.integrity.pin import GitPinStore

    try:
        cfg = _load_config(config_path)
    except Exception as e:
        raise AdminError(
            f"健康檢查需要設定檔 {config_path}：{e}") from None
    drive = None
    if cfg.rclone_conf_path and Path(cfg.rclone_conf_path).is_file():
        drive = HttpDriveClient(RcloneConfToken(cfg.rclone_conf_path))
    pins = None
    if cfg.pin_repo_url:
        pins = GitPinStore(
            repo_url=cfg.pin_repo_url,
            workdir=Path(tempfile.mkdtemp(prefix="admin_health_pin_")),
            key_path=cfg.pin_key_path, known_hosts_path=cfg.pin_known_hosts_path,
            allow_production=True)
    if drive is None or pins is None:
        # 沒有憑證也要跑：全部欄位留 None → 判定為 warn（查不到 ≠ 正常）
        from aistorage.admin.health import HealthData

        return HealthData()
    return collect_health(
        CollectSources(drive=drive, pins=pins, repo=cfg.repo,
                       workflow=cfg.committer_workflow,
                       prefix_folder_id=cfg.prefix_folder_id,
                       readview_folder_id=cfg.readview_folder_id,
                       readview_manifest_file_id=cfg.readview_manifest_file_id,
                       quarantine_folder_id=cfg.quarantine_folder_id),
        clock=SystemClock())


def _cmd_create_repo(args: argparse.Namespace) -> int:
    """建 repo 前綴（tasks 7.1）。預設 dry-run，只印計畫；--confirm 才真的建。"""
    from pathlib import Path as _Path

    from aistorage.admin.create_repo import plan_create_repo, run_create_repo
    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient

    conf = _Path(args.rclone_conf)
    if not conf.is_file():
        raise AdminError(f"找不到 rclone 設定檔（只以路徑引用）: {conf}")
    drive = HttpDriveClient(RcloneConfToken(conf, remote=args.rclone_remote))
    plan = plan_create_repo(
        drive, args.test_root_id, args.prefix_name,
        element=args.element, max_git_bundles=args.max_git_bundles,
    )
    if not args.confirm:
        payload = plan.to_dict()
        payload["dry_run"] = True
        payload["next"] = (
            f"python -m aistorage.admin create-repo --element {args.element}"
            f" --prefix-name {args.prefix_name}"
            f" --test-root-id {args.test_root_id}"
            f" --rclone-conf {args.rclone_conf} --confirm"
        )
        _emit_json(payload)
        return 1 if plan.already_exists else 0
    created = run_create_repo(
        drive, args.test_root_id, args.prefix_name,
        element=args.element, rclone_conf=conf,
        rclone_remote=args.rclone_remote,
        max_git_bundles=args.max_git_bundles,
        work_parent=_Path(args.work_parent) if args.work_parent else None,
    )
    _emit_json({"dry_run": False, **created.to_dict(),
                "next": "接著跑 committer init-pin（釘選值）與 admin init-readview（讀取視圖）"})
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    from aistorage.admin.recover import plan_recover_from_drive

    cfg = _load_config(args.config)
    try:
        deps = _build_deps(cfg)
    except Exception as e:
        return _not_wired(f"recover（{type(e).__name__}）", "docs/runbooks/recovery.md")
    plan = plan_recover_from_drive(
        deps.drive, cfg.prefix_folder_id, cfg.repo_uuid,
        new_prefix=args.new_prefix)
    _emit_json({"mode": plan.mode, "steps": list(plan.steps), "detail": plan.detail})
    return 0


if __name__ == "__main__":
    sys.exit(main())

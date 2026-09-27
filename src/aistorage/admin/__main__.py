"""管理操作 CLI（第 6 組，只在 Mac 上、以管理憑證執行）。

所有會動到真本的指令都先 dry-run，只列 id、計數與雜湊；
`--confirm <plan-hash>` 才會執行（內容一變就拒絕）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from aistorage.admin import AdminDeps, AdminError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aistorage.admin", description="AiStorage 管理操作（Mac、管理憑證）")
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock-status", help="查看維護旗標")
    lock.add_argument("--pin-repo", required=True, help="pin repo URL（file:// 可本機用）")
    lock.add_argument("--key", default=None, help="deploy key（非本機 URL 必填）")
    lock.add_argument("--repo", default="agora")
    lock.add_argument("--workdir", default=None)

    erase = sub.add_parser("erase", help="抹除（先 dry-run 列計畫）")
    erase.add_argument("--session", action="append", default=[])
    erase.add_argument("--segment", action="append", default=[],
                       help="SESSION_ID:MID[,MID...]（可重複）")
    erase.add_argument("--annex-key", action="append", default=[])
    erase.add_argument("--why", default="")
    erase.add_argument("--confirm", default=None, help="計畫雜湊，確認執行")

    rollback = sub.add_parser("rollback", help="回滾 Session 到舊快照")
    rollback.add_argument("--repo-dir", required=True, help="本機 clone 的工作樹")
    rollback.add_argument("--session", required=True)
    rollback.add_argument("--list", action="store_true", help="列出可回滾的快照")
    rollback.add_argument("--to", default=None, help="目標 snapshot_sha256")
    rollback.add_argument("--reason", default="")
    rollback.add_argument("--confirm", default=None, help="必須等於 --to")

    health = sub.add_parser("health", help="健康檢查（讀 --data-json 判定）")
    health.add_argument("--data-json", default=None)
    health.add_argument("--notify", action="store_true",
                        help="fail 時 macOS 通知（只在 Mac）")
    health.add_argument("--plist", action="store_true",
                        help="印出 launchd plist 而不檢查")

    recover = sub.add_parser("recover", help="復原計畫（唯讀：偵測＋列步驟）")
    recover.add_argument("--check", action="store_true")
    recover.add_argument("--prefix", default=None)
    recover.add_argument("--uuid", default=None)
    recover.add_argument("--new-prefix", default=None)
    return parser


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lock-status":
            return _cmd_lock_status(args)
        if args.command == "erase":
            return _cmd_erase(args)
        if args.command == "rollback":
            return _cmd_rollback(args)
        if args.command == "health":
            return _cmd_health(args)
        if args.command == "recover":
            return _cmd_recover(args)
    except AdminError as e:
        print(json.dumps({"error": "admin_error", "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    raise AssertionError(f"未知子命令: {args.command}")


def _cmd_lock_status(args: argparse.Namespace) -> int:
    import tempfile
    from aistorage.admin.lock import GitPinFiles, read_maintenance
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="pin-"))
    pins = GitPinFiles(args.pin_repo, workdir,
                       key_path=args.key if args.key else None)
    flag = read_maintenance(pins, args.repo)
    if flag is None:
        _emit_json({"maintenance": False})
    else:
        _emit_json({"maintenance": True, "reason": flag.reason,
                    "at": flag.at, "by": flag.by})
    return 0


def _cmd_erase(args: argparse.Namespace) -> int:
    print(json.dumps(
        {"error": "not_wired",
         "message": "erase 需管理 Drive 連線（rclone conf）；先用程式 API 產生計畫，見 docs/runbooks/erase.md"},
        ensure_ascii=False), file=sys.stderr)
    return 2


def _cmd_rollback(args: argparse.Namespace) -> int:
    from aistorage.admin.rollback import list_rollback_points, rollback_session
    from aistorage.agora.store import AgoraStore, FakeRawStorage
    from aistorage.clock import SystemClock
    worktree = Path(args.repo_dir)
    if not (worktree / ".git").is_dir() and not (worktree / "sessions").is_dir():
        raise AdminError(f"不是可用的工作樹: {worktree}")
    import tempfile
    store = AgoraStore(worktree, FakeRawStorage(),
                       temp_dir=Path(tempfile.mkdtemp(prefix="admin_rollback_")))
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
    result = rollback_session(
        store=store, session_id=args.session, target_snapshot_sha256=args.to,
        reason=args.reason, clock=SystemClock())
    _emit_json({"session_id": result.session_id, "from": result.from_sha256,
                "to": result.to_sha256, "record_id": result.record_id,
                "note": "運作中的 Session 下次同步會以來源端內容成為新版本；永久移除請用抹除"})
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    import platform
    import subprocess
    if args.plist:
        from aistorage.admin.health import launchd_plist
        print(launchd_plist())
        return 0
    if not args.data_json:
        raise AdminError("health 需要 --data-json（或用 --plist 只印 plist）")
    from aistorage.admin.health import HealthData, run_health, summarize
    from datetime import datetime, timezone
    try:
        data = json.loads(Path(args.data_json).read_text(encoding="utf-8"))
    except OSError as e:
        raise AdminError(f"讀不到健康資料檔: {args.data_json}: {e}") from None
    except ValueError as e:
        raise AdminError(f"健康資料檔不是合法 JSON: {e}") from None
    checks = run_health(HealthData(**data), now=datetime.now(timezone.utc))
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


def _cmd_recover(args: argparse.Namespace) -> int:
    print(json.dumps(
        {"error": "not_wired",
         "message": "recover 需管理 Drive 連線；先用 plan_recover 產生計畫，見 docs/runbooks/recovery.md"},
        ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

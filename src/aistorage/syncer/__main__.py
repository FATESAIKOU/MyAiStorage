"""同步器 CLI（tasks 5.2／5.3）。

用法（容器內）：

    python -m aistorage.syncer opencode once                 # 跑一輪
    python -m aistorage.syncer opencode daemon [--interval 600]
    python -m aistorage.syncer opencode status               # 印出狀態摘要
    python -m aistorage.syncer sync-and-commit --session <id>... [--items <dir>] [--timeout 15m] [--json]

憑證全部**只以路徑引用**（`/secrets/…`、`/tmp/aistorage/rclone.conf`），log 只印 id 與代碼。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Sequence

from aistorage.clock import SystemClock
from aistorage.errors import AiStorageError
from aistorage.syncer.commit import SyncDeps, sync_and_commit
from aistorage.syncer.config import ConfigError, SyncerConfig
from aistorage.syncer.core import SyncOutcome, sync_once
from aistorage.syncer.opencode_api import OpencodeApi
from aistorage.syncer.state import SyncState


def _reader(cfg: SyncerConfig) -> Any:
    """建立讀取端（SA 身分；金鑰只以路徑）。"""
    from aistorage.drive.http import HttpDriveClient
    from aistorage.drive.sa_auth import ServiceAccountToken
    from aistorage.reader import AgoraReader
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig

    if not Path(cfg.sa_key).is_file():
        raise ConfigError(f"找不到讀取身分金鑰：{cfg.sa_key}")
    reader_cfg = ReaderConfig.load(cfg.reader_config, env={"AISTORAGE_SA_KEY": cfg.sa_key})
    drive = HttpDriveClient(ServiceAccountToken(cfg.sa_key))
    client = ReadViewClient(drive, reader_cfg, clock=SystemClock())
    return AgoraReader(client, clock=SystemClock())


def _drive(cfg: SyncerConfig) -> Any:
    """建立上傳用的 DriveClient（worker 的 drive.file 憑證）。"""
    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient

    if not Path(cfg.rclone_conf).is_file():
        raise ConfigError(f"找不到 rclone 設定：{cfg.rclone_conf}")
    return HttpDriveClient(RcloneConfToken(cfg.rclone_conf))


def _converter(source: str = "opencode") -> Any:
    from aistorage.converters import get_converter

    return get_converter(source)


def _deps(cfg: SyncerConfig) -> SyncDeps:
    return SyncDeps(
        api=OpencodeApi(cfg.base_url, directory=cfg.directory),
        reader=_reader(cfg),
        drive=_drive(cfg),
        inbox_folder_id=cfg.inbox_folder_id or "",
        signer=cfg.signer(),
        state=SyncState.load(cfg.state_path),
        clock=SystemClock(),
        workdir=Path("/tmp/aistorage/syncer"),
        converter=_converter(),
        pat_path=Path(cfg.gh_pat_path) if cfg.gh_pat_path else None,  # 5.3
        repo=cfg.repo,
        workflow=cfg.workflow,
    )


def _print_outcome(outcome: SyncOutcome, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(outcome.counts(), sort_keys=True, ensure_ascii=False))
        return
    print(
        "SYNC "
        + " ".join(f"{k}={v}" for k, v in sorted(outcome.counts().items()))
    )
    # 只印 id 與代碼，不印內容（D2 的 log 規則）
    for label, values in (
        ("uploaded", outcome.uploaded),
        ("reuploaded", outcome.reuploaded),
        ("waiting", outcome.waiting),
        ("resumed_after_stop", outcome.resumed_after_stop),
        ("too_large", outcome.too_large),
    ):
        for value in values:
            print(f"  {label}: {value}")
    for sid, code in outcome.rejected + outcome.errors:
        print(f"  rejected/error: {sid} {code}")


def cmd_once(args: argparse.Namespace) -> int:
    cfg = SyncerConfig.load()
    deps = _deps(cfg)
    outcome = sync_once(
        api=deps.api,
        reader=deps.reader,
        drive=deps.drive,
        inbox_folder_id=deps.inbox_folder_id,
        signer=deps.signer,
        state=deps.state,
        clock=deps.clock,
        workdir=deps.workdir,
        converter=deps.converter,
        only=args.session or None,
    )
    _print_outcome(outcome, as_json=args.json)
    return 0 if not outcome.errors else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    """每 interval 秒跑一輪；一輪失敗只記錄（id 與代碼）後繼續，不中止。"""
    cfg = SyncerConfig.load()
    deps = _deps(cfg)
    stopping = {"flag": False}

    def _stop(_signum: int, _frame: Any) -> None:
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # 先探一次 API：還沒起來就等，不要讓容器退出（entrypoint 才不會跟著死）
    while not deps.api.ping() and not stopping["flag"]:
        time.sleep(2)
    print(f"[syncer] daemon 開始，間隔 {args.interval or cfg.interval_s} 秒", flush=True)

    while not stopping["flag"]:
        deps.state = SyncState.load(cfg.state_path)
        try:
            outcome = sync_once(
                api=deps.api,
                reader=deps.reader,
                drive=deps.drive,
                inbox_folder_id=deps.inbox_folder_id,
                signer=deps.signer,
                state=deps.state,
                clock=deps.clock,
                workdir=deps.workdir,
                converter=deps.converter,
            )
            _print_outcome(outcome, as_json=False)
            # 停止中的 Session 又恢復 → 立刻同步並提交（D9）
            for sid in outcome.resumed_after_stop:
                print(f"[syncer] {sid} 停止後又被追加，立刻同步並提交", flush=True)
                result = sync_and_commit(session_ids=[sid], deps=deps)
                print(f"[syncer] {result.summary()}", flush=True)
        except (AiStorageError, ConfigError) as e:
            # 只印類型與訊息（訊息裡不得含秘密）
            print(f"[syncer] 這一輪失敗：{type(e).__name__}: {e}", flush=True)
        except Exception as e:  # pragma: no cover - 最後一線
            print(f"[syncer] 這一輪未預期的失敗：{type(e).__name__}", flush=True)

        waited = 0.0
        interval = float(args.interval or cfg.interval_s)
        while waited < interval and not stopping["flag"]:
            time.sleep(min(1.0, interval - waited))
            waited += 1.0
    print("[syncer] daemon 結束", flush=True)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # 診斷用的指令：缺收件匣 id 也要能看（見 SyncerConfig.load 的說明）
    cfg = SyncerConfig.load(require_inbox=False)
    state = SyncState.load(cfg.state_path)
    data = {
        "state_path": str(cfg.state_path),
        "sessions": len(state.sessions),
        "pending": state.pending(),
        "rejected": state.rejected(),
        "too_large": state.too_large(),
    }
    if args.json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print(f"狀態檔：{data['state_path']}（{data['sessions']} 個 Session）")
        print(f"  等待中：{len(data['pending'])}")
        for sid in data["pending"]:
            print(f"    {sid}")
        print(f"  被拒收（不自動重傳）：{len(data['rejected'])}")
        for sid in data["rejected"]:
            print(f"    {sid}")
        print(f"  過大未上傳：{len(data['too_large'])}")
        for sid in data["too_large"]:
            print(f"    {sid}")
    return 0


def _parse_timeout(value: str | None) -> Any:
    from datetime import timedelta

    if not value:
        return None
    text = value.strip().lower()
    if text.endswith("m"):
        return timedelta(minutes=float(text[:-1]))
    if text.endswith("s"):
        return timedelta(seconds=float(text[:-1]))
    if text.endswith("h"):
        return timedelta(hours=float(text[:-1]))
    return timedelta(seconds=float(text))


def cmd_sync_and_commit(args: argparse.Namespace) -> int:
    from datetime import timedelta

    cfg = SyncerConfig.load()
    deps = _deps(cfg)
    extra = _load_items(Path(args.items)) if args.items else []
    result = sync_and_commit(
        session_ids=list(args.session or []),
        extra_items=extra,
        deps=deps,
        timeout=_parse_timeout(args.timeout) or timedelta(minutes=15),
        progress=(lambda msg: print(msg, flush=True)) if not args.json else (lambda _m: None),
    )
    if args.json:
        print(json.dumps(
            {
                "visible": [f"{a.kind}:{a.target}" for a in result.visible],
                "rejected": [[f"{a.kind}:{a.target}", c] for a, c in result.rejected],
                "pending": [f"{a.kind}:{a.target}" for a in result.pending],
                "timed_out": result.timed_out,
                "elapsed_s": round(result.elapsed_s, 1),
                "trigger_error": result.trigger_error,
            },
            sort_keys=True, ensure_ascii=False,
        ))
    else:
        print(result.summary())
    return 0 if result.ok else 1


def _load_items(directory: Path) -> list[Any]:
    """從目錄讀入組好的 BuiltItem（5.4 的 skill 會寫在這裡）。

    檔名格式：`<item_key>.json`，內容是 `aistorage.inbox_builder.BuiltItem` 的
    sidecar 位元組（base64 的 raw 另存 `<item_key>.raw`）。
    """
    import base64

    from aistorage.inbox_builder import BuiltItem

    out: list[BuiltItem] = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        sidecar_bytes = base64.b64decode(raw["sidecar_b64"])
        raw_path = None
        if raw.get("raw_path"):
            raw_path = directory / raw["raw_path"]
        out.append(
            BuiltItem(
                item_key=raw["item_key"],
                item_id=raw["item_id"],
                sidecar_bytes=sidecar_bytes,
                sig=raw["sig"],
                raw_path=raw_path,
            )
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aistorage.syncer", description="AiStorage 同步器（住民容器內）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    oc = sub.add_parser("opencode", help="opencode 同步")
    oc_sub = oc.add_subparsers(dest="mode", required=True)

    once = oc_sub.add_parser("once", help="跑一輪")
    once.add_argument("--session", action="append", help="只同步指定的 session id（可重複）")
    once.add_argument("--json", action="store_true")
    once.set_defaults(func=cmd_once)

    daemon = oc_sub.add_parser("daemon", help="定期同步")
    daemon.add_argument("--interval", type=int, default=0, help="間隔秒數（預設 600）")
    daemon.set_defaults(func=cmd_daemon)

    status = oc_sub.add_parser("status", help="印出狀態摘要")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    sac = sub.add_parser("sync-and-commit", help="同步並提交（D9）")
    sac.add_argument("--session", action="append", help="要同步並提交的 session id（可重複）")
    sac.add_argument("--items", help="組好的項目目錄（交接單／認領／參考）")
    sac.add_argument("--timeout", help="等待時間，例如 15m")
    sac.add_argument("--json", action="store_true")
    sac.set_defaults(func=cmd_sync_and_commit)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except ConfigError as e:
        print(f"[syncer] 設定不足：{e}", file=sys.stderr)
        return 2
    except AiStorageError as e:
        print(f"[syncer] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

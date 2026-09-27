"""讀取介面 CLI（tasks 4.3）。

預設輸出 JSON（給 AI 的 skill 使用），一律帶有 freshness 區塊；
--text 則輸出人看的表格，警告醒目顯示。
exit code：成功 0（未達新鮮度仍然是 0）；AccessDenied 3；
ReadError 4；MismatchError／找不到 5。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from aistorage.clock import SystemClock
from aistorage.drive.http import HttpDriveClient
from aistorage.drive.model import DriveClient
from aistorage.drive.sa_auth import ServiceAccountToken
from aistorage.errors import MismatchError, ReadError
from aistorage.reader import AccessDenied, AgoraReader
from aistorage.reader.client import ReadViewClient
from aistorage.reader.config import ReaderConfig
from aistorage.search.index import normalize_time
from aistorage.search.query import Query

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """解析 10m、7d、30s、2h（或純秒數）為 timedelta。"""
    raw = text.strip()
    if not raw:
        raise ValueError("時間長度不可為空")
    if raw[-1].isalpha():
        unit = raw[-1].lower()
        if unit not in _DURATION_UNITS:
            raise ValueError(f"不支援的時間單位: {raw}")
        amount = float(raw[:-1])
        return timedelta(seconds=amount * _DURATION_UNITS[unit])
    return timedelta(seconds=float(raw))


def _format_ms(moment: datetime) -> str:
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def parse_since(text: str, now: datetime) -> str:
    """--since/--until：相對時間（7d）或 RFC3339，統一回傳毫秒形狀。"""
    raw = text.strip()
    try:
        return _format_ms(now - parse_duration(raw))
    except ValueError:
        pass
    normalized = normalize_time(raw)
    assert normalized is not None
    return normalized


def parse_cursor(text: str) -> tuple[str, str]:
    at, _, sid = text.partition(",")
    if not at or not sid:
        raise ValueError(f"cursor 格式須為 updated_at,session_id: {text}")
    return normalize_time(at), sid  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aistorage.reader", description="AiStorage 讀取介面")
    parser.add_argument("--config", default=None, help="讀者設定檔路徑")
    parser.add_argument("--text", action="store_true", help="輸出人看的表格（預設 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="找 Session")
    find.add_argument("--text-query", dest="text_query", default=None)
    find.add_argument("--title", default=None)
    find.add_argument("--case", default=None)
    find.add_argument("--source", default=None)
    find.add_argument("--status", default=None)
    find.add_argument("--since", default=None)
    find.add_argument("--until", default=None)
    find.add_argument("--main-only", action="store_true")
    find.add_argument("--limit", type=int, default=50)
    find.add_argument("--cursor", default=None)
    find.add_argument("--max-lag", default=None)

    show = sub.add_parser("show", help="讀單一 Session 的視圖")
    show.add_argument("session_id")
    show.add_argument("--max-lag", default=None)

    read = sub.add_parser("read", help="讀閱讀版")
    read.add_argument("session_id")
    read.add_argument("--snapshot", default=None)
    read.add_argument("--include-reverted", action="store_true")
    read.add_argument("--include-reasoning", action="store_true")
    read.add_argument("--max-lag", default=None)

    cont = sub.add_parser("continuation", help="讀交接單＋接續點之前的內容")
    cont.add_argument("handoff_id")

    handoffs = sub.add_parser("handoffs", help="列出交接單")
    handoffs.add_argument("--open", action="store_true")
    handoffs.add_argument("--case", default=None)

    rej = sub.add_parser("rejection", help="查拒收原因")
    rej.add_argument("item_key")

    catalog = sub.add_parser("catalog", help="同步器比對用目錄")
    catalog.add_argument("session_ids", nargs="+")

    wait = sub.add_parser("wait", help="等到快照可見（同步並提交用）")
    wait.add_argument("session_id")
    wait.add_argument("--raw-sha256", required=True)
    wait.add_argument("--timeout", default="10m")
    return parser


def _default_drive_factory(cfg: ReaderConfig) -> DriveClient:
    return HttpDriveClient(ServiceAccountToken(cfg.sa_key_path))


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _warn_line(freshness: Any) -> str:
    warning = freshness.get("warning") if isinstance(freshness, dict) else None
    return f" !!! {warning}" if warning else ""


def _emit_text(command: str, payload: dict) -> None:
    value = payload.get("value")
    freshness = payload.get("freshness", {})
    if command == "find":
        for found in value if isinstance(value, list) else []:
            hit = found.get("hit", {})
            session = hit.get("session", {})
            n = len(hit.get("matches", []))
            print(f"{session.get('session_id')}\t{session.get('status')}\t"
                  f"{session.get('snapshot_at')}\tmatches={n}\t{session.get('title')}")
        print(f"(generation={freshness.get('generation')}){_warn_line(freshness)}")
    elif command == "wait":
        print("visible" if payload.get("value") else "timeout")
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None, *,
         drive_factory: Callable[[ReaderConfig], DriveClient] | None = None,
         clock: Any = None) -> int:
    """CLI 進入點（drive_factory 供測試注入 FakeDrive）。回傳 exit code。"""
    args = build_parser().parse_args(argv)
    clock = clock or SystemClock()
    try:
        cfg = ReaderConfig.load(args.config)
        factory = drive_factory or _default_drive_factory
        reader = AgoraReader(ReadViewClient(factory(cfg), cfg, clock=clock), clock=clock)
        now = clock.now()
        if args.command == "find":
            q = Query(
                text=args.text_query,
                title_contains=args.title,
                case_id=args.case,
                source=args.source,
                status=args.status,
                parent_id="" if args.main_only else None,
                updated_after=parse_since(args.since, now) if args.since else None,
                updated_before=parse_since(args.until, now) if args.until else None,
                limit=args.limit,
                cursor=parse_cursor(args.cursor) if args.cursor else None,
            )
            max_lag = parse_duration(args.max_lag) if args.max_lag else None
            result = reader.find_sessions(q, max_lag=max_lag)
        elif args.command == "show":
            result = reader.get_session(
                args.session_id,
                max_lag=parse_duration(args.max_lag) if args.max_lag else None)
        elif args.command == "read":
            result = reader.get_reading(
                args.session_id, snapshot_sha256=args.snapshot,
                include_reverted=args.include_reverted,
                include_reasoning=args.include_reasoning,
                max_lag=parse_duration(args.max_lag) if args.max_lag else None)
        elif args.command == "continuation":
            result = reader.get_continuation(args.handoff_id)
        elif args.command == "handoffs":
            rows = reader.list_open_handoffs(case_id=args.case) if args.open else None
            if rows is None:
                raise ValueError("handoffs 目前只支援 --open（列出待認領）")
            result = rows
        elif args.command == "rejection":
            result = reader.get_rejection(args.item_key)
        elif args.command == "catalog":
            result = reader.catalog(args.session_ids)
        elif args.command == "wait":
            result = reader.wait_for_snapshot(
                args.session_id, args.raw_sha256,
                timeout=parse_duration(args.timeout))
        else:
            raise ValueError(f"未知子命令: {args.command}")
    except AccessDenied as e:
        print(json.dumps({"error": "access_denied", "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 3
    except ReadError as e:
        print(json.dumps({"error": "read_error", "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 4
    except (MismatchError, KeyError, ValueError, FileNotFoundError) as e:
        print(json.dumps({"error": "data_error", "message": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 5
    payload = dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result
    if args.text:
        _emit_text(args.command, payload)
    else:
        _emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

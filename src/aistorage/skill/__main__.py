"""住民工具的 CLI（tasks 5.4）。

plugin 只把 `context.sessionID` 傳進來，其餘都是這裡決定：

    python -m aistorage.skill whoami     --session <id>
    python -m aistorage.skill split       --session <id> --parts <file.json>
    python -m aistorage.skill handoff-end --session <id> --summary <text> [--next-steps <text>]
    python -m aistorage.skill claim       --session <id> --handoff <id> [--handoff <id>…]
    python -m aistorage.skill find        --query <text> [--case <id>] [--max-lag 5m]
    python -m aistorage.skill read        --session <id> [--max-lag 5m]
    python -m aistorage.skill reference   --session <id> --to <session_id> [--read-snapshot-at <ts>]
    python -m aistorage.skill list-handoffs [--case <id>]
    python -m aistorage.skill stop        --session <id>

輸出是 JSON（給 plugin 回給模型）。錯誤走 stderr 與非零 exit code，
訊息裡只有 id 與代碼，不含秘密。
"""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from aistorage.clock import SystemClock
from aistorage.errors import AiStorageError
from aistorage.reader.client import AccessDenied
from aistorage.syncer.__main__ import _converter, _deps, _reader
from aistorage.syncer.config import ConfigError, SyncerConfig
from aistorage.syncer.state import SyncState
from aistorage.skill import tools
from aistorage.skill.tools import MainSessionRequired, RejectedItems, SkillDeps, SkillError


def _lag(value: str | None) -> timedelta | None:
    if not value:
        return None
    text = value.strip().lower()
    for suffix, unit in (("s", "seconds"), ("m", "minutes"), ("h", "hours")):
        if text.endswith(suffix):
            return timedelta(**{unit: float(text[:-1])})
    return timedelta(seconds=float(text))


def _sd() -> SkillDeps:
    cfg = SyncerConfig.load()
    deps = _deps(cfg)
    return SkillDeps(deps=deps, state=deps.state)


def _emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_whoami(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.whoami(sd.api, args.session))


def cmd_split(args: argparse.Namespace, sd: SkillDeps) -> int:
    parts = json.loads(Path(args.parts).read_text(encoding="utf-8"))
    if isinstance(parts, dict):
        parts = [parts]
    if not isinstance(parts, list) or not parts:
        raise SkillError("--parts 要是一個非空的清單")
    return _emit(tools.split(sd, args.session, parts, timeout=_lag(args.timeout)))


def cmd_handoff_end(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.handoff_end(
        sd, args.session, args.summary, next_steps=args.next_steps,
        timeout=_lag(args.timeout),
    ))


def cmd_claim(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.claim(
        sd, args.session, args.handoff or [], timeout=_lag(args.timeout)))


def cmd_find(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.find(
        sd.reader, args.query, max_lag=_lag(args.max_lag),
        case_id=args.case, limit=args.limit,
    ))


def cmd_read(args: argparse.Namespace, sd: SkillDeps) -> int:
    target = args.target or args.session
    return _emit(tools.read(sd.reader, target, max_lag=_lag(args.max_lag)))


def cmd_reference(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.reference(
        sd, args.session, args.to, read_snapshot_at=args.read_snapshot_at))


def cmd_list_handoffs(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.list_handoffs(sd, case_id=args.case))


def cmd_stop(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.stop(sd, args.session, timeout=_lag(args.timeout)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aistorage.skill", description="AiStorage 住民工具"
    )
    parser.add_argument("--json", action="store_true", help="保留（輸出本來就是 JSON）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("whoami", help="目前的 Session 是誰")
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("split", help="分裂：同步自己 ＋ 各寫一張交接單，一起提交")
    p.add_argument("--session", required=True)
    p.add_argument("--parts", required=True, help="[{title, summary, next_steps}] 的 JSON 檔")
    p.add_argument("--timeout")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("handoff-end", help="交出末端（只寫一張交接單）")
    p.add_argument("--session", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--next-steps")
    p.add_argument("--timeout")
    p.set_defaults(func=cmd_handoff_end)

    p = sub.add_parser("claim", help="認領交接單（多張＝統合）")
    p.add_argument("--session", required=True)
    p.add_argument("--handoff", action="append", help="可重複")
    p.add_argument("--timeout")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("find", help="找 Session（一律附上新鮮度）")
    p.add_argument("--query", required=True)
    p.add_argument("--case")
    p.add_argument("--max-lag", dest="max_lag")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("read", help="讀一個 Session（一律附上新鮮度）")
    p.add_argument("--session", required=True, help="目前的 Session id（plugin 傳入）")
    p.add_argument("--target", help="要讀的 Session；預設就是自己")
    p.add_argument("--max-lag", dest="max_lag")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("reference", help="留下參考 Link（PM 決定 4：只上傳不提交）")
    p.add_argument("--session", required=True)
    p.add_argument("--to", required=True, help="被參考的 Session id")
    p.add_argument("--read-snapshot-at", dest="read_snapshot_at")
    p.set_defaults(func=cmd_reference)

    p = sub.add_parser("list-handoffs", help="列出待認領的交接單")
    p.add_argument("--case")
    p.set_defaults(func=cmd_list_handoffs)

    p = sub.add_parser("stop", help="宣告停止（主 Session 限定）")
    p.add_argument("--session", required=True)
    p.add_argument("--timeout")
    p.set_defaults(func=cmd_stop)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        sd = _sd()
        return int(args.func(args, sd))
    except RejectedItems as e:
        # 認領／交接被拒收 → 明確要求停下（spec 4.2）
        print(f"[skill] 必須停下：{e}", file=sys.stderr)
        return 3
    except MainSessionRequired as e:
        print(f"[skill] {e}", file=sys.stderr)
        return 4
    except AccessDenied as e:
        # 讀取視圖還沒發佈，或 SA 身分沒有讀取權：講清楚是「讀不到」，
        # 不要丟 traceback 給 AI（plugin 會把 stderr 原樣回給模型）。
        print(
            f"[skill] 讀不到 Agora 的讀取視圖：{e}\n"
            "[skill] 這是 fail-closed：不能確認內容有沒有進去，就不會寫任何東西。"
            "請先讓提交流程發佈讀取視圖（6.3 健康檢查）。",
            file=sys.stderr,
        )
        return 5
    except (SkillError, ConfigError) as e:
        print(f"[skill] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except AiStorageError as e:
        print(f"[skill] {type(e).__name__}: {e}", file=sys.stderr)
        return 6


if __name__ == "__main__":
    sys.exit(main())

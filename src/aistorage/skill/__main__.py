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
    python -m aistorage.skill register-artifact --session <id> --kind link|contained --name <檔名>
        [--content-type <mime>] [--link <url>] [--repo <repo> --path <path>]
        [--file <本體檔>] [--description <說明>] [--case <id>]
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


def cmd_register_artifact(args: argparse.Namespace, sd: SkillDeps) -> int:
    return _emit(tools.register_artifact(
        sd, args.session,
        kind=args.kind,
        name=args.name,
        content_type=args.content_type,
        link=args.link,
        repo=args.repo,
        path=args.path,
        file_path=args.file,
        description=args.description,
        case_id=args.case,
    ))


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
    # plugin 對**每一個**工具都會帶 `--session`（Session id 由 context 帶入，
    # 不在參數 schema 裡）。所以連用不到它的唯讀指令也要收下這個參數，
    # 否則 argparse 會以「unrecognized arguments」讓整個工具失敗
    # （9.1 e2e 實測：`aistorage_find`／`aistorage_list_handoffs` 在容器裡
    #  100% 失敗，AI 因此拿不到任何清單）。
    p.add_argument("--session", help="目前的 Session id（plugin 傳入；本指令用不到）")
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
    p.add_argument("--read-snapshot-at", dest="read_snapshot_at",
                   help="必填：剛剛 aistorage_read 讀到的 snapshot_at")
    p.set_defaults(func=cmd_reference)

    p = sub.add_parser("list-handoffs", help="列出待認領的交接單")
    p.add_argument("--session", help="目前的 Session id（plugin 傳入；本指令用不到）")
    p.add_argument("--case")
    p.set_defaults(func=cmd_list_handoffs)

    p = sub.add_parser("register-artifact", help="登錄產出到 Foundry 產出目錄（只上傳）")
    p.add_argument("--session", required=True, help="目前的 Session id（plugin 傳入）")
    p.add_argument("--kind", required=True, choices=("link", "contained"))
    p.add_argument("--name", required=True, help="產出的檔名（顯示用，也是物件的安全檔名來源）")
    p.add_argument("--content-type", dest="content_type")
    p.add_argument("--link", help="link 型：對外連結")
    p.add_argument("--repo", help="link 型：原處 repo（與 --path 搭配）")
    p.add_argument("--path", help="link 型：原處路徑（與 --repo 搭配）")
    p.add_argument("--file", help="contained 型：容器內的本體檔路徑（上限 100 MiB）")
    p.add_argument("--description")
    p.add_argument("--case")
    p.set_defaults(func=cmd_register_artifact)

    p = sub.add_parser("stop", help="宣告停止（主 Session 限定）")
    p.add_argument("--session", required=True)
    p.add_argument("--timeout")
    p.set_defaults(func=cmd_stop)

    return parser


def _log_traceback(exc: BaseException) -> None:
    """把完整堆疊寫到容器裡的錯誤日誌（模型看不到這份）。

    plugin 會把這個行程的 stderr 收走再回給模型，所以堆疊不能走 stderr；
    寫在 state 目錄下（`/tmp/aistorage/skill-errors.log`），要查就
    `docker exec <容器> cat /tmp/aistorage/skill-errors.log`。
    """
    import os
    import traceback
    from pathlib import Path

    state_dir = Path(os.environ.get("AISTORAGE_STATE_DIR") or "/tmp/aistorage")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / "skill-errors.log").open("a", encoding="utf-8") as fh:
            fh.write(f"=== {type(exc).__name__}: {exc}\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
    except OSError:
        # 連日誌都寫不了就罷了：stderr 上已經有人話訊息
        pass


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
    except Exception as e:  # noqa: BLE001 - CLI 的最後一道：不要把 traceback 丟給模型
        # plugin 會把 stderr **原樣**回給模型。免費模型看到 Python traceback
        # 只會照著 stack 一行行重試（9.1 e2e 實測：S2 因為一個 FileNotFoundError
        # 連續呼叫同一個工具、整場卡了 30 分鐘）。所以這裡只給模型一句人話，
        # 完整堆疊寫進容器裡的錯誤日誌（人要看得到，模型看不到），並明確說
        # 「這是系統問題，不是你參數寫錯」。
        print(f"[skill] 內部錯誤（{type(e).__name__}）：{e}", file=sys.stderr)
        print(
            "[skill] 這不是你的參數問題，是 AiStorage 這邊的錯誤；"
            "請把現況原樣回報給使用者，不要重試同一個呼叫。",
            file=sys.stderr,
        )
        _log_traceback(e)
        return 7


if __name__ == "__main__":
    sys.exit(main())

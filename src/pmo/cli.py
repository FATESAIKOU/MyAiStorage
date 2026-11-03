"""The `pmo` command's front door (design §3.2, T-2's `inbox` half).

Arguments come from the environment only: `PMO_SKILL_CONFIG` names the teams
settings, `PMO_SKILL_STATE_DIR` the state directory (§3.4; a missing one is a
refusal, never a guessed path). Exit codes are the table's: 0 the round ran
(including「部分失敗、已印 [warn]」), 1 用法錯（argparse's replies included），
2 設定或狀態檔壞, or a sending command was run under another identity. The read
round itself is `pmo.round.read_round` (tick shares it); its state files are
pre-checked before the cursor moves, so a damaged one never costs a message,
and a send that went out before a state failure says so instead of passing for
「送不出去」(`SentButNotRecorded`).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import config as config_module
from . import decision, inbox, ledger, paths, sanitize
from .adapters import Angareion
from .errors import (
    IdentityMismatch,
    MissingEnvError,
    PMOError,
    SchemaError,
    SentButNotRecorded,
    UsageError,
)
from .ports import PortError
from .round import read_round
from .runtime import Ctx, Deps, assert_pmo_identity
from .timeutil import iso_utc

__all__ = ["build_parser", "main"]


class _Parser(argparse.ArgumentParser):
    """An argparse parser whose errors are the design's exit-1 usage errors.

    argparse answers bad usage with SystemExit(2); the design's table calls an
    argument problem exit 1, so `error()` raises `UsageError`, which `main`
    turns into the 1. A well-formed `--help` keeps its SystemExit(0) (mapped
    in `main` below).
    """

    def error(self, message: str):  # noqa: A003 (argparse's own name)
        raise UsageError(f"用法錯誤：{message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="pmo", description="PMO 工具（#110；design §3.2）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp_inbox = sub.add_parser("inbox", help="PMO 收件匣：read / list / ack / report / decision-send")
    inbox_sub = sp_inbox.add_subparsers(dest="inbox_cmd", required=True)

    inbox_sub.add_parser("read", help="拉取新信、解析、去重並回覆 ack（不比對 agents、不跑看門狗）")

    sp_il = inbox_sub.add_parser("list", help="唯讀檢視收件匣狀態")
    sp_il.add_argument("--json", action="store_true", help="以 JSON 印出收件項目")

    sp_ia = inbox_sub.add_parser("ack", help="針對特定工作補發 ack")
    sp_ia.add_argument("ulid", help="工作的 26 碼 ULID")
    sp_ia.add_argument("--issue", default=None, help="相關工單號（預設沿用既有工單號）")
    sp_ia.add_argument(
        "--schedule",
        default=None,
        choices=list(inbox.VALID_SCHEDULES),
        help="預估安排（預設沿用既有安排）",
    )

    sp_irp = inbox_sub.add_parser("report", help="發送工作回報給秘書")
    sp_irp.add_argument("ulid", help="工作的 26 碼 ULID")
    sp_irp.add_argument("--status", required=True, help="工作狀態（例如：已完成、進行中、阻塞…）")
    sp_irp.add_argument("--issue", default=None, help="相關工單連結或編號")
    sp_irp.add_argument("--pr", default=None, help="相關 PR 連結")
    sp_irp.add_argument("--body", default=None, help="回報內文（'-' 代表由 stdin 讀入）")

    sp_ids = inbox_sub.add_parser("decision-send", help="送出一則決定題（urgency 8，走 Angareion）")
    sp_ids.add_argument("--body", default=None, help="JSON（text＋questions）；-＝由 stdin 讀入")

    return parser


# --------------------------------------------------------------------------- #
# 指令：inbox（輸入、事件行與結束碼照 design §3.2）
# --------------------------------------------------------------------------- #


def cmd_inbox(ctx: Ctx, args: argparse.Namespace) -> int:
    subcmd = getattr(args, "inbox_cmd", None)
    if subcmd == "read":
        return cmd_inbox_read(ctx)
    if subcmd == "list":
        return cmd_inbox_list(ctx, args)
    if subcmd == "ack":
        return cmd_inbox_ack(ctx, args)
    if subcmd == "report":
        return cmd_inbox_report(ctx, args)
    if subcmd == "decision-send":
        return cmd_inbox_decision_send(ctx, args)
    raise UsageError(f"未知的 inbox 子指令：{subcmd}")


def cmd_inbox_read(ctx: Ctx) -> int:
    """One inbox round (design §3.2: 「只做 tick 的收信一輪」).

    The round itself is `pmo.round.read_round` (tick will call the same
    function, arch review 中-2); this command only prints its events and maps
    the two failures the design names onto exit codes:

    - a damaged state file raises before the read → `SchemaError`, exit 2
      (this round did nothing, the cursor did not move);
    - `angareion read` itself failing → `[warn]` on stderr, exit 0
      (§3.2's「部分失敗」; §2.3: 不推游標、下一輪再讀).
    """
    try:
        events = read_round(ctx)
    except PortError as error:
        ctx.log(f"[warn] angareion read：{inbox.one_line_for_event(error)}")
        return 0

    if not events:
        ctx.emit("[inbox] 沒有新信件")
    else:
        for event in events:
            ctx.emit(event)
    return 0


def _items_by_creation(st: dict) -> list[dict]:
    """The ledger's items, oldest first — the one order the text and JSON share.

    `received_at` is the back end's time, so it sorts the same way the text
    output always did; items without one keep their place at the front.
    """
    return sorted(
        st.get("items", {}).values(),
        key=lambda item: (str(item.get("received_at") or ""), str(item.get("id") or "")),
    )


def cmd_inbox_list(ctx: Ctx, args: argparse.Namespace) -> int:
    st = ledger.load_state(ctx.state_dir)
    items = _items_by_creation(st)
    if getattr(args, "json", False):
        # An array of the item objects themselves, oldest first (arch review:
        # the design gives no shape, and this output is a public one — the
        # mapping keyed by ULID would be changed after merge at a cost).
        ctx.emit(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        ctx.emit("[inbox] 收件匣是空的（0 筆工作）")
        return 0
    ctx.emit(f"[inbox] 秘書工作共 {len(items)} 筆：")
    for item in items:
        ulid = str(item.get("id") or "")
        title = item.get("title", "")
        level = item.get("level", "b")
        recv = item.get("received_at", "")
        issue = item.get("issue") or "待排程"
        status = item.get("status", "scheduled")
        team = item.get("team") or "未指派"
        tag = "NEW" if status == "scheduled" else status
        status_text = inbox.STATUS_DISPLAY_MAP.get(status, status)
        ctx.emit(f"- [{tag}] {ulid} | {title} | {level} 級 | {recv}")
        ctx.emit(f"  工單: {issue} | 狀態: {status_text} | 建議團隊: {team}")
    return 0


def cmd_inbox_ack(ctx: Ctx, args: argparse.Namespace) -> int:
    """手動補發 ack。

    送出與寫帳的先後照現行工具（先送），但寫帳失敗**不再**默默用 exit 2 冒充
    「送不出去」：`SentButNotRecorded` 帶著訊息 id，由 `main` 印成「已送出、帳本
    沒更新，不要重送」（Sonnet L-1）。
    """
    assert_pmo_identity(ctx.config)
    ulid = str(args.ulid).strip()
    if not inbox.ULID_RE.match(ulid):
        raise UsageError(f"工作 ID 不是合法的 26 碼 ULID：{ulid}")
    with ledger.state_lock(ctx.state_dir):
        st = ledger.load_state(ctx.state_dir)
        if ulid not in st.get("items", {}):
            raise UsageError(f"收件匣中找不到工作 ID：{ulid}")

        item = st["items"][ulid]
        # 低 6：手動 ack 預設沿用既有 issue 與既有 schedule
        issue = args.issue if args.issue is not None else item.get("issue")
        schedule = (
            args.schedule if args.schedule is not None else item.get("schedule", "排入待開工佇列")
        )

        try:
            res = inbox.send_ack(
                ctx, ulid, issue=issue, schedule=schedule,
                channel=inbox.item_channel(item, ctx.config),
            )
        except PortError as error:
            ctx.log(f"[warn] 發送 ack 失敗：{inbox.one_line_for_event(error)}")
            return 2
        item["ack_at"] = iso_utc(ctx.deps.now())
        if issue is not None:
            item["issue"] = issue
        if schedule is not None:
            item["schedule"] = schedule
        try:
            ledger.save_state(ctx.state_dir, st, now=ctx.deps.now())
        except SchemaError as error:
            raise SentButNotRecorded(
                f"ack 已送出（id={res}），但帳本寫不進去：{inbox.one_line_for_event(error)}。"
                f"不要重送，先修 state 檔再跑一次 `pmo inbox list`",
                message_id=res,
            ) from error
        ctx.emit(f"[ack] 已發送 ack：收到工作 {ulid} id={res}")
    return 0


def cmd_inbox_report(ctx: Ctx, args: argparse.Namespace) -> int:
    assert_pmo_identity(ctx.config)
    ulid = str(args.ulid).strip()
    if not inbox.ULID_RE.match(ulid):
        raise UsageError(f"工作 ID 不是合法的 26 碼 ULID：{ulid}")

    body_text = ""
    if args.body == "-":
        body_text = ctx.inp.read()
    elif args.body is not None:
        body_text = args.body

    try:
        res = inbox.send_report(
            ctx,
            ulid=ulid,
            status=args.status,
            issue=args.issue,
            pr=args.pr,
            body_text=body_text,
        )
    except PortError as error:
        ctx.log(f"[warn] 發送工作回報失敗：{inbox.one_line_for_event(error)}")
        return 2
    ctx.emit(f"[report] 已向秘書發送工作回報：{ulid}（{args.status}） id={res}")
    return 0


def cmd_inbox_decision_send(ctx: Ctx, args: argparse.Namespace) -> int:
    assert_pmo_identity(ctx.config)
    if not args.body:
        raise UsageError("要給 --body -（用 stdin）或 --body <JSON>")
    raw = ctx.inp.read() if args.body == "-" else args.body
    if not str(raw).strip():
        raise UsageError("--body 是空的")
    try:
        body = decision.build_decision_send_body(raw)
    except ValueError as error:
        raise UsageError(str(error)) from error
    try:
        res = decision.send_decision(ctx, body)
    except PortError as error:
        ctx.log(f"[warn] 發送決定題失敗：{inbox.one_line_for_event(error)}")
        return 2
    ctx.emit(f"[decision] 已送出決定題 id={res}")
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


_HANDLERS = {"inbox": cmd_inbox}


def main(
    argv: Optional[list] = None,
    deps: Deps | None = None,
    stdout=None,
    stderr=None,
    stdin=None,
) -> int:
    """`pmo` 的入口；測試可以塞假的 deps 與三個 stream（同現行工具的作法）。"""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    inp = stdin if stdin is not None else sys.stdin

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as e:
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 1
    except SystemExit as e:  # e.g. --help
        return 0 if e.code in (0, None) else 1

    try:
        config_path = paths.get_config_path()
        state_dir = paths.get_state_dir()
    except PMOError as e:
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 2
    try:
        cfg = config_module.load(config_path)
    except (MissingEnvError, SchemaError) as e:
        print(sanitize.sanitize(f"[pmo] 設定讀不起來：{e}"), file=err, flush=True)
        return 2

    deps = deps if deps is not None else Deps(angareion=Angareion())
    ctx = Ctx(deps=deps, config=cfg, state_dir=state_dir, out=out, err=err, inp=inp)
    try:
        return _HANDLERS[args.cmd](ctx, args)
    except UsageError as e:
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 1
    except IdentityMismatch as e:
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 2
    except SentButNotRecorded as e:
        # 已送出、帳本沒更新：exit 2 會讓呼叫端以為沒送出去，字樣一定要說清楚
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 2
    except (SchemaError, PortError) as e:
        print(sanitize.sanitize(f"[pmo] {e}"), file=err, flush=True)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""The `pmo` command's front door (design §3.2; T-2's `inbox` half, T-3's
rebuild, T-4a's `check`／`send`, T-4b's `open`, T-4c's
`reply`／`poke`／`status`／`close`).

Arguments come from the environment only: `PMO_SKILL_CONFIG` names the teams
settings, `PMO_SKILL_STATE_DIR` the state directory (§3.4; a missing one is a
refusal, never a guessed path). Exit codes are the table's: 0 the round ran
(including「部分失敗、已印 [warn]」), 1 用法錯（argparse's replies included），
2 設定或狀態檔壞, or a sending command was run under another identity. The read
round itself is `pmo.round.read_round` (tick shares it); its state files are
pre-checked before the cursor moves, so a damaged one never costs a message,
and a send that went out before a state failure says so instead of passing for
「送不出去」(`SentButNotRecorded`). `check`／`send` live in `pmo.dispatch`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Optional

from . import config as config_module
from . import decision, dispatch, inbox, ledger, ops, paths, rebuild, sanitize, tick
from . import open as open_module
from ._lock import LockTimeout
from .adapters import Aicomm, Angareion, GitHub, Sidecar
from .errors import (
    IdentityMismatch,
    MissingEnvError,
    PMOError,
    SchemaError,
    SentButNotRecorded,
    UsageError,
)
from .ports import AicommTimeout, JobRecord, PortError, SidecarError, SidecarShapeError
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

    sp_irb = inbox_sub.add_parser("rebuild", help="從 angareion log 重建收件帳本（帳本以 Angareion 為準，本機是快取）")
    sp_irb.add_argument("--since", default=None, help="只重建這個時刻（含）之後的信（遞給後端的 log）")
    sp_irb.add_argument("--dry-run", action="store_true", help="只算差異，不寫帳本")

    sp_check = sub.add_parser("check", help="派工前的本機預檢：逐項印 OK／FAIL（有 FAIL 回 1）")
    sp_check.add_argument("team", help="team 的 key（<專案>-<team 代號>）")

    sp_send = sub.add_parser("send", help="把工單與指示送到 team 的 channel，並寫 team 狀態")
    sp_send.add_argument("team", help="team 的 key（<專案>-<team 代號>）")
    sp_send.add_argument("ticket", help="工單，寫成 ‹owner/repo›#‹n›")
    sp_send.add_argument("--body", default=None, help="指示內文（'-' 代表由 stdin 讀入）")

    sp_open = sub.add_parser("open", help="在 team 的 sidecar 上開 team PM（第一則輸入帶派工的 --since）")
    sp_open.add_argument("team", help="team 的 key（<專案>-<team 代號>）")
    sp_open.add_argument("--session", default=None, help="接回既有的 session（agora:…）")
    sp_open.add_argument("--fresh", action="store_true", help="開一個新的 session")
    sp_open.add_argument("--provider", default=None, help="provider（旗標優先；可以單獨給）")
    sp_open.add_argument("--model", default=None, help="model（要與 --provider 一起給）")

    sp_reply = sub.add_parser("reply", help="回話給 team PM（經 aicomm：寫入＋叫醒）")
    sp_reply.add_argument("team", help="team 的 key（<專案>-<team 代號>）")
    sp_reply.add_argument("--urgency", type=int, default=None, help="urgent 程度 0-9（同 angareion）")
    sp_reply.add_argument("--body", default=None, help="回覆內文（'-' 代表由 stdin 讀入）")

    sp_poke = sub.add_parser("poke", help="戳 team PM 的 agent 一下（sidecar 的 poke）")
    sp_poke.add_argument("team", help="team 的 key（<專案>-<team 代號>）")
    sp_poke.add_argument("--interrupt", action="store_true", help="打斷它正在做的那一輪")
    sp_poke.add_argument("--body", default=None, help="要它看的一句話（'-' 代表由 stdin 讀入）")

    sp_status = sub.add_parser("status", help="team PM 的 state／exit／session，以及 PMO 自己排的 job")
    sp_status.add_argument("team", nargs="?", default=None, help="只查這個 team（不給＝全部）")
    sp_status.add_argument("--json", action="store_true", help="以 JSON 印出")

    sp_close = sub.add_parser("close", help="照 Close SOP 關掉 team 的 agent（先刪自動重開 job，其他 job 只列不動）")
    sp_close.add_argument("team", help="team 的 key（<專案>-<team 代號>）")
    sp_close.add_argument("--timeout", type=float, default=ops.CLOSE_TIMEOUT_SECONDS,
                          help="等 exited 的秒數（預設 60）")

    sub.add_parser("tick", help="跑一輪 PMO 的監看（收信、比對 agents、看門狗、worker 交接）")

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
    if subcmd == "rebuild":
        return cmd_inbox_rebuild(ctx, args)
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


def _cite(ids: list[str]) -> str:
    """id 清單的一行字；空的顯示「無」。"""
    return "、".join(ids) if ids else "無"


def cmd_inbox_rebuild(ctx: Ctx, args: argparse.Namespace) -> int:
    """由 angareion log 重建收件帳本（design §3.2 的 rebuild 列；#110 T-3）。

    結束碼照設計的表：0 重建跑完（含 dry-run）；1 angareion 自己的用法錯
    （exit 1：旗面拼錯一類，修呼叫端）；2 其他外部失敗——log 讀不到、輸出不合
    格式、後端失敗（其餘 `PortError`）或帳本壞、寫不進去（`SchemaError`），
    帳本維持原樣。stdout 是差異（重建幾筆、加／換了哪些、team 為空而重建不出
    來的清單、`notified.poke` 保留的說明），身分守門不在這裡：重建不寄信。
    """
    try:
        report = rebuild.rebuild(ctx, since=args.since, dry_run=args.dry_run)
    except PortError as error:
        ctx.log(f"[rebuild] 重建失敗：{inbox.one_line_for_event(error)}")
        return 1 if error.returncode == 1 else 2
    ctx.emit(
        f"[rebuild] 重建 {len(report.added) + len(report.updated)} 筆"
        f"（新增 {len(report.added)}、更新 {len(report.updated)}），"
        f"叫人去重帳共 {report.call_notified} 筆（既有 ∪ 重建）"
    )
    ctx.emit(f"[rebuild] 新增：{_cite(sorted(report.added))}")
    ctx.emit(f"[rebuild] 更新：{_cite(sorted(report.updated))}")
    ctx.emit(
        f"[rebuild] team：{len(report.team_blank)} 筆重建不出來，已留空並列出："
        f"{_cite(sorted(report.team_blank))}"
    )
    ctx.emit(
        f"[rebuild] notified.poke：{report.poke_kept} 筆保留（急件去重只有本機有、"
        f"log 重建不出來；照原樣留著，不會多叫醒）"
    )
    if args.dry_run:
        ctx.emit("[rebuild] [dry-run] 只算差異，帳本未寫")
    return 0


# --------------------------------------------------------------------------- #
# 指令：check／send（派工；design §3.2、§5.2 T-4a）
# --------------------------------------------------------------------------- #


def cmd_check(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo check ‹team›`：逐項 OK／FAIL；有 FAIL 就是 1（design §3.2）。

    Worker team 的那一行是 `--  ‹項目›：‹說明›`（資訊列，不算 FAIL）。
    """
    items = dispatch.check(ctx, args.team)
    for item in items:
        if item.note:
            ctx.emit(f"--  {item.name}：{item.detail}")
        else:
            ctx.emit(f"{'OK' if item.ok else 'FAIL'} {item.name}：{item.detail}")
    return 0 if all(item.ok for item in items) else 1


def _body_from(ctx: Ctx, value: Optional[str]) -> str:
    """`--body -` reads stdin; a missing or empty body is a usage error."""
    if value is None:
        raise UsageError("要給 --body -（用 stdin）或 --body <文字>")
    text = ctx.inp.read() if value == "-" else value
    if not str(text).strip():
        raise UsageError("--body 是空的")
    return str(text)


def cmd_send(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo send ‹team› ‹repo#n› --body -`（design §2.1、§3.2）。

    結束碼照表：工單讀不到（GitHub 埠失敗）是 1；Angareion 送不出去是 2；
    身分不符由 `assert_pmo_identity` 擋成 2（送信前的守門，送之前就拒）。
    已送出但後端時刻讀不回或 team 狀態寫不進去時是 `SentButNotRecorded`
    （exit 2），字樣明說「不要直接重送」。`[wake-hint]` 只是提示，不改任何
    東西（D-7）。
    """
    assert_pmo_identity(ctx.config)
    team = ctx.config.team(args.team)
    if team is None:
        raise UsageError(f"未知的 team：{args.team}")
    if team.retired:
        raise UsageError(f"team {team.key} 已退役（retired=true），不派工")
    repo, number = dispatch.parse_ticket(args.ticket)
    body = _body_from(ctx, args.body)
    # D-12 的邊界：指示信會離開本機，憑證樣式的內容不送出去。判斷只用
    # `pmo.sanitize` 的 TOKEN_SHAPES／secret-field 規則（#355 裁定 5）；錯誤
    # 訊息只說哪一段，不回顯值。
    if sanitize.has_credential_shape(body):
        raise UsageError("正文含憑證樣式，拒送（請先移除再送）")
    if ctx.deps.github is None:
        raise UsageError("GitHub 埠沒有接上（pmo send 需要讀工單）")

    try:
        issue = ctx.deps.github.issue_view(repo, number)
    except PortError as error:
        ctx.log(f"[warn] 工單讀不到：{inbox.one_line_for_event(error)}")
        return 1
    if sanitize.has_credential_shape(issue.title):
        # 標題也原樣進信（`- 標題：…` 與 `標題：…` 兩處），來源與本文相同，
        # 所以同一道牆也要掃；訊息指出是「工單標題」那一段。
        raise UsageError("工單標題含憑證樣式，拒送；請先改工單")
    if sanitize.has_credential_shape(issue.body):
        # 工單本文也會原樣進到信裡（公開 GitHub 上也可能有誤貼的憑證），
        # 所以要指出是「工單本文」那一段：PMO 改正文沒有用。
        raise UsageError("工單本文含憑證樣式，拒送；請先改工單")

    try:
        result = dispatch.send(
            ctx,
            team=team,
            repo=repo,
            number=number,
            issue=issue,
            instructions=body,
        )
    except PortError as error:
        ctx.log(f"[warn] 指示信送不出去：{inbox.one_line_for_event(error)}")
        return 2

    ctx.emit(f"[send] 指示信已送出 id={result.message_id} 後端時刻={result.received_at}")
    if result.wake_hint is not None:
        ctx.emit(f"[wake-hint] 看門狗 10m 後到期，目前叫醒 {result.wake_hint}")
    return 0


# --------------------------------------------------------------------------- #
# 指令：open（派工的第二半；design §3.2、§5.2 T-4b）
# --------------------------------------------------------------------------- #


def _log_sidecar_failure(ctx: Ctx, error: SidecarError) -> None:
    """Print what is known about a sidecar refusal or an unreachable sidecar.

    §2.3：400 印錯誤碼、不重試；409 印錯誤碼交 PMO 判斷；連不上印
    `[要本人處理]`。All are exit 2. The text is the adapter's known-field
    string (`SidecarError.detail` is only ever the code), never the raw
    response body (D-12).
    """
    if error.status is None:
        ctx.log(f"[要本人處理] sidecar 不可用：{inbox.one_line_for_event(error)}")
    elif error.status == 400:
        ctx.log(
            f"[warn] sidecar 參數錯（400）：{inbox.one_line_for_event(error)}；不重試"
        )
    elif error.status == 409:
        ctx.log(f"[warn] sidecar 拒絕（409）：{inbox.one_line_for_event(error)}")
    else:
        ctx.log(
            f"[warn] sidecar 回應 {error.status}：{inbox.one_line_for_event(error)}"
        )


def cmd_open(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo open ‹team› [--session …] [--fresh] [--provider P --model M]`（§3.2）。

    結束碼照表：0 印 agent 的 state 與 attach（互動版印 sidecar 給的
    `attach.local` 原樣；非互動版印「（非互動版，沒有 attach）」）；1 用法或狀態
    不足（沒先 `pmo send`、team 已退役、worker team、設定沒有 workdir、只有
    `--model`、互動版帶 `--session`、非互動版帶 `--fresh`）；2 sidecar 拒絕或
    連不上（只印已知欄位，D-12）。agent 已開但 team 狀態寫不進去時仍印 state 與
    attach（它已經起來了），再回 2 並說「已開、沒記錄」，不要讓人重開。
    """
    team = ops.team_or_usage(ctx, args.team)
    try:
        result = open_module.open_agent(
            ctx,
            team=team,
            session=args.session,
            fresh=args.fresh,
            provider=args.provider,
            model=args.model,
        )
    except SidecarShapeError as error:
        # `POST /agents` answered 2xx but in a shape this port cannot read: the
        # agent may already be open, so this is not「sidecar 不可用」and must not
        # invite a straight reopen (the same worry as `state_error`).
        ctx.log(
            "[warn] sidecar 回應形狀不符；POST 若是 2xx 則 agent 可能已開，"
            "請先 `pmo status ‹team›` 確認，不要直接重開："
            f"{inbox.one_line_for_event(error)}"
        )
        return 2
    except SidecarError as error:
        _log_sidecar_failure(ctx, error)
        return 2
    except PortError as error:
        # Got an answer, but not one the port can read: still a sidecar
        # problem (not a retry-worthy「sidecar 不可用」).
        ctx.log(f"[warn] sidecar 回應不合預期：{inbox.one_line_for_event(error)}")
        return 2

    ctx.emit(
        f"[open] team={result.team_key} id={result.agent_id} "
        f"state={result.state} attach={result.attach or '（非互動版，沒有 attach）'}"
    )
    if result.state_error is not None:
        ctx.log(
            f"[warn] agent 已開（id={result.agent_id}），team 狀態寫不進去："
            f"{result.state_error}"
        )
        return 2
    return 0


# --------------------------------------------------------------------------- #
# 指令：reply／poke／status／close（design §3.2、§5.2 T-4c）
# --------------------------------------------------------------------------- #


def cmd_reply(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo reply ‹team› [--urgency N] --body -`（design §3.2）。

    會寄信，所以先過身分守門（送之前就拒）。結束碼照表：0 印訊息 id；2 沒寫
    進去。aicomm 逾時是特例：信可能已經寫進去了，字樣明說「送出結果不明，不
    要直接重送」，不印 id（不知道就算了，不編）。retired team 是 1：回話會
    叫醒人，退役的 team 不再叫（#357 裁定 (d)）。
    """
    assert_pmo_identity(ctx.config)
    team = ops.team_or_usage(ctx, args.team, refuses_retired=True)
    if args.urgency is not None and not 0 <= args.urgency <= 9:
        raise UsageError("--urgency 要在 0-9 之間")
    body = _body_from(ctx, args.body)
    try:
        message_id = ops.reply(ctx, team=team, body=body, urgency=args.urgency)
    except AicommTimeout as error:
        ctx.log(
            f"[warn] 送出結果不明，不要直接重送：{inbox.one_line_for_event(error)}"
        )
        return 2
    except PortError as error:
        ctx.log(f"[warn] 回覆送不出去：{inbox.one_line_for_event(error)}")
        return 2
    ctx.emit(f"[reply] 訊息已送出 id={message_id}")
    return 0


def cmd_poke(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo poke ‹team› [--interrupt] --body -`（design §3.2）：印 sidecar 的回應碼。

    poke 不寄信，所以不過身分守門（design §3.2 的守門只列會寄信的指令）。
    0 印 `[poke] ‹pm› ‹回應碼›`（sidecar 回 204 就印 204）；sidecar 拒絕或
    連不上是 2，印它的狀態與錯誤碼（只印已知欄位，D-12）。retired team 是 1：
    poke 會叫醒人，退役的 team 不再叫（#357 裁定 (d)）。
    """
    team = ops.team_or_usage(ctx, args.team, refuses_retired=True)
    text = _body_from(ctx, args.body)
    try:
        code = ops.poke(ctx, team=team, text=text, interrupt=args.interrupt)
    except SidecarError as error:
        ctx.log(f"[warn] 戳「{team.pm}」沒成功：{inbox.one_line_for_event(error)}")
        return 2
    ctx.emit(f"[poke] {team.pm} {code}")
    return 0


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo status [‹team›] [--json]`（design §3.2）：唯讀。

    逐 team 印 state／exit／session，再印 PMO 自己排的 job；PMO 不在 sidecar
    時不查 job（§3.4）。指名一個 team 時，另外列出瞄準這個 team PM 的 job
    （id、action、every、句子是不是固定句），**只列不動**：這是裁定 (d) 末段
    的建議，讓 PMO 自己決定要不要請排的人刪。讀不到 sidecar 是 2（其他 team
    照印，§2.3 的部分失敗）。
    """
    report = ops.status(ctx, args.team)
    if args.json:
        ctx.emit(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for team in report.teams:
            line = (
                f"[status] {team.key} pm={team.pm} state={team.state} "
                f"exit={team.exit_reason or '-'} session={team.session or '-'} "
                f"attach={team.attach or '-'}"
            )
            if team.note:
                line += f"（{team.note}）"
            ctx.emit(line)
        if report.jobs_note is not None:
            ctx.emit(f"[status] {report.jobs_note}")
        elif not report.jobs:
            ctx.emit("[status] 沒有 PMO 自己排的 job")
        else:
            for job in report.jobs:
                ctx.emit(
                    f"[status] job {job.job_id} {_every_of(job)} "
                    f"last_run={job.last_run if job.last_run is not None else '-'} "
                    f"last_error={job.last_error or '-'}"
                )
        if report.team_jobs:
            ctx.emit(
                f"[status] 瞄準 {report.teams[0].pm} 的 job（只列不動，不會刪）："
            )
            for job in report.team_jobs:
                ctx.emit(f"[status]   {_job_fields(job, ctx.config.wake_text)}")
    for failure in report.failures:
        ctx.log(f"[warn] {failure}")
    return 2 if report.failures else 0


def _every_of(job: JobRecord) -> str:
    """`every=600s`, or `at=<時刻>` for a one-off job (`every` is `None`, #370 高-2)."""
    if job.every is None:
        return f"at={job.at or '-'}"
    return f"every={job.every:g}s"


def _job_fields(job: JobRecord, wake_text: str) -> str:
    """One listed job: id, action, every (or at), and whether its sentence is the fixed one."""
    fixed = "是" if ops.has_fixed_sentence(job, wake_text) else "否"
    return f"{job.job_id} action={job.action} {_every_of(job)} 固定句={fixed}"


def cmd_close(ctx: Ctx, args: argparse.Namespace) -> int:
    """`pmo close ‹team› [--timeout 秒]`（design §3.2；Close SOP 見 `pmo.ops.close`）。

    0 回在「關掉了、或 close 還在背景進行、而且要刪的自動重開 job 都處理了」：
    印是否已關、刪掉（或本來就不在）的自動重開 job，以及只列不動的「瞄準這個
    agent 的非 auto job」。等不到 exited、或自動重開 job 刪不掉是 2（字樣分開
    寫，不把「已關」講成沒關）；`GET /jobs` 本身失敗只是 `[warn]`，不改結束碼
    （#357 裁定 (d)）。retired team 照常可關。
    """
    team = ops.team_or_usage(ctx, args.team)
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise UsageError("--timeout 要是 0 或正數")
    try:
        result = ops.close(ctx, team=team, timeout=args.timeout)
    except SidecarError as error:
        ctx.log(f"[warn] 關不掉「{team.pm}」：{inbox.one_line_for_event(error)}")
        return 2

    if result.already_gone:
        ctx.emit(f"[close] {result.pm} 本來就不在 sidecar 上（已關）")
    elif result.closed:
        ctx.emit(f"[close] {result.pm} 已關（state={result.state}）")
    elif result.close_in_progress:
        ctx.emit(
            f"[close] {result.pm} 的 close 已送出，還在背景進行（state={result.state}）；"
            f"用 pmo status 確認"
        )
    else:
        ctx.log(
            f"[close] {result.pm} 等不到 exited（state={result.state}）；"
            f"沒有重送 close（Close SOP）"
        )
    for job_id in result.deleted_jobs:
        ctx.emit(f"[close] 刪掉自動重開 job：{job_id}")
    for job_id in result.missing_jobs:
        ctx.emit(f"[close] 自動重開 job {job_id} 已經不在（404）")
    if (
        not result.deleted_jobs
        and not result.missing_jobs
        and result.jobs_warn is None
        and result.job_error is None
    ):
        # 讀不到或清不掉時不印這行：它會和 stderr 的「讀不到／清不掉」互相矛盾。
        ctx.emit("[close] 沒有要刪的自動重開 job")
    if result.other_jobs:
        ctx.emit(f"[close] 瞄準 {result.pm} 的非 auto job（只列不動，要停請找排的人刪）：")
        for job in result.other_jobs:
            ctx.emit(f"[close]   {_job_fields(job, ctx.config.wake_text)}")
    if result.jobs_warn is not None:
        ctx.log(
            f"[warn] job 讀不到（{result.jobs_warn}），照樣 close："
            f"close 之後它到點可能又把 agent 開回來"
        )
    if result.job_error is not None:
        ctx.log(
            f"[warn] job 清不掉（{result.job_error}）："
            f"close 之後它到點可能又把 agent 開回來"
        )
    ok = (result.closed or result.close_in_progress) and result.job_error is None
    return 0 if ok else 2


# --------------------------------------------------------------------------- #
# 指令：tick（一輪；design §2.2、§3.2.1、§3.4；#358 T5-2）
# --------------------------------------------------------------------------- #


def cmd_tick(ctx: Ctx, _args: argparse.Namespace | None = None) -> int:
    """`pmo tick`：跑一輪並把設計表的失敗對到結束碼（§3.2、§3.4）。

    0＝這一輪跑完（含部分失敗；步驟內部的 `PortError` 已印 `[warn]`，裁定
    S-2）；1 由 argparse 回；2＝壞檔（`[要本人處理]`，什麼都沒做）或
    `tick.json` 的鎖等不到（其他步驟已做完，只有 tick.json 沒寫）。
    """
    try:
        tick.tick(ctx)
    except LockTimeout:
        ctx.log(
            "[warn] tick.json 的鎖等不到（10 秒），這一輪的 tick.json 沒寫，下一輪再寫"
        )
        return 2
    except SchemaError as error:
        ctx.log(f"[要本人處理] {inbox.one_line_for_event(error)}")
        return 2
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


_HANDLERS = {
    "inbox": cmd_inbox,
    "check": cmd_check,
    "send": cmd_send,
    "open": cmd_open,
    "reply": cmd_reply,
    "poke": cmd_poke,
    "status": cmd_status,
    "close": cmd_close,
    "tick": cmd_tick,
}


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

    deps = deps if deps is not None else Deps(
        angareion=Angareion(),
        aicomm=Aicomm(),
        # The sidecar port is looked up per address (#356, design §3.1): a
        # team's operations use the team's `sidecar`, the PMO's own wake jobs
        # use `pmo_sidecar`. `adapters.Sidecar` is the factory.
        sidecar_for=Sidecar,
        github=GitHub(),
    )
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

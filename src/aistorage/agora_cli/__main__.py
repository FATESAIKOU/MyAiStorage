"""`agora`：Agora 的單一指令入口。

依據 `docs/design/agora-session-operations.md`（2026-09-28 本人確認）：

    agora find [關鍵字] [--case <案件>] [--waiting]      # 找 session
    agora show <session>                                 # 看內容與前後關係
    agora read <session>                                 # 讀最新已提交＋記參考
    agora handoff <session> [--at <訊息>] --task "…" …    # 每個 --task 一張交接單
    agora checkout <起點>… [--task "…"] -o <目錄>        # 產出起點包

**這裡不 import 任何 coding agent 專屬的東西**。載入起點包成原生 session 是
轉接器（`agora-opencode load`）的事；Agora 只管讀、寫交接單、產出起點包。

輸出預設是 JSON（AI 的工具要解析）；`--text` 給人看的表格。
exit code：成功 0；讀不到 3／4；資料或起點有問題 2；被拒收（認領失敗）6。
"""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
from typing import Any, Callable, Sequence

from aistorage.agora_cli.checkout import (
    CheckoutDeps,
    CheckoutError,
    ClaimRejected,
    checkout,
)
from aistorage.agora_cli.package import (
    DEFAULT_MAX_CONTEXT_CHARS,
    ContextLimitExceeded,
    ContextPackageError,
)
from aistorage.agora_cli.startpoint import StartPointError
from aistorage.clock import SystemClock
from aistorage.errors import AiStorageError, MismatchError, ReadError
from aistorage.reader import AccessDenied
from aistorage.search.query import Query
from aistorage.syncer.commit import sync_and_commit


def _lag(value: str | None) -> timedelta | None:
    """把 `5m`／`2h`／`90` 解析成 timedelta（與 skill CLI 的做法一致）。"""
    if not value:
        return None
    text = value.strip().lower()
    for suffix, unit in (("s", "seconds"), ("m", "minutes"), ("h", "hours")):
        if text.endswith(suffix):
            return timedelta(**{unit: float(text[:-1])})
    return timedelta(seconds=float(text))


def _emit(payload: Any) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def _reader_settings(args: argparse.Namespace) -> tuple[Any, Any, Any]:
    """建立讀取端：回傳 `(AgoraReader, ReaderConfig, DriveClient)`。

    **讀取身分（SA）**是唯讀的：金鑰只以路徑引用。`DriveClient` 除了讀取視圖，
    `agora checkout` 還會用它去 Agora 的物件資料夾取原始紀錄——那個資料夾對讀取
    身分只有唯讀權限。

    有 worker 的設定（`SyncerConfig`）時沿用它（容器裡是同一份設定檔），否則退回
    讀者設定檔——讀取不需要寫入身分，所以 `agora find/show/read` 在只有讀取身分的
    機器上也能跑。
    """
    from aistorage.drive.http import HttpDriveClient
    from aistorage.drive.sa_auth import ServiceAccountToken
    from aistorage.reader import AgoraReader
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig

    cfg: ReaderConfig | None = None
    try:
        from aistorage.syncer.config import SyncerConfig

        syncer_cfg = SyncerConfig.load()
        cfg = ReaderConfig.load(
            syncer_cfg.reader_config, env={"AISTORAGE_SA_KEY": syncer_cfg.sa_key})
    except Exception:
        cfg = ReaderConfig.load(getattr(args, "reader_config", None))

    clock = SystemClock()
    drive = HttpDriveClient(ServiceAccountToken(cfg.sa_key_path))
    reader = AgoraReader(ReadViewClient(drive, cfg, clock=clock), clock=clock)
    return reader, cfg, drive


def _writer_deps(args: argparse.Namespace) -> tuple[CheckoutDeps, Any]:
    """建立寫入端（簽章金鑰、收件匣、觸發提交流程、本機認領記錄）。沒有就明說。

    `commit_claim` **不上傳任何來源應用的 session**——新 session 這時還不存在
    於任何來源應用裡，它的第一份（空的）快照是**認領單自己帶的預留**。所以這裡
    只把認領放進收件匣、觸發提交流程、然後等認領在讀取介面裡變成 Link。
    """
    from aistorage.agora_cli.claims import ClaimJournal
    from aistorage.syncer.__main__ import _deps
    from aistorage.syncer.config import SyncerConfig

    deps = _deps(SyncerConfig.load())

    def commit(claims: list[Any], *, timeout: timedelta) -> Any:
        return sync_and_commit(
            session_ids=(), extra_items=list(claims), deps=deps,
            timeout=timeout, progress=lambda _m: None,
        )

    return (
        CheckoutDeps(
            reader=None, clock=deps.clock, signer=deps.signer,
            inbox_folder_id=deps.inbox_folder_id, drive=deps.drive,
            commit_claim=commit,
            journal=ClaimJournal(getattr(args, "claims_path", None)),
        ),
        deps,
    )


def _object_fetcher(cfg: Any, drive: Any) -> Any:
    """建 `ObjectFetcher`（唯讀身分 ＋ Agora 物件資料夾）。

    讀取視圖不發佈 raw 的位元組，所以 `checkout` 要自己依 annex key 去取並用
    key 內嵌的 sha256 驗證（見 `agora_cli/objects.py`）。
    """
    from aistorage.agora_cli.objects import ObjectFetcher

    if not cfg.agora_folder_id:
        return None
    return ObjectFetcher(drive, cfg.agora_folder_id)


def _main_session_id(_deps_obj: Any) -> str:
    """本機 opencode 的主 Session id（認領的 claimer 必須是主 Session）。

    取不到就明確報錯——`apply_claim` 會擋子 Session，所以寧可現在就說清楚。
    """
    from aistorage.skill.tools import SkillError

    sessions = [s for s in _deps_obj.api.list_sessions() if s.is_main]
    if not sessions:
        raise SkillError(
            "本機 opencode 沒有主 Session（可能沒在跑、或全部都是子 Session）；"
            "接續要在主 Session 進行"
        )
    return sessions[0].id


# ---------------------------------------------------------------------------
# find / show / read
# ---------------------------------------------------------------------------


def cmd_find(args: argparse.Namespace, reader: Any) -> int:
    """`agora find`：找 Session（`--waiting` 列出等人接的交接單）。"""
    if args.waiting:
        result = reader.list_open_handoffs(case_id=args.case)
        value = getattr(result, "value", result)
        return _emit({
            "handoffs": [
                {
                    "handoff_id": getattr(h, "handoff_id", None),
                    "target_session_id": getattr(h, "target_session_id", None),
                    "producer": getattr(h, "producer", None),
                    "author_session_id": getattr(h, "author_session_id", None),
                    "case_id": getattr(h, "case_id", None),
                    "updated_at": getattr(h, "updated_at", None),
                    "body": getattr(h, "body_json", ""),
                }
                for h in value
            ],
            "freshness": _freshness(getattr(result, "freshness", None)),
        })
    result = reader.find_sessions(
        Query(text=(args.query or None) or None, case_id=args.case,
              limit=args.limit, status=args.status),
        max_lag=_lag(args.max_lag),
    )
    value = getattr(result, "value", result)
    overall = getattr(result, "freshness", None)
    hits = []
    for found in value:
        hit = getattr(found, "hit", found)
        row = getattr(hit, "session", hit)
        own = getattr(found, "freshness", overall)
        hits.append({
            "session_id": getattr(row, "session_id", None),
            "title": getattr(row, "title", None),
            "status": getattr(row, "status", None),
            "case_id": getattr(row, "case_id", None),
            "snapshot_at": getattr(row, "snapshot_at", None),
            "freshness": _freshness(own),
        })
    return _emit({"hits": hits, "freshness": _freshness(overall)})


def _freshness(freshness: Any) -> dict:
    if freshness is None:
        return {}
    if isinstance(freshness, dict):
        return dict(freshness)
    return {
        "snapshot_at": getattr(freshness, "snapshot_at", None),
        "generation": getattr(freshness, "generation", None),
        "published_at": getattr(freshness, "published_at", None),
        "satisfied": getattr(freshness, "satisfied", None),
        "warning": getattr(freshness, "warning", None),
        "stopped_ok": getattr(freshness, "stopped_ok", None),
    }


def cmd_show(args: argparse.Namespace, reader: Any) -> int:
    """`agora show`：看一個 Session 的內容摘要與前後關係。"""
    result = reader.get_session(args.session, max_lag=_lag(args.max_lag))
    view = getattr(result, "value", result)
    session = getattr(view, "session", None)

    def _links(rows: Any) -> list[dict]:
        return [
            {
                "kind": getattr(r, "kind", None),
                "from": getattr(r, "from_session_id", None),
                "to": getattr(r, "to_session_id", None),
                "handoff_id": getattr(r, "handoff_id", None),
                "reference_id": getattr(r, "reference_id", None),
                "message_id": getattr(r, "message_id", None),
            }
            for r in (rows or ())
        ]

    def _handoffs(rows: Any) -> list[dict]:
        return [
            {
                "handoff_id": getattr(r, "handoff_id", None),
                "target_session_id": getattr(r, "target_session_id", None),
                "claimed_by_session_id": getattr(r, "claimed_by_session_id", None),
                "updated_at": getattr(r, "updated_at", None),
                "body": getattr(r, "body_json", ""),
            }
            for r in (rows or ())
        ]

    return _emit({
        "session": session if isinstance(session, dict) else getattr(session, "__dict__", {}),
        "links_out": _links(getattr(view, "links_out", ())),
        "links_in": _links(getattr(view, "links_in", ())),
        "handoffs_targeting": _handoffs(getattr(view, "handoffs_targeting", ())),
        "handoffs_by_holder": _handoffs(getattr(view, "handoffs_by_holder", ())),
        "snapshots": [
            {
                "snapshot_sha256": getattr(s, "snapshot_sha256", None),
                "snapshot_at": getattr(s, "snapshot_at", None),
                "via": getattr(s, "via", None),
            }
            for s in (getattr(view, "snapshots", ()) or ())
        ],
        "freshness": _freshness(getattr(result, "freshness", None)),
    })


def cmd_read(args: argparse.Namespace, reader: Any) -> int:
    """`agora read`：讀最新已提交的內容，並（給了 `--from` 時）記下參考關係。

    參考的 `read_snapshot_at` 就是**這一則剛讀到的**快照時間——讀與記在同一個
    指令裡，所以不會出現 skill 那個「自己去抓對方目前的時間，等於宣稱讀到了
    其實沒讀過的版本」的問題。沒有 `--from` 就只讀、不記（沒有人能宣稱是
    誰參考的）。
    """
    result = reader.get_reading(
        args.session, include_reasoning=args.include_reasoning,
        max_lag=_lag(args.max_lag),
    )
    reading = getattr(result, "value", result)
    freshness = getattr(result, "freshness", None)
    payload: dict[str, Any] = {
        "session_id": args.session,
        "title": reading.get("title"),
        "source": reading.get("source"),
        "snapshot_sha256": reading.get("snapshot_sha256"),
        "messages": reading.get("messages", []),
        "freshness": _freshness(freshness),
    }
    if args.from_session:
        payload["reference"] = _record_reference(
            args, reader, from_session=args.from_session,
            to_session=args.session,
            read_snapshot_at=freshness.snapshot_at if freshness else None,
        )
    else:
        payload["reference"] = None
    return _emit(payload)


def _record_reference(args: argparse.Namespace, reader: Any, *, from_session: str,
                      to_session: str, read_snapshot_at: str | None) -> dict:
    """照 skill 的 `reference` 做法放一筆參考（只上傳，不觸發提交）。"""
    from aistorage.inbox_builder import build_reference_item, upload_item
    from aistorage.syncer.__main__ import _deps
    from aistorage.syncer.config import SyncerConfig

    if not read_snapshot_at:
        raise CheckoutError(
            "讀不到這個 Session 的快照時間，參考一定要記「我讀到的那個版本」；"
            "請不要留下沒有依據的參考"
        )
    deps = _deps(SyncerConfig.load())
    item = build_reference_item(
        from_session_id=_qualify(from_session), to_session_id=_qualify(to_session),
        read_snapshot_at=read_snapshot_at, profile=deps.signer.profile,
        key=deps.signer.key, key_id=deps.signer.key_id, now=deps.clock.now_utc(),
    )
    upload_item(deps.drive, deps.inbox_folder_id, item)
    return {
        "reference_id": item.item_id,
        "from_session_id": item.sidecar["body"]["from_session_id"],
        "to_session_id": item.sidecar["body"]["to_session_id"],
        "read_snapshot_at": read_snapshot_at,
        "uploaded_only": True,
    }


def _qualify(session_id: str) -> str:
    """Session id 沒有 `<source>:` 前綴就補上（Agora 的 id 一律帶前綴）。"""
    text = session_id.strip()
    return text if ":" in text else f"opencode:{text}"


# ---------------------------------------------------------------------------
# handoff / checkout
# ---------------------------------------------------------------------------


def cmd_handoff(args: argparse.Namespace, reader: Any) -> int:
    """`agora handoff`：為每一份工作各寫一張交接單。

    **每個 task 物件一張交接單**（review-73dbf2c H3）：plugin 把模型給的
    `[{title, summary, next_steps}]` 原樣寫成一個 JSON 檔（`--tasks-file`），
    這裡讀進來交給 `split`——一個物件一張單。舊的 `--task` 是**每個字串一張**
    （給只有一句話的工作用），兩條路都留著。

    **只呼叫 skill 既有的 `split`／`handoff_end`**（它們自己會同步自己、算接續點、
    簽章、提交並等可見）。這裡不重寫交接單的組裝與接續點判定——那份邏輯有
    自己的測試與邊界（主 Session 限定、接續點必須是已完成的訊息、匯出檔重取…）。

    輸入的檢查放在**載入設定之前**：給錯參數時要在碰設定檔與狀態之前就講清楚，
    不要先因為找不到收件匣 folder id 而丟一個不相干的錯。
    """
    from aistorage.skill import tools
    from aistorage.skill.tools import SkillDeps
    from aistorage.syncer.__main__ import _deps
    from aistorage.syncer.config import SyncerConfig

    parts = _handoff_parts(args)
    if not parts:
        raise StartPointError("至少要給一個 task（每一個 task 是一張交接單）")
    if args.at:
        # 接續點由 `split` 依「最後一則已完成的訊息」決定；指定 --at 意味著要
        # 在別的位置交接，期 1 不支援——明確說清楚，不要靜靜忽略。
        raise StartPointError(
            "--at 還沒支援：交接單的接續點一定是「最後一則已完成的訊息」"
            "（AGENTS.md 的接續點定義）。要從別的位置開始，請用 "
            "`agora checkout <session>@<訊息>` 直接從那裡起一個新 session。"
        )
    deps = _deps(SyncerConfig.load())
    skill_deps = SkillDeps(deps=deps, state=deps.state)
    session_id = args.session or _main_session_id(deps)
    result = tools.split(skill_deps, session_id, parts, timeout=_lag(args.timeout))
    return _emit(result)


def _handoff_parts(args: argparse.Namespace) -> list[dict]:
    """`agora handoff` 的工作清單：`--tasks-file` 的物件一張單、`--task` 一字串一張。

    兩者可以混著給（後者接在後面）。回傳的是**形狀已正規化**的 dict 清單，
    `tools.split` 會再驗一次 title。
    """
    from aistorage.skill.tools import SkillError, normalize_parts

    parts: list[dict] = []
    path = getattr(args, "tasks_file", None)
    if path:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except ValueError as e:
            raise SkillError(f"--tasks-file 不是合法的 JSON: {e}") from None
        try:
            parts.extend(normalize_parts(parsed))
        except SkillError as e:
            raise SkillError(f"--tasks-file 的工作清單有問題: {e}") from None
    for title in (args.task or []):
        text = str(title).strip()
        if text:
            parts.append({"title": text, "summary": text,
                          "next_steps": args.next_steps})
    return parts


def cmd_checkout(args: argparse.Namespace, reader: Any) -> int:
    """`agora checkout`：產出起點包。"""
    writer_deps, _syncer = _writer_deps(args)
    writer_deps.reader = reader
    cfg = getattr(args, "_reader_cfg", None)
    drive = getattr(args, "_read_drive", None)
    fetcher = _object_fetcher(cfg, drive) if (cfg is not None and drive is not None) else None
    writer_deps.objects = fetcher
    try:
        pkg = _run_checkout(args, reader, writer_deps)
    finally:
        if fetcher is not None:
            fetcher.close()
    return _emit(pkg)


def _run_checkout(args: argparse.Namespace, reader: Any,
                  writer_deps: Any) -> dict[str, Any]:
    """跑 checkout 並回傳給人／給 AI 看的摘要（不是 `ContextPackage`）。"""
    pkg = checkout(
        reader,
        writer_deps,
        args.startpoint,
        Path(args.out),
        task=args.task,
        new_session_id=args.new_session_id,
        source=args.source,
        max_lag=_lag(args.max_lag),
        max_context_chars=args.max_chars or DEFAULT_MAX_CONTEXT_CHARS,
        claim_timeout=_lag(args.claim_timeout) or timedelta(minutes=15),
        resume=args.resume,
    )
    return {
        "package": str(Path(args.out) / "package.json"),
        "new_session_id": pkg.new_session_id,
        "segments": [
            {
                "order": s.order,
                "source_session_id": s.session_id,
                "snapshot_sha256": s.snapshot_sha256,
                "message_id": s.resolved.message_id,
                "handoff_id": s.resolved.handoff_id,
                "claim_id": s.claim_id,
                "text_chars": s.text_chars,
                "message_count": s.message_count,
            }
            for s in pkg.segments
        ],
        "totals": {
            "messages": pkg.messages,
            "raw_bytes": pkg.raw_bytes,
            "text_chars": pkg.text_chars,
        },
        "context_limit": pkg.max_context_chars,
        "claimed_handoffs": list(pkg.claimed_handoffs),
        # 呼叫者接下來要做的事（設計文件的下一行）
        "next": "把起點包交給轉接器載入：agora-opencode load "
                f"{args.out}",
    }


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agora", description="Agora：Session 的存放、查找與接續")
    parser.add_argument("--reader-config", default=None, help="讀者設定檔路徑")
    sub = parser.add_subparsers(dest="command", required=True)

    find = sub.add_parser("find", help="找 Session；--waiting 列出等人接的交接單")
    find.add_argument("query", nargs="?", default="", help="關鍵字（留空＝全部）")
    find.add_argument("--case", default=None, help="只看某個案件")
    find.add_argument("--status", default=None, choices=("running", "stopped"))
    find.add_argument("--limit", type=int, default=50)
    find.add_argument("--max-lag", dest="max_lag", default=None, help="例如 5m")
    find.add_argument("--waiting", action="store_true", help="列出還沒被認領的交接單")
    find.set_defaults(func=cmd_find)

    show = sub.add_parser("show", help="看一個 Session 的內容與前後關係")
    show.add_argument("session")
    show.add_argument("--max-lag", dest="max_lag", default=None)
    show.set_defaults(func=cmd_show)

    read = sub.add_parser("read", help="讀最新已提交的內容（給了 --from 就記參考）")
    read.add_argument("session")
    read.add_argument("--from", dest="from_session", default=None,
                      help="是誰讀的；給了才會記一條參考關係")
    read.add_argument("--include-reasoning", action="store_true")
    read.add_argument("--max-lag", dest="max_lag", default=None)
    read.set_defaults(func=cmd_read)

    handoff = sub.add_parser("handoff", help="每個 task 一張交接單並提交")
    handoff.add_argument("session", nargs="?", default=None,
                         help="要交出的 Session（預設是本機主 Session）")
    handoff.add_argument("--at", default=None, help="（還沒支援）指定接續的訊息")
    handoff.add_argument("--task", action="append",
                         help="一句話的工作；可重複，每個一張交接單")
    handoff.add_argument("--tasks-file", dest="tasks_file", default=None,
                         help="[{title, summary, next_steps}] 的 JSON 檔；"
                              "每個物件一張交接單")
    handoff.add_argument("--next-steps", dest="next_steps", default=None)
    handoff.add_argument("--timeout", default=None)
    handoff.set_defaults(func=cmd_handoff)

    co = sub.add_parser("checkout", help="產出起點包（不屬於任何 coding agent）")
    co.add_argument("startpoint", nargs="+",
                    help="起點：handoff:<id> 或 <session>[@<訊息>]（可多個＝n→1）")
    co.add_argument("-o", "--out", required=True, help="起點包的輸出目錄")
    co.add_argument("--task", default=None, help="要交代給接手者的任務")
    co.add_argument("--source", default="opencode",
                    help="省略 <source>: 前綴時補哪一個（預設 opencode）")
    co.add_argument("--new-session-id", dest="new_session_id", default=None,
                    help="為這個起點指定新 Session id（預設自編；1→n 時每個各編）")
    co.add_argument("--max-chars", dest="max_chars", type=int, default=None,
                    help=f"長度上限（單位 text_chars，預設 {DEFAULT_MAX_CONTEXT_CHARS}）")
    co.add_argument("--max-lag", dest="max_lag", default=None)
    co.add_argument("--claim-timeout", dest="claim_timeout", default=None,
                    help="等認領被讀取介面確認的時間（預設 15m）")
    co.add_argument("--resume", action="store_true",
                    help="沿用本機記錄的同一個認領（認領逾時或行程中斷後重跑）")
    co.add_argument("--claims-path", dest="claims_path", default=None,
                    help="本機認領記錄路徑（預設 "
                         "$AISTORAGE_CHECKOUT_CLAIMS 或 ~/.aistorage/checkout-claims.json）")
    co.set_defaults(func=cmd_checkout)

    return parser


def main(argv: Sequence[str] | None = None, *,
         reader_factory: Callable[[argparse.Namespace], Any] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if reader_factory is not None:
            reader = reader_factory(args)
        else:
            # 讀取設定與 Drive 用戶端一併帶著走：`checkout` 還要用它們去取原始紀錄
            reader, reader_cfg, read_drive = _reader_settings(args)
            args._reader_cfg = reader_cfg
            args._read_drive = read_drive
        return int(args.func(args, reader))
    except ClaimRejected as e:
        print(f"[agora] {e}", file=sys.stderr)
        return 6
    except ContextLimitExceeded as e:
        print(f"[agora] {e}", file=sys.stderr)
        return 2
    except AccessDenied as e:
        print(
            f"[agora] 讀不到 Agora 的讀取視圖：{e}\n"
            "[agora] 這是 fail-closed：不能確認內容有沒有進去，就不會寫任何東西。",
            file=sys.stderr,
        )
        return 3
    except ReadError as e:
        print(f"[agora] ReadError: {e}", file=sys.stderr)
        return 4
    except (ContextPackageError, StartPointError, CheckoutError) as e:
        print(f"[agora] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except (KeyError, ValueError, MismatchError) as e:
        print(f"[agora] {type(e).__name__}: {e}", file=sys.stderr)
        return 5
    except AiStorageError as e:
        print(f"[agora] {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

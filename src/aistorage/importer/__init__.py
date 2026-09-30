"""AiStorage 單一 Session 手動匯入工具（tasks 3.11）。

依據規格：
- docs/impl/group3-modules.md 第 7.4 節
- design D2（所有寫入先進收件匣）、D3、D4
- specs/common/item-model（Session id 為 `<來源應用>:<來源端 Session id>`）

用法：
    python -m aistorage.importer opencode    --export <匯出 JSON> ...
    python -m aistorage.importer claude-code --jsonl  <session jsonl> ...

輸出位置二選一：`--inbox-folder`（上傳到 Drive 收件匣資料夾）或 `--out-dir`
（寫成本機目錄的三個檔案）。產出的三個檔就是收件匣項目的形狀：
`<ULID>.raw`、`<ULID>.sidecar.json`、`<ULID>.sig`。

私鑰只以檔案路徑讀取，任何輸出都不含私鑰或簽章金鑰的私密內容。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from aistorage.clock import Clock, SystemClock, format_rfc3339
from aistorage.converters import get_converter
from aistorage.converters.base import SessionFacts
from aistorage.inbox_builder import (
    InboxBuildError,
    build_inbox_item,
    detect_source_session_id,
    load_private_key,
    read_item_key,
    serialize_json,
)

#: CLI 支援的來源應用（每個來源應用一個子命令）。
SUPPORTED_SOURCES = ("opencode", "claude-code")

#: 出處的預設值：手動匯入是本工具唯一的用途，固定字串足以辨識，不必帶本機路徑。
DEFAULT_PROVENANCE = "manual-import"

#: 預設讀取 rclone 設定檔的環境變數（workflow 只傳路徑）。
RCLONE_CONF_ENV = "AISTORAGE_RCLONE_CONF"


@dataclass(frozen=True)
class ImportResult:
    """一次匯入的結果（只含可公開的資訊）。"""

    source: str
    source_session_id: str
    item_id: str
    item_key: str
    raw_sha256: str
    raw_size: int
    snapshot_at: str
    status: str
    destination: str
    written: tuple[str, ...]
    facts_error: str | None = None      # 轉換器讀不到事實時的原因（仍然照匯入）


def file_mtime(path: Path) -> str:
    """原始紀錄檔的 mtime（RFC 3339 UTC，含小數）。

    手動匯入的檔案可能是幾天前匯出的，所以預設的快照時間與「來源端給不出時間
    時的保守值」都以它為基準（review-g3g L4／M10）。
    """
    return format_rfc3339(
        datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc),
        include_fraction=True,
    )


def _facts_or_conservative(
    conv: Any, raw_path: Path, session_id: str, mtime: str, strict: bool
) -> tuple[SessionFacts, str | None]:
    """取得 SessionFacts；失敗時回傳保守值與原因（`strict=True` 時直接拋出）。"""
    try:
        return conv.facts(raw_path, session_id=session_id), None
    except Exception as e:
        if strict:
            raise InboxBuildError(
                f"讀取 Session 事實失敗（--strict）: {type(e).__name__}: {e}"
            ) from e
        return (
            SessionFacts(
                title=None,
                created_at=mtime,
                updated_at=mtime,
                message_ids=(),
                archived_at=None,
                last_message_at=mtime,
                in_progress=False,
            ),
            f"{type(e).__name__}: {e}",
        )


def _resolve_parent_id(parent_id: str | None, source: str, raw_path: Path) -> str | None:
    """決定母 Session id：呼叫端指定的優先，否則從原始紀錄帶入。

    opencode 的匯出檔有 `info.parentID`（子代理 Session）；Claude Code 的 jsonl
    沒有這種欄位，帶入不到就回 None（期 1 不保證取得得到子 Session id）。
    兩者不一致是輸入問題，直接報錯不要猜。
    """
    from_raw = _parent_id_from_raw(source, raw_path)
    if not parent_id:
        return from_raw
    given = str(parent_id).strip()
    if not given:
        return from_raw
    if ":" not in given:
        given = f"{source}:{given}"
    if from_raw and from_raw != given:
        raise InboxBuildError(
            f"指定的 --parent-id ('{given}') 與原始紀錄中的母 Session ('{from_raw}') 不一致"
        )
    return given


def _parent_id_from_raw(source: str, raw_path: Path) -> str | None:
    """從原始紀錄讀出母 Session id（目前只有 opencode 的 `info.parentID`）。"""
    if source != "opencode":
        return None
    try:
        data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    info = data.get("info")
    if not isinstance(info, dict):
        return None
    parent = info.get("parentID")
    if isinstance(parent, str) and parent.strip() and parent.strip() not in ("null", "None"):
        value = parent.strip()
        return value if ":" in value else f"opencode:{value}"
    return None


def _write_local(
    out_dir: Path, item_key: str, raw_path: Path, sidecar_bytes: bytes, sig: dict
) -> tuple[str, ...]:
    """把三個檔案寫進本機目錄；sidecar 逐位元組照簽章時的內容寫出。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    names = (
        f"{item_key}.raw",
        f"{item_key}.sidecar.json",
        f"{item_key}.sig",
    )
    shutil.copyfile(raw_path, out_dir / names[0])
    (out_dir / names[1]).write_bytes(sidecar_bytes)
    (out_dir / names[2]).write_bytes(serialize_json(sig))
    return names


def _upload_drive(
    inbox_folder_id: str,
    item_key: str,
    raw_path: Path,
    sidecar_bytes: bytes,
    sig: dict,
    *,
    rclone_conf: str | Path | None,
    remote: str,
    drive: Any | None = None,
) -> tuple[str, ...]:
    """把三個檔案上傳到 Drive 的收件匣資料夾，回傳建立的 file id。

    `drive` 可注入既有的 DriveClient（例如單元測試的 FakeDrive）；未提供時才以
    rclone 設定檔建立 HttpDriveClient。憑證只以路徑取得，不在此讀取或輸出。
    """
    if drive is None:
        from aistorage.drive import HttpDriveClient, RcloneConfToken  # 延遲匯入

        conf = rclone_conf or os.environ.get(RCLONE_CONF_ENV)
        if not conf:
            raise InboxBuildError(
                f"上傳到收件匣需要 rclone 設定檔：請以 --rclone-conf 指定，"
                f"或設定環境變數 {RCLONE_CONF_ENV}（只傳路徑）"
            )
        drive = HttpDriveClient(RcloneConfToken(Path(conf), remote=remote))

    sig_bytes = serialize_json(sig)
    payloads = (
        (f"{item_key}.raw", raw_path, "application/octet-stream"),
        (f"{item_key}.sidecar.json", sidecar_bytes, "application/json"),
        (f"{item_key}.sig", sig_bytes, "application/json"),
    )
    created: list[str] = []
    for name, content, mime in payloads:
        created.append(drive.create(inbox_folder_id, name, content, mime_type=mime).id)
    return tuple(created)


def import_session(
    source: str,
    raw_path: Path,
    *,
    key_path: str | Path,
    key_id: str,
    profile: str,
    out_dir: Path | None = None,
    inbox_folder_id: str | None = None,
    source_session_id: str | None = None,
    parent_id: str | None = None,
    status: str = "running",
    stopped_at: str | None = None,
    case_id: str | None = None,
    provenance: str | None = DEFAULT_PROVENANCE,
    snapshot_at: str | None = None,
    now: str | None = None,
    item_key: str | None = None,
    rclone_conf: str | Path | None = None,
    remote: str = "gdrive",
    drive: Any | None = None,
    clock: Clock | None = None,
    strict: bool = False,
) -> ImportResult:
    """把單一 Session 的原始紀錄包成收件匣項目，輸出到本機目錄或上傳到收件匣。

    重複匯入同一個 Session 會得到同一個項目 id（`<source>:<source_session_id>`），
    內容相同時提交流程判為 ALREADY（3.4），不產生新版本。

    出處預設填 `DEFAULT_PROVENANCE`（`manual-import`）而不是本機路徑：本機路徑會把
    家目錄名稱寫進 Agora。`--provenance` 可覆寫。

    幾個刻意採取的行為（review-g3g M10、L3、L4）：
    - **轉換器讀不到事實時仍然匯入**（除非 `strict=True`）：這個工具存在的理由是
      「Claude Code 的 Session 約 30 天就會被本機清掉，需要時要能及時救進來」，
      轉換失敗就整個拒絕＝這個 Session 永遠救不回來。改用保守值（時間取檔案的
      mtime、`in_progress=False`、無標題），並在 metadata 留 `time_source: import`，
      提交流程那邊會照收原始紀錄、只在閱讀版上標記失敗。
    - **母 Session 自動帶入**：沒給 `parent_id` 時從原始紀錄取（opencode 的
      `info.parentID`、Claude Code 的子代理關聯）；兩者不一致就報錯。
    - **快照時間預設用檔案的 mtime**（可以用 `snapshot_at` 覆寫）：手動匯入的檔案
      可能是幾天前匯出的，mtime 比「匯入的當下」更接近 D4 的「擷取時間」。

    期 1 的已知限制：Claude Code 的**子代理內容不會被匯入**，也不保證取得得到子
    Session 的 id（見 reading-version.md 對應表）。母 Session 的內容完整保留。
    """
    if source not in SUPPORTED_SOURCES:
        raise InboxBuildError(
            f"不支援的來源應用: {source!r}（可用: {', '.join(SUPPORTED_SOURCES)}）"
        )
    if (out_dir is None) == (inbox_folder_id is None):
        raise InboxBuildError("必須且只能指定 --out-dir 或 --inbox-folder 其中之一")

    raw_p = Path(raw_path)
    if not raw_p.is_file():
        raise InboxBuildError(f"找不到原始紀錄檔案: {raw_p}")

    conv = get_converter(source)
    sid = source_session_id or detect_source_session_id(source, raw_p)
    raw_session_id = f"{source}:{sid}"

    mtime = file_mtime(raw_p)
    facts, facts_error = _facts_or_conservative(conv, raw_p, raw_session_id, mtime, strict)

    # 母 Session：呼叫端沒給就從原始紀錄帶入；兩者不一致是輸入問題，報錯不要猜
    effective_parent = _resolve_parent_id(parent_id, source, raw_p)

    key = load_private_key(key_path)

    sidecar_bytes, sig = build_inbox_item(
        raw_p,
        source=source,
        source_session_id=sid,
        facts=facts,
        profile=profile,
        key=key,
        key_id=key_id,
        parent_id=effective_parent,
        status=status,
        stopped_at=stopped_at,
        case_id=case_id,
        provenance=provenance,
        snapshot_at=snapshot_at or mtime,
        now=now,
        item_key=item_key,
        clock=clock or SystemClock(),
        # 事實是保守值時，時間來自檔案 mtime 而非來源端，明確標記
        time_source="import" if facts_error else None,
    )

    item_key_value = read_item_key(sidecar_bytes)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    raw_meta = sidecar["raw"]
    session_meta = sidecar["session"]

    if out_dir is not None:
        written = _write_local(Path(out_dir), item_key_value, raw_p, sidecar_bytes, sig)
        destination = f"out-dir:{Path(out_dir)}"
    else:
        written = _upload_drive(
            str(inbox_folder_id),
            item_key_value,
            raw_p,
            sidecar_bytes,
            sig,
            rclone_conf=rclone_conf,
            remote=remote,
            drive=drive,
        )
        destination = f"inbox-folder:{inbox_folder_id}"

    return ImportResult(
        source=source,
        source_session_id=sid,
        item_id=sidecar["metadata"]["id"],
        item_key=item_key_value,
        raw_sha256=raw_meta["sha256"],
        raw_size=raw_meta["size"],
        snapshot_at=session_meta["snapshot_at"],
        status=session_meta["status"],
        destination=destination,
        written=written,
        facts_error=facts_error,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點：`python -m aistorage.importer <source> ...`。"""
    import argparse

    parser = argparse.ArgumentParser(
        prog="aistorage.importer",
        description="AiStorage 單一 Session 手動匯入工具（包成收件匣項目）",
    )
    subparsers = parser.add_subparsers(dest="source", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--key", required=True, help="簽章金鑰私鑰檔路徑（權限 600）")
        p.add_argument("--key-id", required=True, help="簽章金鑰識別碼")
        p.add_argument("--profile", required=True, help="寫入者 profile（例如 mac-opencode）")
        p.add_argument("--out-dir", help="輸出到本機目錄（三個檔案）")
        p.add_argument("--inbox-folder", help="上傳到此 Drive 收件匣資料夾 id")
        p.add_argument("--session-id", help="明確指定來源端 Session id（省略時自動推測）")
        p.add_argument("--parent-id", help="子 Session 的母 Session id")
        p.add_argument(
            "--stopped",
            action="store_true",
            help="明確宣告此 Session 已停止中（不可與仍在生成中的 Session 並存）",
        )
        p.add_argument("--stopped-at", help="停止時間（RFC 3339 UTC Z，預設為現在）")
        p.add_argument("--case-id", help="所屬案件 id")
        p.add_argument(
            "--provenance",
            help=f"出處說明（預設 {DEFAULT_PROVENANCE}；不帶本機路徑）",
        )
        p.add_argument("--snapshot-at", help="快照時間（RFC 3339 UTC Z，預設為現在）")
        p.add_argument("--item-key", help="項目識別碼 ULID（測試或重試時指定）")
        p.add_argument(
            "--strict",
            action="store_true",
            help="轉換器讀不到 Session 事實時直接拒絕匯入（預設是照匯入並留下 time_source 標記）",
        )
        p.add_argument("--rclone-conf", help=f"rclone 設定檔路徑（或 ${RCLONE_CONF_ENV}）")
        p.add_argument("--remote", default="gdrive", help="rclone remote 名稱（預設 gdrive）")

    p_opencode = subparsers.add_parser(
        "opencode", help="匯入 opencode 匯出的單一 Session JSON"
    )
    p_opencode.add_argument("--export", required=True, help="opencode 匯出 JSON 路徑")
    add_common(p_opencode)

    p_claude = subparsers.add_parser(
        "claude-code", help="匯入 Claude Code 的單一 Session jsonl"
    )
    p_claude.add_argument("--jsonl", required=True, help="Claude Code session jsonl 路徑")
    add_common(p_claude)

    args = parser.parse_args(argv)
    raw_path = getattr(args, "export", None) or getattr(args, "jsonl")
    stopped_at = args.stopped_at
    if args.stopped and not stopped_at:
        from aistorage.clock import SystemClock

        stopped_at = SystemClock().now_utc()

    try:
        result = import_session(
            args.source,
            Path(raw_path),
            key_path=args.key,
            key_id=args.key_id,
            profile=args.profile,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            inbox_folder_id=args.inbox_folder,
            source_session_id=args.session_id,
            parent_id=args.parent_id,
            status="stopped" if args.stopped else "running",
            stopped_at=stopped_at,
            case_id=args.case_id,
            provenance=args.provenance or DEFAULT_PROVENANCE,
            snapshot_at=args.snapshot_at,
            item_key=args.item_key,
            rclone_conf=args.rclone_conf,
            remote=args.remote,
            strict=args.strict,
        )
    except Exception as e:
        sys.stderr.write(f"匯入失敗: {e}\n")
        return 1

    if result.facts_error:
        # 原始紀錄已送出，但閱讀版會是失敗狀態：講清楚，不要埋在 log 裡
        sys.stderr.write(
            f"警告：讀取 Session 事實失敗（{result.facts_error}）；"
            "仍已匯入原始紀錄，提交流程會照收並把閱讀版標記為失敗。\n"
        )

    # 只印可公開的資訊：id、雜湊、計數、位置。絕不印私鑰或簽章內容。
    print(f"item_id: {result.item_id}")
    print(f"item_key: {result.item_key}")
    print(f"source_session_id: {result.source_session_id}")
    print(f"raw_sha256: {result.raw_sha256}")
    print(f"raw_size: {result.raw_size}")
    print(f"snapshot_at: {result.snapshot_at}")
    print(f"status: {result.status}")
    print(f"destination: {result.destination}")
    print(f"written: {len(result.written)} 個檔案")
    for name in result.written:
        print(f"  - {name}")
    return 0


__all__ = [
    "ImportResult",
    "SUPPORTED_SOURCES",
    "DEFAULT_PROVENANCE",
    "import_session",
    "main",
]

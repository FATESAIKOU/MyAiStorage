"""同步並提交（tasks 5.3，D9／ADR 0007）。

流程（docs/impl/group5-7 第 3 節）：
1. `sync_once(only=session_ids)` 強制上傳指定 Session 的最新版本。
2. 上傳 `extra_items`（交接單、認領、參考），順序在 session 之後。
3. 觸發提交流程（PAT 只從檔案讀，不進 argv／log／例外訊息；1.6）。
4. 輪詢讀取介面，等每一個項目「看得到」或有拒收記錄。
5. 顯示進度；逾時訊息固定包含「提交流程可能被停用或遭到注入（請執行健康檢查 6.3）」。
6. **不以 run id 判斷完成**（concurrency group 可能取消排隊中的 run；D2／ADR 0007）。

讀取端只需要三件事：`catalog`（Session 有沒有進來）、`get_session`（Link／交接單）、
`get_rejection`（被拒收了嗎）。全部都是**讀取**，不觸發任何寫入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import json
from pathlib import Path
import time
from typing import Any, Callable, Protocol, Sequence, runtime_checkable
import urllib.error
import urllib.request

from aistorage.clock import Clock
from aistorage.drive.model import DriveClient
from aistorage.errors import ReadError
from aistorage.inbox_builder import BuiltItem, upload_item
from aistorage.syncer.core import Signer, SyncOutcome, sync_once
from aistorage.syncer.opencode_api import OpencodeApi
from aistorage.syncer.state import SyncState

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = timedelta(minutes=15)
DEFAULT_POLL = timedelta(seconds=20)

#: 逾時時固定顯示的字串（spec：一定要提示健康檢查）
STALENESS_HINT = "提交流程可能被停用或遭到注入（請執行健康檢查 6.3）"

KIND_SESSION = "session"
KIND_HANDOFF = "handoff"
KIND_CLAIM = "claim"
KIND_REFERENCE = "reference"


@dataclass(frozen=True)
class Awaited:
    """等待中的一個項目。

    `view_session` 是「要去哪個 Session 的視圖看」：交接單看**被接續**的
    Session（它會收到那張交接單）；認領／參考看**自己**（Link 是自己發出的）。
    `target` 則是項目自己的 id（handoff:…／claim:…／reference:…）。
    """

    item_key: str
    kind: str
    target: str
    sha256: str | None = None      # session：上傳的 raw sha256
    source: str = "opencode"       # session：Agora id 的 source
    view_session: str = ""         # handoff/claim/reference：要看的 Session id

    @property
    def session_id(self) -> str | None:
        return f"{self.source}:{self.target}" if self.kind == KIND_SESSION else None


@dataclass(frozen=True)
class CommitWaitResult:
    """等待的結果。"""

    visible: tuple[Awaited, ...] = ()
    rejected: tuple[tuple[Awaited, str], ...] = ()
    pending: tuple[Awaited, ...] = ()
    timed_out: bool = False
    elapsed_s: float = 0.0
    trigger_error: str | None = None
    #: 這一次有沒有真的嘗試觸發提交流程。`False` = 根本沒有 PAT／repo 可用
    #: （不是「觸發失敗」）。兩者對「要不要等」的處置不同，見 sync_and_commit。
    trigger_attempted: bool = True
    sync: SyncOutcome | None = None
    sync_errors: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        """全部項目都有結果（看得到或被拒收）且沒有逾時、也沒有同步錯誤。

        `trigger_error` 不算在這裡：它只代表「這次沒有即時提交」，
        排程或外部的提交流程仍然會收進去，所以上傳本身還是成功的。
        """
        return (
            not self.timed_out
            and not self.pending
            and not self.sync_errors
        )

    def summary(self) -> str:
        done = len(self.visible) + len(self.rejected)
        total = done + len(self.pending)
        head = f"{done}／{total} 有結果，已等 {self.elapsed_s:.0f}s"
        if self.trigger_error:
            ids = ", ".join(f"{a.kind}:{a.target}" for a in self.pending) or "-"
            if self.trigger_attempted:
                # 嘗試過但失敗：等待期間不會有任何變化
                return (
                    f"{head}；觸發提交流程失敗（{self.trigger_error}）→ 沒有等待；"
                    "上傳已成功，排程的提交流程會在下一輪定時提交時收進去；"
                    f"未提交的 id：{ids}"
                )
            # 根本沒有 PAT／repo：排程或外部的提交流程仍可能收進去（ADR 0007）
            return (
                f"{head}；未觸發提交流程（{self.trigger_error}）→ 仍等讀取介面"
                "（排程或外部的提交流程可能會收進去，ADR 0007）；"
                f"未提交的 id：{ids}"
            )
        if self.timed_out:
            ids = ", ".join(f"{a.kind}:{a.target}" for a in self.pending) or "-"
            return f"{head}；逾時：{STALENESS_HINT}；還沒看到的 id：{ids}"
        return head


@runtime_checkable
class CommitReader(Protocol):
    """等待期間用到的讀取介面。"""

    def catalog(self, session_ids: Sequence[str]) -> Any: ...

    def get_session(self, session_id: str, *, max_lag: Any = None) -> Any: ...

    def get_rejection(self, item_key: str) -> Any: ...


@dataclass
class SyncDeps:
    """`sync_and_commit` 需要的相依（全部注入，方便測試）。"""

    api: OpencodeApi
    reader: CommitReader
    drive: DriveClient
    inbox_folder_id: str
    signer: Signer
    state: SyncState
    clock: Clock
    workdir: Path
    converter: Any | None = None
    pat_path: Path | None = None
    repo: str | None = None
    workflow: str = "committer.yml"


def _value(result: Any, default: Any = None) -> Any:
    if result is None:
        return default
    if hasattr(result, "value"):
        return result.value
    return result


def trigger_committer(
    pat_path: Path | str,
    repo: str,
    workflow: str = "committer.yml",
    *,
    timeout: float = 30.0,
) -> None:
    """觸發提交流程（`workflow_dispatch`，**一律帶 `ref: main`**）。

    PAT 只從檔案讀，放進 Authorization header：**不進 argv、不進 log、
    不進例外訊息**（1.6 的做法；D2 的 workflow 不接受任何 inputs）。
    """
    path = Path(pat_path)
    if not path.is_file():
        raise ReadError(f"找不到 GitHub PAT 檔案（只以路徑引用）: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ReadError(f"GitHub PAT 檔案是空的: {path}")
    if not repo or "/" not in repo:
        raise ReadError(f"repo 必須是 owner/name 格式: {repo!r}")

    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": "main"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "content-type": "application/json",
            "user-agent": "aistorage-syncer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (204, 200, 201):
                raise ReadError(f"觸發提交流程失敗 (HTTP {resp.status})")
    except urllib.error.HTTPError as e:
        # 訊息裡不帶 token
        raise ReadError(f"觸發提交流程失敗 (HTTP {e.code}): repo={repo} workflow={workflow}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ReadError(f"觸發提交流程失敗：{type(e).__name__}") from None


def _rejection_code(reader: CommitReader, item_key: str) -> str | None:
    if not item_key:
        return None
    try:
        row = _value(reader.get_rejection(item_key))
    except Exception:
        return None
    if row is None:
        return None
    code = getattr(row, "code", None) or (row.get("code") if isinstance(row, dict) else None)
    return str(code) if code else "rejected"


def _is_visible(reader: CommitReader, item: Awaited) -> bool:
    """判斷一個項目是否已經在讀取視圖裡看得到。"""
    if item.kind == KIND_SESSION:
        sid = f"{item.source}:{item.target}"
        try:
            catalog = _value(reader.catalog([sid]), {}) or {}
        except Exception:
            return False
        entry = catalog.get(sid)
        if entry is None:
            return False
        raw_sha = (
            entry.get("raw_sha256")
            if isinstance(entry, dict)
            else getattr(entry, "raw_sha256", None)
        )
        return bool(item.sha256) and raw_sha == item.sha256

    # handoff / claim / reference 都要看連結在哪裡
    if not item.view_session:
        return False
    try:
        view = _value(reader.get_session(item.view_session))
    except Exception:
        return False
    if view is None:
        return False
    if item.kind == KIND_HANDOFF:
        # 被接續的 Session 的交接單清單裡有沒有這個 handoff id
        for group in ("handoffs_targeting", "handoffs_by_holder"):
            for h in getattr(view, group, ()) or ():
                if getattr(h, "handoff_id", None) == item.target:
                    return True
        return False
    # claim：接續 Link 的 claim_id 是這次的 claim，而且 from 是自己
    # （view_session 已經保證是「自己」）
    for link in getattr(view, "links_out", ()) or ():
        if (item.kind == KIND_CLAIM
                and getattr(link, "kind", None) == "continuation"
                and getattr(link, "claim_id", None) == item.target):
            return True
        if (item.kind == KIND_REFERENCE
                and getattr(link, "reference_id", None) == item.target):
            return True
    return False


def _other_session(item: Awaited) -> str:
    """相容用：非 session 項目的 view_session 缺值時不能判斷。"""
    return item.view_session


def awaited_for_item(item: BuiltItem) -> Awaited:
    """從 BuiltItem 推出等待條件（看哪個 Session 的視圖）。"""
    kind = item.item_type
    body = item.sidecar.get("body") or {}
    if kind == KIND_HANDOFF:
        return Awaited(
            item_key=item.item_key, kind=kind, target=item.item_id,
            view_session=str(body.get("target_session_id") or ""),
        )
    if kind == KIND_CLAIM:
        return Awaited(
            item_key=item.item_key, kind=kind, target=item.item_id,
            view_session=str(body.get("claimer_session_id") or ""),
        )
    if kind == KIND_REFERENCE:
        return Awaited(
            item_key=item.item_key, kind=kind, target=item.item_id,
            view_session=str(body.get("from_session_id") or ""),
        )
    return Awaited(item_key=item.item_key, kind=kind, target=item.item_id)


def wait_visible(
    reader: CommitReader,
    awaited: Sequence[Awaited],
    *,
    timeout: timedelta = DEFAULT_TIMEOUT,
    poll: timedelta = DEFAULT_POLL,
    progress: Callable[[str], None] = print,
    clock: Clock | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> CommitWaitResult:
    """輪詢讀取介面，等每個項目看得到或被拒收。

    **不以 run id 判斷完成**：只看讀取端看得到什麼。

    `monotonic`／`sleeper` 可注入：單元測試用假時鐘走完整個逾時流程，
    不用真的睡到逾時（PM 追加第 1 點）。
    """
    started = monotonic()
    deadline = started + max(0.0, timeout.total_seconds())
    remaining = list(awaited)
    visible: list[Awaited] = []
    rejected: list[tuple[Awaited, str]] = []
    pending: list[Awaited] = []
    last_note = -1

    while True:
        still: list[Awaited] = []
        for item in remaining:
            code = _rejection_code(reader, item.item_key)
            if code:
                rejected.append((item, code))
                continue
            if _is_visible(reader, item):
                visible.append(item)
                continue
            still.append(item)
        remaining = still
        elapsed = monotonic() - started
        if not remaining:
            return CommitWaitResult(
                visible=tuple(visible),
                rejected=tuple(rejected),
                pending=(),
                timed_out=False,
                elapsed_s=elapsed,
            )
        if monotonic() >= deadline:
            done = len(visible) + len(rejected)
            total = done + len(remaining)
            progress(
                f"{done}／{total} 有結果，已等 {elapsed:.0f}s；"
                f"{STALENESS_HINT}；還沒看到的 id："
                + ", ".join(f"{a.kind}:{a.target}" for a in remaining)
            )
            return CommitWaitResult(
                visible=tuple(visible),
                rejected=tuple(rejected),
                pending=tuple(remaining),
                timed_out=True,
                elapsed_s=elapsed,
            )
        if int(elapsed) // 30 != last_note:
            last_note = int(elapsed) // 30
            done = len(visible) + len(rejected)
            progress(f"{done}／{done + len(remaining)} 有結果，已等 {elapsed:.0f}s")
        # 睡到「再睡就會超過 deadline」為止：逾時時間比輪詢間隔短時也不會
        # 被整個 poll 間隔拖住（否則 --timeout 2s 會變成 20s 才回來）。
        left = deadline - monotonic()
        sleeper(max(0.05, min(poll.total_seconds(), left)))


def build_awaited_session(
    item_key: str, session_id: str, sha256: str, source: str = "opencode"
) -> Awaited:
    """session 的 Awaited（target 是來源端的 session id）。"""
    return Awaited(item_key=item_key, kind=KIND_SESSION, target=session_id, sha256=sha256,
                   source=source)


def build_awaited(kind: str, item_key: str, target: str, view_session: str = "") -> Awaited:
    """handoff／claim／reference 的 Awaited。

    `target` 是項目自己的 id；`view_session` 是要去哪個 Session 的視圖看
    （交接單看被接續者，認領／參考看自己）。
    """
    return Awaited(item_key=item_key, kind=kind, target=target, view_session=view_session)


def sync_and_commit(
    *,
    session_ids: Sequence[str] = (),
    extra_items: Sequence[BuiltItem] = (),
    deps: SyncDeps,
    timeout: timedelta = DEFAULT_TIMEOUT,
    poll: timedelta = DEFAULT_POLL,
    progress: Callable[[str], None] = print,
    already_synced: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> CommitWaitResult:
    """同步並提交：上傳 → 觸發提交流程 → 等讀取介面看得到。

    `already_synced=True`：呼叫端（例如 `split`）為了拿快照雜湊已經先同步過，
    這裡不要再同步一次——否則同一份 raw 會在收件匣裡出現兩組檔案。

    Returns:
        CommitWaitResult（含每個項目的結果；逾時時 pending 裡是還沒看到的 id）。
    """
    outcome = None if already_synced else sync_once(
        api=deps.api,
        reader=deps.reader,          # type: ignore[arg-type]
        drive=deps.drive,
        inbox_folder_id=deps.inbox_folder_id,
        signer=deps.signer,
        state=deps.state,
        clock=deps.clock,
        workdir=deps.workdir,
        converter=deps.converter,
        only=list(session_ids) if session_ids else None,
    )

    # extra_items 在 session 之後上傳（apply 的順序也是 session 在前）
    for item in extra_items:
        upload_item(deps.drive, deps.inbox_folder_id, item)

    # 等待清單：session 用上傳的 item_key 與 raw sha；其他用項目 id
    failed = {sid for sid, _code in (outcome.errors if outcome is not None else ())}
    awaited: list[Awaited] = []
    for session_id in session_ids:
        sid = session_id if ":" in session_id else f"opencode:{session_id}"
        rec = deps.state.sessions.get(sid)
        if rec is None or not rec.last_item_key or not rec.last_uploaded_sha:
            continue
        if sid in failed:
            # 這一輪沒上傳成功（還沒被拒收過 → 重新算 item_key）
            continue
        awaited.append(
            build_awaited_session(
                rec.last_item_key, sid.split(":", 1)[1], rec.last_uploaded_sha,
                source=sid.split(":", 1)[0],
            )
        )
    for item in extra_items:
        awaited.append(awaited_for_item(item))

    # 「沒有觸發」和「觸發失敗」是兩件事，處置不同：
    # - 沒有 PAT／repo（e2e、以及不想用 GitHub Actions 的環境）：**照樣等**。
    #   ADR 0007 的「寫入者以讀取介面判斷完成」就是這種情況——提交流程可能由
    #   排程、由測試在本機跑。e2e 正是靠這個等待，讓測試端的提交流程把項目收進去。
    # - 觸發**嘗試過而且失敗**：立刻回報（見下）。等待期間不會有任何變化。
    trigger_error: str | None = None
    trigger_attempted = False
    if deps.pat_path and deps.repo:
        trigger_attempted = True
        try:
            trigger_committer(deps.pat_path, deps.repo, deps.workflow)
            progress("已觸發提交流程（workflow_dispatch，ref=main）")
        except ReadError as e:
            trigger_error = str(e)
            progress(f"觸發提交流程失敗：{trigger_error}")
    else:
        trigger_error = "缺少 gh-pat-actions.txt 或 repo 設定（本次沒有觸發）"
        progress(
            f"未觸發提交流程：{trigger_error}；仍然等讀取介面，"
            "因為排程或外部的提交流程可能會把它收進去（ADR 0007）"
        )

    # 同步階段就失敗的 Session：不可能看得到，明確帶出來（不要靜靜地逾時）
    sync_errors = tuple(outcome.errors) if outcome is not None else ()
    if sync_errors and progress:
        for sid, code in sync_errors:
            progress(f"同步 {sid} 失敗：{code}")

    # 觸發失敗就**立即**回報，完全不進入等待（review-g5-6 M4／PM 追加第 1 點）。
    # 沒有任何東西會讓讀取視圖改變：排程的提交流程還沒被觸發，乾等 15 分鐘
    # 只會讓呼叫端（AI 的工具）卡住，然後回報一個沒有意義的逾時。
    # 上傳本身是成功的，排程的提交流程仍然會在下一輪把這些項目收進去。
    if trigger_error and trigger_attempted:
        progress(
            f"觸發提交流程失敗，這一輪不等了（等待期間不會有任何變化）；"
            "上傳已成功，排程的提交流程會在下一輪定時提交時收進去：{trigger_error}"
        )
        return CommitWaitResult(
            visible=(),
            rejected=(),
            pending=tuple(awaited),
            timed_out=False,
            elapsed_s=0.0,
            trigger_error=trigger_error,
            trigger_attempted=trigger_attempted,
            sync=outcome,
            sync_errors=sync_errors,
        )

    result = wait_visible(
        deps.reader,  # type: ignore[arg-type]
        awaited,
        timeout=timeout,
        poll=poll,
        progress=progress,
        clock=deps.clock,
        monotonic=monotonic,
        sleeper=sleeper,
    )
    return CommitWaitResult(
        visible=result.visible,
        rejected=result.rejected,
        pending=result.pending,
        timed_out=result.timed_out,
        elapsed_s=result.elapsed_s,
        trigger_error=trigger_error,
        trigger_attempted=trigger_attempted,
        sync=outcome,
        sync_errors=sync_errors,
    )

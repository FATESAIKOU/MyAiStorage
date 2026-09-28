"""AiStorage 提交流程主體編排模組（tasks 3.1）。

依據規格：
- docs/impl/group3-modules.md 第 7 節
- design D2（13 步提交流程、兩階段釘選、清掃與驗證、log 規則）
- ADR 0008（信任錨點與偵測隔離）
- review-1.2-1.6 H3（push 後以 ls-remote 與 manifest 驗證）
- review-1.4f3、review-1.4f5（結算待定、清掃計畫）
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from typing import Any
import urllib.error
import urllib.request

from aistorage.agora import layout
from aistorage.agora.apply import (
    apply_claim,
    apply_handoff,
    apply_reference,
    apply_session,
)
from aistorage.agora.store import (
    AgoraStore,
    AnnexRawStorage,
    FakeRawStorage,
    GitRawStorage,
    RawStorage,
)
from aistorage.annex.fake import FakeAnnexGit
from aistorage.annex.git import AnnexGit, SubprocessAnnexGit
from aistorage.annex.manifest import parse_manifest
from aistorage.clock import Clock, format_rfc3339
from aistorage.committer.config import CommitterConfig, RepoConfig
from aistorage.committer.publish import NullPublisher, ReadViewPublisher
from aistorage.converters import CONVERTERS, get_converter
from aistorage.converters.base import Converter
from aistorage.drive.model import DriveClient, DriveFile
from aistorage.errors import (
    AbortRun,
    AiStorageError,
    MismatchError,
    NotFound,
    ReadError,
    WriteError,
)
from aistorage.identity import Registry
from aistorage.integrity.gc import (
    collect_removed_bundles,
    gc_removed,
    purge_quarantine,
)
from aistorage.integrity.pin import PinPending, PinState, PinStore
from aistorage.integrity.settle import (
    RepoListing,
    SettleOutcome,
    settle,
)
from aistorage.integrity.sweep import (
    Disposition,
    SweepDecision,
    apply_sweep,
    check_parents,
    plan_readview_sweep,
    plan_sweep,
    resolve_content_checks,
)
from aistorage.integrity.verify import (
    precheck,
    verify_after_push,
    verify_clone,
)
from aistorage.intake.evaluate import (
    Decision,
    DecisionKind,
    evaluate,
    sort_accepted_decisions,
)
from aistorage.intake.ledger import Ledger
from aistorage.foundry.apply import apply_artifact
from aistorage.intake.scan import count_shaped, scan_inboxes
from aistorage.publish.publisher import load_manifest
from aistorage.publish.rejections import collect_rejections
from aistorage.readview.model import trusted_ids
from aistorage.schema import generate_ulid


@dataclass
class Deps:
    """提交流程相依元件聚合。"""

    drive: DriveClient
    pins: PinStore
    git_factory: Callable[[Path], AnnexGit]
    registry: Registry
    converters: dict[str, Converter]
    publisher: ReadViewPublisher
    clock: Clock
    raw_storage_factory: Callable[[Path, AnnexGit], RawStorage] | None = None


@dataclass
class RunReport:
    """單次提交流程執行報告。

    遵循 D2 log 規則：只記錄 id、計數與耗時，絕不記錄機敏內文或金鑰。
    """

    run_id: str
    aborted_at: str | None = None
    code: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)
    guard: str = "ok"
    readview_sweep: str | None = None
    #: 讀取視圖發佈結果：published／skipped／planned／publish_failed／skipped_no_readview
    readview_publish: str | None = None
    #: 發佈失敗時的例外類型名（不印內文，避免洩漏路徑或內容）
    publish_error: str | None = None
    #: 管理操作進行中：active（有維護旗標）／flag_corrupt（旗標損毀，fail-closed）
    maintenance: str | None = None
    #: 為什麼沒有處理 Foundry：no_artifacts（收件匣沒有 artifact，依 PM 決定 9 不 clone）
    foundry_skipped: str | None = None
    #: 維護旗標的原因字串（管理者留下的，會出現在 log，所以只印短字串）
    maintenance_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.aborted_at is None

    def format_log(self) -> str:
        """格式化日誌輸出。"""
        status = "SUCCESS" if self.ok else f"ABORTED({self.aborted_at}:{self.code})"
        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        durations_str = ", ".join(f"{k}={v}ms" for k, v in sorted(self.durations_ms.items()))
        rv_str = f" | readview_sweep={self.readview_sweep}" if self.readview_sweep else ""
        pub_str = f" | readview_publish={self.readview_publish}" if self.readview_publish else ""
        err_str = f" ({self.publish_error})" if self.publish_error else ""
        mt_str = f" | maintenance={self.maintenance}" if self.maintenance else ""
        return (
            f"[RunReport {self.run_id}] {status} | counts: [{counts_str}]"
            f" | durations: [{durations_str}]{rv_str}{pub_str}{mt_str}{err_str}"
        )


def _check_test_kill(step_name: str) -> None:
    """測試中斷注入檢查（AISTORAGE_TEST_KILL_AFTER）。"""
    kill_after = os.environ.get("AISTORAGE_TEST_KILL_AFTER")
    if kill_after and (kill_after == step_name or kill_after == step_name.split(".")[-1]):
        os._exit(42)


#: 收件匣裡的 junk（名稱不符合格式的檔案或資料夾）超過這個時間就刪除（D2：24 小時）。
JUNK_RETENTION = timedelta(hours=24)

#: traceback 除錯檔的輸出目錄（review-g3g L：只寫檔，不進 Actions log）。
DEBUG_DIR_ENV = "AISTORAGE_DEBUG_DIR"
DEFAULT_DEBUG_DIR = "debug"


def _publish_foundry(
    target: RepoTarget,
    deps: Deps,
    store: Any,
    state: PinState | None,
    workdir: Path,
    dry_run: bool,
) -> str:
    """Foundry 讀取視圖發佈（第 13 步的 Foundry 版本，group5-7 第 6.3 節）。

    讀取介面以 **Drive file id** 取收容產出（D5），所以索引裡必須先有
    `object_file_id`：`resolve_object_file_ids` 以前綴列舉比對 `name == annex_key`，
    並擋掉不在正式 pin 的 key。對不上（`object_not_found`／`checksum_mismatch`／
    `size_mismatch`／`key_not_in_pin`）的那幾筆**不發佈**，只回報計數。
    """
    from aistorage.foundry.index import (
        ArtifactRow,
        FoundryIndexMeta,
        build_foundry_index,
        resolve_object_file_ids,
    )

    if not target.readview_folder_id:
        return "skipped_no_readview"
    if store is None:
        return "skipped_no_store"

    rows: list[ArtifactRow] = []
    for cat in store.list_catalog():
        meta = cat.get("metadata") or {}
        body = cat.get("body") or {}
        rows.append(
            ArtifactRow(
                artifact_id=str(meta.get("id") or ""),
                kind=str(body.get("kind") or "link"),
                name=str(body.get("name") or ""),
                producer=str(meta.get("producer") or ""),
                produced_by_session_id=str(body.get("produced_by_session_id") or ""),
                created_at=str(meta.get("created_at") or ""),
                updated_at=str(meta.get("updated_at") or ""),
                content_type=body.get("content_type"),
                case_id=meta.get("case_id"),
                size=body.get("size"),
                sha256=body.get("sha256"),
                annex_key=body.get("object_key"),
                repo=body.get("repo"),
                path=body.get("path"),
                link=body.get("link"),
            )
        )

    publishable, issues = resolve_object_file_ids(
        rows,
        drive=deps.drive,
        prefix_folder_id=target.prefix_folder_id,
        allowed_keys=state.annex_keys if state is not None else None,
    )
    generation = int(deps.clock.now().timestamp())
    index_path = Path(workdir) / "foundry-index.json"
    build_foundry_index(
        index_path,
        artifacts=publishable,
        rejections=collect_rejections(store, []),
        meta=FoundryIndexMeta(
            generation=generation,
            built_at=format_rfc3339(deps.clock.now()),
            foundry_main_sha=(state.refs.get("refs/heads/main", "") if state else ""),
        ),
    )
    if dry_run:
        return "planned"
    deps.drive.create(
        target.readview_folder_id,
        f"foundry-index-{generation}.json",
        index_path.read_bytes(),
        mime_type="application/json",
    )
    # 對不上的那幾筆不發佈，但要把數量講出來（review F-H3）
    return "published" if not issues else f"published_partial({len(issues)})"


def _pipeline_step(current_step: str, ctx: PipelineContext | None) -> str:
    """多 repo 時，中止點要標成「哪個 repo 的哪一步」。

    pipeline 內部用 `ctx.step` 記錄進度（RepoPipeline 只能看到自己的 ctx），
    所以例外發生時以它為準；還沒進 pipeline 就失敗時退回外層的 current_step。
    """
    if ctx is not None and ctx.step and ctx.step != "pipeline":
        return ctx.step if not ctx.prefix else f"{ctx.prefix}{ctx.step}"
    return current_step


def _check_maintenance(cfg: CommitterConfig, deps: Deps, report: RunReport) -> bool:
    """第 1b 步：檢查 pin repo 的維護旗標；有旗標就整輪不做任何事。

    回傳 True 表示「因為維護中而結束」。旗標讀不到或損毀時 fail-closed：
    把它當成維護中（中止），寧可少跑一輪，也不要在管理操作期間動真本。
    """
    from aistorage.admin.lock import maintenance_relpath, parse_maintenance

    read_text = getattr(deps.pins, "read_text", None)
    if not callable(read_text):
        # L（review-b1039a8）：PinStore protocol 要求 read_text。舊的 fake 缺這個
        # 方法時要立刻發現（測試會爆），不能默默當成「沒有維護中」。
        raise TypeError(
            "PinStore 缺少 read_text(relpath)：維護旗標無法檢查，拒絕執行"
            f"（{type(deps.pins).__name__}）"
        )

    try:
        raw = read_text(maintenance_relpath(cfg.repo))
    except AiStorageError as e:
        # 讀不到旗標本身（例如 pin repo 連不上）：fail-closed，中止這一輪
        report.aborted_at = "maintenance"
        report.code = type(e).__name__
        return True

    if raw is None:
        return False

    try:
        flag = parse_maintenance(raw)
    except Exception as e:  # noqa: BLE001 - 旗標損毀 fail-closed
        report.aborted_at = "maintenance"
        report.code = "flag_corrupt"
        report.maintenance = "flag_corrupt"
        _dump_traceback(report.run_id, "maintenance", e)
        return True

    report.maintenance = "active"
    report.maintenance_reason = flag.reason
    return True


def _build_publisher(
    cfg: CommitterConfig, deps: Deps, workdir: Path
) -> ReadViewPublisher | None:
    """決定第 13 步要用哪個發佈器。

    讀取視圖有設定（資料夾 ＋ manifest 檔）就用第 4 組的 `DriveReadViewPublisher`；
    沒設定就沿用 `deps.publisher`（第 3 組階段是 NullPublisher，之後可換別的實作）。
    換發佈器不影響介面：`Deps.publisher` 仍可注入別的實作（例如測試用），
    只要它接受 `agora_main_sha`／`run_rejections` 關鍵字。
    """
    if not (cfg.readview_folder_id and cfg.readview_manifest_file_id):
        return deps.publisher
    from aistorage.publish.publisher import DriveReadViewPublisher

    return DriveReadViewPublisher(
        deps.drive,
        folder_id=cfg.readview_folder_id or "",
        manifest_file_id=cfg.readview_manifest_file_id or "",
        converters=deps.converters,
        clock=deps.clock,
        workdir=workdir,
        rebuild_epoch=cfg.readview_rebuild_epoch,
    )


def _dump_traceback(run_id: str, step: str, exc: BaseException) -> str | None:
    """把 traceback 寫到本機除錯檔；Actions log 只印路徑與例外類別名稱。

    traceback 可能含有本機路徑或訊息內容，所以不印到 log（D2 的 log 規則）。
    """
    try:
        debug_dir = Path(os.environ.get(DEBUG_DIR_ENV) or DEFAULT_DEBUG_DIR)
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"run-{run_id}.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n=== run={run_id} step={step} ===\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return str(path)
    except Exception:
        return None


def _target_source(dec: Decision) -> str:
    """取出交接單／參考紀錄的目標 Session 所屬來源（`target_session_id` 的前半段）。

    交接單的接續點是以目標 Session 的原始紀錄驗證的，轉換器必須是那個來源的
    （review-g3e L／review-g3g M4）。取不到時回 `opencode`：evaluate 已用 sidecar
    schema 驗過 `target_session_id`，走不到這裡代表輸入異常，交由 apply 判為
    `invalid_format`，不要讓整輪中止。
    """
    sidecar = getattr(dec, "sidecar", None)
    body = sidecar.get("body") if isinstance(sidecar, dict) else None
    target_id = ""
    if isinstance(body, dict):
        target_id = str(body.get("target_session_id") or "")
    source = target_id.split(":", 1)[0] if ":" in target_id else ""
    return source or "opencode"


def step1_guard(cfg: CommitterConfig, clock: Clock) -> str:
    """第 1 步：guard 檢查。

    在 GitHub Actions 環境中：
    - 檢查 GITHUB_REF == 'refs/heads/main'
    - 透過 GitHub API (GITHUB_TOKEN) 查詢遠端 main HEAD sha
    - 檢查 GITHUB_SHA == remote main HEAD sha
    在非 GitHub Actions 環境（本機或單元測試）：
    - 跳過並回傳 'local'
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return "local"

    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref != "refs/heads/main":
        raise AbortRun("guard", "invalid_ref", f"GITHUB_REF is '{github_ref}', expected 'refs/heads/main'")

    github_sha = os.environ.get("GITHUB_SHA", "")
    github_repo = os.environ.get("GITHUB_REPOSITORY", cfg.github_repository or "")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not github_token:
        raise AbortRun("guard", "missing_token", "GITHUB_ACTIONS is true but GITHUB_TOKEN is not set")
    if not github_repo:
        raise AbortRun("guard", "missing_repo", "GITHUB_REPOSITORY is not set")

    url = f"https://api.github.com/repos/{github_repo}/commits/main"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "aistorage-committer",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            remote_sha = data.get("sha", "")
    except urllib.error.HTTPError as e:
        raise AbortRun("guard", "api_error", f"無法自 GitHub API 取得遠端 main HEAD (HTTP {e.code})") from e
    except Exception as e:
        raise AbortRun("guard", "api_error", f"無法自 GitHub API 取得遠端 main HEAD ({type(e).__name__})") from e

    if not remote_sha or remote_sha != github_sha:
        raise AbortRun(
            "guard",
            "sha_mismatch",
            f"GITHUB_SHA ({github_sha}) 與遠端 main HEAD ({remote_sha}) 不相符",
        )

    return "ok"


def prescan(cfg: CommitterConfig, deps: Deps) -> int:
    """提交流程預先掃描（第 2 步專用進入點）。

    stdout 只印形狀符合的收件匣項目數（workflow 以 `shaped=<N>` 寫進
    $GITHUB_OUTPUT，後續步驟用 `!= '0'` 決定要不要繼續）；空收件匣另外在
    stderr 印一行 EMPTY 供人閱讀。讀不到收件匣時直接 raise，不可吞掉錯誤。
    """
    scan = scan_inboxes(deps.drive, deps.registry)
    shaped = count_shaped(scan)
    if shaped == 0:
        print("EMPTY", file=sys.stderr)
    print(shaped)
    return shaped


def plan_sweep_cli(cfg: CommitterConfig, deps: Deps) -> list[SweepDecision]:
    """CLI plan-sweep: 僅執行第 3〜4 步判定並回傳處置計畫（唯讀，不寫入）。"""
    children = deps.drive.list_children(cfg.prefix_folder_id)
    files = tuple(f for f in children if not f.is_folder)
    subfolders = tuple(f for f in children if f.is_folder)
    repo_listing = RepoListing(
        prefix_folder_id=cfg.prefix_folder_id,
        files=files,
        subfolders=subfolders,
    )

    state, pending = deps.pins.load(cfg.repo)
    with tempfile.TemporaryDirectory(prefix="aistorage_plan_sweep_") as td:
        workdir = Path(td)
        _outcome, current_state = settle(
            state,
            pending,
            repo_listing,
            deps.drive,
            workdir=workdir,
            clock=deps.clock,
        )
        parent_decisions = []
        if cfg.prefix_levels:
            parent_decisions = check_parents(list(cfg.prefix_levels), deps.drive)
        sweep_decisions = plan_sweep(
            repo_listing,
            current_state,
            repo_uuid=cfg.repo_uuid,
            prefix_folder_id=cfg.prefix_folder_id,
        )
        sweep_decisions = resolve_content_checks(
            sweep_decisions,
            deps.drive,
            {},
            current_state,
            repo_uuid=cfg.repo_uuid,
            listing=repo_listing,
            prefix_folder_id=cfg.prefix_folder_id,
            workdir=workdir,
        )
        return parent_decisions + sweep_decisions


def init_pin_cli(
    cfg: CommitterConfig,
    deps: Deps,
    *,
    confirm: bool = False,
) -> PinState:
    """CLI init-pin: 首次建立正式釘選值（管理者身分，只有 confirm=True 會寫入）。"""
    children = deps.drive.list_children(cfg.prefix_folder_id)
    files = tuple(f for f in children if not f.is_folder)
    subfolders = tuple(f for f in children if f.is_folder)
    repo_listing = RepoListing(
        prefix_folder_id=cfg.prefix_folder_id,
        files=files,
        subfolders=subfolders,
    )

    manifest_name = f"GITMANIFEST--{cfg.repo_uuid}"
    m_files = deps.drive.find_by_name(cfg.prefix_folder_id, manifest_name)
    if len(m_files) != 1:
        raise AbortRun("init-pin", "invalid_manifest", f"找不到唯一之主 manifest: {manifest_name}")

    mf = m_files[0]
    m_data = deps.drive.download_bytes(mf.id, max_bytes=1024 * 1024)
    m_sha = hashlib.sha256(m_data).hexdigest().lower()
    parsed = parse_manifest(m_data, repo_uuid=cfg.repo_uuid)

    with tempfile.TemporaryDirectory(prefix="aistorage_init_pin_") as td:
        workdir = Path(td)
        from aistorage.integrity.settle import _download_and_replay

        replay_refs = _download_and_replay(
            parsed.active,
            cfg.repo_uuid,
            files,
            deps.drive,
            workdir,
        )

        # Clone 並與 ls-remote 比對，同時讀取 annex keys 集合 (H5)
        git_dir = workdir / "repo"
        git = deps.git_factory(git_dir)
        for op in ("ls_remote", "annex_keys_in", "local_refs"):
            if not hasattr(git, op):
                # H5：拿不到就 fail-closed。寫入空集合等同於宣告「遠端沒有任何
                # annex 物件」，promote 之後第一輪 sweep 就會把它們全部隔離。
                raise AbortRun(
                    "init-pin", "unsupported_git",
                    f"AnnexGit 不支援 {op}()，無法確認釘選值內容；中止",
                )
        remote_refs = git.ls_remote("origin")

        for b in ("main", "git-annex"):
            full_ref = f"refs/heads/{b}"
            if full_ref in replay_refs:
                if remote_refs.get(full_ref) != replay_refs[full_ref]:
                    raise AbortRun(
                        "init-pin",
                        "ref_mismatch",
                        f"重放之 {full_ref} ({replay_refs[full_ref]}) 與 ls-remote ({remote_refs.get(full_ref)}) 不符",
                    )

        # annex key 集合一定要讀到；讀不到就是中止，不是空集合
        annex_keys = git.annex_keys_in(cfg.repo_uuid)

    now_iso = format_rfc3339(deps.clock.now(), include_fraction=True)
    state = PinState(
        repo=cfg.repo,
        repo_uuid=cfg.repo_uuid,
        refs=replay_refs,
        manifest_sha256=m_sha,
        prev_manifest_sha256=None,
        active_bundles=parsed.active,
        removed_bundles=parsed.removed,
        annex_keys=annex_keys,
        promoted_at=now_iso,
        run_id="init-pin",
    )

    if confirm:
        deps.pins.promote(state)
        print(
            f"INIT_PIN_PROMOTED: repo={state.repo} manifest={m_sha[:8]} "
            f"active={len(state.active_bundles)} keys={len(state.annex_keys)}"
        )
    else:
        print(
            f"INIT_PIN_PLAN: repo={state.repo} manifest={m_sha[:8]} "
            f"active={len(state.active_bundles)} keys={len(state.annex_keys)} (dry-run)"
        )

    return state


@dataclass(frozen=True)
class RepoTarget:
    """一輪要處理的一個真本（Agora／Foundry）。

    欄位名稱刻意與 `CommitterConfig` 相同，讓 pipeline 內部的程式碼不必分叉；
    差別在 `element`（決定分派與發佈方式）與 `largefiles`（annex 收檔規則）。
    """

    element: str  # "agora" | "foundry"
    repo: str
    repo_uuid: str
    repo_url: str
    prefix_folder_id: str
    quarantine_folder_id: str
    readview_folder_id: str | None = None
    readview_manifest_file_id: str | None = None
    readview_rebuild_epoch: int = 0
    largefiles: str | None = None
    # 以下沿用 CommitterConfig 的名稱與預設值
    max_git_bundles: int = 20
    max_gc_per_run: int = 200
    max_raw_size: int = 52428800
    quarantine_retention_days: int = 7
    prefix_levels: tuple[PrefixLevel, ...] = ()
    identity_registry_path: str = ""
    pin_repo_url: str = ""

    @classmethod
    def from_config(cls, cfg: CommitterConfig) -> RepoTarget:
        """Agora：由上層設定檔欄位組出。"""
        return cls(
            element="agora",
            repo=cfg.repo,
            repo_uuid=cfg.repo_uuid,
            repo_url=cfg.repo_url,
            prefix_folder_id=cfg.prefix_folder_id,
            quarantine_folder_id=cfg.quarantine_folder_id,
            readview_folder_id=cfg.readview_folder_id,
            readview_manifest_file_id=cfg.readview_manifest_file_id,
            readview_rebuild_epoch=cfg.readview_rebuild_epoch,
            max_git_bundles=cfg.max_git_bundles,
            max_gc_per_run=cfg.max_gc_per_run,
            max_raw_size=cfg.max_raw_size,
            quarantine_retention_days=cfg.quarantine_retention_days,
            prefix_levels=cfg.prefix_levels,
            identity_registry_path=cfg.identity_registry_path,
            pin_repo_url=cfg.pin_repo_url,
        )

    @classmethod
    def from_repo_config(cls, cfg: CommitterConfig, rc: RepoConfig) -> RepoTarget:
        """Foundry（或之後的第三個 repo）：由 `repos` 區塊組出，繼承共用設定。"""
        return cls(
            element=rc.name,
            repo=rc.name,
            repo_uuid=rc.uuid,
            repo_url=rc.url,
            prefix_folder_id=rc.prefix_folder_id,
            quarantine_folder_id=rc.quarantine_folder_id,
            readview_folder_id=rc.readview_folder_id,
            readview_manifest_file_id=rc.readview_manifest_file_id,
            readview_rebuild_epoch=rc.readview_rebuild_epoch,
            largefiles=rc.largefiles,
            max_git_bundles=cfg.max_git_bundles,
            max_gc_per_run=cfg.max_gc_per_run,
            max_raw_size=cfg.max_raw_size,
            quarantine_retention_days=cfg.quarantine_retention_days,
            prefix_levels=cfg.prefix_levels,
            identity_registry_path=cfg.identity_registry_path,
            pin_repo_url=cfg.pin_repo_url,
        )


@dataclass
class RepoRunResult:
    """單一 repo 這一輪的結果（第 14 步清收件匣需要）。"""

    target: RepoTarget
    decisions: list[Decision]
    applied_accepted: list[Decision]
    store: Any
    state: PinState | None = None


@dataclass
class PipelineContext:
    """一輪共用的輸入與報告累加器（多 repo 時每個 repo 各自呼叫一次 pipeline）。"""

    cfg: CommitterConfig
    deps: Deps
    report: RunReport
    scan: Any
    run_id: str
    base_temp: Path
    dry_run: bool
    content_cache: dict[Any, Any]
    step: str = "pipeline"
    prefix: str = ""  #: 非 agora 的 repo 在計數／耗時的鍵前加前綴，避免互相覆蓋

    def bump(self, key: str, value: int) -> None:
        name = f"{self.prefix}{key}" if self.prefix else key
        self.report.counts[name] = self.report.counts.get(name, 0) + int(value)

    def time(self, key: str, ms: int) -> None:
        name = f"{self.prefix}{key}" if self.prefix else key
        self.report.durations_ms[name] = self.report.durations_ms.get(name, 0) + int(ms)

    def set_field(self, field: str, value: Any) -> None:
        name = f"{self.prefix}{field}" if self.prefix else field
        setattr(self.report, name, value)

    def items_for(self, target: RepoTarget) -> list[Any]:
        """這個 repo 該評估哪些收件匣項目（依型態分派）。"""
        out = []
        for item in self.scan.items:
            item_type = ""
            for sc in item.sidecars or ():
                if sc.name.endswith(".sidecar.json"):
                    item_type = _item_type_of(sc, self.deps.drive)
                    break
            if target.element == "foundry":
                if item_type == "artifact":
                    out.append(item)
            elif item_type != "artifact":
                out.append(item)
        return out


def _item_type_of(sidecar_file: Any, drive: DriveClient) -> str:
    """讀 sidecar 的 metadata.type（只為分派；壞掉就當未知，交給 evaluate 處理）。"""
    try:
        data = json.loads(
            drive.download_bytes(sidecar_file.id, max_bytes=1 << 20).decode("utf-8")
        )
        return str((data.get("metadata") or {}).get("type") or "")
    except Exception:
        return ""


def eval_store_for_ledger(store: Any, target: RepoTarget) -> Any:
    """Ledger 需要 `worktree` 與 `append_line`；FoundryStore 兩者都沒有同名方法。"""
    return store if target.element == "agora" else _FoundryEvaluateStore(store)


class _FoundryEvaluateStore:
    """給 evaluate 用的最小 store 介面（Foundry 的「既有紀錄」是產出目錄）。

    evaluate 只會讀 `store.get_record()` 與 `store.worktree`，Ledger 會用
    `store.append_line()`；這裡把 FoundryStore 轉成那幾個方法，避免把 Agora 的
    紀錄語意硬套到 Foundry 上。
    """

    def __init__(self, foundry_store: Any) -> None:
        self._store = foundry_store

    @property
    def worktree(self) -> Path:
        return self._store.worktree

    def get_record(self, item_id: str) -> dict[str, Any] | None:
        ulid = item_id.rsplit(":", 1)[-1]
        try:
            return self._store.get_catalog(ulid)
        except Exception:
            return None

    def append_line(self, rel_path: str, line: str) -> None:
        """Ledger 是 jsonl：直接附加到工作樹的檔案，並記為已變更路徑。"""
        dest = self._store.worktree / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "a", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")
        changed = getattr(self._store, "_changed_paths", None)
        if isinstance(changed, list):
            changed.append(rel_path)

    def put_json(self, rel_path: str, obj: Any) -> str:
        return self._store.put_json(rel_path, obj)


def _run_repo_pipeline(
    ctx: PipelineContext,
    target: RepoTarget,
    *,
    agora_store: Any = None,
) -> RepoRunResult:
    """第 3〜13 步：對單一真本跑一輪（settle → sweep → clone → apply → push → 轉正 → 發佈）。

    多 repo 時依序呼叫：Agora 先（Foundry 的 `produced_by_session_id` 檢查要讀 Agora
    的工作樹），Foundry 後。收件匣是共用的，依型態分派（見 `PipelineContext.items_for`）。
    """
    rcfg = target  # 欄位名稱與 CommitterConfig 相同，直接用
    work_temp = ctx.base_temp / f"work_{target.element}"
    store_temp = ctx.base_temp / f"store_{target.element}"
    work_temp.mkdir(parents=True, exist_ok=True)
    store_temp.mkdir(parents=True, exist_ok=True)
    git_dir = ctx.base_temp / f"repo_{target.element}"
    deps = ctx.deps
    report = ctx.report
    dry_run = ctx.dry_run
    run_id = ctx.run_id
    scan = ctx.scan
    content_cache = ctx.content_cache
    content_cache.clear()
    foundry_store = None
    # ---------------------------------------------------------
    # 第 3 步：integrity.settle 結算待定釘選值
    # ---------------------------------------------------------
    ctx.step = "integrity.settle"
    t0 = time.monotonic()
    children = deps.drive.list_children(rcfg.prefix_folder_id)
    files = tuple(f for f in children if not f.is_folder)
    subfolders = tuple(f for f in children if f.is_folder)
    repo_listing = RepoListing(
        prefix_folder_id=rcfg.prefix_folder_id,
        files=files,
        subfolders=subfolders,
    )

    state, pending = deps.pins.load(rcfg.repo)
    settle_outcome, settled_state = settle(
        state,
        pending,
        repo_listing,
        deps.drive,
        workdir=work_temp,
        clock=deps.clock,
    )

    if not dry_run:
        if settle_outcome == SettleOutcome.PROMOTED:
            deps.pins.promote(settled_state)
        elif settle_outcome in (SettleOutcome.DROPPED, SettleOutcome.BAK_RECOVERY):
            deps.pins.drop_pending(rcfg.repo)

    state = settled_state
    ctx.time("integrity.settle", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 4 步：integrity.sweep 清掃前綴資料夾
    # ---------------------------------------------------------
    ctx.step = "integrity.sweep"
    t0 = time.monotonic()
    parent_decisions: list[SweepDecision] = []
    if rcfg.prefix_levels:
        parent_decisions = check_parents(list(rcfg.prefix_levels), deps.drive)

    sweep_decisions = plan_sweep(
        repo_listing,
        state,
        repo_uuid=rcfg.repo_uuid,
        prefix_folder_id=rcfg.prefix_folder_id,
    )

    sweep_decisions = resolve_content_checks(
        sweep_decisions,
        deps.drive,
        content_cache,
        state,
        repo_uuid=rcfg.repo_uuid,
        listing=repo_listing,
        prefix_folder_id=rcfg.prefix_folder_id,
        workdir=work_temp,
    )

    readview_decisions: list[SweepDecision] = []
    if not rcfg.readview_folder_id:
        ctx.set_field("readview_sweep", "skipped_no_folder")
    elif not rcfg.readview_manifest_file_id:
        # 管理者還沒初始化讀取視圖發佈：沒有可信集合，動它等於把整個讀取視圖
        # 隔離掉（g3a M2）。跳過，等有 manifest 再說。
        ctx.set_field("readview_sweep", "skipped_no_manifest")
    else:
        # 可信集合＝讀取視圖 manifest 列出的 file id（4.3）。讀不到或損毀
        # → MismatchError，fail-closed 讓整輪中止，不猜。
        rv_manifest = load_manifest(deps.drive, rcfg.readview_manifest_file_id)
        if rv_manifest.is_initial:
            # generation=0：還沒發佈過，讀取視圖資料夾裡的東西都還沒被 manifest 記錄
            ctx.set_field("readview_sweep", "skipped_initial")
        else:
            rv_children = deps.drive.list_children(rcfg.readview_folder_id)
            rv_listing = RepoListing(
                prefix_folder_id=rcfg.readview_folder_id,
                files=tuple(f for f in rv_children if not f.is_folder),
                subfolders=tuple(f for f in rv_children if f.is_folder),
            )
            readview_decisions = plan_readview_sweep(
                rv_listing,
                trusted_ids(rv_manifest, rcfg.readview_manifest_file_id),
                readview_folder_id=rcfg.readview_folder_id,
            )
            ctx.set_field("readview_sweep", "swept")
    all_sweep_decisions = parent_decisions + sweep_decisions + readview_decisions
    moved_count = apply_sweep(
        all_sweep_decisions,
        deps.drive,
        quarantine_folder_id=rcfg.quarantine_folder_id,
        clock=deps.clock,
        prefix_folder_id=rcfg.prefix_folder_id,
        dry_run=dry_run,
    )
    ctx.bump("quarantined_files", moved_count)
    ctx.time("integrity.sweep", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 5 步：annex.git.clone + verify_clone
    # ---------------------------------------------------------
    ctx.step = "annex.git.clone"
    t0 = time.monotonic()
    git = deps.git_factory(git_dir)
    # 覆蓋率檢查：clone 出來的遠端必須至少涵蓋釘選值記錄的每一個 key。
    # 之前這裡沒有傳 expected_annex_keys，verify_annex_coverage 拿到的是空集合，
    # 檢查形同虛設（review-g7-e2e A-M1）。
    verify_clone(
        git,
        state,
        drive=deps.drive,
        prefix_folder_id=rcfg.prefix_folder_id,
    )
    # H1（review-b1039a8）：第 5 步的覆蓋率要比對**遠端實況**。
    # 原本傳 `expected_annex_keys=state.annex_keys` 進 verify_clone，而它內部是
    # `verify_annex_coverage(state.annex_keys, expected)`——拿同一個集合跟自己比，
    # 恆真。這裡改成直接檢查：遠端的 key 集合（clone 下來的 git-annex location log）
    # 必須涵蓋釘選值記錄的每一個 key；少一個就代表 pin 與遠端不一致，中止。
    if hasattr(git, "annex_keys_in"):
        remote_keys = git.annex_keys_in(state.repo_uuid)
        missing_remote = set(state.annex_keys) - set(remote_keys)
        if missing_remote:
            raise MismatchError(
                "clone 後遠端 annex key 集合缺少釘選值記載之物件: "
                f"{sorted(missing_remote)}（pin 與 Drive 的 location log 不一致）"
            )
    ctx.time("annex.git.clone", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 6 步：annex.git 環境確認
    # ---------------------------------------------------------
    ctx.step = "annex.git"
    t0 = time.monotonic()
    # 若為 SubprocessAnnexGit，已於 clone_for_commit 設定 annex.max-git-bundles
    ctx.time("annex.git", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 7 步：intake.evaluate + agora.apply
    # ---------------------------------------------------------
    ctx.step = "intake.evaluate"
    t0 = time.monotonic()

    if target.element == "foundry":
        # Foundry 有自己的 store（佈局、annex 規則都不同，review F-H2）
        from aistorage.foundry.store import FoundryStore

        foundry_store = FoundryStore(
            git_dir,
            git=git,
            temp_dir=store_temp,
            largefiles=target.largefiles,
            # 測試注入 raw_storage_factory 時 git 是假的，設定不了 annex
            configure_annex=deps.raw_storage_factory is None,
        )
        store = foundry_store
    else:
        # 2.6 決策：原始紀錄與閱讀版放 git-annex 物件庫（keys 由 git annex
        # lookupkey 產生，見 AnnexRawStorage）。測試仍可注入 raw_storage_factory。
        if deps.raw_storage_factory:
            raw_storage = deps.raw_storage_factory(git_dir, git)
        elif isinstance(git, FakeAnnexGit):
            raw_storage = FakeRawStorage()
        else:
            raw_storage = AnnexRawStorage(git_dir, git=git)

        store = AgoraStore(
            worktree=git_dir,
            raw_storage=raw_storage,
            git=git,
            temp_dir=store_temp,
        )
    ledger = Ledger(eval_store_for_ledger(store, target))
    # 這一輪「之前」真本已經記著的 key（H1：pending 只放 find 的結果 ＋ 這一輪
    # 新增的 key，不要把 snapshots 的全部歷史 key 聯集進來——那些 key 如果遠端
    # 真的沒有，本來就是警訊，會被第 10 步的遠端實況比對抓出來）
    keys_before = store.annex_keys()

    # 評估所有收件匣項目
    # 收件匣共用、依型態分派：Agora 收 session/handoff/claim/reference，
    # Foundry 收 artifact（group5-7 第 6.1 節）。
    # evaluate 對 Foundry 只需要「既有紀錄」的讀取介面
    eval_store = _FoundryEvaluateStore(store) if target.element != "agora" else store
    decisions: list[Decision] = []
    for item in ctx.items_for(target):
        dec = evaluate(
            item,
            drive=deps.drive,
            registry=deps.registry,
            store=eval_store,
            ledger=ledger,
            clock=deps.clock,
            workdir=work_temp,
            max_raw=rcfg.max_raw_size,
            foundry_enabled=target.element == "foundry",
            foundry_store=foundry_store,
        )
        decisions.append(dec)

    accepted = [d for d in decisions if d.kind == DecisionKind.ACCEPT]
    sorted_accepted = sort_accepted_decisions(accepted)

    # 依型態順序套用至真本
    applied_accepted: list[Decision] = []
    now_iso = format_rfc3339(deps.clock.now(), include_fraction=True)

    for dec in sorted_accepted:
        item_type = dec.record_metadata.get("type") if dec.record_metadata else ""
        if item_type == "artifact":
            if foundry_store is None:
                # 沒有 Foundry 設定時 evaluate 已 REJECT(foundry_not_enabled)，走不到這裡
                continue
            # 產生者檢查要讀 Agora 的工作樹，所以必須傳 agora_store
            res = apply_artifact(foundry_store, dec, agora_store, deps.clock)
        elif item_type == "session":
            source = dec.sidecar["session"]["source"] if dec.sidecar else "opencode"
            conv = deps.converters.get(source) or get_converter(source)
            res = apply_session(store, dec, conv, deps.clock)
        elif item_type == "handoff":
            # 依目標 Session 的 source 選轉換器（review-g3e L／g3g M4）：
            # 目標是 Claude Code 的 Session 時不能拿 opencode 的轉換器去驗接續點。
            target_source = _target_source(dec)
            conv = deps.converters.get(target_source) or get_converter(target_source)
            res = apply_handoff(store, dec, conv, deps.clock)
        elif item_type == "claim":
            res = apply_claim(store, dec, deps.clock)
        elif item_type == "reference":
            res = apply_reference(store, dec, deps.clock)
        else:
            # 改寫：evaluate 在驗章之後、下載 raw 之前就 REJECT(rewrite_disabled)，
            # 走不到這裡（期 1 不提供改寫，見 PM 決定）。留在這裡只是不讓
            # 未知的型態被靜默當成已套用。
            continue

        item_id = dec.record_metadata.get("id", "") if dec.record_metadata else ""
        raw_sha = (dec.sidecar.get("raw") or {}).get("sha256") if dec.sidecar else None

        if not res.ok:
            # apply 判定失敗轉為 REJECT（拒絕記錄已由 apply 模組寫入）
            if dec.authenticated:
                ledger.record(
                    dec.item.item_key,
                    item_id=item_id,
                    decision=res.code,
                    raw_sha256=raw_sha,
                    at=now_iso,
                )
        else:
            applied_accepted.append(dec)
            ledger.record(
                dec.item.item_key,
                item_id=item_id,
                decision="ok",
                raw_sha256=raw_sha,
                at=now_iso,
            )

    # 記錄 ALREADY 與經認證的 REJECT 至清冊
    for dec in decisions:
        item_id = dec.record_metadata.get("id", "") if dec.record_metadata else ""
        raw_sha = (dec.sidecar.get("raw") or {}).get("sha256") if dec.sidecar else None
        if dec.kind == DecisionKind.ALREADY:
            ledger.record(
                dec.item.item_key,
                item_id=item_id,
                decision="already",
                raw_sha256=raw_sha,
                at=now_iso,
            )
        elif dec.kind == DecisionKind.REJECT and dec.authenticated:
            ledger.record(
                dec.item.item_key,
                item_id=item_id,
                decision=dec.code,
                raw_sha256=raw_sha,
                at=now_iso,
            )

    # Git commit 變更
    changed_paths = store.changed_paths()
    if changed_paths:
        git.add(changed_paths)
        # D2 規則：commit message 只記錄計數與 run_id，不記錄標題與內容
        git.commit(f"committer: batch processed ({len(applied_accepted)} accepted, run={run_id})")

    ctx.bump("accepted", len(applied_accepted))
    ctx.bump("rejected", sum(1 for d in decisions if d.kind == DecisionKind.REJECT))
    ctx.bump("already", sum(1 for d in decisions if d.kind == DecisionKind.ALREADY))
    ctx.bump("deferred", sum(1 for d in decisions if d.kind == DecisionKind.DEFER))
    ctx.time("intake.evaluate", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 8 步：annex.git.copy（先上傳物件）+ 計算 refs／annex keys
    # ---------------------------------------------------------
    ctx.step = "annex.git.copy"
    t0 = time.monotonic()
    # H1：`git annex copy` 會改寫本機的 git-annex 分支（location log），
    # 並讓新的 key 變成「在 remote 上」。所以必須在算 refs 與 key 集合
    # **之前**執行，否則 pending 記錄的是 copy 之前的狀態：
    #   1. push 出去的 git-annex ref 與 pending 不符 → verify_after_push 中止；
    #   2. 下一輪 settle 時遠端既不等於 pending 也不等於正式值 → MismatchError，
    #      之後每一輪都中止，需要人工重建 pin；
    #   3. pending 的 annex_keys 少了新上傳的 key → promote 之後下一輪 sweep
    #      會把新上傳的物件全部隔離。
    # 中止時這些物件不在釘選值裡，下一輪會被隔離，是安全的方向。
    # M1：dry-run 不得寫入遠端。
    if not dry_run:
        git.copy("origin")
    ctx.time("annex.git.copy", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 9 步：pins.write_pending
    # ---------------------------------------------------------
    ctx.step = "pins.write_pending"
    t0 = time.monotonic()
    # refs 與 annex key 集合取自 AnnexGit 自己的公開方法；缺少必要分支
    # 由 SubprocessAnnexGit.local_refs() raise，不在這裡吞掉例外。
    local_refs = git.local_refs()
    # 遠端此刻「看得見」的 key（`git annex find --in=…`）**還不含**這一輪
    # 剛 copy 上去的物件：那筆 location log 要等 git-annex 分支被 push
    # 之後才進得去（實測）。所以要把 store 記錄的 key（全部來自
    # `git annex lookupkey`）聯集進來——pending 要記的是「這輪 push 之後
    # 遠端會有什麼」，第 10 步的 verify 才會真的驗到有沒有推上去。
    # 只靠 find 的話 pending 會少記新 key，下一輪 sweep 就把它們隔離
    # （H2 的第 3 點）。
    annex_keys = (
        git.annex_keys_in(state.repo_uuid)
        if hasattr(git, "annex_keys_in")
        else frozenset(state.annex_keys)
    ) | (store.annex_keys() - keys_before)

    has_git_changes = (local_refs != state.refs) or (annex_keys != state.annex_keys)

    if has_git_changes:
        pending = PinPending(
            repo=rcfg.repo,
            base_manifest_sha256=state.manifest_sha256,
            refs=local_refs,
            annex_keys=annex_keys,
            written_at=format_rfc3339(deps.clock.now(), include_fraction=True),
            run_id=run_id,
        )
        if not dry_run:
            deps.pins.write_pending(pending)

    ctx.time("pins.write_pending", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 10 步：verify.precheck + git.push
    # ---------------------------------------------------------
    ctx.step = "git.push"
    t0 = time.monotonic()
    push_started_at = deps.clock.now()

    if has_git_changes:
        precheck(
            deps.drive,
            rcfg.prefix_folder_id,
            f"GITMANIFEST--{state.repo_uuid}",
            state,
        )
        if not dry_run:
            git.push("origin", ("main", "git-annex"))

    ctx.time("git.push", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 11 步：verify.verify_after_push
    # ---------------------------------------------------------
    ctx.step = "verify.verify_after_push"
    t0 = time.monotonic()
    push_verification = None

    if has_git_changes and not dry_run:
        # H1（review-b1039a8）：覆蓋率檢查要比對**遠端實況**，不是自己跟自己比。
        #   - 必要 key＝這一輪新寫進去的 key（`store.annex_keys()` 減掉這一輪之前的），
        #     全部來自 `git annex lookupkey`；
        #   - 比較對象＝push 之後**重新**向遠端查一次 `git annex find --in=<uuid>`，
        #     因為 location log 是在 push 之後才進得去（實測）。
        # 這樣「物件沒有真的上到 Drive」會在第 10 步就被擋住，而不是等到某天
        # 交接單驗證快照時才爆。
        new_keys = store.annex_keys() - keys_before
        remote_keys_after_push = (
            git.annex_keys_in(state.repo_uuid)
            if hasattr(git, "annex_keys_in")
            else annex_keys
        )
        push_verification = verify_after_push(
            git,
            deps.drive,
            repo_listing,
            state,
            local_refs,
            push_started_at,
            workdir=work_temp,
            expected_annex_keys=new_keys,
            pushed_annex_keys=remote_keys_after_push,
        )

    ctx.time("verify.verify_after_push", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 12 步：pins.promote + gc.gc_removed
    # ---------------------------------------------------------
    ctx.step = "pins.promote"
    t0 = time.monotonic()

    # H2（review-b1039a8）：管理者是在這一輪跑到一半才上鎖的情況，也要擋。
    # 在寫入 pin 之前再查一次：有旗標就中止（pending 留著，下一輪由 settle 結算），
    # 絕對不要在管理操作進行中 promote。
    if _check_maintenance(rcfg, deps, report):
        return RepoRunResult(
            target=target, decisions=decisions, applied_accepted=applied_accepted, store=store
        )

    if has_git_changes and not dry_run and push_verification is not None:
        new_state = PinState(
            repo=rcfg.repo,
            repo_uuid=state.repo_uuid,
            refs=local_refs,
            manifest_sha256=push_verification.new_manifest_sha256,
            prev_manifest_sha256=state.manifest_sha256,
            active_bundles=push_verification.active,
            removed_bundles=push_verification.removed,
            annex_keys=annex_keys,
            promoted_at=format_rfc3339(deps.clock.now()),
            run_id=run_id,
        )
        deps.pins.promote(new_state)
        state = new_state

    # 回收 removed bundle
    removed_candidates = collect_removed_bundles(repo_listing, state)
    gc_count = gc_removed(
        removed_candidates,
        deps.drive,
        prefix_folder_id=rcfg.prefix_folder_id,
        state=state,
        max_delete=rcfg.max_gc_per_run,
        dry_run=dry_run,
    )
    ctx.bump("gc_deleted", gc_count)

    # 清理過期隔離檔案 (7天)
    purge_count = purge_quarantine(
        deps.drive,
        rcfg.quarantine_folder_id,
        older_than_days=rcfg.quarantine_retention_days,
        now=deps.clock.now(),
        max_delete=rcfg.max_gc_per_run,
        dry_run=dry_run,
    )
    ctx.bump("quarantine_purged", purge_count)
    ctx.time("pins.promote", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 13 步：publisher.publish（讀取視圖 4.1）
    # ---------------------------------------------------------
    # 發佈失敗只標記、不中止：真本已經轉正（第 12 步），收件匣照常清理
    # （第 14 步）。讀取視圖是衍生物，下一輪補發即可（4.5 prescan）。
    ctx.step = "publisher.publish"
    t0 = time.monotonic()
    try:
        if target.element == "foundry":
            ctx.set_field(
                "readview_publish",
                _publish_foundry(target, deps, foundry_store, state, work_temp, dry_run),
            )
        else:
            pub = _build_publisher(rcfg, deps, work_temp)
            if pub is None:
                ctx.set_field("readview_publish", "skipped_no_readview")
            else:
                pub_rep = pub.publish(
                    store,
                    agora_main_sha=local_refs.get("refs/heads/main", ""),
                    run_rejections=collect_rejections(store, decisions),
                    dry_run=dry_run,
                )
                ctx.set_field("readview_publish", getattr(pub_rep, "status", "published"))
    except Exception as e:  # noqa: BLE001 - 發佈失敗不得影響真本與收件匣
        ctx.set_field("readview_publish", "publish_failed")
        ctx.set_field("publish_error", type(e).__name__)
        _dump_traceback(run_id, "publisher.publish", e)
    ctx.time("publisher.publish", int((time.monotonic() - t0) * 1000))

    return RepoRunResult(
        target=target,
        decisions=decisions,
        applied_accepted=applied_accepted,
        store=store,
        state=state,
    )


def run(cfg: CommitterConfig, deps: Deps, *, dry_run: bool = False) -> RunReport:
    """執行 13 步提交流程主體。

    各步驟（review-g3g H1：copy 必須在算 refs／annex keys 之前，所以它獨佔一步，
    後面的步驟順延；實際執行順序見下方註解）：
    1 guard: ref 與 sha 驗證（Actions 環境）
    2 intake.scan: 掃描收件匣，空則提前結束
    3 integrity.settle: 結算待定釘選值
    4 integrity.sweep: 清掃前綴資料夾與上層同名資料夾
    5 annex.git.clone + integrity.verify.verify_clone: clone 真本並核對釘選值
    6 annex.git: 準備 git annex 環境
    7 intake.evaluate + agora.apply: 評估決策並套用至真本
    8 annex.git.copy: 上傳 annex 物件（必須早於計算 refs／keys）
    9 pins.write_pending: 寫入待定釘選值
    10 verify.precheck + git.push: 預檢並推送
    11 verify.verify_after_push: push 後遠端狀態驗證
    12 pins.promote + gc.gc_removed: 轉正釘選值與 bundle 回收
    13 publisher.publish: 發佈讀取視圖
    14 clean_inbox: 刪除已處理之收件匣項目與逾時的 junk
    """
    run_id = generate_ulid()
    report = RunReport(run_id=run_id)
    content_cache: dict[Any, Any] = {}

    with tempfile.TemporaryDirectory(prefix="aistorage_run_") as temp_dir_str:
        base_temp = Path(temp_dir_str)
        git_dir = base_temp / "repo"
        work_temp = base_temp / "work"
        store_temp = base_temp / "store_tmp"
        work_temp.mkdir(parents=True, exist_ok=True)
        store_temp.mkdir(parents=True, exist_ok=True)

        current_step = "guard"
        ctx: PipelineContext | None = None
        try:
            # ---------------------------------------------------------
            # 第 1 步：guard 檢查
            # ---------------------------------------------------------
            t0 = time.monotonic()
            guard_status = step1_guard(cfg, deps.clock)
            report.guard = guard_status
            report.durations_ms["guard"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 1b 步：維護旗標（H4／PM 決定 7）
            # ---------------------------------------------------------
            # 管理操作（抹除、回滾）進行中時，提交流程必須**整輪不做任何事**：
            # 不清扫、不 push、不發佈、不刪收件匣。沒有這一步，住民重新啟用
            # workflow 觸發就會和管理操作撞在一起。
            # 旗標內容損毀 → 當成維護中（fail-closed）並中止，不猜。
            current_step = "maintenance"
            if _check_maintenance(cfg, deps, report):
                report.counts["scanned_items"] = 0
                return report

            # ---------------------------------------------------------
            # 第 2 步：intake.scan 掃描收件匣
            # ---------------------------------------------------------
            current_step = "intake.scan"
            t0 = time.monotonic()
            scan = scan_inboxes(deps.drive, deps.registry)
            shaped_count = count_shaped(scan)
            report.counts["scanned_items"] = len(scan.items)
            report.counts["shaped_items"] = shaped_count
            report.counts["junk_files"] = len(scan.junk)
            report.durations_ms["intake.scan"] = int((time.monotonic() - t0) * 1000)

            if shaped_count == 0:
                # 收件匣為空（無符合形狀之項目），提早結束，不 clone 真本
                print(report.format_log())
                return report

            # ---------------------------------------------------------
            # 第 3〜13 步：逐 repo 跑 pipeline（Agora → Foundry）
            # ---------------------------------------------------------
            # PM 決定 9：只有收件匣真的有 artifact 時才 clone Foundry，平常成本不變。
            targets = [RepoTarget.from_config(cfg)]
            if cfg.repos:
                has_artifact = any(
                    _item_type_of(sc, deps.drive) == "artifact"
                    for item in scan.items
                    for sc in (item.sidecars or ())
                    if sc.name.endswith(".sidecar.json")
                )
                if has_artifact:
                    for rc in cfg.repos:
                        targets.append(RepoTarget.from_repo_config(cfg, rc))
                else:
                    report.foundry_skipped = "no_artifacts"

            ctx = PipelineContext(  # type: ignore[assignment]
                cfg=cfg,
                deps=deps,
                report=report,
                scan=scan,
                run_id=run_id,
                base_temp=base_temp,
                dry_run=dry_run,
                content_cache=content_cache,
            )

            results: list[RepoRunResult] = []
            agora_store = None
            for target in targets:
                ctx.prefix = "" if target.element == "agora" else f"{target.element}."
                result = _run_repo_pipeline(
                    ctx, target, agora_store=agora_store
                )
                results.append(result)
                if target.element == "agora":
                    agora_store = result.store
                current_step = ctx.step

            # 第 14 步要用的 decisions／applied 是「所有 repo 的聯集」
            decisions = [d for r in results for d in r.decisions]
            applied_accepted = [d for r in results for d in r.applied_accepted]

            # ---------------------------------------------------------
            # 第 14 步：clean_inbox 刪除收件匣檔案
            # ---------------------------------------------------------
            current_step = "clean_inbox"
            if ctx is not None:
                ctx.step = "clean_inbox"
            t0 = time.monotonic()
            # M5：這一輪的讀取視圖有沒有真的發佈？只有真的發佈了，拒收原因才會
            # 進入某個世代，刪掉才安全（見下面 REJECT 的分支）。
            publish_succeeded = report.readview_publish == "published"
            deleted_inbox_count = 0
            failed_delete_count = 0
            inbox_folders = set(deps.registry.inbox_folders().keys())
            now_dt = deps.clock.now()

            # 挑選符合刪除條件的決策項目
            for dec in decisions:
                should_delete = False
                if dec.kind == DecisionKind.ACCEPT and dec in applied_accepted:
                    should_delete = True
                elif dec.kind == DecisionKind.ALREADY:
                    should_delete = True
                elif dec.kind == DecisionKind.REJECT:
                    # M5（review-b1039a8）：拒收原因要等「已經被發佈出去」才可以刪。
                    # 這一輪的讀取視圖發佈失敗（publish_failed／skipped）時，拒收原因
                    # 還沒有任何讀者看得到，這時刪掉等於讓寫入者永遠不知道為什麼被拒。
                    # publish_fail 的輪次一律保留，等下一次真的發佈成功再刪。
                    if publish_succeeded and dec.deletable_after is not None \
                            and now_dt >= dec.deletable_after:
                        should_delete = True

                if should_delete:
                    # 匯總該項目所有的候選檔案
                    item_files: list[DriveFile] = []
                    item_files.extend(dec.item.sidecars)
                    item_files.extend(dec.item.sigs)
                    item_files.extend(dec.item.raws)
                    item_files.extend(dec.item.extras)

                    for f in item_files:
                        # 防呆安全檢查：確認 parents 包含合法收件匣資料夾
                        try:
                            df = deps.drive.get(f.id)
                        except (ReadError, NotFound):
                            continue

                        is_inbox = any(p in inbox_folders for p in df.parents)
                        if not is_inbox:
                            raise MismatchError(
                                f"收件匣清理防呆檢查失敗：檔案 {df.id} ({df.name}) 之 parents 不屬於合法收件匣資料夾"
                            )

                        if not dry_run:
                            try:
                                deps.drive.delete_permanently(df.id)
                                deleted_inbox_count += 1
                            except Exception:
                                # M5：刪除失敗不可靜默吞掉，計數回報讓 6.3 的監控看得到
                                failed_delete_count += 1
                        else:
                            deleted_inbox_count += 1

            # M5：junk（名稱不符合格式的檔案、收件匣裡的資料夾）超過 24 小時
            # 就刪除並永久刪除（D2／review-g3d M4），從前不會被刪，收件匣因此
            # 永遠不是空的。刪除前一樣做 parents 防呆檢查。
            junk_deleted = 0
            for f in scan.junk:
                try:
                    df = deps.drive.get(f.id)
                except (ReadError, NotFound):
                    continue
                if not any(p in inbox_folders for p in df.parents):
                    raise MismatchError(
                        f"junk 清理防呆檢查失敗：檔案 {df.id} ({df.name}) 之 parents 不屬於合法收件匣資料夾"
                    )
                if (now_dt - df.created_at) < JUNK_RETENTION:
                    continue  # 還沒滿 24 小時，留到下一輪
                if not dry_run:
                    try:
                        deps.drive.delete_permanently(df.id)
                        junk_deleted += 1
                    except Exception:
                        failed_delete_count += 1
                else:
                    junk_deleted += 1

            report.counts["inbox_deleted"] = deleted_inbox_count
            report.counts["inbox_junk_deleted"] = junk_deleted
            report.counts["inbox_delete_failed"] = failed_delete_count
            report.durations_ms["clean_inbox"] = int((time.monotonic() - t0) * 1000)

        except AbortRun as e:
            report.aborted_at = e.step
            report.code = e.code
        except AiStorageError as e:
            step = _pipeline_step(current_step, ctx)
            report.aborted_at = step
            report.code = type(e).__name__
            _dump_traceback(run_id, step, e)
        except Exception as e:
            step = _pipeline_step(current_step, ctx)
            report.aborted_at = step
            report.code = type(e).__name__
            _dump_traceback(run_id, step, e)

    print(report.format_log())
    return report

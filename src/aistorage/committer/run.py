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
from aistorage.annex.git import DEFAULT_LARGEFILES, AnnexGit, SubprocessAnnexGit
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
    verify_new_keys_on_drive,
    verify_pin_keys_on_drive,
)
from aistorage.intake.evaluate import (
    FOUNDRY_ITEM_TYPES,
    Decision,
    DecisionKind,
    VerifiedSidecar,
    evaluate,
    sort_accepted_decisions,
    verify_item_sidecar,
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
    #: H1（review-25a48a9）：factory **必須**拿到 target。
    #: 舊簽名只給 `dest`，於是 URL 寫死成 Agora 的——Foundry 的 pipeline 會
    #: clone 到 Agora 的 repo（第 3、4 步用 Foundry 的 pin，第 5 步卻拿到 Agora
    #: 的 repo，最壞情況是把 Foundry 的內容寫進 Agora 並 push）。
    git_factory: Callable[[Path, "RepoTarget"], AnnexGit]
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
    #: M2：設定檔有列、但釘選值還沒初始化（= 沒有設定）的 repo 名稱
    repo_not_enabled: str | None = None
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


# H5：Foundry 的讀取視圖發佈已改用 `aistorage.publish.foundry.FoundryReadViewPublisher`
# （與 Agora 同一套 manifest 機制、固定 id 原地更新、世代、退役檔輪替）。
# 舊的 `_publish_foundry` 會每次 `drive.create` 一個新 index 檔、卻不更新 manifest，
# 而且回報 published 但實際上一件都沒發佈——已刪除。


def _pipeline_step(current_step: str, ctx: PipelineContext | None) -> str:
    """多 repo 時，中止點要標成「哪個 repo 的哪一步」。

    pipeline 內部用 `ctx.step` 記錄進度（RepoPipeline 只能看到自己的 ctx），
    所以例外發生時以它為準；還沒進 pipeline 就失敗時退回外層的 current_step。
    """
    if ctx is not None and ctx.step and ctx.step != "pipeline":
        return ctx.step if not ctx.prefix else f"{ctx.prefix}{ctx.step}"
    return current_step


def _check_maintenance(
    cfg: CommitterConfig | RepoTarget, deps: Deps, report: RunReport
) -> bool:
    """檢查 pin repo 的維護旗標；**整輪**不做任何事。

    回傳 True 表示「因為維護中而結束」。旗標讀不到或損毀時 fail-closed：
    把它當成維護中（中止），寧可少跑一輪，也不要在管理操作期間動真本。

    H3（review-25a48a9）：參數可以是 `RepoTarget`（pipeline 內的重查，查的是
    該 target 自己的旗標，例如 `foundry.maintenance`）。讀不到 `read_text` 一律
    raise `TypeError`——不能默默當成「沒有維護中」。
    """
    from aistorage.admin.lock import maintenance_relpath, parse_maintenance

    repo = cfg.repo
    read_text = getattr(deps.pins, "read_text", None)
    if not callable(read_text):
        # L（review-b1039a8）：PinStore protocol 要求 read_text。舊的 fake 缺這個
        # 方法時要立刻發現（測試會爆），不能默默當成「沒有維護中」。
        raise TypeError(
            "PinStore 缺少 read_text(relpath)：維護旗標無法檢查，拒絕執行"
            f"（{type(deps.pins).__name__}）"
        )

    try:
        raw = read_text(maintenance_relpath(repo))
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


def _maintenance_repo_names(cfg: CommitterConfig) -> tuple[str, ...]:
    """這一輪會碰到的所有 repo 名稱（H3：1b 要檢查**每一個** target 的旗標）。"""
    return (cfg.repo, *(rc.name for rc in cfg.repos))


def _assert_no_maintenance(
    repo_name: str, deps: Deps, report: RunReport, step: str
) -> None:
    """pipeline 內的重查（H3）：命中就讓**整輪**停下來。

    H2 指出第 1b 步只查 Agora，Foundry 的 settle／sweep／clone／push 全部照跑；
    原本 promote 前的重查又在 push 之後，命中之後還會繼續跑第 14 步——那一輪
    push 出去但還沒 promote 的項目，會被從收件匣刪掉（交接單、認領是一次性的，
    刪了就永久遺失）。所以：

    - 重查點放在 **write_pending 之前**與 **push 之前**（push 最怕撞上抹除）；
    - 命中一律 `raise AbortRun("maintenance", "active")`，讓 `run()` 走
      `except AbortRun`：不處理下一個 repo，也不執行第 14 步。
    """
    probe = _ProbeRunReport()
    if not _check_maintenance(_RepoNameShim(repo_name), deps, probe):  # type: ignore[arg-type]
        return
    report.maintenance = probe.maintenance or "active"
    report.maintenance_reason = probe.maintenance_reason
    raise AbortRun("maintenance", "active", f"{repo_name} 正在維護中（{step}）")


class _RepoNameShim:
    """只帶 `repo` 的輕量物件（重查只需要 repo 名稱）。"""

    def __init__(self, repo: str) -> None:
        self.repo = repo


class _ProbeRunReport:
    """重查用的報告殼（只為了不污染這一輪的 RunReport，再由呼叫端轉記）。"""

    run_id: str = "maintenance-probe"
    aborted_at: str | None = None
    code: str | None = None
    maintenance: str | None = None
    maintenance_reason: str | None = None


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
    target: RepoTarget | None = None,
) -> PinState:
    """CLI init-pin: 首次建立正式釘選值（管理者身分，只有 confirm=True 會寫入）。

    `target` 給定時用它取代 cfg 的各欄位（H1：Foundry 的釘選值必須用**自己的**
    repo_url／uuid 來 clone 與驗證；共用 Agora 的 URL 就是 H1 描述的最壞情況）。
    """
    if target is not None:
        cfg = _target_as_config(cfg, target)
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
        # H1：URL 與 largefiles 從 target 來（target=None 時就是 cfg 自己）
        git = deps.git_factory(git_dir, target or RepoTarget.from_config(cfg))
        # H1：clone 之後確認身分就是這個 repo（錯了就中止，不要拿錯的 pin 建釘選值）
        verify_clone_identity(git, target or RepoTarget.from_config(cfg))
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

        # H1（review-cdb4a34，會遺失資料）：釘選值的 annex key 集合要涵蓋
        # **整棵樹狀**。
        #
        # 原本只取 `annex_keys_in(uuid)`（location log），而 store 也不知道
        # reading／meta 這類被 `include=*.json` 規則收進 annex 的檔案。少記的
        # 後果：promote 之後第一輪 sweep 就把它們隔離，資料沒了。
        # 所以這裡取聯集：
        #   - `store.annex_keys()`：樹狀的 snapshots 歷史（真正的鍵值來源是
        #     `git annex lookupkey`，見 AnnexRawStorage.keys 的說明）；
        #   - `annex_keys_in(uuid)`：location log 的觀點（涵蓋 store 不知道的
        #     那一類）。
        # 並且**每一個** key 都要通過 Drive 實況檢查（verify_pin_keys_on_drive）：
        # 缺一個就不建立釘選值——寧可不要有，也不要有一份會被隔離的釘選值。
        store_keys = _store_annex_keys(git_dir, git, target)
        annex_keys = frozenset(store_keys) | frozenset(
            git.annex_keys_in(cfg.repo_uuid))
        _assert_keys_on_drive_for_init_pin(
            deps.drive, cfg.prefix_folder_id, cfg.repo_uuid, annex_keys)

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

    @property
    def largefiles_rule(self) -> str:
        """這個 repo 的 `annex.largefiles`（H1/L：clone 時就依 target 設定）。

        Agora 沒有自己的欄位時用 `annex.git.DEFAULT_LARGEFILES`（Agora 的單一來源
        `agora.store.LARGEFILES_RAW` 就是它）；Foundry 一定要在 `repos` 區塊裡
        明寫自己的規則。
        """
        return self.largefiles or DEFAULT_LARGEFILES

    @property
    def allowed_item_types(self) -> frozenset[str]:
        """這個 pipeline 該處理的收件匣項目型態（H2：分派依「驗章通過的」型態）。"""
        if self.element == "foundry":
            return frozenset({"artifact"})
        return frozenset({"session", "handoff", "claim", "reference", "rewrite"})

    def allowed_types_for(self, *, others_configured: bool) -> frozenset[str]:
        """這一輪實際該收的型態。

        沒有其他 repo 會接手時，Agora 連 artifact 一起收——讓 evaluate 回
        `REJECT(foundry_not_enabled)`（M2：寫入者看得到原因，24 小時後清掉），
        否則 artifact 會永遠躺在收件匣裡沒有人評估。
        """
        allowed = self.allowed_item_types
        if self.element != "foundry" and not others_configured:
            return allowed | FOUNDRY_ITEM_TYPES
        return allowed

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
        """Foundry（或之後的第三個 repo）：由 `repos` 區塊組出。

        M4（review-25a48a9）：`prefix_levels` 與 `max_raw_size` 必須是**它自己的**
        ——沿用 Agora 的 prefix_levels 會讓 Foundry 的清掃去檢查 Agora 的上層資料夾
        （而 Foundry 自己的上層完全沒有保護），沿用 Agora 的 `max_raw_size`（50 MiB）
        會讓 50〜100 MB 的收容產出被拒收成 `too_large`（D7 是 100 MB）。
        """
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
            max_raw_size=rc.max_raw_size,
            quarantine_retention_days=cfg.quarantine_retention_days,
            prefix_levels=rc.prefix_levels,
            identity_registry_path=cfg.identity_registry_path,
            pin_repo_url=cfg.pin_repo_url,
        )


def _store_annex_keys(git_dir: Path, git: AnnexGit, target: RepoTarget | None) -> frozenset[str]:
    """在 clone 上建立**唯讀**的 store，取它的 annex key 集合（init-pin 用）。

    刻意用 `GitRawStorage`：它不會動 `annex.largefiles`（`AnnexRawStorage` 的
    建構會覆寫那條規則，會把這個 repo 原來的規則——例如 Foundry 的
    `include=objects/*/*` 或 e2e 的 `include=*.json`——改掉）。
    `AgoraStore.annex_keys()` 在這種情況下回傳的是 `snapshots.jsonl` 的歷史鍵。
    """
    if target is not None and target.element != "agora":
        from aistorage.foundry.store import FoundryStore

        store = FoundryStore(
            git_dir, git=git, temp_dir=None, largefiles=target.largefiles,
            configure_annex=False)
        return store.annex_keys()
    store = AgoraStore(git_dir, raw_storage=GitRawStorage(git_dir), git=git)
    return store.annex_keys()


def _assert_keys_on_drive_for_init_pin(
    drive: DriveClient, prefix_folder_id: str, repo_uuid: str,
    keys: frozenset[str],
) -> None:
    """init-pin 的最後一道：記錄的每一個 annex 物件都必須真的在 Drive 上。

    缺一個就拒絕建立釘選值（`MismatchError`）——有缺口的釘選值會讓第一輪 sweep
    把那些物件隔離，資料就沒了（review-cdb4a34 H1）。
    """
    by_name = {
        f.name: f for f in drive.list_children(prefix_folder_id) if not f.is_folder
    }
    missing: list[str] = []
    for key in sorted(keys):
        f = by_name.get(key)
        if f is None:
            missing.append(key)
            continue
        size_text, _, sha_text = key.partition("--")
        try:
            key_size = int(size_text.split("-s", 1)[1])
        except (IndexError, ValueError):
            missing.append(f"{key}（形狀不合法）")
            continue
        key_sha = sha_text.split(".", 1)[0].lower()
        if f.sha256 is None or f.sha256.lower() != key_sha or f.size != key_size:
            missing.append(key)
    if missing:
        raise MismatchError(
            f"拒絕建立釘選值：{len(missing)} 個 annex 物件在 Drive 上不存在或內容不符"
            f"（{prefix_folder_id}）：{missing[:3]}；先把真本推上去（或用 "
            "`git annex copy --to=<remote>` 補齊），再重新 init-pin"
        )


def _target_as_config(cfg: CommitterConfig, target: RepoTarget) -> CommitterConfig:
    """把 RepoTarget 覆蓋回 CommitterConfig（`init-pin` 用同一條程式路徑）。"""
    import dataclasses

    return dataclasses.replace(
        cfg,
        repo=target.repo,
        repo_uuid=target.repo_uuid,
        repo_url=target.repo_url,
        prefix_folder_id=target.prefix_folder_id,
        quarantine_folder_id=target.quarantine_folder_id,
        readview_folder_id=target.readview_folder_id,
        readview_manifest_file_id=target.readview_manifest_file_id,
        readview_rebuild_epoch=target.readview_rebuild_epoch,
        max_git_bundles=target.max_git_bundles,
        max_gc_per_run=target.max_gc_per_run,
        max_raw_size=target.max_raw_size,
        quarantine_retention_days=target.quarantine_retention_days,
        prefix_levels=tuple(target.prefix_levels),
    )


def verify_clone_identity(git: AnnexGit, target: RepoTarget) -> None:
    """H1：clone 之後確認這個 repo **就是**這個 target 的（不符就中止）。

    clone 到錯的 repo 時，第 3、4 步（settle／sweep）用的是 Foundry 的 pin，
    第 5 步卻拿到 Agora 的 repo，之後 `verify_clone` 拿 Agora 的 `ls-remote` 去比
    Foundry 的 pin——最壞的情況是整條比對通過，Foundry 的內容被寫進 Agora 並 push。
    這裡在比對之前就先確認身分：
    - `remote.origin.url` 必須等於 `target.repo_url`；
    - annex special remote 的 uuid 必須等於 `target.repo_uuid`。
    """
    for op in ("origin_url", "remote_uuid"):
        if not hasattr(git, op):
            raise AbortRun(
                "annex.git.clone", "unsupported_git",
                f"AnnexGit 不支援 {op}()，無法確認 clone 到的是目標 repo；中止")
    actual_url = git.origin_url()
    if actual_url != target.repo_url:
        raise MismatchError(
            f"clone 到的 repo 不是 {target.element}：remote.origin.url "
            f"({actual_url}) 與設定的 repo_url ({target.repo_url}) 不符")
    actual_uuid = git.remote_uuid("origin")
    if actual_uuid != target.repo_uuid:
        raise MismatchError(
            f"clone 到的 annex 遠端不是 {target.element}：remote uuid "
            f"({actual_uuid}) 與設定的 repo_uuid ({target.repo_uuid}) 不符")


@dataclass
class RepoRunResult:
    """單一 repo 這一輪的結果（第 14 步清收件匣需要）。"""

    target: RepoTarget
    decisions: list[Decision]
    applied_accepted: list[Decision]
    store: Any
    state: PinState | None = None
    #: 這一輪**完整走完**（可以安全清收件匣）。H3／M2：任何一步中止時為 False，
    #: 第 14 步就要保留這個 repo 的收件匣項目。
    complete: bool = True
    #: 這一筆拒收已經進入過某個已發佈的世代（H4）。只有這���的拒收才可以刪。
    published_rejections: frozenset[str] = frozenset()


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
    #: H2：item_key → 通過驗章的候選（`None` = 驗章沒過或形狀不完整）。
    #: 分派與「要不要 clone Foundry」都只看它，不再看未驗章的 sidecar。
    verified: dict[str, VerifiedSidecar | None] = field(default_factory=dict)

    def bump(self, key: str, value: int) -> None:
        name = f"{self.prefix}{key}" if self.prefix else key
        self.report.counts[name] = self.report.counts.get(name, 0) + int(value)

    def time(self, key: str, ms: int) -> None:
        name = f"{self.prefix}{key}" if self.prefix else key
        self.report.durations_ms[name] = self.report.durations_ms.get(name, 0) + int(ms)

    def set_field(self, field: str, value: Any) -> None:
        name = f"{self.prefix}{field}" if self.prefix else field
        setattr(self.report, name, value)

    def field_name(self, field: str) -> str:
        """這個 pipeline 的計數／欄位在 RunReport 裡叫什麼（加不加上 repo 前綴）。"""
        return f"{self.prefix}{field}" if self.prefix else field

    # ------------------------------------------------------------------
    # H2／M3：先驗章，再依「通過驗章的候選」的型態分派
    # ------------------------------------------------------------------
    def verify_all(self) -> None:
        """對每一個收件匣項目驗章一次，結果放進 `self.verified`。

        只做一次：兩個 pipeline 都會用到（Foundry 的項目在 Agora 那邊是 SKIP）。
        這一步**不寫任何真本**，所以驗章失敗的項目照舊由 evaluate 記帳。
        """
        for item in self.scan.items:
            if item.item_key in self.verified:
                continue
            verified, _code = verify_item_sidecar(
                item, drive=self.deps.drive, registry=self.deps.registry)
            self.verified[item.item_key] = verified

    def verified_type(self, item: Any) -> str:
        """這個項目**通過驗章**的型態（沒有通過就是空字串）。"""
        verified = self.verified.get(item.item_key)
        return verified.item_type if verified is not None else ""

    def has_verified_type(self, wanted: str) -> bool:
        return any(self.verified_type(i) == wanted for i in self.scan.items)

    def items_for(self, target: RepoTarget, *, others_configured: bool) -> list[Any]:
        """這個 repo 該評估哪些收件匣項目（依**驗章通過的**型態分派）。

        `others_configured`：這一輪有沒有其他 repo 會接手（Foundry 有設定時，
        artifact 就不歸 Agora，否則歸 Agora 讓它回 REJECT(foundry_not_enabled)，
        寫入者才看得到原因）。
        """
        allowed = target.allowed_types_for(others_configured=others_configured)
        out = []
        for item in self.scan.items:
            item_type = self.verified_type(item)
            if item_type and item_type not in allowed:
                # 驗章通過、但型態屬於**另一個** pipeline → 這裡不碰它
                # （H2：不進清冊、不寫拒收、不影響收件匣的刪除判斷）
                self.report.counts[
                    f"{self.prefix}skipped_wrong_pipeline"] = self.report.counts.get(
                        f"{self.prefix}skipped_wrong_pipeline", 0) + 1
                continue
            # item_type == "" = 沒有任何候選通過驗章 → 交給 Agora 照常評估，
            # 由 evaluate 記帳（bad_signature／orphan／unauthorized…）。
            out.append(item)
        return out


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
    others_configured: bool = False,
) -> RepoRunResult:
    """第 3〜13 步：對單一真本跑一輪（settle → sweep → clone → apply → push → 轉正 → 發佈）。

    多 repo 時依序呼叫：Agora 先（Foundry 的 `produced_by_session_id` 檢查要讀 Agora
    的工作樹），Foundry 後。收件匣是共用的，依**驗章通過的**型態分派（見
    `PipelineContext.items_for`；H2：未驗章的 sidecar 不再決定歸屬）。

    `others_configured`：這一輪有沒有其他 repo 會接手（決定 artifact 是不是也
    歸這裡，見 `items_for`）。
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

    # M2（review-25a48a9）：這個 repo 的釘選值還沒初始化 → 整個 repo 視為
    # 「沒有設定」，整輪以 `repo_not_enabled` 結束。**不要**讓 `pins.load` 的
    # ReadError 變成整輪中止：只要收件匣裡有 artifact，Agora 已經 promote 的
    # 內容就永遠不會被清理，Agora 會被 Foundry 拖住（每一輪都這樣）。
    try:
        state, pending = deps.pins.load(rcfg.repo)
    except AiStorageError as e:
        if target.element != "agora":
            ctx.step = "integrity.settle"
            ctx.set_field("repo_not_enabled", target.element)
            report.aborted_at = f"{target.prefix}integrity.settle"
            report.code = "repo_not_enabled"
            return RepoRunResult(
                target=target, decisions=[], applied_accepted=[], store=None,
                state=None, complete=False)
        raise
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
    # H1：URL 與 annex 規則都從 target 來（舊簽名只給 dest，Foundry 會 clone 到 Agora）
    git = deps.git_factory(git_dir, rcfg)
    # H1：clone 之後先確認身分，再做任何比對（clone 到錯的 repo 時，後面的
    # verify_clone 會拿錯的 ls-remote 去比對，最壞的情況是整條通過）。
    verify_clone_identity(git, rcfg)
    # 覆蓋率檢查：clone 出來的遠端必須至少涵蓋釘選值記錄的每一個 key。
    # 之前這裡沒有傳 expected_annex_keys，verify_annex_coverage 拿到的是空集合，
    # 檢查形同虛設（review-g7-e2e A-M1）。
    verify_clone(
        git,
        state,
        drive=deps.drive,
        prefix_folder_id=rcfg.prefix_folder_id,
    )
    # 覆蓋率檢查（e2e 修正）：釘選值記載的每一個 annex 物件都必須**在 Drive 上**
    # （名稱、checksum、size 都對得上）。
    #
    # 原本這裡比對的是 `git annex find --in=<uuid>`——那是**本機 location log**，
    # 也就是提交流程自己寫的帳本。e2e 實測（impl3／9.1）：第一次提交成功之後
    # **每一輪**都在這裡 MismatchError 中止（「缺少釘選值記載之物件
    # SHA256E-s19984--…」，18 次），而且不會自己好——釘選值與 Drive 其實是對的，
    # 錯在拿自己寫的帳本去對帳，而且 consolidate（annex.max-git-bundles）會讓
    # 那份帳本變動。改問 Drive 之後，這個檢查既更有權威（少了就是真的不見），
    # 也不會被帳本的形式擺平。
    # location log 仍然拿來算 pending（第 9 步要記「push 之後遠端會有什麼」），
    # 只是不再拿來當這一輪的門檻。
    verify_pin_keys_on_drive(
        deps.drive, rcfg.prefix_folder_id, state, repo_listing=repo_listing)
    ctx.bump("annex_keys_checked", len(state.annex_keys))
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
    for item in ctx.items_for(target, others_configured=others_configured):
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
            # H2：分派已經用驗章後的型態擋過一次；這裡再擋一次是防呆
            allowed_types=rcfg.allowed_types_for(others_configured=others_configured),
            preverified=ctx.verified.get(item.item_key),
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
            if target.element != "foundry":
                # H2：Agora 的 pipeline 不該拿到 artifact。分派已經用驗章後的型態
                # 擋過，這裡是第二道防呆——寧可整輪中止，也不要把 artifact 寫進
                # Agora 的 repo（FoundryStore 沒有 AgoraStore 的方法時，例外會被
                # 誤讀成「暫時的問題」，於是每一輪都重複，變成 DoS）。
                raise MismatchError(
                    f"Agora 的 pipeline 收到了 artifact 項目 {dec.item.item_key}："
                    "分派（依驗章後的型態）不該把它送來這裡")
            # 產生者檢查要讀 Agora 的工作樹，所以必須傳 agora_store
            res = apply_artifact(foundry_store, dec, agora_store, deps.clock)
        elif item_type in ("session", "handoff", "claim", "reference"):
            if target.element == "foundry":
                # H2：Foundry 的 pipeline 只收 artifact
                raise MismatchError(
                    f"Foundry 的 pipeline 收到了 {item_type} 項目 "
                    f"{dec.item.item_key}：分派不該把它送來這裡")
            if item_type == "session":
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
            else:
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
        # H1（review-cdb4a34，資料遺失）：`git annex copy` 預設只搬「樹狀裡還在」
        # 的檔案。同一輪收進同一個 Session 的兩版時，樹狀的 raw 只剩最新那一版，
        # 另一版的物件雖然已經建好、也記進 snapshots.jsonl 與 pending，卻不會被
        # 搬到 Drive——它只留在提交流程的暫存 clone 裡，clone 一刪就沒了。key 覆蓋
        # 率與 sweep 都看不出來（key 有名字、物件卻不存在）。所以明確把這一輪新增
        # 的 key 交給 annex copy，不讓它靠樹狀決定。
        new_keys_to_copy = sorted(store.annex_keys() - keys_before)
        git.copy("origin", to_copy=new_keys_to_copy or None)
    ctx.time("annex.git.copy", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 9 步：pins.write_pending
    # ---------------------------------------------------------
    ctx.step = "pins.write_pending"
    t0 = time.monotonic()
    # H3：寫 pending **之前**重查一次。管理者可能在我們 clone 之後才上鎖
    # （第 1b 步那時還沒有旗標）。寫了 pending 再被擋下來，下一輪的 settle 會
    # 碰到「遠端既不等於 pending 也不等於正式值」，整個 repo 卡住。
    _assert_no_maintenance(rcfg.repo, deps, report, "pins.write_pending")
    # refs 與 annex key 集合取自 AnnexGit 自己的公開方法；缺少必要分支
    # 由 SubprocessAnnexGit.local_refs() raise，不在這裡吞掉例外。
    local_refs = git.local_refs()
    # H1（review-cdb4a34，會遺失資料）：釘選值的 annex key 集合必須**單調遞增**。
    #
    # 原本這裡是 `annex_keys_in(uuid) | (store.annex_keys() - keys_before)`——
    # `annex_keys_in` 讀的是 location log（提交流程自己寫的），它**可能漏記**
    # 某些樹狀裡的物件（e2e 的 `include=*.json` 規則把 reading／meta 也收進
    # annex，store 不知道它們；consolidate 又會讓 location log 變動）。漏記的
    # 後果是 promote 之後下一輪 sweep 把那些物件當成「不在釘選值裡」而隔離，
    # 資料就這麼沒了。
    #
    # 所以改成聯集：正式值 ∪ store 現在知道的（樹狀＋snapshots 歷史）∪ 這一輪
    # 新增的 ∪ location log 的觀點。只有**管理者抹除**（admin/erase.py 重建
    # 釘選值）可以讓它縮小。
    #
    # 為什麼連 `annex_keys_in` 也要留著：它涵蓋 store 不知道的那一類（被
    # `include=*.json` 收走的 reading／meta 檔——store 不記它們的 key）。
    # 不記的話，下一輪 sweep 會把它們當成「不在釘選值裡」而隔離。漏記會遺失
    # 資料，多記只會讓第 5 步的 Drive 檢查去擋（那是對的行為）。
    store_keys = store.annex_keys()
    new_keys = store_keys - keys_before
    location_keys = (
        git.annex_keys_in(state.repo_uuid) if hasattr(git, "annex_keys_in")
        else frozenset()
    )
    annex_keys = (
        frozenset(state.annex_keys) | store_keys | new_keys | location_keys
    )

    # 單調性斷言（fail-closed）：pending 少了正式值記載的任何一個 key，就代表
    # 有人在這一輪把它算掉了 → 中止，不要寫出一個會讓下一輪隔離資料的 pending。
    dropped = set(state.annex_keys) - set(annex_keys)
    if dropped:
        raise MismatchError(
            f"pending 的 annex key 集合不得小於正式釘選值（少了 {len(dropped)} 個："
            f"{sorted(dropped)[:3]}）；中止，避免下一輪 sweep 把它們隔離"
        )

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
        # H3：push **之前**重查一次。管理操作最怕的正是這一步（抹除是
        # 「刪遠端 → 重推」）；原本的重查在 push 之後，撞上了也已經推出去。
        _assert_no_maintenance(rcfg.repo, deps, report, "git.push")
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
        #   - 比較對象＝**本機 clone 下來的 git-annex location log**
        #     （`git annex find --in=<uuid>`，clone 已有、push 會更新它）。
        # M1（review-25a48a9）：location log 是提交流程自己寫的，Drive 上的物件
        #   真的不在時它仍會宣稱在。所以第 11 步**另外**對新 key 做一次 Drive 實況
        #   檢查（列舉前綴：name == key、sha256Checksum == key 內嵌雜湊、size 相符）。
        new_keys = store.annex_keys() - keys_before
        remote_keys_after_push = (
            git.annex_keys_in(state.repo_uuid)
            if hasattr(git, "annex_keys_in")
            else annex_keys
        )
        # H1（review-cdb4a34，資料遺失）：被換掉的那一版（同一輪裡先收、
        # 隨後又被新一版取代的快照）不在樹狀裡——真本的 raw 只有最新一版。
        # git-annex 的 location log 只認「樹狀裡的檔案」，所以這種 key 永遠不會
        # 出現在 `annex_keys_in()` 裡，拿它當必要條件會讓每一輪都中止
        # （observed：expected 有兩個 key，pushed 只有最新那一版）。
        # 分工：location log 只能證明「樹狀裡那些 key 有被 push 上去」，
        # 每一輪新增的 key（包含不在樹狀裡的舊版本）一律由下面的 Drive 實況檢查
        # 負責——那才是「物件真的在 Drive 上」的證據。
        branch_keys = (
            git.annex_keys_in_branch()
            if hasattr(git, "annex_keys_in_branch") else None)
        expected_in_log = (
            new_keys & branch_keys
            if isinstance(branch_keys, (set, frozenset)) else new_keys)
        push_verification = verify_after_push(
            git,
            deps.drive,
            repo_listing,
            state,
            local_refs,
            push_started_at,
            workdir=work_temp,
            expected_annex_keys=expected_in_log,
            pushed_annex_keys=remote_keys_after_push,
        )
        verify_new_keys_on_drive(deps.drive, rcfg.prefix_folder_id, new_keys)

    ctx.time("verify.verify_after_push", int((time.monotonic() - t0) * 1000))

    # ---------------------------------------------------------
    # 第 12 步：pins.promote + gc.gc_removed
    # ---------------------------------------------------------
    ctx.step = "pins.promote"
    t0 = time.monotonic()

    # H3（review-25a48a9）：管理者是在這一輪跑到一半才上鎖的情況，也要擋。
    # 在寫入 pin 之前再查一次：命中就讓**整輪**停下來（`AbortRun` 會讓 `run()`
    # 不處理下一個 repo、也不執行第 14 步）。原本這裡只是 `return`，於是已經
    # push 但還沒 promote 的項目會被從收件匣刪掉——交接單、認領是一次性的，
    # 刪了就永久遺失，而且這一輪還被回報成成功。
    _assert_no_maintenance(rcfg.repo, deps, report, "pins.promote")

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
    published_rejections: frozenset[str] = frozenset()
    pub_rep: Any = None
    try:
        if target.element == "foundry":
            # H5：Foundry 改用與 Agora 同一套的發佈器（impl2 提供，
            # 呼叫方式見 publish/foundry.py 的模組 docstring）。回報的 status 進
            # RunReport、對不上的筆數記成 `foundry.unpublished`。
            if not rcfg.readview_folder_id or not rcfg.readview_manifest_file_id:
                ctx.set_field("readview_publish", "skipped_no_readview")
            else:
                from aistorage.publish.foundry import FoundryReadViewPublisher

                fpub = FoundryReadViewPublisher(
                    deps.drive,
                    folder_id=rcfg.readview_folder_id,
                    manifest_file_id=rcfg.readview_manifest_file_id,
                    prefix_folder_id=rcfg.prefix_folder_id,
                    clock=deps.clock, workdir=work_temp,
                )
                fresult = fpub.publish(
                    foundry_store,
                    foundry_main_sha=local_refs.get("refs/heads/main", ""),
                    run_rejections=collect_rejections(store, decisions),
                    allowed_keys=state.annex_keys,
                    dry_run=dry_run,
                )
                pub_rep = fresult.report
                ctx.set_field("readview_publish", pub_rep.status)
                ctx.set_field("unpublished", len(fresult.unpublished))
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
        published_rejections = _published_rejection_keys(
            store, decisions,
            getattr(report, ctx.field_name("readview_publish"), None),
            pub_rep,
            dry_run=dry_run)
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
        complete=True,
        published_rejections=published_rejections,
    )


def _published_rejection_keys(
    store: Any,
    decisions: list[Decision],
    publish_status: Any,
    pub_rep: Any,
    *,
    dry_run: bool,
) -> frozenset[str]:
    """H4：這一輪（或既有世代）已經包含在讀取視圖裡的拒收 item_key。

    第 14 步刪除拒收項目的條件不是「這一輪有沒有發佈」，而是「這一筆拒收已經進入過
    某個已發佈的世代」：

    - `published`／`planned`：這一輪的 `run_rejections` 全部進了這個世代；
    - `skipped`：上一個世代就已經有這一輪完全相同的拒收集合（發佈器的冪等判斷
      正是比對這一點），所以同樣算「已發佈」；
    - 沒有發佈器／發佈失敗：一律不算（`empty`），寧可留著。

    同時把世代號寫進真本 `_committer/rejections/<key>.json` 的
    `published_generation`（稽核用；這一步在 push 之後，所以不會再 commit——
    真正用來刪除的依據是這裡回傳的集合）。
    """
    status = str(publish_status or "")
    keys_attr = getattr(pub_rep, "published_item_keys", None)
    if keys_attr is None:
        # 沒有回報 item_key 的發佈器（Foundry 的舊路徑 `_publish_foundry`、以及
        # 第三方發佈器）：退回「這一輪的拒收都算已發佈」。impl2 接上 Foundry 的
        # publisher 之後，它要像 Agora 一樣回 `published_item_keys`，否則 Foundry
        # 的拒收會永遠留著（H4 的原問題換到 Foundry 而已）。
        if not (status == "published" or status.startswith("published")
                or status == "planned"):
            return frozenset()
        keys = {dec.item.item_key for dec in decisions if dec.kind == DecisionKind.REJECT}
    else:
        keys = set(keys_attr)
    if not keys or dry_run:
        return frozenset()
    generation = getattr(pub_rep, "generation", None)
    if generation is not None:
        mark_rejections_published(store, sorted(keys), int(generation))
    return frozenset(keys)


def mark_rejections_published(store: Any, item_keys: list[str], generation: int) -> None:
    """把 `published_generation` 寫進真本的拒收紀錄（只影響真本副本，不 commit）。"""
    for item_key in item_keys:
        rel = layout.rejection_path(item_key)
        path = store.worktree / rel
        if not path.is_file():
            continue  # 驗章前的拒收本來就不寫進真本
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("published_generation") == generation:
            continue
        data["published_generation"] = generation
        store.put_json(rel, data)


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
            # 第 1b 步：維護旗標（H3／H4／PM 決定 7）
            # ---------------------------------------------------------
            # 管理操作（抹除、回滾）進行中時，提交流程必須**整輪不做任何事**：
            # 不清扫、不 push、不發佈、不刪收件匣。沒有這一步，住民重新啟用
            # workflow 觸發就會和管理操作撞在一起。
            # 旗標內容損毀 → 當成維護中（fail-closed）並中止，不猜。
            # H3：**每一個** target 的旗標都要查（管理���作可能只對 Foundry 上鎖；
            # 只查 Agora 的話，Foundry 的 settle／sweep／clone／push 全部照跑）。
            current_step = "maintenance"
            for repo_name in _maintenance_repo_names(cfg):
                if _check_maintenance(_RepoNameShim(repo_name), deps, report):  # type: ignore[arg-type]
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

            # H2／M3：分派之前先驗章。分派（哪個 repo 收哪些項目）與「要不要
            # clone Foundry」都只看**通過驗章的候選**的型態——原本看的是
            # 「第一個 sidecar 候選」，住民放一個排在前面的垃圾 sidecar 就能
            # 把合法的 session 導到 Foundry（或反過來被永久燒掉）。
            current_step = "intake.verify"
            t0 = time.monotonic()
            ctx.verify_all()
            report.durations_ms["intake.verify"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 3〜13 步：逐 repo 跑 pipeline（Agora → Foundry）
            # ---------------------------------------------------------
            # PM 決定 9：只有收件匣真的有**通過驗章的** artifact 時才 clone
            # Foundry，平常成本不變。
            # M2（review-25a48a9）：設定檔有列 Foundry、但它的釘選值還沒初始化
            # → **整個 Foundry 視為沒有設定**，這一輪不跑它的 pipeline。
            # 這樣 artifact 會落到 Agora 那一邊，被 evaluate 回
            # REJECT(foundry_not_enabled)（寫入者看得到原因，24 小時後清掉）；
            # 否則 `pins.load` 的 ReadError 會讓整輪中止，Agora 已經 promote 的
            # 內容永遠不會被清理——只要收件匣裡有 artifact，就每一輪都這樣。
            targets = [RepoTarget.from_config(cfg)]
            if cfg.repos:
                if ctx.has_verified_type("artifact"):
                    for rc in cfg.repos:
                        try:
                            deps.pins.load(rc.name)
                        except AiStorageError:
                            report.repo_not_enabled = rc.name
                            report.foundry_skipped = "repo_not_enabled"
                            continue
                        targets.append(RepoTarget.from_repo_config(cfg, rc))
                else:
                    report.foundry_skipped = "no_artifacts"

            results: list[RepoRunResult] = []
            agora_store = None
            for target in targets:
                ctx.prefix = "" if target.element == "agora" else f"{target.element}."
                try:
                    result = _run_repo_pipeline(
                        ctx, target, agora_store=agora_store,
                        others_configured=len(targets) > 1,
                    )
                except AbortRun:
                    # H3：維護中 → **整輪**停止：不處理下一個 repo，也不執行第 14 步
                    # （第 14 步會刪掉已 push 但未 promote 的項目，而交接單、
                    # 認領是一次性的，刪了就永久遺失）。
                    raise
                except Exception as e:  # noqa: BLE001 - M2：逐 repo 隔離
                    # 這個 repo 失敗（沒有釘選值、clone 失敗、H1 的 URL 錯誤…）
                    # → 記下步驟與代碼，**不**連帶讓已經走完的 repo 白跑：
                    # 第 14 步照常清理那些走完的 repo 的項目。
                    report.aborted_at = f"{ctx.prefix}{ctx.step}"
                    report.code = type(e).__name__
                    _dump_traceback(run_id, report.aborted_at, e)
                    current_step = ctx.step
                    break
                results.append(result)
                if target.element == "agora":
                    agora_store = result.store
                current_step = ctx.step

            # ---------------------------------------------------------
            # 第 14 步：clean_inbox 刪除收件匣檔案
            # ---------------------------------------------------------
            # M2／H3：第 14 步以 **repo 為單位**——走完的 repo 照常清理，
            # 沒走完的（維護中、未設定、任何一步失敗）整組保留。留下來下一輪
            # 還有機會，刪掉就可能永久遺失（交接單、認領是一次性的）。
            current_step = "clean_inbox"
            if ctx is not None:
                ctx.step = "clean_inbox"
            t0 = time.monotonic()
            # M2：只清理「完整走完」的 repo 的項目。失敗的 repo 保留（fail-closed：
            # 留下來下一輪還有機會，刪掉就永久遺失——交接單、認領是一次性的）。
            complete_repos = {r.target.repo for r in results if r.complete}
            # H4：拒收能不能刪，看「這一筆已經進入過某個已發佈的世代」，
            # 不是「這一輪有沒有發佈」。
            published_rejections: dict[str, frozenset[str]] = {
                r.target.repo: r.published_rejections for r in results
            }
            deleted_inbox_count = 0
            failed_delete_count = 0
            inbox_folders = set(deps.registry.inbox_folders().keys())
            now_dt = deps.clock.now()

            # 挑選符合刪除條件的決策項目（M2／H3：repo 沒走完就整組保留）
            for result, dec in ((r, d) for r in results for d in r.decisions):
                if result.target.repo not in complete_repos:
                    continue
                published = published_rejections.get(result.target.repo, frozenset())
                should_delete = False
                if dec.kind == DecisionKind.ACCEPT and dec in result.applied_accepted:
                    should_delete = True
                elif dec.kind == DecisionKind.ALREADY:
                    should_delete = True
                elif dec.kind == DecisionKind.REJECT:
                    # H4（review-25a48a9）：只有「已進入某個已發佈世代」的拒收可以刪。
                    # 舊的判斷是「這一輪的發佈狀態是 published」，但被拒收的項目
                    # 通常在它可以刪的那一輪（24 小時後）不會有新內容 →
                    # publisher 回 skipped → 永遠不刪 → 收件匣永遠不是空的。
                    # 這一筆還沒被任何讀者看得到時就刪掉，等於讓寫入者永遠不知道
                    # 為什麼被拒。
                    if dec.item.item_key in published and dec.deletable_after is not None \
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
            if e.step == "maintenance" and e.code == "active":
                # 報告要說得出「為什麼不動」（不論旗標是被誰 raise 出來的）
                report.maintenance = report.maintenance or "active"
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

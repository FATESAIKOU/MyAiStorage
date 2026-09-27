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
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import urllib.request

from aistorage.agora import layout
from aistorage.agora.apply import (
    apply_claim,
    apply_handoff,
    apply_reference,
    apply_rewrite,
    apply_session,
)
from aistorage.agora.store import (
    AgoraStore,
    FakeRawStorage,
    GitRawStorage,
    RawStorage,
)
from aistorage.annex.fake import FakeAnnexGit
from aistorage.annex.git import AnnexGit, SubprocessAnnexGit
from aistorage.annex.manifest import parse_manifest
from aistorage.clock import Clock, format_rfc3339
from aistorage.committer.config import CommitterConfig
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
    LEDGER_CODES,
    Decision,
    DecisionKind,
    evaluate,
    sort_accepted_decisions,
)
from aistorage.intake.ledger import Ledger
from aistorage.intake.scan import count_shaped, scan_inboxes
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

    @property
    def ok(self) -> bool:
        return self.aborted_at is None

    def format_log(self) -> str:
        """格式化日誌輸出。"""
        status = "SUCCESS" if self.ok else f"ABORTED({self.aborted_at}:{self.code})"
        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        durations_str = ", ".join(f"{k}={v}ms" for k, v in sorted(self.durations_ms.items()))
        return f"[RunReport {self.run_id}] {status} | counts: [{counts_str}] | durations: [{durations_str}]"


def _get_local_refs(git: AnnexGit, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
    """自 AnnexGit 取得本地指定分支之完整 ref 集合。"""
    if hasattr(git, "local_refs"):
        return git.local_refs(branches)  # type: ignore[no-any-return]
    if isinstance(git, FakeAnnexGit):
        combined = dict(git.refs)
        combined.update(git.pending_refs)
        refs: dict[str, str] = {}
        for b in branches:
            full = f"refs/heads/{b}"
            if full in combined:
                refs[full] = combined[full]
            elif b in combined:
                refs[full] = combined[b]
        return refs
    if hasattr(git, "_run"):
        refs = {}
        for b in branches:
            try:
                sha = git._run(["git", "rev-parse", f"refs/heads/{b}"], is_write=False).strip()  # type: ignore[no-untyped-call]
                if sha:
                    refs[f"refs/heads/{b}"] = sha
            except Exception:
                pass
        return refs
    return {}


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
    except Exception as e:
        raise AbortRun("guard", "api_error", f"無法自 GitHub API 取得遠端 main HEAD: {e}") from e

    if not remote_sha or remote_sha != github_sha:
        raise AbortRun(
            "guard",
            "sha_mismatch",
            f"GITHUB_SHA ({github_sha}) 與遠端 main HEAD ({remote_sha}) 不相符",
        )

    return "ok"


def prescan(cfg: CommitterConfig, deps: Deps) -> int:
    """提交流程預先掃描（第 2 步專用進入點）。

    形狀符合的收件匣項目數；若為 0 則印出 EMPTY 並回傳 0。
    """
    scan = scan_inboxes(deps.drive, deps.registry)
    shaped = count_shaped(scan)
    if shaped == 0:
        print("EMPTY")
        return 0
    print(f"{shaped}")
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

        refs = _download_and_replay(
            parsed.active,
            cfg.repo_uuid,
            files,
            deps.drive,
            workdir,
        )

    now_iso = format_rfc3339(deps.clock.now())
    state = PinState(
        repo=cfg.repo,
        repo_uuid=cfg.repo_uuid,
        refs=refs,
        manifest_sha256=m_sha,
        prev_manifest_sha256=None,
        active_bundles=parsed.active,
        removed_bundles=parsed.removed,
        annex_keys=frozenset(),
        promoted_at=now_iso,
        run_id="init-pin",
    )

    if confirm:
        deps.pins.promote(state)
        print(f"INIT_PIN_PROMOTED: repo={state.repo} manifest={m_sha[:8]} active={len(state.active_bundles)}")
    else:
        print(f"INIT_PIN_PLAN: repo={state.repo} manifest={m_sha[:8]} active={len(state.active_bundles)} (dry-run)")

    return state


def run(cfg: CommitterConfig, deps: Deps, *, dry_run: bool = False) -> RunReport:
    """執行 13 步提交流程主體。

    各步驟：
    1 guard: ref 與 sha 驗證（Actions 環境）
    2 intake.scan: 掃描收件匣，空則提前結束
    3 integrity.settle: 結算待定釘選值
    4 integrity.sweep: 清掃前綴資料夾與上層同名資料夾
    5 annex.git.clone + integrity.verify.verify_clone: clone 真本並核對釘選值
    6 annex.git: 準備 git annex 環境
    7 intake.evaluate + agora.apply: 評估決策並套用至真本
    8 pins.write_pending: 寫入待定釘選值
    9 verify.precheck + git.copy + git.push: 預檢並推送
    10 verify.verify_after_push: push 後遠端狀態驗證
    11 pins.promote + gc.gc_removed: 轉正釘選值與 bundle 回收
    12 publisher.publish: 發佈讀取視圖
    13 clean_inbox: 刪除已處理之收件匣項目
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
        try:
            # ---------------------------------------------------------
            # 第 1 步：guard 檢查
            # ---------------------------------------------------------
            t0 = time.monotonic()
            guard_status = step1_guard(cfg, deps.clock)
            report.guard = guard_status
            report.durations_ms["guard"] = int((time.monotonic() - t0) * 1000)

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
            # 第 3 步：integrity.settle 結算待定釘選值
            # ---------------------------------------------------------
            current_step = "integrity.settle"
            t0 = time.monotonic()
            children = deps.drive.list_children(cfg.prefix_folder_id)
            files = tuple(f for f in children if not f.is_folder)
            subfolders = tuple(f for f in children if f.is_folder)
            repo_listing = RepoListing(
                prefix_folder_id=cfg.prefix_folder_id,
                files=files,
                subfolders=subfolders,
            )

            state, pending = deps.pins.load(cfg.repo)
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
                    deps.pins.drop_pending(cfg.repo)

            state = settled_state
            report.durations_ms["integrity.settle"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 4 步：integrity.sweep 清掃前綴資料夾
            # ---------------------------------------------------------
            current_step = "integrity.sweep"
            t0 = time.monotonic()
            parent_decisions: list[SweepDecision] = []
            if cfg.prefix_levels:
                parent_decisions = check_parents(list(cfg.prefix_levels), deps.drive)

            sweep_decisions = plan_sweep(
                repo_listing,
                state,
                repo_uuid=cfg.repo_uuid,
                prefix_folder_id=cfg.prefix_folder_id,
            )

            sweep_decisions = resolve_content_checks(
                sweep_decisions,
                deps.drive,
                content_cache,
                state,
                repo_uuid=cfg.repo_uuid,
                listing=repo_listing,
                prefix_folder_id=cfg.prefix_folder_id,
                workdir=work_temp,
            )

            readview_decisions: list[SweepDecision] = []
            if cfg.readview_folder_id:
                rv_children = deps.drive.list_children(cfg.readview_folder_id)
                rv_files = tuple(f for f in rv_children if not f.is_folder)
                rv_subfolders = tuple(f for f in rv_children if f.is_folder)
                rv_listing = RepoListing(
                    prefix_folder_id=cfg.readview_folder_id,
                    files=rv_files,
                    subfolders=rv_subfolders,
                )
                readview_decisions = plan_readview_sweep(
                    rv_listing,
                    set(),  # 第 4 組發佈前先清除非授權檔案
                    readview_folder_id=cfg.readview_folder_id,
                )

            all_sweep_decisions = parent_decisions + sweep_decisions + readview_decisions
            moved_count = apply_sweep(
                all_sweep_decisions,
                deps.drive,
                quarantine_folder_id=cfg.quarantine_folder_id,
                clock=deps.clock,
                prefix_folder_id=cfg.prefix_folder_id,
                dry_run=dry_run,
            )
            report.counts["quarantined_files"] = moved_count
            report.durations_ms["integrity.sweep"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 5 步：annex.git.clone + verify_clone
            # ---------------------------------------------------------
            current_step = "annex.git.clone"
            t0 = time.monotonic()
            git = deps.git_factory(git_dir)
            verify_clone(
                git,
                state,
                drive=deps.drive,
                prefix_folder_id=cfg.prefix_folder_id,
            )
            report.durations_ms["annex.git.clone"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 6 步：annex.git 環境確認
            # ---------------------------------------------------------
            current_step = "annex.git"
            t0 = time.monotonic()
            # 若為 SubprocessAnnexGit，已於 clone_for_commit 設定 annex.max-git-bundles
            report.durations_ms["annex.git"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 7 步：intake.evaluate + agora.apply
            # ---------------------------------------------------------
            current_step = "intake.evaluate"
            t0 = time.monotonic()

            if deps.raw_storage_factory:
                raw_storage = deps.raw_storage_factory(git_dir, git)
            elif isinstance(git, FakeAnnexGit):
                raw_storage = FakeRawStorage()
            else:
                raw_storage = GitRawStorage(git_dir)

            store = AgoraStore(
                worktree=git_dir,
                raw_storage=raw_storage,
                git=git,
                temp_dir=store_temp,
            )
            ledger = Ledger(store)

            # 評估所有收件匣項目
            decisions: list[Decision] = []
            for item in scan.items:
                dec = evaluate(
                    item,
                    drive=deps.drive,
                    registry=deps.registry,
                    store=store,
                    ledger=ledger,
                    clock=deps.clock,
                    workdir=work_temp,
                    max_raw=cfg.max_raw_size,
                )
                decisions.append(dec)

            accepted = [d for d in decisions if d.kind == DecisionKind.ACCEPT]
            sorted_accepted = sort_accepted_decisions(accepted)

            # 依型態順序套用至真本
            applied_accepted: list[Decision] = []
            now_iso = format_rfc3339(deps.clock.now())

            for dec in sorted_accepted:
                item_type = dec.record_metadata.get("type") if dec.record_metadata else ""
                if item_type == "session":
                    source = dec.sidecar["session"]["source"] if dec.sidecar else "opencode"
                    conv = deps.converters.get(source) or get_converter(source)
                    res = apply_session(store, dec, conv, deps.clock)
                elif item_type == "rewrite":
                    conv = deps.converters.get("opencode") or get_converter("opencode")
                    res = apply_rewrite(store, dec, conv)
                elif item_type == "handoff":
                    conv = deps.converters.get("opencode") or get_converter("opencode")
                    res = apply_handoff(store, dec, conv)
                elif item_type == "claim":
                    res = apply_claim(store, dec)
                elif item_type == "reference":
                    res = apply_reference(store, dec)
                else:
                    continue

                item_id = dec.record_metadata.get("id", "") if dec.record_metadata else ""
                raw_sha = dec.sidecar.get("raw", {}).get("sha256") if dec.sidecar else None

                if not res.ok:
                    # apply 判定失敗轉為 REJECT（拒絕記錄已由 apply 模組寫入）
                    if res.code in LEDGER_CODES:
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
                raw_sha = dec.sidecar.get("raw", {}).get("sha256") if dec.sidecar else None
                if dec.kind == DecisionKind.ALREADY:
                    ledger.record(
                        dec.item.item_key,
                        item_id=item_id,
                        decision="already",
                        raw_sha256=raw_sha,
                        at=now_iso,
                    )
                elif dec.kind == DecisionKind.REJECT and dec.authenticated:
                    if dec.code in LEDGER_CODES:
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

            report.counts["accepted"] = len(applied_accepted)
            report.counts["rejected"] = sum(1 for d in decisions if d.kind == DecisionKind.REJECT)
            report.counts["already"] = sum(1 for d in decisions if d.kind == DecisionKind.ALREADY)
            report.counts["deferred"] = sum(1 for d in decisions if d.kind == DecisionKind.DEFER)
            report.durations_ms["intake.evaluate"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 8 步：pins.write_pending
            # ---------------------------------------------------------
            current_step = "pins.write_pending"
            t0 = time.monotonic()
            local_refs = _get_local_refs(git)
            annex_keys = (
                git.annex_keys_in(state.repo_uuid)
                if hasattr(git, "annex_keys_in")
                else state.annex_keys
            )

            has_git_changes = (local_refs != state.refs) or (annex_keys != state.annex_keys)

            if has_git_changes:
                pending = PinPending(
                    repo=cfg.repo,
                    base_manifest_sha256=state.manifest_sha256,
                    refs=local_refs,
                    annex_keys=annex_keys,
                    written_at=format_rfc3339(deps.clock.now()),
                    run_id=run_id,
                )
                if not dry_run:
                    deps.pins.write_pending(pending)

            report.durations_ms["pins.write_pending"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 9 步：verify.precheck + git.copy + git.push
            # ---------------------------------------------------------
            current_step = "git.push"
            t0 = time.monotonic()
            push_started_at = deps.clock.now()

            if has_git_changes:
                precheck(
                    deps.drive,
                    cfg.prefix_folder_id,
                    f"GITMANIFEST--{state.repo_uuid}",
                    state,
                )
                git.copy("origin")
                if not dry_run:
                    git.push("origin", ("main", "git-annex"))

            report.durations_ms["git.push"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 10 步：verify.verify_after_push
            # ---------------------------------------------------------
            current_step = "verify.verify_after_push"
            t0 = time.monotonic()
            push_verification = None

            if has_git_changes and not dry_run:
                push_verification = verify_after_push(
                    git,
                    deps.drive,
                    repo_listing,
                    state,
                    local_refs,
                    push_started_at,
                    workdir=work_temp,
                )

            report.durations_ms["verify.verify_after_push"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 11 步：pins.promote + gc.gc_removed
            # ---------------------------------------------------------
            current_step = "pins.promote"
            t0 = time.monotonic()

            if has_git_changes and not dry_run and push_verification is not None:
                new_state = PinState(
                    repo=cfg.repo,
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
                prefix_folder_id=cfg.prefix_folder_id,
                state=state,
                max_delete=cfg.max_gc_per_run,
                dry_run=dry_run,
            )
            report.counts["gc_deleted"] = gc_count

            # 清理過期隔離檔案 (7天)
            purge_count = purge_quarantine(
                deps.drive,
                cfg.quarantine_folder_id,
                older_than_days=cfg.quarantine_retention_days,
                now=deps.clock.now(),
                max_delete=cfg.max_gc_per_run,
                dry_run=dry_run,
            )
            report.counts["quarantine_purged"] = purge_count
            report.durations_ms["pins.promote"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 12 步：publisher.publish
            # ---------------------------------------------------------
            current_step = "publisher.publish"
            t0 = time.monotonic()
            deps.publisher.publish(store, dry_run=dry_run)
            report.durations_ms["publisher.publish"] = int((time.monotonic() - t0) * 1000)

            # ---------------------------------------------------------
            # 第 13 步：clean_inbox 刪除收件匣檔案
            # ---------------------------------------------------------
            current_step = "clean_inbox"
            t0 = time.monotonic()
            deleted_inbox_count = 0
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
                    if dec.deletable_after is not None and now_dt >= dec.deletable_after:
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
                                pass
                        else:
                            deleted_inbox_count += 1

            report.counts["inbox_deleted"] = deleted_inbox_count
            report.durations_ms["clean_inbox"] = int((time.monotonic() - t0) * 1000)

        except AbortRun as e:
            report.aborted_at = e.step
            report.code = e.code
        except AiStorageError as e:
            report.aborted_at = current_step
            report.code = type(e).__name__
        except Exception as e:
            report.aborted_at = current_step
            report.code = type(e).__name__

    print(report.format_log())
    return report

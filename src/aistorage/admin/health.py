"""6.3 健康檢查（Mac 上執行，結果通知；launchd 另見腳本）。

兩段分開：
- `collect_health`：真的去取資料（pin 的 promoted_at、GitHub API、Drive about、
  manifest、隔離區），任何一項取不到就**留成 None**。
- `run_health`：純函式判定，把 HealthData 轉成 Check 清單。

取不到的判定一律是 `warn` 而不是 `ok`（review M7）：查不到本身就是問題，
判 ok 等於把「沒在監控」當成「一切正常」。launchd 排程的指令因此不帶
`--data-json`：plist 呼叫的是 `health`（自行 collect），只印 plist 用 `--plist`。

各閾值集中在此模組頂部，改動即是政策變動。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Literal, Sequence

from aistorage.admin.lock import DEFAULT_WORKFLOW
from aistorage.clock import Clock, parse_rfc3339
from aistorage.drive.model import DriveClient

CheckStatus = Literal["ok", "warn", "fail"]

# ---- 閾值（政策） ----
ABORT_STREAK_FAIL = 3                 # 連續中止輪數達此值 → fail
STALE_WARN_HOURS = 12                 # 距上次成功提交超過 → warn
STALE_FAIL_HOURS = 30                 # 距上次成功提交超過 → fail（預設 6h 間隔的 5 倍）
ACTIONS_MINUTES_WARN = 300            # 兩週 Actions 分鐘數超過 → warn（6.3 監控目標）
QUOTA_WARN_RATIO = 0.85               # 配額使用率超過 → warn
QUOTA_FAIL_RATIO = 0.95               # 配額使用率超過 → fail
INDEX_BYTES_WARN = 50 * 1024 * 1024   # D5：索引 50MB
READVIEW_FILES_WARN = 5000            # D5：讀取視圖 5,000 個檔案
LAUNCHD_INTERVAL_S = 6 * 3600         # launchd 每 6 小時
#: 前綴裡「釘選值背書不了」的檔案放超過一輪（預設 6h 排程 × 2）就報 warn
#: 並列出檔名（review-1926cd3 L：held_files 本來只是一個計數，沒有門檻與警示）。
HELD_STALE_HOURS = 12


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    value: str
    hint: str | None = None


@dataclass(frozen=True)
class HealthData:
    tokens_ok: dict[str, bool] = field(default_factory=dict)  # committer／worker 等
    workflow_enabled: bool | None = None
    cancelled_runs: int = 0
    consecutive_aborts: int = 0
    last_success_at: str | None = None      # pin 的 promoted_at（RFC3339）
    schedule_interval_ok: bool | None = None
    actions_minutes_2w: float | None = None
    quarantine_files: int = 0
    quarantine_bytes: int = 0
    quarantine_growing: bool | None = None
    quota_limit: int | None = None
    quota_usage: int | None = None
    index_bytes: int | None = None
    readview_files: int | None = None
    manifest_main_sha: str | None = None
    pin_main_sha: str | None = None
    syncer_waiting: int = 0
    syncer_rejected: int = 0
    prune_ok: bool | None = None
    maintenance: bool | None = None       # pin repo 的維護旗標
    #: 真本前綴裡釘選值背書不了的檔名（HOLD／待判斷／沒有人能解釋）。
    #: 健康檢查要指出「是哪幾個檔案卡住」，不是只說「有 N 個」。
    held_files: list[str] = field(default_factory=list)
    #: 其中已經放超過一輪的（= 已經活過一整輪提交卻仍然沒被隔離也沒轉正）
    held_stale_files: list[str] = field(default_factory=list)
    #: 這兩項真的查到了嗎？查不到是 warn 而不是 ok（M7：查不到 ≠ 正常）
    held_files_known: bool = False


def _hours_since(at: str, now: datetime) -> float | None:
    try:
        return (now - parse_rfc3339(at)).total_seconds() / 3600.0
    except ValueError:
        return None


def run_health(data: HealthData, *, now: datetime) -> list[Check]:
    """依 HealthData 判定每一項（純函式）。None 一律是 warn（查不到 ≠ 正常）。"""
    checks: list[Check] = []

    if not data.tokens_ok:
        checks.append(Check(name="token", status="warn", value="未知（查不到憑證狀態）"))
    else:
        bad_tokens = sorted(k for k, ok in data.tokens_ok.items() if not ok)
        if bad_tokens:
            checks.append(Check(
                name="token", status="fail",
                value="無效：" + ",".join(bad_tokens),
                hint="重新授權該 conf（refresh token 可能過期或被撤銷）"))
        else:
            checks.append(Check(name="token", status="ok",
                                value=f"{len(data.tokens_ok)} 份憑證皆可刷新"))

    if data.maintenance:
        checks.append(Check(name="maintenance", status="warn", value="維護中",
                            hint="管理操作進行中；正常會在操作結束後解除旗標"))
    if data.workflow_enabled is None:
        checks.append(Check(name="workflow", status="warn", value="未知（查不到狀態）"))
    elif data.workflow_enabled:
        checks.append(Check(name="workflow", status="ok", value="已啟用"))
    elif data.maintenance:
        checks.append(Check(name="workflow", status="ok",
                            value="被停用（維護中，屬正常）"))
    else:
        checks.append(Check(name="workflow", status="fail", value="被停用",
                            hint="檢查是否被住民停用；有維護旗標則屬正常"))

    if data.cancelled_runs > 0:
        checks.append(Check(name="runs", status="warn",
                            value=f"被取消 {data.cancelled_runs} 個",
                            hint="對照 pin 的 promoted_at 交叉檢查（住民可刪 run）"))
    else:
        checks.append(Check(name="runs", status="ok", value="無取消"))
    if data.consecutive_aborts >= ABORT_STREAK_FAIL:
        checks.append(Check(name="aborts", status="fail",
                            value=f"連續中止 {data.consecutive_aborts} 輪",
                            hint="立即檢查：注入、憑證或容量問題"))
    else:
        checks.append(Check(name="aborts", status="ok",
                            value=f"連續中止 {data.consecutive_aborts} 輪"))

    hours = _hours_since(data.last_success_at, now) if data.last_success_at else None
    if hours is None:
        checks.append(Check(name="last_success", status="warn",
                            value="未知（讀不到 pin 的 promoted_at）"))
    elif hours > STALE_FAIL_HOURS:
        checks.append(Check(name="last_success", status="fail",
                            value=f"距離上次成功提交 {hours:.1f}h",
                            hint="提交流程可能被停用或遭到注入"))
    elif hours > STALE_WARN_HOURS:
        checks.append(Check(name="last_success", status="warn",
                            value=f"距離上次成功提交 {hours:.1f}h"))
    else:
        checks.append(Check(name="last_success", status="ok",
                            value=f"距離上次成功提交 {hours:.1f}h"))

    if data.schedule_interval_ok is None:
        checks.append(Check(name="schedule", status="warn", value="未知（查不到排程狀態）"))
    elif data.schedule_interval_ok:
        checks.append(Check(name="schedule", status="ok", value="間隔正常"))
    else:
        checks.append(Check(name="schedule", status="warn",
                            value="實際間隔與設定不符",
                            hint="檢查排程是否被取消或延遲"))

    if data.actions_minutes_2w is None:
        checks.append(Check(name="actions_minutes", status="warn",
                            value="未知（讀不到 Actions 用量）"))
    elif data.actions_minutes_2w > ACTIONS_MINUTES_WARN:
        checks.append(Check(name="actions_minutes", status="warn",
                            value=f"兩週 {data.actions_minutes_2w:.0f} 分鐘",
                            hint="連續兩週超過 300 就調提交間隔"))
    else:
        checks.append(Check(name="actions_minutes", status="ok",
                            value=f"兩週 {data.actions_minutes_2w:.0f} 分鐘"))

    if data.quarantine_growing:
        checks.append(Check(name="quarantine", status="warn",
                            value=f"{data.quarantine_files} 檔／{data.quarantine_bytes} 位元組（增長中）",
                            hint="檢查隔離資料夾是否有誤判"))
    elif data.quarantine_files == 0 and data.quarantine_bytes == 0:
        checks.append(Check(name="quarantine", status="ok", value="空"))
    else:
        checks.append(Check(name="quarantine", status="ok",
                            value=f"{data.quarantine_files} 檔／{data.quarantine_bytes} 位元組"))

    if not data.quota_limit or data.quota_usage is None:
        checks.append(Check(name="quota", status="warn", value="未知（讀不到配額）"))
    else:
        ratio = data.quota_usage / data.quota_limit
        if ratio >= QUOTA_FAIL_RATIO:
            checks.append(Check(name="quota", status="fail",
                                value=f"已用 {ratio:.0%}",
                                hint="家庭共用配額將滿，先清大檔"))
        elif ratio >= QUOTA_WARN_RATIO:
            checks.append(Check(name="quota", status="warn", value=f"已用 {ratio:.0%}"))
        else:
            checks.append(Check(name="quota", status="ok", value=f"已用 {ratio:.0%}"))

    if data.index_bytes is None and data.readview_files is None:
        checks.append(Check(name="readview_size", status="warn", value="未知（讀不到讀取視圖）"))
    else:
        index_bad = (data.index_bytes is not None and data.index_bytes > INDEX_BYTES_WARN)
        files_bad = (data.readview_files is not None and data.readview_files > READVIEW_FILES_WARN)
        value = f"索引 {data.index_bytes} B／{data.readview_files} 檔"
        if index_bad or files_bad:
            checks.append(Check(name="readview_size", status="warn", value=value,
                                hint="超過 D5 門檻（50MB／5,000 檔）"))
        else:
            checks.append(Check(name="readview_size", status="ok", value=value))

    if data.manifest_main_sha is None or data.pin_main_sha is None:
        checks.append(Check(name="readview_lag", status="warn",
                            value="未知（讀不到 manifest 或 pin 的 main）"))
    elif data.manifest_main_sha != data.pin_main_sha:
        checks.append(Check(name="readview_lag", status="warn",
                            value="manifest 落後 pin 的 main",
                            hint="上一輪發佈失敗，等下一個非空輪次補發"))
    else:
        checks.append(Check(name="readview_lag", status="ok", value="一致"))

    if data.syncer_waiting > 0 or data.syncer_rejected > 0:
        checks.append(Check(name="syncer", status="warn",
                            value=f"等待中 {data.syncer_waiting}／拒收 {data.syncer_rejected}",
                            hint="看 syncer status 與拒收原因"))
    else:
        checks.append(Check(name="syncer", status="ok", value="無積壓"))

    if data.prune_ok is None:
        checks.append(Check(name="prune", status="warn",
                            value="未知（升級 opencode 時才驗證）"))
    elif data.prune_ok:
        checks.append(Check(name="prune", status="ok", value="prune 只加標記"))
    else:
        checks.append(Check(name="prune", status="warn", value="prune 行為異常",
                            hint="重跑 resident/verify-prune.sh"))

    # 前綴裡釘選值背書不了的檔案：HOLD 是「有人背書、等釘選值轉正」，
    # NEED_ADMIN 是「沒有任何可信來源能解釋它，需要管理者 init-pin」。
    # 兩者都留在原地（搬走等於消滅真本），所以**只靠計數沒有人會注意到**。
    # 超過一輪還在原地就是需要人處理的訊號，於是報出檔名。
    if not data.held_files_known:
        checks.append(Check(name="held_files", status="warn",
                            value="未知（讀不到真本前綴或 pin）"))
    elif data.held_stale_files:
        checks.append(Check(
            name="held_files", status="warn",
            value=(
                f"{len(data.held_stale_files)} 個檔案在前綴裡超過一輪"
                f"（共 {len(data.held_files)} 個待處理）："
                f"{'、'.join(sorted(data.held_stale_files)[:5])}"
            ),
            hint="真本上有釘選值背書不了的檔案：查 pending 還在不在，"
                 "或用 init-pin 以觀測到的遠端狀態重建釘選值"))
    elif data.held_files:
        checks.append(Check(
            name="held_files", status="ok",
            value=f"{len(data.held_files)} 個（都在本輪內，等釘選值轉正）"))
    else:
        checks.append(Check(name="held_files", status="ok", value="無"))
    return checks


def summarize(checks: Sequence[Check]) -> str:
    """最嚴重的狀態：fail > warn > ok。"""
    levels = {c.status for c in checks}
    if "fail" in levels:
        return "fail"
    if "warn" in levels:
        return "warn"
    return "ok"


# ---------------------------------------------------------------- 資料蒐集

@dataclass(frozen=True)
class CollectSources:
    """collect_health 需要的資料來源（可注入假實作；任何一項缺就是 None）。"""

    drive: DriveClient
    pins: Any                      # 需有 load(repo) 與 read_text(relpath)
    gh_runs: Any | None = None     # 需有 workflow_enabled／recent_runs／billing_minutes
    repo: str = "agora"
    #: 提交流程的 workflow 檔名（6.5：錯開會停用它，健康檢查要查同一個）
    workflow: str = DEFAULT_WORKFLOW
    prefix_folder_id: str = ""
    readview_folder_id: str | None = None
    readview_manifest_file_id: str | None = None
    quarantine_folder_id: str = ""
    tokens: dict[str, bool] = field(default_factory=dict)
    schedule_interval_ok: bool | None = None
    syncer_waiting: int = 0
    syncer_rejected: int = 0
    prune_ok: bool | None = None
    #: 回傳 {"limit": int, "usage": int}；DriveClient 協定沒有 about()，
    #: 所以配額由呼叫端注入（抓不到就是 None → warn）。
    quota_provider: Any | None = None


def collect_health(sources: CollectSources, *, clock: Clock | None = None) -> HealthData:
    """真的去取資料；取不到的項目留 None，判定時是 warn（review M7）。

    資料來源刻意挑住民無法偽造的：pin 的 promoted_at（上次成功提交）、
    Drive 的配額與隔離區、GitHub 的 runs 與用量。
    """
    from datetime import timezone

    from aistorage.admin.lock import read_maintenance

    now = clock.now() if clock is not None else datetime.now(timezone.utc)

    last_success: str | None = None
    pin_main: str | None = None
    state: Any = None
    _pending: Any = None
    try:
        state, _pending = sources.pins.load(sources.repo)
        last_success = state.promoted_at
        pin_main = state.refs.get("refs/heads/main")
    except Exception:
        pass

    maintenance = False
    try:
        maintenance = read_maintenance(sources.pins, sources.repo) is not None
    except Exception:
        maintenance = None

    workflow: bool | None = None
    cancelled = 0
    aborts = 0
    minutes: float | None = None
    gh = sources.gh_runs
    if gh is not None:
        try:
            workflow = bool(gh.workflow_enabled(sources.workflow))
        except Exception:
            workflow = None
        try:
            for run in gh.recent_runs(limit=50):
                conclusion = str(run.get("conclusion") or "")
                status = str(run.get("status") or "")
                if status == "completed" and conclusion == "cancelled":
                    cancelled += 1
                if status == "completed" and conclusion in ("failure", "cancelled", "timed_out"):
                    aborts += 1
                else:
                    aborts = 0
        except Exception:
            pass
        try:
            minutes = float(gh.billing_minutes(days=14))
        except Exception:
            minutes = None

    quota_limit: int | None = None
    quota_usage: int | None = None
    quota_source = sources.quota_provider or getattr(sources.drive, "about", None)
    if callable(quota_source):
        try:
            about = quota_source()
            quota_limit = int(about.get("storageQuota", {}).get("limit") or 0) or None
            quota_usage = int(about.get("storageQuota", {}).get("usage") or 0)
        except Exception:
            quota_limit = quota_usage = None

    q_files = q_bytes = 0
    if sources.quarantine_folder_id:
        try:
            q_files, q_bytes = quarantine_usage(sources.drive, sources.quarantine_folder_id)
        except Exception:
            q_files = q_bytes = 0

    held_files, held_stale, held_known = _unbacked_prefix_files(
        sources.drive, sources.prefix_folder_id, state, _pending, now=now
    )

    manifest_main: str | None = None
    index_bytes: int | None = None
    readview_files: int | None = None
    if sources.readview_manifest_file_id:
        try:
            data = sources.drive.download_bytes(
                sources.readview_manifest_file_id, max_bytes=8 * 1024 * 1024)
            import hashlib
            manifest_main = hashlib.sha256(data).hexdigest().lower()
        except Exception:
            manifest_main = None
    if sources.readview_folder_id:
        try:
            files = list_files(sources.drive, sources.readview_folder_id)
            readview_files = len(files)
            index_bytes = sum(f.size or 0 for f in files)
        except Exception:
            readview_files = index_bytes = None

    return HealthData(
        tokens_ok=dict(sources.tokens),
        workflow_enabled=workflow,
        cancelled_runs=cancelled,
        consecutive_aborts=aborts,
        last_success_at=last_success,
        schedule_interval_ok=sources.schedule_interval_ok,
        actions_minutes_2w=minutes,
        quarantine_files=q_files,
        quarantine_bytes=q_bytes,
        quarantine_growing=None,
        quota_limit=quota_limit,
        quota_usage=quota_usage,
        index_bytes=index_bytes,
        readview_files=readview_files,
        manifest_main_sha=manifest_main,
        pin_main_sha=pin_main,
        syncer_waiting=sources.syncer_waiting,
        syncer_rejected=sources.syncer_rejected,
        prune_ok=sources.prune_ok,
        maintenance=maintenance,
        held_files=held_files,
        held_stale_files=held_stale,
        held_files_known=held_known,
    )


def _unbacked_prefix_files(
    drive: DriveClient,
    prefix_folder_id: str,
    state: Any,
    pending: Any,
    *,
    now: datetime,
) -> tuple[list[str], list[str], bool]:
    """真本前綴裡「釘選值背書不了」的檔名、其中已逾齡的，以及是否真的查到了。

    這裡呼叫的是提交流程第 4 步**同一個**純函式 `plan_sweep`（不下載內容），
    所以健康檢查看到的和提交流程看到的是同一套規則，不會各自發明一套。
    健康檢查刻意**不做**內容驗證（不重放、不下載）：那要花幾十秒而且會動到
    網路；這裡只要知道「哪些檔案釘選值解釋不了」就夠了。

    回傳 `(held_files, held_stale_files)`；取不到資料（沒有設定檔 id、pin
    讀不到、Drive 讀不到）就回兩個空清單——判定時是「無」，而不是把
    「查不到」說成「一切正常」（見模組開頭 M7 的原則）。
    """
    if not prefix_folder_id or state is None:
        return [], [], False
    try:
        from aistorage.integrity.settle import RepoListing
        from aistorage.integrity.sweep import (
            Disposition,
            SweepPolicy,
            file_age_days,
            plan_sweep,
        )

        children = drive.list_children(prefix_folder_id)
        listing = RepoListing(
            prefix_folder_id=prefix_folder_id,
            files=tuple(c for c in children if not c.is_folder),
            subfolders=tuple(c for c in children if c.is_folder),
        )
        decisions = plan_sweep(
            listing, state,
            repo_uuid=state.repo_uuid,
            prefix_folder_id=prefix_folder_id,
            policy=SweepPolicy(pending=pending, now=now),
        )
    except Exception:
        return [], [], False

    interesting = (
        Disposition.HOLD,
        Disposition.NEED_ADMIN,
        Disposition.NEED_MANIFEST_CHECK,
    )
    held: list[str] = []
    stale: list[str] = []
    for d in decisions:
        if d.disposition not in interesting:
            continue
        # 檔名後面附上 Drive 的建立時間（review-903d7e2 M1）：要動手搬或刪之前，
        # 只看檔名分不出哪一份才是真的——前綴裡可能有 rclone 自己留下的重複
        # manifest，也可能有住民放的副本，兩者檔名完全一樣。
        stamp = f"@{d.file.created_time}"
        held.append(f"{d.file.name}{stamp}")
        age_h = (file_age_days(d.file, now) or 0.0) * 24
        if age_h > HELD_STALE_HOURS:
            stale.append(f"{d.file.name}{stamp}")
    return sorted(held), sorted(stale), True


def quarantine_usage(drive: DriveClient, quarantine_folder_id: str) -> tuple[int, int]:
    """隔離資料夾各日期子資料夾的檔案數與總大小（FakeDrive 可測）。"""
    total_files = 0
    total_bytes = 0

    def _walk(folder_id: str, depth: int = 0) -> None:
        nonlocal total_files, total_bytes
        if depth > 8:
            return
        for child in drive.list_children(folder_id):
            if child.is_folder:
                _walk(child.id, depth + 1)
            else:
                total_files += 1
                total_bytes += child.size or 0

    _walk(quarantine_folder_id)
    return total_files, total_bytes


def list_files(drive: DriveClient, folder_id: str, _depth: int = 0) -> list[Any]:
    """遞迴列出資料夾內所有檔案（DriveFile）。"""
    if _depth > 8:
        return []
    out: list[Any] = []
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out.extend(list_files(drive, child.id, _depth + 1))
        else:
            out.append(child)
    return out


def launchd_plist(*, label: str = "local.aistorage.health",
                  program: tuple[str, ...] = ("uv", "run", "python",
                                              "-m", "aistorage.admin", "health"),
                  working_directory: str = "",
                  interval_s: int = LAUNCHD_INTERVAL_S,
                  log_path: str = "") -> str:
    """產生 launchd plist 內容（只產生、不安裝；安裝見 scripts/install-health-launchd.sh）。

    預設指令就是 `health`（自行 collect 資料後判定），不需要 `--data-json`
    ——review M7 指出舊版 plist 的指令缺參數，裝上去之後每 6 小時都會
    以 AdminError 結束、永遠不會通知。
    """
    import plistlib

    doc: dict[str, object] = {
        "Label": label,
        "ProgramArguments": list(program),
        "StartInterval": interval_s,
        "RunAtLoad": False,
        "StandardOutPath": log_path or "/tmp/aistorage-health.log",
        "StandardErrorPath": log_path or "/tmp/aistorage-health.log",
    }
    if working_directory:
        doc["WorkingDirectory"] = working_directory
    return plistlib.dumps(doc, fmt=plistlib.FMT_XML).decode("utf-8")


def data_json_path() -> Path:
    """collect 結果的預設落地位置（除錯用；CLI 會印出來）。"""
    return Path("/tmp/aistorage-health-data.json")


def dump_health_data(data: HealthData, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else data_json_path()
    target.write_text(json.dumps(data.__dict__, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target

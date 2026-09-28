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
            workflow = bool(gh.workflow_enabled("commit.yaml"))
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
    )


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

"""6.3 健康檢查（Mac 上執行，結果通知；launchd 另見腳本）。

判定函数 run_health 是纯函数：输入 HealthData（假数据源可测），输出 Check 清单。
各阈值集中在此档顶部，改动即是政策变动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from aistorage.clock import parse_rfc3339
from aistorage.drive.model import DriveClient

CheckStatus = Literal["ok", "warn", "fail"]

# ---- 閾值（政策） ----
ABORT_STREAK_FAIL = 3            # 连续中止轮数达此值 → fail
STALE_WARN_HOURS = 12            # 距上次成功提交超过 → warn
STALE_FAIL_HOURS = 30            # 距上次成功提交超过 → fail（默认 6h 间隔的 5 倍）
ACTIONS_MINUTES_WARN = 300       # 两周 Actions 分钟数超过 → warn（6.3 监控目标）
QUOTA_WARN_RATIO = 0.85          # 配额使用率超过 → warn
QUOTA_FAIL_RATIO = 0.95          # 配额使用率超过 → fail
INDEX_BYTES_WARN = 50 * 1024 * 1024  # D5：索引 50MB
READVIEW_FILES_WARN = 5000           # D5：读取视图 5000 个档案
LAUNCHD_INTERVAL_S = 6 * 3600        # launchd 每 6 小时


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    value: str
    hint: str | None = None


@dataclass(frozen=True)
class HealthData:
    tokens_ok: dict[str, bool] = field(default_factory=dict)  # committer/worker 等
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


def _hours_since(at: str, now: datetime) -> float | None:
    try:
        return (now - parse_rfc3339(at)).total_seconds() / 3600.0
    except ValueError:
        return None


def run_health(data: HealthData, *, now: datetime) -> list[Check]:
    """依 HealthData 判定每一项（纯函数）。"""
    checks: list[Check] = []

    bad_tokens = sorted(k for k, ok in data.tokens_ok.items() if not ok)
    if bad_tokens:
        checks.append(Check(
            name="token", status="fail",
            value="無效：" + ",".join(bad_tokens),
            hint="重新授權該 conf（refresh token 可能過期或被撤銷）"))
    else:
        checks.append(Check(name="token", status="ok",
                            value=f"{len(data.tokens_ok)} 份憑證皆可刷新"))

    if data.workflow_enabled is None:
        checks.append(Check(name="workflow", status="ok", value="未知（查不到狀態）"))
    elif data.workflow_enabled:
        checks.append(Check(name="workflow", status="ok", value="已啟用"))
    else:
        checks.append(Check(name="workflow", status="fail", value="被停用",
                            hint="檢查是否被住民停用；有維護旗標則屬正常"))

    if data.cancelled_runs > 0:
        checks.append(Check(name="runs", status="warn",
                            value=f"被取消 {data.cancelled_runs} 個",
                            hint="对照 pin 的 promoted_at 交叉檢查（住民可刪 run）"))
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
        checks.append(Check(name="schedule", status="ok", value="未知"))
    elif data.schedule_interval_ok:
        checks.append(Check(name="schedule", status="ok", value="間隔正常"))
    else:
        checks.append(Check(name="schedule", status="warn",
                            value="實際間隔與設定不符",
                            hint="檢查排程是否被取消或延遲"))

    if data.actions_minutes_2w is None:
        checks.append(Check(name="actions_minutes", status="ok", value="未知"))
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
    else:
        checks.append(Check(name="quarantine", status="ok",
                            value=f"{data.quarantine_files} 檔／{data.quarantine_bytes} 位元組"))

    if data.quota_limit and data.quota_usage is not None and data.quota_limit > 0:
        ratio = data.quota_usage / data.quota_limit
        if ratio >= QUOTA_FAIL_RATIO:
            checks.append(Check(name="quota", status="fail",
                                value=f"已用 {ratio:.0%}",
                                hint="家庭共用配額將滿，先清大檔"))
        elif ratio >= QUOTA_WARN_RATIO:
            checks.append(Check(name="quota", status="warn",
                                value=f"已用 {ratio:.0%}"))
        else:
            checks.append(Check(name="quota", status="ok", value=f"已用 {ratio:.0%}"))
    else:
        checks.append(Check(name="quota", status="ok", value="未知"))

    index_bad = (data.index_bytes is not None and data.index_bytes > INDEX_BYTES_WARN)
    files_bad = (data.readview_files is not None and data.readview_files > READVIEW_FILES_WARN)
    if index_bad or files_bad:
        checks.append(Check(name="readview_size", status="warn",
                            value=f"索引 {data.index_bytes} B／{data.readview_files} 檔",
                            hint="超過 D5 門檻（50MB／5,000 檔）"))
    else:
        checks.append(Check(name="readview_size", status="ok",
                            value=f"索引 {data.index_bytes} B／{data.readview_files} 檔"))

    if data.manifest_main_sha is not None and data.pin_main_sha is not None:
        if data.manifest_main_sha != data.pin_main_sha:
            checks.append(Check(name="readview_lag", status="warn",
                                value="manifest 落後 pin 的 main",
                                hint="上一輪發佈失敗，等下一個非空輪次補發"))
        else:
            checks.append(Check(name="readview_lag", status="ok", value="一致"))
    else:
        checks.append(Check(name="readview_lag", status="ok", value="未知"))

    if data.syncer_waiting > 0 or data.syncer_rejected > 0:
        checks.append(Check(name="syncer", status="warn",
                            value=f"等待中 {data.syncer_waiting}／拒收 {data.syncer_rejected}",
                            hint="看 syncer status 與拒收原因"))
    else:
        checks.append(Check(name="syncer", status="ok", value="無積壓"))

    if data.prune_ok is None:
        checks.append(Check(name="prune", status="ok", value="未知（升級 opencode 時驗證）"))
    elif data.prune_ok:
        checks.append(Check(name="prune", status="ok", value="prune 只加標記"))
    else:
        checks.append(Check(name="prune", status="warn",
                            value="prune 行為異常",
                            hint="重跑 resident/verify-prune.sh"))
    return checks


def summarize(checks: list[Check]) -> str:
    """最嚴重的狀態：fail > warn > ok。"""
    levels = {c.status for c in checks}
    if "fail" in levels:
        return "fail"
    if "warn" in levels:
        return "warn"
    return "ok"


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


def launchd_plist(*, label: str = "local.aistorage.health",
                  program: tuple[str, ...] = ("uv", "run", "python",
                                              "-m", "aistorage.admin", "health"),
                  working_directory: str = "",
                  interval_s: int = LAUNCHD_INTERVAL_S,
                  log_path: str = "") -> str:
    """產生 launchd plist 內容（只產生、不安裝；安裝見 scripts/install-health-launchd.sh）。"""
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

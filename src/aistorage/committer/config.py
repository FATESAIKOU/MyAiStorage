"""AiStorage 提交流程設定管理模組。

依據規格：
- docs/impl/group3-modules.md 第 7 節
- design D2（13 步提交流程、環境變數只傳路徑）
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from aistorage.errors import ReadError
from aistorage.integrity.sweep import PrefixLevel

DEFAULT_MAX_GIT_BUNDLES = 20
DEFAULT_MAX_GC_PER_RUN = 200
DEFAULT_MAX_RAW_SIZE = 50 * 1024 * 1024  # 50 MiB
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_LEDGER_MONTHS = 3
DEFAULT_SCHEDULE_CRON = "7 */6 * * *"


@dataclass(frozen=True)
class CommitterConfig:
    """提交流程全域設定結構。"""

    repo: str
    repo_uuid: str
    repo_url: str
    prefix_folder_id: str
    quarantine_folder_id: str
    readview_folder_id: str | None = None
    pin_repo_url: str = ""
    identity_registry_path: str = "config/identity.example.json"
    max_git_bundles: int = DEFAULT_MAX_GIT_BUNDLES
    max_gc_per_run: int = DEFAULT_MAX_GC_PER_RUN
    max_raw_size: int = DEFAULT_MAX_RAW_SIZE
    quarantine_retention_days: int = DEFAULT_QUARANTINE_DAYS
    ledger_retention_months: int = DEFAULT_LEDGER_MONTHS
    prefix_levels: tuple[PrefixLevel, ...] = ()
    github_repository: str = ""
    schedule_cron: str = DEFAULT_SCHEDULE_CRON

    # 自環境變數傳入之憑證與金鑰路徑（不包含秘密原文）
    rclone_conf_path: Path | None = None
    pin_key_path: Path | None = None
    pin_known_hosts_path: Path | None = None

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> CommitterConfig:
        """載入提交流程設定檔並結合環境變數路徑。

        Args:
            config_path: JSON 設定檔路徑。
            env: 環境變數字典（預設為 os.environ）。

        Returns:
            CommitterConfig 執行個體。
        """
        current_env = env if env is not None else os.environ

        data: dict[str, Any] = {}
        if config_path:
            p = Path(config_path)
            if not p.is_file():
                raise ReadError(f"找不到提交流程設定檔: {p}")
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                raise ReadError(f"提交流程設定檔解析失敗 ({p}): {e}") from e

        # 必填欄位檢查
        repo = data.get("repo", "agora")
        repo_uuid = data.get("repo_uuid", "")
        repo_url = data.get("repo_url", "")
        prefix_folder_id = data.get("prefix_folder_id", "")
        quarantine_folder_id = data.get("quarantine_folder_id", "")

        # 解析 prefix_levels
        raw_levels = data.get("prefix_levels", [])
        levels: list[PrefixLevel] = []
        for item in raw_levels:
            if isinstance(item, dict):
                levels.append(
                    PrefixLevel(
                        parent_id=item["parent_id"],
                        name=item["name"],
                        expected_id=item["expected_id"],
                    )
                )

        # 環境變數路徑注入
        rclone_conf_env = current_env.get("AISTORAGE_RCLONE_CONF")
        rclone_conf_path = Path(rclone_conf_env) if rclone_conf_env else None

        pin_key_env = current_env.get("AISTORAGE_PIN_KEY")
        pin_key_path = Path(pin_key_env) if pin_key_env else None

        pin_known_hosts_env = current_env.get("AISTORAGE_PIN_KNOWN_HOSTS")
        pin_known_hosts_path = Path(pin_known_hosts_env) if pin_known_hosts_env else None

        github_repo = current_env.get("GITHUB_REPOSITORY", data.get("github_repository", ""))

        return cls(
            repo=repo,
            repo_uuid=repo_uuid,
            repo_url=repo_url,
            prefix_folder_id=prefix_folder_id,
            quarantine_folder_id=quarantine_folder_id,
            readview_folder_id=data.get("readview_folder_id"),
            pin_repo_url=data.get("pin_repo_url", ""),
            identity_registry_path=data.get("identity_registry_path", "config/identity.example.json"),
            max_git_bundles=int(data.get("max_git_bundles", DEFAULT_MAX_GIT_BUNDLES)),
            max_gc_per_run=int(data.get("max_gc_per_run", DEFAULT_MAX_GC_PER_RUN)),
            max_raw_size=int(data.get("max_raw_size", DEFAULT_MAX_RAW_SIZE)),
            quarantine_retention_days=int(data.get("quarantine_retention_days", DEFAULT_QUARANTINE_DAYS)),
            ledger_retention_months=int(data.get("ledger_retention_months", DEFAULT_LEDGER_MONTHS)),
            prefix_levels=tuple(levels),
            github_repository=github_repo,
            schedule_cron=data.get("schedule_cron", DEFAULT_SCHEDULE_CRON),
            rclone_conf_path=rclone_conf_path,
            pin_key_path=pin_key_path,
            pin_known_hosts_path=pin_known_hosts_path,
        )

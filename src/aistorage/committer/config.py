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

import re
from aistorage.errors import ReadError
from aistorage.integrity.sweep import PrefixLevel

DEFAULT_MAX_GIT_BUNDLES = 20
DEFAULT_MAX_GC_PER_RUN = 200
DEFAULT_MAX_RAW_SIZE = 50 * 1024 * 1024  # 50 MiB
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_LEDGER_MONTHS = 3

_FOLDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class RepoConfig:
    """單一真本 repo 的設定（Agora／Foundry 各一份，group5-7 第 6.1 節）。

    兩者共用同一個 pin repo，釘選值以 repo 名稱分檔（`.pin/<repo>.json`）。
    """

    name: str
    uuid: str
    url: str
    prefix_folder_id: str
    quarantine_folder_id: str
    readview_folder_id: str | None = None
    readview_manifest_file_id: str | None = None
    readview_rebuild_epoch: int = 0
    largefiles: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict, *, defaults: dict | None = None) -> RepoConfig:
        d = dict(defaults or {})
        d.update(data)
        for required in ("uuid", "url", "prefix_folder_id", "quarantine_folder_id"):
            if not d.get(required):
                raise ValueError(f"repos.{name} 缺少必填欄位: {required}")
        for fid_key in ("prefix_folder_id", "quarantine_folder_id", "readview_folder_id"):
            value = d.get(fid_key)
            if value and not _FOLDER_ID_PATTERN.match(str(value)):
                raise ValueError(f"repos.{name} 不合法的 {fid_key}: {value!r}")
        return cls(
            name=name,
            uuid=str(d["uuid"]),
            url=str(d["url"]),
            prefix_folder_id=str(d["prefix_folder_id"]),
            quarantine_folder_id=str(d["quarantine_folder_id"]),
            readview_folder_id=d.get("readview_folder_id"),
            readview_manifest_file_id=d.get("readview_manifest_file_id"),
            readview_rebuild_epoch=int(d.get("readview_rebuild_epoch", 0)),
            largefiles=d.get("largefiles"),
        )


@dataclass(frozen=True)
class CommitterConfig:
    """提交流程全域設定結構。"""

    repo: str
    repo_uuid: str
    repo_url: str
    prefix_folder_id: str
    quarantine_folder_id: str
    identity_registry_path: str
    readview_folder_id: str | None = None
    #: 讀取視圖 manifest 的 Drive file id（4.3）。沒有它就跳過讀取視圖清掃（fail-open：
    #: 管理者還沒初始化發佈時，清掃沒有可信集合可用，動它等於把整個讀取視圖隔離掉）。
    readview_manifest_file_id: str | None = None
    #: 讀取視圖 manifest 的世代（4.5）。設定值大於 manifest 內的 rebuild_epoch 時，
    #: publisher 會做一次完整重建（格式改版、轉換器改版時用）。
    readview_rebuild_epoch: int = 0
    pin_repo_url: str = ""
    max_git_bundles: int = DEFAULT_MAX_GIT_BUNDLES
    max_gc_per_run: int = DEFAULT_MAX_GC_PER_RUN
    max_raw_size: int = DEFAULT_MAX_RAW_SIZE
    quarantine_retention_days: int = DEFAULT_QUARANTINE_DAYS
    ledger_retention_months: int = DEFAULT_LEDGER_MONTHS
    prefix_levels: tuple[PrefixLevel, ...] = ()
    github_repository: str = ""
    #: 多 repo 設定（group5-7 第 6.1 節）：只列 Agora 以外的 repo，依序處理
    #: （PM 決定 9：同一個 job 依序，只有收件匣有 artifact 才 clone Foundry）。
    repos: tuple[RepoConfig, ...] = ()

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

            # M6: 格式驗證
            fmt = data.get("format")
            if fmt != "aistorage.committer/v1":
                raise ValueError(f"不支援的設定檔格式標識: {repr(fmt)}（預期 'aistorage.committer/v1'）")

        # M6: 必填欄位檢查
        repo = data.get("repo", "agora")
        if not repo:
            raise ValueError("缺少必填欄位: repo")

        repo_uuid = data.get("repo_uuid")
        if not repo_uuid:
            raise ValueError("缺少必填欄位: repo_uuid")

        repo_url = data.get("repo_url")
        if not repo_url:
            raise ValueError("缺少必填欄位: repo_url")

        prefix_folder_id = data.get("prefix_folder_id")
        if not prefix_folder_id or not _FOLDER_ID_PATTERN.match(prefix_folder_id):
            raise ValueError(f"缺少或不合法的 prefix_folder_id: {repr(prefix_folder_id)}")

        quarantine_folder_id = data.get("quarantine_folder_id")
        if not quarantine_folder_id or not _FOLDER_ID_PATTERN.match(quarantine_folder_id):
            raise ValueError(f"缺少或不合法的 quarantine_folder_id: {repr(quarantine_folder_id)}")

        identity_registry_path = data.get("identity_registry_path")
        if not identity_registry_path:
            raise ValueError("缺少必填欄位: identity_registry_path")

        readview_folder_id = data.get("readview_folder_id")
        if readview_folder_id and not _FOLDER_ID_PATTERN.match(readview_folder_id):
            raise ValueError(f"不合法的 readview_folder_id: {repr(readview_folder_id)}")

        readview_manifest_file_id = data.get("readview_manifest_file_id")
        if readview_manifest_file_id and not _FOLDER_ID_PATTERN.match(readview_manifest_file_id):
            raise ValueError(
                f"不合法的 readview_manifest_file_id: {repr(readview_manifest_file_id)}"
            )

        readview_rebuild_epoch = data.get("readview_rebuild_epoch", 0)
        if not isinstance(readview_rebuild_epoch, int) or readview_rebuild_epoch < 0:
            raise ValueError(
                f"不合法的 readview_rebuild_epoch: {repr(readview_rebuild_epoch)}（要非負整數）"
            )

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

        # 多 repo（Foundry）：`repos: {foundry: {...}}`，共用 pin repo
        repos: list[RepoConfig] = []
        raw_repos = data.get("repos") or {}
        if isinstance(raw_repos, dict):
            for repo_name, repo_data in raw_repos.items():
                repos.append(
                    RepoConfig.from_dict(
                        repo_name,
                        repo_data,
                        defaults={
                            "readview_manifest_file_id": data.get(
                                f"readview_manifest_file_id_{repo_name}"
                            ),
                        },
                    )
                )
        elif raw_repos:
            raise ValueError("repos 必須是物件（{名稱: {...}}）")
        # `repos` 只列「額外」的 repo（Agora 由上層欄位描述），順序＝處理順序
        if any(r.name == repo for r in repos):
            raise ValueError(f"repos 不該再列出 agora（{repo!r}），它由上層欄位描述")

        return cls(
            repo=repo,
            repo_uuid=repo_uuid,
            repo_url=repo_url,
            prefix_folder_id=prefix_folder_id,
            quarantine_folder_id=quarantine_folder_id,
            identity_registry_path=identity_registry_path,
            readview_folder_id=readview_folder_id,
            readview_manifest_file_id=readview_manifest_file_id,
            readview_rebuild_epoch=readview_rebuild_epoch,
            pin_repo_url=data.get("pin_repo_url", ""),
            max_git_bundles=int(data.get("max_git_bundles", DEFAULT_MAX_GIT_BUNDLES)),
            max_gc_per_run=int(data.get("max_gc_per_run", DEFAULT_MAX_GC_PER_RUN)),
            max_raw_size=int(data.get("max_raw_size", DEFAULT_MAX_RAW_SIZE)),
            quarantine_retention_days=int(data.get("quarantine_retention_days", DEFAULT_QUARANTINE_DAYS)),
            ledger_retention_months=int(data.get("ledger_retention_months", DEFAULT_LEDGER_MONTHS)),
            prefix_levels=tuple(levels),
            repos=tuple(repos),
            github_repository=github_repo,
            rclone_conf_path=rclone_conf_path,
            pin_key_path=pin_key_path,
            pin_known_hosts_path=pin_known_hosts_path,
        )

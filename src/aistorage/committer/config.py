"""AiStorage 提交流程設定管理模組。

依據規格：
- docs/impl/group3-modules.md 第 7 節
- design D2（13 步提交流程、環境變數只傳路徑）
"""

from __future__ import annotations

from dataclasses import dataclass
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
#: 每個 profile 能同時掛著的未結預留（`reserved` 且沒有後續快照）數量上限
#: （review-2bc0785 M2，review-1926cd3-142fd04 M3 改成跨輪累計）。**只接受正整數**。
DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE = 20
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_LEDGER_MONTHS = 3
#: `.github/workflows/committer.yml`（3.1 建的骨架；錯開要停用的就是它）
DEFAULT_COMMITTER_WORKFLOW = "committer.yml"

_FOLDER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class CommitterConfig:
    """**一個**儲存要素的提交流程設定（期 1 只有 Agora，ADR 0009）。

    ADR 0009：寫入閘門是每個實體各一套。之後別的實體若需要閘門，就用**另一份**
    設定檔（自己的 repo／uuid／url／前綴／隔離區／讀取視圖）＋另一個 workflow
    跑同一套程式——不要在一次執行裡分派多個 repo（那正是被移除的多 repo 管線）。
    """

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
    #: 每個 profile 能同時掛著的**未結預留**（`status=reserved` 且還沒有後續快照）
    #: 數量上限（review-2bc0785 M2；review-1926cd3-142fd04 M3 從「每輪」改成跨輪
    #: 累計，直接從真本算，不留帳本）。任何 profile 都能為任何一份既有快照送接續，
    #: 沒有這個上限時讀取視圖要發佈的閱讀版數量沒有邊界。**只接受正整數**。
    max_open_reservations_per_profile: int = (
        DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE)
    quarantine_retention_days: int = DEFAULT_QUARANTINE_DAYS
    ledger_retention_months: int = DEFAULT_LEDGER_MONTHS
    prefix_levels: tuple[PrefixLevel, ...] = ()
    github_repository: str = ""
    #: 提交流程的 workflow 檔名（`.github/workflows/` 底下那個檔）。
    #: 6.5 的錯開要停用它；名字寫錯（例如指向不存在的檔）時 `gh workflow disable`
    #: 會直接失敗，於是整個管理操作根本跑不起來，所以它必須由設定檔帶著走，
    #: 不能在程式裡散落硬編碼。
    committer_workflow: str = DEFAULT_COMMITTER_WORKFLOW

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

        # 預留數量上限（review-1926cd3-142fd04 M3）：**只接受正整數**。0 或負數
        # 在舊版代表「不設上限」，那等於一個打錯字就默默關掉保護，所以改成載入時
        # 就報錯（review-1926cd3-142fd04 建議）。
        max_open_reservations = data.get(
            "max_open_reservations_per_profile",
            DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE)
        if (isinstance(max_open_reservations, bool)
                or not isinstance(max_open_reservations, int)
                or max_open_reservations <= 0):
            raise ValueError(
                f"max_open_reservations_per_profile 必須是正整數: "
                f"{max_open_reservations!r}（0 與負數**不**代表「不設上限」，"
                f"要放寬請直接給一個更大的數）"
            )
        # 舊欄位（每輪限速版）已經被這個取代：留著不報錯的話，設定檔會安靜地退回
        # 預設值，上限看起來有設、其實沒生效。照 `_foundry` 那條的作法直接擋。
        if "max_links_per_profile_per_round" in data:
            raise ValueError(
                "設定檔還有舊欄位 `max_links_per_profile_per_round`（每輪限速版）。"
                "它已被 `max_open_reservations_per_profile`（每個 profile 的未結"
                "預留上限，跨輪累計）取代，請改名並確認數值——兩個欄位的意義不同，"
                "直接沿用舊值會讓上限變成另一件事。"
            )

        # ADR 0009：設定檔只描述**一個**實體（期 1 是 Agora）。舊的設定檔帶著
        # `repos: {foundry: …}`（多 repo 的提交流程）時**必須報錯**，不能默默忽略：
        # 默默忽略的後果是「以為還在跑的 Foundry 其實從期 1 開始就沒被處理過」，
        # 而沒有任何人會發現。舊的 `readview_*_foundry` 欄位一起擋。
        if "repos" in data:
            # 以「有沒有這個鍵」判斷，不是「值是否為空」：空物件一樣是多 repo 時代
            # 的殘留，放著只會讓人以為 Foundry 還有設定。
            stale_repos = data["repos"]
            listed = (
                ", ".join(sorted(stale_repos)) if isinstance(stale_repos, dict) and stale_repos
                else repr(stale_repos)
            )
            raise ValueError(
                f"設定檔還有多 repo 的 `repos` 區塊（{listed}），但提交流程只處理一個"
                "實體（ADR 0009：Foundry 改成 Drive 共享資料夾＋GitHub，產出登錄不再"
                "經過 Agora 的收件匣）。請刪掉 `repos`，以及同一個設定檔裡的 "
                "`readview_folder_id_foundry`／`readview_manifest_file_id_foundry`；"
                "要為另一個實體跑閘門就用**另一份設定檔**（欄位相同、值不同）。"
            )
        stale_foundry_keys = [k for k in data if k.endswith("_foundry")]
        if stale_foundry_keys:
            raise ValueError(
                f"設定檔還有多 repo 時代的欄位（{', '.join(sorted(stale_foundry_keys))}），"
                "請刪除（ADR 0009：只有 Agora 有讀取視圖）"
            )

        # 6.5：錯開要 `gh workflow disable <name>`，名字必須是 workflow 檔名本身。
        # 寫成路徑或別的副檔名時 gh 才會在管理操作跑到一半才失敗。
        committer_workflow = str(
            data.get("committer_workflow") or DEFAULT_COMMITTER_WORKFLOW)
        if ("/" in committer_workflow or "\\" in committer_workflow
                or not committer_workflow.endswith((".yml", ".yaml"))):
            raise ValueError(
                f"committer_workflow 必須是 .github/workflows/ 底下的檔名: "
                f"{repr(committer_workflow)}"
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
            max_open_reservations_per_profile=max_open_reservations,
            quarantine_retention_days=int(data.get("quarantine_retention_days", DEFAULT_QUARANTINE_DAYS)),
            ledger_retention_months=int(data.get("ledger_retention_months", DEFAULT_LEDGER_MONTHS)),
            prefix_levels=tuple(levels),
            github_repository=github_repo,
            committer_workflow=committer_workflow,
            rclone_conf_path=rclone_conf_path,
            pin_key_path=pin_key_path,
            pin_known_hosts_path=pin_known_hosts_path,
        )

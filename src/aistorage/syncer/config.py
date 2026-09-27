"""同步器的設定（全部**只以路徑引用**，值不進 log）。

容器內的預設（由 resident/image/entrypoint.sh 設定環境變數）：

| 設定 | 環境變數 | 預設 |
|---|---|---|
| opencode API | `AISTORAGE_OPENCODE_URL` | `http://127.0.0.1:4096` |
| opencode 工作目錄 | `AISTORAGE_OPENCODE_DIR` | `/work` |
| 收件匣（Drive 憑證） | `AISTORAGE_RCLONE_CONF` | `/tmp/aistorage/rclone.conf` |
| 收件匣 folder id | `AISTORAGE_INBOX_FOLDER_ID` 或 `reader.json` 的 `inbox_folder_ids[profile]` | — |
| 讀取設定（manifest id） | `AISTORAGE_READER_CONFIG` | `/secrets/reader.json` |
| 讀取身分（SA 金鑰） | `AISTORAGE_SA_KEY` | `/secrets/sa-reader.json` |
| 簽章金鑰 | `AISTORAGE_SIGNING_KEY` | `/secrets/signing.key` |
| profile | `AISTORAGE_PROFILE` | `mac-opencode` |
| 狀態檔 | `AISTORAGE_SYNC_STATE` | `/work/.aistorage/sync-state.json` |
| 同步間隔（daemon） | `AISTORAGE_SYNC_INTERVAL` | 600 秒 |
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping

from aistorage.syncer.core import Signer
from aistorage.syncer.opencode_api import DEFAULT_BASE_URL, DEFAULT_DIRECTORY
from aistorage.syncer.state import DEFAULT_STATE_PATH

DEFAULT_PROFILE = "mac-opencode"
DEFAULT_INTERVAL_S = 600
DEFAULT_RCLONE_CONF = "/tmp/aistorage/rclone.conf"
DEFAULT_READER_CONFIG = "/secrets/reader.json"
DEFAULT_SA_KEY = "/secrets/sa-reader.json"
DEFAULT_SIGNING_KEY = "/secrets/signing.key"
DEFAULT_GH_PAT = "/secrets/gh-pat-actions.txt"


class ConfigError(RuntimeError):
    """同步器設定缺少或不可用。"""


@dataclass(frozen=True)
class SyncerConfig:
    """同步器需要的全部設定。"""

    profile: str = DEFAULT_PROFILE
    base_url: str = DEFAULT_BASE_URL
    directory: str = DEFAULT_DIRECTORY
    rclone_conf: str = DEFAULT_RCLONE_CONF
    reader_config: str = DEFAULT_READER_CONFIG
    sa_key: str = DEFAULT_SA_KEY
    signing_key: str = DEFAULT_SIGNING_KEY
    state_path: Path = DEFAULT_STATE_PATH
    interval_s: int = DEFAULT_INTERVAL_S
    inbox_folder_id: str | None = None
    gh_pat: str | None = None          # 5.3 觸發提交流程（只以路徑／內容持有）
    gh_pat_path: str | None = None     # 檔案路徑
    repo: str | None = None            # 5.3 workflow_dispatch 的目標 repo
    workflow: str = "committer.yml"

    @classmethod
    def load(cls, *, env: Mapping[str, str] | None = None,
             require_inbox: bool = True) -> SyncerConfig:
        """自環境變數 ＋ `reader.json` 組出設定。

        `reader.json`（唯讀、非秘密）提供 manifest id 與收件匣 folder id：
        {"format": "aistorage.reader/v1", "manifest_file_id": "...",
         "inbox_folder_ids": {"mac-opencode": "..."}}
        環境變數可以覆寫 inbox folder id。

        `require_inbox=False` 只給 `status` 用：診斷時不該因為缺收件匣 id 就
        完全看不到狀態（那正是最需要看狀態的時候）。
        """
        e = env if env is not None else os.environ
        profile = e.get("AISTORAGE_PROFILE", DEFAULT_PROFILE)
        reader_cfg_path = e.get("AISTORAGE_READER_CONFIG", DEFAULT_READER_CONFIG)
        reader_raw = _read_json_if_exists(reader_cfg_path)

        inbox = e.get("AISTORAGE_INBOX_FOLDER_ID")
        if not inbox and isinstance(reader_raw, dict):
            mapping = reader_raw.get("inbox_folder_ids")
            if isinstance(mapping, dict):
                inbox = mapping.get(profile)
        if not isinstance(inbox, str) or not inbox:
            if require_inbox:
                raise ConfigError(
                    "找不到收件匣 folder id：請設 AISTORAGE_INBOX_FOLDER_ID，"
                    f"或在 {reader_cfg_path} 放 inbox_folder_ids['{profile}']"
                )
            inbox = None

        interval = e.get("AISTORAGE_SYNC_INTERVAL", str(DEFAULT_INTERVAL_S))
        try:
            interval_s = max(30, int(interval))
        except ValueError:
            interval_s = DEFAULT_INTERVAL_S

        return cls(
            profile=profile,
            base_url=e.get("AISTORAGE_OPENCODE_URL", DEFAULT_BASE_URL),
            directory=e.get("AISTORAGE_OPENCODE_DIR", DEFAULT_DIRECTORY),
            rclone_conf=e.get("AISTORAGE_RCLONE_CONF", DEFAULT_RCLONE_CONF),
            reader_config=reader_cfg_path,
            sa_key=e.get("AISTORAGE_SA_KEY", DEFAULT_SA_KEY),
            signing_key=e.get("AISTORAGE_SIGNING_KEY", DEFAULT_SIGNING_KEY),
            state_path=Path(e.get("AISTORAGE_SYNC_STATE", str(DEFAULT_STATE_PATH))),
            interval_s=interval_s,
            inbox_folder_id=inbox,
            # 觸發提交流程用（5.3）。PAT **只以路徑**引用：預設就是 1.6 的
            # /secrets/gh-pat-actions.txt，環境變數只能改路徑（不是改內容）。
            gh_pat_path=e.get("AISTORAGE_GH_PAT_PATH") or DEFAULT_GH_PAT,
            repo=e.get("AISTORAGE_GH_REPO") or None,
        )

    def signer(self) -> Signer:
        """讀簽章金鑰（只以路徑；金鑰本身不進 log）。"""
        if not Path(self.signing_key).is_file():
            raise ConfigError(f"找不到簽章金鑰：{self.signing_key}")
        return Signer.from_key_file(self.signing_key, profile=self.profile)


def _read_json_if_exists(path: str) -> Any:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

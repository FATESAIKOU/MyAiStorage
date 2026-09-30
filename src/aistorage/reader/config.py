"""讀取介面：讀者設定（tasks 4.3）。

manifest 的 id 跟著 profile 的讀取設定一起發放（D5）。這不是秘密，
但只放在讀者設定裡。SA 金鑰永遠只以路徑引用。

`agora_folder_id` 是 Agora **真本前綴資料夾**（git-annex 的物件資料夾）的 id。
`agora checkout` 要依快照的 annex key 去那裡取原始紀錄本體——讀取視圖只發佈
閱讀版（閱讀版還原不了位元組相同的開頭，ADR 0010），也不該把真本的位元組複製
一份到衍生物裡。取得位元組之後會用 **key 內嵌的 sha256** 驗證，所以「塞一份假
的同名檔」過不了。唯讀身分對那個資料夾只有唯讀權限。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_CONFIG_PATH = Path("~/.config/aistorage/reader.json").expanduser()
ENV_CONFIG = "AISTORAGE_READER_CONFIG"
ENV_SA_KEY = "AISTORAGE_SA_KEY"
DEFAULT_CACHE_DIR = Path("~/.cache/aistorage/reader").expanduser()


@dataclass(frozen=True)
class ReaderConfig:
    manifest_file_id: str
    sa_key_path: Path
    cache_dir: Path = DEFAULT_CACHE_DIR
    #: Agora 真本前綴（annex 物件）資料夾 id；`agora checkout` 取原始紀錄用。
    agora_folder_id: str = ""

    @classmethod
    def load(cls, path: Path | str | None = None, *,
             env: Mapping[str, str] = os.environ) -> ReaderConfig:
        """讀取設定檔；路徑順序：參數 ＞ AISTORAGE_READER_CONFIG ＞ 預設路徑。

        容器內可用 AISTORAGE_SA_KEY 覆寫 sa_key_path。
        """
        raw_path = str(path or env.get(ENV_CONFIG) or str(DEFAULT_CONFIG_PATH))
        cfg_path = Path(raw_path).expanduser()
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except OSError as e:
            raise FileNotFoundError(f"找不到讀者設定檔: {cfg_path}: {e}") from None
        except ValueError as e:
            raise ValueError(f"讀者設定檔不是合法 JSON: {cfg_path}: {e}") from None
        if not isinstance(data, dict):
            raise ValueError(f"讀者設定檔最外層必須是物件: {cfg_path}")
        manifest_file_id = data.get("manifest_file_id")
        if not isinstance(manifest_file_id, str) or not manifest_file_id:
            raise ValueError(f"讀者設定檔缺少 manifest_file_id: {cfg_path}")
        sa_key = env.get(ENV_SA_KEY) or data.get("sa_key_path")
        if not isinstance(sa_key, str) or not sa_key:
            raise ValueError(f"讀者設定檔缺少 sa_key_path: {cfg_path}")
        cache_dir = data.get("cache_dir")
        agora_folder_id = data.get("agora_folder_id") or ""
        if not isinstance(agora_folder_id, str):
            raise ValueError(
                f"讀者設定檔的 agora_folder_id 必須是字串: {cfg_path}"
            )
        return cls(
            manifest_file_id=manifest_file_id,
            sa_key_path=Path(sa_key).expanduser(),
            cache_dir=Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR,
            agora_folder_id=agora_folder_id,
        )

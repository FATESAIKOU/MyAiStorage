"""讀取介面：讀者設定（tasks 4.3）。

manifest 的 id 跟著 profile 的讀取設定一起發放（D5）。這不是秘密，
但只放在讀者設定裡。SA 金鑰永遠只以路徑引用。
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
        return cls(
            manifest_file_id=manifest_file_id,
            sa_key_path=Path(sa_key).expanduser(),
            cache_dir=Path(cache_dir).expanduser() if cache_dir else DEFAULT_CACHE_DIR,
        )

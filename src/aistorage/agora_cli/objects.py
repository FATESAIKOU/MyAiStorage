"""從 Agora 的**物件資料夾**依 git-annex key 取原始紀錄本體。

`agora checkout` 的起點包要「原始紀錄原封不動」（ADR 0010 的 KV cache 要求）。
閱讀版還原不了（它把工具呼叫的輸入輸出壓成摘要），而讀取視圖**不發佈** raw 的
位元組——那等於把真本的位元組複製一份到衍生物裡。所以這裡走另一條路：

1. 讀取介面只給**位址**：該快照的 annex key（`SHA256E-s<size>--<sha256>`，
   內容定址，所以 key 本身就是位址）。
2. 唯讀身分被分享了 Agora 的**物件資料夾**（`agora_folder_id`），所以能直接
   按 key 取物件。
3. **取回後用 key 內嵌的 sha256 與 size 驗證**。這是這個設計的安全關鍵：
   key 是內容的位址，所以「塞一份假的同名檔進去」過不了這一步
   （README 規格的簽章／雜湊同一個道理）。對不上就明確拒絕，絕不產出起點包。

只讀、唯讀權限。取不到就明確拒絕，不回退去猜或用別的版本頂替。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import tempfile
from typing import Any

from aistorage.agora.store import AnnexRawStorage
from aistorage.errors import MismatchError, NotFound, TooLarge

#: 單一個原始紀錄的下載上限。與 inbox 對原始紀錄的 100 MiB 上限一致
#: （`inbox.DEFAULT_MAX_RAW_SIZE`）：讀取端不因為自己是大檔就放寬。
DEFAULT_MAX_OBJECT_BYTES = 100 << 20

#: annex key 的文法只有一份定義（`AnnexRawStorage.parse_annex_key`）。自己再寫
#: 一個 regex 就會和寫入端漂移，而這整個安全性建立在「key 內嵌的 sha256 就是
#: 內容雜湊」上。
parse_annex_key = AnnexRawStorage.parse_annex_key


class ObjectError(RuntimeError):
    """取不到 Agora 的原始紀錄物件。訊息裡只有 key 與代碼，不含內容。"""


def verify_against_key(key: str, data: bytes) -> None:
    """用 key 內嵌的 size 與 sha256 驗證取回來的位元組；不符就明確拒絕。

    這是「原始紀錄原封不動」的最後一道：key 是內容的位址，所以對不上就代表
    這不是那一版的內容——不管是 Drive 上被動過、還是 key 與內容對不起來。
    """
    parsed = parse_annex_key(key)
    if parsed is None:
        raise ObjectError(
            f"annex key 格式看不懂（要 SHA256E-s<size>--<sha256>）: {key!r}"
        )
    _key, expected_size, expected_sha = parsed
    if len(data) != expected_size:
        raise ObjectError(
            f"原始紀錄大小不符：key 宣告 {expected_size} bytes，實際 {len(data)}"
            f"（key={key[:24]}…）"
        ) from MismatchError(f"annex key 宣告的大小與取回的內容不符: {key}")
    actual = hashlib.sha256(data).hexdigest().lower()
    if actual != expected_sha:
        raise ObjectError(
            f"原始紀錄的 SHA-256 與 key 不符：key 宣告 {expected_sha[:12]}，"
            f"實際 {actual[:12]}（key={key[:24]}…）。"
            "這份位元組不是那一版的原始紀錄，拒絕用它產出起點包。"
        ) from MismatchError(
            f"取回的原始紀錄與 annex key 不符: {key}（{expected_sha[:12]} != {actual[:12]}）"
        )


class ObjectFetcher:
    """依 key 從 Agora 物件資料夾取回原始紀錄（一次 checkout 內部重用清單結果）。

    `list_children` 在 Drive 上是昂貴的呼叫（限流），而 n→1 會有好幾個 key，
    所以第一次呼叫時把整個資料夾列一次、快取 `key → file id`。
    """

    def __init__(self, drive: Any, folder_id: str, *,
                 max_bytes: int = DEFAULT_MAX_OBJECT_BYTES) -> None:
        self._drive = drive
        self._folder_id = folder_id
        self._max_bytes = max_bytes
        self._by_key: dict[str, list[Any]] | None = None
        self._workdir = Path(tempfile.mkdtemp(prefix="agora-objects-"))

    @property
    def folder_id(self) -> str:
        return self._folder_id

    def _index(self) -> dict[str, list[Any]]:
        """列出物件資料夾一次，建立 `key → [檔案…]`。

        **一個 key 對應一個檔案，不是多個**（annex key 的前 64 碼就是內容雜湊，
        相同內容一定相同檔名）。但實測得到過同名多檔，所以這裡**保留全部**候選：
        見 `file_id_for`——只挑 sha256 與 size 都與 key 相符的那一個，猜錯了就
        寧可明確拒絕，也不要下載一個同名但內容不同的檔案再靠事後雜湊發現。
        """
        if self._by_key is None:
            by_key: dict[str, list[Any]] = {}
            for f in self._drive.list_children(self._folder_id):
                if f.name.startswith("SHA256"):
                    by_key.setdefault(f.name, []).append(f)
            self._by_key = by_key
        return self._by_key

    def file_id_for(self, key: str) -> str:
        """這個 key 對應的檔案 id；同名多檔時只挑 metadata 相符的那一個。"""
        candidates = self._index().get(key)
        if not candidates:
            raise ObjectError(
                f"Agora 的物件資料夾裡沒有這個 key: {key}"
                "（提交流程還沒把這個物件推上去，或該快照沒有走 annex）"
            )
        if len(candidates) == 1:
            return candidates[0].id

        parsed = parse_annex_key(key)
        if parsed is None:
            raise ObjectError(
                f"Agora 的物件資料夾裡有多個檔案叫 {key[:24]}…，"
                "而且這個 key 格式看不懂，沒辦法分辨哪一個才是要的"
            )
        _key, expected_size, expected_sha = parsed
        matching = [
            f for f in candidates
            if f.size == expected_size
            and isinstance(f.sha256, str)
            and f.sha256.lower() == expected_sha
        ]
        if len(matching) == 1:
            return matching[0].id
        raise ObjectError(
            f"Agora 的物件資料夾裡有 {len(candidates)} 個檔案叫 {key[:24]}…，"
            f"其中 {len(matching)} 個的 sha256 與 size 都和 key 相符。"
            "這是物件資料夾的狀態異常（同名就代表相同內容，不該有多個）："
            "請確認提交流程有沒有在共用同一個 pin／資料夾，並請人處理那些重複檔案。"
        )

    def fetch(self, key: str) -> bytes:
        """依 key 取回位元組並驗證。取不到或對不上就明確拒絕。"""
        parsed = parse_annex_key(key)
        if parsed is None:
            raise ObjectError(
                f"annex key 格式看不懂（要 SHA256E-s<size>--<sha256>）: {key!r}"
            )
        _key, expected_size, _sha = parsed
        # ★ key 的格式與 metadata 的比對都在**下載之前**（impl2-review4 M5）：
        #   早一步拒絕，就不會為了一個明顯不對的 key 白下載一次。
        file_id = self.file_id_for(key)
        dest = self._workdir / "object.bin"
        try:
            self._drive.download(file_id, dest, max_bytes=min(self._max_bytes,
                                                               expected_size + 1))
        except NotFound as e:
            raise ObjectError(
                f"依 key 找不到物件（{key[:24]}…）：{e}"
            ) from e
        except TooLarge as e:
            raise ObjectError(
                f"原始紀錄物件超過上限：key 宣告 {expected_size} bytes"
            ) from e
        except Exception as e:  # 連線等：明確講清楚，不要 traceback
            raise ObjectError(
                f"取原始紀錄物件失敗（key={key[:24]}…）：{type(e).__name__}"
            ) from e
        data = dest.read_bytes()
        dest.unlink(missing_ok=True)
        verify_against_key(key, data)
        return data

    def close(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)

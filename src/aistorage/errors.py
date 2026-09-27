"""AiStorage 共用例外定義模組。

依據規格：
- docs/impl/group3-modules.md 第 1、2.1 節
- review-g3a.md（NotFound 與 TooLarge 語意說明、移除別名）
"""

from __future__ import annotations


class AiStorageError(Exception):
    """AiStorage 基礎例外類別。"""


class AbortRun(AiStorageError):
    """提交流程步驟失敗中止例外。

    Attributes:
        step: 發生失敗的步驟名稱（例如 'guard', 'intake.scan' 等）
        code: 失敗代碼字串
        message: 詳細錯誤訊息
    """

    def __init__(self, step: str, code: str, message: str = "") -> None:
        self.step = step
        self.code = code
        self.message = message
        full_msg = f"[{step}] {code}" + (f": {message}" if message else "")
        super().__init__(full_msg)


class ReadError(AiStorageError):
    """讀取、網路、解析失敗例外（呼叫端可判斷讀不到並安全中止）。"""


class WriteError(AiStorageError):
    """寫入操作失敗例外。"""


class NotFound(ReadError):
    """404 資源不存在例外。

    注意：NotFound 不能作為破壞性操作（如清理、刪除）的依據。
    Google Drive 對沒有權限或不可達的檔案亦可能回傳 404。
    「檔案不存在」的可信證明只能來自父資料夾之完整列舉，不能單憑單一 404。
    """


class TooLarge(AiStorageError):
    """下載或串流位元組數超過上限例外。

    注意：TooLarge 不是 ReadError 的子類別（它是「確定過大」而非「讀取不到/網路失敗」）。
    呼叫端需依業務邏輯決定其處置（例如收件匣驗證轉為 REJECT，settle bundle 轉為 MismatchError）。
    """


class MismatchError(AiStorageError):
    """完整性不符、未預期的檔案或格式錯誤例外。"""

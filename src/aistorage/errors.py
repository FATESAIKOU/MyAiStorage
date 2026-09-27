"""AiStorage 共用例外定義模組。

依據規格：docs/impl/group3-modules.md 第 1、2.1 節
- AbortRun: 提交流程步驟失敗中止
- ReadError: 讀取、網路、解析失敗（429、5xx、逾時、網路錯誤）
- WriteError: 寫入失敗
- NotFound: 404 資源不存在
- TooLarge: 下載或內容超過上限
- MismatchError: 完整性核對不符、manifest 格式錯誤
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
    """404 資源不存在例外。"""


class TooLarge(AiStorageError):
    """下載或串流位元組數超過上限例外。"""


class MismatchError(AiStorageError):
    """完整性不符、未預期的檔案或格式錯誤例外。"""


# 別名相容
NotFoundError = NotFound
TooLargeError = TooLarge

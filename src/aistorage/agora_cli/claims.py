"""`agora checkout` 的**本機認領記錄**（`--resume` 的依據，review-73dbf2c H1）。

`checkout` 產出起點包之前，要先把「這次 checkout 預留了什麼」記在本機：預留的
新 session id、那一筆收件匣項目的 id、以及預留時間。它是 `--resume` 能沿用同一組
id 的唯一依據。

**為什麼交接單起點一定要有**（impl2 M6 之前就有的部分）：一張交接單只能被認領
一次。所以認領一旦放進收件匣，就**不能**因為本機出錯（輸出目錄不可寫、等待逾時、
行程被中斷）而重來一次：重跑若換一個新的認領 id，那張交接單就永遠卡在「已被認領、
卻沒有任何 session 接手」的狀態。

**直接起點也記**（impl2 M6）：接續不需要交接單，所以沒有「只能被接一次」這回事，
但**重跑會留下一筆沒有人開工的預留 session 與接續 Link**——而提交流程對同一個
新 session 對同一個起點只留一條 Link，那筆多出來的預留就成了沒有人接手的空 session。
記下來，重跑就能沿用同一組 id，位元組也相同（見下面的 `reserved_at`）。

所以記錄的鍵是**起點鍵**（`startpoint_key`）：交接單起點用交接單 id，直接起點用
`startpoint:<session>@<訊息>`。同一個起點永遠對到同一個檔案。

- 提交流程的 `apply_claim`／`apply_continuation` **本來就把重複的 id 當成完成**
  （同 id、同 body 且 Link 都在 → `already`），所以重送是冪等的；
- 清冊（ledger）認得同一個 `item_key` 與同一份 raw 雜湊 → 直接 `already`，
  收件匣清空、讀取介面看得到那條接續 Link，等待因此成功。

**預留時間一定要記下來**：新 session 的空匯出檔由它決定（見 `checkout.py`
的 `_empty_export_bytes`），重跑時必須產生**位元組相同**的 raw，否則清冊會看成
`replayed_item_key` 而拒收。

這個模組**不碰任何 coding agent**，也不含秘密：只有 id 與時間。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

FORMAT = "aistorage.checkout-claims/v1"

#: 預設位置（**一個目錄**，底下每個起點一個檔案；見 `ClaimJournal`）。
#: 容器裡 `HOME=/work`，所以與同步器狀態檔（`/work/.aistorage/…`）同層。
#: 測試與離線環境用 `AISTORAGE_CHECKOUT_CLAIMS` 覆寫。
DEFAULT_PATH = Path(
    os.environ.get("AISTORAGE_CHECKOUT_CLAIMS")
    or (Path.home() / ".aistorage" / "checkout-claims")
)

#: 直接起點（`<session>[@<訊息>]`）的起點鍵前綴，與交接單 id 不會撞。
SESSION_STARTPOINT_PREFIX = "startpoint:"


def startpoint_key(
    handoff_id: str | None, *, session_id: str, message_id: str | None = None
) -> str:
    """這個起點在本機記錄裡的鍵：交接單 id，或 `startpoint:<session>@<訊息>`。

    兩種起點都要能重跑時認回同一筆預留，所以鍵必須由**起點本身**決定，而不是
    由這次是第幾次 checkout 決定。
    """
    if handoff_id:
        return str(handoff_id)
    return f"{SESSION_STARTPOINT_PREFIX}{session_id}@{message_id or ''}"


class ClaimJournalError(RuntimeError):
    """本機認領記錄讀不到／寫不進去。訊息裡只有路徑與代碼。"""


@dataclass(frozen=True)
class ClaimRecord:
    """一次 checkout 的完整識別資訊（重跑時全部沿用）。"""

    #: 起點鍵（見 `startpoint_key`）：交接單 id 或 `startpoint:<session>@<訊息>`
    startpoint_key: str
    #: 收件匣項目的 id（`claim:<ULID>` 或 `continuation:<ULID>`；提交流程的冪等
    #: 判斷用它）
    claim_id: str
    #: 收件匣檔名的 ULID 前綴（`<item_key>.sidecar.json` …）
    item_key: str
    #: 這次預留給新 session 的 id（`opencode:<native>`）
    new_session_id: str
    #: 預留時間（RFC 3339 UTC Z，含毫秒）。**決定空匯出檔的位元組**，重跑要一致。
    reserved_at: str
    #: 新 session 的標題（空匯出檔裡的 `info.title`）
    title: str
    profile: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "startpoint_key": self.startpoint_key,
            "claim_id": self.claim_id,
            "item_key": self.item_key,
            "new_session_id": self.new_session_id,
            "reserved_at": self.reserved_at,
            "title": self.title,
            "profile": self.profile,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ClaimRecord:
        if not isinstance(data, dict):
            raise ClaimJournalError("認領記錄不是一個物件")
        keys = ("startpoint_key", "claim_id", "item_key", "new_session_id",
                "reserved_at", "title", "profile", "created_at")
        missing = [k for k in keys if not isinstance(data.get(k), str)]
        if missing:
            raise ClaimJournalError(f"認領記錄缺少欄位: {'、'.join(missing)}")
        return cls(**{k: str(data[k]) for k in keys})


class ClaimJournal:
    """`起點鍵 → ClaimRecord` 的本機記錄，**一個起點一個檔案**。

    這是**本機**狀態，不是 Agora 的真本：它只記住「我這次為哪個起點預留了哪個新
    session、送了哪一個收件匣項目」，讓重跑能重送同一筆。

    **為什麼是一個一個檔案**（review-7a4ca87 M1）：1→n 的典型用法是同一個容器裡
    **並行**跑好幾次 checkout。單一 JSON 檔的「讀→改→寫」沒有鎖，兩個行程同時寫
    會後蓋先，其中一個起點的記錄就消失了——那個之後逾時就無從救回。分成一個檔
    一個起點，寫入用 `O_EXCL` 的暫存檔再一次改名，兩條線互不干擾，也不需要鎖。

    **損毀要出聲**（review-7a4ca87 M1）：讀到壞掉的記錄**拋錯**，不要當成沒有。
    當成「沒有」會讓 `put` 覆蓋掉其他起點還在用的記錄——那正是最不能靜靜發生的
    事。錯誤訊息會說明是哪個檔案、請人先看過再決定。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH

    def _file_for(self, key: str) -> Path:
        # 起點鍵形如 `handoff:01ARZ…` 或 `startpoint:opencode:ses_x@msg_y`；
        # `:` 與 `/` 在檔名裡不合法也不好看
        safe = key.replace(":", "_").replace("/", "_")
        return self.path / f"{safe}.json"

    def get(self, key: str) -> ClaimRecord | None:
        """這個起點本機記得的預留；沒有（或目錄還在）就 None。"""
        path = self._file_for(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ClaimJournalError(
                f"認領記錄讀不來（壞掉或不完整）: {path}（{type(e).__name__}）。"
                "沒有它就沒辦法確認這個起點上次預留了什麼；"
                "請先看過那個檔案，壞掉的話刪掉它再重跑，"
                "不要在這裡直接覆蓋（會連帶弄壞其他起點的記錄）"
            ) from None
        try:
            return ClaimRecord.from_dict(data)
        except ClaimJournalError as e:
            raise ClaimJournalError(f"認領記錄的內容不合法: {path}（{e}）") from None

    def put(self, record: ClaimRecord) -> None:
        """記下一次預留。**送出之前**就要寫，行程中斷才救得回來。

        已經有同一個起點的記錄時**拒絕覆蓋**（review-7a4ca87 H1）：舊記錄對應的
        可能是一個已經被接受的項目，蓋掉它等於讓那筆預留永遠認不回來。要沿用就用
        舊的（`get`），不是寫一份新的。
        """
        path = self._file_for(record.startpoint_key)
        if path.is_file():
            existing = self.get(record.startpoint_key)
            raise ClaimJournalError(
                f"認領記錄已經有 {record.startpoint_key} 了（預留 "
                f"{existing.new_session_id if existing else '?'}，"
                f"記錄於 {path}）。"
                "這筆預留可能已經被接受；覆蓋記錄會讓它認不回來。"
                "重跑請帶 --resume 沿用同一個預留。"
            )
        data = json.dumps(
            {"format": FORMAT, **record.to_dict()},
            ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 暫存檔用 O_EXCL 併一次改名：兩個行程同時寫也不會互相覆蓋
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(path.parent),
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                handle.write(data)
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            temp_path.replace(path)
        except OSError as e:
            raise ClaimJournalError(
                f"認領記錄寫不進去: {path}（{type(e).__name__}）。"
                "沒有它，這次預留一旦被接受就認不回來；"
                "請用 AISTORAGE_CHECKOUT_CLAIMS 指到可寫的位置再試"
            ) from None

    def forget(self, key: str) -> None:
        """預留被**明確拒收**時刪掉記錄——那個起點已經不是這次 checkout 的了。

        只有「確定不會成功」才呼叫（被拒時）。逾時**不**呼叫：那筆可能下一輪就被
        接受，記錄是救命的。
        """
        path = self._file_for(key)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            raise ClaimJournalError(
                f"認領記錄刪不掉: {path}（{type(e).__name__}）。"
                "留著不影響這次結果（記錄只是重跑時的提示），"
                "但下次重跑會以為已經預留過。"
            ) from None


__all__ = [
    "ClaimJournal",
    "ClaimJournalError",
    "ClaimRecord",
    "DEFAULT_PATH",
    "FORMAT",
    "SESSION_STARTPOINT_PREFIX",
    "startpoint_key",
]

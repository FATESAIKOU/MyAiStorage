"""`agora checkout` 的**本機認領記錄**（`--resume` 的依據，review-73dbf2c H1）。

一份交接單只能被認領一次（提交流程的 `already_claimed`）。所以認領一旦放進收件匣，
就**不能**因為本機出錯（輸出目錄不可寫、等待逾時、行程被中斷）而重來一次：
重跑若換一個新的認領 id，那張交接單就永遠卡在「已被認領、卻沒有任何 session
接手」的狀態。

所以認領送出之前先把這次的**認領 id ＋ 新 session id ＋ 預留時間**寫進本機記錄；
`--resume` 重跑時沿用同一組值：

- 提交流程的 `apply_claim` **本來就把重複的 claim id 當成完成**（同 id、同 body
  且 link 與 `claimed_by` 都在 → `already`），所以重送是冪等的；
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

#: 預設位置。容器裡 `HOME=/work`，所以與同步器狀態檔（`/work/.aistorage/…`）同層。
#: 測試與離線環境用 `AISTORAGE_CHECKOUT_CLAIMS` 覆寫。
DEFAULT_PATH = Path(
    os.environ.get("AISTORAGE_CHECKOUT_CLAIMS")
    or (Path.home() / ".aistorage" / "checkout-claims.json")
)


class ClaimJournalError(RuntimeError):
    """本機認領記錄讀不到／寫不進去。訊息裡只有路徑與代碼。"""


@dataclass(frozen=True)
class ClaimRecord:
    """一次認領的完整識別資訊（重跑時全部沿用）。"""

    handoff_id: str
    #: `claim:<ULID>`：收件匣項目的 id（提交流程的冪等判斷用它）
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
            "handoff_id": self.handoff_id,
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
        keys = ("handoff_id", "claim_id", "item_key", "new_session_id",
                "reserved_at", "title", "profile", "created_at")
        missing = [k for k in keys if not isinstance(data.get(k), str)]
        if missing:
            raise ClaimJournalError(f"認領記錄缺少欄位: {'、'.join(missing)}")
        return cls(**{k: str(data[k]) for k in keys})


class ClaimJournal:
    """`handoff_id → ClaimRecord` 的本機檔案（原子寫入、損壞就當空）。

    這是**本機**狀態，不是 Agora 的真本：它只記住「我這次用哪個 claim id 認領了
    哪張交接單」，讓 `--resume` 能重送同一筆。記錄遺失不會讓任何東西壞掉
    （只是那次認領認不回來），所以讀不到時從空白開始、不拋錯。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH

    def _read(self) -> dict[str, ClaimRecord]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 記錄檔是衍生狀態：壞了就當沒有，不要讓 checkout 不能跑
            return {}
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return {}
        raw = data.get("claims")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, ClaimRecord] = {}
        for handoff_id, entry in raw.items():
            if not isinstance(handoff_id, str):
                continue
            try:
                out[handoff_id] = ClaimRecord.from_dict(entry)
            except ClaimJournalError:
                continue
        return out

    def get(self, handoff_id: str) -> ClaimRecord | None:
        """這張交接單本機記得的認領；沒有就 None。"""
        return self._read().get(handoff_id)

    def put(self, record: ClaimRecord) -> None:
        """記下（或覆蓋）一次認領。**送出之前**就要寫，行程中斷才救得回來。"""
        records = self._read()
        records[record.handoff_id] = record
        self._write(records)

    def forget(self, handoff_id: str) -> None:
        """認領被明確拒收時刪掉記錄——那張單已經不是這次 checkout 的了。"""
        records = self._read()
        if handoff_id in records:
            del records[handoff_id]
            self._write(records)

    def _write(self, records: dict[str, ClaimRecord]) -> None:
        payload = {
            "format": FORMAT,
            "claims": {k: v.to_dict() for k, v in sorted(records.items())},
        }
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 先寫暫存檔再一次改名：中斷不會留下半份記錄檔
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.path.parent),
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                handle.write(data)
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            temp_path.replace(self.path)
        except OSError as e:
            raise ClaimJournalError(
                f"認領記錄寫不進去: {self.path}（{type(e).__name__}）。"
                "沒有它，這次認領一旦被接受就認不回來；"
                "請用 AISTORAGE_CHECKOUT_CLAIMS 指到可寫的位置再試"
            ) from None


__all__ = [
    "ClaimJournal",
    "ClaimJournalError",
    "ClaimRecord",
    "DEFAULT_PATH",
    "FORMAT",
]

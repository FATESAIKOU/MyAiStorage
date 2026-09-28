"""起點包的組裝與讀取（`schemas/context-package.schema.json`）。

起點包**不屬於任何 coding agent**：它只是把讀取介面拿到的原始紀錄**原封不動**
放進一個目錄，加上任務與來源 id。截斷、重編 id、匯入都是轉接器的事。

`agora checkout` 的取捨寫在 `schemas/context-package.md`：
- n→1 時最長的一段放最前面（ADR 0010 的 prompt cache）；
- 長度超過上限就明確拒絕，**不產出**（不留半套產物）。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Sequence
from uuid import uuid4

from aistorage.agora_cli.startpoint import ResolvedStartPoint

FORMAT = "aistorage.contextpackage/v1"
PACKAGE_FILE = "package.json"
RAW_DIR = "raw"
GENERATOR_VERSION = "1"

#: 預設的長度上限（`context_limit.measured` 的單位＝`text_chars`）。
#:
#: ADR 0010：n→1 合併兩個長 session 可能超過模型的 context 上限，
#: 「期 1 先偵測並明確拒絕，不默默截斷」。以**純文字字元數**計，不用 token：
#: 不必為了算長度就呼叫模型，而它對 token 數是保守的估計（字元數/token 數的比
#: 依內容而異，繁體中文大約 1〜2，程式碼大約 3〜4，所以 800k 字元對 200k token
#: 的模型是安全的保守值）。呼叫端可用 `--max-chars` 覆寫。
DEFAULT_MAX_CONTEXT_CHARS = 800_000


class ContextPackageError(ValueError):
    """起點包組不起來、寫不進去，或讀回來的內容不自洽。"""


class ContextLimitExceeded(ContextPackageError):
    """長度超過上限。**明確拒絕，不產出**（目錄不會被建立）。"""


@dataclass(frozen=True)
class PackageSegment:
    """起點包裡的一段。`raw` 是原始紀錄的位元組（原封不動）。"""

    resolved: ResolvedStartPoint
    raw: bytes
    message_count: int = 0
    text_chars: int = 0
    claim_id: str | None = None
    order: int = 0

    @property
    def session_id(self) -> str:
        return self.resolved.session_id

    @property
    def source(self) -> str:
        return self.resolved.source

    @property
    def snapshot_sha256(self) -> str:
        return self.resolved.snapshot_sha256

    @property
    def raw_name(self) -> str:
        """`raw/` 下的檔名：次序 ＋ 可辨識的 session ＋ 快照前 12 碼。"""
        native = self.resolved.startpoint.native_session_id or "session"
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in native)[:40]
        snap = self.resolved.snapshot_sha256[:12]
        return f"{self.order + 1:02d}-{self.source}-{safe}-{snap}.json"


@dataclass(frozen=True)
class ContextPackage:
    """組好（或讀回來）的起點包內容。"""

    segments: tuple[PackageSegment, ...]
    task: str | None = None
    new_session_id: str = ""
    claimed_handoffs: tuple[str, ...] = ()
    created_at: str = ""
    created_by: str = ""
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS

    @property
    def text_chars(self) -> int:
        return sum(s.text_chars for s in self.segments)

    @property
    def raw_bytes(self) -> int:
        return sum(len(s.raw) for s in self.segments)

    @property
    def messages(self) -> int:
        return sum(s.message_count for s in self.segments)


def order_segments(segments: Sequence[PackageSegment]) -> list[PackageSegment]:
    """n→1 的次序：**最長的一段放最前面**，其餘維持呼叫端給的次序（ADR 0010）。

    「長」以 `text_chars`（接續點之前的純文字字元數）計——那是真正會變成 token
    的部分，raw 位元組還包含沒有進模型的欄位。

    平手時維持呼叫端給的次序（`sorted` 是穩定的）：呼叫端已經把最重要的那個
    起點排在第一個時，不要打亂它。
    """
    indexed = list(enumerate(segments))
    ordered = sorted(indexed, key=lambda pair: (-pair[1].text_chars, pair[0]))
    out: list[PackageSegment] = []
    for position, (_original, segment) in enumerate(ordered):
        out.append(_with_order(segment, position))
    return out


def _with_order(segment: PackageSegment, order: int) -> PackageSegment:
    return PackageSegment(
        resolved=segment.resolved, raw=segment.raw,
        message_count=segment.message_count, text_chars=segment.text_chars,
        claim_id=segment.claim_id, order=order,
    )


def check_context_length(pkg: ContextPackage) -> None:
    """超過長度上限就明確拒絕。

    **在寫任何檔案之前呼叫**：目錄不該被建立一半（`context-package.md` 的
    「被拒就不產出」）。這裡不默默截斷——截掉的是別人交出來的工作。
    """
    total = pkg.text_chars
    if total > pkg.max_context_chars:
        per = "；".join(
            f"{s.session_id}@{s.resolved.snapshot_sha256[:12]}={s.text_chars}"
            for s in pkg.segments
        )
        raise ContextLimitExceeded(
            f"起點太長：接續點之前的內容合計 {total} 字元，"
            f"超過上限 {pkg.max_context_chars}（{pkg.messages} 則訊息）。"
            f"各段：{per}。"
            "請減少要接續的起點數、或改用較大的 --max-chars；"
            "Agora 不會默默截斷別人交出來的工作。"
        )


def to_dict(pkg: ContextPackage) -> dict[str, Any]:
    """組出 `package.json` 的字典（schema 的形狀）。"""
    return {
        "format": FORMAT,
        "created_at": pkg.created_at,
        "created_by": pkg.created_by,
        "generator": {"tool": "agora", "version": GENERATOR_VERSION},
        "task": pkg.task,
        "segments": [
            {
                "order": s.order,
                "startpoint": s.resolved.startpoint.text,
                "source_session_id": s.session_id,
                "source": s.source,
                "snapshot_sha256": s.snapshot_sha256,
                "snapshot_at": s.resolved.snapshot_at,
                "message_id": s.resolved.message_id,
                "handoff_id": s.resolved.handoff_id,
                "claim_id": s.claim_id,
                "raw_file": f"{RAW_DIR}/{s.raw_name}",
                "raw_size": len(s.raw),
                "raw_sha256": hashlib.sha256(s.raw).hexdigest().lower(),
                "message_count": s.message_count,
                "text_chars": s.text_chars,
            }
            for s in pkg.segments
        ],
        "new_session": {
            "session_id": pkg.new_session_id,
            "source": pkg.segments[0].source if pkg.segments else "",
            "claimed_handoffs": list(pkg.claimed_handoffs),
        },
        "totals": {
            "segments": len(pkg.segments),
            "messages": pkg.messages,
            "raw_bytes": pkg.raw_bytes,
            "text_chars": pkg.text_chars,
        },
        "context_limit": {
            "limit": pkg.max_context_chars,
            "measured": "text_chars",
            "within_limit": True,
        },
    }


def stage_package(pkg: ContextPackage, out_dir: Path) -> Path:
    """把起點包組到 `out_dir` 旁邊的暫存目錄並自我驗證；回傳暫存目錄路徑。

    **組裝與驗證都刻意與「改名」分開**（review-73dbf2c H1）：輸出目錄不可寫、
    已經有東西、位元組被改動——這些本機問題必須在**登記認領之前**就發現，
    否則認領會卡死成一張沒有人接的交接單（被認領了卻永遠不產出起點包）。

    這裡只寫 `raw/`（位元組最多的部分）並驗證；`package.json` 留到
    `commit_staged_package` 寫，因為它要含認領結果（claim id／認領了哪幾張單）。
    """
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise ContextPackageError(
            f"起點包目錄已經有東西，不會覆蓋: {out}（請給一個空的目錄）"
        )
    # 暫存目錄名稱帶 pid 與隨機後綴（review-7a4ca87 M2）：固定名
    # `.<name>.staging` 讓同一個 `-o` 的兩次並行執行互相刪掉對方的暫存目錄，
    # 而那時其中一邊的認領可能已經送出去了。
    staging = out.parent / f".{out.name}.{os.getpid()}-{uuid4().hex[:8]}.staging"
    try:
        (staging / RAW_DIR).mkdir(parents=True, exist_ok=True)
        for segment in pkg.segments:
            (staging / RAW_DIR / segment.raw_name).write_bytes(segment.raw)
        _verify_staging(pkg, staging)
    except Exception:
        _rmtree(staging)
        raise
    return staging


def commit_staged_package(pkg: ContextPackage, staging: Path,
                          out_dir: Path) -> Path:
    """把已驗證的暫存目錄寫成 `package.json` 並一次改名成 `out_dir`。

    `package.json` 在**認領確認之後**才寫：它記著 `claim_id` 與
    `claimed_handoffs`（`new_session` 區塊），那時才有。
    """
    out = Path(out_dir)
    staging = Path(staging)
    try:
        payload = json.dumps(to_dict(pkg), ensure_ascii=False, sort_keys=True,
                             indent=2) + "\n"
        (staging / PACKAGE_FILE).write_text(payload, encoding="utf-8")
        _verify_staging(pkg, staging)
        if out.exists():
            out.rmdir()
        staging.rename(out)
    except Exception as e:
        _rmtree(staging)
        # 這一步在**認領確認之後**才做（`checkout()` 先 `_claim` 才呼叫這裡）。
        # 所以失敗時交接單已經被接走、Agora 裡已經有一筆預留的 session——不是
        # 可以「修好再重跑」的錯誤，重跑會撞 already_claimed。換一個講得清楚的
        # 例外，讓 `checkout()` 把它轉成「認領已經成立」的訊息（review-7a4ca87 M2）。
        raise ContextPackageError(
            f"起點包組好了，但產出到 {out} 失敗: {e}"
        ) from e
    return out / PACKAGE_FILE


def write_package(pkg: ContextPackage, out_dir: Path) -> Path:
    """把起點包寫到 `out_dir`（目錄必須不存在或為空）。

    **先在暫存目錄組好再一次改名**：中途失敗不會留下半套起點包，而
    `agora checkout` 的承諾是「被拒就不產出」。`agora checkout` 自己用
    `stage_package` ＋ `commit_staged_package`（中間要插認領）；這個函式是
    兩步的直接組合。
    """
    return commit_staged_package(pkg, stage_package(pkg, out_dir), out_dir)


def _verify_staging(pkg: ContextPackage, staging: Path) -> None:
    """寫完之後自己驗一次：位元組原封不動、雜湊對得上。

    這是「原始紀錄原封不動」這個承諾的最後一道——寫進去的東西如果被轉換過
    （換行、編碼、JSON 重排版），`agora-opencode load` 的開頭就不會與原 session
    位元組相同，KV cache 全部落空。
    """
    for segment in pkg.segments:
        path = staging / RAW_DIR / segment.raw_name
        data = path.read_bytes()
        if data != segment.raw:
            raise ContextPackageError(
                f"起點包裡的原始紀錄與來源不一致（寫入後被改動）: {path}"
            )
        digest = hashlib.sha256(data).hexdigest().lower()
        if digest != segment.snapshot_sha256:
            raise ContextPackageError(
                f"原始紀錄的 SHA-256 {digest[:12]} 與快照 "
                f"{segment.snapshot_sha256[:12]} 不符: {path}"
            )


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def read_package(package_dir: Path) -> tuple[dict[str, Any], list[bytes]]:
    """讀回一個起點包：回傳 `(package.json 字典, 各段的原始紀錄位元組)`。

    **會驗每段的 `raw_sha256`**：起點包會被搬來搬去（不同機器、不同 agent），
    所以不能假設它沒被動過。對不上就明確拒絕——寧可不要開工，也不要拿一份
    位元組不同的開頭去建 session。
    """
    root = Path(package_dir)
    manifest_path = root / PACKAGE_FILE
    if not manifest_path.is_file():
        raise ContextPackageError(f"找不到起點包的 {PACKAGE_FILE}: {root}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ContextPackageError(f"起點包的 {PACKAGE_FILE} 不是合法 JSON: {e}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ContextPackageError(
            f"起點包格式不對（要 {FORMAT}）: {data.get('format')!r}"
        )
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ContextPackageError("起點包沒有任何 segments")

    raws: list[bytes] = []
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ContextPackageError(f"segments[{position}] 不是物件")
        rel = segment.get("raw_file")
        if not isinstance(rel, str) or not rel:
            raise ContextPackageError(f"segments[{position}] 缺少 raw_file")
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve()) + "/"):
            raise ContextPackageError(
                f"segments[{position}] 的 raw_file 指向起點包外面: {rel}"
            )
        if not path.is_file():
            raise ContextPackageError(f"起點包缺少原始紀錄: {rel}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest().lower()
        declared = str(segment.get("raw_sha256") or "").lower()
        # ★ 缺 `raw_sha256` 就**拒絕**，不要略過驗證（impl2-review4 L）。
        #   略過等於「沒有雜湊的段落照樣能用」——那正是位元組相同這個保證失效的
        #   入口，而起點包會被搬來搬去，搬運途中被改掉是最自然的事。
        if not declared:
            raise ContextPackageError(
                f"起點包的 segments[{position}] 沒有 raw_sha256，"
                "無法驗證原始紀錄有沒有被動過：拒絕讀取。"
                "（沒有雜湊就不要略過驗證——略過等於不驗。）"
            )
        if digest != declared:
            raise ContextPackageError(
                f"起點包裡的原始紀錄已被動過（{rel}：記錄 {declared[:12]}，"
                f"實際 {digest[:12]}）"
            )
        raws.append(raw)
    return data, raws

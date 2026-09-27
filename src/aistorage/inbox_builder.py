"""AiStorage 收件匣項目組裝模組（寫入端共用）。

依據規格：
- docs/impl/group3-modules.md 第 7.4 節（`build_inbox_item` 為手動匯入工具與
  5.2 同步器共用，刻意放在 `aistorage/inbox_builder.py` 而非 importer 套件內，
  讓同步器不必匯入 CLI 套件）
- schemas/inbox-sidecar.schema.json、schemas/metadata-inbox.schema.json
- design D2（不可分單位）、D3（簽章與 profile 綁定）、D4（快照時間由寫入端記）

組裝出來的位元組就是寫進收件匣的位元組：簽章涵蓋 sidecar 的**原始位元組**
（`aistorage.inbox/v1\\n` + sidecar_bytes），所以 sidecar 只序列化一次，寫出時
不得重新格式化，否則驗章會失敗。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from aistorage.clock import Clock, SystemClock
from aistorage.converters.base import SessionFacts
from aistorage.inbox import (
    DEFAULT_MAX_RAW_SIZE,
    check_raw,
    sign_sidecar_bytes,
    validate_sidecar,
)
from aistorage.schema import generate_ulid, make_session_id

SIDECAR_FORMAT = "aistorage.inbox/v1"

#: 項目識別碼（ULID）與 profile 名稱的格式。
ITEM_KEY_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
SESSION_ID_PATTERN = re.compile(
    r"^(?!(handoff|claim|reference|rewrite|artifact|session):)[a-z0-9][a-z0-9_-]*:\S+$"
)

VALID_STATUSES = ("running", "stopped")

#: metadata 擴充欄位：轉換器給不出時間、只能退回匯入時間時留下的標記。
#: `metadata-inbox` 與 `metadata-record` 兩個 schema 都允許擴充欄位，
#: 提交流程的 `stamp_record` 也只會去掉 producer，其餘欄位原樣帶進真本。
TIME_SOURCE_IMPORT = "import"

#: sidecar 的位元組格式：排序鍵、縮排 2、結尾換行（與 `AgoraStore.put_json` 相同）。
JSON_DUMP_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "indent": 2,
}


class InboxBuildError(ValueError):
    """組裝收件匣項目時發現無法通過 sidecar 驗證或輸出一致性的問題。"""


def serialize_json(obj: dict) -> bytes:
    """將物件序列化為 sidecar／sig 的位元組格式（尾端含換行）。"""
    return (json.dumps(obj, **JSON_DUMP_KWARGS) + "\n").encode("utf-8")


def read_item_key(sidecar_bytes: bytes) -> str:
    """從 sidecar 位元組取回 item_key（呼叫端需要它來組檔名）。"""
    try:
        return str(json.loads(bytes(sidecar_bytes).decode("utf-8"))["item_key"])
    except Exception as e:
        raise InboxBuildError(f"sidecar 位元組無法解析出 item_key: {e}") from None


def load_private_key(path: str | Path) -> bytes:
    """自檔案讀取 32 位元組 Ed25519 私鑰。

    只以路徑讀取，絕不印出內容。權限過寬（group／other 可讀寫）時拒絕，
    與 `aistorage.identity keygen` 的 600 權限要求一致。
    """
    p = Path(path)
    if not p.is_file():
        raise InboxBuildError(f"找不到私鑰檔案: {p}")
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise InboxBuildError(
            f"私鑰檔案權限過寬 ({oct(mode)})，請先執行 chmod 600 '{p}' 再試"
        )
    key = p.read_bytes()
    if len(key) != 32:
        raise InboxBuildError(
            f"私鑰必須是 32 位元組的原始 Ed25519 私鑰，實際為 {len(key)} 位元組"
        )
    return key


def _hash_raw(raw_path: Path, max_size: int) -> tuple[str, int]:
    """串流計算原始紀錄的 sha256 與大小；超過上限立刻中止。"""
    hasher = hashlib.sha256()
    size = 0
    with open(raw_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_size:
                raise InboxBuildError(
                    f"原始紀錄大小超過 {max_size} 位元組上限（已讀 {size} 位元組後停止）"
                )
            hasher.update(chunk)
    return hasher.hexdigest().lower(), size


def _normalize_parent_id(parent_id: str | None, source: str) -> str | None:
    """把母 Session 識別碼補上 `<source>:` 前綴並檢查格式。"""
    if parent_id is None:
        return None
    candidate = str(parent_id).strip()
    if not candidate:
        return None
    if ":" not in candidate:
        candidate = f"{source}:{candidate}"
    if not SESSION_ID_PATTERN.match(candidate):
        raise InboxBuildError(
            f"parent_id 必須是 <source>:<source_session_id> 格式: {parent_id!r}"
        )
    return candidate


def _usable_time(value: Any) -> bool:
    """判斷時間字串是否可用（非空、非純空白）。"""
    return isinstance(value, str) and bool(value.strip())


def build_inbox_item(
    raw_path: Path,
    *,
    source: str,
    source_session_id: str,
    facts: SessionFacts,
    profile: str,
    key: bytes,
    key_id: str,
    parent_id: str | None = None,
    status: str = "running",
    stopped_at: str | None = None,
    case_id: str | None = None,
    provenance: str | None = None,
    snapshot_at: str | None = None,
    now: str | None = None,
    item_key: str | None = None,
    max_raw: int = DEFAULT_MAX_RAW_SIZE,
    clock: Clock | None = None,
) -> tuple[bytes, dict]:
    """把一份來源 Session 的原始紀錄包成收件匣項目的 sidecar 與簽章。

    回傳 `(sidecar_bytes, sig_obj)`。sidecar_bytes 就是必須逐位元組寫進收件匣
    （或 --out-dir）的內容；簽章涵蓋 `aistorage.inbox/v1\\n` + sidecar_bytes。

    組裝前會以 `validate_sidecar` 與 `check_raw` 自查，不通過就丟出
    InboxBuildError：寧可在這裡失敗，也不要產出提交流程會拒收的項目。

    Args:
        raw_path: 來源應用的原始紀錄檔（opencode 匯出 JSON／Claude Code jsonl）。
        source: 來源應用識別（`opencode`、`claude-code`）。
        source_session_id: 來源端自己的 Session id（不含 `<source>:` 前綴）。
        facts: 轉換器給出的 SessionFacts（時間、是否仍在生成）。
        profile: 寫入者 profile（例如 `mac-opencode`），必須與簽章金鑰所屬 profile 一致。
        key: 32 位元組 Ed25519 私鑰原始位元組。
        key_id: 簽章金鑰識別碼。
        parent_id: 子 Session 的母 Session 識別碼（可省略前綴，會自動補上）。
        status: `running` 或 `stopped`（停止中只能明確宣告，不從閒置時間推測）。
        stopped_at: 停止時間（status=stopped 時必填）。
        case_id: 所屬案件 id，不確定時可為 None。
        provenance: 出處說明，不適用時可為 None（手動匯入由呼叫端填固定字串）。
        snapshot_at: 快照時間（擷取原始紀錄的時間），預設為 now。
        now: 本次組裝時間，預設取 clock.now_utc()。
        item_key: 項目識別碼（ULID），預設新產生一個。
        max_raw: 原始紀錄大小上限。
        clock: 時鐘（測試可注入 FixedClock）。

    建立／更新時間取自 `facts`；轉換器給不出來時退回 `now`，並在 metadata 加上
    擴充欄位 `time_source: "import"`，讓讀者知道這兩個時間是匯入時間而非來源端時間。
    """
    raw_p = Path(raw_path)
    if not raw_p.is_file():
        raise InboxBuildError(f"找不到原始紀錄檔案: {raw_p}")

    if not isinstance(profile, str) or not PROFILE_PATTERN.match(profile):
        raise InboxBuildError(
            f"profile 名稱不合法 ({profile!r})，必須符合 ^[a-z][a-z0-9-]*$"
        )
    if status not in VALID_STATUSES:
        raise InboxBuildError(
            f"status 必須是 {VALID_STATUSES} 之一: {status!r}"
        )
    if status == "stopped":
        if not stopped_at or not str(stopped_at).strip():
            raise InboxBuildError("status=stopped 時必須提供 stopped_at")
        if facts.in_progress:
            raise InboxBuildError(
                "來源端仍在生成中（in_progress=true），不能宣告停止中；"
                "請先讓 Session 產生完畢或以 running 匯入"
            )
    elif stopped_at is not None:
        raise InboxBuildError("status=running 時不得提供 stopped_at")

    # 項目 id 對到來源端的 Session id：重複匯入同一個 Session 必得同一個 id。
    item_id = make_session_id(source, source_session_id)
    key_id_value = item_key or generate_ulid()
    if not ITEM_KEY_PATTERN.match(key_id_value):
        raise InboxBuildError(f"item_key 不是合法的 26 字元 ULID: {key_id_value!r}")

    now_value = now or (clock or SystemClock()).now_utc()
    snapshot_value = snapshot_at.strip() if _usable_time(snapshot_at) else now_value
    raw_sha256, raw_size = _hash_raw(raw_p, max_raw)

    # 建立／更新時間取自轉換器；取不到才退回匯入時間，並留下 time_source 標記。
    from_source_created = _usable_time(facts.created_at)
    from_source_updated = _usable_time(facts.updated_at)
    created_at = facts.created_at.strip() if from_source_created else now_value
    updated_at = facts.updated_at.strip() if from_source_updated else now_value

    metadata: dict[str, Any] = {
        "id": item_id,
        "type": "session",
        "created_at": created_at,
        "updated_at": updated_at,
        "case_id": case_id,
        "provenance": provenance,
    }
    if not (from_source_created and from_source_updated):
        metadata["time_source"] = TIME_SOURCE_IMPORT

    sidecar: dict[str, Any] = {
        "format": SIDECAR_FORMAT,
        "item_key": key_id_value,
        "profile": profile,
        "metadata": metadata,
        "raw": {
            "sha256": raw_sha256,
            "size": raw_size,
        },
        "session": {
            "source": source,
            "source_session_id": source_session_id,
            "snapshot_at": snapshot_value,
            "status": status,
            "stopped_at": stopped_at if status == "stopped" else None,
            "in_progress": bool(facts.in_progress),
            "parent_id": _normalize_parent_id(parent_id, source),
        },
        "body": {},
    }

    # 自查：格式與 raw 一致性都通過才簽章
    errors = validate_sidecar(sidecar, expected_item_key=key_id_value)
    if errors:
        raise InboxBuildError(
            "組裝出的 sidecar 未通過驗證: "
            + "; ".join(f"{e.field}: {e.message}" for e in errors)
        )
    with open(raw_p, "rb") as raw_fh:
        raw_errors = check_raw(sidecar, raw_fh, max_size=max_raw)
    if raw_errors:
        raise InboxBuildError(
            "原始紀錄與 sidecar 不一致: "
            + "; ".join(f"{e.field}: {e.message}" for e in raw_errors)
        )

    sidecar_bytes = serialize_json(sidecar)
    return sidecar_bytes, sign_sidecar_bytes(sidecar_bytes, key, key_id)


def detect_source_session_id(source: str, raw_path: Path) -> str:
    """自原始紀錄推測來源端自己的 Session id。

    - opencode：匯出 JSON 的 `info.id`。
    - claude-code：jsonl 內最常見的 `sessionId`（同一份檔案含主幹與子代理紀錄）；
      完全沒有時退回檔名。

    判斷不確定時呼叫端應以 `--session-id` 明確指定。
    """
    p = Path(raw_path)
    if source == "opencode":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise InboxBuildError(f"opencode 匯出檔解析失敗: {e}") from None
        info = data.get("info") if isinstance(data, dict) else None
        sid = info.get("id") if isinstance(info, dict) else None
        if not isinstance(sid, str) or not sid.strip():
            raise InboxBuildError(
                "opencode 匯出檔缺少 info.id，請以 --session-id 明確指定"
            )
        return sid.strip()

    if source == "claude-code":
        counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        with open(p, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # 生成中的 Session 最後一行可能寫到一半；這裡只是嗅探 id，
                    # 真正的解析由轉換器負責（它對寫到一半的容錯更嚴格）。
                    continue
                if not isinstance(rec, dict):
                    continue
                sid = rec.get("sessionId")
                if isinstance(sid, str) and sid.strip():
                    sid = sid.strip()
                    counts[sid] = counts.get(sid, 0) + 1
                    first_seen.setdefault(sid, i)
        if counts:
            return min(counts.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))[0]
        stem = p.stem.strip()
        if not stem:
            raise InboxBuildError(
                "無法從 jsonl 推測 Session id（檔名也無法定），請以 --session-id 指定"
            )
        return stem

    raise InboxBuildError(f"不支援的來源應用: {source!r}")

"""AiStorage 收件匣項目組裝模組（寫入端共用）。

依據規格：
- docs/impl/group3-modules.md 第 7.4 節（`build_inbox_item` 為手動匯入工具與
  5.2 同步器共用，刻意放在 `aistorage/inbox_builder.py` 而非 importer 套件內，
  讓同步器不必匯入 CLI 套件）
- docs/impl/group5-7-modules.md 第 0 節（handoff／claim／reference／artifact
  四種型態與 `upload_item`，**同步器、skill、匯入、Foundry 共用同一份**）
- schemas/inbox-sidecar.schema.json、schemas/metadata-inbox.schema.json
- design D2（不可分單位）、D3（簽章與 profile 綁定）、D4（快照時間由寫入端記）

組裝出來的位元組就是寫進收件匣的位元組：簽章涵蓋 sidecar 的**原始位元組**
（`aistorage.inbox/v1\\n` + sidecar_bytes），所以 sidecar 只序列化一次，寫出時
不得重新格式化，否則驗章會失敗。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from aistorage.clock import Clock, SystemClock
from aistorage.converters.base import SessionFacts
from aistorage.inbox import (
    DEFAULT_MAX_RAW_SIZE,
    check_raw,
    sign_sidecar_bytes,
    validate_sidecar,
)
from aistorage.schema import (
    SESSION_ID_PATTERN,
    generate_ulid,
    make_item_id,
    make_session_id,
)

SIDECAR_FORMAT = "aistorage.inbox/v1"

#: 項目識別碼（ULID）與 profile 名稱的格式。
ITEM_KEY_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")

#: 簽章金鑰識別碼必須是 `<profile>-<sha256(公鑰)前8位>`（2.3）。
#: 在本機先檢查，避免上傳之後才被提交流程拒收成 unauthorized。
KEY_ID_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9-]*-[0-9a-f]{8}$")

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
    time_source: str | None = None,
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
        time_source: 強制標記 metadata.time_source（呼叫端知道時間不是從來源端
            來的時候用，例如匯入工具用檔案 mtime 當保守值）。

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
    if not isinstance(key_id, str) or not KEY_ID_PREFIX_PATTERN.match(key_id):
        raise InboxBuildError(
            f"簽章金鑰識別碼不合法 ({key_id!r})，必須是 <profile>-<公鑰雜湊前8位小寫hex>"
        )
    if not key_id.startswith(f"{profile}-"):
        raise InboxBuildError(
            f"簽章金鑰識別碼 '{key_id}' 不屬於 profile '{profile}'（必須以 '<profile>-' 開頭）"
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
    if time_source or not (from_source_created and from_source_updated):
        metadata["time_source"] = time_source or TIME_SOURCE_IMPORT

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


# ---------------------------------------------------------------------------
# 第 0 節：其他型態（handoff／claim／reference／artifact）與上傳
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuiltItem:
    """組好並簽章的收件匣項目（還沒寫出去）。

    `sidecar_bytes` 就是必須**逐位元組**寫出去的內容；重新格式化會讓驗章失敗。
    `raw_path` 只有 session／rewrite／artifact(contained) 才有，其餘為 None。
    """

    item_key: str
    item_id: str
    sidecar_bytes: bytes
    sig: dict
    raw_path: Path | None

    @property
    def item_type(self) -> str:
        return str(self.sidecar["metadata"]["type"])

    @property
    def sidecar(self) -> dict:
        """已組好的 sidecar 字典（唯讀檢視用；不要重新序列化）。"""
        return json.loads(self.sidecar_bytes.decode("utf-8"))

    @property
    def raw_sha256(self) -> str | None:
        raw = self.sidecar.get("raw")
        return raw.get("sha256") if isinstance(raw, dict) else None

    @property
    def raw_size(self) -> int | None:
        raw = self.sidecar.get("raw")
        return raw.get("size") if isinstance(raw, dict) else None

    def file_names(self) -> tuple[str, str, str]:
        """收件匣裡的三個檔名（item_key.raw／.sidecar.json／.sig）。"""
        return (
            f"{self.item_key}.raw",
            f"{self.item_key}.sidecar.json",
            f"{self.item_key}.sig",
        )

    def local_files(self) -> tuple[tuple[str, Path], ...]:
        """寫到本機目錄時的 (檔名, 內容來源)；raw 沒有就只兩個。"""
        out = [(self.file_names()[1], None), (self.file_names()[2], None)]
        if self.raw_path is not None:
            out.insert(0, (self.file_names()[0], self.raw_path))
        return out


def _check_signer(profile: str, key_id: str) -> None:
    """本機先檢查 profile 與簽章金鑰一致，避免上傳後才被拒收成 unauthorized。"""
    if not isinstance(profile, str) or not PROFILE_PATTERN.match(profile):
        raise InboxBuildError(
            f"profile 名稱不合法 ({profile!r})，必須符合 ^[a-z][a-z0-9-]*$"
        )
    if not isinstance(key_id, str) or not KEY_ID_PREFIX_PATTERN.match(key_id):
        raise InboxBuildError(
            f"簽章金鑰識別碼不合法 ({key_id!r})，必須是 <profile>-<公鑰雜湊前8位小寫hex>"
        )
    if not key_id.startswith(f"{profile}-"):
        raise InboxBuildError(
            f"簽章金鑰識別碼 '{key_id}' 不屬於 profile '{profile}'（必須以 '<profile>-' 開頭）"
        )


def _check_item_key(item_key: str | None) -> str:
    value = item_key or generate_ulid()
    if not ITEM_KEY_PATTERN.match(value):
        raise InboxBuildError(f"item_key 不是合法的 26 字元 ULID: {value!r}")
    return value


def _sign_and_check(
    *,
    item_type: str,
    item_id: str,
    profile: str,
    key: bytes,
    key_id: str,
    body: dict,
    raw: dict | None,
    raw_path: Path | None,
    created_at: str,
    updated_at: str,
    case_id: str | None,
    provenance: str | None,
    item_key: str | None,
    max_raw: int,
) -> BuiltItem:
    """組出 sidecar、自查（sidecar ＋ raw）、簽章，回傳 BuiltItem。"""
    _check_signer(profile, key_id)
    key_value = _check_item_key(item_key)
    if not isinstance(created_at, str) or not _usable_time(created_at):
        raise InboxBuildError("created_at 必須是可用的時間字串")
    if not isinstance(updated_at, str) or not _usable_time(updated_at):
        raise InboxBuildError("updated_at 必須是可用的時間字串")

    metadata: dict[str, Any] = {
        "id": item_id,
        "type": item_type,
        "created_at": created_at,
        "updated_at": updated_at,
        "case_id": case_id,
        "provenance": provenance,
    }
    sidecar: dict[str, Any] = {
        "format": SIDECAR_FORMAT,
        "item_key": key_value,
        "profile": profile,
        "metadata": metadata,
        "raw": raw,
        "body": body,
    }

    errors = validate_sidecar(sidecar, expected_item_key=key_value)
    if errors:
        raise InboxBuildError(
            f"組裝出的 {item_type} sidecar 未通過驗證: "
            + "; ".join(f"{e.field}: {e.message}" for e in errors)
        )
    if raw_path is not None:
        with open(raw_path, "rb") as raw_fh:
            raw_errors = check_raw(sidecar, raw_fh, max_size=max_raw)
        if raw_errors:
            raise InboxBuildError(
                "原始紀錄與 sidecar 不一致: "
                + "; ".join(f"{e.field}: {e.message}" for e in raw_errors)
            )
    elif raw is not None:
        raise InboxBuildError("sidecar 宣告了 raw，但沒有對應的本體檔案")

    sidecar_bytes = serialize_json(sidecar)
    return BuiltItem(
        item_key=key_value,
        item_id=item_id,
        sidecar_bytes=sidecar_bytes,
        sig=sign_sidecar_bytes(sidecar_bytes, key, key_id),
        raw_path=raw_path,
    )


def _now(
    now: str | None, clock: Clock | None, created_at: str | None, updated_at: str | None
) -> tuple[str, str]:
    current = now or (clock or SystemClock()).now_utc()
    return (created_at or current, updated_at or current)


def build_session_item(
    raw_path: Path,
    *,
    source: str,
    source_session_id: str,
    facts: SessionFacts,
    profile: str,
    key: bytes,
    key_id: str,
    **kwargs: Any,
) -> BuiltItem:
    """`build_inbox_item` 的 BuiltItem 版本（同步器與 skill 走這裡）。"""
    sidecar_bytes, sig = build_inbox_item(
        raw_path,
        source=source,
        source_session_id=source_session_id,
        facts=facts,
        profile=profile,
        key=key,
        key_id=key_id,
        **kwargs,
    )
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    return BuiltItem(
        item_key=str(sidecar["item_key"]),
        item_id=str(sidecar["metadata"]["id"]),
        sidecar_bytes=sidecar_bytes,
        sig=sig,
        raw_path=Path(raw_path),
    )


def build_handoff_item(
    *,
    target_session_id: str,
    continuation: dict[str, Any],
    body: dict[str, Any],
    profile: str,
    key: bytes,
    key_id: str,
    case_id: str | None = None,
    provenance: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    item_key: str | None = None,
    now: str | None = None,
    clock: Clock | None = None,
) -> BuiltItem:
    """組一張交接單（handoff）。

    body 必須含 `content`（交接說明字串）；其餘鍵（例如 title／summary／
    next_steps／author_session_id）原樣併入 sidecar 的 body。
    接續點由 `continuation` 帶（快照雜湊 ＋ message id）；提交流程會驗證它
    存在於目標 Session 的快照歷史、已完成、未撤銷且是該快照最後一則完成的訊息。
    """
    if not isinstance(body, dict):
        raise InboxBuildError("body 必須是字典")
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise InboxBuildError("handoff 的 body.content 必須是非空字串（交接說明）")
    if not isinstance(continuation, dict):
        raise InboxBuildError("continuation 必須是字典")
    snap = continuation.get("snapshot_sha256")
    message_id = continuation.get("message_id")
    if not isinstance(snap, str) or not re.fullmatch(r"[0-9a-f]{64}", snap):
        raise InboxBuildError("continuation.snapshot_sha256 必須是 64 字元小寫 hex")
    if not isinstance(message_id, str) or not message_id.strip():
        raise InboxBuildError("continuation.message_id 必須是非空字串")

    item_key_value = _check_item_key(item_key)
    created, updated = _now(now, clock, created_at, updated_at)
    merged: dict[str, Any] = {
        **{k: v for k, v in body.items() if k != "content"},
        "target_session_id": target_session_id,
        "continuation": {"snapshot_sha256": snap, "message_id": message_id},
        "content": content,
    }
    return _sign_and_check(
        item_type="handoff",
        item_id=make_item_id("handoff"),
        profile=profile,
        key=key,
        key_id=key_id,
        body=merged,
        raw=None,
        raw_path=None,
        created_at=created,
        updated_at=updated,
        case_id=case_id,
        provenance=provenance,
        item_key=item_key_value,
        max_raw=DEFAULT_MAX_RAW_SIZE,
    )


def build_claim_item(
    *,
    handoff_id: str,
    claimer_session_id: str,
    profile: str,
    key: bytes,
    key_id: str,
    case_id: str | None = None,
    provenance: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    item_key: str | None = None,
    now: str | None = None,
    clock: Clock | None = None,
    extra: dict[str, Any] | None = None,
) -> BuiltItem:
    """組一張認領單（claim）。claimer 必須已經在 Agora 裡（所以要先上傳它自己的
    session，兩者同一批提交，apply 的順序 session 在前）。"""
    if not re.fullmatch(r"handoff:[0-9A-HJKMNP-TV-Z]{26}", str(handoff_id or "")):
        raise InboxBuildError(f"handoff_id 必須是 handoff:<ULID>: {handoff_id!r}")
    created, updated = _now(now, clock, created_at, updated_at)
    body: dict[str, Any] = {
        "handoff_id": handoff_id,
        "claimer_session_id": claimer_session_id,
    }
    if extra:
        body.update(extra)
    return _sign_and_check(
        item_type="claim",
        item_id=make_item_id("claim"),
        profile=profile,
        key=key,
        key_id=key_id,
        body=body,
        raw=None,
        raw_path=None,
        created_at=created,
        updated_at=updated,
        case_id=case_id,
        provenance=provenance,
        item_key=_check_item_key(item_key),
        max_raw=DEFAULT_MAX_RAW_SIZE,
    )


def build_reference_item(
    *,
    from_session_id: str,
    to_session_id: str,
    read_snapshot_at: str,
    profile: str,
    key: bytes,
    key_id: str,
    case_id: str | None = None,
    provenance: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    item_key: str | None = None,
    now: str | None = None,
    clock: Clock | None = None,
    extra: dict[str, Any] | None = None,
) -> BuiltItem:
    """組一筆參考（reference）。`read_snapshot_at` 是剛讀到的對方快照時間，
    必須與來源端對該 Session 的 `updated_at` 單調（提交流程擋舊值）。"""
    if not _usable_time(read_snapshot_at):
        raise InboxBuildError("read_snapshot_at 必須是可用的時間字串")
    created, updated = _now(now, clock, created_at, updated_at)
    body: dict[str, Any] = {
        "from_session_id": from_session_id,
        "to_session_id": to_session_id,
        "read_snapshot_at": read_snapshot_at,
    }
    if extra:
        body.update(extra)
    return _sign_and_check(
        item_type="reference",
        item_id=make_item_id("reference"),
        profile=profile,
        key=key,
        key_id=key_id,
        body=body,
        raw=None,
        raw_path=None,
        created_at=created,
        updated_at=updated,
        case_id=case_id,
        provenance=provenance,
        item_key=_check_item_key(item_key),
        max_raw=DEFAULT_MAX_RAW_SIZE,
    )


def build_artifact_item(
    *,
    kind: Literal["link", "contained"],
    produced_by_session_id: str,
    name: str,
    profile: str,
    key: bytes,
    key_id: str,
    content_type: str | None = None,
    link: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    raw_path: Path | None = None,
    case_id: str | None = None,
    provenance: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    item_key: str | None = None,
    now: str | None = None,
    clock: Clock | None = None,
    max_raw: int = DEFAULT_MAX_RAW_SIZE,
) -> BuiltItem:
    """組一筆產出登錄（artifact，第 7 組 Foundry）。

    - `link`：本體是對外連結，沒有 raw。
    - `contained`：本體放在 `<item_key>.raw`，必須給 content_type 與 raw_path。
    """
    if kind not in ("link", "contained"):
        raise InboxBuildError(f"kind 必須是 'link' 或 'contained': {kind!r}")
    if not _usable_time(name):
        raise InboxBuildError("產出的檔名（name）必須是非空字串")

    body: dict[str, Any] = {"kind": kind, "produced_by_session_id": produced_by_session_id}
    raw_meta: dict[str, Any] | None = None
    if kind == "link":
        if not _usable_time(link):
            raise InboxBuildError("kind=link 時必須提供 link（對外連結）")
        body["link"] = link
        if content_type:
            body["content_type"] = content_type
        if repo is not None:
            body["repo"] = repo
        if path is not None:
            body["path"] = path
    else:
        if not _usable_time(content_type):
            raise InboxBuildError("kind=contained 時必須提供 content_type")
        if raw_path is None:
            raise InboxBuildError("kind=contained 時必須提供 raw_path（本體檔案）")
        raw_p = Path(raw_path)
        if not raw_p.is_file():
            raise InboxBuildError(f"找不到產出本體檔案: {raw_p}")
        sha256, size = _hash_raw(raw_p, max_raw)
        body["content_type"] = content_type
        body["link"] = None
        raw_meta = {"sha256": sha256, "size": size}
        if repo is not None:
            body["repo"] = repo
        if path is not None:
            body["path"] = path

    created, updated = _now(now, clock, created_at, updated_at)
    return _sign_and_check(
        item_type="artifact",
        item_id=make_item_id("artifact"),
        profile=profile,
        key=key,
        key_id=key_id,
        body=body,
        raw=raw_meta,
        raw_path=Path(raw_path) if raw_path is not None else None,
        created_at=created,
        updated_at=updated,
        case_id=case_id,
        provenance=provenance,
        item_key=_check_item_key(item_key),
        max_raw=max_raw,
    )


def upload_item(
    drive: Any, inbox_folder_id: str, item: BuiltItem
) -> tuple[str, ...]:
    """把一個組好的項目上傳到收件匣資料夾，回傳建立的 file id（依上傳順序）。

    順序固定為 **raw → sidecar → sig**（D2 的不可分單位：提交流程看到 sig
    就視為完整項目，缺 sig 的半套會被保留到 24 小時後當孤兒清掃）。
    沒有本體的型態（handoff／claim／reference）只上 sidecar 與 sig。
    憑證由 drive 物件自帶，這裡不碰任何秘密。
    """
    raw_name, sidecar_name, sig_name = item.file_names()
    payloads: list[tuple[str, Any, str]] = []
    if item.raw_path is not None:
        payloads.append((raw_name, item.raw_path, "application/octet-stream"))
    payloads.append((sidecar_name, item.sidecar_bytes, "application/json"))
    payloads.append((sig_name, serialize_json(item.sig), "application/json"))

    created: list[str] = []
    for filename, content, mime in payloads:
        created.append(
            drive.create(inbox_folder_id, filename, content, mime_type=mime).id
        )
    return tuple(created)

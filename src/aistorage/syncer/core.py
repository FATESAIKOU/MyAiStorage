"""同步器（tasks 5.2）：把 opencode 的 Session 同步進 Agora 的收件匣。

依據：docs/impl/group5-7-modules.md 第 2.1〜2.3 節、design D4、Q3、
技術驗證 1.7a／1.7b／1.7e／1.7h、PM 決定 3（補傳條件）。

`sync_once` 是**純邏輯**：api／reader／drive／signer／clock／workdir 全部注入，
所以單元測試可以用假的 opencode（FakeOpencodeApi）＋ FakeDrive 跑完所有分支，
不必真的開 opencode、也不碰網路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from aistorage.clock import Clock, format_rfc3339
from aistorage.converters.base import ConversionError, SessionFacts
from aistorage.drive.model import DriveClient
from aistorage.errors import AiStorageError, IncompleteFetch, ReadError
from aistorage.inbox_builder import build_session_item, load_private_key, upload_item
from aistorage.syncer.opencode_api import OpencodeApi, OcSession
from aistorage.syncer.state import SyncState

# 原始紀錄大小上限（超過就不上傳，交給 6.3 回報；D2 100 MiB）
MAX_RAW_BYTES = 100 * 1024 * 1024

SOURCE = "opencode"


@runtime_checkable
class ReaderLike(Protocol):
    """同步器需要的讀取介面（AgoraReader 的一小部分，方便注入假的）。"""

    def catalog(self, session_ids: Sequence[str]) -> Any:
        ...

    def get_rejection(self, item_key: str) -> Any:
        ...

    def manifest(self) -> Any:
        ...


@runtime_checkable
class ConverterLike(Protocol):
    """轉換器（只需要 facts；閱讀版是提交流程的事）。"""

    source: str

    def facts(self, raw_path: Path, *, session_id: str | None = None) -> SessionFacts:
        ...


@dataclass(frozen=True)
class Signer:
    """簽章金鑰（只以路徑／位元組持有；不進 log、不進 repr）。"""

    profile: str
    key_id: str
    key: bytes

    def __repr__(self) -> str:  # 不洩漏金鑰
        return f"<Signer profile={self.profile} key_id={self.key_id}>"

    @classmethod
    def from_key_file(
        cls, path: Path | str, *, profile: str, key_id: str | None = None
    ) -> Signer:
        """自金鑰檔建立；key_id 未給就用公鑰推導（`<profile>-<sha256前8位>`）。"""
        key = load_private_key(path)
        if key_id:
            return cls(profile=profile, key_id=key_id, key=key)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv = Ed25519PrivateKey.from_private_bytes(key)
        pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        digest = hashlib.sha256(pub).hexdigest()[:8]
        return cls(profile=profile, key_id=f"{profile}-{digest}", key=key)


@dataclass(frozen=True)
class SyncOutcome:
    """一輪同步的結果（只有 id 與計數，沒有內容）。"""

    uploaded: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    reuploaded: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    resumed_after_stop: tuple[str, ...] = ()
    too_large: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def touched(self) -> tuple[str, ...]:
        """這一輪有實際動作（上傳或重傳）的 Session。"""
        return tuple(sorted(set(self.uploaded) | set(self.reuploaded)))

    def counts(self) -> dict[str, int]:
        return {
            "uploaded": len(self.uploaded),
            "waiting": len(self.waiting),
            "unchanged": len(self.unchanged),
            "reuploaded": len(self.reuploaded),
            "rejected": len(self.rejected),
            "resumed_after_stop": len(self.resumed_after_stop),
            "too_large": len(self.too_large),
            "errors": len(self.errors),
        }


def _value(result: Any, default: Any = None) -> Any:
    """讀取介面回傳 `Result[T]`；測試注入的假物件可能直接回值。"""
    if result is None:
        return default
    inner = getattr(result, "value", None)
    if inner is not None or hasattr(result, "value"):
        return inner
    return result


def entry_field(entry: Any, name: str, default: Any = None) -> Any:
    """讀 `reader.catalog()` 回來的一筆，欄位名不分 dict 或 dataclass。

    真實的 `AgoraReader.catalog()` 回的是 `CatalogEntry` dataclass
    （session_id / raw_sha256 / snapshot_at），不是 dict。9.1 e2e 才發現
    同步器一直假設是 dict，所以「已經在 Agora 裡 → unchanged」與「等待中 →
    補傳」兩條路徑在真實環境從來沒執行過：一有非空 catalog 就
    `AttributeError: 'CatalogEntry' object has no attribute 'get'`。
    單元測試用 dict 假讀取端，所以沒抓到。
    """
    if entry is None:
        return default
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _manifest_info(reader: ReaderLike) -> tuple[str | None, int | None]:
    """讀取視圖目前世代的 (published_at, generation)；讀不到就 (None, None)。

    補傳判斷（PM 決定 3）與上傳紀錄的世代都要用到。
    """
    try:
        manifest = reader.manifest()
    except Exception:
        return None, None
    if manifest is None:
        return None, None
    if isinstance(manifest, dict):
        published = manifest.get("published_at")
        gen = manifest.get("generation")
        return (
            published if isinstance(published, str) else None,
            int(gen) if isinstance(gen, int) else None,
        )
    published = getattr(manifest, "published_at", None)
    gen = getattr(manifest, "generation", None)
    return (
        published if isinstance(published, str) else None,
        int(gen) if isinstance(gen, int) else None,
    )


def _is_stopped(archived_ms: int | None, last_message_created_ms: int | None) -> bool:
    """停止的判定（1.7e + D4）：封存時間之後**沒有新訊息被建立**才算停止。

    `archived = 0` 與沒有封存都視為未封存；拿不到訊息時間（facts 失敗）時
    保守視為**未**停止——寧可多同步一次，也不要把還在用的 Session 標成停止。

    **用 created，不用 completed**（review-g5-6 H3、D10）：宣告停止一定發生在
    AI 回覆生成中，那一則訊息在封存**之前**建立、封存**之後**才完成。用
    completed 判，下一輪會自己恢復成 running，宣告停止永遠不成立。
    這條規則與 `agora/apply._is_stopped` 必須一致。
    """
    if not archived_ms or archived_ms <= 0:
        return False
    if last_message_created_ms is None:
        return False
    return last_message_created_ms <= archived_ms


def _facts_or_none(converter: ConverterLike, raw: Path, session_id: str) -> SessionFacts | None:
    try:
        return converter.facts(raw, session_id=session_id)
    except (ConversionError, Exception):  # noqa: B014 - facts 失敗以保守值處理
        return None


def sync_once(
    *,
    api: OpencodeApi,
    reader: ReaderLike,
    drive: DriveClient,
    inbox_folder_id: str,
    signer: Signer,
    state: SyncState,
    clock: Clock,
    workdir: Path,
    converter: ConverterLike | None = None,
    only: Sequence[str] | None = None,
    max_raw: int = MAX_RAW_BYTES,
    source: str = SOURCE,
    keep_exports: bool = False,
) -> SyncOutcome:
    """跑一輪同步：把有新版本的 Session 上傳到收件匣。

    規則（docs/impl/group5-7 第 2.2 節）：
    1. 先問 API 要全部 Session，再一次 `reader.catalog()` 拿 Agora 裡的
       `{raw_sha256, snapshot_at, status}`（Q3：不逐個下載閱讀版）。
    2. 每個 Session：**先記快照時間再匯出**（D4 的擷取時間），算 sha256。
       - sha 與 Agora 相同 → unchanged，清掉等待狀態。
       - sha 等於上次上傳的、Agora 還沒有 → waiting，不重傳。
         - 補傳（PM 決定 3）：上傳之後**已經有新的世代發佈**、Agora 仍然沒有、
           而且沒有拒收記錄 → 用新的 item_key 重傳。
         - 有拒收記錄 → 記 rejected，**不自動重傳**（避免無限迴圈）。
       - 其他 → 上傳新版本。
    3. `facts` 失敗 → 以保守值（running、in_progress=False、title=None）上傳，
       閱讀版交給提交流程標記失敗。
    4. 停止：封存時間之後沒有新訊息。`stopped_at` 取**同步器第一次觀測到**的時間。
       Agora 已經是 stopped 但本地又有新訊息 → 以 running 上傳並列入
       `resumed_after_stop`（daemon 看到就立刻同步並提交）。
    5. `parent_id` 取 API 的 parentID（子 Session 也是 Agora 的 Session）。
    6. 超過 raw 上限 → 不上傳，標 too_large。
    7. **取得不完整**（匯出被截斷、形狀不符、訊息對不上 API）→ 不上傳，
       記成 `incomplete`（明確有別於 ReadError 的「讀不到」）。半份原始紀錄
       進了 Agora 就再也分不出來，所以寧可這一輪不收。
    8. `keep_exports=True`：上傳之後保留匯出檔（`skill` 算接續點時需要；
       預設會刪掉，因為匯出檔是原始紀錄的副本，留在 /work 會累積真實對話
       內容，而且抹除 6.1 不會涵蓋那個目錄——review-g5-6 M5）。
    """
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    export_dir = work / "exports"

    sessions = api.list_sessions()
    if only is not None:
        wanted = {s if ":" in s else f"{source}:{s}" for s in only}
        sessions = [s for s in sessions if s.session_id(source) in wanted]

    catalog_in = _value(reader.catalog([s.session_id(source) for s in sessions]), {}) or {}
    _published_at, generation = _manifest_info(reader)

    uploaded: list[str] = []
    waiting: list[str] = []
    unchanged: list[str] = []
    reuploaded: list[str] = []
    rejected: list[tuple[str, str]] = []
    resumed: list[str] = []
    too_large: list[str] = []
    errors: list[tuple[str, str]] = []

    for oc in sessions:
        sid = oc.session_id(source)
        rec = state.record(sid)
        try:
            # D4：快照時間在匯出**之前**記錄
            snapshot_at = clock.now_utc()
            export = api.export(oc.id, export_dir / f"{oc.id}.json")
            raw_bytes = export.read_bytes()
            sha = hashlib.sha256(raw_bytes).hexdigest().lower()
            rec.last_seen_sha = sha
            rec.last_synced_at = snapshot_at

            if len(raw_bytes) > max_raw:
                # 不上傳；讓 6.3 回報（同步器不丟棄資料，也不偷偷縮小）
                rec.too_large = True
                rec.error_code = "too_large"
                too_large.append(sid)
                continue
            rec.too_large = False

            entry = (catalog_in or {}).get(sid)
            in_agora = bool(entry) and entry_field(entry, "raw_sha256") == sha

            if in_agora:
                # 已經收進 Agora：清掉等待／拒收狀態
                unchanged.append(sid)
                rec.last_uploaded_sha = sha
                rec.last_item_key = rec.last_item_key or None
                rec.error_code = None
                continue

            if rec.last_uploaded_sha == sha and not rec.error_code:
                # 上傳過但 Agora 還沒看到 → 等待中（PM 決定 3 的補傳條件）
                rejection = _rejection_code(reader, rec.last_item_key)
                if rejection:
                    rec.error_code = "rejected"
                    rejected.append((sid, rejection))
                    continue
                # M2：用「世代」判斷，不比較兩台機器的時間字串
                # （published_at 是 committer 的時鐘、uploaded_at 是 Mac 的時鐘）。
                newer_generation = bool(
                    generation is not None
                    and rec.uploaded_generation is not None
                    and generation > rec.uploaded_generation
                )
                if not newer_generation:
                    waiting.append(sid)
                    continue
                # 有新世代發佈了仍看不到 → 補傳（新的 item_key）
                outcome = _upload_one(
                    drive=drive,
                    inbox_folder_id=inbox_folder_id,
                    signer=signer,
                    converter=converter,
                    state=state,
                    clock=clock,
                    oc=oc,
                    raw=export,
                    raw_sha256=sha,
                    snapshot_at=snapshot_at,
                    source=source,
                    max_raw=max_raw,
                    generation=generation,
                    agora_status=entry_field(entry, "status"),
                    keep_export=keep_exports,
                )
                if outcome.error:
                    errors.append((sid, outcome.error))
                    rec.error_code = outcome.error
                    continue
                reuploaded.append(sid)
                if outcome.resumed:
                    resumed.append(sid)
                continue

            if rec.error_code == "rejected":
                # 已經知道被拒收：同一個 sha 不再重傳（新的 sha 才試）
                if rec.last_uploaded_sha == sha:
                    rejected.append((sid, "rejected"))
                    continue
                rec.error_code = None

            outcome = _upload_one(
                drive=drive,
                inbox_folder_id=inbox_folder_id,
                signer=signer,
                converter=converter,
                state=state,
                clock=clock,
                oc=oc,
                raw=export,
                raw_sha256=sha,
                snapshot_at=snapshot_at,
                source=source,
                max_raw=max_raw,
                generation=generation,
                agora_status=entry_field(entry, "status"),
                keep_export=keep_exports,
            )
            if outcome.error:
                errors.append((sid, outcome.error))
                rec.error_code = outcome.error
                continue
            uploaded.append(sid)
            if outcome.resumed:
                resumed.append(sid)
        except IncompleteFetch as e:
            # 匯出只拿到一部分（9.5 e2e：stdout 是 pipe 時超過約 64 KiB 就會被截斷，
            # rc 卻是 0）。`OpencodeApi.export` 在驗證通過前只寫暫存檔，所以這裡
            # **一定沒有半份被上傳**；這裡只負責把它明確記成「取得不完整」，
            # 不要混在 ReadError 裡（那是「讀不到」，處置與判讀都不同）。
            # last_seen_sha 刻意不更新：這一輪我們並沒有看到完整的 Session。
            errors.append((sid, f"incomplete: {e}"))
            rec.error_code = "incomplete"
        except Exception as e:  # noqa: BLE001 - 逐 Session 攔截，daemon 要繼續跑
            # 一個 Session 失敗不影響其他（daemon 記錄 id 與代碼後繼續）。
            # M1：原本只擋 (ReadError, OSError, ValueError)，而 `upload_item`
            # 丟的是 WriteError（AiStorageError 的子類）→ 會中止整輪，連已經
            # 上傳成功的 Session 都不會存進狀態（下一輪重複上傳，提交流程判
            # ALREADY，但收件匣會多出一組重複檔案）。
            # 這裡擋全部的 Exception：同步器是背景服務，一個 Session 壞掉不該
            # 讓整輪中斷。KeyboardInterrupt／SystemExit 不在 Exception 之下，
            # 所以 Ctrl-C 與關機訊號仍然會往上拋。
            # 只有例外型別等於沒說是什麼錯（9.1 e2e 實測：整輪只看到
            # `rejected/error: opencode:ses_… AttributeError`）。帶上訊息。
            code = f"{type(e).__name__}: {e}".strip()
            errors.append((sid, code))
            rec.error_code = type(e).__name__

    # M1：逐 Session 都攔住了，這裡一定會執行；狀態一定要存下來
    state.save()
    return SyncOutcome(
        uploaded=tuple(sorted(uploaded)),
        waiting=tuple(sorted(waiting)),
        unchanged=tuple(sorted(unchanged)),
        reuploaded=tuple(sorted(reuploaded)),
        rejected=tuple(sorted(rejected)),
        resumed_after_stop=tuple(sorted(resumed)),
        too_large=tuple(sorted(too_large)),
        errors=tuple(sorted(errors)),
    )


@dataclass
class _UploadOutcome:
    error: str | None = None
    resumed: bool = False


def _rejection_code(reader: ReaderLike, item_key: str | None) -> str | None:
    """查這個 item_key 有沒有被拒收（PM 決定 3：被拒收就不自動重傳）。"""
    if not item_key:
        return None
    try:
        row = _value(reader.get_rejection(item_key))
    except Exception:
        return None
    if row is None:
        return None
    code = getattr(row, "code", None) or (
        row.get("code") if isinstance(row, dict) else None
    )
    return str(code) if code else "rejected"


def _upload_one(
    *,
    drive: DriveClient,
    inbox_folder_id: str,
    signer: Signer,
    converter: ConverterLike | None,
    state: SyncState,
    clock: Clock,
    oc: OcSession,
    raw: Path,
    raw_sha256: str,
    snapshot_at: str,
    source: str,
    max_raw: int,
    generation: int | None = None,
    agora_status: str | None = None,
    keep_export: bool = False,
) -> _UploadOutcome:
    """組 item 並上傳（同步器的「唯一寫入點」）。"""
    sid = oc.session_id(source)
    rec = state.record(sid)
    facts = _facts_or_none(converter, raw, sid) if converter is not None else None
    if facts is None:
        # 保守值：視為還在跑、沒有標題；閱讀版交給提交流程標記失敗（review-g3e H2）
        facts = SessionFacts(
            title=None,
            created_at=None,
            updated_at=None,
            message_ids=(),
            archived_at=None,
            last_message_at=None,
            in_progress=False,
        )
        facts_ok = False
    else:
        facts_ok = True

    # H3：用「訊息被建立的時間」判斷（宣告停止時那則回覆在封存之前就建立了）
    stopped = _is_stopped(
        oc.archived_ms, getattr(facts, "last_message_created_ms", None)
    )
    # 恢復的判定以 **Agora 的狀態**為準：Agora 是 stopped、本地卻算出 running，
    # 才算「停止後被恢復」。本機記錄只用來記停止觀測到的時間，而且在恢復
    # 之後要**清掉**——否則之後每次上傳都會被列進 resumed_after_stop，
    # daemon 每 10 分鐘就會觸發一次 workflow（review-g5-6 H3）。
    was_stopped = agora_status == "stopped" and not stopped
    if not stopped and rec.stop_observed_at is not None:
        rec.stop_observed_at = None
    if stopped and rec.stop_observed_at is None:
        # 停止時間取同步器**第一次觀測到**的時間，不信來源端填的值（D4／3.9）
        rec.stop_observed_at = snapshot_at

    parent_id = oc.parent_session_id(source)
    item = build_session_item(
        raw,
        source=source,
        source_session_id=oc.id,
        facts=facts,
        profile=signer.profile,
        key=signer.key,
        key_id=signer.key_id,
        parent_id=parent_id,
        status="stopped" if (stopped and facts_ok) else "running",
        stopped_at=rec.stop_observed_at if (stopped and facts_ok) else None,
        case_id=None,          # 同步器不知道所屬案件；skill／plugin 可另外帶
        snapshot_at=snapshot_at,
        now=snapshot_at,
        max_raw=max_raw,
        archive_ms=oc.archived_ms,   # H3：宣告停止的判斷依據
    )
    upload_item(drive, inbox_folder_id, item)

    rec.last_uploaded_sha = raw_sha256
    rec.last_item_key = item.item_key
    rec.uploaded_at = snapshot_at
    rec.uploaded_generation = generation
    rec.error_code = None
    # M5：匯出檔是原始紀錄的副本，上傳成功後就不留（避免在 /work 累積
    # 真實對話內容，而且抹除 6.1 不會涵蓋這個目錄）。
    if not keep_export:
        _drop_export(item.raw_path)
    return _UploadOutcome(resumed=was_stopped)


def _drop_export(path: Path | None) -> None:
    """刪掉上傳後的匯出副本（刪不到就算了，不影響同步結果）。"""
    if path is None:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass

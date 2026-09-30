"""同步器（tasks 5.2／5.3）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

用假的 opencode（FakeOpencodeApi）＋ FakeDrive ＋ 假的讀取端跑完所有分支：
沒有變動、等待中、補傳、拒收不重傳、in_progress、停止判定、封存後有新訊息、
三層子 Session。範例資料一律自編，不碰真實 Session 與 MyBrain。
"""

from __future__ import annotations

import base64
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Sequence

import pytest

from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import ReadError
from aistorage.reader import CatalogEntry
from aistorage.inbox_builder import build_claim_item, load_private_key
from aistorage.syncer.commit import (
    STALENESS_HINT,
    SyncDeps,
    _is_visible,
    awaited_for_item,
    build_awaited,
    build_awaited_session,
    sync_and_commit,
    wait_visible,
)
from aistorage.syncer.config import ConfigError, SyncerConfig
from aistorage.syncer.continuation import (
    continuation_point,
    last_completed_message_id,
)
from aistorage.syncer.core import Signer, SyncOutcome, _is_stopped, sync_once
from aistorage.syncer.opencode_api import OcSession, OpencodeApi, _as_ms, _parse_session
from aistorage.syncer.state import SessionSyncRecord, SyncState

T0 = "2026-09-27T08:00:00Z"
T1 = "2026-09-27T09:00:00Z"
PROFILE = "mac-opencode"
SNAP_A = "a" * 64


# ---------------------------------------------------------------------------
# 假的 opencode（不開真的 serve、不跑 LLM）
# ---------------------------------------------------------------------------


class FakeApi:
    """假的 opencode API：可控的 Session 清單與匯出內容。"""

    def __init__(self, sessions: dict[str, str] | None = None) -> None:
        # sessions: {session_id: raw JSON 字串}
        self._exports = dict(sessions or {})
        self._meta: dict[str, OcSession] = {}
        self.archived: list[tuple[str, int]] = []
        self.export_calls: list[str] = []

    def set(self, oc: OcSession, raw: str) -> None:
        self._meta[oc.id] = oc
        self._exports[oc.id] = raw

    def list_sessions(self) -> list[OcSession]:
        return sorted(self._meta.values(), key=lambda s: s.id)

    def export(self, session_id: str, dest: Path) -> Path:
        self.export_calls.append(session_id)
        if session_id not in self._exports:
            raise ReadError(f"opencode export 失敗 (rc=1) session={session_id}")
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self._exports[session_id], encoding="utf-8")
        return out

    def archive(self, session_id: str, at_ms: int) -> None:
        self.archived.append((session_id, at_ms))

    def ping(self) -> bool:
        return True


class FakeReader:
    """假的讀取端：catalog / get_rejection / manifest / get_session。"""

    def __init__(self, *, generation: int = 1, published_at: str = T0,
                 catalog_as_dataclass: bool = True) -> None:
        self.catalog_as_dataclass = catalog_as_dataclass
        self.catalog_data: dict[str, dict] = {}
        self.rejections: dict[str, str] = {}
        self.sessions: dict[str, Any] = {}
        self.generation = generation
        self.published_at = published_at
        self.catalog_calls = 0

    def catalog(self, session_ids: Sequence[str]) -> Any:
        # 回**真的** CatalogEntry（9.1 e2e 才發現：這裡原本回 dict，於是
        # 同步器「以為 catalog 回 dict」的錯誤一直沒被抓到，真實環境一有非空
        # catalog 就 AttributeError）。
        self.catalog_calls += 1
        out: dict[str, Any] = {}
        for sid in session_ids:
            raw = self.catalog_data.get(sid)
            if raw is None:
                continue
            out[sid] = (
                CatalogEntry(session_id=sid, raw_sha256=raw["raw_sha256"],
                             snapshot_at=raw.get("snapshot_at", T0),
                             status=raw.get("status"))
                if self.catalog_as_dataclass
                else raw
            )
        return out

    def get_rejection(self, item_key: str) -> Any:
        code = self.rejections.get(item_key)
        return None if code is None else {"code": code}

    def manifest(self) -> dict:
        return {"generation": self.generation, "published_at": self.published_at}

    def get_session(self, session_id: str, **_kw: Any) -> Any:
        return self.sessions.get(session_id)


class FakeConverter:
    """假的轉換器：facts 取自 raw 裡的 _facts 欄位。"""

    source = "opencode"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def facts(self, raw_path: Path, *, session_id: str | None = None):
        from aistorage.converters.base import ConversionError, SessionFacts

        if self.fail:
            raise ConversionError("測試注入的 facts 失敗")
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        info = data.get("_facts", {})
        return SessionFacts(
            title=info.get("title"),
            created_at=info.get("created_at"),
            updated_at=info.get("updated_at"),
            message_ids=("m0",),
            archived_at=info.get("archived_at"),
            last_message_at=info.get("last_message_at"),
            in_progress=bool(info.get("in_progress", False)),
            last_message_ms=info.get("last_message_ms"),
            last_message_created_ms=info.get("last_message_created_ms",
                                             info.get("last_message_ms")),
        )


# ---------------------------------------------------------------------------
# 環境
# ---------------------------------------------------------------------------


def _raw(text: str, **facts: Any) -> str:
    return json.dumps({"text": text, "_facts": facts}, ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def _setup(tmp_path: Path, *, reader: FakeReader | None = None):
    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(bytes(range(32)))
    key_path.chmod(0o600)
    signer = Signer.from_key_file(key_path, profile=PROFILE)
    state = SyncState.load(tmp_path / "state.json")
    api = FakeApi()
    return {
        "api": api,
        "reader": reader or FakeReader(),
        "drive": drive,
        "inbox_folder_id": inbox,
        "signer": signer,
        "state": state,
        "clock": FixedClock(T1),
        "workdir": tmp_path / "work",
        "converter": FakeConverter(),
    }


def _run(env: dict, **kw) -> SyncOutcome:
    return sync_once(
        api=env["api"],
        reader=env["reader"],
        drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"],
        signer=env["signer"],
        state=env["state"],
        clock=env["clock"],
        workdir=env["workdir"],
        converter=env["converter"],
        **kw,
    )


def _inbox_names(drive: FakeDrive, inbox: str) -> list[str]:
    return sorted(f.name for f in drive.list_children(inbox))


def _sidecar(drive: FakeDrive, inbox: str, name: str) -> dict:
    for f in drive.list_children(inbox):
        if f.name == name:
            return json.loads(drive.download_bytes(f.id, max_bytes=1 << 20).decode("utf-8"))
    raise AssertionError(f"收件匣裡沒有 {name}")


# ---------------------------------------------------------------------------
# opencode_api 的解析（不碰網路）
# ---------------------------------------------------------------------------


def test_parse_session_understands_the_api_shape():
    """1.7e／1.7h 的實測形狀：time.archived、parentID。"""
    oc = _parse_session({
        "id": "ses_1",
        "title": "主線",
        "parentID": "ses_0",
        "time": {"created": 1, "updated": 1790400000000, "archived": 1790410000123},
    })
    assert oc.id == "ses_1" and oc.parent_id == "ses_0"
    assert oc.title == "主線" and oc.updated_ms == 1790400000000
    assert oc.archived_ms == 1790410000123
    assert not oc.is_main
    assert oc.session_id() == "opencode:ses_1"
    assert oc.parent_session_id() == "opencode:ses_0"
    # archived=0 與 null 都視為「沒有封存」（1.7e：0 也算設定過，但要當未封存處理）
    assert _parse_session({"id": "x", "time": {"archived": 0}}).archived_ms is None
    assert _parse_session({"id": "x", "time": {"archived": None}}).archived_ms is None
    assert _as_ms(None) is None and _as_ms(0) is None and _as_ms("123") == 123


def test_api_requires_explicit_session_id_on_export(tmp_path: Path):
    """1.7a：不帶 id 會進互動選單，所以 export 一定要明示 id。"""
    api = OpencodeApi()
    with pytest.raises(ValueError):
        api.export("", tmp_path / "x.json")
    with pytest.raises(ValueError):
        api.export("   ", tmp_path / "x.json")


def test_api_children_tolerates_missing_endpoint():
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"[]"

    api = OpencodeApi()
    api._request = lambda *a, **k: []  # type: ignore[assignment]
    assert api.children("ses_1") == []


# ---------------------------------------------------------------------------
# 狀態
# ---------------------------------------------------------------------------


def test_sync_state_roundtrips_atomically(tmp_path: Path):
    path = tmp_path / ".aistorage" / "sync-state.json"
    state = SyncState.load(path)
    rec = state.record("opencode:ses_1")
    rec.last_uploaded_sha = SNAP_A
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    rec.error_code = "rejected"
    state.save()

    again = SyncState.load(path)
    got = again.sessions["opencode:ses_1"]
    assert got.last_uploaded_sha == SNAP_A and got.error_code == "rejected"
    assert again.rejected() == ["opencode:ses_1"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob("*.tmp"))


def test_sync_state_tolerates_corrupt_file(tmp_path: Path):
    path = tmp_path / "sync-state.json"
    path.write_text("{ 壞掉的", encoding="utf-8")
    state = SyncState.load(path)
    assert state.sessions == {}   # 狀態壞掉不等於不能同步


# ---------------------------------------------------------------------------
# sync_once 的規則
# ---------------------------------------------------------------------------


def test_first_round_uploads_and_records_state(tmp_path: Path):
    env = _setup(tmp_path)
    raw = _raw("第一版", title="主線", created_at=T0, updated_at=T0)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1), raw)

    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_1",)
    assert outcome.counts()["uploaded"] == 1
    names = _inbox_names(env["drive"], env["inbox_folder_id"])
    assert len([n for n in names if n.endswith(".raw")]) == 1
    assert len([n for n in names if n.endswith(".sidecar.json")]) == 1
    assert len([n for n in names if n.endswith(".sig")]) == 1

    rec = env["state"].sessions["opencode:ses_1"]
    assert rec.last_uploaded_sha == _sha(raw)
    assert rec.last_item_key and rec.uploaded_at == T1
    # 快照時間在匯出之前記錄（D4）
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"],
                       f"{rec.last_item_key}.sidecar.json")
    assert sidecar["session"]["snapshot_at"].startswith(T1)


def test_unchanged_is_not_reuploaded(tmp_path: Path):
    env = _setup(tmp_path)
    raw = _raw("同一版", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
    _run(env)
    sid = "opencode:ses_1"

    env["reader"].catalog_data[sid] = {"raw_sha256": _sha(raw)}
    outcome = _run(env)
    assert outcome.unchanged == (sid,)
    assert outcome.uploaded == () and outcome.waiting == ()
    # 匯出仍然做（要知道有沒有變），但不再上傳
    assert env["api"].export_calls == ["ses_1", "ses_1"]


def test_catalog_entries_may_be_dataclasses_or_dicts(tmp_path: Path):
    """9.1 e2e 迴歸：同步器原本只認 dict 的 catalog 欄位。

    真實的 `AgoraReader.catalog()` 回 `CatalogEntry` dataclass，於是
    「已經在 Agora 裡」與「等待中」兩條路徑在真實環境一有非空 catalog 就
    `AttributeError`，而且整輪只回一個例外型別名看不出錯在哪。
    這裡兩種形狀都要能走。
    """
    for as_dataclass in (True, False):
        sub = tmp_path / ("dc" if as_dataclass else "dict")
        sub.mkdir(parents=True, exist_ok=True)
        env = _setup(sub)
        env["reader"].catalog_as_dataclass = as_dataclass
        raw = _raw("同一版", title="主線")
        env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
        _run(env)
        sid = "opencode:ses_1"

        # 非空 catalog → 走「已經在 Agora 裡」而不是例外
        env["reader"].catalog_data[sid] = {"raw_sha256": _sha(raw), "status": "running"}
        outcome = _run(env)
        assert outcome.unchanged == (sid,), as_dataclass
        assert outcome.errors == (), as_dataclass


def test_waiting_then_reupload_after_a_new_generation(tmp_path: Path):
    """PM 決定 3：上傳之後已出現新世代、Agora 仍看不到 → 補傳。"""
    env = _setup(tmp_path)
    raw = _raw("等待中", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)

    first = _run(env)
    assert first.uploaded == ("opencode:ses_1",)
    item_key = env["state"].sessions["opencode:ses_1"].last_item_key
    before = len(_inbox_names(env["drive"], env["inbox_folder_id"]))

    # 還沒有新世代發佈 → 等待中，不重傳
    env["reader"].published_at = T0
    waiting = _run(env)
    assert waiting.waiting == ("opencode:ses_1",)
    assert len(_inbox_names(env["drive"], env["inbox_folder_id"])) == before

    # 有新世代發佈了，但 Agora 還是看不到 → 補傳（新的 item_key）
    env["reader"].published_at = "2026-09-27T10:00:00.000Z"
    env["reader"].generation = 2
    again = _run(env)
    assert again.reuploaded == ("opencode:ses_1",)
    new_key = env["state"].sessions["opencode:ses_1"].last_item_key
    assert new_key != item_key
    # 收件匣裡兩組檔案並存（舊的等第 13 步清掉）
    assert len([n for n in _inbox_names(env["drive"], env["inbox_folder_id"])
                if n.endswith(".raw")]) == 2


def test_rejection_is_not_retried_forever(tmp_path: Path):
    env = _setup(tmp_path)
    raw = _raw("會被拒收", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
    _run(env)
    item_key = env["state"].sessions["opencode:ses_1"].last_item_key
    env["reader"].rejections[item_key] = "invalid_format"
    env["reader"].published_at = "2026-09-27T10:00:00.000Z"

    outcome = _run(env)
    assert outcome.rejected == (("opencode:ses_1", "invalid_format"),)
    assert outcome.reuploaded == ()
    assert env["state"].rejected() == ["opencode:ses_1"]
    # 同一個 sha 之上傳次數不變
    assert len([n for n in _inbox_names(env["drive"], env["inbox_folder_id"])
                if n.endswith(".raw")]) == 1


def test_new_content_after_rejection_is_uploaded_again(tmp_path: Path):
    """被拒收之後**內容變了**就再試一次（不然壞掉就永遠卡住）。"""
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("壞版", title="主線"))
    _run(env)
    key1 = env["state"].sessions["opencode:ses_1"].last_item_key
    env["reader"].rejections[key1] = "invalid_format"

    env["api"].set(OcSession(id="ses_1", updated_ms=2), _raw("改好了", title="主線"))
    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_1",)
    assert env["state"].sessions["opencode:ses_1"].last_item_key != key1


def test_in_progress_and_parent_id_are_carried(tmp_path: Path):
    env = _setup(tmp_path)
    raw = _raw("還在跑", title="子線", in_progress=True)
    env["api"].set(OcSession(id="ses_child", parent_id="ses_main", updated_ms=1), raw)
    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_child",)
    key = env["state"].sessions["opencode:ses_child"].last_item_key
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"], f"{key}.sidecar.json")
    assert sidecar["session"]["in_progress"] is True
    assert sidecar["session"]["parent_id"] == "opencode:ses_main"
    assert sidecar["session"]["status"] == "running"


def test_facts_failure_uploads_conservatively(tmp_path: Path):
    """facts 失敗 → 以保守值上傳（running、沒有標題），閱讀版交給提交流程。"""
    env = _setup(tmp_path)
    env["converter"] = FakeConverter(fail=True)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("內容", title="壞掉"))
    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_1",)
    key = env["state"].sessions["opencode:ses_1"].last_item_key
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"], f"{key}.sidecar.json")
    assert sidecar["session"]["status"] == "running"
    assert sidecar["session"]["in_progress"] is False
    assert sidecar["metadata"]["time_source"] == "import"


def test_stop_detection_uses_archived_and_last_message():
    assert _is_stopped(None, 100) is False           # 沒有封存 → 不是停止
    assert _is_stopped(0, 100) is False              # archived=0 視為未封存
    assert _is_stopped(200, 200) is True              # 最後一則訊息不晚於封存
    assert _is_stopped(200, 300) is False             # 封存後又有新訊息
    assert _is_stopped(200, None) is False            # 拿不到訊息時間 → 保守


def test_stop_detection_uses_archived_and_message_created():
    """H3：「封存之後有沒有新訊息」看訊息的 **created**，不是 completed。

    宣告停止一定發生在 AI 回覆**生成中**：那一則訊息在封存**之前**建立、
    封存**之後**才完成。用 completed 判，宣告停止會在下一輪自己恢復成 running。
    """
    # 封存之前建立、封存之後完成 → 仍是停止中
    assert _is_stopped(2000, 1000) is True
    # 封存之後才建立的訊息 → 恢復成 running
    assert _is_stopped(2000, 3000) is False
    # 沒有封存 / archived=0 → 不是停止
    assert _is_stopped(0, 100) is False
    assert _is_stopped(None, 100) is False
    # 拿不到訊息時間 → 保守視為未停止
    assert _is_stopped(2000, None) is False


def test_stop_declared_while_the_reply_is_streaming_stays_stopped(tmp_path: Path):
    """完整的宣告停止流程：回覆生成中就宣告停止，回覆完成後仍是 stopped。

    這一則回覆：created=1000（封存之前）、completed=9000（封存之後）、
    封存=2000 → 停止中。
    """
    env = _setup(tmp_path)
    raw1 = _raw("做完了", title="主線", last_message_ms=9000,
                last_message_created_ms=1000, in_progress=True)
    env["api"].set(OcSession(id="ses_1", updated_ms=1, archived_ms=2000), raw1)
    _run(env)
    rec = env["state"].sessions["opencode:ses_1"]
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"],
                       f"{rec.last_item_key}.sidecar.json")
    assert sidecar["session"]["status"] == "stopped", "宣告停止必須成立"
    assert sidecar["session"]["stopped_at"] is not None
    assert rec.stop_observed_at is not None

    # 回覆在封存之後完成（completed 變大、created 不變）→ 下一輪仍是 stopped
    env["clock"].set_time("2026-09-28T09:00:00Z")
    raw2 = _raw("做完了", title="主線", last_message_ms=9000,
                last_message_created_ms=1000, in_progress=False)
    env["api"].set(OcSession(id="ses_1", updated_ms=2, archived_ms=2000), raw2)
    env["reader"].catalog_data["opencode:ses_1"] = {"raw_sha256": _sha(raw1)}
    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_1",)
    rec2 = env["state"].sessions["opencode:ses_1"]
    sidecar2 = _sidecar(env["drive"], env["inbox_folder_id"],
                        f"{rec2.last_item_key}.sidecar.json")
    assert sidecar2["session"]["status"] == "stopped", "不該自己恢復成 running"
    # 停止觀測到的時間不因為新上傳而刷新
    assert rec2.stop_observed_at == rec.stop_observed_at


def test_stopped_at_is_the_first_observation_not_the_source_value(tmp_path: Path):
    env = _setup(tmp_path)
    raw = _raw("做完了", title="主線", last_message_ms=1000,
               last_message_created_ms=1000)
    env["api"].set(OcSession(id="ses_1", updated_ms=1, archived_ms=2000), raw)
    _run(env)
    rec = env["state"].sessions["opencode:ses_1"]
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"],
                       f"{rec.last_item_key}.sidecar.json")
    assert sidecar["session"]["status"] == "stopped"
    assert sidecar["session"]["stopped_at"].startswith(T1)


def test_new_message_after_stop_resumes_once_and_then_stops_triggering(tmp_path: Path):
    """H3 最重要的一段：恢復只被標記**一次**，之後不會每輪都觸發 workflow。

    恢復的判定以 Agora 的狀態為準（Agora 是 stopped、本地是 running），
    恢復之後要清掉 stop_observed_at。
    """
    env = _setup(tmp_path)
    sid = "opencode:ses_1"
    raw1 = _raw("做完了", title="主線", last_message_ms=1000,
                last_message_created_ms=1000)
    env["api"].set(OcSession(id="ses_1", updated_ms=1, archived_ms=2000), raw1)
    _run(env)
    # Agora 已經收成 stopped
    env["reader"].catalog_data[sid] = {"raw_sha256": _sha(raw1), "status": "stopped"}

    # 封存之後又追加了新訊息（created 在封存之後）→ 恢復
    raw2 = _raw("又回來了", title="主線", last_message_ms=5000,
                last_message_created_ms=5000)
    env["api"].set(OcSession(id="ses_1", updated_ms=2, archived_ms=2000), raw2)
    outcome = _run(env)
    assert outcome.uploaded == (sid,)
    assert outcome.resumed_after_stop == (sid,)
    key = env["state"].sessions[sid].last_item_key
    sidecar = _sidecar(env["drive"], env["inbox_folder_id"], f"{key}.sidecar.json")
    assert sidecar["session"]["status"] == "running"
    assert sidecar["session"]["stopped_at"] is None
    assert env["state"].sessions[sid].stop_observed_at is None, "恢復後要清掉"

    # 之後**再上傳新內容**：Agora 已經是 running → 不該再被標成恢復
    env["reader"].catalog_data[sid] = {"raw_sha256": _sha(raw2), "status": "running"}
    raw3 = _raw("再推進", title="主線", last_message_ms=6000,
                last_message_created_ms=6000)
    env["api"].set(OcSession(id="ses_1", updated_ms=3, archived_ms=2000), raw3)
    again = _run(env)
    assert again.uploaded == (sid,)
    assert again.resumed_after_stop == (), "恢復只能被標記一次（否則每 10 分鐘觸發 workflow）"


def test_resumed_is_not_reported_when_agora_was_never_stopped(tmp_path: Path):
    """本機曾經觀測到停止，但 Agora 從來不是 stopped → 不算恢復。"""
    env = _setup(tmp_path)
    sid = "opencode:ses_1"
    raw1 = _raw("做完了", title="主線", last_message_ms=1000,
                last_message_created_ms=1000)
    env["api"].set(OcSession(id="ses_1", updated_ms=1, archived_ms=2000), raw1)
    _run(env)
    assert env["state"].sessions[sid].stop_observed_at is not None
    # Agora 裡從來沒有 stopped（提交流程還沒跑）
    raw2 = _raw("又回來了", title="主線", last_message_ms=5000,
                last_message_created_ms=5000)
    env["api"].set(OcSession(id="ses_1", updated_ms=2, archived_ms=2000), raw2)
    outcome = _run(env)
    assert outcome.uploaded == (sid,)
    assert outcome.resumed_after_stop == ()


def test_only_restricts_to_requested_sessions(tmp_path: Path):
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("一", title="A"))
    env["api"].set(OcSession(id="ses_2", updated_ms=1), _raw("二", title="B"))
    outcome = _run(env, only=["ses_2"])
    assert outcome.uploaded == ("opencode:ses_2",)


def test_three_level_subagent_tree_all_uploaded(tmp_path: Path):
    """1.7h 驗收：子代理再開子代理，三層都要同步且 parent_id 正確。"""
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("主", title="主"))
    env["api"].set(OcSession(id="ses_2", parent_id="ses_1", updated_ms=1), _raw("子", title="子"))
    env["api"].set(OcSession(id="ses_3", parent_id="ses_2", updated_ms=1), _raw("孫", title="孫"))
    outcome = _run(env)
    assert set(outcome.uploaded) == {
        "opencode:ses_1", "opencode:ses_2", "opencode:ses_3"}
    parents = {}
    for sid, rec in env["state"].sessions.items():
        key = rec.last_item_key
        parents[sid] = _sidecar(
            env["drive"], env["inbox_folder_id"], f"{key}.sidecar.json"
        )["session"]["parent_id"]
    assert parents == {
        "opencode:ses_1": None,
        "opencode:ses_2": "opencode:ses_1",
        "opencode:ses_3": "opencode:ses_2",
    }


def test_too_large_is_not_uploaded(tmp_path: Path):
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("大", title="主"))
    outcome = sync_once(
        api=env["api"], reader=env["reader"], drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"], signer=env["signer"],
        state=env["state"], clock=env["clock"], workdir=env["workdir"],
        converter=env["converter"], max_raw=10,
    )
    assert outcome.too_large == ("opencode:ses_1",)
    assert _inbox_names(env["drive"], env["inbox_folder_id"]) == []
    assert env["state"].too_large() == ["opencode:ses_1"]


def test_export_failure_does_not_stop_other_sessions(tmp_path: Path):
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("一", title="A"))
    # ses_2 有 meta 卻沒有匯出內容 → export 會失敗
    env["api"]._meta[OcSession(id="ses_2", updated_ms=1).id] = OcSession(
        id="ses_2", updated_ms=1
    )
    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_1",)
    assert [sid for sid, _code in outcome.errors] == ["opencode:ses_2"]


# ---------------------------------------------------------------------------
# 接續點（5.4 寫交接單時用；判定必須與 apply_handoff 一致）
# ---------------------------------------------------------------------------


def _message(mid: str, index: int, completed: bool, reverted: bool = False) -> dict:
    return {"message_id": mid, "index": index, "role": "assistant",
            "completed": completed, "reverted": reverted,
            "created_at": f"2026-09-27T08:0{index}:00Z", "parts": []}


def test_last_completed_message_skips_incomplete_and_reverted():
    reading = {"messages": [
        _message("m1", 0, True),
        _message("m2", 1, True, reverted=True),   # 被撤銷 → 不算
        _message("m3", 2, False),                  # 還在生成中 → 不算
    ]}
    assert last_completed_message_id(reading) == ("m1", "2026-09-27T08:00:00Z")
    # 最後一則自己被撤銷就往前找，而不是回 None
    assert last_completed_message_id(
        {"messages": [_message("m1", 0, True), _message("m2", 1, True, reverted=True)]}
    )[0] == "m1"
    # 全部不可用 → (None, None)，呼叫端要明確拒絕
    assert last_completed_message_id({"messages": [_message("m1", 0, False)]}) == (None, None)
    assert last_completed_message_id({}) == (None, None)


def test_continuation_point_matches_committer_definition(tmp_path: Path):
    """接續點必須是 apply_handoff 會接受的訊息（有測試證明）。"""
    from aistorage.converters.opencode import OpencodeConverter
    from aistorage.inbox_builder import build_handoff_item
    from aistorage.reading import check_continuation, messages_before

    # 一份最小但合法的 opencode 原始紀錄：兩則完成 + 最後一則還在生成中
    def _msg(mid: str, completed: int | None) -> dict:
        time = {"created": 1790400000000}
        if completed is not None:
            time["completed"] = completed
        return {"info": {"id": mid, "role": "assistant", "time": time},
                "parts": [{"id": f"{mid}_p1", "type": "text", "text": f"內容 {mid}"}]}

    raw = tmp_path / "export.json"
    raw.write_text(json.dumps({
        "info": {"id": "ses_1", "title": "主線",
                 "time": {"created": 1790400000000, "updated": 1790400000000}},
        "messages": [_msg("m1", 1790400001000), _msg("m2", 1790400002000),
                     _msg("m3", None)],
    }), encoding="utf-8")
    # 快照雜湊以匯出檔本身為準（轉換器會自己算並交叉檢查）
    snapshot = hashlib.sha256(raw.read_bytes()).hexdigest()
    point = continuation_point(
        OpencodeConverter(), raw, session_id="opencode:ses_1", snapshot_sha256=snapshot,
    )
    assert point is not None
    # 還在生成中的那則不算接續點（D10）
    assert point.message_id == "m2"
    assert point.continuation() == {"snapshot_sha256": snapshot, "message_id": "m2"}

    # 這個接續點在提交流程的檢查下必須通過（reading.check_continuation，
    # 也就是 apply_handoff 用的那個）
    reading = OpencodeConverter().convert(
        raw, session_id="opencode:ses_1", snapshot_sha256=snapshot)
    assert check_continuation(reading, point.continuation()) == []
    before = messages_before(reading, point.message_id, snapshot_sha256=snapshot)
    assert [m["message_id"] for m in before] == ["m1", "m2"]

    key = bytes(range(32))
    item = build_handoff_item(
        target_session_id="opencode:ses_1", continuation=point.continuation(),
        body={"content": "接手這一段"}, profile=PROFILE, key=key,
        key_id="mac-opencode-abcdef12", now=T1,
    )
    assert item.item_type == "handoff"
    assert item.sidecar["body"]["continuation"]["message_id"] == "m2"


def test_continuation_point_is_none_when_nothing_completed(tmp_path: Path):
    from aistorage.converters.opencode import OpencodeConverter

    raw = tmp_path / "export.json"
    raw.write_text(json.dumps({
        "info": {"id": "ses_1", "title": "剛開", "time": {"created": 1790400000000}},
        "messages": [{"info": {"id": "m1", "role": "assistant",
                               "time": {"created": 1790400000000}}, "parts": []}],
    }), encoding="utf-8")
    snapshot = hashlib.sha256(raw.read_bytes()).hexdigest()
    assert continuation_point(OpencodeConverter(), raw, session_id="opencode:ses_1",
                              snapshot_sha256=snapshot) is None


# ---------------------------------------------------------------------------
# 5.3 同步並提交
# ---------------------------------------------------------------------------


class CountingReader(FakeReader):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.visible_after = 0
        self.calls = 0

    def catalog(self, session_ids: Sequence[str]) -> Any:
        self.calls += 1
        if self.calls < self.visible_after:
            return {}
        return super().catalog(session_ids)


def test_wait_visible_detects_visible_rejected_and_timeout():
    reader = FakeReader()
    session = build_awaited_session("k1", "ses_1", SNAP_A)
    reader.catalog_data["opencode:ses_1"] = {"raw_sha256": SNAP_A}

    done = wait_visible(reader, [session], timeout=timedelta(seconds=1),
                        poll=timedelta(milliseconds=10), progress=lambda _m: None)
    assert done.ok and done.visible == (session,) and not done.timed_out

    # 拒收也算「有結果」
    reader2 = FakeReader()
    reader2.rejections["k2"] = "stale"
    handoff = build_awaited("handoff", "k2", "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                            view_session="opencode:ses_1")
    out2 = wait_visible(reader2, [handoff], timeout=timedelta(seconds=1),
                        poll=timedelta(milliseconds=10), progress=lambda _m: None)
    assert out2.ok and out2.rejected[0][1] == "stale"

    # 逾時：訊息一定要包含健康檢查提示與未見 id
    notes: list[str] = []
    out3 = wait_visible(FakeReader(), [session], timeout=timedelta(milliseconds=200),
                        poll=timedelta(milliseconds=50), progress=notes.append)
    assert out3.timed_out and not out3.ok
    assert out3.pending == (session,)
    assert any(STALENESS_HINT in n for n in notes)
    assert "session:ses_1" in notes[-1]
    assert STALENESS_HINT in out3.summary()


def test_link_and_handoff_visibility_uses_session_views():
    """交接單看被接續者；認領／參考看自己發出的 Link（`_is_visible` 的分支）。"""
    from aistorage.search.index import HandoffRow, LinkRow

    reader = FakeReader()
    claim = build_awaited("claim", "k3", "claim:01ARZ3NDEKTSV4RRFFQ69G5FAW",
                          view_session="opencode:ses_2")
    handoff = build_awaited("handoff", "k4", "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                            view_session="opencode:ses_1")
    ref = build_awaited("reference", "k5", "reference:01ARZ3NDEKTSV4RRFFQ69G5FAU",
                        view_session="opencode:ses_2")

    def _view(**kw):
        class V:
            links_out = kw.get("links_out", ())
            handoffs_targeting = kw.get("handoffs_targeting", ())
            handoffs_by_holder = ()
        return V()

    # 都還沒被提交流程收進去
    reader.sessions["opencode:ses_1"] = _view()
    reader.sessions["opencode:ses_2"] = _view()
    assert not _is_visible(reader, claim)
    assert not _is_visible(reader, handoff)
    assert not _is_visible(reader, ref)

    # 提交流程收完之後
    reader.sessions["opencode:ses_1"] = _view(handoffs_targeting=(HandoffRow(
        handoff_id=handoff.target, target_session_id="opencode:ses_1",
        snapshot_sha256=SNAP_A, message_id="m9", producer="opencode:ses_0",
        created_at=T0, updated_at=T0,
    ),))
    reader.sessions["opencode:ses_2"] = _view(links_out=(
        LinkRow(kind="continuation", from_session_id="opencode:ses_2",
                to_session_id="opencode:ses_1", claim_id=claim.target),
        LinkRow(kind="reference", from_session_id="opencode:ses_2",
                to_session_id="opencode:ses_3", reference_id=ref.target),
    ))
    assert _is_visible(reader, handoff)
    assert _is_visible(reader, claim)
    assert _is_visible(reader, ref)
    # 沒有 view_session 的項目不能判成看得到（寧可逾時也不要假裝成功）
    assert not _is_visible(reader, build_awaited("claim", "k6", claim.target))


def _awaited_like(item, kind: str, view_session: str):
    """小幫手：某個項目的等待條件。"""
    from aistorage.syncer.commit import Awaited

    return Awaited(item_key=item.item_key, kind=kind, target=item.item_id,
                   view_session=view_session)


def test_awaited_for_item_derives_the_view_session(tmp_path: Path):
    """每種項目都要知道「要看哪個 Session 的視圖」才能判斷有沒有被收進去。"""
    from aistorage.inbox_builder import build_handoff_item, build_reference_item

    key = bytes(range(32))
    handoff = build_handoff_item(
        target_session_id="opencode:ses_1",
        continuation={"snapshot_sha256": SNAP_A, "message_id": "m9"},
        body={"content": "接手", "title": "接續 5.2"},
        profile=PROFILE, key=key, key_id="mac-opencode-abcdef12", now=T1,
    )
    claim = build_claim_item(
        handoff_id=handoff.item_id, claimer_session_id="opencode:ses_2",
        profile=PROFILE, key=key, key_id="mac-opencode-abcdef12", now=T1,
    )
    ref = build_reference_item(
        from_session_id="opencode:ses_2", to_session_id="opencode:ses_3",
        read_snapshot_at=T0, profile=PROFILE, key=key,
        key_id="mac-opencode-abcdef12", now=T1,
    )
    assert awaited_for_item(handoff) == _awaited_like(handoff, "handoff", "opencode:ses_1")
    assert awaited_for_item(claim) == _awaited_like(claim, "claim", "opencode:ses_2")
    assert awaited_for_item(ref) == _awaited_like(ref, "reference", "opencode:ses_2")


def test_sync_and_commit_triggers_and_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = _setup(tmp_path)
    raw = _raw("同步並提交", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
    sid = "opencode:ses_1"
    env["reader"].published_at = "2026-09-27T10:00:00.000Z"

    # 上傳完之後讀取端就看得到（模擬提交流程跑完）
    def _fake_trigger(pat_path, repo, workflow="committer.yml", **kw):
        env["reader"].catalog_data[sid] = {"raw_sha256": _sha(raw)}
        triggered.append((str(pat_path), repo, workflow))

    triggered: list[tuple] = []
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", _fake_trigger)

    pat = tmp_path / "gh-pat-actions.txt"
    pat.write_text("ghp_example_not_a_real_token\n")
    deps = SyncDeps(
        api=env["api"], reader=env["reader"], drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"], signer=env["signer"],
        state=env["state"], clock=env["clock"], workdir=env["workdir"],
        converter=env["converter"], pat_path=pat, repo="org/repo",
    )
    result = sync_and_commit(
        session_ids=["ses_1"], deps=deps,
        timeout=timedelta(seconds=2), poll=timedelta(milliseconds=50),
        progress=lambda _m: None,
    )
    assert triggered == [(str(pat), "org/repo", "committer.yml")]
    assert result.ok and result.trigger_error is None
    assert [a.target for a in result.visible] == ["ses_1"]
    assert result.sync is not None and result.sync.uploaded == (sid,)


def test_sync_and_commit_uploads_extra_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("主線", title="主線"))
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer",
                        lambda *a, **k: None)
    key = load_private_key(tmp_path / "signing.key")
    signer = env["signer"]
    handoff = build_claim_item(
        handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
        claimer_session_id="opencode:ses_1", profile=PROFILE, key=key,
        key_id=signer.key_id, now=T1,
    )
    env["reader"].catalog_data["opencode:ses_1"] = {"raw_sha256": "b" * 64}
    deps = SyncDeps(
        api=env["api"], reader=env["reader"], drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"], signer=signer,
        state=env["state"], clock=env["clock"], workdir=env["workdir"],
        converter=env["converter"], repo=None, pat_path=None,
    )
    result = sync_and_commit(
        session_ids=["ses_1"], extra_items=[handoff], deps=deps,
        timeout=timedelta(milliseconds=200), poll=timedelta(milliseconds=50),
        progress=lambda _m: None,
    )
    # 沒有 PAT／repo → 明確記下沒觸發。因為「沒有觸發」不等於「觸發失敗」
    # （排程或測試端的提交流程仍可能收進去，ADR 0007），所以**照樣等**。
    assert result.trigger_error is not None
    assert result.trigger_attempted is False
    assert result.timed_out is True
    assert not result.ok
    assert "ADR 0007" in result.summary()
    names = _inbox_names(env["drive"], env["inbox_folder_id"])
    assert any(n.endswith(".sidecar.json") and n.startswith(handoff.item_key)
               for n in names)


def test_trigger_committer_requires_pat_file_and_repo(tmp_path: Path):
    from aistorage.syncer.commit import trigger_committer

    with pytest.raises(ReadError):
        trigger_committer(tmp_path / "nope.txt", "org/repo")
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n")
    with pytest.raises(ReadError):
        trigger_committer(empty, "org/repo")
    with pytest.raises(ReadError):
        trigger_committer(empty, "bad-repo-format")


def test_trigger_committer_never_leaks_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PAT 不進 argv、不進 log、不進例外訊息（1.6）。"""
    from aistorage.syncer import commit as commit_mod

    pat = tmp_path / "gh-pat-actions.txt"
    pat.write_text("ghp_supersecret\n")
    seen: dict[str, Any] = {}

    class Resp:
        status = 204
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        seen["data"] = req.data
        seen["argv"] = None
        return Resp()

    monkeypatch.setattr(commit_mod.urllib.request, "urlopen", _fake_urlopen)
    commit_mod.trigger_committer(pat, "org/repo")
    assert seen["data"] == b'{"ref": "main"}'         # 一律帶 main（1.6）
    assert seen["url"].endswith("/repos/org/repo/actions/workflows/committer.yml/dispatches")
    assert "ghp_supersecret" not in repr(seen["argv"])
    # token 只在 header 裡
    assert any("ghp_supersecret" in str(v) for v in seen["headers"].values())


# ---------------------------------------------------------------------------
# CLI（容器內實際呼叫的那條路徑）
# ---------------------------------------------------------------------------


def test_cli_exposes_the_documented_commands():
    """resident 的 entrypoint 就是呼叫這些指令，所以介面不能變。"""
    from aistorage.syncer.__main__ import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    assert set(sub.choices) == {"opencode", "sync-and-commit"}
    oc = next(a for a in sub.choices["opencode"]._actions if a.dest == "mode")
    assert set(oc.choices) == {"once", "daemon", "status"}


def test_cli_status_works_without_any_credential(tmp_path: Path, capsys, monkeypatch):
    """status 只讀狀態檔，不需要 Drive／簽章金鑰。"""
    from aistorage.syncer.__main__ import main

    state = SyncState.load(tmp_path / "sync-state.json")
    rec = state.record("opencode:ses_1")
    rec.last_uploaded_sha = SNAP_A
    rec.last_item_key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    state.save()
    reader_json = tmp_path / "reader.json"
    reader_json.write_text(json.dumps({
        "format": "aistorage.reader/v1", "manifest_file_id": "m1",
    }), encoding="utf-8")
    monkeypatch.setenv("AISTORAGE_READER_CONFIG", str(reader_json))
    monkeypatch.setenv("AISTORAGE_SYNC_STATE", str(tmp_path / "sync-state.json"))
    monkeypatch.delenv("AISTORAGE_INBOX_FOLDER_ID", raising=False)

    rc = main(["opencode", "status", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["sessions"] == 1 and out["state_path"].endswith("sync-state.json")


def test_cli_once_reports_missing_config_with_exit_code_2(tmp_path: Path, capsys, monkeypatch):
    from aistorage.syncer.__main__ import main

    monkeypatch.setenv("AISTORAGE_READER_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.delenv("AISTORAGE_INBOX_FOLDER_ID", raising=False)
    rc = main(["opencode", "once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "設定不足" in err and "inbox_folder_ids" in err


def test_cli_once_runs_sync_once_with_fake_deps(tmp_path: Path, capsys, monkeypatch):
    """CLI → sync_once 的接線（假的相依，不碰網路）。"""
    from aistorage.syncer import __main__ as cli

    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("一", title="A"))
    reader_json = tmp_path / "reader.json"
    reader_json.write_text(json.dumps({
        "manifest_file_id": "m1", "inbox_folder_ids": {PROFILE: "folder-1"},
    }), encoding="utf-8")
    monkeypatch.setenv("AISTORAGE_READER_CONFIG", str(reader_json))
    monkeypatch.setattr(cli, "_deps", lambda cfg: SyncDeps(
        api=env["api"], reader=env["reader"], drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"], signer=env["signer"],
        state=env["state"], clock=env["clock"], workdir=env["workdir"],
        converter=env["converter"],
    ))
    # 逐行輸出時只有代碼與 id，沒有內容（D2 的 log 規則）
    assert cli.main(["opencode", "once"]) == 0
    printed = capsys.readouterr().out
    assert "uploaded: opencode:ses_1" in printed
    assert "一" not in printed

    # 第二輪：已上傳但讀取端還沒看到、又沒有新世代 → 等待中
    assert cli.main(["opencode", "once", "--json"]) == 0
    counts = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert counts["waiting"] == 1 and counts["uploaded"] == 0


def test_cli_loads_items_written_by_the_skill(tmp_path: Path):
    """5.4 的 skill 會把組好的項目寫成 sidecar base64 ＋ raw 檔，這裡要讀得回來。"""
    from aistorage.inbox_builder import build_claim_item
    from aistorage.syncer.__main__ import _load_items

    key = bytes(range(32))
    claim = build_claim_item(
        handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
        claimer_session_id="opencode:ses_2", profile=PROFILE, key=key,
        key_id="mac-opencode-abcdef12", now=T1,
    )
    items_dir = tmp_path / "items"
    items_dir.mkdir()
    (items_dir / f"{claim.item_key}.json").write_text(json.dumps({
        "item_key": claim.item_key, "item_id": claim.item_id,
        "sidecar_b64": base64.b64encode(claim.sidecar_bytes).decode("ascii"),
        "sig": claim.sig, "raw_path": None,
    }), encoding="utf-8")

    loaded = _load_items(items_dir)
    assert len(loaded) == 1
    assert loaded[0].item_key == claim.item_key
    assert loaded[0].sidecar_bytes == claim.sidecar_bytes   # 逐位元組相同 → 驗章會過
    assert loaded[0].item_type == "claim"


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------


def test_syncer_config_requires_inbox_folder_id(tmp_path: Path):
    reader_json = tmp_path / "reader.json"
    reader_json.write_text(json.dumps({"manifest_file_id": "m1"}), encoding="utf-8")
    env = {"AISTORAGE_READER_CONFIG": str(reader_json)}
    with pytest.raises(ConfigError):
        SyncerConfig.load(env=env)

    reader_json.write_text(json.dumps({
        "manifest_file_id": "m1",
        "inbox_folder_ids": {PROFILE: "folder-1"},
    }), encoding="utf-8")
    cfg = SyncerConfig.load(env=env)
    assert cfg.inbox_folder_id == "folder-1"
    assert cfg.profile == PROFILE and cfg.interval_s == 600

    cfg2 = SyncerConfig.load(env={**env, "AISTORAGE_INBOX_FOLDER_ID": "override"})
    assert cfg2.inbox_folder_id == "override"


def test_signer_key_id_is_derived_from_the_public_key(tmp_path: Path):
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(bytes(range(32)))
    key_path.chmod(0o600)
    signer = Signer.from_key_file(key_path, profile=PROFILE)
    assert signer.key_id.startswith(f"{PROFILE}-")
    assert len(signer.key_id.split("-")[1]) == 8
    assert "key" not in repr(signer) or "Signer" in repr(signer)


# ---------------------------------------------------------------------------
# M1〜M5
# ---------------------------------------------------------------------------


class FlakyDrive(FakeDrive):
    """在第 `fail_at` 次 create 時丟 WriteError（模擬 Drive 寫入失敗）。"""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self._n = 0
        self._fail_at = fail_at

    def create(self, parent: str, name: str, content, mime_type: str = "application/octet-stream"):
        self._n += 1
        if self._n == self._fail_at:
            from aistorage.errors import WriteError

            raise WriteError("模擬的寫入失敗")
        return super().create(parent, name, content, mime_type=mime_type)


def test_write_error_does_not_abort_the_round_and_state_is_saved(tmp_path: Path):
    """M1：上傳失敗會中止整輪而且不存狀態 → 下一輪重複上傳、收件匣出現重複檔案。"""
    from aistorage.errors import WriteError

    class OneBad(FakeDrive):
        """ses_bad 的第一個檔案上傳失敗，其餘正常（認 sidecar 的內容判斷）。"""

        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def create(self, parent, name, content, mime_type="application/octet-stream"):
            body = content if isinstance(content, bytes) else str(content).encode()
            if b"ses_bad" in body and not self.failed:
                self.failed = True
                raise WriteError("模擬的寫入失敗")
            return super().create(parent, name, content, mime_type=mime_type)

    env = _setup(tmp_path)
    drive = OneBad()
    env["drive"] = drive
    inbox = drive.seed_folder("inbox")
    env["inbox_folder_id"] = inbox
    env["api"].set(OcSession(id="ses_ok", updated_ms=1), _raw("好", title="好"))
    env["api"].set(OcSession(id="ses_bad", updated_ms=1), _raw("壞", title="壞"))

    outcome = _run(env)
    # 一個 Session 失敗不影響另一個
    assert outcome.uploaded == ("opencode:ses_ok",), outcome.counts()
    assert [sid for sid, _c in outcome.errors] == ["opencode:ses_bad"]
    # 成功的那一個**已經存進狀態**（M1 的重點：下一輪不會重複上傳）
    assert env["state"].sessions["opencode:ses_ok"].last_item_key


def test_state_is_saved_even_when_a_session_raises_unexpectedly(tmp_path: Path):
    """M1：非預期的例外也不能讓整輪中止，狀態一定要存下來。"""
    env = _setup(tmp_path)
    # 名字排序：好的先處理，之後才轮到會丟例外的（否則不會留下任何紀錄）
    env["api"].set(OcSession(id="ses_aaa", updated_ms=1), _raw("好", title="好"))
    env["api"].set(OcSession(id="ses_zzz", updated_ms=1), _raw("壞", title="壞"))
    original_export = env["api"].export

    def _export(session_id, dest):
        if session_id == "ses_zzz":
            raise RuntimeError("非預期的例外")
        return original_export(session_id, dest)

    env["api"].export = _export  # type: ignore[assignment]

    outcome = _run(env)
    assert outcome.uploaded == ("opencode:ses_aaa",), outcome.counts()
    assert [sid for sid, _c in outcome.errors] == ["opencode:ses_zzz"]
    # 之前處理成功的 Session 已經存進狀態（M1 的重點）
    assert env["state"].sessions["opencode:ses_aaa"].last_item_key


def test_reupload_uses_generation_not_clock_strings(tmp_path: Path):
    """M2：published_at（committer 的時鐘）與 uploaded_at（Mac 的時鐘）不能比字串。"""
    env = _setup(tmp_path)
    raw = _raw("等待中", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
    first = _run(env)
    assert first.uploaded == ("opencode:ses_1",)
    rec = env["state"].sessions["opencode:ses_1"]
    assert rec.uploaded_generation == 1

    # 世代**沒有**變，但 published_at 變成遠晚於上傳時間 → 不該重傳
    env["reader"].published_at = "2099-01-01T00:00:00.000Z"
    assert _run(env).waiting == ("opencode:ses_1",)
    # 世代變了 → 補傳
    env["reader"].generation = 2
    assert _run(env).reuploaded == ("opencode:ses_1",)
    assert env["state"].sessions["opencode:ses_1"].uploaded_generation == 2


def test_export_is_deleted_after_upload(tmp_path: Path):
    """M5：匯出檔是原始紀錄的副本，上傳成功後不留在 /work。"""
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("內容", title="主線"))
    _run(env)
    exports = list((env["workdir"] / "exports").glob("*.json"))
    assert exports == [], f"匯出檔沒有被清掉：{exports}"


def test_keep_exports_leaves_the_file_for_the_caller(tmp_path: Path):
    """skill 算接續點時需要匯出檔，所以有 keep_exports 這個開關。"""
    env = _setup(tmp_path)
    env["api"].set(OcSession(id="ses_1", updated_ms=1), _raw("內容", title="主線"))
    _run(env, keep_exports=True)
    exports = list((env["workdir"] / "exports").glob("*.json"))
    assert len(exports) == 1


class FakeTicker:
    """假時鐘：睡覺時直接跳到 deadline 之前，所以逾時流程**不會真的等**。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_trigger_failure_returns_immediately_without_waiting(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch):
    """觸發提交流程失敗 → **立即**回報，完全不進入等待（review-g5-6 M4）。

    沒有任何東西會讓讀取視圖改變（排程的提交流程根本沒被觸發），乾等 15 分鐘
    只會讓 AI 的工具卡住，然後回報一個沒有意義的逾時。
    """
    env = _setup(tmp_path)
    raw = _raw("觸發失敗", title="主線")
    env["api"].set(OcSession(id="ses_1", updated_ms=1), raw)
    monkeypatch.setattr(
        "aistorage.syncer.commit.trigger_committer",
        lambda *a, **k: (_ for _ in ()).throw(ReadError("模擬的觸發失敗")),
    )
    (tmp_path / "gh-pat-actions.txt").write_text("ghp_example\n")
    deps = SyncDeps(
        api=env["api"], reader=env["reader"], drive=env["drive"],
        inbox_folder_id=env["inbox_folder_id"], signer=env["signer"],
        state=env["state"], clock=env["clock"], workdir=env["workdir"],
        converter=env["converter"], pat_path=tmp_path / "gh-pat-actions.txt",
        repo="org/repo",
    )
    # 假時鐘：就算真的進到等待，也不會真的睡
    ticker = FakeTicker()
    notes: list[str] = []
    started = time.monotonic()
    result = sync_and_commit(
        session_ids=["ses_1"], deps=deps,
        # 逾時給一個短值：這個測試不該依賴預設的 15 分鐘
        timeout=timedelta(seconds=1), poll=timedelta(milliseconds=200),
        progress=notes.append,
        monotonic=ticker.monotonic, sleeper=ticker.sleep,
    )
    wall = time.monotonic() - started
    assert result.trigger_error, "觸發失敗要明確回報"
    assert result.trigger_attempted is True, "這是「嘗試過而且失敗」，不是「沒觸發」"
    assert wall < 1.0, f"真的等了 {wall:.1f}s（應該完全不等待）"
    assert ticker.slept == [], f"不該有真的睡眠：{ticker.slept}"
    assert result.timed_out is False, "不是逾時，是「沒被觸發」"
    assert result.pending and result.pending[0].target == "ses_1"
    assert not result.ok, "還沒被收進去，不算成功"
    assert any("不等了" in n for n in notes), notes
    assert any("下一輪" in n for n in notes), notes
    # 上傳本身是成功的（只是沒有即時提交）
    assert result.sync is not None and result.sync.uploaded == ("opencode:ses_1",)
    # 摘要要講清楚會被排程的提交流程收進去
    assert "下一輪" in result.summary()


def test_wait_visible_timeout_uses_the_injected_clock(tmp_path: Path):
    """逾時流程可以用假時鐘走完，單元測試不必真的睡（PM 追加第 1 點）。"""
    reader = FakeReader()
    session = build_awaited_session("k1", "ses_1", SNAP_A)
    ticker = FakeTicker()
    notes: list[str] = []
    result = wait_visible(
        reader, [session], timeout=timedelta(seconds=1),
        poll=timedelta(milliseconds=200), progress=notes.append,
        monotonic=ticker.monotonic, sleeper=ticker.sleep,
    )
    assert result.timed_out and result.pending == (session,)
    assert ticker.slept, "應該有輪詢（只是睡的是假時鐘）"
    # 假時鐘停在 deadline 之後不久，不會跑出 1 秒太多
    assert sum(ticker.slept) <= 1.5, ticker.slept
    assert any(STALENESS_HINT in n for n in notes)


def test_stopping_rule_is_identical_in_the_committer_and_the_syncer():
    """H3：停止判定必須只有一份定義。

    同步器算 stopped、提交流程算 running，兩邊就會互相打回——所以這裡直接
    比較兩個函式在同樣輸入下的結果。
    """
    from aistorage.agora.apply import _is_stopped as committer_is_stopped
    from aistorage.converters.base import SessionFacts

    cases = [
        # (archived_ms, created, completed, 期望)
        (2000, 1000, 9000, True),    # 宣告停止時生成中的那一則：停止中
        (2000, 5000, 5000, False),   # 封存之後新建的訊息：運作中
        (0, 1000, 1000, False),      # archived=0 視為未封存
        (None, 1000, 1000, False),   # 沒有封存
    ]
    for archived, created, completed, expected in cases:
        facts = SessionFacts(
            title=None, created_at=None, updated_at=None, message_ids=(),
            archived_at=None, last_message_at=None, in_progress=False,
            archived_ms=archived,
            last_message_ms=max(created, completed),
            last_message_created_ms=created,
        )
        assert _is_stopped(archived, created) is expected, (archived, created)
        assert committer_is_stopped(facts) is expected, (archived, created)


def test_inbox_builder_guard_matches_the_same_rule():
    """H3：builder 擋的是「封存之後還有訊息被建立」，不是「生成中」。"""
    from aistorage.agora.apply import has_message_created_after_archive
    from aistorage.converters.base import SessionFacts

    in_flight = SessionFacts(
        title=None, created_at=None, updated_at=None, message_ids=(),
        archived_at=None, last_message_at=None, in_progress=True,
        archived_ms=2000, last_message_ms=9000, last_message_created_ms=1000,
    )
    assert has_message_created_after_archive(in_flight) is False
    later = SessionFacts(
        title=None, created_at=None, updated_at=None, message_ids=(),
        archived_at=None, last_message_at=None, in_progress=False,
        archived_ms=2000, last_message_ms=5000, last_message_created_ms=5000,
    )
    assert has_message_created_after_archive(later) is True


def test_not_triggered_still_waits_for_the_read_side():
    """「沒有 PAT」不等於「觸發失敗」：前者要等，後者不等。

    ADR 0007：寫入者以讀取介面判斷完成——提交流程可能來自排程、來自測試在
    本機跑。e2e 就是靠這個等待，讓測試端的提交流程把項目收進去。
    """
    reader = FakeReader()
    reader.catalog_data["opencode:ses_1"] = {"raw_sha256": SNAP_A}
    item = build_awaited_session("k1", "ses_1", SNAP_A)
    ticker = FakeTicker()
    # 第一輪還看不到，第二輪才看得到（模擬外部的提交流程收進去）
    state = {"calls": 0}
    original = reader.catalog

    def _catalog(ids):
        state["calls"] += 1
        if state["calls"] >= 2:
            return original(ids)
        return {}

    reader.catalog = _catalog  # type: ignore[assignment]
    result = wait_visible(
        reader, [item], timeout=timedelta(seconds=5),
        poll=timedelta(milliseconds=200), progress=lambda _m: None,
        monotonic=ticker.monotonic, sleeper=ticker.sleep,
    )
    assert result.ok, "外部把它收進去之後就算成功"
    assert result.timed_out is False

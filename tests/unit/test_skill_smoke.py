"""住民工具（tasks 5.4）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

用假的 opencode ＋ FakeDrive ＋ 假讀取端跑完工具的分支：主 Session 限定的
第二層、接續點、分裂／交出末端／參考、拒收就停、讀取附上新鮮度。
**這裡沒有認領**：接手新 session 走 `agora checkout`（`aistorage.agora_cli`，
由 `tests/unit/test_agora_cli_smoke.py` 覆蓋），ADR 0010。
範例資料一律自編。
"""

from __future__ import annotations

from datetime import timedelta
import tempfile
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

import hashlib

from aistorage.clock import FixedClock
from aistorage.converters import get_converter
from aistorage.drive.fake import FakeDrive
from aistorage.syncer.commit import SyncDeps
from aistorage.syncer.config import ConfigError
from aistorage.syncer.core import Signer, sync_once
from aistorage.syncer.opencode_api import OcSession
from aistorage.syncer.state import SyncState
from aistorage.skill import tools
from aistorage.skill.tools import (
    MainSessionRequired,
    RejectedItems,
    SkillDeps,
    SkillError,
    handoff_end,
    list_handoffs,
    read,
    reference,
    split,
    stop,
    whoami,
)

T0 = "2026-09-27T08:00:00Z"
T1 = "2026-09-27T09:00:00Z"
PROFILE = "mac-opencode"
SNAP = "b" * 64
#: 測試用的短逾時（預設 15 分鐘會讓失敗的分支把測試掛住）
SHORT = timedelta(milliseconds=200)


# ---------------------------------------------------------------------------
# 假的環境
# ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self, sessions: dict[str, OcSession] | None = None) -> None:
        self._meta = dict(sessions or {})
        self._exports: dict[str, str] = {}
        self.archived: list[tuple[str, int]] = []

    def set(self, oc: OcSession, raw: str) -> None:
        self._meta[oc.id] = oc
        self._exports[oc.id] = raw

    def list_sessions(self) -> list[OcSession]:
        return sorted(self._meta.values(), key=lambda s: s.id)

    def export(self, session_id: str, dest: Path) -> Path:
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self._exports[session_id], encoding="utf-8")
        return out

    def archive(self, session_id: str, at_ms: int) -> None:
        self.archived.append((session_id, at_ms))


class FakeFreshness:
    """欄位名稱要和真的 `reader.Freshness` 一致（M3）。"""

    def __init__(self, snapshot_at: str = T0, ok: bool = True,
                 warning: str | None = None) -> None:
        self.snapshot_at = snapshot_at
        self.generation = 2
        self.published_at = T0
        self.satisfied = ok
        self.warning = warning
        self.stopped_ok = True


class FakeResult:
    def __init__(self, value: Any, freshness: Any) -> None:
        self.value = value
        self.freshness = freshness


class FakeReader:
    """假讀取端：find_sessions / get_session / get_continuation /
    list_open_handoffs。"""

    def __init__(self) -> None:
        self.catalog_data: dict[str, dict] = {}
        self.rejections: dict[str, str] = {}
        self.handoff_rows: list[Any] = []
        self.continuations: dict[str, Any] = {}
        self.warnings: list[list[str]] = []
        self.published_at = "2026-09-27T10:00:00.000Z"
        self.generation = 2
        # 提交流程「收進去」的樣子（由 _run_committer 填）
        self.handoff_ids: dict[str, set] = {}
        self.link_kind: str | None = None
        self.link_id: str | None = None
        self.link_body: dict = {}

    def catalog(self, session_ids: Sequence[str]) -> Any:
        return {s: self.catalog_data[s] for s in session_ids if s in self.catalog_data}

    def get_rejection(self, item_key: str) -> Any:
        code = self.rejections.get(item_key)
        return None if code is None else {"code": code}

    def manifest(self) -> dict:
        return {"generation": self.generation, "published_at": self.published_at}

    # -- skill 用到的三個 --
    def get_session(self, session_id: str, *, max_lag=None) -> Any:
        """反映 `_run_committer` 記錄的交接單與 Link（skill 端只讀這裡）。

        形狀對齊真的 `AgoraReader.get_session`：`Result[SessionView]`。
        """
        targeting = tuple(
            _Row(handoff_id=hid) for hid in sorted(self.handoff_ids.get(session_id, ()))
        )
        links = ()
        body = self.link_body
        owner = body.get("claimer_session_id") or body.get("from_session_id")
        if self.link_id and owner == session_id:
            links = (_Row(
                kind="continuation" if self.link_kind == "claim" else "reference",
                from_session_id=owner,
                to_session_id=body.get("target_session_id")
                or body.get("handoff_id"),
                claim_id=self.link_id if self.link_kind == "claim" else None,
                reference_id=self.link_id if self.link_kind == "reference" else None,
            ),)
        view = _Row(
            session=_Row(session_id=session_id, snapshot_at=T0, status="running",
                         title="主線"),
            links_out=links, links_in=(),
            handoffs_targeting=targeting, handoffs_by_holder=(),
        )
        return FakeResult(view, FakeFreshness())

    def find_sessions(self, query: Any, *, max_lag=None) -> Any:
        assert query.text, "查詢文字要帶進去（不從模型的可信度假設）"
        return FakeResult(
            [_Row(hit=_Row(session_id="opencode:ses_1", title="主線",
                           status="running", snapshot_at=T0),
                  freshness=FakeFreshness())],
            FakeFreshness(),
        )

    def get_continuation(self, handoff_id: str) -> Any:
        return FakeResult(self.continuations[handoff_id],
                          FakeFreshness())

    def list_open_handoffs(self, *, case_id: str | None = None) -> Any:
        rows = self.handoff_rows
        if case_id:
            rows = [r for r in rows if r.case_id == case_id]
        return FakeResult(rows, FakeFreshness())


def _raw(text: str = "內容", *, n_completed: int = 2, last_incomplete: bool = True) -> str:
    """一份最小但合法的 opencode 匯出（用真的 opencode 轉換器驗證過）。"""
    messages = []
    for i in range(n_completed):
        messages.append({
            "info": {"id": f"m{i}", "role": "assistant",
                     "time": {"created": 1790400000000 + i * 1000,
                              "completed": 1790400001000 + i * 1000}},
            "parts": [{"id": f"m{i}_p", "type": "text", "text": f"{text} {i}"}],
        })
    if last_incomplete:
        messages.append({
            "info": {"id": f"m{n_completed}", "role": "assistant",
                     "time": {"created": 1790400009000}},
            "parts": [{"id": f"m{n_completed}_p", "type": "text", "text": "生成中"}],
        })
    return json.dumps({
        "info": {"id": "ses_1", "title": "主線",
                 "time": {"created": 1790400000000, "updated": 1790400009000}},
        "messages": messages,
    }, ensure_ascii=False)


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _sd(tmp_path: Path | None, *, reader: FakeReader | None = None,
        sessions: dict[str, OcSession] | None = None) -> tuple[SkillDeps, dict]:
    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    root = tmp_path or Path(tempfile.mkdtemp())
    key_path = root / "signing.key"
    key_path.write_bytes(bytes(range(32)))
    key_path.chmod(0o600)
    signer = Signer.from_key_file(key_path, profile=PROFILE)
    state = SyncState.load(root / "state.json")
    workdir = root / "work"
    api = FakeApi(sessions)
    reader = reader or FakeReader()
    # 有 pat_path ＋ repo，`sync_and_commit` 才會真的去觸發（測試再把它換成假的）
    pat = root / "gh-pat-actions.txt"
    pat.write_text("ghp_example_not_a_real_token\n")
    pat.chmod(0o600)
    deps = SyncDeps(
        api=api, reader=reader, drive=drive, inbox_folder_id=inbox,
        signer=signer, state=state, clock=FixedClock(T1), workdir=workdir,
        converter=get_converter("opencode"), repo="org/repo", pat_path=pat,
    )
    return SkillDeps(deps=deps, state=state), {
        "drive": drive, "inbox": inbox, "api": api, "reader": reader, "workdir": workdir,
    }


def _inbox_files(drive: FakeDrive, inbox: str) -> list[str]:
    return sorted(f.name for f in drive.list_children(inbox))


def _sidecars(env: dict) -> list[dict]:
    """收件匣裡所有 sidecar 的內容。"""
    return [
        json.loads(env["drive"].download_bytes(f.id, max_bytes=1 << 20))
        for f in env["drive"].list_children(env["inbox"])
        if f.name.endswith(".sidecar.json")
    ]


def _uploaded_kinds(env: dict) -> list[str]:
    """收件匣裡所有 sidecar 的 metadata.type。"""
    out = []
    for f in env["drive"].list_children(env["inbox"]):
        if f.name.endswith(".sidecar.json"):
            out.append(json.loads(
                env["drive"].download_bytes(f.id, max_bytes=1 << 20)
            )["metadata"]["type"])
    return sorted(out)


def _run_committer(monkeypatch: pytest.MonkeyPatch, env: dict) -> list[str]:
    """假裝提交流程跑了一輪：把收件匣裡的項目變成讀取端看得到的。

    回傳被收進去的 item 種類清單。
    """
    drive, inbox, reader = env["drive"], env["inbox"], env["reader"]
    seen: list[str] = []

    def _trigger(*_a, **_k):
        for f in drive.list_children(inbox):
            if not f.name.endswith(".sidecar.json"):
                continue
            data = json.loads(drive.download_bytes(f.id, max_bytes=1 << 20))
            meta = data["metadata"]
            kind = meta["type"]
            seen.append(kind)
            if kind == "session":
                # session 的 source 在 sidecar 的 "session" 區塊（不是 metadata）
                ses = data["session"]
                sid = f'{ses["source"]}:{ses["source_session_id"]}'
                reader.catalog_data[sid] = {
                    "raw_sha256": data["raw"]["sha256"],
                    "snapshot_at": ses["snapshot_at"],
                }
            elif kind == "handoff":
                # 多張交接單都要收進去：記成一個集合，get_session 一次回傳
                reader.handoff_ids.setdefault(
                    data["body"]["target_session_id"], set()).add(meta["id"])
            else:
                reader.link_kind = kind
                reader.link_id = meta["id"]
                reader.link_body = data["body"]

    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", _trigger)
    return seen

def _main_session(sd: SkillDeps, env: dict) -> None:
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1), _raw())


# ---------------------------------------------------------------------------
# whoami 與主 Session 限定（第二層）
# ---------------------------------------------------------------------------


def test_whoami_reports_identity():
    sd, env = _sd(None, sessions={"ses_1": OcSession(id="ses_1", title="主線", updated_ms=1)})
    out = whoami(sd.api, "ses_1")
    assert out == {"session_id": "opencode:ses_1", "parent_id": None, "is_main": True}
    # plugin 傳完整 id 也行
    assert whoami(sd.api, "opencode:ses_1")["is_main"] is True
    # 子 Session
    env["api"]._meta["ses_2"] = OcSession(id="ses_2", parent_id="ses_1", updated_ms=1)
    assert whoami(sd.api, "ses_2") == {
        "session_id": "opencode:ses_2", "parent_id": "opencode:ses_1", "is_main": False,
    }


def test_unknown_session_is_rejected():
    sd, _ = _sd(None)
    with pytest.raises(SkillError) as e:
        whoami(sd.api, "ses_999")
    assert "找不到" in str(e.value)
    with pytest.raises(SkillError):
        whoami(sd.api, "")


def test_stop_requires_main_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """plugin 擋一次之外，Python 端再擋一次（宣告停止沒有提交流程那層）。"""
    sd, env = _sd(tmp_path, sessions={"ses_2": OcSession(id="ses_2", parent_id="ses_1",
                                                       updated_ms=1)})
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)
    with pytest.raises(MainSessionRequired):
        stop(sd, "ses_2")
    # 子 Session 的父代存在時，也不得因為 plugin 傳錯就當主 Session
    with pytest.raises(MainSessionRequired):
        stop(sd, "opencode:ses_2")


# ---------------------------------------------------------------------------
# 分裂／交出末端
# ---------------------------------------------------------------------------


def test_split_writes_one_handoff_per_part_and_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sd, env = _sd(tmp_path)
    _main_session(sd, env)

    kinds = _run_committer(monkeypatch, env)

    out = split(sd, "ses_1", [
        {"title": "做甲", "summary": "甲的工作", "next_steps": "先做 A"},
        {"title": "做乙", "summary": "乙的工作"},
    ], timeout=SHORT)
    assert len(out["handoff_ids"]) == 2
    # 每張交接單的接續點都是同一個（同一份快照、同一個接續點）
    # 1 個 session ＋ 2 張交接單（收件匣裡不會有重複的 session）
    assert _uploaded_kinds(env) == ["handoff", "handoff", "session"]
    # 內容不同
    titles, points = [], set()
    for data in _sidecars(env):
        if data["metadata"]["type"] != "handoff":
            continue
        titles.append(data["body"]["title"])
        points.add(json.dumps(data["body"]["continuation"], sort_keys=True))
    assert sorted(titles) == ["做乙", "做甲"]
    # 接續點用的是同一份快照、同一個訊息（D10）
    assert len(points) == 1


def test_split_refuses_when_there_is_no_continuation_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """沒有已完成的訊息就不寫交接單（不要猜接續點）。"""
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)
    sd, env = _sd(tmp_path)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1),
                   _raw(n_completed=0, last_incomplete=True))
    with pytest.raises(SkillError) as e:
        split(sd, "ses_1", [{"title": "做甲", "summary": "甲"}])
    assert "接續點" in str(e.value)


def test_normalize_parts_absorbs_a_sloppy_model():
    """9.1 e2e 實測：space-bunny-free 把清單包成 `{"item": [...]}`。

    那是模型的形狀問題，不該讓它整個失敗並回「每一份工作都要有 title」。
    """
    from aistorage.skill import normalize_parts

    assert normalize_parts({"item": [{"title": "甲"}, {"title": "乙"}]}) == [
        {"title": "甲"}, {"title": "乙"}]
    assert normalize_parts({"items": [{"title": "甲"}]}) == [{"title": "甲"}]
    # 其餘三種 9.1 e2e 實測看過的包法：包兩層、清單裡塞 JSON 字串、整個當字串
    assert normalize_parts([{"item": [{"title": "甲"}]}]) == [{"title": "甲"}]
    assert normalize_parts([json.dumps({"title": "甲"})]) == [{"title": "甲"}]
    assert normalize_parts(json.dumps([{"title": "甲"}])) == [{"title": "甲"}]
    assert normalize_parts('{"parts": [{"title": "甲"}]}') == [{"title": "甲"}]
    assert normalize_parts({"title": "甲", "summary": "s"}) == [
        {"title": "甲", "summary": "s"}]
    assert normalize_parts([{"title": "甲"}]) == [{"title": "甲"}]
    for bad in ("nope", 5, {"a": 1}, [1, 2], [], [{}]):
        with pytest.raises(SkillError):
            normalize_parts(bad)


def test_split_accepts_a_wrapped_parts_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """整條路徑：模型給 `{"item": [...]}` 也要寫出兩張交接單。"""
    _run_committer(monkeypatch, _sd(tmp_path)[1])
    sd, env = _sd(tmp_path)
    _run_committer(monkeypatch, env)
    _main_session(sd, env)
    out = split(sd, "ses_1", {"item": [
        {"title": "做甲", "summary": "甲的工作"},
        {"title": "做乙", "summary": "乙的工作"},
    ]}, timeout=SHORT)
    assert len(out["handoff_ids"]) == 2
    assert _uploaded_kinds(env).count("handoff") == 2


def test_split_requires_title_for_each_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    with pytest.raises(SkillError) as e:
        split(sd, "ses_1", [{"summary": "沒有 title"}])
    # 錯誤訊息要讓模型知道正確形狀（9.1 實測：模糊的訊息讓模型重試四次）
    assert "title" in str(e.value) and "summary" in str(e.value)
    with pytest.raises(SkillError):
        split(sd, "ses_1", ["不是字典"])


def test_handoff_end_writes_exactly_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    _run_committer(monkeypatch, env)
    out = handoff_end(sd, "ses_1", "做完了", next_steps="下一步是 X", timeout=SHORT)
    assert len(out["handoff_ids"]) == 1


# ---------------------------------------------------------------------------
# 參考（PM 決定 4：只上傳不提交）
# ---------------------------------------------------------------------------


def test_reference_uploads_only_and_does_not_trigger(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch):
    triggered: list[str] = []
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer",
                        lambda *a, **k: triggered.append("x"))
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    out = reference(sd, "ses_1", "opencode:ses_9", read_snapshot_at=T0)
    # reference 完全不經過 sync_and_commit
    assert out["read_snapshot_at"] == T0
    assert out["uploaded_only"] is True
    assert triggered == []          # 沒有觸發提交流程
    # 收件匣裡確實有一個 reference 項目（以 sidecar 內容為準，不看檔名）
    kinds = _uploaded_kinds(env)
    assert kinds.count("reference") == 1


def test_reference_requires_the_snapshot_time_that_was_actually_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """L5：`read_snapshot_at` 必填。

    自己抓對方「目前」的快照時間，等於宣稱讀到了一個其實沒讀過的版本。
    """
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    # 讀取端就算查得到快照時間，也不代勞
    with pytest.raises(SkillError) as e:
        reference(sd, "ses_1", "opencode:ses_9")
    assert "read_snapshot_at" in str(e.value)
    # 有帶就正常
    out = reference(sd, "ses_1", "opencode:ses_9", read_snapshot_at=T0)
    assert out["read_snapshot_at"] == T0


def test_reference_does_not_call_the_reader_at_all(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    """L5：沒有 read_snapshot_at 時**不准**自己去讀（避免記下沒讀過的時間）。"""
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)
    sd, env = _sd(tmp_path)
    _main_session(sd, env)

    def _boom(*a, **k):
        raise AssertionError("不該為了拿快照時間去讀取端")

    monkeypatch.setattr(env["reader"], "get_session", _boom)
    with pytest.raises(SkillError):
        reference(sd, "ses_1", "opencode:ses_9")


def test_stop_works_while_the_reply_is_still_streaming(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    """H3：宣告停止是在回覆**生成中**呼叫的，那一則訊息在封存之前建立。

    所以 stop 必須成立（舊的「in_progress 一律拒絕」會讓它每一次都失敗）。
    """
    sd, env = _sd(tmp_path)
    _run_committer(monkeypatch, env)
    _main_session(sd, env)
    # 最後一則 assistant 訊息還在生成（created=1790400009000、沒有 completed）
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1,
                             archived_ms=1790400010000),
                   _raw(last_incomplete=True))
    out = stop(sd, "ses_1", timeout=SHORT)
    assert env["api"].archived
    statuses = [
        json.loads(env["drive"].download_bytes(f.id, max_bytes=1 << 20))["session"]["status"]
        for f in env["drive"].list_children(env["inbox"])
        if f.name.endswith(".sidecar.json")
    ]
    assert "stopped" in statuses, f"宣告停止必須成立：{statuses}"
    assert out["session_id"] == "opencode:ses_1"


def test_message_created_after_the_archive_keeps_the_session_running(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch):
    """封存**之後**才建立的訊息 → 這個 Session 還是 running，不得宣告停止中。

    同步器算出來就是 running，所以不會有 stopped 的 sidecar，也沒有錯誤。
    """
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    # archived_ms 比最後一則訊息的 created 早 → 封存之後又有新訊息
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1,
                             archived_ms=1), _raw(last_incomplete=False))
    _run_committer(monkeypatch, env)
    stop(sd, "ses_1", timeout=SHORT)
    statuses = [
        json.loads(env["drive"].download_bytes(f.id, max_bytes=1 << 20))["session"]["status"]
        for f in env["drive"].list_children(env["inbox"])
        if f.name.endswith(".sidecar.json")
    ]
    assert statuses and "stopped" not in statuses, f"不該宣告停止中：{statuses}"


# ---------------------------------------------------------------------------
# 讀取（一定要附新鮮度）
# ---------------------------------------------------------------------------


def test_find_and_read_attach_freshness():
    sd, _ = _sd(None)
    out = tools.find(sd.reader, "接續")
    hit = out["hits"][0]
    assert hit["session_id"] == "opencode:ses_1"
    assert hit["freshness"]["snapshot_at"] == T0
    assert "freshness" in out

    read_out = read(sd.reader, "opencode:ses_1")
    assert read_out["session"]["session_id"] == "opencode:ses_1"
    assert read_out["freshness"]["snapshot_at"] == T0
    # M3：用讀取介面真正的欄位名，否則 AI 看不到「未達新鮮度」
    assert read_out["freshness"]["satisfied"] is True
    assert read_out["freshness"]["stopped_ok"] is True
    assert read_out["freshness"]["generation"] == 2
    assert "ok" not in read_out["freshness"] and "lag_s" not in read_out["freshness"]


def test_find_surfaces_the_freshness_warning():
    """有新鮮度警告時一定要讓 AI 看得見（不能吞掉）。"""
    class WarningReader(FakeReader):
        def find_sessions(self, query: Any, *, max_lag=None) -> Any:
            return FakeResult(
                [_Row(hit=_Row(session_id="opencode:ses_1", title="舊的",
                               status="running",
                               snapshot_at="2026-09-01T00:00:00Z"),
                      freshness=FakeFreshness(
                          snapshot_at="2026-09-01T00:00:00Z", ok=False,
                          warning="內容可能不是最新的（快照太舊）"))],
                FakeFreshness(snapshot_at="2026-09-01T00:00:00Z", ok=False,
                              warning="內容可能不是最新的（快照太舊）"),
            )

    sd, _ = _sd(None, reader=WarningReader())
    out = tools.find(sd.reader, "舊的")
    assert out["hits"][0]["freshness"]["satisfied"] is False
    assert "可能不是最新" in out["hits"][0]["freshness"]["warning"]
    assert "可能不是最新" in out["freshness"]["warning"]


def test_list_handoffs_filters_by_case():
    sd, env = _sd(None)
    env["reader"].handoff_rows = [
        _Row(handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV",
             target_session_id="opencode:ses_0", producer="opencode:ses_0",
             author_session_id="opencode:ses_0", case_id="c1",
             updated_at=T0),
        _Row(handoff_id="handoff:01ARZ3NDEKTSV4RRFFQ69G5FAW",
             target_session_id="opencode:ses_3", producer="opencode:ses_2",
             author_session_id="opencode:ses_2", case_id=None, updated_at=T0),
    ]
    all_rows = list_handoffs(sd)
    assert len(all_rows["handoffs"]) == 2
    only_c1 = list_handoffs(sd, case_id="c1")
    assert [h["handoff_id"] for h in only_c1["handoffs"]] == [
        "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert "freshness" in all_rows


# ---------------------------------------------------------------------------
# 宣告停止
# ---------------------------------------------------------------------------


def test_stop_archives_then_syncs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    # 宣告停止的對話已經收斂（沒有生成中的訊息），而且已經封存
    raw = _raw(last_incomplete=False)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1, archived_ms=1790400009000),
                   raw)
    _run_committer(monkeypatch, env)
    out = stop(sd, "ses_1", timeout=SHORT)
    assert env["api"].archived, "應該先用 API 設定 time.archived"
    assert env["api"].archived[0][0] == "ses_1"
    # 上傳的 sidecar 必須是 stopped（這是宣告停止的整個目的）
    statuses = []
    for f in env["drive"].list_children(env["inbox"]):
        if f.name.endswith(".sidecar.json"):
            statuses.append(json.loads(
                env["drive"].download_bytes(f.id, max_bytes=1 << 20)
            )["session"]["status"])
    assert "stopped" in statuses
    assert out["archived_ms"] == int(
        __import__("datetime").datetime.fromisoformat(
            T1.replace("Z", "+00:00")).timestamp() * 1000)


# ---------------------------------------------------------------------------
# CLI 接線
# ---------------------------------------------------------------------------


def test_skill_cli_exposes_the_documented_commands():
    from aistorage.skill.__main__ import build_parser

    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert set(sub.choices) == {
        "whoami", "split", "handoff-end", "find", "read",
        "reference", "list-handoffs", "stop",
    }
    # AI 不再自己認領：認領由 `agora checkout` 一併登記（ADR 0010）
    assert "claim" not in sub.choices
    # 產出登錄（Foundry）已隨 ADR 0009 移除
    assert "register-artifact" not in sub.choices


def test_skill_cli_whoami_prints_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    from aistorage.skill import __main__ as cli

    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    monkeypatch.setattr(cli, "_sd", lambda: sd)
    assert cli.main(["whoami", "--session", "ses_1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"session_id": "opencode:ses_1", "parent_id": None, "is_main": True}


def test_skill_cli_reports_read_side_fail_closed_cleanly(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch, capsys):
    """讀取視圖還沒發佈時要講清楚「讀不到」，不要丟 traceback 給 AI。"""
    from aistorage.reader.client import AccessDenied
    from aistorage.skill import __main__ as cli

    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    monkeypatch.setattr(cli, "_sd", lambda: sd)

    def _denied(*a, **k):
        raise AccessDenied("讀取 manifest 被拒（無 Agora 讀取權）: 找不到檔案 (HTTP 404)")

    monkeypatch.setattr(env["reader"], "find_sessions", _denied)
    assert cli.main(["find", "--query", "任何東西"]) == 5
    err = capsys.readouterr().err
    assert "讀不到 Agora 的讀取視圖" in err
    assert "fail-closed" in err
    assert "Traceback" not in err, "不該把 traceback 回給模型"


def test_skill_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    """3＝被拒收（必須停下）、4＝主 Session 限定、2＝其他 SkillError。"""
    from aistorage.skill import __main__ as cli

    sd, env = _sd(tmp_path)
    env["api"].set(OcSession(id="ses_2", parent_id="ses_1", updated_ms=1), _raw())
    monkeypatch.setattr(cli, "_sd", lambda: sd)
    monkeypatch.setattr("aistorage.syncer.commit.trigger_committer", lambda *a, **k: None)  # 不觸發
    assert cli.main(["stop", "--session", "ses_2"]) == 4
    assert "主 Session" in capsys.readouterr().err
    assert cli.main(["whoami", "--session", "ses_999"]) == 2

    # 被拒收
    def _reject(*a, **k):
        raise RejectedItems([("01ARZ3NDEKTSV4RRFFQ69G5FAV", "stale")])

    monkeypatch.setattr(tools, "_export_and_sync", _reject)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1), _raw())
    assert cli.main(["handoff-end", "--session", "ses_1", "--summary", "做完了"]) == 3
    assert "必須停下" in capsys.readouterr().err


def test_every_tool_accepts_the_argv_the_plugin_passes():
    """plugin 對**每個**工具都會帶 `--session`（Session id 由 context 帶入）。

    9.1 e2e 實測：`aistorage_find` 與 `aistorage_list_handoffs` 的子指令沒有宣告
    `--session`，argparse 直接以「unrecognized arguments」讓整個工具失敗——這兩個
    唯讀工具在容器裡 100% 不能用，AI 因此拿不到任何清單，連帶 claim 也失敗。
    單元測試都直接呼叫 Python API，繞過 argv，所以沒抓到。

    這裡照 plugin 的形狀（`aistorage_x` → `x-…` ＋ 固定帶 `--session`）把每個
    指令都送進 parser，只驗「argparse 收得下」，不碰後面的邏輯。
    """
    from aistorage.skill.__main__ import build_parser

    # (工具名, plugin 會帶的額外參數)
    calls = [
        ("aistorage_whoami", []),
        ("aistorage_split", ["--parts", "p.json"]),
        ("aistorage_handoff_end", ["--summary", "s"]),
        ("aistorage_find", ["--query", "q"]),
        ("aistorage_read", []),
        ("aistorage_reference", ["--to", "opencode:ses_2", "--read-snapshot-at", "t"]),
        ("aistorage_list_handoffs", []),
        ("aistorage_stop", []),
    ]
    parser = build_parser()
    for tool, extra in calls:
        command = tool.removeprefix("aistorage_").replace("_", "-")
        argv = [command, "--session", "ses_1", *extra]
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:  # argparse 失敗會 SystemExit(2)
            pytest.fail(f"{tool} 不接受 plugin 會送的參數 {argv}：{e}")
        assert args.command == command
        assert getattr(args, "session", "ses_1") == "ses_1"


def _inbox_sidecar_names(env: dict) -> set[str]:
    """收件匣裡所有 sidecar 的檔名（用來比對「這一趟多了什麼」）。"""
    return {f.name for f in env["drive"].list_children(env["inbox"])
            if f.name.endswith(".sidecar.json")}


def _kind_of(env: dict, name: str) -> str:
    for f in env["drive"].list_children(env["inbox"]):
        if f.name == name:
            return json.loads(
                env["drive"].download_bytes(f.id, max_bytes=1 << 20)
            )["metadata"]["type"]
    raise AssertionError(f"收件匣裡找不到 {name}")


def _sync_and_commit_self(sd, monkeypatch: pytest.MonkeyPatch, env: dict,
                          session_id: str = "ses_1") -> None:
    """同步自己並提交一次（提交流程是假的，見 _run_committer）。"""
    _run_committer(monkeypatch, env)
    tools._export_and_sync(sd, session_id, timeout=SHORT)


def test_split_recovers_when_the_export_file_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """9.1 e2e 實測的 bug：接續點算到一半，匯出檔不見了 → 整個工具崩掉。

    實際成因：容器裡同步器 daemon 也在同一個 workdir 上跑，而它**不**保留匯出檔
    （`keep_exports=False`，上傳完就刪）。所以工具剛匯出來的檔案，可能在
    `sync_once` 返回之後、`continuation_point` 讀它之前被 daemon 刪掉——
    於是 `FileNotFoundError`，而且 CLI 把整份 traceback 丟給模型，模型就一直
    重試，整場 e2e 卡死。

    這裡用「sync_once 跑完後把匯出檔刪掉」來重現那個競態。
    """
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1),
                   _raw("先講一句"))

    real_sync_once = tools.sync_once

    def _sync_then_lose_export(*args, **kwargs):  # noqa: ANN002, ANN003 - 測試替身
        outcome = real_sync_once(*args, **kwargs)
        exports = sd.deps.workdir / "exports"
        for f in exports.glob("*.json"):
            f.unlink()          # 模擬 daemon 把匯出檔清掉
        return outcome

    monkeypatch.setattr(tools, "sync_once", _sync_then_lose_export)
    _run_committer(monkeypatch, env)

    out = split(sd, "ses_1", [{"title": "做甲", "summary": "甲的工作"}], timeout=SHORT)
    assert len(out["handoff_ids"]) == 1
    # 接續點綁在已提交的快照上（也就是重新匯出來的那份）
    committed = env["reader"].catalog_data["opencode:ses_1"]["raw_sha256"]
    assert committed
    points = {
        data["body"]["continuation"]["snapshot_sha256"]
        for data in _sidecars(env) if data["metadata"]["type"] == "handoff"
    }
    assert points == {committed}


def test_ensure_export_refuses_a_stale_export_file(tmp_path: Path):
    """匯出檔是舊的 → 接續點會釘在錯的內容上，必須拒絕而不是硬算。

    寧可叫模型重試，也不要把接續點釘在沒被提交的內容上（D10）。`split` 正常
    結束時會把匯出檔刪掉，但呼叫中途中斷（逾時、容器被砍）就會留下來。
    """
    sd, _env = _sd(tmp_path)
    committed = "b" * 64

    stale = tmp_path / "work" / "exports" / "ses_1.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(_raw("很久以前的內容"), encoding="utf-8")

    class _FakeOc:
        id = "ses_1"

        @staticmethod
        def session_id(_source: str = "opencode") -> str:
            return "opencode:ses_1"

    with pytest.raises(SkillError) as e:
        tools._ensure_export(sd.deps, _FakeOc(), raw_path=stale,
                             snapshot_sha256=committed)
    assert "已提交的快照" in str(e.value)


def test_ensure_export_re_exports_and_accepts_the_committed_snapshot(tmp_path: Path):
    """檔案不存在 → 就地重新匯出；雜湊對得上就繼續。"""
    sd, env = _sd(tmp_path)
    _main_session(sd, env)
    env["api"].set(OcSession(id="ses_1", title="主線", updated_ms=1), _raw("一句話"))
    missing = tmp_path / "work" / "exports" / "ses_1.json"
    assert not missing.exists()
    committed = hashlib.sha256(_raw("一句話").encode("utf-8")).hexdigest()

    class _FakeOc:
        id = "ses_1"

    got = tools._ensure_export(sd.deps, _FakeOc(), raw_path=missing,
                               snapshot_sha256=committed)
    assert got == missing and missing.is_file()
    assert hashlib.sha256(missing.read_bytes()).hexdigest() == committed


def test_cli_turns_unexpected_exceptions_into_a_clean_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """非預期例外不能以 traceback 的形式回給模型（9.1 e2e 實測的卡死原因）。

    plugin 會把 stderr 原樣回給模型；免費模型看到 Python traceback 會照著
    stack 一直重試同一個呼叫。所以 CLI 最後一道要給人話 + 明確說「不是你參數
    寫錯」，完整堆疊寫到容器裡的日誌。
    """
    from aistorage.skill import __main__ as cli

    state_dir = tmp_path / "state"

    def _boom() -> SkillDeps:
        raise FileNotFoundError(f"{state_dir}/exports/ses_1.json")

    monkeypatch.setattr(cli, "_sd", _boom)
    monkeypatch.setenv("AISTORAGE_STATE_DIR", str(state_dir))
    assert cli.main(["whoami", "--session", "ses_1"]) == 7

    err = capsys.readouterr().err
    assert "Traceback" not in err, "traceback 不可以直接給模型"
    assert "FileNotFoundError" in err
    assert "不是你的參數問題" in err
    # 完整堆疊仍然留給人看
    log = (state_dir / "skill-errors.log").read_text(encoding="utf-8")
    assert "FileNotFoundError" in log and "boom" in log


def test_cli_still_uses_its_own_exit_codes_for_known_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """已知的錯誤還是要走原本的退出碼（plugin／測試靠它們分辨）。"""
    from aistorage.skill import __main__ as cli

    sd, _env = _sd(tmp_path)

    def _denied(*_a, **_k):
        raise ConfigError("找不到收件匣 folder id")

    monkeypatch.setattr(cli, "_sd", lambda: sd)
    monkeypatch.setattr(cli.tools, "whoami", _denied)
    assert cli.main(["whoami", "--session", "ses_1"]) == 2
    assert "ConfigError" in capsys.readouterr().err

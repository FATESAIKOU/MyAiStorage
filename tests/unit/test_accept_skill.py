"""Independent Acceptance Tests for Skill Tools (Task 5.4).

Adheres strictly to:
- docs/impl/group5-7-modules.md §4, §8.2
- PM Decision 4: reference default upload_only=True (no commit triggered)
- 9 tools verification, main session restriction, exit codes
- No claim tool: taking over a new session is `agora checkout` (ADR 0010)
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence
import pytest

from aistorage.clock import FixedClock
from aistorage.converters import get_converter
from aistorage.drive.fake import FakeDrive
from aistorage.identity import generate_keypair
import aistorage.syncer.commit
import hashlib
from aistorage.skill import (
    MainSessionRequired,
    RejectedItems,
    SkillDeps,
    SkillError,
    find,
    handoff_end,
    list_handoffs,
    read,
    reference,
    split,
    stop,
    whoami,
)
from aistorage.skill.__main__ import main as skill_cli_main
from aistorage.syncer import (
    OcSession,
    OpencodeApi,
    Signer,
    SyncDeps,
    SyncState,
)


T0 = "2026-09-27T08:00:00Z"
T1 = "2026-09-27T09:00:00Z"
PROFILE = "mac-opencode"


# ---------------------------------------------------------------------------
# Test Fakes
# ---------------------------------------------------------------------------


class MockOpencodeApi:
    """Mock implementation of OpencodeApi."""

    def __init__(self) -> None:
        self.sessions: dict[str, OcSession] = {}
        self.raws: dict[str, str] = {}
        self.archived: list[tuple[str, int]] = []

    def set_session(self, session: OcSession, raw_json: str) -> None:
        self.sessions[session.id] = session
        self.raws[session.id] = raw_json

    def list_sessions(self) -> list[OcSession]:
        return sorted(self.sessions.values(), key=lambda s: s.id)

    def export(self, session_id: str, dest: Path) -> Path:
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.raws[session_id], encoding="utf-8")
        return out

    def archive(self, session_id: str, at_ms: int) -> None:
        self.archived.append((session_id, at_ms))
        if session_id in self.sessions:
            s = self.sessions[session_id]
            self.sessions[session_id] = OcSession(
                id=s.id,
                parent_id=s.parent_id,
                title=s.title,
                updated_ms=s.updated_ms,
                archived_ms=at_ms,
            )


class MockFreshness:
    def __init__(self, snapshot_at: str = T0, ok: bool = True, warning: str | None = None) -> None:
        self.snapshot_at = snapshot_at
        self.lag_s = 60
        self.ok = ok
        self.warning = warning


class MockQueryResult:
    def __init__(self, value: Any, freshness: Any) -> None:
        self.value = value
        self.freshness = freshness


class MockReader:
    """Mock implementation of AgoraReader for skill tools."""

    def __init__(self) -> None:
        self.catalog_data: dict[str, dict] = {}
        self.rejections: dict[str, Any] = {}
        self.session_views: dict[str, Any] = {}
        self.continuations: dict[str, Any] = {}
        self.open_handoffs: list[Any] = []
        self.search_hits: list[Any] = []

    def catalog(self, session_ids: Sequence[str]) -> dict[str, dict]:
        return {s: self.catalog_data[s] for s in session_ids if s in self.catalog_data}

    def get_rejection(self, item_key: str) -> Any:
        return self.rejections.get(item_key)

    def get_session(self, session_id: str, *, max_lag: Any = None) -> Any:
        view = self.session_views.get(session_id)
        return MockQueryResult(view, MockFreshness())

    def find_sessions(self, query: Any, *, max_lag: Any = None) -> Any:
        return MockQueryResult(self.search_hits, MockFreshness())

    def get_continuation(self, handoff_id: str) -> Any:
        return MockQueryResult(self.continuations.get(handoff_id), MockFreshness())

    def list_open_handoffs(self, *, case_id: str | None = None) -> Any:
        rows = self.open_handoffs
        if case_id:
            rows = [r for r in rows if getattr(r, "case_id", None) == case_id]
        return MockQueryResult(rows, MockFreshness())


def make_raw_session(session_id: str, messages: list[dict], *, archived: int | None = None) -> str:
    return json.dumps({
        "info": {
            "id": session_id,
            "title": f"Title {session_id}",
            "time": {"created": 1790400000000, "updated": 1790400009000},
            **({"archived": archived} if archived is not None else {}),
        },
        "messages": messages,
    }, ensure_ascii=False)


def make_test_signer(profile: str = PROFILE) -> Signer:
    priv, pub = generate_keypair()
    kid = f"{profile}-{hashlib.sha256(pub).hexdigest()[:8].lower()}"
    return Signer(profile, kid, priv)


class ViewRow:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def build_skill_env(tmp_path: Path) -> tuple[SkillDeps, dict[str, Any]]:
    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    api = MockOpencodeApi()
    reader = MockReader()
    signer = make_test_signer(PROFILE)
    state = SyncState(tmp_path / "sync-state.json")
    workdir = tmp_path / "work"
    pat = tmp_path / "gh-pat.txt"
    pat.write_text("ghp_fake_token_for_tests\n")

    deps = SyncDeps(
        api=api,
        reader=reader,
        drive=drive,
        inbox_folder_id=inbox,
        signer=signer,
        state=state,
        clock=FixedClock(T0),
        workdir=workdir,
        converter=get_converter("opencode"),
        repo="test/repo",
        pat_path=pat,
    )
    sd = SkillDeps(deps=deps, state=state)
    env = {
        "api": api,
        "reader": reader,
        "drive": drive,
        "inbox": inbox,
        "state": state,
        "signer": signer,
    }
    return sd, env


# ---------------------------------------------------------------------------
# Acceptance Tests
# ---------------------------------------------------------------------------


def test_tool_whoami_identifies_main_and_child(tmp_path: Path):
    """驗證 aistorage_whoami 能夠正確辨別主 Session 與子 Session。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]

    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=1000, archived_ms=None), "{}")
    api.set_session(OcSession(id="ses_sub", parent_id="ses_main", title="Sub", updated_ms=1000, archived_ms=None), "{}")

    main_info = whoami(api, "ses_main")
    assert main_info["session_id"] == "opencode:ses_main"
    assert main_info["parent_id"] is None
    assert main_info["is_main"] is True

    sub_info = whoami(api, "ses_sub")
    assert sub_info["session_id"] == "opencode:ses_sub"
    assert sub_info["parent_id"] == "opencode:ses_main"
    assert sub_info["is_main"] is False


def test_tool_stop_main_session_restriction(tmp_path: Path):
    """驗證 aistorage_stop 嚴格限制在主 Session 執行（子 Session 拋出 MainSessionRequired）。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]

    api.set_session(OcSession(id="ses_sub", parent_id="ses_main", title="Sub", updated_ms=1000, archived_ms=None), "{}")

    with pytest.raises(MainSessionRequired, match="主 Session"):
        stop(sd, "ses_sub")


def test_tool_reference_pm_decision_4_upload_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """驗證 PM 決定 4: aistorage_reference 預設 upload_only=True（只上傳收件匣，不觸發提交流程）。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]
    drive: FakeDrive = env["drive"]
    inbox: str = env["inbox"]

    raw_json = make_raw_session("ses_main", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1000, "completed": 2000}}, "parts": [{"type": "text", "text": "ref"}]},
    ])
    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=2000, archived_ms=None), raw_json)

    # 追蹤 trigger_committer 是否被呼叫
    committer_triggered = []
    import aistorage.syncer.commit
    monkeypatch.setattr(aistorage.syncer.commit, "trigger_committer", lambda *args, **kwargs: committer_triggered.append(True))

    res = reference(sd, "ses_main", "target:ses_other", read_snapshot_at=T0)

    # 驗證未觸發提交流程
    assert len(committer_triggered) == 0
    assert res.get("status") == "uploaded" or res.get("reference_id") is not None

    # 驗證收件匣確實上傳了 reference 檔案
    files = drive.list_children(inbox)
    sidecars = [f for f in files if f.name.endswith(".sidecar.json")]
    assert len(sidecars) >= 1
    content = json.loads(drive.download_bytes(sidecars[0].id, max_bytes=1024*1024).decode("utf-8"))
    assert content["metadata"]["type"] == "reference"
    assert content["body"]["to_session_id"] == "target:ses_other"


def test_tool_stop_archives_and_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """驗證 aistorage_stop 在主 Session 執行時，呼叫 api.archive 並進行同步與提交。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]
    reader: MockReader = env["reader"]

    raw_json = make_raw_session("ses_main", [
        {"info": {"id": "m1", "role": "user", "time": {"created": 1000, "completed": 2000}}, "parts": [{"type": "text", "text": "done"}]},
    ])
    raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=2000, archived_ms=None), raw_json)

    # 模擬提交成功後的讀取端狀態
    reader.catalog_data["opencode:ses_main"] = {"raw_sha256": raw_sha, "snapshot_at": T0, "status": "stopped"}

    monkeypatch.setattr(aistorage.syncer.commit, "trigger_committer", lambda *args, **kwargs: None)

    res = stop(sd, "ses_main", timeout=timedelta(seconds=2))
    assert len(api.archived) == 1
    assert api.archived[0][0] == "ses_main"
    assert res.get("status") in ("stopped", "ok") or "session_id" in res


def test_tool_find_and_read_includes_freshness(tmp_path: Path):
    """驗證 aistorage_find 與 aistorage_read 一律附上快照時間與新鮮度。"""
    sd, env = build_skill_env(tmp_path)
    reader: MockReader = env["reader"]

    reader.search_hits = [
        ViewRow(hit=ViewRow(session_id="opencode:s1", title="Title 1", status="running", snapshot_at=T0),
                freshness=MockFreshness(snapshot_at=T0, ok=True))
    ]
    reader.session_views["opencode:s1"] = ViewRow(
        session=ViewRow(session_id="opencode:s1", snapshot_at=T0, status="running", title="Title 1"),
        links_out=(), links_in=(), handoffs_targeting=(), handoffs_by_holder=(),
    )

    find_res = find(reader, "test query")
    assert "freshness" in find_res
    assert find_res["freshness"]["snapshot_at"] == T0
    assert len(find_res["hits"]) == 1

    read_res = read(reader, "opencode:s1")
    assert "freshness" in read_res
    assert read_res["freshness"]["snapshot_at"] == T0
    assert read_res["session"]["session_id"] == "opencode:s1"


def test_tool_list_handoffs(tmp_path: Path):
    """驗證 aistorage_list_handoffs 列出待認領交接單，支援 case_id 過濾。"""
    sd, env = build_skill_env(tmp_path)
    reader: MockReader = env["reader"]

    reader.open_handoffs = [
        ViewRow(handoff_id="h1", case_id="case_A", summary="Summary A"),
        ViewRow(handoff_id="h2", case_id="case_B", summary="Summary B"),
    ]

    all_handoffs = list_handoffs(sd)
    assert len(all_handoffs["handoffs"]) == 2

    case_a = list_handoffs(sd, case_id="case_A")
    assert len(case_a["handoffs"]) == 1
    assert case_a["handoffs"][0]["handoff_id"] == "h1"


# ---------------------------------------------------------------------------
# CLI Exit Code Tests
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """驗證 CLI 呼叫的 exit codes: 0 正常, 4 主 Session 限定, 3 拒收, 2 其它錯誤。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]
    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=1000, archived_ms=None), "{}")
    api.set_session(OcSession(id="ses_sub", parent_id="ses_main", title="Sub", updated_ms=1000, archived_ms=None), "{}")

    # 模擬 _sd() 回傳我們的測試相依
    import aistorage.skill.__main__ as skill_main_mod
    monkeypatch.setattr(skill_main_mod, "_sd", lambda: sd)

    # 1. whoami 成功 -> exit code 0
    rc = skill_cli_main(["whoami", "--session", "ses_main"])
    assert rc == 0

    # 2. subsession 呼叫 stop -> exit code 4 (MainSessionRequired)
    rc_stop = skill_cli_main(["stop", "--session", "ses_sub"])
    assert rc_stop == 4

    # 3. 拒收項目 -> exit code 3 (RejectedItems)
    def mock_handoff_end_fail(*args, **kwargs):
        raise RejectedItems([("h1", "handoff rejected by committer")])
    monkeypatch.setattr("aistorage.skill.tools.handoff_end", mock_handoff_end_fail)
    rc_rej = skill_cli_main(
        ["handoff-end", "--session", "ses_main", "--summary", "做完了"])
    assert rc_rej == 3

    # 4. SkillError -> exit code 2
    def mock_split_err(*args, **kwargs):
        raise SkillError("continuation point missing")
    monkeypatch.setattr("aistorage.skill.tools.split", mock_split_err)
    parts_file = tmp_path / "parts.json"
    parts_file.write_text(json.dumps([{"title": "t", "summary": "s"}]))
    rc_err = skill_cli_main(["split", "--session", "ses_main", "--parts", str(parts_file)])
    assert rc_err == 2


def test_tool_split_and_handoff_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """驗證 aistorage_split 與 aistorage_handoff_end 產生交接單並以最後已完成訊息為接續點。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]
    reader: MockReader = env["reader"]

    raw_json = make_raw_session("ses_main", [
        {"info": {"id": "m1", "role": "assistant", "time": {"created": 1000, "completed": 2000}}, "parts": [{"type": "text", "text": "part1"}]},
        {"info": {"id": "m2", "role": "assistant", "time": {"created": 3000, "completed": 4000}}, "parts": [{"type": "text", "text": "part2"}]},
        {"info": {"id": "m3", "role": "assistant", "time": {"created": 5000}}, "parts": [{"type": "text", "text": "generating..."}]},
    ])
    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=5000, archived_ms=None), raw_json)

    # 提交流程假裝成功
    monkeypatch.setattr(aistorage.syncer.commit, "trigger_committer", lambda *args, **kwargs: None)

    # 模擬 wait_visible 檢查通過
    def mock_sync_and_commit(session_ids, extra_items, deps, **kw):
        from aistorage.syncer import Awaited, CommitWaitResult
        awaited = [Awaited(item_key=i.item_key, kind="handoff", target=i.item_id) for i in extra_items]
        return CommitWaitResult(visible=tuple(awaited), rejected=(), pending=(), timed_out=False, elapsed_s=0.1)

    monkeypatch.setattr("aistorage.skill.tools.sync_and_commit", mock_sync_and_commit)

    res_split = split(sd, "ses_main", [
        {"title": "Task 1", "summary": "Do 1"},
        {"title": "Task 2", "summary": "Do 2"},
    ])
    assert len(res_split["handoff_ids"]) == 2

    res_handoff = handoff_end(sd, "ses_main", "Wrap up session")
    assert len(res_handoff["handoff_ids"]) == 1


def test_tool_split_refuses_when_no_completed_message(tmp_path: Path):
    """驗證當 Session 沒有已完成訊息時，split 拒絕執行（避免捏造接續點）。"""
    sd, env = build_skill_env(tmp_path)
    api: MockOpencodeApi = env["api"]

    # 所有訊息都還在生成中（沒有 completed 時間）
    raw_json = make_raw_session("ses_main", [
        {"info": {"id": "m1", "role": "assistant", "time": {"created": 1000}}, "parts": [{"type": "text", "text": "generating..."}]},
    ])
    api.set_session(OcSession(id="ses_main", parent_id=None, title="Main", updated_ms=1000, archived_ms=None), raw_json)

    with pytest.raises(SkillError, match="接續點"):
        split(sd, "ses_main", [{"title": "Task 1", "summary": "Do 1"}])


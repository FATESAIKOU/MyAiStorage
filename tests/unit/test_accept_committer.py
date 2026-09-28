"""3.1 提交流程主體的驗收測試（C 線撰寫）。

未看實作，只依文件與簽名：
- docs/impl/group3-modules.md 第 7 節（workflow、CLI、13 步骨架）
- design D2 的 13 步、D3、ADR 0008。
用 fake（FakeDrive、MemoryPinStore、FakeAnnexGit 家族、NullPublisher）。
失敗＝回報 A 線（impl1），不改 src/。

已知限制（見回報）：
- git 主分支推進語意是 fake 保真度缺口：snapshot commit 推進 fake-local，
  但 fake copy 不產生新 bundle，真實重放對不上。因此成功輪使用「不推進
  main 的 git」（annex 模式建模：apply 不動 main，只有 consolidate 才動）；
  pins 的寫待定→轉正完整路徑需真實 bundle 管線，另見下述。
- 曾驗證：advancing git 下任何非空輪都在 verify_after_push 中止。
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aistorage.agora.store import FakeRawStorage
from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock
from aistorage.committer import Deps, NullPublisher, prescan, run
from aistorage.committer.config import CommitterConfig, PrefixLevel
from aistorage.converters import get_converter
from aistorage.drive.fake import FakeDrive
from aistorage.errors import ReadError, WriteError
from aistorage.identity import Registry
from aistorage.inbox import sign_sidecar_bytes
from aistorage.inbox_builder import build_inbox_item
from aistorage.integrity.pin import MemoryPinStore, PinPending, PinState
from aistorage.publish.publisher import PublishReport
from aistorage.schema import generate_ulid

UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "mac-opencode"
GOLDEN = Path("tests/unit/data/converters/opencode/basic.json")
SECRET_MARKER = "SECRET_MARKER_XYZ_777"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """真實可重放 bundle（git 製作；D2 settle／verify 需要）。"""
    d = tmp_path_factory.mktemp("bundles")
    repo = d / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}

    def g(*args: str) -> str:
        subprocess.run(["git", *args], cwd=repo, env=env,
                       check=True, capture_output=True)
        return ""

    g("init", "-b", "main", "-q", ".")
    (repo / "f.txt").write_text("one\n")
    g("add", "f.txt")
    g("commit", "-qm", "c1")
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env,
                         check=True, capture_output=True, text=True)
    c1 = out.stdout.strip()
    g("update-ref", f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main", c1)
    g("bundle", "create", str(d / "b1.bundle"),
      f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main")
    raw = (d / "b1.bundle").read_bytes()
    name = f"GITBUNDLE-s{len(raw)}--{UUID}-{hashlib.sha256(raw).hexdigest()}"
    manifest = (name + "\n").encode()
    return {"c1": c1, "name": name, "bytes": raw,
            "manifest": manifest, "manifest_sha": hashlib.sha256(manifest).hexdigest()}


def _keypair() -> tuple[bytes, bytes, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    seed = Ed25519PrivateKey.generate().private_bytes_raw()
    pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    return seed, pub, f"{PROFILE}-{hashlib.sha256(pub).hexdigest()[:8]}"


class Env:
    pass


def _variant(tmp_path: Path, name: str, title: str) -> Path:
    data = json.loads(GOLDEN.read_bytes().decode("utf-8"))
    data["info"]["title"] = title
    p = tmp_path / name
    p.write_bytes(json.dumps(data, sort_keys=True).encode("utf-8"))
    return p


def make_env(tmp_path: Path, bundle: dict, *, with_items: bool = True,
             marker: bool = False) -> Env:
    """完整一輪夾具：prefix（manifest＋真 bundle）＋收件匣 5 件
   （2 session＋handoff＋claim＋reference）＋ registry＋pin。"""
    e = Env()
    e.conv = get_converter("opencode")
    drive = FakeDrive()
    e.drive = drive
    e.root = drive.seed_folder("root")
    e.prefix = drive.seed_folder("prefix", parent=e.root)
    e.quar = drive.seed_folder("quarantine")
    e.inbox = drive.seed_folder("inbox")
    drive.seed_file(e.prefix, f"GITMANIFEST--{UUID}", bundle["manifest"])
    drive.seed_file(e.prefix, bundle["name"], bundle["bytes"])
    seed, pub, key_id = _keypair()
    e.seed, e.key_id = seed, key_id
    reg = {"format": "aistorage.identity/v1",
           "profiles": {PROFILE: {
               "allowed_types": ["session", "handoff", "claim", "reference"],
               "signing_keys": [{"key_id": key_id,
                                 "public_key": base64.b64encode(pub).decode(),
                                 "status": "active",
                                 "added_at": "2026-09-27T00:00:00Z",
                                 "revoked_at": None}],
               "inbox_folder_ids": [e.inbox]}}}
    e.reg_path = tmp_path / "registry.json"
    e.reg_path.write_text(json.dumps(reg))
    e.item_keys: list[str] = []

    def seed_kv(sidecar: dict, raw_bytes: bytes | None) -> str:
        item_key = generate_ulid()
        sidecar["item_key"] = item_key
        scb = json.dumps(sidecar, sort_keys=True).encode()
        drive.seed_file(e.inbox, f"{item_key}.sidecar.json", scb)
        drive.seed_file(
            e.inbox, f"{item_key}.sig",
            json.dumps(sign_sidecar_bytes(scb, seed, key_id)).encode())
        if raw_bytes is not None:
            drive.seed_file(e.inbox, f"{item_key}.raw", raw_bytes)
        e.item_keys.append(item_key)
        return item_key

    e.seed_kv = seed_kv
    if with_items:
        title = (SECRET_MARKER + " ") if marker else ""
        p_t = _variant(tmp_path, "target.json", f"{title}title-target")
        scb, _ = build_inbox_item(
            p_t, source="opencode", source_session_id="target-1",
            facts=e.conv.facts(p_t), profile=PROFILE, key=seed, key_id=key_id,
            snapshot_at="2026-09-27T08:00:00Z")
        seed_kv(json.loads(scb.decode()), p_t.read_bytes())
        p_c = _variant(tmp_path, "claimer.json", "title-claimer")
        scb, _ = build_inbox_item(
            p_c, source="opencode", source_session_id="claimer-1",
            facts=e.conv.facts(p_c), profile=PROFILE, key=seed, key_id=key_id,
            snapshot_at="2026-09-27T08:05:00Z")
        seed_kv(json.loads(scb.decode()), p_c.read_bytes())

        reading = e.conv.convert(p_t, session_id="opencode:target-1")
        last_mid = [m for m in reading["messages"]
                    if m["completed"] and not m["reverted"]][-1]["message_id"]
        sha_t = hashlib.sha256(p_t.read_bytes()).hexdigest()
        handoff_ulid = generate_ulid()
        e.handoff_ulid = handoff_ulid
        seed_kv({"format": "aistorage.inbox/v1", "profile": PROFILE,
                 "metadata": {"id": f"handoff:{handoff_ulid}", "type": "handoff",
                              "created_at": "2026-09-27T08:30:00Z",
                              "updated_at": "2026-09-27T08:30:00Z"},
                 "raw": None,
                 "body": {"target_session_id": "opencode:target-1",
                          "continuation": {"snapshot_sha256": sha_t,
                                           "message_id": last_mid},
                          "content": "接手"}}, None)
        seed_kv({"format": "aistorage.inbox/v1", "profile": PROFILE,
                 "metadata": {"id": f"claim:{generate_ulid()}", "type": "claim",
                              "created_at": "2026-09-27T08:40:00Z",
                              "updated_at": "2026-09-27T08:40:00Z"},
                 "raw": None,
                 "body": {"handoff_id": f"handoff:{handoff_ulid}",
                          "claimer_session_id": "opencode:claimer-1"}}, None)
        seed_kv({"format": "aistorage.inbox/v1", "profile": PROFILE,
                 "metadata": {"id": f"reference:{generate_ulid()}",
                              "type": "reference",
                              "created_at": "2026-09-27T08:50:00Z",
                              "updated_at": "2026-09-27T08:50:00Z"},
                 "raw": None,
                 "body": {"from_session_id": "opencode:claimer-1",
                          "to_session_id": "opencode:target-1",
                          "read_snapshot_at": "2026-09-27T08:00:00Z"}}, None)
    e.state = PinState(
        repo="agora", repo_uuid=UUID, refs={"refs/heads/main": bundle["c1"]},
        manifest_sha256=bundle["manifest_sha"], prev_manifest_sha256=None,
        active_bundles=(bundle["name"],), removed_bundles=frozenset(),
        annex_keys=frozenset(), promoted_at="2026-09-27T00:00:00Z", run_id="r0")
    return e


class NoCommitGit(FakeAnnexGit):
    """apply 不推進 main 的 git（annex 模式建模；見模組 docstring 的已知限制）。"""

    def add(self, paths):
        return None

    def commit(self, message):
        return "0" * 40


class CountingGit(NoCommitGit):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def push(self, *args, **kwargs):
        self.calls.append("push")
        return super().push(*args, **kwargs)

    def copy(self, *args, **kwargs):
        self.calls.append("copy")
        return super().copy(*args, **kwargs)


class SpyPublisher:
    """記錄 publish 呼叫並拷貝當下 worktree（step 12 時真本仍在）。"""

    def __init__(self, dest: Path) -> None:
        self.calls: list[str] = []
        self.dest = dest

    def publish(self, store, **kwargs):
        self.calls.append("publish")
        shutil.copytree(store.worktree, self.dest, dirs_exist_ok=True)
        return None


def _run_env(e: Env, tmp_path: Path, *, git=None, pins=None, levels=(),
             publisher=None, dry_run: bool = False,
             clock=None) -> tuple:
    git = git if git is not None else NoCommitGit(refs={"refs/heads/main": e.state.refs["refs/heads/main"]})
    pins = pins if pins is not None else MemoryPinStore(initial_state=e.state)
    cfg = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id=e.prefix, quarantine_folder_id=e.quar,
        identity_registry_path=str(e.reg_path), prefix_levels=tuple(levels))
    # H1：假 git 也要報得出自己的身分，verify_clone_identity 才會過
    git.repo_url = cfg.repo_url
    git.repo_uuid = cfg.repo_uuid
    deps = Deps(
        drive=e.drive, pins=pins, git_factory=lambda path, target: git,
        registry=Registry(json.loads(e.reg_path.read_text())),
        converters={"opencode": e.conv}, publisher=publisher or NullPublisher(),
        clock=clock or FixedClock("2026-09-27T10:00:00Z"),
        raw_storage_factory=lambda worktree, git: FakeRawStorage())
    return run(cfg, deps, dry_run=dry_run), git, pins


def _pins_state(pins) -> tuple:
    try:
        return pins.load("agora")
    except Exception as e:
        return type(e).__name__


# ---------------------------------------------------------------------------
# guard（第 1 步）與空輪（第 2 步）
# ---------------------------------------------------------------------------

def test_guard_local_without_env(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle, with_items=False)
    git = NoCommitGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.guard == "local"
    assert report.aborted_at is None


def test_guard_rejects_foreign_ref(tmp_path: Path, bundle: dict,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    e = make_env(tmp_path, bundle)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/evil")
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.aborted_at == "guard"
    assert report.code == "invalid_ref"
    assert git.calls == []  # 第 1 步之後什麼都沒執行


def test_guard_requires_token_for_sha_check(tmp_path: Path, bundle: dict,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    e = make_env(tmp_path, bundle)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.aborted_at == "guard"
    assert report.code == "missing_token"
    assert git.calls == []


def test_empty_inbox_no_clone(tmp_path: Path, bundle: dict, capsys) -> None:
    e = make_env(tmp_path, bundle, with_items=False)
    cfg_only = CommitterConfig(
        repo="agora", repo_uuid=UUID, repo_url="drive://agora",
        prefix_folder_id=e.prefix, quarantine_folder_id=e.quar,
        identity_registry_path=str(e.reg_path))
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    deps = Deps(
        drive=e.drive, pins=MemoryPinStore(), git_factory=lambda path, target: git,
        registry=Registry(json.loads(e.reg_path.read_text())),
        converters={"opencode": e.conv}, publisher=NullPublisher(),
        clock=FixedClock("2026-09-27T10:00:00Z"),
        raw_storage_factory=lambda worktree, git: FakeRawStorage())
    assert prescan(cfg_only, deps) == 0
    captured = capsys.readouterr()
    assert "EMPTY" in captured.err
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.aborted_at is None
    assert git.calls == []  # 空收件匣不 clone


def test_dry_run_writes_nothing(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    before_drive = e.drive.snapshot()
    before_inbox = sorted(f.name for f in e.drive.list_children(e.inbox))
    assert len(before_inbox) == 12
    pins = MemoryPinStore(initial_state=e.state)
    report, _, _ = _run_env(e, tmp_path, pins=pins, dry_run=True)
    assert report.aborted_at is None
    assert e.drive.snapshot() == before_drive
    state_after, pending_after = pins.load("agora")
    assert state_after == e.state and pending_after is None
    assert sorted(f.name for f in e.drive.list_children(e.inbox)) == before_inbox


# ---------------------------------------------------------------------------
# 每一步注入失敗都中止且後續步驟沒執行（D2）
# ---------------------------------------------------------------------------

def test_step2_scan_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    e.drive.inject("list_children", None, error=ReadError)
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.aborted_at == "intake.scan"
    assert git.calls == []


def test_step3_settle_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    pend = PinPending(repo="agora", base_manifest_sha256=bundle["manifest_sha"],
                      refs={"refs/heads/main": "0" * 40}, annex_keys=frozenset(),
                      written_at="2026-09-27T01:00:00Z", run_id="rx")
    pins = MemoryPinStore(initial_state=e.state, initial_pending=pend)
    for f in e.drive.find_by_name(e.prefix, f"GITMANIFEST--{UUID}"):
        e.drive.update_content(f.id, b"corrupted!!!")
    before_drive = e.drive.snapshot()
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git, pins=pins)
    assert report.aborted_at == "integrity.settle"
    assert git.calls == []  # clone（第 5 步）沒執行
    state_after, pending_after = pins.load("agora")
    assert state_after == e.state and pending_after == pend  # pin 沒被碰
    assert e.drive.snapshot() == before_drive  # 全程唯讀


def test_step4_sweep_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    git = CountingGit(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(
        e, tmp_path, git=git,
        levels=[PrefixLevel(parent_id=e.root, name="prefix", expected_id="wrong-id")])
    assert report.aborted_at == "integrity.sweep"
    assert git.calls == []


def test_step5_clone_verify_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    pins = MemoryPinStore(initial_state=e.state)
    report, _, _ = _run_env(
        e, tmp_path, git=NoCommitGit(refs={"refs/heads/main": "f" * 40}), pins=pins)
    assert report.aborted_at == "annex.git.clone"
    state_after, pending_after = pins.load("agora")
    assert state_after == e.state and pending_after is None
    assert len(e.drive.list_children(e.inbox)) == 12  # 收件匣沒被碰


def test_step6_copy_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)

    class FailCopy(NoCommitGit):
        def copy(self, *args, **kwargs):
            raise WriteError("boom-copy")

    git = FailCopy(refs={"refs/heads/main": bundle["c1"]})
    pushed: list[str] = []
    orig_push = git.push
    git.push = lambda *a, **k: (pushed.append("push"), orig_push(*a, **k))[1]
    report, _, _ = _run_env(e, tmp_path, git=git)
    assert report.aborted_at == "annex.git.copy"
    assert pushed == []  # push（第 9 步）沒執行


def test_step7_evaluate_abort(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    raws = [f for f in e.drive.list_children(e.inbox) if f.name.endswith(".raw")]
    assert raws
    e.drive.inject("download", raws[0].id, error=ReadError)
    pins = MemoryPinStore(initial_state=e.state)
    report, _, _ = _run_env(e, tmp_path, pins=pins)
    assert report.aborted_at == "intake.evaluate"
    state_after, pending_after = pins.load("agora")
    assert state_after == e.state and pending_after is None  # 待定沒寫入


def test_step8_write_pending_abort(tmp_path: Path, bundle: dict) -> None:
    """待定寫入失敗 → 中止，push 沒執行（advancing git 才會走到這一步）。"""
    e = make_env(tmp_path, bundle)

    class FailWrite(MemoryPinStore):
        def write_pending(self, pending):
            raise WriteError("boom-write-pending")

    class PushCount(FakeAnnexGit):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.calls: list[str] = []

        def push(self, *args, **kwargs):
            self.calls.append("push")
            return super().push(*args, **kwargs)

    git = PushCount(refs={"refs/heads/main": bundle["c1"]})
    report, _, _ = _run_env(e, tmp_path, git=git,
                            pins=FailWrite(initial_state=e.state))
    assert report.aborted_at == "pins.write_pending"
    assert git.calls == []


def test_step9_push_abort_keeps_pending(tmp_path: Path, bundle: dict) -> None:
    """push 失敗 → 中止，待定留著給下一輪結算。"""
    e = make_env(tmp_path, bundle)

    class FailPush(FakeAnnexGit):
        def push(self, *args, **kwargs):
            raise WriteError("boom-push")

    pins = MemoryPinStore(initial_state=e.state)
    report, _, _ = _run_env(
        e, tmp_path, git=FailPush(refs={"refs/heads/main": bundle["c1"]}), pins=pins)
    assert report.aborted_at == "git.push"
    _, pending_after = pins.load("agora")
    assert pending_after is not None


def test_step10_verify_abort_on_silent_push(tmp_path: Path, bundle: dict) -> None:
    """push 靜默失敗 → push 後驗證擋下，待定留著。"""
    e = make_env(tmp_path, bundle)
    pins = MemoryPinStore(initial_state=e.state)
    report, _, _ = _run_env(
        e, tmp_path,
        git=FakeAnnexGit(refs={"refs/heads/main": bundle["c1"]},
                         push_effect="silent_fail"),
        pins=pins)
    assert report.aborted_at == "verify.verify_after_push"
    _, pending_after = pins.load("agora")
    assert pending_after is not None


def test_step12_publish_failure_tolerated(tmp_path: Path, bundle: dict) -> None:
    """第 12 步失敗 → 標記後繼續第 13 步（group4 附記），收件匣照常清理。"""

    class FailPublisher:
        def publish(self, *args, **kwargs):
            from aistorage.publish.publisher import PublishReport
            return PublishReport(
                status="publish_failed", manifest_file_id="manifest-1",
                generation=0, agora_main_sha="deadbeef")

    e = make_env(tmp_path, bundle)
    report, _, _ = _run_env(e, tmp_path, publisher=FailPublisher())
    assert report.aborted_at is None  # 第 13 步照常執行
    assert e.drive.list_children(e.inbox) == []


# ---------------------------------------------------------------------------
# 一輪之內 session＋handoff＋claim＋reference（tasks 3.7 驗收形狀）
# ---------------------------------------------------------------------------

def test_round_applies_all_link_types(tmp_path: Path, bundle: dict) -> None:
    e = make_env(tmp_path, bundle)
    captured = tmp_path / "captured"

    class SpyPublisher:
        def publish(self, store, **kwargs):
            shutil.copytree(store.worktree, captured, dirs_exist_ok=True)
            return None

    report, _, _ = _run_env(e, tmp_path, publisher=SpyPublisher())
    assert report.aborted_at is None, report
    assert report.counts.get("accepted") == 5
    assert e.drive.list_children(e.inbox) == []  # 第 13 步清理

    metas = list((captured / "sessions").rglob("meta.json"))
    assert len(metas) == 2
    by_id = {json.loads(p.read_text(encoding="utf-8"))["id"]: p for p in metas}
    assert set(by_id) == {"opencode:target-1", "opencode:claimer-1"}

    handoffs = list((captured / "handoffs").glob("*.json"))
    assert len(handoffs) == 1
    handoff = json.loads(handoffs[0].read_text(encoding="utf-8"))
    assert handoff["claimed_by"] is not None
    assert handoff["claimed_by"]["session_id"] == "opencode:claimer-1"

    cont_links = list((captured / "links" / "continuation").rglob("*.json"))
    assert len(cont_links) == 1
    link = json.loads(cont_links[0].read_text(encoding="utf-8"))
    assert link["from"] == "opencode:claimer-1"
    assert link["to"] == "opencode:target-1"
    assert link["continuation"]["message_id"] == handoff["body"]["continuation"]["message_id"]

    ref_links = list((captured / "links" / "reference").rglob("*.json"))
    assert len(ref_links) == 1
    ref = json.loads(ref_links[0].read_text(encoding="utf-8"))
    assert (ref["from"], ref["to"]) == ("opencode:claimer-1", "opencode:target-1")


def test_rejected_item_stays_until_timeout(tmp_path: Path, bundle: dict) -> None:
    """拒收（驗章失敗）計數正確，且 24 小時內不刪除。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from aistorage.inbox import sign_sidecar_bytes as _sign
    e = make_env(tmp_path, bundle, with_items=False)
    p = _variant(tmp_path, "bad.json", "bad")
    scb, _ = build_inbox_item(
        p, source="opencode", source_session_id="bad-1",
        facts=e.conv.facts(p), profile=PROFILE, key=e.seed, key_id=e.key_id,
        snapshot_at="2026-09-27T08:00:00Z")
    sc = json.loads(scb.decode())
    wrong = Ed25519PrivateKey.generate().private_bytes_raw()
    item_key = generate_ulid()
    sc["item_key"] = item_key
    scb2 = json.dumps(sc, sort_keys=True).encode()
    e.drive.seed_file(e.inbox, f"{item_key}.sidecar.json", scb2)
    e.drive.seed_file(e.inbox, f"{item_key}.sig",
                      json.dumps(_sign(scb2, wrong, e.key_id)).encode())
    e.drive.seed_file(e.inbox, f"{item_key}.raw", p.read_bytes())
    report, _, _ = _run_env(e, tmp_path)
    assert report.aborted_at is None
    assert report.counts.get("rejected") == 1
    assert sorted(f.name for f in e.drive.list_children(e.inbox)) == sorted(
        [f"{item_key}.sidecar.json", f"{item_key}.sig", f"{item_key}.raw"])


def test_log_discipline_no_content(tmp_path: Path, bundle: dict, capsys) -> None:
    """log 只輸出 id、計數與耗時：raw 內文（標題）不得出現在輸出。"""
    e = make_env(tmp_path, bundle, marker=True)
    report, _, _ = _run_env(e, tmp_path)
    assert report.aborted_at is None
    out = capsys.readouterr().out
    assert SECRET_MARKER not in out
    assert report.run_id in out

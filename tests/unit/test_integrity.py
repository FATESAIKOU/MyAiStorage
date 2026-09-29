"""3.2 完整性機制的驗收測試（測試方撰寫，未看實作，只依文件與簽名）。

對照：
- docs/impl/group3-modules.md 第 3 節（介面）與第 8.2 節（案例表）
- design D2 的 13 步、ADR 0008（釘選值是唯一信任來源、讀不到就中止不移動）
- review-g3c.md（H1〜H4、M1〜M9）與 review-g3c-recheck.md（R1〜R3）

全部直接 import，不 skip；只用 FakeDrive／FakeAnnexGit／MemoryPinStore 與本機 file:// pin repo。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock
from aistorage.drive.fake import FakeDrive
from aistorage.errors import MismatchError, ReadError, WriteError
from aistorage.integrity.gc import (
    collect_removed_bundles,
    gc_removed,
    purge_quarantine,
)
from aistorage.integrity.pin import (
    GitPinStore,
    MemoryPinStore,
    PinPending,
    PinState,
)
from aistorage.integrity.settle import (
    RepoListing,
    SettleOutcome,
    check_manifest_continuity,
    settle,
)
from aistorage.integrity.sweep import (
    Disposition,
    PrefixLevel,
    SweepDecision,
    apply_sweep,
    check_parents,
    plan_readview_sweep,
    plan_sweep,
    resolve_content_checks,
    resolve_manifest_evidence,
    run_settle_and_sweep,
)
from aistorage.integrity.verify import (
    precheck,
    verify_after_push,
    verify_clone,
)

UUID = "11111111-2222-3333-4444-555555555555"
MANIFEST_NAME = f"GITMANIFEST--{UUID}"
BAK_NAME = f"GITMANIFEST--{UUID}.bak"
CLOCK_T0 = "2026-09-27T10:00:00Z"


# ---------------------------------------------------------------------------
# 夾具：可重放的真實 bundle（git 製作，refs 帶 git-remote-annex 命名空間前綴）
# ---------------------------------------------------------------------------

def _git(env: dict[str, str], *args: str, cwd: Path) -> str:
    r = subprocess.run(
        ["git", *args], cwd=cwd, env=env,
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict:
    d = tmp_path_factory.mktemp("bundles")
    repo = d / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    _git(env, "init", "-b", "main", "-q", ".", cwd=repo)
    (repo / "f.txt").write_text("one\n")
    _git(env, "add", "f.txt", cwd=repo)
    _git(env, "commit", "-qm", "c1", cwd=repo)
    c1 = _git(env, "rev-parse", "HEAD", cwd=repo)
    _git(env, "update-ref",
         f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main", c1, cwd=repo)
    _git(env, "bundle", "create", str(d / "b1.bundle"),
         f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main", cwd=repo)
    (repo / "f.txt").write_text("two\n")
    _git(env, "commit", "-qam", "c2", cwd=repo)
    c2 = _git(env, "rev-parse", "HEAD", cwd=repo)
    _git(env, "update-ref",
         f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main", c2, cwd=repo)
    _git(env, "bundle", "create", str(d / "b2.bundle"),
         f"refs/namespaces/git-remote-annex/{UUID}/refs/heads/main", cwd=repo)

    out: dict = {"uuid": UUID, "c1": c1, "c2": c2}
    for key, src in (("b1", d / "b1.bundle"), ("b2", d / "b2.bundle")):
        raw = src.read_bytes()
        name = f"GITBUNDLE-s{len(raw)}--{UUID}-{hashlib.sha256(raw).hexdigest()}"
        out[key] = name
        out[key + "_bytes"] = raw
    out["m1"] = (out["b1"] + "\n").encode()
    out["m2"] = (out["b1"] + "\n" + out["b2"] + "\n").encode()
    out["m1sha"] = hashlib.sha256(out["m1"]).hexdigest()
    out["m2sha"] = hashlib.sha256(out["m2"]).hexdigest()
    return out


def _state(bx: dict, *, active: tuple[str, ...] | None = None,
           refs: dict[str, str] | None = None,
           removed: frozenset[str] = frozenset(),
           keys: frozenset[str] = frozenset(),
           manifest_sha: str | None = None,
           prev: str | None = None) -> PinState:
    act = active if active is not None else (bx["b1"],)
    return PinState(
        repo="agora", repo_uuid=UUID,
        refs=refs if refs is not None else {"refs/heads/main": bx["c1"]},
        manifest_sha256=manifest_sha or hashlib.sha256(
            ("\n".join(act) + "\n" + "".join(f"-{r}\n" for r in sorted(removed))).encode()
        ).hexdigest(),
        prev_manifest_sha256=prev,
        active_bundles=act,
        removed_bundles=removed,
        annex_keys=keys,
        promoted_at="2026-09-27T00:00:00Z",
        run_id="run-1",
    )


def _pending(bx: dict, *, base: str | None = None,
             refs: dict[str, str] | None = None) -> PinPending:
    return PinPending(
        repo="agora",
        base_manifest_sha256=base or bx["m1sha"],
        refs=refs if refs is not None else {"refs/heads/main": bx["c2"]},
        annex_keys=frozenset(),
        written_at="2026-09-27T01:00:00Z",
        run_id="run-2",
    )


def _seed_prefix(drive: FakeDrive, bx: dict, manifest: bytes | None,
                 bundle_keys: list[str],
                 extra: list[tuple[str, bytes]] | None = None) -> tuple[str, RepoListing]:
    """在前綴資料夾佈置 manifest＋bundle，回傳 (prefix_id, listing)。"""
    prefix = drive.seed_folder("prefix")
    if manifest is not None:
        drive.seed_file(prefix, MANIFEST_NAME, manifest)
    for key in bundle_keys:
        drive.seed_file(prefix, bx[key], bx[key + "_bytes"])
    for name, content in extra or []:
        drive.seed_file(prefix, name, content)
    files = tuple(drive.list_children(prefix))
    return prefix, RepoListing(prefix_folder_id=prefix, files=files, subfolders=tuple())


def _by_name(listing: RepoListing, name: str):
    return [f for f in listing.files if f.name == name]


def _all_ids_under(drive: FakeDrive, folder_id: str) -> list[str]:
    """遞迴收集資料夾下所有檔案 id（不依賴 get 的例外型別）。"""
    out: list[str] = []
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out += _all_ids_under(drive, child.id)
        else:
            out.append(child.id)
    return out


# ---------------------------------------------------------------------------
# settle：決策表（§8.2）
# ---------------------------------------------------------------------------

def test_settle_no_pending_reads_nothing(bundles: dict, tmp_path: Path):
    """無待定 → NO_PENDING，且不做任何讀取（第 1 個讀取就注入 ReadError 仍通過）。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
    before = drive.snapshot()
    drive.inject_nth_read(1)
    outcome, ns = settle(st, None, listing, drive, workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.NO_PENDING
    assert ns == st
    assert drive.snapshot() == before


def test_settle_promoted(bundles: dict, tmp_path: Path):
    """遠端＝待定 → PROMOTED：refs、manifest、prev、active 轉正。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
    outcome, ns = settle(st, _pending(bundles), listing, drive,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.PROMOTED
    assert ns.refs == {"refs/heads/main": bundles["c2"]}
    assert ns.manifest_sha256 == bundles["m2sha"]
    assert ns.prev_manifest_sha256 == bundles["m1sha"]
    assert ns.active_bundles == (bundles["b1"], bundles["b2"])


def test_settle_dropped(bundles: dict, tmp_path: Path):
    """遠端＝正式 → DROPPED，狀態不變。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
    outcome, ns = settle(st, _pending(bundles), listing, drive,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.DROPPED
    assert ns == st


def test_settle_neither_aborts(bundles: dict, tmp_path: Path):
    """遠端既非待定亦非正式 → MismatchError 中止。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
    bad_pending = _pending(bundles, refs={"refs/heads/main": "0" * 40})
    with pytest.raises(MismatchError):
        settle(st, bad_pending, listing, drive, workdir=tmp_path, clock=FixedClock())


def test_settle_bak_recovery(bundles: dict, tmp_path: Path):
    """只剩 .bak 且等於正式 → BAK_RECOVERY，狀態不變。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, BAK_NAME, bundles["m1"])
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    outcome, ns = settle(st, _pending(bundles), listing, drive,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.BAK_RECOVERY
    assert ns == st


class _StaleListingDrive:
    """包住 FakeDrive：前 `stale_calls` 次 list_children 回傳「push 還沒完成」的舊畫面。

    這是 impl1 現場的形狀：Drive 的列舉會落後寫入。某一輪已經 push 成功、釘選值還
    沒轉正（pending 留著），另一輪在 push 落實**之前**就列舉了前綴，於是把
    「遠端已經往前」看成「遠端沒動」→ 丟掉 pending → 之後遠端再也沒有人負責，
    下一輪的清掃就把新的 manifest 當注入物搬走，真本從此無法 clone。
    """

    def __init__(self, inner: FakeDrive, *, folder_id: str, stale_calls: int) -> None:
        self._inner = inner
        self._folder_id = folder_id
        self._stale_calls = stale_calls
        self._calls = 0
        self._stale_snapshot: list = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_children(self, folder_id):
        if folder_id != self._folder_id:
            return self._inner.list_children(folder_id)
        self._calls += 1
        if self._calls <= self._stale_calls and self._stale_snapshot:
            return list(self._stale_snapshot)
        return self._inner.list_children(folder_id)


def test_settle_promotes_when_the_push_lands_while_it_was_deciding(bundles: dict, tmp_path: Path):
    """push 在 settle 判斷的期間才落實 → 必須 PROMOTED，不能 DROPPED 掉 pending。

    這就是 impl1 現場的形狀：判定前的那份畫面裡，遠端看起來還是正式值
    （DROPPED 的條件成立），但判定真正做完之前，別的輪次已經把 push 落實了。
    舊行為會在這裡丟掉 pending，遠端從此領先釘選值而沒有人負責。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    # 進場時的畫面：遠端還停在正式值
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    stale = _StaleListingDrive(drive, folder_id=prefix, stale_calls=1)
    stale._stale_snapshot = list(drive.list_children(prefix))
    stale_listing = RepoListing(
        prefix_folder_id=prefix, files=tuple(stale._stale_snapshot), subfolders=tuple()
    )

    # 幾乎是同一瞬間，push 落實：新的 manifest（b1+b2）與新 bundle 出現
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])

    outcome, ns = settle(st, _pending(bundles), stale_listing, stale,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.PROMOTED, "push 已落實卻被判成 DROPPED＝丟掉了 pending"
    assert ns.refs == _pending(bundles).refs
    assert ns.manifest_sha256 == bundles["m2sha"]


def test_settle_promotes_when_the_manifest_appears_mid_settle(bundles: dict, tmp_path: Path):
    """判定用的畫面裡連 manifest 都還沒有，落實之後才有 → 同樣不能中止或丟 pending。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    stale = _StaleListingDrive(drive, folder_id=prefix, stale_calls=1)
    stale._stale_snapshot = list(drive.list_children(prefix))
    stale_listing = RepoListing(
        prefix_folder_id=prefix, files=tuple(stale._stale_snapshot), subfolders=tuple()
    )

    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])

    outcome, ns = settle(st, _pending(bundles), stale_listing, stale,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.PROMOTED
    assert ns.manifest_sha256 == bundles["m2sha"]


def test_settle_still_drops_when_the_remote_really_has_not_moved(bundles: dict, tmp_path: Path):
    """重查之後確認遠端真的還在正式值 → 仍然 DROPPED（不是一律不丟）。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
    outcome, ns = settle(st, _pending(bundles), listing, drive,
                         workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.DROPPED
    assert ns == st


def test_settle_two_matching_candidates_abort(bundles: dict, tmp_path: Path):
    """兩個同名候選內容不同但都能重放相符 → 中止（M2：不信任任一個）。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"] + b"\n")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    assert len(_by_name(listing, MANIFEST_NAME)) == 2
    with pytest.raises(MismatchError):
        settle(st, _pending(bundles), listing, drive, workdir=tmp_path, clock=FixedClock())


def test_settle_read_error_propagates_and_changes_nothing(bundles: dict, tmp_path: Path):
    """任何 ReadError → 原樣往上拋（嚴格 ReadError），FakeDrive 快照不變。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
    before = drive.snapshot()
    drive.inject_nth_read(1)
    with pytest.raises(ReadError):
        settle(st, _pending(bundles), listing, drive, workdir=tmp_path, clock=FixedClock())
    assert drive.snapshot() == before


def test_settle_base_manifest_mismatch_aborts(bundles: dict, tmp_path: Path):
    """M3：pending.base_manifest_sha256 與正式 manifest 不符 → 中止。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
    with pytest.raises(MismatchError):
        settle(st, _pending(bundles, base="f" * 64), listing, drive,
               workdir=tmp_path, clock=FixedClock())


def test_settle_promoted_continuity_violation_aborts(bundles: dict, tmp_path: Path):
    """M3：refs 相符但舊 active 憑空消失 → PROMOTED 也不轉正，中止。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    m_bad = (bundles["b2"] + "\n").encode()  # b1 既非 active 亦未列 removed
    drive = FakeDrive()
    _, listing = _seed_prefix(drive, bundles, m_bad, ["b1", "b2"])
    with pytest.raises(MismatchError):
        settle(st, _pending(bundles), listing, drive, workdir=tmp_path, clock=FixedClock())


# ---------------------------------------------------------------------------
# check_manifest_continuity（M3／M5 純函式）
# ---------------------------------------------------------------------------

def test_manifest_continuity_ok_and_violations(bundles: dict):
    from aistorage.annex.manifest import parse_manifest
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    check_manifest_continuity(st, parse_manifest(bundles["m1"], repo_uuid=UUID))
    check_manifest_continuity(st, parse_manifest(bundles["m2"], repo_uuid=UUID))

    # removed 縮水
    st2 = _state(bundles, manifest_sha=bundles["m1sha"],
                 removed=frozenset({"GITBUNDLE-s9--" + UUID + "-" + "a" * 64}))
    with pytest.raises(MismatchError):
        check_manifest_continuity(st2, parse_manifest(bundles["m1"], repo_uuid=UUID))

    # 新增的 removed 不是舊 active
    bad = (bundles["b1"] + "\n-GITBUNDLE-s9--" + UUID + "-" + "b" * 64 + "\n").encode()
    with pytest.raises(MismatchError):
        check_manifest_continuity(st, parse_manifest(bad, repo_uuid=UUID))

    # 舊 active 憑空消失
    vanished = (bundles["b2"] + "\n").encode()
    with pytest.raises(MismatchError):
        check_manifest_continuity(st, parse_manifest(vanished, repo_uuid=UUID))


# ---------------------------------------------------------------------------
# sweep：plan 規則（§8.2 每條一個案例）
# ---------------------------------------------------------------------------

def _sweep_state(bundles: dict, **kw) -> PinState:
    keys = kw.pop("keys", frozenset())
    return _state(bundles, manifest_sha=bundles["m1sha"], keys=keys, **kw)


def test_plan_sweep_manifest_roles(bundles: dict):
    """主 manifest 只 KEEP 正式值；同名第二份、上一版冒充（非 .bak 名）→ QUARANTINE；
    .bak 只接受正式值或上一版；其他名稱 → QUARANTINE。

    impl1：內容雜湊**既不是正式值也不是上一版**的 manifest 不再直接隔離——那正是
    「釘選值還沒轉正、push 已經完成」的那一份，隔離它等於消滅真本（`git clone`
    之後就找不到 manifest，而且沒有任何一輪能自己回來）。它被標成
    NEED_MANIFEST_CHECK，讀完內容才決定：解析不出來 → 隔離。
    """
    st = _sweep_state(bundles, prev=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    good = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    dup = drive.seed_file(prefix, MANIFEST_NAME, b"junk-content")
    prev_impostor = drive.seed_file(prefix, f"{MANIFEST_NAME}.old", bundles["m1"])
    bak = drive.seed_file(prefix, BAK_NAME, bundles["m1"])
    bak_bad = drive.seed_file(prefix, f"{MANIFEST_NAME}.bak2", b"junk")
    other = drive.seed_file(prefix, "notes.txt", b"hello")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[good] == Disposition.KEEP
    assert decs[dup] == Disposition.NEED_MANIFEST_CHECK
    assert decs[prev_impostor] == Disposition.QUARANTINE
    assert decs[bak] == Disposition.KEEP
    assert decs[bak_bad] == Disposition.QUARANTINE
    assert decs[other] == Disposition.QUARANTINE

    # 讀內容：junk-content 解析不出來 → 有證據是注入物 → 隔離
    resolved = resolve_manifest_evidence(
        plan_sweep(listing, st, repo_uuid=UUID), drive, {}, st,
        repo_uuid=UUID, listing=listing, prefix_folder_id=prefix,
    )
    decs2 = {d.file.id: d.disposition for d in resolved}
    assert decs2[dup] == Disposition.QUARANTINE
    assert decs2[good] == Disposition.KEEP


def test_plan_sweep_prev_manifest_with_normal_name_quarantined(bundles: dict):
    """上一版 manifest 用正常主檔名冒充（雜湊＝prev 而非正式值）→ QUARANTINE。"""
    m_prev = (bundles["b1"] + "\n").encode()
    prev_sha = hashlib.sha256(m_prev).hexdigest()
    assert prev_sha == bundles["m1sha"]
    m2 = bundles["m2"]
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    cur = drive.seed_file(prefix, MANIFEST_NAME, m2)
    impostor = drive.seed_file(prefix, MANIFEST_NAME, m_prev)  # 上一版內容、正常檔名
    st2 = _state(bundles, manifest_sha=bundles["m2sha"], prev=prev_sha,
                 active=(bundles["b1"], bundles["b2"]))
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st2, repo_uuid=UUID)}
    assert decs[cur] == Disposition.KEEP
    assert decs[impostor] == Disposition.QUARANTINE


def test_plan_sweep_bundle_rules(bundles: dict):
    """active＋雜湊＋size 相符 → KEEP（同內容重複只留一個）；
    removed → GC；其他 → QUARANTINE；active 同名雜湊不符 → QUARANTINE。"""
    st = _sweep_state(bundles, removed=frozenset({bundles["b2"]}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    keep = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    keep_dup = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    gc_file = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    unknown = drive.seed_file(prefix, bundles["b2"].replace("s509", "s510"), b"x" * 10)
    tampered = drive.seed_file(prefix, bundles["b1"], b"tampered-by-resident!!")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert [decs[keep], decs[keep_dup]].count(Disposition.KEEP) == 1
    assert decs[gc_file] == Disposition.GC
    assert decs[unknown] == Disposition.QUARANTINE
    assert decs[tampered] == Disposition.QUARANTINE


def test_plan_sweep_annex_objects(bundles: dict):
    """key ∈ 釘選值且雜湊＋size 相符 → KEEP。

    impl1：不在釘選值裡的物件分兩種——內容與 key 自稱值相符的（多半是「剛 push
    上去、還沒轉正」）→ HOLD；不相符的（key 內嵌的雜湊／大小與內容對不上，
    證據確鑿是注入物）→ QUARANTINE。
    """
    content = b"0123456789a"  # 11 bytes
    key = f"SHA256E-s11--{hashlib.sha256(content).hexdigest()}"
    st = _sweep_state(bundles, keys=frozenset({key}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    keep = drive.seed_file(prefix, key, content)
    orphan = drive.seed_file(prefix, f"SHA256E-s3--{hashlib.sha256(b'zzz').hexdigest()}", b"zzz")
    sizemismatch = drive.seed_file(prefix, key, b"short")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[keep] == Disposition.KEEP
    assert decs[orphan] == Disposition.HOLD
    assert decs[sizemismatch] == Disposition.QUARANTINE


# ---------------------------------------------------------------------------
# impl1：釘選值落後遠端時，清掃不得消滅真本
# ---------------------------------------------------------------------------

def test_plan_sweep_keeps_a_manifest_the_pin_has_not_caught_up_with(bundles: dict):
    """push 成功、驗證未完成 → 釘選值還在上一版。

    這種狀態下遠端有的是**真的** manifest（內容合法、它列的 bundle 都在前綴裡）
    與一個**真的**新 bundle。舊規則會把兩者都當注入物搬走，於是 `git clone`
    再也找不到 manifest，真本被提交流程自己消滅，而且沒有任何一輪能自己回來。
    證明它是真的之後只能 HOLD（留在原地），不能 QUARANTINE。
    """
    st = _sweep_state(bundles)  # 釘選值只有 b1
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    official = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    keep_bundle = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    new_bundle = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    ahead = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])  # b1+b2，釘選值還沒跟上

    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    first = plan_sweep(listing, st, repo_uuid=UUID)
    by_id = {d.file.id: d for d in first}
    assert by_id[official].disposition == Disposition.KEEP
    assert by_id[ahead].disposition == Disposition.NEED_MANIFEST_CHECK
    assert by_id[new_bundle].disposition == Disposition.HOLD
    assert by_id[keep_bundle].disposition == Disposition.KEEP

    second = resolve_manifest_evidence(first, drive, {}, st, repo_uuid=UUID,
                                       listing=listing, prefix_folder_id=prefix)
    by_id2 = {d.file.id: d for d in second}
    assert by_id2[ahead].disposition == Disposition.HOLD
    assert by_id2[new_bundle].disposition == Disposition.HOLD
    assert by_id2[official].disposition == Disposition.KEEP


def test_resolve_manifest_evidence_quarantines_manifest_with_missing_bundles(
    bundles: dict,
):
    """manifest 本身合法，但它列的 bundle 不在前綴裡 → 有證據是注入物 → 隔離。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    # m2 宣告 b1、b2，但前綴裡只有 b1
    impostor = drive.seed_file(prefix, f"{MANIFEST_NAME}.bak", bundles["m2"])
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])

    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    resolved = resolve_manifest_evidence(
        plan_sweep(listing, st, repo_uuid=UUID), drive, {}, st,
        repo_uuid=UUID, listing=listing, prefix_folder_id=prefix,
    )
    by_id = {d.file.id: d for d in resolved}
    assert by_id[impostor].disposition == Disposition.QUARANTINE


def test_plan_sweep_quarantines_bundle_whose_name_lies(bundles: dict):
    """自我一致但不在釘選值裡的 bundle → HOLD；檔名宣告與內容不符 → QUARANTINE。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    honest = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    liar = drive.seed_file(prefix, bundles["b2"], b"payload-from-a-resident")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[honest] == Disposition.HOLD
    assert decs[liar] == Disposition.QUARANTINE


def test_apply_sweep_never_moves_held_files(bundles: dict, tmp_path: Path):
    """HOLD 只是「不動」，不是「漏處理」：apply_sweep 不會搬它。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    held = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decisions = plan_sweep(listing, st, repo_uuid=UUID)
    moved = apply_sweep(decisions, drive, quarantine_folder_id=quarantine,
                        clock=FixedClock(), prefix_folder_id=prefix)
    assert moved == 0
    assert [f.name for f in drive.list_children(prefix)] == [bundles["b2"]]


def test_plan_sweep_missing_checksum_needs_content_check(bundles: dict):
    """sha256 缺少（或 size 缺少）→ NEED_CONTENT_CHECK。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    no_sha = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.set_checksum(no_sha, None)
    no_size_name = drive.seed_file(prefix, "mystery.bin", b"data")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[no_sha] == Disposition.NEED_CONTENT_CHECK
    assert decs[no_size_name] in (Disposition.NEED_CONTENT_CHECK, Disposition.QUARANTINE)


def test_plan_sweep_subfolders_quarantined(bundles: dict):
    """子資料夾（整個子樹）→ QUARANTINE。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    sub = drive.seed_folder("nested", parent=prefix)
    listing = RepoListing(prefix_folder_id=prefix, files=tuple(),
                          subfolders=tuple(drive.list_children(prefix)))
    assert sub in [f.id for f in listing.subfolders]
    decs = plan_sweep(listing, st, repo_uuid=UUID)
    by_id = {d.file.id: d.disposition for d in decs}
    assert by_id[sub] == Disposition.QUARANTINE


def test_resolve_content_checks_replaces_and_replans(bundles: dict, tmp_path: Path):
    """M8：缺少 sha 的檔下載驗證後替換回完整 listing 重判：相符 → KEEP，不符 → QUARANTINE。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    fid_ok = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.set_checksum(fid_ok, None)
    fid_bad = drive.seed_file(prefix, bundles["b2"], b"forged-content!!")
    drive.set_checksum(fid_bad, None)
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    first = plan_sweep(listing, st, repo_uuid=UUID)
    assert {d.disposition for d in first} == {Disposition.NEED_CONTENT_CHECK}
    second = resolve_content_checks(first, drive, {}, st, repo_uuid=UUID,
                                    listing=listing, workdir=tmp_path)
    by_id = {d.file.id: d.disposition for d in second}
    assert by_id[fid_ok] == Disposition.KEEP
    assert by_id[fid_bad] == Disposition.QUARANTINE


def test_resolve_content_checks_rewrite_is_redownloaded(bundles: dict, tmp_path: Path):
    """R1：同一個 file id 被原地改寫（size 變化）→ 必須重新下載，不能用舊雜湊判定。"""
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    fid = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.set_checksum(fid, None)
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    cache: dict = {}
    first = plan_sweep(listing, st, repo_uuid=UUID)
    resolved = resolve_content_checks(first, drive, cache, st, repo_uuid=UUID,
                                      listing=listing, workdir=tmp_path)
    assert {d.disposition for d in resolved} == {Disposition.KEEP}

    drive.update_content(fid, b"completely-different-and-longer-content")
    listing2 = RepoListing(prefix_folder_id=prefix,
                           files=tuple(drive.list_children(prefix)), subfolders=tuple())
    first2 = plan_sweep(listing2, st, repo_uuid=UUID)
    resolved2 = resolve_content_checks(first2, drive, cache, st, repo_uuid=UUID,
                                       listing=listing2, workdir=tmp_path)
    assert {d.disposition for d in resolved2} == {Disposition.QUARANTINE}


# ---------------------------------------------------------------------------
# check_parents／apply_sweep（M7、H3、dry-run）
# ---------------------------------------------------------------------------

def test_check_parents_levels():
    drive = FakeDrive()
    root = drive.seed_folder("root")
    expected = drive.seed_folder("repo", parent=root)
    extra = drive.seed_folder("repo", parent=root)
    levels = [PrefixLevel(parent_id=root, name="repo", expected_id=expected)]
    decs = check_parents(levels, drive)
    assert len(decs) == 1
    assert decs[0].file.id == extra
    assert decs[0].disposition == Disposition.QUARANTINE
    assert decs[0].from_parent == root

    with pytest.raises(MismatchError):
        check_parents([PrefixLevel(parent_id=root, name="repo", expected_id="no-such-id")], drive)


def test_apply_sweep_moves_to_date_folder_and_dry_run(bundles: dict, tmp_path: Path):
    """隔離進 quarantine/<YYYY-MM-DD>/；dry-run 不搬任何檔。"""
    clock = FixedClock(CLOCK_T0)
    drive = FakeDrive(clock=clock)
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    fid = drive.seed_file(prefix, "evil.txt", b"injected")
    before = drive.snapshot()

    apply_sweep(
        [SweepDecision(
            file=drive.get(fid), disposition=Disposition.QUARANTINE,
            reason="test", from_parent=prefix)],
        drive, quarantine_folder_id=quarantine, clock=clock,
        prefix_folder_id=prefix, dry_run=True)
    assert drive.snapshot() == before

    moved = apply_sweep(
        [SweepDecision(file=drive.get(fid), disposition=Disposition.QUARANTINE,
                       reason="test", from_parent=prefix)],
        drive, quarantine_folder_id=quarantine, clock=clock, prefix_folder_id=prefix)
    assert moved == 1
    date_dir = drive.find_by_name(quarantine, "2026-09-27")
    assert len(date_dir) == 1
    assert drive.get(fid).parents == (date_dir[0].id,)


def test_apply_sweep_uses_each_decision_from_parent():
    """M7：上層同名資料夾用自己的 from_parent 搬移（非前綴資料夾）。"""
    drive = FakeDrive()
    root = drive.seed_folder("root")
    prefix = drive.seed_folder("repo", parent=root)
    quarantine = drive.seed_folder("quarantine")
    extra = drive.seed_folder("repo", parent=root)
    decs = check_parents(
        [PrefixLevel(parent_id=root, name="repo", expected_id=prefix)], drive)
    assert decs and decs[0].from_parent == root
    moved = apply_sweep(decs, drive, quarantine_folder_id=quarantine,
                        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix)
    assert moved == 1
    assert root not in drive.get(extra).parents


def test_plan_readview_sweep_stub(bundles: dict):
    """讀取視圖清掃 stub：可信 file id 保留，其他隔離。"""
    drive = FakeDrive()
    rv = drive.seed_folder("readview")
    trusted = drive.seed_file(rv, "index.json", b"{}")
    stranger = drive.seed_file(rv, "evil.json", b"{}")
    listing = RepoListing(prefix_folder_id=rv,
                          files=tuple(drive.list_children(rv)), subfolders=tuple())
    decs = {d.file.id: d.disposition
            for d in plan_readview_sweep(listing, {trusted}, readview_folder_id=rv)}
    assert decs[trusted] == Disposition.KEEP
    assert decs[stranger] == Disposition.QUARANTINE


# ---------------------------------------------------------------------------
# verify_clone／precheck（§8.2）
# ---------------------------------------------------------------------------

def test_verify_clone_ok_and_mismatches(bundles: dict):
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    git = FakeAnnexGit(refs={"refs/heads/main": bundles["c1"]})
    verify_clone(git, st, drive=drive, prefix_folder_id=prefix)

    with pytest.raises(MismatchError):
        verify_clone(FakeAnnexGit(refs={"refs/heads/main": bundles["c2"]}),
                     st, drive=drive, prefix_folder_id=prefix)
    with pytest.raises(MismatchError):
        verify_clone(FakeAnnexGit(refs={"refs/heads/main": bundles["c1"],
                                        "refs/heads/evil": bundles["c1"]}),
                     st, drive=drive, prefix_folder_id=prefix)

    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    with pytest.raises(MismatchError):
        verify_clone(git, st, drive=drive, prefix_folder_id=prefix)


def test_verify_clone_missing_checksum(bundles: dict):
    """sha256 缺少 → 明確指出 Drive 尚未提供 checksum 的 MismatchError。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    fid = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    drive.set_checksum(fid, None)
    git = FakeAnnexGit(refs={"refs/heads/main": bundles["c1"]})
    with pytest.raises(MismatchError, match="checksum"):
        verify_clone(git, st, drive=drive, prefix_folder_id=prefix)


def test_precheck_rules(bundles: dict):
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    precheck(drive, prefix, MANIFEST_NAME, st)

    drive.seed_file(prefix, MANIFEST_NAME, b"other")
    with pytest.raises(MismatchError):
        precheck(drive, prefix, MANIFEST_NAME, st)


# ---------------------------------------------------------------------------
# verify_after_push（§8.2：active 比對、removed 連續性、ref、時間）
# ---------------------------------------------------------------------------

def _push_fixture(bundles: dict, *, b2_created: str,
                  manifest: bytes | None = None,
                  extra_refs: dict[str, str] | None = None,
                  corrupt_b2: bool = False,
                  remove_manifest_first: bool = True):
    """佈置 push 後遠端：M2＋B1＋B2。回傳 dict(lising_before, drive, prefix, git... )。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    listing_before = RepoListing(prefix_folder_id=prefix,
                                 files=tuple(drive.list_children(prefix)),
                                 subfolders=tuple())
    if remove_manifest_first:
        for f in drive.find_by_name(prefix, MANIFEST_NAME):
            drive.delete_permanently(f.id)
    drive.seed_file(prefix, MANIFEST_NAME, manifest or bundles["m2"])
    b2content = b"corrupted!!" if corrupt_b2 else bundles["b2_bytes"]
    drive.seed_file(prefix, bundles["b2"], b2content, created_time=b2_created)
    refs = {"refs/heads/main": bundles["c2"], **(extra_refs or {})}
    return st, drive, prefix, listing_before, FakeAnnexGit(refs=refs)


def test_verify_after_push_ok(bundles: dict, tmp_path: Path):
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T02:00:00Z")
    pv = verify_after_push(git, drive, listing_before, st,
                           {"refs/heads/main": bundles["c2"]},
                           "2026-09-27T01:00:00Z", workdir=tmp_path)
    assert pv.new_manifest_sha256 == bundles["m2sha"]
    assert pv.active == (bundles["b1"], bundles["b2"])
    assert pv.removed == frozenset()


def test_verify_after_push_new_bundle_too_old(bundles: dict, tmp_path: Path):
    """新增 bundle 的建立時間早於 push 開始 → 中止。"""
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T00:00:00Z")
    with pytest.raises(MismatchError):
        verify_after_push(git, drive, listing_before, st,
                          {"refs/heads/main": bundles["c2"]},
                          "2026-09-27T01:00:00Z", workdir=tmp_path)


def test_verify_after_push_removed_shrunk(bundles: dict, tmp_path: Path):
    """removed 少了舊項目 → 中止（連續性規則 1）。"""
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T02:00:00Z")
    st_old_removed = _state(
        bundles, manifest_sha=bundles["m1sha"],
        removed=frozenset({"GITBUNDLE-s9--" + UUID + "-" + "a" * 64}))
    with pytest.raises(MismatchError):
        verify_after_push(git, drive, listing_before, st_old_removed,
                          {"refs/heads/main": bundles["c2"]},
                          "2026-09-27T01:00:00Z", workdir=tmp_path)


def test_verify_after_push_extra_ref(bundles: dict, tmp_path: Path):
    """ls-remote 多一個 ref → 中止。"""
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T02:00:00Z",
        extra_refs={"refs/heads/evil": bundles["c1"]})
    with pytest.raises(MismatchError):
        verify_after_push(git, drive, listing_before, st,
                          {"refs/heads/main": bundles["c2"]},
                          "2026-09-27T01:00:00Z", workdir=tmp_path)


def test_verify_after_push_old_active_vanished(bundles: dict, tmp_path: Path):
    """舊 active 既不在新 active 也不在 removed → 中止（M5）。"""
    m_bad = (bundles["b2"] + "\n").encode()
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T02:00:00Z", manifest=m_bad)
    with pytest.raises(MismatchError):
        verify_after_push(git, drive, listing_before, st,
                          {"refs/heads/main": bundles["c2"]},
                          "2026-09-27T01:00:00Z", workdir=tmp_path)


def test_verify_after_push_corrupt_new_bundle(bundles: dict, tmp_path: Path):
    """同名但雜湊不符的 bundle 不得被選來重放 → 中止（M5 同名篩選）。"""
    st, drive, prefix, listing_before, git = _push_fixture(
        bundles, b2_created="2026-09-27T02:00:00Z", corrupt_b2=True)
    with pytest.raises(MismatchError):
        verify_after_push(git, drive, listing_before, st,
                          {"refs/heads/main": bundles["c2"]},
                          "2026-09-27T01:00:00Z", workdir=tmp_path)


# ---------------------------------------------------------------------------
# pin：MemoryPinStore 狀態機、GitPinStore（file://）、non-ff、URL 白名單（R2）
# ---------------------------------------------------------------------------

def test_memory_pin_store_lifecycle(bundles: dict):
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    pend = _pending(bundles)
    pins = MemoryPinStore()
    with pytest.raises(ReadError):
        pins.load("agora")
    pins.write_pending(pend)
    with pytest.raises(ReadError):
        pins.load("agora")
    pins.promote(st)
    loaded, no_pending = pins.load("agora")
    assert loaded == st and no_pending is None
    pins.write_pending(pend)
    _, still_pending = pins.load("agora")
    assert still_pending == pend
    pins.drop_pending("agora")
    _, gone = pins.load("agora")
    assert gone is None


def _bare_repo(path: Path) -> str:
    subprocess.run(["git", "init", "--bare", "-b", "main", str(path)],
                   check=True, capture_output=True)
    return f"file://{path}"


def test_git_pin_store_roundtrip_without_key(bundles: dict, tmp_path: Path):
    """本機路徑／file:// 不需要 deploy key 即可操作。"""
    url = _bare_repo(tmp_path / "pin.git")
    pins = GitPinStore(url, tmp_path / "wd")
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    pend = _pending(bundles)
    pins.write_pending(pend)
    with pytest.raises(ReadError):
        pins.load("agora")
    pins.promote(st)
    loaded, no_pending = pins.load("agora")
    assert loaded == st and no_pending is None
    pins.write_pending(pend)
    _, still_pending = pins.load("agora")
    assert still_pending == pend
    pins.drop_pending("agora")
    _, gone = pins.load("agora")
    assert gone is None


def _chmod_tree(path: Path, dir_mode: int, file_mode: int) -> None:
    for root, dirs, files in os.walk(path):
        os.chmod(root, dir_mode)
        for d in dirs:
            os.chmod(Path(root) / d, dir_mode)
        for f in files:
            os.chmod(Path(root) / f, file_mode)


def test_git_pin_store_push_failure_aborts_and_keeps_remote(bundles: dict, tmp_path: Path):
    """push 失敗（此處以唯讀遠端模擬 non-ff／斷線等寫入失敗）→ raise WriteError，
    且遠端正式值不被覆寫。"""
    bare = tmp_path / "pin.git"
    url = _bare_repo(bare)
    a = GitPinStore(url, tmp_path / "wd-a")
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    a.write_pending(_pending(bundles))
    a.promote(st)
    _chmod_tree(bare, 0o555, 0o444)
    try:
        b = GitPinStore(url, tmp_path / "wd-b")
        with pytest.raises(WriteError):
            b.write_pending(_pending(bundles))
            b.promote(_state(bundles, manifest_sha=bundles["m2sha"],
                             active=(bundles["b1"], bundles["b2"])))
    finally:
        _chmod_tree(bare, 0o755, 0o644)
    fresh = GitPinStore(url, tmp_path / "wd-c")
    loaded, _ = fresh.load("agora")
    assert loaded == st


def test_git_pin_store_rejects_non_whitelisted_url(tmp_path: Path):
    """R2：scp 別名等非白名單 URL 形式一律拒絕（避免退回個人 SSH 身分）。"""
    with pytest.raises(ValueError):
        GitPinStore("github-pin:owner/repo", tmp_path / "wd")
    with pytest.raises(ValueError):
        GitPinStore("https://github.com/owner/repo.git", tmp_path / "wd")


def test_git_pin_store_ssh_url_requires_key(tmp_path: Path):
    """R2：SSH URL 沒有 deploy key 時直接失敗，不得退回個人身分。"""
    with pytest.raises(ValueError):
        GitPinStore("git@github.com:owner/repo.git", tmp_path / "wd")


def test_git_pin_store_refuses_production_outside_ci(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """R2：正式 pin repo 在非 CI 環境拒絕寫入（本機只能寫 pin-test）。"""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    key = tmp_path / "k"; key.write_bytes(b"x")
    hosts = tmp_path / "h"; hosts.write_bytes(b"y")
    with pytest.raises(PermissionError):
        GitPinStore("git@github.com:owner/MyAiStorage-pin.git", tmp_path / "wd",
                    key_path=key, known_hosts_path=hosts)


# ---------------------------------------------------------------------------
# gc：removed 回收、防呆、盡力而為（M6）、隔離 7 天從移入算（H3）
# ---------------------------------------------------------------------------

def test_collect_removed_bundles_sorted(bundles: dict):
    st = _state(bundles, manifest_sha=bundles["m1sha"],
                removed=frozenset({bundles["b2"], bundles["b1"]}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    got = collect_removed_bundles(listing, st)
    assert [f.name for f in got] == sorted([bundles["b1"], bundles["b2"]])


def test_gc_removed_happy_and_dry_run(bundles: dict):
    st = _state(bundles, manifest_sha=bundles["m1sha"],
                removed=frozenset({bundles["b2"]}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    fid = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    assert gc_removed(collect_removed_bundles(listing, st), drive,
                      prefix_folder_id=prefix, state=st, dry_run=True) == 1
    assert [f.id for f in drive.list_children(prefix)] == [fid]
    n = gc_removed(collect_removed_bundles(listing, st), drive,
                   prefix_folder_id=prefix, state=st)
    assert n == 1
    assert [f.id for f in drive.list_children(prefix)] == []


def test_gc_removed_max_delete(bundles: dict):
    names = sorted([bundles["b1"], bundles["b2"]])
    st = _state(bundles, manifest_sha=bundles["m1sha"], removed=frozenset(names))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    for key in names:
        drive.seed_file(prefix, key, bundles["b1_bytes"] if key == bundles["b1"] else bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    got = collect_removed_bundles(listing, st)
    assert [f.name for f in got] == names
    n = gc_removed(got, drive, prefix_folder_id=prefix, state=st, max_delete=1)
    assert n == 1
    remaining = [f.name for f in drive.list_children(prefix)]
    assert remaining == names[1:]


def test_gc_removed_wrong_parent_raises_and_keeps(bundles: dict):
    """防呆：parents 不含前綴 → MismatchError，不刪除。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"],
                removed=frozenset({bundles["b2"]}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    elsewhere = drive.seed_folder("elsewhere")
    fid = drive.seed_file(elsewhere, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(elsewhere)), subfolders=tuple())
    with pytest.raises(MismatchError):
        gc_removed(collect_removed_bundles(listing, st), drive,
                   prefix_folder_id=prefix, state=st)
    assert drive.get(fid).id == fid


def test_gc_removed_best_effort_on_delete_error(bundles: dict):
    """M6：單檔刪除失敗 → 盡力而為，不中止整輪，其他照刪。"""
    names = sorted([bundles["b1"], bundles["b2"]])
    st = _state(bundles, manifest_sha=bundles["m1sha"], removed=frozenset(names))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    ids = {}
    for key in names:
        ids[key] = drive.seed_file(
            prefix, key,
            bundles["b1_bytes"] if key == bundles["b1"] else bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    drive.inject("delete_permanently", ids[names[0]], error=WriteError)
    n = gc_removed(collect_removed_bundles(listing, st), drive,
                   prefix_folder_id=prefix, state=st)
    assert n == 1
    assert drive.get(ids[names[0]]).id == ids[names[0]]
    assert [f.id for f in drive.list_children(prefix)] == [ids[names[0]]]


def test_gc_removed_best_effort_on_get_error(bundles: dict):
    """M6：單檔 get() 讀取失敗 → 跳過不中止。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"],
                removed=frozenset({bundles["b2"]}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    fid = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    drive.inject("get", fid, error=ReadError)
    n = gc_removed(collect_removed_bundles(listing, st), drive,
                   prefix_folder_id=prefix, state=st)
    assert n == 0
    drive.get(fid)  # 注入是一次性的；檔案仍在


def test_purge_quarantine_counts_from_move_in_date(bundles: dict):
    """H3：隔離 7 天從移入算。30 天前建立的檔今天被隔離 → 隔天 purge 不得刪除；
    第 8 天 purge 才刪除。"""
    from aistorage.integrity.sweep import SweepDecision
    t0 = FixedClock(CLOCK_T0)
    drive = FakeDrive(clock=t0)
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    old = drive.seed_file(prefix, "old-bundle", b"x" * 8,
                          created_time="2026-08-01T00:00:00Z")
    apply_sweep(
        [SweepDecision(file=drive.get(old), disposition=Disposition.QUARANTINE,
                       reason="test", from_parent=prefix)],
        drive, quarantine_folder_id=quarantine, clock=t0, prefix_folder_id=prefix)

    day1 = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)
    assert purge_quarantine(drive, quarantine, now=day1) == 0
    assert drive.get(old).id == old

    day8 = datetime(2026, 10, 5, 10, 0, 1, tzinfo=timezone.utc)
    assert purge_quarantine(drive, quarantine, now=day8) >= 1
    assert old not in _all_ids_under(drive, quarantine)


def test_purge_quarantine_dry_run(bundles: dict):
    t0 = FixedClock(CLOCK_T0)
    drive = FakeDrive(clock=t0)
    quarantine = drive.seed_folder("quarantine")
    daydir = drive.seed_folder("2026-09-01", parent=quarantine)
    fid = drive.seed_file(daydir, "junk", b"junk")
    before = drive.snapshot()
    now = datetime(2026, 10, 5, 10, 0, 1, tzinfo=timezone.utc)
    assert purge_quarantine(drive, quarantine, now=now, dry_run=True) >= 1
    assert drive.snapshot() == before
    assert fid in _all_ids_under(drive, quarantine)
    assert purge_quarantine(drive, quarantine, now=now) >= 1
    assert fid not in _all_ids_under(drive, quarantine)


# ---------------------------------------------------------------------------
# 性質測試（H4／R3）：任何 ReadError 下都不移動；pins 也不被寫入
# ---------------------------------------------------------------------------

class CountingDrive:
    """唯讀計數包裝：轉發 FakeDrive，計算讀取操作次數。"""

    def __init__(self, inner: FakeDrive):
        self._inner = inner
        self.reads = 0

    def _counted(self, name: str):
        def wrapper(*args, **kwargs):
            self.reads += 1
            return getattr(self._inner, name)(*args, **kwargs)
        return wrapper

    def list_children(self, *a, **k):
        return self._counted("list_children")(*a, **k)

    def find_by_name(self, *a, **k):
        return self._counted("find_by_name")(*a, **k)

    def get(self, *a, **k):
        return self._counted("get")(*a, **k)

    def download(self, *a, **k):
        return self._counted("download")(*a, **k)

    def download_bytes(self, *a, **k):
        return self._counted("download_bytes")(*a, **k)

    def __getattr__(self, name: str):
        return getattr(self.__dict__["_inner"], name)


def _run_property(bundles: dict, tmp_path: Path, scenario: str):
    """三組情境 × 窮舉第 n 個讀取失敗。回傳 (reads_on_success, outcomes)。"""
    probe = FakeDrive()
    if scenario == "no_pending":
        prefix, _ = _seed_prefix(probe, bundles, bundles["m1"], ["b1"],
                                 extra=[("junk.txt", b"junk")])
        no_sha = probe.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
        probe.set_checksum(no_sha, None)
        st = _state(bundles, manifest_sha=bundles["m1sha"])
        pend = None
    elif scenario == "promoted":
        prefix, _ = _seed_prefix(probe, bundles, bundles["m2"], ["b1", "b2"])
        st = _state(bundles, manifest_sha=bundles["m1sha"])
        pend = _pending(bundles)
    else:  # content_check
        prefix, _ = _seed_prefix(probe, bundles, bundles["m1"], ["b1"])
        no_sha = probe.seed_file(prefix, "extra.bin", b"0123456789a")
        probe.set_checksum(no_sha, None)
        st = _state(bundles, manifest_sha=bundles["m1sha"])
        pend = None
    quarantine = probe.seed_folder("quarantine")
    counter = CountingDrive(probe)
    pins = MemoryPinStore(initial_state=st,
                          initial_pending=pend)
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(probe.list_children(prefix)), subfolders=tuple())
    res = run_settle_and_sweep(st, pend, drive=counter, pins=pins,
                               prefix_folder_id=prefix,
                               quarantine_folder_id=quarantine,
                               repo_uuid=UUID, workdir=tmp_path,
                               clock=FixedClock(CLOCK_T0), cache={})
    total_reads = counter.reads

    outcomes: list[tuple[int, str]] = []
    for n in range(1, total_reads + 10):
        drive = FakeDrive()
        if scenario == "no_pending":
            p, _ = _seed_prefix(drive, bundles, bundles["m1"], ["b1"],
                                extra=[("junk.txt", b"junk")])
            fid = drive.seed_file(p, bundles["b1"], bundles["b1_bytes"])
            drive.set_checksum(fid, None)
        elif scenario == "promoted":
            p, _ = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
        else:
            p, _ = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
            fid = drive.seed_file(p, "extra.bin", b"0123456789a")
            drive.set_checksum(fid, None)
        q = drive.seed_folder("quarantine")
        before = drive.snapshot()
        pins_n = MemoryPinStore(initial_state=st, initial_pending=pend)
        lst = RepoListing(prefix_folder_id=p,
                          files=tuple(drive.list_children(p)), subfolders=tuple())
        wd = tmp_path / f"wd-{scenario}-{n}"
        wd.mkdir(exist_ok=True)
        drive.inject_nth_read(n)
        try:
            run_settle_and_sweep(st, pend, drive=drive, pins=pins_n,
                                 prefix_folder_id=p, quarantine_folder_id=q,
                                 repo_uuid=UUID, workdir=wd,
                                 clock=FixedClock(CLOCK_T0), cache={})
            outcomes.append((n, "success"))
            break  # 讀取點已窮舉，後續只會成功
        except ReadError:
            outcomes.append((n, "readerror"))
            assert drive.snapshot() == before, f"scenario={scenario} n={n}：失敗時發生移動"
            loaded_state, loaded_pending = pins_n.load("agora")
            assert loaded_state == st, f"scenario={scenario} n={n}：pins 被寫入"
            assert (loaded_pending == pend) or (pend is None and loaded_pending is None), \
                f"scenario={scenario} n={n}：pending 被更動"
    assert outcomes and outcomes[-1] == (outcomes[-1][0], "success"), \
        f"scenario={scenario}：讀取點窮舉後應成功收尾"
    assert all(code in ("readerror", "success") for _, code in outcomes)
    return total_reads, outcomes


def test_property_no_move_on_read_error_no_pending(bundles: dict, tmp_path: Path):
    """R3-1：無 pending 情境，任一讀取失敗 → 嚴格 ReadError、零移動、pins 不變。"""
    _run_property(bundles, tmp_path, "no_pending")


def test_property_no_move_on_read_error_promoted(bundles: dict, tmp_path: Path):
    """R3-2：PROMOTED 情境（含 settle 讀取路徑），任一讀取失敗 → ReadError、零移動、pins 不變。"""
    _run_property(bundles, tmp_path, "promoted")


def test_property_no_move_on_read_error_content_check(bundles: dict, tmp_path: Path):
    """R3-3：NEED_CONTENT_CHECK 情境，任一讀取失敗 → ReadError、零移動。"""
    _run_property(bundles, tmp_path, "content_check")


def test_run_settle_and_sweep_dry_run_changes_nothing(bundles: dict, tmp_path: Path):
    """dry-run：判定照跑但零寫入（drive 與 pins 都不變；回傳值為預計移動數）。"""
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"],
                                   extra=[("junk.txt", b"junk")])
    quarantine = drive.seed_folder("quarantine")
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    pins = MemoryPinStore(initial_state=st)
    before = drive.snapshot()
    res = run_settle_and_sweep(st, None, drive=drive, pins=pins,
                               prefix_folder_id=prefix,
                               quarantine_folder_id=quarantine,
                               repo_uuid=UUID, workdir=tmp_path,
                               clock=FixedClock(CLOCK_T0), cache={}, dry_run=True)
    assert res.moved_count == 1  # dry-run 回傳預計移動數，但不實際搬移
    assert drive.snapshot() == before
    loaded, pending = pins.load("agora")
    assert loaded == st and pending is None


def test_run_settle_and_sweep_promoted_writes_pins(bundles: dict, tmp_path: Path):
    """成功 PROMOTED 時 pins 轉正（對照 R3：失敗時則不寫入，已於性質測試斷言）。"""
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m2"], ["b1", "b2"])
    quarantine = drive.seed_folder("quarantine")
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    pend = _pending(bundles)
    pins = MemoryPinStore(initial_state=st, initial_pending=pend)
    res = run_settle_and_sweep(st, pend, drive=drive, pins=pins,
                               prefix_folder_id=prefix,
                               quarantine_folder_id=quarantine,
                               repo_uuid=UUID, workdir=tmp_path,
                               clock=FixedClock(CLOCK_T0), cache={})
    assert res.settle_outcome == SettleOutcome.PROMOTED
    loaded, no_pending = pins.load("agora")
    assert loaded.refs == {"refs/heads/main": bundles["c2"]}
    assert loaded.manifest_sha256 == bundles["m2sha"]
    assert no_pending is None

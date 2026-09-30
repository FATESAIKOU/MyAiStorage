"""3.2 完整性機制的驗收測試（測試方撰寫，未看實作，只依文件與簽名）。

對照：
- docs/impl/group3-modules.md 第 3 節（介面）與第 8.2 節（案例表）
- design D2 的 13 步、ADR 0008（釘選值是唯一信任來源、讀不到就中止不移動）
- review-g3c.md（H1〜H4、M1〜M9）與 review-g3c-recheck.md（R1〜R3）

全部直接 import，不 skip；只用 FakeDrive／FakeAnnexGit／MemoryPinStore 與本機 file:// pin repo。
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import FixedClock, parse_rfc3339
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
    SweepPolicy,
    apply_sweep,
    check_parents,
    plan_readview_sweep,
    plan_sweep,
    plan_upload_exclusive,
    resolve_content_checks,
    resolve_manifest_evidence,
    run_settle_and_sweep,
    upload_window,
    verify_upload_window,
)
from aistorage.integrity.verify import (
    precheck,
    verify_after_push,
    verify_clone,
    verify_pin_keys_on_drive,
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
    outcome, ns = settle(st, _pending(bundles), listing, drive, workdir=tmp_path, clock=FixedClock())
    assert outcome == SettleOutcome.DROPPED
    assert ns == st


class _JitteringListingDrive:
    """每次列舉都多放一個垃圾檔（住民持續寫入前綴的形狀）。

    M2：原本 `_fingerprint` 涵蓋前綴裡所有檔案，於是這種寫入會讓每一次
    重查都「有變動」、必定用完上限，最後照樣丟掉 pending——上限等於沒有。
    指紋縮小到與結論有關的檔案之後，垃圾檔不再影響判斷。
    """

    def __init__(self, inner: FakeDrive, *, folder_id: str, extra: int) -> None:
        self._inner = inner
        self._folder_id = folder_id
        self._extra = extra
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_children(self, folder_id):
        if folder_id != self._folder_id:
            return self._inner.list_children(folder_id)
        self._calls += 1
        for i in range(self._extra):
            self._inner.seed_file(
                self._folder_id, f"resident-junk-{self._calls}-{i}.bin", b"junk" * 8
            )
        return self._inner.list_children(folder_id)


def test_settle_ignores_resident_junk_when_rechecking(bundles: dict, tmp_path: Path):
    """M2：住民在重查期間持續放垃圾檔，不該讓重查失效、也不該讓 pending 被丟。

    舊行為：每一次重新列舉都「有變動」→ 用完上限 → 照樣回傳 DROPPED。
    新行為：垃圾檔不在結論依賴的檔名集合裡，指紋不變 → 結論成立，DROPPED。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
    jitter = _JitteringListingDrive(drive, folder_id=prefix, extra=3)
    outcome, ns = settle(
        st, _pending(bundles), listing, jitter, workdir=tmp_path, clock=FixedClock()
    )
    assert outcome == SettleOutcome.DROPPED, "垃圾檔不該讓 settle 放棄結論"
    assert ns == st
    # 進場列舉一次 + 確認一次；不會因為垃圾檔反覆重列（否則重查上限形同虛設）
    assert jitter._calls == 2


class _ShiftingManifestIdDrive:
    """每次列舉都給主 manifest 一個新的 file id（內容與雜湊完全相同）。

    指紋含 file id，所以這代表「與結論有關的檔案在判定期間換了一份」——
    對應的現實是 Drive 原地更新同一個 manifest（rclone 重寫）而列表回報的
    檔案換了新 id。settle 必須重判；重查到上限還在換，就只能中止。
    """

    def __init__(self, inner: FakeDrive, *, folder_id: str, name: str) -> None:
        self._inner = inner
        self._folder_id = folder_id
        self._name = name
        self._n = 0
        self._calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_children(self, folder_id):
        if folder_id != self._folder_id:
            return self._inner.list_children(folder_id)
        self._calls += 1
        # 每次列舉前都把那一份 manifest「原地改寫」成新的 file id
        # （Drive 改寫檔案會換 revision id；內容與雜湊不變）
        current = [f for f in self._inner.list_children(folder_id) if f.name == self._name]
        for f in current:
            self._n += 1
            content = self._inner.download_bytes(f.id, max_bytes=1 << 20)
            new_id = self._inner.seed_file(
                self._folder_id, self._name, content, file_id=f"{f.id}-r{self._n}"
            )
            self._inner._files.pop(f.id, None)
            self._inner._contents.pop(f.id, None)
            assert new_id  # 新 id 真的建出來了（否則 download 會失敗）
        return self._inner.list_children(folder_id)


def test_settle_aborts_and_keeps_pending_when_the_listing_never_settles(
    bundles: dict, tmp_path: Path,
):
    """M2：重查用完上限、前綴**仍在變動** → 中止這一輪並保留 pending。

    舊行為：第三次不論清單是否還在變動都回傳 DROPPED，等於「用完上限」就
    照舊丟掉 pending——而 pending 是「這一輪 push 之後遠端會變成什麼」的唯一
    說明，丟掉之後遠端就再也沒有人負責（impl1 的終局）。而且方向和
    「主 manifest 缺失」那條路（raise）不一致。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix, listing = _seed_prefix(drive, bundles, bundles["m1"], ["b1"])
    shifting = _ShiftingManifestIdDrive(drive, folder_id=prefix, name=MANIFEST_NAME)
    with pytest.raises(MismatchError, match="保留待定釘選值"):
        settle(st, _pending(bundles), listing, shifting,
               workdir=tmp_path, clock=FixedClock())
    # 進場列舉 1 次 + 每次判斷後的確認列舉（_MAX_RECHECK + 1 次）= 4 次；
    # 用完之後中止，而不是硬把 pending 丟掉。
    assert shifting._calls == 4


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
    """主 manifest 只 KEEP 正式值；上一版冒充（非 .bak 名）→ QUARANTINE；
    .bak 只接受正式值或上一版；其他名稱 → QUARANTINE。

    review-1926cd3 H1：**已有 KEEP 候選時，同名的第二份主 manifest 直接隔離，
    不讀內容**。rclone 更新 manifest 是原地更新，正常流程不會產生第二份同名
    主 manifest，所以「KEEP 候選還在 + 又多一份同名檔」只可能是注入物。
    舊規則在這裡會讀內容、看到合法就 HOLD，於是 `verify_clone` 的「恰好一份」
    每輪都失敗——提交流程永久停擺，而且任何一個住民都做得到。
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
    assert decs[dup] == Disposition.QUARANTINE
    assert decs[prev_impostor] == Disposition.QUARANTINE
    assert decs[bak] == Disposition.KEEP
    assert decs[bak_bad] == Disposition.QUARANTINE
    assert decs[other] == Disposition.QUARANTINE

    # 就算那份同名檔的內容**完全合法**（刪掉一行、引用的 bundle 都還在），
    # 有 KEEP 候選在場時一樣隔離，而且不讀它的內容。
    legal = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"] + b"\n")
    listing2 = RepoListing(prefix_folder_id=prefix,
                           files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs2 = {d.file.id: d.disposition for d in plan_sweep(listing2, st, repo_uuid=UUID)}
    assert decs2[legal] == Disposition.QUARANTINE
    assert decs2[good] == Disposition.KEEP


def test_plan_sweep_quarantines_second_legal_manifest_and_verify_clone_recovers(
    bundles: dict,
):
    """H1 的完整案例：KEEP 候選 + 一份「刪一行的合法 manifest」→ 隔離，
    下一輪的 `verify_clone`（要求主 manifest 恰好一份）恢復正常。

    住民拿得到真正的 manifest 內容與 bundle 名稱，把真正那份刪掉一行放進去
    就得到一個「解析得出來、引用的 bundle 都在」的同名 manifest。舊規則會
    HOLD 它，於是 `verify_clone` 每一輪都以「主 manifest 數量異常」中止。
    """
    from aistorage.annex.fake import FakeAnnexGit
    from aistorage.integrity.verify import verify_clone

    st = _sweep_state(bundles, active=(bundles["b1"], bundles["b2"]))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    official = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    # 合法但內容不同：多一個換行（解析仍通過，active 仍是 b1+b2）
    forged = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"] + b"\n")

    quarantine = drive.seed_folder("quarantine")
    clock = FixedClock(CLOCK_T0)

    def _sweep_once() -> int:
        listing = RepoListing(
            prefix_folder_id=prefix,
            files=tuple(drive.list_children(prefix)),
            subfolders=tuple(),
        )
        return apply_sweep(
            plan_sweep(listing, st, repo_uuid=UUID),
            drive, quarantine_folder_id=quarantine,
            clock=clock, prefix_folder_id=prefix,
        )

    # 第 1 輪：那份同名 manifest 被隔離（搬走，不是刪除）
    assert _sweep_once() == 1
    assert [f.name for f in drive.list_children(prefix)].count(MANIFEST_NAME) == 1
    assert forged not in [f.id for f in drive.list_children(prefix)]
    assert official in [f.id for f in drive.list_children(prefix)]
    assert any(
        f.id == forged
        for day in drive.list_children(quarantine)
        if day.is_folder
        for f in drive.list_children(day.id)
    ), "隔離是搬到 quarantine/<日期>/ 底下，不是刪除"

    # 第 2 輪起：前綴乾淨，什麼都不動（不會反覆搬移）
    assert _sweep_once() == 0

    # 隔離之後 verify_clone 通過（主 manifest 恰好一份且雜湊等於正式值）
    git = FakeAnnexGit(refs=dict(st.refs))
    verify_clone(git, st, drive=drive, prefix_folder_id=prefix)


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

    review-1926cd3 M1：**自洽不等於被背書**。不在釘選值、又不在
    `pending.annex_keys` 裡的 annex 物件，舊規則會 HOLD（檔名自稱的 sha256／
    大小與內容相符就留下），於是住民可以把自己的任意位元組以「檔名＝自己的
    雜湊」的形式永久放存在真本前綴裡。現在沒有 pending 背書就隔離。
    """
    content = b"0123456789a"  # 11 bytes
    key = f"SHA256E-s11--{hashlib.sha256(content).hexdigest()}"
    new_content = b"brand-new-raw"  # 13 bytes，這輪 push 上去的新物件
    new_key = f"SHA256E-s13--{hashlib.sha256(new_content).hexdigest()}"
    st = _sweep_state(bundles, keys=frozenset({key}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    keep = drive.seed_file(prefix, key, content)
    orphan = drive.seed_file(prefix, f"SHA256E-s3--{hashlib.sha256(b'zzz').hexdigest()}", b"zzz")
    sizemismatch = drive.seed_file(prefix, key, b"short")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[keep] == Disposition.KEEP
    assert decs[orphan] == Disposition.QUARANTINE
    assert decs[sizemismatch] == Disposition.QUARANTINE

    # 唯一可信的背書是 pending.annex_keys（push **之前**就寫進 pin repo）
    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    drive2.seed_file(prefix2, MANIFEST_NAME, bundles["m1"])
    pending_obj = drive2.seed_file(prefix2, new_key, new_content)
    orphan2 = drive2.seed_file(
        prefix2, f"SHA256E-s3--{hashlib.sha256(b'zzz').hexdigest()}", b"zzz"
    )
    listing2 = RepoListing(prefix_folder_id=prefix2,
                           files=tuple(drive2.list_children(prefix2)), subfolders=tuple())
    pend = _pending(bundles)
    pend = PinPending(
        repo=pend.repo, base_manifest_sha256=pend.base_manifest_sha256,
        refs=pend.refs, annex_keys=frozenset({new_key}),
        written_at=pend.written_at, run_id=pend.run_id,
    )
    decs2 = {
        d.file.id: d.disposition
        for d in plan_sweep(
            listing2, st, repo_uuid=UUID,
            policy=SweepPolicy(pending=pend, now=None),
        )
    }
    assert decs2[pending_obj] == Disposition.HOLD
    assert decs2[orphan2] == Disposition.QUARANTINE


def test_plan_sweep_hold_has_an_age_limit(bundles: dict):
    """M1：HOLD 設年齡上限——超過 `quarantine_retention_days` 就升級成 NEED_ADMIN。

    否則 HOLD 就是住民的永久存放區：釘選值一個禮拜都沒追上（pending 被丟掉、
    管理操作卡住）的情況會無限期拖下去，而且沒有人知道。

    review-903d7e2 L：**升級成 NEED_ADMIN 而不是自動隔離**。逾齡代表 pending 卡了
    一整週，這時候被隔離的很可能**就是真正的新世代**（impl1 事故的形狀：把新 raw
    物件當注入物搬走，之後 `init-pin` 斷言它們在 Drive 上）。所以不搬移，改由健康
    檢查報出「可能是真本」讓管理者判斷。

    （兩個檔用**不同的 key**：同名同內容的副本現在由 createdTime 決勝，那條規則在
    另一個測試裡。）
    """
    fresh_content = b"brand-new-raw"
    old_content = b"stuck-for-a-week"
    fresh_key = f"SHA256E-s13--{hashlib.sha256(fresh_content).hexdigest()}"
    old_key = f"SHA256E-s16--{hashlib.sha256(old_content).hexdigest()}"
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    fresh = drive.seed_file(prefix, fresh_key, fresh_content)
    old = drive.seed_file(
        prefix, old_key, old_content,
        created_time="2026-08-01T00:00:00Z", file_id="old_file",
    )
    pend = PinPending(
        repo="agora", base_manifest_sha256=bundles["m1sha"],
        refs={"refs/heads/main": bundles["c2"]},
        annex_keys=frozenset({fresh_key, old_key}),
        written_at="2026-09-27T01:00:00Z", run_id="run-2",
    )
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    now = parse_rfc3339(CLOCK_T0)
    decs = {
        d.file.id: d
        for d in plan_sweep(
            listing, st, repo_uuid=UUID,
            policy=SweepPolicy(pending=pend, hold_max_age_days=7, now=now),
        )
    }
    assert decs[fresh].disposition == Disposition.HOLD
    assert decs[old].disposition == Disposition.NEED_ADMIN
    assert "超過" in decs[old].reason
    assert "可能是真本" in decs[old].reason
    # 逾齡的不再自動隔離：apply_sweep 一個都不搬（搬掉的可能就是真本）
    quarantine = drive.seed_folder("quarantine")
    moved = apply_sweep(
        list(decs.values()), drive, quarantine_folder_id=quarantine,
        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix,
    )
    assert moved == 0
    assert drive.get(old).parents[0] == prefix


# ---------------------------------------------------------------------------
# impl1：釘選值落後遠端時，清掃不得消滅真本
# ---------------------------------------------------------------------------

def test_plan_sweep_keeps_a_manifest_the_pin_has_not_caught_up_with(bundles: dict):
    """push 成功、驗證未完成 → 釘選值還在上一版。

    這種狀態下遠端有的是**真的** manifest（內容合法、它列的 bundle 都在前綴裡）
    與一個**真的**新 bundle。舊規則會把兩者都當注入物搬走，於是 `git clone`
    再也找不到 manifest，真本被提交流程自己消滅，而且沒有任何一輪能自己回來。

    證明它是真的之後只能 HOLD（留在原地），不能 QUARANTINE——而「證明」的
    標準是 **pending**（review-1926cd3 M1）：pending 在 push 之前就寫定
    「這一輪 push 之後遠端會變成什麼」，候選 manifest 重放出來的 refs 必須
    等於 `pending.refs`。它列為 active 的新 bundle 也一併被背書。

    注意這裡的前綴**沒有**那份舊的正式 manifest：rclone 是原地更新同一個檔案，
    釘選值落後時前綴裡看不到舊內容（所以 H1 的「已有 KEEP 候選就隔離同名檔」
    不會誤傷這個狀況）。
    """
    st = _sweep_state(bundles)  # 釘選值只有 b1
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    keep_bundle = drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    new_bundle = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    ahead = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])  # b1+b2，釘選值還沒跟上

    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    pend = _pending(bundles)  # refs == 重放 m2 的結果
    first = plan_sweep(
        listing, st, repo_uuid=UUID,
        policy=SweepPolicy(pending=pend, now=None),
    )
    by_id = {d.file.id: d for d in first}
    assert by_id[ahead].disposition == Disposition.NEED_MANIFEST_CHECK

    second = resolve_manifest_evidence(
        first, drive, {}, st, repo_uuid=UUID,
        listing=listing, prefix_folder_id=prefix,
        policy=SweepPolicy(pending=pend, now=None),
    )
    by_id2 = {d.file.id: d for d in second}
    assert by_id2[ahead].disposition == Disposition.HOLD, (
        "重放出來的 refs 等於 pending.refs → 有背書，必須留著"
    )
    assert by_id2[new_bundle].disposition == Disposition.HOLD, (
        "被 pending 對應的 manifest 列為 active 的 bundle 一併被背書"
    )
    assert by_id2[keep_bundle].disposition == Disposition.KEEP


def test_manifest_without_pending_backing_is_need_admin_not_hold(bundles: dict):
    """沒有 pending（或 refs 對不上）時不是 HOLD，而是 NEED_ADMIN。

    HOLD 的意思是「有人替它背書、釘選值這一輪就會追上」；沒有 pending 就沒有
    這種背書，於是它是 **NEED_ADMIN**：不搬移（沒有「是注入物」的證據，搬走
    等於消滅真本），但健康檢查會報出檔名等人處理——而不是無限期地安靜留著。

    這正是 impl1 的終局（pending 被丟掉、遠端領先釘選值）會遇到的分支。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    ahead = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())

    resolved = resolve_manifest_evidence(
        plan_sweep(listing, st, repo_uuid=UUID, policy=SweepPolicy(now=None)),
        drive, {}, st, repo_uuid=UUID, listing=listing, prefix_folder_id=prefix,
        policy=SweepPolicy(now=None),
    )
    by_id = {d.file.id: d for d in resolved}
    assert by_id[ahead].disposition == Disposition.NEED_ADMIN
    assert "init-pin" in by_id[ahead].reason

    # apply_sweep 不會搬它（真本不能被消滅）
    quarantine = drive.seed_folder("quarantine")
    moved = apply_sweep(resolved, drive, quarantine_folder_id=quarantine,
                        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix)
    assert moved == 0
    assert ahead in [f.id for f in drive.list_children(prefix)]


def test_resolve_manifest_evidence_needs_matching_pending_refs(bundles: dict):
    """有 pending、但候選 manifest 重放出來的 refs 對不上 → NEED_ADMIN（不隔離）。

    refs 對不上代表「沒有任何可信來源能解釋這份 manifest」。它還不是注入物
    （內容合法、bundle 都在），所以不能隔離；但也沒有 pending 背書，所以不是
    HOLD。這一輪會在 `verify_clone` 中止，等管理者用 init-pin 重建釘選值。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"])
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    ahead = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    wrong_pending = _pending(bundles, refs={"refs/heads/main": "0" * 40})

    resolved = resolve_manifest_evidence(
        plan_sweep(listing, st, repo_uuid=UUID, policy=SweepPolicy(now=None)),
        drive, {}, st, repo_uuid=UUID, listing=listing, prefix_folder_id=prefix,
        policy=SweepPolicy(pending=wrong_pending, now=None),
    )
    by_id = {d.file.id: d for d in resolved}
    assert by_id[ahead].disposition == Disposition.NEED_ADMIN


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
    """自我一致但沒有任何背書的 bundle → QUARANTINE；檔名宣告與內容不符 → QUARANTINE。

    review-1926cd3 M1：舊規則是「自洽就 HOLD」，等於住民可以把自己的位元組
    以「檔名＝自己的雜湊」的形式永久放存在真本前綴裡（前綴因此不再是
    「只有釘選值背書的東西」）。現在自洽只是必要條件，還需要有人背書。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    honest = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    liar = drive.seed_file(prefix, bundles["b2"], b"payload-from-a-resident")
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    # 釘選值與前綴一致（主 manifest 的內容等於正式值）→ 自洽但沒背書就隔離
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[honest] == Disposition.NEED_ADMIN
    assert decs[liar] == Disposition.QUARANTINE, "檔名宣告與內容不符永遠是注入物"

    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    drive2.seed_file(prefix2, MANIFEST_NAME, bundles["m1"])
    honest2 = drive2.seed_file(prefix2, bundles["b2"], bundles["b2_bytes"])
    listing2 = RepoListing(prefix_folder_id=prefix2,
                           files=tuple(drive2.list_children(prefix2)), subfolders=tuple())
    decs2 = {d.file.id: d.disposition for d in plan_sweep(listing2, st, repo_uuid=UUID)}
    assert decs2[honest2] == Disposition.QUARANTINE


def test_unbacked_files_survive_only_while_the_pin_has_no_manifest(bundles: dict):
    """M1 的閘門：釘選值與前綴一致時，自洽但沒背書的檔案一律隔離；
    釘選值對前綴沒有權威時（沒有任何主 manifest 的內容等於正式值），
    「不在釘選值裡」不足以指認注入物 → 不搬移，改列入健康檢查。

    為什麼要有這個閘門：impl1 的終局就是 pending 被丟掉、遠端領先釘選值的
    狀態。那時候前綴裡的「新 raw 物件／新 bundle」是真本的新世代，不是注入物。
    照字面把「自洽但沒有背書 → 隔離」套上去，就會把它們搬走：raw 物件被隔離
    之後 `init-pin` 會因為「釘選值記載的物件不在 Drive 上」而拒絕建立釘選值，
    只能照 recovery runbook 模式 0 從隔離區搬回來。釘選值一旦追上（或管理者
    init-pin），這些檔案在下一輪就會被正常隔離——所以不是永久存放區。
    """
    content = b"brand-new-raw"
    key = f"SHA256E-s{len(content)}--{hashlib.sha256(content).hexdigest()}"

    # (a) 前綴有 KEEP 候選：釘選值有權威 → 自洽但沒背書 → 隔離
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    self_consistent = drive.seed_file(prefix, key, content)
    unbacked_bundle = drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"])
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decs = {d.file.id: d.disposition for d in plan_sweep(listing, st, repo_uuid=UUID)}
    assert decs[self_consistent] == Disposition.QUARANTINE
    assert decs[unbacked_bundle] == Disposition.QUARANTINE

    # (b) 前綴沒有 KEEP 候選（釘選值落後遠端）：不搬移，列入健康檢查
    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    ahead = drive2.seed_file(prefix2, MANIFEST_NAME, bundles["m2"])
    raw2 = drive2.seed_file(prefix2, key, content)
    drive2.seed_file(prefix2, bundles["b1"], bundles["b1_bytes"])
    drive2.seed_file(prefix2, bundles["b2"], bundles["b2_bytes"])
    listing2 = RepoListing(prefix_folder_id=prefix2,
                           files=tuple(drive2.list_children(prefix2)), subfolders=tuple())
    decs2 = {
        d.file.id: d.disposition
        for d in plan_sweep(listing2, st, repo_uuid=UUID)
    }
    assert decs2[ahead] == Disposition.NEED_MANIFEST_CHECK
    assert decs2[raw2] == Disposition.NEED_ADMIN
    # apply_sweep 不會搬它們（真本不能被消滅）
    quarantine = drive2.seed_folder("quarantine")
    assert apply_sweep(
        plan_sweep(listing2, st, repo_uuid=UUID), drive2,
        quarantine_folder_id=quarantine, clock=FixedClock(CLOCK_T0),
        prefix_folder_id=prefix2,
    ) == 0


def test_apply_sweep_never_moves_held_files(bundles: dict, tmp_path: Path):
    """HOLD 只是「不動」，不是「漏處理」：apply_sweep 不會搬它。

    NEED_ADMIN 同理——那是「沒有證據是注入物、也沒有任何背書」的狀態，
    搬走等於消滅真本（impl1 的終局），所以它留在原地，改由健康檢查報出來。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    new_raw = b"brand-new-raw"
    key = f"SHA256E-s13--{hashlib.sha256(new_raw).hexdigest()}"
    held = drive.seed_file(prefix, key, new_raw)
    pend = PinPending(
        repo="agora", base_manifest_sha256=bundles["m1sha"],
        refs={"refs/heads/main": bundles["c2"]},
        annex_keys=frozenset({key}), written_at="2026-09-27T01:00:00Z", run_id="run-2",
    )
    listing = RepoListing(prefix_folder_id=prefix,
                          files=tuple(drive.list_children(prefix)), subfolders=tuple())
    decisions = plan_sweep(
        listing, st, repo_uuid=UUID,
        policy=SweepPolicy(pending=pend, now=None),
    )
    assert {d.file.id: d.disposition for d in decisions}[held] == Disposition.HOLD
    moved = apply_sweep(decisions, drive, quarantine_folder_id=quarantine,
                        clock=FixedClock(), prefix_folder_id=prefix)
    assert moved == 0
    assert held in [f.id for f in drive.list_children(prefix)]


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

    # 位元組相同的第二份主 manifest（rclone 每輪 push 都重寫 manifest、同一輪
    # push 內也可能因 Drive 列表落後留下兩份，實測 1.75.1 與 1.69.3）：這是
    # **健康狀態**，clone 驗證不該因此失敗（review-903d7e2）。
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    verify_clone(git, st, drive=drive, prefix_folder_id=prefix)
    precheck(drive, prefix, MANIFEST_NAME, st)

    # 內容不同的第二份仍然是注入物 → 中止
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"])
    with pytest.raises(MismatchError, match="內容不一致"):
        verify_clone(git, st, drive=drive, prefix_folder_id=prefix)
    with pytest.raises(MismatchError, match="內容不一致"):
        precheck(drive, prefix, MANIFEST_NAME, st)


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


# ---------------------------------------------------------------------------
# review-903d7e2 H1／M1／M2：重複檔用 createdTime 決勝、偏離時的注入物證據
# ---------------------------------------------------------------------------

#: 提交流程那一輪 push 的時刻（manifest 就是在這個時候之後被 rclone 寫出來的）
PUSH_AT = "2026-09-27T02:00:00Z"
#: 比 push 早：真正的 manifest／bundle／物件（正常流程先上傳它們，最後才寫 manifest）
BEFORE_PUSH = "2026-09-27T01:59:00Z"
#: 比 push 晚十分鐘以上：正常流程不可能有這種東西（`INJECTION_SKEW_GRACE` 之後）
AFTER_PUSH = "2026-09-27T02:30:00Z"
#: 比 push **還早**：住民搶在 rclone 寫出 manifest 之前放進去的變體／物件
#: （review-5d4dd52 H1／H2 的攻擊前提：他可以在 push 之前就贏「建立最早」）
EARLIER_THAN_PUSH = "2026-09-27T01:58:00Z"


def _decide(drive: FakeDrive, st: PinState, **policy) -> dict[str, SweepDecision]:
    prefix = policy.pop("prefix")
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)),
        subfolders=(),
    )
    return {
        d.file.id: d
        for d in plan_sweep(listing, st, repo_uuid=UUID, prefix_folder_id=prefix, **policy)
    }


def test_h1_dup_active_bundle_keeps_the_earlier_created_one(bundles: dict):
    """住民上傳一份位元組相同的 active bundle 副本，而且排在清單**最前面**。

    舊規則是「清單裡的第一個留著」，清單順序 Drive 不保證，所以住民可以一直重試
    直到自己的副本被留下、真正的那份被隔離；`verify_after_push` 與
    `verify_pin_keys_on_drive` 之後每一輪都失敗。createdTime 是住民偽造不了的，
    而且 bundle 一個世代只上傳一次 → 副本一定比較晚。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    official = drive.seed_file(
        prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH,
    )
    # 副本**先**放進清單（FakeDrive 的 list_children 依建立順序回傳）
    copy_id = drive.seed_file(
        prefix, bundles["b1"], bundles["b1_bytes"], created_time=AFTER_PUSH,
    )
    decs = _decide(drive, st, prefix=prefix)
    assert decs[official].disposition == Disposition.KEEP
    assert decs[copy_id].disposition == Disposition.QUARANTINE
    assert "建立最早" in decs[copy_id].reason


def test_h1_dup_annex_object_keeps_the_earlier_created_one(bundles: dict):
    """釘選值記載的 annex 物件也一樣：副本（晚建立、位元組相同）被隔離。"""
    body = b"a-raw-record"
    key = f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}"
    st = _sweep_state(bundles, keys=frozenset({key}))
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"])
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    official = drive.seed_file(prefix, key, body, created_time=BEFORE_PUSH)
    copy_id = drive.seed_file(prefix, key, body, created_time=AFTER_PUSH)
    decs = _decide(drive, st, prefix=prefix)
    assert decs[official].disposition == Disposition.KEEP
    assert decs[copy_id].disposition == Disposition.QUARANTINE
    assert "建立最早" in decs[copy_id].reason


def test_h1_identical_official_manifest_duplicates_are_left_alone(bundles: dict):
    """位元組相同的主 manifest 重複：**兩份都不搬**，只有最早那份 KEEP。

    為什麼不用 createdTime 決勝（review-903d7e2 的前提是錯的）：rclone 每輪 push
    都重寫 manifest、**file id 每輪都會變**（實測 1.75.1 與 1.69.3），所以正式那份
    的 createdTime 是「上一輪 push 的時間」；而 manifest 的內容在好幾輪 push 之間
    可以位元組相同，於是住民的舊副本可能比正式那份還早——留最早和留最新都各被居民
    贏一次，而贏了的代價是隔離真本。而且健康前綴本來就可能有兩份同名同內容
    （同一輪 push 內 Drive 列表落後，實測每輪都會發生）。

    所以：分不出真身就不動手，改列入健康檢查等人判斷（附建立時間）。
    `verify_clone`／`precheck`／`verify_after_push` 也改成要求「只有一種內容」，
    push 與 clone 在有這種重複時都實測正常。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    official = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH,
    )
    copy_id = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m1"], created_time=AFTER_PUSH,
    )
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    decs = _decide(drive, st, prefix=prefix)
    assert decs[official].disposition == Disposition.KEEP
    assert decs[copy_id].disposition == Disposition.NEED_ADMIN
    assert "created=" in decs[copy_id].reason
    # 而且一個都不搬
    quarantine = drive.seed_folder("quarantine")
    assert apply_sweep(
        list(decs.values()), drive, quarantine_folder_id=quarantine,
        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix,
    ) == 0
    assert drive.get(copy_id).parents[0] == prefix


def test_h1_verify_clone_accepts_identical_duplicate_manifests(bundles: dict):
    """前綴裡有兩份位元組相同的主 manifest 時，clone 驗證與預檢都要通過。"""
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=AFTER_PUSH)
    git = FakeAnnexGit(refs={"refs/heads/main": bundles["c1"]})
    verify_clone(git, st, drive=drive, prefix_folder_id=prefix)
    precheck(drive, prefix, MANIFEST_NAME, st)


def test_m1_deviation_quarantines_files_created_after_the_last_manifest_write(
    bundles: dict,
):
    """偏離期間（釘選值對前綴沒有權威）仍然隔得掉注入物——證據是 createdTime。

    正常流程一定先上傳物件與 bundle、最後才重寫 manifest，所以建立時間晚於主
    manifest 最後一次寫入的檔案不屬於遠端任何一個世代。沒有 pending 時這些檔案
    會是 NEED_ADMIN（impl1 的終局就是新世代物件被搬走）；有了這條證據之後，
    偏離期間住民放的垃圾檔照樣隔離，而真正的物件（push 之前建立）不動。
    """
    ahead = bundles["m2"]
    ahead_sha = hashlib.sha256(ahead).hexdigest()
    st = _sweep_state(bundles)          # 釘選值還停在 m1 → 偏離
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    # 遠端領先：主 manifest 的內容既不是正式值也不是上一版
    drive.seed_file(
        prefix, MANIFEST_NAME, ahead, created_time=PUSH_AT, modified_time=PUSH_AT,
    )
    # 新世代的 bundle：push 之前上傳（正常流程）→ 不動
    new_bundle = drive.seed_file(
        prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH,
    )
    # 住民在 push 之後放的檔案（檔名宣告與內容相符，所以「自洽」不能拿來指認它）
    junk_body = b"resident-junk"
    junk_name = f"GITBUNDLE-s{len(junk_body)}--{UUID}-{hashlib.sha256(junk_body).hexdigest()}"
    junk_bundle = drive.seed_file(prefix, junk_name, junk_body, created_time=AFTER_PUSH)
    body = b"a-raw-record"
    key = f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}"
    real_obj = drive.seed_file(prefix, key, body, created_time=BEFORE_PUSH)
    junk_obj_body = b"resident-junk-raw"
    junk_obj = f"SHA256E-s{len(junk_obj_body)}--{hashlib.sha256(junk_obj_body).hexdigest()}"
    junk_obj_id = drive.seed_file(prefix, junk_obj, junk_obj_body, created_time=AFTER_PUSH)
    decs = _decide(drive, st, prefix=prefix)
    assert ahead_sha != bundles["m1sha"]
    assert decs[new_bundle].disposition == Disposition.NEED_ADMIN
    assert decs[real_obj].disposition == Disposition.NEED_ADMIN
    assert decs[junk_bundle].disposition == Disposition.QUARANTINE
    assert decs[junk_obj_id].disposition == Disposition.QUARANTINE
    assert "晚於前綴裡主 manifest 最後一次寫入" in decs[junk_bundle].reason
    assert "晚於前綴裡主 manifest 最後一次寫入" in decs[junk_obj_id].reason


def test_m1_deviation_keeps_the_earlier_new_generation_manifest(bundles: dict):
    """偏離期間同一份「新世代」內容有兩份同名 manifest → 早建立的那份是候選。

    這裡 createdTime 判得出真身：新世代的 manifest 是上一輪 push 剛寫出來的，
    住民要複製就得先讀到它，一定比較晚。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    real = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT, modified_time=PUSH_AT,
    )
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH)
    copy_id = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"], created_time=AFTER_PUSH, modified_time=AFTER_PUSH,
    )
    decs = _decide(
        drive, st, prefix=prefix, policy=SweepPolicy(
            now=parse_rfc3339(CLOCK_T0)),
    )
    # 沒有 pending、refs 對不上 → 兩份都不是 HOLD/NEED_ADMIN 的背書來源：真的那份
    # 進內容驗證（NEED_MANIFEST_CHECK），副本直接隔離。
    assert decs[copy_id].disposition == Disposition.QUARANTINE
    assert "副本" in decs[copy_id].reason
    assert decs[real].disposition == Disposition.NEED_MANIFEST_CHECK


def test_m2_settle_picks_the_earlier_created_candidate_that_replays_pending(
    bundles: dict, tmp_path: Path,
):
    """refs 相符但位元組相異的多份候選：**只有一種內容**時就採用它（review-903d7e2 M2）。

    舊規則是直接 raise，於是住民放一份「差一個換行、重放出同樣 refs」的主
    manifest，settle 就**每一輪都中止在第 3 步**；而第 3 步在 sweep 之前，那份
    副本永遠不會被隔離，清掃形同停擺。候選只有一種內容時沒有可選的余地，直接
    採用（真正會被多份不同內容絆住的，是 H1 那條，見下面兩個測試）。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH)
    real = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT,
    )
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    outcome, ns = settle(st, _pending(bundles), listing, drive,
                         workdir=tmp_path, clock=FixedClock(CLOCK_T0))
    assert outcome == SettleOutcome.PROMOTED
    assert ns.manifest_sha256 == hashlib.sha256(bundles["m2"]).hexdigest()
    assert decs_keeper_is_official(drive, ns, prefix, real)


def decs_keeper_is_official(drive, ns, prefix, official_id) -> bool:
    """轉正之後，那一份正式 manifest 在 sweep 眼裡是 KEEP（不是被隔離）。"""
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    decs = {
        d.file.id: d.disposition
        for d in plan_sweep(listing, ns, repo_uuid=UUID, prefix_folder_id=prefix)
    }
    return decs[official_id] == Disposition.KEEP


# ---------------------------------------------------------------------------
# review-5d4dd52 H1：候選只認 pending 記的內容雜湊（不用 createdTime 猜）
# ---------------------------------------------------------------------------

def _pending_with_expected(bx: dict, expected: str | None) -> PinPending:
    """帶（或不帶）`expected_manifest_sha256` 的 pending。"""
    from dataclasses import replace

    return replace(_pending(bx), expected_manifest_sha256=expected)


def test_h1_settle_promotes_the_recorded_content_not_the_earlier_variant(
    bundles: dict, tmp_path: Path,
):
    """H1：變體建立得**比真正那份還早**、兩者都重放出 pending.refs → 被轉正的仍是真本。

    攻擊（review-5d4dd52 H1）：住民自己就能製造 pending 狀態（push 期間放一份
    「重放出同樣 refs、位元組不同」的主 manifest 變體），而且只要**早於** rclone
    寫出 manifest 就能在「建立最早者勝」裡贏。贏了之後他的變體被轉正成正式值、
    真正那份被當成「內容不同的同名 manifest」隔離，他再刪掉自己那份，前綴就沒有
    能用的 manifest，`git clone` 失敗而且不會自己好——而且每一輪都可以重來。

    提交流程自己知道剛寫出去的位元組（push 之後本機
    `.git/annex/git-remote-annex/<uuid>/manifest` 與遠端那份 sha256 相同），
    記在 pending 的 `expected_manifest_sha256`。所以無論變體建立得多早，被轉正的
    都只有內容等於那個雜湊的那一份。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH)
    # 住民的變體**比真正那份早 30 秒**建立（他搶在 rclone 寫出 manifest 之前）
    forged = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"] + b"\n", created_time=EARLIER_THAN_PUSH,
    )
    real = drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT)
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    outcome, ns = settle(
        st, _pending_with_expected(bundles, bundles["m2sha"]), listing, drive,
        workdir=tmp_path, clock=FixedClock(CLOCK_T0),
    )
    assert outcome == SettleOutcome.PROMOTED
    assert ns.manifest_sha256 == bundles["m2sha"], "被轉正的必須是真正 push 出去的那份"
    assert ns.manifest_sha256 != hashlib.sha256(bundles["m2"] + b"\n").hexdigest()
    assert decs_keeper_is_official(drive, ns, prefix, real)
    # 轉正之後下一輪的 sweep 把住民那份變體隔離（內容既不是新的正式值也不是上一版）
    decs = _decide(drive, ns, prefix=prefix)
    assert decs[forged].disposition == Disposition.QUARANTINE
    assert decs[real].disposition == Disposition.KEEP


def test_h1_settle_fails_closed_when_pending_has_no_expected_sha(
    bundles: dict, tmp_path: Path,
):
    """H1 fail-closed：pending 沒有 `expected_manifest_sha256` 又有多種內容 → 中止。

    「停擺」是 ADR 0008 殘餘風險 (1) 已接受的（偵測得到、不會遺失或竄改內容）；
    「被接管」不是。所以這裡一律中止、保留 pending 與遠端原狀，**不用 createdTime
    猜**——猜就等於讓住民自己決定真身。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT)
    drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"] + b"\n", created_time=EARLIER_THAN_PUSH,
    )
    before = drive.snapshot()
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    with pytest.raises(MismatchError, match="expected_manifest_sha256"):
        settle(st, _pending(bundles), listing, drive,
               workdir=tmp_path, clock=FixedClock(CLOCK_T0))
    assert drive.snapshot() == before, "中止時一個檔都不能動"


def test_h1_verify_after_push_accepts_the_recorded_content_among_variants(
    bundles: dict, tmp_path: Path,
):
    """H1：`verify_after_push` 只認 pending 記的內容——住民的變體不再拖住 promote。

    舊規則要求「所有同名主 manifest 只有一種內容」，於是住民在 push 期間放一份
    位元組不同的變體就能讓這一輪**永遠不 promote**：pending 延一輪才結算、收件匣
    的項目多等一輪，而他可以一再重來。現在只要遠端有 pending 記載的那一份就過。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    # push 前的 listing（`listing_before`）：只有 b1 與舊的 manifest
    listing_before = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    # 這一輪 push 寫出來的新世代 manifest（＋ 新的 bundle）
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=PUSH_AT)
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT)
    # 住民的變體（位元組不同，但重放出同樣 refs）
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m2"] + b"\n", created_time=PUSH_AT)
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )

    class _Git:
        def ls_remote(self, remote: str = "origin") -> dict[str, str]:
            return {"refs/heads/main": bundles["c2"]}

    pv = verify_after_push(
        _Git(), drive, listing_before, st, {"refs/heads/main": bundles["c2"]}, PUSH_AT,
        workdir=tmp_path, expected_manifest_sha256=bundles["m2sha"],
    )
    assert pv.new_manifest_sha256 == bundles["m2sha"]

    # 遠端沒有 pending 記載的那一份 → 中止（而且訊息要說得出是哪一份）
    with pytest.raises(MismatchError, match="expected_manifest_sha256"):
        verify_after_push(
            _Git(), drive, listing_before, st, {"refs/heads/main": bundles["c2"]}, PUSH_AT,
            workdir=tmp_path, expected_manifest_sha256="0" * 64,
        )

    # pending 沒有這個欄位（push 與補寫之間中斷）＋有多種內容 → 一樣 fail-closed
    with pytest.raises(MismatchError, match="expected_manifest_sha256"):
        verify_after_push(
            _Git(), drive, listing_before, st, {"refs/heads/main": bundles["c2"]}, PUSH_AT,
            workdir=tmp_path, expected_manifest_sha256=None,
        )
    assert listing.files, "listing 本身沒被動過"


def test_h1_plan_sweep_keeps_the_recorded_new_generation_manifest(
    bundles: dict,
):
    """H1：偏離期間「新世代」同名 manifest 的保留者 = pending 記的內容雜湊。

    這裡故意讓**住民的變體建立得更早**：舊規則（建立最早者勝）會保留他那份、
    隔離真正上傳的那份。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    real = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT, modified_time=PUSH_AT,
    )
    forged = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"] + b"\n",
        created_time=EARLIER_THAN_PUSH, modified_time=EARLIER_THAN_PUSH,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(
            pending=_pending_with_expected(bundles, bundles["m2sha"]),
            now=parse_rfc3339(CLOCK_T0),
        ),
    )
    # 真正那份是 pending 記載的內容 → 不是「副本」，進內容驗證等著被背書；
    # 住民那份無論建立得多早都是副本，隔離。
    assert decs[forged].disposition == Disposition.QUARANTINE
    assert "expected_manifest_sha256" in decs[forged].reason
    assert decs[real].disposition == Disposition.NEED_MANIFEST_CHECK


def test_h1_plan_sweep_needs_admin_when_no_new_gen_file_matches(
    bundles: dict,
):
    """H1 fail-closed：pending 記了 expected sha，但前綴裡一份都沒有那個內容 → 不猜。

    沒有任何證據指認哪一份是新世代的真本（可能是那一輪在 push 與補寫之間中斷，
    也可能是變體被刪了），所以**一個都不搬**，全部列入健康檢查等人處理。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    forged = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"] + b"\n", created_time=PUSH_AT,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(
            pending=_pending_with_expected(bundles, bundles["m2sha"]),
            now=parse_rfc3339(CLOCK_T0),
        ),
    )
    assert decs[forged].disposition == Disposition.NEED_ADMIN
    assert "expected_manifest_sha256" in decs[forged].reason
    quarantine = drive.seed_folder("quarantine")
    assert apply_sweep(
        list(decs.values()), drive, quarantine_folder_id=quarantine,
        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix,
    ) == 0, "fail-closed 時一個檔都不能搬"


def test_h1_injection_cutoff_uses_the_recorded_manifest_created_time(bundles: dict):
    """M1：偏離時的注入物證據取 `expected_manifest_sha256` 那一份的 **createdTime**。

    舊規則取「建立最早者」的 `modifiedTime`，而「建立最早者」現在由住民的搶跑
    決定 → 證據的時間點也由住民控制（他隨時改寫自己的副本就會更新
    `modifiedTime`），偏離期間所有垃圾檔都會變成 NEED_ADMIN、掃描形同停擺。
    這裡讓住民那份變體建立得更早、而且**改寫時間很新**，斷言真正那份建立的時間
    仍然把垃圾檔照樣隔離掉。
    """
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b2"], bundles["b2_bytes"], created_time=BEFORE_PUSH)
    real = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"], created_time=PUSH_AT, modified_time=PUSH_AT,
    )
    forged = drive.seed_file(
        prefix, MANIFEST_NAME, bundles["m2"] + b"\n",
        created_time=EARLIER_THAN_PUSH, modified_time=AFTER_PUSH,
    )
    junk_body = b"resident-junk"
    junk_name = f"GITBUNDLE-s{len(junk_body)}--{UUID}-{hashlib.sha256(junk_body).hexdigest()}"
    junk_bundle = drive.seed_file(prefix, junk_name, junk_body, created_time=AFTER_PUSH)
    real_body = b"a-raw-record"
    real_key = f"SHA256E-s{len(real_body)}--{hashlib.sha256(real_body).hexdigest()}"
    real_obj = drive.seed_file(prefix, real_key, real_body, created_time=BEFORE_PUSH)
    junk_obj_body = b"resident-junk-raw"
    junk_key = f"SHA256E-s{len(junk_obj_body)}--{hashlib.sha256(junk_obj_body).hexdigest()}"
    junk_obj_id = drive.seed_file(prefix, junk_key, junk_obj_body, created_time=AFTER_PUSH)
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(
            pending=_pending_with_expected(bundles, bundles["m2sha"]),
            now=parse_rfc3339(CLOCK_T0),
        ),
    )
    # 真正那份的 createdTime（PUSH_AT）＋ 十分鐘容差 < AFTER_PUSH，所以垃圾檔照樣隔離
    assert decs[junk_bundle].disposition == Disposition.QUARANTINE
    assert decs[junk_obj_id].disposition == Disposition.QUARANTINE
    assert "晚於前綴裡主 manifest 最後一次寫入" in decs[junk_bundle].reason
    # push 之前建立的（真世代）不動；住民那份變體被隔離；真正那份等著被背書
    assert decs[real_obj].disposition == Disposition.NEED_ADMIN
    assert decs[forged].disposition == Disposition.QUARANTINE
    assert decs[real].disposition == Disposition.NEED_MANIFEST_CHECK


def test_m2_late_same_named_manifest_cannot_exhaust_the_rechecks(
    bundles: dict, tmp_path: Path,
):
    """push 之後才出現的同名 manifest 連指紋都不算 → 住民無法用重查上限拖住 settle。

    舊規則（指紋只看檔名）留下這個洞：主 manifest 的**檔名本身**就在相關檔名裡，
    住民只要在重查那幾十秒裡新增同名檔，每一次重查都必定「有變動」→ 必定用完上限
    → 每一輪都中止在第 3 步、sweep 永遠跑不到。
    """
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(drive.list_children(prefix)), subfolders=(),
    )
    shift = _ShiftingManifestIdDrive(drive, folder_id=prefix, name=MANIFEST_NAME)
    with pytest.raises(MismatchError, match="保留待定釘選值"):
        settle(st, _pending(bundles), listing, shift, workdir=tmp_path, clock=FixedClock())

    # 同一個情境，但「變動」是 push 之後才出現的同名 manifest → 不算變動，
    # settle 照樣 DROPPED（pending 丟掉是對的：遠端真的還停在正式值）。
    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    drive2.seed_file(prefix2, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    drive2.seed_file(prefix2, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    listing2 = RepoListing(
        prefix_folder_id=prefix2,
        files=tuple(drive2.list_children(prefix2)), subfolders=(),
    )

    class _AddsLateManifest:
        """每次 list_children 都多放一份「push 之後建立的同名 manifest」。"""

        def __init__(self, inner: FakeDrive, folder_id: str) -> None:
            self._inner = inner
            self._folder_id = folder_id
            self._added: set[str] = set()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def list_children(self, folder_id):
            out = list(self._inner.list_children(folder_id))
            if folder_id == self._folder_id:
                for idx in range(4):
                    new_id = f"late_dup_{idx}"
                    if new_id not in self._added:
                        self._added.add(new_id)
                        self._inner.seed_file(
                            self._folder_id, MANIFEST_NAME, bundles["m1"] + b"\n",
                            created_time=AFTER_PUSH, file_id=new_id,
                        )
                out = list(self._inner.list_children(folder_id))
            return out

    outcome, _ns = settle(st, _pending(bundles), listing2, _AddsLateManifest(drive2, prefix2),
                          workdir=tmp_path, clock=FixedClock(CLOCK_T0))
    assert outcome == SettleOutcome.DROPPED


# ---------------------------------------------------------------------------
# review-5d4dd52 H2：annex 物件的真身是「上傳時間窗內那一份」，不是建立最早者
# ---------------------------------------------------------------------------

#: 第 8 步 `git annex copy` 的上傳時間窗（提交流程自己量的，寫進 pending）
UPLOAD_START = "2026-09-27T02:00:00Z"
UPLOAD_END = "2026-09-27T02:00:30Z"


def _uploaded_pending(bx: dict, keys: frozenset[str], **kw) -> PinPending:
    """帶上傳時間窗的 pending（`state` 還沒有那些 key → 它們是這一輪才上傳的）。"""
    from dataclasses import replace

    return replace(
        _pending(bx), annex_keys=keys,
        upload_window_start=kw.get("start", UPLOAD_START),
        upload_window_end=kw.get("end", UPLOAD_END),
    )


def _raw_key(body: bytes) -> str:
    return f"SHA256E-s{len(body)}--{hashlib.sha256(body).hexdigest()}"


def test_h2_sweep_keeps_the_in_window_upload_not_the_earlier_preplaced_copy(bundles: dict):
    """H2：住民**預先**放一份同名同內容的物件 → 保留的是上傳時間窗內那一份。

    攻擊（review-5d4dd52 H2）：住民送出自己的 session 時就知道自己 raw 的 key，
    從這一輪開始每隔幾秒就在前綴放一份同名同內容的物件。第 4 步 sweep 會隔離
    「那一刻已經存在」的那些，但第 4 步列舉**之後**才放進去的會留下來，而且它
    建立得比提交流程上傳的那份更早 → 「建立最早者勝」會保留它、隔離真正上傳的
    那份。住民再刪掉自己那份，被釘選的 key 就從 Drive 上消失：
    `verify_pin_keys_on_drive` 每一輪都中止、不會自己好，而那份 raw 已經是 Agora
    的真本。
    """
    body = b"a-raw-record-for-this-round"
    key = _raw_key(body)
    st = _sweep_state(bundles)          # 釘選值還沒有這個 key
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    # 住民預先放的那份：建立得比上傳時間窗**還早**
    preplaced = drive.seed_file(
        prefix, key, body, created_time=EARLIER_THAN_PUSH, modified_time=AFTER_PUSH,
    )
    # 提交流程上傳的那一份（落在 pending 記的時間窗裡）
    uploaded = drive.seed_file(
        prefix, key, body, created_time=UPLOAD_START, modified_time=UPLOAD_START,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(pending=_uploaded_pending(bundles, frozenset({key})),
                           now=parse_rfc3339(CLOCK_T0)),
    )
    assert decs[uploaded].disposition == Disposition.HOLD, "窗內那一份才是提交流程上傳的"
    assert decs[preplaced].disposition == Disposition.QUARANTINE
    assert "上傳時間窗" in decs[preplaced].reason, (
        "保留者是用上傳時間窗認的，理由不能寫成「建立最早者勝」"
    )
    assert "預先放置" in decs[preplaced].reason


def test_h2_sweep_needs_admin_when_no_copy_falls_inside_the_upload_window(bundles: dict):
    """H2 fail-closed：有同名副本但窗內一份都沒有 → 不退回「建立最早」，也不搬移。

    這同時是 M3 的答案：rclone 若因為前綴裡已有同名檔而跳過上傳（review-903d7e2
    一直沒實測的那件事），提交流程上傳的那一份就不存在。於是窗內一份都沒有，而
    剩下的都是預先放置的——不能保留（會被住民刪掉）、也不能搬移（他還在不斷重放，
    搬了下一輪又長出來）。列入健康檢查等人處理。
    """
    body = b"a-raw-record-for-this-round"
    key = _raw_key(body)
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    # 兩份都在時間窗之外（一份上傳前、一份上傳後，都是住民放的）
    preplaced = drive.seed_file(
        prefix, key, body, created_time=EARLIER_THAN_PUSH, modified_time=AFTER_PUSH,
    )
    late = drive.seed_file(
        prefix, key, body, created_time=AFTER_PUSH, modified_time=AFTER_PUSH,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(pending=_uploaded_pending(bundles, frozenset({key})),
                           now=parse_rfc3339(CLOCK_T0)),
    )
    assert decs[preplaced].disposition == Disposition.NEED_ADMIN
    assert decs[late].disposition == Disposition.NEED_ADMIN
    assert "上傳時間窗" in decs[preplaced].reason
    assert "checkpresent" in decs[preplaced].reason
    quarantine = drive.seed_folder("quarantine")
    assert apply_sweep(
        list(decs.values()), drive, quarantine_folder_id=quarantine,
        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix,
    ) == 0, "沒有證據時一個都不能搬"


def test_h2_sweep_does_not_pick_any_copy_when_the_window_holds_two(bundles: dict):
    """M1（review-final a）：上傳時間窗內**兩份**時一個都不選、**一個都不搬**。

    攻擊：住民在第 8 步上傳**之後**、promote 之前再放一份同名同內容的物件。前綴裡
    當時沒有同名檔，第 8 步的隔離什麼都沒搬；第 8 步的 `verify_upload_window` 只要求
    「窗內**至少**有一份」（而且那時他還沒放），所以兩道門都過。下一輪 sweep 看到窗內
    有兩份——舊規則是「窗內最早者勝」，而他自己那份一定比較早，於是他贏了、提交流程
    上傳的那份被當成重複檔隔離，他再刪掉自己那份，被釘選的 key 就從 Drive 上消失
    （每一輪都中止、不會自己好，而那份 raw 已經是 Agora 的真本）。

    現在窗內兩份就不選：兩份都是 NEED_ADMIN、`apply_sweep` 一個都不搬。代價是住民可以
    讓自己的 session 一直進不來（只影響他自己，而且真本沒有消失——兩份都還在前綴裡）。
    """
    body = b"a-raw-record-for-this-round"
    key = _raw_key(body)
    st = _sweep_state(bundles)          # 釘選值還沒有這個 key → 它是這一輪才上傳的
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    # 提交流程上傳的那一份（落在 pending 記的時間窗裡）
    uploaded = drive.seed_file(
        prefix, key, body, created_time=UPLOAD_START, modified_time=UPLOAD_START,
    )
    # 住民在 promote 之前補放的：同樣落在窗內，而且**更早**（舊規則會選它）
    late_preplaced = drive.seed_file(
        prefix, key, body, created_time="2026-09-27T02:00:10Z",
        modified_time="2026-09-27T02:00:10Z",
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(pending=_uploaded_pending(bundles, frozenset({key})),
                           now=parse_rfc3339(CLOCK_T0)),
    )
    assert decs[uploaded].disposition == Disposition.NEED_ADMIN
    assert decs[late_preplaced].disposition == Disposition.NEED_ADMIN
    for dec in (decs[uploaded], decs[late_preplaced]):
        assert "2 份" in dec.reason, dec.reason
        assert "上傳時間窗" in dec.reason
    # 一個都不能搬：真本沒有消失（兩份都還在前綴裡），只是等人判斷
    quarantine = drive.seed_folder("quarantine")
    assert apply_sweep(
        list(decs.values()), drive, quarantine_folder_id=quarantine,
        clock=FixedClock(CLOCK_T0), prefix_folder_id=prefix,
    ) == 0
    left = {f.name for f in drive.list_children(prefix)}
    assert key in left, "窗內兩份時一份都不能被搬走"


def test_m1_sweep_still_keeps_the_single_copy_inside_the_window(bundles: dict):
    """M1 a 的回歸保護：窗內**恰好一份**時照舊保留、窗外的預先放置份照舊隔離。

    「不選」只針對指認不出唯一真身的情形；正常流程（一個世代上傳一次、窗內一份）
    不能被這個規則擋下來。
    """
    body = b"a-raw-record-for-this-round"
    key = _raw_key(body)
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    preplaced = drive.seed_file(
        prefix, key, body, created_time=EARLIER_THAN_PUSH, modified_time=AFTER_PUSH,
    )
    uploaded = drive.seed_file(
        prefix, key, body, created_time=UPLOAD_START, modified_time=UPLOAD_START,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        policy=SweepPolicy(pending=_uploaded_pending(bundles, frozenset({key})),
                           now=parse_rfc3339(CLOCK_T0)),
    )
    assert decs[uploaded].disposition == Disposition.HOLD
    assert decs[preplaced].disposition == Disposition.QUARANTINE


def test_h2_plan_upload_exclusive_quarantines_preexisting_same_named_copies(bundles: dict):
    """H2：第 8 步上傳之前，前綴裡既有的同名檔先隔離。

    這一批 key 是這一輪才建出來的（`store.annex_keys() - keys_before`），遠端上
    不可能有正當的同名檔，所以既有的那些都是預先放置的。順帶讓 rclone 沒有東西
    可以「因為已存在而跳過上傳」（M3）。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    key_a, key_b = _raw_key(b"round-a"), _raw_key(b"round-b")
    keep = drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    other = drive.seed_file(
        prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH,
    )
    preplaced_a = drive.seed_file(
        prefix, key_a, b"round-a", created_time=EARLIER_THAN_PUSH,
    )
    preplaced_b = drive.seed_file(prefix, key_b, b"round-b", created_time=EARLIER_THAN_PUSH)
    decoy = drive.seed_file(prefix, _raw_key(b"other-round"), b"other-round")

    listing = RepoListing(
        prefix_folder_id=prefix,
        files=tuple(c for c in drive.list_children(prefix) if not c.is_folder),
        subfolders=(),
    )
    decs = plan_upload_exclusive(listing, [key_a, key_b], stage="第 8 步上傳前")
    decided = {d.file.id: d.disposition for d in decs}
    assert decided[preplaced_a] == Disposition.QUARANTINE
    assert decided[preplaced_b] == Disposition.QUARANTINE
    assert len(decs) == 2, decs
    assert apply_sweep(
        decs, drive, quarantine_folder_id=quarantine, clock=FixedClock(CLOCK_T0),
        prefix_folder_id=prefix,
    ) == 2
    left = {f.name for f in drive.list_children(prefix)}
    assert key_a not in left and key_b not in left
    assert keep in [f.id for f in drive.list_children(prefix)]
    assert other in [f.id for f in drive.list_children(prefix)]
    assert decoy in [f.id for f in drive.list_children(prefix)], "不屬於這一輪的不動"


def test_h2_verify_upload_window_requires_a_copy_inside_the_window():
    """H2：第 8 步之後，每個新 key 都必須有一份建立時間落在窗裡的檔案。

    這一步就是 M3 的實測答案：如果 rclone 的 `checkpresent` 因為同名檔已存在而
    跳過上傳，前綴裡就一份窗內的檔都沒有 → 中止（不寫 pending、不 push）。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    body = b"round-raw"
    key = _raw_key(body)
    window = upload_window(
        parse_rfc3339(UPLOAD_START), parse_rfc3339(UPLOAD_END),
    )
    assert window is not None

    # 窗內有 → 通過
    drive.seed_file(prefix, key, body, created_time=UPLOAD_START)
    assert verify_upload_window(drive, prefix, [key], window) is None

    # 只有窗外的一份（住民預先放的）→ 重試之後中止
    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    drive2.seed_file(prefix2, key, body, created_time=EARLIER_THAN_PUSH)
    with pytest.raises(MismatchError, match="上傳時間窗"):
        verify_upload_window(drive2, prefix2, [key], window, attempts=2, retry_delay_s=0.0)

    # 完全沒有同名檔 → 也中止
    drive3 = FakeDrive()
    prefix3 = drive3.seed_folder("prefix")
    with pytest.raises(MismatchError, match="上傳時間窗"):
        verify_upload_window(drive3, prefix3, [key], window, attempts=1, retry_delay_s=0.0)

    # 沒有 key 要查 → 什麼都不做（空集合不該被擋）
    assert verify_upload_window(drive3, prefix3, [], window) is None


def test_h2_upload_window_parsing_is_fail_closed_on_broken_fields():
    """時間窗欄位壞掉 → 回 None（沒有證據），而且**不是**反過來推一個窗出來。"""
    assert upload_window(None, UPLOAD_END) is None
    assert upload_window(UPLOAD_START, None) is None
    assert upload_window("not-a-time", UPLOAD_END) is None
    assert upload_window(UPLOAD_START, "not-a-time") is None
    assert upload_window(UPLOAD_END, UPLOAD_START) is None, "順序不對也不猜"
    assert upload_window(UPLOAD_START, UPLOAD_END) is not None


def test_git_pin_store_roundtrips_the_h1_and_h2_pending_fields(
    bundles: dict, tmp_path: Path,
):
    """H1／H2 的三個欄位要真的進得了 pin repo、也讀得回來（缺席 ≠ null）。

    這三個欄位是 settle／sweep／verify 唯一的權威證據，寫不進去就等於整套規則
    退回去用 `createdTime` 猜——所以 pin store 的往返必須被釘住。
    """
    url = _bare_repo(tmp_path / "pin.git")
    pins = GitPinStore(url, tmp_path / "wd")
    st = _state(bundles, manifest_sha=bundles["m1sha"])
    pins.promote(st)

    pend = _uploaded_pending(bundles, frozenset({"SHA256E-s3--aa"}))
    pend = replace(pend, expected_manifest_sha256=bundles["m2sha"])
    pins.write_pending(pend)
    _loaded, got = pins.load("agora")
    assert got == pend
    assert got.expected_manifest_sha256 == bundles["m2sha"]
    assert got.upload_window == (UPLOAD_START, UPLOAD_END)

    # 舊格式的 pending（push 之前就中斷、還沒補寫）讀起來是「沒有這個證據」，
    # 而不是「證據是 null」——settle 靠這個區分決定要不要 fail-closed。
    pending_json = pins.workdir / ".pin" / "agora.pending.json"
    raw = json.loads(pending_json.read_text(encoding="utf-8"))
    stripped = {
        k: v for k, v in raw.items()
        if k not in ("expected_manifest_sha256", "upload_window_start", "upload_window_end")
    }
    pending_json.write_text(
        json.dumps(stripped, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    pins._commit_and_push("pin(agora): drop the post-push fields", repo="agora")
    fresh = GitPinStore(url, tmp_path / "wd-fresh")
    _loaded, old = fresh.load("agora")
    assert old is not None
    assert old.expected_manifest_sha256 is None
    assert old.upload_window_start is None
    assert old.upload_window is None
    # 而且新寫回去的 pending 也不會憑空生出那些鍵
    fresh.write_pending(replace(pend, expected_manifest_sha256=None,
                                upload_window_start=None, upload_window_end=None))
    written = json.loads(
        (fresh.workdir / ".pin" / "agora.pending.json").read_text(encoding="utf-8")
    )
    assert "expected_manifest_sha256" not in written
    assert "upload_window_start" not in written
    assert "upload_window_end" not in written


def test_h2_verify_upload_window_refuses_to_guess_without_a_window(bundles: dict) -> None:
    """H2：沒有量到時間窗卻有 key 要查 → 中止，不用 createdTime 猜。"""
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    body = b"round-raw"
    key = _raw_key(body)
    drive.seed_file(prefix, key, body, created_time=BEFORE_PUSH)
    with pytest.raises(MismatchError, match="沒有量到上傳時間窗"):
        verify_upload_window(drive, prefix, [key], None)
    # 沒有 key 要查時，空的時間窗不算問題（不該為了沒有東西而中止一輪）
    assert verify_upload_window(drive, prefix, [], None) is None


def test_h2_sweep_does_not_fall_back_to_earliest_when_the_window_is_missing(
    bundles: dict,
) -> None:
    """H2：pending 記得到 key 卻沒有時間窗 → 有副本也一樣不搬移。

    這是從舊格式 pending（pin repo 裡還沒有上傳時間窗的那種）轉換過來的缺口。
    那種 pending 撐不到下一輪就會被 settle 結算掉，但在那之前 sweep 必須站在安全
    的那一邊：預先放置的那份建立得更早，「最早者勝」正是住民要的。
    """
    body = b"a-raw-record-for-this-round"
    key = _raw_key(body)
    st = _sweep_state(bundles)
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    drive.seed_file(prefix, MANIFEST_NAME, bundles["m1"], created_time=BEFORE_PUSH)
    drive.seed_file(prefix, bundles["b1"], bundles["b1_bytes"], created_time=BEFORE_PUSH)
    preplaced = drive.seed_file(
        prefix, key, body, created_time=EARLIER_THAN_PUSH, modified_time=AFTER_PUSH,
    )
    late = drive.seed_file(
        prefix, key, body, created_time=AFTER_PUSH, modified_time=AFTER_PUSH,
    )
    decs = _decide(
        drive, st, prefix=prefix,
        # annex_keys 有這個 key，但沒有 upload_window_*（舊格式 pending）
        policy=SweepPolicy(
            pending=_uploaded_pending(
                bundles, frozenset({key}), start=None, end=None,
            ),
            now=parse_rfc3339(CLOCK_T0),
        ),
    )
    assert decs[preplaced].disposition == Disposition.NEED_ADMIN
    assert decs[late].disposition == Disposition.NEED_ADMIN
    assert "上傳時間窗" in decs[preplaced].reason


# ---------------------------------------------------------------------------
# review-final M1 b：被釘選的 key 不見時，先到隔離區自動搬回來
# ---------------------------------------------------------------------------


def _key_state(key: str) -> PinState:
    """釘選值只記載一個 annex key 的最小狀態（這個測試不需要真的 bundle）。"""
    return PinState(
        repo="agora", repo_uuid=UUID,
        refs={"refs/heads/main": "c" * 40},
        manifest_sha256="0" * 64,
        prev_manifest_sha256=None,
        active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset({key}),
        promoted_at="2026-09-27T00:00:00Z", run_id="run-1",
    )


def test_m1_pin_key_check_moves_the_object_back_from_quarantine():
    """M1 b：key 在前綴裡不見了，但隔離區有一份 sha256／size 都相符的 → 自動搬回。

    這是 review-final 的失敗情境的收尾：住民預先放一份同名同內容的物件贏了「保留
    者」，提交流程上傳的那份被隔離，他再刪掉自己那份——被釘選的 key 就從 Drive 上
    消失，`verify_pin_keys_on_drive` 每一輪都中止、不會自己好，而那份 raw 已經是
    Agora 的真本。

    內容定址（key 的形狀是 `SHA256E-s<size>--<sha256>`），所以搬回來的一定是對的位元
    組；這等於 recovery runbook 模式 0 的自動版，impl1 那一類事故也會自己好。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    day = drive.create(quarantine, "2026-09-27", b"", mime_type="application/vnd.google-apps.folder")
    body = b"the canonical raw record"
    key = _raw_key(body)
    quarantined = drive.seed_file(day.id, key, body, created_time="2026-09-27T03:00:00Z")

    restored = verify_pin_keys_on_drive(
        drive, prefix, _key_state(key), quarantine_folder_id=quarantine,
    )

    assert [r.key for r in restored] == [key]
    assert restored[0].quarantine_file_id == quarantined
    assert restored[0].dry_run is False
    # 檔案真的回到前綴裡（parents 是前綴），而且隔離區那邊沒有它了
    moved = drive.get(quarantined)
    assert moved.parents == (prefix,), moved.parents
    assert not [
        f for f in drive.list_children(day.id) if f.id == quarantined
    ]
    # 再查一次當然過（這一輪照常繼續，沒有中止）
    assert verify_pin_keys_on_drive(
        drive, prefix, _key_state(key), quarantine_folder_id=quarantine,
    ) == ()


def test_m1_pin_key_check_ignores_a_quarantine_copy_whose_bytes_differ():
    """只有 sha256 **與** size 都相符才算：內容不符的同名檔不是被搬回的對象。

    這是自癒唯一的安全邊界——搬回一個位元組不同的檔等於往真本裡塞東西。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    day = drive.create(quarantine, "2026-09-27", b"", mime_type="application/vnd.google-apps.folder")
    body = b"the canonical raw record"
    key = _raw_key(body)
    # 同名、但位元組不同（Drive 的 sha256 與 key 內嵌的不符）
    drive.seed_file(day.id, key, body + b" tampered", created_time="2026-09-27T03:00:00Z")

    with pytest.raises(MismatchError) as excinfo:
        verify_pin_keys_on_drive(
            drive, prefix, _key_state(key), quarantine_folder_id=quarantine,
        )

    assert "隔離區裡也沒有" in str(excinfo.value)
    assert [f.name for f in drive.list_children(prefix)] == [], "一個都不能搬進前綴"


def test_m1_pin_key_check_says_the_quarantine_looked_empty_before_it_gives_up():
    """隔離區也沒有 → 照原樣中止，而且訊息要說明「隔離區也沒有」。

    隔離區 7 天後會被 purge，所以「隔離區沒有」和「被刪掉」對管理者的意義完全不同：
    前者還來得及照 recovery runbook 模式 0 處理，後者只能重建釘選值。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    key = _raw_key(b"the canonical raw record")

    with pytest.raises(MismatchError) as excinfo:
        verify_pin_keys_on_drive(
            drive, prefix, _key_state(key), quarantine_folder_id=quarantine,
        )

    message = str(excinfo.value)
    assert "隔離區裡也沒有" in message
    assert "7 天後會被 purge" in message


def test_m1_pin_key_check_without_a_quarantine_folder_stays_fail_closed():
    """沒有傳隔離區 id（舊呼叫端）→ 行為與原本完全一樣：中止，不猜。

    自癒是**加上去**的，不是取代掉原有的中止；拿不到隔離區就只能照舊。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    key = _raw_key(b"the canonical raw record")

    with pytest.raises(MismatchError) as excinfo:
        verify_pin_keys_on_drive(drive, prefix, _key_state(key))

    assert "隔離區裡也沒有" not in str(excinfo.value)
    assert key in str(excinfo.value)


def test_m1_pin_key_check_dry_run_records_the_restore_without_moving_anything():
    """dry-run 不搬檔（`--dry-run` 不得寫入 Drive），但要記下會搬哪幾個。

    沒有記錄的話，dry-run 就看不出「這一輪其實可以自己好」；而因為什麼都沒搬，
    這一輪仍然照原樣中止（方向是 fail-closed）。
    """
    from aistorage.integrity.verify import restore_pinned_keys_from_quarantine

    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    quarantine = drive.seed_folder("quarantine")
    body = b"the canonical raw record"
    key = _raw_key(body)
    quarantined = drive.seed_file(quarantine, key, body, created_time="2026-09-27T03:00:00Z")

    planned = restore_pinned_keys_from_quarantine(
        drive, quarantine, prefix, [key], dry_run=True,
    )
    assert [(r.key, r.dry_run) for r in planned] == [(key, True)]
    assert drive.get(quarantined).parents == (quarantine,), "dry-run 不得搬任何東西"

    with pytest.raises(MismatchError) as excinfo:
        verify_pin_keys_on_drive(
            drive, prefix, _key_state(key), quarantine_folder_id=quarantine, dry_run=True,
        )
    assert "dry-run" in str(excinfo.value)
    assert drive.get(quarantined).parents == (quarantine,)

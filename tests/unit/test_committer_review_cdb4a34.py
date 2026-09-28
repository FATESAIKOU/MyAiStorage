"""review-cdb4a34 的 M1／M3 ＋ 仍適用於單一 Agora 的 L 項。

ADR 0009 之後提交流程只處理一個實體（Agora），所以 M2（多 repo 的「讀不到＝沒有
設定」）、H2（Foundry 那側的分派）都隨多 repo 一起移除；留下來的是：

- **M1**：維護旗標的重查必須讀**遠端**。只讀本機 clone 時，write_pending 前的
  重查讀到的是第 3 步 `pins.load` 那時的狀態（近乎恆真），promote 前的重查看不到
  push 期間才上的鎖——「管理操作進行中不得 push」在真正危險的時機失效。
  推送釘選值時若發現遠端出現 `.maintenance`，要以 `AbortRun("maintenance")` 中止。
- **M3**：Drive 允許同一資料夾有多個同名檔（1.4i 實測）。舊的 `{name: file}` 讓
  後者覆蓋前者，於是住民放一個同名垃圾檔就能讓每一輪都 `MismatchError` 中止
  （零成本 DoS）。修法有兩道：同名時**任一**檔 checksum＋size 相符就算存在，
  以及第 5 步改用 sweep **之後**重新列舉的 listing。
- **L**：`__main__` 在中止時必須非零結束；快取命中的拒收不重新下載；旗標「讀不到」
  與「有維護中」分開報告；`promote` 必須清掉遠端的 pending（管理者的 swap 走這條）。

每支都附能重現**原問題**的測試（修正前應該失敗或本來就該被擋）。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from aistorage.annex.fake import FakeAnnexGit
from aistorage.committer.config import CommitterConfig
from aistorage.committer.run import Deps, RunReport, run
from aistorage.drive.fake import FakeDrive
from aistorage.errors import AbortRun, MismatchError, ReadError
from aistorage.integrity.pin import (
    GitPinStore,
    PinPending,
    PinState,
)
from aistorage.integrity.verify import (
    verify_new_keys_on_drive,
    verify_pin_keys_on_drive,
)

from test_committer_readview_wiring import _env

REPO_UUID = "00000000-0000-0000-0000-000000000001"


# ---------------------------------------------------------------- M3 的小工具


def _annex_key(payload: bytes) -> str:
    return f"SHA256E-s{len(payload)}--{hashlib.sha256(payload).hexdigest()}"


def _pin_state(**overrides: Any) -> PinState:
    base = dict(
        repo="agora",
        repo_uuid=REPO_UUID,
        refs={"refs/heads/main": "a" * 40, "refs/heads/git-annex": "b" * 40},
        manifest_sha256="0" * 64,
        prev_manifest_sha256=None,
        active_bundles=(),
        removed_bundles=frozenset(),
        annex_keys=frozenset(),
        promoted_at="2026-09-28T08:00:00Z",
        run_id="run-init",
    )
    base.update(overrides)
    return PinState(**base)


# ============================================================== M3：同名檔


def test_same_name_junk_file_does_not_break_the_pin_key_check() -> None:
    """M3：釘選值記載的物件在 Drive 上，旁邊有一個**同名**的垃圾檔。

    舊的寫法是 `{f.name: f}`，後者覆蓋前者 → 正好選到垃圾檔 → 判成「內容不符」
    → 每一輪都中止。改成「任一檔 checksum＋size 相符就算存在」後就過。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    payload = b"the real annexed object"
    key = _annex_key(payload)
    real_id = drive.seed_file(prefix, key, payload)
    # 同名的垃圾檔，**排在後面**（舊寫法一定選到它）
    junk_id = drive.seed_file(prefix, key, b"junk")

    verify_pin_keys_on_drive(drive, prefix, _pin_state(annex_keys=frozenset({key})))

    assert {drive.get(real_id).name, drive.get(junk_id).name} == {key}, (
        "兩個同名檔都要真的在 Drive 上")


def test_pin_key_check_still_fails_when_no_candidate_matches() -> None:
    """M3 的反向：同名檔 checksum／size 都不符 → 真的要擋（不得放水）。"""
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    payload = b"the real annexed object"
    key = _annex_key(payload)
    drive.seed_file(prefix, key, b"junk one")
    drive.seed_file(prefix, key, b"junk two")

    with pytest.raises(MismatchError, match="內容不符"):
        verify_pin_keys_on_drive(drive, prefix, _pin_state(annex_keys=frozenset({key})))


def test_new_key_check_accepts_any_matching_same_name_file() -> None:
    """M3：`verify_new_keys_on_drive` 同樣是「任一檔相符就算存在」。

    它的 listing 是 push 之後新列的，所以新 key 的名稱住民算得出來——同名注入
    一樣能打中它（review-cdb4a34 M3 的第二段）。
    """
    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    payload = b"a freshly pushed object"
    key = _annex_key(payload)
    drive.seed_file(prefix, key, b"look-alike")
    drive.seed_file(prefix, key, payload)

    assert verify_new_keys_on_drive(drive, prefix, {key}) == 1


def test_run_uses_the_prefix_listing_from_after_the_sweep(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M3：第 5 步要比對 sweep **之後**的前綴。

    情境：住民在前綴放一個與釘選 key 同名的垃圾檔。第 4 步的 sweep 會把它隔離
    （雜湊不符 → QUARANTINE），但第 5 步若還用 sweep 之前的 listing，就會把
    「真的那份物件明明還在」判成不存在 → 整輪中止，而且每輪重放一次就是零成本
    的 DoS。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    payload = b"annexed object recorded in the pin"
    key = _annex_key(payload)
    real_id = drive.seed_file(extra["prefix_folder_id"], key, payload)
    junk_id = drive.seed_file(extra["prefix_folder_id"], key, b"junk")
    # 讓正式釘選值記住這個 key（其餘欄位沿用 _env() 產生的那一筆）
    promoted = deps.pins.load("agora")[0]
    deps.pins = type(deps.pins)(
        initial_state=dataclasses.replace(promoted, annex_keys=frozenset({key})))

    # 除了「沒有中止」，還要確認第 5 步**看到的** listing 是 sweep 之後的
    # （只看結果的話，「任一檔相符就算存在」那條也會讓它通過）。
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    seen_listing: list[Any] = []
    original = run_mod.verify_pin_keys_on_drive

    def _spy(drive_, prefix, state, *, repo_listing=None):
        seen_listing.append(repo_listing)
        return original(drive_, prefix, state, repo_listing=repo_listing)

    monkeypatch.setattr(run_mod, "verify_pin_keys_on_drive", _spy)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert len(seen_listing) == 1 and seen_listing[0] is not None
    seen_ids = {f.id for f in seen_listing[0].files}
    assert junk_id not in seen_ids, (
        "第 5 步的 listing 必須是 sweep 之後的（垃圾檔已被隔離，不該還在裡面）")
    assert real_id in seen_ids, "真的那份物件一定要在 listing 裡"
    # 垃圾檔確實被隔離了（不是被忽略掉）
    assert report.counts.get("quarantined_files", 0) >= 1
    assert drive.get(junk_id).name == key


# ======================================================== M1：維護旗標讀遠端


def _seeded_bare_pin_repo(tmp_path: Path) -> Path:
    """一個有第一個 commit 的本機 bare pin repo（不碰網路）。"""
    remote = tmp_path / "pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(Path.home()),
        "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.com",
        "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=seed, env=env, check=True)
    (seed / ".pin").mkdir()
    (seed / ".pin" / "README").write_text("pin repo\n", encoding="utf-8")
    subprocess.run(["git", "add", ".pin"], cwd=seed, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=seed, env=env, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed, env=env, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, env=env, check=True)
    return remote


def _push_flag_to_remote(remote: Path, tmp_path: Path, relpath: str, content: str) -> None:
    """**不經過** store，直接在遠端加上檔案（模擬另一位管理者）。"""
    work = tmp_path / f"other-{relpath.replace('/', '-')}"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(Path.home()),
        "GIT_AUTHOR_NAME": "admin", "GIT_AUTHOR_EMAIL": "admin@example.com",
        "GIT_COMMITTER_NAME": "admin", "GIT_COMMITTER_EMAIL": "admin@example.com",
    }
    subprocess.run(["git", "clone", "-q", str(remote), "."], cwd=work, env=env, check=True)
    target = work / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", relpath], cwd=work, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "admin: maintenance"], cwd=work, env=env, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=work, env=env, check=True)


def test_read_text_sees_a_flag_pushed_after_our_clone(tmp_path: Path) -> None:
    """M1：旗標必須讀**遠端**。

    舊的 `read_text` 只 `_ensure_cloned()`（不 fetch），所以 clone 之後才在遠端
    放上的旗標完全看不到——而這正是「管理者在我們 clone 之後上鎖」的情況。
    """
    remote = _seeded_bare_pin_repo(tmp_path)
    store = GitPinStore(f"file://{remote}", tmp_path / "work", allow_production=True)

    # 先讀一次：觸發 clone，此時遠端還沒有旗標
    assert store.read_text(".pin/agora.maintenance") is None

    _push_flag_to_remote(
        remote, tmp_path, ".pin/agora.maintenance",
        json.dumps({"reason": "erase", "at": "2026-09-28T09:00:00Z", "by": "admin"}) + "\n",
    )

    raw = store.read_text(".pin/agora.maintenance")
    assert raw is not None, "遠端已經有旗標，讀遠端就必須看得到"
    assert json.loads(raw)["reason"] == "erase"


def test_read_text_raises_when_the_fetch_fails(tmp_path: Path) -> None:
    """M1：fetch 失敗一律 raise（不得當成「沒有維護中」）。"""
    remote = _seeded_bare_pin_repo(tmp_path)
    store = GitPinStore(f"file://{remote}", tmp_path / "work", allow_production=True)
    assert store.read_text(".pin/agora.maintenance") is None  # 先 clone

    real_run_git = store._run_git

    def _fail_fetch(args, *, check=True):
        if args[:2] == ["fetch", "origin"]:
            class _Proc:
                returncode = 128
                stdout = ""
                stderr = "fatal: could not read from remote repository"
            return _Proc()
        return real_run_git(args, check=check)

    store._run_git = _fail_fetch  # type: ignore[assignment]
    with pytest.raises(ReadError, match="fetch 失敗"):
        store.read_text(".pin/agora.maintenance")


def test_write_pending_aborts_when_the_remote_gained_a_maintenance_flag(
        tmp_path: Path) -> None:
    """M1：我們 clone 之後管理者上鎖 → 推送釘選值時要 `AbortRun("maintenance")`。

    這是「上一次重查之後、push 之前」上鎖的最後一道防線。舊的寫法以
    `WriteError`（同一個 repo 的檔案被遠端改動）中止，方向安全但語意不對：
    回報寫成錯誤而不是 `maintenance=active`。
    """
    remote = _seeded_bare_pin_repo(tmp_path)
    store = GitPinStore(f"file://{remote}", tmp_path / "work", allow_production=True)
    assert store.read_text(".pin/agora.maintenance") is None  # 先 clone

    _push_flag_to_remote(
        remote, tmp_path, ".pin/agora.maintenance",
        json.dumps({"reason": "erase", "at": "2026-09-28T09:00:00Z", "by": "admin"}) + "\n",
    )

    pending = PinPending(
        repo="agora", base_manifest_sha256="0" * 64, refs={},
        annex_keys=frozenset(), written_at="2026-09-28T09:05:00Z", run_id="run-x",
    )
    with pytest.raises(AbortRun) as excinfo:
        store.write_pending(pending)
    assert excinfo.value.step == "maintenance"
    assert excinfo.value.code == "active"


# ==================================== L：中止時非零結束／旗標狀態分開／pending


def test_committer_cli_exits_non_zero_when_the_round_aborts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L：`report.aborted_at` 不為空時，`python -m aistorage.committer run` 必須非零。

    否則 Actions 顯示綠燈，管理操作期間的「整輪不做任何事」與真正的失敗看起來
    一樣。
    """
    import aistorage.committer.__main__ as committer_main

    cfg_path = tmp_path / "c.json"
    cfg_path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora",
        "repo_uuid": "uuid-agora", "repo_url": "annex::agora",
        "prefix_folder_id": "p", "quarantine_folder_id": "q",
        "identity_registry_path": "config/identity.json",
    }), encoding="utf-8")
    monkeypatch.setattr(committer_main, "build_production_deps", lambda cfg, **kw: object())

    aborted = RunReport(run_id="r1", aborted_at="git.push", code="MismatchError")
    monkeypatch.setattr(committer_main, "run", lambda cfg, deps, dry_run=False: aborted)
    assert committer_main.main(["run", "--config", str(cfg_path)]) == 1

    ok = RunReport(run_id="r2")
    monkeypatch.setattr(committer_main, "run", lambda cfg, deps, dry_run=False: ok)
    assert committer_main.main(["run", "--config", str(cfg_path)]) == 0

    # 維護中：第 1b 步就結束，這一輪沒有中止（它正確地什麼都沒做）→ 0
    maintenance = RunReport(run_id="r3", maintenance="active", maintenance_reason="erase")
    monkeypatch.setattr(committer_main, "run",
                        lambda cfg, deps, dry_run=False: maintenance)
    assert committer_main.main(["run", "--config", str(cfg_path)]) == 0


def test_flag_read_failure_is_reported_as_unreadable_not_active(tmp_path: Path) -> None:
    """L：旗標**讀不到**與**有維護中**要分開報告。

    `active` 是「有人在管理操作，之後會解除」；`unreadable` 是「pin repo 連不上
    或 fetch 失敗，要人處理」。混在一起會讓 6.3 的健康檢查把連線故障當成正常的
    維護窗口。
    """
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _env(tmp_path)
    real_pins = deps.pins
    calls: list[str] = []

    class _Unreadable:
        def __getattr__(self, name):
            return getattr(real_pins, name)

        def read_text(self, relpath: str) -> str | None:
            calls.append(relpath)
            raise ReadError("pin repo 連不上")

    deps.pins = _Unreadable()  # type: ignore[assignment]
    report = run(cfg, deps, dry_run=False)

    assert report.aborted_at == "maintenance"
    assert report.maintenance == "unreadable", "讀不到 ≠ 有維護中"
    assert report.code == "ReadError"
    assert calls and calls[0] == maintenance_relpath("agora")


def test_pipeline_recheck_reports_unreadable_separately(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """L：pipeline 內的重查（第 1b 步之後才壞掉）也要分開報告。"""
    import importlib

    from aistorage.admin.lock import maintenance_relpath

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env(tmp_path)
    real_pins = deps.pins
    reads: list[str] = []

    class _Flaky:
        def __getattr__(self, name):
            return getattr(real_pins, name)

        def read_text(self, relpath: str) -> str | None:
            reads.append(relpath)
            if len(reads) > 1:
                raise ReadError("fetch 失敗")
            return real_pins.read_text(relpath)

    deps.pins = _Flaky()  # type: ignore[assignment]
    report = run(cfg, deps, dry_run=False)

    assert report.aborted_at == "maintenance"
    assert report.maintenance == "unreadable"
    assert report.code == "ReadError", "中止原因是讀不到，不是「有人在維護」"
    assert reads[-1] == maintenance_relpath("agora")


def test_promote_clears_a_pending_the_local_clone_never_saw(tmp_path: Path) -> None:
    """L：管理者的 swap（SWAP_PIN → `init-pin --confirm` → `promote`）必須清掉 pending。

    情境：提交流程寫了 pending、在 promote 前被維護旗標擋下（pending 留在遠端），
    之後管理者重建釘選值。`promote()` 原本只 `_ensure_cloned()`：本機 clone 是
    舊的（clone 時 pending 還不存在）→ 刪不到遠端那份 → pending 留著 → 下一輪
    settle 撞上「遠端既不是 pending 也不是正式釘選值」而卡住。
    """
    remote = _seeded_bare_pin_repo(tmp_path)
    stale = GitPinStore(f"file://{remote}", tmp_path / "stale", allow_production=True)
    assert stale.read_text(".pin/agora.maintenance") is None  # stale 先 clone

    other = GitPinStore(f"file://{remote}", tmp_path / "other", allow_production=True)
    other.write_pending(PinPending(
        repo="agora", base_manifest_sha256="0" * 64, refs={},
        annex_keys=frozenset(), written_at="2026-09-28T09:05:00Z", run_id="run-x",
    ))
    assert other.read_text(".pin/agora.pending.json") is not None, "遠端確實有 pending"

    stale.promote(_pin_state(), maintenance_ok=True)

    assert other.read_text(".pin/agora.pending.json") is None, (
        "遠端的 pending 必須被這次 promote 清掉")
    assert other.read_text(".pin/agora.pending.keys") is None
    assert other.read_text(".pin/agora.json") is not None


def test_promote_refuses_to_rebase_onto_a_maintenance_flag(tmp_path: Path) -> None:
    """M1／L：`promote` 不得默默 rebase 到帶著維護旗標的遠端上。

    守門有兩層：run.py 第 12 步的旗標重查（讀遠端），以及 store 這一層。
    只有管理者自己在 `AdminLock` 裡的 init-pin／swap 傳 `maintenance_ok=True`。
    """
    remote = _seeded_bare_pin_repo(tmp_path)
    store = GitPinStore(f"file://{remote}", tmp_path / "work", allow_production=True)
    assert store.read_text(".pin/agora.maintenance") is None

    _push_flag_to_remote(
        remote, tmp_path, ".pin/agora.maintenance",
        json.dumps({"reason": "erase", "at": "2026-09-28T09:00:00Z", "by": "admin"}) + "\n",
    )

    with pytest.raises(AbortRun) as excinfo:
        store.promote(_pin_state())
    assert excinfo.value.step == "maintenance"
    assert excinfo.value.code == "active"


# ============================== L：快取命中的拒收不重新下載（分派移除後）


def test_cached_rejection_does_not_download_the_sidecar_again(tmp_path: Path) -> None:
    """L：已被拒收快取命中的項目，下一輪不得再下載它的 sidecar／sig。

    理由：那兩個檔寫入者隨時可以刪掉（`NotFound`）。舊的 `verify_all` 在評估之前
    就對**每一個**項目下載 sidecar 與 sig，所以只要有任何一個已刪的項目留在收件匣
    裡（DEFER、寫入者自己放 junk……），**整輪**在任何套用之前就中止，而且每輪
    都一樣。評估裡的拒收快取短路本來就排在驗章之前，順序要保持住。
    """
    from aistorage.drive.fake import FakeDrive
    from aistorage.intake.evaluate import evaluate
    from aistorage.intake.ledger import Ledger
    from aistorage.intake.scan import scan_inboxes
    from aistorage.agora.store import AgoraStore, FakeRawStorage

    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    scan = scan_inboxes(drive, deps.registry)
    assert scan.items, "_env() 應該有放一個項目"

    worktree = tmp_path / "store"
    worktree.mkdir(parents=True, exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "st")

    item = scan.items[0]
    cand_ids = sorted([f.id for f in item.sidecars] + [f.id for f in item.sigs])
    # 先寫一筆「同一個收件匣資料夾、完全相同的候選檔案組合」的拒收快取
    # （形狀與 evaluate 自己寫出的一致：entries 裡一筆一筆）
    store.put_json(
        _rejection_rel(item.item_key),
        {"item_key": item.item_key, "code": "bad_signature",
         "at": "2026-09-27T09:00:00Z",
         "inbox_folder_id": item.inbox_folder_id,
         "candidate_ids": cand_ids,
         "entries": [{"inbox_folder_id": item.inbox_folder_id,
                      "candidate_ids": cand_ids,
                      "code": "bad_signature",
                      "at": "2026-09-27T09:00:00Z"}]},
    )
    # 這些檔在下一輪「寫入者已經刪掉了」→ 真的驗章一定會是 ReadError
    drive.inject("download_bytes", item.sidecars[0].id, error=FileNotFoundError, times=1)

    dec = evaluate(item, drive=drive, registry=deps.registry, store=store,
                   ledger=Ledger(store), clock=deps.clock, workdir=worktree)

    assert dec.kind.value == "reject"
    assert dec.code == "bad_signature", "沿用快取裡的代碼"
    assert dec.rejected_at == "2026-09-27T09:00:00Z", "沿用快取裡的時間（不重新評估）"


def _rejection_rel(item_key: str) -> str:
    from aistorage.agora import layout

    return layout.rejection_path(item_key)


# ============================================== 附帶：身分檢查仍要用 fake git


def test_verify_clone_identity_still_uses_the_config() -> None:
    """H1 的行為在這輪之後不變（順手迴歸保護）。"""
    from aistorage.committer.run import verify_clone_identity

    cfg = CommitterConfig(
        repo="agora", repo_uuid="u1", repo_url="drive://agora",
        prefix_folder_id="p", quarantine_folder_id="q",
        identity_registry_path="config/identity.json",
    )
    verify_clone_identity(FakeAnnexGit(repo_uuid="u1", repo_url="drive://agora"), cfg)
    with pytest.raises(MismatchError, match="remote.origin.url"):
        verify_clone_identity(FakeAnnexGit(repo_uuid="u1", repo_url="drive://x"), cfg)


def test_deps_git_factory_still_receives_the_config(tmp_path: Path) -> None:
    """`Deps.git_factory(dest, cfg)` 的第二個參數就是這輪的設定。"""
    cfg, deps, extra = _env(tmp_path)
    seen: list[str] = []
    original = deps.git_factory

    def _factory(dest, cfg_):
        seen.append(cfg_.repo)
        return original(dest, cfg_)

    deps.git_factory = _factory  # type: ignore[assignment]
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert set(seen) == {"agora"}

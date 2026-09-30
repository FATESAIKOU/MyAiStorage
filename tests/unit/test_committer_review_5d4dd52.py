"""review-5d4dd52 的接線測試：H1（pending 記內容雜湊）與 H2（上傳時間窗）。

純邏輯的部分（settle／sweep／verify 怎麼用那兩個證據）在 `test_integrity.py`；
這裡驗的是**提交流程真的會產生並且寫下它們**：

- **H1**：push 成功之後、`verify_after_push` 之前，`run()` 讀本機
  `.git/annex/git-remote-annex/<uuid>/manifest`、算出雜湊、**再寫一次 pending**。
  修正之前 pending 完全沒有這個欄位，於是 settle／sweep 只能退回
  「建立最早者勝」——那等於讓住民自己決定真身。
- **H2**：第 8 步 `git annex copy` 之前先把「這一輪要上傳的 key」既有的同名檔隔離，
  之後確認每個新 key 都有一份**建立時間落在上傳時間窗裡**的檔案。這一步同時是
  M3（rclone 的 `checkpresent` 會不會因為同名檔已存在而跳過上傳）的答案。
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

from aistorage.agora.store import AnnexRawStorage
from aistorage.annex.fake import FakeAnnexGit
from aistorage.clock import format_rfc3339
from aistorage.committer.run import run
from aistorage.integrity.pin import MemoryPinStore, PinPending

from test_committer_readview_wiring import _env

run_mod = importlib.import_module("aistorage.committer.run")

#: 收件匣裡那份 session 的 raw（與 test_committer_smoke._seed_valid_inbox_session 相同）
RAW = b'{"messages": [{"message_id": "m1"}, {"message_id": "m2"}]}'
RAW_KEY = f"SHA256E-s{len(RAW)}--{hashlib.sha256(RAW).hexdigest()}"
#: 住民預先放置的時間（比提交流程上傳早得多）
PREPLACED_AT = "2026-09-27T09:00:00Z"


class _RecordingPinStore(MemoryPinStore):
    """記下每一次 `write_pending` 寫了什麼（轉正之後 pending 就查不到了）。"""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.written: list[PinPending] = []

    def write_pending(self, pending: PinPending) -> None:
        self.written.append(pending)
        super().write_pending(pending)


class _UploadingGit(FakeAnnexGit):
    """`git annex copy` 真的把 annex 物件寫進 Drive。

    真的 `SubprocessAnnexGit` 是透過 git-remote-annex → rclone 做到的；`FakeAnnexGit`
    預設只更新本機的 location log。H2 的驗證需要前綴裡真的出現那份物件，所以這裡
    補上這個副作用（用 fake 自己的時鐘當建立時間，與 `upload_window` 量到的一致）。
    """

    def __init__(self, *a, **kw) -> None:
        self.uploaded_objects: list[str] = []
        # 真的 `git annex copy` 會更新本機的 location log（`annex_keys_in` 的來源），
        # 預設的 "noop" 會讓第 11 步的覆蓋率檢查拿到空集合。
        kw.setdefault("copy_effect", "annex_upload")
        super().__init__(*a, **kw)

    def copy(self, remote: str, to_copy: list[str] | None = None, *, timeout: float = 900.0):
        super().copy(remote, to_copy, timeout=timeout)
        if not to_copy:
            return
        now = format_rfc3339(self.clock.now(), include_fraction=True)
        for key in to_copy:
            data = self._fake_objects.get(key)
            if data is None:
                continue
            self.uploaded_objects.append(key)
            self.drive.seed_file(self.prefix_folder_id, key, data, created_time=now)


def _annex_env(tmp_path: Path):
    """`_env()` 的變體：用真的 `AnnexRawStorage`（走 FakeAnnexGit 的 lookupkey），
    所以這一轮真的會有「新上傳的 annex key」，H2 的路徑才跑得起來。"""
    cfg, deps, extra = _env(tmp_path)
    base = deps.git_factory(None, cfg)
    git = _UploadingGit(
        refs=dict(base.refs),
        workdir=base.workdir,
        annex_keys=frozenset(),
        drive=deps.drive,
        prefix_folder_id=cfg.prefix_folder_id,
        repo_uuid=cfg.repo_uuid,
        clock=deps.clock,
    )
    git.repo_url = cfg.repo_url

    def _factory(dest: Path, _target):
        # `AnnexRawStorage._lookup_key` 問假 git 時用的是**這一轮 clone 的目的地**
        # （相對路徑），所以假 git 的 workdir 必須跟著換，否則查不到 key。
        git.workdir = Path(dest)
        return git

    deps.git_factory = _factory
    deps.raw_storage_factory = lambda w, g: AnnexRawStorage(w, git=g)
    state, pending = deps.pins.load(cfg.repo)
    deps.pins = _RecordingPinStore(initial_state=state, initial_pending=pending)
    extra["git"] = git
    extra["base_git"] = base
    return cfg, deps, extra


# ================================================================== H1


def test_h1_run_records_the_pushed_manifest_hash_into_pending(tmp_path: Path) -> None:
    """H1：`run()` 真的算出並**再寫一次** pending，欄位值等於遠端上那份 manifest。

    斷言兩件事：(1) `write_pending` 被呼叫兩次（push 前一次、push 後補 `expected`
    一次）；(2) 補的那一次帶的雜湊 == 轉正後正式值的 `manifest_sha256`——也就是
    「這一輪 push 實際寫出去的那份」的位元組。
    """
    cfg, deps, extra = _env(tmp_path)
    pins = _RecordingPinStore(
        initial_state=deps.pins.load(cfg.repo)[0],
        initial_pending=deps.pins.load(cfg.repo)[1],
    )
    deps.pins = pins

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert len(pins.written) == 2, [w.expected_manifest_sha256 for w in pins.written]
    before_push, after_push = pins.written
    assert before_push.expected_manifest_sha256 is None, (
        "push 之前還不知道會寫出什麼位元組，那一次不該帶這個欄位"
    )
    promoted = pins.load(cfg.repo)[0]
    assert after_push.expected_manifest_sha256 == promoted.manifest_sha256, (
        "pending 記的必須是這一輪 push 實際寫出去的那份 manifest 的內容雜湊"
    )
    # 兩次的 refs／keys／基準必須一致（第二次只是補一個欄位）
    assert after_push.refs == before_push.refs
    assert after_push.annex_keys == before_push.annex_keys
    assert after_push.base_manifest_sha256 == before_push.base_manifest_sha256
    assert report.expected_manifest_recorded is not False


def test_h1_run_reports_when_the_local_manifest_is_unreadable(
    tmp_path: Path, monkeypatch,
) -> None:
    """讀不到本機 manifest → 不猜、不中止，但報告要說得出來（健康檢查看得到）。

    沒有這個欄位時 settle／sweep 會 fail-closed（多份內容不同的候選一律中止），
    但**單一內容的正常情況**不該因為讀不到一個本機快取檔就整輪失敗。
    """
    cfg, deps, extra = _env(tmp_path)
    pins = _RecordingPinStore(
        initial_state=deps.pins.load(cfg.repo)[0],
        initial_pending=deps.pins.load(cfg.repo)[1],
    )
    deps.pins = pins
    base = deps.git_factory(None, cfg)

    class _NoLocalManifest(FakeAnnexGit):
        def local_manifest_sha256(self, remote_uuid: str) -> str | None:
            return None

    blind = _NoLocalManifest(
        refs=dict(base.refs), workdir=base.workdir, drive=base.drive,
        prefix_folder_id=cfg.prefix_folder_id, repo_uuid=cfg.repo_uuid,
        clock=deps.clock,
    )
    blind.repo_url = cfg.repo_url
    deps.git_factory = lambda _p, _target: blind

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert report.expected_manifest_recorded is False
    assert len(pins.written) == 1, "讀不到就不該多寫一次"
    assert pins.written[0].expected_manifest_sha256 is None
    assert "expected_manifest=UNRECORDED" in report.format_log()


# ================================================================== H2


def test_h2_run_quarantines_the_preplaced_copy_then_uploads_inside_the_window(
    tmp_path: Path,
) -> None:
    """H2：住民預先放一份同名同內容的物件 → 那一輪照樣成功，而且真本只剩上傳的那份。

    沒有這兩道時的結局（review-5d4dd52 H2）：預先放置的那份建立得更早，sweep 的
    「建立最早者勝」會保留它、隔離提交流程上傳的那份；住民再刪掉自己那份，被釘選
    的 key 就從 Drive 上消失，`verify_pin_keys_on_drive` 每一輪都中止、不會自己好。
    """
    cfg, deps, extra = _annex_env(tmp_path)
    drive, git = deps.drive, extra["git"]
    prefix = cfg.prefix_folder_id
    # 住民預先放的那一份（位元組相同、建立得比上傳早得多）
    preplaced = drive.seed_file(
        prefix, RAW_KEY, RAW, created_time=PREPLACED_AT, modified_time=PREPLACED_AT,
    )

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code} {report.code}"
    assert git.uploaded_objects == [RAW_KEY], "這一輪確實有把新 key 傳上去"
    left = {f.name: f for f in drive.list_children(prefix)}
    assert RAW_KEY in left, "提交流程上傳的那一份必須留在真本裡"
    assert left[RAW_KEY].id != preplaced
    # 住民預先放的那一份被隔離（不是留在真本裡當真身）
    quarantined = [
        f for f in drive.list_children(cfg.quarantine_folder_id) if not f.is_folder
    ]
    for day in drive.list_children(cfg.quarantine_folder_id):
        if day.is_folder:
            quarantined += [f for f in drive.list_children(day.id) if not f.is_folder]
    assert preplaced in [f.id for f in quarantined], (
        "預先放置的同名檔必須在第 8 步之前就被隔離"
    )
    # 而且 pending 記下了上傳時間窗（下一輪的 sweep 用它認真身）
    pending_written = deps.pins.written
    assert pending_written
    assert pending_written[-1].upload_window == (
        format_rfc3339(deps.clock.now(), include_fraction=True),
        format_rfc3339(deps.clock.now(), include_fraction=True),
    )


def test_h2_run_aborts_when_the_upload_was_skipped(tmp_path: Path, monkeypatch) -> None:
    """H2／M3：上傳之後窗內一份都沒有 → 中止，**不寫 pending**。

    這就是 M3（review-903d7e2 留下的未實測項）的行為答案：如果 rclone 的
    `checkpresent` 因為前綴裡已有同名檔而跳過上傳，前綴裡唯一的那一份就是住民
    預先放的——所以這一輪必須停在第 8 步，而不是讓它變成真本。
    """
    cfg, deps, extra = _annex_env(tmp_path)
    drive = deps.drive

    class _SkippingGit(_UploadingGit):
        """rclone 的 checkpresent 說「已經有了」→ 完全不傳。"""

        def copy(self, remote, to_copy=None, *, timeout=900.0):
            FakeAnnexGit.copy(self, remote, to_copy, timeout=timeout)

    base = extra["base_git"]
    skipper = _SkippingGit(
        refs=dict(base.refs), workdir=base.workdir, drive=base.drive,
        prefix_folder_id=cfg.prefix_folder_id, repo_uuid=cfg.repo_uuid,
        clock=deps.clock,
    )
    skipper.repo_url = cfg.repo_url
    deps.git_factory = lambda dest, _target: (setattr(skipper, "workdir", Path(dest)), skipper)[1]
    # 第 8 步之前的隔離照做（所以前綴裡連一份同名檔都沒有 → 窗內當然也沒有）
    monkeypatch.setattr(run_mod, "verify_upload_window", _no_sleep_window_check)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.aborted_at == "annex.git.copy", report.aborted_at
    assert deps.pins.written == [], "中止時不能留下沒人負責的 pending"
    assert {f.name for f in drive.list_children(cfg.prefix_folder_id)} & {
        RAW_KEY
    } == set(), "中止的輪次不該把任何東西留在真本裡"


def _no_sleep_window_check(drive, prefix_folder_id, keys, window, **kw) -> None:
    """`verify_upload_window` 的零等待版本（測試不要真的睡 60 秒）。"""
    import aistorage.integrity.sweep as sweep_mod

    sweep_mod.verify_upload_window(
        drive, prefix_folder_id, keys, window, attempts=1, retry_delay_s=0.0,
    )

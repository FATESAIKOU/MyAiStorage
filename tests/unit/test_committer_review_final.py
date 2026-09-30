"""merge 前最後一次 review（review-final）的接線測試：M1 a／M1 b。

review-5d4dd52 的 H2 已經擋掉「住民**預先**放一份同名同內容的物件」。review-final
指出剩下的一條：**他可以在第 8 步上傳之後、promote 之前再放一份**——前綴裡當時沒有
同名檔，第 8 步的隔離什麼都沒搬，而 `verify_upload_window` 只要求窗內「至少有一份」。
於是下一輪 sweep 看到窗內有兩份。這裡兩件事各補一道：

- **M1 a**（純邏輯在 `test_integrity.py`）：窗內**兩份**時一個都不選、一個都不搬，
  全部 NEED_ADMIN。真本沒有消失（兩份都還在前綴裡），代價只是停擺等人判斷。
- **M1 b**：`verify_pin_keys_on_drive` 發現被釘選的 key 不見時，先到**隔離區**找
  sha256 與 size 都相符的檔，找到就**自動搬回**並照常繼續這一輪。內容定址，所以搬回
  來的一定是對的位元組；這是 recovery runbook 模式 0 的自動版。這裡驗的是**接線**：
  `run()` 有沒有把隔離區 id 傳進去、有沒有把搬了哪幾個記進報告。

另外 pin 回歸（review-final L）：`local_manifest_sha256` 依賴 git-remote-annex 的本機
快取路徑，那是實作細節——整合測試（`tests/integration/test_committer_dup_manifest.py`）
釘住「本機雜湊 == push 之後遠端那份的雜湊」，這裡釘住它在**沒有**對應快取檔時回
None（呼叫端要把它當「沒有這個證據」，而不是猜）。
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
import subprocess

from aistorage.committer.run import run

from test_committer_readview_wiring import _env


def _annex_key(payload: bytes) -> str:
    return f"SHA256E-s{len(payload)}--{hashlib.sha256(payload).hexdigest()}"


def _quarantined_files(drive, quarantine_folder_id: str) -> list:
    """隔離區（含日期子資料夾）裡的檔案。"""
    out = []
    for child in drive.list_children(quarantine_folder_id):
        if child.is_folder:
            out.extend(f for f in drive.list_children(child.id) if not f.is_folder)
        else:
            out.append(child)
    return out


# ============================================================== M1 b：自癒接線


def test_run_moves_a_wrongly_quarantined_pinned_key_back_and_says_so(
    tmp_path: Path,
) -> None:
    """M1 b：被釘選的 key 被誤隔離 → 這一輪**自己好**，而且報告裡看得見搬了誰。

    這是 review-final 的失敗情境的最後一段：住民贏了「保留者」、提交流程上傳的那份被
    隔離，他再刪掉自己那份，被釘選的 key 就從 Drive 上消失。修正之前，`verify_pin_
    keys_on_drive` 從此**每一輪**都中止、而且不會自己好——那份 raw 已經是 Agora 的
    真本，只能人工照 recovery runbook 模式 0 搬回來。

    自癒要看得見：反覆發生代表有東西在被誤隔離（impl1 那一類），所以搬了哪幾個寫進
    執行報告，不做無聲的復原。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive
    payload = b"the canonical raw record of this session"
    key = _annex_key(payload)
    prefix = cfg.prefix_folder_id

    # 釘選值記載這個 key，而真的那份物件被隔離了（隔離區的日期子資料夾裡還在）
    promoted = deps.pins.load(cfg.repo)[0]
    deps.pins = type(deps.pins)(
        initial_state=dataclasses.replace(promoted, annex_keys=frozenset({key})))
    quarantined = drive.seed_file(
        cfg.quarantine_folder_id, key, payload, created_time="2026-09-27T09:00:00Z",
    )
    assert _annex_key(drive._contents[quarantined]) == key

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert report.restored_from_quarantine == [key], (
        "自癒必須記進報告：反覆發生代表有東西在被誤隔離")
    assert report.counts.get("restored_from_quarantine") == 1
    assert f"restored=['{key}']" in report.format_log()
    # 檔案真的回到前綴裡了
    assert prefix in drive.get(quarantined).parents
    assert key in {f.name for f in drive.list_children(prefix)}


def test_run_does_not_restore_anything_when_the_prefix_is_intact(tmp_path: Path) -> None:
    """回歸保護：key 好好在前綴裡時不該有任何自癒的痕跡。

    否則「自動從隔離區搬回」就變成一條可以往真本裡塞東西的旁路。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive
    payload = b"a healthy annexed object"
    key = _annex_key(payload)
    drive.seed_file(cfg.prefix_folder_id, key, payload, created_time="2026-09-27T09:00:00Z")
    promoted = deps.pins.load(cfg.repo)[0]
    deps.pins = type(deps.pins)(
        initial_state=dataclasses.replace(promoted, annex_keys=frozenset({key})))
    # 隔離區裡放一份同名但位元組不同的垃圾檔（自癒必須無視它）
    junk = drive.seed_file(cfg.quarantine_folder_id, key, b"junk", created_time="2026-09-27T09:00:00Z")

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert report.restored_from_quarantine == []
    assert "restored=" not in report.format_log()
    assert cfg.quarantine_folder_id in drive.get(junk).parents, "垃圾檔不該被搬進前綴"


# ================================================== L（review-final）：本機 manifest


def test_local_manifest_sha256_is_none_without_the_git_remote_annex_cache(
    tmp_path: Path,
) -> None:
    """L：`local_manifest_sha256` 讀不到 git-remote-annex 的本機快取就回 None。

    路徑 `.git/annex/git-remote-annex/<uuid>/manifest` 是 git-annex 的**實作細節**
    （不是公開介面）。所以整合測試釘住「讀到的雜湊 == push 之後遠端那份的雜湊」，
    這裡釘住另一頭：路徑不在時回 None，呼叫端（run.py 第 11 步）要把它當成「沒有
    這個證據」——記下 `expected_manifest=UNRECORDED`，而不是猜一個值讓 settle／sweep
    照著錯的雜湊去認真本。
    """
    from aistorage.annex.git import SubprocessAnnexGit

    workdir = tmp_path / "clone"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=workdir, check=True,
                   capture_output=True)

    git = SubprocessAnnexGit(workdir)
    assert git.local_manifest_sha256("00000000-0000-0000-0000-000000000001") is None

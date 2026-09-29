"""前綴裡有位元組相同的重複主 manifest 時，push／clone 與提交流程的驗證都要正常。

review-903d7e2 H1 的前提（「rclone 對 manifest 原地更新，正常流程不會有第二份同名
manifest」）是錯的，所以這裡把它變成一個**會被持續檢查的事實**：

- rclone 每輪 push 都會重寫 `GITMANIFEST--<uuid>`，file id 會變（刪掉重建），
  所以那份的 `createdTime` 是「上一輪 push 的時間」，不是 repo 初始化的時間；
- 同一輪 push 內因為 Drive 的列表落後，前綴裡會留下**兩份同名、位元組相同**的
  主 manifest（兩個 rclone 版本都實測到：committer workflow 釘的 1.75.1 與
  Mac 上的 1.69.3）；
- 有這種重複時 `git push` 與 `git clone` 都成功，而且下一輪 push 會由 rclone 自己
  把多餘的那份清掉。

因此 `plan_sweep` 不搬任何一份（分不出真身，搬錯就是消滅真本），而
`verify_clone`／`precheck` 判的是「只有一種內容」而不是「恰好一個檔」。

（2026-09-30 實測，腳本見 decision-log 同一節；這支測試把結論釘住。）
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aistorage.integrity.pin import PinState
from aistorage.integrity.verify import precheck, verify_clone
from ._harness import build_annex_repo, git_env

pytestmark = pytest.mark.integration


def test_duplicate_identical_manifest_does_not_break_push_or_clone(
    real_drive, sandbox, it_settings, tmp_path: Path,
):
    name, prefix_id, _quarantine = sandbox.create()
    annex = build_annex_repo(
        prefix=name, workdir=tmp_path / "w",
        rclone_conf=Path(str(it_settings["rclone_conf"])),
        max_git_bundles=20, annex_object_sizes=(300,),
    )
    main_name = f"GITMANIFEST--{annex.uuid}"
    env = git_env(Path(str(it_settings["rclone_conf"])))
    repo = annex.workdir

    def manifests() -> list:
        return [f for f in real_drive.list_children(prefix_id) if f.name == main_name]

    official = manifests()
    assert official, "第一次 push 之後應該有主 manifest"
    data = real_drive.download_bytes(official[0].id, max_bytes=1 << 20)

    # 住民上傳一份位元組完全相同的副本（drive.file 對前綴有建檔權限，1.4 已實證）
    copy_id = real_drive.create(prefix_id, main_name, data).id
    assert len(manifests()) == 2

    # 有重複檔時 clone 正常
    clone = subprocess.run(
        ["git", "clone", "-b", "main", annex.url, str(tmp_path / "c1")],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert clone.returncode == 0, clone.stderr[-2000:]

    # 有重複檔時 push 正常（不會 Duplicate object 之類的失敗）
    (repo / "notes.md").write_text("dup\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.md"], cwd=repo, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "notes"], cwd=repo, env=env, check=True,
                   capture_output=True)
    push = subprocess.run(
        ["git", "push", "drive", "main", "git-annex"],
        cwd=repo, env=env, capture_output=True, text=True,
    )
    assert push.returncode == 0, push.stderr[-2000:]

    # 提交流程的驗證也接受「只有一種內容」的多份同名 manifest
    after = manifests()
    state = PinState(
        repo="agora", repo_uuid=annex.uuid,
        refs=_LsRemoteGit(annex).ls_remote(),
        manifest_sha256=after[0].sha256 or "",
        prev_manifest_sha256=None, active_bundles=(), removed_bundles=frozenset(),
        annex_keys=frozenset(), promoted_at="2026-09-30T00:00:00Z", run_id="probe",
    )
    verify_clone(_LsRemoteGit(annex), state, drive=real_drive, prefix_folder_id=prefix_id)
    precheck(real_drive, prefix_id, main_name, state)

    # 副本的建立時間晚於正式那份（createdTime 是 Drive 記錄的）
    by_id = {f.id: f for f in after}
    if copy_id in by_id:
        original = [f for f in after if f.id != copy_id]
        assert original, "副本之外應該還有正式那份"
        assert by_id[copy_id].created_time >= original[0].created_time


class _LsRemoteGit:
    """只支援 `ls_remote()` 的最小 AnnexGit（`verify_clone` 只需要它）。"""

    def __init__(self, annex) -> None:
        self._annex = annex

    def ls_remote(self) -> dict[str, str]:
        out = subprocess.run(
            ["git", "ls-remote", "drive"],
            cwd=self._annex.workdir, env=self._annex.env,
            capture_output=True, text=True, check=True,
        ).stdout
        refs: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name:
                refs[name.strip()] = sha.strip()
        return refs

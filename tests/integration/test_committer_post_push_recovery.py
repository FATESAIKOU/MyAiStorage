"""整合測試（impl1）：push 後驗證失敗 → 下一輪必須能自己回來，而且真本不會被搬走。

重現的現場（impl1 在 e2e 環境量到的固定序列）：

- 某一輪 push **成功**（遠端 refs／manifest／bundle 全部往前），但第 11 步
  `verify_new_keys_on_drive` 因為 Drive 的列表落後而誤判「剛上傳的物件不在」，
  於是整輪中止、**釘選值沒有轉正**，pending 留在 pin repo 裡。
- 下一輪的 settle 拿「push 落實之前」的畫面判斷 → 以為遠端沒動 → 丟掉 pending。
- 再下一輪：遠端已經領先釘選值，而且沒有任何東西能說明它。第 4 步的清扫於是
  把那份 manifest、它引用的新 bundle 與新 annex 物件全當注入物搬走 →
  `git clone` 從此 `No git repository found in this remote`，**沒有任何一輪能
  自己好**。

本測試在真的 Drive、真的 git-annex、真的 pin-test repo 上驗兩件事：

1. **能自己回來**：第 11 步失敗之後的下一輪，settle 把 pending 轉正，提交完成，
   釘選值追上遠端。
2. **不會消滅真本**：就算 pending 不見了（遠端仍領先釘選值），那一輪也只會中止，
   不會搬走 manifest／bundle；真本仍然 clone 得出來。

第 2 點另外用一支測試獨立驗證「掃描不會消滅釘選值還沒追上的真本」。
"""

from __future__ import annotations

import importlib

import pytest

from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.errors import MismatchError
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    RAW_S1,
    RAW_S2,
    S1,
    S2,
    _put_session,
    annex_git_factory,
    build_annex_repo,
    main_sha_of,
    make_signer,
    write_registry,
)

#: `aistorage.committer.__init__` 把 `run` 函式匯出成套件屬性，所以
#: `from aistorage.committer import run` 拿到的是函式而不是模組。要 monkeypatch
#: 模組裡的符號得用 importlib。
run_mod = importlib.import_module("aistorage.committer.run")

pytestmark = [pytest.mark.integration]

MAX_BUNDLES = 1


class _FailStep11Once:
    """讓第 11 步失敗一次（模擬 Drive 列表落後造成的誤判）。

    只擋 `verify_new_keys_on_drive`，其餘步驟完全不動：那一輪照樣 clone、
    照樣 `git annex copy`、照樣 `git push`——所以中止之後遠端是真的往前了，
    這正是要重現的狀態。
    """

    def __init__(self) -> None:
        self.failed = False
        # monkeypatch 會把模組屬性換成自己，所以真正的實作必須先抓好
        self._real = run_mod.verify_new_keys_on_drive

    def __call__(self, drive, prefix_folder_id, new_keys, **kwargs):
        if not self.failed:
            self.failed = True
            raise MismatchError(
                "這一輪新寫的 annex 物件沒有真的在 Drive 上（1 個）: "
                "['（測試注入：Drive 列表落後）']（已重新列舉 3 次）"
            )
        return self._real(drive, prefix_folder_id, new_keys, **kwargs)


def _files_under(drive, folder_id: str) -> set[str]:
    names: set[str] = set()
    stack = [folder_id]
    while stack:
        for child in drive.list_children(stack.pop()):
            if child.is_folder:
                stack.append(child.id)
            else:
                names.add(child.name)
    return names


def test_next_round_settles_the_pending_after_a_failed_post_push_check(
    it_settings, real_drive, sandbox, tmp_path, monkeypatch,
):
    """第 11 步失敗 → pending 留著、遠端領先 → 下一輪自己轉正並完成提交。"""
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=MAX_BUNDLES, annex_object_sizes=(300,),
    )
    pins = GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]), workdir=tmp_path / "pin",
        key_path=it_settings["pin_key"], known_hosts_path=it_settings["known_hosts"])
    repo_name = sandbox.pin_repo_name()
    sandbox.register_pin_store(pins)
    cfg = CommitterConfig(
        repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=MAX_BUNDLES)
    deps = Deps(
        drive=real_drive, pins=pins, git_factory=annex_git_factory(annex),
        registry=load_registry(signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock())
    init_pin_cli(cfg, deps, confirm=True)

    # ── 第 1 輪：正常成功 ───────────────────────────────────────────
    _put_session(real_drive, inbox_id, signer, RAW_S1, S1, "2026-09-27T08:00:00Z")
    r1 = run(cfg, deps, dry_run=False)
    assert r1.ok is True, f"第 1 輪中止於 {r1.aborted_at}:{r1.code}"
    state1, _ = pins.load(repo_name)
    assert state1.manifest_sha256

    # ── 第 2 輪：push 成功、第 11 步失敗 ────────────────────────────
    _put_session(real_drive, inbox_id, signer, RAW_S2, S2, "2026-09-27T09:00:00Z")
    boom = _FailStep11Once()
    monkeypatch.setattr(run_mod, "verify_new_keys_on_drive", boom)
    r2 = run(cfg, deps, dry_run=False)
    monkeypatch.undo()

    assert boom.failed, "測試沒有真的注入第 11 步的失敗"
    assert r2.ok is False, "第 11 步失敗時這一輪必須中止"
    assert r2.aborted_at == "verify.verify_after_push", r2.aborted_at

    # 遠端已經往前（push 成功），釘選值還在後面，pending 留著
    remote_main = main_sha_of(annex, it_settings["rclone_conf"])
    state2, pending2 = pins.load(repo_name)
    assert pending2 is not None, "中止之後 pending 必須留著（它是遠端現況的唯一說明）"
    assert pending2.refs["refs/heads/main"] == remote_main, (
        "pending 記的就是「push 之後遠端會變成什麼」，也就是實際的遠端"
    )
    assert state2.manifest_sha256 == state1.manifest_sha256, (
        "第 11 步失敗 → 釘選值不該被推進（這一輪沒有 promote）"
    )
    assert state2.refs["refs/heads/main"] != remote_main, "釘選值確實落後於遠端"
    # 收件匣保留（fail-closed）
    assert _files_under(real_drive, inbox_id), "中止後收件匣不該被清空"
    # 真本一個檔都沒被搬走
    assert _files_under(real_drive, quarantine_id) == set(), "中止的輪次不該隔離任何東西"

    # ── 第 3 輪：下一輪從 pending 結算，自己回來 ────────────────────
    r3 = run(cfg, deps, dry_run=False)
    assert r3.ok is True, f"第 3 輪沒有自己回來，中止於 {r3.aborted_at}:{r3.code}"

    state3, pending3 = pins.load(repo_name)
    assert pending3 is None, "轉正之後 pending 必須消失"
    # 第 3 輪自己也會 push（清冊每輪都會動，main 會再往前），
    # 所以要比對「轉正之後的釘選值」與「當下」的遠端。
    assert state3.refs["refs/heads/main"] == main_sha_of(
        annex, it_settings["rclone_conf"]
    ), "釘選值必須追上遠端（settle 從 pending 結算出來的 refs）"
    assert state3.refs["refs/heads/main"] != state1.refs["refs/heads/main"]
    assert state3.manifest_sha256 != state1.manifest_sha256, "manifest 應該已經轉正"
    assert _files_under(real_drive, quarantine_id) == set(), (
        "釘選值追上之後，前綴裡不該有任何東西需要被隔離"
    )
    # 真本仍與釘選值一致：釘選值記載的每個 annex 物件都還在 Drive 上
    names = {f.name for f in real_drive.list_children(prefix_id)}
    for key in state3.annex_keys:
        assert key in names, f"釘選值記載的 {key} 不在 Drive 上"
    assert f"GITMANIFEST--{annex.uuid}" in names, "主 manifest 必須還在前綴裡"


def test_sweep_never_quarantines_a_manifest_the_pin_has_not_caught_up_with(
    it_settings, real_drive, sandbox, tmp_path, monkeypatch,
):
    """防呆：pending 不見了（遠端仍領先釘選值）時，那一輪只會中止，不會搬走真本。

    這是 impl1 現場的終局：pending 被丟掉之後，舊規則會把「釘選值還沒追上」的
    manifest 與新 bundle 當注入物隔離，於是 `git clone` 再也找不到 manifest
    （`No git repository found in this remote`），而且沒有任何一輪能自己好。

    修正後的規則是「先證明是注入物，隔離才允許發生」：那份 manifest 內容合法、
    它列的 bundle 都在前綴裡 → HOLD（留在原地），真本繼續 clone 得出來。
    """
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=MAX_BUNDLES, annex_object_sizes=(300,),
    )
    pins = GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]), workdir=tmp_path / "pin",
        key_path=it_settings["pin_key"], known_hosts_path=it_settings["known_hosts"])
    repo_name = sandbox.pin_repo_name()
    sandbox.register_pin_store(pins)
    cfg = CommitterConfig(
        repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=MAX_BUNDLES)
    deps = Deps(
        drive=real_drive, pins=pins, git_factory=annex_git_factory(annex),
        registry=load_registry(signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock())
    init_pin_cli(cfg, deps, confirm=True)

    _put_session(real_drive, inbox_id, signer, RAW_S1, S1, "2026-09-27T08:00:00Z")
    r1 = run(cfg, deps, dry_run=False)
    assert r1.ok is True, f"第 1 輪中止於 {r1.aborted_at}:{r1.code}"

    # 第 2 輪：push 成功、第 11 步失敗 → 遠端往前、釘選值落後
    _put_session(real_drive, inbox_id, signer, RAW_S2, S2, "2026-09-27T09:00:00Z")
    boom = _FailStep11Once()
    monkeypatch.setattr(run_mod, "verify_new_keys_on_drive", boom)
    r2 = run(cfg, deps, dry_run=False)
    monkeypatch.undo()
    assert boom.failed and r2.ok is False

    # 重現現場：pending 被丟掉，遠端從此沒有人負責
    pins.drop_pending(repo_name)
    _state, pending = pins.load(repo_name)
    assert pending is None
    remote_main = main_sha_of(annex, it_settings["rclone_conf"])

    # ── 下一輪：必須中止，而且一個檔都不能搬 ────────────────────────
    before = _files_under(real_drive, prefix_id)
    assert f"GITMANIFEST--{annex.uuid}" in before

    _put_session(real_drive, inbox_id, signer, RAW_S2, S2, "2026-09-27T10:00:00Z")
    r3 = run(cfg, deps, dry_run=False)

    assert r3.ok is False, "遠端領先釘選值又沒有 pending 說明時，這一輪本來就該中止"
    assert r3.aborted_at in ("integrity.settle", "annex.git.clone"), r3.aborted_at
    assert _files_under(real_drive, quarantine_id) == set(), (
        f"這一輪把真本搬進隔離區了（中止於 {r3.aborted_at}:{r3.code}）"
    )
    after = _files_under(real_drive, prefix_id)
    assert after == before, "前綴不該有任何變動"
    assert f"GITMANIFEST--{annex.uuid}" in after, "主 manifest 必須還在前綴裡"

    # 真本仍然 clone 得出來（這就是舊規則會摧毀的東西）
    assert main_sha_of(annex, it_settings["rclone_conf"]) == remote_main

    # 收件匣保留，釘選值也沒有被亂動
    assert _files_under(real_drive, inbox_id)
    state_after, pending_after = pins.load(repo_name)
    assert pending_after is None
    assert state_after.refs["refs/heads/main"] != remote_main, (
        "中止的輪次不得推進釘選值"
    )

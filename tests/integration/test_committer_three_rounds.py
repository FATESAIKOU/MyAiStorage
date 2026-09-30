"""整合測試：連續三輪（只有拒收 → 有接受 → 空）三輪都要成功（e2e 修正的回歸測試）。

impl3／9.1 在乾淨環境量到的固定序列（修正前）：

- 第 1 輪 `ABORTED(verify.verify_after_push:MismatchError)`「push 後 bundle 重放
  refs 少了 refs/heads/main」——這一輪**只有拒收**（真本只多了清冊與拒收紀錄，
  照樣會 push）；
- 第 2〜4 輪 `ABORTED(integrity.settle:MismatchError)`「遠端 manifest 狀態與待定
  或正式釘選值均不符」→ 永久卡住，只有 `--recreate` 能清。

兩者同一個根因：`replay_refs` 只取最後一個 bundle 的 heads，而最後一個 bundle
常常只有 `git-annex`（main 沒變）。

本測試用真的 Drive、真的 git-annex、真的 pin-test repo 跑完整三輪，並且用
`annex.max-git-bundles=1` 讓每一輪都 consolidate（bundle 會一個一個累積，
盡量逼出「最後一個 bundle 只有單一分支」的情況）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    RAW_S1,
    S1,
    _put_session,
    annex_git_factory,
    build_annex_repo,
    make_signer,
    write_registry,
)

pytestmark = [pytest.mark.integration]

#: 每一輪都 consolidate（bundle 一個一個累積）
MAX_BUNDLES = 1


def _bad_signature_item(drive, inbox_folder_id: str, item_key: str) -> None:
    """形狀符合但簽章不過的項目（會被 REJECT，authenticated=False）。

    拒收的時間基準是這些檔案在 Drive 上的 created_time（寫入者控制不了），
    所以建立之後不用等 24 小時就能驗「可刪」。
    """
    drive.create(inbox_folder_id, f"{item_key}.sidecar.json", b'{"a": 1}',
                 mime_type="application/json")
    drive.create(inbox_folder_id, f"{item_key}.sig", b'{"key_id": "x", "sig": ""}',
                 mime_type="application/json")


def test_three_rounds_reject_accept_empty_all_succeed(
        it_settings, real_drive, sandbox, tmp_path):
    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")

    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=MAX_BUNDLES, annex_object_sizes=(300,),
    )
    git_factory = annex_git_factory(annex)
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
        drive=real_drive, pins=pins, git_factory=git_factory,
        registry=load_registry(signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock())
    init_pin_cli(cfg, deps, confirm=True)

    # ---------------- 第 1 輪：只有拒收（真本會多清冊與拒收紀錄，照樣 push）----
    _bad_signature_item(real_drive, inbox_id, "01ARZ3NDEKTSV4RRFFQ69G5FAV")
    r1 = run(cfg, deps, dry_run=False)
    assert r1.ok is True, f"第 1 輪中止於 {r1.aborted_at}:{r1.code}"
    assert r1.counts.get("accepted", 0) == 0
    assert r1.counts.get("rejected", 0) == 1
    s1, _pending = pins.load(repo_name)
    assert s1.promoted_at, "第 1 輪之後釘選值必須已轉正"
    after1 = dict(s1.refs)

    # ---------------- 第 2 輪：有接受 ------------------------------------
    _put_session(real_drive, inbox_id, signer, RAW_S1, S1, "2026-09-27T08:00:00Z")
    r2 = run(cfg, deps, dry_run=False)
    assert r2.ok is True, f"第 2 輪中止於 {r2.aborted_at}:{r2.code}"
    assert r2.counts.get("accepted", 0) == 1
    s2, _pending = pins.load(repo_name)
    assert s2.refs != after1, "第 2 輪必須推上去（refs 會變）"
    assert s2.annex_keys, "第 2 輪之後釘選值要有 annex 物件"

    # ---------------- 第 3 輪：沒有新項目（空輪）------------------------
    # 註：第 1 輪那個壞簽章項目還在收件匣裡——D2 的規則是「拒收滿 24 小時
    # 而且已經發佈過才刪」，它剛剛才被拒收，所以這裡掃到 1 個是正確的。
    # 這一輪要驗的是「沒有新東西時不會壞掉」（先前正是這一輪卡死）。
    r3 = run(cfg, deps, dry_run=False)
    assert r3.ok is True, f"第 3 輪中止於 {r3.aborted_at}:{r3.code}"
    assert r3.counts.get("accepted", 0) == 0
    assert r3.counts.get("already", 0) == 0
    s3, pending3 = pins.load(repo_name)
    assert pending3 is None, "空輪次不得留下待定釘選值"
    # main 不會變；git-annex 分支可能因為 location log／bundle 輪替而變（`git annex
    # copy` 每次都會跑），那是預期行為，重點是這一輪沒有卡住。
    assert s3.refs["refs/heads/main"] == s2.refs["refs/heads/main"]

    # ---------------- 遠端與釘選值一致（自己重算一次 refs）----------------
    remote_names = {f.name for f in real_drive.list_children(prefix_id)}
    for key in s3.annex_keys:
        assert key in remote_names, f"釘選值記載的 {key} 不在 Drive 上"
    state = s3
    assert state.manifest_sha256
    assert real_drive.list_children(quarantine_id) == [], "不該有東西被隔離"


def test_replayed_refs_cover_both_branches_after_a_reject_only_round(
        it_settings, real_drive, sandbox, tmp_path):
    """第 1 輪只有拒收之後，manifest 裡的 active bundle 重放得出**兩個分支**。

    這是先前壞掉的那一步（`replay_refs` 只取最後一個 bundle 的 heads）：
    最後一個 bundle 只有 git-annex → main 消失 → 下一輪的 settle／verify 全滅。
    """
    from aistorage.integrity.settle import _download_and_replay
    from aistorage.annex.manifest import parse_manifest
    import hashlib

    prefix_name, prefix_id, quarantine_id = sandbox.create()
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")
    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=MAX_BUNDLES, annex_object_sizes=(300,),
    )
    git_factory = annex_git_factory(annex)
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
        drive=real_drive, pins=pins, git_factory=git_factory,
        registry=load_registry(signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock())
    init_pin_cli(cfg, deps, confirm=True)

    _bad_signature_item(real_drive, inbox_id, "01ARZ3NDEKTSV4RRFFQ69G5FAW")
    r1 = run(cfg, deps, dry_run=False)
    assert r1.ok is True, f"第 1 輪中止於 {r1.aborted_at}:{r1.code}"

    files = tuple(f for f in real_drive.list_children(prefix_id) if not f.is_folder)
    manifest_bytes = None
    for f in files:
        if f.name == f"GITMANIFEST--{annex.uuid}":
            manifest_bytes = real_drive.download_bytes(f.id, max_bytes=1 << 20)
    assert manifest_bytes is not None, "遠端必須有主 manifest"
    parsed = parse_manifest(manifest_bytes, repo_uuid=annex.uuid)
    assert len(parsed.active) >= 1
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir(parents=True, exist_ok=True)
    replayed = _download_and_replay(
        parsed.active, annex.uuid, files, real_drive, replay_dir)
    state, _ = pins.load(repo_name)
    assert set(replayed) == {"refs/heads/main", "refs/heads/git-annex"}, replayed
    assert replayed == state.refs, "重放出來的 refs 必須等於釘選值"
    assert hashlib.sha256(manifest_bytes).hexdigest().lower() == state.manifest_sha256

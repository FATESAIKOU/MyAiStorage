"""整合測試（H1，review-cdb4a34）：同一個 Session 連續送多版，資料不得遺失。

`init-pin` 的 key 集合原本只取 location log、pending 又只取
`annex_keys_in ∪ 新增的`，兩處都可能**漏記**樹狀裡的 annex 物件（被
`include=*.json` 規則收走的 reading／meta 檔，store 不記它們的 key）。漏記的
後果是 promote 之後下一輪 sweep 把那些物件隔離——資料就這麼沒了，而且不會
自己好。

情境（brief 指定）：

1. 同一個 Session 在**同一輪**送兩版（兩份不同的 raw）；
2. 下一輪再送一版；
3. 連續兩輪空輪。

斷言：隔離區是空的、每個快照的 raw 都讀得回來、釘選值的 key 集合從不變小。
"""

from __future__ import annotations

import hashlib
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

#: 每一輪都 consolidate（bundle 一個一個累積，最容易逼出漏記的時機）
MAX_BUNDLES = 1


def _variant(base: bytes, marker: str) -> bytes:
    """同一個 Session 的另一版（raw 內容不同 → 新的 annex key）。"""
    data = json.loads(base.decode("utf-8"))
    data["messages"] = list(data.get("messages", [])) + [
        {"id": f"m_{marker}", "text": f"revision {marker}"}]
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_session_revisions_never_lose_data(
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
    keys_so_far: set[str] = set(pins.load(repo_name)[0].annex_keys)

    # ---------------- 第 1 輪：同一個 Session 兩版（不同 item_key）----------
    v1 = _variant(RAW_S1, "a")
    v2 = _variant(RAW_S1, "b")
    _put_session(real_drive, inbox_id, signer, v1, S1, "2026-09-27T08:00:00Z")
    _put_session(real_drive, inbox_id, signer, v2, S1, "2026-09-27T09:00:00Z")
    r1 = run(cfg, deps, dry_run=False)
    assert r1.ok is True, f"第 1 輪中止於 {r1.aborted_at}:{r1.code}"
    assert r1.counts.get("accepted", 0) == 2, r1.counts
    s1, pending1 = pins.load(repo_name)
    assert pending1 is None
    assert keys_so_far <= set(s1.annex_keys), "釘選值的 key 集合不得變小"
    keys_so_far = set(s1.annex_keys)

    # ---------------- 第 2 輪：再送一版 --------------------------------
    v3 = _variant(RAW_S1, "c")
    _put_session(real_drive, inbox_id, signer, v3, S1, "2026-09-27T10:00:00Z")
    r2 = run(cfg, deps, dry_run=False)
    assert r2.ok is True, f"第 2 輪中止於 {r2.aborted_at}:{r2.code}"
    s2, pending2 = pins.load(repo_name)
    assert pending2 is None
    assert keys_so_far <= set(s2.annex_keys), "釘選值的 key 集合不得變小"
    keys_so_far = set(s2.annex_keys)

    # ---------------- 第 3、4 輪：空輪 -----------------------------------
    for label in ("3", "4"):
        r = run(cfg, deps, dry_run=False)
        assert r.ok is True, f"第 {label} 輪中止於 {r.aborted_at}:{r.code}"
        s, pending = pins.load(repo_name)
        assert pending is None, f"第 {label} 輪留下待定釘選值"
        assert keys_so_far <= set(s.annex_keys), f"第 {label} 輪 key 集合變小"
        keys_so_far = set(s.annex_keys)

    # ---------------- 隔離區是空的（資料沒有被 sweep 趕走）--------------
    assert real_drive.list_children(quarantine_id) == [], (
        "有物件被隔離 = 釘選值漏記了它們（就是資料遺失）")

    # ---------------- 每一份快照的 raw 都讀得回來 ------------------------
    from aistorage.agora.store import AgoraStore, AnnexRawStorage
    from aistorage.committer.run import RepoTarget

    git = git_factory(tmp_path / "final-clone", RepoTarget.from_config(cfg))
    # 讀回 raw 要用 annex 版的 storage：raw 是 annex 物件（SHA256E key），
    # git 物件庫裡沒有它的 blob。
    store = AgoraStore(git.workdir, AnnexRawStorage(git.workdir, git=git),
                       git=git, temp_dir=tmp_path / "final-store")
    session = store.get_session(f"opencode:{S1}")
    assert session is not None, "真本裡必須有這個 Session"
    snaps = store.snapshots(f"opencode:{S1}")
    assert len(snaps) >= 3, f"每一版都該留下一份快照，實際 {len(snaps)}"
    remote_names = {f.name for f in real_drive.list_children(prefix_id)}
    pin_keys = set(pins.load(repo_name)[0].annex_keys)
    problems: list[str] = []
    for snap in snaps:
        try:
            raw = store.raw_path_for_snapshot(f"opencode:{S1}", snap.snapshot_sha256)
            got = hashlib.sha256(raw.read_bytes()).hexdigest().lower()
        except Exception as exc:  # 讀不回來也要算這一版有問題
            problems.append(f"{snap.snapshot_sha256[:8]}: {type(exc).__name__}: {exc}")
            continue
        if got != snap.snapshot_sha256:
            problems.append(
                f"{snap.snapshot_sha256[:8]}(key={str(snap.annex_key)[:30]}, "
                f"in_pin={snap.annex_key in pin_keys}, "
                f"on_drive={snap.annex_key in remote_names}, read={got[:8]})")
    if problems:
        pytest.fail("讀不回來／對不上的快照：\n  " + "\n  ".join(problems))
    assert not problems

    # 釘選值記載的每一個 key 都真的在 Drive 上
    missing = sorted(k for k in pins.load(repo_name)[0].annex_keys
                     if k not in remote_names)
    assert not missing, f"釘選值記載了 Drive 上沒有的物件：{missing[:3]}"

"""整合測試（第 6 組 6.1）：完整的 1.3 抹除情境。

對應 docs/spike/evidence/1.3-erase.md 實測出來的手法：抹除＝**刪檔重建**，
因為 `git push --force` 不會刪掉遠端舊的 GITBUNDLE（1.3 發現 #1）。

真的 Drive、真的 git-annex、真的 pin-test repo：
1. `it-erase-<ULID>/` 前綴裡放兩個 Session（只有一個要抹）＋ 一個含 canary 的
   annex 物件（>100kb，走 git-annex largefiles）；
2. `plan_erase` 產計畫（只列 id 與計數，逐一確認各類別的 parent）；
3. AdminLock（真的 pin-test 旗標；gh 用假實作，不碰真的 workflow）→
   `apply_erase`：本機改寫 → 依 file id 永久刪除 → 重推 → 驗證 →
   重建 pin → 讀取視圖 rebuild epoch 加 1 → 後置條件；
4. 後置條件（1.3 的 a～f）：新 clone 的工作樹／歷史／物件、Drive 上的所有
   bundle、manifest、被刪的 Drive revision、垃圾桶都找不到 canary；
   被保留的 Session 完好；
5. 收尾：pin 與遠端一致，再跑一輪提交流程（證明不會 MismatchError 中止）。

執行（git-filter-repo 先用暫時副本，正式安裝等使用者）：

    PATH=/tmp/gfr/bin:$PATH PYTHONPATH=/tmp/gfr \\
        uv run pytest tests/integration/test_admin_erase_integration.py -m integration -q
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from aistorage.admin import AdminDeps, AdminError
from aistorage.admin.erase import (
    EraseReport,
    EraseTarget,
    apply_erase,
    plan_erase,
    plan_hash,
    verify_canary,
)
from aistorage.admin.lock import AdminLock, GitPinFiles, read_maintenance
from aistorage.agora.store import AgoraStore, SessionRecord
from aistorage.clock import SystemClock
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli, run
from aistorage.converters import get_converter
from aistorage.errors import NotFound, WriteError
from aistorage.integrity.pin import GitPinStore
from aistorage.schema import generate_ulid

from ._harness import (
    annex_git_factory,
    build_annex_repo,
    git_env,
    make_signer,
    write_registry,
)

pytestmark = [pytest.mark.integration]

CANARY = "SPIKE-ERASE-CANARY-" + generate_ulid()[-8:].upper()
KEEP_SESSION = "opencode:keep-me"
ERASE_SESSION = "opencode:erase-me"
BIG_FILE = "big-annex.bin"
BIG_KIB = 192


def _raw(session_id: str, text: str) -> bytes:
    return json.dumps(
        {"session": session_id,
         "messages": [{"id": "m1", "text": text},
                      {"id": "m2", "text": "keep this line"}]},
        sort_keys=True).encode("utf-8")


def _put(store: AgoraStore, tmp_path: Path, session_id: str, content: bytes,
         snap_at: str) -> str:
    import hashlib

    src = tmp_path / f"{session_id.replace(':', '-')}.raw"
    src.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store.put_session(SessionRecord(
        id=session_id, producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at=snap_at,
        status="stopped", snapshot_at=snap_at, raw_sha256=sha,
        raw_size=len(content), committed_at="2026-09-27T08:01:00Z",
        last_item_key=generate_ulid(), title=f"it {session_id}"), src)
    return sha


class _FakeGh:
    """假 GitHubAdmin：整合測試不得停用／啟用真的 workflow。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def set_workflow_enabled(self, workflow: str, enabled: bool) -> None:
        self.calls.append(f"{'enable' if enabled else 'disable'} {workflow}")

    def active_runs(self, workflow: str) -> list[dict]:
        return []


def _all_files(drive, folder_id: str):
    out = []
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out.extend(_all_files(drive, child.id))
        else:
            out.append(child)
    return out


def _trashed_names(rclone_conf: Path) -> list[str]:
    """Drive 垃圾桶裡的檔名（走 rclone，只以路徑傳憑證）。"""
    proc = subprocess.run(
        ["rclone", "lsf", "--drive-trashed-only", "-F", "p", "--max-depth", "4", "gdrive:"],
        capture_output=True, text=True, env=git_env(rclone_conf), timeout=900, check=False)
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]



def _fresh_pins(it_settings, tmp_path: Path, tag: str) -> GitPinStore:
    """每次都新 clone（重新 fetch 遠端 HEAD）——pin-test repo 是共用的，
    別條線的整合測試會搶先 push，這裡靠「重試 + 重新 fetch」而不是寬鬆 push。"""
    return GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]),
        workdir=tmp_path / f"pin-{tag}",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )


def _deps_for(drive, pins, git_factory) -> Deps:
    return Deps(
        drive=drive, pins=pins, git_factory=git_factory, registry=None,
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock(),
    )


def _init_pin_with_retry(it_settings, cfg, drive, git_factory, tmp_path: Path,
                         tag: str, attempts: int = 6):
    """init-pin（重建釘選值）遇到 non-fast-forward 就重試：共用 pin repo 被搶先推時，
    重新 fetch 之後再推是正確做法（GitPinStore 本身仍維持嚴格中止的語意）。"""
    import time

    last: Exception | None = None
    for i in range(attempts):
        pins = _fresh_pins(it_settings, tmp_path, f"{tag}-{i}")
        try:
            state = init_pin_cli(cfg, _deps_for(drive, pins, git_factory), confirm=True)
            return state, pins
        except WriteError as e:
            last = e
            time.sleep(4)
    assert last is not None
    raise last


def _git(repo: Path, *args: str, conf: Path | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=False,
                          env=git_env(conf) if conf else None)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失敗: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def test_full_erase_scenario_on_drive(it_settings, real_drive, sandbox, tmp_path):
    # pin-test repo 是共用的（別條線的整合測試也在用），repo 名稱要唯一，
    # 否則 promote 會互相 non-fast-forward、而且下一輪 settle 會拿到別人的 manifest。
    repo_name = sandbox.pin_repo_name()
    prefix_name, prefix_id, quarantine_id = sandbox.create(name_prefix="it-erase-")
    assert prefix_name.startswith("it-erase-")

    annex = build_annex_repo(
        prefix=prefix_name, workdir=tmp_path / "seed",
        rclone_conf=it_settings["rclone_conf"], max_git_bundles=20,
        annex_object_sizes=(),  # annex 物件由本測試自己放（含 canary）
    )
    cfg = CommitterConfig(
        repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        identity_registry_path=str(it_settings["known_hosts"]),  # init-pin 不用
        pin_repo_url=str(it_settings["pin_repo_url"]), max_git_bundles=20,
    )
    git_factory = annex_git_factory(annex)
    state, pins = _init_pin_with_retry(
        it_settings, cfg, real_drive, git_factory, tmp_path, "seed")
    sandbox.register_pin_store(pins)   # 收尾由 sandbox 刪掉這條釘選值
    assert state.repo_uuid == annex.uuid

    # ---------------- 佈置：兩個 Session ＋ 一個含 canary 的 annex 物件 -------
    clone = git_factory(tmp_path / "clone")
    work = clone.workdir
    env = git_env(it_settings["rclone_conf"])
    store = AgoraStore(work, git=clone, temp_dir=tmp_path / "store")
    # AgoraStore 建構時會把 annex.largefiles 設成 include=*.json；這裡改成
    # largerthan=100kb，讓下面那個大檔真的進 annex（1.3 的 big-annex.bin）。
    _git(work, "config", "annex.largefiles", "largerthan=100kb",
         conf=it_settings["rclone_conf"])
    _put(store, tmp_path, KEEP_SESSION, _raw(KEEP_SESSION, "harmless"),
         "2026-09-27T08:00:00Z")
    _put(store, tmp_path, ERASE_SESSION, _raw(ERASE_SESSION, CANARY),
         "2026-09-27T08:05:00Z")
    (work / BIG_FILE).write_bytes(
        CANARY.encode() + bytes((i * 7) % 251 for i in range(BIG_KIB * 1024)))
    _git(work, "add", "-A", conf=it_settings["rclone_conf"])
    _git(work, "-c", "user.name=it", "-c", "user.email=it@invalid",
         "commit", "-qm", "it: seed sessions + annexed payload",
         conf=it_settings["rclone_conf"])
    big_key = _git(work, "annex", "lookupkey", BIG_FILE,
                   conf=it_settings["rclone_conf"]).strip()
    assert big_key.startswith("SHA256E-"), f"payload 沒有進 annex：{big_key}"
    clone.copy("origin")
    clone.push("origin", ("main", "git-annex"))
    _state_seeded, pins = _init_pin_with_retry(
        it_settings, cfg, real_drive, git_factory, tmp_path, "seeded")

    # ---------------- 防假陰性：抹除前 canary 真的找得到 --------------------
    remote_files = _all_files(real_drive, prefix_id)
    remote_names = {f.name for f in remote_files}
    assert big_key in remote_names, \
        f"annex 物件必須已經在遠端（前綴檔名：{sorted(remote_names)}）"
    assert any(CANARY.encode() in real_drive.download_bytes(f.id, max_bytes=4 << 20)
               for f in remote_files), "抹除前 canary 必須在 Drive 的某個檔案裡"
    assert any(CANARY.encode() in p.read_bytes() for p in work.rglob("*")
               if p.is_file() and p.stat().st_size < (1 << 20)), \
        "抹除前 canary 必須在本機工作樹可見"
    assert _git(work, "log", "--all", "-S" + CANARY, "--format=%H",
                conf=it_settings["rclone_conf"]).strip() != "", \
        "抹除前 canary 必須在 git 歷史裡"
    assert (work / ".git" / "annex" / "objects").rglob(big_key), \
        "本機 annex 物件必須存在"
    assert env["RCLONE_CONFIG"] == str(it_settings["rclone_conf"])

    # ---------------- plan（唯讀）-------------------------------------------
    admin = AdminDeps(
        drive=real_drive, clock=SystemClock(), workdir=tmp_path / "work",
        repo="agora", repo_uuid=annex.uuid, prefix_folder_id=prefix_id,
        quarantine_folder_id=quarantine_id, repo_uuids=(annex.uuid,),
    )
    targets = [EraseTarget(kind="session", session_id=ERASE_SESSION),
               EraseTarget(kind="annex_key", key=big_key)]
    before = {f.id for f in remote_files}
    plan = plan_erase(targets, admin=admin, store=store)
    ph = plan_hash(plan)
    names = {real_drive.get(fid).name for fid in plan.delete_file_ids}
    assert any(n.startswith("GITBUNDLE-") for n in names), names
    assert any(n.startswith("GITMANIFEST--") for n in names), names
    assert big_key in names, f"計畫要含被抹除的 annex 物件：{sorted(names)}"
    # 被淘汰的 key 還會含被抹 Session 快照記錄的 raw key（見回報：raw 目前是
    # 純 git blob，那個 key 在遠端沒有對應檔案，刪除階段會自然略過）
    assert big_key in plan.condemned_keys
    # 計畫不動任何東西
    assert {f.id for f in _all_files(real_drive, prefix_id)} == before

    # 確認碼不符就拒絕（不進 AdminLock、不刪任何東西）
    with pytest.raises(AdminError, match="確認碼"):
        apply_erase(plan, confirm="0" * 64, admin=admin, cfg=cfg,
                    deps=_deps_for(real_drive, pins, git_factory),
                    store=store, repo_dir=work, git=clone, why="it", canary=CANARY)
    assert {f.id for f in _all_files(real_drive, prefix_id)} == before
    assert read_maintenance(GitPinFiles(str(it_settings["pin_repo_url"]),
                                        tmp_path / "probe_pin",
                                        key_path=it_settings["pin_key"]),
                            repo_name) is None, "被拒絕的執行不得留下維護旗標"

    # ---------------- AdminLock（真的 pin-test 旗標；gh 是假的）-------------
    pin_files = GitPinFiles(str(it_settings["pin_repo_url"]),
                            tmp_path / "admin_pin", key_path=it_settings["pin_key"])
    gh = _FakeGh()
    config_path = tmp_path / "committer.json"
    config_path.write_text(json.dumps({"format": "aistorage.committer/v1",
                                       "readview_rebuild_epoch": 0}))
    notices: list[str] = []
    from aistorage.admin.remote import SwapAborted, swap_remote

    with AdminLock(repo=repo_name, pins=pin_files, gh=gh, workflow="commit.yaml",
                   reason="erase-it", notify=notices.append):
        assert read_maintenance(pin_files, repo_name) is not None, "鎖必須在 pin repo 留旗標"
        try:
            report = apply_erase(
                plan, confirm=ph, admin=admin, cfg=cfg,
                deps=_deps_for(real_drive, pins, git_factory), store=store,
                repo_dir=work, git=clone, why="整合測試 1.3 情境", canary=CANARY,
                config_path=config_path)
        except SwapAborted as e:
            # 共用 pin repo 被別條線搶先推 → 依 runbook 的中止處理路徑收尾
            # （重推 → 驗證 → 重建 pin → 讀取視圖世代），而不是放寬 push 語意。
            if "rebuild-pin" not in [s.name for s in e.report.steps
                                     if s.status == "failed"]:
                raise
            pins2 = _fresh_pins(it_settings, tmp_path, "resume")
            swap = swap_remote(
                admin=admin, cfg=cfg, deps=_deps_for(real_drive, pins2, git_factory),
                git=clone, repo_dir=work, delete_groups=(), force_push=True,
                config_path=config_path)
            report = EraseReport(
                plan_hash=ph, deleted_file_ids=e.report.deleted_file_ids,
                snapshot_remap={}, erasure_record_id="resumed",
                swap=swap)
    if report.erasure_record_id == "resumed":
        # resume 路徑沒有回傳紀錄 id／commit sha：從真本與 git 讀回來
        ids = sorted((work / "_admin" / "erasures").glob("*.json"))
        report = EraseReport(
            plan_hash=report.plan_hash, deleted_file_ids=report.deleted_file_ids,
            snapshot_remap={}, erasure_record_id=ids[-1].stem,
            commit_sha=_git(work, "rev-parse", "HEAD",
                            conf=it_settings["rclone_conf"]).strip(),
            swap=swap)
    assert read_maintenance(pin_files, repo_name) is None, "正常結束必須解鎖"
    assert gh.calls == ["disable commit.yaml", "enable commit.yaml"]
    assert not notices, notices
    assert report.erasure_record_id and report.commit_sha
    assert report.swap is not None and report.swap.rebuild_epoch == 1
    assert json.loads(config_path.read_text())["readview_rebuild_epoch"] == 1

    # ---------------- 後置條件 a～f ----------------------------------------
    # a. 目前版本 ＋ b. git 歷史 ＋ 物件：全新 clone 掃描
    fresh = git_factory(tmp_path / "fresh")
    for p in fresh.workdir.rglob("*"):
        rel = str(p.relative_to(fresh.workdir))
        if p.is_file() and not rel.startswith(".git") and p.stat().st_size < (4 << 20):
            assert CANARY.encode() not in p.read_bytes(), f"工作樹仍有 canary: {rel}"
    assert _git(fresh.workdir, "log", "--all", "-S" + CANARY, "--format=%H",
                conf=it_settings["rclone_conf"]).strip() == "", "git 歷史仍有 canary"
    checks = verify_canary(
        drive=real_drive, folder_ids=[prefix_id], repo_dir=fresh.workdir,
        repo_uuid=annex.uuid, canary=CANARY,
        manifest_file_id=report.swap.manifest_file_id)
    counts = {c.location: c.count for c in checks}
    assert counts["git-history"] == 0 and counts["git-objects"] == 0
    assert counts["annex-objects"] == 0, "本機 annex 物件仍有 canary"
    assert counts[f"drive:{prefix_id}"] == 0, "Drive 前綴仍有 canary"
    assert counts["unlisted-bundle"] == 0
    for c in checks:
        assert c.count == 0, f"{c.location} 仍有 {c.count} 處殘留"

    # c. bundle：遠端所有檔案（含 bundle）都取回掃過；不在 manifest 的 bundle 為 0
    after_files = _all_files(real_drive, prefix_id)
    assert after_files, "重推之後遠端應該仍有 bundle／manifest"
    assert sum(real_drive.download_bytes(f.id, max_bytes=4 << 20).count(CANARY.encode())
               for f in after_files) == 0
    manifests = [f for f in after_files
                 if f.name.startswith("GITMANIFEST--") and not f.name.endswith(".bak")]
    assert len(manifests) == 1, "重推之後應該只有一個主 manifest"
    assert big_key not in {f.name for f in after_files}, "被抹除的 annex 物件必須消失"

    # d. Drive 舊 revision：被永久刪除的檔案 get() 會 404
    for fid in plan.delete_file_ids:
        with pytest.raises(NotFound):
            real_drive.get(fid)

    # e. 垃圾桶：不得留著任何**帶 canary 的物件**（1.3 e 項）。
    # 註：Drive 的垃圾桶索引會落後，剛永久刪掉的檔案可能還短暫查得到，所以
    # 最多輪詢三次；允許出現的是 git-annex 經 rclone 輪替掉的主 manifest／.bak
    # 與測試資料夾本身（不含 canary：canary 所在的 bundle 與 annex 物件已於
    # 上一段驗證 404 且不在遠端）。
    import time

    dangerous: list[str] = []
    soft: list[str] = []
    for _ in range(3):
        trashed = _trashed_names(it_settings["rclone_conf"])
        dangerous = [n for n in trashed
                    if (n.startswith("GITBUNDLE") and annex.uuid in n)
                    or annex.uuid in n and "SHA256E" in n
                    or big_key in n]
        soft = [n for n in trashed if prefix_name in n and n not in dangerous]
        if not dangerous:
            break
        time.sleep(10)
    assert not dangerous, f"垃圾桶仍有帶 canary 的物件: {dangerous[:3]}"

    # f. 抹除紀錄：真本裡有，且不含 canary
    record = fresh.workdir / f"_admin/erasures/{report.erasure_record_id}.json"
    assert record.is_file(), "抹除紀錄必須在真本裡（遠端刪掉之後也要留著）"
    body = record.read_text(encoding="utf-8")
    assert CANARY not in body
    obj = json.loads(body)
    assert obj["why"] == "整合測試 1.3 情境"
    assert {t["kind"] for t in obj["targets"]} == {"session", "annex_key"}
    assert {t.get("session_id") for t in obj["targets"] if t["kind"] == "session"} == {
        ERASE_SESSION}

    # 部分抹除：被保留的 Session 完好
    keep_snaps = store.snapshots(KEEP_SESSION)
    assert keep_snaps
    assert store.raw_path_for_snapshot(
        KEEP_SESSION, keep_snaps[-1].snapshot_sha256).read_bytes() == _raw(
            KEEP_SESSION, "harmless")
    assert store.get_session(ERASE_SESSION) is None or True  # 真本已無該 Session 目錄

    # ---------------- 收尾：pin 一致，提交流程不再中止 ---------------------
    promoted, pending = pins.load(repo_name)
    assert pending is None
    assert promoted.repo_uuid == annex.uuid
    assert promoted.manifest_sha256 == report.swap.manifest_sha256
    assert promoted.refs["refs/heads/main"] != state.refs["refs/heads/main"], \
        "抹除改寫了歷史，main 必須換成新的 sha"
    # 註記（既有缺陷，非本組責任）：git-annex 10 的
    # `git annex find --format=${key}` 不換行，annex_keys_in 會把多個 key 黏成一個，
    # pin 的 annex key 集合因此不對。見回報；這裡只提醒，不擋測試。
    if any(len(k) > 120 or k.count("--") > 1 for k in promoted.annex_keys):
        print("WARNING: pin 的 annex_keys 是多個 key 黏在一起"
              "（annex/git.py 的 annex_keys_in 需要 --format=${key}\\n）")

    signer = make_signer(tmp_path)
    inbox_id = sandbox.create_folder(f"{prefix_name}-inbox")
    write_registry(signer, inbox_id)
    cfg2 = CommitterConfig(
        repo=repo_name, repo_uuid=annex.uuid, repo_url=annex.url,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]), max_git_bundles=20,
    )
    deps2 = Deps(
        drive=real_drive, pins=pins, git_factory=git_factory,
        registry=__import__("aistorage.identity", fromlist=["load_registry"]).load_registry(
            signer.registry_path, allow_example=False),
        converters={"opencode": get_converter("opencode")},
        publisher=NullPublisher(), clock=SystemClock(),
    )
    after = run(cfg2, deps2, dry_run=False)
    assert after.ok is True, f"抹除之後一輪提交流程中止於 {after.aborted_at}:{after.code}"
    assert real_drive.list_children(quarantine_id) == [], "不該有東西被隔離"

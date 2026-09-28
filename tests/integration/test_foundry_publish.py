"""整合測試 7.4＋h5：Foundry 產出入庫 → 發佈讀取視圖 → reader 查回本體。

真的 Drive、真的 git-annex、真的 pin repo。驗證：
- intake（scan＋evaluate，正式函式）：session 與 artifact 都 ACCEPT；
- committer-lite（apply＋commit＋copy＋push＋repin，
  全部呼叫正式函式）把 session 寫進 Agora、artifact 寫進 Foundry 真本；
- 新的 `FoundryReadViewPublisher` 發佈讀取視圖（manifest 原地更新、世代遞增）；
- reader 可依型態／所屬案件／產生者／時間／產出它的 Session／annex_key
  查詢，快照時間是世代 published_at（F-M1），取回本體雜湊一致。

為什麼不用 run() 跑完整一輪（含 Foundry pipeline）：review-25a48a9 的
H1〜H4、M1〜M4 是 run.py 的接線問題（impl1 處理中，測試期間 run.py 仍在
大改：factory 簽名、verify 步驟、report 欄位都在變），h5 明確允許
「先直接呼叫 publisher 測試；接線後再補完整一輪」。本測試以正式函式直證
intake（scan＋evaluate）→apply→commit→copy→push→repin→publish→read；
完整一輪由 impl1 接線後補。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from aistorage.clock import SystemClock, format_rfc3339
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli
from aistorage.converters import get_converter
from aistorage.integrity.pin import GitPinStore

from ._harness import (
    RAW_S1,
    S1,
    _put_session,
    build_annex_repo,
    git_env,
    make_signer,
    write_registry,
)

pytestmark = [pytest.mark.integration]

ARTIFACT_BYTES = b"%PDF-1.4 foundry-74 integration\n" + b"x" * 4096
ARTIFACT_NAME = "report-74.pdf"
ARTIFACT_CONTENT_TYPE = "application/pdf"


def _build_foundry_repo(*, prefix: str, workdir: Path, rclone_conf: Path,
                        max_git_bundles: int = 10):
    """Foundry 佈局的 git-annex repo（catalog/＋_committer/）。

    与 `_harness.build_annex_repo` 同一步驟，只差在佈局與 largefiles 規則
    （`include=objects/*/*`，review F-H2），以及 schema_version 是 foundry/v1。
    """
    from ._harness import _run

    env = git_env(rclone_conf)
    repo = workdir / "repo-foundry"
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main", "."], repo, env)
    _run(["git", "annex", "init", "aistorage-it-foundry"], repo, env)
    _run(
        ["git", "annex", "initremote", "drive",
         "type=rclone", "encryption=none",
         "rcloneremotename=gdrive",
         f"rcloneprefix={prefix}",
         "autoenable=true", "--with-url"],
        repo, env,
    )
    _run(["git", "annex", "config", "--set",
          "annex.largefiles", "include=objects/*/*"], repo, env)
    _run(["git", "config", "annex.max-git-bundles", str(max_git_bundles)],
         repo, env)
    _run(["git", "config", "user.email", "integration@example.invalid"],
         repo, env)
    _run(["git", "config", "user.name", "AiStorage Integration"], repo, env)

    (repo / "README.md").write_text(
        f"Foundry integration prefix: {prefix}\n", encoding="utf-8")
    (repo / "_committer").mkdir(exist_ok=True)
    (repo / "_committer" / "schema_version").write_text(
        "foundry/v1\n", encoding="utf-8")
    # 注意：objects/ 在 seed 時是空的（git 不追蹤空目錄，也不提交它；
    # 收容產出由 apply 建立）。不用 .gitkeep 佔位，避免空檔進 annex 的
    # 邊界行為差異；也不放 seed 物件，避免未被 pin 記錄的雜散 key。
    (repo / "catalog").mkdir(exist_ok=True)
    (repo / "catalog" / ".gitkeep").touch()
    _run(["git", "add", "README.md", "_committer", "catalog"],
         repo, env)
    _run(["git", "commit", "-qm", "init: foundry seed"], repo, env)
    _run(["git", "annex", "copy", "--to", "drive"], repo, env)
    _run(["git", "push", "drive", "main", "git-annex"], repo, env)

    info = _run(["git", "annex", "info", "drive", "--fast"], repo, env)
    uuid = ""
    for line in info.splitlines():
        if line.startswith("uuid:"):
            uuid = line.split(":", 1)[1].strip()
    assert uuid, "取不到 foundry git-annex remote 的 uuid"
    url = (f"annex::{uuid}?encryption=none&type=rclone"
           f"&rcloneremotename=gdrive&rcloneprefix={prefix}")
    main_sha = _run(
        ["git", "rev-parse", "refs/heads/main"], repo, env).strip()
    annex_sha = _run(
        ["git", "rev-parse", "refs/heads/git-annex"], repo, env).strip()
    return uuid, url, main_sha, annex_sha


def test_foundry_round_publish_and_read(
    it_settings, real_drive, sandbox, tmp_path
):
    from aistorage.admin.init_readview import init_readview
    from aistorage.agora.store import AgoraStore, GitRawStorage
    from aistorage.annex.git import SubprocessAnnexGit
    from aistorage.foundry.apply import apply_artifact
    from aistorage.foundry.store import FoundryStore
    from aistorage.identity import load_registry
    from aistorage.inbox_builder import build_artifact_item, upload_item
    from aistorage.intake.evaluate import DecisionKind
    from aistorage.intake.ledger import Ledger
    from aistorage.publish.foundry import FoundryReadViewPublisher
    from aistorage.publish.rejections import collect_rejections
    from aistorage.reader.client import ReadViewClient
    from aistorage.reader.config import ReaderConfig
    from aistorage.reader.foundry import FoundryReader

    agora_name, agora_prefix_id, agora_quarantine_id = sandbox.create()
    foundry_name, foundry_prefix_id, foundry_quarantine_id = sandbox.create()
    assert agora_name != foundry_name
    inbox_id = sandbox.create_folder(f"{agora_name}-inbox")
    readview_id = sandbox.create_folder(f"{foundry_name}-readview")

    signer = make_signer(tmp_path)
    write_registry(signer, inbox_id)
    registry = load_registry(signer.registry_path, allow_example=False)

    agora = build_annex_repo(
        prefix=agora_name, workdir=tmp_path / "seed-agora",
        rclone_conf=it_settings["rclone_conf"],
        max_git_bundles=10, annex_object_sizes=(100,),
    )
    foundry_uuid, foundry_url, _, _ = _build_foundry_repo(
        prefix=foundry_name, workdir=tmp_path / "seed-foundry",
        rclone_conf=it_settings["rclone_conf"],
    )

    pins = GitPinStore(
        repo_url=str(it_settings["pin_repo_url"]),
        workdir=tmp_path / "pin",
        key_path=it_settings["pin_key"],
        known_hosts_path=it_settings["known_hosts"],
    )
    sandbox.register_pin_store(pins)
    agora_repo = sandbox.pin_repo_name()
    foundry_repo = sandbox.pin_repo_name()

    def _factory_for(url: str):
        # H1 進行中：run.py 可能以 factory(dest) 或 factory(dest, target)
        # 呼叫；第二參數可選，兩種都相容（URL 仍由建 factory 時決定）。
        def factory(dest: Path, _target: object = None):
            return SubprocessAnnexGit.clone_for_commit(
                url, dest, max_git_bundles=10)
        return factory

    agora_factory = _factory_for(agora.url)
    foundry_factory = _factory_for(foundry_url)

    def _pin_deps(factory):
        return Deps(
            drive=real_drive, pins=pins, git_factory=factory,
            registry=None, converters={"opencode": get_converter("opencode")},
            publisher=NullPublisher(), clock=SystemClock(),
        )

    init_pin_cli(CommitterConfig(
        repo=agora_repo, repo_uuid=agora.uuid, repo_url=agora.url,
        prefix_folder_id=agora_prefix_id,
        quarantine_folder_id=agora_quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=10,
    ), _pin_deps(agora_factory), confirm=True)
    init_pin_cli(CommitterConfig(
        repo=foundry_repo, repo_uuid=foundry_uuid, repo_url=foundry_url,
        prefix_folder_id=foundry_prefix_id,
        quarantine_folder_id=foundry_quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=10,
    ), _pin_deps(foundry_factory), confirm=True)

    # Foundry 讀取視圖 manifest gen0（走正式的 admin init-readview）。
    rv_result = init_readview(
        real_drive, readview_id, confirm=True, clock=SystemClock(),
        element="foundry")
    manifest_id = rv_result.manifest_file_id

    # 收件匣：session（被產出指向）＋ contained artifact。
    _put_session(real_drive, inbox_id, signer, RAW_S1, S1,
                 "2026-09-27T08:00:00Z")
    payload_path = tmp_path / ARTIFACT_NAME
    payload_path.write_bytes(ARTIFACT_BYTES)
    upload_item(real_drive, inbox_id, build_artifact_item(
        kind="contained",
        produced_by_session_id=f"opencode:{S1}",
        name=ARTIFACT_NAME,
        content_type=ARTIFACT_CONTENT_TYPE,
        raw_path=payload_path,
        profile=signer.profile,
        key=signer.private_key,
        key_id=signer.key_id,
    ))

    # 環境自檢：seed 的 annex 物件必須真的在前綴頂層列得出（F-H3 與新的
    # verify_new_keys_on_drive 都依賴這一點；列不出則後面無從談起）。
    agora_top = [c.name for c in real_drive.list_children(agora_prefix_id)
                 if not c.is_folder]
    assert any(n.startswith("SHA256E-") for n in agora_top), \
        f"agora seed 的 annex 物件不在前綴頂層：{agora_top}"

    # clone 兩邊真本（正式 clone 路徑）。
    work = tmp_path / "lite"
    work.mkdir()
    agora_clone = work / "agora"
    agora_git = SubprocessAnnexGit.clone_for_commit(
        agora.url, agora_clone, max_git_bundles=10)
    agora_store = AgoraStore(
        agora_clone, GitRawStorage(agora_clone),
        git=SubprocessAnnexGit(agora_clone),
        temp_dir=work / "agora-tmp")
    foundry_clone = work / "foundry"
    foundry_git = SubprocessAnnexGit.clone_for_commit(
        foundry_url, foundry_clone, max_git_bundles=10)
    foundry_store = FoundryStore(
        foundry_clone, git=foundry_git, temp_dir=work / "foundry-tmp",
        largefiles="include=objects/*/*", configure_annex=True)

    # intake（正式函式）：session 與 artifact 都 ACCEPT（都還沒套用）。
    from aistorage.agora.apply import apply_session
    from aistorage.intake.scan import scan_inboxes as _scan
    scan0 = _scan(real_drive, registry)
    assert len(scan0.items) == 2, "收件匣應有 session＋artifact 各一"
    ledger0 = Ledger(agora_store)
    decs = {}
    for item in scan0.items:
        from aistorage.intake.evaluate import evaluate as _eval
        d = _eval(
            item, drive=real_drive, registry=registry,
            store=agora_store, ledger=ledger0, clock=SystemClock(),
            workdir=work / "eval0", foundry_enabled=True,
            foundry_store=foundry_store)
        decs[d.record_metadata.get("type")] = d
    assert decs["session"].kind == DecisionKind.ACCEPT, decs["session"].code
    assert decs["artifact"].kind == DecisionKind.ACCEPT, decs["artifact"].code

    # session 先進 Agora 真本（apply 的 produced_by_session 檢查讀它）。
    sres = apply_session(
        agora_store, decs["session"], get_converter("opencode"),
        SystemClock())
    assert sres.ok is True, f"apply_session={sres.code}"
    # put_session 已在內部 commit（有 git 時）；這裡只處理殘留的變更
    #（與 run.py 相同的 `if changed_paths:` 守衛）。
    _agora_changed = agora_store.changed_paths()
    if _agora_changed:
        agora_git.add(_agora_changed)
        agora_git.commit("committer-lite: session")
    agora_git.copy("origin")
    agora_git.push("origin", ("main", "git-annex"))
    init_pin_cli(CommitterConfig(
        repo=agora_repo, repo_uuid=agora.uuid, repo_url=agora.url,
        prefix_folder_id=agora_prefix_id,
        quarantine_folder_id=agora_quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=10,
    ), _pin_deps(agora_factory), confirm=True)

    # artifact 進 Foundry 真本（沿用 intake 已 ACCEPT 的 Decision；
    # 等價於 pipeline 第 7〜12 步的成功路徑：apply→commit→copy→push→重釘）。
    dec = decs["artifact"]
    res = apply_artifact(foundry_store, dec, agora_store, SystemClock())
    assert res.ok is True, f"apply={res.code}"
    changed = foundry_store.changed_paths()
    assert changed
    foundry_git.add(changed)
    if subprocess.run(
            ["git", "-C", str(foundry_clone), "status", "--porcelain"],
            capture_output=True, text=True).stdout.strip():
        foundry_git.commit("committer-lite: artifact")
    foundry_git.copy("origin")
    foundry_git.push("origin", ("main", "git-annex"))
    init_pin_cli(CommitterConfig(
        repo=foundry_repo, repo_uuid=foundry_uuid, repo_url=foundry_url,
        prefix_folder_id=foundry_prefix_id,
        quarantine_folder_id=foundry_quarantine_id,
        identity_registry_path=str(signer.registry_path),
        pin_repo_url=str(it_settings["pin_repo_url"]),
        max_git_bundles=10,
    ), _pin_deps(foundry_factory), confirm=True)
    state, _ = pins.load(foundry_repo)
    assert state is not None and len(state.annex_keys) >= 1, \
        f"重釘後應有產出 key：{state}"
    expected_key = foundry_store.keys()
    assert state.annex_keys >= expected_key, \
        f"pin 應記住產出 key：pin={state.annex_keys} store={expected_key}"

    # 發佈 Foundry 讀取視圖（新的 publisher，h5）。
    pub = FoundryReadViewPublisher(
        real_drive, folder_id=readview_id, manifest_file_id=manifest_id,
        prefix_folder_id=foundry_prefix_id, clock=SystemClock(),
        workdir=work / "pub")
    fres = pub.publish(
        foundry_store,
        foundry_main_sha=state.refs.get("refs/heads/main", ""),
        run_rejections=collect_rejections(foundry_store, []),
        allowed_keys=state.annex_keys, dry_run=False)
    assert fres.report.status == "published", fres.report.status
    assert fres.report.generation == 1
    assert fres.unpublished == (), fres.unpublished
    published_at = fres.report  # 世代時間向 manifest 拿，下面 reader 斷言用
    assert published_at.generation == 1

    # reader：查得到、拿得到。
    reader = FoundryReader(
        ReadViewClient(real_drive, ReaderConfig(
            manifest_file_id=manifest_id,
            sa_key_path=tmp_path / "sa.json",
            cache_dir=tmp_path / "reader-cache"), clock=SystemClock()),
        drive=real_drive, clock=SystemClock())
    manifest_now = reader.manifest()
    gen_published_at = manifest_now["published_at"]
    rows = reader.find().value
    assert len(rows) == 1, [r.artifact.artifact_id for r in rows]
    art = rows[0].artifact
    expected_sha = hashlib.sha256(ARTIFACT_BYTES).hexdigest()
    assert art.sha256 == expected_sha
    assert rows[0].freshness.snapshot_at == gen_published_at  # F-M1

    assert reader.find(session_id=f"opencode:{S1}").value[0].artifact.artifact_id \
        == art.artifact_id
    assert reader.find(producer=art.producer).value[0].artifact.artifact_id \
        == art.artifact_id
    assert reader.find(kind="contained").value[0].artifact.artifact_id \
        == art.artifact_id
    assert reader.find(since="2026-01-01T00:00:00Z").value[0].artifact.artifact_id \
        == art.artifact_id
    by_key = reader.find(annex_key=art.annex_key).value
    assert len(by_key) == 1 and by_key[0].artifact.artifact_id == art.artifact_id

    got = reader.get(art.artifact_id)
    assert got.value.kind == "contained"
    assert got.value.data == ARTIFACT_BYTES

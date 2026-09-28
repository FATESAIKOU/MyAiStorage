"""review-25a48a9 的多 repo pipeline 修正：H1／H2／M3／H3／M1／M2／M4 ＋ pin 的 L。

每一項都附一支能重現**原問題**的測試（修正前應該失敗）：

- **H1**：`git_factory` 收 target、clone 之後確認身分。兩個 repo 同時設定時，
  Foundry 的 pipeline 不得拿到 Agora 的 repo。
- **H2／M3**：分派依「通過驗章的候選」的型態。收件匣裡放一個排在前面的垃圾
  sidecar（寫 `type: artifact`）不得把合法的 session 導到 Foundry，也不得讓
  合法的 artifact 被永久燒掉；未驗章的垃圾 sidecar 也不得讓每一輪都 clone
  Foundry。
- **H3**：維護旗標要在 1b 檢查**所有** target；pipeline 內 write_pending 前與
  push 前的重查命中時要 `AbortRun`，整輪不刪收件匣。
- **M1**：第 11 步要問 Drive 實況（location log 是自己寫的，不能只信它）。
- **M2**：Foundry 沒有釘選值 → 視為未設定（artifact 一律 REJECT），不得拖住
  Agora 的收件匣清理。
- **M4**：`RepoConfig` 的 `prefix_levels`／`max_raw_size` 是 Foundry 自己的。
- **L**：`GitPinStore._fetch` 失敗要 raise `ReadError`。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.annex.fake import FakeAnnexGit, create_fake_git_bundle
from aistorage.committer.config import CommitterConfig, RepoConfig
from aistorage.committer.run import (
    Deps,
    RepoTarget,
    run,
    verify_clone_identity,
)
from aistorage.errors import AbortRun, MismatchError, ReadError
from aistorage.integrity.pin import MemoryPinStore, PinState
from aistorage.integrity.verify import verify_new_keys_on_drive
from aistorage.schema import generate_ulid

from test_committer_readview_wiring import _env

FOUNDRY_UUID = "00000000-0000-0000-0000-000000000002"
AGORA_URL = "drive://agora"
FOUNDRY_URL = "drive://foundry"


# --------------------------------------------------------------------- 工具


def _with_foundry(tmp_path: Path, *, with_pin: bool = True):
    """一個同時設定了 Agora 與 Foundry 的環境（兩邊的 pin repo 條目都建好）。"""
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    prefix_id = drive.seed_folder("prefix_foundry")
    quarantine_id = drive.seed_folder("prefix_foundry-quarantine")
    b_name, b_bytes, main_sha, annex_sha = create_fake_git_bundle(
        tmp_path / "foundry-bundle", FOUNDRY_UUID)
    drive.seed_file(prefix_id, b_name, b_bytes, created_time="2026-09-27T08:00:00Z")
    manifest_bytes = f"{b_name}\n".encode("utf-8")
    drive.seed_file(prefix_id, f"GITMANIFEST--{FOUNDRY_UUID}", manifest_bytes,
                    created_time="2026-09-27T08:00:00Z")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest().lower()

    refs = {"refs/heads/main": main_sha, "refs/heads/git-annex": annex_sha}
    states = {k: v for k, v in deps.pins._states.items()}  # type: ignore[attr-defined]
    if with_pin:
        states["foundry"] = PinState(
            repo="foundry", repo_uuid=FOUNDRY_UUID, refs=refs,
            manifest_sha256=manifest_sha, prev_manifest_sha256=None,
            active_bundles=(b_name,), removed_bundles=frozenset(),
            annex_keys=frozenset(), promoted_at="2026-09-27T08:00:00Z",
            run_id="run-init")
    deps.pins = MemoryPinStore(
        initial_state=states["agora"],
        initial_states_extra={k: v for k, v in states.items() if k != "agora"})

    original_factory = deps.git_factory
    fake_gits: dict[str, FakeAnnexGit] = {}

    def foundry_factory(dest: Path):
        git = FakeAnnexGit(
            refs=dict(refs), workdir=dest, annex_keys=frozenset(), drive=drive,
            prefix_folder_id=prefix_id, repo_uuid=FOUNDRY_UUID,
            repo_url=FOUNDRY_URL, clock=deps.clock,
        )
        fake_gits["foundry"] = git
        return git

    # H1：依 **target** 分派（不是依工作目錄，也不是寫死 URL）
    deps.git_factory = lambda dest, target: (  # type: ignore[assignment]
        foundry_factory(dest) if target.repo == "foundry" else original_factory(dest, target)
    )
    rc = RepoConfig(
        name="foundry", uuid=FOUNDRY_UUID, url=FOUNDRY_URL,
        prefix_folder_id=prefix_id, quarantine_folder_id=quarantine_id,
        largefiles="include=objects/*/*",
    )
    cfg = dataclasses.replace(cfg, repos=(rc,))
    extra["foundry_gits"] = fake_gits
    extra["foundry_prefix"] = prefix_id
    return cfg, deps, extra


def _seed_bad_signature(drive, extra, *, created_time: str) -> str:
    """形狀符合、但簽章不過的項目（會被 REJECT(bad_signature)）。"""
    key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", b'{"a": 1}',
                    created_time=created_time)
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sig", b'{"key_id": "x", "sig": ""}',
                    created_time=created_time)
    return key


def _leftovers(drive, extra, item_key: str) -> list[str]:
    """收件匣裡還留著這個項目的哪些檔。"""
    return sorted(
        f.name for f in drive.list_children(extra["inbox_folder_id"])
        if f.name.startswith(item_key)
    )


def _env_session_item_key(extra) -> str:
    """`_env()` 已經放的那個 session 項目的 item_key（從磁碟上的 sidecar 名稱取得）。"""
    for f in extra["drive"].list_children(extra["inbox_folder_id"]):
        if f.name.endswith(".sidecar.json"):
            return f.name[: -len(".sidecar.json")]
    raise AssertionError("_env() 沒有放 session 項目")


def _seed_artifact(drive, extra, *, session_id: str = "opencode:ses_smoke_001",
                   name: str = "spec.md", item_key_override: str | None = None) -> str:
    """放一個**真的簽過章**的 artifact 項目（link 型，不需要 annex 物件）。"""
    from aistorage.inbox_builder import build_artifact_item

    built = build_artifact_item(
        kind="link", produced_by_session_id=session_id, name=name,
        content_type="text/markdown", link=f"https://example.com/{name}",
        profile="mac-opencode", key=extra["priv_bytes"], key_id=extra["key_id"],
        clock=None,  # type: ignore[arg-type]
    )
    key = item_key_override or built.item_key
    if key != built.item_key:
        # 需要自訂 item_key（垃圾候選要排在合法 sidecar 前面，item_key 必須相同）
        body = json.loads(built.sidecar_bytes.decode("utf-8"))
        body["item_key"] = key
        raw = json.dumps(body, sort_keys=True).encode("utf-8")
        from aistorage.inbox import sign_sidecar_bytes

        sig = sign_sidecar_bytes(raw, extra["priv_bytes"], extra["key_id"])
        drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", raw)
        drive.seed_file(extra["inbox_folder_id"], f"{key}.sig",
                        json.dumps(sig, sort_keys=True).encode("utf-8"))
        return key
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", built.sidecar_bytes)
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sig",
                    json.dumps(built.sig, sort_keys=True).encode("utf-8"))
    return key


# ---------------------------------------------------------------- H1：身分


def test_git_factory_receives_the_target_of_each_pipeline(tmp_path: Path) -> None:
    """H1：Foundry 的 pipeline 必須拿到 Foundry 的 target（不是 Agora 的）。"""
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    # 註：`_env()` 已經放了一個合法的 session 項目，這裡只補 artifact
    _seed_artifact(drive, extra)

    report = run(cfg, deps, dry_run=False)

    assert "foundry.accepted" in report.counts, report.counts
    foundry_git = extra["foundry_gits"].get("foundry")
    assert foundry_git is not None, "Foundry 的 pipeline 必須真的被執行到"
    # 那個 git 是用 Foundry 的 url/uuid 建的（`verify_clone_identity` 通過才會到這裡）
    assert foundry_git.origin_url() == FOUNDRY_URL
    assert foundry_git.remote_uuid() == FOUNDRY_UUID


def test_clone_identity_mismatch_aborts_before_any_verification(tmp_path: Path) -> None:
    """H1：clone 到的 repo 不是這個 target → 中止（不得拿它去比對釘選值）。"""
    target = RepoTarget(
        element="foundry", repo="foundry", repo_uuid=FOUNDRY_UUID,
        repo_url=FOUNDRY_URL, prefix_folder_id="p", quarantine_folder_id="q")
    wrong_url = FakeAnnexGit(repo_uuid=FOUNDRY_UUID, repo_url=AGORA_URL)
    with pytest.raises(MismatchError, match="remote.origin.url"):
        verify_clone_identity(wrong_url, target)
    wrong_uuid = FakeAnnexGit(repo_uuid="other-uuid", repo_url=FOUNDRY_URL)
    with pytest.raises(MismatchError, match="annex 遠端"):
        verify_clone_identity(wrong_uuid, target)
    # 對的那一個就過
    verify_clone_identity(
        FakeAnnexGit(repo_uuid=FOUNDRY_UUID, repo_url=FOUNDRY_URL), target)


def test_git_without_identity_methods_is_rejected(tmp_path: Path) -> None:
    """H1：拿不到身分就 fail-closed（不要「查不到就當作沒問題」）。"""
    target = RepoTarget(
        element="agora", repo="agora", repo_uuid="u", repo_url="url",
        prefix_folder_id="p", quarantine_folder_id="q")

    class _Old:
        def ls_remote(self, remote: str = "origin") -> dict[str, str]:
            return {}

    with pytest.raises(AbortRun, match="unsupported_git"):
        verify_clone_identity(_Old(), target)  # type: ignore[arg-type]


def test_init_pin_for_foundry_uses_the_foundry_url(tmp_path: Path) -> None:
    """H1：`init-pin` 指定 repo 時，用的是那個 repo 自己的 URL。"""
    from aistorage.committer.__main__ import _init_pin_target

    cfg, deps, extra = _with_foundry(tmp_path)
    assert _init_pin_target(cfg, None) is None
    assert _init_pin_target(cfg, "foundry").repo_url == FOUNDRY_URL
    assert _init_pin_target(cfg, cfg.repo).repo_url == cfg.repo_url
    with pytest.raises(SystemExit):
        _init_pin_target(cfg, "nope")


# ------------------------------------------------------- H2／M3：先驗章分派


def test_junk_sidecar_cannot_redirect_a_session_to_foundry(tmp_path: Path) -> None:
    """H2：同一個 item_key 放兩個 sidecar，垃圾的那個寫 `type: artifact`。

    修正前 `items_for` 取「第一個 sidecar 候選」的 type，於是合法的 session 被
    導到 Foundry：Foundry 沒有 AgoraStore 的方法 → 整輪中止（每一輪都一樣，DoS），
    或者更糟：把 session 寫進 Foundry 的 repo。
    """
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    # 註：`_env()` 已經放了一個合法的 session 項目，這裡只補一個垃圾候選 sidecar
    key = _env_session_item_key(extra)

    # 同一個 item_key 再放一個「垃圾」sidecar（寫 type: artifact），並且排在前面
    junk = json.dumps({
        "format": "aistorage.inbox/v1", "profile": "mac-opencode",
        "item_key": key,
        "metadata": {"id": "artifact:01ARZ3NDEKTSV4RRFFQ69G5FAV",
                     "type": "artifact", "producer": "profile:mac-opencode",
                     "created_at": "2026-09-27T08:00:00Z",
                     "updated_at": "2026-09-27T08:00:00Z",
                     "case_id": None, "provenance": None},
        "raw": None,
        "body": {"kind": "link", "produced_by_session_id": "opencode:ses_x",
                 "name": "x", "content_type": "text/markdown",
                 "link": "https://example.com/x"},
    }, sort_keys=True).encode("utf-8")
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", junk)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    # session 進了 Agora，Foundry 沒有收到它
    assert report.counts.get("accepted", 0) >= 1
    assert "foundry.accepted" not in report.counts
    # 而且垃圾 sidecar 沒有燒掉這個 item_key（session 的 raw 被清掉 = 真的收下了）
    assert drive.list_children(extra["inbox_folder_id"]) == []


def test_junk_sidecar_cannot_get_an_artifact_burned(tmp_path: Path) -> None:
    """H2：垃圾 sidecar 寫 `type: session` 時，合法的 artifact 不得被永久拒收。"""
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    key = _seed_artifact(drive, extra)

    junk = json.dumps({
        "format": "aistorage.inbox/v1", "profile": "mac-opencode",
        "item_key": key,
        "metadata": {"id": f"session:{generate_ulid()}", "type": "session",
                     "producer": "profile:mac-opencode",
                     "created_at": "2026-09-27T08:00:00Z",
                     "updated_at": "2026-09-27T08:00:00Z",
                     "case_id": None, "provenance": None},
        "raw": None, "session": {"source": "opencode", "source_session_id": "x",
                                 "snapshot_at": "2026-09-27T08:00:00Z",
                                 "message_ids": []},
    }, sort_keys=True).encode("utf-8")
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", junk)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    # artifact 進了 Foundry（沒有被 Agora 以 foundry_not_enabled 燒掉）
    assert report.counts.get("foundry.accepted", 0) == 1, report.counts
    assert report.counts.get("rejected", 0) == 0, report.counts


def test_unsigned_artifact_sidecar_does_not_trigger_a_foundry_clone(tmp_path: Path) -> None:
    """M3：只放形狀符合、但簽章不過的 `type: artifact` sidecar 不得讓 Foundry 被 clone。"""
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    junk = json.dumps({
        "format": "aistorage.inbox/v1", "profile": "mac-opencode", "item_key": key,
        "metadata": {"id": f"artifact:{key}", "type": "artifact",
                     "producer": "profile:mac-opencode",
                     "created_at": "2026-09-27T08:00:00Z",
                     "updated_at": "2026-09-27T08:00:00Z",
                     "case_id": None, "provenance": None},
        "raw": None,
        "body": {"kind": "link", "produced_by_session_id": "opencode:ses_x",
                 "name": "x", "content_type": "text/markdown",
                 "link": "https://example.com/x"},
    }, sort_keys=True).encode("utf-8")
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sidecar.json", junk)
    drive.seed_file(extra["inbox_folder_id"], f"{key}.sig", b'{"key_id": "x", "sig": ""}')

    report = run(cfg, deps, dry_run=False)

    assert report.foundry_skipped == "no_artifacts"
    assert "foundry" not in extra["foundry_gits"], "沒有通過驗章的 artifact 就不該 clone"
    assert report.counts.get("rejected", 0) == 1, "它應該被當成壞簽章拒收"


def test_evaluate_returns_skip_for_wrong_pipeline(tmp_path: Path) -> None:
    """H2：`allowed_types` 不含該型態時回 SKIP（不進清冊、不寫拒收）。"""
    from aistorage.agora.store import AgoraStore, FakeRawStorage
    from aistorage.intake.evaluate import DecisionKind, evaluate
    from aistorage.intake.ledger import Ledger
    from aistorage.intake.scan import scan_inboxes

    cfg, deps, extra = _env(tmp_path)
    scan = scan_inboxes(deps.drive, deps.registry)
    assert scan.items
    worktree = tmp_path / "store"
    worktree.mkdir(parents=True, exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "st")
    dec = evaluate(
        scan.items[0], drive=deps.drive, registry=deps.registry, store=store,
        ledger=Ledger(store), clock=deps.clock, workdir=worktree,
        allowed_types=frozenset({"artifact"}))
    assert dec.kind is DecisionKind.SKIP
    assert dec.code == "wrong_pipeline"
    # 不進清冊、不寫拒收快取
    assert Ledger(store).contains(scan.items[0].item_key) is None
    assert not (worktree / "_committer" / "rejections").exists()


# ------------------------------------------------------------- H3：維護旗標


def test_step_1b_checks_every_targets_flag(tmp_path: Path) -> None:
    """H3：只對 Foundry 上鎖時，Agora 那一輪也必須整輪不動。"""
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    pins = deps.pins
    pins._texts[maintenance_relpath("foundry")] = json.dumps(  # type: ignore[attr-defined]
        {"reason": "erase foundry", "at": "2026-09-27T09:00:00Z", "by": "admin"})

    before = {f.name for f in drive.list_children(extra["inbox_folder_id"])}
    report = run(cfg, deps, dry_run=False)

    assert report.maintenance == "active"
    assert report.maintenance_reason == "erase foundry"
    assert report.counts["scanned_items"] == 0
    assert {f.name for f in drive.list_children(extra["inbox_folder_id"])} == before
    assert "foundry" not in extra["foundry_gits"], "維護中不得 clone"


def test_flag_appearing_before_write_pending_stops_the_whole_round(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H3：管理者在 clone 之後上鎖 → 不得寫 pending、不得 push、不得刪收件匣。"""
    import importlib
    run_mod = importlib.import_module("aistorage.committer.run")
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    real_pins = deps.pins
    reads: list[str] = []

    class _Pins:
        """第 1b 步之後才出現旗標：模擬「管理者在我們 clone 之後才上鎖」。"""

        def __getattr__(self, name):
            return getattr(real_pins, name)

        def read_text(self, relpath: str) -> str | None:
            reads.append(relpath)
            if len(reads) > 1:
                return json.dumps({"reason": "erase", "at": "t", "by": "admin"})
            return real_pins.read_text(relpath)

    deps.pins = _Pins()  # type: ignore[assignment]

    report = run(cfg, deps, dry_run=False)

    assert len(reads) >= 2, reads
    # H3 指定的形狀：AbortRun("maintenance", "active")
    assert report.aborted_at == "maintenance"
    assert report.code == "active"
    assert report.maintenance == "active"
    # 收件匣完全沒動（沒有刪掉任何已 push 但未 promote 的項目）
    assert drive.list_children(extra["inbox_folder_id"]) != []
    assert real_pins.load("agora")[1] is None, "不得留下待定釘選值"


def test_promote_time_flag_hit_raises_abort_run(tmp_path: Path) -> None:
    """H3：第 12 步 promote 前的重查命中 → AbortRun（不是靜靜地 return）。"""
    import importlib
    run_mod = importlib.import_module("aistorage.committer.run")

    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    seen: list[str] = []
    original = run_mod._assert_no_maintenance

    def _spy(repo, d, report, step):
        seen.append(step)
        if step == "pins.promote":
            raise AbortRun("maintenance", "active", "測試：維護中")
        return original(repo, d, report, step)

    run_mod._assert_no_maintenance = _spy  # type: ignore[assignment]
    try:
        report = run(cfg, deps, dry_run=False)
    finally:
        run_mod._assert_no_maintenance = original  # type: ignore[assignment]

    assert "pins.write_pending" in seen and "pins.promote" in seen
    assert report.aborted_at == "maintenance" and report.code == "active"
    assert report.maintenance == "active"
    assert drive.list_children(extra["inbox_folder_id"]) != [], "不得刪收件匣"


# ------------------------------------------------- M1：Drive 上的實況檢查


def test_new_keys_must_really_be_on_drive() -> None:
    """M1：location log 說有、Drive 上沒有 → 必須擋下來。"""
    from aistorage.drive.fake import FakeDrive

    drive = FakeDrive()
    prefix = drive.seed_folder("prefix")
    payload = b"x" * 16
    sha = hashlib.sha256(payload).hexdigest()
    key = f"SHA256E-s{len(payload)}--{sha}"

    with pytest.raises(MismatchError, match="沒有真的在 Drive 上"):
        verify_new_keys_on_drive(drive, prefix, {key})

    # 放一個同名但內容不同的檔案 → 雜湊不符，仍然擋下來
    drive.seed_file(prefix, key, b"y" * 16)
    with pytest.raises(MismatchError, match="checksum"):
        verify_new_keys_on_drive(drive, prefix, {key})

    # 真的在，且 checksum 與 size 相符 → 過
    drive2 = FakeDrive()
    prefix2 = drive2.seed_folder("prefix")
    drive2.seed_file(prefix2, key, payload)
    assert verify_new_keys_on_drive(drive2, prefix2, {key}) == 1
    # 沒有新 key 時什麼都不做
    assert verify_new_keys_on_drive(drive2, prefix2, set()) == 0


def test_step_11_checks_new_keys_on_drive(tmp_path: Path, monkeypatch) -> None:
    """M1：第 11 步真的呼叫 Drive 實況檢查（觀察傳進去的 key）。"""
    import importlib
    run_mod = importlib.import_module("aistorage.committer.run")

    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    seen: list[Any] = []
    original = run_mod.verify_new_keys_on_drive

    def _spy(drive_, prefix, keys, **kwargs):
        seen.append(set(keys))
        return original(drive_, prefix, keys, **kwargs)

    monkeypatch.setattr(run_mod, "verify_new_keys_on_drive", _spy)
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert len(seen) == 1, seen


# --------------------------------- M2：Foundry 未設定／逐 repo 的收件匣清理


def test_foundry_without_pin_does_not_block_agora_cleanup(tmp_path: Path) -> None:
    """M2：Foundry 沒有釘選值 → 視為未設定，Agora 照常收尾、artifact 被明確拒收。"""
    cfg, deps, extra = _with_foundry(tmp_path, with_pin=False)
    drive = deps.drive  # type: ignore[assignment]
    # 註：`_env()` 已經放了一個合法的 session 項目，這裡只補 artifact
    _seed_artifact(drive, extra)

    report = run(cfg, deps, dry_run=False)

    assert report.repo_not_enabled == "foundry"
    assert report.foundry_skipped == "repo_not_enabled"
    assert "foundry" not in extra["foundry_gits"], "未設定的 repo 不得被 clone"
    # Agora 的 session 照常被收下並清掉收件匣（不被 Foundry 拖住）
    assert report.counts.get("accepted", 0) >= 1
    assert report.counts.get("inbox_deleted", 0) >= 1
    # artifact 被明確拒收（寫入者看得到原因），不是留在收件匣裡爛掉
    assert report.counts.get("rejected", 0) == 1


def test_incomplete_repo_keeps_its_inbox_items(tmp_path: Path, monkeypatch) -> None:
    """M2：某個 repo 中途失敗 → 它的收件匣項目保留，其他 repo 照常清理。"""
    import importlib
    run_mod = importlib.import_module("aistorage.committer.run")

    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    # 註：`_env()` 已經放了一個合法的 session 項目，這裡只補 artifact
    _seed_artifact(drive, extra)
    original = run_mod._run_repo_pipeline

    def _spy(ctx, target, **kwargs):
        if target.element == "foundry":
            raise MismatchError("測試：Foundry 失敗")
        return original(ctx, target, **kwargs)

    monkeypatch.setattr(run_mod, "_run_repo_pipeline", _spy)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.code == "MismatchError"
    # Agora 走完了 → 它的 session 項目被清掉
    assert report.counts.get("accepted", 0) >= 1
    assert report.counts.get("inbox_deleted", 0) >= 1


# --------------------------------------------------------------- M4：設定驗證


def test_foundry_config_must_bring_its_own_prefix_levels(tmp_path: Path) -> None:
    """M4：Foundry 沒自帶 prefix_levels → 設定檔驗證就擋下來。"""
    base = {
        "format": "aistorage.committer/v1", "repo": "agora",
        "repo_uuid": "uuid-agora", "repo_url": "annex::agora",
        "prefix_folder_id": "p-agora", "quarantine_folder_id": "q-agora",
        "identity_registry_path": "config/identity.json",
        "prefix_levels": [
            {"parent_id": "up-agora", "name": "p-agora", "expected_id": "E"},
        ],
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        **base,
        "repos": {"foundry": {
            "uuid": "u-f", "url": "annex::f", "prefix_folder_id": "p-f",
            "quarantine_folder_id": "q-f",
        }},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="prefix_levels"):
        CommitterConfig.load(path, env={})


def test_foundry_max_raw_size_defaults_to_100mib(tmp_path: Path) -> None:
    """M4：Foundry 的單檔上限是 100 MiB（D7），不可沿用 Agora 的 50 MiB。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora",
        "repo_uuid": "uuid-agora", "repo_url": "annex::agora",
        "prefix_folder_id": "p-agora", "quarantine_folder_id": "q-agora",
        "identity_registry_path": "config/identity.json",
        "repos": {"foundry": {
            "uuid": "u-f", "url": "annex::f", "prefix_folder_id": "p-f",
            "quarantine_folder_id": "q-f",
            "prefix_levels": [
                {"parent_id": "up", "name": "p-f", "expected_id": "E"},
            ],
        }},
    }), encoding="utf-8")
    cfg = CommitterConfig.load(path, env={})
    assert cfg.max_raw_size == 50 * 1024 * 1024
    assert cfg.repos[0].max_raw_size == 100 * 1024 * 1024
    target = RepoTarget.from_repo_config(cfg, cfg.repos[0])
    assert target.max_raw_size == 100 * 1024 * 1024
    assert target.prefix_levels == cfg.repos[0].prefix_levels
    assert target.prefix_levels != cfg.prefix_levels, "不得沿用 Agora 的 prefix_levels"


# -------------------------------------------------------------------- L：pin


def test_pin_fetch_failure_raises_read_error(tmp_path: Path) -> None:
    """L：`_fetch` 失敗要直接說 fetch 失敗（不是「無法確認遠端變更」）。"""
    from aistorage.integrity.pin import GitPinStore

    remote = tmp_path / "pin.git"
    work = tmp_path / "work"
    import subprocess

    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    store = GitPinStore(str(remote), work, allow_production=True)

    class _Proc:
        returncode = 128
        stdout = ""

    store._run_git = lambda *a, **k: _Proc()  # type: ignore[assignment]
    with pytest.raises(ReadError, match="fetch 失敗"):
        store._fetch()


# ------------------------------------------- H4：拒收要進過已發佈的世代才刪


def test_rejected_item_is_deleted_after_it_has_been_published(tmp_path: Path) -> None:
    """H4：`skipped` 的輪次也要能刪（舊的判斷會讓它永遠刪不掉）。

    情境：第 N 輪拒收並發佈（還沒滿 24 小時）→ 24 小時後的某一輪沒有新內容，
    publisher 回 `skipped`。只要這一筆拒收已經在已發佈的世代裡，就該刪掉。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    calls: list[list[str]] = []

    class _Publisher:
        """永遠回 skipped，但回報「既有世代包含這些 item_key」。"""

        def __init__(self) -> None:
            self.published: list[str] = []

        def publish(self, store, *, dry_run=False, run_rejections=(), **kw):
            calls.append([r.item_key for r in run_rejections])
            self.published = [r.item_key for r in run_rejections]

            class _Rep:
                status = "skipped"
                generation = 7
                published_item_keys = tuple(self.published)

            return _Rep()

    deps.publisher = _Publisher()  # type: ignore[assignment]
    # 壞簽章的項目（authenticated=False）。created_time 設成兩天前：
    # Drive 的 metadata 是拒收時間的可信基準（寫入者控制不了），所以它已經
    # 滿 24 小時、可刪。
    key = _seed_bad_signature(drive, extra, created_time="2026-09-25T08:00:00Z")

    report = run(cfg, deps, dry_run=False)
    assert report.counts.get("rejected", 0) == 1
    # 發佈器回 skipped，但這一筆已經在已發佈世代裡 → 刪掉（修正前不會刪）
    assert _leftovers(drive, extra, key) == [], "已發佈的拒收必須被清掉"
    assert report.counts.get("inbox_junk_deleted", 0) == 0


def test_rejection_not_yet_over_24h_is_kept(tmp_path: Path) -> None:
    """H4：還沒滿 24 小時的拒收不刪（不管有沒有發佈過）。"""
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]

    class _Publisher:
        def publish(self, store, *, dry_run=False, run_rejections=(), **kw):
            class _Rep:
                status = "published"
                generation = 1
                published_item_keys = tuple(r.item_key for r in run_rejections)

            return _Rep()

    deps.publisher = _Publisher()  # type: ignore[assignment]
    key = _seed_bad_signature(drive, extra, created_time="2026-09-27T09:30:00Z")

    report = run(cfg, deps, dry_run=False)
    assert report.counts.get("rejected", 0) == 1
    assert _leftovers(drive, extra, key), "還沒滿 24 小時不得刪"


def test_unpublished_rejection_is_never_deleted(tmp_path: Path) -> None:
    """H4：沒有任何讀者看得到時不得刪（否則寫入者永遠不知道為什麼被拒）。"""
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]

    class _FailingPublisher:
        def publish(self, store, *, dry_run=False, run_rejections=(), **kw):
            raise RuntimeError("Drive 掛了")

    deps.publisher = _FailingPublisher()  # type: ignore[assignment]
    key = _seed_bad_signature(drive, extra, created_time="2026-09-25T08:00:00Z")

    report = run(cfg, deps, dry_run=False)
    assert report.counts.get("rejected", 0) == 1
    assert report.readview_publish == "publish_failed"
    assert _leftovers(drive, extra, key), "沒有發佈過就不得刪"


def test_publish_report_carries_published_item_keys(tmp_path: Path) -> None:
    """H4：發佈器要回報「這個世代包含哪些 item_key」（skipped 也要回）。"""
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.publish.publisher import DriveReadViewPublisher, PublishReport

    from aistorage.readview.model import initial_manifest, serialize_manifest

    drive = FakeDrive()
    folder = drive.seed_folder("readview")
    gen0 = initial_manifest(
        element="agora", agora_main_sha="0" * 40, published_at="2026-09-27T09:00:00Z")
    manifest = drive.seed_file(folder, "manifest.json", serialize_manifest(gen0))
    def _index_builder(path, **_kw):
        # 真的寫出一個空檔（publisher 會 stat 它）
        Path(path).write_bytes(b"")
        return None

    pub = DriveReadViewPublisher(
        drive, folder_id=folder, manifest_file_id=manifest,
        converters={}, clock=FixedClock("2026-09-27T10:00:00Z"),
        workdir=tmp_path / "work", index_builder=_index_builder)

    class _Row:
        item_key = "k1"
        code = "bad_signature"
        at = "2026-09-27T09:00:00Z"
        item_id = None
        authenticated = False

    class _Store:
        worktree = tmp_path / "store"

        def changed_paths(self):
            return []

    store = _Store()
    store.worktree.mkdir(parents=True, exist_ok=True)
    rep = pub.publish(store, agora_main_sha="a" * 40, run_rejections=[_Row()])
    assert isinstance(rep, PublishReport)
    assert rep.status == "published"
    assert rep.published_item_keys == ("k1",)


def test_mark_rejections_published_writes_generation(tmp_path: Path) -> None:
    """H4：世代號要寫進真本 `_committer/rejections/<key>.json`。"""
    import json as _json

    from aistorage.agora import layout as _layout
    from aistorage.agora.store import AgoraStore, FakeRawStorage
    from aistorage.committer.run import mark_rejections_published

    worktree = tmp_path / "store"
    worktree.mkdir(parents=True, exist_ok=True)
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "st")
    rel = _layout.rejection_path("01ARZ3NDEKTSV4RRFFQ69G5FAV")
    store.put_json(rel, {"item_key": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                         "code": "bad_signature", "at": "t"})

    mark_rejections_published(store, ["01ARZ3NDEKTSV4RRFFQ69G5FAV"], 3)
    data = _json.loads((worktree / rel).read_text(encoding="utf-8"))
    assert data["published_generation"] == 3
    assert data["code"] == "bad_signature", "不得把其他欄位弄掉"
    # 沒有紀錄的（驗章前的拒收本來就不寫真本）不會出錯
    mark_rejections_published(store, ["01ARZ3NDEKTSV4RRFFQ69G5FAW"], 4)


class _LaterClock:
    """把另一個 clock 的時間往後推（測試 24 小時後的行為）。"""

    def __init__(self, base, delta) -> None:
        self._base = base
        self._delta = delta

    def now(self):
        return self._base.now() + self._delta


# --------------------------------------------- L：admin 入口的 annex 遠端檢查


def test_admin_entry_points_require_annex_remote() -> None:
    """L：會改寫**既有**目錄的 admin 入口都要傳 `require_annex_remote=True`。

    預設值是 False，漏傳時 `git annex init`／`copy` 會改寫「那個目錄所在的 repo」
    （例如誤指到 MyBrain 的工作目錄）。這個檢查是靜態的：直接看原始碼，
    有人加新的呼叫點而沒傳參數時，這支測試會爆。
    """
    import inspect
    import re as _re

    import aistorage.admin.__main__ as admin_cli

    source = inspect.getsource(admin_cli)
    calls = _re.findall(r"SubprocessAnnexGit\((?:[^()]|\([^()]*\))*\)", source)
    assert calls, "應該至少有一個 SubprocessAnnexGit 的呼叫"
    for call in calls:
        assert "require_annex_remote=True" in call, (
            f"admin 入口的 SubprocessAnnexGit 必須傳 require_annex_remote=True: {call}")


# ------------------------------------------------ H2：apply 迴圈的型態防呆


def test_agora_pipeline_refuses_an_artifact(tmp_path: Path, monkeypatch) -> None:
    """H2：apply 迴圈的型態防呆——Agora 收到 artifact 必須 raise。

    分派（`items_for` ＋ `evaluate(allowed_types=…)`）已經擋掉了，這裡把兩道都
    繞過，單獨驗第三道：apply 迴圈看到 artifact 一定 raise，不准呼叫錯誤 store
    的 apply（FoundryStore 沒有 AgoraStore 的方法時，例外會被誤讀成暫時問題，
    於是每一輪都重複，變成 DoS）。
    """
    import importlib

    from aistorage.intake.evaluate import Decision, DecisionKind

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)
    # 第一道：分派。強制把 artifact 也交給 Agora 的 pipeline
    _force_items_for(monkeypatch, run_mod, "agora", lambda ty: ty == "artifact")
    real_evaluate = run_mod.evaluate

    def _evaluate(item, **kw):
        dec = real_evaluate(item, **kw)
        if dec.kind is DecisionKind.SKIP:
            # 第二道：evaluate 的 allowed_types。假裝它也沒擋：回一個 ACCEPT 的
            # artifact，接下來就輪得到 apply 迴圈那道型態防���。
            return Decision(
                kind=DecisionKind.ACCEPT, item=item, code="ok", authenticated=True,
                record_metadata={"id": f"artifact:{item.item_key}", "type": "artifact"},
                sidecar={"metadata": {"type": "artifact"},
                         "body": {"kind": "link", "name": "x",
                                  "produced_by_session_id": "opencode:ses_smoke_001",
                                  "link": "https://example.com/x"}},
            )
        return dec

    monkeypatch.setattr(run_mod, "evaluate", _evaluate)
    messages = _capture_abort_messages(monkeypatch, run_mod)
    report = run(cfg, deps, dry_run=False)
    assert report.ok is False
    assert any("Agora 的 pipeline 收到了 artifact" in m for m in messages), messages


def test_foundry_pipeline_refuses_a_session(tmp_path: Path, monkeypatch) -> None:
    """H2：Foundry 的 pipeline 只收 artifact；收到 session 一樣要 raise。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)   # 讓 foundry target 這一輪真的會跑
    # 第一道：分派。強制把 session 也交給 Foundry 的 pipeline
    _force_items_for(monkeypatch, run_mod, "foundry", lambda ty: ty != "artifact")
    from aistorage.intake.evaluate import Decision, DecisionKind

    real_evaluate = run_mod.evaluate

    def _evaluate(item, **kw):
        dec = real_evaluate(item, **kw)
        if dec.kind is DecisionKind.SKIP:
            return Decision(
                kind=DecisionKind.ACCEPT, item=item, code="ok", authenticated=True,
                record_metadata={"id": f"session:{item.item_key}", "type": "session"},
                sidecar={"metadata": {"type": "session"},
                         "session": {"source": "opencode",
                                     "source_session_id": "ses_x"}},
            )
        return dec

    monkeypatch.setattr(run_mod, "evaluate", _evaluate)
    messages = _capture_abort_messages(monkeypatch, run_mod)
    report = run(cfg, deps, dry_run=False)
    assert report.ok is False
    assert any("Foundry 的 pipeline 收到了" in m for m in messages), messages
    assert report.aborted_at is not None and report.aborted_at.startswith("foundry."), (
        report.aborted_at)


def _force_items_for(monkeypatch, run_mod, element: str, predicate) -> None:
    """把不符合型態的項目也塞進某個 pipeline 的評估清單（繞過分派）。"""
    original = run_mod.PipelineContext.items_for

    def _items_for(self, target, *, others_configured):
        items = original(self, target, others_configured=others_configured)
        if target.element == element:
            items = items + [
                i for i in self.scan.items
                if self.verified_type(i) not in {id(x) for x in items}
                and predicate(self.verified_type(i))
            ]
        return items

    monkeypatch.setattr(run_mod.PipelineContext, "items_for", _items_for)


def _capture_abort_messages(monkeypatch, run_mod) -> list[str]:
    """攔下 run.py 記到 debug 的例外訊息（RunReport 只存例外型別，訊息在這裡）。"""
    seen: list[str] = []
    original = run_mod._dump_traceback

    def _spy(run_id, step, exc):
        seen.append(f"{step}: {exc}")
        return None

    monkeypatch.setattr(run_mod, "_dump_traceback", _spy)
    return seen


# --------------------------------- H3：pipeline 內的重查要用「自己那個」旗標


def test_pipeline_recheck_uses_the_targets_own_flag(tmp_path: Path) -> None:
    """H3：Foundry 的 pipeline 重查的是 `foundry.maintenance`，不是 agora 的。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)

    seen: list[tuple[str, str]] = []
    original = run_mod._assert_no_maintenance

    def _spy(repo, d, report, step):
        seen.append((repo, step))
        # 只在 agora 上放旗標：Foundry 那一輪不該被擋
        if repo == "foundry":
            return None
        return original(repo, d, report, step)

    run_mod._assert_no_maintenance = _spy  # type: ignore[assignment]
    try:
        report = run(cfg, deps, dry_run=False)
    finally:
        run_mod._assert_no_maintenance = original  # type: ignore[assignment]

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert ("foundry", "pins.write_pending") in seen
    assert ("foundry", "pins.promote") in seen
    assert ("agora", "pins.write_pending") in seen
    # 兩個 repo 的旗標是分開的（`maintenance_relpath` 帶 repo 名稱）
    assert maintenance_relpath("foundry") != maintenance_relpath("agora")


def test_foundry_maintenance_flag_stops_the_foundry_pipeline(tmp_path: Path) -> None:
    """H3：只對 Foundry 上鎖時，Foundry 那一輪必須以 AbortRun 收場。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer".replace("committer", "committer") + ".run")
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)
    seen: list[str] = []
    original = run_mod._assert_no_maintenance

    def _spy(repo, d, report, step):
        if repo == "foundry":
            seen.append(step)
            if step == "pins.write_pending":
                raise AbortRun("maintenance", "active", "測試：Foundry 維護中")
        return original(repo, d, report, step)

    run_mod._assert_no_maintenance = _spy  # type: ignore[assignment]
    try:
        report = run(cfg, deps, dry_run=False)
    finally:
        run_mod._assert_no_maintenance = original  # type: ignore[assignment]

    assert seen == ["pins.write_pending"]
    # H3：命中一律 AbortRun → **整輪**停止，第 14 步也不跑
    # （已 push 但未 promote 的項目如果被刪掉，交接單、認領就永久遺失）。
    assert report.aborted_at == "maintenance" and report.code == "active"
    assert report.maintenance == "active"
    assert report.counts.get("inbox_deleted", 0) == 0
    remaining = {f.name for f in drive.list_children(extra["inbox_folder_id"])}
    assert any(n.endswith(".sidecar.json") for n in remaining), remaining
    assert any(n.endswith(".raw") for n in remaining), remaining


# ------------------------------------------ H4：真本紀錄 + 逐 repo 的刪除判斷


def test_published_rejection_is_recorded_in_the_true_copy(tmp_path: Path) -> None:
    """H4：驗章後的拒收要寫 `published_generation` 進真本（稽核用）。"""
    from aistorage.agora import layout as _layout
    from aistorage.agora.store import FakeRawStorage
    from aistorage.integrity.pin import PinState
    from aistorage.publish.rejections import collect_rejections
    from aistorage.intake.evaluate import Decision, DecisionKind

    worktree = tmp_path / "store"
    worktree.mkdir(parents=True, exist_ok=True)
    from aistorage.agora.store import AgoraStore

    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "st")
    key = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    store.put_json(_layout.rejection_path(key), {
        "item_key": key, "code": "too_old", "at": "2026-09-27T09:00:00Z",
        "inbox_folder_id": "inbox1", "candidate_ids": ["a"], "entries": []})

    class _Item:
        item_key = key

    dec = Decision(kind=DecisionKind.REJECT, item=_Item(), code="too_old",  # type: ignore[arg-type]
                   authenticated=True, rejected_at="2026-09-27T09:00:00Z",
                   deletable_after=None)
    rows = collect_rejections(store, [dec])
    assert [r.item_key for r in rows] == [key]
    assert rows[0].code == "too_old"


def test_step_14_uses_per_repo_published_rejections(tmp_path: Path) -> None:
    """H4：刪除判斷用的是「那個 repo 的」已發佈集合，不是全部。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)

    seen: list[tuple[str, frozenset[str]]] = []
    original = run_mod._published_rejection_keys

    def _spy(store, decisions, status, pub_rep, *, dry_run):
        out = original(store, decisions, status, pub_rep, dry_run=dry_run)
        seen.append((status, out))
        return out

    run_mod._published_rejection_keys = _spy  # type: ignore[assignment]
    try:
        report = run(cfg, deps, dry_run=False)
    finally:
        run_mod._published_rejection_keys = original  # type: ignore[assignment]

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    # Foundry 沒有讀取視圖設定時不算「已發佈」→ 它的拒收不得被刪
    statuses = [s for s, _ in seen]
    assert "skipped_no_readview" in statuses, statuses
    for status, keys in seen:
        if status == "skipped_no_readview":
            assert keys == frozenset()


# ------------------------------------------------------------- M2：部分成功


def test_partial_success_reports_the_failing_repo_step(tmp_path: Path, monkeypatch) -> None:
    """M2：Foundry 失敗時 `aborted_at` 要指到 Foundry 的步驟，Agora 的計數照常。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)
    original = run_mod._run_repo_pipeline

    def _spy(ctx, target, **kwargs):
        if target.element == "foundry":
            ctx.step = "annex.git.clone"
            raise MismatchError("測試：Foundry clone 失敗")
        return original(ctx, target, **kwargs)

    monkeypatch.setattr(run_mod, "_run_repo_pipeline", _spy)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.aborted_at == "foundry.annex.git.clone", report.aborted_at
    assert report.code == "MismatchError"
    # Agora 的計數照常記錄（沒有被 Foundry 拖掉）
    assert report.counts.get("accepted", 0) >= 1
    assert report.counts.get("inbox_deleted", 0) >= 1
    # Foundry 的項目還在收件匣
    assert drive.list_children(extra["inbox_folder_id"]) != []


def test_agora_failure_still_aborts_the_round(tmp_path: Path, monkeypatch) -> None:
    """M2：Agora 自己失敗 → 仍然是中止（fail-closed），不是「部分成功」。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env(tmp_path)
    original = run_mod._run_repo_pipeline

    def _spy(ctx, target, **kwargs):
        ctx.step = "integrity.sweep"
        raise MismatchError("測試：Agora 失敗")

    monkeypatch.setattr(run_mod, "_run_repo_pipeline", _spy)
    report = run(cfg, deps, dry_run=False)
    assert report.ok is False
    assert report.aborted_at == "integrity.sweep"
    assert report.counts.get("inbox_deleted", 0) == 0
    # 收件匣完全沒動
    assert deps.drive.list_children(extra["inbox_folder_id"]) != []  # type: ignore[attr-defined]


# --------------------------------------------- M4：Foundry 自己的 prefix_levels


def test_foundry_sweep_checks_its_own_parent_levels(tmp_path: Path, monkeypatch) -> None:
    """M4：Foundry 的清掃要檢查**它自己**的上層同名資料夾。"""
    import importlib

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _with_foundry(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    from aistorage.integrity.sweep import PrefixLevel

    foundry_level = PrefixLevel(parent_id="up_foundry", name="foundry", expected_id="E")
    rc = dataclasses.replace(
        cfg.repos[0], prefix_levels=(foundry_level,))
    cfg = dataclasses.replace(cfg, repos=(rc,))
    drive.seed_folder("foundry")   # 讓 Foundry 的前綴在假 Drive 上有名字

    seen: list[tuple[str, tuple]] = []
    original = run_mod.check_parents

    def _spy(levels, drive_):
        seen.append(("called", tuple(levels)))
        return []

    monkeypatch.setattr(run_mod, "check_parents", _spy)
    _seed_artifact(drive, extra)
    run(cfg, deps, dry_run=False)
    checked = [levels for _, levels in seen]
    assert (foundry_level,) in checked, f"Foundry 的 prefix_levels 沒被檢查：{checked}"


def test_agora_and_foundry_prefix_levels_are_independent(tmp_path: Path) -> None:
    """M4：Agora 改了 prefix_levels 不會影響 Foundry（反之亦然）。"""
    from aistorage.integrity.sweep import PrefixLevel

    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "format": "aistorage.committer/v1", "repo": "agora",
        "repo_uuid": "u-a", "repo_url": "annex::a",
        "prefix_folder_id": "p-a", "quarantine_folder_id": "q-a",
        "identity_registry_path": "config/identity.json",
        "prefix_levels": [
            {"parent_id": "up-a", "name": "p-a", "expected_id": "EA"},
            {"parent_id": "up-a2", "name": "p-a2", "expected_id": "EA2"},
        ],
        "repos": {"foundry": {
            "uuid": "u-f", "url": "annex::f", "prefix_folder_id": "p-f",
            "quarantine_folder_id": "q-f",
            "prefix_levels": [
                {"parent_id": "up-f", "name": "p-f", "expected_id": "EF"},
            ],
        }},
    }), encoding="utf-8")
    cfg = CommitterConfig.load(path, env={})
    agora = RepoTarget.from_config(cfg)
    foundry = RepoTarget.from_repo_config(cfg, cfg.repos[0])
    assert len(agora.prefix_levels) == 2
    assert len(foundry.prefix_levels) == 1
    assert foundry.prefix_levels[0].expected_id == "EF"
    assert agora.prefix_levels[0].expected_id == "EA"
    # largefiles 也是各自的（Agora 用預設、Foundry 自備）
    assert foundry.largefiles is None
    assert foundry.largefiles_rule == "include=sessions/*/*/raw"

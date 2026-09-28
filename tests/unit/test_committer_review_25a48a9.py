"""review-25a48a9 的提交流程修正：H1／H2／H3／M1／M2／M4 ＋ pin 的 L。

ADR 0009 之後提交流程只處理一個實體（Agora），原本「多 repo 分派」那���不再存在
（`RepoTarget`／`items_for`／Foundry pipeline 都已移除，見 tasks 7.1）。留下來的是
與分派無關、對單一 Agora 仍然成立的那部分防護，每一項都附一支能重現**原問題**的
測試（修正前應該失敗）：

- **H1**：`git_factory` 收設定、clone 之後確認身分。設定的 `repo_url`／`repo_uuid`
  不符就中止，不要拿錯的 repo 去比對釘選值。
- **H2**：垃圾候選 sidecar 不得左右結果（原本是「導到 Foundry」，現在是「不得燒掉
  合法的 session」）；apply 迴圈遇到不認得的型態要中止，不准靜默跳過。
- **H3**：維護旗標在 1b 檢查，pipeline 內 write_pending／push／promote 前重查命中時
  `AbortRun`，整輪不刪收件匣。
- **M1**：第 11 步要問 Drive 實況（location log 是自己寫的，不能只信它）。
- **M2**：pipeline 中途失敗 → 收件匣整組保留（fail-closed）。
- **L**：`GitPinStore._fetch` 失敗要 raise `ReadError`。
- **H4**：拒收要進過某個已發佈的世代才刪得掉。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.annex.fake import FakeAnnexGit
from aistorage.committer.config import CommitterConfig
from aistorage.committer.run import (
    Deps,
    run,
    verify_clone_identity,
)
from aistorage.errors import AbortRun, MismatchError, ReadError
from aistorage.integrity.pin import MemoryPinStore, PinState
from aistorage.integrity.verify import verify_new_keys_on_drive
from aistorage.schema import generate_ulid

from test_committer_readview_wiring import _env

AGORA_URL = "drive://agora"
FOUNDRY_UUID = "00000000-0000-0000-0000-000000000002"


# --------------------------------------------------------------------- 工具


def _cfg_with_identity(*, repo: str, repo_uuid: str, repo_url: str) -> CommitterConfig:
    """只給 `verify_clone_identity` 用的最小設定。"""
    return CommitterConfig(
        repo=repo,
        repo_uuid=repo_uuid,
        repo_url=repo_url,
        prefix_folder_id="p",
        quarantine_folder_id="q",
        identity_registry_path="config/identity.json",
    )


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
    """放一個**真的簽過章**的產出登錄項目（link 型，不需要 annex 物件）。

    ADR 0009 之後 Agora 不收它，這裡用來驗「明確拒收」與「垃圾候選不左右結果」。
    """
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


def test_clone_identity_mismatch_aborts_before_any_verification() -> None:
    """H1：clone 到的 repo 不是這份設定描述的 → 中止（不得拿它去比對釘選值）。"""
    cfg = _cfg_with_identity(repo="agora", repo_uuid=FOUNDRY_UUID, repo_url=AGORA_URL)
    wrong_url = FakeAnnexGit(repo_uuid=FOUNDRY_UUID, repo_url="drive://someone-else")
    with pytest.raises(MismatchError, match="remote.origin.url"):
        verify_clone_identity(wrong_url, cfg)
    wrong_uuid = FakeAnnexGit(repo_uuid="other-uuid", repo_url=AGORA_URL)
    with pytest.raises(MismatchError, match="annex 遠端"):
        verify_clone_identity(wrong_uuid, cfg)
    # 對的那一個就過
    verify_clone_identity(
        FakeAnnexGit(repo_uuid=FOUNDRY_UUID, repo_url=AGORA_URL), cfg)


def test_git_without_identity_methods_is_rejected() -> None:
    """H1：拿不到身分就 fail-closed（不要「查不到就當作沒問題」）。"""
    cfg = _cfg_with_identity(repo="agora", repo_uuid="u", repo_url="url")

    class _Old:
        def ls_remote(self, remote: str = "origin") -> dict[str, str]:
            return {}

    with pytest.raises(AbortRun, match="unsupported_git"):
        verify_clone_identity(_Old(), cfg)  # type: ignore[arg-type]


def test_git_factory_receives_the_configured_repo(tmp_path: Path) -> None:
    """H1：factory 拿到的設定就是這輪的設定（URL 與 uuid 都不會被寫死成別的）。"""
    cfg, deps, extra = _env(tmp_path)
    seen: list[tuple[str, str]] = []
    original = deps.git_factory

    def _factory(dest, cfg_):
        seen.append((cfg_.repo, cfg_.repo_url))
        return original(dest, cfg_)

    deps.git_factory = _factory  # type: ignore[assignment]
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert seen and set(seen) == {(cfg.repo, cfg.repo_url)}


# ------------------------------------------------------- H2：候選與型態


def test_junk_sidecar_cannot_burn_a_valid_session(tmp_path: Path) -> None:
    """H2：同一個 item_key 再放一個「垃圾」候選 sidecar（寫 `type: artifact`）。

    修正前的分派讀「第一個 sidecar 候選」的 type，於是合法的 session 被導去別的
    pipeline：那一邊沒有 AgoraStore 的方法 → 整輪中止（每一輪都一樣，DoS），或者
    更糟：把 session 寫進錯的 repo。判斷必須以**通過驗章的候選**為準。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    # 註：`_env()` 已經放了一個合法的 session 項目，這裡只補一個垃圾候選 sidecar
    key = _env_session_item_key(extra)

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
    # session 進了 Agora
    assert report.counts.get("accepted", 0) >= 1
    # 而且垃圾 sidecar 沒有燒掉這個 item_key（session 的 raw 被清掉 = 真的收下了）
    assert drive.list_children(extra["inbox_folder_id"]) == []


def test_artifact_item_is_explicitly_rejected(tmp_path: Path) -> None:
    """ADR 0009：產出登錄不進 Agora 的收件匣，但必須**明確拒收**。

    為什麼不是讓它躺在收件匣裡：沒有人評估的項目會讓收件匣永遠不是空的，而且
    寫入者永遠不知道為什麼被拒。拒收會被發佈到讀取視圖，24 小時後清掉。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert report.counts.get("rejected", 0) == 1, report.counts
    assert report.counts.get("accepted", 0) == 1, "只有 session 被收下"
    assert report.counts.get("inbox_deleted", 0) >= 1, "收下的 session 要被清掉"


def test_apply_loop_refuses_an_unknown_item_type(tmp_path: Path, monkeypatch) -> None:
    """H2：apply 迴圈遇到不認得的型態要中止，不准靜默跳過。

    靜默跳過的項目永遠不會被清掉，收件匣就永遠不是空的（而且真本裡不會有任何
    痕跡說明它被評估過）。寧可整輪中止。
    """
    import importlib

    from aistorage.intake.evaluate import Decision, DecisionKind

    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    _seed_artifact(drive, extra)
    real_evaluate = run_mod.evaluate

    def _evaluate(item, **kw):
        dec = real_evaluate(item, **kw)
        if dec.kind is DecisionKind.REJECT:
            # 假裝 evaluate 沒有拒收（繞過型態判斷），接下來輪到 apply 迴圈那道防呆
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
    assert any("apply 迴圈不認得型態" in m for m in messages), messages


def _capture_abort_messages(monkeypatch, run_mod) -> list[str]:
    """攔下 run.py 記到 debug 的例外訊息（RunReport 只存例外型別，訊息在這裡）。"""
    seen: list[str] = []
    original = run_mod._dump_traceback

    def _spy(run_id, step, exc):
        seen.append(f"{step}: {exc}")
        return None

    monkeypatch.setattr(run_mod, "_dump_traceback", _spy)
    return seen


# ------------------------------------------------------------- H3：維護旗標


def test_flag_present_at_step_1b_stops_the_whole_round(tmp_path: Path) -> None:
    """H3：第 1b 步就看到旗標 → 整輪不動（不掃描、不 clone、不刪收件匣）。"""
    from aistorage.admin.lock import maintenance_relpath

    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    deps.pins._texts[maintenance_relpath("agora")] = json.dumps(  # type: ignore[attr-defined]
        {"reason": "erase", "at": "2026-09-27T09:00:00Z", "by": "admin"})

    before = {f.name for f in drive.list_children(extra["inbox_folder_id"])}
    report = run(cfg, deps, dry_run=False)

    assert report.maintenance == "active"
    assert report.maintenance_reason == "erase"
    assert report.counts["scanned_items"] == 0
    assert {f.name for f in drive.list_children(extra["inbox_folder_id"])} == before


def test_flag_appearing_before_write_pending_stops_the_whole_round(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H3：管理者在 clone 之後上鎖 → 不得寫 pending、不得 push、不得刪收件匣。"""
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

    def _spy(cfg_, d, report, step):
        seen.append(step)
        if step == "pins.promote":
            raise AbortRun("maintenance", "active", "測試：維護中")
        return original(cfg_, d, report, step)

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
    seen: list[Any] = []
    original = run_mod.verify_new_keys_on_drive

    def _spy(drive_, prefix, keys, **kwargs):
        seen.append(set(keys))
        return original(drive_, prefix, keys, **kwargs)

    monkeypatch.setattr(run_mod, "verify_new_keys_on_drive", _spy)
    report = run(cfg, deps, dry_run=False)
    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert len(seen) == 1, seen


# --------------------------------------------- M2：pipeline 失敗就保留收件匣


def test_pipeline_failure_keeps_inbox_items(tmp_path: Path, monkeypatch) -> None:
    """M2：pipeline 中途失敗 → 收件匣整組保留（fail-closed），而且回報是中止。"""
    import importlib
    run_mod = importlib.import_module("aistorage.committer.run")

    cfg, deps, extra = _env(tmp_path)
    original = run_mod._run_pipeline

    def _spy(ctx):
        ctx.step = "integrity.sweep"
        raise MismatchError("測試：清掃失敗")

    monkeypatch.setattr(run_mod, "_run_pipeline", _spy)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is False
    assert report.aborted_at == "integrity.sweep"
    assert report.code == "MismatchError"
    assert report.counts.get("inbox_deleted", 0) == 0
    # 收件匣完全沒動
    assert deps.drive.list_children(extra["inbox_folder_id"]) != []  # type: ignore[attr-defined]


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


# ------------------------------------------- H4：拒收要進過已發佈的世代才刪


def test_rejected_item_is_deleted_after_it_has_been_published(tmp_path: Path) -> None:
    """H4：`skipped` 的輪次也要能刪（舊的判斷會讓它永遠刪不掉）。

    情境：第 N 輪拒收並發佈（還沒滿 24 小時）→ 24 小時後的某一輪沒有新內容，
    publisher 回 `skipped`。只要這一筆拒收已經在已發佈的世代裡，就該刪掉。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]

    class _Publisher:
        """永遠回 skipped，但回報「既有世代包含這些 item_key」。"""

        def publish(self, store, *, dry_run=False, run_rejections=(), **kw):
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


def test_published_rejection_is_recorded_in_the_true_copy(tmp_path: Path) -> None:
    """H4：驗章後的拒收要寫進真本（稽核用，publish/rejections 讀得到）。"""
    from aistorage.agora import layout as _layout
    from aistorage.agora.store import AgoraStore, FakeRawStorage
    from aistorage.intake.evaluate import Decision, DecisionKind
    from aistorage.publish.rejections import collect_rejections

    worktree = tmp_path / "store"
    worktree.mkdir(parents=True, exist_ok=True)
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


def test_artifact_rejection_code_is_published_as_the_reason(tmp_path: Path) -> None:
    """ADR 0009：拒收原因 `artifact_not_supported` 要進得了讀取視圖的發佈內容。

    寫入者看得到原因才可能改用新 Foundry 的路徑；看不到就只會一直重試。
    """
    cfg, deps, extra = _env(tmp_path)
    drive = deps.drive  # type: ignore[assignment]
    seen: list[tuple[str, str]] = []

    class _Publisher:
        def publish(self, store, *, dry_run=False, run_rejections=(), **kw):
            seen.extend((r.item_key, r.code) for r in run_rejections)

            class _Rep:
                status = "published"
                generation = 1
                published_item_keys = tuple(r.item_key for r in run_rejections)

            return _Rep()

    deps.publisher = _Publisher()  # type: ignore[assignment]
    key = _seed_artifact(drive, extra)

    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    # 這一筆拒收連同它的原因一起進了這次的發佈內容（寫入者看得到）
    assert (key, "artifact_not_supported") in seen, seen


# -------------------------------------------------- 設定：只有一個實體


def test_committer_config_refuses_a_stale_multi_repo_block(tmp_path: Path) -> None:
    """ADR 0009：設定檔只描述一個實體；舊的 `repos` 區塊必須**報錯**。

    默默忽略的後果是「以為 Foundry 還在跑，其實從期 1 開始就沒有任何東西處理它」，
    而且沒有任何人會發現（review-73dbf2c L）。
    """
    import json as _json

    base = {
        "format": "aistorage.committer/v1", "repo": "agora",
        "repo_uuid": "uuid-agora", "repo_url": "annex::agora",
        "prefix_folder_id": "p-agora", "quarantine_folder_id": "q-agora",
        "identity_registry_path": "config/identity.json",
    }

    def _load(extra: dict) -> CommitterConfig:
        path = tmp_path / "c.json"
        path.write_text(_json.dumps({**base, **extra}), encoding="utf-8")
        return CommitterConfig.load(path, env={})

    with pytest.raises(ValueError, match="repos"):
        _load({"repos": {"foundry": {"uuid": "u-f", "url": "annex::f",
                                     "prefix_folder_id": "p-f",
                                     "quarantine_folder_id": "q-f"}}})
    # 空物件一樣是殘留（放著只會讓人以為 Foundry 還有設定）
    with pytest.raises(ValueError, match="repos"):
        _load({"repos": {}})
    with pytest.raises(ValueError, match="_foundry"):
        _load({"readview_manifest_file_id_foundry": "1_ManifestFoundry"})

    # 只有 Agora 的設定照常載入
    cfg = _load({"readview_manifest_file_id": "1_ManifestAgora"})
    assert cfg.repo == "agora"
    assert not hasattr(cfg, "repos")

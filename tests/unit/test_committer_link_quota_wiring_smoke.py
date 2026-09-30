"""提交流程把設定檔的 `max_open_reservations_per_profile` 傳到 apply（未結預留上限）。

apply 層的額度語意（每個 profile 的未結預留、跨輪累計、預留開工後不再計分、
被拒不佔額度、0 與負數是程式錯誤）在 `test_continuation_dedup_quota_smoke.py`
驗過了。這裡驗的是**接線**與**設定檔驗證**：`committer/config.py` 的設定欄位真的有
到達 `apply_claim` 與 `apply_continuation`——不傳的話，apply 只會吃自己的預設值
（`DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE`），設定檔改了完全不會生效，而且沒有
任何測試會發現。

證明方式不是 spy 參數，而是**真的讓額度生效**：
`max_open_reservations_per_profile=1` 時，同一個 profile 的**第二個未結預留**會被
明確拒收成 `link_quota_exceeded`。

每一輪的目標 session 與接續單放在**同一批**提交：apply 的順序是
session → handoff → claim → continuation（`sort_accepted_decisions`），所以目標
session 一定先落地。分兩輪不行——單元測試環境的假 git 不會把上一輪的 commit
留給下一輪的 clone（`AgoraStore` 每輪開新的）。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
from pathlib import Path

import pytest

from aistorage.committer.config import (
    DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE,
    CommitterConfig,
)
from aistorage.committer.run import run
from aistorage.inbox_builder import (
    NewSessionReservation,
    build_claim_item,
    build_continuation_item,
    build_handoff_item,
)

from test_committer_readview_wiring import _env
from test_committer_smoke import _seed_valid_inbox_session

PROFILE = "mac-opencode"
NOW = "2026-09-27T09:30:00Z"


def _allow_continuation(deps) -> None:
    """把 `continuation` 加進這個 profile 的 allowed_types。

    共用的測試登錄檔 `_registry_payload` 還停在 `continuation` 出現之前
    （session／handoff／claim／reference／rewrite／artifact），所以接續單會在
    `intake.evaluate` 被判成 `unauthorized`，根本走不到 apply 與額度檢查。
    這裡就地補上，不去改別的線的共用夾具。
    """
    profiles = deps.registry._profiles  # noqa: SLF001 - 測試夾具就是這種形狀
    allowed = profiles[PROFILE]["allowed_types"]
    if "continuation" not in allowed:
        allowed.append("continuation")


def _upload(drive, inbox: str, built, raw: Path | None) -> None:
    if raw is not None:
        drive.seed_file(inbox, f"{built.item_key}.raw", raw.read_bytes())
    drive.seed_file(inbox, f"{built.item_key}.sidecar.json", built.sidecar_bytes)
    drive.seed_file(inbox, f"{built.item_key}.sig",
                    json.dumps(built.sig, sort_keys=True).encode("utf-8"))


def _empty_export(tmp_path: Path, tag: str) -> Path:
    """預留用的空匯出檔（零則訊息；`agora checkout` 就是這樣造的）。"""
    path = tmp_path / f"empty-{tag}.json"
    path.write_bytes(b'{"messages": []}')
    return path


def _continuation(drive, extra, tmp_path: Path, *,
                  target: str, new_id: str, snap_sha: str, message_id: str,
                  tag: str) -> None:
    empty = _empty_export(tmp_path, tag)
    built = build_continuation_item(
        target_session_id=target, new_session_id=new_id,
        continuation={"snapshot_sha256": snap_sha, "message_id": message_id},
        profile=PROFILE, key=extra["priv_bytes"], key_id=extra["key_id"], now=NOW,
        new_session=NewSessionReservation(
            session_id=new_id, raw_path=empty, snapshot_at=NOW),
    )
    _upload(drive, extra["inbox_folder_id"], built, empty)


def _handoff(drive, extra, tmp_path: Path, *,
             target: str, snap_sha: str, message_id: str) -> str:
    built = build_handoff_item(
        target_session_id=target,
        continuation={"snapshot_sha256": snap_sha, "message_id": message_id},
        body={"content": "接手後續調查"},
        profile=PROFILE, key=extra["priv_bytes"], key_id=extra["key_id"], now=NOW,
    )
    _upload(drive, extra["inbox_folder_id"], built, None)
    return built.item_id


def _claim(drive, extra, tmp_path: Path, *,
           handoff_id: str, claimer: str, tag: str) -> None:
    empty = _empty_export(tmp_path, tag)
    built = build_claim_item(
        handoff_id=handoff_id, claimer_session_id=claimer,
        profile=PROFILE, key=extra["priv_bytes"], key_id=extra["key_id"], now=NOW,
        new_session=NewSessionReservation(
            session_id=claimer, raw_path=empty, snapshot_at=NOW),
    )
    _upload(drive, extra["inbox_folder_id"], built, empty)


def _apply_results(monkeypatch, module) -> list[tuple[str, bool, str]]:
    """把 apply 換成會記錄結果的薄殼：真的呼叫原本的實作，只留下 (型態, ok, code)。

    這樣斷言的是「真的被拒成 link_quota_exceeded」，而不是「參數有沒有送到」。
    """
    seen: list[tuple[str, bool, str]] = []
    real_claim = module.apply_claim
    real_cont = module.apply_continuation

    def _claim(store, dec, clock, **kw):
        res = real_claim(store, dec, clock, **kw)
        seen.append(("claim", res.ok, res.code))
        return res

    def _cont(store, dec, conv, clock, **kw):
        res = real_cont(store, dec, conv, clock, **kw)
        seen.append(("continuation", res.ok, res.code))
        return res

    monkeypatch.setattr(module, "apply_claim", _claim)
    monkeypatch.setattr(module, "apply_continuation", _cont)
    return seen


def _env_with_cap(tmp_path: Path, cap: int | None):
    cfg, deps, extra = _env(tmp_path, seed_session=False)
    _allow_continuation(deps)
    if cap is not None:
        cfg = dataclasses.replace(cfg, max_open_reservations_per_profile=cap)
    return cfg, deps, extra


def test_configured_reservation_cap_reaches_apply_continuation(tmp_path, monkeypatch):
    """`max_open_reservations_per_profile=1` → 第二個未結預留被拒成 link_quota_exceeded。

    被拒是**明確決定**不是中止：整輪仍然成功（`report.ok`），拒收原因也會經由
    讀取視圖發佈給寫入端。
    """
    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env_with_cap(tmp_path, 1)
    drive, inbox = extra["drive"], extra["inbox_folder_id"]

    _key, snap_sha = _seed_valid_inbox_session(
        drive, inbox, extra["priv_bytes"], extra["key_id"],
        session_id="ses_target_001")
    _continuation(drive, extra, tmp_path, target="opencode:ses_target_001",
                  new_id="opencode:ses_branch_a", snap_sha=snap_sha,
                  message_id="m2", tag="a")
    _continuation(drive, extra, tmp_path, target="opencode:ses_target_001",
                  new_id="opencode:ses_branch_b", snap_sha=snap_sha,
                  message_id="m2", tag="b")

    seen = _apply_results(monkeypatch, run_mod)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert [s for s in seen if s[0] == "continuation"] == [
        ("continuation", True, "ok"),
        ("continuation", False, "link_quota_exceeded"),
    ], seen
    # 被拒的那筆不算「收下」，所以 accepted 只有 session ＋第一筆接續。
    # （`counts["rejected"]` 數的是 `intake.evaluate` 階段的拒收；apply 階段的
    # 拒絕是寫進清冊、再由讀取視圖發佈原因，不是那個計數器。）
    assert report.counts["accepted"] == 2, report.counts


def test_configured_reservation_cap_reaches_apply_claim(tmp_path, monkeypatch):
    """同一個設定值也要管到 `apply_claim`（交接單的認領同樣會預留一個新 session）。

    這一輪是「一筆 claim ＋ 一筆 continuation」：claim 先拿到唯一的名額
    （`_DISPATCH_ORDER`：handoff → claim → continuation），接續單撞上限。兩條路徑
    共用同一個上限（`_check_reservation_cap`），所以這裡同時證明設定值對
    claim 生效、以及兩種型態共用同一份額度。
    """
    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env_with_cap(tmp_path, 1)
    drive, inbox = extra["drive"], extra["inbox_folder_id"]

    _key, snap_sha = _seed_valid_inbox_session(
        drive, inbox, extra["priv_bytes"], extra["key_id"],
        session_id="ses_target_002")
    handoff_id = _handoff(drive, extra, tmp_path, target="opencode:ses_target_002",
                          snap_sha=snap_sha, message_id="m2")
    _claim(drive, extra, tmp_path, handoff_id=handoff_id,
           claimer="opencode:ses_claimer", tag="c")
    _continuation(drive, extra, tmp_path, target="opencode:ses_target_002",
                  new_id="opencode:ses_branch_c", snap_sha=snap_sha,
                  message_id="m2", tag="d")

    seen = _apply_results(monkeypatch, run_mod)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    by_kind: dict[str, tuple[bool, str]] = {}
    for kind, ok, code in seen:
        by_kind[kind] = (ok, code)
    assert by_kind.get("claim") == (True, "ok"), seen
    assert by_kind.get("continuation") == (False, "link_quota_exceeded"), seen
    # accepted：session ＋ handoff ＋ claim（被拒的接續單不算）
    assert report.counts["accepted"] == 3, report.counts


def test_default_cap_is_high_enough_for_two_links(tmp_path, monkeypatch):
    """反證：同一組項目在預設上限下**兩筆都收下**。

    沒有這一條的話，上面的測試可能只是「無論上限是什麼都拒第二筆」（例如兩筆
    因為別的原因被拒），證明力不足。
    """
    run_mod = importlib.import_module("aistorage.committer.run")
    cfg, deps, extra = _env_with_cap(tmp_path, None)
    assert cfg.max_open_reservations_per_profile >= 2, \
        cfg.max_open_reservations_per_profile
    drive, inbox = extra["drive"], extra["inbox_folder_id"]

    _key, snap_sha = _seed_valid_inbox_session(
        drive, inbox, extra["priv_bytes"], extra["key_id"],
        session_id="ses_target_003")
    _continuation(drive, extra, tmp_path, target="opencode:ses_target_003",
                  new_id="opencode:ses_ok_a", snap_sha=snap_sha,
                  message_id="m2", tag="e")
    _continuation(drive, extra, tmp_path, target="opencode:ses_target_003",
                  new_id="opencode:ses_ok_b", snap_sha=snap_sha,
                  message_id="m2", tag="f")

    seen = _apply_results(monkeypatch, run_mod)
    report = run(cfg, deps, dry_run=False)

    assert report.ok is True, f"{report.aborted_at}:{report.code}"
    assert [s for s in seen if s[0] == "continuation"] == [
        ("continuation", True, "ok"),
        ("continuation", True, "ok"),
    ], seen
    assert report.counts["accepted"] == 3, report.counts   # session + 兩筆接續
    assert report.counts["rejected"] == 0, report.counts


# ---------------------------------------------------------------------
# 設定檔驗證：只接受正整數
# ---------------------------------------------------------------------


def _config_json(tmp_path: Path, **overrides) -> Path:
    data = {
        "format": "aistorage.committer/v1",
        "repo": "agora",
        "repo_uuid": "uuid-1",
        "repo_url": "drive://agora",
        "prefix_folder_id": "pf",
        "quarantine_folder_id": "qf",
        "identity_registry_path": "config/identity.json",
    }
    data.update(overrides)
    cfg_path = tmp_path / "committer.json"
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    return cfg_path


def test_config_reads_a_positive_reservation_cap(tmp_path: Path) -> None:
    cfg = CommitterConfig.load(
        _config_json(tmp_path, max_open_reservations_per_profile=7), env={})
    assert cfg.max_open_reservations_per_profile == 7


def test_config_defaults_the_reservation_cap(tmp_path: Path) -> None:
    cfg = CommitterConfig.load(_config_json(tmp_path), env={})
    assert cfg.max_open_reservations_per_profile == \
        DEFAULT_MAX_OPEN_RESERVATIONS_PER_PROFILE
    assert cfg.max_open_reservations_per_profile > 0


@pytest.mark.parametrize("bad", [0, -1, -20, 1.5, "20", True, None])
def test_config_rejects_a_non_positive_integer_reservation_cap(
        tmp_path: Path, bad) -> None:
    """0、負數與非整數在**載入時**就報錯。

    舊版把 0 與負數當成「不設上限」，那等於一個打錯字就默默關掉成本上限
    （review-1926cd3-142fd04 建議）。報錯而不是默默照收，寧可開不起來。
    """
    with pytest.raises(ValueError, match="max_open_reservations_per_profile"):
        CommitterConfig.load(
            _config_json(tmp_path, max_open_reservations_per_profile=bad), env={})


def test_config_rejects_the_renamed_per_round_cap_field(tmp_path: Path) -> None:
    """舊欄位 `max_links_per_profile_per_round` 直接報錯，不會被安靜忽略。

    兩個欄位的意義不同（每輪限速 vs. 跨輪累計的未結預留），沿用舊值會讓上限變成
    另一件事；忽略舊欄位則會讓設定看起來有設、其實退回預設值。
    """
    with pytest.raises(ValueError, match="max_open_reservations_per_profile"):
        CommitterConfig.load(
            _config_json(tmp_path, max_links_per_profile_per_round=20), env={})

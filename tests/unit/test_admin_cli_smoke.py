"""admin CLI 的冒煙測試（只寫冒煙；需真 Drive 的子命令只測參數面與中止路徑）。"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aistorage.admin.__main__ import main


def test_cli_help_and_arg_errors() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["health", "--help"])


def test_cli_health_plist(capsys) -> None:
    """plist 的指令必須能自己跑（review M7：不能缺參數）。"""
    import plistlib

    rc = main(["health", "--plist"])
    assert rc == 0
    doc = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    assert doc["StartInterval"] == 21600
    assert doc["ProgramArguments"] == [
        "uv", "run", "python", "-m", "aistorage.admin", "health"]


def test_cli_health_data_json(tmp_path: Path, capsys) -> None:
    # `last_success_at` 要**相對於現在**：health CLI 內部用真的 datetime.now()，
    # 硬寫一個日期會隨著時間過去而越過 STALE_FAIL_HOURS(=30h)，讓這支測試在
    # 某天之後無故失敗（`test_admin_health_smoke.py` 傳 now= 才沒有這個問題）。
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    data = tmp_path / "health.json"
    data.write_text(json.dumps({
        "tokens_ok": {"committer": True},
        "workflow_enabled": True,
        "last_success_at": recent,
    }))
    rc = main(["health", "--data-json", str(data)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list) and out

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "tokens_ok": {"committer": False},
        "workflow_enabled": False,
    }))
    rc = main(["health", "--data-json", str(bad)])
    assert rc == 1

    rc = main(["health", "--data-json", str(tmp_path / "missing.json")])
    assert rc == 1


def test_cli_health_without_data_is_warn_not_ok(tmp_path: Path, capsys) -> None:
    """查不到憑證時不判 fail，但也不能判全綠（fail-open 修掉）。"""
    data = tmp_path / "empty.json"
    data.write_text("{}")
    rc = main(["health", "--data-json", str(data)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert all(c["status"] != "ok" or c["name"] in ("runs", "aborts", "quarantine", "syncer")
               for c in out)
    assert any(c["status"] == "warn" for c in out)


def _pin_remote(tmp_path: Path) -> str:
    remote = tmp_path / "pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    (seed / ".pin").mkdir()
    (seed / ".pin" / "agora.maintenance").write_text(
        json.dumps({"reason": "erase", "at": "t", "by": "admin"}))
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True)
    return str(remote)


def test_cli_lock_status_and_unlock(tmp_path: Path, capsys) -> None:
    remote = _pin_remote(tmp_path)
    url = f"file://{remote}"
    rc = main(["lock-status", "--pin-repo", url, "--repo", "agora",
               "--workdir", str(tmp_path / "w1")])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["maintenance"] is True and out["reason"] == "erase"

    # 沒有 --confirm 不解
    rc = main(["unlock", "--pin-repo", url, "--repo", "agora",
               "--workdir", str(tmp_path / "w2")])
    assert rc == 1
    assert "confirm" in capsys.readouterr().err

    # 沒有 --gh-repo 直接拒絕（M4：不再預設 repo），旗標不動
    rc = main(["unlock", "--pin-repo", url, "--repo", "agora", "--confirm",
               "--workdir", str(tmp_path / "w2b")])
    assert rc == 1
    assert "gh-repo" in capsys.readouterr().err
    rc = main(["lock-status", "--pin-repo", url, "--repo", "agora",
               "--workdir", str(tmp_path / "w2c")])
    assert json.loads(capsys.readouterr().out)["maintenance"] is True

    # gh 沒有登入時會失敗，但旗標已清掉（先清旗標是重點）
    rc = main(["unlock", "--pin-repo", url, "--repo", "agora", "--confirm",
               "--gh-repo", "owner/repo", "--workdir", str(tmp_path / "w3")])
    out_err = capsys.readouterr().err
    assert rc in (0, 1)
    rc = main(["lock-status", "--pin-repo", url, "--repo", "agora",
               "--workdir", str(tmp_path / "w4")])
    assert json.loads(capsys.readouterr().out)["maintenance"] is False


def _annex_like_repo(dest: Path) -> Path:
    remote = dest.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   check=True)
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin",
                    f"annex::{remote}"], check=True)
    return dest


def _config(tmp_path: Path, repo_dir: Path) -> Path:
    cfg = tmp_path / "committer.json"
    cfg.write_text(json.dumps({
        "format": "aistorage.committer/v1",
        "repo": "agora",
        "repo_uuid": "11111111-2222-3333-4444-555555555555",
        "repo_url": f"annex::{repo_dir.parent / 'remote.git'}",
        "prefix_folder_id": "folder_x",
        "quarantine_folder_id": "folder_q",
        "identity_registry_path": str(tmp_path / "registry.json"),
        "pin_repo_url": f"file://{repo_dir.parent / 'pin.git'}",
    }))
    return cfg


def test_cli_rollback_list_and_confirm(tmp_path: Path, capsys, monkeypatch) -> None:
    import hashlib

    from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
    from aistorage.schema import generate_ulid

    repo = _annex_like_repo(tmp_path / "clone")
    cfg = _config(tmp_path, repo)
    content = b'{"v": 1}'
    store = AgoraStore(repo, FakeRawStorage(), temp_dir=tmp_path / "t")
    raw = tmp_path / "r.raw"
    raw.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store.put_session(SessionRecord(
        id="opencode:s1", producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at="2026-09-27T08:00:00Z",
        status="stopped", snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=sha, raw_size=len(content),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key=generate_ulid(), title="t"), raw)
    monkeypatch.setenv("AISTORAGE_ALLOWED_WORKDIR", str(repo))

    rc = main(["rollback", "--config", str(cfg), "--repo-dir", str(repo),
               "--session", "opencode:s1", "--list"])
    assert rc == 0
    points = json.loads(capsys.readouterr().out)
    assert points[0]["snapshot_sha256"] == sha

    # 沒有 --confirm 不執行
    rc = main(["rollback", "--config", str(cfg), "--repo-dir", str(repo),
               "--session", "opencode:s1", "--to", sha, "--reason", "x"])
    assert rc == 1
    assert "confirm" in capsys.readouterr().err


def test_cli_erase_requires_targets(capsys) -> None:
    rc = main(["erase", "--config", "/nonexistent.json", "--canary", "x"])
    assert rc == 1
    assert "目標" in capsys.readouterr().err


def test_cli_erase_reports_bad_config(capsys) -> None:
    rc = main(["erase", "--config", "/nonexistent.json", "--canary", "x",
               "--session", "opencode:s1"])
    assert rc == 1
    assert "設定檔" in capsys.readouterr().err


def test_cli_recover_not_wired_without_credentials(capsys) -> None:
    rc = main(["recover", "--config", "/nonexistent.json"])
    assert rc == 1
    assert "設定檔" in capsys.readouterr().err

"""admin CLI 的冒煙測試（只寫冒煙；需真 Drive 的子命令只測參數面）。"""

import json
import subprocess
from pathlib import Path

import pytest

from aistorage.admin.__main__ import main


def test_cli_help_and_arg_errors(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["health", "--help"])


def test_cli_health_plist(capsys) -> None:
    import plistlib
    rc = main(["health", "--plist"])
    assert rc == 0
    doc = plistlib.loads(capsys.readouterr().out.encode("utf-8"))
    assert doc["StartInterval"] == 21600


def test_cli_health_data_json(tmp_path: Path, capsys) -> None:
    data = tmp_path / "health.json"
    data.write_text(json.dumps({
        "tokens_ok": {"committer": True},
        "workflow_enabled": True,
        "last_success_at": "2026-09-27T09:00:00.000Z",
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


def test_cli_lock_status_local_pin(tmp_path: Path, capsys) -> None:
    remote = tmp_path / "pin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)],
                   check=True)
    rc = main(["lock-status", "--pin-repo", f"file://{remote}", "--repo", "agora"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"maintenance": False}

    # 寫入旗標後看得到
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
    rc = main(["lock-status", "--pin-repo", f"file://{remote}", "--repo", "agora"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["maintenance"] is True and out["reason"] == "erase"


def test_cli_rollback_list_and_confirm(tmp_path: Path, capsys) -> None:
    import hashlib
    from aistorage.agora.store import AgoraStore, FakeRawStorage, SessionRecord
    from aistorage.schema import generate_ulid
    worktree = tmp_path / "agora"
    worktree.mkdir()
    store = AgoraStore(worktree, FakeRawStorage(), temp_dir=tmp_path / "t")
    content = b'{"v": 1}'
    p = tmp_path / "r.raw"
    p.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    store.put_session(SessionRecord(
        id="opencode:s1", producer="profile:mac-opencode",
        created_at="2026-09-27T08:00:00Z", updated_at="2026-09-27T08:00:00Z",
        status="running", snapshot_at="2026-09-27T08:00:00Z",
        raw_sha256=sha, raw_size=len(content),
        committed_at="2026-09-27T08:01:00Z",
        last_item_key=generate_ulid(), title="t"), p)
    rc = main(["rollback", "--repo-dir", str(worktree),
               "--session", "opencode:s1", "--list"])
    assert rc == 0
    points = json.loads(capsys.readouterr().out)
    assert points[0]["snapshot_sha256"] == sha

    # 沒有 --confirm 不執行
    rc = main(["rollback", "--repo-dir", str(worktree),
               "--session", "opencode:s1", "--to", sha, "--reason", "x"])
    assert rc == 1

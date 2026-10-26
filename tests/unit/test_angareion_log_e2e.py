"""End-to-end (throwaway) for angareion log (tasks 4.1)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json, entity
from angareion.backend import FakeBackend
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.registry import FakeRegistryClient
from angareion.messages import Message


TOKEN = "gh" + "p_FAKE_CLI_TOKEN_ABC123"
IDENTITY = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    clone = tmp_path / "clone"
    clone.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    config = Config(identity=IDENTITY, config_dir=clone, registry_repo="owner/MyAiStorage", registry_issue=77, token_file=token_file, state_dir=state_dir, config_file=config_file)
    save_config(config, config_file)
    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))
    git = FakeGit({f"origin/main:{CHANNELS_PATH}": channels_json({"general": ("github-main", 10)}), f"origin/main:{BACKENDS_PATH}": backends_json({"github-main": ("github", "owner/repo")})}, fetch_head_mtime=1000.0)
    store = ChannelStore(clone, git=git, interval=0)
    backend = FakeBackend(store)
    backend.add_entity("general", number=10)
    registry = FakeRegistryClient()
    return {"config": config, "store": store, "backend": backend, "registry": registry, "state_dir": state_dir, "tmp_path": tmp_path}


def run(env, argv):
    return main(argv, backend=env["backend"], store=env["store"], registry=env["registry"])


def caprun(env, argv):
    buf = io.StringIO()
    real = sys.stdout
    sys.stdout = buf
    rc = run(env, argv)
    sys.stdout = real
    return rc, buf.getvalue()


def test_e2e_multiline_thread_around_cursor(env):
    target = entity(channel="general", issue=10)
    # thread: m3 <- m5 <- m9; m8 replies m3
    ids = {
        "m3": "01M03000000000000000000000",
        "m5": "01M05000000000000000000000",
        "m8": "01M08000000000000000000000",
        "m9": "01M09000000000000000000000",
    }
    for cid, content, irt in [(3, "m3", None), (5, "m5\nline2", "01M03000000000000000000000"), (8, "m8", "01M03000000000000000000000"), (9, "m9", "01M05000000000000000000000")]:
        mid = ids[{3:"m3",5:"m5",8:"m8",9:"m9"}[cid]]
        msg = Message(from_=PM, to=IDENTITY, channel="general", content=content, id=mid, at=f"2026-10-10T12:00:{cid:02d}Z", in_reply_to=irt)
        env["backend"].deliver(target, msg, comment_id=cid, created_at=f"2026-10-10T12:00:{cid:02d}Z")
    cursor_file = env["state_dir"] / "cursor-mylinuxpool-impl-w1-1.json"
    if cursor_file.exists():
        cursor_file.unlink()
    before = hashlib.sha256(cursor_file.read_bytes()).hexdigest() if cursor_file.exists() else None

    # full content
    rc, out = caprun(env, ["log", "--channel", "general", "--json"])
    assert rc == 0
    data = json.loads(out)
    m5 = [m for m in data if m["id"] == ids["m5"]][0]
    assert m5["content"] == "m5\nline2"

    # around restores middle
    rc, out = caprun(env, ["log", "--channel", "general", "--around", ids["m5"], "--before", "1", "--after", "1", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"].split("\n")[0] for m in data] == ["m3", "m5", "m8"] or [m["content"].split("\n")[0] for m in data] == ["m3", "m5", "m8"]

    # thread
    rc, out = caprun(env, ["log", "--channel", "general", "--thread", ids["m5"], "--json"])
    assert rc == 0
    data = json.loads(out)
    assert [m["content"].split("\n")[0] for m in data] == ["m3", "m5", "m8", "m9"]

    # cursor unchanged
    after = hashlib.sha256(cursor_file.read_bytes()).hexdigest() if cursor_file.exists() else None
    assert before == after
    assert not cursor_file.exists()


def test_e2e_angareion_log_to_me_vs_aicomm_log_argv(env, monkeypatch, capsys):
    # aicomm log should inject --to me
    import os
    from _aicomm_fakes import install_fake_angareion, run_aicomm
    bin_dir = env["tmp_path"] / "bin"
    install_fake_angareion(bin_dir)
    log = env["tmp_path"] / "ang.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ANG_LOG", str(log))
    monkeypatch.setenv("ANGAREION_IDENTITY", IDENTITY)
    monkeypatch.setenv("AICOMM_SIDECAR", "http://127.0.0.1:8471")
    # sidecar not needed for log; but run_aicomm asserts
    from _aicomm_fakes import FakeSidecar
    sc = FakeSidecar()
    monkeypatch.setenv("AICOMM_SIDECAR", sc.url)
    try:
        r = run_aicomm(["log"], monkeypatch, capsys)
        assert r.code == 0
        import json as _j
        calls = [ _j.loads(l) for l in log.read_text().splitlines() ]
        assert calls and calls[-1]["argv"][:3] == ["log", "--to", "me"]
    finally:
        sc.close()

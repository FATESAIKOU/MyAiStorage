import _guard  # noqa: F401
import json
import shutil
import pytest
from agora import cli, store
from test_cli import FakeAgent, env, run  # noqa: F401
from test_search_json import _staged, remote  # noqa: F401
from test_search_sort_limit import _staged_as, LOW, HIGH
from test_search_strict import _fixture, _on_drive


def search(capsys, *extra):
    code, out, err = run(capsys, "search", "session", "--filter", "tags=role:R",
                         "--sort", "updated", "--json", *extra)
    assert code == 0, err
    return json.loads(out)


def test_role_order_exact_match_and_limit(remote, capsys):
    ids = [_staged(session_id=f"s{i}", tags=["role:R"],
                   updated_at=f"2026-10-{10+i}T00:00:00Z") for i in range(3)]
    _staged(session_id="other", tags=["role:B", "role:R-long"])
    assert [r["id"] for r in search(capsys, "--strict")] == ids[::-1]
    assert [r["id"] for r in search(capsys, "--strict", "--limit", "1")] == ids[-1:]


def test_role_updated_tie_uses_ulid_descending(remote, capsys):
    low = _staged_as(LOW, session_id="low", tags=["role:R"])
    high = _staged_as(HIGH, session_id="high", tags=["role:R"])
    assert [r["id"] for r in search(capsys, "--strict")] == [high, low]


@pytest.mark.parametrize("source,target", [("opencode", "opencode"), ("claude", "claude"),
                                          ("claude", "opencode"), ("opencode", "claude")])
def test_role_continue_preserves_id_and_tags(env, capsys, monkeypatch, tmp_path, source, target):
    agents = {name: FakeAgent() for name in ("claude", "opencode")}
    for name, agent in agents.items():
        agent.name = name
    monkeypatch.setattr(cli, "load_agent", agents.__getitem__)
    code, sid, err = run(capsys, "import", "session", "--agent", source,
                         "--external-session-id", "ses_a", "--add-tag", "keep", "--add-tag", "role:R")
    assert code == 0, err
    code, out, err = run(capsys, "continue", "session", sid, "--agent", target,
                         "--dir", str(tmp_path), "--add-tag", "role:R")
    assert code == 0 and out == sid, err
    hdr = store.Index(store.Paths.from_env()).header(sid.split(":")[1])
    assert hdr["tags"] == ["keep", "role:R"]
    assert hdr["agora"]["source"]["agent"] == target
    assert agents[target].launched


def test_role_first_session_recorded_and_found(env, capsys):
    import os
    from pathlib import Path
    (Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions").mkdir(parents=True)
    assert search(capsys, "--strict") == []
    env.sessions["fresh"] = ["new conversation", "done"]
    code, sid, err = run(capsys, "import", "session", "--agent", "opencode",
                         "--external-session-id", "fresh", "--add-tag", "role:R")
    assert code == 0, err
    assert [r["id"] for r in search(capsys, "--strict")] == [sid]


def clean_sync_fixture(kind):
    old = _fixture(kind)
    paths = store.Paths.from_env()
    assert not store.waiting_ulids(paths)
    assert not list(paths.pending.glob("*.json"))
    return paths, old


@pytest.mark.parametrize("kind", ["empty", "old"])
@pytest.mark.parametrize("stamp_kind", ["absent", "expired"])
def test_plain_listing_failure_preserves_stamp(remote, capsys, monkeypatch, kind, stamp_kind):
    paths, old = clean_sync_fixture(kind)
    stamp = paths.state / "last-sync"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    if stamp_kind == "expired":
        stamp.write_text("1")
    else:
        stamp.unlink(missing_ok=True)
    before = stamp.read_bytes() if stamp.exists() else None
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "lsjson")
    code, out, _ = run(capsys, "search", "session", "--json")
    assert code == 0
    assert [r["id"] for r in json.loads(out)] == ([old] if old else [])
    assert (stamp.read_bytes() if stamp.exists() else None) == before


@pytest.mark.parametrize("kind", ["empty", "old"])
@pytest.mark.parametrize("failure", ["download", "bulk"])
def test_plain_download_failure_or_bulk_recovery(remote, capsys, monkeypatch, kind, failure):
    paths, old = clean_sync_fixture(kind)
    newest = _staged(title="latest", session_id="latest", tags=["role:r"],
                     updated_at="2026-11-08T00:00:00Z")
    store.upload_batch(store.Drive(paths), paths)
    other = _on_drive("other")
    for sid in ((newest, other) if failure == "bulk" else (newest,)):
        ulid = sid.split(":")[1]
        shutil.rmtree(paths.mirror / ulid)
        store.Index(paths).drop(ulid)
    stamp = paths.state / "last-sync"
    stamp.unlink(missing_ok=True)
    if failure == "download":
        monkeypatch.setenv("FAKE_RCLONE_FAIL", f"sessions/{newest.split(':')[1]}/session.md")
    else:
        monkeypatch.setenv("FAKE_RCLONE_BULK_PARTIAL", "1")
    code, out, _ = run(capsys, "search", "session", "--json")
    assert code == 0
    ids = {r["id"] for r in json.loads(out)}
    assert (newest in ids) == (failure == "bulk")
    assert other in ids
    if old:
        assert old in ids
    assert stamp.exists() and float(stamp.read_text()) > 1


def test_role_deleted_during_continue_is_rescued_with_tags(env, capsys, monkeypatch, tmp_path):
    import os
    from pathlib import Path
    code, sid, err = run(capsys, "import", "session", "--agent", "opencode",
                         "--external-session-id", "ses_a", "--add-tag", "keep", "--add-tag", "role:R")
    assert code == 0, err
    collect = env.collect
    def collect_after_deletion(launch):
        result = collect(launch)
        shutil.rmtree(Path(os.environ["FAKE_REMOTE"]) / "agora" / "sessions" / sid.split(":")[1])
        return result
    monkeypatch.setattr(env, "collect", collect_after_deletion)
    code, rescued, err = run(capsys, "continue", "session", sid, "--agent", "opencode",
                             "--dir", str(tmp_path), "--add-tag", "role:R")
    assert code == 0 and rescued != sid, err
    hdr = store.Index(store.Paths.from_env()).header(rescued.split(":")[1])
    assert hdr["tags"] == ["keep", "role:R"]
    assert [p["id"] for p in hdr["agora"]["parents"]] == [sid]

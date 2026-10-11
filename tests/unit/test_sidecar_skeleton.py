"""Unit tests for sidecar skeleton: config, registry, queue, and CLI status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sidecar.cli import main
from sidecar.config import SidecarConfig, get_default_config_path, get_default_state_dir, load_config
from sidecar.queue import MessageQueue
from sidecar.registry import AgentRecord, Registry


def test_config_defaults_and_env_overrides(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SIDECAR_CONFIG", raising=False)
    monkeypatch.delenv("SIDECAR_STATE_DIR", raising=False)

    assert get_default_config_path() == home / ".config" / "sidecar" / "config.json"
    assert get_default_state_dir() == home / ".local" / "state" / "sidecar"

    custom_cfg = tmp_path / "custom" / "cfg.json"
    custom_state = tmp_path / "custom" / "state"
    monkeypatch.setenv("SIDECAR_CONFIG", str(custom_cfg))
    monkeypatch.setenv("SIDECAR_STATE_DIR", str(custom_state))

    assert get_default_config_path() == custom_cfg
    assert get_default_state_dir() == custom_state

    # Missing file returns default object
    cfg = load_config()
    assert cfg.config_path == custom_cfg
    assert cfg.state_dir == custom_state
    assert cfg.identities == []


def test_config_loading_and_token_permissions(tmp_path):
    token_file = tmp_path / "token.txt"
    token_file.write_text("secret_token\n")
    token_file.chmod(0o600)

    cfg_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    cfg_data = {
        "state_dir": str(state_dir),
        "a2a_token_path": str(token_file),
        "identities": ["team-alpha", "team-beta"],
        "teams": {
            "team-alpha": {
                "agent": "opencode",
                "home_session": "agora:01JALPHA",
                "harness_path": "/path/to/harness",
            }
        },
        "llm_gateway": "http://127.0.0.1:8000",
    }
    cfg_file.write_text(json.dumps(cfg_data))

    cfg = load_config(cfg_file)
    assert cfg.config_path == cfg_file
    assert cfg.state_dir == state_dir
    assert cfg.identities == ["team-alpha", "team-beta"]
    assert cfg.teams["team-alpha"]["agent"] == "opencode"
    assert cfg.verify_token_permissions() is True

    # Insecure permission 0644 must fail verification
    token_file.chmod(0o644)
    assert cfg.verify_token_permissions() is False


def test_registry_atomic_persistence_and_restart_recovery(tmp_path):
    state_dir = tmp_path / "state"
    reg1 = Registry(state_dir)
    assert reg1.list_agents() == []

    rec = AgentRecord(
        agent_id="ag_01JTEST1",
        identity="team-alpha",
        agora_session="agora:01JTEST1",
        agent_kind="opencode",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name="impl1",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "agents" / "ag_01JTEST1" / "log"),
        updated_at="2026-10-11T12:00:00Z",
    )
    reg1.register(rec)

    # File exists on disk
    reg_file = state_dir / "registry.json"
    assert reg_file.is_file()
    assert (state_dir / ".tmp-registry.json").exists() is False

    # Lookup
    found = reg1.get("ag_01JTEST1")
    assert found is not None
    assert found.agent_name == "impl1"
    assert reg1.get_by_name("impl1") == found

    # Restart recovery: a new Registry instance reads from disk
    reg2 = Registry(state_dir)
    recovered = reg2.get("ag_01JTEST1")
    assert recovered is not None
    assert recovered.status == "running"
    assert recovered.herdr_pane_id == "w1:p1"

    # Status update
    assert reg2.update_status("ag_01JTEST1", "terminated", exit_reason="ctrl+c") is True
    assert reg2.get("ag_01JTEST1").status == "terminated"
    assert reg2.get("ag_01JTEST1").exit_reason == "ctrl+c"

    # Persisted after update
    reg3 = Registry(state_dir)
    assert reg3.get("ag_01JTEST1").status == "terminated"


def test_queue_atomic_priority_ordering_and_deduplication(tmp_path):
    state_dir = tmp_path / "state"
    q1 = MessageQueue(state_dir)

    ident = "team-alpha"
    msg_low = {
        "id": "msg-1",
        "from": "pmo",
        "to": ident,
        "urgency": 3,
        "at": "2026-10-11T10:00:00Z",
        "content": "routine update",
    }
    msg_high = {
        "id": "msg-2",
        "from": "pmo",
        "to": ident,
        "urgency": 9,
        "at": "2026-10-11T10:05:00Z",
        "content": "emergency interrupt",
    }
    msg_same_urgency_earlier = {
        "id": "msg-3",
        "from": "pmo",
        "to": ident,
        "urgency": 5,
        "at": "2026-10-11T09:00:00Z",
        "content": "earlier five",
    }
    msg_same_urgency_later = {
        "id": "msg-4",
        "from": "pmo",
        "to": ident,
        "urgency": 5,
        "at": "2026-10-11T11:00:00Z",
        "content": "later five",
    }

    assert q1.enqueue(ident, msg_low) is True
    assert q1.enqueue(ident, msg_high) is True
    assert q1.enqueue(ident, msg_same_urgency_later) is True
    assert q1.enqueue(ident, msg_same_urgency_earlier) is True

    # Deduplication by ID: duplicate enqueue must return False and not duplicate
    assert q1.enqueue(ident, {"id": "msg-2", "content": "duplicate"}) is False
    assert q1.count(ident) == 4

    # Ordering check: 9 -> 5 (earlier 09:00) -> 5 (later 11:00) -> 3
    peeked = q1.peek(ident)
    assert [m["id"] for m in peeked] == ["msg-2", "msg-3", "msg-4", "msg-1"]

    # Restart recovery: new queue object reads from disk
    q2 = MessageQueue(state_dir)
    assert q2.count(ident) == 4
    assert [m["id"] for m in q2.peek(ident)] == ["msg-2", "msg-3", "msg-4", "msg-1"]

    # Pop highest priority
    popped = q2.pop(ident)
    assert popped["id"] == "msg-2"
    assert q2.count(ident) == 3

    # Pop specific ID
    popped_specific = q2.pop(ident, message_id="msg-4")
    assert popped_specific["id"] == "msg-4"
    assert q2.count(ident) == 2

    # Remove by ID
    assert q2.remove(ident, "msg-3") is True
    assert q2.count(ident) == 1
    assert q2.pop(ident)["id"] == "msg-1"
    assert q2.count(ident) == 0
    assert q2.pop(ident) is None


def test_cli_status_text_and_json(tmp_path, capsys, monkeypatch):
    cfg_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    cfg_data = {
        "state_dir": str(state_dir),
        "identities": ["team-gamma"],
        "teams": {},
    }
    cfg_file.write_text(json.dumps(cfg_data))

    reg = Registry(state_dir)
    reg.register(
        AgentRecord(
            agent_id="ag_1",
            identity="team-gamma",
            agora_session="agora:01J1",
            agent_kind="claude",
            herdr_tab_id="w1:t1",
            herdr_pane_id="w1:p1",
            agent_name="tester",
            started_at="2026-10-11T12:00:00Z",
            status="running",
            log_path=str(state_dir / "log"),
            updated_at="2026-10-11T12:00:00Z",
        )
    )

    q = MessageQueue(state_dir)
    q.enqueue("team-gamma", {"id": "m1", "urgency": 5, "at": "2026-10-11T12:00:00Z", "content": "hi"})

    # Test status --json
    code = main(["--config", str(cfg_file), "status", "--json"])
    assert code == 0
    out, _ = capsys.readouterr()
    parsed = json.loads(out)
    assert parsed["status"] == "ok"
    assert parsed["agents"]["total"] == 1
    assert parsed["agents"]["running"] == 1
    assert parsed["queues"]["team-gamma"] == 1

    # Test status (text format)
    code = main(["--config", str(cfg_file), "status"])
    assert code == 0
    out_text, _ = capsys.readouterr()
    assert "Sidecar Status:" in out_text
    assert "1 running / 1 total" in out_text
    assert "team-gamma: 1 queued" in out_text


def test_queue_identity_with_special_characters_encoded(tmp_path):
    state_dir = tmp_path / "state"
    q = MessageQueue(state_dir)

    identities = [
        "owner-org/worker-team",
        "repo:role",
        "nested/path/to/org:sub-role",
    ]

    for ident in identities:
        msg = {
            "id": f"msg-{ident}",
            "urgency": 7,
            "content": f"hello for {ident}",
        }
        assert q.enqueue(ident, msg) is True
        assert q.count(ident) == 1
        assert q.peek(ident)[0]["id"] == f"msg-{ident}"

    # Verify no raw slashes caused nested directories under queue/
    queue_dir = state_dir / "queue"
    assert not (queue_dir / "owner-org").exists()
    assert not (queue_dir / "nested").exists()

    # list_identities decodes back to exact identity names
    listed = q.list_identities()
    assert listed == sorted(identities)

    # Pop and remove work with special character identities
    popped = q.pop("owner-org/worker-team")
    assert popped["id"] == "msg-owner-org/worker-team"
    assert q.count("owner-org/worker-team") == 0


def test_registry_corrupt_file_backup(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    reg_file = state_dir / "registry.json"

    # Write corrupt JSON
    reg_file.write_text("{broken json: 123", encoding="utf-8")

    reg = Registry(state_dir)
    # Corrupt file reset to empty records
    assert reg.list_agents() == []

    # Corrupt backup file created
    corrupt_files = list(state_dir.glob("registry.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "{broken json: 123"
    # Original corrupt file is replaced / removed
    assert not reg_file.exists()

    # Saving new record creates new valid registry.json and keeps backup
    rec = AgentRecord(
        agent_id="ag_valid",
        identity="team-1",
        agora_session="agora:01JVALID",
        agent_kind="opencode",
        herdr_tab_id="w1:t1",
        herdr_pane_id="w1:p1",
        agent_name="valid-worker",
        started_at="2026-10-11T12:00:00Z",
        status="running",
        log_path=str(state_dir / "log"),
        updated_at="2026-10-11T12:00:00Z",
    )
    reg.register(rec)
    assert reg_file.is_file()
    assert len(list(state_dir.glob("registry.json.corrupt-*"))) == 1


def test_queue_corrupt_file_backup(tmp_path):
    state_dir = tmp_path / "state"
    queue_dir = state_dir / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    ident = "owner/team"
    q = MessageQueue(state_dir)
    qfile = q._queue_file(ident)

    # Write corrupt JSON
    qfile.write_text("invalid json content", encoding="utf-8")

    assert q.peek(ident) == []
    assert q.count(ident) == 0

    # Backup file exists in queue_dir
    corrupt_files = list(queue_dir.glob(f"{qfile.name}.corrupt-*"))
    assert len(corrupt_files) == 1
    assert corrupt_files[0].read_text(encoding="utf-8") == "invalid json content"

    # Subsequent enqueue creates clean queue file and keeps backup
    assert q.enqueue(ident, {"id": "m1", "urgency": 5, "content": "fresh"}) is True
    assert q.count(ident) == 1
    assert len(list(queue_dir.glob(f"{qfile.name}.corrupt-*"))) == 1


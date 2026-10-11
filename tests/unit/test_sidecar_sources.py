"""FileSubscriptionSource and LLMGatewaySource (change sidecar-v0, task 2.5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import time
from datetime import datetime, timezone

import pytest

import _sidecar_fakes as F
from sidecar.protocols import SidecarError, SubscriptionError, UnavailableError, iso
from sidecar.sources import FileSubscriptionSource, LLMGatewaySource, connect_llmgateway

ROW = {
    "schema": 1,
    "id": "claude-personal",
    "provider": "anthropic",
    "kind": "claude-subscription",
    "agents": ["claude-code"],
    "priority": 10,
    "models": {"allow": ["*"], "default": None},
    "endpoint": None,
    "key": "file-secret",
    "cooldown": {"5h": None, "1w": None},
    "available": True,
}


def write_subs(tmp_path, data):
    path = tmp_path / "subscriptions.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_file_source_list_strips_key():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = write_subs(Path(tmp), [ROW])
        rows = FileSubscriptionSource(path).list()
    assert [row.id for row in rows] == ["claude-personal"]
    assert rows[0].key is None
    assert rows[0].available is True
    assert "file-secret" not in repr(rows[0])


def test_file_source_get_returns_key_and_checks_agent(tmp_path):
    source = FileSubscriptionSource(write_subs(tmp_path, [ROW]))
    sub = source.get("claude-personal", "claude-code")
    assert sub.key == "file-secret"
    with pytest.raises(SubscriptionError):
        source.get("claude-personal", "opencode")
    with pytest.raises(SubscriptionError):
        source.get("nope", "claude-code")


def test_file_source_cooldown_marks_the_row_unavailable(tmp_path):
    source = FileSubscriptionSource(write_subs(tmp_path, [ROW]))
    until = time.time() + 3600
    source.cooldown("claude-personal", "5h", until)
    rows = source.list()
    assert rows[0].available is False
    assert rows[0].cooldown["5h"] == until
    with pytest.raises(SubscriptionError):
        source.cooldown("claude-personal", "3h", until)


def test_file_source_accepts_the_wrapped_shape_and_iso_cooldowns(tmp_path):
    row = dict(ROW, available=False, cooldown={"1w": {"until": "2030-01-01T00:00:00Z"}})
    source = FileSubscriptionSource(write_subs(tmp_path, {"subscriptions": [row]}))
    rows = source.list()
    assert rows[0].available is False
    assert rows[0].cooldown["1w"] == datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()


def test_file_source_bad_shapes_raise(tmp_path):
    path = write_subs(tmp_path, {"nope": 1})
    with pytest.raises(SidecarError):
        FileSubscriptionSource(path).list()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SidecarError):
        FileSubscriptionSource(broken).list()
    with pytest.raises(SidecarError):
        FileSubscriptionSource(tmp_path / "missing.json").list()


def test_llmgateway_source_translates_the_three_ops():
    row_no_key = {k: v for k, v in ROW.items() if k != "key"}
    transport = F.FakeTransport(
        {
            "list": {"subscriptions": [row_no_key]},
            "get": dict(ROW, key="gw-secret"),
            "cooldown": {"ok": True},
        }
    )
    source = LLMGatewaySource(transport, endpoint="http://127.0.0.1:8480")

    rows = source.list()
    assert rows[0].key is None
    assert rows[0].id == "claude-personal"

    sub = source.get("claude-personal", "claude-code")
    assert sub.key == "gw-secret"

    source.cooldown("claude-personal", "5h", 5000.0)

    assert transport.calls == [
        ("list", {}),
        ("get", {"id": "claude-personal", "agent": "claude-code"}),
        ("cooldown", {"id": "claude-personal", "window": "5h", "resets_at": iso(5000.0)}),
    ]


def test_llmgateway_get_refuses_an_agent_the_row_disallows():
    transport = F.FakeTransport({"get": dict(ROW, key="gw-secret")})
    source = LLMGatewaySource(transport)
    with pytest.raises(SubscriptionError):
        source.get("claude-personal", "opencode")


def test_llmgateway_connect_is_still_a_todo():
    with pytest.raises(UnavailableError) as excinfo:
        connect_llmgateway(None)
    assert "lgw connect" in str(excinfo.value)

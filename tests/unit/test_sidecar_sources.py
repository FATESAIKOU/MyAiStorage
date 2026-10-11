"""FileSubscriptionSource and LLMGatewaySource (change sidecar-v0, task 2.5).

Both answer values / SourceError / Unreachable - never exceptions (D3).  The
LLMGateway side runs over injected lgw.connect / lgw.client doubles; the real
`lgw` import only happens when the gateway extra is installed.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json

import _sidecar_fakes as F
from sidecar.protocols import SourceError, Unreachable
from sidecar.sources import FileSubscriptionSource, LLMGatewaySource

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
NO_KEY_ROW = {k: v for k, v in ROW.items() if k != "key"}


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- FileSubscriptionSource ---------------------------------------------------


def test_file_source_list_strips_key(tmp_path):
    source = FileSubscriptionSource(write_json(tmp_path / "subs.json", [ROW]))
    rows = source.list()
    assert [row.id for row in rows] == ["claude-personal"]
    assert rows[0].key is None
    assert rows[0].available is True
    assert "file-secret" not in repr(rows[0])


def test_file_source_get_returns_key_and_checks_agent(tmp_path):
    source = FileSubscriptionSource(write_json(tmp_path / "subs.json", [ROW]))
    sub = source.get("claude-personal", "claude-code")
    assert sub.key == "file-secret"
    assert source.get("claude-personal", "opencode") == SourceError("agent_not_allowed")
    assert source.get("nope", "claude-code") == SourceError("not_found")


def test_file_source_cooldown_marks_the_row_unavailable(tmp_path):
    source = FileSubscriptionSource(write_json(tmp_path / "subs.json", [ROW]))
    until = "2030-01-01T00:00:00Z"
    assert source.cooldown("claude-personal", "5h", until) is None
    rows = source.list()
    assert rows[0].available is False
    assert rows[0].cooldown["5h"] == until
    assert source.cooldown("claude-personal", "3h", until) == SourceError("invalid_window")
    assert source.cooldown("claude-personal", "5h", "not a time") == SourceError(
        "invalid_resets_at"
    )


def test_file_source_accepts_the_wrapped_shape_and_iso_cooldowns(tmp_path):
    row = dict(ROW, available=False, cooldown={"1w": {"until": "2030-01-01T00:00:00Z"}})
    source = FileSubscriptionSource(write_json(tmp_path / "subs.json", {"subscriptions": [row]}))
    rows = source.list()
    assert rows[0].available is False
    assert rows[0].cooldown["1w"] == "2030-01-01T00:00:00Z"


def test_file_source_bad_files_are_unreachable(tmp_path):
    missing = FileSubscriptionSource(tmp_path / "missing.json").list()
    assert isinstance(missing, Unreachable)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert isinstance(FileSubscriptionSource(broken).list(), Unreachable)
    wrong = FileSubscriptionSource(write_json(tmp_path / "wrong.json", {"nope": 1})).list()
    assert isinstance(wrong, Unreachable)


def test_file_source_refuses_a_newer_schema(tmp_path):
    row = dict(ROW, schema=2)
    source = FileSubscriptionSource(write_json(tmp_path / "subs.json", [row]))
    result = source.list()
    assert isinstance(result, Unreachable)
    assert "schema" in (result.reason or "")


# --- LLMGatewaySource (injected doubles) --------------------------------------


def _lgw_source(tmp_path, client, connect=None, *, name="connect.json"):
    connect_file = tmp_path / name
    if not connect_file.exists():
        write_json(connect_file, {"schema": 1, "base_url": "fake://a"})
    factory = F.FakeClientFactory(client)
    return (
        LLMGatewaySource(connect_file, connect=connect or F.FakeConnect(), client_factory=factory),
        factory,
    )


def test_llmgateway_translates_the_three_ops(tmp_path):
    client = F.FakeGatewayClient(rows=[NO_KEY_ROW], get_row=dict(ROW, key="gw-secret"))
    source, factory = _lgw_source(tmp_path, client)

    rows = source.list()
    assert rows[0].key is None and rows[0].id == "claude-personal"
    sub = source.get("claude-personal", "claude-code")
    assert sub.key == "gw-secret"
    assert source.cooldown("claude-personal", "5h", "2030-01-01T00:00:00Z") is None

    assert client.calls == [
        ("list", {}),
        ("get", {"id": "claude-personal", "agent": "claude-code"}),
        ("cooldown", {"id": "claude-personal", "window": "5h", "resets_at": "2030-01-01T00:00:00Z"}),
    ]
    assert factory.base_urls == ["fake://a"] * 3  # one connection per direct call


def test_llmgateway_one_operation_uses_one_connection(tmp_path):
    client = F.FakeGatewayClient(rows=[NO_KEY_ROW], get_row=dict(ROW))
    connect = F.FakeConnect()
    source, _factory = _lgw_source(tmp_path, client, connect)

    with source.operation() as op:
        op.list()
        op.get("claude-personal", "claude-code")
        op.cooldown("claude-personal", "5h", None)

    assert connect.closed == 1  # one logical operation, one connection (S1)
    assert len(client.calls) == 3


def test_llmgateway_rereads_the_connect_file_every_operation(tmp_path):
    client = F.FakeGatewayClient(rows=[NO_KEY_ROW])
    connect = F.FakeConnect()
    source, factory = _lgw_source(tmp_path, client, connect)
    path = source.connect_file

    source.list()
    # The external world rotates the file atomically while sidecar runs (D7).
    tmp = path.with_suffix(".tmp")
    write_json(tmp, {"schema": 1, "base_url": "fake://b"})
    tmp.replace(path)
    source.list()

    assert len(connect.configs) == 2  # re-read on every operation, never cached
    assert factory.base_urls == ["fake://a", "fake://b"]


def test_llmgateway_bad_connect_file_is_unreachable(tmp_path):
    client = F.FakeGatewayClient(rows=[NO_KEY_ROW])
    connect_file = write_json(tmp_path / "connect.json", {"schema": 1})
    factory = F.FakeClientFactory(client)
    source = LLMGatewaySource(
        connect_file,
        connect=F.FakeConnect(bad_paths=[connect_file]),
        client_factory=factory,
    )
    for result in (source.list(), source.get("x", "claude-code"), source.cooldown("x", "5h", None)):
        assert isinstance(result, Unreachable)
    assert client.calls == []  # nothing was asked over the wire
    missing = LLMGatewaySource(tmp_path / "missing.json", connect=F.FakeConnect(),
                               client_factory=factory)
    assert isinstance(missing.list(), Unreachable)


def test_llmgateway_client_states_map_to_results(tmp_path):
    client = F.FakeGatewayClient(unreachable=True)
    source, _factory = _lgw_source(tmp_path, client)
    assert isinstance(source.list(), Unreachable)
    assert isinstance(source.get("x", "claude-code"), Unreachable)
    assert isinstance(source.cooldown("x", "5h", None), Unreachable)

    client = F.FakeGatewayClient(rows=[NO_KEY_ROW], get_row=dict(ROW), error_code="not_deployed")
    source, _factory = _lgw_source(tmp_path, client)
    assert source.list() == SourceError("not_deployed", "fake refusal")
    assert source.get("claude-personal", "claude-code").code == "not_deployed"
    assert source.cooldown("claude-personal", "5h", None).code == "not_deployed"


def test_llmgateway_row_that_disallows_the_agent_is_a_source_error(tmp_path):
    client = F.FakeGatewayClient(get_row=dict(ROW, key="gw-secret"))
    source, _factory = _lgw_source(tmp_path, client)
    assert source.get("claude-personal", "opencode") == SourceError("agent_not_allowed")


def test_llmgateway_without_the_extra_says_to_install_it(tmp_path):
    """In this test environment lgw is not installed (it is the gateway extra)."""
    connect_file = write_json(tmp_path / "connect.json", {"schema": 1})
    source = LLMGatewaySource(connect_file)  # no injected doubles
    result = source.list()
    assert isinstance(result, Unreachable)
    assert "gateway" in (result.reason or "")

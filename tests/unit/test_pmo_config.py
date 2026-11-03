"""Unit tests for the pmo teams settings schema (`#110` T-1, design §3.4)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from pmo.config import DEFAULT_WAKE_TEXT, Team, TeamsConfig, parse
from pmo.errors import SchemaError


def _config(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "version": 1,
        "pmo_identity": "mypmo-pm-m1",
        "secretary_identity": "myaistorage-secretary-m1",
        "dispatch_channel": "dispatch-pmo",
        "teams": [
            {
                "key": "myaistorage-m1",
                "pm": "myaistorage-pm-m1",
                "channel": "team-myaistorage-m1",
                "sidecar": "http://127.0.0.1:8471",
                "tui": True,
                "life": False,
                "retired": False,
            },
            {
                "key": "mylinuxpool-w1",
                "pm": "mylinuxpool-pm-w1",
                "channel": "team-mylinuxpool-w1",
                "sidecar": "worker",
                "tui": False,
                "life": True,
                "retired": False,
                "extras": {"anything": 1},  # unknown field: preserved
            },
        ],
        "unknown_top": {"kept": [1, 2]},
    }
    data.update(overrides)
    return data


def test_a_good_settings_file_parses_with_the_defaults():
    config = parse(_config())
    assert config.pmo_identity == "mypmo-pm-m1"
    assert config.secretary_identity == "myaistorage-secretary-m1"
    assert config.dispatch_channel == "dispatch-pmo"
    # pmo_sidecar absent → the PMO is not in a sidecar (design §3.4).
    assert config.pmo_sidecar is None
    # wake_text absent → the fixed default sentence (design §3.4).
    assert config.wake_text == DEFAULT_WAKE_TEXT


def test_two_teams_come_out_as_two_records():
    config = parse(_config())
    assert [team.key for team in config.teams] == ["myaistorage-m1", "mylinuxpool-w1"]
    first, second = config.teams
    assert (first.pm, first.channel, first.sidecar) == (
        "myaistorage-pm-m1",
        "team-myaistorage-m1",
        "http://127.0.0.1:8471",
    )
    assert (first.tui, first.life, first.retired) == (True, False, False)
    assert (second.sidecar, second.tui, second.life) == ("worker", False, True)
    assert config.team("mylinuxpool-w1") is second
    assert config.team("nowhere") is None


def test_unknown_fields_are_kept_on_the_loaded_record():
    config = parse(_config())
    assert config.extra == {"unknown_top": {"kept": [1, 2]}}
    second = config.teams[1]
    assert second.extra == {"extras": {"anything": 1}}
    # The canonical form writes the default wake sentence the file left out, so
    # the round trip is compared against the file with that one field filled in.
    expected = _config()
    expected["wake_text"] = DEFAULT_WAKE_TEXT
    assert config.to_dict() == expected


def test_works_file_save_and_load_round_trips(tmp_path, monkeypatch):
    from pmo.config import load, save

    path = tmp_path / "cfg" / "teams.json"
    config = parse(_config(wake_text="自訂的句子", pmo_sidecar="http://sidecar.local:9000"))
    save(path, config)
    monkeypatch.setenv("PMO_SKILL_CONFIG", str(path))
    reloaded = load(path)
    assert reloaded == config


def test_a_missing_field_is_refused():
    for missing in ("version", "pmo_identity", "secretary_identity", "dispatch_channel", "teams"):
        data = _config()
        del data[missing]
        with pytest.raises(SchemaError) as caught:
            parse(data)
        assert missing in str(caught.value)


def test_a_version_that_is_not_the_integer_one_is_refused():
    for bad in (2, "1", True, None, 1.0):
        with pytest.raises(SchemaError) as caught:
            parse(_config(version=bad))
        assert "version" in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"pmo_identity": 3}, "must be a string"),
        ({"pmo_identity": "NotLower-pm"}, "not a valid AI name"),
        ({"secretary_identity": "myaistorage-secretary-1"}, "not a valid AI name"),
        ({"dispatch_channel": "Dispatch-Pmo"}, "slug"),
        ({"dispatch_channel": ""}, "slug"),
        ({"dispatch_channel": "team-myaistorage-m1"}, "also a team channel"),
        ({"pmo_sidecar": "worker"}, "neither"),
        ({"pmo_sidecar": "http://"}, "neither"),
        ({"pmo_sidecar": 17}, "must be a string"),
        ({"wake_text": ""}, "empty"),
        ({"teams": "nope"}, "must be a list"),
    ],
)
def test_wrong_top_level_fields_are_refused(changes, expect_in_message):
    with pytest.raises(SchemaError) as caught:
        parse(_config(**changes))
    assert expect_in_message in str(caught.value)


@pytest.mark.parametrize(
    ["changes", "expect_in_message"],
    [
        ({"pm": 3}, "must be a string"),
        ({"pm": "unknownrole-m1"}, "not a valid AI name"),
        ({"channel": "team-myaistorage-m2"}, "must be 'team-myaistorage-m1'"),
        ({"channel": "Teams/me"}, "slug"),
        ({"sidecar": "yes"}, "neither an http(s) address nor 'worker'"),
        ({"sidecar": ""}, "neither an http(s) address nor 'worker'"),
        ({"sidecar": 9}, "must be a string"),
        ({"tui": "yes"}, "boolean"),
        ({"life": 1}, "boolean"),
        ({"retired": None}, "boolean"),
        ({"model": "glm"}, "go together"),
        ({"agent": "agy"}, "must be one of"),
        ({"agent": ""}, "must be one of"),
        ({"agent": 7}, "must be a string"),
        ({"agent": "opencode"}, "tui"),  # the first team is tui: true
        ({"workdir": "relative/path"}, "absolute path"),
        ({"workdir": ""}, "absolute path"),
        ({"agent_args": "nope"}, "array of strings"),
        ({"agent_args": ["--effort", 3]}, "a string in every element"),
        ({"key": "myaistorage"}, "'<project>-<team code>'"),
        ({"key": "MyProject-m1"}, "slug"),
        ({"key": "mypmo-impl-m1"}, "do not agree"),
    ],
)
def test_wrong_team_fields_are_refused(changes, expect_in_message):
    data = _config()
    team = data["teams"][0]
    assert isinstance(team, dict)
    team.update(changes)
    # A private sidecar field must stay an address even when showing a worker team.
    if changes.get("pm") == "unknownrole-m1":
        team["channel"] = "team-nowhere-m1"
    with pytest.raises(SchemaError) as caught:
        parse(data)
    assert expect_in_message in str(caught.value)


def test_a_channel_prefix_that_lyingly_matches_the_key_is_refused():
    data = _config()
    team = data["teams"][0]
    assert isinstance(team, dict)
    team["channel"] = "team-myaistorage-m2"
    with pytest.raises(SchemaError) as caught:
        parse(data)
    assert "must be 'team-myaistorage-m1'" in str(caught.value)


def test_a_team_reads_the_agent_workdir_and_agent_args_fields():
    """#356 裁定 §2：teams 設定新增三個正式欄位（不再是 unknown extra）。"""
    data = _config()
    team = data["teams"][1]
    assert isinstance(team, dict)
    team.update(
        {
            "agent": "opencode",
            "workdir": "/home/worker/work/demo/wt/7",
            "agent_args": ["--effort", "high"],
        }
    )
    config = parse(data)
    second = config.teams[1]
    assert second.agent == "opencode"
    assert second.workdir == "/home/worker/work/demo/wt/7"
    assert second.agent_args == ("--effort", "high")
    assert second.extra == {"extras": {"anything": 1}}  # 其他未知欄位照樣保留
    assert config.to_dict()["teams"][1]["agent"] == "opencode"
    assert config.to_dict()["teams"][1]["workdir"] == "/home/worker/work/demo/wt/7"
    assert config.to_dict()["teams"][1]["agent_args"] == ["--effort", "high"]


def test_the_agent_defaults_to_claude_code():
    """#356 裁定 §2：`agent` 選填，預設 `claude-code`（現行工具的寫法）。"""
    first = parse(_config()).teams[0]
    assert first.agent == "claude-code"
    # 預設值不寫回：原樣 round trip（同一個意思）。
    assert "agent" not in parse(_config()).to_dict()["teams"][0]
    assert "workdir" not in parse(_config()).to_dict()["teams"][0]
    assert "agent_args" not in parse(_config()).to_dict()["teams"][0]


def test_a_provider_may_stand_alone_but_a_model_may_not():
    """#356 裁定 §2：放寬成「model 需要 provider，provider 可以單獨給」。"""
    data = _config()
    team = data["teams"][1]
    assert isinstance(team, dict)
    team["provider"] = "ollama-cloud"
    second = parse(data).teams[1]
    assert (second.provider, second.model) == ("ollama-cloud", None)
    assert second.to_dict()["provider"] == "ollama-cloud"
    assert "model" not in second.to_dict()


def test_an_interactive_team_may_keep_the_claude_code_agent():
    """`tui: true` 配 `agent: claude-code`（明寫或預設）都合法。"""
    data = _config()
    team = data["teams"][0]
    assert isinstance(team, dict)
    team["agent"] = "claude-code"
    assert parse(data).teams[0].agent == "claude-code"


def test_repeated_keys_names_and_channels_are_refused():
    data = _config()
    same = dict(data["teams"][0])
    assert isinstance(same, dict)
    data["teams"].append(same)
    with pytest.raises(SchemaError) as caught:
        parse(data)
    assert "twice" in str(caught.value)


def test_the_default_wake_text_is_the_design_sentence():
    assert DEFAULT_WAKE_TEXT == "定時叫醒：請跑 pmo tick"


def test_a_team_record_round_trips_through_to_dict():
    team = Team(
        key="myaistorage-m1",
        pm="myaistorage-pm-m1",
        channel="team-myaistorage-m1",
        sidecar="worker",
        tui=False,
        life=False,
        retired=True,
        provider="opencode",
        model="glm",
        extra={"note": "kept"},
    )
    assert team.to_dict() == {
        "key": "myaistorage-m1",
        "pm": "myaistorage-pm-m1",
        "channel": "team-myaistorage-m1",
        "sidecar": "worker",
        "tui": False,
        "life": False,
        "retired": True,
        "provider": "opencode",
        "model": "glm",
        "note": "kept",
    }
    # A record with no provider/model pair writes neither of them.
    bare = Team(
        key="myaistorage-m1",
        pm="myaistorage-pm-m1",
        channel="team-myaistorage-m1",
        sidecar="worker",
        tui=False,
        life=False,
        retired=False,
    )
    assert "provider" not in bare.to_dict() and "model" not in bare.to_dict()

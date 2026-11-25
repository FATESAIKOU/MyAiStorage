"""Unit tests for the registry rules and the rollover action (design 542 §1.2, C-7, C-8, C-25).

The rules are pure functions over the comment thread; the dedupe (C-25) runs
through the rebuild's injectable registry fake. No network, no real GitHub.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
import subprocess
import sys
from typing import Any

from _angareion_fakes import FakeRegistryClient, backends_json
from angareion.registry import (
    Action,
    InvalidAction,
    parse_comment,
    register_comment,
    rollover_comment,
    unregister_comment,
)
from angareion.registry_rebuild import BACKENDS_PATH, CHANNELS_PATH, Rebuilder
from angareion.registry_rules import rebuild_channels, render_channels, report_text

BACKEND = "aicompany"
OTHER = "other"
BACKEND_NAMES = {BACKEND, OTHER}


def _c(body: str, *, id_: int, created: str | None = None, assoc: str = "OWNER") -> dict[str, Any]:
    """One registry comment in the shape the rebuild reads."""
    return {
        "id": id_,
        "body": body,
        "author_association": assoc,
        "created_at": created or f"2026-11-25T10:00:{id_:02d}Z",
    }


def _rebuild(comments: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    return rebuild_channels(comments, BACKEND_NAMES)


# -- C-7: the rollover comment and parse_comment --------------------------


def test_c7_rollover_comment_round_trips_through_parse_comment():
    text = rollover_comment("team-x-w2", BACKEND, 41, 28)
    assert list(json.loads(text)) == ["schema", "action", "channel", "backend", "issue", "from"]
    assert parse_comment(text) == Action(
        action="rollover", channel="team-x-w2", backend=BACKEND, issue=41, from_issue=28
    )


def test_c7_rollover_without_from_is_missing_fields():
    text = json.dumps({"schema": 1, "action": "rollover", "channel": "c", "backend": BACKEND, "issue": 41})
    assert parse_comment(text) == InvalidAction("missing fields: from")


def test_c7_register_and_unregister_reject_from_as_unknown_fields():
    for action in ("register", "unregister"):
        text = json.dumps(
            {"schema": 1, "action": action, "channel": "c", "backend": BACKEND, "issue": 41, "from": 28}
        )
        assert parse_comment(text) == InvalidAction("unknown fields: from")


def test_c7_from_must_be_a_positive_integer_and_differ_from_issue():
    base = {"schema": 1, "action": "rollover", "channel": "c", "backend": BACKEND, "issue": 41}
    for bad in (0, -3, True, "28", None):
        result = parse_comment(json.dumps({**base, "from": bad}))
        assert isinstance(result, InvalidAction), bad
        assert "from issue number must be a positive integer" in result.reason
    same = parse_comment(json.dumps({**base, "from": 41}))
    assert same == InvalidAction("the from issue number must differ from the issue number")


def test_c7_register_and_unregister_comments_are_unchanged():
    assert parse_comment(register_comment("general", BACKEND, 10)) == Action(
        action="register", channel="general", backend=BACKEND, issue=10
    )
    assert parse_comment(unregister_comment("general", BACKEND, 10)) == Action(
        action="unregister", channel="general", backend=BACKEND, issue=10
    )


# -- C-8: the §1.2 table, one test per row ---------------------------------


def test_c8_rollover_of_an_unregistered_channel_is_reported_and_skipped():
    channels, reports = _rebuild([_c(rollover_comment("nope", BACKEND, 41, 28), id_=1)])
    assert channels == {}
    assert len(reports) == 1 and "is not registered" in reports[0]


def test_c8_rollover_to_a_different_backend_is_reported_and_skipped():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", OTHER, 41, 28), id_=2),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {"general": {"backend": BACKEND, "issue": 28}}
    assert len(reports) == 1 and "is on backend 'aicompany', not 'other'" in reports[0]


def test_c8_rollover_to_the_current_page_is_a_silent_noop():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 28, 99), id_=2),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {"general": {"backend": BACKEND, "issue": 28}}
    assert reports == []


def test_c8_rollover_to_an_already_retired_page_is_a_silent_noop():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2, created="2026-11-25T10:01:00Z"),
        _c(rollover_comment("general", BACKEND, 28, 41), id_=3, created="2026-11-25T10:02:00Z"),
    ]
    channels, reports = _rebuild(comments)
    assert channels["general"]["issue"] == 41
    assert channels["general"]["retired"] == [{"issue": 28, "until": "2026-11-25T10:01:00Z"}]
    assert reports == []


def test_c8_rollover_from_the_current_page_retires_it():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2, created="2026-11-25T10:05:00Z"),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {
        "general": {
            "backend": BACKEND,
            "issue": 41,
            "retired": [{"issue": 28, "until": "2026-11-25T10:05:00Z"}],
        }
    }
    assert reports == []


def test_c8_rollover_from_a_retired_page_becomes_a_side_page():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2, created="2026-11-25T10:01:00Z"),
        _c(rollover_comment("general", BACKEND, 42, 28), id_=3, created="2026-11-25T10:01:05Z"),
    ]
    channels, reports = _rebuild(comments)
    assert channels["general"]["issue"] == 41
    assert channels["general"]["retired"] == [
        {"issue": 28, "until": "2026-11-25T10:01:00Z"},
        {"issue": 42, "until": "2026-11-25T10:01:05Z", "side": True},
    ]
    assert reports == []


def test_c8_two_rollovers_from_the_same_page_race_to_one_current_and_one_side():
    """Both rollovers were written before either was seen; the (created_at, id) order decides."""
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 42, 28), id_=3, created="2026-11-25T10:02:00Z"),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2, created="2026-11-25T10:01:00Z"),
    ]
    channels, _ = _rebuild(comments)
    # id 2 (created earlier) is applied first: 41 becomes the current page.
    assert channels["general"]["issue"] == 41
    assert [page["issue"] for page in channels["general"]["retired"]] == [28, 42]
    assert channels["general"]["retired"][1]["side"] is True


def test_c8_rollover_from_an_unknown_page_is_reported_and_skipped():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 99), id_=2),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {"general": {"backend": BACKEND, "issue": 28}}
    assert len(reports) == 1 and "is neither the current page nor a retired page" in reports[0]


def test_c8_rollover_by_an_unauthorized_author_is_ignored_without_a_report():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2, assoc="NONE"),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {"general": {"backend": BACKEND, "issue": 28}}
    assert reports == []


def test_c8_register_compares_the_current_page_only():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2),
        _c(register_comment("general", BACKEND, 41), id_=3),  # the current page: same entity, no-op
        _c(register_comment("general", BACKEND, 28), id_=4),  # a retired page: a different entity
    ]
    channels, reports = _rebuild(comments)
    assert channels["general"]["issue"] == 41
    assert len(reports) == 1 and "already registered to a different entity" in reports[0]


def test_c8_unregister_of_the_current_page_removes_the_whole_mapping():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2),
        _c(unregister_comment("general", BACKEND, 41), id_=3),
    ]
    channels, reports = _rebuild(comments)
    assert channels == {}
    assert reports == []


def test_c8_unregister_of_a_retired_page_is_a_reported_noop():
    comments = [
        _c(register_comment("general", BACKEND, 28), id_=1),
        _c(rollover_comment("general", BACKEND, 41, 28), id_=2),
        _c(unregister_comment("general", BACKEND, 28), id_=3),
    ]
    channels, reports = _rebuild(comments)
    assert channels["general"]["issue"] == 41
    assert channels["general"]["retired"] == [{"issue": 28, "until": "2026-11-25T10:00:02Z"}]
    assert len(reports) == 1 and "does not point at that entity" in reports[0]


def test_c8_render_omits_retired_for_channels_that_never_moved():
    text = render_channels({"general": {"backend": BACKEND, "issue": 28}})
    assert "retired" not in text


def test_c8_render_writes_retired_pages_and_the_side_flag():
    channels = {
        "general": {
            "backend": BACKEND,
            "issue": 41,
            "retired": [
                {"issue": 28, "until": "2026-11-25T10:01:00Z"},
                {"issue": 42, "until": "2026-11-25T10:01:05Z", "side": True},
            ],
        }
    }
    assert json.loads(render_channels(channels))["channels"] == channels


# C-8 golden: the rebuild output for a registry without rollover is byte-identical to
# the output before PR-2b (captured from the pre-change rebuild on this same thread).
GOLDEN_COMMENTS = [
    _c(register_comment("team-a-w1", BACKEND, 10), id_=1, created="2026-10-10T12:00:01Z"),
    _c(register_comment("team-b-w1", BACKEND, 11), id_=2, created="2026-10-10T12:00:02Z"),
    _c(register_comment("team-a-w1", BACKEND, 10), id_=3, created="2026-10-10T12:00:03Z"),
    _c(register_comment("team-a-w1", BACKEND, 12), id_=4, created="2026-10-10T12:00:04Z"),
    _c(unregister_comment("team-b-w1", BACKEND, 99), id_=5, created="2026-10-10T12:00:05Z"),
    _c(unregister_comment("team-b-w1", BACKEND, 11), id_=6, created="2026-10-10T12:00:06Z"),
    _c(register_comment("team-c", "nope", 13), id_=7, created="2026-10-10T12:00:07Z"),
    _c("hello there", id_=8, created="2026-10-10T12:00:08Z"),
    _c("{bad json", id_=9, created="2026-10-10T12:00:09Z", assoc="MEMBER"),
    _c(register_comment("team-d", BACKEND, 14), id_=10, created="2026-10-10T12:00:10Z", assoc="NONE"),
    _c(register_comment("team-e", BACKEND, 15), id_=11, created="2026-10-10T12:00:11Z"),
    _c(register_comment("team-f", BACKEND, 16), id_=12, created="2026-10-10T12:00:12Z"),
]

GOLDEN_CHANNELS_TEXT = (
    "{\n"
    '  "channels": {\n'
    '    "team-a-w1": {\n'
    '      "backend": "aicompany",\n'
    '      "issue": 10\n'
    "    },\n"
    '    "team-e": {\n'
    '      "backend": "aicompany",\n'
    '      "issue": 15\n'
    "    },\n"
    '    "team-f": {\n'
    '      "backend": "aicompany",\n'
    '      "issue": 16\n'
    "    }\n"
    "  },\n"
    '  "version": 1\n'
    "}\n"
)


def test_c8_golden_rebuild_without_rollover_is_byte_identical():
    channels, reports = _rebuild(GOLDEN_COMMENTS)
    assert render_channels(channels) == GOLDEN_CHANNELS_TEXT
    assert reports == [
        report_text(
            "channel 'team-a-w1' is already registered to a different entity (first come, first served)",
            GOLDEN_COMMENTS[3]["body"],
        ),
        report_text(
            "channel 'team-b-w1' does not point at that entity (nothing to remove)",
            GOLDEN_COMMENTS[4]["body"],
        ),
        report_text("the backend 'nope' is not configured", GOLDEN_COMMENTS[6]["body"]),
        report_text(
            'the comment starts with \'{\' but is not valid JSON',
            "{bad json",
        ),
    ]


def test_c8_the_rules_module_imports_only_the_standard_library_and_registry():
    repo = _p.Path(__file__).resolve().parent.parent.parent
    code = (
        "import sys; import angareion.registry_rules; "
        "loaded = sorted(m for m in sys.modules if m.startswith('angareion.')); "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=repo,
        env={"PYTHONPATH": "src", "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.strip().split(","))
    assert "angareion.registry_rules" in loaded
    assert not loaded & {"angareion.backend", "angareion.channel_config", "angareion.registry_rebuild"}


# -- C-25: the rebuild posts a quoted report once ---------------------------


def _fake_registry(comments: list[dict[str, Any]], **kwargs: Any) -> FakeRegistryClient:
    files = {BACKENDS_PATH: backends_json({BACKEND: ("github", "owner/repo")})}
    return FakeRegistryClient(comments=comments, files=files, **kwargs)


def test_c25_the_same_quoted_report_is_posted_once_across_two_runs():
    api = _fake_registry([_c("{bad json", id_=1)])
    rebuilder = Rebuilder(api, sleeper=lambda _seconds: None)

    first = rebuilder.run()
    second = rebuilder.run()

    assert len(first.posted) == 1
    assert second.posted == []
    assert second.reports == first.reports
    assert api.posted == first.posted


def test_c25_different_quoted_reports_are_each_posted_once():
    api = _fake_registry([_c("{bad one", id_=1), _c("{bad two", id_=2)])
    rebuilder = Rebuilder(api, sleeper=lambda _seconds: None)

    first = rebuilder.run()
    second = rebuilder.run()

    assert len(first.posted) == 2
    assert len(set(first.posted)) == 2
    assert second.posted == []


def test_c25_a_fixed_text_report_is_posted_every_time_it_happens():
    api = _fake_registry([], stale_writes=10)  # every write attempt of both runs conflicts
    rebuilder = Rebuilder(api, sleeper=lambda _seconds: None)

    rebuilder.run()
    second = rebuilder.run()

    fixed = report_text("the repository file kept changing while writing; this run did not update it", "")
    assert api.posted.count(fixed) == 2
    assert second.posted == [fixed]
    assert second.reports == [fixed]

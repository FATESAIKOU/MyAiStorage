"""Unit tests for the registry rebuild script (section 3.5; design sections 9/10).

The rebuild is a pure function over the comment thread, plus a small runner
around the injectable registry API. No network, no real GitHub.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from typing import Any

import pytest

from _angareion_fakes import FakeRegistryClient, backends_json
from angareion.registry import parse_comment, register_comment, unregister_comment
from angareion.registry_rebuild import (
    BACKENDS_PATH,
    CHANNELS_PATH,
    CONFIG_VERSION,
    WRITE_ATTEMPTS,
    RebuildError,
    Rebuilder,
    load_backend_names,
    main as rebuild_main,
    rebuild_channels,
    render_channels,
)

BACKENDS = {"github-main": ("github", "owner/repo")}
BACKEND_NAMES = {"github-main"}


def _comment(body: str, association: str = "OWNER", user_type: str = "User") -> dict[str, Any]:
    return {"body": body, "author_association": association, "user": {"type": user_type}}


def _fake_registry(comments: list[dict[str, Any]] | None = None, **kwargs) -> FakeRegistryClient:
    files = {BACKENDS_PATH: backends_json(BACKENDS)}
    files.update(kwargs.pop("files", {}))
    return FakeRegistryClient(comments=comments, files=files, **kwargs)


def _channels(text: str) -> dict[str, Any]:
    return json.loads(text)["channels"]


# -- the pure rebuild --------------------------------------------------------


def test_register_adds_a_mapping():
    channels, reports = rebuild_channels(
        [_comment(register_comment("X", "github-main", 3))], BACKEND_NAMES
    )
    assert channels == {"X": {"backend": "github-main", "issue": 3}}
    assert reports == []


def test_register_then_unregister_removes_it():
    channels, reports = rebuild_channels(
        [
            _comment(register_comment("X", "github-main", 3)),
            _comment(unregister_comment("X", "github-main", 3)),
        ],
        BACKEND_NAMES,
    )
    assert channels == {}
    assert reports == []


def test_first_come_first_served_and_the_loser_is_reported():
    channels, reports = rebuild_channels(
        [
            _comment(register_comment("X", "github-main", 1)),
            _comment(register_comment("X", "github-main", 2)),
        ],
        BACKEND_NAMES,
    )
    assert channels == {"X": {"backend": "github-main", "issue": 1}}
    assert len(reports) == 1
    assert "already registered" in reports[0]
    assert parse_comment(reports[0]) is None  # a report is never a parseable action


def test_out_of_order_api_results_still_apply_first_come_first_served():
    """L2: the API order is transport order; (created_at, id) decides who was first."""
    early = {
        "body": register_comment("X", "github-main", 1),
        "author_association": "OWNER",
        "user": {"type": "User"},
        "created_at": "2026-10-10T12:00:01Z",
        "id": 101,
    }
    late = {
        "body": register_comment("X", "github-main", 2),
        "author_association": "OWNER",
        "user": {"type": "User"},
        "created_at": "2026-10-10T12:00:02Z",
        "id": 102,
    }
    channels, reports = rebuild_channels([late, early], BACKEND_NAMES)
    assert channels == {"X": {"backend": "github-main", "issue": 1}}
    assert len(reports) == 1
    assert "already registered" in reports[0]

    # Same timestamp: the smaller id was first, even when listed last.
    first_id = dict(early, created_at="2026-10-10T12:00:01Z", id=201)
    second_id = dict(late, created_at="2026-10-10T12:00:01Z", id=202)
    channels, _ = rebuild_channels([second_id, first_id], BACKEND_NAMES)
    assert channels == {"X": {"backend": "github-main", "issue": 1}}


def test_repeat_register_of_the_same_entity_is_a_noop_without_a_report():
    channels, reports = rebuild_channels(
        [
            _comment(register_comment("X", "github-main", 3)),
            _comment(register_comment("X", "github-main", 3)),
        ],
        BACKEND_NAMES,
    )
    assert channels == {"X": {"backend": "github-main", "issue": 3}}
    assert reports == []


def test_unregister_that_matches_nothing_is_reported():
    channels, reports = rebuild_channels(
        [
            _comment(register_comment("X", "github-main", 3)),
            _comment(unregister_comment("X", "github-main", 4)),  # different entity
            _comment(unregister_comment("Y", "github-main", 9)),  # never registered
        ],
        BACKEND_NAMES,
    )
    assert channels == {"X": {"backend": "github-main", "issue": 3}}
    assert len(reports) == 2
    assert all("nothing to remove" in report for report in reports)


def test_unknown_backend_is_reported_and_not_applied():
    channels, reports = rebuild_channels(
        [_comment(register_comment("X", "ghost", 3))], BACKEND_NAMES
    )
    assert channels == {}
    assert len(reports) == 1
    assert "not configured" in reports[0]


def test_invalid_action_attempts_are_reported_and_do_not_break_the_rest():
    channels, reports = rebuild_channels(
        [
            _comment(register_comment("X", "github-main", 3)),
            _comment('{"schema": 1, "action": "register", "channel": "小明", "backend": "github-main", "issue": 3}'),
            _comment('{"schema": 1,'),  # broken JSON
            _comment(register_comment("Y", "github-main", 4)),
        ],
        BACKEND_NAMES,
    )
    assert channels == {
        "X": {"backend": "github-main", "issue": 3},
        "Y": {"backend": "github-main", "issue": 4},
    }
    assert len(reports) == 2


def test_plain_comments_and_bot_reports_are_ignored_silently():
    channels, reports = rebuild_channels(
        [
            _comment("hello, just chatting"),
            _comment("Angareion registry: this comment could not be applied.\n> x\nReason: y", "NONE", "Bot"),
            _comment(register_comment("X", "github-main", 3)),
        ],
        BACKEND_NAMES,
    )
    assert channels == {"X": {"backend": "github-main", "issue": 3}}
    assert reports == []


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"])
def test_non_authorized_comments_are_ignored_without_a_report(association):
    channels, reports = rebuild_channels(
        [_comment(register_comment("X", "github-main", 3), association=association)],
        BACKEND_NAMES,
    )
    assert channels == {}
    assert reports == []  # not authorized: no report either (H1)


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_the_three_authorized_associations_count(association):
    channels, _ = rebuild_channels(
        [_comment(register_comment("X", "github-main", 3), association=association)],
        BACKEND_NAMES,
    )
    assert channels == {"X": {"backend": "github-main", "issue": 3}}


def test_pagination_keeps_early_registers():
    """150 comments: an early register must survive the later pages (review M4)."""
    comments = [_comment(register_comment("early", "github-main", 1))]
    comments += [_comment("chat %d" % index, "NONE") for index in range(120)]
    comments += [_comment(register_comment("late", "github-main", 2))]
    comments += [_comment("more chat %d" % index, "NONE") for index in range(29)]
    assert len(comments) == 151

    fake = _fake_registry()
    for comment in comments:
        fake.add_comment(
            comment["body"],
            author_association=comment["author_association"],
            user_type=comment["user"]["type"],
        )
    Rebuilder(fake).run()

    assert _channels(fake.files[CHANNELS_PATH]) == {
        "early": {"backend": "github-main", "issue": 1},
        "late": {"backend": "github-main", "issue": 2},
    }
    assert fake.list_calls[0] == 1 and 2 in fake.list_calls  # followed the pages


# -- the runner --------------------------------------------------------------


def test_runner_writes_the_file_and_posts_reports():
    fake = _fake_registry()
    fake.add_comment(register_comment("X", "github-main", 3))
    fake.add_comment(register_comment("X", "github-main", 4))  # conflict: reported

    summary = Rebuilder(fake).run()

    assert summary.committed is True
    assert summary.changed is True
    assert _channels(fake.files[CHANNELS_PATH]) == {"X": {"backend": "github-main", "issue": 3}}
    assert len(summary.reports) == 1
    assert fake.posted == summary.reports
    # The written text is the canonical format.
    assert fake.files[CHANNELS_PATH] == render_channels({"X": {"backend": "github-main", "issue": 3}})


def test_runner_does_not_commit_when_nothing_changed():
    text = render_channels({"X": {"backend": "github-main", "issue": 3}})
    fake = _fake_registry(files={BACKENDS_PATH: backends_json(BACKENDS), CHANNELS_PATH: text})
    fake.add_comment(register_comment("X", "github-main", 3))

    summary = Rebuilder(fake).run()

    assert summary.committed is False
    assert summary.changed is False
    assert fake.put_calls == []
    assert fake.files[CHANNELS_PATH] == text


def test_manual_rerun_is_safe_and_idempotent():
    fake = _fake_registry()
    fake.add_comment(register_comment("X", "github-main", 3))

    first = Rebuilder(fake).run()
    second = Rebuilder(fake).run()

    assert first.committed is True
    assert second.committed is False  # same result: no second commit
    assert fake.files[CHANNELS_PATH] == render_channels({"X": {"backend": "github-main", "issue": 3}})
    assert len(fake.put_calls) == 1


def test_a_cancelled_run_loses_nothing():
    """Run 1 is cancelled before it writes; the next run rebuilds the whole thread."""
    fake = _fake_registry()
    fake.add_comment(register_comment("A", "github-main", 1))
    fake.add_comment(register_comment("B", "github-main", 2))

    # Run 1: the write never lands (the run was cancelled mid-flight).
    fake.stale_writes = WRITE_ATTEMPTS
    first = Rebuilder(fake, sleeper=lambda seconds: None).run()
    assert first.committed is False
    assert CHANNELS_PATH not in fake.files

    # Run 2 (next comment or a manual re-run): everything is there.
    summary = Rebuilder(fake).run()
    assert summary.committed is True
    assert _channels(fake.files[CHANNELS_PATH]) == {
        "A": {"backend": "github-main", "issue": 1},
        "B": {"backend": "github-main", "issue": 2},
    }


def test_the_optimistic_lock_retries_then_succeeds():
    fake = _fake_registry(stale_writes=2)
    fake.add_comment(register_comment("X", "github-main", 3))

    summary = Rebuilder(fake, sleeper=lambda seconds: None).run()

    assert summary.committed is True
    assert len(fake.put_calls) == 3  # two stale rejections, then the write


def test_the_optimistic_lock_gives_up_and_reports_without_touching_main():
    fake = _fake_registry(stale_writes=WRITE_ATTEMPTS)
    fake.add_comment(register_comment("X", "github-main", 3))

    summary = Rebuilder(fake, sleeper=lambda seconds: None).run()

    assert summary.committed is False
    assert CHANNELS_PATH not in fake.files  # main is untouched
    assert any("kept changing" in report for report in summary.reports)
    assert summary.reports == fake.posted


def test_the_runner_fails_when_backends_json_is_missing_or_broken():
    fake = FakeRegistryClient()  # no backends.json at all
    with pytest.raises(RebuildError):
        Rebuilder(fake).run()

    fake = FakeRegistryClient(files={BACKENDS_PATH: "{broken"})
    with pytest.raises(RebuildError):
        Rebuilder(fake).run()


def test_load_backend_names_validates_the_file():
    assert load_backend_names(backends_json(BACKENDS)) == {"github-main"}
    with pytest.raises(RebuildError):
        load_backend_names("{broken")
    with pytest.raises(RebuildError):
        load_backend_names(json.dumps({"version": 2, "backends": {}}))
    with pytest.raises(RebuildError):
        load_backend_names(json.dumps({"version": CONFIG_VERSION, "backends": []}))


def test_the_entrypoint_reads_the_environment():
    fake = _fake_registry()
    fake.add_comment(register_comment("X", "github-main", 3))
    written: list[str] = []

    code = rebuild_main(
        env={
            "ANGAREION_REGISTRY_REPO": "owner/MyAiStorage",
            "ANGAREION_REGISTRY_ISSUE": "77",
            "GITHUB_TOKEN": "secret",
            "ANGAREION_TRIGGER_COMMENT_BODY": register_comment("X", "github-main", 3),
        },
        api=fake,
        write=written.append,
    )
    assert code == 0
    assert _channels(fake.files[CHANNELS_PATH]) == {"X": {"backend": "github-main", "issue": 3}}
    assert "committed" in written[-1]


def test_the_entrypoint_rejects_bad_environment():
    written: list[str] = []
    assert rebuild_main(env={"ANGAREION_REGISTRY_REPO": "nope"}, api=_fake_registry(), write=written.append) == 1
    assert "owner/repo" in written[-1]

    written = []
    assert rebuild_main(
        env={"ANGAREION_REGISTRY_REPO": "owner/MyAiStorage", "ANGAREION_REGISTRY_ISSUE": "abc"},
        api=_fake_registry(),
        write=written.append,
    ) == 1
    assert "issue number" in written[-1]


def test_the_entrypoint_surfaces_a_rebuild_failure():
    written: list[str] = []
    code = rebuild_main(
        env={"ANGAREION_REGISTRY_REPO": "owner/MyAiStorage", "ANGAREION_REGISTRY_ISSUE": "77"},
        api=FakeRegistryClient(),  # no backends.json
        write=written.append,
    )
    assert code == 1
    assert "failed" in written[-1]


def test_the_rebuild_module_needs_no_third_party_packages():
    """The workflow runs it on a bare runner: `python3 -S` must be enough."""
    import os
    import subprocess
    import sys

    repo = _p.Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import angareion.registry_rebuild; print('ok')"],
        cwd=repo,
        env={"PYTHONPATH": "src", "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

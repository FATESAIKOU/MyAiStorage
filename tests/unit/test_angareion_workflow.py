"""Unit tests for the registry rebuild workflow file (section 3.5; design sections 9/10).

The workflow only triggers, grants permissions and reports; the rebuild logic
lives in the script. These tests pin the trigger guard (Bot + single issue),
the concurrency, the permissions and the no-shell-interpolation rule.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "angareion-registry.yml"


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _on(data: dict) -> dict:
    # PyYAML reads the bare `on:` key as the boolean True (YAML 1.1).
    return data.get("on", data.get(True))


def test_the_workflow_exists_and_triggers_on_comments_a_schedule_and_manual_runs():
    data = _workflow()
    triggers = _on(data)
    assert triggers["issue_comment"]["types"] == ["created"]
    assert triggers["workflow_dispatch"] is None
    crons = triggers["schedule"]
    assert len(crons) == 1 and crons[0]["cron"].split()[4] == "*"  # once a day
    assert crons[0]["cron"].count(" ") == 4


def test_the_trigger_guard_blocks_bots_and_other_issues():
    data = _workflow()
    condition = data["jobs"]["rebuild"]["if"]
    assert "github.event.comment.user.type != 'Bot'" in condition
    # `!= 'Bot'` (not `== 'User'`): schedule events have no comment, and
    # `null != 'Bot'` is true, so the daily safety net still runs (review g).
    assert "== 'User'" not in condition
    # Only the single registry issue triggers runs.
    assert "github.event.issue.number == vars.ANGAREION_REGISTRY_ISSUE" in condition
    # schedule/workflow_dispatch runs are not filtered by the comment guard.
    assert "github.event_name != 'issue_comment'" in condition


def test_the_workflow_has_the_required_permissions():
    data = _workflow()
    permissions = data["permissions"]
    assert permissions["contents"] == "write"  # commit channels.json into main
    assert permissions["issues"] == "write"  # post reports on the registry issue


def test_the_workflow_cancels_older_runs_in_the_same_group():
    data = _workflow()
    concurrency = data["concurrency"]
    assert concurrency["group"] == "angareion-channels"
    assert concurrency["cancel-in-progress"] is True


def test_the_step_never_interpolates_comment_content_into_the_shell():
    data = _workflow()
    steps = data["jobs"]["rebuild"]["steps"]
    run_steps = [step for step in steps if "run" in step]
    assert len(run_steps) == 1
    command = run_steps[0]["run"]
    assert "${{" not in command  # no shell interpolation: inputs go through env
    assert "python3 -m angareion.registry_rebuild" in command

    env = run_steps[0]["env"]
    assert env["PYTHONPATH"] == "src"  # the runner needs no installed package
    assert env["GITHUB_TOKEN"] == "${{ github.token }}"
    assert env["ANGAREION_REGISTRY_REPO"] == "${{ github.repository }}"
    assert env["ANGAREION_REGISTRY_ISSUE"] == "${{ vars.ANGAREION_REGISTRY_ISSUE }}"
    # The comment body is passed as an environment variable (design section 9).
    assert env["ANGAREION_TRIGGER_COMMENT_BODY"] == "${{ github.event.comment.body }}"


def test_the_guard_semantics_match_the_design():
    """A small stand-in for the GitHub expression, evaluated for the key cases."""
    data = _workflow()
    condition = data["jobs"]["rebuild"]["if"]

    def guard(event_name: str, comment_user_type: str | None, issue_number: int, registry_issue: int) -> bool:
        if event_name != "issue_comment":
            return True
        return comment_user_type != "Bot" and issue_number == registry_issue

    # The expression the workflow actually carries, clause by clause.
    assert "github.event_name != 'issue_comment'" in condition
    assert "github.event.comment.user.type != 'Bot'" in condition

    assert guard("schedule", None, 0, 77) is True        # the daily run still runs
    assert guard("workflow_dispatch", None, 0, 77) is True
    assert guard("issue_comment", "User", 77, 77) is True
    assert guard("issue_comment", "Bot", 77, 77) is False   # a report never re-triggers
    assert guard("issue_comment", "User", 12, 77) is False  # another issue does nothing

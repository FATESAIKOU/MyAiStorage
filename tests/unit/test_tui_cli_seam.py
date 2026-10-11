"""T3: what the interactive mode hands the command mode has to be what the command mode
parses (review /tmp/agora-review/continue-periodic-sync-tui.md).

The TUI and the command parser are two halves built apart, and nothing else in the
suite ties them together: every interactive test compares argv with a FakeCli, and
every command-mode test never sees a TUI. If the two drift, the interactive continue
dies at `exit 2` on an unrecognised flag, and only a person at a terminal finds out.
So the TUI's own output goes through the real `cli.build_parser()` and the real
`cli._sync_interval_seconds()` here, and both sides' refusals are compared.

The values are the ones the box can produce (the default `5`, `0` meaning "never sync
on your own", a fraction like `0.5`) and the ones it refuses; the refused side is
checked on both sides of the seam, because a value the question lets through but the
command mode rejects is a continue that opens no agent at best and is refused after
the question at worst.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from agora import cli, tui

#: One row of the agora tab, which is what a continue stands for (argv_for contract).
ROWS = [tui.Row("agora:01K6ZZZZZZZZZZZZZZZZZZZZZZ", ["x"], "x", "opencode", "/tmp/p")]


def parse(argv: list[str]):
    """The real parser's verdict on argv; SystemExit means argparse refused it."""
    return cli.build_parser().parse_args(argv)


def continue_argv(typed: str) -> list[str]:
    """The command line the continue flow would run for this answer."""
    return tui.argv_for("continue", ROWS, "claude", "/w", typed)[0]


@pytest.mark.parametrize("typed, seconds", [
    ("5", 300.0),      # what the box offers
    ("0", None),       # a real answer: never sync on your own, not "no answer"
    ("0.5", 30.0),     # a fraction is minutes too
    ("2", 120.0),      # whatever was typed arrives as itself
])
def test_the_continue_argv_the_tui_builds_is_what_the_command_mode_parses(typed, seconds):
    argv = continue_argv(typed)
    assert argv[argv.index("--sync-period") + 1] == typed, \
        "旗標的值不是使用者填的那個字串（TUI 傳的是原文，兩邊都靠 float()）"
    args = parse(argv)   # an argparse exit here would be exactly the T3 failure
    assert args.action == "continue" and args.ids == [ROWS[0].key]
    assert args.sync_period == typed
    assert cli._sync_interval_seconds(args) == seconds


def test_every_action_the_tui_builds_is_something_the_command_mode_accepts():
    """Not just continue: a wrong flag in any action is a command that cannot run."""
    for action in ("continue", "merge", "edit", "delete", "pull", "push"):
        args = parse(tui.argv_for(action, ROWS, "opencode", "/w", "5")[0])
        assert args.action == action and args.type == "session", action
        # the period belongs to continue alone (design: other actions ignore it),
        # so only there does the value have to survive
        assert (args.sync_period == "5") == (action == "continue"), action
    imp = [tui.Row("opencode:ses_x", ["x"], "x", "opencode")]
    args = parse(tui.argv_for("import", imp, None, None)[0])
    assert args.action == "import" and args.external_session_id == ["ses_x"]


@pytest.mark.parametrize("typed", ["", " ", "abc", "-1", "-0.5", "nan", "inf"])
def test_a_period_the_question_refuses_is_one_the_command_mode_refuses_too(typed):
    """The question keeps the window open on these (spec「不是的話 MUST 留在輸入框」),
    so they are never sent - and if one ever were, the command mode gives exit 1
    rather than multiplying nonsense by 60."""
    assert tui.sync_period_problem(typed) is not None, "TUI 該拒絕，這個值才不會被送出去"
    args = parse(continue_argv(typed))   # argparse takes any string; the check is ours
    with pytest.raises(cli.InputError):
        cli._sync_interval_seconds(args)


def test_the_number_the_box_offers_is_the_number_the_command_defaults_to():
    """The box's default and the flag-less default are the same promise; drift here
    means the interactive mode silently offers one interval and gets another."""
    assert tui.SYNC_PERIOD_DEFAULT == 5          # the person's 10-10 decision
    args = parse(tui.argv_for("continue", ROWS, "claude", "/w")[0])   # no answer: no flag
    assert args.sync_period is None
    assert cli._sync_interval_seconds(args) == 5 * 60.0
    assert cli.SYNC_PERIOD_MINUTES == tui.SYNC_PERIOD_DEFAULT

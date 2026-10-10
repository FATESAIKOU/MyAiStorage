"""Unit tests for Angareion a2a CLI."""

from __future__ import annotations

import pytest

from angareion.cli import build_parser, main


def test_cli_help(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--help"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "a2a" in captured.out
    assert "send" in captured.out
    assert "inbox" in captured.out
    assert "ack" in captured.out


def test_subcommand_helps(capsys):
    parser = build_parser()
    for sub in ("send", "inbox", "ack"):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args([sub, "--help"])
        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert sub in captured.out


def test_cli_no_subcommand(capsys):
    ret = main([])
    assert ret == 1
    captured = capsys.readouterr()
    assert "usage:" in captured.err


def test_cli_subcommand_placeholder(capsys):
    ret = main(["send", "--to", "owner/repo", "--channel", "test"])
    assert ret == 0
    captured = capsys.readouterr()
    assert "placeholder" in captured.err

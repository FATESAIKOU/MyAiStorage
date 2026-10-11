"""The agent-facing skill doc exists and covers the four interfaces (D6)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SKILL = REPO / "skills" / "angareion" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_doc_exists_and_covers_the_four_interfaces():
    text = _text()
    for command in (
        "angareion channel create <名稱>",
        "angareion send --channel",
        "angareion wait [--channel",
        "angareion read [--channel",
    ):
        assert command in text


def test_skill_doc_tells_idle_agents_to_block_on_wait():
    text = _text()
    assert "angareion wait --timeout 600" in text  # the idle example
    assert "不要" in text and "反覆" in text  # no polling-the-model loop


def test_skill_doc_says_what_to_do_with_high_urgency():
    text = _text()
    assert "urgency" in text
    assert "7 以上先處理" in text


def test_skill_doc_contains_no_secrets_or_private_info():
    text = _text()
    assert not re.search(r"ghp_[A-Za-z0-9]", text)
    assert "BEGIN" not in text  # no PEM blocks
    assert "token" in text  # it may *mention* the token, but never its value

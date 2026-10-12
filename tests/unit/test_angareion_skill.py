"""The agent-facing skill doc exists and covers the five interfaces (D6/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SKILL = REPO / "skills" / "angareion" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_doc_exists_and_covers_the_five_interfaces():
    text = _text()
    for command in (
        "angareion channel create <名稱>",
        "angareion channel delete <名稱>",
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


def test_skill_doc_explains_exit_2_retry_and_the_reply_address():
    text = _text()
    assert "exit 2" in text and "稍等" in text  # wait exit 2 is a transient backend error: retry
    assert "<agent 名>" in text  # the reply address is the agent name in the message header
    assert "--to" in text


def test_skill_doc_defines_to_me_including_broadcasts():
    text = _text()
    assert "廣播" in text
    # "To me" includes messages with no `to` at all.
    assert "沒有 `to`" in text or "沒有 to" in text


def test_skill_doc_explains_the_channel_landing_delay():
    text = _text()
    assert "2 分鐘" in text  # create waits for the mapping, bounded
    assert "channel delete" in text and "留紀錄" in text


def test_skill_doc_uses_agent_names_for_addresses():
    text = _text()
    assert "LLMGateway-pm" in text or "MyLinuxPool-impl-2" in text
    assert "pm|arch|impl|test" in text


def test_skill_doc_contains_no_secrets_or_private_info():
    text = _text()
    assert not re.search(r"ghp_[A-Za-z0-9]", text)
    assert "BEGIN" not in text  # no PEM blocks
    assert "token" in text  # it may *mention* the token, but never its value


def test_skill_doc_says_private_info_does_not_belong_in_messages():
    text = _text()
    assert "私人資訊" in text
    assert "不要在訊息裡放私人資訊" in text
    assert "MyBrain" in text  # point the other side at MyBrain instead of pasting the content

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
    assert "<AI 名>" in text  # the reply address is the AI name in the message header
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


def test_skill_doc_uses_ai_names_for_addresses():
    text = _text()
    assert "mypmo-pm-m1" in text or "mylinuxpool-impl-w1-1" in text
    assert "`pm`／`arch`／`impl`／`test`／`rev`／`secretary`" in text
    assert "<專案>-<角色>-<team>[-<序號>]" in text
    assert "群組位址期 3" in text
    # The trim the tool does not do must not be promised to the reader.
    assert "換行、tab、CR 不會" in text


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


def test_skill_doc_covers_mentions_and_the_upgrade_order():
    text = _text()
    for flag in ("--mention", "--in-reply-to", "--mentioned"):
        assert flag in text
    assert "mentions" in text and "in_reply_to" in text
    assert "廣播不算" in text  # "@ me" is not "to me"
    assert "`--mention` 可以用了" in text and "讀得懂 `mentions`" in text  # --mention is open; new reader kinds must understand it
    assert "先升級" not in text  # the old "upgrade first" ban is gone


def test_skill_doc_to_me_definition_counts_mentions_and_the_first_mentioned_read_is_explained():
    text = _text()
    line = next(l for l in text.splitlines() if l.startswith("- 不給 `--channel` 時"))
    assert "mentions" in line and "廣播" in line
    assert "從頭讀" in text and "--since" in text


def test_idle_wait_is_limited_to_agents_nobody_pokes():
    text = _text()
    description = re.search(r"^description: (.*)$", text, re.M).group(1)
    assert "not managed by a sidecar" in description
    section = text.split("## 閒著的時候")[1].split("\n## ")[0]
    assert "只限沒有人會 poke 的 agent" in section
    assert "被 sidecar 管的 agent" in section and "閒著就結束這一輪、等 poke" in section
    assert "被叫醒就在前景讀信" in section
    assert "不要跑背景讀信迴圈" in section
    assert "同一個身分只留一個讀者" in section

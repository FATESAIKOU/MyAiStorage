"""The aicomm skill file and docs: the soft rules are written down, no private info (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SKILL = REPO / "skills" / "aicomm" / "SKILL.md"
DOCS = REPO / "docs" / "aicomm.md"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_has_frontmatter_with_name_and_description():
    text = _skill()
    head = text.split("---")[1]
    assert re.search(r"^name: aicomm$", head, re.M)
    assert re.search(r"^description: \S", head, re.M)


def test_skill_names_the_two_commands_and_nothing_else():
    text = _skill()
    assert "aicomm send" in text and "aicomm inbox" in text
    assert "aicomm poke" not in text and "aicomm status" not in text


def test_skill_states_the_three_soft_rules():
    text = _skill()
    assert "預設不中斷" in text and "--interrupt" in text
    assert "隊員基本上不要中斷" in text and "PM" in text and "PMO" in text
    rule3 = next(l for l in text.splitlines() if l.startswith("3. "))
    assert "「收到」類回覆" in rule3 and "--no-wake" in rule3 and "不叫醒" in rule3


def test_skill_points_hard_control_at_sidecar_open_not_at_aicomm_and_states_the_cost():
    text = _skill()
    assert "sidecar open" in text and "白名單" in text and "MyLinuxPool#346" in text
    assert "任何同台 agent 都能叫醒 PMO" in text
    assert "M2 D6" in text


def test_skill_explains_delivery_exit_codes_and_the_idle_wait():
    text = _skill()
    assert "送達" in text and "寫進 Angareion" in text
    for code in ("exit 0", "exit 1", "exit 2", "exit 3"):
        assert code in text
    assert "inbox --wait" in text
    assert "不要在訊息裡放私人資訊" in text


def test_skill_does_not_tell_the_agent_to_inbox_at_the_end_of_every_round():
    assert "每輪結束前" not in _skill()


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_no_secrets_or_private_info_patterns(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]", text)
    assert not re.search(r"\bsk-[A-Za-z0-9]", text)
    assert "BEGIN" not in text  # no PEM blocks
    assert "@gmail.com" not in text
    # no machine addresses: the only IP allowed is the sidecar's documented loopback default
    ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    assert ips <= {"127.0.0.1", "127.0.0.0"}  # the sidecar default and the loopback range spelling
    assert not re.search(r"/Users/|/home/[a-z]", text)


def test_docs_record_the_sidecar_fields_aicomm_depends_on():
    text = DOCS.read_text(encoding="utf-8")
    for needle in ("GET /agents/", "POST /agents/", "/poke", "state", "idle", "running", "404", "AICOMM_SIDECAR", "127.0.0.1:8471"):
        assert needle in text


# --- #219: one reader per identity ---

REMEDY = "angareion read --channel <ch> --since <時刻>"


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_one_reader_rule_and_remedy_are_written_down(path):
    text = path.read_text(encoding="utf-8")
    assert "同一個身分只留一個讀者" in text
    assert "背景" in text and "前景" in text
    assert "inbox --wait" in text
    assert "被叫醒但 inbox 是空的" in text
    assert REMEDY in text and "不移動游標" in text


def test_skill_does_not_call_a_background_inbox_wait_safe():
    text = _skill()
    assert "wait 之後會接著 read" in text or "wait 之後會接著讀" in text


def test_the_hint_in_the_cli_matches_the_documented_remedy():
    cli = (REPO / "src" / "aicomm" / "cli.py").read_text(encoding="utf-8")
    assert REMEDY in cli and "同一個身分" in cli

"""The user-facing docs carry the private-info reminder (#52)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DOCS = REPO / "docs" / "angareion.md"


def test_docs_carry_the_private_info_reminder():
    """The reminder section is in docs/angareion.md; deleting it fails this test."""
    text = DOCS.read_text(encoding="utf-8")
    assert "私人資訊" in text
    assert "不要在訊息裡放私人資訊" in text
    assert "參照 MyBrain" in text  # point the other side at MyBrain, not paste the content


def test_docs_state_the_ai_name_grammar_and_the_read_wide_rule():
    text = DOCS.read_text(encoding="utf-8")
    assert "<專案>-<角色>-<team>[-<序號>]" in text
    assert "src/ainame" in text  # where the grammar and the vectors live
    assert "讀信用寬的" in text
    assert "mylinuxpool-impl-w1-1" in text
    # The old grammar is documented as read-only, not as something to send.
    assert "MyLinuxPool-impl-2" in text
    assert "群組位址（`@…`）期 3 才有" in text


def test_docs_explain_the_environment_identity_switches_the_cursor_too():
    text = DOCS.read_text(encoding="utf-8")
    assert "ANGAREION_IDENTITY" in text
    assert "cursor-<AI 名>.json" in text
    assert "wait-<AI 名>.json" in text


def test_docs_state_that_only_half_width_spaces_are_forgiven():
    """R3/R4: the docs must not promise a trim the tool does not do."""
    text = DOCS.read_text(encoding="utf-8")
    assert "`--to` 只原諒半形空格" in text
    assert "換行、tab、CR 不會被去掉" in text
    assert "不修剪空白" in text

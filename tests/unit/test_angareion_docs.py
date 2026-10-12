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

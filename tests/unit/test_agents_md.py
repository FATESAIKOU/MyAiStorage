"""AGENTS.md is public: it carries no private information (#243).

The patterns come from test_atelier_harness (one copy); this file only points
them at the repo's own instruction file.
"""

from __future__ import annotations
import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from pathlib import Path

from test_atelier_harness import EMAIL_RE, HOME_PATH_RE, IPV4_RE, TOKEN_RES

AGENTS_MD = Path(__file__).resolve().parents[2] / "AGENTS.md"


def _text() -> str:
    return AGENTS_MD.read_text(encoding="utf-8")


def test_agents_md_has_no_private_information():
    text = _text()
    assert not IPV4_RE.search(text), "IPv4 address"
    assert not HOME_PATH_RE.search(text), "home directory path"
    assert not EMAIL_RE.search(text), "email address"
    for label, pattern in TOKEN_RES:
        assert not pattern.search(text), label


def test_agents_md_keeps_the_repo_practices_section():
    text = _text()
    assert "## 這個 repo 的做法" in text
    assert "AGORA_STATE_DIR" in text

"""Local caches (design 5.10): lazy full text of agent sessions, refreshed in one go."""

from __future__ import annotations

import json
import os

from agora import cache, store
from agora.agents.base import Exported, Listed


class Agent:
    name = "claude"

    def __init__(self, texts, listed=()):
        self.texts, self.listed, self.exports = texts, list(listed), 0

    def export(self, session_id):
        self.exports += 1
        if session_id not in self.texts:
            raise RuntimeError("gone")
        return Exported(session_id=session_id, raw=json.dumps({"m": self.texts[session_id]}).encode())

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def list_sessions(self):
        return self.listed


def test_full_text_is_kept_and_reread_only_when_the_session_is_newer():
    paths = store.Paths.from_env()
    agent = Agent({"s": ["問", "答"]})
    first = cache.local_reading(paths, agent, "s", "2026-10-02T00:00:00Z")
    assert "## user\n問" in first and agent.exports == 1
    assert cache.local_reading(paths, agent, "s", "2026-10-02T00:00:00Z") == first and agent.exports == 1
    agent.texts["s"] = ["問", "答", "又問"]
    assert "又問" in cache.local_reading(paths, agent, "s", "2026-10-03T00:00:00Z") and agent.exports == 2
    assert (paths.reading / "claude" / "s.md").exists()


def test_refresh_local_goes_on_past_a_failure_and_reports_progress(capsys):
    paths = store.Paths.from_env()
    agent = Agent({"a": ["一"], "c": ["三"]}, [Listed(x, None, None, None) for x in "abc"])
    assert cache.refresh_local(paths, [agent]) == (2, 1)
    out = capsys.readouterr().out
    assert "claude 3/3" in out and "讀不到" in out


def test_search_finds_cached_text_whatever_the_width_and_case():
    paths = store.Paths.from_env()
    cache.local_reading(paths, Agent({"s": ["Ｈｅｌｌｏ 表格"]}), "s")
    cache.local_reading(paths, Agent({"t": ["別的"]}), "t")
    assert list(cache.search_cached(paths, "claude", "hello")) == ["s"]
    assert list(cache.search_cached(paths, "opencode", "hello")) == []

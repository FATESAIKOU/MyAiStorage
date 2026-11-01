"""Tests for Exported raw helpers and turns_of."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
from pathlib import Path

from agora.agents.base import Exported, Turns, turns_of


class _SpyAgent:
    name = "spy"

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def turns(self, raw: bytes) -> Turns:
        self.calls.append(raw)
        return [("user", [raw.decode("utf-8")])]


def test_raw_digest_uses_raw_md5_without_accessing_raw(monkeypatch, tmp_path):
    accesses: list[int] = []
    real_getter = Exported.raw.fget

    def tracked_raw(self):
        accesses.append(1)
        return real_getter(self)

    monkeypatch.setattr(Exported, "raw", property(tracked_raw))

    p = tmp_path / "raw.json"
    p.write_bytes(b'{"key": "value"}')
    preset_md5 = "preset-md5-digest-string"
    exported = Exported(session_id="ses_1", raw_file=p, raw_md5=preset_md5)

    assert exported.raw_digest() == preset_md5
    assert not accesses, "raw_digest must not access .raw when raw_md5 is present"


def test_raw_digest_falls_back_to_computing_md5_from_raw(monkeypatch):
    accesses: list[int] = []
    real_getter = Exported.raw.fget

    def tracked_raw(self):
        accesses.append(1)
        return real_getter(self)

    monkeypatch.setattr(Exported, "raw", property(tracked_raw))

    payload = b"conversation byte stream"
    exported = Exported(session_id="ses_2", raw=payload, raw_md5=None)

    expected = hashlib.md5(payload).hexdigest()
    assert exported.raw_digest() == expected
    assert len(accesses) == 1, "raw_digest must access .raw when raw_md5 is None"


def test_raw_source_returns_file_when_present(tmp_path):
    file_path = tmp_path / "session_raw.jsonl"
    file_path.write_bytes(b'{"msg": 1}')
    exported = Exported(session_id="ses_file", raw_file=file_path)

    assert exported.raw_source() == file_path
    assert isinstance(exported.raw_source(), Path)


def test_raw_source_returns_raw_bytes_when_file_absent():
    payload = b'{"msg": 2}'
    exported = Exported(session_id="ses_bytes", raw=payload)

    assert exported.raw_source() == payload
    assert isinstance(exported.raw_source(), bytes)


def test_turns_of_uses_cached_turns_without_calling_agent():
    cached_turns: Turns = [("assistant", ["cached line"])]
    exported = Exported(session_id="ses_cached", turns=cached_turns, raw=b"raw-bytes")
    agent = _SpyAgent()

    result = turns_of(agent, exported)

    assert result == cached_turns
    assert not agent.calls, "turns_of must not call agent.turns when turns is present"


def test_turns_of_falls_back_to_agent_turns_when_cached_turns_absent():
    exported = Exported(session_id="ses_uncached", turns=None, raw=b"raw-content")
    agent = _SpyAgent()

    result = turns_of(agent, exported)

    assert result == [("user", ["raw-content"])]
    assert agent.calls == [b"raw-content"], "turns_of must invoke agent.turns(raw) when turns is None"

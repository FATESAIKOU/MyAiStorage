"""Unit tests for the angareion log cache (tasks 2.3)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from angareion.config import channel_slug
from angareion.log_cache import LogCache, log_cache_dir, log_cache_path


def test_channel_slug_keeps_safe_chars():
    assert channel_slug("general") == "general"
    assert channel_slug("abc_123") == "abc_123"


def test_channel_slug_encodes_slash_and_uppercase():
    assert "/" not in channel_slug("a/b")
    assert channel_slug("a/b").startswith("a%2F")
    # A and a differ
    assert channel_slug("A") != channel_slug("a")


def test_channel_slug_roundtrip_reversible():
    for name in ["general", "A", "a", "a/b", "../x", "MixedName", "x%y", "café"]:
        slug = channel_slug(name)
        # reversibility: decode %XX bytes
        data = bytearray()
        i = 0
        s = slug
        while i < len(s):
            if s[i] == "%" and i + 2 < len(s) + 1:
                data.append(int(s[i+1:i+3], 16))
                i += 3
            else:
                data.extend(s[i].encode("utf-8"))
                i += 1
        assert data.decode("utf-8") == name, (name, slug)


def test_dir_and_file_permissions(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    d = log_cache_dir(state_dir, identity)
    f = log_cache_path(state_dir, identity, "general")
    assert d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o700
    assert (f.stat().st_mode & 0o777) == 0o600


def test_bad_file_treated_as_empty(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    f = log_cache_path(state_dir, identity, "general")
    # corrupt
    f.write_text("{not json", encoding="utf-8")
    assert cache.load("general", {"repo": "r", "issue": 1}) is None
    # version mismatch
    f.write_text(json.dumps({"version": 999, "channel": "general", "entity": {"repo": "r", "issue": 1}, "messages": []}), encoding="utf-8")
    assert cache.load("general", {"repo": "r", "issue": 1}) is None


def test_entity_mismatch_treated_as_empty(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    f = log_cache_path(state_dir, identity, "general")
    data = json.loads(f.read_text())
    data["entity"] = {"repo": "r", "issue": 2}
    f.write_text(json.dumps(data), encoding="utf-8")
    assert cache.load("general", {"repo": "r", "issue": 1}) is None


def test_write_failure_warns_once(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    # make state_dir a file to force failure
    state_dir.write_text("x")
    cache = LogCache(state_dir, identity, warn=lambda m: None)
    warnings = []
    cache._warn = warnings.append
    cache._warned_write_failure = False
    # redirect path to read-only parent by using path under a file
    # Just call save; parent mkdir fails
    ret = cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    assert ret is False
    assert len(warnings) == 1
    # second failure no new warning
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    assert len(warnings) == 1


def test_channel_slug_no_path_escape(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("a/b", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    # file must be inside log dir
    d = log_cache_dir(state_dir, identity)
    files = list(d.iterdir())
    assert len(files) == 1
    assert files[0].parent == d


def test_atomic_replace_failure_keeps_original(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    f = log_cache_path(state_dir, identity, "general")
    original = f.read_text()
    import os as _os
    monkeypatch.setattr(_os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    ret = cache.save("general", {"repo": "r", "issue": 1}, "2", "2026-10-10T12:00:01Z", None, [])
    assert ret is False
    assert f.read_text() == original
    d = log_cache_dir(state_dir, identity)
    assert not any(p.name.endswith(".tmp") for p in d.iterdir())


def test_missing_field_treated_as_empty(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("general", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    f = log_cache_path(state_dir, identity, "general")
    data = json.loads(f.read_text())
    del data["last_comment_id"]
    f.write_text(json.dumps(data), encoding="utf-8")
    assert cache.load("general", {"repo": "r", "issue": 1}) is None


def test_a_and_a_different_files(tmp_path):
    state_dir = tmp_path / "state"
    identity = "mylinuxpool-impl-w1-1"
    cache = LogCache(state_dir, identity)
    cache.save("A", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    cache.save("a", {"repo": "r", "issue": 1}, "1", "2026-10-10T12:00:00Z", None, [])
    d = log_cache_dir(state_dir, identity)
    assert len(list(d.iterdir())) == 2

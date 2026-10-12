"""Unit tests for the local channel/backend config copy (v5 section 1.2).

Reading only, through an injected git runner: `git show origin/main:<path>`.
Nothing here boots git, touches the network or the working tree.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json

import pytest

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion.backend import ChannelNotFoundError
from angareion.channel_config import (
    BACKENDS_PATH,
    CHANNELS_PATH,
    BackendChoiceError,
    ChannelConfigError,
    ChannelStore,
)
from angareion.config import ConfigError


def _store(tmp_path, files=None, **kwargs) -> tuple[ChannelStore, FakeGit]:
    git = FakeGit(files)
    store = ChannelStore(
        tmp_path,
        git=git,
        interval=kwargs.pop("interval", 900),
        warn=kwargs.pop("warn", lambda message: None),
        now=kwargs.pop("now", lambda: 1000.0),
        clock=kwargs.pop("clock", lambda: 0.0),
        sleeper=kwargs.pop("sleeper", lambda seconds: None),
        fetch_head_mtime=kwargs.pop("fetch_head_mtime", None) or (lambda: git.fetch_head_mtime),
        **kwargs,
    )
    return store, git


def _files(channels=None, backends=None) -> dict[str, str]:
    files = {
        f"origin/main:{CHANNELS_PATH}": channels_json(channels or {}),
        f"origin/main:{BACKENDS_PATH}": backends_json(backends or {}),
    }
    return files


def test_reads_channel_and_backend_mappings_from_origin_main(tmp_path):
    store, git = _store(
        tmp_path,
        _files(
            channels={"general": ("github-main", 123)},
            backends={"github-main": ("github", "owner/repo")},
        ),
    )

    assert store.channel_names() == ["general"]
    ref = store.resolve("general")
    assert ref is not None
    assert (ref.name, ref.backend, ref.issue) == ("general", "github-main", 123)

    backend = store.backend("github-main")
    assert (backend.name, backend.type, backend.repo) == ("github-main", "github", "owner/repo")

    # The read is `git show origin/main:<path>`; the working tree is never used.
    assert git.show_calls() == [
        f"origin/main:{CHANNELS_PATH}",  # channel_names()
        f"origin/main:{CHANNELS_PATH}",  # resolve()
        f"origin/main:{BACKENDS_PATH}",  # resolve() checks the backend exists
        f"origin/main:{BACKENDS_PATH}",  # backend()
    ]


def test_unknown_channel_is_none_not_an_error(tmp_path):
    store, _ = _store(tmp_path, _files(channels={"general": ("github-main", 1)}, backends={"github-main": ("github", "owner/repo")}))
    assert store.resolve("nobody-knows") is None
    with pytest.raises(ChannelNotFoundError, match="does not exist"):
        store.entity_for("nobody-knows")


def test_missing_local_copy_is_a_config_error(tmp_path):
    missing = tmp_path / "no-clone-here"
    store = ChannelStore(missing, git=FakeGit(), warn=lambda message: None)
    with pytest.raises(ChannelConfigError) as excinfo:
        store.channels()
    assert str(missing) in str(excinfo.value)
    assert isinstance(excinfo.value, ConfigError)  # the CLI maps these to exit 2


def test_missing_channels_file_is_a_config_error(tmp_path):
    git = FakeGit({f"origin/main:{BACKENDS_PATH}": backends_json({})})
    store = ChannelStore(tmp_path, git=git, warn=lambda message: None)
    with pytest.raises(ChannelConfigError, match=CHANNELS_PATH):
        store.channels()


def test_broken_json_is_a_config_error(tmp_path):
    store, _ = _store(tmp_path, {f"origin/main:{CHANNELS_PATH}": "{not json"})
    with pytest.raises(ChannelConfigError, match="not valid JSON") as excinfo:
        store.channels()
    assert isinstance(excinfo.value, ConfigError)


def test_wrong_version_and_non_object_files_are_config_errors(tmp_path):
    store, _ = _store(tmp_path, {f"origin/main:{CHANNELS_PATH}": '{"channels": {}, "version": 2}'})
    with pytest.raises(ChannelConfigError, match="version"):
        store.channels()

    store, _ = _store(tmp_path, {f"origin/main:{CHANNELS_PATH}": "[]"})
    with pytest.raises(ChannelConfigError, match="JSON object"):
        store.channels()

    store, _ = _store(
        tmp_path, {f"origin/main:{CHANNELS_PATH}": json.dumps({"version": 1, "channels": []})}
    )
    with pytest.raises(ChannelConfigError, match="'channels'"):
        store.channels()


def test_broken_channel_entries_are_config_errors(tmp_path):
    bad_entries = [
        {"general": "not-an-object"},
        {"general": {"issue": 1}},
        {"general": {"backend": "github-main"}},
        {"general": {"backend": "github-main", "issue": "1"}},
        {"general": {"backend": "github-main", "issue": 0}},
    ]
    for entry in bad_entries:
        store, _ = _store(tmp_path, {f"origin/main:{CHANNELS_PATH}": json.dumps({"version": 1, "channels": entry})})
        with pytest.raises(ChannelConfigError):
            store.channels()


def test_broken_backend_entries_are_config_errors(tmp_path):
    bad_entries = [
        {"main": "not-an-object"},
        {"main": {"type": "github"}},  # a github backend needs a repo
        {"main": {"type": "", "repo": "owner/repo"}},
    ]
    for entry in bad_entries:
        store, _ = _store(tmp_path, {f"origin/main:{BACKENDS_PATH}": json.dumps({"version": 1, "backends": entry})})
        with pytest.raises(ChannelConfigError):
            store.backends()


def test_unsupported_backend_type_is_a_config_error_when_used(tmp_path):
    store, _ = _store(
        tmp_path,
        _files(
            channels={"general": ("other", 1)},
            backends={"other": ("matrix", "")},
        ),
    )
    with pytest.raises(ChannelConfigError, match="unsupported backend type"):
        store.resolve("general")
    with pytest.raises(ChannelConfigError, match="unsupported backend type"):
        store.backend("other")
    # Listing everything is fine; only using the unknown backend is an error.
    assert store.backends()["other"].type == "matrix"


def test_channel_pointing_to_an_unknown_backend_is_a_config_error(tmp_path):
    store, _ = _store(tmp_path, _files(channels={"general": ("ghost", 1)}))
    with pytest.raises(ChannelConfigError, match="unknown backend"):
        store.resolve("general")


def test_choose_backend_for_create(tmp_path):
    store, _ = _store(tmp_path, _files(backends={"github-main": ("github", "owner/repo")}))
    assert store.choose_backend_for_create(None).name == "github-main"  # exactly one: implicit
    assert store.choose_backend_for_create("github-main").name == "github-main"

    with pytest.raises(BackendChoiceError, match="not configured"):
        store.choose_backend_for_create("nosuch")

    two, _ = _store(
        tmp_path,
        _files(backends={"github-main": ("github", "owner/repo"), "github-2": ("github", "owner/repo2")}),
    )
    with pytest.raises(BackendChoiceError, match="--backend"):
        two.choose_backend_for_create(None)
    assert two.choose_backend_for_create("github-2").name == "github-2"

    empty, _ = _store(tmp_path, _files())
    with pytest.raises(BackendChoiceError, match="no backend"):
        empty.choose_backend_for_create(None)

    unknown_type, _ = _store(tmp_path, _files(backends={"other": ("matrix", "")}))
    with pytest.raises(ChannelConfigError, match="unsupported backend type"):
        unknown_type.choose_backend_for_create(None)

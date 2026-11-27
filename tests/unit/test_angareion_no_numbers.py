import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

# C-21 (design 542 §7, PR-3c): every new message of the paged write path (stdout, stderr and the
# exception text) carries no issue number and no repo name. The scenarios run through the CLI for
# `send`, `decision ask` and `channel rollover`, and the message builders are checked one by one.

import re
from types import SimpleNamespace

import pytest
import yaml

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion.backend import (
    BackendRef,
    ChannelFullError,
    ChannelNotFoundError,
    Entity,
)
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.fake_backend import FakeBackend
from angareion.registry import FakeRegistryClient, register_comment
from angareion.rollover import (
    REASONS,
    filling_warning,
    prepare_failed_warning,
    stuck_message,
)

ME = "myaistorage-impl-w1-1"
ANSWERER = "myaistorage-impl-w1-2"
TOKEN = "gh" + "p_FAKE_NO_NUMBERS_TOKEN_123"
REPO = "owner/repo"
REGISTRY_REPO = "owner/MyAiStorage"
BACKEND = "github-main"
CHANNEL = "general"
P, N = 1, 2
FORBIDDEN = [re.compile(r"#\d+"), re.compile(re.escape(REPO)), re.compile(re.escape(REGISTRY_REPO))]
DECISION_BODY = {
    "answerer": ANSWERER,
    "title": "題",
    "background": "背景",
    "body": "內容",
    "options": [{"key": "A", "text": "先合"}, {"key": "B", "text": "等確認"}],
    "recommended": "A",
    "reason": "理由",
}


class PagedStore:
    """One channel on one entity, with the `fetch` and `backends` that the page writer reads."""

    def __init__(self) -> None:
        self.entity = Entity(channel=CHANNEL, backend=BACKEND, repo=REPO, issue=P)

    def ensure_fresh(self) -> None:
        pass

    def fetch(self) -> bool:
        return True

    def entity_for(self, channel: str) -> Entity:
        if channel != CHANNEL:
            raise ChannelNotFoundError(f"channel '{channel}' does not exist")
        return self.entity

    def entities(self) -> list[Entity]:
        return [self.entity]

    def backends(self) -> dict[str, BackendRef]:
        return {BACKEND: BackendRef(name=BACKEND, type="github", repo=REPO)}


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    state_dir = tmp_path / "state"
    clone = tmp_path / "clone"
    clone.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    token_file.chmod(0o600)
    save_config(
        Config(
            identity=ME,
            config_dir=clone,
            registry_repo=REGISTRY_REPO,
            registry_issue=77,
            token_file=token_file,
            state_dir=state_dir,
            config_file=config_file,
        ),
        config_file,
    )
    monkeypatch.setenv("ANGAREION_CONFIG", str(config_file))
    monkeypatch.setenv("ANGAREION_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))
    return SimpleNamespace(clone=clone)


def _registry(*, register: bool = True, post_association: str = "OWNER", **kwargs) -> FakeRegistryClient:
    registry = FakeRegistryClient(post_association=post_association, **kwargs)
    if register:
        registry.add_comment(register_comment(CHANNEL, BACKEND, P), author_association="OWNER")
    return registry


def _backend(store: PagedStore, *, size: int | None) -> FakeBackend:
    backend = FakeBackend(store)
    backend.add_entity(CHANNEL, number=P)
    if size is not None:
        backend.set_page_size(P, size)
    return backend


def _send_full_registry_unreadable(cli_env, capsys):
    store = PagedStore()
    registry = _registry(fail_comments_times=1)
    backend = _backend(store, size=2500)
    return main(["send", "--channel", CHANNEL, "--body", "hi"], backend=backend, store=store, registry=registry)


def _send_prepare_failed(cli_env, capsys):
    store = PagedStore()
    registry = _registry(fail_comments_times=1)
    backend = _backend(store, size=2450)
    return main(["send", "--channel", CHANNEL, "--body", "hi"], backend=backend, store=store, registry=registry)


def _decision_ask_full(cli_env, capsys):
    store = PagedStore()
    registry = _registry(fail_comments_times=1)
    backend = _backend(store, size=2500)
    body = yaml.safe_dump(DECISION_BODY, allow_unicode=True, sort_keys=False)
    return main(["decision", "ask", "--channel", CHANNEL, "--body", body], backend=backend, store=store, registry=registry)


def _rollover(cli_env, registry: FakeRegistryClient) -> int:
    git = FakeGit(
        {
            f"origin/main:{CHANNELS_PATH}": channels_json({CHANNEL: (BACKEND, P)}),
            f"origin/main:{BACKENDS_PATH}": backends_json({BACKEND: ("github", REPO)}),
        },
        fetch_head_mtime=1000.0,
    )
    now = [0.0]

    def sleep(seconds: float) -> None:  # the landing wait must end even if a scenario ever reaches it
        now[0] += seconds

    store = ChannelStore(cli_env.clone, git=git, interval=0, clock=lambda: now[0], sleeper=sleep)
    backend = FakeBackend(store)
    backend.add_entity(CHANNEL, number=P)
    return main(["channel", "rollover", CHANNEL], backend=backend, store=store, registry=registry, sleeper=sleep)


def _rollover_unreadable(cli_env, capsys):
    return _rollover(cli_env, _registry(fail_comments_times=1))


def _rollover_mismatch(cli_env, capsys):
    return _rollover(cli_env, _registry(register=False))


def _rollover_rejected(cli_env, capsys):
    return _rollover(cli_env, _registry(post_association="NONE"))


SCENARIOS = {
    "send-stuck-full": _send_full_registry_unreadable,
    "send-prepare-failed": _send_prepare_failed,
    "decision-ask-stuck-full": _decision_ask_full,
    "rollover-unreadable": _rollover_unreadable,
    "rollover-mismatch": _rollover_mismatch,
    "rollover-rejected": _rollover_rejected,
}


def _assert_clean(text: str) -> None:
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{pattern.pattern!r} found in {text!r}"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_c21_the_failing_scenarios_print_no_issue_number_and_no_repo_name(name, cli_env, capsys):
    main_call = SCENARIOS[name]
    ret = main_call(cli_env, capsys)
    captured = capsys.readouterr()

    assert ret in (0, 2)
    assert captured.err, "every scenario here prints something on stderr"
    _assert_clean(captured.out)
    _assert_clean(captured.err)


def test_c21_the_channel_full_error_text_has_no_issue_number_or_repo_name():
    err = ChannelFullError(CHANNEL, 2500, presumed=False, status=403, detail="Commenting is disabled")

    _assert_clean(str(err))
    assert "channel rollover" in str(err)


@pytest.mark.parametrize("kind", sorted(REASONS))
def test_c21_every_successor_reason_renders_clean_in_the_stuck_message(kind):
    _assert_clean(stuck_message(CHANNEL, REASONS[kind]))


@pytest.mark.parametrize("kind", sorted(REASONS))
def test_c21_every_successor_reason_renders_clean_in_the_prepare_warning(kind):
    _assert_clean(prepare_failed_warning(CHANNEL, REASONS[kind]))


def test_c21_the_fill_warning_renders_clean_at_every_count_the_writer_reports():
    for count in (2000, 2400, 2450, 2490, 2500):
        _assert_clean(filling_warning(CHANNEL, count))

import _guard  # noqa: F401  (must be first: isolates HOME, agora and angareion state)

# Contract tests of design 542 PR-3c (§6.4 3c): C-19 `channel rollover NAME` and C-20 `send` and
# `decision` writing through the page writer. The Fake backend and the in-memory registry stand in
# for GitHub; no network, and no real waiting: the landing wait of `poll_for_page` runs on a clock
# that the fake sleep advances.

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Callable

import pytest
import yaml

from _angareion_fakes import FakeGit, backends_json, channels_json
from angareion.backend import (
    BackendError,
    BackendRef,
    ChannelNotFoundError,
    Entity,
    RetiredPage,
    format_backend_time,
)
from angareion.channel_cli import cmd_channel_rollover
from angareion.channel_config import BACKENDS_PATH, CHANNELS_PATH, ChannelStore
from angareion.cli import main
from angareion.config import Config, save_config
from angareion.fake_backend import FakeBackend
from angareion.registry import FakeRegistryClient, register_comment, rollover_comment
from angareion.rollover import (
    MISMATCH,
    OPEN_FAILED,
    REASONS,
    REGISTRY_UNREADABLE,
    REJECTED,
    UNREGISTERED,
    filling_warning,
    prepare_failed_warning,
)
from angareion.state import PendingPageStore

ME = "myaistorage-impl-w1-1"
ANSWERER = "myaistorage-impl-w1-2"
TOKEN = "gh" + "p_FAKE_PR3C_CLI_TOKEN_123"
REPO = "owner/repo"
BACKEND = "github-main"
CHANNEL = "general"
P = 1  # the channel's current page at the start
N = 2  # the page FakeBackend opens next, when no other entity is known yet
CHANNELS_KEY = f"origin/main:{CHANNELS_PATH}"
DECISION_BODY = {
    "answerer": ANSWERER,
    "title": "要不要先合 PR-3c",
    "background": "背景",
    "body": "內容",
    "options": [{"key": "A", "text": "先合"}, {"key": "B", "text": "等確認"}],
    "recommended": "A",
    "reason": "理由",
}
NOTICE = (
    "Notice: channel 'general' has a new page but the local copy has not caught up yet; "
    "it will be used once the registry rebuild lands.\n"
)


class Ticks:
    """A clock that moves only when something sleeps on it, so waiting costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class PagedStore:
    """The store `main` reads for `send` and `decision`: one channel on one entity.

    `StaticResolver` has no `fetch` or `backends`, which `PageWriter` and the refresh need.
    `fetched`, when set, is the entity the next `fetch` switches to.
    """

    def __init__(self, entity: Entity) -> None:
        self.entity = entity
        self.fetches = 0
        self.fetched: Entity | None = None

    def ensure_fresh(self) -> None:
        pass

    def fetch(self) -> bool:
        self.fetches += 1
        if self.fetched is not None:
            self.entity = self.fetched
        return True

    def entity_for(self, channel: str) -> Entity:
        if channel != self.entity.channel:
            raise ChannelNotFoundError(f"channel '{channel}' does not exist")
        return self.entity

    def entities(self) -> list[Entity]:
        return [self.entity]

    def backends(self) -> dict[str, BackendRef]:
        return {BACKEND: BackendRef(name=BACKEND, type="github", repo=REPO)}


def _entity(issue: int = P, backend: str = BACKEND, retired: tuple[RetiredPage, ...] = ()) -> Entity:
    return Entity(channel=CHANNEL, backend=backend, repo=REPO, issue=issue, retired=retired)


def _on_page(backend: FakeBackend, issue: int) -> list:
    return [c for c in backend._comments if c.page == (REPO, issue)]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A config file and token for `main`, with a state directory of its own."""
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
            registry_repo="owner/MyAiStorage",
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
    return SimpleNamespace(state_dir=state_dir, clone=clone, ticks=Ticks())


def _git(on_fetch: Callable[[FakeGit], None] | None = None) -> FakeGit:
    """The local configuration copy: channel 'general' on page P. `on_fetch` may bring a new page in."""
    git = FakeGit(
        {
            CHANNELS_KEY: channels_json({CHANNEL: (BACKEND, P)}),
            f"origin/main:{BACKENDS_PATH}": backends_json({BACKEND: ("github", REPO)}),
        },
        fetch_head_mtime=1000.0,
    )
    if on_fetch is not None:
        git.on_fetch = lambda: on_fetch(git)
    return git


def _land(git: FakeGit, issue: int) -> None:
    git.files[CHANNELS_KEY] = channels_json({CHANNEL: (BACKEND, issue)})


def _real_store(env, git: FakeGit) -> ChannelStore:
    return ChannelStore(env.clone, git=git, interval=0, clock=env.ticks.clock, sleeper=env.ticks.sleep)


def _registry(*, post_association: str = "OWNER", register_page: int | None = P, **kwargs) -> FakeRegistryClient:
    registry = FakeRegistryClient(post_association=post_association, **kwargs)
    if register_page is not None:
        registry.add_comment(register_comment(CHANNEL, BACKEND, register_page), author_association="OWNER")
    return registry


def _rollover_backend(store) -> FakeBackend:
    backend = FakeBackend(store)
    backend.add_entity(CHANNEL, number=P)
    return backend


def _run_rollover(env, store, backend, registry, capsys, name: str = CHANNEL):
    ret = main(["channel", "rollover", name], backend=backend, store=store, registry=registry, sleeper=env.ticks.sleep)
    captured = capsys.readouterr()
    return ret, captured.out, captured.err


# -- C-19: channel rollover --------------------------------------------------------------------------


def test_c19_rollover_opens_a_page_whatever_the_count_and_registers_it(cli_env, capsys):
    git = _git(lambda g: _land(g, backend.opened[-1] if backend.opened else P))
    store = _real_store(cli_env, git)
    registry = _registry()
    backend = _rollover_backend(store)
    backend.set_page_size(P, 5)  # a nearly empty page is rolled over all the same

    ret, out, err = _run_rollover(cli_env, store, backend, registry, capsys)

    assert (ret, out, err) == (0, f"{CHANNEL}\n", "")
    assert backend.opened == [N]
    assert backend.size_calls == []  # the count is not looked at
    assert len(registry.posted) == 1


def test_c19_the_fetch_comes_first_so_a_page_that_just_landed_is_forced_again(cli_env, capsys):
    # The local copy says P, but the registry already moved the channel to N and the fetch brings N in.
    # With the fetch, the forced rollover works from N and opens a page after it. Without the fetch it
    # would work from P, find P retired, and wrongly open nothing.
    git = _git(lambda g: _land(g, backend.opened[-1] if backend.opened else N))
    store = _real_store(cli_env, git)
    registry = _registry()
    registry.add_comment(rollover_comment(CHANNEL, BACKEND, N, P), author_association="OWNER")
    backend = _rollover_backend(store)
    backend.add_entity(CHANNEL, number=N)

    ret, out, err = _run_rollover(cli_env, store, backend, registry, capsys)

    assert (ret, out, err) == (0, f"{CHANNEL}\n", "")
    assert backend.opened == [N + 1]


def test_c19_a_pending_successor_is_not_opened_again_and_the_wait_only_prints_a_notice(cli_env, capsys):
    store = _real_store(cli_env, _git())  # nothing lands: the local copy stays on P
    registry = _registry()
    registry.add_comment(rollover_comment(CHANNEL, BACKEND, N, P), author_association="OWNER")
    backend = _rollover_backend(store)
    backend.add_entity(CHANNEL, number=N)

    ret, out, err = _run_rollover(cli_env, store, backend, registry, capsys)

    assert ret == 0
    assert out == f"{CHANNEL}\n"
    assert err == NOTICE
    assert backend.opened == []  # S-3: the registry already has the new page
    assert cli_env.ticks.now == 120.0  # the landing wait ran to its limit, on the fake clock


def test_c19_an_unregistered_name_exits_1(cli_env, capsys):
    store = _real_store(cli_env, _git())
    backend = _rollover_backend(store)

    ret, out, err = _run_rollover(cli_env, store, backend, _registry(), capsys, name="nosuch")

    assert ret == 1
    assert out == ""
    assert "does not exist" in err
    assert backend.opened == []


def test_c19_a_name_that_is_not_a_channel_name_exits_1(cli_env, capsys):
    store = _real_store(cli_env, _git())
    backend = _rollover_backend(store)

    ret, out, err = _run_rollover(cli_env, store, backend, _registry(), capsys, name="bad name")

    assert ret == 1
    assert out == ""
    assert backend.opened == []


@pytest.mark.parametrize(
    ("kind", "opened", "registry_kwargs", "open_error"),
    [
        (REGISTRY_UNREADABLE, 0, {"fail_comments_times": 1}, False),  # S-1
        (MISMATCH, 0, {"register_page": None}, False),  # S-2: the registry does not know the channel
        (OPEN_FAILED, 0, {}, True),  # S-5
        (UNREGISTERED, 1, {"fail_posts_times": 100}, False),  # S-7
        (REJECTED, 1, {"post_association": "NONE"}, False),  # S-8′: the listing ignores the new page
    ],
)
def test_c19_a_failed_rollover_exits_2_and_says_no_page_was_rolled_over(
    kind, opened, registry_kwargs, open_error, cli_env, capsys
):
    store = _real_store(cli_env, _git())
    registry = _registry(**registry_kwargs)
    backend = _rollover_backend(store)
    if open_error:
        backend.next_open_error = BackendError("refused")

    ret, out, err = _run_rollover(cli_env, store, backend, registry, capsys)

    assert ret == 2
    assert out == ""
    assert err == f"Error: channel '{CHANNEL}' was not rolled over: {REASONS[kind]}\n"
    assert len(backend.opened) == opened


def test_c19_a_repost_registers_a_page_the_listing_already_has_but_does_not_apply(cli_env, capsys):
    # A pending record names N, and the listing already has the same rollover from an author the rebuild
    # does not accept. The automatic path would refuse it (S-4′); `channel rollover` posts it again.
    registry = _registry()
    registry.add_comment(rollover_comment(CHANNEL, BACKEND, N, P), author_association="NONE")
    store = _real_store(cli_env, _git(lambda g: _land(g, N) if registry.posted else None))
    backend = _rollover_backend(store)
    backend.add_entity(CHANNEL, number=N)
    PendingPageStore(cli_env.state_dir, ME).put(CHANNEL, BACKEND, N, P)

    ret, out, err = _run_rollover(cli_env, store, backend, registry, capsys)

    assert (ret, out, err) == (0, f"{CHANNEL}\n", "")
    assert len(registry.posted) == 1
    assert backend.opened == []  # the record's page is used, not a new one


def test_c19_the_command_refuses_an_app_without_a_writer(cli_env):
    store = _real_store(cli_env, _git())

    ret = cmd_channel_rollover(SimpleNamespace(name=CHANNEL), None, SimpleNamespace(writer=None), _registry(), store, TOKEN)

    assert ret == 2


def test_c19_poll_for_page_returns_once_the_page_lands(cli_env):
    def land_on_third_fetch(git: FakeGit) -> None:
        if git.fetch_calls == 3:
            _land(git, N)

    git = _git(land_on_third_fetch)
    store = _real_store(cli_env, git)

    assert store.poll_for_page(CHANNEL, N) is True
    assert git.fetch_calls == 3
    assert cli_env.ticks.now == 10.0  # two waits of the landing interval before the third fetch lands it


def test_c19_poll_for_page_gives_up_on_the_clock_limit(cli_env):
    store = _real_store(cli_env, _git())

    assert store.poll_for_page(CHANNEL, N, timeout=12.0) is False
    assert cli_env.ticks.now == 12.0


# -- C-20: send and decision go through the page writer ----------------------------------------------


def _send_world(issue_size: int | None = None, *, registry_fail: int = 0):
    store = PagedStore(_entity())
    registry = _registry(fail_comments_times=registry_fail)
    backend = FakeBackend(store)
    backend.add_entity(CHANNEL, number=P)
    if issue_size is not None:
        backend.set_page_size(P, issue_size)
    return store, registry, backend


def _send(store, registry, backend, capsys):
    ret = main(["send", "--channel", CHANNEL, "--body", "hi"], backend=backend, store=store, registry=registry)
    captured = capsys.readouterr()
    return ret, captured.out, captured.err


def test_c20_send_on_a_full_page_lands_on_the_next_page_with_no_warning(cli_env, capsys):
    store, registry, backend = _send_world(2500)

    ret, out, err = _send(store, registry, backend, capsys)

    assert ret == 0
    assert out.strip() != ""
    assert err == ""  # the warning is for a page that is still filling, and this one has moved on
    assert backend.opened == [N]
    assert len(_on_page(backend, N)) == 1
    assert _on_page(backend, P) == []


def test_c20_decision_ask_on_a_full_page_lands_on_the_next_page(cli_env, capsys):
    store, registry, backend = _send_world(2500)
    body = yaml.safe_dump(DECISION_BODY, allow_unicode=True, sort_keys=False)

    ret = main(["decision", "ask", "--channel", CHANNEL, "--body", body], backend=backend, store=store, registry=registry)
    captured = capsys.readouterr()

    assert ret == 0, captured.err
    assert captured.err == ""
    assert backend.opened == [N]
    assert len(_on_page(backend, N)) == 1
    assert _on_page(backend, P) == []


def test_c20_a_page_at_2100_gets_exactly_the_c4_line_and_the_message_stays_on_the_page(cli_env, capsys):
    store, registry, backend = _send_world(2100)

    ret, out, err = _send(store, registry, backend, capsys)

    assert ret == 0
    assert err.splitlines() == ["Warning: " + filling_warning(CHANNEL, 2100)]
    assert len(_on_page(backend, P)) == 1
    assert backend.opened == []


def test_c20_a_failed_prepare_at_2450_prints_two_lines_in_order_and_writes_the_current_page(cli_env, capsys):
    # The registry cannot be read (W-8 fails): the message goes to P, and the prepare warning comes first.
    store, registry, backend = _send_world(2450, registry_fail=1)

    ret, out, err = _send(store, registry, backend, capsys)

    assert ret == 0
    assert err.splitlines() == [
        "Warning: " + prepare_failed_warning(CHANNEL, REASONS[REGISTRY_UNREADABLE]),
        "Warning: " + filling_warning(CHANNEL, 2450),
    ]
    assert len(_on_page(backend, P)) == 1
    assert backend.opened == []


def test_c20_a_refresh_fetches_the_local_copy_once_and_then_writes_the_new_page(cli_env, capsys):
    # The local copy names a backend the registry does not know (S-2), so W-9 refreshes it once; the
    # fetch brings in the right entity, and the next try opens the page and writes to it.
    store = PagedStore(_entity(backend="stale-backend"))
    store.fetched = _entity()
    registry = _registry()
    backend = FakeBackend(store)
    backend.add_entity(CHANNEL, number=P)
    backend.set_page_size(P, 2500)

    ret, out, err = _send(store, registry, backend, capsys)

    assert ret == 0
    assert err == ""
    assert store.fetches == 1
    assert backend.opened == [N]
    assert len(_on_page(backend, N)) == 1


def test_c20_a_write_to_a_retired_page_inside_its_window_prints_no_warning(cli_env, capsys):
    # Retired page P was retired an hour ago and holds 2100 messages: W-1 writes there, quietly.
    now = _utc_now()
    until = format_backend_time(now - timedelta(hours=1))
    store = PagedStore(_entity(issue=N, retired=(RetiredPage(issue=P, until=until, side=False),)))
    registry = _registry(register_page=None)
    backend = FakeBackend(store, start_entity_id=N + 1, page_date=lambda: format_backend_time(now))
    backend.add_entity(CHANNEL, number=P)
    backend.add_entity(CHANNEL, number=N)
    backend.set_page_size(P, 2100)

    ret, out, err = _send(store, registry, backend, capsys)

    assert ret == 0
    assert err == ""
    assert len(_on_page(backend, P)) == 1
    assert _on_page(backend, N) == []

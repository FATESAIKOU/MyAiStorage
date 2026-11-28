"""#491 PR-3 (design §6.3, contracts C-12 to C-21): the list first, the sync in a child process behind it.

The sync child is a stand-in (`SyncProc`): its lines and its end come when the test releases
them, so every wait is on an event, never on a sleep. The write actions are the same stand-ins
as in test_tui.py (`_spawn`): nothing real starts.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import asyncio
import threading

import pytest
from textual.widgets import Static

from agora import store, tui, tui_sync
from agora.agents.base import Listed
from test_tui import FakeAgent, FakeCli, _hdr, _index, _spawn

RESULT = tui_sync.RESULT
ERROR = tui_sync.ERROR
DONE_LINES = [RESULT + '{"offline": false}']
OFFLINE_LINES = [RESULT + '{"offline": true}']
FAILED_DETAIL = "RuntimeError: 連不上的那一行"
FAILED_LINES = [ERROR + FAILED_DETAIL]


def ulid_of(letter: str) -> str:
    return "01" + letter * 24


A, B, C, D = ulid_of("A"), ulid_of("B"), ulid_of("C"), ulid_of("D")   # D: a session the sync brings


def session(ulid: str, title: str, body: str = "## user\n別的\n", sid: str | None = None):
    return (_hdr(ulid, title, sid=sid or f"ses_{ulid[-1]}"), body)


def two_sessions():
    return (session(A, "第一個", "## user\n表格的問題\n"), session(B, "第二個"))


def three_sessions():
    return (session(A, "第一個"), session(B, "第二個"), session(C, "第三個"))


class SyncProc:
    """The sync child, as the reader thread sees it. Its lines and its end wait for `release`.

    `signals` records anything sent to it: C-20 needs it to stay empty.
    """

    pid = 5151

    def __init__(self):
        self.gate = threading.Event()
        self.lines: list[str] = []
        self.code = 0
        self.signals: list = []
        self.stdout = self._out()

    def _out(self):
        self.gate.wait(timeout=10)
        for line in self.lines:
            yield f"{line}\n"

    def release(self, lines=DONE_LINES, code: int = 0) -> None:
        self.lines, self.code = list(lines), code
        self.gate.set()

    def wait(self):
        self.gate.wait(timeout=10)
        return self.code

    def poll(self):
        return self.code if self.gate.is_set() else None

    def send_signal(self, sig):
        self.signals.append(int(sig))

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


def make_app(*, sessions=None, agents=(), sync_on_start: bool = True):
    paths, _ = _index(*(two_sessions() if sessions is None else sessions))
    cli = FakeCli()
    app = tui.AgoraApp(paths, cli, agents=list(agents), check_setup=False, sync_on_start=sync_on_start)
    app.proc = SyncProc()
    app.sync_started = []

    def spawn_sync():
        app.sync_started.append(1)
        return app.proc
    app.spawn_sync = spawn_sync
    app.spawn, app._last_spawned = _spawn()     # no write action reaches a real process
    return app, cli


def run_app(app, body):
    """Run `body(pilot)` on a test screen; the sync child is released on the way out, whatever happens."""
    async def go():
        try:
            async with app.run_test(size=(120, 30)) as pilot:
                await body(pilot)
        finally:
            app.proc.release()          # never leave the reader thread waiting for its gate
    asyncio.run(go())


async def until(pred, pilot, tries: int = 200) -> bool:
    for _ in range(tries):
        if pred():
            return True
        await pilot.pause()
    return pred()


def bar(app) -> str:
    return str(app.query_one("#bar", Static).render())


def msg(app) -> str:
    return str(app.query_one("#msg", Static).render())


def msg_failed(app) -> bool:
    return app.query_one("#msg").has_class("failed")


def count_shows(app, monkeypatch) -> list:
    calls: list = []
    real = app.show

    def counting(*args, **kwargs):
        calls.append(kwargs.get("preview", True))
        real(*args, **kwargs)
    monkeypatch.setattr(app, "show", counting)
    return calls


async def settled(app, pilot) -> None:
    await asyncio.wait_for(app.sync_settled.wait(), 5)
    await pilot.pause()


# --- C-12: the list is there and usable before the child is started -----------------------------

def test_c12_the_list_is_usable_while_the_sync_child_is_still_running():
    app, _ = make_app()
    seen = {}
    real = app.spawn_sync

    def spawn_sync():
        seen["rows"] = len(app.rows["agora"])        # reload has come first (C-12: after reload)
        seen["idle"] = app._sync_idle.is_set()
        return real()
    app.spawn_sync = spawn_sync

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot), "the sync child never started"
        assert seen == {"rows": 2, "idle": False}
        table = app.query_one("#table")
        assert table.row_count == 2 and table.has_focus
        assert not app._sync_idle.is_set()
        await pilot.press("down")
        assert table.cursor_row == 1, "the list must move while the sync runs"
    run_app(app, body)


def test_c12_begin_sync_is_refused_while_one_is_running(monkeypatch):
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        starts = []
        monkeypatch.setattr(app.sync, "start", lambda: (starts.append(1), False)[1])
        assert app.begin_sync() is False
        assert not app._sync_idle.is_set()
        assert starts == []
        assert app.sync_started == [1]
    run_app(app, body)


def test_c12_no_background_sync_when_the_screen_says_so():
    app, _ = make_app(sync_on_start=False)

    async def body(pilot):
        await pilot.pause()
        assert app.sync_started == [] and app._sync_idle.is_set()
        assert app.query_one("#table").row_count == 2
    run_app(app, body)


# --- C-12′: no child at all -------------------------------------------------------------------

def test_c12_prime_spawn_error_is_a_failed_sync_told_on_this_thread(monkeypatch):
    app, _ = make_app()

    def no_child():
        raise OSError("沒有 python")
    app.spawn_sync = no_child

    async def body(pilot):
        await settled(app, pilot)                    # the list settles although no child ever ran
        assert app.status == "同步失敗：只有本機資料"
        assert "叫不起同步子行程" in msg(app) and msg_failed(app)
        await pilot.press("d")
        await pilot.pause()
        assert not isinstance(app.screen, tui.WaitSync), "nothing is syncing: no wait"
        assert isinstance(app.screen, tui.Choose)
    run_app(app, body)


# --- C-13: the title's indicator and the states -----------------------------------------------

def test_c13_the_bar_shows_the_sync_when_it_was_due(monkeypatch):
    monkeypatch.setattr(store, "sync_due", lambda paths: True)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        assert "同步 Drive 中" in bar(app)
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert "同步 Drive 中" not in bar(app)
        assert app.status == "" and msg(app) == ""       # done with no change says nothing (Q-4)
    run_app(app, body)


def test_c13_the_bar_stays_quiet_when_the_sync_was_throttled(monkeypatch):
    monkeypatch.setattr(store, "sync_due", lambda paths: False)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        assert "同步 Drive 中" not in bar(app)
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
    run_app(app, body)


def test_c13_bar_repaints_on_sync_tick(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_TICK", 0.01)
    app, _ = make_app()
    ticks: list = []
    real_paint = app.paint_bar
    monkeypatch.setattr(app, "paint_bar", lambda: (ticks.append(1), real_paint()))

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        assert await until(lambda: len(ticks) >= 3, pilot)
        assert app._sync_tick_timer is not None
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert app._sync_tick_timer is None
    run_app(app, body)


def test_c13_offline_and_failed_put_their_word_in_the_bar():
    app, _ = make_app()

    async def offline(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.proc.release(OFFLINE_LINES)
        await settled(app, pilot)
        assert app.status == "離線：只有本機資料" and "離線" in bar(app)
        assert msg(app) == "" and not msg_failed(app)     # no change: the message line keeps quiet
    run_app(app, offline)

    app2, _ = make_app()

    async def failed(pilot):
        assert await until(lambda: app2.sync.running, pilot)
        app2.proc.release(FAILED_LINES, code=2)
        await settled(app2, pilot)
        assert app2.status == "同步失敗：只有本機資料" and "同步失敗" in bar(app2)
        assert msg(app2) == FAILED_DETAIL and msg_failed(app2)
    run_app(app2, failed)


# --- C-14: what happens to the list when the sync settles -----------------------------------

@pytest.mark.parametrize("lines, add_session, expect_show, expect_msg, expect_failed", [
    (DONE_LINES, False, 0, "", False),
    (DONE_LINES, True, 1, "同步完成：新增 1", False),
    (OFFLINE_LINES, True, 1, "本機清單有更新：新增 1", False),
    (FAILED_LINES, True, 1, f"{FAILED_DETAIL}（本機清單有更新：新增 1）", True),
    (FAILED_LINES, False, 0, FAILED_DETAIL, True),
])
def test_c14_the_list_is_redrawn_only_when_it_changed_and_the_words_follow_the_outcome(
        monkeypatch, lines, add_session, expect_show, expect_msg, expect_failed):
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        calls = count_shows(app, monkeypatch)
        if add_session:
            _index(session(C, "第三個", "## user\n表格新的\n"))     # what the child wrote to the index
        app.proc.release(lines, code=2 if lines is FAILED_LINES else 0)
        await settled(app, pilot)
        assert len(calls) == expect_show
        assert msg(app) == expect_msg
        assert msg_failed(app) is expect_failed
    run_app(app, body)


def test_c14_a_change_keeps_the_cursor_on_its_session_and_the_marks_that_stay(monkeypatch):
    app, _ = make_app(sessions=three_sessions())

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("space")                    # the row under the cursor
        await pilot.press("down")
        cursor_key = app.current().key
        marked_before = set(app.marked)
        _index(session(D, "第四個", "## user\n新的\n"))  # a new row, above or below
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert app.current().key == cursor_key
        assert app.marked == marked_before
        assert msg(app) == "同步完成：新增 1"
    run_app(app, body)


def test_c14_a_mark_whose_row_is_gone_is_dropped_and_said(monkeypatch):
    app, _ = make_app(sessions=three_sessions())

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("space")                  # the row under the cursor is marked
        gone = app.current().key.split(":", 1)[1]
        store.Index(app.paths).drop(gone)           # the child removes it from the index
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert app.marked == set()
        assert msg(app) == "同步完成：移除 1；1 個勾選的列已不在"
    run_app(app, body)


# --- C-15: no redraw on top of a box or while a box is being typed in --------------------------

def test_c15_a_box_on_top_keeps_the_new_list_back_until_it_closes(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 0.01)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.push_screen(tui.Tell("標題", "一句話"))
        await pilot.pause()
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        assert app.query_one("#table").row_count == 2
        await pilot.press("enter")                  # the box goes; the retry takes the new list
        assert await until(lambda: app._pending_rows is None, pilot)
        assert app.query_one("#table").row_count == 3
    run_app(app, body)


def test_c15_hide_preview_search_applies_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.open_preview_search()
        await pilot.pause()
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        app.set_focus(app.query_one("#table"))
        app.hide_preview_search()
        await pilot.pause()
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
    run_app(app, body)


def test_c15_close_preview_search_applies_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.open_preview_search()
        await pilot.pause()
        assert app.focused.id == "search" and app.search_open is True
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        app.close_preview_search()
        await pilot.pause()
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
        assert app.search_open is False
    run_app(app, body)


def test_esc_in_the_preview_search_box_closes_it_and_gives_the_focus_back_to_the_pane():
    app, _ = make_app()

    async def body(pilot):
        app.open_preview_search()
        await pilot.pause()
        assert app.focused.id == "search" and app.search_open is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.search_open is False
        assert app.focused.id == "right"
    run_app(app, body)


def test_esc_in_the_preview_search_box_applies_a_waiting_list_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.open_preview_search()
        await pilot.pause()
        assert app.focused.id == "search" and app.search_open is True
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert app.search_open is False
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
    run_app(app, body)


def test_c15_toggle_focus_applies_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("slash")
        await pilot.pause()
        assert app.focused.id == "filter"
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        assert app.query_one("#table").row_count == 2
        await pilot.press("tab")
        await pilot.pause()
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
        assert app.focused.id != "filter"
    run_app(app, body)


def test_c15_filter_done_applies_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("slash")
        await pilot.pause()
        assert app.focused.id == "filter"
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        assert app.query_one("#table").row_count == 2
        await pilot.press("enter")
        await pilot.pause()
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
    run_app(app, body)


def test_c15_escape_filter_applies_immediately(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 3600)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("slash")
        await pilot.pause()
        assert app.focused.id == "filter"
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        assert app.query_one("#table").row_count == 2
        await pilot.press("escape")
        await pilot.pause()
        assert app._pending_rows is None
        assert app.query_one("#table").row_count == 3
    run_app(app, body)


def test_c15_the_filter_text_survives_the_new_list(monkeypatch):
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.search("表格")
        await pilot.pause()
        _index(session(C, "表格新的", "## user\n別的\n"))
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert app.text == "表格"
        assert app.query_one("#table").row_count == 1      # the filter still applies to the new list
    run_app(app, body)


# --- C-16: the preview is read again only when the row or its file changed ----------------------

def test_c16_same_row_same_file_leaves_the_preview_alone(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 0.01)
    monkeypatch.setattr(tui, "PREVIEW_IDLE", 0.01)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.shown_preview is not None, pilot)
        before = app.shown_preview
        puts: list = []
        real_put = app.put_preview
        monkeypatch.setattr(app, "put_preview", lambda p: (puts.append(p), real_put(p)))
        _index(session(C, "第三個"))
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        await asyncio.sleep(tui.PREVIEW_IDLE * 4)      # a read that would come has come by now (negative check)
        await pilot.pause()
        assert puts == [], "the same session in the same file must not be read again"
        assert app.shown_preview is before
    run_app(app, body)


def test_c16_only_the_cloud_column_changed_does_not_read_the_preview_again(monkeypatch):
    monkeypatch.setattr(tui, "PREVIEW_IDLE", 0.01)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.shown_preview is not None, pilot)
        before = app.shown_preview
        ulid = app.current().key.split(":", 1)[1]
        puts: list = []
        real_put = app.put_preview
        monkeypatch.setattr(app, "put_preview", lambda p: (puts.append(p), real_put(p)))
        store.Index(app.paths).mark_missing([ulid])   # Drive no longer has it: the row's cloud cell changes
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        await asyncio.sleep(tui.PREVIEW_IDLE * 4)      # negative check: give a read the time it would take
        await pilot.pause()
        assert puts == [] and app.shown_preview is before
        assert msg(app).startswith("同步完成：更新 1")
    run_app(app, body)


def test_c16_import_tab_row_changed_reads_the_preview_again(monkeypatch):
    monkeypatch.setattr(tui, "PREVIEW_IDLE", 0.01)
    agent = _claude_agent()
    app, _ = make_app(agents=[agent])

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("]")
        assert await until(lambda: app.tab == "import", pilot)
        assert await until(lambda: app.shown_preview is not None, pilot)

        app._listed["claude"] = [Listed("s1", "/tmp/p", "未匯入的", "2026-10-03T00:00:00Z")]

        puts: list = []
        real_put = app.put_preview
        put_done = asyncio.Event()
        monkeypatch.setattr(app, "put_preview", lambda p: (puts.append(p), put_done.set(), real_put(p)))

        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        await asyncio.wait_for(put_done.wait(), 5)
        assert len(puts) >= 1
    run_app(app, body)


def test_c16_a_changed_file_is_read_again_and_says_so_when_searched(monkeypatch):
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.shown_preview is not None, pilot)
        ulid = app.current().key.split(":", 1)[1]
        app.run_preview_search("表格")
        await pilot.pause()
        session_md = app.paths.mirror / ulid / "session.md"
        session_md.write_text(session_md.read_text(encoding="utf-8") + "\n## user\n又一句\n", encoding="utf-8")
        puts: list = []
        real_put = app.put_preview
        monkeypatch.setattr(app, "put_preview", lambda p: (puts.append(p), real_put(p)))
        put_done = asyncio.Event()
        monkeypatch.setattr(app, "put_preview", lambda p: (puts.append(p), put_done.set(), real_put(p)))
        _index(session(D, "第四個"))                  # the list must change for the new list to be shown at all
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        await asyncio.wait_for(put_done.wait(), 5)        # the preview comes after its quiet period
        await pilot.pause()                               # and the search that goes with it runs after the refresh
        assert "內容已更新" in str(app.query_one("#count", Static).render())
    run_app(app, body)


# --- C-17: content search keeps its matches, without searching the agents again ----------------

def test_c17_content_matches_are_recounted_without_a_new_agent_scan(monkeypatch):
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.content = True
        app.search("表格")
        await pilot.pause()
        scans: list = []
        monkeypatch.setattr(app, "scan_agents", lambda text: scans.append(text))
        monkeypatch.setattr(app, "find_in_agents", lambda text: scans.append(text))
        _index(session(C, "第三個", "## user\n表格新的\n"))
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert f"agora:{C}" in app.matches["agora"]
        assert scans == []
    run_app(app, body)


# --- C-18: the write actions wait for the sync -------------------------------------------------

def _claude_agent():
    return FakeAgent("claude", [Listed("s1", "/tmp/p", "未匯入的", "2026-10-02T00:00:00Z")],
                     texts={"s1": ["問", "答"]})


GATED = [
    pytest.param([], "d", False, id="agora-d"),
    pytest.param([], "m", True, id="agora-m"),
    pytest.param([], "e", False, id="agora-e"),
    pytest.param([], "p", False, id="agora-p"),
    pytest.param([], "P", False, id="agora-P"),
    pytest.param([], "enter", False, id="agora-enter"),
    pytest.param(["]"], "enter", False, id="import-enter"),
    pytest.param(["]"], "p", False, id="import-p"),
]


@pytest.mark.parametrize("prefix, key, two_marked", GATED)
def test_c18_each_write_action_waits_and_esc_starts_nothing(prefix, key, two_marked):
    app, cli = make_app(agents=[_claude_agent()])

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        for k in prefix:
            await pilot.press(k)
        if two_marked:
            await pilot.press("space", "down", "space")
        marked = set(app.marked)
        await pilot.press(key)
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot), f"{key} did not wait"
        assert app._last_spawned == [] and cli.calls == []
        await pilot.press("escape")
        assert await until(lambda: not isinstance(app.screen, tui.WaitSync), pilot)
        assert set(app.marked) == marked                     # the marks stay as they were
        assert app.proc.signals == [] and app.sync.running   # the sync goes on
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        await pilot.pause()
        assert app._last_spawned == [] and cli.calls == []   # Esc means: nothing starts later either
    run_app(app, body)


def test_c18_after_the_sync_the_action_goes_on_with_the_new_list():
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        app.proc.release(DONE_LINES)
        assert await until(lambda: isinstance(app.screen, tui.Choose), pilot), "the question never came"
        assert app.sync_started == [1]
    run_app(app, body)


def test_c18_the_wait_title_says_throttled_when_no_indicator_is_shown(monkeypatch):
    monkeypatch.setattr(store, "sync_due", lambda paths: False)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        assert "正在整理本機索引" in str(app.screen.query_one(".box-title", Static).render())
        await pilot.press("escape")
    run_app(app, body)


def test_c18_the_wait_title_says_the_sync_when_the_indicator_is_shown(monkeypatch):
    monkeypatch.setattr(store, "sync_due", lambda paths: True)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        assert "背景同步還沒完成" in str(app.screen.query_one(".box-title", Static).render())
        await pilot.press("escape")
    run_app(app, body)


def test_c18_a_row_gone_after_the_sync_stops_the_action_and_says_so():
    app, cli = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        gone = app.current().key.split(":", 1)[1]
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        store.Index(app.paths).drop(gone)
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert await until(lambda: not isinstance(app.screen, tui.WaitSync), pilot)
        assert "同步後清單變了" in msg(app) and msg_failed(app)
        assert app._last_spawned == [] and app.screen is app.default_screen
    run_app(app, body)


def test_c18_the_row_under_the_cursor_is_acted_on_not_the_marked_ones(monkeypatch):
    app, _ = make_app(sessions=three_sessions())
    keys_seen: list = []
    real_gate = app.gate

    async def recording_gate(keys):
        keys_seen.append(list(keys))
        return await real_gate(keys)
    monkeypatch.setattr(app, "gate", recording_gate)

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        first, second, third = [r.key for r in app.shown()]
        await pilot.press("space", "down", "space", "down")     # two marked, the cursor on the third
        cursor = app.current().key
        assert cursor == third
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        app.proc.release(DONE_LINES)
        assert await until(lambda: not isinstance(app.screen, tui.WaitSync), pilot)
        assert keys_seen == [[cursor]]
        assert app.marked == {first, second}
        await pilot.press("escape")                          # the continue question
    run_app(app, body)


def test_c18_the_cursor_row_gone_stops_even_while_the_marked_rows_stay(monkeypatch):
    app, _ = make_app(sessions=three_sessions())

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        await pilot.press("space", "down", "space", "down")
        cursor_ulid = app.current().key.split(":", 1)[1]
        await pilot.press("e")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot)
        store.Index(app.paths).drop(cursor_ulid)
        app.proc.release(DONE_LINES)
        assert await until(lambda: not isinstance(app.screen, tui.WaitSync), pilot)
        assert "同步後清單變了" in msg(app)
        assert app._last_spawned == []
    run_app(app, body)


def test_c19_without_a_sync_in_progress_nothing_waits(monkeypatch):
    app, _ = make_app(sync_on_start=False)

    async def body(pilot):
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert not isinstance(app.screen, tui.WaitSync)
        assert isinstance(app.screen, tui.Choose)
        await pilot.press("escape")
    run_app(app, body)


# --- C-20: leaving does not wait for the sync, and sends it nothing ----------------------------

def test_c20_quitting_during_the_sync_signals_nothing_and_keeps_no_group():
    app, _ = make_app()

    async def go():
        try:
            async with app.run_test(size=(120, 30)) as pilot:
                assert await until(lambda: app.sync.running, pilot)
                await pilot.press("q")
                await pilot.pause()
        finally:
            app.proc.release()
    asyncio.run(go())
    assert app.proc.signals == []
    assert app._groups == set()


# --- C-20′: the child is done but the list is still being built -----------------------------------

def test_c20_prime_a_key_while_the_list_is_being_built_waits_for_it(monkeypatch):
    gate = threading.Event()
    real_build = tui.build_rows

    def slow_build(*args, **kwargs):
        gate.wait(timeout=10)
        return real_build(*args, **kwargs)
    monkeypatch.setattr(tui, "build_rows", slow_build)
    app, cli = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.proc.release(DONE_LINES)
        assert await until(lambda: not app.sync.running and not app._sync_idle.is_set(), pilot)
        await pilot.press("d")
        assert await until(lambda: isinstance(app.screen, tui.WaitSync), pilot), \
            "a key pressed while the list is being built must wait"
        assert app.sync_started == [1] and app._last_spawned == []
        gate.set()
        assert await until(lambda: isinstance(app.screen, tui.Choose), pilot)
        await pilot.press("escape")
    try:
        run_app(app, body)
    finally:
        gate.set()


# --- C-21: the first sync, with nothing on the list yet -------------------------------------------

def test_c21_the_first_sync_says_the_list_comes_later_then_the_empty_hint_if_still_empty():
    app, _ = make_app(sessions=())

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        assert msg(app) == "正在同步 Drive，清單稍後出現"
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert msg(app) == tui.EMPTY_HINT
    run_app(app, body)


def test_c21_the_first_sync_that_brings_sessions_says_what_it_brought():
    app, _ = make_app(sessions=())

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        _index(session(A, "第一個"))
        app.proc.release(DONE_LINES)
        await settled(app, pilot)
        assert msg(app) == "同步完成：新增 1"
    run_app(app, body)


# --- the contract of the finally in `_sync_rows_ready` and the no-list path -----------------------

def test_the_gate_is_opened_whatever_way_the_list_ends(monkeypatch):
    app, _ = make_app(sync_on_start=False)

    async def body(pilot):
        await pilot.pause()
        app._sync_idle.clear()
        app.sync_settled.clear()
        app._sync_rows_ready(None)                         # the list cannot be read
        assert app._sync_idle.is_set() and "讀不到本機清單" in msg(app)
        app._sync_idle.clear()
        def boom():
            raise RuntimeError("boom")
        monkeypatch.setattr(app, "_try_apply_sync", boom)
        with pytest.raises(RuntimeError, match="boom"):
            app._sync_rows_ready({"agora": [], "import": []})
        assert app._sync_idle.is_set(), "a list that fails to show must still open the gate (finally)"
        app._pending_rows = None
    run_app(app, body)


def test_reload_drops_a_list_that_has_not_been_shown(monkeypatch):
    monkeypatch.setattr(tui, "SYNC_APPLY_RETRY", 0.01)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.push_screen(tui.Tell("標題", "一句話"))
        await pilot.pause()
        _index(session(D, "第四個"))
        app.proc.release(DONE_LINES)
        assert await until(lambda: app._pending_rows is not None, pilot)
        app.reload()
        assert app._pending_rows is None
    run_app(app, body)


def test_compute_sync_rows_failure_settles_and_opens_gate(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(tui, "build_rows", boom)
    app, _ = make_app()

    async def body(pilot):
        assert await until(lambda: app.sync.running, pilot)
        app.proc.release(DONE_LINES)
        await asyncio.wait_for(app.sync_settled.wait(), 5)
        assert "讀不到本機清單" in msg(app)
        await pilot.press("d")
        await pilot.pause()
        assert not any(isinstance(s, tui.WaitSync) for s in app.screen_stack)
    run_app(app, body)


def test_wait_sync_bindings_only_escape():
    assert [b.key for b in tui.WaitSync.BINDINGS] == ["escape"]

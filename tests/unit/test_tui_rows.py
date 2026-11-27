"""#491 PR-2 (design §6.2, contracts C-8 to C-11): the list as plain functions, and `show` with a view."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import threading

from agora import store, tui
from agora.agents.base import Listed
from test_tui import FakeAgent, _app, _index, _run


class CountingAgent(FakeAgent):
    def __init__(self, name, listed):
        super().__init__(name, listed)
        self.calls = 0

    def list_sessions(self):
        self.calls += 1
        return super().list_sessions()


class BrokenAgent:
    name = "broken"

    def list_sessions(self):
        raise RuntimeError("nope")


def _row(key: str, text: str = "") -> tui.Row:
    return tui.Row(key, ["c"] * len(tui.COLUMNS["agora"]), text or key)


# --- C-8: list_agent_sessions and import_rows(listed=…) ----------------------

def test_c8_one_agent_failing_leaves_the_other_list(capsys):
    good = FakeAgent("claude", [Listed("s1", "/tmp/p", "標題", "2026-10-02T03:04:00Z")])
    listed = tui.list_agent_sessions([BrokenAgent(), good])
    assert listed == {"broken": [], "claude": good.listed}
    assert "[agora] broken 的 session 清單讀不到：nope" in capsys.readouterr().err


def test_c8_import_rows_with_listed_matches_and_asks_no_agent():
    _, index = _index()
    agent = CountingAgent("claude", [Listed("s1", "/tmp/p", "標題", "2026-10-02T03:04:00Z")])
    listed = tui.list_agent_sessions([agent])
    assert agent.calls == 1
    with_listed = tui.import_rows(index, [agent], None, listed)
    assert agent.calls == 1                                   # given: no second call
    without = tui.import_rows(index, [agent])
    assert agent.calls == 2
    assert with_listed == without and [r.key for r in with_listed] == ["claude:s1"]


# --- C-9: build_rows works on any thread ---------------------------------------

def test_c9_build_rows_on_another_thread_equals_main_thread():
    app, _cli = _app([FakeAgent("claude", [Listed("s1", "/tmp/p", "標題", "2026-10-02T03:04:00Z")])])
    paths, agents = app.paths, app.agents
    listed = tui.list_agent_sessions(agents)
    got, errors = {}, []

    def worker():
        try:
            got.update(tui.build_rows(paths, listed, agents))
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive() and errors == []
    index = store.Index(paths)
    assert got == {"agora": tui.agora_rows(index, [], paths),
                   "import": tui.import_rows(index, agents, paths, listed)}
    assert tui.agora_rows(app.index, [], paths) == got["agora"]   # the main thread's index still answers


# --- C-10: row_changes and sync_note ------------------------------------------

def test_c10_row_changes_each_kind_and_none():
    old = [_row("a", "1"), _row("b", "1"), _row("c", "1")]
    new = [_row("b", "2"), _row("c", "1"), _row("d", "1")]
    assert tui.row_changes(old, new) == tui.RowChanges(added=1, changed=1, removed=1)
    assert tui.row_changes(old, [_row("x")]) == tui.RowChanges(added=1, changed=0, removed=3)
    assert tui.row_changes(old, list(old)) == tui.RowChanges(0, 0, 0)


def test_c10_sync_note_exact_wording():
    assert tui.sync_note(tui.RowChanges(2, 1, 0), 0, done=True) == "同步完成：新增 2、更新 1"
    assert tui.sync_note(tui.RowChanges(0, 0, 0), 1, done=True) == "同步完成；1 個勾選的列已不在"
    assert tui.sync_note(tui.RowChanges(0, 0, 0), 0, done=True) == ""
    assert tui.sync_note(tui.RowChanges(0, 0, 0), 0, done=False) == ""
    assert tui.sync_note(tui.RowChanges(0, 0, 0), 0, done=False, error="X") == ""
    assert tui.sync_note(tui.RowChanges(1, 0, 0), 0, done=False) == "本機清單有更新：新增 1"
    assert tui.sync_note(tui.RowChanges(1, 0, 0), 0, done=False, error="X") == "X（本機清單有更新：新增 1）"
    assert tui.sync_note(tui.RowChanges(2, 1, 3), 0, done=True) == "同步完成：新增 2、更新 1、移除 3"


# --- C-11: show(view=…), show(preview=False), rows_by_keys ---------------------

def _sixty_rows() -> list[tui.Row]:
    return [_row(f"agora:{n:03d}") for n in range(60)]


def test_c11_show_view_keeps_key_and_screen_line_when_a_row_is_added_in_front():
    async def go():
        app, _cli = _app([])
        async with app.run_test(size=(120, 30)) as pilot:
            app.rows["agora"] = _sixty_rows()
            app.show()
            await pilot.pause()
            table = app.query_one("#table")
            table.move_cursor(row=39, scroll=False)
            table.scroll_to(y=25, animate=False)             # the cursor sits on screen line 14, not at the edge
            await pilot.pause()
            view = app.capture_view()
            line = table.cursor_row - table.scroll_y
            assert line == 14
            app.rows["agora"] = [_row("agora:new")] + _sixty_rows()
            app.show(view=view)
            await pilot.pause()
            assert table.cursor_row == 40
            assert app.current().key == "agora:039"
            assert table.cursor_row - table.scroll_y == line
    _run(go)


def test_c11_show_view_key_gone_stays_on_same_number():
    async def go():
        app, _cli = _app([])
        async with app.run_test(size=(120, 30)) as pilot:
            app.rows["agora"] = _sixty_rows()
            app.show()
            await pilot.pause()
            table = app.query_one("#table")
            table.move_cursor(row=39)
            await pilot.pause()
            view = app.capture_view()
            app.rows["agora"] = [r for r in _sixty_rows() if r.key != "agora:039"]
            app.show(view=view)
            await pilot.pause()
            assert table.cursor_row == 39
    _run(go)


def test_c11_show_view_list_shrinks_to_last_row():
    async def go():
        app, _cli = _app([])
        async with app.run_test(size=(120, 30)) as pilot:
            app.rows["agora"] = _sixty_rows()
            app.show()
            await pilot.pause()
            table = app.query_one("#table")
            table.move_cursor(row=39)
            await pilot.pause()
            view = app.capture_view()
            app.rows["agora"] = _sixty_rows()[:10]
            app.show(view=view)
            await pilot.pause()
            assert table.cursor_row == 9                     # min(39, 10 - 1): the last of ten rows
    _run(go)


def test_c11_show_without_preview_does_not_call_preview(monkeypatch):
    app, _cli = _app([])
    calls = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            app.rows["agora"] = _sixty_rows()
            monkeypatch.setattr(app, "preview", lambda: calls.append(1))
            app.show(preview=False)
            await pilot.pause()
            assert calls == []
            app.show()
            assert calls == [1]
    _run(go)


def test_c11_quiet_redraw_clears_on_move_so_the_next_move_previews_again(monkeypatch):
    app, _cli = _app([])
    calls = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            app.rows["agora"] = _sixty_rows()
            monkeypatch.setattr(app, "preview", lambda: calls.append(app.current().key))
            app.show(preview=False)
            await pilot.pause()
            table = app.query_one("#table")
            table.move_cursor(row=1)
            await pilot.pause()
            table.move_cursor(row=0)
            await pilot.pause()
            assert calls == ["agora:001", "agora:000"]
    _run(go)


def test_c11_rows_by_keys_screen_order_missing_and_filtered():
    app, _cli = _app([])
    app.rows["agora"] = [_row("agora:a", "甲"), _row("agora:b", "乙"), _row("agora:c", "甲")]
    assert [r.key for r in app.rows_by_keys(["agora:c", "agora:a"])] == ["agora:a", "agora:c"]
    assert app.rows_by_keys(["agora:a", "agora:zz"]) is None
    app.text = "甲"                                          # the title filter leaves out "agora:b"
    assert app.rows_by_keys(["agora:b"]) is None
    assert [r.key for r in app.rows_by_keys(["agora:a"])] == ["agora:a"]


def test_c9_build_rows_opens_its_own_index_on_its_own_thread(monkeypatch):
    app, _cli = _app([])
    paths = app.paths
    opened, ran_on = [], []
    real = store.Index

    def spy(p):
        index = real(p)
        opened.append(index)
        ran_on.append(threading.get_ident())
        return index
    monkeypatch.setattr(tui.store, "Index", spy)
    thread = threading.Thread(target=lambda: tui.build_rows(paths, {}, []))
    thread.start()
    thread.join(timeout=30)
    assert len(opened) == 1 and opened[0] is not app.index
    assert ran_on == [thread.ident]

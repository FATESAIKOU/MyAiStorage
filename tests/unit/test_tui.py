"""The interactive mode (design 5.9): its plain parts, and the Textual screen driven by key presses."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import asyncio
import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest
from textual.widget import Widget
from textual.widgets import Input, Static

from agora import header as h
from agora import cache, store, tui
from agora.agents.base import Exported, Listed


def _hdr(ulid: str, title: str, relation: str = "import", agent: str | None = "opencode", sid: str = "ses_a") -> dict:
    agora = {"header": 2, "created_at": "2026-10-02T00:00:00Z", "updated_at": "2026-10-02T00:00:00Z",
             "relation": relation, "parents": []}
    if agent:
        agora["source"] = {"agent": agent, "session_id": sid, "dir": "/tmp/p", "created_at": "2026-10-01T00:00:00Z"}
    return {"type": "Session", "title": title, "tags": ["驗收"], "id": f"agora:{ulid}", "refs": [], "case": None,
            "agora": agora}


def _index(*sessions: tuple[dict, str]) -> tuple[store.Paths, store.Index]:
    paths = store.Paths.from_env()
    index = store.Index(paths)
    for hdr, body in sessions:
        ulid = hdr["id"].split(":")[1]
        (paths.mirror / ulid).mkdir(parents=True, exist_ok=True)
        (paths.mirror / ulid / "session.md").write_text(h.dump_document(hdr, body), encoding="utf-8")
        index.put(ulid, "md5", hdr, body)
    return paths, index


class FakeAgent:
    def __init__(self, name, listed, last=None, texts=None):
        self.name, self.listed, self.last, self.texts = name, listed, last, texts or {}

    def list_sessions(self):
        return self.listed

    def last_message(self, session_id):
        if self.last == "boom":
            raise RuntimeError("unreadable")
        return self.last

    def export(self, session_id):
        msgs = self.texts.get(session_id, ["問", "答"])
        return Exported(session_id=session_id, raw=json.dumps({"m": msgs}, ensure_ascii=False).encode())

    def turns(self, raw):
        msgs = json.loads(raw)["m"]
        return [("user" if i % 2 == 0 else "assistant", [m]) for i, m in enumerate(msgs)]

    def search_text(self, keyword, only=None):
        self.searched = only
        yield from (sid for sid, msgs in self.texts.items()
                    if (only is None or sid in only) and any(keyword in m for m in msgs))


# --- the plain parts -----------------------------------------------------------

def test_rows_carry_their_last_update():  # feedback 10
    _, index = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "一"), "## user\nx\n"))
    row = tui.agora_rows(index, [])[0]
    assert row.cells[:3] == ["AAAAAAAA", "一", "opencode"] and row.cells[3]
    agent = FakeAgent("claude", [Listed("s1", "/tmp/p", "標題", "2026-10-02T03:04:00Z")])
    cells = tui.import_rows(index, [agent])[0].cells
    assert cells[1:3] == ["標題", "claude"] and cells[3].endswith(":04") and cells[4] == "/tmp/p"


def test_import_tab_lists_only_sessions_not_in_agora():
    _, index = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "已匯入", sid="ses_in"), "## user\nx\n"))
    agent = FakeAgent("opencode", [Listed("ses_in", "/tmp/p", "已匯入", "2026-10-01T00:00:00Z"),
                                   Listed("ses_new", "/tmp/q", "新的", "2026-10-02T00:00:00Z"),
                                   Listed("ses_old", None, None, "2026-09-01T00:00:00Z")])
    assert [r.key for r in tui.import_rows(index, [agent])] == ["opencode:ses_new", "opencode:ses_old"]


def test_import_tab_leaves_out_agoras_own_copies():  # review T5
    paths, index = _index()
    paths.state.mkdir(parents=True, exist_ok=True)
    (paths.state / "unsaved-launches").write_text("opencode:ses_copy\n")
    agent = FakeAgent("opencode", [Listed("ses_copy", "/tmp/p", "複本", None),
                                   Listed("ses_sum", str(paths.state / "summarize"), "要約用", None),
                                   Listed("ses_real", "/tmp/p", "真的", None)])
    assert [r.key for r in tui.import_rows(index, [agent], paths)] == ["opencode:ses_real"]


def test_a_session_talked_to_after_its_import_comes_back_marked():
    _, index = _index((_hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "舊的", sid="ses_old"), "## user\nx\n"),
                      (_hdr("01EEEEEEEEEEEEEEEEEEEEEEEE", "沒動", sid="ses_same"), "## user\nx\n"))
    agent = FakeAgent("opencode", [Listed("ses_old", "/tmp/p", "舊的", "2026-10-03T00:00:00Z"),
                                   Listed("ses_same", "/tmp/p", "沒動", "2026-10-01T00:00:00Z")])
    rows = tui.import_rows(index, [agent])
    assert [(r.key, r.cells[1]) for r in rows] == [("opencode:ses_old", "↻ 舊的")]


def test_a_failing_adapter_leaves_the_others_list(capsys):  # review U1
    _, index = _index()

    class Broken(FakeAgent):
        def list_sessions(self):
            raise ValueError("odd file")
    rows = tui.import_rows(index, [Broken("claude", []), FakeAgent("opencode", [Listed("ses_ok", None, "好", None)])])
    assert [r.key for r in rows] == ["opencode:ses_ok"] and "claude 的 session 清單讀不到" in capsys.readouterr().err


def test_filter_by_words_or_by_content_matches():
    rows = [tui.Row(f"agora:{n}", [n], f"{n} 表格" if n != "c" else n) for n in "abc"]
    assert [r.key for r in tui.filtered(rows, "表格")] == ["agora:a", "agora:b"]
    assert [r.key for r in tui.filtered(rows, "", {"agora:c"})] == ["agora:c"]


def test_previews_show_what_was_read_and_never_fail():
    normal = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "一般")
    paths, index = _index((normal, "## user\n第一句\n\n## assistant\n最後的回答\n"))
    preview = tui.agora_preview(paths, index, normal["id"])
    assert "dir /tmp/p" in preview.pinned and "tags 驗收" in preview.pinned
    assert preview.text.endswith("最後的回答") and "第一句" in preview.text
    assert not preview.text.startswith("---"), "the front matter is not conversation"
    assert preview.more() is False and preview.hint() == ""      # a short file is all here
    one = tui.import_preview(FakeAgent("claude", [], ("assistant", "好")), "s")
    assert one.pinned.startswith("最後一則（assistant）") and one.text == "## assistant\n好"
    assert not one.more() and one.hint() == "", "no file, so there is nothing above it"
    assert tui.import_preview(FakeAgent("claude", [], None), "s").text == ""
    broken = tui.import_preview(FakeAgent("claude", [], "boom"), "s")
    assert broken.text == "" and broken.hint() == ""
    whole = tui.import_preview(FakeAgent("claude", [], texts={"s": ["問題", "回答"]}), "s", full=True)
    assert "問題" in whole.text and whole.text.endswith("回答")


def test_actions_and_what_they_run():
    rows = [tui.Row("agora:a", ["a"], "a"), tui.Row("agora:b", ["b"], "b")]
    assert tui.argv_for("merge", rows, "claude", None) == [["merge", "session", "agora:a", "agora:b", "--agent", "claude"]]
    assert tui.argv_for("delete", rows, None, None) == [["delete", "session", "agora:a", "agora:b", "--yes"]]
    assert tui.argv_for("continue", rows[:1], "opencode", "/w") == [
        ["continue", "session", "agora:a", "--agent", "opencode", "--dir", "/w"]]
    # one command for all of that agent's sessions, not one per row (T1 R2)
    imp = [tui.Row("opencode:ses_x", ["x"], "x", "opencode"), tui.Row("opencode:ses_y", ["y"], "y", "opencode")]
    assert tui.argv_for("import", imp, None, None) == [
        ["import", "session", "--agent", "opencode",
         "--external-session-id", "ses_x", "--external-session-id", "ses_y"]]


def test_setup_asks_for_rclone_then_for_authorization(monkeypatch, tmp_path):
    paths = store.Paths(config=tmp_path / "config", cache=tmp_path / "cache", state=tmp_path / "state")
    monkeypatch.setenv("AGORA_RCLONE", "/nonexistent/rclone")
    assert tui.setup_needed(paths) == "rclone"
    monkeypatch.setenv("AGORA_RCLONE", "/bin/sh")
    assert tui.setup_needed(paths) == "auth"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rclone.conf").write_text("[gdrive]\n")
    assert tui.setup_needed(paths) is None


def test_no_interactive_mode_without_a_terminal(monkeypatch, capsys):  # review T1
    from agora import cli
    called = []
    monkeypatch.setattr(tui, "main", lambda paths: called.append(1) or 0)
    assert cli.main([]) == cli.EXIT_ERROR and not called       # pytest's stdin/stdout are not a TTY
    assert "互動模式只在終端機裡開" in capsys.readouterr().err


# --- the screen, driven by key presses (no real agent, no Drive) ---------------

class FakeCli:
    AGENTS = ("opencode", "claude")

    def __init__(self):
        self.calls = []

    def main(self, argv):
        self.calls.append(argv)
        print(f"agora:01FAKE {' '.join(argv[:2])}")
        return 0


class FakeProc:
    """What AgoraApp.spawn hands back: its output, and signals it was sent."""

    pid = 4242                # a group of its own, so the real signalling path runs

    def __init__(self, lines=(), code=0, hang=False):
        self.stdout = iter(f"{line}\n" for line in lines)
        self.code, self.hang, self.signals = code, hang, []
        self.done = threading.Event()
        if not hang:
            self.done.set()

    def poll(self):
        return None if self.hang and not self.done.is_set() else self.code

    def wait(self):
        self.done.wait(timeout=10)
        return self.code

    def send_signal(self, sig):
        self.signals.append(int(sig))
        self.done.set()


class DripProc(FakeProc):
    """A process whose lines arrive one at a time, when the test opens the gate.

    One that prints everything at once cannot show that the bar *walks* - only that
    it ends up somewhere.
    """

    def __init__(self, lines):
        super().__init__(hang=True)
        self.gates = [threading.Event() for _ in lines]

        def arriving():
            for line, gate in zip(lines, self.gates):
                gate.wait(timeout=10)
                yield f"{line}\n"
        self.stdout = arriving()

    def send(self, n: int) -> None:
        """Let the n-th line out."""
        self.gates[n].set()


def _spawn(proc=None, **kw):
    """A spawn that records what it was asked for and runs a fake process."""
    started: list[list[str]] = []

    def spawn(argv):
        started.append(argv)
        return proc if proc is not None else FakeProc(**kw)
    return spawn, started


def _app(agents):
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "第一個", sid="ses_1"), "## user\n表格的問題\n"),
                      (_hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "第二個", sid="ses_2"), "## user\n別的\n"))
    cli = FakeCli()
    app = tui.AgoraApp(paths, cli, agents=agents, check_setup=False)
    app.spawn, started = _spawn()    # no action reaches a real process in a test
    app._last_spawned = started      # what the screen asked the command mode to run
    return app, cli


def _run(test):
    asyncio.run(test())


def test_screen_lists_with_column_names_and_switches_tabs():  # feedback 8
    agent = FakeAgent("claude", [Listed("s1", "/tmp/p", "未匯入的", "2026-10-02T00:00:00Z")], texts={"s1": ["問", "答"]})
    app, _ = _app([agent])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            table = app.query_one("#table")
            assert [str(c.label) for c in table.columns.values()][2:] == list(tui.COLUMNS["agora"])
            assert table.row_count == 2
            await pilot.press("]")
            await pilot.pause()
            assert app.tab == "import" and table.row_count == 1
            assert [str(c.label) for c in table.columns.values()][2:] == list(tui.COLUMNS["import"])
    _run(go)


def test_shift_tab_moves_focus_between_list_and_preview():  # feedback 6
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 20)) as pilot:
            await pilot.pause()
            assert app.focused is app.query_one("#table")
            await pilot.press("shift+tab")
            assert app.focused is app.query_one("#right")
            assert not app.check_action("delete", ())      # list keys are off in the preview
            await pilot.press("shift+tab")
            assert app.focused is app.query_one("#table")
    _run(go)


def test_mark_two_then_merge_runs_one_command_in_a_child_process():
    """T2 1.1: the action is a command-mode command in a child process of its own."""
    app, _ = _app([])
    spawn, started = _spawn()
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space")
            assert app.query_one("#table").cursor_row == 0         # marking does not move the cursor
            await pilot.press("down", "space", "m")
            await pilot.pause()
            order = [r.key for r in app.rows["agora"] if r.key in app.marked]
            await pilot.press("enter")                     # opencode writes the summaries
            for _ in range(60):
                await pilot.pause(0.05)
                if started:
                    break
            assert started == [["merge", "session", *order, "--agent", "opencode"]]
    _run(go)


def test_delete_starts_on_cancel():
    app, _ = _app([])
    spawn, started = _spawn()
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")                     # the first option is 取消
            await pilot.pause()
            assert started == []
    _run(go)


def test_content_search_finds_in_agora_and_streams_in_the_import_tab():  # feedback 9
    agent = FakeAgent("claude", [Listed("s1", "/tmp/p", "甲", None), Listed("s2", "/tmp/p", "乙", None)],
                      texts={"s1": ["沒有"], "s2": ["表格在這裡"]})
    app, _ = _app([agent])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t", "slash")
            for ch in "表格":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert [r.key for r in app.shown()] == ["agora:01AAAAAAAAAAAAAAAAAAAAAAAA"]   # its body has 表格
            await pilot.press("]")
            await pilot.pause(0.2)
            assert [r.key for r in app.shown()] == ["claude:s2"]
    _run(go)


def test_content_search_scans_only_what_the_cache_lacks():
    from agora import cache
    agent = FakeAgent("claude", [Listed("s1", "/tmp/p", "甲", None), Listed("s2", "/tmp/p", "乙", None)],
                      texts={"s1": ["表格在快取裡"], "s2": ["表格不在快取裡"]})
    app, _ = _app([agent])
    cache.local_reading(app.paths, agent, "s1")                   # s1 is cached, s2 is not

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]", "ctrl+t", "slash")
            for ch in "表格":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert {r.key for r in app.shown()} == {"claude:s1", "claude:s2"}
            assert agent.searched == {"s2"}                         # the slow scan skipped s1
    _run(go)


def test_import_tab_leaves_out_agent_sessions_a_continue_moved_past():
    hdr = _hdr("01FFFFFFFFFFFFFFFFFFFFFFFF", "接續過的", sid="ses_new")
    hdr["agora"]["previous_sources"] = ["opencode:ses_old"]
    _, index = _index((hdr, "## user\nx\n"))
    agent = FakeAgent("opencode", [Listed("ses_old", "/tmp/p", "舊的", None), Listed("ses_other", "/tmp/p", "別的", None)])
    assert [r.key for r in tui.import_rows(index, [agent])] == ["opencode:ses_other"]



def test_delete_takes_every_marked_row():
    app, _ = _app([])
    spawn, started = _spawn()
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "d")
            await pilot.pause()
            order = [r.key for r in app.rows["agora"] if r.key in app.marked]
            await pilot.press("down", "enter")             # 確定
            for _ in range(60):
                await pilot.pause(0.05)
                if started:
                    break
            assert started == [["delete", "session", *order, "--yes"]]
    _run(go)


def test_import_runs_one_command_per_agent():
    """V6: opencode and claude are separate stores, so they are separate commands -
    and each agent's sessions go into one of them."""
    agent = FakeAgent("opencode", [Listed("s1", "/tmp/p", "甲", None), Listed("s2", "/tmp/q", "乙", None)],
                      texts={"s1": ["問"], "s2": ["答"]})
    agent2 = FakeAgent("claude", [Listed("c1", "/tmp/r", "丙", None)], texts={"c1": ["問"]})
    app, _ = _app([agent, agent2])
    spawn, started = _spawn()
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")                       # 未匯入
            await pilot.pause()
            for _ in range(3):
                await pilot.press("space")
                await pilot.press("down")
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if started:
                    break
            await pilot.press("space")                   # close the first result window
            for _ in range(60):
                await pilot.pause(0.05)
                if len(started) >= 2:
                    break
            assert started == [
                ["import", "session", "--agent", "opencode",
                 "--external-session-id", "s1", "--external-session-id", "s2"],
                ["import", "session", "--agent", "claude", "--external-session-id", "c1"]]
    _run(go)


def test_the_progress_bar_follows_the_k_of_n_lines():
    app, _ = _app([])
    spawn, _ = _spawn(lines=["[agora] 刪除 1/2", "[agora] 刪除 2/2"], hang=True)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")             # 確定
            for _ in range(60):
                await pilot.pause(0.05)
                bar = app.screen.query_one("#bar")
                if bar.total:
                    break
            assert (bar.progress, bar.total) == (1, 2)      # 2/2 started, so one is finished
    _run(go)


def test_progress_is_read_only_from_our_own_lines():   # review V8
    """A date, a title or a path can hold digits and a slash; only `[agora] … k/N` counts."""
    app, _ = _app([])
    spawn, _ = _spawn(lines=["下載 3/9 個檔案", "/tmp/2026/10/03", "[agora] 刪除 1/1"], hang=True)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            for _ in range(60):
                await pilot.pause(0.05)
                bar = app.screen.query_one("#bar")
                if bar.total:
                    break
            assert (bar.progress, bar.total) == (0, 1)      # 1/1 means the first started
    _run(go)


def test_a_child_process_cannot_take_the_users_keystrokes():   # review V1
    """start_new_session detaches the child from the terminal, but fd 0 is still the
    terminal Textual is reading. And a buffered stdout would hold the ids back until
    the end."""
    app, _ = _app([])
    seen = {}

    class Popen:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, **kw)

    real = tui.subprocess.Popen
    tui.subprocess.Popen = Popen
    try:
        tui.AgoraApp.spawn(app, ["merge", "session", "agora:a"])   # the real one, not the fake
    finally:
        tui.subprocess.Popen = real
    assert seen["argv"][1:] == ["-m", "agora.cli", "merge", "session", "agora:a"]
    assert seen["stdin"] is tui.subprocess.DEVNULL
    assert seen["start_new_session"] is True and seen["stderr"] is tui.subprocess.STDOUT
    assert seen["env"]["PYTHONUNBUFFERED"] == "1"


# --- review T2-sec1: stopping a command, and stopping it completely -----------


@pytest.fixture
def group_calls(monkeypatch):
    """Watch the real signalling functions instead of signalling anything."""
    sent: list[tuple[int, int]] = []
    alive = {4242: True}
    monkeypatch.setattr(tui, "killpg", lambda pgid, sig: (sent.append((pgid, int(sig))) or alive.get(pgid, False)))
    monkeypatch.setattr(tui, "group_alive", lambda pgid: alive.get(pgid, False))
    return sent, alive


async def _wait(predicate, pilot, tries=80):
    """Give the screen the pauses it needs; `pilot.pause` is what runs the timers."""
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def test_esc_stops_the_group_and_the_window_says_it_was_interrupted(group_calls, monkeypatch):
    """review M4: Esc sends SIGINT, and the interruption is what the screen reports."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 60)      # no escalation in this one
    sent, alive = group_calls
    app, _ = _app([])
    proc = FakeProc(lines=["[agora] 刪除 1/3"], hang=True)
    spawn, _ = _spawn(proc)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            await pilot.press("escape")
            await _wait(lambda: sent, pilot)
            assert sent == [(4242, int(signal.SIGINT))]
            alive[4242] = False                             # the group is gone
            proc.done.set()
            assert _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            await pilot.press("space")                      # close the result window
            assert "重跑同一個動作會接著做" in " ".join(str(app.screen.lines).split())
            await pilot.press("space")                      # close the result window
            await _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            assert str(app.query_one("#msg").render()) == ""      # Q4
    _run(go)


def test_the_escalation_goes_on_after_agora_itself_is_gone(group_calls, monkeypatch):
    """review M1: our child can exit first and leave its agent in the group, so
    'stopped' means killpg(pgid, 0) says there is nobody left - not that we reaped it."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 0.05)
    sent, alive = group_calls
    app, _ = _app([])
    proc = FakeProc(lines=["[agora] 合併 1/2"], hang=True)
    spawn, _ = _spawn(proc)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "m")
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            await pilot.press("escape")
            await _wait(lambda: sent, pilot)
            proc.done.set()                     # agora itself is gone; the agent is not
            proc.code = 130
            await _wait(lambda: len(sent) >= 3, pilot)
            assert [sig for _pgid, sig in sent] == [int(signal.SIGINT),
                                                     int(signal.SIGTERM), int(signal.SIGKILL)]
    _run(go)


def test_the_escalation_stops_when_the_group_is_gone(group_calls, monkeypatch):
    """review M4: a process that ends on SIGINT gets no SIGTERM."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 0.15)
    sent, alive = group_calls
    app, _ = _app([])
    proc = FakeProc(lines=["[agora] 刪除 1/1"], hang=True)
    spawn, _ = _spawn(proc)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            alive[4242] = False                  # nothing left in the group, from the start
            await pilot.press("escape")
            await _wait(lambda: sent, pilot)
            proc.done.set()
            await _wait(lambda: len(sent) > 1, pilot, tries=20)   # both later steps had their turn
            assert [sig for _pgid, sig in sent] == [int(signal.SIGINT)]
    _run(go)


def test_ctrl_q_in_the_window_interrupts_instead_of_leaving(group_calls, monkeypatch):
    """review M3: Textual's own ctrl+q is priority, so it reached the app while a
    command ran and closed everything without stopping it."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 60)
    sent, alive = group_calls
    app, _ = _app([])
    proc = FakeProc(lines=["[agora] 合併 1/2"], hang=True)
    spawn, _ = _spawn(proc)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "m")
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: sent, pilot)
            await pilot.press("ctrl+q")
            assert _wait(lambda: len(sent) >= 1, pilot)
            assert app.is_running                       # still here: it interrupted
            alive[4242] = False
            proc.done.set()
    _run(go)


def test_leaving_stops_a_command_that_is_still_running(group_calls):
    """review M3: ctrl+q from the list, or the terminal closing, must not leave a
    child writing summaries or deleting on Drive."""
    sent, alive = group_calls
    app, _ = _app([])
    app._groups.add(4242)
    app.stop_everything()
    assert sent == [(4242, int(signal.SIGTERM))]


def test_an_interruption_does_not_start_the_next_command(group_calls, monkeypatch):
    """review M2: Esc stops the action, not just the command that happened to be on
    screen - the second agent's import must not start by itself."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 60)
    sent, alive = group_calls
    agent = FakeAgent("opencode", [Listed("s1", "/tmp/p", "甲", None)], texts={"s1": ["問"]})
    agent2 = FakeAgent("claude", [Listed("c1", "/tmp/r", "乙", None)], texts={"c1": ["問"]})
    app, _ = _app([agent, agent2])
    first = FakeProc(lines=["[agora] 匯入 1/1"], hang=True)
    started: list[list[str]] = []

    def spawn(argv):
        started.append(argv)
        return first

    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            await pilot.press("space", "down", "space", "enter")     # mark both, then import
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            await pilot.press("escape")
            await _wait(lambda: sent, pilot)
            alive[4242] = False
            first.done.set()
            assert _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            await pilot.press("space")                               # close the result window
            await pilot.pause(0.3)
            assert len(started) == 1, "中斷之後不該再開始第二段"
    _run(go)


def test_a_second_escape_does_not_resend_or_restart_the_escalation(group_calls, monkeypatch):
    """review L3: a second SIGINT would land while agora is keeping its pending
    record, and the old timer would bring SIGKILL forward."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 60)
    sent, alive = group_calls
    app, _ = _app([])
    proc = FakeProc(lines=["[agora] 合併 1/2"], hang=True)
    spawn, _ = _spawn(proc)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "m")
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: sent, pilot)
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert [sig for _pgid, sig in sent] == [int(signal.SIGINT)]
            alive[4242] = False
            proc.done.set()
    _run(go)


def test_a_failure_line_with_a_timestamp_in_it_is_not_progress():
    """review L1: `[agora] … 拉不到：rclone … 2026/10/03` read as 2026 of 10."""
    app, _ = _app([])
    # the progress line comes first and the failure line last: the bar reads the
    # newest match, so a loose `k/N` anywhere in a line would take the date instead
    spawn, _ = _spawn(lines=["[agora] pull 1/2",
                              "[agora] pull 拉不到：rclone copyto 失敗，3/4 個檔案，"
                              "2026/10/03 12:00:00 ERROR"],
                       hang=True)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")

            def progress():
                if not isinstance(app.screen, tui.Run):
                    return None
                bar = app.screen.query_one("#bar")
                return (bar.progress, bar.total) if bar.total else None
            await _wait(lambda: progress() is not None, pilot)
            assert progress() == (0, 2)      # 1/2 means the first one started (Q3)
    _run(go)


def test_the_selection_is_cleared_only_when_the_command_succeeded():
    """spec: a failure keeps it, so the same key can be pressed again."""
    app, _ = _app([])
    spawn, _ = _spawn(code=2)
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space")
            assert app.marked
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            assert _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            assert app.marked, "失敗之後勾選要留著"
            await pilot.press("space")
            await _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            assert app.marked
    _run(go)


def test_the_selection_is_cleared_after_a_success():
    app, _ = _app([])
    spawn, _ = _spawn()
    app.spawn = spawn

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space")
            assert app.marked
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            assert _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            await pilot.press("space")
            assert _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            assert not app.marked
    _run(go)


# --- the selection rules (T2 2.1) --------------------------------------------


def _marked_app(tmp_path, count=3):
    """Three agora sessions with titles nothing else in the row shares."""
    sessions = tuple((_hdr(f"01{'0' * 23}{n}", title, sid=f"ses_{n}"),
                      f"## user\n{n} 的內容\n")
                     for n, title in enumerate("甲乙丙"[:count], 1))
    paths, _ = _index(*sessions)
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    app.spawn, started = _spawn()
    app._last_spawned = started
    return app


async def _filter_to(pilot, text):
    await pilot.press("slash")
    for ch in text:
        await pilot.press(ch)
    await pilot.press("enter")
    await pilot.pause(0.2)


def test_actions_take_the_marked_rows_that_are_on_screen():
    """review V4: a row marked earlier and then filtered away is not on screen, so
    pressing d must not delete it."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):                                    # mark every row
                await pilot.press("space", "down")
            await _filter_to(pilot, "甲")
            assert [r.cells[1] for r in app.shown()] == ["甲"]
            assert [r.cells[1] for r in app.chosen_rows()] == ["甲"]   # the hidden two are not acted on
            assert app.hidden_marked() == 2                       # and the header says so
    _run(go)


def test_the_header_says_how_many_marked_rows_are_hidden():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space")
            assert "篩選掉" not in str(app.query_one("#bar").render())
            marked = set(app.marked)
            hidden = len([r for r in app.rows["agora"] if r.key in marked and r.cells[1] != "甲"])
            await _filter_to(pilot, "甲")
            assert f"另有 {hidden} 個勾選被篩選掉" in str(app.query_one("#bar").render())
    _run(go)


def test_the_cursor_row_is_used_when_nothing_visible_is_marked():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            assert app.chosen_rows() == [app.current()]
    _run(go)


def test_merge_reads_its_sources_from_top_to_bottom():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "down", "space")
            order = [r.key for r in app.shown()]         # what the screen shows
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(60):
                await pilot.pause(0.05)
                if app._last_spawned:
                    break
            assert app._last_spawned[0][2:5] == order
    _run(go)


def test_a_marks_every_visible_row_and_toggles_them_off_again():
    """spec 2.2: nothing marked -> all marked -> none marked."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert app.marked == {r.key for r in app.shown()}
            await pilot.press("a")
            await pilot.pause()
            assert app.marked == set()
    _run(go)


def test_a_does_not_touch_the_rows_the_filter_hides():
    """spec: a hidden row's mark is not changed, so it is still there when it comes back."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await _filter_to(pilot, "甲")
            await _wait(lambda: len(app.shown()) == 1, pilot)
            await pilot.press("a")
            await pilot.pause()
            assert app.marked == {r.key for r in app.rows["agora"] if r.cells[1] == "甲"}
            await pilot.press("slash", "backspace", "enter")  # clear the filter
            await _wait(lambda: len(app.shown()) == 3, pilot)
            await pilot.press("a")
            await pilot.pause()
            assert app.marked == {r.key for r in app.rows["agora"]}
    _run(go)


def test_space_marks_without_moving_the_cursor():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            before = app.query_one("#table").cursor_row
            await pilot.press("space")
            await pilot.pause()
            assert app.query_one("#table").cursor_row == before
    _run(go)


# --- review M1: only the marks of the rows that actually went (T2 2.4) --------


def _two_tab_app():
    """One agent session on each agent, so the import tab has two segments."""
    agent = FakeAgent("opencode", [Listed("s1", "/tmp/p", "甲", None)], texts={"s1": ["問"]})
    agent2 = FakeAgent("claude", [Listed("c1", "/tmp/r", "乙", None)], texts={"c1": ["問"]})
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "已在 agora", sid="ses_in"), "## user\\nx\\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[agent, agent2], check_setup=False)
    app.spawn, started = _spawn()
    app._last_spawned = started
    return app, started


def _spawn_per_call(codes):
    """A spawn whose processes return the given exit codes, one per call."""
    made: list[FakeProc] = []

    def spawn(argv):
        proc = FakeProc(lines=["[agora] 匯入 1/1"], code=codes[min(len(made), len(codes) - 1)])
        made.append(proc)
        return proc
    return spawn, made


async def _close_result(pilot, app):
    for _ in range(80):
        await pilot.pause(0.05)
        if isinstance(app.screen, tui.Tell):
            break
    await pilot.press("space")


def test_a_failed_segment_keeps_its_own_marks():
    """M1(a): opencode imported, claude failed - claude's row is still marked."""
    app, _ = _two_tab_app()
    app.spawn, _mades = _spawn_per_call([0, 2])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")                          # 未匯入
            await pilot.pause()
            await pilot.press("space", "down", "space")        # mark both agents' rows
            assert app.marked == {"opencode:s1", "claude:c1"}
            await pilot.press("enter")
            await _close_result(pilot, app)                     # the opencode segment
            await _close_result(pilot, app)                     # the claude one, which failed
            assert app.marked == {"claude:c1"}                  # that row is still marked
    _run(go)


def test_a_failure_on_one_tab_keeps_the_other_tabs_marks():
    """M1(b): a delete that fails on the agora tab must not touch the import tab's
    marks. It used to keep only the current tab's, which dropped them."""
    app, _ = _two_tab_app()
    app.spawn, _mades = _spawn_per_call([2])
    on_import = {r.key for r in app.rows["import"]}
    app.marked = {r.key for r in app.rows["agora"]} | on_import

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")                       # the agora row under the cursor
            await pilot.pause()
            await pilot.press("down", "enter")
            await _close_result(pilot, app)
            assert on_import <= set(app.marked), "另一頁的勾選被丟掉了"
    _run(go)


def test_a_success_clears_only_the_rows_it_acted_on():
    """M1(c): the row the filter hid was not sent, so its mark stays."""
    app = _marked_app(None)
    app.spawn, _started = _spawn_per_call([0])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("space", "down")            # mark all three
            await _filter_to(pilot, "甲")
            await _wait(lambda: len(app.shown()) == 1, pilot)
            hidden = set(app.marked) - {r.key for r in app.shown()}
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _close_result(pilot, app)
            assert set(app.marked) == hidden                   # only the hidden one is left
    _run(go)


def test_a_leaves_the_cursor_where_it_was():
    """review T2-sec2: marking everything must not move the cursor to the top."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down", "down")
            before = app.current().key
            assert app.query_one("#table").cursor_row == 2
            await pilot.press("a")
            await pilot.pause()
            assert app.current().key == before and app.query_one("#table").cursor_row == 2
    _run(go)


def test_cancelling_every_visible_row_keeps_the_hidden_marks():
    """review T2-sec2: `a` twice with a filter on unmarks what is on screen only."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("a")                       # all three
            await pilot.pause()
            await _filter_to(pilot, "甲")
            await _wait(lambda: len(app.shown()) == 1, pilot)
            await pilot.press("a")                       # 甲 visible and marked -> unmark it
            await pilot.pause()
            assert app.marked == {r.key for r in app.rows["agora"] if r.cells[1] != "甲"}
    _run(go)


def test_the_key_bar_shows_only_what_works_on_this_tab():
    """spec 2.3 /「按鍵」: the import tab has no merge, header, delete or push."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            keys = " ".join(str(app.query_one("#keys").render()).split())
            assert "m 合併" in keys and "P push" in keys and "enter 接續" in keys
            await pilot.press("]")
            await pilot.pause()
            keys = " ".join(str(app.query_one("#keys").render()).split())
            for gone in ("m 合併", "P push", "d 刪除", "e 改標頭"):
                assert gone not in keys
            assert "enter 匯入" in keys and "p pull" in keys
    _run(go)


def test_p_pulls_and_P_pushes():
    """spec 2.3: p pulls, P pushes; the old r and s are gone."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.check_action("pull", ()) and app.check_action("push", ())
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("down", "enter")            # 確定 in the confirm window
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1][:2] == ["pull", "session"]
            await pilot.pause()
            for _ in range(60):                      # close the result window
                await pilot.pause(0.05)
                if isinstance(app.screen, tui.Tell):
                    break
            await pilot.press("space")
            await _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            await pilot.press("P")
            await pilot.pause()
            assert isinstance(app.screen, tui.Confirm)          # push asks first
            await pilot.press("escape")
    _run(go)


def test_the_agora_only_keys_do_nothing_on_the_import_tab():
    """merge, edit, delete and push are not bound there (spec「按鍵」)."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            assert not app.check_action("merge", ()) and not app.check_action("delete", ())
            await pilot.press("m", "d", "P")
            await pilot.pause()
            assert not app._last_spawned
    _run(go)


def test_the_cloud_column_says_where_each_session_is():   # spec 3.1
    """✓ on Drive, ✗ deleted by another machine, 未上傳 still in the outbox."""
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "在雲端", sid="s1"), "## user\nx\n"),
                      (_hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "被刪了", sid="s2"), "## user\nx\n"))
    store.Index(paths).mark_missing(["01BBBBBBBBBBBBBBBBBBBBBBBB"])   # deleted elsewhere
    staged = store.stage(paths, _hdr("01CCCCCCCCCCCCCCCCCCCCCCCC", "還沒上傳"),
                         "## user\nx\n", None)
    store.remember(paths, staged, store.Index(paths))     # saved here, not on Drive yet
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    app.spawn, _ = _spawn()

    async def go():
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.pause()
            table = app.query_one("#table")
            assert [str(c.label) for c in table.columns.values()][2:] == list(tui.COLUMNS["agora"])
            cells = {r.key: r.cells for r in app.rows["agora"]}
            assert cells["agora:01AAAAAAAAAAAAAAAAAAAAAAAA"][4] == "✓"
            assert cells["agora:01BBBBBBBBBBBBBBBBBBBBBBBB"][4] == "✗"
            assert cells["agora:01CCCCCCCCCCCCCCCCCCCCCCCC"][4] == "未上傳"
            assert str(table.get_row("agora:01BBBBBBBBBBBBBBBBBBBBBBBB")[-1]) == "✗"
    _run(go)


def test_pull_and_push_ask_before_doing_it_and_offer_the_flag():   # spec 3.2
    """The option that changes what happens to a session Drive lost is off by
    default - it is the decision, not the default."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            window = app.screen
            assert isinstance(window, tui.Confirm)
            assert window.extra.startswith("雲端沒有的就刪掉")
            assert not window.picked                     # nothing pre-ticked
            await pilot.press("tab")                      # the page below must not change
            await pilot.pause()
            assert app.tab == "agora" and isinstance(app.screen, tui.Confirm)
            from textual.widgets import Checkbox
            assert isinstance(app.focused, Checkbox)
            await pilot.press("space")                   # tick it
            await pilot.pause()
            assert window.picked
            await pilot.press("shift+tab")                # back to the buttons
            await pilot.press("down", "enter")            # 確定
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1][-1] == "--not-exist-delete"
    _run(go)


def test_push_without_the_option_sends_no_flag():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("P")
            await pilot.pause()
            assert isinstance(app.screen, tui.Confirm)
            assert app.screen.extra == "雲端沒有的就傳回去（等同 --not-exist-upload）"
            await pilot.press("down", "enter")            # 確定, unticked
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1][:2] == ["push", "session"]
            assert "--not-exist-upload" not in app._last_spawned[-1]
    _run(go)


def test_cancelling_pull_runs_nothing():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not app._last_spawned
    _run(go)


def test_push_is_not_bound_on_the_import_tab():   # review Q1
    """P there started a push with no ids at all."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            assert not app.check_action("push", ())
            await pilot.press("P")
            await pilot.pause()
            assert not app._last_spawned
            assert "P push" not in " ".join(str(app.query_one("#keys").render()).split())
    _run(go)


def test_no_action_starts_a_process_without_rows_to_send():
    """review Q1: an empty list of ids is not a command, so nothing runs."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "zzzz":
                await pilot.press(ch)
            await pilot.press("enter")
            await _wait(lambda: app.shown() == [], pilot)
            for key in ("d", "p"):
                await pilot.press(key)
                await pilot.pause()
                assert "先選" in str(app.query_one("#msg").render())
            assert not app._last_spawned
    _run(go)


def test_continuing_a_session_the_cloud_lost_is_refused_on_screen():   # spec 3.3
    """No agent is opened, and the message is the command mode's own - with the
    two commands that settle it."""
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "被刪了", sid="s1"), "## user\\nx\\n"))
    store.Index(paths).mark_missing(["01AAAAAAAAAAAAAAAAAAAAAAAA"])
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    launched = []
    app.spawn, started = _spawn()
    app.outside = lambda argv: launched.append(argv) or 0

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            said = app.screen.title_ + " " + " ".join(str(app.screen.lines).split())
            assert "不能接續" in said and "雲端沒有" in said
            assert "--not-exist-upload" in said and "--not-exist-delete" in said
            assert not launched                       # no agent, no directory question
    _run(go)


def test_editing_a_session_the_cloud_lost_is_refused_too():
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "被刪了", sid="s1"), "## user\\nx\\n"))
    store.Index(paths).mark_missing(["01AAAAAAAAAAAAAAAAAAAAAAAA"])
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    launched = []
    app.outside = lambda argv: launched.append(argv) or 0

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            assert "不能改標頭" in app.screen.title_
            assert not launched                       # the editor never opened
    _run(go)


def test_the_refusal_is_the_command_modes_own_wording():
    """One wording for both (T2 3.3): the screen asks the data, not cli.py."""
    from agora import cli
    paths, index = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "被刪了", sid="s1"), "## user\\nx\\n"))
    store.Index(paths).mark_missing(["01AAAAAAAAAAAAAAAAAAAAAAAA"])
    refusal = store.cloud_gone(store.Index(paths), "agora:01AAAAAAAAAAAAAAAAAAAAAAAA")
    assert refusal == cli._cloud_lost("agora:01AAAAAAAAAAAAAAAAAAAAAAAA")


def test_enter_on_the_checkbox_confirms_and_does_not_tick_it():   # review U1
    """The option that deletes a local copy or puts a session back must not be one
    Enter away from being ticked."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            window = app.screen
            assert isinstance(window, tui.Confirm)
            await pilot.press("down")                      # 確定 is highlighted
            await pilot.press("tab")                       # then focus the checkbox
            await pilot.pause()
            assert not window.picked
            await pilot.press("enter")                      # confirm, not tick
            await _wait(lambda: app._last_spawned, pilot)
            assert "--not-exist-delete" not in app._last_spawned[-1]
    _run(go)


def test_space_is_what_ticks_it():
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("P")
            await pilot.pause()
            window = app.screen
            await pilot.press("down")                      # 確定
            await pilot.press("tab")
            await pilot.press("space")                      # tick
            await pilot.pause()
            assert window.picked
            await pilot.press("shift+tab")                  # back to the buttons
            await pilot.press("enter")                      # 確定 (already highlighted)
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1][-1] == "--not-exist-upload"
    _run(go)


# --- spec「進度與中斷」and every remaining Scenario, driven by key presses ---


def test_deleting_three_marked_rows_is_one_command():
    """spec: 三列勾選後按 d → 三個 Session 送進一個 delete 指令。"""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("space", "down")
            marked = [r.key for r in app.shown() if r.key in app.marked]
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1] == ["delete", "session", *marked, "--yes"]
    _run(go)


def test_import_uses_the_row_under_the_cursor_when_nothing_is_marked():
    """spec: 未匯入頁沒有勾選任何列，按 Enter → 只有游標那一列被匯入。"""
    agent = FakeAgent("opencode", [Listed("s1", "/tmp/p", "甲", None), Listed("s2", "/tmp/q", "乙", None)],
                      texts={"s1": ["問"], "s2": ["答"]})
    app = tui.AgoraApp(app_paths := store.Paths.from_env(), FakeCli(), agents=[agent], check_setup=False)
    for n, title in enumerate("甲乙", 1):
        ulid = f"02{'0' * 23}{n}"
        (app_paths.mirror / ulid).mkdir(parents=True, exist_ok=True)
        (app_paths.mirror / ulid / "session.md").write_text(
            h.dump_document(_hdr(ulid, f"已在 agora {n}", sid=f"in{n}"), "## user\\nx\\n"), encoding="utf-8")
        store.Index(app_paths).put(ulid, "md5", _hdr(ulid, f"已在 agora {n}", sid=f"in{n}"), "## user\\nx\\n")
    app.spawn, started = _spawn()
    app._last_spawned = started

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")                      # 未匯入
            await pilot.pause()
            await pilot.press("down")                     # the second row, un-marked
            await pilot.press("enter")
            await _wait(lambda: app._last_spawned, pilot)
            assert app._last_spawned[-1] == ["import", "session", "--agent", "opencode",
                                             "--external-session-id", "s2"]
    _run(go)


def test_the_progress_bar_walks_from_one_to_five():
    """spec: 匯入五個 session → 進度條從 1/5 走到 5/5。"""
    app = _marked_app(None)
    app.spawn, _ = _spawn(lines=[f"[agora] 匯入 {n}/5" for n in range(1, 6)], hang=True)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")

            def progress():
                if not isinstance(app.screen, tui.Run):
                    return None
                bar = app.screen.query_one("#bar")
                return (bar.progress, bar.total) if bar.total else None
            await _wait(lambda: progress() == (4, 5), pilot)
            assert progress() == (4, 5)      # the fifth started, so four are finished
    _run(go)


def test_interrupting_a_merge_returns_to_the_list_and_says_it_carries_on(group_calls):
    """spec: 中斷 merge → 動作停止、回到清單，並說重跑會接著做。"""
    sent, alive = group_calls
    app = _marked_app(None)
    proc = FakeProc(lines=["[agora] 來源 1/2", "[agora] 來源 2/2"], hang=True)
    app.spawn, _ = _spawn(proc)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("space", "down")
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            await pilot.press("escape")
            alive[4242] = False
            proc.done.set()
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            assert "重跑同一個動作會接著做" in " ".join(str(app.screen.lines).split())
            await pilot.press("space")
            assert _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)   # back at the list
            assert str(app.query_one("#msg").render()) == ""       # nothing left over (Q4)
    _run(go)


# --- 4.1b: a real process group, a real signal (review V7) --------------------

#: A leader that exits on SIGINT the way `agora` does (KeyboardInterrupt -> 130)
#: and leaves behind a grandchild that ignores SIGINT and SIGTERM - the case
#: review M1 is about, where our own child is gone and the agent is not.
LEADER = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
# DEVNULL, not the leader's stdout: the agent agora writes a summary with has its
# own pipe, so it does not hold the interactive mode's pipe open - and a grandchild
# that did would keep the leader a zombie, which is what made this test blind to
# the "only look at the leader" bug (review X1).
child = subprocess.Popen([sys.executable, "-c", "import signal, time\\n"
                         "signal.signal(signal.SIGINT, signal.SIG_IGN)\\n"
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
                         "time.sleep(300)"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# its own file, not stdout: the waiting window's reader thread owns that pipe
open(sys.argv[1], "w").write(f"{os.getpgid(0)} {child.pid}")
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_esc_stops_a_real_process_group_with_a_stubborn_grandchild(monkeypatch, tmp_path):
    """The real thing, once (review V7, M1): the whole group goes and nothing is left
    behind - not even a grandchild that ignores SIGINT and SIGTERM.

    The leader behaves like `agora` does under Esc: it catches the signal and exits
    130 while the agent it started keeps running. A fake process cannot show that,
    because its death is what the fake checks."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 0.5)
    sent: list[int] = []
    real_killpg = tui.killpg
    monkeypatch.setattr(tui, "killpg", lambda pgid, sig: (sent.append(int(sig)),
                                                          real_killpg(pgid, sig))[1])
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "甲", sid="s1"), "## user\nx\n"),
                      (_hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "乙", sid="s2"), "## user\nx\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    pidfile = tmp_path / "group.pids"
    pids: dict[str, int] = {}

    def spawn(argv):
        """The command mode, as a real process group with a stubborn child."""
        return subprocess.Popen([sys.executable, "-c", LEADER, str(pidfile)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, start_new_session=True)

    async def go():
        app.spawn = spawn
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            for _ in range(60):                     # it writes the two pids and goes on
                await pilot.pause(0.1)
                if pidfile.exists():
                    break
            pgid, grandchild = pidfile.read_text().split()
            pids.update(pgid=int(pgid), grandchild=int(grandchild))
            assert _alive(pids["grandchild"])

            await pilot.press("escape")
            for _ in range(120):
                await pilot.pause(0.1)
                if not _alive(pids["grandchild"]):
                    break
            assert sent == [int(signal.SIGINT), int(signal.SIGTERM), int(signal.SIGKILL)]
            assert not _alive(pids["grandchild"]), "孫程序還活著"
            try:
                os.killpg(pids["pgid"], 0)
                raise AssertionError("process group 還在")
            except ProcessLookupError:
                pass                     # the group is empty: nothing was left running
    try:
        _run(go)
    finally:                            # a failed assertion must not leave it sleeping
        with contextlib.suppress(ProcessLookupError, KeyError, OSError):
            os.killpg(pids["pgid"], signal.SIGKILL)


def test_each_step_of_the_escalation_gets_its_own_time(group_calls, monkeypatch):
    """Review W1: SIGTERM and SIGKILL used the same timer, so they arrived together
    and an agent had no time to wind up between them."""
    monkeypatch.setattr(tui, "ESCALATE_AFTER", 0.4)
    sent, alive = group_calls
    stamps: dict[int, float] = {}
    real_killpg = tui.killpg

    def timed(pgid, sig):
        stamps.setdefault(int(sig), time.monotonic())
        return real_killpg(pgid, sig)

    monkeypatch.setattr(tui, "killpg", timed)
    app = _marked_app(None)
    proc = FakeProc(lines=["[agora] 合併 1/2"], hang=True)
    app.spawn, _ = _spawn(proc)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "down", "space", "m")
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            await pilot.press("escape")
            for count in (1, 2, 3):
                await _wait(lambda count=count: len(stamps) >= count, pilot)
            assert [sig for _pgid, sig in sent] == [int(signal.SIGINT),
                                                     int(signal.SIGTERM), int(signal.SIGKILL)]
            gap = stamps[int(signal.SIGKILL)] - stamps[int(signal.SIGTERM)]
            assert gap >= tui.ESCALATE_AFTER * 0.9, f"SIGKILL 只比 SIGTERM 晚 {gap:.2f} 秒"
            alive[4242] = False
            proc.done.set()
    _run(go)


def test_marking_all_leaves_the_rows_the_filter_hides_alone():   # review W2
    """The direction the other test did not cover: `a` marking everything must not
    overwrite a mark made on a row that is filtered out."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            second = next(r for r in app.shown() if r.cells[1] == "乙")
            app.marked = {second.key}
            await _filter_to(pilot, "甲")
            await _wait(lambda: len(app.shown()) == 1, pilot)
            await pilot.press("a")                       # mark what is on screen
            await pilot.pause()
            assert second.key in app.marked               # 乙 was already marked, still is
            assert app.marked == {second.key, next(r.key for r in app.rows["agora"]
                                                   if r.cells[1] == "甲")}
            await pilot.press("slash", "backspace", "enter")     # and the filter off
            await _wait(lambda: len(app.shown()) == 3, pilot)
            assert app.marked == {second.key, next(r.key for r in app.rows["agora"]
                                                   if r.cells[1] == "甲")}
    _run(go)


def test_enter_on_the_confirmation_window_keeps_it_cancelled():   # review W2
    """It opens on 取消, so Enter straight away is the safe one - the distance
    between "one Enter too many" and deleting a local copy."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, tui.Confirm)
            assert app.screen.query_one("OptionList").highlighted == 0
            await pilot.press("enter")                   # still on 取消
            await pilot.pause()
            assert not app._last_spawned
    _run(go)


# --- PM's own run through the screen (docs/tickets/T2-pm-run.md) ------------


def test_the_tick_is_readable_as_text_and_the_line_says_what_it_does():   # Q1
    """A tick that only changes colour is a tick nobody can read."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            window = app.screen
            assert "[ ]" in str(window.query_one("#extra").label)
            assert "本機的不動" in str(window.query_one("#effect").render())
            await pilot.press("tab")
            await pilot.press("space")
            await pilot.pause()
            assert "[x]" in str(window.query_one("#extra").label)
            assert "會被刪掉" in str(window.query_one("#effect").render())
    _run(go)


def test_the_push_option_says_what_ticking_it_does():   # Q1
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("P")
            await pilot.pause()
            window = app.screen
            assert "不傳" in str(window.query_one("#effect").render())
            await pilot.press("tab")
            await pilot.press("space")
            await pilot.pause()
            assert "傳回 Drive" in str(window.query_one("#effect").render())
    _run(go)


def test_enter_selects_rather_than_confirms():   # Q2
    """The wording read as 「Enter＝確定」 when Enter is 「選停著的那一個」, and
    Enter on the default (取消) cancels."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")                      # the delete window
            await pilot.pause()
            hints = [str(w.render()) for w in app.screen.query("Static")]
            assert any("Enter 選擇" in h for h in hints)
            assert not any("Enter 確定" in h for h in hints)
    _run(go)


def test_a_window_with_nothing_to_choose_says_enter_submits():   # E3
    """The working-directory prompt has no options to pick between, so 「選擇」 is
    simply wrong there - Enter sends what was typed."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            app.push_screen(tui.AskText("工作目錄", "/tmp"))
            await _wait(lambda: isinstance(app.screen, tui.AskText), pilot)
            await pilot.pause()                        # let it compose
            assert isinstance(app.screen, tui.AskText), app.screen
            hints = [str(w.render()) for w in app.screen.query("Static")]
            assert any("Enter 確定" in h for h in hints), hints
            assert not any("Enter 選擇" in h for h in hints), hints
    _run(go)


def test_the_bar_really_reaches_n_n_before_the_window_goes():   # E2
    """A clean exit fills the bar - on screen, before the window closes.

    `tick` redraws every 0.1s and the window used to dismiss the instant the process
    ended, so `N/N` was in the code and nowhere on the screen. Recording every value
    the bar is given is the only way to see it: a test that looks at the bar after
    the window closed sees nothing at all.
    """
    app = _marked_app(None)
    proc = FakeProc(lines=["[agora] 刪除 1/3", "[agora] 刪除 2/3", "[agora] 刪除 3/3"],
                    code=0, hang=True)
    app.spawn, _ = _spawn(proc)
    seen: list[tuple] = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Run), pilot)
            window, bar = app.screen, app.screen.query_one("#bar")
            real_update = bar.update

            def recording(**kw):
                seen.append((kw.get("progress"), kw.get("total"), app.screen is window))
                return real_update(**kw)
            bar.update = recording
            proc.done.set()                            # and now it ends cleanly
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
    _run(go)
    full = [row for row in seen if row[:2] == (3, 3)]
    assert full, f"the bar never reached full: {seen}"
    assert full[-1][2], f"it filled only after the window closed: {seen}"


def test_the_bar_counts_what_is_finished_and_fills_only_on_a_clean_exit():   # Q3
    """`k/N` says the k-th has started, so k-1 are done; N/N means the command
    said it finished - not that the last one had begun."""
    app = _marked_app(None)
    proc = FakeProc(lines=["[agora] 匯入 1/3", "[agora] 匯入 2/3", "[agora] 匯入 3/3"], hang=True)
    app.spawn, _ = _spawn(proc)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")

            def progress():
                if not isinstance(app.screen, tui.Run):
                    return None
                bar = app.screen.query_one("#bar")
                return (bar.progress, bar.total) if bar.total else None
            await _wait(lambda: progress() == (2, 3), pilot)
            assert progress() == (2, 3)              # the third started, two are done
            proc.done.set()                           # and now it exits cleanly
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            assert "已從本機刪除" in " ".join(str(app.screen.lines).split())
    _run(go)


def test_the_status_line_is_empty_after_the_result_window_closes():   # Q4
    app = _marked_app(None)
    app.spawn, _ = _spawn()

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            assert "已從本機刪除" in " ".join(str(app.screen.lines).split())   # it is in the window
            await pilot.press("space")
            await _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            assert str(app.query_one("#msg").render()) == ""
    _run(go)


# --- the result window has to say what the exit code means (review T2-final) ---


def _result_of(app, code, *, mark_one=False, action="delete", said=()):
    """Run one `action` that exits `code`, and hand back what the result window said."""
    lines: list[str] = []
    key = {"delete": "d", "pull": "P"}.get(action)

    async def go():
        proc = FakeProc(lines=[f"[agora] {action} 1/1", *said], code=code)
        app.spawn, _ = _spawn(proc)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            if mark_one:
                await pilot.press("space")
            if action == "import":       # Enter on the import tab is 匯入; elsewhere it is 接續
                await pilot.press("]")
                await pilot.pause()
                await pilot.press("enter")
            else:
                await pilot.press(key)
                await pilot.pause()
                await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            lines.append(" ".join(str(line) for line in app.screen.lines))
    _run(go)
    return lines


def test_the_result_window_says_a_command_that_worked():
    """exit 0, for an action with nothing to send afterwards."""
    assert "完成" in " ".join(_result_of(_marked_app(None), 0, action="pull"))


def test_the_result_window_says_some_of_it_failed():
    """exit 2: at least one item did not work, and the window says so rather than
    leaving the user to work it out from the output."""
    said = _result_of(_marked_app(None), 2, mark_one=True)
    assert "沒有全部成功" in " ".join(said)


def test_the_result_window_says_it_is_waiting_to_be_uploaded():
    """exit 3: saved here, not on Drive yet - which is not the same as done. Every
    action but delete, which stores nothing in the outbox and says its own thing (G4)."""
    said = _result_of(_marked_app(None), 3, action="pull")
    assert "outbox" in " ".join(said) and "再送" in " ".join(said)


# --- the bar walks, it does not jump (spec「看得到進度」) --------------------


def test_the_progress_bar_walks_through_the_middle_values():
    """One line at a time, so the intermediate values are on screen before the end."""
    app = _marked_app(None)
    proc = DripProc(["[agora] 刪除 1/3", "[agora] 刪除 2/3", "[agora] 刪除 3/3"])
    app.spawn, _ = _spawn(proc)
    seen: list[tuple] = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("space", "down")
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")            # 確定

            def progress():
                if not isinstance(app.screen, tui.Run):
                    return None
                bar = app.screen.query_one("#bar")
                return (bar.progress, bar.total) if bar.total else None

            proc.send(0)                                 # 1/3: nothing finished yet
            await _wait(lambda: progress() == (0, 3), pilot)
            seen.append(progress())
            proc.send(1)                                 # 2/3
            await _wait(lambda: progress() == (1, 3), pilot)
            seen.append(progress())
            proc.send(2)                                 # 3/3
            await _wait(lambda: progress() == (2, 3), pilot)
            seen.append(progress())
    _run(go)
    assert seen == [(0, 3), (1, 3), (2, 3)]              # it walked, and stopped short of full


# --- what the background is doing, said where the user is looking (4.1) --------


def test_the_result_window_does_not_promise_a_background_delete_that_is_not_queued():
    """W5: rows that were all cloud-lost never enter the queue, so there is no Drive half
    coming - and the command's own line is what says so."""
    said = " ".join(_result_of(_marked_app(None), 0,
                               said=["[agora] 已從本機刪除 3 個，雲端沒有，只刪本機這份"]))
    assert "已從本機刪除" in said
    assert "背景移到 Drive 垃圾桶" not in said


def test_the_result_window_says_the_drive_half_is_on_its_way():
    """4.1: delete finished here; Drive is somebody else's turn now."""
    said = " ".join(_result_of(_marked_app(None), 0,
                               said=["[agora] 已從本機刪除 3 個，背景移到 Drive 垃圾桶"]))
    assert "已從本機刪除，背景移到 Drive 垃圾桶" in said
    assert "已存進 outbox" not in said, "nothing is waiting in the outbox"


def _after_one_import(code=0) -> str:
    """Import the one session the fake agent has, and hand back what the window said."""
    agent = FakeAgent("opencode", [Listed("s1", "/tmp/p", "甲", None)], texts={"s1": ["問"]})
    app = tui.AgoraApp(store.Paths.from_env(), FakeCli(), agents=[agent], check_setup=False)
    app.spawn, _ = _spawn(FakeProc(lines=["[agora] 匯入 1/1"], code=code))
    said: list[str] = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")                       # the import tab
            await pilot.pause()
            await pilot.press("enter")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            said.append(" ".join(str(line) for line in app.screen.lines))
    _run(go)
    return " ".join(said)


def test_the_result_window_says_an_import_is_safe_here():
    """4.1: the same for the other direction - the session is whole on this machine and
    going up, which is not the same as "it is on Drive"."""
    said = _after_one_import()
    assert "已經存在本機，背景上傳中" in said
    assert "已存進 outbox" not in said


def test_only_a_failed_start_still_says_the_outbox():
    """4.1: exit 3 is the one case where a background could not start, so it is the one
    case that still means「已存進 outbox」."""
    said = _after_one_import(code=3)
    assert "已存進 outbox" in said
    assert "背景上傳中" not in said


# --- first run: your own OAuth client is optional (T5, design D5) ---------------

#: Self-made values. Nothing here is a real credential, and nothing that reads them
#: is allowed to put them anywhere but rclone's argv.
FAKE_ID = "1234567890-fakeclientid.apps.googleusercontent.com"
FAKE_SECRET = "GOCSPX-fakesecret-not-a-real-one"


def _client_json(tmp_path) -> str:
    """The shape Google hands out for a Desktop client, with made-up values."""
    path = tmp_path / "client_secret_fake.json"
    path.write_text(json.dumps({"installed": {"client_id": FAKE_ID,
                                              "client_secret": FAKE_SECRET,
                                              "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                              "token_uri": "https://oauth2.googleapis.com/token"}},
                              ensure_ascii=False), encoding="utf-8")
    return str(path)


def _client_text(tmp_path) -> str:
    """rclone's own two-line form, with made-up values."""
    path = tmp_path / "rclone-client.conf"
    path.write_text(f"[gdrive]\ntype = drive\nClient-ID = {FAKE_ID}\nSECRET = {FAKE_SECRET}\n",
                    encoding="utf-8")
    return str(path)


def test_a_downloaded_client_json_is_read_by_path(tmp_path, capsys):
    assert tui.read_client(_client_json(tmp_path)) == (FAKE_ID, FAKE_SECRET)
    said = capsys.readouterr()
    assert FAKE_ID not in said.out and FAKE_SECRET not in said.out
    assert FAKE_SECRET not in said.err, "reading a client file says nothing about it"


def test_the_two_line_client_file_is_read_too(tmp_path):
    assert tui.read_client(_client_text(tmp_path)) == (FAKE_ID, FAKE_SECRET)


def test_a_client_file_we_cannot_use_is_none_not_a_guess(tmp_path):
    """A wrong path, a file that is not one, and one with half the pair: all of them
    fall back to rclone's own client rather than sending something half-read."""
    assert tui.read_client(str(tmp_path / "not-there.json")) is None
    (tmp_path / "notes.txt").write_text("記得換 client\n", encoding="utf-8")
    assert tui.read_client(str(tmp_path / "notes.txt")) is None
    half = tmp_path / "half.json"
    half.write_text(json.dumps({"installed": {"client_id": FAKE_ID}}), encoding="utf-8")
    assert tui.read_client(str(half)) is None
    assert tui.read_client(str(tmp_path)) is None          # a directory


def test_a_utf8_bom_is_not_a_reason_to_refuse_the_client_file(tmp_path):
    """T4: a file saved by an editor that writes a BOM is still a UTF-8 JSON."""
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"installed": {"client_id": FAKE_ID, "client_secret": FAKE_SECRET}}).encode())
    assert tui.read_client(str(path)) == (FAKE_ID, FAKE_SECRET)


def test_a_client_file_nested_deep_enough_to_exhaust_the_parser(tmp_path, capfd):
    """U1: `json.loads` raises RecursionError on a very deep document, and when the
    first-run screen dies Textual prints its locals - `text` among them, with the
    secret at the front. So: None, and nothing on any stream, not even the file
    descriptor level."""
    deep = tmp_path / "deep.json"
    depth = 200_000
    deep.write_text('{"installed": {"client_secret": "' + FAKE_SECRET + '", "x": '
                    + "[" * depth + "]" * depth + "}", encoding="utf-8")
    assert tui.read_client(str(deep)) is None
    assert capfd.readouterr() == ("", ""), "not even on the file descriptors"
    assert deep.read_text(encoding="utf-8").startswith("{\"installed\"")


def test_a_path_naming_a_user_that_does_not_exist_is_not_a_crash(tmp_path, monkeypatch, capsys):
    """U1: `~nosuchuser/x.json` makes `Path.expanduser` raise RuntimeError. Inside the
    reader that is just another None; outside it - the screen asks 「is this a file?」
    before reading - it went straight to the screen and ended the app."""
    assert tui.read_client("~nosuchuser_review/x.json") is None
    # and the question the screen asks *before* reading, which is not inside the reader
    assert tui._is_file("~nosuchuser_review/x.json") is False
    calls, said = _first_run(1, "~nosuchuser_review/x.json", tmp_path, monkeypatch, give_up=True)
    assert calls == [None], "a path we cannot even expand is a path we cannot use"
    assert "讀不到 client 設定檔" in said
    quiet = capsys.readouterr()
    assert quiet.out == "" and quiet.err == ""


def test_only_a_desktop_client_is_accepted(tmp_path):
    """T4: rclone redirects a `web` client to http://127.0.0.1:53682/, which such a
    client will not have registered - the user would only see「授權沒有完成」. And a
    shape we do not recognise is refused rather than guessed at."""
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": FAKE_ID, "client_secret": FAKE_SECRET}}),
                   encoding="utf-8")
    assert tui.read_client(str(web)) is None, "a web client cannot finish this authorization"
    odd = tmp_path / "odd.json"
    odd.write_text(json.dumps({"installed": [FAKE_SECRET]}), encoding="utf-8")
    assert tui.read_client(str(odd)) is None
    half = tmp_path / "half-web.json"
    half.write_text(json.dumps({"installed": "x", "web": {"client_id": FAKE_ID,
                                                          "client_secret": FAKE_SECRET}}),
                    encoding="utf-8")
    assert tui.read_client(str(half)) is None


def test_the_rclone_command_carries_the_client_when_there_is_one(tmp_path):
    paths = store.Paths(config=tmp_path / "config", cache=tmp_path / "cache", state=tmp_path / "state")
    built_in = tui.authorize_argv(paths)
    assert "client_id=1234" not in " ".join(built_in), "nothing offered, nothing sent"

    mine = tui.authorize_argv(paths, (FAKE_ID, FAKE_SECRET))
    assert f"client_id={FAKE_ID}" in mine and f"client_secret={FAKE_SECRET}" in mine
    assert "scope=drive.file" in mine, "the scope does not change: drive.file either way"
    # a list, never a string: a secret that goes through a shell is a secret in `ps`
    assert isinstance(mine, list) and mine[:1] != ["rclone config create"]


def test_nothing_about_the_client_is_printed(monkeypatch, tmp_path, capsys):
    """rclone echoes the remote it wrote, secret and all. The values must not reach the
    screen, the log, or stdout (design D5)."""
    said: list[str] = []
    calls: list[tuple] = []
    proc = FakeProc(lines=[f"client_id = {FAKE_ID}",
                           f"client_secret = {FAKE_SECRET}",
                           "token = {\"access_token\": \"ya29.fake\"}",
                           "Created remote gdrive"])
    monkeypatch.setattr(tui.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)) or proc)
    paths = store.Paths(config=tmp_path / "config", cache=tmp_path / "cache", state=tmp_path / "state")

    assert tui.authorize(paths, said.append, (FAKE_ID, FAKE_SECRET)) == 0

    assert calls and isinstance(calls[0][0][0], list), "argv is a list, not a shell string"
    assert not calls[0][1].get("shell"), "a secret through a shell is a secret in `ps`"
    said_text = "\n".join(said)
    assert FAKE_SECRET not in said_text and "ya29.fake" not in said_text
    assert "Created remote gdrive" in said_text, "the lines that are safe still come through"
    assert FAKE_SECRET not in capsys.readouterr().out


def _first_run(pick: int, typed: str, tmp_path, monkeypatch, give_up: bool | None = None):
    """Drive the first-run screens: `pick` on the Choose, then `typed` in the prompt."""
    paths = store.Paths(config=tmp_path / "config", cache=tmp_path / "cache", state=tmp_path / "state")
    (tmp_path / "config").mkdir()
    monkeypatch.setenv("AGORA_RCLONE", "/bin/sh")
    authorize_calls: list[tuple] = []

    def fake_authorize(p, say=print, client=None):
        authorize_calls.append(client)
        (tmp_path / "config" / "rclone.conf").write_text("[gdrive]\n", encoding="utf-8")
        return 0
    monkeypatch.setattr(tui, "authorize", fake_authorize)
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=True)
    said: list[str] = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down" * pick, "enter")
            await pilot.pause()
            if pick == 1:
                await pilot.pause()
                for ch in typed:
                    await pilot.press("space" if ch == " " else ch)
                await pilot.press("enter")
                # a path we cannot use stops at a window that has to be read first
                await _wait(lambda: isinstance(app.screen, tui.Choose) or bool(authorize_calls),
                            pilot)
                said.extend(str(w.render()) for w in app.screen.walk_children(Widget)
                            if isinstance(w, Static))
                if isinstance(app.screen, tui.Choose) and give_up is not None:
                    await pilot.press("down", "enter")    # 用內建的 client
                    await pilot.pause()
            await _wait(lambda: bool(authorize_calls), pilot)
    _run(go)
    return authorize_calls, " ".join(said)


def test_the_first_run_can_use_your_own_client(tmp_path, monkeypatch):
    calls, _ = _first_run(1, _client_json(tmp_path), tmp_path, monkeypatch)
    assert calls == [(FAKE_ID, FAKE_SECRET)]


def test_the_first_run_works_on_rclones_own_client_when_you_skip_it(tmp_path, monkeypatch):
    """The second option, then Esc: rclone's own client, exactly as before this existed."""
    calls, _ = _first_run(1, "", tmp_path, monkeypatch)
    assert calls == [None]


def test_a_client_file_we_cannot_read_falls_back_to_rclones_own(tmp_path, monkeypatch):
    calls, said = _first_run(1, str(tmp_path / "nowhere.json"), tmp_path, monkeypatch, give_up=True)
    assert calls == [None], "it says what happened, then does the thing that works"
    assert "讀不到 client 設定檔" in said
    assert "找不到這個檔案" in said, "a typo should not read as「你的檔案內容不對」"
    assert FAKE_SECRET not in said and FAKE_ID not in said, "the path is not the values"


def test_a_client_file_that_is_not_utf8_falls_back_without_saying_what_was_in_it(
        tmp_path, monkeypatch, capsys):
    """T1: a client file saved as UTF-16 raises UnicodeDecodeError, and *its message
    carries the whole file* - which is the credential. The app must not die, and neither
    the screen nor anything raised may contain the secret."""
    utf16 = tmp_path / "client_secret_utf16.json"
    utf16.write_bytes(json.dumps({"installed": {"client_id": FAKE_ID,
                                                "client_secret": FAKE_SECRET}}
                                 ).encode("utf-16"))
    assert tui.read_client(str(utf16)) is None
    # and saying why is not an option either: the reason *is* the whole file, mangled
    # by the failed decode but readable. So: nothing at all comes out of here.
    quiet = capsys.readouterr()
    assert quiet.out == "" and quiet.err == "", f"讀一個壞掉的 client 檔不該出聲：{quiet}"

    calls, said = _first_run(1, str(utf16), tmp_path, monkeypatch, give_up=True)

    assert calls == [None], "carry on with rclone's own client, do not fall over"
    assert "讀不到 client 設定檔" in said
    assert FAKE_SECRET not in said, "the secret leaked into the window"
    assert FAKE_ID not in said
    assert "UnicodeDecodeError" not in said, "nor the exception that would have carried it"


def test_a_delete_whose_uploader_could_not_start_does_not_say_it_was_saved_to_the_outbox():
    """G4: exit 3 from delete is not「已存進 outbox」- nothing was stored; the Drive half
    is what waits for a later command."""
    said = " ".join(_result_of(_marked_app(None), 3,
                               said=["[agora] 背景上傳啟動失敗，移到 Drive 垃圾桶要等之後的指令"]))
    assert "已從本機刪除；移到 Drive 垃圾桶要等之後的指令" in said
    assert "已存進 outbox" not in said


# --- T6: the preview reads a step, not the file ---------------------------------


def _big_body(mb: int = 3) -> str:
    """A few MB of conversation, written here. The end of it is what is on screen, so it
    has to end mid-file, not at a message boundary (T6)."""
    one = "## user\n" + "話" * 3000 + "\n\n## assistant\n" + "答" * 3000 + "\n\n"
    return one * ((mb * 1024 * 1024) // len(one)) + "## user\n最後一則\n"


def _big_session(mb: int = 3) -> tuple[store.Paths, store.Index, dict, str]:
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    body = _big_body(mb)
    paths, index = _index((hdr, body))
    return paths, index, hdr, body


def test_reading_the_preview_never_reads_the_whole_file(monkeypatch):
    """T6: 3 MB, and only the last step of it is ever in hand. The old code called
    `read_text` on the whole session.md and rendered all of it (0.72 s a move)."""
    paths, index, hdr, body = _big_session()
    assert len(body.encode("utf-8")) > 2 << 20

    def boom(*a, **kw):
        raise AssertionError("the preview must seek, not read the file")
    monkeypatch.setattr(pathlib.Path, "read_text", boom)

    preview = tui.agora_preview(paths, index, hdr["id"])
    assert len(preview.text.encode("utf-8")) <= tui.PREVIEW_CHUNK
    assert preview.text.endswith("最後一則")            # the end of the conversation
    assert preview.more() and "還有約" in preview.hint()


def test_a_step_is_cut_on_a_boundary_and_breaks_no_character():
    """T6: a step starts at a `## ` heading or after a blank line, and never inside a
    multibyte character - a broken one would show as a replacement character."""
    paths, index, hdr, _ = _big_session()
    preview = tui.agora_preview(paths, index, hdr["id"])
    assert preview.text.startswith("## ")
    assert "\ufffd" not in preview.text and preview.text == preview.text.strip("\ufffd")
    earlier = preview.step()                      # the step above
    assert earlier and preview.text.startswith("## ")
    assert "\ufffd" not in preview.text
    assert preview.text.count("## ") >= 2        # both steps are on screen now


def test_scrolling_to_the_top_adds_the_step_above_and_keeps_the_line():
    """T6: reaching the top reads one more step and puts it above, and the line the
    reader was on stays where it is - which is what the rendered height says, not the
    source's line count (review Y2, R4)."""
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, _big_body()))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")              # onto the row
            await pilot.pause(0.4)                 # the cursor comes to rest
            first = app.cache[hdr["id"]].text
            assert app.cache[hdr["id"]].more(), "there is more above"
            pane = app.query_one("#right", tui.PreviewText)
            pane.focus()
            await pilot.pause()
            was = pane.wrapped_document.height
            pane.scroll_to(y=10, animate=False)
            await pilot.pause(0.05)
            pane.scroll_to(y=0, animate=False)
            await pilot.pause(0.2)
            assert len(app.cache[hdr["id"]].text) > len(first), "a step was added above"
            grew = pane.wrapped_document.height - was
            assert grew > 0
            # Review R4: 捲到頂時原本最上面那一行的螢幕位置完全不變 (was at row 0, remains at row 0)
            original_line_screen_y = grew - pane.scroll_y
            assert original_line_screen_y == pytest.approx(0, abs=1), \
                "the line the reader was on has to stay at the same screen position"
    _run(go)


def test_three_steps_up_are_each_the_one_just_above():
    """Y6: the offset `read_tail` hands back has to be the one the next call wants. It
    used to be an absolute start, fed back as "bytes from the end", so the second step
    jumped to the top of the file and most of it could never be seen. Three steps in a
    row, each starting one chunk above the last - never at the file's start."""
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, _big_body(3)))
    preview = tui.agora_preview(paths, index, hdr["id"])

    seen, ats = [preview.text], [preview.at]
    for _ in range(3):
        assert preview.more(), "3 MB is more than one step"
        assert preview.step()
        assert preview.text.endswith(seen[-1]), "each step goes directly above the last"
        seen.append(preview.text)
        ats.append(preview.at)
    assert ats == sorted(ats, reverse=True) and len(set(ats)) == len(ats), ats
    assert 0 < ats[1] < ats[0], f"the second step must not jump to the start: {ats}"
    for above, below in zip(ats, ats[1:]):
        assert below > 0 and above - below <= tui.PREVIEW_CHUNK, \
            f"one chunk at a time, going backwards: {ats}"
    assert preview.text.startswith("## "), "every step lands on a heading"



def test_reaching_the_top_after_everything_is_here_adds_nothing():
    """Y1: `before == 0` is both "nothing read yet" and "the whole file is on screen".
    A short session is all here the first time, so going to the top again must not put
    the tail on top of itself."""
    normal = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "小的")
    paths, index = _index((normal, "## user\n第一句\n\n## assistant\n最後的回答\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            text = app.cache[normal["id"]].text
            assert text.count("最後的回答") == 1 and not app.cache[normal["id"]].more()
            pane = app.query_one("#right", tui.PreviewText)
            pane.focus()
            for _ in range(3):
                pane.scroll_to(y=3, animate=False)
                await pilot.pause(0.05)
                pane.scroll_to(y=0, animate=False)
                await pilot.pause(0.05)
            assert app.cache[normal["id"]].text.count("最後的回答") == 1
    _run(go)


def test_a_moving_cursor_reads_nothing(monkeypatch):
    """T6: only once the cursor has come to rest for ~150 ms. Moving through a list must
    not read or lay out anything per keystroke."""
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, _index_ = _index((hdr, _big_body()), (_hdr("01EEEEEEEEEEEEEEEEEEEEEEEE", "小的", sid="ses_b"), "## user\nx\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)
    calls: list[int] = []
    real = tui.read_tail

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(tui, "read_tail", counting)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for _ in range(6):
                await pilot.press("down")
                await pilot.press("up")
            assert calls == [], f"read while the cursor was moving: {len(calls)}"
            await pilot.pause(0.5)
            assert calls, "and once it rests, it does read"
    _run(go)


def test_the_import_tab_reads_the_tail_of_the_reading_version():
    """T6: the other tab reads the same way - the tail of `reading/<agent>/<id>.md`, and
    builds it in the background when it is not there yet."""
    paths = store.Paths.from_env()
    agent = FakeAgent("opencode", [], texts={"ses_big": ["問"]})
    path = cache.reading_path(paths, agent, "ses_big")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_big_body(), encoding="utf-8")

    preview = tui.import_preview(agent, "ses_big", True, paths, None)

    assert preview.pinned == "整份對話（閱讀版）"
    assert len(preview.text.encode("utf-8")) <= tui.PREVIEW_CHUNK
    assert preview.text.endswith("最後一則") and preview.more()

    # not there yet: it is built in the background, and then the same one step is read
    other = FakeAgent("claude", [], texts={"s2": ["新問題", "新回答"]})
    assert not cache.reading_path(paths, other, "s2").exists()
    built = tui.import_preview(other, "s2", True, paths, None)
    assert cache.reading_path(paths, other, "s2").is_file(), "the cache is built"
    assert built.text.endswith("新回答") and len(built.text.encode("utf-8")) <= tui.PREVIEW_CHUNK
    assert "新問題" in built.text or built.more()      # a short one is all here


# --- the filter has to work with an input method (T7 F3) -----------------------


def test_the_filter_narrows_while_the_chinese_is_typed():
    """T7 F3: an IME takes Enter for its candidate list, so a filter that needs Enter
    never gets it. Typing the word alone has to be enough - no Enter at all here."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert len(app.shown()) == 3
            await pilot.press("slash")
            await pilot.press("乙")                 # a committed Chinese character
            await pilot.pause(0.2)
            shown = [r.text for r in app.shown()]
            assert len(shown) == 1 and "乙" in shown[0], f"邊打邊篩就該只剩乙：{shown}"
            await pilot.press("甲")                 # and it re-filters on the next one
            await pilot.pause(0.2)
            assert app.shown() == [], "甲乙不是任何一列的一部分"
    _run(go)


def test_enter_is_only_how_the_filter_ends_when_the_ime_lets_it_through():
    """The IME's Enter usually does not reach the app, so nothing may depend on it; and
    when it does arrive it means "done", not "apply" (it was applied already)."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("丙")
            await pilot.pause(0.2)
            assert [r.text for r in app.shown()] == [r.text for r in app.rows["agora"]
                                                     if "丙" in r.text]
            await pilot.press("enter")              # the one an IME usually swallows
            await pilot.pause(0.1)
            assert not app.query_one("#filterbar").has_class("on"), "Enter 收工"
            assert len(app.shown()) == 1, "而結果留著"
            # and the bar is still where the user can clear it
            await pilot.press("slash")
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert len(app.shown()) == 3, "Esc 清掉，全部回來"
    _run(go)


def test_a_half_composed_character_does_not_break_the_filter():
    """While a character is being composed the IME has not committed anything; when it
    commits, the input changes and the filter follows. Nothing here may raise or empty
    the table for a value that is not a word yet."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("甲", "乙")           # "甲乙" is in no title
            await pilot.pause(0.1)
            assert app.shown() == [], "沒有符合的就空著"
            await pilot.press("backspace")          # the IME deletes a mis-picked character
            await pilot.pause(0.1)
            assert len(app.shown()) == 1, "回到只剩甲的那一列"
            await pilot.press("backspace")
            await pilot.pause(0.1)
            assert len(app.shown()) == 3, "整串刪掉就全部回來"
    _run(go)


# --- the content search waits for a pause, and only one runs (T7 P1) -----------


class SlowAgent(FakeAgent):
    """An agent whose search takes a while, and which can say how many were running.

    A real content search reads opencode's database or claude's jsonl, which is what
    makes "one scan per keystroke" worth fixing (review T7 P1).
    """

    def __init__(self, name, listed, texts=None, seconds=0.1):
        super().__init__(name, listed, texts=texts)
        self.seconds, self.calls = seconds, 0
        self.search_nothing = False
        self.last_keyword = None
        self.running = 0
        self.most_at_once = 0
        self.counts: list[int] = []    # how many results each call actually yielded
        self.lock = threading.Lock()

    def search_text(self, keyword, only=None):
        with self.lock:
            self.calls += 1
            self.running += 1
            self.most_at_once = max(self.most_at_once, self.running)
            mine = len(self.counts)
            self.counts.append(0)
            self.last_keyword = keyword
        try:
            if self.search_nothing:
                time.sleep(self.seconds * 4)      # scan the whole store, find nothing
                yielded = []
            else:
                yielded = [sid for sid, msgs in self.texts.items()
                           if (only is None or sid in only) and any(keyword in m for m in msgs)]
            for sid in yielded:
                time.sleep(self.seconds)      # results arrive as found, and slowly
                with self.lock:
                    self.counts[mine] += 1
                yield sid
        finally:
            with self.lock:
                self.running -= 1


def _slow_search_app(seconds=0.2):
    agent = SlowAgent("claude", [Listed("s1", "/tmp/p", "甲", None)],
                      texts={"s1": ["table 在這裡"]}, seconds=seconds)
    app, _ = _app([agent])
    return app, agent


def test_content_search_waits_for_the_typing_to_pause():
    """P1: `table` typed one letter at a time is one search, not five - the debounce
    starts it only after the last keystroke, and Enter does not start another."""
    app, agent = _slow_search_app()

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t", "slash")
            for ch in "table":                     # five keystrokes, quickly
                await pilot.press(ch)
            assert agent.calls == 0, "還沒停下來就不該開始掃描"
            await pilot.pause(0.6)                 # the debounce elapses: one scan
            assert agent.calls == 1, f"暫停之後應該只掃一次，掃了 {agent.calls} 次"
            await pilot.press("enter")             # the IME-swallowed key: not a second scan
            assert agent.calls == 1, f"Enter 不該再掃一次，掃了 {agent.calls} 次"
    _run(go)


def test_a_search_that_is_replaced_stops_instead_of_running_to_the_end():
    """`exclusive=True` only marks the old worker cancelled; the thread has to look.
    A real scan reads opencode's database and can take seconds, so the one that was
    replaced must stop where it is, not run to the end beside the new one (T7 P1)."""
    app, agent = _slow_search_app(seconds=0.2)                    # 0.2 s per result
    agent.texts = {f"s{n}": [f"t 第 {n} 筆"] for n in range(8)}   # eight results: 1.6 s
    agent.listed.extend(Listed(f"s{n}", "/tmp/p", f"第{n}", None) for n in range(1, 8))

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t", "slash")
            await pilot.press("t")
            await _wait(lambda: agent.counts and agent.counts[0] >= 1, pilot)  # it started
            await pilot.press("a")                 # and is replaced while it runs
            await _wait(lambda: agent.calls >= 2, pilot)   # the new one has started
            stopped_at = agent.counts[0]
            await pilot.pause(1.0)                 # five more results, if it kept going
            assert agent.counts[0] <= stopped_at + 1, \
                f"被取代的掃描應該停在原地，而不是繼續產出：{agent.counts}"
            assert agent.counts[0] < 8, \
                f"更不能把整份掃完：{agent.counts}"
            # they may overlap for the one result it takes the old worker to notice it
            # was cancelled; what must not happen is the whole old scan running on
            assert agent.most_at_once <= 2, f"同時在跑的不該超過兩個：{agent.most_at_once}"
    _run(go)


def test_the_title_filter_is_still_per_keystroke_and_never_scans_an_agent():
    """The debounce is for the content mode only: the title filter is a memory lookup,
    and it must stay immediate (T7 F3 was fixed that way)."""
    app, agent = _slow_search_app()

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "第一":                      # one key at a time, like a person
                await pilot.press(ch)
            await pilot.pause(0.1)                 # well inside the content debounce
            assert [r.key for r in app.shown()] == ["agora:01AAAAAAAAAAAAAAAAAAAAAAAA"], \
                "標題模式立即縮到剩第一個"
            assert agent.calls == 0, "標題模式不碰 agent"
    _run(go)


def test_a_scan_that_finds_nothing_does_not_stack_up():
    """Q1: `search_text` yields only what matches, so a word that matches nothing never
    comes back to the worker and cannot notice it was replaced. An input method commits
    one character at a time with a pause after each, so every pause used to start
    another whole scan beside the last. One scan at a time: the newest word waits."""
    app, agent = _slow_search_app(seconds=0.5)     # one scan takes about two seconds
    agent.search_nothing = True           # scan the whole store, yield nothing

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t", "slash")
            for ch in "xyz":                  # three characters, a pause after each
                await pilot.press(ch)
                await pilot.pause(0.4)        # past the debounce, like picking candidates
            assert agent.calls == 1, f"三個字應該只有一個掃描在跑，開了 {agent.calls} 個"
            assert agent.most_at_once == 1, f"同時在跑的不該超過一個：{agent.most_at_once}"
            # and the newest word goes once the running one is done - once
            await _wait(lambda: agent.calls == 2, pilot)
            await pilot.pause(0.3)
            assert agent.calls == 2, f"中間的字不該各補一次：{agent.calls}"
            assert agent.last_keyword == "xyz", "跑的是最後打的那個字"
            assert agent.most_at_once == 1, "從頭到尾都只有一個"
    _run(go)


def test_enter_before_the_pause_searches_once_not_twice():
    """Q2: typing a word and pressing Enter inside the debounce window. Enter searches
    right away and the timer must be stopped with it - otherwise the timer fires a
    moment later and the same word is scanned again."""
    app, agent = _slow_search_app(seconds=0.05)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t", "slash")
            await pilot.press("t")
            await pilot.press("enter")             # well inside FILTER_IDLE
            await pilot.pause(0.1)
            assert agent.calls == 1, f"Enter 搜一次就好，搜了 {agent.calls} 次"
            await pilot.pause(0.5)                 # past the debounce: nothing more
            assert agent.calls == 1, f"計時器應該被停掉，總共搜了 {agent.calls} 次"
    _run(go)


# --- preview-search-keys section 1: two sets of keys --------------------------

def test_tab_and_shift_tab_toggle_focus_between_list_and_preview():
    """Tab and shift+tab toggle focus between list and preview (spec「按鍵」)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 20)) as pilot:
            await pilot.pause()
            assert app.focused is app.query_one("#table")
            assert app.side() == "list"

            # Tab switches to preview
            await pilot.press("tab")
            assert app.focused is app.query_one("#right")
            assert app.side() == "preview"

            # Tab switches back to list
            await pilot.press("tab")
            assert app.focused is app.query_one("#table")
            assert app.side() == "list"

            # shift+tab switches to preview
            await pilot.press("shift+tab")
            assert app.focused is app.query_one("#right")
            assert app.side() == "preview"

            # shift+tab switches back to list
            await pilot.press("shift+tab")
            assert app.focused is app.query_one("#table")
            assert app.side() == "list"
    _run(go)


def test_bracket_changes_page_only_on_list_side():
    """[ ] switches tab only when focus is on the list side (spec「按鍵」)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.focused is app.query_one("#table")
            assert app.tab == "agora"

            # In list, ] switches to import tab
            await pilot.press("]")
            await pilot.pause()
            assert app.tab == "import"

            # In list, [ switches back to agora tab
            await pilot.press("[")
            await pilot.pause()
            assert app.tab == "agora"

            # Switch focus to preview
            await pilot.press("tab")
            assert app.focused is app.query_one("#right")
            assert app.side() == "preview"

            # In preview, ] and [ do not change tab
            await pilot.press("]")
            await pilot.pause()
            assert app.tab == "agora"

            await pilot.press("[")
            await pilot.pause()
            assert app.tab == "agora"

            # Switch back to list, then ] switches tab
            await pilot.press("tab")
            assert app.focused is app.query_one("#table")
            await pilot.press("]")
            await pilot.pause()
            assert app.tab == "import"
    _run(go)


def test_preview_side_ignores_list_keys():
    """List keys (d, m, a, ctrl+t, etc.) do nothing on the preview side (spec「按鍵」)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("tab")  # switch to preview
            assert app.focused is app.query_one("#right")
            assert app.side() == "preview"

            for act in ("delete", "merge", "mark_all", "search_mode", "mark", "primary", "filter", "pull", "push"):
                assert not app.check_action(act, ())

            # Press d: no Confirm window opened
            await pilot.press("d")
            await pilot.pause()
            assert not isinstance(app.screen, tui.Confirm)
            assert not app._last_spawned

            # Press m: no merge window opened
            await pilot.press("m")
            await pilot.pause()
            assert not isinstance(app.screen, tui.Confirm)
            assert not app._last_spawned

            # Press a: nothing marked
            await pilot.press("a")
            await pilot.pause()
            assert len(app.marked) == 0

            # Press ctrl+t: search mode not changed
            was_content = app.content
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.content == was_content
    _run(go)


def test_key_bar_differs_by_side_and_click_switches():
    """Key bar differs between list and preview, and mouse click switches it (spec「按鍵列」)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.side() == "list"
            keys_list = " ".join(str(app.query_one("#keys").render()).split())
            assert "m 合併" in keys_list and "enter 接續" in keys_list and "q 離開" in keys_list
            assert "[ ] 換頁" in keys_list and "Tab 切焦點" in keys_list

            # Click on #right to focus preview
            await pilot.click("#right")
            await pilot.pause()
            assert app.side() == "preview"
            keys_preview = " ".join(str(app.query_one("#keys").render()).split())
            assert "m 合併" not in keys_preview
            assert "enter 接續" not in keys_preview
            assert "Tab 切焦點" in keys_preview and "q 離開" in keys_preview

            # Click on #table to focus list
            await pilot.click("#table")
            await pilot.pause()
            assert app.side() == "list"
            keys_back = " ".join(str(app.query_one("#keys").render()).split())
            assert "m 合併" in keys_back and "enter 接續" in keys_back
    _run(go)


def test_confirm_modal_tab_moves_down_and_shift_tab_moves_up():
    """Confirm dialog: Tab moves down, shift+tab moves up, underlying page untouched (W4)."""
    app = _marked_app(None)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            window = app.screen
            assert isinstance(window, tui.Confirm)
            from textual.widgets import Checkbox, OptionList
            assert isinstance(app.focused, OptionList)

            # Tab moves down to Checkbox
            await pilot.press("tab")
            await pilot.pause()
            assert isinstance(app.focused, Checkbox)
            assert app.tab == "agora"

            # shift+tab moves back up to OptionList
            await pilot.press("shift+tab")
            await pilot.pause()
            assert isinstance(app.focused, OptionList)
            assert app.tab == "agora"
    _run(go)


def test_ask_text_modal_tab_does_not_affect_underlying_screen():
    """Modals other than Confirm: Tab and shift+tab do nothing to underlying screen (W4)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.tab == "agora"
            assert app.focused is app.query_one("#table")

            # Push AskText modal
            async def push_modal():
                await app.push_screen(tui.AskText("設定檔路徑", ""))
            app.call_after_refresh(push_modal)
            await pilot.pause(0.2)
            assert isinstance(app.screen, tui.AskText)
            modal_focused = app.focused
            assert modal_focused is not None

            # Press tab and shift+tab: must not change app.tab or underlying table focus
            await pilot.press("tab")
            await pilot.pause()
            assert app.tab == "agora"
            assert app.focused is modal_focused

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.tab == "agora"
            assert app.focused is modal_focused

            # Dismiss modal
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, tui.ModalScreen)
            assert app.tab == "agora"
            assert app.focused is app.query_one("#table")
    _run(go)


def test_filter_open_mouse_click_preview_esc_keeps_focus_and_filter():
    """R6: When filterbar is open and user clicks on preview pane, Esc leaves focus in preview and filter intact."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert app.query_one("#filterbar").has_class("on")
            assert app.focused is app.query_one("#filter")
            for ch in "01A":
                await pilot.press(ch)
            await pilot.pause(0.1)
            assert len(app.shown()) == 1

            # Click on preview pane while filterbar is still on
            await pilot.click("#right")
            await pilot.pause()
            assert app.side() == "preview"
            assert app.focused is app.query_one("#right")
            assert app.query_one("#filterbar").has_class("on")

            # Press escape while focus is in preview and filterbar is on
            await pilot.press("escape")
            await pilot.pause()
            assert app.side() == "preview"
            assert app.focused is app.query_one("#right")
            assert app.query_one("#filterbar").has_class("on")
            assert len(app.shown()) == 1
    _run(go)


def test_ctrl_t_in_filter_toggles_mode_without_changing_filter_text():
    """In filter input, ctrl+t toggles title/content mode without changing filter text."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert not app.content
            assert "標題" in str(app.query_one("#mode").render())

            for ch in "hello":
                await pilot.press(ch)
            await pilot.pause(0.05)
            assert app.query_one("#filter").value == "hello"

            # Press ctrl+t inside filter input
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.content is True
            assert "內文" in str(app.query_one("#mode").render())
            assert app.query_one("#filter").value == "hello"

            # Press ctrl+t again
            await pilot.press("ctrl+t")
            await pilot.pause()
            assert app.content is False
            assert "標題" in str(app.query_one("#mode").render())
            assert app.query_one("#filter").value == "hello"
    _run(go)


def test_tab_from_filter_preserves_filter_and_esc_in_preview_does_not_clear_it():
    """Tab from filter closes filterbar, keeps filter active; Esc in preview does not clear list filter (R6)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            # Press slash to open filter
            await pilot.press("slash")
            await pilot.pause()
            assert app.query_one("#filterbar").has_class("on")
            assert app.focused is app.query_one("#filter")
            assert app.side() == "list"

            # Type filter
            for ch in "01A":
                await pilot.press(ch)
            await pilot.pause(0.1)
            assert len(app.shown()) == 1

            # Press tab: switches focus to preview, closes filterbar, filter stays active
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is app.query_one("#right")
            assert app.side() == "preview"
            assert not app.query_one("#filterbar").has_class("on")
            assert len(app.shown()) == 1

            # Press escape while focus is in preview: does NOT clear list filter
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is app.query_one("#right")
            assert len(app.shown()) == 1

            # Tab back to list table: focus is on table
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is app.query_one("#table")
            assert len(app.shown()) == 1

            # Open filter again
            await pilot.press("slash")
            await pilot.pause()
            assert app.query_one("#filterbar").has_class("on")
            assert app.focused is app.query_one("#filter")

            # Esc while in filter clears filter
            await pilot.press("escape")
            await pilot.pause()
            assert not app.query_one("#filterbar").has_class("on")
            assert app.focused is app.query_one("#table")
            assert len(app.shown()) == 2

            # Now test Esc when filterbar is on and focus is on table
            await pilot.press("slash")
            await pilot.pause()
            for ch in "01A":
                await pilot.press(ch)
            await pilot.pause(0.1)
            # Focus table while filterbar still has 'on'
            app.query_one("#table").focus()
            await pilot.pause()
            assert app.query_one("#filterbar").has_class("on")
            assert app.focused is app.query_one("#table")
            assert app.side() == "list"
            # Press escape: clears filter because side() == 'list' and filterbar is on
            await pilot.press("escape")
            await pilot.pause()
            assert not app.query_one("#filterbar").has_class("on")
            assert len(app.shown()) == 2
    _run(go)


def test_filter_enter_does_not_trigger_primary():
    """Enter in filter bar submits/closes filter without triggering primary action (R1)."""
    app, _ = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert app.focused is app.query_one("#filter")
            assert not app.check_action("primary", ())
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert not app._last_spawned
            assert not isinstance(app.screen, tui.Confirm)
            assert app.focused is app.query_one("#table")
            assert app.check_action("primary", ())
    _run(go)


# --- Section 2: PreviewText, cursor, navigation, and loading --------------------

def test_preview_cursor_movement_short_lines():
    """Scenario: 游標移動 (2.3). Short lines, j/k/up/down moves cursor, and line is highlighted."""
    lines = [f"## line {i}" for i in range(20)]
    body = "\n".join(lines) + "\n"
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "短行")
    paths, index = _index((hdr, body))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is pane
            assert pane.highlight_cursor_line is True

            last_line = pane.document.line_count - 1
            assert pane.cursor_location[0] == last_line

            await pilot.press("k", "k", "k")
            await pilot.pause()
            assert pane.cursor_location[0] == last_line - 3

            await pilot.press("up")
            await pilot.pause()
            assert pane.cursor_location[0] == last_line - 4

            await pilot.press("j", "j")
            await pilot.pause()
            assert pane.cursor_location[0] == last_line - 2

            await pilot.press("down")
            await pilot.pause()
            assert pane.cursor_location[0] == last_line - 1
    _run(go)


def test_preview_g_loads_all_content_and_G_moves_to_end():
    """Scenario: 到最前面 (2.3). g loads all chunks in one go, cursor at (0, 0), hint goes away. G goes to end."""
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, _big_body(3)))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            assert app.cache[hdr["id"]].more()
            assert "還有約" in str(app.query_one("#hint", tui.Static).content)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()

            await pilot.press("g")
            await pilot.pause(0.3)
            assert not app.cache[hdr["id"]].more(), "all chunks loaded"
            assert str(app.query_one("#hint", tui.Static).content) == "", "hint is gone"
            assert pane.cursor_location == (0, 0)
            assert pane.scroll_y == 0

            await pilot.press("G")
            await pilot.pause()
            last_line = pane.document.line_count - 1
            assert pane.cursor_location[0] == last_line
    _run(go)


def _steps_body(steps: int = 3) -> str:
    """A few steps of conversation, so one step is only a part of it (T6).

    Its lines are long, like a real conversation's paragraphs, so they wrap - that is the
    case where `k` has to move a row on the screen, not a line in the file (review S3).
    """
    one = "## user\n" + "話" * 3000 + "\n\n## assistant\n" + "答" * 3000 + "\n\n"
    return one * ((steps * tui.PREVIEW_CHUNK) // len(one)) + "## user\n最後一則\n"


def _short_body(lines: int = 9000) -> str:
    """The same, in lines short enough not to wrap - what 「游標移動」 is written for."""
    return "".join(f"## line {i}\n" for i in range(lines))


def _screen_row(pane: tui.PreviewText, location) -> int:
    """Where a place in the document is on the screen: its wrapped row, less the scroll."""
    return pane.wrapped_document.location_to_offset(location).y - int(pane.scroll_y)


def _at_the_top_of_the_pane(pane: tui.PreviewText) -> None:
    """Put the cursor on the first loaded row and the view at its top, without that scroll
    being read as the reader reaching the top (which is a load of its own)."""
    pane._suppress_scroll_load = True
    pane.move_cursor((0, 0))
    pane.scroll_to(y=0, animate=False)
    pane._suppress_scroll_load = False
    assert pane.cursor_location[0] == 0 and pane.scroll_y == 0


def _go_to_row(app, key: str) -> None:
    """Put the list cursor on a row by key, whatever order the rows are in."""
    keys = [row.key for row in app.shown()]
    app.query_one("#table").move_cursor(row=keys.index(key))


@pytest.mark.parametrize("body", [_steps_body(), _short_body()], ids=["wrapped-lines", "short-lines"])
def test_prepend_chunk_does_not_jump_when_pressing_k_at_top(body):
    """Scenario: 按 k 補前一段不跳 (review R4, S3).

    The row the cursor was on moves down by exactly one row on the screen, and the cursor
    ends up on the row above it - the last row of the step that was added. With a long line
    that wraps, that row is not the first segment of the line above, so the cursor has to be
    placed by the wrapped document (what review S3 measured: screen row -124).
    """
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, body))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            assert app.cache[hdr["id"]].more(), "there is more above"
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            _at_the_top_of_the_pane(pane)
            was_lines = pane.document.line_count

            await pilot.press("k")
            await pilot.pause(0.3)

            added = pane.document.line_count - was_lines
            assert added > 0, "a step was added above"
            assert len(app.cache[hdr["id"]]._chunks) == 2, "and only one step"
            assert _screen_row(pane, (added, 0)) == 1, \
                "the line the cursor was on moves down by exactly one row"
            assert pane.cursor_location[0] == added - 1, "the cursor is on the last line above"
            assert _screen_row(pane, pane.cursor_location) == 0
            assert 0 <= _screen_row(pane, pane.cursor_location) < pane.content_size.height, \
                "and it is on the screen"
    _run(go)


def test_prepend_chunk_triggers_on_page_up_at_top():
    """PageUp at line 0 triggers loading earlier chunk, and the cursor stays on the screen."""
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, _steps_body()))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            assert app.cache[hdr["id"]].more(), "there is more above"
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            _at_the_top_of_the_pane(pane)
            before_len = len(app.cache[hdr["id"]].text)

            await pilot.press("pageup")
            await pilot.pause(0.3)
            assert len(app.cache[hdr["id"]].text) > before_len, "pageup loaded earlier chunk"
            assert len(app.cache[hdr["id"]]._chunks) == 2, "one page up, one step"
            row = _screen_row(pane, pane.cursor_location)
            assert 0 <= row < pane.content_size.height, \
                f"a page up is a page of rows on the screen, so the cursor is on it ({row})"
            assert pane.cursor_location[0] > 0, "and it went up into the step that was added"
    _run(go)


def test_preview_syntax_fallback_no_tree_sitter(monkeypatch):
    """When tree-sitter is missing, PreviewText falls back to plain text without error."""
    from textual.widgets import _text_area
    monkeypatch.setattr(_text_area, "TREE_SITTER", False)
    pt = tui.PreviewText("## user\nhello", id="right")
    assert pt.document is not None
    assert "## user" in pt.document.text


def test_preview_syntax_fallback_language_does_not_exist(monkeypatch):
    """When tree-sitter is present but markdown grammar is missing, falls back to language=None."""
    from textual.widgets import _text_area
    monkeypatch.setattr(_text_area, "TREE_SITTER", True)
    monkeypatch.setattr(_text_area, "get_language", lambda lang: None)
    pt = tui.PreviewText("## user\nhello", id="right")
    assert pt.language is None
    assert "## user" in pt.document.text


def test_preview_highlight_cursor_line_only_when_focused():
    """W13: cursor line is only highlighted when preview has focus."""
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "測試")
    paths, index = _index((hdr, "## user\nhello\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            pane = app.query_one("#right", tui.PreviewText)
            assert app.focused is app.query_one("#table")
            assert pane.highlight_cursor_line is False

            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is pane
            assert pane.highlight_cursor_line is True

            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is app.query_one("#table")
            assert pane.highlight_cursor_line is False
    _run(go)


def test_preview_get_line_user_and_assistant_colors():
    """## user and ## assistant headings are drawn in their own colours (PM 10-04).

    They are neither cyan nor green - those are the agent and merge colours in the list -
    and neither is any other colour the list already gives a meaning to.
    """
    pt = tui.PreviewText("## user\n第 1 則\n## assistant\n回答\n# 其他\n普通文字", id="right")
    line0 = pt.get_line(0)
    line2 = pt.get_line(2)
    line4 = pt.get_line(4)

    assert any(span.style == "bold #87afff" for span in line0.spans)
    assert any(span.style == "bold #d787ff" for span in line2.spans)
    assert not any(span.style in ("bold #87afff", "bold #d787ff") for span in line4.spans)


def test_the_two_headings_are_drawn_in_their_own_colour():
    """The override of `_build_highlight_map` takes the theme's `heading` off those two rows.

    Tree-sitter styles a heading's text as `heading`, and `_render_line` applies that after
    `get_line`, so without the override the two colours would never be seen (spec「預覽區的游標
    與捲動」: the two MUST look different). So it is the drawn row that has to be checked.
    """
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "標題")
    paths, index = _index((hdr, "## user\n一句話\n\n## assistant\n答\n\n# 其他\n普通\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            heading = pane._theme.syntax_styles["heading"]
            for row, colour in ((0, "#87afff"), (3, "#d787ff")):
                drawn = {style for text, style in
                         ((seg.text, seg.style) for seg in pane.render_line(row)) if text.strip()}
                assert any(style.color.name == colour and style.bold for style in drawn), \
                    f"row {row} is not drawn in {colour}: {drawn}"
                assert heading.color not in {style.color for style in drawn}, \
                    f"row {row} is drawn with the theme's heading style: {drawn}"
            # the other headings keep the theme's own style - only these two are ours
            other = {style for text, style in
                     ((seg.text, seg.style) for seg in pane.render_line(6)) if text.strip()}
            assert heading.color in {style.color for style in other}
    _run(go)


def test_a_session_ending_in_a_code_block_loads_without_raising():
    """S1: a fenced code block ends one line past the last one, so the highlight map asked
    for a line the document does not have - IndexError, and with it the whole interactive
    mode (spec「以 code block 結尾」)."""
    pane = tui.PreviewText(id="right")
    pane.load_text("## assistant\n```\nx\n```")

    assert pane.document.lines[-1] == "```"
    assert max(pane._highlights, default=-1) < pane.document.line_count, \
        "the highlight map only holds lines the document has"


def test_selecting_a_session_that_ends_in_a_code_block_does_not_crash_the_app():
    """S1: the same, on the screen: an assistant's answer often ends with a code block."""
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "以 code block 結尾")
    paths, index = _index((hdr, "## user\n幫我改\n\n## assistant\n好：\n```python\nprint(1)\n```"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            assert pane.document.text.endswith("```"), pane.document.text[-40:]
            await pilot.press("tab")
            await pilot.pause()
            await pilot.press("k")            # and the cursor can walk over it
            await pilot.pause(0.2)
            assert app.query_one("#right", tui.PreviewText) is pane
    _run(go)


def test_switching_to_another_session_reads_one_step_and_not_two():
    """S2: swapping sessions must read the tail once, as opening one does (T6).

    `load_text` puts the scroll back at the top, which is what reaching the top of the pane
    looks like - so it read a second step, and did another insert and scroll restore for it.
    """
    body = _steps_body()
    first, second = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "大的一"), _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "大的二")
    paths, index = _index((first, body), (second, body))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            for key in (first["id"], second["id"], first["id"]):
                _go_to_row(app, key)
                await pilot.pause(0.4)
                assert len(app.cache[key]._chunks) == 1, \
                    f"one step for {key}: {[len(c) for c in app.cache[key]._chunks]}"
    _run(go)


def test_a_step_in_the_middle_of_the_file_is_not_read_as_front_matter(monkeypatch, tmp_path):
    """W10: `_no_header` is for the top of the file only.

    A step that happens to start with `---` - a markdown rule - is conversation, and the
    front matter above it is already gone; eating up to the next `---` would take the content
    with it, and section 3 counts matches against what is on screen (design「分段載入」).
    """
    step = "---\n" + "話" * 40 + "\n---\n最後一段\n"
    # The tail is read from a byte earlier than the step and the cut drops that byte, so the
    # step starts exactly at the rule - in the middle of the file, not at its head.
    monkeypatch.setattr(tui, "PREVIEW_CHUNK", len(step.encode("utf-8")) + 1)
    path = tmp_path / "session.md"
    path.write_text("## user\n前面的一段對話\n" + step, encoding="utf-8")

    preview = tui.Preview(path)
    assert preview.step() is not None
    assert preview.at, "the step did not start at the file's head"

    assert "話" * 40 in preview.text, f"a rule inside the file is not front matter: {preview.text!r}"
    assert preview.text.endswith("最後一段")


def test_preview_staleness_detection(tmp_path):
    """W11: Preview records file size and mtime and detects staleness."""
    f = tmp_path / "test.md"
    f.write_text("hello", encoding="utf-8")
    p = tui.Preview(f)
    assert not p.is_stale()
    time.sleep(0.02)
    f.write_text("hello world modified", encoding="utf-8")
    assert p.is_stale()


def test_a_session_whose_file_changed_is_read_again_when_it_is_selected_again():
    """W11: the same, on the screen: coming back to a row whose file changed reads it again.

    Otherwise the preview is the one from before, and section 3 counts matches against a file
    that is no longer what it read (design「分段載入」).
    """
    other, changed = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "別的"), _hdr("01CCCCCCCCCCCCCCCCCCCCCCCC", "改了的")
    paths, index = _index((other, "## user\n別的內容\n"),
                          (changed, "## user\n" + "原來的內容" * 200 + "\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _go_to_row(app, changed["id"])
            await pilot.pause(0.4)
            assert "原來的內容" in app.cache[changed["id"]].text

            session = paths.mirror / changed["id"].split(":")[1] / "session.md"
            session.write_text(h.dump_document(changed, "## user\n改過了\n"), encoding="utf-8")

            _go_to_row(app, other["id"])
            await pilot.pause(0.4)
            _go_to_row(app, changed["id"])
            await pilot.pause(0.4)
            assert app.cache[changed["id"]].text == "## user\n改過了", \
                f"the file changed under it: {app.cache[changed['id']].text[:40]!r}"
            assert not app.cache[changed["id"]].is_stale()
    _run(go)


def test_the_hint_says_that_k_and_g_load_what_is_above():
    """spec「預覽區的游標與捲動」: the hint MUST mention `k` and `g`, not only the scroll.

    And it goes away once everything is here (T6).
    """
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "大的")
    paths, index = _index((hdr, _steps_body(2)))
    preview = tui.agora_preview(paths, index, hdr["id"])

    hint = preview.hint()
    assert "k" in hint and "g" in hint, f"the hint names the keys too: {hint}"
    assert "還有約" in hint
    while preview.more():
        preview.step()
    assert preview.hint() == ""


def test_the_preview_adds_its_own_keys_and_inherits_the_rest():
    """PreviewText's own BINDINGS are the keys it adds; TextArea's are inherited.

    Copying them into the list would do nothing but make every reader wonder which of the two
    copies a key comes from (review Low). The keys TextArea brings are the ones the spec keeps:
    PgUp／PgDn, ←／→, Home／End, selecting and copying (W7).
    """
    pane = tui.PreviewText("## user\n一句話\n")
    keys = {key: binding.action for _, binding in pane._bindings
            for key in binding.key.split(",")}

    assert [binding.key for binding in tui.PreviewText.BINDINGS] == ["j", "k", "g", "G",
                                                                    "slash", "n", "N"]
    for key, action in (("j", "cursor_down"), ("k", "cursor_up"), ("g", "top"), ("G", "bottom"),
                        ("slash", "search"), ("n", "search_next"), ("N", "search_prev"),
                        ("up", "cursor_up"), ("down", "cursor_down"), ("left", "cursor_left"),
                        ("right", "cursor_right"), ("pageup", "cursor_page_up"),
                        ("pagedown", "cursor_page_down"), ("home", "cursor_line_start"),
                        ("end", "cursor_line_end"), ("shift+right", "cursor_right(True)"),
                        ("f6", "select_line"), ("ctrl+c", "copy")):
        assert keys.get(key) == action, f"{key} -> {keys.get(key)}"


def test_a_step_that_is_blank_reads_as_nothing_to_show(monkeypatch, tmp_path):
    """`step()` answers None when there is nothing to show (review Low).

    It used to answer a single space for "something was read, but it is blank" - a piece of
    text like any other, which the caller then wrote into the pane.
    """
    monkeypatch.setattr(tui, "PREVIEW_CHUNK", 40)
    path = tmp_path / "session.md"
    path.write_text("## user\n問\n" + "\n" * 100, encoding="utf-8")

    preview = tui.Preview(path)

    assert preview.step() is None
    assert preview.text == "", "and nothing was put on screen"


def test_benchmark_3mb_g_press():
    """Benchmark 3 MB g press: `g` loads it all in one go, not a step at a time.

    The limit here is loose (5 s) on purpose - it is there to catch the quadratic way of
    doing it (a step at a time is 15-30 s), not to measure the machine. The 1.5 s of the
    design is measured by hand in 4.2, on a real 3 MB session, in a pane.
    """
    import time
    mb = 3
    one = "## user\n" + "話" * 3000 + "\n\n## assistant\n" + "答" * 3000 + "\n\n"
    body = one * ((mb * 1024 * 1024) // len(one)) + "## user\n最後一則\n"
    hdr = _hdr("01DDDDDDDDDDDDDDDDDDDDDDDD", "3MB")
    paths, index = _index((hdr, body))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()

            t0 = time.perf_counter()
            await pilot.press("g")
            for _ in range(50):
                if not app.cache[hdr["id"]].more():
                    break
                await pilot.pause(0.05)
            t1 = time.perf_counter()
            elapsed = t1 - t0
            print(f"\n[BENCHMARK] 3 MB g press time: {elapsed:.3f} s")
            assert elapsed <= 5, f"3 MB g took {elapsed:.3f} s, which exceeds 5 s"
            assert not app.cache[hdr["id"]].more()
            assert pane.cursor_location == (0, 0)
    _run(go)



# --- Section 3: the search in the preview ----------------------------------------


def _far_body(front: str = "最早的表格", tail: str = "最後的表格", steps: int = 3,
              middle: str = "") -> str:
    """A conversation with a line at its very front and one in the last step, and bulk between.

    So a word in the first line is a whole file above the tail that is read first, and a word in
    the last line is in it - which is what makes "the first search does not load the front" and
    "jumping to what is not loaded yet" two different things to test.
    """
    one = "## user\n" + "話" * 3000 + "\n\n## assistant\n" + "答" * 3000 + "\n\n"
    return (f"## user\n{front}\n\n" + (f"## user\n{middle}\n\n" if middle else "")
            + one * ((steps * tui.PREVIEW_CHUNK) // len(one.encode("utf-8")))
            + f"## user\n{tail}\n")


def _three_matches_body() -> str:
    """One short session with three matches, at rows 4, 7 and 10 - so `n` and `N` have a way to
    go and both ends to wrap at, all inside the one step that is read."""
    return ("## user\n先問一句\n\n## assistant\n表格在這裡\n\n## user\n還有表格\n\n"
            "## assistant\n表格第三次\n\n## user\n結尾\n")


def _count(app) -> str:
    return str(app.query_one("#count", Static).content)


def _drawn(pane: tui.PreviewText, row: int) -> list[tuple[str, str]]:
    """The drawn row as the screen sees it: (text, style) per segment."""
    return [(seg.text, str(seg.style)) for seg in pane.render_line(_screen_row(pane, (row, 0)))]


async def _focus_pane(app, pilot) -> tui.PreviewText:
    """Tab until the preview pane has the focus - the list may be holding it."""
    for _ in range(3):
        if app.focused is app.query_one("#right"):
            break
        await pilot.press("tab")
        await pilot.pause()
    return app.query_one("#right", tui.PreviewText)


async def _search(pilot, word: str) -> None:
    """`/`, the word, Enter - what a reader does."""
    await pilot.press("slash")
    for ch in word:
        await pilot.press(ch)
    await pilot.press("enter")
    await pilot.pause(0.2)


def _search_app(body: str, title: str = "搜尋") -> tuple:
    """An app with one session whose conversation is `body` - the row the cursor starts on."""
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", title)
    paths, _index_ = _index((hdr, body))
    return hdr, tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)


def test_the_count_agrees_with_what_is_on_screen_at_every_step(tmp_path):
    """W1: for every `at`, the matches above + the ones on screen == the whole session's.

    Nothing converts an offset anywhere, so this is an invariant of the counting rather than a
    coincidence - and it has to hold for the awkward bodies too: a step cut on a blank line and
    one cut on a heading, Chinese, `ß` next to its own upper-case spelling, and a word that is
    in the front matter as well (spec「預覽區搜尋」, design「搜尋：計數」).
    """
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "有表格的標題")   # the word is in the front matter too
    body = ("## user\n最早的表格\n\n## assistant\nStraße 與 STRASSE\n\n"
            + ("## user\n" + "話" * 600 + "\n\n") * 40           # steps cut on \\n\\n
            + ("## assistant\n" + "答" * 600 + "\n\n") * 30      # and on \\n## 
            + "## user\n最後的表格\n")
    paths, _index_ = _index((hdr, body))
    preview = tui.Preview(paths.mirror / hdr["id"].split(":")[1] / "session.md")
    preview.step()

    words = ("表格", "最早的表格", "Straße", "STRASSE", "straße", "strasse", "ss", "沒有���字")
    steps = 1
    while True:
        for word in words:
            total, above = preview.counts(word)
            on_screen = sum(1 for _ in tui.find_in_lines(preview.text.splitlines(), word))
            assert above + on_screen == total, \
                f"{word!r} after {steps} step(s): {above} above + {on_screen} on screen != {total}"
        if not preview.more():
            break
        preview.step()
        steps += 1
    assert preview.at == 0 and steps > 2, "and the steps really did walk the whole file"
    assert preview.counts("表格")[0] == 2, "the front matter is not conversation"
    assert preview.counts("最早的表格")[0] == 1 and preview.counts("沒有這個字")[0] == 0
    assert "最早的表格" in preview.text, "the content itself is untouched"


def test_the_search_goes_to_the_match_and_marks_the_loaded_ones():
    """Scenario: 找到並跳過去 (3.3).

    The pane opens with the cursor at the end, so the first search goes up to the match nearest
    the cursor - the last one in the file (review R2) - and every match on screen is underlined
    and bolded, not only the one the cursor is on.
    """
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")

            assert app.focused is pane, "Enter 送出後焦點回到預覽區"
            assert _count(app) == "第 3 個／共 3 個", "the one nearest the end is the third"
            assert not pane.selection.is_empty, "目前那一個用 selection 標"
            assert pane.document.get_line(pane.selection.start[0]) == "表格第三次"
            marked = {row for row, _s, _e in tui.find_in_lines(pane.document.lines, "表格")}
            assert marked == {4, 7, 10}, marked
            drawn = _drawn(pane, 7)                # a match that is not the current one
            assert [text for text, _style in drawn][:2] == ["還有", "表格"], drawn
            assert all("underline" in style for text, style in drawn if text == "表格"), drawn
            assert all("underline" not in style for text, style in drawn if text == "還有"), drawn
            # the mark survives the syntax colours and the cursor line: it is not a colour
            assert any("underline" in style for _t, style in _drawn(pane, 10)), "on the cursor row too"
            await _search(pilot, "表格在這裡")            # a word inside a line, one match only
            assert _count(app) == "第 1 個／共 1 個"
            assert pane.document.get_line(pane.selection.start[0]) == "表格在這裡"
    _run(go)


def test_n_and_walk_to_the_next_and_the_previous_one_wrapping_at_the_ends():
    """`n` goes on from the cursor, `N` goes back, and at either end it wraps (spec「預覽區搜尋」)."""
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")
            assert _count(app) == "第 3 個／共 3 個"

            await pilot.press("n")                       # from the last one, round to the first
            await pilot.pause()
            assert _count(app) == "第 1 個／共 3 個"
            assert pane.document.get_line(pane.selection.start[0]) == "表格在這裡"

            await pilot.press("n")                       # then on to the second
            await pilot.pause()
            assert _count(app) == "第 2 個／共 3 個"

            await pilot.press("N")                       # back to the first
            await pilot.pause()
            assert _count(app) == "第 1 個／共 3 個"
            await pilot.press("N")                       # and round to the last
            await pilot.pause()
            assert _count(app) == "第 3 個／共 3 個"
            assert pane.document.get_line(pane.selection.start[0]) == "表格第三次"

            await pilot.press("j")                       # the selection goes, the marks stay
            await pilot.pause()
            assert pane.selection.is_empty
            assert any("underline" in style for _t, style in _drawn(pane, 4))
    _run(go)


def test_the_first_search_takes_the_match_nearest_the_end_and_reads_no_more():
    """Scenario: 第一次搜尋找離尾端最近的 - and does not load the front for it (review R2)."""
    hdr, app = _search_app(_far_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")

            assert _count(app) == "第 2 個／共 2 個", "the one at the end is the second"
            assert pane.document.get_line(pane.selection.start[0]) == "最後的表格"
            assert len(app.cache[hdr["id"]]._chunks) == 1, "one step is still one step"
            assert "最早的表格" not in pane.document.text, "and the front is not on screen"
    _run(go)


def test_a_word_only_in_the_unread_part_goes_up_to_the_nearest_of_the_two():
    """Review R2: the first search goes up to the match nearest the cursor, not to the first one
    in the file. Both are unread here, and reading down to either reads the whole of it - they
    are at the front - so what is being tested is which of the two it goes to."""
    hdr, app = _search_app(_far_body(front="最早的獨有的字", middle="中間的獨有的字",
                                     tail="最後的普通的一句話"))

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await _focus_pane(app, pilot)
            await _search(pilot, "獨有的字")

            assert _count(app) == "第 2 個／共 2 個", "the nearer of the two above"
            assert pane.document.get_line(pane.selection.start[0]) == "中間的獨有的字"
            assert app.cache[hdr["id"]].at == 0, "both are at the front, so it read all of it"
            row = _screen_row(pane, pane.selection.start)
            assert 0 <= row < pane.content_size.height, f"and it is on the screen ({row})"
    _run(go)


def test_entering_a_word_that_is_only_at_the_front_loads_down_to_it_once():
    """Scenario: 跳到還沒載入的地方 - one load, not a step at a time (W3)."""
    hdr, app = _search_app(_far_body(front="最早的獨有的字", tail="最後的普通的一句話"))

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "獨有的字")

            assert _count(app) == "第 1 個／共 1 個"
            assert pane.document.get_line(pane.selection.start[0]) == "最早的獨有的字"
            assert app.cache[hdr["id"]].at == 0, "read down to it"
            assert str(app.query_one("#hint", Static).content) == "", "and there is nothing above it"
            row = _screen_row(pane, pane.selection.start)
            assert 0 <= row < pane.content_size.height, f"and it is on the screen ({row})"
    _run(go)


def test_a_word_that_is_only_in_the_front_matter_is_not_found(tmp_path):
    """Scenario: 檔頭不算 - the front matter is metadata, not conversation."""
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "只有檔頭有這個字")
    paths, _index_ = _index((hdr, "## user\n普通的一句話\n\n## assistant\n回答\n"))
    path = paths.mirror / hdr["id"].split(":")[1] / "session.md"
    assert "只有檔頭有這個字" in path.read_text(encoding="utf-8").split("---")[1]
    preview = tui.Preview(path)
    preview.step()

    assert preview.counts("只有檔頭有這個字") == (0, 0)
    assert tui.Preview(path).counts("普通的一句話") == (1, 1)   # the first step is the whole file


def test_escape_closes_the_box_first_and_then_only_the_highlights():
    """Scenario: 清掉標亮 - Esc in the box closes it; Esc again clears the marks, cursor and all."""
    body = ("## user\n先問一句\n\n## assistant\n表格在這裡\n\n## user\n還有表格\n\n## user\n結尾\n")
    hdr, app = _search_app(body)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")
            assert _count(app)

            await pilot.press("slash")
            await pilot.press("x")
            await pilot.pause()
            assert app.focused is app.query_one("#search"), "「/」打開搜尋框並取得焦點"
            await pilot.press("escape")
            await pilot.pause()
            assert app.focused is pane, "Esc 關掉搜尋框，焦點回到預覽區"
            assert _count(app) == "第 2 個／共 2 個", "and the count is still there"

            where = pane.cursor_location
            await pilot.press("escape")
            await pilot.pause()
            assert _count(app) == "", "Esc 清掉標亮與計數"
            assert app.focused is pane and pane.cursor_location == where, "游標留在原地"
            assert pane._query == "" and not any("underline" in s for _t, s in _drawn(pane, 4))
    _run(go)


def test_another_session_starts_without_the_search():
    """Scenario: 換 Session - the highlights and the count go with the row they were made on."""
    body = "## user\n表格在這裡\n\n## assistant\n回答\n"
    other = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "別的", sid="ses_b")
    hdr = _hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "有表格的")
    paths, _index_ = _index((hdr, body), (other, "## user\n沒有表格\n"))
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            _go_to_row(app, hdr["id"])
            await pilot.pause(0.4)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")
            assert _count(app) == "第 1 個／共 1 個"

            await pilot.press("tab")                    # back to the list
            await pilot.pause()
            _go_to_row(app, other["id"])
            await pilot.pause(0.4)
            assert _count(app) == "", "the count goes with the row"
            pane = app.query_one("#right", tui.PreviewText)
            assert pane._query == "" and pane.selection.is_empty
            assert not any("underline" in s for _t, s in _drawn(pane, 0))

            _go_to_row(app, hdr["id"])            # and back: it starts clean again
            await pilot.pause(0.4)
            await _focus_pane(app, pilot)
            await _search(pilot, "表格")
            assert _count(app) == "第 1 個／共 1 個"
            await pilot.press("tab")               # `]` is a list key: back to the list first
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause(0.4)
            assert _count(app) == "", "another tab is another session"
    _run(go)


def test_a_word_that_is_not_there_says_so_and_leaves_the_cursor_alone():
    """spec「預覽區搜尋」: 找不到時 MUST 說「找不到」，游標不動."""
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            pane = app.query_one("#right", tui.PreviewText)
            await pilot.press("tab")
            await pilot.pause()
            where = pane.cursor_location

            await _search(pilot, "沒有這個字")
            assert _count(app) == "找不到"
            assert pane.cursor_location == where

            await _search(pilot, "表格")           # and a word that is there works again
            assert _count(app) == "第 3 個／共 3 個"
    _run(go)


def test_the_letters_in_the_search_box_are_typing():
    """The box is an input: `n`, `N`, `j`, `k`, `q` and `/` are words, not keys (spec「預覽區搜尋」)."""
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            await pilot.press("tab")
            await pilot.pause()
            await pilot.press("slash")
            for ch in "nNjkq/表格":
                await pilot.press(ch)
            await pilot.pause()

            assert app.focused is app.query_one("#search"), "the box has the focus"
            assert app.query_one("#search", Input).value == "nNjkq/表格"
            assert _count(app) == "", "nothing is searched until Enter"
            assert app.query_one("#right").document.text, "and the app is still here"
            await pilot.press("tab")
            await pilot.pause()
            assert app.query_one("#search", Input).value == "", "Tab 丟掉沒送出的字"
            assert app.focused is app.query_one("#table")
            assert not app.query_one("#searchbar").has_class("on"), "and the box goes"
            await pilot.press("n", "N")             # and on the list side they are not keys
            await pilot.pause()
            assert app.focused is app.query_one("#table") and _count(app) == ""
    _run(go)


def test_the_key_bar_says_the_keys_of_the_search_box_while_it_is_open():
    """spec「按鍵」: the key bar shows only the keys that work where the focus is (W8)."""
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            await pilot.press("tab")
            await pilot.pause()
            bar = str(app.query_one("#keys", Static).content)
            assert "搜尋" in bar and "n/N" in bar and "清標亮" in bar, bar

            await pilot.press("slash")
            await pilot.pause()
            bar = str(app.query_one("#keys", Static).content)
            assert "送出" in bar and "關掉" in bar, bar
            assert "n/N" not in bar and "j/k" not in bar, bar
    _run(go)


def test_the_search_runs_again_when_the_whole_conversation_replaces_the_last_message(monkeypatch):
    """R5: the same session whose content was replaced is searched again with the same word.

    The count first says it is only looking at the last message; when the reading version
    arrives the matches are counted again and the line says which change it was.
    """
    agent = FakeAgent("claude", [Listed("s1", "/tmp/p", "未匯入的", "2026-10-02T00:00:00Z")],
                      last=("user", "表格的問題"), texts={"s1": ["表格的問題", "回答也有表格"]})
    gate, real = threading.Event(), agent.export

    def gated(session_id):
        gate.wait(10)                 # the reading version is built only when the test says so
        return real(session_id)
    agent.export = gated
    paths = store.Paths.from_env()
    app = tui.AgoraApp(paths, FakeCli(), agents=[agent], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.3)
            assert "載入中" in str(app.query_one("#pinned", Static).content)

            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")
            assert _count(app) == "第 1 個／共 1 個（只有最後一則）", _count(app)

            gate.set()
            for _ in range(30):                    # the reading version is built in a thread
                if "整份對話" in str(app.query_one("#pinned", Static).content):
                    break
                await pilot.pause(0.1)
            await pilot.pause(0.2)
            assert _count(app) == "第 2 個／共 2 個  已換成整份對話", _count(app)
            pane = app.query_one("#right", tui.PreviewText)
            assert pane.document.get_line(pane.selection.start[0]) == "回答也有表格"
    _run(go)


def test_reading_the_rows_again_searches_again_and_says_the_content_changed():
    """R5: the same after an action, which re-reads the index and the previews (reload)."""
    hdr, app = _search_app(_three_matches_body())

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            await pilot.press("tab")
            await pilot.pause()
            await _search(pilot, "表格")
            assert _count(app) == "第 3 個／共 3 個"

            app.reload()                       # what an action does when it is done
            await pilot.pause(0.5)
            assert _count(app) == "第 3 個／共 3 個  內容已更新", _count(app)

            app.clear_preview_search()          # with no search there is nothing to say
            app.reload()
            await pilot.pause(0.5)
            assert _count(app) == ""
    _run(go)


def test_a_whole_conversation_that_cannot_be_read_does_not_stay_on_loading(monkeypatch):
    """spec「預覽區搜尋」: 整份讀取失敗時，畫面 MUST NOT 一直停在「載入中」."""
    class Broken(FakeAgent):
        def export(self, session_id):
            raise RuntimeError("no reading version today")

    broken = Broken("claude", [Listed("s1", "/tmp/p", "未匯入的", "2026-10-02T00:00:00Z")], last="boom")
    assert tui.import_preview(broken, "s1", True, None).pinned == "讀不到整份對話"
    assert tui.import_preview(broken, "s1").pinned == "讀不到這個 session"

    real, boom = tui.import_preview, None

    def only_the_full_one(agent, session_id, full=False, paths=None, updated=None):
        if full:
            raise RuntimeError("the worker died")
        return real(agent, session_id, full, paths, updated)
    monkeypatch.setattr(tui, "import_preview", only_the_full_one)
    app = tui.AgoraApp(store.Paths.from_env(), FakeCli(), agents=[broken], check_setup=False)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            await pilot.press("down")
            for _ in range(30):
                pinned = str(app.query_one("#pinned", Static).content)
                if "讀不到整份對話" in pinned:
                    break
                await pilot.pause(0.1)
            assert "讀不到整份對話" in str(app.query_one("#pinned", Static).content)
            assert "載入中" not in str(app.query_one("#pinned", Static).content)
    _run(go)


def test_benchmark_3mb_search_word_that_is_only_at_the_front():
    """Enter on a word that is only at the front of a 3 MB session: count the whole file and
    put it in the pane once (W3). A loose limit, like the `g` one - it is there to catch the
    quadratic way (a step at a time), not to measure the machine."""
    one = "## user\n" + "話" * 3000 + "\n\n## assistant\n" + "答" * 3000 + "\n\n"
    body = ("## user\n最早的獨有的字\n\n"
            + one * ((3 * 1024 * 1024) // len(one.encode("utf-8")))
            + "## user\n最後的普通一句話\n")
    assert 2.5 * (1 << 20) < len(body.encode("utf-8")) <= 3 * (1 << 20)
    hdr, app = _search_app(body)

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause(0.4)
            assert app.cache[hdr["id"]].more()
            await pilot.press("tab")
            await pilot.pause()

            t0 = time.perf_counter()
            await _search(pilot, "獨有的字")
            t1 = time.perf_counter()
            print(f"\n[BENCHMARK] 3 MB search to the front: {t1 - t0:.3f} s")
            assert t1 - t0 <= 5, f"3 MB search took {t1 - t0:.3f} s, which exceeds 5 s"
            assert _count(app) == "第 1 個／共 1 個"
            assert app.cache[hdr["id"]].at == 0, "the whole file was read"
            pane = app.query_one("#right", tui.PreviewText)
            row = _screen_row(pane, pane.selection.start)
            assert 0 <= row < pane.content_size.height, f"and it is on the screen ({row})"
    _run(go)

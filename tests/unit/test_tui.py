"""The interactive mode (design 5.9): its plain parts, and the Textual screen driven by key presses."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from agora import header as h
from agora import store, tui
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


def test_previews_show_the_whole_history_and_never_fail():
    normal = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "一般")
    paths, index = _index((normal, "## user\n第一句\n\n## assistant\n最後的回答\n"))
    pinned, history = tui.agora_preview(paths, index, normal["id"])
    assert "dir /tmp/p" in pinned and "tags 驗收" in pinned
    assert history.startswith("## user") and history.endswith("最後的回答")
    pinned, history = tui.import_preview(FakeAgent("claude", [], ("assistant", "好")), "s")
    assert pinned.startswith("最後一則（assistant）") and history == "## assistant\n好"
    assert tui.import_preview(FakeAgent("claude", [], None), "s") == ("", "")
    assert tui.import_preview(FakeAgent("claude", [], "boom"), "s")[1] == ""
    pinned, history = tui.import_preview(FakeAgent("claude", [], texts={"s": ["問題", "回答"]}), "s", full=True)
    assert "## user\n問題" in history and history.endswith("回答")


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
            await pilot.press("tab")
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
            await pilot.press("tab")
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
            await pilot.press("tab", "ctrl+t", "slash")
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
            await pilot.press("tab")                       # 未匯入
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
            await pilot.press("tab")
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
            await pilot.press("tab")                          # 未匯入
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
            await pilot.press("tab")
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
            await pilot.press("tab")
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
            await pilot.press("tab")
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
            await pilot.press("tab")                      # 未匯入
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
            assert "完成" in " ".join(str(app.screen.lines).split())
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
            assert "完成" in " ".join(str(app.screen.lines).split())     # it is in the window
            await pilot.press("space")
            await _wait(lambda: not isinstance(app.screen, tui.ModalScreen), pilot)
            assert str(app.query_one("#msg").render()) == ""
    _run(go)


# --- the result window has to say what the exit code means (review T2-final) ---


def _result_of(app, code, *, mark_one=False):
    """Run one delete that exits `code`, and hand back what the result window said."""
    lines: list[str] = []

    async def go():
        proc = FakeProc(lines=["[agora] 刪除 1/1"], code=code)
        app.spawn, _ = _spawn(proc)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            if mark_one:
                await pilot.press("space")
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("down", "enter")
            await _wait(lambda: isinstance(app.screen, tui.Tell), pilot)
            lines.append(" ".join(str(line) for line in app.screen.lines))
    _run(go)
    return lines


def test_the_result_window_says_a_command_that_worked():
    """exit 0."""
    assert "完成" in " ".join(_result_of(_marked_app(None), 0))


def test_the_result_window_says_some_of_it_failed():
    """exit 2: at least one item did not work, and the window says so rather than
    leaving the user to work it out from the output."""
    said = _result_of(_marked_app(None), 2, mark_one=True)
    assert "沒有全部成功" in " ".join(said)


def test_the_result_window_says_it_is_waiting_to_be_uploaded():
    """exit 3: saved here, not on Drive yet - which is not the same as done."""
    said = _result_of(_marked_app(None), 3)
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

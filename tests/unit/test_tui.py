"""The interactive mode (design 5.9): its plain parts, and the Textual screen driven by key presses."""

from __future__ import annotations

import asyncio
import json

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

    def search_text(self, keyword):
        yield from (sid for sid, msgs in self.texts.items() if any(keyword in m for m in msgs))


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
    assert tui.argv_for("delete", rows[:1], None, None) == [["delete", "session", "agora:a", "--yes"]]
    assert tui.argv_for("continue", rows[:1], "opencode", "/w") == [
        ["continue", "session", "agora:a", "--agent", "opencode", "--dir", "/w"]]
    imp = [tui.Row("opencode:ses_x", ["x"], "x", "opencode")]
    assert tui.argv_for("import", imp, None, None) == [
        ["import", "session", "--external-session-id", "ses_x", "--agent", "opencode"]]


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


def _app(agents):
    paths, _ = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "第一個", sid="ses_1"), "## user\n表格的問題\n"),
                      (_hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "第二個", sid="ses_2"), "## user\n別的\n"))
    cli = FakeCli()
    return tui.AgoraApp(paths, cli, agents=agents, check_setup=False), cli


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


def test_mark_two_then_merge_runs_the_command():
    app, cli = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "space", "m")
            await pilot.pause()
            await pilot.press("enter")                     # opencode writes the summaries
            for _ in range(40):
                await pilot.pause(0.05)
                if cli.calls:
                    break
            assert cli.calls and cli.calls[0][:2] == ["merge", "session"] and len(cli.calls[0]) == 6
    _run(go)


def test_delete_starts_on_cancel():
    app, cli = _app([])

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")                     # the first option is 取消
            await pilot.pause()
            assert cli.calls == []
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

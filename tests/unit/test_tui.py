"""The interactive mode's plain parts: layout, rows, previews, keys (design 5.9)."""

from __future__ import annotations

from agora import header as h
from agora import store, tui
from agora.agents.base import Listed


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
    def __init__(self, name, listed, last=None):
        self.name, self.listed, self.last = name, listed, last

    def list_sessions(self):
        return self.listed

    def last_message(self, session_id):
        if self.last == "boom":
            raise RuntimeError("unreadable")
        return self.last


def test_cjk_takes_two_columns():
    assert tui.width("表格a") == 5
    assert tui.clip("表格表格", 5) == "表格 "            # never half a character
    assert tui.wrap("表格表格", 4) == ["表格", "表格"]


def test_preview_on_the_right_when_wide_below_when_narrow():
    wide = tui.layout(40, 120)
    assert wide["list"][0] == wide["preview"][0] and wide["preview"][1] > wide["list"][3] - 1
    tall = tui.layout(40, 80)
    assert tall["list"][1] == tall["preview"][1] == 0 and tall["preview"][0] > tall["list"][2]


def test_import_tab_lists_only_sessions_not_in_agora():
    _, index = _index((_hdr("01AAAAAAAAAAAAAAAAAAAAAAAA", "已匯入", sid="ses_in"), "## user\nx\n"))
    agent = FakeAgent("opencode", [Listed("ses_in", "/tmp/p", "已匯入", "2026-10-01T00:00:00Z"),
                                   Listed("ses_new", "/tmp/q", "新的", "2026-10-02T00:00:00Z"),
                                   Listed("ses_old", None, None, "2026-09-01T00:00:00Z")])
    rows = tui.import_rows(index, [agent])
    assert [r.key for r in rows] == ["opencode:ses_new", "opencode:ses_old"]      # newest first


def test_previews_show_the_last_native_turn_and_never_fail():
    normal = _hdr("01BBBBBBBBBBBBBBBBBBBBBBBB", "一般")
    merge = _hdr("01CCCCCCCCCCCCCCCCCCCCCCCC", "合併", relation="merge", agent=None)
    paths, index = _index(
        (normal, "## user\n第一句\n\n## assistant\n最後的回答\n"),
        (merge, "## 要約\n\n### 「A」（原本是 opencode）\n甲\n\n### 「B」（原本是 claude）\n乙\n\n## 來源\n\n- x\n"))
    assert tui.agora_preview(paths, index, normal["id"])[:2] == ["## assistant", "最後的回答"]
    shown = tui.agora_preview(paths, index, merge["id"])
    assert shown[0] == "### 「B」（原本是 claude）" and "乙" in shown and "- x" not in shown
    assert "tags  驗收" in shown
    assert tui.import_preview(FakeAgent("claude", [], ("assistant", "好\n了")), "s") == ["最後一則（assistant）", "好", "了"]
    assert tui.import_preview(FakeAgent("claude", [], None), "s") == []          # nothing stored: no preview
    assert tui.import_preview(FakeAgent("claude", [], "boom"), "s") == []


def _state():
    rows = {"agora": [tui.Row(f"agora:{n}", [n], f"{n} 表格" if n != "c" else n) for n in "abc"],
            "import": [tui.Row(f"opencode:ses_{n}", [n], n, "opencode") for n in "xy"]}
    return tui.State(rows=rows)


def test_keys_move_mark_and_switch_tabs():
    s = _state()
    assert tui.handle(s, "DOWN") is None and s.current().key == "agora:b"
    tui.handle(s, " ")                                  # marks b, moves to c
    assert s.marked == {"agora:b"} and s.current().key == "agora:c"
    tui.handle(s, "\t")
    assert s.tab == "import" and s.cursor == 0
    tui.handle(s, "\t")
    assert s.tab == "agora"


def test_filter_typing():
    s = _state()
    for key in "/表格\n":
        tui.handle(s, key)
    assert [r.key for r in s.shown()] == ["agora:a", "agora:b"] and not s.editing
    tui.handle(s, "/")
    tui.handle(s, "ESC")
    assert s.filter == "" and len(s.shown()) == 3


def test_actions_and_what_they_run():
    s = _state()
    assert tui.handle(s, "m")[0] == "say"               # merge needs two marked
    tui.handle(s, " ")
    tui.handle(s, " ")
    action, rows = tui.handle(s, "m")
    assert action == "merge" and [r.key for r in rows] == ["agora:a", "agora:b"]
    assert tui.argv_for("merge", rows, "claude", None) == [["merge", "session", "agora:a", "agora:b", "--agent", "claude"]]
    action, rows = tui.handle(s, "d")
    assert action == "delete" and len(rows) == 1         # never several at once
    assert tui.argv_for("delete", rows, None, None) == [["delete", "session", rows[0].key, "--yes"]]
    action, rows = tui.handle(s, "\n")
    assert tui.argv_for(action, rows, "opencode", "/w") == [
        ["continue", "session", rows[0].key, "--agent", "opencode", "--dir", "/w"]]
    tui.handle(s, "\t")
    action, rows = tui.handle(s, "\n")                  # nothing marked: the row under the cursor
    assert tui.argv_for(action, rows, None, None) == [
        ["import", "session", "--external-session-id", "ses_x", "--agent", "opencode"]]
    tui.handle(s, " ")
    tui.handle(s, " ")
    assert len(tui.handle(s, "\n")[1]) == 2             # both marked
    assert tui.handle(s, "q") == ("quit", [])


def test_scrolling_keeps_the_cursor_visible():
    s = _state()
    s.cursor = 2
    tui.scroll(s, 2)
    assert s.top == 1
    s.cursor = 0
    tui.scroll(s, 2)
    assert s.top == 0


def test_no_interactive_mode_without_a_terminal(monkeypatch, capsys):  # review T1
    from agora import cli
    called = []
    monkeypatch.setattr(tui, "main", lambda paths: called.append(1) or 0)
    assert cli.main([]) == cli.EXIT_ERROR and not called       # pytest's stdin/stdout are not a TTY
    assert "互動模式只在終端機裡開" in capsys.readouterr().err


def test_import_tab_leaves_out_agoras_own_copies():  # review T5
    paths, index = _index()
    paths.state.mkdir(parents=True, exist_ok=True)
    (paths.state / "unsaved-launches").write_text("opencode:ses_copy\n")
    agent = FakeAgent("opencode", [Listed("ses_copy", "/tmp/p", "複本", None),
                                   Listed("ses_sum", str(paths.state / "summarize"), "要約用", None),
                                   Listed("ses_real", "/tmp/p", "真的", None)])
    assert [r.key for r in tui.import_rows(index, [agent], paths)] == ["opencode:ses_real"]


def test_short_ids_are_the_random_part_and_control_characters_stay_inert():  # review T9, T12
    _, index = _index((_hdr("01M3XB78461N9TCC4DB84PKF2V", "一"), "## user\nx\n"))
    assert tui.agora_rows(index, [])[0].cells[0] == "4DB84PKF2V"[-8:]
    assert tui.clip("a\x1b[31mb", 6) == "a·[31m"


def test_columns_line_up_by_display_width():
    rows = [tui.Row("a", ["ses_1", "claude", "/tmp/p", "標題"], ""),
            tui.Row("b", ["ses_22", "opencode", "/tmp/其他", "另一個"], "")]
    first, second = tui.aligned(rows)
    assert tui.width(first.split("標題")[0]) == tui.width(second.split("另一個")[0])

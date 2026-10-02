"""Interactive mode: `agora` with no arguments (design 5.9).

Two tabs - sessions already in agora, and agent sessions on this machine not
imported yet - a preview of the last native message, and a key bar. Every
action runs the command mode's own code (cli.main), so this file only lists,
draws and asks. Layout, rows, previews and keys are plain functions; curses
only paints them.
"""

from __future__ import annotations

import curses
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from agora import header as h
from agora import store

WIDE = 100                       # columns from which the preview goes to the right
TABS = ("agora", "import")
TAB_NAMES = {"agora": "Agora", "import": "未匯入"}
KEYS = {
    "agora": "↑↓ 移動 空白 勾選 Enter 接續 m 合併 e 改標頭 d 刪除 / 篩選 Tab 換頁 q 離開",
    "import": "↑↓ 移動 空白 勾選 Enter 匯入 / 篩選 Tab 換頁 q 離開",
}


# --- text that fits the screen (CJK characters take two columns) ---------------

def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clip(text: str, cols: int) -> str:
    """text cut and padded to exactly `cols` display columns."""
    out, used = [], 0
    for c in text.replace("\n", " "):
        w = width(c)
        if used + w > cols:
            break
        out.append(c)
        used += w
    return "".join(out) + " " * (cols - used)


def wrap(text: str, cols: int) -> list[str]:
    lines = []
    for para in text.splitlines() or [""]:
        line, used = "", 0
        for c in para:
            w = width(c)
            if used + w > cols:
                lines.append(line)
                line, used = "", 0
            line += c
            used += w
        lines.append(line)
    return lines


def layout(rows: int, cols: int) -> dict[str, tuple[int, int, int, int]]:
    """(top, left, height, width) of the list and the preview; the last two rows are the key bar."""
    body = max(rows - 3, 2)
    if cols >= WIDE:
        left = cols * 11 // 20
        return {"list": (1, 0, body, left), "preview": (1, left + 1, body, cols - left - 1)}
    half = max(body // 2, 1)
    return {"list": (1, 0, half, cols), "preview": (half + 2, 0, body - half - 1, cols)}


# --- what each tab lists -------------------------------------------------------

@dataclass
class Row:
    key: str                     # "agora:<ULID>", or "<agent>:<session id>" on the import tab
    cells: list[str]
    text: str                    # what the filter looks in
    agent: str | None = None
    dir: str | None = None


def agora_rows(index: store.Index, filters: list) -> list[Row]:
    rows = []
    for ulid, hdr, _snippet in index.search(filters):
        agora = h.agora_of(hdr)
        source = agora.get("source") or {}
        kind = source.get("agent") or agora.get("relation") or ""
        title = str(hdr.get("title") or "")
        rows.append(Row(f"agora:{ulid}", [ulid[:8], store.sort_date(hdr)[5:10], kind, title],
                        f"{ulid} {kind} {title}", kind, source.get("dir")))
    return rows


def import_rows(index: store.Index, agents: list) -> list[Row]:
    found = []
    for agent in agents:
        for s in agent.list_sessions():
            if not index.by_source(agent.name, s.session_id):
                found.append((s.updated_at or "", Row(
                    f"{agent.name}:{s.session_id}",
                    [s.session_id[:12], agent.name, _home(s.dir), s.title or ""],
                    f"{s.session_id} {agent.name} {s.dir or ''} {s.title or ''}", agent.name, s.dir)))
    return [row for _when, row in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _home(path: str | None) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def filtered(rows: list[Row], text: str) -> list[Row]:
    words = text.lower().split()
    return [r for r in rows if all(w in r.text.lower() for w in words)]


# --- previews: the last native message, never generated ------------------------

def agora_preview(paths: store.Paths, index: store.Index, agora_id: str) -> list[str]:
    """The last turn of session.md (for a merge, its last section), then dir and tags."""
    ulid = agora_id.split(":", 1)[1]
    hdr = index.header(ulid) or {}
    try:
        _, body = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
    except (OSError, h.HeaderError):
        body = ""
    if h.agora_of(hdr).get("relation") == "merge":
        part = body.split("\n## 來源", 1)[0]
        last = "### " + part.rsplit("\n### ", 1)[-1] if "\n### " in part else part
    else:
        last = "## " + body.rsplit("\n## ", 1)[-1] if "\n## " in body else body
    meta = [f"dir   {(h.agora_of(hdr).get('source') or {}).get('dir') or '—'}",
            f"tags  {', '.join(map(str, hdr.get('tags') or [])) or '—'}"]
    return [*last.strip().splitlines(), "", *meta]


def import_preview(agent, session_id: str) -> list[str]:
    try:
        last = agent.last_message(session_id)
    except Exception:            # a preview must never take the screen down
        return []
    if not last:
        return []
    role, text = last
    return [f"最後一則（{role}）", *text.splitlines()]


# --- the screen's state and its keys -------------------------------------------

@dataclass
class State:
    rows: dict[str, list[Row]]
    tab: str = "agora"
    cursor: int = 0
    top: int = 0
    filter: str = ""
    editing: bool = False        # typing into the filter
    marked: set[str] = field(default_factory=set)

    def shown(self) -> list[Row]:
        return filtered(self.rows[self.tab], self.filter)

    def current(self) -> Row | None:
        shown = self.shown()
        return shown[self.cursor] if 0 <= self.cursor < len(shown) else None


def handle(state: State, key: str) -> tuple[str, list[Row]] | None:
    """Apply one key; return (action, rows) when an action has to run outside the screen."""
    if state.editing:
        if key in ("\n", "ESC"):
            state.editing = False
            if key == "ESC":
                state.filter = ""
        elif key == "BACKSPACE":
            state.filter = state.filter[:-1]
        elif len(key) == 1 and key.isprintable():
            state.filter += key
        state.cursor = 0
        return None
    shown, row = state.shown(), state.current()
    if key in ("UP", "k"):
        state.cursor = max(state.cursor - 1, 0)
    elif key in ("DOWN", "j"):
        state.cursor = min(state.cursor + 1, max(len(shown) - 1, 0))
    elif key == "\t":
        state.tab = TABS[(TABS.index(state.tab) + 1) % len(TABS)]
        state.cursor, state.filter = 0, ""
    elif key == "/":
        state.editing = True
    elif key == " " and row:
        state.marked.symmetric_difference_update({row.key})
        state.cursor = min(state.cursor + 1, max(len(shown) - 1, 0))
    elif key == "q":
        return "quit", []
    elif key == "\n" and state.tab == "import" and row:
        marked = [r for r in state.rows["import"] if r.key in state.marked]
        return "import", marked or [row]
    elif key == "\n" and row:
        return "continue", [row]
    elif key == "m" and state.tab == "agora":
        marked = [r for r in state.rows["agora"] if r.key in state.marked]
        return ("merge", marked) if len(marked) >= 2 else ("say", [Row("", ["合併要先用空白鍵勾選至少兩個"], "")])
    elif key == "e" and state.tab == "agora" and row:
        return "edit", [row]
    elif key == "d" and state.tab == "agora" and row:
        return "delete", [row]
    return None


def scroll(state: State, height: int) -> None:
    if state.cursor < state.top:
        state.top = state.cursor
    elif state.cursor >= state.top + height:
        state.top = state.cursor - height + 1


def argv_for(action: str, rows: list[Row], agent: str | None, workdir: str | None) -> list[list[str]]:
    """The command-mode invocations an action stands for."""
    ids = [r.key for r in rows]
    if action == "import":
        return [["import", "session", "--external-session-id", r.key.split(":", 1)[1], "--agent", r.agent]
                for r in rows]
    if action == "continue":
        return [["continue", "session", ids[0], "--agent", agent, "--dir", workdir]]
    if action == "merge":
        return [["merge", "session", *ids, "--agent", agent]]
    if action == "edit":
        return [["edit", "session", ids[0]]]
    if action == "delete":
        return [["delete", "session", ids[0], "--yes"]]
    return []


# --- curses: paint, and ask small questions ------------------------------------

_NAMED = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_BACKSPACE: "BACKSPACE",
          127: "BACKSPACE", 27: "ESC", 10: "\n", 13: "\n", 9: "\t"}


def _key(screen) -> str:
    k = screen.get_wch()
    if isinstance(k, str):
        return _NAMED.get(ord(k), k) if len(k) == 1 else k
    return _NAMED.get(k, "")


def _put(screen, y: int, x: int, text: str, cols: int, attr: int = 0) -> None:
    try:
        screen.addstr(y, x, clip(text, cols), attr)
    except curses.error:         # the bottom-right cell cannot be written; nothing to do about it
        pass


def paint(screen, state: State, preview: list[str], message: str) -> None:
    rows, cols = screen.getmaxyx()
    screen.erase()
    tabs = "  ".join(f"[{TAB_NAMES[t]} {len(state.rows[t])}]" if t == state.tab else f"{TAB_NAMES[t]} {len(state.rows[t])}"
                     for t in TABS)
    flt = f"篩選: {state.filter}{'_' if state.editing else ''}" if state.filter or state.editing else ""
    _put(screen, 0, 0, f" agora  {tabs}  {flt}", cols, curses.A_BOLD)
    box = layout(rows, cols)
    top, left, height, wide = box["list"]
    scroll(state, height)
    shown = state.shown()
    if not shown:
        _put(screen, top, left, "  （沒有東西；按 / 改篩選，或按 Tab 換頁）", wide)
    for i, row in enumerate(shown[state.top:state.top + height]):
        n = state.top + i
        mark = "✓" if row.key in state.marked else " "
        _put(screen, top + i, left, f"{'▸' if n == state.cursor else ' '}{mark} " + "  ".join(row.cells), wide,
             curses.A_REVERSE if n == state.cursor else 0)
    ptop, pleft, pheight, pwide = box["preview"]
    lines = [line for text in preview for line in wrap(text, max(pwide - 1, 1))]
    for i, line in enumerate(lines[:pheight]):
        _put(screen, ptop + i, pleft, line, pwide)
    _put(screen, rows - 2, 0, message, cols, curses.A_BOLD)
    _put(screen, rows - 1, 0, KEYS[state.tab], cols, curses.A_DIM)
    screen.refresh()


def choose(screen, title: str, options: list[str], note: str = "") -> int | None:
    """A small window in the middle; the index chosen, or None on Esc."""
    rows, cols = screen.getmaxyx()
    wide = min(max(width(title), *(width(o) for o in options), width(note)) + 6, cols)
    tall = len(options) + (4 if note else 3)
    win = curses.newwin(tall, wide, max((rows - tall) // 2, 0), max((cols - wide) // 2, 0))
    win.keypad(True)
    pick = 0
    while True:
        win.erase()
        win.box()
        _put(win, 0, 2, f" {title} ", wide - 4, curses.A_BOLD)
        for i, option in enumerate(options):
            _put(win, 1 + i, 2, f"{'▸' if i == pick else ' '} {option}", wide - 4, curses.A_REVERSE if i == pick else 0)
        if note:
            _put(win, len(options) + 1, 2, note, wide - 4, curses.A_DIM)
        _put(win, tall - 1, 2, " Enter 確定  Esc 取消 ", wide - 4)
        win.refresh()
        key = _key(win)
        if key in ("UP", "k"):
            pick = max(pick - 1, 0)
        elif key in ("DOWN", "j"):
            pick = min(pick + 1, len(options) - 1)
        elif key == "\n":
            return pick
        elif key in ("ESC", "q"):
            return None


def screen_loop(screen, state: State, previews, message: str):
    """Run the screen until an action needs the terminal; return (action, rows, agent, workdir)."""
    curses.curs_set(0)
    screen.keypad(True)
    while True:
        row = state.current()
        paint(screen, state, previews(state.tab, row) if row else [], message)
        message = ""
        result = handle(state, _key(screen))
        if result is None:
            continue
        action, rows = result
        if action == "say":
            message = rows[0].cells[0]
            continue
        if action in ("quit", "import", "edit"):
            return action, rows, None, None
        if action == "delete":
            if choose(screen, "移到 Drive 垃圾桶？", ["確定", "取消"], f"{rows[0].key}「{rows[0].cells[-1]}」") == 0:
                return action, rows, None, None
            continue
        workdir = rows[0].dir if action == "continue" and rows[0].dir and os.path.isdir(rows[0].dir) else os.getcwd()
        title = "用哪個 agent 接續？" if action == "continue" else f"合併 {len(rows)} 個：由誰寫要約？"
        pick = choose(screen, title, ["opencode", "claude"], f"工作目錄：{workdir}" if action == "continue" else "")
        if pick is not None:
            return action, rows, ("opencode", "claude")[pick], workdir


def main(paths: store.Paths) -> int:
    from agora import cli       # the command mode does the work; imported here to avoid a cycle
    agents = [cli.load_agent(name) for name in cli.AGENTS]
    print("[agora] 同步 Drive、列出這台機器上的 session…")
    index = store.sync(paths)
    state = State(rows={"agora": agora_rows(index, []), "import": import_rows(index, agents)})
    cache: dict[str, list[str]] = {}

    def previews(tab: str, row: Row) -> list[str]:
        if row.key not in cache:
            agent = next((a for a in agents if a.name == row.agent), None)
            cache[row.key] = (agora_preview(paths, index, row.key) if tab == "agora"
                              else import_preview(agent, row.key.split(":", 1)[1]) if agent else [])
        return cache[row.key]

    message = ""
    while True:
        action, rows, agent, workdir = curses.wrapper(screen_loop, state, previews, message)
        if action == "quit":
            return 0
        codes = [cli.main(argv) for argv in argv_for(action, rows, agent, workdir)]
        input("\n按 Enter 回到選單…")
        message = "完成" if all(code == 0 for code in codes) else "有動作沒有成功，訊息在上一個畫面"
        state.marked.clear()
        index = store.Index(paths)
        state.rows = {"agora": agora_rows(index, []), "import": import_rows(index, agents)}
        state.cursor = min(state.cursor, max(len(state.shown()) - 1, 0))
        cache.clear()

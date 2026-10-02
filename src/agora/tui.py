"""Interactive mode: `agora` with no arguments (design 5.9).

Two tabs - sessions already in agora, and agent sessions on this machine not
imported yet - a preview of the last native message, and a key bar. Every
action runs the command mode's own code (cli.main), so this file only lists,
draws and asks. Layout, rows, previews and keys are plain functions; curses
only paints them.
"""

from __future__ import annotations

import contextlib
import curses
import io
import locale
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from agora import header as h
from agora import store

WIDE = 100                       # columns from which the preview goes to the right
MIN_COLS, MIN_ROWS = 40, 10      # smaller than this, the screen only says so (review T4)
TABS = ("agora", "import")
TAB_NAMES = {"agora": "Agora", "import": "未匯入"}
KEYS = {
    "agora": [("↑↓", "移動"), ("空白", "勾選"), ("Enter", "接續"), ("m", "合併"), ("e", "改標頭"), ("d", "刪除"),
              ("/", "篩選"), ("Tab", "換頁"), ("⇧Tab", "左右"), ("q", "離開")],
    "import": [("↑↓", "移動"), ("空白", "勾選"), ("Enter", "匯入"), ("/", "篩選"), ("Tab", "換頁"),
               ("⇧Tab", "左右"), ("q", "離開")],
    "preview": [("↑↓", "捲動"), ("PgUp/PgDn", "翻頁"), ("g/G", "最上／最下"), ("⇧Tab", "回清單"), ("q", "離開")],
}


# --- text that fits the screen (CJK characters take two columns) ---------------

def width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clip(text: str, cols: int) -> str:
    """text cut and padded to exactly `cols` display columns."""
    out, used = [], 0
    for c in text.replace("\n", " "):
        c = c if c.isprintable() else "·"        # control characters and escapes stay visible but inert (T12)
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


def aligned(rows: list["Row"], cap: int = 24) -> list[list[str]]:
    """Each row's cells padded to a shared width per column, so the columns line up; the last is free."""
    if not rows:
        return []
    n = max(len(r.cells) for r in rows)
    widths = [min(max(width(r.cells[i]) for r in rows if i < len(r.cells)), cap) for i in range(n - 1)]
    return [[clip(c, widths[i]) if i < n - 1 else c for i, c in enumerate(r.cells)] for r in rows]


def window(lines: list[str], height: int, back: int) -> tuple[list[str], int]:
    """The `height` lines ending `back` lines above the bottom, and `back` kept within range."""
    back = max(0, min(back, len(lines) - height))
    end = len(lines) - back
    return lines[max(end - height, 0):end], back


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
        rows.append(Row(f"agora:{ulid}", [ulid[-8:], store.sort_date(hdr)[5:10], kind, title],   # the random part (T9)
                        f"{ulid} {kind} {title}", kind, source.get("dir")))
    return rows


def import_rows(index: store.Index, agents: list, paths: store.Paths | None = None) -> list[Row]:
    """Agent sessions not in agora yet, newest first, leaving out the copies agora made itself (review T5)."""
    skip, own = set(), None
    if paths is not None:
        own = str(paths.state)
        try:
            skip = set((paths.state / "unsaved-launches").read_text(encoding="utf-8").split())
        except OSError:
            pass
    found = []
    for agent in agents:
        try:
            listed = agent.list_sessions()
        except Exception as e:   # one adapter failing leaves the other's list (review U1)
            print(f"[agora] {agent.name} 的 session 清單讀不到：{e}", file=sys.stderr)
            listed = []
        for s in listed:
            key = f"{agent.name}:{s.session_id}"
            if key in skip or (own and (s.dir or "").startswith(own)):
                continue
            imported = index.by_source(agent.name, s.session_id)
            if imported and not _newer(s.updated_at, index, imported[0]):
                continue
            mark = "↻ " if imported else ""   # imported before, talked to since: import again
            found.append((s.updated_at or "", Row(
                key, [s.session_id[-12:], agent.name, _home(s.dir), mark + (s.title or "")],
                f"{s.session_id} {agent.name} {s.dir or ''} {s.title or ''}", agent.name, s.dir)))
    return [row for _when, row in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _newer(updated_at: str | None, index: store.Index, ulid: str) -> bool:
    """Whether the agent's session changed after agora last saved it."""
    saved = str(h.agora_of(index.header(ulid) or {}).get("updated_at") or "")
    return bool(updated_at and saved) and updated_at[:19] > saved[:19]


def _home(path: str | None) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def filtered(rows: list[Row], text: str) -> list[Row]:
    words = text.lower().split()
    return [r for r in rows if all(w in r.text.lower() for w in words)]


# --- previews: what is stored, never generated -------------------------------
# A preview is (pinned, history): two pinned lines on top, and the history the
# pane shows from its bottom up.

def agora_preview(paths: store.Paths, index: store.Index, agora_id: str) -> tuple[list[str], list[str]]:
    """The whole session.md text (the reading version, or a merge's sections), with dir and tags pinned."""
    ulid = agora_id.split(":", 1)[1]
    hdr = index.header(ulid) or {}
    try:
        _, body = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
    except (OSError, h.HeaderError):
        body = ""
    pinned = [f"dir   {(h.agora_of(hdr).get('source') or {}).get('dir') or '—'}",
              f"tags  {', '.join(map(str, hdr.get('tags') or [])) or '—'}"]
    return pinned, body.strip().splitlines()


def import_preview(agent, session_id: str, full: bool = False) -> tuple[list[str], list[str]]:
    """The last message; with `full`, the whole conversation as its reading version (existing code, no new conversion)."""
    from agora.agents.base import reading
    try:
        if full:
            return ["整份對話（閱讀版）", ""], reading(agent, agent.export(session_id).raw).strip().splitlines()
        last = agent.last_message(session_id)
    except Exception:            # a preview must never take the screen down
        return ["讀不到這個 session", ""], []
    if not last:
        return ["", ""], []
    role, text = last
    return [f"最後一則（{role}）", "⇧Tab 看整份對話"], text.splitlines()


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
    focus: str = "list"          # "list" or "preview" (shift+tab)
    back: int = 0                # preview lines scrolled up from the bottom

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
    if key == "BTAB":
        state.focus = "preview" if state.focus == "list" else "list"
        state.back = 0
        return None
    if state.focus == "preview":     # the preview has the keys: scroll the history
        steps = {"UP": 1, "k": 1, "DOWN": -1, "j": -1, "PGUP": 10, "PGDN": -10, "g": 10 ** 9, "G": -10 ** 9}
        if key in steps:
            state.back = max(state.back + steps[key], 0)
        elif key == "q":
            return "quit", []
        return None
    shown, row = state.shown(), state.current()
    if key in ("UP", "k", "DOWN", "j"):
        state.back = 0                # a new row's history opens at its bottom
    if key in ("UP", "k"):
        state.cursor = max(state.cursor - 1, 0)
    elif key in ("DOWN", "j"):
        state.cursor = min(state.cursor + 1, max(len(shown) - 1, 0))
    elif key == "\t":
        state.tab = TABS[(TABS.index(state.tab) + 1) % len(TABS)]
        state.cursor, state.filter, state.back = 0, "", 0
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


# --- curses: colours, painting, small windows ----------------------------------

_NAMED = {curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_BACKSPACE: "BACKSPACE",
          curses.KEY_RESIZE: "RESIZE", curses.KEY_BTAB: "BTAB", curses.KEY_PPAGE: "PGUP",
          curses.KEY_NPAGE: "PGDN", 127: "BACKSPACE", 27: "ESC", 10: "\n", 13: "\n", 9: "\t"}
COLORS: dict[str, int] = {}


def colours() -> None:
    """Colour pairs by role; a terminal without colours just gets none."""
    COLORS.clear()
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_MAGENTA
    roles = {"bar": (curses.COLOR_BLACK, curses.COLOR_CYAN), "opencode": (curses.COLOR_CYAN, bg),
             "claude": (orange, bg), "merge": (curses.COLOR_GREEN, bg), "mark": (curses.COLOR_YELLOW, bg),
             "key": (curses.COLOR_YELLOW, bg), "user": (curses.COLOR_BLUE, bg),
             "assistant": (curses.COLOR_GREEN, bg), "frame": (curses.COLOR_CYAN, bg), "warn": (curses.COLOR_RED, bg)}
    for n, (role, (fg, back)) in enumerate(roles.items(), 1):
        try:
            curses.init_pair(n, fg, back)
            COLORS[role] = curses.color_pair(n)
        except curses.error:
            pass


def c(role: str, extra: int = 0) -> int:
    return COLORS.get(role, 0) | extra


def line_attr(line: str) -> int:
    """How a history line is drawn: who speaks stands out, tool summaries step back."""
    if line.startswith("## user"):
        return c("user", curses.A_BOLD)
    if line.startswith(("## assistant", "### ", "## 要約", "## 來源")):
        return c("assistant", curses.A_BOLD)
    return curses.A_DIM if line.startswith(("[tool]", "[skip")) else 0


def _key(screen) -> str:
    k = screen.get_wch()
    if isinstance(k, str):
        return _NAMED.get(ord(k), k) if len(k) == 1 else k
    return _NAMED.get(k, "")


def _put(screen, y: int, x: int, text: str, cols: int, attr: int = 0, pad: bool = True) -> None:
    try:
        screen.addstr(y, x, clip(text, cols) if pad else clip(text, min(cols, width(text))), attr)
    except curses.error:         # the bottom-right cell cannot be written; nothing to do about it
        pass


def paint(screen, state: State, preview: tuple[list[str], list[str]], message: str, status: str) -> None:
    rows, cols = screen.getmaxyx()
    screen.erase()
    if cols < MIN_COLS or rows < MIN_ROWS:
        _put(screen, 0, 0, f"終端機太小（至少 {MIN_COLS}×{MIN_ROWS}）", cols)
        screen.refresh()
        return
    tabs = "  ".join(f"[{TAB_NAMES[t]} {len(state.rows[t])}]" if t == state.tab else f" {TAB_NAMES[t]} {len(state.rows[t])} "
                     for t in TABS)
    flt = f"篩選: {state.filter}{'_' if state.editing else ''}" if state.filter or state.editing else ""
    _put(screen, 0, 0, f" agora  {tabs}  {flt}", cols, c("bar", curses.A_BOLD))
    if status:
        _put(screen, 0, max(cols - width(status) - 2, 0), status, width(status) + 1, c("bar"), pad=False)
    box = layout(rows, cols)
    top, left, height, wide = box["list"]
    scroll(state, height)
    shown = state.shown()
    if not shown:
        _put(screen, top, left, "  （沒有東西；按 / 改篩選，或按 Tab 換頁）", wide, curses.A_DIM)
    cells = aligned(shown)
    for i, row in enumerate(shown[state.top:state.top + height]):
        n = state.top + i
        here = n == state.cursor
        base = (curses.A_REVERSE if state.focus == "list" else curses.A_BOLD) if here else 0
        _put(screen, top + i, left, f"{'▸' if here else ' '}", wide, base)
        _put(screen, top + i, left + 1, "✓" if row.key in state.marked else " ", 1, c("mark", curses.A_BOLD) | base)
        x = left + 3
        for cell in cells[n]:
            if x >= left + wide:
                break
            role = cell.strip() if cell.strip() in ("opencode", "claude", "merge") else ""
            _put(screen, top + i, x, cell, left + wide - x, (c(role) if role else 0) | base)
            x += width(cell) + 2
    ptop, pleft, pheight, pwide = box["preview"]
    frame = c("frame", curses.A_BOLD) if state.focus == "preview" else curses.A_DIM
    if pleft:                    # a rule between the list and the preview
        for y in range(ptop, ptop + pheight):
            _put(screen, y, pleft - 1, "┃" if state.focus == "preview" else "│", 1, frame)
    else:
        _put(screen, ptop - 1, 0, "━" * cols if state.focus == "preview" else "─" * cols, cols, frame)
    pinned, history = preview
    for i, line in enumerate(pinned[:2]):
        _put(screen, ptop + i, pleft, line, pwide, curses.A_DIM)
    lines = [(line, line_attr(text)) for text in history for line in wrap(text, max(pwide - 1, 1))]
    visible, state.back = window(lines, max(pheight - 2, 1), state.back)
    for i, (line, attr) in enumerate(visible):
        _put(screen, ptop + 2 + i, pleft, line, pwide, attr)
    _put(screen, rows - 2, 0, message, cols, c("warn", curses.A_BOLD) if "失敗" in message else curses.A_BOLD)
    x = 0
    for key, label in KEYS["preview" if state.focus == "preview" else state.tab]:
        _put(screen, rows - 1, x, key, cols - x, c("key", curses.A_BOLD), pad=False)
        x += width(key) + 1
        _put(screen, rows - 1, x, label, max(cols - x, 0), curses.A_DIM, pad=False)
        x += width(label) + 2
        if x >= cols:
            break
    screen.refresh()


def _window(screen, tall: int, wide: int, title: str):
    screen.touchwin()            # bring the main screen back first, so an earlier window leaves no trace
    screen.noutrefresh()
    rows, cols = screen.getmaxyx()
    wide = min(wide, cols)
    win = curses.newwin(min(tall, rows), wide, max((rows - tall) // 2, 0), max((cols - wide) // 2, 0))
    win.keypad(True)
    win.erase()
    win.attron(c("frame"))
    win.box()
    win.attroff(c("frame"))
    _put(win, 0, 2, f" {title} ", wide - 4, c("frame", curses.A_BOLD), pad=False)
    return win, wide


def choose(screen, title: str, options: list[str], note: str = "") -> int | None:
    """A small window in the middle; the index chosen, or None on Esc."""
    notes = note.splitlines() if note else []
    wide = max(width(title), *(width(o) for o in options), *(width(n) for n in notes)) + 6
    pick = 0
    while True:
        win, wide = _window(screen, len(options) + len(notes) + 2, wide, title)
        for i, option in enumerate(options):
            _put(win, 1 + i, 2, f"{'▸' if i == pick else ' '} {option}", wide - 4, curses.A_REVERSE if i == pick else 0)
        for i, line in enumerate(notes):
            _put(win, len(options) + 1 + i, 2, line, wide - 4, curses.A_DIM)
        _put(win, len(options) + len(notes) + 1, 2, " Enter 確定  Esc 取消 ", wide - 4, pad=False)
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


def ask_text(screen, title: str, text: str) -> str | None:
    """One line of input in a small window (get_wch, so CJK input works); None on Esc."""
    while True:
        win, wide = _window(screen, 3, max(width(title), width(text), 40) + 6, title)
        _put(win, 1, 2, text[-(wide - 6):] + "_", wide - 4)
        _put(win, 2, 2, " Enter 確定  Esc 取消 ", wide - 4, pad=False)
        win.refresh()
        key = _key(win)
        if key == "\n":
            return text
        if key == "ESC":
            return None
        if key == "BACKSPACE":
            text = text[:-1]
        elif len(key) == 1 and key.isprintable():
            text += key


def ask_dir(screen, row: Row) -> str | None:
    """Where to open the agent: the source's directory when it exists here, and always changeable (review T7)."""
    known = bool(row.dir and os.path.isdir(row.dir))
    workdir = row.dir if known else os.getcwd()
    note = "" if known else "⚠ 來源沒有記錄目錄（或這台機器上沒有），會在目前目錄開"
    while True:
        pick = choose(screen, "在哪裡開？", [f"在這裡開：{workdir}", "改目錄…"], note)
        if pick is None:
            return None
        if pick == 0:
            return workdir
        typed = ask_text(screen, "工作目錄", workdir)
        if typed and os.path.isdir(os.path.expanduser(typed)):
            workdir, note = os.path.expanduser(typed), ""
        elif typed is not None:
            note = f"⚠ 找不到這個目錄：{typed}"


def busy(screen, title: str, work):
    """Run `work` while a window says so, its latest output line under a spinner; (value, output, error)."""
    out, result = io.StringIO(), {}

    def run():
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                result["value"] = work()
            except Exception as e:   # shown in the result window, never a traceback over the screen
                result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    started, spin = time.monotonic(), "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    screen.timeout(120)
    while thread.is_alive():
        last = (out.getvalue().strip().splitlines() or [""])[-1]
        lines = [f"{spin[int(time.monotonic() * 8) % len(spin)]} 執行中…（{int(time.monotonic() - started)} 秒）", last]
        win, wide = _window(screen, 4, max(width(title) + 6, 60, width(last) + 6), title)
        for i, line in enumerate(lines):
            _put(win, 1 + i, 2, line, wide - 4, c("key") if i == 0 else curses.A_DIM)
        win.refresh()
        try:
            screen.get_wch()         # keys wait; the work cannot be interrupted halfway
        except (curses.error, KeyboardInterrupt):
            pass
    screen.timeout(-1)
    return result.get("value"), out.getvalue(), result.get("error")


def tell(screen, title: str, text: str, ok: bool = True) -> None:
    """A window with the last lines of an action's output; any key closes it."""
    lines = [line for line in text.strip().splitlines() if line.strip()][-10:] or ["（沒有輸出）"]
    win, wide = _window(screen, len(lines) + 3, max(width(title), *(width(x) for x in lines)) + 6,
                        f"{'✓' if ok else '✗'} {title}")
    for i, line in enumerate(lines):
        _put(win, 1 + i, 2, line, wide - 4, 0 if ok else c("warn"))
    _put(win, len(lines) + 2, 2, " 按任意鍵回到清單 ", wide - 4, curses.A_DIM, pad=False)
    win.refresh()
    _key(win)


# --- the app: setup, listing, actions -------------------------------------------

def setup_needed(paths: store.Paths) -> str | None:
    """What is missing before agora can reach Drive: "rclone", "auth", or nothing."""
    if not shutil.which(os.environ.get("AGORA_RCLONE", "rclone")):
        return "rclone"
    return None if (paths.config / "rclone.conf").exists() else "auth"


def authorize(paths: store.Paths) -> int:
    """`rclone config create` with rclone's own client: it opens the browser; we pass its lines on."""
    paths.config.mkdir(parents=True, exist_ok=True)
    argv = [os.environ.get("AGORA_RCLONE", "rclone"), "config", "create", "gdrive", "drive", "scope=drive.file",
            "--config", str(paths.config / "rclone.conf")]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "token" not in line.lower():   # the token is a secret: never on screen
            print(line.rstrip())
    return proc.wait()


class App:
    def __init__(self, paths: store.Paths, cli):
        self.paths, self.cli = paths, cli
        self.agents = [cli.load_agent(name) for name in cli.AGENTS]
        self.state = State(rows={"agora": [], "import": []})
        self.index = store.Index(paths)
        self.cache: dict = {}
        self.message = self.status = ""

    def reload(self) -> None:
        self.index = store.Index(self.paths)       # local only; no full sync after every action (T6)
        self.state.rows = {"agora": agora_rows(self.index, []),
                           "import": import_rows(self.index, self.agents, self.paths)}
        self.state.cursor = min(self.state.cursor, max(len(self.state.shown()) - 1, 0))
        self.state.marked.clear()
        self.cache.clear()

    def start(self, screen) -> bool:
        """Guide a first run through setup, then sync once; False to leave."""
        need = setup_needed(self.paths)
        if need == "rclone":
            choose(screen, "需要 rclone", ["離開"], "請先在終端機執行：brew install rclone\n裝好之後再打 agora")
            return False
        if need == "auth":
            note = ("agora 把 Session 存在你的 Google Drive。\n用 rclone 內建的 client 授權，權限只有 drive.file：\n"
                    "只看得到 agora 自己建的檔案。")
            if choose(screen, "還沒設定 Google Drive", ["用瀏覽器授權", "離開"], note) != 0:
                return False
            code, out, error = busy(screen, "請在瀏覽器完成授權", lambda: authorize(self.paths))
            if error or code != 0 or setup_needed(self.paths):
                tell(screen, "授權沒有完成", f"{out}\n{error or ''}", ok=False)
                return False
            tell(screen, "授權完成", "接著在 Drive 建立 agora/ 資料夾並同步")
        _, out, _ = busy(screen, "同步 Drive", lambda: store.sync(self.paths, throttle=True))
        self.status = "離線：只有本機資料" if "連不上 Drive" in out else ""
        self.reload()
        if not self.state.rows["agora"]:
            self.message = "Agora 還沒有 Session：按 Tab 到「未匯入」，勾選後按 Enter 匯入"
        return True

    def preview(self, screen, row: Row) -> tuple[list[str], list[str]]:
        full = self.state.tab == "import" and self.state.focus == "preview"
        key = (row.key, full)
        if key not in self.cache:
            agent = next((a for a in self.agents if a.name == row.agent), None)
            if self.state.tab == "agora":
                self.cache[key] = agora_preview(self.paths, self.index, row.key)
            elif full and agent:
                value, _, _ = busy(screen, "讀取整份對話", lambda: import_preview(agent, row.key.split(":", 1)[1], True))
                self.cache[key] = value or (["讀不到這個 session", ""], [])
            else:
                self.cache[key] = import_preview(agent, row.key.split(":", 1)[1]) if agent else ([], [])
        return self.cache[key]

    def act(self, screen, title: str, argvs: list[list[str]]) -> None:
        """An action that needs no terminal of its own: run it under a window, then show what it said."""
        codes, out, error = busy(screen, title, lambda: [self.cli.main(argv) for argv in argvs])
        codes = codes or [2]
        ok = sum(code == 0 for code in codes)
        tell(screen, title, f"{out}\n{error or ''}", ok == len(codes))
        self.message = "完成" if ok == len(codes) else f"成功 {ok} 個、失敗 {len(codes) - ok} 個"
        self.reload()


def screen_loop(screen, app: App, first: bool):
    """Run the screen until an action needs the whole terminal; return (action, rows, agent, workdir)."""
    curses.curs_set(0)
    screen.keypad(True)
    colours()
    if first and not app.start(screen):
        return "quit", [], None, None
    state = app.state
    while True:
        row = state.current()
        paint(screen, state, app.preview(screen, row) if row else ([], []), app.message, app.status)
        app.message = ""
        result = handle(state, _key(screen))
        if result is None:
            continue
        action, rows = result
        if action == "say":
            app.message = rows[0].cells[0]
        elif action in ("quit", "edit"):      # the editor needs the terminal
            return action, rows, None, None
        elif action == "import":
            app.act(screen, f"匯入 {len(rows)} 個", argv_for(action, rows, None, None))
        elif action == "delete":
            if choose(screen, "移到 Drive 垃圾桶？", ["取消", "確定"], f"{rows[0].key}「{rows[0].cells[-1]}」") == 1:
                app.act(screen, "刪除", argv_for(action, rows, None, None))
        elif action == "continue":            # the agent takes the terminal
            pick = choose(screen, "用哪個 agent 接續？", ["opencode", "claude"])
            workdir = ask_dir(screen, rows[0]) if pick is not None else None
            if workdir is not None:
                return action, rows, ("opencode", "claude")[pick], workdir
        else:
            pick = choose(screen, f"合併 {len(rows)} 個：由誰寫要約？", ["opencode", "claude"],
                          "每個來源叫一次 AI；內容會送到那個 agent 的模型供應商")
            if pick is not None:
                app.act(screen, "合併", argv_for(action, rows, ("opencode", "claude")[pick], None))


def main(paths: store.Paths) -> int:
    from agora import cli       # the command mode does the work; imported here to avoid a cycle
    locale.setlocale(locale.LC_ALL, "")         # or curses prints CJK as garbage (review T3)
    os.environ.setdefault("ESCDELAY", "25")     # Esc closes a window at once, not after a second
    app, first = App(paths, cli), True
    while True:
        noise = io.StringIO()   # a warning printed under curses would scribble over the screen
        with contextlib.redirect_stderr(noise):
            action, rows, agent, workdir = curses.wrapper(screen_loop, app, first)
        first = False
        if action == "quit":
            return 0
        if noise.getvalue().strip():             # warnings from under curses, shown now (review U8)
            print(noise.getvalue().strip(), file=sys.stderr)
        codes = [cli.main(argv) for argv in argv_for(action, rows, agent, workdir)]
        try:
            input("\n按 Enter 回到選單…")
        except (KeyboardInterrupt, EOFError):
            pass
        app.message = "完成" if all(code == 0 for code in codes) else "沒有成功，訊息在上一個畫面"
        app.reload()

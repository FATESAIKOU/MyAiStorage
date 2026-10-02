"""Interactive mode: `agora` with no arguments (design 5.9), on Textual.

Two tabs - sessions already in agora, and agent sessions on this machine not
imported yet - a table with column names, a preview of the whole conversation
rendered as Markdown, and a key bar. Every action runs the command mode's own
commands, as child processes, so this file only lists, draws and asks. The data
side (rows, previews, the commands an action stands for) is plain functions with
tests.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, OptionList, ProgressBar, Static

from agora import cache
from agora import header as h
from agora import store

WIDE = 100                       # columns from which the preview goes to the right
TABS = ("agora", "import")
TAB_NAMES = {"agora": "Agora", "import": "未匯入"}
COLUMNS = {"agora": ("id", "標題", "agent", "更新"), "import": ("id", "標題", "agent", "更新", "目錄")}
TITLE_MAX = 36                   # the title column is cut here, so the others stay on screen
AGENT_STYLE = {"opencode": "cyan", "claude": "#ff8700", "merge": "green"}


# --- what each tab lists -------------------------------------------------------

@dataclass
class Row:
    key: str                     # "agora:<ULID>", or "<agent>:<session id>" on the import tab
    cells: list[str]             # in COLUMNS order
    text: str                    # what the title filter looks in
    agent: str | None = None
    dir: str | None = None
    updated: str | None = None   # the agent session's own update time, to tell a stale cache


def _when(stamp: str | None) -> str:
    """An RFC 3339 time as local "MM-DD HH:MM"; the column to sort by at a glance (feedback 10)."""
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return stamp[5:16].replace("T", " ")


def agora_rows(index: store.Index, filters: list) -> list[Row]:
    rows = []
    for ulid, hdr, _snippet in index.search(filters):
        agora = h.agora_of(hdr)
        source = agora.get("source") or {}
        kind = source.get("agent") or agora.get("relation") or ""
        title = str(hdr.get("title") or "")
        updated = str(agora.get("updated_at") or store.sort_date(hdr))
        rows.append(Row(f"agora:{ulid}", [ulid[-8:], _short(title), kind, _when(updated)],   # the random part (T9)
                        f"{ulid} {kind} {title}", kind, source.get("dir")))
    return rows


def import_rows(index: store.Index, agents: list, paths: store.Paths | None = None) -> list[Row]:
    """Agent sessions not in agora yet (or talked to since), newest first, leaving out agora's own copies (T5)."""
    skip, own = set(), None
    if paths is not None:
        own = str(paths.state)
        try:
            skip = set((paths.state / "unsaved-launches").read_text(encoding="utf-8").split())
        except OSError:
            pass
    for _ulid, hdr, _snippet in index.search([]):   # agent sessions a continue has moved past
        skip.update(h.agora_of(hdr).get("previous_sources") or [])
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
                key, [s.session_id[-8:], _short(mark + (s.title or "")), agent.name, _when(s.updated_at), _home(s.dir)],
                f"{s.session_id} {agent.name} {s.dir or ''} {s.title or ''}", agent.name, s.dir, s.updated_at)))
    return [row for _stamp, row in sorted(found, key=lambda pair: pair[0], reverse=True)]


def _short(title: str) -> str:
    return title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 1] + "…"


def _newer(updated_at: str | None, index: store.Index, ulid: str) -> bool:
    """Whether the agent's session changed after agora last saved it."""
    saved = str(h.agora_of(index.header(ulid) or {}).get("updated_at") or "")
    return bool(updated_at and saved) and updated_at[:19] > saved[:19]


def _home(path: str | None) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else (path or "")


def filtered(rows: list[Row], text: str, matches: set[str] | None = None) -> list[Row]:
    """Rows whose visible text has every word; with `matches`, only those keys (a content search)."""
    if matches is not None:
        return [r for r in rows if r.key in matches]
    words = text.lower().split()
    return [r for r in rows if all(w in r.text.lower() for w in words)]


# --- previews: what is stored, never generated -------------------------------
# A preview is (pinned, history): a pinned line on top, and the history as Markdown.

def agora_preview(paths: store.Paths, index: store.Index, agora_id: str) -> tuple[str, str]:
    """The whole session.md text (the reading version, or a merge's sections), with dir and tags pinned."""
    ulid = agora_id.split(":", 1)[1]
    hdr = index.header(ulid) or {}
    try:
        _, body = h.split_document((paths.mirror / ulid / "session.md").read_text(encoding="utf-8"))
    except (OSError, h.HeaderError):
        body = ""
    pinned = (f"dir {(h.agora_of(hdr).get('source') or {}).get('dir') or '—'}   "
              f"tags {', '.join(map(str, hdr.get('tags') or [])) or '—'}")
    return pinned, body.strip()


def import_preview(agent, session_id: str, full: bool = False, paths: store.Paths | None = None,
                   updated: str | None = None) -> tuple[str, str]:
    """The last message; with `full`, the whole conversation as its reading version, kept in the cache (5.10)."""
    from agora.agents.base import reading
    try:
        if full:
            text = (cache.local_reading(paths, agent, session_id, updated) if paths
                    else reading(agent, agent.export(session_id).raw))
            return "整份對話（閱讀版）", text.strip()
        last = agent.last_message(session_id)
    except Exception:            # a preview must never take the screen down
        return "讀不到這個 session", ""
    if not last:
        return "", ""
    role, text = last
    return f"最後一則（{role}），整份對話載入中…", f"## {role}\n{text}"


def argv_for(action: str, rows: list[Row], agent: str | None, workdir: str | None) -> list[list[str]]:
    """The command-mode invocations an action stands for."""
    if action == "import":
        # One command for every session of that agent: the command mode syncs once
        # and imports them one by one, with a k/N line for each (T1 R2).
        return [["import", "session", "--agent", rows[0].agent,
                 *[a for r in rows for a in ("--external-session-id", r.key.split(":", 1)[1])]]]
    first = rows[0].key if rows else ""
    return {"continue": [["continue", "session", first, "--agent", agent, "--dir", workdir]],
            "merge": [["merge", "session", *[r.key for r in rows], "--agent", agent]],
            "edit": [["edit", "session", first]],
            "delete": [["delete", "session", *[r.key for r in rows], "--yes"]],
            # pull/push take ids (T1 R5), so the screen passes the rows it has
            "pull": [["pull", "session", *[r.key for r in rows]]],
            "push": [["push", "session", *[r.key for r in rows if r.key.startswith("agora:")]]]}.get(action, [])


def setup_needed(paths: store.Paths) -> str | None:
    """What is missing before agora can reach Drive: "rclone", "auth", or nothing."""
    if not shutil.which(os.environ.get("AGORA_RCLONE", "rclone")):
        return "rclone"
    return None if (paths.config / "rclone.conf").exists() else "auth"


def authorize(paths: store.Paths, say=print) -> int:
    """`rclone config create` with rclone's own client: it opens the browser; we pass its lines on.

    `say` is where the lines go - the waiting window when the interactive mode runs
    it, stdout otherwise - rather than the thread printing behind the screen's back
    (review K3).
    """
    paths.config.mkdir(parents=True, exist_ok=True)
    argv = [os.environ.get("AGORA_RCLONE", "rclone"), "config", "create", "gdrive", "drive", "scope=drive.file",
            "--config", str(paths.config / "rclone.conf")]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if "token" not in line.lower():   # the token is a secret: never on screen
            say(line.rstrip())
    return proc.wait()


# --- small windows -------------------------------------------------------------

def killpg(pgid: int, sig) -> bool:
    """Send `sig` to a whole process group; False if there is nothing left to signal."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False            # the group is gone, which is the answer we wanted
    except OSError:
        return False
    return True


def group_alive(pgid: int) -> bool:
    """Whether anyone is still in this process group.

    Not whether *our* child is running: agora can exit first and leave the agent it
    started in the group (review M1), and that agent is exactly what an escalation
    is for. Signal 0 asks without sending anything.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class Choose(ModalScreen):
    """A few options in the middle of the screen; dismisses with the index chosen, or None on Esc."""
    BINDINGS = [Binding("escape", "dismiss(None)", "取消")]

    def __init__(self, title: str, options: list[str], note: str = ""):
        super().__init__()
        self.title_, self.options, self.note = title, options, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(self.title_, classes="box-title")
            yield OptionList(*self.options)
            if self.note:
                yield Static(self.note, classes="note")
            yield Static("Enter 確定   Esc 取消", classes="hint")

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)


class AskText(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "取消")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(self.title_, classes="box-title")
            yield Input(self.text)
            yield Static("Enter 確定   Esc 取消", classes="hint")

    @on(Input.Submitted)
    def done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


#: What Esc escalates to, and how long each step is given (review V2). SIGINT first:
#: it is what Ctrl-C would send, and it is what cli.main turns into exit 130 after
#: it has kept the pending record. An agent that ignores it gets SIGTERM, then
#: SIGKILL - always to the whole process group, so the agent goes with it.
ESCALATION = (signal.SIGTERM, signal.SIGKILL)


#: What a group is sent after SIGINT, and how long each step is given (review V2).
ESCALATION = (signal.SIGTERM, signal.SIGKILL)
ESCALATE_AFTER = 5.0


class Run(ModalScreen):
    """One command-mode command in a child process, with a progress bar and Esc.

    A child process rather than a thread, because the interruption has to be a real
    one and it has to reach the agent the command starts - the opencode writing a
    summary - which is why the child is given its own process group (review V2).
    Only our own `[agora] … k/N` lines move the bar (review V8): a title, a date or
    a path can easily contain digits and a slash. Dismisses (code, output).
    """

    BINDINGS = [Binding("escape", "stop", "中斷", priority=True),
                Binding("ctrl+q", "stop", "中斷", priority=True)]   # review M3
    # Only our own progress lines: `[agora] <word> k/N` and nothing after (review
    # V8, L1). A failure line carries rclone's tail, and its timestamp reads as 2026/10.
    PROGRESS = re.compile(r"^\[agora\] \S+ (\d+)/(\d+)(?:\s|$)")

    def __init__(self, title: str, argv: list[str], spawn):
        super().__init__()
        self.title_, self.argv, self.spawn = title, argv, spawn
        self.lines: list[str] = []
        self.proc = None
        self.started = time.monotonic()
        self.stopping = False
        self.signals: list[int] = []      # what was sent, in order (a test reads this)

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(self.title_, classes="box-title")
            yield ProgressBar(total=None, show_eta=False, id="bar")
            yield Static("", id="last", classes="note")
            yield Static("Esc 中斷（之後重跑同一個動作會接著做）", classes="hint")

    def on_mount(self) -> None:
        self.proc = self.spawn(self.argv)
        threading.Thread(target=self.read, daemon=True).start()
        self.set_interval(0.1, self.tick)

    def read(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.append(line.rstrip())
            code = self.proc.wait()
        except Exception as e:       # never leave the window up forever (review L6)
            code, self.lines = 2, self.lines + [f"讀不到輸出：{e}"]
        # The app may already be gone (ctrl+q closed it); that is not our problem.
        with contextlib.suppress(Exception):
            self.app.call_from_thread(self.dismiss, (code, "\n".join(self.lines)))

    def tick(self) -> None:
        step = next((m for m in map(self.PROGRESS.match, reversed(self.lines))
                     if m and 1 <= int(m.group(1)) <= int(m.group(2))), None)
        if step:
            self.query_one("#bar", ProgressBar).update(total=int(step.group(2)),
                                                        progress=int(step.group(1)))
        spent = int(time.monotonic() - self.started)
        last = self.lines[-1] if self.lines else ""
        prefix = "中斷中… " if self.stopping else ""
        self.query_one("#last", Static).update(f"{prefix}（{spent} 秒）{last}")

    def action_stop(self) -> None:
        """Esc (or ctrl+q): stop the whole group, and go on stopping it after we go.

        A second Esc does nothing: the escalation is already running, and a second
        SIGINT would interrupt agora while it is keeping its pending record (review L3).
        """
        if self.stopping or self.proc is None or self.proc.poll() is not None:
            return
        self.stopping = True
        group = getattr(self.proc, "pid", None)
        if isinstance(group, int):
            self.app.stop_group(group)
        else:
            with contextlib.suppress(Exception):
                self.proc.send_signal(signal.SIGINT)   # a fake in a test


class Busy(ModalScreen):
    """Run `work_(say)` in a thread while a window says so; dismiss (value, lines, error).

    The work is handed a `say` instead of the thread redirecting the program's
    stdout, which took every other thread's output with it (review K3). Only the
    first-run sync and rclone's own authorization still run this way - an action
    is a child process now - and both say what they have to say through `say`.
    """

    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, title: str, work_):
        super().__init__()
        self.title_, self.work_, self.lines, self.started = title, work_, [], time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(self.title_, classes="box-title")
            yield Static("", id="spin")
            yield Static("", id="last", classes="note")

    def on_mount(self) -> None:
        self.set_interval(0.12, self.tick)
        self.run()

    def tick(self) -> None:
        spent = time.monotonic() - self.started
        self.query_one("#spin", Static).update(f"{self.SPIN[int(spent * 8) % len(self.SPIN)]} 執行中…（{int(spent)} 秒）")
        self.query_one("#last", Static).update(self.lines[-1] if self.lines else "")

    @work(thread=True)
    def run(self) -> None:
        value = error = None

        def say(text: str) -> None:
            self.lines.append(str(text))

        try:
            value = self.work_(say)
        except Exception as e:   # shown in the result window, never a traceback over the screen
            error = e
        self.app.call_from_thread(self.dismiss, (value, "\n".join(self.lines), error))


class Tell(ModalScreen):
    """The last lines of an action's output; any key closes it."""

    def __init__(self, title: str, text: str, ok: bool = True):
        super().__init__()
        self.title_, self.ok = f"{'✓' if ok else '✗'} {title}", ok
        self.lines = [line for line in text.strip().splitlines() if line.strip()][-12:] or ["（沒有輸出）"]

    def compose(self) -> ComposeResult:
        with Vertical(classes="box" + ("" if self.ok else " failed")):
            yield Static(self.title_, classes="box-title")
            yield Static(Text("\n".join(self.lines)))
            yield Static("按任意鍵回到清單", classes="hint")

    def on_key(self, event) -> None:
        event.stop()
        self.dismiss(None)


# --- the app ---------------------------------------------------------------------

class AgoraApp(App):
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #121212; }
    #bar { height: 1; background: #1c1c1c; }
    #main { layout: horizontal; height: 1fr; }
    #main.narrow { layout: vertical; }
    #left { width: 55%; background: #303030; }
    #right { width: 1fr; background: #303030; padding: 0 1; }
    #main.narrow #left { width: 100%; height: 50%; }
    #main.narrow #right { width: 100%; height: 1fr; }
    #left:focus-within, #right:focus { background: #000000; }
    DataTable { background: transparent; }
    DataTable > .datatable--cursor { background: #3a3a3a; text-style: bold; }
    DataTable > .datatable--header { background: transparent; color: #8a8a8a; text-style: bold; }
    #pinned { color: #8a8a8a; }
    #filterbar { height: 1; display: none; }
    #filterbar.on { display: block; }
    #mode { width: auto; padding: 0 1; background: #5f0000; }
    #filter { border: none; height: 1; padding: 0; background: #1c1c1c; }
    #msg { height: 1; color: #ffd75f; }
    #msg.failed { color: #ff5f5f; }
    .box { width: 70; height: auto; max-height: 80%; padding: 0 1; border: round #00afaf; background: #1c1c1c; }
    .box.failed { border: round #ff5f5f; }
    Choose, AskText, Busy, Run, Tell { align: center middle; }
    .box-title { text-style: bold; color: #00d7d7; }
    .note, .hint { color: #8a8a8a; }
    OptionList { height: auto; max-height: 8; background: transparent; border: none; }
    """
    BINDINGS = [
        Binding("tab", "next_tab", "換頁", priority=True),
        Binding("shift+tab", "toggle_focus", "左右", priority=True),
        Binding("space", "mark", "勾選"),
        Binding("a", "mark_all", "全選／全不選"),
        Binding("enter", "primary", "接續／匯入", priority=True),   # the table would take it for itself
        Binding("m", "merge", "合併"),
        Binding("e", "edit", "改標頭"),
        Binding("d", "delete", "刪除"),
        Binding("slash", "filter", "篩選"),
        Binding("ctrl+t", "search_mode", "標題／內文", priority=True),
        Binding("r", "refresh_cache", "更新快取"),
        Binding("s", "sync", "寫回 Drive"),
        Binding("q", "quit", "離開"),
    ]

    def __init__(self, paths: store.Paths, cli, agents: list | None = None, check_setup: bool = True):
        super().__init__()
        self.paths, self.cli, self.check_setup = paths, cli, check_setup
        self.agents = agents if agents is not None else [cli.load_agent(name) for name in cli.AGENTS]
        self.index = store.Index(paths)
        self.rows: dict[str, list[Row]] = {"agora": [], "import": []}
        self.tab, self.text, self.content = "agora", "", False      # content: search the conversations
        self.matches: dict[str, set[str] | None] = {"agora": None, "import": None}
        self.marked: set[str] = set()
        self.cache: dict[str, tuple[str, str]] = {}
        self.status = ""
        self._stopped = False        # an Esc went through, whatever the exit code says
        self._groups: set[int] = set()   # process groups an action started

    def compose(self) -> ComposeResult:
        yield Static(id="bar")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=False,
                                cursor_foreground_priority="renderable")   # the red bar and agent colours stay
            with VerticalScroll(id="right"):
                yield Static(id="pinned")
                yield Static(id="history")
        with Horizontal(id="filterbar"):
            yield Static("標題", id="mode")
            yield Input(id="filter", placeholder="輸入後按 Enter；Esc 清掉；ctrl+t 切換標題／內文")
        yield Static(id="msg")
        yield Footer()

    # -- setup and data ------------------------------------------------------

    def on_mount(self) -> None:
        self.start()

    @work
    async def start(self) -> None:
        if self.check_setup and not await self.setup():
            self.exit()
            return
        if self.check_setup:
            _, out, _ = await self.push_screen_wait(
                Busy("同步 Drive", lambda say: store.sync(self.paths, throttle=True, warn=say)))
            self.status = "離線：只有本機資料" if "連不上 Drive" in (out or "") else ""
        self.reload()
        if not self.rows["agora"]:
            self.say("Agora 還沒有 Session：按 Tab 到「未匯入」，空白鍵勾選後按 Enter 匯入")
        self.query_one("#table").focus()

    async def setup(self) -> bool:
        """Guide a first run: rclone missing, or Drive not authorized yet (design 5.9)."""
        need = setup_needed(self.paths)
        if need == "rclone":
            await self.push_screen_wait(Choose("需要 rclone", ["離開"], "請先在終端機執行：brew install rclone\n裝好之後再打 agora"))
            return False
        if need == "auth":
            note = ("agora 把 Session 存在你的 Google Drive。\n用 rclone 內建的 client 授權，權限只有 drive.file：\n"
                    "只看得到 agora 自己建的檔案。")
            if await self.push_screen_wait(Choose("還沒設定 Google Drive", ["用瀏覽器授權", "離開"], note)) != 0:
                return False
            code, out, error = await self.push_screen_wait(
                Busy("請在瀏覽器完成授權", lambda say: authorize(self.paths, say)))
            if error or code != 0 or setup_needed(self.paths):
                await self.push_screen_wait(Tell("授權沒有完成", f"{out}\n{error or ''}", ok=False))
                return False
        return True

    def reload(self, keep_marked: bool = False) -> None:
        self.index = store.Index(self.paths)       # local only; no full sync after every action (T6)
        self.rows = {"agora": agora_rows(self.index, []), "import": import_rows(self.index, self.agents, self.paths)}
        if not keep_marked:
            self.marked.clear()
        else:
            self.marked &= {r.key for r in self.rows[self.tab]}   # only rows that are still there
        self.cache.clear()
        self.show()

    def shown(self) -> list[Row]:
        return filtered(self.rows[self.tab], "" if self.content else self.text, self.matches[self.tab])

    def show(self, keep: str | None = None) -> None:
        """Fill the table for the current tab, keeping the cursor on `keep` if it is still there."""
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_column("", key="gutter", width=1)
        table.add_column("", key="mark", width=1)
        for name in COLUMNS[self.tab]:
            table.add_column(name, key=name)
        rows = self.shown()
        for row in rows:
            cells = [Text(c, style=AGENT_STYLE.get(c, "")) for c in row.cells]
            table.add_row(Text("▌", style="#585858"), self.tick(row), *cells, key=row.key)
        if keep and keep in {r.key for r in rows}:
            table.move_cursor(row=[r.key for r in rows].index(keep))
        self.paint_bar()
        self.gutter()
        self.preview()

    def tick(self, row: Row) -> Text:
        return Text("✓", style="bold #ffd75f") if row.key in self.marked else Text(" ")

    def current(self) -> Row | None:
        rows, table = self.shown(), self.query_one("#table", DataTable)
        return rows[table.cursor_row] if rows and 0 <= table.cursor_row < len(rows) else None

    def paint_bar(self) -> None:
        bar = Text(" agora ", style="bold #000000 on #00afaf")
        bar.append(" ")
        for t in TABS:   # the current tab is a lit chip, the other a dim one (feedback 7)
            bar.append(f" {TAB_NAMES[t]} {len(self.rows[t])} ",
                       style="bold #000000 on #ffd75f" if t == self.tab else "#bcbcbc on #3a3a3a")
            bar.append(" ")
        if self.text:
            bar.append(f" {'內文' if self.content else '標題'}：{self.text} ", style="#ffffff on #5f0000")
        hidden = self.hidden_marked()
        if hidden:
            bar.append(f" 另有 {hidden} 個勾選被篩選掉", style="#ffd75f")
        if self.status:
            bar.append(f"  {self.status}", style="#ff5f5f")
        self.query_one("#bar", Static).update(bar)

    def gutter(self) -> None:
        """Every row has a grey bar on the left; the current one is red (feedback 2, like mlp's fzf)."""
        table = self.query_one("#table", DataTable)
        for n, row in enumerate(self.shown()):
            colour = "#ff5f5f" if n == table.cursor_row else "#585858"
            table.update_cell(row.key, "gutter", Text("▌", style=f"bold {colour}"))

    def say(self, text: str, failed: bool = False) -> None:
        msg = self.query_one("#msg", Static)
        msg.update(text)
        msg.set_class(failed, "failed")

    # -- preview ---------------------------------------------------------------

    def preview(self) -> None:
        row = self.current()
        if row is None:
            self.put_preview("", "")
            return
        if row.key in self.cache:
            self.put_preview(*self.cache[row.key])
        elif self.tab == "agora":
            self.cache[row.key] = agora_preview(self.paths, self.index, row.key)
            self.put_preview(*self.cache[row.key])
        else:                    # the last message at once, the whole thing when it is read (feedback 3)
            agent = next((a for a in self.agents if a.name == row.agent), None)
            if agent:
                self.put_preview(*import_preview(agent, row.key.split(":", 1)[1]))
                self.load_full(agent, row.key, row.updated)

    @work(thread=True, exclusive=True, group="preview")
    def load_full(self, agent, key: str, updated: str | None) -> None:
        result = import_preview(agent, key.split(":", 1)[1], True, self.paths, updated)
        self.call_from_thread(self.loaded, key, result)

    def loaded(self, key: str, result: tuple[str, str]) -> None:
        self.cache[key] = result
        row = self.current()
        if row and row.key == key:
            self.put_preview(*result)

    def put_preview(self, pinned: str, history: str) -> None:
        self.query_one("#pinned", Static).update(pinned)
        # Rendered once by rich and then only scrolled, so a long history scrolls smoothly (feedback 4, 5).
        self.query_one("#history", Static).update(Markdown(history) if history else "")
        self.call_after_refresh(self.query_one("#right", VerticalScroll).scroll_end, animate=False)

    @on(DataTable.RowHighlighted)
    def moved(self) -> None:
        self.gutter()
        self.preview()

    def on_resize(self, event) -> None:
        self.query_one("#main").set_class(event.size.width < WIDE, "narrow")

    # -- keys --------------------------------------------------------------------

    def check_action(self, action: str, parameters) -> bool | None:
        """Show only the keys that work here (the key bar follows the tab and the focus)."""
        in_preview = isinstance(self.focused, VerticalScroll)
        if action == "primary":          # a priority key: only for the list, or Enter in a window or input breaks
            return isinstance(self.focused, DataTable)
        if action in ("merge", "edit", "delete"):
            return self.tab == "agora" and not in_preview
        if action in ("mark", "primary", "filter", "refresh_cache", "sync"):
            return not in_preview
        return True

    def action_next_tab(self) -> None:
        self.tab = TABS[(TABS.index(self.tab) + 1) % len(TABS)]
        self.say("")
        self.show()
        self.refresh_bindings()

    def action_toggle_focus(self) -> None:
        target = "#right" if self.focused is self.query_one("#table") else "#table"
        self.query_one(target).focus()
        self.refresh_bindings()

    def action_mark(self) -> None:
        row = self.current()
        if row:
            self.marked.symmetric_difference_update({row.key})
            table = self.query_one("#table", DataTable)
            table.update_cell(row.key, "mark", self.tick(row))   # the cursor stays put (user's call)

    def action_filter(self) -> None:
        self.query_one("#filterbar").add_class("on")
        self.query_one("#filter", Input).focus()

    def action_search_mode(self) -> None:
        self.content = not self.content
        self.query_one("#mode", Static).update("內文" if self.content else "標題")
        if self.text:
            self.search(self.text)

    @on(Input.Submitted, "#filter")
    def filter_done(self, event: Input.Submitted) -> None:
        self.query_one("#filterbar").remove_class("on")
        self.query_one("#table").focus()
        self.search(event.value.strip())

    def on_key(self, event) -> None:
        if event.key == "escape" and self.query_one("#filterbar").has_class("on"):
            self.query_one("#filter", Input).value = ""
            self.query_one("#filterbar").remove_class("on")
            self.query_one("#table").focus()
            self.search("")

    def search(self, text: str) -> None:
        self.text = text
        self.matches = {"agora": None, "import": None}
        if text and self.content:
            # Agora: its full-text index, at once. Import: each adapter's search, rows coming in as found (feedback 9).
            self.matches["agora"] = {f"agora:{u}" for u, _h, _s in self.index.search([((h.TEXT_KEY,), "~=", text)])}
            self.matches["import"] = set()
            self.say(f"內文搜尋「{text}」中…")
            self.find_in_agents(text)
        self.show()

    @work(thread=True, exclusive=True, group="search")
    def find_in_agents(self, text: str) -> None:
        for agent in self.agents:   # the cache first (fast), then only what is not cached or is stale
            rows = [r for r in self.rows["import"] if r.agent == agent.name]
            missing = {r.key.split(":", 1)[1] for r in rows
                       if not cache.is_fresh(self.paths, agent.name, r.key.split(":", 1)[1], r.updated)}
            seen: set[str] = set()
            try:
                later = agent.search_text(text, only=missing) if missing else ()
                for source in (cache.search_cached(self.paths, agent.name, text), later):
                    for session_id in source:
                        if session_id not in seen:
                            seen.add(session_id)
                            self.call_from_thread(self.found, text, f"{agent.name}:{session_id}")
            except Exception:    # a search must never take the screen down
                continue
        self.call_from_thread(self.say, f"內文搜尋「{text}」完成")

    def found(self, text: str, key: str) -> None:
        if self.text == text and self.content and self.matches["import"] is not None:
            self.matches["import"].add(key)
            if self.tab == "import":
                row = self.current()
                self.show(keep=row.key if row else None)

    # -- actions -------------------------------------------------------------------

    def chosen_rows(self) -> list[Row]:
        """The rows an action acts on, in the order they are on screen.

        Only the ones the user can see. A row that is marked but filtered out is not
        acted on (review V4): pressing `d` should not delete a Session that is not on
        the screen. The header says how many are hidden, so nothing disappears
        quietly - and merge reads its sources top to bottom, as they are shown.
        """
        marked = [r for r in self.shown() if r.key in self.marked]
        if marked:
            return marked
        row = self.current()
        return [row] if row else []

    def hidden_marked(self) -> int:
        """Marked rows the filter is hiding (review V4)."""
        visible = {r.key for r in self.shown()}
        return len([r for r in self.rows[self.tab]
                    if r.key in self.marked and r.key not in visible])

    def action_quit(self) -> None:
        """ctrl+q while a command is running stops it first (review M3).

        Textual's own ctrl+q is a priority binding on the app, so it reaches over a
        modal screen: with the window up, quitting used to close everything and leave
        the command running with nobody watching. Whichever of the two bindings wins,
        the answer is the same - stop the group - and a second one is ignored while
        the escalation is already going.
        """
        screen = self.screen
        if isinstance(screen, Run) and screen.proc is not None:
            screen.action_stop()
            return
        self.exit()

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        """The child process an action runs in; a test replaces this (design, T2).

        stdin is /dev/null: start_new_session only detaches the child from the
        terminal, but it still inherits fd 0, and Textual is reading that terminal
        in raw mode - one read by the agent or by rclone would take the user's
        keystrokes (review V1). Its own group is what lets Esc reach the agent the
        command starts, and PYTHONUNBUFFERED keeps the ids off a buffer until the
        end (review V1).
        """
        return subprocess.Popen(
            [sys.executable, "-m", "agora.cli", *argv],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",   # review L6: a bad byte must not wedge the window
            bufsize=1, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})

    async def act(self, title: str, argv: list[str]) -> bool:
        """One command in a child process, under a window with its progress and Esc.

        Says whether it was stopped, so a caller with more than one command does not
        start the next one after an interruption (review M2) - and clears the
        selection only when the command succeeded, so a failure can be run again by
        pressing the same key (spec「成功後清掉勾選」).

        Re-running the same action carries on from where it stopped (design 5.11):
        the command mode is the one that knows how to skip what is already done.
        """
        self._stopped = False
        code, out = await self.push_screen_wait(Run(title, argv, self.spawn))
        # Esc wins over the exit code (review L2): the child may have finished in the
        # same instant, and a -9 from the OOM killer is not an interruption. What we
        # know is that we sent SIGINT to a group that was still running.
        stopped = bool(self._stopped or code == 130)
        if stopped:
            note = "已中斷；重跑同一個動作會接著做"
        elif code == 0:
            note = "完成"
        elif code == 3:
            note = "已存進 outbox，之後的指令會自動再送"
        else:
            note = "沒有全部成功，訊息在下面"
        await self.push_screen_wait(Tell(f"{title}{'（已中斷）' if stopped else ''}",
                                         f"{note}\n\n{out}", code == 0))
        self.reload(keep_marked=code != 0)
        self.say(note, failed=code != 0)
        return stopped

    def stop_group(self, pgid: int) -> None:
        """SIGINT, then SIGTERM, then SIGKILL to a process group (review V2).

        Here, on the app, rather than in the waiting window: the window goes away
        as soon as agora itself exits, and whatever it started can still be running
        in that group (review M1). Whether it is gone is `killpg(pgid, 0)` saying so,
        not our child having exited. Timers belong to the app, so they outlive the
        screen.
        """
        self._groups.add(pgid)
        if killpg(pgid, signal.SIGINT):
            self._stopped = True
            for sig in ESCALATION:
                self.set_timer(ESCALATE_AFTER, lambda sig=sig: self._step(pgid, sig))

    def _step(self, pgid: int, sig) -> None:
        if group_alive(pgid):
            killpg(pgid, sig)
        else:
            self._groups.discard(pgid)      # nothing left in it

    def stop_everything(self) -> None:
        """Leaving with a command still running: SIGTERM to whatever is left."""
        for pgid in list(self._groups):
            killpg(pgid, signal.SIGTERM)

    def outside(self, argv: list[str]) -> int:
        """Hand the whole terminal over (an agent, an editor), then come back."""
        with self.suspend():
            code = self.cli.main(argv)
            with contextlib.suppress(KeyboardInterrupt, EOFError):
                input("\n按 Enter 回到選單…")
        return code

    @work
    async def action_primary(self) -> None:
        rows = self.chosen_rows()
        if not rows:
            return
        if self.tab == "import":
            # One command per agent: they are separate stores, and each of them gets
            # its own k/N progress in the window (V6).
            for agent in dict.fromkeys(r.agent for r in rows):
                mine = [r for r in rows if r.agent == agent]
                if await self.act(f"匯入 {len(mine)} 個（{agent}）",
                                  argv_for("import", mine, None, None)[0]):
                    break      # Esc means stop this action, not half of it (review M2)
            return
        row = self.current()
        pick = await self.push_screen_wait(Choose("用哪個 agent 接續？", ["opencode", "claude"]))
        if pick is None:
            return
        workdir = await self.ask_dir(row)
        if workdir is None:
            return
        code = self.outside(argv_for("continue", [row], ("opencode", "claude")[pick], workdir)[0])
        self.reload()
        self.say("完成" if code == 0 else "接續沒有成功，訊息在上一個畫面", failed=code != 0)

    async def ask_dir(self, row: Row) -> str | None:
        """Where to open the agent: the source's directory when it exists here, and always changeable (review T7)."""
        known = bool(row.dir and os.path.isdir(row.dir))
        workdir = row.dir if known else os.getcwd()
        note = "" if known else "⚠ 來源沒有記錄目錄（或這台機器上沒有），會在目前目錄開"
        while True:
            pick = await self.push_screen_wait(Choose("在哪裡開？", [f"在這裡開：{workdir}", "改目錄…"], note))
            if pick is None:
                return None
            if pick == 0:
                return workdir
            typed = await self.push_screen_wait(AskText("工作目錄", workdir))
            if typed and os.path.isdir(os.path.expanduser(typed)):
                workdir, note = os.path.expanduser(typed), ""
            elif typed is not None:
                note = f"⚠ 找不到這個目錄：{typed}"

    @work
    async def action_merge(self) -> None:
        rows = self.chosen_rows()          # the visible marked ones, in screen order
        if len(rows) < 2:
            self.say("合併要先用空白鍵勾選至少兩個")
            return
        pick = await self.push_screen_wait(Choose(f"合併 {len(rows)} 個：由誰寫要約？", ["opencode", "claude"],
                                                  "每個來源叫一次 AI；內容會送到那個 agent 的模型供應商"))
        if pick is not None:
            await self.act("合併", argv_for("merge", rows, ("opencode", "claude")[pick], None)[0])

    def action_mark_all(self) -> None:
        """`a`: mark every row on screen, or unmark them if they all are.

        Only the visible ones: a row the filter hides keeps whatever state it was
        left in (spec「全選切換」), so filtering back and forth does not quietly
        unmark what was marked on purpose.
        """
        keys = [r.key for r in self.shown()]
        if not keys:
            return
        if all(key in self.marked for key in keys):
            self.marked -= set(keys)
        else:
            self.marked |= set(keys)
        self.show()

    def action_edit(self) -> None:
        row = self.current()
        if row:
            code = self.outside(argv_for("edit", [row], None, None)[0])   # the editor needs the terminal
            self.reload()
            self.say("完成" if code == 0 else "改標頭沒有成功", failed=code != 0)

    @work
    async def action_refresh_cache(self) -> None:
        """Pull what is marked: a row on the agora tab off Drive, one on the import
        tab into the full-text cache. There is no "all of them" here either."""
        rows = self.chosen_rows()
        if not rows:
            self.say("先選要拉下來的 Session", failed=True)
            return
        await self.act(f"拉下 {len(rows)} 個", argv_for("pull", rows, None, None)[0])

    @work
    async def action_sync(self) -> None:
        rows = self.chosen_rows()
        if not rows:
            self.say("先選要寫回的 Session", failed=True)
            return
        if await self.push_screen_wait(Choose(f"把 {len(rows)} 個寫回 Drive？", ["取消", "確定"],
                                              "同名的檔案直接覆蓋；Drive 上多的不動")) == 1:
            await self.act("寫回 Drive", argv_for("push", rows, None, None)[0])

    @work
    async def action_delete(self) -> None:
        rows = self.chosen_rows()            # every marked row, or the one under the cursor
        if not rows:
            return
        listed = "\n".join(f"{r.key}「{r.cells[1]}」" for r in rows[:6]) + ("\n…" if len(rows) > 6 else "")
        if await self.push_screen_wait(Choose(f"把 {len(rows)} 個移到 Drive 垃圾桶？", ["取消", "確定"], listed)) == 1:
            await self.act(f"刪除 {len(rows)} 個", argv_for("delete", rows, None, None)[0])


def main(paths: store.Paths) -> int:
    from agora import cli       # the command mode does the work; imported here to avoid a cycle
    app = AgoraApp(paths, cli)

    def on_hangup(_signum, _frame):
        # The terminal window is gone. The children are in their own sessions, so
        # they would not get the SIGHUP - they would just keep writing summaries or
        # deleting on Drive (review M3).
        app.stop_everything()
        raise SystemExit(130)

    with contextlib.suppress(ValueError, AttributeError, OSError):
        signal.signal(signal.SIGHUP, on_hangup)
    try:
        app.run()
    finally:
        app.stop_everything()   # ctrl+q closes the app without telling the group
    return 0

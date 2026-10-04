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
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import partial
from datetime import datetime
from pathlib import Path
from typing import Iterator

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.worker import get_current_worker
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import Checkbox, DataTable, Input, OptionList, ProgressBar, Static, TextArea
from textual.widgets.text_area import Edit, EditResult, LanguageDoesNotExist, Selection

from agora import cache
from agora import header as h
from agora import store

WIDE = 100                       # columns from which the preview goes to the right
TABS = ("agora", "import")
TAB_NAMES = {"agora": "Agora", "import": "未匯入"}
COLUMNS = {"agora": ("id", "標題", "agent", "更新", "雲端"),
           "import": ("id", "標題", "agent", "更新", "目錄")}
CLOUD_YES, CLOUD_NO, CLOUD_NEW = "✓", "✗", "未上傳"
TITLE_MAX = 36                   # the title column is cut here, so the others stay on screen
AGENT_STYLE = {"opencode": "cyan", "claude": "#ff8700", "merge": "green"}
#: The key bar, per tab and side (spec「按鍵」): what a key does here, and only here.
KEYS = {
    "list": {
        "agora": [("[ ]", "換頁"), ("Tab", "切焦點"), ("空白", "勾選"), ("a", "全選／全不選"),
                  ("enter", "接續"), ("m", "合併"), ("e", "改標頭"), ("d", "刪除"),
                  ("p", "pull"), ("P", "push"), ("/", "篩選（邊打邊篩）"),
                  ("ctrl+t", "標題／內文"), ("q", "離開")],
        "import": [("[ ]", "換頁"), ("Tab", "切焦點"), ("空白", "勾選"), ("a", "全選／全不選"),
                   ("enter", "匯入"), ("p", "pull"), ("/", "篩選（邊打邊篩）"),
                   ("ctrl+t", "標題／內文"), ("q", "離開")],
    },
    "preview": [("j/k", "移動"), ("g/G", "最前／最後"), ("/", "搜尋"), ("n/N", "下一個／上一個"),
                ("Esc", "清標亮"), ("Tab", "切焦點"), ("q", "離開")],
    "search": [("Enter", "送出"), ("Esc", "關掉"), ("Tab", "切焦點")],
}


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


def agora_rows(index: store.Index, filters: list, paths: store.Paths | None = None) -> list[Row]:
    """The agora tab, newest first, each row saying whether Drive still has it.

    The cloud column reads the marker sync left in the index and the outbox - no
    call to Drive from here. 「未上傳」 is its own state: a session staged here is
    not a session another machine deleted (T2 3.1).
    """
    staged = store.waiting_ulids(paths) if paths is not None else set()
    rows = []
    for ulid, hdr, _snippet in index.search(filters):
        agora = h.agora_of(hdr)
        source = agora.get("source") or {}
        kind = source.get("agent") or agora.get("relation") or ""
        title = str(hdr.get("title") or "")
        updated = str(agora.get("updated_at") or store.sort_date(hdr))
        cloud = CLOUD_NEW if ulid in staged else (CLOUD_YES if index.cloud_has(ulid) else CLOUD_NO)
        rows.append(Row(f"agora:{ulid}", [ulid[-8:], _short(title), kind, _when(updated), cloud],  # the random part (T9)
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

PREVIEW_CHUNK = 30 << 10     # about 30 KB a step; a session.md is 340 KB to 3 MB (T6)
FILTER_IDLE = 0.3            # content search: this long without a keystroke before it reads the agents (T7 P1)
PREVIEW_IDLE = 0.15          # the cursor has to rest this long before anything is read


def read_tail(path: Path, at: int | None = None, size: int | None = None) -> tuple[str, int]:
    """(text, where it starts): the `size` bytes of `path` ending at offset `at`.

    `at` is an absolute offset - `None` is the end of the file - and the offset handed
    back is the one the next call passes, so the steps walk backwards one after another
    (review Y6: a "bytes from the end" answer fed back as "bytes from the end" jumped to
    the start of the file). seek, never the whole file: only the end of a conversation is
    ever on screen. The cut lands just after a `## ` heading or a blank line, so a message
    is not cut in half, and both ends of what is kept are then just after a newline, so a
    multibyte character cannot be broken either.
    """
    if size is None:
        size = PREVIEW_CHUNK
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell() if at is None else min(at, f.tell())
            start = max(0, end - size)
            f.seek(start)
            raw = f.read(end - start)
    except OSError:
        return "", 0
    if start:
        cut = min((i for i in (raw.find(b"\n## "), raw.find(b"\n\n")) if i > 0), default=0)
        raw, start = raw[cut + 1:], start + cut + 1
    return raw.decode("utf-8", "ignore"), start


def _header_end(text: str) -> int:
    """Where the YAML front matter ends, in characters - the same rule for both the preview
    and the search over the file, so they cannot count different bodies (W1, W10)."""
    if not text.startswith("---"):
        return 0
    end = text.find("\n---", 3)
    return text.find("\n", end + 1) + 1 if 0 <= end <= PREVIEW_CHUNK else 0


def _no_header(text: str) -> str:
    """The YAML front matter is metadata, not conversation, so it is not previewed."""
    return text[_header_end(text):]


def find_in_lines(lines, query: str) -> Iterator[tuple[int, int, int]]:
    """Every match of `query` in `lines`, as `(row, start, end)`, line by line, ignoring case.

    The file and the screen both go through this, on decoded text, and nothing is carried from
    one to the other as an offset - a match is a match on both sides or on neither (W1).
    """
    if not query:
        return
    for row, line in enumerate(lines):
        for found in re.finditer(re.escape(query), line, re.IGNORECASE):
            yield row, found.start(), found.end()


def count_in(text: str, query: str) -> int:
    """How many matches `query` has in `text` - the file's side of the count."""
    return sum(1 for _ in find_in_lines(text.splitlines(), query))


class Preview:
    """One row's preview: the text read so far, and how much of the file is left above it.

    Only what was read is rendered (T6). The first step is the tail; every time the pane
    reaches its top one more step is read and put above it.
    """

    def __init__(self, path: Path | None, pinned: str = "", text: str = ""):
        # `at` is where the part that is already here starts, an absolute offset; None is
        # "nothing read yet". 0 is the beginning of the file, which is how "all of it is
        # here" is told apart from "none of it is" (review Y1).
        self.path, self.pinned, self.at = path, pinned, None
        self._chunks: list[str] = [text] if text else []
        self._stat: tuple[int, float] | None = None
        self._counts: tuple | None = None    # (word, size, mtime, at, total, above) - see counts()
        if path is not None and path.is_file():
            try:
                st = path.stat()
                self._stat = (st.st_size, st.st_mtime)
            except OSError:
                pass

    @property
    def text(self) -> str:
        return "\n".join(self._chunks)

    @text.setter
    def text(self, value: str) -> None:
        self._chunks = [value] if value else []

    def is_stale(self) -> bool:
        """Whether the backing file has changed since the preview was first read (W11)."""
        if self.path is None:
            return False
        try:
            st = self.path.stat()
            return self._stat is not None and (st.st_size, st.st_mtime) != self._stat
        except OSError:
            return False

    @property
    def last_only(self) -> bool:
        """Whether this is the last message alone, with no file behind it (spec「預覽區搜尋」)."""
        return self.path is None and self.pinned.startswith("最後一則")

    def _file_key(self) -> tuple[int, float] | None:
        """The file's (size, mtime) now, or None - what the counts are remembered under, so a
        file that changed under us is counted again (review T2)."""
        try:
            st = self.path.stat() if self.path is not None else None
        except OSError:
            return None
        return (st.st_size, st.st_mtime) if st is not None else None

    def counts(self, query: str) -> tuple[int, int]:
        """(how many matches the whole session has, how many of them are above what is read).

        The file is read once, the front matter is skipped by the same rule as the preview, and
        both sides are counted by `find_in_lines` - so for every `at`,
        `above + what is on screen == found`, and the k-th match of the session is the
        (k - above)-th one on screen (design「搜尋：計數」, W1).

        Both numbers are remembered for the word, for the file's size and mtime, and for how much
        has been read: `n` asks again on every press, and reading and counting megabytes each
        time is what made it slow (review T2).
        """
        if self.path is None or not self.path.is_file():
            return count_in(self.text, query), 0     # the last message: all of it is here
        key = (query, self._file_key(), self.at)
        if self._counts is not None and self._counts[:3] == key:
            return self._counts[3], self._counts[4]
        try:
            raw = self.path.read_bytes()
        except OSError:
            return 0, 0
        text = raw.decode("utf-8", "ignore")
        start = _header_end(text)
        found = count_in(text[start:], query)
        above = found if self.at is None else count_in(       # nothing read yet: all of it is above
            text[start:max(start, len(raw[:self.at].decode("utf-8", "ignore")))], query)
        self._counts = (*key, found, above)
        return found, above

    def count_loaded(self, query: str, above: int) -> None:
        """How many matches are above now that more has been read.

        `step` counts what each step brings down, so the file does not have to be read again to
        know (review T2).
        """
        if self._counts is not None and self._counts[0] == query and self._counts[1] == self._file_key():
            self._counts = (query, self._counts[1], self.at, self._counts[3], above)

    def step(self) -> str | None:
        """Read one more step - the tail first, the step above after that.

        The step's text, or `None` when there is nothing above it to read: everything is
        already here (which is how the caller knows the hint can go), or what was read is
        blank once its newlines are stripped, and so shows nothing.
        """
        if not self.more():
            return None         # everything is already here; there is nothing above it (Y1)
        if self.path is None or not self.path.is_file():
            self.at = 0          # nothing to read: all here, and the hint goes for good
            return None
        try:
            st = self.path.stat()
            if self._stat is None:
                self._stat = (st.st_size, st.st_mtime)
        except OSError:
            pass
        text, start = read_tail(self.path, self.at)
        self.at = start
        if not text:
            return None
        step = (_no_header(text) if start == 0 else text).strip("\n")
        if step:
            self._chunks.insert(0, step)     # an earlier one goes above (W3: list accumulator)
        return step or None

    def more(self) -> bool:
        """Whether anything above is still unread. Nothing read yet counts as "more".

        A preview with no file behind it (the last message, or one that could not be
        read) has nothing above it, so it says nothing (review Z1).
        """
        return self.path is not None and self.at != 0

    def hint(self) -> str:
        """The line that says there is more above - gone once it is all here (T6, W13)."""
        return f"↑ 往上捲、按 k 或 g 載入更早的內容（還有約 {max(1, round((self.at or 0) / 1024))} KB）" \
            if self.more() else ""


class PreviewText(TextArea):
    """The preview pane: a read-only TextArea with cursor and syntax highlighting (design 5.9)."""

    #: Set while the app is replacing what the pane shows and scrolling it: `load_text` puts
    #: the scroll back at the top, which is not the reader reaching the top (review S2).
    _suppress_scroll_load = False
    #: The word being searched for; every match on a drawn row gets `MATCH_STYLE`.
    _query = ""
    #: Bumped whenever what is on screen changes, so a cache of match positions lets go of them.
    #: TextArea's own caches are keyed by row, scroll and selection - not by content - so there
    #: is nothing of its own to key on (review T2).
    content_version = 0

    #: Bold and underlined, not a colour: `_render_line` puts the syntax's colours on after
    #: `get_line`, and the cursor line's background on after that, and both would cover a
    #: colour or a background of ours (W6, review (4)).
    MATCH_STYLE = "bold underline"

    BINDINGS = [   # TextArea's own are inherited; only the keys this pane adds (spec「游標與捲動」)
        Binding("j", "cursor_down", "向下", show=False),
        Binding("k", "cursor_up", "向上", show=False),
        Binding("g", "top", "到最前", show=False),
        Binding("G", "bottom", "到最後", show=False),
        Binding("slash", "search", "搜尋", show=False),
        Binding("n", "search_next", "下一個", show=False),
        Binding("N", "search_prev", "上一個", show=False),
    ]

    def __init__(
        self,
        text: str = "",
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        try:
            super().__init__(
                text=text,
                read_only=True,
                soft_wrap=True,
                show_line_numbers=False,
                language="markdown",
                id=id,
                classes=classes,
            )
        except LanguageDoesNotExist:
            super().__init__(
                text=text,
                read_only=True,
                soft_wrap=True,
                show_line_numbers=False,
                language=None,
                id=id,
                classes=classes,
            )
        self.highlight_cursor_line = False

    def on_focus(self) -> None:
        self.highlight_cursor_line = True

    def on_blur(self) -> None:
        self.highlight_cursor_line = False

    def action_cursor_up(self, select: bool = False) -> None:
        if self.cursor_location[0] == 0 and self.get_cursor_up_location() == self.cursor_location:
            self.app.load_earlier(cursor_up=True)
            return
        super().action_cursor_up(select=select)

    def action_cursor_page_up(self) -> None:
        if self.cursor_location[0] == 0 and self.scroll_y == 0:
            self.app.load_earlier(cursor_page_up=True)
            return
        super().action_cursor_page_up()

    def action_top(self) -> None:
        self.app.load_all_earlier()

    def action_bottom(self) -> None:
        last_line = max(0, self.document.line_count - 1)
        self.move_cursor((last_line, 0))
        self.scroll_end(animate=False)

    def action_search(self) -> None:
        self.app.open_preview_search()

    def action_search_next(self) -> None:
        self.app.preview_search_next()

    def action_search_prev(self) -> None:
        self.app.preview_search_prev()

    def load_text(self, text: str) -> None:
        self.content_version += 1
        super().load_text(text)

    def edit(self, edit: Edit) -> EditResult:
        self.content_version += 1
        return super().edit(edit)

    def set_query(self, query: str) -> None:
        """Highlight every match of `query` from now on.

        `_line_cache` is TextArea's own, and its key does not include the highlights, so a row
        that has already been drawn would keep the old ones unless the cache goes (W6).
        """
        self._query = query
        self._line_cache.clear()
        self.refresh()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if old_value > 0 and new_value == 0 and self.is_attached and not self._suppress_scroll_load:
            self.app.load_earlier()

    def _build_highlight_map(self) -> None:
        super()._build_highlight_map()
        # Keep ## user and ## assistant headings in their distinct styles (W6, W9): their
        # own colour goes on in get_line, and it would be covered by the heading style here.
        for line_index in list(self._highlights.keys()):
            if line_index >= self.document.line_count:
                # A fenced code block ends one line past the last one, so a session whose last
                # message ends with ``` has a highlight on a line the document does not have.
                del self._highlights[line_index]
                continue
            line = self.document.get_line(line_index).strip()
            if line.startswith("## user") or line.startswith("## assistant"):
                self._highlights[line_index] = [
                    (s, e, name) for s, e, name in self._highlights[line_index]
                    if name != "heading"
                ]

    def get_line(self, line_index: int) -> Text:
        line_string = self.document.get_line(line_index)
        text = Text(line_string, end="", no_wrap=True)
        stripped = line_string.strip()
        if stripped.startswith("## user"):
            text.stylize("bold #87afff")
        elif stripped.startswith("## assistant"):
            text.stylize("bold #d787ff")
        self._stylize_search_matches(text, line_index, line_string)
        return text

    def _stylize_search_matches(self, text: Text, line_index: int, line_string: str) -> None:
        """Every match on this row is underlined and bolded - the search's own seam (spec「搜尋」).

        The same matcher as the file's side counts with, so what is marked here is what was
        counted there.
        """
        for _row, start, end in find_in_lines([line_string], self._query):
            text.stylize(self.MATCH_STYLE, start, end)


def agora_preview(paths: store.Paths, index: store.Index, agora_id: str) -> Preview:
    """This session's preview: the tail of its session.md, dir and tags pinned above."""
    ulid = agora_id.split(":", 1)[1]
    hdr = index.header(ulid) or {}
    pinned = (f"dir {(h.agora_of(hdr).get('source') or {}).get('dir') or '—'}   "
              f"tags {', '.join(map(str, hdr.get('tags') or [])) or '—'}")
    preview = Preview(paths.mirror / ulid / "session.md", pinned)
    preview.step()
    return preview


def import_preview(agent, session_id: str, full: bool = False, paths: store.Paths | None = None,
                   updated: str | None = None) -> Preview | None:
    """The last message at once; with `full`, the tail of the reading version, kept in the
    cache (5.10). Same rule as the agora side: read a step, not the whole thing (T6)."""
    from agora.agents.base import reading
    try:
        if full:
            if paths is None:                     # no cache to read: the only way is all of it
                return Preview(None, "整份對話（閱讀版）",
                               reading(agent, agent.export(session_id).raw).strip())
            path = cache.reading_path(paths, agent, session_id)
            if not path.is_file():
                cache.local_reading(paths, agent, session_id, updated)   # builds it, once
            preview = Preview(path, "整份對話（閱讀版）")
            preview.step()
            return preview
        last = agent.last_message(session_id)
    except Exception:            # a preview must never take the screen down
        # The whole conversation that could not be read is nothing to show: the caller keeps
        # what is on the screen and says so, rather than replacing it with an empty pane
        # (spec「整份讀取失敗時」, review T3).
        return None if full else Preview(None, "讀不到這個 session")
    if not last:
        return Preview(None)
    role, text = last
    return Preview(None, f"最後一則（{role}），整份對話載入中…", f"## {role}\n{text}")


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


def _is_file(where: str) -> bool:
    """Whether `where` names a file at all. Never raises.

    The first-run screen asks this before it reads, and `~someone-who-does-not-exist`
    makes `Path.expanduser` raise `RuntimeError` - which, from a screen, ends the app
    (review U1). `os.path.expanduser` leaves such a path as it is instead.
    """
    return os.path.isfile(os.path.expanduser(where))


def read_client(where: str) -> tuple[str, str] | None:
    """(client_id, client_secret) from the file at `where`, or None if it is not one.

    Two shapes: the JSON Google hands out when you create a **Desktop** client, and a
    two-line `Client-ID=` / `SECRET=` text file. Nothing is printed, logged or put on
    the screen - the values only ever travel into rclone's own argv (design D5).

    Every way this can fail is None, and nothing is said about which. That is not
    politeness: a file saved as UTF-16 raises `UnicodeDecodeError`, whose message
    carries **the whole file**, and that file is a credential (review T1). A deeply
    nested JSON raises `RecursionError`, and when the app dies Textual prints the
    locals - `text` is one of them (review U1). So:

    * the answer to any failure is None, with no reason attached, and the `except` is
      `Exception` rather than a list - this function's whole vocabulary is "a pair or
      nothing", and a class of failure we did not think of is still just a None;
    * the two values are read out of whatever we parsed, and nothing else is kept, so
      a local variable never holds a file we are not sure about.

    A `web` client is refused rather than accepted: rclone redirects it to
    `http://127.0.0.1:53682/`, which a web client will not have registered, and the
    user would only ever see 「授權沒有完成」 (review T4).
    """
    try:
        # utf-8-sig, so a BOM is not mistaken for a format we do not know (review T4)
        text = Path(os.path.expanduser(where)).read_bytes().decode("utf-8-sig")
        if text.lstrip().startswith("{"):
            block = json.loads(text).get("installed")
            client_id = block.get("client_id") if isinstance(block, dict) else None
            secret = block.get("client_secret") if isinstance(block, dict) else None
        else:
            pairs = {}
            for line in text.splitlines():
                key, _, value = line.partition("=")
                pairs[key.strip()] = value.strip()     # rclone writes `Client-ID = …`
            client_id, secret = pairs.get("Client-ID"), pairs.get("SECRET")
        if not all(isinstance(v, str) and v.strip() for v in (client_id, secret)):
            return None
        return client_id.strip(), secret.strip()
    except Exception:
        # `os.path.expanduser` raises RuntimeError for `~someone-who-does-not-exist`,
        # json.loads raises RecursionError on a very deep document, and neither is
        # worth an exception message: the message would carry the file (review U1).
        return None


def authorize_argv(paths: store.Paths, client: tuple[str, str] | None = None) -> list[str]:
    """The rclone command that writes `[gdrive]`; `client` is the user's own OAuth client.

    A list, never a string: the secret must not go through a shell, where it would end
    up in the process table and in every `ps` (design D5).
    """
    argv = [os.environ.get("AGORA_RCLONE", "rclone"), "config", "create", "gdrive", "drive",
            "scope=drive.file"]
    if client:
        argv += [f"client_id={client[0]}", f"client_secret={client[1]}"]
    return argv + ["--config", str(paths.config / "rclone.conf")]


def authorize(paths: store.Paths, say=print, client: tuple[str, str] | None = None) -> int:
    """`rclone config create`: it opens the browser; we pass its lines on.

    With rclone's own client when the user gave none (design D5), and with their own
    Desktop client when they pointed at one - same scope either way, and rclone reuses
    an existing `[gdrive]`'s token only if the client matches, so switching is a
    deliberate step rather than a silent one.

    `say` is where the lines go - the waiting window when the interactive mode runs
    it, stdout otherwise - rather than the thread printing behind the screen's back
    (review K3). Lines naming a token or a secret are dropped: rclone echoes what it
    wrote, and neither belongs on a screen.
    """
    paths.config.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(authorize_argv(paths, client), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        if not any(word in line.lower() for word in ("token", "secret")):
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
            yield Static("Enter 選擇   Esc 取消", classes="hint")

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
            # Nothing to choose between here: Enter submits what was typed (review E3)
            yield Static("Enter 確定   Esc 取消", classes="hint")

    @on(Input.Submitted)
    def done(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


#: Esc escalates SIGINT → SIGTERM → SIGKILL, a step every ESCALATE_AFTER seconds
#: (review V2, W1). SIGINT is what Ctrl-C would send and what cli.main turns into
#: exit 130 after keeping its pending record; the agent that ignores it gets
#: SIGTERM and then SIGKILL, always to the whole process group. The gap matters:
#: a process sent both at once has no time to wind up.
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
        self.done = False                  # the command exited cleanly: the bar is full
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
            self.done = code == 0
        except Exception as e:       # never leave the window up forever (review L6)
            code, self.lines = 2, self.lines + [f"讀不到輸出：{e}"]
        # The app may already be gone (ctrl+q closed it); that is not our problem.
        with contextlib.suppress(Exception):
            self.app.call_from_thread(self.finish, code)

    def finish(self, code: int) -> None:
        """Close the window - but only after a clean run has shown its full bar.

        `tick` redraws every 0.1s, so dismissing the moment the process ended usually
        closed the window before the last bar was ever drawn: `N/N` existed in the
        code and nowhere on the screen (review E2).
        """
        if self.done:
            step = next((m for m in map(self.PROGRESS.match, reversed(self.lines))
                         if m and 1 <= int(m.group(1)) <= int(m.group(2))), None)
            with contextlib.suppress(Exception):
                if step:
                    self.query_one("#bar", ProgressBar).update(
                        total=int(step.group(2)), progress=int(step.group(2)))
        self.dismiss((code, "\n".join(self.lines)))

    def tick(self) -> None:
        step = next((m for m in map(self.PROGRESS.match, reversed(self.lines))
                     if m and 1 <= int(m.group(1)) <= int(m.group(2))), None)
        if step:
            # `k/N` means the k-th one has *started*, so N-1 of them are finished;
            # reaching N/N means the command said it was done (review Q3)
            total, started = int(step.group(2)), int(step.group(1))
            self.query_one("#bar", ProgressBar).update(
                total=total, progress=total if self.done else max(0, started - 1))
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


class Confirm(ModalScreen):
    """Cancel or confirm, with one extra option that is off unless asked for.

    The extra option is the one whose default is "do nothing about it": deleting
    the local copy of a session Drive no longer has, or putting that session back.
    Both are decisions, so neither is pre-ticked (spec 3.2). Dismisses
    `(index, extra)`, or None on Esc.

    Tab moves between the buttons and the checkbox *here* and nowhere else: the app
    below binds Tab to changing tab, and a priority binding on the screen wins over
    it - without that, reaching the checkbox with the keyboard switched the page
    underneath and left nothing to tick (review S1).
    """

    BINDINGS = [Binding("escape", "dismiss((None, False))", "取消"),
                Binding("enter", "confirm", "確定", priority=True),
                Binding("space", "toggle", "勾選", priority=True)]

    def __init__(self, title: str, options: list[str], note: str = "", extra: str = "",
                 note_on: str = ""):
        super().__init__()
        self.title_, self.options, self.note, self.extra = title, options, note, extra
        self.note_on = note_on
        self.picked = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="box"):
            yield Static(self.title_, classes="box-title")
            yield OptionList(*self.options)
            if self.extra:
                # [ ] / [x] in the label: a tick that only changes colour is a tick
                # the user cannot read (review Q1)
                yield Checkbox(f"[ ] {self.extra}", value=False, id="extra")
            if self.note:
                yield Static(self.note, classes="note", id="effect")
            yield Static("空白 勾選   Enter 選擇   Esc 取消", classes="hint")

    @on(Checkbox.Changed, "#extra")
    def ticked(self, event: Checkbox.Changed) -> None:
        """Ticking shows `[x]`, and the line under it says what will happen - which
        is the whole reason to tick it (review Q1)."""
        self.picked = event.value
        box = self.query_one("#extra", Checkbox)
        box.label = Text(f"[{'x' if self.picked else ' '}] {self.extra}")
        if self.note_on:
            self.query_one("#effect", Static).update(self.note_on if self.picked else self.note)

    def action_toggle(self) -> None:
        """Space on the checkbox ticks it; on the list there is nothing to tick here."""
        box = self.query_one("#extra", Checkbox)
        if self.focused is box:
            box.toggle()

    def action_focus_next(self) -> None:
        """Tab and shift+tab move between the buttons and the checkbox (review S1)."""
        box = self.query_one("#extra", Checkbox)
        self.query_one("OptionList" if self.focused is box else "#extra").focus()

    def action_focus_previous(self) -> None:
        self.action_focus_next()      # two things to focus: the other one is the point

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss((event.option_index, self.picked))

    def action_confirm(self) -> None:
        """Enter confirms with whatever is ticked; it never ticks (review U1).

        A checkbox that toggles on Enter makes the dangerous direction one keystroke
        away: the box means deleting a local copy or putting a session back, and
        Enter is the key people press to accept what they were shown.
        """
        options = self.query_one("OptionList")
        self.dismiss((options.highlighted if options.highlighted is not None else 0, self.picked))


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
    #preview_pane { width: 1fr; background: #303030; padding: 0 1; }
    #right { width: 100%; height: 1fr; background: transparent; border: none; padding: 0; }
    #main.narrow #left { width: 100%; height: 50%; }
    #main.narrow #preview_pane { width: 100%; height: 1fr; }
    #left:focus-within, #right:focus, #preview_pane:focus-within { background: #000000; }
    DataTable { background: transparent; }
    DataTable > .datatable--cursor { background: #3a3a3a; text-style: bold; }
    DataTable > .datatable--header { background: transparent; color: #8a8a8a; text-style: bold; }
    #pinned, #hint { color: #8a8a8a; height: auto; }
    #filterbar { height: 1; display: none; }
    #filterbar.on { display: block; }
    #mode { width: auto; padding: 0 1; background: #5f0000; }
    #filter { border: none; height: 1; padding: 0; background: #1c1c1c; }
    #searchbar { height: 1; display: none; }
    #searchbar.on { display: block; }
    #search { border: none; width: 1fr; height: 1; padding: 0; background: #1c1c1c; }
    #count { width: auto; height: 1; padding: 0 1; color: #ffd75f; background: #1c1c1c; }
    #msg { height: 1; color: #ffd75f; }
    #keys { height: 1; background: #1c1c1c; }
    #msg.failed { color: #ff5f5f; }
    .box { width: 70; height: auto; max-height: 80%; padding: 0 1; border: round #00afaf; background: #1c1c1c; }
    .box.failed { border: round #ff5f5f; }
    Choose, AskText, Busy, Run, Tell { align: center middle; }
    .box-title { text-style: bold; color: #00d7d7; }
    .note, .hint { color: #8a8a8a; }
    OptionList { height: auto; max-height: 8; background: transparent; border: none; }
    """
    BINDINGS = [
        Binding("tab", "tab", "切焦點", priority=True),
        Binding("shift+tab", "shift_tab", "切焦點", priority=True),
        Binding("[", "prev_tab", "換頁"),
        Binding("]", "next_tab", "換頁"),
        Binding("space", "mark", "勾選"),
        Binding("a", "mark_all", "全選／全不選"),
        Binding("enter", "primary", "接續／匯入", priority=True),   # the table would take it for itself
        Binding("m", "merge", "合併"),
        Binding("e", "edit", "改標頭"),
        Binding("d", "delete", "刪除"),
        Binding("slash", "filter", "篩選"),
        Binding("ctrl+t", "search_mode", "標題／內文"),
        Binding("p", "pull", "pull"),
        Binding("P", "push", "push"),
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
        self._filter_timer = None      # the debounce for the content search (T7 P1)
        self._pending_filter = ""
        self._scanning: str | None = None   # the word being scanned, if one is (T7 Q1)
        self._scan_wanted = ""              # and the newest one waiting for it
        self.marked: set[str] = set()
        self.cache: dict[str, Preview] = {}
        self.status = ""
        self._stopped = False        # an Esc went through, whatever the exit code says
        self._timer = None                     # the pending "the cursor came to rest" timer
        self._groups: set[int] = set()   # process groups an action started
        # The search in the preview (spec「預覽區搜尋」): the word, how many matches the whole
        # session has, which one the cursor is on, and the row it belongs to.
        self.query, self.total, self.which = "", 0, 0
        self.search_key: str | None = None
        self.search_open = False          # the box under the pane is showing
        self.note, self.pending_note = "", ""   # 「已換成整份對話」, 「內容已更新」
        self.last_only = False
        self.shown_preview: Preview | None = None
        self._matches: list[tuple[int, int, int]] = []   # the matches on screen, remembered
        self._matches_key: tuple | None = None           # by the word and by what is on screen

    def compose(self) -> ComposeResult:
        yield Static(id="bar")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield DataTable(id="table", cursor_type="row", zebra_stripes=False,
                                cursor_foreground_priority="renderable")   # the red bar and agent colours stay
            with Vertical(id="preview_pane"):
                yield Static(id="pinned")
                yield Static(id="hint")
                yield PreviewText(id="right")
                with Horizontal(id="searchbar"):
                    yield Input(id="search", placeholder="搜尋對話內文；Enter 送出；Esc 關掉")
                    yield Static(id="count")
        yield Static(id="keys")
        with Horizontal(id="filterbar"):
            yield Static("標題", id="mode")
            yield Input(id="filter", placeholder="邊打邊篩；Esc 清掉；ctrl+t 切換標題／內文")
        yield Static(id="msg")

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
            self.say("Agora 還沒有 Session：按 ] 到「未匯入」，空白鍵勾選後按 Enter 匯入")
        self.query_one("#table").focus()

    async def setup(self) -> bool:
        """Guide a first run: rclone missing, or Drive not authorized yet (design 5.9)."""
        need = setup_needed(self.paths)
        if need == "rclone":
            await self.push_screen_wait(Choose("需要 rclone", ["離開"], "請先在終端機執行：brew install rclone\n裝好之後再打 agora"))
            return False
        if need == "auth":
            note = ("agora 把 Session 存在你的 Google Drive。權限只有 drive.file：只看得到 agora 自己建的檔案。\n"
                    "用 rclone 內建的 client 就能用；有自己的 OAuth client（Google Cloud 的 Desktop "
                    "client）會快很多，選第二項可以給一個設定檔的路徑。")
            pick = await self.push_screen_wait(Choose("還沒設定 Google Drive", [
                "用瀏覽器授權（rclone 內建的 client）", "用自己的 OAuth client（選填）", "離開"], note))
            if pick is None or pick == 2:
                return False
            client = None
            if pick == 1:
                # The path is all we ask for: the values are read here and handed to
                # rclone, never shown (design D5).
                leave = False
                while True:
                    typed = (await self.push_screen_wait(
                        AskText("自己的 client 設定檔路徑", "")) or "").strip()
                    if not typed:
                        break                        # 沒給就用內建的，和以前一樣
                    if not _is_file(typed):
                        found, why = None, "找不到這個檔案。"
                    else:
                        found = read_client(typed)
                        why = ("這個檔案裡沒有 client_id／client_secret。要用 Google Cloud 的"
                               " **Desktop** client 下載的 JSON，"
                               "或兩行 Client-ID=／SECRET= 的文字檔。")
                    if found:
                        client = found
                        break
                    # 換 client 要搬家，所以打錯一個字值得再問一次（review T4）
                    again = await self.push_screen_wait(Choose(
                        "讀不到 client 設定檔", ["重新輸入路徑", "用內建的 client", "離開"],
                        f"{typed}\n{why}\n用內建的 client 一樣能用，只是共用配額會慢。"))
                    if again != 0:
                        leave = again == 2
                        break
                if leave:
                    return False
            code, out, error = await self.push_screen_wait(
                Busy("請在瀏覽器完成授權", lambda say: authorize(self.paths, say, client)))
            if error or code != 0 or setup_needed(self.paths):
                await self.push_screen_wait(Tell("授權沒有完成", f"{out}\n{error or ''}", ok=False))
                return False
        return True

    def reload(self, keep_marked: bool = False) -> None:
        self.index = store.Index(self.paths)       # local only; no full sync after every action (T6)
        self.rows = {"agora": agora_rows(self.index, [], self.paths),
                     "import": import_rows(self.index, self.agents, self.paths)}
        if not keep_marked:
            self.marked.clear()
        else:
            # Only rows that are gone go; a mark on either tab survives (review M1)
            self.marked &= {r.key for tab in TABS for r in self.rows[tab]}
        if self.query:
            self.pending_note = "內容已更新"     # put_preview searches again with it (R5)
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
        self.track_row()                    # the rows are new: another row, another session
        self.paint_bar()
        self.paint_keys()
        self.gutter()
        self.preview()

    def tick(self, row: Row) -> Text:
        return Text("✓", style="bold #ffd75f") if row.key in self.marked else Text(" ")

    def current(self) -> Row | None:
        rows, table = self.shown(), self.query_one("#table", DataTable)
        return rows[table.cursor_row] if rows and 0 <= table.cursor_row < len(rows) else None

    def paint_keys(self) -> None:
        """The keys that work here, per (tab, side) (spec「按鍵列」, W8)."""
        side = self.side()
        items = (KEYS["list"][self.tab] if side == "list"
                 else KEYS["search"] if self.search_open else KEYS["preview"])
        bar = Text(" ")
        for key, what in items:
            bar.append(f" {key} ", style="bold #000000 on #00afaf")
            bar.append(f" {what}  ", style="#bcbcbc")
        self.query_one("#keys", Static).update(bar)

    def on_descendant_focus(self, event) -> None:
        if self.screen is self.default_screen:
            self.paint_keys()

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

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()          # a pending preview must not fire into a dead app

    def preview(self) -> None:
        """Read the tail for the row under the cursor - and only once it has come to rest.

        Moving up and down a list must not read or lay out anything per keystroke: a
        whole session.md is up to 3 MB and rendering all of it took 0.72 s (T6).
        """
        if self._timer is not None:
            self._timer.stop()                 # a moving cursor reads nothing (T6)
        self._timer = self.set_timer(PREVIEW_IDLE, self.settled)

    def settled(self) -> None:
        try:
            self.query_one("#table", DataTable)
        except Exception:
            return                   # the app is going away; a preview is not worth a crash
        row = self.current()
        if row is None:
            self.put_preview(Preview(None))
        elif row.key in self.cache and not self.cache[row.key].is_stale():
            self.put_preview(self.cache[row.key])
        elif self.tab == "agora":
            self.cache[row.key] = agora_preview(self.paths, self.index, row.key)
            self.put_preview(self.cache[row.key])
        else:                    # the last message at once, the reading version when it is read
            agent = next((a for a in self.agents if a.name == row.agent), None)
            if agent:
                self.put_preview(import_preview(agent, row.key.split(":", 1)[1]))
                self.load_full(agent, row.key, row.updated)

    @work(thread=True, exclusive=True, group="preview")
    def load_full(self, agent, key: str, updated: str | None) -> None:
        try:
            result = import_preview(agent, key.split(":", 1)[1], True, self.paths, updated)
        except Exception:                # whatever happens, the screen must not stay 「載入中」
            result = None
        self.call_from_thread(self.loaded, key, result)

    def loaded(self, key: str, result: Preview | None) -> None:
        """The reading version is ready, or it could not be read.

        A failure says so on the line above the pane and changes nothing else: the last message
        the reader was reading stays, and nothing is cached, so selecting the row again tries the
        read again (spec「整份讀取失敗時」, review T3).
        """
        row = self.current()
        if result is None:
            if row and row.key == key:            # the reader may have moved on since (review R1)
                self.query_one("#pinned", Static).update("讀不到整份對話")
            return
        self.cache[key] = result
        if row and row.key == key:
            self.pending_note = "已換成整份對話"
            self.put_preview(result)

    def load_earlier(self, cursor_up: bool = False, cursor_page_up: bool = False) -> None:
        """At the top of the pane: one more step above, and stay on the line we were on."""
        row = self.current()
        preview = self.cache.get(row.key) if row else None
        if preview is None or not preview.more():
            return
        if preview.is_stale():
            if self.tab == "agora":
                preview = agora_preview(self.paths, self.index, row.key)
            else:
                agent = next((a for a in self.agents if a.name == row.agent), None)
                if agent:
                    preview = import_preview(agent, row.key.split(":", 1)[1], True, self.paths, row.updated)
            if preview:
                self.cache[row.key] = preview
                self.put_preview(preview)
            return

        step = preview.step()
        if step is None:
            return
        pane = self.query_one("#right", PreviewText)
        keep_scroll_y = pane.scroll_y
        was_height = pane.wrapped_document.height
        pane.insert(f"{step}\n", location=(0, 0), maintain_selection_offset=True)
        pane.history.clear()
        self.query_one("#hint", Static).update(preview.hint())

        def restore() -> None:
            delta = max(0, pane.wrapped_document.height - was_height)
            if cursor_up or cursor_page_up:
                # Up moves by a row on the screen, not a line in the file (spec「游標與捲動」):
                # the row above the cursor's may be the last row of a line that wraps (review S3).
                rows = 1 if cursor_up else max(1, pane.content_size.height)
                x, y = pane.wrapped_document.location_to_offset(pane.cursor_location)
                above = pane.wrapped_document.offset_to_location(Offset(x, max(0, y - rows)))
                pane.move_cursor(above)
                pane.scroll_to(y=max(0, keep_scroll_y + delta - rows), animate=False)
            else:
                pane.scroll_to(y=keep_scroll_y + delta, animate=False)

        self.call_after_refresh(restore)

    def load_all_earlier(self) -> None:
        """Load all earlier chunks and place cursor at the very top (spec「到最前面」, g)."""
        row = self.current()
        preview = self.cache.get(row.key) if row else None
        if preview is None:
            return
        pane = self.query_one("#right", PreviewText)
        if preview.more():
            while preview.more():
                preview.step()
            self.show_read_text(pane, preview)
        pane.move_cursor((0, 0))

        def settle() -> None:
            pane.scroll_to(y=0, animate=False)

        self.call_after_refresh(settle)

    def show_read_text(self, pane: PreviewText, preview: Preview) -> None:
        """Everything read so far goes into the pane in one go (W3).

        The scroll that comes with it is the app's, not the reader's, so it must not be read as
        reaching the top of the pane (review S2) - hence the flag, let go after the refresh.
        """
        pane._suppress_scroll_load = True
        pane.load_text(preview.text)
        self.query_one("#hint", Static).update(preview.hint())

        def release() -> None:
            pane._suppress_scroll_load = False

        self.call_after_refresh(release)

    def put_preview(self, preview: Preview) -> None:
        """What the pane shows for the row under the cursor, and what happens to the search.

        Another session starts clean. The same session with its content replaced is searched
        again with the same word - the matches have moved - and says which change it was
        (spec「搜尋狀態的生命週期」, review R5).
        """
        row = self.current()
        key = row.key if row else None
        replaced = preview is not self.shown_preview
        self.shown_preview = preview
        if self.query and key != self.search_key:
            self.clear_preview_search()
        note = self.pending_note if self.query else ""
        self.pending_note = ""
        self.query_one("#pinned", Static).update(preview.pinned)
        self.query_one("#hint", Static).update(preview.hint())
        pane = self.query_one("#right", PreviewText)
        pane._suppress_scroll_load = True
        pane.load_text(preview.text)
        last_line = max(0, pane.document.line_count - 1)
        pane.move_cursor((last_line, 0))

        def settle() -> None:
            pane.scroll_end(animate=False)
            pane._suppress_scroll_load = False
            if self.query and replaced:
                # Counted and marked again, but nothing is jumped to and nothing is read: the
                # reader did not ask for the front of the file, and the cursor goes back to the
                # end where the new content starts (spec R5, review T1).
                self.run_preview_search(self.query, note=note, jump=False)

        self.call_after_refresh(settle)

    def track_row(self) -> None:
        """The search belongs to the row it was made on; any other row - or none at all, which
        is what another tab with no rows of its own looks like - starts clean (spec「搜尋狀態
        的生命週期」). Called whenever the row under the cursor may have changed."""
        row = self.current()
        key = row.key if row else None
        if key != self.search_key:
            self.search_key = key
            if self.query:
                self.clear_preview_search()

    # -- the search in the preview (spec「預覽區搜尋」) -----------------------------

    def open_preview_search(self) -> None:
        """`/` on the preview side: the box under the pane, with the focus in it."""
        self.search_open = True
        self.query_one("#search", Input).value = ""
        self.query_one("#searchbar").add_class("on")
        self.query_one("#search").focus()

    def hide_preview_search(self) -> None:
        """The box goes, and anything typed in it and not sent goes with it (design「搜尋框與狀態」)."""
        self.search_open = False
        self.query_one("#search", Input).value = ""
        self.paint_count()

    def close_preview_search(self) -> None:
        """Enter or Esc in the box: it goes and the pane takes the focus back."""
        self.hide_preview_search()
        self.query_one("#right").focus()

    def clear_preview_search(self) -> None:
        """No word, no highlight, no count - and the cursor stays where it is."""
        self.query, self.total, self.which, self.note, self.last_only = "", 0, 0, "", False
        pane = self.query_one("#right", PreviewText)
        pane.set_query("")
        pane.selection = Selection.cursor(pane.cursor_location)
        self.paint_count()

    def paint_count(self) -> None:
        """The line under the pane: which match, how many, and what is being looked at."""
        if not self.query:
            text = ""
        elif not self.total:
            text = "找不到"
        elif not self.which:
            text = f"共 {self.total} 個"       # nowhere to put the cursor: do not claim one
        else:
            text = f"第 {self.which} 個／共 {self.total} 個"
        if text and self.last_only:
            text += "（只有最後一則）"
        if text and self.note:
            text += f"  {self.note}"
        self.query_one("#count", Static).update(text)
        self.query_one("#searchbar").set_class(self.search_open or bool(text), "on")

    @on(Input.Submitted, "#search")
    def preview_search_typed(self, event: Input.Submitted) -> None:
        """Enter in the box: search, close it, and give the focus back to the pane."""
        query = event.value.strip()
        self.close_preview_search()
        if query:
            self.run_preview_search(query)
        else:
            self.clear_preview_search()

    def preview_search_next(self) -> None:
        if self.query:
            self.run_preview_search(self.query, step=1)

    def preview_search_prev(self) -> None:
        if self.query:
            self.run_preview_search(self.query, step=-1)

    @staticmethod
    def _match_from(loaded: list[tuple[int, int, int]], here, down: bool) -> int | None:
        """Which of the matches on screen the cursor is at or after (`down`), or the one before it.

        The cursor sits at the end of the match it is on, so that match is not the next one.
        """
        if down:
            return next((i for i, m in enumerate(loaded) if (m[0], m[1]) >= here), None)
        return next((i for i in range(len(loaded) - 1, -1, -1)
                     if (loaded[i][0], loaded[i][1]) < here), None)

    def loaded_matches(self, pane: PreviewText, query: str) -> list[tuple[int, int, int]]:
        """Every match in what is on screen, in order - remembered for the word and for what the
        pane is showing, because `n` asks again on every press and walking 100,000 lines is not
        free (review T2)."""
        key = (query, pane.content_version)
        if self._matches_key != key:
            self._matches = list(find_in_lines(pane.document.lines, query))
            self._matches_key = key
        return self._matches

    def _load_to(self, pane: PreviewText, preview: Preview, query: str, target: int,
                  above: int, loaded: list) -> tuple[int, list]:
        """Read down until the `target`-th match is on screen: how many are left above, and the
        matches on screen afterwards.

        The steps are read without touching the screen and go in all at once (W3): a step at a
        time re-computes the whole syntax tree for every one of them, which is 15-30 s on 3 MB.
        Nothing is read when the match is already here, and then nothing is put in the pane
        either - `n` at the end of a big document must not reload it to stay where it was.
        """
        read = False
        while preview.more() and above >= target:
            step = preview.step()
            if step is None:
                break
            read = True
            above -= count_in(step, query)     # the step's own matches are the ones that came down
        if read:
            preview.count_loaded(query, above)     # no need to read the file to know this
            self.show_read_text(pane, preview)
            loaded = self.loaded_matches(pane, query)
        return above, loaded

    def run_preview_search(self, query: str, step: int = 0, note: str = "", jump: bool = True) -> None:
        """Find `query` in the whole session and put the cursor on one of its matches.

        `step` is 0 for the first search - the match nearest the cursor, down first and then up,
        and never round to the front of the file, which is where the cursor is furthest from and
        what the reader almost never wants (review R2) - and +1 for `n`, -1 for `N`, which do
        wrap at the ends. With `jump=False` the word is counted and marked and nothing else: the
        automatic search after the content was replaced leaves the cursor where it is and reads
        nothing (spec R5, review T1).

        Nothing is carried from the file to the screen as an offset: the file says how many
        matches there are and how many are above what is read, the screen says where its own
        are, and the k-th of the session is the (k - above)-th on screen (design「搜尋：計數」).
        """
        row = self.current()
        # The cache has a session read from its file; the import tab's last message is shown but
        # not cached - moving off the row and back reads the agent again - so fall back to
        # whatever was last put on the screen.
        preview = (self.cache.get(row.key) if row else None) or self.shown_preview
        pane = self.query_one("#right", PreviewText)
        if preview is None:
            self.clear_preview_search()
            return
        self.query, self.note = query, note
        pane.set_query(query)
        total, above = preview.counts(query)
        self.total, self.last_only = total, preview.last_only
        if not total:
            self.which = 0
            pane.selection = Selection.cursor(pane.cursor_location)   # nothing is the current one
            self.paint_count()
            return
        if not jump:
            self.which = 0                     # counted and marked; the cursor is left alone
            self.paint_count()
            return
        loaded = self.loaded_matches(pane, query)
        here = pane.cursor_location                     # where the cursor is: the end of the match
        before = pane.selection.start if not pane.selection.is_empty else here
        # How many matches are before the one the cursor is on, and before the cursor itself -
        # which is the same thing unless the cursor is inside a match.
        passed = above + sum(1 for m in loaded if (m[0], m[1]) < before)
        on = above + sum(1 for m in loaded if (m[0], m[1]) < here)
        index = self._match_from(loaded, here, down=True) if step >= 0 else None
        if index is None and step <= 0:       # up means before the match the cursor is on
            index = self._match_from(loaded, before, down=False)
        if index is None:
            # None that way on the screen: the one that way is either still above, and has to be
            # read, or there is none and it is the far end to wrap to.
            if step == 0:
                target = max(passed, 1)
            elif step > 0:
                target = on + 1 if on < total else 1
            else:
                target = passed or total
            above, loaded = self._load_to(pane, preview, query, target, above, loaded)
            index = target - above - 1
        if not 0 <= index < len(loaded):
            self.which = 0
            self.paint_count()
            return
        where, start, end = loaded[index]
        self.which = above + index + 1
        pane.selection = Selection(start=(where, start), end=(where, end))
        self.paint_count()

    @on(DataTable.RowHighlighted)
    def moved(self) -> None:
        self.gutter()
        self.track_row()
        self.preview()

    def on_resize(self, event) -> None:
        self.query_one("#main").set_class(event.size.width < WIDE, "narrow")

    # -- keys --------------------------------------------------------------------

    def side(self) -> str | None:
        """The side with focus: "list" for #table and #filter; "preview" for #right and #search."""
        f = self.focused
        while f is not None:
            fid = getattr(f, "id", None)
            if fid in ("table", "filter"):
                return "list"
            if fid in ("right", "search"):
                return "preview"
            f = getattr(f, "parent", None)
        return None

    def check_action(self, action: str, parameters) -> bool | None:
        """Show only the keys that work here (the key bar follows the tab and the focus)."""
        side = self.side()
        if action == "primary":          # a priority key: only for the table, or Enter in a window or input breaks
            return side == "list" and getattr(self.focused, "id", None) == "table"
        if action in ("merge", "edit", "delete", "push"):
            return side == "list" and self.tab == "agora"
        if action in ("mark", "mark_all", "filter", "pull", "search_mode", "next_tab", "prev_tab"):
            return side == "list"
        return True

    def action_tab(self) -> None:
        """Tab moves between list and preview, or forwards in Confirm (spec「按鍵」, W4)."""
        if isinstance(self.screen, Confirm):
            self.screen.action_focus_next()
            return
        if self.screen is not self.default_screen:
            return
        self.toggle_focus()

    def action_shift_tab(self) -> None:
        """shift+tab moves between list and preview, or backwards in Confirm (W4)."""
        if isinstance(self.screen, Confirm):
            self.screen.action_focus_previous()
            return
        if self.screen is not self.default_screen:
            return
        self.toggle_focus()

    def toggle_focus(self) -> None:
        if getattr(self.focused, "id", None) == "filter":
            text = self.query_one("#filter", Input).value.strip()
            if self._filter_timer is not None:
                self._filter_timer.stop()
                self._filter_timer = None
            self.query_one("#filterbar").remove_class("on")
            if text != self.text:
                self.search(text)
        if getattr(self.focused, "id", None) == "search":
            self.hide_preview_search()
        target = "#table" if self.side() == "preview" else "#right"
        self.query_one(target).focus()

    def _change_tab(self, step: int) -> None:
        """[ and ] change page on the list side (spec「按鍵」)."""
        if self.screen is not self.default_screen or self.side() != "list":
            return
        self.tab = TABS[(TABS.index(self.tab) + step) % len(TABS)]
        self.say("")
        self.show()
        self.refresh_bindings()

    def action_next_tab(self) -> None:
        self._change_tab(1)

    def action_prev_tab(self) -> None:
        self._change_tab(-1)

    def action_mark(self) -> None:
        row = self.current()
        if row:
            self.marked.symmetric_difference_update({row.key})
            table = self.query_one("#table", DataTable)
            table.update_cell(row.key, "mark", self.tick(row))   # the cursor stays put (user's call)

    def action_filter(self) -> None:
        self.query_one("#filterbar").add_class("on")
        self.query_one("#filter", Input).focus()

    @on(Input.Changed, "#filter")
    def filter_typed(self, event: Input.Changed) -> None:
        """Filter on every keystroke, so Enter is never needed to see the result.

        An input method (macOS Chinese, Japanese, Korean) keeps the composing text to
        itself and takes Enter for the candidate list; the app only sees the committed
        text. With "type it, then press Enter", a Chinese word could therefore be typed
        and then never applied - the key that would have applied it never arrived
        (T7 F3). Filtering as the text changes means the IME's Enter only ends the
        composition, and the rows are already narrowing while the characters are picked.

        `event.value` is what is committed so far, so a half-composed character is not
        filtered on; the next keystroke re-filters anyway.

        In the title mode this is all there is to it: the rows are in memory. In the
        content mode a search also reads the agents' own stores, so it waits for the
        typing to stop (`FILTER_IDLE`) - otherwise every keystroke of a word sets a
        whole scan going, and the earlier ones do not stop when a new one starts
        (review T7 P1).
        """
        text = event.value.strip()
        if not self.content:
            self.search(text)          # in memory: filter as fast as the keys arrive
            return
        self._pending_filter = text    # reading the agents' stores: wait for a pause
        if self._filter_timer is not None:
            self._filter_timer.stop()
        self._filter_timer = self.set_timer(FILTER_IDLE, self.run_pending_filter)

    def run_pending_filter(self) -> None:
        self._filter_timer = None
        self.search(self._pending_filter)

    def action_search_mode(self) -> None:
        self.content = not self.content
        self.query_one("#mode", Static).update("內文" if self.content else "標題")
        if self.text:
            self.search(self.text)

    @on(Input.Submitted, "#filter")
    def filter_done(self, event: Input.Submitted) -> None:
        # The filter is already applied (filter_typed); Enter only means "done - back
        # to the table". With an IME it usually does not get here at all, which is why
        # nothing depends on it any more (T7 F3). It must not search again either: in
        # the content mode that was one more whole scan on top of the one the keystroke
        # had started (review T7 P1).
        text = event.value.strip()
        if self._filter_timer is not None:
            self._filter_timer.stop()
            self._filter_timer = None
        self.query_one("#filterbar").remove_class("on")
        self.query_one("#table").focus()
        if text != self.text:          # the keystroke before this one already searched
            self.search(text)

    def on_key(self, event) -> None:
        if event.key != "escape":
            return
        if self.query_one("#filterbar").has_class("on") and self.side() == "list":
            if self._filter_timer is not None:
                self._filter_timer.stop()
                self._filter_timer = None
            self._pending_filter = ""
            self.query_one("#filter", Input).value = ""
            self.query_one("#filterbar").remove_class("on")
            self.query_one("#table").focus()
            self.search("")
        elif self.search_open:
            self.close_preview_search()       # Esc in the box: the box goes (spec「預覽區搜尋」)
        elif self.side() == "preview" and self.query:
            self.clear_preview_search()       # and then the highlights, the cursor stays put

    def search(self, text: str) -> None:
        self.text = text
        self.matches = {"agora": None, "import": None}
        if text and self.content:
            # Agora: its full-text index, at once. Import: each adapter's search, rows coming in as found (feedback 9).
            self.matches["agora"] = {f"agora:{u}" for u, _h, _s in self.index.search([((h.TEXT_KEY,), "~=", text)])}
            self.matches["import"] = set()
            self.say(f"內文搜尋「{text}」中…")
            self.scan_agents(text)
        self.show()

    def scan_agents(self, text: str) -> None:
        """One agent scan at a time; the newest word waits for the running one.

        A scan that finds nothing never comes back to the worker, so it cannot notice
        it was replaced - with an input method the user pauses after each committed
        character, and every pause started another whole scan beside the last (review
        T7 Q1). Instead of starting one per pause: remember the newest word, and when
        the scan that is running returns, start it only if the word moved on.
        """
        self._scan_wanted = text
        if self._scanning is None:
            self._begin_scan(text)

    def _begin_scan(self, text: str) -> None:
        self._scanning = text
        self.find_in_agents(text)

    def scan_finished(self, text: str) -> None:
        """The worker's last word: it is not scanning any more, and if the filter has
        moved on, the newest word goes now."""
        self._scanning = None
        if self._scan_wanted and self._scan_wanted != text:
            self._begin_scan(self._scan_wanted)

    @work(thread=True, exclusive=True, group="search")
    def find_in_agents(self, text: str) -> None:
        """Scan the agents for a content word, in a thread, and stop when replaced.

        `exclusive=True` only marks the older workers cancelled - a thread that never
        looks is a thread that runs to the end, and a word typed one letter at a time
        left one whole scan per letter going at once (review T7 P1). So this checks
        between results, and between agents: the work that is still running when the
        screen has moved on stops as soon as it notices.
        """
        worker = get_current_worker()
        for agent in self.agents:   # the cache first (fast), then only what is not cached or is stale
            if worker.is_cancelled:
                return
            rows = [r for r in self.rows["import"] if r.agent == agent.name]
            missing = {r.key.split(":", 1)[1] for r in rows
                       if not cache.is_fresh(self.paths, agent.name, r.key.split(":", 1)[1], r.updated)}
            seen: set[str] = set()
            try:
                later = agent.search_text(text, only=missing) if missing else ()
                for source in (cache.search_cached(self.paths, agent.name, text), later):
                    for session_id in source:
                        if worker.is_cancelled:
                            return
                        if session_id not in seen:
                            seen.add(session_id)
                            self.call_from_thread(self.found, text, f"{agent.name}:{session_id}")
            except Exception:    # a search must never take the screen down
                continue
        if not worker.is_cancelled:
            self.call_from_thread(self.say, f"內文搜尋「{text}」完成")
        with contextlib.suppress(Exception):     # the app may be gone
            self.call_from_thread(self.scan_finished, text)

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

    async def act(self, title: str, argv: list[str], sent: list[str] | None = None) -> bool:
        """One command in a child process, under a window with its progress and Esc.

        Says whether it was stopped, so a caller with more than one command does not
        start the next one after an interruption (review M2).

        `sent` is the rows this command acted on, and only their marks are cleared
        when it succeeded (spec「成功後清掉勾選」, review M1). Everything else stays:
        the rows of a segment that failed, the rows on the other tab, and the rows
        the filter hid - none of them took part in this command, so none of them
        should quietly lose their mark.

        Re-running the same action carries on from where it stopped (design 5.11):
        the command mode is the one that knows how to skip what is already done.
        """
        self._stopped = False
        code, out = await self.push_screen_wait(Run(title, argv, self.spawn))
        # Esc wins over the exit code (review L2): the child may have finished in the
        # same instant, and a -9 from the OOM killer is not an interruption. What we
        # know is that we sent SIGINT to a group that was still running.
        stopped = bool(self._stopped or code == 130)
        action = argv[0] if argv else ""
        if stopped:
            note = "已中斷；重跑同一個動作會接著做"
        elif code == 0:
            if action == "delete":
                # Rows that were all cloud-lost never enter the queue, so there is no
                # Drive half to promise - the command says so, and we repeat it (W5)
                note = ("已從本機刪除，背景移到 Drive 垃圾桶" if "背景移到 Drive 垃圾桶" in out
                        else "已從本機刪除")
            elif action in ("import", "merge"):
                note = "已經存在本機，背景上傳中"
            else:
                note = "完成"
        elif code == 3 and action == "delete":
            # G4: delete stores nothing in the outbox; what waits is the Drive half, or
            # (nothing queued) the uploads that were already there - the command says which
            note = ("已從本機刪除；移到 Drive 垃圾桶要等之後的指令" if "移到 Drive 垃圾桶要等" in out
                    else "已從本機刪除；背景上傳啟動失敗，outbox 要等之後的指令")
        elif code == 3:
            note = "已存進 outbox，之後的指令會自動再送"
        else:
            note = "沒有全部成功，訊息在下面"
        await self.push_screen_wait(Tell(f"{title}{'（已中斷）' if stopped else ''}",
                                         f"{note}\n\n{out}", code == 0))
        self.reload(keep_marked=True)
        if code == 0 and sent:
            self.marked -= set(sent)
            self.show()
        # The note is in the window the user just read; leaving it on the status
        # line too made the next screen look like it was still about that action
        # (review Q4).
        self.say("", failed=code != 0)
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
            for step, sig in enumerate(ESCALATION, 1):
                # step n waits n * ESCALATE_AFTER, so each signal gets its own
                # window and the agent can wind up between them (review W1)
                self.set_timer(ESCALATE_AFTER * step, lambda sig=sig: self._step(pgid, sig))

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
            self._nothing_to_do("先選要處理的 Session")
            return
        if self.tab == "import":
            # One command per agent: they are separate stores, and each of them gets
            # its own k/N progress in the window (V6).
            for agent in dict.fromkeys(r.agent for r in rows):
                mine = [r for r in rows if r.agent == agent]
                if await self.act(f"匯入 {len(mine)} 個（{agent}）",
                                  argv_for("import", mine, None, None)[0],
                                  sent=[r.key for r in mine]):
                    break      # Esc means stop this action, not half of it (review M2)
            return
        row = self.current()
        if await self._refuse(row, "接續"):
            return
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
        if self.tab != "agora":
            self.say("合併是 Agora 頁的動作；未匯入的用 Enter 匯入")
            return
        rows = self.chosen_rows()          # the visible marked ones, in screen order
        if len(rows) < 2:
            self.say("合併要先用空白鍵勾選至少兩個", failed=True)
            return
        pick = await self.push_screen_wait(Choose(f"合併 {len(rows)} 個：由誰寫要約？", ["opencode", "claude"],
                                                  "每個來源叫一次 AI；內容會送到那個 agent 的模型供應商"))
        if pick is not None:
            await self.act("合併", argv_for("merge", rows, ("opencode", "claude")[pick], None)[0],
                           sent=[r.key for r in rows])

    def _nothing_to_do(self, what: str) -> None:
        """No rows to send: say so in the status line and start nothing (review Q1).

        A command with no ids is not a harmless no-op - `push session` with none
        would be a command that cannot do what it says, and it would still have
        been a process to wait for.
        """
        self.say(f"{what}（空白鍵勾選，游標那一列也可以）", failed=True)

    def action_mark_all(self) -> None:
        """`a`: mark every row on screen, or unmark them if they all are.

        Only the visible ones: a row the filter hides keeps whatever state it was
        left in (spec「全選切換」), so filtering back and forth does not quietly
        unmark what was marked on purpose.
        """
        row = self.current()          # the cursor stays where it was: this is not a jump
        keys = [r.key for r in self.shown()]
        if not keys:
            return
        if all(key in self.marked for key in keys):
            self.marked -= set(keys)  # visible ones only; a hidden mark is another decision
        else:
            self.marked |= set(keys)
        self.show(keep=row.key if row else None)

    @work
    async def action_edit(self) -> None:
        if self.tab != "agora":
            self.say("改標頭是 Agora 頁的動作")
            return
        row = self.current()
        if not row:
            return
        if await self._refuse(row, "改標頭"):
            return
        code = self.outside(argv_for("edit", [row], None, None)[0])   # the editor needs the terminal
        self.reload()
        self.say("完成" if code == 0 else "改標頭沒有成功", failed=code != 0)

    async def _refuse(self, row: Row, what: str) -> bool:
        """A session Drive no longer has is not written to from here either (T2 3.3).

        The message is the command mode's own - it says which command settles it -
        shown on the screen, because that is where the user is. Returns whether it
        refused, so no agent is opened and no editor starts.
        """
        refusal = store.cloud_gone(self.index, row.key)
        if not refusal:
            return False
        await self.push_screen_wait(Tell(f"不能{what}", refusal, ok=False))
        return True

    @work
    async def action_pull(self) -> None:
        """Pull what is marked: a row on the agora tab off Drive, one on the import
        tab into the full-text cache. There is no "all of them" here either."""
        rows = self.chosen_rows()
        if not rows:
            self._nothing_to_do("先選要拉下來的 Session")
            return
        argv = argv_for("pull", rows, None, None)[0]
        answer = await self.push_screen_wait(Confirm(
            f"把 {len(rows)} 個拉到本機？", ["取消", "確定"], "雲端沒有的：印一行提醒，本機的不動",
            "雲端沒有的就刪掉本機的（等同 --not-exist-delete）",
            "⚠ 勾了：雲端沒有的，本機這份會被刪掉"))
        if answer and answer[0] == 1:
            await self.act(f"拉下 {len(rows)} 個", argv + (["--not-exist-delete"] if answer[1] else []),
                           sent=[r.key for r in rows])

    @work
    async def action_push(self) -> None:
        rows = self.chosen_rows()
        if not rows:
            self._nothing_to_do("先選要寫回的 Session")
            return
        argv = argv_for("push", rows, None, None)[0]
        answer = await self.push_screen_wait(Confirm(
            f"把 {len(rows)} 個寫回 Drive？", ["取消", "確定"],
            "雲端沒有的：印一行提醒，不傳；同名的檔案直接覆蓋",
            "雲端沒有的就傳回去（等同 --not-exist-upload）",
            "⚠ 勾了：別台機器刪掉的 Session 會被傳回 Drive"))
        if answer and answer[0] == 1:
            await self.act("寫回 Drive", argv + (["--not-exist-upload"] if answer[1] else []),
                           sent=[r.key for r in rows])

    @work
    async def action_delete(self) -> None:
        if self.tab != "agora":
            self.say("刪除是 Agora 頁的動作")
            return
        rows = self.chosen_rows()            # every marked row, or the one under the cursor
        if not rows:
            self._nothing_to_do("先選要刪除的 Session")
            return
        listed = "\n".join(f"{r.key}「{r.cells[1]}」" for r in rows[:6]) + ("\n…" if len(rows) > 6 else "")
        if await self.push_screen_wait(Choose(f"把 {len(rows)} 個移到 Drive 垃圾桶？", ["取消", "確定"], listed)) == 1:
            await self.act(f"刪除 {len(rows)} 個", argv_for("delete", rows, None, None)[0],
                           sent=[r.key for r in rows])


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

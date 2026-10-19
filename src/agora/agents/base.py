"""What every agent adapter provides (design.md 5.2 and 5.4).

`cli` only talks to this interface, so adding an agent is one new module.
An adapter knows only its own format: how to split its raw into shared
turns, and how to build its own raw from turns. Adapters never upload
anything; they read and write the agent's own files.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol
import tempfile

TOOL_SUMMARY_MAX = 200

#: summarize is one model round trip, not a CLI poke, so it gets its own budget: a
#: merge prompt is a whole session to read, and the 60 s a CLI poke gets is not
#: enough for a free model to answer one (was in opencode.py, review B1).
SUMMARIZE_TIMEOUT = 600

#: The stand-in answer an adapter puts in when the session it loads ends with a
#: question nobody answered (a user turn, or a tool_use whose result never came),
#: so the next thing that is said is not read as the answer to it (design 5.4 W1).
#: cli has the same string for the cross-agent path; this is the copy the adapters
#: may use, so `append_native` does not have to be told which text to write.
NO_REPLY = "（這一段在這裡結束，當時沒有回覆）"

Turns = list[tuple[str, list[str]]]   # [(role, lines)]; role is "user" or "assistant"


class _NoRaw:
    """Sentinel meaning `raw` was not passed; a raw_file may still supply it."""


@dataclass
class Exported:
    """One agent session read back from the agent's own storage.

    `raw_file` is the file-backed form (impl-mem B1b-2): the bytes are on disk,
    and the in-memory `raw` property reads them only when something still needs
    the whole bytes. Callers that only need the md5 or to stage the file should
    use `raw_file`/`raw_md5`/`raw_size` and never touch `.raw`.
    """

    session_id: str                 # the agent's id (ses_… / uuid)
    raw_file: Path | None = None      # file-backed export (B1b-2)
    raw_md5: str | None = None        # md5 of the bytes raw_file names
    raw_size: int | None = None       # size of those bytes
    dir: str | None = None            # project directory the session belongs to
    title: str | None = None
    created_at: str | None = None     # RFC 3339 UTC
    agent_version: str | None = None
    message_count: int = 0
    model: str | None = None          # the model used most recently
    # Contract on `turns` (review 低1): it caches what the adapter parsed from
    # `raw` at export time, so it must equal `agent.turns(raw)`. A path that
    # swaps or rewrites `raw` without reparsing must set `turns` back to None.
    turns: Turns | None = None        # shared turns carried from the parse (B1a)
    # Private carriers of `raw`: not part of equality or repr (review 低1) - two exports of
    # the same session are the same export whichever has read its file back yet.
    _raw: bytes | None = field(default=None, repr=False, compare=False, init=False)
    _raw_tmpdir: tempfile.TemporaryDirectory | None = field(default=None, repr=False,
                                                            compare=False, init=False)

    def __init__(self, session_id: str, *, raw: bytes = _NoRaw,
                 raw_file: Path | None = None, raw_md5: str | None = None,
                 raw_size: int | None = None, dir: str | None = None,
                 title: str | None = None, created_at: str | None = None,
                 agent_version: str | None = None, message_count: int = 0,
                 model: str | None = None, turns: Turns | None = None,
                 _raw_tmpdir: tempfile.TemporaryDirectory | None = None) -> None:
        self.session_id = session_id
        self.raw_file = raw_file
        self.raw_md5 = raw_md5
        self.raw_size = raw_size
        self.dir = dir
        self.title = title
        self.created_at = created_at
        self.agent_version = agent_version
        self.message_count = message_count
        self.model = model
        self.turns = turns
        self._raw_tmpdir = _raw_tmpdir
        self._raw = None if raw is _NoRaw else raw

    @property
    def raw(self) -> bytes:
        """The original export bytes. First access from a file reads it once."""
        if self._raw is not None:
            return self._raw
        if self.raw_file is not None:
            self._raw = self.raw_file.read_bytes()
            return self._raw
        return b""

    @raw.setter
    def raw(self, value: bytes) -> None:
        # New bytes make the file-backed facts stale (review 低2): `_write_one` trusts
        # `raw_md5`, and an old one would skip a version that has to be written.
        self._raw = value
        self.raw_file = None
        self.raw_md5 = None
        self.raw_size = None


@dataclass
class Launch:
    """How to run the agent in the foreground, and how to find the result."""

    argv: list[str]
    cwd: str
    agent_session_id: str | None    # known before launch when the agent lets us pick it
    before_count: int = 0           # messages present before the user starts (S9)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Listed:
    """One agent session found on this machine, for the interactive mode's import tab."""

    session_id: str
    dir: str | None
    title: str | None
    updated_at: str | None          # RFC 3339 UTC, for sorting newest first


class Agent(Protocol):
    name: str                       # "opencode" | "claude"

    def export(self, session_id: str) -> Exported:
        """Read one session by the agent's id. Raise AgentError if it is missing or unreadable."""

    def turns(self, raw: bytes) -> Turns:
        """Split this agent's raw into shared turns: text plus one line per tool call (design 4.4)."""

    def native(self, turns: Turns) -> bytes:
        """Build this agent's raw from shared turns, so a merged or other-agent session loads natively."""

    def append_native(self, raw: bytes, turns: Turns) -> bytes:
        """`raw` (this agent's own format, unchanged) with `turns` hung off its end.

        The same-agent fork: A's raw loads as it is, tool calls and all (design
        5.4 point 2), so the two turns that say what the fork is cannot go through
        `native()` - that rebuilds the conversation and loses everything a rebuild
        cannot express. The contract:
          - `turns(result)` is `turns(raw)` plus, if A ended with a question
            nobody answered, one assistant `NO_REPLY`, plus `turns`;
          - nothing in `raw` is rewritten - not one line, not one byte - and the
            sidecar (claude's `aux`) is handed on untouched;
          - the new turns are chained to A's last real line, so the agent picks the
            whole conversation up again rather than only the tail.
        """

    def list_sessions(self) -> list[Listed]:
        """Every session of this agent on this machine, across all projects (design 5.9). Read only."""

    def last_message(self, session_id: str) -> tuple[str, str] | None:
        """(role, text) of the session's last user or assistant text, as stored; None if there is none."""

    def search_text(self, keyword: str, only: "set[str] | None" = None) -> "Iterator[str]":
        """Session ids, across all projects (or only those in `only`), whose text contains keyword; as found."""

    def summarize(self, prompt: str, workdir: Path) -> tuple[str, str | None]:
        """Run the agent once headless with no tools allowed; return (text, model).

        Leaves no session behind: deletes only the one session this run made (design 5.3).
        """

    def start_native(self, raw: bytes, workdir: Path, *, headless: bool = False,
                     headless_port: int | None = None,
                     harness_file: Path | None = None) -> Launch:
        """Load raw (this agent's format) as a brand-new session and return how to open it.

        `headless` (sidecar's `agora continue --headless`) asks for the agent's
        programmatic mode instead of the TUI (design D6): claude speaks
        stream-json on stdio, opencode listens on `headless_port` after
        `opencode serve`. The caller owns stdio and the port; agora stays off
        stdout while the agent runs.
        """

    def collect(self, launch: Launch) -> Exported | None:
        """After the agent exits, read the session it used; None if nothing new was said."""


class AgentError(RuntimeError):
    """The agent's files or CLI did not behave as expected."""


def env_seconds(name: str, default: float) -> float:
    """A deadline from the environment; a value we cannot read leaves the default.

    Seconds, so a fraction is allowed - claude's `float()` read "1.5" and a test does
    too. The two adapters used to read this differently and claude's raised on
    "abc", which `summarize` does not catch, so a typo in the environment became the
    CLI's "unexpected error" instead of a fallback (review B1, K3).
    """
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def summarize_timeout() -> float:
    return env_seconds("AGORA_SUMMARIZE_TIMEOUT", SUMMARIZE_TIMEOUT)


def iso_utc(seconds: float) -> str:
    """RFC 3339 UTC to the second - the one shape three places used to write (B2)."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def warn(message: str) -> None:
    """One line on stderr, marked the way every agora message is (review B5)."""
    print(f"[agora] {message}", file=sys.stderr)


def agent_cmd(name: str) -> str:
    """The agent executable agora runs: `AGORA_<NAME>_CMD` overrides the name (#77).

    Every call that runs the agent's CLI goes through here, so the value may be
    an executable path or a name found on PATH - a wrapper executable can stand
    in for `opencode`/`claude` (MyLinuxPool's sidecar sets
    `AGORA_CLAUDE_CMD=claude-real`). Unset, the name itself is used.
    """
    return os.environ.get(f"AGORA_{name.upper()}_CMD", name)


def tool_line(name: str, args: object) -> str:
    text = str(args).replace("\n", " ")
    if len(text) > TOOL_SUMMARY_MAX:
        text = text[:TOOL_SUMMARY_MAX] + "…"
    return f"[tool] {name} {text}".rstrip()


def merge_turns(turns: Turns) -> Turns:
    """Drop empty lines and merge consecutive same-role entries into one turn (CL10)."""
    merged: Turns = []
    for role, lines in turns:
        lines = [line for line in lines if line.strip()]
        if not lines:
            continue
        if merged and merged[-1][0] == role:
            merged[-1][1].extend(lines)
        else:
            merged.append((role, list(lines)))
    return merged


def format_reading(turns: Turns) -> str:
    """The reading version (design.md 4.4).

    turns: [(role, lines)] where role is "user" or "assistant" and lines are
    already filtered: text, tool_line(...) results, or "[skip <type>]".
    Tool results and thinking must not be passed in.
    """
    return "\n".join(f"## {role}\n" + "\n".join(lines) + "\n" for role, lines in merge_turns(turns))


def reading(agent: "Agent", raw: bytes) -> str:
    """The reading version of a raw session, through the agent's own turns()."""
    return format_reading(agent.turns(raw))


def reading_of(agent: "Agent", exported: Exported) -> str:
    """The reading version of an export, without parsing raw a second time (impl-mem 2a C1).

    The export already parsed the session when the adapter carried the turns
    over on it (`Exported.turns`); then the reading version is just those turns
    laid out. When it did not - the other adapters, fakes, or a caller-built
    Exported - raw is parsed here, exactly as `reading` always did. Both arms
    produce the same bytes for the same session; the flag only decides whether
    the parse happens twice.
    """
    if exported.turns is not None:
        return format_reading(exported.turns)
    return reading(agent, exported.raw)

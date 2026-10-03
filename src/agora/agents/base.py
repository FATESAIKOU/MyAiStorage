"""What every agent adapter provides (design.md 5.2 and 5.4).

`cli` only talks to this interface, so adding an agent is one new module.
An adapter knows only its own format: how to split its raw into shared
turns, and how to build its own raw from turns. Adapters never upload
anything; they read and write the agent's own files.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

TOOL_SUMMARY_MAX = 200

#: summarize is one model round trip, not a CLI poke, so it gets its own budget: a
#: merge prompt is a whole session to read, and the 60 s a CLI poke gets is not
#: enough for a free model to answer one (was in opencode.py, review B1).
SUMMARIZE_TIMEOUT = 600

Turns = list[tuple[str, list[str]]]   # [(role, lines)]; role is "user" or "assistant"


@dataclass
class Exported:
    """One agent session read back from the agent's own storage."""

    session_id: str                 # the agent's id (ses_… / uuid)
    raw: bytes                      # the original export, unchanged
    dir: str | None = None          # project directory the session belongs to
    title: str | None = None
    created_at: str | None = None   # RFC 3339 UTC
    agent_version: str | None = None
    message_count: int = 0
    model: str | None = None        # the model used most recently, e.g. "space-bunny-free" or "claude-opus-5-5"


@dataclass
class Launch:
    """How to run the agent in the foreground, and how to find the result."""

    argv: list[str]
    cwd: str
    agent_session_id: str | None    # known before launch when the agent lets us pick it
    before_count: int = 0           # messages present before the user starts (S9)


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

    def start_native(self, raw: bytes, workdir: Path) -> Launch:
        """Load raw (this agent's format) as a brand-new session and return how to open it."""

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
    """The agent executable; AGORA_OPENCODE_CMD / AGORA_CLAUDE_CMD override it for tests."""
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

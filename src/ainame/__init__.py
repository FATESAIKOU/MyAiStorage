"""AI names: the reference grammar for a seat (`#97`, MyPMO#80).

An AI name is `<project>-<role>-<team>[-<seq>]` and names a seat: which
project, which team, which role, which one. A string is a legal AI name if and
only if all five steps below pass; `parse` reports which step failed.

1. characters and length: 1..`MAX_LEN` characters, only `a-z`, `0-9` and `-`;
   the first character is a letter; no trailing `-`; no `--`. The comparison is
   over the whole string - a trailing newline or a space is illegal.
2. split on `-`; the rightmost segment that is a role word and is not the first
   segment is the role.
3. project = every segment left of the role (at least one; it may contain `-`
   and may itself contain a role word).
4. team = the segment right of the role, always required: 1..`TEAM_MAX_LEN`
   lowercase letters and digits, starting with a letter.
5. seq: `pm` and `secretary` must not be followed by another segment;
   `arch`, `impl`, `test` and `rev` must be followed by exactly one sequence
   number, 1..`SEQ_MAX`, without a leading zero.

There are no exceptions to any of this. `@…` is a group address: it only
arrives in phase 3, and `is_group` here only looks at the first character.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

#: The longest legal AI name, in characters.
MAX_LEN = 32

#: Every role word, in the order the design lists them.
ROLES = ("pm", "arch", "impl", "test", "rev", "secretary")

#: The roles that name one seat on their own: no sequence number.
SINGLE_ROLES = ("pm", "secretary")

#: The longest legal team code, in characters.
TEAM_MAX_LEN = 8

#: The sequence numbers run 1..SEQ_MAX and are never zero-padded.
SEQ_MAX = 99

_CHARS_RE = re.compile(r"[a-z0-9-]+")  # fullmatch: the whole string
_LETTER_RE = re.compile(r"[a-z]")
_TEAM_RE = re.compile(r"[a-z][a-z0-9]{0,%d}" % (TEAM_MAX_LEN - 1))
_SEQ_RE = re.compile(r"[1-9][0-9]?")

_ROLE_LIST = " | ".join(ROLES)


def _step1(name: object) -> list[str]:
    """Step 1: characters and length. Returns the segments split on `-`."""
    if not isinstance(name, str):
        raise ValueError(f"step 1 failed: not a string ({type(name).__name__})")
    if not name:
        raise ValueError("step 1 failed: the name is empty")
    if len(name) > MAX_LEN:
        raise ValueError(f"step 1 failed: longer than {MAX_LEN} characters ({len(name)})")
    if not _CHARS_RE.fullmatch(name):
        bad = sorted({character for character in name if character not in "abcdefghijklmnopqrstuvwxyz0123456789-"})
        raise ValueError(
            f"step 1 failed: only a-z, 0-9 and '-' are allowed, and the name is all lowercase "
            f"(unexpected {bad!r} in {name!r})"
        )
    if not _LETTER_RE.match(name[0]):
        raise ValueError(f"step 1 failed: must start with a letter ({name!r})")
    if name.endswith("-"):
        raise ValueError(f"step 1 failed: must not end with '-' ({name!r})")
    if "--" in name:
        raise ValueError(f"step 1 failed: '--' is not allowed ({name!r})")
    return name.split("-")


def _step2(segments: list[str]) -> int:
    """Step 2: the rightmost role segment that is not the first segment."""
    for index in range(len(segments) - 1, 0, -1):
        if segments[index] in ROLES:
            return index
    raise ValueError(
        f"step 2 failed: no role segment ({_ROLE_LIST}) after the project name ({'-'.join(segments)!r})"
    )


def _step3(segments: list[str], role_index: int) -> str:
    """Step 3: every segment left of the role, joined back with `-`."""
    project = "-".join(segments[:role_index])
    if not project:
        # Unreachable: step 2 never returns index 0.
        raise ValueError(f"step 3 failed: the project name is missing ({'-'.join(segments)!r})")
    return project


def _step4(segments: list[str], role_index: int) -> str:
    """Step 4: the team segment right of the role; it is always required."""
    if len(segments) <= role_index + 1:
        raise ValueError(
            "step 4 failed: the team segment is missing; it is always required "
            "(write '<project>-<role>-<team>', e.g. 'mylinuxpool-impl-w1-1')"
        )
    team = segments[role_index + 1]
    if not _TEAM_RE.fullmatch(team):
        raise ValueError(
            f"step 4 failed: the team segment must be 1-{TEAM_MAX_LEN} lowercase letters and digits "
            f"starting with a letter, got {team!r}"
        )
    return team


def _step5(segments: list[str], role_index: int) -> int | None:
    """Step 5: the sequence number after the team, or its absence."""
    role = segments[role_index]
    tail = segments[role_index + 2:]
    if role in SINGLE_ROLES:
        if tail:
            raise ValueError(
                f"step 5 failed: role {role!r} takes no sequence number "
                f"(remove '-{'-'.join(tail)}')"
            )
        return None
    if not tail:
        raise ValueError(
            f"step 5 failed: role {role!r} needs a sequence number after the team "
            f"(1-{SEQ_MAX}, for example 'mylinuxpool-impl-w1-1')"
        )
    if len(tail) > 1:
        raise ValueError(
            f"step 5 failed: role {role!r} takes exactly one segment after the team "
            f"(got {len(tail)})"
        )
    text = tail[0]
    if not _SEQ_RE.fullmatch(text):
        raise ValueError(
            f"step 5 failed: the sequence number must be 1-{SEQ_MAX} without a leading zero, got {text!r}"
        )
    return int(text)


@dataclass(frozen=True)
class Name:
    """An AI name taken apart. `str(name)` is the name itself."""

    project: str
    role: str
    team: str
    seq: int | None = None

    def __str__(self) -> str:
        if self.seq is None:
            return f"{self.project}-{self.role}-{self.team}"
        return f"{self.project}-{self.role}-{self.team}-{self.seq}"


def parse(name: object) -> Name:
    """Take an AI name apart; ValueError says which step failed."""
    segments = _step1(name)
    role_index = _step2(segments)
    return Name(
        project=_step3(segments, role_index),
        role=segments[role_index],
        team=_step4(segments, role_index),
        seq=_step5(segments, role_index),
    )


def is_valid(name: object) -> bool:
    """Whether the string is a legal AI name."""
    try:
        parse(name)
    except ValueError:
        return False
    return True


def format(project: str, role: str, team: str, seq: int | None = None) -> str:  # noqa: A001
    """Write an AI name from its parts, and check that it is legal."""
    parts = [project, role, team]
    if seq is not None:
        parts.append(str(seq))
    name = "-".join(parts)
    parse(name)
    return name


def is_group(name: object) -> bool:
    """Whether the address is a group address (`@…`). Only phase 3 sends those."""
    return isinstance(name, str) and name.startswith("@")
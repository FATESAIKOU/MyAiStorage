"""`render`: build the instruction file of one agent (spec「產生 agent 指示檔」).

A harness is delivered to the agent it belongs to, not to the user level: the
harness is read through the same reading layer as `show` (byte for byte), an
environment note can be put after it, and the result is written to `--out` and
nowhere else. Both parts go in as they are - no blank line is inserted and no
trailing newline is dropped - so what the agent reads is exactly what the
harness and the note already say; a note that wants a blank line before itself
starts with one.

`render` validates the harness format before writing (parseable frontmatter
and `type: Harness`), sharing the check with `install`: an agent is not started
with a broken harness or without rules.
"""

from __future__ import annotations

from pathlib import Path

from atelier.atomic import WriteError, write_atomic


class AppendMissing(Exception):
    """`--append` names a file that is not there (exit 1, like a missing harness)."""


def read_append(path: str | Path) -> bytes:
    """The `--append` file's exact bytes.

    A file that is not there is exit 1 - the caller named a note that does not
    exist, which is a wrong request, not a broken machine. A file that exists but
    cannot be read is exit 2 with the reason.
    """
    note = Path(path).expanduser()
    try:
        return note.read_bytes()
    except FileNotFoundError as e:
        raise AppendMissing(f"--append 的檔不存在：{note}") from e
    except OSError as e:
        raise WriteError(f"讀不到 --append 的檔 {note}：{e}") from e


def compose(harness: bytes, note: bytes | None) -> bytes:
    """The harness, then the note after it; the order is fixed and neither part
    is touched. The harness alone stays byte-for-byte what `show` prints."""
    return harness if note is None else harness + note


def write_instructions(out: str | Path, data: bytes) -> Path:
    """Write `data` to `--out` atomically, and return the path.

    The directory of `--out` is the caller's - the agent's own state directory -
    so a missing one is a wrong path (exit 2), not something atelier builds for
    you: a path that cannot be written must not leave a directory behind that
    only looks like it was meant.
    """
    target = Path(out).expanduser()
    if not target.parent.is_dir():
        raise WriteError(f"--out 的目錄不存在：{target.parent}"
                         "（目錄由啟動 agent 的一方準備，render 不會自己建立）")
    write_atomic(target, data, make_parents=False)
    return target
"""Install a harness into each agent's user-level instruction file (design D8).

Option 1, the one chosen 2026-10-16: claude gets its own file under the
user-level `rules/` (one file per harness, nothing else touched), opencode gets
a managed block in `config/opencode/AGENTS.md`. Both targets are derived from
the environment - `CLAUDE_CONFIG_DIR` else `$HOME/.claude`,
`XDG_CONFIG_HOME` else `$HOME/.config` - so a test (or an image build) can
point them anywhere. Writes go through a same-directory temporary file and
`os.replace`, so an interrupted install leaves the old file, never a truncated
one. Re-running is idempotent: the block is replaced, user content outside it
is kept byte-for-byte, and `settings.json`, project files and the working
directory are never touched.
"""

from __future__ import annotations

import os
from pathlib import Path

from atelier.atomic import write_atomic
from atelier.harness import HarnessFormatError, HarnessId, frontmatter


class InstallError(RuntimeError):
    """A target atelier would not write into: a damaged block, a harness that is
    not one (exit 2). A target that simply cannot be written raises WriteError."""


def claude_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def opencode_dir() -> Path:
    override = os.environ.get("XDG_CONFIG_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".config"
    return base / "opencode"


def claude_target(ident: HarnessId) -> Path:
    """The independent per-harness file claude loads from the user level (D8)."""
    return claude_dir() / "rules" / f"atelier-{ident.name}.md"


def opencode_target(ident: HarnessId) -> Path:
    return opencode_dir() / "AGENTS.md"


def marker_lines(ident: HarnessId) -> tuple[str, str]:
    return f"<!-- atelier:{ident.name} -->", f"<!-- /atelier:{ident.name} -->"


def managed_block(ident: HarnessId, content: str) -> str:
    start, end = marker_lines(ident)
    if not content.endswith("\n"):
        content += "\n"
    return f"{start}\n{content}{end}\n"


def _marker_positions(text: str, start: str, end: str) -> list[tuple[str, int, int]]:
    """(kind, line start, line end) for every whole line equal to a marker.

    Only a complete line counts: a mention of the marker inside a line of the
    user's own prose is not a marker and cannot start (or end) a block. A
    trailing \\r of a \\r\\n file belongs to the line ending, not to the line.
    """
    found: list[tuple[str, int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body == start:
            found.append(("open", offset, offset + len(line)))
        elif body == end:
            found.append(("close", offset, offset + len(line)))
        offset += len(line)
    return found


def _looks_like_our_block(region: str) -> bool:
    """Only our blocks hold a harness: frontmatter that parses and says type: Harness.

    A pair of marker lines around text that merely starts with `---` (or whose
    frontmatter is another kind of document) is not ours - the caller refuses
    it rather than replacing the user's text (低6).
    """
    try:
        data = frontmatter(region.replace("\r\n", "\n").replace("\r", "\n"))
    except HarnessFormatError:
        return False
    return data.get("type") == "Harness"


def replace_managed_block(text: str, ident: HarnessId, block: str) -> str:
    """Replace this harness's block, keep everything else, never stack.

    A block is ours only when a whole opening marker line is paired, in order,
    with a whole closing marker line and the text between them looks like a
    block we wrote (a harness with frontmatter). Anything else - an opening
    marker without its closing marker, an orphan closing marker, or a pair
    around the user's own text - is refused, so a stray marker line in the
    user's content can never make the replacement swallow it. Duplicates of the
    same harness (should they exist) collapse into the one fresh block.
    """
    start, end = marker_lines(ident)
    marks = _marker_positions(text, start, end)
    if not marks:
        if not text:
            return block
        return text + ("" if text.endswith("\n") else "\n") + block
    kinds = [kind for kind, _, _ in marks]
    if kinds != ["open", "close"] * (len(kinds) // 2):
        raise InstallError(f"{ident} 的管理區塊壞了（標記不成對或有孤兒），不覆蓋")
    for index in range(0, len(marks), 2):
        open_line, close_line = marks[index], marks[index + 1]
        if not _looks_like_our_block(text[open_line[2]:close_line[1]]):
            raise InstallError(f"{ident} 的標記成對了，但中間不是 atelier 寫的區塊，不覆蓋")
    # The first block is replaced; duplicates of the same harness collapse into
    # it; everything around all of them is kept byte-for-byte.
    pairs = list(zip(marks[0::2], marks[1::2]))
    parts = [text[:pairs[0][0][1]], block]
    for index, (_, close_line) in enumerate(pairs):
        next_start = pairs[index + 1][0][1] if index + 1 < len(pairs) else len(text)
        parts.append(text[close_line[2]:next_start])
    return "".join(parts)


def install_claude(target: Path, raw: bytes) -> None:
    """Write the harness as-is: the file is atelier's alone (spec「獨立檔」)."""
    write_atomic(target, raw)


def install_opencode(target: Path, ident: HarnessId, raw_text: str) -> None:
    """Wrap the harness in the managed block, keeping the user's own content."""
    start, end = marker_lines(ident)
    if _marker_positions(raw_text, start, end):
        # The block we would write would carry marker lines inside itself, and
        # every later install would refuse the target: ask for a harness rewrite
        # instead of writing something we cannot manage (低2).
        raise InstallError(
            f"{ident} 的 harness 內文含整行的管理標記；裝進去會讓之後的 install 拒絕這個檔，"
            "請改寫 harness 後再裝"
        )
    try:
        # Bytes, not text: read_text would translate the user's CRLF/CR lines
        # to LF, and "everything outside the block is kept" must be true (低1).
        current = target.read_bytes().decode("utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError) as e:
        raise InstallError(f"讀不到 {target}：{e}") from e
    new_text = replace_managed_block(current, ident, managed_block(ident, raw_text))
    write_atomic(target, new_text.encode("utf-8"))

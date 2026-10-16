"""Install a harness into each agent's user-level instruction file (design D8).

Option 1, the one chosen 2026-10-16: claude gets its own file under the
user-level `rules/` (one file per harness, nothing else touched), opencode gets
a managed block in `config/opencode/AGENTS.md`. Both targets are derived from
the environment - `CLAUDE_CONFIG_DIR` else `$HOME/.claude`,
`XDG_CONFIG_HOME` else `$HOME/.config` - so a test (or an image build) can
point them anywhere. Re-running is idempotent: the block is replaced, user
content outside it is kept byte-for-byte, and `settings.json`, project files
and the working directory are never touched.
"""

from __future__ import annotations

import os
from pathlib import Path
import re

from atelier.harness import HarnessId


class InstallError(RuntimeError):
    """An install target cannot be written (exit 2)."""


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


def replace_managed_block(text: str, ident: HarnessId, block: str) -> str:
    """Replace this harness's block, keep everything else, never stack.

    Duplicates of the same harness (should they exist) collapse into the one
    fresh block; an opening marker without its closing marker is a damaged file
    and is refused rather than appended to.
    """
    start, end = marker_lines(ident)
    pattern = re.compile(rf"(?ms)^{re.escape(start)}\n.*?^{re.escape(end)}\n?")
    matches = list(pattern.finditer(text))
    if matches:
        # The first block is replaced; duplicates of the same harness collapse
        # into it; everything around all of them is kept byte-for-byte.
        parts: list[str] = []
        last = 0
        for index, match in enumerate(matches):
            parts.append(text[last:match.start()])
            if index == 0:
                parts.append(block)
            last = match.end()
        parts.append(text[last:])
        return "".join(parts)
    if start in text:
        raise InstallError(f"{ident} 的管理區塊壞了（有開頭沒有結尾），不覆蓋")
    if not text:
        return block
    return text + ("" if text.endswith("\n") else "\n") + block


def install_claude(target: Path, raw: bytes) -> None:
    """Write the harness as-is: the file is atelier's alone (spec「獨立檔」)."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as e:
        raise InstallError(f"寫不進去 {target}：{e}") from e


def install_opencode(target: Path, ident: HarnessId, raw_text: str) -> None:
    """Wrap the harness in the managed block, keeping the user's own content."""
    try:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
    except OSError as e:
        raise InstallError(f"讀不到 {target}：{e}") from e
    new_text = replace_managed_block(current, ident, managed_block(ident, raw_text))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
    except OSError as e:
        raise InstallError(f"寫不進去 {target}：{e}") from e

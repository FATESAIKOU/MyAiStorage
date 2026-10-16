"""Harness ids, paths, version strings and the section split (design D2/D3/D4).

The version is decided by the version string alone - the frontmatter's
`version` and the file name - never by the body (spec「識別、版本與路徑」).
The body is only split into its H2 sections for `--section` / `--json`.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import yaml

#: The roles of a harness (design D2; the phase-3 reviewer maps to `arch`).
ROLES = ("pm", "arch", "impl", "test")

#: The five H2 sections of a harness body (design D2).
SECTION_NAMES = ("know", "do", "judge", "dont", "capabilities")

_ID = re.compile(r"^atelier:([a-z0-9][a-z0-9-]*)-(pm|arch|impl|test)$")
_VERSION = re.compile(r"^v(\d+)$")
_PATH = re.compile(r"^atelier/([a-z0-9][a-z0-9-]*)/(pm|arch|impl|test)/v(\d+)\.md$")


class InvalidId(ValueError):
    """The id is not `atelier:<team>-<role>[@v<N>]` (exit 1)."""


class HarnessFormatError(RuntimeError):
    """The file cannot be split/parsed (exit 2): frontmatter missing or not YAML."""


@dataclass(frozen=True)
class HarnessId:
    team: str
    role: str
    version: int | None   # None = the latest on disk

    @property
    def name(self) -> str:
        return f"{self.team}-{self.role}"

    @property
    def version_str(self) -> str:
        assert self.version is not None
        return f"v{self.version}"

    def __str__(self) -> str:
        if self.version is None:
            return f"atelier:{self.name}"
        return f"atelier:{self.name}@{self.version_str}"


def parse_id(text: str) -> HarnessId:
    """`atelier:<team>-<role>` or `atelier:<team>-<role>@v<N>` → HarnessId."""
    raw = (text or "").strip()
    version: int | None = None
    if "@" in raw:
        raw, _, version_text = raw.partition("@")
        match = _VERSION.match(version_text)
        if not match:
            raise InvalidId(f"版本要寫成 @v<N>（例如 @v0）：{text}")
        version = int(match.group(1))
    match = _ID.match(raw)
    if not match:
        raise InvalidId(f"id 要寫成 atelier:<team>-<role>[@v<N>]（例如 atelier:worker-team-pm@v0）：{text}")
    return HarnessId(team=match.group(1), role=match.group(2), version=version)


def path_for(team: str, role: str, version: int) -> str:
    return f"atelier/{team}/{role}/v{version}.md"


def parse_path(path: str) -> tuple[str, str, int] | None:
    """`atelier/<team>/<role>/v<N>.md` → (team, role, N); anything else → None."""
    match = _PATH.match(path)
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def split_sections(text: str) -> dict[str, str]:
    """Every H2 section's body, in file order, without the heading itself.

    Leading/trailing blank lines of a body are dropped; the lines in between
    are kept as they are (trailing spaces included - `show` is byte-for-byte
    when no section is asked for, and a section must not be tidied either).
    """
    sections: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if name is not None:
            sections[name] = "\n".join(lines).strip("\n")

    for line in text.split("\n"):
        if line.startswith("## "):
            flush()
            name = line[3:].strip()
            lines = []
        elif name is not None:
            lines.append(line)
    flush()
    return sections


def frontmatter(text: str) -> dict:
    """The YAML frontmatter block at the top of the file, or HarnessFormatError."""
    if not text.startswith("---\n"):
        raise HarnessFormatError("檔案開頭沒有 frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise HarnessFormatError("frontmatter 沒有結尾的 ---")
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError as e:
        raise HarnessFormatError(f"frontmatter 不是 YAML：{e}") from e
    if not isinstance(data, dict):
        raise HarnessFormatError("frontmatter 不是一個 YAML 物件")
    return data

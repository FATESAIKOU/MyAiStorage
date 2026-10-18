"""Format and append-only checks for the harness tree (openspec atelier-v0, section 2).

The spec's「harness 檔的格式」「識別、版本與路徑」「能力需求與模型欄位」are
checked for every file under `atelier/`: frontmatter fields, id/version/file-name
consistency, the five H2 sections, the `capabilities` H3 subsections (`angareion`
required at v0), `models.floor`/`models.preferred`, no `angareion` H2 section,
and the `know` statement that project instructions come first.

The `v1` additions (change atelier-pm-harness-v1, design D7) are checked in the
same pass: every `pm` harness keeps「審查一律非作者」(not only v0); a `pm` file of
`v>=1` must still write the key sentences (`不是權限`／`照 PMO 的模型輪換表`／
`不指定頻道`／`長時間的指令` - the union of the four body sections, each sentence
at least once, not one section each); and a `v>=1` file's body must not hardcode
tool names, model names, other projects' names, machine addresses or the user's
home path (B-15/B-16; general words like `session`/`輪換表`/`git` are not on the
list, and `CLAUDE.md`/`AGENTS.md` are masked before matching). `v0` is exempt from
the content limits - it is append-only and can no longer change. The body those
limits read is `know`/`do`/`judge`/`dont`: capability subsection names and the
frontmatter are not body (spec「harness 檔的格式」). A capability name only one
environment provides is a warning, never an error (未知能力名只警告), and so is a
`v>=1` file whose `team` is not the earlier version's (同一職務的下一版沿用 team -
only a warning: warnings never block). The
blacklists live here, in one place, with a comment on what they deliberately do
not contain. The
「版本檔 append-only」rule is checked with `git diff --name-status` against the
merge-base with `origin/main` - for HEAD and for the working tree - so an edit
that was committed and then reverted in the working tree, a mode change, a
rename and a file/symlink swap all count; small fake repositories pin every
case, so the rule is exercised even where no origin exists (there the real-tree
check skips with a reason - never silently passes).

The checker lives here, with the tests, as section 2 of the change scopes it
(`tests/unit/test_atelier_harness.py`); wiring it into CI is task 2.6, not done.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from dataclasses import dataclass, field
from pathlib import Path
import os
import re
import subprocess
from typing import Callable

import pytest
import yaml

from atelier.harness import HarnessFormatError, ROLES, frontmatter, parse_path, split_sections

REPO = Path(__file__).resolve().parents[2]
HARNESS_ROOT = REPO / "atelier"

#: The spec's MUSTs, in one place so the checker and the tests cannot drift.
REQUIRED_FIELDS = ("type", "id", "title", "status", "team", "role", "version", "models")
SECTIONS = ("know", "do", "judge", "dont", "capabilities")
STATUSES = ("draft", "stable", "deprecated")
#: Capability names an environment may already use. Unknown names only warn, so
#: this list is a reminder to look after the new names, not a hard gate.
KNOWN_CAPABILITIES = ("github", "angareion", "agora", "channel", "team", "monitor")
#: Names one environment alone provides: a harness shared by several environments
#: (Mac and worker) must not name them - it declares the neutral name instead
#: (`channel`/`team`/`monitor`, design D6). Warned about, never an error.
SINGLE_ENVIRONMENT_CAPABILITIES = ("angareion",)

#: The sections the content limits and the key sentences read - the body. The
#: `capabilities` chapter (its subsection names) and the frontmatter are not body.
BODY_SECTIONS = ("know", "do", "judge", "dont")

#: What a `pm` harness of `v>=1` must still say, wherever it puts it: the union of
#: the four body sections, each sentence at least once (not one section each -
#: `不是權限` lives in `know`/`judge`, the rotation table only in `know`).
PM_KEY_SENTENCES = ("不是權限", "照 PMO 的模型輪換表", "不指定頻道", "長時間的指令")
#: The sentence every `pm` version must keep (judge or dont).
REVIEW_BY_NON_AUTHOR = "審查一律非作者"

#: Content limits for `v>=1` (B-15/B-16): the body names capabilities, never the
#: concrete tool, model, address or path of one environment. Deliberately NOT in
#: the lists below - 一般詞 the rules themselves need (`session`, `輪換表`,
#: `通道`, `CI`, `git`, `commit`, `repo`, `ssh`, `brief`, `worktree`...), this
#: repo's own component names (`atelier`, `github`, `agora`), and general system
#: paths (`/tmp` - the v1 rule「工作檔不放 `/tmp`」needs the word). Tool names of
#: one environment only; model names; other projects' names (public repo).
#: A model name built on a tool name (`claude-…`) is caught by the tool entry.
#: `herdr`／`SendMessage` are the tools this environment actually hands out
#: (review L1) - names only, the harness itself still says nothing about tools.
TOOL_NAMES = ("angareion", "opencode", "claude", "tmux", "llmgateway", "sidecar", "zsh",
              "herdr", "SendMessage")
MODEL_NAMES = ("gpt-", "gpt3", "gpt4", "sonnet", "opus", "haiku", "gemini",
               "deepseek", "qwen", "llama", "mistral", "kimi", "grok")
OTHER_PROJECT_NAMES = ("MyPMO", "MyLinuxPool", "MyBrain")
#: A machine address, or a path inside somebody's home directory. The body is
#: lower-cased before matching, so the pattern is case-insensitive as well:
#: a case-sensitive `/Users/` would never match `lower()`ed text (review M1).
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOME_PATH_RE = re.compile(r"~/|/Users/", re.IGNORECASE)
#: The two project instruction file names, masked before matching: the `know`
#: MUST sentence has to name them, and `CLAUDE.md` contains `claude`.
INSTRUCTION_FILES_RE = re.compile(r"(?i)\b(?:CLAUDE|AGENTS)\.md\b")

TEAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERSION_RE = re.compile(r"^v(\d+)$")
ID_RE = re.compile(r"^atelier:([a-z0-9][a-z0-9-]*)-(pm|arch|impl|test)@v(\d+)$")
#: The sentence the `know` section MUST state. Parenthesised asides are dropped
#: before matching (the real file names AGENTS.md/CLAUDE.md inside them), and the
#: words must appear in this order - a sentence like「指示檔沒有 harness 優先」
#: must not pass (低6).
PROJECT_FIRST = "專案自己的指示檔與工單規則優先"
_PARENTHESISED = re.compile(r"（[^）]*）|\([^)]*\)")


def _states_project_first(know: str) -> bool:
    return PROJECT_FIRST in _PARENTHESISED.sub("", know)

V0_REL = "atelier/worker-team/pm/v0.md"
V1_REL = "atelier/worker-team/pm/v1.md"


@dataclass
class Findings:
    """Format violations (errors) and allowed oddities (warnings)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _body(sections: dict[str, str]) -> str:
    """The body the content limits read: know/do/judge/dont, in that order.

    The `capabilities` chapter (its subsection names, which are capability names
    and may name an environment) and the frontmatter (the `generated` note names
    the model that wrote the file) are not body - spec「harness 檔的格式」.
    """
    return "\n".join(sections.get(name, "") for name in BODY_SECTIONS)


def _forbidden_in_body(path: str, body: str) -> list[str]:
    """Tool names, model names, other projects, addresses and home paths in a `v>=1` body.

    The two project instruction file names are masked first (the `know` MUST
    sentence names them and `CLAUDE.md` contains `claude`); matching is
    case-insensitive, so `CLAUDE` and `claude` are one entry.
    """
    text = INSTRUCTION_FILES_RE.sub("（專案指示檔）", body).lower()
    errors: list[str] = []
    for label, names, why in (
            ("工具名", TOOL_NAMES, "只寫能力；工具名寫在各環境的說明"),
            ("模型名", MODEL_NAMES, "模型只寫「照 PMO 的模型輪換表」"),
            ("其他專案的名字", OTHER_PROJECT_NAMES, "各專案的名字不進 harness")):
        for name in names:
            if name.lower() in text:
                errors.append(f"{path}: 正文寫死了{label}「{name}」（{why}）")
    if IPV4_RE.search(text):
        errors.append(f"{path}: 正文有機器位址（IPv4 樣式）")
    if HOME_PATH_RE.search(text):
        errors.append(f"{path}: 正文有使用者家目錄路徑（`~/`、`/Users/`）；"
                      "通用系統路徑（`/tmp`）不算")
    return errors


def _earlier_same_role_teams(role: str, version: int) -> dict[str, int]:
    """team → highest version below `version` for the same role, from the tree.

    Used for the「共用一份」warning (design D7): the next version of one job keeps
    the earlier version's `team`; a new team on a later version means the file is
    being pinned to one environment. Read from the tree because the compared
    version is a different file - `findings_for` only holds this file's text.
    """
    earlier: dict[str, int] = {}
    for candidate in _harness_paths():
        parts = parse_path(candidate.relative_to(REPO).as_posix())
        if parts is None or parts[1] != role or parts[2] >= version:
            continue
        team, _, n = parts
        earlier[team] = max(earlier.get(team, -1), n)
    return earlier


def _capability_subsections(capabilities: str) -> list[tuple[str, str]]:
    """(name, body) for every `### <name>` of the capabilities section, in order."""
    found: list[tuple[str, str]] = []
    name: str | None = None
    lines: list[str] = []
    for line in capabilities.splitlines():
        if line.startswith("### "):
            if name is not None:
                found.append((name, "\n".join(lines)))
            name, lines = line[4:].strip(), []
        elif name is not None:
            lines.append(line)
    if name is not None:
        found.append((name, "\n".join(lines)))
    return found


def findings_for(path: str, text: str) -> Findings:
    """Every format violation in one harness file.

    `path` is the repository-relative posix path (`atelier/<team>/<role>/v<N>.md`):
    the file name is part of the identification, so the check needs it even when
    the text comes from somewhere else (the corruption tests below).
    """
    found = Findings()

    parts = parse_path(path)
    if parts is None:
        found.errors.append(f"{path}: 路徑不符 atelier/<team>/<role>/v<N>.md")

    try:
        front = frontmatter(text)
    except HarnessFormatError as e:
        found.errors.append(f"{path}: frontmatter 讀不了：{e}")
        return found

    for name in REQUIRED_FIELDS:
        if name not in front:
            found.errors.append(f"{path}: frontmatter 缺必填欄位 {name}")
    if front.get("type") != "Harness":
        found.errors.append(f"{path}: type 要是 Harness（{front.get('type')!r}）")

    team, role, version = front.get("team"), front.get("role"), front.get("version")
    if not isinstance(team, str) or not TEAM_RE.match(team):
        found.errors.append(f"{path}: team 要符合 [a-z0-9][a-z0-9-]*（{team!r}）")
    if not isinstance(role, str) or role not in ROLES:
        found.errors.append(f"{path}: role 要是 pm/arch/impl/test 之一（{role!r}）")
    version_match = VERSION_RE.match(version) if isinstance(version, str) else None
    if version_match is None:
        found.errors.append(f"{path}: version 要是 v 加非負整數（{version!r}）")
    if front.get("status") not in STATUSES:
        found.errors.append(
            f"{path}: status 要是 draft/stable/deprecated 之一（{front.get('status')!r}）")

    ident = front.get("id")
    id_match = ID_RE.match(ident) if isinstance(ident, str) else None
    if id_match is None:
        found.errors.append(f"{path}: id 要是 atelier:<team>-<role>@v<N>（{ident!r}）")
    else:
        id_parts = (id_match.group(1), id_match.group(2), int(id_match.group(3)))
        if id_parts[:2] != (team, role):
            found.errors.append(
                f"{path}: id 的 team/role（{id_parts[0]}-{id_parts[1]}）與 frontmatter 不一致")
        if version_match is not None and id_parts[2] != int(version_match.group(1)):
            found.errors.append(
                f"{path}: id 的版本（v{id_parts[2]}）與 frontmatter 的 version 不一致")
    if parts is not None:
        if parts[:2] != (team, role):
            found.errors.append(f"{path}: 檔名的 team/role 與 frontmatter 不一致")
        if version_match is not None and parts[2] != int(version_match.group(1)):
            found.errors.append(f"{path}: 檔名的版本與 frontmatter 的 version 不一致")
        if id_match is not None and id_parts != parts:
            found.errors.append(f"{path}: id 與檔名不一致（{ident} 對 {path}）")

    models = front.get("models")
    if not isinstance(models, dict):
        found.errors.append(f"{path}: models 要是含 floor 與 preferred 的物件（{models!r}）")
    else:
        if "floor" not in models:
            found.errors.append(f"{path}: models 缺 floor（模型識別碼或 null）")
        elif models["floor"] is not None and not isinstance(models["floor"], str):
            found.errors.append(f"{path}: models.floor 要是模型識別碼或 null")
        if "preferred" not in models:
            found.errors.append(f"{path}: models 缺 preferred（有順序的清單，可空）")
        elif not isinstance(models["preferred"], list):
            found.errors.append(f"{path}: models.preferred 要是有序清單（可空）")

    sections = split_sections(text)
    for name in SECTIONS:
        if name not in sections:
            found.errors.append(f"{path}: 缺 {name} 章節")
    if "angareion" in sections:
        found.errors.append(f"{path}: MUST NOT 有 angareion 章節（它是 capabilities 的子節）")
    if not _states_project_first(sections.get("know", "")):
        found.errors.append(f"{path}: know 沒有明寫「專案自己的指示檔與工單規則優先」")

    body = _body(sections)
    judge_and_dont = sections.get("judge", "") + "\n" + sections.get("dont", "")
    if role == "pm" and REVIEW_BY_NON_AUTHOR not in judge_and_dont:
        found.errors.append(f"{path}: pm 職務要寫「{REVIEW_BY_NON_AUTHOR}」（judge 或 dont）")
    is_v1_or_later = version_match is not None and int(version_match.group(1)) >= 1
    if role == "pm" and is_v1_or_later:
        for sentence in PM_KEY_SENTENCES:
            if sentence not in body:
                found.errors.append(
                    f"{path}: v1 起的 pm 職務缺關鍵句「{sentence}」"
                    "（know/do/judge/dont 合起來至少出現一次）")
    if is_v1_or_later:
        found.errors.extend(_forbidden_in_body(path, body))
        if isinstance(team, str) and isinstance(role, str):
            earlier = _earlier_same_role_teams(role, int(version_match.group(1)))
            for earlier_team, earlier_version in sorted(earlier.items()):
                if earlier_team != team:
                    found.warnings.append(
                        f"{path}: 同一職務的下一版要沿用較早版本的 team"
                        f"（v{earlier_version} 是 {earlier_team}，這份是 {team}）；"
                        "共用一份的職務不綁單一環境（只警告、不擋）")

    capabilities = sections.get("capabilities")
    if capabilities is not None:
        subsections = _capability_subsections(capabilities)
        names = [name for name, _ in subsections]
        if not subsections:
            found.errors.append(f"{path}: capabilities 要有以能力名為名的 H3 子節")
        for name in sorted({n for n in names if names.count(n) > 1}):
            found.errors.append(f"{path}: capabilities 的子節名重複：{name}")
        if parts is not None and parts[2] == 0 and "angareion" not in names:
            found.errors.append(f"{path}: v0 職務檔的 capabilities 缺 angareion 子節")
        for name in names:
            if name not in KNOWN_CAPABILITIES:
                found.warnings.append(f"{path}: 未知的能力名 {name}（只警告、不擋）")
            if is_v1_or_later and name in SINGLE_ENVIRONMENT_CAPABILITIES:
                found.warnings.append(
                    f"{path}: capabilities 宣告了只有單一環境提供的 {name}；"
                    "共用一份的職務用中性名（只警告、不擋）")
        for name, body_of_subsection in subsections:
            if not body_of_subsection.strip():
                continue   # 沒有參數＝留空
            try:
                params = yaml.safe_load(body_of_subsection)
            except yaml.YAMLError as e:
                found.errors.append(f"{path}: capabilities/{name} 的參數不是 YAML：{e}")
                continue
            if not isinstance(params, (dict, list)):
                found.errors.append(
                    f"{path}: capabilities/{name} 要用一行 YAML 或清單寫參數，不寫散文")
    return found


# --- the real tree ---------------------------------------------------------------


def _harness_paths() -> list[Path]:
    if not HARNESS_ROOT.is_dir():
        return []
    return sorted(p for p in HARNESS_ROOT.rglob("*.md")
                  if parse_path(p.relative_to(REPO).as_posix()) is not None)


def test_the_tree_has_at_least_one_harness_file():
    assert _harness_paths(), "atelier/ 底下沒有 harness 檔：這個檢查不可以是空的"


@pytest.mark.parametrize("path", _harness_paths(),
                         ids=lambda p: p.relative_to(REPO).as_posix())
def test_every_harness_in_the_tree_passes_the_format_check(path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        pytest.fail(f"{path} 不是 UTF-8：{e}")
    found = findings_for(path.relative_to(REPO).as_posix(), text)
    assert not found.errors, "\n".join(found.errors)


# --- each rule is pinned with a corrupted copy of the real file ------------------


def _v0_text() -> str:
    return (REPO / V0_REL).read_text(encoding="utf-8")


def _v1_text() -> str:
    return (REPO / V1_REL).read_text(encoding="utf-8")


def _drop_line(text: str, needle: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if needle not in line)


def _drop_section(text: str, heading: str) -> str:
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line[3:].strip() == heading
        if not skipping:
            out.append(line)
    return "".join(out)


def _drop_frontmatter_block(text: str, key: str) -> str:
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        if skipping and (line.startswith("  ") or not line.strip()):
            continue
        skipping = False
        if line.startswith(f"{key}:"):
            skipping = True
            continue
        out.append(line)
    return "".join(out)


MUTATIONS: list[tuple[str, Callable[[str], str], tuple[str, ...]]] = [
    ("拿掉必填欄位 status", lambda t: _drop_line(t, "status:"), ("缺必填欄位", "status")),
    ("拿掉必填欄位 title", lambda t: _drop_line(t, "title:"), ("缺必填欄位", "title")),
    ("version 改歪", lambda t: t.replace("version: v0", "version: vX"), ("version",)),
    ("id 的版本與檔名不一致", lambda t: t.replace("@v0", "@v1", 1), ("id",)),
    ("拿掉 dont 章節", lambda t: _drop_section(t, "dont"), ("dont",)),
    ("拿掉 models 一欄", lambda t: _drop_frontmatter_block(t, "models"), ("models",)),
    ("拿掉 capabilities 的 angareion 子節", lambda t: t.replace("### angareion\n", ""),
     ("angareion",)),
    ("能力子節改成散文", lambda t: t.replace("### github\n", "### github\n這是散文。\n", 1),
     ("散文",)),
    ("多一個 angareion 的 H2 章節",
     lambda t: t.replace("## do\n", "## angareion\n\n參數。\n\n## do\n", 1), ("angareion",)),
    ("know 拿掉專案指示優先", lambda t: _drop_line(t, "與工單規則優先"), ("know",)),
    ("know 寫成反句（指示檔沒有 harness 優先）",
     lambda t: t.replace(
         "**專案自己的指示檔（`AGENTS.md`／`CLAUDE.md`）與工單規則優先**；",
         "專案自己的指示檔沒有 harness 優先；", 1),
     ("know",)),
    ("capabilities 沒有 H3 子節",
     lambda t: "".join(line for line in t.splitlines(keepends=True)
                       if not line.startswith("### ")),
     ("H3",)),
]


@pytest.mark.parametrize("name, corrupt, expected",
                         MUTATIONS, ids=[name for name, _, _ in MUTATIONS])
def test_the_checker_catches_each_format_violation(name, corrupt, expected):
    text = _v0_text()
    broken = corrupt(text)
    assert broken != text, f"這個改壞沒有改到東西：{name}"
    found = findings_for(V0_REL, broken)
    assert any(all(part in error for part in expected) for error in found.errors), \
        f"格式檢查沒有抓到「{name}」：{found.errors}"


def test_the_checker_accepts_the_real_file():
    found = findings_for(V0_REL, _v0_text())
    assert not found.errors, "\n".join(found.errors)


# --- the v1 additions (change atelier-pm-harness-v1, design D7) -----------------

#: One injection per new check, on the real v1 file. `v0` is append-only, so the
#: content limits cannot be pinned on it - they start at `v1`.
V1_MUTATIONS: list[tuple[str, Callable[[str], str], tuple[str, ...]]] = [
    ("拿掉關鍵句（長時間的指令）", lambda t: _drop_line(t, "長時間的指令"), ("關鍵句", "長時間的指令")),
    ("know 寫進工具名", lambda t: _inject(t, "- 收發用 angareion 的指令。\n"), ("工具名", "angareion")),
    ("dont 寫進工具名",
     lambda t: t.replace("## dont\n", "## dont\n\n- 派工用 sidecar 開。\n", 1), ("工具名", "sidecar")),
    ("judge 寫進模型名",
     lambda t: t.replace("## judge\n", "## judge\n\n- 審查交給 gpt-5.2。\n", 1), ("模型名", "gpt-")),
    ("know 寫進機器位址",
     lambda t: t.replace("## know\n", "## know\n\n- 服務在 10.20.30.40。\n", 1), ("位址",)),
    ("do 寫進家目錄路徑",
     lambda t: t.replace("## do\n", "## do\n\n- 工作檔放 ~/work/briefs。\n", 1), ("家目錄",)),
    ("do 寫進 /Users/ 路徑",
     lambda t: t.replace("## do\n", "## do\n\n- 工作檔放 /Users/someone/briefs。\n", 1), ("家目錄",)),
    ("know 寫進工具名 herdr",
     lambda t: t.replace("## know\n", "## know\n\n- 開 session 用 herdr。\n", 1), ("工具名", "herdr")),
    ("dont 寫進工具名 SendMessage",
     lambda t: t.replace("## dont\n", "## dont\n\n- 派工用 SendMessage。\n", 1),
     ("工具名", "SendMessage")),
    ("know 寫進其他專案的名字",
     lambda t: t.replace("## know\n", "## know\n\n- 規則照 MyLinuxPool 的那份。\n", 1), ("其他專案",)),
    ("dont 拿掉審查一律非作者",
     lambda t: t.replace(f"**不審、也不合併自己的 PR：{REVIEW_BY_NON_AUTHOR}。**",
                         "**自己看過就好。**", 1), (REVIEW_BY_NON_AUTHOR,)),
]


def _inject(text: str, line: str) -> str:
    """`line` into the `know` section (the first H2 after the title)."""
    return text.replace("## know\n", f"## know\n\n{line}", 1)


@pytest.mark.parametrize("name, corrupt, expected",
                         V1_MUTATIONS, ids=[name for name, _, _ in V1_MUTATIONS])
def test_the_checker_catches_each_v1_violation(name, corrupt, expected):
    text = _v1_text()
    broken = corrupt(text)
    assert broken != text, f"這個改壞沒有改到東西：{name}"
    found = findings_for(V1_REL, broken)
    assert any(all(part in error for part in expected) for error in found.errors), \
        f"格式檢查沒有抓到「{name}」：{found.errors}"


def test_the_v1_harness_keeps_reviews_by_non_authors():
    """「審查一律非作者」是每一個 pm 版本的規則，不只 v0（design D7）。"""
    found = findings_for(V1_REL, _v1_text())
    assert not found.errors, "\n".join(found.errors)


def test_every_pm_harness_keeps_reviews_by_non_authors():
    pm_files = [p for p in _harness_paths() if parse_path(p.relative_to(REPO).as_posix())[1] == "pm"]
    assert len(pm_files) >= 2, "tree 里沒有 pm 的多個版本，這條檢查等於沒驗到"
    for path in pm_files:
        rel = path.relative_to(REPO).as_posix()
        found = findings_for(rel, path.read_text(encoding="utf-8"))
        assert not found.errors, f"{rel}: {found.errors}"


def test_a_single_environment_capability_name_only_warns():
    """共用一份的職務不該綁單一環境的能力名：警告、不擋（design D6／D7）。"""
    text = _v1_text().replace("### channel\n", "### angareion\n")
    found = findings_for(V1_REL, text)
    assert not found.errors, "\n".join(found.errors)
    assert any("angareion" in warning and "中性名" in warning for warning in found.warnings)


def test_the_v1_harness_passes_the_format_check():
    found = findings_for(V1_REL, _v1_text())
    assert not found.errors, "\n".join(found.errors)
    assert not found.warnings, "\n".join(found.warnings)


def test_the_home_path_check_is_case_insensitive():
    """The body is lower-cased before matching; `/Users/` must still be caught (review M1)."""
    assert HOME_PATH_RE.search("/Users/someone/briefs")
    assert HOME_PATH_RE.search("/users/someone/briefs")
    assert not HOME_PATH_RE.search("/tmp/briefs")


def test_a_later_version_with_a_different_team_only_warns():
    """D7「共用一份」的 team 半條：同一職務的下一版換了 team／role → 警告、不擋（review M2）。

    `v0` 的 team 是 `worker-team`；自洽的另一個 team（`mac`）在格式上沒有錯，但
    「同一職務的下一版」不該突然綁到單一環境，所以只警告。
    """
    text = (_v1_text()
            .replace("id: atelier:worker-team-pm@v1", "id: atelier:mac-pm@v1", 1)
            .replace("team: worker-team", "team: mac", 1))
    found = findings_for("atelier/mac/pm/v1.md", text)
    assert not found.errors, "\n".join(found.errors)
    assert any("較早版本的 team" in warning and "worker-team" in warning
               for warning in found.warnings), found.warnings


def test_the_same_team_as_the_earlier_version_does_not_warn():
    """The real v1 keeps v0's team, so the「共用一份」team 半條 stays quiet."""
    assert not findings_for(V1_REL, _v1_text()).warnings


def test_the_content_limits_skip_v0_and_the_frontmatter():
    """`v0` is append-only and exempt; `generated` is not body (spec「格式」)."""
    injected = _v0_text().replace("## judge\n", "## judge\n\n- 派工用 angareion 的指令。\n", 1)
    assert not findings_for(V0_REL, injected).errors


def test_the_instruction_file_names_are_masked_before_matching():
    """`know` MUST name CLAUDE.md/AGENTS.md; `CLAUDE.md` contains `claude`."""
    text = _v1_text().replace("## know\n", "## know\n\n- 專案指示檔是 CLAUDE.md 與 AGENTS.md。\n", 1)
    assert not findings_for(V1_REL, text).errors


def test_a_general_system_path_is_not_a_home_path():
    """`/tmp` is not private: the v1 rule「工作檔不放 `/tmp`」needs the word."""
    text = _v1_text().replace("## know\n", "## know\n\n- 暫存檔放 /tmp/briefs。\n", 1)
    assert not findings_for(V1_REL, text).errors


def test_the_file_name_version_must_match_the_id():
    found = findings_for("atelier/worker-team/pm/v1.md", _v0_text())
    assert any("檔名" in error for error in found.errors)


def test_a_non_v0_role_file_need_not_declare_angareion():
    """`angareion` is a MUST for v0 role files; later versions only SHOULD."""
    text = _v1_text().replace("### angareion\n\n", "")
    found = findings_for(V1_REL, text)
    assert not found.errors, "\n".join(found.errors)


def test_an_unknown_capability_name_only_warns():
    found = findings_for(V0_REL, _v0_text().replace("### agora", "### 新能力"))
    assert not found.errors, "\n".join(found.errors)
    assert any("新能力" in warning for warning in found.warnings)


def test_duplicate_capability_subsection_names_are_flagged():
    found = findings_for(V0_REL, _v0_text().replace("### agora", "### github"))
    assert any("重複" in error for error in found.errors)


def test_a_missing_floor_is_flagged():
    found = findings_for(V0_REL, _drop_line(_v0_text(), "floor:"))
    assert any("floor" in error for error in found.errors)


def test_broken_frontmatter_is_flagged():
    found = findings_for(V0_REL, _v0_text().replace("type: Harness", "type: [Harness", 1))
    assert any("frontmatter" in error for error in found.errors)


# --- append-only (spec「識別、版本與路徑」) ---------------------------------------


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = _git_result(repo, *args)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


def append_only_base(repo: Path = REPO) -> str | None:
    """The merge-base with `origin/main`, or None when there is no origin/base."""
    if _git_result(repo, "rev-parse", "--verify", "--quiet", "origin/main").returncode != 0:
        return None
    proc = _git_result(repo, "merge-base", "HEAD", "origin/main")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _changed_paths(repo: Path, base_rev: str, *rev_args: str) -> list[tuple[str, str | None, str]]:
    """(status, old path or None, path) for every atelier/ change the diff reports."""
    listing = _git(repo, "diff", "--name-status", base_rev, *rev_args, "--", HARNESS_ROOT.name)
    changes: list[tuple[str, str | None, str]] = []
    for line in listing.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2:
            changes.append((fields[0], None, fields[1]))
        elif len(fields) >= 3:   # R/C: <status>\t<old>\t<new>
            changes.append((fields[0], fields[1], fields[-1]))
    return changes


def _violation_for(status: str, old: str | None, path: str) -> str | None:
    """The message for a forbidden change, or None for the one allowed change (A of v<N>.md)."""
    if status == "A":
        if parse_path(path) is not None:
            return None   # a new version file: the only way content may change
        return f"{path}: 新增的不是版本檔（atelier/ 底下只放 v<N>.md 版本檔）"
    changed = {"M": "被修改（含模式變更）", "D": "被刪除", "R": "被改名",
               "T": "型別變更（不再是普通檔案）"}.get(status[0], f"有未預期的變化（{status}）")
    target = f"{old} → {path}" if old else path
    if parse_path(path) is not None:
        return f"{target}: 已發布的版本檔{changed}；內容有變要新增下一個版本檔（append-only）"
    return f"{target}: atelier/ 底下的檔案{changed}（append-only）"


def append_only_violations(repo: Path, base_rev: str) -> list[str]:
    """Everything under atelier/ that changed since `base_rev` in a forbidden way.

    Both the commits (`base_rev..HEAD`) and the live working tree are compared:
    a violation committed and then reverted in the working tree still counts.
    The only allowed change is an added new version file (`A` on `v<N>.md`);
    every M/D/R/T - and a bare mode change, which git reports as `M` - is a
    violation, so edits, deletions, renames, chmod and file/symlink swaps are
    all caught.
    """
    violations: dict[str, None] = {}
    for rev_args in (("HEAD",), ()):   # committed first, then the working tree
        for status, old, path in _changed_paths(repo, base_rev, *rev_args):
            message = _violation_for(status, old, path)
            if message is not None:
                violations.setdefault(message, None)
    return list(violations)


def _check_published_versions(repo: Path = REPO) -> None:
    base = append_only_base(repo)
    if base is None:
        pytest.skip(f"{repo} 沒有 origin/main（或沒有 merge-base）：append-only 沒被驗到，"
                    "這不是通過")
    violations = append_only_violations(repo, base)
    assert not violations, "\n".join(violations)


def test_published_version_files_are_append_only():
    _check_published_versions()


def test_without_an_origin_the_check_skips_with_a_reason(tmp_path):
    """Degradation must be a stated skip, not a silent pass (no origin/base)."""
    repo = tmp_path / "no-origin"
    repo.mkdir()
    _git(repo, "init", "-q")
    with pytest.raises(pytest.skip.Exception, match="origin/main"):
        _check_published_versions(repo)


def _fake_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose base commit holds v0.md, a non-version file and a README."""
    repo = tmp_path / "repo"
    (repo / "atelier/worker-team/pm").mkdir(parents=True)
    (repo / "atelier/worker-team/pm/v0.md").write_text("v0 的內容\n", encoding="utf-8")
    (repo / "atelier/worker-team/pm/notes.md").write_text("筆記\n", encoding="utf-8")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=atelier-test", "-c", "user.email=atelier-test@example.invalid",
         "add", "-A")
    _git(repo, "-c", "user.name=atelier-test", "-c", "user.email=atelier-test@example.invalid",
         "commit", "-q", "-m", "v0")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_append_only_flags_an_edited_version_file(tmp_path):
    repo, base = _fake_repo(tmp_path)
    (repo / "atelier/worker-team/pm/v0.md").write_text("v0 改過了\n", encoding="utf-8")
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "v0.md" in violations[0], violations


def test_append_only_flags_an_edit_that_was_committed_and_then_reverted(tmp_path):
    """The working tree may look clean again - the committed edit still counts (低3)."""
    repo, base = _fake_repo(tmp_path)
    version = repo / "atelier/worker-team/pm/v0.md"
    version.write_text("v0 改過又 commit 了\n", encoding="utf-8")
    _git(repo, "-c", "user.name=atelier-test", "-c", "user.email=atelier-test@example.invalid",
         "add", "-A")
    _git(repo, "-c", "user.name=atelier-test", "-c", "user.email=atelier-test@example.invalid",
         "commit", "-q", "-m", "edit")
    _git(repo, "checkout", base, "--", "atelier")   # working tree back to the published bytes
    assert _git(repo, "diff", "--name-status", base, "--", "atelier").stdout == ""
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "v0.md" in violations[0], violations


def test_append_only_flags_a_mode_change(tmp_path):
    """chmod is a change too: git reports it as M and it must be a violation (低4)."""
    repo, base = _fake_repo(tmp_path)
    os.chmod(repo / "atelier/worker-team/pm/v0.md", 0o755)
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "模式變更" in violations[0], violations


def test_append_only_flags_a_symlink_swap(tmp_path):
    """Replacing the file with a symlink of the same content is a type change (低4)."""
    repo, base = _fake_repo(tmp_path)
    pm = repo / "atelier/worker-team/pm"
    (pm / "v0.target").write_text("v0 的內容\n", encoding="utf-8")
    (pm / "v0.md").unlink()
    (pm / "v0.md").symlink_to("v0.target")
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "型別" in violations[0], violations


def test_append_only_flags_a_deleted_version_file(tmp_path):
    repo, base = _fake_repo(tmp_path)
    (repo / "atelier/worker-team/pm/v0.md").unlink()
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "刪除" in violations[0], violations


def test_append_only_accepts_a_new_version_file(tmp_path):
    repo, base = _fake_repo(tmp_path)
    (repo / "atelier/worker-team/pm/v1.md").write_text("v1 的內容\n", encoding="utf-8")
    assert append_only_violations(repo, base) == []


def test_append_only_flags_a_change_to_a_file_that_is_not_a_version_file(tmp_path):
    repo, base = _fake_repo(tmp_path)
    (repo / "atelier/worker-team/pm/notes.md").write_text("筆記改過\n", encoding="utf-8")
    violations = append_only_violations(repo, base)
    assert len(violations) == 1 and "notes.md" in violations[0], violations


def test_append_only_ignores_changes_outside_the_harness_tree(tmp_path):
    repo, base = _fake_repo(tmp_path)
    (repo / "README.md").write_text("README 改過\n", encoding="utf-8")
    assert append_only_violations(repo, base) == []

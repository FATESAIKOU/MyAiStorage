"""`pmo` 的文件要跟 CLI 對得上（#382）。

文件會走樣：`skills/pmo/SKILL.md` 的指令總覽、事件標籤與結束碼，以及
`docs/pmo/environment-note.template.md` 的標籤，都是人寫的。三支測試各守一塊：

1. **指令總覽 ↔ argparse**：`## 指令總覽` 的 ```bash 區塊每行第一個詞組
   （`pmo tick`、`pmo inbox read`、`pmo wake set`…）必須是 `cli.build_parser()`
   真的註冊的（子）命令，兩邊不得有一邊多；該行的 `--旗標` 與那個子命令的
   option strings 也要雙向一致。argparse 的 `_SubParsersAction` 與 option
   strings 是私有屬性，這裡包成小 helper，失敗訊息列出「哪一行多了／少了誰」。
2. **事件標籤 ↔ 原始碼**：文件用方括號寫的事件標籤（`EVENT_TAGS` 白名單）都要在
   `src/pmo/` 的字串常值出現相同字面（docstring 的敘述不算，那只會讓改名的標籤
   溜過去）。抽取刻意寫窄——只認反引號裡的 `` `[tag]` `` 與事件表
   第一欄——所以 `[ ]` 佔位、JSON 範例、`[[--json]]` 這類不會被誤抓；反過來，
   文件出現白名單以外的候選就失敗，逼人補白名單（新標籤才不會被默默跳過）。
   反方向（原始碼印了、文件沒寫）**不做斷言**，那是報告裡的發現。
3. **結束碼**：SKILL.md「結束碼」通則列的 0／1／2，與 `pmo.cli.main` 實際把三類
   錯誤對到哪裡一致（成功 0、用法錯 1、設定／狀態檔壞 2），通則 2 的另外兩個
   主要來源（`IdentityMismatch` 身分不符、狀態檔壞的 `SchemaError`）也各真的走
   一次 `main` 的 handler。逐指令的結束碼表不做機器比對（見 #382 報告：太脆弱）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import argparse
import ast
import io
import re
from pathlib import Path

from pmo import cli

import _pmo_env as E
from _pmo_env import pmo_env  # noqa: F401  (fixture)

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / "skills" / "pmo" / "SKILL.md"
NOTE_TEMPLATE = REPO / "docs" / "pmo" / "environment-note.template.md"
PKG_DIR = REPO / "src" / "pmo"

#: pmo 自己印的事件標籤（`src/pmo` 的 stdout／stderr 第一個 token）。
#: 這份白名單是照著文件實際有的標籤列的（#382）；文件多出新標籤時測試會紅，
#: 補進來之後第 2 支測試才會去原始碼驗字面。
EVENT_TAGS = (
    "ack",
    "decision",
    "dedupe",
    "group-handoff",
    "group-sweep-due",
    "idle",
    "inbox",
    "msg",
    "no-candidate",
    "open",
    "pmo",
    "poke",
    "report",
    "secretary",
    "send",
    "state",
    "waiting-quota",
    "wake",
    "wake-error",
    "wake-hint",
    "wake-missing",
    "warn",
    "watchdog",
    "worker-action",
    "要本人處理",
)

#: 一行總覽的指令註解的開頭：兩個以上的空白接 `#`（`<repo#n>` 的 `#` 前面沒有空白，
#: 不會被誤切）。
_COMMENT_RE = re.compile(r"\s{2,}#")

#: 一個（子）命令名。佔位符（`<team>`、`[--json]`）不合這個樣式，指令就到此為止。
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*\Z")

#: 一行的旗標；總覽把選填旗標寫成 `[--json]`，所以取 `--` 開頭的字就好。
_FLAG_RE = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")

#: 文件抽取：反引號裡的 `[tag]`，以及表格第一欄開頭的 `[tag]`。
_BACKTICK_TAG_RE = re.compile(r"`\[([^\]`\n]+)\]")
_CELL_TAG_RE = re.compile(r"^\|\s*`?\[([^\]`\n]+)\]")


# --------------------------------------------------------------------------- #
# argparse：列出「真的做事」的（子）命令與它的旗標
# --------------------------------------------------------------------------- #


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    """The parser's `_SubParsersAction`, or None when it is a leaf.

    The action lists the registered subcommands in `.choices`; it sits in
    `parser._actions` (argparse has no public accessor).
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _leaf_commands(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    """Every leaf command as `(path…) -> its parser`.

    Containers (`inbox`, `wake`) are not leaves: `pmo inbox` alone is argparse's
    "a subcommand is required" error, not something the overview may list.
    """
    sub = _subparsers(parser)
    if sub is None:
        return {prefix: parser}
    leaves: dict[tuple[str, ...], argparse.ArgumentParser] = {}
    for name, child in sub.choices.items():
        leaves.update(_leaf_commands(child, prefix + (name,)))
    return leaves


def _flags(parser: argparse.ArgumentParser) -> set[str]:
    """The leaf's option strings, `-h/--help` excluded (the overview never lists it)."""
    return {
        option
        for action in parser._actions
        if not isinstance(action, argparse._HelpAction)
        for option in action.option_strings
    }


# --------------------------------------------------------------------------- #
# SKILL.md 的指令總覽
# --------------------------------------------------------------------------- #


def _overview_block(text: str) -> str:
    """The ```bash block under `## 指令總覽`, without the fences."""
    section = text.index("## 指令總覽")
    start = text.index("```bash", section) + len("```bash")
    end = text.index("```", start)
    return text[start:end]


def _overview_rows(text: str) -> dict[tuple[str, ...], set[str]]:
    """`(path…) -> the flags written on that line`.

    A row is `pmo <name…> [<args>] # 中文說明`; the path is the leading bare
    names, so `pmo status [<team>] [--json]` is `("status",)`.

    One command, one row: a second row for the same path would silently replace
    the first one's flags (and the flags test would then miss a typo on the
    dropped line), so a duplicate is a failure, not a merge.
    """
    rows: dict[tuple[str, ...], set[str]] = {}
    for raw in _overview_block(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        body = _COMMENT_RE.split(line, maxsplit=1)[0].strip()
        tokens = body.split()
        assert tokens and tokens[0] == "pmo", f"指令總覽這一行不是 pmo 指令：{line!r}"
        path: tuple[str, ...] = ()
        for token in tokens[1:]:
            if not _NAME_RE.match(token) or len(path) >= 2:
                break
            path += (token,)
        assert path, f"指令總覽這一行沒有指令名：{line!r}"
        assert path not in rows, (
            "指令總覽有重複的指令路徑："
            f"pmo {' '.join(path)}（{line!r}）；同一條指令只該有一行，"
            "重複會讓前面那行的旗標被默默蓋掉"
        )
        rows[path] = set(_FLAG_RE.findall(body))
    return rows


def test_overview_lists_exactly_the_registered_commands():
    doc = _overview_rows(SKILL.read_text(encoding="utf-8"))
    registered = _leaf_commands(cli.build_parser())

    missing = ["pmo " + " ".join(path) for path in sorted(set(registered) - set(doc))]
    extra = ["pmo " + " ".join(path) for path in sorted(set(doc) - set(registered))]
    assert not missing and not extra, (
        "SKILL.md 的「## 指令總覽」與 cli.build_parser() 不一致："
        f"parser 有、總覽漏了 {missing}；總覽有、parser 沒有 {extra}"
    )


def test_overview_flags_match_the_registered_commands():
    doc = _overview_rows(SKILL.read_text(encoding="utf-8"))
    registered = _leaf_commands(cli.build_parser())

    problems = []
    for path in sorted(set(doc) & set(registered)):
        in_parser = _flags(registered[path])
        in_doc = doc[path]
        only_doc = sorted(in_doc - in_parser)
        only_parser = sorted(in_parser - in_doc)
        if only_doc or only_parser:
            problems.append(
                f"pmo {' '.join(path)}：總覽多寫了 {only_doc}、parser 有但總覽沒寫 {only_parser}"
            )
    assert not problems, "旗標與 argparse 不一致：\n" + "\n".join(problems)


# --------------------------------------------------------------------------- #
# 事件標籤
# --------------------------------------------------------------------------- #


def _documented_tags(text: str) -> set[str]:
    """Bracketed tags the doc presents as event tags (narrow on purpose)."""
    tags: set[str] = set()
    for line in text.splitlines():
        tags.update(match.group(1) for match in _BACKTICK_TAG_RE.finditer(line))
        cell = _CELL_TAG_RE.match(line.strip())
        if cell:
            tags.add(cell.group(1))
    return tags


def _docstring_strings(tree: ast.AST) -> set[int]:
    """`id()` of the `Constant` nodes that are docstrings (prose, not output)."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _printed_strings() -> list[tuple[str, str]]:
    """Every string literal in `src/pmo` that is not a docstring.

    Docstrings are excluded on purpose: a tag named only in prose is not
    something the code prints, and counting it would let a renamed event slip
    through (found by #382's break check).
    """
    strings: list[tuple[str, str]] = []
    for path in sorted(PKG_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_strings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                strings.append((path.name, node.value))
    return strings


def test_documented_event_tags_are_printed_by_the_source():
    documented: set[str] = set()
    for path in (SKILL, NOTE_TEMPLATE):
        documented |= _documented_tags(path.read_text(encoding="utf-8"))

    unknown = sorted(documented - set(EVENT_TAGS))
    assert not unknown, (
        "文件出現了白名單以外的方括號候選 "
        f"{unknown}；若是 pmo 新印的事件標籤，請補進 EVENT_TAGS 再跑一次"
    )

    # 反方向：文件刪掉標籤而白名單留著時，白名單的殘留項仍逼源碼要有字面
    # （誤紅，不會誤綠），所以白名單與文件要相等，兩邊差集一起列出。
    only_in_whitelist = sorted(set(EVENT_TAGS) - documented)
    assert not only_in_whitelist, (
        f"EVENT_TAGS 有、文件（SKILL.md 與範本）抽不到 {only_in_whitelist}；"
        "文件拿掉標籤時請同步刪白名單，或把標籤寫回文件"
    )

    strings = _printed_strings()
    missing = [
        tag for tag in EVENT_TAGS if not any(f"[{tag}]" in text for _, text in strings)
    ]
    assert not missing, (
        "文件寫了這些標籤、src/pmo 的字串裡找不到相同字面（docstring 不算）：" + str(missing)
    )


# --------------------------------------------------------------------------- #
# 結束碼
# --------------------------------------------------------------------------- #


def _documented_exit_codes(text: str) -> dict[int, str]:
    """The code -> one-line meaning bullets under `**結束碼**（…通則）：`."""
    marker = "**結束碼**（每個指令的細節見上；這裡是通則）："
    body = text[text.index(marker) + len(marker):]
    codes: dict[int, str] = {}
    for line in body.splitlines():
        if line.startswith("**"):  # the next bold heading
            break
        match = re.match(r"^- \*\*(\d)\*\*：(.*)$", line)
        if match:
            codes[int(match.group(1))] = match.group(2)
    return codes


def test_documented_exit_codes_are_the_ones_the_cli_returns():
    codes = _documented_exit_codes(SKILL.read_text(encoding="utf-8"))
    assert sorted(codes) == [0, 1, 2], f"結束碼通則只該列 0／1／2，實際是 {sorted(codes)}"
    assert "用法錯" in codes[1]
    assert "設定" in codes[2] and "狀態檔" in codes[2]

    # 三個碼各走一次真的入口（不碰網路：parse 失敗與缺環境都在接上 deps 之前）。
    out, err = io.StringIO(), io.StringIO()
    assert cli.main(["tick", "--help"], stdout=out, stderr=err) == 0  # 成功
    assert cli.main(["tick", "--no-such-flag"], stdout=out, stderr=err) == 1  # 用法錯
    # `isolated_home` 清掉了 PMO_SKILL_CONFIG／PMO_SKILL_STATE_DIR：設定讀不到是 2
    assert cli.main(["tick"], stdout=out, stderr=err) == 2


def test_exit_two_also_covers_a_bad_identity_and_a_damaged_state_file(pmo_env):
    """通則 2 的另兩個主要來源：都真的走到 `main` 的 handler 才算數。

    上面的測試只碰得到「設定讀不到」那一條；身分守門（`IdentityMismatch`）與
    狀態檔壞（`SchemaError`）要在有設定、有狀態檔的環境才走得到，兩個都走
    `pmo.cli.main`，斷言它對到 2（#382 審查中-1：把 handler 的 `return 2` 改成
    `return 1` 時這裡要紅）。
    """
    # 身分不符：設定裡的 PMO 是 `demo-pm-m1`，這次跑成另一個合法 AI 名。
    # `inbox report` 先守門再做事，拒送前不會碰到網路或狀態檔。
    rc, out, err = E.run_cli(
        ["inbox", "report", E.ULID_1, "--status", "已完成"],
        pmo_env,
        identity=E.OTHER_IDENTITY_WRONG,
    )
    assert rc == 2
    assert "身分不符拒送" in err and E.OTHER_IDENTITY_WRONG in err
    assert out == ""

    # 狀態檔壞：帳本不是合法 JSON，`inbox list` 讀不起來（`SchemaError`），
    # 由 `main` 印 `[pmo] …` 並回 2。
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.inbox_file().write_text("{not json", encoding="utf-8")
    rc, out, err = E.run_cli(["inbox", "list"], pmo_env)
    assert rc == 2
    assert err.startswith("[pmo] ") and "not valid JSON" in err
    assert out == ""

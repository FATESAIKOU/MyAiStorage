"""`atelier`: read and install the harness tree; exit 0/1/2 (design D7/D10).

`ls` lists ids and versions, `show` prints a harness (whole file byte-for-byte,
one section, or JSON), `install` writes it into the user-level instruction files
of the named agents and `render` writes the instruction file of a single agent.
Messages never carry harness content.
"""

from __future__ import annotations

import argparse
import json
import sys

from atelier.atomic import WriteError
from atelier.harness import (
    HarnessFormatError,
    InvalidId,
    SECTION_NAMES,
    frontmatter,
    parse_id,
    split_sections,
    validate_harness,
)
from atelier.install import (
    InstallError,
    PolicyError,
    check_install_policy,
    claude_target,
    install_claude,
    install_opencode,
    opencode_target,
)
from atelier.render import AppendMissing, compose, read_append, write_instructions
from atelier.repo import HarnessNotFound, Repo, RepoError

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_DATA = 2
EXIT_NOT_FOUND = 3


class UsageError(Exception):
    """A usage error: exit 1, like angareion's ArgumentParser contract."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise UsageError(f"{self.prog}: error: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="atelier", description="讀 Atelier harness 樹，或交給 agent："
                                                "install 寫使用者層級指示檔，render 寫單一個 agent 的指示檔。")
    sub = parser.add_subparsers(dest="command")
    ls = sub.add_parser("ls", help="列出職務與版本")
    ls.add_argument("--json", action="store_true", help="機器的輸出")
    show = sub.add_parser("show", help="印一份 harness（預設整份原樣）")
    show.add_argument("id", help="atelier:<team>-<role>[@v<N>]")
    show.add_argument("--section", choices=SECTION_NAMES, help="只印這一章")
    show.add_argument("--json", action="store_true", help="機器的輸出")
    install = sub.add_parser("install", help="把 harness 裝進 agent 的使用者層級指示檔")
    install.add_argument("id", help="atelier:<team>-<role>[@v<N>]")
    install.add_argument("--agent", action="append", choices=("claude", "opencode"),
                         default=[], metavar="claude|opencode",
                         help="可以給多次；每個值各裝一次（同值去重）")
    install.add_argument("--global", dest="global_install", action="store_true",
                         help="允許安裝 v>=1 的 PM harness 至使用者層級（整台機器只有一個 agent 時才該使用）")
    render = sub.add_parser("render", help="產生單一個 agent 的指示檔（harness ＋環境說明）")
    render.add_argument("id", help="atelier:<team>-<role>[@v<N>]")
    render.add_argument("--append", dest="append_file", metavar="檔",
                        help="接在 harness 後面的環境說明（要存在）")
    render.add_argument("--out", required=True, metavar="檔",
                        help="寫到哪裡；唯一寫入的檔案（不給＝用法錯）")
    return parser


def _agents(values: list[str]) -> list[str]:
    """Each distinct agent once, in a fixed order (order-independent outcome)."""
    chosen = set(values)
    return [agent for agent in ("claude", "opencode") if agent in chosen]


def _json_key(key: object) -> str:
    """YAML mapping keys are objects: dates, numbers, even bytes. JSON wants strings."""
    if isinstance(key, str):
        return key
    if isinstance(key, bytes):
        return key.decode("utf-8", "replace")
    return str(key)


def _json_safe(value: object) -> object:
    """A JSON-ready copy of a YAML-loaded structure.

    Frontmatter is YAML, so values and keys can be `datetime.date`, `bytes` or
    `set` (`history: {2026-10-16: created}`, `tags: !!set {...}`): keys become
    strings, sets become sorted lists, bytes decode, everything else falls back
    to `str` - `json.dumps` then never meets an object it cannot print.
    """
    if isinstance(value, dict):
        return {_json_key(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=str)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_print(data: object) -> None:
    print(json.dumps(_json_safe(data), ensure_ascii=False, indent=2, sort_keys=False))


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RepoError(f"harness 檔不是 UTF-8：{e}") from e


def _ident_row(team: str, role: str, version: int) -> dict:
    return {"id": f"atelier:{team}-{role}@v{version}", "team": team, "role": role,
            "version": f"v{version}"}


def _entries(repo: Repo) -> list[tuple[str, str, int]]:
    entries = sorted(repo.entries())
    if not entries:
        repo.refresh_for_lookup()
        entries = sorted(repo.entries())
    return entries


def _cmd_ls(repo: Repo, args: argparse.Namespace) -> int:
    repo.ensure_fresh()
    entries = _entries(repo)
    if args.json:
        _json_print([_ident_row(team, role, version) for team, role, version in entries])
        return EXIT_OK
    for team, role, version in entries:
        print(f"atelier:{team}-{role}@v{version}")
    return EXIT_OK


def _cmd_show(repo: Repo, args: argparse.Namespace) -> int:
    ident = parse_id(args.id)   # a pure syntax check comes before any fetch (L2)
    repo.ensure_fresh()
    resolved = repo.resolve(ident)
    if args.section is not None:
        content = split_sections(_decode(resolved.raw)).get(args.section)
        if content is None:
            raise HarnessNotFound(f"找不到章節：{args.section}")
        if args.json:
            row = _ident_row(resolved.ident.team, resolved.ident.role, resolved.ident.version)
            row.update({"section": args.section, "content": content})
            _json_print(row)
        elif content:
            sys.stdout.write(content + "\n")
        return EXIT_OK
    if args.json:
        text = _decode(resolved.raw)
        try:
            front = frontmatter(text)
        except HarnessFormatError as e:
            raise RepoError(f"{resolved.path} 讀不了：{e}") from e
        row = _ident_row(resolved.ident.team, resolved.ident.role, resolved.ident.version)
        row.update({"frontmatter": front, "sections": split_sections(text)})
        _json_print(row)
        return EXIT_OK
    # The whole file, byte for byte (spec「讀整份」).
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(resolved.raw)
        buffer.flush()
    else:   # an embedded caller may hand us a text stream without a buffer (L11)
        sys.stdout.write(_decode(resolved.raw))
        sys.stdout.flush()
    return EXIT_OK


def _cmd_install(repo: Repo, args: argparse.Namespace) -> int:
    if not args.agent:
        raise UsageError("install 需要至少一個 --agent claude|opencode")
    ident = parse_id(args.id)   # a pure syntax check comes before any fetch (L2)
    check_install_policy(ident, global_install=args.global_install)
    repo.ensure_fresh()
    resolved = repo.resolve(ident)
    check_install_policy(resolved.ident, global_install=args.global_install)
    text = _decode(resolved.raw)
    validate_harness(text, resolved.path)
    if args.global_install and resolved.ident.role == "pm" and (resolved.ident.version is None or resolved.ident.version >= 1):
        print("Warning: 同一個使用者的每個 agent 都會拿到這份 PM harness", file=sys.stderr)
    for agent in _agents(args.agent):
        if agent == "claude":
            target = claude_target(resolved.ident)
            install_claude(target, resolved.raw)
        else:
            target = opencode_target(resolved.ident)
            install_opencode(target, resolved.ident, text)
        print(f"installed {resolved.ident} -> {target}")
    return EXIT_OK


def _cmd_render(repo: Repo, args: argparse.Namespace) -> int:
    if not args.out:
        raise UsageError("--out 不能為空字串")
    ident = parse_id(args.id)   # a pure syntax check comes before any fetch (L2)
    # The note is read before the copy is touched: a request that already names a
    # file that is not there must not make atelier fetch for it (exit 1, no fetch).
    note = read_append(args.append_file) if args.append_file is not None else None
    repo.ensure_fresh()
    resolved = repo.resolve(ident)
    text = _decode(resolved.raw)
    validate_harness(text, resolved.path)
    target = write_instructions(args.out, compose(resolved.raw, note))
    print(f"rendered {resolved.ident} -> {target}")
    return EXIT_OK


def main(argv: list[str] | None = None, *, repo: Repo | None = None) -> int:
    """The `atelier` entry point; `repo` is a test seam for the reading layer."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command is None:
            parser.print_help(sys.stderr)
            return EXIT_USAGE
        active = repo if repo is not None else Repo.from_env()
        if args.command == "ls":
            return _cmd_ls(active, args)
        if args.command == "show":
            return _cmd_show(active, args)
        if args.command == "render":
            try:
                return _cmd_render(active, args)
            except HarnessNotFound as e:
                print(f"Error: {e}", file=sys.stderr)
                return EXIT_NOT_FOUND
        return _cmd_install(active, args)
    except (UsageError, InvalidId, HarnessNotFound, AppendMissing, PolicyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (RepoError, InstallError, WriteError, HarnessFormatError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_DATA


if __name__ == "__main__":
    sys.exit(main())

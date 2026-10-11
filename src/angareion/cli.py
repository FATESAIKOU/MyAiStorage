"""Command line interface: the four commands, exit codes 0/1/2/3, stdout vs stderr (D1)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Sequence

from angareion.backend import (
    BackendError,
    ChannelBackend,
    ChannelNotFoundError,
    Target,
)
from angareion.client import POLL_INTERVAL_SECONDS, Angareion
from angareion.config import (
    Config,
    ConfigError,
    TokenError,
    load_config,
    load_token,
    mask_token,
)
from angareion.github import HttpTransport, GitHubIssuesBackend
from angareion.messages import (
    Message,
    MessageValidationError,
    is_group_address,
    message_to_dict,
    validate_address,
)
from angareion.state import CursorStore

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2
EXIT_TIMEOUT = 3


class UsageError(Exception):
    """A usage error, so bad arguments exit 1 like the rest of the input errors."""


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; Angareion's contract is 1."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise UsageError(f"{self.prog}: error: {message}")


def parse_address(addr_str: str) -> dict[str, Any]:
    """Parse an address string into a dictionary.

    Supports:
    - JSON dictionary: '{"repo": "...", "role": "..."}' or '{"group": "..."}'
    - Key-value pairs: 'repo=...,role=...,name=...' or 'group=...'
    - Cross-format: 'repo×role'
    - Colon-format: 'repo:role'
    """
    addr_str = addr_str.strip()
    if not addr_str:
        raise MessageValidationError("Address string cannot be empty")

    if addr_str.startswith("{") and addr_str.endswith("}"):
        try:
            val = json.loads(addr_str)
            if isinstance(val, dict):
                validate_address(val)
                return val
        except json.JSONDecodeError as e:
            raise MessageValidationError(f"Invalid JSON in address: {e}") from e

    if "group=" in addr_str or "repo=" in addr_str or "role=" in addr_str:
        parts = [p.strip() for p in addr_str.split(",") if p.strip()]
        res: dict[str, Any] = {}
        for p in parts:
            if "=" in p:
                k, v = p.split("=", 1)
                res[k.strip()] = v.strip()
            else:
                raise MessageValidationError(f"Invalid key=value pair in address: {p}")
        validate_address(res)
        return res

    if "×" in addr_str:
        repo, role = addr_str.split("×", 1)
        res = {"repo": repo.strip(), "role": role.strip()}
        validate_address(res)
        return res

    if ":" in addr_str and not addr_str.startswith("http:") and not addr_str.startswith("https:"):
        repo, role = addr_str.split(":", 1)
        res = {"repo": repo.strip(), "role": role.strip()}
        validate_address(res)
        return res

    raise MessageValidationError(
        f"Unrecognized address format: '{addr_str}'. Expected 'repo=...,role=...' or JSON object"
    )


def _target_from_args(args: argparse.Namespace, config: Config) -> Target:
    if args.channel:
        return Target(channel=args.channel)
    return Target(recipient=config.identity)


def _format_message(message: Message) -> str:
    sender = message.from_
    sender_str = f"{sender.get('repo')}×{sender.get('role')}"
    if sender.get("name"):
        sender_str += f" ({sender['name']})"
    lines = [f"[{message.urgency}] {message.id} {message.at} <{sender_str}> channel: {message.channel}"]
    body = message.content.rstrip("\n")
    if body:
        lines.extend("  " + line for line in body.splitlines())
    if message.attachments:
        kinds = [f"{att.get('name')} ({'ref' if 'ref' in att else 'inline'})" for att in message.attachments]
        lines.append("  attachments: " + ", ".join(kinds))
    return "\n".join(lines) + "\n"


def _build_attachments(args: argparse.Namespace) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for att_item in args.attach or []:
        if "=" not in att_item:
            raise MessageValidationError(f"--attach must be name=path, got '{att_item}'")
        name, file_path_str = att_item.split("=", 1)
        file_path = Path(os.path.expanduser(file_path_str.strip()))
        if not file_path.is_file():
            raise MessageValidationError(f"attachment file not found: {file_path}")
        try:
            content_text = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raise MessageValidationError(f"cannot read attachment file {file_path}: {e}") from e
        attachments.append({"name": name.strip(), "inline": content_text})

    for ref_item in args.ref or []:
        if "=" not in ref_item:
            raise MessageValidationError(f"--ref must be name=locator, got '{ref_item}'")
        name, locator = ref_item.split("=", 1)
        attachments.append({"name": name.strip(), "ref": locator.strip()})
    return attachments


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None and args.body != "-":
        return args.body
    if args.body == "-" or not sys.stdin.isatty():
        return sys.stdin.read()
    raise MessageValidationError("no message body: pass --body <text> or pipe the text on stdin")


def cmd_channel_create(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    if not args.name or not args.name.strip():
        sys.stderr.write("Error: channel name must be a non-empty string.\n")
        return EXIT_USAGE

    members: list[dict[str, Any]] = []
    for member_str in args.member or []:
        try:
            member = parse_address(member_str)
        except MessageValidationError as e:
            sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
            return EXIT_USAGE
        if is_group_address(member):
            sys.stderr.write("Error: group addressing is not supported in v0.\n")
            return EXIT_USAGE
        members.append(member)

    channel = app.create_channel(args.name, members)
    sys.stdout.write(f"{channel.name} #{channel.number}\n")
    return EXIT_OK


def cmd_send(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    try:
        to_addr = parse_address(args.to)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    if is_group_address(to_addr):
        sys.stderr.write("Error: group addressing is not supported in v0.\n")
        return EXIT_USAGE

    if not (0 <= args.urgency <= 9):
        sys.stderr.write(f"Error: urgency must be between 0 and 9, got {args.urgency}\n")
        return EXIT_USAGE

    try:
        attachments = _build_attachments(args)
        body = _read_body(args)
        message = Message(
            from_=config.identity,
            to=to_addr,
            channel=args.channel,
            urgency=args.urgency,
            content=body,
            attachments=attachments,
        )
        # Validate everything before touching the backend: a bad message sends nothing.
        message.validate()
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    message_id = app.send(args.channel, message)
    sys.stdout.write(f"{message_id}\n")
    return EXIT_OK


def cmd_wait(
    args: argparse.Namespace,
    config: Config,
    app: Angareion,
    token: str,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    if args.timeout is not None and args.timeout < 0:
        sys.stderr.write(f"Error: --timeout must not be negative, got {args.timeout}\n")
        return EXIT_USAGE

    target = _target_from_args(args, config)
    found = app.wait(target, args.timeout, poll_interval=POLL_INTERVAL_SECONDS, sleeper=sleeper, clock=clock)
    if found:
        return EXIT_OK
    sys.stderr.write("Timed out with no new messages.\n")
    return EXIT_TIMEOUT


def cmd_read(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    target = _target_from_args(args, config)
    batch = app.read(target, since=args.since)

    if args.json:
        sys.stdout.write(json.dumps([message_to_dict(m) for m in batch.messages], indent=2, ensure_ascii=False) + "\n")
    else:
        for message in batch.messages:
            sys.stdout.write(_format_message(message))
    sys.stdout.flush()
    # Only now that every message reached stdout may the cursor move (L2):
    # a closed pipe (| head) must not swallow the messages for the next read.
    batch.commit()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for `angareion`."""
    parser = _Parser(
        prog="angareion",
        description="Angareion: leave and read messages on a channel (GitHub Issues or a fake backend).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_channel = subparsers.add_parser("channel", help="Channel operations.")
    channel_sub = p_channel.add_subparsers(dest="channel_action", metavar="ACTION")
    p_create = channel_sub.add_parser("create", help="Create a channel (idempotent); prints '<name> #<number>'.")
    p_create.add_argument("name", help="Channel name.")
    p_create.add_argument("--member", action="append", default=[], metavar="ADDRESS",
                          help="Member address (repeatable).")

    p_send = subparsers.add_parser("send", help="Send a message to a channel; prints the message id.")
    p_send.add_argument("--channel", required=True, help="Channel name.")
    p_send.add_argument("--to", required=True, help="Recipient address ({repo, role, [name]}).")
    p_send.add_argument("--urgency", type=int, default=5, help="Urgency 0-9 (default: 5).")
    p_send.add_argument("--attach", action="append", default=[], metavar="NAME=PATH",
                        help="Attach a file inline (repeatable).")
    p_send.add_argument("--ref", action="append", default=[], metavar="NAME=LOCATOR",
                        help="Attach a reference or link (repeatable).")
    p_send.add_argument("--body", metavar="TEXT",
                        help="Message text; '-' or omitted with piped stdin reads stdin.")

    p_wait = subparsers.add_parser("wait", help="Wait for new messages: exit 0, or 3 on timeout.")
    p_wait.add_argument("--channel", help="Channel to watch; default: everything addressed to me.")
    p_wait.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="Wait at most this long; 0 checks once; omit to wait forever.")

    p_read = subparsers.add_parser("read", help="Read new messages, oldest first.")
    p_read.add_argument("--channel", help="Channel to read; default: everything addressed to me.")
    p_read.add_argument("--since", metavar="TIMESTAMP",
                        help="Read after this time; does not move the local cursor.")
    p_read.add_argument("--json", action="store_true", help="Print a JSON array.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    backend: ChannelBackend | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    transport: HttpTransport | None = None,
) -> int:
    """Entrypoint for the `angareion` command; returns the exit code.

    `backend` and `transport` are test seams: a fake backend, or a fake HTTP
    transport for the real GitHubIssuesBackend, so tests never touch the
    network. `sleeper` and `clock` make `wait` testable without real waiting.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_USAGE

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "channel" and args.channel_action is None:
        sys.stderr.write("Error: channel needs an action; try 'angareion channel create <name>'.\n")
        return EXIT_USAGE

    try:
        config = load_config()
    except ConfigError as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_BACKEND

    # Load the token where possible, only to mask it out of any later message.
    token_for_masking = ""
    try:
        token_for_masking = load_token(config)
    except TokenError:
        pass

    try:
        active_backend = backend if backend is not None else GitHubIssuesBackend.from_config(config, transport=transport)
    except (TokenError, ConfigError, BackendError) as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token_for_masking)}\n")
        return EXIT_BACKEND

    app = Angareion(active_backend, CursorStore(config.state_dir, config.identity))
    active_sleeper = sleeper or time.sleep
    active_clock = clock or time.monotonic

    try:
        if args.command == "channel":
            return cmd_channel_create(args, config, app, token_for_masking)
        if args.command == "send":
            return cmd_send(args, config, app, token_for_masking)
        if args.command == "wait":
            return cmd_wait(args, config, app, token_for_masking, active_sleeper, active_clock)
        if args.command == "read":
            return cmd_read(args, config, app, token_for_masking)
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    except ChannelNotFoundError as e:
        # A missing channel is an input error, not a backend failure.
        sys.stderr.write(f"Error: {mask_token(str(e), token_for_masking)}\n")
        return EXIT_USAGE
    except MessageValidationError as e:
        # A parse-level ValueError from a backend is converted to BackendError
        # inside the backend (L1); only input validation exits 1 here.
        sys.stderr.write(f"Error: {mask_token(str(e), token_for_masking)}\n")
        return EXIT_USAGE
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token_for_masking)}\n")
        return EXIT_BACKEND
    except BrokenPipeError:
        # stdout (a pipe like `| head`) closed before the messages were out.
        # The cursor was not advanced; keep the interpreter from complaining
        # about the broken pipe on shutdown.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except Exception:
            pass
        sys.stderr.write("Error: stdout was closed before the messages were written; the cursor was not advanced.\n")
        return EXIT_USAGE
    except Exception as e:
        sys.stderr.write(f"Error: {mask_token(f'Unexpected error: {e}', token_for_masking)}\n")
        return EXIT_BACKEND


if __name__ == "__main__":
    sys.exit(main())

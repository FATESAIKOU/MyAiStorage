"""Command line interface: the five commands, exit codes 0/1/2/3, stdout vs stderr (D1/v5)."""

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
    BackendRef,
    ChannelBackend,
    ChannelNotFoundError,
    EntityResolver,
    Target,
    parse_backend_time,
)
from angareion.channel_config import (
    BackendChoiceError,
    ChannelConfigError,
    ChannelStore,
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
    message_to_dict,
    validate_address,
    validate_channel_name,
)
from angareion.registry import (
    CommentPoster,
    RegistryClient,
    RegistryError,
    post_with_retries,
    register_comment,
    unregister_comment,
)
from angareion.state import CursorStore

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2
EXIT_TIMEOUT = 3

# #52: a soft reminder only - no code checks or blocks any message. Channel
# messages live on GitHub, so the help, the skill and the docs all say the
# same thing: keep private information out of messages.
_PRIVACY_REMINDER = (
    "私人資訊提醒：channel 的訊息存在 GitHub（channel 對應的 repo）上，\n"
    "AI 之間的對話無法完全避免被看見；請不要在訊息裡放私人資訊。\n"
    "需要傳遞私人資訊時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。"
)
_PRIVACY_REMINDER_SHORT = "不要在訊息裡放私人資訊（改用 MyBrain 路徑或案件 id）。"


class UsageError(Exception):
    """A usage error, so bad arguments exit 1 like the rest of the input errors."""


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; Angareion's contract is 1."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise UsageError(f"{self.prog}: error: {message}")


def parse_address(addr_str: str) -> str:
    """Validate one address and return it as an agent name (the v5 grammar).

    v5 has one address shape: `<repo 名>-<pm|arch|impl|test>[-<id>]`. The old
    `repo=…`, `×`, `:` and JSON shapes are gone (and are format errors now).
    """
    if not isinstance(addr_str, str) or not addr_str.strip():
        raise MessageValidationError("Address string cannot be empty")
    address = addr_str.strip()
    validate_address(address)
    return address


def _target_from_args(args: argparse.Namespace, config: Config) -> Target:
    if args.channel:
        return Target(channel=args.channel)
    return Target(recipient=config.identity)


def _format_message(message: Message) -> str:
    header = f"[{message.urgency}] {message.id} {message.at}"
    received_at = getattr(message, "received_at", None)
    if received_at:
        header += f" (received {received_at})"
    header += f" <{message.from_}> channel: {message.channel}"
    lines = [header]
    body = message.content.rstrip("\n")
    if body:
        lines.extend("  " + line for line in body.splitlines())
    if message.attachments:
        kinds = [f"{att.get('name')} ({'ref' if 'ref' in att else 'inline'})" for att in message.attachments]
        lines.append("  attachments: " + ", ".join(kinds))
    return "\n".join(lines) + "\n"


def _message_for_output(message: Message) -> dict[str, Any]:
    """The message as a dict for `--json`, plus the backend's received_at (D1)."""
    data = message_to_dict(message)
    data["received_at"] = getattr(message, "received_at", None)
    return data


def _report_old_cursors(app: Angareion) -> None:
    """Say once per run when an old-format ({at, id}) cursor was dropped (D4)."""
    for key in sorted(app.cursors.dropped_old_format):
        sys.stderr.write(
            f"Notice: cursor for '{key}' was in the old (at, id) format and cannot be translated; "
            "reading that target from the start. The file is rewritten in the new format.\n"
        )


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
    # L5: only --body TEXT or an explicit `--body -` reads stdin. Piped stdin
    # that nobody wrote to (an agent's child process) must not swallow the run.
    if args.body is None:
        raise MessageValidationError(
            "no message body: pass --body <text>, or --body - to read the text on stdin"
        )
    if args.body == "-":
        return sys.stdin.read()
    return args.body


def cmd_channel_create(
    args: argparse.Namespace,
    config: Config,
    app: Angareion,
    registry: CommentPoster,
    store: ChannelStore,
    token: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """The three-step create (design section 6).

    1. open the backend entity; 2. verify it, then post a register comment on
    the registry issue; 3. wait (bounded) for the rebuild to land the mapping.
    """
    try:
        validate_channel_name(args.name)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    store.ensure_fresh()
    if args.name in store.channels():
        sys.stderr.write(
            f"Error: channel '{args.name}' already exists; use `angareion channel delete` first to replace it.\n"
        )
        return EXIT_USAGE

    try:
        backend_ref = store.choose_backend_for_create(args.backend)
    except BackendChoiceError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE
    except ChannelConfigError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_BACKEND

    # Step 1: open the entity in the backend.
    try:
        entity = app.create_channel(args.name, backend_ref)
    except BackendError:
        sys.stderr.write(f"Error: channel '{args.name}' was not created.\n")
        return EXIT_BACKEND

    # Step 2: the client verifies the entity (exists and its title is the
    # channel name) before anything is registered (review H2).
    if not app.verify_channel(args.name, backend_ref, entity):
        sys.stderr.write(
            f"Error: channel '{args.name}' could not be verified in the backend; nothing was registered.\n"
        )
        return EXIT_BACKEND

    body = register_comment(args.name, backend_ref.name, entity)
    try:
        post_with_retries(registry, body, sleeper=sleeper)
    except RegistryError:
        sys.stderr.write(
            f"Error: channel '{args.name}' was not created: the registration could not be posted.\n"
        )
        return EXIT_BACKEND

    # Step 3: wait for the mapping to land (bounded); a timeout is not an error.
    if not store.poll_for_channel(args.name):
        sys.stderr.write(
            f"Notice: channel '{args.name}' is registered but has not shown up in the local copy yet; "
            "it will be usable once the registry rebuild lands.\n"
        )
    sys.stdout.write(f"{args.name}\n")
    return EXIT_OK


def cmd_channel_delete(
    args: argparse.Namespace,
    config: Config,
    app: Angareion,
    registry: CommentPoster,
    store: ChannelStore,
    token: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Delete a channel: close the entity (keep the record), then unregister (design section 7)."""
    try:
        validate_channel_name(args.name)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    store.ensure_fresh()
    entity = store.entity_for(args.name)  # a missing channel raises ChannelNotFoundError

    try:
        app.close_channel(entity)
    except BackendError:
        sys.stderr.write(f"Error: channel '{args.name}' was not deleted: the backend entity could not be closed.\n")
        return EXIT_BACKEND

    body = unregister_comment(args.name, entity.backend, entity.issue)
    try:
        post_with_retries(registry, body, sleeper=sleeper)
    except RegistryError:
        sys.stderr.write(
            f"Error: channel '{args.name}' was closed but the unregistration could not be posted.\n"
        )
        return EXIT_BACKEND

    sys.stdout.write(f"{args.name}\n")
    return EXIT_OK


def cmd_send(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    to_addr: str | None = None
    if args.to is not None:
        try:
            to_addr = parse_address(args.to)
        except MessageValidationError as e:
            sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
            return EXIT_USAGE

    if not (0 <= args.urgency <= 9):
        sys.stderr.write(f"Error: urgency must be between 0 and 9, got {args.urgency}\n")
        return EXIT_USAGE

    try:
        validate_channel_name(args.channel)
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

    app.store.ensure_fresh()
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

    # The pull happens once, before the loop: wait must not fetch while polling.
    app.store.ensure_fresh()
    target = _target_from_args(args, config)
    found = app.wait(target, args.timeout, poll_interval=POLL_INTERVAL_SECONDS, sleeper=sleeper, clock=clock)
    _report_old_cursors(app)
    if found:
        return EXIT_OK
    sys.stderr.write("Timed out with no new messages.\n")
    return EXIT_TIMEOUT


def cmd_read(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    if args.since is not None:
        # L-1: a bad --since is an input error (exit 1) with a format hint, not
        # a backend surprise later on.
        try:
            parse_backend_time(args.since)
        except ValueError:
            sys.stderr.write(
                "Error: --since must be an RFC 3339 timestamp such as "
                f"2026-10-12T03:00:00Z, got '{args.since}'\n"
            )
            return EXIT_USAGE

    target = _target_from_args(args, config)
    app.store.ensure_fresh()
    batch = app.read(target, since=args.since)

    if args.json:
        sys.stdout.write(json.dumps([_message_for_output(m) for m in batch.messages], indent=2, ensure_ascii=False) + "\n")
    else:
        for message in batch.messages:
            sys.stdout.write(_format_message(message))
    sys.stdout.flush()
    # Only now that every message reached stdout may the cursor move (L2):
    # a closed pipe (| head) must not swallow the messages for the next read.
    batch.commit()
    _report_old_cursors(app)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for `angareion`."""
    parser = _Parser(
        prog="angareion",
        description="Angareion: leave and read messages on a channel (GitHub Issues or a fake backend).",
        # The reminder gets its own paragraph after the options; Raw... keeps
        # the explicit line breaks instead of gluing it to the English text.
        epilog=_PRIVACY_REMINDER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_channel = subparsers.add_parser("channel", help="Channel operations.")
    channel_sub = p_channel.add_subparsers(dest="channel_action", metavar="ACTION")
    p_create = channel_sub.add_parser("create", help="Create a channel; prints the channel name.")
    p_create.add_argument("name", help="Channel name.")
    p_create.add_argument("--backend", metavar="NAME",
                          help="Which backend to open the channel in (required when several are configured).")
    p_delete = channel_sub.add_parser("delete", help="Delete a channel; prints the channel name.")
    p_delete.add_argument("name", help="Channel name.")

    p_send = subparsers.add_parser(
        "send",
        help="Send a message to a channel; prints the message id. " + _PRIVACY_REMINDER_SHORT,
    )
    p_send.add_argument("--channel", required=True, help="Channel name.")
    p_send.add_argument("--to", metavar="AGENT",
                        help="Recipient agent name (e.g. MyLinuxPool-impl-2); omit to send to the whole channel.")
    p_send.add_argument("--urgency", type=int, default=5, help="Urgency 0-9 (default: 5).")
    p_send.add_argument("--attach", action="append", default=[], metavar="NAME=PATH",
                        help="Attach a file inline (repeatable).")
    p_send.add_argument("--ref", action="append", default=[], metavar="NAME=LOCATOR",
                        help="Attach a reference or link (repeatable).")
    p_send.add_argument("--body", metavar="TEXT",
                        help="Message text; '-' reads the text on stdin. Required.")

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
    store: EntityResolver | None = None,
    registry: CommentPoster | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    transport: HttpTransport | None = None,
) -> int:
    """Entrypoint for the `angareion` command; returns the exit code.

    `backend`, `store`, `registry` and `transport` are test seams: a fake
    backend, a fake config store, a fake registry issue, or a fake HTTP
    transport for the real GitHub clients, so tests never touch the network.
    `sleeper` and `clock` make `wait` and the create retry testable without
    real waiting.
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
        sys.stderr.write(
            "Error: channel needs an action; try 'angareion channel create <name>' "
            "or 'angareion channel delete <name>'.\n"
        )
        return EXIT_USAGE

    try:
        config = load_config()
    except ConfigError as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_BACKEND

    # Load the token where possible (also to mask it out of later messages).
    token = ""
    try:
        token = load_token(config)
    except TokenError as e:
        # With fakes for both the backend and the registry no token is needed.
        if backend is None or registry is None:
            sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
            return EXIT_BACKEND

    try:
        active_store = store if store is not None else ChannelStore(
            config.config_dir, interval=config.config_pull_interval
        )
        active_backend = backend if backend is not None else GitHubIssuesBackend(
            active_store, token=token, transport=transport
        )
        active_registry = registry if registry is not None else RegistryClient(
            config.registry_repo, config.registry_issue, token=token, transport=transport
        )
    except (TokenError, ConfigError, BackendError, RegistryError) as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_BACKEND

    app = Angareion(active_backend, CursorStore(config.state_dir, config.identity), active_store, config.identity)
    active_sleeper = sleeper or time.sleep
    active_clock = clock or time.monotonic

    try:
        if args.command == "channel":
            if args.channel_action == "create":
                return cmd_channel_create(
                    args, config, app, active_registry, active_store, token, sleeper=active_sleeper
                )
            if args.channel_action == "delete":
                return cmd_channel_delete(args, config, app, active_registry, active_store, token, sleeper=active_sleeper)
            parser.print_help(sys.stderr)
            return EXIT_USAGE
        if args.command == "send":
            return cmd_send(args, config, app, token)
        if args.command == "wait":
            return cmd_wait(args, config, app, token, active_sleeper, active_clock)
        if args.command == "read":
            return cmd_read(args, config, app, token)
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    except ChannelNotFoundError as e:
        # A missing channel is an input error, not a backend failure.
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE
    except BackendChoiceError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE
    except MessageValidationError as e:
        # A parse-level ValueError from a backend is converted to BackendError
        # inside the backend (L1); only input validation exits 1 here.
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE
    except (ChannelConfigError, RegistryError) as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_BACKEND
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
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
        sys.stderr.write(f"Error: {mask_token(f'Unexpected error: {e}', token)}\n")
        return EXIT_BACKEND


if __name__ == "__main__":
    sys.exit(main())

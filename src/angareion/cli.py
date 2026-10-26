"""Command line interface: the five commands, exit codes 0/1/2/3, stdout vs stderr (D1/v5)."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

from ainame import is_group
from angareion.backend import (
    BackendError,
    BackendRef,
    ChannelBackend,
    ChannelNotFoundError,
    EntityResolver,
    Target,
    format_backend_time,
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
    resolve_identity,
)
from angareion.github import HttpTransport, GitHubIssuesBackend
from angareion.messages import (
    Message,
    MessageValidationError,
    is_ulid,
    message_to_dict,
    serialize_message,
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
from angareion.log_cache import LogCache
from angareion.state import CursorStore, FileETagCache

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

# #96 (M0 §3.1): receiving with --channel spans every message in the channel,
# including instructions addressed to someone else. The tool cannot stop that,
# so read/wait say it once on stderr; stdout and exit codes stay unchanged.
_CHANNEL_READ_WARNING = (
    "--channel reads every message in the channel, including messages addressed to other agents; "
    "omit --channel to get only messages addressed to you."
)


def _warn(message: str) -> None:
    sys.stderr.write(f"Warning: {message}\n")


class UsageError(Exception):
    """A usage error, so bad arguments exit 1 like the rest of the input errors."""


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; Angareion's contract is 1."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise UsageError(f"{self.prog}: error: {message}")


def parse_address(addr_str: str, *, field_name: str = "address") -> str:
    """Validate one address and return it as an AI name (the #97 grammar).

    An AI name is `<project>-<role>-<team>[-<seq>]`, e.g. `mypmo-pm-m1` or
    `mylinuxpool-impl-w1-1`. Only half-width spaces around it are forgiven (a
    shell quoting slip): a newline, a tab or a carriage return is copied-in
    dirt, and it is refused loudly with the grammar's reason, because the
    grammar compares the whole string. The old `repo=…`, `×`, `:` and JSON
    shapes are format errors, and a `@…` group address is one too: group
    addresses arrive in phase 3.
    """
    if not isinstance(addr_str, str) or not addr_str.strip(" "):
        raise MessageValidationError("Address string cannot be empty")
    address = addr_str.strip(" ")
    if is_group(address):
        raise MessageValidationError(
            f"{field_name} {address!r} is a group address; group addresses arrive in phase 3, "
            "phase 2 sends to one AI name or to the whole channel (no --to)"
        )
    validate_address(address, field_name=field_name)
    return address


def _target_from_args(args: argparse.Namespace, config: Config) -> Target:
    if args.channel:
        return Target(channel=args.channel)
    if args.mentioned:
        return Target(mentioned=config.identity)
    return Target(recipient=config.identity)


def _check_scope_args(args: argparse.Namespace) -> None:
    """`--mentioned` is "@ me" across all channels, so it cannot be narrowed to one."""
    if args.mentioned and args.channel:
        raise UsageError("--mentioned cannot be combined with --channel (it reads @-mentions in every channel)")


def _format_message(message: Message) -> str:
    header = f"[{message.urgency}] {message.id} {message.at}"
    received_at = getattr(message, "received_at", None)
    if received_at:
        header += f" (received {received_at})"
    header += f" <{message.from_}> channel: {message.channel}"
    lines = [header]
    if message.mentions:
        lines.append("  mentions: " + ", ".join(message.mentions))
    if message.in_reply_to:
        lines.append(f"  in_reply_to: {message.in_reply_to}")
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
            to_addr = parse_address(args.to, field_name="--to")
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
            mentions=[parse_address(name, field_name="--mention") for name in args.mention or []],
            in_reply_to=args.in_reply_to,
        )
        # Validate everything before touching the backend: a bad message sends nothing.
        serialize_message(message)  # validates, then reads it back: one that cannot be read is refused here
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

    try:
        _check_scope_args(args)
    except UsageError as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_USAGE

    # The pull happens once, before the loop: wait must not fetch while polling.
    app.store.ensure_fresh()
    if args.channel:
        # REVIEW-96 L2: validate the channel before warning; a missing channel
        # is an input error, not a reason to warn about reading other agents' mail.
        app.store.entity_for(args.channel)
        _warn(_CHANNEL_READ_WARNING)
    target = _target_from_args(args, config)
    found = app.wait(target, args.timeout, poll_interval=POLL_INTERVAL_SECONDS, sleeper=sleeper, clock=clock)
    _report_old_cursors(app)
    if found:
        return EXIT_OK
    sys.stderr.write("Timed out with no new messages.\n")
    return EXIT_TIMEOUT


def cmd_read(args: argparse.Namespace, config: Config, app: Angareion, token: str) -> int:
    try:
        _check_scope_args(args)
    except UsageError as e:
        sys.stderr.write(f"Error: {e}\n")
        return EXIT_USAGE

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

    if args.channel:
        # REVIEW-96 L2: validate the channel before warning (see cmd_wait).
        app.store.entity_for(args.channel)
        _warn(_CHANNEL_READ_WARNING)

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


def _package_version() -> str:
    """Return the installed package version from its metadata (never hardcoded).

    The distribution is named ``agora`` (pyproject.toml); ``angareion`` is not
    a distribution, so only that name is consulted. If it is not installed,
    report ``unknown`` rather than falling back to a hardcoded constant.
    """
    try:
        return importlib.metadata.version("agora")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _commit_sha() -> str:
    """Best-effort short commit sha of the source tree; 'unknown' if unavailable.

    Only report a sha when ``repo_root`` is itself the top level of a git
    work tree: ``git -C`` walks up the directory tree looking for a ``.git``,
    so an install sitting inside an unrelated repository would otherwise print
    that repository's HEAD. Compare the resolved top level to ``repo_root``
    and fall back to ``unknown`` on any mismatch or failure.
    """
    repo_root = Path(__file__).resolve().parents[2]
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if toplevel.returncode != 0:
        return "unknown"
    try:
        git_root = Path(toplevel.stdout.strip()).resolve()
    except (OSError, ValueError):
        return "unknown"
    if git_root != repo_root.resolve():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    sha = result.stdout.strip()
    return sha if sha else "unknown"


class _VersionAction(argparse.Action):
    """Print the version + commit lazily, only when ``--version`` is given."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help="show program's version number and exit"):
        super().__init__(option_strings, dest, nargs=0, default=default, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        version = f"angareion {_package_version()} ({_commit_sha()})"
        sys.stdout.write(version + "\n")
        parser.exit()


def cmd_log(args, config, app, token):
    import datetime as _dt

    channels = list(args.channel or [])
    try:
        app.store.ensure_fresh()
    except Exception:
        pass

    if getattr(args, "around", None) is not None:
        if not is_ulid(args.around):
            sys.stderr.write("Error: --around must be a 26-character ULID, got '{}'\n".format(args.around))
            return EXIT_USAGE
    if getattr(args, "thread", None) is not None:
        if not is_ulid(args.thread):
            sys.stderr.write("Error: --thread must be a 26-character ULID, got '{}'\n".format(args.thread))
            return EXIT_USAGE
    if getattr(args, "around", None) is not None and getattr(args, "thread", None) is not None:
        sys.stderr.write("Error: --around and --thread are mutually exclusive\n")
        return EXIT_USAGE
    if (getattr(args, "before", None) is not None or getattr(args, "after", None) is not None) and getattr(args, "around", None) is None:
        sys.stderr.write("Error: --before/--after require --around\n")
        return EXIT_USAGE
    for name in ("before", "after"):
        val = getattr(args, name, None)
        if val is not None and val < 0:
            sys.stderr.write("Error: --{} must not be negative, got {}\n".format(name, val))
            return EXIT_USAGE
    if getattr(args, "limit", None) is not None and args.limit < 0:
        sys.stderr.write("Error: --limit must not be negative, got {}\n".format(args.limit))
        return EXIT_USAGE

    if args.since is not None:
        try:
            parse_backend_time(args.since)
        except ValueError:
            sys.stderr.write(
                "Error: --since must be an RFC 3339 timestamp such as "
                "2026-10-12T03:00:00Z, got '{}'\n".format(args.since)
            )
            return EXIT_USAGE
    if args.until is not None:
        try:
            parse_backend_time(args.until)
        except ValueError:
            sys.stderr.write(
                "Error: --until must be an RFC 3339 timestamp such as "
                "2026-10-12T03:00:00Z, got '{}'\n".format(args.until)
            )
            return EXIT_USAGE
    if args.since is not None and args.until is not None:
        if parse_backend_time(args.since) > parse_backend_time(args.until):
            sys.stderr.write("Error: --since must not be later than --until\n")
            return EXIT_USAGE

    if not channels:
        channels = None
    else:
        for ch in channels:
            try:
                app.store.entity_for(ch)
            except Exception as e:
                sys.stderr.write("Error: {}\n".format(e))
                return EXIT_USAGE

    if channels is None:
        entities = list(app.store.entities())
        selected_channels = [e.channel for e in entities]
    else:
        entities = [app.store.entity_for(ch) for ch in channels]
        selected_channels = list(channels)

    from angareion.log_cache import LogCache
    cache = LogCache(config.state_dir, config.identity)

    channel_messages = {}
    try:
        for entity in entities:
            ch = entity.channel
            cached_entity = {"repo": entity.repo, "issue": entity.issue}
            cached_msgs, last_cid, last_created, etag = cache.load_messages(ch, cached_entity)
            mirror = list(cached_msgs)
            if last_created is None:
                since = None
            else:
                since_dt = parse_backend_time(last_created) - _dt.timedelta(seconds=60)
                since = format_backend_time(since_dt)
            enumerated, new_etag, not_modified = app.backend.enumerate_conditional(entity, since=since, etag=etag)
            by_id = {}
            for m in mirror:
                try:
                    by_id[int(m["comment_id"])] = m
                except Exception:
                    pass
            max_cid = None
            max_created = last_created
            if not not_modified:
                for cid, created_at, parsed in enumerated:
                    if parsed is None:
                        continue
                    cid_int = int(cid)
                    msg_dict = {
                        "comment_id": str(cid_int),
                        "created_at": created_at,
                        "comment_url": getattr(parsed, "comment_url", "") or "",
                        "message": message_to_dict(parsed),
                    }
                    existing = by_id.get(cid_int)
                    if existing is None or existing.get("message") != msg_dict["message"] or existing.get("created_at") != created_at:
                        by_id[cid_int] = msg_dict
                    if max_cid is None or cid_int > max_cid:
                        max_cid = cid_int
                    if max_created is None or parse_backend_time(created_at) > parse_backend_time(max_created):
                        max_created = created_at
                if by_id:
                    max_created = max((m["created_at"] for m in by_id.values()), key=lambda t: parse_backend_time(t))
                    max_cid = max(int(m["comment_id"]) for m in by_id.values())
                messages_list = sorted(by_id.values(), key=lambda m: int(m["comment_id"]))
                cache.save(ch, cached_entity, str(max_cid) if max_cid is not None else None, max_created, new_etag if new_etag is not None else etag, messages_list)
            else:
                messages_list = list(mirror)

            msgs_for_query = []
            for m in messages_list:
                d = m["message"]
                from angareion.messages import Message as _M
                msg = _M(
                    id=d["id"],
                    at=d["at"],
                    from_=d["from"],
                    to=d["to"],
                    channel=d["channel"],
                    urgency=d["urgency"],
                    content=d["content"],
                    attachments=d.get("attachments", []),
                    mentions=d.get("mentions", []),
                    in_reply_to=d.get("in_reply_to"),
                )
                setattr(msg, "received_at", m["created_at"])
                setattr(msg, "comment_id", int(m["comment_id"]))
                setattr(msg, "comment_url", m.get("comment_url", ""))
                msgs_for_query.append(msg)
            channel_messages[ch] = msgs_for_query
    except Exception as e:
        sys.stderr.write("Error: {}\n".format(e))
        return EXIT_BACKEND

    me = config.identity
    from_filter = getattr(args, "from_", None)
    if from_filter == "me":
        from_filter = me
    to_filter = getattr(args, "to", None)
    if to_filter == "me":
        to_filter = me
    mentioned_flag = bool(getattr(args, "mentioned", False))

    anchor_id = getattr(args, "around", None) or getattr(args, "thread", None)
    is_thread = getattr(args, "thread", None) is not None

    for ch in channel_messages:
        channel_messages[ch].sort(key=lambda m: getattr(m, "comment_id", 0))

    candidates = []
    anchor_channel = None
    if anchor_id is not None:
        anchor_msg = None
        for ch in selected_channels:
            for m in channel_messages.get(ch, []):
                if m.id == anchor_id:
                    anchor_msg = m
                    anchor_channel = ch
                    break
            if anchor_msg is not None:
                break
        if anchor_msg is None:
            sys.stderr.write("Error: message '{}' not found\n".format(anchor_id))
            return EXIT_USAGE
        if is_thread:
            msgs = channel_messages.get(anchor_channel, [])
            by_id_msg = {m.id: m for m in msgs}
            # find root
            current = anchor_msg
            visited = set()
            while True:
                irt = getattr(current, "in_reply_to", None)
                if irt is None or irt not in by_id_msg or irt in visited:
                    break
                visited.add(current.id)
                current = by_id_msg[irt]
            root = current
            closure_ids = {root.id}
            changed = True
            while changed:
                changed = False
                for m in msgs:
                    irt = getattr(m, "in_reply_to", None)
                    if irt in closure_ids and m.id not in closure_ids:
                        closure_ids.add(m.id)
                        changed = True
            collected = [m for m in msgs if m.id in closure_ids]
            collected.sort(key=lambda m: getattr(m, "comment_id", 0))
            candidates = collected
        else:
            msgs = channel_messages.get(anchor_channel, [])
            sorted_msgs = sorted(msgs, key=lambda m: getattr(m, "comment_id", 0))
            idx = next(i for i, m in enumerate(sorted_msgs) if m.id == anchor_id)
            before_n = getattr(args, "before", 0) or 0
            after_n = getattr(args, "after", 0) or 0
            start = max(0, idx - before_n)
            end = min(len(sorted_msgs), idx + after_n + 1)
            candidates = list(sorted_msgs[start:end])
    else:
        for ch in selected_channels:
            candidates.extend(channel_messages.get(ch, []))

    def keep(m):
        if from_filter is not None and m.from_ != from_filter:
            return False
        if to_filter is not None:
            if not (m.to is None or m.to == to_filter or to_filter in (m.mentions or [])):
                return False
        if mentioned_flag:
            if not (m.to == me or me in (m.mentions or [])):
                return False
        if args.since is not None:
            if parse_backend_time(m.received_at) < parse_backend_time(args.since):
                return False
        if args.until is not None:
            if parse_backend_time(m.received_at) > parse_backend_time(args.until):
                return False
        if getattr(args, "grep", None):
            if args.grep.lower() not in (m.content or "").lower():
                return False
        return True

    filtered = [m for m in candidates if keep(m)]

    if len(selected_channels) == 1:
        filtered.sort(key=lambda m: getattr(m, "comment_id", 0))
    else:
        filtered.sort(key=lambda m: (getattr(m, "received_at", "") or "", m.channel, m.id))

    if getattr(args, "limit", None) is not None:
        n = args.limit
        if n == 0:
            filtered = []
        else:
            filtered = filtered[-n:]

    if args.json:
        sys.stdout.write(json.dumps([_message_for_output(m) for m in filtered], indent=2, ensure_ascii=False) + "\n")
    else:
        for m in filtered:
            sys.stdout.write(_format_message(m))
    sys.stdout.flush()
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
    parser.add_argument("--version", action=_VersionAction, help="show program's version number and exit")
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
                        help="Recipient AI name '<project>-<role>-<team>[-<seq>]' "
                             "(e.g. mylinuxpool-impl-w1-1); omit to send to the whole channel.")
    p_send.add_argument("--mention", action="append", default=[], metavar="AGENT",
                        help="Also call this AI name (repeatable); it is not the recipient, `--to` is. "
                             "Every reader understands mentions since 2026-10-25; a new kind of reader must be checked first.")
    p_send.add_argument("--in-reply-to", metavar="ULID",
                        help="The id of the message this one replies to.")
    p_send.add_argument("--urgency", type=int, default=5, help="Urgency 0-9 (default: 5).")
    p_send.add_argument("--attach", action="append", default=[], metavar="NAME=PATH",
                        help="Attach a file inline (repeatable).")
    p_send.add_argument("--ref", action="append", default=[], metavar="NAME=LOCATOR",
                        help="Attach a reference or link (repeatable).")
    p_send.add_argument("--body", metavar="TEXT",
                        help="Message text; '-' reads the text on stdin. Required.")

    p_wait = subparsers.add_parser("wait", help="Wait for new messages: exit 0, or 3 on timeout.")
    p_wait.add_argument(
        "--channel",
        help="Channel to watch; default: everything addressed to me. Includes messages addressed to other agents.",
    )
    p_wait.add_argument("--mentioned", action="store_true",
                        help="Only messages that are addressed to me or mention me (broadcasts excluded), "
                             "in every channel; cannot be combined with --channel.")
    p_wait.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="Wait at most this long; 0 checks once; omit to wait forever.")

    p_read = subparsers.add_parser("read", help="Read new messages, oldest first.")
    p_read.add_argument(
        "--channel",
        help="Channel to read; default: everything addressed to me. Includes messages addressed to other agents.",
    )
    p_read.add_argument("--mentioned", action="store_true",
                        help="Only messages that are addressed to me or mention me (broadcasts excluded), "
                             "in every channel; cannot be combined with --channel.")
    p_read.add_argument("--since", metavar="TIMESTAMP",
                        help="Read after this time; does not move the local cursor.")
    p_read.add_argument("--json", action="store_true", help="Print a JSON array.")

    p_log = subparsers.add_parser("log", help="Query messages (read-only; does not move cursors).")
    p_log.add_argument("--channel", action="append", default=[], metavar="CH",
                       help="Channel to query (repeatable).")
    p_log.add_argument("--from", dest="from_", metavar="AGENT",
                       help="Sender AI name (or 'me').")
    p_log.add_argument("--to", metavar="AGENT",
                       help="Recipient filter (or 'me').")
    p_log.add_argument("--mentioned", action="store_true",
                       help="@ me filter.")
    p_log.add_argument("--since", metavar="TIMESTAMP",
                       help="Lower bound (inclusive).")
    p_log.add_argument("--until", metavar="TIMESTAMP",
                       help="Upper bound (inclusive).")
    p_log.add_argument("--grep", metavar="TEXT",
                       help="Literal case-insensitive substring in content.")
    p_log.add_argument("--around", metavar="ULID",
                       help="Anchor message id.")
    p_log.add_argument("--before", type=int, metavar="N",
                       help="Messages before anchor (requires --around).")
    p_log.add_argument("--after", type=int, metavar="N",
                       help="Messages after anchor (requires --around).")
    p_log.add_argument("--thread", metavar="ULID",
                       help="Thread root message id.")
    p_log.add_argument("--limit", type=int, metavar="N",
                       help="Keep the last N after sorting.")
    p_log.add_argument("--json", action="store_true", help="Print a JSON array.")
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
    except SystemExit as e:  # argparse --help / --version terminate via SystemExit; convert to an int return code
        return e.code if isinstance(e.code, int) else EXIT_OK
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

    # ANGAREION_IDENTITY wins over config.json's identity (#97), and it is
    # resolved before anything is built from the config: `from`, "to me", the
    # cursor file and the wait state file all follow the name that is in force
    # here, so a run under a different seat never touches the other seat's
    # files. A bad name in either place is a configuration error (exit 2).
    try:
        config = config.with_identity(resolve_identity(config))
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
            active_store,
            token=token,
            transport=transport,
            etag_cache=FileETagCache(config.state_dir, config.identity),
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
        if args.command == "log":
            return cmd_log(args, config, app, token)
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

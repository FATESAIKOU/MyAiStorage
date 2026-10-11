"""Command line interface for Angareion (a2a)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Sequence

from angareion.backend import (
    AuthenticationError,
    Backend,
    BackendError,
    ChannelNotFoundError,
    FakeBackend,
    RateLimitError,
)
from angareion.config import (
    Config,
    ConfigError,
    TokenError,
    get_state_dir,
    load_config,
    load_token,
    mask_token,
)
from angareion.github import GitHubBackend
from angareion.messages import (
    MAX_MESSAGE_LENGTH,
    Message,
    MessageValidationError,
    is_group_address,
    new_ulid,
    now_iso,
    serialize_message,
    validate_address,
)
from angareion.state import LocalState, identity_slug


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


class RateLimiter:
    """Sliding-window rate limiter (80/min, 500/hour) with state in state_dir and flock."""

    def __init__(self, state_dir: Path, sleeper: Callable[[float], None] = time.sleep):
        self.state_file = state_dir / "rate_limit.json"
        self.sleeper = sleeper

    def acquire(self, max_wait: float = 10.0) -> None:
        """Check limits (80/min, 500/hour) with file locking (fcntl.flock).

        The wait, if any, happens outside the lock: each pass takes the lock
        only to read the history and decide (record now / wait N seconds /
        raise), releases it, and — after sleeping — reads again. A concurrent
        sender therefore only ever blocks on the short read-modify-write,
        never on our whole wait.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        while True:
            wait_needed = self._decide_under_lock(max_wait)
            if wait_needed is None:
                return
            sys.stderr.write(f"Rate throttle: waiting {wait_needed:.1f}s before sending...\n")
            self.sleeper(wait_needed)

    def _decide_under_lock(self, max_wait: float) -> float | None:
        """One locked pass: prune the history, then record now (None), name a
        wait in seconds, or raise RateLimitError when the wait exceeds max_wait."""
        with open(self.state_file, "a+", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                content = f.read().strip()
                history: list[float] = []
                if content:
                    try:
                        data = json.loads(content)
                        if isinstance(data, list):
                            history = [float(x) for x in data]
                    except Exception:
                        history = []

                now = time.time()
                history = [t for t in history if now - t < 3600.0]

                # 1-minute window (limit: 80)
                recent_1m = [t for t in history if now - t < 60.0]
                if len(recent_1m) >= 80:
                    oldest_in_window = min(recent_1m)
                    wait_needed = 60.0 - (now - oldest_in_window)
                    if wait_needed > 0:
                        if wait_needed <= max_wait:
                            return wait_needed
                        raise RateLimitError(
                            f"Rate limit exceeded (80 messages/min). Required wait {wait_needed:.1f}s exceeds limit of {max_wait}s.",
                            retry_after=int(wait_needed),
                        )

                # 1-hour window (limit: 500)
                if len(history) >= 500:
                    oldest_in_window = min(history)
                    wait_needed = 3600.0 - (now - oldest_in_window)
                    if wait_needed > 0:
                        if wait_needed <= max_wait:
                            return wait_needed
                        raise RateLimitError(
                            f"Rate limit exceeded (500 messages/hour). Required wait {wait_needed:.1f}s exceeds limit of {max_wait}s.",
                            retry_after=int(wait_needed),
                        )

                history.append(now)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(history) + "\n")
                f.flush()
                return None
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def get_backend(config: Config) -> Backend:
    """Get backend instance based on environment or configuration."""
    backend_type = os.environ.get("A2A_BACKEND", "").lower()
    if backend_type == "fake":
        return FakeBackend()
    return GitHubBackend.from_config(config)


def cmd_send(
    args: argparse.Namespace,
    config: Config,
    backend: Backend,
    token: str = "",
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Handle `a2a send` subcommand."""
    # 1. Parse 'to' address
    try:
        to_addr = parse_address(args.to)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 1

    if is_group_address(to_addr):
        sys.stderr.write("Error: Group addressing is not supported in v0.\n")
        return 1

    # 2. Parse 'from' address
    try:
        if args.from_addr:
            from_addr = parse_address(args.from_addr)
        else:
            from_addr = config.identity
        validate_address(from_addr, field_name="from")
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 1

    if is_group_address(from_addr):
        sys.stderr.write("Error: 'from' address cannot be a group address.\n")
        return 1

    # 3. Urgency validation
    if not (0 <= args.urgency <= 9):
        sys.stderr.write(f"Error: urgency must be between 0 and 9, got {args.urgency}\n")
        return 1

    # 4. Attachments
    attachments: list[dict[str, Any]] = []
    for att_item in args.attach or []:
        if "=" not in att_item:
            sys.stderr.write(f"Error: --attach must be name=path, got '{att_item}'\n")
            return 1
        name, file_path_str = att_item.split("=", 1)
        file_path = Path(os.path.expanduser(file_path_str.strip()))
        if not file_path.is_file():
            sys.stderr.write(f"Error: attachment file not found: {file_path}\n")
            return 1
        try:
            content_text = file_path.read_text(encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"Error reading attachment file {file_path}: {e}\n")
            return 1
        attachments.append({"name": name.strip(), "inline": content_text})

    for ref_item in args.ref or []:
        if "=" not in ref_item:
            sys.stderr.write(f"Error: --ref must be name=locator, got '{ref_item}'\n")
            return 1
        name, locator = ref_item.split("=", 1)
        attachments.append({"name": name.strip(), "ref": locator.strip()})

    # 5. Body
    if args.body == "-":
        body = sys.stdin.read()
    elif args.body is not None:
        body = args.body
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        body = ""

    msg_id = args.msg_id or new_ulid()

    try:
        msg = Message(
            from_=from_addr,
            to=to_addr,
            channel=args.channel,
            urgency=args.urgency,
            content=body,
            attachments=attachments,
            id=msg_id,
            at=now_iso(),
        )
        serialize_message(msg)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 1

    # 6. Check channel existence
    try:
        channel_num = backend.find_channel(args.channel)
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 2

    if channel_num is None:
        if args.new:
            try:
                backend.create_channel(args.channel)
            except BackendError as e:
                sys.stderr.write(f"Error creating channel '{args.channel}': {mask_token(str(e), token)}\n")
                return 2
        else:
            known_channels = [ch for ch, _ in (backend.list_channels() if hasattr(backend, "list_channels") else [])]
            known_str = f" Known channels: {', '.join(known_channels)}" if known_channels else ""
            sys.stderr.write(f"Error: Channel '{args.channel}' does not exist. Use --new to create it.{known_str}\n")
            return 1

    # 7. Rate throttle
    rate_limiter = RateLimiter(config.state_dir, sleeper=sleeper)
    try:
        rate_limiter.acquire(max_wait=10.0)
    except RateLimitError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 2

    # 8. Post message
    try:
        backend.post_message(args.channel, msg)
    except RateLimitError as e:
        sys.stderr.write(f"Rate limit hit, retrying in {e.retry_after}s...\n")
        sleeper(float(e.retry_after))
        try:
            backend.post_message(args.channel, msg)
        except BackendError as e_inner:
            sys.stderr.write(f"Error: {mask_token(str(e_inner), token)}\n")
            return 2
    except (ChannelNotFoundError, MessageValidationError, ValueError) as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 1
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 2

    # 9. Output on stdout: exactly `<id> <channel>`
    sys.stdout.write(f"{msg.id} {args.channel}\n")
    return 0


def cmd_inbox(args: argparse.Namespace, config: Config, backend: Backend, token: str = "") -> int:
    """Handle `a2a inbox` subcommand."""
    if args.identity:
        try:
            identity = parse_address(args.identity)
            validate_address(identity, field_name="identity")
        except MessageValidationError as e:
            sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
            return 1
    else:
        identity = config.identity

    state = LocalState.load_or_create(config.state_dir, identity)

    # We maintain a cached message history in state_dir for rapid 304 polling
    cache_file = config.state_dir / f"{identity_slug(identity)}_cache.json"
    cached_messages_dict: dict[str, dict[str, Any]] = {}
    if cache_file.is_file():
        try:
            cached_messages_dict = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached_messages_dict = {}

    etag = state.etags.get("messages") if cached_messages_dict else None
    # If we have no cached messages, query since=None to ensure complete rebuild
    since_query = state.since if cached_messages_dict else None

    try:
        res = backend.list_messages(since=since_query, etag=etag)
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 2

    if not res.not_modified:
        if res.etag:
            state.etags["messages"] = res.etag
        for m in res.messages:
            if m.ack:
                state.mark_acked(m.ack)
            issue_num = getattr(m, "issue_number", None)
            if issue_num is not None and m.channel:
                state.set_channel(m.channel, issue_num)
            if state.since is None or m.at > state.since:
                state.since = m.at

            # Store serialized dict of message in cache
            cached_messages_dict[m.id] = {
                "id": m.id,
                "at": m.at,
                "from": m.from_,
                "to": m.to,
                "channel": m.channel,
                "urgency": m.urgency,
                "content": m.content,
                "attachments": m.attachments,
                "ack": m.ack,
                "comment_url": getattr(m, "comment_url", ""),
            }

        state.save()
        if cached_messages_dict:
            # An empty channel leaves no cache file: {} is falsy, so the next
            # inbox fetches full (etag/since None) either way — writing it
            # would only buy a 200 that says "still empty".
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cached_messages_dict, indent=2) + "\n", encoding="utf-8")

    # Filter messages for current recipient identity
    target_repo = identity.get("repo")
    target_role = identity.get("role")

    matched_messages: list[dict[str, Any]] = []
    for msg_data in cached_messages_dict.values():
        if msg_data.get("ack"):
            continue  # Pure ack messages are not display items
        msg_to = msg_data.get("to", {})
        if is_group_address(msg_to):
            continue  # Group messages are not delivered to inboxes in v0

        if msg_to.get("repo") == target_repo and msg_to.get("role") == target_role:
            is_acked = msg_data["id"] in state.acked
            if not is_acked or args.all:
                item = dict(msg_data)
                item["acked"] = is_acked
                matched_messages.append(item)

    # Sort from oldest to newest by (at, id)
    matched_messages.sort(key=lambda m: (m.get("at", ""), m.get("id", "")))

    if args.json:
        sys.stdout.write(json.dumps(matched_messages, indent=2) + "\n")
    else:
        for m in matched_messages:
            from_str = f"{m['from'].get('repo')}×{m['from'].get('role')}"
            ack_tag = " [acked]" if m.get("acked") else ""
            sys.stdout.write(
                f"[{m.get('urgency', 5)}] {m.get('id')} {m.get('at')}{ack_tag} "
                f"<{from_str}> channel: {m.get('channel')}\n"
                f"  {m.get('content', '').strip()}\n"
            )

    return 0


def cmd_ack(args: argparse.Namespace, config: Config, backend: Backend, token: str = "") -> int:
    """Handle `a2a ack` subcommand."""
    identity = config.identity
    validate_address(identity, field_name="identity")

    state = LocalState.load_or_create(config.state_dir, identity)

    # Scan messages from backend to locate targets
    try:
        res = backend.list_messages(since=None)
    except BackendError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return 2

    msg_map: dict[str, Message] = {}
    for m in res.messages:
        if m.ack:
            state.mark_acked(m.ack)
        else:
            msg_map[m.id] = m
    state.save()

    has_error = False
    for target_id in args.ids:
        if target_id in state.acked:
            sys.stderr.write(f"Notice: Message {target_id} is already acknowledged.\n")
            continue

        if target_id not in msg_map:
            sys.stderr.write(f"Error: Message {target_id} not found.\n")
            has_error = True
            continue

        target_msg = msg_map[target_id]
        ack_msg = Message(
            from_=identity,
            to=target_msg.from_,
            channel=target_msg.channel,
            urgency=5,
            content=args.note or "",
            ack=target_id,
            id=new_ulid(),
            at=now_iso(),
        )

        try:
            backend.post_message(target_msg.channel, ack_msg)
            state.mark_acked(target_id)
            state.save()
            sys.stderr.write(f"Acknowledged message {target_id} on channel {target_msg.channel}\n")
        except BackendError as e:
            sys.stderr.write(f"Error posting ack for {target_id}: {mask_token(str(e), token)}\n")
            return 2

    return 1 if has_error else 0


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for `a2a`."""
    parser = argparse.ArgumentParser(
        prog="a2a",
        description="Angareion: Append-only, auditable agent-to-agent message channel CLI.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    # `a2a send`
    p_send = subparsers.add_parser("send", help="Send a message to a channel.")
    p_send.add_argument("--to", required=True, help="Recipient address ({repo, role, [name]} or {group}).")
    p_send.add_argument("--channel", required=True, help="Channel topic name.")
    p_send.add_argument("--new", action="store_true", help="Create channel topic if it does not exist.")
    p_send.add_argument("--urgency", type=int, default=5, help="Urgency level 0-9 (default: 5).")
    p_send.add_argument("--from", dest="from_addr", help="Sender address override.")
    p_send.add_argument("--id", dest="msg_id", help="Message ULID (for idempotency / retry).")
    p_send.add_argument("--attach", action="append", default=[], help="Attach inline file: name=path.")
    p_send.add_argument("--ref", action="append", default=[], help="Attach reference: name=locator.")
    p_send.add_argument("--body", help="Message body markdown text or '-' for stdin.")

    # `a2a inbox`
    p_inbox = subparsers.add_parser("inbox", help="List unread/unacked messages.")
    p_inbox.add_argument("--identity", help="Recipient identity override (repo×role).")
    p_inbox.add_argument("--all", action="store_true", help="Include acknowledged messages.")
    p_inbox.add_argument("--json", action="store_true", help="Output machine-readable JSON array.")

    # `a2a ack`
    p_ack = subparsers.add_parser("ack", help="Acknowledge one or more messages.")
    p_ack.add_argument("ids", nargs="+", help="Message ULID(s) to acknowledge.")
    p_ack.add_argument("--note", help="Optional acknowledgment note.")

    return parser


def main(
    argv: Sequence[str] | None = None,
    backend: Backend | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> int:
    """Main entrypoint for the `a2a` command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 1

    active_sleeper = sleeper or time.sleep
    token_for_masking = ""
    try:
        config = load_config()
    except ConfigError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 2

    try:
        token_for_masking = load_token(config)
    except Exception:
        pass

    try:
        active_backend = backend or get_backend(config)
    except (TokenError, ConfigError, BackendError) as e:
        msg = mask_token(str(e), token_for_masking)
        sys.stderr.write(f"Error: {msg}\n")
        return 2

    try:
        if args.subcommand == "send":
            return cmd_send(args, config, active_backend, token=token_for_masking, sleeper=active_sleeper)
        elif args.subcommand == "inbox":
            return cmd_inbox(args, config, active_backend, token=token_for_masking)
        elif args.subcommand == "ack":
            return cmd_ack(args, config, active_backend, token=token_for_masking)
        else:
            parser.print_help(sys.stderr)
            return 1
    except (MessageValidationError, ValueError) as e:
        msg = mask_token(str(e), token_for_masking)
        sys.stderr.write(f"Error: {msg}\n")
        return 1
    except BackendError as e:
        msg = mask_token(str(e), token_for_masking)
        sys.stderr.write(f"Error: {msg}\n")
        return 2
    except Exception as e:
        msg = mask_token(f"Unexpected error: {e}", token_for_masking)
        sys.stderr.write(f"Error: {msg}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())

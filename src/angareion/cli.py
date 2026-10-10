"""Command line interface for Angareion (a2a)."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence


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


def main(argv: Sequence[str] | None = None) -> int:
    """Main entrypoint for the `a2a` command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_help(sys.stderr)
        return 1

    # Placeholder for subcommands (implemented in tasks 2.x)
    sys.stderr.write(f"a2a {args.subcommand}: subcommand placeholder (to be implemented in tasks 2.x)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

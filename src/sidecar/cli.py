"""Command-line interface for agent sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from sidecar.config import load_config
from sidecar.queue import MessageQueue
from sidecar.registry import Registry


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    registry = Registry(config.state_dir)
    queue = MessageQueue(config.state_dir)

    all_agents = registry.list_agents()
    running_agents = [a for a in all_agents if a.status == "running"]

    queue_counts = {}
    # Count configured identities as well as any existing queue files
    identities = set(config.identities) | set(queue.list_identities())
    for ident in sorted(identities):
        queue_counts[ident] = queue.count(ident)

    status_data = {
        "status": "ok",
        "config_path": str(config.config_path),
        "state_dir": str(config.state_dir),
        "identities": config.identities,
        "token_configured": bool(config.a2a_token_path),
        "token_valid_permissions": config.verify_token_permissions(),
        "agents": {
            "total": len(all_agents),
            "running": len(running_agents),
            "list": [a.to_dict() for a in all_agents],
        },
        "queues": queue_counts,
    }

    if args.json:
        print(json.dumps(status_data, indent=2, ensure_ascii=False))
        return 0

    print("Sidecar Status:")
    print(f"  Config:     {config.config_path}")
    print(f"  State Dir:  {config.state_dir}")
    print(f"  Identities: {', '.join(config.identities) if config.identities else '(none)'}")
    print(f"  Token:      {'0600 ok' if status_data['token_valid_permissions'] else ('configured' if status_data['token_configured'] else 'none')}")
    print(f"  Agents:     {len(running_agents)} running / {len(all_agents)} total")
    for a in all_agents:
        print(f"    - [{a.status}] {a.agent_name} ({a.identity}, id={a.agent_id}, pane={a.herdr_pane_id})")
    print("  Queues:")
    for ident, cnt in queue_counts.items():
        print(f"    - {ident}: {cnt} queued")

    return 0


def cmd_say(args: argparse.Namespace) -> int:
    from sidecar.relay import send_relay_message
    try:
        res = send_relay_message(
            content=args.message,
            to=args.to,
            urgency=args.urgency,
            socket_path=args.sock,
        )
        if args.json:
            print(json.dumps({"ok": True, "result": res}, indent=2, ensure_ascii=False))
        else:
            print(f"Sent message to {args.to} (from {res.get('from', 'agent')}, id={res.get('id', '')})")
        return 0
    except Exception as e:
        sys.stderr.write(f"Error sending message: {e}\n")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sidecar", description="Agent Sidecar supervisor CLI")
    parser.add_argument("--config", "-c", help="Path to config.json (overrides SIDECAR_CONFIG)")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    status_parser = subparsers.add_parser("status", help="Show sidecar status and running agents")
    status_parser.add_argument("--json", action="store_true", help="Output status as JSON")
    status_parser.set_defaults(func=cmd_status)

    say_parser = subparsers.add_parser("say", help="Report a message to Angareion via sidecar relay")
    say_parser.add_argument("message", help="Message content to report")
    say_parser.add_argument("--to", "-t", default="pmo", help="Recipient identity (default: pmo)")
    say_parser.add_argument("--urgency", "-u", type=int, default=5, help="Urgency level (0-10, default: 5)")
    say_parser.add_argument("--sock", "-s", help="Path to relay socket (overrides SIDECAR_SOCK)")
    say_parser.add_argument("--json", action="store_true", help="Output result as JSON")
    say_parser.set_defaults(func=cmd_say)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        return args.func(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

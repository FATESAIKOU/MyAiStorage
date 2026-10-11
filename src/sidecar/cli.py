"""Command-line interface for agent sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from sidecar import __version__ as _pkg_version
from sidecar.config import load_config
from sidecar.queue import MessageQueue
from sidecar.registry import Registry


def sidecar_version_string() -> str:
    """Human-readable version: installed sha, else package version (D11)."""
    sha = installed_sha()
    if sha:
        return f"sidecar {sha}"
    return f"sidecar {_pkg_version} (install sha unknown)"


def installed_sha() -> str | None:
    """Commit sha this install came from (PEP 610 direct_url), if any."""
    try:
        from importlib import metadata

        dist = metadata.distribution("agora")
        text = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not text:
        return None
    try:
        info = json.loads(text)
    except ValueError:
        return None
    vcs = info.get("vcs_info") or {}
    return vcs.get("commit_id") or None


def cmd_status(args: argparse.Namespace, agora_client=None) -> int:
    from sidecar.agora import LibraryAgoraClient

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

    # 3.9/D11: fork sessions on agora that belong to our identities.
    agora_client = agora_client or LibraryAgoraClient()
    agora_forks: list[dict] = []
    agora_forks_error: str | None = None
    try:
        for ident in sorted(config.identities):
            agora_forks.extend(agora_client.search_sessions([f"sidecar.identity={ident}"]))
    except Exception as e:
        agora_forks_error = str(e)

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
        "agora_forks": agora_forks,
        "agora_forks_error": agora_forks_error,
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
    print("  Agora forks:")
    if agora_forks_error is not None:
        print(f"    - unavailable: {agora_forks_error}")
    elif not agora_forks:
        print("    - (none)")
    for f in agora_forks:
        state = "active" if f.get("active") else "idle"
        print(f"    - [{state}] {f.get('id')} (identity={f.get('identity')}, message={f.get('message')}, title={f.get('title')})")

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


def cmd_run(args: argparse.Namespace) -> int:
    """Resident daemon entry (D10, D-run): poll + relay + recover/heartbeat."""
    import os

    from sidecar.config import load_config
    from sidecar.herdr import RealHerdrClient
    from sidecar.host import AgentHost
    from sidecar.queue import MessageQueue
    from sidecar.receiver import Receiver
    from sidecar.registry import Registry
    from sidecar.relay import (
        AngareionRelaySender,
        RelayServer,
        get_default_sock_path,
    )
    from sidecar.run import run_loop

    config = load_config(args.config)

    if not config.channel_repo:
        sys.stderr.write("Error: channel_repo is not configured (sidecar config.json)\n")
        return 1

    if os.environ.get("A2A_BACKEND", "").lower() == "fake":
        from angareion.backend import FakeBackend
        backend = FakeBackend()
    else:
        if not config.a2a_token_path:
            sys.stderr.write("Error: a2a_token_path is not configured (sidecar config.json)\n")
            return 1
        from angareion.config import TokenError, load_token
        from angareion.github import GitHubBackend
        try:
            token = load_token(config.a2a_token_path)
        except TokenError as e:
            sys.stderr.write(f"Error: {e}\n")
            return 1
        backend = GitHubBackend(channel_repo=config.channel_repo, token=token)

    registry = Registry(config.state_dir)
    queue = MessageQueue(config.state_dir)
    try:
        # 3.8/D11: every herdr call carries the configured session name
        # (RealHerdrClient prepends the global --session option).
        host = AgentHost(RealHerdrClient(session=config.herdr_session), registry, config=config)
    except RuntimeError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1

    receiver = Receiver(backend, config, registry, queue, host)
    # L8/D10: relayed reports carry each team's full address from config.
    team_addresses = {
        identity: team.get("identity")
        for identity, team in config.teams.items()
        if isinstance(team.get("identity"), dict)
    }
    sender = AngareionRelaySender(
        backend, default_channel=args.channel, team_addresses=team_addresses
    )
    sock_path = args.sock if args.sock else get_default_sock_path()
    server = RelayServer(
        socket_path=sock_path,
        registry=registry,
        sender=sender,
        herdr=host.herdr,
    )

    try:
        return run_loop(
            receiver=receiver,
            registry=registry,
            relay_server=server,
            poll_interval=args.poll_interval,
            recover_interval=args.recover_interval,
            max_cycles=1 if args.once else None,
        )
    except KeyboardInterrupt:
        return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Health verification with three-state exit codes (D11, task 3.7)."""
    from sidecar.check import load_config_for_check, run_checks
    from sidecar.herdr import RealHerdrClient

    try:
        config = load_config_for_check(args.config)
    except Exception as e:
        return _report_check(
            [{"name": "config", "status": "fail", "reason": f"config unreadable: {e}"}],
            1,
            args.json,
        )

    from angareion.github import GitHubBackend

    def backend_factory(channel_repo: str, token: str):
        return GitHubBackend(channel_repo=channel_repo, token=token)

    items, exit_code = run_checks(
        config,
        RealHerdrClient(session=config.herdr_session),
        backend_factory,
    )
    return _report_check([i.to_dict() for i in items], exit_code, args.json)


def _report_check(checks: list[dict], exit_code: int, as_json: bool) -> int:
    if as_json:
        print(json.dumps(
            {"ok": exit_code == 0, "exit": exit_code, "checks": checks},
            indent=2,
            ensure_ascii=False,
        ))
        return exit_code
    for c in checks:
        mark = {"ok": "ok", "fail": "FAIL", "unknown": "UNKNOWN"}.get(c["status"], "?")
        print(f"{mark} {c['name']}: {c['reason']}")
    print(f"sidecar check: {'ok' if exit_code == 0 else 'FAIL' if exit_code == 1 else 'UNKNOWN'} (exit {exit_code})")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sidecar", description="Agent Sidecar supervisor CLI")
    parser.add_argument("--config", "-c", help="Path to config.json (overrides SIDECAR_CONFIG)")
    parser.add_argument("--version", "-V", action="version", version=sidecar_version_string())
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    status_parser = subparsers.add_parser("status", help="Show sidecar status and running agents")
    status_parser.add_argument("--json", action="store_true", help="Output status as JSON")
    status_parser.set_defaults(func=cmd_status)

    say_parser = subparsers.add_parser("say", help="Report a message to Angareion via sidecar relay")
    say_parser.add_argument("message", help="Message content to report")
    say_parser.add_argument("--to", "-t", default="pmo", help="Recipient identity (default: pmo)")
    say_parser.add_argument("--urgency", "-u", type=int, default=5, help="Urgency level (0-9, default: 5)")
    say_parser.add_argument("--sock", "-s", help="Path to relay socket (overrides SIDECAR_SOCK)")
    say_parser.add_argument("--json", action="store_true", help="Output result as JSON")
    say_parser.set_defaults(func=cmd_say)

    run_parser = subparsers.add_parser("run", help="Run the resident sidecar daemon (poll + relay + recover)")
    run_parser.add_argument("--sock", "-s", help="Path to relay socket (overrides SIDECAR_SOCK)")
    run_parser.add_argument("--channel", default="general", help="Default Angareion channel for relayed reports")
    run_parser.add_argument("--poll-interval", type=float, default=30.0, help="Seconds between inbox polls (default: 30)")
    run_parser.add_argument("--recover-interval", type=float, default=300.0, help="Seconds between herdr recover/heartbeat passes (default: 300)")
    run_parser.add_argument("--once", action="store_true", help="Run a single poll+recover cycle and exit")
    run_parser.set_defaults(func=cmd_run)

    check_parser = subparsers.add_parser("check", help="Verify sidecar health (exit 0 ok, 1 fail, 2 unknown)")
    check_parser.add_argument("--json", action="store_true", help="Output check results as JSON")
    check_parser.set_defaults(func=cmd_check)

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

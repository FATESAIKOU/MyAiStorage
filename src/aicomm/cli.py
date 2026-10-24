"""aicomm: send a message to another AI (and wake it), read what was sent to me.

Two commands only. `send` writes the message with the `angareion` command - that is the
delivery - and then, as a best effort, pokes the recipient on the local sidecar. `inbox`
reads the messages addressed to me with `angareion read`/`wait`. Standard library only;
aicomm talks to Angareion through its command line and to the sidecar through two of its
HTTP endpoints (look up one agent, poke one agent), nothing else.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from aicomm import __version__

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2
EXIT_TIMEOUT = 3

# The sidecar's documented default address. Not configurable; `AICOMM_SIDECAR` exists for
# tests and manual runs only (a sidecar-opened agent never sees it).
DEFAULT_SIDECAR = "http://127.0.0.1:8471"
SIDECAR_ENV = "AICOMM_SIDECAR"

GET_TIMEOUT = 3.0
POKE_TIMEOUT = 5.0

# The poke carries this and nothing else: never the sender, the body, a ticket or an id.
WAKE_TEXT = "有新訊息：請執行 aicomm inbox。"

TICKET_RE = re.compile(r"^[A-Za-z0-9._-]+#[0-9]+$")
WAKEABLE_STATES = ("idle", "running")


class UsageError(Exception):
    """A wrong command line; reported on stderr, exit 1."""


def sidecar_base(override: str | None) -> str:
    """The sidecar's base URL: the default, or a single loopback override.

    Raises ValueError for an override that is not exactly one loopback address. This is a
    sanity check on a test hook - it cannot tell a forwarded port from a local one.
    """
    if override is None:
        return DEFAULT_SIDECAR
    if not override or re.search(r"[\s,;]", override):
        raise ValueError(f"{SIDECAR_ENV} 只收單一位址：{override!r}")
    text = override if "://" in override else f"http://{override}"
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "http" or parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError(f"{SIDECAR_ENV} 只收 http://<主機>[:<埠>]：{override!r}")
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError(f"{SIDECAR_ENV} 必須是 loopback 位址：{override!r}")
    return f"http://{parts.netloc}"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit 2; this tool's usage code is 1
        raise UsageError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="aicomm", description="Send a message to another AI and wake it; read my inbox.")
    parser.add_argument("--version", action="version", version=f"aicomm {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    p_send = sub.add_parser("send", help="Write a message with angareion, then poke the recipient (best effort).")
    p_send.add_argument("--channel", required=True, help="Channel name.")
    p_send.add_argument("--to", metavar="AGENT", help="Recipient AI name; omit to broadcast (nobody is woken).")
    p_send.add_argument("--mention", action="append", default=[], metavar="AGENT",
                        help="Also call this AI name (repeatable); it is woken like the recipient.")
    p_send.add_argument("--in-reply-to", metavar="ULID", help="The id of the message this one replies to.")
    p_send.add_argument("--urgency", type=int, help="Urgency 0-9 (angareion's default when omitted).")
    p_send.add_argument("--ticket", metavar="REPO#N", help="Attach the ticket as a ref (ticket=REPO#N).")
    p_send.add_argument("--body", required=True, metavar="TEXT", help="Message text; '-' reads it on stdin.")
    group = p_send.add_mutually_exclusive_group()
    group.add_argument("--no-wake", action="store_true", help="Do not poke anybody.")
    group.add_argument("--interrupt", action="store_true",
                       help="Interrupt a running recipient instead of queueing the poke after its turn.")

    p_inbox = sub.add_parser("inbox", help="Read the messages addressed to me.")
    p_inbox.add_argument("--wait", type=float, metavar="SECONDS",
                         help="Block in `angareion wait` first; exit 3 on timeout, 2 on a backend error.")
    p_inbox.add_argument("--mentioned", action="store_true", help="Only messages addressed to or mentioning me.")
    p_inbox.add_argument("--json", action="store_true", help="Print angareion's JSON as it is.")
    return parser


def _angareion(args: list[str], input_text: str | None = None) -> tuple[int, str, str]:
    """Run `angareion <args>`; returns (exit code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["angareion", *args],
            input=input_text if input_text is not None else "",
            capture_output=True, text=True, encoding="utf-8",
        )
    except OSError as e:
        return EXIT_BACKEND, "", f"aicomm: 無法執行 angareion：{e}\n"
    return proc.returncode, proc.stdout, proc.stderr


def _relay(out: str, err: str) -> None:
    if err:
        sys.stderr.write(err)
    if out:
        sys.stdout.write(out)
    sys.stdout.flush()


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return isinstance(error, urllib.error.URLError) and isinstance(error.reason, (TimeoutError, socket.timeout))


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never via a proxy


def _request(method: str, url: str, payload: dict | None, timeout: float) -> tuple[int, bytes]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _wake_one(base: str, name: str, interrupt: bool) -> str:
    """Look up one agent, poke it when it is idle or running; returns one stderr line."""
    quoted = urllib.parse.quote(name, safe="")
    failed = f"已送達，未叫醒 {name}："
    try:
        status, body = _request("GET", f"{base}/agents/{quoted}", None, GET_TIMEOUT)
    except (OSError, urllib.error.URLError) as e:
        return failed + ("查詢逾時" if _is_timeout(e) else f"連不上本機 sidecar（{e}）")
    if status == 404:
        return f"{name}：不在本機，不戳"
    if status != 200:
        return failed + f"查詢回 HTTP {status}"
    try:
        state = json.loads(body).get("state")
    except (ValueError, AttributeError):
        state = None
    if not isinstance(state, str):
        return failed + "查詢的回應看不懂"
    if state not in WAKEABLE_STATES:
        return f"{name}：{state}，略過"
    payload = {"text": WAKE_TEXT, "interrupt": bool(interrupt and state == "running")}
    try:
        status, _ = _request("POST", f"{base}/agents/{quoted}/poke", payload, POKE_TIMEOUT)
    except (OSError, urllib.error.URLError) as e:
        if _is_timeout(e):
            return f"{name}：叫醒結果未知（戳逾時，不重送）"
        return failed + f"戳失敗（{e}）"
    if status != 200:
        return failed + f"戳回 HTTP {status}"
    return f"已叫醒 {name}（{state}{'，打斷' if payload['interrupt'] else ''}）"


def _cmd_send(args: argparse.Namespace) -> int:
    if args.ticket is not None and not TICKET_RE.fullmatch(args.ticket):
        raise UsageError(f"--ticket 要是 <repo>#<n>：{args.ticket!r}")

    me = os.environ.get("ANGAREION_IDENTITY")
    candidates: list[str] = []
    for name in ([args.to] if args.to else []) + args.mention:
        if name != me and name not in candidates:
            candidates.append(name)
    wake = bool(candidates) and not args.no_wake

    # Refuse a bad address before anything is written: once the message is in, the caller
    # must not be told "failed" about something that was delivered.
    base = None
    if wake:
        try:
            base = sidecar_base(os.environ.get(SIDECAR_ENV))
        except ValueError as e:
            raise UsageError(str(e))

    command = ["send", "--channel", args.channel]
    if args.to:
        command += ["--to", args.to]
    for name in args.mention:
        command += ["--mention", name]
    if args.in_reply_to:
        command += ["--in-reply-to", args.in_reply_to]
    if args.urgency is not None:
        command += ["--urgency", str(args.urgency)]
    if args.ticket:
        command += ["--ref", f"ticket={args.ticket}"]
    command += ["--body", args.body]
    stdin_text = sys.stdin.read() if args.body == "-" else None

    code, out, err = _angareion(command, stdin_text)
    _relay(out, err)
    if code != 0:
        return code

    for name in candidates if wake else []:
        sys.stderr.write(_wake_one(base, name, args.interrupt) + "\n")
    return EXIT_OK


def _cmd_inbox(args: argparse.Namespace) -> int:
    if args.wait is not None:
        if args.wait < 0:
            raise UsageError("--wait 要是 0 或正數")
        wait = ["wait"] + (["--mentioned"] if args.mentioned else []) + ["--timeout", f"{args.wait:g}"]
        code, out, err = _angareion(wait)
        _relay(out, err)
        if code != 0:  # 3 = timeout, 2 = backend error: neither is "there is mail"
            return code
    read = ["read"] + (["--mentioned"] if args.mentioned else []) + (["--json"] if args.json else [])
    code, out, err = _angareion(read)
    _relay(out, err)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint for the `aicomm` command; returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] not in ("send", "inbox", "-h", "--help", "--version"):
            raise UsageError(f"沒有 {argv[0]!r} 這個指令：aicomm 只有 send 與 inbox")
        args = _build_parser().parse_args(argv)
        if args.command is None:
            raise UsageError("aicomm 只有 send 與 inbox 兩個指令")
        return _cmd_send(args) if args.command == "send" else _cmd_inbox(args)
    except UsageError as e:
        sys.stderr.write(f"aicomm: {e}\n")
        return EXIT_USAGE
    except SystemExit as e:  # --help / --version
        return e.code if isinstance(e.code, int) else EXIT_OK

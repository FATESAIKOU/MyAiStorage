"""aicomm: send a message to another AI (and wake it), read what was sent to me.

Two commands only. `send` writes the message with the `angareion` command - that is the
delivery - and then, as a best effort, pokes the recipient on the local sidecar. `inbox`
reads the messages addressed to me with `angareion read`/`wait`. Standard library only;
aicomm talks to Angareion through its command line and to the sidecar through two of its
HTTP endpoints (look up one agent, poke one agent), nothing else.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple, Sequence

from aicomm import __version__

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2
EXIT_TIMEOUT = 3
# `--require-wake`: the message is delivered, but somebody was not woken. Never resend.
EXIT_NOT_WOKEN = 4

# The sidecar's documented default address. Not configurable; `AICOMM_SIDECAR` exists for
# tests and manual runs only (a sidecar-opened agent never sees it).
DEFAULT_SIDECAR = "http://127.0.0.1:8471"
SIDECAR_ENV = "AICOMM_SIDECAR"

GET_TIMEOUT = 3.0
POKE_TIMEOUT = 5.0

# The poke carries this and nothing else: never the sender, the body, a ticket or an id.
WAKE_TEXT = "有新訊息：請執行 aicomm inbox。"

# The reason codes of the `wake:` line (design of MyAiStorage#368 §3.7). The docs list
# exactly these two groups; `state-` and `http-` are prefixes (`state-<state>`,
# `http-<code>`). A new code means changing the design first, then these and the docs.
REASON_NOT_FOUND = "not-found"
REASON_PEER_UNAVAILABLE = "peer-unavailable"
REASON_LOOKUP_INCOMPLETE = "lookup-incomplete"
REASON_PEER_PORT_OUT_OF_RANGE = "peer-port-out-of-range"
REASON_PEER_BAD_ANSWER = "peer-bad-answer"
REASON_BAD_ANSWER = "bad-answer"
REASON_BAD_NAME = "bad-name"
REASON_AGENT_NOT_RUNNING = "agent-not-running"
REASON_AWAITING_ANSWER = "awaiting-answer"
REASON_STATE = "state-"  # `state-<state>`
REASON_HTTP = "http-"    # `http-<code>`
REASON_POKE_TIMEOUT = "poke-timeout"
REASON_POKE_ERROR = "poke-error"
REASON_INTERNAL_ERROR = "internal-error"

# `not-woken`: nobody was poked, or the one that was poked said no. `unknown`: the request
# may have reached somebody - never resend, the message is already delivered.
NOT_WOKEN_CODES = frozenset({
    REASON_NOT_FOUND,
    REASON_PEER_UNAVAILABLE,
    REASON_LOOKUP_INCOMPLETE,
    REASON_PEER_PORT_OUT_OF_RANGE,
    REASON_PEER_BAD_ANSWER,
    REASON_BAD_ANSWER,
    REASON_BAD_NAME,
    REASON_AGENT_NOT_RUNNING,
    REASON_AWAITING_ANSWER,
    REASON_STATE,
    REASON_HTTP,
})
UNKNOWN_CODES = frozenset({
    REASON_POKE_TIMEOUT,
    REASON_POKE_ERROR,
    REASON_PEER_BAD_ANSWER,
    REASON_INTERNAL_ERROR,
})

# The three kinds of a `wake:` line, and the woken code of the local sidecar. A woken code
# is `local` or `peer:<port>[,<port>…]`; the peer form has no producer before #368 PR-2.
WAKE_WOKEN = "woken"
WAKE_NOT_WOKEN = "not-woken"
WAKE_UNKNOWN = "unknown"
WAKE_LOCAL = "local"

TICKET_RE = re.compile(r"^[A-Za-z0-9._-]+#[0-9]+$")
# One recipient gets exactly one `wake:` line, so a reason code is one token: a `state`
# with a blank or a newline cannot be spelled as `state-<state>` and is a `bad-answer`.
STATE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
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
    try:
        parts.port  # a bad port raises ValueError here, not later in the middle of a send
    except ValueError:
        raise ValueError(f"{SIDECAR_ENV} 的埠號不對：{override!r}") from None
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
        if "invalid choice" in message:  # a wrong subcommand name; other mistakes are about arguments
            message += "（aicomm 只有 send、inbox 與 log）"
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
    p_send.add_argument("--ref", action="append", default=[], metavar="NAME=LOCATOR",
                        help="Attach a reference or link (repeatable).")
    p_send.add_argument("--body", required=True, metavar="TEXT", help="Message text; '-' reads it on stdin.")
    group = p_send.add_mutually_exclusive_group()
    group.add_argument("--no-wake", action="store_true", help="Do not poke anybody.")
    group.add_argument("--interrupt", action="store_true",
                       help="Interrupt a running recipient instead of queueing the poke after its turn.")
    p_send.add_argument("--require-wake", action="store_true",
                        help="Exit 4 when a recipient was not woken (the message is still delivered; do not resend).")

    p_inbox = sub.add_parser(
        "inbox",
        help="Read the messages addressed to me.",
        description=(
            "Read the messages addressed to me (angareion read without --channel). A new "
            "identity's first read still gets everything addressed to it from the start, but "
            "older broadcasts only from that moment: use angareion read --since for those."
        ),
    )
    p_inbox.add_argument("--wait", type=float, metavar="SECONDS",
                         help="Block in `angareion wait` first; exit 3 on timeout, 2 on a backend error.")
    p_inbox.add_argument("--mentioned", action="store_true", help="Only messages addressed to or mentioning me.")
    p_inbox.add_argument("--json", action="store_true", help="Print angareion's JSON as it is.")

    # `log` declares no flags: everything after it is forwarded verbatim to
    # `angareion log` (see main()). add_help=False so `--help` is forwarded too.
    sub.add_parser("log", help="Query messages (read-only; thin wrapper over angareion log).", add_help=False)
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
        # The body of an error answer carries the reason (409 says why); a body that cannot
        # be read is simply an empty one.
        try:
            return e.code, e.read()
        except Exception:  # noqa: BLE001 - an unreadable error body is an empty body
            return e.code, b""


class _Wake(NamedTuple):
    """One recipient's result: the kind, the reason code (or the wake target), and the name.

    `code` is `local` or `peer:<port>[,<port>…]` when woken, and a reason code otherwise.
    `line` is the only place that spells the one stderr line; PR-2 merges several peer
    answers from `kind` and `code`, never by parsing that line.
    """

    kind: str
    code: str
    name: str
    interrupt: bool = False

    @property
    def woken(self) -> bool:
        return self.kind == WAKE_WOKEN

    @property
    def line(self) -> str:
        suffix = " interrupt" if self.woken and self.interrupt else ""
        return f"wake: {self.name} {self.kind} {self.code}{suffix}"


def _woken(name: str, interrupt: bool, code: str = WAKE_LOCAL) -> _Wake:
    return _Wake(WAKE_WOKEN, code, name, interrupt)


def _not_woken(name: str, code: str) -> _Wake:
    return _Wake(WAKE_NOT_WOKEN, code, name)


def _unknown(name: str, code: str) -> _Wake:
    return _Wake(WAKE_UNKNOWN, code, name)


def _agent_state(body: bytes) -> str | None:
    """The `state` of a `GET /agents/<id>` answer; None when the answer cannot be read."""
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    state = answer.get("state") if isinstance(answer, dict) else None
    return state if isinstance(state, str) else None


def _refusal_code(body: bytes) -> str:
    """Why a poke got 409: `agent-not-running`, `awaiting-answer`, or plain `http-409`.

    The error code is the body's `code`, else its `error`; anything else (unreadable body,
    neither field a string, a code we do not know) is just the HTTP status.
    """
    try:
        answer = json.loads(body)
    except ValueError:
        answer = None
    for field in ("code", "error"):
        value = answer.get(field) if isinstance(answer, dict) else None
        if isinstance(value, str):
            if value == "agent_not_running":
                return REASON_AGENT_NOT_RUNNING
            if value == "agent_awaiting_answer":
                return REASON_AWAITING_ANSWER
            break
    return f"{REASON_HTTP}409"


def _poke_answer(status: int, body: bytes) -> tuple[str, str]:
    """Read a poke answer as `(kind, code)` - the table the local poke and `pool-peer call` share.

    200 is `woken local`; 409 carries its reason in the body (`_refusal_code`); anything
    else is `http-<status>`. Only this table is shared: the transport-level outcomes (a
    timeout, a broken connection, `call`'s rc) stay with each side's caller.
    """
    if status == 200:
        return WAKE_WOKEN, WAKE_LOCAL
    if status == 409:
        return WAKE_NOT_WOKEN, _refusal_code(body)
    return WAKE_NOT_WOKEN, f"{REASON_HTTP}{status}"


def _wake_elsewhere(name: str, interrupt: bool) -> _Wake:
    """The peer fallback would run here: `pool-peer find` plus `call` (PR-2 of #368).

    PR-1 has no peer fallback, which is the same result as "no `pool-peer` on this
    machine": the recipient is not on this sidecar and nobody else was asked, so it is
    `not-woken peer-unavailable`. PR-2 replaces this body; its call sites (404, lookup
    timeout, sidecar unreachable) and `interrupt` stay as they are.
    """
    return _not_woken(name, REASON_PEER_UNAVAILABLE)


def _wake_one(base: str, name: str, interrupt: bool) -> _Wake:
    """Wake one recipient; returns one result and never raises.

    The message is already delivered when this runs, so whatever goes wrong here (a half
    answer, a reset connection, anything unforeseen) is only "delivered, not woken".
    """
    try:
        return _wake_one_inner(base, name, interrupt)
    except Exception:  # noqa: BLE001 - best-effort edge: nothing may undo a delivery
        # Nobody knows whether a poke went out, so this is `unknown`, not `not-woken`.
        return _unknown(name, REASON_INTERNAL_ERROR)


def _wake_one_inner(base: str, name: str, interrupt: bool) -> _Wake:
    """Look up one agent on the local sidecar and poke it when it is idle or running."""
    quoted = urllib.parse.quote(name, safe="")
    try:
        status, body = _request("GET", f"{base}/agents/{quoted}", None, GET_TIMEOUT)
    except http.client.HTTPException:  # the answer was unreadable; nobody was poked yet
        return _not_woken(name, REASON_BAD_ANSWER)
    except (OSError, urllib.error.URLError):
        # Not on this sidecar as far as we can tell (404 below, no sidecar, or no answer in
        # time). Only the peer fallback may look further, and it has not poked anybody.
        return _wake_elsewhere(name, interrupt)
    if status == 404:
        return _wake_elsewhere(name, interrupt)
    if status != 200:
        return _not_woken(name, f"{REASON_HTTP}{status}")
    state = _agent_state(body)
    if state is None:
        return _not_woken(name, REASON_BAD_ANSWER)
    if state not in WAKEABLE_STATES:
        if not STATE_TOKEN_RE.fullmatch(state):
            return _not_woken(name, REASON_BAD_ANSWER)
        return _not_woken(name, f"{REASON_STATE}{state}")
    payload = {"text": WAKE_TEXT, "interrupt": bool(interrupt and state == "running")}
    try:
        status, body = _request("POST", f"{base}/agents/{quoted}/poke", payload, POKE_TIMEOUT)
    except http.client.HTTPException:  # the request may have reached the sidecar
        return _unknown(name, REASON_POKE_ERROR)
    except (OSError, urllib.error.URLError) as e:
        # The request may have been sent and then lost: `unknown`, never "not woken".
        return _unknown(name, REASON_POKE_TIMEOUT if _is_timeout(e) else REASON_POKE_ERROR)
    kind, code = _poke_answer(status, body)
    if kind == WAKE_WOKEN:
        return _woken(name, payload["interrupt"], code)
    return _Wake(kind, code, name)


def _cmd_send(args: argparse.Namespace) -> int:
    if args.require_wake and args.no_wake:
        # Refused before anything is written: no `angareion send`, no sidecar request.
        raise UsageError("--require-wake 與 --no-wake 不能同時給")
    if args.ticket is not None and not TICKET_RE.fullmatch(args.ticket):
        raise UsageError(f"--ticket 要是 <repo>#<n>：{args.ticket!r}")
    # angareion refuses a ref without "="; refuse it here too, before anything is written.
    for ref in args.ref:
        if "=" not in ref:
            raise UsageError(f"--ref 要是 <name>=<locator>：{ref!r}")

    me = os.environ.get("ANGAREION_IDENTITY")
    candidates: list[str] = []
    for name in ([args.to] if args.to is not None else []) + args.mention:
        if name and name != me and name not in candidates:
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

    # Every value is one `--name=value` token, so a value that starts with "-" is not an option.
    command = ["send", f"--channel={args.channel}"]
    if args.to is not None:
        command.append(f"--to={args.to}")
    for name in args.mention:
        command.append(f"--mention={name}")
    if args.in_reply_to:
        command.append(f"--in-reply-to={args.in_reply_to}")
    if args.urgency is not None:
        command.append(f"--urgency={args.urgency}")
    if args.ticket:
        command.append(f"--ref=ticket={args.ticket}")
    for ref in args.ref:  # the ticket's ref first, then the given ones in order
        command.append(f"--ref={ref}")
    command.append(f"--body={args.body}")
    stdin_text = sys.stdin.read() if args.body == "-" else None

    code, out, err = _angareion(command, stdin_text)
    _relay(out, err)
    if code != 0:
        return code

    missed = False
    for name in candidates if wake else []:
        result = _wake_one(base, name, args.interrupt)
        sys.stderr.write(result.line + "\n")
        missed = missed or not result.woken
    return EXIT_NOT_WOKEN if (args.require_wake and missed) else EXIT_OK


EMPTY_INBOX_HINT = (
    "aicomm: 沒有新信。若剛被叫醒卻是空的，可能已被同一個身分的其他讀者（例如背景讀信任務）讀走；"
    "先看它的輸出，要補讀用 angareion read --since <時刻>（RFC 3339，例如 2026-10-25T03:00:00Z；不移動游標）。"
    "要補查用 aicomm log（不動游標）。"
)


def _is_empty_read(out: str, as_json: bool) -> bool:
    if not out.strip():
        return True
    if as_json:
        try:
            return json.loads(out) == []
        except ValueError:
            return False
    return False


def _cmd_inbox(args: argparse.Namespace) -> int:
    if args.wait is not None:
        if not math.isfinite(args.wait) or args.wait < 0:
            raise UsageError("--wait 要是 0 或正數")
        wait = ["wait"] + (["--mentioned"] if args.mentioned else []) + ["--timeout", f"{args.wait:g}"]
        code, out, err = _angareion(wait)
        _relay(out, err)
        if code != 0:  # 3 = timeout, 2 = backend error: neither is "there is mail"
            return code
    read = ["read"] + (["--mentioned"] if args.mentioned else []) + (["--json"] if args.json else [])
    code, out, err = _angareion(read)
    _relay(out, err)
    if code == EXIT_OK and _is_empty_read(out, args.json):
        sys.stderr.write(EMPTY_INBOX_HINT + "\n")
    return code


def _user_specified_recipient(extras: list[str]) -> bool:
    for tok in extras:
        if not tok.startswith("--"):
            continue
        head = tok.split("=", 1)[0]
        name = head[2:]
        if not name:
            continue
        if "to".startswith(name) or "mentioned".startswith(name):
            return True
    return False


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint for the `aicomm` command; returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = _build_parser()
        args, extras = parser.parse_known_args(argv)
        if args.command is None:
            raise UsageError("aicomm 只有 send、inbox 與 log 三個指令")
        if args.command == "send":
            if extras:
                raise UsageError(f"無法識別的引數：{' '.join(extras)}")
            return _cmd_send(args)
        if args.command == "inbox":
            if extras:
                raise UsageError(f"無法識別的引數：{' '.join(extras)}")
            return _cmd_inbox(args)
        # log: forward extras verbatim to `angareion log`, injecting --to me when needed.
        if not _user_specified_recipient(extras):
            forwarded = ["--to", "me", *extras]
        else:
            forwarded = list(extras)
        code, out, err = _angareion(["log", *forwarded])
        _relay(out, err)
        return code
    except UsageError as e:
        sys.stderr.write(f"aicomm: {e}\n")
        return EXIT_USAGE
    except SystemExit as e:  # --help / --version
        return e.code if isinstance(e.code, int) else EXIT_OK

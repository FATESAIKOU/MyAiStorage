"""aicomm: send a message to another AI (and wake it), read what was sent to me.

Two commands only. `send` writes the message with the `angareion` command - that is the
delivery - and then, as a best effort, pokes the recipient on the local sidecar; a
recipient that is not on this machine is looked up on the others through MyLinuxPool's
`pool-peer` command (`find`, then `call <port> POST /agents/<id>/poke`). `inbox` reads the
messages addressed to me with `angareion read`/`wait`. Standard library only; aicomm talks
to Angareion and to `pool-peer` through their command lines and to the sidecar through two
of its HTTP endpoints (look up one agent, poke one agent), nothing else.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
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

# The peer fallback (design of MyAiStorage#368 §2, §3.8). `pool-peer` is MyLinuxPool's
# command for reaching the sidecar of another machine in the pool: `find` asks every
# machine, `call <port>` makes one HTTP request there. Both take their own `--timeout`;
# aicomm waits a little longer and then kills the child itself.
POOL_PEER_ENV = "AICOMM_POOL_PEER"
POOL_PEER_COMMAND = "pool-peer"
PEER_FIND_TIMEOUT = 10
PEER_CALL_TIMEOUT = 5
PEER_CHILD_GRACE = 2
# The ports a pool sidecar listens on. aicomm rejects anything else before calling, so
# "out of range" does not turn into `pool-peer`'s rc 2 ("cannot connect") and stay hidden.
PEER_PORT_MIN = 5200
PEER_PORT_MAX = 5399

# The poke's text is this and nothing else: never the sender, the body or a ticket. The body
# may also carry `via` and `ref` (MyLinuxPool#786 D3): metadata for the sidecar's wake
# record, never pasted into the recipient's input.
WAKE_TEXT = "有新訊息：請執行 aicomm inbox。"
# `via` is who poked (self-declared, a hint and not proof); both fit `^[a-z][a-z0-9-]{0,31}$`.
POKE_VIA_LOCAL = "aicomm"
POKE_VIA_PEER = "aicomm-peer"
# `ref` is the message id; the sidecar answers 400 to any other shape, so check before sending.
# Kept apart from RECORD_ID_RE below although the shape is the same today.
POKE_REF_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

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
# is `local` or `peer:<port>[,<port>…]` (the ports of the sidecars on other machines).
WAKE_WOKEN = "woken"
WAKE_NOT_WOKEN = "not-woken"
WAKE_UNKNOWN = "unknown"
WAKE_LOCAL = "local"
# `queued` modifies `woken` (the recipient was busy, so the poke waits for its next turn).
# It is never a reason code: `not-woken`/`unknown` keep the codes of `REASON_*`.
WAKE_QUEUED = "queued"

# The optional fields of a poke answer (MyLinuxPool#786, poke-records-v1): the sidecar's
# record id and whether the poke was queued. A record id is used only when it is exactly
# one token of this shape, so no answer can turn one `wake:` line into two or forge a field.
RECORD_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
POKE_OUTCOME_QUEUED = "queued"

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
    `queued` marks a `woken` result whose poke waits for the recipient's next turn, and
    `records` is each poke's sidecar record (`local/<id>` or `peer:<port>/<id>`), already
    carrying its location; the empty tuple means none, and then the line is unchanged.
    `line` is the only place that spells the one stderr line; the peer fallback merges
    several machines' answers from `kind`, `code`, `interrupt`, `queued` and `records`,
    never by parsing it.
    """

    kind: str
    code: str
    name: str
    interrupt: bool = False
    queued: bool = False
    records: tuple[str, ...] = ()

    @property
    def woken(self) -> bool:
        return self.kind == WAKE_WOKEN

    @property
    def line(self) -> str:
        suffix = ""
        if self.woken:
            # ` interrupt` wins: a poke that interrupts never queues.
            if self.interrupt:
                suffix = " interrupt"
            elif self.queued:
                suffix = f" {WAKE_QUEUED}"
        record = f" record={','.join(self.records)}" if self.records else ""
        return f"wake: {self.name} {self.kind} {self.code}{suffix}{record}"


def _woken(name: str, interrupt: bool, code: str = WAKE_LOCAL, *,
           queued: bool = False, records: tuple[str, ...] = ()) -> _Wake:
    return _Wake(WAKE_WOKEN, code, name, interrupt, queued, records)


def _not_woken(name: str, code: str, *, records: tuple[str, ...] = ()) -> _Wake:
    return _Wake(WAKE_NOT_WOKEN, code, name, records=records)


def _unknown(name: str, code: str, *, records: tuple[str, ...] = ()) -> _Wake:
    return _Wake(WAKE_UNKNOWN, code, name, records=records)


def _agent_state(body: bytes) -> str | None:
    """The `state` of a `GET /agents/<id>` answer; None when the answer cannot be read."""
    try:
        answer = json.loads(body)
    except ValueError:
        return None
    state = answer.get("state") if isinstance(answer, dict) else None
    return state if isinstance(state, str) else None


def _answer_fields(body: bytes) -> dict | None:
    """The top-level JSON object of a poke answer, or None when the body cannot be read."""
    try:
        answer = json.loads(body)
    except (ValueError, RecursionError):
        # A body nested too deeply is as unreadable as one that is not JSON: the decoder
        # gives up with RecursionError instead of ValueError (#412).
        return None
    return answer if isinstance(answer, dict) else None


def _record_id(answer: dict | None) -> str | None:
    """The answer's `poke_id` when it is one `[A-Za-z0-9_-]{1,64}` token, else None."""
    value = answer.get("poke_id") if answer is not None else None
    if isinstance(value, str) and RECORD_ID_RE.fullmatch(value):
        return value
    return None


def _error_code(answer: dict | None) -> str | None:
    """The first error code an answer spells: nested `error.code`, `code`, string `error`.

    One body is one error, so the first of those that is a string settles it - a code we do
    not know does not fall through to a second shape (design D-7 / L-4).
    """
    if answer is None:
        return None
    nested = answer.get("error")
    if isinstance(nested, dict):
        value = nested.get("code")
        if isinstance(value, str):
            return value
    for field in ("code", "error"):
        value = answer.get(field)
        if isinstance(value, str):
            return value
    return None


def _refusal_code(answer: dict | None) -> str:
    """Why a poke got 409: `agent-not-running`, `awaiting-answer`, or plain `http-409`.

    The sidecar's error body is nested (`{"error": {"code": …}}`); the flat shapes
    (`{"code": …}`, `{"error": "<string>"}`) stay readable. Anything else - unreadable
    body, no string code, a code we do not know - is just the HTTP status.
    """
    value = _error_code(answer)
    if value == "agent_not_running":
        return REASON_AGENT_NOT_RUNNING
    if value == "agent_awaiting_answer":
        return REASON_AWAITING_ANSWER
    return f"{REASON_HTTP}409"


class _PokeAnswer(NamedTuple):
    """The shared table's reading: the fields `_Wake` describes, before the one line is spelled."""

    kind: str
    code: str
    poke_id: str | None = None
    queued: bool = False


def _poke_answer(status: int, body: bytes) -> _PokeAnswer:
    """Read a poke answer - the table the local poke and `pool-peer call` share.

    200 is `woken local`; 409 carries its reason in the body (`_refusal_code`); anything
    else is `http-<status>`. Any answer may carry a usable `poke_id`; only a 200 may say
    the poke was queued (`poke_outcome`, and only the exact value `queued`). Neither
    changes kind or reason. Only this table is shared: the transport-level outcomes (a
    timeout, a broken connection, `call`'s rc) stay with each side's caller.
    """
    answer = _answer_fields(body)
    poke_id = _record_id(answer)
    if status == 200:
        outcome = answer.get("poke_outcome") if answer is not None else None
        return _PokeAnswer(WAKE_WOKEN, WAKE_LOCAL, poke_id, outcome == POKE_OUTCOME_QUEUED)
    if status == 409:
        return _PokeAnswer(WAKE_NOT_WOKEN, _refusal_code(answer), poke_id)
    return _PokeAnswer(WAKE_NOT_WOKEN, f"{REASON_HTTP}{status}", poke_id)


def _poke_ref(out: str) -> str | None:
    """The message id from `angareion send`'s stdout: its first line, stripped, if it fits."""
    first = out.split("\n", 1)[0].strip()
    return first if POKE_REF_RE.fullmatch(first) else None


def _poke_body(interrupt: bool, via: str, message_id: str | None) -> dict:
    """The one poke body, local and cross-machine alike; no `ref` key without a valid id."""
    body: dict = {"text": WAKE_TEXT, "interrupt": interrupt, "via": via}
    if message_id is not None:
        body["ref"] = message_id
    return body


def _peer_executable() -> str | None:
    """The `pool-peer` to run: the test hook, else the one on PATH (#392), else a worker
    install that left it out of PATH (`~/.mylinuxpool/bin/pool-peer`). None if none.

    The bindir fallback counts only when the file exists and is executable - a file
    without the exec bit is as absent as one that is not there.
    """
    hook = os.environ.get(POOL_PEER_ENV)
    if hook:
        return hook
    on_path = shutil.which(POOL_PEER_COMMAND)
    if on_path:
        return on_path
    try:
        # `Path.home()` can raise RuntimeError (no HOME and no passwd entry for the uid)
        # or OSError (permission trouble walking the home): either way there is no
        # install dir to check, which is as absent as one without the executable (#394).
        in_bin = Path.home() / ".mylinuxpool" / "bin" / POOL_PEER_COMMAND
        if in_bin.is_file() and os.access(in_bin, os.X_OK):
            return str(in_bin)
    except (OSError, RuntimeError):
        pass
    return None


def _peer_child_limit(seconds: int) -> float:
    """How long aicomm waits for one `pool-peer` run: what it gave, plus a grace."""
    return seconds + PEER_CHILD_GRACE


def _run_pool_peer(executable: str, args: list[str], stdin_text: str, limit: float):
    """Run one `pool-peer` argv - never a shell - and return the finished process.

    `errors="replace"`: an answer in bytes that are not UTF-8 is an answer aicomm cannot
    read, and an unreadable one is classified by the ordinary paths (`find` →
    `peer-bad-answer`, `call` → `peer-bad-answer`), never an internal error.
    """
    return subprocess.run(
        [executable, *args],
        input=stdin_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=limit,
    )


def _parse_peers(stdout: str) -> list[tuple[int, str]] | None:
    """The machines `pool-peer find` reported, or None when the answer cannot be read.

    Each usable item is a sidecar `port` (an integer, and not a bool) and the `agent.state`
    of that machine. Anything else is dropped; the first item of a port wins. An empty
    result means the answer named nobody usable, which is as unreadable as a broken one.
    """
    try:
        answer = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(answer, list):
        return None
    peers: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in answer:
        if not isinstance(item, dict):
            continue
        port = item.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            continue
        agent = item.get("agent")
        state = agent.get("state") if isinstance(agent, dict) else None
        if not isinstance(state, str):
            continue
        if port in seen:
            continue
        seen.add(port)
        peers.append((port, state))
    return peers or None


def _lookup_peers(executable: str, name: str) -> tuple[str | None, list[tuple[int, str]]]:
    """`pool-peer find <name> --json`; returns (reason code, []) or (None, the machines)."""
    args = ["find", name, "--json", "--timeout", str(PEER_FIND_TIMEOUT)]
    try:
        proc = _run_pool_peer(executable, args, "", _peer_child_limit(PEER_FIND_TIMEOUT))
    except subprocess.TimeoutExpired:  # nobody was poked: this is "not woken", not "unknown"
        return REASON_LOOKUP_INCOMPLETE, []
    except OSError:  # the child never started
        return REASON_PEER_UNAVAILABLE, []
    if proc.returncode == 0:
        peers = _parse_peers(proc.stdout)
        if peers is None:
            return REASON_PEER_BAD_ANSWER, []
        return None, peers
    if proc.returncode == 1:
        return REASON_NOT_FOUND, []
    if proc.returncode == 2:
        return REASON_PEER_UNAVAILABLE, []
    if proc.returncode == 3:
        return REASON_LOOKUP_INCOMPLETE, []
    return REASON_PEER_BAD_ANSWER, []  # an rc outside the contract


def _call_answer(stdout: str) -> tuple[int | None, bytes]:
    """Read `pool-peer call`'s stdout: the whole first line is the HTTP status, then the body.

    None means the first line is not an HTTP status at all - the peer answered something,
    so the poke may have gone through.
    """
    first, _, rest = stdout.partition("\n")
    # The whole first line, not its first token: `200 OK` is not a status line (design §2).
    head = first.strip()
    if not re.fullmatch(r"[0-9]{3}", head):
        return None, b""
    status = int(head)
    if not 100 <= status <= 599:
        return None, b""
    return status, rest.encode("utf-8")


def _poke_peer(executable: str, name: str, port: int, state: str, interrupt: bool,
               message_id: str | None) -> _Wake:
    """Poke one machine's sidecar through `pool-peer call`, with the local poke's body."""
    payload = _poke_body(bool(interrupt and state == "running"), POKE_VIA_PEER, message_id)
    quoted = urllib.parse.quote(name, safe="")
    args = ["call", str(port), "POST", f"/agents/{quoted}/poke", "--timeout", str(PEER_CALL_TIMEOUT)]
    try:
        proc = _run_pool_peer(executable, args, json.dumps(payload), _peer_child_limit(PEER_CALL_TIMEOUT))
    except subprocess.TimeoutExpired:  # the request may have arrived: never resend
        return _unknown(name, REASON_POKE_TIMEOUT)
    except OSError:  # the child never started
        return _not_woken(name, REASON_PEER_UNAVAILABLE)
    if proc.returncode == 2:
        return _not_woken(name, REASON_PEER_UNAVAILABLE)
    if proc.returncode == 3:
        return _unknown(name, REASON_POKE_TIMEOUT)
    if proc.returncode != 0:  # `pool-peer` did not answer as agreed: was it sent?
        return _unknown(name, REASON_PEER_BAD_ANSWER)
    status, body = _call_answer(proc.stdout)
    if status is None:
        return _unknown(name, REASON_PEER_BAD_ANSWER)
    answer = _poke_answer(status, body)  # the same table as the local poke
    records = (f"peer:{port}/{answer.poke_id}",) if answer.poke_id else ()
    if answer.kind == WAKE_WOKEN:
        return _woken(name, payload["interrupt"], f"peer:{port}",
                      queued=answer.queued, records=records)
    return _Wake(answer.kind, answer.code, name, records=records)


def _merge_peers(name: str, answers: list[tuple[int, _Wake]]) -> _Wake:
    """One recipient on several machines becomes one result (design of #368 §3.7, #406 D-3).

    Any 200 is `woken` (the successful ports, in order); otherwise any `unknown` wins;
    otherwise `not-woken`. A reason code comes from the lowest port that has one.
    `records` lists every poke that carried a usable id (failed machines too), lowest port
    first; `queued` needs every successful poke to say so.
    """
    ordered = sorted(answers, key=lambda pair: pair[0])
    if not ordered:  # cannot happen with a parsed answer; keep one line per recipient anyway
        return _not_woken(name, REASON_PEER_BAD_ANSWER)
    records = tuple(record for _, answer in ordered for record in answer.records)
    woken = [(port, answer) for port, answer in ordered if answer.woken]
    if woken:
        ports = ",".join(str(port) for port, _ in woken)
        return _woken(name, any(answer.interrupt for _, answer in woken), f"peer:{ports}",
                      queued=all(answer.queued for _, answer in woken), records=records)
    for _, answer in ordered:
        if answer.kind == WAKE_UNKNOWN:
            return _unknown(name, answer.code, records=records)
    return _not_woken(name, ordered[0][1].code, records=records)


def _wake_elsewhere(name: str, interrupt: bool, message_id: str | None) -> _Wake:
    """Look on the other machines through `pool-peer` and poke every sidecar that can take it.

    Runs only when the local sidecar said 404, could not be reached, or did not answer in
    time - nobody was poked yet, so every outcome here is about the peers alone. The
    peer's own output is never relayed: only a reason code reaches the `wake:` line.
    """
    if name.startswith("-"):
        # The name would be read as an option by `pool-peer`: refuse before handing it over.
        return _not_woken(name, REASON_BAD_NAME)
    executable = _peer_executable()
    if executable is None:
        return _not_woken(name, REASON_PEER_UNAVAILABLE)
    reason, peers = _lookup_peers(executable, name)
    if reason is not None:
        return _not_woken(name, reason)
    answers: list[tuple[int, _Wake]] = []
    for port, state in peers:
        if state not in WAKEABLE_STATES:
            # `state-<state>` must stay one token on the one line this recipient gets.
            code = f"{REASON_STATE}{state}" if STATE_TOKEN_RE.fullmatch(state) else REASON_PEER_BAD_ANSWER
            answers.append((port, _not_woken(name, code)))
        elif not PEER_PORT_MIN <= port <= PEER_PORT_MAX:
            answers.append((port, _not_woken(name, REASON_PEER_PORT_OUT_OF_RANGE)))
        else:
            answers.append((port, _poke_peer(executable, name, port, state, interrupt, message_id)))
    return _merge_peers(name, answers)


def _wake_one(base: str, name: str, interrupt: bool, message_id: str | None) -> _Wake:
    """Wake one recipient; returns one result and never raises.

    The message is already delivered when this runs, so whatever goes wrong here (a half
    answer, a reset connection, anything unforeseen) is only "delivered, not woken".
    """
    try:
        return _wake_one_inner(base, name, interrupt, message_id)
    except Exception:  # noqa: BLE001 - best-effort edge: nothing may undo a delivery
        # Nobody knows whether a poke went out, so this is `unknown`, not `not-woken`.
        return _unknown(name, REASON_INTERNAL_ERROR)


def _wake_one_inner(base: str, name: str, interrupt: bool, message_id: str | None) -> _Wake:
    """Look up one agent on the local sidecar and poke it when it is idle or running."""
    quoted = urllib.parse.quote(name, safe="")
    try:
        status, body = _request("GET", f"{base}/agents/{quoted}", None, GET_TIMEOUT)
    except http.client.HTTPException:  # the answer was unreadable; nobody was poked yet
        return _not_woken(name, REASON_BAD_ANSWER)
    except (OSError, urllib.error.URLError):
        # Not on this sidecar as far as we can tell (404 below, no sidecar, or no answer in
        # time). Only the peer fallback may look further, and it has not poked anybody.
        return _wake_elsewhere(name, interrupt, message_id)
    if status == 404:
        return _wake_elsewhere(name, interrupt, message_id)
    if status != 200:
        return _not_woken(name, f"{REASON_HTTP}{status}")
    state = _agent_state(body)
    if state is None:
        return _not_woken(name, REASON_BAD_ANSWER)
    if state not in WAKEABLE_STATES:
        if not STATE_TOKEN_RE.fullmatch(state):
            return _not_woken(name, REASON_BAD_ANSWER)
        return _not_woken(name, f"{REASON_STATE}{state}")
    payload = _poke_body(bool(interrupt and state == "running"), POKE_VIA_LOCAL, message_id)
    try:
        status, body = _request("POST", f"{base}/agents/{quoted}/poke", payload, POKE_TIMEOUT)
    except http.client.HTTPException:  # the request may have reached the sidecar
        return _unknown(name, REASON_POKE_ERROR)
    except (OSError, urllib.error.URLError) as e:
        # The request may have been sent and then lost: `unknown`, never "not woken".
        return _unknown(name, REASON_POKE_TIMEOUT if _is_timeout(e) else REASON_POKE_ERROR)
    answer = _poke_answer(status, body)
    records = (f"local/{answer.poke_id}",) if answer.poke_id else ()
    if answer.kind == WAKE_WOKEN:
        return _woken(name, payload["interrupt"], answer.code,
                      queued=answer.queued, records=records)
    return _Wake(answer.kind, answer.code, name, records=records)


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
    # One id for every poke of this send (every recipient, every machine).
    message_id = _poke_ref(out) if wake else None
    for name in candidates if wake else []:
        result = _wake_one(base, name, args.interrupt, message_id)
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

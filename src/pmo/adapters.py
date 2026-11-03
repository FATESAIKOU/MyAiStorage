"""The real ports, wired to this machine's commands (design §2.3, §3.3).

T-2 wires Angareion only: `send` runs `angareion send --channel --to
[--in-reply-to] [--urgency] [--ref] --body -` and answers with the message id
the backend prints; `read` runs `angareion read --json [--since]` and turns
the records into `pmo.ports.Message`s. The aicomm、sidecar and GitHub ports
are wired by their own tickets (§5.2 T-4a..T-5); until then the core only ever
gets them through `Deps`, never importing this module behind its back.
"""

from __future__ import annotations

import json
import subprocess

from .ports import Message, PortError

__all__ = ["Angareion"]

#: The CLI the port wraps; tests swap this path in tests, never the real one.
ANGAREION_COMMAND = "angareion"

_TIMEOUT = 60.0


def _run(args: list[str], body: str | None = None) -> str:
    # The port does not check the identity: the guard is the core's job
    # (assert_pmo_identity), and `angareion` reads ANGAREION_IDENTITY itself.
    # `body` goes in on stdin (`--body -`); `None` leaves stdin alone.
    try:
        proc = subprocess.run(
            args,
            input=body,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as e:
        raise PortError(f"{ANGAREION_COMMAND} 指令不在 PATH 上") from e
    except subprocess.TimeoutExpired as e:
        raise PortError(f"{ANGAREION_COMMAND} 逾時") from e
    except OSError as e:
        raise PortError(f"{ANGAREION_COMMAND} 起不來：{e}") from e
    if proc.returncode != 0:
        raise PortError(f"{ANGAREION_COMMAND} exit {proc.returncode}：{(proc.stderr or '').strip()}")
    return (proc.stdout or "").strip()


class Angareion:
    """The Angareion seam over the real `angareion` CLI."""

    def send(
        self,
        *,
        channel: str,
        to: str,
        body: str,
        in_reply_to: str | None = None,
        urgency: int | None = None,
        ref: str | None = None,
    ) -> str:
        args = [ANGAREION_COMMAND, "send", "--channel", channel, "--to", to]
        if in_reply_to is not None:
            args += ["--in-reply-to", in_reply_to]
        if urgency is not None:
            args += ["--urgency", str(urgency)]
        if ref is not None:
            args += ["--ref", ref]
        args += ["--body", "-"]
        return _run(args, body=body)

    def read(self, *, since: str | None = None) -> list[Message]:
        args = [ANGAREION_COMMAND, "read", "--json"]
        if since is not None:
            args += ["--since", since]
        out = _run(args)
        try:
            records = json.loads(out)
        except json.JSONDecodeError as e:
            raise PortError(f"angareion read 的輸出不是 JSON：{e}") from e
        if not isinstance(records, list):
            raise PortError("angareion read 的輸出不是訊息陣列")
        return [_message_of(record) for record in records]

    def log(
        self,
        *,
        channel: str | None = None,
        sender: str | None = None,
        to: str | None = None,
        thread: str | None = None,
        grep: str | None = None,
        since: str | None = None,
    ) -> list[Message]:
        """`log` is rebuild's seam (T-3); until then it refuses, not guesses."""
        raise NotImplementedError("angareion log 的接線是 T-3（帳本重建）的工作")


def _message_of(record: object) -> Message:
    if not isinstance(record, dict):
        raise PortError("angareion read 的每一筆都必須是物件")
    mentions = record.get("mentions")
    if isinstance(mentions, str):
        mentions = (mentions,)
    elif isinstance(mentions, (list, tuple)):
        mentions = tuple(str(m) for m in mentions)
    else:
        mentions = None
    try:
        return Message(
            message_id=str(record.get("id") or ""),
            channel=str(record.get("channel") or ""),
            sender=str(record.get("from") or record.get("sender") or ""),
            to=(str(record["to"]) if record.get("to") is not None else None),
            received_at=str(record.get("received_at") or ""),
            body=str(record.get("content") or record.get("body") or ""),
            urgency=(
                int(record["urgency"]) if record.get("urgency") is not None else None
            ),
            in_reply_to=(
                str(record["in_reply_to"]) if record.get("in_reply_to") is not None else None
            ),
            mentions=mentions,
        )
    except (TypeError, ValueError) as e:
        raise PortError(f"angareion read 的訊息欄位不合格式：{e}") from e

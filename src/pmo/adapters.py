"""The real ports, wired to this machine's commands (design §2.3, §3.3).

T-2 wires Angareion: `send` runs `angareion send --channel --to
[--in-reply-to] [--urgency] [--ref] --body -` and answers with the message id
the backend prints; `read` runs `angareion read --json [--since]` and turns
the records into `pmo.ports.Message`s. T-4a wires GitHub: `gh issue view <n>
--repo <owner/repo> --json title,body`, read-only. T-4c wires aicomm (`pmo
reply`; write plus wake) and the sidecar HTTP calls `pmo poke`／`status`／
`close` need; T-4b adds `GET /agents` (`pmo check`) and `POST /agents`
(`pmo open`). The remaining sidecar call (`create_job`) comes with T-5.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from .ports import (
    AgentRecord,
    AicommTimeout,
    IssueData,
    JobRecord,
    Message,
    PortError,
    SidecarError,
    SidecarShapeError,
    seconds_of,
    sidecar_address,
)

__all__ = ["Aicomm", "Angareion", "GitHub", "Sidecar"]

#: The CLI the Angareion port wraps; tests swap this path in tests, never the real one.
ANGAREION_COMMAND = "angareion"

#: The CLI the aicomm port wraps (`aicomm send`: write into a channel and wake).
AICOMM_COMMAND = "aicomm"

#: The GitHub CLI the GitHub port wraps (`gh issue view`, read-only).
GH_COMMAND = "gh"

_TIMEOUT = 60.0

#: One sidecar HTTP call's timeout (design §3.6 uses 10 seconds per call).
_SIDECAR_TIMEOUT = 10.0

#: Loopback sidecars are never reached through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: The fields of a `GET /jobs` record the adapter reads; the rest stays in `extra`.
#: `auto` deliberately stays in `extra`: `pmo.ops.is_auto_job` reads it from
#: either the job field or `args`, and `to_dict` (D-12) does not print it.
_KNOWN_JOB = ("id", "action", "args", "every", "at", "last_run", "last_error")

#: The fields of one agent record the adapter reads; the rest stays in `extra`.
#: `attach` and `input_unconfirmed_at` are read into their own fields (#356 裁定:
#: `attach.local` is what `pmo open`／`status` print; `input_unconfirmed_at` is
#: how「卡在 starting」is told apart from a normal start). `provider` is the
#: provider the seat is actually using, which `[no-candidate]` prints (#358).
_KNOWN_AGENT = (
    "id",
    "agent_id",
    "state",
    "exit",
    "session",
    "provider",
    "attach",
    "input_unconfirmed_at",
)


def _run(
    args: list[str],
    body: str | None = None,
    command: str = ANGAREION_COMMAND,
    *,
    timeout_error: type[PortError] = PortError,
) -> str:
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
        raise PortError(f"{command} 指令不在 PATH 上") from e
    except subprocess.TimeoutExpired as e:
        raise timeout_error(f"{command} 逾時") from e
    except OSError as e:
        raise PortError(f"{command} 起不來：{e}") from e
    if proc.returncode != 0:
        raise PortError(
            f"{command} exit {proc.returncode}：{(proc.stderr or '').strip()}",
            returncode=proc.returncode,
        )
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
        """`log` lists the past without moving the cursor (T-3, rebuild's seam).

        Flags check `angareion log --help` (2026-11-03): `--channel` (repeatable),
        `--from`, `--to`, `--thread`, `--grep`, `--since`, `--json` — the bits
        rebuild needs (listing with `--channel --to`, the per-work threads with
        `--from --thread`). The records are `read --json`'s shape, so they go
        through the same record reader.
        """
        args: list[str] = [ANGAREION_COMMAND, "log"]
        if channel is not None:
            args += ["--channel", channel]
        if sender is not None:
            args += ["--from", sender]
        if to is not None:
            args += ["--to", to]
        if thread is not None:
            args += ["--thread", thread]
        if grep is not None:
            args += ["--grep", grep]
        if since is not None:
            args += ["--since", since]
        args.append("--json")
        out = _run(args)
        try:
            records = json.loads(out)
        except json.JSONDecodeError as e:
            raise PortError(f"angareion log 的輸出不是 JSON：{e}") from e
        if not isinstance(records, list):
            raise PortError("angareion log 的輸出不是訊息陣列")
        return [_message_of(record, "angareion log") for record in records]


class Aicomm:
    """The aicomm seam over the real `aicomm` CLI (`pmo reply`, design §3.3).

    `aicomm send` writes the message into Angareion first and wakes the
    recipient second, and prints the message id on its first stdout line. A
    child timeout is `AicommTimeout`, not a plain failure: the message may
    already be written, so the caller must say「送出結果不明，不要直接重送」.
    """

    def send(
        self,
        *,
        channel: str,
        to: str,
        body: str,
        ticket: str | None = None,
        urgency: int | None = None,
        no_wake: bool = False,
    ) -> str:
        args = [AICOMM_COMMAND, "send", f"--channel={channel}", f"--to={to}"]
        if ticket is not None:
            args.append(f"--ticket={ticket}")
        if urgency is not None:
            args.append(f"--urgency={urgency}")
        if no_wake:
            args.append("--no-wake")
        args.append("--body=-")
        out = _run(args, body=body, command=AICOMM_COMMAND, timeout_error=AicommTimeout)
        first = out.splitlines()[0].strip() if out else ""
        if not first:
            raise PortError("aicomm send 沒有印出訊息 id")
        return first


def _message_of(record: object, what: str = "angareion read") -> Message:
    if not isinstance(record, dict):
        raise PortError(f"{what} 的每一筆都必須是物件")
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
        raise PortError(f"{what} 的訊息欄位不合格式：{e}") from e


class GitHub:
    """The GitHub seam over the real `gh` CLI, read-only (design §3.3).

    Only `gh issue view <n> --repo <owner/repo> --json title,body` is used:
    `pmo send` needs the issue's title and body to put them in the dispatch
    letter. An empty body is `null` from `gh`; it is read as the empty string.
    """

    def issue_view(self, repo: str, number: int) -> IssueData:
        out = _run(
            [
                GH_COMMAND,
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "title,body",
            ],
            command=GH_COMMAND,
        )
        try:
            record = json.loads(out)
        except json.JSONDecodeError as e:
            raise PortError(f"gh issue view 的輸出不是 JSON：{e}") from e
        if not isinstance(record, dict):
            raise PortError("gh issue view 的輸出不是物件")
        title = record.get("title")
        body = record.get("body")
        if not isinstance(title, str):
            raise PortError("gh issue view 的輸出缺少 title 字串")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise PortError("gh issue view 的輸出中 body 不是字串")
        return IssueData(title=title, body=body)


class Sidecar:
    """The sidecar seam over HTTP (`pmo poke`／`status`／`close`／`open`, design §3.3).

    One instance points at one sidecar (`<http(s) address>`); `Deps.sidecar_for`
    builds one per address, because a team's `sidecar` and the PMO's
    `pmo_sidecar` need not be the same machine (#356, design §3.1). The calls
    are `GET /agents`, `GET /agents/<id>`, `POST /agents`,
    `POST /agents/<id>/poke`, `POST /agents/<id>/close`, `GET /jobs` and
    `DELETE /jobs/<id>`; `POST /jobs` comes with T-5.

    Errors keep only known fields (D-12): the HTTP status and the body's
    `code` (`workdir_not_found`, `agent_not_running`, …). The body's message
    text is never copied into an error string.
    """

    def __init__(self, base: str, *, timeout: float = _SIDECAR_TIMEOUT) -> None:
        self.base = sidecar_address(base)
        self.timeout = timeout

    # -- HTTP --------------------------------------------------------------- #

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except OSError:  # the refusal's body is optional; the code is all we print
                body = b""
            return e.code, body
        except (OSError, urllib.error.URLError) as e:
            raise SidecarError(detail=f"sidecar 連不上（{type(e).__name__}）") from e

    @staticmethod
    def _error_of(status: int, body: bytes) -> SidecarError:
        code: str | None = None
        try:
            record = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            record = None
        if isinstance(record, dict):
            for name in ("code", "error"):
                value = record.get(name)
                if isinstance(value, str) and value:
                    code = value
                    break
        return SidecarError(status=status, code=code, detail=code or f"sidecar 回 HTTP {status}")

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
        status, body = self._request(method, path, payload)
        if not 200 <= status < 300:
            raise self._error_of(status, body)
        return status, body

    # -- agents ------------------------------------------------------------- #

    def list_agents(self) -> list[AgentRecord]:
        """`GET /agents`: every seat on this sidecar.

        The answer is `{"agents": [ … ]}` and nothing else (sidecar.md §2.2;
        #356 裁定 §3). An outer that is not an object, or an `agents` that is
        not an array, is a `SidecarShapeError` tagged with the 2xx status it came
        with, so `pmo check` can say「回應形狀不符」instead of「沒有回應」—
        the sidecar is up, the two versions just talk different shapes.
        """
        status, body = self._call("GET", "/agents")
        try:
            return _agents_of(body)
        except SidecarShapeError as error:
            raise error.at_status(status) from None

    def get_agent(self, agent_id: str) -> AgentRecord:
        """`GET /agents/<id>`; a 404 is `SidecarError(code='agent_not_found')`."""
        quoted = urllib.parse.quote(agent_id, safe="")
        _status, body = self._call("GET", f"/agents/{quoted}")
        return _agent_of(_json_object(body, "GET /agents/<id>"), agent_id)

    def open_agent(
        self,
        *,
        agent_id: str,
        agent: str,
        workdir: str,
        input: str,
        provider: str | None = None,
        model: str | None = None,
        tui: bool = False,
        session: str | None = None,
        fresh: bool = False,
        agent_args: tuple[str, ...] | None = None,
    ) -> AgentRecord:
        """`POST /agents` with the ruling's request shape (#356 裁定 §2).

        Always sent: `id`, `agent`, `workdir`, `input`. Sent only when they
        apply: `tui` (interactive), `fresh` (tui only), `session` (`--session`;
        the whole key is left out on a new open — `null` is a 400), `provider`
        and `model` (a `provider` alone is legal; both absent lets the sidecar
        pick), and `agent_args` with `agent_args_mode: "append"`. `subscription`
        is never sent: it is only for pinning one seat, which this phase does
        not do. The answer is one agent record, read with the same known fields
        as `GET /agents/<id>`; success is the sidecar's `201`, which `_call`'s
        2xx check already accepts.
        """
        payload: dict[str, object] = {
            "id": agent_id,
            "agent": agent,
            "workdir": workdir,
            "input": input,
        }
        if tui:
            payload["tui"] = True
        if fresh:
            payload["fresh"] = True
        if session is not None:
            payload["session"] = session
        if provider is not None:
            payload["provider"] = provider
        if model is not None:
            payload["model"] = model
        if agent_args:
            payload["agent_args"] = list(agent_args)
            payload["agent_args_mode"] = "append"
        _status, body = self._call("POST", "/agents", payload)
        return _agent_of(_json_object(body, "POST /agents"), agent_id)

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        """`POST /agents/<id>/poke` with `{"text", "interrupt"}` (design §3.6).

        Returns the status the sidecar answered with, so `pmo poke` prints the
        real code (a 204 is not quietly reported as a 200).
        """
        quoted = urllib.parse.quote(agent_id, safe="")
        status, _body = self._call(
            "POST", f"/agents/{quoted}/poke", {"text": text, "interrupt": bool(interrupt)}
        )
        return status

    def close_agent(self, agent_id: str) -> None:
        """`POST /agents/<id>/close`; a 409 `close_in_progress` is the caller's to poll."""
        quoted = urllib.parse.quote(agent_id, safe="")
        self._call("POST", f"/agents/{quoted}/close")

    # -- jobs --------------------------------------------------------------- #

    def list_jobs(self) -> list[JobRecord]:
        """`GET /jobs`; the answer is `{"jobs": [...]}` (sidecar `http.py`).

        The sidecar wraps the list in an object (`http.py`:
        `{"jobs": [job.to_dict() ...]}`, sidecar.md「`GET /jobs` →
        `{"jobs": [ … ]}`」); an outer that is not an object, or a `jobs` that
        is not an array, is the only shape refused here (#370 高-1). `every`
        comes back in seconds (`600.0`), and an auto-reopen job is an `at` job
        with `every: null` (#370 高-2).
        """
        _status, body = self._call("GET", "/jobs")
        records = _json_object(body, "GET /jobs").get("jobs")
        if not isinstance(records, list):
            raise SidecarShapeError(detail="GET /jobs 的輸出不是 job 陣列")
        return [_job_of(record) for record in records]

    def delete_job(self, job_id: str) -> bool:
        """`DELETE /jobs/<id>`; reports whether such a job existed."""
        quoted = urllib.parse.quote(job_id, safe="")
        status, body = self._request("DELETE", f"/jobs/{quoted}")
        if status == 404:
            return False
        if not 200 <= status < 300:
            raise self._error_of(status, body)
        return True


def _json_object(body: bytes, what: str) -> dict:
    try:
        record = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeDecodeError) as e:
        raise SidecarShapeError(detail=f"{what} 的輸出不是 JSON（{type(e).__name__}）") from e
    if not isinstance(record, dict):
        raise SidecarShapeError(detail=f"{what} 的輸出不是物件")
    return record


def _agents_of(body: bytes) -> list[AgentRecord]:
    """`GET /agents`'s answer (`{"agents": [...]}`) → the agent records.

    The wrapper is the sidecar's own (sidecar.md §2.2), so anything else — a
    bare array, a different key — is the shape error `pmo check` reports as a
    version mismatch (#356 裁定 §3).
    """
    records = _json_object(body, "GET /agents").get("agents")
    if not isinstance(records, list):
        raise SidecarShapeError(detail="GET /agents 的輸出不是 agent 陣列")
    agents: list[AgentRecord] = []
    for item in records:
        if not isinstance(item, dict):
            raise SidecarShapeError(
                detail="GET /agents 的每一筆 agent 記錄都必須是物件", record=True
            )
        agents.append(_agent_of(item, ""))
    return agents


def _attach_local_of(value: object) -> str | None:
    """`attach.local` as the sidecar sent it; `null` (or no `local`) is `None`.

    Only `local` is read: `tmux` is a transitional alias the sidecar will drop,
    and the caller MUST run the line verbatim rather than parse it (sidecar.md
    §2.1, #356 裁定 §3).
    """
    if not isinstance(value, dict):
        return None
    local = value.get("local")
    return local if isinstance(local, str) else None


def _agent_of(record: dict, fallback_id: str) -> AgentRecord:
    """One `GET /agents/<id>` record → `AgentRecord`, known fields only."""
    if not isinstance(record, dict):
        raise SidecarShapeError(detail="agent 記錄不是物件", record=True)
    agent_id = record.get("id", record.get("agent_id", fallback_id))
    state = record.get("state")
    if not isinstance(agent_id, str) or not agent_id:
        raise SidecarShapeError(detail="agent 記錄沒有 id", record=True)
    if not isinstance(state, str) or not state:
        raise SidecarShapeError(detail=f"agent {agent_id} 的記錄沒有 state", record=True)
    raw_exit = record.get("exit")
    raw_session = record.get("session")
    raw_provider = record.get("provider")
    raw_unconfirmed = record.get("input_unconfirmed_at")
    return AgentRecord(
        agent_id=agent_id,
        state=state,
        exit=dict(raw_exit) if isinstance(raw_exit, dict) else None,
        session=raw_session if isinstance(raw_session, str) else None,
        provider=raw_provider if isinstance(raw_provider, str) else None,
        attach=_attach_local_of(record.get("attach")),
        input_unconfirmed_at=raw_unconfirmed if isinstance(raw_unconfirmed, str) else None,
        extra={key: value for key, value in record.items() if key not in _KNOWN_AGENT},
    )


def _job_of(record: object) -> JobRecord:
    """One `GET /jobs` record → `JobRecord` (unknown fields stay in `extra`).

    `every` is only converted when it is not `None`: an auto-reopen job is an
    `at` job and its `every` is `null`, and `seconds_of(None)` would fail the
    whole list — the one job `pmo close` must delete (#370 高-2). `last_error`
    is the sidecar's `{"status", "code", "message"}` object (or `null`); only
    the code is kept (D-12: no message text), and a plain string still reads
    (#370 低-1).
    """
    if not isinstance(record, dict):
        raise SidecarShapeError(detail="GET /jobs 的每一筆都必須是物件")
    job_id = record.get("id")
    action = record.get("action")
    if not isinstance(job_id, str) or not job_id:
        raise SidecarShapeError(detail="job 記錄沒有 id")
    if not isinstance(action, str) or not action:
        raise SidecarShapeError(detail=f"job {job_id} 的記錄沒有 action")
    args = record.get("args")
    raw_every = record.get("every")
    raw_at = record.get("at")
    if raw_every is None:
        if raw_at is None:
            # A job with neither `every` nor `at` is not a schedule at all; an
            # `at` job always names its one time (sidecar.md: `every` is null,
            # `at` carries the time).
            raise SidecarShapeError(detail=f"job {job_id} 的 every 不合格式（沒有 every，也沒有 at）")
        every: float | None = None
    else:
        try:
            every = seconds_of(raw_every)
        except SidecarError as e:
            raise SidecarShapeError(detail=f"job {job_id} 的 every 不合格式（{e}）") from e
    return JobRecord(
        job_id=job_id,
        action=action,
        args=dict(args) if isinstance(args, dict) else {},
        every=every,
        last_run=record.get("last_run"),
        last_error=_last_error_code(record.get("last_error")),
        at=_at_of(raw_at),
        extra={key: value for key, value in record.items() if key not in _KNOWN_JOB},
    )


def _last_error_code(value: object) -> str | None:
    """The job's last error code only (#370 低-1; D-12 keeps the message out).

    The sidecar sends `{"status": <HTTP 碼>, "code": "<碼>", "message": "…"}`
    or `null`; an older or faked sidecar may send the code as a plain string,
    which still reads. Anything else is no code.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        code = value.get("code")
        return code if isinstance(code, str) and code else None
    return None


def _at_of(value: object) -> str | None:
    """The one-off job's `at` time, as the sidecar sent it (`null` otherwise)."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)

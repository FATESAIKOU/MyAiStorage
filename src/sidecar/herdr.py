"""Herdr client wrapper and fake client for agent sidecar."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Protocol

AGENT_NAME_REGEX = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HERDR_CONTEXT_VARS = (
    "HERDR_ENV",
    "HERDR_WORKSPACE_ID",
    "HERDR_TAB_ID",
    "HERDR_PANE_ID",
)


class HerdrError(RuntimeError):
    """Raised when a herdr CLI command fails."""

    def __init__(self, command: list[str], returncode: int, message: str, raw_error: dict[str, Any] | None = None) -> None:
        super().__init__(f"herdr command {command} failed (code {returncode}): {message}")
        self.command = command
        self.returncode = returncode
        self.message = message
        self.raw_error = raw_error


def check_nesting_env(env: dict[str, str] | None = None) -> list[str]:
    """Return any herdr context variables present in the environment."""
    e = os.environ if env is None else env
    return [k for k in HERDR_CONTEXT_VARS if k in e]


def sanitize_herdr_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dictionary with all HERDR context variables stripped."""
    base = os.environ.copy() if env is None else env.copy()
    for k in HERDR_CONTEXT_VARS:
        base.pop(k, None)
    return base


class HerdrClient(Protocol):
    def tab_create(
        self,
        cwd: str | None = None,
        label: str | None = None,
        env: dict[str, str] | None = None,
        focus: bool = False,
    ) -> dict[str, Any]: ...

    def pane_run(self, pane_id: str, command: str) -> dict[str, Any]: ...

    def agent_rename(self, target: str, name: str | None = None, clear: bool = False) -> dict[str, Any]: ...

    def agent_prompt(
        self,
        target: str,
        text: str,
        wait: bool = False,
        until: list[str] | str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]: ...

    def agent_get(self, target: str) -> dict[str, Any]: ...

    def agent_read(
        self,
        target: str,
        source: str = "recent-unwrapped",
        lines: int = 120,
        format_: str = "text",
    ) -> str: ...

    def pane_wait_output(
        self,
        pane_id: str,
        match: str | None = None,
        regex: str | None = None,
        source: str = "recent",
        lines: int | None = None,
        timeout_ms: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]: ...

    def worktree_create(
        self,
        cwd: str | None = None,
        branch: str | None = None,
        base: str | None = None,
        path: str | None = None,
        label: str | None = None,
        focus: bool = False,
    ) -> dict[str, Any]: ...

    def agent_list(self) -> list[dict[str, Any]]: ...

    def pane_process_info(self, pane_id: str) -> dict[str, Any]: ...

    def agent_send_keys(self, target: str, *keys: str) -> dict[str, Any]: ...

    def pane_close(self, pane_id: str) -> dict[str, Any]: ...

    def status_server(self) -> dict[str, Any]:
        """Show running server status (`herdr status server --json`)."""
        ...


class RealHerdrClient:
    """Subprocess-based client calling herdr subcommands."""

    def __init__(self, executable: str = "herdr", session: str | None = None) -> None:
        self.executable = executable
        self.session = session

    def _run(self, args: list[str], parse_json: bool = True) -> Any:
        cmd = [self.executable]
        if self.session:
            cmd.extend(["--session", self.session])
        cmd.extend(args)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=sanitize_herdr_env(),
        )

        if proc.returncode != 0:
            err_data = None
            err_msg = proc.stderr.strip()
            try:
                err_data = json.loads(proc.stderr)
                if isinstance(err_data, dict) and "error" in err_data:
                    err_msg = err_data["error"].get("message", err_msg)
            except Exception:
                pass
            raise HerdrError(cmd, proc.returncode, err_msg or proc.stdout.strip(), raw_error=err_data)

        if not parse_json:
            return proc.stdout

        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise HerdrError(cmd, proc.returncode, f"Failed to parse JSON response: {proc.stdout}") from e

    def tab_create(
        self,
        cwd: str | None = None,
        label: str | None = None,
        env: dict[str, str] | None = None,
        focus: bool = False,
    ) -> dict[str, Any]:
        args = ["tab", "create"]
        if cwd:
            args.extend(["--cwd", str(cwd)])
        if label:
            args.extend(["--label", label])
        if env:
            for k, v in env.items():
                args.extend(["--env", f"{k}={v}"])
        if focus:
            args.append("--focus")
        else:
            args.append("--no-focus")
        return self._run(args)

    def pane_run(self, pane_id: str, command: str) -> dict[str, Any]:
        return self._run(["pane", "run", pane_id, command])

    def agent_rename(self, target: str, name: str | None = None, clear: bool = False) -> dict[str, Any]:
        if clear:
            return self._run(["agent", "rename", target, "--clear"])
        if not name:
            raise ValueError("name is required when clear is False")
        if not AGENT_NAME_REGEX.match(name):
            raise ValueError(f"Agent name '{name}' does not match pattern [a-z][a-z0-9_-]{{0,31}}")
        return self._run(["agent", "rename", target, name])

    def agent_prompt(
        self,
        target: str,
        text: str,
        wait: bool = False,
        until: list[str] | str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        args = ["agent", "prompt", target, text]
        if wait:
            args.append("--wait")
            if until:
                if isinstance(until, str):
                    args.extend(["--until", until])
                else:
                    for u in until:
                        args.extend(["--until", u])
        if timeout_ms is not None:
            args.extend(["--timeout", str(timeout_ms)])
        return self._run(args)

    def agent_get(self, target: str) -> dict[str, Any]:
        return self._run(["agent", "get", target])

    def agent_read(
        self,
        target: str,
        source: str = "recent-unwrapped",
        lines: int = 120,
        format_: str = "text",
    ) -> str:
        args = ["agent", "read", target, "--source", source, "--lines", str(lines), "--format", format_]
        return self._run(args, parse_json=False)

    def pane_wait_output(
        self,
        pane_id: str,
        match: str | None = None,
        regex: str | None = None,
        source: str = "recent",
        lines: int | None = None,
        timeout_ms: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        args = ["pane", "wait-output"]
        if match:
            args.extend(["--match", match])
        elif regex:
            args.extend(["--regex", regex])
        else:
            raise ValueError("Must specify either match or regex")
        args.extend(["--source", source])
        if lines is not None:
            args.extend(["--lines", str(lines)])
        if timeout_ms is not None:
            args.extend(["--timeout", str(timeout_ms)])
        if raw:
            args.append("--raw")
        args.append(pane_id)
        return self._run(args)

    def worktree_create(
        self,
        cwd: str | None = None,
        branch: str | None = None,
        base: str | None = None,
        path: str | None = None,
        label: str | None = None,
        focus: bool = False,
    ) -> dict[str, Any]:
        args = ["worktree", "create"]
        if cwd:
            args.extend(["--cwd", str(cwd)])
        if branch:
            args.extend(["--branch", branch])
        if base:
            args.extend(["--base", base])
        if path:
            args.extend(["--path", str(path)])
        if label:
            args.extend(["--label", label])
        if focus:
            args.append("--focus")
        else:
            args.append("--no-focus")
        return self._run(args)

    def agent_list(self) -> list[dict[str, Any]]:
        res = self._run(["agent", "list"])
        if isinstance(res, dict) and "result" in res:
            return res["result"].get("agents", [])
        return []

    def pane_process_info(self, pane_id: str) -> dict[str, Any]:
        res = self._run(["pane", "process-info", "--pane", pane_id])
        if isinstance(res, dict) and "result" in res:
            return res["result"].get("process_info", {})
        return {}

    def agent_send_keys(self, target: str, *keys: str) -> dict[str, Any]:
        return self._run(["agent", "send-keys", target, *keys])

    def pane_close(self, pane_id: str) -> dict[str, Any]:
        return self._run(["pane", "close", pane_id])

    def status_server(self) -> dict[str, Any]:
        return self._run(["status", "server", "--json"])


class FakeHerdrClient:
    """In-memory mock Herdr client for testing without a real herdr server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._tab_seq = 0
        self._pane_seq = 0
        self.tabs: dict[str, dict[str, Any]] = {}
        self.panes: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.pane_outputs: dict[str, list[str]] = {}
        self.agent_outputs: dict[str, str] = {}
        self.process_infos: dict[str, dict[str, Any]] = {}
        self.agent_kinds: dict[str, str] = {}
        self.closed_panes: set[str] = set()
        self.sent_keys: list[tuple[str, tuple[str, ...]]] = []
        self.prompts: list[tuple[str, str]] = []
        self.server_status: dict[str, Any] = {
            "status": "running",
            "running": True,
            "version": "0.0.0-fake",
            "compatible": True,
        }
        self.next_server_error: Exception | None = None

    def tab_create(
        self,
        cwd: str | None = None,
        label: str | None = None,
        env: dict[str, str] | None = None,
        focus: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("tab_create", (cwd, label, env, focus), {}))
        self._tab_seq += 1
        self._pane_seq += 1
        tab_id = f"w1:t{self._tab_seq}"
        pane_id = f"w1:p{self._pane_seq}"

        tab_data = {"tab_id": tab_id, "label": label, "cwd": cwd}
        pane_data = {"pane_id": pane_id, "tab_id": tab_id, "cwd": cwd}
        self.tabs[tab_id] = tab_data
        self.panes[pane_id] = pane_data
        self.process_infos[pane_id] = {
            "foreground_process_group_id": 1000 + self._pane_seq,
            "shell_pid": 2000 + self._pane_seq,
            "foreground_processes": [],
            "pane_id": pane_id,
        }

        return {
            "id": "cli:tab:create",
            "result": {
                "tab": tab_data,
                "root_pane": pane_data,
            },
            "type": "tab_created",
        }

    def pane_run(self, pane_id: str, command: str) -> dict[str, Any]:
        self.calls.append(("pane_run", (pane_id, command), {}))
        if pane_id not in self.panes:
            raise HerdrError(["pane", "run"], 1, f"Unknown pane: {pane_id}")
        return {"id": "cli:pane:run", "result": {"pane_id": pane_id, "command": command}}

    def agent_rename(self, target: str, name: str | None = None, clear: bool = False) -> dict[str, Any]:
        self.calls.append(("agent_rename", (target, name, clear), {}))
        if clear:
            self.agents.pop(target, None)
            return {"id": "cli:agent:rename", "result": {"cleared": True}}
        if not name:
            raise ValueError("name is required when clear is False")
        if not AGENT_NAME_REGEX.match(name):
            raise ValueError(f"Agent name '{name}' does not match pattern [a-z][a-z0-9_-]{{0,31}}")

        # Target can be pane_id or existing agent name
        pane_id = target if target in self.panes else self.agents.get(target, {}).get("pane_id", target)
        agent_data = {
            "name": name,
            "pane_id": pane_id,
            "agent_status": "idle",
            "cwd": self.panes.get(pane_id, {}).get("cwd", "/tmp"),
        }
        self.agents[name] = agent_data
        return {"id": "cli:agent:rename", "result": {"agent": agent_data}}

    def agent_prompt(
        self,
        target: str,
        text: str,
        wait: bool = False,
        until: list[str] | str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("agent_prompt", (target, text, wait, until, timeout_ms), {}))
        self.prompts.append((target, text))
        return {"id": "cli:agent:prompt", "result": {"target": target, "submitted": True}}

    def agent_get(self, target: str) -> dict[str, Any]:
        self.calls.append(("agent_get", (target,), {}))
        agent = self.agents.get(target)
        resolved_name = target
        if not agent:
            # Check by pane_id
            for name, a in self.agents.items():
                if a.get("pane_id") == target:
                    agent = a
                    resolved_name = name
                    break
        if not agent:
            raise HerdrError(["agent", "get"], 1, f"Agent '{target}' not found")
        data = dict(agent)
        # Real herdr reports the detected kind in the `agent` field; tests
        # set it via agent_kinds. Absent means "unknown", not a mismatch.
        if resolved_name in self.agent_kinds:
            data["agent"] = self.agent_kinds[resolved_name]
        return {"id": "cli:agent:get", "result": {"agent": data}}

    def agent_read(
        self,
        target: str,
        source: str = "recent-unwrapped",
        lines: int = 120,
        format_: str = "text",
    ) -> str:
        self.calls.append(("agent_read", (target, source, lines, format_), {}))
        return self.agent_outputs.get(target, "")

    def pane_wait_output(
        self,
        pane_id: str,
        match: str | None = None,
        regex: str | None = None,
        source: str = "recent",
        lines: int | None = None,
        timeout_ms: int | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("pane_wait_output", (pane_id, match, regex, source, lines, timeout_ms, raw), {}))
        return {"id": "cli:pane:wait_output", "result": {"matched": True}}

    def worktree_create(
        self,
        cwd: str | None = None,
        branch: str | None = None,
        base: str | None = None,
        path: str | None = None,
        label: str | None = None,
        focus: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("worktree_create", (cwd, branch, base, path, label, focus), {}))
        return self.tab_create(cwd=path or cwd, label=label, focus=focus)

    def agent_list(self) -> list[dict[str, Any]]:
        self.calls.append(("agent_list", (), {}))
        return list(self.agents.values())

    def pane_process_info(self, pane_id: str) -> dict[str, Any]:
        self.calls.append(("pane_process_info", (pane_id,), {}))
        return self.process_infos.get(pane_id, {})

    def agent_send_keys(self, target: str, *keys: str) -> dict[str, Any]:
        self.calls.append(("agent_send_keys", (target, *keys), {}))
        self.sent_keys.append((target, keys))
        return {"id": "cli:agent:send_keys", "result": {"sent": list(keys)}}

    def pane_close(self, pane_id: str) -> dict[str, Any]:
        self.calls.append(("pane_close", (pane_id,), {}))
        self.closed_panes.add(pane_id)
        self.panes.pop(pane_id, None)
        self.process_infos.pop(pane_id, None)
        # Clear any agent tied to this pane
        to_del = [name for name, a in self.agents.items() if a.get("pane_id") == pane_id]
        for name in to_del:
            self.agents.pop(name, None)
        return {"id": "cli:pane:close", "result": {"closed": pane_id}}

    def status_server(self) -> dict[str, Any]:
        """Fake `herdr status server --json` (D11 check uses running/compatible)."""
        self.calls.append(("status_server", (), {}))
        if self.next_server_error is not None:
            err = self.next_server_error
            self.next_server_error = None
            raise err
        return dict(self.server_status)

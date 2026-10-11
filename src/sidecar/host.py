"""Agent host running AI agents inside Herdr panes."""

from __future__ import annotations

import re
import secrets
import signal
import time
from pathlib import Path
from typing import Any, Callable

from sidecar.config import SidecarConfig
from sidecar.herdr import (
    HerdrClient,
    HerdrError,
    check_nesting_env,
    sanitize_herdr_env,
)
from sidecar.registry import AgentRecord, Registry, now_iso


def _sanitize_agent_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"a-{cleaned}" if cleaned else "agent"
    return cleaned[:32]


def default_kill_pg(pgid: int, sig: int) -> None:
    import os
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class AgentHost:
    def __init__(
        self,
        herdr: HerdrClient,
        registry: Registry,
        config: SidecarConfig | None = None,
        reject_nested: bool = True,
        kill_pg_func: Callable[[int, int], None] | None = None,
    ) -> None:
        self.herdr = herdr
        self.registry = registry
        self.config = config
        self.kill_pg = kill_pg_func or default_kill_pg

        if reject_nested:
            nested = check_nesting_env()
            if nested:
                raise RuntimeError(
                    f"Detected HERDR environment variables ({', '.join(nested)}); "
                    "agent sidecar cannot run nested inside a herdr pane."
                )

    def start_agent(
        self,
        identity: str,
        agora_session: str,
        agent_kind: str,
        agent_name: str | None = None,
        fork: bool = True,
        sync_period: int | float | None = 300,
        cwd: Path | str | None = None,
        worktree: bool = False,
        harness_content: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentRecord:
        """Start an agent inside herdr running 'agora continue'."""
        if not agent_name:
            suffix = secrets.token_hex(3)
            agent_name = _sanitize_agent_name(f"{identity}-{suffix}")
        else:
            agent_name = _sanitize_agent_name(agent_name)

        agent_id = f"ag_{int(time.time())}_{secrets.token_hex(4)}"

        # Strip any herdr nesting variables
        clean_env = sanitize_herdr_env(env)

        if worktree:
            res = self.herdr.worktree_create(cwd=cwd, focus=False)
        else:
            res = self.herdr.tab_create(cwd=cwd, env=clean_env, focus=False)

        tab_id = res["result"]["tab"]["tab_id"]
        pane_id = res["result"]["root_pane"]["pane_id"]

        # Build agora continue command
        cmd_parts = ["agora", "continue", "session", agora_session, "--agent", agent_kind]
        if fork:
            cmd_parts.append("--fork")
        if sync_period is not None:
            cmd_parts.extend(["--sync-period", str(sync_period)])
        cmd_str = " ".join(cmd_parts)

        # Launch command in pane
        self.herdr.pane_run(pane_id, cmd_str)

        # Name agent in pane
        self.herdr.agent_rename(pane_id, agent_name)

        # Harness loading if provided
        if harness_content:
            self.herdr.agent_prompt(agent_name, harness_content)

        log_path = str(self.registry.state_dir / "agents" / agent_id / "log")
        record = AgentRecord(
            agent_id=agent_id,
            identity=identity,
            agora_session=agora_session,
            agent_kind=agent_kind,
            herdr_tab_id=tab_id,
            herdr_pane_id=pane_id,
            agent_name=agent_name,
            started_at=now_iso(),
            status="running",
            log_path=log_path,
            updated_at=now_iso(),
        )
        self.registry.register(record)
        return record

    def start_team(
        self,
        identity: str,
        agora_session: str | None = None,
        fork: bool = True,
        sync_period: int | float | None = None,
        cwd: Path | str | None = None,
        worktree: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> AgentRecord:
        """Start a team task using the configured home session, agent kind, and harness.

        Per D4:
        - base command: 'agora continue session <home> --agent <agent> --fork --sync-period <N>'
        - harness: load raw verbatim content as first input (Atelier seam)
        - isolate via --fork
        """
        team_cfg = self.config.teams.get(identity, {}) if self.config else {}

        home = agora_session or team_cfg.get("home_session")
        if not home:
            raise ValueError(f"No home session configured for team '{identity}'")

        agent_kind = team_cfg.get("agent", "opencode")
        harness_path = team_cfg.get("harness_path")
        harness_content = None
        if harness_path:
            hp = Path(harness_path)
            if hp.is_file():
                harness_content = hp.read_text(encoding="utf-8")

        sp = sync_period if sync_period is not None else team_cfg.get("sync_period", 300)
        team_cwd = cwd or team_cfg.get("cwd")
        env = dict(team_cfg.get("env", {}))
        if extra_env:
            env.update(extra_env)

        return self.start_agent(
            identity=identity,
            agora_session=home,
            agent_kind=agent_kind,
            fork=fork,
            sync_period=sp,
            cwd=team_cwd,
            worktree=worktree,
            harness_content=harness_content,
            env=env,
        )

    def terminate_team(self, identity: str, timeout_stage: float = 5.0) -> bool:
        """Terminate all active agents running for the given team/identity."""
        active = [a for a in self.registry.list_agents(status="running") if a.identity == identity]
        if not active:
            return False
        success = True
        for rec in active:
            if not self.terminate_agent(rec.agent_id, timeout_stage=timeout_stage):
                success = False
        return success

    def export_agent_session(self, agent_target: str) -> str | None:
        """Return the Agora session ID where this agent's session is stored.

        Per D4: conversations started with 'agora continue' are automatically
        written back by continue itself. Sidecar does not assemble raw session files.
        """
        rec = self.registry.get(agent_target) or self.registry.get_by_name(agent_target)
        if not rec:
            return None
        return rec.agora_session

    def get_agent_status(self, agent_name: str, agent_kind: str | None = None) -> str:
        """Read agent status, verifying opencode terminal output for 'esc interrupt'."""
        try:
            res = self.herdr.agent_get(agent_name)
            agent_info = res.get("result", {}).get("agent", {})
            status = agent_info.get("agent_status", "unknown")
        except HerdrError:
            return "exited"

        rec = self.registry.get_by_name(agent_name)
        kind = agent_kind or (rec.agent_kind if rec else None)

        if kind == "opencode":
            try:
                screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
                # D2a / D8: for opencode, presence of 'esc interrupt' means it's working
                if "esc interrupt" in screen:
                    return "working"
            except HerdrError:
                pass
        elif kind == "claude":
            try:
                screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
                if "esc to interrupt" in screen:
                    return "working"
            except HerdrError:
                pass

        return status

    def is_working(self, agent_name: str) -> bool:
        """Check whether the agent is actively running a turn."""
        rec = self.registry.get_by_name(agent_name)
        kind = rec.agent_kind if rec else "opencode"
        indicator = "esc to interrupt" if kind == "claude" else "esc interrupt"
        try:
            screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
            if indicator in screen:
                return True
        except Exception:
            pass
        status = self.get_agent_status(agent_name, agent_kind=kind)
        return status == "working"

    def interrupt(self, agent_name: str, timeout: float = 5.0) -> bool:
        """Interrupt an actively running turn using esc keys without killing the agent.

        D3 spike:
        - If not working: does not send esc, returns True immediately.
        - claude: send 'esc' 1 time (shows 'Interrupted · What should Claude do instead?').
        - opencode: send 'esc' 2 times (first time only shows 'esc again to interrupt').
        - wait until working indicator ('esc to interrupt' / 'esc interrupt') disappears from screen.
        """
        if not self.is_working(agent_name):
            return True

        rec = self.registry.get_by_name(agent_name)
        kind = rec.agent_kind if rec else "opencode"
        indicator = "esc to interrupt" if kind == "claude" else "esc interrupt"

        if kind == "claude":
            self.herdr.agent_send_keys(agent_name, "esc")
        else:
            self.herdr.agent_send_keys(agent_name, "esc")
            self.herdr.agent_send_keys(agent_name, "esc")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
                if indicator not in screen:
                    return True
            except Exception:
                return True
            time.sleep(0.02)

        return False

    def send_prompt(
        self,
        agent_name: str,
        text: str,
        interrupt_if_working: bool = False,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Atomically send prompt text to agent without --wait.

        If interrupt_if_working is True and the agent is actively working,
        interrupts via esc before sending the prompt.
        """
        if interrupt_if_working:
            self.interrupt(agent_name, timeout=timeout)
        return self.herdr.agent_prompt(agent_name, text, wait=False)

    def interrupt_and_prompt(
        self,
        agent_name: str,
        text: str,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Interrupt active turn if working and submit prompt."""
        return self.send_prompt(agent_name, text, interrupt_if_working=True, timeout=timeout)

    def recover_agents(self) -> list[AgentRecord]:
        """Compare active registry records against herdr agent list after restart."""
        self.registry.load()
        active_records = self.registry.list_agents(status="running")
        if not active_records:
            return []

        try:
            live_agents = self.herdr.agent_list()
            live_names = {a.get("name") for a in live_agents if a.get("name")}
        except HerdrError:
            live_names = set()

        recovered = []
        for rec in active_records:
            if rec.agent_name in live_names:
                recovered.append(rec)
            else:
                self.registry.update_status(
                    rec.agent_id,
                    "exited",
                    exit_reason="not found in herdr agent list on restart",
                )
        return recovered

    def terminate_agent(
        self,
        agent_target: str,
        timeout_stage: float = 5.0,
        check_alive_func: Callable[[str], bool] | None = None,
    ) -> bool:
        """Three-stage escalation termination: ctrl+c -> SIGTERM -> SIGKILL -> pane close."""
        rec = self.registry.get(agent_target) or self.registry.get_by_name(agent_target)
        if not rec:
            return False

        agent_name = rec.agent_name
        pane_id = rec.herdr_pane_id

        def is_alive() -> bool:
            if check_alive_func:
                return check_alive_func(agent_name)
            try:
                info = self.herdr.pane_process_info(pane_id)
                pgid = info.get("foreground_process_group_id")
                return bool(pgid and pgid > 0)
            except Exception:
                return False

        # Stage 1: Send Ctrl+C
        try:
            self.herdr.agent_send_keys(agent_name, "ctrl+c")
        except HerdrError:
            pass

        time.sleep(min(timeout_stage, 0.05))
        if not is_alive():
            try:
                self.herdr.pane_close(pane_id)
            except HerdrError:
                pass
            self.registry.update_status(rec.agent_id, "terminated", exit_reason="SIGINT (ctrl+c)")
            return True

        # Stage 2: SIGTERM to process group
        try:
            proc_info = self.herdr.pane_process_info(pane_id)
            pgid = proc_info.get("foreground_process_group_id")
            if pgid and pgid > 0:
                self.kill_pg(pgid, signal.SIGTERM)
        except Exception:
            pass

        time.sleep(min(timeout_stage, 0.05))
        if not is_alive():
            try:
                self.herdr.pane_close(pane_id)
            except HerdrError:
                pass
            self.registry.update_status(rec.agent_id, "terminated", exit_reason="SIGTERM")
            return True

        # Stage 3: SIGKILL to process group
        try:
            proc_info = self.herdr.pane_process_info(pane_id)
            pgid = proc_info.get("foreground_process_group_id")
            if pgid and pgid > 0:
                self.kill_pg(pgid, signal.SIGKILL)
        except Exception:
            pass

        try:
            self.herdr.pane_close(pane_id)
        except HerdrError:
            pass

        self.registry.update_status(rec.agent_id, "terminated", exit_reason="SIGKILL escalation")
        return True

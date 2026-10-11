"""Agent host running AI agents inside Herdr panes."""

from __future__ import annotations

import logging
import re
import secrets
import shlex
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from sidecar.config import SidecarConfig
from sidecar.herdr import (
    HerdrClient,
    HerdrError,
    check_nesting_env,
    sanitize_herdr_env,
)
from sidecar.registry import AgentRecord, Registry, now_iso

if TYPE_CHECKING:
    from sidecar.receiver import Receiver

logger = logging.getLogger(__name__)


# Liveness gate for pane wait-output: any rendered output proves the process
# is alive. Readiness (can it take input) is decided by agent get below.
PROMPT_WAIT_REGEX = r"\S"

# Polling steps for readiness and fork-id capture.
READY_POLL_INTERVAL = 0.5
FORK_POLL_INTERVAL = 1.0
# Scrollback window for the fork announcement (it prints before the TUI).
FORK_READ_LINES = 200

# agora continue --fork prints the new session id into the pane on startup
# ("[agora] fork 已存成 <id>"); sidecar parses it to track the task session.
_FORK_SESSION_RE = re.compile(r"fork 已存成 (\S+)")


def _sanitize_agent_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "-", raw.lower()).strip("-_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"a-{cleaned}" if cleaned else "agent"
    return cleaned[:32]


def _has_indicator_in_tail(screen: str, indicator: str, tail_lines: int = 3) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if not lines:
        return False
    tail = lines[-tail_lines:]
    return any(indicator in line for line in tail)


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
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.herdr = herdr
        self.registry = registry
        self.config = config
        self.kill_pg = kill_pg_func or default_kill_pg
        self._sleep = sleeper or time.sleep

        if reject_nested:
            nested = check_nesting_env()
            if nested:
                raise RuntimeError(
                    f"Detected HERDR environment variables ({', '.join(nested)}); "
                    "agent sidecar cannot run nested inside a herdr pane."
                )

    def wait_for_ready(
        self,
        agent_name: str,
        pane_id: str,
        agent_kind: str,
        timeout_ms: int | float = 60000,
        poll_interval: float = READY_POLL_INTERVAL,
    ) -> None:
        """Wait until an agent is ready to receive input (D10, 3.12).

        Pane output only proves liveness; readiness means `herdr agent get`
        reports the expected agent kind (claude/opencode) and idle status.
        Raises RuntimeError on timeout. A missing kind field (older herdr,
        test doubles) skips only the kind check, never the idle check.
        """
        try:
            self.herdr.pane_wait_output(
                pane_id, regex=PROMPT_WAIT_REGEX, timeout_ms=timeout_ms
            )
        except Exception as e:
            raise RuntimeError(
                f"Agent '{agent_name}' showed no output in time: {e}"
            ) from e

        deadline = time.monotonic() + float(timeout_ms) / 1000.0
        last_kind: Any = "<unknown>"
        last_status: Any = "<unknown>"
        while True:
            try:
                info = self.herdr.agent_get(agent_name).get("result", {}).get("agent", {})
            except Exception as e:
                info = {}
                last_kind = f"<agent get failed: {e}>"
                last_status = "<unknown>"
            else:
                last_kind = info.get("agent")
                last_status = info.get("agent_status")
            kind_ok = last_kind is None or last_kind == agent_kind
            if kind_ok and last_status == "idle":
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Agent '{agent_name}' not ready in time: last kind={last_kind!r} "
                    f"status={last_status!r} (expected {agent_kind}/idle)"
                )
            self._sleep(poll_interval)

    def poll_fork_session(
        self,
        agent_name: str,
        timeout_ms: int | float | None = None,
        poll_interval: float = FORK_POLL_INTERVAL,
    ) -> str | None:
        """Poll the pane until agora's fork announcement appears (D10, 3.12).

        Returns the forked Session id, or None on timeout (export then falls
        back to home). timeout_ms=None reads once without waiting.
        """
        if timeout_ms is None:
            try:
                screen = self.herdr.agent_read(
                    agent_name, source="recent-unwrapped", lines=FORK_READ_LINES
                )
            except Exception as e:
                logger.warning(
                    "Could not read pane output for fork session id of '%s': %s",
                    agent_name,
                    e,
                )
                return None
            match = _FORK_SESSION_RE.search(screen or "")
            return match.group(1) if match else None

        deadline = time.monotonic() + float(timeout_ms) / 1000.0
        while True:
            try:
                screen = self.herdr.agent_read(
                    agent_name, source="recent-unwrapped", lines=FORK_READ_LINES
                )
            except Exception as e:
                logger.warning(
                    "Could not read pane output for fork session id of '%s': %s",
                    agent_name,
                    e,
                )
                screen = ""
            match = _FORK_SESSION_RE.search(screen or "")
            if match:
                return match.group(1)
            if time.monotonic() >= deadline:
                logger.warning(
                    "Fork session id never appeared for '%s'; export falls back to home",
                    agent_name,
                )
                return None
            self._sleep(poll_interval)

    def start_agent(
        self,
        identity: str,
        agora_session: str,
        agent_kind: str,
        agent_name: str | None = None,
        fork: bool = True,
        sync_period: int | float | None = 5,
        cwd: Path | str | None = None,
        worktree: bool = False,
        harness_content: str | None = None,
        env: dict[str, str] | None = None,
        wait_timeout_ms: int | None = 60000,
        headers: dict[str, str] | None = None,
        fork_timeout_ms: int | None = None,
        ready_poll_interval: float = READY_POLL_INTERVAL,
        fork_poll_interval: float = FORK_POLL_INTERVAL,
    ) -> AgentRecord:
        """Start an agent inside herdr running 'agora continue'.

        sync_period is in minutes, passed through verbatim to agora's
        --sync-period (D10). env=None means no extra environment, never the
        sidecar process environment (D6/M2). fork_timeout_ms=None reads the
        pane once for the fork announcement; a timeout polls until it
        appears (D10, 3.12).
        """
        if not agent_name:
            suffix = secrets.token_hex(3)
            agent_name = _sanitize_agent_name(f"{identity}-{suffix}")
        else:
            agent_name = _sanitize_agent_name(agent_name)

        agent_id = f"ag_{int(time.time())}_{secrets.token_hex(4)}"

        # Strip any herdr nesting variables. None means an empty extra
        # environment: never inherit the sidecar process environment, which
        # may contain the A2A token (D6).
        clean_env = sanitize_herdr_env(env if env is not None else {})

        if worktree:
            res = self.herdr.worktree_create(cwd=cwd, focus=False)
        else:
            res = self.herdr.tab_create(cwd=cwd, env=clean_env, focus=False)

        tab_id = res["result"]["tab"]["tab_id"]
        pane_id = res["result"]["root_pane"]["pane_id"]

        # Build agora continue command. Every interpolated value comes from
        # config or the wire (message ids), so quote each word: pane_run
        # executes this through a shell (batch3 review: shell injection).
        cmd_parts = ["agora", "continue", "session", agora_session, "--agent", agent_kind]
        if fork:
            cmd_parts.append("--fork")
        if sync_period is not None:
            cmd_parts.extend(["--sync-period", str(sync_period)])
        # 3.9/D11: fork sessions carry which team and which message they are for.
        if fork and headers:
            for key, value in headers.items():
                cmd_parts.extend(["--header", f"{key}={value}"])
        cmd_str = shlex.join(cmd_parts)

        # Launch command in pane
        self.herdr.pane_run(pane_id, cmd_str)

        # Name agent in pane
        self.herdr.agent_rename(pane_id, agent_name)

        # D4/M3/D10-3.12: wait until the agent is ready before sending
        # anything. On timeout, record the evidence and fail loudly instead
        # of sending the harness into the void.
        if wait_timeout_ms is not None:
            try:
                self.wait_for_ready(
                    agent_name,
                    pane_id,
                    agent_kind,
                    timeout_ms=wait_timeout_ms,
                    poll_interval=ready_poll_interval,
                )
            except Exception as e:
                logger.warning("Agent '%s' did not become ready in time: %s", agent_name, e)
                log_path = str(self.registry.state_dir / "agents" / agent_id / "log")
                self.registry.register(
                    AgentRecord(
                        agent_id=agent_id,
                        identity=identity,
                        agora_session=agora_session,
                        agent_kind=agent_kind,
                        herdr_tab_id=tab_id,
                        herdr_pane_id=pane_id,
                        agent_name=agent_name,
                        started_at=now_iso(),
                        status="starting",
                        log_path=log_path,
                        updated_at=now_iso(),
                        exit_reason=f"readiness wait failed: {e}",
                    )
                )
                raise
        # 3.12/D10: the fork announcement only prints after Drive sync,
        # export and saving, seconds after the first output. Poll for it.
        forked_session: str | None = None
        if fork:
            forked_session = self.poll_fork_session(
                agent_name,
                timeout_ms=fork_timeout_ms,
                poll_interval=fork_poll_interval,
            )

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
            extra={"forked_session": forked_session} if forked_session else None,
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
        receiver: Receiver | None = None,
        trigger_message_id: str | None = None,
        fork_timeout_ms: int | None = 60000,
    ) -> AgentRecord:
        """Start a team task using the configured home session, agent kind, and harness.

        Per D4:
        - base command: 'agora continue session <home> --agent <agent> --fork --sync-period <N>'
          where <N> is minutes, passed through verbatim (D10, default 5)
        - harness: load raw verbatim content as first input (Atelier seam),
          only after the agent prompt appears (M3)
        - isolate via --fork
        Per D11 (3.9): fork sessions carry --header sidecar.identity=<role>
        and, when known, --header sidecar.message=<triggering message id>.
        When a receiver is given (the resident `sidecar run` loop owns it),
        queued low-urgency messages for this identity are handed off right
        after the harness (H2/D10).
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

        sp = sync_period if sync_period is not None else team_cfg.get("sync_period", 5)
        team_cwd = cwd or team_cfg.get("cwd")
        env = dict(team_cfg.get("env", {}))
        if extra_env:
            env.update(extra_env)

        fork_headers: dict[str, str] | None = None
        if fork:
            team_identity = team_cfg.get("identity") or {}
            role = team_identity.get("role") or identity
            fork_headers = {"sidecar.identity": str(role)}
            if trigger_message_id:
                fork_headers["sidecar.message"] = str(trigger_message_id)

        rec = self.start_agent(
            identity=identity,
            agora_session=home,
            agent_kind=agent_kind,
            fork=fork,
            sync_period=sp,
            cwd=team_cwd,
            worktree=worktree,
            harness_content=harness_content,
            env=env,
            headers=fork_headers,
            fork_timeout_ms=fork_timeout_ms,
        )

        if receiver is not None:
            try:
                receiver.deliver_hand_off(identity, rec.agent_name)
            except Exception as e:
                logger.warning(
                    "Hand-off delivery failed for team '%s' agent '%s': %s "
                    "(messages stay queued for the next hand-off)",
                    identity,
                    rec.agent_name,
                    e,
                )

        return rec

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
        With --fork the dialogue lives in the forked Session (L3), not home.
        """
        rec = self.registry.get(agent_target) or self.registry.get_by_name(agent_target)
        if not rec:
            return None
        if rec.extra and rec.extra.get("forked_session"):
            return rec.extra["forked_session"]
        return rec.agora_session

    def get_agent_status(self, agent_name: str, agent_kind: str | None = None) -> str:
        """Read agent status, verifying opencode/claude status bar for interrupt indicator."""
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
                # D2a / D8: for opencode, presence of 'esc interrupt' in status bar means it's working
                if _has_indicator_in_tail(screen, "esc interrupt", tail_lines=3):
                    return "working"
            except HerdrError:
                pass
        elif kind == "claude":
            try:
                screen = self.herdr.agent_read(agent_name, source="recent-unwrapped", lines=15)
                if _has_indicator_in_tail(screen, "esc to interrupt", tail_lines=3):
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
            if _has_indicator_in_tail(screen, indicator, tail_lines=3):
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
                if not _has_indicator_in_tail(screen, indicator, tail_lines=3):
                    return True
            except Exception as e:
                logger.warning("Error reading agent screen during interrupt: %s", e)
            self._sleep(0.02)

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
            if not self.interrupt(agent_name, timeout=timeout):
                raise RuntimeError(f"Failed to interrupt agent '{agent_name}' before sending prompt")
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
        """Compare active registry records against herdr agent list and pane process info after restart.

        Fail-closed (M4): when herdr is temporarily unreachable, the registry
        is left untouched so live agents are not mass-marked exited.
        """
        self.registry.load()
        active_records = self.registry.list_agents(status="running")
        if not active_records:
            return []

        try:
            live_agents = self.herdr.agent_list()
            live_names = {a.get("name") for a in live_agents if a.get("name")}
        except Exception as e:
            logger.warning(
                "herdr agent list unavailable; leaving registry untouched: %s", e
            )
            return []

        recovered = []
        for rec in active_records:
            if rec.agent_name not in live_names:
                self.registry.update_status(
                    rec.agent_id,
                    "exited",
                    exit_reason="not found in herdr agent list on restart",
                )
                continue

            try:
                proc_info = self.herdr.pane_process_info(rec.herdr_pane_id)
            except Exception as e:
                logger.warning(
                    "herdr pane process-info unavailable for agent '%s'; "
                    "leaving status unchanged: %s",
                    rec.agent_name,
                    e,
                )
                continue
            pgid = proc_info.get("foreground_process_group_id")
            alive = bool(pgid and pgid > 0)

            if alive:
                recovered.append(rec)
            else:
                self.registry.update_status(
                    rec.agent_id,
                    "exited",
                    exit_reason="no foreground process in pane on restart",
                )
        return recovered

    def terminate_agent(
        self,
        agent_target: str,
        timeout_stage: float = 5.0,
        check_alive_func: Callable[[str], bool] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> bool:
        """Three-stage escalation termination: ctrl+c -> SIGTERM -> SIGKILL -> pane close."""
        rec = self.registry.get(agent_target) or self.registry.get_by_name(agent_target)
        if not rec:
            return False

        agent_name = rec.agent_name
        pane_id = rec.herdr_pane_id
        sleep_fn = sleeper or self._sleep

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

        sleep_fn(timeout_stage)
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

        sleep_fn(timeout_stage)
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

        # L4: verify the process is really gone before reporting success.
        # A survivor (D-state, unkillable) must not be recorded as terminated.
        if self._pane_has_foreground(pane_id):
            logger.warning(
                "Agent '%s' still has a foreground process after SIGKILL; "
                "not marking terminated",
                agent_name,
            )
            return False

        self.registry.update_status(rec.agent_id, "terminated", exit_reason="SIGKILL escalation")
        return True

    def _pane_has_foreground(self, pane_id: str) -> bool:
        """Best-effort check whether a pane still has a foreground process."""
        try:
            info = self.herdr.pane_process_info(pane_id)
        except Exception:
            return False
        pgid = info.get("foreground_process_group_id")
        return bool(pgid and pgid > 0)

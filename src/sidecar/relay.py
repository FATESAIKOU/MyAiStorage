"""Relay server and client for sidecar agent reporting (Task 3.5, Design D6)."""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from angareion.backend import ChannelNotFoundError
from sidecar.herdr import HerdrClient
from sidecar.registry import AgentRecord, Registry

logger = logging.getLogger(__name__)


def get_default_sock_path() -> Path:
    env = os.environ.get("SIDECAR_SOCK")
    if env:
        return Path(env)
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "sidecar.sock"
    return Path.home() / ".local" / "state" / "sidecar" / "sidecar.sock"


def get_peer_pid(sock: socket.socket) -> int | None:
    """Extract peer process ID from a UNIX domain socket connection.

    - Linux worker uses socket.SO_PEERCRED (struct ucred: pid, uid, gid).
    - macOS uses LOCAL_PEERPID via getsockopt(SOL_LOCAL, LOCAL_PEERPID).
    """
    # 1. Linux SO_PEERCRED
    if hasattr(socket, "SO_PEERCRED"):
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, _, _ = struct.unpack("3i", creds)
            return pid
        except OSError:
            pass

    # 2. macOS / Darwin LOCAL_PEERPID (SOL_LOCAL = 0, LOCAL_PEERPID = 2)
    SOL_LOCAL = getattr(socket, "SOL_LOCAL", 0)
    LOCAL_PEERPID = getattr(socket, "LOCAL_PEERPID", 2)
    try:
        data = sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, struct.calcsize("i"))
        (pid,) = struct.unpack("i", data)
        return pid
    except OSError:
        pass

    return None


def get_process_ancestors(pid: int) -> set[int]:
    """Return set containing pid and all its parent / ancestor PIDs."""
    ancestors = {pid}
    curr = pid
    while curr > 1:
        # On Linux: try reading /proc/<curr>/stat
        stat_file = Path(f"/proc/{curr}/stat")
        if stat_file.is_file():
            try:
                # 4th field in /proc/[pid]/stat is ppid
                parts = stat_file.read_text().split()
                # Command name may contain spaces/parens, find the closing paren index
                close_paren = [i for i, p in enumerate(parts) if p.endswith(")")][-1]
                ppid = int(parts[close_paren + 2])
                if ppid in ancestors or ppid <= 0:
                    break
                ancestors.add(ppid)
                curr = ppid
                continue
            except Exception:
                pass

        # Fallback (macOS / generic Unix): use ps command
        try:
            out = subprocess.check_output(["ps", "-o", "ppid=", "-p", str(curr)], text=True).strip()
            if not out:
                break
            ppid = int(out)
            if ppid in ancestors or ppid <= 0:
                break
            ancestors.add(ppid)
            curr = ppid
        except Exception:
            break

    return ancestors


class RelaySender(Protocol):
    def send(
        self,
        from_identity: str,
        to_identity: str,
        content: str,
        urgency: int = 5,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class FakeRelaySender:
    """Mock Angareion sender for testing and development."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(
        self,
        from_identity: str,
        to_identity: str,
        content: str,
        urgency: int = 5,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        msg = {
            "id": f"msg_{len(self.sent) + 1}",
            "from": from_identity,
            "to": to_identity,
            "content": content,
            "urgency": urgency,
            "extra": extra or {},
            "sent_at": time.time(),
        }
        self.sent.append(msg)
        return msg


class AngareionRelaySender:
    """RelaySender implementation that dispatches to an Angareion Backend.

    Per D10: the report's `from` is the team's full address
    ({repo, role, name}) taken from the sidecar config, never a hardcoded
    repo; a missing channel is an error, never auto-created.
    """

    def __init__(
        self,
        backend: Any,
        default_channel: str = "general",
        team_addresses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.backend = backend
        self.default_channel = default_channel
        self.team_addresses = dict(team_addresses or {})

    def _from_address(self, from_identity: str) -> dict[str, Any]:
        addr = self.team_addresses.get(from_identity)
        if not isinstance(addr, dict) or not addr.get("repo") or not addr.get("role"):
            raise ValueError(
                f"No full address {{repo, role, name}} configured for team "
                f"'{from_identity}': refusing to report"
            )
        return dict(addr)

    def send(
        self,
        from_identity: str,
        to_identity: str,
        content: str,
        urgency: int = 5,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from angareion.messages import Message

        from_addr = self._from_address(from_identity)

        channel = (extra or {}).get("channel", self.default_channel)
        if hasattr(self.backend, "find_channel"):
            if self.backend.find_channel(channel) is None:
                raise ChannelNotFoundError(
                    f"Channel '{channel}' does not exist; "
                    "sidecar never creates channels"
                )

        to_addr = {"repo": "pmo", "role": to_identity}
        msg = Message(
            from_=from_addr,
            to=to_addr,
            channel=channel,
            urgency=urgency,
            content=content,
        )
        self.backend.post_message(channel, msg)
        return {
            "id": msg.id,
            "from": from_identity,
            "to": to_identity,
            "channel": channel,
            "content": content,
            "urgency": urgency,
        }


def authenticate_peer(
    peer_pid: int,
    registry: Registry,
    herdr: HerdrClient | None = None,
    checker: Callable[[int, AgentRecord], bool] | None = None,
) -> AgentRecord | None:
    """Authenticate that peer_pid belongs to a running agent's process tree.

    Pids 0 and 1 (kernel / init) are never valid evidence on either side:
    every ancestor chain ends at 1, so accepting it would admit any local
    process (L1).
    """
    if peer_pid <= 1:
        return None
    running_agents = registry.list_agents(status="running")
    if not running_agents:
        return None

    if checker is not None:
        for rec in running_agents:
            if checker(peer_pid, rec):
                return rec
        return None

    ancestors = {p for p in get_process_ancestors(peer_pid) if p > 1}

    for rec in running_agents:
        target_pids: set[int] = set()

        if herdr is not None:
            try:
                info = herdr.pane_process_info(rec.herdr_pane_id)
                if info.get("shell_pid") and info["shell_pid"] > 1:
                    target_pids.add(info["shell_pid"])
                if info.get("foreground_process_group_id") and info["foreground_process_group_id"] > 1:
                    target_pids.add(info["foreground_process_group_id"])
                for p in info.get("foreground_processes", []):
                    if p.get("pid") and p["pid"] > 1:
                        target_pids.add(p["pid"])
            except Exception:
                pass

        if rec.extra and isinstance(rec.extra.get("pids"), list):
            target_pids.update(p for p in rec.extra["pids"] if isinstance(p, int) and p > 1)

        if ancestors & target_pids:
            return rec

    return None


class RelayServer:
    def __init__(
        self,
        socket_path: Path | str,
        registry: Registry,
        sender: RelaySender,
        herdr: HerdrClient | None = None,
        peer_checker: Callable[[int, AgentRecord], bool] | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.registry = registry
        self.sender = sender
        self.herdr = herdr
        self.peer_checker = peer_checker

        self._server_sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._owns_socket = False

    def start(self) -> None:
        if self._running:
            return

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            # L5: never unlink first. If a live server answers on this path,
            # refuse to steal its socket; only a stale file may be replaced.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(self.socket_path))
            except OSError:
                self.socket_path.unlink()
            else:
                raise RuntimeError(
                    f"Sidecar relay socket {self.socket_path} is already "
                    "served by a live server; refusing to take over"
                )
            finally:
                probe.close()

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(self.socket_path))
        except OSError as e:
            srv.close()
            raise RuntimeError(
                f"Cannot bind sidecar relay socket {self.socket_path}: {e}"
            ) from e
        srv.listen(16)
        # L6: owner-only socket; peer authentication is the only gate, so keep
        # it closed to other local users.
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError:
            pass
        # L6: tighten the socket directory as well (best effort: the path may
        # be user-configured).
        try:
            os.chmod(self.socket_path.parent, 0o700)
        except OSError:
            pass

        self._server_sock = srv
        self._running = True
        self._owns_socket = True

        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

        # Only unlink a socket this server actually bound (L5: a refused
        # second server must not remove the live server's socket).
        if self._owns_socket and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        self._owns_socket = False

        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _serve_loop(self) -> None:
        assert self._server_sock is not None
        while self._running:
            try:
                conn, _ = self._server_sock.accept()
            except OSError:
                break

            t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            t.start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            with conn:
                peer_pid = get_peer_pid(conn)
                if not peer_pid:
                    self._send_json(conn, {"ok": False, "error": "Unable to determine peer PID"})
                    return

                agent = authenticate_peer(
                    peer_pid,
                    self.registry,
                    herdr=self.herdr,
                    checker=self.peer_checker,
                )
                if not agent:
                    self._send_json(
                        conn,
                        {"ok": False, "error": f"Unauthorized: pid {peer_pid} is not a registered agent process"},
                    )
                    return

                data = conn.recv(65536)
                if not data:
                    return

                try:
                    req = json.loads(data.decode("utf-8"))
                except Exception as e:
                    self._send_json(conn, {"ok": False, "error": f"Invalid JSON payload: {e}"})
                    return

                content = req.get("content") or req.get("message")
                to_identity = req.get("to", "pmo")
                try:
                    urgency = int(req.get("urgency", 5))
                except (TypeError, ValueError):
                    self._send_json(conn, {"ok": False, "error": "urgency must be an integer between 0 and 9"})
                    return

                # L2/D10: urgency is 0-9 (Angareion spec). Out of range is an
                # explicit error, never silently dropped.
                if isinstance(req.get("urgency", 5), bool) or not 0 <= urgency <= 9:
                    self._send_json(conn, {"ok": False, "error": f"urgency must be between 0 and 9, got {req.get('urgency')}"})
                    return

                if not content:
                    self._send_json(conn, {"ok": False, "error": "Missing message content"})
                    return

                # Send using the verified agent's identity as 'from'
                try:
                    res = self.sender.send(
                        from_identity=agent.identity,
                        to_identity=to_identity,
                        content=content,
                        urgency=urgency,
                        extra={"agent_id": agent.agent_id, "caller_pid": peer_pid},
                    )
                except Exception as e:
                    logger.warning("Relay send failed for agent '%s': %s", agent.identity, e)
                    self._send_json(conn, {"ok": False, "error": str(e)})
                    return

                self._send_json(conn, {"ok": True, "result": res})
        except Exception:
            logger.exception("Unexpected error handling relay connection")

    def _send_json(self, conn: socket.socket, payload: dict[str, Any]) -> None:
        try:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            conn.sendall(raw)
        except OSError:
            pass


def send_relay_message(
    content: str,
    to: str = "pmo",
    urgency: int = 5,
    socket_path: Path | str | None = None,
) -> dict[str, Any]:
    """Client function used by 'sidecar say' to send a message through the relay socket."""
    if isinstance(urgency, bool) or not isinstance(urgency, int) or not 0 <= urgency <= 9:
        raise ValueError(f"urgency must be an integer between 0 and 9, got {urgency!r}")
    sock_p = Path(socket_path) if socket_path else get_default_sock_path()
    if not sock_p.exists():
        raise RuntimeError(f"Sidecar relay socket not found at {sock_p}")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(sock_p))
        payload = json.dumps({"action": "say", "content": content, "to": to, "urgency": urgency})
        s.sendall(payload.encode("utf-8"))
        raw = s.recv(65536)
        if not raw:
            raise RuntimeError("Empty response from sidecar relay socket")
        res = json.loads(raw.decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "Relay request failed"))
        return res.get("result", {})
    finally:
        s.close()

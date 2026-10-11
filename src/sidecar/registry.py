"""Persistent registry for sidecar-managed agents."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentRecord:
    agent_id: str
    identity: str
    agora_session: str
    agent_kind: str
    herdr_tab_id: str
    herdr_pane_id: str
    agent_name: str
    started_at: str
    status: str
    log_path: str
    updated_at: str
    exit_reason: str | None = None
    extra: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentRecord:
        return cls(
            agent_id=data["agent_id"],
            identity=data["identity"],
            agora_session=data["agora_session"],
            agent_kind=data["agent_kind"],
            herdr_tab_id=data["herdr_tab_id"],
            herdr_pane_id=data["herdr_pane_id"],
            agent_name=data["agent_name"],
            started_at=data["started_at"],
            status=data["status"],
            log_path=data["log_path"],
            updated_at=data["updated_at"],
            exit_reason=data.get("exit_reason"),
            extra=data.get("extra"),
        )


class Registry:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.registry_file = self.state_dir / "registry.json"
        self._records: dict[str, AgentRecord] = {}
        self.load()

    def load(self) -> dict[str, AgentRecord]:
        if not self.registry_file.is_file():
            self._records = {}
            return self._records

        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = {}
            if isinstance(data, dict):
                agents_data = data.get("agents", {})
                for aid, entry in agents_data.items():
                    if isinstance(entry, dict):
                        records[aid] = AgentRecord.from_dict(entry)
            else:
                raise ValueError("Registry JSON root must be an object")
            self._records = records
        except Exception as e:
            logger.warning("Corrupt registry file detected at %s: %s", self.registry_file, e)
            try:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                corrupt_path = self.registry_file.with_name(f"{self.registry_file.name}.corrupt-{timestamp}")
                self.registry_file.rename(corrupt_path)
            except Exception as re_err:
                logger.error("Failed to rename corrupt registry file %s: %s", self.registry_file, re_err)
            self._records = {}
        return self._records

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_dir / ".tmp-registry.json"
        payload = {
            "version": 1,
            "updated_at": now_iso(),
            "agents": {aid: rec.to_dict() for aid, rec in self._records.items()},
        }
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, self.registry_file)

    def register(self, record: AgentRecord) -> None:
        record.updated_at = now_iso()
        self._records[record.agent_id] = record
        self.save()

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def get_by_name(self, agent_name: str) -> AgentRecord | None:
        for rec in self._records.values():
            if rec.agent_name == agent_name:
                return rec
        return None

    def list_agents(self, status: str | None = None) -> list[AgentRecord]:
        records = list(self._records.values())
        if status is not None:
            return [r for r in records if r.status == status]
        return records

    def update_status(self, agent_id: str, status: str, exit_reason: str | None = None) -> bool:
        rec = self._records.get(agent_id)
        if not rec:
            return False
        rec.status = status
        rec.updated_at = now_iso()
        if exit_reason is not None:
            rec.exit_reason = exit_reason
        self.save()
        return True

    def touch(self, agent_id: str) -> bool:
        """Refresh updated_at heartbeat for a running agent (D5, D-run)."""
        rec = self._records.get(agent_id)
        if not rec:
            return False
        rec.updated_at = now_iso()
        self.save()
        return True

"""ModelBroker interface and fake broker for LLMGateway lease coordination."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


def _future_iso(seconds: int = 3600) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


@dataclass
class Lease:
    lease_id: str
    worker: str
    role: str
    coding_agent: str
    model: str
    granted: bool
    access: dict[str, str] = field(default_factory=dict)
    expires_at: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelBroker(Protocol):
    def request_lease(
        self,
        worker: str,
        role: str,
        coding_agent: str,
        model: str,
    ) -> Lease: ...

    def release(self, lease_id: str) -> None: ...


class FakeModelBroker:
    """Configurable in-memory fake model broker for development and testing."""

    def __init__(
        self,
        default_granted: bool = True,
        default_access: dict[str, str] | None = None,
        default_reason: str | None = None,
        lease_duration: int = 3600,
    ) -> None:
        self.default_granted = default_granted
        self.default_access = default_access or {"MOCK_MODEL_TOKEN": "mock-token-xyz"}
        self.default_reason = default_reason
        self.lease_duration = lease_duration
        self.active_leases: dict[str, Lease] = {}
        self.released_leases: list[str] = []
        self.requests: list[dict[str, str]] = []
        self._rules: dict[tuple[str, str], tuple[bool, str | None]] = {}

    def set_rule(self, role: str, model: str, granted: bool, reason: str | None = None) -> None:
        """Override grant decision for a specific (role, model) pair."""
        self._rules[(role, model)] = (granted, reason)

    def request_lease(
        self,
        worker: str,
        role: str,
        coding_agent: str,
        model: str,
    ) -> Lease:
        req = {
            "worker": worker,
            "role": role,
            "coding_agent": coding_agent,
            "model": model,
        }
        self.requests.append(req)

        rule = self._rules.get((role, model))
        if rule is not None:
            granted, reason = rule
        else:
            granted = self.default_granted
            reason = self.default_reason

        lease_id = f"lease_{int(time.time())}_{secrets.token_hex(4)}"
        if not granted:
            return Lease(
                lease_id=lease_id,
                worker=worker,
                role=role,
                coding_agent=coding_agent,
                model=model,
                granted=False,
                reason=reason or "Lease denied by gateway policy",
            )

        lease = Lease(
            lease_id=lease_id,
            worker=worker,
            role=role,
            coding_agent=coding_agent,
            model=model,
            granted=True,
            access=dict(self.default_access),
            expires_at=_future_iso(self.lease_duration),
        )
        self.active_leases[lease_id] = lease
        return lease

    def release(self, lease_id: str) -> None:
        self.released_leases.append(lease_id)
        self.active_leases.pop(lease_id, None)

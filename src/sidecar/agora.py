"""Agora session lookup for sidecar status (D11, task 3.9).

Lists fork sessions on agora that belong to our identities, so an operator
(or the next message) can decide whether to resume them after a rebuild.
`LibraryAgoraClient` reads the local agora index through the agora package
(lazy import: only `sidecar status` pays for it); tests use FakeAgoraClient.
"""

from __future__ import annotations

from typing import Any, Protocol


class AgoraClient(Protocol):
    def search_sessions(self, filters: list[str]) -> list[dict[str, Any]]:
        """Return fork-session rows matching every `KEY=VALUE` filter.

        Each row: {"id", "title", "identity", "message", "active"} where
        active means a continue holds the session right now.
        """
        ...


class LibraryAgoraClient:
    """AgoraClient backed by the agora package's own index (same repo)."""

    def search_sessions(self, filters: list[str]) -> list[dict[str, Any]]:
        from agora import header as h
        from agora import store

        paths = store.Paths.from_env()
        index = store.sync(paths, throttle=True)
        parsed = h.parse_filters(filters)
        rows: list[dict[str, Any]] = []
        for ulid, hdr, _snippet in index.search(parsed):
            agora_blk = h.agora_of(hdr)
            if agora_blk.get("relation") != "fork":
                continue
            side = hdr.get("sidecar") if isinstance(hdr.get("sidecar"), dict) else {}
            rows.append(
                {
                    "id": f"agora:{ulid}",
                    "title": str(hdr.get("title") or ""),
                    "identity": side.get("identity"),
                    "message": side.get("message"),
                    "active": store.continuing(paths, ulid),
                }
            )
        return rows


class FakeAgoraClient:
    """In-memory agora for tests, mirroring real header shapes."""

    def __init__(self, sessions: list[dict[str, Any]] | None = None) -> None:
        self.sessions: list[dict[str, Any]] = list(sessions or [])
        self.searches: list[list[str]] = []

    def add_session(
        self,
        session_id: str,
        identity: str | None = None,
        message: str | None = None,
        title: str = "",
        active: bool = False,
        fork: bool = True,
    ) -> None:
        self.sessions.append(
            {
                "id": session_id,
                "title": title,
                "active": active,
                "fork": fork,
                "sidecar": {"identity": identity, "message": message},
            }
        )

    def search_sessions(self, filters: list[str]) -> list[dict[str, Any]]:
        self.searches.append(list(filters))
        rows = []
        for sess in self.sessions:
            if not sess.get("fork", True):
                continue
            match = True
            for f in filters:
                if "=" not in f:
                    continue
                key, _, value = f.partition("=")
                node: Any = sess
                for part in key.split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                if node is None or str(node) != value:
                    match = False
                    break
            if match:
                side = sess.get("sidecar") or {}
                rows.append(
                    {
                        "id": sess["id"],
                        "title": sess.get("title", ""),
                        "identity": side.get("identity"),
                        "message": side.get("message"),
                        "active": sess.get("active", False),
                    }
                )
        return rows

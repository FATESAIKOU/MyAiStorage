"""Persistent store for seen message IDs (Design D8)."""

from __future__ import annotations

import json
import os
from pathlib import Path


class SeenStore:
    """Persists seen message IDs atomically to prevent duplicate delivery across restarts."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.seen_file = self.state_dir / "seen.json"
        self._seen: set[str] = set()
        self.load()

    def load(self) -> set[str]:
        if not self.seen_file.is_file():
            self._seen = set()
            return self._seen
        try:
            with open(self.seen_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._seen = set(data)
            elif isinstance(data, dict) and "seen" in data:
                self._seen = set(data["seen"])
        except Exception:
            self._seen = set()
        return self._seen

    def has(self, msg_id: str) -> bool:
        return msg_id in self._seen

    def add(self, msg_id: str) -> None:
        self._seen.add(msg_id)
        self.save()

    def save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_dir / ".tmp-seen.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seen": sorted(list(self._seen))}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.seen_file)

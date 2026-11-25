"""The registry rebuild: the whole comment thread decides config/angareion/channels.json.

Standard library only — the workflow runs this on a bare runner with
`PYTHONPATH=src` and no third-party packages. The rebuild is a pure function
over the comment list (in time order):

- only comments whose `author_association` is OWNER/MEMBER/COLLABORATOR count;
  anything else is ignored (not applied, not reported — no new comments);
- a comment must be one line of JSON in the registry format; a comment that
  starts with `{` but is not a valid action is skipped and reported (plain
  text, never a parseable action);
- `register` adds a name unless it is already taken (first come, first served;
  a repeat of the same entity is a no-op); `unregister` only removes a mapping
  that points at the same entity. Everything else is a no-op plus a report;
- every run rebuilds from the whole thread, so a cancelled or failed run
  loses nothing; writing uses the file's sha as an optimistic lock (5 tries)
  and skips the commit when the result already matches main.

The workflow's trigger layer filters out Bot comments and comments on other
issues; this script re-checks the author anyway (manual runs and scheduled
runs read the whole thread).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from angareion.registry import (
    RegistryClient,
    RegistryError,
    RegistryWriteConflict,
)
from angareion.registry_rules import (  # noqa: F401  (re-exported: existing imports keep working)
    CONFIG_VERSION,
    _comment_order,
    _quote,
    rebuild_channels,
    render_channels,
    report_text,
)

CHANNELS_PATH = "config/angareion/channels.json"
BACKENDS_PATH = "config/angareion/backends.json"
WRITE_ATTEMPTS = 5
COMMIT_MESSAGE = "angareion: rebuild config/angareion/channels.json from the registry issue"


class RebuildError(Exception):
    """The rebuild cannot run (unreadable config, missing environment)."""


class RebuildApi(Protocol):
    """What the rebuild needs from the registry: comments and the config file."""

    def all_comments(self) -> list[dict[str, Any]]:
        ...

    def get_file(self, path: str) -> tuple[str, str | None]:
        ...

    def put_file(self, path: str, text: str, *, sha: str | None, message: str) -> None:
        ...

    def post_comment(self, body: str) -> None:
        ...


@dataclass
class RebuildSummary:
    """What one rebuild run did; useful for tests and the workflow log."""

    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: list[str] = field(default_factory=list)
    posted: list[str] = field(default_factory=list)
    changed: bool = False
    committed: bool = False
    comments_seen: int = 0


def load_backend_names(text: str) -> set[str]:
    """The backend names configured in backends.json (names only; design section 9)."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RebuildError(f"{BACKENDS_PATH} is not valid JSON: {e}") from e
    if not isinstance(data, dict) or data.get("version") != CONFIG_VERSION:
        raise RebuildError(f"{BACKENDS_PATH} has an unsupported version")
    backends = data.get("backends", {})
    if not isinstance(backends, dict):
        raise RebuildError(f"{BACKENDS_PATH} has no 'backends' object")
    return set(backends)


class Rebuilder:
    """One rebuild run over the registry issue, with the sha optimistic lock."""

    def __init__(
        self,
        api: RebuildApi,
        *,
        branch: str = "main",
        sleeper: Callable[[float], None] = time.sleep,
        warn: Callable[[str], None] | None = None,
    ):
        self.api = api
        self.branch = branch
        self.sleeper = sleeper
        self.warn = warn or (lambda message: sys.stderr.write(f"Warning: {message}\n"))

    def _current_file(self) -> tuple[str, str | None]:
        try:
            return self.api.get_file(CHANNELS_PATH)
        except FileNotFoundError:
            return "", None

    def _write_if_changed(self, text: str) -> tuple[bool, bool]:
        """Returns (changed, committed); retries the write while the file keeps moving."""
        for attempt in range(WRITE_ATTEMPTS):
            current, sha = self._current_file()
            if current == text:
                return False, False  # identical to main: no commit
            try:
                self.api.put_file(CHANNELS_PATH, text, sha=sha, message=COMMIT_MESSAGE)
                return True, True
            except RegistryWriteConflict:
                if attempt < WRITE_ATTEMPTS - 1:
                    self.sleeper(1.0)
                continue
        raise RegistryWriteConflict("the file kept changing while rebuilding (gave up after 5 tries)")

    def run(self) -> RebuildSummary:
        comments = self.api.all_comments()
        backends_text, _ = self._current_file_backends()
        backend_names = load_backend_names(backends_text)

        channels, reports = rebuild_channels(comments, backend_names)
        text = render_channels(channels)

        changed = committed = False
        fixed: list[str] = []
        try:
            changed, committed = self._write_if_changed(text)
        except RegistryWriteConflict:
            fixed.append(
                report_text(
                    "the repository file kept changing while writing; this run did not update it",
                    "",
                )
            )

        # A report that quotes a comment describes that comment, which does not change,
        # so the same text is posted once (C-25). A fixed-text report describes this run,
        # so it is posted every time it happens.
        thread_bodies = {comment.get("body") for comment in comments}
        posted: list[str] = []
        for report in reports:
            if report in thread_bodies:
                continue
            self.api.post_comment(report)
            posted.append(report)
        for report in fixed:
            self.api.post_comment(report)
            posted.append(report)

        return RebuildSummary(
            channels=channels,
            reports=reports + fixed,
            posted=posted,
            changed=changed,
            committed=committed,
            comments_seen=len(comments),
        )

    def _current_file_backends(self) -> tuple[str, str | None]:
        try:
            return self.api.get_file(BACKENDS_PATH)
        except FileNotFoundError:
            raise RebuildError(f"{BACKENDS_PATH} is missing from the repository")


def main(
    argv: list[str] | None = None,
    *,
    api: RebuildApi | None = None,
    env: dict[str, str] | None = None,
    write: Callable[[str], Any] = sys.stdout.write,
) -> int:
    """The workflow entry point: environment variables in, one rebuild out.

    The comment content never arrives through a shell string; the workflow
    passes it as an environment variable. The rebuild re-reads the whole
    thread anyway, so the triggering body is not an input to the mapping.
    """
    env = dict(os.environ if env is None else env)
    repo = env.get("ANGAREION_REGISTRY_REPO") or env.get("GITHUB_REPOSITORY") or ""
    issue_text = (env.get("ANGAREION_REGISTRY_ISSUE") or "").strip()
    token = env.get("GITHUB_TOKEN") or env.get("ANGAREION_REGISTRY_TOKEN") or ""

    if "/" not in repo:
        write("registry rebuild failed: ANGAREION_REGISTRY_REPO is not 'owner/repo'\n")
        return 1
    if not issue_text.isdigit() or int(issue_text) <= 0:
        write("registry rebuild failed: ANGAREION_REGISTRY_ISSUE is not an issue number\n")
        return 1

    active_api: RebuildApi = api if api is not None else RegistryClient(repo, int(issue_text), token=token)
    rebuilder = Rebuilder(active_api)
    try:
        summary = rebuilder.run()
    except (RegistryError, RebuildError) as e:
        write(f"registry rebuild failed: {e}\n")
        return 1

    write(
        f"registry rebuild: {summary.comments_seen} comments, "
        f"{len(summary.channels)} channels, "
        f"{'committed' if summary.committed else 'no commit needed'}, "
        f"{len(summary.reports)} reports\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

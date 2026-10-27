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
    AUTHORIZED_ASSOCIATIONS,
    InvalidAction,
    RegistryClient,
    RegistryError,
    RegistryWriteConflict,
    parse_comment,
)

CHANNELS_PATH = "config/angareion/channels.json"
BACKENDS_PATH = "config/angareion/backends.json"
CONFIG_VERSION = 1
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
    changed: bool = False
    committed: bool = False
    comments_seen: int = 0


def render_channels(channels: dict[str, dict[str, Any]]) -> str:
    """The channels.json text: sorted keys, two-space indent, trailing newline."""
    return json.dumps(
        {"channels": channels, "version": CONFIG_VERSION},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


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


def _quote(body: Any) -> str:
    text = "" if body is None else str(body)
    text = " ".join(text.split())
    if len(text) > 300:
        text = text[:300] + "…"
    return text


def report_text(reason: str, body: Any) -> str:
    """A plain-text report; never a single-line JSON that could be parsed as an action."""
    return (
        "Angareion registry: this comment could not be applied.\n"
        f"> {_quote(body)}\n"
        f"Reason: {reason}\n"
        "Nothing was changed by it; the rebuild always starts from the whole thread.\n"
    )


def _comment_order(comment: dict[str, Any]) -> tuple[str, int]:
    """Sort key for the rebuild: the authoritative arrival order is (created_at, id)."""
    created = comment.get("created_at")
    if not isinstance(created, str):
        created = ""
    try:
        comment_id = int(comment.get("id", 0))
    except (TypeError, ValueError):
        comment_id = 0
    return (created, comment_id)


def rebuild_channels(
    comments: list[dict[str, Any]], backend_names: set[str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Rebuild the mapping from the comment thread (in order); return it plus reports."""
    channels: dict[str, dict[str, Any]] = {}
    reports: list[str] = []

    for comment in sorted(comments, key=_comment_order):
        if comment.get("author_association") not in AUTHORIZED_ASSOCIATIONS:
            continue  # not authorized: ignored, and never reported
        body = comment.get("body")
        parsed = parse_comment(body)
        if parsed is None:
            continue  # not an action attempt: ignored silently
        if isinstance(parsed, InvalidAction):
            reports.append(report_text(parsed.reason, body))
            continue
        if parsed.backend not in backend_names:
            reports.append(report_text(f"the backend {parsed.backend!r} is not configured", body))
            continue

        entry = {"backend": parsed.backend, "issue": parsed.issue}
        current = channels.get(parsed.channel)
        if parsed.action == "register":
            if current is None:
                channels[parsed.channel] = entry
            elif current == entry:
                continue  # the same entity registered twice: success, no-op
            else:
                reports.append(
                    report_text(
                        f"channel {parsed.channel!r} is already registered to a different entity "
                        "(first come, first served)",
                        body,
                    )
                )
        else:  # unregister
            if current == entry:
                del channels[parsed.channel]
            else:
                reports.append(
                    report_text(
                        f"channel {parsed.channel!r} does not point at that entity (nothing to remove)",
                        body,
                    )
                )

    return channels, reports


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
        try:
            changed, committed = self._write_if_changed(text)
        except RegistryWriteConflict:
            reports.append(
                report_text(
                    "the repository file kept changing while writing; this run did not update it",
                    "",
                )
            )

        for report in reports:
            self.api.post_comment(report)

        return RebuildSummary(
            channels=channels,
            reports=reports,
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

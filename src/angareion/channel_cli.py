"""`angareion channel <action>`: create, delete and status (design 542 §6.1, C-5).

`create` and `delete` moved here from cli.py unchanged; `status` is new and only
reads (fetch, no writes, no cursor). cli.py imports this module, so it is not
imported back: the exit codes and `mask_token` are repeated here, as
decision_cli.py does.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

from angareion.backend import PAGE_COMMENT_LIMIT, BackendError, PageBackend, page_level
from angareion.channel_config import (
    BackendChoiceError,
    ChannelConfigError,
    ChannelStore,
)
from angareion.client import Angareion
from angareion.config import Config, mask_token
from angareion.messages import MessageValidationError, validate_channel_name
from angareion.registry import (
    CommentPoster,
    RegistryCounter,
    RegistryError,
    post_with_retries,
    register_comment,
    unregister_comment,
)

# The same exit codes as cli.py (cli imports this module, so they are not imported back).
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BACKEND = 2

# PR-1: a channel is one page until rollover exists (C-26 fills in the real page count).
PAGES_UNTIL_ROLLOVER = 1
REGISTRY_ROW = "registry"


def cmd_channel_create(
    args: argparse.Namespace,
    config: Config,
    app: Angareion,
    registry: CommentPoster,
    store: ChannelStore,
    token: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """The three-step create (design section 6).

    1. open the backend entity; 2. verify it, then post a register comment on
    the registry issue; 3. wait (bounded) for the rebuild to land the mapping.
    """
    try:
        validate_channel_name(args.name)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    store.ensure_fresh()
    if args.name in store.channels():
        sys.stderr.write(
            f"Error: channel '{args.name}' already exists; use `angareion channel delete` first to replace it.\n"
        )
        return EXIT_USAGE

    try:
        backend_ref = store.choose_backend_for_create(args.backend)
    except BackendChoiceError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE
    except ChannelConfigError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_BACKEND

    # Step 1: open the entity in the backend.
    try:
        entity = app.create_channel(args.name, backend_ref)
    except BackendError:
        sys.stderr.write(f"Error: channel '{args.name}' was not created.\n")
        return EXIT_BACKEND

    # Step 2: the client verifies the entity (exists and its title is the
    # channel name) before anything is registered (review H2).
    if not app.verify_channel(args.name, backend_ref, entity):
        sys.stderr.write(
            f"Error: channel '{args.name}' could not be verified in the backend; nothing was registered.\n"
        )
        return EXIT_BACKEND

    body = register_comment(args.name, backend_ref.name, entity)
    try:
        post_with_retries(registry, body, sleeper=sleeper)
    except RegistryError:
        sys.stderr.write(
            f"Error: channel '{args.name}' was not created: the registration could not be posted.\n"
        )
        return EXIT_BACKEND

    # Step 3: wait for the mapping to land (bounded); a timeout is not an error.
    if not store.poll_for_channel(args.name):
        sys.stderr.write(
            f"Notice: channel '{args.name}' is registered but has not shown up in the local copy yet; "
            "it will be usable once the registry rebuild lands.\n"
        )
    sys.stdout.write(f"{args.name}\n")
    return EXIT_OK


def cmd_channel_delete(
    args: argparse.Namespace,
    config: Config,
    app: Angareion,
    registry: CommentPoster,
    store: ChannelStore,
    token: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """Delete a channel: close the entity (keep the record), then unregister (design section 7)."""
    try:
        validate_channel_name(args.name)
    except MessageValidationError as e:
        sys.stderr.write(f"Error: {mask_token(str(e), token)}\n")
        return EXIT_USAGE

    store.ensure_fresh()
    entity = store.entity_for(args.name)  # a missing channel raises ChannelNotFoundError

    try:
        app.close_channel(entity)
    except BackendError:
        sys.stderr.write(f"Error: channel '{args.name}' was not deleted: the backend entity could not be closed.\n")
        return EXIT_BACKEND

    body = unregister_comment(args.name, entity.backend, entity.issue)
    try:
        post_with_retries(registry, body, sleeper=sleeper)
    except RegistryError:
        sys.stderr.write(
            f"Error: channel '{args.name}' was closed but the unregistration could not be posted.\n"
        )
        return EXIT_BACKEND

    sys.stdout.write(f"{args.name}\n")
    return EXIT_OK



def _page_count(backend: Any, entity: Any, token: str) -> tuple[int | None, bool]:
    """(messages on the current page or None, ok). A backend without page sizes gives (None, True)."""
    if not isinstance(backend, PageBackend):
        return None, True
    try:
        return backend.page_size(entity), True
    except BackendError as e:
        sys.stderr.write(
            f"Warning: channel '{entity.channel}': the page size could not be read: {mask_token(str(e), token)}\n"
        )
        return None, False


def _registry_count(registry: Any, token: str) -> tuple[int | None, bool]:
    """(comments on the registry issue or None, ok); a seam without comment_count gives (None, True)."""
    if not isinstance(registry, RegistryCounter):
        return None, True
    try:
        return registry.comment_count(), True
    except RegistryError as e:
        sys.stderr.write(f"Warning: registry: the comment count could not be read: {mask_token(str(e), token)}\n")
        return None, False


def _status_row(name: str, count: int | None, pages: int) -> dict[str, Any]:
    return {
        "name": name,
        "count": count,
        "limit": PAGE_COMMENT_LIMIT,
        "level": page_level(count),
        "pages": pages,
        "flags": [],
    }


def _status_table(rows: list[dict[str, Any]]) -> str:
    """The rows as aligned text; an empty count or flag list is shown as `-`."""
    header = ["name", "count", "limit", "level", "pages", "flags"]
    cells = [
        [
            row["name"],
            "-" if row["count"] is None else str(row["count"]),
            str(row["limit"]),
            row["level"],
            str(row["pages"]),
            ",".join(row["flags"]) or "-",
        ]
        for row in rows
    ]
    widths = [max(len(line[i]) for line in [header, *cells]) for i in range(len(header))]
    lines = ["  ".join(text.ljust(widths[i]) for i, text in enumerate(line)).rstrip() for line in [header, *cells]]
    return "\n".join(lines) + "\n"


def cmd_channel_status(
    args: argparse.Namespace,
    app: Angareion,
    registry: CommentPoster | RegistryCounter,
    store: ChannelStore,
    token: str,
) -> int:
    """Each channel's page, then the registry issue. Read-only: fetch, no posts, no cursor (C-5).

    A name that is not registered is a usage error (exit 1) before anything is read.
    A failed read leaves that row at level `unknown`, says so on stderr, and the
    other rows still print; the run then exits 2.
    """
    store.ensure_fresh()
    entities = {entity.channel: entity for entity in store.entities()}
    names = list(dict.fromkeys(args.names or []))
    for name in names:
        if name not in entities:
            sys.stderr.write(f"Error: channel '{name}' is not registered\n")
            return EXIT_USAGE
    targets = [entities[name] for name in names] if names else list(entities.values())

    rows: list[dict[str, Any]] = []
    failed = False
    for entity in targets:
        count, ok = _page_count(app.backend, entity, token)
        failed = failed or not ok
        rows.append(_status_row(entity.channel, count, PAGES_UNTIL_ROLLOVER))
    count, ok = _registry_count(registry, token)
    failed = failed or not ok
    rows.append(_status_row(REGISTRY_ROW, count, PAGES_UNTIL_ROLLOVER))

    if args.json:
        sys.stdout.write(json.dumps(rows, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_status_table(rows))
    return EXIT_BACKEND if failed else EXIT_OK



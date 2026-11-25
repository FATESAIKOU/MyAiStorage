"""The registry rules: the comment thread becomes the channel mapping (design 542 §1.2, D-34).

Pure functions, standard library plus `registry` only. The rebuild workflow
(`registry_rebuild`) and, in later PRs, the writer side (`rollover`) use the same
rules from here, so there is exactly one copy of them.

The mapping has one current page per channel (`backend`, `issue`); a channel
that has moved pages also carries `retired`, the pages it left, in the order the
rebuild applied them. A `rollover` comment moves the page `from` the current one
to `issue`; when `from` is already retired (someone else moved first), the new
page is recorded as a side page instead (`side: true`) and the current page stays.
"""

from __future__ import annotations

import json
from typing import Any

from angareion.registry import (
    AUTHORIZED_ASSOCIATIONS,
    InvalidAction,
    parse_comment,
)

CONFIG_VERSION = 1


def render_channels(channels: dict[str, dict[str, Any]]) -> str:
    """The channels.json text: sorted keys, two-space indent, trailing newline."""
    return json.dumps(
        {"channels": channels, "version": CONFIG_VERSION},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


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


def _same_page(entry: dict[str, Any], backend: str, issue: int) -> bool:
    """Whether a mapping's current page is this entity (retired pages do not count)."""
    return entry["backend"] == backend and entry["issue"] == issue


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

        current = channels.get(parsed.channel)
        if parsed.action == "register":
            if current is None:
                channels[parsed.channel] = {"backend": parsed.backend, "issue": parsed.issue}
            elif _same_page(current, parsed.backend, parsed.issue):
                continue  # the same entity registered twice: success, no-op
            else:
                reports.append(
                    report_text(
                        f"channel {parsed.channel!r} is already registered to a different entity "
                        "(first come, first served)",
                        body,
                    )
                )
        elif parsed.action == "unregister":
            if current is not None and _same_page(current, parsed.backend, parsed.issue):
                del channels[parsed.channel]  # the whole mapping, retired pages included
            else:
                reports.append(
                    report_text(
                        f"channel {parsed.channel!r} does not point at that entity (nothing to remove)",
                        body,
                    )
                )
        else:  # rollover
            if current is None:
                reports.append(report_text(f"channel {parsed.channel!r} is not registered", body))
                continue
            if current["backend"] != parsed.backend:
                reports.append(
                    report_text(
                        f"channel {parsed.channel!r} is on backend {current['backend']!r}, not {parsed.backend!r}",
                        body,
                    )
                )
                continue
            retired: list[dict[str, Any]] = current.get("retired", [])
            retired_issues = {page["issue"] for page in retired}
            if parsed.issue == current["issue"] or parsed.issue in retired_issues:
                continue  # already the page, or already retired: no-op
            until = str(comment.get("created_at", ""))
            if parsed.from_issue == current["issue"]:
                retired.append({"issue": parsed.from_issue, "until": until})
                current["issue"] = parsed.issue
                current["retired"] = retired
            elif parsed.from_issue in retired_issues:
                # Another rollover from the same page came first: keep it as a side page.
                retired.append({"issue": parsed.issue, "until": until, "side": True})
                current["retired"] = retired
            else:
                reports.append(
                    report_text(
                        f"issue {parsed.from_issue} is neither the current page nor a retired page "
                        f"of channel {parsed.channel!r}",
                        body,
                    )
                )

    return channels, reports

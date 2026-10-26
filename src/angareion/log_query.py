"""Pure query helpers for `angareion log`: anchor/thread assembly, filtering, sorting, limit.

This module holds the read-only selection logic so it can be tested without a
backend or a cache. ``select`` never touches I/O; ``cmd_log`` does the sync and
then calls ``select`` to turn the cached messages into the final list.
"""

from __future__ import annotations

from typing import Any

from angareion.backend import parse_backend_time
from angareion.messages import Message


class AnchorNotFoundError(Exception):
    """Raised when the anchor message id cannot be found in the selected channels."""


def select(
    messages_by_channel,
    selected_channels,
    *,
    me,
    from_=None,
    to=None,
    mentioned=False,
    since=None,
    until=None,
    grep=None,
    around=None,
    before=None,
    after=None,
    thread=None,
    limit=None,
) -> list:
    """Select the messages ``cmd_log`` should print.

    ``messages_by_channel`` maps channel name -> list of ``Message`` objects
    carrying ``received_at`` / ``comment_id`` (set by the cache sync). The
    messages are not mutated except for an in-place sort of each channel's list.
    """
    messages_by_channel = messages_by_channel or {}
    selected_channels = list(selected_channels or [])

    from_filter = from_
    to_filter = to
    mentioned_flag = bool(mentioned)

    anchor_id = around if around is not None else thread
    is_thread = thread is not None

    for ch in list(messages_by_channel.keys()):
        messages_by_channel[ch].sort(key=lambda m: m.comment_id)

    candidates = []
    anchor_channel = None
    if anchor_id is not None:
        anchor_msg = None
        for ch in selected_channels:
            for m in messages_by_channel.get(ch, []):
                if m.id == anchor_id:
                    anchor_msg = m
                    anchor_channel = ch
                    break
            if anchor_msg is not None:
                break
        if anchor_msg is None:
            raise AnchorNotFoundError("message '{}' not found".format(anchor_id))
        if is_thread:
            msgs = messages_by_channel.get(anchor_channel, [])
            by_id_msg = {m.id: m for m in msgs}
            current = anchor_msg
            visited = set()
            while True:
                irt = getattr(current, "in_reply_to", None)
                if irt is None or irt not in by_id_msg or irt in visited:
                    break
                visited.add(current.id)
                current = by_id_msg[irt]
            root = current
            closure_ids = {root.id}
            changed = True
            while changed:
                changed = False
                for m in msgs:
                    irt = getattr(m, "in_reply_to", None)
                    if irt in closure_ids and m.id not in closure_ids:
                        closure_ids.add(m.id)
                        changed = True
            collected = [m for m in msgs if m.id in closure_ids]
            collected.sort(key=lambda m: m.comment_id)
            candidates = collected
        else:
            msgs = messages_by_channel.get(anchor_channel, [])
            sorted_msgs = sorted(msgs, key=lambda m: m.comment_id)
            idx = next(i for i, m in enumerate(sorted_msgs) if m.id == anchor_id)
            before_n = before if before is not None else 0
            after_n = after if after is not None else 0
            start = max(0, idx - before_n)
            end = min(len(sorted_msgs), idx + after_n + 1)
            candidates = list(sorted_msgs[start:end])
    else:
        for ch in selected_channels:
            candidates.extend(messages_by_channel.get(ch, []))

    def keep(m):
        if from_filter is not None and m.from_ != from_filter:
            return False
        if to_filter is not None:
            if not (m.to is None or m.to == to_filter or to_filter in (m.mentions or [])):
                return False
        if mentioned_flag:
            if not (m.to == me or me in (m.mentions or [])):
                return False
        if since is not None:
            if parse_backend_time(m.received_at) < parse_backend_time(since):
                return False
        if until is not None:
            if parse_backend_time(m.received_at) > parse_backend_time(until):
                return False
        if grep:
            if grep.lower() not in (m.content or "").lower():
                return False
        return True

    filtered = [m for m in candidates if keep(m)]

    if len(selected_channels) == 1:
        filtered.sort(key=lambda m: m.comment_id)
    else:
        filtered.sort(key=lambda m: (getattr(m, "received_at", "") or "", m.channel, m.id))

    if limit is not None:
        n = limit
        if n == 0:
            filtered = []
        else:
            filtered = filtered[-n:]
    return filtered
"""Local cache for `angareion log` (D3): per identity, per channel."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from datetime import timedelta

from angareion.backend import format_backend_time, parse_backend_time
from angareion.config import channel_slug, identity_slug
from angareion.messages import Message, message_to_dict


LOG_CACHE_VERSION = 1


def log_cache_dir(state_dir, identity):
    return Path(state_dir) / f"log-{identity_slug(identity)}"


def log_cache_path(state_dir, identity, channel):
    return log_cache_dir(state_dir, identity) / f"{channel_slug(channel)}.json"


def _warn_to_stderr(message):
    sys.stderr.write(f"Warning: {message}\n")


def _etag_to_dict(record):
    if record is None:
        return None
    if isinstance(record, dict):
        url = record.get("url")
        etag = record.get("etag")
        if not isinstance(url, str) or not url or not isinstance(etag, str) or not etag:
            return None
        return {"url": url, "etag": etag}
    url = getattr(record, "url", None)
    etag = getattr(record, "etag", None)
    if not isinstance(url, str) or not url or not isinstance(etag, str) or not etag:
        return None
    return {"url": url, "etag": etag}


def _etag_from_dict(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    etag = value.get("etag")
    if not isinstance(url, str) or not url or not isinstance(etag, str) or not etag:
        return None
    return {"url": url, "etag": etag}


class LogCache:
    def __init__(self, state_dir=None, identity=None, path=None, warn=None, state=None):
        if state_dir is None and state is not None:
            state_dir = state
        if path is not None and state_dir is None and identity is None:
            self._single_path = Path(path)
            self.state_dir = None
            self.identity = None
        else:
            self._single_path = None
            if state_dir is None or identity is None:
                raise ValueError("LogCache needs state_dir and identity, or path")
            self.state_dir = Path(state_dir)
            self.identity = identity
        self._warn = warn or _warn_to_stderr
        self._warned_write_failure = False

    def _dir(self):
        if self.state_dir is None or self.identity is None:
            return None
        return log_cache_dir(self.state_dir, self.identity)

    def path_for(self, channel=None):
        if self._single_path is not None:
            return self._single_path
        return log_cache_path(self.state_dir, self.identity, channel)

    def load(self, channel=None, entity=None):
        if channel is None and self._single_path is not None:
            channel = self._single_path.stem
        path = self.path_for(channel) if channel is not None else self._single_path
        if path is None or not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("version") != LOG_CACHE_VERSION:
            return None
        if data.get("channel") != channel:
            return None
        cached_entity = data.get("entity")
        if entity is not None and cached_entity != entity:
            return None
        # required fields
        if "last_comment_id" not in data or "last_created_at" not in data or "messages" not in data or "entity" not in data or "channel" not in data:
            return None
        if not isinstance(data.get("messages"), list):
            return None
        for _m in data.get("messages"):
            if not isinstance(_m, dict):
                return None
            try:
                int(_m.get("comment_id"))
            except (TypeError, ValueError):
                return None
            if not isinstance(_m.get("created_at"), str):
                return None
            _msg = _m.get("message")
            if not isinstance(_msg, dict):
                return None
            for _k in ("id", "at", "from", "to", "channel", "urgency", "content"):
                if _k not in _msg:
                    return None
            if not isinstance(_msg.get("id"), str):
                return None
            if not isinstance(_msg.get("at"), str):
                return None
            if _msg.get("from") is not None and not isinstance(_msg.get("from"), str):
                return None
            if _msg.get("to") is not None and not isinstance(_msg.get("to"), str):
                return None
            if not isinstance(_msg.get("channel"), str):
                return None
            if not isinstance(_msg.get("urgency"), int) or isinstance(_msg.get("urgency"), bool):
                return None
            if not isinstance(_msg.get("content"), str):
                return None
        last_cid = data.get("last_comment_id")
        if last_cid is not None and not isinstance(last_cid, str):
            return None
        last_created = data.get("last_created_at")
        if last_created is not None and not isinstance(last_created, str):
            return None
        return data

    def load_messages(self, channel=None, entity=None):
        data = self.load(channel, entity)
        if not data:
            return [], None, None, None
        return (
            list(data.get("messages", [])),
            data.get("last_comment_id"),
            data.get("last_created_at"),
            _etag_from_dict(data.get("etag")),
        )

    def sync(self, backend, entity):
        ch = entity.channel
        cached_entity = {"repo": entity.repo, "issue": entity.issue}
        cached_msgs, _last_cid, last_created, etag = self.load_messages(ch, cached_entity)
        mirror = list(cached_msgs)
        if last_created is None:
            since = None
        else:
            since_dt = parse_backend_time(last_created) - timedelta(seconds=60)
            since = format_backend_time(since_dt)
        enumerated, new_etag, not_modified = backend.enumerate_conditional(entity, since=since, etag=etag)
        by_id = {}
        for m in mirror:
            by_id[int(m["comment_id"])] = m
        had_new_message = False
        if not not_modified:
            for cid, created_at, parsed in enumerated:
                if parsed is None:
                    continue
                cid_int = int(cid)
                msg_dict = {
                    "comment_id": str(cid_int),
                    "created_at": created_at,
                    "comment_url": getattr(parsed, "comment_url", "") or "",
                    "message": message_to_dict(parsed),
                }
                existing = by_id.get(cid_int)
                is_new = existing is None or existing.get("message") != msg_dict["message"] or existing.get("created_at") != created_at
                if is_new:
                    had_new_message = True
                    by_id[cid_int] = msg_dict
            if by_id:
                max_created = max((m["created_at"] for m in by_id.values()), key=lambda t: parse_backend_time(t))
                max_cid = max(int(m["comment_id"]) for m in by_id.values())
            else:
                max_created = last_created
                max_cid = None
            if had_new_message:
                new_etag = None
            messages_list = sorted(by_id.values(), key=lambda m: int(m["comment_id"]))
            self.save(ch, cached_entity, str(max_cid) if max_cid is not None else None, max_created, new_etag, messages_list)
        else:
            messages_list = list(mirror)

        msgs_for_query = []
        for m in messages_list:
            d = m["message"]
            msg = Message(
                id=d["id"],
                at=d["at"],
                from_=d["from"],
                to=d["to"],
                channel=d["channel"],
                urgency=d["urgency"],
                content=d["content"],
                attachments=d.get("attachments", []),
                mentions=d.get("mentions", []),
                in_reply_to=d.get("in_reply_to"),
            )
            setattr(msg, "received_at", m["created_at"])
            setattr(msg, "comment_id", int(m["comment_id"]))
            setattr(msg, "comment_url", m.get("comment_url", ""))
            msgs_for_query.append(msg)
        return msgs_for_query

    def save(self, channel, entity, last_comment_id, last_created_at, etag, messages):
        path = self.path_for(channel)
        payload = {
            "version": LOG_CACHE_VERSION,
            "channel": channel,
            "entity": entity,
            "last_comment_id": last_comment_id,
            "last_created_at": last_created_at,
            "etag": _etag_to_dict(etag),
            "messages": messages if messages is not None else [],
        }
        try:
            parent = path.parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as e:
            self._report_write_failure(e)
            return False
        return True

    def _report_write_failure(self, error):
        if self._warned_write_failure:
            return
        self._warned_write_failure = True
        self._warn(
            "could not write the log cache file ({}); ".format(error)
            + "log will use an in-memory mirror this run. Messages are not affected."
        )

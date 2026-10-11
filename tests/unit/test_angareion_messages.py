"""Unit tests for the Angareion message model, serialization, parsing, validation (D3)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from angareion.messages import (
    ANGAREION_VERSION,
    MAX_MESSAGE_LENGTH,
    Message,
    MessageValidationError,
    is_group_address,
    message_to_dict,
    new_ulid,
    now_iso,
    parse_message,
    serialize_message,
    validate_address,
    validate_attachment,
)


def test_ulid_generation():
    uid1 = new_ulid()
    uid2 = new_ulid()
    assert len(uid1) == 26
    assert len(uid2) == 26
    assert uid1 != uid2
    crockford = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    assert all(c in crockford for c in uid1)


def test_now_iso():
    iso = now_iso()
    assert "T" in iso
    assert "+" in iso or "Z" in iso or "-" in iso


def test_message_roundtrip():
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM", "name": "PMO"},
        to={"repo": "owner-org/worker-team", "role": "team-pm", "name": "worker-1"},
        channel="phase-1-task-42",
        urgency=7,
        content="Here is the body content.\nMultiple lines.\n",
        attachments=[
            {"name": "工單", "ref": "https://github.com/owner-org/repo-channel/issues/42"},
            {"name": "片段", "inline": "print('hello world')"},
        ],
    )
    serialized = serialize_message(msg)
    assert len(serialized) <= MAX_MESSAGE_LENGTH

    parsed = parse_message(serialized, strict=True)
    assert parsed is not None
    assert parsed.from_ == msg.from_
    assert parsed.to == msg.to
    assert parsed.channel == msg.channel
    assert parsed.urgency == 7
    assert parsed.content == msg.content
    assert parsed.attachments == msg.attachments
    assert parsed.id == msg.id
    assert parsed.at == msg.at
    assert parsed.angareion == ANGAREION_VERSION


def test_front_matter_version_field_is_angareion():
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="ch-1",
    )
    serialized = serialize_message(msg)
    assert serialized.startswith("---\nangareion: 1\n")
    # v1's ack is gone: nothing writes an ack field any more.
    assert "ack" not in serialized


def test_message_to_dict():
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="ch-1",
        content="body",
    )
    data = message_to_dict(msg)
    assert data == {
        "id": msg.id,
        "at": msg.at,
        "from": msg.from_,
        "to": msg.to,
        "channel": "ch-1",
        "urgency": 5,
        "content": "body",
        "attachments": [],
    }


def test_content_with_horizontal_rule_delimiters():
    body = "Line 1\n---\nLine 2\n---\nAnother line\n"
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="ch-1",
        urgency=5,
        content=body,
        attachments=[],
    )
    serialized = serialize_message(msg)
    parsed = parse_message(serialized, strict=True)
    assert parsed is not None
    assert parsed.content == body


def test_missing_required_fields():
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="",
    )
    with pytest.raises(MessageValidationError, match="channel"):
        serialize_message(msg)

    msg2 = Message(
        from_={},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="test-channel",
    )
    with pytest.raises(MessageValidationError, match="from"):
        serialize_message(msg2)

    msg3 = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={},
        channel="test-channel",
    )
    with pytest.raises(MessageValidationError, match="to"):
        serialize_message(msg3)


def test_address_validation():
    validate_address({"repo": "owner-org/repo-channel", "role": "PM", "name": "Alice"})
    validate_address({"repo": "owner-org/repo-channel", "role": "PM"})

    with pytest.raises(MessageValidationError, match="requires 'repo' and 'role'"):
        validate_address({"role": "PM"})

    with pytest.raises(MessageValidationError, match="non-empty string"):
        validate_address({"repo": "   ", "role": "PM"})

    with pytest.raises(MessageValidationError, match="unknown keys"):
        validate_address({"repo": "owner-org/repo-channel", "role": "PM", "extra": 123})

    # The parser still recognises the group shape so the CLI can reject it.
    validate_address({"group": "all-workers"})
    assert is_group_address({"group": "all-workers"})
    assert not is_group_address({"repo": "owner-org/repo-channel", "role": "PM"})

    with pytest.raises(MessageValidationError, match="extra keys"):
        validate_address({"group": "all-workers", "role": "PM"})

    msg_group_from = Message(
        from_={"group": "all-workers"},
        to={"repo": "owner-org/repo-channel", "role": "PM"},
        channel="ch-1",
    )
    with pytest.raises(MessageValidationError, match="'from' cannot be a group address"):
        serialize_message(msg_group_from)


def test_attachment_validation():
    validate_attachment({"name": "doc", "ref": "https://example.com"})
    validate_attachment({"name": "snippet", "inline": "code content"})

    with pytest.raises(MessageValidationError, match="requires non-empty string 'name'"):
        validate_attachment({"ref": "https://example.com"})

    with pytest.raises(MessageValidationError, match="got both"):
        validate_attachment({"name": "doc", "ref": "https://example.com", "inline": "text"})

    with pytest.raises(MessageValidationError, match="got neither"):
        validate_attachment({"name": "doc"})

    with pytest.raises(MessageValidationError, match="unknown keys"):
        validate_attachment({"name": "doc", "ref": "https://example.com", "sha256": "abc"})


def test_urgency_validation():
    for u in range(10):
        msg = Message(
            from_={"repo": "owner-org/repo-channel", "role": "PM"},
            to={"repo": "owner-org/worker-team", "role": "worker"},
            channel="ch",
            urgency=u,
        )
        msg.validate()

    for bad in (-1, 10, 100, True, False, "5"):
        msg = Message(
            from_={"repo": "owner-org/repo-channel", "role": "PM"},
            to={"repo": "owner-org/worker-team", "role": "worker"},
            channel="ch",
            urgency=bad,  # type: ignore
        )
        with pytest.raises(MessageValidationError):
            msg.validate()


def test_message_length_limit():
    huge_body = "x" * 65500
    msg = Message(
        from_={"repo": "owner-org/repo-channel", "role": "PM"},
        to={"repo": "owner-org/worker-team", "role": "worker"},
        channel="ch",
        content=huge_body,
    )
    with pytest.raises(MessageValidationError, match="exceeds maximum limit of 65536"):
        serialize_message(msg)


def test_parse_non_messages():
    assert parse_message("Just a regular github comment") is None
    assert parse_message("---\n: invalid : yaml : ---\nbody") is None

    text_v2 = (
        "---\nangareion: 2\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: {repo: a, role: b}\nto: {repo: c, role: d}\nchannel: x\nurgency: 5\nattachments: []\n---\n"
    )
    assert parse_message(text_v2) is None
    with pytest.raises(MessageValidationError, match="Unsupported angareion version"):
        parse_message(text_v2, strict=True)

    # v1-shaped front matter (a2a, ack) is not a v0 message.
    text_v1 = (
        "---\na2a: 1\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: {repo: a, role: b}\nto: {repo: c, role: d}\nchannel: x\nurgency: 5\n"
        "attachments: []\n---\n"
    )
    assert parse_message(text_v1) is None

"""Unit tests for the Angareion message model, serialization, parsing, validation (D3/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from angareion.messages import (
    AGENT_NAME_RE,
    ANGAREION_VERSION,
    MAX_MESSAGE_LENGTH,
    Message,
    MessageValidationError,
    message_to_dict,
    parse_identity,
    new_ulid,
    now_iso,
    parse_message,
    serialize_message,
    validate_address,
    validate_attachment,
    validate_channel_name,
)

ME = "MyLinuxPool-impl-2"
PM = "LLMGateway-pm"


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
        from_="MyLinuxPool-impl-2",
        to=PM,
        channel="general",
        urgency=7,
        content="Here is the body content.\nMultiple lines.\n",
        attachments=[
            {"name": "工單", "ref": "https://example.com/issue/42"},
            {"name": "片段", "inline": "print('hello world')"},
        ],
    )
    serialized = serialize_message(msg)
    assert len(serialized) <= MAX_MESSAGE_LENGTH

    parsed = parse_message(serialized, strict=True)
    assert parsed is not None
    assert parsed.from_ == "MyLinuxPool-impl-2"
    assert parsed.to == PM
    assert parsed.channel == msg.channel
    assert parsed.urgency == 7
    assert parsed.content == msg.content
    assert parsed.attachments == msg.attachments
    assert parsed.id == msg.id
    assert parsed.at == msg.at
    assert parsed.angareion == ANGAREION_VERSION


def test_broadcast_omits_to_and_parses_back_as_none():
    msg = Message(from_=PM, to=None, channel="general", content="everyone")
    serialized = serialize_message(msg)
    assert "to:" not in serialized.split("---")[1]

    parsed = parse_message(serialized, strict=True)
    assert parsed is not None
    assert parsed.to is None
    assert parsed.content == "everyone"


def test_front_matter_version_field_is_angareion():
    msg = Message(from_=PM, to=ME, channel="ch-1")
    serialized = serialize_message(msg)
    assert serialized.startswith("---\nangareion: 1\n")
    assert "ack" not in serialized
    # v5 addresses are plain strings in the front matter, not repo/role objects.
    assert "from: LLMGateway-pm" in serialized
    assert "to: MyLinuxPool-impl-2" in serialized


def test_message_to_dict():
    msg = Message(from_=PM, to=ME, channel="ch-1", content="body")
    data = message_to_dict(msg)
    assert data == {
        "id": msg.id,
        "at": msg.at,
        "from": PM,
        "to": ME,
        "channel": "ch-1",
        "urgency": 5,
        "content": "body",
        "attachments": [],
    }


def test_content_with_horizontal_rule_delimiters():
    body = "Line 1\n---\nLine 2\n---\nAnother line\n"
    msg = Message(from_=PM, to=ME, channel="ch-1", urgency=5, content=body, attachments=[])
    parsed = parse_message(serialize_message(msg), strict=True)
    assert parsed is not None
    assert parsed.content == body


def test_unquoted_yaml_timestamp_at_is_accepted():
    """L7: YAML turns an unquoted timestamp into datetime; that must parse, not reject."""
    text = (
        "---\nangareion: 1\nid: 01JABC\nat: 2026-10-10T12:00:00+00:00\n"
        "from: LLMGateway-pm\nto: MyLinuxPool-impl-2\nchannel: x\nurgency: 5\n"
        "attachments: []\n---\nbody"
    )
    parsed = parse_message(text, strict=True)
    assert parsed is not None
    assert isinstance(parsed.at, str)
    assert parsed.at == "2026-10-10T12:00:00+00:00"
    assert parsed.content == "body"

    # A bare date becomes a `date` object; the ISO string survives too.
    date_only = text.replace("2026-10-10T12:00:00+00:00", "2026-10-10")
    parsed_date = parse_message(date_only, strict=True)
    assert parsed_date is not None
    assert parsed_date.at == "2026-10-10"


def test_missing_required_fields():
    with pytest.raises(MessageValidationError, match="channel"):
        serialize_message(Message(from_=PM, to=ME, channel=""))

    with pytest.raises(MessageValidationError, match="from"):
        serialize_message(Message(from_="", to=ME, channel="test-channel"))

    with pytest.raises(MessageValidationError, match="from must be an agent name"):
        # `None` is not an agent name; only `to` may be omitted.
        serialize_message(Message(from_=None, to=ME, channel="test-channel"))  # type: ignore[arg-type]

    with pytest.raises(MessageValidationError, match="to must be an agent name"):
        serialize_message(Message(from_=PM, to="not-an-agent", channel="test-channel"))


@pytest.mark.parametrize("name", ["LLMGateway-pm", "MyLinuxPool-impl-2", "my-repo-impl-2", "repo_1.2-arch"])
def test_address_grammar_accepts_agent_names(name):
    validate_address(name)


@pytest.mark.parametrize(
    "name",
    [
        "myrepo-dev",     # role not in the list
        "MyRepo-PM",      # roles are lower case
        "-pm",            # empty repo
        "foo-impl-",      # empty id segment
        "foo-impl-2!",    # illegal character
        "group=全員",      # there are no group addresses in v5
        "repo=owner/team,role=pm",
        "owner/team",     # no role at all
        {"repo": "owner/team", "role": "pm"},  # the v0 shape is not an agent name
    ],
)
def test_address_grammar_rejects_everything_else(name):
    with pytest.raises(MessageValidationError):
        validate_address(name)


def test_parse_identity_takes_the_name_apart_from_the_right():
    parsed = parse_identity("my-repo-impl-2")
    assert (parsed.repo, parsed.role, parsed.agent_id) == ("my-repo", "impl", "2")

    parsed = parse_identity("LLMGateway-pm")
    assert (parsed.repo, parsed.role, parsed.agent_id) == ("LLMGateway", "pm", None)

    # The rightmost role segment wins; the repo keeps the earlier ones.
    parsed = parse_identity("foo-pm-test")
    assert (parsed.repo, parsed.role, parsed.agent_id) == ("foo-pm", "test", None)

    parsed = parse_identity("MyLinuxPool-impl-2")
    assert (parsed.repo, parsed.role, parsed.agent_id) == ("MyLinuxPool", "impl", "2")

    with pytest.raises(MessageValidationError):
        parse_identity("myrepo-dev")


def test_address_regex_and_parser_agree_on_the_shape():
    # The regex only checks the overall shape; the parser is a separate step.
    assert AGENT_NAME_RE.match("my-repo-impl-2")
    assert not AGENT_NAME_RE.match("myrepo-dev")


def test_channel_name_validation():
    for good in ("general", "phase-1", "a_b-9"):
        validate_channel_name(good)
    for bad in ("", "小明", "has space", "dot.name", None, 3):
        with pytest.raises(MessageValidationError):
            validate_channel_name(bad)


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
        Message(from_=PM, to=ME, channel="ch", urgency=u).validate()

    for bad in (-1, 10, 100, True, False, "5"):
        with pytest.raises(MessageValidationError):
            Message(from_=PM, to=ME, channel="ch", urgency=bad).validate()  # type: ignore[arg-type]


def test_message_length_limit():
    huge_body = "x" * 65500
    msg = Message(from_=PM, to=ME, channel="ch", content=huge_body)
    with pytest.raises(MessageValidationError, match="exceeds maximum limit of 65536"):
        serialize_message(msg)


def test_parse_non_messages():
    assert parse_message("Just a regular github comment") is None
    assert parse_message("---\n: invalid : yaml : ---\nbody") is None

    text_v2 = (
        "---\nangareion: 2\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: LLMGateway-pm\nto: MyLinuxPool-impl-2\nchannel: x\nurgency: 5\nattachments: []\n---\n"
    )
    assert parse_message(text_v2) is None
    with pytest.raises(MessageValidationError, match="Unsupported angareion version"):
        parse_message(text_v2, strict=True)

    # The v0 {repo, role} address shape is not a v5 message: ignored on read.
    text_v0 = (
        "---\nangareion: 1\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: {repo: a, role: b}\nto: {repo: c, role: d}\nchannel: x\nurgency: 5\n"
        "attachments: []\n---\n"
    )
    assert parse_message(text_v0) is None

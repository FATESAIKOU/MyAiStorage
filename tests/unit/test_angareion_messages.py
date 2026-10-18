"""Unit tests for the Angareion message model, serialization, parsing, validation (D3/v5)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from angareion.messages import (
    ANGAREION_VERSION,
    LEGACY_AGENT_NAME_RE,
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

ME = "mylinuxpool-impl-w1-1"
PM = "mypmo-pm-m1"


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
        from_="mylinuxpool-impl-w1-1",
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
    assert parsed.from_ == "mylinuxpool-impl-w1-1"
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
    assert "from: mypmo-pm-m1" in serialized
    assert "to: mylinuxpool-impl-w1-1" in serialized


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
        "from: mypmo-pm-m1\nto: mylinuxpool-impl-w1-1\nchannel: x\nurgency: 5\n"
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

    with pytest.raises(MessageValidationError, match="from must be an AI name"):
        # `None` is not an AI name; only `to` may be omitted.
        serialize_message(Message(from_=None, to=ME, channel="test-channel"))  # type: ignore[arg-type]

    with pytest.raises(MessageValidationError, match="to must be an AI name"):
        serialize_message(Message(from_=PM, to="not-an-agent", channel="test-channel"))


@pytest.mark.parametrize(
    "name",
    [
        "mypmo-pm-m1",
        "mylinuxpool-impl-w1-1",
        "mylinuxpool-rev-w1-1",
        "myaistorage-arch-m1-2",
        "myaientry-secretary-p1",
        "pm-tools-impl-m1-1",   # a project name may contain a role word
    ],
)
def test_address_grammar_accepts_ai_names(name):
    validate_address(name)


@pytest.mark.parametrize(
    "name",
    [
        "myrepo-dev-m1",       # role not in the list
        "MyRepo-PM-m1",        # names are all lowercase
        "-pm-m1",              # empty project
        "mypmo-pm",            # no team segment (an old-style name: send refuses it)
        "mylinuxpool-impl-pm", # the team segment is a role word
        "mypmo-pm-m1-1",       # pm takes no sequence number
        "mylinuxpool-impl-w1",  # impl needs one
        "mylinuxpool-impl-w1-0",
        "mylinuxpool-impl-w1-01",
        "mylinuxpool-impl-workerpool-1",  # a team code is at most 8 characters
        "mylinuxpool-impl-w1-",  # a trailing '-'
        "mylinuxpool-impl-w1-1\n",  # the comparison is over the whole string
        "a" * 27 + "-pm-m1",    # 33 characters
        "group=全員",            # there are no group addresses in phase 2
        "@all",
        "repo=owner/team,role=pm",
        "owner/team",     # no role at all
        {"repo": "owner/team", "role": "pm"},  # the v0 shape is not an AI name
    ],
)
def test_address_grammar_rejects_everything_else(name):
    with pytest.raises(MessageValidationError):
        validate_address(name)


def test_the_refusal_says_which_step_failed():
    """"缺 team 段" and "請用小寫" are in the message, not just in the docs (#97)."""
    with pytest.raises(MessageValidationError) as caught:
        validate_address("mypmo-pm", field_name="to")
    assert "team" in str(caught.value) and "missing" in str(caught.value)

    with pytest.raises(MessageValidationError) as caught:
        validate_address("MyPMO-pm-m1", field_name="--to")
    assert "lowercase" in str(caught.value)
    assert "--to" in str(caught.value)


def test_reading_accepts_the_old_grammar_while_sending_does_not():
    """Sending is strict; reading is wide: old names are still delivered (#97)."""
    for old in ("MyLinuxPool-impl-2", "LLMGateway-pm", "mypmo-pm", "mylinuxpool-pm", "repo_1.2-arch"):
        with pytest.raises(MessageValidationError):
            validate_address(old)
        validate_address(old, allow_legacy=True)

    # `mypmo-pm-m1` is legal under both grammars; accepted once, either way.
    for both in ("mypmo-pm-m1",):
        validate_address(both)
        validate_address(both, allow_legacy=True)

    # A team code cannot start with a digit, so this old name is read, not sent.
    with pytest.raises(MessageValidationError):
        validate_address("mylinuxpool-impl-1")
    validate_address("mylinuxpool-impl-1", allow_legacy=True)

    # Still not a message: nonsense is rejected on the read side too.
    for bad in ("owner/team", "group=全員", "@all", "mylinuxpool-dev-m1", "mypmo-pm\n"):
        with pytest.raises(MessageValidationError):
            validate_address(bad, allow_legacy=True)


def test_the_legacy_regex_is_only_the_old_grammar():
    # The regex is matched whole, like the grammar it stands for: a name that
    # merely starts with an old name ("mypmo-pm\n") is not an old name.
    assert LEGACY_AGENT_NAME_RE.fullmatch("mylinuxpool-impl-1")
    assert LEGACY_AGENT_NAME_RE.fullmatch("mypmo-pm")
    assert LEGACY_AGENT_NAME_RE.fullmatch("MyLinuxPool-impl-2")
    assert not LEGACY_AGENT_NAME_RE.fullmatch("mylinuxpool-dev-m1")
    assert not LEGACY_AGENT_NAME_RE.fullmatch("mypmo-pm\n")
    # The old grammar allowed one optional id segment, so a team code plus a
    # sequence number never matched it ...
    assert not LEGACY_AGENT_NAME_RE.fullmatch("mylinuxpool-impl-w1-1")
    # ... and secretary/rev are new roles the old version skipped in silence.
    assert not LEGACY_AGENT_NAME_RE.fullmatch("myaientry-secretary-p1")
    assert not LEGACY_AGENT_NAME_RE.fullmatch("mylinuxpool-rev-w1-1")
    # All of them are read now.
    for new in ("mylinuxpool-impl-w1-1", "myaientry-secretary-p1", "mylinuxpool-rev-w1-1"):
        validate_address(new, allow_legacy=True)


def test_parse_identity_takes_the_name_apart():
    parsed = parse_identity("mylinuxpool-impl-w1-1")
    assert (parsed.project, parsed.role, parsed.team, parsed.seq) == ("mylinuxpool", "impl", "w1", 1)
    assert str(parsed) == "mylinuxpool-impl-w1-1"

    parsed = parse_identity("mypmo-pm-m1")
    assert (parsed.project, parsed.role, parsed.team, parsed.seq) == ("mypmo", "pm", "m1", None)
    assert str(parsed) == "mypmo-pm-m1"

    # The rightmost role segment wins; the project keeps the earlier ones.
    parsed = parse_identity("pm-tools-impl-m1-1")
    assert (parsed.project, parsed.role, parsed.team, parsed.seq) == ("pm-tools", "impl", "m1", 1)

    with pytest.raises(MessageValidationError):
        parse_identity("myrepo-dev-m1")

    # The old grammar is not an identity any more: only the send/read split
    # keeps it, and `parse_identity` is the send side.
    with pytest.raises(MessageValidationError):
        parse_identity("mylinuxpool-impl-2")


def test_a_message_from_an_old_version_is_still_read():
    """讀寬: a message the old version wrote parses, and prints unchanged."""
    text = (
        "---\nangareion: 1\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: MyLinuxPool-impl-2\nto: mypmo-pm\nchannel: general\nurgency: 5\n"
        "attachments: []\n---\nfrom the old version\n"
    )
    parsed = parse_message(text, strict=True)
    assert parsed is not None
    assert parsed.from_ == "MyLinuxPool-impl-2"
    assert parsed.to == "mypmo-pm"

    # The same addresses cannot be sent: serializing validates strictly.
    with pytest.raises(MessageValidationError):
        serialize_message(Message(from_="MyLinuxPool-impl-2", to=None, channel="general", content="x"))


def test_a_new_name_the_old_grammar_rejects_is_still_read():
    """讀寬: a message the old version would have skipped is no longer skipped."""
    text = (
        "---\nangareion: 1\nid: 01JABC\nat: 2026-10-10T00:00:00Z\n"
        "from: myaientry-secretary-p1\nto: mylinuxpool-rev-w1-1\nchannel: general\n"
        "urgency: 5\nattachments: []\n---\nsecretary and reviewer\n"
    )
    assert not LEGACY_AGENT_NAME_RE.fullmatch("myaientry-secretary-p1")
    parsed = parse_message(text, strict=True)
    assert parsed is not None
    assert parsed.from_ == "myaientry-secretary-p1"
    assert parsed.to == "mylinuxpool-rev-w1-1"


def _front_matter(sender: str | None = None, recipient: str | None = None) -> str:
    """A message's front matter; `None` leaves the field out, `""` is a value."""
    lines = ["---", "angareion: 1", "id: 01JABC", "at: '2026-10-10T00:00:00Z'"]
    if sender is not None:
        lines.append(f'from: "{sender}"')
    if recipient is not None:
        lines.append(f'to: "{recipient}"')
    lines += ["channel: general", "urgency: 5", "attachments: []", "---"]
    return "\n".join(lines) + "\nbody\n"


# Strings that neither grammar allows. Reading is wide, not blind: these stay
# skipped (a comment that is not a message is ignored), while the old-grammar
# ones beside them are delivered.
NEITHER_GRAMMAR = [
    "owner/team",
    "group=全員",
    "repo=owner/team,role=pm",
    "@all",
    "mylinuxpool-dev-m1",
    "mylinuxpool-impl-w1-0",
    "mylinuxpool-impl-w1-1-2",
    "mypmo-pm-m1\n",
    "mylinuxpool--impl-w1-1",
]


@pytest.mark.parametrize("address", NEITHER_GRAMMAR)
@pytest.mark.parametrize("field", ["from", "to"])
def test_reading_still_skips_an_address_neither_grammar_allows(address, field):
    """R2: `parse_message` returns None - the message is not a message."""
    text = _front_matter(address, ME) if field == "from" else _front_matter(PM, address)
    assert parse_message(text) is None
    with pytest.raises(MessageValidationError):
        parse_message(text, strict=True)


def test_the_type_check_still_holds_on_the_read_side():
    """A non-string address is not a name either: the check is not skipped."""
    text = (
        "---\nangareion: 1\nid: 01JABC\nat: '2026-10-10T00:00:00Z'\n"
        "from: [mypmo, pm]\nchannel: general\nurgency: 5\nattachments: []\n---\nbody\n"
    )
    assert parse_message(text) is None
    with pytest.raises(MessageValidationError, match="AI name string"):
        parse_message(text, strict=True)


@pytest.mark.parametrize(
    "address",
    ["MyLinuxPool-impl-2", "mypmo-pm", "mylinuxpool-pm", "repo_1.2-arch", "mylinuxpool-impl-1"],
)
def test_reading_delivers_what_the_old_grammar_allows(address):
    text = _front_matter(sender=address, recipient="mylinuxpool-impl-w1-1")
    parsed = parse_message(text, strict=True)
    assert parsed is not None
    assert parsed.from_ == address


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
        "from: mypmo-pm-m1\nto: mylinuxpool-impl-w1-1\nchannel: x\nurgency: 5\nattachments: []\n---\n"
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

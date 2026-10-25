"""Python <-> TS interop: the vectors copied from MyAiEntry (see PINNED.md there).

Everything here is offline: files under tests/vectors/angareion-interop/ and
in-memory messages. No backend, no real angareion, no config or state dirs.
"""

import json
from pathlib import Path

import pytest

from angareion import messages
from angareion.backend import mention_matches, recipient_matches
from angareion.messages import (
    Message,
    MessageValidationError,
    message_to_dict,
    parse_message,
    serialize_message,
    split_front_matter,
)

VECTORS = Path(__file__).resolve().parent.parent / "vectors" / "angareion-interop"
EXPECTED = json.loads((VECTORS / "expected.json").read_text(encoding="utf-8"))
DIRS = ["ts-written", "python-written"]
CASES = [(d, name) for d in DIRS for name in sorted(EXPECTED["messages"])]
CANNOT_READ = VECTORS / "python-written-py-cannot-read"


def _read(path: Path) -> Message:
    msg = parse_message(path.read_text(encoding="utf-8"))
    assert msg is not None, f"{path} did not parse"
    return msg


def test_every_expected_message_has_a_file_in_both_directories():
    for d in DIRS:
        assert sorted(p.name for p in (VECTORS / d).glob("*.md")) == sorted(EXPECTED["messages"])


@pytest.mark.parametrize(("directory", "name"), CASES)
def test_parse_message_reads_the_fields_in_expected_json(directory, name):
    got = message_to_dict(_read(VECTORS / directory / name))
    assert got == EXPECTED["messages"][name]


@pytest.mark.parametrize("reader", sorted(EXPECTED["readers"]))
@pytest.mark.parametrize(("directory", "name"), CASES)
def test_recipient_and_mention_filters_match_expected_json(reader, directory, name):
    msg = _read(VECTORS / directory / name)
    want = EXPECTED["readers"][reader][name]
    assert recipient_matches(msg, reader) is want["recipient"]
    assert mention_matches(msg, reader) is want["mention"]


def _cannot_read_cases():
    inputs = json.loads((CANNOT_READ / "inputs.json").read_text(encoding="utf-8"))
    return [pytest.param(item, id=item["file"]) for item in inputs]


@pytest.mark.parametrize("item", _cannot_read_cases())
def test_messages_the_old_reader_could_not_read_now_read_back(item):
    msg = _read(CANNOT_READ / "python-written" / item["file"])
    want = {k: v for k, v in item.items() if k != "file"}
    assert message_to_dict(msg) == want


def _message_with_inline(inline: str) -> Message:
    return Message(
        from_="mypmo-pm-m1",
        to="mylinuxpool-impl-w1-1",
        channel="general",
        content="see attached\n",
        attachments=[{"name": "a.md", "inline": inline}, {"name": "b.md", "ref": "docs/b.md"}],
        id="01ARZ3NDEKTSV4RRFFQ69G5FB2",
        at="2026-10-25T01:00:00+00:00",
    )


@pytest.mark.parametrize(
    "inline",
    ["before\n---\nafter\n", "---\ntitle: x\n---\n\n# h\n", "---", "a\n---\n---\nb", "x\r\n---\r\ny"],
)
def test_a_message_with_a_dash_line_in_an_inline_attachment_reads_back(inline):
    msg = _message_with_inline(inline)
    back = parse_message(serialize_message(msg))
    assert back is not None
    assert message_to_dict(back) == message_to_dict(msg)


@pytest.mark.parametrize(
    "text",
    [
        "---\nangareion: 1\n    ---\nrest",  # indented: not a closing line
        " ---\nangareion: 1\n---\nrest",  # indented opening
        "---\na: 1\n---\x0c\nb",  # form feed after the closing line: not a blank
        "---\na: 1\n---\xa0\nb",  # no-break space after the closing line
    ],
)
def test_split_front_matter_does_not_take_an_indented_dash_line(text):
    with pytest.raises(MessageValidationError):
        split_front_matter(text)


@pytest.mark.parametrize("tail", [" ", "\t", "\r", "  \t\r"])
def test_split_front_matter_allows_trailing_blanks_like_ts(tail):
    raw, body = split_front_matter(f"---{tail}\na: 1\n---{tail}\nbody")
    assert raw == "a: 1\n"
    assert body == "body"


def test_split_front_matter_takes_the_first_bare_dash_line_and_keeps_the_body():
    # `\r\n` in the body is read as `\n`, like the TS side (#232).
    raw, body = split_front_matter("---\na: 1\n    ---\n---\nbody\n---\nmore\r\n")
    assert raw == "a: 1\n    ---\n"
    assert body == "body\n---\nmore\n"


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c"])
def test_split_front_matter_does_not_break_lines_at_unicode_separators(sep):
    # str.splitlines() would break at `sep` and see `---` as its own line.
    raw, body = split_front_matter(f"---\na: 1{sep}---\nb: 2\n---\nbody")
    assert raw == f"a: 1{sep}---\nb: 2\n"
    assert body == "body"


def test_serialize_message_refuses_an_inline_attachment_that_reads_back_changed():
    # No monkeypatch: NEL in an inline attachment is read back as a space.
    msg = _message_with_inline("x\x85y")
    with pytest.raises(MessageValidationError, match="attachments"):
        serialize_message(msg)


def test_serialize_message_refuses_what_it_cannot_read_back(monkeypatch):
    # Simulate a writer/reader pair that disagree (the reader loses a field).
    msg = _message_with_inline("plain")
    real = messages.parse_message

    def lossy(text, *, strict=False):
        back = real(text, strict=strict)
        back.urgency = 9
        return back

    monkeypatch.setattr(messages, "parse_message", lossy)
    with pytest.raises(MessageValidationError, match="different fields"):
        serialize_message(msg)


def test_serialize_message_refuses_when_the_reader_cannot_parse_it(monkeypatch):
    msg = _message_with_inline("plain")
    monkeypatch.setattr(messages, "split_front_matter", lambda text: (_ for _ in ()).throw(
        MessageValidationError("boom")))
    with pytest.raises(MessageValidationError, match="Refusing to send"):
        serialize_message(msg)


def test_an_old_reader_that_ignores_mentions_and_in_reply_to_still_reads_a_ts_message(monkeypatch):
    """M2a section 3 (c): a reader that does not know `mentions` / `in_reply_to`.

    The old reader (before #209) built a Message from the seven original keys
    and never looked at the others. It is simulated by making the two readers
    return their defaults, so the unknown keys are ignored as the old code did.

    This is a simulation. The real old reader (messages.py at 89b9686) was run
    in a scratch copy and reads all 18 ts-written / python-written messages.
    """
    monkeypatch.setattr(messages, "_read_mentions", lambda value: [])
    monkeypatch.setattr(messages, "_read_in_reply_to", lambda value: None)
    name = "01-mentions-attachments-reply.md"
    msg = _read(VECTORS / "ts-written" / name)
    want = dict(EXPECTED["messages"][name], mentions=[], in_reply_to=None)
    assert message_to_dict(msg) == want


def test_an_unknown_top_level_key_does_not_hurt_the_known_fields():
    name = "03-to-and-mentions.md"
    text = (VECTORS / "ts-written" / name).read_text(encoding="utf-8")
    text = text.replace("\nchannel:", "\nfuture_key: [1, 2]\nchannel:", 1)
    msg = parse_message(text)
    assert msg is not None
    assert message_to_dict(msg) == EXPECTED["messages"][name]


LOCAL = VECTORS / "local-additions"


def test_a_crlf_message_reads_with_lf_content_like_ts():
    # Local addition (not from MyAiEntry, see PINNED.md): the file is CRLF throughout.
    raw = (LOCAL / "99-crlf-content.md").read_bytes()
    lines = raw.split(b"\n")[:-1]
    assert lines and all(line.endswith(b"\r") for line in lines)
    assert raw.endswith(b"\r\n")
    msg = parse_message(raw.decode("utf-8"))
    assert msg is not None
    want = json.loads((LOCAL / "99-crlf-content.expected.json").read_text(encoding="utf-8"))
    assert message_to_dict(msg) == want
    assert msg.content == "body\nmore\n"


def test_serialize_message_sends_crlf_content_and_it_reads_as_lf():
    msg = _message_with_inline("plain")
    msg.content = "body\r\nmore\r\n"
    text = serialize_message(msg)
    assert text.endswith("---\nbody\r\nmore\r\n")
    back = parse_message(text)
    assert back is not None
    assert back.content == "body\nmore\n"


@pytest.mark.parametrize(
    "damage",
    [
        lambda c: c + "x",  # one more character
        lambda c: c + "\n",  # only trailing whitespace differs
        lambda c: " " + c,  # only leading whitespace differs
        lambda c: c.replace("\r", "\n"),  # a lone CR turned into LF
    ],
    ids=["extra-char", "trailing-nl", "leading-space", "lone-cr-to-lf"],
)
def test_serialize_message_refuses_when_content_reads_back_changed(monkeypatch, damage):
    msg = _message_with_inline("plain")
    msg.content = "x\ry\r\n"
    real = messages.parse_message

    def lossy(text, *, strict=False):
        back = real(text, strict=strict)
        back.content = damage(back.content)
        return back

    monkeypatch.setattr(messages, "parse_message", lossy)
    with pytest.raises(MessageValidationError, match="content"):
        serialize_message(msg)


def test_serialize_message_sends_a_lone_cr_and_reads_it_back_as_it_is():
    msg = _message_with_inline("plain")
    msg.content = "x\ry\r\n"
    text = serialize_message(msg)
    assert text.endswith("---\nx\ry\r\n")
    back = parse_message(text)
    assert back is not None
    assert back.content == "x\ry\n"


def test_split_front_matter_turns_crlf_into_lf_in_the_content():
    assert split_front_matter("---\na: 1\n---\nbody\r\nmore\r\n") == ("a: 1\n", "body\nmore\n")


def test_split_front_matter_reads_a_crlf_front_matter():
    assert split_front_matter("---\r\na: 1\r\n---\r\nbody\r\n") == ("a: 1\n", "body\n")


def test_split_front_matter_keeps_a_lone_cr():
    raw, body = split_front_matter("---\na: 1\n---\nx\ry\r\r\nz\r")
    assert raw == "a: 1\n"
    assert body == "x\ry\r\nz\r"


def test_parse_message_reads_the_crlf_example_of_the_ticket():
    msg = parse_message(
        "---\nangareion: 1\nid: 01JB0000000000000000000098\nat: '2026-10-25T01:00:00+00:00'\n"
        "from: mypmo-pm-m1\nchannel: general\nurgency: 5\nattachments: []\n---\nbody\r\nmore\r\n"
    )
    assert msg is not None
    assert msg.content == "body\nmore\n"

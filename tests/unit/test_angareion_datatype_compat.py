"""Old-reader compatibility of the `datatype` front matter key (#468, C-24).

The old reader is main `0577584`, pinned under `tests/fixtures/angareion-reader-0577584/`
(`messages.py` whole; `matching.py` and `output.py` are the three functions the
old reader used, copied verbatim). The texts below are the shapes a new writer
produces: a plain message (`datatype: message`), an `ask`, and a `submit`
(`datatype: decision`, with the payload as a YAML body). All three texts are written
out here by hand, because no writer exists yet (PR-2 and PR-3 add them). After PR-4,
a test should feed the 0577584 reader the output of the real generators.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ASKER = "mypmo-pm-m1"
ANSWERER = "mylinuxpool-impl-w1-1"
THIRD = "mylinuxpool-arch-w1-2"
CHANNEL = "team-myaistorage-w1"

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "angareion-reader-0577584"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / filename)
    module = importlib.util.module_from_spec(spec)
    # Registered first: the old module declares a dataclass, which looks its module up by name.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


old_messages = _load("angareion_reader_0577584_messages", "messages.py")
old_matching = _load("angareion_reader_0577584_matching", "matching.py")
old_output = _load("angareion_reader_0577584_output", "output.py")


def _text(*, datatype: str, msg_id: str, from_: str, to: str | None, mentions: list[str], body: str) -> str:
    lines = [
        "---",
        "angareion: 1",
        f"datatype: {datatype}",
        f"id: {msg_id}",
        "at: '2026-11-15T03:00:00+00:00'",
        f"from: {from_}",
    ]
    if to is not None:
        lines.append(f"to: {to}")
    if mentions:
        lines.append("mentions: [" + ", ".join(mentions) + "]")
    lines += [f"channel: {CHANNEL}", "urgency: 5", "attachments: []", "---"]
    return "\n".join(lines) + "\n" + body


PLAIN_BODY = "hello 中文 — keep this content\n"

ASK_BODY = """decision: 1
op: ask
ask_channel: team-myaistorage-w1
asker: mypmo-pm-m1
answerer: mylinuxpool-impl-w1-1
title: 要不要先合 PR-1
background: 背景
body: 內文 with an emoji 🙂
options:
- key: A
  text: 先合
- key: B
  text: 等 App 確認
recommended: B
reason: 理由
"""

SUBMIT_BODY = """decision: 1
op: submit
asker: mypmo-pm-m1
items:
- ref: 01JB0000000000000000000301
  base: 01JB0000000000000000000301
  title: 要不要先合 PR-1
  choice: B
  note: 等 MyAiEntry 的測試
- ref: 01JB0000000000000000000302
  base: 01JB0000000000000000000302
  title: 命名
  note: 兩個都不好，用 X
"""


def _plain() -> str:
    return _text(
        datatype="message",
        msg_id="01JB0000000000000000000201",
        from_=ASKER,
        to=ANSWERER,
        mentions=[],
        body=PLAIN_BODY,
    )


def _ask() -> str:
    return _text(
        datatype="decision",
        msg_id="01JB0000000000000000000301",
        from_=ASKER,
        to=ANSWERER,
        mentions=[],
        body=ASK_BODY,
    )


def _submit() -> str:
    return _text(
        datatype="decision",
        msg_id="01JB0000000000000000000401",
        from_=ANSWERER,
        to=ASKER,
        mentions=[ASKER],
        body=SUBMIT_BODY,
    )


def test_c24_old_reader_parses_a_plain_message_and_keeps_its_content():
    msg = old_messages.parse_message(_plain(), strict=True)
    assert msg is not None
    assert msg.content == PLAIN_BODY
    assert old_matching.recipient_matches(msg, ANSWERER) is True
    assert old_matching.recipient_matches(msg, THIRD) is False


def test_c24_old_reader_parses_an_ask_and_only_the_answerer_is_addressed():
    msg = old_messages.parse_message(_ask(), strict=True)
    assert msg is not None
    assert msg.content == ASK_BODY
    assert old_matching.recipient_matches(msg, ANSWERER) is True
    assert old_matching.mention_matches(msg, ANSWERER) is True
    # The asker wrote it but is not addressed by it; the old reader has no
    # "own writes" rule, so this is only the plain address check.
    assert old_matching.recipient_matches(msg, ASKER) is False
    assert old_matching.mention_matches(msg, ASKER) is False
    assert old_matching.recipient_matches(msg, THIRD) is False
    assert old_matching.mention_matches(msg, THIRD) is False


def test_c24_old_reader_parses_a_submit_and_the_asker_is_addressed_and_mentioned():
    msg = old_messages.parse_message(_submit(), strict=True)
    assert msg is not None
    assert msg.content == SUBMIT_BODY
    assert old_matching.recipient_matches(msg, ASKER) is True
    assert old_matching.mention_matches(msg, ASKER) is True
    assert old_matching.recipient_matches(msg, THIRD) is False
    assert old_matching.mention_matches(msg, THIRD) is False


def test_c24_old_format_shows_the_content_of_each_shape():
    for text, body in ((_plain(), PLAIN_BODY), (_ask(), ASK_BODY), (_submit(), SUBMIT_BODY)):
        msg = old_messages.parse_message(text, strict=True)
        shown = old_output._format_message(msg)
        for line in body.rstrip("\n").splitlines():
            assert "  " + line in shown


PINNED_COMMIT = "0577584"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_c24_pinned_messages_fixture_is_the_0577584_file():
    # The fixture is that file as it was at 0577584, behind a leading comment block that says
    # where it came from. Compare the blob (what `git show` prints for it) with that block removed.
    if subprocess.run(["git", "cat-file", "-e", PINNED_COMMIT], cwd=REPO_ROOT, capture_output=True).returncode != 0:
        pytest.skip(f"commit {PINNED_COMMIT} is not in this clone")
    shown = subprocess.run(
        ["git", "cat-file", "blob", f"{PINNED_COMMIT}:src/angareion/messages.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    lines = (FIXTURES / "messages.py").read_bytes().splitlines(keepends=True)
    header = 0
    while header < len(lines) and lines[header].startswith(b"#"):
        header += 1
    assert b"".join(lines[header:]) == shown

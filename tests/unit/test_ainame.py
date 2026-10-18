"""Unit tests for `ainame`: every vector in tests/vectors/ainame-vectors.json.

The vectors are cross-checked against an independent reference: one plain
regular expression that says the same thing in a single line (`_REFERENCE_RE`
below). `parse` is the step-by-step implementation from the design; the regex
is written from the same rules without looking at `parse`'s structure, so a
mistake in one is not a mistake in the other. A deterministic fuzz over short
random strings compares the two over the whole alphabet, which is what makes
the vector file trustworthy rather than merely self-consistent.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from pathlib import Path
import random
import re

import pytest

from ainame import MAX_LEN, ROLES, SINGLE_ROLES, Name, format, is_group, is_valid, parse

VECTORS = Path(__file__).resolve().parent.parent / "vectors" / "ainame-vectors.json"

# The independent reference: `<project>-<role>-<team>[-<seq>]`, with the two
# families of roles spelled out, plus the length rule (checked outside).
_PROJECT = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
_TEAM = r"[a-z][a-z0-9]{0,7}"
_SEQ = r"[1-9][0-9]?"
_REFERENCE_RE = re.compile(
    rf"{_PROJECT}-(?:pm|secretary)-{_TEAM}"
    rf"|{_PROJECT}-(?:arch|impl|test|rev)-{_TEAM}-{_SEQ}"
)


def reference_is_valid(name: object) -> bool:
    """The same verdict as `is_valid`, from the regular expression alone."""
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_LEN:
        return False
    return _REFERENCE_RE.fullmatch(name) is not None


def load_vectors() -> list[dict]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_the_vector_file_has_the_documented_shape():
    vectors = load_vectors()
    assert vectors, "the vector file is empty"
    for vector in vectors:
        assert set(vector) == {"name", "valid", "project", "role", "team", "seq", "note"}
        assert isinstance(vector["name"], str)
        assert isinstance(vector["valid"], bool)
        assert vector["note"], f"vector {vector['name']!r} has no note"


def test_the_vectors_cover_what_the_design_names():
    """The vectors must carry every case #97 lists, not just a sample."""
    vectors = {vector["name"]: vector for vector in load_vectors()}
    required_valid = {
        "mypmo-pm-m1",
        "myaientry-secretary-p1",
        "mylinuxpool-impl-w1-1",
        "mylinuxpool-rev-w1-1",
        "myaistorage-arch-m1-2",
        "pm-tools-impl-m1-1",
        "mkcommunication-secretary-p1",
        "mkcommunication-impl-w12-99",
        "a" * 25 + "-pm-m1",   # 31 characters
        "a" * 26 + "-pm-m1",   # 32 characters
    }
    required_invalid = {
        "mypmo-pm",
        "mylinuxpool-pm",
        "myaientry-secretary",
        "MyPMO-pm-m1",
        "mypmo-pm-m1\n",
        "mypmo-pm-m1\t",
        "ｍypmo-pm-m1",
        "mylinuxpool-impl-pm",
        "mypmo-pm-m1-1",
        "mylinuxpool-impl-w1",
        "mylinuxpool-impl-w1-0",
        "mylinuxpool-impl-w1-01",
        "mylinuxpool-impl-w1-100",
        "mylinuxpool--impl-w1-1",
        "mylinuxpool-impl-w1-",
        "1mypmo-pm-m1",
        "a" * 27 + "-pm-m1",
        "",
        "@all",
    }
    assert required_valid <= set(vectors)
    assert required_invalid <= set(vectors)
    for name in required_valid:
        assert vectors[name]["valid"] is True, name
    for name in required_invalid:
        assert vectors[name]["valid"] is False, name


@pytest.mark.parametrize("vector", load_vectors(), ids=lambda v: repr(v["name"]))
def test_is_valid_agrees_with_every_vector(vector):
    assert is_valid(vector["name"]) is vector["valid"]


@pytest.mark.parametrize("vector", load_vectors(), ids=lambda v: repr(v["name"]))
def test_the_reference_regex_agrees_with_every_vector(vector):
    assert reference_is_valid(vector["name"]) is vector["valid"]


@pytest.mark.parametrize("vector", load_vectors(), ids=lambda v: repr(v["name"]))
def test_parse_gives_the_parts_the_vector_pins(vector):
    name = vector["name"]
    if not vector["valid"]:
        with pytest.raises(ValueError):
            parse(name)
        return
    parsed = parse(name)
    assert parsed.project == vector["project"]
    assert parsed.role == vector["role"]
    assert parsed.team == vector["team"]
    assert parsed.seq == vector["seq"]
    assert str(parsed) == name


@pytest.mark.parametrize("vector", load_vectors(), ids=lambda v: repr(v["name"]))
def test_an_invalid_vector_fails_at_the_step_its_note_names(vector):
    """The note says which step rejects it; `parse` has to say the same one.

    Without this a name could be rejected for a different reason than the one
    the vector documents - a trailing newline, for instance, might fail at the
    team segment instead of at the character check, and the valid/invalid
    verdict would still be right.
    """
    if vector["valid"]:
        pytest.skip("a legal name fails at no step")
    step = re.match(r"step (\d)", vector["note"]).group(1)
    assert f"step {step}" in _reason(vector["name"])


def test_the_length_boundary_is_inside_and_outside():
    """32 characters in, 33 characters out - one character apart."""
    inside = "a" * 26 + "-pm-m1"
    outside = "a" * 27 + "-pm-m1"
    assert len(inside) == 32 == MAX_LEN
    assert len(outside) == 33
    assert is_valid(inside) and reference_is_valid(inside)
    assert not is_valid(outside) and not reference_is_valid(outside)


def test_the_five_steps_are_reported_by_number():
    """A ValueError names the step that failed, so the message can be acted on."""
    assert "step 1" in _reason("MyPMO-pm-m1")
    assert "step 1" in _reason("mypmo-pm-m1\n")
    assert "step 1" in _reason("a" * 27 + "-pm-m1")
    assert "step 1" in _reason("1mypmo-pm-m1")
    assert "step 2" in _reason("mylinuxpool-dev-m1")
    assert "step 4" in _reason("mypmo-pm")
    assert "step 4" in _reason("mylinuxpool-impl-workerpool-1")
    assert "step 5" in _reason("mypmo-pm-m1-1")
    assert "step 5" in _reason("mylinuxpool-impl-w1")
    assert "step 5" in _reason("mylinuxpool-impl-w1-0")
    # Step 4 is checked before step 5: a team segment that is a role word leaves
    # no team at all, and that is the step to report.
    assert "step 4" in _reason("mylinuxpool-impl-pm")


def test_the_lowercase_hint_is_in_the_step_1_message():
    assert "lowercase" in _reason("MyPMO-pm-m1")


def test_the_missing_team_message_says_so():
    reason = _reason("mypmo-pm")
    assert "team" in reason and "missing" in reason


def test_parse_reads_the_role_from_the_right():
    # A project name may contain a role word; the rightmost role segment wins.
    parsed = parse("pm-tools-impl-m1-1")
    assert (parsed.project, parsed.role, parsed.team, parsed.seq) == ("pm-tools", "impl", "m1", 1)

    parsed = parse("test-test-impl-w1-1")
    assert (parsed.project, parsed.role, parsed.team, parsed.seq) == ("test-test", "impl", "w1", 1)

    # A name whose only role segment is the first one has no project at all.
    with pytest.raises(ValueError, match="step 2"):
        parse("impl-w1-1")


def test_parse_returns_an_immutable_name():
    parsed = parse("mylinuxpool-impl-w1-1")
    assert isinstance(parsed, Name)
    assert parsed == Name(project="mylinuxpool", role="impl", team="w1", seq=1)
    with pytest.raises(Exception):
        parsed.role = "pm"  # type: ignore[misc]


def test_format_writes_the_name_and_checks_it():
    assert format("mylinuxpool", "impl", "w1", 1) == "mylinuxpool-impl-w1-1"
    assert format("mypmo", "pm", "m1") == "mypmo-pm-m1"
    assert format("myaistorage", "arch", "m1", 2) == "myaistorage-arch-m1-2"


@pytest.mark.parametrize(
    "parts",
    [
        ("mypmo", "pm", "m1", 1),       # pm takes no sequence number
        ("mypmo", "secretary", "p1", 2),  # nor does secretary
        ("mylinuxpool", "impl", "w1", None),  # impl needs one
        ("mylinuxpool", "impl", "w1", 0),
        ("mylinuxpool", "impl", "w1", 100),
        ("MyPMO", "pm", "m1", None),
        ("mylinuxpool", "dev", "w1", None),
        ("mylinuxpool", "impl", "workerpool", 1),
    ],
)
def test_format_refuses_to_write_an_illegal_name(parts):
    with pytest.raises(ValueError):
        format(*parts)


def test_format_round_trips_through_parse():
    for vector in load_vectors():
        if not vector["valid"]:
            continue
        written = format(vector["project"], vector["role"], vector["team"], vector["seq"])
        assert written == vector["name"]
        assert parse(written) == parse(vector["name"])


def test_is_group_only_looks_at_the_first_character():
    assert is_group("@all") is True
    assert is_group("@") is True
    assert is_group("mypmo-pm-m1") is False
    assert is_group("") is False
    assert is_group(None) is False
    # A group address is not an AI name: the two never overlap.
    assert is_group("@all") and not is_valid("@all")


def test_the_constants_are_the_designs():
    assert ROLES == ("pm", "arch", "impl", "test", "rev", "secretary")
    assert SINGLE_ROLES == ("pm", "secretary")
    assert MAX_LEN == 32


def test_is_valid_is_false_for_things_that_are_not_strings():
    for value in (None, 3, ["mypmo-pm-m1"], b"mypmo-pm-m1"):
        assert is_valid(value) is False
        with pytest.raises(ValueError):
            parse(value)


def test_the_step_parser_and_the_reference_regex_agree_on_random_strings():
    """A deterministic fuzz: the same rules, written twice, on every short string."""
    rng = random.Random(20261018)
    alphabet = "abz09- pmrevsecretary_@\nMP"
    mismatches = []
    for _ in range(60000):
        length = rng.randint(0, 14)
        candidate = "".join(rng.choice(alphabet) for _ in range(length))
        if is_valid(candidate) != reference_is_valid(candidate):
            mismatches.append(candidate)
    assert mismatches == [], f"parse and the reference regex disagree on {mismatches[:5]}"


def _reason(name: object) -> str:
    """The ValueError message `parse` raises for `name`."""
    with pytest.raises(ValueError) as caught:
        parse(name)
    return str(caught.value)
"""Unit tests for `pmo.sanitize` (`#110` T-1, design §3.2, D-12).

The credential values below are made at run time from their parts, so no file in
the repository holds a string that a credential scanner would flag: the point of
the test is the shape, not a real key (AGENTS.md, tests/unit/test_credscan.py).
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import random
import string

import pytest

from pmo.sanitize import (
    REDACTED,
    SECRET_FIELD,
    TOKEN_SHAPES,
    has_credential_shape,
    known_fields_only,
    sanitize,
)

_RUN = string.ascii_letters + string.digits + "_-"


def _run(length: int, alphabet: str = _RUN) -> str:
    """A fixed pseudo-random run, so a failure names the same value again."""
    rng = random.Random(110)
    return "".join(rng.choice(alphabet) for _ in range(length))


def _shapes() -> list[tuple[str, str]]:
    """(label, credential-shaped text), one per shape `sanitize` redacts."""
    return [
        ("google-access-token", "ya29" + "." + _run(24)),
        ("google-refresh-token", "1//0" + _run(24)),
        ("google-client-secret", "GOCSPX" + "-" + _run(24)),
        ("github-token", "gh" + "p_" + _run(36, string.ascii_letters + string.digits)),
        ("github-pat", "github" + "_pat_" + _run(36, string.ascii_letters + string.digits)),
        ("anthropic-key", "sk" + "-ant-" + _run(24)),
        ("sk-key", "sk" + "-" + _run(24)),
        ("aws-access-key", "AKIA" + _run(16, string.ascii_uppercase + string.digits)),
        ("slack-token", "xox" + "b-" + _run(12, string.ascii_letters + string.digits + "-")),
        ("google-api-key", "AIza" + _run(35)),
        ("private-key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ]


@pytest.mark.parametrize("label,value", _shapes(), ids=[shape for shape, _ in _shapes()])
def test_every_credential_shape_is_redacted(label, value):
    assert sanitize(value) == REDACTED, label


@pytest.mark.parametrize("label,value", _shapes(), ids=[shape for shape, _ in _shapes()])
def test_a_token_inside_a_line_is_redacted_and_the_line_is_kept(label, value):
    line = f"[warn] 讀取失敗 token={value} 其餘照舊"
    assert sanitize(line) == f"[warn] 讀取失敗 token={REDACTED} 其餘照舊", label


def test_a_password_shaped_pair_keeps_its_key_and_hides_the_value():
    value = _run(12)
    assert sanitize(f"password: {value}") == f"password: {REDACTED}"
    assert sanitize(f'"client_secret": "{value}"') == f'"client_secret": "{REDACTED}"'


def test_a_secret_field_value_is_a_credential_shape():
    """`password: …` 這類形狀也算憑證樣式（#355 裁定 5 的 SECRET_FIELD 路徑）。"""
    value = _run(12)
    assert has_credential_shape(f"password: {value}")
    assert has_credential_shape(f'"client_secret": "{value}"')


def test_normal_text_is_not_a_credential_shape():
    for line in (
        "[idle] [next-due=2026-11-03T01:10:00Z]",
        "[msg] from=myaistorage-secretary-m1 text=請看工單 #352",
        "passwordless login is allowed",
        "[msg] text=no secret here",
    ):
        assert not has_credential_shape(line), line


def test_normal_event_lines_are_kept():
    for line in (
        "[idle] [next-due=2026-11-03T01:10:00Z]",
        "[ack] myaistorage-m1 myaistorage#352",
        "team-myaistorage-m1",
        "myaistorage-pm-m1",
        "01JABCDEFGHJKMNPQRSTVWXYZ0",
        "http://127.0.0.1:8471",
        "[msg] from=myaistorage-secretary-m1 text=請看工單 #352",
        "passwordless login is allowed",
    ):
        assert sanitize(line) == line


def test_an_exact_secret_is_redacted_even_without_a_known_shape():
    secret = "not-a-pattern-" + _run(6)
    assert sanitize(f"a {secret} b", secrets=[secret]) == f"a {REDACTED} b"


def test_the_longest_secret_is_replaced_first():
    """Sonnet 中-2: a short secret must not eat the front of a longer one."""
    assert sanitize("abcdWXYZ1234", secrets=["abcd", "abcdWXYZ1234"]) == REDACTED
    assert sanitize("x abcdWXYZ1234 y", secrets=["abcd", "abcdWXYZ1234"]) == f"x {REDACTED} y"
    # Whatever order the caller hands them over in.
    assert sanitize("abcdWXYZ1234", secrets=["abcdWXYZ1234", "abcd"]) == REDACTED


def test_an_empty_secret_is_ignored():
    # An empty string would otherwise match everywhere and blank the whole line.
    assert sanitize("[idle]", secrets=["", None]) == "[idle]"  # type: ignore[list-item]


def test_sanitize_is_idempotent():
    line = f"[warn] token={_shapes()[0][1]} 其餘照舊"
    once = sanitize(line)
    assert sanitize(once) == once


def test_the_pattern_lists_are_regexes_of_the_expected_shape():
    assert all(hasattr(pattern, "sub") for pattern in TOKEN_SHAPES)
    assert SECRET_FIELD.pattern


def test_the_token_shapes_are_agora_credscans_patterns():
    """Architect 低-3: the shapes are a copy of `agora.credscan`; the copy is pinned.

    D-16 keeps `pmo` from importing `agora`, so the patterns are copied; a test
    may import both sides, so a drift on either side shows up here.
    """
    from agora.credscan import PATTERNS

    credscan = {name: pattern.pattern for name, pattern in PATTERNS}
    assert sorted(pattern.pattern for pattern in TOKEN_SHAPES) == sorted(
        pattern for name, pattern in credscan.items() if name != "secret-field"
    )


def test_the_secret_field_shape_matches_agora_credscans():
    """The one shape whose value (not the run) is redacted, compared by behaviour."""
    from agora.credscan import PATTERNS

    credscan_secret = dict(PATTERNS)["secret-field"]
    samples = [
        "password: hunter2hunter2",
        'client_secret = "abcdefgh"',
        '{"refresh_token": "abcdefghij"}',
        '{"access_token":"short"}',
        "passwordless login is allowed",
        "[msg] text=no secret here",
    ]
    for sample in samples:
        assert [match.span() for match in SECRET_FIELD.finditer(sample)] == [
            match.span() for match in credscan_secret.finditer(sample)
        ], sample


def test_known_fields_only_drops_every_other_field():
    # D-12: a sidecar response prints its known fields, never the rest.
    record = {"id": "a1", "state": "running", "token": "x", "host": "127.0.0.1"}
    assert known_fields_only(record, ("id", "state")) == {"id": "a1", "state": "running"}


def test_known_fields_only_on_an_empty_record():
    assert known_fields_only({}, ("id",)) == {}

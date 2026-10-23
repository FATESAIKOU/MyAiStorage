"""credscan (#189): real-format secrets are reported, base64 look-alikes are not, values are never printed.

Every secret is assembled at run time from a split prefix and random characters, so
this file holds nothing a scanner would flag. Only tmp_path files are read.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import base64
import io
import json
import random
import string

import pytest

from agora import credscan

ALNUM = string.ascii_letters + string.digits


def rnd(n: int, alphabet: str = ALNUM) -> str:
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(n))


def gh_token() -> str:
    return "gh" + "p_" + rnd(36)


def google_refresh() -> str:
    return "1/" + "/0" + rnd(40)


def run(argv, tmp_path, capsys=None):
    buf = io.StringIO()
    code = credscan.main([str(a) for a in argv], out=buf)
    return code, buf.getvalue()


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def big_b64_with(needle: str) -> str:
    """A long base64 payload with `needle` spliced into the middle of it."""
    body = base64.b64encode(random.SystemRandom().randbytes(600)).decode().replace("=", "A")
    mid = len(body) // 2
    return body[:mid] + needle + body[mid:]


def test_image_source_data_is_not_reported(tmp_path):
    secret = gh_token()
    row = {"type": "user", "message": {"content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA" + secret + "AAAA"}}]}}
    f = write_jsonl(tmp_path / "s.jsonl", [row])
    code, out = run([f], tmp_path)
    assert code == 0 and out == ""


def test_tool_result_file_base64_is_not_reported(tmp_path):
    row = {"toolUseResult": {"file": {"base64": "xx" + google_refresh() + "yy", "type": "image/png"}}}
    f = write_jsonl(tmp_path / "s.jsonl", [row])
    assert run([f], tmp_path)[0] == 0


def test_lookalike_inside_long_base64_text_is_not_reported(tmp_path):
    f = tmp_path / "blob.txt"
    f.write_text(big_b64_with("1/" + "/0" + rnd(30)) + "\n" + big_b64_with(gh_token()) + "\n")
    code, out = run([f], tmp_path)
    assert code == 0 and out == ""


def test_plain_text_secret_is_reported(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text(f"token is {gh_token()} ok\n")
    code, out = run([f], tmp_path)
    assert code == 1
    assert "github-token: 1" in out


@pytest.mark.parametrize("make,name", [
    (lambda: "ya" + "29." + rnd(40), "google-access-token"),
    (google_refresh, "google-refresh-token"),
    (lambda: "GOCSPX" + "-" + rnd(28), "google-client-secret"),
    (lambda: "sk" + "-ant-" + rnd(40), "anthropic-key"),
    (lambda: "sk" + "-" + rnd(40), "sk-key"),
    (lambda: "AK" + "IA" + rnd(16, string.ascii_uppercase + string.digits), "aws-access-key"),
    (lambda: "xo" + "xb-" + rnd(24), "slack-token"),
    (lambda: "AI" + "za" + rnd(35), "google-api-key"),
    (lambda: "-----BEGIN " + "RSA PRIVATE" + " KEY-----", "private-key"),
    (lambda: "github" + "_pat_" + rnd(40), "github-pat"),
])
def test_each_format_is_reported(tmp_path, make, name):
    f = tmp_path / "t.txt"
    f.write_text("x " + make() + " y\n")
    code, out = run([f], tmp_path)
    assert code == 1 and f"{name}: 1" in out


def test_secret_field_value_in_json_is_reported(tmp_path):
    inner = json.dumps({"client" + "_secret": rnd(20)})
    f = write_jsonl(tmp_path / "s.jsonl", [{"type": "assistant", "text": inner}])
    code, out = run([f], tmp_path)
    assert code == 1 and "secret-field: 1" in out


def test_output_never_contains_value_or_context(tmp_path):
    secret = gh_token()
    f = tmp_path / "t.txt"
    f.write_text(f"BEFORE-CONTEXT {secret} AFTER-CONTEXT\n")
    code, out = run([f], tmp_path)
    assert code == 1
    assert secret not in out and secret[4:12] not in out
    assert "CONTEXT" not in out
    assert "len=40" in out and "kinds=4" in out and "sha256=" in out


def test_same_value_has_same_hash(tmp_path):
    secret = gh_token()
    f = tmp_path / "t.txt"
    f.write_text(f"{secret}\n{secret}\n{gh_token()}\n")
    _, out = run([f], tmp_path)
    hashes = [w.split("=")[1] for ln in out.splitlines() for w in ln.split() if w.startswith("sha256=")]
    assert len(hashes) == 3 and hashes[0] == hashes[1] != hashes[2]


def test_unreadable_file_is_exit_2(tmp_path):
    assert run([tmp_path / "missing.jsonl"], tmp_path)[0] == 2


def test_invalid_json_line_falls_back_to_text(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text('{"broken": ' + gh_token() + "\n")
    assert run([f], tmp_path)[0] == 1


def make_projects(tmp_path, sid, jsonl_rows, sidecar_text=None):
    proj = tmp_path / "projects" / "-some-proj"
    proj.mkdir(parents=True)
    write_jsonl(proj / f"{sid}.jsonl", jsonl_rows)
    if sidecar_text is not None:
        (proj / sid / "tool-results").mkdir(parents=True)
        (proj / sid / "tool-results" / "out.txt").write_text(sidecar_text)
    return tmp_path / "projects"


def test_claude_session_scans_sidecar_dir(tmp_path):
    sid = "abc-123"
    projects = make_projects(tmp_path, sid, [{"type": "user", "text": "clean"}], f"x {gh_token()}\n")
    code, out = run(["--claude-session", sid, "--claude-projects", projects], tmp_path)
    assert code == 1 and "out.txt" in out and "github-token: 1" in out


def test_claude_session_projects_from_env(tmp_path, monkeypatch):
    sid = "env-1"
    projects = make_projects(tmp_path, sid, [{"type": "user", "text": gh_token()}])
    monkeypatch.setenv(credscan.PROJECTS_ENV, str(projects))
    assert run(["--claude-session", sid], tmp_path)[0] == 1


def test_claude_session_not_found_is_exit_2(tmp_path):
    (tmp_path / "projects").mkdir()
    assert run(["--claude-session", "nope", "--claude-projects", tmp_path / "projects"], tmp_path)[0] == 2


def test_clean_file_is_exit_0(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("nothing to see\n")
    assert run([f], tmp_path) == (0, "")


# --- round 2: reviewer's B1-B3, N1, N2 ---


def test_long_plain_token_is_reported(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text('Authorization: "' + "ya" + "29." + rnd(250) + '"\n')
    code, out = run([f], tmp_path)
    assert code == 1 and "google-access-token: 1" in out and "len=255" in out


def test_long_jwt_like_secret_field_is_reported(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("access" + "_token=" + rnd(560, ALNUM + "._-") + "\n")
    code, out = run([f], tmp_path)
    assert code == 1 and "secret-field: 1" in out


def test_token_after_long_path_is_reported(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("/".join(rnd(25) for _ in range(9)) + " " + gh_token() + "\n")
    assert run([f], tmp_path)[0] == 1


def test_lookalike_with_base64_on_one_side_only_is_reported(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text(rnd(300) + " " + gh_token() + "\n")
    assert run([f], tmp_path)[0] == 1


@pytest.mark.parametrize("key", ["refresh" + "_token", "Access" + "_Token", "PASS" + "WORD", "client" + "_secret"])
def test_structured_json_secret_field_is_reported(tmp_path, key):
    f = write_jsonl(tmp_path / "s.jsonl", [{"toolUseResult": {key: rnd(20)}}])
    code, out = run([f], tmp_path)
    assert code == 1 and "secret-field: 1" in out
    assert "sha256=" in out


def test_structured_secret_field_short_or_non_string_is_not_reported(tmp_path):
    f = write_jsonl(tmp_path / "s.jsonl", [{"pass" + "word": "short", "access" + "_token": None, "refresh" + "_token": 12345678}])
    assert run([f], tmp_path)[0] == 0


def test_structured_secret_value_not_printed(tmp_path):
    value = rnd(24)
    f = write_jsonl(tmp_path / "s.jsonl", [{"client" + "_secret": value}])
    _, out = run([f], tmp_path)
    assert value not in out and value[:8] not in out


@pytest.mark.parametrize("name", ["GOOGLE_CLIENT" + "_SECRET", "DB_PASS" + "WORD", "my_refresh" + "_token"])
def test_prefixed_env_style_field_is_reported(tmp_path, name):
    f = tmp_path / ".env"
    f.write_text(f"export {name}={rnd(20)}\n")
    code, out = run([f], tmp_path)
    assert code == 1 and "secret-field: 1" in out


def test_field_name_inside_a_longer_word_is_not_reported(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("passwords=" + rnd(20) + "\nxpassword_hash=" + rnd(20) + "\n")
    assert run([f], tmp_path)[0] == 0


def test_image_url_source_is_still_scanned(tmp_path):
    row = {"type": "user", "message": {"content": [
        {"type": "image", "source": {"type": "url", "url": "https://x.example/p.png?k=" + gh_token()}}]}}
    f = write_jsonl(tmp_path / "s.jsonl", [row])
    code, out = run([f], tmp_path)
    assert code == 1 and "github-token: 1" in out


def test_base64_key_with_non_base64_text_is_still_scanned(tmp_path):
    f = write_jsonl(tmp_path / "s.jsonl", [{"base64": "remote token " + gh_token() + " here"}])
    assert run([f], tmp_path)[0] == 1


def test_realistic_image_base64_with_lookalike_is_not_reported(tmp_path):
    row = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big_b64_with("1/" + "/0" + rnd(30))}}
    other = {"toolUseResult": {"file": {"base64": big_b64_with(gh_token()[:4].replace("_", "A") + rnd(40))}}}
    f = write_jsonl(tmp_path / "s.jsonl", [row, other])
    assert run([f], tmp_path)[0] == 0


def test_deeply_nested_line_falls_back_and_later_files_are_scanned(tmp_path):
    deep = tmp_path / "deep.jsonl"
    deep.write_text("[" * 100000 + "\n")
    later = tmp_path / "later.txt"
    later.write_text(gh_token() + "\n")
    code, out = run([deep, later], tmp_path)
    assert code == 1 and "later.txt" in out and "github-token: 1" in out


def test_deeply_nested_line_with_secret_is_scanned_as_text(tmp_path):
    deep = tmp_path / "deep.jsonl"
    deep.write_text("[" * 100000 + gh_token() + "\n")
    code, out = run([deep], tmp_path)
    assert code == 1 and "github-token: 1" in out


def test_data_key_outside_a_base64_source_is_still_scanned(tmp_path):
    f = write_jsonl(tmp_path / "s.jsonl", [{"type": "text", "data": "x " + gh_token()}])
    assert run([f], tmp_path)[0] == 1


@pytest.mark.parametrize("key", ["db_pass" + "word", "GOOGLE_CLIENT" + "_SECRET", "my_access" + "_token"])
def test_prefixed_json_key_is_reported(tmp_path, key):
    f = write_jsonl(tmp_path / "s.jsonl", [{key: rnd(20)}])
    code, out = run([f], tmp_path)
    assert code == 1 and "secret-field: 1" in out


@pytest.mark.parametrize("key", ["pass" + "words", "pass" + "word_hash", "xpass" + "word2"])
def test_longer_json_key_is_not_reported(tmp_path, key):
    f = write_jsonl(tmp_path / "s.jsonl", [{key: rnd(20)}])
    assert run([f], tmp_path)[0] == 0

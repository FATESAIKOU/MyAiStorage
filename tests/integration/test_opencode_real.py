"""The opencode adapter against the real opencode and the real store.

Marked integration, so `uv run pytest -q` skips it:
    uv run pytest -q -m integration tests/integration/test_opencode_real.py

Everything runs in /tmp/agora-it-opencode/proj, a throwaway git repo, so the
user's own sessions are out of reach (spike V5: opencode scopes sessions to the
project directory). The conversation is self-authored filler about turning a CSV
into a Markdown table; the model is a free one.

The sessions this creates are deleted by id at the end, one at a time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agora.agents import opencode as oc

pytestmark = pytest.mark.integration

MODEL = os.environ.get("AGORA_TEST_MODEL", "opencode/space-bunny-free")
PROJ = Path("/tmp/agora-it-opencode/proj")

# OC4: an earlier version of this file set the *real* HOME in a module-scope
# fixture and was then overridden by conftest's autouse per-test HOME, so the
# test body and the teardown used two different opencode databases. Now the
# database is isolated on purpose: HOME (and the XDG_* vars) point at a folder
# that belongs to this module and to nothing else, and teardown deletes exactly
# the ids that were recorded. The free model needs no auth, so nothing is lost.

FIRST_ASK = "把 CSV 轉成 Markdown 表格，先列三個步驟就好，不要真的動手做。"
SECOND_ASK = "你前面在做什麼？用一句話回答。"


#: The free model is a shared, rate-limited endpoint. Calls that normally take
#: 45s have been measured taking more than seven minutes with no output at all
#: (spike V1 saw the same hang). Retry a couple of times, then skip rather than
#: fail: a slow third-party model is not a defect in the adapter.
MODEL_TIMEOUT = int(os.environ.get("AGORA_TEST_TIMEOUT", "300"))
MODEL_ATTEMPTS = int(os.environ.get("AGORA_TEST_ATTEMPTS", "3"))


def _opencode(*args: str, cwd: Path = PROJ, timeout: int = MODEL_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(["opencode", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def ask(*args: str) -> subprocess.CompletedProcess:
    """`opencode run`, with retries; skip the test if the model stays silent."""
    last = ""
    for attempt in range(MODEL_ATTEMPTS):
        try:
            proc = _opencode(*args)
        except subprocess.TimeoutExpired:
            last = f"timed out after {MODEL_TIMEOUT}s (attempt {attempt + 1})"
            continue
        if proc.returncode == 0:
            return proc
        last = proc.stderr[-400:]
    pytest.skip(f"the free model did not answer: {last}")


def raw_of(session_id: str) -> bytes:
    """Export through a file, the way the adapter does (spike V1: never a pipe)."""
    out = Path("/tmp/agora-it-opencode/raw.json")
    with open(out, "wb") as handle:
        proc = subprocess.run(["opencode", "export", session_id], cwd=str(PROJ),
                              stdout=handle, stderr=subprocess.DEVNULL)
    assert proc.returncode == 0
    return out.read_bytes()


@pytest.fixture(scope="module")
def opencode_home(tmp_path_factory):
    """A HOME that exists only for this module, with an empty opencode database."""
    home = tmp_path_factory.mktemp("opencode-home")
    return home


@pytest.fixture
def isolated_store(opencode_home, monkeypatch):
    """Point this test at that HOME. Function-scoped through monkeypatch, so the
    teardown below still sees it."""
    monkeypatch.setenv("HOME", str(opencode_home))
    for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return opencode_home


@pytest.fixture(scope="module")
def project(opencode_home):
    PROJ.mkdir(parents=True, exist_ok=True)
    if not (PROJ / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(PROJ), check=True)
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(PROJ),
                      capture_output=True).returncode != 0:
        subprocess.run(["git", "-c", "user.email=agora@example.invalid",
                        "-c", "user.name=agora", "commit", "-q", "--allow-empty",
                        "-m", "init"], cwd=str(PROJ), check=True)
    return PROJ


@pytest.fixture
def trash(project, isolated_store):
    """Delete the ids this test recorded, one at a time, and nothing else.

    Recording is the point: the database is this module's, so deleting by id is
    both sufficient and impossible to overreach (design.md section 7).
    """
    recorded: list[str] = []
    yield recorded
    for session_id in sorted(set(recorded)):
        subprocess.run(["opencode", "session", "delete", session_id],
                       cwd=str(PROJ), capture_output=True)


@pytest.fixture
def source(project, isolated_store, trash):
    """A real session built with the free model, from self-authored filler.

    It records its own id for cleanup the moment it exists, so a failure later in
    the test still tidies up after it.
    """
    proc = ask("run", "-m", MODEL, "--format", "json", "--title",
               "agora-it-source", FIRST_ASK)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    session_id = next(event["sessionID"] for event in events if "sessionID" in event)
    trash.append(session_id)
    return session_id


def test_real_round_trip(project, source, trash, isolated_store):
    """export -> start_native -> one real turn -> collect (design.md 5.2, 5.4)."""
    exported = oc.ADAPTER.export(source)
    assert exported.message_count >= 2
    assert exported.dir, "spike V5: the export records the project directory"
    assert exported.agent_version and exported.agent_version[0].isdigit()
    assert exported.created_at.endswith("Z")
    untouched = raw_of(source)

    launch = oc.ADAPTER.start_native(exported.raw, project)
    trash.append(launch.agent_session_id)
    assert launch.before_count == exported.message_count
    assert launch.cwd == str(project)
    # launch.argv is the TUI (`opencode --session <id>`); the headless form used
    # here is `run --session <id>` on the same session id.
    assert launch.argv[1:] == ["--session", launch.agent_session_id]

    ask("run", "-m", MODEL, "-s", launch.agent_session_id,
        "--format", "json", SECOND_ASK)

    collected = oc.ADAPTER.collect(launch)
    assert collected is not None, "the agent said something new"
    assert collected.session_id == launch.agent_session_id
    assert collected.message_count > launch.before_count
    assert collected.dir == str(project)

    body = oc.ADAPTER.reading(collected.raw)
    assert body.count("## user") >= 2  # the original ask plus the new one
    assert "## assistant" in body

    # the session we branched from is still exactly as it was (spike V1a)
    assert raw_of(source) == untouched


def test_real_injected_round_trip(project, source, trash, tmp_path, isolated_store):
    """The reading version goes in as one user message with a known id (N4)."""
    exported = oc.ADAPTER.export(source)
    reading = tmp_path / "reading.md"
    reading.write_text(oc.ADAPTER.reading(exported.raw), encoding="utf-8")

    launch = oc.ADAPTER.start_injected(reading, project)
    trash.append(launch.agent_session_id)
    assert launch.before_count == 1

    ask("run", "-m", MODEL, "-s", launch.agent_session_id,
        "--format", "json", "你讀到的閱讀版在講什麼？用一句話回答。")

    collected = oc.ADAPTER.collect(launch)
    assert collected is not None
    assert collected.message_count > 1


def test_real_reimport_is_idempotent(project, source, trash, monkeypatch, isolated_store):
    """Importing the same transcript twice under the same id must keep every
    message, and the adapter's post-import count check must pass.

    A genuine collision cannot be staged from the adapter's own output: the id
    salt is derived from the new session id, so two imports never share a message
    id. That is the point of the salt. What the count check really guards is
    opencode accepting *less* than we sent, which the fake reproduces
    deterministically (FAKE_OPENCODE_DROP in tests/unit/test_agent_opencode.py).
    """
    raw = oc.ADAPTER.export(source).raw
    expected = len(json.loads(raw)["messages"])
    pinned = "ses_" + "A" * 16
    trash.append(pinned)
    monkeypatch.setattr(oc, "_session_id", lambda: pinned)

    first = oc.ADAPTER.start_native(raw, project)
    assert first.agent_session_id == pinned and first.before_count == expected
    again = oc.ADAPTER.start_native(raw, project)
    assert again.before_count == expected
    assert oc.ADAPTER.export(pinned).message_count == expected


def test_fixture_is_self_authored():
    """Guard against a fixture that was ever copied from a real session."""
    text = (Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
            / "oc-basic.json").read_text(encoding="utf-8")
    for marker in ("ZZTOOLOUT", "ZZTHINK", "ZZSRCID-", "zzunknown"):
        assert marker in text
    assert "把 CSV 轉成 Markdown 表格" in text


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode is not installed")
def test_opencode_is_the_executable_we_expect():
    assert oc.agent_cmd("opencode") in ("opencode", os.environ.get("AGORA_OPENCODE_CMD", "opencode"))
"""Unit tests for the `atelier` CLI: ls / show / install (openspec atelier-v0, section 1).

Reads and installs run against a real, local-only git pair (a bare "origin" and
a clone whose remote points at it, both under tmp_path): no network, no
credentials. Install targets are derived from HOME / XDG_CONFIG_HOME /
CLAUDE_CONFIG_DIR, all pointed at tmp_path before anything can write, so the
user's real ~/.claude and ~/.config/opencode are never touched.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from atelier import cli
from atelier import repo as repo_mod

# --- the fake harness files (never in the repo tree: PR 1 ships no harness) ----


def _harness(version: str, tag: str, *, role: str = "pm") -> str:
    """One fake harness file: five H2 sections, trailing spaces, a final blank line."""
    return f"""---
type: Harness
id: atelier:worker-team-{role}@{version}
title: 假 harness {role} {version}
status: draft
team: worker-team
role: {role}
version: {version}
models:
  floor: null
  preferred: []
---

## know

{tag} know：專案自己的指示檔與工單規則優先。  
{tag} know 第二行。

## do

{tag} do 內容。

## judge

{tag} judge 內容。

## dont

{tag} dont 內容。

## capabilities

### github

### angareion

### agora

"""


H0 = _harness("v0", "v0")
H1 = _harness("v1", "v1")
H9 = _harness("v9", "v9")
I0 = _harness("v0", "impl-v0", role="impl")

PM = "atelier:worker-team-pm"
IMPL = "atelier:worker-team-impl"
CLAUDE_MARKER = "<!-- atelier:worker-team-pm -->"
CLAUDE_END = "<!-- /atelier:worker-team-pm -->"
IMPL_MARKER = "<!-- atelier:worker-team-impl -->"
IMPL_END = "<!-- /atelier:worker-team-impl -->"


# --- the local-only git pair ---------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"


def _commit(work: Path, message: str) -> None:
    _git("add", "-A", cwd=work)
    _git("-c", "user.name=atelier-test", "-c", "user.email=atelier-test@example.invalid",
         "commit", "-q", "-m", message, cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)


@pytest.fixture
def harness_repo(tmp_path, monkeypatch):
    """A bare origin + the clone angareion would share, with v0/v1 pm and v0 impl."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    work = tmp_path / "work"
    _git("init", "-q", str(work), cwd=tmp_path)
    _git("checkout", "-q", "-b", "main", cwd=work)
    for rel, text in (("atelier/worker-team/pm/v0.md", H0),
                      ("atelier/worker-team/pm/v1.md", H1),
                      ("atelier/worker-team/impl/v0.md", I0)):
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git("remote", "add", "origin", str(origin), cwd=work)
    _commit(work, "harness: fake v0/v1")
    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(clone))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    return SimpleNamespace(origin=origin, work=work, clone=clone)


def _publish(repo, rel: str, text: str) -> None:
    path = repo.work / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _commit(repo.work, f"harness: {rel}")


def _age_fetch_head(clone: Path, seconds: float) -> None:
    """Make the copy look stale (or fresh, with 0)."""
    fetch_head = clone / "FETCH_HEAD"
    if not fetch_head.exists():
        fetch_head.write_text("", encoding="utf-8")
    mtime = os.stat(fetch_head).st_mtime - seconds
    os.utime(fetch_head, (mtime, mtime))


def _run(capsysbinary, *argv):
    code = cli.main([str(a) for a in argv])
    out, err = capsysbinary.readouterr()
    return code, out, err


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under root, `.git` included: a stray lock file must show."""
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file()}


def _block_between(text: str, start: str, end: str) -> str:
    """What sits between one managed block's two marker lines."""
    _, _, rest = text.partition(start + "\n")
    block, _, _ = rest.partition(end)
    return block


# --- show ----------------------------------------------------------------------


def test_show_prints_the_file_byte_for_byte(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert out == H0.encode()
    assert err == b""


def test_show_pins_a_version_instead_of_taking_the_latest(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert out == H0.encode(), "an explicit @v0 must not read the latest (v1)"


def test_show_without_version_reads_the_largest(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 0, err
    assert out == H1.encode()


def test_show_ignores_the_working_tree(harness_repo, capsysbinary):
    edited = harness_repo.clone / "atelier/worker-team/pm/v1.md"
    edited.write_text("LOCAL EDIT\n", encoding="utf-8")
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 0, err
    assert out == H1.encode(), "reads go through origin/main, never the working tree"


def test_show_section_prints_only_that_section(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0",
                          "--section", "dont")
    assert code == 0, err
    assert out == "v0 dont 內容。\n".encode()


def test_show_section_must_be_one_of_the_five(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", PM, "--section", "nope")
    assert code == 1
    assert b"nope" in err


def test_show_json_carries_frontmatter_and_sections(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", PM, "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["id"] == "atelier:worker-team-pm@v1"
    assert data["team"] == "worker-team" and data["role"] == "pm" and data["version"] == "v1"
    assert data["frontmatter"]["models"] == {"floor": None, "preferred": []}
    assert data["sections"]["dont"] == "v1 dont 內容。"


def test_show_json_serializes_yaml_dates(harness_repo, capsysbinary):
    """A frontmatter date (PyYAML hands back `datetime.date`) must still print as JSON.

    The first real harness carries `generated: {by: ..., at: 2026-10-16}` (PR 2);
    before this test, `--json` died with a TypeError on it.
    """
    dated = H1.replace("status: draft", "status: draft\ngenerated: {by: x, at: 2026-10-16}")
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", dated)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v3", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["frontmatter"]["generated"]["at"] == "2026-10-16"


def test_show_json_with_section(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0",
                          "--section", "know", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["section"] == "know"
    assert data["content"].startswith("v0 know：")
    assert data["content"].rstrip().endswith("v0 know 第二行。")


def test_show_works_when_stdout_has_no_binary_buffer(harness_repo, monkeypatch):
    """An embedded caller's stdout may be a plain text stream (L11)."""
    fake = io.StringIO()
    monkeypatch.setattr(_s, "stdout", fake)
    code = cli.main(["show", "atelier:worker-team-pm@v0"])
    assert code == 0
    assert fake.getvalue() == H0


BAD_FRONTMATTER = "---\nkey: [unclosed\nsecret-line: LEAKME\n---\n\n## know\n\n內文\n"


def test_show_json_broken_frontmatter_is_exit_2_and_does_not_leak_the_file(harness_repo,
                                                                           capsysbinary):
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", BAD_FRONTMATTER)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v3", "--json")
    assert code == 2
    text = err.decode()
    assert "LEAKME" not in text, "error messages must not carry harness content"
    assert "secret-line" not in text, "not even the offending YAML snippet"


def test_show_json_without_frontmatter_is_exit_2(harness_repo, capsysbinary):
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", "# no frontmatter\n")
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v3", "--json")
    assert code == 2
    assert err != b""


def test_show_section_missing_from_an_otherwise_valid_file_is_exit_1(harness_repo, capsysbinary):
    only_know = "---\ntype: Harness\nid: atelier:worker-team-pm@v3\n---\n\n## know\n\n只有 know。\n"
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", only_know)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v3",
                          "--section", "dont")
    assert code == 1
    assert b"dont" in err


@pytest.mark.parametrize("bad", ["worker-team-pm", "atelier:worker-team", "atelier:worker-team-pm@0",
                                 "atelier:worker-team-pm@vX"])
def test_show_bad_id_is_a_usage_error(bad, harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", bad)
    assert code == 1
    assert err != b""


def test_show_missing_version_is_exit_1(harness_repo, capsysbinary):
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 1
    assert "v9" in err.decode()
    assert b"v0 know" not in err, "error messages must not leak harness content"


def test_show_missing_role_is_exit_1(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-test")
    assert code == 1


@pytest.mark.parametrize("argv", [("show", "atelier:worker-team-pm@0"),
                                  ("install", "atelier:worker-team-pm@0", "--agent", "claude")])
def test_a_bad_id_without_a_copy_is_a_usage_error(argv, tmp_path, monkeypatch, capsysbinary):
    """A usage error is exit 1, not a copy failure, even with no copy configured (L2)."""
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    code, out, err = _run(capsysbinary, *argv)
    assert code == 1
    assert err != b""


def test_a_usage_error_does_not_fetch(harness_repo, capsysbinary):
    """The id is checked before any network work (L2)."""
    _git("remote", "set-url", "origin", str(harness_repo.origin.parent / "gone"),
         cwd=harness_repo.clone)
    _age_fetch_head(harness_repo.clone, 10_000)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@0")
    assert code == 1
    assert b"Warning" not in err, "an invalid id must be rejected before a fetch"


# --- fetch policy ---------------------------------------------------------------


def test_show_pinned_version_fetches_on_a_miss(harness_repo, capsysbinary):
    _run(capsysbinary, "show", "atelier:worker-team-pm@v0")   # makes FETCH_HEAD
    _publish(harness_repo, "atelier/worker-team/pm/v9.md", H9)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 0, err
    assert out == H9.encode(), "a miss must fetch once and then answer"


def test_a_fresh_copy_is_not_fetched(harness_repo, capsysbinary):
    _run(capsysbinary, "show", "atelier:worker-team-pm@v0")   # makes FETCH_HEAD
    _publish(harness_repo, "atelier/worker-team/pm/v9.md", H9)
    _age_fetch_head(harness_repo.clone, 0)                    # fresh: within the interval
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 1, "a fresh copy must not be fetched again"
    _age_fetch_head(harness_repo.clone, 120)                  # stale: the refresh may run
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 0, err
    assert out == H9.encode()


def test_offline_fetch_failure_warns_once_and_uses_the_copy(harness_repo, capsysbinary):
    _git("remote", "set-url", "origin", str(harness_repo.origin.parent / "gone"),
         cwd=harness_repo.clone)
    _age_fetch_head(harness_repo.clone, 1000)   # past the pull interval: a fetch is attempted
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert out == H0.encode()
    text = err.decode()
    assert text.count("Warning") == 1 and "using the current copy" in text


def test_an_offline_lookup_miss_warns_only_once(harness_repo, capsysbinary):
    """One command tries one fetch, not two (periodic + event): one warning (L5)."""
    _git("remote", "set-url", "origin", str(harness_repo.origin.parent / "gone"),
         cwd=harness_repo.clone)
    _age_fetch_head(harness_repo.clone, 10_000)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 1
    assert err.decode().count("Warning") == 1, "the failed fetch must not be retried per command"


def test_a_lookup_miss_still_fetches_when_the_periodic_pull_was_off(harness_repo, capsysbinary,
                                                                    monkeypatch, tmp_path):
    """`config_pull_interval=0` disables the periodic pull only, not the event fetch (L5)."""
    monkeypatch.delenv("ATELIER_CONFIG_DIR")
    monkeypatch.setenv("ANGAREION_CONFIG",
                       str(_angareion_config(tmp_path, harness_repo.clone,
                                             config_pull_interval=0)))
    _run(capsysbinary, "show", "atelier:worker-team-pm@v0")   # makes FETCH_HEAD
    _publish(harness_repo, "atelier/worker-team/pm/v9.md", H9)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v9")
    assert code == 0, err
    assert out == H9.encode(), "a miss must still fetch once with interval 0"


def _fake_copy(root: Path) -> Path:
    """A directory shaped like a git copy: `.git` is a directory with HEAD."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def test_fetch_uses_the_shared_lock_and_the_20_second_timeout(tmp_path):
    class RecordingGit:
        def __init__(self):
            self.calls: list[list[str]] = []
            self.timeouts: list[float] = []

        def run(self, args, *, cwd, timeout):
            self.calls.append(list(args))
            self.timeouts.append(timeout)
            return repo_mod.GitResult(0, b"", "")

    assert repo_mod.GIT_TIMEOUT_SECONDS == 20.0
    clone = _fake_copy(tmp_path / "clone")   # a copy, not a plain directory
    git = RecordingGit()
    instance = repo_mod.Repo(clone, git=git)
    assert instance.lock_path == clone / ".git" / "angareion-pull.lock"
    assert instance.fetch() is True
    assert git.calls == [["fetch", "--quiet", "origin", "main"]]
    assert git.timeouts == [20.0]


def test_fetch_runs_while_the_shared_lock_is_held(tmp_path):
    """The lock file is really flock-held during git: dropping the lock turns this red (L5)."""
    fcntl = pytest.importorskip("fcntl")

    class LockProbeGit:
        def __init__(self):
            self.lock_was_held: bool | None = None

        def run(self, args, *, cwd, timeout):
            lock = Path(cwd) / ".git" / repo_mod.FETCH_LOCK_FILENAME
            try:
                fd = os.open(lock, os.O_RDONLY)
            except FileNotFoundError:
                self.lock_was_held = False   # no lock file at all: nothing was held
                return repo_mod.GitResult(0, b"", "")
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self.lock_was_held = True   # someone holds it: the fetch is serialized
                else:
                    self.lock_was_held = False
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return repo_mod.GitResult(0, b"", "")

    clone = _fake_copy(tmp_path / "clone")
    probe = LockProbeGit()
    instance = repo_mod.Repo(clone, git=probe)
    assert instance.fetch() is True
    assert probe.lock_was_held is True, "git must run under the shared flock"


def test_clone_path_falls_back_to_the_angareion_config(harness_repo, tmp_path, monkeypatch,
                                                       capsysbinary):
    monkeypatch.delenv("ATELIER_CONFIG_DIR")
    config = tmp_path / "angareion-config.json"
    config.write_text(json.dumps({"identity": "x-pm", "config_dir": str(harness_repo.clone),
                                  "registry_repo": "o/MyAiStorage", "registry_issue": 1}),
                      encoding="utf-8")
    monkeypatch.setenv("ANGAREION_CONFIG", str(config))
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert out == H0.encode()


def _angareion_config(tmp_path: Path, clone: Path, **extra) -> Path:
    config = tmp_path / "angareion-config.json"
    config.write_text(json.dumps({"identity": "x-pm", "config_dir": str(clone),
                                  "registry_repo": "o/MyAiStorage", "registry_issue": 1,
                                  **extra}),
                      encoding="utf-8")
    return config


def _offline_stale_copy(harness_repo, seconds: float, tmp_path: Path, monkeypatch, **config):
    """Point the clone at angareion's config, make origin unreachable and the copy stale."""
    monkeypatch.delenv("ATELIER_CONFIG_DIR")
    monkeypatch.setenv("ANGAREION_CONFIG",
                       str(_angareion_config(tmp_path, harness_repo.clone, **config)))
    _git("remote", "set-url", "origin", str(harness_repo.origin.parent / "gone"),
         cwd=harness_repo.clone)
    _age_fetch_head(harness_repo.clone, seconds)


def test_pull_interval_zero_from_the_angareion_config_disables_the_periodic_pull(
        harness_repo, tmp_path, monkeypatch, capsysbinary):
    """`config_pull_interval: 0` (angareion's "no periodic pull") still applies here (L4)."""
    _offline_stale_copy(harness_repo, 10_000, tmp_path, monkeypatch, config_pull_interval=0)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert out == H0.encode()
    assert b"Warning" not in err, "interval 0: no periodic fetch, so no fetch failure"


def test_a_configured_pull_interval_is_honoured(harness_repo, tmp_path, monkeypatch, capsysbinary):
    """A longer interval than the 900s default also silences the periodic fetch (L4)."""
    _offline_stale_copy(harness_repo, 1000, tmp_path, monkeypatch, config_pull_interval=10_000)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert b"Warning" not in err, "1000s < 10000s: no fetch yet"


def test_without_the_field_the_900_second_default_is_used(harness_repo, tmp_path, monkeypatch,
                                                           capsysbinary):
    _offline_stale_copy(harness_repo, 1000, tmp_path, monkeypatch)
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert err.decode().count("Warning") == 1, "no config field: the 900s default fetches"


def test_a_bad_pull_interval_falls_back_to_the_default(harness_repo, tmp_path, monkeypatch,
                                                       capsysbinary):
    _offline_stale_copy(harness_repo, 1000, tmp_path, monkeypatch, config_pull_interval="soon")
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert code == 0, err
    assert err.decode().count("Warning") == 1, "a non-integer interval is ignored, not honoured"


def test_show_without_a_configured_copy_is_exit_2(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 2
    assert "clone" in err.decode()


def test_a_plain_directory_is_not_a_copy_and_is_left_alone(tmp_path, monkeypatch, capsysbinary):
    """A directory that exists but is not a git copy gets no .git and no lock file (L1)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(plain))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    before = _tree(plain)
    code, out, err = _run(capsysbinary, "ls")
    assert code == 2
    assert "複本" in err.decode()
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 2
    assert _tree(plain) == before, "a non-git directory must not get a .git or a lock file"


def test_a_git_worktree_is_refused_without_a_traceback_or_a_lock_file(harness_repo, tmp_path,
                                                                       monkeypatch, capsysbinary):
    """A worktree's .git is a file: refused as not-a-copy, before the lock (中1)."""
    worktree = tmp_path / "worktree"
    _git("worktree", "add", "--detach", str(worktree), "HEAD", cwd=harness_repo.clone)
    assert (worktree / ".git").is_file()
    git_file_before = (worktree / ".git").read_bytes()
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(worktree))
    before = _tree(worktree)
    code, out, err = _run(capsysbinary, "ls")
    assert code == 2
    assert b"Traceback" not in err
    assert (worktree / ".git").read_bytes() == git_file_before
    assert _tree(worktree) == before, "no lock file may be left behind"


def test_a_read_only_git_directory_is_exit_2_without_a_traceback(tmp_path, monkeypatch,
                                                                  capsysbinary):
    """A copy whose lock cannot be taken is a copy problem: exit 2, no traceback (中1)."""
    clone = _fake_copy(tmp_path / "ro-clone")
    git_dir = clone / ".git"
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(clone))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    os.chmod(git_dir, 0o500)
    try:
        code, out, err = _run(capsysbinary, "ls")
    finally:
        os.chmod(git_dir, 0o755)
    assert code == 2
    assert b"Traceback" not in err
    assert not (git_dir / "angareion-pull.lock").exists()


def test_a_git_directory_without_head_is_refused_and_gets_no_lock_file(tmp_path, monkeypatch,
                                                                       capsysbinary):
    """An empty fake .git is not a copy: no lock file may appear in it (L4)."""
    fake = tmp_path / "fake"
    (fake / ".git").mkdir(parents=True)
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(fake))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    before = _tree(fake)
    code, out, err = _run(capsysbinary, "ls")
    assert code == 2
    assert b"Traceback" not in err
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 2
    assert _tree(fake) == before, "a fake .git must not get a lock file"


# --- ls --------------------------------------------------------------------------


def test_ls_lists_every_role_and_version(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "ls")
    assert code == 0, err
    assert out.decode().splitlines() == [
        "atelier:worker-team-impl@v0",
        "atelier:worker-team-pm@v0",
        "atelier:worker-team-pm@v1",
    ]


def test_ls_json(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "ls", "--json")
    assert code == 0, err
    rows = json.loads(out)
    assert rows == [
        {"id": "atelier:worker-team-impl@v0", "team": "worker-team", "role": "impl", "version": "v0"},
        {"id": "atelier:worker-team-pm@v0", "team": "worker-team", "role": "pm", "version": "v0"},
        {"id": "atelier:worker-team-pm@v1", "team": "worker-team", "role": "pm", "version": "v1"},
    ]


def test_ls_without_a_configured_copy_is_exit_2(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    code, out, err = _run(capsysbinary, "ls")
    assert code == 2
    assert err != b""


def test_reads_do_not_touch_the_working_tree_or_the_cwd(harness_repo, tmp_path, monkeypatch,
                                                          capsysbinary):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# project\n", encoding="utf-8")
    monkeypatch.chdir(project)
    _run(capsysbinary, "ls")   # the first lookup may fetch; later ones must not touch anything
    clone_before = _tree(harness_repo.clone)   # .git included: no lock file may appear
    cwd_before = _tree(project)
    _run(capsysbinary, "ls")
    _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert _tree(harness_repo.clone) == clone_before, "reads must not write the clone at all"
    assert _tree(project) == cwd_before, "reads must not write the working directory"


# --- install ---------------------------------------------------------------------


@pytest.fixture
def install_env(harness_repo, tmp_path, monkeypatch):
    home = tmp_path / "user-home"   # tmp_path/"home" is the conftest's isolated home
    home.mkdir()
    claude = tmp_path / "claude"
    claude.mkdir()
    xdg = tmp_path / "xdg-config"
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return SimpleNamespace(home=home, claude=claude, xdg=xdg, project=project)


def _claude_file(env) -> Path:
    return env.claude / "rules" / "atelier-worker-team-pm.md"


def _opencode_file(env) -> Path:
    return env.xdg / "opencode" / "AGENTS.md"


def test_install_claude_writes_the_rules_file(harness_repo, install_env, capsysbinary):
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "claude")
    assert code == 0, err
    assert _claude_file(install_env).read_bytes() == H1.encode()
    assert str(_claude_file(install_env)) in out.decode()


def test_install_version_pin_installs_that_version(harness_repo, install_env, capsysbinary):
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v0",
                          "--agent", "claude")
    assert code == 0, err
    assert _claude_file(install_env).read_bytes() == H0.encode()


def test_install_opencode_wraps_the_harness_in_one_managed_block(harness_repo, install_env,
                                                                  capsysbinary):
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    user_text = "# 使用者自己的規矩\n不要做壞事。\n"
    target.write_text(user_text, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    first = target.read_text(encoding="utf-8")
    assert first.startswith(user_text), "the user's own content is kept verbatim"
    assert H1 in first, "the harness is inside the managed block"
    assert first.count(CLAUDE_MARKER) == 1 and first.count(CLAUDE_END) == 1
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    assert target.read_text(encoding="utf-8") == first, "a second install replaces, not stacks"


def test_install_opencode_creates_the_file_when_missing(harness_repo, install_env, capsysbinary):
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    text = _opencode_file(install_env).read_text(encoding="utf-8")
    assert text.startswith(CLAUDE_MARKER + "\n")
    assert H1 in text


@pytest.mark.parametrize("agents", [("claude", "opencode"), ("opencode", "claude"),
                                    ("claude", "claude", "opencode")])
def test_install_repeated_agents_each_installed_once(agents, harness_repo, install_env,
                                                      capsysbinary):
    argv = ["install", PM]
    for agent in agents:
        argv += ["--agent", agent]
    code, out, err = _run(capsysbinary, *argv)
    assert code == 0, err
    assert _claude_file(install_env).read_bytes() == H1.encode()
    assert _opencode_file(install_env).read_text(encoding="utf-8").count(CLAUDE_MARKER) == 1


def test_install_without_agent_is_a_usage_error(harness_repo, install_env, capsysbinary):
    code, out, err = _run(capsysbinary, "install", PM)
    assert code == 1
    assert not (install_env.claude / "rules").exists()
    assert not _opencode_file(install_env).parent.exists()


def test_install_missing_version_is_exit_1(harness_repo, install_env, capsysbinary):
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v9",
                          "--agent", "claude")
    assert code == 1
    assert not _claude_file(install_env).exists()


def test_install_write_failure_is_exit_2(harness_repo, install_env, capsysbinary):
    _opencode_file(install_env).mkdir(parents=True)   # a directory where the file goes
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2
    assert err != b""


def test_install_targets_fall_back_to_home(harness_repo, install_env, monkeypatch, capsysbinary):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "claude", "--agent", "opencode")
    assert code == 0, err
    assert (install_env.home / ".claude/rules/atelier-worker-team-pm.md").read_bytes() == H1.encode()
    assert (install_env.home / ".config/opencode/AGENTS.md").is_file()


def test_install_never_touches_settings_project_files_or_the_cwd(harness_repo, install_env,
                                                                  capsysbinary):
    settings = install_env.claude / "settings.json"
    settings.write_text('{"permissions": {"allow": []}}\n', encoding="utf-8")
    user_claude = install_env.claude / "CLAUDE.md"
    user_claude.write_text("# user claude\n", encoding="utf-8")
    project_agents = install_env.project / "AGENTS.md"
    project_agents.write_text("# project agents\n", encoding="utf-8")
    project_claude = install_env.project / "CLAUDE.md"
    project_claude.write_text("# project claude\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in (settings, user_claude, project_agents, project_claude)}
    cwd_before = _tree(install_env.project)
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "claude", "--agent", "opencode")
    assert code == 0, err
    for path, data in before.items():
        assert path.read_bytes() == data, f"{path} must be byte-for-byte unchanged"
    assert _tree(install_env.project) == cwd_before, "install must not write the working directory"


# --- install durability and the managed-block edge cases (M1/M2/L6-L9) -----------


def test_install_writes_through_a_same_directory_tmp_file_and_replace(harness_repo, install_env,
                                                                      capsysbinary, monkeypatch):
    """Every write is a same-directory tmp file + os.replace, never a truncating write (M1)."""
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "claude", "--agent", "opencode")
    assert code == 0, err
    assert {dst for _, dst in calls} == {_claude_file(install_env), _opencode_file(install_env)}
    for src, dst in calls:
        assert src.parent == dst.parent, "the tmp file must sit next to its target"
        assert not src.exists(), "os.replace moves the tmp file onto the target"


@pytest.mark.parametrize("agent", ["claude", "opencode"])
def test_a_failed_install_write_keeps_the_old_file_and_leaves_no_residue(
        agent, harness_repo, install_env, capsysbinary, monkeypatch):
    """A write that cannot be committed leaves only the old file - no half block (M1)."""
    if agent == "opencode":
        target = _opencode_file(install_env)
        target.parent.mkdir(parents=True)
        target.write_text("USER\n", encoding="utf-8")
    else:
        code, out, err = _run(capsysbinary, "install", PM, "--agent", "claude")
        assert code == 0, err
        target = _claude_file(install_env)
    before_bytes = target.read_bytes()
    before_listing = sorted(p.name for p in target.parent.iterdir())

    real_replace = os.replace

    def boom(src, dst, *args, **kwargs):
        raise OSError("simulated failure before the new file is in place")

    monkeypatch.setattr(os, "replace", boom)
    code, out, err = _run(capsysbinary, "install", PM, "--agent", agent)
    assert code == 2
    assert err != b""
    monkeypatch.setattr(os, "replace", real_replace)
    assert target.read_bytes() == before_bytes, "a failed write must leave the old file"
    assert sorted(p.name for p in target.parent.iterdir()) == before_listing, "no tmp residue"
    code, out, err = _run(capsysbinary, "install", PM, "--agent", agent)
    assert code == 0, "the failure must not poison later installs"


def test_install_opencode_with_a_non_utf8_target_is_exit_2_without_a_traceback(
        harness_repo, install_env, capsysbinary):
    """A target that cannot be read is a file problem: exit 2, no traceback (M2)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe not utf8\n")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2
    assert b"Traceback" not in err
    assert target.read_bytes() == b"\xff\xfe not utf8\n"


def test_two_roles_in_one_file_keep_their_own_blocks(harness_repo, install_env, capsysbinary):
    """pm and impl blocks live side by side; neither replaces the other (L6)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    user = "# 使用者自己的規矩\n"
    target.write_text(user, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    code, out, err = _run(capsysbinary, "install", IMPL, "--agent", "opencode")
    assert code == 0, err
    text = target.read_text(encoding="utf-8")
    assert text.startswith(user), "the user's own content is kept"
    assert text.count(CLAUDE_MARKER) == 1 and text.count(CLAUDE_END) == 1
    assert text.count(IMPL_MARKER) == 1 and text.count(IMPL_END) == 1
    assert _block_between(text, CLAUDE_MARKER, CLAUDE_END) == H1
    assert _block_between(text, IMPL_MARKER, IMPL_END) == I0
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    text = target.read_text(encoding="utf-8")
    assert _block_between(text, IMPL_MARKER, IMPL_END) == I0, "the other role's block is untouched"


def test_install_refuses_a_damaged_managed_block_and_keeps_the_file(harness_repo, install_env,
                                                                    capsysbinary):
    """An opening marker without its closing marker is refused, not appended to (L7)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    damaged = "# user\n" + CLAUDE_MARKER + "\nUSER NOTE\n"
    target.write_text(damaged, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2
    assert err != b""
    assert target.read_text(encoding="utf-8") == damaged


def test_install_refuses_a_stray_opening_marker_that_would_eat_user_content(
        harness_repo, install_env, capsysbinary):
    """A user line equal to our opening marker must not swallow what follows (L8)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    stray = ("# user top\n" + CLAUDE_MARKER + "\nUSER NOTE BETWEEN\n"
             + CLAUDE_MARKER + "\n" + H1 + CLAUDE_END + "\n")
    target.write_text(stray, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2, "an unbalanced pair of marker lines must be refused, not used"
    assert target.read_text(encoding="utf-8") == stray, "the file must be untouched"


def test_install_refuses_a_marker_pair_that_is_not_ours(harness_repo, install_env, capsysbinary):
    """A paired marker written by the user around user content is refused (L8)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    quoted = "# user\n" + CLAUDE_MARKER + "\nUSER QUOTES OUR MARKERS\n" + CLAUDE_END + "\n"
    target.write_text(quoted, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2
    assert target.read_text(encoding="utf-8") == quoted


def test_install_refuses_an_orphan_closing_marker(harness_repo, install_env, capsysbinary):
    """A closing marker alone is refused too (L9: the chosen policy, see tool.md)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    orphan = "# user\n" + CLAUDE_END + "\n"
    target.write_text(orphan, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2
    assert target.read_text(encoding="utf-8") == orphan


def test_install_refuses_a_harness_without_frontmatter_and_writes_nothing(harness_repo,
                                                                          install_env,
                                                                          capsysbinary):
    """install validates first: no frontmatter means exit 2 and nothing is written."""
    _publish(harness_repo, "atelier/worker-team/pm/v3.md",
             "# 沒有 frontmatter\n\n## know\n\n內文\n")
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v3",
                          "--agent", "claude", "--agent", "opencode")
    assert code == 2
    assert err != b"" and b"Traceback" not in err
    assert not (install_env.claude / "rules").exists(), "nothing may be written"
    assert not _opencode_file(install_env).parent.exists(), "nothing may be written"


def test_install_refuses_a_harness_with_broken_frontmatter_and_writes_nothing(harness_repo,
                                                                              install_env,
                                                                              capsysbinary):
    """Frontmatter that exists but is not YAML is refused too - and does not leak."""
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", BAD_FRONTMATTER)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v3",
                          "--agent", "claude")
    assert code == 2
    assert b"LEAKME" not in err
    assert not (install_env.claude / "rules").exists(), "nothing may be written"


def test_install_refuses_a_harness_whose_type_is_not_harness(harness_repo, install_env,
                                                             capsysbinary):
    """The block checker only accepts type: Harness, so install refuses it up front (低6)."""
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", H1.replace("type: Harness", "type: Note"))
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v3",
                          "--agent", "claude", "--agent", "opencode")
    assert code == 2
    assert err != b""
    assert not (install_env.claude / "rules").exists(), "nothing may be written"
    assert not _opencode_file(install_env).parent.exists(), "nothing may be written"


def test_install_opencode_keeps_the_users_line_endings(harness_repo, install_env, capsysbinary):
    """A user file's \\r\\n is kept byte-for-byte outside the block (低1)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    user = b"# user\r\nsecond\r\n"
    target.write_bytes(user)
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    first = target.read_bytes()
    assert first.startswith(user), "\\r\\n outside the block is kept byte-for-byte"
    assert CLAUDE_MARKER.encode() in first and H1.encode() in first
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    assert target.read_bytes() == first, "a second install replaces the block, nothing else"


def test_install_refuses_a_harness_whose_body_contains_a_marker_line(harness_repo, install_env,
                                                                      capsysbinary):
    """A harness body with a whole marker line would make later installs refuse (低2)."""
    polluted = H1.replace("## dont", CLAUDE_END + "\n\n## dont")
    assert CLAUDE_END in polluted.splitlines(), "the test harness really carries the marker line"
    _publish(harness_repo, "atelier/worker-team/pm/v3.md", polluted)
    _age_fetch_head(harness_repo.clone, 120)
    code, out, err = _run(capsysbinary, "install", "atelier:worker-team-pm@v3",
                          "--agent", "opencode")
    assert code == 2
    assert err != b""
    assert not _opencode_file(install_env).parent.exists(), "nothing is written"


def test_an_inline_marker_mention_is_plain_text_not_a_block(harness_repo, install_env,
                                                             capsysbinary):
    """Only whole lines are markers: an inline mention never starts or ends a block (低3)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    inline = "說明：<!-- atelier:worker-team-pm --> 的用法\n"
    target.write_text(inline, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    text = target.read_text(encoding="utf-8")
    assert text.startswith(inline), "the inline line is kept as it was"
    assert text.count(CLAUDE_MARKER) == 2, "the inline mention + our own opening marker"
    assert _block_between(text, CLAUDE_MARKER, CLAUDE_END) == H1
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 0, err
    assert target.read_text(encoding="utf-8") == text, "the inline mention is not a marker"


@pytest.mark.parametrize("body", ["---\nUSER QUOTED FRONTMATTER\n",
                                  "---\ntype: Note\ntitle: 不是 harness\n---\n"])
def test_install_refuses_a_marker_pair_that_only_starts_like_a_harness(body, harness_repo,
                                                                        install_env,
                                                                        capsysbinary):
    """Paired markers are ours only when the block really is a harness (低6)."""
    target = _opencode_file(install_env)
    target.parent.mkdir(parents=True)
    quoted = "# user\n" + CLAUDE_MARKER + "\n" + body + CLAUDE_END + "\n"
    target.write_text(quoted, encoding="utf-8")
    code, out, err = _run(capsysbinary, "install", PM, "--agent", "opencode")
    assert code == 2, "a lookalike block must be refused, not replaced"
    assert target.read_text(encoding="utf-8") == quoted, "the file must be untouched"

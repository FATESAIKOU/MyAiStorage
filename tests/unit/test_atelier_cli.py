"""Unit tests for the `atelier` CLI: ls / show / install (openspec atelier-v0, section 1).

Reads and installs run against a real, local-only git pair (a bare "origin" and
a clone whose remote points at it, both under tmp_path): no network, no
credentials. Install targets are derived from HOME / XDG_CONFIG_HOME /
CLAUDE_CONFIG_DIR, all pointed at tmp_path before anything can write, so the
user's real ~/.claude and ~/.config/opencode are never touched.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

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
CLAUDE_MARKER = "<!-- atelier:worker-team-pm -->"
CLAUDE_END = "<!-- /atelier:worker-team-pm -->"


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
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.parts}


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


def test_show_json_with_section(harness_repo, capsysbinary):
    code, out, err = _run(capsysbinary, "show", "atelier:worker-team-pm@v0",
                          "--section", "know", "--json")
    assert code == 0, err
    data = json.loads(out)
    assert data["section"] == "know"
    assert data["content"].startswith("v0 know：")
    assert data["content"].rstrip().endswith("v0 know 第二行。")


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
    clone = tmp_path / "clone"
    clone.mkdir()
    git = RecordingGit()
    instance = repo_mod.Repo(clone, git=git)
    assert instance.lock_path == clone / ".git" / "angareion-pull.lock"
    assert instance.fetch() is True
    assert git.calls == [["fetch", "--quiet", "origin", "main"]]
    assert git.timeouts == [20.0]


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


def test_show_without_a_configured_copy_is_exit_2(tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setenv("ATELIER_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.delenv("ANGAREION_CONFIG", raising=False)
    code, out, err = _run(capsysbinary, "show", PM)
    assert code == 2
    assert "clone" in err.decode()


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


def test_reads_do_not_touch_the_working_tree_or_the_cwd(harness_repo, tmp_path, monkeypatch,
                                                          capsysbinary):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("# project\n", encoding="utf-8")
    monkeypatch.chdir(project)
    clone_before = _tree(harness_repo.clone)
    cwd_before = _tree(project)
    _run(capsysbinary, "ls")
    _run(capsysbinary, "show", "atelier:worker-team-pm@v0")
    assert _tree(harness_repo.clone) == clone_before, "reads must not write the clone's tree"
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

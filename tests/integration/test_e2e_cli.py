"""End to end through agora.cli.main, design v4 grammar:
import -> search -> show -> continue (native) -> merge (summary) -> continue
-> edit -> delete, with real Drive (agora-test/) and real claude.

Continue spawns interactive claude, so AGORA_CLAUDE_CMD points at a sh
wrapper (E1) around tests/fakes/claude_noninteractive.py, which re-runs the
real CLI with -p and a fixed self-made question. Prompts are self-made
filler (CSV table steps); at most 3 real `claude -p` calls. Everything
created (Drive sessions/<ULID>, own jsonl uuids) is deleted one by one at
teardown; ids are re-collected from the wrapper log and our own printed ids
(E3) so a mid-test failure still cleans up.

Config pattern follows tests/integration/test_store_drive.py: AGORA_CONFIG is
a temp dir whose rclone.conf symlinks to ~/.config/agora/rclone.conf.

Run: uv run pytest -q -m integration tests/integration/test_e2e_cli.py
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import uuid
import warnings

import pytest

from agora import cli, header as h, store
from agora.agents import claude as C

pytestmark = pytest.mark.integration

# pwd, not HOME: the sandbox replaces HOME per test (E7).
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
REAL_CONF = REAL_HOME / ".config" / "agora" / "rclone.conf"
WRAPPER_SRC = Path(__file__).parent.parent / "fakes" / "claude_noninteractive.py"
DISALLOW = "Bash Read Glob Grep Edit Write WebFetch WebSearch Task"

P1 = "請用繁體中文列出「把 CSV 轉成 Markdown 表格」的三個步驟。只要文字回答，不要呼叫任何工具、不要寫檔案。"


@pytest.fixture()
def e2e(tmp_path, monkeypatch, capsys):
    if not REAL_CONF.exists():
        pytest.fail("需要 ~/.config/agora/rclone.conf（整合測試不能 skip）")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found")
    config = tmp_path / "config"
    config.mkdir()
    (config / "rclone.conf").symlink_to(REAL_CONF)
    monkeypatch.setenv("AGORA_CONFIG", str(config))
    monkeypatch.setenv("AGORA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("AGORA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGORA_FOLDER_NAME", "agora-test")
    monkeypatch.delenv("AGORA_RCLONE", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # This file is about pull/push and the CLI, not about the background uploader:
    # in the foreground the upload is done when the command returns, which is what
    # these assertions have always meant. The detached uploader has its own tests
    # (tests/integration/test_background_writes.py, tests/unit/test_background.py).
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(REAL_HOME))
    wrapper = tmp_path / "claude-noninteractive"  # E1: no shebang roulette
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {WRAPPER_SRC} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGORA_CLAUDE_CMD", str(wrapper))
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(fake_home))

    proj = Path("/tmp/agora-it-e2e/proj")
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=proj, check=True)
    proj = proj.resolve()
    paths = store.Paths.from_env()
    created = {"ulids": [], "uuids": [], "printed": []}  # E3/D-1: recorded at creation
    yield {"proj": proj, "paths": paths, "created": created,
           "fake_home": fake_home, "capsys": capsys}
    uuids = set(created["uuids"])
    log = fake_home / "e2e-args.log"  # E3: launches logged before agent start
    if log.exists():
        for line in log.read_text().splitlines():
            argv = json.loads(line)
            for flag in ("--resume", "--session-id"):
                if flag in argv:
                    uuids.add(argv[argv.index(flag) + 1])
    # D-1: ULIDs come ONLY from this test's own printed ids (run_main records
    # them). Never collect from the shared cache index: agora-test/ also holds
    # other people's sessions and purging them would delete their data.
    ulids = {u.split(":", 1)[1] for u in created["printed"]}
    ulids.update(u for u in created["ulids"])
    drive = store.Drive(paths)
    for ulid in sorted(ulids):
        proc = subprocess.run(
            ["rclone", "--config", str(REAL_CONF),
             "--drive-root-folder-id", drive.folder_id(),
             "purge", f"gdrive:sessions/{ulid}"], capture_output=True)
        if proc.returncode != 0:  # E5: cleanup failures must be visible
            warnings.warn(f"purge sessions/{ulid} failed: {proc.stderr.decode()[-200:]}")
    basedir = C.projects_dir() / C.encode_project_dir(proj)
    cfgdir = C.config_dir()
    for sid in sorted(uuids):  # own jsonl uuids, one by one (E4: plus side files)
        jsonl = basedir / f"{sid}.jsonl"
        if jsonl.is_file():
            jsonl.unlink()
        sidecar = basedir / sid
        if sidecar.is_dir():
            shutil.rmtree(sidecar)
        for extra in [cfgdir / "session-env" / sid, cfgdir / "file-history" / sid]:
            if extra.is_dir():
                shutil.rmtree(extra)
        # D-2: no glob over the shared todos/ dir (it lists real filenames).
        # Our tool-banned sessions never create todos; per-uuid session-env
        # and file-history above are exact paths.
    try:
        basedir.rmdir()
    except OSError:
        pass
    for leftover in [basedir / "memory"]:
        try:
            leftover.rmdir()  # claude leaves an empty memory/ dir; drop it too
        except OSError:
            pass
    try:
        basedir.rmdir()
    except OSError:
        pass
    shutil.rmtree(proj, ignore_errors=True)  # E5


def drive_session_names(e2e) -> set[str]:
    """The session folder names currently on Drive (names only, no contents)."""
    drive = store.Drive(e2e["paths"])
    out = drive._run("lsjson", "gdrive:sessions", "--dirs-only")
    return {e["Name"] for e in json.loads(out or "[]")}


def run_main(e2e, *argv: str) -> str:
    rc = cli.main(list(argv))
    out = e2e["capsys"].readouterr().out
    if argv and argv[0] in ("import", "continue", "merge", "edit"):
        # D-1: record our own new id the moment it is printed, even if the
        # command later fails (e.g. exit 3). Never scan search/show output:
        # those list other people's sessions too.
        for tok in out.split():
            if tok.startswith("agora:") and len(tok) == len("agora:") + 26:
                if tok not in e2e["created"]["printed"]:
                    e2e["created"]["printed"].append(tok)
                break
    assert rc == 0, out[-500:]
    return out


def read_header(e2e, agora_id: str) -> dict:
    # v4 has no sync action: any search syncs (throttled) for us.
    run_main(e2e, "search", "session", "--filter", "text~=e2e")
    hdr = store.Index(e2e["paths"]).header(agora_id.split(":", 1)[1])
    assert hdr is not None
    return hdr


def drive_md5(e2e, agora_id: str) -> str:
    """The Drive md5 of one session's session.md (content check, not a listing)."""
    drive = store.Drive(e2e["paths"])
    files = drive.list_one(agora_id.split(":", 1)[1])
    return files.get("session.md", "")


def test_import_search_show_continue_merge_continue_edit_delete(e2e):
    proj = e2e["proj"]
    created = e2e["created"]
    env = {**os.environ, "HOME": str(REAL_HOME),
           "AGORA_CLAUDE_HOME": str(REAL_HOME)}

    def seed() -> str:
        """One self-made short dialogue through the real CLI."""
        sid = str(uuid.uuid4())
        created["uuids"].append(sid)
        proc = subprocess.run(
            ["claude", "--disallowedTools", DISALLOW, "--model", "haiku",  # E6
             "-p", "--session-id", sid, P1],
            cwd=proj, env=env, capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, proc.stderr[-500:]
        return sid

    # 1. import the first self-made dialogue.
    uuid1 = seed()
    out = run_main(e2e, "import", "session", "--external-session-id", uuid1,
                   "--agent", "claude", "--header", "title=e2e 表格")
    id1 = out.split()[0]
    assert id1.startswith("agora:")
    created["ulids"].append(id1.split(":", 1)[1])

    # 2. search finds it, agora id first.
    out = run_main(e2e, "search", "session", "--filter", "text~=表格")
    assert any(line.split()[0] == id1 for line in out.splitlines() if line.split())

    # 2b. show and show --raw through the CLI (E2).
    out = run_main(e2e, "show", "session", id1)
    assert f"id: {id1}" in out and "e2e 表格" in out
    out = run_main(e2e, "show", "session", id1, "--raw")
    assert '"format": "claude-jsonl/1"' in out

    # 3. native continue writes back into the SAME agora session (design 5.4).
    before_md5 = drive_md5(e2e, id1)
    out = run_main(e2e, "continue", "session", id1, "--agent", "claude",
                   "--dir", str(proj))
    assert out.split()[0] == id1, "continue must keep the same agora id"
    hdr1 = read_header(e2e, id1)
    source = h.agora_of(hdr1)["source"]
    uuid2 = source["session_id"]
    assert uuid2 != uuid1
    uuid.UUID(uuid2)
    created["uuids"].append(uuid2)          # the agent-side session this created
    assert h.agora_of(hdr1)["previous_sources"] == [f"claude:{uuid1}"]
    assert h.agora_of(hdr1)["relation"] == "import"      # not a merge: unchanged
    assert hdr1["title"] == "e2e 表格"                    # user fields untouched
    assert drive_md5(e2e, id1) != before_md5, "session.md on Drive must be rewritten"

    # 4. a second session, then merge (claude writes the summary headless, v6).
    uuid_b = seed()
    out = run_main(e2e, "import", "session", "--external-session-id", uuid_b,
                   "--agent", "claude", "--header", "title=e2e 第二份")
    id2 = out.split()[0]
    created["ulids"].append(id2.split(":", 1)[1])
    out = run_main(e2e, "merge", "session", f"{id1},", id2, "--agent", "claude")
    idm = out.split()[0]
    assert idm.startswith("agora:")
    created["ulids"].append(idm.split(":", 1)[1])
    shown = run_main(e2e, "show", "session", idm)
    assert "## 要約" in shown and f"- {id1}「" in shown and f"- {id2}「" in shown
    assert read_header(e2e, idm)["status"] == "draft"

    # 5. continuing the merge turns it into that conversation, same id.
    out = run_main(e2e, "continue", "session", idm, "--agent", "claude",
                   "--dir", str(proj))
    assert out.split()[0] == idm
    hdr_m = read_header(e2e, idm)
    assert h.agora_of(hdr_m)["relation"] == "continue"
    assert "merge" not in h.agora_of(hdr_m)
    assert "status" not in hdr_m
    assert [p["id"] for p in h.agora_of(hdr_m)["parents"]] == [id1, id2]
    uuid_m = h.agora_of(hdr_m)["source"]["session_id"]
    assert uuid_m not in (uuid1, uuid2, uuid_b)
    created["uuids"].append(uuid_m)
    # a merge had no source of its own, so nothing lands in previous_sources
    assert not h.agora_of(hdr_m).get("previous_sources")
    loaded = run_main(e2e, "show", "session", idm, "--raw")
    assert cli.MERGE_NOTE.split("{by}")[0] in loaded and f"- {id1}「" in loaded

    # 6. edit a header field; same id, user fields change.
    out = run_main(e2e, "edit", "session", id1, "--header", "title=接續後改名")
    assert out.split()[0] == id1
    assert read_header(e2e, id1)["title"] == "接續後改名"

    # 7. delete several at once: refused without --yes, then id1 (whose only
    #    child is idm) and idm go together, off Drive.
    rc = cli.main(["delete", "session", id1, idm])
    err = e2e["capsys"].readouterr().err
    assert rc == cli.EXIT_INPUT and "--yes" in err
    out = run_main(e2e, "delete", "session", f"{id1},", idm, "--yes")
    assert set(out.split()) >= {id1, idm}
    left = drive_session_names(e2e)
    assert id1.split(":", 1)[1] not in left and idm.split(":", 1)[1] not in left
    for gone in (id1, idm):                  # teardown must not re-purge them
        created["ulids"].remove(gone.split(":", 1)[1])
        created["printed"].remove(gone)

    # Every continue is a --resume now, the merge one included
    # (--version probes also land in the log; only launches count).
    launches = [json.loads(line)
                for line in (e2e["fake_home"] / "e2e-args.log").read_text().splitlines()
                if "--resume" in line or "--session-id" in line]
    assert len(launches) == 2
    assert all("--resume" in argv and "--session-id" not in argv for argv in launches)

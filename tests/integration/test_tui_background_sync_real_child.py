"""#491 PR-3, C-23: the TUI with the real sync child, against the fake rclone held at its listing.

Not marked `integration`: nothing here touches Drive or a real agent. The remote is the fake
rclone's folder, the sync child is started the way the TUI starts it, and the listing is held
by `FAKE_RCLONE_HOLD` until the test lets it go - so "the list is usable while the sync runs"
and "a write waits for it" are checked while the child really is running.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import asyncio
import sys
from pathlib import Path

import pytest
from textual.widgets import Static

from agora import header as h
from agora import store, tui
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit"))
from test_tui import FakeCli, _hdr, _index, _spawn  # noqa: E402

FAKE_RCLONE = Path(__file__).resolve().parent.parent / "fakes" / "fake_rclone.py"
LOCAL_ONLY = [f"01{'L' * 23}{n}" for n in "AB"]           # the two sessions this machine already has
REMOTE_ONLY = [f"01{'R' * 22}{n:02d}" for n in range(10)]  # ten more on Drive


@pytest.fixture
def remote(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    wrapper = tmp_path / "rclone"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_RCLONE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_REMOTE", str(remote))
    monkeypatch.setenv("AGORA_RCLONE", str(wrapper))
    monkeypatch.setenv("AGORA_UPLOAD", "inline")
    return remote


def _hdr_for(ulid: str, title: str) -> dict:
    hdr = _hdr(ulid, title, sid=f"ses_{ulid[-2:]}")
    hdr["agora"]["updated_at"] = "2026-10-02T00:00:00Z"
    return hdr


def _publish_remote(tmp_path, ulids_and_titles) -> None:
    """Another machine's sessions on the remote (its own state and cache, the same remote)."""
    paths = store.Paths.from_env()
    other = store.Paths(config=paths.config, cache=tmp_path / "m2" / "cache", state=tmp_path / "m2" / "state")
    for ulid, title in ulids_and_titles:
        store.stage(other, _hdr_for(ulid, title), "## user\n把 CSV 轉成 Markdown 表格\n", b'{"x": 1}')
    store.upload_batch(store.Drive(other), other)


def test_c23_the_list_works_while_the_real_child_waits_on_the_listing(remote, tmp_path, monkeypatch):
    # Remote: ten new sessions and the two we have, with other titles (their content differs).
    _publish_remote(tmp_path, [(u, f"遠端 {u[-2:]}") for u in REMOTE_ONLY]
                    + [(u, f"遠端的 {u[-1]}") for u in LOCAL_ONLY])
    _index(*[(_hdr_for(u, f"本機 {u[-1]}"), "## user\n本機的內容\n") for u in LOCAL_ONLY])
    gate = tmp_path / "gate"
    monkeypatch.setenv("FAKE_RCLONE_HOLD", str(gate))

    paths = store.Paths.from_env()
    app = tui.AgoraApp(paths, FakeCli(), agents=[], check_setup=False, sync_on_start=True)
    app.spawn, app._last_spawned = _spawn()     # no write action may reach a real process

    async def wait_for(pred, pilot, tries=400):
        for _ in range(tries):
            if pred():
                return True
            await pilot.pause()
        return pred()

    async def go():
        try:
            async with app.run_test(size=(120, 30)) as pilot:
                waiting = Path(f"{gate}.waiting")
                for _ in range(1000):           # the child reports it is at the listing: a file, not a time
                    if waiting.exists():
                        break
                    await asyncio.sleep(0.02)
                assert waiting.exists(), "the sync child never reached the listing"
                assert app.sync.running and app.query_one("#table").row_count == 2

                await pilot.press("down")       # the list is usable while the child waits
                assert app.query_one("#table").cursor_row == 1
                assert "同步 Drive 中" in str(app.query_one("#bar", Static).render())

                await pilot.press("d")          # a write waits for the sync ...
                assert await wait_for(lambda: isinstance(app.screen, tui.WaitSync), pilot)
                await pilot.press("escape")     # ... and Esc starts nothing
                assert await wait_for(lambda: not isinstance(app.screen, tui.WaitSync), pilot)
                assert app._last_spawned == [] and app.sync.running

                gate.touch()                    # let the listing go on
                await asyncio.wait_for(app.sync_settled.wait(), 60)
                await pilot.pause()
                assert app.query_one("#table").row_count == 12
                assert str(app.query_one("#msg", Static).render()) == "同步完成：新增 10、更新 2"
                assert "同步 Drive 中" not in str(app.query_one("#bar", Static).render())
                assert (paths.state / "last-sync").exists()
        finally:
            gate.touch()                        # the child must never be left at its gate
    asyncio.run(go())
    assert app._last_spawned == []

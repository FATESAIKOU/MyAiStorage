"""Fixtures shared by the aicomm tests (the other unit tests do not use them)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from _aicomm_fakes import FakeSidecar, angareion_calls, install_fake_angareion

SENDER = "demo-impl-t1-1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake `angareion` first on PATH plus a fake loopback sidecar; both are ours alone.

    The environment is cleaned first, so nothing from the shell (a real AICOMM_SIDECAR, a
    real identity, FAKE_ANG_* leftovers) leaks into a test.
    """
    for name in list(os.environ):
        if name.startswith(("FAKE_ANG_", "AICOMM_")):
            monkeypatch.delenv(name)
    bin_dir = tmp_path / "bin"
    install_fake_angareion(bin_dir)
    log = tmp_path / "angareion-calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ANG_LOG", str(log))
    monkeypatch.setenv("ANGAREION_IDENTITY", SENDER)
    sidecar = FakeSidecar()
    monkeypatch.setenv("AICOMM_SIDECAR", sidecar.url)
    try:
        yield SimpleNamespace(
            sidecar=sidecar,
            calls=lambda: angareion_calls(log),
            sender=SENDER,
            setenv=monkeypatch.setenv,
        )
    finally:
        sidecar.close()
    # The sidecar is only ever looked up or poked, whatever the test did.
    assert sidecar.foreign_requests() == []

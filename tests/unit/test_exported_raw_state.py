"""State rules for the file-backed `Exported` and the round child's SIGTERM exit (B1b-2 review).

Review 低1/低2: `raw` is a lazy property over `raw_file`; the private carriers must not
take part in equality, and new bytes must not leave an old `raw_md5` behind (the
write-back skips a version when the md5 matches the last one it wrote). Review 中3: a
round killed by SIGTERM has to leave through the interpreter's normal exit so the
export's temp directory is removed.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import hashlib
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from agora.agents.base import Exported


def _file_export(tmp_path: Path, data: bytes = b'{"x": 1}') -> Exported:
    path = tmp_path / "raw.json"
    path.write_bytes(data)
    return Exported(session_id="s", raw_file=path,
                    raw_md5=hashlib.md5(data).hexdigest(), raw_size=len(data))


def test_the_lazy_raw_reads_the_file_once_and_matches_it(tmp_path):
    e = _file_export(tmp_path, b'{"x": "\xe4\xb8\xad"}')
    assert e._raw is None
    assert e.raw == b'{"x": "\xe4\xb8\xad"}'
    assert e._raw is not None


def test_equality_ignores_whether_the_raw_was_read_back_yet(tmp_path):
    a = _file_export(tmp_path)
    b = Exported(session_id="s", raw_file=a.raw_file, raw_md5=a.raw_md5, raw_size=a.raw_size)
    assert a == b
    a.raw                                     # one of them reads its file back
    assert a == b


def test_setting_raw_clears_the_file_backed_facts(tmp_path):
    e = _file_export(tmp_path)
    old_md5 = e.raw_md5
    e.raw = b'{"x": 2}'
    assert e.raw == b'{"x": 2}'
    assert e.raw_file is None and e.raw_md5 is None and e.raw_size is None
    assert old_md5 != hashlib.md5(e.raw).hexdigest()


def test_the_round_child_turns_sigterm_into_a_normal_exit():
    """`_exit_on_sigterm` is what lets the export's temp directory be removed: in a real
    child, SIGTERM now exits 143 through the finalizers instead of dying at once."""
    code = textwrap.dedent("""
        import sys, tempfile, time
        from agora import round_child
        round_child._exit_on_sigterm()
        d = tempfile.TemporaryDirectory(prefix="agora-raw-")
        print(d.name, flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        name = proc.stdout.readline().strip()
        assert name and Path(name).is_dir()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
        deadline = time.monotonic() + 5
        while Path(name).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rc == 143
        assert not Path(name).exists(), "the temp directory outlived the SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

#!/usr/bin/env python3
"""Fake `agora` for AgoraSessionStore unit tests (task 3.0).

Implements just enough of `agora merge` and `agora continue --headless`; it
never touches Drive and never runs a real agent.

Every call appends its argv to $FAKE_HOME/agora-args.log and the *names* of
the interesting environment variables (never their values) to
$FAKE_HOME/agora-env-names.log, so a test can prove the secret went into the
environment and not the command line.

merge: prints $FAKE_AGORA_MERGE_ID (default agora:FAKEMERGED000000000000000),
       or fails when FAKE_AGORA_MERGE_FAIL is set.

continue: prints the fixed stderr lines - `agora-session <id>`,
       `agora-agent-session <id>`, and with --headless-port also
       `agora-port <port>` (before listening, like the real one) - then
       listens on that port and, on SIGTERM, writes $FAKE_HOME/agora-stopped
       and exits 0 (the gentle stop's "last write-back").

Knobs: FAKE_AGORA_SESSION, FAKE_AGORA_AGENT_SESSION, FAKE_AGORA_NO_AGENT_LINE,
FAKE_AGORA_NO_PORT_LINE, FAKE_AGORA_EXIT_EARLY, FAKE_AGORA_REFUSE_TERM.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-agora"))
home.mkdir(parents=True, exist_ok=True)
with open(home / "agora-args.log", "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
names = sorted(
    key for key in os.environ
    if key.startswith(("AGORA_", "FAKE_", "ANTHROPIC_", "OPENAI_", "SIDECAR_"))
)
with open(home / "agora-env-names.log", "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "env": names}) + "\n")

if argv[:1] == ["merge"]:
    if os.environ.get("FAKE_AGORA_MERGE_FAIL"):
        print("fake merge failure", file=sys.stderr)
        sys.exit(1)
    print(os.environ.get("FAKE_AGORA_MERGE_ID", "agora:FAKEMERGED000000000000000"))
    sys.exit(0)

if not (argv[:1] == ["continue"] and argv[1:2] == ["session"]):
    print(f"fake agora does not support {argv}", file=sys.stderr)
    sys.exit(2)

session_id = os.environ.get("FAKE_AGORA_SESSION", argv[2])
print(f"agora-session {session_id}", file=sys.stderr, flush=True)
if not os.environ.get("FAKE_AGORA_NO_AGENT_LINE"):
    agent_session = os.environ.get("FAKE_AGORA_AGENT_SESSION", "ses_fake000000000000")
    print(f"agora-agent-session {agent_session}", file=sys.stderr, flush=True)
port = None
if "--headless-port" in argv:
    port = int(argv[argv.index("--headless-port") + 1])
    if not os.environ.get("FAKE_AGORA_NO_PORT_LINE"):
        # The real one prints before the server listens (b8ab0f0).
        print(f"agora-port {port}", file=sys.stderr, flush=True)

if os.environ.get("FAKE_AGORA_EXIT_EARLY"):
    sys.exit(3)


def _gentle_stop(signum, frame):
    (home / "agora-stopped").write_text("stopped\n", encoding="utf-8")
    sys.exit(0)


if os.environ.get("FAKE_AGORA_REFUSE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    signal.signal(signal.SIGTERM, _gentle_stop)

server = None
if port is not None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    server.settimeout(0.2)
while True:
    if server is not None:
        try:
            conn, _ = server.accept()
            conn.close()
            continue
        except socket.timeout:
            continue
    time.sleep(0.2)

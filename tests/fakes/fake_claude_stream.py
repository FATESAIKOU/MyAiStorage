#!/usr/bin/env python3
"""Fake `claude` speaking stream-json (sidecar driver tests, task 3.1).

Reads user messages / interrupt control requests on stdin and answers with
scripted events on stdout, so the ClaudeCodeDriver can be tested without the
real claude.  Nothing here touches a network or a real session.

Every call appends its argv to $FAKE_HOME/claude-args.log, the names of the
interesting environment variables (never values) to
$FAKE_HOME/claude-env-names.log, and every stdin line to
$FAKE_HOME/claude-stdin.log.

FAKE_CLAUDE_SCENARIO picks the scripted events:
  ok / hold / crash /
  quota_rejected_five / quota_rejected_seven / quota_unknown_type /
  quota_unknown_status / quota_allowed_warning / quota_credits /
  quota_api_retry / quota_hit_your / api_retry_other
FAKE_CLAUDE_SESSION overrides the session id (otherwise --resume's value).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
home = Path(os.environ.get("FAKE_HOME", "/tmp/fake-claude-stream"))
home.mkdir(parents=True, exist_ok=True)
with open(home / "claude-args.log", "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
names = sorted(
    key for key in os.environ
    if key.startswith(("ANTHROPIC_", "CLAUDE_", "OPENAI_", "FAKE_", "AGORA_"))
)
with open(home / "claude-env-names.log", "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "env": names}) + "\n")

session_id = os.environ.get("FAKE_CLAUDE_SESSION")
if "--resume" in argv:
    session_id = session_id or argv[argv.index("--resume") + 1]
session_id = session_id or "11111111-2222-4333-8444-666666666666"

scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
if os.environ.get("FAKE_CLAUDE_STDERR_ECHO"):
    # Print the named env var on stderr at startup (driver tail tests, 3.6.3).
    value = os.environ.get(os.environ["FAKE_CLAUDE_STDERR_ECHO"], "")
    print(f"fake claude: {os.environ['FAKE_CLAUDE_STDERR_ECHO']}={value}",
          file=sys.stderr, flush=True)
if scenario == "die":
    sys.exit(2)
RESETS = 4102444800  # 2100-01-01T00:00:00Z


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def answer() -> None:
    emit({"type": "system", "subtype": "init", "session_id": session_id})
    if scenario == "crash":
        sys.exit(2)
    if scenario == "hold":
        return  # no result: the round is still running
    if scenario == "ok":
        emit({"type": "assistant",
              "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}})
        emit({"type": "result", "subtype": "success", "result": "done"})
    elif scenario == "quota_rejected_five":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour",
                                  "resetsAt": RESETS}})
        emit({"type": "result", "subtype": "error", "result": "quota"})
    elif scenario == "quota_rejected_seven":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day",
                                  "resetsAt": RESETS}})
    elif scenario == "quota_unknown_type":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "rejected", "rateLimitType": "overage",
                                  "resetsAt": RESETS}})
    elif scenario == "quota_unknown_status":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "weird_value", "rateLimitType": "five_hour"}})
        emit({"type": "result", "subtype": "success", "result": "done"})
    elif scenario == "quota_allowed_warning":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "allowed_warning",
                                  "rateLimitType": "five_hour"}})
        emit({"type": "result", "subtype": "success", "result": "done"})
    elif scenario == "quota_credits":
        emit({"type": "rate_limit_event",
              "rate_limit_info": {"status": "allowed", "errorCode": "credits_required",
                                  "rateLimitType": "seven_day", "resetsAt": RESETS}})
    elif scenario == "quota_api_retry":
        emit({"type": "system", "subtype": "api_retry", "error": "rate_limit"})
    elif scenario == "api_retry_other":
        emit({"type": "system", "subtype": "api_retry", "error": "connection"})
        emit({"type": "result", "subtype": "success", "result": "done"})
    elif scenario == "quota_hit_your":
        emit({"type": "result", "subtype": "success",
              "result": "You've hit your limit · resets at 3pm"})


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    with open(home / "claude-stdin.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if message.get("type") == "control_request":
        emit({"type": "control_response",
              "response": {"request_id": message.get("request_id")}})
        emit({"type": "result", "subtype": "error_during_execution",
              "result": "interrupted"})
    elif message.get("type") == "user":
        answer()

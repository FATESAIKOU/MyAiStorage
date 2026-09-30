#!/usr/bin/env python3
"""spike/scripts/test_pilot_2_6.py

快速試跑：測試 10 個 Session 生成與 Config A / B 的單次 push 耗時。
"""

import hashlib
import json
import os
from pathlib import Path
import random
import string
import subprocess
import time


def generate_random_chunk(min_kb=0.5, max_kb=8.0):
    size = int(random.uniform(min_kb, max_kb) * 1024)
    code_snippets = [
        "def process_data(records):\n    for r in records:\n        yield transform(r)\n",
        "SELECT id, session_id, created_at, status FROM sessions WHERE in_progress = 1 ORDER BY id DESC LIMIT 50;\n",
        "class WorkerPool:\n    def __init__(self, size=10):\n        self.size = size\n",
        "import hashlib\nh = hashlib.sha256(data).hexdigest()\nassert len(h) == 64\n",
    ]
    parts = []
    current = 0
    while current < size:
        if random.random() < 0.4:
            s = random.choice(code_snippets)
        else:
            words = [
                "".join(
                    random.choices(
                        string.ascii_letters, k=random.randint(3, 10)
                    )
                )
                for _ in range(20)
            ]
            s = " ".join(words) + "\n"
        parts.append(s)
        current += len(s.encode("utf-8"))
    return "".join(parts)


def init_session(session_id: str) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "info": {
            "id": session_id,
            "title": f"Test Session {session_id}",
            "created_at": now_ms,
            "updated_at": now_ms,
        },
        "messages": [],
    }


def advance_session(session: dict, sync_idx: int) -> None:
    num_msgs = random.randint(1, 3)
    now_ms = int(time.time() * 1000)
    session["info"]["updated_at"] = now_ms
    start_mid = len(session["messages"])
    for i in range(num_msgs):
        role = "user" if (start_mid + i) % 2 == 0 else "assistant"
        chunk = generate_random_chunk()
        msg = {
            "id": f"msg_{session['info']['id']}_{start_mid + i:04d}",
            "role": role,
            "time": {"created": now_ms},
            "parts": [{"type": "text", "text": chunk}],
        }
        if role == "assistant" and random.random() < 0.5:
            tool_output = (
                "Running compiler...\nBuild succeeded: 0 errors, 0 warnings.\n"
                * 3
            )
            msg["parts"].append({
                "type": "tool",
                "call": {"name": "compile", "input": "make -j4"},
                "state": {"status": "success", "output": tool_output},
            })
        session["messages"].append(msg)


if __name__ == "__main__":
    print("Testing synthetic session generation...")
    s = init_session("ses_001")
    for step in range(50):
        advance_session(s, step)
    data = json.dumps(s, indent=2).encode("utf-8")
    print(
        f"Session with 50 syncs: {len(s['messages'])} messages, size: {len(data) / 1024:.1f} KB"
    )

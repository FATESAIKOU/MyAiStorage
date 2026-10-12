"""The offline fake model provider (change agora-opencode-v2, task 1.1).

It stands in for the model opencode would otherwise call, so the v2 fixture and the
acceptance runs need no free endpoint and no credentials. What matters: the config
it writes is the one opencode 2.x reads, a tool call comes out when the model is
supposed to call a tool, a sentence comes out once opencode sends the tool's result
back, both the streaming and the plain shape answer, and every request is recorded.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fakes"))
import fake_upstream  # noqa: E402  (needs the path above)


def _post(url: str, payload: dict) -> str:
    request = urllib.request.Request(f"{url}/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def test_the_config_is_the_one_opencode_2x_reads():
    config = fake_upstream.opencode_config("http://127.0.0.1:1234/v1")
    provider = config["providers"][fake_upstream.PROVIDER]
    assert provider["package"] == "aisdk:@ai-sdk/openai-compatible"
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:1234/v1"
    assert fake_upstream.MODEL in provider["models"]


def test_a_tool_call_comes_out_and_a_sentence_after_its_result(tmp_path):
    log = tmp_path / "upstream-calls.log"
    upstream = fake_upstream.start(text="ZZHELLO 這是一句話。",
                                   tool_args='{"path": "fake-note.txt"}', log=log)
    try:
        first = json.loads(_post(upstream.url, {
            "model": fake_upstream.MODEL,
            "messages": [{"role": "user", "content": "讀 fake-note.txt"}]}))
        call = first["choices"][0]["message"]["tool_calls"][0]
        assert call["function"]["name"] == "read"
        assert json.loads(call["function"]["arguments"]) == {"path": "fake-note.txt"}

        # opencode runs the tool and sends the result back: now it answers
        second = json.loads(_post(upstream.url, {
            "model": fake_upstream.MODEL,
            "messages": [{"role": "user", "content": "讀 fake-note.txt"},
                         {"role": "assistant", "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": call["id"],
                          "content": "fake-note.txt: ZZNOTE"}]}))
        assert second["choices"][0]["message"]["content"] == "ZZHELLO 這是一句話。"
    finally:
        upstream.stop()

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["request"]["messages"][0]["role"] for call in calls] == ["user", "user"]
    assert calls[0]["path"] == "/chat/completions"


def test_the_streaming_shape_carries_the_same_answer(tmp_path):
    upstream = fake_upstream.start(mode="text", text="ZZSTREAM",
                                   log=tmp_path / "calls.log")
    try:
        body = _post(upstream.url, {"model": fake_upstream.MODEL, "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]})
    finally:
        upstream.stop()
    assert "ZZSTREAM" in body
    assert "data: [DONE]" in body
    chunks = [json.loads(line[len("data: "):]) for line in body.splitlines()
              if line.startswith("data: ") and not line.endswith("[DONE]")]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_a_text_only_mode_never_calls_a_tool(tmp_path):
    upstream = fake_upstream.start(mode="text", log=tmp_path / "calls.log")
    try:
        answer = json.loads(_post(upstream.url, {"model": fake_upstream.MODEL,
                                                 "messages": []}))
    finally:
        upstream.stop()
    assert answer["choices"][0]["message"]["content"] == fake_upstream.DEFAULT_TEXT
    assert "tool_calls" not in answer["choices"][0]["message"]

"""`conftest.py` 裡容器匯出輔助的單元測試（不需要容器與憑證）。

9.1 e2e 已經被「AI 確實有把交接單 id 傳進去，測試卻讀不到」卡過兩次；改寫成
checkout 版之後不再需要解析模型送來的參數形狀，但**匯出讀不到時要怎麼回報**這件事
還沒變：對話被免費模型回覆塞到很大時，`opencode export` 會吐出被截斷的 JSON，
而呼叫端（`assert_tool_called`）與開頭比對（`wire_prefix`）都靠匯出，於是整場
看起來像「AiStorage 的工具被呼叫了但都失敗」。這裡把形狀釘住，改壞會立刻在這裡
看到，不用等 10 分鐘的 e2e。
"""

from __future__ import annotations

import pytest

from .conftest import (
    ExportFailed,
    ResidentContainerHandle,
    assert_tool_called,
    wire_prefix,
)


class _FakeExec:
    """假的 `docker exec`：回一段被截斷的 opencode export 輸出。"""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _handle_with_export_output(stdout: str):
    h = ResidentContainerHandle(
        name="e2e-x", port=4096, work_dir=None, secrets_root=None,
        profile="mac-opencode-test", model="opencode/space-bunny-free", log_path=None,
    )
    h.exec_in = lambda *a, **k: _FakeExec(stdout)  # type: ignore[assignment]
    return h


@pytest.mark.e2e
def test_truncated_export_is_reported_as_an_export_failure():
    """截斷的 opencode export → 明確說「匯出失敗」，不要像系統壞掉。"""
    truncated = '{"messages": [{"info": {"id": "m1"}, "parts": [{"text": "很長的回覆'
    with pytest.raises(ExportFailed) as e:
        _handle_with_export_output(truncated).export("ses_1")
    msg = str(e.value)
    assert "匯出" in msg
    assert "不是 AiStorage 的錯誤" in msg
    assert "ses_1" in msg
    assert "Traceback" not in msg


@pytest.mark.e2e
def test_export_shape_mismatch_is_reported_as_an_export_failure():
    with pytest.raises(ExportFailed) as e:
        _handle_with_export_output('{"not_messages": []}').export("ses_1")
    assert "匯出" in str(e.value) and "messages" in str(e.value)


@pytest.mark.e2e
def test_assert_tool_called_says_it_could_not_verify_when_the_export_fails():
    """匯出失敗時要說「無法驗證」，不是「模型沒照做」也不是「工具失敗」。"""
    truncated = '{"messages": [{"info": {"id": "m1"}, "parts": [{"text": "很長'
    with pytest.raises(AssertionError) as e:
        assert_tool_called(
            _handle_with_export_output(truncated), "ses_1", "agora_handoff"
        )
    assert "無法驗證" in str(e.value)


def _export(messages: list[dict]) -> dict:
    return {"info": {"id": "ses_1"}, "messages": messages}


@pytest.mark.e2e
def test_wire_prefix_truncates_inclusive_to_the_given_message():
    """接續點那一則要**含在內**（新 session 從它之後開始寫）。"""
    export = _export([
        {"info": {"id": "m1", "role": "user"}, "parts": [{"type": "text", "text": "一"}]},
        {"info": {"id": "m2", "role": "assistant"}, "parts": [{"type": "text", "text": "二"}]},
        {"info": {"id": "m3", "role": "user"}, "parts": [{"type": "text", "text": "三"}]},
    ])
    assert wire_prefix(export) == wire_prefix(export, "m3")
    assert wire_prefix(export, "m2") != wire_prefix(export, "m3")
    with pytest.raises(AssertionError) as e:
        wire_prefix(export, "m9")
    assert "m9" in str(e.value)


@pytest.mark.e2e
def test_wire_prefix_ignores_reidentified_ids_but_keeps_the_tool_call_id():
    """重編 session／message／part 的 id 不影響開頭；`callID` 影響。

    這正是 ADR 0010 說的位元組相同：轉接器只重編 id 與 parent，其餘一個位元組都
    不動，所以重編前後算出來的開頭必須相同（`callID` 也一樣）。
    """
    parts = [{"type": "tool", "tool": "agora_handoff", "callID": "call_function_abc",
              "state": {"status": "completed", "input": {"tasks": ["x"]}, "output": "{}"}}]
    before = _export([{
        "info": {"id": "msg_old", "role": "assistant"}, "parts": parts,
    }])
    after = _export([{
        "info": {"id": "msg_new", "role": "assistant"},
        "parts": [{**parts[0], "id": "prt_new", "messageID": "msg_new"}],
    }])
    assert wire_prefix(before) == wire_prefix(after)

    other_call_id = _export([{
        "info": {"id": "msg_new", "role": "assistant"},
        "parts": [{**parts[0], "callID": "call_function_other"}],
    }])
    assert wire_prefix(before) != wire_prefix(other_call_id), (
        "callID 換掉就會影響送給模型的開頭（比對必須看得到這個差別）"
    )

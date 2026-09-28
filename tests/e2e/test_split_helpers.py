"""`test_split.py` 裡的解析輔助函式的單元測試（不需要容器與憑證）。

9.1 e2e 已經被「AI 確實有把交接單 id 傳進去，測試卻讀不到」卡過兩次：免費模型
送出的參數形狀一次一個樣（`{"handoff_ids": ["x"]}`／`{"handoff_id": "x"}`／
`{"handoff_ids": {"item": "x"}}`／整包 `{"properties": {...}}`），而且 claim 的
輸出同時帶著認領單自己的 item id。這裡把形狀都釘住，改壞會立刻在這裡看到，
不用等 10 分鐘的 e2e。
"""

from __future__ import annotations

import pytest

from .test_split import _claimed_ids_from_parts, _ids_from_value

NAMES = ("handoff_ids", "handoff_id", "handoffs", "handoff")


@pytest.mark.e2e
@pytest.mark.parametrize("payload", [
    {"handoff_ids": ["handoff:A"]},
    {"handoff_id": "handoff:A"},
    {"handoff_ids": {"item": "handoff:A"}},
    {"handoffs": {"item": ["handoff:A"]}},
    {"properties": {"handoff_ids": {"item": "handoff:A"}}},
    {"properties": {"handoff_id": "handoff:A"}},
    {"handoff_ids": '["handoff:A"]'},
])
def test_claimed_ids_absorbs_the_shapes_the_model_actually_sends(payload: dict):
    assert _ids_from_value(payload, NAMES) == {"handoff:A"}


@pytest.mark.e2e
def test_claimed_ids_ignores_unrelated_keys():
    """`reference` 之類的參數包在同一個 envelope 裡時，不該把無關的字串當成 id。"""
    assert _ids_from_value(
        {"properties": {"to": "opencode:ses_1", "read_snapshot_at": "2026-01-01T00:00:00Z"}},
        NAMES,
    ) == set()


@pytest.mark.e2e
def test_claimed_ids_prefers_cli_output_and_ignores_claim_item_ids():
    part = {
        "input": {"properties": {"handoff_ids": {"item": "handoff:A"}}},
        "output": (
            '{"claim_ids": ["claim:01ABC"], '
            '"handoffs": [{"handoff_id": "handoff:A"}]}'
        ),
    }
    assert _claimed_ids_from_parts([part]) == {"handoff:A"}


@pytest.mark.e2e
def test_claimed_ids_falls_back_to_the_model_input_when_output_is_unusable():
    part = {"input": {"handoff_ids": ["handoff:B"]}, "output": "not json"}
    assert _claimed_ids_from_parts([part]) == {"handoff:B"}


class _FakeExec:
    """假的 `docker exec`：回一段被截斷的 opencode export 輸出。"""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _handle_with_export_output(stdout: str):
    from .conftest import ResidentContainerHandle

    h = ResidentContainerHandle(
        name="e2e-x", port=4096, work_dir=None, secrets_root=None,
        profile="mac-opencode-test", model="opencode/space-bunny-free", log_path=None,
    )
    h.exec_in = lambda *a, **k: _FakeExec(stdout)  # type: ignore[assignment]
    return h


@pytest.mark.e2e
def test_truncated_export_is_reported_as_an_export_failure():
    """截斷的 opencode export → 明確說「匯出失敗」，不要像系統壞掉。

    9.1 e2e 實測：對話被免費模型回覆塞到很大時，`opencode export` 會吐出被
    截斷的 JSON。原本這裡丟 `RuntimeError`，而 `assert_tool_called` 正好也靠匯出
    讀工具呼叫，於是整場看起來像「AiStorage 的工具被呼叫了但都失敗」。
    """
    from .conftest import ExportFailed

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
    from .conftest import ExportFailed

    with pytest.raises(ExportFailed) as e:
        _handle_with_export_output('{"not_messages": []}').export("ses_1")
    assert "匯出" in str(e.value) and "messages" in str(e.value)


@pytest.mark.e2e
def test_assert_tool_called_says_it_could_not_verify_when_the_export_fails():
    """匯出失敗時要說「無法驗證」，不是「模型沒照做」也不是「工具失敗」。"""
    from .conftest import assert_tool_called
    from .conftest import ExportFailed  # noqa: F401 - 說明這個例外是預期路徑

    truncated = '{"messages": [{"info": {"id": "m1"}, "parts": [{"text": "很長'
    with pytest.raises(AssertionError) as e:
        assert_tool_called(
            _handle_with_export_output(truncated), "ses_1", "aistorage_split"
        )
    assert "無法驗證" in str(e.value)

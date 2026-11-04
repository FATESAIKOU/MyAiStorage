"""`pmo.adapters` 的真接線測試：aicomm 的旗面與逾時、sidecar 的請求與錯誤。

從 `tests/unit/test_pmo_ops.py` 原樣搬來（#357 第二輪審查 L-a：那個檔超過 1000
行）。斷言沒有改；共用的假資料（`BODY`、`POKE_TEXT`、`PMO_SIDECAR`）仍由原檔
提供，這裡只 import。全部用假的 `subprocess.run` 與假的 `urllib` 開瓶器，不打
真的 aicomm／sidecar（`tests/conftest.py` 的 `isolated_home`）。

`pmo.ops` 的 §3.6 純函式判斷（`is_own_job`／`is_auto_job`，不經後端）也一起搬
到這裡的第 3 段：搬完 adapter 兩段之後主檔仍超過 1000 行，這一段是與四個指令
無關、最獨立的一塊。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import subprocess
import urllib.error

import pytest

from pmo import ops
from pmo.adapters import Aicomm, Sidecar
from pmo.config import DEFAULT_WAKE_TEXT
from pmo.ports import (
    AgentRecord,
    AicommTimeout,
    JobCreatedUnreadable,
    JobRecord,
    PortError,
    SidecarError,
    SidecarShapeError,
)

from _pmo_env import PMO_IDENTITY, TEAM_CHANNEL, TEAM_PM
from test_pmo_ops import BODY, PMO_SIDECAR, POKE_TEXT

# --------------------------------------------------------------------------- #
# 1. 真接線：`adapters.Aicomm` 的旗面、逾時與失敗
# --------------------------------------------------------------------------- #


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def aicomm_runs(monkeypatch):
    """記下 `aicomm` 的 argv 與 stdin，回放指定的 stdout／結束碼。"""
    calls: list[dict] = []
    reply: dict = {"stdout": "01JMSG\n", "returncode": 0, "stderr": "", "timeout": False}

    def _fake_run(args, **kwargs):
        calls.append({"args": list(args), "input": kwargs.get("input")})
        if reply["timeout"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)
        return _Proc(stdout=reply["stdout"], returncode=reply["returncode"], stderr=reply["stderr"])

    monkeypatch.setattr("pmo.adapters.subprocess.run", _fake_run)
    return calls, reply


def test_aicomm_adapter_builds_the_send_command(aicomm_runs):
    calls, reply = aicomm_runs
    reply["stdout"] = "01JMSG\n已送出\n"
    message_id = Aicomm().send(
        channel=TEAM_CHANNEL, to=TEAM_PM, body=BODY, urgency=8
    )
    assert message_id == "01JMSG"  # 第一行就是訊息 id
    (call,) = calls
    assert call["args"] == [
        "aicomm",
        "send",
        f"--channel={TEAM_CHANNEL}",
        f"--to={TEAM_PM}",
        "--urgency=8",
        "--body=-",
    ]
    assert call["input"] == BODY


def test_aicomm_adapter_without_urgency_or_ticket(aicomm_runs):
    calls, _reply = aicomm_runs
    Aicomm().send(channel=TEAM_CHANNEL, to=TEAM_PM, body=BODY)
    (call,) = calls
    assert "--urgency" not in " ".join(call["args"])
    assert "--ticket" not in " ".join(call["args"])
    assert "--no-wake" not in call["args"]


def test_aicomm_adapter_timeout_is_its_own_error(aicomm_runs):
    _calls, reply = aicomm_runs
    reply["timeout"] = True
    with pytest.raises(AicommTimeout, match="逾時"):
        Aicomm().send(channel=TEAM_CHANNEL, to=TEAM_PM, body=BODY)


def test_aicomm_adapter_carries_the_exit_code(aicomm_runs):
    _calls, reply = aicomm_runs
    reply.update(returncode=2, stderr="backend down")
    with pytest.raises(PortError) as caught:
        Aicomm().send(channel=TEAM_CHANNEL, to=TEAM_PM, body=BODY)
    assert caught.value.returncode == 2
    assert "aicomm exit 2" in str(caught.value)


def test_aicomm_adapter_refuses_an_answer_without_a_message_id(aicomm_runs):
    _calls, reply = aicomm_runs
    reply["stdout"] = ""
    with pytest.raises(PortError, match="訊息 id"):
        Aicomm().send(channel=TEAM_CHANNEL, to=TEAM_PM, body=BODY)


# --------------------------------------------------------------------------- #
# 2. 真接線：`adapters.Sidecar` 的請求、回應與錯誤（D-12）
# --------------------------------------------------------------------------- #


class _Answer:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    """`urllib` 的開瓶器替身：記下請求，回放 `(status, body)` 或丟例外。"""

    def __init__(self, answer=None) -> None:
        self.requests: list[dict] = []
        self.answer = answer if answer is not None else (200, b"{}")

    def open(self, request, timeout=None):
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "data": request.data,
                "timeout": timeout,
            }
        )
        status, body = self.answer
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", None, io.BytesIO(body))
        return _Answer(status, body)


@pytest.fixture
def opener(monkeypatch):
    fake = FakeOpener()
    monkeypatch.setattr("pmo.adapters._OPENER", fake)
    return fake


def test_sidecar_adapter_reads_one_agent(opener):
    opener.answer = (
        200,
        json.dumps(
            {"id": TEAM_PM, "state": "running", "session": "ses-1", "workdir": "/tmp/w"}
        ).encode(),
    )
    record = Sidecar(PMO_SIDECAR).get_agent(TEAM_PM)
    assert record == AgentRecord(agent_id=TEAM_PM, state="running", session="ses-1")
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/agents/{TEAM_PM}"
    assert request["method"] == "GET"
    assert request["timeout"] == 10.0


def test_sidecar_adapter_turns_a_404_into_the_known_code(opener):
    opener.answer = (404, json.dumps({"code": "agent_not_found"}).encode())
    with pytest.raises(SidecarError) as caught:
        Sidecar(PMO_SIDECAR).get_agent(TEAM_PM)
    assert (caught.value.status, caught.value.code) == (404, "agent_not_found")


def test_sidecar_adapter_keeps_the_message_text_out_of_the_error(opener):
    """D-12：錯誤只印已知欄位，回應的說明文字不進訊息。"""
    opener.answer = (
        400,
        json.dumps({"code": "workdir_not_found", "message": "secret-looking detail"}).encode(),
    )
    with pytest.raises(SidecarError) as caught:
        Sidecar(PMO_SIDECAR).poke(TEAM_PM, text=POKE_TEXT)
    assert "workdir_not_found" in str(caught.value)
    assert "secret-looking detail" not in str(caught.value)


def test_sidecar_adapter_says_when_it_cannot_connect(monkeypatch):
    def _boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    class _Opener:
        open = staticmethod(_boom)

    monkeypatch.setattr("pmo.adapters._OPENER", _Opener())
    with pytest.raises(SidecarError, match="連不上"):
        Sidecar(PMO_SIDECAR).get_agent(TEAM_PM)


def test_sidecar_adapter_marks_an_answer_whose_shape_it_cannot_read(opener):
    """低-1（#356）：2xx 但形狀不認得是 `SidecarShapeError`，不是「連不上」。

    `{"items": [...]}` 是這一版不認的外層（形狀容錯不擴大猜測）：錯誤要帶著
    回應的 HTTP 狀態，`pmo check` 才能把「有回應、看不懂」與「連不上」分開。
    """
    opener.answer = (200, json.dumps({"items": []}).encode())
    with pytest.raises(SidecarShapeError) as caught:
        Sidecar(PMO_SIDECAR).list_agents()
    assert caught.value.status == 200
    assert "不是 agent 陣列" in str(caught.value)
    assert "連不上" not in str(caught.value)


def test_sidecar_adapter_reads_the_agents_wrapper(opener):
    """#356 裁定 §3：`GET /agents` 的外層就是 `{"agents": [...]}`，沒有別的形狀。"""
    opener.answer = (
        200,
        json.dumps(
            {"agents": [{"id": TEAM_PM, "state": "running", "session": "ses-1"}]}
        ).encode(),
    )
    (record,) = Sidecar(PMO_SIDECAR).list_agents()
    assert (record.agent_id, record.state, record.session) == (TEAM_PM, "running", "ses-1")


def test_sidecar_adapter_refuses_a_bare_array(opener):
    """上一輪收的裸陣列不再收：那是「形狀不符」，交給 check 報版本不一致。"""
    opener.answer = (200, json.dumps([{"id": TEAM_PM, "state": "running"}]).encode())
    with pytest.raises(SidecarShapeError) as caught:
        Sidecar(PMO_SIDECAR).list_agents()
    assert caught.value.status == 200


@pytest.mark.parametrize("element", ["x", None, [1]])
def test_sidecar_adapter_refuses_a_non_object_agent_record(opener, element):
    """中-2（#356 修正三）：`agents` 陣列內的非物件是形狀錯誤，不是 `AttributeError`。"""
    opener.answer = (200, json.dumps({"agents": [element]}).encode())
    with pytest.raises(SidecarShapeError) as caught:
        Sidecar(PMO_SIDECAR).list_agents()
    assert caught.value.status == 200
    assert caught.value.record is True
    assert "物件" in str(caught.value)


def test_sidecar_adapter_marks_a_record_without_state_as_a_record_shape_error(opener):
    """低-2（#356 修正三）：外層對了、單筆記錄缺欄位 → 記錄層的形狀錯誤。"""
    opener.answer = (200, json.dumps({"agents": [{"id": TEAM_PM}]}).encode())
    with pytest.raises(SidecarShapeError) as caught:
        Sidecar(PMO_SIDECAR).list_agents()
    assert caught.value.status == 200
    assert caught.value.record is True
    assert "state" in str(caught.value)


def test_sidecar_adapter_reads_attach_local_and_not_tmux(opener):
    """#356 裁定 §3：只讀 `attach.local`（原樣），`tmux` 是過渡別名不讀。"""
    opener.answer = (
        200,
        json.dumps(
            {
                "id": TEAM_PM,
                "state": "running",
                "attach": {
                    "local": "tmux attach -t demo",
                    "ssh": "ssh demo 'tmux attach -t demo'",
                    "tmux": "SHOULD-NOT-BE-READ",
                },
                "input_unconfirmed_at": "2026-11-03T10:00:00Z",
            }
        ).encode(),
    )
    record = Sidecar(PMO_SIDECAR).get_agent(TEAM_PM)
    assert record.attach == "tmux attach -t demo"
    assert record.input_unconfirmed_at == "2026-11-03T10:00:00Z"
    assert "SHOULD-NOT-BE-READ" not in json.dumps(record.to_dict())


def test_sidecar_adapter_reads_a_null_attach_as_none(opener):
    """非互動版（`-p`、opencode）的 `attach` 是 `null`；印的時候說「沒有 attach」。"""
    opener.answer = (200, json.dumps({"id": TEAM_PM, "state": "running", "attach": None}).encode())
    record = Sidecar(PMO_SIDECAR).get_agent(TEAM_PM)
    assert record.attach is None
    assert "attach" not in record.to_dict()


def test_sidecar_adapter_pokes_with_the_sop_payload(opener):
    status = Sidecar(PMO_SIDECAR).poke(TEAM_PM, text=ops.CLOSE_POKE_TEXT, interrupt=True)
    assert status == 200
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/agents/{TEAM_PM}/poke"
    assert request["method"] == "POST"
    assert json.loads(request["data"]) == {"text": ops.CLOSE_POKE_TEXT, "interrupt": True}


def test_sidecar_adapter_poke_returns_the_real_status(opener):
    """L3：`pmo poke` 印 sidecar 的回應碼，204 不假裝成 200。"""
    opener.answer = (204, b"")
    assert Sidecar(PMO_SIDECAR).poke(TEAM_PM, text=POKE_TEXT) == 204


def test_sidecar_adapter_closes_an_agent(opener):
    Sidecar(PMO_SIDECAR).close_agent(TEAM_PM)
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/agents/{TEAM_PM}/close"
    assert request["method"] == "POST"


def test_sidecar_adapter_reads_jobs_with_their_seconds(opener):
    """真的 sidecar 回 `{"jobs": [...]}`（sidecar `http.py`／`GET /jobs` 文件）。"""
    opener.answer = (
        200,
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "action": "poke",
                        "args": {
                            "id": PMO_IDENTITY,
                            "text": DEFAULT_WAKE_TEXT,
                            "interrupt": False,
                        },
                        "every": 600.0,
                        "at": None,
                        "auto": False,
                        "last_run": "2026-11-03T01:00:00Z",
                        "last_error": "agent_not_running",
                        "next_run_at": "2026-11-03T01:10:00Z",
                    }
                ]
            }
        ).encode(),
    )
    (job,) = Sidecar(PMO_SIDECAR).list_jobs()
    assert job.job_id == "job-1"
    assert job.every == 600.0
    assert job.at is None
    assert job.last_error == "agent_not_running"
    # 不認得的欄位留在 extra，不印（`auto` 由 `ops.is_auto_job` 讀）
    assert job.extra == {"auto": False, "next_run_at": "2026-11-03T01:10:00Z"}
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/jobs"


def test_sidecar_adapter_reads_the_jobs_object_wrapper(opener):
    """高-1：外層是物件（`{"jobs": []}`），不是陣列。"""
    opener.answer = (200, json.dumps({"jobs": []}).encode())
    assert Sidecar(PMO_SIDECAR).list_jobs() == []


def test_sidecar_adapter_refuses_a_jobs_answer_whose_outer_is_not_an_object(opener):
    """高-1：外層不是物件就丟 `SidecarError`（舊的陣列形狀不再收）。"""
    opener.answer = (200, json.dumps([{"id": "job-1", "action": "poke"}]).encode())
    with pytest.raises(SidecarError, match="不是物件"):
        Sidecar(PMO_SIDECAR).list_jobs()


def test_sidecar_adapter_reads_an_at_job_without_an_every(opener):
    """高-2：自動重開 job 是 `at` 型、`every: null`（sidecar.md）。"""
    opener.answer = (
        200,
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-auto",
                        "action": "open",
                        "args": {"id": TEAM_PM, "provider": "opencode", "model": "m"},
                        "every": None,
                        "at": "2026-11-03T02:00:00Z",
                        "auto": True,
                        "last_run": None,
                        "last_error": None,
                        "next_run_at": "2026-11-03T02:00:00Z",
                    }
                ]
            }
        ).encode(),
    )
    (job,) = Sidecar(PMO_SIDECAR).list_jobs()
    assert (job.job_id, job.action) == ("job-auto", "open")
    assert (job.every, job.at) == (None, "2026-11-03T02:00:00Z")
    assert job.extra["auto"] is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 低-1：sidecar 回的是物件，只存碼，`message` 不存（D-12）
        ({"status": 409, "code": "agent_not_running", "message": "no agent here"}, "agent_not_running"),
        ("agent_awaiting_answer", "agent_awaiting_answer"),  # 字串也容忍
        (None, None),
    ],
)
def test_sidecar_adapter_keeps_only_the_last_error_code(opener, value, expected):
    opener.answer = (
        200,
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "action": "poke",
                        "args": {"id": PMO_IDENTITY, "text": DEFAULT_WAKE_TEXT},
                        "every": 600.0,
                        "last_error": value,
                    }
                ]
            }
        ).encode(),
    )
    (job,) = Sidecar(PMO_SIDECAR).list_jobs()
    assert job.last_error == expected


def test_sidecar_adapter_accepts_a_job_without_an_every_or_at(opener):
    """#371：沒有 `every` 也沒有 `at` 的 job 照收（`every`／`at` 都是 None）。

    以前整份 `GET /jobs` 會因此被拒，連 `pmo close` 要刪的自動重開 job 都讀不到；
    現在只有沒有 `id`／`action` 或 `every` 壞掉才失敗（見下面三支）。
    """
    opener.answer = (
        200,
        json.dumps({"jobs": [{"id": "job-1", "action": "poke"}]}).encode(),
    )
    (job,) = Sidecar(PMO_SIDECAR).list_jobs()
    assert (job.job_id, job.action) == ("job-1", "poke")
    assert (job.every, job.at) == (None, None)


def test_sidecar_adapter_refuses_a_job_whose_every_is_broken(opener):
    """`every` 有值但不是時長（字串也不是數字）→ 整份清單拒絕（#371）。"""
    opener.answer = (
        200,
        json.dumps({"jobs": [{"id": "job-1", "action": "poke", "every": "soon"}]}).encode(),
    )
    with pytest.raises(SidecarError, match="every"):
        Sidecar(PMO_SIDECAR).list_jobs()


@pytest.mark.parametrize(
    "record",
    [
        {"action": "poke", "every": 600.0},  # 沒有 id
        {"id": "job-1", "every": 600.0},  # 沒有 action
    ],
)
def test_sidecar_adapter_refuses_a_job_without_an_id_or_action(opener, record):
    opener.answer = (200, json.dumps({"jobs": [record]}).encode())
    with pytest.raises(SidecarError):
        Sidecar(PMO_SIDECAR).list_jobs()


def test_sidecar_adapter_creates_the_designs_poke_job(opener):
    """`POST /jobs` 的 body 照 §3.6：action／args／every，不帶 `until`。"""
    opener.answer = (
        201,
        json.dumps(
            {
                "id": "job-9",
                "action": "poke",
                "args": {"id": PMO_IDENTITY, "text": DEFAULT_WAKE_TEXT, "interrupt": False},
                "every": 600.0,
                "next_run_at": "2026-11-03T02:10:00Z",
            }
        ).encode(),
    )
    job = Sidecar(PMO_SIDECAR).create_job(
        every="10m", agent_id=PMO_IDENTITY, text=DEFAULT_WAKE_TEXT
    )
    assert (job.job_id, job.every) == ("job-9", 600.0)
    (request,) = opener.requests
    assert (request["url"], request["method"]) == (f"{PMO_SIDECAR}/jobs", "POST")
    body = json.loads(request["data"])
    assert body == {
        "action": "poke",
        "args": {"id": PMO_IDENTITY, "text": DEFAULT_WAKE_TEXT, "interrupt": False},
        "every": "10m",
    }
    assert "until" not in body


@pytest.mark.parametrize(
    ("body", "detail"),
    [(b"", "不是物件"), (b"{not json", "不是 JSON")],
    ids=["empty-body", "unreadable-body"],
)
def test_sidecar_adapter_reports_a_created_job_whose_201_body_is_unreadable(
    opener, body, detail
):
    """201 但 body 讀不懂：`JobCreatedUnreadable`（帶 201），不是建立失敗。

    sidecar 先 create、再寫回應，所以 job 已經在了；`wake set` 靠這個型別的
    狀態碼決定再 `GET /jobs` 找回它（裁定第 1 節）。
    """
    opener.answer = (201, body)
    with pytest.raises(JobCreatedUnreadable) as caught:
        Sidecar(PMO_SIDECAR).create_job(
            every="10m", agent_id=PMO_IDENTITY, text=DEFAULT_WAKE_TEXT
        )
    assert caught.value.status == 201
    assert detail in str(caught.value)


def test_sidecar_adapter_refuses_a_jobs_answer_that_is_not_a_list(opener):
    opener.answer = (200, json.dumps({"id": "job-1"}).encode())
    with pytest.raises(SidecarError, match="job 陣列"):
        Sidecar(PMO_SIDECAR).list_jobs()


@pytest.mark.parametrize(("status", "expected"), [(204, True), (200, True), (404, False)])
def test_sidecar_adapter_delete_job_reports_whether_it_existed(opener, status, expected):
    opener.answer = (status, b"")
    assert Sidecar(PMO_SIDECAR).delete_job("job-1") is expected
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/jobs/job-1"
    assert request["method"] == "DELETE"


def test_sidecar_adapter_delete_job_raises_on_other_refusals(opener):
    opener.answer = (500, json.dumps({"code": "job_store_broken"}).encode())
    with pytest.raises(SidecarError) as caught:
        Sidecar(PMO_SIDECAR).delete_job("job-1")
    assert caught.value.code == "job_store_broken"
# --------------------------------------------------------------------------- #
# 3. §3.6「自己的 job」的判斷（純函式）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (
            JobRecord("j", "poke", {"id": "someone", "text": "叫醒"}, 600.0),
            True,
        ),
        (JobRecord("j", "poke", {"id": "someone", "text": "別的句子"}, 600.0), False),
        (JobRecord("j", "close", {"id": "someone", "text": "叫醒"}, 600.0), False),
        (
            JobRecord("j", "poke", {"id": "someone", "text": "叫醒", "auto": True}, 600.0),
            False,
        ),
        (
            JobRecord("j", "poke", {"id": "someone", "text": "叫醒"}, 600.0, extra={"auto": True}),
            False,
        ),
    ],
)
def test_is_own_job_follows_the_fixed_sentence_and_auto(job, expected):
    assert ops.is_own_job(job, "叫醒") is expected


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (JobRecord("j", "poke", {"id": "a", "text": "重開"}, 600.0, extra={"auto": True}), True),
        (JobRecord("j", "poke", {"id": "a", "text": "重開", "auto": True}, 600.0), True),
        (JobRecord("j", "poke", {"id": "a", "text": "重開"}, 600.0, extra={"auto": False}), False),
        (JobRecord("j", "poke", {"id": "a", "text": "重開"}, 600.0), False),
        (JobRecord("j", "open", {"id": "a"}, 600.0, extra={"auto": True}), True),
    ],
)
def test_is_auto_job_reads_the_job_field_and_the_args_field(job, expected):
    """裁定 (d)：`auto` 是 job 欄位，舊的或假的 sidecar 可能放在 `args` 裡。"""
    assert ops.is_auto_job(job) is expected

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
from pmo.ports import AgentRecord, AicommTimeout, JobRecord, PortError, SidecarError

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
    opener.answer = (
        200,
        json.dumps(
            [
                {
                    "id": "job-1",
                    "action": "poke",
                    "args": {"id": PMO_IDENTITY, "text": DEFAULT_WAKE_TEXT, "interrupt": False},
                    "every": 600.0,
                    "last_run": "2026-11-03T01:00:00Z",
                    "last_error": "agent_not_running",
                    "auto": False,
                }
            ]
        ).encode(),
    )
    (job,) = Sidecar(PMO_SIDECAR).list_jobs()
    assert job.job_id == "job-1"
    assert job.every == 600.0
    assert job.last_error == "agent_not_running"
    assert job.extra == {"auto": False}  # 不認得的欄位留在 extra，不印
    (request,) = opener.requests
    assert request["url"] == f"{PMO_SIDECAR}/jobs"


def test_sidecar_adapter_refuses_a_job_without_an_every(opener):
    opener.answer = (200, json.dumps([{"id": "job-1", "action": "poke"}]).encode())
    with pytest.raises(SidecarError, match="every"):
        Sidecar(PMO_SIDECAR).list_jobs()


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

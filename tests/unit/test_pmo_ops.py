"""`pmo reply`／`poke`／`status`／`close` 的測試（#110 T-4c；design §3.2、§3.3）。

驗收（工單 #357）：§3.2 對應四列的參數、輸出與每個結束碼（含拒絕條件與埠失敗）；
`close` 照環境的 Close SOP（先刪 sidecar 的自動重開 job → poke 打斷 → close →
409 `close_in_progress` 時輪詢、不重送）與 #357 架構師裁定 (d)：只刪 `auto: true`
且 `args.id` 是這個 team PM 的 job，任何 poke job 都不刪、只列不動；agent 已
`exited` 時仍送 close；agent 不在時不呼叫 `close_agent`；retired team 的
`reply`／`poke` 回 1，`status`／`close` 照常。`status` 只印已知欄位、PMO 不在
sidecar 時不查 job；會寄信的 `reply` 有身分守門；錯誤訊息不含訊息內文（D-12）。
全部用假 Angareion／aicomm／sidecar 與暫存 state（`tests/conftest.py` 的
`isolated_home`），不打真的後端。`adapters` 的真接線測試（aicomm 的旗面與
sidecar 的請求／錯誤）在 `test_pmo_ops_adapters.py`（#357 第二輪 L-a：這個檔
不要超過 1000 行）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import io
import json
import subprocess
import urllib.error
from datetime import datetime, timezone

import pytest

from pmo import ops
from pmo.adapters import Aicomm, Sidecar
from pmo.config import DEFAULT_WAKE_TEXT
from pmo.fakes import FakeAicomm, FakeSidecar
from pmo.ports import AgentRecord, AicommTimeout, JobRecord, PortError, SidecarError
from pmo.runtime import Deps

import _pmo_env as E
from _pmo_env import (
    PMO_IDENTITY,
    TEAM_CHANNEL,
    TEAM_KEY,
    TEAM_PM,
    PmoEnv,
    config_data,
    pmo_env,
    run_cli,
    seed_team_state,
)

# --------------------------------------------------------------------------- #
# 假資料（全部是假值；不含真頻道、真身分、真 sidecar 位址）
# --------------------------------------------------------------------------- #

#: 假的 PMO sidecar 位址：測試只注入假物件，不會連上去。
PMO_SIDECAR = "http://127.0.0.1:8499"
OTHER_TEAM_KEY = "demo2-w1"
OTHER_TEAM_PM = "demo2-pm-w1"
RETIRED_TEAM_KEY = "demo3-m1"
RETIRED_TEAM_PM = "demo3-pm-m1"
BODY = "請說明目前的進度，回報前不要重開。"
POKE_TEXT = "看一下 pmo status"
#: 假的「自動重開 job」句子（sidecar 排的那種；不是叫醒固定句）。
AUTO_TEXT = "重開"
NOW = datetime(2026, 10, 19, 10, 15, 0, tzinfo=timezone.utc)


def write_config(env: PmoEnv, *, top: dict | None = None, teams: list[dict] | None = None) -> None:
    """改寫假設定（PMO sidecar 一類的頂層欄位、或第二個 team）。"""
    data = config_data()
    if top:
        data.update(top)
    if teams:
        data["teams"].extend(teams)
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def worker_team() -> dict:
    """第二個 team：worker（sidecar 在別的機器上，D-8 不碰）。"""
    return {
        "key": OTHER_TEAM_KEY,
        "pm": OTHER_TEAM_PM,
        "channel": f"team-{OTHER_TEAM_KEY}",
        "sidecar": "worker",
        "tui": True,
        "life": False,
        "retired": False,
    }


def retired_team() -> dict:
    """第三個 team：已退役（#357 裁定 (d)：reply／poke 拒，status／close 照常）。"""
    return {
        "key": RETIRED_TEAM_KEY,
        "pm": RETIRED_TEAM_PM,
        "channel": f"team-{RETIRED_TEAM_KEY}",
        "sidecar": "http://127.0.0.1:8472",  # 假埠：不是真的 sidecar 埠
        "tui": False,
        "life": False,
        "retired": True,
    }


def add_auto_job(sidecar, job_id: str, *, agent_id: str = TEAM_PM, every: float = 600.0) -> JobRecord:
    """sidecar 在 waiting-quota 時替 `agent_id` 排的自動重開 job（`auto: true`）。"""
    job = JobRecord(
        job_id=job_id,
        action="poke",
        args={"id": agent_id, "text": AUTO_TEXT, "interrupt": False},
        every=every,
        extra={"auto": True},
    )
    sidecar.jobs[job_id] = job
    return job


class RecordingSidecar(FakeSidecar):
    """`FakeSidecar` 加上呼叫順序（M3：agent 不在時不該呼叫 `close_agent`）。"""

    def __init__(self, *, close_error: SidecarError | None = None) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.close_error = close_error

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        self.calls.append("poke")
        return super().poke(agent_id, text=text, interrupt=interrupt)

    def close_agent(self, agent_id: str) -> None:
        self.calls.append("close_agent")
        if self.close_error is not None:
            raise self.close_error
        super().close_agent(agent_id)

    def list_jobs(self) -> list[JobRecord]:
        self.calls.append("list_jobs")
        return super().list_jobs()

    def delete_job(self, job_id: str) -> bool:
        self.calls.append(f"delete_job:{job_id}")
        return super().delete_job(job_id)

    def count(self, name: str) -> int:
        return self.calls.count(name)


class VanishingJobSidecar(FakeSidecar):
    """`list_jobs` 還看得到，`delete_job` 卻回 False（別處先刪掉了；#357 L5）。"""

    def delete_job(self, job_id: str) -> bool:
        self.deleted_jobs.append(job_id)
        self.jobs.pop(job_id, None)
        return False


class DeleteFailsSidecar(RecordingSidecar):
    """指定的 auto job 刪不掉（給定的錯誤碼），其餘照刪（#357 第二輪 M-1）。"""

    def __init__(self, failing: dict[str, str]) -> None:
        super().__init__()
        self.failing = dict(failing)

    def delete_job(self, job_id: str) -> bool:
        code = self.failing.get(job_id)
        if code is not None:
            self.calls.append(f"delete_job:{job_id}")
            raise SidecarError(status=500, code=code)
        return super().delete_job(job_id)


class StuckCloseSidecar(RecordingSidecar):
    """`close` 回 200，但 agent 一直沒 `exited`（200 已收下、輪詢逾時）。"""

    def close_agent(self, agent_id: str) -> None:
        self.calls.append("close_agent")


def ops_deps(*, sidecar=None, aicomm=None, port: bool = True):
    """假 aicomm＋假 sidecar＋固定時鐘；回 `(deps, aicomm, sidecar)`。"""
    aicomm = aicomm if aicomm is not None else FakeAicomm()
    sidecar = sidecar if sidecar is not None else FakeSidecar()
    deps = Deps(
        angareion=E.FakeAngareion(),
        aicomm=aicomm,
        sidecar=sidecar if port else None,
        now=lambda: NOW,
    )
    return deps, aicomm, sidecar


def team_state(env: PmoEnv) -> dict:
    return json.loads(env.team_file().read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. `pmo reply`：經 aicomm 寫入＋叫醒（design §3.2）
# --------------------------------------------------------------------------- #


def test_reply_goes_through_aicomm_with_channel_recipient_and_urgency(pmo_env):
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--urgency", "8", "--body", "-"],
        pmo_env,
        deps=deps,
        stdin_text=BODY,
    )

    assert (rc, err) == (0, "")
    (sent,) = aicomm.sent
    assert (sent.channel, sent.to, sent.body, sent.urgency) == (
        TEAM_CHANNEL,
        TEAM_PM,
        BODY,
        8,
    )
    assert out.strip() == f"[reply] 訊息已送出 id={sent.message_id}"
    assert BODY not in out  # 只印 id，不把內文回印


def test_reply_sends_no_urgency_when_the_flag_is_absent(pmo_env):
    deps, aicomm, _sidecar = ops_deps()
    rc, _out, err = run_cli(
        ["reply", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert (rc, err) == (0, "")
    assert aicomm.sent[0].urgency is None


def test_reply_under_another_identity_is_exit_2_and_sends_nothing(pmo_env):
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--body", "-"],
        pmo_env,
        deps=deps,
        stdin_text=BODY,
        identity=E.OTHER_IDENTITY_WRONG,
    )
    assert rc == 2
    assert out == ""
    assert "身分不符拒送" in err
    assert aicomm.sent == []


def test_reply_unknown_team_is_exit_1_and_sends_nothing(pmo_env):
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(
        ["reply", "no-such-team", "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err
    assert aicomm.sent == []


@pytest.mark.parametrize(
    "argv",
    [
        ["reply", TEAM_KEY],
        ["reply", TEAM_KEY, "--body", ""],
        ["reply", TEAM_KEY, "--body", "-"],
    ],
)
def test_reply_with_a_missing_or_empty_body_is_exit_1(pmo_env, argv):
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(argv, pmo_env, deps=deps, stdin_text="  \n")
    assert rc == 1
    assert out == ""
    assert "--body" in err
    assert aicomm.sent == []


@pytest.mark.parametrize("urgency", ["-1", "10"])
def test_reply_refuses_an_urgency_outside_zero_to_nine(pmo_env, urgency):
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--urgency", urgency, "--body", "-"],
        pmo_env,
        deps=deps,
        stdin_text=BODY,
    )
    assert rc == 1
    assert out == ""
    assert "0-9" in err
    assert aicomm.sent == []


def test_reply_that_aicomm_refuses_is_exit_2(pmo_env):
    aicomm = FakeAicomm()
    aicomm.always_fail = PortError("backend down")
    deps, _aicomm, _sidecar = ops_deps(aicomm=aicomm)
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert rc == 2
    assert out == ""
    assert "回覆送不出去" in err


def test_reply_aicomm_timeout_says_the_result_is_unknown(pmo_env):
    """aicomm 逾時時信可能已經寫進去：說「結果不明」，不印 id、不叫人重送。"""
    aicomm = FakeAicomm()
    aicomm.always_fail = AicommTimeout("aicomm 逾時")
    deps, _aicomm, _sidecar = ops_deps(aicomm=aicomm)
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert rc == 2
    assert out == ""
    assert "送出結果不明" in err and "不要直接重送" in err
    assert "id=" not in out


def test_reply_without_the_aicomm_port_is_exit_2(pmo_env):
    deps = Deps(angareion=E.FakeAngareion(), sidecar=FakeSidecar(), now=lambda: NOW)
    rc, out, err = run_cli(
        ["reply", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert rc == 2
    assert out == ""
    assert "aicomm 埠沒有接上" in err


# --------------------------------------------------------------------------- #
# 2. `pmo poke`：sidecar 的回應碼（design §3.2）
# --------------------------------------------------------------------------- #


def test_poke_prints_the_response_code_and_pokes_the_team_pm(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert (rc, err) == (0, "")
    assert sidecar.pokes == [(TEAM_PM, POKE_TEXT, False)]
    assert out.strip() == f"[poke] {TEAM_PM} 200"


def test_poke_interrupt_is_passed_through(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    rc, _out, err = run_cli(
        ["poke", TEAM_KEY, "--interrupt", "--body", "-"],
        pmo_env,
        deps=deps,
        stdin_text=POKE_TEXT,
    )
    assert (rc, err) == (0, "")
    assert sidecar.pokes == [(TEAM_PM, POKE_TEXT, True)]


class PokeCodeSidecar(FakeSidecar):
    """poke 的回應碼不是 200（L3：印 sidecar 真正的回應碼）。"""

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        super().poke(agent_id, text=text, interrupt=interrupt)
        return 204


def test_poke_prints_the_code_the_sidecar_really_answered(pmo_env):
    deps, _aicomm, sidecar = ops_deps(sidecar=PokeCodeSidecar())
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert (rc, err) == (0, "")
    assert out.strip() == f"[poke] {TEAM_PM} 204"


@pytest.mark.parametrize(
    "error",
    [
        SidecarError(status=409, code="agent_awaiting_answer"),
        SidecarError(status=404, code="agent_not_running"),
        SidecarError(detail="sidecar 連不上（URLError）"),
    ],
)
def test_poke_refused_by_the_sidecar_is_exit_2(pmo_env, error):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.errors["poke"] = error
    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert rc == 2
    assert out == ""  # 沒有成功的回應碼可印
    assert "沒成功" in err
    assert BODY not in err


def test_poke_unknown_team_is_exit_1(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    rc, out, err = run_cli(
        ["poke", "no-such-team", "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err
    assert sidecar.pokes == []


def test_poke_a_worker_team_is_exit_2_and_never_touches_the_sidecar(pmo_env):
    write_config(pmo_env, teams=[worker_team()])
    deps, _aicomm, sidecar = ops_deps()
    sidecar.errors["poke"] = SidecarError(status=500, detail="should not be called")
    rc, out, err = run_cli(
        ["poke", OTHER_TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert rc == 2
    assert out == ""
    assert "worker team" in err and "D-8" in err


@pytest.mark.parametrize("argv", [["poke", TEAM_KEY], ["poke", TEAM_KEY, "--body", ""]])
def test_poke_with_a_missing_or_empty_body_is_exit_1(pmo_env, argv):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(argv, pmo_env, deps=deps, stdin_text="")
    assert rc == 1
    assert out == ""
    assert "--body" in err
    assert sidecar.pokes == []


def test_poke_without_the_sidecar_port_is_exit_2(pmo_env):
    deps, _aicomm, _sidecar = ops_deps(port=False)
    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert rc == 2
    assert out == ""
    assert "sidecar 埠沒有接上" in err


# --------------------------------------------------------------------------- #
# 2b. retired team：會開始工作或叫醒人的指令拒絕，讀與收尾照常（裁定 (d)）
# --------------------------------------------------------------------------- #


def test_reply_to_a_retired_team_is_exit_1_and_sends_nothing(pmo_env):
    write_config(pmo_env, teams=[retired_team()])
    deps, aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(
        ["reply", RETIRED_TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=BODY
    )
    assert (rc, out) == (1, "")
    assert "已退役" in err
    assert aicomm.sent == []


def test_poke_to_a_retired_team_is_exit_1_and_pokes_nothing(pmo_env):
    write_config(pmo_env, teams=[retired_team()])
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(RETIRED_TEAM_PM)
    rc, out, err = run_cli(
        ["poke", RETIRED_TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text=POKE_TEXT
    )
    assert (rc, out) == (1, "")
    assert "已退役" in err
    assert sidecar.pokes == []


def test_status_and_close_still_work_for_a_retired_team(pmo_env):
    """收尾正是 retired team 會需要的：兩個都照常（裁定 (d) §3.2）。"""
    write_config(pmo_env, teams=[retired_team()])
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(RETIRED_TEAM_PM, state="running")

    rc, out, err = run_cli(["status", RETIRED_TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    assert f"[status] {RETIRED_TEAM_KEY} pm={RETIRED_TEAM_PM} state=running" in out

    rc, out, err = run_cli(["close", RETIRED_TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    assert sidecar.closed == [RETIRED_TEAM_PM]
    assert f"[close] {RETIRED_TEAM_PM} 已關（state=exited）" in out


# --------------------------------------------------------------------------- #
# 3. `pmo status`：state／exit／session 與 PMO 自己排的 job（design §3.2）
# --------------------------------------------------------------------------- #


def test_status_prints_one_teams_state_exit_and_session(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="running", session="ses-1")
    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    assert out.splitlines()[0] == (
        f"[status] {TEAM_KEY} pm={TEAM_PM} state=running exit=- session=ses-1"
    )
    assert "PMO 不在 sidecar" in out  # 沒有 pmo_sidecar 時不查 job（§3.4）


def test_status_shows_the_exit_reason(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(
        TEAM_PM,
        state="exited",
        session=None,
        exit={"reason": "no candidate is left after exclude"},
    )
    rc, out, _err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 0
    assert "state=exited" in out
    assert "exit=no candidate is left after exclude" in out
    assert "session=-" in out


def test_status_reports_an_absent_agent_without_failing(pmo_env):
    deps, _aicomm, _sidecar = ops_deps()  # 沒有任何 agent
    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    assert "state=absent" in out


def test_status_lists_every_team_and_marks_the_worker_one(pmo_env):
    write_config(pmo_env, teams=[worker_team()], top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="idle", session="s2")
    rc, out, err = run_cli(["status"], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    lines = out.splitlines()
    assert f"[status] {TEAM_KEY} pm={TEAM_PM} state=idle exit=- session=s2" in lines
    worker_line = next(line for line in lines if line.startswith(f"[status] {OTHER_TEAM_KEY} "))
    assert "state=worker" in worker_line and "D-8" in worker_line


def test_status_unknown_team_is_exit_1(pmo_env):
    deps, _aicomm, _sidecar = ops_deps()
    rc, out, err = run_cli(["status", "no-such-team"], pmo_env, deps=deps)
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err


def test_status_json_holds_the_same_known_fields(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="running", session="ses-1")
    sidecar.add_job("job-1", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)
    rc, out, err = run_cli(["status", "--json"], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    data = json.loads(out)
    assert data["teams"] == [
        {"key": TEAM_KEY, "pm": TEAM_PM, "state": "running", "exit": None, "session": "ses-1"}
    ]
    (job,) = data["jobs"]
    assert job["id"] == "job-1"
    assert job["every"] == 600.0
    assert job["args"]["text"] == DEFAULT_WAKE_TEXT
    assert "jobs_note" not in data


def test_status_lists_the_pmos_own_jobs_and_the_ones_aimed_at_the_named_team(pmo_env):
    """§3.6：自己的 job 用固定句認；`status ‹team›` 另外只列瞄準該 team PM 的 job。"""
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.add_job("job-own", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)
    sidecar.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    sidecar.add_job("job-other-text", every="5m", text="別人的句子", agent_id=PMO_IDENTITY)
    rc, out, _err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 0
    assert "job-own" in out
    assert "瞄準" in out and "job-team" in out  # 只列不動（裁定 (d) 末段）
    assert "固定句=否" in out
    assert "job-other-text" not in out  # 不是自己的、也不是瞄準這個 team PM
    assert sidecar.deleted_jobs == []  # 唯讀：什麼都沒刪


def test_status_does_not_list_jobs_when_no_team_is_named(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    rc, out, _err = run_cli(["status"], pmo_env, deps=deps)
    assert rc == 0
    assert "job-team" not in out


def test_status_json_carries_the_named_teams_jobs(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    rc, out, err = run_cli(["status", TEAM_KEY, "--json"], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    data = json.loads(out)
    (job,) = data["team_jobs"]
    assert (job["id"], job["action"], job["every"]) == ("job-team", "poke", 600.0)


def test_status_without_a_pmo_sidecar_does_not_query_jobs(pmo_env):
    """§3.4：沒有 pmo_sidecar＝PMO 不在 sidecar，不查 /jobs。"""
    deps, _aicomm, sidecar = ops_deps()
    sidecar.errors["list_jobs"] = SidecarError(status=500, detail="must not be called")
    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    assert "PMO 不在 sidecar" in out


def test_status_keeps_reading_the_other_teams_when_one_sidecar_fails(pmo_env):
    write_config(pmo_env, teams=[worker_team()], top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.errors["get_agent"] = SidecarError(status=503, code="sidecar_busy")
    rc, out, err = run_cli(["status"], pmo_env, deps=deps)
    assert rc == 2
    assert "state=unknown" in out and "sidecar_busy" in out
    assert "state=worker" in out  # 其他 team 照印（§2.3 的部分失敗）
    assert "sidecar_busy" in err


# --------------------------------------------------------------------------- #
# 4. `pmo close`：裁定 (d) 的 job 處理、Close SOP 的順序與輪詢
# --------------------------------------------------------------------------- #


def test_close_pokes_interrupts_then_closes_and_deletes_the_auto_jobs(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    seed_team_state(pmo_env, ticket="demo#45")
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="running", session="ses-1")
    add_auto_job(sidecar, "job-auto")
    sidecar.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.pokes == [(TEAM_PM, ops.CLOSE_POKE_TEXT, True)]  # Close SOP 第 1 步
    assert sidecar.closed == [TEAM_PM]
    assert sidecar.deleted_jobs == ["job-auto"]  # 只刪 auto（裁定 (d)）
    lines = out.splitlines()
    assert lines[0] == f"[close] {TEAM_PM} 已關（state=exited）"
    assert lines[1] == "[close] 刪掉自動重開 job：job-auto"
    assert f"[close] 瞄準 {TEAM_PM} 的非 auto job（只列不動，要停請找排的人刪）：" in lines
    assert any("job-team action=poke every=600s 固定句=否" in line for line in lines)
    assert "job-team" in sidecar.jobs  # 只列不動

    state = team_state(pmo_env)
    assert state["closed"] is True
    assert state["state"] == "exited"
    assert state["ticket"] == "demo#45"  # 其他欄位不動


def test_close_deletes_only_the_auto_reopen_jobs_for_this_team_pm(pmo_env):
    """裁定 (d)：任何 poke job 都不刪（含固定句相同）；只刪 auto＋`args.id` 的 job。"""
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto")
    add_auto_job(sidecar, "job-auto-other-agent", agent_id=OTHER_TEAM_PM)
    add_auto_job(sidecar, "job-auto-pmo", agent_id=PMO_IDENTITY)
    sidecar.add_job("job-own", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)
    sidecar.add_job("job-same-sentence", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=TEAM_PM)
    sidecar.add_job("job-other-agent", every="10m", text="看一下", agent_id=OTHER_TEAM_PM)
    rc, out, _err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 0
    assert sidecar.deleted_jobs == ["job-auto"]
    assert set(sidecar.jobs) == {
        "job-auto-other-agent",
        "job-auto-pmo",
        "job-own",
        "job-same-sentence",
        "job-other-agent",
    }
    assert "job-own" not in out and "job-other-agent" not in out
    assert "job-auto-pmo" not in out  # auto：不列，也不刪（不是這個 team PM 的）
    assert "job-same-sentence" in out  # 瞄準這個 agent：只列不動
    assert "固定句=是" in out


def test_close_an_agent_that_is_not_on_the_sidecar_never_opens_a_close(pmo_env):
    """M3：agent 不在時不呼叫 `close_agent`（也不 poke），但自動重開 job 照處理。"""
    deps, _aicomm, sidecar = ops_deps(sidecar=RecordingSidecar())
    add_auto_job(sidecar, "job-auto")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.count("close_agent") == 0
    assert sidecar.count("poke") == 0
    assert sidecar.deleted_jobs == ["job-auto"]
    assert out.splitlines()[0] == f"[close] {TEAM_PM} 本來就不在 sidecar 上（已關）"
    assert team_state(pmo_env)["closed"] is True


def test_close_an_exited_agent_still_sends_close(pmo_env):
    """裁定 3.1：sidecar 的 close 是 idempotent，而且會刪掉 handoff，所以要送。"""
    deps, _aicomm, sidecar = ops_deps(sidecar=RecordingSidecar())
    sidecar.add_agent(TEAM_PM, state="exited")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.count("poke") == 0  # 已 exited 不戳
    assert sidecar.count("close_agent") == 1  # 但仍送 close
    assert out.splitlines()[0] == f"[close] {TEAM_PM} 已關（state=exited）"


def test_close_deletes_the_auto_jobs_before_it_closes_a_waiting_quota_agent(pmo_env):
    """裁定 (d)：先刪自動重開 job 再 close；waiting-quota 的 agent 也一樣。"""
    deps, _aicomm, sidecar = ops_deps(sidecar=RecordingSidecar())
    sidecar.add_agent(TEAM_PM, state="waiting-quota")
    add_auto_job(sidecar, "job-auto")
    rc, _out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.calls.index("delete_job:job-auto") < sidecar.calls.index("close_agent")


def test_close_still_closes_and_exits_zero_when_jobs_cannot_be_listed(pmo_env):
    """裁定 (d)：`GET /jobs` 失敗只印 `[warn]`，照樣 close，結束碼不因此變 2。"""
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.errors["list_jobs"] = SidecarError(status=503, code="sidecar_busy")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 0
    assert "已關" in out
    assert "sidecar_busy" in err
    assert "到點可能又把 agent 開回來" in err


def test_close_does_not_count_a_job_that_was_already_gone(pmo_env):
    """L5：`delete_job` 回 False（404）＝本來就不在，不算「刪掉的 job」。"""
    deps, _aicomm, sidecar = ops_deps(sidecar=VanishingJobSidecar())
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert "[close] 刪掉自動重開 job：job-auto" not in out
    assert "[close] 自動重開 job job-auto 已經不在（404）" in out


class CloseInProgressSidecar(FakeSidecar):
    """`close` 一律回 409 `close_in_progress`（或指定的錯誤）；`get_agent` 依序回狀態。

    `None` 代表 agent 已經不在 sidecar 上（404）。Close SOP：409 時不重送
    close，改成輪詢 `GET /agents/<id>`；`close_error` 用來驗別碼的 409 不輪詢。
    """

    def __init__(self, states: list[str | None], *, close_error: SidecarError | None = None) -> None:
        super().__init__()
        self.states = list(states)
        self.close_calls = 0
        self.get_calls = 0
        self.close_error = close_error

    def get_agent(self, agent_id: str) -> AgentRecord:
        self.get_calls += 1
        if not self.states:
            raise SidecarError(status=404, code="agent_not_found")
        state = self.states.pop(0)
        if state is None:
            raise SidecarError(status=404, code="agent_not_found")
        return AgentRecord(agent_id=agent_id, state=state, session="ses-1")

    def poke(self, agent_id: str, *, text: str, interrupt: bool = False) -> int:
        self.pokes.append((agent_id, text, interrupt))
        return 200

    def close_agent(self, agent_id: str) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        raise SidecarError(status=409, code="close_in_progress")


@pytest.fixture
def fast_close(monkeypatch):
    """不真的等：`_sleep` 只記次數，`_monotonic` 每次前進 1 秒。"""
    slept: list[float] = []
    ticks = {"now": 0.0}

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        ticks["now"] += seconds

    def _monotonic() -> float:
        ticks["now"] += 1.0
        return ticks["now"]

    monkeypatch.setattr(ops, "_sleep", _sleep)
    monkeypatch.setattr(ops, "_monotonic", _monotonic)
    return slept


def test_close_polls_on_409_and_never_resends_close(pmo_env, fast_close):
    sidecar = CloseInProgressSidecar(["running", "running", "exited"])
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.close_calls == 1  # 沒有重送 close
    assert sidecar.get_calls == 3  # 開頭一次＋輪詢兩次
    assert fast_close == [ops.CLOSE_POLL_SECONDS]
    assert out.splitlines()[0] == f"[close] {TEAM_PM} 已關（state=exited）"


def test_close_in_progress_that_never_exits_is_exit_0_and_says_to_check_status(pmo_env, monkeypatch):
    """裁定 3.1：409 `close_in_progress` 記 `close_requested_at`、回 0、提示用 status 確認。"""
    sidecar = CloseInProgressSidecar(["running"] * 10)
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    # `--timeout 0`：不給它輪詢的時間，回最後看到的 state。
    monkeypatch.setattr(ops, "_sleep", lambda seconds: None)
    monkeypatch.setattr(ops, "_monotonic", lambda: 1000.0)
    rc, out, err = run_cli(["close", TEAM_KEY, "--timeout", "0"], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert "已關" not in out  # 沒有說它關好了
    assert "還在背景進行" in out and "pmo status" in out
    assert sidecar.close_calls == 1  # 沒有重送 close
    state = team_state(pmo_env)
    assert state["close_requested_at"] == "2026-10-19T10:15:00Z"
    assert state.get("closed") is not True


def test_close_with_a_409_that_is_not_close_in_progress_is_exit_2_without_polling(pmo_env):
    """M1：只有 `close_in_progress` 的 409 才輪詢，別碼照一般 sidecar 拒絕。"""
    error = SidecarError(status=409, code="agent_awaiting_answer")
    sidecar = CloseInProgressSidecar(["running"] * 3, close_error=error)
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 2
    assert out == ""
    assert "agent_awaiting_answer" in err
    assert "等不到" not in err  # 沒有把它當成「關閉中」去輪詢
    assert sidecar.close_calls == 1
    assert sidecar.get_calls == 1  # 開頭只讀了一次


def test_close_that_is_accepted_but_never_exits_is_exit_2_without_claiming_success(pmo_env, monkeypatch):
    """close 回 200 但等不到 exited：回 2、不重送、不說已關（和 close_in_progress 分開）。"""
    sidecar = StuckCloseSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    monkeypatch.setattr(ops, "_sleep", lambda seconds: None)
    monkeypatch.setattr(ops, "_monotonic", lambda: 1000.0)
    rc, out, err = run_cli(["close", TEAM_KEY, "--timeout", "0"], pmo_env, deps=deps)

    assert rc == 2
    assert "已關" not in out
    assert "等不到 exited（state=running）" in err
    assert sidecar.count("close_agent") == 1
    assert not pmo_env.team_file().exists()  # 什麼都沒寫：既沒關、也沒「已要求關」


def test_close_that_the_sidecar_refuses_is_exit_2(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.errors["get_agent"] = SidecarError(status=503, code="sidecar_busy")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 2
    assert out == ""
    assert "關不掉" in err and "sidecar_busy" in err


def test_close_keeps_the_close_when_the_job_deletion_fails(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto")
    sidecar.errors["delete_job"] = SidecarError(status=500, detail="job store broken")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 2
    assert "已關" in out  # 不能把已經關掉的講成沒關
    assert "job 清不掉" in err
    assert "到點可能又把 agent 開回來" in err
    assert "job-auto" not in out  # 沒刪掉的不算「刪掉的 job」


def test_close_keeps_deleting_the_remaining_auto_jobs_after_one_delete_fails(pmo_env):
    """M-1：一個 job 刪不掉時仍要試其餘的；close 照送、報第一個錯誤碼、結束碼 2。"""
    sidecar = DeleteFailsSidecar({"job-auto-1": "job_store_broken"})
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto-1")
    add_auto_job(sidecar, "job-auto-2")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 2  # job 清不掉（和單一 job 失敗時一樣）
    assert sidecar.deleted_jobs == ["job-auto-2"]  # 第二個仍然刪掉了
    assert "job-auto-2" not in sidecar.jobs
    assert "job-auto-1" in sidecar.jobs  # 刪不掉的那個還在（側車仍會重開）
    assert sidecar.closed == [TEAM_PM]  # close 照送
    assert "已關" in out
    assert "[close] 刪掉自動重開 job：job-auto-2" in out
    assert "job 清不掉" in err and "job_store_broken" in err
    assert team_state(pmo_env)["closed"] is True


def test_close_reports_the_first_delete_error_and_still_tries_both_jobs(pmo_env):
    """M-1：兩個都刪不掉時，報的是第一個錯誤碼（不是最後一個），兩個都試過。"""
    sidecar = DeleteFailsSidecar(
        {"job-auto-1": "job_store_broken", "job-auto-2": "job_store_busy"}
    )
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto-1")
    add_auto_job(sidecar, "job-auto-2")
    rc, _out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 2
    assert sidecar.count("delete_job:job-auto-1") == 1
    assert sidecar.count("delete_job:job-auto-2") == 1  # 第二個也試過
    assert "job_store_broken" in err  # 第一個錯誤碼
    assert "job_store_busy" not in err
    assert sidecar.count("close_agent") == 1


def test_close_treats_a_404_from_the_close_call_as_never_open(pmo_env):
    """M-2：GET 時 agent 在、`close_agent` 回 404＝本來就沒開，回 0、記 closed。"""
    error = SidecarError(status=404, code="agent_not_found")
    sidecar = RecordingSidecar(close_error=error)
    deps, _aicomm, _sidecar = ops_deps(sidecar=sidecar)
    sidecar.add_agent(TEAM_PM, state="running")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.count("close_agent") == 1
    assert out.splitlines()[0] == f"[close] {TEAM_PM} 已關（state=exited）"
    state = team_state(pmo_env)
    assert (state["closed"], state["state"]) == (True, "exited")


def test_close_never_says_nothing_to_delete_when_the_jobs_call_or_delete_failed(pmo_env):
    """L-c：讀不到或清不掉 job 時不印「沒有要刪的自動重開 job」（與 stderr 矛盾）。"""
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.errors["list_jobs"] = SidecarError(status=503, code="sidecar_busy")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 0
    assert "沒有要刪的自動重開 job" not in out
    assert "job 讀不到" in err and "sidecar_busy" in err

    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto")
    sidecar.errors["delete_job"] = SidecarError(status=500, detail="job store broken")
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 2
    assert "沒有要刪的自動重開 job" not in out
    assert "job 清不掉" in err


def test_close_without_the_sidecar_port_is_exit_2(pmo_env):
    deps, _aicomm, _sidecar = ops_deps(port=False)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 2
    assert out == ""
    assert "sidecar 埠沒有接上" in err


def test_close_unknown_team_is_exit_1(pmo_env):
    deps, _aicomm, sidecar = ops_deps()
    rc, out, err = run_cli(["close", "no-such-team"], pmo_env, deps=deps)
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err
    assert sidecar.closed == []


def test_close_a_worker_team_is_exit_2(pmo_env):
    write_config(pmo_env, teams=[worker_team()])
    deps, _aicomm, sidecar = ops_deps()
    rc, out, err = run_cli(["close", OTHER_TEAM_KEY], pmo_env, deps=deps)
    assert rc == 2
    assert out == ""
    assert "worker team" in err
    assert sidecar.closed == []


@pytest.mark.parametrize("timeout", ["-1", "nan", "inf"])
def test_close_refuses_a_timeout_that_is_not_a_duration(pmo_env, timeout):
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(["close", TEAM_KEY, "--timeout", timeout], pmo_env, deps=deps)
    assert rc == 1
    assert out == ""
    assert "--timeout" in err
    assert sidecar.closed == []


def test_close_keeps_unknown_team_state_fields_and_warns_on_damage(pmo_env):
    seed_team_state(pmo_env, ticket="demo#45", worker_only_field="keep-me")
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    state = team_state(pmo_env)
    assert state["worker_only_field"] == "keep-me"

    pmo_env.team_file().write_text("{ not json", encoding="utf-8")
    sidecar.add_agent(TEAM_PM)
    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)
    assert rc == 0  # 關掉了；只是 closed 沒記
    assert "已關" in out
    assert "closed 沒記" in err

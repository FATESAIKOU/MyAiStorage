"""#370 修正的 `pmo close`／`pmo status`／sidecar 位址測試（#357）。

從 `tests/unit/test_pmo_ops.py` 分出來（那個檔已接近 1000 行，見它的檔頭）：

- 高-2：自動重開 job 是 `at` 型、`every: null`；清單同時有它與 `every` 型
  poke job 時，`close` 要刪前者、只列後者，`status` 印 `at=…`。
- 中-1：`team.sidecar` 和 `pmo_sidecar` 不同時拒絕（不猜），相同才照常。
- 中-2：`_record_close` 在 `teamstate.state_lock` 內重讀再改，和 ack 的寫入
  交錯時不互相蓋掉。

全部用假 sidecar 與暫存 state（`tests/conftest.py` 的 `isolated_home`）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import threading
from io import StringIO

from pmo import ops, teamstate
from pmo.config import DEFAULT_WAKE_TEXT, parse as parse_config
from pmo.fakes import FakeAicomm, FakeSidecar, FakeSidecars
from pmo.ports import SidecarError
from pmo.runtime import Ctx, Deps

import _pmo_env as E
from _pmo_env import PMO_IDENTITY, TEAM_KEY, TEAM_PM, pmo_env, run_cli, seed_team_state
from test_pmo_ops import (
    AUTO_TEXT,
    NOW,
    PMO_SIDECAR,
    TEAM_SIDECAR,
    add_auto_job,
    ops_deps,
    team_state,
    write_config,
)

# --------------------------------------------------------------------------- #
# 高-2：`at` 型的自動重開 job（`every: null`）
# --------------------------------------------------------------------------- #


def test_close_deletes_the_at_type_auto_job_and_lists_the_every_poke_job(pmo_env):
    """清單同時有 at 型 auto open job 與 every 型 poke job：刪前者、只列後者。"""
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="waiting-quota")
    sidecar.add_job(
        "job-auto-open",
        action="open",
        every=None,
        at="2026-11-03T02:00:00Z",
        text=AUTO_TEXT,
        agent_id=TEAM_PM,
        auto=True,
    )
    sidecar.add_job("job-poke", every="10m", text="看一下", agent_id=TEAM_PM)

    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.deleted_jobs == ["job-auto-open"]  # at 型的 auto job 刪掉了
    assert "job-auto-open" not in sidecar.jobs
    assert "job-poke" in sidecar.jobs  # every 型的 poke job 不刪
    lines = out.splitlines()
    assert "[close] 刪掉自動重開 job：job-auto-open" in lines
    assert any("job-poke action=poke every=600s 固定句=否" in line for line in lines)
    assert not any("job-auto-open action=" in line for line in lines)  # 只列不動的那段不列它
    assert team_state(pmo_env)["closed"] is True


def test_status_prints_at_for_a_one_off_job(pmo_env):
    """`status` 自己排的 job 與瞄準 team PM 的 job，`every` 是 None 時印 `at=‹時刻›`。"""
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    sidecar.add_job(
        "job-own-at",
        action="poke",
        every=None,
        at="2026-11-03T03:00:00Z",
        text=DEFAULT_WAKE_TEXT,
        agent_id=PMO_IDENTITY,
    )
    sidecar.add_job(
        "job-team-at",
        action="poke",
        every=None,
        at="2026-11-03T02:00:00Z",
        text="看一下",
        agent_id=TEAM_PM,
    )

    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert "[status] job job-own-at at=2026-11-03T03:00:00Z" in out
    assert "job-team-at action=poke at=2026-11-03T02:00:00Z 固定句=否" in out


# --------------------------------------------------------------------------- #
# 中-1（#356）：sidecar 埠依位址取得 —— team 的操作用 team.sidecar，
# PMO 自己的 job 用 pmo_sidecar
# --------------------------------------------------------------------------- #


def routed_deps(team_port, pmo_port=None):
    """兩個位址各一台假 sidecar（未指到的位址回 `team_port`）。"""
    by_address = {TEAM_SIDECAR: team_port}
    if pmo_port is not None:
        by_address[PMO_SIDECAR] = pmo_port
    return Deps(
        angareion=E.FakeAngareion(),
        aicomm=FakeAicomm(),
        sidecar_for=FakeSidecars(by_address),
        now=lambda: NOW,
    )


def test_close_operates_the_teams_own_sidecar_not_the_pmos(pmo_env):
    """team 的 sidecar 和 PMO 不同台時，close 打 team 那台（不是 pmo_sidecar）。"""
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    team_port, pmo_port = FakeSidecar(TEAM_SIDECAR), FakeSidecar(PMO_SIDECAR)
    team_port.add_agent(TEAM_PM)
    add_auto_job(team_port, "job-auto")

    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=routed_deps(team_port, pmo_port))

    assert (rc, err) == (0, "")
    assert team_port.closed == [TEAM_PM]
    assert team_port.deleted_jobs == ["job-auto"]
    assert pmo_port.closed == [] and pmo_port.deleted_jobs == []  # 沒打到 PMO 那台


def test_poke_goes_to_the_teams_own_sidecar(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    team_port, pmo_port = FakeSidecar(TEAM_SIDECAR), FakeSidecar(PMO_SIDECAR)
    team_port.add_agent(TEAM_PM)

    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"],
        pmo_env,
        deps=routed_deps(team_port, pmo_port),
        stdin_text="看一下",
    )

    assert (rc, err) == (0, "")
    assert team_port.pokes == [(TEAM_PM, "看一下", False)]
    assert pmo_port.pokes == []


def test_status_reads_teams_on_their_sidecar_and_own_jobs_on_the_pmos(pmo_env):
    """team 的 state 從 team.sidecar 讀；PMO 自己排的 job 從 pmo_sidecar 讀。"""
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    team_port, pmo_port = FakeSidecar(TEAM_SIDECAR), FakeSidecar(PMO_SIDECAR)
    team_port.add_agent(TEAM_PM, state="running", session="ses-1")
    team_port.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    pmo_port.add_job("job-own", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)

    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=routed_deps(team_port, pmo_port))

    assert (rc, err) == (0, "")
    assert f"[status] {TEAM_KEY} pm={TEAM_PM} state=running exit=- session=ses-1" in out
    assert "job-own" in out and "job-team" in out
    assert pmo_port.pokes == [] and team_port.closed == []  # 唯讀


def test_close_works_when_the_team_sidecar_is_the_pmos(pmo_env):
    """同一台時只問一次；`pmo_sidecar` 沒設時 team.sidecar 照常可用。"""
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="running")

    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.closed == [TEAM_PM]
    assert f"[close] {TEAM_PM} 已關（state=exited）" in out


class CountingSidecar(FakeSidecar):
    """記下 `list_jobs` 被呼叫幾次：低-2／低-3 要證明同一台只問一次。"""

    def __init__(self, address: str = TEAM_SIDECAR) -> None:
        super().__init__(address)
        self.job_reads = 0

    def list_jobs(self):
        self.job_reads += 1
        return super().list_jobs()


def shared_sidecar_deps(port):
    """`sidecar_for` 只認正規化後的 team 位址：別的寫法會直接 AssertionError。"""
    return Deps(
        angareion=E.FakeAngareion(),
        aicomm=FakeAicomm(),
        sidecar_for=FakeSidecars({TEAM_SIDECAR: port}),
        now=lambda: NOW,
    )


def test_status_asks_a_shared_sidecar_for_jobs_once(pmo_env):
    """低-3：own jobs 與 aimed jobs 同址時，`GET /jobs` 只呼叫一次（§3.1）。"""
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR})
    port = CountingSidecar()
    port.add_agent(TEAM_PM, state="running")
    port.add_job("job-own", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)
    port.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)
    deps = Deps(
        angareion=E.FakeAngareion(),
        aicomm=FakeAicomm(),
        sidecar_for=lambda _address: port,
        now=lambda: NOW,
    )

    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert port.job_reads == 1
    assert "job-own" in out and "job-team" in out


def test_status_treats_a_trailing_slash_as_the_same_sidecar(pmo_env):
    """低-2（#371 遺留低項）：`http://h:8471/` 與 `http://h:8471` 是同一台。

    位址進埠查找與 job 快取前先正規化：兩邊只差一個結尾斜線時，`GET /jobs`
    仍然只問一次，兩份 job 都照常列出來。
    """
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR + "/"})
    port = CountingSidecar()
    port.add_agent(TEAM_PM, state="running")
    port.add_job("job-own", every="10m", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)
    port.add_job("job-team", every="10m", text="看一下", agent_id=TEAM_PM)

    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=shared_sidecar_deps(port))

    assert (rc, err) == (0, "")
    assert port.job_reads == 1
    assert "job-own" in out and "job-team" in out


def test_status_reports_a_shared_failing_sidecar_once_with_a_trailing_slash(pmo_env):
    """低-2：同一台失敗只算一條失敗（不是兩條），而且仍然只問一次。"""
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR + "/"})
    port = CountingSidecar()
    port.add_agent(TEAM_PM, state="running")
    port.errors["list_jobs"] = SidecarError(detail="sidecar 連不上（URLError）")

    rc, out, err = run_cli(["status", TEAM_KEY], pmo_env, deps=shared_sidecar_deps(port))

    assert rc == 2
    assert port.job_reads == 1
    assert err.count("[warn] jobs：連不上") == 1
    assert "的 job：" not in err  # 同一台不重複報第二次


# --------------------------------------------------------------------------- #
# 中-2：`_record_close` 在 team 的鎖內重讀再改
# --------------------------------------------------------------------------- #


def _ctx(env, deps) -> Ctx:
    """一個直接呼叫 `ops._record_close` 的 ctx（假時鐘與暫存 state）。"""
    return Ctx(
        deps=deps,
        config=parse_config(E.config_data()),
        state_dir=env.state_dir,
        out=StringIO(),
        err=StringIO(),
        inp=StringIO(),
    )


def test_close_records_the_state_under_the_team_lock(pmo_env, monkeypatch):
    """`close` 寫 `closed` 與 ack 的寫入交錯時不互相蓋掉（#369 中-1 的形狀）。

    重現窗口：close 已讀、還沒寫時，另一個寫入者（`inbox read` 的 ack）拿同一
    把 `teamstate.state_lock` 讀改寫。鎖生效時 ack 要等 close 寫完、再讀到
    `closed`；沒有鎖時 ack 先寫、close 再用舊的副本蓋掉它。
    """
    seed_team_state(pmo_env, ticket="demo#45")
    deps, _aicomm, _sidecar = ops_deps()
    ctx = _ctx(pmo_env, deps)
    team = ctx.config.team(TEAM_KEY)
    assert team is not None

    started = threading.Event()
    release = threading.Event()
    other_done = threading.Event()
    real_save = teamstate.save_state
    first_save = {"pending": True}

    def slow_save(state_dir, key, data):
        # 讓 close 停在「已讀、還沒寫」的窗口
        if first_save["pending"]:
            first_save["pending"] = False
            started.set()
            release.wait(5.0)
        return real_save(state_dir, key, data)

    monkeypatch.setattr(teamstate, "save_state", slow_save)

    def close_writer():
        ops._record_close(ctx, team, state="exited")

    def ack_writer():
        with teamstate.state_lock(pmo_env.state_dir, TEAM_KEY):
            st = teamstate.load_state(pmo_env.state_dir, TEAM_KEY)
            st["ack_at"] = "2026-10-19T10:16:00Z"
            teamstate.save_state(pmo_env.state_dir, TEAM_KEY, st)
        other_done.set()

    closer = threading.Thread(target=close_writer)
    closer.start()
    assert started.wait(5.0)  # close 已讀、卡在寫之前
    acker = threading.Thread(target=ack_writer)
    acker.start()
    try:
        waited = other_done.wait(0.3)  # 有鎖時 ack 還拿不到鎖，這裡會逾時
    finally:
        release.set()
    closer.join(5.0)
    acker.join(5.0)

    assert waited is False
    assert not closer.is_alive() and not acker.is_alive()
    state = team_state(pmo_env)
    assert state["closed"] is True  # close 的 write 沒被蓋掉
    assert state["ack_at"] == "2026-10-19T10:16:00Z"  # ack 的 write 也沒被蓋掉

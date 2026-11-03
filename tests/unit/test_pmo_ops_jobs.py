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
from pmo.runtime import Ctx

import _pmo_env as E
from _pmo_env import PMO_IDENTITY, TEAM_KEY, TEAM_PM, pmo_env, run_cli, seed_team_state
from test_pmo_ops import (
    AUTO_TEXT,
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
# 中-1：team 的 sidecar 不是 PMO 這台時拒絕（第一階段只支援同一台）
# --------------------------------------------------------------------------- #


def test_close_refuses_a_team_whose_sidecar_is_not_the_pmos(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)
    add_auto_job(sidecar, "job-auto")

    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert rc == 2
    assert out == ""
    assert "不是 PMO 這台" in err
    # 拒絕在動任何東西之前：不 poke、不 close、不刪 job
    assert sidecar.pokes == []
    assert sidecar.closed == []
    assert sidecar.deleted_jobs == []


def test_poke_refuses_a_team_whose_sidecar_is_not_the_pmos(pmo_env):
    write_config(pmo_env, top={"pmo_sidecar": PMO_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM)

    rc, out, err = run_cli(
        ["poke", TEAM_KEY, "--body", "-"], pmo_env, deps=deps, stdin_text="看一下"
    )

    assert rc == 2
    assert out == ""
    assert "不是 PMO 這台" in err
    assert sidecar.pokes == []


def test_close_works_when_the_team_sidecar_is_the_pmos(pmo_env):
    """同一台就照常；`pmo_sidecar` 沒設時也照常（既有 close 測試都是那個形狀）。"""
    write_config(pmo_env, top={"pmo_sidecar": TEAM_SIDECAR})
    deps, _aicomm, sidecar = ops_deps()
    sidecar.add_agent(TEAM_PM, state="running")

    rc, out, err = run_cli(["close", TEAM_KEY], pmo_env, deps=deps)

    assert (rc, err) == (0, "")
    assert sidecar.closed == [TEAM_PM]
    assert f"[close] {TEAM_PM} 已關（state=exited）" in out


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

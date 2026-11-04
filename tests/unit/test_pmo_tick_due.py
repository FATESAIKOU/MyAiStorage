"""`pmo tick` 的看門狗與 worker team 交接（#358 T5-2 ③；design §3.2.1、§3.4）。

驗收（工單 #358 T5-2、架構師裁定第 2、3、9 條）：
- 10 分鐘沒有「收到」→ 先在 team 鎖內記 `ack_poke_at`，再在鎖外 poke 一次；
  poke 的句子照裁定 §3 原樣（`dispatched_at` 當 `--since`、`interrupt` false）；
- 有 `ack_at` 不 poke；已 poke 不再 poke；再 10 分鐘升級成 `[要本人處理]`，
  已升級不再印；很久沒叫醒也只各一次（不補跑）；
- poke 送不到 → stdout 的 `[要本人處理]`，`ack_poke_at` 仍記著（第 3 條）；
- 兩個 tick 並行只 poke 一次（第 9 條：先佔再 poke）；
- worker team 不 poke，只印 `[worker-action] ‹team› watchdog-poke`；
- worker team 的 `[worker-action] poll` 到期印一次並把 `next_due_at` 往後推。

全部用假 sidecar 與暫存 state；不連真的 sidecar。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import threading
from datetime import timedelta

from pmo import config as config_module
from pmo import tick as tick_module
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.ports import SidecarError
from pmo.runtime import Ctx, Deps

from _pmo_env import (
    NOW,
    PMO_IDENTITY,
    TEAM_KEY,
    TEAM_PM,
    PmoEnv,
    config_data,
    fixed_now,
    pmo_env,
    read_team_state,
    read_tick_state,
    run_cli,
    seed_team_state,
    seed_tick_state,
)

TICKET = "demo#45"
DISPATCHED_AT = "2026-10-19T10:03:00Z"
RECEIVED_AT = "2026-10-19T10:04:00Z"

#: 裁定 §3 的 poke 句子（原樣；不從程式匯入，逐字核對）。
POKE_TEXT = (
    "你還沒回「收到 demo#45」。指示在 Angareion：用 "
    "`angareion read --channel team-demo2-m1 --since 2026-10-19T10:03:00Z --json` 讀；"
    "讀到先回一則「收到 demo#45」。"
)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def make_deps(sidecar: FakeSidecar | None = None, *, ang=None, now=NOW):
    sidecar = sidecar if sidecar is not None else FakeSidecar()
    deps = Deps(
        angareion=ang if ang is not None else FakeAngareion(),
        sidecar_for=FakeSidecars({}, default=sidecar),
        now=fixed_now(now),
    )
    return deps, sidecar


def run_tick(env: PmoEnv, deps: Deps):
    return run_cli(["tick"], env, deps=deps)


def seed_dispatch(env: PmoEnv, *, key: str = TEAM_KEY, **extra) -> None:
    """一份「派工了、還沒回收到」的 team 狀態（`received_at` 是權威起算點）。"""
    fields: dict = {
        "ticket": TICKET,
        "ack_at": None,
        "state": "running",
        "dispatched_at": DISPATCHED_AT,
        "received_at": RECEIVED_AT,
    }
    fields.update(extra)
    if key == TEAM_KEY:
        seed_team_state(env, **fields)
    else:
        env.state_dir.mkdir(parents=True, exist_ok=True)
        data = {"agent": key, "closed": False, "dispatched_at": DISPATCHED_AT}
        data.update(fields)
        (env.state_dir / f"{key}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


def make_worker_team(env: PmoEnv) -> None:
    data = config_data()
    data["teams"][0]["sidecar"] = "worker"
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def ctx_for(env: PmoEnv, deps: Deps, out: io.StringIO, err: io.StringIO) -> Ctx:
    return Ctx(
        deps=deps,
        config=config_module.load(env.config_path),
        state_dir=env.state_dir,
        out=out,
        err=err,
        inp=io.StringIO(),
    )


# --------------------------------------------------------------------------- #
# ③ 看門狗：poke 一次
# --------------------------------------------------------------------------- #


def test_the_watchdog_pokes_once_after_ten_minutes(pmo_env):
    """派工 10 分鐘沒有「收到」：記 `ack_poke_at`、poke 一次，句子原樣。"""
    seed_dispatch(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[watchdog] {TEAM_PM} 派工 10 分鐘沒有「收到」→ poke 一次（200）\n"
    assert sidecar.pokes == [(TEAM_PM, POKE_TEXT, False)]
    assert read_team_state(pmo_env)["ack_poke_at"] == "2026-10-19T10:15:00Z"


def test_the_watchdog_counts_from_received_at_not_dispatched_at(pmo_env):
    """第 2 條：`received_at` 優先；`dispatched_at` 在 deadline 另一側時仍要 poke。"""
    # NOW 是 10:15：dispatched_at 只過了 9 分鐘（未到期），received_at 過了 11 分鐘。
    seed_dispatch(
        pmo_env,
        dispatched_at="2026-10-19T10:06:00Z",
        received_at="2026-10-19T10:04:00Z",
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[watchdog] {TEAM_PM} 派工 10 分鐘沒有「收到」→ poke 一次（200）\n"
    assert len(sidecar.pokes) == 1
    assert read_team_state(pmo_env)["ack_poke_at"] == "2026-10-19T10:15:00Z"


def test_an_ack_means_no_poke(pmo_env):
    """「收到」之後看門狗不再發（有 `ack_at`）。"""
    seed_dispatch(pmo_env, ack_at="2026-10-19T10:05:00Z")
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert sidecar.pokes == []


def test_a_second_tick_does_not_poke_again(pmo_env):
    """`ack_poke_at` 已寫：同一輪之後不再 poke；下一次到期時刻進了 next-due。"""
    seed_dispatch(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert len(sidecar.pokes) == 1

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert len(sidecar.pokes) == 1
    assert out == "[idle] [next-due=2026-10-19T10:25:00Z]\n"


def test_it_escalates_ten_minutes_after_the_poke(pmo_env):
    """poke 後再 10 分鐘仍沒有「收到」→ `[要本人處理]`，附 `GET /agents` 的 state。"""
    seed_dispatch(pmo_env, ack_poke_at="2026-10-19T10:04:00Z")
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == (
        f"[要本人處理] {TEAM_PM} poke 後 10 分鐘仍沒有回「收到 {TICKET}」；"
        f"GET /agents/{TEAM_PM}：state=running\n"
    )
    assert sidecar.pokes == []  # 升級不再 poke
    assert read_team_state(pmo_env)["ack_escalated_at"] == "2026-10-19T10:15:00Z"


def test_the_escalation_is_printed_once(pmo_env):
    """已升級（`ack_escalated_at` 有值）就不再多印。"""
    seed_dispatch(
        pmo_env,
        ack_poke_at="2026-10-19T10:04:00Z",
        ack_escalated_at="2026-10-19T10:14:00Z",
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, _sidecar = make_deps(sidecar)
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_a_long_sleep_pokes_and_escalates_at_most_once_each(pmo_env):
    """很久沒叫醒（一小時）：poke 一次、升級一次，之後就安靜（第 2、9 條）。"""
    seed_dispatch(
        pmo_env,
        dispatched_at="2026-10-19T09:03:00Z",
        received_at="2026-10-19T09:04:00Z",
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert rc == 0
    assert len(sidecar.pokes) == 1
    assert "[watchdog]" in out and "[要本人處理]" not in out

    later, _sidecar = make_deps(sidecar, now=NOW + timedelta(minutes=15))
    rc, out, err = run_tick(pmo_env, later)
    assert rc == 0
    assert out.count("[要本人處理]") == 1
    assert "[watchdog]" not in out
    assert len(sidecar.pokes) == 1

    rc, out, err = run_tick(pmo_env, later)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert len(sidecar.pokes) == 1


def test_a_poke_that_cannot_be_sent_needs_the_person_but_is_still_recorded(pmo_env):
    """poke 送不到：`[要本人處理]`（stdout），`ack_poke_at` 仍寫（第 3 條）。"""
    seed_dispatch(pmo_env, state="absent")
    sidecar = FakeSidecar()
    sidecar.errors["poke"] = SidecarError(status=404, code="agent_not_running")
    deps, sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert rc == 0
    assert f"[要本人處理] {TEAM_PM} 的看門狗 poke 送不到" in out
    assert read_team_state(pmo_env)["ack_poke_at"] == "2026-10-19T10:15:00Z"


def test_two_ticks_running_together_poke_only_once(pmo_env, monkeypatch):
    """第 9 條：兩個 tick 並行時只有一個先佔到 `ack_poke_at`，只 poke 一次。"""
    monkeypatch.setenv("ANGAREION_IDENTITY", PMO_IDENTITY)
    seed_dispatch(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    outputs: list[tuple[str, str]] = []

    def one_round() -> None:
        out, err = io.StringIO(), io.StringIO()
        ctx = ctx_for(pmo_env, deps, out, err)
        barrier.wait()
        try:
            tick_module.tick(ctx)
        except BaseException as error:  # noqa: BLE001 - the test reports it
            errors.append(error)
        finally:
            outputs.append((out.getvalue(), err.getvalue()))

    threads = [threading.Thread(target=one_round) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(sidecar.pokes) == 1
    assert sum("[watchdog]" in out for out, _err in outputs) == 1


def test_two_ticks_escalate_only_once(pmo_env, monkeypatch):
    """第 9 條：`ack_escalated_at` 也是先佔再印，兩個 tick 並行只升級一次。"""
    monkeypatch.setenv("ANGAREION_IDENTITY", PMO_IDENTITY)
    seed_dispatch(pmo_env, ack_poke_at="2026-10-19T10:04:00Z")
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar = make_deps(sidecar)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    outputs: list[tuple[str, str]] = []

    def one_round() -> None:
        out, err = io.StringIO(), io.StringIO()
        ctx = ctx_for(pmo_env, deps, out, err)
        barrier.wait()
        try:
            tick_module.tick(ctx)
        except BaseException as error:  # noqa: BLE001 - the test reports it
            errors.append(error)
        finally:
            outputs.append((out.getvalue(), err.getvalue()))

    threads = [threading.Thread(target=one_round) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sidecar.pokes == []
    assert sum("[要本人處理]" in out for out, _err in outputs) == 1
    assert read_team_state(pmo_env)["ack_escalated_at"] == "2026-10-19T10:15:00Z"


def test_a_worker_team_gets_the_watchdog_handoff_and_no_poke(pmo_env):
    """worker team：不 poke，只印 `[worker-action] … watchdog-poke`（D-8）。"""
    make_worker_team(pmo_env)
    seed_dispatch(pmo_env)
    # worker poll 先擺到未來，這一支只驗看門狗那一段。
    seed_tick_state(
        pmo_env, worker_poll={TEAM_KEY: {"next_due_at": "2026-10-19T10:20:00Z"}}
    )
    deps, sidecar = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[worker-action] {TEAM_KEY} watchdog-poke\n"
    assert sidecar.pokes == []
    assert sidecar.agent_reads == []
    assert read_team_state(pmo_env)["ack_poke_at"] == "2026-10-19T10:15:00Z"


# --------------------------------------------------------------------------- #
# ③ worker team：`[worker-action] poll`
# --------------------------------------------------------------------------- #


def test_the_worker_poll_prints_once_and_pushes_the_next_due(pmo_env):
    """到期印一次並把 `next_due_at` 往後推；下一輪還沒到期就不印。"""
    make_worker_team(pmo_env)
    seed_dispatch(pmo_env, ack_at="2026-10-19T10:05:00Z")
    deps, _sidecar = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[worker-action] {TEAM_KEY} poll\n"
    entry = read_tick_state(pmo_env)["worker_poll"][TEAM_KEY]
    assert entry["next_due_at"] == "2026-10-19T10:25:00Z"
    assert entry["last_reported_at"] == "2026-10-19T10:15:00Z"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:25:00Z]\n"


def test_the_worker_poll_not_due_prints_nothing(pmo_env):
    """`next_due_at` 還沒到：不印，只把它放進 next-due。"""
    make_worker_team(pmo_env)
    seed_dispatch(pmo_env, ack_at="2026-10-19T10:05:00Z")
    seed_tick_state(
        pmo_env, worker_poll={TEAM_KEY: {"next_due_at": "2026-10-19T10:20:00Z"}}
    )
    deps, _sidecar = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:20:00Z]\n"


def test_the_next_due_is_the_earliest_of_the_watchdog_handoff_and_poll(pmo_env):
    """`next-due` 取三者最早（交接 10:17 比 worker poll 10:20 早）。"""
    seed_dispatch(pmo_env, ack_at="2026-10-19T10:05:00Z")
    seed_tick_state(
        pmo_env,
        handoffs={
            "01JGROUP": {
                "handed_at": "2026-10-19T10:00:00Z",
                "next_due_at": "2026-10-19T10:17:00Z",
            }
        },
        worker_poll={TEAM_KEY: {"next_due_at": "2026-10-19T10:20:00Z"}},
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, _sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:17:00Z]\n"

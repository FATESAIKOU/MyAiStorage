"""`pmo tick` 的 `tick.json`：`last_tick_at`、3 倍間隔警告與鎖（#358 T5-2 ④）。

驗收（工單 #358 T5-2、design §3.4、架構師裁定第 4 條與 S-3）：
- 每輪都寫 `last_tick_at`；`wake.every` 非 null 且「現在 − last_tick_at」超過
  3 倍間隔 → `[warn] 上次 tick 在 …`；`every` 是 null 不檢查；
- `tick.json` 不存在 → 建立、`wake.every` 視為 null；
- `tick.json.lock` **不在 HTTP 期間被拿著**（假 sidecar 卡在 `get_agent` 時，
  另一個執行緒拿得到鎖）；
- 鎖內重讀：HTTP 期間 `wake.every` 被改掉，commit 之後仍是新值（不蓋掉）；
- 等不到鎖 → `[warn]`＋exit 2（裁定 S-3）；
- 壞 `tick.json` → exit 2、`[要本人處理]`（`test_pmo_tick.py` 另有一支驗
  `angareion.read` 沒被呼叫）。

全部用假 sidecar 與暫存 state；鎖是真的 flock，但只鎖暫存目錄裡的檔案。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import threading

import pytest

from pmo import _lock
from pmo import teamstate
from pmo import tick as tick_module
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.runtime import Deps

from _pmo_env import (
    NOW,
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


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def make_deps(sidecar: FakeSidecar | None = None, *, now=NOW):
    sidecar = sidecar if sidecar is not None else FakeSidecar()
    deps = Deps(
        angareion=FakeAngareion(),
        sidecar_for=FakeSidecars({}, default=sidecar),
        now=fixed_now(now),
    )
    return deps, sidecar


def run_tick(env: PmoEnv, deps: Deps):
    return run_cli(["tick"], env, deps=deps)


def lock_path(env: PmoEnv):
    return env.state_dir / "tick.json.lock"


# --------------------------------------------------------------------------- #
# ④ last_tick_at 與 3 倍間隔
# --------------------------------------------------------------------------- #


def test_last_tick_at_is_written_every_round(pmo_env):
    """每輪結束都寫 `last_tick_at`（這一輪的 now）。"""
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert read_tick_state(pmo_env)["last_tick_at"] == "2026-10-19T10:15:00Z"


def test_a_stale_tick_warns(pmo_env):
    """`wake.every=10m`、上次 tick 在 31 分鐘前（>3 倍）→ `[warn]`（stderr）。"""
    seed_tick_state(
        pmo_env,
        last_tick_at="2026-10-19T09:44:00Z",
        wake={"every": "10m", "set_at": "2026-10-19T09:00:00Z"},
    )
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out) == (0, "[idle]\n")
    assert err == "[warn] 上次 tick 在 2026-10-19T09:44:00Z\n"


def test_a_tick_inside_three_intervals_does_not_warn(pmo_env):
    """29 分鐘前（<3×10m）不印。"""
    seed_tick_state(
        pmo_env,
        last_tick_at="2026-10-19T09:46:00Z",
        wake={"every": "10m", "set_at": "2026-10-19T09:00:00Z"},
    )
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_every_null_skips_the_stale_check(pmo_env):
    """PMO 自己關掉叫醒（`every` 是 null）：不做 3 倍間隔檢查。"""
    seed_tick_state(pmo_env, last_tick_at="2026-10-19T08:00:00Z")
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_no_last_tick_at_does_not_warn(pmo_env):
    """沒有 `last_tick_at`：不印（第一次跑，沒有可比的上次）。"""
    seed_tick_state(pmo_env, wake={"every": "10m", "set_at": "2026-10-19T09:00:00Z"})
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_a_missing_tick_json_is_created_with_every_null(pmo_env):
    """`tick.json` 不存在 → 建立，`wake.every` 視為 null，不印 `[wake-missing]`。"""
    assert not (pmo_env.state_dir / "tick.json").exists()
    deps, _sidecar = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    state = read_tick_state(pmo_env)
    assert state["wake"]["every"] is None
    assert state["last_tick_at"] == "2026-10-19T10:15:00Z"


# --------------------------------------------------------------------------- #
# 鎖：不在 HTTP 期間持有、鎖內重讀、等不到就是 exit 2
# --------------------------------------------------------------------------- #


def test_the_tick_lock_is_not_held_while_http_is_out(pmo_env):
    """假 sidecar 在 `get_agent` 時，另一個執行緒拿得到 `tick.json.lock`。"""
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    got: list[bool] = []

    def probe() -> bool:
        try:
            with _lock.path_lock(lock_path(pmo_env), timeout=0.5):
                return True
        except _lock.LockTimeout:
            return False

    def hook(method: str) -> None:
        if method != "get_agent":
            return
        thread = threading.Thread(target=lambda: got.append(probe()))
        thread.start()
        thread.join(timeout=10)

    sidecar.on_call = hook
    deps, _sidecar = make_deps(sidecar)
    rc, _out, _err = run_tick(pmo_env, deps)
    assert rc == 0
    assert got == [True]


def test_the_commit_re_reads_tick_json_inside_the_lock(pmo_env):
    """HTTP 期間 `wake.every` 被改成 `2h`：commit 之後仍是 `2h`（不蓋掉）。"""
    seed_tick_state(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")

    def hook(method: str) -> None:
        if method == "get_agent":
            state = read_tick_state(pmo_env)
            state["wake"]["every"] = "2h"
            state["wake"]["set_at"] = "2026-10-19T10:10:00Z"
            (pmo_env.state_dir / "tick.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )

    sidecar.on_call = hook
    deps, _sidecar = make_deps(sidecar)
    rc, _out, _err = run_tick(pmo_env, deps)
    assert rc == 0
    after = read_tick_state(pmo_env)
    assert after["wake"]["every"] == "2h"
    assert after["last_tick_at"] == "2026-10-19T10:15:00Z"


def test_a_lock_timeout_is_a_warn_and_exit_two(pmo_env, monkeypatch):
    """裁定 S-3：等不到 `tick.json.lock` → `[warn]`＋exit 2，`tick.json` 沒寫。"""
    seed_tick_state(pmo_env, last_tick_at="2026-10-19T09:00:00Z")
    monkeypatch.setattr(tick_module, "LOCK_TIMEOUT_SECONDS", 0.2)

    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with _lock.path_lock(lock_path(pmo_env)):
            ready.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(10)
        deps, _sidecar = make_deps()
        rc, out, err = run_tick(pmo_env, deps)
    finally:
        release.set()
        thread.join(timeout=10)

    assert (rc, out) == (2, "")
    assert err == (
        "[warn] tick.json 的鎖等不到（10 秒），這一輪的 tick.json 沒寫，下一輪再寫\n"
    )
    assert read_tick_state(pmo_env)["last_tick_at"] == "2026-10-19T09:00:00Z"


# --------------------------------------------------------------------------- #
# team 鎖逾時（#358 T5-3；架構師 2026-11-04）
# --------------------------------------------------------------------------- #

SECOND_SIDECAR = "http://127.0.0.1:8473"
SECOND_KEY = "demo3-m1"
SECOND_PM = "demo3-pm-m1"


def add_second_team(env: PmoEnv) -> None:
    """設定裡再加一個本機 team（`demo3-m1`），另一台假 sidecar。"""
    data = config_data()
    data["teams"].append(
        {
            "key": SECOND_KEY,
            "pm": SECOND_PM,
            "channel": f"team-{SECOND_KEY}",
            "sidecar": SECOND_SIDECAR,
            "tui": False,
            "life": False,
            "retired": False,
        }
    )
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def seed_second_state(env: PmoEnv) -> None:
    """第二個 team 的狀態檔：已派工、沒有 `state`（這一輪會變化）。"""
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / f"{SECOND_KEY}.json").write_text(
        json.dumps(
            {
                "agent": SECOND_PM,
                "ticket": TICKET,
                "closed": False,
                "ack_at": "2026-10-19T10:01:00Z",
                "session": "s1",
                "dispatched_at": "2026-10-19T10:00:00Z",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_the_team_lock_wait_is_ten_seconds():
    """tick 拿 team 鎖的預設上限是 10 秒（設計 §3.4）。"""
    assert tick_module.TEAM_LOCK_TIMEOUT_SECONDS == 10.0


def test_a_team_lock_timeout_warns_and_skips_that_team(pmo_env, monkeypatch):
    """一個 team 的鎖等不到：`[warn]` 跳過它，另一個 team 照做、exit 0。"""
    add_second_team(pmo_env)
    seed_team_state(pmo_env, ticket=TICKET)
    seed_second_state(pmo_env)
    first = FakeSidecar()
    first.add_agent(TEAM_PM, state="running")
    second = FakeSidecar()
    second.add_agent(SECOND_PM, state="running")
    factory = FakeSidecars(
        {"http://127.0.0.1:8472": first, SECOND_SIDECAR: second}
    )
    deps = Deps(angareion=FakeAngareion(), sidecar_for=factory, now=fixed_now(NOW))
    monkeypatch.setattr(tick_module, "TEAM_LOCK_TIMEOUT_SECONDS", 0.2)

    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with teamstate.state_lock(pmo_env.state_dir, TEAM_KEY):
            ready.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(10)
        rc, out, err = run_tick(pmo_env, deps)
    finally:
        release.set()
        thread.join(timeout=10)

    assert rc == 0
    assert out == f"[state] {SECOND_KEY} -→running\n"
    assert err == (
        f"[warn] {TEAM_KEY} 的狀態鎖等不到（0.2 秒），這一輪跳過它\n"
    )
    # 被跳過的 team 沒有被寫回（下一輪再比對）。
    assert "state" not in read_team_state(pmo_env)


def test_state_lock_timeout_raises_when_another_holder_has_it(pmo_env):
    """`teamstate.state_lock(..., timeout=)` 等不到就丟 `LockTimeout`（T5-3 的參數）。"""
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with teamstate.state_lock(pmo_env.state_dir, TEAM_KEY):
            ready.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(10)
        with pytest.raises(_lock.LockTimeout):
            with teamstate.state_lock(
                pmo_env.state_dir, TEAM_KEY, timeout=0.05
            ):
                pass
    finally:
        release.set()
        thread.join(timeout=10)

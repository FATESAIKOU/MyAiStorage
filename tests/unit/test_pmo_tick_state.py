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

from pmo import _lock
from pmo import tick as tick_module
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.runtime import Deps

from _pmo_env import (
    NOW,
    TEAM_PM,
    PmoEnv,
    fixed_now,
    pmo_env,
    read_tick_state,
    run_cli,
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

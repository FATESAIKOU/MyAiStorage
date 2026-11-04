"""`pmo wake set|off|show` 與 tick 的 job 健康（#358 T5-4；design §3.6、§3.4）。

驗收（工單 #358 T5-4、設計 §5.2 第 7 點的 wake 相關清單）：

- `wake set` 的同間隔不動 sidecar（假 sidecar 的 `GET /jobs` 回**秒數** `600.0`，
  字串形狀也收）；改間隔**先建後刪**；只動「自己的」job（句子不同、`auto` 的
  都不碰）；`args.id` 換過時重排；刪除失敗只印 `[warn]`、exit 仍是 0；
- 兩個 `set` 同時跑只留一個 job（`wake.lock`）；等不到 `wake.lock` 回 exit 2；
  `off` 和卡在 `create_job` 的 `set` 同時跑時排在後面；`create_job` 失敗時
  exit 2、舊 job 還在、`tick.json` 沒寫；`set`／`off` 清掉兩個「已回報」印記；
- `POST /jobs` 回 2xx 但 body 讀不懂時靠 `GET /jobs` 找回剛建的 job、照常完成；
  找不到（或 `GET /jobs` 也失敗）時不刪、不寫、`[warn]` 與 exit 2；
- `wake off` 之後 tick 不印 `[wake-missing]`；`off` 不刪句子不同的 job；
- `wake show` 只列不動句子不同的 poke job（且只列 `args.id` 是 PMO 的）；
- `wake show` 的最早到期含看門狗，與 tick 的 `[idle] [next-due=…]` 同一個答案；
  沒有 team 狀態時只算 `tick.json`；`wake` 不 import `tick`（`pmo.due` 共用）；
- `[wake-missing]` 只印一次，job 回來後印記清掉；
- `[wake-error]` 依碼去重（失敗期間 `last_run` 前進也不重印）、碼改變再印、
  `last_error` 變回 null 後清記錄、同一個碼再出現要再印；
- 自己的 job 超過一個的 `[warn]`；`wake.every` 是 null 卻有 own job 的 `[warn]`
  只印一次（`orphan_reported_at`，job 消失後清掉）；
- `tick.json.lock` 不在 HTTP 期間被拿著（tick 卡在 `GET /jobs` 時鎖拿得到），
  tick 也不等 `wake.lock`；
- 鎖內重讀：tick 卡在 HTTP 時跑的 `wake set` 不會被 tick 的 commit 蓋掉；
- PMO 不在 sidecar（沒設 `pmo_sidecar`）時 tick 不查 `/jobs`、`wake` 回 2；
- `wake.every` 是 null 時不做「3 倍間隔」警告（T5-2 的
  `test_every_null_skips_the_stale_check` 已守住；這裡再加一次 null ＋ own job
  的一輪，確認那一輪只有孤兒 `[warn]`）。

全部用假 sidecar／假 angareion 與暫存 state（`tests/conftest.py` 的
`isolated_home`），不連真的 sidecar 與通道。

`pmo wake show` 與退路（recover）的測試在 `test_pmo_wake_show.py`／
`test_pmo_wake_recover.py`（拆檔，斷言原樣；第二輪審查 nit-3）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import threading
from io import StringIO

import pytest

from pmo import _lock, cli, wake
from pmo.config import DEFAULT_WAKE_TEXT
from pmo.fakes import FakeSidecar
from pmo.ports import SidecarError

from _pmo_env import (
    OTHER_TEXT,
    PMO_IDENTITY,
    TEAM_KEY,
    TEAM_PM,
    TICKET,
    WAKE_JOB,
    PmoEnv,
    make_deps,
    own_jobs,
    pmo_env,
    put_job,
    read_tick_state,
    record_calls,
    run_tick,
    run_wake,
    seed_team_state,
    seed_tick_state,
    set_config,
)


# --------------------------------------------------------------------------- #
# A. `pmo wake set`：驗證、同間隔不動、先建後刪、只動自己的
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("written", "shaped"), [("10m", 600.0), ("10m", "10m"), ("2h", "2h")])
def test_set_leaves_the_sidecar_alone_for_the_same_interval(pmo_env, written, shaped):
    """同間隔、`args.id` 是 PMO：不動 sidecar，只更新 `tick.json`（M5-1）。

    `GET /jobs` 在真的 sidecar 回**秒數**（`600.0`），字串形狀也要收。
    """
    set_config(pmo_env)
    deps, sidecar = make_deps()
    put_job(sidecar, every=shaped)
    calls = record_calls(sidecar)

    rc, out, err = run_wake(pmo_env, deps, "set", written)

    assert (rc, err) == (0, "")
    assert "create_job" not in calls and "delete_job" not in calls
    assert list(sidecar.jobs) == [WAKE_JOB]
    assert read_tick_state(pmo_env)["wake"]["every"] == written
    assert out == f"[wake] every={written} job={WAKE_JOB} next=-\n"


def test_set_changes_the_interval_by_creating_before_deleting(pmo_env):
    """改間隔：先 `POST /jobs`，成功後才 `DELETE` 舊的（中間沒有空窗）。"""
    set_config(pmo_env)
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)  # 5m
    calls = record_calls(sidecar)

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert calls.index("create_job") < calls.index("delete_job")
    assert sidecar.deleted_jobs == [WAKE_JOB]
    (job,) = own_jobs(sidecar)
    assert (job.job_id, job.every) == ("job-1", 600.0)
    assert job.args == {"id": PMO_IDENTITY, "text": DEFAULT_WAKE_TEXT, "interrupt": False}
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"
    assert out == "[wake] every=10m job=job-1 next=-\n"


def test_set_does_not_touch_other_sentences_or_auto_jobs(pmo_env):
    """句子不同的 poke job（別人排的）與 `auto` 的 job 都不是自己的：不刪。"""
    set_config(pmo_env)
    deps, sidecar = make_deps()
    put_job(sidecar, "job-other", every=300.0, text=OTHER_TEXT)
    put_job(sidecar, "job-auto", every=360.0, auto=True)

    rc, _out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert sidecar.deleted_jobs == []
    assert {"job-other", "job-auto"} <= set(sidecar.jobs)
    (job,) = own_jobs(sidecar)
    assert job.job_id == "job-1"


def test_set_recreates_a_job_left_under_another_pmo_id(pmo_env):
    """同間隔但 `args.id` 不是目前的 PMO：建新的再刪舊的（換 id 的清理）。"""
    set_config(pmo_env)
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0, agent_id="demo-pm-m9")

    rc, _out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert sidecar.deleted_jobs == ["job-own"]
    (job,) = own_jobs(sidecar)
    assert job.args["id"] == PMO_IDENTITY


def test_set_reports_a_failed_delete_but_still_exits_zero(pmo_env):
    """刪不掉舊 job：印 `[warn]` 附 job id，exit 仍是 0（新 job 已經在了）。"""
    set_config(pmo_env)
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)
    sidecar.errors["delete_job"] = SidecarError(status=500, code="job_store_broken")

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert rc == 0
    assert out == "[wake] every=10m job=job-1 next=-\n"
    assert err == "[warn] 刪不掉自己的叫醒 job job-own：job_store_broken\n"
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"


def test_a_failed_create_leaves_the_old_job_and_the_interval_alone(pmo_env):
    """`create_job` 失敗：exit 2、舊 job 還在、`tick.json` 沒寫（先建後刪的另一半）。

    先建後刪的前提是「新的建好才刪舊的」：建失敗時舊 job 必須原封不動，否則
    PMO 就沒有叫醒了（設計 §3.6 step 3）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)  # 5m：與 tick.json 記的一致
    sidecar.errors["create_job"] = SidecarError(status=500, code="job_store_broken")

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, out) == (2, "")
    assert err == "[warn] pmo wake 失敗：SidecarError: job_store_broken\n"
    assert sidecar.deleted_jobs == []
    assert list(sidecar.jobs) == [WAKE_JOB]
    wake_state = read_tick_state(pmo_env)["wake"]
    assert (wake_state["every"], wake_state["set_at"]) == (
        "5m",
        "2026-10-19T09:00:00Z",
    )


def test_two_sets_at_the_same_time_leave_exactly_one_job(pmo_env):
    """兩個 `set` 同時跑會排隊（`wake.lock`）：只留一個自己的 job。

    第一個的 `create_job` 先卡住，第二個在這時進來；沒有 `wake.lock` 時第二個
    會看到「還沒有 job」而自己再建一個（留下兩個），有鎖時它排在後面、看到第一
    個建好的那一個就不再建。
    """
    set_config(pmo_env)
    deps, sidecar = make_deps()
    first_create = threading.Event()
    release_first = threading.Event()

    def hook(method: str) -> None:
        if method != "create_job" or first_create.is_set():
            return
        first_create.set()
        release_first.wait(15)

    sidecar.on_call = hook

    def one() -> None:
        cli.main(
            ["wake", "set", "10m"], deps=deps, stdout=StringIO(), stderr=StringIO(),
            stdin=StringIO(),
        )

    first = threading.Thread(target=one)
    second = threading.Thread(target=one)
    first.start()
    try:
        assert first_create.wait(10)  # 第一個已經在 POST /jobs
        second.start()
        # 給第二個一點時間走到鎖（有鎖時它會停在這裡；沒有鎖時它會直接建 job）
        release_first.wait(0.2)
    finally:
        release_first.set()
        first.join(timeout=30)
        second.join(timeout=30)

    assert len(own_jobs(sidecar)) == 1
    assert len(sidecar.jobs) == 1
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"


@pytest.mark.parametrize("written", ["30s", "1m", "25h", "10", "abc", ""])
def test_wake_set_rejects_an_interval_outside_the_grammar(pmo_env, written):
    """只收 `‹整數›m`／`‹整數›h` 且 2m～24h（Q-8 的邊界）：exit 1、不碰 sidecar。"""
    set_config(pmo_env)
    deps, sidecar = make_deps()
    calls = record_calls(sidecar)

    rc, out, err = run_wake(pmo_env, deps, "set", written)

    assert (rc, out) == (1, "")
    assert "叫醒間隔" in err
    assert "list_jobs" not in calls
    assert sidecar.jobs == {}
    assert not (pmo_env.state_dir / "tick.json").exists()


def test_a_damaged_tick_json_is_exit_two_before_any_sidecar_change(pmo_env):
    """壞 `tick.json`：exit 2、`[要本人處理]`，而且一個 job 都沒動（設計 §3.4）。"""
    set_config(pmo_env)
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    (pmo_env.state_dir / "tick.json").write_text("{not json", encoding="utf-8")
    deps, sidecar = make_deps()
    calls = record_calls(sidecar)

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, out) == (2, "")
    assert err.startswith("[要本人處理] ")
    assert calls == []  # 連 GET /jobs 都沒打
    assert sidecar.jobs == {}


# --------------------------------------------------------------------------- #
# A. `pmo wake off`
# --------------------------------------------------------------------------- #


def test_off_deletes_only_the_own_jobs_and_clears_the_interval(pmo_env):
    """`off`：刪掉自己的 job、`wake.every` 設成 null；句子不同的不碰。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0)
    put_job(sidecar, "job-other", every=300.0, text=OTHER_TEXT)

    rc, out, err = run_wake(pmo_env, deps, "off")

    assert (rc, err) == (0, "")
    assert sidecar.deleted_jobs == [WAKE_JOB]
    assert list(sidecar.jobs) == ["job-other"]
    assert read_tick_state(pmo_env)["wake"]["every"] is None
    assert out == "[wake] off\n[wake] 刪掉自己的叫醒 job：job-own\n"


def test_off_with_nothing_to_delete_still_clears_the_interval(pmo_env):
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, _sidecar = make_deps()

    rc, out, err = run_wake(pmo_env, deps, "off")

    assert (rc, err) == (0, "")
    assert out == "[wake] off\n[wake] 沒有自己的叫醒 job\n"
    assert read_tick_state(pmo_env)["wake"]["every"] is None


def test_off_whose_delete_fails_is_exit_two(pmo_env):
    """刪不掉是 sidecar 拒絕：exit 2；`wake.every` 仍是 null（孤兒警告接手）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0)
    sidecar.errors["delete_job"] = SidecarError(status=409, code="job_not_deletable")

    rc, out, err = run_wake(pmo_env, deps, "off")

    assert (rc, out) == (2, "[wake] off\n")
    assert err == "[warn] 刪不掉自己的叫醒 job job-own：job_not_deletable\n"
    assert read_tick_state(pmo_env)["wake"]["every"] is None


@pytest.mark.parametrize("argv", [["set", "10m"], ["off"]])
def test_set_and_off_clear_the_reported_marks(pmo_env, argv):
    """`wake set|off` 清掉 `missing_reported_at`／`orphan_reported_at`。

    這兩個印記只在「同一個消失／孤兒還沒被處理」期間有意義；PMO 自己動了
    叫醒就是那個時刻結束（設計 §3.4、§3.6）。
    """
    set_config(pmo_env)
    seed_tick_state(
        pmo_env,
        wake={
            "every": "10m",
            "missing_reported_at": "2026-10-19T09:00:00Z",
            "orphan_reported_at": "2026-10-19T09:05:00Z",
        },
    )
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0)

    rc, _out, err = run_wake(pmo_env, deps, *argv)

    assert (rc, err) == (0, "")
    wake_state = read_tick_state(pmo_env)["wake"]
    assert wake_state["missing_reported_at"] is None
    assert wake_state["orphan_reported_at"] is None


def test_an_off_queues_behind_a_set_stuck_on_create(pmo_env):
    """`off` 和 `set` 同時跑：`off` 排在 `set` 後面（`wake.lock`），不留下 job。

    第一個 `set` 的 `create_job` 先卡住，`off` 在這時進來。`off` 沒拿鎖時會
    在新 job 出現前就跑完（最後 sidecar 上留下一個沒人記得的 job、`wake.every`
    還寫著 `2h`）；拿鎖時它排在後面，看到建好的那個 job 並把它刪掉。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()
    first_create = threading.Event()
    release_first = threading.Event()

    def hook(method: str) -> None:
        if method != "create_job" or first_create.is_set():
            return
        first_create.set()
        release_first.wait(20)

    sidecar.on_call = hook
    results: list[tuple[str, int]] = []

    def one(argv: list[str]) -> None:
        rc = cli.main(
            argv, deps=deps, stdout=StringIO(), stderr=StringIO(), stdin=StringIO()
        )
        results.append((argv[1], rc))

    setter = threading.Thread(target=one, args=(["wake", "set", "2h"],))
    offer = threading.Thread(target=one, args=(["wake", "off"],))
    setter.start()
    try:
        assert first_create.wait(10)  # 第一個 set 已經在 POST /jobs
        offer.start()
        release_first.wait(0.2)  # 給 off 一點時間走到鎖
    finally:
        release_first.set()
        setter.join(timeout=30)
        offer.join(timeout=30)

    assert sorted(results) == [("off", 0), ("set", 0)]
    assert list(sidecar.jobs) == []  # off 刪掉了那個 job
    assert read_tick_state(pmo_env)["wake"]["every"] is None


def test_off_after_a_missing_job_stops_the_wake_missing_line(pmo_env):
    """`off` 之後 tick 不再印 `[wake-missing]`（every 是 null 就不做健康檢查）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()

    rc, out, _err = run_tick(pmo_env, deps)
    assert out == "[wake-missing] every=10m\n"

    rc, out, err = run_wake(pmo_env, deps, "off")
    assert (rc, err) == (0, "")
    assert read_tick_state(pmo_env)["wake"]["every"] is None

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


@pytest.mark.parametrize("argv", [["set", "10m"], ["off"], ["show"]])
def test_wake_without_a_pmo_sidecar_is_exit_two(pmo_env, argv):
    """沒設 `pmo_sidecar`（PMO 不在 sidecar）：`wake` 回 2，不猜位址。"""
    set_config(pmo_env, pmo_sidecar=None)
    deps, sidecar = make_deps()
    calls = record_calls(sidecar)

    rc, out, err = run_wake(pmo_env, deps, *argv)

    assert (rc, out) == (2, "")
    assert "pmo_sidecar" in err
    assert calls == []


# --------------------------------------------------------------------------- #
# B. tick 的 job 健康
# --------------------------------------------------------------------------- #


def test_wake_missing_prints_once_and_is_cleared_when_the_job_returns(pmo_env):
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[wake-missing] every=10m\n"
    assert read_tick_state(pmo_env)["wake"]["missing_reported_at"] == "2026-10-19T10:15:00Z"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")

    put_job(sidecar, every=600.0)  # PMO 跑了 pmo wake set，job 回來了
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert read_tick_state(pmo_env)["wake"]["missing_reported_at"] is None


def test_wake_error_is_printed_once_per_code(pmo_env):
    """同一碼不重印（`last_run` 前進也一樣）；碼改變再印；null 後同碼再印。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()
    put_job(sidecar, last_error="agent_not_running", last_run="2026-10-19T10:05:00Z")

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[wake-error] job-own agent_not_running\n"
    assert read_tick_state(pmo_env)["wake"]["error_reported"] == {
        "job-own": "agent_not_running"
    }

    # 失敗期間每次到點 last_run 都前進，碼一樣：不重印
    put_job(sidecar, last_error="agent_not_running", last_run="2026-10-19T10:14:00Z")
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")

    # 碼改變：再印一次
    put_job(sidecar, last_error="agent_awaiting_answer", last_run="2026-10-19T10:14:00Z")
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[wake-error] job-own agent_awaiting_answer\n"

    # 成功過一次（last_error 變回 null）：記錄清掉
    put_job(sidecar, last_error=None, last_run="2026-10-19T10:14:00Z")
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert read_tick_state(pmo_env)["wake"]["error_reported"] == {}

    # 同一個碼再出現是一次新的失敗：要再印
    put_job(sidecar, last_error="agent_awaiting_answer", last_run="2026-10-19T10:30:00Z")
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[wake-error] job-own agent_awaiting_answer\n"


def test_a_missing_job_drops_its_reported_code(pmo_env):
    """job 已不在：`wake.error_reported` 的那一筆刪掉（設計 §3.4）。"""
    set_config(pmo_env)
    seed_tick_state(
        pmo_env,
        wake={"every": "10m", "error_reported": {"job-gone": "agent_not_running"}},
    )
    deps, _sidecar = make_deps()

    rc, out, _err = run_tick(pmo_env, deps)

    assert rc == 0
    assert out == "[wake-missing] every=10m\n"
    assert read_tick_state(pmo_env)["wake"]["error_reported"] == {}


def test_more_than_one_own_job_is_a_warn(pmo_env):
    """先建後刪中斷留下兩個：`[warn]` 說明個數（PMO 再跑一次 set 就收斂）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()
    put_job(sidecar, "job-a", every=600.0)
    put_job(sidecar, "job-b", every=600.0)

    rc, out, err = run_tick(pmo_env, deps)

    assert (rc, out) == (0, "[idle]\n")
    assert err == "[warn] 自己的叫醒 job 有 2 個\n"


def test_an_orphan_job_warns_once_and_clears_when_it_disappears(pmo_env):
    """`wake.every` 是 null 卻有自己的 job：`[warn]` 只印一次；job 消失後清掉。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env)  # wake.every 是 null
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out) == (0, "[idle]\n")
    assert err == (
        "[warn] tick.json 沒記叫醒頻率，sidecar 上有自己的 job job-own every=600s\n"
    )
    assert read_tick_state(pmo_env)["wake"]["orphan_reported_at"] == "2026-10-19T10:15:00Z"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")

    sidecar.jobs.clear()  # job 消失（sidecar 重啟）
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert read_tick_state(pmo_env)["wake"]["orphan_reported_at"] is None


def test_every_null_still_skips_the_three_interval_warning(pmo_env):
    """`wake.every` 是 null：不做「3 倍間隔」警告（T5-2 已守；這裡含孤兒 job）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, last_tick_at="2026-10-19T08:00:00Z")
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0)

    rc, out, err = run_tick(pmo_env, deps)

    assert (rc, out) == (0, "[idle]\n")
    assert "上次 tick 在" not in err
    assert "自己的 job" in err  # 只有孤兒那一行


def test_tick_asks_for_jobs_when_pmo_sidecar_is_set(pmo_env):
    """設了 `pmo_sidecar` 就每輪 `GET /jobs` 一次（即使 every 是 null）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env)
    deps, sidecar = make_deps()
    calls = record_calls(sidecar)

    rc, _out, _err = run_tick(pmo_env, deps)

    assert rc == 0
    assert calls.count("list_jobs") == 1


def test_tick_does_not_ask_for_jobs_without_pmo_sidecar(pmo_env):
    """沒設 `pmo_sidecar`：tick 不查 `/jobs`（PMO 不在 sidecar）。"""
    set_config(pmo_env, pmo_sidecar=None)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    deps, sidecar = make_deps()
    calls = record_calls(sidecar)

    rc, out, err = run_tick(pmo_env, deps)

    assert (rc, err) == (0, "")
    assert out == "[idle]\n"  # 沒有 [wake-missing]：根本沒查
    assert "list_jobs" not in calls


def test_a_failed_get_jobs_is_a_warn_and_the_round_goes_on(pmo_env):
    """`GET /jobs` 失敗：`[warn]`，這一輪其他步驟照做（agents 比對照印）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    seed_team_state(
        pmo_env, ticket=TICKET, ack_at="2026-10-19T10:14:00Z", state="running"
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="idle")
    sidecar.errors["list_jobs"] = SidecarError(status=500, code="boom")
    deps, _sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)

    assert rc == 0
    assert out == f"[state] {TEAM_KEY} running→idle\n"
    assert err == "[warn] GET /jobs：boom\n"


# --------------------------------------------------------------------------- #
# B. 鎖：不在 HTTP 期間拿著、tick 不等 wake.lock、鎖內重讀
# --------------------------------------------------------------------------- #


def _can_take_tick_lock(env: PmoEnv) -> bool:
    try:
        with _lock.path_lock(env.state_dir / "tick.json.lock", timeout=0.5):
            return True
    except _lock.LockTimeout:
        return False


def test_the_tick_lock_is_not_held_during_the_jobs_call(pmo_env):
    """假 sidecar 卡在 `GET /jobs`：另一個執行緒拿得到 `tick.json.lock`。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    sidecar = FakeSidecar()
    got: list[bool] = []

    def hook(method: str) -> None:
        if method != "list_jobs":
            return
        thread = threading.Thread(target=lambda: got.append(_can_take_tick_lock(pmo_env)))
        thread.start()
        thread.join(timeout=10)

    sidecar.on_call = hook
    deps, _sidecar = make_deps(sidecar)

    rc, _out, _err = run_tick(pmo_env, deps)

    assert rc == 0
    assert got == [True]


def test_tick_does_not_wait_for_the_wake_lock(pmo_env):
    """`wake.lock` 被別人的 `set` 拿著：tick 照跑完（它不拿這把鎖）。

    holder 拿到鎖後**一直不放**（直到 tick 回來、或 `finally` 才放）；tick 在
    另一個執行緒跑，`join(timeout=5)` 之後必須已經結束。tick 若在 `GET /jobs`
    外面拿了 `wake.lock`，它會卡到 holder 放鎖，這一支就紅。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    pmo_env.monkeypatch.setenv("ANGAREION_IDENTITY", PMO_IDENTITY)
    deps, _sidecar = make_deps()
    ready, release = threading.Event(), threading.Event()
    results: list[tuple[int, str, str]] = []

    def holder() -> None:
        with _lock.path_lock(pmo_env.state_dir / "wake.lock"):
            ready.set()
            release.wait(20)

    def one() -> None:
        out, err = StringIO(), StringIO()
        rc = cli.main(["tick"], deps=deps, stdout=out, stderr=err, stdin=StringIO())
        results.append((rc, out.getvalue(), err.getvalue()))

    thread = threading.Thread(target=holder)
    thread.start()
    tick_thread = threading.Thread(target=one)
    try:
        assert ready.wait(10)
        tick_thread.start()
        tick_thread.join(timeout=5)
        finished = not tick_thread.is_alive()
    finally:
        release.set()
        thread.join(timeout=10)
        tick_thread.join(timeout=30)

    assert finished, "tick 等了 wake.lock 才回來（它不該拿這把鎖）"
    assert results == [(0, "[wake-missing] every=10m\n", "")]


def test_a_wake_set_stuck_on_the_lock_is_a_warn_and_exit_two(pmo_env, monkeypatch):
    """`set` 等不到 `wake.lock`：`[warn]`＋exit 2，sidecar 與 `tick.json` 都沒動。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "10m"})
    monkeypatch.setattr(wake, "WAKE_LOCK_TIMEOUT", 0.2)
    deps, sidecar = make_deps()
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with _lock.path_lock(pmo_env.state_dir / "wake.lock"):
            ready.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(10)
        rc, out, err = run_wake(pmo_env, deps, "set", "2h")
    finally:
        release.set()
        thread.join(timeout=10)

    assert (rc, out) == (2, "")
    assert err == (
        "[warn] pmo wake 等不到鎖（wake.lock 最多 30 秒、tick.json.lock 最多 10 秒），"
        "沒有寫 tick.json\n"
    )
    assert sidecar.jobs == {}
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"


def test_a_wake_set_inside_a_stuck_round_survives_the_commit(pmo_env):
    """tick 卡在 HTTP 時跑的 `wake set`，不會被 tick 的 commit 蓋掉（鎖內重讀）。"""
    set_config(pmo_env)
    seed_tick_state(pmo_env)
    seed_team_state(
        pmo_env, ticket=TICKET, ack_at="2026-10-19T10:14:00Z", state="running"
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, _sidecar = make_deps(sidecar)
    results: list[tuple[int, str]] = []
    already_ran: list[bool] = []

    def hook(method: str) -> None:
        if method != "get_agent" or already_ran:
            return
        already_ran.append(True)
        out, err = StringIO(), StringIO()
        rc = cli.main(
            ["wake", "set", "2h"], deps=deps, stdout=out, stderr=err, stdin=StringIO()
        )
        results.append((rc, err.getvalue()))

    sidecar.on_call = hook
    rc, _out, _err = run_tick(pmo_env, deps)

    assert rc == 0
    assert results == [(0, "")]
    state = read_tick_state(pmo_env)
    assert state["wake"]["every"] == "2h"
    assert state["last_tick_at"] == "2026-10-19T10:15:00Z"


# --------------------------------------------------------------------------- #
# C. `_job_of` 的容錯在 adapter 測試（`test_pmo_ops_adapters.py`）：沒有 every
#    也沒有 at 照收、只有沒有 id／action 或 every 壞掉才失敗。
# --------------------------------------------------------------------------- #

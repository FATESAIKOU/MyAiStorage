"""`pmo wake set` 的退路：`POST /jobs` 的 2xx body 讀不懂時找回剛建的 job。

從 `tests/unit/test_pmo_wake.py` 原樣搬過來（只拆檔、不改斷言；nit-3），
加上第二輪審查 warning-1 的三個誘餌與「只有誘餌」兩條：

- 認出剛建的 job 的三個條件缺一不可：POST 前的清單沒有它（`seen`）、是自己的
  （句子、不是 `auto`）、`every` 是這次的間隔（`every=None` 的 at job 不算
  相同間隔）；
- 清單只有誘餌 → 不刪、不寫、`[warn]` 與 exit 2（裁定第 1 節 step 3）。

全部用假 sidecar 與暫存 state（`tests/conftest.py` 的 `isolated_home`）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import pytest

from pmo.ports import JobCreatedUnreadable, SidecarError

from _pmo_env import (
    OTHER_TEXT,
    TEAM_PM,
    WAKE_JOB,
    make_deps,
    own_jobs,
    pmo_env,
    put_job,
    read_tick_state,
    run_wake,
    seed_tick_state,
    set_config,
)


@pytest.mark.parametrize(
    "unreadable",
    [
        "POST /jobs 的輸出不是物件",
        "POST /jobs 的輸出不是 JSON（JSONDecodeError）",
    ],
    ids=["empty-body", "unreadable-body"],
)
def test_set_recovers_a_job_whose_201_body_cannot_be_read(pmo_env, unreadable):
    """2xx 但 body 讀不懂：不當失敗，再 `GET /jobs` 找到剛建的 job 照常完成。

    真的 sidecar 是「先 create、再寫回應」，所以 body 壞掉時 job 已經在了
    （裁定第 1 節）：`wake set` 靠「POST 之前沒有、自己的、`every` 相同」認出
    它，照原流程刪掉其他自己的 job、寫 `wake.every`，exit 0。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)  # 舊的 5m job
    sidecar.create_job_unreadable = unreadable

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert out == "[wake] every=10m job=job-1 next=-\n"
    assert sidecar.deleted_jobs == [WAKE_JOB]
    (job,) = own_jobs(sidecar)
    assert (job.job_id, job.every) == ("job-1", 600.0)
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"


@pytest.mark.parametrize("get_jobs_fails", [False, True], ids=["not-found", "get-fails"])
def test_set_leaves_every_job_and_the_interval_alone_when_the_new_job_cannot_be_found(
    pmo_env, get_jobs_fails
):
    """2xx 但 `GET /jobs` 也找不到剛建的 job：不刪、不寫、`[warn]` 與 exit 2。

    sidecar 上最多多一個自己的 job，下一輪 tick 的「自己的叫醒 job 有 n 個」
    會看到，再跑一次 `wake set` 就收斂（裁定第 1 節 step 3）。`GET /jobs` 本身
    失敗也是同一條退路。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)
    # 假的 POST 回 201 但沒有留下 job（比真的更壞），`GET /jobs` 找不到它。
    sidecar.errors["create_job"] = JobCreatedUnreadable(
        status=201, detail="POST /jobs 的輸出不是物件"
    )
    reads = {"n": 0}

    def hook(method: str) -> None:
        if method != "list_jobs":
            return
        reads["n"] += 1
        if get_jobs_fails and reads["n"] >= 2:  # 第一次是 POST 前的清單
            raise SidecarError(status=500, code="job_store_broken")

    sidecar.on_call = hook

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, out) == (2, "")
    assert err == (
        "[warn] POST /jobs 回 201，但回應讀不懂，"
        "找不到剛建的 job；請跑 pmo wake show 確認\n"
    )
    assert sidecar.deleted_jobs == []
    assert list(sidecar.jobs) == [WAKE_JOB]
    wake_state = read_tick_state(pmo_env)["wake"]
    assert (wake_state["every"], wake_state["set_at"]) == (
        "5m",
        "2026-10-19T09:00:00Z",
    )


def test_set_recovery_tells_the_created_job_from_the_look_alikes(pmo_env):
    """退路的三個條件缺一不可：三個誘餌都在，只有真匹配的算剛建的 job。

    POST 後的清單同時有：

    (i) 自己的舊 job，`every` 相同但 `args.id` 是舊的 PMO 名字（在 POST 前的
        清單裡，`seen` 認得它）；
    (ii) 自己的 job、POST 後才出現，但 `every` 是別的間隔；
    (iii) `every` 相同、句子不同的別人的 job，POST 後才出現。

    只有 (i) 被條件一擋（在 `seen`）、(ii) 被條件三擋（間隔不同）、(iii) 被
    條件二擋（不是自己的）；真正的 `job-1` 才被當成剛建的：其他自己的 job 被
    刪、別人的不碰、`wake.every` 寫成這次的值（第二輪審查 warning-1）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, "job-old", every=600.0, agent_id="demo-pm-m9")

    def hook(method: str) -> None:
        if method != "create_job":
            return
        put_job(sidecar, "job-own-other-interval", every=120.0)
        put_job(sidecar, "job-other-sentence", every=600.0, text=OTHER_TEXT, agent_id=TEAM_PM)

    sidecar.on_call = hook
    sidecar.create_job_unreadable = "POST /jobs 的輸出不是物件"

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert out == "[wake] every=10m job=job-1 next=-\n"
    assert sidecar.deleted_jobs == ["job-old"]
    assert set(sidecar.jobs) == {
        "job-own-other-interval",
        "job-other-sentence",
        "job-1",
    }
    assert read_tick_state(pmo_env)["wake"]["every"] == "10m"


def test_set_recovers_nothing_when_the_list_has_only_look_alikes(pmo_env):
    """清單只有誘餌（自己的但間隔不同、間隔相同但是別人的）：不刪、不寫、exit 2。

    `POST /jobs` 的 201 body 讀不懂、而且 sidecar 上沒有真正的匹配（假的 POST
    連 job 都沒留下），退路必須承認找不到：舊 job 與兩個誘餌都不動，`wake.every`
    維持原值，印重試提示並 exit 2（裁定第 1 節 step 3）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)
    sidecar.errors["create_job"] = JobCreatedUnreadable(
        status=201, detail="POST /jobs 的輸出不是物件"
    )
    reads = {"n": 0}

    def hook(method: str) -> None:
        if method != "list_jobs":
            return
        reads["n"] += 1
        if reads["n"] < 2:  # 第一次是 POST 前的清單
            return
        put_job(sidecar, "job-own-other-interval", every=120.0)
        put_job(sidecar, "job-other-sentence", every=600.0, text=OTHER_TEXT, agent_id=TEAM_PM)

    sidecar.on_call = hook

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, out) == (2, "")
    assert err == (
        "[warn] POST /jobs 回 201，但回應讀不懂，"
        "找不到剛建的 job；請跑 pmo wake show 確認\n"
    )
    assert sidecar.deleted_jobs == []
    assert set(sidecar.jobs) == {
        WAKE_JOB,
        "job-own-other-interval",
        "job-other-sentence",
    }
    wake_state = read_tick_state(pmo_env)["wake"]
    assert (wake_state["every"], wake_state["set_at"]) == (
        "5m",
        "2026-10-19T09:00:00Z",
    )


def test_recovery_does_not_take_a_job_without_an_interval_as_the_new_one(pmo_env):
    """`every=None` 的自己的 job（at job）不算「相同間隔」：退路仍找回 `job-1`。

    第二輪審查 m21：`_same_interval` 若把沒有 `every` 的 job 當成同一個間隔，
    這裡會把 POST 後出現的 at job 當成剛建的那一個（第二輪審查 warning-1）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": "5m", "set_at": "2026-10-19T09:00:00Z"})
    deps, sidecar = make_deps()
    put_job(sidecar, every=300.0)

    def hook(method: str) -> None:
        if method != "create_job":
            return
        put_job(sidecar, "job-at", every=None)

    sidecar.on_call = hook
    sidecar.create_job_unreadable = "POST /jobs 的輸出不是物件"

    rc, out, err = run_wake(pmo_env, deps, "set", "10m")

    assert (rc, err) == (0, "")
    assert out == "[wake] every=10m job=job-1 next=-\n"
    assert sidecar.deleted_jobs == [WAKE_JOB]
    assert set(sidecar.jobs) == {"job-at", "job-1"}


"""交接出去的群請求：插槽 A／B 與 `pmo inbox handoff-close`（#358 T5-3）。

驗收（工單 #358 T5-3、design §3.2.1／§3.4、Q-9 (a)）：

- 插槽 A：這一輪看到的群請求 id 記進 `tick.json` 的 `handoffs`
  （`handed_at`＝now、`last_reminded_at` 空、`next_due_at`＝now＋10 分鐘）；
  同一 id 重複出現不覆蓋 `handed_at`；
- 插槽 B：未到期不印；到期印一行 `[group-sweep-due] ‹id…›` 並把
  `last_reminded_at`／`next_due_at` 從這一輪往後推；很久沒叫醒也只印一次；
  多筆同時到期印在同一行；未到期的到期時刻進 `[idle] [next-due=…]`；
- `handoff-close`：刪掉那一筆、之後不再印；查無 id 回 1；`tick.json` 壞回 2；
  `tick.json.lock` 等不到回 2。

全部用假 sidecar／假 angareion 與暫存 state（`tests/conftest.py` 的
`isolated_home`），不連真的 sidecar、通道或 Drive。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import threading

from pmo import _lock
from pmo import handoff as handoff_module
from pmo import tickstate
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.runtime import Deps

from _pmo_env import (
    NOW,
    TEAM_KEY,
    ULID_2,
    PmoEnv,
    fixed_now,
    pmo_env,
    read_team_state,
    read_tick_state,
    run_cli,
    secretary_msg,
    seed_team_state,
    seed_tick_state,
    tick_file,
)

GROUP_ID = "01JGROUP0000000000000000AA"
OTHER_GROUP_ID = "01JGROUP0000000000000000BB"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def make_deps(*, ang=None) -> Deps:
    return Deps(
        angareion=ang if ang is not None else FakeAngareion(),
        sidecar_for=FakeSidecars({}, default=FakeSidecar()),
        now=fixed_now(NOW),
    )


def run_tick(env: PmoEnv, deps: Deps | None = None):
    return run_cli(["tick"], env, deps=deps if deps is not None else make_deps())


def seed_handoff(
    env: PmoEnv,
    handoff_id: str = GROUP_ID,
    *,
    handed_at: str = "2026-10-19T10:00:00Z",
    last_reminded_at: str | None = None,
    next_due_at: str = "2026-10-19T10:10:00Z",
) -> None:
    seed_tick_state(
        env,
        handoffs={
            handoff_id: {
                "handed_at": handed_at,
                "last_reminded_at": last_reminded_at,
                "next_due_at": next_due_at,
            }
        },
    )


def handoff_of(env: PmoEnv, handoff_id: str = GROUP_ID):
    return tickstate.load_or_empty(tick_file(env)).handoffs[handoff_id]


def group_request(ulid: str = ULID_2):
    return secretary_msg(ulid=ulid, content="[create] grp-demo")


# --------------------------------------------------------------------------- #
# 插槽 A：記錄
# --------------------------------------------------------------------------- #


def test_a_group_request_is_recorded_with_a_ten_minute_due(pmo_env):
    """這一輪看到的群請求記進 `handoffs`：now、空提醒、now＋10 分鐘（A）。"""
    deps = make_deps(ang=FakeAngareion([group_request(ULID_2)]))

    rc, out, err = run_tick(pmo_env, deps)

    assert (rc, err) == (0, "")
    assert f"[group-handoff] {ULID_2}" in out
    entry = handoff_of(pmo_env, ULID_2)
    assert entry.handed_at == "2026-10-19T10:15:00Z"
    assert entry.last_reminded_at is None
    assert entry.next_due_at == "2026-10-19T10:25:00Z"


def test_a_known_id_keeps_its_handed_at(pmo_env):
    """同一個 id 再被讀到：`handed_at` 不覆蓋；到期照樣提醒、往後推。"""
    seed_handoff(
        pmo_env,
        handed_at="2026-10-19T10:00:00Z",
        next_due_at="2026-10-19T10:10:00Z",
    )
    deps = make_deps(ang=FakeAngareion([group_request(GROUP_ID)]))

    rc, out, err = run_tick(pmo_env, deps)

    assert (rc, err) == (0, "")
    assert out == (
        f"[group-handoff] {GROUP_ID}\n"
        f"[group-sweep-due] {GROUP_ID}\n"
    )
    entry = handoff_of(pmo_env)
    assert entry.handed_at == "2026-10-19T10:00:00Z"
    assert entry.last_reminded_at == "2026-10-19T10:15:00Z"
    assert entry.next_due_at == "2026-10-19T10:25:00Z"


def test_a_second_group_request_in_the_same_round_is_recorded_once(pmo_env):
    """同一輪同一 id 只記一筆，`handed_at` 是這一輪。"""
    deps = make_deps(
        ang=FakeAngareion([group_request(ULID_2), group_request(ULID_2)])
    )

    rc, _out, err = run_tick(pmo_env, deps)

    assert rc == 0
    entry = handoff_of(pmo_env, ULID_2)
    assert entry.handed_at == "2026-10-19T10:15:00Z"


# --------------------------------------------------------------------------- #
# 插槽 B：到期提醒
# --------------------------------------------------------------------------- #


def test_a_handoff_not_due_prints_nothing(pmo_env):
    """未到期：不印 `[group-sweep-due]`，只把到期時刻放進 `next-due`。"""
    seed_handoff(pmo_env, next_due_at="2026-10-19T10:20:00Z")

    rc, out, err = run_tick(pmo_env)

    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:20:00Z]\n"
    entry = handoff_of(pmo_env)
    assert entry.last_reminded_at is None
    assert entry.next_due_at == "2026-10-19T10:20:00Z"


def test_a_due_handoff_prints_once_and_pushes_both_times(pmo_env):
    """到期：印一次，`last_reminded_at`／`next_due_at` 從這一輪往後推。"""
    seed_handoff(pmo_env, next_due_at="2026-10-19T10:10:00Z")

    rc, out, err = run_tick(pmo_env)

    assert (rc, err) == (0, "")
    assert out == f"[group-sweep-due] {GROUP_ID}\n"
    entry = handoff_of(pmo_env)
    assert entry.last_reminded_at == "2026-10-19T10:15:00Z"
    assert entry.next_due_at == "2026-10-19T10:25:00Z"

    rc, out, err = run_tick(pmo_env)
    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:25:00Z]\n"


def test_a_long_sleep_reminds_only_once(pmo_env):
    """很久沒叫醒（到期一小時前）：也只印一次、不補跑（B）。"""
    seed_handoff(pmo_env, next_due_at="2026-10-19T09:00:00Z")

    rc, out, err = run_tick(pmo_env)

    assert (rc, err) == (0, "")
    assert out.count("[group-sweep-due]") == 1
    assert handoff_of(pmo_env).next_due_at == "2026-10-19T10:25:00Z"

    rc, out, err = run_tick(pmo_env)
    assert (rc, err) == (0, "")
    assert "[group-sweep-due]" not in out


def test_due_handoffs_print_on_one_line(pmo_env):
    """多筆同時到期：同一行（依 id 排序，輸出穩定）。"""
    seed_tick_state(
        pmo_env,
        handoffs={
            OTHER_GROUP_ID: {
                "handed_at": "2026-10-19T10:00:00Z",
                "last_reminded_at": None,
                "next_due_at": "2026-10-19T10:05:00Z",
            },
            GROUP_ID: {
                "handed_at": "2026-10-19T10:00:00Z",
                "last_reminded_at": None,
                "next_due_at": "2026-10-19T10:06:00Z",
            },
        },
    )

    rc, out, err = run_tick(pmo_env)

    assert (rc, err) == (0, "")
    assert out == f"[group-sweep-due] {GROUP_ID} {OTHER_GROUP_ID}\n"
    for key in (GROUP_ID, OTHER_GROUP_ID):
        assert handoff_of(pmo_env, key).next_due_at == "2026-10-19T10:25:00Z"


# --------------------------------------------------------------------------- #
# `pmo inbox handoff-close`
# --------------------------------------------------------------------------- #


def test_handoff_close_deletes_the_entry_and_stops_the_reminder(pmo_env):
    """`handoff-close` 之後不再印 `[group-sweep-due]`（C）。"""
    seed_handoff(pmo_env, next_due_at="2026-10-19T10:10:00Z")

    rc, out, err = run_cli(
        ["inbox", "handoff-close", GROUP_ID], pmo_env, deps=make_deps()
    )

    assert (rc, err) == (0, "")
    assert GROUP_ID in out
    assert read_tick_state(pmo_env)["handoffs"] == {}

    rc, out, err = run_tick(pmo_env)
    assert (rc, err) == (0, "")
    assert out == "[idle]\n"


def test_handoff_close_of_an_unknown_id_exits_one(pmo_env):
    """查無 id → exit 1（C）。"""
    seed_handoff(pmo_env)

    rc, out, err = run_cli(
        ["inbox", "handoff-close", "01JNOPE00000000000000000AA"],
        pmo_env,
        deps=make_deps(),
    )

    assert (rc, out) == (1, "")
    assert err.startswith("[pmo] ")
    assert GROUP_ID in read_tick_state(pmo_env)["handoffs"]


def test_handoff_close_with_a_bad_tick_state_exits_two(pmo_env):
    """`tick.json` 壞 → exit 2、`[要本人處理]`（C；design §3.4）。"""
    seed_handoff(pmo_env)
    tick_file(pmo_env).write_text("{ not json", encoding="utf-8")

    rc, out, err = run_cli(
        ["inbox", "handoff-close", GROUP_ID], pmo_env, deps=make_deps()
    )

    assert (rc, err) == (2, "")
    assert out.startswith("[要本人處理] ")


def test_handoff_close_lock_timeout_exits_two(pmo_env, monkeypatch):
    """`tick.json.lock` 等不到 → `[warn]`＋exit 2，那一筆還在（C；§3.4）。"""
    seed_handoff(pmo_env)
    monkeypatch.setattr(handoff_module, "HANDOFF_LOCK_TIMEOUT_SECONDS", 0.2)
    lock_path = tick_file(pmo_env).with_name("tick.json.lock")
    ready, release = threading.Event(), threading.Event()

    def holder() -> None:
        with _lock.path_lock(lock_path):
            ready.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert ready.wait(10)
        rc, out, err = run_cli(
            ["inbox", "handoff-close", GROUP_ID], pmo_env, deps=make_deps()
        )
    finally:
        release.set()
        thread.join(timeout=10)

    assert (rc, out) == (2, "")
    assert err == "[warn] tick.json 的鎖等不到（10 秒），沒有結案，下一輪再試\n"
    assert GROUP_ID in read_tick_state(pmo_env)["handoffs"]


def test_handoff_close_write_failure_exits_two(pmo_env, monkeypatch):
    """寫不進去 → exit 2、`[要本人處理]`，那一筆還在（C）。"""
    seed_handoff(pmo_env)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(handoff_module.tickstate, "save", boom)

    rc, out, err = run_cli(
        ["inbox", "handoff-close", GROUP_ID], pmo_env, deps=make_deps()
    )

    assert (rc, err) == (2, "")
    assert out.startswith("[要本人處理] tick 狀態寫不進去：")
    assert GROUP_ID in read_tick_state(pmo_env)["handoffs"]


def test_handoff_close_does_not_change_the_team_state(pmo_env):
    """只刪 `handoffs`：team 狀態與 `last_tick_at` 原樣（C 的範圍）。"""
    seed_handoff(pmo_env)
    seed_team_state(pmo_env, ticket="demo#45")
    before = read_team_state(pmo_env)

    rc, _out, err = run_cli(
        ["inbox", "handoff-close", GROUP_ID], pmo_env, deps=make_deps()
    )

    assert (rc, err) == (0, "")
    assert read_team_state(pmo_env) == before
    assert read_tick_state(pmo_env)["last_tick_at"] is None
    assert read_tick_state(pmo_env)["handoffs"] == {}

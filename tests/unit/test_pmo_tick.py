"""`pmo tick` 的骨架、收信、agents 比對與 `[idle]`（#358 T5-2；design §2.2、§3.2.1）。

驗收（工單 #358 T5-2）：
- 骨架照架構師裁定 §1：事件進 `run.events`、`tick.json` 的改動進 `run.writes`；
- 裁定 S-1：身分不符在 `_precheck` 最前面擋成 exit 2，什麼都沒做（游標不動、
  不打 sidecar、不寫任何檔）；
- 裁定 S-2：收信失敗的 `PortError` 印 `[warn]`，這一輪的 agents 比對照做；
  壞檔是 `[要本人處理]`＋exit 2；
- 第 1、5、10 條：`[state]` 只在變化時印、印完寫回；`NEED_REOPEN`（含 tui 版）、
  `[no-candidate]`、`[waiting-quota]`、非預期結束、PMO 自己 close 的 exited；
- `[idle]` 只在沒有 stdout 事件時印；worker team 不打 `GET /agents`。

全部用假 sidecar／假 angareion 與暫存 state（`tests/conftest.py` 的
`isolated_home`），不連真的 sidecar 與通道。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json
import threading

from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo import teamstate
from pmo import tick as tick_module
from pmo.ports import Message, PortError, SidecarError
from pmo.runtime import Deps

from _pmo_env import (
    NOW,
    OTHER_CHANNEL,
    OTHER_IDENTITY_WRONG,
    OTHER_PM,
    PMO_IDENTITY,
    TEAM_CHANNEL,
    TEAM_KEY,
    TEAM_PM,
    ULID_1,
    ULID_2,
    ULID_3,
    PmoEnv,
    config_data,
    fixed_now,
    pmo_env,
    read_team_state,
    run_cli,
    seed_team_state,
    seed_tick_state,
    secretary_msg,
)

TEAM_SIDECAR = "http://127.0.0.1:8472"  # 同 `config_data()` 的假埠
SECOND_SIDECAR = "http://127.0.0.1:8473"
TICKET = "demo#45"
SESSION = "agora:01TICK2SESSION000000000"


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def make_deps(sidecar: FakeSidecar | None = None, *, ang=None, factory=None):
    """一組假 deps；sidecar 用 `FakeSidecars` 依位址回答（預設一台）。"""
    sidecar = sidecar if sidecar is not None else FakeSidecar()
    factory = factory if factory is not None else FakeSidecars({}, default=sidecar)
    deps = Deps(
        angareion=ang if ang is not None else FakeAngareion(),
        sidecar_for=factory,
        now=fixed_now(NOW),
    )
    return deps, sidecar, factory


def run_tick(env: PmoEnv, deps: Deps, *, identity: str = PMO_IDENTITY):
    return run_cli(["tick"], env, deps=deps, identity=identity)


def seed_dispatched(env: PmoEnv, *, key: str = TEAM_KEY, **extra) -> None:
    """一份「已派工、已回收到」的 team 狀態（看門狗因此不會發）。"""
    fields: dict = {"ticket": TICKET, "ack_at": "2026-10-19T10:01:00Z"}
    fields.update(extra)
    if key == TEAM_KEY:
        seed_team_state(env, **fields)
    else:
        env.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "agent": key,
            "closed": False,
            "dispatched_at": "2026-10-19T10:00:00Z",
        }
        data.update(fields)
        (env.state_dir / f"{key}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )


def add_second_team(env: PmoEnv, *, sidecar: str = SECOND_SIDECAR) -> tuple[str, str]:
    """設定裡再加一個本機 team（`demo3-m1`），回 `(key, pm)`。"""
    data = config_data()
    key, pm = "demo3-m1", "demo3-pm-m1"
    data["teams"].append(
        {
            "key": key,
            "pm": pm,
            "channel": f"team-{key}",
            "sidecar": sidecar,
            "tui": False,
            "life": False,
            "retired": False,
        }
    )
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return key, pm


def a_message(message_id: str, *, sender: str, channel: str, body: str) -> Message:
    return Message(
        message_id=message_id,
        channel=channel,
        sender=sender,
        to=PMO_IDENTITY,
        received_at="2026-10-19T10:14:00Z",
        body=body,
        urgency=5,
    )


# --------------------------------------------------------------------------- #
# 骨架與 ⑤：沒事的一輪
# --------------------------------------------------------------------------- #


def test_an_empty_round_prints_only_idle(pmo_env):
    """沒有信、沒有 team 在跑：一行 `[idle]`，exit 0，stderr 空的。"""
    deps, _sidecar, _factory = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_idle_carries_the_earliest_due_moment(pmo_env):
    """有未到期的項目時 `[idle]` 附上最早的那一個（第 6 條：只看 stdout）。"""
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
    deps, _sidecar, _factory = make_deps()
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == "[idle] [next-due=2026-10-19T10:17:00Z]\n"


def test_a_warn_only_round_still_prints_idle(pmo_env):
    """只有 `[warn]`（stderr）的一輪仍印 `[idle]`（第 6 條）。"""
    sidecar = FakeSidecar()
    sidecar.errors["get_agent"] = SidecarError(status=500, code="boom")
    deps, _sidecar, _factory = make_deps(sidecar)
    rc, out, err = run_tick(pmo_env, deps)
    assert rc == 0
    assert out == "[idle]\n"
    assert err == f"[warn] {TEAM_KEY}：boom\n"


# --------------------------------------------------------------------------- #
# ① 收信：既有標籤照原樣出現
# --------------------------------------------------------------------------- #


def test_the_round_prints_the_existing_labels(pmo_env):
    """`[secretary]`／`[group-handoff]`／`[ack]`／`[msg]` 各一則（T5-2 不改字樣）。"""
    # 還沒 ack：teammsg 才會比對「收到」並寫 ack_at；派工時刻就在現在附近，
    # 看門狗還沒到期。
    seed_team_state(pmo_env, ticket=TICKET, dispatched_at="2026-10-19T10:14:00Z")
    messages = [
        secretary_msg(ulid=ULID_1),
        secretary_msg(ulid=ULID_2, content="[create] grp-demo"),
        a_message(ULID_3, sender=TEAM_PM, channel=TEAM_CHANNEL, body=f"收到 {TICKET}"),
        a_message(
            "01JX9A1B2C3D4E5F6G7H8J9K2R",
            sender=OTHER_PM,
            channel=OTHER_CHANNEL,
            body="一般訊息",
        ),
    ]
    deps, _sidecar, _factory = make_deps(ang=FakeAngareion(messages))
    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert f"[secretary] 收到新工作 {ULID_1}" in out
    assert f"[group-handoff] {ULID_2}" in out
    assert f"[ack] {TEAM_PM} 回了「收到 {TICKET}」" in out
    assert "[msg]" in out
    assert "一般訊息" in out


def test_a_group_handoff_id_reaches_the_rounds_handoffs_list(pmo_env, monkeypatch):
    """第 7 條：`tick` 把 `run.handoffs_seen` 交進 `read_round`，群請求 id 進得去（T5-3 用）。

    斷言的是**同一個 list object**（`is`），不是內容：傳臨時 list 也會被 append 出同樣內容。
    """
    handed: list = []
    original = tick_module.round_module.read_round
    original_new_run = tick_module._new_run
    runs: list = []

    def spy(ctx, *, handoffs=None, team_lock_timeout=None, lock_skipped=None):
        handed.append(handoffs)
        return original(
            ctx,
            handoffs=handoffs,
            team_lock_timeout=team_lock_timeout,
            lock_skipped=lock_skipped,
        )

    def spy_new_run(ctx):
        run = original_new_run(ctx)
        runs.append(run)
        return run

    monkeypatch.setattr(tick_module.round_module, "read_round", spy)
    monkeypatch.setattr(tick_module, "_new_run", spy_new_run)
    deps, _sidecar, _factory = make_deps(
        ang=FakeAngareion([secretary_msg(ulid=ULID_2, content="[create] grp-demo")])
    )

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert f"[group-handoff] {ULID_2}" in out
    assert len(runs) == 1
    run = runs[0]
    # `handed[0]` 就是這一輪 `run.handoffs_seen` 本尊，id 也在裡面。
    assert len(handed) == 1
    assert handed[0] is run.handoffs_seen
    assert ULID_2 in run.handoffs_seen


# --------------------------------------------------------------------------- #
# ② agents 比對：變化才印、印完寫回
# --------------------------------------------------------------------------- #


def test_a_state_change_prints_state_once_and_is_written_back(pmo_env):
    """`[state]` 只印變化；寫回之後下一輪不再印（第 1 條）。"""
    seed_dispatched(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[state] {TEAM_KEY} -→running\n"
    state = read_team_state(pmo_env)
    assert (state["state"], state["session"]) == ("running", "s1")

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_an_absent_team_prints_need_reopen_once(pmo_env):
    """應該在跑卻不在 sidecar 上：`NEED_REOPEN … session=…`，恢復前不重印。"""
    seed_dispatched(pmo_env, session=SESSION)
    deps, _sidecar, _factory = make_deps()  # 假 sidecar 上沒有這個 agent

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"NEED_REOPEN {TEAM_KEY} session={SESSION}\n"
    assert read_team_state(pmo_env)["state"] == "absent"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_an_absent_interactive_team_says_tui(pmo_env):
    """互動版不吃 `--session`：`NEED_REOPEN ‹team› tui=true`（裁定第 10 條）。"""
    data = config_data()
    data["teams"][0]["tui"] = True
    pmo_env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    seed_dispatched(pmo_env, session=SESSION)
    deps, _sidecar, _factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"NEED_REOPEN {TEAM_KEY} tui=true\n"


def test_no_candidate_prints_the_sidecars_provider(pmo_env):
    """`exit.reason` 含 no candidate → `[no-candidate] ‹team› ‹provider›`（第 5 條）。"""
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(
        TEAM_PM,
        state="exited",
        exit={"code": 1, "reason": "no candidate is left after exclude"},
        provider="ollama-cloud",
    )
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[no-candidate] {TEAM_KEY} ollama-cloud\n"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_no_candidate_falls_back_to_the_teams_provider(pmo_env):
    """sidecar 沒回 `provider` 時用團隊設定，再沒有就 `-`（第 5 條）。"""
    data = config_data()
    data["teams"][0]["provider"] = "opencode-zen"
    pmo_env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(
        TEAM_PM, state="exited", exit={"reason": "No Candidate: none left"}
    )
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[no-candidate] {TEAM_KEY} opencode-zen\n"


def test_no_candidate_prefers_the_sidecars_provider_over_the_teams(pmo_env):
    """sidecar 與 team 都有 `provider`：印 sidecar 回的那一個（第 5 條）。"""
    data = config_data()
    data["teams"][0]["provider"] = "opencode-zen"
    pmo_env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(
        TEAM_PM,
        state="exited",
        exit={"reason": "no candidate is left after exclude"},
        provider="ollama-cloud",
    )
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[no-candidate] {TEAM_KEY} ollama-cloud\n"


def test_no_candidate_without_any_provider_prints_a_dash(pmo_env):
    """sidecar 與 team 都沒有 `provider`：`[no-candidate] … -`（第 5 條）。"""
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(
        TEAM_PM, state="exited", exit={"reason": "no candidate is left after exclude"}
    )
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[no-candidate] {TEAM_KEY} -\n"


def test_waiting_quota_prints_once(pmo_env):
    """`[waiting-quota]` 只印一次；有 `reopen_at` 時不勞煩本人（第 10 條 (c)）。"""
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(
        TEAM_PM, state="waiting-quota", extra={"reopen_at": "2026-10-19T10:30:00Z"}
    )
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[waiting-quota] {TEAM_KEY}\n"
    assert read_team_state(pmo_env)["reopen_at"] == "2026-10-19T10:30:00Z"

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")


def test_waiting_quota_without_reopen_at_needs_the_person(pmo_env):
    """`reopen_at` 是 null：全部冷卻而且沒有候選，要本人決定重開。"""
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="waiting-quota")
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == (
        f"[waiting-quota] {TEAM_KEY}\n"
        f"[要本人處理] {TEAM_PM} 全部冷卻而且沒有候選（reopen_at=null）；"
        "要不要重開由本人決定\n"
    )


def test_an_unexpected_exit_needs_the_person(pmo_env):
    """不是 no candidate、也不是 PMO 自己 close 的結束 → `[要本人處理]`。"""
    seed_dispatched(pmo_env, state="running")
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="exited", exit={"code": 1, "reason": "boom"})
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[要本人處理] {TEAM_PM} 結束（exit=boom）；要不要重開由本人決定\n"


def test_an_exit_after_the_pmos_close_is_only_a_state_line(pmo_env):
    """team 狀態有 `close_requested_at`（PMO 自己 close 的）→ 只有 `[state]`。"""
    seed_dispatched(
        pmo_env, state="running", close_requested_at="2026-10-19T10:05:00Z"
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="exited", exit={"code": 0, "reason": "closed"})
    deps, _sidecar, _factory = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out == f"[state] {TEAM_KEY} running→exited\n"


def test_a_worker_team_is_never_looked_up(pmo_env):
    """worker team：tick 不打 `GET /agents`（D-8）。"""
    data = config_data()
    data["teams"][0]["sidecar"] = "worker"
    pmo_env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # 沒有派工：worker team 也輪不到 poll，這一輪只該是 idle。
    seed_team_state(pmo_env, ticket="")
    deps, sidecar, factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle]\n", "")
    assert sidecar.agent_reads == []
    assert factory.addresses == []


def test_one_teams_sidecar_failure_is_a_warn_and_the_others_run(pmo_env):
    """`GET /agents` 失敗：`[warn]`、exit 0，另一個 team 照樣比對（§2.3）。"""
    key, pm = add_second_team(pmo_env)
    seed_dispatched(pmo_env)
    seed_dispatched(pmo_env, key=key)
    bad = FakeSidecar()
    bad.errors["get_agent"] = SidecarError(status=500, code="boom")
    good = FakeSidecar()
    good.add_agent(pm, state="running")
    factory = FakeSidecars({TEAM_SIDECAR: bad, SECOND_SIDECAR: good})
    deps, _sidecar, _factory = make_deps(factory=factory)

    rc, out, err = run_tick(pmo_env, deps)
    assert rc == 0
    assert err == f"[warn] {TEAM_KEY}：boom\n"
    assert out == f"[state] {key} -→running\n"


# --------------------------------------------------------------------------- #
# ⑥ 前置失敗：壞檔／身分不符
# --------------------------------------------------------------------------- #


def test_a_bad_team_state_exits_two_without_reading(pmo_env):
    """壞 team 狀態 → exit 2、`[要本人處理]`（stdout），`angareion read` 沒被呼叫。"""
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.team_file().write_text("{ not json", encoding="utf-8")
    deps, sidecar, _factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (2, "")
    assert out.startswith("[要本人處理] ")
    assert deps.angareion.read_calls == []
    assert sidecar.agent_reads == []


def test_a_bad_tick_state_exits_two_without_reading(pmo_env):
    """壞 `tick.json` → exit 2、`[要本人處理]`（stdout），游標不動。"""
    seed_tick_state(pmo_env)
    (pmo_env.state_dir / "tick.json").write_text("{ not json", encoding="utf-8")
    deps, _sidecar, _factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, err) == (2, "")
    assert out.startswith("[要本人處理] ")
    assert deps.angareion.read_calls == []


def test_an_identity_mismatch_exits_two_and_does_nothing(pmo_env):
    """裁定 S-1：身分不符在 `_precheck` 最前面擋下，什麼都沒做。"""
    seed_dispatched(pmo_env)
    deps, sidecar, _factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps, identity=OTHER_IDENTITY_WRONG)
    assert (rc, out) == (2, "")
    assert err.startswith("[pmo] 身分不符拒送")
    assert deps.angareion.read_calls == []
    assert sidecar.agent_reads == []
    assert not (pmo_env.state_dir / "tick.json").exists()


def test_identity_mismatch_wins_over_a_bad_tick_state(pmo_env, monkeypatch):
    """裁定 S-1：守門在 `tick` 最前，身分不符時連壞掉的 `tick.json` 都不讀。"""
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    (pmo_env.state_dir / "tick.json").write_text("{ not json", encoding="utf-8")
    reads: list = []
    original = tick_module.tickstate.load_or_empty

    def spy(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(tick_module.tickstate, "load_or_empty", spy)
    deps, sidecar, _factory = make_deps()

    rc, out, err = run_tick(pmo_env, deps, identity=OTHER_IDENTITY_WRONG)
    assert (rc, out) == (2, "")
    assert err.startswith("[pmo] 身分不符拒送")
    assert reads == []
    assert deps.angareion.read_calls == []
    assert sidecar.agent_reads == []


def test_a_read_failure_is_a_warn_and_the_agents_are_still_compared(pmo_env):
    """裁定 S-2／§2.3：`angareion read` 失敗印 `[warn]`，agents 比對照做。"""
    seed_dispatched(pmo_env)
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    ang = FakeAngareion()
    ang.errors["read"] = PortError("back end is down")
    deps, _sidecar, _factory = make_deps(sidecar, ang=ang)

    rc, out, err = run_tick(pmo_env, deps)
    assert rc == 0
    assert err == "[warn] angareion read：PortError: back end is down\n"
    assert out == f"[state] {TEAM_KEY} -→running\n"
    assert ang.read_calls == [None]


def test_a_team_lock_timeout_in_the_read_round_skips_the_ack(pmo_env, monkeypatch):
    """收信階段拿不到 team 鎖：`[warn]` 跳過該 team、不寫 `ack_at`、exit 0。"""
    seed_team_state(
        pmo_env,
        ticket=TICKET,
        state="running",
        session="s1",
        dispatched_at="2026-10-19T10:14:00Z",
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    messages = [
        a_message(ULID_3, sender=TEAM_PM, channel=TEAM_CHANNEL, body=f"收到 {TICKET}")
    ]
    deps, _sidecar, _factory = make_deps(
        sidecar, ang=FakeAngareion(messages)
    )
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
    assert "[msg]" in out
    assert "[ack]" not in out
    assert err == (
        f"[warn] {TEAM_KEY} 的狀態鎖等不到（0.2 秒），這一輪跳過它\n"
    )
    assert read_team_state(pmo_env)["ack_at"] is None

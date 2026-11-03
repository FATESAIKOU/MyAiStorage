"""`pmo open` 的測試（#110 T-4b；design §3.2、§5.2）。

驗收（工單 #356）：§3.2 那一列的參數、輸出與每個結束碼；拒絕條件（未知／
退役／worker team、沒先 send、狀態沒有 dispatched_at、缺 workdir、
provider／model 不成對、--session 與 --fresh 同時給）；第一則輸入帶 team
狀態的 `dispatched_at`（`--since`）；成功把 `state`／`exit` 寫回 team 狀態
且在 `teamstate.state_lock` 內重讀再改；sidecar 的拒絕（400／409）與連不上；
錯誤訊息不含憑證、sidecar 回應只挑已知欄位（D-12）。sidecar 埠依位址取得
（#356）：`pmo open` 打 team 的 `sidecar`，不是 PMO 的 `pmo_sidecar`。

全部用假 sidecar 與暫存 state（`tests/conftest.py` 的 `isolated_home`），不打
真的 sidecar。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import io
import json
import threading
import urllib.error
from datetime import datetime, timezone

import pytest

from pmo import open as open_module
from pmo import teamstate
from pmo.adapters import Sidecar
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.ports import AgentRecord, PortError, SidecarError, SidecarShapeError
from pmo.runtime import Deps

from _pmo_env import (
    TEAM_KEY,
    TEAM_PM,
    PmoEnv,
    config_data,
    pmo_env,
    run_cli,
    seed_team_state,
)

# --------------------------------------------------------------------------- #
# 假資料（全部是假值；不含真頻道、真 repo、真身分）
# --------------------------------------------------------------------------- #

REPO = "demo/repo"
NUMBER = 7
TICKET = f"{REPO}#{NUMBER}"
WORKDIR = "/tmp/fake-wt-356b"  # 假路徑；測試不會去讀它
SESSION = "agora:01M60WT5NPK2FTZ73Y7Y3WANX9"
ATTACH = "attach demo2-pm-m1"
PROVIDER = "opencode-zen"
MODEL = "glm"
DISPATCHED_AT = "2026-10-19T10:00:00Z"
PMO_SIDECAR = "http://127.0.0.1:8499"
#: team 的 sidecar 位址（同 `_pmo_env.config_data()`；假埠）。
TEAM_SIDECAR = "http://127.0.0.1:8472"
NOW = datetime(2026, 10, 19, 10, 15, 0, tzinfo=timezone.utc)
#: 假 secret：前綴在執行時才組起來（檔案裡不留完整樣式）。
FAKE_SECRET = "sk" + "-ant-" + "x" * 40


def write_team_config(
    env: PmoEnv, *, overrides: dict | None = None, drop: tuple[str, ...] = ()
) -> None:
    """把 team 設定改成這個測試要的形狀（workdir／預設 provider/model…）。"""
    data = config_data()
    team = data["teams"][0]
    for key in drop:
        team.pop(key, None)
    if overrides:
        team.update(overrides)
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class OpenSidecar(FakeSidecar):
    """`open_agent` 回一份 sidecar 形狀的回應，並記下它收到的參數。

    真的 sidecar 會回 agent 的 `state`／`session`／`attach` 等欄位；`attach`
    是 sidecar 給的（不是本指令自己組的），所以照 `adapters._agent_of` 讀到的
    形狀放在 `AgentRecord.attach`，驗「只印已知欄位」也順便放一個不該出現在
    輸出的欄位。
    """

    def __init__(
        self,
        *,
        state: str = "starting",
        attach: str | None = ATTACH,
        session: str | None = None,
        exit: dict | None = None,
        address: str = TEAM_SIDECAR,
        input_unconfirmed_at: str | None = None,
    ) -> None:
        super().__init__(address)
        self.reply_state = state
        self.reply_attach = attach
        self.reply_session = session
        self.reply_exit = exit
        self.reply_unconfirmed_at = input_unconfirmed_at
        self.payloads: list[dict] = []

    def open_agent(self, **kwargs):  # type: ignore[override]
        self._maybe_fail("open_agent")
        self.payloads.append(dict(kwargs))
        self.opened_sessions.append(
            (kwargs["agent_id"], kwargs.get("session"), kwargs.get("fresh", False))
        )
        extra = {
            key: value
            for key, value in kwargs.items()
            if key not in ("agent_id", "attach", "input_unconfirmed_at")
        }
        extra["token"] = "opaque-value-that-must-not-be-printed"
        record = AgentRecord(
            agent_id=kwargs["agent_id"],
            state=self.reply_state,
            exit=self.reply_exit,
            session=self.reply_session,
            attach=self.reply_attach,
            input_unconfirmed_at=self.reply_unconfirmed_at,
            extra=extra,
        )
        self.agents[record.agent_id] = record
        return record


def deps_with(sidecar, *, addresses: dict | None = None) -> tuple[Deps, FakeSidecars]:
    """假 sidecar 依位址的 deps；回 `(deps, factory)`（factory 記下問過的位址）。"""
    factory = FakeSidecars(addresses or {}, default=sidecar)
    deps = Deps(
        angareion=FakeAngareion(),
        sidecar_for=factory,
        now=lambda: NOW,
    )
    return deps, factory


def run_open(env: PmoEnv, deps: Deps, *extra: str):
    return run_cli(["open", TEAM_KEY, *extra], env, deps=deps)


def team_state(env: PmoEnv) -> dict:
    return json.loads(env.team_file().read_text(encoding="utf-8"))


def ready_env(env: PmoEnv, *, overrides: dict | None = None) -> OpenSidecar:
    """一個「已 send、設定齊全」的 team 與一台假 sidecar。

    預設是非互動版（`tui: false`，`agent` 用設定預設的 `claude-code`）；
    互動版的測試自己帶 `{"tui": True}`。
    """
    settings = {"workdir": WORKDIR, "provider": PROVIDER, "model": MODEL}
    if overrides:
        settings.update(overrides)
    write_team_config(env, overrides=settings)
    seed_team_state(env, ticket=TICKET, dispatched_at=DISPATCHED_AT)
    return OpenSidecar()


# --------------------------------------------------------------------------- #
# 1. 正常路徑：POST /agents、印 state 與 attach、寫 team 狀態（§2.1、§2.3）
# --------------------------------------------------------------------------- #


def test_open_prints_state_and_attach_and_writes_the_team_state(pmo_env):
    sidecar = ready_env(pmo_env)
    deps, factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)

    assert (rc, err) == (0, "")
    assert out == f"[open] team={TEAM_KEY} id={TEAM_PM} state=starting attach={ATTACH}\n"
    assert "opaque-value-that-must-not-be-printed" not in out
    assert factory.addresses == [TEAM_SIDECAR]  # 依 team.sidecar 取埠

    (payload,) = sidecar.payloads
    assert payload["agent_id"] == TEAM_PM
    assert payload["agent"] == "claude-code"  # teams 設定的預設值
    assert payload["workdir"] == WORKDIR
    assert payload["provider"] == PROVIDER
    assert payload["model"] == MODEL
    assert payload["tui"] is False
    assert payload["session"] is None
    assert payload["fresh"] is False
    assert payload["agent_args"] is None
    assert sidecar.opened_sessions == [(TEAM_PM, None, False)]

    state = team_state(pmo_env)
    assert state["state"] == "starting"
    assert state["exit"] is None
    assert state["ticket"] == TICKET  # 派工欄位原樣留著
    assert state["dispatched_at"] == DISPATCHED_AT


def test_the_first_input_carries_the_dispatched_at_as_since(pmo_env):
    """§2.1／#356：第一則輸入要帶 team 狀態的 `dispatched_at`（send 寫的 --since）。"""
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    first_input = sidecar.payloads[0]["input"]
    assert f"--since {DISPATCHED_AT}" in first_input
    assert "Angareion" in first_input
    assert first_input == open_module.input_for(DISPATCHED_AT)
    # 互動版把第一則輸入當一行打進去：展開後不能有換行。
    assert "\n" not in first_input
    assert "\n" not in open_module.OPEN_INPUT


def test_open_sends_the_teams_agent(pmo_env):
    """#356 裁定 §2：`agent` 來自 teams 設定（不是寫死），預設是 `claude-code`。"""
    sidecar = ready_env(pmo_env, overrides={"agent": "opencode"})
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["agent"] == "opencode"
    assert sidecar.payloads[0]["tui"] is False


def test_open_of_an_interactive_team_sends_tui(pmo_env):
    """互動版：`tui: true` 進請求；`attach` 印 sidecar 給的 `attach.local` 原樣。"""
    sidecar = ready_env(pmo_env, overrides={"tui": True})
    sidecar.reply_attach = "tmux attach -t demo && echo ready"
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["tui"] is True
    assert out == (
        f"[open] team={TEAM_KEY} id={TEAM_PM} state=starting "
        f"attach=tmux attach -t demo && echo ready\n"
    )


def test_open_sends_agent_args_as_an_appended_tail(pmo_env):
    """#356 裁定 §2：teams 設定的 `agent_args` 以 `agent_args_mode: "append"` 帶上。"""
    sidecar = ready_env(pmo_env, overrides={"agent_args": ["--effort", "high"]})
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["agent_args"] == ("--effort", "high")


def test_open_uses_the_teams_sidecar_not_the_pmos(pmo_env):
    """#356：team 的操作用 team.sidecar；pmo_sidecar 是 PMO 自己的位置。"""
    sidecar = ready_env(pmo_env)
    data = config_data()
    data["pmo_sidecar"] = PMO_SIDECAR
    data["teams"][0].update(
        {"workdir": WORKDIR, "provider": PROVIDER, "model": MODEL}
    )
    pmo_env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    deps, factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert factory.addresses == [TEAM_SIDECAR]


def test_open_without_attach_prints_the_noninteractive_note(pmo_env):
    """#356 裁定 §3：`attach` 為 `null`（非互動版）時印「沒有 attach」。"""
    sidecar = ready_env(pmo_env)
    sidecar.reply_attach = None
    deps, _factory = deps_with(sidecar)
    rc, out, _err = run_open(pmo_env, deps)
    assert rc == 0
    assert " attach=（非互動版，沒有 attach）" in out


def test_open_passes_a_session_through(pmo_env):
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps, "--session", SESSION)
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["session"] == SESSION
    assert sidecar.payloads[0]["fresh"] is False
    assert sidecar.opened_sessions == [(TEAM_PM, SESSION, False)]


def test_open_passes_fresh_through(pmo_env):
    """`--fresh` 只配互動版（tui）；`fresh: true` 進請求，`session` 整個鍵省略。"""
    sidecar = ready_env(pmo_env, overrides={"tui": True})
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps, "--fresh")
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["fresh"] is True
    assert sidecar.payloads[0]["session"] is None
    assert sidecar.payloads[0]["tui"] is True
    assert sidecar.opened_sessions == [(TEAM_PM, None, True)]


def test_open_flags_override_the_team_defaults(pmo_env):
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(
        pmo_env, deps, "--provider", "ollama-cloud", "--model", "deepseek-v4.1-flash"
    )
    assert (rc, err) == (0, "")
    assert (sidecar.payloads[0]["provider"], sidecar.payloads[0]["model"]) == (
        "ollama-cloud",
        "deepseek-v4.1-flash",
    )


def test_open_writes_the_state_under_the_team_lock(pmo_env, monkeypatch):
    """寫回 `state`／`exit` 在 `teamstate.state_lock` 內重讀再改（#369 的形狀）。"""
    ready_env(pmo_env)
    sidecar = OpenSidecar()
    deps, _factory = deps_with(sidecar)

    started = threading.Event()
    release = threading.Event()
    other_done = threading.Event()
    real_save = teamstate.save_state
    first_save = {"pending": True}

    def slow_save(state_dir, key, data):
        # 讓 open 停在「已讀、還沒寫」的窗口
        if first_save["pending"]:
            first_save["pending"] = False
            started.set()
            release.wait(5.0)
        return real_save(state_dir, key, data)

    monkeypatch.setattr(teamstate, "save_state", slow_save)

    result: dict = {}

    def opener():
        result["rc"] = run_open(pmo_env, deps)[0]

    def ack_writer():
        with teamstate.state_lock(pmo_env.state_dir, TEAM_KEY):
            st = teamstate.load_state(pmo_env.state_dir, TEAM_KEY)
            st["ack_at"] = "2026-10-19T10:16:00Z"
            teamstate.save_state(pmo_env.state_dir, TEAM_KEY, st)
        other_done.set()

    opener_thread = threading.Thread(target=opener)
    opener_thread.start()
    assert started.wait(5.0)  # open 已讀、卡在寫之前
    acker = threading.Thread(target=ack_writer)
    acker.start()
    try:
        waited = other_done.wait(0.3)  # 有鎖時 ack 還拿不到鎖
    finally:
        release.set()
    opener_thread.join(5.0)
    acker.join(5.0)

    assert waited is False
    assert result["rc"] == 0
    state = team_state(pmo_env)
    assert state["state"] == "starting"  # open 的寫沒被蓋掉
    assert state["ack_at"] == "2026-10-19T10:16:00Z"  # ack 的寫也沒被蓋掉


# --------------------------------------------------------------------------- #
# 2. 拒絕條件：1 用法或狀態不足（design §3.2）
# --------------------------------------------------------------------------- #


def test_unknown_team_is_exit_1_and_calls_no_sidecar(pmo_env):
    sidecar = OpenSidecar()
    deps, factory = deps_with(sidecar)
    rc, out, err = run_cli(["open", "no-such-team"], pmo_env, deps=deps)
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err
    assert factory.addresses == [] and sidecar.payloads == []


def test_retired_team_is_exit_1_and_calls_no_sidecar(pmo_env):
    sidecar = ready_env(pmo_env, overrides={"retired": True})
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "已退役" in err
    assert sidecar.payloads == []


def test_a_worker_team_is_exit_1_and_calls_no_sidecar(pmo_env):
    """D-8：第一階段 pmo 不碰 worker team 的動作。"""
    sidecar = ready_env(pmo_env, overrides={"sidecar": "worker"})
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "worker" in err and "MyPMO" in err
    assert sidecar.payloads == []


def test_open_without_a_prior_send_is_exit_1(pmo_env):
    """狀態不足（沒先 send）：team 狀態沒有 ticket。"""
    sidecar = OpenSidecar()
    write_team_config(
        pmo_env, overrides={"workdir": WORKDIR, "provider": PROVIDER, "model": MODEL}
    )
    assert not pmo_env.team_file().exists()
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "狀態不足" in err and "pmo send" in err
    assert sidecar.payloads == []


def test_open_without_dispatched_at_is_exit_1(pmo_env):
    """沒有 dispatched_at 就算不出第一則輸入的 --since：不猜，exit 1。"""
    sidecar = ready_env(pmo_env)
    seed_team_state(pmo_env, ticket=TICKET, dispatched_at=None)
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "dispatched_at" in err and "狀態不足" in err
    assert sidecar.payloads == []


def test_open_without_a_workdir_is_exit_1(pmo_env):
    """待確認：design 沒寫 workdir 從哪來；本版讀 team 設定的選填 workdir。"""
    sidecar = ready_env(pmo_env)
    write_team_config(pmo_env, overrides={"provider": PROVIDER, "model": MODEL})
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "workdir" in err
    assert sidecar.payloads == []


def test_open_without_any_provider_or_model_omits_them(pmo_env):
    """#356 裁定 §2：旗標與 team 都沒有時兩個鍵都不帶，sidecar 依政策自己挑。"""
    sidecar = OpenSidecar()
    write_team_config(pmo_env, overrides={"workdir": WORKDIR})
    seed_team_state(pmo_env, ticket=TICKET, dispatched_at=DISPATCHED_AT)
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert out.startswith("[open] ")
    (payload,) = sidecar.payloads
    assert payload["provider"] is None and payload["model"] is None
    # 送出的 JSON 裡整個鍵都不出現：見下面 adapter 的
    # `test_the_agents_post_omits_provider_and_model_when_absent`。


def test_open_with_only_a_provider_is_legal_and_sends_no_model(pmo_env):
    """#356 裁定 §2：`--provider` 可以單獨給（sidecar 的「有 provider、省略 model」）。"""
    sidecar = ready_env(pmo_env, overrides={"provider": None, "model": None})
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps, "--provider", "ollama-cloud")
    assert (rc, err) == (0, "")
    assert sidecar.payloads[0]["provider"] == "ollama-cloud"
    assert sidecar.payloads[0]["model"] is None


def test_a_flag_provider_does_not_inherit_the_teams_model(pmo_env):
    """低-1（#356 修正三）：旗標只給 `--provider` 時，team 的 `model` 不能混進來。

    team 的 provider／model 是一組；旗標覆寫 provider 之後，team 的 model 就不
    再屬於它，混進去會送出「provider 與 model 不屬同一家」的組合。
    """
    sidecar = ready_env(pmo_env)  # team 設定有 PROVIDER + MODEL
    deps, _factory = deps_with(sidecar)
    rc, _out, err = run_open(pmo_env, deps, "--provider", "ollama-cloud")
    assert (rc, err) == (0, "")
    (payload,) = sidecar.payloads
    assert payload["provider"] == "ollama-cloud"
    assert payload["model"] is None


def test_open_with_only_a_model_is_exit_1(pmo_env):
    """單獨的 `--model` 沒有 provider 可配：exit 1，不叫 sidecar（#356 裁定 §2）。"""
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps, "--model", "glm")
    assert rc == 1
    assert out == ""
    assert "--provider" in err and "--model" in err
    assert sidecar.payloads == []


@pytest.mark.parametrize(
    "extra",
    [("--session", SESSION, "--fresh"), ("--session", "")],
)
def test_open_refuses_bad_session_and_fresh_combinations(pmo_env, extra):
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps, *extra)
    assert rc == 1
    assert out == ""
    assert "session" in err
    assert sidecar.payloads == []


def test_open_refuses_a_session_on_an_interactive_team(pmo_env):
    """#356 裁定 §2：tui 不帶 session（sidecar 會 400），在 CLI 層就拒。"""
    sidecar = ready_env(pmo_env, overrides={"tui": True})
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps, "--session", SESSION)
    assert rc == 1
    assert out == ""
    assert "tui" in err and "session" in err
    assert sidecar.payloads == []


def test_open_refuses_fresh_on_a_noninteractive_team(pmo_env):
    """#356 裁定 §2：`--fresh` 只配互動版。"""
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps, "--fresh")
    assert rc == 1
    assert out == ""
    assert "--fresh" in err and "tui" in err
    assert sidecar.payloads == []


def test_a_damaged_team_state_is_exit_2_and_calls_no_sidecar(pmo_env):
    sidecar = ready_env(pmo_env)
    pmo_env.team_file().write_text("{ not json", encoding="utf-8")
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "team 狀態讀不起來" in err
    assert sidecar.payloads == []


# --------------------------------------------------------------------------- #
# 3. sidecar 的拒絕與連不上：一律 2，只印已知欄位（§2.3、D-12）
# --------------------------------------------------------------------------- #


def test_a_400_from_the_sidecar_is_exit_2_and_prints_the_code(pmo_env):
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = SidecarError(
        status=400, code="workdir_not_found", detail="workdir_not_found：找不到目錄"
    )
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "400" in err and "workdir_not_found" in err
    # 沒有成功，就沒有寫 state
    assert "state" not in team_state(pmo_env)


def test_a_409_from_the_sidecar_is_exit_2_and_prints_the_code(pmo_env):
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = SidecarError(status=409, code="close_in_progress")
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "409" in err and "close_in_progress" in err


def test_an_unreachable_sidecar_is_exit_2_with_the_person_tag(pmo_env):
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = SidecarError(detail="sidecar 連不上：Connection refused")
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "[要本人處理] sidecar 不可用" in err


def test_a_missing_sidecar_port_is_exit_2(pmo_env):
    ready_env(pmo_env)
    deps = Deps(angareion=FakeAngareion(), now=lambda: NOW)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "sidecar 埠沒有接上" in err


def test_an_unexpected_sidecar_answer_is_exit_2(pmo_env):
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = PortError("sidecar 回應缺少 state 字串")
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "sidecar 回應不合預期" in err


def test_a_shape_error_on_a_2xx_open_says_the_agent_may_be_open(pmo_env):
    """中-1（#356 修正三）：2xx 但 body 形狀不符，不可報成「sidecar 不可用」。

    `SidecarShapeError` 是 `SidecarError` 的子類；`POST /agents` 若已回 2xx，
    agent 可能已經起來了，字樣要人先 `pmo status` 確認而不是直接重開。
    """
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = SidecarShapeError(
        detail="POST /agents 的輸出不是物件"
    )
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "回應形狀不符" in err
    assert "agent 可能已開" in err and "pmo status" in err
    assert "不可用" not in err and "要本人處理" not in err
    # 沒有成功（形狀讀不懂），就沒有寫 team 狀態
    assert "state" not in team_state(pmo_env)


def test_a_sidecar_error_does_not_leak_a_credential(pmo_env):
    sidecar = ready_env(pmo_env)
    sidecar.errors["open_agent"] = SidecarError(detail=f"boom {FAKE_SECRET}")
    deps, _factory = deps_with(sidecar)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert FAKE_SECRET not in out and FAKE_SECRET not in err
    assert "***" in err


def test_a_state_write_failure_says_the_agent_did_open(pmo_env, monkeypatch):
    """agent 已經起來但狀態寫不進去：仍印 state 與 attach，exit 2 說「已開」。"""
    sidecar = ready_env(pmo_env)
    deps, _factory = deps_with(sidecar)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pmo.open.teamstate.save_state", boom)
    rc, out, err = run_open(pmo_env, deps)
    assert rc == 2
    assert f"[open] team={TEAM_KEY} id={TEAM_PM} state=starting attach={ATTACH}" in out
    assert "agent 已開" in err and "寫不進去" in err
    assert sidecar.payloads != []


# --------------------------------------------------------------------------- #
# 4. `adapters.Sidecar`：POST /agents 的形狀與錯誤解析
# --------------------------------------------------------------------------- #


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Opener:
    def __init__(self, answer=None) -> None:
        self.requests: list[dict] = []
        self.answer = answer if answer is not None else (200, b"{}")

    def open(self, request, timeout=None):
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "data": request.data,
                "timeout": timeout,
            }
        )
        status, body = self.answer
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "err", None, io.BytesIO(body)
            )
        return _Response(status, body)


#: 一個 agent 物件的全部欄位（#356 裁定 §3 的表：真的 sidecar 回這些）。
AGENT_OBJECT: dict = {
    "id": TEAM_PM,
    "agent": "claude-code",
    "session": "agora:01M60WT5NPK2FTZ73Y7Y3WANX9",
    "subscription": None,
    "provider": PROVIDER,
    "model": MODEL,
    "state": "starting",
    "reopen_at": None,
    "started_at": "2026-11-03T10:00:00Z",
    "exit": None,
    "attach": {"local": ATTACH, "ssh": f"ssh demo {ATTACH}", "tmux": ATTACH},
    "input_unconfirmed_at": "2026-11-03T10:00:05Z",
    "role": "pm",
    "data": {"workdir": WORKDIR},
    "awaiting_answer_since": None,
    "usage": {"input_tokens": 1, "output_tokens": 2},
}


def agent_object(**overrides: object) -> dict:
    """AGENT_OBJECT 的一份複本，測試改哪個欄位就覆寫哪個。"""
    return {**AGENT_OBJECT, **overrides}


@pytest.fixture
def opener(monkeypatch):
    fake = _Opener()
    monkeypatch.setattr("pmo.adapters._OPENER", fake)
    return fake


def test_sidecar_open_builds_the_agents_post(opener):
    """#356 裁定 §2：一定帶 id／agent／workdir／input；不帶 subscription。"""
    opener.answer = (201, json.dumps(agent_object()).encode())
    record = Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM,
        agent="claude-code",
        workdir=WORKDIR,
        input="i",
        provider=PROVIDER,
        model=MODEL,
    )

    (request,) = opener.requests
    assert (request["method"], request["url"]) == ("POST", f"{TEAM_SIDECAR}/agents")
    assert json.loads(request["data"]) == {
        "id": TEAM_PM,
        "agent": "claude-code",
        "workdir": WORKDIR,
        "input": "i",
        "provider": PROVIDER,
        "model": MODEL,
    }
    assert record.state == "starting"
    assert record.attach == ATTACH  # 只讀 attach.local
    assert record.input_unconfirmed_at == "2026-11-03T10:00:05Z"
    assert "usage" in record.extra  # 留在物件上；印出去時只挑已知欄位
    assert "subscription" not in json.loads(request["data"])


def test_the_agents_post_omits_provider_and_model_when_absent(opener):
    """#356 裁定 §2：都沒有就省略整個鍵（sidecar 依政策挑）。"""
    opener.answer = (201, json.dumps(agent_object()).encode())
    Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM, agent="claude-code", workdir=WORKDIR, input="i"
    )
    assert json.loads(opener.requests[0]["data"]) == {
        "id": TEAM_PM,
        "agent": "claude-code",
        "workdir": WORKDIR,
        "input": "i",
    }


def test_the_agents_post_carries_tui_and_omits_session_on_a_new_open(opener):
    """#356 裁定 §2：tui 只有 team.tui；新開時 `session` 整個鍵省略（null 會 400）。"""
    opener.answer = (201, json.dumps(agent_object()).encode())
    Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM, agent="claude-code", workdir=WORKDIR, input="i",
        provider=PROVIDER, tui=True, fresh=True,
    )
    payload = json.loads(opener.requests[0]["data"])
    assert payload["tui"] is True and payload["fresh"] is True
    assert "session" not in payload


def test_the_agents_post_carries_agent_args_as_an_appended_tail(opener):
    """#356 裁定 §2：`agent_args` 配 `agent_args_mode: "append"`。"""
    opener.answer = (201, json.dumps(agent_object()).encode())
    Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM, agent="opencode", workdir=WORKDIR, input="i",
        agent_args=("--effort", "high"),
    )
    payload = json.loads(opener.requests[0]["data"])
    assert payload["agent_args"] == ["--effort", "high"]
    assert payload["agent_args_mode"] == "append"


def test_sidecar_open_sends_the_session_when_asked(opener):
    opener.answer = (201, json.dumps(agent_object()).encode())
    Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM,
        agent="claude-code",
        workdir=WORKDIR,
        input="i",
        provider=PROVIDER,
        model=MODEL,
        session=SESSION,
    )
    payload = json.loads(opener.requests[0]["data"])
    assert payload["session"] == SESSION


class _StrictAgentsSidecar(_Opener):
    """假 sidecar，照真的 `core.open` 檢查：缺 `agent`（或空字串）就是 400。

    真的 sidecar 對沒有 `agent` 的 `POST /agents` 每次都是 400 `invalid_request`
    （#356 裁定 §1），假物件若不驗這一欄，測試就看不出「請求漏了 agent」。
    """

    def open(self, request, timeout=None):
        payload = json.loads(request.data or b"{}")
        agent = payload.get("agent")
        if not isinstance(agent, str) or not agent:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "invalid_request",
                None,
                io.BytesIO(json.dumps({"code": "invalid_request"}).encode()),
            )
        return super().open(request, timeout=timeout)


def test_a_post_without_agent_is_400_invalid_request(monkeypatch):
    """負向對照：假 sidecar 真的會擋「沒有 agent」，所以上面的形狀測試不是空的。"""
    monkeypatch.setattr("pmo.adapters._OPENER", _StrictAgentsSidecar((201, b"{}")))
    with pytest.raises(SidecarError) as caught:
        Sidecar(TEAM_SIDECAR).open_agent(
            agent_id=TEAM_PM, agent="", workdir=WORKDIR, input="i"
        )
    assert (caught.value.status, caught.value.code) == (400, "invalid_request")


def test_the_agents_post_passes_the_strict_sidecar(monkeypatch):
    """同一個嚴格的假 sidecar：`pmo open` 真的送出的形狀會被接受（201）。"""
    strict = _StrictAgentsSidecar((201, json.dumps(agent_object()).encode()))
    monkeypatch.setattr("pmo.adapters._OPENER", strict)
    record = Sidecar(TEAM_SIDECAR).open_agent(
        agent_id=TEAM_PM, agent="claude-code", workdir=WORKDIR, input="i"
    )
    assert record.state == "starting"
    assert json.loads(strict.requests[0]["data"])["agent"] == "claude-code"


def test_sidecar_open_refuses_a_response_without_state(opener):
    opener.answer = (200, json.dumps({"id": TEAM_PM}).encode())
    with pytest.raises(PortError, match="state"):
        Sidecar(TEAM_SIDECAR).open_agent(
            agent_id=TEAM_PM,
            agent="claude-code",
            workdir=WORKDIR,
            input="i",
        )


def test_sidecar_list_agents_reads_the_agents_wrapper(opener):
    """#356 裁定 §3：`GET /agents` 的外層是 `{"agents": [...]}`。"""
    opener.answer = (200, json.dumps({"agents": [agent_object()]}).encode())
    (record,) = Sidecar(TEAM_SIDECAR).list_agents()
    assert (record.agent_id, record.state, record.session) == (
        TEAM_PM,
        "starting",
        AGENT_OBJECT["session"],
    )
    assert record.attach == ATTACH
    assert "usage" in record.extra  # 未知欄位留在 extra，不印
    (request,) = opener.requests
    assert (request["method"], request["url"]) == ("GET", f"{TEAM_SIDECAR}/agents")


def test_sidecar_list_agents_refuses_a_bare_array(opener):
    """#356 裁定 §3：只收物件外層；裸陣列是「形狀不符」。"""
    opener.answer = (200, json.dumps([agent_object()]).encode())
    with pytest.raises(SidecarError, match="不是物件"):
        Sidecar(TEAM_SIDECAR).list_agents()


def test_sidecar_list_agents_refuses_a_shape_without_a_list(opener):
    opener.answer = (200, json.dumps({"agents": "nope"}).encode())
    with pytest.raises(SidecarError, match="agent 陣列"):
        Sidecar(TEAM_SIDECAR).list_agents()


def test_sidecar_open_error_keeps_only_the_known_code(opener):
    opener.answer = (
        400,
        json.dumps({"code": "workdir_not_found", "message": "secret-looking detail"}).encode(),
    )
    with pytest.raises(SidecarError) as caught:
        Sidecar(TEAM_SIDECAR).open_agent(
            agent_id=TEAM_PM,
            agent="claude-code",
            workdir=WORKDIR,
            input="i",
        )
    assert (caught.value.status, caught.value.code) == (400, "workdir_not_found")
    assert "secret-looking detail" not in str(caught.value)

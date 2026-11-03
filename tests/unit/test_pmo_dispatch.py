"""`pmo check` 與 `pmo send` 的測試（#110 T-4a；design §3.2、§5.2）。

驗收（工單 #355）：兩列的參數、輸出與每個結束碼；`[wake-hint]` 的四種情況
（比 10m 長、`null`、剛好 10m 與以下、`tick.json` 不存在）；拒絕條件（未知
team、缺 body、需 sanitize 的內文、埠失敗）。全部用假 Angareion／GitHub 與
暫存 state（`tests/conftest.py` 的 `isolated_home`），不打真的後端。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
from datetime import datetime, timezone

import pytest

from pmo import dispatch
from pmo.adapters import GitHub
from pmo.fakes import FakeGithub
from pmo.ports import Message, PortError
from pmo.runtime import Deps

import _pmo_env as E
from _pmo_env import (
    PMO_IDENTITY,
    TEAM_CHANNEL,
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
ISSUE_TITLE = "修好派工的前半"
ISSUE_BODY = "把 check 與 send 做完，測試用假物件。"
INSTRUCTIONS = f"請先回「收到 {TICKET}」，再照工單做。"
BACKEND_TIME = "2026-10-19T10:16:00Z"
#: 回看窗口裡另一封信的後端時刻（比指示信早，方便看出挑錯）。
DECOY_TIME = "2026-10-19T10:15:30Z"
NOW = datetime(2026, 10, 19, 10, 15, 0, tzinfo=timezone.utc)

#: 假 secret：前綴在執行時才組起來（檔案裡不留完整樣式）。
FAKE_SECRET = "sk" + "-ant-" + "x" * 40
#: 假密碼值（secret-field 形狀）：同樣在執行時組起來。
FAKE_PASSWORD_VALUE = "pw" + "y" * 10


class DispatchAngareion(E.FakeAngareion):
    """`send` 把剛送出的信放回 `logged`，讓 `log --channel` 撈得到它。

    真的後端就是這樣：送出的信立刻在自己的 channel 上，`log --channel`
    看得到、也不動游標（#355 高-1：讀回後端時刻走 log，不走 read）。回一個
    假造的後端時刻，測試才驗得到 team 狀態的 `received_at`。
    """

    def __init__(self, *args, received_at: str = BACKEND_TIME, **kwargs):
        super().__init__(*args, **kwargs)
        self.received_at = received_at

    def send(self, **kwargs):  # type: ignore[override]
        message_id = super().send(**kwargs)
        self.logged.append(
            Message(
                message_id=message_id,
                channel=kwargs["channel"],
                sender=PMO_IDENTITY,
                to=kwargs["to"],
                received_at=self.received_at,
                body=kwargs["body"],
                urgency=kwargs.get("urgency"),
                in_reply_to=kwargs.get("in_reply_to"),
            )
        )
        return message_id


class NoReadBackAngareion(DispatchAngareion):
    """`log --channel --since` 找不到剛送出的信（模擬後端還沒寫進去）。"""

    def send(self, **kwargs):  # type: ignore[override]
        return E.FakeAngareion.send(self, **kwargs)  # 不把信放進 logged


class BrokenReadBackAngareion(DispatchAngareion):
    """`log --channel --since` 失敗（後端／網路）；信已經送出去了。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors["log"] = PortError("backend down")


class DecoyFirstAngareion(DispatchAngareion):
    """`log --channel --since` 的回看窗口裡還有別人的信：目標在第二位。

    真的後端只保證「在這段時間裡」，不保證指示信是第一封；`_backend_time`
    因此要用 message_id 挑。這裡先回一封別人的信，再回剛送出的指示信。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.decoy = Message(
            message_id="fake-msg-decoy",
            channel=TEAM_CHANNEL,
            sender=TEAM_PM,
            to=PMO_IDENTITY,
            received_at=DECOY_TIME,
            body="窗口裡另一封信（不是指示信）",
        )

    def send(self, **kwargs):  # type: ignore[override]
        message_id = super().send(**kwargs)
        self.logged.insert(0, self.decoy)
        return message_id


class AddressedAngareion(DispatchAngareion):
    """`read` 照 angareion 的「寄給我的」過濾，`log --channel` 照頻道過濾。

    指示信的 `to` 是 team PM，不在 PMO 的「寄給我的」裡：不帶 `--channel`
    的 `read` 讀不到它（#355 高-1 的實機情況），所以讀回後端時刻只能用
    `log --channel`。
    """

    def read(self, *, since=None):  # type: ignore[override]
        self.read_calls.append(since)
        self._maybe_fail("read")
        mine = [
            message
            for message in self.pending
            if message.to in (None, PMO_IDENTITY)
            or (message.mentions is not None and PMO_IDENTITY in message.mentions)
        ]
        self.pending = [message for message in self.pending if message not in mine]
        return mine


def write_tick(env: PmoEnv, every: str | None = "10m") -> None:
    """寫一份合法的 `tick.json`（`every=None` 代表 PMO 選擇不排叫醒）。"""
    env.state_dir.mkdir(parents=True, exist_ok=True)
    (env.state_dir / "tick.json").write_text(
        json.dumps({"version": 1, "wake": {"every": every}}, ensure_ascii=False),
        encoding="utf-8",
    )


def dispatch_deps(
    env: PmoEnv, *, every: str | None = "10m", tick: bool = True, issue: bool = True
):
    """假 angareion＋假 GitHub＋固定的時鐘；回 `(ang, github, deps)`。"""
    ang = DispatchAngareion()
    github = FakeGithub()
    if issue:
        github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    if tick:
        write_tick(env, every)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)
    return ang, github, deps


def run_send(
    env: PmoEnv,
    deps,
    *,
    ticket: str = TICKET,
    body: str | None = "-",
    stdin_text: str = INSTRUCTIONS,
    identity: str = PMO_IDENTITY,
):
    argv = ["send", TEAM_KEY, ticket]
    if body is not None:
        argv += ["--body", body]
    return run_cli(argv, env, deps=deps, stdin_text=stdin_text, identity=identity)


def team_state(env: PmoEnv) -> dict:
    return json.loads(env.team_file().read_text(encoding="utf-8"))


def rewrite_team_config(env: PmoEnv, **overrides) -> None:
    data = config_data()
    data["teams"][0].update(overrides)
    env.config_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. `pmo send`：送信、讀回後端時刻、寫 team 狀態（design §2.1）
# --------------------------------------------------------------------------- #


def test_send_writes_the_letter_and_the_team_state(pmo_env):
    ang, github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_send(pmo_env, deps, body="-", stdin_text=INSTRUCTIONS)

    assert (rc, err) == (0, "")
    (sent,) = ang.sent
    assert (sent.channel, sent.to, sent.urgency) == (TEAM_CHANNEL, TEAM_PM, 5)
    # 固定頭照 MyPMO `build_instruction`（#355 裁定 2）：工單號、標題、範圍、
    # 驗收、回報格式、派工時刻，然後才是工單本文與正文。
    assert sent.body.startswith(f"## {TICKET} 派工指示\n\n")
    assert f"- 工單號：{TICKET}" in sent.body
    assert f"- 標題：{ISSUE_TITLE}" in sent.body
    assert "- 範圍：見下方指示本文的「範圍」一節" in sent.body
    assert "- 驗收：見下方指示本文的「驗收」一節" in sent.body
    assert "做到哪、半成品在哪、測試實際輸出、剩下的問題、要本人決定的事附建議；" in sent.body
    assert (
        f"  PR 連結直接寫在 body，用 `aicomm send --channel {TEAM_CHANNEL} "
        f"--to {PMO_IDENTITY} --body -`；" in sent.body
    )
    assert "  要本人決定的急件用 `--urgency 8` 以上。" in sent.body
    assert "- 派工時刻：2026-10-19T10:15:00Z（本機時間，僅供對照；" in sent.body
    assert "## 工單本文（PMO 派工時從 GitHub 讀出）\n\n" in sent.body
    assert f"標題：{ISSUE_TITLE}\n\n{ISSUE_BODY}" in sent.body
    assert sent.body.endswith(f"---\n\n{INSTRUCTIONS}\n")
    assert "# 派工：" not in sent.body  # 舊的自訂格式不再用

    assert f"[send] 指示信已送出 id={sent.message_id} 後端時刻={BACKEND_TIME}" in out
    assert "[wake-hint]" not in out  # 預設 10m：不提示

    state = team_state(pmo_env)
    assert state["agent"] == TEAM_PM
    assert state["team"] == TEAM_KEY  # #355 裁定 3
    assert state["ticket"] == TICKET  # team PM 的「收到 ‹工單›」要對得上
    assert state["message_id"] == sent.message_id
    assert state["received_at"] == BACKEND_TIME
    assert state["dispatched_at"] == "2026-10-19T10:15:00Z"  # received_at 往前 60 秒
    assert state["updated_at"] == "2026-10-19T10:15:00Z"  # 寫入時刻
    assert state["closed"] is False
    assert state["ack_at"] is None

    # 讀回後端時刻走唯讀的 `log --channel`，回看 60 秒、不動游標
    assert ang.log_calls == [
        {
            "channel": TEAM_CHANNEL,
            "sender": None,
            "to": None,
            "thread": None,
            "grep": None,
            "since": "2026-10-19T10:14:00Z",
        }
    ]
    assert ang.read_calls == []  # 不走 read（PMO 的讀信只走 tick）
    assert github.calls == [(REPO, NUMBER)]


def test_backend_time_is_picked_by_id_not_by_position(pmo_env):
    """回看窗口有別的信、指示信不在第一位時，取的是指示信的後端時刻。

    `_backend_time` 的 docstring：`log --channel --since` 的回看窗口可能夾著
    別人的信，所以用 message_id 挑，不用位置。挑錯會把別人信的 `received_at`
    寫進 team 狀態，而那是看門狗的計時起點。
    """
    ang = DecoyFirstAngareion()
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert (rc, err) == (0, "")
    (sent,) = ang.sent
    assert DECOY_TIME != BACKEND_TIME
    assert f"[send] 指示信已送出 id={sent.message_id} 後端時刻={BACKEND_TIME}" in out
    assert DECOY_TIME not in out

    state = team_state(pmo_env)
    assert state["message_id"] == sent.message_id
    assert state["received_at"] == BACKEND_TIME  # 指示信的，不是窗口第一封的
    assert ang.read_calls == []
    assert ang.log_calls[0]["since"] == "2026-10-19T10:14:00Z"


def test_send_keeps_unknown_state_fields_and_clears_the_old_watchdog(pmo_env):
    seed_team_state(
        pmo_env,
        ticket="demo#1",
        closed=True,
        ack_at="2026-10-18T00:00:00Z",
        ack_poke_at="2026-10-18T00:01:00Z",
        ack_escalated_at="2026-10-18T00:02:00Z",
        worker_only_field="keep-me",
    )
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, _out, err = run_send(pmo_env, deps)

    assert (rc, err) == (0, "")
    state = team_state(pmo_env)
    assert state["ticket"] == TICKET
    assert state["message_id"] == ang.sent[0].message_id
    assert state["closed"] is False
    assert state["ack_at"] is None
    assert state["ack_poke_at"] is None
    assert state["ack_escalated_at"] is None
    assert state["worker_only_field"] == "keep-me"  # Q-9：不認得的欄位原樣保留
    # #355 裁定 3：新的派工把 dispatched_at 換成這一封的（received_at 往前 60 秒），
    # 不再是舊派工留下的值。
    assert state["dispatched_at"] == "2026-10-19T10:15:00Z"


def test_record_dispatch_reads_and_writes_inside_the_team_lock(pmo_env, monkeypatch):
    """`_record_dispatch` 的讀、改、寫都在 team 鎖內（arch review 中-1）。"""
    import fcntl
    import os

    from pmo import teamstate as teamstate_module

    ang, _github, deps = dispatch_deps(pmo_env)

    def lock_held(state_dir, key):
        fd = os.open(str(teamstate_module.lock_file(state_dir, key)), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    checked: list[tuple[str, bool]] = []
    real_load = teamstate_module.load_state
    real_save = teamstate_module.save_state

    def load_checked(state_dir, key):
        checked.append(("load", lock_held(state_dir, key)))
        return real_load(state_dir, key)

    def save_checked(state_dir, key, data):
        checked.append(("save", lock_held(state_dir, key)))
        return real_save(state_dir, key, data)

    monkeypatch.setattr(teamstate_module, "load_state", load_checked)
    monkeypatch.setattr(teamstate_module, "save_state", save_checked)

    rc, _out, err = run_send(pmo_env, deps)

    assert (rc, err) == (0, "")
    assert ang.sent  # 有真的跑過
    assert {what for what, _held in checked} == {"load", "save"}
    assert all(held for _what, held in checked)


# --------------------------------------------------------------------------- #
# 2. `[wake-hint]` 的四種情況（design §3.2、§5.2 T-4a）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("every", ["30m", "2h"])
def test_wake_hint_prints_when_every_is_longer_than_ten_minutes(pmo_env, every):
    _ang, _github, deps = dispatch_deps(pmo_env, every=every)
    rc, out, err = run_send(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert f"[wake-hint] 看門狗 10m 後到期，目前叫醒 {every}" in out


def test_wake_hint_prints_off_when_every_is_null(pmo_env):
    _ang, _github, deps = dispatch_deps(pmo_env, every=None)
    rc, out, err = run_send(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert "[wake-hint] 看門狗 10m 後到期，目前叫醒 off" in out


def test_wake_hint_treats_a_missing_tick_json_as_null(pmo_env):
    _ang, _github, deps = dispatch_deps(pmo_env, tick=False)
    assert not (pmo_env.state_dir / "tick.json").exists()
    rc, out, err = run_send(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert "[wake-hint] 看門狗 10m 後到期，目前叫醒 off" in out


@pytest.mark.parametrize("every", ["5m", "10m"])
def test_wake_hint_is_silent_at_or_below_ten_minutes(pmo_env, every):
    _ang, _github, deps = dispatch_deps(pmo_env, every=every)
    rc, out, err = run_send(pmo_env, deps)
    assert (rc, err) == (0, "")
    assert "[wake-hint]" not in out


def test_a_damaged_tick_json_does_not_stop_the_dispatch(pmo_env):
    """信已送出：壞檔只 warn、exit 0，叫醒印 `unknown` 不印 `off`（#355 裁定 4）。"""
    _ang, _github, deps = dispatch_deps(pmo_env, tick=False)
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    (pmo_env.state_dir / "tick.json").write_text("{ not json", encoding="utf-8")
    rc, out, err = run_send(pmo_env, deps)
    assert (rc) == 0
    assert "[send] 指示信已送出" in out
    assert "[wake-hint] 看門狗 10m 後到期，目前叫醒 unknown（tick.json 讀不起來）" in out
    assert "叫醒 off" not in out
    assert "[warn] tick.json 讀不起來：" in err
    assert "視為叫醒 off" not in err


# --------------------------------------------------------------------------- #
# 3. 拒絕條件：未知 team、退役、缺 body、需 sanitize 的內文、工單讀不到
# --------------------------------------------------------------------------- #


def test_unknown_team_is_exit_1_and_sends_nothing(pmo_env):
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_cli(
        ["send", "no-such-team", TICKET, "--body", "-"],
        pmo_env,
        deps=deps,
        stdin_text=INSTRUCTIONS,
    )
    assert rc == 1
    assert out == ""
    assert "未知的 team" in err
    assert ang.sent == []


def test_retired_team_is_exit_1_and_sends_nothing(pmo_env):
    rewrite_team_config(pmo_env, retired=True)
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_send(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "已退役" in err
    assert ang.sent == []
    assert not pmo_env.team_file().exists()


@pytest.mark.parametrize("argv", [["send", TEAM_KEY, TICKET], ["send", TEAM_KEY, TICKET, "--body", ""]])
def test_a_missing_or_empty_body_is_exit_1(pmo_env, argv):
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_cli(argv, pmo_env, deps=deps, stdin_text="")
    assert rc == 1
    assert out == ""
    assert "--body" in err
    assert ang.sent == []


def test_body_with_a_credential_shape_is_refused(pmo_env):
    ang, _github, deps = dispatch_deps(pmo_env)
    body = f"順便附上金鑰：{FAKE_SECRET}"
    rc, out, err = run_send(pmo_env, deps, body="-", stdin_text=body)

    assert rc == 1
    assert ang.sent == []
    assert FAKE_SECRET not in out and FAKE_SECRET not in err
    assert "正文含憑證樣式" in err


def test_issue_body_with_a_credential_shape_is_refused(pmo_env):
    """工單本文也會原樣進信裡，所以要掃；訊息指出是「工單本文」那一段。"""
    ang = DispatchAngareion()
    github = FakeGithub()
    github.add_issue(
        REPO, NUMBER, title=ISSUE_TITLE, body=f"貼錯的金鑰：{FAKE_SECRET}"
    )
    write_tick(pmo_env)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert rc == 1
    assert ang.sent == []
    assert not pmo_env.team_file().exists()
    assert FAKE_SECRET not in out and FAKE_SECRET not in err
    assert "工單本文含憑證樣式，拒送；請先改工單" in err
    assert github.calls == [(REPO, NUMBER)]  # 讀過工單才掃得到本文


def test_issue_title_with_a_credential_shape_is_refused(pmo_env):
    """工單標題也原樣進信（兩處），所以要掃；訊息指出是「工單標題」那一段。"""
    ang = DispatchAngareion()
    github = FakeGithub()
    github.add_issue(
        REPO, NUMBER, title=f"順手貼到標題：{FAKE_SECRET}", body=ISSUE_BODY
    )
    write_tick(pmo_env)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert rc == 1
    assert ang.sent == []
    assert not pmo_env.team_file().exists()
    assert FAKE_SECRET not in out and FAKE_SECRET not in err
    assert "工單標題含憑證樣式，拒送；請先改工單" in err


def test_body_with_a_secret_field_shape_is_refused(pmo_env):
    """`password: …` 這類形狀也要拒送，不只 TOKEN_SHAPES（#355 裁定 5）。"""
    ang, _github, deps = dispatch_deps(pmo_env)
    body = f"連線設定：password: {FAKE_PASSWORD_VALUE}"
    rc, out, err = run_send(pmo_env, deps, body="-", stdin_text=body)

    assert rc == 1
    assert ang.sent == []
    assert FAKE_PASSWORD_VALUE not in out and FAKE_PASSWORD_VALUE not in err
    assert "正文含憑證樣式" in err


def test_issue_body_with_a_secret_field_shape_is_refused(pmo_env):
    """工單本文的 secret-field 形狀也拒送；訊息指出是「工單本文」那一段。"""
    ang = DispatchAngareion()
    github = FakeGithub()
    github.add_issue(
        REPO, NUMBER, title=ISSUE_TITLE, body=f"貼錯的設定：password: {FAKE_PASSWORD_VALUE}"
    )
    write_tick(pmo_env)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert rc == 1
    assert ang.sent == []
    assert not pmo_env.team_file().exists()
    assert FAKE_PASSWORD_VALUE not in out and FAKE_PASSWORD_VALUE not in err
    assert "工單本文含憑證樣式，拒送；請先改工單" in err
    assert github.calls == [(REPO, NUMBER)]


def test_a_letter_over_the_limit_is_refused_without_truncating(pmo_env):
    """組好的信超過 65536 字元就拒送，訊息寫明工單本文多長（#355 裁定 2）。"""
    long_body = "x" * (dispatch.MAX_LETTER_CHARS + 1000)
    ang = DispatchAngareion()
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=long_body)
    write_tick(pmo_env)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert rc == 1
    assert ang.sent == []
    assert not pmo_env.team_file().exists()
    assert str(dispatch.MAX_LETTER_CHARS) in err
    assert f"工單本文 {len(long_body)} 字元" in err
    assert "不截斷" in err


def test_backend_time_is_read_with_log_on_the_team_channel(pmo_env):
    """`read`（寄給我的）讀不到寄給 team PM 的信；`log --channel` 讀得到。

    #355 高-1：指示信的 `to` 是 team PM，所以不帶 `--channel` 的 `read`
    在實機上永遠找不到它；讀回後端時刻只能走唯讀的 `log --channel`。
    """
    ang = AddressedAngareion()
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert (rc, err) == (0, "")
    (sent,) = ang.sent
    assert sent.to == TEAM_PM
    # 這次 send 沒有用 read 讀回任何東西
    assert ang.read_calls == []
    # log 限定 team channel、回看 60 秒
    assert ang.log_calls == [
        {
            "channel": TEAM_CHANNEL,
            "sender": None,
            "to": None,
            "thread": None,
            "grep": None,
            "since": "2026-10-19T10:14:00Z",
        }
    ]
    # 反過來驗：同一個 fake 的 read（「寄給我的」）讀不到那封信
    assert ang.read(since="2026-10-19T10:14:00Z") == []
    # 而 log --channel 讀得到
    assert [
        message.message_id
        for message in ang.log(channel=TEAM_CHANNEL, since="2026-10-19T10:14:00Z")
    ] == [sent.message_id]


def test_a_bad_backend_time_is_sent_but_not_recorded(pmo_env):
    """後端回的時刻看不懂時算不出 dispatched_at：exit 2、帶 id、不要重送。"""
    ang = DispatchAngareion(received_at="not-a-time")
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)

    assert rc == 2
    assert out == ""
    assert "已送出" in err and "不要直接重送" in err
    assert ang.sent[0].message_id in err
    assert not pmo_env.team_file().exists()


def test_a_life_team_gets_the_life_reporting_lines(pmo_env):
    rewrite_team_config(pmo_env, life=True)
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, _out, err = run_send(pmo_env, deps)

    assert (rc, err) == (0, "")
    body = ang.sent[0].body
    assert "做到哪、產出位置（工作目錄／檔案路徑）、驗證輸出（若有）、剩下的問題、" in body
    assert "回報用 `aicomm send --channel " in body
    assert "PR 連結直接寫在 body" not in body
    assert "`--urgency 8` 以上" in body


def test_an_unreadable_issue_is_exit_1_and_sends_nothing(pmo_env):
    ang, _github, deps = dispatch_deps(pmo_env, issue=False)
    rc, out, err = run_send(pmo_env, deps)
    assert rc == 1
    assert out == ""
    assert "工單讀不到" in err
    assert ang.sent == []
    assert not pmo_env.team_file().exists()


@pytest.mark.parametrize(
    "ticket", ["demo/repo", "demo/repo#", "demo/repo#0", "#7", "demo#7", "demo/repo#x"]
)
def test_a_malformed_ticket_is_exit_1(pmo_env, ticket):
    ang, github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_send(pmo_env, deps, ticket=ticket)
    assert rc == 1
    assert out == ""
    assert "owner/repo" in err
    assert ang.sent == []
    assert github.calls == []


def test_send_under_another_identity_is_exit_2_and_sends_nothing(pmo_env):
    ang, _github, deps = dispatch_deps(pmo_env)
    rc, out, err = run_send(pmo_env, deps, identity=E.OTHER_IDENTITY_WRONG)
    assert rc == 2
    assert out == ""
    assert "身分不符拒送" in err
    assert ang.sent == []


# --------------------------------------------------------------------------- #
# 4. 埠失敗（send 是 2、工單讀不到是 1）與「已送出但讀不回」的明說
# --------------------------------------------------------------------------- #


def test_angareion_send_failure_is_exit_2_and_writes_no_state(pmo_env):
    ang, _github, deps = dispatch_deps(pmo_env)
    ang.errors["send"] = PortError("backend down")
    rc, out, err = run_send(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "指示信送不出去" in err
    assert not pmo_env.team_file().exists()


def test_missing_backend_time_says_sent_and_not_to_resend(pmo_env):
    ang = NoReadBackAngareion()
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "已送出" in err and "不要直接重送" in err
    assert ang.sent[0].message_id in err
    assert not pmo_env.team_file().exists()


def test_backend_time_read_failure_says_sent_and_not_to_resend(pmo_env):
    ang = BrokenReadBackAngareion()
    github = FakeGithub()
    github.add_issue(REPO, NUMBER, title=ISSUE_TITLE, body=ISSUE_BODY)
    deps = Deps(angareion=ang, github=github, now=lambda: NOW)

    rc, out, err = run_send(pmo_env, deps)
    assert rc == 2
    assert out == ""
    assert "已送出" in err and "不要直接重送" in err
    assert not pmo_env.team_file().exists()


# --------------------------------------------------------------------------- #
# 5. `pmo check`：逐項 OK／FAIL、0 全過／1 有 FAIL（design §3.2）
# --------------------------------------------------------------------------- #


def test_check_passes_with_no_state_files_yet(pmo_env):
    deps = Deps(angareion=pmo_env.ang, now=lambda: NOW)
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env, deps=deps)
    assert (rc, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == f"OK team：設定裡有 team「{TEAM_KEY}」，未退役"
    assert lines[1] == f"OK pm：PM 名合法且角色是 pm（{TEAM_PM}）"
    assert lines[2] == "OK state：team 狀態檔讀得起來（尚未派工）"
    assert lines[3] == "OK tick：tick.json 不存在（叫醒視為 off）"
    assert lines[4] == f"OK channel：channel 讀得到（{TEAM_CHANNEL}）"
    # channel 檢查是唯讀的 `log --channel --since 現在`（不動游標）
    assert pmo_env.ang.log_calls == [
        {
            "channel": TEAM_CHANNEL,
            "sender": None,
            "to": None,
            "thread": None,
            "grep": None,
            "since": "2026-10-19T10:15:00Z",
        }
    ]
    assert pmo_env.ang.read_calls == []


def test_check_reports_the_existing_ticket_and_the_wake_interval(pmo_env):
    seed_team_state(pmo_env, ticket=TICKET)
    write_tick(pmo_env, "30m")
    rc, out, _err = run_cli(["check", TEAM_KEY], pmo_env)
    assert rc == 0
    assert f"OK state：team 狀態檔讀得起來（ticket={TICKET}）" in out
    assert "OK tick：tick.json 讀得起來（叫醒 30m）" in out


def test_check_unknown_team_is_a_fail_and_exit_1(pmo_env):
    rc, out, _err = run_cli(["check", "no-such-team"], pmo_env)
    assert rc == 1
    assert out.strip() == "FAIL team：設定裡沒有 team「no-such-team」"


def test_check_retired_team_is_a_fail(pmo_env):
    rewrite_team_config(pmo_env, retired=True)
    rc, out, _err = run_cli(["check", TEAM_KEY], pmo_env)
    assert rc == 1
    assert f"FAIL team：設定裡有 team「{TEAM_KEY}」，已退役（retired=true）" in out


def test_check_a_pm_name_whose_role_is_not_pm_is_a_fail(pmo_env):
    """PM 名合法但角色不是 pm：設定載入不會擋（key／channel 仍一致），check 要擋。"""
    rewrite_team_config(pmo_env, pm="demo2-impl-m1-1")
    rc, out, _err = run_cli(["check", TEAM_KEY], pmo_env)
    assert rc == 1
    assert "FAIL pm：PM 名的角色是 impl，不是 pm（demo2-impl-m1-1）" in out


def test_check_reports_a_damaged_team_state(pmo_env):
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    pmo_env.team_file().write_text("{ not json", encoding="utf-8")
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env)
    assert (rc, err) == (1, "")
    assert "FAIL state：team 狀態檔讀不起來" in out


def test_check_reports_a_damaged_tick_state(pmo_env):
    pmo_env.state_dir.mkdir(parents=True, exist_ok=True)
    (pmo_env.state_dir / "tick.json").write_text("{ not json", encoding="utf-8")
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env)
    assert (rc, err) == (1, "")
    assert "FAIL tick：tick.json 讀不起來" in out


def test_check_channel_not_readable_is_a_fail(pmo_env):
    """angareion 回 exit 1（channel 名字錯／還沒落地）是一種 FAIL。"""
    pmo_env.ang.errors["log"] = PortError("no such channel", returncode=1)
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env)
    assert (rc, err) == (1, "")
    assert "FAIL channel：channel 讀不到（還沒落地或名字錯）" in out


@pytest.mark.parametrize("returncode", [2, None])
def test_check_a_backend_error_on_the_channel_is_a_fail(pmo_env, returncode):
    """其他非 0（含沒有結束碼的失敗）是「後端錯誤」，不是名字錯。"""
    pmo_env.ang.errors["log"] = PortError("backend down", returncode=returncode)
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env)
    assert (rc, err) == (1, "")
    assert "FAIL channel：後端錯誤：backend down" in out


def test_check_a_worker_team_prints_the_transition_line_and_passes(pmo_env):
    """worker team 的 sidecar 檢查由過渡工具負責：印一行資訊列，不算 FAIL（D-8）。"""
    rewrite_team_config(pmo_env, sidecar="worker")
    rc, out, err = run_cli(["check", TEAM_KEY], pmo_env)
    assert (rc, err) == (0, "")
    assert out.splitlines()[-1] == (
        "--  worker team：worker 側的檢查由過渡工具負責（D-8）"
    )


def test_check_a_local_team_has_no_worker_line(pmo_env):
    rc, out, _err = run_cli(["check", TEAM_KEY], pmo_env)
    assert rc == 0
    assert "worker team" not in out


# --------------------------------------------------------------------------- #
# 6. 真接線：`adapters.GitHub.issue_view` 的旗面與失敗
# --------------------------------------------------------------------------- #


class _Proc:
    def __init__(self, stdout: str = "{}", returncode: int = 0, stderr: str = ""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def gh_runs(monkeypatch):
    """記下 `gh` 的 argv，回放指定的 stdout／結束碼。"""
    calls: list[list[str]] = []
    reply: dict = {"stdout": "{}", "returncode": 0, "stderr": ""}

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return _Proc(
            stdout=reply["stdout"],
            returncode=reply["returncode"],
            stderr=reply["stderr"],
        )

    monkeypatch.setattr("pmo.adapters.subprocess.run", _fake_run)
    return calls, reply


def test_adapter_issue_view_builds_the_gh_command(gh_runs):
    calls, reply = gh_runs
    reply["stdout"] = json.dumps({"title": ISSUE_TITLE, "body": ISSUE_BODY})
    issue = GitHub().issue_view(REPO, NUMBER)
    assert (issue.title, issue.body) == (ISSUE_TITLE, ISSUE_BODY)
    (argv,) = calls
    assert argv == [
        "gh",
        "issue",
        "view",
        str(NUMBER),
        "--repo",
        REPO,
        "--json",
        "title,body",
    ]


def test_adapter_issue_view_reads_a_null_body_as_empty(gh_runs):
    _calls, reply = gh_runs
    reply["stdout"] = json.dumps({"title": ISSUE_TITLE, "body": None})
    assert GitHub().issue_view(REPO, NUMBER).body == ""


def test_adapter_issue_view_refuses_a_non_json_answer(gh_runs):
    _calls, reply = gh_runs
    reply["stdout"] = "not json"
    with pytest.raises(PortError, match="不是 JSON"):
        GitHub().issue_view(REPO, NUMBER)


def test_adapter_issue_view_refuses_a_missing_title(gh_runs):
    _calls, reply = gh_runs
    reply["stdout"] = json.dumps({"body": "x"})
    with pytest.raises(PortError, match="title"):
        GitHub().issue_view(REPO, NUMBER)


def test_adapter_issue_view_carries_the_exit_code(gh_runs):
    _calls, reply = gh_runs
    reply.update(returncode=2, stderr="gh: not found")
    with pytest.raises(PortError) as caught:
        GitHub().issue_view(REPO, NUMBER)
    assert caught.value.returncode == 2
    assert "gh exit 2" in str(caught.value)


# --------------------------------------------------------------------------- #
# 7. 純函式：工單字串與 wake-hint 的邊界
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("owner/repo#1", ("owner/repo", 1)),
        ("demo-x/repo.y#42", ("demo-x/repo.y", 42)),
    ],
)
def test_parse_ticket_accepts_owner_repo_and_number(text, expected):
    assert dispatch.parse_ticket(text) == expected


def test_hint_uses_strictly_longer_than_the_watchdog():
    assert dispatch.hint_for(None) == "off"
    assert dispatch.hint_for("10m") is None
    assert dispatch.hint_for("11m") == "11m"

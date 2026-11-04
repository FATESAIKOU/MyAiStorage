"""Shared fixtures and fakes for the pmo inbox tests（#110 T-2）.

移植自 MyPMO `tests/dispatch/helpers.py` 的收件匣部分：同一個「假 angareion＋
假 teams 設定」的測試環境，但身分、頻道與團隊全部走假值（不碰真的 channel、
真的身分與真的 state 目錄），經 `tests/conftest.py` 的 `isolated_home` 隔離。
"""

from __future__ import annotations

import json as _json
import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace

import pytest

from pmo import cli, ledger
from pmo.config import DEFAULT_WAKE_TEXT
from pmo.fakes import FakeAngareion, FakeSidecar, FakeSidecars
from pmo.ports import JobRecord, Message, PortError
from pmo.runtime import Deps

#: 假的 PMO／秘書身分與頻道（都是合法 AI 名與 slug；不含任何真環境的值）。
PMO_IDENTITY = "demo-pm-m1"
SECRETARY_IDENTITY = "demo-secretary-m1"
DISPATCH_CHANNEL = "dispatch-demo"
TEAM_KEY = "demo2-m1"
TEAM_PM = "demo2-pm-m1"
TEAM_CHANNEL = "team-demo2-m1"
OTHER_PM = "demo2-pm-w1"
OTHER_IDENTITY_WRONG = "demo-pm-m2"
OTHER_CHANNEL = "team-chat"

ULID_1 = "01JX9A1B2C3D4E5F6G7H8J9K0M"
ULID_2 = "01JX9A1B2C3D4E5F6G7H8J9K0N"
ULID_3 = "01JX9A1B2C3D4E5F6G7H8J9K2Q"

VALID_WORK_BODY = f"""# 工作：修復測試 43 邊界斷言

- 工作 ID：{ULID_1}
- 來源：手機端秘書
- 派工者：本人
- 建議層級：c

## 需求說明
測試 43 的擁有者斷言要是最後一個動作，
範圍檢查不能被繞過，請協助補齊測試並開 PR。

## 限制與備註
請指派給 team 實作，需通過非作者獨立審查。
"""

OTHER_WORK_BODY = VALID_WORK_BODY.replace(ULID_1, ULID_2)


def config_data(**overrides: object) -> dict:
    """一份合法的 teams 設定（測試各自的假值）。"""
    data: dict = {
        "version": 1,
        "pmo_identity": PMO_IDENTITY,
        "secretary_identity": SECRETARY_IDENTITY,
        "dispatch_channel": DISPATCH_CHANNEL,
        "teams": [
            {
                "key": TEAM_KEY,
                "pm": TEAM_PM,
                "channel": TEAM_CHANNEL,
                "sidecar": "http://127.0.0.1:8472",  # 假埠：不是真的 sidecar 埠
                "tui": False,
                "life": False,
                "retired": False,
            }
        ],
    }
    data.update(overrides)
    return data


@dataclass
class PmoEnv:
    """一個測試的收件環境：假設定檔、假 state 目錄、假 angareion。"""

    tmp_path: object
    monkeypatch: object
    config_path: object = None
    state_dir: object = None
    ang: FakeAngareion = field(default_factory=FakeAngareion)

    # -- angareion ---------------------------------------------------------- #

    def sends(self) -> list:
        """每一次送出去的訊息（等價 MyPMO tests 的 `_sends(env)`）。"""
        return list(self.ang.sent)

    # -- state ------------------------------------------------------------- #

    def inbox_file(self):
        return self.state_dir / "secretary-inbox.json"

    def team_file(self):
        return self.state_dir / f"{TEAM_KEY}.json"


@pytest.fixture
def pmo_env(tmp_path, monkeypatch):
    """設定與 state 指到暫存目錄；身分是 PMO 本席（autouse 的下一層）。"""
    env = PmoEnv(tmp_path=tmp_path, monkeypatch=monkeypatch)
    env.config_path = tmp_path / "teams.json"
    env.state_dir = tmp_path / "state"
    env.config_path.write_text(_json.dumps(config_data(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PMO_SKILL_CONFIG", str(env.config_path))
    monkeypatch.setenv("PMO_SKILL_STATE_DIR", str(env.state_dir))
    # 身分守門用 alternative：測試要驗不符時自己 monkeypatch.setenv 覆蓋。
    return env


def secretary_msg(
    ulid: str = ULID_1,
    content: str = VALID_WORK_BODY,
    channel: str = DISPATCH_CHANNEL,
    sender: str = SECRETARY_IDENTITY,
    received_at: str = "2026-10-19T10:15:30Z",
    **extra,
) -> Message:
    """一則派工信（合規樣板預設）。"""
    return Message(
        message_id=ulid,
        channel=channel,
        sender=sender,
        to=PMO_IDENTITY,
        received_at=received_at,
        body=content,
        urgency=5,
        **extra,
    )


class ReplayAngareion(FakeAngareion):
    """每次 `read` 都重發同一批信：模擬游標重來／`--since` 補讀（MyPMO 假腳本的做法）。"""

    def __init__(self, messages=None):
        super().__init__(messages)
        self._original = list(messages or [])

    def read(self, *, since=None):
        got = super().read(since=since)
        self.pending = list(self._original)
        return got


def run_cli(
    argv: list[str],
    env: PmoEnv,
    msgs: list[Message] | None = None,
    deps=None,
    replay: bool = False,
    stdin_text: str = "",
    identity: str = PMO_IDENTITY,
):
    """跑一次 `pmo …` 指令；回 (rc, out, err)。用了假 angareion，不碰真的通道。"""
    env.monkeypatch.setenv("ANGAREION_IDENTITY", identity)
    if msgs is not None:
        env.ang = ReplayAngareion(msgs) if replay else FakeAngareion(msgs)
    if deps is None:
        deps = Deps(angareion=env.ang)
    out, err, inp = StringIO(), StringIO(), StringIO(stdin_text)
    rc = cli.main(argv, deps=deps, stdout=out, stderr=err, stdin=inp)
    return rc, out.getvalue(), err.getvalue()


def set_seat(monkeypatch, identity: str = PMO_IDENTITY) -> None:
    """The seat a test runs as（直接呼叫送信函式的測試也照守門）。"""
    monkeypatch.setenv("ANGAREION_IDENTITY", identity)


def item_data(ulid: str = ULID_1, **extra) -> dict:
    """一筆帳本項目的欄位（schema 必要的都在），`extra` 覆寫。"""
    item = {
        "id": ulid,
        "title": "t",
        "level": "c",
        "sender": SECRETARY_IDENTITY,
        "received_at": "2026-10-19T10:15:30Z",
        "ack_at": None,
        "status": "scheduled",
        "schedule": "排入待開工佇列",
        "issue": None,
        "team": None,
        "reported_at": None,
    }
    item.update(extra)
    return item


def seed_item(env: PmoEnv, ulid: str = ULID_1, **extra) -> None:
    """預置一筆已收件的工作到帳本（含 `notified`，schema 必要）。"""
    seed_items(env, [item_data(ulid, **extra)])


def seed_items(env: PmoEnv, items: list[dict]) -> None:
    """預置多筆（一次寫好；順序類的測試才不會互相蓋掉）。"""
    ledger.save_state(
        env.state_dir,
        {
            "version": 1,
            "updated_at": None,
            "items": {item["id"]: item for item in items},
            "notified": {"call": {}, "poke": {}},
        },
    )


def seed_team_state(env: PmoEnv, ticket: str = "demo#45", **extra) -> None:
    """預置一份 team state 檔（有派工但尚未 ack）。"""
    import json as _j

    env.state_dir.mkdir(parents=True, exist_ok=True)
    st = {
        "agent": TEAM_PM,
        "ticket": ticket,
        "closed": False,
        "ack_at": None,
        "dispatched_at": "2026-10-19T10:00:00Z",
    }
    st.update(extra)
    env.team_file().write_text(_j.dumps(st, ensure_ascii=False), encoding="utf-8")


def a_port_error(message: str = "network failure") -> PortError:
    return PortError(message)


# --------------------------------------------------------------------------- #
# tick（#358 T5-2）：假時鐘、tick.json 種子
# --------------------------------------------------------------------------- #

#: The one "now" the tick tests use; the state files are seeded around it.
NOW = datetime(2026, 10, 19, 10, 15, 0, tzinfo=timezone.utc)


def fixed_now(moment: datetime = NOW):
    """A `Deps.now` that always answers the same moment (the tick tests' clock)."""
    return lambda: moment


def tick_file(env: PmoEnv):
    return env.state_dir / "tick.json"


def seed_tick_state(env: PmoEnv, **fields: object) -> None:
    """Write a `tick.json` (the schema's defaults, then `fields` on top).

    Only the fields a test names are changed; `wake.every` stays `null` (the
    PMO chose not to schedule), so a tick with a plain seed is a quiet one.
    """
    state: dict = {
        "version": 1,
        "last_tick_at": None,
        "handoffs": {},
        "worker_poll": {},
        "wake": {
            "every": None,
            "set_at": None,
            "missing_reported_at": None,
            "error_reported": {},
            "orphan_reported_at": None,
        },
    }
    state.update(fields)
    env.state_dir.mkdir(parents=True, exist_ok=True)
    tick_file(env).write_text(
        _json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_tick_state(env: PmoEnv) -> dict:
    return _json.loads(tick_file(env).read_text(encoding="utf-8"))


def read_team_state(env: PmoEnv, key: str = TEAM_KEY) -> dict:
    return _json.loads((env.state_dir / f"{key}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# wake（#358 T5-4）：假 sidecar 的 job、`pmo wake` 的設定與時鐘
# --------------------------------------------------------------------------- #

#: 假的 PMO sidecar 位址（與 team 的 8472 不同一台；全是假埠）。
PMO_SIDECAR = "http://127.0.0.1:8499"
WAKE_JOB = "job-own"
OTHER_TEXT = "別人排的一句話"
TICKET = "demo#45"


def set_config(env: PmoEnv, *, pmo_sidecar: str | None = PMO_SIDECAR) -> None:
    """寫一份假設定；預設把 PMO 放進 sidecar（`pmo_sidecar`）。"""
    data = config_data()
    if pmo_sidecar is not None:
        data["pmo_sidecar"] = pmo_sidecar
    env.config_path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_deps(sidecar: FakeSidecar | None = None):
    """假 sidecar（每個位址都回同一個）＋假 angareion＋固定時鐘。"""
    sidecar = sidecar if sidecar is not None else FakeSidecar()
    deps = Deps(
        angareion=FakeAngareion(),
        sidecar_for=FakeSidecars({}, default=sidecar),
        now=fixed_now(NOW),
    )
    return deps, sidecar


def put_job(
    sidecar: FakeSidecar,
    job_id: str = WAKE_JOB,
    *,
    every: object = 600.0,
    text: str = DEFAULT_WAKE_TEXT,
    agent_id: str = PMO_IDENTITY,
    action: str = "poke",
    last_run: object = None,
    last_error: str | None = None,
    auto: bool = False,
    at: str | None = None,
    next_run_at: str | None = None,
) -> JobRecord:
    """一個 sidecar job，形狀照 `GET /jobs`（`every` 是秒數；`auto` 是欄位）。"""
    extra: dict = {}
    if auto:
        extra["auto"] = True
    if next_run_at is not None:
        extra["next_run_at"] = next_run_at
    job = JobRecord(
        job_id=job_id,
        action=action,
        args={"id": agent_id, "text": text, "interrupt": False},
        every=None if every is None else (every if isinstance(every, str) else float(every)),
        last_run=last_run,
        last_error=last_error,
        at=at,
        extra=extra,
    )
    sidecar.jobs[job_id] = job
    return job


def own_jobs(sidecar: FakeSidecar) -> list[JobRecord]:
    """這個測試裡「自己的」job（句子是固定句、不是 auto）——測試端的獨立算法。"""
    return [
        job
        for job in sidecar.jobs.values()
        if job.action == "poke"
        and job.args.get("text") == DEFAULT_WAKE_TEXT
        and not job.extra.get("auto")
    ]


def run_wake(env: PmoEnv, deps: Deps, *argv: str):
    return run_cli(["wake", *argv], env, deps=deps)


def run_tick(env: PmoEnv, deps: Deps):
    return run_cli(["tick"], env, deps=deps)


def record_calls(sidecar: FakeSidecar) -> list[str]:
    calls: list[str] = []
    sidecar.on_call = calls.append
    return calls

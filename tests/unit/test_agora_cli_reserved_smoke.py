"""`agora find`／`agora show` 對**預留**（reserved）的顯示（review-2bc0785 M2）。

預留是 `agora checkout` 替新 session 準備好、還沒有人真正開工的空紀錄。讀取介面
要能把它和「已經在跑」的工作分開：

- `agora find --status reserved` 只列出預留，命中時附上期限；
- `agora show` 顯示預留與期限，期限過了就標 `expired`。

**期 1 不自動刪除過期的預留**（PM 裁決）：期限只是顯示與管理用的訊號，刪除真本裡
的項目是管理操作（`admin erase`／rollback），由人決定。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aistorage.agora_cli.__main__ import build_parser, cmd_find, cmd_show
from aistorage.clock import FixedClock

NOW = "2026-09-27T09:10:00.000Z"
SID = "opencode:ses_reserved"


class FakeSessionRow:
    def __init__(self, status: str, reserved_until: str | None) -> None:
        self.session_id = SID
        self.source = "opencode"
        self.title = "接手甲的工作"
        self.producer = "profile:mac-opencode"
        self.case_id = None
        self.status = status
        self.stopped_at = None
        self.in_progress = False
        self.created_at = "2026-09-27T09:00:00.000Z"
        self.updated_at = "2026-09-27T09:00:00.000Z"
        self.snapshot_at = "2026-09-27T09:00:00.000Z"
        self.raw_sha256 = "a" * 64
        self.raw_size = 10
        self.parent_id = None
        self.reading_status = "ok"
        self.reading_error_code = None
        self.committed_at = "2026-09-27T09:00:00.000Z"
        self.reserved_until = reserved_until


class FakeReader:
    """只支援 `find_sessions`／`get_session`（cmd_find／cmd_show 用到的那幾個）。"""

    def __init__(self, row: FakeSessionRow, now: str = NOW) -> None:
        self.row = row
        self._clock = FixedClock(now)
        self.last_query = None

    def find_sessions(self, query, *, max_lag=None):
        self.last_query = query
        value = ([SimpleNamespace(
            hit=SimpleNamespace(session=self.row, matches=()),
            freshness=SimpleNamespace(
                snapshot_at=self.row.snapshot_at, generation=1,
                published_at=NOW, satisfied=None, warning=None, stopped_ok=False),
        )] if (query.status is None or query.status == self.row.status) else [])
        return SimpleNamespace(value=value, freshness=SimpleNamespace(
            snapshot_at=self.row.snapshot_at, generation=1, published_at=NOW,
            satisfied=None, warning=None, stopped_ok=False))

    def get_session(self, session_id, *, max_lag=None):
        return SimpleNamespace(
            value=SimpleNamespace(
                session=self.row, links_out=(), links_in=(),
                handoffs_targeting=(), handoffs_by_holder=(), snapshots=()),
            freshness=SimpleNamespace(
                snapshot_at=self.row.snapshot_at, generation=1, published_at=NOW,
                satisfied=None, warning=None, stopped_ok=False))


def _args(argv: list[str]):
    return build_parser().parse_args(argv)


def test_find_accepts_reserved_as_a_status(capsys: pytest.CaptureFixture[str]):
    parser = build_parser()
    action = next(a for a in parser._subparsers._group_actions[0].choices["find"]
                  ._actions if a.dest == "status")
    assert "reserved" in action.choices, "agora find --status 要能選 reserved"


def test_find_reports_the_reservation_deadline(capsys: pytest.CaptureFixture[str]):
    reader = FakeReader(FakeSessionRow("reserved", "2026-10-04T09:00:00.000Z"))
    assert cmd_find(_args(["find", "--status", "reserved"]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert reader.last_query.status == "reserved"
    assert payload["hits"][0]["status"] == "reserved"
    assert payload["hits"][0]["reserved_until"] == "2026-10-04T09:00:00.000Z"


def test_find_without_status_filter_does_not_invent_a_deadline(
        capsys: pytest.CaptureFixture[str]):
    reader = FakeReader(FakeSessionRow("running", None))
    assert cmd_find(_args(["find"]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hits"][0]["status"] == "running"
    assert payload["hits"][0]["reserved_until"] is None


def test_show_marks_a_reservation_within_its_deadline(
        capsys: pytest.CaptureFixture[str]):
    reader = FakeReader(FakeSessionRow("reserved", "2026-10-04T09:00:00.000Z"))
    assert cmd_show(_args(["show", SID]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reservation"] == {
        "reserved_until": "2026-10-04T09:00:00.000Z", "expired": False}


def test_show_marks_an_expired_reservation(capsys: pytest.CaptureFixture[str]):
    reader = FakeReader(FakeSessionRow("reserved", "2026-09-28T09:00:00.000Z"),
                        now="2026-10-01T00:00:00.000Z")
    assert cmd_show(_args(["show", SID]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reservation"]["expired"] is True
    assert payload["reservation"]["reserved_until"] == "2026-09-28T09:00:00.000Z"


def test_show_has_no_reservation_block_for_a_working_session(
        capsys: pytest.CaptureFixture[str]):
    reader = FakeReader(FakeSessionRow("running", None))
    assert cmd_show(_args(["show", SID]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reservation"] is None


def test_show_does_not_guess_when_the_deadline_is_malformed(
        capsys: pytest.CaptureFixture[str]):
    """期限格式壞掉就不猜（`expired: null`），不要默默說它還沒過期。"""
    reader = FakeReader(FakeSessionRow("reserved", "不是時間"))
    assert cmd_show(_args(["show", SID]), reader) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reservation"]["expired"] is None

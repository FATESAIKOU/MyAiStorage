"""`agora` CLI 與 `agora-opencode` 轉接器的單元測試（impl2 I1）。

對應 `docs/design/agora-session-operations.md` 與 ADR 0010：

- 起點解析（`handoff:<id>` / `<session>[@<訊息>]`）；
- 起點包內容（原始紀錄**原封不動**、來源 id、任務、上限）；
- 認領流程：**被拒就不產出**；
- n→1 的排序（最長的放最前面）與**長度上限明確拒絕**；
- 轉接器的重編 id、n→1 串接、以及「匯入前不碰 opencode」。

規則：只用自編測試資料與 FakeDrive；不碰真 Drive／真 opencode／真 pin repo。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from aistorage.agora_cli.checkout import (
    CheckoutDeps,
    CheckoutError,
    ClaimRejected,
    checkout,
)
from aistorage.agora_cli.package import (
    ContextLimitExceeded,
    ContextPackageError,
    order_segments,
    read_package,
    write_package,
)
from aistorage.agora_cli.startpoint import (
    StartPointError,
    last_completed,
    parse_startpoint,
    resolve_startpoint,
)
from aistorage.clock import FixedClock

# 轉接器的函式在測試裡直接用（它刻意不碰 opencode，所以可以單元測）
from aistorage.adapters.opencode import chain_parents, reidentify, truncate_to

T0 = "2026-09-28T08:00:00.000Z"
T1 = "2026-09-28T09:00:00.000Z"
PROFILE = "mac-opencode"
S1 = "opencode:ses_aaa"
S2 = "opencode:ses_bbb"


# ---------------------------------------------------------------------------
# 測試用的 opencode 匯出檔（形狀真實，內容是自編的）
# ---------------------------------------------------------------------------


def _export(native: str, texts: Sequence[str]) -> dict:
    """造一份 opencode export JSON（`{info, messages}`）。

    形狀照 `tests/unit/data/converters/opencode/basic.json`（spike 與
    `converters/opencode.py` 都吃這個形狀），內容自編。
    """
    messages: list[dict] = []
    for i, text in enumerate(texts):
        role = "user" if i % 2 == 0 else "assistant"
        time = {"created": 1790420000000 + i * 1000}
        if role == "assistant":
            time["completed"] = 1790420000000 + i * 1000 + 500
        messages.append({
            "info": {
                "id": f"msg_{native}_{i}",
                "sessionID": native,
                "role": role,
                "time": time,
            },
            "parts": [
                {
                    "id": f"prt_{native}_{i}",
                    "sessionID": native,
                    "messageID": f"msg_{native}_{i}",
                    "type": "text",
                    "text": text,
                }
            ],
        })
    return {
        "info": {
            "id": native,
            "title": f"{native} 的標題",
            "time": {"created": 1790420000000, "updated": 1790420001000},
        },
        "messages": messages,
    }


def _raw(native: str, texts: Sequence[str]) -> bytes:
    return json.dumps(_export(native, texts), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _message_ids_of(raw: bytes) -> list[str]:
    """匯出檔裡的訊息 id（照原順序）。閱讀版的 id 必須與原始紀錄一致。"""
    return [str(m["info"]["id"]) for m in json.loads(raw)["messages"]]


# ---------------------------------------------------------------------------
# 假的讀取介面
# ---------------------------------------------------------------------------


class FakeReader:
    """只支援 checkout 用到的那幾個方法；內容都是自編的。"""

    def __init__(self, sessions: dict[str, dict], handoffs: dict[str, dict] | None = None
                 ) -> None:
        #: session_id -> {"raw": bytes, "texts": [...]}
        self.sessions = sessions
        self.handoffs = handoffs or {}
        self.raw_calls: list[tuple[str, str]] = []

    def get_session(self, session_id: str, *, max_lag: Any = None) -> Any:
        entry = self.sessions.get(session_id)
        if entry is None:
            raise KeyError(session_id)
        return SimpleNamespace(value=SimpleNamespace(
            session=SimpleNamespace(
                session_id=session_id,
                raw_sha256=_sha(entry["raw"]),
                snapshot_at=entry.get("snapshot_at", T0),
                title=entry.get("title", "t"),
            )))

    def get_reading(self, session_id: str, *, snapshot_sha256: str | None = None,
                    max_lag: Any = None) -> Any:
        entry = self.sessions[session_id]
        payload = {
            "format": "aistorage.reading/v1",
            "session_id": session_id,
            "source": session_id.split(":", 1)[0],
            "title": entry.get("title", "t"),
            "parent_id": None,
            "snapshot_sha256": _sha(entry["raw"]),
            "in_progress": False,
            "messages": [
                {
                    "message_id": mid,
                    "index": i,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "created_at": T0,
                    "completed": True,
                    "reverted": False,
                    "parts": [{"type": "text", "text": t}],
                }
                for i, (mid, t) in enumerate(
                    zip(_message_ids_of(entry["raw"]), entry["texts"]))
            ],
        }
        return SimpleNamespace(value=payload)

    def get_raw(self, session_id: str, snapshot_sha256: str) -> Any:
        self.raw_calls.append((session_id, snapshot_sha256))
        entry = self.sessions.get(session_id)
        if entry is None or _sha(entry["raw"]) != snapshot_sha256.lower():
            raise KeyError(f"{session_id}@{snapshot_sha256[:12]}")
        return SimpleNamespace(value=SimpleNamespace(
            data=entry["raw"], size=len(entry["raw"]), snapshot_at=T0))

    def get_continuation(self, handoff_id: str) -> Any:
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(handoff_id)
        return SimpleNamespace(value=SimpleNamespace(
            handoff=SimpleNamespace(
                handoff_id=handoff_id,
                target_session_id=handoff["target_session_id"],
                snapshot_sha256=handoff["snapshot_sha256"],
                message_id=handoff["message_id"],
                body_json=json.dumps({"content": handoff.get("content", "")},
                                     ensure_ascii=False),
            ),
            messages=(), sibling_links=()))


def _make_signer(profile: str = PROFILE):
    from aistorage.identity import generate_keypair
    from aistorage.syncer.core import Signer

    priv, pub = generate_keypair()
    key_id = f"{profile}-{hashlib.sha256(pub).hexdigest()[:8].lower()}"
    return Signer(profile, key_id, priv)


class FakeCommit:
    """假的「同步並提交」：記錄放上去的項目，結果由測試決定。"""

    def __init__(self, *, rejected: Sequence[tuple[str, str]] = (),
                 timed_out: bool = False) -> None:
        self.rejected = tuple(rejected)
        self.timed_out = timed_out
        self.seeds: list[Any] = []
        self.claims: list[Any] = []

    def __call__(self, seed: Any, claims: list[Any], *, timeout: Any) -> Any:
        self.seeds.append(seed)
        self.claims.extend(claims)
        return SimpleNamespace(
            rejected=self.rejected, timed_out=self.timed_out,
            summary=lambda: "0／0 有結果")


def _deps(reader: FakeReader, commit: Any = None, *, profile: str = PROFILE
          ) -> CheckoutDeps:
    return CheckoutDeps(
        reader=reader, clock=FixedClock(T1), signer=_make_signer(profile),
        inbox_folder_id="inbox-test", drive=None,
        commit_claim=commit if commit is not None else FakeCommit(),
    )


# ---------------------------------------------------------------------------
# 1. 起點解析
# ---------------------------------------------------------------------------


def test_handoff_startpoint_is_parsed_as_a_handoff():
    """`handoff:<ULID>` → 交接單起點（目標 Session 要查讀取介面才知道）。"""
    sp = parse_startpoint("handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert sp.is_handoff
    assert sp.handoff_id == "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert sp.message_id is None


def test_session_startpoint_accepts_with_and_without_source_prefix():
    """`<session>[@<訊息>]`：前綴可省略（補預設來源），`@` 後是接續點。"""
    plain = parse_startpoint("ses_abc")
    assert plain.session_id == "opencode:ses_abc" and plain.message_id is None
    at = parse_startpoint("ses_abc@msg_7")
    assert at.message_id == "msg_7" and at.session_id == "opencode:ses_abc"
    full = parse_startpoint("claude-code:abc123@msg_7", source="opencode")
    assert full.source == "claude-code" and full.session_id == "claude-code:abc123"


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "handoff:not-a-ulid",
    "ses_abc@",              # 有 @ 卻沒給訊息
    "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV@msg_1",  # 交接單不能帶 @
    "claim:01ARZ3NDEKTSV4RRFFQ69G5FAV",            # 保留字不是來源應用
    "有空白 的id",
])
def test_bad_startpoints_are_rejected_with_a_reason(bad: str):
    """看不懂就明確拒絕，不要猜。"""
    with pytest.raises(StartPointError):
        parse_startpoint(bad)


def test_startpoint_resolves_to_last_completed_message_when_no_at():
    """不指定位置 → 最新已提交的那一則（語意與寫交接單的那一邊一致）。"""
    raw = _raw("ses_aaa", ["一", "二", "三"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二", "三"]}})
    resolved = resolve_startpoint(reader, parse_startpoint(S1))
    assert resolved.message_id == "msg_ses_aaa_2"
    assert resolved.snapshot_sha256 == _sha(raw)
    assert resolved.source == "opencode"


def test_startpoint_with_at_rejects_a_message_outside_the_pinned_snapshot():
    """接續點必須在被釘住的那一份快照裡；查不到就明確拒絕。"""
    reader = FakeReader({S1: {"raw": _raw("ses_aaa", ["一"]), "texts": ["一"]}})
    with pytest.raises(StartPointError) as excinfo:
        resolve_startpoint(reader, parse_startpoint(f"{S1}@msg_nope"))
    assert "msg_nope" in str(excinfo.value)


def test_handoff_startpoint_takes_position_from_the_handoff_only():
    """交接單起點：位置由交接單決定，呼叫端不能改。"""
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader(
        {S1: {"raw": raw, "texts": ["一", "二"]}},
        {"handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV": {
            "target_session_id": S1,
            "snapshot_sha256": _sha(raw),
            "message_id": "msg_ses_aaa_1",
            "content": "接手後續調查",
        }},
    )
    resolved = resolve_startpoint(
        reader, parse_startpoint("handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    assert resolved.session_id == S1
    assert resolved.snapshot_sha256 == _sha(raw)
    assert resolved.message_id == "msg_ses_aaa_1"
    assert resolved.handoff_id == "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert resolved.task == "接手後續調查"


def test_last_completed_skips_unfinished_and_reverted_messages():
    """「最後一則**已完成**的訊息」＝不算未完成、不算已撤銷。"""
    reading = {"messages": [
        {"message_id": "m1", "index": 0, "completed": True, "reverted": False},
        {"message_id": "m2", "index": 1, "completed": False, "reverted": False},
        {"message_id": "m3", "index": 2, "completed": True, "reverted": True},
    ]}
    assert last_completed(reading) == "m1"


# ---------------------------------------------------------------------------
# 2. 起點包內容
# ---------------------------------------------------------------------------


def test_checkout_writes_the_raw_log_untouched(tmp_path: Path):
    """原始紀錄**原封不動**放進起點包：位元組相同、雜湊等於快照雜湊。"""
    raw = _raw("ses_aaa", ["一", "二", "三"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二", "三"]}})
    out = tmp_path / "pkg"

    pkg = checkout(reader, _deps(reader), [S1], out, task="接著做")

    data, raws = read_package(out)
    assert raws[0] == raw, "原始紀錄必須一個位元組都不動"
    assert data["format"] == "aistorage.contextpackage/v1"
    assert data["task"] == "接著做"
    assert data["created_by"] == f"profile:{PROFILE}"
    segment = data["segments"][0]
    assert segment["source_session_id"] == S1
    assert segment["snapshot_sha256"] == _sha(raw)
    assert segment["raw_sha256"] == _sha(raw)
    assert segment["message_id"] == "msg_ses_aaa_2"
    assert segment["message_count"] == 3
    assert data["new_session"]["session_id"] == pkg.new_session_id
    assert data["new_session"]["claimed_handoffs"] == []
    assert data["context_limit"]["within_limit"] is True
    # 原始紀錄真的在 raw/ 底下
    assert (out / segment["raw_file"]).read_bytes() == raw


def test_checkout_does_not_import_any_opencode_specific_module(tmp_path: Path):
    """Agora 不 import 任何 coding agent 專屬的東西（設計文件的硬性要求）。"""
    import subprocess
    import sys

    code = (
        "import sys, aistorage.agora_cli, aistorage.agora_cli.checkout;"
        "leaked = [m for m in sys.modules if m.startswith('aistorage.adapters')];"
        "print(leaked)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, check=True)
    assert proc.stdout.strip() == "[]", (
        f"agora_cli 不該 import 轉接器：{proc.stdout.strip()}")


def test_package_keeps_the_raw_even_though_the_startpoint_is_in_the_middle(tmp_path: Path):
    """起點在中間時，起點包放的是**整份**原始紀錄（截斷是轉接器的事）。"""
    raw = _raw("ses_aaa", ["一", "二", "三", "四"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二", "三", "四"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [f"{S1}@msg_ses_aaa_1"], out)
    data, raws = read_package(out)
    assert raws[0] == raw
    assert data["segments"][0]["message_id"] == "msg_ses_aaa_1"
    assert data["segments"][0]["message_count"] == 2


def test_read_package_rejects_a_tampered_raw(tmp_path: Path):
    """起點包被搬來搬去，所以載入端要自己驗；對不上就明確拒絕。"""
    raw = _raw("ses_aaa", ["一"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out)
    target = out / json.loads((out / "package.json").read_text())["segments"][0]["raw_file"]
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ContextPackageError) as excinfo:
        read_package(out)
    assert "已被動過" in str(excinfo.value)


def test_write_package_refuses_to_overwrite_a_non_empty_directory(tmp_path: Path):
    out = tmp_path / "pkg"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(ContextPackageError) as excinfo:
        write_package(_empty_package(), out)
    assert "不會覆蓋" in str(excinfo.value)


def _empty_package():
    from aistorage.agora_cli.package import ContextPackage, PackageSegment
    from aistorage.agora_cli.startpoint import ResolvedStartPoint, StartPoint

    sp = parse_startpoint(S1)
    resolved = ResolvedStartPoint(
        startpoint=sp, session_id=S1, source="opencode", snapshot_sha256="a" * 64,
        snapshot_at=T0, message_id="m1")
    return ContextPackage(
        segments=(PackageSegment(resolved=resolved, raw=b"{}", order=0),),
        task=None, new_session_id="opencode:ses_new", created_at=T0,
        created_by="profile:x", max_context_chars=1000,
    )


def test_package_validates_against_the_published_schema(tmp_path: Path):
    """起點包必須符合 `schemas/context-package.schema.json`（規格的一部分）。"""
    from jsonschema import Draft202012Validator

    from aistorage.schema import _locate_schema_file

    schema = json.loads(
        Path(_locate_schema_file("context-package.schema.json")).read_text("utf-8"))
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out, task="做後端")
    data = json.loads((out / "package.json").read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(data),
                    key=lambda e: list(e.path))
    assert errors == [], f"起點包不符合 schema: {errors[:3]}"


# ---------------------------------------------------------------------------
# 3. 認領流程
# ---------------------------------------------------------------------------


HANDOFF = "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"


def _reader_with_handoff() -> FakeReader:
    raw = _raw("ses_aaa", ["一", "二"])
    return FakeReader(
        {S1: {"raw": raw, "texts": ["一", "二"]}},
        {HANDOFF: {
            "target_session_id": S1, "snapshot_sha256": _sha(raw),
            "message_id": "msg_ses_aaa_1", "content": "接手後續調查",
        }},
    )


def test_handoff_startpoint_registers_a_claim_before_producing_the_package(tmp_path: Path):
    """交接單起點：放一筆認領進收件匣，等確認後才產出起點包。"""
    reader = _reader_with_handoff()
    commit = FakeCommit()
    out = tmp_path / "pkg"

    pkg = checkout(reader, _deps(reader, commit), [HANDOFF], out)

    assert len(commit.claims) == 1
    claim_sidecar = commit.claims[0].sidecar
    assert claim_sidecar["body"]["handoff_id"] == HANDOFF
    # 認領者就是這個起點包預留的新 session（提交流程要求它先在 Agora 裡）
    assert claim_sidecar["body"]["claimer_session_id"] == pkg.new_session_id
    # 新 session 的第一份快照與認領同一批（apply 的順序 session 在前）
    assert len(commit.seeds) == 1
    assert commit.seeds[0].sidecar["metadata"]["id"] == pkg.new_session_id
    data, _raws = read_package(out)
    assert data["new_session"]["claimed_handoffs"] == [HANDOFF]
    assert data["segments"][0]["handoff_id"] == HANDOFF
    assert data["segments"][0]["claim_id"] == commit.claims[0].item_id


def test_rejected_claim_produces_no_package(tmp_path: Path):
    """**被拒就不產出**：目錄不該被建立起來。"""
    reader = _reader_with_handoff()
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim",
                                                    target="claim:01X"), "already_claimed")])
    out = tmp_path / "pkg"

    with pytest.raises(ClaimRejected) as excinfo:
        checkout(reader, _deps(reader, commit), [HANDOFF], out)

    assert "already_claimed" in str(excinfo.value)
    assert "沒有產出起點包" in str(excinfo.value)
    assert not out.exists(), "被拒時目錄不該被建立"


def test_claim_timeout_produces_no_package(tmp_path: Path):
    """等不到確認也不產出（寧可不要開工，也不要沒有 Link 的孤兒 session）。"""
    reader = _reader_with_handoff()
    commit = FakeCommit(timed_out=True)
    out = tmp_path / "pkg"
    with pytest.raises(ClaimRejected):
        checkout(reader, _deps(reader, commit), [HANDOFF], out)
    assert not out.exists()


def test_handoff_startpoint_without_writer_identity_is_refused(tmp_path: Path):
    """沒有寫入身分就明確拒絕（不能假裝認領了）。"""
    reader = _reader_with_handoff()
    deps = CheckoutDeps(reader=reader, clock=FixedClock(T1), signer=None,
                        inbox_folder_id="", commit_claim=None)
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, deps, [HANDOFF], tmp_path / "pkg")
    assert "沒有可用的寫入身分" in str(excinfo.value)
    assert not (tmp_path / "pkg").exists()


def test_plain_session_startpoint_needs_no_claim(tmp_path: Path):
    """直接起點（`session[@訊息]`）本來就沒有交接單，也不需要認領。"""
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    commit = FakeCommit()
    checkout(reader, _deps(reader, commit), [S1], tmp_path / "pkg")
    assert commit.claims == [] and commit.seeds == []


# ---------------------------------------------------------------------------
# 4. n→1：排序與長度上限
# ---------------------------------------------------------------------------


def _pkg_segment(reader_raw: bytes, texts: Sequence[str], session_id: str):
    from aistorage.agora_cli.package import PackageSegment
    from aistorage.agora_cli.startpoint import ResolvedStartPoint

    resolved = ResolvedStartPoint(
        startpoint=parse_startpoint(session_id), session_id=session_id,
        source="opencode", snapshot_sha256=_sha(reader_raw), snapshot_at=T0,
        message_id=f"msg_{session_id.split(':', 1)[1]}_{len(texts) - 1}")
    return PackageSegment(resolved=resolved, raw=reader_raw,
                          message_count=len(texts),
                          text_chars=sum(len(t) for t in texts))


def test_n_to_one_puts_the_longest_segment_first(tmp_path: Path):
    """n→1：最長的一段放最前面（ADR 0010 的 prompt cache）。"""
    short = _pkg_segment(_raw("ses_aaa", ["短"]), ["短"], S1)
    long_ = _pkg_segment(_raw("ses_bbb", ["中" * 50, "中" * 50]),
                         ["中" * 50, "中" * 50], S2)
    middle = _pkg_segment(_raw("ses_ccc", ["中" * 10]), ["中" * 10],
                           "opencode:ses_ccc")

    ordered = order_segments([short, long_, middle])
    assert [s.session_id for s in ordered] == [S2, "opencode:ses_ccc", S1]
    assert [s.order for s in ordered] == [0, 1, 2]


def test_n_to_one_keeps_caller_order_on_a_tie(tmp_path: Path):
    """長度相同時維持呼叫端給的次序（呼叫端已經排好的不要打亂）。"""
    a = _pkg_segment(_raw("ses_aaa", ["一樣長"]), ["一樣長"], S1)
    b = _pkg_segment(_raw("ses_bbb", ["一樣長"]), ["一樣長"], S2)
    assert [s.session_id for s in order_segments([a, b])] == [S1, S2]
    assert [s.session_id for s in order_segments([b, a])] == [S2, S1]


def test_n_to_one_writes_the_longest_segment_first_into_the_package(tmp_path: Path):
    """寫進起點包之後的順序也是「最長的先」。"""
    raw_short = _raw("ses_aaa", ["短"])
    raw_long = _raw("ses_bbb", ["長" * 80, "長" * 80])
    reader = FakeReader({
        S1: {"raw": raw_short, "texts": ["短"]},
        S2: {"raw": raw_long, "texts": ["長" * 80, "長" * 80]},
    })
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1, S2], out, task="整合兩邊的成果")
    data, _raws = read_package(out)
    assert [s["source_session_id"] for s in data["segments"]] == [S2, S1]
    assert [s["order"] for s in data["segments"]] == [0, 1]


def test_over_the_length_limit_is_refused_explicitly_and_writes_nothing(tmp_path: Path):
    """超過上限就**明確拒絕**，不默默截斷、也不產出半套（ADR 0010）。"""
    raw_long = _raw("ses_aaa", ["長" * 500, "長" * 500])
    reader = FakeReader({S1: {"raw": raw_long, "texts": ["長" * 500, "長" * 500]}})
    out = tmp_path / "pkg"

    with pytest.raises(ContextLimitExceeded) as excinfo:
        checkout(reader, _deps(reader), [S1], out, max_context_chars=100)

    message = str(excinfo.value)
    assert "100" in message and "不會默默截斷" in message
    assert not out.exists(), "被拒時目錄不該被建立"


def test_length_limit_is_recorded_in_the_package(tmp_path: Path):
    """沒超過時，上限與計量單位寫在檔案裡（載入端不必再猜）。"""
    raw = _raw("ses_aaa", ["短"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["短"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out, max_context_chars=12345)
    data, _raws = read_package(out)
    assert data["context_limit"] == {
        "limit": 12345, "measured": "text_chars", "within_limit": True}


# ---------------------------------------------------------------------------
# 5. 轉接器：重編 id、n→1 串接
# ---------------------------------------------------------------------------


def _adapter_export(session_id: str, texts: Sequence[str]) -> dict:
    return _export(session_id, texts)


def test_truncate_includes_the_startpoint_message():
    """截斷**含**接續點那一則（模型才看得到交接的那一段）。"""
    payload = _adapter_export("ses_aaa", ["一", "二", "三", "四"])
    kept = truncate_to(payload, "msg_ses_aaa_1")
    assert [m["info"]["id"] for m in kept] == ["msg_ses_aaa_0", "msg_ses_aaa_1"]
    assert truncate_to(payload, None) == payload["messages"]
    with pytest.raises(Exception) as excinfo:
        truncate_to(payload, "msg_nope")
    assert "msg_nope" in str(excinfo.value)


def test_every_id_is_reidentified_and_the_rest_is_untouched():
    """session／message／part id 全換新，**其餘欄位一個位元組都不動**。

    動到別的欄位，開頭就不會與原 session 位元組相同，KV cache 全部落空
    （docs/spike/session-import.md Q2）。
    """
    from aistorage.adapters.opencode import reidentify

    payload = _adapter_export("ses_aaa", ["一", "二"])
    original = json.loads(json.dumps(payload))
    kept = truncate_to(payload, "msg_ses_aaa_1")
    out = reidentify(payload, kept, session_id="ses_NEW01", tag="NEW01")

    assert out["info"]["id"] == "ses_NEW01"
    ids = [m["info"]["id"] for m in out["messages"]]
    assert all(i.startswith("msg_NEW01") for i in ids)
    assert ids != ["msg_ses_aaa_0", "msg_ses_aaa_1"]
    for original_m, m in zip(original["messages"], out["messages"]):
        # 除了 id 與 sessionID 之外，每個欄位都必須一模一樣
        assert m["info"]["role"] == original_m["info"]["role"]
        assert m["info"]["time"] == original_m["info"]["time"]
        assert m["info"]["sessionID"] == "ses_NEW01"
        for p, original_p in zip(m["parts"], original_m["parts"]):
            assert p["sessionID"] == "ses_NEW01"
            assert p["messageID"] == m["info"]["id"]
            assert p["id"].startswith("prt_NEW01")
            assert p["type"] == original_p["type"]
            assert p["text"] == original_p["text"]


def test_parent_pointing_outside_the_truncation_is_cleared():
    """parent 指向被截掉的那一則時要清掉，不要留著指向不存在的訊息。"""
    from aistorage.adapters.opencode import reidentify

    payload = _adapter_export("ses_aaa", ["一", "二", "三"])
    kept = truncate_to(payload, "msg_ses_aaa_1")
    out = reidentify(payload, kept, session_id="ses_NEW01", tag="T")
    for m in out["messages"]:
        parent = m["info"].get("parentID")
        assert parent is None or parent in {x["info"]["id"] for x in out["messages"]}


def test_n_to_one_chains_parents_by_hand():
    """n→1：第二段首則的 parent 手工鏈到第一段末則（spike Q4）。"""
    from aistorage.adapters.opencode import chain_parents, reidentify

    seg1 = reidentify(_adapter_export("ses_aaa", ["一", "二"]),
                      truncate_to(_adapter_export("ses_aaa", ["一", "二"]), None),
                      session_id="ses_NEW01", tag="S1")["messages"]
    seg2 = reidentify(_adapter_export("ses_bbb", ["三", "四"]),
                      truncate_to(_adapter_export("ses_bbb", ["三", "四"]), None),
                      session_id="ses_NEW01", tag="S2")["messages"]
    merged = chain_parents([seg1, seg2])
    assert [m["info"]["id"] for m in merged] == (
        [m["info"]["id"] for m in seg1] + [m["info"]["id"] for m in seg2])
    assert merged[2]["info"]["parentID"] == merged[1]["info"]["id"]


def test_build_export_uses_the_reserved_session_id_and_verifies_the_raw(tmp_path: Path):
    """轉接器沿用起點包預留的 session id，並且自己驗過原始紀錄。"""
    from aistorage.adapters.opencode import build_export

    raw = _raw("ses_aaa", ["一", "二", "三"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二", "三"]}})
    out = tmp_path / "pkg"
    pkg = checkout(reader, _deps(reader), [f"{S1}@msg_ses_aaa_1"], out)

    payload, session_id, segments, source = build_export(out)
    # 匯出檔用來源端自己的 id（opencode 的 info.id 不帶 <source>: 前綴）
    assert session_id == pkg.new_session_id.split(":", 1)[1]
    assert source == "opencode" and segments == 1
    assert payload["info"]["id"] == session_id
    assert len(payload["messages"]) == 2
    # 內容來自原始紀錄，沒被改寫
    assert [p["parts"][0]["text"] for p in payload["messages"]] == ["一", "二"]


def test_build_export_refuses_to_mix_sources(tmp_path: Path):
    """跨來源應用開頭一定會被改寫（KV cache 會斷）——期 1 明確拒絕。"""
    from aistorage.adapters.opencode import AdapterError, build_export

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out)
    data = json.loads((out / "package.json").read_text(encoding="utf-8"))
    data["new_session"]["source"] = "claude-code"
    (out / "package.json").write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    with pytest.raises(AdapterError) as excinfo:
        build_export(out)
    assert "跨來源應用" in str(excinfo.value)


def test_load_passes_the_export_to_opencode_in_the_target_directory(tmp_path: Path):
    """`load` 在**目標專案目錄**呼叫 `opencode import`（spike Q1-2）。"""
    from aistorage.adapters.opencode import load

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    workdir = tmp_path / "project"
    workdir.mkdir()
    pkg = checkout(reader, _deps(reader), [S1], out)

    seen: dict[str, Any] = {}

    def runner(argv: Sequence[str], cwd: Path) -> str:
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        seen["payload"] = json.loads(
            Path(argv[2]).read_text(encoding="utf-8"))
        return "Imported session: ses_x"

    result = load(out, workdir=workdir, runner=runner)
    assert seen["argv"][:2] == ["opencode", "import"]
    assert seen["cwd"] == workdir
    assert seen["payload"]["info"]["id"] == result.session_id
    assert result.session_id == pkg.new_session_id.split(":", 1)[1]
    assert result.messages == 2 and result.segments == 1


def test_load_reports_a_failed_import_without_leaking_content(tmp_path: Path):
    """匯入失敗只回報代碼與 stderr 尾端，不帶 Session 內文。"""
    from aistorage.adapters.opencode import AdapterError, load

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out)

    def runner(argv: Sequence[str], cwd: Path) -> str:
        raise AdapterError("opencode import 失敗 (rc=1)")

    with pytest.raises(AdapterError):
        load(out, workdir=tmp_path, runner=runner)


# ---------------------------------------------------------------------------
# 6. 位元組相同：checkout + load 的開頭與原 session 相同
# ---------------------------------------------------------------------------


GOLDEN = Path(__file__).parent / "data" / "converters" / "opencode" / "basic.json"


def _golden_bytes() -> bytes:
    return GOLDEN.read_bytes()


def _realistic_export(native: str) -> dict:
    """一份**形狀真實**的 opencode 匯出檔：含工具呼叫。

    與 `basic.json` 的差別是工具 part 帶 `callID`（真實的 opencode 匯出檔有這個
    欄位，值就是 spike Q2 看到的 `call_function_…`）。有它才談得上
    「重播時 tool_call id 保留原值」，那才是開頭位元組相同的原因。
    """
    data = _export(native, ["幫我看看 config", "讀取完成", "再跑一次 echo"])
    data["messages"][1]["parts"].insert(0, {
        "id": f"prt_{native}_1_tool",
        "sessionID": native,
        "messageID": f"msg_{native}_1",
        "type": "tool",
        "tool": "read",
        "callID": "call_function_aaa",
        "state": {"status": "completed", "input": {"path": "config.json"},
                  "output": "{\"ok\": true}"},
    })
    data["messages"][2]["parts"].insert(0, {
        "id": f"prt_{native}_2_tool",
        "sessionID": native,
        "messageID": f"msg_{native}_2",
        "type": "tool",
        "tool": "bash",
        "callID": "call_function_bbb",
        "state": {"status": "completed", "input": {"command": "echo hi"},
                  "output": "hi"},
    })
    return data


def test_checkout_and_load_replay_a_byte_identical_prefix(tmp_path: Path):
    """**新 session 送給模型的開頭與原 session 位元組相同**（ADR 0010）。

    用 spike 的比對工具（`scripts/spike/session_import_compare.py`）把匯出檔
    重播成送給模型的 messages 陣列。spike Q2／Q3 實測只要這個位元組相同，
    opencode import 出去之後開頭就位元組相同（歷史重播保留原 `tool_call id`
    與工具結果）。所以這裡驗的是同一個量，只是不需要真的跑 opencode。
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "spike"))
    from session_import_compare import export_prefix_bytes

    native = "ses_real"
    data = _realistic_export(native)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    session_id = f"opencode:{native}"
    texts = ["幫我看看 config", "讀取完成", "再跑一次 echo"]
    reader = FakeReader({session_id: {"raw": raw, "texts": texts}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [session_id], out)

    from aistorage.adapters.opencode import build_export

    export, new_id, _segments, _source = build_export(out)
    original = json.loads(raw)
    # 對照組截到接續點為止（不含接續點那之後的訊息）
    original["messages"] = original["messages"][:len(export["messages"])]
    assert export_prefix_bytes(export) == export_prefix_bytes(original)
    # 雜湊鏈沒有斷：起點包的 raw_sha256 == 快照雜湊 == 原始位元組雜湊
    data_out, raws = read_package(out)
    assert data_out["segments"][0]["raw_sha256"] == hashlib.sha256(raw).hexdigest().lower()
    assert raws[0] == raw
    # id 全換了（新 session 的 session id 來自起點包，轉接器不自己編）
    assert new_id == data_out["new_session"]["session_id"].split(":", 1)[1]
    assert all(m["info"]["id"] != o["info"]["id"]
               for m, o in zip(export["messages"], original["messages"]))
    # 工具呼叫的 callID 保留原值（重播時位元組相同的原因）
    assert [p.get("callID") for m in export["messages"] for p in m["parts"]
            if p.get("type") == "tool"] == ["call_function_aaa", "call_function_bbb"]
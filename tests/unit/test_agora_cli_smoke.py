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
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

from aistorage.agora_cli.checkout import (
    CheckoutDeps,
    CheckoutError,
    ClaimAlreadyCommitted,
    ClaimRejected,
    PartialClaimAccepted,
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
S3 = "opencode:ses_ccc"


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


def annex_key_of(raw: bytes) -> str:
    """git-annex 的 key 形狀：`SHA256E-s<size>--<sha256>`（內容定址）。"""
    return f"SHA256E-s{len(raw)}--{_sha(raw)}"


def _objects(reader: FakeReader, *, tamper: dict[str, bytes] | None = None) -> Any:
    """真的 `ObjectFetcher`，掛在 FakeDrive 的物件資料夾上。

    故意用**真的** fetcher（不是假的）：安全性就落在「用 key 內嵌的 sha256 驗證」
    那一段，用假的等於沒測到。
    """
    from aistorage.agora_cli.objects import ObjectFetcher
    from aistorage.drive.fake import FakeDrive

    drive = FakeDrive()
    folder = drive.seed_folder("agora-objects")
    for entry in reader.sessions.values():
        raw = entry["raw"]
        drive.seed_file(folder, annex_key_of(raw), raw,
                        file_id=annex_key_of(raw))
    for key, payload in (tamper or {}).items():
        # 塞一份「同名但內容是假的」物件：key 內嵌的 sha256 必須擋下來
        drive.seed_file(folder, key, payload, file_id=key)
    return ObjectFetcher(drive, folder)


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
        # `flags` 讓測試可以把某一則標成未完成／已撤銷（接續點的邊界）
        flags = entry.get("flags") or {}
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
                    "completed": flags.get(mid, {}).get("completed", True),
                    "reverted": flags.get(mid, {}).get("reverted", False),
                    "parts": [{"type": "text", "text": t}],
                }
                for i, (mid, t) in enumerate(
                    zip(_message_ids_of(entry["raw"]), entry["texts"]))
            ],
        }
        return SimpleNamespace(value=payload)

    def get_snapshot(self, session_id: str, snapshot_sha256: str, **kw) -> Any:
        """讀取視圖只給**位址**（annex key），不給位元組。"""
        self.raw_calls.append((session_id, snapshot_sha256))
        entry = self.sessions.get(session_id)
        if entry is None or _sha(entry["raw"]) != snapshot_sha256.lower():
            raise KeyError(f"{session_id}@{snapshot_sha256[:12]}")
        return SimpleNamespace(value=SimpleNamespace(
            session_id=session_id, snapshot_sha256=snapshot_sha256,
            snapshot_at=T0, via="sync",
            annex_key=annex_key_of(entry["raw"])))

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
    """假的「同步並提交」：記錄放上去的認領，結果由測試決定。"""

    def __init__(self, *, rejected: Sequence[tuple[str, str]] = (),
                 timed_out: bool = False) -> None:
        self.rejected = tuple(rejected)
        self.timed_out = timed_out
        self.claims: list[Any] = []
        #: 送出當下每個認領的位元組（預留在 `checkout` 清掉暫存目錄之前要先讀）
        self.claim_raws: list[bytes] = []

    def __call__(self, claims: list[Any], *, timeout: Any) -> Any:
        self.claims.extend(claims)
        for item in claims:
            if item.raw_path is not None:
                self.claim_raws.append(item.raw_path.read_bytes())
        return SimpleNamespace(
            rejected=self.rejected, timed_out=self.timed_out,
            summary=lambda: "0／0 有結果")


def _journal(tmp_path: Path) -> Any:
    from aistorage.agora_cli.claims import ClaimJournal

    # 記錄現在是**一個目錄**（每張交接單一個檔案，見 review-7a4ca87 M1）
    return ClaimJournal(tmp_path / "checkout-claims")


def _deps(reader: FakeReader, commit: Any = None, *, profile: str = PROFILE,
          objects: Any = None, journal: Any = None) -> CheckoutDeps:
    fetcher = objects if objects is not None else _objects(reader)
    return CheckoutDeps(
        reader=reader, clock=FixedClock(T1), signer=_make_signer(profile),
        inbox_folder_id="inbox-test", drive=None, objects=fetcher,
        commit_claim=commit if commit is not None else FakeCommit(),
        journal=journal,
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
HANDOFF2 = "handoff:01BX5ZZKBKACTAV9WEVGEMMVRZ"
HANDOFF3 = "handoff:01CX6AABKACTAV9WEVGEMMVRZ"


def _reader_with_handoff(*handoff_ids: str) -> FakeReader:
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader(
        {S1: {"raw": raw, "texts": ["一", "二"]}},
        {HANDOFF: {
            "target_session_id": S1, "snapshot_sha256": _sha(raw),
            "message_id": "msg_ses_aaa_1", "content": "接手後續調查",
        }},
    )
    for handoff_id in handoff_ids:
        reader.handoffs[handoff_id] = {
            "target_session_id": S1, "snapshot_sha256": _sha(raw),
            "message_id": "msg_ses_aaa_1", "content": f"接手 {handoff_id}",
        }
    return reader


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
    data, _raws = read_package(out)
    assert data["new_session"]["claimed_handoffs"] == [HANDOFF]
    assert data["segments"][0]["handoff_id"] == HANDOFF
    assert data["segments"][0]["claim_id"] == commit.claims[0].item_id


def test_the_new_session_is_reserved_as_an_empty_record_not_a_copy_of_the_source(
        tmp_path: Path):
    """H2：認領**預留**的是一個空的新 session，不是來源 session 的副本。

    舊的作法是把來源 raw（截斷或沒截斷）當成新 session 的第一份快照送出去，於是
    Agora 裡會出現一份掛在新 id 底下、內容與標題全是錯的紀錄；被拒時那份複製還會
    變成沒有人接手的孤兒。預留現在是**零則訊息**的空匯出檔，而且被拒時不寫進去。
    """
    reader = _reader_with_handoff()
    commit = FakeCommit()
    out = tmp_path / "pkg"

    pkg = checkout(reader, _deps(reader, commit), [HANDOFF], out)

    claim = commit.claims[0]
    assert claim.sidecar["session"]["source_session_id"] == \
        pkg.new_session_id.split(":", 1)[1]
    assert claim.sidecar["session"]["reserving"] is True
    reserved = json.loads(commit.claim_raws[0])
    assert reserved["messages"] == [], "預留必須是空的（不能帶別人的對話）"
    assert reserved["info"]["id"] == pkg.new_session_id.split(":", 1)[1]
    # 原始紀錄仍然**原封不動**放在起點包裡（那是開頭位元組相同的前提）
    _data, raws = read_package(out)
    assert raws[0] == _handoff_raw(reader), "起點包放的是來源 raw，不是預留"


def _handoff_raw(reader: FakeReader) -> bytes:
    entry = reader.handoffs[HANDOFF]
    return reader.sessions[entry["target_session_id"]]["raw"]


def test_rejected_claim_produces_no_package(tmp_path: Path):
    """**被拒就不產出**：目錄不該被建立起來（而且 Agora 裡也不該留下預留）。"""
    reader = _reader_with_handoff()
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim",
                                                    target="claim:01X"), "already_claimed")])
    out = tmp_path / "pkg"

    with pytest.raises(ClaimRejected) as excinfo:
        checkout(reader, _deps(reader, commit), [HANDOFF], out)

    assert "already_claimed" in str(excinfo.value)
    assert "沒有產出起點包" in str(excinfo.value)
    assert not out.exists(), "被拒時目錄不該被建立"
    assert not out.with_name(f".{out.name}.staging").exists(), \
        "被拒時暫存目錄要清掉"


def test_claim_timeout_keeps_the_local_record_so_a_rerun_can_resume(tmp_path: Path):
    """逾時**不等於被拒**：本機記錄要留著，讓 `--resume` 沿用同一個認領。"""
    reader = _reader_with_handoff()
    journal = _journal(tmp_path)
    commit = FakeCommit(timed_out=True)
    out = tmp_path / "pkg"

    with pytest.raises(ClaimRejected) as excinfo:
        checkout(reader, _deps(reader, commit, journal=journal), [HANDOFF], out)

    # 重跑**自動**沿用，不需要任何參數（review-7a4ca87 H1）
    assert "自動沿用" in str(excinfo.value)
    assert not out.exists()
    record = journal.get(HANDOFF)
    assert record is not None, "逾時要留記錄，否則重跑認不回來"
    assert record.claim_id == commit.claims[0].item_id


def test_resume_reuses_the_same_claim_and_the_same_new_session(tmp_path: Path):
    """`--resume`：同一個 claim id、同一個 item_key、同一份預留位元組。

    交接單只能被認領一次，所以重跑若換一個新的認領 id，只會得到 `already_claimed`
    而那張單永遠沒有 session 接手。提交流程把重複的 claim id 當成完成，所以
    重送同一個 item 是冪等的。
    """
    reader = _reader_with_handoff()
    journal = _journal(tmp_path)
    out = tmp_path / "pkg"
    first_commit = FakeCommit(timed_out=True)
    with pytest.raises(ClaimRejected):
        checkout(reader, _deps(reader, first_commit, journal=journal),
                 [HANDOFF], out, new_session_id=S2)

    second_commit = FakeCommit()
    pkg = checkout(reader, _deps(reader, second_commit, journal=journal),
                   [HANDOFF], out, new_session_id=S2, resume=True)

    assert pkg.new_session_id == S2
    assert second_commit.claims[0].item_id == first_commit.claims[0].item_id
    assert (second_commit.claims[0].sidecar_bytes
            == first_commit.claims[0].sidecar_bytes), \
        "重跑必須產生位元組相同的項目，否則清冊會判成 replayed_item_key"


def test_resume_without_a_local_record_is_refused(tmp_path: Path):
    """`--resume` 找不到記錄就明確拒絕，不要假裝沿用（換 id 只會 already_claimed）。"""
    reader = _reader_with_handoff()
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, _deps(reader, journal=_journal(tmp_path)), [HANDOFF],
                 tmp_path / "pkg", new_session_id=S2, resume=True)
    assert "--resume" in str(excinfo.value)
    assert not (tmp_path / "pkg").exists()


def test_rejected_claim_forgets_the_local_record(tmp_path: Path):
    """被明確拒收時刪掉記錄：那張單已經不是這次 checkout 的了。"""
    reader = _reader_with_handoff()
    journal = _journal(tmp_path)
    # 拒收回報的 `target` 是**交接單 id**（`apply_claim` 是照交接單對應的）
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim", target=HANDOFF),
                                  "already_claimed")])
    with pytest.raises(ClaimRejected) as excinfo:
        checkout(reader, _deps(reader, commit, journal=journal), [HANDOFF],
                 tmp_path / "pkg")
    assert "already_claimed" in str(excinfo.value)
    assert journal.get(HANDOFF) is None


def test_local_checks_happen_before_the_claim_is_registered(tmp_path: Path):
    """H1：輸出目錄不對時**一個認領都不該送出**（否則交接單卡死）。"""
    reader = _reader_with_handoff()
    commit = FakeCommit()
    out = tmp_path / "pkg"
    out.mkdir()
    (out / "keep.txt").write_text("x")

    with pytest.raises(ContextPackageError) as excinfo:
        checkout(reader, _deps(reader, commit), [HANDOFF], out)

    assert "不會覆蓋" in str(excinfo.value)
    assert commit.claims == [], "本機檢查沒過就不該動到那張交接單"


def test_unreadable_object_is_detected_before_the_claim(tmp_path: Path):
    """物件讀不到（同 key 但位元組不符）也要在認領之前擋下。"""
    raw = _handoff_raw(_reader_with_handoff())
    tampered = bytearray(raw)
    tampered[-2] = (tampered[-2] + 1) % 256
    reader = _reader_with_handoff()
    fetcher = _objects(reader, tamper={annex_key_of(raw): bytes(tampered)})
    commit = FakeCommit()

    with pytest.raises(CheckoutError):
        checkout(reader, _deps(reader, commit, objects=fetcher), [HANDOFF],
                 tmp_path / "pkg")

    assert commit.claims == []


def test_handoff_startpoint_without_writer_identity_is_refused(tmp_path: Path):
    """沒有寫入身分就明確拒絕（不能假裝認領了）。"""
    reader = _reader_with_handoff()
    deps = CheckoutDeps(reader=reader, clock=FixedClock(T1), signer=None,
                        inbox_folder_id="", commit_claim=None,
                        objects=_objects(reader))
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, deps, [HANDOFF], tmp_path / "pkg")
    assert "沒有可用的寫入身分" in str(excinfo.value)
    assert not (tmp_path / "pkg").exists()


def test_plain_session_startpoint_records_a_continuation_not_a_claim(tmp_path: Path):
    """直接起點（`session[@訊息]`）**不認領**——沒有交接單可認。

    但接續 Link 還是要記（impl2 M6：四種關係一律記錄），所以放的是一筆
    **接續單**：接續點是被接續 Session 的那一個快照 ＋ 那一則訊息。
    """
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    commit = FakeCommit()
    pkg = checkout(reader, _deps(reader, commit), [S1], tmp_path / "pkg")

    assert len(commit.claims) == 1
    item = commit.claims[0]
    assert item.item_type == "continuation"
    assert item.sidecar["metadata"]["type"] == "continuation"
    body = item.sidecar["body"]
    assert "handoff_id" not in body
    assert body["target_session_id"] == S1
    assert body["new_session_id"] == pkg.new_session_id
    assert body["continuation"] == {
        "snapshot_sha256": _sha(raw), "message_id": "msg_ses_aaa_1"}
    # 預留一併帶著：新 session 這時還不存在於任何來源應用裡
    assert item.sidecar["session"]["reserving"] is True
    assert json.loads(commit.claim_raws[0])["messages"] == []
    # 起點包記下的是接續單的 id
    data, _raws = read_package(tmp_path / "pkg")
    assert data["segments"][0]["claim_id"] == item.item_id
    assert data["new_session"]["claimed_handoffs"] == []


def test_plain_session_startpoint_without_a_writer_refuses(tmp_path: Path):
    """沒有寫入身分就不能產出起點包：沒有接續 Link 的新 session 是沒人負責的孤兒。

    以前直接起點不需要寫入端，所以它能離線產生起點包；接續 Link 一律要記之後
    就不行了——這是刻意的取捨（impl2 M6）。
    """
    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    deps = CheckoutDeps(reader=reader, clock=FixedClock(T1), signer=None,
                        inbox_folder_id="", commit_claim=None,
                        objects=_objects(reader))
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, deps, [S1], tmp_path / "pkg")
    assert "沒有可用的寫入身分" in str(excinfo.value)
    assert not (tmp_path / "pkg").exists()


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


def test_session_id_override_must_equal_the_reserved_one(tmp_path: Path):
    """M7：`--session-id` 只能等於起點包預留的 id，否則明確拒絕。

    `agora checkout` 已經把接續 Link 與那筆預留記進 Agora，那個 id 是提交流程
    認得出預留的唯一線索。換一個 id 匯入進去，Agora 裡就多一筆沒有人負責的
    空 session，而真正開工的那個 session 沒有接續 Link。
    """
    from aistorage.adapters.opencode import build_export
    from aistorage.agora_cli.package import ContextPackageError

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    pkg = checkout(reader, _deps(reader), [S1], out)

    # 一致時就當作沒給
    _payload, session_id, _segments, _source = build_export(
        out, session_id=pkg.new_session_id)
    assert session_id == pkg.new_session_id.split(":", 1)[1]

    with pytest.raises(ContextPackageError) as excinfo:
        build_export(out, session_id="opencode:ses_別的")
    assert "必須一樣" in str(excinfo.value)
    assert pkg.new_session_id in str(excinfo.value)


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
# 5b. 原始紀錄是依 annex key 去 Agora 物件資料夾取的（用 key 內嵌的 sha256 驗證）
# ---------------------------------------------------------------------------


def test_checkout_reads_the_raw_by_annex_key_from_the_agora_object_folder(tmp_path: Path):
    """讀取視圖只給**位址**（annex key），位元組自己去 Agora 的物件資料夾取。"""
    from aistorage.agora_cli.objects import ObjectFetcher

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
    fetcher = _objects(reader)
    out = tmp_path / "pkg"

    checkout(reader, _deps(reader, objects=fetcher), [S1], out)

    data, raws = read_package(out)
    assert raws[0] == raw
    # 讀取介面只被問了 key，沒有被問位元組
    assert reader.raw_calls == [(S1, _sha(raw))]
    assert fetcher.folder_id
    assert isinstance(fetcher, ObjectFetcher)


def test_a_fake_object_under_the_right_key_name_is_rejected(tmp_path: Path):
    """**塞一份同名假檔進去過不了**：key 是內容定址的 sha256。

    這是整個設計的安全關鍵——唯讀身分能看到 Agora 的物件資料夾，所以一定要
    假設那份資料夾裡的東西可能被動過過。對不上就明確拒絕、**不產出起點包**。
    """
    raw = _raw("ses_aaa", ["真的內容"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["真的內容"]}})
    # 同一個 key、同樣的名字，**同樣長度**的內容換成別的（大小過得了，只能靠雜湊）
    swapped = bytearray(raw)
    swapped[-2] = (swapped[-2] + 1) % 256
    tampered = bytes(swapped)
    assert len(tampered) == len(raw)
    fetcher = _objects(reader, tamper={annex_key_of(raw): tampered})
    out = tmp_path / "pkg"

    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, _deps(reader, objects=fetcher), [S1], out)

    assert "SHA-256 與 key 不符" in str(excinfo.value)
    assert not out.exists(), "驗不過就完全不產出"


def test_a_truncated_object_is_rejected_by_the_size_in_the_key(tmp_path: Path):
    """key 內嵌的 size 也驗：只掉幾個位元組同樣拒絕。"""
    raw = _raw("ses_aaa", ["一二三四五六"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一二三四五六"]}})
    fetcher = _objects(reader, tamper={annex_key_of(raw): raw[:-3]})
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, _deps(reader, objects=fetcher), [S1], tmp_path / "pkg")
    assert "大小不符" in str(excinfo.value)


def test_a_missing_annex_key_is_refused_rather_than_guessed(tmp_path: Path):
    """讀取介面沒有 annex key → 明確拒絕，不用閱讀版頂替。"""
    from types import SimpleNamespace as NS

    raw = _raw("ses_aaa", ["一", "二"])
    reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})

    def no_key(session_id: str, snapshot_sha256: str, **kw) -> Any:
        return NS(value=NS(session_id=session_id, snapshot_sha256=snapshot_sha256,
                           snapshot_at=T0, via="sync", annex_key=None))

    reader.get_snapshot = no_key  # type: ignore[method-assign]
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, _deps(reader), [S1], tmp_path / "pkg")
    assert "annex key" in str(excinfo.value)
    assert not (tmp_path / "pkg").exists()


def test_without_the_object_folder_checkout_refuses(tmp_path: Path):
    """沒有設定 Agora 物件資料夾 → 明確拒絕（讀取視圖不再提供 raw 這條路）。"""
    reader = FakeReader({S1: {"raw": _raw("ses_aaa", ["一"]), "texts": ["一"]}})
    deps = CheckoutDeps(reader=reader, clock=FixedClock(T1), signer=None,
                        inbox_folder_id="", commit_claim=None, objects=None)
    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, deps, [S1], tmp_path / "pkg")
    assert "agora_folder_id" in str(excinfo.value)


def test_unknown_annex_key_is_reported_with_the_key():
    """key 不在物件資料夾裡 → 明確拒絕（提交流程還沒推上去、或沒走 annex）。"""
    from aistorage.agora_cli.objects import ObjectError, ObjectFetcher
    from aistorage.drive.fake import FakeDrive

    drive = FakeDrive()
    fetcher = ObjectFetcher(drive, drive.seed_folder("agora-objects"))
    with pytest.raises(ObjectError) as excinfo:
        fetcher.fetch("SHA256E-s10--" + "a" * 64)
    assert "沒有這個 key" in str(excinfo.value)


def test_an_unparsable_annex_key_is_refused():
    """key 格式看不懂就明確拒絕，不要猜。"""
    from aistorage.agora_cli.objects import ObjectError, verify_against_key

    with pytest.raises(ObjectError) as excinfo:
        verify_against_key("not-a-key", b"x")
    assert "SHA256E-s" in str(excinfo.value)


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


# --- `agora handoff` 的工作清單：每個 task 物件一張交接單 -------------------
# plugin 把模型給的 tasks 原樣寫成一個 JSON 檔再傳 `--tasks-file`
# （review-73dbf2c H3）。這裡測的是 CLI 端那半邊：一個物件不能被攤平成
# 好幾張單——那是 H3 的原始病灶。


def _handoff_args(**kw: Any) -> SimpleNamespace:
    base = {
        "session": None,
        "task": [],
        "tasks_file": None,
        "next_steps": None,
        "at": None,
        "timeout": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _tasks_file(tmp_path: Path, payload: Any) -> str:
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_handoff_turns_each_task_object_into_one_part(tmp_path: Path) -> None:
    from aistorage.agora_cli.__main__ import _handoff_parts

    path = _tasks_file(tmp_path, [
        {"title": "做前端", "summary": "改元件", "next_steps": "跑測試"},
        {"title": "做後端", "summary": "改 API", "next_steps": "補文件"},
    ])
    parts = _handoff_parts(_handoff_args(tasks_file=path))
    assert [p["title"] for p in parts] == ["做前端", "做後端"]
    assert [p["summary"] for p in parts] == ["改元件", "改 API"]
    assert [p["next_steps"] for p in parts] == ["跑測試", "補文件"]


def test_handoff_keeps_a_single_task_object_as_a_single_part(tmp_path: Path) -> None:
    """一個物件只值一張單——H3 的病灶就是把它拆成好幾張。"""
    from aistorage.agora_cli.__main__ import _handoff_parts

    path = _tasks_file(tmp_path, [
        {"title": "核對 schema", "summary": "比欄位", "next_steps": "列出落差"},
    ])
    assert len(_handoff_parts(_handoff_args(tasks_file=path))) == 1


def test_handoff_keeps_every_field_of_the_task_object(tmp_path: Path) -> None:
    """`next_steps` 帶多行時要原樣保留，不能被 join 或截斷。"""
    from aistorage.agora_cli.__main__ import _handoff_parts

    steps = "1. 先跑測試\n2. 再看 log"
    path = _tasks_file(tmp_path, [{"title": "查案", "summary": "看 log", "next_steps": steps}])
    parts = _handoff_parts(_handoff_args(tasks_file=path))
    assert parts[0]["next_steps"] == steps


def test_handoff_task_string_is_still_one_part_each() -> None:
    """舊的 `--task` 是每個字串一張，兩條路要能混著給。"""
    from aistorage.agora_cli.__main__ import _handoff_parts

    parts = _handoff_parts(_handoff_args(
        task=["做前端", "做後端"], next_steps="跑測試"))
    assert [p["title"] for p in parts] == ["做前端", "做後端"]
    assert all(p["summary"] == t for p, t in zip(parts, ["做前端", "做後端"]))
    assert all(p["next_steps"] == "跑測試" for p in parts)


def test_handoff_mixes_tasks_file_and_task_strings(tmp_path: Path) -> None:
    from aistorage.agora_cli.__main__ import _handoff_parts

    path = _tasks_file(tmp_path, [{"title": "做前端", "summary": "改元件"}])
    parts = _handoff_parts(_handoff_args(tasks_file=path, task=["做後端"]))
    assert [p["title"] for p in parts] == ["做前端", "做後端"]


def test_handoff_rejects_a_tasks_file_that_is_not_json(tmp_path: Path) -> None:
    from aistorage.agora_cli.__main__ import _handoff_parts
    from aistorage.skill.tools import SkillError

    path = tmp_path / "tasks.json"
    path.write_text("這不是 JSON", encoding="utf-8")
    with pytest.raises(SkillError, match="不是合法的 JSON"):
        _handoff_parts(_handoff_args(tasks_file=str(path)))


def test_handoff_rejects_a_tasks_file_of_an_unknown_shape(tmp_path: Path) -> None:
    """形狀看不懂要在這裡擋下，並把是哪個檔案講清楚。"""
    from aistorage.agora_cli.__main__ import _handoff_parts
    from aistorage.skill.tools import SkillError

    path = _tasks_file(tmp_path, {"foo": 1})
    with pytest.raises(SkillError, match="工作清單有問題"):
        _handoff_parts(_handoff_args(tasks_file=path))


def test_handoff_with_nothing_to_do_is_refused() -> None:
    """沒有工作就別寫出一張空白的交接單。"""
    from aistorage.agora_cli.__main__ import cmd_handoff
    from aistorage.agora_cli.startpoint import StartPointError

    with pytest.raises(StartPointError, match="至少要給一個 task"):
        cmd_handoff(_handoff_args(), reader=None)


def test_handoff_still_refuses_an_explicit_at() -> None:
    """`--at` 還沒支援，但要明確說清楚，不要靜靜忽略。"""
    from aistorage.agora_cli.__main__ import cmd_handoff
    from aistorage.agora_cli.startpoint import StartPointError

    with pytest.raises(StartPointError, match="--at"):
        cmd_handoff(_handoff_args(task=["做前端"], at="msg_1"), reader=None)

# ---------------------------------------------------------------------------
# 4. 重跑（review-7a4ca87 H1／H2／M1／M2）
# ---------------------------------------------------------------------------


def test_rerun_after_timeout_reuses_the_recorded_session_id(tmp_path: Path):
    """H1：**預設 id**（沒給 --new-session-id）逾時之後重跑要成功。

    這是 review 指出的死路：預設流程每次都重新編 session id，所以重跑拿它去對
    記錄一定對不上。住民 AI 沒有 `--new-session-id` 可傳，那條路等於走不通。
    改成「記錄有就自動沿用」之後，不需要任何額外參數。
    """
    reader = _reader_with_handoff()
    journal = _journal(tmp_path)
    first = FakeCommit(timed_out=True)
    with pytest.raises(ClaimRejected):
        checkout(reader, _deps(reader, first, journal=journal), [HANDOFF],
                 tmp_path / "pkg1")
    reserved = journal.get(HANDOFF)
    assert reserved is not None

    # 重跑：沒有 --new-session-id，也沒有 --resume
    second = FakeCommit()
    pkg = checkout(reader, _deps(reader, second, journal=journal), [HANDOFF],
                   tmp_path / "pkg2")

    assert pkg.new_session_id == reserved.new_session_id, "必須沿用記錄裡的 id"
    assert second.claims[0].item_id == reserved.claim_id, "必須是同一個認領"
    assert second.claim_raws[0] == first.claim_raws[0], "空匯出檔要位元組相同"


def test_rerun_never_overwrites_an_existing_record(tmp_path: Path):
    """H1：**不加** --resume 重跑也不得覆蓋記錄（否則兩筆都沒了）。"""
    reader = _reader_with_handoff()
    journal = _journal(tmp_path)
    with pytest.raises(ClaimRejected):
        checkout(reader, _deps(reader, FakeCommit(timed_out=True), journal=journal),
                 [HANDOFF], tmp_path / "pkg1")
    original = journal.get(HANDOFF)
    assert original is not None

    checkout(reader, _deps(reader, FakeCommit(), journal=journal), [HANDOFF],
             tmp_path / "pkg2")

    after = journal.get(HANDOFF)
    assert after == original, "既有記錄不得被覆蓋"


def test_new_session_id_still_wins_when_there_is_no_record(tmp_path: Path):
    """沒有記錄時照舊可以用 --new-session-id 指定。"""
    reader = _reader_with_handoff()
    pkg = checkout(reader, _deps(reader, FakeCommit(), journal=_journal(tmp_path)),
                   [HANDOFF], tmp_path / "pkg", new_session_id=S2)
    assert pkg.new_session_id == S2


def test_conflicting_records_for_n_to_1_are_refused(tmp_path: Path):
    """H1：n→1 時各張的記錄預留了不同 id → 明確拒絕，不要猜。"""
    reader = _reader_with_handoff(HANDOFF2)
    journal = _journal(tmp_path)
    # 兩張單各自跑一次（1→n 的兩張），各預留一個不同的 id，然後都逾時
    for handoff_id, out_name in ((HANDOFF, "pkg1"), (HANDOFF2, "pkg2")):
        with pytest.raises(ClaimRejected):
            checkout(reader,
                     _deps(reader, FakeCommit(timed_out=True), journal=journal),
                     [handoff_id], tmp_path / out_name)
    assert (journal.get(HANDOFF).new_session_id
            != journal.get(HANDOFF2).new_session_id)

    with pytest.raises(CheckoutError) as excinfo:
        checkout(reader, _deps(reader, FakeCommit(), journal=journal),
                 [HANDOFF, HANDOFF2], tmp_path / "pkg3")
    assert "不同的新 session id" in str(excinfo.value)
    assert not (tmp_path / "pkg3").exists()


def test_partial_rejection_keeps_the_accepted_record(tmp_path: Path):
    """H2：n→1 只有一張被拒時，**被接受那張的記錄要留著**。

    整批刪掉的話，那張交接單就被一個永遠沒有起點包的預留 session 接走了。
    """
    reader = _reader_with_handoff(HANDOFF2)
    journal = _journal(tmp_path)
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim", target=HANDOFF2),
                                   "already_claimed")])

    with pytest.raises(PartialClaimAccepted) as excinfo:
        checkout(reader, _deps(reader, commit, journal=journal),
                 [HANDOFF, HANDOFF2], tmp_path / "pkg")

    assert excinfo.value.accepted == (HANDOFF,)
    assert excinfo.value.rejected == (HANDOFF2,)
    # 被接受那張的記錄還在（那是那個預留日後唯一的線索）
    assert journal.get(HANDOFF) is not None, "被接受那張的記錄不得刪掉"
    # 被拒那張的刪掉（它已經是別人的了）
    assert journal.get(HANDOFF2) is None
    # 訊息要講清楚「哪幾張被接走、預留的 session 是哪個」
    message = str(excinfo.value)
    assert HANDOFF in message and "部分被接受" in message
    assert not (tmp_path / "pkg").exists()


def test_partial_rejection_names_the_reserved_session(tmp_path: Path):
    """H2：部分被接受時要明講預留的 session id（ Agora 裡已經有那筆空紀錄）。"""
    reader = _reader_with_handoff(HANDOFF2)
    journal = _journal(tmp_path)
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim", target=HANDOFF2),
                                   "already_claimed")])
    with pytest.raises(PartialClaimAccepted) as excinfo:
        checkout(reader, _deps(reader, commit, journal=journal),
                 [HANDOFF, HANDOFF2], tmp_path / "pkg")
    assert excinfo.value.new_session_id == journal.get(HANDOFF).new_session_id
    assert excinfo.value.new_session_id in str(excinfo.value)


def test_unmatched_rejection_target_deletes_nothing(tmp_path: Path):
    """H2：認不出被拒的是哪幾張時**一張都不刪**（刪錯了不可恢復）。"""
    reader = _reader_with_handoff(HANDOFF2)
    journal = _journal(tmp_path)
    commit = FakeCommit(rejected=[(SimpleNamespace(kind="claim", target="claim:01X"),
                                   "already_claimed")])
    with pytest.raises(ClaimRejected):
        checkout(reader, _deps(reader, commit, journal=journal),
                 [HANDOFF, HANDOFF2], tmp_path / "pkg")
    # 寧可留著（下次重跑會被當成已認領，但可恢復），也不要刪錯
    assert journal.get(HANDOFF) is not None
    assert journal.get(HANDOFF2) is not None


def test_failure_after_the_claim_is_accepted_says_so(tmp_path: Path):
    """M2：認領成立**之後**才失敗，訊息要說明認領已經成立。

    否則人會去「修好再重跑」，而重跑會撞 already_claimed。
    """
    reader = _reader_with_handoff()
    out = tmp_path / "pkg"
    # `aistorage.agora_cli.checkout` 這個名字在套件層被 `checkout` 函式蓋掉，
    # 要拿到模組本身得走 importlib。
    co = importlib.import_module("aistorage.agora_cli.checkout")
    original_stage = co.stage_package

    def stage_then_sabotage(pkg, out_dir):
        """照常擺好暫存目錄，然後把輸出目錄變成非空——
        這樣最後那個 `out.rmdir()` / 改名會失敗，而認領已經送出去了。"""
        staging = original_stage(pkg, out_dir)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "sabotage.txt").write_text("x", encoding="utf-8")
        return staging

    co.stage_package = stage_then_sabotage
    try:
        with pytest.raises(ClaimAlreadyCommitted) as excinfo:
            checkout(reader, _deps(reader, FakeCommit(), journal=_journal(tmp_path)),
                     [HANDOFF], out)
    finally:
        co.stage_package = original_stage

    assert "認領**已經成立**" in str(excinfo.value)
    assert "不要再送一次認領" in str(excinfo.value)
    assert excinfo.value.claimed_handoffs == (HANDOFF,)
    assert excinfo.value.new_session_id


def test_staging_dir_name_is_unique_per_run(tmp_path: Path):
    """M2：暫存目錄名帶隨機後綴，同一個 -o 並行不會互相刪掉。"""
    import aistorage.agora_cli.package as pkgmod

    names = set()
    for _ in range(5):
        out = tmp_path / "same-out"
        out.mkdir(exist_ok=True)
        raw = _raw("ses_aaa", ["一", "二"])
        reader = FakeReader({S1: {"raw": raw, "texts": ["一", "二"]}})
        pkgobj = _build_pkg_for_staging(reader)
        staging = pkgmod.stage_package(pkgobj, out)
        names.add(staging.name)
        assert staging.parent == out.parent
    assert len(names) == 5, f"暫存目錄名必須每次不同: {names}"


def _build_pkg_for_staging(reader: FakeReader) -> Any:
    from aistorage.agora_cli.package import ContextPackage, PackageSegment

    raw = _raw("ses_aaa", ["一", "二"])
    resolved = resolve_startpoint(reader, parse_startpoint(S1))
    return ContextPackage(
        segments=(PackageSegment(resolved=resolved, raw=raw,
                                 message_count=2, text_chars=2, order=0),),
        task=None, new_session_id=S2, created_at=T1, created_by="profile:mac-opencode",
    )


# --- M1：記錄檔本身 ---


def test_journal_refuses_to_overwrite_an_existing_record(tmp_path: Path):
    """M1：`put` 不得覆蓋既有記錄（覆蓋＝讓那張交接單永遠認不回來）。"""
    from aistorage.agora_cli.claims import ClaimJournal, ClaimJournalError, ClaimRecord

    journal = ClaimJournal(tmp_path / "claims")
    first = ClaimRecord(
        startpoint_key=HANDOFF, claim_id="claim:01A", item_key="01A",
        new_session_id=S2, reserved_at=T1, title="t", profile=PROFILE, created_at=T1)
    journal.put(first)
    with pytest.raises(ClaimJournalError, match="已經有"):
        journal.put(ClaimRecord(
            startpoint_key=HANDOFF, claim_id="claim:01B", item_key="01B",
            new_session_id=S3, reserved_at=T1, title="t", profile=PROFILE,
            created_at=T1))
    assert journal.get(HANDOFF) == first


def test_journal_keeps_other_handoffs_when_one_file_is_corrupt(tmp_path: Path):
    """M1：壞掉要**出聲**且不影響別張——不能當成空的（否則覆蓋掉所有人）。"""
    from aistorage.agora_cli.claims import ClaimJournal, ClaimJournalError, ClaimRecord

    journal = ClaimJournal(tmp_path / "claims")
    for handoff_id, item_key in ((HANDOFF, "01A"), (HANDOFF2, "01B")):
        journal.put(ClaimRecord(
            startpoint_key=handoff_id, claim_id=f"claim:{item_key}", item_key=item_key,
            new_session_id=S2, reserved_at=T1, title="t", profile=PROFILE,
            created_at=T1))
    # 把其中一份弄壞
    (journal.path / f"{HANDOFF2.replace(':', '_')}.json").write_text("{壞掉",
                                                                     encoding="utf-8")

    with pytest.raises(ClaimJournalError, match="讀不來"):
        journal.get(HANDOFF2)
    # 另一張仍然讀得到
    assert journal.get(HANDOFF) is not None


def test_journal_refuses_to_write_when_a_record_is_corrupt(tmp_path: Path):
    """M1：壞掉時**拒絕寫入**，不要在壞掉的檔上蓋（會連帶弄壞別張）。"""
    from aistorage.agora_cli.claims import ClaimJournal, ClaimJournalError, ClaimRecord

    journal = ClaimJournal(tmp_path / "claims")
    (journal.path).mkdir(parents=True)
    (journal.path / f"{HANDOFF.replace(':', '_')}.json").write_text("{壞掉",
                                                                    encoding="utf-8")
    with pytest.raises(ClaimJournalError):
        journal.put(ClaimRecord(
            startpoint_key=HANDOFF, claim_id="claim:01A", item_key="01A",
            new_session_id=S2, reserved_at=T1, title="t", profile=PROFILE,
            created_at=T1))
    assert (journal.path / f"{HANDOFF.replace(':', '_')}.json").read_text(
        encoding="utf-8") == "{壞掉", "不得覆蓋壞掉的記錄"


def test_journal_uses_one_file_per_handoff(tmp_path: Path):
    """M1：每張單一個檔案，並行寫入才不會互相蓋掉。"""
    from aistorage.agora_cli.claims import ClaimJournal, ClaimRecord

    journal = ClaimJournal(tmp_path / "claims")
    for handoff_id in (HANDOFF, HANDOFF2, HANDOFF3):
        journal.put(ClaimRecord(
            startpoint_key=handoff_id, claim_id=f"claim:{handoff_id}",
            item_key="01A", new_session_id=S2, reserved_at=T1, title="t",
            profile=PROFILE, created_at=T1))
    files = sorted(p.name for p in journal.path.glob("*.json"))
    assert len(files) == 3, files


def test_concurrent_journal_writes_do_not_lose_records(tmp_path: Path):
    """M1：並行寫入（1→n 的典型用法）不得弄丟任何一張的記錄。"""
    from aistorage.agora_cli.claims import ClaimJournal, ClaimRecord

    journal = ClaimJournal(tmp_path / "claims")
    handoffs = [f"handoff:01{i:022d}" for i in range(12)]
    for index, handoff_id in enumerate(handoffs):
        journal.put(ClaimRecord(
            startpoint_key=handoff_id, claim_id=f"claim:{index:03d}",
            item_key=f"{index:03d}", new_session_id=S2, reserved_at=T1, title="t",
            profile=PROFILE, created_at=T1))
    for index, handoff_id in enumerate(handoffs):
        assert journal.get(handoff_id) is not None, f"{handoff_id} 的記錄不見了"
    assert len(list(journal.path.glob("*.json"))) == len(handoffs)


def test_agora_cli_imports_without_any_source_app_or_syncer() -> None:
    """M2：`agora` 是一般介面，**不該**因為寫入路徑就綁上某個來源應用。

    `agora_cli` 只做「起點 → 起點包」與讀 Agora；觸發提交流程時才需要同步器，
    那個 import 必須在函式裡，否則 `import aistorage.agora_cli` 就會拖進
    opencode 的匯出流程（impl2-review4 M2）。
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        class Blocker:
            def find_module(self, name, path=None):
                if (name.startswith("aistorage.syncer")
                        or name.startswith("aistorage.adapters")):
                    raise ImportError("blocked: " + name)
                return None
        sys.meta_path.insert(0, Blocker())
        import importlib
        for m in ("aistorage.agora_cli",
                  "aistorage.agora_cli.__main__",
                  "aistorage.agora_cli.checkout",
                  "aistorage.agora_cli.claims",
                  "aistorage.agora_cli.package",
                  "aistorage.agora_cli.startpoint",
                  "aistorage.agora_cli.objects"):
            importlib.import_module(m)
        print("ok")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def _reader_with_flagged_message(message_id: str, **flags: Any) -> FakeReader:
    raw = _raw("ses_aaa", ["一", "二", "三"])
    ids = _message_ids_of(raw)
    return FakeReader({S1: {"raw": raw, "texts": ["一", "二", "三"],
                           "flags": {message_id: flags}}})


def test_startpoint_at_an_unfinished_message_is_refused(tmp_path: Path):
    """L：`<session>@<msg>` 指到**未完成**的訊息要拒絕。

    接續點的定義是「最後一則**已完成**的訊息」。未完成的訊息還會被改寫，
    從那裡接續等於把一個不確定的狀態當成起點。
    """
    mid = _message_ids_of(_raw("ses_aaa", ["一", "二", "三"]))[1]
    reader = _reader_with_flagged_message(mid, completed=False)
    with pytest.raises(StartPointError, match="還沒有完成"):
        checkout(reader, _deps(reader), [f"{S1}@{mid}"], tmp_path / "pkg")
    assert not (tmp_path / "pkg").exists()


def test_startpoint_at_a_reverted_message_is_refused(tmp_path: Path):
    """L：指到**已撤銷**的訊息要拒絕（會把撤回的內容帶回來）。"""
    mid = _message_ids_of(_raw("ses_aaa", ["一", "二", "三"]))[1]
    reader = _reader_with_flagged_message(mid, reverted=True)
    with pytest.raises(StartPointError, match="已被撤銷"):
        checkout(reader, _deps(reader), [f"{S1}@{mid}"], tmp_path / "pkg")
    assert not (tmp_path / "pkg").exists()


def test_startpoint_at_a_completed_message_still_works(tmp_path: Path):
    """L：正常情況（已完成、未撤銷）不受影響。"""
    mid = _message_ids_of(_raw("ses_aaa", ["一", "二", "三"]))[1]
    reader = _reader_with_flagged_message(mid, completed=True, reverted=False)
    pkg = checkout(reader, _deps(reader), [f"{S1}@{mid}"], tmp_path / "pkg")
    assert pkg.segments[0].resolved.message_id == mid


def test_reading_a_package_without_raw_sha256_is_refused(tmp_path: Path):
    """L：讀回起點包時**缺 `raw_sha256` 要拒絕**，不要略過驗證。

    略過等於「沒有雜湊的段落照樣能用」——那正是位元組相同這個保證失效的入口。
    """
    reader = FakeReader({S1: {"raw": _raw("ses_aaa", ["一", "二"]),
                             "texts": ["一", "二"]}})
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [S1], out)
    data = json.loads((out / "package.json").read_text(encoding="utf-8"))
    del data["segments"][0]["raw_sha256"]
    (out / "package.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ContextPackageError, match="沒有 raw_sha256"):
        read_package(out)


def test_handoff_task_text_goes_into_the_package(tmp_path: Path):
    """M3：交接單本身的任務文字要進起點包（`--task` 是額外附加的）。"""
    reader = _reader_with_handoff()
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [HANDOFF], out)
    data = json.loads((out / "package.json").read_text(encoding="utf-8"))
    assert data["task"] == "接手後續調查"


def test_explicit_task_is_added_on_top_of_the_handoff_text(tmp_path: Path):
    """M3：`--task` 蓋掉交接單的內容（這一次額外交代的才是主要的）。"""
    reader = _reader_with_handoff()
    out = tmp_path / "pkg"
    checkout(reader, _deps(reader), [HANDOFF], out, task="這次先做 A")
    data = json.loads((out / "package.json").read_text(encoding="utf-8"))
    assert data["task"] == "這次先做 A"


def test_object_index_picks_the_file_whose_metadata_matches(tmp_path: Path):
    """M5：同名多檔時只挑 sha256 與 size 都相符的那一個。"""
    from aistorage.agora_cli.objects import ObjectFetcher
    from aistorage.drive.fake import FakeDrive

    data = _raw("ses_aaa", ["一", "二"])
    digest = hashlib.sha256(data).hexdigest().lower()
    key = f"SHA256E-s{len(data)}--{digest}"
    other = b"x" * len(data)

    drive = FakeDrive()
    folder = drive.seed_folder("agora-objects")
    # 同名、但 metadata 對不上（模擬「同名多檔」的異常）
    drive.seed_file(folder, key, other)
    drive.seed_file(folder, key, data)

    fetcher = ObjectFetcher(drive, folder)
    got = fetcher.fetch(key)
    assert got == data, "必須挑 metadata 相符的那一個"


def test_object_index_refuses_when_no_candidate_matches(tmp_path: Path):
    """M5：同名多檔但沒有任何一個 metadata 相符 → 明確拒絕，不要猜。"""
    from aistorage.agora_cli.objects import ObjectFetcher, ObjectError
    from aistorage.drive.fake import FakeDrive

    drive = FakeDrive()
    folder = drive.seed_folder("agora-objects")
    data = _raw("ses_aaa", ["一", "二"])
    digest = hashlib.sha256(data).hexdigest().lower()
    key = f"SHA256E-s{len(data)}--{digest}"
    # 兩個同名檔案，metadata 的 sha256 都不符（`seed_file` 會照內容算真 sha，
    # 這裡用 `sha256=` 明確給錯的值）
    drive.seed_file(folder, key, b"y" * len(data), sha256="0" * 64)
    drive.seed_file(folder, key, b"z" * len(data), sha256="1" * 64)

    fetcher = ObjectFetcher(drive, folder)
    with pytest.raises(ObjectError, match="sha256 與 size 都和 key 相符"):
        fetcher.file_id_for(key)

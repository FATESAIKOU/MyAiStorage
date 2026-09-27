"""單一 Session 手動匯入工具（tasks 3.11）冒煙測試。

涵蓋：`build_inbox_item` 產出的 sidecar 必須通過提交流程自己的檢查
（validate_sidecar／check_raw／verify_sidecar_bytes）、--out-dir 三個檔案的
形狀、重複匯入對到同一個 id、來源端 Session id 的推測、錯誤處理，
以及私鑰絕不外洩。
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from aistorage.clock import FixedClock
from aistorage.converters.base import SessionFacts
from aistorage.importer import ImportResult, import_session, main
from aistorage.inbox import (
    check_raw,
    is_complete,
    validate_sidecar,
    verify_sidecar_bytes,
)
from aistorage.inbox_builder import (
    InboxBuildError,
    build_inbox_item,
    detect_source_session_id,
    load_private_key,
    read_item_key,
)

DATA_DIR = Path(__file__).parent / "data" / "converters"
OPENCODE_EXPORT = DATA_DIR / "opencode" / "basic.json"
CLAUDE_JSONL = DATA_DIR / "claude-code" / "basic.jsonl"
NOW = "2026-09-27T12:00:00Z"
KEY_ID = "mac-opencode-1a2b3c4d"
PROFILE = "mac-opencode"
PARENT_SESSION = "11111111-1111-4111-8111-999999999999"


@pytest.fixture
def keypair(tmp_path: Path) -> tuple[bytes, bytes, Path]:
    """產生 600 權限的私鑰檔與對應公鑰。"""
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(priv_bytes)
    key_path.chmod(0o600)
    return priv_bytes, pub_bytes, key_path


def _read_out(out_dir: Path, item_key: str) -> tuple[bytes, dict, bytes]:
    sidecar_bytes = (out_dir / f"{item_key}.sidecar.json").read_bytes()
    sig = json.loads((out_dir / f"{item_key}.sig").read_text(encoding="utf-8"))
    raw = (out_dir / f"{item_key}.raw").read_bytes()
    return sidecar_bytes, sig, raw


# ------------------------------------------------------------ build_inbox_item


def test_build_inbox_item_passes_committer_side_checks(tmp_path: Path, keypair):
    """產出的 sidecar 必須通過提交流程（3.3）自己會做的每一項檢查。"""
    priv_bytes, pub_bytes, _key_path = keypair
    facts = SessionFacts(
        title="匯入測試",
        created_at="2026-09-27T08:00:00Z",
        updated_at="2026-09-27T09:00:00Z",
        message_ids=("m1",),
        archived_at=None,
        last_message_at="2026-09-27T09:00:00Z",
        in_progress=False,
    )
    sidecar_bytes, sig = build_inbox_item(
        OPENCODE_EXPORT,
        source="opencode",
        source_session_id="ses_opencode_basic_001",
        facts=facts,
        profile=PROFILE,
        key=priv_bytes,
        key_id=KEY_ID,
        clock=FixedClock(NOW),
    )
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))

    # 1. 格式驗證
    assert validate_sidecar(sidecar, expected_item_key=sidecar["item_key"]) == []
    # 2. raw 一致性（提交流程就是這樣拿到 raw 的）
    assert check_raw(sidecar, OPENCODE_EXPORT.read_bytes()) == []
    # 3. 驗章（簽章涵蓋 sidecar 的原始位元組）
    assert verify_sidecar_bytes(sidecar_bytes, sig, {KEY_ID: pub_bytes}) == KEY_ID
    # 4. 換一把金鑰就驗不過
    assert verify_sidecar_bytes(sidecar_bytes, sig, {"other-key": pub_bytes}) is None
    # 5. sidecar 位元組被改過一個空白就驗不過（不得重新格式化）
    tampered = sidecar_bytes.replace(b'"profile"', b'"profile" ')
    assert verify_sidecar_bytes(tampered, sig, {KEY_ID: pub_bytes}) is None

    assert sidecar["format"] == "aistorage.inbox/v1"
    assert sidecar["profile"] == PROFILE
    assert sidecar["metadata"]["id"] == "opencode:ses_opencode_basic_001"
    assert sidecar["metadata"]["type"] == "session"
    assert sidecar["session"]["snapshot_at"] == NOW
    assert sidecar["session"]["status"] == "running"
    assert sidecar["session"]["stopped_at"] is None
    assert sidecar["session"]["in_progress"] is False
    assert sidecar["raw"] == {
        "sha256": hashlib.sha256(OPENCODE_EXPORT.read_bytes()).hexdigest(),
        "size": OPENCODE_EXPORT.stat().st_size,
    }
    # 寫入者不填產生者（由介面蓋）
    assert "producer" not in sidecar["metadata"]


def test_build_inbox_item_status_and_in_progress_rules(tmp_path: Path, keypair):
    priv_bytes, _pub, _kp = keypair
    running = SessionFacts(None, None, None, (), None, None, False)
    in_progress = SessionFacts(None, None, None, (), None, None, True)

    _b, sig_stopped = build_inbox_item(
        OPENCODE_EXPORT,
        source="opencode",
        source_session_id="ses_1",
        facts=running,
        profile=PROFILE,
        key=priv_bytes,
        key_id=KEY_ID,
        status="stopped",
        stopped_at="2026-09-27T11:00:00Z",
        clock=FixedClock(NOW),
    )
    assert sig_stopped["key_id"] == KEY_ID

    with pytest.raises(InboxBuildError, match="in_progress"):
        build_inbox_item(
            OPENCODE_EXPORT,
            source="opencode",
            source_session_id="ses_1",
            facts=in_progress,
            profile=PROFILE,
            key=priv_bytes,
            key_id=KEY_ID,
            status="stopped",
            stopped_at="2026-09-27T11:00:00Z",
        )

    with pytest.raises(InboxBuildError, match="stopped_at"):
        build_inbox_item(
            OPENCODE_EXPORT,
            source="opencode",
            source_session_id="ses_1",
            facts=running,
            profile=PROFILE,
            key=priv_bytes,
            key_id=KEY_ID,
            status="stopped",
        )

    with pytest.raises(InboxBuildError, match="running"):
        build_inbox_item(
            OPENCODE_EXPORT,
            source="opencode",
            source_session_id="ses_1",
            facts=running,
            profile=PROFILE,
            key=priv_bytes,
            key_id=KEY_ID,
            stopped_at="2026-09-27T11:00:00Z",
        )


def test_build_inbox_item_rejects_bad_arguments(tmp_path: Path, keypair):
    priv_bytes, _pub, _kp = keypair
    facts = SessionFacts(None, None, None, (), None, None, False)
    common = dict(
        source="opencode",
        source_session_id="ses_1",
        facts=facts,
        profile=PROFILE,
        key=priv_bytes,
        key_id=KEY_ID,
    )
    missing = tmp_path / "nope.json"
    with pytest.raises(InboxBuildError, match="找不到原始紀錄"):
        build_inbox_item(missing, **common)
    with pytest.raises(InboxBuildError, match="profile"):
        build_inbox_item(OPENCODE_EXPORT, **{**common, "profile": "Bad Profile"})
    with pytest.raises(InboxBuildError, match="item_key"):
        build_inbox_item(OPENCODE_EXPORT, **{**common, "item_key": "not-a-ulid"})
    with pytest.raises(InboxBuildError, match="item_key"):
        build_inbox_item(OPENCODE_EXPORT, **common, item_key="01ARZ3NDEKTSV4RRFFQ69G5FA")
    with pytest.raises(InboxBuildError, match="parent_id"):
        build_inbox_item(OPENCODE_EXPORT, **common, parent_id="session:bad")
    with pytest.raises(ValueError, match="source_session_id"):
        build_inbox_item(OPENCODE_EXPORT, **{**common, "source_session_id": " "})


def test_build_inbox_item_enforces_raw_size_limit(tmp_path: Path, keypair):
    priv_bytes, _pub, _kp = keypair
    facts = SessionFacts(None, None, None, (), None, None, False)
    with pytest.raises(InboxBuildError, match="上限"):
        build_inbox_item(
            OPENCODE_EXPORT,
            source="opencode",
            source_session_id="ses_1",
            facts=facts,
            profile=PROFILE,
            key=priv_bytes,
            key_id=KEY_ID,
            max_raw=10,
        )


def test_parent_id_gets_source_prefix(tmp_path: Path, keypair):
    priv_bytes, _pub, _kp = keypair
    facts = SessionFacts(None, None, None, (), None, None, False)
    sidecar_bytes, _sig = build_inbox_item(
        OPENCODE_EXPORT,
        source="opencode",
        source_session_id="ses_child",
        facts=facts,
        profile=PROFILE,
        key=priv_bytes,
        key_id=KEY_ID,
        parent_id="ses_parent",
    )
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert sidecar["session"]["parent_id"] == "opencode:ses_parent"
    assert validate_sidecar(sidecar, expected_item_key=sidecar["item_key"]) == []


# ------------------------------------------------------------------ 私鑰


def test_load_private_key_requires_600(tmp_path: Path, keypair):
    _priv, _pub, key_path = keypair
    assert len(load_private_key(key_path)) == 32

    key_path.chmod(0o644)
    with pytest.raises(InboxBuildError, match="權限過寬"):
        load_private_key(key_path)

    short = tmp_path / "short.key"
    short.write_bytes(b"too-short")
    short.chmod(0o600)
    with pytest.raises(InboxBuildError, match="32 位元組"):
        load_private_key(short)

    with pytest.raises(InboxBuildError, match="找不到私鑰"):
        load_private_key(tmp_path / "absent.key")


# ------------------------------------------------- 來源端 Session id 推測


def test_detect_source_session_id(tmp_path: Path):
    assert detect_source_session_id("opencode", OPENCODE_EXPORT) == "ses_opencode_basic_001"
    # jsonl：取最常見的 sessionId（子代理紀錄的次數較少）
    assert detect_source_session_id("claude-code", CLAUDE_JSONL) == PARENT_SESSION

    # 沒有 sessionId 時退回檔名
    no_sid = tmp_path / "abc123.jsonl"
    no_sid.write_text('{"type":"user","uuid":"u1","timestamp":"2026-09-27T08:00:00Z"}\n', encoding="utf-8")
    assert detect_source_session_id("claude-code", no_sid) == "abc123"

    broken = tmp_path / "broken.jsonl"
    broken.write_text("{oops}\n", encoding="utf-8")
    with pytest.raises(InboxBuildError, match="解析失敗"):
        detect_source_session_id("claude-code", broken)

    with pytest.raises(InboxBuildError, match="不支援的來源應用"):
        detect_source_session_id("other-app", OPENCODE_EXPORT)


# ------------------------------------------------------------- import_session


def test_import_opencode_to_out_dir(tmp_path: Path, keypair):
    priv_bytes, pub_bytes, key_path = keypair
    out = tmp_path / "out"
    result = import_session(
        "opencode",
        OPENCODE_EXPORT,
        key_path=key_path,
        key_id=KEY_ID,
        profile=PROFILE,
        out_dir=out,
        clock=FixedClock(NOW),
    )
    assert isinstance(result, ImportResult)
    assert result.item_id == "opencode:ses_opencode_basic_001"
    assert result.source_session_id == "ses_opencode_basic_001"
    assert result.destination == f"out-dir:{out}"
    assert result.status == "running"
    assert result.snapshot_at == NOW
    assert result.raw_sha256 == hashlib.sha256(OPENCODE_EXPORT.read_bytes()).hexdigest()
    assert result.raw_size == OPENCODE_EXPORT.stat().st_size

    # 三個檔案，名稱符合收件匣項目的形狀，且 is_complete 為真
    assert sorted(result.written) == sorted([
        f"{result.item_key}.raw",
        f"{result.item_key}.sidecar.json",
        f"{result.item_key}.sig",
    ])
    assert sorted(os.listdir(out)) == sorted(result.written)

    sidecar_bytes, sig, raw = _read_out(out, result.item_key)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert is_complete(set(os.listdir(out)), result.item_key, sidecar) is True
    assert read_item_key(sidecar_bytes) == result.item_key
    # 寫出的 sidecar 位元組就是簽章時的那份（驗章直接對磁碟上的檔案）
    assert verify_sidecar_bytes(sidecar_bytes, sig, {KEY_ID: pub_bytes}) == KEY_ID
    # raw 逐位元組等於來源檔
    assert raw == OPENCODE_EXPORT.read_bytes()
    assert check_raw(sidecar, raw) == []


def test_import_claude_code_to_out_dir(tmp_path: Path, keypair):
    _priv, pub_bytes, key_path = keypair
    out = tmp_path / "out"
    result = import_session(
        "claude-code",
        CLAUDE_JSONL,
        key_path=key_path,
        key_id=KEY_ID,
        profile=PROFILE,
        out_dir=out,
        status="stopped",
        stopped_at="2026-09-27T11:59:00Z",
        case_id="case-abc",
        provenance="mac 手動匯入",
        clock=FixedClock(NOW),
    )
    assert result.item_id == f"claude-code:{PARENT_SESSION}"
    assert result.source == "claude-code"

    sidecar_bytes, sig, _raw = _read_out(out, result.item_key)
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert sidecar["session"]["source"] == "claude-code"
    assert sidecar["session"]["status"] == "stopped"
    assert sidecar["session"]["stopped_at"] == "2026-09-27T11:59:00Z"
    assert sidecar["metadata"]["case_id"] == "case-abc"
    assert sidecar["metadata"]["provenance"] == "mac 手動匯入"
    assert sidecar["session"]["snapshot_at"] == NOW
    assert validate_sidecar(sidecar, expected_item_key=result.item_key) == []
    assert verify_sidecar_bytes(sidecar_bytes, sig, {KEY_ID: pub_bytes}) == KEY_ID


def test_repeated_import_maps_to_same_item_id(tmp_path: Path, keypair):
    _priv, pub_bytes, key_path = keypair
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    first = import_session(
        "opencode", OPENCODE_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out1, clock=FixedClock(NOW),
    )
    second = import_session(
        "opencode", OPENCODE_EXPORT, key_path=key_path, key_id=KEY_ID,
        profile=PROFILE, out_dir=out2, clock=FixedClock("2026-09-27T13:00:00Z"),
    )
    # 同一個 id、同一份內容 → 提交流程會判 ALREADY（3.4）
    assert first.item_id == second.item_id == "opencode:ses_opencode_basic_001"
    assert first.raw_sha256 == second.raw_sha256
    # 每次匯入是不同的收件匣項目（item_key 與快照時間不同）
    assert first.item_key != second.item_key
    assert first.snapshot_at != second.snapshot_at

    sidecar1 = json.loads(_read_out(out1, first.item_key)[0].decode("utf-8"))
    sidecar2 = json.loads(_read_out(out2, second.item_key)[0].decode("utf-8"))
    assert sidecar1["metadata"]["id"] == sidecar2["metadata"]["id"]
    assert sidecar1["raw"] == sidecar2["raw"]


def test_import_session_argument_errors(tmp_path: Path, keypair):
    _priv, _pub, key_path = keypair
    out = tmp_path / "out"
    common = dict(key_path=key_path, key_id=KEY_ID, profile=PROFILE)

    with pytest.raises(InboxBuildError, match="不支援的來源應用"):
        import_session("other-app", OPENCODE_EXPORT, out_dir=out, **common)
    with pytest.raises(InboxBuildError, match="只能指定"):
        import_session("opencode", OPENCODE_EXPORT, **common)
    with pytest.raises(InboxBuildError, match="只能指定"):
        import_session(
            "opencode", OPENCODE_EXPORT, out_dir=out, inbox_folder_id="fid", **common
        )
    with pytest.raises(InboxBuildError, match="找不到原始紀錄"):
        import_session("opencode", tmp_path / "nope.json", out_dir=out, **common)

    # 仍在生成中的 Session 不能宣告停止中
    streaming = tmp_path / "streaming.jsonl"
    streaming.write_text(
        '{"parentUuid":null,"isSidechain":false,"sessionId":"s1","type":"user",'
        '"message":{"role":"user","content":"嗨"},"uuid":"u1",'
        '"timestamp":"2026-09-27T08:00:00.000Z"}\n'
        '{"parentUuid":"u1","isSidechain":false,"sessionId":"s1","type":"assistant",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"生成中…"}],'
        '"stop_reason":null},"uuid":"a1","timestamp":"2026-09-27T08:00:01.000Z"}\n',
        encoding="utf-8",
    )
    with pytest.raises(InboxBuildError, match="in_progress"):
        import_session(
            "claude-code", streaming, out_dir=out, status="stopped",
            stopped_at=NOW, **common,
        )


def test_import_propagates_conversion_error(tmp_path: Path, keypair):
    """轉換失敗時不產生任何半成品。"""
    _priv, _pub, key_path = keypair
    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json}\n", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(Exception):
        import_session(
            "claude-code", broken, key_path=key_path, key_id=KEY_ID,
            profile=PROFILE, out_dir=out, clock=FixedClock(NOW),
        )
    assert not out.exists()


# ------------------------------------------------------------------- CLI


def test_cli_opencode_prints_only_public_info(tmp_path: Path, keypair, capsys):
    import base64
    import re

    priv_bytes, _pub, key_path = keypair
    out = tmp_path / "out"
    code = main([
        "opencode",
        "--export", str(OPENCODE_EXPORT),
        "--key", str(key_path),
        "--key-id", KEY_ID,
        "--profile", PROFILE,
        "--out-dir", str(out),
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "item_id: opencode:ses_opencode_basic_001" in captured.out
    assert "item_key:" in captured.out
    # 快照時間由 CLI 執行當下決定（RFC 3339 UTC Z）
    printed = re.search(r"^snapshot_at: (\S+)$", captured.out, re.M)
    assert printed and re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", printed.group(1))
    sidecar = json.loads(
        next(p for p in out.iterdir() if p.name.endswith(".sidecar.json"))
        .read_text(encoding="utf-8")
    )
    assert sidecar["session"]["snapshot_at"] == printed.group(1)
    assert "written: 3 個檔案" in captured.out
    # 私鑰絕不出現在輸出裡
    assert priv_bytes.hex() not in captured.out
    assert base64.b64encode(priv_bytes).decode() not in captured.out
    assert priv_bytes.hex() not in captured.err
    assert len(list(out.iterdir())) == 3


def test_cli_claude_code_stopped_and_session_id_override(tmp_path: Path, keypair, capsys):
    _priv, _pub, key_path = keypair
    out = tmp_path / "out"
    code = main([
        "claude-code",
        "--jsonl", str(CLAUDE_JSONL),
        "--key", str(key_path),
        "--key-id", KEY_ID,
        "--profile", PROFILE,
        "--out-dir", str(out),
        "--stopped",
        "--stopped-at", "2026-09-27T11:59:00Z",
        "--session-id", "explicit-session",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert "item_id: claude-code:explicit-session" in captured.out
    assert "status: stopped" in captured.out

    sidecar_bytes, _sig, _raw = _read_out(out, read_item_key(
        next(p for p in out.iterdir() if p.name.endswith(".sidecar.json")).read_bytes()
    ))
    sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    assert sidecar["session"]["source_session_id"] == "explicit-session"
    assert sidecar["session"]["stopped_at"] == "2026-09-27T11:59:00Z"


def test_cli_reports_errors_without_traceback(tmp_path: Path, keypair, capsys):
    _priv, _pub, key_path = keypair
    code = main([
        "opencode",
        "--export", str(tmp_path / "missing.json"),
        "--key", str(key_path),
        "--key-id", KEY_ID,
        "--profile", PROFILE,
        "--out-dir", str(tmp_path / "out"),
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "匯入失敗" in captured.err
    assert "Traceback" not in captured.err

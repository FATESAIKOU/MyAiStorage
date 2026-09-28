"""取得原始紀錄（`OpencodeApi.export`）的完整性与取法（impl1／9.5 e2e）。

背景：9.5 e2e 實測到 `opencode export` 在 stdout 是 **pipe** 時，輸出超過約
64 KiB 就會以 rc=0 回傳被截斷的 JSON（`Unterminated string`），整個 Session 收不進
Agora。實測記錄見 `docs/spike/evidence/impl1-export-truncation.md`。

這裡用假的 `subprocess.run` 與假的 API 端點測三件事：
1. stdout 開成一般檔案、而且**不用 shell**（session id 不被二次解讀）；
2. 被截斷／形狀不符／對不上 API 的輸出都要被擋下，且**不留半份檔案**；
3. 完整時寫出去的位元組與子行程寫出來的**完全相同**（原始紀錄要原封不動）。

不需要 opencode、不需要模型、不碰網路。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence

import pytest

from aistorage.errors import IncompleteFetch, ReadError
from aistorage.syncer.opencode_api import PARTIAL_SUFFIX, OpencodeApi


# ---------------------------------------------------------------------------
# 假的子行程與假的 API
# ---------------------------------------------------------------------------


class FakeRun:
    """假的 `subprocess.run`：把預備好的位元組寫進 `stdout` 指定的檔案物件。

    重現 9.5 的真實行為：stdout 是 pipe 時只送出 64 KiB 的倍數，rc 仍然是 0。
    """

    def __init__(self, *, stdout_bytes: bytes = b"", returncode: int = 0,
                 stderr: bytes = b"", truncate_to: int | None = None) -> None:
        self.stdout_bytes = stdout_bytes
        self.returncode = returncode
        self.stderr = stderr
        self.truncate_to = truncate_to
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess:
        self.calls.append({"argv": list(argv), **kw})
        sink = kw.get("stdout")
        assert sink is not None, "stdout 必須開成檔案物件（不能是 pipe）"
        data = self.stdout_bytes
        if self.truncate_to is not None:
            data = data[: self.truncate_to]
        sink.write(data)
        return subprocess.CompletedProcess(
            args=list(argv), returncode=self.returncode, stdout=None,
            stderr=self.stderr,
        )


def _export_bytes(messages: int = 3, *, session_id: str = "ses_1",
                  filler: int = 20) -> bytes:
    """造一份合法 export 的位元組（形狀同 1.18.32 原生輸出）。"""
    doc = {
        "info": {"id": session_id, "slug": "probe", "projectID": "global",
                 "directory": "/work", "path": "work", "title": "測",
                 "version": "1.18.32", "cost": 0,
                 "tokens": {"input": 0, "output": 0, "reasoning": 0,
                            "cache": {"read": 0, "write": 0}},
                 "time": {"created": 1, "updated": 2}},
        "messages": [
            {"info": {"id": f"msg_{i}", "sessionID": session_id,
                      "role": "user", "time": {"created": i}},
             "parts": [{"id": f"prt_{i}", "sessionID": session_id,
                        "messageID": f"msg_{i}", "type": "text",
                        "text": "x" * filler}]}
            for i in range(messages)
        ],
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class FakeApi(OpencodeApi):
    """只替掉 HTTP 端點的 OpencodeApi（`message_ids`／list_sessions）。"""

    def __init__(self, ids: Sequence[str], **kw: Any) -> None:
        super().__init__(**kw)
        self.known = list(ids)
        self.message_calls: list[str] = []

    def message_ids(self, session_id: str) -> list[str]:
        self.message_calls.append(session_id)
        return list(self.known)

    def list_sessions(self):  # pragma: no cover - 這個測試不列舉
        return []


def _api(ids: Sequence[str]) -> FakeApi:
    return FakeApi(ids, base_url="http://127.0.0.1:1", directory="")


# ---------------------------------------------------------------------------
# 1. 取法：stdout 是檔案、不是 pipe、也不經過 shell
# ---------------------------------------------------------------------------


def test_export_writes_stdout_to_a_real_file_and_never_uses_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """9.5 的根因就是 stdout 是 pipe；這裡把它釘死成「一般檔案」。

    - `stdout` 必須是檔案物件（子行程看到的是檔案 fd，不是 pipe）；
    - **不能**用 `shell=True`／`sh -c`（session id 會被二次解讀）；
    - 也不能同時用 `capture_output`（Python 會直接報錯）。
    """
    data = _export_bytes()
    runner = FakeRun(stdout_bytes=data)
    monkeypatch.setattr(subprocess, "run", runner)
    api = _api(["msg_0", "msg_1", "msg_2"])

    dest = tmp_path / "raw" / "ses_1.json"
    out = api.export("ses_1", dest)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"][:2] == ["opencode", "export"] and call["argv"][2] == "ses_1"
    assert call.get("shell") in (None, False)
    assert call.get("capture_output") is None
    assert call["stdout"].name.endswith(PARTIAL_SUFFIX)
    # 交出去的就是子行程寫的位元組，一個 byte 都沒多
    assert out.read_bytes() == data
    assert hashlib.sha256(out.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
    # 驗證通過之後沒有暫存檔
    assert not list(dest.parent.glob(f"*{PARTIAL_SUFFIX}"))


def test_export_keeps_the_previous_file_when_a_new_export_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """失敗時 `dest` 必須維持原狀——不能變成半份、也不能變空。

    `skill/tools._ensure_export` 會直接拿 `dest` 算接續點，混到半份就是錯的接續點。
    """
    dest = tmp_path / "ses_1.json"
    dest.write_bytes(b'{"messages": []}\n')
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=b"boom"))
    api = _api(["msg_0"])

    with pytest.raises(ReadError):
        api.export("ses_1", dest)

    assert dest.read_bytes() == b'{"messages": []}\n'
    assert not list(tmp_path.glob(f"*{PARTIAL_SUFFIX}"))


# ---------------------------------------------------------------------------
# 2. 不完整要被擋下，而且明確是「取得不完整」
# ---------------------------------------------------------------------------


def test_truncated_export_is_rejected_as_incomplete_not_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """9.5 的實際症狀：rc=0、位元組是完整 JSON 的一半。"""
    data = _export_bytes(messages=400, filler=400)
    assert len(data) > 128 * 1024
    runner = FakeRun(stdout_bytes=data, truncate_to=64 * 1024)
    monkeypatch.setattr(subprocess, "run", runner)
    api = _api(["msg_0"])
    dest = tmp_path / "ses_1.json"

    with pytest.raises(IncompleteFetch) as e:
        api.export("ses_1", dest)

    # 明確講「取得不完整」，而且是獨立型別（不是 ReadError）
    assert "取得不完整" in str(e.value)
    assert not isinstance(e.value, ReadError)
    # 沒有半份留下來可以被當成原始紀錄上傳
    assert not dest.exists()
    assert not list(tmp_path.glob(f"*{PARTIAL_SUFFIX}"))


def test_incomplete_fetch_is_not_a_read_error():
    """型別上就要分得開：ReadError 是「讀不到」，IncompleteFetch 是「只拿到一部分」。"""
    assert not issubclass(IncompleteFetch, ReadError)
    assert issubclass(IncompleteFetch, Exception)


@pytest.mark.parametrize(
    "payload, why",
    [
        (b"", "空輸出 → ReadError（讀不到）"),
        (b"{", "截斷在最前面"),
        (b'{"info": {"id": "ses_1"}, "messages": []', "少了收尾的括號"),
        (b'{"info": {"id": "ses_1"}}', "缺 messages"),
        (b'{"info": {"id": "ses_1"}, "messages": {}}', "messages 不是清單"),
        (b'{"messages": []}', "缺 info"),
        (b'{"info": {"id": "ses_other"}, "messages": []}', "匯出的是別的 Session"),
    ],
)
def test_bad_shapes_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                payload: bytes, why: str):
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout_bytes=payload))
    api = _api(["msg_0"])
    dest = tmp_path / "ses_1.json"

    if not payload:
        # 空輸出是「讀不到」，維持原本的 ReadError 語意
        with pytest.raises(ReadError):
            api.export("ses_1", dest)
    else:
        with pytest.raises(IncompleteFetch):
            api.export("ses_1", dest)
    assert not dest.exists(), why
    assert not list(tmp_path.glob(f"*{PARTIAL_SUFFIX}")), why


def test_missing_session_is_a_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """不存在的 id → rc=1（1.7a）：是讀不到，不是取得不完整。"""
    monkeypatch.setattr(
        subprocess, "run",
        FakeRun(returncode=1, stderr=b"Error: Session not found: ses_nope"),
    )
    api = _api(["msg_0"])
    with pytest.raises(ReadError) as e:
        api.export("ses_nope", tmp_path / "x.json")
    assert "rc=1" in str(e.value)


def test_tail_message_missing_from_the_api_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """形狀對、JSON 也對，但內容不是整個 Session（最後一則在 API 上找不到）。

    這是「輸出被截斷但仍可解析」那一種的守門：原始紀錄少一段之後，
    提交流程與 checkout 都會以為它是完整的。
    """
    data = _export_bytes(messages=5)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout_bytes=data))
    # API 只看得到前 2 則：匯出宣稱到了 msg_4，API 上卻沒有
    api = _api(["msg_0", "msg_1"])

    with pytest.raises(IncompleteFetch) as e:
        api.export("ses_1", tmp_path / "ses_1.json")
    assert "msg_4" in str(e.value)
    assert "取得不完整" in str(e.value)
    assert api.message_calls == ["ses_1"]


def test_a_live_session_growing_is_not_mistaken_for_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """匯出之後 Session 又長出新訊息 → API 比匯出多，**不算**取得不完整。

    訊息數相等在活的 Session 上做不到，所以只問「匯出的最後一則還在不在」。
    """
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout_bytes=_export_bytes(messages=3)))
    api = _api(["msg_0", "msg_1", "msg_2", "msg_3", "msg_4"])   # 匯出之後又長了兩則

    out = api.export("ses_1", tmp_path / "ses_1.json")
    assert out.exists()


def test_export_still_requires_an_explicit_session_id(tmp_path: Path):
    """1.7a：不帶 id 會進互動選單（改用檔案輸出後這條一樣要成立）。"""
    api = _api(["msg_0"])
    for bad in ("", "   ", "\t"):
        with pytest.raises(ValueError):
            api.export(bad, tmp_path / "x.json")


# ---------------------------------------------------------------------------
# 3. sync_once：取得不完整 → 不上傳、明確記成 incomplete
# ---------------------------------------------------------------------------


def test_sync_once_reports_incomplete_and_uploads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """半份原始紀錄進了 Agora 就再也分不出來，所以這一輪寧可不收。"""
    from aistorage.drive.fake import FakeDrive
    from aistorage.syncer.core import Signer, sync_once
    from aistorage.syncer.state import SyncState

    class TruncatingApi:
        """匯出只給一半（9.5 的真狀：rc=0、內容被砍掉）。"""

        def list_sessions(self):
            from aistorage.syncer.opencode_api import OcSession
            return [OcSession(id="ses_1", updated_ms=1)]

        def export(self, session_id: str, dest: Path) -> Path:
            data = _export_bytes(messages=200, filler=600)
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data[: 64 * 1024])
            raise IncompleteFetch(
                f"取得不完整：opencode export 的輸出不是合法 JSON"
                f"（session={session_id}，{64 * 1024} bytes）"
            )

    class FakeReader:
        def catalog(self, session_ids):
            return {}

        def get_rejection(self, item_key):
            return None

        def manifest(self):
            return {"generation": 1, "published_at": "2026-09-27T08:00:00Z"}

    from aistorage.clock import FixedClock

    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(bytes(range(32)))
    key_path.chmod(0o600)
    state = SyncState.load(tmp_path / "state.json")

    outcome = sync_once(
        api=TruncatingApi(),
        reader=FakeReader(),
        drive=drive,
        inbox_folder_id=inbox,
        signer=Signer.from_key_file(key_path, profile="mac-opencode"),
        state=state,
        clock=FixedClock("2026-09-27T09:00:00Z"),
        workdir=tmp_path / "work",
        converter=None,
    )

    sid = "opencode:ses_1"
    assert outcome.uploaded == () and outcome.errors != ()
    errors = dict(outcome.errors)
    assert sid in errors
    assert errors[sid].startswith("incomplete: ")
    assert "取得不完整" in errors[sid]
    # 收件匣裡一個檔案都不能有
    assert drive.list_children(inbox) == []
    assert state.record(sid).error_code == "incomplete"


def test_sync_once_uploads_a_complete_large_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """對照組：完整的大匯出照常上傳（`too_large` 之外沒有新的上限擋它）。"""
    from aistorage.clock import FixedClock
    from aistorage.drive.fake import FakeDrive
    from aistorage.syncer.core import Signer, sync_once
    from aistorage.syncer.opencode_api import OcSession
    from aistorage.syncer.state import SyncState

    data = _export_bytes(messages=300, filler=800)     # > 300 KB
    assert len(data) > 300 * 1024

    class OkApi:
        def list_sessions(self):
            return [OcSession(id="ses_1", updated_ms=1)]

        def export(self, session_id: str, dest: Path) -> Path:
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return dest

    class FakeReader:
        def catalog(self, session_ids):
            return {}

        def get_rejection(self, item_key):
            return None

        def manifest(self):
            return {"generation": 1, "published_at": "2026-09-27T08:00:00Z"}

    drive = FakeDrive()
    inbox = drive.seed_folder("inbox")
    key_path = tmp_path / "signing.key"
    key_path.write_bytes(bytes(range(32)))
    key_path.chmod(0o600)
    state = SyncState.load(tmp_path / "state.json")

    outcome = sync_once(
        api=OkApi(),
        reader=FakeReader(),
        drive=drive,
        inbox_folder_id=inbox,
        signer=Signer.from_key_file(key_path, profile="mac-opencode"),
        state=state,
        clock=FixedClock("2026-09-27T09:00:00Z"),
        workdir=tmp_path / "work",
        converter=None,
    )

    assert outcome.errors == ()
    assert outcome.uploaded == ("opencode:ses_1",)
    rec = state.record("opencode:ses_1")
    assert rec.last_uploaded_sha == hashlib.sha256(data).hexdigest().lower()
    raws = [f for f in drive.list_children(inbox) if f.name.endswith(".raw")]
    assert len(raws) == 1
    assert drive.download_bytes(raws[0].id, max_bytes=1 << 22) == data

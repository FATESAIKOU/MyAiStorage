"""Task 9.2 End-to-End Acceptance Test: Consolidation (n -> 1).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.2
- `docs/design/agora-session-operations.md`：多個起點＝統合，**最長的一段放最前面**，
  超過 context 上限就明確拒絕、不產出
- ADR 0010（開頭位元組相同）、CONTEXT.md 的「統合（n→1）」

流程（全部走真實的 CLI）：
1. S2、S3 在各自的容器裡 `agora handoff` 交出末端（同步 ＋ 寫交接單 ＋ 提交）；
2. 先用 `--max-chars 1` 跑一次 `agora checkout`：**明確拒絕、目錄不被建立**；
3. 再 `agora checkout handoff:H2 handoff:H3 --task …` → 起點包 → `agora-opencode
   load` → S4；
4. 驗證 S4 的開頭帶著兩者接續點之前的內容（最長的一段在最前面），並有兩條接續
   Link。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from aistorage.agora_cli.package import read_package
from aistorage.reader import AgoraReader
from aistorage.schema import generate_ulid

from .conftest import (
    agora_session_id,
    assert_tool_called,
    export_message_ids,
    poll,
    wire_prefix,
)

def _package_dir(label: str) -> str:
    """容器內的起點包目錄，**每次呼叫都不同**（`/work` 在容器刪掉之後還在，
    而 `agora checkout` 拒絕覆蓋非空目錄——固定名稱會讓重跑撞上上一輪的產物）。"""
    return f"/work/pkg-{label}-{generate_ulid()[-8:]}"


def _open_handoff_for(reader: AgoraReader, session_id: str):
    hits = [h for h in reader.list_open_handoffs().value if h.author_session_id == session_id]
    return hits[0] if hits else None


def _handoff_content(handoff: Any) -> str:
    body = handoff.body_json or "{}"
    parsed = json.loads(body) if body.strip() else {}
    content = parsed.get("content") if isinstance(parsed, dict) else None
    return str(content or "").strip()


def _package_on_host(container, package_dir: str) -> tuple[dict, list[bytes]]:
    host = Path(str(container.work_dir)) / package_dir.removeprefix("/work/")
    return read_package(host)


@pytest.mark.e2e
def test_9_2_consolidation_n_to_1(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.2 統合（n→1）：S2、S3 交出末端，checkout 兩個起點建出 S4，開頭帶著兩者內容。"""
    # 1. S2 與 S3 各自交出末端（agora handoff 內部同步並提交）
    c2 = resident_pool("e2e-s2-worker")
    s2_id, _ = c2.prompt_with_commits(
        "後端實作已完成，包含 RESTful API 與認證模組。"
        "請用 agora_handoff 交出末端（一個 task，summary 說明後端成果），"
        "完成後回報 handoff_id。",
        run_committer,
    )
    assert_tool_called(c2, s2_id, "agora_handoff")

    c3 = resident_pool("e2e-s3-worker")
    s3_id, _ = c3.prompt_with_commits(
        "前端設計已完成，包含響應式介面與狀態管理。"
        "請用 agora_handoff 交出末端（一個 task，summary 說明前端成果），"
        "完成後回報 handoff_id。",
        run_committer,
    )
    assert_tool_called(c3, s3_id, "agora_handoff")

    s2_agora = agora_session_id(s2_id)
    s3_agora = agora_session_id(s3_id)

    # 2. 讀取介面查詢待認領交接單（確認 S2、S3 都交出了）
    h_s2 = poll(lambda: _open_handoff_for(e2e_reader, s2_agora), what="S2 的交接單出現")
    h_s3 = poll(lambda: _open_handoff_for(e2e_reader, s3_agora), what="S3 的交接單出現")
    assert h_s2.handoff_id != h_s3.handoff_id

    cont_s2 = e2e_reader.get_continuation(h_s2.handoff_id).value
    cont_s3 = e2e_reader.get_continuation(h_s3.handoff_id).value
    # handoff_id 本身已經是 `handoff:<ULID>` 的完整形式，不要再加前綴
    startpoints = [h_s2.handoff_id, h_s3.handoff_id]

    # 3. 超出 context 上限要**明確拒絕、不產出**（先跑這一條，確認它與後面的
    #    成功路徑無關：被拒時連認領都還沒送出）
    c4 = resident_pool("e2e-s4-consolidator")
    too_long_dir = _package_dir("s4-too-long")
    too_long = c4.checkout_with_commits(
        startpoints, too_long_dir, run_committer,
        task="整合兩邊的成果", extra_args=["--max-chars", "1"],
    )
    assert too_long.returncode == 2, (
        f"超過上限必須明確拒絕（exit code 2），實際 rc={too_long.returncode}\n"
        f"STDOUT: {too_long.stdout[-800:]}\nSTDERR: {too_long.stderr[-800:]}"
    )
    assert "超過上限" in too_long.stderr, (
        f"拒絕訊息要說清楚是長度上限：{too_long.stderr[-800:]}"
    )
    assert not (c4.work_dir / too_long_dir.removeprefix("/work/")).exists(), (
        "被拒就不產出起點包（目錄不該被建立）"
    )
    # 被拒的那一次沒有送出認領：兩張交接單還是沒人接
    for hid in (h_s2.handoff_id, h_s3.handoff_id):
        assert e2e_reader.get_continuation(hid).value.handoff.claimed_by_session_id is None

    # 4. 真的 checkout 兩個起點 → 起點包 → agora-opencode load → S4
    package_dir = _package_dir("s4")
    res = c4.checkout_with_commits(
        startpoints, package_dir, run_committer,
        task="把 S2 的後端與 S3 的前端整合成一份可以上線的說明",
    )
    assert res.returncode == 0, (
        f"agora checkout 失敗 (rc={res.returncode})\n"
        f"STDOUT: {res.stdout[-1500:]}\nSTDERR: {res.stderr[-1500:]}"
    )
    s4_id = c4.opencode_load(package_dir)
    assert s4_id, "agora-opencode load 必須印出新 session id"
    s4_agora = agora_session_id(s4_id)
    assert s4_agora not in (s2_agora, s3_agora)

    # 5. 起點包：兩段都在、原料與真本同一個位元組，而且**最長的一段在最前面**
    pkg, raws = _package_on_host(c4, package_dir)
    segments = pkg["segments"]
    assert len(segments) == 2, f"起點包必須有兩段，實際 {len(segments)}"
    first, second = segments[0], segments[1]
    assert first["text_chars"] >= second["text_chars"], (
        f"最長的一段必須放最前面（{first['text_chars']} vs {second['text_chars']}）"
    )
    assert {first["source_session_id"], second["source_session_id"]} == {s2_agora, s3_agora}
    for segment, raw in zip(segments, raws):
        assert hashlib.sha256(raw).hexdigest().lower() == segment["snapshot_sha256"], (
            "起點包裡的原料必須與被釘住的快照同一個位元組"
        )
    assert pkg["new_session"]["session_id"] == s4_agora
    assert set(pkg["new_session"]["claimed_handoffs"]) == {
        h_s2.handoff_id, h_s3.handoff_id
    }
    # 每一段在接續點之前都要有內容（不是空殼）
    assert all(seg["message_count"] > 0 for seg in segments), [
        s["message_count"] for s in segments
    ]
    assert cont_s2.messages and cont_s3.messages, "接續點之前必須有訊息"

    # 6. S4 的開頭：最長那一段的整段內容位元組相同地放在最前面
    s4_export = c4.export(s4_id)
    s4_wire = wire_prefix(s4_export)
    longest_wire = wire_prefix(json.loads(raws[0]), first["message_id"])
    assert s4_wire.startswith(longest_wire), (
        "S4 開頭必須先帶著最長那一段在接續點之前的內容（位元組相同）"
    )
    # 另一段也有帶進來（n→1 會把後一段的首則手工鏈在前一段的末則之後）
    second_raw = json.loads(raws[1])
    second_first_text = "\n".join(
        p.get("text") or ""
        for p in (second_raw["messages"][0].get("parts") or [])
        if p.get("type") == "text"
    )
    assert second_first_text, "第二段的第一則必須有文字"
    assert second_first_text in json.dumps(s4_export, ensure_ascii=False), (
        "S4 的開頭必須也帶著另一段接續點之前的內容"
    )
    assert len(export_message_ids(s4_export)) == first["message_count"] + 1

    # 7. 兩張交接單都被登記成指向 S4 的接續 Link
    def _both_claimed():
        rows = {
            h: e2e_reader.get_continuation(h).value.handoff
            for h in (h_s2.handoff_id, h_s3.handoff_id)
        }
        if all(r.claimed_by_session_id == s4_agora for r in rows.values()):
            return rows
        return None

    poll(_both_claimed, what="S4 接手兩張交接單")
    s4_view = e2e_reader.get_session(s4_agora).value
    to_sessions = {
        link.to_session_id
        for link in s4_view.links_out
        if link.kind == "continuation"
    }
    assert {s2_agora, s3_agora} <= to_sessions, (
        f"S4 必須有兩條接續 Link 指向 S2、S3，實際 {to_sessions}"
    )

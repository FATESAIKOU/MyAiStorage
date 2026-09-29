"""1→1「接著做」的端到端驗收（`docs/design/agora-session-operations.md` 場景 A）。

起點是 `<session>[@<訊息>]`，**不經交接單**（CONTEXT.md 的「接續不需要交接單」）：
持有者不必事先寫交接單，接手的人只要指定一個位置。commit 2bc0785 起
`agora checkout` 會送一筆接續單（`continuation:`），提交流程收進 Agora 後建出
接續 Link，所以四種關係（1→1、1→n、n→1、n↔m）都記錄得到。

流程（全部走真實的 CLI）：
1. S1 在自己的容器裡講幾句（第一則刻意呼叫一次工具，讓接續點之前有真的 tool
   part，位元組比對才連 `callID` 都驗得到）→ 明確同步 → 提交流程收進 Agora；
2. `agora checkout opencode:<S1>`（直接從 session 起點）→ 起點包；
3. `agora-opencode load` → S2（S2 在自己的容器裡，import 在那個 opencode 做）；
4. 驗 S2 送給模型的開頭與 S1 在接續點之前的內容**位元組相同**、接續點正確、
   S2 有一條接續 Link 指向 S1，而且全程沒有交接單。
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
from aistorage.syncer.continuation import last_completed_message_id

from .conftest import (
    agora_session_id,
    assert_tool_called,
    export_message_ids,
    poll,
    wire_prefix,
)

#: 容器內的起點包目錄（`/work` 就是該容器的工作目錄，宿主機讀得到同一份）
PKG_DIR = "/work/pkg-1to1"


def _message_text(message: dict) -> str:
    """把一則閱讀版訊息的所有 part 攤平成文字（比對 canary 用）。"""
    return json.dumps(message, ensure_ascii=False)


def _package_on_host(container, package_dir: str) -> tuple[dict, list[bytes]]:
    """讀回容器產出的起點包（`/work/...` → 宿主機的工作目錄）。"""
    host = Path(str(container.work_dir)) / package_dir.removeprefix("/work/")
    return read_package(host)


def _links_to(view, session_id: str, kind: str) -> list[Any]:
    return [
        link
        for link in view.links_out
        if link.kind == kind and link.to_session_id == session_id
    ]


@pytest.mark.e2e
def test_continuation_1_to_1(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 1→1 接著做：直接從 session 起點 checkout，S2 開頭位元組相同且有接續 Link。

    這一條不是 tasks.md 的某一項（9.1〜9.5 沒有它），它是設計文件場景 A 的驗收：
    四種 Session 關係裡的 1→1，**不經交接單**也能記錄接續（2bc0785）。
    """
    canary = f"CANARY-1TO1-{generate_ulid()}"

    # 1. S1 講幾句（第一則帶一次工具呼叫），明確同步＋提交
    c1 = resident_pool("e2e-1to1-s1")
    s1_id, _ = c1.prompt_with_commits(
        "請先使用 aistorage_whoami 確認你現在在哪個 Session，然後用一句話確認收到。"
        "這句是後續的識別碼：" + canary,
        run_committer,
    )
    assert_tool_called(c1, s1_id, "aistorage_whoami")
    s1_agora = agora_session_id(s1_id)
    s1_view = poll(
        lambda: e2e_reader.get_session(s1_agora).value,
        what="S1 已進 Agora",
    )
    pinned_sha = s1_view.session.raw_sha256
    # 接續點：被釘住那份快照裡「最後一則已完成、未撤銷」的訊息。用同一個函式算，
    # 不自己重寫那個判定（`syncer/continuation.py` 的模組說明）。
    pinned_reading = e2e_reader.get_reading(s1_agora, snapshot_sha256=pinned_sha).value
    cont_message_id, _at = last_completed_message_id(pinned_reading)
    assert cont_message_id, "S1 必須有可用的接續點"
    assert any(canary in _message_text(m) for m in pinned_reading["messages"]), (
        "接續點之前必須包含 canary"
    )

    # 2. 直接從 session 起點 checkout（不經交接單）→ 起點包 → S2
    c2 = resident_pool("e2e-1to1-s2")
    res = c2.checkout_with_commits(
        [s1_agora], PKG_DIR, run_committer,
        task="接著把這一則做完",
    )
    assert res.returncode == 0, (
        f"agora checkout 失敗 (rc={res.returncode})\n"
        f"STDOUT: {res.stdout[-1500:]}\nSTDERR: {res.stderr[-1500:]}"
    )
    s2_id = c2.opencode_load(PKG_DIR)
    assert s2_id, "agora-opencode load 必須印出新 session id"
    s2_agora = agora_session_id(s2_id)
    assert s2_agora != s1_agora, "S2 必須是不同的 Session"

    # 3. 起點包：直接起點沒有交接單、沒有認領，原料與被釘住的快照同一個位元組
    pkg, raws = _package_on_host(c2, PKG_DIR)
    assert len(pkg["segments"]) == 1, f"起點包必須只有一段，實際 {len(pkg['segments'])}"
    segment = pkg["segments"][0]
    assert segment["source_session_id"] == s1_agora
    assert segment["snapshot_sha256"] == pinned_sha
    assert segment["message_id"] == cont_message_id, (
        "起點包的接續點必須是被釘住快照裡最後一則已完成的訊息"
    )
    assert segment["handoff_id"] is None, "直接起點不經交接單"
    assert segment["claim_id"] is None
    assert pkg["new_session"]["claimed_handoffs"] == [], "直接起點沒有認領"
    assert pkg["new_session"]["session_id"] == s2_agora
    assert hashlib.sha256(raws[0]).hexdigest().lower() == pinned_sha

    # 4. 開頭位元組相同：以起點包裡那份原料（與真本同一個位元組）截到接續點，
    #    當成 S1 在接續點之前送給模型的內容，逐位元組比對 S2 的開頭
    s2_export = c2.export(s2_id)
    assert wire_prefix(s2_export) == wire_prefix(json.loads(raws[0]), cont_message_id), (
        "S2 送給模型的開頭必須與 S1 在接續點之前的內容位元組相同"
    )
    # 接續點那一則**含在內**（新 session 從它之後開始寫）
    s2_message_ids = export_message_ids(s2_export)
    assert s2_message_ids[-1] == cont_message_id, (
        f"S2 開頭的最後一則必須就是接續點 {cont_message_id}，實際 {s2_message_ids[-1]}"
    )

    # 5. S2 有一條接續 Link 指向 S1，接續點正確；S1 那邊也查得到
    def _link_ready():
        links = _links_to(e2e_reader.get_session(s2_agora).value, s1_agora, "continuation")
        return links[0] if len(links) == 1 else None

    link = poll(_link_ready, what="S2 的接續 Link 出現在讀取視圖")
    assert link.from_session_id == s2_agora
    assert link.snapshot_sha256 == pinned_sha, "接續 Link 必須釘在被接續的那份快照"
    assert link.message_id == cont_message_id, "接續 Link 的接續點必須正確"
    # 直接起點不帶交接單、也不帶認領
    assert link.handoff_id is None and link.claim_id is None

    s1_links_in = [
        link
        for link in e2e_reader.get_session(s1_agora).value.links_in
        if link.kind == "continuation" and link.from_session_id == s2_agora
    ]
    assert len(s1_links_in) == 1, f"S1 也要看得到這一條接續 Link，實際 {len(s1_links_in)}"
    assert s1_links_in[0].message_id == cont_message_id

    # 6. 全程沒有交接單：接續不需要交接單（CONTEXT.md）
    assert [
        h for h in e2e_reader.list_open_handoffs().value
        if h.author_session_id == s1_agora
    ] == [], "1→1 接著做不該產生任何交接單"

    # 7. S2 真的能接著往下做（開頭帶著 S1 的內容一起進了 Agora）
    c2.send(s2_id, "請只回覆 ACK-1TO1-CARRY，不要呼叫任何工具。")
    c2.sync_once([s2_id])
    run_committer()

    def _carried() -> dict | None:
        reading = e2e_reader.get_reading(s2_agora).value
        return reading if "ACK-1TO1-CARRY" in json.dumps(
            reading, ensure_ascii=False
        ) else None

    reading = poll(_carried, what="S2 接續後的內容進了 Agora")
    assert reading["messages"], "S2 的閱讀版不得為空"
    assert any(canary in _message_text(m) for m in reading["messages"]), (
        "S2 必須帶著 S1 的內容（canary）"
    )
    assert reading.get("parent_id") is None, "S2 是主 Session，不是子 Session"

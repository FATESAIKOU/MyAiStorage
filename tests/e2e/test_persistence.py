"""Task 9.5 End-to-End Acceptance Test: Persistence after Container Destruction.

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.5
- Spawns sessions and sub-sessions (tasks)
- Commits to Agora and Foundry
- Completely destroys all resident containers and wipes all local /work directories
- AgoraReader continues to query and retrieve all sessions, readings, and links
- Sub-sessions remain queryable with valid parent_id referencing mother session

修正的假綠燈（review）：
- 子 Session 的驗證 `for h in hits: assert …` 在空集合時直接通過 → 改成
  `assert len(hits) >= 1`，並從 export 確認 `task` 工具真的建立了子 Session。
- 刪除的是**容器真正的工作目錄**：fixture 設定 `AISTORAGE_WORK_ROOT`，
  刪除後斷言目錄不存在、`docker inspect` 失敗（容器真的沒了）。
- 補上 Foundry 的持久性（task 9.5 要求 Agora 與 Foundry）。
"""

from __future__ import annotations

import re
import shutil

import pytest

from aistorage.reader import AgoraReader, Query

from .conftest import (
    agora_session_id,
    assert_tool_called,
    poll,
    wait_session_sha,
)


@pytest.mark.e2e
def test_9_5_persistence_across_local_destruction(
    resident_pool, run_committer, e2e_reader: AgoraReader
):
    """驗證 9.5 持久性：刪除住民容器與本機工作資料後，Agora 內容皆完好可查。"""
    # 1. 啟動住民容器，建立母 Session 與子代理 Session (task)
    c = resident_pool("e2e-persist-root")
    s_root_id, _ = c.prompt(
        "請呼叫 task 工具啟動一個子代理處理細部模組驗證（子代理請自我介紹），"
        "母 Session 則維持整體協調並回報完成。"
    )
    assert_tool_called(c, s_root_id, "task")
    root_agora = agora_session_id(s_root_id)

    # 從 export 的 task part 找出**真的建立出來的**子 Session id（不是靠清單順序）
    task_parts = assert_tool_called(c, s_root_id, "task")
    child_ids: set[str] = set()
    for part in task_parts:
        output = str(part.get("output") or "")
        for found in re.findall(r"ses_[A-Za-z0-9]+", output):
            child_ids.add(found)
    assert child_ids, f"task 工具的輸出裡找不到子 Session id：{task_parts}"
    child_oc_id = sorted(child_ids)[0]
    child_agora = agora_session_id(child_oc_id)

    # 2. 明確同步母與子 Session，再提交至 Agora
    c.sync_once([s_root_id, child_oc_id])
    run_committer()

    # 3. 讀取介面確認母 Session 已存在真本（用我們自己建立的 id）
    view_root = wait_session_sha(e2e_reader, root_agora, None)
    assert view_root.session.session_id == root_agora
    assert view_root.session.parent_id is None

    # 查詢子 Session：必須至少一筆，且 parent_id 指向母 Session
    def _child_hits():
        hits, _ = e2e_reader.find_sessions(Query(parent_id=root_agora)).value
        return hits or None

    hits = poll(_child_hits, what="子 Session 出現在 Agora")
    assert len(hits) >= 1, "子 Session 必須查得到（task 9.5）"
    assert any(h.hit.session.parent_id == root_agora for h in hits)
    assert any(h.hit.session.session_id == child_agora for h in hits), (
        f"task 建立的子 Session {child_agora} 必須在 Agora 裡；"
        f"實際：{[h.hit.session.session_id for h in hits]}"
    )

    # 母 Session 的閱讀版與子 Session 的閱讀版都讀得到
    root_reading = e2e_reader.get_reading(root_agora).value
    assert root_reading["messages"], "母 Session 的閱讀版不得為空"
    child_reading = e2e_reader.get_reading(child_agora).value
    assert child_reading["messages"], "子 Session 的閱讀版不得為空"
    assert child_reading.get("parent_id") == root_agora, (
        "閱讀版必須記有母 Session"
    )

    # 4. 徹底銷毀容器與**它真正的工作目錄**（fixture 設定了 AISTORAGE_WORK_ROOT）
    work_dir = c.work_dir
    assert work_dir.exists(), f"容器的工作目錄必須存在（run.sh 真的用它）：{work_dir}"
    (work_dir / "destroy-marker.txt").write_text("before-destroy\n", encoding="utf-8")

    c.stop()
    assert not c.container_exists(), f"容器 {c.name} 必須真的被刪掉（docker inspect 失敗）"
    shutil.rmtree(work_dir)
    assert not work_dir.exists(), "工作目錄必須真的不存在"

    # 5. 再次使用 AgoraReader：Session、閱讀版與關聯都還在
    res_main_after = e2e_reader.get_session(root_agora)
    assert res_main_after.value.session.session_id == root_agora
    assert res_main_after.value.session.status in ("running", "stopped")

    reading_res = e2e_reader.get_reading(root_agora)
    assert "messages" in reading_res.value and reading_res.value["messages"]

    hits_after, _ = e2e_reader.find_sessions(Query(parent_id=root_agora)).value
    assert len(hits_after) >= 1, "刪除本機資料後子 Session 仍必須查得到"
    assert any(h.hit.session.session_id == child_agora for h in hits_after)


@pytest.mark.e2e
@pytest.mark.xfail(
    strict=True,
    reason="F-H1/F-H3：Foundry 尚未接進提交流程與發佈（object_file_id 沒人寫），"
    "修好後會 XPASS，請移除這個標記",
)
def test_9_5_foundry_persistence(resident_pool, run_committer, e2e_foundry_reader):
    """9.5 的 Foundry 部分：容器與本機資料刪除後，Foundry 產出仍取得到。"""
    from aistorage.schema import generate_ulid

    marker = f"PERSIST-FOUNDRY-{generate_ulid()}"
    c = resident_pool("e2e-persist-foundry")
    s_id, _ = c.prompt(
        "請產生一份報告，內容包含識別碼 " + marker + "，"
        "並使用工具 aistorage_register_artifact 登錄至 Foundry（kind=contained）。"
    )
    assert_tool_called(c, s_id, "aistorage_register_artifact")
    c.sync_once([s_id])
    run_committer()

    artifacts = poll(
        lambda: [
            f.artifact
            for f in e2e_foundry_reader.find(session_id=agora_session_id(s_id)).value
        ]
        or None,
        what="Foundry 目錄出現產出",
    )
    artifact = artifacts[0]

    # 銷毀容器與工作目錄
    work_dir = c.work_dir
    c.stop()
    shutil.rmtree(work_dir, ignore_errors=True)

    # Foundry 的目錄與本體都還在
    again = [f.artifact for f in e2e_foundry_reader.find(session_id=agora_session_id(s_id)).value]
    assert any(a.artifact_id == artifact.artifact_id for a in again)
    content = e2e_foundry_reader.get(artifact.artifact_id).value
    assert content.data and marker.encode("utf-8") in content.data

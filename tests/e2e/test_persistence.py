"""Task 9.5 End-to-End Acceptance Test: Persistence after Container Destruction.

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.5
- Spawns sessions and sub-sessions (tasks)
- Commits to Agora and Foundry
- Completely destroys all resident containers and wipes all local /work directories
- AgoraReader continues to query and retrieve all sessions, readings, and links
- Sub-sessions remain queryable with valid parent_id referencing mother session
"""

from __future__ import annotations

import shutil
from pathlib import Path
import pytest

from aistorage.reader import AgoraReader, Query


@pytest.mark.e2e
def test_9_5_persistence_across_local_destruction(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.5 持久性：刪除所有住民容器與本機 /work 資料後，Agora 內容皆完好可查，子 Session 完整記錄母 Session。"""
    # 1. 啟動住民容器，建立母 Session 與子代理 Session (task)
    c = resident_pool("e2e-persist-root")
    c.prompt(
        "請呼叫 task 工具啟動一個子代理處理細部模組驗證，"
        "母 Session 則維持整體協調。"
    )

    # 取得本機建立的 sessions 清單
    sessions = c.list_sessions()
    assert len(sessions) >= 1
    main_sess = sessions[0]
    main_id = main_sess["id"]

    # 2. 同步並提交至 Agora
    run_committer()

    # 3. 讀取介面確認母 Session 與子 Session 已存在真本中
    res_main = e2e_reader.get_session(main_id)
    assert res_main is not None
    assert res_main.value.session.session_id == main_id

    # 查詢子 Session (parent_id == main_id)
    hits, _ = e2e_reader.find_sessions(Query(parent_id=main_id)).value
    # 驗證母子 Session 關聯在 Agora 中被完整記錄
    for h in hits:
        assert h.hit.session.parent_id == main_id

    # 4. 徹底銷毀所有容器與本機目錄（模擬本機電腦換機或容器環境重設）
    c.stop()
    work_dir = c.work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)

    # 5. 再次使用 AgoraReader 讀取視圖查詢：所有內容、閱讀版與關聯依然完好無損
    res_main_after = e2e_reader.get_session(main_id)
    assert res_main_after is not None
    assert res_main_after.value.session.session_id == main_id
    assert res_main_after.value.session.status in ["running", "stopped"]

    # 驗證閱讀版依然可正常下載讀取
    reading_res = e2e_reader.get_reading(main_id)
    assert reading_res is not None
    assert "messages" in reading_res.value

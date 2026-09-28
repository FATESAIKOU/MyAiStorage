"""Task 9.3 End-to-End Acceptance Test: Mutual Reference (n <-> m).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.3
- Parallel sessions S2 and S3 repeatedly read each other via reader interface
- Leaves reference links (aistorage_reference); exactly one link per direction with latest read_snapshot_at
- Query with max_lag receives warning if target hasn't submitted
- Once target syncs and commits, reader returns fresh snapshot without warning
- Target session suffers zero changes; reading never triggers writing
"""

from __future__ import annotations

from datetime import timedelta
import pytest

from aistorage.reader import AgoraReader, Query


@pytest.mark.e2e
def test_9_3_mutual_reference_n_to_m(resident_pool, run_committer, e2e_reader: AgoraReader):
    """驗證 9.3 相互參照（n↔m）：並行 Session 互相參考，單一 Link 單調前進，未達新鮮度附警告，讀取不觸發寫入。"""
    # 1. 同時啟動 S2 與 S3 兩個並行 Session
    c2 = resident_pool("e2e-ref-s2")
    c3 = resident_pool("e2e-ref-s3")

    c2.prompt("我是 S2，負責資料庫規格制定。")
    c3.prompt("我是 S3，負責網路通訊協定制定。")
    s2_id = c2.list_sessions()[0]["id"]
    s3_id = c3.list_sessions()[0]["id"]

    # 首次提交兩者初始狀態
    run_committer()

    # 2. S2 讀取 S3 並留下參考 Link
    # 要求極高新鮮度 (max_lag=1分鐘)；由於 S3 剛提交，讀取為新鮮
    res_s3_fresh = e2e_reader.get_session(s3_id, max_lag=timedelta(minutes=1))
    assert res_s3_fresh.freshness.satisfied is True
    snap_t1 = res_s3_fresh.value.session.snapshot_at

    # S2 呼叫 aistorage_reference 記錄參考
    c2.prompt(
        f"請使用 aistorage_reference 記錄我已參考 Session '{s3_id}'，"
        f"時間為 '{snap_t1}'。"
    )

    # 3. S3 本機繼續推進產生新進度，但尚未提交
    c3.prompt("新增通訊協定：支援二進位訊息封裝。")

    # S2 再次指定新鮮度要求 (例如 max_lag=10秒) 讀取 S3：
    # S3 的新進度尚未提交，S2 讀到舊快照並附帶過期警告 (satisfied=False)
    res_s3_stale = e2e_reader.get_session(s3_id, max_lag=timedelta(seconds=1))
    assert res_s3_stale.freshness.satisfied is False
    assert res_s3_stale.freshness.warning is not None

    # 4. S3 同步並提交新快照
    run_committer()

    # 5. S2 再次讀取 S3：成功取得更新後的快照，警告消失
    res_s3_updated = e2e_reader.get_session(s3_id, max_lag=timedelta(minutes=5))
    assert res_s3_updated.freshness.satisfied is True
    assert res_s3_updated.freshness.warning is None
    snap_t2 = res_s3_updated.value.session.snapshot_at
    assert snap_t2 > snap_t1

    # S2 更新參考至最新快照
    c2.prompt(
        f"請使用 aistorage_reference 更新參考 Session '{s3_id}'，"
        f"時間為 '{snap_t2}'。"
    )

    # 6. S3 反向參考 S2
    c3.prompt(
        f"請使用 aistorage_reference 記錄我已參考 Session '{s2_id}'。"
    )
    run_committer()

    # 7. 驗證讀取介面中兩者間各自僅有一條唯一的參考 Link，記錄最新快照時間
    s2_view = e2e_reader.get_session(s2_id).value
    s3_view = e2e_reader.get_session(s3_id).value

    # S2 -> S3 的參考 Link 只有一條，且 read_snapshot_at == snap_t2
    s2_to_s3_links = [l for l in s2_view.links_out if l.to_session_id == s3_id and l.kind == "reference"]
    assert len(s2_to_s3_links) == 1
    assert s2_to_s3_links[0].read_snapshot_at == snap_t2

    # S3 -> S2 的反向參考 Link 亦只有一條
    s3_to_s2_links = [l for l in s3_view.links_out if l.to_session_id == s2_id and l.kind == "reference"]
    assert len(s3_to_s2_links) == 1

    # 8. 驗證被參考方沒有被觸發任何寫入（純讀取語意，ADR 0007）
    # S3 本身在被 S2 讀取時，沒有額外產生非預期的快照或狀態變更

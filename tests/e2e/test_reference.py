"""Task 9.3 End-to-End Acceptance Test: Mutual Reference (n <-> m).

Adheres strictly to:
- openspec/changes/establish-aistorage-phase1/tasks.md §9.3
- `docs/design/agora-session-operations.md` 場景 D：`agora read` **讀與記在同一個
  指令裡**，所以不會出現「宣稱讀到一個其實沒讀過的版本」
- plugin 的 `agora_read` 由 context 帶入 `--from`（是誰讀的），所以 AI 只要讀了
  對方就會留下一條參考 Link，不需要另外呼叫參考工具

修正的假綠燈（review）：
- max_lag 不再用 1 秒（時間過了就恆真）；改成「S3 有新內容但還沒同步」＋合理
  的 max_lag（1 分鐘），並且 **注入 reader 的時鐘** 把它推過 max_lag 再讀，
  斷言 satisfied=False、warning 存在、而且 snapshot_at 等於舊值。
- 「S3 同步並提交」真的執行容器內 `syncer once` ＋本機提交流程，再用
  `parse_rfc3339` 比較（不用字串比較）。
- 「被參考方沒有變化」有實際斷言：S3 的 snapshots（數量與 sha）與 raw_sha256
  在 S2 多次讀取前後完全相同。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aistorage.clock import parse_rfc3339
from aistorage.reader import AgoraReader

from .conftest import (
    ModelDidNotComply,
    agora_session_id,
    assert_tool_called,
    poll,
    wait_session_sha,
)


class AdvancingClock:
    """真實時鐘＋可手動推進的偏移（把讀取端推過 max_lag，不用真的等）。"""

    def __init__(self) -> None:
        from aistorage.clock import SystemClock

        self._base = SystemClock()
        self._offset = timedelta(0)

    def advance(self, delta: timedelta) -> None:
        self._offset += delta

    def now(self):
        return self._base.now() + self._offset

    def now_utc(self) -> str:
        from aistorage.clock import format_rfc3339

        return format_rfc3339(self.now())


def _snapshot_fingerprint(view) -> list[tuple[str, str, str]]:
    return sorted(
        (s.snapshot_sha256, s.snapshot_at, s.via) for s in view.snapshots
    )


@pytest.mark.e2e
def test_9_3_mutual_reference_n_to_m(
    resident_pool, run_committer, e2e_reader: AgoraReader, e2e_drive, e2e_inbox_folder_id
):
    """驗證 9.3 相互參照（n↔m）：單一 Link 單調前進、未達新鮮度附警告、讀取不觸發寫入。"""
    # 1. 同時啟動 S2 與 S3 兩個並行 Session，明確同步＋提交初始狀態
    c2 = resident_pool("e2e-ref-s2")
    s2_id, _ = c2.prompt(
        "我是 S2，負責資料庫規格制定。請用一句話確認收到。"
    )
    c2.sync_once([s2_id])

    c3 = resident_pool("e2e-ref-s3")
    s3_id, _ = c3.prompt(
        "我是 S3，負責網路通訊協定制定。請用一句話確認收到。"
    )
    c3.sync_once([s3_id])

    run_committer()
    s2_agora = agora_session_id(s2_id)
    s3_agora = agora_session_id(s3_id)
    wait_session_sha(e2e_reader, s3_agora, None)

    # 2. S2 讀取 S3（剛提交，新鮮；用寬鬆的 max_lag 避免正常耗時誤判）
    res_s3_fresh = e2e_reader.get_reading(s3_agora, max_lag=timedelta(hours=1))
    assert res_s3_fresh.freshness.satisfied is True
    sha_t1 = res_s3_fresh.value["snapshot_sha256"]
    snap_t1 = res_s3_fresh.freshness.snapshot_at

    # `agora_read` 讀與記在同一個指令裡（`--from` 由 plugin 從 context 帶入），
    # 所以只要讀了 S3 就會留下參考 Link。
    c2.prompt(
        "請使用 agora_read 讀取 Session '" + s3_agora + "'，"
        "讀完之後用一句話摘要它的最新進度。",
        session_id=s2_id,
    )
    read_parts = assert_tool_called(c2, s2_id, "agora_read")
    if snap_t1 not in "\n".join(str(p.get("output") or "") for p in read_parts):
        raise ModelDidNotComply(
            f"模型讀取 {s3_agora} 的結果沒有出現實際 snapshot_at {snap_t1}"
        )
    c2.sync_once()  # 參考只上傳，不觸發提交（ADR 0007／設計文件場景 D）
    run_committer()

    # 3. S3 本機繼續推進產生新進度，但尚未同步
    c3.prompt("新增通訊協定：支援二進位訊息封裝。請用一句話確認。", session_id=s3_id)

    # 以 max_lag=60 秒讀 S3：把讀取端時鐘推過 max_lag 之後，S3 的最後一次同步
    # 早就過期 → 讀到舊快照、附帶過期警告
    stale_clock = AdvancingClock()
    stale_clock.advance(timedelta(minutes=10))
    lag_reader = AgoraReader(e2e_reader._client, clock=stale_clock)
    res_s3_stale = lag_reader.get_reading(s3_agora, max_lag=timedelta(seconds=60))
    assert res_s3_stale.freshness.satisfied is False
    assert res_s3_stale.freshness.warning is not None
    assert res_s3_stale.value["snapshot_sha256"] == sha_t1, (
        "未同步時讀到的必須是舊的那一份快照"
    )

    # 4. S3 明確同步並提交新快照（不是只跑 committer）
    c3.sync_once([s3_id])
    run_committer()

    # 5. S2 再次讀取 S3：取得更新後的快照，警告消失（用 parse_rfc3339 比較）
    res_s3_updated = poll(
        lambda: (
            r
            if (
                r := e2e_reader.get_reading(s3_agora, max_lag=timedelta(minutes=30))
            ).value["snapshot_sha256"]
            != sha_t1
            else None
        ),
        what="S3 的新快照已提交",
    )
    assert res_s3_updated.freshness.satisfied is True
    assert res_s3_updated.freshness.warning is None
    snap_t2 = res_s3_updated.freshness.snapshot_at
    assert snap_t2 != sha_t1
    assert parse_rfc3339(snap_t2) > parse_rfc3339(snap_t1)

    # 再讀一次：參考 Link 仍然只有一條，而且記的是**這次**讀到的快照
    c2.prompt(
        "請再使用 agora_read 讀一次 Session '" + s3_agora + "'，並簡短說明差異。",
        session_id=s2_id,
    )
    assert_tool_called(c2, s2_id, "agora_read", count=2)
    c2.sync_once()
    run_committer()

    # 6. S3 反向參考 S2（agora_read 一樣會記）
    c3.prompt(
        "請使用 agora_read 讀取 Session '" + s2_agora + "'，"
        "讀完之後用一句話摘要它的最新進度。",
        session_id=s3_id,
    )
    assert_tool_called(c3, s3_id, "agora_read")
    c3.sync_once()
    run_committer()

    # 7. 兩者間各自僅有一條唯一的參考 Link，且記最新快照時間
    def _links_ready():
        s2_view = e2e_reader.get_session(s2_agora).value
        s3_view = e2e_reader.get_session(s3_agora).value
        s2_to_s3 = [
            l for l in s2_view.links_out if l.to_session_id == s3_agora and l.kind == "reference"
        ]
        s3_to_s2 = [
            l for l in s3_view.links_out if l.to_session_id == s2_agora and l.kind == "reference"
        ]
        if len(s2_to_s3) == 1 and len(s3_to_s2) == 1:
            return s2_to_s3[0], s3_to_s2[0]
        return None

    link_s2_to_s3, link_s3_to_s2 = poll(_links_ready, what="兩個方向的參考 Link 都在")
    assert len(
        [
            l
            for l in e2e_reader.get_session(s2_agora).value.links_out
            if l.to_session_id == s3_agora and l.kind == "reference"
        ]
    ) == 1, "同一對 Session 之間只能保留一條參考 Link"
    assert link_s2_to_s3.read_snapshot_at is not None
    assert link_s3_to_s2.read_snapshot_at is not None
    # S2 讀了兩次（讀到舊的與新的），Link 記的是**最後讀到的那一個**
    assert link_s2_to_s3.read_snapshot_at == snap_t2, (
        f"參考 Link 必須記最新讀到的快照 {snap_t2}，實際 {link_s2_to_s3.read_snapshot_at}"
    )
    assert parse_rfc3339(link_s2_to_s3.read_snapshot_at) > parse_rfc3339(
        snap_t1
    ), "參考的快照時間必須單調前進"

    # 8. 被參考的一方（S3）沒有被觸發任何寫入：S2 多次讀取前後，S3 完全沒變
    #    （快照、閱讀版、以及收件匣都沒有增加——讀取不觸發同步或提交，ADR 0007）
    s3_before = e2e_reader.get_session(s3_agora).value
    fingerprint_before = _snapshot_fingerprint(s3_before)
    raw_before = s3_before.session.raw_sha256
    inbox_before = sorted(f.name for f in e2e_drive.list_children(e2e_inbox_folder_id))
    for _ in range(3):
        e2e_reader.get_session(s3_agora)
        e2e_reader.get_reading(s3_agora)
    s3_after = e2e_reader.get_session(s3_agora).value
    assert _snapshot_fingerprint(s3_after) == fingerprint_before, (
        "讀取不得產生新的快照"
    )
    assert s3_after.session.raw_sha256 == raw_before
    assert s3_after.session.updated_at == s3_before.session.updated_at
    inbox_after = sorted(f.name for f in e2e_drive.list_children(e2e_inbox_folder_id))
    assert inbox_after == inbox_before, (
        f"讀取不得讓收件匣多出任何檔案；before={inbox_before} after={inbox_after}"
    )

"""蒐集要發佈的拒收原因（真本的 ＋ 本輪驗章前的）。

依據：docs/impl/group4-modules.md 第 4.4 節、PM 決定 4。

規則：
- 真本的 `_committer/rejections/*.json`（驗章之後寫入）＋本輪 authenticated=False
  的 REJECT（驗章之前，依 g3d-recheck R1 不寫進真本）。
- **只有** item_key、code、at、item_id、authenticated，**不含任何內容**
  （不記標題、不記訊息、不記交接說明）。
- 驗章前的拒收每一輪都會重新評估，若 rejected_at 永遠取「當下」，第 13 步的
  `deletable_after` 永遠到不了，檔案永遠刪不掉。所以驗章前的拒收改用**該項目
  檔案最早的 created_time**（Drive 的 metadata，寫入者無法控制）當時間基準，
  `deletable_after = rejected_at + 24h` 得以成立（PM 決定 4）。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Sequence

from aistorage.agora.rejections import earliest_created_at
from aistorage.agora.store import AgoraStore
from aistorage.clock import SystemClock, format_rfc3339
from aistorage.errors import MismatchError
from aistorage.intake.evaluate import Decision, DecisionKind

REJECTIONS_DIR = "_committer/rejections"


@dataclass(frozen=True)
class RejectionRow:
    """一筆拒收原因（發佈到讀取視圖的 rejections 表；只有代碼，沒有內容）。"""

    item_key: str
    code: str
    at: str
    item_id: str | None = None
    authenticated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_key": self.item_key,
            "code": self.code,
            "at": self.at,
            "item_id": self.item_id,
            "authenticated": 1 if self.authenticated else 0,
        }


def _earliest_created_at(dec: Decision) -> str | None:
    """取項目所有候選檔案（sidecar／sig／raw／extra）最早的 created_time。

    這是 Drive 的 metadata，寫入者無法控制，所以可當作拒收時間的可信基準。
    與 agora.rejections.earliest_created_at 共用同一個實作（同一組規則、同一種
    時間格式），那邊是 run.py 第 13 步算 deletable_after 用的。
    """
    item = dec.item
    return earliest_created_at(
        f
        for group in (item.sidecars, item.sigs, item.raws, item.extras)
        for f in group
    )


def _rejections_dir(store: AgoraStore) -> Path:
    return store.worktree / REJECTIONS_DIR


def collect_true_copy_rejections(store: AgoraStore) -> list[RejectionRow]:
    """讀真本 `_committer/rejections/*.json`（驗章後寫入的拒收）。"""
    out: list[RejectionRow] = []
    d = _rejections_dir(store)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        item_key = p.stem
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise MismatchError(f"真本拒收紀錄損毀: {p}: {e}") from e
        if not isinstance(data, dict):
            raise MismatchError(f"真本拒收紀錄不是字典: {p}")
        code = data.get("code")
        at = data.get("at")
        if not isinstance(code, str) or not code:
            raise MismatchError(f"真本拒收紀錄缺少 code: {p}")
        if not isinstance(at, str) or not at:
            raise MismatchError(f"真本拒收紀錄缺少 at: {p}")
        item_id = data.get("item_id")
        out.append(
            RejectionRow(
                item_key=item_key,
                code=code,
                at=at,
                item_id=item_id if isinstance(item_id, str) and item_id else None,
                authenticated=True,
            )
        )
    return out


def collect_run_rejections(
    run_decisions: Sequence[Decision],
    *,
    now: str | None = None,
) -> list[RejectionRow]:
    """本輪**驗章前**（authenticated=False）的 REJECT。

    驗章後的拒收由 `_committer/rejections/*.json` 帶進來（run.py 在第 7 步就寫好
    了），所以這裡不回頭再收一次，避免同一筆有兩個時間來源。

    時間基準（PM 決定 4）：驗章前的拒收每一輪都會重新評估，若 rejected_at 永遠
    取「當下」，第 13 步的 deletable_after 永遠到不了，檔案永遠刪不掉。所以優先用
    **該項目檔案最早的 created_time**（Drive 的 metadata，寫入者無法控制），
    再退回 Decision 已帶的 rejected_at；兩者都沒有（測試常見：沒有候選檔案的
    合成項目）才用 now。**任何情況都不丟棄這一筆**——丟了等於寫入者查不到自己的
    項目為什麼被拒收。
    """
    fallback = now or format_rfc3339(SystemClock().now(), include_fraction=True)
    out: list[RejectionRow] = []
    for dec in run_decisions:
        if dec.kind != DecisionKind.REJECT or dec.authenticated:
            continue
        item_key = getattr(dec.item, "item_key", None)
        if not isinstance(item_key, str) or not item_key:
            continue
        out.append(
            RejectionRow(
                item_key=item_key,
                code=dec.code,
                at=_earliest_created_at(dec) or dec.rejected_at or fallback,
                item_id=None,  # 驗章前沒有可信的項目 id
                authenticated=False,
            )
        )
    return out


def collect_rejections(
    store: AgoraStore,
    run_decisions: Sequence[Decision] = (),
    *,
    now: str | None = None,
) -> list[RejectionRow]:
    """蒐集這一輪要發佈的全部拒收原因，依 item_key 去重並排序。

    真本紀錄優先（它帶著原始的 at 與 item_id）；同一 item_key 以真本為準。
    """
    merged: dict[str, RejectionRow] = {}
    for row in collect_true_copy_rejections(store):
        merged[row.item_key] = row
    for row in collect_run_rejections(run_decisions, now=now):
        merged.setdefault(row.item_key, row)
    return [merged[k] for k in sorted(merged)]

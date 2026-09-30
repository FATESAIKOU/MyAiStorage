"""清掉整合測試在 Drive 與 pin-test repo 留下的東西（先 dry-run）。

留給「已經知道要清什麼」的維護動作用。日常跑測試不需要它——
`scripts/run_integration.py` 會自己收尾，並在摘要裡指出這一輪的殘留。

**範圍只限整合測試自己的東西**（這是刪資料的腳本，邊界要硬）：

- Drive：`TEST_FOLDER_ID` 底下名字符合 `it-<ULID>`（含 `it-erase-`）的前綴，
  以及同名的 `-inbox`／`-quarantine`／`-readview` 等附屬資料夾；
- pin-test repo：`.pin/it-*`。

**絕不碰 `e2e-*`**：e2e 環境是單例，impl3 正在用（`tests/e2e/README.md`
的「e2e 環境鎖」一節）。要清 e2e 的東西走 `scripts/e2e_setup.py --sweep-orphans`，
那支知道自己的形狀。

**只看名字還不夠，也要看年齡**（review-55edd374 M2）：另一個終端**正在跑**的
整合測試用的是同一種前綴，刪掉它的沙箱會讓那一輪以奇怪的錯誤失敗、pin 條目
也被抽走。ULID 的前 10 碼就是建立時間（48-bit 毫秒），所以只刪建立時間超過
`--min-age-hours`（預設 6 小時）的；太新的照樣列在 dry-run 清單裡，但標成
「太新、跳過」——邊界要讓人看得見。

用法（預設就是 dry-run，不帶 `--confirm` 絕不會刪）：

    uv run python scripts/cleanup_integration_leftovers.py
    uv run python scripts/cleanup_integration_leftovers.py --confirm
    uv run python scripts/cleanup_integration_leftovers.py --min-age-hours 24

刪之前一律先 `get()` 確認該資料夾的 parents 確實是 `TEST_FOLDER_ID`，
並且名字通過上面的白名單、年齡也超過門檻；對不上就跳過並說明，不猜。
秘密只以路徑引用，內容不讀不印。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _extra in (REPO_ROOT / "src", REPO_ROOT):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from run_integration import (  # 需要先補 sys.path
    Settings,
    resolve_settings,
)

from aistorage.intake.ledger import parse_ulid_timestamp_ms

#: 整合測試自己的前綴：`it-<ULID>` 或 `it-erase-<ULID>`。
#: ULID 是 26 碼 Crockford base32（少了 I L O U），大小寫不拘。
ULID_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: 前綴的附屬資料夾只有這三種（`create_prefix` 與各測試的 `create_folder` 用的），
#: 白名舉列出來——這支會刪資料，不能用寬鬆的 `-[a-z-]+` 放行任何形狀。
KNOWN_SUFFIXES = ("inbox", "quarantine", "readview")
INTEGRATION_PREFIX = re.compile(
    rf"^it-(?:erase-)?[{ULID_CHARS}]{{26}}"
    rf"(?:-(?:{'|'.join(KNOWN_SUFFIXES)}))?$",
    re.IGNORECASE,
)

#: 只刪建立時間比這麼久以前的 `it-<ULID>`（review-55edd374 M2）。
#: 6 小時夠跨過任何一場整合測試，又不會讓殘留留在測試根資料夾裡幾天。
DEFAULT_MIN_AGE_HOURS = 6.0

#: 明確不碰的名字前綴（e2e 環境是單例，別人的）。
PROTECTED_PREFIXES = ("e2e-", "syncer-", "probe-")


def is_integration_prefix(name: str) -> bool:
    """這個 Drive 子資料夾名是不是整合測試留下的？"""
    if name.startswith(PROTECTED_PREFIXES):
        return False
    return bool(INTEGRATION_PREFIX.match(name))


def strip_known_suffix(name: str) -> str:
    """剝掉 `-inbox`／`-quarantine`／`-readview` 附屬後綴，留下測試前綴名。

    分組與算年齡都靠它，所以兩邊不會對「哪個資料夾屬於哪一組」有不同解讀。
    """
    for suffix in KNOWN_SUFFIXES:
        if name.lower().endswith(f"-{suffix}"):
            return name[: -(len(suffix) + 1)]
    return name


def integration_ulid(name: str) -> str | None:
    """取出 `it-<ULID>`／`it-erase-<ULID>`／`it-<ULID>-inbox` 裡的那 26 碼 ULID。

    回傳 `None` 表示這個名字不是整合測試的前綴（附屬後綴先剝掉，所以同一組
    共用同一個 ULID）。
    """
    if not is_integration_prefix(name):
        return None
    stem = strip_known_suffix(name)
    if stem.lower().startswith("it-erase-"):
        return stem[len("it-erase-"):]
    if stem.lower().startswith("it-"):
        return stem[len("it-"):]
    return None


def age_hours(name: str, now: datetime) -> float | None:
    """這個整合測試前綴離現在幾個小時（依 ULID 前 10 碼的建立時間）。

    ULID 的前 10 碼是 48-bit 的 UTC 毫秒時間戳（`generate_ulid` 怎麼編出來的，
    這裡就怎麼解回來）。回傳 `None` 表示時間解析不出來——那個時候一律**當成
    太新**（不刪）：刪資料的腳本寧可少刪也不要刪錯。
    """
    ulid = integration_ulid(name)
    if ulid is None:
        return None
    ms = parse_ulid_timestamp_ms(ulid)
    if ms is None:
        return None
    created = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds() / 3600.0


def is_old_enough(name: str, now: datetime, min_age_hours: float) -> bool:
    """這個前綴是不是已經老到可以刪了（建立時間超過 `min_age_hours` 小時）？"""
    hours = age_hours(name, now)
    return hours is not None and hours >= min_age_hours


#: 釘選值條目的副檔名。`.maintenance` 是第 6 組抹除測試用 `AdminLock` 留下的
#: 維護旗標（`reason: "erase-it"`），repo 名同樣是 `it-<ULID>`，所以算在內——
#: 但它只在 repo 名通過 `is_integration_prefix` 之後才會被刪，`e2e-*` 的旗標
#: 因為前綴就被擋掉，碰不到。
PIN_SUFFIXES = (".pending.json", ".pending.keys", ".maintenance", ".json", ".keys")


def pin_entry_stem(file_name: str) -> str:
    """去掉副檔名，只留條目名（`it-<ULID>.pending.json` → `it-<ULID>`）。"""
    for suffix in PIN_SUFFIXES:
        if file_name.endswith(suffix):
            return file_name[: -len(suffix)]
    return file_name


def is_integration_pin_entry(file_name: str) -> bool:
    """這個 `.pin/` 底下的檔名是不是整合測試留下的？

    判斷分兩層，順序有意義：

    1. 副檔名必須是 `PIN_SUFFIXES` 認得的其中一種（`pin_entry_stem` 認不出來就
       原樣回傳，後面自然會被形狀檢查擋下）；
    2. 去掉的副檔名之後，剩下的 repo 名必須通過 `is_integration_prefix`
       （`it-<ULID>`／`it-erase-<ULID>`，且不是 `e2e-`／`syncer-`／`probe-`）。

    所以 `agora-e2e-<ULID>.json` 與 `agora-e2e-<ULID>.maintenance` 都不會被刪：
    e2e 環境是單例，別人的線正在用。
    """
    return is_integration_prefix(pin_entry_stem(file_name))


def group_prefix_folders(names: Sequence[str]) -> list[tuple[str, list[str]]]:
    """把資料夾名依「同一個測試前綴」分組。

    `it-<ULID>`、`it-<ULID>-inbox`、`it-<ULID>-quarantine` 是一組；回傳
    `(前綴名, [該組的資料夾名…])`，順序穩定（依第一個出現的位置）。
    """
    groups: dict[str, list[str]] = {}
    for name in sorted(names):
        if not is_integration_prefix(name):
            continue
        groups.setdefault(strip_known_suffix(name), []).append(name)
    return [(base, members) for base, members in sorted(groups.items())]


# ---------------------------------------------------------------------------
# 列清單
# ---------------------------------------------------------------------------


def drive_candidates(settings: Settings) -> list[tuple[str, str, str]]:
    """列出 Drive 上該清的資料夾：`(前綴名, 資料夾名, file id)`。"""
    from run_integration import read_test_folder_id

    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient

    root_id = read_test_folder_id(settings.ids_env)
    drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
    children = {c.name: c.id for c in drive.list_children(root_id)}
    out: list[tuple[str, str, str]] = []
    for base, members in group_prefix_folders(list(children)):
        for member in members:
            out.append((base, member, children[member]))
    return out


def pin_candidates(settings: Settings, workdir: Path) -> list[str]:
    """列出 pin repo 的 `.pin/` 底下該清的條目檔名（含副檔名，去重）。

    `pin_entry_names` 回的是 `.pin/` 底下的**檔名**（含 `.json`／`.keys`
    副檔名），一個條目會有兩個檔；回傳去重後的清單並保留副檔名，
    刪的時候才知道要 `git rm` 哪些路徑。
    """
    from run_integration import pin_entry_names

    names = pin_entry_names(settings, workdir)
    return sorted({n for n in names if is_integration_pin_entry(n)})


def plan(
    settings: Settings,
    workdir: Path,
    *,
    now: datetime | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> dict[str, object]:
    """跑一次完整的 dry-run：會刪什麼都先算出來，不動任何東西。

    白名單過關的再按**年齡**分兩堆（review-55edd374 M2）：

    - `folders`／`pin_files`：老到可以刪的（`--confirm` 只碰這兩堆）；
    - `young_folders`／`young_pin_files`：太新的，標成「太新、跳過」並連同
      幾歲一起列出來——很可能是另一個終端**正在跑**的整合測試，刪了會害到它。

    `skipped` 是連名字都不符合的（e2e／syncer／probe 之類），本來就不在範圍內。
    """
    at = now or datetime.now(timezone.utc)
    all_folders = drive_candidates(settings)
    all_pin = pin_candidates(settings, workdir)
    skipped = sorted(
        n for n in _all_drive_names(settings) if not is_integration_prefix(n)
    )

    old_folders: list[tuple[str, str, str]] = []
    young_folders: list[tuple[str, str, float | None]] = []
    for entry in all_folders:
        name = entry[1]
        hours = age_hours(name, at)
        if hours is not None and hours >= min_age_hours:
            old_folders.append(entry)
        else:
            young_folders.append((entry[0], name, hours))

    old_pin: list[str] = []
    young_pin: list[tuple[str, float | None]] = []
    for name in all_pin:
        hours = age_hours(pin_entry_stem(name), at)
        if hours is not None and hours >= min_age_hours:
            old_pin.append(name)
        else:
            young_pin.append((name, hours))

    return {
        "folders": old_folders,
        "pin_files": old_pin,
        "young_folders": young_folders,
        "young_pin_files": young_pin,
        "skipped": skipped,
        "groups": group_prefix_folders([f[1] for f in old_folders]),
        "min_age_hours": min_age_hours,
    }


def _all_drive_names(settings: Settings) -> list[str]:
    from run_integration import read_test_folder_id

    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient

    root_id = read_test_folder_id(settings.ids_env)
    drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
    return [c.name for c in drive.list_children(root_id)]


def confirm_deletions(
    settings: Settings,
    folders: Sequence[tuple[str, str, str]],
    *,
    now: datetime | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> dict:
    """真的刪：每個資料夾刪之前重新 `get()` 確認 parents 與名字都對得上。

    對不上就跳過並回報——快照是上一個行程拍的，中間別人可能動過。
    **年齡也再確認一次**（review-55edd374 M2）：刪之前那一步不管篩過沒有，
    這裡都自己算，所以就算呼叫端不小心把太新的清單傳進來也不會刪到別人
    正在跑的整合測試。
    """
    from run_integration import read_test_folder_id

    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient
    from tests.integration._harness import destroy_tree

    at = now or datetime.now(timezone.utc)
    root_id = read_test_folder_id(settings.ids_env)
    drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
    removed: list[str] = []
    skipped: list[str] = []
    for base, name, folder_id in folders:
        if not is_integration_prefix(name):
            skipped.append(f"{name}（名字不符合整合測試前綴）")
            continue
        if not is_old_enough(name, at, min_age_hours):
            skipped.append(f"{name}（太新、可能正在跑，跳過）")
            continue
        # 下面三個 except 都是刻意的：清理一個前綴不該讓整場清理中斷，
        # 對不上的就跳過並說明（刪資料的腳本寧可少刪也不要刪錯）。
        try:
            meta = drive.get(folder_id)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{name}（讀不到 metadata：{type(e).__name__}）")
            continue
        if root_id not in meta.parents:
            skipped.append(f"{name}（parents {meta.parents} 不含測試根資料夾）")
            continue
        if meta.name != name:
            skipped.append(f"{name}（Drive 上現在叫 {meta.name}，名字對不上）")
            continue
        try:
            destroy_tree(drive, folder_id, root_id)
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{name}（刪除失敗：{type(e).__name__}: {e}）")
            continue
        removed.append(name)
    return {"removed": removed, "skipped": skipped}


def confirm_pin_deletions(
    settings: Settings,
    file_names: Sequence[str],
    *,
    now: datetime | None = None,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
) -> dict:
    """真的刪 pin repo 的條目：一個 commit 刪一批，再 push。

    用 `GitPinStore` 自己的環境變數（帶 deploy key 的 `GIT_SSH_COMMAND`），
    push 不掉就明確回報失敗，不假裝成功。**push 掉之前，遠端一個條目都沒少**，
    所以失敗時回傳的 `removed` 一律是空的（`staged` 只是本機那個用完就丟的
    暫存 clone 裡 `git rm` 過的檔名）——訊息要照這個事實講。

    年齡同樣在刪之前自己算一次（review-55edd374 M2）。
    """
    from aistorage.integrity.pin import GitPinStore

    at = now or datetime.now(timezone.utc)
    # 暫存目錄只是這次清理用的 clone，結束就留著給系統清（呼叫端是短命 CLI）
    workdir = Path(tempfile.mkdtemp(prefix="pin-cleanup-"))
    store = GitPinStore(
        repo_url=settings.pin_repo_url,
        workdir=workdir / "pin",
        key_path=settings.pin_key,
        known_hosts_path=settings.known_hosts,
    )
    import subprocess

    env = store._env
    store._ensure_cloned_and_updated()
    removed: list[str] = []
    for name in file_names:
        if not is_integration_pin_entry(name):
            continue
        if not is_old_enough(pin_entry_stem(name), at, min_age_hours):
            continue
        rel = f".pin/{name}"
        if not (store.workdir / rel).is_file():
            continue
        proc = subprocess.run(
            ["git", "-C", str(store.workdir), "rm", "-q", rel],
            capture_output=True, text=True, check=False, env=env,
        )
        if proc.returncode == 0:
            removed.append(name)
    if not removed:
        return {"removed": [], "error": None, "staged": []}
    commit = subprocess.run(
        ["git", "-C", str(store.workdir), "commit", "-q", "-m",
         f"pin: drop {len(removed)} integration leftovers"],
        capture_output=True, text=True, check=False, env=env,
    )
    if commit.returncode != 0:
        return {
            "removed": [],
            "error": commit.stderr.strip()[:300],
            "staged": removed,
        }
    push = subprocess.run(
        ["git", "-C", str(store.workdir), "push", "-q", "origin", "main"],
        capture_output=True, text=True, check=False, env=env,
    )
    if push.returncode != 0:
        return {
            "removed": [],
            "error": push.stderr.strip()[:300],
            "staged": removed,
        }
    return {"removed": removed, "error": None, "staged": []}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _age_text(hours: float | None) -> str:
    """年齡的顯示文字（解析不出來就說看不出來，不假裝知道）。"""
    if hours is None:
        return "看不出建立時間"
    if hours < 1:
        return f"{hours * 60:.0f} 分鐘前建立"
    if hours < 48:
        return f"{hours:.1f} 小時前建立"
    return f"{hours / 24:.1f} 天前建立"


def format_plan(plan_result: dict, settings: Settings) -> str:
    folders = plan_result["folders"]
    pin_files = plan_result["pin_files"]
    groups = plan_result["groups"]
    skipped = plan_result["skipped"]
    young_folders = plan_result["young_folders"]
    young_pin = plan_result["young_pin_files"]
    min_age = plan_result["min_age_hours"]
    lines = [
        "=== dry-run：以下是「會刪」的東西（沒有 --confirm 所以不動）===",
        f"Drive 測試根資料夾：{settings.ids_env} 裡的 TEST_FOLDER_ID",
        f"pin repo：{settings.pin_repo_url}",
        f"年齡門檻：建立超過 {min_age:g} 小時才刪",
        "",
        f"Drive：{len(folders)} 個資料夾，{len(groups)} 組前綴",
    ]
    for base, members in groups:
        lines.append(f"  {base}/")
        for member in members:
            lines.append(f"    - {member}")
    lines += ["", f"pin repo：{len(pin_files)} 個條目檔"]
    lines += [f"    - {n}" for n in pin_files[:40]]
    if len(pin_files) > 40:
        lines.append(f"    …另外還有 {len(pin_files) - 40} 個")

    # 太新的照樣列出來（帶年齡），讓人知道為什麼它沒被算進上面那堆
    # （review-55edd374 M2）：多半是另一個終端正在跑的整合測試。
    lines += ["", (
        f"太新、跳過（不滿 {min_age:g} 小時）："
        f"{len(young_folders)} 個資料夾、{len(young_pin)} 個條目檔"
    )]
    for base, name, hours in young_folders:
        lines.append(f"    - {name}（{_age_text(hours)}，{base}）")
    for name, hours in young_pin:
        lines.append(f"    - {name}（{_age_text(hours)}）")

    lines += ["", f"不碰的（名字不符合 it-<ULID>）：{len(skipped)} 個"]
    lines += [f"    - {n}" for n in skipped]
    lines += [
        "",
        "e2e-* 一律不碰（e2e 環境是單例，另有 scripts/e2e_setup.py --sweep-orphans）。",
        "太新的要等過了門檻再跑一次（門檻用 --min-age-hours 調）。",
        "真的要刪就加 --confirm。",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanup-integration-leftovers",
        description="清掉整合測試留在 Drive 與 pin-test repo 的 it-* 殘留（預設 dry-run）",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="真的刪（沒有這個旗標就只列清單）",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=DEFAULT_MIN_AGE_HOURS,
        help=(
            "只刪建立時間超過這麼多小時的 it-<ULID>"
            f"（預設 {DEFAULT_MIN_AGE_HOURS:g}；太小會踩到另一個終端正在跑的整合測試）"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.min_age_hours < 0:
        print("--min-age-hours 不能是負的", file=sys.stderr)
        return 2
    settings = resolve_settings()
    workdir = Path(tempfile.mkdtemp(prefix="cleanup-plan-"))
    result = plan(settings, workdir, min_age_hours=args.min_age_hours)
    print(format_plan(result, settings))

    if not args.confirm:
        return 0

    folders = result["folders"]
    pin_files = result["pin_files"]
    if not folders and not pin_files:
        print("\n沒有東西要清（太新的都留著，等過了門檻再跑）。")
        return 0

    print("\n=== --confirm：開始刪 ===", flush=True)
    drive_result = confirm_deletions(
        settings, folders, min_age_hours=args.min_age_hours)
    pin_result = confirm_pin_deletions(
        settings, pin_files, min_age_hours=args.min_age_hours)

    print(f"Drive：刪掉 {len(drive_result['removed'])} 個資料夾")
    for name in drive_result["removed"]:
        print(f"    - {name}")
    for note in drive_result["skipped"]:
        print(f"    跳過 {note}")

    if pin_result["removed"]:
        print(f"pin repo：刪掉 {len(pin_result['removed'])} 個條目")
    if pin_result.get("error"):
        # 措辭要照事實講（review-55edd374 M2／L）：commit 與 push 都在一個用完
        # 就丟的暫存 clone 裡，遠端的條目一個都還在。舊的說法「已 stage 但沒推
        # 上去」會讓人以為遠端已經少東西了。
        print("pin repo：失敗 —— 沒有任何東西被刪掉"
              f"（{len(pin_result.get('staged') or [])} 個條目只在本機的暫存 clone 裡刪掉，"
              "遠端原封不動）")
        print(f"    原因：{pin_result['error']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

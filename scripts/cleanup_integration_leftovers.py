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

用法（預設就是 dry-run，不帶 `--confirm` 絕不會刪）：

    uv run python scripts/cleanup_integration_leftovers.py
    uv run python scripts/cleanup_integration_leftovers.py --confirm

刪之前一律先 `get()` 確認該資料夾的 parents 確實是 `TEST_FOLDER_ID`，
並且名字通過上面的白名單；對不上就跳過並說明，不猜。
秘密只以路徑引用，內容不讀不印。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from run_integration import (  # 需要先補 sys.path
    Settings,
    resolve_settings,
)

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
#: pin repo 的條目檔名：`.pin/it-<ULID>.json` 之類。
INTEGRATION_PIN_ENTRY = re.compile(rf"^it-[{ULID_CHARS}]{{26}}(?:[.-].*)?$", re.IGNORECASE)

#: 明確不碰的名字前綴（e2e 環境是單例，別人的）。
PROTECTED_PREFIXES = ("e2e-", "syncer-", "probe-")


def is_integration_prefix(name: str) -> bool:
    """這個 Drive 子資料夾名是不是整合測試留下的？"""
    if name.startswith(PROTECTED_PREFIXES):
        return False
    return bool(INTEGRATION_PREFIX.match(name))


#: 釘選值條目的副檔名（含待定那兩個）。
PIN_SUFFIXES = (".pending.json", ".pending.keys", ".json", ".keys")


def pin_entry_stem(file_name: str) -> str:
    """去掉副檔名，只留條目名（`it-<ULID>.pending.json` → `it-<ULID>`）。"""
    for suffix in PIN_SUFFIXES:
        if file_name.endswith(suffix):
            return file_name[: -len(suffix)]
    return file_name


def is_integration_pin_entry(file_name: str) -> bool:
    """這個 `.pin/` 底下的檔名是不是整合測試的釘選值條目？"""
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
        base = name
        for suffix in KNOWN_SUFFIXES:
            if base.lower().endswith(f"-{suffix}"):
                base = base[: -(len(suffix) + 1)]
                break
        groups.setdefault(base, []).append(name)
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


def plan(settings: Settings, workdir: Path) -> dict[str, object]:
    """跑一次完整的 dry-run：會刪什麼都先算出來，不動任何東西。"""
    folders = drive_candidates(settings)
    pin_files = pin_candidates(settings, workdir)
    skipped = sorted(
        n for n in _all_drive_names(settings) if not is_integration_prefix(n)
    )
    return {
        "folders": folders,
        "pin_files": pin_files,
        "skipped": skipped,
        "groups": group_prefix_folders([f[1] for f in folders]),
    }


def _all_drive_names(settings: Settings) -> list[str]:
    from run_integration import read_test_folder_id

    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient

    root_id = read_test_folder_id(settings.ids_env)
    drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
    return [c.name for c in drive.list_children(root_id)]


def confirm_deletions(settings: Settings, folders: Sequence[tuple[str, str, str]]) -> dict:
    """真的刪：每個資料夾刪之前重新 `get()` 確認 parents 與名字都對得上。

    對不上就跳過並回報——快照是上一個行程拍 的，中間別人可能動過。
    """
    from run_integration import read_test_folder_id

    from aistorage.drive.auth import RcloneConfToken
    from aistorage.drive.http import HttpDriveClient
    from tests.integration._harness import destroy_tree

    root_id = read_test_folder_id(settings.ids_env)
    drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
    removed: list[str] = []
    skipped: list[str] = []
    for base, name, folder_id in folders:
        if not is_integration_prefix(name):
            skipped.append(f"{name}（名字不符合整合測試前綴）")
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


def confirm_pin_deletions(settings: Settings, file_names: Sequence[str]) -> dict:
    """真的刪 pin repo 的條目：一個 commit 刪一批，再 push。

    用 `GitPinStore` 自己的環境變數（帶 deploy key 的 `GIT_SSH_COMMAND`），
    push 不掉就明確回報失敗，不假裝成功。
    """
    from aistorage.integrity.pin import GitPinStore

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
        return {"removed": [], "error": None}
    commit = subprocess.run(
        ["git", "-C", str(store.workdir), "commit", "-q", "-m",
         f"pin: drop {len(removed)} integration leftovers"],
        capture_output=True, text=True, check=False, env=env,
    )
    if commit.returncode != 0:
        return {"removed": [], "error": commit.stderr.strip()[:300]}
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
    return {"removed": removed, "error": None}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def format_plan(plan_result: dict, settings: Settings) -> str:
    folders = plan_result["folders"]
    pin_files = plan_result["pin_files"]
    groups = plan_result["groups"]
    skipped = plan_result["skipped"]
    lines = [
        "=== dry-run：以下是「會刪」的東西（沒有 --confirm 所以不動）===",
        f"Drive 測試根資料夾：{settings.ids_env} 裡的 TEST_FOLDER_ID",
        f"pin repo：{settings.pin_repo_url}",
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
    lines += ["", f"不碰的（名字不符合 it-<ULID>）：{len(skipped)} 個"]
    lines += [f"    - {n}" for n in skipped]
    lines += [
        "",
        "e2e-* 一律不碰（e2e 環境是單例，另有 scripts/e2e_setup.py --sweep-orphans）。",
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = resolve_settings()
    workdir = Path(tempfile.mkdtemp(prefix="cleanup-plan-"))
    result = plan(settings, workdir)
    print(format_plan(result, settings))

    if not args.confirm:
        return 0

    folders = result["folders"]
    pin_files = result["pin_files"]
    if not folders and not pin_files:
        print("\n沒有東西要清。")
        return 0

    print("\n=== --confirm：開始刪 ===", flush=True)
    drive_result = confirm_deletions(settings, folders)
    pin_result = confirm_pin_deletions(settings, pin_files)

    print(f"Drive：刪掉 {len(drive_result['removed'])} 個資料夾")
    for name in drive_result["removed"]:
        print(f"    - {name}")
    for note in drive_result["skipped"]:
        print(f"    跳過 {note}")

    if pin_result["removed"]:
        print(f"pin repo：刪掉 {len(pin_result['removed'])} 個條目")
    if pin_result.get("error"):
        print(f"pin repo：失敗 —— {pin_result['error']}")
        print(f"    （已 stage 但沒推上去：{pin_result.get('staged')}）")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Foundry／Agora 真本 repo 建立（tasks 7.1）。

依據：
- docs/impl/group5-7-modules.md 第 6.1 節（`python -m aistorage.admin create-repo foundry`）
- 技術驗證 1.2 的 annex 建 repo 步驟（`spike/scripts/annex_repo_init.sh` 做法）
- Foundry 的 annex.largefiles 是 `include=objects/*/*`（review F-H2），
  catalog 的 JSON 留在 git；Agora 是 `include=sessions/*/*/raw`。

只做 7.1 的範圍：測試資料夾之下建前綴資料夾＋隔離資料夾、建 git-annex
repo 並推到 Drive（產生第一個 GITMANIFEST／GITBUNDLE）。不做 pin（走
`committer init-pin`）與讀取視圖（走 `admin init-readview`），那兩步各有
自己的指令與信任規則。

安全規則（與 g3-common 相同）：
- 憑證只以路徑引用（`rclone_conf` 只傳路徑給環境變數，不讀內容）；
- git／git-annex 只在暫存目錄執行（`safety.assert_safe_workdir` 會擋）；
- 只在測試資料夾底下建東西；前綴已存在就拒絕覆寫（冪等：重跑同名即停）。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence

from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient

#: Foundry 的 annex 收檔規則（review F-H2 實測：objects 進 annex、catalog 留 git）。
FOUNDRY_LARGEFILES = "include=objects/*/*"
#: Agora 的 annex 收檔規則（group5-7 第 6.1 節）。
AGORA_LARGEFILES = "include=sessions/*/*/raw"

FOUNDRY_SCHEMA_VERSION = "foundry/v1"
AGORA_SCHEMA_VERSION = "agora/v1"

#: 前綴名稱規則：小寫英文、數字與連字號，3〜64 字元（Drive 名稱路徑用）。
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def check_prefix_name(name: str) -> str:
    """驗證前綴名稱；不合法就 raise（rcloneprefix 只能是名稱路徑，不能是 id）。"""
    if not isinstance(name, str) or not _PREFIX_RE.match(name):
        raise ValueError(
            f"不合法的前綴名稱 {name!r}（要 3〜64 字元的小寫英文／數字／連字號）"
        )
    return name


def largefiles_for(element: str) -> str:
    """依要素取 annex.largefiles 規則；未知要素就 raise（不猜）。"""
    if element == "foundry":
        return FOUNDRY_LARGEFILES
    if element == "agora":
        return AGORA_LARGEFILES
    raise ValueError(f"未知的要素 {element!r}（只要 'agora' 或 'foundry'）")


def schema_version_for(element: str) -> str:
    """依要素取 _committer/schema_version 內容。"""
    if element == "foundry":
        return FOUNDRY_SCHEMA_VERSION
    if element == "agora":
        return AGORA_SCHEMA_VERSION
    raise ValueError(f"未知的要素 {element!r}（只要 'agora' 或 'foundry'）")


@dataclass(frozen=True)
class RepoPlan:
    """建 repo 的計畫（dry-run 印這個，不寫入任何東西）。"""

    element: str
    prefix_name: str
    quarantine_name: str
    rcloneprefix: str
    largefiles: str
    schema_version: str
    max_git_bundles: int
    already_exists: bool
    existing_names: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "prefix_name": self.prefix_name,
            "quarantine_name": self.quarantine_name,
            "rcloneprefix": self.rcloneprefix,
            "largefiles": self.largefiles,
            "schema_version": self.schema_version,
            "max_git_bundles": self.max_git_bundles,
            "already_exists": self.already_exists,
            "existing_names": list(self.existing_names),
        }


def plan_create_repo(
    drive: DriveClient,
    test_root_id: str,
    prefix_name: str,
    *,
    element: str = "foundry",
    max_git_bundles: int = 10,
) -> RepoPlan:
    """看測試資料夾底下有沒有同名前綴，回傳計畫（唯讀，不寫入）。

    已存在 → `already_exists=True`，呼叫端必須停下來（不覆寫、不刪除重建；
    要重來就換個前綴名，舊的走正常清理依 file id 刪除）。
    """
    check_prefix_name(prefix_name)
    largefiles = largefiles_for(element)
    quarantine_name = f"{prefix_name}-quarantine"
    children = drive.list_children(test_root_id)
    names = tuple(sorted(c.name for c in children if c.is_folder))
    clash = prefix_name in names or quarantine_name in names
    return RepoPlan(
        element=element,
        prefix_name=prefix_name,
        quarantine_name=quarantine_name,
        rcloneprefix=prefix_name,
        largefiles=largefiles,
        schema_version=schema_version_for(element),
        max_git_bundles=int(max_git_bundles),
        already_exists=clash,
        existing_names=names,
    )


def _run(args: Sequence[str], cwd: Path, env: dict[str, str]) -> str:
    proc = subprocess.run(
        list(args), cwd=str(cwd), env=env,
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"指令失敗 (rc={proc.returncode}): {' '.join(args[:4])}…\n"
            f"{proc.stderr.strip()[-500:]}"
        )
    return proc.stdout


def _git_env(rclone_conf: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["RCLONE_CONFIG"] = str(rclone_conf)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


@dataclass(frozen=True)
class CreatedRepo:
    """實際建好的 repo（只記非秘密的 id 與名稱，不記任何憑證）。"""

    element: str
    prefix_name: str
    prefix_folder_id: str
    quarantine_folder_id: str
    repo_uuid: str
    repo_url: str
    main_sha: str
    annex_sha: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "element": self.element,
            "prefix_name": self.prefix_name,
            "prefix_folder_id": self.prefix_folder_id,
            "quarantine_folder_id": self.quarantine_folder_id,
            "repo_uuid": self.repo_uuid,
            "repo_url": self.repo_url,
            "main_sha": self.main_sha,
            "annex_sha": self.annex_sha,
        }


def run_create_repo(
    drive: DriveClient,
    test_root_id: str,
    prefix_name: str,
    *,
    element: str = "foundry",
    rclone_conf: Path | str,
    rclone_remote: str = "gdrive",
    max_git_bundles: int = 10,
    work_parent: Path | str | None = None,
) -> CreatedRepo:
    """真的建 repo（前綴資料夾＋隔離資料夾＋git-annex repo 推上 Drive）。

    呼叫前請先 `plan_create_repo` 確認 `already_exists` 為 False。
    git 只在暫存目錄執行；rclone 憑證只以路徑傳遞。
    """
    from aistorage.safety import assert_safe_workdir

    check_prefix_name(prefix_name)
    largefiles = largefiles_for(element)
    schema_version = schema_version_for(element)
    conf_path = Path(rclone_conf)
    if not conf_path.is_file():
        raise FileNotFoundError(f"找不到 rclone 設定檔（只以路徑引用）: {conf_path}")

    plan = plan_create_repo(
        drive, test_root_id, prefix_name,
        element=element, max_git_bundles=max_git_bundles,
    )
    if plan.already_exists:
        raise RuntimeError(
            f"前綴 {prefix_name!r}（或它的隔離資料夾）已存在，不覆寫；"
            "換個前綴名再跑一次"
        )

    # 1. Drive 前綴資料夾（與 Agora 分開）＋隔離資料夾（同一層，保持佈局是平的）
    prefix = drive.create(
        test_root_id, prefix_name, b"", mime_type=GOOGLE_FOLDER_MIME)
    if not prefix.is_folder:
        raise RuntimeError(f"建立前綴資料夾失敗：{prefix_name!r} 不是資料夾")
    quarantine = drive.create(
        test_root_id, f"{prefix_name}-quarantine", b"",
        mime_type=GOOGLE_FOLDER_MIME)
    if not quarantine.is_folder:
        raise RuntimeError("建立隔離資料夾失敗：不是資料夾")

    # 2. 暫存目錄裡建 git-annex repo 並推上 Drive
    import tempfile

    parent = Path(work_parent) if work_parent is not None else Path(
        tempfile.mkdtemp(prefix=f"aistorage_create_{element}_"))
    parent.mkdir(parents=True, exist_ok=True)
    workdir = parent / "seed"
    workdir.mkdir(parents=True, exist_ok=True)
    assert_safe_workdir(workdir, purpose=f"{element} 建 repo 工作目錄")

    env = _git_env(conf_path)
    _run(["git", "init", "-q", "-b", "main", "."], workdir, env)
    _run(["git", "annex", "init", f"aistorage-{element}"], workdir, env)
    _run(
        ["git", "annex", "initremote", "drive",
         "type=rclone", "encryption=none",
         f"rcloneremotename={rclone_remote}",
         f"rcloneprefix={prefix_name}",
         "autoenable=true", "--with-url"],
        workdir, env,
    )
    _run(["git", "annex", "config", "--set", "annex.largefiles", largefiles],
         workdir, env)
    _run(["git", "config", "annex.max-git-bundles", str(max_git_bundles)],
         workdir, env)
    _run(["git", "config", "user.email", f"{element}@aistorage.local"],
         workdir, env)
    _run(["git", "config", "user.name", f"AiStorage {element.capitalize()}"],
         workdir, env)

    (workdir / "README.md").write_text(
        f"AiStorage {element.capitalize()}\n", encoding="utf-8")
    (workdir / "_committer").mkdir(exist_ok=True)
    (workdir / "_committer" / "schema_version").write_text(
        f"{schema_version}\n", encoding="utf-8")
    add_targets = ["README.md", "_committer"]
    if element == "foundry":
        (workdir / "catalog").mkdir(exist_ok=True)
        (workdir / "objects").mkdir(exist_ok=True)
        (workdir / "catalog" / ".gitkeep").touch()
        (workdir / "objects" / ".gitkeep").touch()
        add_targets += ["catalog", "objects"]
    _run(["git", "add", *add_targets], workdir, env)
    _run(["git", "commit", "-qm", f"init: {element} seed"], workdir, env)
    _run(["git", "annex", "copy", "--to", "drive"], workdir, env)
    _run(["git", "push", "drive", "main", "git-annex"], workdir, env)

    info = _run(["git", "annex", "info", "drive", "--fast"], workdir, env)
    uuid = ""
    for line in info.splitlines():
        if line.startswith("uuid:"):
            uuid = line.split(":", 1)[1].strip()
    if not uuid:
        raise RuntimeError("取不到 git-annex remote 的 uuid")
    url = (f"annex::{uuid}?encryption=none&type=rclone"
           f"&rcloneremotename={rclone_remote}&rcloneprefix={prefix_name}")
    main_sha = _run(["git", "rev-parse", "refs/heads/main"], workdir, env).strip()
    annex_sha = _run(
        ["git", "rev-parse", "refs/heads/git-annex"], workdir, env).strip()
    return CreatedRepo(
        element=element,
        prefix_name=prefix_name,
        prefix_folder_id=prefix.id,
        quarantine_folder_id=quarantine.id,
        repo_uuid=uuid,
        repo_url=url,
        main_sha=main_sha,
        annex_sha=annex_sha,
    )

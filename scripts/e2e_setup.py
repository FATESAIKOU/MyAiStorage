"""第 9 組端到端測試環境設定腳本 (scripts/e2e_setup.py)。

用途：
- 在 TEST_FOLDER_ID 底下建立唯一之 `e2e-<ULID>/` 前綴；
- 建立 Agora 與 Foundry 之 git-annex repo（annex.largefiles 依 2.6 include=*.json、max-git-bundles=10）；
- 在 MyAiStorage-pin-test 上執行 init-pin（使用 ~/.config/aistorage/pin-test.key）；
- 建立讀取視圖資料夾與空的 manifest（generation 0，用 files.create 取得固定 id），並分享給 SA reader；
- 產生測試簽章金鑰（~/.config/aistorage/test-keys/）並產出 config/identity.e2e.json（只有公開金鑰）；
- 使用 worker client（~/.config/aistorage/rclone-worker.conf）建立收件匣資料夾；
- 產出 config/committer.e2e.json、config/committer.foundry.e2e.json 與 resident 用的 reader.e2e.json（只有非秘密 id）；
- 提供 --teardown 依 file id 遞迴永久刪除整個前綴與清理本機設定；
- 提供冪等重跑支援（重複執行檢查現有資源狀態，亦可指定 --recreate 強制重建）。

規則：
- 所有秘密只以路徑引用，絕不輸出任何機敏內文；
- git / git-annex 一律在暫存目錄執行，絕不在 repo 目錄執行 git-annex。
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

# 確保可自 src 匯入 aistorage 模組
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aistorage.annex.git import SubprocessAnnexGit
from aistorage.clock import SystemClock, format_rfc3339
from aistorage.committer.config import CommitterConfig
from aistorage.committer.publish import NullPublisher
from aistorage.committer.run import Deps, init_pin_cli
from aistorage.converters import CONVERTERS
from aistorage.drive.auth import RcloneConfToken
from aistorage.drive.http import DRIVE_API_BASE, HttpDriveClient
from aistorage.drive.model import GOOGLE_FOLDER_MIME, DriveClient
from aistorage.errors import NotFound
from aistorage.identity import load_registry
from aistorage.integrity.pin import GitPinStore
from aistorage.readview.model import initial_manifest, serialize_manifest
from aistorage.schema import generate_ulid

#: E2E 環境在 pin repo 裡的名字**必須和整合測試分開**。
#: `MyAiStorage-pin-test` 是共用的，`.pin/<repo>.json` 以 repo 名字分檔；如果
#: e2e 也用 `agora`，任何一輪整合測試都會把 e2e 的釘選值蓋掉（實測：A 線的
#: 整合測試在我 setup 之後跑了 `pin(agora): promote`，e2e 的 uuid 就被換掉了）。
DEFAULT_REPO_SUFFIX = "e2e"


CONFIG_DIR = Path.home() / ".config" / "aistorage"

# ── 測試用 profile（E-P4：期 1 除了正式 profile 另有測試用 profile）──────────
DEFAULT_E2E_PROFILE = "mac-opencode-test"
ENV_E2E_PROFILE = "AISTORAGE_E2E_PROFILE"
DEFAULT_E2E_SECRETS_ROOT = CONFIG_DIR / "resident-e2e"
ENV_E2E_SECRETS_ROOT = "AISTORAGE_E2E_SECRETS_ROOT"
PRODUCTION_SECRETS_ROOT = CONFIG_DIR / "resident"
PRODUCTION_PROFILE = "mac-opencode"

#: e2e 的預設模型（與 tests/e2e/conftest.py 的 DEFAULT_E2E_MODEL 一致）。
DEFAULT_E2E_MODEL = "opencode/space-bunny-free"
ENV_E2E_MODEL = "AISTORAGE_E2E_MODEL"

#: LLM 金鑰的**來源路徑**（只複製／連結檔案本身，絕不讀內容）。
ENV_E2E_LLM_KEY = "AISTORAGE_E2E_LLM_KEY"

#: 免金鑰的 provider（與 `resident/run.sh`／`entrypoint.sh` 同一份設定；
#: opencode zen 的免費模型匿名可用、依 IP 限流）。可用環境變數覆寫。
KEYLESS_PROVIDERS_ENV = "AISTORAGE_KEYLESS_PROVIDERS"
DEFAULT_KEYLESS_PROVIDERS = "opencode"


def provider_needs_no_key(provider: str, env: dict[str, str] | None = None) -> bool:
    """這個 provider 是否免金鑰（與 run.sh／entrypoint.sh 同一個判斷）。"""
    e = env if env is not None else os.environ
    raw = e.get(KEYLESS_PROVIDERS_ENV) or DEFAULT_KEYLESS_PROVIDERS
    return provider in raw.split()


#: `resident/run.sh` 的白名單（檔名必須在這裡面，否則容器拒絕啟動）。
RESIDENT_WHITELIST = (
    "rclone-worker.conf",
    "sa-reader.json",
    "signing.key",
    "reader.json",
    "gh-pat-actions.txt",
)
IDS_ENV = CONFIG_DIR / "ids.env"
COMMITTER_CONF = CONFIG_DIR / "rclone-committer-test.conf"
WORKER_CONF = CONFIG_DIR / "rclone-worker.conf"
SA_READER_KEY = CONFIG_DIR / "sa-reader.json"
PIN_KEY = CONFIG_DIR / "pin-test.key"
KNOWN_HOSTS = REPO_ROOT / "config" / "github_known_hosts"
PIN_REPO_URL = "git@github.com:FATESAIKOU/MyAiStorage-pin-test.git"
TEST_KEYS_DIR = CONFIG_DIR / "test-keys"

DEFAULT_STATE_FILE = REPO_ROOT / "config" / "e2e_state.json"
COMMITTER_E2E_JSON = REPO_ROOT / "config" / "committer.e2e.json"
COMMITTER_FOUNDRY_E2E_JSON = REPO_ROOT / "config" / "committer.foundry.e2e.json"
IDENTITY_E2E_JSON = REPO_ROOT / "config" / "identity.e2e.json"
READER_E2E_JSON = REPO_ROOT / "reader.e2e.json"
READER_CONFIG_E2E_JSON = REPO_ROOT / "config" / "reader.e2e.json"

RCLONE_REMOTE = "gdrive"

#: 前綴資料夾的名稱格式：**剛好**是 ``e2e-<ULID>``。
#: 刻意不放寬成「開頭是 e2e-」：那會連 ``e2e-<ULID>-inbox`` 這種名字都算進來，
#: 誤刪別人放在同一層的資料夾。自訂名稱（``--prefix``）的前綴由狀態檔的
#: keep_ids 保護，不靠這個格式。
PREFIX_NAME_RE = re.compile(r"^e2e-[0-9A-HJKMNP-TV-Z]{26}$")


# -----------------------------------------------------------------------------
# 輔助函式：環境設定讀取
# -----------------------------------------------------------------------------


def _require_file(path: Path, desc: str) -> Path:
    if not path.is_file():
        raise RuntimeError(f"缺少必要檔案: {desc} (預期路徑: {path})")
    return path


def read_test_folder_id(env_path: Path) -> str:
    """自 ids.env 讀取 TEST_FOLDER_ID（非秘密 id）。"""
    _require_file(env_path, "TEST_FOLDER_ID 來源檔 ids.env")
    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"^\s*TEST_FOLDER_ID\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"{env_path} 裡找不到 TEST_FOLDER_ID")
    return match.group(1).strip().strip("'\"")


def read_sa_email(sa_key_path: Path) -> str:
    """自 sa-reader.json 讀取 client_email（非機敏識別碼）。"""
    _require_file(sa_key_path, "SA 金鑰檔 sa-reader.json")
    try:
        data = json.loads(sa_key_path.read_text(encoding="utf-8"))
        email = data.get("client_email")
        if not email or not isinstance(email, str):
            raise ValueError("缺少 client_email 欄位")
        return email
    except Exception as e:
        raise RuntimeError(f"解析 {sa_key_path} client_email 失敗: {e}") from e


def git_env(rclone_conf: Path) -> dict[str, str]:
    """建立乾淨的 git / git-annex 環境變數，帶入 RCLONE_CONFIG 路徑。"""
    env = dict(os.environ)
    env["RCLONE_CONFIG"] = str(rclone_conf)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


class safe_workdir_ctx:
    """暫時切換工作目錄至安全暫存區，確保任何 git / git-annex 命令絕不在專案 repo 目錄執行。"""

    def __init__(self, target_dir: Path):
        self.target = target_dir
        self.orig = Path.cwd()

    def __enter__(self):
        os.chdir(self.target)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        os.chdir(self.orig)


def _run(args: Sequence[str], cwd: Path, env: dict[str, str], *, check: bool = True) -> str:
    proc = subprocess.run(
        list(args),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"指令失敗 (rc={proc.returncode}): {' '.join(args[:3])}…\n{proc.stderr.strip()[-500:]}"
        )
    return proc.stdout


# -----------------------------------------------------------------------------
# Drive 操作工具
# -----------------------------------------------------------------------------


def destroy_tree(
    drive: DriveClient,
    folder_id: str,
    allowed_parent: str,
    *,
    max_retries: int = 3,
) -> int:
    """依 file id 永久刪除整棵樹；刪除前驗證 parents 確實在允許之父層底下。"""
    try:
        meta = drive.get(folder_id)
    except NotFound:
        return 0

    if allowed_parent not in meta.parents:
        raise RuntimeError(
            f"拒絕刪除 {meta.name} (id={folder_id})：parents {meta.parents} 不含允許之父層 {allowed_parent}"
        )

    removed = 0
    children = drive.list_children(folder_id)
    for child in children:
        if child.is_folder:
            removed += destroy_tree(drive, child.id, folder_id, max_retries=max_retries)
        else:
            for attempt in range(max_retries):
                try:
                    drive.delete_permanently(child.id)
                    removed += 1
                    break
                except Exception:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(1.0 + attempt)

    for attempt in range(max_retries):
        try:
            drive.delete_permanently(folder_id)
            removed += 1
            break
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(1.0 + attempt)

    return removed


def share_folder_with_reader(
    drive: HttpDriveClient,
    folder_id: str,
    sa_email: str,
) -> None:
    """將讀取視圖資料夾分享給 SA reader (role=reader, type=user)。"""
    # 檢查是否已具備權限（維持冪等）
    list_url = f"{DRIVE_API_BASE}/files/{folder_id}/permissions?fields=permissions(id,role,type,emailAddress)"
    _, _, list_resp = drive._request(list_url, method="GET")
    existing_perms = json.loads(list_resp.decode("utf-8")).get("permissions", [])
    for perm in existing_perms:
        if perm.get("emailAddress") == sa_email:
            return

    create_url = f"{DRIVE_API_BASE}/files/{folder_id}/permissions?fields=id,role,type,emailAddress"
    body = json.dumps({"type": "user", "role": "reader", "emailAddress": sa_email}).encode("utf-8")
    drive._request(
        create_url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=body,
        is_write=True,
    )


# -----------------------------------------------------------------------------
# git-annex 倉庫建立
# -----------------------------------------------------------------------------


@dataclass
class AnnexSetup:
    prefix: str
    uuid: str
    url: str
    main_sha: str
    annex_sha: str
    max_git_bundles: int


def build_annex_repo(
    *,
    repo_name: str,
    prefix: str,
    workdir: Path,
    rclone_conf: Path,
    max_git_bundles: int = 10,
    seed_payload: bytes,
    seed_filename: str,
    schema_version: str,
) -> AnnexSetup:
    """在暫存目錄建立乾淨之 git-annex repo，配置 largefiles 與 max-git-bundles，推送到 Drive。"""
    env = git_env(rclone_conf)
    repo = workdir / f"{repo_name}_seed"
    repo.mkdir(parents=True, exist_ok=True)

    _run(["git", "init", "-q", "-b", "main", "."], repo, env)
    _run(["git", "annex", "init", f"aistorage-{repo_name}-e2e"], repo, env)
    _run(
        [
            "git", "annex", "initremote", "drive",
            "type=rclone", "encryption=none",
            f"rcloneremotename={RCLONE_REMOTE}",
            f"rcloneprefix={prefix}",
            "autoenable=true", "--with-url",
        ],
        repo,
        env,
    )
    # 決策 2.6：annex.largefiles 包含 *.json，max-git-bundles=10
    _run(["git", "annex", "config", "--set", "annex.largefiles", "include=*.json"], repo, env)
    _run(["git", "config", "annex.max-git-bundles", str(max_git_bundles)], repo, env)
    _run(["git", "config", "user.email", f"e2e-{repo_name}@aistorage.local"], repo, env)
    _run(["git", "config", "user.name", f"AiStorage E2E {repo_name.capitalize()}"], repo, env)

    (repo / "README.md").write_text(f"{repo_name.capitalize()} E2E prefix: {prefix}\n", encoding="utf-8")
    (repo / "_committer").mkdir(exist_ok=True)
    (repo / "_committer" / "schema_version").write_text(f"{schema_version}\n", encoding="utf-8")

    if repo_name == "foundry":
        (repo / "catalog").mkdir(exist_ok=True)
        (repo / "objects").mkdir(exist_ok=True)
        (repo / "catalog" / ".gitkeep").touch()
        (repo / "objects" / ".gitkeep").touch()

    _run(["git", "add", "README.md", "_committer"], repo, env)
    if repo_name == "foundry":
        _run(["git", "add", "catalog", "objects"], repo, env)
    _run(["git", "commit", "-qm", f"init: {repo_name} seed layout"], repo, env)

    # 寫入 seed annex payload（匹配 include=*.json，成為 annex 物件）
    payload_path = repo / seed_filename
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(seed_payload)
    _run(["git", "add", str(payload_path.relative_to(repo))], repo, env)
    _run(["git", "commit", "-qm", f"init: seed annexed payload {seed_filename}"], repo, env)

    _run(["git", "annex", "copy", "--to", "drive"], repo, env)
    _run(["git", "push", "drive", "main", "git-annex"], repo, env)

    info = _run(["git", "annex", "info", "drive", "--fast"], repo, env)
    uuid = ""
    for line in info.splitlines():
        if line.startswith("uuid:"):
            uuid = line.split(":", 1)[1].strip()
    if not uuid:
        raise RuntimeError(f"取不到 {repo_name} git-annex remote 的 uuid")

    url = (
        f"annex::{uuid}?encryption=none&type=rclone"
        f"&rcloneremotename={RCLONE_REMOTE}&rcloneprefix={prefix}"
    )
    main_sha = _run(["git", "rev-parse", "refs/heads/main"], repo, env).strip()
    annex_sha = _run(["git", "rev-parse", "refs/heads/git-annex"], repo, env).strip()

    return AnnexSetup(
        prefix=prefix,
        uuid=uuid,
        url=url,
        main_sha=main_sha,
        annex_sha=annex_sha,
        max_git_bundles=max_git_bundles,
    )


# -----------------------------------------------------------------------------
# 簽章金鑰與身分登錄檔
# -----------------------------------------------------------------------------


def setup_signing_keys(
    keys_dir: Path,
    inbox_folder_id: str,
    identity_path: Path,
    profile: str = PRODUCTION_PROFILE,
) -> tuple[str, str]:
    """確保測試簽章金鑰就緒，並產出合規之 config/identity.e2e.json（只有公開公鑰）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    keys_dir.mkdir(parents=True, exist_ok=True)
    key_file = keys_dir / "signing.key"

    if key_file.is_file() and key_file.stat().st_size == 32:
        priv_raw = key_file.read_bytes()
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(priv_raw)
    else:
        priv = ed25519.Ed25519PrivateKey.generate()
        priv_raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_file.write_bytes(priv_raw)
        os.chmod(key_file, 0o600)

    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = f"{profile}-{hashlib.sha256(pub_raw).hexdigest()[:8]}"
    public_key_b64 = base64.b64encode(pub_raw).decode("utf-8")

    registry_payload = {
        "format": "aistorage.identity/v1",
        "profiles": {
            profile: {
                "allowed_types": [
                    "session",
                    "handoff",
                    "claim",
                    "reference",
                    "rewrite",
                    "artifact",
                ],
                "signing_keys": [
                    {
                        "key_id": key_id,
                        "public_key": public_key_b64,
                        "status": "active",
                        "added_at": "2026-09-28T00:00:00Z",
                        "revoked_at": None,
                    }
                ],
                "inbox_folder_ids": [inbox_folder_id],
            }
        },
    }

    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(registry_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 自行驗證 registry 符合 schema 且非 example
    load_registry(identity_path, allow_example=False)

    return key_id, public_key_b64


def list_e2e_prefixes(drive: DriveClient, test_root_id: str) -> list[Any]:
    """列出 TEST_FOLDER_ID **直接**底下所有 `e2e-*` 資料夾（依 name 排序）。

    只看直接子層：前綴裡的 inbox／readview 等子資料夾不是前綴。
    """
    found = [c for c in drive.list_children(test_root_id)
             if c.is_folder and PREFIX_NAME_RE.match(c.name)]
    return sorted(found, key=lambda c: c.name)


def sweep_orphan_prefixes(
    *,
    drive: DriveClient,
    test_root_id: str,
    keep_prefix_id: str | None = None,
    keep_ids: Sequence[str] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """清掉中斷留下的 `e2e-*` 前綴（依 file id 永久刪除）。

    「留下來的那一個」是 `keep_prefix_id`（狀態檔或 `--prefix-id` 指的那個），
    其餘 `e2e-*` 都是上一次中斷的殘留。**刪之前一定先 `get()` 確認 parents**
    確實在 TEST_FOLDER_ID 底下（`destroy_tree` 會擋）。

    為什麼需要：中斷會留下前綴與 pin repo 的不一致狀態，之後每一輪整合測試都會
    多看到一份垃圾，而且沒有人認得那是什麼。
    """
    prefixes = list_e2e_prefixes(drive, test_root_id)
    keep_set = {i for i in (keep_prefix_id, *keep_ids) if i}
    removed: list[str] = []
    kept: list[str] = []
    for meta in prefixes:
        if meta.id in keep_set:
            kept.append(meta.name)
            continue
        if dry_run:
            removed.append(meta.name)
            continue
        count = destroy_tree(drive, meta.id, allowed_parent=test_root_id)
        removed.append(f"{meta.name}（刪了 {count} 個項目）")
    return {
        "found": [m.name for m in prefixes],
        "removed": removed,
        "kept": kept,
        "dry_run": dry_run,
    }


# -----------------------------------------------------------------------------
# 測試 profile 的秘密目錄（E-P4）
# -----------------------------------------------------------------------------


def resolve_e2e_profile(env: dict[str, str] | None = None) -> str:
    """測試 profile 名稱（預設 ``mac-opencode-test``，可由環境變數覆寫）。"""
    e = env if env is not None else os.environ
    profile = (e.get(ENV_E2E_PROFILE) or "").strip() or DEFAULT_E2E_PROFILE
    if profile == PRODUCTION_PROFILE:
        raise RuntimeError(
            f"E2E 不能用正式的 {PRODUCTION_PROFILE} profile：spec「期 1 的身分種類」"
            f"要求另有測試用 profile。用 {ENV_E2E_PROFILE} 指定別的名稱。"
        )
    return profile


def resolve_secrets_root(env: dict[str, str] | None = None) -> Path:
    """測試 profile 的秘密根目錄（預設 ``~/.config/aistorage/resident-e2e``）。"""
    e = env if env is not None else os.environ
    root = Path(e.get(ENV_E2E_SECRETS_ROOT) or DEFAULT_E2E_SECRETS_ROOT).expanduser()
    if root.resolve() == PRODUCTION_SECRETS_ROOT.resolve():
        raise RuntimeError(
            f"E2E 禁止使用正式的 profile 秘密根目錄（{PRODUCTION_SECRETS_ROOT}）；"
            f"測試 profile 要放在另一個根目錄（{ENV_E2E_SECRETS_ROOT} 可覆寫）。"
        )
    return root


# ── e2e 環境鎖 ────────────────────────────────────────────────────────────────
#: e2e 環境是**單例**：Drive 上只有一個前綴、pin repo 只有一份釘選值、本機只有一份
#: `config/*.e2e.json`。所以任何一條線跑 `--teardown`／`--recreate` 會直接摧毀另一條
#: 線正在跑的東西（實測：別條線 teardown 把 profile 的 `reader.json` 刪掉，跑中的
#: 容器讀不到收件匣 id，`sync_once` 直接 ConfigError）。
E2E_LOCK_NAME = ".e2e-env.lock.json"


class E2EEnvLocked(RuntimeError):
    """e2e 環境正被別人使用（鎖檔存在且持有者還活著）。"""


def e2e_lock_path(secrets_root: Path | None = None, env: dict[str, str] | None = None) -> Path:
    """鎖檔路徑：``<測試秘密根目錄>/.e2e-env.lock.json``。

    放秘密根目錄而不是 repo：鎖是「這台機器上這個環境有人在用」狀態，
    跟某個 worktree 无关（多個 worktree 共用同一個 e2e 環境）。
    """
    root = secrets_root if secrets_root is not None else resolve_secrets_root(env)
    return root / E2E_LOCK_NAME


def read_e2e_lock(secrets_root: Path | None = None) -> dict[str, Any] | None:
    """讀鎖檔；不存在或壞掉都當作沒有鎖（回 ``None``）。"""
    path = e2e_lock_path(secrets_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    """這個 pid 還活著嗎（同一台機器上）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 別人的行程：存在，但我們沒權限送訊號
        return True
    return True


def e2e_lock_holder(
    secrets_root: Path | None = None, *, now: float | None = None
) -> dict[str, Any] | None:
    """鎖的**有效**持有者；沒有鎖或鎖已過期就回 ``None``。

    判定：pid 還活著（且是同一台機器）才算有人在用。pid 已死 → 過期，
    可以接手；別的機器留下的鎖 → 無法驗證，也當過期。
    """
    data = read_e2e_lock(secrets_root)
    if data is None:
        return None
    host = str(data.get("host") or "")
    if host and host != _lock_host():
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    age_s: float | None = None
    started = data.get("started_at")
    if now is not None and isinstance(started, (int, float)):
        age_s = max(0.0, now - float(started))
    return {"pid": pid, "started_at": started, "action": data.get("action"),
            "host": host, "age_s": age_s}


def _lock_host() -> str:
    return socket.gethostname()


def acquire_e2e_lock(
    *,
    action: str,
    secrets_root: Path | None = None,
    force: bool = False,
    now: float | None = None,
) -> Path:
    """取得 e2e 環境鎖；有人在用就丟 `E2EEnvLocked`。

    `force=True` 等於 `--force-unlock`：明知有人在用也硬拿（真的要去救一個
    卡死的環境時才用，而且要留痕在鎖檔的 `forced_by`）。
    """
    path = e2e_lock_path(secrets_root)
    holder = e2e_lock_holder(secrets_root, now=now)
    if holder is not None and holder["pid"] == os.getpid():
        # 同一個行程重入（例如 --recreate 內部呼叫 run_teardown）：
        # 不是「別人在用」，不必也不該 --force-unlock。
        holder = None
    if holder and not force:
        raise E2EEnvLocked(
            f"e2e 環境正被 pid {holder['pid']} 使用"
            f"（{holder.get('action') or '未標明動作'}，開始於 {holder.get('started_at')}）。\n"
            f"teardown／recreate 會摧毀對方正在跑的東西，所以拒絕。\n"
            f"請等對方結束（鎖檔: {path}），或確認對方已經卡死後加 --force-unlock。"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": time.time() if now is None else now,
        "host": _lock_host(),
        "action": action,
    }
    if holder and force:
        payload["forced_by"] = {"prev_pid": holder["pid"], "at": payload["started_at"]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if holder and force:
        print(f"[e2e-lock] --force-unlock：從 pid {holder['pid']} 手上搶過 e2e 環境鎖")
    return path


def release_e2e_lock(secrets_root: Path | None = None, *, pid: int | None = None) -> bool:
    """放掉鎖；只有自己持有的那份才會被刪掉。回傳是否真的刪了。"""
    path = e2e_lock_path(secrets_root)
    data = read_e2e_lock(secrets_root)
    if data is None:
        return False
    owner = pid if pid is not None else os.getpid()
    if int(data.get("pid") or -1) != owner:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _place_secret(target: Path, source: Path, *, how: str = "link") -> str:
    """把秘密檔「以檔案本身」放到位（**絕不讀內容**）。

    為什麼不用 symlink：`resident/run.sh` 的權限檢查用 `stat -f '%Sp'` 看
    **連結本身**的權限（symlink 一定是 `lrwxr-xr-x`），所以 symlink 會被
    「權限過寬」擋下、容器起不來。實測過。

    - `link`（預設）：`os.link` 硬連結——對 run.sh 就是一般的 0600 檔案，
      而且來源更新時不必重新佈置；跨檔案系統失敗就退回 `copy`。
    - `copy`：`shutil.copyfile` ＋ chmod 600。
    - `symlink`：留著除錯用，**會被 run.sh 擋**（呼叫端要自己知道）。
    """
    if how not in ("link", "copy", "symlink"):
        raise ValueError(f"未知的放置方式：{how}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            raise RuntimeError(f"目標位置已經有東西而且不是檔案：{target}")
    if how == "symlink":
        os.symlink(source, target)
        return "symlink"
    if how == "copy":
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        return "copy"
    try:
        os.link(source, target)
    except OSError:
        # 跨檔案系統（或不支援硬連結）→ 退回複製
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        return "copy"
    # 硬連結共用 inode：權限是來源的。如果來源對 group/other 開放，
    # **不要 chmod**（那會改到使用者自己的憑證檔），直接退回複製並收緊到 0600，
    # 否則 resident/run.sh 會以「權限過寬」拒絕啟動。
    if stat.S_IMODE(target.stat().st_mode) & 0o077:
        target.unlink()
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        return "copy(權限已收緊)"
    return "hardlink"


def setup_resident_profile_dir(
    *,
    profile: str,
    secrets_root: Path,
    reader_config_payload: dict[str, Any],
    llm_key_source: Path | None,
    worker_conf: Path = WORKER_CONF,
    sa_reader_key: Path = SA_READER_KEY,
    signing_key: Path | None = None,
    model: str = DEFAULT_E2E_MODEL,
    copy_secrets: bool = False,
    secrets_how: str = "link",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """佈置住民容器要用的秘密目錄（檔名依 `resident/run.sh` 的白名單）。

    只以路徑引用：簽章金鑰來自 `~/.config/aistorage/test-keys/`，LLM 金鑰來自
    `AISTORAGE_E2E_LLM_KEY` 指定的來源路徑。**任何檔案的內容都不被讀取**。
    """
    e = env if env is not None else os.environ
    profile_dir = secrets_root / profile
    if secrets_root.resolve() == PRODUCTION_SECRETS_ROOT.resolve():
        raise RuntimeError("E2E 不能佈置到正式的 profile 秘密根目錄")
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)

    provider = model.split("/", 1)[0] if "/" in model else "opencode"
    signing_key = signing_key or (TEST_KEYS_DIR / "signing.key")
    _require_file(signing_key, "測試簽章金鑰")

    placements: dict[str, str] = {}
    how = "copy" if copy_secrets else secrets_how
    # 來源檔先檢查存在（不要放到一半才失敗）
    _require_file(worker_conf, "worker rclone 設定")
    _require_file(sa_reader_key, "SA 讀取身分")
    placements["signing.key"] = _place_secret(
        profile_dir / "signing.key", signing_key, how=how
    )
    placements["rclone-worker.conf"] = _place_secret(
        profile_dir / "rclone-worker.conf", worker_conf, how=how
    )
    placements["sa-reader.json"] = _place_secret(
        profile_dir / "sa-reader.json", sa_reader_key, how=how
    )
    # reader.json 不是秘密，但容器裡要的就是這一份（測試收件匣的 id）
    reader_path = profile_dir / "reader.json"
    reader_path.write_text(
        json.dumps(reader_config_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(reader_path, 0o600)
    placements["reader.json"] = "write"

    llm_name = f"llm-{provider}.key"
    llm_source = llm_key_source
    llm_note = ""
    if provider_needs_no_key(provider, e):
        # 免金鑰的 provider：**不要放** llm-<provider>.key。放一個佔位值反而會壞掉
        # ——實測 apiKey 指向佔位／不存在的檔案時，連 POST /session 都會 400。
        if llm_source is not None:
            llm_note = (
                f"provider={provider} 免金鑰（匿名可用），已忽略 {ENV_E2E_LLM_KEY}；"
                f"不會放 {llm_name}（放佔位值會讓 opencode 認證失敗）"
            )
        else:
            llm_note = f"provider={provider} 免金鑰（匿名可用），不需要 {llm_name}"
        llm_source = None
        stale = profile_dir / llm_name
        if stale.is_symlink() or stale.exists():
            stale.unlink()
            llm_note += f"；已移除既有的 {llm_name}"
    if llm_source is None and not provider_needs_no_key(provider, e):
        llm_note = (
            f"沒有 {ENV_E2E_LLM_KEY}，而且 provider={provider} 需要金鑰 → "
            f"請把憑證放在任何路徑並用 {ENV_E2E_LLM_KEY}=<路徑> 指定，"
            "setup 只會複製／連結檔案本身（不讀內容）。"
        )
    if llm_source is not None:
        _require_file(llm_source, f"LLM 金鑰（{ENV_E2E_LLM_KEY}）")
        placements[llm_name] = _place_secret(
            profile_dir / llm_name, llm_source, how=how
        )
    else:
        if not provider_needs_no_key(provider, e):
            llm_note = (
                f"找不到 LLM 金鑰：請把憑證放在任何路徑並用 {ENV_E2E_LLM_KEY}=<路徑> 指定，"
                "setup 只會複製／連結檔案本身。"
            )

    # 名字必須在 resident/run.sh 的白名單裡，否則容器拒絕啟動
    unexpected = [n for n in placements if n not in RESIDENT_WHITELIST
                  and not n.startswith("llm-")]
    if unexpected:
        raise RuntimeError(f"產出了白名單外的檔名（容器會拒絕啟動）：{unexpected}")

    return {
        "profile": profile,
        "secrets_root": str(secrets_root),
        "profile_dir": str(profile_dir),
        "model": model,
        "provider": provider,
        "files": sorted(placements),
        "placements": placements,
        "llm_key_source": str(llm_source) if llm_source else None,
        "llm_note": llm_note,
    }


# -----------------------------------------------------------------------------
# 主要 Setup 流程
# -----------------------------------------------------------------------------


def run_setup(
    *,
    prefix_override: str | None = None,
    state_file: Path = DEFAULT_STATE_FILE,
    recreate: bool = False,
    pin_repo_url: str = PIN_REPO_URL,
    repo_suffix: str = DEFAULT_REPO_SUFFIX,
    profile: str | None = None,
    secrets_root: Path | None = None,
    model: str | None = None,
    llm_key_source: Path | None = None,
    copy_secrets: bool = False,
    secrets_how: str = "link",
    force_unlock: bool = False,
) -> dict[str, Any]:
    """執行完整 E2E 環境佈建。可重跑且具備冪等性。

    `repo_suffix` 決定 pin repo 裡的名稱（`agora-e2e`／`foundry-e2e`），必須和
    整合測試用的名稱分開，否則兩邊會互相覆蓋釘選值。

    `profile`（預設 `mac-opencode-test`，`AISTORAGE_E2E_PROFILE` 可覆寫）是測試
    專用的 profile：身分登錄檔、讀取設定的收件匣對應、還有住民容器的秘密目錄
    都用它，而且**不能用正式的 `mac-opencode`**（E-P4）。
    """
    profile = profile or resolve_e2e_profile()
    secrets_root = secrets_root or resolve_secrets_root()
    # setup 本身也是使用者：別人正在跑 e2e 時不要順手把前綴掃掉
    acquire_e2e_lock(action="setup", secrets_root=secrets_root, force=force_unlock)
    try:
        return _setup_locked(
            prefix_override=prefix_override,
            state_file=state_file,
            recreate=recreate,
            pin_repo_url=pin_repo_url,
            repo_suffix=repo_suffix,
            profile=profile,
            secrets_root=secrets_root,
            model=model,
            llm_key_source=llm_key_source,
            copy_secrets=copy_secrets,
            secrets_how=secrets_how,
        )
    finally:
        release_e2e_lock(secrets_root)


def _setup_locked(
    *,
    prefix_override: str | None,
    state_file: Path,
    recreate: bool,
    pin_repo_url: str,
    repo_suffix: str,
    profile: str,
    secrets_root: Path,
    model: str | None,
    llm_key_source: Path | None,
    copy_secrets: bool,
    secrets_how: str,
) -> dict[str, Any]:
    model = model or os.environ.get(ENV_E2E_MODEL) or DEFAULT_E2E_MODEL
    test_root_id = read_test_folder_id(IDS_ENV)
    sa_email = read_sa_email(SA_READER_KEY)
    _require_file(COMMITTER_CONF, "提交流程 rclone 設定檔")
    _require_file(WORKER_CONF, "住民 worker rclone 設定檔")
    _require_file(PIN_KEY, "pin repo deploy key")
    _require_file(KNOWN_HOSTS, "GitHub known_hosts")

    os.environ["RCLONE_CONFIG"] = str(COMMITTER_CONF)

    committer_drive = HttpDriveClient(RcloneConfToken(COMMITTER_CONF, remote=RCLONE_REMOTE))
    worker_drive = HttpDriveClient(RcloneConfToken(WORKER_CONF, remote=RCLONE_REMOTE))

    # 0. 先清掉中斷留下的 e2e-* 前綴（依 file id，刪前確認 parents）
    keep_prefix_id = None
    keep_ids: list[str] = []
    if state_file.is_file():
        try:
            prior = json.loads(state_file.read_text(encoding="utf-8"))
            keep_prefix_id = prior.get("prefix_folder_id")
            # 狀態檔裡記錄的所有 id（子資料夾等）都要保留，不只是前綴本身
            keep_ids = [v for v in prior.values()
                        if isinstance(v, str) and re.fullmatch(r"[0-9A-Za-z_-]{10,}", v)]
        except (OSError, ValueError):
            keep_prefix_id, keep_ids = None, []
    sweep = sweep_orphan_prefixes(
        drive=committer_drive, test_root_id=test_root_id,
        keep_prefix_id=keep_prefix_id, keep_ids=keep_ids,
    )
    if sweep["found"]:
        print(
            f"[e2e-setup] TEST_FOLDER_ID 底下有 {len(sweep['found'])} 個 e2e-* 前綴；"
            f"保留 {sweep['kept'] or '（無）'}，清除其餘"
        )
        for name in sweep["removed"]:
            print(f"[e2e-setup]   已清除: {name}")
    else:
        print("[e2e-setup] TEST_FOLDER_ID 底下沒有 e2e-* 前綴殘留")

    # 檢查現有狀態
    if state_file.is_file() and not recreate:
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            existing_prefix_id = state.get("prefix_folder_id")
            if existing_prefix_id:
                meta = committer_drive.get(existing_prefix_id)
                if test_root_id in meta.parents:
                    print(
                        f"[e2e-setup] 現有 E2E 環境仍然存在且有效: "
                        f"name={meta.name}, id={existing_prefix_id}"
                    )
                    print(f"[e2e-setup] 略過重複建立（若欲重建請帶 --recreate 或先執行 --teardown）")
                    return state
        except Exception as e:
            print(f"[e2e-setup] 現有狀態檔讀取或驗證未通過 ({e})，重新執行完整 setup")

    if state_file.is_file() and recreate:
        print("[e2e-setup] 偵測到 --recreate，先執行 teardown...")
        # 走 public run_teardown（--recreate 的語意是「先 teardown 再 setup」，
        # acceptance 測試訂的就是這個接縫）；鎖已由這個行程自己持有，重入不算阻擋
        run_teardown(
            state_file=state_file,
            profile=profile,
            secrets_root=secrets_root,
        )

    ulid = generate_ulid()
    prefix_name = prefix_override or f"e2e-{ulid}"
    # pin repo 裡的名稱：和整合測試分開，否則釘選值會互相覆蓋
    agora_pin_name = f"agora-{repo_suffix}" if repo_suffix else "agora"
    foundry_pin_name = f"foundry-{repo_suffix}" if repo_suffix else "foundry"
    print(f"[e2e-setup] 開始在 TEST_FOLDER_ID ({test_root_id}) 底下建立前綴: {prefix_name}")

    # 1. 在 TEST_FOLDER_ID 底下建立唯一之前綴資料夾
    top_folder = committer_drive.create(
        test_root_id, prefix_name, b"", mime_type=GOOGLE_FOLDER_MIME
    )
    prefix_folder_id = top_folder.id
    print(f"[e2e-setup] 已建立前綴資料夾: {prefix_name} (ID: {prefix_folder_id})")

    # 2. 建立各子資料夾
    agora_folder = committer_drive.create(
        prefix_folder_id, "agora", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    foundry_folder = committer_drive.create(
        prefix_folder_id, "foundry", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    quarantine_folder = committer_drive.create(
        prefix_folder_id, "quarantine", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    readview_folder = committer_drive.create(
        prefix_folder_id, "readview", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    foundry_readview_folder = committer_drive.create(
        prefix_folder_id, "readview-foundry", b"", mime_type=GOOGLE_FOLDER_MIME
    )

    # 3. 使用 worker client 建立收件匣資料夾
    inbox_folder = worker_drive.create(
        prefix_folder_id, "inbox", b"", mime_type=GOOGLE_FOLDER_MIME
    )
    print(f"[e2e-setup] Worker client 已建立收件匣: inbox (ID: {inbox_folder.id})")

    # 4. 建立 Agora 與 Foundry 之 git-annex repos
    with tempfile.TemporaryDirectory(prefix="aistorage_e2e_annex_") as td:
        temp_dir = Path(td)
        with safe_workdir_ctx(temp_dir):
            print("[e2e-setup] 正在建立 Agora git-annex 倉庫並推送到 Drive...")
            agora_seed_payload = (
                json.dumps({"format": "aistorage.agora/v1", "seed": True, "ulid": ulid}) + "\n"
            ).encode("utf-8")
            agora_annex = build_annex_repo(
                repo_name="agora",
                prefix=f"{prefix_name}/agora",
                workdir=temp_dir,
                rclone_conf=COMMITTER_CONF,
                max_git_bundles=10,
                seed_payload=agora_seed_payload,
                seed_filename="seed-agora.json",
                schema_version="agora/v1",
            )
            print(f"[e2e-setup] Agora 倉庫就緒: uuid={agora_annex.uuid} main={agora_annex.main_sha[:8]}")

            print("[e2e-setup] 正在建立 Foundry git-annex 倉庫並推送到 Drive...")
            foundry_seed_payload = (
                json.dumps({"format": "aistorage.foundry/v1", "seed": True, "ulid": ulid}) + "\n"
            ).encode("utf-8")
            foundry_annex = build_annex_repo(
                repo_name="foundry",
                prefix=f"{prefix_name}/foundry",
                workdir=temp_dir,
                rclone_conf=COMMITTER_CONF,
                max_git_bundles=10,
                seed_payload=foundry_seed_payload,
                seed_filename="objects/seed-foundry.json",
                schema_version="foundry/v1",
            )
            print(
                f"[e2e-setup] Foundry 倉庫就緒: uuid={foundry_annex.uuid} main={foundry_annex.main_sha[:8]}"
            )

            # 5. 在 MyAiStorage-pin-test 上執行 init-pin
            print("[e2e-setup] 正在對 Agora 與 Foundry 執行 init-pin 釘選...")
            pin_temp = temp_dir / "pins"
            pins = GitPinStore(
                repo_url=pin_repo_url,
                workdir=pin_temp,
                key_path=PIN_KEY,
                known_hosts_path=KNOWN_HOSTS,
            )

            # Agora init-pin
            cfg_agora = CommitterConfig(
                repo=agora_pin_name,
                repo_uuid=agora_annex.uuid,
                repo_url=agora_annex.url,
                prefix_folder_id=agora_folder.id,
                quarantine_folder_id=quarantine_folder.id,
                identity_registry_path=str(IDENTITY_E2E_JSON),
                pin_repo_url=pin_repo_url,
                max_git_bundles=10,
            )
            deps_agora = Deps(
                drive=committer_drive,
                pins=pins,
                git_factory=lambda dest: SubprocessAnnexGit.clone_for_commit(
                    agora_annex.url, dest, max_git_bundles=10
                ),
                registry=None,  # init-pin 不需要 registry
                converters=CONVERTERS,
                publisher=NullPublisher(),
                clock=SystemClock(),
            )
            pin_state_agora = init_pin_cli(cfg_agora, deps_agora, confirm=True)
            print(
                f"[e2e-setup] Agora init-pin 成功: manifest={pin_state_agora.manifest_sha256[:8]} "
                f"annex_keys={len(pin_state_agora.annex_keys)}"
            )

            # Foundry init-pin
            cfg_foundry = CommitterConfig(
                repo=foundry_pin_name,
                repo_uuid=foundry_annex.uuid,
                repo_url=foundry_annex.url,
                prefix_folder_id=foundry_folder.id,
                quarantine_folder_id=quarantine_folder.id,
                identity_registry_path=str(IDENTITY_E2E_JSON),
                pin_repo_url=pin_repo_url,
                max_git_bundles=10,
            )
            deps_foundry = Deps(
                drive=committer_drive,
                pins=pins,
                git_factory=lambda dest: SubprocessAnnexGit.clone_for_commit(
                    foundry_annex.url, dest, max_git_bundles=10
                ),
                registry=None,
                converters=CONVERTERS,
                publisher=NullPublisher(),
                clock=SystemClock(),
            )
            pin_state_foundry = init_pin_cli(cfg_foundry, deps_foundry, confirm=True)
            print(
                f"[e2e-setup] Foundry init-pin 成功: manifest={pin_state_foundry.manifest_sha256[:8]} "
                f"annex_keys={len(pin_state_foundry.annex_keys)}"
            )

    # 6. 建立讀取視圖資料夾與空的 manifest (generation 0)
    now_iso = format_rfc3339(SystemClock().now(), include_fraction=True)
    agora_manifest_bytes = serialize_manifest(
        initial_manifest(element="agora", published_at=now_iso)
    )
    agora_manifest_file = committer_drive.create(
        readview_folder.id,
        "readview-manifest.json",
        agora_manifest_bytes,
        mime_type="application/json",
    )
    print(f"[e2e-setup] Agora 讀取視圖 manifest (gen 0) 已建立 (ID: {agora_manifest_file.id})")

    foundry_manifest_bytes = serialize_manifest(
        initial_manifest(element="foundry", published_at=now_iso)
    )
    foundry_manifest_file = committer_drive.create(
        foundry_readview_folder.id,
        "readview-manifest.json",
        foundry_manifest_bytes,
        mime_type="application/json",
    )
    print(f"[e2e-setup] Foundry 讀取視圖 manifest (gen 0) 已建立 (ID: {foundry_manifest_file.id})")

    # 7. 分享讀取視圖資料夾給 SA reader
    share_folder_with_reader(committer_drive, readview_folder.id, sa_email)
    share_folder_with_reader(committer_drive, foundry_readview_folder.id, sa_email)
    print(f"[e2e-setup] 讀取視圖資料夾已分享給 SA reader ({sa_email})")

    # 8. 產生測試簽章金鑰並寫出 config/identity.e2e.json
    key_id, pub_b64 = setup_signing_keys(
        keys_dir=TEST_KEYS_DIR,
        inbox_folder_id=inbox_folder.id,
        identity_path=IDENTITY_E2E_JSON,
        profile=profile,
    )
    print(f"[e2e-setup] 身分登錄檔已產生: {IDENTITY_E2E_JSON} (key_id: {key_id})")

    # 9. 產出 config/committer.e2e.json 與 config/committer.foundry.e2e.json
    committer_cfg_payload = {
        "format": "aistorage.committer/v1",
        "repo": agora_pin_name,
        "repo_uuid": agora_annex.uuid,
        "repo_url": agora_annex.url,
        "prefix_folder_id": agora_folder.id,
        "quarantine_folder_id": quarantine_folder.id,
        "readview_folder_id": readview_folder.id,
        "readview_manifest_file_id": agora_manifest_file.id,
        "readview_rebuild_epoch": 0,
        "pin_repo_url": pin_repo_url,
        "identity_registry_path": str(IDENTITY_E2E_JSON.relative_to(REPO_ROOT)),
        "max_git_bundles": 10,
        "max_gc_per_run": 200,
        "max_raw_size": 52428800,
        "quarantine_retention_days": 7,
        "ledger_retention_months": 3,
        "prefix_levels": [],
        "github_repository": "FATESAIKOU/MyAiStorage-pin-test",
        "foundry": {
            "repo": foundry_pin_name,
            "repo_uuid": foundry_annex.uuid,
            "repo_url": foundry_annex.url,
            "prefix_folder_id": foundry_folder.id,
            "quarantine_folder_id": quarantine_folder.id,
            "readview_folder_id": foundry_readview_folder.id,
            "readview_manifest_file_id": foundry_manifest_file.id,
            "pin_repo_url": pin_repo_url,
            "max_git_bundles": 10,
        },
    }
    COMMITTER_E2E_JSON.parent.mkdir(parents=True, exist_ok=True)
    COMMITTER_E2E_JSON.write_text(
        json.dumps(committer_cfg_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    foundry_committer_cfg_payload = {
        "format": "aistorage.committer/v1",
        "repo": foundry_pin_name,
        "repo_uuid": foundry_annex.uuid,
        "repo_url": foundry_annex.url,
        "prefix_folder_id": foundry_folder.id,
        "quarantine_folder_id": quarantine_folder.id,
        "readview_folder_id": foundry_readview_folder.id,
        "readview_manifest_file_id": foundry_manifest_file.id,
        "readview_rebuild_epoch": 0,
        "pin_repo_url": pin_repo_url,
        "identity_registry_path": str(IDENTITY_E2E_JSON.relative_to(REPO_ROOT)),
        "max_git_bundles": 10,
        "max_gc_per_run": 200,
        "max_raw_size": 104857600,
        "quarantine_retention_days": 7,
        "ledger_retention_months": 3,
        "prefix_levels": [],
        "github_repository": "FATESAIKOU/MyAiStorage-pin-test",
    }
    COMMITTER_FOUNDRY_E2E_JSON.write_text(
        json.dumps(foundry_committer_cfg_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 10. 產出 resident 用的 reader.e2e.json（只有非秘密 id）
    reader_cfg_payload = {
        "format": "aistorage.reader/v1",
        "manifest_file_id": agora_manifest_file.id,
        "readview_folder_id": readview_folder.id,
        "inbox_folder_ids": {
            profile: inbox_folder.id,
        },
        "sa_key_path": "~/.config/aistorage/sa-reader.json",
        "foundry_manifest_file_id": foundry_manifest_file.id,
        "foundry_readview_folder_id": foundry_readview_folder.id,
    }
    READER_E2E_JSON.write_text(
        json.dumps(reader_cfg_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    READER_CONFIG_E2E_JSON.write_text(
        json.dumps(reader_cfg_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[e2e-setup] Reader 設定已產出: {READER_E2E_JSON}")

    # 10b. 佈置住民容器的秘密目錄（測試 profile 專用；E-P4）
    profile_dir_info = setup_resident_profile_dir(
        profile=profile,
        secrets_root=secrets_root,
        reader_config_payload=reader_cfg_payload,
        llm_key_source=llm_key_source,
        model=model,
        copy_secrets=copy_secrets,
        secrets_how=secrets_how,
    )
    print(
        f"[e2e-setup] 測試 profile 的秘密目錄已佈置: {profile_dir_info['profile_dir']}"
    )
    for name, how in sorted(profile_dir_info["placements"].items()):
        print(f"[e2e-setup]   {name}  ({how})")
    if profile_dir_info["llm_note"]:
        print(f"[e2e-setup]   注意：{profile_dir_info['llm_note']}")

    # 11. 儲存狀態檔
    state = {
        "ulid": ulid,
        "prefix_name": prefix_name,
        "test_root_id": test_root_id,
        "prefix_folder_id": prefix_folder_id,
        "agora_folder_id": agora_folder.id,
        "agora_repo_uuid": agora_annex.uuid,
        "agora_repo_url": agora_annex.url,
        "agora_pin_name": agora_pin_name,
        "foundry_folder_id": foundry_folder.id,
        "foundry_repo_uuid": foundry_annex.uuid,
        "foundry_repo_url": foundry_annex.url,
        "foundry_pin_name": foundry_pin_name,
        "quarantine_folder_id": quarantine_folder.id,
        "readview_folder_id": readview_folder.id,
        "readview_manifest_file_id": agora_manifest_file.id,
        "foundry_readview_folder_id": foundry_readview_folder.id,
        "foundry_manifest_file_id": foundry_manifest_file.id,
        "inbox_folder_id": inbox_folder.id,
        "signing_key_id": key_id,
        "profile": profile,
        "profile_dir": profile_dir_info["profile_dir"],
        "secrets_root": profile_dir_info["secrets_root"],
        "e2e_model": model,
        "e2e_provider": model.split("/", 1)[0] if "/" in model else "opencode",
        "e2e_provider_needs_no_key": provider_needs_no_key(
            model.split("/", 1)[0] if "/" in model else "opencode"
        ),
        "agora_pin_name": agora_pin_name,
        "foundry_pin_name": foundry_pin_name,
        "created_at": now_iso,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[e2e-setup] E2E 設定狀態檔已記錄至: {state_file}")
    print("[e2e-setup] 全部設定完成！")
    return state


# -----------------------------------------------------------------------------
# 主要 Teardown 流程
# -----------------------------------------------------------------------------


def run_teardown(
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    prefix_folder_id: str | None = None,
    profile: str | None = None,
    secrets_root: Path | None = None,
    force_unlock: bool = False,
) -> int:
    """依 file id 遞迴永久刪除整棵前綴樹，並清理本機暫存設定檔。

    別人正在用這個 e2e 環境時**拒絕**（`E2EEnvLocked`）：teardown 會刪掉對方
    正在跑的 Drive 前綴、pin 釘選值與本機設定檔。鎖已過期（持有者行程已死）
    就當作沒有人用。
    """
    test_root_id = read_test_folder_id(IDS_ENV)
    profile = profile or resolve_e2e_profile()
    secrets_root = secrets_root or resolve_secrets_root()
    # 鎖在真正刪任何東西**之前**就取得：取得之後別人就進不來了
    acquire_e2e_lock(action="teardown", secrets_root=secrets_root, force=force_unlock)
    try:
        return _teardown_locked(
            state_file=state_file,
            prefix_folder_id=prefix_folder_id,
            profile=profile,
            secrets_root=secrets_root,
        )
    finally:
        release_e2e_lock(secrets_root)


def _teardown_locked(
    *,
    state_file: Path,
    prefix_folder_id: str | None,
    profile: str,
    secrets_root: Path,
) -> int:
    test_root_id = read_test_folder_id(IDS_ENV)
    committer_drive = HttpDriveClient(RcloneConfToken(COMMITTER_CONF, remote=RCLONE_REMOTE))

    target_id = prefix_folder_id
    prefix_name = ""

    if not target_id and state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            target_id = state.get("prefix_folder_id")
            prefix_name = state.get("prefix_name", "")
        except Exception as e:
            print(f"[e2e-teardown] 解析狀態檔失敗 ({e})")

    if not target_id:
        print("[e2e-teardown] 找不到欲刪除的前綴 folder ID（無狀態檔且未指定 --prefix-id），無需清理。")
        return 0

    print(f"[e2e-teardown] 正在永久刪除 Drive 前綴資料夾: {prefix_name} (ID: {target_id})")
    removed_count = destroy_tree(committer_drive, target_id, allowed_parent=test_root_id)
    print(f"[e2e-teardown] 已永久刪除 {removed_count} 個項目。")

    # 清理產出之本機設定檔案
    files_to_clean = [
        state_file,
        COMMITTER_E2E_JSON,
        COMMITTER_FOUNDRY_E2E_JSON,
        IDENTITY_E2E_JSON,
        READER_E2E_JSON,
        READER_CONFIG_E2E_JSON,
    ]
    for p in files_to_clean:
        if p.is_file():
            try:
                p.unlink()
                print(f"[e2e-teardown] 已移除本機檔案: {p.relative_to(REPO_ROOT)}")
            except Exception as e:
                print(f"[e2e-teardown] 移除 {p} 失敗: {e}")

    # profile 目錄裡的 reader.json 是 setup 產出的、指向**這一次**的前綴；
    # 前綴刪掉之後它就是指向不存在資料夾的舊設定，容器會照著它上傳到錯誤的地方。
    # 所以刪掉它（其他檔案是使用者自己的憑證／硬連結，保留）。
    profile_dir = secrets_root / profile
    stale_reader = profile_dir / "reader.json"
    if stale_reader.is_file():
        stale_reader.unlink()
        print(f"[e2e-teardown] 已移除過期的 profile 設定: {stale_reader}")
    print(
        "[e2e-teardown] 測試 profile 的秘密目錄（resident-e2e/）**保留**："
        "裡面是硬連結的憑證檔（需要金鑰的 provider 才有 LLM 金鑰），"
        "重新 setup 會冪等重用。"
    )
    print("[e2e-teardown] Teardown 清理完畢！")
    return removed_count


# -----------------------------------------------------------------------------
# CLI 命令列進入點
# -----------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第 9 組端到端環境設定與清理工具")
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="清理模式：依 file id 永久刪除整個前綴與本機設定檔",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="強制重新建立：先執行 teardown 再重新 setup",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=None,
        help="自訂前綴資料夾名稱（預設為 e2e-<ULID>）",
    )
    parser.add_argument(
        "--sweep-orphans",
        action="store_true",
        help="只清除 TEST_FOLDER_ID 底下中斷留下的 e2e-* 前綴（依 file id），不做 setup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="搭配 --sweep-orphans：只列出會刪什麼，不真的刪",
    )
    parser.add_argument(
        "--prefix-id",
        type=str,
        default=None,
        help="手動指定要刪除的 Drive 前綴資料夾 ID（僅在 --teardown 時使用）",
    )
    parser.add_argument(
        "--force-unlock",
        action="store_true",
        help=(
            "明知有人正在用這個 e2e 環境也硬拿鎖（對方行程已死但忘記解鎖時用；"
            "會在鎖檔留下 forced_by 紀錄）"
        ),
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="狀態記錄檔路徑（預設: config/e2e_state.json）",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            f"測試用 profile 名稱（預設 {DEFAULT_E2E_PROFILE}，"
            f"也可由 {ENV_E2E_PROFILE} 覆寫）。不能用正式的 {PRODUCTION_PROFILE}"
        ),
    )
    parser.add_argument(
        "--secrets-root",
        type=Path,
        default=None,
        help=(
            "測試 profile 的秘密根目錄（預設 "
            f"{DEFAULT_E2E_SECRETS_ROOT}，也可由 {ENV_E2E_SECRETS_ROOT} 覆寫）"
        ),
    )
    parser.add_argument(
        "--llm-key",
        type=Path,
        default=None,
        help=(
            "LLM 金鑰的**來源路徑**（只複製／連結檔案本身，不讀內容；"
            f"也可由 {ENV_E2E_LLM_KEY} 覆寫）"
        ),
    )
    parser.add_argument(
        "--symlink-secrets",
        action="store_true",
        help=(
            "用 symlink 放置秘密檔（**會被 resident/run.sh 的權限檢查擋下**，"
            "容器起不來；只留著除錯用）"
        ),
    )
    parser.add_argument(
        "--copy-secrets",
        action="store_true",
        help="用複製而不是硬連結放置秘密檔（預設硬連結；symlink 會被 run.sh 的權限檢查擋下）",
    )
    parser.add_argument(
        "--repo-suffix",
        type=str,
        default=DEFAULT_REPO_SUFFIX,
        help=(
            "pin repo 裡的名稱後綴（預設 "
            f"{DEFAULT_REPO_SUFFIX!r} → agora-e2e／foundry-e2e）。"
            "必須和整合測試的名稱分開，否則釘選值會互相覆蓋"
        ),
    )
    parser.add_argument(
        "--pin-repo-url",
        type=str,
        default=PIN_REPO_URL,
        help=f"Pin repo URL（預設: {PIN_REPO_URL}）",
    )

    args = parser.parse_args(argv)

    if args.sweep_orphans:
        test_root_id = read_test_folder_id(IDS_ENV)
        drive = HttpDriveClient(RcloneConfToken(COMMITTER_CONF, remote=RCLONE_REMOTE))
        keep = args.prefix_id
        if not keep and args.state_file.is_file():
            try:
                keep = json.loads(
                    args.state_file.read_text(encoding="utf-8")).get("prefix_folder_id")
            except (OSError, ValueError):
                keep = None
        keep_ids: list[str] = []
        if args.state_file.is_file():
            try:
                prior = json.loads(args.state_file.read_text(encoding="utf-8"))
                keep_ids = [v for v in prior.values()
                            if isinstance(v, str) and re.fullmatch(r"[0-9A-Za-z_-]{10,}", v)]
            except (OSError, ValueError):
                keep_ids = []
        report = sweep_orphan_prefixes(
            drive=drive, test_root_id=test_root_id, keep_prefix_id=keep,
            keep_ids=keep_ids, dry_run=args.dry_run,
        )
        verb = "會刪除" if args.dry_run else "已刪除"
        print(f"[e2e-sweep] 找到 {len(report['found'])} 個 e2e-* 前綴：{report['found']}")
        for name in report["removed"]:
            print(f"[e2e-sweep]   {verb}: {name}")
        if report["kept"]:
            print(f"[e2e-sweep]   保留: {report['kept']}")
        if not report["found"]:
            print("[e2e-sweep] 沒有需要清除的前綴")
        return 0

    try:
        if args.teardown:
            run_teardown(
                state_file=args.state_file,
                prefix_folder_id=args.prefix_id,
                profile=args.profile,
                secrets_root=args.secrets_root,
                force_unlock=args.force_unlock,
            )
        else:
            run_setup(
                force_unlock=args.force_unlock,
                prefix_override=args.prefix,
                state_file=args.state_file,
                recreate=args.recreate,
                pin_repo_url=args.pin_repo_url,
                repo_suffix=args.repo_suffix,
                profile=args.profile,
                secrets_root=args.secrets_root,
                llm_key_source=args.llm_key,
                copy_secrets=args.copy_secrets,
                secrets_how="symlink" if args.symlink_secrets else "link",
            )
    except E2EEnvLocked as e:
        # 別人在用：講清楚就好，不要丟 traceback（這是預期中的拒絕）
        print(f"[e2e-lock] 拒絕執行：{e}", file=sys.stderr)
        return 75  # EX_TEMPFAIL：暫時不可用，稍後再試

    return 0


if __name__ == "__main__":
    sys.exit(main())

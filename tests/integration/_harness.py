"""第 3 組整合測試的共用工具：真的建 Drive 前綴、真的 git-annex repo、真的簽章收件匣項目。

規則（docs/impl/group3-modules.md 8.3）：
- 一切都建在 `TEST_FOLDER_ID` 底下的 `it-<ULID>/` 前綴裡；
- 測完依 file id 永久刪除，刪之前用 `get()` 確認 parents 確實是測試資料夾底下；
- 憑證與金鑰只以路徑引用，這裡不讀、不印任何秘密。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from aistorage.drive.model import DriveClient
from aistorage.schema import generate_ulid

RCLONE_REMOTE = "gdrive"


# --------------------------------------------------------------------- Drive


def new_prefix_name() -> str:
    return f"it-{generate_ulid()}"


def create_prefix(drive: DriveClient, test_root_id: str, name: str) -> tuple[str, str]:
    """在測試根資料夾底下建立前綴資料夾與隔離資料夾。

    回傳 `(prefix_folder_id, quarantine_folder_id)`；quarantine 放在前綴的**外面**
    （與前綴同一層），這樣前綴內不會出現子資料夾，清掃的「佈局是平的」才成立。
    """
    prefix = drive.create(test_root_id, name, b"", mime_type="application/vnd.google-apps.folder")
    assert prefix.is_folder, "建立前綴失敗：不是資料夾"
    quarantine = drive.create(
        test_root_id, f"{name}-quarantine", b"", mime_type="application/vnd.google-apps.folder"
    )
    assert quarantine.is_folder, "建立隔離資料夾失敗：不是資料夾"
    return prefix.id, quarantine.id


def destroy_tree(drive: DriveClient, folder_id: str, allowed_parent: str) -> int:
    """依 file id 永久刪除整棵樹；刪之前確認 parents 確實在允許的父層之下。"""
    meta = drive.get(folder_id)
    if allowed_parent not in meta.parents:
        raise RuntimeError(
            f"拒絕刪除 {meta.name}：parents {meta.parents} 不含允許的父層 {allowed_parent}"
        )
    removed = 0
    for child in drive.list_children(folder_id):
        if child.is_folder:
            removed += destroy_tree(drive, child.id, folder_id)
        else:
            drive.delete_permanently(child.id)
            removed += 1
    drive.delete_permanently(folder_id)
    return removed + 1


# ----------------------------------------------------------------- git-annex


@dataclass
class AnnexSetup:
    """一個真的 git-annex repo（在 Drive 前綴裡）的組裝結果。"""

    prefix: str
    uuid: str
    url: str
    workdir: Path
    main_sha: str
    annex_sha: str
    max_git_bundles: int
    env: dict[str, str] = field(default_factory=dict)


def git_env(rclone_conf: Path) -> dict[str, str]:
    """git-annex 會 shell out 叫 rclone，所以要帶 RCLONE_CONFIG（只傳路徑）。"""
    env = dict(os.environ)
    env["RCLONE_CONFIG"] = str(rclone_conf)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _run(args: Sequence[str], cwd: Path, env: dict[str, str], *, check: bool = True) -> str:
    proc = subprocess.run(
        list(args), cwd=str(cwd) if cwd is not None else None, env=env,
        capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"指令失敗 ({proc.returncode}): {' '.join(args[:3])}…\n{proc.stderr.strip()[-500:]}"
        )
    return proc.stdout


def build_annex_repo(
    *,
    prefix: str,
    workdir: Path,
    rclone_conf: Path,
    max_git_bundles: int = 20,
    annex_object_sizes: Sequence[int] = (300,),
) -> AnnexSetup:
    """建立一個真的 git-annex repo 並推到 Drive 前綴（git-remote-annex 會寫 manifest/bundle）。

    做法照技術驗證 1.2 的 `spike/scripts/annex_repo_init.sh`：`initremote drive
    type=rclone`、commit、`git annex copy --to drive`、`git push drive main git-annex`。
    push 之後 Drive 前綴裡就會有 `GITMANIFEST--<uuid>` 與 `GITBUNDLE-*`。
    """
    env = git_env(rclone_conf)
    repo = workdir / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    # rcloneprefix 只能是資料夾「名稱」。傳 file id 進去的話 rclone 會在測試根資料夾
    # 底下另外建一個以 id 命名的資料夾（真本會寫到錯的地方，測試看起來卻像沒建立）。
    if not prefix.startswith("it-"):
        raise ValueError(
            f"prefix 必須是以 'it-' 開頭的資料夾名稱（不是 Drive file id）: {prefix!r}"
        )

    _run(["git", "init", "-q", "-b", "main", "."], repo, env)
    _run(["git", "annex", "init", "aistorage-it"], repo, env)
    _run(
        [
            "git", "annex", "initremote", "drive",
            "type=rclone", "encryption=none",
            f"rcloneremotename={RCLONE_REMOTE}",
            f"rcloneprefix={prefix}",
            "autoenable=true", "--with-url",
        ],
        repo, env,
    )
    _run(["git", "annex", "config", "--set", "annex.largefiles", "largerthan=100kb"], repo, env)
    _run(["git", "config", "annex.max-git-bundles", str(max_git_bundles)], repo, env)
    _run(["git", "config", "user.email", "integration@example.invalid"], repo, env)
    _run(["git", "config", "user.name", "AiStorage Integration"], repo, env)

    (repo / "README.md").write_text(f"Agora integration prefix: {prefix}\n", encoding="utf-8")
    (repo / "_committer").mkdir(exist_ok=True)
    (repo / "_committer" / "schema_version").write_text("agora/v1\n", encoding="utf-8")
    _run(["git", "add", "README.md", "_committer"], repo, env)
    _run(["git", "commit", "-qm", "init: integration seed"], repo, env)

    for idx, kib in enumerate(annex_object_sizes):
        payload = repo / f"payload-{idx}.bin"
        payload.write_bytes(bytes((i * 7 + idx) % 251 for i in range(kib * 1024)))
        _run(["git", "add", payload.name], repo, env)
        _run(["git", "commit", "-qm", f"add annexed payload {idx}"], repo, env)

    _run(["git", "annex", "copy", "--to", "drive"], repo, env)
    _run(["git", "push", "drive", "main", "git-annex"], repo, env)

    info = _run(["git", "annex", "info", "drive", "--fast"], repo, env)
    uuid = ""
    for line in info.splitlines():
        if line.startswith("uuid:"):
            uuid = line.split(":", 1)[1].strip()
    if not uuid:
        raise RuntimeError("取不到 git-annex remote 的 uuid")

    # 完整 URL：annex:: 只帶 uuid/encryption/type 時 clone 會缺 rclone 參數（1.2 實測）
    url = (
        f"annex::{uuid}?encryption=none&type=rclone"
        f"&rcloneremotename={RCLONE_REMOTE}&rcloneprefix={prefix}"
    )
    main_sha = _run(["git", "rev-parse", "refs/heads/main"], repo, env).strip()
    annex_sha = _run(["git", "rev-parse", "refs/heads/git-annex"], repo, env).strip()
    return AnnexSetup(
        prefix=prefix, uuid=uuid, url=url, workdir=repo,
        main_sha=main_sha, annex_sha=annex_sha,
        max_git_bundles=max_git_bundles, env=env,
    )


def annex_git_factory(annex: AnnexSetup):
    """提交流程用的 git_factory：每次呼叫都真的把真本 clone 進指定目錄。

    `init_pin_cli` 會直接對 factory 產生的物件呼叫 `ls_remote` / `local_refs`，
    所以 factory 必須給一個「已經存在且已 clone」的目錄，不能只是包一層 workdir。
    """
    from aistorage.annex.git import SubprocessAnnexGit

    def factory(dest: Path) -> SubprocessAnnexGit:
        return SubprocessAnnexGit.clone_for_commit(
            annex.url, dest, max_git_bundles=annex.max_git_bundles
        )

    return factory


def main_sha_of(annex: AnnexSetup, rclone_conf: Path) -> str:
    """讀真本在 Drive 上的 refs/heads/main（不 clone，很便宜）。

    刻意用 seed repo（暫存目錄裡、annex remote 已經配好）當 cwd：
    `git ls-remote annex::…` 必須在一個 git repo 裡跑，git-annex 的 remote helper
    才會運作；而整合測試裡任何 git 指令都不該以專案 repo 當 cwd。
    """
    out = _run(
        ["git", "ls-remote", "drive", "refs/heads/main"], annex.workdir, git_env(rclone_conf)
    )
    return out.split()[0]


def files_changed_between(
    url: str, old_sha: str, new_sha: str, tmp_path: Path, rclone_conf: Path, seed_repo: Path
) -> list[str]:
    """真的 clone 一份，回報兩個 commit 之間被改動的檔案（確認某一輪沒動到 Session 內容）。"""
    import subprocess

    env = git_env(rclone_conf)
    dest = tmp_path / f"inspect-{new_sha[:8]}"
    subprocess.run(
        ["git", "clone", "-b", "main", url, str(dest)],
        check=True, capture_output=True, text=True, env=env, cwd=str(seed_repo),
    )
    out = subprocess.run(
        ["git", "diff", "--name-only", old_sha, new_sha],
        cwd=str(dest), check=True, capture_output=True, text=True, env=env,
    )
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def is_ancestor(
    url: str, older_sha: str, newer_sha: str, tmp_path: Path, rclone_conf: Path, seed_repo: Path
) -> bool:
    """確認 older 是 newer 的祖先（恢復不得改寫歷史，只能往前接）。"""
    import subprocess

    env = git_env(rclone_conf)
    dest = tmp_path / f"anc-{newer_sha[:8]}"
    subprocess.run(
        ["git", "clone", "-b", "main", url, str(dest)],
        check=True, capture_output=True, text=True, env=env, cwd=str(seed_repo),
    )
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older_sha, newer_sha],
        cwd=str(dest), capture_output=True, text=True, env=env,
    )
    return proc.returncode == 0


# ------------------------------------------------------------------ 收件匣


@dataclass
class Signer:
    """測試用的簽章金鑰（Ed25519）與身分登錄檔。

    登錄檔照 `schemas/identity-registry.schema.json` 的真實形狀（不是簡化版），
    這樣整合測試走的是與正式環境同一條 `load_registry` 路徑。
    """

    profile: str
    key_id: str
    private_key: bytes  #: Ed25519 raw 32 bytes（簽章走 raw bytes，不留 key 物件）
    public_key_b64: str
    registry_path: Path

    def registry_payload(self, inbox_folder_id: str) -> dict:
        """身分登錄檔內容（只放公開資訊；私鑰永遠不進 Drive）。"""
        return {
            "format": "aistorage.identity/v1",
            "profiles": {
                self.profile: {
                    "allowed_types": [
                        "session", "handoff", "claim", "reference", "rewrite", "artifact",
                    ],
                    "signing_keys": [
                        {
                            "key_id": self.key_id,
                            "public_key": self.public_key_b64,
                            "status": "active",
                            "added_at": "2026-09-27T00:00:00Z",
                            "revoked_at": None,
                        }
                    ],
                    "inbox_folder_ids": [inbox_folder_id],
                }
            },
        }


def make_signer(tmp_path: Path, profile: str = "mac-opencode") -> Signer:
    """產生測試簽章金鑰與身分登錄檔路徑（私鑰只留在本機檔案）。"""
    import base64
    import hashlib

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv = ed25519.Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return Signer(
        profile=profile,
        key_id=f"{profile}-{hashlib.sha256(pub_raw).hexdigest()[:8]}",
        private_key=priv_raw,
        public_key_b64=base64.b64encode(pub_raw).decode(),
        registry_path=tmp_path / f"identity-{profile}.json",
    )


def write_registry(signer: Signer, inbox_folder_id: str) -> Path:
    """把身分登錄檔寫到磁碟（`identity_registry_path` 指向的位置）。"""
    signer.registry_path.write_text(
        json.dumps(signer.registry_payload(inbox_folder_id), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return signer.registry_path


def put_inbox_item(
    drive: DriveClient,
    inbox_folder_id: str,
    signer: Signer,
    *,
    raw_bytes: bytes,
    source: str,
    source_session_id: str,
    sidecar_overrides: dict[str, Any] | None = None,
) -> str:
    """組出並上傳一個真的、簽過章的收件匣項目，回傳 item_key。

    直接複用 `build_inbox_item`（3.11 的共用函式），所以上傳到收件匣的形狀與
    手動匯入／同步器產生的完全相同。
    """
    import tempfile

    from aistorage.converters.base import SessionFacts
    from aistorage.inbox_builder import build_inbox_item, serialize_json

    with tempfile.TemporaryDirectory(prefix="aistorage_it_item_") as td:
        raw_path = Path(td) / "raw"
        raw_path.write_bytes(raw_bytes)
        facts = SessionFacts(
            title="integration",
            created_at="2026-09-27T08:00:00Z",
            updated_at="2026-09-27T09:00:00Z",
            message_ids=("m1", "m2"),
            archived_at=None,
            last_message_at="2026-09-27T09:00:00Z",
            in_progress=False,
        )
        sidecar_bytes, sig = build_inbox_item(
            raw_path,
            source=source,
            source_session_id=source_session_id,
            facts=facts,
            profile=signer.profile,
            key=signer.private_key,
            key_id=signer.key_id,
            item_key=generate_ulid(),
        )
    if sidecar_overrides:
        # 只給測試用的覆寫（例如把 snapshot_at 設成特定值）
        sidecar = json.loads(sidecar_bytes.decode("utf-8"))
        for dotted, value in sidecar_overrides.items():
            node = sidecar
            parts = dotted.split(".")
            for key in parts[:-1]:
                node = node[key]
            node[parts[-1]] = value
        from aistorage.inbox import sign_sidecar_bytes

        sidecar_bytes = serialize_json(sidecar)
        sig = sign_sidecar_bytes(sidecar_bytes, signer.private_key, signer.key_id)

    item_key = json.loads(sidecar_bytes.decode("utf-8"))["item_key"]
    # D2 的不可分單位順序：raw → sidecar → sig
    uploads = (
        (f"{item_key}.raw", raw_bytes, "application/octet-stream"),
        (f"{item_key}.sidecar.json", sidecar_bytes, "application/json"),
        (f"{item_key}.sig", serialize_json(sig), "application/json"),
    )
    for name, content, mime in uploads:
        drive.create(inbox_folder_id, name, content, mime_type=mime)
    return item_key


def require_drive() -> None:
    """明確提醒：整合測試只以路徑引用憑證。"""
    print(
        "整合測試使用 ~/.config/aistorage/rclone-committer-test.conf 的憑證（只以路徑引用）",
        file=sys.stderr,
    )


# ------------------------------------------------- 整合測試共用的 Session 資料


#: 用單元測試的 opencode 黃金樣本當原始紀錄（形狀真實、內容是測試資料）
GOLDEN = Path(__file__).resolve().parents[1] / "unit" / "data" / "converters" / "opencode" / "basic.json"

#: 共用的兩個 Session（黃金樣本改 id 而來，形狀真實、內容是測試資料）
S1 = "ses_opencode_basic_001"
S2 = "ses_opencode_basic_002"


def _raw_for(session_id: str) -> bytes:
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    data["info"]["id"] = session_id
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _reading_for(raw: bytes, session_id: str) -> dict:
    """真的跑一次轉換器，讓接續點的 snapshot_sha256 與訊息 id 來自真實輸出。"""
    import tempfile

    from aistorage.converters import get_converter

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "raw"
        p.write_bytes(raw)
        return get_converter("opencode").convert(p, session_id=session_id)


RAW_S1 = _raw_for(S1)
RAW_S2 = _raw_for(S2)


def _put_session(drive, inbox, signer, raw: bytes, session_id: str, snapshot_at: str) -> str:
    return put_inbox_item(
        drive, inbox, signer,
        raw_bytes=raw,
        source="opencode",
        source_session_id=session_id,
        sidecar_overrides={
            "session.snapshot_at": snapshot_at,
            "metadata.updated_at": snapshot_at,
        },
    )


def _put_handoff_and_claim(
    drive, inbox, signer, *,
    target_session_id: str,
    claimer_session_id: str,
    target_raw_sha: str,
    last_message_id: str,
) -> tuple[str, str]:
    """放一個 handoff ＋ 一個 claim（claim 指向 handoff，claimer 是 S2）。"""
    import tempfile

    from aistorage.inbox import sign_sidecar_bytes, validate_sidecar
    from aistorage.inbox_builder import serialize_json
    from aistorage.schema import generate_ulid

    handoff_ulid = generate_ulid()
    claim_ulid = generate_ulid()

    def upload(sidecar: dict) -> None:
        sidecar["item_key"] = generate_ulid()
        raw = json.dumps(sidecar, ensure_ascii=False, sort_keys=True).encode("utf-8")
        errors = validate_sidecar(sidecar, expected_item_key=sidecar["item_key"])
        assert errors == [], f"測試產出的 sidecar 不合法: {errors}"
        sig = sign_sidecar_bytes(raw, signer.private_key, signer.key_id)
        drive.create(inbox, f"{sidecar['item_key']}.sidecar.json", raw, mime_type="application/json")
        drive.create(
            inbox, f"{sidecar['item_key']}.sig", serialize_json(sig), mime_type="application/json"
        )

    base_meta = {
        "created_at": "2026-09-27T09:00:00Z",
        "updated_at": "2026-09-27T09:00:00Z",
        "case_id": None,
        "provenance": None,
    }
    upload({
        "format": "aistorage.inbox/v1",
        "profile": signer.profile,
        "metadata": {**base_meta, "id": f"handoff:{handoff_ulid}", "type": "handoff"},
        "raw": None,
        "body": {
            "target_session_id": target_session_id,
            "continuation": {"snapshot_sha256": target_raw_sha, "message_id": last_message_id},
            "content": "接手後續調查",
        },
    })
    upload({
        "format": "aistorage.inbox/v1",
        "profile": signer.profile,
        "metadata": {**base_meta, "id": f"claim:{claim_ulid}", "type": "claim"},
        "raw": None,
        "body": {
            "handoff_id": f"handoff:{handoff_ulid}",
            "claimer_session_id": f"opencode:{S2}",
        },
    })
    return handoff_ulid, claim_ulid

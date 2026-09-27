"""AiStorage 信任錨點釘選值與 PinStore 儲存模組。

依據規格：
- docs/impl/group3-modules.md 第 3.2 節
- design.md D2（兩階段釘選、獨立 pin repo、單一 job）
- ADR 0008（以釘選保證真本，不靠 Drive 權限禁止）
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Protocol, runtime_checkable

from aistorage.annex.git import get_git_env
from aistorage.errors import ReadError, WriteError


@dataclass(frozen=True)
class PinState:
    """正式釘選值（代表真本 repo 當前可信基準）。"""

    repo: str                         # 例如 "agora"
    repo_uuid: str                    # git-annex remote UUID
    refs: dict[str, str]              # 全部 ref (clean ref -> sha)
    manifest_sha256: str              # 當前主 GITMANIFEST 內容雜湊
    prev_manifest_sha256: str | None  # 上一版主 GITMANIFEST 內容雜湊（.bak 合法值之一）
    active_bundles: tuple[str, ...]   # 當前有效 bundle 清單（依順序）
    removed_bundles: frozenset[str]   # 已被 consolidate 取代之 bundle 集合
    annex_keys: frozenset[str]        # 當前 git-annex 分支記錄之 annex key 集合
    promoted_at: str                  # 轉正時間 (ISO 8601)
    run_id: str                       # 轉正之 workflow run id


@dataclass(frozen=True)
class PinPending:
    """待定釘選值（在 push 前記錄即將推入之目標狀態）。"""

    repo: str
    base_manifest_sha256: str         # 寫待定時的正式 manifest 雜湊（基準檢驗）
    refs: dict[str, str]              # 即將 push 之 refs
    annex_keys: frozenset[str]        # push 之後 remote 將具備之 key 集合
    written_at: str                   # 寫入時間 (ISO 8601)
    run_id: str                       # 當前 workflow run id


@runtime_checkable
class PinStore(Protocol):
    """釘選值儲存庫協定。"""

    def load(self, repo: str) -> tuple[PinState, PinPending | None]:
        """讀取指定 repo 之正式與待定釘選值。若正式釘選值讀不到則拋出 ReadError。"""
        ...

    def write_pending(self, pending: PinPending) -> None:
        """寫入待定釘選值。"""
        ...

    def promote(self, state: PinState) -> None:
        """將釘選值轉為正式並刪除待定。"""
        ...

    def drop_pending(self, repo: str) -> None:
        """丟棄待定釘選值。"""
        ...


class MemoryPinStore(PinStore):
    """記憶體釘選值儲存實作（供單元測試與純函式流程使用）。"""

    def __init__(
        self,
        initial_state: PinState | None = None,
        initial_pending: PinPending | None = None,
    ) -> None:
        self._states: dict[str, PinState] = {}
        self._pendings: dict[str, PinPending] = {}
        if initial_state:
            self._states[initial_state.repo] = initial_state
        if initial_pending:
            self._pendings[initial_pending.repo] = initial_pending

    def load(self, repo: str) -> tuple[PinState, PinPending | None]:
        if repo not in self._states:
            raise ReadError(f"找不到正式釘選值: {repo}")
        return self._states[repo], self._pendings.get(repo)

    def write_pending(self, pending: PinPending) -> None:
        self._pendings[pending.repo] = pending

    def promote(self, state: PinState) -> None:
        self._states[state.repo] = state
        self._pendings.pop(state.repo, None)

    def drop_pending(self, repo: str) -> None:
        self._pendings.pop(repo, None)


class GitPinStore(PinStore):
    """透過獨立 Git 儲存庫（pin repo）管理釘選值之實作。

    安全與架構規則（design D2 & ADR 0008）：
    - 釘選值置於獨立的 pin repo（僅包含 .pin/ 目錄，無 workflow）。
    - 提交流程使用專屬 deploy key 操作，main repo 維持 contents: read。
    - 每次寫入：確保同步最新遠端 HEAD，commit 後 push；若發生 non-fast-forward 則嚴格中止。
    """

    def __init__(
        self,
        repo_url: str,
        workdir: Path | str,
        *,
        key_path: Path | str | None = None,
        known_hosts_path: Path | str | None = None,
        user_name: str = "AiStorage Committer",
        user_email: str = "committer@aistorage.local",
    ) -> None:
        self.repo_url = repo_url
        self.workdir = Path(workdir).resolve()
        self.key_path = Path(key_path).resolve() if key_path else None
        self.known_hosts_path = Path(known_hosts_path).resolve() if known_hosts_path else None
        self.user_name = user_name
        self.user_email = user_email

        self._env = get_git_env()
        if self.key_path and self.known_hosts_path:
            ssh_cmd = (
                f"ssh -i {self.key_path} -o IdentitiesOnly=yes "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={self.known_hosts_path}"
            )
            self._env["GIT_SSH_COMMAND"] = ssh_cmd

    def _run_git(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = ["git"] + args
        proc = subprocess.run(
            cmd,
            cwd=str(self.workdir) if (self.workdir / ".git").exists() else None,
            capture_output=True,
            text=True,
            env=self._env,
            timeout=60.0,
            check=False,
        )
        if check and proc.returncode != 0:
            raise WriteError(f"GitPinStore 指令失敗: {' '.join(cmd)}\nstderr: {proc.stderr}")
        return proc

    def _ensure_cloned_and_updated(self) -> None:
        self.workdir.parent.mkdir(parents=True, exist_ok=True)
        git_dir = self.workdir / ".git"
        if not git_dir.exists():
            proc = subprocess.run(
                ["git", "clone", self.repo_url, str(self.workdir)],
                capture_output=True,
                text=True,
                env=self._env,
                timeout=60.0,
                check=False,
            )
            if proc.returncode != 0:
                raise ReadError(f"無法複製 pin repo ({self.repo_url}): {proc.stderr}")

            # 設定 committer 身分
            self._run_git(["config", "user.name", self.user_name])
            self._run_git(["config", "user.email", self.user_email])
        else:
            # fetch 並對齊 origin/main
            proc = self._run_git(["fetch", "origin"], check=False)
            if proc.returncode != 0:
                raise ReadError(f"pin repo fetch 失敗: {proc.stderr}")

            # 嘗試切換至 main 並 reset
            self._run_git(["checkout", "-B", "main", "origin/main"], check=False)

    def load(self, repo: str) -> tuple[PinState, PinPending | None]:
        self._ensure_cloned_and_updated()
        pin_dir = self.workdir / ".pin"
        state_file = pin_dir / f"{repo}.json"
        if not state_file.is_file():
            raise ReadError(f"找不到正式釘選值檔案: {state_file}")

        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise ReadError(f"解析正式釘選值 JSON 失敗: {e}") from None

        keys_file = pin_dir / f"{repo}.keys"
        annex_keys: frozenset[str] = frozenset()
        if keys_file.is_file():
            annex_keys = frozenset(
                line.strip()
                for line in keys_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )

        state = PinState(
            repo=data["repo"],
            repo_uuid=data["repo_uuid"],
            refs=dict(data.get("refs", {})),
            manifest_sha256=data["manifest_sha256"],
            prev_manifest_sha256=data.get("prev_manifest_sha256"),
            active_bundles=tuple(data.get("active_bundles", [])),
            removed_bundles=frozenset(data.get("removed_bundles", [])),
            annex_keys=annex_keys,
            promoted_at=data["promoted_at"],
            run_id=data["run_id"],
        )

        # 檢查是否存在 pending
        pending_file = pin_dir / f"{repo}.pending.json"
        pending: PinPending | None = None
        if pending_file.is_file():
            try:
                pdata = json.loads(pending_file.read_text(encoding="utf-8"))
                pkeys_file = pin_dir / f"{repo}.pending.keys"
                pending_keys: frozenset[str] = frozenset()
                if pkeys_file.is_file():
                    pending_keys = frozenset(
                        line.strip()
                        for line in pkeys_file.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                pending = PinPending(
                    repo=pdata["repo"],
                    base_manifest_sha256=pdata["base_manifest_sha256"],
                    refs=dict(pdata.get("refs", {})),
                    annex_keys=pending_keys,
                    written_at=pdata["written_at"],
                    run_id=pdata["run_id"],
                )
            except Exception:
                pending = None

        return state, pending

    def _commit_and_push(self, commit_msg: str) -> None:
        self._run_git(["add", ".pin/"])
        status = self._run_git(["status", "--porcelain"])
        if not status.stdout.strip():
            # 無變更，直接返回
            return

        self._run_git(["commit", "-m", commit_msg])
        proc = self._run_git(["push", "origin", "main"], check=False)
        if proc.returncode != 0:
            raise WriteError(f"pin repo push 失敗 (可能遭遇衝突或 non-fast-forward): {proc.stderr}")

    def write_pending(self, pending: PinPending) -> None:
        self._ensure_cloned_and_updated()
        pin_dir = self.workdir / ".pin"
        pin_dir.mkdir(parents=True, exist_ok=True)

        pdata = {
            "repo": pending.repo,
            "base_manifest_sha256": pending.base_manifest_sha256,
            "refs": {k: pending.refs[k] for k in sorted(pending.refs)},
            "written_at": pending.written_at,
            "run_id": pending.run_id,
        }
        (pin_dir / f"{pending.repo}.pending.json").write_text(
            json.dumps(pdata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        keys_content = "\n".join(sorted(pending.annex_keys))
        if keys_content:
            keys_content += "\n"
        (pin_dir / f"{pending.repo}.pending.keys").write_text(keys_content, encoding="utf-8")

        self._commit_and_push(f"pin({pending.repo}): write pending for run {pending.run_id}")

    def promote(self, state: PinState) -> None:
        self._ensure_cloned_and_updated()
        pin_dir = self.workdir / ".pin"
        pin_dir.mkdir(parents=True, exist_ok=True)

        sdata = {
            "repo": state.repo,
            "repo_uuid": state.repo_uuid,
            "refs": {k: state.refs[k] for k in sorted(state.refs)},
            "manifest_sha256": state.manifest_sha256,
            "prev_manifest_sha256": state.prev_manifest_sha256,
            "active_bundles": list(state.active_bundles),
            "removed_bundles": sorted(state.removed_bundles),
            "promoted_at": state.promoted_at,
            "run_id": state.run_id,
        }
        (pin_dir / f"{state.repo}.json").write_text(
            json.dumps(sdata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        keys_content = "\n".join(sorted(state.annex_keys))
        if keys_content:
            keys_content += "\n"
        (pin_dir / f"{state.repo}.keys").write_text(keys_content, encoding="utf-8")

        # 移除 pending 檔案
        p_json = pin_dir / f"{state.repo}.pending.json"
        p_keys = pin_dir / f"{state.repo}.pending.keys"
        if p_json.exists():
            p_json.unlink()
        if p_keys.exists():
            p_keys.unlink()

        self._commit_and_push(f"pin({state.repo}): promote for run {state.run_id}")

    def drop_pending(self, repo: str) -> None:
        self._ensure_cloned_and_updated()
        pin_dir = self.workdir / ".pin"
        p_json = pin_dir / f"{repo}.pending.json"
        p_keys = pin_dir / f"{repo}.pending.keys"

        changed = False
        if p_json.exists():
            p_json.unlink()
            changed = True
        if p_keys.exists():
            p_keys.unlink()
            changed = True

        if changed:
            self._commit_and_push(f"pin({repo}): drop pending")

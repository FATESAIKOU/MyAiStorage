"""AiStorage 信任錨點釘選值與 PinStore 儲存模組。

依據規格：
- docs/impl/group3-modules.md 第 3.2 節
- design.md D2（兩階段釘選、獨立 pin repo、單一 job）
- ADR 0008（以釘選保證真本，不靠 Drive 權限禁止）
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
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

    def read_text(self, relpath: str) -> str | None:
        """讀取 pin repo 內的文字檔；不存在回傳 None。

        第 6 組的維護旗標（`admin.lock.read_maintenance`）靠這個方法判斷
        管理操作進行中；提交流程的唯讀 deploy key 讀得到這個檔案。
        """
        ...


class MemoryPinStore(PinStore):
    """記憶體釘選值儲存實作（供單元測試與純函式流程使用）。"""

    def __init__(
        self,
        initial_state: PinState | None = None,
        initial_pending: PinPending | None = None,
        *,
        texts: dict[str, str] | None = None,
        initial_states_extra: dict[str, PinState] | None = None,
    ) -> None:
        self._states: dict[str, PinState] = {}
        self._pendings: dict[str, PinPending] = {}
        self._texts: dict[str, str] = dict(texts or {})
        # 多 repo（Agora／Foundry 各一個釘選值條目，group5-7 第 6.1 節）
        for name, st in (initial_states_extra or {}).items():
            self._states[name] = st
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

    def read_text(self, relpath: str) -> str | None:
        return self._texts.get(relpath)


_ALLOWED_REMOTE_GITHUB_SSH = re.compile(
    r"^(?:git@github\.com:[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:\.git)?|ssh://git@github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(?:\.git)?)$"
)


def _is_local_path_or_file_url(url: str) -> bool:
    if url.startswith("file://"):
        return True
    if ":" not in url:
        if url.startswith(("/", "./", "../")):
            return True
        p = Path(url)
        if p.is_absolute() or p.exists():
            return True
    p = Path(url)
    if p.is_absolute() or p.exists():
        return True
    return False


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
        allow_production: bool = False,
    ) -> None:
        from aistorage.safety import assert_safe_workdir

        self.repo_url = repo_url
        # M8：pin repo 的 clone 目錄同樣不得位於專案 repo 之內。pin repo 會被
        # commit＋push，寫錯地方等於在專案裡製造無關的歷史。
        self.workdir = assert_safe_workdir(workdir, purpose="pin repo 工作目錄")
        self.key_path = Path(key_path).resolve() if key_path else None
        self.known_hosts_path = Path(known_hosts_path).resolve() if known_hosts_path else None
        self.user_name = user_name
        self.user_email = user_email

        # R2: URL 白名單驗證（僅接受本機路徑/file:// 或 GitHub SSH: git@github.com:... / ssh://git@github.com/...）
        if not _is_local_path_or_file_url(self.repo_url) and not _ALLOWED_REMOTE_GITHUB_SSH.match(self.repo_url):
            raise ValueError(
                f"不允許的 pin repo URL 形式: {repr(self.repo_url)}（僅接受本機路徑、file:// 或 GitHub SSH: git@github.com:... / ssh://git@github.com/...）"
            )

        # R2: 正式 pin repo（MyAiStorage-pin）非 CI 環境禁止寫入
        if re.search(r"/MyAiStorage-pin(?:\.git)?$", self.repo_url):
            if os.environ.get("GITHUB_ACTIONS") != "true" and not allow_production:
                raise PermissionError(
                    "非 CI 環境 (GITHUB_ACTIONS != 'true') 禁止寫入正式 pin repo (MyAiStorage-pin)！本機測試請使用 MyAiStorage-pin-test。"
                )

        self._env = get_git_env()

        # R2: 一律設定 GIT_SSH_COMMAND 隔離個人身分，防止退回 ~/.ssh/config 或 ssh-agent
        if self.key_path and self.known_hosts_path:
            ssh_cmd = (
                f"ssh -F /dev/null -i {self.key_path} -o IdentitiesOnly=yes "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={self.known_hosts_path} "
                f"-o IdentityAgent=none"
            )
            self._env["GIT_SSH_COMMAND"] = ssh_cmd
        else:
            self._env["GIT_SSH_COMMAND"] = "ssh -F /dev/null -o IdentityAgent=none -o IdentitiesOnly=yes"

        # 若存取遠端 GitHub SSH 時，強制要求 key_path 與 known_hosts_path 且必須存在
        if _ALLOWED_REMOTE_GITHUB_SSH.match(self.repo_url):
            if not self.key_path or not self.known_hosts_path:
                raise ValueError("使用 SSH 存取遠端 pin repo 時，key_path 與 known_hosts_path 為必填 (M2)")
            if not self.key_path.is_file():
                raise ValueError(f"找不到 SSH deploy key 檔案: {self.key_path}")
            if not self.known_hosts_path.is_file():
                raise ValueError(f"找不到 known_hosts 檔案: {self.known_hosts_path}")

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
            cmd_name = f"{cmd[0]} {cmd[1]}" if len(cmd) > 1 else cmd[0]
            raise WriteError(f"GitPinStore 指令 '{cmd_name}' 失敗 (rc={proc.returncode})")
        return proc

    def _ensure_cloned(self) -> None:
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
                raise ReadError(f"無法複製 pin repo (rc={proc.returncode})")

            # 設定 committer 身分
            self._run_git(["config", "user.name", self.user_name])
            self._run_git(["config", "user.email", self.user_email])

    def _ensure_cloned_and_updated(self) -> None:
        self._ensure_cloned()
        # M1: fetch 並 hard reset 清理乾淨，防殘留本地未提交或未推送的變更
        proc_fetch = self._run_git(["fetch", "origin"], check=False)
        if proc_fetch.returncode != 0:
            raise ReadError(f"pin repo fetch 失敗 (rc={proc_fetch.returncode})")

        has_origin_main = self._run_git(["rev-parse", "--verify", "origin/main"], check=False).returncode == 0
        if has_origin_main:
            self._run_git(["reset", "--hard", "origin/main"])
        self._run_git(["clean", "-fdx"])

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

        if not isinstance(data, dict):
            raise ReadError("正式釘選值 JSON 必須是物件 (dict)")

        # L & H2: 檢查 repo 欄位相符
        if data.get("repo") != repo:
            raise ReadError(f"正式釘選值 repo 欄位 ('{data.get('repo')}') 與查詢目標 ('{repo}') 不符")

        required_state_keys = {
            "repo", "repo_uuid", "refs", "manifest_sha256",
            "active_bundles", "removed_bundles", "promoted_at", "run_id",
            "annex_keys_count", "annex_keys_sha256",
        }
        missing_state_keys = required_state_keys - set(data.keys())
        if missing_state_keys:
            raise ReadError(f"正式釘選值缺少必填欄位: {missing_state_keys}")

        if not isinstance(data["refs"], dict) or not isinstance(data["active_bundles"], list) or not isinstance(data["removed_bundles"], list):
            raise ReadError("正式釘選值欄位型別不符")

        # H2: keys 檔必須存在
        keys_file = pin_dir / f"{repo}.keys"
        if not keys_file.is_file():
            raise ReadError(f"缺少必填之 keys 檔案: {keys_file}")

        keys_bytes = keys_file.read_bytes()
        annex_keys = frozenset(
            line.strip()
            for line in keys_bytes.decode("utf-8").splitlines()
            if line.strip()
        )
        if len(annex_keys) != data["annex_keys_count"]:
            raise ReadError(f"annex_keys 數量 ({len(annex_keys)}) 與宣告 ({data['annex_keys_count']}) 不符")
        calc_keys_sha = hashlib.sha256(keys_bytes).hexdigest().lower()
        if calc_keys_sha != data["annex_keys_sha256"]:
            raise ReadError(f"keys 檔案雜湊 ({calc_keys_sha}) 與宣告 ({data['annex_keys_sha256']}) 不符")

        state = PinState(
            repo=data["repo"],
            repo_uuid=data["repo_uuid"],
            refs=dict(data["refs"]),
            manifest_sha256=data["manifest_sha256"],
            prev_manifest_sha256=data.get("prev_manifest_sha256"),
            active_bundles=tuple(data["active_bundles"]),
            removed_bundles=frozenset(data["removed_bundles"]),
            annex_keys=annex_keys,
            promoted_at=data["promoted_at"],
            run_id=data["run_id"],
        )

        # H1 & H2: 檢查是否存在 pending（檔案存在但損毀時一律 raise ReadError）
        pending_file = pin_dir / f"{repo}.pending.json"
        pending: PinPending | None = None
        if pending_file.is_file():
            pkeys_file = pin_dir / f"{repo}.pending.keys"
            if not pkeys_file.is_file():
                raise ReadError(f"存在 pending.json 但缺少對應之 pending.keys: {pkeys_file}")

            try:
                pdata = json.loads(pending_file.read_text(encoding="utf-8"))
            except Exception as e:
                raise ReadError(f"解析待定釘選值 pending.json 失敗: {e}") from e

            if not isinstance(pdata, dict):
                raise ReadError("待定釘選值 pending.json 必須是物件 (dict)")

            if pdata.get("repo") != repo:
                raise ReadError(f"待定釘選值 repo 欄位 ('{pdata.get('repo')}') 與查詢目標 ('{repo}') 不符")

            required_pending_keys = {"repo", "base_manifest_sha256", "refs", "written_at", "run_id"}
            missing_pending_keys = required_pending_keys - set(pdata.keys())
            if missing_pending_keys:
                raise ReadError(f"待定釘選值缺少必填欄位: {missing_pending_keys}")

            if not isinstance(pdata["refs"], dict):
                raise ReadError("待定釘選值 refs 必須是字典 (dict)")

            pkeys_bytes = pkeys_file.read_bytes()
            pending_keys = frozenset(
                line.strip()
                for line in pkeys_bytes.decode("utf-8").splitlines()
                if line.strip()
            )
            if "annex_keys_count" in pdata and len(pending_keys) != pdata["annex_keys_count"]:
                raise ReadError(f"pending annex_keys 數量 ({len(pending_keys)}) 與宣告 ({pdata['annex_keys_count']}) 不符")
            if "annex_keys_sha256" in pdata:
                calc_pkeys_sha = hashlib.sha256(pkeys_bytes).hexdigest().lower()
                if calc_pkeys_sha != pdata["annex_keys_sha256"]:
                    raise ReadError(f"pending.keys 檔案雜湊 ({calc_pkeys_sha}) 與宣告 ({pdata['annex_keys_sha256']}) 不符")

            pending = PinPending(
                repo=pdata["repo"],
                base_manifest_sha256=pdata["base_manifest_sha256"],
                refs=dict(pdata["refs"]),
                annex_keys=pending_keys,
                written_at=pdata["written_at"],
                run_id=pdata["run_id"],
            )

        return state, pending

    def _fetch(self) -> None:
        """fetch 遠端 main；失敗一律 raise（review-25a48a9 L）。

        原本 `check=False` 且不回頭看回傳碼：fetch 失敗之後 `_incoming_paths()`
        拿舊的 `origin/main` 算，會得到空清單，最後以「無法確認遠端變更只屬於
        其他 repo」中止——方向是 fail-closed，但訊息會指向錯的原因。fetch 失敗就
        直接說 fetch 失敗。
        """
        proc = self._run_git(["fetch", "origin", "main"], check=False)
        if proc.returncode != 0:
            raise ReadError(f"pin repo fetch 失敗 (rc={proc.returncode})")

    def _incoming_paths(self) -> list[str]:
        """遠端比本地多出的檔案（`HEAD..origin/main`），相對於 repo 根。"""
        proc = self._run_git(
            ["diff", "--name-only", "HEAD...origin/main"], check=False
        )
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _commit_and_push(self, commit_msg: str, *, repo: str) -> None:
        self._run_git(["add", ".pin/"])
        status = self._run_git(["status", "--porcelain"])
        if not status.stdout.strip():
            # 無變更，直接返回
            return

        self._run_git(["commit", "-m", commit_msg])
        proc = self._run_git(["push", "origin", "main"], check=False)
        if proc.returncode == 0:
            return

        # 遠端在我們 fetch 之後又往前走了（多個工作流／多條線共用同一個 pin repo 時
        # 會發生）。允許自動 rebase 的**唯一**情況是：遠端新增的檔案全部屬於
        # **別的 repo** 的條目（review-b1039a8 M1）。只要同一個 repo 的任何檔案
        # ——包含 `.maintenance`——被動過，就中止不推，保留現況給人工看。
        self._fetch()
        incoming = self._incoming_paths()
        repo_stem = repo
        foreign = [p for p in incoming if not p.startswith(f".pin/{repo_stem}.")]
        same_repo = [p for p in incoming if p.startswith(f".pin/{repo_stem}.")]
        if same_repo:
            raise WriteError(
                f"pin repo 同一個 repo ({repo_stem}) 的檔案在遠端已被改動，"
                f"中止不推（人工確認後再處理）: {sorted(same_repo)}"
            )
        if not foreign:
            raise WriteError(
                "pin repo push 失敗且無法確認遠端變更只屬於其他 repo，保留現況待人工處理"
            )
        pull = self._run_git(["pull", "--rebase", "origin", "main"], check=False)
        if pull.returncode != 0:
            raise WriteError(
                f"pin repo push 失敗且 rebase 衝突，保留現況待人工處理 (rc={pull.returncode})"
            )
        retry = self._run_git(["push", "origin", "main"], check=False)
        if retry.returncode != 0:
            raise WriteError(
                f"pin repo push 失敗 (可能遭遇衝突或 non-fast-forward, rc={retry.returncode})"
            )

    def write_pending(self, pending: PinPending) -> None:
        self._ensure_cloned()
        pin_dir = self.workdir / ".pin"
        pin_dir.mkdir(parents=True, exist_ok=True)

        keys_content = "\n".join(sorted(pending.annex_keys))
        if keys_content:
            keys_content += "\n"
        keys_bytes = keys_content.encode("utf-8")
        (pin_dir / f"{pending.repo}.pending.keys").write_bytes(keys_bytes)

        pdata = {
            "repo": pending.repo,
            "base_manifest_sha256": pending.base_manifest_sha256,
            "refs": {k: pending.refs[k] for k in sorted(pending.refs)},
            "written_at": pending.written_at,
            "run_id": pending.run_id,
            "annex_keys_count": len(pending.annex_keys),
            "annex_keys_sha256": hashlib.sha256(keys_bytes).hexdigest().lower(),
        }
        (pin_dir / f"{pending.repo}.pending.json").write_text(
            json.dumps(pdata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        self._commit_and_push(
            f"pin({pending.repo}): write pending for run {pending.run_id}", repo=pending.repo
        )

    def promote(self, state: PinState) -> None:
        self._ensure_cloned()
        pin_dir = self.workdir / ".pin"
        pin_dir.mkdir(parents=True, exist_ok=True)

        keys_content = "\n".join(sorted(state.annex_keys))
        if keys_content:
            keys_content += "\n"
        keys_bytes = keys_content.encode("utf-8")
        (pin_dir / f"{state.repo}.keys").write_bytes(keys_bytes)

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
            "annex_keys_count": len(state.annex_keys),
            "annex_keys_sha256": hashlib.sha256(keys_bytes).hexdigest().lower(),
        }
        (pin_dir / f"{state.repo}.json").write_text(
            json.dumps(sdata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # 移除 pending 檔案
        p_json = pin_dir / f"{state.repo}.pending.json"
        p_keys = pin_dir / f"{state.repo}.pending.keys"
        if p_json.exists():
            p_json.unlink()
        if p_keys.exists():
            p_keys.unlink()

        self._commit_and_push(
            f"pin({state.repo}): promote for run {state.run_id}", repo=state.repo
        )

    def read_text(self, relpath: str) -> str | None:
        """讀 pin repo 裡的檔案（唯讀）。

        **只有檔案確實不存在才回 None**（review-b1039a8 H2）：clone 或讀取失敗
        一律 raise `ReadError`。吞掉失敗會讓提交流程把「讀不到維護旗標」誤判成
        「沒有維護中」，於是在管理操作進行中照常 push——正是 H4 要防的情況。
        """
        self._ensure_cloned()
        path = (self.workdir / relpath).resolve()
        # 防呆：只讀 pin repo 內的檔案，路徑逃逸（../）一律拒絕
        if not str(path).startswith(str(self.workdir.resolve()) + "/"):
            raise ReadError(f"拒絕讀取 pin repo 外的路徑: {relpath}")
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ReadError(f"讀取 pin repo 檔案失敗: {type(e).__name__}") from None

    def drop_pending(self, repo: str) -> None:
        self._ensure_cloned()
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
            self._commit_and_push(f"pin({repo}): drop pending", repo=repo)

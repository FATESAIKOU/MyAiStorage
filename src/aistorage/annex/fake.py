"""AiStorage AnnexGit 測試假實作（FakeAnnexGit）。

依據規格：
- docs/impl/group3-modules.md 第 1 節
- review-g3a.md M5（push_effect, inject, annex_keys_in）
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Literal
from datetime import datetime, timezone

from aistorage.errors import ReadError

from aistorage.annex.git import AnnexGit
from aistorage.annex.manifest import normalize_ls_remote
from aistorage.errors import WriteError


def create_fake_git_bundle(
    workdir: Path,
    repo_uuid: str,
    *,
    branch: str = "refs/heads/main",
    content: str = "initial\n",
) -> tuple[str, bytes, str, str]:
    """建立符合規格與 git-remote-annex 命名空間之真實可解開 git bundle。

    回傳：(bundle_name, bundle_bytes, main_commit_sha, annex_commit_sha)
    """
    workdir.mkdir(parents=True, exist_ok=True)
    repo = workdir / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    subprocess.run(["git", "init", "-b", "main", "-q", "."], cwd=repo, env=env, check=True)
    (repo / "f.txt").write_text(content)
    subprocess.run(["git", "add", "f.txt"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, env=env, check=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True, capture_output=True, text=True)
    c_main = out.stdout.strip()

    subprocess.run(["git", "checkout", "-b", "git-annex", "-q"], cwd=repo, env=env, check=True)
    (repo / "uuid.log").write_text(f"{repo_uuid} agora\n")
    subprocess.run(["git", "add", "uuid.log"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-qm", "annex init"], cwd=repo, env=env, check=True)
    out_annex = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True, capture_output=True, text=True)
    c_annex = out_annex.stdout.strip()
    subprocess.run(["git", "checkout", "main", "-q"], cwd=repo, env=env, check=True)

    ns_ref_main = f"refs/namespaces/git-remote-annex/{repo_uuid}/refs/heads/main"
    ns_ref_annex = f"refs/namespaces/git-remote-annex/{repo_uuid}/refs/heads/git-annex"
    subprocess.run(["git", "update-ref", ns_ref_main, c_main], cwd=repo, env=env, check=True)
    subprocess.run(["git", "update-ref", ns_ref_annex, c_annex], cwd=repo, env=env, check=True)
    b_path = workdir / "b1.bundle"
    subprocess.run(["git", "bundle", "create", str(b_path), ns_ref_main, ns_ref_annex], cwd=repo, env=env, check=True)
    raw = b_path.read_bytes()
    name = f"GITBUNDLE-s{len(raw)}--{repo_uuid}-{hashlib.sha256(raw).hexdigest()}"
    return name, raw, c_main, c_annex


class FakeAnnexGit(AnnexGit):
    """記憶體中模擬 AnnexGit 操作。"""

    def __init__(
        self,
        refs: dict[str, str] | None = None,
        workdir: Path | None = None,
        annex_keys: frozenset[str] | set[str] | None = None,
        push_effect: Literal["apply", "silent_fail", "error"] = "apply",
        copy_effect: Literal["noop", "annex_upload"] = "noop",
        local_keys: frozenset[str] | set[str] | None = None,
        *,
        drive: Any | None = None,
        prefix_folder_id: str | None = None,
        repo_uuid: str | None = None,
        repo_url: str | None = None,
        clock: Any | None = None,
    ) -> None:
        self.refs: dict[str, str] = dict(refs or {})
        self.workdir: Path = workdir or Path("/mock/repo")
        self.annex_keys: frozenset[str] = frozenset(annex_keys or ())
        self.push_effect: Literal["apply", "silent_fail", "error"] = push_effect
        # review-g3g H1：`git annex copy` 會寫入本機 git-annex 分支的 location log
        # （refs/heads/git-annex 的 sha 改變），並讓本機的 key 變成「在 remote 上」。
        # copy_effect="annex_upload" 才模擬這個副作用，預設維持 noop 不影響既有測試。
        self.copy_effect: Literal["noop", "annex_upload"] = copy_effect
        self.local_keys: frozenset[str] = frozenset(local_keys or ())
        self.drive: Any | None = drive
        self.prefix_folder_id: str | None = prefix_folder_id
        self.repo_uuid: str | None = repo_uuid
        #: H1：`origin_url()` 回報這個值（clone 到錯的 repo 的測試就是餵不同的值）
        self.repo_url: str = repo_url or f"annex::{repo_uuid or 'fake'}"
        self.clock: Any | None = clock
        self.added_paths: list[str] = []
        #: fake annex 物件庫（lookupkey 會填；register_object 可手動登錄「遠端」物件）
        self._fake_objects: dict[str, bytes] = {}
        self.commits: list[tuple[str, str]] = []
        self.copied: list[tuple[str, list[str] | None]] = []
        self.pushed: list[tuple[str, tuple[str, ...]]] = []
        self.pending_refs: dict[str, str] = {}
        self._injections: dict[str, type[Exception]] = {}
        #: H1：上一次 push 寫出去的 manifest 位元組（`local_manifest_sha256` 的來源）
        self._local_manifest_bytes: bytes | None = None

    def inject(self, op: str, error: type[Exception] = WriteError) -> None:
        """注入指定操作的例外。"""
        self._injections[op] = error

    def _check_injection(self, op: str) -> None:
        if op in self._injections:
            err_cls = self._injections.pop(op)
            raise err_cls(f"FakeAnnexGit injected error on {op}")

    def ls_remote(self, remote: str = "origin") -> dict[str, str]:
        self._check_injection("ls_remote")
        return normalize_ls_remote(self.refs)

    def _ensure_git_repo(self) -> None:
        if not (self.workdir / ".git").is_dir():
            self.workdir.mkdir(parents=True, exist_ok=True)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "committer",
                "GIT_AUTHOR_EMAIL": "committer@aistorage.local",
                "GIT_COMMITTER_NAME": "committer",
                "GIT_COMMITTER_EMAIL": "committer@aistorage.local",
                "GIT_AUTHOR_DATE": "2026-09-27T10:00:00Z",
                "GIT_COMMITTER_DATE": "2026-09-27T10:00:00Z",
            }
            subprocess.run(["git", "init", "-b", "main", "-q", "."], cwd=self.workdir, env=env, check=True)
            if self.drive and self.prefix_folder_id and self.repo_uuid:
                m_name = f"GITMANIFEST--{self.repo_uuid}"
                m_files = self.drive.find_by_name(self.prefix_folder_id, m_name)
                if m_files:
                    m_bytes = self.drive.download_bytes(m_files[0].id, max_bytes=10 * 1024 * 1024)
                    from aistorage.annex.manifest import parse_manifest
                    try:
                        parsed = parse_manifest(m_bytes, repo_uuid=self.repo_uuid)
                        with tempfile.TemporaryDirectory(prefix="fake_unbundle_") as td:
                            for b_name in parsed.active:
                                bf = self.drive.find_by_name(self.prefix_folder_id, b_name)
                                if bf:
                                    b_dest = Path(td) / b_name
                                    self.drive.download(bf[0].id, b_dest, max_bytes=100 * 1024 * 1024)
                                    subprocess.run(
                                        ["git", "bundle", "unbundle", str(b_dest)],
                                        cwd=self.workdir,
                                        env=env,
                                        check=False,
                                    )
                    except Exception:
                        pass
            for ref_name, sha in list(self.refs.items()):
                short = ref_name.replace("refs/heads/", "")
                ns_ref = f"refs/namespaces/git-remote-annex/{self.repo_uuid}/{ref_name}" if self.repo_uuid else None
                chk_ns = subprocess.run(["git", "rev-parse", ns_ref], cwd=self.workdir, capture_output=True, text=True) if ns_ref else None
                if chk_ns and chk_ns.returncode == 0:
                    target_sha = chk_ns.stdout.strip()
                    subprocess.run(["git", "update-ref", f"refs/heads/{short}", target_sha], cwd=self.workdir, env=env, check=False)
                    self.refs[ref_name] = target_sha
                elif sha:
                    chk_sha = subprocess.run(["git", "rev-parse", sha], cwd=self.workdir, capture_output=True)
                    if chk_sha.returncode == 0:
                        subprocess.run(["git", "update-ref", f"refs/heads/{short}", sha], cwd=self.workdir, env=env, check=False)

            chk = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=self.workdir, capture_output=True)
            if chk.returncode != 0:
                (self.workdir / ".gitkeep").write_text("initial\n")
                subprocess.run(["git", "add", "."], cwd=self.workdir, env=env, check=True)
                subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.workdir, env=env, check=True)
                out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workdir, capture_output=True, text=True, check=True)
                self.refs["refs/heads/main"] = out.stdout.strip()

    def add(self, paths: list[str | Path] | str | Path) -> None:
        self._check_injection("add")
        if isinstance(paths, (str, Path)):
            self.added_paths.append(str(paths))
        else:
            self.added_paths.extend(str(p) for p in paths)
        if self.drive and self.prefix_folder_id and self.repo_uuid:
            self._ensure_git_repo()
            plist = [str(paths)] if isinstance(paths, (str, Path)) else [str(p) for p in paths]
            subprocess.run(["git", "add", *plist], cwd=self.workdir, check=False)

    def commit(self, message: str) -> str:
        self._check_injection("commit")
        if self.drive and self.prefix_folder_id and self.repo_uuid:
            self._ensure_git_repo()
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "committer",
                "GIT_AUTHOR_EMAIL": "committer@aistorage.local",
                "GIT_COMMITTER_NAME": "committer",
                "GIT_COMMITTER_EMAIL": "committer@aistorage.local",
                "GIT_AUTHOR_DATE": "2026-09-27T10:00:00Z",
                "GIT_COMMITTER_DATE": "2026-09-27T10:00:00Z",
            }
            subprocess.run(["git", "add", "."], cwd=self.workdir, env=env, check=True)
            subprocess.run(["git", "commit", "-qm", message, "--allow-empty"], cwd=self.workdir, env=env, check=True)
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.workdir, env=env, check=True, capture_output=True, text=True)
            sha = out.stdout.strip()
        else:
            sha = hashlib.sha1(f"{message}-{len(self.commits)}".encode("utf-8")).hexdigest()
        self.commits.append((message, sha))
        # 模擬本地 HEAD 前進
        self.pending_refs["refs/heads/main"] = sha
        return sha

    def copy(
        self,
        remote: str,
        to_copy: list[str] | None = None,
        *,
        timeout: float = 900.0,
    ) -> None:
        self._check_injection("copy")
        self.copied.append((remote, to_copy))
        if self.copy_effect == "annex_upload":
            # 模擬 `git annex copy`：本機的 key 變成在 remote 上，且 location log
            # 讓 refs/heads/git-annex 的 sha 前進（review-g3g H1）
            if self.local_keys:
                self.annex_keys = frozenset(set(self.annex_keys) | set(self.local_keys))
            if self.drive and self.prefix_folder_id and self.repo_uuid:
                self._ensure_git_repo()
                env = {
                    **os.environ,
                    "GIT_AUTHOR_NAME": "committer",
                    "GIT_AUTHOR_EMAIL": "committer@aistorage.local",
                    "GIT_COMMITTER_NAME": "committer",
                    "GIT_COMMITTER_EMAIL": "committer@aistorage.local",
                    "GIT_AUTHOR_DATE": "2026-09-27T10:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-09-27T10:00:00Z",
                }
                out = subprocess.run(
                    ["git", "commit-tree", "HEAD^{tree}", "-m", "annex location log update"],
                    cwd=self.workdir,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                new_annex_sha = out.stdout.strip()
                subprocess.run(
                    ["git", "update-ref", "refs/heads/git-annex", new_annex_sha],
                    cwd=self.workdir,
                    env=env,
                    check=True,
                )
                self.pending_refs["refs/heads/git-annex"] = new_annex_sha
            else:
                current = self.pending_refs.get(
                    "refs/heads/git-annex", self.refs.get("refs/heads/git-annex", "")
                )
                self.pending_refs["refs/heads/git-annex"] = hashlib.sha1(
                    f"annex-location-log-{current}".encode("utf-8")
                ).hexdigest()

    def _generate_bundle_and_update_manifest(self) -> None:
        """為 FakeAnnexGit 產生新 bundle 與更新 manifest（FakeAnnexGit 保真度）。"""
        self._ensure_git_repo()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "committer",
            "GIT_AUTHOR_EMAIL": "committer@aistorage.local",
            "GIT_COMMITTER_NAME": "committer",
            "GIT_COMMITTER_EMAIL": "committer@aistorage.local",
            "GIT_AUTHOR_DATE": "2026-09-27T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-09-27T10:00:00Z",
        }
        ns_refs: list[str] = []
        for ref_name, sha in self.refs.items():
            clean = ref_name if ref_name.startswith("refs/") else f"refs/heads/{ref_name}"
            ns_ref = f"refs/namespaces/git-remote-annex/{self.repo_uuid}/{clean}"
            subprocess.run(["git", "update-ref", ns_ref, sha], cwd=self.workdir, env=env, check=True)
            ns_refs.append(ns_ref)

        if not ns_refs:
            return

        with tempfile.TemporaryDirectory(prefix="fake_bundle_gen_") as td:
            b_path = Path(td) / "bundle.pack"
            subprocess.run(
                ["git", "bundle", "create", str(b_path), *ns_refs],
                cwd=self.workdir,
                env=env,
                check=True,
                capture_output=True,
            )
            raw = b_path.read_bytes()
            b_size = len(raw)
            b_sha = hashlib.sha256(raw).hexdigest().lower()
            b_name = f"GITBUNDLE-s{b_size}--{self.repo_uuid}-{b_sha}"

            from aistorage.clock import format_rfc3339
            now_iso = format_rfc3339(
                self.clock.now() if self.clock else datetime.now(timezone.utc),
                include_fraction=True,
            )
            if hasattr(self.drive, "seed_file"):
                self.drive.seed_file(
                    self.prefix_folder_id,
                    b_name,
                    raw,
                    created_time=now_iso,
                )
            else:
                self.drive.create(
                    self.prefix_folder_id,
                    b_name,
                    raw,
                )

            # 更新 manifest
            m_name = f"GITMANIFEST--{self.repo_uuid}"
            m_files = self.drive.find_by_name(self.prefix_folder_id, m_name)
            if m_files:
                old_bytes = self.drive.download_bytes(m_files[0].id, max_bytes=10 * 1024 * 1024)
                old_text = old_bytes.decode("utf-8").strip()
                new_manifest_text = f"{old_text}\n{b_name}\n" if old_text else f"{b_name}\n"
                if hasattr(self.drive, "seed_file"):
                    self.drive.delete_permanently(m_files[0].id)
                    self.drive.seed_file(
                        self.prefix_folder_id,
                        m_name,
                        new_manifest_text.encode("utf-8"),
                        created_time=now_iso,
                    )
                else:
                    self.drive.update_content(m_files[0].id, new_manifest_text.encode("utf-8"))
            else:
                new_manifest_text = f"{b_name}\n"
                if hasattr(self.drive, "seed_file"):
                    self.drive.seed_file(
                        self.prefix_folder_id,
                        m_name,
                        new_manifest_text.encode("utf-8"),
                        created_time=now_iso,
                    )
                else:
                    self.drive.create(
                        self.prefix_folder_id,
                        m_name,
                        new_manifest_text.encode("utf-8"),
                    )
            # H1：真的 git-remote-annex 會把剛寫上遠端的那份 manifest 留在本機
            # `.git/annex/git-remote-annex/<uuid>/manifest`，sha256 完全相同。fake
            # 記住位元組，`local_manifest_sha256()` 就是它的雜湊。
            self._local_manifest_bytes = new_manifest_text.encode("utf-8")

    def push(
        self,
        remote: str = "origin",
        branches: tuple[str, ...] | str = ("main", "git-annex"),
        *,
        timeout: float = 900.0,
    ) -> None:
        self._check_injection("push")
        branch_tuple = (branches,) if isinstance(branches, str) else tuple(branches)
        self.pushed.append((remote, branch_tuple))

        if self.push_effect == "error":
            raise WriteError("FakeAnnexGit simulated push network error")
        elif self.push_effect == "silent_fail":
            # 靜默失敗：exit 0，但遠端 refs 不變
            pass
        elif self.push_effect == "apply":
            # 成功：遠端 refs 更新為 pending_refs
            for k, v in self.pending_refs.items():
                self.refs[k] = v

            if self.drive is not None and self.prefix_folder_id and self.repo_uuid:
                self._generate_bundle_and_update_manifest()

    def annex_keys_in(self, remote_uuid: str) -> frozenset[str]:
        self._check_injection("annex_keys_in")
        return self.annex_keys

    def origin_url(self) -> str:
        """H1：fake 回報自己被當成哪一個 target clone 出來的。"""
        self._check_injection("origin_url")
        return self.repo_url

    def remote_uuid(self, remote: str = "origin") -> str:
        self._check_injection("remote_uuid")
        if not self.repo_uuid:
            raise ReadError("FakeAnnexGit 沒有 repo_uuid，無法確認遠端身分")
        return self.repo_uuid

    def lookupkey(self, path: str | Path) -> str | None:
        """測試用的 key 查詢：以工作樹上實際的檔案內容按 git-annex 的命名規則推算。

        真實環境的 key 一律由 `git annex lookupkey` 提供（SubprocessAnnexGit）；
        這裡只是讓不跑真 git-annex 的測試也能走同一條介面。命名規則與 git-annex
        一致：副檔名取自**工作樹檔名**，沒有副檔名就沒有副檔名。
        """
        self._check_injection("lookupkey")
        target = Path(path)
        if not target.is_absolute():
            target = self.workdir / target
        if not target.is_file():
            return None
        data = target.read_bytes()
        import hashlib as _hashlib

        sha = _hashlib.sha256(data).hexdigest().lower()
        ext = target.suffix
        key = f"SHA256E-s{len(data)}--{sha}{ext}"
        self.local_keys = frozenset(set(self.local_keys) | {key})
        self._fake_objects[key] = data
        return key

    def get_key(self, key: str, *, from_remote: str = "origin") -> None:
        """測試用的取回：只能取回本 fake 曾經看過的物件，取不到就 raise。

        對應真實行為：全新 clone 的本機沒有物件，必須從遠端 special remote 取回；
        fake 沒有遠端，所以未登錄的 key 一律 fail-closed（ReadError）。
        """
        self._check_injection("get_key")
        if key in self._fake_objects:
            return
        raise ReadError(f"fake annex 沒有這個物件（測試要先 store 或 register_object）: {key}")

    def register_object(self, key: str, data: bytes) -> None:
        """登錄一個「遠端上存在」的物件，讓 get_key 取得得到。"""
        self._fake_objects[key] = bytes(data)

    def local_manifest_sha256(self, remote_uuid: str) -> str | None:
        """H1：fake 版的「剛寫上去那份 manifest 的雜湊」。

        真的 `SubprocessAnnexGit` 讀的是 `.git/annex/git-remote-annex/<uuid>/
        manifest`（push 之後 git-remote-annex 留在本機的那份）。fake 沒有真的
        git-remote-annex，所以它記住自己**上一次 push 寫出去的 manifest 內容**
        並回它的雜湊；還沒 push 過就回 None（沒有這個證據）。
        """
        if self._local_manifest_bytes is None:
            return None
        return hashlib.sha256(self._local_manifest_bytes).hexdigest().lower()

    def set_local_manifest(self, data: bytes) -> None:
        """測試用：宣告「這一輪 push 寫出去的 manifest 就是這些位元組」。"""
        self._local_manifest_bytes = bytes(data)

    def local_refs(self, branches: tuple[str, ...] = ("main", "git-annex")) -> dict[str, str]:
        self._check_injection("local_refs")
        combined = dict(self.refs)
        combined.update(self.pending_refs)
        res: dict[str, str] = {}
        for b in branches:
            full = f"refs/heads/{b}" if not b.startswith("refs/") else b
            short = b.replace("refs/heads/", "")
            if full in combined:
                res[full] = combined[full]
            elif short in combined:
                res[full] = combined[short]
        return res

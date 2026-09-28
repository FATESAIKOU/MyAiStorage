"""管理者的 swap：刪遠端 → 重讀遠端 manifest → push → 驗證 → 重建 pin → 讀取視圖重建世代。

抹除（6.1 的 `swap_remote`）與回滾（6.2 的 `swap_remote`）共用這一段，
因為兩者對「遠端變更之後要怎麼讓它重新變成可信狀態」的答案完全一樣：
先讓遠端與本機一致（push），再用**觀測到的遠端狀態**重建正式 pin，
並把讀取視圖的 rebuild epoch 加 1，下一輪提交流程會做完整重建。

6.5 的「push 前重讀遠端 manifest」在這裡（`recheck-remote` 步驟）：
swap 開始時記下遠端 manifest 的指紋，push 前再讀一次；只要兩者不同，
就是有人在管理操作期間動了遠端，這一輪中止並保留鎖（不要用 force push 蓋過去）。

順序取自 1.3 抹除驗證的實測（docs/spike/evidence/1.3-erase.md）：
`git push --force` 不會刪掉遠端舊的 GITBUNDLE，所以抹除必須「永久刪除 +
重推」；重建 pin 之前遠端是沒有可信基準的，那段時間提交流程一定被
AdminLock 擋住（H5：出錯就保留鎖）。

任何一步失敗都丟 `SwapAborted`，帶著「已完成到哪一步」與「接下來的指令」，
呼叫端（AdminLock 的 with 區塊）就會保留鎖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence

from aistorage.admin import AdminDeps, AdminError
from aistorage.annex.git import get_git_env
from aistorage.annex.manifest import parse_bundle_name, parse_manifest
from aistorage.drive.model import DriveClient

SWAP_DELETE = "delete-remote"
SWAP_RECHECK = "recheck-remote"
SWAP_PUSH = "push"
SWAP_VERIFY = "verify-remote"
SWAP_PIN = "rebuild-pin"
SWAP_EPOCH = "readview-epoch"

SWAP_ORDER = (SWAP_DELETE, SWAP_RECHECK, SWAP_PUSH, SWAP_VERIFY, SWAP_PIN, SWAP_EPOCH)


@dataclass(frozen=True)
class RemoteCheck:
    """某一個位置的殘留計數（只輸出計數，不含內容）。"""

    location: str
    count: int


@dataclass(frozen=True)
class SwapStep:
    name: str
    status: str          # ok / failed / skipped
    detail: str = ""


@dataclass(frozen=True)
class DeleteGroup:
    """一類要刪的遠端檔案，以及該類別**自己的**合法 parent 根。

    H6：讀取視圖、收件匣、隔離區的檔案不在真本前綴之下，所以不能拿
    `prefix_folder_id` 去檢查它們的 parent，否則一定會被拒絕
    （而且舊寫法是「先刪 repo 的檔案、刪到讀取視圖第一個才失敗」）。
    隔離區還多一層日期子資料夾，所以比對的是「parent 位於哪個根底下」，
    不是「parent 剛好等於哪個資料夾」。
    """

    name: str
    file_ids: tuple[str, ...]
    parent_roots: tuple[str, ...]


@dataclass
class SwapReport:
    steps: list[SwapStep] = field(default_factory=list)
    deleted_file_ids: tuple[str, ...] = ()
    manifest_file_id: str | None = None
    manifest_sha256: str | None = None
    promoted_at: str | None = None
    rebuild_epoch: int | None = None
    pushed_refs: dict[str, str] = field(default_factory=dict)
    #: 重讀遠端 manifest 的兩次結果（6.5：push 前要確認遠端沒有被動過）。
    #: 第一次是 swap 開始時的現況，第二次是 push 前；`None` 代表當時遠端沒有 manifest。
    remote_manifest_start: str | None = None
    remote_manifest_before_push: str | None = None

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)

    @property
    def done(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps if s.status == "ok")

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [{"name": s.name, "status": s.status, "detail": s.detail}
                      for s in self.steps],
            "deleted_file_ids": list(self.deleted_file_ids),
            "manifest_file_id": self.manifest_file_id,
            "manifest_sha256": self.manifest_sha256,
            "promoted_at": self.promoted_at,
            "rebuild_epoch": self.rebuild_epoch,
            "pushed_refs": self.pushed_refs,
            "remote_manifest_start": self.remote_manifest_start,
            "remote_manifest_before_push": self.remote_manifest_before_push,
        }


class SwapAborted(AdminError):
    """swap 中途失敗：遠端可能已經不一致，必須保留鎖等人處理。"""

    def __init__(self, report: SwapReport, message: str, *,
                 resume_hint: str = "") -> None:
        done = " → ".join(report.done) or "(無)"
        text = f"{message}｜已完成：{done}"
        if resume_hint:
            text += f"｜下一步：{resume_hint}"
        super().__init__(text)
        self.report = report
        self.resume_hint = resume_hint


def _run_git(repo_dir: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo_dir), capture_output=True, text=True,
        env=get_git_env(), timeout=900, check=False)
    if check and proc.returncode != 0:
        raise AdminError(f"git {' '.join(args)} 失敗 (rc={proc.returncode})")
    return proc.stdout


def check_repo_dir(repo_dir: Path | str, *, purpose: str) -> Path:
    """M8：會改寫 repo 的指令（filter-repo、git annex、force push）只能在
    「不是專案 repo、而且 origin 確實是 annex:: 遠端」的工作目錄執行。"""
    from aistorage.safety import assert_safe_workdir

    return assert_safe_workdir(
        repo_dir, purpose=purpose, require_annex_remote=True)


def _ids_under(drive: DriveClient, roots: Sequence[str]) -> set[str]:
    """某個根（遞迴，含根本身）底下的所有檔案 id。"""
    ids: set[str] = set()
    for root in roots:
        for f in _list_files(drive, root):
            ids.add(f.id)
    return ids


def manifest_name_for(repo_uuid: str) -> str:
    """主 manifest 在前綴底下的檔名（只認這一個名字，其他檔不查）。"""
    return f"GITMANIFEST--{repo_uuid}"


def read_remote_manifest_sha256(
    drive: DriveClient, prefix_folder_id: str, repo_uuid: str, *, allow_missing: bool
) -> str | None:
    """重讀遠端主 manifest 的內容雜湊（6.5 的「push 前重讀遠端 manifest」）。

    只用 Drive 的 metadata（`sha256Checksum`），不下載整個 manifest——
    這裡要的只是「有沒有被動過」的指紋。回傳 `None` 代表遠端沒有主 manifest。

    - 找到多個 → 拒絕（狀態已經不明，中止，不要猜）；
    - 找不到且 `allow_missing=False` → 拒絕；
    - 檔案沒有 checksum → 拒絕（fail-closed：判斷不出有沒有被動過就不准推）。
    """
    name = manifest_name_for(repo_uuid)
    found = drive.find_by_name(prefix_folder_id, name)
    if not found:
        if allow_missing:
            return None
        raise AdminError(
            f"重讀遠端 manifest 失敗：{name} 不存在（這一輪不該不見）")
    if len(found) > 1:
        raise AdminError(
            f"重讀遠端 manifest 失敗：{name} 找到 {len(found)} 個（應為 1）")
    sha = found[0].sha256
    if not sha:
        raise AdminError(
            f"重讀遠端 manifest 失敗：Drive 尚未提供 checksum（{name}）")
    return sha.lower()


def check_delete_group(drive: DriveClient, group: DeleteGroup) -> None:
    """刪前確認：檔案存在，而且確實在某個合法 parent 根底下。"""
    allowed = _ids_under(drive, group.parent_roots)
    for file_id in group.file_ids:
        try:
            info = drive.get(file_id)
        except Exception as e:
            raise AdminError(f"刪除前確認失敗 {file_id}: {e}") from None
        if not info.parents:
            raise AdminError(f"拒絕刪除 {group.name}：{file_id} 沒有 parent 資訊")
        if file_id not in allowed:
            raise AdminError(
                f"拒絕刪除 {group.name}：{file_id} 不在合法 parent 根 "
                f"{list(group.parent_roots)} 之下")


def delete_remote_files(drive: DriveClient, groups: Sequence[DeleteGroup],
                        *, report: SwapReport | None = None) -> tuple[str, ...]:
    """依類別永久刪除；每個檔案刪前確認 parent 屬於**該類別**。"""
    deleted: list[str] = []
    for group in groups:
        check_delete_group(drive, group)
        for file_id in group.file_ids:
            drive.delete_permanently(file_id)
            deleted.append(file_id)
    if report is not None:
        report.deleted_file_ids = tuple(deleted)
    return tuple(deleted)


def push_history(*, repo_dir: Path, git: Any, force: bool) -> dict[str, str]:
    """`git annex copy --to=origin` → `git push [--force] origin main git-annex`。

    抹除改寫過歷史，所以必須 force；回滾只是新增 commit，不需要。
    """
    git.copy("origin")
    args = ["push"]
    if force:
        args.append("--force")
    args += ["origin", "main", "git-annex"]
    _run_git(repo_dir, *args)
    return git.local_refs()


def verify_remote_refs(*, repo_dir: Path, git: Any, drive: DriveClient,
                       prefix_folder_id: str, repo_uuid: str) -> tuple[str, str]:
    """push 後的驗證：ls-remote 與本機一致、manifest 在遠端、沒有不在 manifest 的 bundle。"""
    remote_refs = git.ls_remote("origin")
    local_refs = git.local_refs()
    for branch in ("main", "git-annex"):
        full = f"refs/heads/{branch}"
        if remote_refs.get(full) != local_refs.get(full):
            raise AdminError(
                f"push 後驗證失敗：{full} 遠端 {remote_refs.get(full)} "
                f"與本機 {local_refs.get(full)} 不符")

    manifest_name = f"GITMANIFEST--{repo_uuid}"
    found = drive.find_by_name(prefix_folder_id, manifest_name)
    if len(found) != 1:
        raise AdminError(
            f"push 後驗證失敗：遠端主 manifest {manifest_name} 找到 {len(found)} 個（應為 1）")
    data = drive.download_bytes(found[0].id, max_bytes=1024 * 1024)
    manifest = parse_manifest(data, repo_uuid=repo_uuid)
    known = set(manifest.active) | set(manifest.removed)
    unlisted = [f.name for f in _list_files(drive, prefix_folder_id)
                if parse_bundle_name(f.name) is not None and f.name not in known]
    if unlisted:
        raise AdminError(
            f"push 後驗證失敗：遠端有 {len(unlisted)} 個不在 manifest 的 bundle"
            f"（{unlisted[0]}…）")
    return found[0].id, hashlib.sha256(data).hexdigest().lower()


def _list_files(drive: DriveClient, folder_id: str, _depth: int = 0) -> list[Any]:
    if _depth > 8:
        return []
    out: list[Any] = []
    for child in drive.list_children(folder_id):
        if child.is_folder:
            out.extend(_list_files(drive, child.id, _depth + 1))
        else:
            out.append(child)
    return out


def bump_readview_epoch(config_path: Path | str) -> int:
    """把設定檔的 `readview_rebuild_epoch` 加 1；下一輪提交流程做完整讀取視圖重建。"""
    path = Path(config_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AdminError(f"讀不到設定檔 {path}: {e}") from None
    epoch = int(data.get("readview_rebuild_epoch", 0)) + 1
    data["readview_rebuild_epoch"] = epoch
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return epoch


def swap_remote(*, admin: AdminDeps, cfg: Any, deps: Any, git: Any,
                repo_dir: Path | str, delete_groups: Sequence[DeleteGroup] = (),
                force_push: bool, config_path: Path | str | None = None,
                resume_hint: str = "", push_fn: Any | None = None,
                pin_rebuild_fn: Any | None = None) -> SwapReport:
    """刪（可選）→ push 前重讀遠端 manifest → push → 驗證 → 重建 pin → 讀取視圖重建世代。

    呼叫端必須已經在 AdminLock 內；本函式不碰 workflow，也不解除鎖。
    中途失敗丟 `SwapAborted`（內含已完成步驟與下一步指令），由呼叫端決定
    如何回報；AdminLock 因為收到例外會保留鎖（H5）。

    `push_fn`／`pin_rebuild_fn` 只有單元測試會注入（真實 push 與 init-pin
    是整合測試的範圍）；正式路徑走 `push_history` 與 `init_pin_cli`。
    """
    workdir = check_repo_dir(repo_dir, purpose="管理 swap 工作目錄")
    report = SwapReport()

    def _fail(step: str, detail: str) -> None:
        report.steps.append(SwapStep(name=step, status="failed", detail=detail))
        raise SwapAborted(
            report, f"swap 在「{step}」中止：{detail}",
            resume_hint=resume_hint or _default_resume_hint(step, cfg))

    # 0. 現況基準（6.5）：先記下 swap 開始時遠端 manifest 的指紋，
    #    push 前再讀一次比對——中間只要有人動過遠端，這裡就擋下來。
    #    找不到不算錯：中途中止後接手（swap-finish）時遠端本來就可能沒有 manifest。
    try:
        report.remote_manifest_start = read_remote_manifest_sha256(
            deps.drive, cfg.prefix_folder_id, cfg.repo_uuid, allow_missing=True)
    except Exception as e:
        _fail(SWAP_RECHECK, f"{type(e).__name__}: {e}")

    # 1. 刪遠端（抹除才有；回滾不刪）
    if delete_groups:
        try:
            deleted = delete_remote_files(deps.drive, delete_groups, report=report)
            report.steps.append(SwapStep(
                name=SWAP_DELETE, status="ok",
                detail=f"永久刪除 {len(deleted)} 個檔案"))
        except AdminError as e:
            _fail(SWAP_DELETE, str(e))
    else:
        report.steps.append(SwapStep(name=SWAP_DELETE, status="skipped",
                                     detail="無需刪除（保留遠端檔案）"))

    # 1b. push 前重讀遠端 manifest（6.5＋M4）。這一輪有刪遠端檔案時（抹除），
    #     主 manifest 已經被自己刪掉，「不存在」是正常的預期結果；「與開始時
    #     完全相同」也可以接受（例如只刪了其他類別，本輪沒動 manifest）。
    #     讀到**新的**主 manifest，就是抹除期間有人 push 過，這一輪中止並保留
    #     鎖（不要用 force push 蓋過去）。
    try:
        current = read_remote_manifest_sha256(
            deps.drive, cfg.prefix_folder_id, cfg.repo_uuid,
            allow_missing=bool(delete_groups))
        report.remote_manifest_before_push = current
        if current != report.remote_manifest_start and (
                not delete_groups or current is not None):
            raise AdminError(
                "push 前重讀遠端 manifest：與 swap 開始時不同"
                f"（{report.remote_manifest_start} → {current}），"
                "遠端在管理操作期間被動過，中止")
        report.steps.append(SwapStep(
            name=SWAP_RECHECK, status="ok",
            detail=(f"遠端 manifest 未被動過 {current[:8]}"
                    if current else "遠端無主 manifest（已由本輪刪除）")))
    except Exception as e:
        _fail(SWAP_RECHECK, f"{type(e).__name__}: {e}")

    # 2. push
    try:
        do_push = push_fn or (lambda: push_history(repo_dir=workdir, git=git,
                                                   force=force_push))
        report.pushed_refs = do_push()
        report.steps.append(SwapStep(
            name=SWAP_PUSH, status="ok",
            detail=f"{'force ' if force_push else ''}push 完成（"
                   f"{len(report.pushed_refs)} refs）"))
    except Exception as e:
        _fail(SWAP_PUSH, f"{type(e).__name__}: {e}")

    # 3. push 後驗證
    try:
        (report.manifest_file_id, report.manifest_sha256) = verify_remote_refs(
            repo_dir=workdir, git=git, drive=deps.drive,
            prefix_folder_id=cfg.prefix_folder_id, repo_uuid=cfg.repo_uuid)
        report.steps.append(SwapStep(
            name=SWAP_VERIFY, status="ok",
            detail=f"manifest {report.manifest_sha256[:8]}"))
    except Exception as e:
        _fail(SWAP_VERIFY, f"{type(e).__name__}: {e}")

    # 4. 以觀測到的遠端狀態重建正式 pin
    try:
        if pin_rebuild_fn is not None:
            state = pin_rebuild_fn()
        else:
            from aistorage.committer.run import init_pin_cli

            # 呼叫端已在 AdminLock 裡（旗標就是自己放的）→ 允許 promote 覆蓋它；
            # 這一輪也順便把上一輪被維護旗標擋下而留下的 pending 清掉（L，review-cdb4a34）。
            state = init_pin_cli(cfg, deps, confirm=True, maintenance_ok=True)
        report.promoted_at = state.promoted_at
        report.steps.append(SwapStep(
            name=SWAP_PIN, status="ok",
            detail=f"pin 重建（manifest {state.manifest_sha256[:8]}、"
                   f"{len(state.annex_keys)} keys）"))
    except Exception as e:
        _fail(SWAP_PIN, f"{type(e).__name__}: {e}")

    # 5. 讀取視圖重建世代加 1
    if config_path is not None:
        try:
            report.rebuild_epoch = bump_readview_epoch(config_path)
            report.steps.append(SwapStep(
                name=SWAP_EPOCH, status="ok",
                detail=f"readview_rebuild_epoch={report.rebuild_epoch}"))
        except Exception as e:
            _fail(SWAP_EPOCH, f"{type(e).__name__}: {e}")
    else:
        report.steps.append(SwapStep(name=SWAP_EPOCH, status="skipped",
                                     detail="未提供設定檔，未加 rebuild epoch"))
    return report


def _default_resume_hint(step: str, cfg: Any) -> str:
    """失敗後管理者接下來該做的事（依 D2 與 1.3 的實測順序）。"""
    if step in (SWAP_DELETE, SWAP_PUSH):
        return ("遠端已不一致但本機已改寫完成：保留維護旗標，"
                "在管理 clone 重新 push（git annex copy --to=origin；"
                "git push --force origin main git-annex），"
                "再執行 `python -m aistorage.admin swap-finish`")
    if step == SWAP_RECHECK:
        return ("遠端在管理操作期間被動過（可能有另一輪提交流程或另一個管理操作跑過）："
                "先確認遠端現況與釘選值誰才對，必要的話重跑 `init-pin --confirm` "
                "建立基準，再用 `python -m aistorage.admin swap-finish`")
    if step == SWAP_VERIFY:
        return ("不要解除維護旗標：先確認遠端 bundle／manifest 狀態，"
                "修正後重跑 push，再用 `python -m aistorage.admin swap-finish`")
    if step == SWAP_PIN:
        return ("遠端已一致，只是 pin 還是舊的：執行 "
                "`python -m aistorage.committer init-pin --confirm` 重建 pin，"
                "再 `python -m aistorage.admin swap-finish` 加讀取視圖重建世代")
    return "確認遠端與 pin 一致後，執行 `python -m aistorage.admin unlock --confirm`"

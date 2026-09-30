"""第 3 組整合測試的真實環境設定（Drive ＋ GitHub pin repo）。

依據 docs/impl/group3-modules.md 第 8.3 節：
- 只用 `~/.config/aistorage/ids.env` 的 `TEST_FOLDER_ID`、
  `~/.config/aistorage/rclone-committer-test.conf`、`~/.config/aistorage/pin-test.key`
  與 `FATESAIKOU/MyAiStorage-pin-test`；**只以路徑引用**，絕不讀取或輸出秘密。
- 選了 `integration` 標記但設定缺少時要 **FAIL，不能 skip**。
- 每個測試在 `TEST_FOLDER_ID` 底下建自己的前綴（`it-<ULID>/`），測完依 file id
  永久刪除（刪前 `get()` 確認 parents 確實是測試資料夾底下）。
"""

from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

from aistorage.errors import NotFound
from aistorage.schema import generate_ulid

REPO_ROOT = Path(__file__).resolve().parents[2]
AISTORAGE_HOME = Path.home() / ".config" / "aistorage"
IDS_ENV = AISTORAGE_HOME / "ids.env"
RCLONE_CONF = AISTORAGE_HOME / "rclone-committer-test.conf"
PIN_KEY = AISTORAGE_HOME / "pin-test.key"
KNOWN_HOSTS = REPO_ROOT / "config" / "github_known_hosts"
PIN_REPO_URL = "git@github.com:FATESAIKOU/MyAiStorage-pin-test.git"
RCLONE_REMOTE = "gdrive"

#: 允許用環境變數覆寫（CI 或臨時換設定），值一律是路徑。
ENV_IDS = "AISTORAGE_TEST_IDS"
ENV_RCLONE_CONF = "AISTORAGE_TEST_RCLONE_CONF"
ENV_PIN_KEY = "AISTORAGE_TEST_PIN_KEY"
ENV_KNOWN_HOSTS = "AISTORAGE_TEST_KNOWN_HOSTS"
ENV_PIN_REPO = "AISTORAGE_TEST_PIN_REPO"


class MissingIntegrationSetting(RuntimeError):
    """整合測試的設定缺少或不可用（依 8.3：必須 FAIL，不可 skip）。"""


def _require(path: Path, what: str, env_override: str | None = None) -> Path:
    candidate = Path(os.environ[env_override]) if env_override and os.environ.get(env_override) else path
    if not candidate.exists():
        raise MissingIntegrationSetting(
            f"整合測試需要 {what}，但找不到 {candidate}。"
            "（設定缺少必須讓測試 FAIL；要用 pytest -m integration 明確執行，"
            "不要用 skip 掩蓋。）"
        )
    return candidate


def read_test_folder_id() -> str:
    """從 ids.env 讀 TEST_FOLDER_ID（這是 Drive 的資料夾 id，不是秘密）。"""
    env_path = _require(IDS_ENV, "TEST_FOLDER_ID 的來源檔", ENV_IDS) if not os.environ.get(ENV_IDS) else Path(os.environ[ENV_IDS])
    text = env_path.read_text(encoding="utf-8")
    match = re.search(r"^\s*TEST_FOLDER_ID\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise MissingIntegrationSetting(f"{env_path} 裡沒有 TEST_FOLDER_ID")
    return match.group(1).strip()


def require_settings() -> dict[str, object]:
    """載入全部整合測試設定；任何一項缺失都丟 MissingIntegrationSetting。"""
    return {
        "test_folder_id": read_test_folder_id(),
        "rclone_conf": _require(RCLONE_CONF, "rclone 設定檔", ENV_RCLONE_CONF),
        "pin_key": _require(PIN_KEY, "pin repo 的 deploy key", ENV_PIN_KEY),
        "known_hosts": _require(KNOWN_HOSTS, "GitHub known_hosts", ENV_KNOWN_HOSTS),
        "pin_repo_url": os.environ.get(ENV_PIN_REPO) or PIN_REPO_URL,
    }


@pytest.fixture(scope="session")
def it_settings() -> dict[str, object]:
    """整合測試設定（缺設定 → FAIL，不是 skip）。"""
    settings = require_settings()
    # git-annex 會 shell out 叫 rclone，憑證只以路徑傳遞（workflow 也是這樣）
    os.environ["RCLONE_CONFIG"] = str(settings["rclone_conf"])
    return settings


@pytest.fixture(scope="session")
def real_drive(it_settings):
    """真實的 Drive 用戶端（只用 rclone-committer-test.conf 的憑證）。"""
    from aistorage.drive import HttpDriveClient, RcloneConfToken

    return HttpDriveClient(RcloneConfToken(it_settings["rclone_conf"], remote=RCLONE_REMOTE))


@pytest.fixture(scope="session")
def test_root_id(it_settings) -> str:
    """Drive 上所有整合測試共用的根資料夾 id。"""
    return str(it_settings["test_folder_id"])


#: 本次 pytest session 建過、但可能還沒刪掉的 Drive 資料夾 id。
#: 只記錄自己建過的 id，所以 session 收尾的補掃不會碰到別的線正在用的前綴。
_SESSION_CREATED: list[str] = []

#: 本場清不掉的名字（測試跑完印在摘要裡，別讓殘留變成無聲的垃圾）。
_SESSION_CLEANUP_FAILURES: list[str] = []


def report_cleanup_failure(message: str) -> None:
    """記一筆清理失敗並立刻讓人看到（pytest 會把它顯示成 warning 摘要）。"""
    _SESSION_CLEANUP_FAILURES.append(message)
    import warnings

    warnings.warn(f"[integration cleanup] {message}", stacklevel=2)


def _forget_created(folder_id: str) -> None:
    """這個 id 已經清掉了（或本來就不在），不要留給 session 補掃再打一次。

    沒有這裡的話，已經刪乾淨的測試會在補掃時全部撞 404，
    警告清單就淹在「其實沒事」的錯誤裡，真正該看的反而看不到。
    """
    _SESSION_CREATED[:] = [c for c in _SESSION_CREATED if c != folder_id]


@pytest.fixture
def sandbox(real_drive, test_root_id):
    """每個測試自己的 Drive 前綴；測完依 file id 永久刪除（8.3 的清理規則）。

    用法：`prefix_name, prefix_id, quarantine_id = sandbox.create()`；沒呼叫
    create 的測試也不會動到 TEST_FOLDER_ID 底下的任何東西。
    """
    from ._harness import create_prefix, destroy_tree, new_prefix_name

    created: list[str] = []

    class Sandbox:
        test_root_id = test_root_id
        pin_names: list[str] = []
        pin_store = None

        def pin_repo_name(self) -> str:
            """本次測試專用的釘選值條目名（`CommitterConfig.repo`）。

            pin repo 是所有線的整合測試共用的一個 repo，而釘選值是「一個名稱一個檔案」。
            大家都用 "agora" 的話兩條線會互相覆蓋對方的釘選值 → 下一輪 settle 拿到
            別人的 manifest → sweep 把自己的真本全隔離。這裡保證名稱唯一。
            """
            name = f"it-{generate_ulid().lower()}"
            self.pin_names.append(name)
            return name

        def register_pin_store(self, store) -> None:
            """登錄本次測試的 PinStore（收尾用來刪掉條目）。"""
            self.pin_store = store

        def create(self, name_prefix: str = "it-") -> tuple[str, str, str]:
            """建立 `<name_prefix><ULID>` 前綴與其隔離資料夾，回傳 (name, prefix_id, quarantine_id)。

            `name_prefix` 預設 "it-"（git-annex 的 rcloneprefix 檢查要求以 it- 開頭）；
            第 6 組的抹除整合測試用 "it-erase-"，好在自己的前綴底下分辨。
            """
            name = (new_prefix_name() if name_prefix == "it-"
                    else f"{name_prefix}{generate_ulid()}")
            prefix_id, quarantine_id = create_prefix(real_drive, test_root_id, name)
            created.extend((prefix_id, quarantine_id))
            _SESSION_CREATED.extend((prefix_id, quarantine_id))
            return name, prefix_id, quarantine_id

        def create_folder(self, name: str) -> str:
            """在 TEST_FOLDER_ID 底下另建一個資料夾（例如收件匣），也納入清理。"""
            folder = real_drive.create(
                test_root_id, name, b"", mime_type="application/vnd.google-apps.folder"
            )
            created.append(folder.id)
            _SESSION_CREATED.append(folder.id)
            return folder.id

        def destroy(self, folder_id: str) -> None:
            destroy_tree(real_drive, folder_id, test_root_id)
            created[:] = [c for c in created if c != folder_id]
            _forget_created(folder_id)

    box = Sandbox()
    try:
        yield box
    finally:
        # 釘選值條目也要清（pin repo 共用，留著會堆成垃圾）
        if box.pin_store is not None and box.pin_names:
            from ._harness import cleanup_pin_entries
            try:
                cleanup_pin_entries(box.pin_store, list(box.pin_names))
            except Exception as e:
                report_cleanup_failure(f"釘選值條目 {box.pin_names}：{e}")
        for folder_id in list(created):
            try:
                destroy_tree(real_drive, folder_id, test_root_id)
            except NotFound:
                # 已經不在了（測試自己先刪過）——不是殘留，不要吵
                _forget_created(folder_id)
            except Exception as e:
                # 清理是盡力而為，測試本身的斷言才是重點——但**不能靜默**：
                # 靜默時「Drive 上留了東西」完全沒有線索（實測累積了十幾組
                # it-<ULID> 前綴而沒有人知道是誰留下的、為什麼沒刪掉）。
                report_cleanup_failure(f"Drive 資料夾 {folder_id}：{e}")
            else:
                _forget_created(folder_id)
        created.clear()


@pytest.fixture(scope="session", autouse=True)
def sweep_session_leftovers(request):
    """整場跑完再補掃一次：把這場建過、卻沒被 teardown 刪掉的資料夾清掉。

    只用 _SESSION_CREATED 裡的 file id（自己建的），不掃描別人的前綴。
    """
    drive = request.getfixturevalue("real_drive")
    root = request.getfixturevalue("test_root_id")
    yield
    if not _SESSION_CREATED:
        return
    from ._harness import destroy_tree

    for folder_id in list(_SESSION_CREATED):
        try:
            destroy_tree(drive, folder_id, root)
        except NotFound:
            pass  # 測試自己的 teardown 已經刪掉了
        except Exception as e:  # noqa: BLE001 - 補掃要繼續，不該被一個失敗中止
            report_cleanup_failure(f"補掃 Drive 資料夾 {folder_id}：{e}")
    _SESSION_CREATED.clear()
    if _SESSION_CLEANUP_FAILURES:
        print(
            f"\n[integration cleanup] {len(_SESSION_CLEANUP_FAILURES)} 筆清理失敗"
            "（Drive 或 pin repo 上會留下東西）：\n  "
            + "\n  ".join(_SESSION_CLEANUP_FAILURES),
            flush=True,
        )


@pytest.fixture(scope="session")
def real_git_factory(it_settings, tmp_path_factory):
    """真實的 SubprocessAnnexGit  factory（產出介面與 run.py 期待的一致）。"""
    from aistorage.annex.git import SubprocessAnnexGit

    base = tmp_path_factory.mktemp("annex-clone")

    def factory(dest: Path) -> SubprocessAnnexGit:
        return SubprocessAnnexGit.clone_for_commit(
            os.environ.get("AISTORAGE_IT_REPO_URL", ""),
            dest,
            max_git_bundles=int(os.environ.get("AISTORAGE_IT_MAX_BUNDLES", "20")),
        )

    factory.base = base  # type: ignore[attr-defined]
    return factory

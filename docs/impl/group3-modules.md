# 第 3 組（提交流程，tasks 3.1〜3.11）：模組切分與介面草案（架構師）

依據：design D2（13 步、收件匣處理、workflow 規則）、D3、D4、D10；review-1.2-1.6、review-1.4f2、review-1.4f3、review-1.4f5、review-1.8、review-followups、review-design-writeback、review-g2-close；既有的 `aistorage.schema`、`aistorage.inbox`、`aistorage.identity`、`aistorage.reading`。

這份是**設計草案**：函式簽名與檔案佈局可以直接照做；標了「**PM 決定**」的地方需要先拍板。

---

## 0. 開工前的前置（不是程式）

| # | 事項 | 誰 |
|---|---|---|
| P1 | review-g2-close 的 M1：刪掉 `inbox.py` 裡的 `canonical_bytes`、`sign_sidecar`、`verify_sidecar`（舊的內嵌簽章路徑），3.3 只能用 `verify_sidecar_bytes` | impl（5 分鐘） |
| P2 | 建立 pin repo `MyAiStorage-pin`（private，**空的，不放任何 workflow**），產生一把 deploy key（write），私鑰放進 MyAiStorage 的 Actions secret `PIN_DEPLOY_KEY`，公鑰加到 pin repo | 使用者（PM 提供步驟） |
| P3 | 整合測試用的 pin repo（例如 `MyAiStorage-pin-test`）與它的 deploy key，放在 `~/.config/aistorage/pin-test.key`（600，只以路徑引用） | 使用者 |
| P4 | 決定 Claude Code 的 source 名稱：`claude-code` 還是 `claude_code`（`schemas/reading-version.md:19` 與 `reading-version.schema.json:31` 的例子寫 `claude_code`，骨架目錄是 `committer/converters/claude-code/`）。兩者都符合 Session id 的 pattern；建議 `claude-code`，並同步改 2.4 文件的例子 | **PM 決定** |
| P5 | 2.6 的結論（原始紀錄放 git 還是 annex、`max-git-bundles` 的值）。本草案以 `RawStorage` 抽象處理兩種都可以，不擋開工 | 2.6 |

---

## 1. 套件佈局

```
src/aistorage/
  schema.py  inbox.py  identity.py  reading.py        # 既有（第 2 組）
  errors.py                 # AbortRun、ReadError、MismatchError 等共用例外
  clock.py                  # Clock protocol（now_utc），測試用 FixedClock
  drive/
    __init__.py
    model.py                # DriveFile dataclass、DriveClient protocol
    auth.py                 # 從 rclone conf 讀 token、刷新
    http.py                 # HttpDriveClient（Drive v3 REST，標準庫 urllib）
    fake.py                 # FakeDrive（in-memory，可注入錯誤）
  annex/
    __init__.py
    manifest.py             # 解析 GITMANIFEST（active／removed）、bundle 名稱
    replay.py               # 依 manifest 順序 unbundle，算出 refs
    git.py                  # AnnexGit：clone、ls-remote、add、commit、copy、push（subprocess）
    fake.py                 # FakeAnnexGit（單元測試用）
  integrity/
    __init__.py
    pin.py                  # PinState、PinPending、PinStore protocol、GitPinStore、MemoryPinStore
    settle.py               # 結算待定（第 3 步）
    sweep.py                # 上層同名檢查、清掃計畫（純函式）、套用
    verify.py               # clone 後核對（第 5 步）、預檢（第 9 步）、push 後驗證（第 10 步）
    gc.py                   # bundle 回收（第 11 步）、隔離資料夾 7 天清理
  intake/
    __init__.py
    scan.py                 # 掃收件匣、組出 InboxItem、完整性
    evaluate.py             # 驗章 → authorize → 格式 → 防重放 → dedup → 決策
    ledger.py               # 處理過的 item_key 清冊（讀寫真本裡的檔）
  agora/
    __init__.py
    layout.py               # 真本 repo 內的路徑規則（純函式）
    store.py                # AgoraStore：在本機 clone 的工作樹上讀寫
    apply.py                # 依型態套用：session、handoff、claim、reference、rewrite
  converters/
    __init__.py             # CONVERTERS 登錄、get_converter(source)
    base.py                 # Converter protocol、SessionFacts
    opencode.py             # 3.5
    claude_code.py          # 3.6
  committer/
    __init__.py
    config.py               # CommitterConfig（讀 config/committer.json＋環境變數裡的路徑）
    run.py                  # 13 步的編排、RunReport
    publish.py              # ReadViewPublisher protocol；第 3 組先放 NullPublisher（第 4 組實作）
    __main__.py             # CLI：python -m aistorage.committer ...
  importer/
    __init__.py
    __main__.py             # 3.11：python -m aistorage.importer ...
config/
  committer.example.json    # 非秘密的 id 與上限（真的 committer.json 在 3.1 建）
.github/workflows/
  committer.yml             # 單一 job
tests/
  unit/…                    # 每個模組一個 test_*.py（直接 import，不用 skip）
  unit/data/converters/{opencode,claude-code}/*.{json,jsonl} + *.reading.json   # 自己編的黃金樣本
  integration/              # pytest -m integration 才跑；設定缺少時要 FAIL，不能 skip
```

repo 根目錄的舊骨架（`committer/README.md` 與 `committer/converters/{opencode,claude-code}/`、`syncers/`、`search/`、`admin/`，只放 README）：建議在 3.1 把 README 改成「程式在 `src/aistorage/<模組>`」，或者直接移除，避免兩套佈局並存（**PM 決定**）。

---

## 2. Drive 存取層（`aistorage.drive`）

### 2.1 資料模型與介面

```python
# drive/model.py
@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    parents: tuple[str, ...]
    size: int | None                 # 資料夾是 None
    sha256: str | None               # Drive 的 sha256Checksum（小寫 hex）；缺少時 None
    md5: str | None
    created_time: str                # RFC 3339 UTC Z（照原樣保留）
    modified_time: str
    trashed: bool

    @property
    def is_folder(self) -> bool: ...

class DriveClient(Protocol):
    def list_children(self, folder_id: str) -> list[DriveFile]: ...
        # 分頁讀完；只回 trashed=false；任何 HTTP 錯誤 raise ReadError（不回部分結果）
    def find_by_name(self, parent_id: str, name: str) -> list[DriveFile]: ...
    def get(self, file_id: str) -> DriveFile: ...              # 404 → raise NotFound；其他錯誤 ReadError
    def download(self, file_id: str, dest: Path, *, max_bytes: int) -> int: ...
        # 串流寫檔，超過 max_bytes 立即中止並 raise TooLarge；回傳位元組數
    def download_bytes(self, file_id: str, *, max_bytes: int) -> bytes: ...   # 小檔（manifest、sidecar、sig）用
    def create(self, parent_id: str, name: str, content: bytes | Path, *, mime_type: str = "application/octet-stream") -> DriveFile: ...
    def update_content(self, file_id: str, content: bytes | Path) -> DriveFile: ...   # files.update 原地（id 不變）
    def move(self, file_id: str, *, from_parent: str, to_parent: str) -> DriveFile: ...
    def delete_permanently(self, file_id: str) -> None: ...     # files.delete（不經垃圾桶）
```

- 錯誤一律分成三類（`errors.py`）：`NotFound`（404）、`ReadError`（429、5xx、逾時、網路、解析失敗）、`WriteError`。**呼叫端從型別就能分辨「讀不到」與「確實不符」**，這是 review-1.4f3 H1「讀不到就中止、不做移動」的基礎。
- 不提供「依名稱下載」「依路徑」的操作：所有讀取都以 id 進行（design D5）。`find_by_name` 只用在上層同名檢查與掃描。
- 自動重試：只在 `HttpDriveClient` 內對 429／5xx 做有上限的指數退避（例如 3 次）；重試用完仍然失敗就 raise `ReadError`，由呼叫端中止這一輪。

### 2.2 憑證

```python
# drive/auth.py
class RcloneConfToken:
    def __init__(self, conf_path: Path, remote: str = "gdrive"): ...
    def access_token(self) -> str: ...     # 記憶體快取；過期前 60 秒自動刷新
```
- 只讀 conf 裡的 `client_id`、`client_secret`、`token.refresh_token`；刷新結果**不寫回檔案**（runner 上的 conf 是暫存複本，這樣做也避免 1.4 的「唯讀 conf 寫不回去」錯誤）。
- token、client secret 不進 log、例外訊息與 repr（`__repr__` 覆寫成 `<RcloneConfToken remote=gdrive>`）。

### 2.3 Fake

```python
# drive/fake.py
class FakeDrive(DriveClient):
    def __init__(self, clock: Clock): ...
    def seed_folder(self, name: str, parent: str | None = None) -> str: ...
    def seed_file(self, parent: str, name: str, content: bytes, *, sha256: str | None = "auto", created_time: str | None = None) -> str: ...
    def inject(self, op: str, file_id: str | None = None, *, error: type[Exception] = ReadError, times: int = 1) -> None: ...
        # 例：inject("download", fid) → 下一次 download 這個檔時 raise ReadError
    def snapshot(self) -> dict: ...       # 讓測試斷言「沒有任何移動」
```
- 同名檔、`sha256` 缺少（`sha256=None`）、垃圾桶狀態都要能模擬。
- 單元測試**一律用 FakeDrive**；`HttpDriveClient` 只在整合測試用。

---

## 3. 完整性機制（3.2）

### 3.1 manifest 與重放（`aistorage.annex`）

```python
# annex/manifest.py
@dataclass(frozen=True)
class BundleName:
    name: str; size: int; repo_uuid: str; sha256: str
def parse_bundle_name(name: str) -> BundleName | None: ...      # GITBUNDLE-s<N>--<uuid>-<sha256>

@dataclass(frozen=True)
class Manifest:
    active: tuple[str, ...]      # 依順序
    removed: frozenset[str]      # '-' 開頭的行（去掉 '-'）
def parse_manifest(data: bytes) -> Manifest: ...
    # 每一行只能是 bundle 名稱或 '-'＋bundle 名稱；其他內容 raise MismatchError；空 active 也是 MismatchError

# annex/replay.py
def replay_refs(bundle_paths_in_order: list[Path], *, workdir: Path) -> dict[str, str]: ...
    # 在空 repo 依序 git bundle unbundle；ref 以「最後一個 bundle 宣告的集合」為準（review-1.4f3 L2）；
    # 去掉 refs/namespaces/git-remote-annex/<uuid>/ 前綴；任何一個解不開 → MismatchError
```

### 3.2 釘選值與 pin repo（`integrity/pin.py`）

pin repo 裡的檔案（每個真本 repo 一組，Foundry 之後照同樣做）：
```
.pin/agora.json            # 正式
.pin/agora.pending.json    # 待定（沒有就不存在）
.pin/agora.keys            # 正式的 annex key 集合，一行一個、排序（避免 agora.json 過大）
.pin/agora.pending.keys
```

```python
@dataclass(frozen=True)
class PinState:                       # 正式
    repo: str                         # "agora"
    repo_uuid: str
    refs: dict[str, str]              # 全部 ref（集合要完全相同）
    manifest_sha256: str
    prev_manifest_sha256: str | None  # .bak 的合法值之一
    active_bundles: tuple[str, ...]
    removed_bundles: frozenset[str]
    annex_keys: frozenset[str]
    promoted_at: str
    run_id: str

@dataclass(frozen=True)
class PinPending:
    repo: str
    base_manifest_sha256: str         # 寫待定時的正式 manifest 雜湊（用來確認基準沒變）
    refs: dict[str, str]              # 即將 push 的 refs
    annex_keys: frozenset[str]        # push 之後 git-annex 分支上這個 remote 會有的 key 集合（本機算得出來）
    written_at: str
    run_id: str

class PinStore(Protocol):
    def load(self, repo: str) -> tuple[PinState, PinPending | None]: ...   # 讀不到 → ReadError（中止）
    def write_pending(self, pending: PinPending) -> None: ...
    def promote(self, state: PinState) -> None: ...          # 寫正式並刪除待定（同一個 commit）
    def drop_pending(self, repo: str) -> None: ...

class GitPinStore(PinStore):
    def __init__(self, ssh_url: str, key_path: Path, known_hosts_path: Path, workdir: Path): ...
    # GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<known_hosts>"
    # known_hosts 放 repo 裡固定的 github.com 主機金鑰（不要 accept-new）
    # 每次寫入：fetch → 以 pin repo 的 HEAD 為基底 commit → push；non-fast-forward 就中止（同一時間只會有一個提交流程）
class MemoryPinStore(PinStore): ...   # 單元測試用
```
- **只有 `committer/run.py` 會呼叫寫入方法**，而且傳入的值一律是 run 自己觀測或本機算出的結果；CLI 沒有「手動寫 pin」的子命令（design D2：不得有可觸發、可輸入的寫入路徑）。管理者重建 pin 是另一個只在 Mac 執行的管理工具（第 6 組），不放在這個套件的 CLI 裡。
- 首次初始化：`python -m aistorage.committer init-pin --dry-run` 印出「從目前遠端觀測到的狀態會寫成什麼」；真正寫入需要 `--confirm`，只能在 Mac 上用管理者的 deploy key 執行（workflow 裡沒有這個步驟）。

### 3.3 結算待定（第 3 步，`integrity/settle.py`）

```python
@dataclass(frozen=True)
class RepoListing:                    # 一次列舉的結果（第 3、4 步共用）
    prefix_folder_id: str
    files: tuple[DriveFile, ...]      # 前綴資料夾的直接子項
    subfolders: tuple[DriveFile, ...]

class SettleOutcome(Enum):
    NO_PENDING = "no_pending"
    PROMOTED = "promoted"             # 遠端＝待定 → 轉正
    DROPPED = "dropped"               # 遠端＝正式 → 丟棄待定
    BAK_RECOVERY = "bak_recovery"     # 主 manifest 不在、.bak＝正式 → 丟棄待定，照常往下

def settle(state: PinState, pending: PinPending | None, listing: RepoListing,
           drive: DriveClient, *, workdir: Path, clock: Clock) -> tuple[SettleOutcome, PinState]:
    # 全程唯讀，不做任何移動。
    # 1. 候選 manifest：名稱是 GITMANIFEST--<uuid> 的檔（可能有多個同名）；逐一 download_bytes（上限 1 MiB）
    # 2. 對每個候選：parse_manifest → active 的每個 bundle 在 listing 裡找「名稱相符且 sha256Checksum 等於名稱內嵌值」的檔
    #    → 依序下載（上限：單一 bundle 256 MiB）→ replay_refs
    # 3. 判定：
    #    - 有候選的 refs == pending.refs，而且沒有第二個「內容不同但也相符」的候選 → PROMOTED
    #      新 PinState：refs、manifest_sha256＝該候選的雜湊、prev＝舊的 manifest_sha256、
    #      active／removed＝該候選的解析結果、annex_keys＝pending.annex_keys
    #    - 有候選的 refs == state.refs 而且雜湊 == state.manifest_sha256 → DROPPED
    #    - 沒有主 manifest、.bak 的內容雜湊 == state.manifest_sha256、.bak 重放出的 refs == state.refs → BAK_RECOVERY
    #    - 其他 → raise MismatchError（中止）
    # 4. 任何下載或列舉的 ReadError → 原樣往上拋（中止，不猜）
```
- 沒有待定時，仍然要做「refs 是否等於正式」的核對，但那一步放在第 5 步（clone 之後），這裡直接回 `NO_PENDING`。
- 注意：**不存在**「信任任何能重放的 manifest」的模式（review-1.4f3 M2）；可信的依據只有 `state` 與 `pending`。

### 3.4 上層同名檢查與清掃（第 4 步，`integrity/sweep.py`）

```python
@dataclass(frozen=True)
class PrefixLevel:
    parent_id: str; name: str; expected_id: str

def check_parents(levels: list[PrefixLevel], drive: DriveClient) -> list[DriveFile]:
    # 回傳要隔離的同名資料夾；expected_id 不在清單裡 → MismatchError；讀取錯誤 → ReadError

class Disposition(Enum):
    KEEP = "keep"
    QUARANTINE = "quarantine"
    GC = "gc"                         # 在 removed_bundles 裡：第 3 步不動，第 11 步永久刪除
    NEED_CONTENT_CHECK = "need_content_check"   # sha256Checksum 缺少：要下載驗證一次

@dataclass(frozen=True)
class SweepDecision:
    file: DriveFile; disposition: Disposition; reason: str

def plan_sweep(listing: RepoListing, state: PinState, *, repo_uuid: str) -> list[SweepDecision]:
    # 純函式，不碰網路。規則（design D2、review-1.4f3 H2/H3、review-1.4f5 H1）：
    # - 非 .bak 的 GITMANIFEST：只 KEEP 一個（sha256 == state.manifest_sha256）；其他 QUARANTINE
    # - .bak：sha256 ∈ {state.manifest_sha256, state.prev_manifest_sha256} 的保留一個；其他 QUARANTINE
    # - GITBUNDLE：名稱 ∈ active 而且 sha256 == 名稱內嵌的雜湊、size 相符 → KEEP（同內容重複的只留一個）
    #             名稱 ∈ removed → GC
    #             其他 → QUARANTINE
    # - annex 物件（SHA256E-s<N>--<sha>…）：key ∈ state.annex_keys 而且 sha256Checksum 相符 → KEEP；其他 QUARANTINE
    # - sha256 是 None 的檔 → NEED_CONTENT_CHECK（由 apply 前的步驟下載驗證，再重新判定）
    # - 子資料夾 → QUARANTINE（整個子樹；layout 是平的）
    # - 其他名稱 → QUARANTINE

def resolve_content_checks(decisions, drive, cache: ChecksumCache) -> list[SweepDecision]: ...
    # 下載 NEED_CONTENT_CHECK 的檔算 sha256，寫進 cache（存在真本的 _committer/checksums.json），重新判定

def apply_sweep(decisions: list[SweepDecision], drive: DriveClient, *, prefix_folder_id: str,
                quarantine_folder_id: str, dry_run: bool) -> int:
    # 只執行 QUARANTINE 的移動；任何一次 WriteError → raise AbortRun（這一輪中止）；回傳移動的數量
```
- **順序保證**：`plan_sweep` 先完整算完，才開始移動；只要計畫階段有任何 `ReadError`，就一個檔都不動。
- 讀取視圖資料夾也用同一套：讀取視圖的可信集合是「讀取視圖 manifest 列出的 file id」（第 4 組定義），第 3 組先只清掃 repo 資料夾，介面預留 `plan_readview_sweep(listing, readview_manifest)`。

### 3.5 核對、預檢、push 後驗證（`integrity/verify.py`）

```python
def verify_clone(git: AnnexGit, state: PinState) -> None:
    # 第 5 步：git.ls_remote() 的 ref 集合與值 == state.refs；遠端主 manifest（以 find_by_name
    #          找、只允許恰好一個）的 sha256Checksum == state.manifest_sha256；否則 MismatchError

def precheck(drive: DriveClient, prefix_folder_id: str, manifest_name: str, state: PinState) -> None:
    # 第 9 步：只查名稱符合的檔（find_by_name）；恰好一個、sha256 == state.manifest_sha256；否則中止

@dataclass(frozen=True)
class PushVerification:
    new_manifest_sha256: str; active: tuple[str, ...]; removed: frozenset[str]

def verify_after_push(git: AnnexGit, drive: DriveClient, listing_before: RepoListing,
                      state: PinState, local_refs: dict[str, str], push_started_at: str) -> PushVerification:
    # 第 10 步（review-1.2-1.6 H3、review-1.4f3 M1）：
    # 1. ls_remote == local_refs（全部 ref）
    # 2. 重新列舉，主 manifest 恰好一個；解析
    # 3. active 裡不在 state.active_bundles 的 bundle，其 created_time ≥ push_started_at，而且 listing_before 裡沒有同名檔
    # 4. removed ⊇ state.removed_bundles，而且 removed − state.removed_bundles ⊆ state.active_bundles
    # 5. 下載新增的 bundle，連同既有的 active 依序重放，refs == local_refs
    # 任何一條不符 → MismatchError（待定留著，下一輪由 settle 判定）
```

### 3.6 回收與隔離清理（`integrity/gc.py`）

```python
def collect_removed_bundles(listing: RepoListing, state: PinState) -> list[DriveFile]: ...
def gc_removed(files: list[DriveFile], drive: DriveClient, *, prefix_folder_id: str, dry_run: bool) -> int:
    # 第 11 步：永久刪除前，逐一 get() 確認 parents 包含 prefix_folder_id、名稱 ∈ state.removed_bundles（防呆，review-1.3c M1）
def purge_quarantine(drive: DriveClient, quarantine_folder_id: str, *, older_than_days: int, now: str, dry_run: bool) -> int: ...
```
- 依 design D2「第 11 步回收」：用**新的**正式釘選值的 removed 清單。第一次回收（大量舊 bundle）要設上限，例如每一輪最多刪 200 個，其餘留到下一輪，避免單一輪超時。

---

## 4. 收件匣處理（3.3、3.4，`aistorage.intake`）

### 4.1 掃描

```python
# intake/scan.py
@dataclass(frozen=True)
class InboxItem:
    item_key: str
    inbox_folder_id: str
    sidecar: DriveFile | None
    sig: DriveFile | None
    raw: DriveFile | None
    extras: tuple[DriveFile, ...]    # 名稱不符合格式的檔（不算項目；24 小時後當孤兒清掉）

def scan_inboxes(drive: DriveClient, registry: Registry) -> list[InboxItem]:
    # registry.inbox_folders() 的每個資料夾；檔名依 <ULID>.(raw|sidecar.json|sig) 分組
def is_actionable(item: InboxItem) -> bool: ...
    # 有 sig 與 sidecar → True（raw 的需要與否在讀 sidecar 之後判定）
def count_shaped(items: list[InboxItem]) -> int: ...
    # 第 2 步用：只算「形狀符合」（有 sig 與 sidecar）的項目數，0 就結束（review-1.8 L4）
```
- 第 2 步在**安裝 git-annex 之前**跑（workflow 裡是獨立的 step，見第 7 節），只需要 `drive` 與登錄檔。

### 4.2 評估（驗章 → authorize → 格式 → 防重放 → dedup）

```python
# intake/evaluate.py
class DecisionKind(Enum):
    ACCEPT = "accept"; REJECT = "reject"; DEFER = "defer"; ALREADY = "already"   # ALREADY：已經收過，只需刪除

@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    item: InboxItem
    code: str                        # 例：bad_signature、unauthorized、stale、collision、orphan、raw_mismatch、foundry_not_enabled
    producer: str | None = None
    record_metadata: dict | None = None
    sidecar: dict | None = None
    raw_path: Path | None = None

def evaluate(item: InboxItem, *, drive: DriveClient, registry: Registry, store: AgoraStore,
             ledger: Ledger, clock: Clock, workdir: Path, max_raw: int) -> Decision:
    # 1. 缺 sig：raw 或 sidecar 的 created_time 超過 24 小時 → REJECT(orphan)；否則 DEFER
    # 2. sidecar_bytes = download_bytes(sidecar, max_bytes=1 MiB)；sig = json(download_bytes(sig, 4 KiB))
    # 3. folder_profile = registry.inbox_folders()[item.inbox_folder_id]
    #    key_id = verify_sidecar_bytes(sidecar_bytes, sig, registry.active_public_keys(folder_profile))；None → REJECT(bad_signature)
    #    （只用「這個收件匣所屬 profile」的金鑰驗章：放錯收件匣的項目直接失敗，不需要新增全域金鑰查詢）
    # 4. sidecar = strict_json(sidecar_bytes)（object_pairs_hook 拒絕重複 key）
    #    errs = validate_sidecar(sidecar, expected_item_key=item.item_key)；有錯 → REJECT(invalid_format)
    # 5. sidecar["profile"] != folder_profile → REJECT(unauthorized)
    #    producer, why = registry.authorize(sidecar, key_id)；None → REJECT(unauthorized)
    # 6. ledger.contains(item.item_key)：同一個 raw sha → ALREADY；不同 → REJECT(replayed_item_key)
    # 7. 需要 raw：raw.size（Drive metadata）> max_raw → REJECT(too_large)（先看 metadata，不下載）
    #    下載到 workdir 後 check_raw(sidecar, file)；有錯 → REJECT(raw_mismatch)
    # 8. session：snapshot_at = min(sidecar.session.snapshot_at, sidecar 檔的 created_time)（D4 的上限）
    # 9. record = stamp_record(sidecar["metadata"], producer=producer)（見下）；
    #    existing = store.get_record(record["id"])；classify_id(existing, record)：collision → REJECT(collision)
    # 10. 單調性（防重放，review-2.2 H3）：
    #     session：existing 的 raw_sha256 == 這次的 → ALREADY（3.4 dedup）；snapshot_at ≤ existing 的 → REJECT(stale)
    #     reference：read_snapshot_at ≤ existing 的 → REJECT(stale)
    #     其他：updated_at ≤ existing 的，而且內容相同 → ALREADY；更舊 → REJECT(stale)
    # 11. artifact → DEFER(foundry_not_enabled)（第 7 組之前留在收件匣，不刪也不當孤兒）
    # 12. 否則 ACCEPT

def stamp_record(inbox_metadata: dict, *, producer: str) -> dict:
    # 唯一的蓋章入口（review-2.1 L2）：strip_claimed_producer → 設 producer → 補 case_id/provenance 為 null
    # → validate_record_metadata；不通過 → raise（程式錯誤，不是輸入錯誤）
    # committed_at 在 commit 前才補（run.py）
```

### 4.3 處理過的 item_key 清冊

```python
# intake/ledger.py
class Ledger:
    def __init__(self, store: AgoraStore): ...
    def contains(self, item_key: str) -> LedgerEntry | None: ...
    def record(self, item_key: str, *, item_id: str, decision: str, raw_sha256: str | None, at: str) -> None: ...
```
- 存放位置：真本 repo 的 `_committer/ledger/<YYYY-MM>.jsonl`（一行一筆，只有 id、雜湊、代碼、時間，**不含內容**）。查詢時載入最近 N 個月（建議 3）；更早的由單調性檢查兜底（item_key 本身帶時間，ULID 早於 N 個月的直接 REJECT(too_old)）。
- REJECT 也記進清冊（附代碼），這樣同一個 item_key 重新上傳不會被重複評估。

### 4.4 分派順序

同一輪 ACCEPT 的項目依下面的順序套用（design D2「先收原始紀錄、再收交接單與認領」）：
`session`（依 snapshot_at 由舊到新）→ `rewrite` → `handoff` → `claim` → `reference`。
套用失敗（例如交接單的接續點不存在）轉成 REJECT，**不影響同一輪其他項目**；但任何 I/O 錯誤都中止整輪。

---

## 5. 轉換器（3.5、3.6，`aistorage.converters`）

```python
# converters/base.py
@dataclass(frozen=True)
class SessionFacts:
    title: str | None
    created_at: str | None
    updated_at: str | None
    message_ids: tuple[str, ...]          # 閱讀版的順序
    archived_at: str | None               # opencode 的 time.archived（>0 才有值）
    last_message_at: str | None           # 3.9：判斷「封存之後有沒有新訊息」
    in_progress: bool

class Converter(Protocol):
    source: str                                            # "opencode" | "claude-code"（P4）
    def facts(self, raw_path: Path) -> SessionFacts: ...
    def convert(self, raw_path: Path, *, session_id: str, snapshot_sha256: str,
                parent_id: str | None) -> dict: ...        # 回傳閱讀版 v1；結果必須通過 validate_reading
    def child_session_ids(self, raw_path: Path) -> tuple[str, ...]: ...   # 子代理（task／sidechain）

# converters/__init__.py
CONVERTERS: dict[str, Converter]
def get_converter(source: str) -> Converter: ...           # 不認得 → KeyError（evaluate 轉成 REJECT(unknown_source)）
```
- **純函式**：不碰網路、不碰 git，只讀本機檔案。這讓 3.5、3.6 可以跟其他模組完全並行開發。
- 依 `schemas/reading-version.md` 的轉換對應表實作；摘要截斷以 code point 計（4,000，含「…」）。
- opencode 的 `info.revert` → 指標之後的訊息 `reverted: true`；Claude Code 沿最新的葉節點展開，其他分支標 `reverted: true`；依 `tool_use_id` 配對組成 `tool_call`。
- 轉換器的輸出**不進真本 repo**（見第 6 節），只在第 7 步用來做檢查（接續點、改寫的位置）與之後的讀取視圖。

---

## 6. Agora 真本資料模型（3.7〜3.10）

### 6.1 repo 內佈局（`agora/layout.py`）

```
sessions/<source>/<enc(source_session_id)>/
  meta.json            # 真本 metadata（2.1 record）＋ session 欄位（見下）
  raw                  # 原始紀錄本體（git 或 annex 由 2.6 決定；路徑固定，不帶副檔名）
  snapshots.jsonl      # 每次收進的快照一行：{snapshot_sha256, snapshot_at, raw_size, item_key, committed_at,
                       #   git_blob（raw 在 git 時）或 annex_key（raw 在 annex 時）}
handoffs/<ULID>.json   # 交接單：真本 metadata＋body＋{claimed_by: {claim_id, session_id, at} | null}
links/continuation/<enc(new_session_id)>/<handoff ULID>.json   # 接續 Link：from、to、continuation、handoff_id、claim_id
links/reference/<enc(from_session_id)>/<enc(to_session_id)>.json  # 同一對只有一個檔，覆寫成最新的 read_snapshot_at
claims/<ULID>.json     # 認領的真本紀錄（metadata＋body＋結果）
rewrites/<ULID>.json   # 改寫提案的真本紀錄：metadata＋body＋{applied_snapshot_sha256}
_committer/
  ledger/<YYYY-MM>.jsonl
  rejections/<item_key>.json     # {code, at, item_id?}；不含內容；第 4 組發佈到讀取視圖
  checksums.json                 # sha256Checksum 缺少時的下載驗證結果（file id → sha256）
  schema_version                 # "agora/v1"
```
- `enc()` 用 `urllib.parse.quote(s, safe="-_.")`，保證路徑安全、可逆；**冒號不會出現在路徑裡**。
- session 的 `meta.json` 額外欄位：`status`、`stopped_at`、`snapshot_at`、`raw_sha256`、`raw_size`、`parent_id`、`in_progress`、`archived_at`、`committed_at`、`last_item_key`、`title`。
- **閱讀版不放在真本**（它是衍生物，design D5）。需要某個舊快照的閱讀版時（接續、第 4 組），用 `snapshots.jsonl` 找到 `git_blob` 或 `annex_key` 取出那一份 raw，再跑轉換器。這也讓「從被釘住的快照讀」（D10）有明確的實作路徑。
- commit 訊息只寫計數與 item_key（不寫標題或內容，design D2 的 log 規則同樣適用於 git 歷史）。

### 6.2 `AgoraStore` 與套用

```python
# agora/store.py
class AgoraStore:
    def __init__(self, worktree: Path, raw_storage: RawStorage): ...
    def get_record(self, item_id: str) -> dict | None: ...
    def get_session(self, session_id: str) -> SessionRecord | None: ...
    def snapshots(self, session_id: str) -> list[SnapshotEntry]: ...
    def raw_path_for_snapshot(self, session_id: str, snapshot_sha256: str) -> Path: ...   # 取出舊版 raw 到暫存
    def put_session(self, rec: SessionRecord, raw_src: Path) -> None: ...
    def put_json(self, relpath: str, obj: dict) -> None: ...     # 固定格式：sort_keys、indent=2、結尾換行
    def changed_paths(self) -> list[str]: ...

class RawStorage(Protocol):                     # 2.6 的決定落在這裡
    def store(self, worktree_path: Path, src: Path) -> str: ...        # 回傳 git_blob 或 annex_key
    def retrieve(self, ref: str, dest: Path) -> None: ...
# GitRawStorage（git add）與 AnnexRawStorage（git annex add，largefiles=anything）兩種實作

# agora/apply.py —— 全部回傳 ApplyResult(ok: bool, code: str, paths: list[str])
def apply_session(store, dec: Decision, conv: Converter, clock: Clock) -> ApplyResult: ...
    # 寫 raw、追加 snapshots.jsonl、更新 meta.json；3.9：
    #   facts.archived_at 而且 facts.last_message_at ≤ archived_at → status=stopped，stopped_at＝sidecar 的 stopped_at（同步器觀測）
    #   否則 running（封存之後又有新訊息 → 回到運作中）
    # 子 Session：meta.json 記 parent_id（來自 sidecar）
def apply_rewrite(store, dec, conv) -> ApplyResult: ...
    # base_snapshot_sha256 必須等於目前的 raw_sha256；新 raw 的閱讀版與舊的相比，既有訊息的 message_id 序列與 index 完全相同
    # （不改變位置）；否則 REJECT(position_changed)。來源端自己的刪改走 apply_session（新版本），不走這裡。
def apply_handoff(store, dec, conv) -> ApplyResult: ...
    # continuation.snapshot_sha256 必須在 target 的 snapshots（含這一輪剛收的）裡；取出那份 raw → convert →
    # reading.check_continuation；不通過 → REJECT(invalid_continuation)
def apply_claim(store, dec) -> ApplyResult: ...
    # 交接單存在、claimed_by 是 null、claimer_session_id 在 Agora（或這一輪剛收）→ 寫 claim、設 claimed_by、
    # 建接續 Link（方向：claimer → target）；否則 REJECT(already_claimed / unknown_handoff / unknown_claimer)
def apply_reference(store, dec) -> ApplyResult: ...
    # 同一對只留一個檔；read_snapshot_at 單調（evaluate 已經擋過，這裡再防一次）
```

---

## 7. workflow 與 CLI

### 7.1 `.github/workflows/committer.yml`（單一 job）

```yaml
on:
  schedule: [{cron: "7 */6 * * *"}]   # 預設 6 小時；分鐘避開整點
  workflow_dispatch: {}               # 沒有任何 inputs
concurrency: {group: committer-agora, cancel-in-progress: false}
permissions: {contents: read}
jobs:
  commit:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@<pinned sha>
      - name: guard            # github.ref == refs/heads/main 且 github.sha == main HEAD（用 GITHUB_TOKEN 查 API）
      - uses: astral-sh/setup-uv@<pinned sha>     # 只快取依賴，不快取任何內容
      - name: inbox-prescan    # uv run python -m aistorage.committer prescan → 0 就 exit 0（這時還沒裝 git-annex）
      - name: install-tools    # git-annex、rclone：固定版本＋sha256（同 spike/env/Dockerfile）
      - name: secrets-to-files # RCLONE_CONF → $RUNNER_TEMP/rclone.conf（600，可寫的暫存複本）；PIN_DEPLOY_KEY → 600
      - name: run              # uv run python -m aistorage.committer run --config config/committer.json
```
- 環境變數只傳**路徑**：`AISTORAGE_RCLONE_CONF`、`AISTORAGE_PIN_KEY`、`AISTORAGE_PIN_KNOWN_HOSTS`（repo 內的檔）。
- log：`RunReport` 只印計數、步驟耗時、中止的步驟與代碼；不印檔名以外的內容（檔名是 ULID）。

### 7.2 CLI（`python -m aistorage.committer`）

| 子命令 | 用途 | 寫入？ |
|---|---|---|
| `prescan` | 第 2 步：形狀符合的收件匣項目數；0 就回傳 exit 0 並印 `EMPTY` | 否 |
| `run [--dry-run]` | 13 步；`--dry-run` 走完所有判定、印出計畫，不移動、不寫 pin、不 push | 是（非 dry-run） |
| `plan-sweep` | 只跑第 3〜4 步的判定並印出處置 | 否 |
| `init-pin --dry-run / --confirm` | 首次建立正式釘選值（只在 Mac、管理者身分） | 只有 `--confirm` |

本機執行（整合測試、手動）：
```
AISTORAGE_RCLONE_CONF=~/.config/aistorage/rclone-committer-test.conf \
AISTORAGE_PIN_KEY=~/.config/aistorage/pin-test.key \
uv run python -m aistorage.committer run --config config/committer.test.json --dry-run
```
- `run` 的第 1 步（github.sha 檢查）在非 Actions 環境下跳過，並在 RunReport 標明 `guard=local`。

### 7.3 `committer/run.py` 的骨架

```python
@dataclass
class Deps:
    drive: DriveClient; pins: PinStore; git_factory: Callable[[Path], AnnexGit]
    registry: Registry; converters: dict[str, Converter]; publisher: ReadViewPublisher; clock: Clock

@dataclass
class RunReport:
    run_id: str; aborted_at: str | None; code: str | None
    counts: dict[str, int]; durations_ms: dict[str, int]

def run(cfg: CommitterConfig, deps: Deps, *, dry_run: bool = False) -> RunReport:
    # 每一步是一個小函式；失敗 raise AbortRun(step, code)；run 捕捉後填 RunReport（exit code 非 0）
```
- 13 步與模組的對應：1 `guard`；2 `intake.scan`；3 `integrity.settle`；4 `integrity.sweep`；5 `annex.git.clone`＋`integrity.verify.verify_clone`；6 `annex.git`；7 `intake.evaluate`＋`agora.apply`；8 `pins.write_pending`；9 `verify.precheck`＋`git.copy`＋`git.push`；10 `verify.verify_after_push`；11 `pins.promote`＋`gc.gc_removed`；12 `publisher.publish`（第 3 組是 NullPublisher）；13 刪除 ACCEPT、ALREADY 與逾時 REJECT 的收件匣項目（`drive.delete_permanently`，刪前 `get()` 確認 parents 是收件匣）。

### 7.4 手動匯入（3.11，`python -m aistorage.importer`）

```
python -m aistorage.importer opencode --export <opencode export 的 JSON 檔> --key <私鑰路徑> --key-id <id> \
       --profile mac-opencode (--inbox-folder <id> | --out-dir <本機目錄>)
python -m aistorage.importer claude-code --jsonl <檔> ...（同上）
```
- 共用一個 `build_inbox_item(raw_path, *, source, source_session_id, facts, profile, key, key_id) -> (sidecar_bytes, sig_obj)`。**同步器（5.2）之後也用這個函式**，匯入與同步產生的收件匣項目形式相同（spec「單一 Session 手動匯入」）。
- 重複匯入：id＝`<source>:<source_session_id>`，內容相同 → 提交流程判為 ALREADY（3.4）。

---

## 8. 並行、順序與測試策略

### 8.1 相依與順序

```
A（可以全部並行）   drive/*（含 fake）  │ annex/manifest+replay │ converters/opencode │ converters/claude_code │ agora/layout+store（純檔案）
B（依賴 A）         integrity/pin（需要 P2/P3）│ integrity/settle+sweep+verify+gc │ intake/scan+evaluate+ledger │ agora/apply
C（依賴 B）         committer/run＋CLI＋workflow │ importer
D（依賴 C）         整合測試（TEST_FOLDER_ID）、中斷注入、bundle 回收的容忍度（3.2 的必要驗收）
```
建議分派：impl-1：A 的 drive＋annex → B 的 integrity；impl-2：A 的兩個轉換器（3.5、3.6）；impl-3：A 的 agora → B 的 intake＋apply；PM 或 impl-1：C。測試方照第 2 組的分工（不看實作、依本草案的介面寫）。

### 8.2 單元測試（每個模組一個檔，直接 import）

| 模組 | 重點案例 |
|---|---|
| drive/http | 用假的 HTTP 伺服器（`http.server` 或 monkeypatch）驗：分頁、429 重試後成功、重試用完 → ReadError、404 → NotFound、下載超過上限中止 |
| annex/manifest、replay | `-` 行、空 active、非法行 → MismatchError；依順序重放；最後一個 bundle 決定 ref 集合；解不開 → MismatchError |
| integrity/settle | 決策表：無待定／遠端＝待定／遠端＝正式／兩者皆非／只剩 `.bak`／兩個同名候選都相符（→ 中止）／任何 ReadError（→ 往上拋，FakeDrive 快照不變） |
| integrity/sweep | 每一條規則一個案例；**性質測試**：任何 ReadError 注入之下，`apply_sweep` 從來不會被呼叫（FakeDrive 快照不變）；上一版 manifest 用非 `.bak` 名稱冒充 → QUARANTINE；沒被引用的 annex 物件 → QUARANTINE；removed → GC 而不是 QUARANTINE；`sha256=None` → NEED_CONTENT_CHECK |
| integrity/verify | push 後：多一個 bundle 但 created_time 早於 push → 中止；removed 少了舊的 → 中止；ref 多一個 → 中止 |
| integrity/pin | MemoryPinStore 的狀態機；GitPinStore 用本機 bare repo（`file://`）測 commit、non-ff 中止 |
| intake/evaluate | 決策表：沒有 sig（新的→DEFER、舊的→orphan）、簽章錯、重複 key 的 sidecar、item_key 不符、冒充 profile、撤銷、type 不允許、raw 太大（只看 metadata、不下載）、raw 雜湊錯、重放（同 item_key、舊 snapshot）、ALREADY（同 raw 雜湊）、collision、artifact → DEFER |
| agora/apply | 臨時目錄＋FakeRawStorage：接續點不在 snapshots → REJECT；接續點未完成／已撤銷 → REJECT；重複認領；統合（一個 Session 認領兩張）；參考 Link 同一對只留一個、單調；改寫改變位置 → REJECT；封存後又有新訊息 → running |
| converters | 黃金樣本（自己編，形狀取自 spike 1.7 與 Claude Code jsonl 的欄位）：輸入 → 預期閱讀版逐欄相同，而且通過 `validate_reading`；revert、壓縮、子代理、reasoning、tool 錯誤、Claude Code 分支與 tool 配對 |
| committer/run | 全部用 fake：正常一輪、每一步注入失敗都會中止而且後續步驟沒有執行、dry-run 沒有任何寫入 |

### 8.3 整合測試（`tests/integration/`，`pytest -m integration`）

- 設定：`~/.config/aistorage/ids.env` 的 `TEST_FOLDER_ID`、`~/.config/aistorage/rclone-committer-test.conf`、`~/.config/aistorage/pin-test.key`，**只以路徑引用**。**選了 integration 標記但設定缺少時要 FAIL，不能 skip**（review-2.1 H1 的教訓）；平常 `pytest` 預設不選 integration（`addopts = -m "not integration"`），所以不會因為缺設定而失敗。
- 每個測試在 `TEST_FOLDER_ID` 底下建自己的前綴（例如 `it-<ULID>/`），測完依 file id 永久刪除（先 `get()` 確認 parents）。
- 必要案例（對應 tasks 9.4 與 design 的「3.2 驗證」）：
  1. 一輪正常提交：session＋handoff＋claim 同一批，接續 Link 建立；ALREADY（重複上傳）不產生 commit。
  2. 中斷注入（用環境變數 `AISTORAGE_TEST_KILL_AFTER=<step>` 讓 run 在指定步驟之後 `os._exit`）：push 後、轉正前 → 下一輪 PROMOTED，真檔沒被隔離；寫待定後、push 前 → DROPPED。
  3. 注入：真 main＋偽造 git-annex、上一版 manifest 冒充、沒被引用的 annex 物件、上層同名資料夾 → 偵測、隔離、下一輪恢復。
  4. **bundle 回收的容忍度**：設小的 `max-git-bundles` 觸發 consolidate，回收 removed 之後 clone、push、再 consolidate 都正常（design D2 明文要求在 3.2 驗證）。
  5. 暫時性錯誤：在 HttpDriveClient 外包一層注入 503 → 中止，沒有任何移動。

---

## 9. 需要 PM 決定的事

1. Claude Code 的 source 名稱：`claude-code` 還是 `claude_code`（P4）。
2. 舊骨架目錄（`committer/`、`syncers/`、`search/`、`admin/`）移除，還是改成指向 `src/aistorage` 的說明（第 1 節）。
3. 處理過的 item_key 清冊的保留月數（建議 3），以及「比 N 個月更早的 ULID 直接拒收」這條規則（4.3）。
4. 第一次 bundle 回收每一輪的上限（建議 200）（3.6）。
5. artifact 在第 7 組之前一律 DEFER（留在收件匣），還是 REJECT（4.2 第 11 點）。建議 DEFER。

---

## PM 的決定（2026-09-27）

1. Claude Code 的 source 名稱：`claude-code`（同步修正 `schemas/reading-version.md` 與 `reading-version.schema.json` 的例子）。
2. 根目錄的舊骨架目錄（`committer/`、`syncers/`、`search/`、`admin/`、`atelier-template/`）只留 README，內容改成「程式在 `src/aistorage/<模組>`」，由 3.1 一併處理。
3. 處理過的 item_key 清冊保留 3 個月；ULID 時間早於 3 個月的收件匣項目直接拒收。
4. 第一次 bundle 回收每一輪最多處理 200 個，其餘留到下一輪。
5. artifact 在第 7 組之前一律 DEFER（留在收件匣）。
6. 前置 P2、P3 已完成：`MyAiStorage-pin`（Actions secret `PIN_DEPLOY_KEY`）、`MyAiStorage-pin-test`（deploy key 私鑰 `~/.config/aistorage/pin-test.key`）。整合測試的 Drive 資料夾 id 在 `~/.config/aistorage/ids.env` 的 `TEST_FOLDER_ID`，rclone 設定 `~/.config/aistorage/rclone-committer-test.conf`（只以路徑引用）。

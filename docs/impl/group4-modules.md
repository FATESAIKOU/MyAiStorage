# 第 4 組（讀取：tasks 4.1〜4.5）：模組切分與介面草案（架構師）

依據：
- design D5（讀取視圖、以 id 定位、增量發佈、門檻）、D9 與 ADR 0007（新鮮度）、D2 第 4 步與第 12 步、D3（讀取身分＝共用的 SA）、D4 與 Q3（同步器以 Agora 比對）
- spec `agora/search`
- 技術驗證 1.4i（`files.update` 原地更新，id 不變，讀者約 2〜5 秒後看得到）
- 技術驗證 1.5（SA 可以讀、可以 clone；**沒有儲存配額**，不能建立檔案；對 committer 建立的檔案不能 update）
- 第 3 組的現況（`AgoraStore`、`reading.py`、`converters`、`run.py` 第 4 步與第 12 步、`plan_readview_sweep`）
- impl2 正在實作的 4.2 草案 `g4-api-4.2.md`（第 3 節列出需要調整的地方）

格式與第 3 組相同：函式簽名與檔案佈局可以直接照做；標了「**PM 決定**」的地方需要先拍板（第 9 節有彙整）。

---

## 0. 核心設計（先讀這段）

1. **搜尋索引就是讀取視圖的目錄（catalog）。** SQLite 檔除了全文，也放 Session 的 metadata、快照歷史、Link（兩個方向）、交接單、拒收原因，以及每一份閱讀版的 file id。這樣讀取視圖只有三種檔案：
   - **manifest**（固定 id）
   - **index**（每一個世代一份）
   - **reading**（每一個「Session × 快照」一份，內容定址、不可變）

   理由：
   - 檔案數量維持在 N（Session 數）＋被釘住的快照數，遠低於 D5 的 5,000 個檔案門檻；
   - 讀者一個世代只需要下載一次 index；
   - TS 用戶端本來就要實作 SQLite 查詢，不必再多一套 JSON 格式。（**PM 決定 1**）
2. **內容檔不可變，只有 manifest 原地更新。** reading 與 index 每次都建立**新檔**（內容定址），manifest 以 `files.update` 原地切換到新的世代；舊檔**保留一個世代**才刪除。讀者拿到任何一份 manifest，裡面引用的檔案都一定還在，而且內容與雜湊一致，不會讀到「半新半舊」的狀態。D5 的「只重寫有變動的檔案」照樣成立，因為沒有變動的 reading 會沿用同一個 id。（**PM 決定 2**）
3. **信任錨點＝manifest 的固定 id。** 只有提交流程能 update 它：SA 與 `drive.file` 的住民對 committer 建立的檔案都是 403（1.5）。讀者只依 manifest 與 index 裡記錄的 id 取檔，並以記錄的 sha256 驗證內容。整條路徑都**不以名稱搜尋**（D5）。
4. **讀取端沒有任何寫入的程式路徑**：讀取函式庫只使用 `DriveClient` 的讀取方法，SA 本身也沒有寫入能力（1.5）。測試要斷言讀取時沒有任何 WRITE_OPS（ADR 0007：讀取不觸發寫入）。

---

## 1. 套件佈局

```
src/aistorage/
  readview/                 # 讀取視圖的格式（提交流程與讀者共用；純資料＋純函式）
    __init__.py
    model.py                # Manifest、FileRef、ReadingRef、常數（format 字串）
    naming.py               # 內容定址的檔名（只是資訊用途，讀取一律以 id 定位）
  search/                   # 4.2（impl2 實作中）：索引的建立與查詢
    __init__.py
    schema.sql              # 索引 schema（規格的一部分，TS 用戶端照抄）
    index.py                # build_index、IndexEntry、IndexStats
    query.py                # Query、search、get_*（純 SQLite，不碰網路）
  publish/                  # 4.1：提交流程第 12 步（只在 committer 端使用）
    __init__.py
    plan.py                 # plan_publish（純函式）：由真本狀態＋舊 manifest 算出要建立、保留、退役的檔案
    publisher.py            # DriveReadViewPublisher（實作 committer.publish.ReadViewPublisher）
    rejections.py           # 蒐集要發佈的拒收原因（真本的＋本輪驗章前的）
  reader/                   # 4.3、4.4：讀取介面函式庫＋CLI（arm64／amd64 都要能跑）
    __init__.py             # AgoraReader、Freshness、錯誤型別
    client.py               # ReadViewClient：manifest、index、reading 的下載、驗證與快取
    freshness.py            # evaluate_freshness（純函式）
    config.py               # ReaderConfig（manifest id、SA 憑證路徑、快取目錄）
    __main__.py             # CLI：python -m aistorage.reader ...
  drive/
    sa_auth.py              # 新增：ServiceAccountToken（JWT bearer，RS256），給 HttpDriveClient 使用
  committer/
    rebuild.py              # 4.5：rebuild-readview（驗證模式、完整重新發佈）
schemas/
  readview-manifest.schema.json
  searchindex.md            # 索引格式＋查詢語意的規格（TS 用戶端的依據）
tests/unit/
  test_readview_model.py、test_publish_plan.py、test_publisher.py、test_search_*.py、
  test_reader.py、test_freshness.py、test_rebuild.py
  data/search/golden/       # 黃金索引的輸入＋查詢→預期結果（TS 用戶端共用）
tests/integration/
  test_readview_roundtrip.py
```

---

## 2. 讀取視圖的格式（`readview/model.py`、`schemas/readview-manifest.schema.json`）

### 2.1 Drive 上的佈局

讀取視圖資料夾是**平的**（沒有子資料夾，子資料夾在清掃時會被隔離）：

| 檔案 | 名稱（僅供資訊用途） | 寫入方式 |
|---|---|---|
| manifest | `readview-manifest.json` | **固定 id**，只用 `files.update` 原地更新；由管理者建立一次，id 寫進 `config/committer.json` 與讀者設定 |
| index | `index-g<世代>-<sha256 前 12 碼>.sqlite` | 每個世代 `create` 一份新檔 |
| reading | `reading-<sha256(session_id) 前 16 碼>-<snapshot_sha256 前 16 碼>.json` | `create`；內容定址，**永遠不更新** |

### 2.2 manifest

```python
@dataclass(frozen=True)
class FileRef:
    id: str; sha256: str; size: int

@dataclass(frozen=True)
class Manifest:
    format: str                   # "aistorage.readview/v1"
    element: str                  # "agora"（期 1 只有 Agora，ADR 0009）
    generation: int               # 單調遞增，從 1 開始
    published_at: str             # RFC 3339 UTC（到毫秒）
    agora_main_sha: str           # 這個世代對應的真本 main commit（第 12 步用來判斷要不要發佈）
    converter_versions: dict[str, str]   # {"opencode": "1", "claude-code": "1"}；變動就觸發閱讀版重建（見 4.5）
    index: FileRef                # index 的格式版本寫在 index 自己的 meta 表
    files: tuple[str, ...]        # 這個世代引用的全部 file id（index＋全部 reading），給清掃當可信集合
    retired: tuple[tuple[str, int], ...]  # (file_id, 退役時的世代)；下一個世代才刪除

def parse_manifest(data: bytes) -> Manifest: ...   # schema 驗證；不符 → MismatchError
def serialize_manifest(m: Manifest) -> bytes: ...  # sort_keys、indent=2、結尾換行
def trusted_ids(m: Manifest, manifest_file_id: str) -> frozenset[str]: ...  # {manifest_id} ∪ files ∪ retired
```

- reading 的 file id **不放在 manifest**，放在 index 的 `readings` 表。所以 manifest 很小（`files` 這個清單在 5,000 個 Session 時大約 250 KB）。
- manifest 本身**不必簽章**：只有提交流程能寫入，而讀者只依固定的 id 讀取。

### 2.3 reading 檔

閱讀版 JSON 就是 `aistorage.reading/v1`（`reading.py` 的 schema），內容定址（`snapshot_sha256`），原樣發佈（包括 reverted 的訊息與 reasoning；過濾交給讀者端）。

發佈的範圍：
- 每個 Session 的**最新**快照；
- 以及每一個**被交接單或接續 Link 釘住**的快照（D10：從被釘住的快照讀）。

閱讀版轉換失敗（`reading_status=failed`）的快照不發佈 reading，index 會記下失敗的代碼。

---

## 3. 搜尋索引（4.2）— 對 `g4-api-4.2.md` 的調整

impl2 的草案方向正確（SQLite FTS5 trigram、全量重建、原子地改名、`<3` 字元時退回 LIKE、50 MB 門檻）。**需要調整的地方如下**，其中 A〜D 會影響 schema，建議在 impl2 繼續寫下去之前就定案。

**A. 索引也是目錄（catalog）**（見第 0 節第 1 點）：除了 `sessions` 與 `session_fts`，再加入下面的表：

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- format='aistorage.searchindex/v1'、generation、built_at、agora_main_sha、converter_versions(JSON)
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, producer TEXT NOT NULL,
  case_id TEXT, status TEXT NOT NULL, stopped_at TEXT, in_progress INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  snapshot_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL, raw_size INTEGER NOT NULL,   -- Q3 同步器比對用
  parent_id TEXT, reading_status TEXT NOT NULL, reading_error_code TEXT,
  committed_at TEXT NOT NULL
);
CREATE TABLE snapshots (session_id TEXT, snapshot_sha256 TEXT, snapshot_at TEXT, committed_at TEXT,
  via TEXT, PRIMARY KEY (session_id, snapshot_sha256));
CREATE TABLE readings (session_id TEXT, snapshot_sha256 TEXT, file_id TEXT NOT NULL, sha256 TEXT NOT NULL,
  size INTEGER NOT NULL, is_latest INTEGER NOT NULL, PRIMARY KEY (session_id, snapshot_sha256));
CREATE TABLE links (kind TEXT NOT NULL,            -- 'continuation' | 'reference'
  from_session_id TEXT NOT NULL, to_session_id TEXT NOT NULL,
  handoff_id TEXT, claim_id TEXT, snapshot_sha256 TEXT, message_id TEXT,    -- 接續用
  reference_id TEXT, read_snapshot_at TEXT);                                -- 參考用
CREATE INDEX links_to ON links(to_session_id);       -- 反向索引（指向它的 Link）
CREATE TABLE handoffs (handoff_id TEXT PRIMARY KEY, target_session_id TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL, message_id TEXT NOT NULL, producer TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, case_id TEXT,
  body_json TEXT NOT NULL,                           -- 寫入者提供的交接內容（原樣）
  claimed_by_claim_id TEXT, claimed_by_session_id TEXT, claimed_at TEXT,
  author_session_id TEXT);                           -- 見下（PM 決定，2026-09-28）
CREATE TABLE rejections (item_key TEXT PRIMARY KEY, code TEXT NOT NULL, at TEXT NOT NULL,
  item_id TEXT, authenticated INTEGER NOT NULL);     -- 只有代碼，沒有內容
```

**`handoffs.author_session_id`（PM 決定 2026-09-28）**：寫這張交接單的那個 Session 的 id，
**由提交流程（publisher）填成 `target_session_id`**。理由：`apply_handoff` 的持有者檢查
已經保證寫交接單的就是被接續 Session 的持有者，而 D10 的接續本來就由被接續 Session 的
持有者發起，所以「目標」即「作者」，不需要寫入端多提供一個欄位。寫入端若在
`body.author_session_id` 或 metadata 明確指定則以它為準（為將來預留；期 1 的同步器
不提供）；連目標都缺才會是 NULL，讀取端視為作者不明而排除（第 9 節決定 9：
列待認領的交接單時只看主 Session 寫的）。語意細節寫在 `schemas/searchindex.md` 第 1 節。

**B. 全文以「訊息」為單位，而不是整個 Session 一筆。** spec 要求「標出命中的位置」，交接與接續也以 message_id 定位。

```sql
CREATE VIRTUAL TABLE message_fts USING fts5(
  text, session_id UNINDEXED, message_id UNINDEXED, idx UNINDEXED, tokenize='trigram');
CREATE VIRTUAL TABLE title_fts USING fts5(title, session_id UNINDEXED, tokenize='trigram');
```

- 只放**最新快照**的閱讀版。
- `text` 取每一則訊息的 `plain_text`（不含 reverted、不含 reasoning）。
- 查詢結果回傳命中的 `message_id` 與 `index`，以及 snippet。

**C. 查詢語意要寫成與實作無關的規格**（`schemas/searchindex.md`），TS 用戶端才能照做。FTS 只是加速手段：
- 「`text` 命中」的定義：訊息文字**包含** query 字串（子字串比對，只有 ASCII 不分大小寫；**不做** NFKC 或全形半形的轉換）。這正好等於 SQLite trigram tokenizer 的預設語意，以及 `LIKE`（ASCII 不分大小寫）的語意，所以 3 字元以上走 FTS、未滿 3 字元走 LIKE，**結果完全相同**。`matched_by` 只是除錯資訊，不屬於規格。
- 多個詞：以空白分隔，**全部**都要命中（AND）；支援 `"…"` 引號包住的片語。期 1 **不支援** FTS5 的運算子語法，query 在程式裡要逐字跳脫。
- 排序：**`updated_at` 由新到舊，平手時依 `session_id`**，**不使用** bm25，讓 Python 與 TS 的結果在逐筆比較時完全一致。
- 分頁：`limit`（預設 50，上限 500）＋`cursor`（上一頁最後一筆的 `(updated_at, session_id)`）。

**D. 篩選**要補上 spec 列出的欄位：
- `title_contains`（子字串，語意同 C）
- `case_id`、`source`、`status`、`producer`
- `updated_after`、`updated_before`（RFC 3339，**依 datetime 比較**；索引裡的時間一律存成 UTC 到毫秒的**固定寬度**字串，所以字串比較與時間比較的順序才會一致）
- `parent_id`：`None` 表示不限；`""` 表示只要主 Session
- `in_progress`

**E. `build_index` 的輸入改由 publisher 提供：** `IndexEntry(metadata, snapshots, reading, reading_ref)`，再加上 `links`、`handoffs`、`rejections` 這三個清單。impl2 可以先用假資料開發，第 4 節的 publisher 會負責組出正確的輸入。

**F. 門檻**：`IndexStats` 保留 `over_threshold`（50 MB），另外加上 `file_count`（讀取視圖的檔案數，5,000 的門檻）。兩個數字都寫進 RunReport，給 6.3 使用。

**G. 確定性**：SQLite 檔案的位元組不是確定的。4.5 的比對要以「**各表依主鍵排序後的內容**」比較，不比較檔案雜湊。所以提供 `dump_tables(path) -> dict[str, list[tuple]]`。

**H. SQLite 版本**：trigram 需要 SQLite 3.34 以上。啟動時檢查 `sqlite_version()`，不符就 raise 並寫明原因（Mac 的系統 Python、舊的 Linux 都可能碰到）。

介面（在 impl2 的版本上補齊）：

```python
# search/index.py
def build_index(path: Path, *, entries: Iterable[IndexEntry], links: Iterable[LinkRow],
                handoffs: Iterable[HandoffRow], rejections: Iterable[RejectionRow],
                meta: IndexMeta) -> IndexStats: ...          # 寫到暫存檔再原子地改名
def dump_tables(path: Path) -> dict[str, list[tuple]]: ...

# search/query.py（純 SQLite，讀者端使用）
@dataclass(frozen=True)
class Query:
    text: str | None = None; title_contains: str | None = None
    case_id: str | None = None; source: str | None = None; status: str | None = None
    producer: str | None = None; parent_id: str | None = None; in_progress: bool | None = None
    updated_after: str | None = None; updated_before: str | None = None
    limit: int = 50; cursor: tuple[str, str] | None = None

@dataclass(frozen=True)
class SessionRow: ...          # 對應 sessions 表的一列
@dataclass(frozen=True)
class Hit:
    session: SessionRow; matches: tuple[MessageMatch, ...]   # 有 text 時才有值
@dataclass(frozen=True)
class MessageMatch:
    message_id: str; index: int; snippet: str

def search(db: sqlite3.Connection, q: Query) -> tuple[list[Hit], tuple[str, str] | None]: ...  # (hits, next_cursor)
def get_session_row(db, session_id) -> SessionRow | None: ...
def get_links(db, session_id) -> tuple[list[LinkRow], list[LinkRow]]: ...     # (出去的, 指向它的)
def get_handoffs(db, *, target_session_id=None, open_only=False) -> list[HandoffRow]: ...
def get_reading_ref(db, session_id, snapshot_sha256=None) -> ReadingRef | None: ...  # None＝最新
def get_rejection(db, item_key) -> RejectionRow | None: ...
```

---

## 4. 發佈（4.1，提交流程第 12 步）

### 4.1 計畫（純函式）

```python
# publish/plan.py
@dataclass(frozen=True)
class ReadingToPublish:
    session_id: str; snapshot_sha256: str; is_latest: bool
    existing: FileRef | None            # 舊世代已經有 → 沿用（不上傳）

@dataclass(frozen=True)
class PublishPlan:
    readings_new: tuple[ReadingToPublish, ...]     # 需要轉換並上傳的
    readings_keep: tuple[ReadingToPublish, ...]    # 沿用舊的 file id
    retire_now: tuple[str, ...]                    # 舊世代有、新世代沒有 → 列入 retired
    delete_now: tuple[str, ...]                    # retired 裡「退役世代 < 新世代 − 1」的 → 永久刪除
    full_rebuild: bool                             # converter_versions 變了，或設定要求完整重建

def plan_publish(store: AgoraStore, prev: Manifest | None, prev_readings: dict[tuple[str, str], FileRef],
                 converter_versions: dict[str, str], *, force_full: bool) -> PublishPlan: ...
```

- 「要發佈的快照集合」＝每個 Session 的最新快照 ∪ 所有交接單與接續 Link 的 `continuation.snapshot_sha256`。
- `prev_readings` 來自**舊世代的 index**（`readings` 表）。所以第 12 步要先下載舊的 index，這只在發佈時需要，一輪一次。

### 4.2 發佈器

```python
# publish/publisher.py
class DriveReadViewPublisher:  # 實作 committer.publish.ReadViewPublisher
    def __init__(self, drive: DriveClient, *, folder_id: str, manifest_file_id: str,
                 converters: dict[str, Converter], clock: Clock, workdir: Path): ...
    def publish(self, store: AgoraStore, *, agora_main_sha: str, run_rejections: Sequence[RejectionRow],
                force_full: bool = False, dry_run: bool = False) -> PublishReport: ...
```

順序（每一步失敗都 raise；在 step 12 失敗**不會**影響真本，下一輪會重新發佈）：
1. `get(manifest_file_id)`＋`download_bytes` → `parse_manifest`。第一次（管理者建立的空 manifest，`generation=0`）時，`prev=None`。
2. 下載舊的 index（依 manifest 的 FileRef，驗證 sha256）→ 取得 `prev_readings`。
3. `plan_publish`。
4. 對 `readings_new` 逐一轉換（converter）→ `create` → 記下 FileRef。轉換失敗的快照不發佈，並記下失敗代碼。
5. `build_index` 到 workdir → `create`。
6. 組出新的 manifest（`generation = prev + 1`、`retired` 加上 `retire_now`，並移除 `delete_now`）→ **`update_content(manifest_file_id, …)`**（原地更新）。
7. `delete_permanently(delete_now)`：刪除前先 `get()`，確認 parents 是讀取視圖資料夾；刪除失敗時盡力而為，記入 RunReport。

**冪等**：沒有變動（`agora_main_sha` 相同，而且 `run_rejections` 相同）時不發佈，回傳 `skipped`。

**限流**：Drive 大約每秒 2 個檔案（D5），一般一輪只有「變動的 reading 數＋1」次 create。完整重建時要分批（每輪最多 N 份，例如 500），其餘留到下一輪；但 manifest 必須一次指向一整組完整的檔案，所以完整重建的中間輪次**不切換** manifest，要等全部上傳完才切換。（**PM 決定 5** 決定完整重建的觸發方式）

### 4.3 第 4 步清掃讀取視圖的可信集合

修正 `run.py` 目前的 `plan_readview_sweep(…, set(), …)`（review-g3g M2）：

```python
trusted = trusted_ids(parse_manifest(drive.download_bytes(manifest_file_id, max_bytes=4 << 20)), manifest_file_id)
```

- manifest 讀不到或解析失敗 → **中止**（fail-closed）。
- manifest 還沒建立（config 沒有 `manifest_file_id`）→ 跳過讀取視圖的清掃，並在 RunReport 標記。

### 4.4 拒收原因的發佈（`publish/rejections.py`）

```python
def collect_rejections(store: AgoraStore, run_decisions: Sequence[Decision]) -> list[RejectionRow]: ...
```

- 真本的 `_committer/rejections/*.json`（驗章之後）＋本輪 `authenticated=False` 的 REJECT（驗章之前，不寫進真本，依 g3d-recheck R1）。只有 `item_key`、`code`、`at`、`item_id`（驗章之後才有），**不含任何內容**。
- **跨模組的缺口（PM 決定 4）**：驗章前的拒收每一輪都會重新評估，`rejected_at` 每一輪都是「當下」，所以 `deletable_after` 永遠到不了，第 13 步永遠不會刪除這些檔案。建議：驗章前的拒收，`rejected_at` 改用**該項目檔案最早的 `created_time`**（這是 Drive 的 metadata，寫入者無法控制），`deletable_after = rejected_at + 24h`。這樣「每一輪都發佈，24 小時後刪除」就成立。
- 同樣地，apply 的拒收檔格式要與 evaluate 統一（g3g M3），否則同一個項目會每一輪重新 apply。

### 4.5 第 12 步什麼時候該跑（**PM 決定 3**）

現在，收件匣是空的時候，run 在第 2 步就結束了，所以**上一輪發佈失敗之後，要等到下一個非空的輪次才會補發**。可以考慮的做法：
- (a) 接受這個延遲，由健康檢查（6.3）回報「manifest 的 `agora_main_sha` ≠ 正式 pin 的 main」。
- (b) prescan 讀取 pin repo（clone 很小，大約 1 秒，需要 deploy key）與 manifest，兩者不一致就不算空輪。

建議 (b)。成本是每一輪多 1〜2 秒，而且 H2（review-g3g）已經要把秘密檔案移到 prescan 之前。

---

## 5. 讀取介面（4.3）

### 5.1 身分與設定

```python
# drive/sa_auth.py
class ServiceAccountToken:
    def __init__(self, key_path: Path): ...    # 讀 SA 的 JSON 金鑰（只以路徑讀取）；private_key 不進 repr、例外、log
    def access_token(self) -> str: ...        # JWT bearer（RS256），scope=https://www.googleapis.com/auth/drive（1.5 H5：完整 scope）
    def invalidate(self) -> None: ...
```

（**PM 決定 6**）RS256 可以用既有的 `cryptography` 自己簽（大約 40 行，不新增依賴），或是加入 `google-auth`。建議自己簽，與 `RcloneConfToken` 的作風一致；另外也支援「從 rclone conf 的 `service_account_file` 取得路徑」，讓讀者只需要一份 rclone 設定。

```python
# reader/config.py
@dataclass(frozen=True)
class ReaderConfig:
    manifest_file_id: str
    sa_key_path: Path
    cache_dir: Path = Path("~/.cache/aistorage/reader").expanduser()
    @classmethod
    def load(cls, path: Path | None = None, *, env=os.environ) -> ReaderConfig: ...
    # 預設路徑 ~/.config/aistorage/reader.json；環境變數 AISTORAGE_READER_CONFIG；
    # 容器內可以用 AISTORAGE_SA_KEY 覆寫 sa_key_path（1.5 的容器做法）
```

manifest 的 id 跟著 profile 的讀取設定一起發放（D5）。這不是秘密，但只放在讀者設定裡。

### 5.2 取檔、驗證與快取

```python
# reader/client.py
class ReadViewClient:
    def __init__(self, drive: DriveClient, cfg: ReaderConfig, *, clock: Clock): ...
    def manifest(self) -> Manifest: ...
    # 每次呼叫都重新下載（很小）；generation 比快取的還小 → raise StaleManifest
    #（不接受倒退，防止回放舊的 manifest）
    def index(self) -> sqlite3.Connection: ...
    # 依 generation 快取：<cache>/<manifest_id>/index-g<n>.sqlite，以唯讀模式開啟（uri mode=ro）
    def reading(self, ref: ReadingRef) -> dict: ...
    # 依 sha256 快取；下載後驗證 sha256 與 size，並跑 validate_reading
```

- 錯誤型別：
  - `AccessDenied`：manifest 回 403 或 404。spec 規定「未授權要拒絕，不能回傳空結果」；Drive 對沒有權限的檔案可能回 404，所以 manifest 的 404 一律視為 AccessDenied。
  - `ReadError`：暫時性錯誤，原樣往上拋。
  - `MismatchError`：雜湊不符、格式錯誤。
- 快取只存讀取視圖的檔案，不存任何憑證。快取目錄權限設為 700。

### 5.3 函式庫 API

```python
# reader/__init__.py
@dataclass(frozen=True)
class Freshness:
    snapshot_at: str | None        # 這一筆的快照時間（Session＝snapshot_at；交接單與 Link＝這個世代的 published_at）
    generation: int; published_at: str
    satisfied: bool | None         # 沒有指定 max_lag 時是 None
    warning: str | None            # 例如 "stale: 快照落後 40m，要求 10m"
    stopped_ok: bool               # 因為「停止中，而且快照在停止之後」而視為符合

@dataclass(frozen=True)
class Result(Generic[T]):
    value: T; freshness: Freshness

class AgoraReader:
    def __init__(self, client: ReadViewClient, *, clock: Clock): ...
    def find_sessions(self, q: Query, *, max_lag: timedelta | None = None) -> Result[list[Hit]]: ...
    def get_session(self, session_id: str, *, max_lag: timedelta | None = None) -> Result[SessionView]: ...
        # SessionView = SessionRow＋links_out＋links_in＋handoffs（它寫的、以它為目標的）＋snapshots
    def get_reading(self, session_id: str, *, snapshot_sha256: str | None = None,
                    include_reverted: bool = False, include_reasoning: bool = False,
                    max_lag: timedelta | None = None) -> Result[dict]: ...
    def get_continuation(self, handoff_id: str) -> Result[ContinuationView]: ...
        # 交接單＋被釘住快照的 messages_before（reading.messages_before，snapshot_sha256 必須相符）
        # ＋指向同一目標的其他接續 Link（spec「知道還有誰在分頭做」）
    def list_open_handoffs(self, *, case_id: str | None = None) -> Result[list[HandoffRow]]: ...
    def get_rejection(self, item_key: str) -> Result[RejectionRow | None]: ...
        # 寫入者用來確認自己的項目有沒有被拒收
    def catalog(self, session_ids: Sequence[str]) -> Result[dict[str, CatalogEntry]]: ...
        # Q3：同步器用來比對 {raw_sha256, snapshot_at}；只讀 index，不下載 reading
    def wait_for_snapshot(self, session_id: str, raw_sha256: str, *, timeout: timedelta,
                          poll: timedelta = timedelta(seconds=15)) -> Result[bool]: ...
        # D9 的「同步並提交」：寫入者確認自己的快照已經可見；只讀、只輪詢，不觸發任何事
```

- 所有方法都**只讀**：`AgoraReader` 與 `ReadViewClient` 只接受 `DriveClient` 的讀取方法，測試時用 FakeDrive 斷言 `calls` 裡沒有任何 WRITE_OPS。
- 讀取介面**不提供**任何觸發 workflow 或同步的函式（ADR 0007）。`wait_for_snapshot` 只負責觀察；「觸發提交流程」屬於寫入端的工具（5.x 的 skill），不放在 reader 裡。

### 5.4 CLI

```
python -m aistorage.reader find   [--text T] [--title T] [--case ID] [--source S] [--status S]
                                  [--since 7d|RFC3339] [--until …] [--main-only] [--limit N] [--cursor C]
                                  [--max-lag 10m] [--json]
python -m aistorage.reader show   <session_id> [--max-lag …] [--json]
python -m aistorage.reader read   <session_id> [--snapshot SHA] [--include-reverted] [--include-reasoning]
python -m aistorage.reader continuation <handoff_id>
python -m aistorage.reader handoffs --open [--case ID]
python -m aistorage.reader rejection <item_key>
python -m aistorage.reader catalog <session_id>...
python -m aistorage.reader wait   <session_id> --raw-sha256 SHA [--timeout 10m]
```

- 預設輸出 JSON（給 AI 的 skill 使用），一律帶有 `freshness` 區塊；`--text` 則輸出人看的表格，警告以醒目的方式標出。
- exit code：成功是 0；**未達新鮮度仍然是 0**（spec：照樣回傳）；AccessDenied 是 3；ReadError 是 4；MismatchError 是 5。
- 只有純 Python 加上標準庫的 sqlite3，所以 arm64 與 amd64 都能跑（D5）。

---

## 6. 新鮮度（4.4）

```python
# reader/freshness.py
def evaluate_freshness(*, snapshot_at: str | None, status: str | None, stopped_at: str | None,
                       generation: int, published_at: str, now: datetime,
                       max_lag: timedelta | None) -> Freshness: ...
```

規則（D9、ADR 0007、spec）：
- `max_lag is None` → `satisfied=None`，不產生警告，但一定附上 `snapshot_at`。
- `status == "stopped"` 而且 `snapshot_at >= stopped_at`（用 datetime 比較）→ `satisfied=True`、`stopped_ok=True`，不論落後多久。
- 否則 `now − snapshot_at <= max_lag` → 符合；不符合 → `satisfied=False`，並附上 `warning`。
- **交接單、Link、拒收**這類不屬於單一 Session 的結果，「快照時間」用這個世代的 `published_at`。
- **清單型的結果**（`find_sessions`）：每一筆 Hit 各自附上 Freshness；清單整體再附一個 Freshness，以最舊的那一筆為準。
- `now` 用讀者本機的時鐘（1.7j 的時鐘漂移可以忽略，量級是秒）。docs 要寫明「落後時間＝讀者時鐘 − 同步器擷取時間」。

測試用 spec 的兩個情境命名：`S2 以 10 分鐘讀 40 分鐘前的 S3 → 回傳並附警告`，以及 `三天前停止、停止後同步過 → 不附警告`。另外要有一個測試：斷言讀取時 FakeDrive 沒有任何寫入呼叫。

---

## 7. 重建（4.5）

```python
# committer/rebuild.py
def rebuild_local(store: AgoraStore, converters: dict[str, Converter], out_dir: Path) -> RebuildResult: ...
    # 從真本全部重新產生閱讀版與 index（不碰 Drive）
def compare_with_published(result: RebuildResult, client: ReadViewClient) -> RebuildDiff: ...
    # 依 (session_id, snapshot_sha256) 比對 reading 的 sha256；index 用 dump_tables 逐表比對
```

- CLI：`python -m aistorage.committer rebuild-readview --verify`
  - 用 SA 讀取者身分 `git clone annex::…`（D5：管理與復原作業可以這樣做）。
  - 從重建的結果比對讀取視圖，輸出差異的**計數與 id**，沒有內容。
  - **任何地方都能跑**（Mac 或 worker），完全唯讀。
- **完整重新發佈**要有提交流程的寫入身分，只能在 Actions 上執行。workflow **不接受輸入**（D2），所以觸發方式建議是：
  - 在 `config/committer.json` 加上 `readview_rebuild_epoch: <整數>`，要重建時在 main 上把它加 1；
  - 提交流程發現 `manifest.rebuild_epoch < config 的值`，就走 `force_full=True`。

  這樣重建的觸發會留下 commit 紀錄，也符合「只能在 main 上改」的規則。（**PM 決定 5**）
- `converter_versions` 改變（轉換器升級）時，也自動走完整重建，因為閱讀版要跟著轉換器重建（spec「閱讀版一律可從原始紀錄重新產生」）。

---

## 8. 並行、順序與測試

### 8.1 相依與順序

```
A（可以並行）  readview/model＋schema（純）｜search（impl2，照第 3 節調整）｜reader/freshness（純）｜drive/sa_auth
B（依賴 A）    publish/plan＋publisher＋rejections｜reader/client＋AgoraReader
C（依賴 B）    reader CLI｜run.py 的第 4 步（可信集合）與第 12 步（接上 publisher）｜committer/rebuild
D（依賴 C）    整合測試（TEST_FOLDER_ID）｜TS 用戶端的黃金資料
```

建議分派：
- impl2 繼續 4.2（先定案第 3 節的 A〜D）。
- 另一位做 readview＋publish（4.1）。
- 再一位做 reader＋freshness＋sa_auth（4.3、4.4）。
- 4.5 在 B 完成後由 4.1 的人接手。
- 驗收測試照慣例由另一位依本草案撰寫。

### 8.2 單元測試

| 模組 | 重點 |
|---|---|
| readview/model | manifest 的 schema、`trusted_ids`、`generation` 倒退時拒絕 |
| search | 中文、日文、英文；2 字元走 LIKE 與 3 字元以上走 FTS，**結果相同**（性質測試：隨機字串，比較 LIKE 與 FTS 的結果集）；AND 與片語；篩選的組合；轉換失敗的 Session 仍然能用 metadata 查到；排序穩定；cursor 分頁；`dump_tables` 的確定性；SQLite 版本檢查 |
| publish/plan | 沒有變動 → skipped；變動一個 Session → 只有一份 reading＋index＋manifest；被釘住的快照會保留；退役的檔案要等一個世代才刪除；`converter_versions` 變了 → full |
| publisher | FakeDrive：API 呼叫的次數與順序（create → create index → **update manifest** → delete）；manifest 的 id 不變；任何一步 ReadError 都不更新 manifest；刪除前檢查 parents |
| reader | 只讀（沒有 WRITE_OPS）；manifest 403 或 404 → AccessDenied；雜湊不符 → MismatchError；依 generation 快取（第二次呼叫不重新下載 index）；manifest 倒退 → StaleManifest；`get_continuation` 只回傳接續點之前的內容（被釘住的快照，而且與最新版本不同） |
| freshness | 決策表（沒有 max_lag、符合、不符合、停止中而且在停止之後、停止中但在停止之前） |
| rebuild | 重建的結果等於增量發佈的結果；故意改動一份 reading → 被列入差異 |

### 8.3 整合測試（`pytest -m integration`，設定缺少時 FAIL）

- 寫入端：`~/.config/aistorage/rclone-committer-test.conf`，在 `TEST_FOLDER_ID` 底下建立 `it-<ULID>/readview/`。
- 讀取端：**需要一份測試用 SA 的憑證**。目前 `~/.config/aistorage/` 底下沒有 SA 讀取者的設定（spike 用的那一份在 `aistorage-spike/`，依規則不能使用）。請 PM 準備 `~/.config/aistorage/sa-reader-test.json`（只以路徑引用），並把測試資料夾分享給這個 SA（reader）。（**PM 決定 7**）
- 情境：
  1. 發佈 → SA 讀取（find、show、read、continuation）
  2. 增量發佈 → SA 在 update 之後 2〜5 秒內看到新的世代（1.4i）
  3. 以住民身分（`rclone-worker.conf`）在讀取視圖注入同名檔 → 讀者不受影響，下一輪清掃會把它隔離
  4. SA 嘗試 update manifest → 403（對照 1.5）

### 8.4 TS 用戶端的準備

在 `tests/unit/data/search/golden/` 放三樣東西：
- 一份固定的 build 輸入（`entries.json`）
- `queries.json`：各種 Query 與它們的預期結果（session_id 的順序、命中的 message_id）
- `freshness.json`：輸入與預期的 Freshness

Python 的測試要跑這三份資料；之後 TS 用戶端也要跑同樣的三份資料，結果必須完全相同。規格文字寫在 `schemas/searchindex.md`，包括 schema.sql、查詢語意（第 3 節 C）與新鮮度的規則。手機的 SQLite 如果沒有 trigram，TS 用戶端可以全部改用 LIKE，因為依第 3 節 C 的定義，結果是一樣的（只是比較慢）。

---

## 9. 需要 PM 決定的事

1. **index 同時是目錄（catalog）**：Link、交接單、拒收、reading 的 file id 都放在 SQLite 裡，不另外發佈 JSON 檔（第 0 節、第 3 節 A）。建議採用。
2. **內容檔不可變，只有 manifest 原地更新**：舊檔保留一個世代（第 0 節）。建議採用。
3. **發佈失敗後的補發**：建議讓 prescan 比對 pin 的 main 與 manifest 的 `agora_main_sha`（第 4.5 節 (b)）。
4. **驗章前拒收的時間基準**：改用檔案的 `created_time`，否則永遠不會被刪除（第 4.4 節；跨第 3 組）。
5. **完整重建的觸發方式**：在 main 上遞增 `config/committer.json` 的 `readview_rebuild_epoch`（第 7 節）；另外，重建的中間輪次不切換 manifest。
6. **SA 的認證**：用 `cryptography` 自己簽 JWT（建議），或是加入 `google-auth`（第 5.1 節）。
7. **整合測試用的 SA 憑證**：放在 `~/.config/aistorage/sa-reader-test.json`，並分享測試資料夾（第 8.3 節）。
8. **全文的語意與排序**：子字串比對、只有 ASCII 不分大小寫、依 `updated_at` 由新到舊、不使用 bm25（第 3 節 C）。這會寫進給 TS 的規格，之後再改會牽動兩邊，所以請現在定案。
9. **子 Session 在搜尋結果中的預設**：建議預設**包含**，並加上 `--main-only` 旗標（`parent_id=""`）；列出待認領的交接單時，只看主 Session 寫的交接單。
10. **manifest 的初始化**：由管理者在 Mac 上用提交流程的身分建立一次空的 manifest（`generation=0`），並把 id 寫進 `config/committer.json` 與讀者設定。這一步需要提交流程的憑證，請決定由誰、在哪裡執行（建議與 init-pin 放在同一個管理步驟）。

---

## 附：與第 3 組的交接點（修改 run.py 時要一起處理）

- 第 4 步：讀取視圖的可信集合改用 manifest（第 4.3 節），取代現在的 `set()`（g3g M2）。
- 第 12 步：`DriveReadViewPublisher.publish(store, agora_main_sha=<push 後的 main>, run_rejections=collect_rejections(…))`；publish 失敗時 RunReport 標記 `publish_failed`，但**不影響**第 13 步（真本已經轉正，收件匣可以照常清理）。
- 第 13 步：驗章前拒收的 `deletable_after` 依第 4.4 節修改。
- `config/committer.json` 新增 `readview_manifest_file_id`、`readview_rebuild_epoch`；`readview_folder_id` 已經有了。

### 實作後的具體介面（4.1／4.5 已完成，供第 3 組接線）

```python
# ── 第 4 步：可信集合（fail-closed）────────────────────────────
from aistorage.publish.publisher import load_manifest          # 讀不到／損毀 → MismatchError
from aistorage.readview.model import trusted_ids
if not cfg.readview_manifest_file_id:
    report.readview_sweep = "skipped_no_manifest"             # 管理者還沒初始化 → 跳過
else:
    m = load_manifest(deps.drive, cfg.readview_manifest_file_id)
    if m.is_initial:                                          # generation=0，還沒發佈過
        report.readview_sweep = "skipped_initial"
    else:
        readview_decisions = plan_readview_sweep(
            rv_listing, trusted_ids(m, cfg.readview_manifest_file_id),
            readview_folder_id=cfg.readview_folder_id)

# ── 第 12 步：發佈（失敗只標記，不影響第 13 步）──────────────────
from aistorage.publish.publisher import DriveReadViewPublisher
from aistorage.publish.rejections import collect_rejections

pub = DriveReadViewPublisher(
    deps.drive,
    folder_id=cfg.readview_folder_id,
    manifest_file_id=cfg.readview_manifest_file_id,
    converters=deps.converters,        # 轉換器可選帶 version 屬性（預設 "1"）
    clock=deps.clock,
    workdir=work_temp,                  # 已存在的暫存目錄即可
    rebuild_epoch=cfg.readview_rebuild_epoch,   # > manifest.rebuild_epoch → 完整重建
)
rep = pub.publish(
    store,
    agora_main_sha=local_refs.get("refs/heads/main", ""),   # push 後的 main
    run_rejections=collect_rejections(store, decisions),
    dry_run=dry_run,
)
report.readview_publish = rep.status      # "skipped" / "published" / "planned"
# 可記錄：rep.readings_created / readings_kept / len(rep.readings_failed) / rep.file_count
#          rep.over_threshold（索引 50 MiB 門檻，D5）／rep.index_file_count（5,000 檔門檻）
```

- `agora_main_sha` 相同且拒收原因相同時 publisher 會回傳 `skipped`（不寫任何東西）。
- prescan 補發（第 4.5 節 (b)、PM 決定 3）：用 `load_manifest(...)` 讀
  `manifest.agora_main_sha`，與 pin 的 `refs/heads/main` 不一致就不算空輪。
- `committer/publish.py` 的協定 `publish(...) -> None`；實作回傳 `PublishReport`，
  建議把協定的回傳型別放寬成 `Any`，或在 run.py 直接用 `DriveReadViewPublisher`。
- 4.5 的驗證 CLI 可獨立跑（唯讀）：`python -m aistorage.committer.rebuild --verify --repo <repo>`；
  要掛成 `python -m aistorage.committer rebuild-readview` 的子指令，由第 3 組在
  `committer/__main__.py` 加一個 dispatch（轉呼叫 `aistorage.committer.rebuild.main`）。

---

## PM 的決定（2026-09-27）

第 9 節的 1〜10 全部照建議採用：
1. index 同時是目錄。2. 內容檔不可變，只有 manifest 原地更新，舊檔保留一個世代。3. prescan 比對 pin 的 main 與 manifest 的 `agora_main_sha` 來補發。4. 驗章前拒收以檔案 `created_time` 為時間基準（第 3 組的 run.py 一併改）。5. 完整重建以遞增 `readview_rebuild_epoch` 觸發，中間輪次不切換 manifest。6. SA 認證用 `cryptography` 自己簽 JWT。7. 整合測試沿用共用的 `spike-reader` SA，金鑰複製到 `~/.config/aistorage/sa-reader.json`（PM 處理），測試資料夾分享給它。8. 全文語意：子字串比對、只有 ASCII 不分大小寫、依 `updated_at` 由新到舊、不用 bm25。9. 子 Session 預設包含，`--main-only` 旗標；待認領交接單只看主 Session 寫的。10. manifest 由 PM 在 Mac 上以提交流程身分初始化（與 init-pin 同一個管理步驟）。

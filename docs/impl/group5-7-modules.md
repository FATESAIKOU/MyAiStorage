# 第 5〜7 組：模組切分與介面草案（架構師）

- 第 5 組：Mac 上的 opencode（5.1〜5.4）
- 第 6 組：管理操作（6.1〜6.5）
- 第 7 組：Foundry 最小（7.1〜7.4）

依據：
- design D2（收件匣、抹除、錯開）、D3（容器、白名單、共用憑證）、D4（同步器）、D5（讀取）、D7（Foundry）、D9（同步並提交）、D10（交接與認領）
- ADR 0006、0007、0008
- 技術驗證 1.3（抹除）、1.4（注入）、1.5（SA）、1.6（PAT 的能力）、1.7a〜1.7h（opencode）、1.2m（`.bak`）
- 已完成的模組：`inbox_builder`、`reader`、`committer`、`publish`、`integrity`、`agora`、`intake`，以及 `docs/impl/group3-modules.md`、`group4-modules.md`

格式同前兩份：函式簽名與檔案佈局可以直接照做；標了「**PM 決定**」的地方需要先拍板（第 9 節有彙整）。

**模型規則**：住民 AI（容器裡的 opencode）使用的模型，依 PM 的隊員順序挑選當下有額度的，**不使用 Claude**。所以模型與 provider 一律是**設定值**，不寫死在 image 裡（第 1.3 節）。

---

## 0. 共用的前置：`inbox_builder` 補上其他型態

目前 `build_inbox_item` 只會組 session。第 5 組的 skill 與第 7 組都需要其他型態，建議在 `src/aistorage/inbox_builder.py` 補齊，**同步器、skill、匯入、Foundry 共用同一份**：

```python
def build_handoff_item(*, target_session_id: str, continuation: dict, body: dict, profile: str,
                       key: bytes, key_id: str, case_id: str | None = None,
                       item_key: str | None = None, clock: Clock | None = None) -> BuiltItem: ...
def build_claim_item(*, handoff_id: str, claimer_session_id: str, profile, key, key_id, ...) -> BuiltItem: ...
def build_reference_item(*, from_session_id: str, to_session_id: str, read_snapshot_at: str, ...) -> BuiltItem: ...
def build_artifact_item(*, kind: Literal["link", "contained"], produced_by_session_id: str,
                        content_type: str, name: str, raw_path: Path | None,
                        repo: str | None, path: str | None, ...) -> BuiltItem: ...   # 第 7 組

@dataclass(frozen=True)
class BuiltItem:
    item_key: str; item_id: str
    sidecar_bytes: bytes; sig: dict; raw_path: Path | None

def upload_item(drive: DriveClient, inbox_folder_id: str, item: BuiltItem) -> tuple[str, ...]: ...
    # 順序：raw → sidecar → sig（D2 的不可分單位）；把 importer 的 _upload_drive 搬到這裡共用
```

- 每一個型態組好之後，都要用 `validate_sidecar` 與 `check_raw` 自查（與 session 相同）。
- 本機要檢查 `key_id` 是否以 `<profile>-` 開頭（g3g 的 L）。

---

## 1. 5.1 執行容器（`resident/`）

### 1.1 檔案佈局

```
resident/
  image/Dockerfile            # Ubuntu 24.04；opencode、rclone、uv、Python 3.12 都固定版本並驗證 sha256
                              # （沿用 spike/env/Dockerfile 的做法）；aistorage 套件以 wheel 安裝到 /opt/aistorage（唯讀）
  image/entrypoint.sh         # 容器啟動流程（見 1.2）
  opencode/opencode.base.json # opencode 設定範本：plugin 路徑、provider 金鑰的「檔案引用」
  opencode/plugin/aistorage.ts# 5.4 的 plugin（安裝到 /opt/aistorage/opencode/plugin，唯讀）
  opencode/skills/            # 5.4 的 skill 說明（唯讀）
  run.sh                      # Mac 端啟動：resident/run.sh <容器名> --profile mac-opencode --model <provider/model>
  verify-boundary.sh          # 在「實際的住民容器」裡錄能力邊界（沿用 spike/env/verify.sh）
docs/resident.md              # 操作說明（白名單、目錄、如何同時開多個容器）
```

### 1.2 `run.sh` 與 `entrypoint.sh`

- **白名單**：Mac 上的來源目錄是 `~/.config/aistorage/resident/<profile>/`（600／700），**只有**以下檔名會以唯讀方式掛到 `/secrets/`，其他檔名一律拒絕並報錯：

  | 檔名 | 用途 |
  |---|---|
  | `rclone-worker.conf` | worker 的 Drive 憑證（`drive.file`），conf 裡的路徑一律寫容器內路徑 |
  | `sa-reader.json` | 讀取身分 |
  | `signing.key` | 該 profile 的簽章私鑰 |
  | `llm-<provider>.key` | 一個或多個 LLM provider 的金鑰 |
  | `gh-pat-actions.txt` | 選用，5.3 觸發提交流程用 |
  | `reader.json` | manifest id 等讀取設定，不是秘密 |

- **工作目錄**：`~/.local/share/aistorage/work/<容器名>/` 掛到 `/work`，而且 `HOME=/work`。opencode 的本機資料就在 `/work/.local/share/opencode`，所以每個容器各自一份，**不共用**。
- 容器名不能重複，否則拒絕啟動。這樣就可以同時開多個容器（D3「每個分岔各自一個容器」）。
- 預設用 Mac 的 uid:gid 執行（spike 的 colima 經驗）。不掛 docker.sock，不加 capability。
- **entrypoint 的流程**：
  1. 把 `/secrets/rclone-worker.conf` 複製到 `/tmp/aistorage/rclone.conf`（600，**可寫**，讓 token 能刷新）。
  2. 由 `opencode.base.json` 產生 `/tmp/aistorage/opencode.json`，provider 金鑰寫成 `{file:/secrets/llm-<provider>.key}`，**不讓 opencode 在 `/work` 寫出明文的 `auth.json`**（1.7a M4）。然後設定 `OPENCODE_CONFIG=/tmp/aistorage/opencode.json`。
     - 啟動時檢查 `/work/.local/share/opencode/auth.json`：存在就**拒絕啟動**，並提示使用者刪除。
  3. 在背景啟動 `opencode serve --hostname 127.0.0.1 --port 4096`，同步器的 API 就用它（D4「全域清單」）。
  4. 在背景啟動同步器 daemon：`python -m aistorage.syncer opencode daemon`。
  5. 前景執行使用者的互動介面。

**PM 決定 1**：前景的互動方式要確認 opencode 1.18.32 是否支援「TUI attach 到既有的 serve」（例如 `opencode attach`）。如果不支援，替代做法是同步器改用 TUI 自己內建的 server（要找得到它的 port），或者改讀 DB。請 impl 在容器裡查 `opencode --help`（**不跑 LLM**）確認。

### 1.3 模型與 provider

- `run.sh --model <provider/model>` 寫入 `opencode.json` 的預設模型。provider 金鑰檔依 `--model` 的 provider 自動選擇（`llm-<provider>.key`）。
- 模型依 PM 的隊員順序挑選當下有額度的，**不使用 Claude**，也不寫死在 image 裡。**PM 決定 2**：要列出期 1 允許的 provider 清單（spike 用的是 ollama-cloud；如果 PM 的規則排除它，就要換成其他 provider，而且要用那個 provider 重跑一次 1.7 的相關驗證）。

### 1.4 驗收

- `verify-boundary.sh` 必須在**真正的住民容器**裡錄一次（1.7a 的 G6 要求）：env 變數的名稱、完整的 `/proc/mounts`、docker.sock、capability，以及 `ls /Users`、寫入 `/secrets` 會失敗。
- 另外跑一次「秘密的值不在 `/work` 裡」的掃描：以 `grep -cFf` 比對 `/secrets/*`，只輸出計數。
- 同時開兩個容器，確認兩邊的 `/work` 與 opencode 的資料互不可見。

---

## 2. 5.2 同步器（`src/aistorage/syncer/`）

```
syncer/
  __init__.py
  opencode_api.py    # OpencodeApi：list_sessions（GET /session，含子 Session 與 time.archived）、export（CLI）、archive（PATCH）
  state.py           # SyncState：/work/.aistorage/sync-state.json（每個 Session 的上傳紀錄）
  core.py            # sync_once（純邏輯，注入 api、reader、drive、builder、clock）
  commit.py          # 5.3 sync_and_commit
  __main__.py        # python -m aistorage.syncer opencode {daemon|once|status} ／ sync-and-commit …
```

### 2.1 介面

```python
# opencode_api.py
@dataclass(frozen=True)
class OcSession:
    id: str; parent_id: str | None; title: str | None
    updated_ms: int; archived_ms: int | None

class OpencodeApi:
    def __init__(self, base_url: str = "http://127.0.0.1:4096", *, directory: str = "/work"): ...
    def list_sessions(self) -> list[OcSession]: ...
        # GET /session（全域清單，**包含子 Session**，1.7h）；需要時再以 /session/{id}/children 遞迴補齊
    def export(self, session_id: str, dest: Path) -> Path: ...
        # 執行 `opencode export <id>`，stdout 寫到 dest（位元組原樣，1.7b 已驗證是確定的）；
        # 一定要明示 id（1.7a：不帶 id 會進入互動選單）
    def archive(self, session_id: str, at_ms: int) -> None: ...     # PATCH /session/{id} {"time":{"archived":at_ms}}（1.7e）

# state.py
@dataclass
class SessionSyncRecord:
    last_uploaded_sha: str | None; last_item_key: str | None
    uploaded_at: str | None; uploaded_generation: int | None   # 上傳當下讀取視圖的世代
    stop_observed_at: str | None                                # 第一次觀測到「封存且之後沒有新訊息」的時間

class SyncState:
    def load(path: Path) -> SyncState: ...
    def save(self) -> None: ...          # 寫暫存檔再原子地改名

# core.py
@dataclass(frozen=True)
class SyncOutcome:
    uploaded: tuple[str, ...]; waiting: tuple[str, ...]; unchanged: tuple[str, ...]
    reuploaded: tuple[str, ...]; rejected: tuple[tuple[str, str], ...]   # (session_id, code)
    resumed_after_stop: tuple[str, ...]  # 需要立刻同步並提交的 Session

def sync_once(*, api: OpencodeApi, reader: AgoraReader, drive: DriveClient, inbox_folder_id: str,
              signer: Signer, state: SyncState, clock: Clock, workdir: Path,
              only: Sequence[str] | None = None) -> SyncOutcome: ...
```

### 2.2 `sync_once` 的規則（D4、Q3、1.7b、1.7e）

1. `sessions = api.list_sessions()`，然後 `catalog = reader.catalog([s.id…])`，一次查詢拿到 Agora 裡每個 Session 的 `{raw_sha256, snapshot_at, status}`。
2. 對每個 Session：`snapshot_at = clock.now()`（**在匯出之前**記錄，D4 的擷取時間）→ export → 算 sha。
   - **sha 等於 Agora 裡的** → 已經收進 Agora；清除等待狀態，記為 unchanged。
   - **sha 等於 `last_uploaded_sha`，而且 Agora 還沒有** → 視為**等待中**，不重傳。
     - 例外（補傳）：`reader.manifest().published_at > uploaded_at`（上傳之後，已經有新的世代發佈了）、Agora 仍然沒有，而且 `reader.get_rejection(last_item_key)` 沒有記錄 → 以新的 item_key 重傳（D4：「下一次提交之後仍然沒有，才補傳」）。
     - 有拒收記錄 → 記進 `rejected`，**不自動重傳**（避免無限迴圈）。`status` 子命令會列出來。
   - 其他情況 → 上傳新版本（`build_inbox_item` → `upload_item`）。
3. `facts = converter.facts(export)`，`in_progress` 來自 facts（build_inbox_item 已經處理）。facts 失敗時照 PM 決定 1 的精神：以保守的值上傳，閱讀版交給提交流程標記失敗。
4. **停止的判定**（1.7e、D4、**review-g5-6 H3**）：`archived_ms > 0` 而且
   `last_message_created_ms <= archived_ms`，才算停止。**用訊息「被建立」的時間，
   不是完成的時間**：宣告停止一定發生在 AI 回覆**生成中**（那一則訊息在封存
   **之前**建立、封存**之後**才完成），用 completed 判會讓它在下一輪自己恢復成
   running，宣告停止永遠不成立。`agora/apply._is_stopped` 與
   `syncer/core._is_stopped` 必須是同一個定義。
   - `stopped_at` 取**同步器第一次觀測到的時間**（`stop_observed_at`），不用來源端的值。
   - Agora 已經是 stopped，但本地在封存之後又有新訊息 → 以 running 上傳，並列入 `resumed_after_stop`。daemon 看到這一項，就**立刻**執行 `sync_and_commit(that_session)`（spec「停止後又被恢復」）。
   - **`resumed_after_stop` 只報一次**（review-g5-6 H3）：恢復的判定以 **Agora 的
     狀態**為準（Agora 是 stopped、本地算出 running），而且恢復之後要**清掉**
     `stop_observed_at`。用本機記錄判的話，停止過一次、恢復之後的**每一次上傳**
     都會被列進去，daemon 就每 10 分鐘觸發一次 workflow，吃掉 D9 的 Actions
     分鐘預算。
5. `parent_id` 取 API 的 `parentID`，一路帶進 sidecar（子 Session 也是 Agora 的 Session）。
6. 超過 raw 上限（100 MiB）→ 不上傳，記進 status，讓 6.3 回報。
7. daemon：預設每 10 分鐘一次（`--interval`）。一輪失敗時記錄 log（只有 id 與代碼）後繼續，**不會觸發提交流程**（只有第 4 點的例外）。

**驗收**：用「子代理再開子代理」建立三層 Session，確認三個都被同步，而且 `parent_id` 正確。

### 2.3 依賴與憑證

- Drive 寫入：`/tmp/aistorage/rclone.conf`（worker 的 `drive.file`），收件匣的 folder id 取自登錄檔（`inbox_folder_ids`），也可以放在 `reader.json`。
- 讀取：`/secrets/sa-reader.json` 與 `reader.json`。
- `Signer`：`load_private_key("/secrets/signing.key")`＋key_id（寫在 `reader.json` 或 `resident.json`）。

---

## 3. 5.3 同步並提交（`syncer/commit.py`）

```python
@dataclass(frozen=True)
class Awaited:
    item_key: str; kind: str; target: str          # target＝session_id／handoff_id／claim_id／reference_id

@dataclass(frozen=True)
class CommitWaitResult:
    visible: tuple[Awaited, ...]; rejected: tuple[tuple[Awaited, str], ...]; pending: tuple[Awaited, ...]
    timed_out: bool; elapsed_s: float

def sync_and_commit(*, session_ids: Sequence[str], extra_items: Sequence[BuiltItem] = (),
                    deps: SyncDeps, timeout: timedelta = timedelta(minutes=15),
                    progress: Callable[[str], None] = print) -> CommitWaitResult: ...
def trigger_committer(pat_path: Path, repo: str, workflow: str = "committer.yml") -> None: ...
    # POST /repos/{repo}/actions/workflows/{workflow}/dispatches {"ref":"main"}；
    # 一律帶 main（1.6：不帶 ref 會失敗）；不送任何 inputs（D2）
def wait_visible(reader: AgoraReader, awaited: Sequence[Awaited], *, timeout, poll=timedelta(seconds=20),
                 progress) -> CommitWaitResult: ...
```

流程：
1. `sync_once(only=session_ids)` 上傳指定 Session 的最新版本（**強制**，不管是否在等待中）。
2. 上傳 `extra_items`（交接單、認領、參考），順序在 session 之後。
3. 觸發提交流程。PAT 由 `/secrets/gh-pat-actions.txt` 讀取，**不進 argv、log 與例外訊息**（1.6 的做法）。
4. 輪詢讀取介面，每一個項目的「看得到」定義如下：

   | 型態 | 看得到的條件 |
   |---|---|
   | session | `catalog[id].raw_sha256 == 上傳的 sha` |
   | handoff | `get_session(target).handoffs` 包含這個 id |
   | claim | 存在 `claim_id == 這次的 claim` 的接續 Link，而且 `from` 是自己 |
   | reference | 參考索引的 `reference_id` 等於這次的 id |

   另外，任何一個項目在 `get_rejection(item_key)` 有記錄，就算「有結果」（拒收）。
5. 顯示進度（例如「3／4 可見，已等 2m10s」）。逾時的訊息要固定包含：「**提交流程可能被停用或遭到注入**（請執行健康檢查 6.3）」，並列出還沒看到的 id。
6. **不以 run id 判斷完成**：concurrency group 可能會取消排隊中的 run（D2、ADR 0007）。

CLI：`python -m aistorage.syncer sync-and-commit --session <id>… [--items <dir>] [--timeout 15m] [--json]`

---

## 4. 5.4 skill 與 plugin

### 4.1 分工

- **plugin（TypeScript，opencode 的限制）**：只負責兩件事：
  1. 從 `context.sessionID` 取得**目前的** Session id（1.7f），並查出它的 `parentID`；
  2. 把工具呼叫轉給 Python CLI `python -m aistorage.skill <cmd> --session <ctx.sessionID> …`。

  Session id 由 plugin 從 context 傳入，**不由模型提供**。
- **Python（`src/aistorage/skill/`）**：負責所有邏輯（同步、組項目、同步並提交、讀取），可以直接做單元測試。
- **skill 說明（`resident/opencode/skills/aistorage/*.md`）**：告訴 AI 什麼時候用哪一個工具，以及「要把快照時間與警告帶進上下文」。

### 4.2 plugin 的工具（名稱加上 `aistorage_` 前綴）

| 工具 | 主 Session 限定 | 做什麼 |
|---|---|---|
| `aistorage_whoami` | 否 | `{session_id, parent_id, is_main}` |
| `aistorage_split(parts: [{title, summary, next_steps}])` | 否 | 分裂：同步自己，為每一份工作各寫一張交接單，**一起**同步並提交，等到全部可見 |
| `aistorage_handoff_end(summary)` | 否 | 交出末端：同上，只寫一張 |
| `aistorage_claim(handoff_ids: string[])` | **是** | 認領（多張＝統合）：同步自己＋所有 claim，一起提交；等 Link 屬於自己**才**回傳交接單＋接續點之前的閱讀版；只要有任何一張被拒收就回傳拒收原因，並指示 AI **停下** |
| `aistorage_find(query)`、`aistorage_read(session_id, max_lag?)` | 否 | 參照：讀取介面的 find、read，**一律附上 freshness** |
| `aistorage_reference(session_id)` | 否 | 留下參考 Link（`read_snapshot_at` 取剛讀到的快照）；**預設只上傳，不觸發提交**，由下一輪提交流程收進去（**PM 決定 4**） |
| `aistorage_list_handoffs(case_id?)` | 否 | 列出待認領的交接單 |
| `aistorage_stop()` | **是** | 宣告停止：以 API 設定 `time.archived=now`，然後同步並提交 |

- **主 Session 限定**：plugin 看到 `parentID` 就拒絕；Python 端再檢查一次（`api.list_sessions()` 查到 parent 就拒絕）。提交流程也有第三道防線：認領由 `apply_claim` 的 `claim_from_subsession` 擋下。宣告停止沒有提交流程這一層的檢查，所以 plugin 與 Python 這兩層是必要的。
- **接續點**：Python 在匯出之後，取「最後一則已完成的訊息」（最後一則 `time.completed` 存在而且沒有被 revert 的訊息）。寫交接單的這一次回覆本身還在生成中，所以自然不會被算進去（D10）。它與 `apply_handoff` 的 `_is_last_completed` 使用同一個定義。
- **認領的前提**：claimer 必須已經在 Agora 裡。所以 `claim` 一定要**先上傳自己的 session**，再上傳 claim；兩者在同一批提交，apply 的順序是 session 在前（這一點已經成立）。
- **信任邊界的說明**：同一個容器裡的 AI 可以透過 bash 直接執行 `python -m aistorage.skill claim --session <別的 id>`，而同一個 profile 的持有者檢查會通過。這是 D3 接受的範圍（同一個 profile 就是同一個信任範圍），請寫進 docs/resident.md。

### 4.3 skill 說明的內容（重點）

- **分裂與統合**：一定要用工具，不要手寫項目檔。
- **認領**：只能在主 Session 裡做；看到拒收就停下，並告訴使用者原因。
- **參照**：回答時要引用快照時間；有新鮮度警告時，要在回答裡明說「可能不是最新的」。
- **不要**為了讀到更新的內容而要求對方同步（ADR 0007）。需要時，由「寫的一方」自己執行同步並提交。

---

## 5. 第 6 組：管理操作（`src/aistorage/admin/`，只在 Mac 上、以管理憑證執行）

```
admin/
  __init__.py
  lock.py          # 6.5 AdminLock：停用 workflow＋等待＋維護旗標＋預檢
  erase.py         # 6.1
  rollback.py      # 6.2
  health.py        # 6.3
  recover.py       # 6.4
  verify_remote.py # 抹除與復原共用的後置條件檢查（修掉 spike 找到的兩個放行漏洞）
  __main__.py      # python -m aistorage.admin {lock-status|erase|rollback|health|recover}
docs/runbooks/{erase,rollback,recovery,health}.md
```

- 憑證只以路徑引用：`~/.config/aistorage/rclone-committer.conf`（專用帳號）、管理者自己的 GitHub token（gh CLI 的登入），以及 pin repo 的管理寫入（你的帳號）。
- 所有指令都**先 dry-run**，只列出 id、計數與雜湊；`--confirm <plan-hash>` 才會執行（計畫的內容改變，雜湊就不同，執行會被拒絕）。

### 5.1 6.5 錯開：`AdminLock`

1.6 已經證實住民的 PAT 可以**重新啟用** workflow，所以「停用 workflow」不能作為鎖。建議用 **pin repo 的維護旗標**當作真正的鎖（**PM 決定 7**）：

```python
class AdminLock:
    def __init__(self, *, repo: str, pins_admin: GitPinStore, gh: GitHubAdmin, reason: str): ...
    def __enter__(self) -> AdminLock:
        # 1. 在 pin repo 寫入 .pin/<repo>.maintenance
        #   （{reason, at, by, op, state}；op 是這次上鎖的操作 id，
        #    state=active；失敗時改成 aborted）→ push
        #    住民與 SA 都寫不進 pin repo（1.5、D3）
        # 2. 停用 committer workflow（輔助措施，可能被重新啟用）
        # 3. 等到沒有 in_progress 或 queued 的 run（逾時就中止）
        # 4. 預檢：遠端的 manifest 雜湊等於正式 pin（不符就中止）
    def __exit__(self, …):
        # 重建或確認 pin → 刪除 maintenance 旗標 → 重新啟用 workflow
```

- **committer 那一側（第 3 組要改一行）**：`pins.load()` 之後，如果 `.pin/<repo>.maintenance` 存在，**立刻以 `maintenance` 結束**（不清掃、不 push、不發佈，也不刪除收件匣）。
  - 這同時滿足 6.5 的「重建釘選值期間暫停清掃」。
  - 即使住民在管理操作期間重新啟用並觸發 workflow，也不會互相覆蓋。
- **驗收**（task 6.5）：管理操作期間，(a) 同時觸發提交流程，(b) 用住民的 token 重新啟用並觸發。兩種情況的 run 都必須在 `maintenance` 結束，而且遠端的 refs 不變。

實作落差（6.5 補齊後，2026-09-28）：

| 事項 | 做法 |
|------|------|
| 停用的 workflow 名稱 | 由設定檔 `committer_workflow` 帶（預設 `committer.yml`）。原本 `erase`／`rollback`／`swap-finish`／`unlock`／`health` 散落硬編碼成 `commit.yaml`——repo 裡沒有那個檔，`gh workflow disable` 會直接失敗，整個管理操作起不來。 |
| 重建釘選值（6.4 的 `init-pin --confirm`） | 也走錯開：沒有維護旗標就自己上鎖；已經有旗標時，只有 `state=aborted`（之前的操作做到一半失敗，正在做中止處理）才沿用既有的鎖，`active` 或狀態不明的舊旗標一律拒絕，避免兩個管理操作並行。precheck 用 `--repo` 指到的 target 自己的前綴與 uuid（M4-2），不再拿 Agora 的。 |
| push 前重讀遠端 manifest | `swap_remote` 多一步 `recheck-remote`：swap 開始時記下遠端 manifest 的指紋，push 前再讀一次比對；有人動過遠端就中止（保留鎖），不覆蓋。有刪遠端檔（抹除）時「主 manifest 已由本輪刪掉（None）」或「與開始時完全相同」才放行；讀到**新的**主 manifest 就中止（M4-3）。 |
| GitHub repo 名稱 | 管理操作（`erase`／`rollback`／`swap-finish`／`unlock`／`init-pin`）用的 repo 名稱一律來自設定檔 `github_repository` 或 `--gh-repo`；缺少時直接 raise，不再預設 `FATESAIKOU/MyAiStorage`（M4-4：預設錯 repo 會停用到別人的 workflow）。 |
| 預檢（`AdminLock` 的 `precheck`） | `erase`／`rollback` 用嚴格版（遠端 manifest 必須等於正式釘選值）；`swap-finish`／`init-pin` 用寬鬆版（它們的前提就是遠端已經不一致），只擋「多個主 manifest」與「判不出有沒有被動過」。 |

### 5.2 6.1 抹除

```python
@dataclass(frozen=True)
class EraseTarget:
    kind: Literal["session", "segment", "annex_key"]
    session_id: str | None = None
    message_ids: tuple[str, ...] = ()       # segment：要抹除的訊息
    key: str | None = None

@dataclass(frozen=True)
class ErasePlan:
    targets: tuple[EraseTarget, ...]
    repo_uuids: tuple[str, ...]             # 每一代的 uuid（過去 consolidate 或復原產生的）
    delete_file_ids: tuple[str, ...]        # 遠端 bundle、manifest、.bak、被抹除的 annex key（依 file id）
    readview_file_ids: tuple[str, ...]      # 含有被抹除內容的 reading 與 index
    inbox_file_ids: tuple[str, ...]; quarantine_file_ids: tuple[str, ...]
    snapshot_remap: dict[str, str]          # segment 抹除：舊的 snapshot_sha → 新的 snapshot_sha
    run_ids_to_delete: tuple[int, ...]      # 可能含有內容的 Actions run（D2 的 log 規則之下通常是空的）
    known_clones: tuple[str, ...]           # 提醒清單

def plan_erase(targets, *, admin: AdminDeps) -> ErasePlan: ...
def apply_erase(plan: ErasePlan, *, confirm: str, admin: AdminDeps) -> EraseReport: ...
```

步驟完全照 design D2「改寫與抹除」與 1.3，全程在 `AdminLock` 之內：
1. clone
2. `git filter-repo`（整個 Session：刪除目錄；segment：改寫該 Session **每一份快照**的 raw，移除指定的訊息）
3. 刪除 `refs/annex/*`；把被抹除的 key 標成 dead，然後 `forget --drop-dead`；`gc --prune=now`
4. **依 file id 永久刪除**遠端的全部 GITBUNDLE、GITMANIFEST（含 `.bak`），以及被抹除的 annex key。刪除前以 API 確認 parents（1.3 的 M1）。
5. push
6. 重建正式 pin（管理者依觀測值寫入）
7. 讀取視圖：遞增 `readview_rebuild_epoch`（g4 的決定 5）讓它完整重新發佈，並依 file id 刪除舊的 reading 與 index
8. 刪除收件匣與隔離資料夾中的相關檔案
9. `gh run delete`（如果有需要）
10. 後置條件：`verify_remote.py`

**segment 抹除會改變快照的雜湊。** 被釘住的接續點（交接單與 Link 的 `continuation.snapshot_sha256`）必須一起重新對應，否則接續就會失效。建議：
- 抹除時同步改寫 `snapshots.jsonl`、`handoffs/*`、`links/continuation/*` 裡的雜湊，並在抹除紀錄裡保存 `snapshot_remap`，但**只記雜湊的對應，不記內容**。
- 接續點指向的訊息本身被抹除時，那張交接單標成 `erased`（讀取時照樣看得到交接單，但內容已經被抹除）。

這需要 PM 決定（**PM 決定 5**）。

- **抹除紀錄**：寫在真本的 `_admin/erasures/<ULID>.json`：`{who, at, why, targets(只有 id), repo_uuids, snapshot_remap}`。**不含任何被抹除的內容**。
- **`verify_remote.py`** 要修掉 spike 找到的兩個會放行的漏洞：
  - remote 上有**不在 manifest 裡**的 bundle → 判為失敗；
  - `git cat-file` 失敗 → 判為失敗，不能略過。

  另外還要檢查：垃圾桶裡沒有任何一代 uuid 的 GITBUNDLE；被抹除的 key 在 remote 上找不到；讀取視圖的 index 查不到被抹除的 message_id。
- **驗收**：
  - 部分抹除（其他 Session 與 annex 物件都保留）。
  - 抹除之後，以下各處都找不到被抹除的內容（在測試資料裡放一個獨特的 canary 字串，搜尋時**只輸出計數**）：目前版本、git 歷史、bundle、Drive 的舊 revision 與垃圾桶、讀取視圖與索引、收件匣、隔離資料夾、Actions log。
  - 用 Mac opencode 的憑證（worker 的 conf）執行抹除，必須得到 403 或 404。

### 5.3 6.2 回滾（改寫已經拿掉，這是剩下的唯一「改內容」途徑）

建議**不要**由管理者直接改真本，而是走「管理者簽章的收件匣項目」，由提交流程處理（仍然只有一個寫入者，也不需要鎖）（**PM 決定 6**）：
- 新型態 `rollback`：`{session_id, target_snapshot_sha256, reason}`，只有 `admin` profile 的 `allowed_types` 包含它。
- `apply_rollback`：`target_snapshot_sha256` 必須在 snapshots 裡。從歷史取出那一份 raw，追加一份 `via="rollback"` 的新快照（raw 的內容就是那一份舊版本）。`snapshot_at` 取提交流程的時鐘，閱讀版會在下一次發佈時重建。
- **同步器必須尊重回滾**：否則來源端的內容比較新，下一輪同步就會把回滾蓋掉（這與改寫的 M2 是同一個問題）。規則：catalog 裡提供 `via` 與 `superseded_sha`；Agora 的最新版本是 `via=rollback`，而且本地匯出的 sha 等於 `superseded_sha` 時，不上傳。等來源端出現新的內容（sha 改變），才恢復同步。
- CLI：`python -m aistorage.admin rollback --session <id> --to <snapshot_sha> --reason "…"`。它會先列出 snapshots 讓你選擇，然後用 admin 的簽章金鑰組出 rollback 項目，放進 **admin 專用的收件匣**，接著觸發並等待（沿用 5.3）。

### 5.4 6.3 健康檢查

```python
@dataclass(frozen=True)
class Check:
    name: str; status: Literal["ok", "warn", "fail"]; value: str; hint: str | None

def run_health(deps: HealthDeps, *, now: datetime) -> list[Check]: ...
```

| 檢查項目 | 資料來源（住民無法偽造的優先） |
|---|---|
| committer 與 worker 的 refresh token 是否有效 | 對兩份 conf 各做一次 token 刷新（不印出任何值） |
| workflow 有沒有被停用 | GitHub API（管理者的 token） |
| 被取消的 run 數、連續中止的輪數 | GitHub runs API。**住民可以刪除 run**（1.6），所以再加上下一列的交叉檢查 |
| 距離上一次成功提交的時間 | **pin repo 的正式 `promoted_at`**（住民無法偽造） |
| 定時提交的實際間隔 | runs API 的 schedule 事件 |
| Actions 分鐘數（連續兩週超過 300 就提醒） | billing API |
| 隔離資料夾的增長 | Drive（committer 的 conf）：各日期子資料夾的檔案數與總大小 |
| 家庭共用的整體配額 | `about.get(storageQuota)` 的 `limit − usage` |
| 搜尋索引大小、讀取視圖的檔案數 | manifest＋index 的 `IndexStats`，對照 D5 的門檻（50 MB、5,000 個） |
| 讀取視圖是否落後 | `manifest.agora_main_sha` 是否等於 pin 的 main（g4 的決定 3） |
| 同步器的積壓 | 各容器 `syncer status` 的輸出（等待中、拒收、超過大小） |
| opencode 升級時 | `resident/verify-prune.sh` 的檢查清單（prune 仍然只加標記、不清除 `state.output`） |

- **通知**（**PM 決定 8**）：期 1 建議在 Mac 上用 launchd 每 6 小時執行一次。出現 `fail` 時用 macOS 的通知（`osascript`）加上非 0 的 exit code。不另外開一個 workflow（會花 Actions 分鐘）。

### 5.5 6.4 復原手冊與 `recover.py`

- 手冊記錄每個 repo 的**完整 clone URL**（`annex::<uuid>?type=rclone&…&rcloneprefix=…`）、pin repo 的位置、manifest 的 file id。
- `recover.py`：
  - 模式 `from-clone`：用任何一個 clone 推到新的前綴（或原本的前綴）；`init-pin --confirm`（用 g3g H5 修好的版本，含 keys）；更新 `config/committer.json`；讀取視圖完整重建。
  - `bak-only`：主 manifest 不在、`.bak` 在（1.2m 的做法）。
  - `both-missing`：主 manifest 與 `.bak` 都不在，只能 `from-clone`。

  全部都在 `AdminLock` 之內執行。
- **演練一次**：在 `TEST_FOLDER_ID` 底下刪掉一個測試 repo，用 clone 復原，然後跑一輪完整的提交流程（包括清掃）確認一切正常，並把步驟與耗時記進 runbook。

---

## 6. 第 7 組：Foundry 最小

### 6.1 7.1 repo 與提交流程的多 repo 支援

- `python -m aistorage.admin create-repo foundry`：
  - **Agora 原始紀錄的 `annex.largefiles`（review A-H1 的實測結論，git-annex 10.20260901）**：
  原始紀錄路徑是 `sessions/<source>/<id>/raw`（**沒有副檔名**），所以
  `include=*.json` 涵蓋不到它（raw 會留在 git blob 裡，2.6 的量測效果不會發生）。
  Agora 用 **`include=sessions/*/*/raw`**：`raw` 進 annex、`meta.json`／`snapshots.jsonl`
  留在 git。規則必須在建構 `AnnexRawStorage` 時就設好（對已在 index 的檔案無效）。
  annex key 一律用 `git annex lookupkey`（review A-H2），取不到就 raise。
  註：git-annex 10 只吃**單一** largefiles 規則（多條會全部失效），需要兩種規則時要選
  一個涵蓋範圍較大的（例如 `include=sessions/*/*/*`）。
- 在 Drive 建立 `foundry/` 前綴資料夾與隔離資料夾，建立 git-annex repo（`type=rclone`、`encryption=none`、不 chunk，與 Agora 相同）；
  - 在 pin repo 建立 `.pin/foundry.*`（init-pin）；
  - 建立 Foundry 的讀取視圖資料夾與 manifest（`element="foundry"`）。
- `config/committer.json` 改成 `repos: {agora: {...}, foundry: {...}}`。
- `run.py` 把第 3〜11 步抽成 `RepoPipeline`，在同一個 job 裡依序處理各 repo（D2：單一 job）。**只有收件匣裡有 artifact 時，才 clone Foundry**，所以平常的成本不變（**PM 決定 9**）。
- 收件匣是共用的，依型態分派：`artifact` 交給 Foundry，其他交給 Agora。evaluate 的 `foundry_not_enabled` DEFER，改成依設定判斷：沒有 foundry 設定才 DEFER。

### 6.2 7.2 項目格式與 Foundry repo 的佈局

- 產出目錄的項目（2.1 的 artifact metadata＋body）：

  ```
  body = {kind: "link" | "contained", produced_by_session_id: str (必填),
          content_type: str, name: str, repo?: str, path?: str,      # link：原處產出（repo＋path，或 URL）
          description?: str}
  ```

- Foundry repo 的佈局：

  ```
  catalog/<ULID>.json               # 產出目錄（每件一筆）：metadata＋body＋{object_key?, size?, sha256?}
  objects/<ULID>/<安全化的檔名>     # contained：以 git annex add 存成 annex 物件
  _committer/…                      # 清冊、拒收（與 Agora 相同）
  ```

- `produced_by_session_id` 必須是 Agora 裡已經存在的 Session，而且產生者是它的持有者（比照 g3e 的 H3）。檢查時讀取的是 **Agora 的工作樹**，所以 pipeline 的順序是先處理 Agora，再處理 Foundry。
- **Foundry 的 `annex.largefiles`（review F-H2 的實測結論，git-annex 10.20260901）**：
  - `include=objects/*/*`：`objects/<ULID>/<檔名>` 進 annex、`catalog/*.json` 留在 git（bundle 只有小檔）——** Foundry 用這條**。
  - `anything`：連 catalog 也進 annex，bundle 會變大，不採用。
  - 收容產出的 key 一律用 `git annex lookupkey <path>` 取，不自己算（副檔名取自工作樹檔名、沒有副檔名就沒有副檔名，還受 `annex.maxextensionlength` 影響）。查不到就是「沒進 annex」，`put_contained_object` 直接 raise。

### 6.3 7.3 收容產出入庫

- evaluate：artifact 的 `max_raw = 100 MiB`（D7）。先看 metadata，超過就 `REJECT(too_large)`，原因發佈在讀取視圖（與 Agora 相同）。
- `apply_artifact(store_foundry, dec, agora_store, clock)`：
  - contained：`git annex add objects/…` 加上 catalog 檔；
  - link：只有 catalog 檔。
- Foundry 的 annex key 會進 Foundry 的 pin（g3g 的 H1 修好之後，copy 之後的 key 集合就正確了）。
- **讀取介面以 Drive file id 取物件（D5）**，所以發佈 Foundry 讀取視圖之前要把 id 寫進索引：
  `foundry.index.resolve_object_file_ids(rows, drive=…, prefix_folder_id=…, allowed_keys=pin.annex_keys)`
  以前綴列舉結果比對 `name == annex_key`，並確認 Drive 的 checksum／size 相符；
  對不上（`object_not_found`／`checksum_mismatch`／`size_mismatch`／`key_not_in_pin`／`missing_annex_key`）
  就**不發佈那一筆**，並把原因記進 RunReport（review F-H3）。

### 6.4 7.4 讀取視圖與 Foundry 的讀取介面

- Foundry 的讀取視圖使用與 Agora **同一套** `readview` 格式（`element="foundry"`）。index 多一張表：

  ```sql
  CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, kind TEXT, content_type TEXT, name TEXT,
    producer TEXT, case_id TEXT, produced_by_session_id TEXT, created_at TEXT, updated_at TEXT,
    size INTEGER, sha256 TEXT, annex_key TEXT, repo TEXT, path TEXT);
  ```

- `FoundryReader`（`src/aistorage/reader/foundry.py`，與 AgoraReader 分開，依 ADR 0001）：
  - `find(type, case_id, producer, since, until, session_id, max_lag)`：依型態、所屬案件、產生者、時間、產出它的 Session 查詢，新鮮度規則與 Agora 相同（g4 第 6 節）。
  - `get(artifact_id, dest)`：
    - contained：用 SA 依 annex key 直接從 rclone special remote 取物件（路徑 `<rcloneprefix>/<完整 key 檔名>`，1.5）。取物件**也必須以 id 定位**：以 index 記錄的 `object_file_id` 取得，**不依名稱搜尋**（D5）。所以 publisher 要把每個 key 的 file id 寫進 index。取得後以 key 驗證內容雜湊。
    - link：回傳出處。
- 永久保存：Foundry 的 GC **只回收 bundle**，annex 物件永遠不刪（spec「永久保存」）。

---

## 7. 並行、順序

```
A（可以並行）  inbox_builder 補齊型態（第 0 節）｜resident/image＋run.sh（5.1）｜admin/lock＋committer 的維護旗標（6.5）
B（依賴 A）    syncer core＋opencode_api（5.2）｜syncer commit（5.3）｜admin/health（6.3）｜Foundry 的 create-repo＋多 repo pipeline（7.1）
C（依賴 B）    skill Python＋plugin TS＋skill 說明（5.4）｜admin/erase＋verify_remote（6.1）｜rollback（6.2，需要 committer 的 apply_rollback）｜apply_artifact＋Foundry 讀取視圖與 reader（7.2〜7.4）
D（依賴 C）    recover＋演練（6.4）｜第 9 組的端到端（1→n、n→1、n↔m）
```

- **5.1 必須在 5.2〜5.4 之前完成**，因為能力邊界要在「實際的住民容器」裡錄。
- **6.5 的維護旗標要在任何管理腳本之前完成**，因為所有管理腳本都需要它。

---

## 8. 測試策略

| 對象 | 單元測試（fake） | 整合測試（`TEST_FOLDER_ID`、測試 pin、測試 SA；全部只以路徑引用） |
|---|---|---|
| inbox_builder 各型態 | 自查的正反例；用 `verify_sidecar_bytes` 驗證組出來的簽章；`key_id` 與 profile 的一致性 | 上傳後提交流程 ACCEPT |
| 5.1 | `run.sh` 的白名單解析（不在白名單的檔名 → 拒絕）；entrypoint 產生的 opencode.json 含有檔案引用，而且沒有 auth.json | 在真正的住民容器錄能力邊界；兩個容器互相隔離 |
| 5.2 | `FakeOpencodeApi`＋FakeDrive＋假的 reader：沒有變動、等待中、補傳（新世代發佈但仍然沒有）、拒收不重傳、in_progress、停止判定、封存後有新訊息 → `resumed_after_stop` | 用真的 opencode serve（**不跑 LLM**，用 `opencode run` 的最小 prompt，或者 import 現成的匯出樣本）同步一個三層的 Session 樹 |
| 5.3 | `wait_visible` 的決策表（看得到、拒收、逾時的訊息）；PAT 不出現在任何輸出 | 觸發測試 repo 的 workflow（`--ref main`），等到讀取介面看得到 |
| 5.4 | Python 端每個指令（主 Session 限定、接續點＝最後一則已完成、claim 先上傳 session） | 在容器裡由 plugin 呼叫一次 `whoami`（1.7f 的驗證方式） |
| 6.5 | committer 看到 `maintenance` 就結束，而且沒有任何寫入 | 管理操作期間，同時觸發＋住民重新啟用，都在 maintenance 結束 |
| 6.1 | `plan_erase` 的清單（依 file id）、`snapshot_remap`、verify_remote 的兩個漏洞 | 1.3 的完整情境＋canary 搜尋（只輸出計數）；用 worker 的 conf 抹除 → 403 |
| 6.2 | `apply_rollback`；同步器尊重回滾 | 回滾後，同步器下一輪不會覆蓋 |
| 6.3 | 每一個 Check 的判定（用假的資料來源） | 對測試資源跑一次 |
| 6.4 | `recover.py` 三種模式的計畫 | 演練一次並記錄 |
| 7.x | artifact 的 evaluate 與 apply（link、contained、過大）；Foundry index；FoundryReader | contained 產出入庫 → SA 取回 → 雜湊一致 |

**規則**：整合測試**不讀任何真實的 Session 或 MyBrain 檔案**（使用者的規則）。opencode 的測試 Session 由測試自己建立，內容是固定的測試字串；需要真實形狀時，先問 PM。

---

## 9. 需要 PM 決定的事

1. **容器內 opencode 的互動方式**：`serve`＋TUI attach（要確認當版是否支援）、改用 TUI 內建的 server，或者讀 DB（第 1.2 節）。
2. **住民使用的 provider 與模型清單**：依隊員順序、不使用 Claude；spike 用的 ollama-cloud 是否仍然可以用；如果換 provider，要重跑 1.7 的相關驗證（第 1.3 節）。
3. **同步器的補傳條件**：用「上傳之後，已經有新的世代發佈」當作「下一次提交之後」的判斷依據（第 2.2 節）。
4. **參考 Link**：只上傳，交給下一輪的提交流程（建議），還是每次都同步並提交（花費 Actions 分鐘）（第 4.2 節）。
5. **segment 抹除時的快照重新對應**：改寫交接單與 Link 的 `snapshot_sha256`；接續點的訊息本身被抹除時，交接單標成 `erased`（第 5.2 節）。
6. **回滾的做法**：admin 簽章的 `rollback` 項目＋同步器尊重回滾（建議），還是管理者直接改真本（需要 AdminLock）（第 5.3 節）。
7. **管理操作的鎖**：用 pin repo 的 `maintenance` 旗標（建議）；committer 需要加一行檢查（第 5.1 節）。
8. **健康檢查的執行與通知**：Mac 的 launchd 每 6 小時執行一次，加上 macOS 通知（第 5.4 節）。
9. **Foundry 的提交流程**：與 Agora 在同一個 job 裡依序處理，而且只有收件匣裡有 artifact 時才 clone Foundry（第 6.1 節）。
10. **admin profile**：新增一個只允許 `rollback` 的 admin profile、它的簽章金鑰與專用收件匣（第 5.3 節；如果決定 6 選直接修改，就不需要）。
11. **skill 的封裝格式**：opencode 1.18.32 的 skill 或 AGENTS 機制（1.7d 看到 `tool == "skill"`，所以當版有 skill）；請 impl 在容器裡確認安裝位置，**不跑 LLM**。

---

## 附：與既有模組的交接點

- **第 3 組**：
  - committer 要加上維護旗標的檢查（第 5.1 節）；
  - `apply_rollback`（第 5.3 節）；
  - 多 repo 的 pipeline 與 `apply_artifact`（第 6 節）；
  - evaluate 的 artifact 分派；
  - catalog 要提供 `via` 與 `superseded_sha`（給同步器的回滾規則）。
- **第 4 組**：
  - `catalog()` 的回傳要補上 `via` 與 `superseded_sha`；
  - Foundry 的讀取視圖使用同一套 publisher（`element` 參數化），index 多一張 `artifacts` 表，以及每個物件的 file id。
- **importer**：`_upload_drive` 改用第 0 節的 `upload_item`。

---

## PM 的決定（2026-09-28 深夜，使用者早上複查）

1. 住民容器用 `opencode serve`＋HTTP API（非互動），由 impl 確認 1.18.32 的支援；人要看時再 TUI attach。
2. 住民 provider：照隊員模型順序挑當下有額度的，不用 Claude（opencode zen 免費模型優先，ollama-cloud 於 09:30 重置後可用）。換 provider 只需重驗匯出形狀（1.7a、1.7b），其他 1.7 結論與 provider 無關。
3. 補傳條件：「上傳之後已經有新世代發佈、仍看不到」才補傳（照建議）。
4. 參考 Link 只上傳，交給下一輪提交流程（照建議）。
5. segment 抹除時重新對應快照、接續點被抹除的交接單標 `erased`（照建議）。
6. **回滾改為管理者直接操作**（以 AdminLock 錯開），不做簽章的 rollback 項目。理由同「期 1 拿掉改寫」：運作中的 Session，下一次同步會以來源端的內容成為新版本，文件與 CLI 的說明要寫明這一點；spec「能回滾到任何舊版本」以「舊版本都能取回、管理者能把某個舊版本恢復成新的快照」達成。
7. 管理操作的鎖用 pin repo 的 `maintenance` 旗標（照建議）。
8. 健康檢查：launchd 每 6 小時＋macOS 通知；**安裝腳本寫好但不在使用者的 Mac 上啟用**，等使用者同意。
9. Foundry 與 Agora 同一個 job 依序處理，只有收件匣有 artifact 才 clone Foundry（照建議）。
10. 不新增 admin profile（因為決定 6）。
11. skill 的封裝格式由 impl 在容器裡確認安裝位置，不跑 LLM。

anthropic/claude-opus-5-5 結論：會發生（建議修法 A：在 config 目錄用本機跨行程鎖包住 find-or-create 和寫回，進鎖後重讀 config.json，只合併 `folders[name]`）

# #330 設計筆記：`Drive.folder_id` 兩個行程同時初次建資料夾

- 單：#330（d；PM 改判「架構師：必經」）。作者：專案架構師 myaistorage-arch-m1-1。
- 對照的程式：main 90dfc90 的 `src/agora/store.py`。rclone 原始碼釘在 rclone/rclone master `ba11197157fe590b581393e801e2b1721fb4d608`（2026-11-01 用 `gh api` 讀）。
- 沒有連真的 Drive，沒有跑真的 rclone，也沒有讀 rclone 設定。下面標「推論」的是沒有實測的部分。

## 1. 競爭會不會發生

**會。rclone 的 drive backend 對 `mkdir` 沒有跨行程的同名保護，Google Drive 本身也允許同名資料夾。**

證據（原樣引用）：

1. Drive 允許同名。`docs/content/drive.md:1920-1924`：
   > `### Duplicated files` … `Drive unlike all the other` / `remotes can have duplicated files.`
2. `rclone mkdir` 走 dircache 的 find-or-create。`backend/drive/drive.go:2686-2688`：
   > `func (f *Fs) Mkdir(ctx context.Context, dir string) error {` / `_, err := f.dirCache.FindDir(ctx, dir, true)`

   根目錄本身（`gdrive:agora`）由 `_findRoot` 建，走的是同一條路。`lib/dircache/dircache.go:305`：
   > `rootID, err := dc._findDir(ctx, dc.root, create)`
3. find-or-create 是「先列、找不到再建」兩次獨立的 API 呼叫，中間沒有任何條件式建立。`lib/dircache/dircache.go:234`、`:242`：
   > `pathID, found, err := dc.fs.FindLeaf(ctx, parentPathID, leaf)`
   > `pathID, err = dc.fs.CreateDir(ctx, parentPathID, leaf)`

   `CreateDir` 只送 `Name`、`MimeType`、`Parents`，不帶任何去重條件。`backend/drive/drive.go:1840-1845`（`createDir` 的 `createInfo := &drive.File{ Name: leaf, MimeType: driveFolderType, Parents: []string{pathID}, }`）。
4. 唯一的互斥是行程內的 Go mutex，跨不了行程。`lib/dircache/dircache.go:25`：
   > `mu           sync.Mutex // protects the below`

所以兩個行程（同機或不同機）在 `FindLeaf` 到 `CreateDir` 之間的窗口內交錯，就會在 Drive 根目錄建出兩個 `agora/`。

**觸發條件與後果**（讀 store.py:532-552 推得，**推論，未重現**）：

- 只有 Drive 上還沒有這個名字的資料夾時才可能發生。實際上就是第一次在任何機器上使用（或新的 `AGORA_FOLDER_NAME`），而且有兩個行程同時第一次用到。已經有資料夾時，`FindLeaf` 會找到，不會新建。所以實際機率很低，但窗口真的存在。
- 發生後，`lsjson --dirs-only` 看到 2 個同名，`folder_id` 丟出既有的 StoreError「Drive 根目錄有 2 個 agora/，請先處理同名資料夾」（store.py:547-548）。若其中一個行程在另一個建好之前就 lsjson 完，它會把自己那個 ID 寫進 config.json，之後照常用。**之後任何沒有快取 ID 的行程或機器都會一直報這個錯，直到人手動處理**。資料不會悄悄分裂：後來的行程都會看到 2 個而報錯（推論；前提是 Drive 的列表在建完之後就看得到，這點沒有驗證）。
- config.json 的 lost update：`Drive.__init__` 讀一次快照（store.py:512），`folder_id` 寫回整個快照（store.py:551）。兩個行程同名時寫的是同一個 ID，無害。不同名時（例如 `agora` 和 `agora-test` 同時第一次用）後寫的會蓋掉先寫的那個名字，下次再找一次就好了（mkdir 找得到，不會新建），屬於自癒、只多花幾次呼叫。目前 config.json 只有 `folder_id` 這一個寫入者（`grep settings_file` 只有 store.py:510、551）。

**需本人授權的真機驗證計畫**（**不建議做**，因為上面的原始碼已經足以下結論；只有想量測機率時才做）：用隔離的 config 和一次性的名字 `AGORA_FOLDER_NAME=agora-race-<日期>`，同時起兩個 `rclone mkdir gdrive:agora-race-<日期>`，再 `rclone lsjson gdrive: --dirs-only` 數同名資料夾。停損：只動這個名字；最多 3 次；每次結束都 `rclone rmdir` 掉建出來的空資料夾；任何一步碰到其他路徑或報錯就停，並回報 PMO。

## 2. 修法與取捨

### A（建議）：本機跨行程鎖＋進鎖後重讀＋只合併 `folders[name]`

- **模組切割**：全部留在 `store.py` 的 `Drive.folder_id` 內，新增一個私有的鎖 helper（例如 `_config_lock(paths)`，context manager，`fcntl.flock(LOCK_EX)` 阻塞等待，寫法和 `Index` 的 `index.lock` 一樣，store.py:2460-2461）。cli、background、migrate 都不改。
- **流程**：
  1. 快取命中（`self.settings["folders"][name]` 有值）就直接回傳：不拿鎖、不呼叫 rclone，和現在一樣。
  2. 否則拿鎖，在鎖內重讀磁碟上的 config.json。
     - 若已經有 `folders[name]`（別的行程剛寫好），就採用它，更新 `self.settings` 後回傳，不呼叫 Drive。
     - 若沒有，就照現況 `mkdir` → `lsjson` → 數同名，再把**磁碟上重讀到的內容**加上 `folders[name]`（以及舊格式欄位的 pop）用 `write_atomic` 寫回，`self.settings` 換成合併後的內容。
  3. 放鎖。
- **鎖檔位置**：`paths.config / "config.json.lock"`，放在它保護的檔旁邊。這樣 `AGORA_STATE_DIR` 不同、但共用 config 的行程也會被串起來。
- **為什麼不沿用 `upload.lock`**：背景上傳拿著 `upload.lock`（background.py:87）的同時，會建 `Drive` 並呼叫 rclone（background.py:98-99，`_run` 會呼叫 `folder_id`，store.py:517）。第一次使用時 `folder_id` 若也去拿 `upload.lock`，同一個行程用另一個 fd 去 flock，會**自己卡死**。另外 `uploader_is_running`（store.py:1020-1026）會把「正在找資料夾」誤判成「背景上傳中」。兩把鎖保護的東西不同，要分開。
- **為什麼重讀不可省**：只有鎖的話，第二個行程進鎖後仍會照自己的舊快照去 `mkdir`。若 Drive 的搜尋在剛建完時還看不到新資料夾（推論，未驗），即使已經串起來，還是可能再建一個。有了重讀，第二個行程完全不碰 Drive。
- **對外介面**：`folder_id()` 的簽名、回傳值、StoreError 文字**都不變**。不新增 Drive 呼叫，不刪任何東西。
- **跨機器**：**顧不到**。兩台機器同時第一次建，仍可能出現同名，結果是既有的 StoreError，由人處理。一人公司的設定是一台一台做的，這個情境實際上不會出現（推論）。所以接受，在 docstring 寫明。
- **風險**：鎖內有 rclone 呼叫，`_run` 本來就沒有逾時。持鎖者卡住時，其他第一次使用的行程會一起等。但它們不拿鎖也會卡在同樣的 rclone 上，所以不算新風險。鎖只在快取沒命中時拿，平常的指令不受影響。

### B（不建議這一張做）：事後收斂——同名多個時依確定性規則挑一個

- 作法：lsjson 看到多個同名時，依確定性規則（例如最早建立，同時建立就取最小 ID）挑一個。可以再加限制：只在其他幾個都是空的時候才挑；甚至把空的那幾個移到垃圾桶。
- 好處：顧得到跨機器。
- 不建議的理由：
  1. 會改掉既有的契約「同名就報錯、請人處理」（U-ST-16，tests/unit/test_store_more.py:143-146）。使用者自己手動建的同名 `agora/` 若有資料，可能被悄悄略過。
  2. 「是否為空」要對每個候選多列一次，「最早建立」要多拿 `createdTime`（lsjson 預設只給 ModTime），都會增加 Drive 呼叫。
  3. 刪或移到垃圾桶是在 Drive 上的破壞性動作，屬於行為面，要 PM 和本人決定。
- 若日後真的在跨機器碰到，另開 c，由 PM 定行為之後再設計。

## 3. 給實作的介面契約

- **C-1**：`Drive.folder_id(self) -> str` 的簽名與回傳不變。Drive 上有多個同名時，仍然丟出 `StoreError`，文字照舊「Drive 根目錄有 {n} 個 {name}/，請先處理同名資料夾」（U-ST-16 仍綠）。
- **C-2**：`self.settings` 已有 `folders[name]` → 直接回傳：不呼叫 rclone、不建鎖檔、不拿鎖（U-ST-17 仍綠）。
- **C-3**：快取沒命中時，「重讀 config.json → mkdir → lsjson → write_atomic」整段都在 `paths.config / "config.json.lock"` 的 `LOCK_EX` 之內。
- **C-4**：進鎖後重讀磁碟上的 config.json，若已有 `folders[name]` → 回傳那個 ID，不呼叫任何 rclone 指令，`self.settings` 跟著更新。
- **C-5**：寫回的內容 = 進鎖後重讀到的內容 + `folders[name]`（並照舊 pop `folder_id`、`folder_name`）。別的名字的 ID 不會遺失。寫入仍用 `write_atomic`（#329 的測試仍綠）。
- **C-6**：不使用 `upload.lock`。鎖 helper 是 store.py 的私有函式，不出現在其他模組。
- **C-7**：不新增 Drive 呼叫種類，不刪、不移任何 Drive 資料夾。跨機器同時初次建不在範圍內，寫進 `folder_id` 的 docstring。

## 4. 測試策略（確定性，不靠睡眠和時序）

都放在 `tests/unit/`（`import _guard`，用既有的 `remote` fixture 和假 rclone）。不需要讓假 rclone 能模擬兩個行程同時 mkdir：它的 `mkdir` 是本機的 `mkdir -p`（tests/fakes/fake_rclone.py:234-235、159-166），本來就不會建出同名。要驗的是「我們有沒有串起來、有沒有重讀」，用下面幾種探針就能確定地驗：

- **T-1（C-3，鎖涵蓋 mkdir 和寫回）**：monkeypatch `Drive._run`，在收到 `mkdir` 時、以及 monkeypatch `write_atomic` 在寫 config.json 時，各用一個新的 `open(...)` 對 `config.json.lock` 試 `flock(LOCK_EX | LOCK_NB)`，斷言**拿不到**。改壞：拿掉鎖、或鎖只包住 mkdir 而沒包住寫回 → 這個探針拿得到 → 紅。flock 是依 open file description，同一個行程另外 open 的 fd 也會互斥，所以不需要開子行程。
- **T-2（C-4，重讀）**：先建 `a = Drive(paths)`（快照是 `{}`），再讓 `Drive(paths).folder_id()` 跑完寫進 config.json。記下 calls.log 的長度，接著 `a.folder_id()` → 斷言回傳同一個 ID，而且 calls.log 沒有新增任何一行。改壞：拿掉重讀 → a 會再呼叫 mkdir 和 lsjson → 紅。
- **T-3（C-5，lost update）**：`a = Drive(paths)`（快照 `{}`）；設 `AGORA_FOLDER_NAME=agora-test` 跑 `Drive(paths).folder_id()`；改回 `agora`，再跑 `a.folder_id()` → 斷言 config.json 的 `folders` 同時有 `agora` 和 `agora-test`。改壞：寫回 `self.settings` 的舊快照 → `agora-test` 不見 → 紅。
- **T-4（C-2）**：快取命中時不建立 `config.json.lock`。可以和既有的 U-ST-17 併在一起，加一行斷言。
- **C-1** 由既有的 U-ST-16（`FAKE_RCLONE_DUP`）保護。建議順手把 `pytest.raises(..., match="請先處理同名資料夾")` 加上，把文字鎖住。
- 跨行程的冒煙測試（多個子行程同時跑 `folder_id`）**不需要**：假 rclone 本來就不會產生同名，紅不起來，沒有鑑別力。
- 驗收門檻（要不要全部套件綠、要不要在 Linux worker 再跑一次）由 PM 定。

## 5. PR 切分

一個 PR（d）：store.py 的 `folder_id` 加上鎖 helper，加 T-1～T-4，再補強 U-ST-16 的 match。實作派給隊員。我依 C-1～C-7 審結構（介面與依賴方向），正確性與測試歸 rev。

## 6. 風險與未決

- 跨機器同時初次建：接受，維持既有的報錯（PM 確認；若不接受就走 B，另開 c）。
- Drive 的列表在剛建完時是否看得到新資料夾（影響第 1 節的後果描述）：未驗，不影響建議。
- 鎖檔名 `config.json.lock` 放在 config 目錄：屬於形狀，由我定；若 PM 認為 config 目錄只能放設定檔，可改放 state 目錄，代價是不同 `AGORA_STATE_DIR`、共用 config 的行程就串不起來。

# #364 設計筆記：冷快取 sync 的下載階段改列表式 copy，加 mirror 外的持久影子目錄

- 單：#364（層級 d）。作者：設計文件作者 myaistorage-impl-m1-35。**核准者模式**：專案架構師已核准（2026-11-03）；§8 的 6 條裁定見 §10，**實作以 §10 與 §4 標 ′ 的契約為準**。
- 依據：`tickets/REPORT-arch-364.md`（§1～§4 與尾端「追加」）、`tickets/REPORT-arch-agora-639.md`、`reports/MEASURE-364.md`、`docs/architecture/decisions.md`（D-2 與 #316 系列）。
- 對照的程式：main 621bb14 的 `src/agora/store.py`、`tests/fakes/fake_rclone.py`、`docs/perf.md`。**引用一律寫符號名**（行號會漂）。
- 這份 note 只讀程式，沒有連真的 Drive、沒有跑真的 rclone、沒有讀憑證或 `~/.config/agora`。標「推論」「未驗」的是沒有實測的部分。
- 範圍：只寫設計。**不改程式、不改 `decisions.md`**（decisions 由架構師在同一 PR 加一條）。

## 1. 背景（含量測數字）

### 1.1 卡在哪

`store.sync` 先用 `Drive.list_sessions`（`lsjson … -R --fast-list --hash --files-only`）列整個 remote，把「本機 index 沒記這個 md5、或 mirror 沒有這個檔」的 session 收進 `changed`，再呼叫 `Drive.download_many`：

```python
_with_files_from(self, [f"{u}/session.md" for u in ulids], mirror,
                 "copy", "gdrive:sessions", str(mirror), "--no-traverse")
```

`--no-traverse` 配 `--files-from` 時，rclone 對清單裡的每個 `sessions/<ulid>/session.md` 各要走一次路徑解析（MLP 的推論），成本與 session 數成正比、且常數很大。worker 上實測 215 秒只拿到 144 KB（MLP#639）。`last-sync` 戳記只在 `sync` 的**最後一行**才寫（`write_atomic(stamp, …)`），所以被 sidecar 的 `import_timeout`（預設 120 秒）砍掉的那一輪不會留下戳記，下一輪從頭再來：**冷快取在 worker 上永遠收斂不了**。

### 1.2 量測（2026-11-03，Mac，PM 自己跑；唯讀 Drive，拋棄式 `AGORA_CACHE_DIR`／`AGORA_STATE_DIR`，只記數字）

| 量測 | 數字 |
|---|---|
| remote | 5716 檔，`session.md` 1371 份，合計 44,244,290 位元組 |
| 列表（`lsjson -R --fast-list --hash --files-only`） | 4.9 秒 |
| 現行路徑（`copy --no-traverse --files-from`，50 份） | 32 秒 → 約 **0.64 秒／份**；外推 1371 份約 14.6 分鐘（worker 實測 18m54s，同量級） |
| 提案 `copy --include "*/session.md" --fast-list`（1372 份、46 MB），transfers 4 | 245 秒 |
| 同上，`--transfers 16 --checkers 16` | **142 秒**（比現行快約 3.6～6.2 倍） |
| 附帶事件 | 列表式複製途中有 4 份「corrupted on transfer（md5/size differ）」，rclone 重試後成功（`Attempt 2/3 succeeded`） |

最後一條的成因：那些 session.md 正在被上傳者改寫，不是下載壞掉。它的意思很明確：**列表式路徑拿到的位元組不保證等於列表的 md5**，正確性必須由 agora 後面那一輪逐一 md5 核對兜底（§1.3）。

### 1.3 為什麼這個修法可以小：逐一核對本來就是兜底

`sync` 在批次下載之後，對**每一個** `changed` 都做一次核對：

```python
if not (local.exists() and md5_file(local) == md5):
    drive.download(ulid, "session.md", local)
```

所以 `download_many` 只是**加速**：它漏了、拿錯版本、拿不到，結果都由這一段保證。修法能維持小，就是因為它只換「怎麼把檔拿下來」，不換「什麼算拿對」。

另外 142 秒仍然超過 sidecar 的 120 秒，所以**只**改列表式不夠：被砍掉時已下載的會全丟，下一輪又是 142 秒以上。這是「追加」把暫存目錄改成**跨行程保留的影子**的理由。

## 2. 目標與非目標

**目標**

1. `need`（扣掉 mirror 已有且 md5 正確之後的數量）達到門檻時，下載階段用**一次**遞迴列表式 rclone，取代逐檔解析：量測 3.6～6.2 倍。
2. 被砍掉或失敗時，已下載的位元組**不丟**：下一次大量下載只補還缺的（續傳）。
3. mirror 只會多出 `changed` 裡、而且 md5 等於列表的檔。G3／#119、垃圾桶佇列、Q4 標記語意**不變**。
4. 對外行為完全不變：CLI、輸出、exit code、mirror 結構、環境變數、設定檔都不動（C-9）。
5. 熱快取、變動少時一行都不變（C-10）。

**非目標（這一張不做，寫下來免得被順手做進去）**

- 不改 sidecar 的 `import_timeout`（MLP 的 D）、不改 worker 的快取放 volume（MLP 的 B）、不改列表那一步（①c）。
- 不改 `strict` 的語意、不改標記（`Index.mark_missing`／`cloud_missing`）、不改 `unfinished.json` 的格式。
- 不動上傳路徑（`upload_batch`、`_copy_batch`、`_delete_batch`）。
- 不做「`--include-from <changed 清單>`」的變形（架構師 §1.4 說值得一起量，但量測沒做，也仍屬同一張 d 的後續選擇）。
- 不處理跨機器共用同一份 cache 的情形（鎖是**本機** flock）。

## 3. 設計

### 3.1 影子目錄：位置、為什麼在 mirror 外

- **位置**：`<cache>/download-staging/<ulid>/session.md`，新常數／屬性放在 `Paths`：
  - `staging = property(lambda s: s.cache / "download-staging")`
  - `staging_lock = property(lambda s: s.cache / "download-staging.lock")`
- **為什麼是 `<cache>` 而不是 `state`**：`os.replace` 需要同一個檔案系統（C-7）；mirror 在 `<cache>/sessions`，影子與它同一個 mount 才不會變成跨裝置複製。`state` 可能在別的分割區。
- **為什麼在 mirror 外**：離線重建只讀 mirror：`Index.rebuild_from_mirror` 是 `paths.mirror.glob("*/session.md")`。影子在 `sessions/` 之外，永遠不會被它讀到，所以「未寫完的 session（#119）」「排隊進垃圾桶的」即使留在影子裡也不會被索引（C-4′、G3）。
- **影子裡會有什麼**：遠端**全部**的 `session.md`（列表式 `--include "*/session.md"` 的特性，架構師 §1.4），不只是 `changed`。隱私與磁碟成本見 §7。

### 3.2 要先扣掉已經落地的：`need`

門檻與下載清單都用 `need`，不是 `changed` 的原始數量（C-14）：

```python
def _need(paths: Paths, changed: list[str], remote: dict[str, dict[str, str]]) -> list[str]:
    """changed 裡 mirror 還沒有、或 md5 和這次列表不同的那些。"""
    out = []
    for u in changed:
        local = paths.mirror / u / "session.md"
        if local.is_file() and md5_file(local) == remote[u]["session.md"]:
            continue        # 上一輪被砍在「複製進 mirror」與「寫進 index」之間
        out.append(u)
    return out
```

- 成本：最多讀一次本機 mirror 的總大小（Mac 上約 44 MB，數十毫秒量級；推論，未量）。比一次 rclone 便宜幾個數量級。
- `need` 與 `changed` 的差別只有兩種情況：index 記了 md5 但 mirror 的檔不見了（`need` 有、`changed` 也有），以及 index 沒記但 mirror 的檔已經對（`need` 沒有、`changed` 有——上一輪死在複製與寫 index 之間）。第二種正是續傳要的效果。
- `need` 為空 → **不呼叫 rclone**；後面逐一核對會發現 mirror 已是對的，也不會 `copyto`。這比現行少了不必要的呼叫（見 §8 第 1 條）。

### 3.3 門檻與常數（C-9）

新增模組常數，**不新增環境變數、不加 CLI 旗標**：

```python
#: need 達到這個數才走列表式（C-14）；影子是熱的時候固定成本只剩列表加本機比對
BULK_DOWNLOAD_MIN = 10
#: 列表式那一條的並行度（量測 142 秒 對 245 秒）
BULK_TRANSFERS = 16
BULK_CHECKERS = 16
```

- 選 10 的理由：「追加」(c) 的算法（10 × 0.64 秒 ≈ 6 秒 ≈ 列表的 5 秒固定成本）。原 §1.3 的 20～50 是「影子不存在、每次都要整份下載」時代的建議，§8 第 4 條請架構師確認版本。
- 門檻只影響**速度**，不影響結果（§1.3），所以不給使用者調。

### 3.4 列表式那一次 rclone（C-2、C-13）

```python
drive._run("copy", "gdrive:sessions", str(paths.staging),
           "--include", "*/session.md", "--fast-list",
           "--checksum", "--transfers", str(BULK_TRANSFERS),
           "--checkers", str(BULK_CHECKERS))
```

- 一律經 `Drive._run`，所以照舊帶 `--config`、`--ask-password=false`、`--drive-root-folder-id`（`folder_id()`）。`AGORA_FOLDER_NAME` 因此照舊有效。
- 旗標說明：`--include "*/session.md"` 只比對第一層資料夾裡的 `session.md`（rclone 的 `*` 不跨越 `/`）；`--fast-list` 用一次遞迴列表算要搬什麼；`--checksum` 用 md5（Drive 有 md5）判斷影子裡的檔要不要更新，這是續傳的機制；`--transfers`／`--checkers` 是 §1.2 量到的並行度。
- 不加 `-q`：現在的批次呼叫也沒有 `-q`，輸出照舊被 `_run` 的 `capture_output` 收走。
- **不帶 `--files-from`**（C-2）：這一條的意義就是不要再逐檔解析路徑。

### 3.5 鎖與續傳（C-11、C-12）

- 影子是**跨行程保留**的內部狀態，一次只讓一個行程寫它：`<cache>/download-staging.lock` 上的 `fcntl.flock(LOCK_EX)`，**阻塞等待**，寫法照 `_config_lock`（進 `open(..., "a")`，`flock`，`yield`）。不是 `LOCK_NB`＋略過：略過就會退回每份 0.64 秒，等則前一個行程做完時影子已經是熱的。
- 鎖的範圍只包住「列表式 rclone ＋ 把驗過的檔複製進 mirror」這一段。**不含**列表（`Drive.list_sessions`）與逐一核對迴圈：那兩段不寫影子。
- 續傳怎麼成立：`--checksum` 讓 rclone 跳過影子裡 md5 已對的檔；影子裡沒有的、或 md5 不同的才搬。第一輪被砍掉時，已完成的檔留在影子，第二輪只補剩下的（C-11）。
- 被 SIGKILL 時會不會留下半截檔：rclone 預設先寫暫存檔再改名（架構師標「未逐行驗」），所以影子裡的檔不是半截的。就算真的留了半截，md5 對不上就不會被複製進 mirror（§3.6），代價只是下一輪再抓一次——**不會壞資料**，這是這個設計的關鍵安全網。
- 行程死掉鎖自動放掉（flock 是 fd 上的鎖），所以被砍掉不會留下永遠拿不到的鎖。

### 3.6 只複製 md5 相符的 `changed` 進 mirror（C-3′、C-7）

```python
def _publish(paths: Paths, ulid: str, md5: str) -> bool:
    src = paths.staging / ulid / "session.md"
    if not src.is_file() or md5_file(src) != md5:
        return False            # 下載途中被更新、或沒抓到：交給逐一核對
    dest = paths.mirror / ulid / "session.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name, suffix=".tmp")
    os.close(handle)
    try:
        shutil.copyfile(src, tmp)       # 複製，影子不動
        os.replace(tmp, dest)           # 同一個檔案系統，讀者只看得到舊的或新的
    finally:
        Path(tmp).unlink(missing_ok=True)
    return True
```

- **複製，不搬走**（「追加」(a)(4)）：影子保持完整，下一次只要抓變了的。這是續傳能跨輪成立的前提。
- `os.replace` 在 mirror 目錄裡做的暫存名是 `session.md…tmp`，不是 `session.md`，所以不會被 `rebuild_from_mirror` 的 `*/session.md` 撿到，也不會被任何讀 mirror 的程式看到半截檔。
- md5 用「影子檔的實算值」比「這次列表的 md5」，不是比 modtime：Drive 的 modtime 不可靠，md5 才是 §1.3 那一輪核對用的判準。
- 只有 `changed` 會被考慮（不是影子裡的全部）：G3／#119、垃圾桶佇列因此不受影響。

### 3.7 失敗與退回逐檔（C-5、C-8）

- 列表式 rclone 丟 `StoreError` → 沿用現有訊息「批次下載失敗，改成逐一下載：{e}」，接著照舊對每個 `need`／`changed` 逐一核對、必要時 `copyto`。失敗路徑**不新增訊息、不改 exit code**。
- 列表之後才消失的 ulid：影子裡沒有它 → 不複製 → 逐一核對的 `copyto` 失敗 → 既有的「讀不到，先跳過」，sync 不中斷（C-8）。
- 列表之後才更新的：影子拿到新版本、md5 等於新版本但不等於這次列表 → 不複製 → 逐一 `copyto` 拿到同一份新版本、md5 仍不等於列表 → 照現況處理（index 記列表的 md5，下一輪自癒）。和現在 `--files-from` 遇到同樣競爭時一樣，不是新行為。
- **失敗時要不要先把影子裡 md5 相符的檔複製進 mirror**：本 note 認為要（續傳的重點；不影響正確性，md5 把關），但這與 C-5 的字面「每個 `changed` 各一次 `copyto`」衝突，列在 §8 第 2 條等裁定。

### 3.8 影子的清理（C-4′）

- **只在列表完整時**清（`missing` 為 False，即 `Drive.list_sessions` 真的回來了）：遠端已經沒有的 ulid，從影子刪掉（`shutil.rmtree(paths.staging / name, ignore_errors=True)`）。和 Q4「標記只在列表完整時才動」是同一個條件、同一個理由。
- 放在 `sync` 的 `if not missing:` 分支（那一段已經有 `mark_missing` 的候選），新增一個私有函式 `_trim_staging(paths, remote)`。**不**放進 `download_many`：它拿不到 `missing`，而且清理不是下載。
- 成本：一次 `iterdir`，可忽略。清單以外的殘檔（被砍掉時留下的 `.tmp`、空資料夾）順手清掉。

### 3.9 接縫與介面

改動集中在三個地方，`sync` 的呼叫點與逐一核對迴圈**不動**：

1. `Paths`：加 `staging`、`staging_lock` 兩個 property（純加法）。
2. `Drive.download_many`：多一個必要參數，傳這次列表的 md5：

   ```python
   def download_many(self, ulids: list[str], mirror: Path,
                     md5s: dict[str, str]) -> None:
       """need 達到門檻時走一次列表式 copy 到影子，再只複製 md5 相符的檔進 mirror；
       否則照舊用一次 --files-from。"""
   ```

   `md5s` 是 `{ulid: 這個 session.md 的 md5}`（由 `remote[ulid]["session.md"]` 組）。**做成必要參數、不做成選用**：選用等於多一條「沒有 md5 就照舊」的分岔，正是 D-2 不想要的第二條路。呼叫點只有 `sync` 一處（上一輪 615403b 的 grep 與這一輪讀到的 `sync` 都只看到這一個）。
3. `sync`：`changed` 之後多一行 `need = _need(paths, changed, remote)`，然後

   ```python
   try:
       drive.download_many(need, paths.mirror, {u: remote[u]["session.md"] for u in need})
   except StoreError as e:
       say(f"批次下載失敗，改成逐一下載：{e}")
   ```
   以及 `if not missing:` 分支裡的 `_trim_staging(paths, remote)`。

`download_many` 的內部形狀（私有函式，名字待實作決定）：

```
if not ulids:                    return          # 呼叫方傳進來的已經是 need
if len(ulids) < BULK_DOWNLOAD_MIN:  舊的 _with_files_from 那條（C-1）
else:                           列表式（持鎖）→ 逐個 _publish（只複製 md5 相符的）
```

門檻判斷以呼叫方傳進來的 `need` 為準（C-14），`download_many` 不再自己算一次 `need`（避免兩處定義漂移）。`sync` 外層的 `if len(changed) > 1` 照舊留著（0 與 1 筆的路徑不動，C-10）；`changed` 多但 `need` 空時，`download_many` 收到空清單就直接回來，不呼叫 rclone。

### 3.10 沒有變的東西（逐條點出來，給審查者對）

- `--files-from` 那條的參數一字不改：`copy gdrive:sessions <mirror> --no-traverse --files-from …`，清單檔仍由 `_with_files_from` 寫在 `where`（mirror）裡並在 `finally` 刪掉（G1）。
- `Drive.fetched` 的語意不變：`download_many` 現在不動它，新的兩條路也不動；只有 `download()`（`copyto`）會加一。`pull` 用它分辨「跳過」與「抓了」（P3）。
- `sync` 的 `strict` 行為、戳記寫入時機、`unfinished.json`、`mark_missing` 全部不動。
- mirror 結構仍是 `<cache>/sessions/<ulid>/session.md`。
- 沒有新的環境變數、CLI 旗標、輸出、exit code。

## 4. 契約 C-1～C-15（架構師報告的原文；標 ′ 的是取代版，實作與測試只依 ′ 版）

「追加」取代 C-3、C-4，並新增 C-11～C-14；核准時（§10）再取代 C-1、C-5、C-7，並新增 C-15。原文留在表裡當作歷史。

| 編號 | 取代版（核准時定，實作依這一欄） |
|---|---|
| C-1′ | `changed` 超過 1 筆、`need` 有 1 筆以上、而且少於 `BULK_DOWNLOAD_MIN` → 恰好一次 `copy gdrive:sessions <mirror> --no-traverse --files-from …`，清單只有 `need` 的名字，其他參數和現在一字不差；不出現 `--include`。`need` 為空 → 不呼叫 rclone |
| C-5′ | 列表式 copy 失敗（`StoreError`）→ 說「批次下載失敗，改成逐一下載：…」（訊息不變）；**仍在鎖內**把影子裡 md5 等於列表的 `need` 複製進 mirror（同 C-3′）；其餘的 `need` 由既有的逐一核對各一次 `copyto`。最後的 mirror 與 index 和成功時相同；exit code 不變 |
| C-7′ | mirror 結構不變：`<cache>/sessions/<ulid>/session.md`。從影子進 mirror 一律是**複製**：先寫 mirror 同目錄的暫存檔，再 `os.replace`；影子檔不動、不搬走 |
| C-15 | 清理影子（遠端已不存在的 ulid）只在列表完整時做，而且要拿到 `download-staging.lock`。用非阻塞方式拿：拿不到（另一個行程正在大量下載）就略過這一輪的清理，不等待、不報錯 |

| 編號 | 契約（輸入 → 輸出或錯誤） | 出處 |
|---|---|---|
| C-1 | 「`changed` 少於門檻（且超過 1 筆）→ 恰好一次 `copy gdrive:sessions <mirror> --no-traverse --files-from …`，參數和現在一字不差；不出現 `--include`」 | §3.2 |
| C-2 | 「`changed` 達到門檻 → 恰好一次 `copy gdrive:sessions <暫存目錄> --include */session.md --fast-list`（經 `_run`，帶 `--drive-root-folder-id`）；不出現 `--files-from`；而且**之後沒有任何 `copyto`**（證明批次真的拿到了檔案，不是被逐一核對兜底）」 | §3.2 |
| C-3 | 「C-2 之後，mirror 裡只有 `changed` 的 `<ulid>/session.md` 被新增或更新；遠端還有的其他 session.md（例如未寫完而跳過的 #119、排隊進垃圾桶的、沒變的）**不會**出現在 mirror，或維持原樣（G3）」 | §3.2（被 C-3′ 取代） |
| C-3′ | 「大量下載之後，mirror 只會新增或更新 `changed` 裡、而且影子檔 md5 等於 agora 列表的 `<ulid>/session.md`（**複製**，影子保留）。其他 ulid（跳過的未寫完 session、排隊進垃圾桶的、沒變的）在 mirror 裡不出現，或維持原樣。」 | 追加 |
| C-4 | 「暫存目錄在成功與失敗後都不存在；兩個行程同時跑時，暫存目錄名不相撞（帶 pid）」 | §3.2（被 C-4′ 取代） |
| C-4′ | 「影子目錄 `<cache>/download-staging` 跨行程保留；它永遠不會被 `rebuild_from_mirror` 讀到。列表完整時，遠端已不存在的 ulid 會從影子裡刪掉。」 | 追加 |
| C-5 | 「列表式 copy 失敗（`FAKE_RCLONE_FAIL` 命中）→ 說「批次下載失敗，改成逐一下載」，每個 `changed` 各一次 `copyto`，最後的 mirror 與 index 和成功時相同」 | §3.2 |
| C-6 | 「md5 比對不變：暫存目錄拿到的檔 md5 和列表不同時（測試用 `FAKE_RCLONE_CLEAN_AFTER` 在列表之後換掉內容）→ 逐一 `copyto` 一次；index 記的 md5 和現在的行為相同」 | §3.2 |
| C-7 | 「mirror 結構不變：`<cache>/sessions/<ulid>/session.md`；搬進 mirror 用 `os.replace`（同一個檔案系統）」 | §3.2 |
| C-8 | 「列表之後才消失的 ulid（暫存目錄沒有它）→ 逐一 `copyto` 失敗 → 「讀不到，先跳過」，sync 不中斷，其他 ulid 照常進 index」 | §3.2 |
| C-9 | 「不新增環境變數或 CLI 旗標；門檻是模組常數（測試用 monkeypatch 改它，來測門檻兩側）」 | §3.2 |
| C-10 | 「熱快取的回歸：`changed` 為 0 或 1 時，和現在完全一樣（0 不呼叫，1 走 `copyto`）」 | §3.2 |
| C-11 | 「第一輪在大量下載途中失敗或被砍掉（測試用 `FAKE_RCLONE_FAIL` 或假 rclone 只複製一部分）→ 下一輪的大量下載只抓影子裡還沒有、或 md5 不同的檔（看 `copies.log`）。」 | 追加 |
| C-12 | 「兩個 sync 同時要大量下載時，由 `download-staging.lock` 排隊，不會同時跑兩個 rclone 寫進影子。」 | 追加 |
| C-13 | 「列表式路徑帶 `--checksum --transfers 16 --checkers 16`（常數）；`--files-from` 路徑的參數不變（C-1）。」 | 追加 |
| C-14 | 「門檻比較的是 `need`，也就是扣掉「mirror 已有、md5 正確」之後的數量，不是 `changed` 的原始數量。」 | 追加 |

改壞驗證（架構師 §3.2 末段要求的三條）：「直接寫進 mirror」（C-3′ 要紅）、「不用暫存目錄就不刪」（C-4′ 要紅）、「門檻判斷反過來」（C-1／C-2 要紅）。

## 5. 測試計畫

測試放在 `tests/unit/test_bulk_download.py`（第一行照既有寫法 `import _guard`；用 `test_cache.py` 的 `drive` fixture 形狀：假 rclone 包裝腳本＋`FAKE_REMOTE`）。跑 `uv run pytest tests/unit/test_bulk_download.py tests/unit/test_cache.py`。

### 5.1 假 rclone（`tests/fakes/fake_rclone.py`）要補的

現在 `copy` 只有兩條：帶 `--files-from` 的（兩個方向都走那條）與「本機 → 遠端」的。**不帶 `--files-from` 的列表式 `copy` 目前會被當成本機 → 遠端**：`resolve("gdrive:sessions")` 不是 `None`，但 `Path("gdrive:sessions")` 不存在，於是什麼都不做、照樣成功——測試會因為逐一核對兜底而「看起來也過」。這是這張單最容易假綠的地方。要補：

1. **遠端 → 本機的 `copy`（沒有 `--files-from`）**：`resolve(params[0])` 不是 `None` 時，把來源樹複製到 `resolve(params[1])` 或本機路徑；放在現有「本機 → 遠端」那一支**之前**判斷。
2. **`--include <glob>`**：以相對路徑比對，`*` 不跨越 `/`（和 rclone 相同：`*/session.md` 只比對第一層資料夾裡的 `session.md`）；可以重複出現多個。
3. **`--fast-list`**：接受並忽略。
4. **`--transfers <n>`／`--checkers <n>`**：接受，跳過它們的值（**不要把 `<n>` 當成路徑**——這是 `params` 是位置清單時最容易踩的坑）。
5. **`--checksum`**：目的地已有、md5 相同的檔不複製；影子裡沒有的、md5 不同的才複製。這是 C-11 測得出來的前提。
6. **`copies.log`**：列表式那一次也寫一筆，形狀沿用現有的 `{"src": …, "files": [{"name", "size", "mtime"}…]}`，但 **`files` 只記實際複製的檔**（`--files-from` 那條照舊記「清單上的名字」，既有測試靠它）。`test_cache.py` 的 `copied_since` 會把 `ulid/session.md` 取成 `session.md`，兩條路都吃得到。
7. **`FAKE_RCLONE_FAIL`**：needle 用 `--include` 就能命中列表式那一次（照現行比對方式即可，不用改）。
8. **部分複製旋鈕（C-11 需要）**：例如 `FAKE_RCLONE_BULK_PARTIAL=<n>`：只複製前 n 份到目的地，然後以 rc=1 結束（模擬被砍掉或途中失敗）。下一輪不帶這個變數時，`--checksum` 會跳過已複製的，`copies.log` 只剩缺的那些。
9. `FAKE_RCLONE_CLEAN_AFTER` 的 needle 也用 `--include`：在列表式 copy 之後換掉某一份的內容，讓影子裡是舊/新版本，驗 C-6。

### 5.2 測試清單（契約 → 測試）

| 測試 | 契約 | 怎麼驗（確定性，不靠睡眠） |
|---|---|---|
| T-1 | C-1 | 3 筆 `changed`（門檻 monkeypatch 成 10）→ `calls.log` 恰有一次 `copy`，帶 `--no-traverse --files-from`，**沒有** `--include`；`copies.log` 那一筆帶 3 個名字 |
| T-2 | C-2 | 門檻 monkeypatch 成 2、3 筆 `changed` → 恰一次 `copy` 帶 `--include */session.md --fast-list --checksum --transfers 16 --checkers 16`，**沒有** `--files-from`；記下 `calls.log` 的 mark，sync 之後**沒有任何 `copyto`** |
| T-3 | C-3′（G3） | remote 上多放一份「未寫完」（raw md5 對不上）與一份「排隊進垃圾桶」的 session.md：前者不在 `changed`、後者在 `trashing` → 大量下載之後，mirror 裡**沒有**這兩個 `ulid/session.md`，影子裡有（影子的存在不影響 mirror 的內容） |
| T-4 | C-4′前半 | 大量下載成功後 `paths.staging` 仍存在且保有檔案（不是「用完就刪」）；`rebuild_from_mirror`（`Index` 版本 bump 或刪 db）出來的索引**不含**只有影子才有的 ulid |
| T-5 | C-4′後半 | 先放一份遠端已不存在的 ulid 在影子裡；一次**完整**的 sync 之後它從影子消失。改壞：拿掉 `_trim_staging` → 紅 |
| T-6 | C-4′後半（反側） | 列表**不完整**（`FAKE_RCLONE_OMIT`）時，影子裡那個孤兒**不**被刪 |
| T-7 | C-5 | `FAKE_RCLONE_FAIL=--include` → 訊息含「批次下載失敗，改成逐一下載」；mirror 與 index 和成功時相同（§8 第 2 條若裁定「失敗也先複製驗過的檔」，這裡改成：驗過的檔不 `copyto`，其餘每個各一次） |
| T-8 | C-6 | `FAKE_RCLONE_CLEAN_AFTER` 在列表式 copy 之後換掉一份的內容 → 那一份被 `copyto` 一次，其餘沒有；index 記的 md5 和現況相同 |
| T-9 | C-7 | mirror 裡每個 `<ulid>/session.md` 都在，且沒有殘留 `*.tmp`；`paths.mirror` 底下不出現 `download-staging` |
| T-10 | C-8 | 列表之後刪掉一個 ulid 的 Drive 資料夾 → 該 ulid 沒有進 mirror、訊息「讀不到，先跳過」，其他 ulid 照常進 index |
| T-11 | C-9、C-10 | `changed` 為 0：沒有 `copy`、沒有 `copyto`；為 1：一次 `copyto`、沒有 `copy`；monkeypatch `BULK_DOWNLOAD_MIN = 2` 才能讓 T-2 成立（證明它是常數，不是環境變數） |
| T-12 | C-11 | 第一輪 `FAKE_RCLONE_BULK_PARTIAL=1` → 第二輪（刪掉 `last-sync`）的 `copies.log` 只記剩下的檔，不是全部 |
| T-13 | C-12 | 列表式 copy 時嘗試對 `staging_lock` 用另一個 fd `flock(LOCK_EX \| LOCK_NB)` → 拿不到（證明持鎖）；另加一個冒煙：另一個執行緒先持鎖，sync 在第二個執行緒跑，等它的列表出現在 `calls.log` 之後斷言「還沒有列表式 copy」，放鎖後再斷言有——用事件不用睡眠 |
| T-14 | C-14 | 3 筆 `changed`，其中 2 筆 mirror 裡已有且 md5 正確（index 沒記它們）→ `need` 只有 1 筆；門檻設 2 → **不呼叫**列表式，走 `--files-from` 帶 1 個名字。改壞：拿 `len(changed)` 當門檻 → 走列表式 → 紅 |

### 5.3 改壞驗證（至少這五處；在副本裡做，原 worktree 不動）

1. 直接把列表式 copy 的目的地改成 mirror（不用影子）→ T-3 要紅（G3）。
2. 列表式成功後刪掉整個影子（回到原案）→ T-12 要紅（續傳沒了）；T-4 也要紅。
3. 門檻判斷反過來（`len(need) < BULK_DOWNLOAD_MIN` 走列表式）→ T-1／T-2 要紅。
4. 拿掉 md5 比對（只要影子有檔就複製進 mirror）→ T-8（C-6）要紅。
5. 拿掉 `--checksum` → T-12 要紅（第二輪重抓全部）。

架構師要求的三條（直接寫進 mirror、不刪、門檻反轉）都在上面。**如果某一處改壞沒有讓測試轉紅，要寫進回報**——那代表測試沒守住。

### 5.4 要跑的指令

```
uv run pytest tests/unit/test_bulk_download.py -q
uv run pytest tests/unit/test_cache.py tests/unit/test_store.py tests/unit/test_store_more.py tests/unit/test_cli_sync.py -q
```

新增的假 rclone 分支會動到既有測試共用的 fake，實作那張單要**全 unit 重跑綠**（#364 完成判準）；整套與 Linux worker 的重跑由 PM 做。

## 6. 對 D-2／#316／G3／#119 的影響

| 不變式 | 影響 |
|---|---|
| decisions **D-2**（`store.sync` 是 Drive／outbox → 索引的唯一入口；不為了測試加模式） | **不影響**：仍然在 sync 裡、仍是同一個呼叫點。`download_many` 的新參數是資料（md5），不是模式；不做成選用是為了不長出第二條路（§3.9）。 |
| **D-1**（`upload_batch` 只上傳；索引只由寫入者與 sync 寫） | 不影響：只動下載；新程式不寫索引（複製進 mirror 之後仍由 `sync` 的 `index.put` 寫）。 |
| **D-3／D-4**（strict、最後一次寫回） | 不影響：列表、strict 語意、錯誤往上傳都不變；批次失敗仍退回逐一下載。 |
| **D-5～D-7** | 不影響（D-7 的 `config.json.lock` 不共用；新鎖是 `download-staging.lock`，另一把、另一件事）。 |
| **G3**（未寫完的 session 不進 mirror） | 不影響，**前提是影子在 mirror 外**：`_publish` 只處理 `changed`；`rebuild_from_mirror` 只讀 `mirror/*/session.md`。未寫完的照舊「讀完 header 就從 mirror 刪」（`local.unlink(missing_ok=True)`）。被砍在「複製進 mirror」與「讀 header 判未寫完」之間的窗口，現在用 `copyto` 也一樣存在，不是新問題。 |
| **#119／`unfinished.json`** | 不影響：格式、寫入時機（只在列表完整時）、判斷條件都不動。影子對 #119 只有一個好處：跳過的 session 若之後寫完，下一次仍是一次列表式。 |
| 垃圾桶佇列（本機刪了的不拉回來） | 不影響：排隊中的 ulid 不在 `changed`，不會被 `_publish`；影子裡留著也不會被索引。 |
| 標記（Q4：列表完整時才動） | 不影響：那一段沒動；`_trim_staging` 用同一個條件，但不是標記。 |
| 對外行為（CLI、輸出、exit code、檔案結構） | 不變。 |
| 新增的內部狀態 | 一個持久目錄（`<cache>/download-staging`）＋一把鎖（`download-staging.lock`）＋三個常數。不是對外介面，所以仍是 d；但要記一條 decisions（見 §9）。 |

## 7. 風險與沒驗到的

**風險**

1. **影子占空間**：遠端**全部** `session.md` 的第二份副本（Mac 量到 44 MB；worker 上 remote 相同量級）。加上 mirror 的 44 MB，一個 cache 約兩倍。只在列表完整時清孤兒，所以長期約等於 remote 的 session.md 總量。
2. **隱私**：影子含遠端全部 session.md，包括別台機器、未寫完的。worker 上本人已裁定「可以存全部」（MLP PLAN 的本人裁定，11-03 04:17Z），Mac 本來就有全量 mirror，所以不是新的暴露面；但這是新增的落地檔案，要寫進 note（就在這裡）與 decisions。真正的隱私界線仍是 Drive token（REPORT-arch-agora-639 §3），不是鏡像位置。
3. **鎖的等待會吃掉額度**：兩個行程同時要大量下載時，第二個要等第一個做完（可能整段 142 秒以上），等的期間 sidecar 的 120 秒可能已經到了。最壞情況是「兩個都只等到鎖就逾時」——見 §8 第 5 條。
4. **第一次升級時是冷的**：Mac 第一次跑新版要整份 44 MB 到影子（142 秒），之後才熱。這一次有續傳，但會發生一次。
5. **`--include` 讓每次大量下載都掃全部 session.md**（不只是 `need`）：rclone 要列 1371 個來源檔並比 md5。影子熱時這是固定成本（架構師 §1.4 認為可接受）。中間地帶（2000 份裡 100 份變了）會多花列表與比對，但不會多傳。
6. **`os.replace` 依賴同一個檔案系統**：影子與 mirror 都在 `<cache>`，成立；但 `AGORA_CACHE_DIR` 底下若有 mount 邊界（例如有人把 `sessions/` 掛出去），會變成跨裝置，`os.replace` 會失敗。這是部署層面的假設，寫在這裡。
7. **假的綠**：假 rclone 目前對列表式 `copy` 會靜默成功（§5.1 第 1 條）。實作那張單一定要先補 fake，否則 C-2～C-4′ 全是假綠。

**沒驗到（照架構師報告，我這一輪也沒有補）**

- rclone 對 `--files-from` 的實際 API 行為（逐檔解析路徑）：沿用 MLP 的推論，沒讀 rclone 原始碼。
- `--checksum` 對 Drive 檔案的判定（Drive 對非 Google 文件的檔提供 md5；`session.md` 是普通檔，推論可用，未實測）。
- 被 SIGKILL 時 rclone 是否真的不留半截檔（架構師標「未逐行驗」；就算留了，md5 對不上不會進 mirror）。
- worker 上的實際時間：142 秒是 Mac 量到的，worker 約 1.3 倍是推論。
- `--include` 上千條時的過濾成本、`--include-from` 變形：留給後續量測（架構師 §1.4、m4）。
- 影子熱了之後「列表＋本機比對」的實際耗時（Mac 上應為數秒級，未量）。

## 8. 待確認（架構師報告內部的讀法衝突；本 note 不自己裁）

1. **門檻與 `--files-from` 清單比的是 `changed` 還是 `need`？** C-1 寫「`changed` 少於門檻」，C-14 寫「門檻比較的是 `need`」，原 §1.3 寫 `len(changed) >= BULK_DOWNLOAD_MIN`。另外 `need` 為空但 `changed` ≥ 2 時，C-1 字面上要求「恰好一次 `--files-from`」，但那一次什麼都不用抓。本 note 採「比 `need`、清單也用 `need`、`need` 為空就不呼叫」（§3.2、§3.9），請架構師確認是否要改寫 C-1 的措辭。
2. **C-5「每個 `changed` 各一次 `copyto`」與續傳衝突。** 列表式失敗時，影子裡可能已經有大部分驗過的檔；本 note 認為失敗後仍應把 md5 相符的複製進 mirror（續傳的重點，且不影響正確性），那就只對缺的 `copyto`。C-5 沒有被「追加」取代，字面上會擋住這個做法。請裁定：改寫成 C-5′（「失敗時仍複製影子裡驗過的檔，其餘每個各一次 `copyto`」），或維持字面（失敗路徑不用影子）。
3. **C-7 的「搬」與「追加」(a)(4) 的「複製，不搬走」。** 兩處的動詞不同。本 note 讀成「複製進 mirror，用 `os.replace` 落地；影子不動」（§3.6），請確認。
4. **門檻數值：10 還是 20～50？** 原 §1.3 建議 20～50（影子尚未存在時的估算），「追加」(c) 建議 10（影子熱了之後）。本 note 用 10（§3.3）。
5. **鎖阻塞等待的退路。** 「追加」(a)(2) 說「阻塞等待，最長的等待由 sidecar 的逾時決定」。但被逾時砍掉的那一輪什麼都沒拿到，兩個行程可能都在等鎖。要不要在「等鎖超過某個時間」時退回 `--files-from`（保證有進度、但慢）？那是行為決定（多一條路徑），本 note 照原文只做阻塞等待，請架構師定。若不定，這一條會是 worker 上仍可能卡住的殘餘風險（§7 第 3 條）。
6. **`_trim_staging` 放哪。** 它需要 `missing`（只有 `sync` 有），但 `download_many` 拿不到；本 note 放進 `sync` 的 `if not missing:` 分支（§3.8）。這是形狀問題（架構師），我先提議，請確認。

另有一條不是衝突、只需要確認版本：**C-3／C-4 已被 C-3′／C-4′ 取代**（「追加」明說）。本 note 兩個版本都列，實作只依 ′ 版；若架構師認為原版要留作歷史，請在核准時說一聲。

## 9. PR 切分（一個 PR，d）

1. `tests/fakes/fake_rclone.py`：§5.1 的九項（**先做，不然測試是假的**）。
2. `src/agora/store.py`：`Paths.staging`／`staging_lock`；三個模組常數；`Drive.download_many` 的新參數與兩條路；私有的 `_need`／`_publish`／`_trim_staging`（或等價）；`sync` 的 `need` 與 `_trim_staging` 呼叫。
3. `tests/unit/test_bulk_download.py`：T-1～T-14。
4. `docs/perf.md`：在「PM 的處理」那一段補一條（冷快取下載改成列表式＋續傳，附 §1.2 的數字），並改寫既有的「冷啟動改成一次批次下載（已做）」那句，免得兩句互相矛盾。
5. `docs/architecture/decisions.md`：**架構師加**（本 note 提議一條：「大量下載用 mirror 外的持久影子目錄，只把 md5 驗過的 `changed`（`need`）複製進 mirror」），我不改這個檔。

順序：架構師核准本 note 與 §8 的六條 → 實作（含 fake）→ 架構師結構審、非作者審正確性與測試 → PM merge。MLP 那邊的 B＋D（快取放 volume、拉長 `import_timeout`）與本單不互相依賴，可以同時進行。

## 10. 架構師裁定（2026-11-03，核准者模式）

核准本 note。§8 的六條裁定如下；對應的契約改寫已放進 §4（C-1′、C-5′、C-7′、C-15）。

1. **門檻與清單都比 `need`。** 門檻比較 `len(need)`，`--files-from` 的清單也只放 `need`；`need` 為空就不呼叫 rclone。C-1 改寫為 C-1′。sync 外層的 `if len(changed) > 1` 維持（C-10 的 0 與 1 筆路徑不動）。
2. **C-5 改寫為 C-5′**：失敗時仍把影子裡 md5 相符的 `need` 複製進 mirror，其餘才 `copyto`。理由：影子裡的檔若 md5 等於列表，就是正確的位元組；rclone 這一次失敗，不影響已經完成的檔（半截檔的 md5 對不上，不會被複製）。不這樣做的話，續傳只在「成功的那一輪」生效，被砍掉之後的那一輪若又失敗，就會退回每份 0.64 秒，正是要避免的情況。
3. **確認：複製進 mirror，用 `os.replace` 落地，影子不動。** C-7 原文的「搬」是在影子設計之前寫的，以 C-7′ 為準。
4. **門檻用 10。** 量測給的數字：逐一路徑每份約 0.64 秒；影子熱了以後，列表式的固定成本約等於列表的 5 秒，所以 10 份（約 6 秒）是交叉點。影子還是冷的那一次（第一次升級）會整份下載一次，但有續傳，只發生一次。20～50 是影子設計之前的估算，作廢。
5. **鎖：阻塞等待，不設退路。** 理由：持鎖的行程每一輪都會把進度留在影子裡，就算它被砍掉，鎖也會隨行程結束釋放，下一個拿到鎖的行程從影子繼續，所以整體一定會收斂，不會互相鎖死。若「等太久就退回 `--files-from`」，兩條路會同時搶 Drive 的配額，又多一條要測的路徑，換到的只是被砍掉那一輪少等一些。等鎖的那一輪被 sidecar 砍掉，代價只是浪費那一輪，不會丟進度。§7 第 3 條的風險因此降為「多花一輪」；MLP 的 D（`import_timeout` 約 300）會讓它幾乎不發生。
6. **`_trim_staging` 放在 `sync` 的 `if not missing:` 分支，同意。** 另外加一條：它要用**非阻塞**的方式拿 `download-staging.lock`，拿不到就略過這一輪的清理（C-15）。否則它可能在另一個行程的 rclone 正在寫影子時刪掉那個資料夾：另一個行程的列表可能比這一個新，看得到這一輪沒看到的 ulid。

其他確認：
- **C-3／C-4 的原文**保留在 §4 當作歷史，實作只依 ′ 版。
- **實作單的前置**：先補 `tests/fakes/fake_rclone.py`（§5.1 的 1～9 項），而且 PR 裡 fake 的改動要先於使用它的測試。不帶 `--files-from` 的 `copy` 現在會被當成上傳、靜默成功，不補的話 C-2～C-4′ 全是假綠。
- **測試要補**：T-7 依 C-5′ 改寫（驗過的檔不 `copyto`，其餘各一次）；新增 **T-15（C-15）**：測試先持有 `download-staging.lock`，再跑一次完整的 sync → 影子裡的孤兒**不**被刪、sync 不等待也不報錯；放開鎖後再跑一次 → 孤兒被刪。改壞驗證加一條：「清理改成阻塞拿鎖或不拿鎖」→ T-15 要紅。
- decisions 加 **D-21**（同一個 PR，架構師寫）。

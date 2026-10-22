# Agora lite 效能量測

2026-10-02，impl2。腳本 `spike/perf/measure.py`（主）與 `spike/perf/probe_split.py`（拆解冷啟動）（2026-10-04 清理時已從 repo 移除，要看請用 `git show fd3ba9f:<路徑>` 從歷史取回）。
真的 Drive `agora-test`，`AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 都在暫存目錄，
`rclone.conf` 是 symlink。對話只有一段自編的 `claude -p`（`--disallowedTools` 放在 prompt 後面）。
每項跑 3 次取中位數。跑完 purge 掉這次建的 23＋5 個 `sessions/<ULID>`、刪掉自己那個 uuid 的
jsonl 與 `session-env/`，`agora-test` 最後是空的。

## 結果（中位數，秒）

| 項目 | 1 個 Session | 21 個 Session |
|---|---|---|
| sync：空鏡像（要下載全部 session.md） | 2.08 | **85.01** |
| sync：無變化（有節流的情況下仍強制跑） | 0.73 | 1.37 |
| import：export（讀 jsonl＋打包） | <0.01 | — |
| import：stage（寫 outbox 本機） | <0.01 | — |
| import：push（上傳＋驗證 md5） | **11.21** | — |
| import：verify（把 raw 抓回來比對） | <0.01 | — |
| search：有 sync | 1.15 | 1.33 |
| search：`--no-sync` | <0.01 | <0.01 |
| （對照）`claude -p` 一輪自編對話 | 6.92 | — |

冷啟動拆解（`probe_split.py`，5 個 Session，只量自己那幾個）：

| 階段 | 秒 |
|---|---|
| `lsjson -R --fast-list --hash`（一次列全部） | 1.12 |
| 每個 session.md 各下一次（`copyto`） | **3.46／個**（5 個共 17.29） |
| 每個 session 寫進索引（FTS5 trigram） | 0.01（5 個共 0.01） |

## 結論

**最慢的是「對 Drive 每個檔案各跑一次 rclone」。** 量到的單次往返約 3.5 秒，
`push_one` 一個 Session 要 3 次（raw、session.md、`lsjson` 驗證）所以 11.2 秒；
冷啟動 20 個 Session 是 1 次列檔 ＋ 20 次下載 ≈ 1.1 ＋ 69 ≈ 70 秒，加上其餘就是 85 秒。
列檔本身只要 1.1 秒，索引寫入幾乎不要時間（0.01 秒）——所以問題不在 Drive 的檔案多，
而在「呼叫次數」。

**使用者每天會感受到的等待**（用他自己的量級估算）：

| 動作 | 現在 | 說明 |
|---|---|---|
| `agora search session '表格'`（日常最常做） | **約 0.7～1.4 秒** | 有 sync／沒變化時；5 分鐘內重複 search 會被節流跳過，實際上常常是 0 秒 |
| `agora import --format opencode …` | 約 11 秒 | 使用者要站在那裡等 |
| `agora continue-session` 結束後存回 | 約 11 秒 | 而且是在關掉 agent 之後再多等 11 秒 |
| 換新機器／刪掉 `~/.cache/agora` 後第一次 sync | 1 個 Session 2 秒、20 個 Session 85 秒 | 一天只會遇到一次，但 85 秒很明顯 |

結論是「日常的 search 已經夠快（1 秒內），真正會卡住的是寫入（11 秒）」。

**值得改的地方（建議，沒動 src）**：

1. **寫入不要擋在前景**（影響最大）。現在 `push_one` 上傳完還要 `list_one` 驗 md5 才回來，
   那次驗證純粹是 paranoia：S1 的保證是「先傳 raw、最後傳 session.md」，
   讀的一方本來就會用 md5 判斷還沒寫完。可以改成上傳完就回頭，
   驗證交給下一次 `sync` 順手做（`list_sessions` 本來就會回 md5），
   11.2 秒 → 大約 7 秒（少一次往返）。
2. **冷啟動改成一次搬，不要 N 次**。`rclone copyto` 逐檔下載可以換成單一
   `rclone copy gdrive:sessions <mirror> --include '*/session.md' --checksum`
   （一個行程、平行傳輸、並用已列到的 md5 決定要不要抓）。
   20 個 Session 預期從 85 秒降到個位數秒；1 個 Session 也從 2 秒降到 1 次往返。
3. **`--fast-list` 已經在了，列檔不是瓶頸**，不用再優化列檔（1.12 秒，且與檔案數幾乎無關）。
4. 如果不做 1，改成把驗證改成「下一次 sync 再確認」也可以；兩者都做最省。

## 跑過的指令（形狀）

```bash
PERF_LEDGER=/tmp/agora-perf-work/ulids.txt python3 spike/perf/measure.py /tmp/agora-perf-work
PERF_LEDGER=/tmp/agora-perf-work/ulids.txt python3 spike/perf/probe_split.py /tmp/agora-perf-work
rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id <agora-test id> \
  lsjson gdrive:sessions --dirs-only        # 只看有沒有自己的 ULID 留著
```

量到的形狀：`results.json`（13 個中位數）、`probe.json`（4 個階段秒數）、
Drive 上 `agora-test/sessions/` 最後 0 筆、自己那個 uuid 的 `projects/<編碼>/` 與
`session-env/<uuid>/` 都已刪。

## PM 的處理（2026-10-02）

- **冷啟動改成一次批次下載（已做）**：sync 把所有要更新的 `session.md` 用一次 `rclone copy --files-from … --no-traverse` 抓下來，失敗才退回逐一下載。真的 Drive 上 3 個 Session 的冷啟動從約 13 秒（依上表推算）變成 5.8 秒；Session 越多差越多。
- **寫入少一次往返（沒做，留給使用者決定）**：建議 1 會拿掉上傳後的 md5 確認，那是 outbox「確認 Drive 有了才移除」的保證（design 4.1），所以不拿掉。另一個做法是把 raw 與 session.md 用一次 `rclone copy --order-by name --transfers 1` 上傳（`raw-…` 排在 `session.md` 前面，順序仍然成立），可以省約 3.5 秒；但要改寫 fake 與幾個依賴 `copyto` 的測試，也要在真的 Drive 上確認順序，所以留到下一輪。

## 2026-10-21：一次 import 的呼叫清單（#119，改前／改後）

量測方式：`tests/fakes/fake_rclone.py`，`FAKE_RCLONE_DELAY=0.5`（每次呼叫睡 0.5 秒），
`HOME`／`AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 都在暫存目錄，
`AGORA_UPLOAD=inline`，每次都刪掉 `state/last-sync` 讓 sync 真的跑。呼叫數是穩定值；
秒數是三次的中位數（同一次執行裡的假延遲固定，差異來自 process 啟動）。

一次**新 import**（Drive 上沒有同名來源）4 次：

1. `lsjson gdrive:sessions -R --fast-list --hash --files-only`（sync 列檔）
2. `copy <outbox> gdrive:sessions --files-from …`（raw）
3. `copy <outbox> gdrive:sessions --files-from …`（session.md）
4. `lsjson …`（上傳後確認 md5，H1）

一次**更新 import**（同一個來源再匯入，raw 也變了）7 次：

1. sync 列檔
2. `lsjson …`（L7：更新前確認 id 還在）
3. `lsjson …`（copy 前再看一次；resurrect 規則）
4. raw copy
5. session.md copy
6. `lsjson …`（確認 md5）
7. `delete gdrive:sessions --files-from …`（清掉被取代的舊 raw）

**#164（刪除偵測改用資料夾 ID）不改次數**：更新 import 的那 7 次裡，三次列檔（第 1、2、6 次的 `lsjson -R`）
現在不加 `--files-only`，目錄項順便帶 `ID`，仍是同樣的三個呼叫。只有兩種情況才多呼叫：
列表沒帶目錄 ID 時，送出前與確認各多一次 `lsjson gdrive:sessions --dirs-only`（上限 7 → 9，
只有一邊缺則 8）；列表少了某一筆、或確認時 ID 變了，才多一次定向 `lsjson gdrive:sessions/<ULID>`。
正常輪次不變。

Drive 上有一個「卡住的舊 session」（session.md 在、raw 不在）時，每一次指令多一次下載：
`copyto gdrive:sessions/<ULID>/session.md → mirror`（要讀標頭才知道它還沒寫完）。

改前／改後（同樣 0.5 秒假延遲）：

| 情境 | 改前 | 改後 |
|---|---|---|
| 新 import，Drive 乾淨 | 4 次（約 2.3～2.9 秒） | 4 次（約 2.3～2.9 秒） |
| 新 import，有 1 個卡住的 session（第一次看到） | 5 次（約 2.9～3.3 秒） | 5 次（約 2.9～3.3 秒） |
| 同上，**下一個指令** | 5 次 / 2.93 秒 | **4 次 / 2.31 秒** |
| 更新 import，有 1 個卡住的 session | 8 次 / 4.63 秒 | **7 次 / 4.05 秒** |

卡住的 session 現在記在 `<state>/unfinished.json`：`session.md` 的 md5 與它標頭指到的 raw 在
listing 裡的值（不存在＝空字串）都沒變，就不再下載、也不再重印「還沒寫完」；任一個變了
（raw 補上、session.md 更新）就重新讀一次。檔案壞掉或不存在＝沒有記憶，退回一次下載。

**issue 的問題（慢在 Drive 寫入還是舊 session 重試？）**：主要是固定的上傳往返。
一次新 import 4 次、更新 import 7 次；舊 session 的重試在多數情況只多 1 次
（清單上一個 `copyto`），但每次指令都會再付一次。以上面量到的單次往返約 3.5 秒估算，
23 秒比較像「更新 import 的 7 次」而不是「很多卡住的 session」。這次只拿掉重複的那一次；
上傳順序（raw → session.md）、上傳後的 md5 確認（H1）、更新前的存在檢查（L7）與
copy 前的再檢查（resurrect）都沒有動。

## 匯入量測（100／400／800 MB）

腳本 `tests/perf/measure_import_mem.py`（R1 #154 寫成，R4 #175 轉正；2026-10-22 在 R3 全部合併之後的版本重量）。
**不是 pytest、不被 CI 跑**（`tests/perf/` 不在 `tests/unit` 之下，也不叫 `test_*.py`）；任何人都可以在 worktree 根目錄手動跑：

```bash
python tests/perf/measure_import_mem.py --out /tmp/import-mem.json     # 預設 100,400,800 MB 與 1,10 MB 單行；約 1 分鐘
python tests/perf/measure_import_mem.py --sizes 5 --long-lines 1       # 煙霧測試
```

全部隔離：假 HOME、`AGORA_CLAUDE_HOME` 指到假 projects、假 rclone（`tests/fakes/fake_rclone.py`）、`AGORA_UPLOAD=inline`、
`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`TMPDIR` 在 `/tmp/r1-*`，量完立刻刪；**不碰真資料、不連 Drive**。
`agora import` 跑在**新的子行程**，數字是那個子行程的 `resource.getrusage(RUSAGE_SELF).ru_maxrss`（不含產生假 session 與量測腳本自己）。
斷言（design I.8／L15）：`peak(800) ≦ 1.2 × peak(100)` 且 `peak(800) ≦ 256 MiB`；秒數沒有門檻，記下來對照。

假 session 的形狀：一小段對話＋固定大小（約 0.5 MB 的 body、一行約 1 MB）的 `tool_result`；閱讀版（`session.md`）因此只隨輪數長，
不是隨工具輸出長。sidecar 固定 64 KB。

| 大小 | 最長一行 | 峰值 RSS | 匯入秒數 | 閱讀版 | 磁碟（原檔／鏡像／假 Drive） | 上傳（`C` = 4 MiB） |
|---|---|---|---|---|---|---|
| 100.9 MB | 0.51 MB | **37.3 MiB** | 2.02 | 24.5 KB | 100.9／102.0／102.0 MB | 28 檔（段＋`session.md`）、2 次 copy |
| 403.5 MB | 0.51 MB | **35.8 MiB** | 6.82 | 94.8 KB | 403.5／407.9／407.9 MB | 104 檔、2 次 |
| 807.5 MB | 0.51 MB | **36.6 MiB** | 18.8 | 189.4 KB | 807.5／816.2／816.2 MB | 206 檔、2 次 |

`peak(800) 36.6 ≦ 1.2 × peak(100) 37.3`（比值 0.98）✔；`peak(800) ≦ 256 MiB` ✔。上傳的檔數是 `ceil(main_end/C)` 個封存段＋tail＋
`session.md`（R3 之後 Drive 上沒有整份 raw）。這次與其他工作同時在一台機器上跑（秒數有雜訊，只當量級）。

單行很長（design K14；一行就是那個大小）：

| 一行 | 峰值 RSS |
|---|---|
| 1.02 MB | 41.3 MiB |
| 10.21 MB | 136.3 MiB |

磁碟峰值：一次匯入同時會有原檔＋outbox 的整份 raw＋Drive 的段，結束後 outbox 已清空，鏡像與 Drive 各留一份；800 MB 那一筆結束時是
2.4 GB（推得峰值約 3.2 GB）。

### 這些數字的前提與未知

- 前提：每輪上傳量的上限是 **`C + I + S`**（C＝一段 4 MiB、I＝這輪新增、S＝閱讀版 `session.md`）——見下一節 814 MB 多輪的實測；
  匯入量測的是**第一次**（整份、一次）。
- 假資料的 `tool_result` 是固定形狀，**閱讀版大小固定**；真 session 的閱讀版（K6）、`aux`／sidecar 的大小與變動頻率（K5）、
  真實 Claude Code 的 jsonl 是不是真的只追加（`/compact`、跨版本 resume，K4）都**沒有量**：K4 決定多常退回整份，K5 決定 tail 多大，
  K6 是剩下的地板（`session.md` 整份上傳）。這三個要靠真資料，本人在真環境量（只看大小與 metadata）。
- 真 Drive 的 listing 延遲、幾千個檔時的 API 配額（K1、K3、K10）沒有量；假 rclone 只回答流程與位元組。

## 814 MB 多輪的增量量測

2026-10-22，MyAiStorage#170。腳本 `tests/perf/measure_increment.py`（`python tests/perf/measure_increment.py --size-mb 814 --rounds 6 --grow-mb 1 --sessions 20`）。全部隔離：假 HOME、假 rclone、`/tmp/r3b2-*`，量完立刻刪。
每輪是一個**新子行程**做 `export_increment` → `stage(plan)` → `upload_batch`（與 `continue` 寫回同一條路；沒用 `import`，因為 `import` 更新時的「不覆蓋較少內容」比對會把舊 raw 整份讀進記憶體）。`C` = 4 MiB，R1 形狀的假 session（約 1 MiB 一行的 `tool_result`），每輪新增約 1 MiB。上傳位元組是 `copies.log` 裡這一輪送出的檔案大小總和（含 `session.md`）。

| 輪 | 原檔 MiB | 送出檔數 | 送出 MiB | rclone 次數 | 秒 | 子行程峰值 RSS |
|---|---|---|---|---|---|---|
| 0（第一次匯入，整份切段） | 822.0 | 210 | 830.6 | 5 | 17.4 | 37.6 MiB |
| 1 | 823.0 | 2 | 1.12 | 5 | 22.7 | 35.1 MiB |
| 2 | 824.1 | 2 | 2.15 | 5 | 27.3 | 35.1 MiB |
| 3 | 825.1 | 2 | 3.18 | 5 | 25.7 | 35.4 MiB |
| 4（累積超過 C，封存） | 826.1 | 4 | 4.22 | 5 | 27.4 | 35.0 MiB |
| 5 | 827.1 | 2 | 1.12 | 5 | 27.0 | 40.5 MiB |
| 6 | 828.2 | 2 | 2.15 | 5 | 24.7 | 35.2 MiB |

- 第 2 輪起每輪 ≤ `C + I + S`（約 4.2 + 1 + 閱讀版），不再是 ~820 MiB；今天（R1 之前）每輪約 816 MB。tail 隨未封存的新增長大，累積超過 C 才封存一次（第 4 輪）。
- 第一次匯入 210 個檔＝約 `ceil(822/4)`＝206 個封存段＋tail＋`session.md`＋（切點落差的小段），與設計 A.1 的 `ceil(main_end/C)` 一致（設計的 195 是 814 MB ÷ 4 MiB 的算法，這個假 session 大 1%）。
- 每輪秒數 22～27，幾乎都是**讀一遍 800 MB 的來源檔並串流打包**（R1 的 807 MB 匯入是 16.4 秒；這裡另含切段、md5、複製、`lsjson`）；上傳本身只有 1～4 MiB。本機磁碟讀取是下限，沒有因為分段變快或變慢的結論要下。
- 峰值記憶體不隨大小成長（35～40 MiB，與 R1 的 39.1 MiB 同量級）。

listing：一個 814 MB 的 session 跑 7 輪後 Drive 上是 218 個檔（`seg-*`＋`session.md`；舊 tail 在寬限期內還在）。20 個這樣的 session 一次 `lsjson -R`：4360 筆、302.5 KiB、3.3 秒（假 rclone 本機，真 Drive 的延遲未量，設計 K1/K10 仍待真 Drive 量）。

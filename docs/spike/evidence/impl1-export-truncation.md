# impl1：大 Session 的 `opencode export` 被截斷（9.5 e2e 的寫入路徑阻塞）

- 執行時間：2026-09-28 UTC；執行者：impl1
- 環境：`aistorage-resident:latest`（ubuntu 24.04、Python 3.12.3），`opencode 1.18.32`
- 隔離：自己起的 probe 容器（`impl1-probe`／`aistorage-it-export-large`），**沒有**動
  e2e 的容器池、`resident_pool`、git-annex 或任何 Drive 憑證；沒有用到任何模型
  （Session 是 `opencode import` 造出來的），全程不需要網路。
- 判定：**確認原因、換取法、補上完整性驗證**。同步器不再會把截斷的匯出當成原始紀錄。

## 1. 症狀（impl3 在 9.5 找到的）

`tests/e2e/test_persistence.py::test_9_5_…`：

```
rejected/error: opencode:ses_f17a0ff48ffea6PI7ZOa4VXN8p
  ReadError: opencode export 的輸出不是合法 JSON: Unterminated string starting at:
  line 1369 column 23 (char 112592)
```

rc=0，輸出卻是被砍過的 JSON。這是**寫入路徑**的問題，不只 e2e：任何被截斷的匯出
一旦被當成原始紀錄收進 Agora，就再也分不出來了。

## 2. 原因：是 CLI 的 stdout 沒排空，**不是** export 本身

同一個 Session（`ses_huge`，匯出 1 488 140 bytes）只換「stdout 去哪裡」，其他都不動：

| stdout 去處 | 結果（各 4 次） |
|---|---|
| 一般檔案 `> f` | `1488140` ×4 —— 完整 |
| pipe `\| wc -c` | `65536` `65536` `65536` `65536` —— 截斷 |
| pipe `\| cat \| wc -c` | `65536` ×4 —— 截斷 |
| `> /dev/stdout \| wc -c` | `65536` `65536` `65536` `131072` —— 截斷 |
| fifo（`mkfifo`） | `65536` ×4 —— 截斷 |

- **rc 全部是 0**，截斷長度永遠是 **64 KiB 的倍數**（65536／131072）——正好是 Linux
  pipe 的預設緩衝區。`opencode export` 寫完就結束行程，沒有等 pipe 排空；pipe 灌滿
  之前行程就沒了。stdout 是**一般檔案**時走的是另一條寫入路徑，同一個匯出每次都完整。
- 所以這既不是「export 算錯了」，也不是呼叫端讀得太慢：呼叫端一直有在讀
  （`subprocess.run(capture_output=True)` 內部是 `communicate()`），是**子行程自己
  沒等**。

### 2.1 大小掃描（每個匯出 3 次，「truth」＝檔案取法的位元組數）

| 匯出 bytes | truth | pipe | 檔案 |
|---|---|---|---|
| 21 208 | 21 208 | 3/3 完整 | 3/3 完整 |
| 62 248 | 62 248 | 3/3 完整 | 3/3 完整 |
| 103 715 | 103 715 | 65536 / 103715 / 65536 | 3/3 完整 |
| 124 320 | 124 320 | 65536 ×3 | 3/3 完整 |
| 144 925 | 144 925 | 65536 / 131072 / 65536 | 3/3 完整 |
| 144 925 | 144 925 | 65536 ×3 | 3/3 完整 |
| 144 925 | 144 925 | 65536 ×3 | 3/3 完整 |
| 144 925 | 144 925 | 131072 / 65536 / 65536 | 3/3 完整 |
| 206 740 | 206 740 | 131072 / 131072 / 65536 | 3/3 完整 |
| 412 790 | 412 790 | 131072 / 131072 / 65536 | 3/3 完整 |
| 804 285 | 804 285 | 131072 / 65536 / 65536 | 3/3 完整 |
| 1 593 822 | 1 593 822 | 65536 / 131072 / 65536 | 3/3 完整 |

（144 925 出現四次是四個不同 id、不同文字內容的匯入 session，長度巧合相同。）

- ≤62 KB：**6/6 完整**。
- ≥103 KB：**10/10 至少壞一次、29/30 次被截斷**；檔案取法 **30/30 完整**。
- 1 593 822 這個大小另外再跑 3 次（換一個匯入的 session）→ pipe **6/6 全被截斷**。

臨界點在 **pipe 緩衝區（64 KiB）**，不是 128 KiB 也不是某個 magic number。
impl3 觀察到的「28／53 KB 正常、131 KB 起出問題」與此一致。

## 3. 三種取法的取捨

| 取法 | 完整？ | 位元組 = export？ | 結論 |
|---|---|---|---|
| pipe（原本的 `capture_output=True`） | **否** | — | 拋棄 |
| `sh -c 'opencode export … > f'` | 是（33/33） | 是 | 可用，但 session id 會被 shell 二次解讀 |
| `stdout=<檔案物件>`（Python 直接開檔） | 是（33/33） | 是 | **採用** |
| HTTP API 逐則取訊息再組回 | 是 | **否**（見 3.1） | 不採用 |

採用 `subprocess.run([…], stdout=<開好的檔案物件>)` 而不是 `sh -c`：效果完全相同
（子行程那邊的 fd 是檔案），但 session id 不會被 shell 解讀——1.7a 對 id 的格式驗得
很鬆（`ses_`／`msg_`／`prt_` 前綴＋自定字串都收），把 id 拼進命令字串等於把
shell 注入的門留著。

### 3.1 為什麼不用 API 組回（實測，不是推論）

`GET /session/{id}` ＋ `GET /session/{id}/message` 組回來的 JSON **內容完全等價**
（`rebuilt == export` 為 True，訊息數一致），但**欄位順序不同**：

```
export: id slug projectID directory path title agent model version summary cost tokens time
API   : id slug projectID directory path title summary cost tokens version agent model time
```

（export 依 DB 欄位順序，API 依自己的順序。）所以重組出來的位元組對不上。原始紀錄
要求與 `opencode export` 位元組相同（轉換器、checkout／轉接器的開頭都依賴這一點，
ADR 0010），重組會破壞它。要靠 API 組就得把 opencode 每個版本的欄位順序都寫死
（`info`、每種 `info`、每種 `part`：text／reasoning／step-start／step-finish／tool／
file／compaction…），升版就會靜默壞掉——不值得。

## 4. 實作

`src/aistorage/syncer/opencode_api.py`

- `export()` 把 `dest` 的暫存檔（`<dest>.partial`）直接當子行程的 stdout，驗證通過才
  `os.replace` 成正式檔。**失敗時 `dest` 維持原狀、暫存檔也會清掉**——
  `skill/tools._ensure_export` 會直接拿 `dest` 算接續點，混到半份就是錯的接續點。
- 新增 `_verify_export()`：合法 JSON、形狀是 `{info, messages}`、`info.id` 就是要求
  的 id、匯出最後一則訊息在 `GET /session/{id}/message` 上找得到。四項任一不成立就丟
  `IncompleteFetch`。
- 新增 `message_ids()`：`GET /session/{id}/message` 的訊息 id（只當交叉檢查用）。

`src/aistorage/errors.py`

- 新增 `IncompleteFetch`。**刻意不是 `ReadError` 的子類別**，理由與 `TooLarge` 相同：
  `ReadError` 是「讀不到」（連不上、rc≠0、session 不存在），`IncompleteFetch` 是
  「讀到了，但只拿到一部分」。兩者的判讀與處置不同，混在一起就看不出匯出壞在哪。

`syncer/core.py`

- `sync_once` 加了 `except IncompleteFetch`：記成 `errors` 裡的 `incomplete: …`，
  `error_code` 設成 `incomplete`，`last_seen_sha` **不更新**（這一輪並沒有看到完整的
  Session）。因為 `export()` 在驗證前不碰 `dest`，這條路徑上**不可能**有半份被上傳。

### 4.1 為什麼不要求「訊息數相等」

驗證第 4 項只問「匯出的最後一則，API 上還在不在」，不問數量相等。原因：

- Session 正在跑時，匯出之後又長出新訊息 → API 比匯出多（正常）。
- `POST /session/{id}/revert` 會把訊息刪掉 → API 可能比匯出少（正常，1.7c）。

兩種都會讓數量對不上，但匯出都是完整的。要求相等會把正常情況誤判成截斷——那比
漏掉截斷更糟（會讓同步器無限重試）。

## 5. 測試

`tests/unit/test_opencode_export.py`（17 個，不需要 opencode／模型／網路）

- stdout 開成檔案物件、**不用 shell**、不用 `capture_output`；寫出去的位元組與子行程
  寫的完全相同（sha256 相同）。
- 被截斷的輸出（rc=0、砍到一半）→ `IncompleteFetch`，而且 `not isinstance(e, ReadError)`。
- 七種壞形狀（空、`{`、缺收尾括號、缺 `messages`、`messages` 不是清單、缺 `info`、
  匯出的是別的 Session）逐一被擋下，`dest` 與暫存檔都不留。
- `rc=1`（session 不存在）維持 `ReadError`——不要把「讀不到」也說成「取得不完整」。
- 交叉檢查：匯出最後一則不在 API 上 → 擋下；**匯出之後 Session 長大 → 不擋**
  （避免誤判）。
- `sync_once`：取得不完整 → `errors` 裡是 `incomplete: …`、收件匣一個檔案都沒有、
  `error_code == "incomplete"`；對照組（300 KB 完整匯出）照常上傳且上傳的位元組相同。

`tests/integration/test_export_truncation.py`（4 個，**不需要模型**）

自己起容器 → `opencode import` 一個約 300 KB 的 Session（>100 則訊息）→ 開
`opencode serve`（驗證要打 API）：

1. 對照組：舊的 pipe 取法 3 次裡至少一次是壞的（這條是 bug 的存在證明；哪天
   opencode 修好了要改成「已修正」的記錄，不是刪掉）。
2. 新取法連續 3 次，sha256 都等於真值（`sh -c > f` 的位元組），沒有殘留 `.partial`。
3. API 組回來 vs export：內容等價、欄位順序不同 → 記錄「為什麼不選那條路」。
4. 真的 CLI 匯出 ＋ 真的 API：把 API 看到的訊息砍掉尾巴後，`export()` 判定
   `IncompleteFetch`、不是 `ReadError`、`dest` 沒被建立、沒有 `.partial`。

跑法：`pytest -m integration tests/integration/test_export_truncation.py`（預設
`addopts` 會 deselect `integration`，要明確指定；映像不在就 FAIL 不是 skip）。

## 6. resident image：**不用改**

實作沒有引入新依賴，也沒有改 entrypoint／權限／掛載。整合測試用
`PYTHONPATH=/probe-src` ＋ `AISTORAGE_SCHEMA_DIR=/probe-schemas` 掛 repo 的 `src` 與
`schemas` 進容器，跑的是**工作目錄的最新實作**而不是映像裡的舊 wheel
（`aistorage.schema` 認得 `AISTORAGE_SCHEMA_DIR`，schema.py 第 100 行）。
映像要重建只因為一般部署流程要吃到新 wheel，那是既有的 `resident/build.sh` 流程，
與這個修正無關。

## 7. 留給後續（不在本次範圍）

- `agora syncer opencode status`（`syncer/__main__.py`）只有 pending／rejected／
  too_large 三個桶，沒有「取得不完整」的桶。目前 incomplete 只出現在每輪的
  `errors` 行（`incomplete: …`）與狀態檔的 `error_code`。要加的話動到
  `syncer/state.py` ＋ `syncer/__main__.py`，先留給協調。
- opencode 端：`opencode export` 應該在結束前等 stdout 寫完。修好之後上面第 2 節的
  對照組測試會失敗，那時改成記錄「已修正」並可以把取法簡化——但**完整性驗證要留著**，
  它擋的是「不管上游怎麼變，拿到不完整的東西絕不當原始紀錄」。

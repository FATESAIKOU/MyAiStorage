# #509／#510 front matter 的 flow 深度上限、後端時刻的排序鍵

- 單：#509、#510（MAS，層級 d，都掛在 #482 底下）。來源是 #482 設計 `docs/design-notes/482-decision-hardening.md` §1.3。
- 作者：專案架構師（作者模式，Claude Opus 5.5），2026-11-22。基準：main `50289be`（#482 PR-1～PR-4 都已合）。
- 實測：本機 CPython 3.12.3、PyYAML 6.0.3，只用假資料；隔離的 HOME／TMPDIR／AGORA_*，並在同一個行程裡 assert（`exec_guard.py`）。機器有負載，數字只當量級看。腳本與輸出在團隊目錄 `scratch-509/`（`probe.py`／`probe.out`、`fmstat.py`／`fmstat.json`、`probe510.py`／`probe510.out`、`proto.diff`／`proto-tests.out`）。

## 0. 結論

1. **#509：做。** 把 #482 PR-1 的 flow 深度上限（scanner 層，32）從 `decision.py` 搬到 `messages.py`，`parse_message` 也改用它。超過上限的 front matter 走現有的「YAML 讀不出」路徑：`strict=False` 時回 `None`，也就是不是訊息；`strict=True` 時丟 `MessageValidationError`。不加對外錯誤碼。深層的壞 front matter 從 1.1～2.0 s 降到 1～2 ms。
2. **#510：做。** `decision.py` 的 `_parse_time`／`_time_key` 搬到 `messages.py`，改成公開的 `parse_received_at`／`received_at_key`。`decision.order_key`、`log_query.select`（跨頻道）、`client`（「寄給我的」跨頻道合併）三處都用同一個鍵，排序是 `(received_at_key(received_at), channel, id)`。
3. **依賴方向不變**：`messages` 是最底層，`decision`、`log_query`、`client` 原本就（直接或間接）在它上面。新的 import 只有 `log_query → messages`，沒有反向，也沒有環。不新增模組。
4. **切成 4 個 PR**：每張單先做一個零行為變更的搬移 PR，再做一個行為變更 PR（§3）。

## 1. #509 front matter 的 flow 深度上限

### 1.1 實測

合法的上限（`fmstat.json`）：跑 `uv run pytest -k angareion` 共 1145 個測試，記錄 `parse_message` 讀過的每一份 front matter，共 2589 份。這些 front matter 含 interop 向量 `tests/vectors/angareion-interop/`（`ts-written/` 是 TS 寫者實際的輸出），以及 `serialize_message` 實際的產出。

- flow 深度最大 1（`mentions: [a, b]`、`attachments: []`），flow 深度 0 的有 62 份。
- 節點深度最大 4，節點數最大 33，長度最大 762。
- 寫入端 `serialize_message` 的寫法：`safe_dump` 產生的 block 格式，加上手寫的一行 `mentions`。它能產生的最大深度就是 1。

`probe.out`，每則約 64 KB，是 GitHub 單則留言的上限：

| 輸入 | 現況 | 上限 32 |
|---|---|---|
| 未閉合的 `[`、`{`、`[{` 交錯（約 6.5 萬層） | 1.8～2.0 s，回 None（RecursionError） | **1.7～1.8 ms**，回 None |
| 閉合的 `[`，1000 層（2 KB） | 1.06 s，回 None | 1.2 ms，回 None |
| 未知鍵下接 32 層 flow（剛好在上限） | 讀成訊息 | 讀成訊息（不誤殺） |
| 未知鍵下接 33 層 flow | 讀成訊息 | **None**（唯一的行為差異） |
| 扁平的 flow list，2.2 萬項；重複的 16 層群組 1980 組；扁平的 block list，1.6 萬項 | 0.8～1.5 s，讀成訊息 | 一樣（擋不到） |
| 一個 64 KB 的純量；block mapping 300 層 | 0.05～0.11 s | 一樣 |
| 合法：一個約 65 KB 的 inline 附件；inline 內文都是 `[[[…40 層…]]]`；990 個 mentions 加 400 個 ref 附件 | 0.04～0.20 s，讀成訊息 | 一樣，欄位完全相同 |

附件內文裡的 `[` 在引號或 block 純量裡，scanner 不會把它算成 flow。所以內文含括號不會被擋，上表已經實測過。

### 1.2 設計

- `messages.py` 新增三個公開名字，內容照搬 `decision.py` 現在的版本，只改名：
  - `FLOW_DEPTH_MAX = 32`；
  - `FlowTooDeep(yaml.YAMLError)`；
  - `FlowDepthLoader(yaml.SafeLoader)`：覆寫 `fetch_flow_collection_start`。
- `decision.py` 刪掉自己的那一份，改從 `messages` import。`parse_payload` 和 `build_payload` 的行為不變。
- 依 D-12，不留舊名 alias。測試只改 import 那一行。
- `parse_message` 的 `yaml.safe_load(raw_yaml)` 改成 `yaml.load(raw_yaml, Loader=FlowDepthLoader)`。
  - `FlowTooDeep` 是 `yaml.YAMLError`，所以落進現有的 `except Exception` → `Invalid YAML in front matter`。
  - `serialize_message` 寫完會讀回檢查，讀回也走同一個 loader。所以讀者會丟掉的，寫者也送不出去（同 #482 R3-4）。
- 門檻沿用 32：合法的最大是 1，有 32 倍餘裕；兩處用同一個常數。
- 對 openspec 的影響：主 spec `angareion` 規定「非訊息留言不列入」，深度超過上限的留言就是讀不出的 front matter，落在這條裡面，不必改 spec。`list`／`log` 的「列舉」仍然看得到這則留言，標示為非訊息。
- 被捨棄的做法：
  - 放在新的低層模組（例如 `_yaml.py`）：只有一個 loader，多一個模組不划算。front matter 的解析本來就是 `messages` 的職責。
  - `messages` 另寫一份 loader：兩份上限會分岔。
  - 長度或節點數門檻：見 #482 §1.2。合法的 front matter 可以有約 65 KB 的 inline 附件。

### 1.3 殘餘風險

- 「節點多、但不深」的 front matter（扁平的 flow 或 block list，放在未知鍵下）一則仍要 0.8～1.5 s。這種留言目前還會讀成訊息，因為未知鍵在讀取端是寬鬆的。
- 擋它需要節點數門檻，那就會開始限制寬鬆讀取。這和 #482 Q-1 是同一類問題，本單不處理。
- 成本發生在哪裡：
  - `read`／`wait`：每次抓到這則新留言時付一次。
  - `log`：同步進 log 快取時付一次，之後讀快取。

## 2. #510 後端時刻的排序鍵

### 2.1 設計

- `messages.py` 新增兩個公開函式，內容照搬 #482 PR-3（#513）的實作：
  - `parse_received_at(text: str) -> datetime | None`：`Z` 換成 `+00:00`，再用 `fromisoformat` 解析。沒有時區或解析失敗時回 `None`。
  - `received_at_key(value: str | None) -> tuple`：
    - `None` → `(0, EPOCH)`；
    - 帶時區、解析得出 → `(1, 時刻)`：帶時區的 datetime 直接比較，不轉 UTC。轉 UTC 時，`0001-01-01T00:00:00+01:00` 會 OverflowError，已實測；
    - 解析不出、或沒有時區 → `(2, EPOCH, 原字串)`。
- `decision.py` 刪掉 `_parse_time`／`_time_key`：
  - `order_key` 改成 `(received_at_key(q["asked_at"]), q["channel"], q["id"])`；
  - `plan_submit` 改成 `(received_at_key(asked_at), ref)`；
  - `snapshot_for` 改用 `parse_received_at`。
  - 三處行為都不變。`asked_at` 就是出題那則紀錄的 `received_at`。
- `log_query.py:127` 和 `client.py:199` 的排序鍵：
  - 改成 `(received_at_key(m.received_at), m.channel, m.id)`；
  - `client` 用 `getattr(m, "received_at", None)`：沒有這個屬性時是 `None`，排最前，和現在的 `""` 一樣。
- 既有語意 `(received_at, channel, id)` 保持（主 spec `angareion` 的 `--limit`）：
  - 只把 `received_at` 從「比字串」改成「比它代表的時刻」。
  - 同一時刻、不同寫法的鍵相同，交給 channel、id 決勝。
  - 單一頻道仍然依 `comment_id` 排，不受影響。
- 依賴：`log_query` 新增 `→ messages`；`client` 原本就 import `messages`；`decision → messages` 不變。不 import `backend.parse_backend_time`：`decision` 不能依賴 `backend`，而且兩者對沒有時區的時刻處理不同。
- 會改變的行為：
  - 格式混用（`Z`／`+00:00`／`+09:00`／小數秒）時，跨頻道的順序改依時刻。`probe510.out`：舊的字串鍵把 `09:00:00.5+09:00` 排在最後，新的鍵把它排在 `00:00:00.9+00:00` 前面。
  - 解析不出的、沒有時區的、空字串，改排在最後。
  - 後端（GitHub、假後端、log 快取）一律寫 `...Z`（`BACKEND_TIME_FORMAT`），所以現行資料的順序不變。
- 既有測試：原型（`proto.diff`，已還原）加上 #509 的改動，`-k angareion` 1145 passed、6 skipped（`proto-tests.out`），沒有測試的預期要改。
  - `test_angareion_library.py:427` 仍斷言 `keys == sorted(keys)`，用的是字串的三元組；資料是同一格式，所以順序和時刻順序一致。
  - `decision` 的測試只改 import。
- 殘餘的不一致（不在本單）：`log --since/--until` 的過濾用 `parse_backend_time`，沒有時區的時刻當成 UTC，解析不出時會丟例外；排序把它們排在最後。後端從來不寫這兩種時刻，所以只記錄。

## 3. PR 切分、介面契約、測試

| PR | 行為變更 | 介面契約 | 測試（行為描述） | 改壞驗證 |
|---|---|---|---|---|
| PR-1（#509）搬 loader | 無 | C-1：`messages.FlowDepthLoader` 讀 `"["*40000` 丟 `messages.FlowTooDeep`，是 `yaml.YAMLError`，不是 RecursionError；32 層可讀、33 層丟 | 既有 `test_angareion_decision.py` 的 PR-1 測試只改 import，全部照過 | 刪掉 `decision` 裡的 import → 測試失敗 |
| PR-2（#509）front matter 用上限 | flow 深度 > 32 的 front matter → 非訊息 | C-2：`parse_message`（未知鍵下接 33 層 flow）→ `None`；`strict=True` → `MessageValidationError`。C-3：32 層、64 KB 的 inline 附件、內文全是 `[` 的 inline、`ts-written` 向量都照常讀，欄位不變。C-4：`"["*40000` 的 front matter：`strict=False` 回 `None`；`strict=True` 丟 `MessageValidationError`，而且它的 `__cause__` 是 `FlowTooDeep`（不是 `RecursionError`）——**不靠牆上時間**，直接鎖住「scanner 層提早中止、不退回慢路徑」（審查 REVIEW-509d.md 建議；剛修過同類的 #496） | (a) C-2 兩個模式；(b) C-3 的邊界；(c) C-4；(d) `read`（假後端）略過深層的那一則，其他照常遞送 | 換回 `yaml.safe_load` → (a)(c) 失敗；門檻差一 → (a)/(b) 之一失敗 |
| PR-3（#510）搬時刻鍵 | 無 | C-5：`messages.received_at_key`：`None` < 解析得出的（依時刻）< 解析不出、無時區的（依原字串）；同一時刻不同寫法 → 鍵相等；`0001-01-01T00:00:00+01:00` 不丟例外。`parse_received_at` 沒有時區 → `None` | 直接測 C-5；既有 `decision`／`decision_cli` 測試全部照過 | `(1, moment)` 改成 `astimezone(utc)` → 邊界案丟 OverflowError |
| PR-4（#510）跨頻道排序 | `log`（多頻道）、`read`（寄給我的）依時刻排 | C-6：`log_query.select` 在多頻道下，依 `(received_at_key, channel, id)` 排；`--limit` 取這個順序的尾端。C-7：`Angareion.read` 在不給 channel 時，跨頻道合併的順序同 C-6 | 混用 `Z`、`+00:00`、`+09:00`、小數秒、`None`；同一時刻不同寫法 → 依 channel、id；`--limit 1` 取時刻最新的那一則；單一頻道仍依 `comment_id` | 換回字串鍵 → 混用格式的案失敗 |

- 依存：PR-2 在 PR-1 之後；PR-4 在 PR-3 之後。PR-1 與 PR-3 都動 `messages.py`、`decision.py`，但動的是不同位置，依序合即可。
- 每個 PR 都跑 `uv run pytest`，並對改過的檔跑私人資訊的 grep（樣式同派工單，此處不列以免自我命中）。
- 測試層：都是單元測試。PR-2 (d) 和 C-7 用現有的假後端，不連網路。驗收門檻由 PM 定。
- 總圖：
  - `overview.md` 的 angareion 節，由架構師在 PR-1、PR-3 內補上新的公開名字（同 #482 Q-3 的做法）；
  - `log_query → messages` 的依賴在 PR-4 內補。
  - 決策清單 D-31 隨本稿一起交。

## 4. 風險、沒驗到的、要決定的

- 風險：
  - PyYAML 內部掛點：同 #482 §5，由 C-1 釘住，`uv.lock` 鎖版本。
  - C-4 不用時間門檻（負載高的 CI 上不穩，見 #496）；改斷言丟的是 `FlowTooDeep`、不是 `RecursionError`。實測約 2 ms，現況約 2 s，只當說明，不進測試。
- 沒驗到：
  - App（TS）以外的寫者：只看了 interop 向量與 `serialize_message`。
  - 舊版讀者遇到深層 front matter 的行為：不受本單影響。
  - 時間只在這台有負載的機器上量過。
  - 原型只跑了 `-k angareion`，沒有跑全套。
- 要 PM 決定：
  - **Q-1**：要不要在主 spec `angareion` 補一個情境，寫明跨頻道的排序依時刻、不依字串？建議補，歸檔時同步；不補也不影響實作。
  - **Q-2**：是否同意切成 4 個 PR（兩個是搬移 PR）。替代做法是每張單一個 PR（搬移加使用），審查時看不出哪一段是純搬移。

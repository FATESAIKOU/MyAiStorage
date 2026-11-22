# #482 angareion decision 的後續三項：壞紀錄預篩、缺鍵與時間排序、兩條檢查鏈

- 單：#482（MAS，層級 d），來源是 #477（PR #481）的審查。
- 作者：專案架構師（作者模式，myaistorage-arch-w1-1，Claude Opus 5.5），2026-11-22。基準：main `39b7265`。
- 範圍：只設計。程式在 `src/angareion/decision.py`，`decision_cli.py` 只動一行排序。依賴方向不變（`decision → messages`），不加對外錯誤碼。
- 實測：本機（16 核，有別的工作在跑，同一案兩次量測差到 ±40%，數字都當量級看），CPython 3.12.3，PyYAML 6.0.3（有 libyaml）。只用假資料。腳本與輸出在團隊目錄 `scratch-482/`（`probe*.py`、`probe*.out`、`consistency-proto.out`、`verify-fixture.out`）；隔離環境用 `run-isolated.sh` 建（暫存的 HOME／TMPDIR／AGORA_*，同一行程 assert）。

## 0. 結論

1. **預篩：做，但只做一件事。** 在 `parse_payload` 用的 YAML loader 的 scanner 層限制 flow 巢狀深度（`[`、`{`）不超過 32，超過就丟 `yaml.YAMLError` 的子類，照現有流程判成 `bad_payload`。深層壞紀錄從約 1.4～2.4 秒降到 0.4～2 毫秒。不設長度門檻：合法的 snapshot 可以寫到約 64 KB。不設節點數門檻：省得不多，又會開始擋到寬鬆讀取。
2. **剩下的成本**：「節點很多但不深」的壞紀錄（扁平的 flow list、重複的淺層群組）一則仍要約 1.1～2.2 秒。最密的合法紀錄是 0.20～0.27 秒，兩者差 4 倍以上。這不在本單處理，見 §1.3、Q-1。
3. **缺 `decision` 鍵：改判 `bad_payload`（Q-2 已決，選 A）**。`decision` 是整數但不是 1、是字串、是 `null` 時，仍判 `unsupported_version`。這修訂 #468 設計 §4.3 的一句話；PMO 2026-11-22 依核准範圍內自行判斷的規則決定，事後知會本人。
4. **`asked_at` 排序改用解析後的時刻。** 時刻解析放在 `decision.py`（沿用 `snapshot_for` 現有的解析方式）。新增一個公開的排序鍵 `order_key`，`fold`、`plan_submit`、`decision_cli` 三處都用它。推論：既有測試的預期不會變。
5. **兩條檢查鏈：不共用，加一個參數化的一致性測試。** 原型實測：72 個組合加上重複 ref，兩條鏈只在「ref 不是 ULID」這一處分歧（fold 判 `bad_item`，`plan_submit` 判 `unknown_ref`），共 24 個組合。這個分歧在測試裡明列，兩邊各自釘住。前提：`answered` 夾具已提交的答案要和矩陣的 entry 不同（例如 `choice: B`），否則 `plan_submit` 走重跑略過，多出 1 組分歧（§3）。
6. **切成 4 個 PR**，每個 PR 只有一個行為變更；PR-4 只加測試。四個 PR 互不依賴。

## 1. 預篩

### 1.1 實測（`probe.out`、`probe.run2.out`、`probe2.out`、`probe3.out`、`probe4.out`）

| 輸入（約 64 KB，除非另註） | 現況 | flow 深度上限 32 |
|---|---|---|
| `[` 巢狀，深度 1000（2 KB） | 0.80～1.5 s（RecursionError；經 `parse_payload` 判 bad_payload） | — |
| `[` 巢狀，深度 32568；只有開括號；`[{` 交錯；合法 mapping 的未知鍵下接深層 | 1.4～2.4 s（RecursionError） | **0.4～0.8 ms**（判 bad_payload） |
| block mapping 一路縮排到深度 355 | 0.06～0.08 s，可以讀 | 不受影響 |
| 重複的淺層群組：1973 組（內層 15＋外層 1＝flow 深度 16），或 1002 組 flow 深度 32 | 1.15～2.2 s，可以讀 | 不受影響（深度上限擋不到） |
| 扁平的 flow list，32k 項 | 1.06～1.8 s | 不受影響 |
| **最密的合法紀錄**：snapshot 239 題，9 個選項；submit 688 項；用 JSON 寫的 submit 613 項 | 0.20～0.27 s；節點數 ≤ 6514；節點深度 ≤ 5；flow 深度：block 寫法 0，JSON 寫的 submit 3、snapshot 5 | 不受影響 |
| 最大的合法 snapshot（96 題，一般長度） | 0.24 s | 不受影響 |

從實測看得出兩件事：

- 時間大致跟著 YAML 節點數走，每個節點約 50 µs。
- 深層巢狀還會再多一份二次方的成本：scanner 在 flow 裡會一直向前看，看能不能構成 simple key。
- 所以在 composer 層限制深度沒用：實測上限 32 時，仍要 0.32～0.46 s 才擋下來（`probe2.out`），因為向前看在 compose 之前就做完了。要在 scanner 層擋。

### 1.2 設計

- `decision.py` 新增私有的 `_PayloadLoader(yaml.SafeLoader)`。它覆寫 `fetch_flow_collection_start`：當 `self.flow_level >= FLOW_DEPTH_MAX`（32）時，丟 `_TooDeep(yaml.YAMLError)`；否則呼叫父類別。
- `parse_payload` 和 `build_payload` 的回讀都改成 `yaml.load(text, Loader=_PayloadLoader)`。寫入端用同一個 loader，讀者會丟掉的，寫者就不會送出（R3-4）。`build_payload` 產生的是 block 格式，flow 深度最多 1（`[]`），實際上不會碰到上限。
- 錯誤碼沿用 `bad_payload`：`_TooDeep` 是 `yaml.YAMLError`，現有的 `except` 已經接住。`_apply_unreadable` 的 `op: snapshot` 規則照舊。
- 門檻：合法紀錄的 flow 深度，block 寫法是 0，用 JSON 寫時 submit 是 3、snapshot 是 5；32 留了 6 倍以上的餘裕。block 巢狀不設限：它受縮排長度限制，實測最便宜。
- **不用 `CSafeLoader`**：實測深度 32568 的輸入會讓行程 segfault（rc=-11，`probe2.out`），`except` 接不到。
- **不設長度門檻**：合法紀錄可以寫到 `MAX_MESSAGE_LENGTH`（65536）。
- **不設節點數門檻**：合法紀錄最多 6514 個節點。上限設成 16384（2.5 倍餘裕）時，扁平壞紀錄最多只省一半；而且未知鍵在 v1 是可以讀的，上限開始限制寬鬆讀取。

### 1.3 不在本單

- 「節點多的壞紀錄」約 1.1～2.2 s 一則。
  - `list` 每則只解析一次（`decision_cli.py:319`）。
  - ask／update／withdraw 會多解析 2 次（`related_count`），達到 snapshot 門檻時再多 2 次。
  - 真正能根治的是把每則的判讀結果存在 log 快取裡（以訊息 id 為鍵），範圍比本單大。見 Q-1。
- 觀察到兩處同類問題，不在本單處理；PMO 2026-11-22 決定各開一張 d、掛在 #482 底下，排在後面：
  - `messages.parse_message` 的 front matter 也是用純 Python 的 `yaml.safe_load` 讀外部文字，所有 datatype 都會受影響。
  - `log_query.py:127`、`client.py:199` 也是用 `received_at` 的字串排序。

## 2. 缺 `decision` 鍵與時間排序

### 2.1 缺鍵（Q-2，已決）

- 現況：`decision.py:323-326` 對 mapping 的判斷是「`decision` 不是整數 1 就判 `unsupported_version`」，所以完全沒有這個鍵時也判 `unsupported_version`。
  - 這是 #468 設計刻意定的。`openspec/changes/archive/2026-11-15-angareion-pending-decision/design.md:200` 原文：「（含**沒有這個鍵**、不是整數）都算這一類：這個讀者看不懂，不斷定是寫壞了」。
- 實測：任意的 mapping 也會判成 `unsupported_version`，例如縮排 200 層的 `a:`、別名展開（`probe.out`）。這等於叫人去升級讀者，但其實是寫壞了。
- 決定（Q-2 選 A；PMO 2026-11-22 依核准範圍內自行判斷的規則決定，事後知會本人）：
  - **只有 `"decision" not in p`** 時改判 `bad_payload`。
  - 鍵存在但值不對（`2`、`"1"`、`null`、`true`）時，仍判 `unsupported_version`。
  - 理由：未來的版本一定還有 `decision` 這個鍵（D-30 的版本錨點）；連鍵都沒有，就不可能是任何版本的寫法。
- 主 spec（`openspec/specs/angareion-pending-decision/spec.md:25`）寫的是「`bad_payload` 或 `unsupported_version`」，不用改。歸檔的設計不改，由本稿修訂那一句（#468 §4.3）。
- 既有測試：推論不受影響。
  - `test_angareion_decision.py:82` 和 `test_angareion_decision_cli_list.py:44,58` 用的都是 `decision: 2`。
  - `related_count` 的參數化測試（`:688-689`）也是 `decision: 2`。
- 被捨棄的替代（Q-2 的 B）：不改程式，只加一個測試把現況釘住。

### 2.2 時間排序

- 現況：有三處用字串排序。
  - `fold`：`decision.py:653` 的 `(q["asked_at"] or "", channel, id)`。
  - `plan_submit`：`:830`、`:837`，同一批次內依 `asked_at` 排序。
  - `decision_cli.py:322`：跨頻道合併後再排一次。
  - 混用 `Z` 與 `+00:00`、不同時區、有無小數秒時，字串順序都會錯。
- 介面（`decision.py`）：
  - `_parse_time(text) -> datetime | None`：從 `snapshot_for`（`:677-682`）抽出來，`Z` 換成 `+00:00` 後用 `fromisoformat` 解析。沒有時區、或解析失敗，回 `None`。`snapshot_for` 改用它，行為不變。
  - `_time_key(value: str | None) -> tuple`：
    - `None` → `(0, EPOCH)`；
    - 解析得出 → `(1, 轉成 UTC 的時刻)`；
    - 解析不出的字串 → `(2, EPOCH, 原字串)`。
    - 同一時刻、不同寫法的字串，排序鍵相同，交給 `channel`、`id` 決勝。
  - **公開** `order_key(q: Decision) -> tuple`，回傳 `(_time_key(q["asked_at"]), q["channel"], q["id"])`。`fold` 和 `decision_cli.py:322` 都用它；`plan_submit` 用 `(_time_key(asked_at), ref)`。
  - 不 import `backend.parse_backend_time`，否則會多一條 `decision → backend` 的依賴。
- 會改變的行為：
  - 只有格式混用、或含解析不出的字串時，順序會變。
  - 解析不出的字串原本按字串夾在中間，改成排在最後。這只可能來自 snapshot 項的 `asked_at`：讀取時只檢查它是字串。
  - 既有測試的時刻都是同一格式（單元測試 `+00:00`、假後端 `Z`）。推論它們的預期都不變，沒有跑過驗證。
- 總圖：`order_key` 是新的公開介面，PR-3 要同時把它補進 `docs/architecture/overview.md` 的 decision 段；這部分由架構師在 PR-3 內補（見 §4 PR-3）。

## 3. `_item_code` 與 `plan_submit`

- 建議**不共用**，理由：
  - **輸入不同**：fold 看的是 `_FoldState`，含墓碑與 snapshot 種子；`plan_submit` 看的是完整的 `FoldResult`。
  - **規則各有專屬的部分**：
    - 只在 fold：`binding_mismatch`；寬鬆讀取（可以有 `title`、未知鍵）。
    - 只在 plan：重跑時略過；嚴格檢查多餘的鍵。
  - 硬合成一份會變成帶 3～4 個開關的函式，fold（純匯總）就和 CLI 的預檢綁在一起，要改其中一邊時，另一邊也得跟著審。
- **原型實測**（`consistency-proto.out`）：
  - 窮舉 題目狀態{open, answered, withdrawn} × ref{正確, 不存在, 非 ULID} × 送出者{作答者, 別人} × base{正確, 過期} × choice{有效, 無效}，共 72 組。
  - 另測同一個 ref 出現兩次：兩邊都判 `duplicate_ref`。
  - 兩邊都判出同一個第一個問題碼，例外只有「ref 非 ULID」的 24 組：fold 是 `bad_item`，plan 是 `unknown_ref`。
  - **夾具前提**：`answered` 題已提交的答案，必須和矩陣送出的 entry 不同。如果相同（同作答者、同 base、`choice: A`、沒有 note），`plan_submit` 會走重跑略過（`skipped`，不丟 `SubmitRejected`），fold 卻判 `closed_ref`，分歧變成 25 組（審查者的 `verify_482.py`）。夾具用 `choice: B`，或用 `choice: A` 加 note，分歧都回到 24 組（`verify-fixture.out`）。重跑略過本身不在矩陣裡比對：它只在 plan 才有（見上面「只在 plan」）。
- 這個分歧是既有的、而且各自有依據，本單不收斂：
  - fold 的 `bad_item` 由 H1 釘住（`test_angareion_decision_cli_robust.py:38-45`，外來的非字串 ref）；
  - plan 的 `unknown_ref` 符合 #468 §6.2 的順序表（第 2 條「題目存在」）；對 CLI 使用者來說，打錯 ref 時也比較好懂。
- 一致性測試（PR-4）：
  - 用上面的笛卡兒積做參數化，每組斷言 `fold 的碼 == plan 的碼`。
  - `answered` 夾具照上面的前提，以 `choice: B` 作答；夾具旁寫一行註解說明為什麼不能用 `A`。
  - 例外表 `DIVERGES = {"nonulid": ("bad_item", "unknown_ref")}` 明列，兩個值都要斷言。
  - 測試只用公開介面（`fold`、`plan_submit`、`SubmitRejected`），不碰私有函式。
  - `_item_code` 的 docstring 改成指向這個測試。

## 4. PR 切分、測試、改壞驗證

| PR | 行為變更 | 測試（行為描述） | 改壞驗證 |
|---|---|---|---|
| PR-1 預篩 | flow 深度超過 32 → `bad_payload` | (a) 33 層 `[`，以及深度 L/2 的未閉合 `[` → 該則 `ignored` 是 `bad_payload`，其他紀錄照常匯總；(b) 合法 ask 的未知鍵下接 32 層 flow → 照常讀（邊界，不誤殺）；(c) `yaml.load("["*40000, Loader=_PayloadLoader)` 丟 `_TooDeep`，不是 RecursionError（釘住 PyYAML 的內部掛點）；(d) JSON（flow）寫的 submit 照常接受 | 拿掉覆寫 → (c) 失敗；門檻差一 → (a)/(b) 之一失敗 |
| PR-2 缺鍵 | 沒有 `decision` 鍵 → `bad_payload` | 沒有鍵 → `bad_payload`；`decision: 2`／`"1"`／`null` → `unsupported_version`；CLI 的 `list --json` 一案 | 改回原判斷 → 第一案失敗；把 `null` 也算缺鍵 → 第二案失敗 |
| PR-3 時間排序 | `asked_at` 以時刻排序 | `Z`、`+00:00`、`+09:00`、小數秒混用，`fold`／`list --all --json` 依時刻排；同一時刻不同寫法 → 依 channel、id；`None` 在最前、解析不出的在最後；`plan_submit` 的批次內順序；`snapshot_for` 既有測試不變。另外，同一個 PR 由架構師補 `docs/architecture/overview.md` decision 段的公開介面 `order_key` | 換回字串鍵 → 混用格式的案失敗 |
| PR-4 一致性測試 | 無（只加測試） | §3 的 72 組（`answered` 夾具以 `choice: B` 作答）＋重複 ref＋例外表 | 在 `_item_code` 對調 `not_answerer` 與 `stale_base` → 失敗；在 `plan_submit` 對調 → 失敗；讓 plan 對非 ULID 判 `bad_item` → 例外表失敗 |

每個 PR 都跑 `uv run pytest`，並對改過的檔跑 §5 的 grep。

## 5. 風險與沒驗到的

- **風險**：
  - PR-1 依賴 PyYAML 的內部方法名（`fetch_flow_collection_start`、`flow_level`）。由 (c) 釘住：升版改了名字，測試會失敗，不會悄悄失效。PyYAML 的版本由 `uv.lock` 鎖住。
  - PR-3 讓 `decision.py` 多了一份時刻解析，和 `backend.parse_backend_time` 的差異是：沒有時區時視為解析不出，不當成 UTC。這和 `snapshot_for` 一致，刻意不同。
- **沒驗到**：
  - 時間只在這台機器量過，而且機器有負載。
  - 實作與既有測試沒有跑：§2.2「預期不變」是推論。
  - CSafeLoader 的 segfault 只在本環境看到。
  - App（TS 寫者）實際的 YAML 形狀沒有看過；用 JSON 寫是假設的最壞情況。
  - 節點數門檻沒做原型。
- **要 PM 決定**：
  - **Q-1**：「節點多的壞紀錄」（約 1.1～2.2 s 一則）要不要另開單，在 log 快取存判讀結果？維持先不開，等真的遇到再說。
- **已決**：
  - **Q-2**：選 A，缺鍵改判 `bad_payload`（PMO 2026-11-22，見 §2.1）。
  - **Q-3**：`order_key` 進總圖，由架構師在 PR-3 內補（§4）。
  - §1.3 的兩件觀察：各開一張 d、掛 #482，排在後面，不在本單。

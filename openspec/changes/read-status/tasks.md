## 0. 前提（不是 PR）

- 本 change 是設計（proposal／design／specs delta／tasks）；經非作者審查、PMO 以介面為主向本人說明、**本人同意才實作**。
- 不動 `openspec/specs/` 的主 spec；delta 在 archive 時才套用。
- 不動：Angareion 協定（`angareion: 1`）、TS 端 MyAiEntry、sidecar、MyPMO 的程式、`openspec/specs/atelier/` 的已發布版本檔（append-only）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的訊息內文、真的 session id。
- 測試隔離：照 `tests/conftest.py` 的 `isolated_home`（`ANGAREION_STATE_DIR` 指暫存目錄）；不讀真 credential、不打真信、不碰真的狀態目錄（`~/.local`、`~/.cache`、`~/.config`）。
- **每一步 main 不更糟**：文件先、再 `status`、再 `wake`、最後收尾；每一步的文件與行為同步，不留「文件說有、程式沒有」的中間狀態。
- **文件落點**：逐檔逐行的表在 design D8；本 tasks 的 PR 內容以那張表為準。

## 1. 定案與審查關卡（不寫程式）

- [ ] 1.1 D-open：本人同意實作（唯讀查詢、不加回條）。
- [ ] 1.2 D-exit：`status` 未讀用 3（與 `wait` 同數字，選項 A；spec 已寫）或 5（選項 B；改動清單見 design D2.2）。
- [x] 1.3 D-fetch：**已決（PM 審查 H1）**：`status` 只用 `channels()`，不經 `resolve()`／`entity_for()`，不呼叫 `ensure_fresh()`；channel 不在本機複本答未知（exit 4，`channel-not-local`）；複本缺或壞答未知（exit 4，`config-unavailable`）。
- [ ] 1.4 D-name：`aicomm` 只戳指令名（spec 寫 `wake`；若改名，全文替換清單見 design D5.5）。`poke` 需同時改 comm-skill 的負面 scenario。
- [ ] 1.5 D-wake-exit：`wake` 的 exit（spec 寫 PM 建議的選項 B：0＝至少戳到一個，3＝沒有可戳，2＝請求失敗，1＝用法錯；選項 A 的改動清單見 design D5.3）。
- [ ] 1.6 D-wake-guard：`wake` 不查有無新訊息（靠文件規定使用時機）。
- [ ] 1.7 D-three-modes：`send` 三種模式的原文（design D6）；PR-1 開工前要定案。
- [ ] 1.8 D-ack：慣例回覆（方案 D）不寫進說明，只列為現行 `--no-wake` 基線。
- [ ] 1.9 D-cross：跨機身分答未知（reason `cursor-missing`）是否接受。
- [x] 1.10 **已由 MLP 確認（事實見 design D4.5）**：Mac 上所有 agent 與 sidecar 共用同一個 HOME 與 `~/.local/state/angareion/`；worker 每個容器一個狀態目錄、容器之間不共享；sidecar 沒有覆寫 `ANGAREION_STATE_DIR`。所以 C-min 只在同 HOME／同容器內可行，跨機或跨容器一律答未知（`cursor-missing`），見 1.9。
- [ ] 1.11 D-cost：`status` 的列舉成本（選項 (a) 全量列舉＋驗收門檻，或 (b) 由 ULID 推 `since`＋全量兜底）；design D4.3。(b) 要先驗 ULID 時間的寬裕量上界，否則用 (a)。
- [ ] 1.12 提案審查：proposal／design／specs delta／tasks 經非作者審查（pstack 或 rev），審查意見記入 design「判斷點」。
- [ ] 1.13 `openspec validate read-status` 通過，原樣輸出貼進報告。
- [ ] 1.14 PMO 以介面為主向本人說明（七個介面、三態、exit 3／4、`wake`）。
- [ ] 1.15 本人核准後才進入 PR-1。

## 2. PR-1 文件：`aicomm send` 三種模式（零行為變更）

**範圍**：`docs/aicomm.md`（新節「送出的三種模式」、`:50-51` 叫醒對象）、`skills/aicomm/SKILL.md`（「送達與叫醒」段 `:52-58`、軟規則 `:70-72`）、`tests/unit/test_aicomm_docs.py`（守護新字樣）。

- [ ] 2.1 寫新節（D6 原文，依 1.7 的裁定），逐條對照 `src/aicomm/cli.py:94`（`--to`）、`:104`（`--no-wake`）、`:105`（`--interrupt`）、`:201`（interrupt 條件）、`:222-226`（候選與 wake）、`:259-260`（所有被叫醒者同一旗標）。
- [ ] 2.2 `test_aicomm_docs.py` 加守護：兩份文件都有「送出的三種模式」、廣播不叫醒、`--interrupt` 只在 running 生效、`--no-wake` 與 `--interrupt` 互斥。
- [ ] 2.3 `skills/aicomm/SKILL.md` 同步（短句），不改軟規則的編號。

**完成判準**：`uv run pytest tests/unit/test_aicomm_docs.py` 綠（貼原樣輸出與 exit code）；`git status` 只有這三檔（加上測試檔）。

**改壞→轉紅**：把文件的「廣播誰都不叫醒」整句刪掉，守護測試 MUST 轉紅；還原後綠。

## 3. PR-2 `angareion status`（唯讀，同機）

**範圍**：`src/angareion/cli.py`（子指令、exit 4、`config-unavailable` 內部接住）、新的查詢模組（只讀；不放進 `CursorStore` 的寫入路徑）、`src/angareion/state.py`（只讀的游標檔解析，回傳 unparseable 而非空）、`tests/unit/test_angareion_status*.py`、`docs/angareion.md`（七個介面、表格加 status、結束碼段補 3／4；落點見 design D8）、`skills/angareion/SKILL.md`（同）、`tests/unit/test_angareion_docs.py`（守既有關鍵句，並加 status 的守護句）。

- [ ] 3.1 只讀的游標檔解析（design D4.1，H-1 修正後與寫入端同形）：不存在 → `cursor-missing`；存在但讀不到 → `cursor-unreadable`；整檔不是 JSON 物件、條目不是物件、缺 `position`／`at`／`seen`、舊格式（有 `id` 無 `position`）、型別真的不符（`position` 不是十進位字串或 null、`at` 不是字串或 null、`seen` 不是字串陣列）→ `cursor-unparseable`；`position` 與 `at` 都是 null → 該組從未越過（不是壞檔）；**不略過壞條目**；**不判斷版本**（游標檔沒有版本欄位）；**不呼叫 `_load`**（它會把壞檔當空）。**fixture 必須由真的 `CursorStore.save`／`save_many` 產生**（至少一個是首讀空 channel 的 null 條目、一個是有字串 `position` 的條目），不得手寫整數 `position` 的假檔——手寫 fixture 會全綠而真檔全炸（H-1 的成因）。
- [ ] 3.2 scope（`to` 是 X、或 `mentions` 含 X；廣播不在 scope 內）與三態判定（已讀／未讀／未知）；三組 key 用 `comment_is_new`；缺的組視為「從未越過」**只在整檔解析成功時**成立（D4.4）。
- [ ] 3.3 找 ULID：用既有的列舉動作，分頁、找到即停；找不到 → `message-not-found`。依 1.11 的選項實作；不論選哪個，**1000 則留言的假後端上，單次 `status` 的列表請求數 ≤ 11**（驗收門檻，spec）。**M-B 子項（必做，缺一不可）**：
  - (1) 列舉介面加**提前停止**（回呼或迭代器）。這是對既有列舉介面的向後相容擴充，不是寫入動作；`log` 與它的快取仍讀完整條列舉，行為與輸出不變（`log` 既有測試照跑、須維持綠）。
  - (2) 假後端加**分頁（100 則一頁）與列表請求計數**（目前假後端一次回全部，沒有計數無從驗收）。
  - (3) 測試：1000 則假後端，`status` 查最後一則與查不存在的 id，列表請求數 ≤ 11；找到即停的測試要證明它確實提早停（請求數 < 總頁數）。
  - 改壞見下表 (q)、(r)。
- [ ] 3.4 CLI：`--channel`、`--message`、`--to`、`--json`；文字模式照 spec 的固定行（已讀 `read: true`；未讀 `read: false`；未知兩行 `read: unknown`、`reason: <字面>`）；exit 0／3／4／1／2；身分或 token 缺 → exit 2（走 `main()` 的完整啟動）；`read_at` 為 null；**不印 `by`**。
- [ ] 3.5 **H1（PM 裁定）**：`status` 只用 `channels()` 查本機複本；不經 `resolve()`／`entity_for()`（它們 miss 時會 `refresh_for_lookup` 去 fetch）；不呼叫 `ensure_fresh()`。複本缺或壞（`ChannelConfigError`）→ 未知 exit 4、`config-unavailable`，在 status 內部接住，不讓 main 轉成 exit 2；channel 不在複本 → `channel-not-local`。**M-A 子項**：找到 X 後，`status` 自己呼叫 `backends()` 與 `backend(X 指向的名字)` 組 Entity（即 `entity_for` 的後兩步，不經 `resolve()`）；`backends()` 缺或壞、X 指向的 backend 不存在、型別非 github → `config-unavailable`（exit 4），不 exit 2。Scenario 見 spec「channel 指向的 backend 壞掉也是未知」。
- [ ] 3.6 文件與守護：docs／SKILL 寫明「已讀＝游標越過」與 inbox 邊界（D1.5）；`status` 不是回條；「未知一律不戳」的用法建議；列舉成本與留言數線性相關；同機限制；測試守既有關鍵句。
- [ ] 3.7 測試：spec「唯讀查詢 status」的**每個** Scenario 至少一條（H-1、M-A、M-B、L-B 新增的 Scenario 也要有：首讀空 channel 的 null 條目不是壞檔、亂序晚到的小位置在窗口內算新（seen 有／無兩種）、backend 壞掉是 `config-unavailable`、1000 則的請求數與找到即停、`log` 行為不變），包含：已讀、未讀、只用 `read --channel` 讀過、只用 `read --mentioned` 讀過、mention 在 `to` 為別人的訊息上算 scope、游標檔不存在、游標檔壞掉、游標檔裡舊格式條目、游標檔無法讀取、查不到訊息、廣播不作答、原因依固定順序取第一個、設定複本缺或壞（`config-unavailable`）、channel 不在本機複本（會計數 `fetch`／`refresh_for_lookup`／`ensure_fresh` 都是 0）、身分缺 exit 2、兩個身分的值不影響結果、不寫入不推進不建快取目錄、不讀 log 快取、不搶讀者、用法錯（缺 `--to`、`--message` 非 ULID、`--to me`、`--to` 含大寫或超長）、大 channel 列舉成本（1000 則 ≤ 11 次）、文字模式的未知兩行、跨機身分答未知。
- [ ] 3.8 **不寫入守衛（D4.7）**：(1) 假 `CursorStore` 的 `save`／`save_many` 在 status 路徑 raise；(2) **另外**在真的 `ChannelStore` 與暫存狀態目錄上，跑完全部 status 用例後比對：游標檔、`wait` 的 ETag 狀態檔、設定複本的 sha256、`log-<身分>/` 是否存在，前後全部相等。(2) 必須用真 store，因為假 store 抓不到直接 `Path.write_*`／`mkdir` 的寫入。

**完成判準**：`uv run pytest tests/unit -k status` 綠（原樣輸出與 exit code）；`uv run pytest tests/unit/test_angareion_docs.py` 綠；全套由 PM 跑。

**改壞→轉紅（每一項各一個測試，都必須轉紅；列出「沒有轉紅」的項目也要寫進報告）**：

| 改壞 | 預期紅的測試 |
|---|---|
| (a) 判準只比 `position`（去掉 `at − 60s` 窗口） | 「亂序晚到、位置較小的留言在窗口內仍算新」（L-B：spec 已補此 Scenario，seen 有／無兩條） |
| (b) 游標檔壞掉改回 `_load` 的 `{}` | 「游標檔壞掉是未知」 |
| (c) 漏掉 `channel:` 組 | 「只用 `read --channel` 讀過也算已讀」 |
| (d) 漏掉 `mention:` 組 | 「只用 `read --mentioned` 讀過也算已讀」 |
| (e) 去掉 scope（廣播可答） | 「廣播不作答」 |
| (f) 去掉 scope 的「mentions 含 X」分支（`to` 是別人、點名 X 的訊息判成範圍外） | 「mention 在 to 為別人的訊息上算 scope」 |
| (g) status 路徑呼叫 `save`／`save_many` | 「不寫入、不推進游標」 |
| (h) status 路徑直接用 `Path.write_*`／`mkdir` 寫狀態目錄（繞過 `CursorStore`） | 不寫入守衛（真 `ChannelStore` 的 sha256 與目錄比對） |
| (i) status 路徑經 `resolve()`（miss 會 fetch） | 「channel 不在本機複本是未知，且不刷新」（fake store 的 `fetch`／`refresh_for_lookup` 計數 > 0） |
| (j) status 路徑呼叫 `ensure_fresh()` | 同上（`ensure_fresh` 計數 > 0） |
| (k) 把 `ChannelConfigError` 交給 main 轉 exit 2；或 X 指向的 backend 壞掉（`backends()` 缺、不存在、非 github）改走 exit 2 | 「設定複本缺或壞是未知，且不讓 main 轉成 exit 2」與「channel 指向的 backend 壞掉也是未知」 |
| (l) 身分的值進入判定（例如用身分當 `--to` 的替代） | 「兩個身分的值不影響結果」 |
| (m) status 讀 `log-<身分>/` 快取 | 「status 不讀 log 快取」（快取與游標矛盾時） |
| (n) 舊格式條目被略過（改回 `_load` 的 `continue`） | 「游標檔裡有舊格式條目是未知，不是略過」 |
| (o) 未讀判成 exit 4、未知判成 exit 3（對調） | 「未讀 exit 3」「游標檔不存在 exit 4」 |
| (p) 未知原因優先序對調（例：`message-not-found` 先於 `cursor-missing`） | 「原因依固定順序取第一個」 |
| (q) 列舉提前停止拿掉（找到即停改成讀完整條才回） | 「大 channel 的列舉成本」：1000 則、查最後一則的請求數仍 ≤ 11 且「找到即停」的提早停測試（請求數 < 總頁數）——拿掉後轉紅 |
| (r) 列表請求數超過 11（例：假後端或列舉改每頁 50 則） | 「大 channel 的列舉成本」（≤ 11 的門檻） |
| (s) `cursor-unreadable` 被當 `cursor-unparseable`（讀取錯誤與解析錯誤合併） | 「游標檔無法讀取是未知」（stderr 說讀不到，`reason` 必須是 `cursor-unreadable`） |
| (t) `position` 改回要求整數（舊稿規則） | 真檔 fixture 的 status case（真的 `save_many` 寫出的 null 條目與字串 `position`）轉紅；手寫整數 fixture 不會轉紅，這正是本項要用真檔抓的原因（H-1） |
| (u) `config-unavailable` 與 `cursor-missing` 同時成立時順序對調（設定複本缺、X 的游標檔也不存在） | 「原因依固定順序取第一個」（第二個 Scenario：`reason` 必須是 `config-unavailable`） |
| (v) `log` 的列舉改用提前停止（擴充波及 `log`） | `log` 既有的完整列表測試（`log` 行為與輸出不變）轉紅 |

（沒有轉紅的改壞要寫進報告；本 change 的 (a)–(v) 都預期轉紅。）

**拒絕條件**（各驗一次，貼結果）：缺 `--to` exit 1；`--message` 不是 ULID exit 1；`--to me` exit 1；`--to` 含大寫 exit 1；`--channel` 格式錯 exit 1；身分與 token 都缺 exit 2（stdout 沒有內容）。

## 4. PR-3 `aicomm wake`（只戳不送）

**範圍**：`src/aicomm/cli.py`（子指令，重用 `_wake_one` 與 `sidecar_base`；wake 專用 stderr 格式，不用「已送達，未叫醒」；F20）、`tests/unit/test_aicomm_wake*.py`（沿用 `tests/unit/_aicomm_fakes.py` 的假 angareion 與假 sidecar）、`skills/aicomm/SKILL.md`（「三個指令」→四個；`:78`「不做的事」改寫）、`docs/aicomm.md`（`:7` 與叫醒段）、`src/aicomm/cli.py` 的用法錯字樣（`:83`、`:319`）、連動 `tests/unit/test_aicomm_send.py` 與 `test_aicomm_docs.py` 的字面斷言。

- [ ] 4.1 子指令與旗標：`--to`、`--mention`（可重複）、`--interrupt`；拒絕 `--body`、`--channel`、`--in-reply-to`、`--ticket`、`--ref`（exit 1，不發任何請求）。
- [ ] 4.2 `wake` 路徑不呼叫 Angareion（假 angareion 的呼叫次數為 0；假後端沒有寫入）。
- [ ] 4.3 候選＝`--to` 加 `--mention` 扣掉自己；`--to` 與 `--mention` 至少要有一個；沒有候選 exit 1，且不發任何請求。
- [ ] 4.4 叫醒條件沿用 `_wake_one`：只戳 `idle`／`running`；404 → `skipped not-local`；其他狀態 → `skipped state=<狀態>`；`--interrupt` 只在 `running` 為真（`poked interrupt`）。stderr 每個候選一行、格式固定；**不含「已送達」**。
- [ ] 4.5 exit（spec 寫選項 B；若 1.5 改選 A 則改寫本項）：至少一個 `poked` → 0；沒有戳成功且有請求失敗 → 2；沒有戳成功也沒有請求失敗 → 3；用法錯 → 1。各情形各一個測試（部分成功、全部 skipped、sidecar 連線失敗、用法錯）。
- [ ] 4.6 覆寫位址的 loopback 檢查在任何請求之前。
- [ ] 4.7 `aicomm --help` 與用法錯字樣更新（`:83`、`:319`）；`skills/aicomm/SKILL.md`「不做的事」改寫為「查狀態不做，只戳用 `aicomm wake`」（仍不做 `status`）。
- [ ] 4.8 文件守護與連動測試字面更新（`test_aicomm_send.py` 的 SUFFIX 與斷言、`test_aicomm_docs.py`）。

**完成判準**：`uv run pytest tests/unit -k "wake or aicomm"` 綠（原樣輸出與 exit code）。

**改壞→轉紅**：

| 改壞 | 預期紅的測試 |
|---|---|
| (a) `wake` 路徑呼叫 `angareion send` | 「不寫 Angareion」（假 angareion 呼叫次數） |
| (b) 去掉 loopback 檢查 | 「覆寫為非 loopback」 |
| (c) `--interrupt` 不限 `running` | 「打斷只對 running 生效」 |
| (d) 去掉自我排除 | 「只有自己的名字是用法錯」 |
| (e) 新增 `aicomm status` 子指令 | comm-skill 的「不認得的子指令」 |
| (f) stderr 改回「已送達，未叫醒」 | stderr 格式（不含「已送達」） |
| (g) exit 判定：有戳成功仍回 3（或全部略過回 0） | 「部分成功是 exit 0」「不戳不在本機 exit 3」 |

**拒絕條件**（各驗一次，貼結果）：`--body` exit 1；無候選 exit 1；覆寫非 loopback exit 1 且 0 個請求。

## 5. 收尾

- [ ] 5.1 `openspec validate read-status` 通過（原樣輸出）。
- [ ] 5.2 `angareion --help` 與 `aicomm --help` 的子指令清單與 spec 一致（七個介面、四個指令）。
- [ ] 5.3 本人同意後 archive，delta 套用到主 spec。archive 後 `grep -n "六個" openspec/specs/angareion/spec.md openspec/specs/comm-skill/spec.md` 必須沒有「介面」「指令」「Scenario」意義的殘留（歷史說明除外），`openspec/specs/angareion/spec.md:460` 的「已讀狀態」用詞已在 delta 統一。

## 6. 不在本單

- 跨機已讀（方案 A 回條、公開游標）：另案，獨立 issue；需要時才做，並且回條 MUST NOT 寫在訊息所在的 issue 上（ETag 成本，design D7）。
- 方案 B（reaction）：不做。
- 方案 D 的機制化：不做，只保留現行 `--no-wake` 基線。
- `aicomm peek`（MyLinuxPool#366）與叫醒延遲量測：不在本單。

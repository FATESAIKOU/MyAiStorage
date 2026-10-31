## Context

工單 MyAiStorage#311：發信方要知道「我送的那則，對方讀了沒」，而且不能靠重送、重戳。本人 2026-10-31 的方向已在 proposal 寫明。本文件是設計，只寫不做；每個判斷點標明是「已決（本人 10-31，PMO 轉述）」、「已決（PM 審查裁定）」、「作者提議」或「待拍板」。

前身草稿 `plans/DRAFT-read-receipt.md`（兩輪審查見 `tickets/REPORT-draft-readreceipt-rev.md`）的技術判斷大多沿用，本文件補上審查指出的缺口：三態、`channel:` 游標、inbox 路徑邊界、廣播的首讀過濾、exit 3 的衝突、`status` 與設定檔刷新的副作用、`comm-skill` 原本禁止單獨戳人。第一次審查（`tickets/REPORT-311-rev.md`）的修正輪結果記在 `REPORT-311-impl.md` 的「修正輪」一節。

**標記**：【事實】附出處（行號以本 change 開工當下的 main 為準，已逐一打開確認）；【推論】是作者從事實推的；【未驗】沒有查證或沒有實測。

### 事實

| # | 事實 | 出處 |
|---|---|---|
| F1 | 游標是純本機檔 `cursor-<身分 slug>.json`，三組 key：`channel:<ch>`、`to:<AI>:<ch>`、`mention:<AI>:<ch>`，三組互不影響 | `src/angareion/state.py:24-26`（檔名）、`:177-198`（三組 key）；`docs/angareion.md:117-120`（游標檔路徑在 `:119`） |
| F2 | `read --channel` 只推進 `channel:<ch>`；預設 `read`（寄給我的）只推進 `to:`；`read --mentioned` 只推進 `mention:` | `src/angareion/client.py:143-153`、`:155-175` |
| F3 | 游標的 `at` 是那則訊息的後端時刻，不是讀取時刻；所以游標本身給不出「何時讀」 | `src/angareion/backend.py:111-122` |
| F4 | 新舊判斷 `comment_is_new`：id 大於 position，或在 `at − 60s` 窗口內且不在 `seen` 裡就是新 | `src/angareion/backend.py:140-159` |
| F5 | 「寄給我的」＝`to` 是我、或 `to` 為空（廣播）、或 `mentions` 含我；「@ 我的」不含廣播 | `src/angareion/backend.py:304-318` |
| F6 | 新身分第一次「寄給我的」讀時，舊廣播被 `new_reader_counts` 過濾掉沒印出，但游標照樣越過 | `src/angareion/backend.py:323-335`；`src/angareion/github.py:458`；`src/angareion/client.py:157-164` |
| F7 | `read` 先印後 commit（子行程內）；`aicomm inbox` 以 `capture_output=True` 包住 `angareion read`，stdout 由 aicomm 在 `_relay` 才印給 agent | `src/angareion/cli.py:416-426`；`src/aicomm/cli.py:128-138`、`:141-146` |
| F8 | 讀取不是純查詢：`read`、`wait`、`send`、`log` 前都會先呼叫 `store.ensure_fresh()`，在超過間隔時從設定 repo 拉一次 | `src/angareion/cli.py:415`、`:536`；`src/angareion/channel_config.py:185` |
| F9 | 游標的 `_load` 在檔不存在、壞 JSON、型別不對時都回空 `{}`，與「從未讀」無法分辨；遇到舊格式條目（有 `id` 無 `position`）時會略過 | `src/angareion/state.py:219-234`（`:232` 起的舊格式分支） |
| F10 | 狀態目錄：`ANGAREION_STATE_DIR` 有設定就用它，否則 `~/.local/state/angareion` | `src/angareion/config.py:62-67` |
| F11 | 列舉動作已存在（log 變更已落地）：backend 的 `enumerate` 回傳留言與到達位置、後端時刻，並標出是否為 angareion 訊息 | `src/angareion/backend.py:543-546`；`openspec/specs/angareion/spec.md:205` |
| F12 | 結束碼：0 成功、1 用法或輸入錯、2 後端／網路／設定錯、3 只給 `wait` 逾時（`EXIT_TIMEOUT`）；`aicomm inbox --wait` 逾時也是 exit 3 | `src/angareion/cli.py:61-64`；`openspec/specs/angareion/spec.md:170-171`；`skills/aicomm/SKILL.md:63` |
| F13 | `aicomm` 叫醒是 `_wake_one`：GET `/agents/<name>`，只有 `idle`／`running` 才 POST `/poke`；`interrupt` 只在 `running` 為真；不在本機（404）不戳 | `src/aicomm/cli.py:42`、`:45`、`:169-210`（`:199` 狀態判斷、`:201` interrupt 條件） |
| F14 | `aicomm send` 的候選是 `--to` 加 `--mention` 扣掉自己；沒有候選（廣播）時不叫醒任何人；`--no-wake` 與 `--interrupt` 互斥 | `src/aicomm/cli.py:94`、`:104-105`、`:222-226`、`:259-260` |
| F15 | comm-skill 的主 spec 明文「skill MUST NOT 提供 … 單獨戳 agent … 等指令」，對外只有 send、inbox、log 三個 | `openspec/specs/comm-skill/spec.md:160-161` |
| F16 | comm-skill 的 spec 也有「`aicomm poke` 或 `aicomm status` → exit 1」的負面 scenario，會被新指令名影響 | `openspec/specs/comm-skill/spec.md:163-165`（scenario 段） |
| F17 | `aicomm` 的 SKILL 明寫「不做：查狀態、單獨戳人」 | `skills/aicomm/SKILL.md:78` |
| F18 | 文件有測試守：`test_aicomm_docs.py`、`test_angareion_docs.py`；改字要同步 | `tests/unit/test_aicomm_docs.py`、`tests/unit/test_angareion_docs.py` |
| F19 | 設定複本的 `resolve(name)`：本機找不到就 `refresh_for_lookup()`（會 fetch 並寫入本機 clone，30 秒節流），再找一次；`entity_for()` 呼叫的就是它。`channels()` 只讀本機複本 | `src/angareion/channel_config.py:319-334` |
| F20 | `aicomm wake` 會複用的 `_wake_one` 回傳的 stderr 文字開頭是「已送達，未叫醒 {name}：…」，對 `wake` 沒有「已送達」這回事 | `src/aicomm/cli.py:178`、`:184` |
| F21 | 列表分頁：`enumerate_conditional` 每頁 `LISTING_PAGE_SIZE=100`，沒有 `since` 時整條 issue 全讀，不讀 log 快取、不存 ETag | `src/angareion/github.py`（`enumerate_conditional`；`LISTING_PAGE_SIZE`） |

### 推論（不是事實）

- I1：同一身分的「寄給我的」游標與「@ 我的」游標之間沒有關聯，所以 `status` 要對每組 key 分別比，任一組越過就算已讀（D2）。
- I2：GitHub 的 ETag 是整條 issue 列表 page 1 的表示，所以任何留言改動都會讓 `wait` 的 ETag 失效；`status` 不寫任何 ETag，不受影響。
- I3：`status` 要找 ULID 所在的留言，需要掃整條 issue 的留言列表（多頁）。成本與留言數線性相關（F21）；單頁、約百則以內可接受，超過時每次查詢的 API 頁數＝⌈留言數／100⌉（**未量**，見 D4.3 的 D-cost）。
- I4：同機的 `status` 會讀到其他身分的游標檔。這只在同一台機器、同一個狀態目錄下有效；跨機的 worker 讀不到（F10 的狀態目錄是本機的）。
- I5：ULID 的時間戳來自寄件者的機器（與 `docs/angareion.md:117` 「新舊只看後端」同理），不能當成後端時刻。

### 未驗

- GitHub 的 secondary rate limit 數字（未查證）。
- `status` 大量呼叫時的額度消耗（未量）；大 channel 的實際頁數（只有假後端的 1000 則測試，見 tasks）。
- sidecar 對 `poke` 的排隊行為（不在本 repo；`docs/aicomm.md` 與 SKILL 只描述結果）。
- 本人 10-31 裁定的原文（只有 PMO 轉述）。

## Goals / Non-Goals

**Goals**

- 發信方能查單則訊息對某個收件人是已讀、未讀或未知；查詢本身不寫入、不改游標、不影響「同一身分只留一個讀者」。
- 未知一律不被當成未讀，所以腳本不會因為查不到就中斷對方。
- `aicomm` 能只戳、不送訊息，且只戳本機 sidecar 上 `idle`／`running` 的 agent。
- `aicomm send` 三種模式在文件寫清楚，並與程式行為一一對應。

**Non-Goals**

- 跨機的已讀（A 回條、公開游標）；B（reaction）；D 的機制化。
- 「全員是否已讀」的列舉：廣播不列人數（後端不存成員，spec:205）。
- 讀取時刻（`read_at`）：游標給不出來（F3），只有寫入式的回條才能記。

## D1 「已讀」的定義

**D1.1 方向（已決，本人 10-31）**：已讀＝收件人的游標越過那則訊息。

**D1.2 精確判準（作者提議）**：對收件人 X 與訊息 m，m 為**已讀**，當且僅當下列三組游標中**任一組**越過 m（`comment_is_new` 為假），且 m 落在該組的 scope 內：

| 游標 key | scope（m 必須符合） | 來源 |
|---|---|---|
| `to:X:<ch>` | m 對 X 是「寄給我的」：`to` 是 X、或 `to` 為空（廣播）、或 `mentions` 含 X | F5 |
| `mention:X:<ch>` | m 的 `to` 是 X、或 `mentions` 含 X（**不含廣播**） | F5 |
| `channel:<ch>` | m 在該 channel 內（不分收件人） | F2 |

表註（L-D）：三列都**先經 D1.4 的 scope 過濾**（廣播、與 X 無關的 m 不作答）；`channel:<ch>` 那一列的「不分收件人」只指 scope 通過之後，不是說它能越過廣播。

理由（作者提議）：誤報未讀會中斷人（`--interrupt` 的成本高），誤報已讀只會少戳一次；風險方向不對稱，應偏向已讀。

**D1.3 `channel:` 組的代價（作者提議）**：`read --channel X` 讀過、訊息印出了，但 `to:` 游標不動。只看 `to:` 會誤報未讀（審查 H2）。接受的漏報方向：`channel:` 越過時，X 可能並未看到 `to` 是 X 的那則。這是本設計明文接受的代價。

**D1.4 廣播不得答已讀（作者提議，對應審查 M3）**：`status` 只對 `to` 是 X 或 `mentions` 含 X 的訊息作答。廣播（`to` 為空且不點名 X）一律答「未知」（reason `out-of-scope`），原因是 F6：新身分的首讀過濾會讓舊廣播的游標越過，但 X 從未看過。後端不存成員（spec:205），所以也不列舉「全員」。

**D1.5 inbox 路徑的邊界（作者提議，對應審查 M2）**：`read` 的「印出後才 commit」只保證直接呼叫 `angareion read` 的情況（F7）。經過 `aicomm inbox` 時，游標在子行程內已推進，`_relay` 前若被中斷（例如 `--interrupt` 或額度中斷），agent 沒看到，但 `status` 會答已讀。所以「已讀」在 inbox 路徑只代表**游標越過**，不代表 agent 已處理。發信方的腳本不應把「已讀」當成「對方已處理」。這是 C 方案的固有上限，不改結論（C 本來就偏向已讀）。

## D2 三態、未知原因與結束碼

**D2.1 三態（已決，本人 10-31；未知的細分與順序由 PM 審查裁定）**：

- **已讀**：D1.2 任一組越過，且 m 在 X 的 scope 內。
- **未讀**：本機能確認 X 的游標檔存在且可解析、訊息的 id 與 created_at 在後端查得到、訊息對 X 在 scope 內，且三組相關游標都沒有越過（缺的組視為沒有越過，前提是整檔解析成功，見 D4.4）。
- **未知**：以下任一成立即為未知，一律不戳。**多個同時成立時，`reason` 依下列固定順序取第一個**（spec 同步寫明；PM 審查 L2 裁定）：

| 順序 | reason | 條件 |
|---|---|---|
| 1 | `config-unavailable` | 本機設定複本缺失或無法解析（`ChannelConfigError`），或 X 指向的 backend 在 `backends()` 缺、型別不是 github（M-A，見 D4.2）；status 內部接住，不讓 main 轉成 exit 2（H1） |
| 2 | `channel-not-local` | `--channel` 不在本機設定複本裡（D4.2） |
| 3 | `cursor-missing` | X 的游標檔整個不存在（可能是別機的身分，或 X 從未讀過；F10） |
| 4 | `cursor-unreadable` | 游標檔存在但讀不到（權限等）。**作者提議的新字面**：與解析異常分開，讓腳本能分辨「修權限」與「檔壞了」 |
| 5 | `cursor-unparseable` | 游標檔存在且讀得到，但不符合 D4.1 的格式（含舊格式條目、型別真的不符；PM 審查 L1 裁定：不略過、不當空；H-1 修正：`position` 是字串，不是整數） |
| 6 | `message-not-found` | 後端列舉查不到 `--message`（可能剛送、還沒落地） |
| 7 | `out-of-scope` | m 不在 X 的 scope 內（廣播、或與 X 無關，D1.4） |

理由：偏向不打擾。代價：同機、真的未讀、但查不到的訊息，腳本也不戳（推論，可接受）。

**D2.2 exit 約定（待拍板 D-exit）**：

| exit | 意義 |
|---|---|
| 0 | 已讀 |
| 3 | 明確未讀 |
| 4 | 未知（新增，與 3 分開） |
| 1 | 用法錯（缺 `--to`、ULID 格式不對、`--to` 不是合法 AI 名、`--channel` 格式不對、`--to me`） |
| 2 | 身分或 token 缺、後端或設定錯（沿用 `EXIT_BACKEND`） |

**衝突（事實，需要裁定）**：exit 3 在 `angareion` 的既有意義是「`wait` 逾時沒有新訊息」（F12，spec:7、spec:170-171），`aicomm inbox --wait` 也用 3。`status` 的 3 與它不同義，但同樣是「還沒有」。兩個選項：

- **選項 A（PM 建議；spec 已寫）**：沿用 3 表示「未讀」，文件明寫「3 只在 `status` 與 `wait` 上表示『還沒有』，各自的 stdout 說明不同」。好處：腳本的「否」都是 3；代價：同一個數字有兩個意思。
- **選項 B**：`status` 的未讀改用 5（0 已讀、5 未讀、4 未知）。好處：不與 `wait` 的 3 重疊；代價：多一個數字要記。

**若選 B，spec 要改的地方**（`specs/angareion/spec.md`）：「輸出與結束碼」段中「status 的 3 與 wait 的 3 是同一個數字」整句；「唯讀查詢 status」的「結束碼」一行（`3 未讀` → `5 未讀`）；Scenario「未讀」的 `exit 3`；Scenario「游標檔不存在是未知，不是未讀」的「不是 exit 3」。tasks 的 1.2、與 §3 的 exit 清單。其他設計不受影響。

**D2.3 `--json`（作者提議；`by` 欄位已刪，PM 審查 L3 裁定）**：`{"message": "<ULID>", "channel": "<ch>", "to": "<AI>", "read": true|false|null, "read_at": null, "reason": "<字面原因>|null"}`。`read_at` 固定為 null（F3，C 方案給不出讀取時刻）；未知時 `read` 為 null，`reason` 寫字面原因（清單同 D2.1）；已讀或未讀時 `reason` 為 null。`by` 不定義、不輸出。

**D2.4 文字輸出（作者提議；格式寫進 spec，PM 審查 L4 裁定）**：stdout 只放固定行——已讀 `read: true` 一行；未讀 `read: false` 一行；未知兩行 `read: unknown`、`reason: <字面原因>`。`read_at` 與 `by` 都不印。

## D3 `angareion status` 的介面

```
angareion status --channel <ch> --message <ULID> --to <AI 名> [--json]
```

- `--channel`、`--message`、`--to` 都必填；缺了 exit 1。`--to` 只接受合法 AI 名（既有文法；大寫、前後空白、超出長度都是 exit 1）；`me` 不解析（發信方要明確寫出收件人；`me` 會讓「是誰在查」與「查誰」混在一起）。
- `--to` 是**被查的收件人**，不是呼叫者。呼叫者的身分（`ANGAREION_IDENTITY`）的**值**不影響結果；但 `status` 走 `main()` 的完整啟動，身分與 token 缺時 exit 2（PM 審查 M1 裁定：不是未知）。
- 不接受 `--channel` 省略（沒有跨 channel 的「全部」查詢）。
- 輸入錯（格式、缺參數）exit 1，stdout 沒有內容。
- 「只給已登記的 channel」：`--channel` 不在本機複本裡時答未知（exit 4，`channel-not-local`），不是 exit 1。

**與 `log` 的差別**：`log` 是多筆、可篩選；`status` 是單筆、固定答三態。兩者都 MUST NOT 推進游標（design D4）。

## D4 查詢實作（唯讀）

**D4.1 讀取游標（作者提議；解析規則 PM 審查 L1 裁定）**：用一個**只讀**的解析函式讀 `cursor-<slug(X)>.json`（不呼叫 `CursorStore.cursor()`，它的 `_load` 會把壞檔當空，F9）。格式規則（嚴格；H-1 修正：與寫入端同形）。寫入端的事實：`position` 是十進位字串（`backend.py:186` `Cursor(position=str(position), …)`；`backend.py:114` docstring「a decimal string」；`comment_is_new` 用 `int(cursor.position)`，`backend.py:154`），首讀空 channel 時 `save_many` 會寫出 `position: null`、`at: null`（`state.py:265-271` 原樣 dump）。

- 整檔 MUST 是 JSON 物件；每個值 MUST 是物件，含 `position`、`at`、`seen` 三欄：`position` 是十進位數字字串或 null；`at` 是字串或 null；`seen` 是字串陣列。
- `position` 與 `at` **都是 null** ＝ 該組從未越過（等同不存在；D4.4）。這是正常狀態，不是壞檔。
- 任何一項不符（不是 JSON、整體不是物件、條目不是物件、舊格式〔有 `id` 無 `position`〕、型別真的不符，例如 `position` 是布林或陣列）→ `cursor-unparseable`。**不略過壞條目**（F9 的舊格式略過行為在 status 路徑不沿用）。
- **沒有版本欄位**，所以「版本不認得」不是判準（已刪）。
- 整檔解析成功後，**該 key 不存在，或存在但 `position`、`at` 都是 null** 才視為「從未越過」；這是 D4.4 的前提。
- 「整數 `position`」是本 design 舊稿的錯誤（H-1）：照它實作，真的游標檔每一條 `position` 都是字串，所有身分都會答 `cursor-unparseable`。tasks 3.1／3.7 的 fixture 因此必須由真的 `CursorStore.save`／`save_many` 產生。

MUST NOT 呼叫 `save`／`save_many`，MUST NOT 寫 `wait` 的 ETag 狀態檔。

**D4.2 channel 查找（已決，PM 審查 H1 裁定）**：`status` 只能用 `channels()` 讀本機複本，**不得經 `resolve()`／`entity_for()`**（F19：它們在 miss 時會 `refresh_for_lookup()` 去 fetch 並寫本機 clone）。也不呼叫 `ensure_fresh()`（F8）。判定：

- 複本缺失或無法解析（`ChannelConfigError`）→ 未知，`config-unavailable`，exit 4；status 在自己內部接住，不讓 main 把它轉成 exit 2。
- channel 不在複本 → 未知，`channel-not-local`，exit 4。
- 驗證方式：fake store 的 `fetch`、`refresh_for_lookup`、`ensure_fresh` 任一被呼叫即紅（見 tasks 改壞表 (i)、(j)）；只換掉 `ensure_fresh` 抓不到 miss 路徑的洩漏，所以不夠。
- **Entity 的建立（M-A）**：`channels()` 只給 `ChannelRef(backend 名, issue)`（`channel_config.py:260-281`），不給 repo。要列舉，`status` 得自己做 `entity_for` 的後兩步：`backends()`（`:283-313`，會 raise `ChannelConfigError`：unknown backend、型別不是 github）與 `backend(X 指向的名字)`，再組 `Entity`（`channel_config.py:334-340` 的拆解）。這兩步的失敗一律歸 `config-unavailable`（exit 4），不歸 `channel-not-local`，也不讓 main 轉 exit 2。spec 已有 Scenario（「channel 指向的 backend 壞掉也是未知」）；tasks 改壞 (k) 涵蓋。

代價：剛建立的 channel 在本機複本還沒更新時，`status` 答未知（`channel-not-local`），而不是錯誤。偏向不打擾，可接受。

**D4.3 找 ULID 所在的留言與列舉成本（作者提議；成本選項待拍板 D-cost）**：用既有的「列舉」動作（F11）找 `message` 的留言 id 與 `created_at`。找不到 → 未知（`message-not-found`）。這一步只做 GET，不寫任何東西（I3）。

**現況（事實）**：列舉是全量分頁，每頁 `LISTING_PAGE_SIZE=100`，沒有 `since`，不讀 log 快取，不存 ETag（F21）。每次 `status` 的 API 頁數＝⌈留言數／100⌉，與 channel 年齡線性相關。核心用法是「送出後隔一段時間輪詢→未讀才戳」，所以長壽 channel 上會重複付這個成本。spec 禁止讀 log 快取（`status` 不讀快取），因此不能靠快取。

- **選項 (a)（spec 預設）**：接受全量列舉，找到即停止翻頁；文件寫明 `status` 的成本與留言數線性相關，並給使用建議（送出後的輪詢間隔）。驗收門檻寫在 spec：1000 則留言的假後端上，列表請求數 ≤ 11。
  - **M-B 的實作前提**：現況 `enumerate`（`backend.py:543-544`）＝`enumerate_conditional(...)[0]`，回傳完整 list；GitHub 版（`github.py:482-530`）`while True` 讀完整條 issue 才 return；假後端（`backend.py:546-`）一次回全部、沒有分頁，所以「列表請求數」在它上面無從計數。要「找到即停」與「≤ 11」可驗，必須先做兩件事：(1) 列舉介面加**提前停止**（回呼或迭代器），(2) 假後端加**分頁（每頁 100 則）與請求計數**。這是對既有列舉介面的**向後相容擴充**，不是新的寫入動作；`log` 與它的快取仍讀完整條列舉，行為與輸出不變（改壞：提前停止拿掉→大 channel 測試轉紅）。proposal 的「不新增寫入動作」指的是寫入，不包括這個擴充。
- **選項 (b)（PM 建議，若可行）**：由訊息 ULID 的時間戳推 `since`，縮小列舉範圍。**要先驗證的事**：(1) ULID 時間來自寄件者機器（I5），沒有可靠的時鐘上界，所以 `since` 必須留寬裕量，而寬裕量的上界要實測或規定；(2) `since` 的語意是**最後編輯時刻**（spec:205），編輯過的舊留言會重新出現，這是多讀不是少讀，不影響正確性；(3) 若用 `since` 沒找到，**必須退回全量列舉**，這樣正確性不靠寬裕量。若 (1) 無法定出上界，則 (b) 只能當「先窄後寬」的優化，仍要 (a) 的全量兜底，成本收益要實測才算數。

**D4.4 判定（順序與游標規則）**：依 D2.1 的順序檢查；第 1–7 項都不成立時，對 X 的 scope（D1.4）與三組 key（`to:X:<ch>`、`mention:X:<ch>`、`channel:<ch>`）各自用 `comment_is_new`（F4）判斷：任一組不是新 → 已讀；全部是新 → 未讀。缺哪一組的 key 視為「從未越過」，**但只在整檔解析成功（D4.1）時成立**；整檔解析成功而該 key 不存在，或存在但 `position`、`at` 都是 null，才是「從未越過」（`comment_is_new` 對這兩種情形都回答「新」，與寫入端的首讀空 channel 條目一致）；key 存在但型別真的壞掉，是 `cursor-unparseable`，不是從未越過。

**D4.5 同機限制與前提（事實，PM 已向 MLP 確認；取代舊稿的「未驗」）**：

**事實（PM 已確認，MLP 回覆）**：
- Mac 上所有 agent 與 sidecar **共用同一個 HOME**，游標檔在同一個 `~/.local/state/angareion/`（每身分一個 `cursor-<AI名>.json`）。
- worker 容器的 HOME 是 `/home/worker`，狀態目錄是 `/home/worker/.local/state/angareion/`；**每個容器一個目錄，容器之間不共享**。
- sidecar **沒有覆寫 `ANGAREION_STATE_DIR`**（子行程環境白名單 KEEP_ENV 沒有它），所以 Mac 上 status 讀得到同機所有身分的游標檔。

**結論**：C-min 只在**同一個 HOME／同一個容器內**可行。**Mac↔worker、或任何跨容器的查詢，一律答未知（reason `cursor-missing`）**，不是 bug，是 C 方案的範圍（F10：狀態目錄在本機）。

**風險（直接限制 M5 之後的用處）**：M5 搬遷之後，PMO 在 Mac、隊員在 worker 容器，PMO 用 `status` 查隊員的已讀會**一律答未知**——也就是說，搬遷之後這個指令對最常見的跨角色查詢幾乎沒用，只對「同一台機器、同一個 HOME」的查詢有用。這點必須在本 change 的說明與判斷點表明寫，不能等實作後才發現。若要涵蓋跨機或跨容器，需要另一條路：(i) 跨機身分的游標公開（方案 A，回條或公開游標，另案）；(ii) `status` 增加 `--state-dir`（只能查到能讀到的目錄，跨容器仍不行，要 volume 共享）；(iii) 把游標寫進後端（違反 spec:224 的「不寫入」，不在本 change）。「跨機（方案 A）何時需要」因此列為待本人決定（D-cross）的理由：是 M5 搬遷後的主要使用場景，還是保留同機查詢就夠。

**退路（舊稿保留，已不需要核對）**：舊稿的「sidecar 是否共用 `ANGAREION_STATE_DIR`」已由上面的事實回答（Mac 共用、worker 不共用）。tasks 1.10 改為「已由 MLP 確認（事實見 D4.5）」。

**D4.6 不參與「只留一個讀者」（事實，spec:226 同款）**：`status` 不是讀者，不消費信，不與背景讀信任務競爭，也不推進任何游標。`docs/aicomm.md:33`（「同一個身分只留一個讀者」，L-A 更正落點；舊稿寫的 `docs/angareion.md:33-38` 是「建 channel 的三步」，錯）的規則不適用於 `status`。

**D4.7 不寫入守衛要在真的 `ChannelStore` 上做（PM 審查 L6 裁定）**：守衛（tasks 3.x 的改壞表）用假 `CursorStore` 的 `save` 與 `save_many` raise；另外對設定複本與快取目錄的 sha256 前後比對。若 status 路徑繞過 `CursorStore` 直接用 `Path.write_*`／`mkdir` 寫入，假的 store 抓不到，所以 sha256 比對要在**真的** `ChannelStore` 與真的狀態目錄（暫存）上跑。

## D5 `aicomm wake`（只戳不送）

**D5.1 介面（作者提議；指令名待拍板 D-name）**：

```
aicomm wake --to <AI 名> [--mention <AI 名>]... [--interrupt]
```

- **`--to` 與 `--mention` 至少要有一個候選**（扣掉自己之後）；都沒有＝用法錯 exit 1（PM 審查 M4 裁定；與 spec 一致，原先 design 寫的「`--to` 必填」已更正）。`--mention` 可重複；`--interrupt` 的意義與 `send` 相同（對方 `running` 時才打斷）。
- **不接受 `--body`**，不呼叫 Angareion（沒有子行程，F7 那條路徑不存在），不寫任何訊息。
- 自己（`ANGAREION_IDENTITY`）不戳（沿用 `cli.py:224` 的規則）。
- 叫醒條件沿用 `_wake_one`（F13）：只有本機 sidecar 上狀態為 `idle` 或 `running` 才戳；`--interrupt` 只在 `running` 生效。
- 覆寫的 sidecar 位址必須是 loopback，否則 exit 1（沿用 comm-skill 既有條款，spec 101-113）。

**D5.2 戳的內容（事實，comm-skill:90）**：沿用 `WAKE_TEXT`（`src/aicomm/cli.py:42`，「有新訊息：請執行 aicomm inbox。」）。**注意**：這句話假設有新訊息，所以 `wake` 只能在 `send` 之後或 `status` 回未讀之後使用；`wake` 本身不檢查（D5.4）。

**D5.3 exit 與 stderr（待拍板 D-wake-exit；spec 寫選項 B，PM 建議 B）**：

wake 的唯一效果就是叫醒，腳本必須能機器判讀「戳到沒有」。`send` 的 exit 0＝已寫入（叫醒是 best-effort，寫入本身是可回報的結果），這個理由不適用於 `wake`（F20：`_wake_one` 的「已送達，未叫醒」文字也不能沿用）。

| 選項 | exit 0 | exit 3 | exit 2 | exit 1 | 取捨 |
|---|---|---|---|---|---|
| **A**（與 `send` 一致） | 命令沒有用法錯（每個目標的結果看 stderr） | 不用 | 不用 | 用法錯 | 腳本與 `send` 一致；但 0 不代表有戳到，腳本只能解析 stderr 中文句 |
| **B（PM 建議；spec 已寫）** | 至少戳到一個（`poked`／`poked interrupt`） | 有候選但沒有任何一個被戳到，且沒有請求失敗 | 沒有任何戳成功，且至少一次請求失敗 | 用法錯 | 0 的意義就是「有叫醒」，機器可判讀；代價：與 `send` 的 0 不同義，文件要寫清 |

判定優先序（B）：有戳成功 → 0；否則有請求失敗 → 2；否則 → 3。

**exit 3 的重疊（L-E，承認）**：同一個 CLI 的 `aicomm inbox --wait` 逾時也是 3（F12）。`wake` 的 3 與它是同數字、不同指令：前者是「有候選但沒有戳到」，後者是「等待逾時沒有新訊息」。這與 `angareion` 的 D-exit 同型問題，但 `angareion` 那一側已經在 D2.2 處理；本段補上 `aicomm` 這一側：spec（comm-skill 的 wake exit 條目）與 `docs/aicomm.md`、`skills/aicomm/SKILL.md` 都 MUST 分指令定義各自的 3，腳本依指令判讀。（若 D-wake-exit 改選 A，3 不再用於 `wake`，重疊自然消失。）

stderr 固定格式（B 與 A 都要求）：每個候選一行 `wake <AI 名>: <結果>`，結果只能是 `poked`、`poked interrupt`、`skipped not-local`、`skipped state=<狀態>`、`failed <原因>`。**不得含「已送達」字樣**（F20）。

**若選 A，spec 要改的地方**（`specs/comm-skill/spec.md`）：「wake 只戳不送」的 exit 條目（改回「0 表示命令本身沒有用法錯」）；Scenario「不戳不在本機或不可叫醒的 agent」的 exit 3 → 0；Scenario「部分成功是 exit 0」與「sidecar 連線失敗是 exit 2」改寫或刪除。stderr 格式與「不得含已送達」兩條不受影響。

**D5.4 不加「是否有新訊息」的檢查（作者提議，待拍板 D-wake-guard）**：`wake` 不查 Angareion，所以無法確認有新訊息。加檢查會讓 `wake` 依賴 Angareion 的狀態，違背「只戳不送、不碰 Angareion」。替代：要求 `--message <ULID>` 並呼叫 `status`——但那會把 `wake` 變成查詢加叫醒，複雜度不值得。作者建議不加，靠文件規定「只在 `send` 成功之後，或 `status` 回未讀之後使用」。

**D5.5 命令名（作者提議；待拍板 D-name）**：

- 建議：**`wake`**（草稿建議；與 `sidecar` 的 `POST /poke` 不同名，避免與 HTTP 名混淆）。spec 寫的是 `wake`。
- 備選一：**`poke`**——與 sidecar 的 API 同名，但 comm-skill 負面 scenario 把 `aicomm poke` 列為「不認得」，改成 `poke` 時那一條要改成 `aicomm wake`。
- 備選二：**`nudge`**——中性，但與現有文件用語（「戳」）不一致。

**若改名，要全文替換的位置清單**（在 spec 內沒有條件句；條件句已移到本段）：
- `specs/comm-skill/spec.md`：RENAMED 的 TO 標題「對外只有 send、inbox、log、wake 四個指令」；MODIFIED 的同一標題與內文的 `wake`；Scenario「不認得的子指令」的 `aicomm poke`；ADDED 的 Requirement 標題「wake 只戳不送」與全文所有 `wake`；stderr 格式 `wake <AI 名>:`；「log 薄包」Scenario 的「（四個指令，含 `wake`）」。
- `proposal.md`：What Changes、Capabilities（comm-skill 段）、Impact 的 `wake` 字樣。
- `tasks.md`：1.4、§4 的標題與項目 4.x、§5 的標題與項目 5.x、§5 改壞表 (e) 的「新增 `aicomm status`」不受影響但要確認。
- 實作時（不在本 change）：`src/aicomm/cli.py` 的子指令名與 `:83`、`:319` 的用法錯字樣；`docs/aicomm.md`、`skills/aicomm/SKILL.md` 的全部 `wake` 字樣；`tests/unit/test_aicomm_docs.py` 的守護字串。
- 用 `grep -n wake openspec/changes/read-status -r` 可以列出現況；改名前再跑一次以補漏。

**D5.6 不查 Angareion、不查 channel**：`wake` 不需要 channel 參數，因為它不寫、不查訊息；PM 要戳的是人，不是 channel。

## D6 `aicomm send` 三種模式（文件，已決方向）

本人要求把中斷／不中斷兩種模式寫清楚。作者提議的原文（繁中，加到 `docs/aicomm.md` 的新一節，`skills/aicomm/SKILL.md` 的「送達與叫醒」段可以同樣短句）：

> ## 送出的三種模式
>
> - **預設（不中斷）**：`--to` 或 `--mention` 點名的人（扣掉自己），只在本機 sidecar 上狀態是 `idle` 或 `running` 時叫醒；閒著就叫醒，正在跑的排到它這一輪結束之後（由 sidecar 決定）。不打斷它正在做的事。沒給 `--to`／`--mention`（廣播）誰都不叫醒。
> - **`--interrupt`（中斷）**：對方正在跑時打斷這一輪、立刻叫醒；閒著時與預設相同。只用在急件，而且只有 PM、PMO 自己決定（SKILL 軟規則 2）。
> - **`--no-wake`（不叫醒）**：只寫入 Angareion，誰都不戳；對方下次自己 `aicomm inbox` 才看得到。
> - 兩者不能同時給。`--mention` 點名的人套用同一個模式，`--interrupt` 會打斷每一位正在跑的被叫醒者。
> - exit 0 只代表訊息已寫入；叫醒結果看 stderr。

事實核對（審查 M4 的修正）：「沒有候選就不叫醒」與「只限本機 sidecar 的 idle／running」都在程式與 SKILL 已有（F14、SKILL:55），所以原文是正確的；它不是新規則。缺的只是「集中的一節」。

## D7 方案比較與為何不選

| | 零改動基線（D'） | A 回條（獨立 issue） | B 逐則 reaction | C 同機查游標（**選用**） | D 慣例回覆 |
|---|---|---|---|---|---|
| 做法 | 現行軟規則（收到類用 `--no-wake`）＋ `log --thread` 查回覆 | 收件人每次 inbox 寫回條 | 在訊息留言上加 reaction | 唯讀讀別人的游標檔 | 收件人送 `--no-wake --in-reply-to <ULID>` |
| 寫入量 | 0 | 每次 inbox 1–3 次（上限 channel 數） | 每讀一則一次 | 0 | 0 或 1（靠自覺） |
| 範圍 | 跨機 | 跨機 | 跨機，但身分不可辨 | 同機 | 跨機 |
| 與 spec:224 | 不違反 | 違反（回條） | 違反（回條的一種） | 唯讀，不違反 | 字面不違反，但「ack」一詞會被當違規（審查 M1） |
| 本單 | 保留為基線 | 不做（跨機再議） | 不做（所有身分共用一把 token，reaction 分不出是誰；推論） | **做（D3–D4）** | 不納入本 change；只把現行 `--no-wake` 規則列為基線（判斷點 D-ack） |

- **B 的不可行（推論）**：token 每台機器一個檔（`docs/angareion.md:216`），GitHub 上 reaction 的作者是 token 擁有者；同機所有身分都會變成同一人。同一人對同一則留言同種 reaction 只算一個（推論，未驗），多人讀會折成一個。
- **A 的代價（推論）**：每則回條都會改變整條 issue 列表的 ETag（I2），所有正在 `wait` 的人下一輪都變成完整查詢；回條若寫在訊息所在的 issue 上會放大這個成本，所以 A 要放獨立 issue（新 entity，另案）。A 也沒有現成的寫失敗處理：`state.py:166-174` 是 wait 狀態檔「警告一次」的做法，游標的 `save_many`（`state.py:265-286`）失敗則是 raise（草稿 §3 的核對，本輪未重驗），要另寫。
- **D 的定位**：D 不新增機制，只是 `SKILL.md:72` 現行規則的正式化。它不由平台產生回條，所以字面上不違反 spec:224，但讀起來仍像 ack。是否在本 change 正式寫進說明，由 PM 決定（判斷點）。本 change 不新增任何規則。
- **`aicomm` 不加 `status`**：`aicomm` 不做查狀態（F17）。發信方要查，直接用 `angareion status`。這樣 `comm-skill` 的「不做查狀態」條款維持原樣。

## D8 文件與 spec 的落點（逐檔逐行）

M2 裁定：tasks 與 design 的文件落點要逐檔逐行列出。「已在 delta」表示 `specs/` 內已經有對應的 MODIFIED／RENAMED；「實作時」表示 PR 階段才改。行號以 main 為準（開工當下，已逐行打開確認）。

| 檔 | 行 | 現況（摘要） | 改動 | 狀態 |
|---|---|---|---|---|
| `openspec/specs/angareion/spec.md` | 170–171 | 結束碼段 | 「輸出與結束碼」MODIFIED，加 status 的 3／4 | 已在 delta |
| 同上 | 194 | 「六個指令的用法」（給 agent 的說明） | MODIFIED「給 agent 的說明」，改七個並寫三態判讀 | 已在 delta |
| 同上 | 205 | 「六個介面 MUST 經由一個後端介面實作」＋列舉用途 | MODIFIED「後端可以替換」，改七個並加 `status` 使用列舉；列舉提前停止的擴充（M-B） | 已在 delta |
| 同上 | 208 | Scenario「執行六個指令」 | 同上 MODIFIED，改七個 | 已在 delta |
| 同上 | 223–224 | Requirement「六個介面」 | RENAMED＋MODIFIED「七個介面」 | 已在 delta |
| 同上 | 226 | 「log 是唯讀的查詢介面」 | 「七個介面」MODIFIED 內提及 status | 已在 delta |
| 同上 | 292 | Scenario「log 是第六個介面」 | 改名「介面清單恰好是七個」 | 已在 delta |
| 同上 | 460 | Scenario「已讀狀態」 | MODIFIED「log 的本機快取」末段改「游標」 | 已在 delta |
| `openspec/specs/comm-skill/spec.md` | 160–165 | 「對外只有 send、inbox、log 三個指令」與負面 scenario | RENAMED＋MODIFIED 為四個指令；`poke` 的負面 scenario 保留 | 已在 delta |
| 同上 | 215 | 「（三個指令）」Scenario | MODIFIED「log 薄包」，改四個指令 | 已在 delta |
| `docs/angareion.md` | 3 | 「對外只有六個介面」 | 七個；一句話說 `status` 是唯讀查詢 | PR-2 |
| 同上 | 5 | 「沒有 ack、已讀回條」 | 保留「沒有 ack、已讀回條」；加「唯讀查詢 `status` 不是回條」 | PR-2 |
| 同上 | 11 | 「## 六個指令」 | 「## 七個指令」，表加 `status` | PR-2 |
| 同上 | 119 | 游標檔路徑 | 不改（design F1 的引用）| — |
| 同上 | 229 | 「Angareion 對外只有六個介面」 | 七個 | PR-2 |
| `skills/angareion/SKILL.md` | 8 | 「對外只有六個指令；沒有 ack、沒有已讀回條」 | 七個；已讀定義與 inbox 邊界（spec 的「說明」） | PR-2 |
| 同上 | 21 | 「## 六個指令」 | 「## 七個指令」，加 `status` 用法與三態判讀 | PR-2 |
| 同上 | 59 | 同一身分只留一個讀者（同款說明，L-A） | 補一句：`status` 不屬於這條（D4.6） | PR-2 |
| `docs/angareion.md` | 29 | 同一身分只留一個讀者（同款說明，L-A） | 同上，補一句 `status` 不屬於這條（D4.6） | PR-2 |
| `docs/aicomm.md` | 7 | 「aicomm 對外只有三個指令：send、inbox、log。」 | 四個（含 `wake`） | PR-3 |
| 同上 | 33 | 「同一個身分只留一個讀者（#219）」（L-A：這是正確的落點，不是 `docs/angareion.md:33-38`） | 補一句：`status` 不屬於這條（D4.6） | PR-2 |
| 同上 | 50–51 | 叫醒對象 | 補 `wake` 的叫醒條件（同 send）；`wake` 的 3 與 `inbox --wait` 的 3 分指令寫（L-E） | PR-3 |
| 同上 | 新節 | 「送出的三種模式」（D6） | 新增 | PR-1 |
| `skills/aicomm/SKILL.md` | 8 | 「對外只有三個指令（send／inbox／log）」 | 四個（含 `wake`） | PR-3 |
| 同上 | 31 | 同一身分只留一個讀者（同款說明，L-A） | 補一句：`status` 不屬於這條（D4.6）；`status` 的說明在 angareion 的 SKILL | PR-2 |
| 同上 | 14 | 「## 三個指令」 | 「## 四個指令」，加 `wake` 一節 | PR-3 |
| 同上 | 52–58 | 「送達與叫醒」段 | 加三種模式短句（D6） | PR-1 |
| 同上 | 70–72 | 軟規則 | 與三種模式對齊；編號不變 | PR-1 |
| 同上 | 78 | 「不做的事：…單獨戳人、查狀態、ack」 | 改成「查狀態不做；只戳用 `aicomm wake`」 | PR-3 |
| `src/aicomm/cli.py` | 83 | 「（aicomm 只有 send、inbox 與 log）」 | 改含 `wake` | PR-3 |
| 同上 | 319 | 「aicomm 只有 send、inbox 與 log 三個指令」 | 改含 `wake` | PR-3 |
| 同上 | 178、184 | 「已送達，未叫醒」 | `send` 保留；`wake` 另用 D5.3 的固定格式（F20） | PR-3 |
| 同上 | 94、104、105 | `send` 的 help 文字 | 只有核對，不改字（PR-1 核對） | PR-1 |
| `tests/unit/test_aicomm_docs.py`、`test_angareion_docs.py`、`test_aicomm_send.py` | — | 守護字串 | 跟著改 | PR-1／PR-2／PR-3 |

## 判斷點（待本人／PMO 拍板）

| 代號 | 問題 | 狀態 | 作者建議／PM 裁定 |
|---|---|---|---|
| D-open | 要不要開 openspec 變更 | **已決（本人 10-31）**：開，但只限縮成唯讀查詢，不加回條 | — |
| D-def | 已讀＝三組游標任一越過（D1.2） | 方向已決；細節作者提議 | 採用 |
| D-state | 三態與未知（D2.1） | 三態已決；未知的細分與固定順序 **已決（PM 審查 L2）** | 採用 |
| D-exit | 3 與 `wait` 的 3 衝突（D2.2）：A 沿用 3，或 B 改用 5 | **待拍板**；spec 目前寫 A | 選項 A（spec 已寫；若選 B 見 D2.2 的改動清單） |
| D-json | `--json` 欄位（D2.3） | 作者提議；`by` 已刪（**PM 審查 L3**） | 採用 |
| D-text | 文字輸出格式（D2.4） | **已決（PM 審查 L4）**：寫進 spec | 採用 |
| D-fetch | `status` 不經 `resolve()`，不呼叫 `ensure_fresh`（D4.2） | **已決（PM 審查 H1）** | 只用 `channels()`；miss 答未知 |
| D-auth | `status` 走完整啟動，身分／token 缺 exit 2（D3） | **已決（PM 審查 M1）** | exit 2，不是未知 |
| D-scope | `status` 只對 `to`＝X 或點名 X 的訊息作答（D1.4） | 作者提議 | 採用 |
| D-cost | `status` 的列舉成本（D4.3） | **待拍板**；spec 寫 (a)＋驗收門檻（1000 則 ≤ 11 次列表請求） | PM 建議 (b)，若可行；否則 (a) |
| D-name | `aicomm` 的只戳指令名（D5.5）：`wake`／`poke`／`nudge` | **待拍板**；spec 寫 `wake` | `wake`（改名位置清單見 D5.5） |
| D-wake-exit | `wake` 的 exit 與 stderr（D5.3） | **待拍板**；spec 寫 B | **PM 建議 B**（機器可判讀；若選 A 見 D5.3） |
| D-wake-guard | `wake` 不檢查有無新訊息（D5.4） | **待拍板** | 不加檢查，靠文件 |
| D-three-modes | `send` 三種模式原文（D6） | 本人要求寫清；原文待確認 | 採用 D6 原文 |
| D-ack | 慣例回覆（方案 D）要不要寫進說明 | **待拍板** | 不寫進本 change；只列為現行基線 |
| D-A | 跨機回條（方案 A）要不要做，獨立 issue 是否接受 | 本人 10-31：跨機之後再議；**不在本單** | 另案 |
| D-cross | 跨機／跨容器的查詢答未知（`cursor-missing`），是否接受（D4.5） | **待拍板**（tasks 1.9）。理由：Mac↔worker 與 worker 之間不共享狀態目錄（PM 已向 MLP 確認），M5 搬遷後 PMO 與隊員分處兩端，`status` 對這類查詢一律未知；若要涵蓋，需要另案（方案 A，或 volume 共享，或游標寫進後端，後者違反 spec:224） | 本人決定：是接受「同機查詢就夠」，還是 M5 之前就要跨機（那就該開方案 A 的獨立 issue）。作者建議：先接受同機，寫進說明；跨機另案 |
| D-share | 同機身分是否共用狀態目錄（D4.5） | **已決（PM 向 MLP 確認，事實見 D4.5）**：Mac 上共用同一 HOME 與 `~/.local/state/angareion/`；worker 每容器一個、不共享；sidecar 不覆寫 `ANGAREION_STATE_DIR` | 不需退路；跨容器的後果見 D-cross |
| D-reason-order | 多個未知原因同時成立時的 reason 優先序（D2.1） | **已決（PM 審查 L2）**：寫進 spec | 採用 D2.1 表 |
| D-cursor-legacy | 舊格式／壞條目一律 `cursor-unparseable`，不略過（D4.1） | **已決（PM 審查 L1）** | 採用 |

## 作者提議（本人未指示）

D1.2（三組游標任一）、D1.4（廣播答未知）、D1.5（inbox 路徑邊界）、D2.1（未知的細分，含 `cursor-unreadable`）、D2.2 的選項 A、D2.3（`read_at` 為 null）、D4.1–D4.7（唯讀實作與不推進游標）、D5.1–D5.6（`wake` 的形狀）、D6 原文、D7 的排除理由、D8 的落點表。

## 待驗／未知

- GitHub 列表 API 的 ETag 與 `since` 行為在留言被新增、編輯時的精確表現（I2，推論）。
- `status` 在大 issue（數百至千則留言）上的延遲與額度（I3，未量；tasks 的 1000 則假後端只量請求數，不量真 GitHub）。
- ULID 時間與後端時刻的差（I5）：選項 (b) 的寬裕量上界（D4.3）。
- sidecar 對 `poke` 的排隊與 `running` 中的行為（不在本 repo）。
- 跨機／跨容器的查詢要不要涵蓋（D-cross，見 D4.5；目前答未知）。
- 本人 10-31 裁定的原文（只有 PMO 轉述）。
- 同一身分的游標檔在 `ANGAREION_STATE_DIR` 被覆寫時的實際路徑（測試以 `isolated_home` 造檔，未實測）。

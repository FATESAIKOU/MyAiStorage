# 搬遷：舊格式 Session → 分段格式

升級到分段格式（`layout: chunks/1`，#169／#170）之後，**升級前上傳的 Session** 是「閱讀版可讀、raw 不可用」：
`show --raw`、`continue`、`edit` 會明確說這個 raw 是舊格式，要用 `agora import … --as-new` 重匯；
它**不會被刪、也沒有原地轉存**。搬遷是一次性的、由本人動手的事，不是每次升級都要做。

規格在 `openspec/changes/archive/2026-10-22-agora-diff-upload/design.md` 的 F、G；這份是操作手冊。範例裡的 id 都是編的，
不是真的 Session。

## 先講結論

0. **升級前先讓 outbox 排空**（第 0 節）。
1. 準備必保清單（第 1 節）。
2. 升級，**先跑一次正常指令**讓索引的版本升級重建完成；**盤點**：`agora migrate inventory --json --must-keep <清單檔>`（唯讀）。**確認點 ①**（第 2 節）。
3. **重匯**：`agora import session --agent <agent> --external-session-id <id>… --as-new`。等上傳完。**確認點 ②**（第 3 節）。
4. **丟之前緊接著再盤點一次**，只把那份輸出的 `drop_list` 原樣交給 `agora delete`：先不帶 `--yes` 看預覽（**確認點 ③**），再帶 `--yes`（第 4 節）。
5. 驗收；其他機器各自做第 2～5 節；沒有原始紀錄的、`different` 的、卡住的，**一律不在 `drop_list`**，由本人逐筆決定（第 5 節）。

## 0. 升級前：讓 outbox 排空

沒有 `agora sync` 這個指令：會同步的一般指令（例如 `agora search session > /dev/null`，輸出可以丟掉）在同步時會啟動背景上傳器；
同步有 5 分鐘節流，節流期間不會重新啟動上傳器、也不會重新列 Drive。在**舊版**上等背景傳完，直到一般指令不再印
`outbox 有 N 筆未上傳`。要立刻送就用 `agora push session <id>`（它會等自己的 id 離開 outbox）。

**確認排空**（三個都成立）：
- `<state>/outbox/`（預設 `~/.local/state/agora/outbox/`）底下沒有 ULID 目錄；
- 沒有 `agora.background` 或 rclone 行程在跑（`pgrep -fl agora.background`、`pgrep -x rclone` 什麼都沒印）；
- 一般指令不再印「outbox 有 N 筆未上傳」。

**同步有 5 分鐘節流**（`store.SYNC_THROTTLE_S = 300`）：離上一次同步（`<state>/last-sync`）不到 5 分鐘，指令不會重新列 Drive，
也就看不到 Drive 上的新變化；要隔一段時間再跑。（索引版本不符時的重建不受節流影響，開索引時就做。）

**升級後，升級前留在 outbox 的舊格式版本永遠不會被上傳**（上傳器逐筆略過，每個 entry 只警告一次，其他筆照常送）：
送上去會把舊格式寫回 Drive。所以升級前處理最省事。排不掉、真的不要那個未上傳版本的，**本人明確說「放棄」**，把 id 記在
搬遷紀錄（不是清單檔）；升級後 `inventory` 會用 `outbox_legacy: true` 列出它，那一筆**不進 `drop_list`**。

不要手動刪 `state/` 裡的東西。

## 1. 必保清單（本人在意的 Session）

搬遷會丟掉舊 Session 的身分（tags、lineage、`description` 等使用者欄位；沒有原地轉存）。哪些絕對不能出錯，只有本人知道，
所以在一般規則之上加一份清單：

- 一個**放在 repo 外**的本機檔，一行一個 `agora:<ULID>` 或裸 `<ULID>`（26 碼大寫），`#` 開頭是註解、空行忽略。
- 放在家目錄下的暫存位置（不要放進任何 git working tree、也不要放進雲端同步的資料夾），`chmod 600`，**用完由本人刪掉**。
- 指令只傳路徑；內容不貼進終端、issue 或報告。PM 的報告**只記「有帶入」與 id 的數量**。
- 清單內的 Session 連同它的整條 lineage，重匯後必須**逐一** `identical` 才會進 `drop_list`；有一筆不是，**整條**都不進。
- 正在被 `agora continue` 接續的：這一輪不重匯、不丟（`continuing`），列出來，等它結束再跑。
- **讀不進來就不跑**（fail-closed）：檔案開不了（不存在、權限、是目錄）、不是 UTF-8、解析出 0 個 id、有一行不是可辨認的 id、
  或有 id 在這台的索引與 Drive 都找不到、也不在刪除記錄（`state/deleted`）裡 → 明確錯誤、exit 非 0、**不給任何候選**。
  錯誤訊息只有路徑、行號、認得的 id 與原因，**不重印清單的任何一行**。空的清單不等於沒有清單——不要清單就不要給 `--must-keep`。
- 多台機器：清單由 PM 維護同一份；在每台跑之前，把已在別台刪掉的 id 拿掉（在刪除記錄裡的 id 只會被標成 `must_keep_deleted`，
  不算錯；這台從來沒有、也不在刪除記錄裡的 id 是錯，可能是打錯字，停下來問本人，不要自己猜）。
- 通過了就可以丟：這份清單管的是「丟之前必須逐一核對」，不是「永不丟」。`identical` 只比 raw，**不保證閱讀版 body 沒被手改過**；
  要保留手改過的 body，本人把那條 lineage **整條**從 `drop_list` 移除。

## 2. 盤點（確認點 ①）

```bash
agora search session > /dev/null        # 升級後先跑一次正常指令（沒有 agora sync），讓索引重建完成
agora migrate inventory --json --must-keep ~/keep.txt
agora migrate inventory                  # 人看的版本：每類的數量、drop_list 的 id、每個不能丟的 id 與原因
```

`inventory` **唯讀**：不 `sync`、不下載任何 `session.md`、不上傳、不啟動上傳器、不重建索引（索引版本不符就報錯，要求先跑
一次正常指令）；Drive 端只做一次 listing（`lsjson`）。它不替沒連過 Drive 的機器建 Drive 資料夾。

輸出（`--json`）只有 metadata：

| 欄位 | 意思 |
|---|---|
| `host` | 這台的 hostname |
| `counts` | 每個 `class` 的數量 |
| `drop_list` | 所有 `droppable: true` 的 id（升冪、只有 id；**完整 lineage 的聯集**） |
| `must_keep_deleted` | 必保清單裡已在刪除記錄的 id（只有 id） |
| `sessions[]` | `ulid`、`agent`、`external_id`、`host`、`relation`、`layout`、`raw_size`、`class`、`replacement`、`old_children`、`lineage`、`must_keep`、`lineage_protected`、`continuing`、`outbox_legacy`、`droppable`、`not_droppable_reason` |

**沒有 title、description、tags、body。** 必保清單只以布林標記（`must_keep`／`lineage_protected`）、`drop_list` 與
`must_keep_deleted` 的 id 與數量出現。

`class`：

| class | 意思 | 能進 `drop_list` |
|---|---|---|
| `local-present` | 來源是這台的、原檔還在 | 是（其他條件也成立時） |
| `local-missing` | 來源是這台的、原檔不在了 | 否 |
| `other-machine` | `source.host` 不是這台 | 否（等那一台跑，或本人逐筆決定） |
| `host-unknown` | header 沒有 `source.host` | 否 |
| `no-source` | 沒有 `source`（merge、手改過的 header） | 否 |
| `agent-unavailable` | 這台沒有那個 agent 的 store | 否 |
| `drive-only` | Drive 上有、這台沒有索引（只有 ULID） | 否 |
| `new-format` | 已經是 `chunks/1` | 否（不用處理） |

**沒有原始紀錄的類別一律不丟。**

`replacement` 是同一個 agent session 的 `chunks/1` Session。`relation_to_old` 是 `identical` 才代表「內容相同的替代版本已在
Drive 上、而且讀得回來」，四件事都要成立：

1. 兩邊整份 raw 的 `md5` 與 `size` 相同；
2. 替代版本就是 Drive 上那一份（索引的 `session.md` md5 等於 listing 的），而且它列的**每一個段都在 listing**；
3. 舊的也還是 Drive 上那一份；
4. 兩邊都有 raw。

其餘是 `different`（`reason`：`md5-differs`／`drive-version-elsewhere`／`segments-missing`／`old-drive-version-elsewhere`／
`old-not-on-drive`）或 `null`（無法比較）。**原檔在匯入後被截短，重匯得到較短的新版本——那是 `different`，不是 `identical`**；
替代版本還在 outbox、沒傳完、或少一個段，也都不是 `identical`。原檔只是長大也會是 `different`，要本人逐筆看。

`not_droppable_reason`（依序取第一個成立）：`class-not-local-present`、`continuing`、`no-replacement`／`relation-null`／
`relation-different`、`outbox-legacy`、`lineage-member-not-droppable`（自己都通過，但同一條 lineage 有別筆沒通過）。
**同一條 lineage 的成員要嘛全在 `drop_list`、要嘛全不在**：父 Session 只要有批外的子 Session 就會被 `delete` 拒絕。

這時 `drop_list` 通常還是空的——替代版本還沒重匯。**確認點 ①：本人看過數量、類別與必保清單的標記。**

## 3. 重匯（確認點 ②）

```bash
agora import session --agent claude --external-session-id <id1>,<id2> --as-new
```

**一定要 `--as-new`。** 沒有它，import 會先用 agent＋session id 找到舊的 Session；舊的還在索引裡（我們刻意不先刪），
所以它會被當成「同一個 Session 重新匯入」。現在一般 import 對舊格式**直接拒絕**（exit 1，訊息指向 `--as-new`；不更新、
不分支），所以不會靜靜地壞掉，但也不會幫你重匯。

`--as-new` 的語意：忽略同來源的舊格式 Session；已經有同來源的 `chunks/1` Session 就印出它的 id（重跑是 no-op，也不讀來源）；
否則建一個新的 Session（`relation: import`、**沒有 `parents`**，否則舊的會因為「有子 Session」而刪不掉）。舊 Session 一個位元組
都不動。每一個都是**第一次匯入就是分段**。失敗不擋下一個。

匯入指令在「上傳排隊」時就回來。**確認點 ② 要等上傳完成**：再跑一次 `inventory`（同一個清單），這些類別的 `replacement`
都出現、而且 `identical`；看到 `drive-version-elsewhere` 就是還沒傳完（指令自己會回報；沒成功就
`agora push session <新 id>`）。不是 `identical` 的逐筆處理。必保清單內正在接續的先跳過、列出來。

## 4. 丟舊的（確認點 ③）

**動手之前緊接著再跑一次 `inventory`（同一個清單），以那一次的輸出為準。** 第 2 步的 `identical` 在重匯與上傳的窗口內可能
過期（有舊版機器改了舊 Session、別台把替代版本的某一段當孤兒清掉）；`delete` 的預覽只列 id 與標題，看不到「相同／不同」。

```bash
agora migrate inventory --json --must-keep ~/keep.txt > ~/inv.json     # 緊接著；輸出放家目錄下，用完刪
python3 -c 'import json,os;print(*json.load(open(os.path.expanduser("~/inv.json")))["drop_list"])'
agora delete session <那些 id>          # 先不帶 --yes：只印預覽（要刪的、被跳過的）= 確認點 ③
agora delete session <那些 id> --yes
```

- 最後一次盤點的 `counts` 若有 `drive-only`（Drive 上有、這台索引沒有的資料夾，可能是別台剛匯入的子 Session），**先隔 5 分鐘以上再跑一次一般指令（例如 `agora search session`，
  這樣才會重新列 Drive；節流見第 0 節），再盤點一次**：`delete` 的「有批外子 Session 就拒絕」也只看本機索引，看不到它們。
- **只把 `drop_list` 原樣交給 `delete`**；不要自己從 `sessions` 挑 id。
- 整條 lineage 在**同一次**指令裡給。搬遷前確認沒有 `continue` 在跑（使用中的會被跳過）。
- `different`／`outbox_legacy` 要丟是**人工例外**：本人明確給整條 lineage 的 id，記在搬遷紀錄；`drop_list` 不會為它變動。
  要保留（例如 body 手改過）也只能整條從 `drop_list` 移除，記在搬遷紀錄。

**不可逆的部分**：本機鏡像（含整份 raw）**立刻消失**；Drive 的資料夾（`session.md`、所有 `seg-*`、舊格式的 `raw-*.json`）
一起進垃圾桶，**30 天內**可以在 Drive 網頁還原，之後沒有。舊 Session 的使用者 metadata 與 lineage 沒有原地轉存：
要留住就**不要丟那條 lineage**，或先把要的欄位抄下來、重匯後用 `agora edit`／`--add-tag` 貼回新的 Session。
舊 raw 沒有逐檔刪的程式，丟舊 Session 就是它唯一的下場。

## 5. 驗收、其他機器、沒有原始紀錄的

- `inventory` 再跑一次（同一個清單）：舊格式、本機有原始紀錄、而且那一條 lineage 沒被 `other-machine`／`no-source`／
  `outbox_legacy` 擋住的應該是 0；必保清單的每條 lineage 全部 `identical`、或被本人明確保留；`drop_list` 應該是空的。抽幾個新
  Session 用 `show --raw` 對 md5。
- 其他機器也要升級，並且**各自**跑 2～5（它們的原始紀錄在它們身上）。
- 沒有原始紀錄的類別：由 `inventory` 列出，本人逐筆決定保留（閱讀版可讀）或 `delete`。

## 隱私

- **掃描整份 raw，不掃單一段檔**。段是按固定大小硬切的，邊界不在行上；一個 token 或密碼可能剛好被切成兩段，逐段掃會漏掉。
  任何憑證／隱私掃描都對**接回的整份 raw** 做（本機鏡像／outbox 裡就是整份）。
- `inventory` 的輸出沒有 title、description、tags、body；`--json` 與人看的版本都是。它不下載 `session.md`、不開 agent 的內容。
- 必保清單的內容不進 repo、不上傳、不複製進 `state`／log、不重印；錯誤訊息只有路徑、行號、認得的 id 與原因。`inventory` 是
  前景指令，不寫 `upload.log`。報告只記「有帶入」與數量。
- Drive 垃圾桶保留 30 天：被 `delete` 的 Session（含它的 raw 段）在這 30 天內仍在使用者的 Drive 裡，30 天後才真的消失。
  要更快清掉，必須自己清 Drive 的垃圾桶。
- 文件與範例不含真 token、真路徑、真 Session id。

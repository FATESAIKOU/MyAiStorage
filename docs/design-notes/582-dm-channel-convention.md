# #582 DM 與成員頻道慣例（MyAiEntry N34 依據）

- 單：#582（MAS，設計文件；App 端 N34 依據本單）。
- 作者：專案架構師（作者模式，Gemini 4 Hydrogen (High)），2026-11-28。基準：main `38aa2b5`。
- 範圍：**只寫設計文件，不改程式**。不動 `src/`、`openspec/`、`config/angareion/`。
- 相關決定：`docs/architecture/decisions.md` D-39（待審查與 PM／本人依 Q-1 定案）。
- 實測：在本機隔離暫存環境（不碰真頻道、不讀憑證、不放 `/tmp`）以 `angareion.cli.main` 接 `FakeBackend`、`ChannelStore(FakeGit)` 與套用 `registry_rules.rebuild_channels` 的 `FakeRegistryClient` 驗證現行指令收發與隔離；原樣輸出見 §5。

---

## 0. 結論（一頁摘要）

**建議採 (C) 單一專用 DM 頻道 `dm-secretary`**（全公司只多 **1** 個頻道實體，所有「本人 ↔ 單一 AI」一對一對話共用這條頻道，訊息強制帶 `--to <對方 AI 名>`，App 依 `(from, to)` 拆成各 AI 的對話串）。

四個核心問題的答案：

1. **命名**：固定頻道名 **`dm-secretary`**（符合 `CHANNEL_NAME_RE = ^[A-Za-z0-9_-]+$`、`channel_slug` 原樣不編碼、不占 `team-*` 前綴）。若 PM／本人選備案 **(B) 每對一頻道**，命名為 **`dm_<a>_<b>`**（`a < b` 字典序，兩者皆合法 `ainame`；不能用單一 `-`，見 §2.2）。
2. **註冊簿怎麼登記（`kind` 或其他欄）**：**沿用現行 schema 1，不新增 `kind` 欄、零改碼**。`registry.parse_comment` 目前嚴格拒絕未知欄位（`unknown fields`），`channels.json` 也只有 `backend`／`issue`／`retired`；DM 身分由固定頻道名 `dm-secretary`（或備案 (B) 的 `dm_` 前綴）辨識。若未來要加 `kind`，改動清單列於 §6。
3. **可見性（誰讀得到、會不會混進團隊頻道）**：
   - **完全不混進團隊頻道**：`team-*` 與 `secretary-pmo` 的 GitHub issue 獨立，DM 不出現在團隊 `read --channel`／`log --channel`，不消耗團隊頻道的 2500 則上限，也不會撞到 `pmo` 在 `secretary-pmo` 的派工樣板檢查。
   - **一般收信隔離**：DM 訊息**強制帶 `--to <對方 AI 名>`**（嚴禁省略 `--to` 廣播、嚴禁帶第三者 `--mention`）。第三者用 `angareion read`、`angareion wait`、`aicomm inbox`（不帶 `--channel`）或 `aicomm log`（預設補 `--to me`）**都讀不到**。
   - **後端與顯式 `--channel` 邊界**：Angareion 第一期無 ACL（所有通道共用 repo 與 token），任何持有 channel token 的隊員顯式執行 `angareion read --channel dm-secretary`（會印 stderr 警告）、`angareion log`（不帶 `--to me`）或直接看 GitHub issue 仍看得到；故仍適用 `docs/angareion.md`「不貼私人資訊，改參照 MyBrain」的規則。
4. **DM 的成員是誰**：每則 DM 的兩方由訊息的 `from` 與 `to` 決定（兩者皆為合法 `ainame`、`from != to`；本人端固定為 `myaientry-secretary-p1`，對象端為單一 AI 席位）。不另外在 `channels.json` 存成員名單。

---

## 1. 現況與限制（依現行程式碼與規格）

1. **頻道名文法與編碼**：
   - `openspec/specs/angareion/spec.md`、`src/angareion/messages.py`（`CHANNEL_NAME_RE`）與 `src/angareion/registry.py`（`CHANNEL_NAME_RE`）規定 channel 名為 `^[A-Za-z0-9_-]+$`（非空 ASCII 字母、數字、`-`、`_`）。Angareion 本身沒有長度上限；GitHub Issue 標題上限為 256 字元。
   - `config.channel_slug` 對 `[a-z0-9_-]` 原樣通過（不作 `%XX` 轉義）。
2. **AI 名文法（`src/ainame/__init__.py`）**：
   - 格式為 `<project>-<role>-<team>[-<seq>]`，`MAX_LEN = 32`，字元限 `a-z`、`0-9`、`-`（字母開頭、不可 `--`、不可 `-` 結尾、**不含 `_`**）。
   - `<project>` 允許含 `-` 且允許含角色字（例：`pm-tools-impl-m1-1`）。
   - `SINGLE_ROLES`（`pm`、`secretary`）不帶 `<seq>`；`arch`、`impl`、`test`、`rev` 必帶 `1..99` 的 `<seq>`。
   - 本人位址固定為 `myaientry-secretary-p1`（`skills/aicomm/SKILL.md`）。
3. **送出、讀取與過濾語意（`client.py`、`backend.py`、`cli.py`、`log_query.py`）**：
   - `angareion send`（`cli.cmd_send`）固定寫入 `datatype: "message"`（沒有 `--datatype` 旗標）；`--to` 走嚴格 `ainame` 檢查，省略 `--to` 即為廣播。
   - `Angareion.read`／`Angareion.check` 不給 `--channel` 時，**遍歷 `ChannelStore.entities()` 的所有已登記頻道**（`READ_CONCURRENCY = 8` 併發），以 `backend.scope_matches` 過濾：`recipient` 模式收 `to is None or to == me or me in mentions`；`mentioned` 模式收 `to == me or me in mentions`。
   - `angareion read --channel <ch>` 不過濾 `to`（讀頻道全體，並在 stderr 印 `_CHANNEL_READ_WARNING`）。
   - `angareion log` 不給 `--to` 時，單／多頻道都列出全部訊息（含寄給別人的）；`aicomm log` 會在使用者未給 `--to`／`--mentioned` 時自動注入 `--to me`。
   - `log_query.select` 對 `--from` 與 `--to` 取**交集**（AND），沒有單一旗標可直接表達「`(from=A ∧ to=B) ∨ (from=B ∧ to=A)`」。
4. **註冊簿與重建（`registry.py`、`registry_rules.py`、`channel_config.py`）**：
   - `registry.parse_comment` 對 `register`／`unregister` 嚴格檢查鍵集合為 `{schema, action, channel, backend, issue}`；**出現任何額外欄位（例如 `kind`）即判 `InvalidAction("unknown fields: ...")` 並回報**。
   - `registry_rules.rebuild_channels` 只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言，產出只有 `{backend, issue, retired?}`。
   - `config/angareion/channels.json`（現有 25 個頻道，含 `secretary-pmo` 與各 `team-*`）由 GitHub Actions workflow 重建並 commit 到 `main`；建立新頻道從送出 `register` 到落地有時間差（`channel create` 最多等 120 秒）。
   - 註冊簿 issue（`MyAiStorage#47`）本身也受 GitHub 單一 issue **2500 則留言上限**限制，所有頻道的建立、刪除與換頁共同消耗它（`docs/design-notes/542-channel-rollover.md`）。
5. **PMO 收信模組的既定行為（`src/pmo/inbox.py`、`src/pmo/round.py`）**：
   - `pmo` 透過 `angareion read`（無 `--channel`）收信。只要訊息在任何頻道帶 `to: mypmo-pm-m1`（且不是 `secretary-pmo` 上的 `# 工作：` 派工單），`inbox.is_call` 就為真；若寄件者又是 `myaientry-secretary-p1`（`is_secretary`），`pmo` 會**自動**對該頻道送出一則機械 `call` ack（`收到，已收件（叫人 id=...）`，`to=myaientry-secretary-p1`），且 `_claimed_by_secretary` 會將其歸入秘書階段。
   - 在 `secretary-pmo`（`dispatch_channel`）上，來自 `myaientry-secretary-p1` 且未以 `# 工作：` 開頭、又沒觸發 `is_call` 的訊息會被 `parse_work_message` 判為「收到不合規秘書訊息」警告。
6. **上限與換頁（#542）**：
   - 每頁上限 `PAGE_COMMENT_LIMIT = 2500`，`PageWriter` 於 `PAGE_ROLLOVER_AT = 2400` 自動開下一頁並寫入 `rollover` 註冊簿留言；既有團隊頻道（`team-mypmo-m1`、`team-mylinuxpool-w2`、`w3`）已經換過頁。
7. **本人的限制（2026-11-28 原話）**：
   - **「不要再另外增加實體了，會很亂」**（雖針對游標 issue，但明確表達了反對為每個小單元增殖 GitHub issue 實體的設計偏好）。

---

## 2. 方案比較

### 2.1 (A) 不新增實體：既有頻道內的雙人視圖

- **做法**：不建新頻道；本人與某 AI 的 DM 直接寫進該 AI 所屬既有頻道（例：本人↔PMO 用 `team-mypmo-m1`，本人↔專案隊員用 `team-<project>-<team>`），強制帶 `--to <對方>`，App 端篩選 `(from == me and to == peer) or (from == peer and to == me)` 顯示為 DM 視圖。
- **優點**：
  - 0 個新 GitHub issue、0 筆 `channels.json` 增長、0 則註冊簿留言。
- **缺點與結構問題**：
  1. **嚴重混進團隊頻道**：DM 留言就落在團隊 issue 上，任何隊員跑 `angareion read --channel <team-*>` 或 `angareion log --channel <team-*>` 都會看見全部 DM；`pmo` 對 `myaientry-secretary-p1` 叫人的機械 ack 也會直接洗進團隊頻道。
  2. **無法區分「DM」與「團隊公開 `@to`」**：在 `team-mypmo-m1` 或 `team-myaistorage-w1` 裡，原本就存在 `from: myaientry-secretary-p1, to: <AI>` 的正常團隊對話。App 沒有欄位可辨識哪一則是 DM、哪一則是團隊頻道裡點名某人：若在團隊視圖隱藏 `(secretary, peer)` 會吃掉正常團隊回覆；不隱藏則 DM 會出現在團隊視圖。
  3. **需要新欄位（且現有 CLI 送不出來）**：要消除上一點歧義，必須定義新欄位（例如 `datatype: dm`），但現行 `angareion send` 寫死 `datatype: "message"`，無法不改程式就由 AI／CLI 送出。即使改了，CLI 端的 `read --channel` 與 `log --channel` 仍會把 `datatype: dm` 讀出來（spec 規定不認得的 datatype 照位址遞送）。
  4. **加速消耗團隊 2500 則額度**：`team-mypmo-m1` 等高流量頻道已經換過頁，塞入一對一聊天只會更快觸發換頁。
  5. **跨團隊／無隊頻道席位無處落腳**：`myaientry-secretary-p1` 本身沒有 `team-myaientry-p1` 頻道；只掛在 `secretary-pmo` 則會與 PMO 派工規則衝突；部分跨組席位也未必對應單一 `team-*` 頻道。
- **App 若採 (A) 所需的額外欄位／資料**：`agent -> team_channel` 映射規則、`datatype: dm`（需改 `angareion` CLI 與 TS writer）、以及團隊視圖排除 `datatype == "dm"` 的過濾邏輯。

### 2.2 (B) 每對一個 DM 頻道（例如 `dm_<a>_<b>`）

- **命名規則分析**：
  - 不可用單一 `-` 當分隔（如 `dm-<a>-<b>`）：因為 `ainame` 的 `<project>` 段允許包含 `-` 與角色字（例：`pm-tools-impl-m1-1`），`dm-foo-pm-m1-bar-impl-w1-1` 無法無歧義切出 `a` 與 `b`。
  - 應改用 `ainame` 禁用的字元 **`_`**（或 `--`）分隔：**`dm_<a>_<b>`**，其中 `a, b = sorted([p1, p2])`（字典序排列，確保一對只有一個頻道名，`a != b`）。
  - 合法性與長度：符合 `^[A-Za-z0-9_-]+$`；`channel_slug` 原樣不轉義；`ainame` 上限 32 字，故 `dm_<a>_<b>` 最長 `3 + 32 + 1 + 32 = 68` 字元，遠低於 GitHub Issue 標題上限 256 字元。
- **註冊簿登記方式**：
  - 現行 `registry.py` 不允許 `register` 帶額外欄位（如 `kind`），故零改碼做法是只用 `dm_` 前綴辨識，註冊簿留言維持 `{schema: 1, action: "register", channel: "dm_<a>_<b>", backend, issue}`；若要顯式存 `kind: "dm"`，需改動 `registry.py`、`registry_rules.py`、`channel_config.py`、`channel_cli.py` 與 spec（§6）。
- **誰能建**：受註冊簿授權（`OWNER`／`MEMBER`／`COLLABORATOR`）與 channel token（MyAiStorage + AiCompany-channel 的 `Issues: Read and write`）限制，僅本人或 PMO（或持有相同授權的執行環境）能執行 `channel create`。App 若無該授權或直接建，還須處理最多 2 分鐘的 workflow 落地延遲。
- **2500 上限與 #542 換頁**：每對獨立一條頻道，各自適用 #542 自動換頁（2400 開新頁）；單對不容易滿，但每次建頻道或換頁都會耗掉共用註冊簿（`MyAiStorage#47`，上限 2500 則）的留言額度。
- **頻道數量增長的嚴重代價**：
  1. **直接衝突本人「不要再另外增加實體了，會很亂」**：10 個對象就是 10 張 issue，30 個對象就是 30 張 issue。
  2. **Worker `<seq>` 換代造成大量孤兒頻道**：`impl`／`arch`／`test`／`rev` 的席位帶序號（例如 `myaistorage-arch-w1-34`、`mylinuxpool-impl-w2-5`），席位重開或換號後舊頻道即廢棄，若不持續手動 `channel delete` 清理，歷史殘留實體會不斷累積（即使 delete 仍留下已關閉的 issue）。
  3. **全公司所有 AI 的輪詢負載 $O(N)$ 放大**：`Angareion.check`（`wait` 每 15 秒一次）、`Angareion.read`（`aicomm inbox`）與無旗標的 `angareion log` **每次都會掃描 `channels.json` 裡的全部頻道**（`READ_CONCURRENCY = 8` 原是依約 15 個頻道估算）。每新增 N 條 DM 頻道，**全公司每一位不相關的 AI** 每次收信與等待都要多查 N 張 GitHub issue（即使有 ETag 304，冷啟動、游標前進、新身分與 `log` 仍會打滿全量請求，逼近 secondary rate limit）。

### 2.3 (C) 單一專用 DM 頻道 `dm-secretary`（建議案）

- **做法**：全系統只建立**唯一 1 條** DM 專用頻道 **`dm-secretary`**（對比既有的 `secretary-pmo` 派工頻道），專供「本人（`myaientry-secretary-p1`）↔ 任一單一 AI 席位」的一對一訊息使用。
  - 所有 DM 訊息一律送往 `--channel dm-secretary`，且**強制帶 `--to <對方 AI 名>`**（嚴禁省略 `--to` 廣播，也不帶第三者 `--mention`）。
  - AI 端不需任何特殊指令：照現有慣例跑 `angareion read`／`aicomm inbox`（不帶 `--channel`），只會收到 `to` 等於自己的 DM；回覆用 `aicomm send --channel dm-secretary --to myaientry-secretary-p1 --body ...`。
  - App（N34）讀 `dm-secretary`（例如經 `angareion log --channel dm-secretary --json`），依對方位址 `peer = (to if from == "myaientry-secretary-p1" else from)` 分組，即呈現每位 AI 的獨立一對一對話串。
- **為什麼 (C) 同時解掉 (A) 與 (B) 的痛點**：
  - 相對 (A)：**完全不混進團隊頻道**、不吃 `team-*` 的 2500 則額度、不與團隊 `@to` 混淆、不撞 `secretary-pmo` 派工檢查、跨組或新開席位都能直接 DM。
  - 相對 (B)：**只有 1 個新實體**（不是 $O(N)$ 個），不因 worker `<seq>` 換號產生孤兒頻道，不消耗註冊簿 2500 則額度，全公司輪詢只多 1 條頻道（25 → 26），而且對任何新 AI 開聊**零落地延遲**（不必等 `channel create` workflow 跑 2 分鐘）。
  - 相對 **(A)＋`datatype: dm`**：**零改碼**，現行 `angareion` CLI 今天就能直接收發（見 §5 實測）。
- **代價與邊界**：
  - 仍新增了 1 個頻道實體（相比 (A) 的 0 個），且所有人的 DM 共用該頻道的 2500 則單頁上限（觸及 2400 時由 #542 自動換頁透明處理，對 App／AI 無感）。
  - 顯式執行 `angareion read --channel dm-secretary` 或 `angareion log`（不帶 `--to me`）會同時看到本人與其他 AI 的 DM；這與 (A)、(B) 的後端無 ACL 本質相同，靠「AI 一律不帶 `--channel` 收信、`aicomm log` 預設 `--to me`」與「不貼私人資訊」規範守住。

### 2.4 比較總表

| 面向 | (A) 不新增實體（既有頻道篩選） | (B) 每對一個 DM 頻道（`dm_<a>_<b>`） | (C) 單一專用 DM 頻道（`dm-secretary`，**建議**） |
|---|---|---|---|
| 新增實體數 | **0** | $O(N)$（每對 1 張 issue＋註冊簿紀錄） | **固定 1**（全系統共用 1 條） |
| 會不會混進團隊頻道 | **會**（`read/log --channel` 全混、與團隊 `@to` 相撞） | 不會 | **不會** |
| 第三者一般 `read`／`inbox` 可見性 | 讀不到（有 `--to`） | 讀不到（有 `--to`） | **讀不到（有 `--to`）** |
| 第三者 `read --channel`／`angareion log` | 看得到（混在團隊頻道裡） | 看得到（需指定該 DM 頻道或無條件 `log`） | 看得到（需指定 `dm-secretary` 或無條件 `log`） |
| 團隊頻道 2500 額度影響 | **有**（加速 `team-mypmo-m1` 等換頁） | 無 | **無** |
| 全公司 `wait`／`inbox` 輪詢成本 | +0 | **全體 AI 每輪多掃 N 條** | **只多 1 條** |
| Worker `<seq>` 換代影響 | 無 | 產生大量廢棄 DM 頻道 | **無**（同一對話依 `peer` 自然延續或開新串） |
| 新對象開聊延遲 | 0（若對象有對應團隊頻道） | 最多 2 分鐘（建頻道等 workflow 落地） | **0（立即可送）** |
| 現行 Angareion 指令能否直接收發 | 部分可行，但無法區分 DM 與團隊 `@to` | 可（不加 `kind` 時） | **完全可行（零改碼）** |
| 與 `pmo` 派工／叫人相容性 | 在 `secretary-pmo` 撞派工；在 `team-*` 洗入 call ack | 相容 | **相容**（不撞 `secretary-pmo`，ack 回在 `dm-secretary`） |

---

## 3. 建議與選案理由

1. **首選建議 (C) `dm-secretary`**：在「不讓 GitHub 實體隨對話對象膨脹混亂」與「不污染團隊頻道、零改碼立即可用」之間取得唯一乾淨的平衡點。從架構角度看，它把實體數量從 $O(\text{席位數})$ 壓回 $O(1)$，同時保持 `team-*`、`secretary-pmo`、`dm-secretary` 三種溝通面的職責分離（SRP）。
2. **若本人堅持 0 新增實體**：不應直接做無標記的 (A)（會把團隊 `@to` 與 DM 混死）；必須先另開單替 `angareion send` 加上 `--datatype dm`（或 `dm` 子指令），並接受 DM 仍會混在團隊 GitHub issue 與 2500 額度內的代價。
3. **若未來需要「AI 對 AI 私有頻道」或「每對獨立換頁／刪除」**：屆時可再依 §4 備案條文的 **`dm_<a>_<b>`** 擴充，與 `dm-secretary` 並不互斥。

---

## 4. 慣例條文（定案後供 Angareion 營運、AI 隊員與 MyAiEntry N34 依循）

### C-1 命名慣例
- **主案 (C)**：DM 專用頻道名固定為 **`dm-secretary`**（全小寫 ASCII、`-`，符合 `CHANNEL_NAME_RE`）。
- **備案 (B)**（若改採每對一頻道）：頻道名為 **`dm_<a>_<b>`**，其中 `a`、`b` 皆為合法 `ainame`、`a != b` 且依 ASCII 字典序 `a < b`。因 `ainame` 不含 `_`，`name.removeprefix("dm_").split("_")` 必可無歧義還原雙方席位；最大長度 68 字元。

### C-2 註冊簿登記
- 沿用現行 schema 1 註冊格式，**不新增 `kind` 或其他欄位**（`registry.py` 與 `channels.json` 維持不變）：
  ```json
  {"schema": 1, "action": "register", "channel": "dm-secretary", "backend": "aicompany-channel", "issue": <N>}
  ```
- 重建後 `config/angareion/channels.json` 條目形狀與一般頻道相同：`{"backend": "aicompany-channel", "issue": <N>}`。

### C-3 建置與權限
- 由本人（`OWNER`）或 PMO（`COLLABORATOR`/`MEMBER`＋channel token）在部署時執行一次：
  ```bash
  angareion channel create dm-secretary --backend aicompany-channel
  ```
- 各 team／worker／App **不自行建立**額外 DM 頻道。

### C-4 成員與位址規範
- DM 只有兩名成員：寄件者 `from` 與收件者 `to`，皆須為合法 `ainame` 且 `from != to`。
- N34 的使用場景中，本人端固定為 **`myaientry-secretary-p1`**；對象端為目標 AI 的完整席位名（例如 `mypmo-pm-m1`、`myaistorage-arch-w1-34`、`mylinuxpool-impl-w1-1`）。
- **兩條硬規矩**：
  1. 在 `dm-secretary` 送訊**必須**指定 `--to <對方 AI 名>`，**嚴禁省略 `--to`**（否則成為廣播，會進入全公司所有 AI 的 `inbox`）。
  2. 在 `dm-secretary` 送訊**不可帶第三者 `--mention`**（確保一對一可見性；若需多方參與應回團隊頻道）。

### C-5 可見性與隱私邊界
- **團隊頻道隔離**：`dm-secretary` 不會混入任何 `team-*` 或 `secretary-pmo`。
- **一般收信隔離**：第三者執行 `angareion read`、`angareion wait`、`aicomm inbox`（不帶 `--channel`）及 `aicomm log`（預設 `--to me`）皆讀不到他人的 DM。
- **無後端 ACL（重要）**：Angareion 第一期沒有成員存取控制；任何持有 channel token 的隊員若顯式執行 `angareion read --channel dm-secretary`（stderr 會警告）或 `angareion log`（不帶 `--to me`），或直接讀取 GitHub issue，仍看得到內容。**因此禁止在 DM 內貼私人資訊或憑證，一律改參照 MyBrain 路徑或案件 id。**

### C-6 2500 則上限與 #542 換頁
- `dm-secretary` 是一般頻道，完全套用 #542 的 `PAGE_WARN_AT`（2000）、`PAGE_ROLLOVER_AT`（2400）、自動換頁與 `retired` 48 小時寬限期讀取；`angareion channel status dm-secretary` 可觀測其現頁則數與頁數。

### C-7 MyAiEntry App（N34）契約
1. **送出 DM**：`channel: "dm-secretary"`、`datatype: "message"`、`from: "myaientry-secretary-p1"`、`to: "<target-ai-name>"`，`mentions: []`（序列化省略）。若透過轉發呼叫 `aicomm send`，可同時叫醒在池內 `idle`／`running` 的目標 AI。
2. **讀取／查詢對話**：以 `angareion log --channel dm-secretary --json`（或讀取該頻道鏡像）取得訊息，依 `(received_at, id)` 排序（同 #510）。
3. **App 端雙人視圖過濾**：對每則訊息檢查 `from` 與 `to` 皆非空且其中之一為 `myaientry-secretary-p1`；以 `peer = (m.to if m.from == "myaientry-secretary-p1" else m.from)` 分組顯示各 AI 的一對一對話串。若遇到異常的廣播（`to == null`）或含其他 `mentions` 的訊息，不歸入一對一對話串（可記警告或略過）。
4. **團隊視圖完全不需改**：`team-*` 與 `secretary-pmo` 本身就不含 DM 訊息，正常 `@to` 訊息照原樣顯示。
5. **PMO 自動 `call` ack 相容**：當本人在 `dm-secretary` 發訊給 `mypmo-pm-m1`，現行 `pmo`（`src/pmo/inbox.py`）會將其視為 `叫人`（`is_call`），並自動在 `dm-secretary` 回一則 `收到，已收件（叫人 id=...）`（`to: myaientry-secretary-p1`），接著 PMO 再依需要回覆正式內容。這是既有行為，N34 應正常顯示，勿誤判為異常。

### C-8 AI 隊員回覆慣例
- 收信：照舊跑 `aicomm inbox`（或 `angareion read`，**不加 `--channel`**）；看到來源 `channel: dm-secretary`、`from: myaientry-secretary-p1` 時，即為本人的 DM。
- 回覆：指定同一頻道與對象：
  ```bash
  aicomm send --channel dm-secretary --to myaientry-secretary-p1 --in-reply-to <原訊息 id> --body '...'
  ```
  （本人不在 sidecar 上，`stderr` 回 `wake: myaientry-secretary-p1 not-woken ...` 但 `exit 0` 已送達，屬正常預期。）

---

## 5. 驗證方式（隔離暫存環境＋FakeBackend 原樣輸出）

以腳本在工作區外之暫存目錄（不碰 `/tmp`、不碰真頻道、不讀真憑證）驗證：
- 清空 `XDG_*`、`CLAUDE_CONFIG_DIR`、`OPENCODE_CONFIG_CONTENT`、`CODEX_HOME`、`ANGAREION_IDENTITY`。
- 將 `HOME`、`TMPDIR`、`AGORA_*`、`ANGAREION_*` 全數指向暫存根目錄，並於**同一行程**斷言 `agora.store.Paths.from_env()` 與 `angareion.config` 的設定／狀態／token 路徑及 `HOME`／`TMPDIR` 均位於該暫存目錄底下。
- 以 `angareion.cli.main` 注入 `FakeBackend`、`ChannelStore(FakeGit)`（預載 `team-mypmo-m1`）與套用 `registry_rules.rebuild_channels` 的 `FakeRegistryClient`，依 §4 慣例執行建立、送訊、收訊、第三者隔離驗證與可視邊界驗證。

原樣輸出如下：

```text
ISOLATION OK: root=/home/worker/.gemini/antigravity-cli/brain/61e86efa-22e4-4733-94f7-8f1739feda72/scratch/playground-582-724719 agora_state=/home/worker/.gemini/antigravity-cli/brain/61e86efa-22e4-4733-94f7-8f1739feda72/scratch/playground-582-724719/agora-state angareion_state=/home/worker/.gemini/antigravity-cli/brain/61e86efa-22e4-4733-94f7-8f1739feda72/scratch/playground-582-724719/angareion-state HOME=/home/worker/.gemini/antigravity-cli/brain/61e86efa-22e4-4733-94f7-8f1739feda72/scratch/playground-582-724719/home

$ ANGAREION_IDENTITY=mypmo-pm-m1 angareion channel create dm-secretary --backend aicompany-channel
exit=0
stdout:
dm-secretary

--- rebuilt channels.json ---
{
  "channels": {
    "dm-secretary": {
      "backend": "aicompany-channel",
      "issue": 100
    },
    "team-mypmo-m1": {
      "backend": "aicompany-channel",
      "issue": 42
    }
  },
  "version": 1
}

$ ANGAREION_IDENTITY=myaientry-secretary-p1 angareion send --channel dm-secretary --to mypmo-pm-m1 --body 請問 #582 進度？
exit=0
stdout:
01M8FZV9XQYF38RCZVBCBPQZMX

$ ANGAREION_IDENTITY=mypmo-pm-m1 angareion read --json
exit=0
stdout:
[
  {
    "id": "01M8FZV9XQYF38RCZVBCBPQZMX",
    "at": "2026-11-28T02:30:46.967746+00:00",
    "from": "myaientry-secretary-p1",
    "to": "mypmo-pm-m1",
    "channel": "dm-secretary",
    "urgency": 5,
    "content": "請問 #582 進度？",
    "attachments": [],
    "mentions": [],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:02Z"
  }
]

$ ANGAREION_IDENTITY=mylinuxpool-impl-w1-1 angareion read --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=mylinuxpool-impl-w1-1 angareion read --channel team-mypmo-m1 --json
exit=0
stdout:
[]
stderr:
Warning: --channel reads every message in the channel, including messages addressed to other agents; omit --channel to get only messages addressed to you.

$ ANGAREION_IDENTITY=mypmo-pm-m1 angareion send --channel dm-secretary --to myaientry-secretary-p1 --in-reply-to 01M8FZV9XQYF38RCZVBCBPQZMX --body 設計文件審核中。
exit=0
stdout:
01M8FZV9Y54RE160HNQJ8PVNQT

$ ANGAREION_IDENTITY=myaientry-secretary-p1 angareion read --json
exit=0
stdout:
[
  {
    "id": "01M8FZV9Y54RE160HNQJ8PVNQT",
    "at": "2026-11-28T02:30:46.981080+00:00",
    "from": "mypmo-pm-m1",
    "to": "myaientry-secretary-p1",
    "channel": "dm-secretary",
    "urgency": 5,
    "content": "設計文件審核中。",
    "attachments": [],
    "mentions": [],
    "in_reply_to": "01M8FZV9XQYF38RCZVBCBPQZMX",
    "datatype": "message",
    "received_at": "2026-11-28T02:00:08Z"
  }
]

$ ANGAREION_IDENTITY=mylinuxpool-impl-w1-1 angareion read --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=mylinuxpool-impl-w1-1 angareion log --channel dm-secretary --to me --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=mylinuxpool-impl-w1-1 angareion read --channel dm-secretary --json
exit=0
stdout:
[
  {
    "id": "01M8FZV9XQYF38RCZVBCBPQZMX",
    "at": "2026-11-28T02:30:46.967746+00:00",
    "from": "myaientry-secretary-p1",
    "to": "mypmo-pm-m1",
    "channel": "dm-secretary",
    "urgency": 5,
    "content": "請問 #582 進度？",
    "attachments": [],
    "mentions": [],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:02Z"
  },
  {
    "id": "01M8FZV9Y54RE160HNQJ8PVNQT",
    "at": "2026-11-28T02:30:46.981080+00:00",
    "from": "mypmo-pm-m1",
    "to": "myaientry-secretary-p1",
    "channel": "dm-secretary",
    "urgency": 5,
    "content": "設計文件審核中。",
    "attachments": [],
    "mentions": [],
    "in_reply_to": "01M8FZV9XQYF38RCZVBCBPQZMX",
    "datatype": "message",
    "received_at": "2026-11-28T02:00:08Z"
  }
]
stderr:
Warning: --channel reads every message in the channel, including messages addressed to other agents; omit --channel to get only messages addressed to you.

$ ANGAREION_IDENTITY=mypmo-pm-m1 angareion channel create dm_myaientry-secretary-p1_mypmo-pm-m1 --backend aicompany-channel
exit=0
stdout:
dm_myaientry-secretary-p1_mypmo-pm-m1

ALL CHECKS PASSED
```

---

## 6. 若未來要改程式的清單（本單不改程式）

建議案 (C) 採行時**不需要改任何程式**；僅在日後決定要加下列能力時才另開單修改：

1. **若要在註冊簿／`channels.json` 顯式加 `kind`（例如 `"kind": "dm"`）**：
   - `src/angareion/registry.py`：`parse_comment`、`register_comment`、`Action` 允許選填 `kind`（驗證合法值）。
   - `src/angareion/registry_rules.py`：`rebuild_channels` 將 `kind` 寫入（或省略預設 `"team"` 保持既有頻道逐位元組不變）。
   - `src/angareion/channel_config.py`：`ChannelRef` 與 `ChannelStore.channels()` 讀取並驗證 `kind`。
   - `src/angareion/channel_cli.py`：`cmd_channel_create` 增加 `--kind`（或依慣例帶入）。
   - `openspec/specs/angareion/channel-config/spec.md`：更新註冊簿 JSON 與 `channels.json` 規格。
2. **若要改採 (A)＋`datatype: dm`（0 新增實體的替代路徑）**：
   - `src/angareion/cli.py`（`build_parser`、`cmd_send`）與 `src/aicomm/cli.py`：新增 `--datatype` 選項（預設 `message`），否則 CLI 送不出 `datatype: dm`。
   - `src/angareion/messages.py`：若要明確宣告 `dm` 語意或防自我回傳，更新相關常數（現有 `DATATYPE_RE` 已允許 `"dm"` 寬鬆讀取）。
3. **若要替 `angareion log` 加雙向查詢便利旗標（可選 UX 改善）**：
   - `src/angareion/log_query.py`（`select`）與 `src/angareion/cli.py`（`p_log`）：新增 `--peer <AI 名>`（語意為 `(from == me and to == peer) or (from == peer and to == me)`），讓單一指令即可取回雙向 DM 串。

---

## 7. 風險與未決（Q-*）

### 已知風險
- **誤漏 `--to` 成為廣播**：若有人或腳本在 `dm-secretary` 忘記帶 `--to`，訊息會進入全公司所有 AI 的 `read` inbox。緩解：App N34 端寫死必須有合法 `to`；`skills/angareion/SKILL.md` 與 `skills/aicomm/SKILL.md` 於定案後加註「`dm-secretary` 嚴禁省略 `--to`」。
- **沒有後端 ACL**：同 `docs/angareion.md` 既有風險，任何能讀 `AiCompany-channel` 的隊員都可看 `dm-secretary` 的 GitHub issue 或用 `read --channel`／`angareion log` 讀出全部 DM。若未來真的需要機密級 DM，須另開私有 backend repo 與獨立 token（非本單範圍）。
- **`dm-secretary` 集中 2500 則上限**：所有 DM 共用一頁時會比每對一頁更快碰到 2400 換頁，但 #542 已實作自動換頁與 48 小時寬限期讀取（僅注意在 PR-3c 全面上線前需依 runbook 手動換頁，或確認用量速度）。

### 待決定（Q-*）

- **Q-1（本人／PM 裁定）**：DM 頻道架構採 **(C) 單一專用 DM 頻道 `dm-secretary`**（架構師建議）、**(B) 每對一頻道 `dm_<a>_<b>`**，還是 **(A) 不新增實體（既有頻道雙人視圖，若要區分需另開單加 `--datatype dm`）**？
- **Q-2（PM 決定）**：註冊簿與 `channels.json` 是否維持**零改碼**（靠固定頻道名 `dm-secretary` 或 `dm_` 前綴辨識），還是另開單在註冊簿與 `channels.json` 加 `kind` 欄？建議維持零改碼。
- **Q-3（PMO／本人決定）**：在 `dm-secretary` 傳給 `mypmo-pm-m1` 的高 urgency 訊息，是否維持現狀只當 `叫人`（`is_call`），急件仍走 `secretary-pmo`（`pmo.inbox.should_poke` 僅認 `dispatch_channel`）？建議維持現狀。
- **Q-4（PM／本人執行）**：若 Q-1 採 (C)，誰在上線前執行一次 `angareion channel create dm-secretary --backend aicompany-channel`（建議由 PMO 或本人執行），並同步將使用規則補進 `skills/aicomm/SKILL.md` 與 `skills/angareion/SKILL.md`（另案或交接給檔案維護者）。

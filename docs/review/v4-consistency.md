# v4 定案版的交叉一致性核對

> 2026-10-12，架構師。只讀不改；對象是 sidecar-v0（#31，本 worktree，HEAD `ab9cb6a`）、angareion-v0（#30）與 LLMGateway gateway-v0 的最終規格（main）。
> 行號都是當下檢出的版本。`S:` 前綴＝sidecar-v0 的 `openspec/changes/sidecar-v0/`，`A:` 前綴＝angareion-v0 的 `openspec/changes/angareion-v0/`，`L:` 前綴＝LLMGateway 的 `openspec/changes/gateway-v0/`。

## 0. 結論

- 兩個 change 大方向忠於 v4 與 Q1～Q8：sidecar 對外只有四個動作（加上 v4 本來就有的 `/jobs` 管理與 `--version`／`run` 指令列旗標，沒有多出本人沒要的）；Angareion 只有四個介面；兩邊的程式與介面互不相依。
- 最大的洞在三個接點：**sidecar ↔ LLMGateway 的錯誤處理**、**sidecar ↔ agora 的 `--headless`（新 Session id、tag、停下與寫回）**、**額度用完的判讀（Claude 的 `status` 值與 opencode 的窗口）**。這些都會讓實作者卡住或做出和設計不一致的東西。
- 另有一個會真的出錯的行為缺口：**換把或自動重開之後，agent 是閒著的，沒有人叫它繼續**（見 Q1）。
- 必須改 9 項（M1～M9）、建議 9 項（S1～S9）、要 PM 決定 8 項（Q1～Q8）。

---

## 1. 必須改

### M1　sidecar 沒有定義 LLMGateway 的 403／404／409／unreachable 怎麼處理（B）

- 位置：`S:design.md:43`（open 流程）、`S:design.md:46-48`（換把）、`S:design.md:82`（只說 unreachable＝「下次再試」）、`S:specs/sidecar/spec.md:11,54`。
- 現況：設計只說 `get` 然後 launch；LLMGateway 的 `get` 有 400／403 `agent_not_allowed`／404／409 `not_deployed`（`L:specs/subscription-registry/spec.md:40`），`lgw.client` 連不上回 `unreachable=True`（不丟例外）。sidecar 這邊沒有任何一條說明：回給控制者什麼、換把途中遇到怎麼辦。
- 建議的改法：在 D4 加一張表，spec 補對應 Scenario。
  | 情況 | open（控制者看到的） | 換把途中 |
  |---|---|---|
  | `get` 403 `agent_not_allowed`（呼叫方指定的 subscription 不支援這個 agent） | 400 `subscription_not_allowed`，什麼都沒開 | 不會發生（`Selector` 已過濾 `agents`）；發生就當 409 的處理 |
  | `get` 404 | 400 `subscription_not_found` | 同上 |
  | `get` 409 `not_deployed` | 409 `not_deployed`（呼叫方指定時）；`Selector` 挑的就換下一個候選，全部不行才回 409 | 換下一個候選；沒有就走「全部冷卻」那一支（M3） |
  | unreachable（`list`／`get`／`cooldown` 任一個） | 503 `gateway_unreachable`，什麼都沒開、不掛 job | 見 M2、Q4 |
  - 404 刻意不用在 subscription 不存在上：`/agents/{id}` 的 404 已經是「沒有這個 agent」，兩種混在一起控制者分不出來。
  - 候選的順序要明寫（`priority` 小的先；相同時以 `id` 排，結果才是確定的）。

### M2　換把可能原地打轉：`cooldown` 失敗後 `list` 仍回同一把 `available`（B）

- 位置：`S:design.md:46`。
- 情境：driver 回報用完，`cooldown` 因為 `resets_at` 在過去（時鐘偏差、已重置）或超過 8 天而被 400 拒絕（`L:specs/subscription-registry/spec.md:58`），或 unreachable → 冷卻沒登錄 → `list` 裡這一把仍 `available` → `Selector` 又挑到它 → 重開 → 又用完 → 無限循環並一直燒 `get`。
- 建議的改法（D4 補三條）：
  1. 一次換把的過程中，**把剛用完的 id 排除在候選之外**，不依賴 `list` 的 `available`。
  2. `cooldown` 的 `resets_at` 轉成 RFC 3339 UTC（Claude 是 epoch 秒、opencode 是 epoch 毫秒；兩個都要在 driver 的出口轉好）。被 400 拒絕時**去掉 `resets_at` 重送一次**（用窗口預設長度）；仍失敗就只靠第 1 條。
  3. `resets_at` 選填：driver 拿不到時間就**不帶欄位**，不要自己猜一個。
- spec 的「額度用完自動換把」（`S:specs/sidecar/spec.md:54`）補 Scenario：「`cooldown` 登錄失敗時不會再挑回同一把」。

### M3　自動重開 job 的時刻（`at`）定義不精確，也沒有「沒有候選」與「到時還是挑不到」的處理（C）

- 位置：`S:design.md:48`、`S:specs/sidecar/spec.md:54,62`、`docs/proposals/aicompany-design-v4.md:120`。
- 「最早解除冷卻的時刻」要寫成算式，否則實作者會各寫各的：
  - 候選＝`agents` 含這個種類、**key 已部署**的 subscription（`available` 為 false 但兩個 `cooldown` 都是 null 的，就是 key 沒部署，不是冷卻，不能算進去）。
  - 每一把的解除時刻＝它兩個窗口 `cooldown.*.until` 中**非 null 的最大值**（`available` 要兩個窗口都沒在冷卻，`L:specs/subscription-registry/spec.md:16`）；`until` 已含 LLMGateway 的 +2 分鐘，不必再加。
  - `at`＝各候選的解除時刻取最小值。
- 補三個邊界：
  1. 沒有任何候選（全都沒部署，或這個種類根本沒有 subscription）：**不掛 job**，狀態 `waiting-quota`、`reopen_at: null`，讓控制者決定。
  2. job 到時 `open` 失敗（挑不到、unreachable、409）：重掛一次新的 `at`（重新算）；unreachable 時用固定間隔（Q4）。否則 agent 永遠卡在 `waiting-quota`，而且沒人知道。
  3. 控制者手動 `DELETE` 了這個 job：`status` 的 `reopen_at` 要變回 null（否則顯示一個不會發生的時刻）。
- 另外：`Selector` 是可替換的（Q8），但「最早解除」是 sidecar 本體在算；換成別的挑法（例如要看 `models`）時，這個時刻可能不對。寫明「這個時刻只看 `agents`／`cooldown`，不經 `Selector`」就好。

### M4　sidecar ↔ agora 的 `--headless` 接點有五個沒定義的地方（C、D）

位置：`S:specs/session-continue/spec.md:4-12`、`S:design.md:34,47,53-54,69,76`。

1. **fork／merge 出來的新 Session id 怎麼交給 sidecar**：現有 `continue --fork` 是在 stdout 印 id（`openspec/specs/session-continue/spec.md` 的「打開就離開」），但 `--headless` 的 stdout 屬於 agent（`S:specs/session-continue/spec.md:4`）。`status.session` 與「換把 continue 同一個 Session」都需要這個 id。要定一個管道（例如 stderr 的第一行 `[agora] session <id>`，或 `--session-id-file <path>`），並寫 Scenario。
2. **tag 會蓋掉原有的 tags**：`--header tags=[sidecar:t1]` 走 `overlay`，**list 是整個取代**（`src/agora/header.py:135-143`）；fork 會把原 Session 照抄的 tags 蓋掉，不 fork 的 continue 還會直接改原 Session 的標頭。設計寫「加上 `sidecar:<id>`」（`S:design.md:54`、`S:specs/sidecar/spec.md:11`）但沒有哪個 agora 介面能「附加」。二選一：(a) agora 加 `--add-tag`（`session-continue` delta 補 Scenario，保證原 tags 不變）；(b) sidecar 先讀原標頭的 tags 再合併給 `--header`。前者較乾淨。另外要寫清楚「不 fork 的 continue」時 tag 的語意（會加在原 Session 上）。
3. **停下與寫回的信號**：`S:design.md:34`（`sync` 由 continue 定期與結束時做）與 `S:design.md:47-48`（換把與全部冷卻時 sidecar 呼叫 `SessionStore.sync`）互相矛盾；而 `session-continue` delta 沒有說 agora 行程收到信號會怎樣。要寫成：
   - 溫和停＝對 agora 行程送 SIGTERM（或關 stdin），agora 轉給 agent、等它結束、做最後一次寫回、放鎖後退出；
   - 逾時才強制：SIGKILL agora 等於**不寫回、可能留鎖**，要寫出這個後果與清鎖的做法（spec 的「同一台機器不能同時接續」鎖，`src/agora/cli.py:1291`）；
   - D4 的「`stop` → `sync`」改成「等 agora 行程退出（它在結束時寫回）」，`SessionStore` 的 `sync` 若不是真的方法就從 D3 表上拿掉，免得測試要做一個假的。
4. **opencode 沒有 Scenario，也沒說埠怎麼來**：`S:design.md:69`（`--port <埠>` 由誰給）、`S:design.md:76`（「agent 印出的埠」）與 `S:specs/session-continue/spec.md:4`（agora 不寫 stdout）對不起來。要定：埠由 sidecar 分配（避免撞埠）並以 `--headless-port` 之類的參數交給 agora，或 agent 啟動後 agora 在 stderr 印一行；spec 補一個 opencode 的 Scenario。
5. **誰加 `--settings '{"autoContinueAtUsageLimit": false}'`**（`S:design.md:61`）與 sidecar 的 `agent=claude-code` 對 agora 的 `--agent claude` 的對應（`S:specs/session-continue/spec.md:6` 用的是 `claude`）：寫進 session-continue delta（agora 的 headless 一律帶這個設定），並在 `AgoraSessionStore` 寫對應表。

### M5　Claude Code「`status` 不是 `allowed` 就是用完」的判讀太寬，而且 api-key 類沒有這個事件（B、D）

- 位置：`S:design.md:67`。
- 設計自己承認實際值沒確認。Claude Code SDK 的 rate limit 資訊型別，我印象中 `status` 有 `allowed`／`allowed_warning`／`rejected` 三種（**需要用你們的 2.1.296 確認，我沒有實測**）。如果有 `allowed_warning`，「不是 `allowed`」會在還沒用完時就觸發換把，而且是每一輪都觸發。改成「`status` 等於 `rejected`」或「不在已知的非用完值之中且伴隨輔助訊號」二選一，並且（Q6）要有個保守的未知值處理。
- `rateLimitType` 只對應了 `five_hour`→`5h`、`seven_day`→`1w`；其他值（我印象中還有 `seven_day_opus`、`seven_day_sonnet`、`overage` 之類）沒有規則。要寫：未知類型 → 以 `5h` 登錄、不帶 `resets_at`、log 一行（不含 key）。
- kind 是 `api-key`（例如 OpenRouter 經 `ANTHROPIC_BASE_URL`，`L:design.md:150`）時**不會有 `rate_limit_event`**，只剩 `system/api_retry`（`error: rate_limit`）與 `result` 的文字。設計把它們當「輔助訊號」（`S:design.md:67` 結尾），對 api-key 類它們其實是**唯一**訊號，也沒有窗口與重置時刻。要明寫這一支：窗口 `5h`、不帶 `resets_at`（用預設 5 小時），或改用 `Retry-After`（若有）。

### M6　opencode 的窗口對應：自相矛盾，而「額度類」沒有列舉（B）

- 位置：`S:design.md:74`。
- 同一句話前半「依 subscription 的種類決定登錄 `5h` 或 `1w`」，括號裡又是「距離重置超過 5 小時就當 `1w`」，是兩套規則。
- 回答你問的「這合理嗎」：**合理，可以留，但要改成明確的規則**。
  - 5h 窗口的重置時刻不可能超過 5 小時以外，所以「超過 5 小時」只能是 1w，這一邊是對的。
  - 反過來「不超過 5 小時」不保證是 5h（1w 的最後幾小時也會落在這裡），但**標錯幾乎無害**：冷卻是以 `until` 為準，窗口名稱只影響「沒給 `resets_at` 時的預設長度」與「同一窗口取較晚的」兩件事；`resets_at` 有給的情況下兩者結果一樣。
  - 邊界要有容差：時鐘偏差與 `next` 的計算時間可能讓 5h 的窗口變成 5h00m02s。建議「`next` 距現在大於 5 小時加 5 分鐘才算 `1w`」。
  - 沒有 `next`（或不是時間）：窗口 `5h`、不帶 `resets_at`。
- 「額度類」要列舉：目前只觀察到一個 `free_tier_limit`（`S:design.md:74`）。`session.status` 的 `retry` 也用在暫時性錯誤（連線、過載），那些 `next` 只有幾秒；只靠「是 `retry`」會誤觸發。建議白名單：`action.reason` 在白名單內才算；不在白名單內的 `retry` 只更新 `state`（維持 `running`）並 log，不換把。白名單放設定（D7），讓之後只改設定就能加。

### M7　四處過時或不一致的指標（C）

| 位置 | 問題 | 改法 |
|---|---|---|
| `S:design.md:49` | 欄位表指向「提案 v3 第 2.4 節」，但 source of truth 是 LLMGateway 的 `specs/subscription-registry` | 改指 `L:specs/subscription-registry/spec.md`（含 `schema: 1`；sidecar 遇到更高的 `schema` 要拒絕並說明，不要默默解析） |
| `S:design.md:34` vs `:47,53` | `SessionStore` 的動作一處叫 `start`，兩處叫 `prepare`；v4 與 v3 都是 `prepare` | 統一（和 M4-3 一起處理 `sync`） |
| `S:proposal.md:20,30` | 「細節由 spike 決定」「spike 之後定，可能補 delta」；spike 已做、delta 已經寫了（`specs/session-continue/spec.md`） | 改成已定案的敘述 |
| `docs/proposals/aicompany-design-v4.md:132` | Q5 仍寫「只綁本機＋控制 token」，本人答案是「只綁本機、不做認證」 | 加一節「本人的回答（10-11）」把 Q1～Q8 的答案寫進去，或直接改 Q5 的敘述；以後的人讀 v4 才不會再問一次 |

### M8　Angareion 的設計越過 Q6 的界線，而且提到了 sidecar 的 branch（A）

- `A:design.md:49`：同一個條目前半寫「Angareion 不寫死任何路徑」，後半就寫了 `/run/mlp-secrets/<GitHub secret 名>`、0400、uid 1000——這是 MyLinuxPool 的規則，不是 Angareion 的。Q6 只授權「定 token 檔的位置與讀法」。
- `A:proposal.md:10,21,38` 與 `A:design.md:55`：寫了 MyLinuxPool「建映像時用 tool-pins 的同一個 sha 取 `skills/angareion/SKILL.md`、放到 `~/.claude/skills/...`」。那是 MyLinuxPool 能力要做的事，Angareion 不該規定別的專案怎麼做。
- `A:proposal.md:3`：「見 `docs/proposals/aicompany-design-v4.md`，在 #31 的 branch」——提到了 #31，違反「兩邊互不提及」。
- 建議：
  1. `design.md` D5／D6 與 `proposal.md` 的 MyLinuxPool 句子整段搬到 `docs/angareion.md` 的「給 MyLinuxPool 的需求」一節（交接資料，不是設計決定）；design 只留「token 檔的位置依序是 …、權限 0400／0600、worker 上由外部以 `ANGAREION_TOKEN_FILE` 指定」。
  2. `proposal.md:3` 改成引用 MyPMO 的決定紀錄或 v4 在 main 上的路徑，不寫 #31。
  3. `A:design.md:44` 的 token 位置三層（`ANGAREION_TOKEN_FILE`→設定→預設）可以留，但 spec 只需要要求第一層；其餘兩層說成「實作細節」比較不會被視為本人沒要的選項。

### M9　Angareion 的游標與 `wait` 的 ETag 放在一起，會破壞「`wait` 不動游標」（A）

- 位置：`A:design.md:33,44`、`A:specs/angareion/spec.md:11,22`。
- `D4` 說游標內容含「最後讀到的時刻與 ETag」；`D2` 說 `check` 用 ETag 條件式請求；spec 說 `wait` 期間 MUST NOT 改動游標。如果 `check` 把新 ETag 寫回游標，下一次 `read` 就會因為 304 而漏讀。
  - 改法：`check` 的 ETag 只放在**這次 `wait` 行程的記憶體**；游標只存「最後讀到的 comment id（與時間）」。
- GitHub 的 `since` 是秒級、含邊界，而且是依 `updated_at`（留言被編輯也會再出現）。游標只存時間會重複讀或漏讀同一秒的多則留言。游標要含**最後一則的訊息 `id`（ULID）**，`read` 依 `id` 去重；spec 補兩個 Scenario：同一秒兩則都讀到、被編輯的留言不重複出現。

---

## 2. 建議

- **S1　一次換把用同一個 `with lgw.connect`**：`S:design.md:82` 的「每次操作才連、用完就關」，換把是 `cooldown`→`list`→`get` 三次，各開一條 SSH 轉發會慢，且三次之間的狀態不是同一個快照。建議以「一次邏輯操作（open／一次換把）」為單位包一個 `with`，仍是不長期持有。
- **S2　spec 的 Scenario 不夠實作者寫測試**（C）：`S:specs/sidecar/spec.md` 缺：
  1. open 不給 subscription（`Selector` 挑）與給了但 403／404／409（M1）；
  2. open merge（M4、Q3）；
  3. `waiting-quota` 時控制者手動 `open` 同一個 id，順帶把自動 job 刪掉（`S:design.md:17`）；
  4. `poke` 對不存在或已 exited 的 id（Q5）；
  5. job：`every`／`until`、`DELETE`、`action` 不是四個之一時被拒、`at` 在過去；
  6. `status` 在 `waiting-quota` 時有 `reopen_at`，且沒有 key；
  7. 只綁 127.0.0.1（測 bind address）；
  8. 環境變數不出現在命令列與 log（`S:tasks.md:14` 提到但 spec 沒有）。
- **S3　`workdir` 的規則**（D 的問題 1）：`open` 本來就有 `workdir`（`S:design.md:17`，v4 也有），沒有「sidecar 不知道工作目錄」的洞；缺的是規則：必須是存在的絕對路徑，不存在回 400（sidecar 不建目錄）；`continue`／`fork` 用 agora 的 `--dir`（`src/agora/cli.py:1300`）傳下去；自動重開沿用同一個。寫進 D1。
- **S4　`status` 的欄位**：`S:design.md:19` 比 v4 §2.2 多了 `starting` 與 `exit`，沒問題，但請在 `docs/proposals` 或 spec 說明 `session` 是「目前跑的那個 agora id」（fork／merge 之後是新的那個，換把後不變）。
- **S5　`Selector` 的細節**：同 `priority` 時依 `id` 排序；輸入是整份 `list`（含不可用的）還是只給可用的，要寫（目前 `S:design.md:36` 兩個都說得通）。要在設定裡留一個 `selector` 的鍵嗎：Q8 說「可替換」，目前只能改程式；先不加也行，但請在 D7 說「v0 只有 priority，替換靠改程式」。
- **S6　opencode 的 env**：`L:design.md:211` 把「opencode 讀 key 與 baseURL 的方式」交給 sidecar 的 `AgentDriver`，在 opencode 1.18.35 驗證。sidecar 的 spike（D6）只驗了生命週期與額度，沒有驗 env_for；tasks 3.2 應該寫上這一項驗證，Mac 上的 3.4 才有東西驗。
- **S7　第一次真實用完要把 raw 事件記下來**：M5 的未知值問題，在實際用完那一次最有價值。driver 遇到任何 `status`／`reason` 的未知值時，把該事件（去掉 key 與任何 token）寫進 log，並在 tasks 3.4 加「找一次真的用完把事件貼回 D6」。
- **S8　sidecar 提到 Angareion 的地方**：`S:design.md:9`、`S:proposal.md:10`、`S:specs/sidecar/spec.md:72` 都是「不依賴」。v4 §2.1 只要求 Angareion 的設計不提 sidecar；sidecar 這邊作為負面敘述可以留。若 PM 要「兩邊真正互不提及」，這三處只需改成「不依賴任何信道工具」。
- **S9（Angareion 的 spec）**：缺 `channel create` 的 Scenario、「寄給我的」只讀到 `to` 是自己的（`A:specs/angareion/spec.md:21-26`）、`wait` 不給 timeout 的行為、後端錯誤 exit 2、群組位址被拒的 exit 1（`A:specs/angareion/spec.md:29` 有規則，沒有 Scenario）。

---

## 3. 要 PM 決定的

| # | 問題 | 影響 | 我的建議 |
|---|---|---|---|
| **Q1** | 換把或自動重開之後，agent 是閒著的（Claude 的行程在等 stdin；opencode 的 session 在 idle），沒有人叫它繼續剛才被打斷的那輪。`open` 的 `input` 是選填，自動重開的 job 也沒帶 `input`（`S:design.md:48`） | 不決定的話，兩種換把與重開都會「接上了但什麼都不做」，要等控制者看到 `status` 再 `poke` | sidecar 在換把與自動重開成功後，自動送一則固定提示（文字放設定 `resume_prompt`，預設「額度恢復了，請接著剛才的工作」）；`open` 原本的 `input` 不重送（它可能是一次性指令）。控制者不想要時 `resume_prompt` 設空字串 |
| **Q2** | 呼叫方指定的 subscription 正在冷卻（`available: false`，`get` 照回，`L:specs/subscription-registry/spec.md:50`）時，`open` 要照辦還是拒絕？ | 控制者指定的 id 與 `Selector` 的結論不一致 | 照辦（呼叫方指定了就是指定）；一開起來馬上用完就走正常的換把。拒絕會讓「定時 open 某一把」多一個失敗的原因 |
| **Q3** | `merge` 要誰寫要約？`agora merge` 必須給 `--agent`（`src/agora/cli.py:504`）而且會呼叫模型（用的是該機器上的 agent 登入，不是 LLMGateway 的 subscription） | 不指定就會用到不受控的額度；`open` 現在沒有指定摘要者的欄位 | `session` 加選填 `by`（預設＝`open` 的 `agent`），sidecar 先挑 subscription、`get`、再用同一個 `env_for` 的 env 跑 `agora merge`，最後 `continue --headless`。若覺得太複雜：v0 把 `merge` 拿掉，由控制者先自己 `agora merge` 再 `continue`（四個動作更少一個模式，v4 §2.2 有列所以要本人同意） |
| **Q4** | LLMGateway 連不上（unreachable）時的重試政策 | M1／M3 的 unreachable 一支沒有答案 | open：503，不重試（控制者自己決定）。換把或自動重開：`stop`＋寫回、`waiting-quota`、掛一個 `now + 5 分鐘` 的 open job，直到成功；間隔放設定 |
| **Q5** | `poke` 對 `starting`／`waiting-quota`／`exited`／不存在的 id 回什麼 | 實作者會各自決定 | 不存在 404；`exited`／`waiting-quota` 409（不排隊）；`starting` 排到起來之後再送（有逾時），逾時 409 |
| **Q6** | Claude 的 `status` 未知值的保守處理（M5） | 不能刻意用完來確認實際值 | 只認 `rejected`；未知值當「沒用完」並 log 原事件（S7）；第一次真實用完後回補 D6 |
| **Q7** | 排程的 job 執行失敗（例如 `every` 的 `open` 撞 409、`at` 的 open 挑不到）結果在哪裡看 | 現在沒有地方可看，控制者只能猜 | `GET /jobs` 每筆加 `last_run`、`last_error`（只放錯誤碼與訊息，不放 key）；不自動重試，除了 M3 的自動重開 job |
| **Q8** | agora 的 tag 附加：agora 加 `--add-tag`（M4-2 的 (a)），還是 sidecar 先讀原標頭（(b)） | 前者要多一個 agora 的小改與測試，後者要多一次 agora 呼叫且有競態 | (a)；agora 的 delta 已經是這個 change 的一部分，順便做 |

---

## 4. 核對過、沒有問題的

- **Q1**（sidecar 依 id 自己 `get`）：`S:design.md:43`、`S:specs/sidecar/spec.md:11` 一致，key 不經過控制者。
- **Q4**（程式化模式、已 spike）：`S:design.md:57-76`、`S:tasks.md:1-5` 一致。
- **Q5**（只綁本機、不認證）：`S:design.md:23`、`S:specs/sidecar/spec.md:47` 一致（過時的只有 v4 文件本身，見 M7）。
- **Q6**（Angareion 只定 token 檔的位置與讀法）：`A:specs/angareion/spec.md:42-51` 符合；越界的是 design／proposal 的敘述（M8）。
- **Q7**（tag）：`S:design.md:54`、`S:specs/sidecar/spec.md:11` 有留；實作缺口在 M4-2。
- **Q8**（`Selector` 可替換、預設 priority）：`S:design.md:36`、`S:specs/sidecar/spec.md:72` 一致。
- **只有四個動作**：sidecar 的 `/agents`（四個動作）加 `/jobs`（v4 §2.2 本來就有）；`sidecar run`、`--version` 是指令列旗標；健康檢查用 `GET /agents`（`S:design.md:84`），沒有多端點。Angareion 的四個指令加後端的 `check` 在 `wait` 內部，對外仍是四個。
- **欄位名**：sidecar 用到的 `agents`／`priority`／`available`／`cooldown.{5h,1w}.until`／`id`／`key` 都和 `L:specs/subscription-registry/spec.md:9-16` 一致；`agent` 的值 `claude-code`／`opencode` 一致；`cooldown` 的 `window` 只有 `5h`／`1w` 一致；沒有環境變數名在共通 JSON 裡一致。
- **`resets_at` 選填、冷卻中的 `get` 照回**：LLMGateway 的行為已知道；sidecar 這邊的對應動作見 M2、Q2。
- **D1 的 409／重開規則、subscription 選填、先停再寫回、自動重開、狀態揮發**：在 proposal、design、spec、tasks 四處的敘述一致（矛盾只有 `sync` 的角色，M4-3）。
- **相依方向**：sidecar → agora、sidecar → LLMGateway（`gateway` extra，固定 tag，`S:design.md:82`）；Angareion 沒有任何對外相依；v4 §1.2 的「沒有」那一行都滿足。

## 5. 給 LLMGateway PM 的註記（不是這兩個 change 的問題）

- `L:design.md:6` 還寫「sidecar v3：對外五個操作」，應為 v4 的四個動作。
- `L:design.md:79`：worker 上 connect 設定由 sidecar 從 `/run/mlp-gateway/gateway.json` 填入；`S:design.md:78-82` 沒有提到這件事（sidecar 的設定只有 `subscription_source`＋連線設定，說「憑證由 `lgw connect` 自己處理」）。這是一個隱含的 sidecar → MyLinuxPool 檔案相依，要在 sidecar 的 D7 寫明（讀這個檔、欄位對應、Mac 上用自己的設定），或由 MyLinuxPool 在 sidecar 的設定裡直接寫入——PM 決定後兩邊對齊。
- `L:design.md:163`：`lgw.signals` 給 AgentDriver 當函式庫用；sidecar 的 driver 看到的是 CLI 事件而不是 HTTP 回應，多半用不到。`S:design.md:82` 把 `signals` 列在 extra 裡，請兩邊確認是不是真的要用，不用就在 sidecar 的說明中拿掉，免得實作者以為有責任用它。

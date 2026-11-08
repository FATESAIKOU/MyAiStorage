# #439 設計筆記：角色式 session 召回（M6）

- 單：#439（MAS）；承接 #205（c，狀態:構想）。作者：設計作者（隊員，claude-sonnet-5-5）。2026-11-08。架構師第 1 次退回（`ARCH-439-review.md`），已修 M-1～M-3、處理 S-1～S-4（見 §9）。
- 範圍：只設計，不寫程式、不改 openspec。sidecar 在別的 repo（MyLinuxPool），本 note 只寫「對 sidecar 的要求」，不碰它的程式。
- 本人原則：介面簡單、不加多餘機制。

## 1. 現況

**#205 的原話**（`gh issue view 205`）：用 agora header 的自由欄位（tag）做「人↔session」關聯；一個人有很多 session，關聯分「主要擔當」與「旁人（協作）」，同種關聯有時序；agent 啟動時要能參照。相關：MyPMO#3（期 4 M2）、MyLinuxPool#340（sidecar 同名再開時接回對話的鍵）。

**agora 已經有的零件**（都不用新寫）：

| 零件 | 位置 | 行為 |
|---|---|---|
| 自由 tag | `docs/design.md:100-107`、`src/agora/header.py:209`（`LIST_KEYS`） | `tags` 是 header 的清單欄位，使用者可寫 |
| 附加 tag | `src/agora/cli.py:195-225`（`_with_user`、`_add_tags`）、`cli.py:2841`（`ADD_TAG_ACTIONS = import／continue／edit`） | `--add-tag X` 附加、保留原有、去重；可重複 |
| 依 tag 找 | `header.py:238`（`parse_filters`）、`src/agora/store.py:2857`（`_matches`，清單欄位逐項比對）、`docs/design.md:263` 範例 `--filter tags=ai:claude-work --json` | `--filter tags=<全等>` 對 tags 逐項比；多個 `--filter` 是 AND |
| 排序、筆數 | `cli.py:432-498`（`cmd_search`）、`store.py:2803`（`Index.search(sort=…)`）、`docs/design.md:240-245` | `--sort updated`＝依 `agora.updated_at` 新到舊，同值 ULID 新到舊；`--limit N`；`--json` |
| 機器可讀列 | `cli.py:500-523`（`_search_json_row`）、`docs/design.md:255-262` | 有 `id`、`agent`、`tags`、`updated_at`、`locked`、`locked_by`、`missing_in_cloud` |
| 接續 | `cli.py:2201`（`cmd_continue`）；`openspec/specs/session-continue/spec.md` | 同 agent 原地寫回同一個 Session（沿用 agent 內部 session id，`docs/design.md:353`）；`--fork` 才分岔；`--dir` 指定工作目錄，沒給就用來源記的目錄（`cli.py:~2252`） |
| 同機互斥 | `cli.py:2253-2258`、`store.py:2880`（`continuing`）、spec「同一台機器上不能同時接續同一個 Session」 | 不含 `--fork` 的接續，同機已在接續就 exit 4＋`agora-locked <id>` |
| 被刪／跨 agent | `cli.py:348`（`_refuse_if_gone`）、`cli.py:403`（`_refuse_cross_agent_source`，目前只擋 agy 與非 agy 互換） | 來源被刪就拒絕；claude↔opencode 可跨 agent 接續（轉換成新 Session） |
| 匯入時打 tag | `import --add-tag`（同 `ADD_TAG_ACTIONS`） | 把剛結束的 agent session 匯入並帶 tag |

**缺的**：沒有任何「角色」的約定；沒有人規定 tag 長什麼樣；sidecar 重開同名 agent 時不知道上次是哪個 session（MyLinuxPool#340 要的鍵就是 agora session id，`open` 的 `session` 已在收它，缺的是從角色推出它）。我在本 repo 找不到 sidecar 呼叫 agora 的程式或文件（只在 `docs/aicomm.md` 看到 sidecar 的叫醒介面，無關）；
**sidecar 已經會用 agora 接續**（本 repo 內的證據）：`docs/design.md:382`、`:394`（呼叫方 sidecar 靠 exit 4／`agora-locked` 判 409）、`cli.py:2238-2242`（`--headless-port` 由 sidecar 分配）；worker 環境說明：sidecar `open` 帶 `session` 會走 agora，舊 agora 回 409 `agora_too_old`。所以 `open` 已有「帶 agora session id 就用 `agora continue … --headless` 起」這條路；本單只補「從角色推出這個 id」。sidecar 程式本身（MyLinuxPool）我沒讀。

## 2. 角色→session 對應鍵與存放位置

**結論：用一個約定好的 tag，存在 Session header 的 `tags` 裡；不新增欄位、不新增檔案、不新增指令。**

- 鍵＝角色名：`role:<ANGAREION_IDENTITY>` 原字串，例如 `role:mylinuxpool-pm-w2`、`role:myaistorage-arch-w1-1`。召回只給長期角色（§6 U-6），它們的名字本來就固定，不必另定「去序號」的推導，sidecar 也不必解析身分格式；代理接手時沿用同一個名字。
- 值是字串全等比對（`--filter tags=role:mylinuxpool-pm-w2`），不需要新的比對方式。
- 存放：Session 自己的 header（隨 Session 上 Drive，換機器也在）。**不**另建「角色→session」對照表——對照表是第二份真相，會和 Session 不同步。
- 「最近的一個」就是時序：同一個 `role:` tag 下，`--sort updated --limit 1`。時序不另存。
- 為什麼不用 header 的其他欄位：`case`／`refs` 是指向實體的 ref，語意不合；`agora` 區塊是系統欄位，使用者不能改（`header.py:SYSTEM_KEYS`）；`--header key=…` 加新頂層欄位（例如 `role: …`）也行，但 `--filter` 要靠 `tags` 才有「清單逐項比」的現成語意與 `--add-tag` 的附加語意，用 tag 零改動。

### 主要擔當／協作（#205 的兩種關聯）

#205 提了兩種。**本期只做「主要擔當」**（`role:<角色>`）。協作（旁人）要有的話，同樣用 tag，另一個前綴（`with:<角色>`）即可，不需要機制改動，但「召回時要不要把協作 session 也接上」是產品決定，見 §6 U-1。這樣介面只有一個前綴，需要時再加第二個。

## 3. 重開時的查找順序

由啟動方（sidecar）在 `open` 帶 `role`（沒帶 `session`）時，在開 agent 之前跑；輸入是這個 agent 的 `role:<身分>`，記為 `R`：

1. **找**：`agora search session --filter tags=role:R --sort updated --json`（離線或逾時才加 `--no-sync`）。
2. **挑**：取**最新的合格者**，只留這一條規則：`missing_in_cloud` 不是 true，且 agent 接得了（同 agent，或 claude↔opencode；agy 只能接 agy，`cli.py:403`）。不做「同 agent 優先」，也**不過濾 `locked`**。
3. **接**：把挑到的 id 當成 `open` 的 `session`，走 sidecar 現有帶 `session` 的 continue 呼叫（含它現在帶的 `--headless`、`--headless-port`、`--harness-file`、`--agent-arg` 等），**再加 `--add-tag role:R`**。對已有的 tag 是 no-op；跨 agent 接續產生的新 Session 是否照抄 tag **未驗**，所以一律再帶一次。
4. **最新的被鎖**：`agora continue` 回 exit 4＋`agora-locked <id>`，sidecar 現有就把它當 409（`docs/design.md:382`、`:394`）。**沿用，不往下挑舊的**：鎖住代表這台機器上同一個角色已經在跑，再開一個會讓同一身分同時有兩個 agent（違反「同一個身分只留一個讀者」），記憶也分成兩條線。
5. **沒找到**（第一次、列表為空、全部被刪）→ 開全新的 agent。agent 結束後 `agora import … --add-tag role:R` 記下，之後找得到。
6. **查找本身失敗**（Drive 連不上、agora 非 0 且不是「沒找到」）→ 見 §6 U-2（建議回錯誤給 PM，不靜默開全新的）。

## 4. 與 agora continue／sidecar open 的分工與最小介面

**agora 側（本 repo）：不新增指令、旗標或 header 欄位。** 只把「`role:` tag 的約定」寫進 `docs/design.md`（header 一節）與 `session-continue` spec 的一條 Scenario（§7）。

**sidecar 側（MyLinuxPool，別的 repo，只提要求、不實作）：**

- `open` 已有「帶 `session` 就用 agora 接」的路。本單只多一件事：`open` 帶 `role`、沒帶 `session` 時，先跑一次「找」，把結果當成 `session` 走現有的路；沒找到就照現在的方式開全新的。**不新增第二套起 agent 的方式。**
- sidecar 不存「角色→session」，每次問 agora（Drive 是真相）。
- 結束後的「記」由 sidecar 在退出流程做（§6 U-4）。

**最小介面（約定，只有一條新指令用法）：**

```
找：agora search session --filter tags=role:<R> --sort updated --json     # 新用法，不是新功能
接：（sidecar 現有帶 session 的 continue 呼叫）… --add-tag role:<R>        # 多一個既有旗標
記：agora import --agent <a> --external-session-id <sid> --add-tag role:<R> # 同上
```

## 5. 不做什麼

- 不新增 `agora recall`／`agora role` 之類的指令；不新增 `--role` 旗標。
- 不新增 header 頂層欄位、不碰 `agora` 系統區塊。
- 不建角色→session 的對照檔或資料庫；sidecar 不存對照。
- 不做 #205 的「協作」關聯與跨角色的脈絡共享（§6 U-1）。
- 不自動摘要、不自動合併多個舊 Session 當記憶（`agora merge` 另一條路，不在這張單）。
- 不改 `agora continue` 的原地寫回／fork 規則、不改同機互斥。
- 不做權限控制：tag 是自由欄位，誰都能寫 `role:別人`。這是約定，不是授權（私人 Drive 的信任範圍同現況）。
- 不解決 sidecar 的 `open` 內部流程；那是 MyLinuxPool 的設計。

## 6. 未決項

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| U-1 | 協作（旁人）關聯要不要做 | (a) 本期不做，只 `role:`；(b) 加 `with:<角色>` 前綴，召回時只當唯讀參考列出，不接續 | (a)。#205 本人說「先設計」，且最小介面原則；(b) 需要「怎麼把協作 session 餵給 agent」的產品決定，不是 tag 的問題 |
| U-2 | 查找失敗時 | (a) 開全新 agent＋警告；(b) sidecar 回錯誤，由 PM 決定（PM 真要開就不帶 `role` 重開） | **(b)**。(a) 的長期後果：退出時的「記」會把這個沒有脈絡的新 Session 標成 `role:R`，它是最新的，之後每次召回都拿它，舊脈絡永久換線，不只失憶一次。(b) 的代價是延後開，不丟資料（席位跨 session 記憶本以交接檔為準）。若 PM／本人仍選 (a)，要接受上述後果。屬行為取捨，交 PM／本人 |
| U-3 | 最新的被鎖時 | (a) 往下挑舊的／開新的；(b) 拒絕＝沿用現有 exit 4→409 | **(b)**（見 §3 第 4 步）：不是新機制，少一個分支；(a) 會讓同身分兩個 agent、記憶分線 |
| U-4 | 「記」（import）誰做 | (a) sidecar 在 agent 退出時做；(b) agent 自己在收尾做；(c) 啟動時就預先 import 一個空殼 | (a)。(b) 依賴 agent 守規矩，被打斷就漏；(c) 增加空殼 Session。**只有「第一次全新開」那一段靠退出時 import**；接續以後由 `agora continue` 執行中自己寫回。**MLP 側前提（請 MLP 架構師確認）**：①容器重建會清家目錄（worker 環境說明 §4），agent 的本機 session 檔隨之消失，第一段若未在重建前 import 就丟；②sidecar 是否知道 agent 的 session id，未驗 |
| U-5 | 角色名 | (a) 完整身分原字串；(b) 去序號；(c) 只用 `<專案>-<角色>` | (a)。長期角色名本來就固定（pm、secretary 不帶序號，架構師固定 `-1`），不必另定推導規則；不同 team 不混。隊員（impl／rev）不召回，見 U-6 |
| U-6 | 哪些角色要召回 | (a) 全部；(b) 只有長期角色（pm、arch、secretary/PMO） | (b)。隊員一單一開，舊脈絡反而污染新單；由 sidecar 的 `open` 參數（有沒有帶 `role`）決定，agora 不管 |
| U-7 | 召回時要接多久以前的 | (a) 不限；(b) 超過 N 天就當沒找到 | (a)。N 是新參數；上下文過長的壓縮是 agent 自己的事。有問題再加 |
| U-8 | 跨機器：兩台機器各有同角色的 Session | 現況：`locked` 只判斷本機（`docs/design.md:258`） | 接受。另一台正在用同一個 Session 的話，寫回時會走既有的「被刪／衝突」規則；本期不加跨機鎖 |
| U-9 | 跨 agent（claude↔opencode）接續是否合格 | 同 agent 優先，或取最新的合格者 | **取最新的合格者**（§3 第 2 步），不做同 agent 優先，否則會捨棄較新的 Session。轉換會產生新 Session（`relation: continue`，`parents` 指回舊的） |
| U-10 | tag 前綴名 `role:` 是否和既有慣例衝突 | 現有範例 `ai:claude-work`、`workdir:/…`（`docs/design.md:264`）是使用者自訂 | 沿用「`前綴:值`」格式；`role:` 本 repo 內未見使用（`grep`），可用。要不要改成 `ai:<角色>` 讓人以為是同一類——不建議，語意不同 |

## 7. 驗證方式（日後實作的可驗判準）

日後實作若只是 §4 的文件約定＋sidecar 側呼叫，agora 側驗收如下（測試放 `tests/`，用 `uv run pytest`，不連真 Drive、不碰真家目錄，照 `AGENTS.md` 的測試隔離）：

1. **找得到、依序**：建三個 Session 帶 `role:R`，`updated_at` 各異；`search --filter tags=role:R --sort updated --json` 回的第一筆是最新的；`--limit 1` 只回一筆。（現有行為；補一個以 `role:` 為例的測試或 spec Scenario，說明約定。）
2. **不同角色不串**：`role:A` 與 `role:B` 各有 Session；`--filter tags=role:A` 看不到 B 的；`tags=role:mylinuxpool-pm` 不會比到 `role:mylinuxpool-pm-w2`（全等，不是包含）。
3. **接續後 tag 保留且不重複**：`continue --add-tag role:R` 對已有 `role:R` 的 Session，tags 裡只有一個 `role:R`（現有的 `_add_tags` 去重行為）。
4. **跨 agent 接續也帶得上**（本 repo 測試）：用假 agent 做 claude↔opencode 的 continue，`--add-tag role:R` 之後新 Session 的 tags 含 `role:R`（驗 §4 末段「不依賴照抄」）。
5. **locked 沿用 exit 4；missing 被排除**（歸屬：locked 的拒絕是 agora 側既有行為，測 `continue` 對被鎖 Session 回 exit 4＋`agora-locked`；「挑 missing_in_cloud 以外的最新者」是 **sidecar 側**測試，不在本 repo）。
6. **找不到→全新→記下→下次找得到**：第一次 `search` 為空；走「記」之後，同一個 `role:R` 下一次 `search` 回那一個。
7. **查找失敗不假裝**（歸屬：**sidecar 側**測試，不在本 repo）：假 rclone 失敗時 `search` exit 非 0，sidecar 走 U-2 的路徑並回錯誤。
8. **沒有新介面**：不寫成測試（凍結 `--help` 沒驗到行為）；審查時對 `build_parser`（`cli.py:2960` 起）的 diff 檢查沒有新旗標／新動作即可。
9. **文件**：`docs/design.md` header 一節有 `role:` tag 的約定說明；`session-continue` spec 有一條 Scenario（接續時帶 `--add-tag role:R`）。

整體驗收（人工，sidecar 側）：開一個 `mylinuxpool-pm-w2`，對話幾句，關掉，再用同一個身分開，它的第一個畫面看得到上次的對話；另一個角色開起來看不到。**此項要 sidecar 實作後才能驗，目前未驗。**

## 8. 範圍與下一步

- 本 note 不改 openspec；若要實作，agora 側只剩文件／測試一個小 PR（d 層級），主體在 sidecar（MyLinuxPool#340）。
- #205 仍是 `狀態:構想`；本 note 是其設計第一步。「是否要做協作關聯」（U-1）與「失敗策略」（U-2、U-3）需要 PM 或本人拍板後再開實作單。

## 9. 第 1 次退回的處理（`ARCH-439-review.md`）

- **M-1**：已修。§1 改寫 sidecar 現況（附 `docs/design.md:382/394`、`cli.py:2238-2242` 證據）；§3 第 3 步與 §4 改成「照 sidecar 現有帶 `session` 的 continue 呼叫再加 `--add-tag`」，不另列新寫法；「取代 #340 的鍵」改成「補從角色推出 id」。
- **M-2**：已修。§3 第 2 步與 U-9 只留「最新的合格者」。
- **M-3**：已修。刪「全部 locked」分支與先過濾 `locked`；最新被鎖就沿用 exit 4→409（§3 第 4 步、U-3）。
- **S-1**：採用。U-2 建議改 (b)，並寫出 (a) 的長期後果。
- **S-2**：採用。鍵用 `role:<ANGAREION_IDENTITY>` 原字串（§2、U-5）。
- **S-3**：採用。U-4 標出 MLP 側兩項前提，請 MLP 架構師確認。
- **S-4**：採用。§7 第 5、7 項標明 sidecar 側歸屬；第 8 項改成審查 diff，不寫測試。

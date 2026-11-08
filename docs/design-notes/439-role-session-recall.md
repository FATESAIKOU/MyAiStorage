# #439 設計筆記：角色式 session 召回（M6）

- 單：#439（MAS）；承接 #205（c，狀態:構想）。作者：設計作者（隊員，claude-sonnet-5-5）。2026-11-08。架構師第 1、2 次退回（`ARCH-439-review.md`、`review2`），已處理（見 §9、§10）。本人 2026-11-08 已裁定 U-1＝(a)、U-2＝(B)、失敗時走 (b)、U-13 隨 U-2（見 §6）。§11 為本人新要求的專家視角四問。
- 範圍：只設計，不寫程式、不改 openspec。sidecar 在別的 repo（MyLinuxPool），本 note 只寫「對 sidecar 的要求」，不碰它的程式。
- 本人原則：介面簡單、不加多餘機制。

## 1. 現況

**#205 的原話**（`gh issue view 205`）：用 agora header 的自由欄位（tag）做「人↔session」關聯；一個人有很多 session，關聯分「主要擔當」與「旁人（協作）」，同種關聯有時序；agent 啟動時要能參照。相關：MyPMO#3（期 4 M2）、MyLinuxPool#340（sidecar 同名再開時接回對話的鍵）。

**agora 已經有的零件**（都不用新寫）：

| 零件 | 位置 | 行為 |
|---|---|---|
| 自由 tag | `docs/design.md:100-107`、`src/agora/header.py:209`（`LIST_KEYS`） | `tags` 是 header 的清單欄位，使用者可寫 |
| 附加 tag | `src/agora/cli.py:195-230`（`_with_user`、`_add_tags`，附加去重在 226-230）、`cli.py:2841`（`ADD_TAG_ACTIONS = import／continue／edit`） | `--add-tag X` 附加、保留原有、去重；可重複 |
| 依 tag 找 | `header.py:238`（`parse_filters`）、`src/agora/store.py:2857`（`_matches`，清單欄位逐項比對）、`docs/design.md:263` 範例 `--filter tags=ai:claude-work --json` | `--filter tags=<全等>` 對 tags 逐項比；多個 `--filter` 是 AND |
| 排序、筆數 | `cli.py:432-482`（`cmd_search`）、`store.py:2803`（`Index.search(sort=…)`）、`docs/design.md:240-245` | `--sort updated`＝依 `agora.updated_at` 新到舊，同值 ULID 新到舊；`--limit N`；`--json` |
| 機器可讀列 | `cli.py:500-523`（`_search_json_row`）、`docs/design.md:250-262` | 有 `id`、`agent`、`tags`、`updated_at`、`locked`、`locked_by`、`missing_in_cloud` |
| 接續 | `cli.py:2201`（`cmd_continue`）；`openspec/specs/session-continue/spec.md` | 接續（含跨 agent）預設原地寫回**同一個 agora Session**（agora id 不變，`cli.py:1472` `same_id = True`、`2301` `agora_id: source_id`；寫回時更新 `source.agent`，`cli.py:1548`，tag 照 `--add-tag` 附加，`1561-1563`）；`--fork` 才分岔。這和 agent 內部 session id 是兩回事：只有 Claude 在滿足沿用條件時沿用內部 id（`cli.py:2140-2141`，碰撞會鑄新的，`claude.py:1007`），opencode 一律重編（`opencode.py:1614-1615`）；本 note 只用 agora id。`--dir` 指定工作目錄，沒給就用來源記的目錄（`cli.py:2262-2263`） |
| 同機互斥 | `cli.py:2253-2258`、`store.py:2880`（`continuing`）、spec「同一台機器上不能同時接續同一個 Session」 | 不含 `--fork` 的接續，同機已在接續就 exit 4＋`agora-locked <id>` |
| 被刪／跨 agent | `cli.py:348`（`_refuse_if_gone`）、`cli.py:403`（`_refuse_cross_agent_source`，目前只擋 agy 與非 agy 互換） | 來源被刪就拒絕；claude↔opencode 可跨 agent 接續（轉換的是 raw，寫回仍是原 agora Session） |
| 匯入時打 tag | `import --add-tag`（同 `ADD_TAG_ACTIONS`） | 把剛結束的 agent session 匯入並帶 tag |

**缺的**：沒有任何「角色」的約定；沒有人規定 tag 長什麼樣；sidecar 重開同名 agent 時不知道上次是哪個 session（MyLinuxPool#340 要的鍵就是 agora session id，`open` 的 `session` 已在收它，缺的是從角色推出它）。
**sidecar 已經會用 agora 接續**（本 repo 內的證據）：`docs/design.md:382`、`:394`（呼叫方 sidecar 靠 exit 4／`agora-locked` 判 409）、`cli.py:2238-2242`（`--headless-port` 由 sidecar 分配）；worker 環境說明：sidecar `open` 帶 `session` 會走 agora，舊 agora 回 409 `agora_too_old`。所以 `open` 已有「帶 agora session id 就用 `agora continue … --headless` 起」這條路；本單只補「從角色推出這個 id」。sidecar 程式本身（MyLinuxPool）我沒讀。

## 2. 角色→session 對應鍵與存放位置

**結論：用一個約定好的 tag，存在 Session header 的 `tags` 裡；不新增欄位、不新增檔案、不新增指令。**

- 鍵＝角色名：`role:<ANGAREION_IDENTITY>` 原字串，例如 `role:mylinuxpool-pm-w2`、`role:myaistorage-arch-w1-1`。召回只給長期角色（§6 U-6），它們的名字本來就固定，不必另定「去序號」的推導，sidecar 也不必解析身分格式；代理接手時沿用同一個名字。
- 值是字串全等比對（`--filter tags=role:mylinuxpool-pm-w2`），不需要新的比對方式。
- 存放：Session 自己的 header（隨 Session 上 Drive，換機器也在）。**不**另建「角色→session」對照表——對照表是第二份真相，會和 Session 不同步。
- 「最近的一個」就是時序：同一個 `role:` tag 下，`search … --strict --sort updated --json`（不加 `--limit`），依 JSON 的 `missing_in_cloud`／`agent` 取最新的合格者（§3 第 1、2 步）。時序不另存。
- 為什麼不用 header 的其他欄位：`case`／`refs` 是指向實體的 ref，語意不合；`agora` 區塊是系統欄位，使用者不能改（`header.py:SYSTEM_KEYS`）；`--header key=…` 加新頂層欄位（例如 `role: …`）也行，但 `--filter` 要靠 `tags` 才有「清單逐項比」的現成語意與 `--add-tag` 的附加語意，用 tag 零改動。

### 主要擔當／協作（#205 的兩種關聯）

#205 提了兩種。**本期只做「主要擔當」**（`role:<角色>`）。協作（旁人）本期不做（本人 2026-11-08 裁定，§6 U-1）；日後要有，同樣用 tag、另一個前綴（`with:<角色>`）即可，不需要機制改動。這樣介面只有一個前綴，需要時再加第二個。

## 3. 重開時的查找順序

由啟動方（sidecar）在 `open` 帶 `role`（沒帶 `session`）時，在開 agent 之前跑；輸入是這個 agent 的 `role:<身分>`，記為 `R`：

1. **找**：`agora search session --filter tags=role:R --strict --sort updated --json`（不加 `--no-sync`、不加 `--limit`）。判斷成敗只看 **exit code**（`--strict` 見 §4、U-2＝(B)，本人 2026-11-08 裁定）。
2. **挑**：取**最新的合格者**。JSON 上的初篩只有兩條：`missing_in_cloud` 不是 true，且 agent 接得了（同 agent，或 claude↔opencode；agy 只能接 agy，`cli.py:403`）。不做「同 agent 優先」，也**不過濾 `locked`**。初篩過了不等於真能接（見 §6 U-11）：**本期適用範圍限定 sidecar 用 claude／opencode 開的角色**（agy 的 headless 被擋，`cli.py:2213-2214`）。
3. **接**：把挑到的 id 當成 `open` 的 `session`，走 sidecar 現有帶 `session` 的 continue 呼叫（含它現在帶的 `--headless`、`--headless-port`、`--harness-file`、`--agent-arg` 等），**再加 `--add-tag role:R`**。對已有的 tag 是 no-op（去重）；跨 agent 接續也是寫回原 Session、`source.agent` 換成新 agent、tag 保留，帶一次只是保險。
4. **最新的被鎖**：`agora continue` 回 exit 4＋`agora-locked <id>`，sidecar 現有就把它當 409（`docs/design.md:382`、`:394`）。**沿用，不往下挑舊的**：鎖住代表這台機器上同一個角色已經在跑，再開一個會讓同一身分同時有兩個 agent（違反「同一個身分只留一個讀者」），記憶也分成兩條線。
5. **沒找到**（第一次、列表為空、全部被刪）→ 開全新的 agent。agent 結束後 `agora import … --add-tag role:R` 記下，之後找得到。
6. **查找本身失敗**（agora 非 0，含 `--strict` 下 Drive 列舉失敗或 `sessions/` 找不到的 exit 2）→ sidecar 回錯誤給 PM，由 PM 決定；**不自動開全新的、不接舊的**（U-2 失敗時 (b)，本人 2026-11-08 裁定）。**不比對 stderr 文字。**

## 4. 與 agora continue／sidecar open 的分工與最小介面

**agora 側（本 repo）：** 兩件事，header 欄位與新指令都不新增：①把「`role:` tag 的約定」寫進 `docs/design.md`（header 一節）與 `session-continue` spec 的一條 Scenario（§7）；②**另開一張 d 單**實作 `search --strict`（U-2＝B）：沿用 `store.sync(strict=True)`；`cmd_search` 在 `--strict` 時用 `throttle=False`（現為 `throttle=True`，`cli.py:437`）；Drive 列舉失敗 exit 2；`sessions/` 找不到也 exit 2（現只印訊息，`store.py:3101`，d 單要補）；**逐筆讀不到不算失敗**。本設計只寫契約，不實作。

**sidecar 側（MyLinuxPool，別的 repo，只提要求、不實作）：**

- `open` 已有「帶 `session` 就用 agora 接」的路。本單只多一件事：`open` 帶 `role`、沒帶 `session` 時，先跑一次「找」，把結果當成 `session` 走現有的路；沒找到就照現在的方式開全新的。**不新增第二套起 agent 的方式。**
- sidecar 不存「角色→session」，每次問 agora（Drive 是真相）。
- 結束後的「記」由 sidecar 在退出流程做（§6 U-4）。

**最小介面（約定，只有一條新指令用法）：**

```
找：agora search session --filter tags=role:<R> --strict --sort updated --json  # 新用法；--strict 由另開的 d 單新增（U-2＝B）
接：（sidecar 現有帶 session 的 continue 呼叫）… --add-tag role:<R>        # 多一個既有旗標
記：agora import --agent <a> --external-session-id <sid> --add-tag role:<R> # 同上
```

## 5. 不做什麼

- 不新增 `agora recall`／`agora role` 之類的指令；不新增 `--role` 旗標。唯一新增的旗標是 `search --strict`（U-2＝B，另開 d 單）。
- 不新增 header 頂層欄位、不碰 `agora` 系統區塊。
- 不建角色→session 的對照檔或資料庫；sidecar 不存對照。
- 不做 #205 的「協作」關聯（`with:`）與跨角色的脈絡共享（U-1＝(a)，本人 2026-11-08 裁定）。
- 不自動摘要、不自動合併多個舊 Session 當記憶（`agora merge` 另一條路，不在這張單）。
- 不改 `agora continue` 的原地寫回／fork 規則、不改同機互斥。
- 不做權限控制：tag 是自由欄位，誰都能寫 `role:別人`。這是約定，不是授權（私人 Drive 的信任範圍同現況）。
- 不解決 sidecar 的 `open` 內部流程；那是 MyLinuxPool 的設計。

## 6. 未決項

| # | 問題 | 選項 | 建議 |
|---|---|---|---|
| U-1 | 協作（旁人）關聯要不要做 | (a) 本期不做，只 `role:`；(b) 加 `with:<角色>` 前綴，召回時只當唯讀參考列出，不接續 | **已裁定（本人 2026-11-08）：(a) 本期不做。**理由：#205 本人說「先設計」，且最小介面原則；(b) 需要「怎麼把協作 session 餵給 agent」的產品決定，不是 tag 的問題 |
| U-2 | 查找失敗時（含如何辨識） | **怎麼辨識**：(A) 只看 exit code，不加介面（**被否決，備查**）；**(B) `search --strict`**，另開 d 單加一個旗標。**失敗時怎麼辦**：(a) 開全新 agent＋警告；(b) sidecar 回錯誤，由 PM 決定（PM 真要開就不帶 `role` 重開） | **已裁定（本人 2026-11-08）：辨識＝(B)；失敗時＝(b)**（sidecar 回錯誤，由 PM 決定，不自動開新）。**事實**：`search` 在 Drive 連不上時退回本機索引仍 **exit 0**（`store.py:3015-3019` 回 `return index`，`cli.py:482` `return 0`）；`store.sync(strict=True)`（`store.py:2985`、`3017`，`cli.py:1329` 已在用）才會往上拋，但 CLI 的 `search` 沒有這個旗標。**不把 stderr 文字（`連不上 Drive`、`讀不到，先跳過`、`Drive 上找不到 sessions/`）寫成 sidecar 的契約**：那是人讀、會改的文字；且 `讀不到，先跳過` 無法限定角色——Drive 上任何一筆無關的壞 Session 永遠不進索引、每次真同步都會再印（`store.py:3037-3058`、`3072-3080`），會讓所有角色的召回一起失敗。**(A) 只看 exit code（被否決，備查一句）**：Drive 連不上時照本機索引召回。同機角色自己的寫入已進本機索引（`_index_outbox`，`store.py:3118`），多數情況仍接得到；**殘餘風險**只剩「容器重建後本機索引為空、又剛好連不上 Drive」→ 開全新、換線；throttle 期間（300 秒，`store.py:3008-3009`）也沿用上次同步的結果。agora 與 sidecar 都沒有新東西。**(B) `search --strict`（定案）**：沿用 `store.sync(strict=True)`；不走 throttle；Drive 列舉失敗 exit 2（`EXIT_ERROR`，`cli.py:55`；`strict=True` 拋出的 `StoreError` 由 `main` 對成 exit 2，`cli.py:3111-3113`，`cmd_search` 本身不必另接）；`sessions/` 找不到也要 exit 2（現有 strict 只在列舉拋錯時 raise，`sessions/` 找不到只印訊息，`store.py:3101`，d 單要補這一處）；**逐筆讀不到不算失敗**（避免一筆壞資料擋住全部角色）；`cmd_search` 在 `--strict` 時用 `throttle=False`。契約是 exit code，和 exit 4 的慣例一致。**(B) 的限制**：最新那一筆剛好讀不到、或對方 raw 還沒寫完（U-13），仍可能取到舊列，明寫接受（殘餘風險）。`--no-sync` 完全不驗新鮮，sidecar 不應對召回使用。**失敗時 (a) 的長期後果**：退出時的「記」會把這個沒有脈絡的新 Session 標成 `role:R`，它是最新的，之後每次召回都拿它，舊脈絡永久換線，不只失憶一次。(b) 的代價是延後開，不丟資料（席位跨 session 記憶本以交接檔為準）。 |
| U-3 | 最新的被鎖時 | (a) 往下挑舊的／開新的；(b) 拒絕＝沿用現有 exit 4→409 | **(b)**（見 §3 第 4 步）：不是新機制，少一個分支；(a) 會讓同身分兩個 agent、記憶分線 |
| U-4 | 「記」（import）誰做 | (a) sidecar 在 agent 退出時做；(b) agent 自己在收尾做；(c) 啟動時就預先 import 一個空殼 | (a)。(b) 依賴 agent 守規矩，被打斷就漏；(c) 增加空殼 Session。**只有「第一次全新開」那一段靠退出時 import**；接續以後由 `agora continue` 執行中自己寫回。**MLP 側前提（請 MLP 架構師確認）**：①容器重建會清家目錄（worker 環境說明 §4），agent 的本機 session 檔隨之消失，第一段若未在重建前 import 就丟；②sidecar 是否知道 agent 的 session id，未驗；③**容器重建前要等 outbox 清空**（營運處理，不需機制）：否則上一次沒上傳完的 raw 在另一台或重建後的容器開時還沒寫完（U-13 第②層） |
| U-5 | 角色名 | (a) 完整身分原字串；(b) 去序號；(c) 只用 `<專案>-<角色>` | (a)。長期角色名本來就固定（pm、secretary 不帶序號，架構師固定 `-1`），不必另定推導規則；不同 team 不混。隊員（impl／rev）不召回，見 U-6 |
| U-6 | 哪些角色要召回 | (a) 全部；(b) 只有長期角色（pm、arch、secretary/PMO） | (b)。隊員一單一開，舊脈絡反而污染新單；由 sidecar 的 `open` 參數（有沒有帶 `role`）決定，agora 不管 |
| U-7 | 召回時要接多久以前的 | (a) 不限；(b) 超過 N 天就當沒找到 | (a)。N 是新參數；上下文過長的壓縮是 agent 自己的事。有問題再加 |
| U-8 | 跨機器：兩台機器各有同角色的 Session | 現況：`locked` 只判斷本機（`docs/design.md:258`） | 接受**既有限制**：跨機同時接續同一個 Session 是**後寫的贏、沒有衝突偵測**，另一台中途改的 header（title、tags）會被本機舊版蓋回（`docs/design.md:397`）。「被刪」救援只處理 Session 被刪，**不是**並行衝突保護。決策者接受的是：兩台同角色同時跑會丟其中一邊的對話；本期不加跨機鎖，靠「同角色只在一台開」的營運約定 |
| U-9 | 跨 agent（claude↔opencode）接續是否合格 | 同 agent 優先，或取最新的合格者 | **取最新的合格者**（§3 第 2 步），不做同 agent 優先，否則會捨棄較新的 Session。跨 agent 接續不產生新 Session：仍寫回同一個 agora id，`source.agent` 換成新 agent（`cli.py:1548`）；另存只發生在被刪救援（`cli.py:1503-1527`），不是跨 agent 的判斷 |
| U-10 | tag 前綴名 `role:` 是否和既有慣例衝突 | 現有範例 `ai:claude-work`、`workdir:/…`（`docs/design.md:264`）是使用者自訂 | 沿用「`前綴:值`」格式；`role:` 本 repo 內未見使用（`grep`），可用。要不要改成 `ai:<角色>` 讓人以為是同一類——不建議，語意不同 |
| U-14 | 召回結果日誌與換線掃描要不要做 | (a) 不做；(b) sidecar 每次召回記一行（角色、挑到的 id 或「無」、exit code）並定期掃「`role:R` 下 Session 多於一筆」 | 屬 sidecar 側，由 MLP 決定；**本設計不做**（符合「不加多餘機制」），agora 不動；§11 ①③ 只是建議 |
| U-13 | 同步部分失敗的殘餘風險（可能取舊列／誤開全新） | 隨 U-2：(A) 只看 exit code，下列風險全部接受（被否決）；(B) `--strict`，只消除「Drive 列舉失敗／`sessions/` 找不到／throttle 內沿用」，下列第②層仍在 | **已裁定（本人 2026-11-08）：隨 U-2＝(B)，殘餘風險明寫、接受。**風險有兩層：①throttle 讓不完整結果沿用約 300 秒（`last-sync` 照寫，`store.py:3124-3125`）——(B) 不走 throttle，可消除；②session 的 raw 還沒寫完（`raw_state` 為 False）時，該列被 `index.drop`（`store.py:3083-3086`）排除，首次會印「還沒寫完」，之後只要 session.md 的 md5 與遠端 raw 指紋不變，就走 `store.py:3045-3057` 的略過路徑（同樣 `index.drop`、**不再提示**），缺列可跨多次同步持續存在而沒有任何訊息——(B) 也不消除，原因是對方 raw 沒寫完，不是本機同步失敗；前提只有「上一次上傳沒完成，這次在另一台／重建後容器開」，營運上用 U-4③處理。後果是接到舊脈絡或多開一次全新（換線，見 U-2 (a) 的後果）。 |
| U-11 | 初篩合格但真的接不了 | 選到後 continue 失敗：(a) 回錯誤；(b) 跳過往下挑 | **(a)**，與 U-2 同理（跳過會讓記憶換線）。已知情況：opencode 1.x 的 v1 格式 Session 不能用 opencode 接（`opencode.py:1600-1609`）；agy headless 被擋（`cli.py:2213-2214`，故本期限定 claude／opencode）。continue 非 0（且不是 exit 4 的鎖）→ sidecar 回錯誤＋agora 的訊息給 PM；PM 可不帶 `role` 重開。不在 JSON 上猜版本 |
| U-12 | 傳了 `session` 又傳 `role` | (a) `session` 優先、不查找；(b) 以 role 為準 | **(a)**（§4 已寫 `role`＋沒帶 `session` 才查找）；有 `role` 時接續仍帶 `--add-tag role:R`，讓顯式指定的 Session 也被記到該角色下 |

## 7. 驗證方式（日後實作的可驗判準）

日後實作若只是 §4 的文件約定＋sidecar 側呼叫，agora 側驗收如下（測試放 `tests/`，用 `uv run pytest`，不連真 Drive、不碰真家目錄，照 `AGENTS.md` 的測試隔離）：

1. **找得到、依序**：建三個**來源不同**（不同 `source.agent`／`session_id`，`cli.py:446-456` 會依此去重）的 Session 帶 `role:R`，`updated_at` 各異；另補一組 `updated_at` 相同、以 ULID 新到舊排序的判準；`search --filter tags=role:R --sort updated --json`（召回用法再加 `--strict`）回的第一筆是最新的；`--limit 1` 只回一筆。（現有行為；補一個以 `role:` 為例的測試或 spec Scenario，說明約定。）
2. **不同角色不串**：`role:A` 與 `role:B` 各有 Session；`--filter tags=role:A` 看不到 B 的；`tags=role:mylinuxpool-pm` 不會比到 `role:mylinuxpool-pm-w2`（全等，不是包含）。
3. **接續後 tag 保留且不重複**：`continue --add-tag role:R` 對已有 `role:R` 的 Session，tags 裡只有一個 `role:R`（現有的 `_add_tags` 去重行為）。
4. **接續仍是原 Session**（本 repo 測試）：用假 agent 做一般（非 fork）continue，含 claude↔opencode 跨 agent：agora id 不變、`source.agent` 更新為目標 agent、原有 tag 保留、`--add-tag role:R` 後 tags 恰有一個 `role:R`；另列被刪救援另存的情況（新 id，tag 仍帶上）。
5. **locked 沿用 exit 4；missing 被排除**（歸屬：locked 的拒絕是 agora 側既有行為，測 `continue` 對被鎖 Session 回 exit 4＋`agora-locked`；「挑 missing_in_cloud 以外的最新者」是 **sidecar 側**測試，不在本 repo）。
6. **找不到→全新→記下→下次找得到**：第一次 `search` 為空；走「記」之後，同一個 `role:R` 下一次 `search` 回那一個。
7. **查找失敗的現況與驗收**：(a) **agora 側，記錄現況的測試**（假 rclone；各情況在「本機索引為空」與「有舊索引」兩種夾具各驗一次；夾具前提：明設沒有 pending／outbox 訊息，stamp 狀態逐項指定；現況 `search --json` 都 exit 0）：①列舉失敗 → 結果退回本機索引、exit 0，起始設「無 stamp」與「過期 stamp」各一次，驗失敗後**不建立、不更新** `last-sync`；②列舉成功但最新列 session.md 下載失敗 → 結果缺最新列、**`last-sync` 仍被寫**（把 U-13 殘餘風險釘成可見的測試）；③sessions/ 找不到 → exit 0、索引照舊；④批次下載失敗但逐筆補救成功 → 最新列在結果中；⑤②之後 300 秒內再 `search`（throttle）→ 假 rclone 列舉呼叫數不增加、仍缺最新列（記錄限制）；⑤′raw 不完整 → 缺該列；stamp 調成過期後再 `search`、遠端指紋不變 → 仍缺列且無首次提示（`store.py:3045-3057`），標為**接受的殘餘風險**（U-13）。這些測試只記錄現況，**不凍結 stderr 字句、也不是給 sidecar 比對用**。(b) **`--strict` 的驗收**（U-2＝B）：agora 側由另開的 d 單驗（假 rclone、隔離夾具；本機索引為空與有舊索引各驗一次）：Drive 列舉失敗 → exit 2；`sessions/` 找不到 → exit 2；一筆壞資料（逐筆讀不到）→ 仍 exit 0 且其他角色結果正常；不走 throttle（stamp 新鮮時仍連 Drive、假 rclone 列舉呼叫數增加）。**sidecar 側驗收**（不在本 repo）：召回一律帶 `--strict`；見 exit 2（或任何非 0）→ 回錯誤給 PM、不開全新、不接舊的；exit 0 → 照結果召回。⑤′與「最新那筆剛好讀不到」為已知殘餘風險（U-13），不要求擋下。
8. **沒有新介面**：不寫成測試（凍結 `--help` 沒驗到行為）；審查時對 `build_parser`（`cli.py:2960` 起）的 diff 檢查：除 `search --strict`（在另開的 d 單，不在本 PR）外沒有新旗標／新動作。
9. **文件**：`docs/design.md` header 一節有 `role:` tag 的約定說明；`session-continue` spec 有一條 Scenario（接續時帶 `--add-tag role:R`）。

**sidecar 側補強驗收**（不在本 repo，列給 MyLinuxPool）：(i) 最新列不合格（`missing_in_cloud` 或 agy）而第二筆合格 → 接第二筆；(ii) 最新合格列被鎖 → 回 409，不改挑舊列、不另開；(iii) 同時傳 `session` 與 `role`（U-12）→ 以 `session` 接、不查找、仍帶 `--add-tag`；(iv) 第一次全新開的退出 import 失敗 → 要有可見錯誤，並由 sidecar 保留該 agent session id 供重試（責任在 sidecar，U-4 前提①）；(v) 選中的 Session continue 失敗（v1 opencode 等，U-11）→ 回錯誤、不跳過。

整體驗收（人工，sidecar 側）：開一個 `mylinuxpool-pm-w2`，對話幾句，關掉，再用同一個身分開，它的第一個畫面看得到上次的對話；另一個角色開起來看不到。**此項要 sidecar 實作後才能驗，目前未驗。**

## 8. 範圍與下一步

- 本 note 不改 openspec；若要實作，agora 側是兩個 d 層級的 PR：文件／測試一個小 PR，另開一張 d 單加 `search --strict`；主體在 sidecar（MyLinuxPool#340）。
- #205 仍是 `狀態:構想`；本 note 是其設計第一步。U-1、U-2、U-13 已由本人 2026-11-08 裁定（§6）；U-3、U-11 隨之定案；召回日誌與換線掃描（U-14）屬 MLP 決定，是目前唯一未決項。實作單照 §4 另開（含 `search --strict` 的 d 單）。

## 9. 第 1 次退回的處理（`ARCH-439-review.md`）

- **M-1**：已修。§1 改寫 sidecar 現況（附 `docs/design.md:382/394`、`cli.py:2238-2242` 證據）；§3 第 3 步與 §4 改成「照 sidecar 現有帶 `session` 的 continue 呼叫再加 `--add-tag`」，不另列新寫法；「取代 #340 的鍵」改成「補從角色推出 id」。
- **M-2**：已修。§3 第 2 步與 U-9 只留「最新的合格者」。
- **M-3**：已修。刪「全部 locked」分支與先過濾 `locked`；最新被鎖就沿用 exit 4→409（§3 第 4 步、U-3）。
- **S-1**：採用。U-2 建議改 (b)，並寫出 (a) 的長期後果。
- **S-2**：採用。鍵用 `role:<ANGAREION_IDENTITY>` 原字串（§2、U-5）。
- **S-3**：採用。U-4 標出 MLP 側兩項前提，請 MLP 架構師確認。
- **S-4**：採用。§7 第 5、7 項標明 sidecar 側歸屬；第 8 項改成審查 diff，不寫測試。

## 10. 非作者審查（`REPORT-439-rev*.md`）的處理

- **H-1**：同步部分失敗的風險，現在由 U-2＝(B) 與 U-13 承接，殘餘風險明寫（本人 2026-11-08 已裁定）；不再用 stderr 文字當契約。
- **M-1**：已修。§1、§3 第 3 步、U-9、§7 第 4 項改為「寫回原 agora id」。
- **M-2**：已修。U-8 如實寫後寫覆蓋、無衝突偵測。
- **M-3**：已修。§3 第 2 步限定 claude／opencode 並分初篩與真接續；新增 U-11；§7 補強驗收 (v)。
- **L-1、L-2**：已修（§1 表格、`cli.py:2213-2214`）。
- **驗收補強**：§7 第 1 項、sidecar 側補強驗收 (i)～(v)、U-12。
- **架構師第 2 次退回（`ARCH-439-review2.md`）**：M-1 已修（U-2／U-13／§3／§4／§5／§7 第 7 項曾改成 A／B 二選一，現已由本人裁定 B，見 §6）；S-1（本節縮短）、S-2（U-4③）已採用。

## 11. 專家視角四問

本人新要求：設計類產出用四點回答。以下只寫「sidecar 以角色召回 Session」這個設計；引用都回 repo 原文看過，外部資料一律不引用（寫「未查」）。

### ① 頂尖專家會做哪些我們沒想到的事

- **定案後也要量失敗有多常發生。** U-2 已裁定 B（不重開）；「Drive 連不上＋本機索引為空」與「raw 沒寫完」在真實營運裡每週發生幾次；現在只有推論（U-2、U-13），沒有計數。專家會讓 sidecar 對每次召回記一行結果（找到／沒找到／失敗＋exit code），作為定案後的營運量測；這不是本期前置條件，也不重開 U-2。**這是我們缺的**：本設計未列出任何一處記召回結果（sidecar 現況未讀，不能斷言它沒有日誌）。
- **查「召回之後接得上嗎」而不只是「找得到嗎」。** §7 已驗接續本身（第 4 項的假 agent continue、sidecar 補強 (v) 的 continue 失敗），但**尚未對真實角色資料做 agent／版本相容性盤點**；U-11 已知 v1 opencode 接不了、agy 被擋，但沒有統計實際角色的 Session 屬於哪一類。專家會先列出現有長期角色（pm、arch、secretary）最新 Session 的 agent 與版本，確認初篩合格者實際能接。
- **把「換線」當成一等的壞結果。** 記憶分成兩條線不會報錯，只會悄悄失憶；專家會為它設一個可查的訊號（`role:R` 下的 Session 多於一筆；因為跨 agent 接續寫回同一個 Session，`source.agent` 變動是正常單線延續，不能當訊號；被刪救援另存的那一筆用 `relation`／`parents` 排除），定期掃一次。現況：設計只在 U-2、U-13 描述風險，沒有偵測（是否要做見 U-14）。
- **懷疑 tag 約定會被繞過。** tag 是自由欄位、誰都能寫（§5 已承認）；專家會問：有沒有人手動 `agora edit` 把 tag 改掉或寫錯字？召回因此靜默失敗（找不到→開全新）。這和 U-2 (a) 的長期後果同型。

### ② 他們會看哪些一手資料（實際可讀的）

| 要查的事 | 一手來源（本 repo，已讀過） |
|---|---|
| tag 比對、排序、去重語意 | `src/agora/header.py:209`（`LIST_KEYS`）、`:238`（`parse_filters`）；`src/agora/store.py:2857`（`_matches`）、`:2803`（`Index.search`）；`src/agora/cli.py:226-230`（附加去重）、`:432-482`（`cmd_search`） |
| `docs/design.md` 對外契約 | `docs/design.md:100-107`（tags）、`:240-262`（search 排序與 JSON 欄位）、`:382`（同機互斥、exit 4＋`agora-locked`）、`:394`（exit code 對照表，sidecar 靠 exit 4 判 409）、`:397`（跨機後寫的贏） |
| 同步失敗時 search 的真實行為 | `src/agora/store.py:3005-3020`（throttle、列舉失敗退回本機索引；`strict` 才 raise）、`:3083-3086`（raw 沒寫完被 `index.drop`）、`:3118`（`_index_outbox`）；`src/agora/cli.py:482`（`return 0`） |
| 接續是不是原地寫回 | `src/agora/cli.py:1472`、`:1548`（更新 `source.agent`）、`:1561-1563`（`--add-tag`）；`openspec/specs/session-continue/spec.md` |
| 既有測試能不能重用 | `tests/unit/test_search_sort_limit.py`（sort／limit 現有測試）、`tests/unit/test_search_json.py`、`tests/unit/test_add_tag_empty.py`；假 rclone `tests/fakes/fake_rclone.py`；測試隔離規格 `openspec/specs/test-isolation/spec.md` |
| sidecar 的 `open`／`session`／`agora_too_old` 行為 | 只有 worker 環境說明與 `docs/design.md:382/394`、`cli.py:2238-2242` 這類 agora 側的引用；**sidecar 本身的程式與文件在 MyLinuxPool，未讀**；MyLinuxPool#340、MyPMO#3 的內文也**未查**（本 note 只引了 #205 的原話） |
| 業界做法（agent 長期記憶、session 召回的現有系統） | **未查**，不引用 |

### ③ 用哪些方法與工具

- **假 rclone＋隔離夾具重現失敗**：`AGORA_RCLONE` 指向 `tests/fakes/fake_rclone.py`、`AGORA_UPLOAD=inline`，照 `AGENTS.md` 的測試隔離（`isolated_home`）。§7 第 7 項(a) 的六種同步失敗情境（①②③④⑤⑤′）應都能用它釘住（**推論**：`fake_rclone.py` 有 `FAKE_RCLONE_FAIL`、`FAKE_RCLONE_OMIT_FILES`、`FAKE_RCLONE_BULK_PARTIAL`、`FAKE_RCLONE_NOT_FOUND_RC` 等注入點，但未逐條對過、⑤′ 未實際跑過），不連真 Drive。
- **用 `agora search … --json` 當唯一查詢介面**：不寫新工具；`search … --strict --sort updated --json`（不加 `--limit`）的 exit code 加上 JSON 的 `missing_in_cloud`／`agent` 欄位，就是 sidecar 的全部判斷依據（§3 第 1、2 步）。
- **審查 `build_parser` 的 diff**（§7 第 8 項）來確認「沒有新介面」，不用測試凍結 `--help`。
- **召回結果日誌（建議，屬 sidecar 側）**：每次召回一行（角色、挑到的 id 或「無」、agora exit code），供①的計數與換線掃描用。**這是本設計目前沒有的工具**；加在 sidecar 側，agora 不動。
- 不用：外部向量檢索、摘要合併（§5 已排除）。

### ④ 怎麼證明結果有用（驗收方式）

對照 §7，不另造新驗收：

| 要證明的事 | 對應 §7 | 狀態 |
|---|---|---|
| 同一角色找得到最新的、不同角色不串 | 第 1、2 項 | 可在 agora 側測，已列 |
| 接續後 tag 保留、仍是原 Session | 第 3、4 項 | 可在 agora 側測，已列 |
| 被鎖沿用 exit 4；missing 排除 | 第 5 項、sidecar 補強 (i)(ii) | 被鎖的 exit 4 為 agora 側；missing 排除與回 409（(i)(ii)）為 sidecar 側 |
| `session`＋`role` 同傳時以 `session` 為準 | sidecar 補強 (iii) | sidecar 側 |
| 沒有新介面、有文件約定 | 第 8、9 項 | 審查 `build_parser` diff／文件 PR |
| 第一次→記下→下次找得到 | 第 6 項 | 已列 |
| 失敗時不靜默換線 | 第 7 項(a)(b)、sidecar 補強 (iv)(v) | 現況可釘；`--strict` 驗收與 sidecar 側 (b) 已按 U-2＝B 定版（§7 第 7 項） |
| **整體「重開後看得到上次對話、另一角色看不到」** | 「整體驗收（人工）」 | **目前未驗**，要 sidecar 實作後才能驗 |

**§7 沒覆蓋的缺口（指出，不在此新增驗收）：**
1. **沒有「有用」的量化判準**：§7 驗的是機制正確（tag、排序、鎖、失敗），不驗「召回後的 agent 是否真的延續了脈絡」。人工整體驗收只有一個通過／不通過的場景。是否要加（例如第一個畫面含上次對話的某個標記字串）屬 sidecar 側驗收，請 PM／本人決定。
2. **沒有營運層指標**：①提到的召回結果日誌與換線掃描不在 §7；若要證明長期上沒有悄悄換線，需要它。
3. **U-13 第②層（raw 沒寫完）無法用 `--strict` 驗掉**：§7 第 7 項只把它記錄成已接受的殘餘風險，沒有驗收能證明它不發生。

**沒驗到**：本節沒有跑任何測試或程式；所列行號是我再讀 repo 對過的原文，不代表 §7 的測試已存在或已通過（測試尚未實作）。

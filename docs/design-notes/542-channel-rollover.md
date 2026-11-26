# #542 Angareion 頻道換頁（rollover）：留言上限、403 分辨、告警

- 單：#542（MAS，層級 c；Angareion 讀寫路徑，架構師必經）。
- 作者：專案架構師（作者模式，myaistorage-arch-w1，Claude Opus 5.5），2026-11-25。基準：main `512f58e`。
- 修訂：
  - 第 2 版（2026-11-25），依非作者審查（Sonnet，H-1、M-1～M-6、L-1～L-9、A-1～A-6）修正。改動對照在 §12。
  - 第 3 版（2026-11-25），依重審（Sonnet，H-1、M-1～M-5、L-1～L-6、A-7～A-9）修正，並同步 PR-1 實作已定的 7 點（不改 PR-1 契約的意義）。改動對照在 §13。
  - 第 4 版（2026-11-25），依第 2 次重審（Sonnet，M-1～M-3、L-1～L-3、nit-1～3、A-10、A-11）修正；游標改成每頁一份（A-10）。PR-1 的範圍與 C-1～C-5 不動。改動對照在 §14。
  - 第 5 版（2026-11-25），第 3 次重審「可以」（Sonnet，head 5b72d07）之後，收低項與 nit（L-1、L-2、nit-1～3），並把 PR-2 切成 2a／2b／2c（§6.3）。PR-1 已由 #544 實作並 merge，範圍與 C-1～C-5 不動。改動對照在 §15。
  - 第 6 版（2026-11-25），依 #545 的審查（Sonnet，head b954a5e：W-1、nit-1～5）修 §6.3、C-22、§9、§10。改動對照在 §16。
  - 第 7 版（2026-11-26），PR-3 開工（PMO 11-26 決定）：新增 §6.4（PR-3 切成 3-0／3a／3b／3c，附簽名）；§1.3 補三處（W-4 認 `WriteRefusedError`、W-9 的 refresh 寫成「以新實體從 A 重來」、新增 S-8′）；§4 補「拿到 403 原樣樣本後要改的地方」。基準：main `15790cf`。改動對照在 §17。
  - 第 8 版（2026-11-26），依第 7 版的非作者審查（Sonnet，head 8b75629：中-1、中-2、低-1～低-7）修正：S-8′ 的重貼加上界（新增 S-4′，`channel rollover` 的 `repost`）、§4.1 的樣本規則加計數條件，其餘是 §6.4 的簽名與測試寫法補齊。切分與依賴不變。改動對照在 §18。
- 範圍：只設計。動到的模組：`backend`、`github`、`state`、`channel_config`、`registry`、`registry_rebuild`、`client`、`log_cache`、`decision_cli`、`cli`；新增 `channel_cli`、`registry_rules`、`rollover`。主 spec（`openspec/specs/angareion/spec.md`、`angareion/channel-config/spec.md`）要補的地方列在 §9，由 PM 決定與撰寫。
- 實測：只讀。`gh api` 讀得到註冊簿 MyAiStorage#47（2026-11-25 有 52 則留言）。AiCompany-channel 是私有 repo，這台 worker 的 `gh` 讀不到（404），所以各頁的留言數照 issue 本文引用，沒有自己量。沒有對真 GitHub 寫入。
- 名詞：
  - **頁**：一個頻道名對應的一張 backend issue。
  - **現頁**：channels.json 的 `issue`，新訊息原則上寫這裡。
  - **退役頁**：`retired`，換頁前的舊頁。
  - **旁頁**：競態下多開、從沒當過現頁的頁（`side: true`）。

## 0. 結論

1. **資料模型**：一個頻道名對應一串頁。channels.json 每一筆照舊有 `backend`、`issue`（＝現頁）。換過頁的頻道多一個 `retired`，內容是 `[{issue, until, side?}]`，舊的在前。
   - 舊讀者只讀 `backend`／`issue`，看不到 `retired`；格式 `version` 不升，沒換頁的頻道重建結果逐位元組不變（§1）。
2. **序化靠註冊簿留言的全序，不靠鎖**。GitHub 的 issue 沒有交易，channel token 也只有 Issues 權限，拿不到 ref 這類 compare-and-swap。
   - 換頁是註冊簿上的一則 `rollover` 動作，帶 `from`（從哪一頁換出）。
   - 重建依 `(created_at, id)` 依序套用：
     - 第一則 `from`＝現頁的換頁成立；
     - 同時開出的第二頁記成旁頁，照樣被讀。
   - 寫入端開新頁前，先在本機用**同一個純函式** `registry_rules.rebuild_channels` 重算註冊簿，所以「已經有人換頁、只是還沒落地」不會再開一頁。
   - 貼完 `rollover` 後**再重算一次**：自己的頁成了旁頁，就改寫贏家那一頁，所以旁頁通常是空的（§1.3）。
3. **讀者游標的 key 不變，值多一個選填的 `pages`：每一頁自己的游標**（頁號→`{position, at, seen}`；第 4 版，A-10）。
   - 游標的 key 本來就是頻道名（`channel:`／`to:`／`mention:` 加頻道），position 是 GitHub 的 comment id。
   - 一次讀取讀「現頁＋還在寬限期內的退役頁」，每一頁用自己的游標照單頁的現行規則讀，回傳的訊息合併後依 comment id 排序。主游標照舊存在，吃合併後的 entries，只用來判斷寬限期、給舊檔與舊版用。
   - 新游標（主游標的 `at` 是 `None`）不讀全部退役頁，只讀「最近一次換頁的 48 小時內」退役的頁（第 5 版，§2.1、§8.4 的 A-12）：讀的頁數不隨換頁次數長大。
   - **游標沒讀過的頁一律從頭讀**（`pages` 不含它）。這是不丟訊息的關鍵：讀者的設定檔晚到時，它看到的頁集合會變大，新加進來的頁上可能有 id 比游標小的訊息（§2.1、§2.3）。
   - 讀過的頁某一輪讀失敗而被略過，它的游標就不動；恢復後從那裡接續，不從頭讀、也不漏（§2.1）。
   - 每一頁的游標只靠它自己的訊息前進，所以讀取不依賴 comment id 跨 issue 遞增；只有「同一次讀取回傳的訊息跨頁的相對順序」依賴它（§10 第 4 項）。
4. **寫入端「舊頁先寫滿」**（Q-2，建議）：換頁發生在 2400 則。之後的 24 小時內，新寫者仍寫舊頁，直到 2490 則。
   - 好處：換頁剛落地時，config 還沒拉新的讀者也收得到，不會出現「被 poke 叫醒、讀到空的、又閒下來」。
   - 落地之後，新讀者兩頁都讀。
5. **403 分辨**：
   - 先看 rate limit 的標頭；標頭不在時，才看已知的訊息字樣（primary：`x-ratelimit-remaining: 0`；secondary：`retry-after`，或訊息含 `secondary rate limit`）。
   - 不是 rate limit 的 POST 留言 403／422，再 GET 一次 issue 看 `comments`：
     - 達 2500：`ChannelFullError`；
     - 達 2400 也當作滿（上限的計法沒驗過，保守處置）；
     - 其他情況：一般錯誤。
     - 兩種錯誤都附 GitHub 回應的 `message`（遮蔽、截短）。
   - 判「滿」用可以重查的數字，不猜上限回應的文字。真正的上限回應長什麼樣沒驗到（§10）。
6. **告警**：不能用 workflow 量，因為 AiCompany-channel 是私有 repo，MyAiStorage 的 `GITHUB_TOKEN` 讀不到。改由 channel token 這一側量：
   - `send` 本來就要 GET 現頁的留言數（換頁要用）。順便在 ≥2000 時在 stderr 警告寫的人。
   - 新增唯讀的 `angareion channel status`，給 PMO 看；建議由 `pmo tick` 定期呼叫並列在 tick 輸出（另開 d，Q-4）。不新增常駐程序。
7. **緊急緩解**：PR-1（403 分辨＋計數＋`channel status`）小而獨立，**可以先實作**，範圍見 §6.1。
   - PR-2（讀側跨頁＋重建認得 `rollover`）合上、所有席位升級之後，就可以**手動換頁**：PMO 在 AiCompany-channel 開新 issue，在註冊簿貼一行 `rollover` JSON。不必等 PR-3 的自動化。
   - 在那之前照現行做法用 `-b` 頂著。w3 已經有 `-b`；m1（約剩 260 則）、w4（約剩 550 則）請 PMO 用 PR-1 的 `channel status` 盯，必要時再開 `-b`（§6）。
8. **`-b` 頻道不併回**：訊息本文的 `channel` 不同、游標 key 不同，併成某頻道的頁會讓已讀過的再送一次。換頁上線後改回原名發訊，`-b` 留 7 天再 `channel delete`（Q-3）。
9. **切成 3 個 PR**：PR-1 → PR-2 → PR-3（PR-1 已 merge，#544）；PR-2 再切成 2a、2b、2c 三個可以各自合併的 PR（§6.3，都已 merge）；PR-3 切成 3-0、3a、3b、3c（§6.4，第 7 版）。每一步合上後 main 都不會更糟：
   - PR-2 在沒有 `retired` 時**輸出**不變（`LogCache` 升版會整份重建一次，見 C-14）；
   - 手動換頁與 PR-3 的 merge，都要等所有席位的 `angareion --capabilities` 列出 `paged-read`（Q-6、§6.2）。
   - 契約 C-1～C-26。

## 1. 資料模型

### 1.1 channels.json（相容）

```json
{
  "channels": {
    "team-x-w2": {
      "backend": "aicompany-channel",
      "issue": 41,
      "retired": [
        {"issue": 28, "until": "2026-11-26T01:02:03Z"},
        {"issue": 42, "until": "2026-11-26T01:02:05Z", "side": true}
      ]
    },
    "team-x-w3": {"backend": "aicompany-channel", "issue": 29}
  },
  "version": 1
}
```

- `issue`：現頁。舊讀者與舊寫者照舊只用它，換頁後就自動讀寫新頁；舊讀者看不到舊頁剩下的未讀訊息（部署順序見 §6.2 與 Q-6）。
- `retired`：退役頁，**依重建時套用的順序**排列。
  - `until`：讓它退役的那則註冊簿留言的 `created_at`，是後端時間（D4：只有後端決定新舊）。旁頁的 `until` 是它自己那則 `rollover` 的時間，也就是它被註冊的時間。
  - `side: true`：旁頁。
- 沒有換過頁的頻道不寫 `retired` 鍵。`render_channels` 用 `sort_keys`，所以既有頻道的輸出逐位元組不變。
- `ChannelStore.channels()` 現在只讀 `backend`／`issue`、忽略其他鍵（`channel_config.ChannelStore.channels`），所以舊版可以讀新檔。新版對 `retired` 嚴格：形狀不對＝`ChannelConfigError`（exit 2），與這個檔的其他欄位同一個規則。這個檔是機器產物。

### 1.2 註冊簿動作 `rollover`

```text
{"schema": 1, "action": "rollover", "channel": "team-x-w2", "backend": "aicompany-channel", "issue": 41, "from": 28}
```

- `schema` 不升：這是新增動作，不是改既有動作。`from` 只有 `rollover` 必填，`register`／`unregister` 帶 `from` 仍是「unknown fields」。
- 舊版重建碰到它，會先因為多了 `from` 而回報 `unknown fields: from`（`registry.py` 的多鍵檢查在動作檢查之前），然後略過，頻道對應不變（審查者以現行程式實跑確認）。但重建只在 workflow 跑，workflow 跑的是 main 的程式；PR-2 把解析與套用一起帶進 main，客戶端要等 PR-3 才會寫出這種留言，手動換頁也在 PR-2 之後，所以不會有「舊重建看到新動作」的時窗。
- 套用規則（`registry_rules.rebuild_channels`，依 `(created_at, id)`）：

| 情況 | 結果 |
|---|---|
| 頻道不存在 | 回報，略過 |
| `backend` 與現頁不同 | 回報，略過 |
| `issue` 已是現頁或已在 `retired` | 重複，no-op（不回報） |
| `from`＝現頁 | `retired` 加 `{from, until}`；現頁＝`issue` |
| `from` 在 `retired` 裡（現頁已被別人換掉） | `retired` 加 `{issue, until, side: true}`；現頁不變 |
| 其他 `from` | 回報，略過 |

- `register` 判斷「同一個實體」只比 `backend`＋`issue`（現頁）。`unregister` 指向現頁時整筆移除（含 `retired`）；指向退役頁是 no-op，並回報。
- 回報照現行做法貼成註冊簿留言。**引用某一則留言的回報，同一段文字只貼一次**：`Rebuilder.run` 貼之前先看留言串裡有沒有一模一樣的本文，有就不貼（C-25）。
  - `RebuildSummary.reports` 照舊是這一輪產生的**全部**回報（含沒貼的），新增 `RebuildSummary.posted` 是這一輪**實際貼出**的（重審 nit-1）。既有測試 `fake.posted == summary.reports` 都是第一輪（沒有可去重的），照舊成立；C-25 的測試對兩者分開斷言。
  - 只對「引用某一則留言」的回報去重（`report_text(reason, body)` 的 `body` 不是空的）。這種回報描述的是那則留言本身，留言不變，回報就不變；每次 run 都會再產生一次。
  - 固定文字的回報（`body` 是空的，例如 `the repository file kept changing while writing; this run did not update it`）描述的是這一次 run 發生的事，每次發生都要貼，不去重（重審 L-4）。
  - 現行程式每次 run 都把全部回報再貼一遍（`registry_rebuild.py` 的 `for report in reports: self.api.post_comment(report)`）。這份設計新增了幾種會永久存在的回報（上表的「回報」列），不去重的話，每次觸發與每天的排程都會讓註冊簿多一則。
  - 「寫進 workflow log、不貼」也可以，但和既有的回報方式不一致，寫的人也看不到。所以去重，不改管道。
- `channel delete` 只關現頁（照舊）。退役頁本來就不再寫入，不另外關（Q-5）。
  - **PR-2 起，關頁前先確認本機的現頁沒有過期**（重審 M-4）：`ensure_fresh` 之後強制 `store.fetch()` 一次，再讀註冊簿、以 `registry_rules.rebuild_channels` 重算。重算裡本機的現頁 P 已在 C 的 `retired` → 不關、不註銷，exit 2（`channel '<C>' was not deleted: its current page changed and the local configuration has not caught up yet; try again later`）。
  - 只有「有證據表示過期」才拒絕。註冊簿讀不到、接縫物件沒有 `all_comments`、重算裡沒有 C，或現頁相同 → 照現行流程（既有 `delete` 測試不改預期）。
  - fetch 失敗照 `ensure_fresh` 的現行處理（不擋）；擋的是上面那個重算結果。

### 1.3 寫入端：選哪一頁、何時開新頁

常數（放 `backend.py`，與 `CURSOR_SEEN_MARGIN_SECONDS` 同一處；值由 PM 定，Q-7）：

| 名稱 | 值 | 用途 |
|---|---|---|
| `PAGE_COMMENT_LIMIT` | 2500 | GitHub 上限（觀測值，issue 本文） |
| `PAGE_WARN_AT` | 2000 | 告警 |
| `PAGE_ROLLOVER_AT` | 2400 | 寫者確保有下一頁；被拒時也當作滿的門檻（§4 規則 3） |
| `PAGE_FILL_LIMIT` | 2490 | 退役頁還可以寫到這裡（留 10 則給競態） |
| `PAGE_FILL_WINDOW` | 24 h | 退役頁在 `until` 之後還可以寫多久 |
| `RETIRED_READ_GRACE` | 48 h | 讀者讀退役頁的寬限，必須大於 `PAGE_FILL_WINDOW` |

#### `PageWriter.write(entity, message) -> str`（`send` 與 `decision` 的寫入都走它）

`E` 一開始是本機 channels.json 給的實體；`ensure_successor` 成功時換成重算的實體，從 A 重來。重來最多 3 次，第 4 次就是 `BackendError`（訊息不含編號）。

- **A（先寫舊頁）**：R＝`E.retired` 裡最後一個非旁頁；沒有 R 就跳到 B。
  - **本機時鐘已超過 `R.until + RETIRED_READ_GRACE`（48 h）→ 直接到 B，不發 `page_size(R)`**（重審 L-2）。窗是 24 h，多留的 24 h 吸收本機時鐘的誤差；超過它的時鐘誤差只會讓 A 多一次 GET 或少寫舊頁幾則，不影響送達（讀者照樣讀現頁）。
- **B（現頁）**：P＝`E.issue`。

窗內的判斷（W-1）一律用 `page_size` 回應的 `Date` 標頭（後端時間），不用本機時鐘；沒有 `Date` 就當作「不在窗內」。本機時鐘只用在上面那個「肯定在窗外」的跳過。

| # | 步驟與輸入 | 結果 | 動作 |
|---|---|---|---|
| W-1 | A：`page_size(R)` | 成功，`Date − R.until < PAGE_FILL_WINDOW`，而且 n < 2490 | 寫 R |
| W-2 | A：`page_size(R)` | 成功，但不在窗內或 n ≥ 2490；或沒有 `Date` | 到 B |
| W-3 | A：`page_size(R)` | 失敗（任何 `BackendError`，含 404） | 到 B（不警告） |
| W-4 | A：寫 R | `ChannelFullError`，或 `WriteRefusedError`（403／422 被拒、不是 rate limit，第 7 版） | 到 B。兩者都是「確定沒寫進去」 |
| W-5 | A：寫 R | 其他錯誤（含 `RateLimitError`、網路） | 往外丟（exit 2）。POST 的結果不確定，換頁再寫可能重複 |
| W-6 | B：`page_size(P)` | n < 2400 | 寫 P |
| W-7 | B：`page_size(P)` | 失敗 | 寫 P（fail-open） |
| W-8 | B：`page_size(P)` | 2400 ≤ n < 2500 | `ensure_successor`：成功 → E＝重算實體，回到 A；失敗 → 寫 P，stderr 一行警告（P 還沒滿） |
| W-9 | B：`page_size(P)` | n ≥ 2500 | `ensure_successor`：成功 → E＝重算實體，回到 A；S-2 不相容 → 先 `refresh(C)`（強制 fetch 本機設定檔、取新的 E_local），E＝E_local，回到 A（算一次重來；第 7 版寫明：新的 E 若已落地新頁，就不必再讀註冊簿；若現頁仍是 P，會再走到 W-9、再呼叫一次 `ensure_successor`）；同一次 `write` 第二次 S-2 不相容，或其他失敗 → exit 2 |
| W-10 | B：寫 P | `ChannelFullError`（含 §4 的「當作滿」） | 同 W-9 |
| W-11 | B：寫 P | 其他錯誤 | 往外丟（exit 2），理由同 W-5 |
| W-12 | 任一步 | 成功寫入 | 回傳訊息 id，結束 |

- W-8 成功後回到 A：重算實體的最後一個非旁頁通常就是 P。窗內、而且 <2490，就照樣寫 P；這就是「舊頁先寫滿」在設定檔還沒落地時的版本。
- 本機設定檔過期的寫者（換頁早就落地，自己還不知道）會在 W-8／W-9 拿到重算實體。到 A 時，P 的 `until` 已經在窗外，就寫新頁，不會一直寫舊頁（M-5 對 ≥PR-3 寫者的部分）。
- 只有 W-8、W-9、W-10 會讀註冊簿。P 在 2400～2499 的期間，每次寫入多 1 次 `page_size` 與 1 次註冊簿列表（現在 1 頁），最多約 100 次，量級可以接受。
- **W-9／W-10 的 `refresh`**（重審 L-5）：S-2 不相容可能只是註冊簿列表比 workflow 落地的檔案還舊（複寫延遲，§10 第 7 項），而 P 已滿、不能退回寫 P。所以先 fetch 一次本機設定檔、用新的 E_local 再試一次，仍不相容才 exit 2。`refresh: Callable[[str], Entity]` 由建構 `PageWriter` 的一方（`cli`）注入（內容是 `store.fetch()` 再 `store.entity_for(C)`），`rollover` 不 import `channel_config`（§8.2）。每次 `write` 最多 refresh 1 次，算進「重來最多 3 次」。W-8 不 refresh（P 沒滿，寫 P 並警告）。

#### `ensure_successor(C, P, E_local) -> Entity`

| # | 步驟與輸入 | 結果 | 動作 |
|---|---|---|---|
| S-1 | 讀註冊簿整串（`all_comments`） | 失敗 | 失敗（「could not read the channel registry」） |
| S-2 | 重算（`rebuild_channels`，backend 名由呼叫方傳入） | C 不存在；或 backend 與 `E_local` 不同；或 P 既不是現頁也不在 `retired`；或 `E_local.retired` 有重算裡沒有的頁 | 失敗，不開頁（「the local channel configuration does not match the registry」）。這是設定過期或規則版本漂移（A-2），不能確定新頁會被重建接受 |
| S-3 | 重算 | 現頁 ≠ P，而且 P 在 `retired` | 回傳重算實體，不開頁 |
| S-4 | 重算 | 現頁＝P | 待註冊紀錄有 `(C, backend, from=P)` 的頁 N → **不開新頁**，用 N 接著做 S-6；沒有 → `open_page`（標題＝C，本文「continued from #P」），成功後立刻把 N 記進待註冊紀錄 |
| S-4′ | N 來自待註冊紀錄（第 8 版） | S-1 讀到的留言裡已有一則與 `rollover_comment(C, backend, N, P)` 解析結果相同的 `rollover`（`parse_comment` 得到同一個 `Action`：同 channel、backend、issue＝N、from＝P），而 S-2／S-3 的重算沒有套用它（現頁仍是 P） | 失敗（S-8′ 的同一個 `REJECTED`），**不驗、不貼**；待註冊紀錄保留。重建是純函式，同一則再貼一次改變不了結果，只會把註冊簿灌滿（註冊簿也有 2500 則上限，所有頻道共用）。例外：`channel rollover`（PMO 明確下的指令，`repost=True`）照樣往下驗、往下貼，給「修好了原因（例如換了作者身分）之後重貼」一條路 |
| S-5 | `open_page` | 失敗 | 失敗 |
| S-6 | `verify_channel(N)` | 回 `False`（404，或標題不是 C） | 失敗；N 留在 backend，沒有註冊。N 來自待註冊紀錄時，同時刪掉那筆紀錄（N 確定不能用，下次重開） |
| S-6′ | `verify_channel(N)` | 丟 `BackendError`（網路、5xx、rate limit 等暫時性錯誤） | 失敗；**待註冊紀錄保留**，下次再驗同一頁（重審 L-2） |
| S-7 | `post_with_retries(rollover_comment(C, backend, N, P))` | 失敗 | 失敗（「opened a new page but could not register it」）；N 上 0 則訊息；待註冊紀錄保留，下一次 S-4 重試註冊同一頁 |
| S-8 | 貼成功 → **事後重算**：再讀一次註冊簿，把自己貼出的那則（`post_with_retries` 回傳的留言：`id`、`created_at`、`author_association`）併進去（同 id 去重） | 讀成功 | 刪掉待註冊紀錄；回傳重算實體：N 是現頁就是 N；N 成了旁頁就是贏家的實體，N 保持空的 |
| S-9 | 事後重算 | 讀失敗 | 刪掉待註冊紀錄；用 S-1 的留言加上自己那則重算，回傳（N 是現頁） |
| S-8′ | S-8／S-9 的重算（第 7 版） | C 的現頁仍是 P（自己那則沒有被套用：例如貼出的留言的 `author_association` 不在 `AUTHORIZED_ASSOCIATIONS`、回應沒有 `id`，或重建回報了它） | 失敗（「the registry did not accept the new page」）；**待註冊紀錄保留**（下一次重試同一頁，不再開頁；下一次在 S-4′ 就停，不再貼）。不這樣擋，`PageWriter` 會帶著同一個 E 回到 A、B，再走一次 S-4，每次重來都多一張頁 |

- **註冊在第一次寫入之前**：新頁在 `rollover` 貼成功、而且事後重算之前，一則訊息都不寫。
- 所有失敗都不在訊息裡印編號（spec：介面不出現 issue 概念）。沒註冊的新頁留在 backend，PMO 用標題找。
- 把自己那則併進去，是因為列表不保證「讀得到自己剛寫的」。沒有它，S-8 可能把自己的頁算成「還沒有下一頁」。
- `post_with_retries` 重試時，可能貼出兩則一樣的 `rollover`。第二則是「`issue` 已在」的 no-op（§1.2）。
- S-8 需要貼留言的回應（重審 L-3）：
  - `CommentPoster.post_comment` 的回傳從 `None` 改成留言物件（dict）；
  - `post_with_retries` 的回傳從 `None` 改成**最後一次成功**的留言物件；
  - `FakeRegistryClient.post_comment` 回傳它存下的留言（`add_comment` 已經回傳 dict）。
  - 既有呼叫端（`channel create`／`delete`、`Rebuilder`）不看回傳值，相容。

**待註冊紀錄（孤兒頁的上界，重審 M-2、A-8）**：

- 問題：S-6／S-7 失敗之後，P 還在 2400～2499（W-8 寫 P）或已滿（W-9 exit 2），下一次 `send` 又會走到 S-4。沒有紀錄的話，每次都再開一頁，最多上百張沒註冊的頁。
- 做法：本機狀態目錄記「已開、還沒註冊的頁」，下一次 S-4 先重試註冊同一頁。
  - 檔案：`pending-pages-<identity>.json`（與游標檔同一個狀態目錄、同一個 `identity_slug`），內容 `{"version": 1, "pages": {"<C>": {"backend": …, "issue": N, "from": P}}}`。每個頻道最多一筆。
  - 實作：`state.PendingPageStore`（`get(C)`、`put(C, backend, issue, from_issue)`、`drop(C)`），寫入照 `state.py` 現有的原子寫入（暫存檔＋`os.replace`）；同一身分兩個行程同時寫，後寫的蓋掉先寫的，最壞多一張孤兒頁。
  - 介面：`rollover` 定義協定 `PendingPages`（同三個方法），`PageWriter` 依賴協定；`cli` 注入 `state.PendingPageStore`，測試注入記憶體版本。`rollover` 不 import `state`，依賴邊不變（§8.2）。
  - 紀錄的 `backend` 或 `from` 與這一次不同 → 當作沒有紀錄，並覆寫（舊的那張成為孤兒）。
  - S-3（別人已經換頁）→ 刪掉 C 的紀錄；紀錄裡那張頁成為孤兒（最多一張）。
  - 紀錄檔讀不出或寫不進 → 當作沒有紀錄，stderr 不另外警告，照常往下做（失去的只是上界，不影響正確性）。
- 上界：同一個身分、同一個 `(C, P)`，孤兒頁最多 1 張，外加每一次「確定不能用」（S-6 回 `False`：頁被刪、或標題被改）各 1 張；暫時性錯誤不刪紀錄（S-6′），不增加孤兒頁；狀態目錄遺失（例如容器重建）時，每遺失一次最多再多 1 張。多個身分各自計。
- 註冊簿留言的上界（第 8 版，S-4′）：同一個身分、同一筆紀錄 `(C, P, N)`，自動寫入貼出的 `rollover` 最多 1 次成功（`post_with_retries` 的重試可能多貼一則相同的），之後被拒也不再貼；每一次 PMO 明確執行的 `channel rollover` 再多 1 次。紀錄換了（S-6 刪掉、狀態目錄遺失）就是新的 N，各再算 1 次。
- 為什麼不在 backend 上找「標題＝C 而沒註冊的頁」來重用：要列出 repo 的 issue（數百張、多頁），每次 S-4 都要付；而且找到的頁可能是別的寫者正要註冊的，重用會造成兩個寫者註冊同一頁。本機紀錄只重用自己開的頁。
- 為什麼放 `state.py`：狀態目錄的檔案、鎖與原子寫入都在那裡；`state` 只存取、不解讀（`rollover` 決定何時用它）。

### 1.4 競態交錯（逐一走過）

A、B 是兩個 ≥PR-3 的寫者，P 在 2400 以上；「讀者」是 ≥PR-2 的讀者。

| # | 交錯 | 結果 |
|---|---|---|
| I-1 | A、B 都在 S-1 讀到「沒有下一頁」，各開 N_A、N_B；A 在 t1 貼、B 在 t2>t1 貼；兩人的 S-8 都讀到兩則 | 重建：N_A 現頁，N_B 旁頁。兩人都寫 N_A（或照 A 的窗內規則寫 P）。N_B 是空的 |
| I-2 | 同 I-1，但 B 的 S-8 列表有延遲，沒看到 A 那則 | B 認為 N_B 是現頁，寫進去。之後的設定檔有 N_B（旁頁）。讀者的游標 `pages` 不含 N_B，所以 N_B 被**從頭讀**（§2.1），b1 照樣送達。審查 H-1 的情境在這裡關掉 |
| I-3 | A 的 S-8 沒看到 B 那則 | B 的 `created_at` 比較晚，A 本來就是贏家，結果與 I-1 相同 |
| I-4 | A 已經貼了，B 才進 S-1 | B 在 S-3 拿到 N_A，`open_page` 0 次 |
| I-5 | A 已經貼了，但 B 的 S-1 列表有延遲 | B 開 N_B、貼；S-8 看到 A → 寫 N_A；看不到 → 同 I-2 |
| I-6 | 兩則 `rollover` 同一秒 | 依 id 決勝；S-8 用同一個排序，與 workflow 一致 |
| I-7 | B 的 S-8 列表裡沒有 B 自己那則 | 自己那則已併進去，結果與看得到時相同 |
| I-8 | 寫者 W 的設定檔過期：P 早已換頁，W 不知道 | P ≥2400 → W-8／W-9 → S-3 拿到重算實體 → A：P 不在窗內 → 寫新頁 |
| I-9 | 「舊頁先寫滿」期間：≥PR-3 寫者寫 P；<PR-3 但設定檔已落地的寫者寫 S；某讀者的設定檔還沒落地，只讀 P，游標前進 | 讀者落地後，S 不在它的 `pages` 裡，從頭讀，S 上的訊息照樣送達。這一格在第 1 版是漏的（第 1 版靠「id 全站遞增」，但讀者先前已越過 S 上較早的 id） |
| I-10 | 退役頁 R 寫到 2490 的邊界，兩個寫者同時寫 R 與現頁 | 訊息交錯在兩頁；讀者兩頁都讀，合併後依 id 排序 |

結論：

- 不重複：每則訊息只在一頁上，comment id 全域唯一。
- 不丟：讀者那一側，靠「沒讀過的頁從頭讀」。
- 旁頁幾乎是空的：寫者那一側，靠 S-8。
- 剩下會漏的情況列在 §2.3。

- 不在舊頁貼「搶頁」標記：那要再讀一次舊頁、還有複寫延遲的問題，窗只會縮小、不會消失，又多吃舊頁的額度。

### 1.5 舊頁封存

建議**不鎖、不關、不加 label**（Q-5）：

- 鎖：channel token 是擁有者的，GitHub 允許有 push 權限的人在鎖住的 issue 留言，所以擋不住我們自己的寫者，只會誤導人。
- 關：`channel delete` 用「關閉」表示刪除，關退役頁會讓人誤會頻道被刪了。
- 封存靠兩件事：channels.json 的 `retired`，以及新頁本文的交互參照。

## 2. 讀者：跨頁接續

### 2.1 游標

- `state.py` 的游標以「目標 key」為準：`channel:<C>`、`to:<我>:<C>`、`mention:<我>:<C>`。值是 `{position, at, seen}`，position＝comment id。
  - key 裡沒有 issue 編號，所以**換頁不必搬游標**。
- **新增選填的 `pages`：每一頁自己的游標**（第 4 版改；裁定 A-10，§8.4）。
  - `Cursor.pages: dict[int, Cursor] | None`，鍵是 issue 編號，值是那一頁的游標（`position`／`at`／`seen` 三欄，它自己的 `pages` 恆為 `None`）。`None`＝舊檔沒有這個鍵。
  - 語意：鍵 p 在 `pages` 裡＝「p 上、`pages[p]` 以前的訊息已經送過」。鍵不在＝這個游標沒讀過 p。
  - 頁的游標用的就是單頁的現行三個函式（`cursor_since`、`comment_is_new`、`advance_cursor`），不另立規則。`advance_cursor` 吃的 entries 是那一頁上**所有可解析的訊息**（不只符合 scope 的，`github.read` 現行就是這樣），所以每一頁的游標只靠它自己的訊息前進，不依賴別頁，也不依賴跨 issue 的 id 遞增（§10 第 4 項）。
  - **主游標**（`position`／`at`／`seen`）照舊存在，＝`advance_cursor(上一個主游標, 各頁 entries 的聯集)`。用途只有三個：`pages_to_read` 的寬限判斷（`cursor.at`）、`pages` 是 `None` 時每一頁的起點、舊版讀檔（舊版只看這三欄）。
  - 舊版只讀 `position`／`at`／`seen`（`state.py` 的 `_load`），看到多出來的鍵會忽略。舊版的 `save_many` 會把檔案裡**每一個** key 重新序列化成三欄，所以舊版存任何一個 target，整個檔案所有 key 的 `pages` 都會丟掉，下一次新版讀到就當作 `None`（範圍見 §6.2 的「同一身分不得有未升級的安裝」）。
- 要讀哪幾頁：`backend.pages_to_read(entity, cursor) -> tuple[Entity, ...]`，純函式，回傳**單頁實體**（`page_entity`，`retired=()`）：
  - 現頁一定讀；
  - 退役頁 r（含旁頁）在 `cursor.at < r.until + RETIRED_READ_GRACE` 時才讀（`cursor` 是主游標）；
  - **新游標**（`cursor.at is None`）：退役頁 r 在 `r.until + RETIRED_READ_GRACE > U` 時才讀，U＝`retired` 裡最大的 `until`（第 5 版，重審 L-2，裁定見 §8.4 的 A-12）。也就是把新游標當成「時間停在最近一次換頁」的讀者：最近一次換頁時還在任何讀者寬限期內的頁都讀，更早退役的不讀。最新的那一張退役頁一定在內。只看實體，仍是純函式；
  - 順序：退役頁（舊的在前）再現頁。
  - `--since`（只有 `at` 的游標）用第一條（`at` 不是 `None`），所以 `--since` 早於換頁時會讀到舊頁。
- **每一頁的起點**：`backend.page_base(cursor, issue) -> Cursor`，純函式，讀與 `check` 都用它（C-10、C-11）：
  - `cursor.pages` 不是 `None`、含這一頁 → `cursor.pages[issue]`；
  - `cursor.pages` 不是 `None`、不含這一頁 → `Cursor()`（空游標）；
  - `cursor.pages` 是 `None`（舊檔、新游標、`--since`）→ 主游標本身（去掉 `pages`）。
  - 每一頁照**單頁的現行規則**用它的起點讀：`since=cursor_since(起點)`、新不新由 `comment_is_new(…, 起點)` 決定。
- **從頭讀**：起點是空游標（`position` 與 `at` 都是 `None`）的頁。三種來源：沒讀過的頁（`pages` 不含它）、讀過但上面還沒有任何訊息的頁（新頁）、新游標（`pages` 是 `None` 而主游標是空的）。單頁現行規則對空游標本來就是「不帶 `since`、全部算新」，所以讀的時候不必另寫一條路徑；`check` 對這種頁另有掃描窗（§2.2）。
  - 理由：這個游標從沒讀過這一頁，上面的訊息不可能送過。頁在寫入前一定已經註冊（§1.3），所以從頭讀就是從註冊後的第一則讀起。
  - 新身分的廣播下限（#224）：同一次 `read` 的所有頁共用同一個 `floor`，取第一個列表回應的 `Date`（重審 L-3）；不是每頁各取一個。
- **讀完的更新**（重審 H-1、M-1、A-7、A-10）：`backend.pages_after_read(prev, to_read, entries_by_page) -> dict[int, Cursor]`，純函式，與 `pages_to_read`、`page_base` 放在一起；`backend.read` 是唯一的呼叫者。
  - `entries_by_page` 只含本輪**讀成功**的頁（值可以是空的 list）；被略過的頁不在裡面。
  - 結果＝`{p: advance_cursor(page_base(prev, p), entries_by_page.get(p, [])) for p in to_read}`。這一行涵蓋全部情況：
    - 讀成功 → 那一頁的游標照單頁規則前進（`advance_cursor` 在 entries 空時回傳原值）；
    - **被略過** → 照舊，也就是自然凍結在最後一次讀成功的位置。恢復後從那裡接續：已送過的不再送，略過期間新到的照樣送；
    - 被略過、而且從沒讀過 → 存成空游標，恢復後從頭讀（與不存等價）；
    - **`prev.pages` 是 `None` 時被略過** → 存成 `prev` 的主游標（`page_base` 的第三種），與讀取時「每一頁都以主游標為起點」一致，恢復後不整頁重送（重審 M-1）；
    - 不在 `to_read` 的頁（主游標過了寬限期，或已不在設定檔）→ 不在結果裡，等於移除。主游標時間只會前進（`--since` 不存游標），所以移除的頁不會再回到 `to_read`。
      - 「主游標時間只會前進」的前提（重審 nit-1，推論）：`advance_cursor` 的 `at` 是最大 id 那則的時間。跨 issue 的 id 與 `created_at` 若不同向（§10 第 4 項，未驗），新的最大 id 那則可能比舊的 `at` 早，`at` 會往回走；剛好跨過某頁 `until + 48h` 的邊界時，已移除的頁會回到 `to_read`，而它不在 `pages` 裡，所以**從頭讀**（整頁重送，不會漏）。要同時滿足「id 與時間反向」與「`at` 落在邊界附近」，機率很低；不另加規則，列在 §10 第 4 項。
  - 新游標＝`dataclasses.replace(advance_cursor(prev, 各頁 entries 的聯集), pages=pages_after_read(...))`。沒有新 entries 也要換上新的 `pages`（`advance_cursor` 在 entries 空時回傳 `previous`，帶的是舊的 `pages`）。
  - 一次 `read` 裡任何一頁往外丟錯（現頁的任何錯誤、退役頁的 rate limit／網路／5xx），整次 `read` 失敗、游標不存，`pages` 也不變（照現行「讀失敗不動游標」）。
  - 單頁頻道：`pages` 恆為 `{現頁: 與主游標相同的值}`，每一次讀的請求與回傳和 PR-2 之前相同（第一次讀時 `pages` 是 `None`，起點就是主游標；之後起點是 `pages[現頁]`，兩者一起前進、值相同）。
- **序列化**（裁定 A-11，§8.4；重審 M-2、nit-3）：
  - `pages` 是 `None` → 不寫 `pages` 鍵（`test_angareion_state.py` 那些只存 `Cursor()` 的測試照舊）；
  - 否則寫 `"pages": {"<issue>": {"position": …, "at": …, "seen": [...]}}`：鍵是十進位字串、依數值遞增；值與主游標同一種三欄格式（`None` 照主游標現行寫成 `null`）。
  - 讀檔的壞資料（第 5 版依重審 nit-2 改；裁定見 §8.4 的 A-13）。都不報錯、不警告：
    - `pages` 不是物件 → `None`（每一頁以主游標為起點）；
    - 某一筆的鍵不符 `^[1-9][0-9]*$` → 丟掉那一筆。本程式寫出的鍵一定符合，所以這種鍵對不到任何一頁；
    - 鍵合格、值不是物件 → 那一頁的值換成**主游標的三欄**（`position`／`at`／`seen` 的複本）。這與 `pages` 是 `None` 時那一頁的起點相同：可能漏，不會整頁重送。不是丟掉（丟掉＝從頭讀，現頁最多 2500 則全部重送）；
    - 某一筆的 `seen` 照主游標現行的防禦（非字串項丟掉、不是 list 就空）。
  - `save_many` 對檔案裡**每一個** key 都照這個格式寫出 `pages`（不只這一次更新的 key）。存別的 key 時，這個 key 的 `pages` 原樣保留（重審 L-1；舊版的 `save_many` 就是在這裡洗掉 `pages`，見上面）。
- `pages` 是 `None` 的舊游標，只會出現在升級的那一刻，或同一身分被未升級的安裝存過游標（§6.2 禁止）。手動換頁與 PR-3 都要等所有席位升級（§6.2），所以升級時頻道只有一頁，`None` 等於「讀過現頁、讀到主游標」，不會誤判。
- 為什麼寬限期用游標而不用本機時鐘：D4 只讓後端時間決定新舊。讀者的主游標時間過了 `until+48h`，就表示它已經讀到換頁兩天之後的訊息；而退役頁的寫入都在 `until+24h` 之前（§1.3 的 W-1），早已被讀過。
- 沒有新訊息的冷頻道，主游標停在換頁前，會一直多讀一頁。代價是每次讀多一個請求，直到下一則訊息出現；可以接受。
- 例（第 3 版 `held` 要解的情況，在這一版自然成立）：R 在 10:00 收到 r1；10:05 讀者讀（R 404，讀到 N 在 10:04 的 n1，主游標到 10:04）；10:10 R 恢復。R 的游標在 10:05 那一輪沒動，仍是 r1 之前的位置，所以用它讀 R 會拿到 r1；主游標的前進不影響 R。

### 2.2 各介面

| 介面 | 跨頁行為 |
|---|---|
| `read`（`backend.read`） | 對 `pages_to_read` 的每一頁，以 `page_base` 為起點照單頁規則列表，把符合的訊息合併，依 comment id 排序（同一頻道內＝到達順序）。各頁的 entries 分頁記下（`pages_after_read`），聯集推進主游標。`--since` 照舊不動游標。新身分的廣播下限：同一次 `read` 的所有頁共用一個，取第一個列表回應的 `Date`（#224、重審 L-3） |
| 讀退役頁失敗 | 404、410、或不是 rate limit 的 403 → 略過這一頁，stderr 一行警告；那一頁的游標不動（§2.1 的 `pages_after_read`）：讀過的頁恢復後從原處接續（不重送、不漏），沒讀過的頁恢復後從頭讀。其他錯誤（rate limit、網路、5xx）照舊往外丟。現頁的任何錯誤照舊往外丟（C-22） |
| `wait`／`check` | 每一頁以 `page_base` 為起點照單頁規則判斷，任一頁有新訊息就是 True（從頭讀的頁上，有任何符合 scope 的訊息就是 True）。ETag 與 `scan_since` 的 key 改成每頁一份（`repo#issue\|scope`，就是現在的 `_etag_key` 套在頁上），ETag 紀錄裡的 `cursor` 是那一頁的起點，單頁的條件請求規則不變。每一頁的起點只在那一頁有新訊息時才變，所以別頁前進不會讓這一頁的 ETag 紀錄失效。退役頁在 `since=at-60s` 下通常是空的、不滿一頁，所以 ETag 可以存，之後是 304。讀退役頁失敗時，照上一列略過，但不警告（`wait` 每 15 秒一次）。`wait` 輪詢中仍不 fetch（spec）。從頭讀的頁的掃描成本見下面 |
| 「給我」／「@ 我」跨頻道合併 | `client.Angareion.read` 照舊依 `(messages.received_at_key(received_at), channel, id)` 排序（D-31）。同一頻道內跨頁依 comment id，兩者一致：同一頻道的時間與 id 同向，同秒內由 id 決勝，與單頁相同 |
| `log` | `LogCache` v3：同一個頻道一個檔，`pages: {"<issue>": {last_created_at, etag}}`，**所有頁**（現頁＋全部退役頁，不看寬限期），訊息以 comment id 合併。v2 檔整份重建（照 #468 升版的先例）；`repo` 變了也重建；不在 entity 裡的頁，它的訊息丟掉。`enumerate_conditional` 的介面不變，由 `LogCache` 逐頁呼叫（每頁一個單頁實體）。退役頁讀失敗照 C-22 略過並警告 |
| `decision list` | 經 `LogCache.sync`，自動看到全部的頁 |
| `decision ask`／`update`／`submit`… | 寫入走 `PageWriter`（§1.3） |

**`check` 對從頭讀的頁的請求數上界**（重審 M-1、A-9；第 4 版加上新游標，重審 M-3）：

- 問題：`check` 不存游標，所以不更新 `pages`。從頭讀的頁（§2.1：起點是空游標的頁，包括沒讀過的頁與**新游標**的每一頁）每次都從頭掃；現行的 `_scan_since` 只在找到符合 scope 的留言時才設，頁上全是給別人的訊息時就一直沒有掃描窗。一張 2000 則的頁，每 15 秒 20 個請求，直到有人跑 `read`。新游標（從沒 `read` 過的身分做 `wait`）會對 `to_read` 裡的每一張退役頁這樣掃（第 5 版起只有最近一次換頁 48 小時內退役的頁，§2.1），請求數乘上頁數。
- 規則：**只對「從頭讀」模式的頁**（起點的 `position` 與 `at` 都是 `None`），第一次整頁掃描乾淨（沒有符合 scope 的新訊息）之後，把這一頁**所有可解析的訊息**（不論是否符合 scope；與 `read` 推進游標的 entries 同一個集合，非訊息的留言不算，重審 nit-2）中最新的 `created_at` 減 60 秒記進 `_scan_since[該頁的 key]`。之後同一個行程的 `check` 對這一頁用 `since=scan_since`，回應通常只有幾則或是空的。頁上一則訊息都沒有（新開的頁）時沒有窗，但整頁掃描就是一個空回應。
- 上界：同一個行程裡，對一張沒讀過、n 則的頁連續 `check` k 次（中間沒有新留言），請求數 ≤ ⌈n/100⌉ ＋ (k−1)。行程重啟後 `_scan_since` 是空的（它本來就只在記憶體），再付一次整頁。
- 為什麼不違反 D2 的掃描窗前提（A-9 的裁定）：
  - 單頁規則（讀過的頁）的 `_scan_since` 不改：仍只由符合 scope 的留言推動。
  - 從頭讀模式下，`scan_since` 的唯一作用是讓 `check` 少掃；它不進游標，`read` 也不看它（`read` 對沒讀過的頁一律從頭讀）。掃描窗若因複寫延遲超過 60 秒而漏看一則較早的留言，結果只是 `check` 晚一點回 True（下一則新留言、或下一次 `read` 就會讀到），不會丟訊息。
  - 用「所有可解析的訊息」而不是「符合 scope 的訊息」推動窗：窗的作用是「這個時刻以前的都掃過了」，與 scope 無關；只用符合 scope 的留言推動，正是這個問題的起因。
  - 這一頁一旦被 `read` 讀到訊息（起點不再是空游標），就回到單頁規則。兩種模式的 `_scan_since` 與 ETag 用不同的 key（從頭讀模式在頁的 key 後面加 `|unread`），所以從頭讀模式推動的窗不會被單頁規則拿去用。
  - 單頁頻道的新游標也走從頭讀模式：第一次 `check` 的請求與結果和現行相同；同一個行程的第二次起帶 `since=scan_since`（現行是每次整頁掃）。這是行為上唯一的單頁差異，只少請求、不改結果（C-11）。

### 2.3 去重與順序（為什麼不會重送、不會漏）

- 同一則留言只在一頁上，comment id 全域唯一，所以合併後不會重複。
- 每一頁的「晚到」（那一頁上 id 較小、較晚才看得到的留言），由那一頁自己游標的 `at - 60s` 視窗與 `seen` 接住，與單頁的複寫延遲同一套；不依賴別頁，也不依賴跨 issue 的 id 遞增（第 4 版）。
- 讀者看到的頁集合變大（設定檔晚到、旁頁晚一步進設定檔）：新加進來的頁從頭讀（§2.1），所以上面 id 比游標小的訊息不會被跳過（§1.4 的 I-2、I-9）。
- **會漏的情況**（已知風險，都列在 §11）：
  1. **設定檔過期的舊版寫者**（<PR-3，`config_pull_interval=0`，或 git fetch 一直失敗）在退役頁的 `until+24h` 之後還寫舊頁。
     - 舊頁沒滿時 POST 會成功；讀者的游標過了 `until+48h` 就不再讀那一頁。
     - 自動換頁發生在 2400，舊頁先寫滿到 2490 後只剩 10 則空間，所以這種漏最多 10 則；之後這些寫者會收到 `ChannelFullError`。
     - 手動或強制換頁（`channel rollover`）可以在更低的則數發生，那時沒有上限。
     - 緩解：部署前提（§6.2：每個席位的 `config_pull_interval` 不是 0，而且 ≤6 小時）；偵測：`channel status` 的 `late` 旗標（C-26）。
     - ≥PR-3 的寫者不會這樣漏：W-8／W-9 會拿到重算實體（I-8）。例外是手動換頁時 P 還不到 2400，這時它們也只靠部署前提。
  2. **退役頁讀不到**（404／410／非 rate-limit 403）整個寬限期：那一頁略過，上面的未讀訊息讀不到。頁恢復之後，只要主游標還在寬限期內就會補回：讀過的頁從它自己沒動過的游標接續（只補略過期間新到的），沒讀過的頁從頭讀（§2.1 的 `pages_after_read`）。兩種都不重送。
  3. **舊版讀者**（<PR-2）：只讀現頁。由部署前提擋住（§6.2）。
  - `log --channel C` 讀所有頁、不看游標，所以 1、2 的訊息照樣查得到。

## 3. `-b` 臨時頻道

- 現況：`team-mylinuxpool-w2-b`（#36）、`w3-b`（#37）已註冊。PMO 用它們發訊；收件不指定頻道，所以照收。
- **不併回成頁**：
  - 那些訊息本文的 `channel` 是 `…-b`；
  - 讀者在 `…-b` 的游標是另一個 key（`to:<我>:…-b`）。併成 w2 的頁之後，w2 的游標沒讀過那一頁，會從頭讀（§2.1），#36 的每一則都會再送一次。
  - 要避免就得改游標或重寫歷史，兩者都不做。
- **收斂**（Q-3，建議）：
  1. PR-2 合上、所有席位都升級之後，PMO 手動換頁（§6）或用 PR-3 的 `channel rollover` 把 w2、w3 換頁。
  2. 發訊改回原名（teams.yml 本來就是原名）。
  3. `-b` 再留 7 天，讓還沒讀的人讀完，之後 `channel delete`。`delete` 會關 issue 並註銷，`log` 之後就查不到 `-b` 的歷史；要保留歷史就不刪（每次讀「給我」多一個請求）。

## 4. 403 分辨

`github.GitHubIssuesBackend._request` 依序判斷。標頭優先；標頭不在時，才看已知的訊息字樣：

1. 403 或 429，而且 `x-ratelimit-remaining: 0` → `RateLimitError`（primary），訊息附 `x-ratelimit-reset` 換成的 UTC 時刻。
2. 403 或 429，而且有 `retry-after`，或 body 的 `message` 含 `secondary rate limit`（不分大小寫）→ `RateLimitError`（secondary），有 `retry-after` 就附秒數。
3. 只在 POST `…/comments`：403 或 422，不是上面兩種 → 用同一個 token GET 一次 issue，看 `comments`（n）：
   - n ≥ `PAGE_COMMENT_LIMIT`（2500）→ `ChannelFullError(channel, n, presumed=False)`；
   - `PAGE_ROLLOVER_AT`（2400）≤ n < 2500 → `ChannelFullError(channel, n, presumed=True)`（保守處置，見下面）；
   - n < 2400，或這個 GET 失敗 → 一般的 `BackendError`。
4. 其他 403：`BackendError`。

說明：

- 所有新例外都是 `BackendError` 的子類，exit 照舊是 2，照舊不自動重試（spec「後端錯誤」情境）。
- 每一種訊息都帶 HTTP 狀態碼，以及 GitHub 的 `message`。`message` 的處理：單行、≤200 字、遮蔽 token、repo 名換成 `<backend>`、`#<數字>` 換成 `#…`。
  - `RateLimitError` 的訊息保留現行的 `HTTP 403`（或 429）與 `not retrying` 兩段字樣（既有測試 `test_angareion_github.py` 與 `test_angareion_cli.py` 的 403 情境靠它們，C-1）。
  - 不再寫「secondary rate limit?」這個猜測。
- 訊息不出現 repo 與 issue 編號（spec「介面不出現 GitHub repo／issue 的概念」）。訊息格式：
  - `ChannelFullError`：`channel '<C>' is full: its current page has <n> of 2500 messages and the backend refused the write (HTTP <碼>: <message>)`；
  - `presumed=True` 時把 `is full` 換成 `looks full (or the token was refused)`（PR-1 已定，重審 M-5）：PR-1 單獨合上時，2400 以上的頻道若其實是 token 失權，訊息不能只叫人去換頁；`looks full` 字樣保留給測試；
  - PR-3 之後在句尾補 `; run 'angareion channel rollover <C>'`。
- **保守處置（第 3 條的第 2 格）**：上限的計法沒驗過（§10 第 1、2 項）。若真正的上限在 `comments` 還不到 2500 時就生效，只看 2500 就會判不成「滿」，2400～2499 這一段又會變回今天的故障。所以在 2400 以上被拒，就當作滿。
  - 判錯的代價：PR-1 只是訊息說「looks full (or the token was refused)」；PR-3 是多開一頁。
  - 若被拒其實是權限問題，新頁也會被拒，最後一樣是 exit 2。
- 把 GitHub 的 `message` 帶進 `ChannelFullError`，第一次真的撞上限時就有樣本（§10 第 1 項）。

### 4.1 拿到 403 原樣樣本之後要改的地方（第 7 版；現在不改）

2026-11-26 w5 對 `team-mylinuxpool-w2`（頁已滿 2500）寫入，收到 HTTP 403，訊息含「Commenting is disabled」。這是真 GitHub 達上限的第一個實例；原樣樣本（狀態碼、標頭、完整 body、當時 angareion 印出的整行 stderr）由 w5 交給 PM。**在樣本到之前，§4 規則 3 的判斷（再 GET 一次看 `comments`，≥2400 就當作滿）不改**，也不預先寫任何 body 字樣。拿到樣本後依序看：

1. **先判斷要不要改**：看 w5 當時的 stderr。若是 `channel '…' is full: … 2500 of 2500 …`，規則 3 已經判對（`comments` 沒有延遲），**程式不必改**，只把樣本記進 §10 第 1、2 項並關掉這兩項。只有下面兩種情況才要改：
   - stderr 是一般的 `Failed to send …`（GET 失敗，或 `comments` 小於 2400：計數有延遲，或上限不是照 `comments` 算）；
   - 樣本的標頭帶 `retry-after` 或 `x-ratelimit-remaining: 0`（會被規則 1、2 誤判成 rate limit）。
2. **要改的函式**：
   - `src/angareion/github.py` 的 `GitHubIssuesBackend._refused_send`（現在的規則 3）：照舊先 GET 計數，再比對樣本的 `message`（用 `_raw_message(resp)` 的原文，不是遮蔽後的；比對方式照樣本定：完整字串相等或固定前綴，不用寬鬆的子字串）。**相符而且（計數讀得到且 ≥ `PAGE_WARN_AT`（2000），或計數讀不到）**才是 `ChannelFullError(presumed=False)`；計數讀得到就帶計數，讀不到時 `count` 用 `PAGE_COMMENT_LIMIT`（訊息照 §4 的格式）。**相符但計數讀得到而且 < 2000**（新頁、剛開的頁）→ 照現行規則 3，也就是 `WriteRefusedError`（第 8 版，審查中-2）：同一句話若其實是整個 repo 或帳號被禁止留言，新頁也會被拒；不這樣擋，PR-3 會把每一張剛開的空頁都當成滿的，一次 `write` 連開到 3 張（`MAX_RESTARTS`），呼叫端每重試一次再來一輪。門檻用 2000 而不是 2400：樣本相符本身已經是「滿」的強證據，計數只用來排除「頁顯然不可能滿」；而計數延遲（§10 第 3 項）若真的存在，延遲量不會到數百則（推論，樣本到了再看）。不相符照現行規則 3。
   - 若是第 1 點的第二種情況：`GitHubIssuesBackend._rate_limit` 要先排除樣本的 `message`（這是規則 1、2 的順序改動，要再審一次 C-1）。
   - 鎖住的 issue 若回同一句話（§1.5、§10 第 6 項），兩者分不開；計數 ≥2000 時照樣當作滿，因為 PR-3 的處置（換頁）對兩者都對；計數很小時是 `WriteRefusedError`（exit 2，不換頁），見上。
3. **要改的契約**：C-2 加一列：「POST 留言回 403，`message` 等於樣本，而且 GET 計數 ≥2000 或 GET 失敗 → `ChannelFullError`，`presumed is False`；GET 失敗時 `count == 2500`。`message` 等於樣本而計數 <2000 → 照 n <2400 那一格（`WriteRefusedError`）」。C-1 只在第 2 點第二項成立時改。C-21 不受影響（樣本的 `message` 照舊經 `_scrub`）。要不要把「計數很小、句子相同」是否代表整個 repo 禁止留言，記進 §10 第 6 項的未驗清單：記（第 6 項已補一句）。
4. **要改的測試**：`tests/unit/test_angareion_pr1_contract.py` 的 C-2 一組加四個（樣本＋計數 2500、樣本＋計數 2100（<2400 也當滿）、樣本＋GET 500、樣本＋計數 0 → `WriteRefusedError`），以及一個反例（相近但不相同的 message 照舊走計數）；樣本放成測試檔裡的常數（先依 C-2 的規則遮蔽 repo 名與 `#數字`）。`tests/unit/test_angareion_github.py` 既有的 403 測試不改。
5. **誰改**：這是 d 或併進 3a（若 3a 還沒 merge），由 PM 決定；它不依賴 PR-3 的任何部分，PR-3 也不依賴它（PR-3 只看 `ChannelFullError` 這個型別）。

## 5. 告警

- **誰量**：channel token 這一側。
  - workflow 量不到：AiCompany-channel 是私有 repo，MyAiStorage 的 `GITHUB_TOKEN` 讀不到；要量得加一把 PAT secret，這不做。
  - PR-1 先在 `send` 加一次 `page_size`：每次 send 多 1 個請求；送出量遠小於輪詢量，可以接受。PR-3 的 `PageWriter` 本來就要它，不再多花。
  - `angareion channel status [NAME…] [--json]` 是唯讀的計數：每個頻道 1 個請求，加註冊簿 1 個，不動游標。PR-2 起，有退役頁的頻道每頁再多 1 個（C-26）。
- **怎麼告警**：
  - 寫的人：`send` 在 ≥2000 時在 stderr 印一行 `Warning`（stdout、exit 都不變；PR-3 之後的語意見 C-20）。
  - PMO：建議 `pmo tick` 每 6 小時（以 pmo 的 `due` 排）跑一次 `angareion channel status --json`，把 ≥2000 的頻道與有旗標的頻道列在 tick 輸出（Q-4；這是 pmo 的改動，另開 d）。在那之前由 PMO 手動跑。
  - 不新增常駐程序，也不從 workflow 發訊息。
- 註冊簿 MyAiStorage#47 本身也有 2500 則的上限，現在 52 則。
  - 每次換頁加 1 則；競態時加 2 則；`post_with_retries` 重試時可能多 1 則。
  - 每一種不同的回報各加 1 則（已去重，C-25）。
  - `channel status` 一併列出註冊簿的留言數（`registry` 一列；`RegistryClient.comment_count()`，GET issue）。

## 6. 緊急緩解、PR 切分與部署前提

時程上的事實：

- w2 已滿，已經有 `-b`；w3 約剩 65 則，已經有 `-b`；
- m1 約剩 260 則；w4 約剩 550 則。

| PR | 內容 | 合上後 main | 契約 |
|---|---|---|---|
| **PR-1**（急） | 403 分辨（`RateLimitError`、`ChannelFullError`）；`page_size`（GitHub＋Fake）；`cmd_send` 的 ≥2000 警告；`channel_cli.py`（搬 `create`／`delete`，加 `status`）；`RegistryCounter`／`comment_count` | 只有錯誤訊息變清楚、多一個唯讀指令；寫入多 1 個 GET（失敗不擋） | C-1～C-5 |
| **PR-2**（切成 2a／2b／2c，§6.3） | `retired` 的解析；`registry_rules.py`（純規則）；`rollover` 動作的解析與重建；回報去重；`pages_to_read`；游標 `pages`、`page_base` 與 `pages_after_read`；GitHub／Fake 的跨頁 `read`／`check`（含從頭讀模式的掃描窗）；退役頁失敗略過；`LogCache` v3；Fake 依頁存留言；`--capabilities`；`channel status` 的頁旗標；`channel delete` 的過期檢查 | 沒有 `retired` 時輸出不變（既有測試不改預期）；重建對既有註冊簿逐位元組相同 | C-6～C-15、C-22～C-26 |
| **PR-3**（切成 3-0／3a／3b／3c，§6.4） | `rollover.PageWriter`／`ensure_successor`；`open_page`；待註冊紀錄（`state.PendingPageStore`）；`post_with_retries` 回傳留言；`send`、`decision` 的寫入改走它；`channel rollover NAME`；先行的 `fake_backend.py`（D-35） | 自動換頁上線（3c）；merge 前提見 §6.2 | C-16～C-21 |

### 6.1 PR-1 可以先實作（不等 PR-2／PR-3）

**已實作並 merge**（#544，main `e324857`）。實作另外公開了兩個 `backend` 的小函式，PR-2／PR-3 照用、不另寫：`page_level(count) -> str`（C-5 的等級）與 `http_cause(status, detail) -> str`（訊息尾的 ` (HTTP <碼>: <message>)`）。§4 的判斷順序與實作的 `GitHubIssuesBackend._rate_limit` 一致（`x-ratelimit-remaining: 0` → primary；否則有 `retry-after` 或訊息字樣 → secondary）。

PR-1 的範圍與契約到此已定，不依賴 PR-2／PR-3 的任何細節：

- **契約**：C-1～C-5（本版的文字為準；相對第 1 版的修訂見 §12，第 3 版只同步實作已定的細節，見 §13）。
- **動到的檔**：`backend.py`（`ChannelFullError`、`RateLimitError`、`PageBackend`（`page_size`、`page_size_at`）、頁的常數、Fake 的 `page_size`／`page_size_at`／`set_page_size`／`comment_limit`）、`github.py`（§4、`page_size`、`page_size_at`）、`registry.py`（`RegistryCounter`、`comment_count`、`FakeRegistryClient.comment_count`）、`cli.py`（`cmd_send` 的 `page_size` 與警告、派發）、新 `channel_cli.py`。`client.py` 在 PR-1 不動。
- **不在 PR-1**：`retired`、`Cursor.pages`、`open_page`、`registry_rules`、`PageWriter`。
  - `PageBackend` 在 PR-1 只有 `page_size`／`page_size_at`，`open_page` 由 PR-3 放進另一個協定 `PageOpener`（A-5）。PR-1 不需要預留。
  - `channel status` 在 PR-1 的「頁數」恆為 1，旗標欄恆為空；PR-2 才補上（C-26）。
- **PR-1 實作已定的 7 點**（PM 在 PR-1 的派工寫死，這一版照實作同步進文件；不改 C-1～C-5 的意義）：
  1. **Fake 的頁大小**：`FakeBackend` 不改既有的存放方式（單一 list、依 `channel` 篩選）；另有 `(repo, issue) -> int` 的覆寫表。`page_size` 有覆寫值就回它，沒有就回該實體的實際留言數；`set_page_size(issue, n, repo=…)` 設覆寫值。`send` 在 `page_size >= comment_limit` 時丟 `ChannelFullError`。依頁存留言是 PR-2 的 C-15。
  2. **警告的位置**：`cli.cmd_send`，在 `app.send` 之前。`client.Angareion` 沒有警告的注入點，而 `client` 不能 import `cli`（會成環）。所以 PR-1 的 `decision` 寫入沒有警告（重審 L-6；C-20 在 PR-3 統一）。
  3. **後端沒有 `page_size`**：`PageBackend` 是 `runtime_checkable` 的協定；`cmd_send` 拿到的後端不是 `PageBackend`（例如測試自己寫的假物件）→ 略過，不警告、照常寫入。
  4. **註冊簿的計數**：新協定 `RegistryCounter`（`comment_count() -> int`）；`RegistryClient` 與 `FakeRegistryClient` 都實作。`channel status` 拿到的註冊簿接縫不是 `RegistryCounter`（例如既有的 `ApplyingRegistry`）→ `registry` 列的等級是 `unknown`，照 C-5 的規則 exit 2。
  5. **`channel status --json` 的欄位**：每列是物件，鍵是 `name`（str）、`count`（int；`unknown` 時是 `null`）、`limit`（int，2500）、`level`（`ok`／`warn`／`rollover`／`full`／`unknown`）、`pages`（int）、`flags`（str 的陣列）。註冊簿那一列 `name` 是 `"registry"`。
  6. **`channel_cli.py` 的 import**：照 `decision_cli.py` 的做法。`cli.py` import `channel_cli`，`channel_cli` 不 import `cli`；需要的 `EXIT_*`、`mask_token` 等從它們原本的模組 import，不從 `cli`。`main` 的「channel needs an action」訊息跟著加 `status`。
  7. **C-21 的泛用掃描器屬 PR-3**。PR-1 只做 C-2 的測試：三種訊息都不含 `#\d+` 與 repo 名。
- **建議實作與審查的重點**：C-1 的字樣保留（L-5）、C-2 的 2400 門檻與 `message` 的遮蔽、C-2 的「不含編號與 repo 名」測試。

### 6.2 部署前提與盤點（M-4、M-5）

- **能力標記**：PR-2 起新增旗標 `angareion --capabilities`：stdout 恰好一行，是空白分隔的能力字樣（PR-2：`paged-read`；PR-3：`paged-read paged-write`），exit 0。
  - 能力字樣只增不改。常數放 `cli.py`（`CAPABILITIES`），測試鎖住這一行的格式。
  - `--version` 不變（仍是一行）。第 2 版把能力放在 `--version` 的第二行，會讓既有的 `test_angareion_version.py`（`out.count("\n") == 1`、整段只有一行的 regex）轉紅，與 C-15 的「既有測試不弱化」矛盾（重審 M-3）。改成獨立旗標，既有測試與 spec 的 `--version` 都不動；舊版跑 `--capabilities` 是用法錯誤，exit 非 0（現行是 1：`cli._Parser.error` 改丟 `UsageError`，`main` 轉成 `EXIT_USAGE`；重審 L-1），盤點只認「exit 0 而且輸出含 `paged-read`」，不看結束碼的值，不會誤判。
  - 理由：套件版本固定是 `0.1.0`，非 editable 安裝時 sha 是 `unknown`；遠端席位也沒有 repo 可以做 `merge-base`。所以「≥PR-2 的 commit」在機制上判斷不了，要一個明確的標記。
- **盤點規則**：以「安裝」為單位（不是以身分）：每台機器、每個 worker 映像，各有一份 angareion。
  - 名單：Mac（PMO 與 Mac 上的 PM）、每一台 worker（PM 與隊員共用映像裡的 angareion）、其他會呼叫 angareion 的程式（aicomm、pmo）所在的機器。
  - 每一份：
    - 跑 `angareion --capabilities`，exit 0 而且輸出含 `paged-read`；
    - angareion 設定檔的 `config_pull_interval` 不是 0，而且 ≤21600（6 小時，遠小於 `PAGE_FILL_WINDOW`）。
  - 盤點不到的一份，當作沒升級。
  - **同一個身分不得有未升級的安裝**（重審 L-1）：舊版存游標時會洗掉整個游標檔所有 key 的 `pages`（§2.1），已換頁頻道的游標就會回到 `None`、每一頁都以主游標為起點讀，I-2／I-9 的保護失效（只會漏，不會重送）。所以盤點除了每一份安裝，還要確認每個身分（`ANGAREION_IDENTITY`）用到的狀態目錄只被已升級的安裝寫入；同一個狀態目錄被兩份安裝共用時，兩份都要升級。
  - worker 映像裡的 angareion 怎麼安裝、跟 main 的哪一版，我沒查；由 PMO 經映像的工單確認。
- **誰盤點**：PMO。
  - 各 worker 由該機的 PM 跑指令回報，結果貼在換頁的工單（手動換頁）或 PR-3 的 PR（merge 前）。
  - 第一次手動換頁之前要做一次，PR-3 merge 之前再做一次。
- **何時需要**：
  - 手動換頁（PR-2 之後）；
  - PR-3 merge；
  - 之後新加入的機器或映像，加入前就要符合。

- **PR-2 之後就能手動換頁**（不等 PR-3；要先完成 §6.2 的盤點）：
  1. PMO 用本人的 gh 在 AiCompany-channel 開 issue，標題＝頻道名。
  2. 在註冊簿貼 `{"schema": 1, "action": "rollover", "channel": "<C>", "backend": "aicompany-channel", "issue": <新>, "from": <舊>}`，等 workflow 落地。
  3. 寫者（≥PR-2 或舊版）落地後寫新頁；≥PR-2 的讀者兩頁都讀，新頁從頭讀。
  - 手動換頁沒有「舊頁先寫滿」（舊版寫者只寫現頁），等於 Q-2 的 A；讀者的設定檔還沒落地時，會晚最多一個拉取週期才看到新頁的訊息，但不會漏（§2.1）。
- **PR-1 能不能只加一個手動換頁指令？** 不行。換頁的另一半是讀者要跟著讀兩頁，那就是 PR-2；只有 PR-1 時，換頁等同開一個 `-b`。
- **PR-1 之前與 PR-2 之前**：照 PMO 現行做法，頻道剩不到約 50 則時開 `-b`。m1、w4 由 PMO 用 `channel status` 或 PMO 的 gh 盯。PR-1 本身不能延後或吸收任何訊息，它的價值是錯誤訊息正確、PMO 看得到則數。

### 6.3 PR-2 的切分：2a、2b、2c（第 5 版）

PR-1 已 merge（#544，main `e324857`）。PR-2 切成三個 PR，每一個都可以單獨合併；介面簽名寫到實作者不必再猜的程度。PR 內的程式怎麼排由實作決定，但**簽名、模組歸屬與依賴方向照這一節**；要偏離就先問架構師。

**順序**：2a 與 2b 互不依賴，可以並行開工、任意順序合併。2c 的程式只依賴 2a（型別與純函式），不依賴 2b。2b 與 2c 誰先合都安全（見下一段），但**手動換頁要兩者都在 main**（2b 讓重建認得 `rollover`，2c 讓讀者跨頁），再加上 §6.2 的盤點。建議順序：2a → 2b → 2c（2b 小，先合可以早點把回報去重帶上線）。

**每一步的安全性**：手動換頁（§6.2）與 PR-3 都以「每份安裝的 `--capabilities` 含 `paged-read`」為前提，而 `paged-read` 在 2c 才出現。
- 2a、2b 已合、2c 未合：重建認得 `rollover`，但沒有人會貼（盤點不會過）。2b 的 PR 本文要寫明「2c merge 而且盤點通過之前，不得在註冊簿貼 `rollover`」。萬一有人貼了，讀者只讀新的現頁，舊頁剩下的未讀讀不到（§2.3 第 3 項的情況），`log` 查得到。
- 2a、2c 已合、2b 未合：讀者會跨頁，但重建不認得 `rollover`（照現行回報 `unknown fields: from` 並略過），channels.json 不會有 `retired`，沒有影響。

#### 2a：頁的型別、純函式、游標序列化、`retired` 解析、Fake 依頁存放（不接上讀取）

- **契約**：C-6、C-9、C-10 的純函式部分（`page_base`、`pages_after_read`、`cursor_after_read` 的單元測試）、C-15 的 Fake 依頁存放（第一、二點；既有測試一個都不改）、C-23 的 `state` 序列化部分（含 L-1 與 A-13 的案例）。
- **檔案**：`src/angareion/backend.py`、`src/angareion/state.py`、`src/angareion/channel_config.py`；測試新檔 `tests/unit/test_angareion_pages.py`（測試名帶契約編號，例如 `test_c9_new_cursor_reads_only_recent_retired`）。
- **介面**（`backend.py`）：

  ```python
  RETIRED_READ_GRACE = timedelta(hours=48)
  PAGE_FILL_WINDOW = timedelta(hours=24)   # §1.3 的表；2a 只定義，2c（C-26 的 late）與 PR-3 才用

  @dataclass(frozen=True)
  class RetiredPage:
      issue: int
      until: str            # RFC 3339（channels.json 原字串）；比較時用 parse_backend_time
      side: bool = False

  @dataclass(frozen=True)
  class Entity:            # 既有；只加最後一個欄位
      channel: str
      backend: str
      repo: str
      issue: int
      retired: tuple[RetiredPage, ...] = ()

  @dataclass
  class Cursor:            # 既有；只加最後一個欄位，== 含 pages（A-11）
      position: str | None = None
      at: str | None = None
      seen: list[str] = field(default_factory=list)
      pages: dict[int, "Cursor"] | None = None

  def page_entity(entity: Entity, issue: int) -> Entity
      # dataclasses.replace(entity, issue=issue, retired=())
  def pages_to_read(entity: Entity, cursor: Cursor) -> tuple[Entity, ...]
      # §2.1；退役頁依 entity.retired 的順序在前，現頁最後；元素都是 page_entity
  def page_base(cursor: Cursor, issue: int) -> Cursor
      # §2.1 的三種；回傳值的 pages 恆為 None
  def pages_after_read(
      prev: Cursor,
      to_read: Sequence[int],
      entries_by_page: Mapping[int, list[tuple[int, str]]],
  ) -> dict[int, Cursor]
      # {p: advance_cursor(page_base(prev, p), entries_by_page.get(p, [])) for p in to_read}
  def cursor_after_read(
      prev: Cursor,
      to_read: Sequence[int],
      entries_by_page: Mapping[int, list[tuple[int, str]]],
  ) -> Cursor
      # replace(advance_cursor(prev 去掉 pages, 各頁 entries 的聯集), pages=pages_after_read(...))
  ```

  - `cursor_after_read` 是第 5 版新增的名字，內容就是 §2.1「新游標＝…」那一行；2c 的 GitHub 與 Fake 兩邊都呼叫它，不各寫一份。
  - `FakeBackend.deliver(entity, message, *, comment_id=None, created_at=None, page: int | None = None)`：存在 `(entity.repo, page or entity.issue)`。`read`／`check`／`enumerate_conditional`／`page_size` 只看 `(entity.repo, entity.issue)` 那一頁，**並照舊**以 `message.channel == entity.channel` 篩選（既有測試可能讓不同頻道共用同一個編號，兩個條件都留，既有測試不改）。2a 的 Fake 仍是單頁讀取；跨頁是 2c。
- **介面**（`state.py`）：公開方法不變（`cursor`、`save`、`save_many`、`has_to_me_cursor`）。私有的 `_pages_from_json(raw: Any, main: Cursor) -> dict[int, Cursor] | None` 與 `_pages_to_json(pages: dict[int, Cursor]) -> dict[str, dict]` 照 §2.1 的序列化與壞資料規則；`save_many` 對每一個 key 都經 `_pages_to_json`。
- **介面**（`channel_config.py`）：`ChannelRef` 加 `retired: tuple[RetiredPage, ...] = ()`；`channels()` 照 C-6 解析與拒絕；`entity_for`／`entities` 把它帶進 `Entity.retired`。`channel_config` 已經 import `backend`，沒有新的邊。
- **合上後 main**：沒有呼叫端用到新函式；channels.json 沒有 `retired`（2b 之前重建不會產出），所以 `Entity.retired` 恆為 `()`；`Cursor.pages` 恆為 `None`，游標檔逐位元組不變。既有測試不改預期。

#### 2b：註冊簿規則 `registry_rules.py`、`rollover` 動作、回報去重、`delete` 的過期檢查

- **契約**：C-7、C-8、C-25。
- **檔案**：`src/angareion/registry.py`、新 `src/angareion/registry_rules.py`、`src/angareion/registry_rebuild.py`、`src/angareion/channel_cli.py`；測試新檔 `tests/unit/test_angareion_registry_rules.py`，C-8 的 `delete` 情境放既有的 CLI 測試旁（新檔或既有檔皆可，報告列出）。
- **介面**（`registry.py`）：

  ```python
  ACTIONS = ("register", "unregister", "rollover")

  @dataclass(frozen=True)
  class Action:             # 既有；只加最後一個欄位
      action: str
      channel: str
      backend: str
      issue: int
      from_issue: int | None = None   # 只有 rollover 有值；JSON 鍵是 "from"

  def rollover_comment(channel: str, backend: str, issue: int, from_issue: int) -> str
      # 一行 JSON，鍵順序 schema, action, channel, backend, issue, from（與 action_comment 同一種寫法）
  ```

  - `parse_comment`：`rollover` 恰好六鍵；`register`／`unregister` 照舊恰好五鍵，多 `from` 是 `unknown fields: from`。`from` 是正整數（`bool` 不算）、不等於 `issue`，否則 `InvalidAction`。
- **介面**（`registry_rules.py`，新；只 import 標準庫與 `registry`）：

  ```python
  def rebuild_channels(
      comments: list[dict[str, Any]], backend_names: set[str]
  ) -> tuple[dict[str, dict[str, Any]], list[str]]
      # 簽名不變（從 registry_rebuild 搬來）；值多一個選填的 "retired": [{"issue", "until", "side"?}]
  def render_channels(channels: dict[str, dict[str, Any]]) -> str
  def report_text(reason: str, body: Any) -> str
  ```

  - 一併搬來的私有輔助：`_quote`、`_comment_order`。`registry_rebuild` 從 `registry_rules` import 這三個公開名字，`from angareion.registry_rebuild import rebuild_channels, render_channels, report_text` 照樣可用（既有測試不改）。
  - **`CONFIG_VERSION = 1` 也搬到 `registry_rules`**（`render_channels` 要用，而 `registry_rules` 不得 import `registry_rebuild`）；`registry_rebuild` 以 `from angareion.registry_rules import CONFIG_VERSION, …` 拿回來給 `load_backend_names` 用，`tests/unit/test_angareion_rebuild.py:21` 的 `from angareion.registry_rebuild import CONFIG_VERSION` 照樣可用。`channel_config.CONFIG_VERSION`（`channel_config.py:34`）是讀者側的同值常數，不動、不合併（`registry_rules` 不得 import `channel_config`；兩者一致由 C-8 的 golden 與既有的 `channel_config` 測試鎖住）。
  - `until` 取那則 `rollover` 留言的 `created_at` 原字串。
- **介面**（`registry_rebuild.py`）：`RebuildSummary` 加 `posted: list[str] = field(default_factory=list)`；`Rebuilder.run` 的去重照 C-25。`load_backend_names`、`Rebuilder`、`main`、環境變數處理留在這裡。
- **介面**（`channel_cli.py`）：私有 `_current_page_moved(registry: Any, backend_names: set[str], name: str, local_issue: int) -> bool`：只在「讀得到註冊簿（`all_comments`）、重算裡有 `name`、而且 `local_issue` 在它的 `retired`」時回 `True`；其餘（讀不到、接縫沒有 `all_comments`、重算沒有 `name`、現頁相同）回 `False`。`cmd_channel_delete` 在 `ensure_fresh` 之後強制 `store.fetch()`，再以 `store.entity_for(name).issue` 呼叫它，`True` 就照 §1.2 的訊息 exit 2（stderr 照 `channel_cli` 現行寫法是 `Error: ` 加上 §1.2 那句，§1.2 的句子本身不帶前綴）。新邊 `channel_cli → registry_rules`（§8.2 已列）。
- **合上後 main**：現有註冊簿重建結果逐位元組相同（C-8 的 golden）；`rollover` 留言只會由人手動貼，被 §6.2 的盤點擋到 2c 之後；回報去重只讓註冊簿少貼；`delete` 只在有證據時拒絕。讀側沒有任何改變。

#### 2c：讀側跨頁接上、能力標記、`channel status` 的頁資訊、手動換頁的操作說明

- **契約**：C-10（接上 `read`）、C-11、C-12、C-13、C-14、C-15 的既有斷言改動（三處，只加 `pages`）、C-22、C-23 的情境部分、C-24、C-26。
- **檔案**：`src/angareion/backend.py`（例外、`ReadResult`、Fake 的跨頁讀取與錯誤注入）、`src/angareion/github.py`、`src/angareion/client.py`（只把警告往上傳）、`src/angareion/log_cache.py`、`src/angareion/cli.py`（`--capabilities`、印讀取警告）、`src/angareion/channel_cli.py`（`status` 的頁數與旗標）、`skills/angareion/SKILL.md`（手動換頁之後讀者不用做任何事、`channel status` 怎麼看；不寫 issue 編號）；測試新檔 `tests/unit/test_angareion_paged_read.py`（Fake 與 GitHub 假 transport 各跑一組同樣的情境，用 `pytest.mark.parametrize` 選後端）。手動換頁的操作步驟（§6.2 的三步與盤點）由 PM 決定放 PMO 的 runbook 或本 repo 的 `docs/`，架構師不寫。
- **介面**（`backend.py`）：

  ```python
  class PageUnavailableError(BackendError):
      """一頁的留言列表回 404、410，或不是 rate limit 的 403。"""
      def __init__(self, message: str, *, status: int): ...

  class ReadResult(NamedTuple):     # 既有；只加最後一個欄位
      messages: list[Message]
      cursor: Cursor
      warnings: tuple[str, ...] = ()

  def skipped_page_warning(channel: str, status: int) -> str
      # "an older page of channel '<C>' could not be read (HTTP <碼>); skipped"（C-22，不含編號）
      # 不帶 "Warning: " 前綴：與 PR-1 的 C-4 一致，前綴由印的那一方加（cli._warn、LogCache 的 warn 回呼）

  CAPABILITIES 不放這裡（放 cli.py）
  ```

  - `FakeBackend.set_page_error(issue: int, error: BackendError | None, *, repo: str = "owner/repo") -> None`：之後對那一頁的 `read`／`check`／`enumerate_conditional` 丟 `error`（`None` 解除）。
  - Fake 的 `read`／`check` 照 §2.1、§2.2 跨頁，與 GitHub 共用 `pages_to_read`、`page_base`、`cursor_after_read`、`skipped_page_warning`；逐頁迴圈各自寫（各約 20 行），由同一組參數化測試鎖住一致（§8.3 的 L）。
- **GitHub 的錯誤分類**：列表的三個入口（`check`、`read`、`enumerate_conditional`，含後續分頁）改用 `_request(..., allow_refusal=True)`，自己判斷：404、410、403 → `PageUnavailableError(<與現行相同的訊息>, status=…)`。404／410 的訊息照各入口現行的寫法（例如 `github.py` 的 `Failed to check for new messages: HTTP {status}`）。403 的訊息現在是 `_request` 內部組的（`github.py:236-237`），所以**先把那兩行抽成私有方法 `_refusal_message(self, resp: HttpResponse, url: str) -> str`**（回傳 `_compose(f"GitHub API refused the request (HTTP {resp.status})", self._scrub(self._raw_message(resp), url))`），`_request` 自己與三個入口都呼叫它，不各複製一份；`_request` 的 403 訊息因此逐字不變。因為是 `BackendError` 的子類，訊息字樣不變，現頁的錯誤對外完全不變（exit 2、stderr 同一句）；跨頁迴圈只在**退役頁**接住它並略過，現頁照舊往外丟。rate limit 仍由 `_request` 先判（`RateLimitError` 不被略過）。
- **介面**（`client.py`）：`ReadBatch` 加最後一個欄位 `warnings: tuple[str, ...] = ()`，收集各頻道 `ReadResult.warnings`（依頻道名排序後串起來）；不寫 stderr。
- **介面**（`cli.py`）：`CAPABILITIES = ("paged-read",)`；`--capabilities` 照 `_VersionAction` 的做法另立一個 action，印 `" ".join(CAPABILITIES)` 一行後 exit 0。`read` 在印訊息之前把 `ReadBatch.warnings` 逐則經既有的 `_warn`（`cli.py:88`，加 `Warning: ` 前綴）寫到 stderr。`--version` 不動。
- **介面**（`log_cache.py`）：`LOG_CACHE_VERSION = 3`；`LogCache.sync(backend: EnumerableBackend, entity: Entity)` 簽名不變，內部對 `(*退役頁, 現頁)` 逐頁呼叫 `enumerate_conditional(page_entity(entity, p), …)`；檔案格式照 §2.2 的 `log` 列；退役頁的 `PageUnavailableError` 把 `skipped_page_warning(...)` 交給既有的 `warn` 回呼（預設 `_warn_to_stderr` 加 `Warning: ` 前綴，`log_cache.py:29-30`）；字串本身不帶前綴，所以 `log`、`decision list` 與 `read` 印出來是同一行。`log_cache` 已經 import `backend`，沒有新的邊。
- **介面**（`channel_cli.py`）：`PAGES_UNTIL_ROLLOVER` 刪掉（現在用在 `channel_cli.py:236` 與 `:239` 兩處），頻道列的頁數＝`1 + len(entity.retired)`，**註冊簿那一列的頁數恆為 1**（註冊簿不換頁，直接寫常值 `1`）；`_page_flags` 查詢失敗的 stderr 照 `_page_count` 現行的 `Warning: channel '<C>': …` 寫法（不含編號）；私有 `_page_flags(backend: Any, entity: Entity, now: str | None, token: str) -> tuple[list[str], bool]` 照 C-26（`now` 取現頁 `page_size_at` 的 `Date`；`now` 是 `None` 時不檢查旗標、也不算失敗）。
- **既有測試**：只有 C-15 列出的三處與 `LOG_CACHE_VERSION` 相關的常數可以改，報告逐一列出（檔名、行號、改前改後）。
- **合上後 main**：channels.json 沒有 `retired` 時，讀取的請求與回傳的訊息和 2c 之前相同；差別只有三個：存下的游標多 `pages` 鍵、`LogCache` 升版整份重建一次、單頁新游標的 `check` 第二次起帶 `since`（只少請求，C-11）。`--capabilities` 印 `paged-read` 之後，§6.2 的盤點才可能通過。

#### 給實作者的共通事項

- 模組與依賴方向照 §8.1、§8.2；`registry_rules` 不得 import `backend`、`channel_config`、`registry_rebuild`。
- 改壞驗證照 §10 的 PR-2 清單，各子 PR 做自己契約的那幾項；**哪一項在哪個子 PR、靠哪個測試轉紅，照 §10 的 PR-2 分表**。要注意：C-23 列的五種 `page_base`／`pages_after_read` 改壞，在 2a 是靠 C-10 的純函式單元測試轉紅；C-22／C-23 的情境測試屬 2c，2c 再驗一次。
- 不連真 GitHub；隔離照 AGENTS.md。

### 6.4 PR-3 的切分：3-0、3a、3b、3c（第 7 版）

PMO 2026-11-26 決定 PR-3 現在開工；**合併條件不變**：3c（讓寫入端開始換頁的那一個）要等 §6.2 的盤點通過才 merge。3-0、3a、3b 不改任何對外行為，可以先 merge。介面簽名寫到實作者不必再猜的程度；PR 內的程式怎麼排由實作決定，但**簽名、模組歸屬、依賴方向與 Fake 的鉤子照這一節**，要偏離就先問架構師。

**和 PM 建議的切法不同的一處**：Fake 的 `open_page` 與 PR-3 的其他 Fake 鉤子從 3a 移到 3b。理由：3-0 把 `FakeBackend` 整段搬到新檔，3a 若也改 Fake，兩者就改到同一段程式，只能排隊；移走之後 3a 只碰 `backend.py` 的協定區、`github.py`、`registry.py`、`registry_rebuild.py`（一行回傳註記）、`state.py`，**3-0 與 3a 可以並行**。Fake 的鉤子只有 3b 的測試用得到，放在第一個用它的 PR 也比較好審（D-37）。

**依賴圖**（箭頭＝「要等它 merge 才能開工」）：

```text
3-0（Fake 搬家） ─┐
                  ├─→ 3b（rollover.py＋Fake 鉤子）─→ 3c（接線，盤點後 merge）
3a（前置介面）  ──┘
```

- 3-0 與 3a 互不依賴，可以同時開工、任意順序 merge。兩者都改 `backend.py`，但區段不重疊（3-0 刪掉檔尾的 Fake；3a 在 `PageBackend` 旁加協定與例外、在常數區加一個常數）；後 merge 的那一個 rebase 一次即可。
- 3b 要等 3-0 與 3a 都在 main（它的 Fake 鉤子寫在 `fake_backend.py`，它的程式用 3a 的 `PageOpener`、`WriteRefusedError`、`post_with_retries` 的回傳值）。
- 3c 要等 3b。

**每一步合上後 main 是否更糟**：

| 子 PR | 對外行為 | 理由 |
|---|---|---|
| 3-0 | 不變 | 只搬程式與改測試的 import；斷言一字不改 |
| 3a | 不變 | `post_comment`／`post_with_retries` 多回傳值，既有呼叫端不看；`open_page`、`PendingPageStore` 沒有呼叫端；`WriteRefusedError` 是 `BackendError` 的子類，訊息逐字不變 |
| 3b | 不變 | `rollover.py` 沒有正式程式 import 它（只有測試） |
| 3c | 寫入開始換頁 | 這是 PR-3 的目的；merge 前提是 §6.2 的盤點（`paged-read` 在每一份安裝），3c 合上之後 `--capabilities` 多 `paged-write` |

#### 3-0：`FakeBackend` 搬到 `angareion/fake_backend.py`（D-35）

- **契約**：沒有新契約（D-35）。驗收是「既有測試全過、斷言一字不改」，外加下面兩個邊界測試。
- **檔案**：
  - 新 `src/angareion/fake_backend.py`：把 `backend.py` 的 `FakeComment`、`_utc_now`、`FakeBackend` 三段**原封不動**搬過來（現在是 `backend.py:612` 起到檔尾）。模組 docstring 一句：只給測試與本機開發用，正式程式不得 import。
  - `src/angareion/backend.py`：刪掉這三段；刪掉只有它們用到的 import（例如 `timezone`，若其他地方不用）。**不留相容的重新匯出**（`from angareion.fake_backend import FakeBackend` 會成環）。
  - 測試：20 個檔、22 行 import 改成 `from angareion.fake_backend import FakeBackend`（`FakeComment` 同理）。一行裡同時 import 別的名字的，拆成兩行，例如 `from angareion.backend import Cursor, FakeBackend, Target` 改成 `from angareion.backend import Cursor, Target` 加 `from angareion.fake_backend import FakeBackend`。函式內的 import 兩處（`test_angareion_log_query.py:252`、`test_angareion_log_sync.py:92` 的 `FakeComment`）一樣改。用 `grep -rn "FakeBackend\|FakeComment\|_utc_now" tests src` 確認沒有漏。`openspec/changes/archive/` 裡的舊文件不改。
  - 新測試 `tests/unit/test_angareion_fake_backend_boundary.py`（第一行 `import _guard`）：
    - `test_d35_no_production_module_imports_fake_backend`：用 `ast` 掃 `src/angareion/*.py`（`fake_backend.py` 本身除外），沒有任何 `import angareion.fake_backend` 或 `from angareion.fake_backend import …`，也沒有 `from angareion import fake_backend`。
    - `test_d35_backend_does_not_reexport_the_fake`：`not hasattr(angareion.backend, "FakeBackend")`，`FakeComment` 同理。
- **介面**：`fake_backend` 只 import `backend` 的公開名字、`messages` 與標準庫（`fake_backend → {backend, messages}`）。名字照 Fake 本體用到的逐一 import，不用 `import *`。
- **合上後 main**：行為不變。`backend.py` 約 610 行。

#### 3a：前置介面（協定、例外、GitHub 的 `open_page`、註冊簿回傳留言、待註冊紀錄）

- **契約**：C-17 的前置部分（`post_with_retries` 回傳最後一次成功的留言物件；待註冊紀錄「讀不出或寫不進 → 當作沒有、不報錯」；GitHub 的 `open_page`）。W-4 的 `WriteRefusedError` 分類（GitHub 側）。
- **檔案**：`src/angareion/backend.py`、`src/angareion/github.py`、`src/angareion/registry.py`、`src/angareion/registry_rebuild.py`（只改 `RebuildApi.post_comment` 的回傳註記，見下）、`src/angareion/state.py`；測試新檔 `tests/unit/test_angareion_pr3_prereq.py`（測試名帶契約編號，例如 `test_c17_post_with_retries_returns_the_posted_comment`）。3a 的測試**不 import `FakeBackend`**（用假 transport、`FakeRegistryClient`、`PendingPageStore`），所以 3-0 與 3a 誰先 merge 都不必改對方的測試。GitHub 的假 transport：repo 裡沒有共用的 helper（現有的是 `test_angareion_pr1_contract.py` 裡的 `Transport` 類別），3a 在 `test_angareion_pr3_prereq.py` 裡照它的形狀自己寫一個，不從別的測試檔 import。
- **介面**（`backend.py`）：

  ```python
  PAGE_FILL_LIMIT = 2490          # §1.3 的表；放在 PAGE_ROLLOVER_AT 旁邊

  class WriteRefusedError(BackendError):
      """POST 留言被拒（403／422）、不是 rate limit、也不是滿：確定沒有寫進去（設計 542 §1.3 W-4）。"""
      def __init__(self, message: str, *, status: int):
          super().__init__(message)
          self.status = status

  @runtime_checkable
  class PageOpener(Protocol):
      """Open the next page of a channel (design 542 §1.3 S-4). Kept apart from PageBackend (A-5)."""
      def open_page(self, entity: Entity) -> int: ...
      # entity＝現頁（單頁實體）。在 entity.repo 開一張新 issue：標題＝entity.channel，
      # 本文含 "continued from #<entity.issue>"；回傳新 issue 的編號。失敗丟 BackendError（訊息不含編號）。
  ```

- **介面**（`github.py`）：
  - `GitHubIssuesBackend.open_page(self, entity: Entity) -> int`：`POST repos/{entity.repo}/issues`，body `{"title": entity.channel, "body": f"Angareion channel '{entity.channel}', continued from #{entity.issue}. Messages are comments on this issue.\n"}`。與 `create_channel` 共用的「POST issue、取 `number`」抽成私有方法（第 8 版改成兩個字串，審查低-1）：

    ```python
    def _open_issue(self, repo: str, title: str, body: str, *, what: str, context: str) -> int
        # 200／201 以外 → BackendError(f"{what}: HTTP {resp.status}")
        # 回應不是 JSON → self._json(resp, context) 的現有訊息：
        #   f"GitHub returned a non-JSON response while {context} (HTTP {resp.status})"
        # 沒有 number（KeyError／TypeError／ValueError）→ BackendError(f"{what}: no issue number in the response")
    ```

    - `create_channel` 改成 `return self._open_issue(backend.repo, name, <現有本文>, what=f"Failed to create channel '{name}'", context=f"creating channel '{name}'")`；名字的 `ValueError` 檢查留在 `create_channel`。三種訊息逐字不變（現況 `github.py:299-305`）。
    - `open_page` 用 `what=f"Failed to open a new page for channel '{entity.channel}'"`、`context=f"opening a new page for channel '{entity.channel}'"`。所以 2xx 以外是 `Failed to open a new page for channel '<C>': HTTP <碼>`、非 JSON 是 `GitHub returned a non-JSON response while opening a new page for channel '<C>' (HTTP <碼>)`、沒有 `number` 是 `Failed to open a new page for channel '<C>': no issue number in the response`；三種都不含編號與 repo 名。
  - `_refused_send` 最後一行的 `BackendError(...)` 改成 `WriteRefusedError(<同一個字串>, status=resp.status)`。`send` 的「不是 200／201、也不是 403／422」那一行不改（那是結果不確定的錯誤，W-5／W-11 照舊往外丟）。
- **介面**（`registry.py`）：

  ```python
  class CommentPoster(Protocol):
      def post_comment(self, body: str) -> dict[str, Any]: ...   # 原本 -> None

  def post_with_retries(poster, body, *, attempts=..., interval=..., sleeper=time.sleep) -> dict[str, Any]
      # 回傳成功的那一次 post_comment 的回傳值（重試在第一次成功時就停，所以它也是 §1.3、C-17 說的「最後一次成功」）；不是 dict 時回傳 {}

  # RegistryClient.post_comment(body) -> dict[str, Any]
  #   回傳 resp.json()；不是 JSON 或不是 dict → 回傳 {}（留言已經貼出，不丟例外）
  ```

  - `FakeRegistryClient`：
    - `__init__` 多兩個關鍵字參數：`post_association: str = "NONE"`、`post_user_type: str = "Bot"`（`post_comment` 存下的留言用它們；預設值等於現行，既有測試不變）。PR-3 的測試要讓自己貼的 `rollover` 被重建接受，就傳 `post_association="OWNER"`。
    - 新屬性 `before_post: Callable[[str], None] | None = None`：`post_comment` 一開始（在 `fail_posts_times` 檢查之前、**在鎖外**）呼叫 `before_post(body)`。C-18 用它當 gate。
    - 新屬性 `hidden_ids: set[int]`（預設空）：`list_comments` 先濾掉 `id` 在這個集合裡的留言，再照 `page_size` 切頁。C-18 的 I-2 用它模擬列表的複寫延遲。
    - 新屬性 `_lock = threading.RLock()`：`add_comment` 的「算 index、append」在鎖內，兩個執行緒同時貼不會拿到同一個 id。`post_comment` 自己不在呼叫 `before_post` 之前拿鎖。
    - `post_comment(body) -> dict[str, Any]`：回傳 `add_comment(...)` 的回傳值。
  - `channel_cli` 的兩個呼叫端、`registry_rebuild.Rebuilder` 不看回傳值，不改。
  - `registry_rebuild.RebuildApi.post_comment(self, body: str) -> dict[str, Any]`（原本 `-> None`，`registry_rebuild.py:67`）：只改註記，與 `CommentPoster` 一致；`Rebuilder` 的程式不改（審查低-7(c)）。
- **介面**（`state.py`）：

  ```python
  def pending_pages_file_path(state_dir: Path | str, identity: str) -> Path
      # Path(state_dir) / f"pending-pages-{identity_slug(identity)}.json"

  class PendingPageStore:
      """已開、還沒註冊成功的頁（設計 542 §1.3「待註冊紀錄」）。只存取，不解讀。"""
      def __init__(self, state_dir: Path | str | None = None, identity: str | None = None,
                   path: Path | str | None = None): ...      # 與 CursorStore 同一種參數規則
      def get(self, channel: str) -> tuple[str, int, int] | None   # (backend, issue, from_issue)
      def put(self, channel: str, backend: str, issue: int, from_issue: int) -> None
      def drop(self, channel: str) -> None
  ```

  - 檔案內容 `{"version": 1, "pages": {"<C>": {"backend": "<名>", "issue": N, "from": P}}}`，`json.dumps(..., indent=2, sort_keys=True)` 加換行。
  - `get`：檔案不存在、讀不出、不是 JSON、`version` 不是 `1`、`pages` 不是物件、那一筆缺鍵或型別不對（`backend` 不是字串；`issue`／`from` 不是正整數，`bool` 不算）→ `None`。不丟例外、不寫 stderr。
  - `put`／`drop`：讀現有內容（讀不出就從空的開始），改一筆，用與 `CursorStore.save_many` 相同的原子寫入（同目錄的 `tempfile.mkstemp` 加 `os.replace`）寫回。`OSError` 一律吞掉（不丟、不寫 stderr）。`drop` 一個不存在的頻道不寫檔。
  - 不加鎖（§1.3：同一身分兩個行程同時寫，後寫的蓋掉先寫的，最壞多一張孤兒頁）。
- **測試**（`test_angareion_pr3_prereq.py`）：
  - GitHub 假 transport：`open_page` 的 URL、方法、`title`、本文含 `continued from #<P>`、回傳編號；500 → `BackendError`，訊息不含 `#數字` 與 repo 名；`create_channel` 的既有測試照舊過。
  - `_refused_send` 小於 2400 → `isinstance(err, WriteRefusedError)`、`err.status == 403`，訊息與改之前逐字相同（拿既有 C-2 測試的期望字串比）；GET 失敗同樣是 `WriteRefusedError`。rate limit 照舊是 `RateLimitError`（不是 `WriteRefusedError`）。
  - `RegistryClient.post_comment` 回傳 transport 給的 dict；回應不是 JSON → `{}`。`post_with_retries`：第一次失敗、第二次成功 → 回傳第二次的留言，`sleeper` 被呼叫 1 次。
  - `FakeRegistryClient`：`post_association="OWNER"` 存下的留言是 `OWNER`；`before_post` 在失敗注入之前被呼叫；**`before_post` 被呼叫時 `registry._lock._is_owned()` 是 `False`**（不用執行緒；`RLock` 對同一個執行緒可重入，所以不能用 `acquire(blocking=False)` 判斷。審查低-6：沒有這一條，「把 `before_post` 放到鎖內」在 3a 單獨 merge 時沒有東西守）；`hidden_ids` 讓 `all_comments` 看不到那幾則、但 `comment_count` 仍算它們。
  - `PendingPageStore`：存讀；兩個頻道互不影響；上面列的每一種壞資料各一個 → `None`；目錄不可寫（`chmod 0o500` 的暫存目錄）時 `put` 不丟例外、`get` 是 `None`。這一個測試開頭照 `test_angareion_etag.py:408` 的前例 `if getattr(os, "geteuid", lambda: -1)() == 0: pytest.skip("root can write into a read-only directory")`（root 寫得進唯讀目錄，審查低-4）。
  - `create_channel` 的三種失敗訊息逐字比對（HTTP 500、非 JSON、沒有 `number`；既有測試只比對子字串 `non-JSON`，抽 `_open_issue` 時要有逐字的守門）；`open_page` 的同三種逐字比對。
- **合上後 main**：行為不變（見上表）。

#### 3b：`rollover.py`（`PageWriter`、`ensure_successor`）與 Fake 的鉤子

- **契約**：C-16、C-17（含 S-8′、S-4′）、C-18。全部在函式庫層驗（`PageWriter` 直接建構），不經 CLI。
- **檔案**：新 `src/angareion/rollover.py`、`src/angareion/fake_backend.py`（鉤子）；測試新檔 `tests/unit/test_angareion_rollover.py`（C-16、C-17）、`tests/unit/test_angareion_rollover_race.py`（C-18）。
- **import 邊**：`rollover → {backend, messages, registry, registry_rules}`（`messages` 只為 `Message` 型別；§8.2 原本漏列，本版補上）。**不 import** `channel_config`、`state`、`config`、`client`、`cli`、`github`、`fake_backend`。
- **介面**（`rollover.py`）：

  ```python
  MAX_RESTARTS = 3

  # SuccessorError.kind 的值（§1.3 的列）
  REGISTRY_UNREADABLE = "registry-unreadable"   # S-1（含：registry 沒有 all_comments）
  MISMATCH = "mismatch"                         # S-2（含：backends() 丟例外、本機 backend 不在 backends() 裡）
  OPEN_FAILED = "open-failed"                   # S-5（含：backend 不是 PageOpener）
  UNVERIFIED = "unverified"                     # S-6、S-6′
  UNREGISTERED = "unregistered"                 # S-7
  REJECTED = "rejected"                         # S-8′、S-4′

  REASONS = {                                   # 固定字串，不含編號
      REGISTRY_UNREADABLE: "could not read the channel registry",
      MISMATCH: "the local channel configuration does not match the registry",
      OPEN_FAILED: "could not open a new page",
      UNVERIFIED: "the new page could not be verified",
      UNREGISTERED: "opened a new page but could not register it",
      REJECTED: "the registry did not accept the new page",
  }

  class SuccessorError(BackendError):
      def __init__(self, kind: str, message: str | None = None):
          self.kind = kind
          self.reason = REASONS[kind]
          super().__init__(message or self.reason)

  class PendingPages(Protocol):                  # state.PendingPageStore 在結構上符合它
      def get(self, channel: str) -> tuple[str, int, int] | None: ...   # (backend, issue, from_issue)
      def put(self, channel: str, backend: str, issue: int, from_issue: int) -> None: ...
      def drop(self, channel: str) -> None: ...

  class MemoryPendingPages:                      # 測試用；與 backend.MemoryETagCache 同一種角色
      def get(...); def put(...); def drop(...)

  def filling_warning(channel: str, count: int) -> str
      # f"channel '{channel}' has {count} of {PAGE_COMMENT_LIMIT} messages on its current page"
      # 與 PR-1 的 C-4 那一行逐字相同（不帶 "Warning: "，前綴由 warn 回呼加）
  def prepare_failed_warning(channel: str, reason: str) -> str
      # f"channel '{channel}' is nearly full and its next page could not be prepared ({reason}); "
      # "the message was written to the current page"
  def stuck_message(channel: str, reason: str) -> str
      # f"channel '{channel}' is full and its next page could not be prepared ({reason}); "
      # f"try again later, or run 'angareion channel rollover {channel}'"

  class PageWriter:
      def __init__(
          self,
          backend: ChannelBackend,
          registry: Any,                                   # CommentPoster；有 all_comments 才能換頁
          *,
          pending: PendingPages,
          backends: Callable[[], Mapping[str, BackendRef]],  # cli：store.backends()
          refresh: Callable[[str], Entity],                # cli：store.fetch() 再 store.entity_for(C)
          warn: Callable[[str], None] = lambda text: None,  # cli：cli._warn（加 "Warning: "）
          now: Callable[[], datetime] = <UTC now>,         # 只用在「肯定在窗外」的跳過（L-2）
          sleeper: Callable[[float], None] = time.sleep,    # 給 post_with_retries
      ) -> None
      def write(self, entity: Entity, message: Message) -> str
      def ensure_successor(self, channel: str, current: int, local: Entity, *, repost: bool = False) -> Entity
      # repost：S-4′ 的例外，只有 channel rollover 傳 True；write 一律用預設的 False
  ```

- **`write` 的流程**（§1.3 的 W 表寫成程式的形狀；這段是規格，實作可以拆私有方法，但分支與順序照這裡）：

  ```python
  def write(self, entity, message):
      if not isinstance(self.backend, PageBackend):
          return self.backend.send(entity, message)       # C-4 第 3 點：沒有頁大小就照舊寫現頁，不警告
      e, restarts, refreshed = entity, 0, False
      while True:
          # A：最後一個非旁頁的退役頁 R
          r = next((p for p in reversed(e.retired) if not p.side), None)
          if r is not None and _maybe_recent(self.now(), r.until):
              page = page_entity(e, r.issue)
              try:
                  n, date = self.backend.page_size_at(page)
              except BackendError:
                  n, date = None, None                                     # W-3
              if n is not None and _in_fill_window(date, r.until) and n < PAGE_FILL_LIMIT:
                  try:
                      return self.backend.send(page, message)              # W-1／W-12，不警告
                  except (ChannelFullError, WriteRefusedError):
                      pass                                                 # W-4 → B
                  # 其他 BackendError 不接（W-5）
          # B：現頁 P
          p = page_entity(e, e.issue)
          try:
              n, _ = self.backend.page_size_at(p)
          except BackendError:
              n = None                                                     # W-7
          prior = None
          if n is not None and PAGE_ROLLOVER_AT <= n < PAGE_COMMENT_LIMIT:  # W-8
              try:
                  e = self.ensure_successor(e.channel, e.issue, e)
                  restarts = self._restart(e.channel, restarts); continue
              except SuccessorError as err:
                  prior = err
                  self.warn(prepare_failed_warning(e.channel, err.reason))
          if n is None or n < PAGE_COMMENT_LIMIT:                          # W-6／W-7／W-8 失敗
              if n is not None and n >= PAGE_WARN_AT:
                  self.warn(filling_warning(e.channel, n))                 # C-20：寫現頁、寫入前 ≥2000
              try:
                  return self.backend.send(p, message)                     # W-12
              except ChannelFullError:
                  pass                                                     # W-10 → 與 W-9 相同
              # 其他 BackendError（含 WriteRefusedError、RateLimitError）不接（W-11）
          # W-9／W-10：P 已滿
          if prior is None:
              try:
                  e = self.ensure_successor(e.channel, e.issue, e)
                  restarts = self._restart(e.channel, restarts); continue
              except SuccessorError as err:
                  prior = err
          if prior.kind == MISMATCH and not refreshed:
              refreshed = True
              e = self.refresh(e.channel)                                  # 例外照原樣往外丟
              restarts = self._restart(e.channel, restarts); continue
          if n is not None and n >= PAGE_COMMENT_LIMIT:
              self.warn(filling_warning(e.channel, n))                     # 保住 PR-1 的 test_c4_the_warning_is_written_before_the_refusal_it_comes_with
          raise SuccessorError(prior.kind, stuck_message(e.channel, prior.reason))
  ```

  - `_restart(channel, restarts) -> int`：`restarts + 1`；超過 `MAX_RESTARTS` 就丟 `BackendError(f"channel '{channel}': could not settle which page to write; try again later")`。
  - `_maybe_recent(now: datetime, until: str) -> bool`（第 8 版，審查低-3）：`parse_backend_time(until)` 丟 `ValueError` → `False`（當作窗外，跳過 A、不發 `page_size(R)`）；否則 `now <= parse_backend_time(until) + RETIRED_READ_GRACE`。真 GitHub 不會給壞的時刻，這是給替身與手寫設定檔的保護（`FakeRegistryClient` 的第 60 則起 `created_at` 是 `12:00:60Z`，解析不了）：沒有它，壞的 `until` 會變成沒有 `BackendError` 包裝的例外，`main` 印 `Unexpected error`。
  - `_in_fill_window(date: str | None, until: str) -> bool`：`date` 是 `None`，或兩者任一 `parse_backend_time` 丟 `ValueError` → `False`；否則 `parse_backend_time(date) - parse_backend_time(until) < PAGE_FILL_WINDOW`。
  - **最後失敗（exit 2）之前的警告**：W-9 走到最後失敗時，先印 C-4 那一行（`filling_warning`），再丟 `SuccessorError`；`stuck_message` 含 `is full`。這讓 PR-1 的兩個既有測試不改也照樣過：`test_angareion_pr1_contract.py` 的 `test_c4_a_refused_write_at_the_limit_exits_2_with_the_full_message`（stderr 含 `is full`、exit 2）與 `test_c4_the_warning_is_written_before_the_refusal_it_comes_with`（警告在 `is full` 之前）。這兩個測試的註冊簿夾具沒有 `general` 的 `register`，所以會走 S-2 → refresh → 再 S-2 → 最後失敗，POST 0 次。W-10（寫入前 <2500、已印過警告）不再印第二次。
  - W-10 在 W-8 失敗之後發生時，**不再呼叫第二次 `ensure_successor`**，直接用 W-8 的錯誤（`prior`）決定 refresh 或失敗。
  - `self.now()` 回傳帶時區的 `datetime`；`parse_backend_time` 回傳的也是帶時區的，可以直接比。邊界：恰好等於 `until + 48h` 時照 W-1～W-3（`<=`）。
- **`ensure_successor` 的流程**（§1.3 的 S 表）：

  ```python
  def ensure_successor(self, channel, current, local, *, repost=False):
      if not isinstance(self.registry, RegistryComments):
          raise SuccessorError(REGISTRY_UNREADABLE)
      try:
          before = self.registry.all_comments()                           # S-1
      except RegistryError:
          raise SuccessorError(REGISTRY_UNREADABLE)
      try:
          refs = dict(self.backends())
      except Exception:
          raise SuccessorError(MISMATCH)
      channels, _ = rebuild_channels(before, set(refs))
      entry = channels.get(channel)
      if local.backend not in refs or not _compatible(entry, current, local):
          raise SuccessorError(MISMATCH)                                  # S-2，open_page 0 次
      if entry["issue"] != current:                                       # S-3
          self.pending.drop(channel)
          return _entity_from_mapping(local, entry)
      record = self.pending.get(channel)                                  # S-4
      if record is not None and record[0] == local.backend and record[2] == current:
          new = record[1]
          mine = Action("rollover", channel, local.backend, new, current)
          if not repost and any(parse_comment(c.get("body")) == mine for c in before):
              raise SuccessorError(REJECTED)                              # S-4′：不驗、不貼，紀錄保留
      else:
          if not isinstance(self.backend, PageOpener):
              raise SuccessorError(OPEN_FAILED)
          try:
              new = self.backend.open_page(page_entity(local, current))
          except BackendError:
              raise SuccessorError(OPEN_FAILED)                           # S-5
          self.pending.put(channel, local.backend, new, current)
      try:
          ok = self.backend.verify_channel(channel, refs[local.backend], new)
      except BackendError:
          raise SuccessorError(UNVERIFIED)                                # S-6′：紀錄保留
      if not ok:
          self.pending.drop(channel)
          raise SuccessorError(UNVERIFIED)                                # S-6：紀錄刪掉
      try:
          own = post_with_retries(self.registry, rollover_comment(channel, local.backend, new, current),
                                  sleeper=self.sleeper)
      except RegistryError:
          raise SuccessorError(UNREGISTERED)                              # S-7：紀錄保留
      own = own if isinstance(own, dict) and "id" in own else None
      try:
          after = self.registry.all_comments()                            # S-8
      except RegistryError:
          after = before                                                  # S-9
      merged = [c for c in after if own is None or c.get("id") != own["id"]] + ([own] if own else [])
      channels, _ = rebuild_channels(merged, set(refs))
      entry = channels.get(channel)
      if entry is None or entry["issue"] == current:
          raise SuccessorError(REJECTED)                                  # S-8′：紀錄保留
      self.pending.drop(channel)
      return _entity_from_mapping(local, entry)
  ```

  - `_compatible(entry, current, local) -> bool`：`entry` 是 `None` → `False`；`entry["backend"] != local.backend` → `False`；令 `retired = {p["issue"] for p in entry.get("retired", [])}`，`current != entry["issue"]` 而且 `current not in retired` → `False`；`local.retired` 有任何一頁的 `issue` 不在 `retired` → `False`；其餘 `True`。（S-2 的四種）
  - `_entity_from_mapping(local, entry) -> Entity`：`Entity(channel=local.channel, backend=entry["backend"], repo=local.repo, issue=entry["issue"], retired=tuple(RetiredPage(issue=p["issue"], until=p["until"], side=bool(p.get("side", False))) for p in entry.get("retired", [])))`。`repo` 取本機的：S-2 已確認 backend 名相同。
  - S-6 對「剛開的頁」也刪紀錄（第 7 版寫明）：剛開的頁在 `put` 之後就在紀錄裡，與「來自紀錄的頁」是同一種情況。
  - 關於 S-8′ 的實際機率：channel token 是擁有者的，真 GitHub 上自己的留言是 `OWNER`。S-8′ 主要擋測試替身與設定錯誤；擋住之後同一頁不再重開（紀錄保留）。
  - S-4′（第 8 版，審查中-1）：第 7 版在 S-8′ 之後每次重試都再貼一則相同的 `rollover`，註冊簿（所有頻道共用、上限 2500）只增不減。現在下一次走到 S-4 拿到同一筆紀錄時，先看 S-1 的 `before`：自己那則已經在、而重算（S-2／S-3 用的就是 `before`）沒套用它 → 直接 `REJECTED`，不發 `verify_channel`、不貼。比對用 `parse_comment(body) == Action(...)`（`registry.Action` 是 frozen dataclass，`==` 比欄位），不比原文字串，空白或鍵順序不同的同義留言也算。只在「N 來自紀錄」的分支看：剛開的頁號是新的，不可能已在註冊簿。上界見 §1.3「註冊簿留言的上界」。
  - `repost=True`（只有 `channel rollover`）跳過 S-4′、照樣驗與貼：S-8′ 的原因修好之後（例如換了作者身分），自動寫入永遠不會再貼，PMO 用 `channel rollover <C>` 重貼一次；仍被拒就再 `REJECTED`，每次執行最多多 1 則。
  - 新 import：`rollover` 從 `registry` 多 import `Action`、`parse_comment`（邊 `rollover → registry` 已有）。
- **Fake 的鉤子**（`fake_backend.py`；全部新加，既有的方法與預設行為不變）：
  - `__init__` 多一個關鍵字參數 `page_date: Callable[[], str | None] | None = None`：`page_size_at` 的第二個值用 `page_date()`；沒給就照現行用 `clock()`。C-16 的「沒有 `Date`」傳 `page_date=lambda: None`。
  - `open_page(self, entity: Entity) -> int`：先 `_check_injected_error()`；再看 `next_open_error`（有就清掉並丟出，與 `next_create_error` 同一種寫法）；新頁號＝`create_channel` 用的同一個計數器 `_next_entity_id`（所以 `add_entity` 過的最大號＋1 起跳；測試要固定新頁號就用 `start_entity_id` 或先 `add_entity`）；存 `{"title": entity.channel, "state": "open", "body": f"continued from #{entity.issue}"}`；把新頁號 append 到 `self.opened: list[int]`；回傳新頁號。
  - `set_send_error(self, number: int, error: BackendError | None, *, repo: str = "owner/repo") -> None`：之後對那一頁的 `send` 一律丟 `error`（`None` 解除）；檢查放在「實體存在」之後、頁大小之前。用來做 W-4、W-5、W-10、W-11。
  - `self.size_calls: list[int]`：`page_size_at` 每次被呼叫就 append `entity.issue`（`page_size` 經 `page_size_at`，也算）。**`send` 內部判斷滿頁改用私有的 `_current_size(entity) -> int`，不經 `page_size_at`**，所以 `size_calls` 只記外部的呼叫。`_current_size` 的語意與現在 `page_size_at` 的第一個值相同：`_page_sizes` 的覆寫值有就回它，沒有就回該頁的實際留言數（`page_size_at` 也改成呼叫 `_current_size` 再 append `size_calls`，兩者不會漂移）。C-16 的「不發 `page_size(R)`」用它斷言。
  - `self._lock = threading.RLock()`：`create_channel`、`open_page`、`deliver` 的本體在鎖內（C-18 兩個執行緒共用一個 Fake）。
- **C-18 的 gate**（`test_angareion_rollover_race.py`；不用 sleep）：
  - 共用：一個 `FakeBackend`（現頁 P，`set_page_size(P, 2450)`，`clock` 固定在註冊簿留言時間之後 1 分鐘內，讓 W-1 的窗成立；`FakeRegistryClient` 的留言時間是 `2026-10-10T12:00:<序號>Z`，所以 `clock` 用 `2026-10-10T12:01:00Z`，兩個 `PageWriter` 的 `now` 也用同一刻）、一個 `FakeRegistryClient(post_association="OWNER")`（夾具裡有 C 的 `register`）、兩個 `PageWriter`（各自一個 `MemoryPendingPages`，`sleeper=lambda s: None`）。
  - gate：`barrier = threading.Barrier(2, timeout=5)`；`registry.before_post = lambda body: barrier.wait()`。兩個執行緒各自 `write`：兩邊都過了 S-1（「沒有下一頁」）、各自開了頁之後，才在貼 `rollover` 前會合，然後一起貼。執行緒內的例外收集到 list，主執行緒 `join(timeout=10)` 後斷言 list 是空的、兩個執行緒都結束了。某一邊沒走到貼留言（例如判斷錯成 S-3），另一邊會在 5 秒後 `BrokenBarrierError`，測試以失敗結束，不會卡住。
  - 斷言：`backend.opened` 有 2 頁；註冊簿有兩則 `rollover from P`；`rebuild_channels` 得到一個現頁加一個 `side`；旁頁上 0 則；用 `rebuild` 出來的實體做一個 `StaticResolver`，PR-2 的讀者（`Angareion(...).read(Target(channel=C))`）讀到兩則訊息各恰好一次。
  - 這一組結束時把 `registry.before_post` 設回 `None`。下面兩組**各自建新的夾具**（新的 `FakeBackend`、`FakeRegistryClient`、兩個 `PageWriter`），不沿用上面那一份：barrier 已用過，留著 `before_post` 的夾具再貼任何留言，都會在 5 秒後 `BrokenBarrierError`（審查低-5）。
  - 「B 在 A 貼出之後才開始」：不用執行緒、不設 `before_post`，A 先 `write` 完、B 再 `write` → `backend.opened` 只有 A 那一頁。
  - I-2：不用執行緒。這一組把 P 設成 `set_page_size(P, 2495)`（在 W-8 的區間、但 ≥2490，所以換頁後不會回頭寫 P，B 的訊息才會落在自己的頁上）。A 先 `write` 完；把 **A 貼的那則 `rollover` 註冊簿留言**（不是 A 寫的訊息；用 A 寫完之後 `registry.all_comments()` 裡 `parse_comment(body).action == "rollover"` 的那一則）的 `id` 放進 `registry.hidden_ids`；B `write`（B 的 S-1 與 S-8 都看不到 A）→ B 開自己的頁並寫進去；清掉 `hidden_ids`，重建 → B 的頁是旁頁；讀者先用「只有現頁」的實體讀一次並 commit，再換成含旁頁的實體讀 → B 的訊息恰好一次。
- **測試的寫法**（`test_angareion_rollover.py`）：
  - 每個 W／S 列至少一個測試，名字帶列號，例如 `test_c16_w1_writes_the_retired_page_inside_the_fill_window`、`test_c17_s6_prime_keeps_the_pending_record`。
  - 「寫到哪一頁」用 Fake 的 `_comments` 裡的 `page` 欄位斷言（`FakeComment.page == ("owner/repo", N)`），不靠訊息內容猜。
  - 時鐘：`now` 傳固定的 `datetime`；`page_date` 傳固定字串；不用 `datetime.now()`。
  - 錯誤訊息的「不含編號」：每個失敗測試對 `str(err)` 斷言不符合 `#\d+`、不含 `owner/repo`。
- **合上後 main**：行為不變，沒有正式程式 import `rollover`。

#### 3c：接線（`send`、`decision`、`channel rollover`、能力標記、說明）

- **契約**：C-19、C-20、C-21；C-24 的 PR-3 部分（`paged-read paged-write`）。
- **檔案**：`src/angareion/client.py`、`src/angareion/cli.py`、`src/angareion/decision_cli.py`、`src/angareion/channel_cli.py`、`src/angareion/channel_config.py`、`src/angareion/backend.py`（只改 `ChannelFullError` 的訊息尾）、`skills/angareion/SKILL.md`；測試新檔 `tests/unit/test_angareion_pr3_cli.py`（C-19、C-20）與 `tests/unit/test_angareion_no_numbers.py`（C-21）。
- **介面**（`client.py`）：
  - `Angareion.__init__(self, backend, cursors, store, identity, writer: PageWriter | None = None)`（最後一個參數，預設 `None`；既有的建構呼叫不改）。存成公開屬性 `self.writer`（`channel_cli.cmd_channel_rollover` 讀 `app.writer`）。
  - 新方法 `write(self, entity: Entity, message: Message) -> str`：`writer` 有值就 `writer.write(entity, message)`，否則 `backend.send(entity, message)`。
  - `send(self, channel, message)` 改成 `return self.write(self.store.entity_for(channel), message)`。
  - 新邊 `client → rollover`（只為 `PageWriter` 的型別；§8.2 已列）。
- **介面**（`cli.py`）：
  - `main` 在建 `app` 時多建一個 `PageWriter`：`backend=active_backend`、`registry=active_registry`、`pending=PendingPageStore(config.state_dir, config.identity)`、`backends=<見下>`、`refresh=<見下>`、`warn=_warn`、`sleeper=active_sleeper`；傳給 `Angareion(..., writer=writer)`。
    - `backends`：`lambda: active_store.backends() if hasattr(active_store, "backends") else {}`（測試的 store 可能沒有這個方法；空的就是 S-2 不相容，不開頁）。
    - `refresh(name)`：`fetch = getattr(active_store, "fetch", None)`；有就呼叫；回傳 `active_store.entity_for(name)`。
  - `cmd_send`：刪掉 `_warn_if_page_is_filling` 的呼叫與函式本身（警告改由 `PageWriter` 經 `warn` 印，請求數不變：PR-1 的那一個 GET 就是 W-6 的 `page_size_at`）。`app.send(args.channel, message)` 不變。
  - `CAPABILITIES = ("paged-read", "paged-write")`。
  - parser：`channel` 多一個子命令 `rollover`（位置參數 `name`）；`main` 的「channel needs an action」訊息加上 `'angareion channel rollover <name>'`；派發到 `channel_cli.cmd_channel_rollover(args, config, app, active_registry, active_store, token, sleeper=active_sleeper)`。
- **介面**（`decision_cli.py`）：三處 `backend.send(entity, message)`（`_maybe_snapshot`、`_write_one`、`cmd_submit` 的迴圈）改成 `app.write(entity, message)`。簽名：`_maybe_snapshot(config, backend, entity, token)` → `_maybe_snapshot(config, app, backend, entity, token)`（**多收** `app`；`backend` 仍用於 `_sync` 的列表）；`_write_one(config, backend, entity, message)`（`decision_cli.py:159`）→ `_write_one(app, entity, message)`（**替換**：`config`、`backend` 拿掉，本體只剩 `serialize_message(message)` 加 `app.write(...)`），五個呼叫端照著改。請求數：`decision` 的每一次寫入多 1 個 GET（W-6 的 `page_size_at`；PR-1 沒有替 `decision` 量），`submit` 一次 N 筆多 N 個（§11）。不 import `client` 或 `rollover`（`app` 照舊是 `Any`），依賴邊不變。`tests/unit/_decision_cli_lab.py` 的 `_TRACKED` 不含 `page_size_at`，所以既有的 C-20(e)（#468 的編號）測試不改、照樣過；實作時若它轉紅，先回報架構師，不要擴大 `C20E_ALLOWED`。
- **介面**（`channel_config.py`）：`ChannelStore.poll_for_page(self, name: str, issue: int, *, timeout: float = LANDING_POLL_TIMEOUT_SECONDS) -> bool`：與 `poll_for_channel` 同一個迴圈，條件改成 `self.channels().get(name)` 不是 `None` 而且它的 `issue == issue`。
- **介面**（`channel_cli.py`）：

  ```python
  def cmd_channel_rollover(args, config, app, registry, store, token, sleeper=time.sleep) -> int
  ```

  1. `validate_channel_name(args.name)`，錯 → exit 1（同 `delete`）。
  2. `store.ensure_fresh()`；`store.fetch()`（M-4）；`entity = store.entity_for(args.name)`（沒有這個頻道 → `ChannelNotFoundError`，由 `main` 轉成 exit 1）。
  3. `app.writer` 是 `None` → stderr `Error: channel '<C>' cannot be rolled over by this command` exit 2。
  4. `result = app.writer.ensure_successor(args.name, entity.issue, entity, repost=True)`（`repost=True` 跳過 S-4′，見 §6.4 的 `ensure_successor`）；`SuccessorError` → stderr `Error: channel '<C>' was not rolled over: <err.reason>` exit 2（不開頁的那幾種，S-1／S-2，`open_page` 0 次；S-5～S-8′ 同樣 exit 2；`repost=True` 所以不會停在 S-4′）。
  5. `store.poll_for_page(args.name, result.issue)` 是 `False` → stderr `Notice: channel '<C>' has a new page but the local copy has not caught up yet; it will be used once the registry rebuild lands.`（不是錯誤）。
  6. stdout `<C>` 一行，exit 0。
  - 不看則數（C-19：強制）。已落地、現頁就是剛換好的頁時再跑一次，會再開一頁：這是「強制」的意思，SKILL.md 要寫明「只在 PMO 判斷要換時跑」。
  - 新邊 `channel_cli → rollover`（為了 `SuccessorError`；§8.2 已列）。
- **介面**（`backend.py`）：`ChannelFullError` 的訊息尾加 `; run 'angareion channel rollover <C>'`（§4）。既有測試只比對子字串（`is full`、`looks full …`、`HTTP 403`），不受影響。
- **`SKILL.md`**：`send` 滿頁時會自動換頁、寫的人不用做任何事；`channel rollover NAME` 是 PMO 的工具（強制換頁）；`--capabilities` 多 `paged-write`。不寫 issue 編號。
- **測試**：
  - C-20 的 store 夾具：`StaticResolver` 沒有 `fetch`／`backends`，3c 在 `test_angareion_pr3_cli.py` 裡寫一個（`entity_for`、`backends()` 回 `{"<名>": BackendRef(...)}`、`fetch()` 計數；`refresh` 測試讓它在 fetch 之後換實體），不放共用 helper。
  - C-20：`main(["send", …], backend=FakeBackend, store=<上面那個 store>, registry=FakeRegistryClient(post_association="OWNER", comments=[register 那則]))`，Fake 現頁 `set_page_size(P, 2500)` → exit 0，訊息在新頁（`FakeComment.page`），stderr 沒有 C-4 那一行（寫的是新頁、寫入前 0 則）；`decision ask` 同樣寫到新頁。另一個：現頁 2100 → stderr 恰好一行 C-4 的警告（與 PR-1 的 `test_c4_*` 同一句）。再一個（第 8 版，審查低-7(d)）：現頁 2450、註冊簿讀不到（W-8 失敗）→ exit 0、訊息在 P、stderr **恰好兩行**，依序是 `Warning: ` 加 `prepare_failed_warning(...)`、`Warning: ` 加 `filling_warning(..., 2450)`。兩行是有意的：前一行說「下一頁沒準備好」，後一行是 C-4 的則數（C-20：寫現頁、寫入前 ≥2000）；W-8 失敗時 n 必然 ≥2400，所以這種情況永遠是兩行。
  - C-19：列出的每一種結果各一個（含 S-3 的 `open_page` 0 次、未註冊的名字 exit 1、`poll_for_page` 逾時只有 Notice）。`poll_for_page` 的逾時測試要給 `ChannelStore` 會前進的 `clock`（`poll_for_channel` 的迴圈靠它結束；固定的 `clock` 會讓測試卡住）。
  - C-21：`test_angareion_no_numbers.py` 用參數化跑過 C-16～C-20 的失敗情境（CLI 層：`send`、`decision ask`、`channel rollover`），對 stdout、stderr 斷言沒有 `#\d+`、沒有 `owner/repo`；另對 `rollover.REASONS` 的每個值與 `stuck_message`、`prepare_failed_warning`、`filling_warning` 的輸出逐一斷言。
  - C-24：`test_angareion_paged_read.py` 的 `test_c24_capabilities_prints_one_line_and_exits_0` 期望值改成 `paged-read paged-write`（這是 C-24 寫明 PR-3 要改的，實作報告列出）。
- **合上後 main**：寫入開始換頁。merge 前提是 §6.2 的盤點；PR 本文要附盤點結果（或連到 PMO 的盤點留言）。

#### 手動換頁與 PR-3 並存（w2 的現實）

w2 已滿 2500，PR-3 合上之前 PMO 會照 runbook 手動換頁。手動換過的頻道在 channels.json 裡就是一般的 `retired`，PR-3 的寫入端不分「手動」或「自動」：

| 情況 | PR-3 寫入端的路徑 | 結果 |
|---|---|---|
| 已落地：現頁 N，`retired=[{P, until}]`，P 有 2500 則 | A：本機時鐘在 `until+48h` 以內 → `page_size(P)`＝2500 ≥ 2490 → W-2 → B：寫 N | 正確。換頁後 48 小時內，每次寫入多 1 個 GET（P 的計數）；之後 A 被本機時鐘跳過，不再多花 |
| 已落地，但舊頁其實沒滿（PMO 提早換，例如 2300 則） | A：窗內而且 <2490 → W-1 寫 P | 這是「舊頁先寫滿」，Q-2 的 B；與自動換頁相同，讀者兩頁都讀。PMO 若不想要這個行為，要在換頁前讓舊頁 ≥2490，或接受它 |
| 已落地，舊頁寫入被拒但計數 <2400（例如被鎖、或「Commenting is disabled」而計數延遲） | A：W-1 寫 P → `WriteRefusedError` → W-4 → B：寫 N | 正確。這是第 7 版把 `WriteRefusedError` 加進 W-4 的原因；第 6 版的表在這裡會 exit 2（W-5）一直到窗外 |
| 舊頁已刪或讀不到（runbook §9 的往前修） | A：`page_size(P)` 失敗 → W-3 → B | 正確 |
| 手動的 `rollover` 已貼、還沒落地（本機現頁仍是 P，已滿） | B：W-9 → `ensure_successor`：S-1 讀到手動那則 → S-3（P 在 `retired`）→ 回到 A：P 窗內但 ≥2490 → B：寫 N | 正確，`open_page` 0 次。S-2 的第四種檢查（本機的 `retired` 都在重算裡）對已有 `retired` 的頻道照樣成立 |
| 手動換頁之後再自動換頁（N 也到 2400） | `ensure_successor(C, N)`：重算的現頁是 N、`retired=[P]` → S-4 → `retired=[P, N]` | 正確；A 看的是最後一個非旁頁（N） |
| PMO 手動換頁的同時，有 PR-3 寫者在自動換 | 與 I-1 相同：先貼的成立，另一頁是旁頁 | 不漏不重送。PMO 的頁若成了旁頁，runbook §6 的驗證會看到現頁不是自己開的那張；照 runbook §8 處理（不必補救）。**PR-3 合上之後，runbook 第 3、4 節應改成 `angareion channel rollover <C>`**（它走 S-3 與待註冊紀錄，不會和自動換頁搶頁）；gh 的手動步驟留作 angareion 不能用時的備案。runbook 不在架構師可以 commit 的路徑，由 PM 改 |
| 某寫者在手動換頁之前留下待註冊紀錄（`from=P`） | 換頁落地後，該頻道下一次走到 S-4 時 `from` 是 N，與紀錄不同 → 當作沒有並覆寫 | 舊紀錄那張頁成為孤兒（最多一張，§1.3 的上界）。不影響送達 |
| `-b` 頻道 | 與換頁無關 | §3 照舊 |

#### 給實作者的共通事項

- 模組與依賴方向照 §8.1、§8.2。`rollover` 不 import `channel_config`、`state`；`decision_cli` 不 import `client`、`rollover`；正式程式不 import `fake_backend`。
- 改壞驗證照 §10 的 PR-3 清單，各子 PR 做自己契約的那幾項。
- 既有測試的斷言不改；唯一要改的是 C-24 的能力字串（3c），以及 3-0 的 import 行。其他的既有測試轉紅，先回報架構師。
- 不連真 GitHub；隔離照 AGENTS.md。測試的假 secret 照 AGENTS.md 的規則。

## 7. 契約（每條都有測試對應；實作報告逐條對到測試名稱）

### PR-1

- **C-1** `_request` 收到 403／429：
  - `x-ratelimit-remaining: "0"` → `RateLimitError`，`kind == "primary"`，訊息含 reset 時刻（UTC，RFC 3339）；
  - 有 `retry-after`，或 body 的 `message` 含 `secondary rate limit` → `RateLimitError`，`kind == "secondary"`，訊息含秒數（有的話）。
  - 兩者都是 `BackendError` 的子類，CLI 都 exit 2，請求只送一次。
  - 訊息仍含 `HTTP <狀態碼>` 與 `not retrying`；既有的 `test_angareion_github.py`（403＋`secondary rate limit`，`match="not retrying"`）與 `test_angareion_cli.py`（`retry-after: 60`，stderr 含 `403`，`transport.calls == 1`）不改。
- **C-2** POST 留言回 403／422，而且不符 C-1 → 接著 GET 一次 issue，得到 n：
  - n ≥ 2500 → `ChannelFullError`，`presumed is False`，訊息照 §4 的格式（`is full`）；
  - 2400 ≤ n < 2500 → `ChannelFullError`，`presumed is True`，訊息含 `looks full (or the token was refused)`；
  - n < 2400，或 GET 失敗 → `BackendError`，訊息含 `HTTP 403`（或 422）與 GitHub 的 `message`，不含 `secondary rate limit?`。
  - 三種訊息都含 `HTTP <碼>` 與處理過的 `message`（單行、≤200 字、token 遮蔽、repo 名→`<backend>`、`#<數字>`→`#…`）。
  - 三種訊息都不含 issue 編號與 repo 名：PR-1 的測試直接對這三種訊息比對 `#\d+` 與 repo 名（泛用掃描器是 PR-3 的 C-21）。
- **C-3** `page_size(entity) -> int`：
  - GitHub＝GET issue 的 `comments`；回應不是 JSON 物件、`comments` 缺漏或不是整數 → `BackendError`。回應的 `Date` 標頭由 `page_size_at(entity) -> tuple[int, str | None]` 一併給出（PR-3 用；PR-1 就提供）。
  - Fake＝`(repo, issue) -> int` 覆寫表有值就回它，沒有就回該實體的實際留言數；測試輔助 `set_page_size(issue, n, repo=…)` 設覆寫值。Fake 的 `send` 在 `page_size >= comment_limit`（預設 2500）時丟 `ChannelFullError`。PR-1 不改 Fake 既有的存放方式（依頁存放是 PR-2 的 C-15）。
  - 新協定 `PageBackend`（`page_size`／`page_size_at`），`runtime_checkable`；`ChannelBackend` 不加方法。
- **C-4** `send`（PR-1 的語意；PR-3 之後見 C-20）：
  - 在 `cli.cmd_send`、`app.send` 之前 `page_size(現頁)`：≥2000 → stderr 恰好一行 `Warning: channel '<C>' has <n> of 2500 messages on its current page`；<2000 → 沒有這一行。
  - `page_size` 失敗，或後端不是 `PageBackend` → 沒有警告，照常寫入，stdout 與 exit 不變。
  - `decision` 的寫入在 PR-1 不警告（C-20 在 PR-3 統一）。
- **C-5** `channel status [NAME…] [--json]`（`channel_cli.py`）：
  - 每個頻道一列：名稱、現頁則數、上限、等級（`ok` <2000、`warn`、`rollover` ≥2400、`full` ≥2500）、頁數、旗標（PR-1 恆為空，C-26）。
  - 最後一列是 `registry`（`RegistryCounter.comment_count()`，註冊簿的留言數，等級用同一組門檻）。
  - `--json` 是物件的陣列，每個物件的鍵：`name`（str）、`count`（int；`unknown` 時 `null`）、`limit`（int）、`level`（`ok`／`warn`／`rollover`／`full`／`unknown`）、`pages`（int）、`flags`（str 的陣列）；註冊簿那一列 `name` 是 `"registry"`。
  - 未註冊的名字 → exit 1。某個頻道或註冊簿查詢失敗，或註冊簿接縫不是 `RegistryCounter` → 那一列等級 `unknown`，stderr 說明，其他列照印，最後 exit 2。
  - 輸出不含 issue 編號，不動游標，除了 fetch 不做任何寫入。
  - `channel create`／`delete` 搬進 `channel_cli.py`（import 照 `decision_cli.py` 的做法，不 import `cli`），行為與既有測試不變。

### PR-2

- **C-6** channels.json 的 `retired`：
  - 合法：`[{issue: int>0, until: 帶時區的 RFC 3339, side?: true}]` → `Entity.retired: tuple[RetiredPage, ...]`，依檔案順序。
  - 沒有這個鍵 → `()`。
  - 形狀不合（不是 list、`issue` 不是正整數、`until` 讀不出、`side` 不是 `true`、與現頁或彼此重號）→ `ChannelConfigError`（exit 2）。
  - 既有的 channels.json 解析結果與 PR-2 之前相同。
- **C-7** `parse_comment`：
  - `rollover` 必須恰好有 `schema/action/channel/backend/issue/from` 六鍵，`from` 是正整數、且不等於 `issue`；
  - `register`／`unregister` 帶 `from` → `InvalidAction`（unknown fields）；
  - `rollover_comment(channel, backend, issue, from_issue)` 產生一行 JSON，`parse_comment` 讀回得到相同的值。
- **C-8** `registry_rules.rebuild_channels` 套用 `rollover` 時照 §1.2 的表：
  - 每一列一個測試，含兩則 `from` 相同的競態，結果是一個現頁加一個 `side`。
  - `register` 判斷同一實體只比 `backend`／`issue`；`unregister` 現頁 → 整筆移除；`unregister` 退役頁 → no-op 並回報。
  - `channel delete` 的使用者可見結果（§1.2，CLI 測試）：本機設定檔的現頁 P、強制 fetch 之後仍是 P，而註冊簿重算裡 P 已在 `retired` → exit 2，stderr 含 `was not deleted`，stdout 空，`close_channel` 0 次，註冊簿沒有新的 `unregister`。註冊簿讀不到、接縫沒有 `all_comments`、重算裡沒有 C，或現頁相同 → 照現行流程（既有 `delete` 測試不改預期）。
  - `render_channels`：沒有退役頁的頻道沒有 `retired` 鍵；現有註冊簿的夾具重建結果與 PR-2 之前**逐位元組相同**（golden）。
  - `registry_rules` 只 import 標準庫與 `registry`。`registry_rebuild` 從它 import `rebuild_channels`／`render_channels`，既有測試的 import 路徑照樣可用。
- **C-9** `pages_to_read(entity, cursor) -> tuple[Entity, ...]`：
  - 現頁一定在最後；`cursor.at` 不是 `None` 時，退役頁只在 `cursor.at < until + 48h` 時列入，舊的在前；`--since` 游標用同一規則。
  - **新游標**（`cursor.at is None`，第 5 版，A-12）：退役頁只在 `until + 48h > U`（U＝最大的 `until`）時列入。測試：三張退役頁，`until` 分別是 U−10 天、U−47 小時、U（其中一張是旁頁也一樣）→ 只列後兩張；沒有退役頁 → 只有現頁。改壞：拿掉這條上限（新游標列入全部退役頁）→ 失敗；把比較改成 `>=` 而邊界值剛好等於 → 失敗（邊界測試用 `until + 48h == U`）。
  - 每個元素是 `retired=()` 的單頁實體。
  - 純函式，不碰網路。
- **C-10** `read`（GitHub、Fake）：
  - 回傳的訊息是 `pages_to_read` 各頁中符合 scope 的新訊息的聯集，依 comment id 遞增；每一頁的「新」以 `page_base(prev, 該頁)` 為起點照單頁規則判斷。
  - 游標＝`advance_cursor(prev, 各頁 entries 的聯集)` 換上 `pages=pages_after_read(prev, to_read, entries_by_page)`（§2.1）；`--since` 不動游標；新身分廣播的下限：同一次 `read` 的所有頁共用第一個列表回應的 `Date`（L-3；測試：兩頁、兩個回應的 `Date` 不同，兩頁上同時刻的廣播判定相同）。
  - `page_base` 與 `pages_after_read` 是純函式，單元測試逐條：
    - `page_base`：`pages` 含該頁 → 那一頁的游標；`pages` 不含 → `Cursor()`；`pages` 是 `None` → 主游標（不帶 `pages`）。
    - `pages_after_read`：讀成功 → `advance_cursor(起點, 該頁 entries)`（entries 空時＝起點）；讀過的頁被略過 → 原值不變；沒讀過的頁被略過 → `Cursor()`；**`prev.pages=None` 時被略過 → `prev` 的主游標**（M-1）；不在 `to_read` → 不在結果裡；結果的鍵恰好是 `to_read` 的頁。
  - 只有一頁時，游標 `pages` 是 `None` 或 `{現頁: …}`，請求與回傳的訊息都和 PR-2 之前相同；存下的游標多一個 `pages` 鍵，值與主游標相同（既有測試只有 C-15 列出的斷言要改）。
- **C-11** `check`：
  - 任一頁有新訊息 → True；
  - 游標沒讀過的頁上有符合 scope 的留言 → True；
  - ETag 紀錄與 `scan_since` 以頁為 key（`repo#issue|scope`；從頭讀模式另加 `|unread`），某頁的 304 不替另一頁回答；
  - **請求數上界**（§2.2）：同一個 `GitHubIssuesBackend` 物件，對一張沒讀過、n＝250 則（3 個列表分頁）而且全都不符合 scope 的頁連續 `check` 5 次 → 都是 False，GitHub 假 transport 記到的那一頁的請求數 ≤ 3＋4；第 2 次起的 URL 帶 `since`。改壞驗證：拿掉「從頭讀模式以所有可解析的訊息推動 `_scan_since`」→ 請求數變成 15，測試失敗。
  - **新游標**（M-3）：游標是 `Cursor()`，實體有一張同樣 250 則、全不符合 scope 的退役頁 R 與一個空的現頁，連續 `check` 5 次 → 都是 False，R 的請求數 ≤ 3＋4。改壞驗證：從頭讀模式只認「`pages` 不含這一頁」、不認新游標 → R 的請求數變成 15。
  - 頁上只有非訊息的留言（`parse_message` 失敗）時不推動窗（nit-2）。
  - 起點不是空游標的頁，`_scan_since` 仍只由符合 scope 的留言推動（現行測試不改預期）；
  - **別頁前進不讓 ETag 失效**：游標 `pages={R: cR, N: cN}`，第一次 `check` 存下 R 的 ETag；N 上有新訊息、`read` 之後（`cN` 前進、`cR` 不變）再 `check` → R 的請求帶 `If-None-Match`。
  - 單頁時與現行相同，唯一的差別是新游標（`Cursor()`）走從頭讀模式：同一個行程第二次起的 `check` 帶 `since`，ETag 與 `scan_since` 的 key 多 `|unread`。讀碼沒找到鎖住這兩者的既有測試；實作時若有，只能改 key 字串或第二次起的 URL，照 C-15 逐一列出。
- **C-12** 接續（情境測試，Fake 與 GitHub 假 transport 各一）：
  - 讀者讀到舊頁第 k 則 → 換頁 → 舊頁再寫 2 則、新頁寫 3 則 → 下一次 `read` 恰好得到這 5 則，依到達順序；再讀一次得到 0 則。
  - 換頁前已讀過的不再出現。
  - 讀者游標超過 `until+48h` 之後，就不再對舊頁發請求（GitHub 假 transport 記錄 URL）。
- **C-13** 「給我」跨頻道合併的排序鍵不變（`received_at_key, channel, id`）；同一頻道跨頁的訊息相對順序＝comment id 順序。
- **C-14** `LogCache`：
  - v3 對**所有頁**逐頁 `enumerate_conditional`（單頁實體），各頁有自己的 `last_created_at` 與 etag，訊息以 comment id 合併，依 id 排序。
  - v2 檔 → 整份重建（升版後第一次 `log`／`decision list` 會多花請求，一次性）；`repo` 不同 → 重建；entity 已沒有的頁 → 它的訊息移除。
  - `log --channel C` 與 `decision list` 看得到舊頁與新頁的訊息。
- **C-15** Fake 依 `(repo, issue)` 存留言：
  - `deliver(entity, …)` 存在 `entity.issue`（可用 `page=` 指定別頁）；
  - `read`／`check`／`enumerate_conditional`／`page_size` 只看所指的頁。
  - 既有測試**不弱化**：斷言不放寬、不刪；只有下面兩類可以改，要在實作報告逐一列出（檔名、行號、改前改後）：
    - `LogCache` 版本號這類隨升版必改的常數；
    - **游標多了 `pages`**（重審 M-2）。讀碼找到三處：`tests/unit/test_angareion_cli.py:401`（`stored == {"channel:general": {...}}`）、`tests/unit/test_angareion_library.py:239`（`set(entry) == {"position", "at", "seen"}`）、`tests/unit/test_angareion_backend.py:237`（Fake 讀空頻道後 `res.cursor == Cursor()`）。只能**加上** `pages` 鍵或欄位（值照 §2.1 的序列化，單頁時是 `{"<現頁>": 與主游標相同的三欄}`／`Cursor(pages={現頁: Cursor()})`）；`position`／`at`／`seen` 的斷言一字不改、不放寬。實作時 grep 到其他同類斷言，照同一規則處理並列出。
    - `test_angareion_state.py` 只存讀不帶 `pages` 的 `Cursor`，照 §2.1 的序列化（`pages` 是 `None` 不寫鍵）不受影響，不在可改之列。
  - `test_angareion_version.py` 不在可改之列（能力標記另立旗標，C-24）。
- **C-22** 讀退役頁失敗（`read`、`check`、`LogCache`）：
  - 404、410、不是 rate limit 的 403 → 略過那一頁，其他頁照讀；
  - `read` 與 `log` 在 stderr 印恰好一行 `Warning: an older page of channel '<C>' could not be read (HTTP <碼>); skipped`（不含編號），`check` 不印；
  - **前綴只有一個**（第 6 版，W-1）：`read` 與 `log` 各一個測試，stderr 不含 `Warning: Warning:`；`skipped_page_warning` 的回傳值不以 `Warning` 開頭（單元測試）。
  - 游標的 `pages` 照 `pages_after_read`：被略過的頁游標不動。情境測試（Fake 與 GitHub 假 transport 各一）：
    - **讀過的頁**：游標 `pages={R: cR, N: cN}`，R 上的訊息都已送達 → R 上新到 2 則（r1、r2）→ 2 分鐘後 N 上新到 1 則（n1）→ 讀（R 回 404）：得到 n1，主游標越過 r1、r2 的 id 與時間，`pages[R]` 仍是 cR → R 恢復 → 下一次 `read` 恰好得到 r1、r2，R 上原本已送達的 0 則重送；再讀一次 0 則。GitHub 假 transport 記到的 R 的請求帶 `since`（cR 的 `at-60s`，不是從頭）。
    - **沒讀過的頁**：游標 `pages={P: cP}`，設定檔加上 S（S 上有 3 則，id 都比主游標小、時間都早於主游標的 `at-60s`）→ S 回 404 一輪 → S 恢復 → 下一次 `read` 從頭讀 S，恰好得到那 3 則；再讀一次 0 則。
    - **舊游標（`pages=None`）＋退役頁 404**（M-1）：游標檔只有三欄（主游標讀到 R 的最後一則），設定檔是現頁 N＋退役頁 R → R 上新到 r1、N 上新到 n1 → 讀（R 回 404）：得到 n1，存下的 `pages[R]` 等於讀之前的主游標 → R 恢復 → 下一次 `read` 恰好得到 r1，R 上原本的訊息 0 則重送。
  - 「給我」跨頻道讀時，一張退役頁 404 不讓其他頻道失敗（exit 0）。
  - 現頁的同樣錯誤照舊 exit 2；rate limit、網路錯誤照舊往外丟。
- **C-23** 游標 `pages`（情境測試，Fake 與 GitHub 假 transport 各一）：
  - 讀者的游標已越過新頁上某些留言的 id（設定檔晚到：先只讀 P 並前進，之後設定檔才加上 S；S 上有 id 比游標小、時間早於 `at-60s` 的訊息）→ 下一次 `read` 得到 S 上的全部訊息，再讀一次 0 則。
  - 旁頁晚一步進設定檔（I-2）同樣送達。
  - **反向**：已讀過的頁不被從頭讀。游標 `pages={P: cP, S: cS}`、S 上的訊息都已送達 → 再 `read` 兩次都是 0 則；GitHub 假 transport 記到的 S 的請求帶 `since`。
  - `state.py` 存讀 `pages`（§2.1 的序列化）：`pages` 是 `None` → 檔案沒有 `pages` 鍵；有值 → 讀回相等（`Cursor` 的 `==` 含 `pages`）；舊檔沒有 `pages` → `None`；壞資料各一個案例，結果照 §2.1，不丟例外（nit-3）：
    - `pages` 不是物件 → `None`；
    - 鍵不符 `^[1-9][0-9]*$`（例如 `"x"`、`"0"`、`"028"`）→ 那一筆不在結果裡，其他筆照讀；
    - 鍵合格、值不是物件 → 那一頁的值等於主游標的三欄（`position`／`at`／`seen` 與主游標相同，`pages` 是 `None`）（第 5 版，A-13）；
    - `seen` 型別不對 → 照主游標現行的防禦。
  - **存別的 key 不洗掉這個 key 的 `pages`**（第 5 版，重審 L-1）：同一個檔有兩個 key，兩者都有 `pages`；`save` 其中一個 → 另一個讀回的 `Cursor`（含 `pages`）與存之前相等。`save_many` 只更新其中一個時也一樣。
  - 改壞驗證：
    - `page_base` 對不在 `pages` 的頁回主游標（改回比 position）→ 第一個情境失敗（漏）；
    - `page_base` 對在 `pages` 的頁也回主游標（等於第 3 版拿掉 `held`）→ C-22「讀過的頁」失敗（漏 r1、r2）；
    - `pages_after_read` 對被略過的頁不寫入結果（等於第 2 版「只留讀成功的頁」）→ C-22「讀過的頁」失敗（重送）；
    - `pages_after_read` 對被略過、沒讀過的頁存主游標 → C-22「沒讀過的頁」失敗（漏）；
    - `prev.pages=None` 時被略過存 `Cursor()` → C-22「舊游標」失敗（重送）；
    - `save_many` 只對這一次更新的 key 寫 `pages`（其他 key 照三欄重寫）→「存別的 key」案例失敗（L-1）；
    - 鍵合格、值不是物件時丟掉那一筆（第 4 版的規則）→ A-13 的案例失敗。
- **C-24** `angareion --capabilities`：stdout 恰好一行 `paged-read`（PR-3 起是 `paged-read paged-write`），exit 0。`angareion --version` 的輸出與 PR-2 之前相同，`test_angareion_version.py` 不改。
- **C-25** `Rebuilder.run`：
  - 引用某一則留言的回報（`report_text` 的本文不是空的），若在留言串裡已有一模一樣的本文 → 不貼。同一個註冊簿連跑兩次，第二次這類回報貼 0 則；不同的回報各貼一次。
  - 固定文字的回報（本文是空的，例如寫檔衝突）每次發生都貼：讓兩次 run 都發生寫檔衝突 → 貼 2 則。
  - `rebuild_channels` 回傳的 reports 不變（純函式不看已貼的）；`summary.reports` 是全部、`summary.posted` 是實際貼出的，第二次 run 的 `summary.reports` 與第一次相同、`summary.posted` 不含去重掉的。
- **C-26** `channel status` 的頁資訊：
  - 頁數＝1＋`retired` 的筆數；
  - 旗標：
    - `side`：有旁頁，而且旁頁上有留言（S-8 之後應該是空的；有留言表示發生過 I-2，只是告知，讀者會從頭讀）；
    - `late`：最後一個非旁頁的退役頁，在 `until + PAGE_FILL_WINDOW` 之後還有留言（§2.3 第 1 項的偵測）。
  - 只檢查 `until` 距今 7 天內的退役頁（距今用回應的 `Date`）；每頁 1 個請求（`read` 該頁、`--since`、不動游標）。
  - 查詢失敗 → 旗標 `unknown`，照 C-5 的規則 exit 2。
  - 輸出不含編號。

### PR-3

- **C-16** `PageWriter.write(entity, message) -> str`：§1.3 的 W-1～W-12 每一列至少一個測試，另外：
  - 重來超過 3 次 → `BackendError`；
  - 沒有 `Date` → 等同 W-2；
  - 本機時鐘（注入）超過 `R.until + 48h` → 不發 `page_size(R)`，直接到 B（L-2）；在 `R.until + 48h` 以內 → 照 W-1～W-3；
  - R 的 `until` 解析不了（例如 `12:00:60Z`）→ 當作窗外：不發 `page_size(R)`、寫 P、不丟 `ValueError`（第 8 版，審查低-3）；
  - W-9 的 S-2 不相容 → 呼叫 `refresh` 恰好 1 次再試；refresh 後相容 → 寫入成功；仍不相容 → exit 2（L-5）；W-8 的不相容不呼叫 `refresh`；
  - 24h 用 `page_size_at` 的 `Date`（Fake 用注入的時鐘）；
  - W-8 成功、P 在窗內且 <2490 → 寫 P；
  - I-8（本機設定檔過期，P 早已換頁）→ 寫新頁，`open_page` 0 次。
  - W-4 的 `WriteRefusedError`（第 7 版）：R 的寫入丟 `WriteRefusedError` → 寫 P；R 的寫入丟 `RateLimitError` → 往外丟（W-5），P 沒有被寫。
  - W-9 最後失敗：stderr（`warn`）先有 C-4 那一行，再丟 `SuccessorError`，訊息含 `is full`；`open_page` 次數照 S 表。
  - 後端不是 `PageBackend` → 直接 `send` 現頁，`page_size_at`、註冊簿都 0 次。
- **C-17** `ensure_successor(C, P, E_local)`：§1.3 的 S-1～S-9（含 S-6′）每一列至少一個測試，另外：
  - S-2 的四種不相容各一個，都是 `open_page` 0 次；
  - S-7 之後新頁上 0 則訊息；
  - **孤兒頁上界**（M-2）：S-7 失敗（註冊簿寫不進）之後，同一個身分再 `send` 3 次（P 在 2400～2499，都走 W-8；註冊簿仍寫不進）→ `open_page` 總共 1 次，`post_comment` 每次都對同一頁重試；註冊簿恢復後的下一次 → `open_page` 仍是 1 次，貼出的 `rollover` 的 `issue` 是那一頁，待註冊紀錄被刪掉。
  - 待註冊紀錄的其他列：S-6 對紀錄裡的頁回 `False` → 紀錄刪掉，下一次 `open_page` 1 次；S-6′（`verify_channel` 丟 `BackendError`）→ 紀錄保留，下一次 `open_page` 0 次、`verify_channel` 再驗同一頁；S-3 → 紀錄刪掉；紀錄的 `from` 與 P 不同 → 當作沒有，`open_page` 1 次並覆寫；紀錄檔壞掉 → 當作沒有，不報錯。
  - `post_with_retries` 回傳最後一次成功的留言物件；S-8 用它的 `id`／`created_at`（L-3）；
  - S-8 的「N 是旁頁」→ 回傳贏家實體，N 上 0 則；
  - S-8 的列表裡沒有自己那則 → 結果與有時相同；
  - S-8′（第 7 版）：`FakeRegistryClient` 用預設的 `post_association="NONE"` → 丟 `SuccessorError`（`kind == REJECTED`），待註冊紀錄保留；
  - S-4′（第 8 版，取代第 7 版「下一次對同一頁再貼一次」）：接著上一條，同一個 `PageWriter` 再 `write` 3 次（P 在 2400～2499，走 W-8）→ `open_page` 總共 1 次、註冊簿的留言數（`registry.comment_count()`）比 S-8′ 之後**沒有增加**、`verify_channel` 0 次（測試用 `monkeypatch` 包一層計數）、每次都是 `REJECTED`（`warn` 的 `prepare_failed_warning` 帶 `the registry did not accept the new page`）、訊息寫在 P；`ensure_successor(..., repost=True)` → 驗 1 次、貼 1 次（仍是 `NONE`，所以仍 `REJECTED`）；把夾具改成 `post_association="OWNER"` 之後 `repost=True` → 成功，回傳的現頁是紀錄那一頁，紀錄刪掉。另一個：註冊簿裡那則同義留言的 JSON 鍵順序不同（手寫的 body）→ 照樣在 S-4′ 停；`from` 不同的 `rollover` → 不算，照常驗、貼；
  - 失敗訊息不含編號。
- **C-18** 競態（兩個寫者以 gate 交錯：兩邊都過了 S-1「沒有下一頁」之後才放行貼留言）：
  - 註冊簿出現兩則 `rollover from P`，重建出一個現頁加一個 `side`，旁頁上 0 則；
  - 兩人寫的訊息，PR-2 的讀者各讀到恰好一次；
  - 另一組：B 在 A 的 `rollover` 貼出之後才開始 → B 的 `open_page` 呼叫 0 次；
  - 另一組（I-2）：`FakeRegistryClient` 在 B 的 S-8 隱藏 A 那則 → B 寫進旁頁；讀者先拿到只有現頁的設定檔並前進，再拿到含旁頁的設定檔 → B 的訊息恰好送達一次。
- **C-19** `channel rollover NAME`：
  - 先強制 `store.fetch()`（M-4），再不看則數、強制 `ensure_successor(C, 現頁)`；已有待落地的下一頁（S-3）→ 不開新頁；S-2 不相容 → exit 2，不開頁。
  - stdout 只印 NAME，exit 0；之後照 `channel create` 輪詢落地（上限 2 分鐘，逾時只在 stderr 提示）。
  - 未註冊的名字 → exit 1；S-1／S-2／S-5～S-8′ → exit 2。
  - 以 `repost=True` 呼叫：待註冊紀錄裡的頁已有一則同義的 `rollover` 而沒被套用時，照樣驗、貼一次（S-4′ 的例外，第 8 版）。
- **C-20** `cli send` 與 `decision_cli` 的每一個寫入都經 `PageWriter`：
  - 測試：Fake 現頁設成 2500，`send` 與 `decision ask` 都寫到下一頁；`ChannelBackend.send` 仍是單頁的原語。
  - 警告（C-4 在 PR-3 後的語意）：實際寫入的那一頁，若是寫入所用實體的**現頁**，而且寫入前 ≥2000，就印 C-4 那一行；寫退役頁（W-1）時不印。
  - W-8 失敗而寫現頁時，C-4 那一行之前多一行 `prepare_failed_warning`（共兩行，有意的；第 8 版寫明，測試見 §6.4 3c）。
- **C-21** PR-1～PR-3 的所有新訊息（stdout、stderr、例外字串）都不含 issue 編號與 repo 名。掃描式測試：跑過上面各情境，對輸出做 `#\d+` 與 repo 名的比對。

## 8. 結構（模組、介面、依賴方向）

### 8.1 模組切割

| 模組 | 新增或改動 | 不做什麼 |
|---|---|---|
| `backend.py` | `RetiredPage(issue, until, side)`、`Entity.retired`（預設 `()`，frozen 不變）、`Cursor.pages`（頁號→那一頁的 `Cursor`）、頁的常數、`pages_to_read`、`page_base`、`pages_after_read`、`cursor_after_read`（第 5 版）、`page_entity`、`skipped_page_warning`、`ReadResult.warnings`、例外 `ChannelFullError`／`RateLimitError`（PR-1）與 `PageUnavailableError`（2c）、協定 `PageBackend`（`page_size`、`page_size_at`，`runtime_checkable`）與 `PageOpener`（`open_page`）、Fake 的頁大小覆寫表（PR-1）與依頁存留言（PR-2） | 不知道註冊簿 |
| `fake_backend.py`（新，3-0；D-35） | `FakeComment`、`FakeBackend`（從 `backend` 搬來）；PR-3 的鉤子（3b：`open_page`、`opened`、`set_send_error`、`size_calls`、`page_date`、鎖） | 只給測試與本機開發；正式程式不 import |
| `github.py` | §4 的分辨；`page_size`；`open_page`；`read`／`check` 逐頁合併、沒讀過的頁從頭讀、退役頁失敗略過 | 不選寫哪一頁 |
| `state.py` | 存讀 `Cursor.pages`（§2.1 的序列化與壞資料處理）；`PendingPageStore`（待註冊紀錄，PR-3） | 不解讀 `pages`；不決定何時用待註冊紀錄 |
| `channel_config.py` | 解析 `retired` | 不決定讀哪幾頁 |
| `registry.py` | `rollover` 的動作與 `from`、`rollover_comment`、協定 `RegistryCounter` 與 `comment_count`（GET issue；Fake 也有）、`post_comment` 與 `post_with_retries` 回傳留言 | 仍只用標準庫 |
| `registry_rules.py`（新） | `rebuild_channels`、`render_channels`（從 `registry_rebuild` 搬來，加上 `rollover`） | 純函式，不做 I/O；只用標準庫與 `registry` |
| `registry_rebuild.py` | workflow 殼：`Rebuilder`（含回報去重）、`main`、環境變數 | 不放規則 |
| `rollover.py`（新） | `PageWriter`、`ensure_successor`、協定 `PendingPages` 與 `MemoryPendingPages`、`SuccessorError`；`refresh`、`backends`、`PendingPages`、`warn`、時鐘由呼叫方注入（§6.4 3b） | 不讀游標、不列訊息、不寫 stderr；不 import `channel_config`、`state` |
| `client.py` | PR-1 不動；2c 的 `ReadBatch.warnings`（只轉交，不寫 stderr）；PR-3 起 `Angareion.send` 經 `PageWriter`；讀的部分不變（跨頁在 backend 裡） | 不印警告 |
| `log_cache.py` | v3 逐頁 | — |
| `decision_cli.py` | 寫入改經 app 的 `PageWriter` | — |
| `channel_cli.py`（新） | `channel create`／`delete`（從 `cli.py` 搬來；PR-2 起 `delete` 關頁前的過期檢查）、`status`、`rollover` | 不做 parser 以外的派發；不 import `cli` |
| `cli.py` | parser、派發到 `channel_cli`、注入（PR-3：`PageWriter` 的 `refresh` 與 `PendingPageStore`）、`cmd_send` 的 ≥2000 警告（PR-1）、`--capabilities` 與 `CAPABILITIES`（PR-2） | — |

### 8.2 依賴方向（新增的邊以 ＋ 標示；沒有環）

- `backend → {messages, config}`
- `github → {backend, messages, transport, config}`
- `state → backend`
- `channel_config → {backend, config}`
- `registry → transport`
- ＋`registry_rules → registry`
- `registry_rebuild → registry`；＋`registry_rebuild → registry_rules`
- ＋`fake_backend → {backend, messages}`（3-0；正式程式不得 import 它）
- ＋`rollover → {backend, messages, registry, registry_rules}`（`messages` 只為 `Message` 型別，第 7 版補）
- `client → {backend, messages, state}`；＋`client → rollover`
- `log_cache → {backend, messages, config}`
- `decision_cli → {decision, log_cache, backend, messages}`
- ＋`channel_cli → {backend, channel_config, registry, registry_rules, client, rollover}`（`registry_rules` 是 PR-2 的 `delete` 過期檢查）
- `cli → *`

說明：

- 規則（註冊簿→對應）只有一份，在 `registry_rules`：workflow 經 `registry_rebuild` 用它，寫入端經 `rollover` 用它。寫入端不再 import workflow 的入口模組（A-1）。
- `rollover` 不 import `channel_config`：backend 名由呼叫方（`channel_cli`、`client`）從 `ChannelStore.backends()` 傳進來；W-9 的 `refresh` 也由 `cli` 注入。
- `rollover` 不 import `state`：待註冊紀錄經 `rollover` 自己定義的協定 `PendingPages`，`state.PendingPageStore` 在結構上符合它，由 `cli` 注入（`cli → state` 本來就有）。
- 第 1 版的邊表漏了 `backend → config`、`log_cache → config`（審查 A-6），本版補上；不影響無環。

### 8.3 SOLID

- **S**：讀哪幾頁（`pages_to_read`，純）、寫哪一頁（`PageWriter`）、頁怎麼存（channels.json 與 `registry_rules`）、重建怎麼跑（`registry_rebuild`），是四個不同的模組。`github` 只負責「對一頁做 HTTP」再合併。
- **O**：`ChannelBackend` 不加方法。頁相關的能力另立 `PageBackend` 與 `PageOpener`，同 #468 的 `EnumerableBackend` 先例；`LogCache` 逐頁呼叫既有的 `enumerate_conditional`。
- **L**：Fake 與 GitHub 都實作兩個協定，同一組契約測試（C-3、C-10、C-11、C-15、C-22、C-23）兩邊都跑。
- **I**：讀側只要 `ChannelBackend`；`channel status` 要 `PageBackend`（只讀）；只有 `PageWriter` 要再加 `PageOpener`（A-5）。
- **D**：`PageWriter` 依賴協定（`PageBackend`、`PageOpener`、`CommentPoster` 加上 `all_comments`、`PendingPages`）與注入的 `refresh`，不依賴具體的 GitHub client、`ChannelStore` 或狀態檔。
- **符合但要說明的一處**：`Entity` 多了 `retired`，所有讀者都會拿到它。這是資料，不是行為；`pages_to_read` 是唯一解讀它的地方，而且回傳的是 `retired=()` 的單頁實體，下游拿不到頁的清單。

### 8.4 架構師的裁決（審查 A-1～A-6）

| # | 問題 | 決定 | 理由 |
|---|---|---|---|
| A-1 | 純規則住在 workflow 入口模組，被寫入端 import | **採納**：新增 `registry_rules.py`，`registry_rebuild` 只剩殼 | 寫入端（每個席位）不該依賴 workflow 的入口與環境變數處理；規則仍只有一份。`registry_rebuild` 照樣 import 這兩個名字，既有測試的 import 不必改 |
| A-2 | 本機重算與 workflow 的版本漂移 | **採納**：S-2 的相容檢查；不相容就不開頁，P 沒滿就寫 P 並警告，滿了就 exit 2 | 開一張重建可能不接受的頁，訊息就沒人讀得到（M-2 同源）。不相容通常只是設定檔過期，等下一次拉取就會好；硬開的代價比等待大 |
| A-3 | `cli.py` 919 行，加子命令會過 1000 | **採納**：PR-1 新增 `channel_cli.py`，把 `channel` 家族整組搬過去 | 與 `decision_cli.py` 同一個先例；在 PR-1 就搬，PR-3 加 `rollover` 時不必再動 `cli.py`。搬移不改行為，測試經 `main` 呼叫，不受影響 |
| A-4 | 「頁」的概念往下漏，要不要另立 `Page` 型別 | **部分採納**：`pages_to_read` 改成回傳單頁實體，不回傳 issue 編號；不另立 `Page` | 一頁就是一個 `retired=()` 的 `Entity`：`ChannelBackend` 的每個方法本來就是對一個 repo＋issue 做事。另立 `Page` 要把所有方法的簽名改一遍，或兩套並存。改成回傳單頁實體，下游就不必碰編號 |
| A-5 | `PageBackend` 讀寫混在一起 | **採納**：`PageBackend`（`page_size`、`page_size_at`）與 `PageOpener`（`open_page`）分開 | `channel status` 與 PR-1 只需要讀；開頁只有 `PageWriter` 用，而且到 PR-3 才有 |
| A-6 | 依賴邊表漏邊 | **採納**，§8.2 已補 | — |
| A-7（重審） | `pages` 的保留規則放哪 | 純函式 `backend.pages_after_read`（第 4 版連同 `page_base`），與 `pages_to_read` 同處；`backend.read` 是唯一呼叫者 | 讀哪幾頁、讀完記哪幾頁是同一個概念的兩半，放一起才不會各改各的；純函式可以逐條單元測試（C-10）。`state` 照舊只存取 |
| A-8（重審） | 孤兒頁：重用 backend 上的頁，還是記在狀態目錄 | 狀態目錄：`state.PendingPageStore`，經 `rollover.PendingPages` 協定注入（§1.3） | 在 backend 上找要列 repo 的所有 issue，每次 S-4 都付，而且可能撿到別的寫者正要註冊的頁；本機紀錄只重用自己開的頁，成本一個小檔 |
| A-9（重審） | `check` 以所有可解析的訊息（第 3 版寫「所有留言」，第 4 版改，nit-2）推動 `_scan_since` 是否違反 D2 的掃描窗前提 | 不違反，但只限從頭讀模式，key 另加 `\|unread` | 從頭讀模式的窗不進游標、`read` 不用它，漏看只會延遲 `check`，不會丟訊息；起點不是空游標的頁照舊只由符合 scope 的留言推動（§2.2） |
| A-10（第 2 次重審） | `pages`＋`held` 要不要合成「頁號→該頁游標」的單一結構 | **採納**：`Cursor.pages: dict[int, Cursor] \| None`，每一頁用自己的游標照單頁規則讀；`held`／`HeldPage` 刪除；主游標保留（寬限判斷、`None` 的起點、舊版相容）。§2.1、§2.2、C-10、C-11、C-22、C-23 已同步 | 第 3 版要兩個結構（頁集合、凍結的游標）、三條讀取路徑（從頭讀、`held`、主游標）、三條更新規則（集合公式、凍結取最早、讀成功移出）；新版是一個結構、一條讀取規則（`page_base` 決定起點，其餘是單頁現行規則）、一行更新式。關鍵事實是 `advance_cursor` 吃的 entries 本來就是頁上**所有**可解析的訊息，所以每一頁的游標靠自己就會前進，不需要借主游標。附帶的好處：M-1（`None` 時被略過）與 M-3（新游標）都落在同一個定義裡；讀取不再依賴跨 issue 的 id 遞增（§10 第 4 項縮小）；別頁前進不再讓這一頁的 ETag 紀錄失效。代價：檔案裡每一頁多存一份三欄（`seen` 只含 60 秒窗內的 id，量小）；單頁頻道主游標與 `pages[現頁]` 重複一份 |
| A-11（第 2 次重審） | 游標的序列化與 `Cursor` 的相等 | `pages` 是 `None` 不寫鍵；有值時每頁照主游標同一種三欄格式。`Cursor` 照舊是普通 dataclass，`==` 含 `pages`（不設 `compare=False`）。ETag 紀錄的游標是那一頁的起點（`pages` 恆為 `None`），`_same_cursor` 不變 | 不寫 `None` 讓只存 `Cursor()` 的既有測試與舊檔不變；`==` 若不比 `pages`，`pages_after_read` 與 `state` 的測試會在 `pages` 錯時照樣通過，比讓三個既有斷言加上 `pages` 更糟。會轉紅的既有斷言列在 C-15，只能加 `pages`，不放寬三欄 |
| A-12（第 3 次重審 L-2） | 新游標要不要讀全部退役頁；`retired` 要不要有上限 | **新游標設上限、`retired` 不設上限**。新游標只讀 `until + 48h > U` 的退役頁（U＝最大的 `until`），純函式、只看實體（§2.1、C-9）；`retired` 照舊只增 | 不設上限時，新游標每次的成本是「頁數×整頁」，而且 `channel:`／`to:`／`mention:` 各付一次；容器重建會清掉狀態目錄（環境說明），所以這不是罕見情況，而是每次重建、每個席位都付，同時重建的席位共用 rate limit。成本隨換頁次數線性長大，是結構上沒有上界。把新游標當成「時間停在最近一次換頁」：它讀的頁就是那一刻任何讀者都還會讀的頁，與寬限規則同一個 48 小時、不新增常數；最新的退役頁一定在內，所以 C-11 的新游標情境、I-2、I-9 不受影響。不用「本機時鐘或回應 `Date` 減某個年齡」：`pages_to_read` 要在第一個請求之前決定，而且 D4 只認後端時間。`retired` 不設上限：讀取已經靠寬限與這條上限而不隨頁數長大，剩下的成本只在 `log`（每頁一個條件請求）；截掉 `retired` 會讓 `log` 失去歷史，又要多一種註冊簿規則，等真的需要時另開單（§11）。行為上的影響（新身分讀不到更早頁上寄給自己的舊訊息）交 PM（Q-9） |
| A-13（第 3 次重審 nit-2） | 單筆壞 `pages[p]` 的退路 | 鍵合格、值壞 → 那一頁用主游標的三欄；鍵壞 → 丟掉（§2.1、C-23） | 「丟掉＝從頭讀」對現頁是最多 2500 則整頁重送，對 agent 的傷害比漏幾則大，也與 `pages` 整個壞掉時退回主游標的方向相反。主游標正是 `pages` 是 `None` 時每一頁的起點，讓兩種壞資料走同一個方向（可能漏、不重送）；單頁頻道 `pages[現頁]` 本來就等於主游標，所以退回主游標是精確的。鍵壞的那一筆對不到本程式寫過的任何一頁，丟掉不影響任何頁。`state` 只是把主游標的三欄複製過去，不決定讀哪幾頁，仍是「只存取、不解讀」 |
| A-14（第 5 版） | PR-2 怎麼切 | 2a（型別、純函式、序列化、`retired` 解析、Fake 依頁存放）、2b（`registry_rules`、`rollover`、回報去重、`delete` 檢查）、2c（讀側跨頁、`--capabilities`、`status` 的頁資訊）；介面見 §6.3 | 依「沒有呼叫端的純部分」「寫入側的註冊簿」「讀側接線」三條線切，每一個都能單獨合併、合上後 main 不更糟。`paged-read` 是讀側完成的宣告，只能在 2c；跨頁迴圈的游標算術抽成 `cursor_after_read`，讓 GitHub 與 Fake 不各寫一份。新增的 `PageUnavailableError` 是 `BackendError` 的子類，只為讓迴圈分辨「可以略過的頁錯誤」，現頁的錯誤對外不變；警告經 `ReadResult.warnings`／`ReadBatch.warnings` 往上傳，backend 與 client 不寫 stderr（與現行一致） |

### 8.5 被捨棄的方案

| 方案 | 捨棄理由 |
|---|---|
| 只由 workflow 開新頁（單一寫者） | workflow 的 `GITHUB_TOKEN` 不能寫私有的 AiCompany-channel，要加 PAT secret；`cancel-in-progress` 下開 issue 不是冪等 |
| 在舊頁貼「搶頁」標記、最小 id 的贏 | 複寫延遲下仍會誤判，多吃舊頁額度，多一輪讀取；全序已經由註冊簿給了 |
| `rollover` 帶 CAS，輸的人被拒 | 註冊簿列表有複寫延遲，輸的人不一定在寫入前知道自己輸了。被拒之後，它寫進新頁的訊息要靠另一套機制才讀得到。改成旁頁＋S-8 改寫贏家：旁頁通常是空的；萬一有訊息，讀者從頭讀，照樣送達 |
| 只靠寫者端的 S-8（審查 H-1 的建議 a），讀者端不改 | 窗只縮到列表的複寫延遲，不會消失；而且擋不住 I-9（舊頁先寫滿期間，已落地的舊版寫者寫新頁，未落地的讀者越過它）。讀者端的 `pages` 兩種都關掉。S-8 仍保留，讓旁頁通常是空的 |
| 只明寫「旁頁可能漏」為已知風險（審查 H-1 的建議 b） | I-9 不是旁頁的問題，每次換頁都會遇到，不能只當風險 |
| 讀者讀所有頁 | 頁數會一直累積，每次讀都要多 N 個請求；寬限期之後的舊頁不會再有新訊息（例外見 §2.3） |
| 換頁後立刻改寫新頁（Q-2 的 A） | config 還沒拉新的讀者會在換頁後最多 900 秒（`wait` 甚至更久）看不到新訊息，aicomm 的 poke 叫醒會讀到空的；作為 Q-2 的選項保留，手動換頁就是這個行為 |
| 舊頁貼「換頁了」的廣播讓讀者 fetch | 要等落地才有意義；`send` 若等落地（最多 2 分鐘）會超過 pmo 呼叫 angareion 的 60 秒逾時（`pmo.adapters._TIMEOUT`），pmo 會當失敗而重送；不等又沒用 |
| 把 `-b` 併成頁 | 游標 key 與訊息本文的 `channel` 不同，會重送（§3） |
| 鎖住舊頁 | 擋不住擁有者的 token（§1.5） |
| 只看 `comments ≥ 2500` 判「滿」 | 上限的計法沒驗過；若上限早一點生效，2400～2499 就回到今天的故障（§4） |

## 9. 主 spec 要補的地方（PM 撰寫；架構師只看介面能否一致表達）

- `angareion/spec.md`「七個介面家族」：`channel` 家族多 `status`（唯讀）與 `rollover`，要寫進允許的指令清單（Q-1）；`channel rollover` 的 stdout 只印頻道名。
- `angareion/spec.md`「輸出與結束碼」的「後端錯誤」情境：分成 rate limit 與「頻道這一頁滿了」，exit 照舊是 2。退役頁讀不到時略過並警告（C-22）。
- `angareion/spec.md`：新增旗標 `--capabilities`（C-24）；`--version` 不變。
- `angareion/channel-config/spec.md`「channel 設定檔」：每筆可以有 `retired`（§1.1）。「channel 對應的更新」：多 `rollover` 動作（§1.2 的表）；回報去重。「讀取與 pull」不變（`wait` 輪詢中仍不 fetch）。
- 「目標與游標」：游標以頻道為單位、跨頁接續；游標記得讀過哪幾頁，沒讀過的頁從頭讀（§2）。
- 「目標與游標」的新身分情境（spec「開之前寄給自己的那一則讀得到」）：換過頁的頻道，範圍是現頁與最近一次換頁 48 小時內退役的頁（Q-9 選 A 時；§2.1）。
- **各條屬於哪個 PR**（第 5 版）：
  - PR-1（已 merge）：`channel status` 已由 PM 補進 spec（`b954a5e`）。**只剩兩件，PM 於 #545 補**：「後端錯誤」情境分成 rate limit 與「這一頁滿了（或看起來滿了）」，exit 照舊是 2；`send` 在現頁 ≥2000 時 stderr 一行警告（C-4，stdout 與 exit 不變）。
  - 2b：`channel-config` 的 `retired` 與 `rollover` 動作、回報去重；`channel delete` 的過期拒絕。
  - 2c：`--capabilities`、退役頁讀不到時略過並警告、「目標與游標」兩條。
  - PR-3：`channel rollover`。
  - 主 spec 不在架構師可以 commit 的路徑（AGENTS.md），由 PM 撰寫。

## 10. 測試策略

- 全部在 `tests/`，用 `uv run pytest`。模組第一行 `import _guard`。
  - 不連真 GitHub：GitHub 的分支用 `transport` 假物件，可以設定 403 body 與標頭，並記錄請求 URL；註冊簿用 `FakeRegistryClient`，加 `before_post` 鉤子，以及「列表時隱藏某幾則」的設定（C-18 的 I-2）。
  - 時間用注入的時鐘與固定的 `Date` 標頭。
- **層級**：
  - 純函式（`pages_to_read`、`rebuild_channels`、`parse_comment`、§4 的分辨）用單元測試；
  - 跨頁讀寫用 Fake 與 GitHub 假 transport 各跑一組同樣的情境（C-10、C-11、C-12、C-22、C-23）；
  - CLI 用 `main(..., backend=, store=, registry=, transport=)` 的既有接縫。
- **競態**（C-18）：兩個執行緒，用 `threading.Barrier(2, timeout=5)` 當 gate（第 8 版改正；第 7 版這裡寫 `threading.Event`，與 §6.4 不一致，`Event` 也沒有逾時保護）。A 與 B 都在「重算完、要貼留言」前停住，兩邊都到了才一起放行；不用 sleep；某一邊走偏時另一邊 5 秒後 `BrokenBarrierError`，不會卡住。寫法見 §6.4 的「C-18 的 gate」。要跨行程時，改用暫存目錄裡的 gate 檔：子行程輪詢檔案存在，以短間隔檢查並設總逾時，不靠睡固定秒數猜時序。
- **golden**：用一份與現行 channels.json 同形狀的合成註冊簿夾具，比對 PR-2 前後的重建輸出，要求逐位元組相同（C-8）。
- **改壞驗證**（每個 PR 至少下列幾種，要能讓對應測試失敗）：
  - PR-1：C-1 拿掉 `retry-after` 判斷；C-1 訊息拿掉 `not retrying`；C-2 不做 GET 探測；C-2 把 2400 門檻拿掉；
  - PR-2（第 6 版依子 PR 分；每一項寫「改壞 → 轉紅的測試」）：
    - **2a**：C-9 寬限比較改成 `<=`，或拿掉新游標的上限（A-12），或上限的比較改成 `>=` → C-9 的單元測試；C-23 的 L-1 與 A-13 兩種 → C-23 的 `state` 案例；`cursor_after_read` 只對現頁 `advance_cursor` → C-10 的純函式單元測試；**C-23 列的五種 `page_base`／`pages_after_read` 改壞 → C-10 的純函式單元測試**（`page_base` 的三種情況、`pages_after_read` 的六種情況逐一對應；「被略過」在單元測試裡就是 `to_read` 有、`entries_by_page` 沒有的頁，所以「不寫入結果」的改壞由「結果的鍵恰好是 `to_read`」那一條轉紅）。2a 沒有情境測試可轉紅，這五種以單元測試為準。
    - **2b**：C-25 拿掉去重 → C-25；C-8 旁頁改成取代現頁 → C-8 的競態列；`channel delete` 拿掉過期檢查 → C-8 的 CLI 測試。
    - **2c**：C-10 讀取只對現頁 `advance_cursor`、每頁各取一個 `floor` → C-10 的情境；C-11 ETag key 不含頁、拿掉從頭讀模式的 `_scan_since`、從頭讀模式不認新游標 → C-11；C-14 不讀退役頁 → C-14；C-22／C-23 的五種改壞再以情境測試各驗一次（列在 C-23）；`skipped_page_warning` 帶回 `Warning: ` 前綴 → C-22 的 `log` 測試（W-1）。
  - PR-3（第 7 版依子 PR 分；每一項寫「改壞 → 轉紅的測試」）：
    - **3-0**：在 `backend.py` 留一行 `from angareion.fake_backend import FakeBackend` → `test_d35_backend_does_not_reexport_the_fake`；在任一正式模組 import `fake_backend` → `test_d35_no_production_module_imports_fake_backend`。
    - **3a**：`post_with_retries` 改回回傳 `None` → C-17 前置的回傳值測試；`_refused_send` 改回丟一般 `BackendError` → `WriteRefusedError` 的型別測試；`PendingPageStore.get` 對壞資料丟例外 → 壞資料各案例；`FakeRegistryClient.before_post` 放到鎖內 → 3a 的 `_is_owned()` 斷言（第 8 版；3b 的 C-18 也會轉紅：逾時）；`_open_issue` 的非 JSON 訊息沒照 `context` → `create_channel` 的逐字比對；`PendingPageStore.put` 對 `OSError` 不吞 → 不可寫目錄的測試（root 下跳過）。
    - **3b**：C-16 拿掉「先寫舊頁」（A 整段）→ W-1；W-4 不接 `WriteRefusedError` → W-4 的第 7 版測試；拿掉本機時鐘的跳過 → L-2 的 `size_calls` 斷言；拿掉 W-9 的 `refresh` → W-9 refresh 測試；W-10 在 W-8 失敗後再呼叫一次 `ensure_successor` → W-10 的 `open_page`／註冊簿次數斷言；拿掉最後失敗前的警告 → W-9 最後失敗的測試；C-17 先寫新頁再貼註冊 → S-7 的「新頁上 0 則」；拿掉 S-8（事後重算）→ C-18 的「旁頁上 0 則」；拿掉 S-2 的相容檢查 → S-2 四種；拿掉待註冊紀錄 → 孤兒頁上界（`open_page` 次數變多）；S-6′ 也刪紀錄 → S-6′（`open_page` 次數變多）；拿掉 S-8′ → S-8′ 測試（`open_page` 次數變多，或 `BackendError`「could not settle」）；拿掉 S-4′ → S-4′ 測試（註冊簿的留言數在重試後變多）；S-4′ 不看 `repost` → `repost=True` 的測試（`post_comment` 0 次）；`write` 對壞的 `until` 不包 `ValueError` → 低-3 的測試（變成 `ValueError`）。
    - **3c**：`cmd_send` 留著 PR-1 的 `_warn_if_page_is_filling` → C-20 的「恰好一行」（變成兩行）；`decision_cli` 有一處仍直接 `backend.send` → C-20 的 `decision ask` 寫到新頁；`CAPABILITIES` 少 `paged-write` → C-24；`channel rollover` 不先 `fetch` → C-19 的 S-3 情境（`open_page` 變成 1 次）。
- **沒驗到、也不在測試裡驗的**（真 GitHub）：
  1. 達上限時的實際狀態碼與 body（issue 本文只記錄到 403 和我們自己的訊息）；
  2. 上限是否恰好是 `comments ≥ 2500`，有沒有把時間軸事件算進去；
  3. GET issue 的 `comments` 會不會延遲；
  4. comment id **跨 issue** 是否全站遞增。這是這份設計新增的依賴；D4 只依賴同一實體內遞增。審查者對 MyAiStorage 的 71 則留言抽樣，結果單調，但那不是 AiCompany-channel，也不是證明。第 4 版起每一頁用自己的游標讀，送達（不漏、不重送）不依賴它；若不成立，只有同一次讀取回傳的訊息跨頁的相對順序可能與到達順序不同，主游標的 `position`（跨頁最大 id）只用在 `pages` 是 `None` 的起點。另外，頁的寬限移除依賴主游標的 `at` 單調前進，而 `at` 是最大 id 那則的時間；不成立時最壞是已移除的頁回到 `to_read`、被從頭讀而整頁重送（不會漏；§2.1，重審 nit-1）；
  5. 交互參照會不會吃留言額度；
  6. 鎖住的 issue 是否擋得住擁有者留言（文件說擋不住）；以及計數很小的頁收到與滿頁相同的拒絕句（§4.1 的樣本）時，是不是代表整個 repo 或帳號被禁止留言（第 8 版補；§4.1 對這種情況不換頁）；
  7. 註冊簿列表的複寫延遲；
  8. 留言的 `since` 過濾是否以 `updated_at` 為準（C-26 的 `late` 旗標靠它；推論）。
  - 第 1 項會在 PR-1 上線後，靠錯誤訊息裡的 GitHub `message` 取得樣本。

## 11. 風險與未決

| # | 問題 | 選項 | 建議 | 誰決定 |
|---|---|---|---|---|
| Q-1 | 新指令 `channel status`、`channel rollover` 與旗標 `--capabilities` 進 spec | 都加／只加 status、換頁只走自動與手動註冊 | 都加：PMO 需要手動換頁 w2、w3；盤點需要能力標記 | PM |
| Q-2 | 換頁後寫哪一頁 | A 立刻寫新頁（簡單；config 舊的讀者最多延遲 900 秒）／B 舊頁先寫滿到 2490（24h 內） | B | PM（行為） |
| Q-3 | `-b` 頻道 | 換頁後停用並在 7 天後 delete／永久保留／併回（不可行） | 停用＋7 天後 delete；要保留歷史就不 delete | PMO |
| Q-4 | ≥2000 告警與頁旗標怎麼到 PMO | `pmo tick` 定期跑 `channel status`（另開 d）／PMO 手動跑／寫者發 Angareion 訊息（要新設定與去重） | `pmo tick`；在那之前手動 | PMO |
| Q-5 | 舊頁封存 | 不動（建議）／鎖／關／加 label | 不動 | PMO（經本人） |
| Q-6 | 手動換頁與 PR-3 merge 的前提 | §6.2 的盤點（`--capabilities` 含 `paged-read`、`config_pull_interval` 在範圍內、同一身分沒有未升級的安裝）通過才做／直接做 | 先盤點，由 PMO 做 | PMO |
| Q-7 | 門檻值 | 2000／2400／2490／24h／48h（§1.3）；`config_pull_interval` 上限 6h（§6.2） | 照表 | PM |
| Q-8 | 換頁前的營運 | m1、w4 先開 `-b`／等 PR-2 之後手動換頁 | 用 PR-1 的 `channel status` 盯；剩不到 50 則還沒有 PR-2 就開 `-b` | PMO |
| Q-10 | 403「Commenting is disabled」的原樣樣本（第 7 版，§4.1） | 樣本到了照 §4.1 判斷要不要改；改的話另開 d 或併進 3a | 先看 w5 當時的 stderr 是不是 `is full`：是就不改程式 | PM（樣本由 w5 提供） |
| Q-11 | PR-3 之後手動換頁的做法（第 7 版，§6.4 的並存表） | runbook 第 3、4 節改成 `angareion channel rollover <C>`、gh 步驟留作備案／照舊只用 gh | 改成 `channel rollover`（它會走 S-3 與待註冊紀錄，不和自動換頁搶頁） | PM（runbook 由 PM 改）、PMO（照做） |
| Q-9 | 新游標（新身分、狀態目錄遺失）讀退役頁的範圍（第 5 版，A-12） | A 只讀最近一次換頁 48 小時內退役的頁（更早的頁上寄給自己的舊訊息讀不到，`log` 查得到）／B 全部退役頁（不漏，但請求數與重送量隨換頁次數長大） | A | PM（行為；結構面架構師建議 A，選 B 時只要刪掉 C-9 的上限與 §2.1 那一條，其餘不變） |

其他風險：

- **§2.3 的三種漏**：設定檔過期的舊版寫者、退役頁整個寬限期讀不到、舊版讀者。前兩種有 `channel status` 的旗標或警告，第三種由盤點擋住；`log` 都查得到。
- **註冊簿的上限**：#47 也是 2500 則，現在 52 則；每次換頁多 1～2 則，回報去重後每種多 1 則；自己的 `rollover` 被拒時，同一筆待註冊紀錄最多貼 1 次（S-4′，第 8 版），之後只有 PMO 的 `channel rollover` 會再貼。短期沒事，`channel status` 會顯示。真的要處理時，註冊簿換頁是另一張單。
- **舊版讀者**：channels.json 換頁後，舊版讀者只讀新頁，看不到舊頁剩下的未讀訊息。手動換頁（§6）與 PR-3 都要等盤點（§6.2）。
- **寫入多一個 GET**：用的是 core rate limit，不是 secondary；`send` 的量遠小於輪詢。`send` 的請求數與 PR-1 相同（PR-1 的那一個 GET 就是 W-6 的 `page_size_at`）；`decision` 的每一次寫入（`ask`／`update`／`withdraw` 的 `_write_one`、自動 snapshot、`submit` 的每一筆）在 PR-3 之後**多 1 個 GET**（PR-1 沒有替 `decision` 量頁大小），`submit` 一次 N 筆就多 N 個（第 8 版補，審查低-2）。
- **`page_size` 回應的 `Date` 當作「現在」**：沒有 `Date` 時，跳過 W-1，直接看現頁（保守，等於 Q-2 的 A）。
- **從頭讀的成本**（第 5 版依重審 L-2 改寫）：
  - 沒讀過的頁（舊讀者）：一般是剛開的新頁，幾則；離線很久的讀者最多把一頁讀完（≤25 個請求，一次性）。那些訊息本來就是它沒讀過的。
  - 新游標（新身分，或容器重建後狀態目錄遺失的身分；`channel:`／`to:`／`mention:` 每一種 key 各算一次）：讀現頁與「最近一次換頁 48 小時內」退役的頁，一般是 2 頁（≤50 個請求，一次性），不隨換頁次數長大（A-12）。代價：新游標讀不到更早退役的頁上「寄給自己」的舊訊息（`log` 查得到）；單頁頻道與現行完全相同。這是行為上的取捨，列為 Q-9 請 PM 確認。
- **`retired` 不設上限**（A-12）：讀取已經不隨頁數長大（寬限期＋新游標上限）；只有 `log`／`decision list` 對每一頁發一個條件請求（沒變時是 304；GitHub 文件說 304 不計入 primary rate limit，沒驗）。註冊簿與 channels.json 每次換頁多一筆，量級很小。回頭看的時機：任何頻道在 `channel status` 的 `pages` 到 10 時，另開單討論退役頁的歸檔（例如註冊簿多一種動作把很舊的退役頁移出 `retired`）。

## 12. 第 2 版的改動（對照審查）

- H-1：讀者端加游標 `pages`，沒讀過的頁從頭讀（§2.1、C-23）。寫者端加 S-8 事後重算並併入自己那則（§1.3、C-17）。§1.4 重走交錯，含第 1 版漏掉的 I-9。§8.5 與 D-34 的理由跟著改。
- M-1：`PageWriter` 與 `ensure_successor` 寫成表（W-1～W-12、S-1～S-9），C-16／C-17 逐列對應。第 1 版「回 `ChannelFullError` → 第 3 步」的錯誤引用已刪。
- M-2：S-2 的相容檢查，不相容不開頁。
- M-3：§4 規則 3 的 2400 門檻（`presumed`）。
- M-4：§6.2 的能力標記、盤點規則與誰盤點；C-24。
- M-5：W-8／I-8 讓 ≥PR-3 寫者不再一直寫舊頁；舊版寫者列為部署前提（§6.2）與已知風險（§2.3），偵測是 C-26 的 `late`。
- M-6：C-22。
- L-1：交叉引用改正（§1.1 指 §6.2、§0 指 §10、§1.3 的表）。L-2：§1.2 的文字。L-3：回報去重（§1.2、C-25）。L-4：§0.3、§10 第 4 項改成「新增依賴」。L-5：C-1 保留字樣。L-6：§0.5、§4 改成「標頭優先，再看已知字樣」。L-7：`comment_count`（§8.1、C-5）。L-8：§0.9、C-14 改成「輸出不變」，並寫明升版成本。L-9：C-20 的警告語意。
- A-1～A-6：§8.4。
- 審查的其他點：
  - 「孤兒契約」：`open_page` 由 C-17 的 S-4／S-5 覆蓋；沒有 `Date` 的測試在 C-16。
  - C-15 的措辭改成「不弱化」。
  - P 在 2400～2499 期間每次寫入的成本寫進 §1.3。
- 自己重讀時找到的同類不一致：
  - §0.3 說「游標不用改格式」，與本版的 `pages` 不符，已改；
  - §9 寫「六個介面」，與 spec 的「七個介面家族」不符，已改；
  - 第 1 版 §6 說「≥PR-2 的讀者兩頁都讀」，沒提舊讀者以外的晚到問題（I-9），已補；
  - §3 的重送理由改成依 `pages` 的說法；
  - 手動換頁沒有「舊頁先寫滿」（舊版寫者不會先寫舊頁），已在 §6.2 寫明。

## 13. 第 3 版的改動（對照重審）

- **H-1**：`pages` 的語意改成「讀過、而且還在追蹤的頁」；更新規則 `pages_new = ((prev ∩ skipped) ∪ read_ok) ∩ to_read`，純函式 `backend.pages_after_read`（§2.1、§2.2 的「讀退役頁失敗」列、§2.3 第 2 項）。
  - **另加 `held`（派工沒寫、我加的；第 4 版以每頁游標取代，見 §14 的 A-10）**：只保留 `pages` 時，略過那一輪主游標被別頁推進，恢復後用主游標讀 R 會漏掉略過期間 R 上新到、而時間早於新 `at-60s` 的訊息（§2.1 的例子）。所以讀過而被略過的頁凍結自己的游標，恢復後用它接續。`pages` 的公式照派工不變。C-10 加單元測試；C-22 補「讀過的頁 404→恢復：已送達 0 則重送、略過期間新到的有送」與「沒讀過的頁 404→恢復後從頭讀」；C-23 補反向測試（已讀過的頁不被從頭讀）與兩種改壞；D-34 對應句改寫。
- **M-1**：從頭讀模式的頁，第一次整頁掃描乾淨後以所有留言的最新時間推動 `_scan_since`（key 加 `|unread`）；上界 ⌈n/100⌉＋(k−1)（§2.2）；C-11 補請求數斷言與改壞。A-9 的裁定在 §8.4。
- **M-2**：待註冊紀錄（`state.PendingPageStore`、`rollover.PendingPages`），S-4 先重試註冊同一頁；S-6／S-7／S-8／S-9／S-3 對紀錄的處置寫進 S 表（§1.3）；C-17 驗 `open_page` 次數。A-8 的裁定在 §8.4。
- **M-3**：能力標記改成獨立旗標 `angareion --capabilities`，`--version` 與 `test_angareion_version.py` 都不動（§6.2、C-24、C-15、§9、Q-1、Q-6）。
- **M-4**：PR-2 起 `channel delete` 關頁前強制 fetch，並以註冊簿重算確認本機現頁沒有過期，有證據才拒絕（§1.2、C-8）；`channel rollover` 先強制 fetch（C-19）。
- **M-5**：`presumed` 的訊息是 `looks full (or the token was refused)`（PR-1 已定；§4、C-2）。
- **L-1**：§2.1 寫明舊版 `save_many` 洗掉整個檔的 `pages`；§6.2 加「同一身分不得有未升級的安裝」。
- **L-2**：本機時鐘超過 `R.until + 48h` 直接跳過 A（§1.3、C-16）。
- **L-3**：`post_with_retries` 回傳最後一次成功的留言，`FakeRegistryClient.post_comment` 回傳留言（§1.3、C-17）。
- **L-4**：回報去重只對引用某則留言的回報；固定文字的回報每次都貼（§1.2、C-25）。
- **L-5**：W-9／W-10 在 S-2 不相容時先 `refresh`（強制 fetch）再重算一次（§1.3、C-16）。
- **L-6**：記下：PR-1 的 `decision` 寫入沒有警告（§6.1 第 2 點、C-4）。
- **PR-1 已定的 7 點**：§6.1、C-2～C-5、§8.1 的 `backend`／`registry`／`client`／`channel_cli`／`cli` 列。`client.py` 在 PR-1 不動（第 2 版寫成在 `client.py` 警告，與實作不一致）。
- 自己重讀找到的同類不一致：
  - 表格裡 code span 的 `|`（`repo#issue|scope`）會切斷表格欄位，已跳脫；
  - PR 切分表的 PR-1／PR-2／PR-3 內容補上本版新增的項目（`pages_after_read`、`delete` 的過期檢查、待註冊紀錄、`RegistryCounter`）；
  - §8.2 補 `channel_cli → registry_rules`（`delete` 的過期檢查）；寫明 `rollover` 不 import `state`；
  - §8.3 的 D 補 `PendingPages` 與 `refresh`；
  - §10 的改壞驗證補 C-11、C-16 的 `refresh`、C-17 的待註冊紀錄，C-22 改成本版規則；
  - §2.1 的 `None` 游標補「同一身分被未升級的安裝存過」這一種來源。

## 14. 第 4 版的改動（對照第 2 次重審）

PR-1（§6.1、C-1～C-5）一字未動；以下都是 PR-2／PR-3。

- **A-10（採納）**：`pages` 改成「頁號→那一頁的游標」，刪掉 `held`／`HeldPage` 與第 3 版的集合公式；新增純函式 `backend.page_base`，`pages_after_read` 剩一行（§2.1）。§0 第 3 點、§2.2 的 `read`／「讀退役頁失敗」／`wait`、§2.3、§6.2、§8.1、§10、C-10、C-11、C-22、C-23、D-34 同步。理由見 §8.4 的 A-10 列。
- **A-11**：序列化（`None` 不寫鍵、每頁三欄）與 `Cursor` 的 `==` 含 `pages`（§2.1、§8.4）。
- **M-1**：`prev.pages is None` 時，每一頁的起點是主游標；被略過的頁存成主游標（`page_base` 的第三種，§2.1），恢復後不整頁重送。C-10 的單元測試改成這條；C-22 加「舊游標＋退役頁 404」情境。
- **M-2**：C-15 列出允許改的三個既有斷言（`test_angareion_cli.py:401`、`test_angareion_library.py:239`、`test_angareion_backend.py:237`；後者是我重讀時找到的，審查列了前兩個），只能加 `pages`、不放寬三欄；序列化規則寫在 §2.1。
- **M-3**：從頭讀＝起點是空游標，新游標（`pages` 是 `None`、主游標空）也算；`check` 的掃描窗涵蓋它（§2.2）。C-11 加新游標的請求數案例與改壞，並寫明單頁新游標的唯一行為差（只少請求）。
- **L-1**：舊版 `--capabilities` 是 exit 1（非 0），盤點只認「exit 0 且含 `paged-read`」（§6.2）。
- **L-2**：S-6 拆成 S-6（`verify_channel` 回 `False` → 刪紀錄）與 S-6′（丟 `BackendError` → 保留紀錄）；孤兒頁上界的敘述改寫（§1.3）；C-17 補 S-6′ 與改壞。
- **L-3**：同一次 `read` 的所有頁共用同一個 `floor`（§2.1、§2.2、C-10）。
- **nit-1**：`summary.reports` 是全部，新增 `summary.posted` 是實際貼出的（§1.2、C-25）。
- **nit-2**：掃描窗以「所有可解析的訊息」推動，與 `read` 的 entries 同一個集合（§2.2、§8.4 的 A-9 列、C-11）。
- **nit-3**：`pages` 壞資料一律當作沒有（§2.1、C-23）。
- 自己重讀找到的同類不一致：
  - C-11 的「單頁時與現行相同」在 M-3 之後不再完全成立（新游標第二次起帶 `since`），已寫明差異與既有測試的處理；
  - C-11 加「別頁前進不讓 ETag 失效」（A-10 宣稱的好處要有測試鎖住）；
  - C-22「沒讀過的頁」情境原本沒寫 S 上訊息比游標舊，改壞（對沒讀過的頁存主游標）會測不出來，已補條件；
  - §10 第 4 項（跨 issue id 遞增）的影響縮小到回傳順序；§2.3 的「晚到」改成每頁各自的窗；
  - §6.2 的「舊版洗掉 `pages` 與 `held`」、§8.1 的 `backend`／`state` 列、§8.4 的 A-7 列、A-9 列的「所有留言」改成本版用語；
  - §10 的改壞清單把 S-6′ 放在 PR-3、加上 `floor` 與新游標兩種。

## 15. 第 5 版的改動（對照第 3 次重審，與 PR-2 的切分）

PR-1（§6.1、C-1～C-5）的契約一字未動；§6.1 只補「已 merge」與實作公開的兩個小函式。以下都是 PR-2。

- **L-1**：§2.1 寫明 `save_many` 對每一個 key 都寫 `pages`；C-23 加「存別的 key 不洗掉這個 key 的 `pages`」案例與改壞。
- **L-2**：新游標只讀最近一次換頁 48 小時內退役的頁（§0 第 3 點、§2.1、§2.2 的 `check` 成本、C-9 與改壞）；`retired` 不設上限、回頭看的時機（§11）；§11 的成本說法改寫。裁定在 §8.4 的 A-12，行為面交 PM（Q-9）。
- **nit-1**：§2.1「移除的頁不會回來」寫明依賴主游標 `at` 單調、不成立時的最壞結果；§10 第 4 項同步。
- **nit-2**：單筆壞 `pages[p]` 退回主游標（鍵壞才丟）；§2.1、C-23、改壞；裁定在 §8.4 的 A-13。
- **nit-3**：PR 表的 PR-2 列補 `page_base`。
- **PR-2 的切分**：新增 §6.3（2a／2b／2c 的範圍、契約、順序、安全性、介面簽名與檔案）；§0 第 9 點、§6 的 PR 表、§8.1 同步；裁定在 §8.4 的 A-14。新增的名字：`cursor_after_read`、`PageUnavailableError`、`skipped_page_warning`、`ReadResult.warnings`、`ReadBatch.warnings`、`FakeBackend.set_page_error`、`FakeBackend.deliver(page=)`、`RebuildSummary.posted`（第 4 版已有）。依賴邊沒有新增（§8.2 不變）。

## 16. 第 6 版的改動（對照 #545 的審查，Sonnet，head b954a5e）

PR-1 的契約與 2a／2b／2c 的範圍、契約歸屬、順序都不動；只補實作者要自己猜的地方。

- **W-1**：`skipped_page_warning` 的回傳值不帶 `Warning: `，前綴由 `cli._warn` 與 `LogCache` 的 `warn` 回呼加（與 PR-1 的 C-4 一致）；§6.3 的 `cli.py`／`log_cache.py` 兩處寫明走哪一個；C-22 改成「恰好一行」並加「前綴只有一個」的測試；§10 的 2c 加對應改壞。
- **nit-1**：`PAGE_FILL_WINDOW = timedelta(hours=24)` 放進 2a 的 `backend.py` 介面區塊。
- **nit-2**：`CONFIG_VERSION` 搬到 `registry_rules`，`registry_rebuild` 再 import 回來；`channel_config.CONFIG_VERSION` 不動。
- **nit-3**：`channel status` 註冊簿那一列的頁數恆為 1。
- **nit-4**：403 訊息抽成 `GitHubIssuesBackend._refusal_message`，`_request` 與三個列表入口共用。
- **nit-5**：§9 的 PR-1 那一條改成「只剩兩件，PM 於 #545 補」；§10 的 PR-2 改壞清單依 2a／2b／2c 分，五種 `page_base`／`pages_after_read` 改壞在 2a 由 C-10 的純函式單元測試轉紅；§6.3 的共通事項指過去。防呆與手動換頁步驟的歸屬仍是 PM 的事（§6.3 2c 的檔案段），不在這一版。
- **重讀 §6.3 找到的同類**（前綴與訊息由誰組）：2b 的 `channel delete` 過期拒絕寫明 stderr＝`Error: `＋§1.2 的句子；2c 的 `_page_flags` 查詢失敗照 `_page_count` 現行的 `Warning: channel '<C>': …` 寫法。沒有其他自帶前綴的字串。

## 17. 第 7 版的改動（PR-3 開工，BRIEF-pr3-arch，2026-11-26）

- 新增 §6.4：PR-3 切成 3-0（D-35 的 Fake 搬家）、3a（協定、例外、GitHub 的 `open_page`、註冊簿回傳留言、`PendingPageStore`）、3b（`rollover.py` 與 Fake 的鉤子）、3c（接線）。與 PM 的建議不同處：Fake 的鉤子從 3a 移到 3b，讓 3-0 與 3a 可以並行（D-37）。
- §1.3：
  - W-4 多認 `WriteRefusedError`（403／422 被拒、不是 rate limit、計數 <2400）：這種錯誤確定沒寫進去，退役頁被鎖或計數延遲時改寫現頁，不再 exit 2（§6.4 的並存表第 3 列）。`github._refused_send` 的訊息逐字不變。
  - W-9 的 refresh 寫成「以新實體從 A 重來」：意思與第 6 版「再呼叫一次 `ensure_successor`」相同，但新的實體若已落地就不必再讀註冊簿。
  - 新增 S-8′：事後重算裡現頁仍是 P（自己那則沒被套用）→ 失敗、紀錄保留。第 6 版沒有這一列，`PageWriter` 會在同一次 `write` 裡重開頁到 3 次。
- `write` 的警告改由注入的 `warn` 回呼印（`cmd_send` 的 PR-1 預檢刪掉，請求數不變）；W-9 最後失敗前先印 C-4 那一行，PR-1 的既有測試不改。
- §4.1：拿到 403 原樣樣本後要改的地方（函式、契約、測試），現在不改。
- §6.4 末：手動換頁與 PR-3 的並存表（Q-11）。
- §7：C-16、C-17 補第 7 版的案例；§8.1、§8.2 補 `fake_backend` 與 `rollover → messages`；§10 的 PR-3 改壞依子 PR 分；§11 加 Q-10、Q-11。
- 契約的意義沒有改：C-16～C-21 的既有文字都保留，只加案例。

## 18. 第 8 版的改動（對照第 7 版的非作者審查，Sonnet，head 8b75629）

切分（3-0／3a／3b／3c）、依賴圖與契約編號不變。

- **中-1**（S-8′ 之後每次重試都再貼一則 `rollover`，註冊簿只增不減）：採審查的第一個建議，新增 S-4′（§1.3 的 S 表、§6.4 `ensure_successor`）。紀錄裡的頁在 S-1 的 `before` 已有同義的 `rollover`（`parse_comment` 得到同一個 `Action`）而重算沒套用 → 直接 `REJECTED`，不驗、不貼，紀錄保留。不選「每 `(C, P)` 最多 N 則」：重建是純函式，第 2 則起的結果一定與第 1 則相同，N 只是讓灌的量變小；S-4′ 是 N＝1，而且不必再存計數。為了不讓「原因修好之後」永遠卡住，`ensure_successor` 多一個 `repost` 關鍵字參數，只有 `channel rollover` 傳 `True`（每次 PMO 執行最多多 1 則）。上界寫進 §1.3「註冊簿留言的上界」與 §11；C-17 的 S-8′ 案例改寫並加 S-4′ 的測試；C-19 補 `repost`；§10 的 3b 改壞加三項。
- **中-2**（§4.1 的樣本規則不看計數）：§4.1 第 2、3、4 點改成「樣本相符**而且**（計數讀得到且 ≥ `PAGE_WARN_AT`，或計數讀不到）才當滿」；相符而計數 <2000 照現行規則 3（`WriteRefusedError`）。C-2 的預定列與測試同步（多「樣本＋計數 2100」「樣本＋計數 0」兩個）；§10 第 6 項補「計數很小、句子相同」的未驗。
- **低-1**：`_open_issue` 改成 `what` 與 `context` 兩個字串，`create_channel` 的三種訊息逐字不變；`open_page` 的非 JSON 訊息照 `_json` 的格式寫明；3a 的測試加兩組逐字比對。
- **低-2**：`decision` 的每一次寫入多 1 個 GET（`submit` 多 N 個）寫進 §6.4 3c、§11、D-37。
- **低-3**：`write` 的 A 改用 `_maybe_recent(now, until)`，`until` 解析不了就當作窗外；C-16 加一條。
- **低-4**：3a 的唯讀目錄測試照 `test_angareion_etag.py:408` 在 root 下跳過。
- **低-5**：C-18 寫明 `hidden_ids` 放的是 A 貼的 `rollover` 註冊簿留言；用過 barrier 的夾具把 `before_post` 設回 `None`，另兩組各自建新夾具；§10 的 `threading.Event` 改成 `threading.Barrier(2, timeout=5)`。
- **低-6**：3a 加「`before_post` 被呼叫時 `_lock._is_owned()` 是 `False`」的斷言（不用執行緒）；§10 的 3a 改壞改成由它轉紅。
- **低-7**：(a) `post_with_retries` 的回傳寫成「成功的那一次（也就是最後一次）」；(b) `_write_one(config, backend, entity, message)` → `_write_one(app, entity, message)` 寫明是替換，`_maybe_snapshot` 是多收 `app`；(c) 3a 的檔案清單加 `registry_rebuild.py`（`RebuildApi.post_comment -> dict[str, Any]`，只改註記）；(d) W-8 失敗而寫現頁時兩行警告是有意的，寫進 C-20 並在 3c 加「恰好兩行、依序」的測試。
- **重讀 §6.4 補的同類缺口**（審查「核對 2」列的會猜處）：`FakeBackend._current_size` 的語意（覆寫值優先，`page_size_at` 也經它）；`Angareion` 存公開屬性 `self.writer`；C-20 的 store 夾具寫在 `test_angareion_pr3_cli.py`；3a 的 GitHub 假 transport 在 3a 的測試檔自己寫；3a 的測試不 import `FakeBackend`；§6.4 開頭「3a 只碰」補 `registry_rebuild.py`；`channel rollover` 第 4 步的 exit 2 列表補 S-4′ 的說明。

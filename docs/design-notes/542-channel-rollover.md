# #542 Angareion 頻道換頁（rollover）：留言上限、403 分辨、告警

- 單：#542（MAS，層級 c；Angareion 讀寫路徑，架構師必經）。
- 作者：專案架構師（作者模式，myaistorage-arch-w1，Claude Opus 5.5），2026-11-25。基準：main `512f58e`。
- 修訂：
  - 第 2 版（2026-11-25），依非作者審查（Sonnet，H-1、M-1～M-6、L-1～L-9、A-1～A-6）修正。改動對照在 §12。
  - 第 3 版（2026-11-25），依重審（Sonnet，H-1、M-1～M-5、L-1～L-6、A-7～A-9）修正，並同步 PR-1 實作已定的 7 點（不改 PR-1 契約的意義）。改動對照在 §13。
  - 第 4 版（2026-11-25），依第 2 次重審（Sonnet，M-1～M-3、L-1～L-3、nit-1～3、A-10、A-11）修正；游標改成每頁一份（A-10）。PR-1 的範圍與 C-1～C-5 不動。改動對照在 §14。
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
9. **切成 3 個 PR**：PR-1 → PR-2 → PR-3。每一步合上後 main 都不會更糟：
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
| W-4 | A：寫 R | `ChannelFullError` | 到 B |
| W-5 | A：寫 R | 其他錯誤（含 `RateLimitError`、網路） | 往外丟（exit 2）。POST 的結果不確定，換頁再寫可能重複 |
| W-6 | B：`page_size(P)` | n < 2400 | 寫 P |
| W-7 | B：`page_size(P)` | 失敗 | 寫 P（fail-open） |
| W-8 | B：`page_size(P)` | 2400 ≤ n < 2500 | `ensure_successor`：成功 → E＝重算實體，回到 A；失敗 → 寫 P，stderr 一行警告（P 還沒滿） |
| W-9 | B：`page_size(P)` | n ≥ 2500 | `ensure_successor`：成功 → E＝重算實體，回到 A；S-2 不相容 → 先 `refresh(C)`（強制 fetch 本機設定檔、取新的 E_local），再呼叫一次 `ensure_successor`；仍失敗（或其他失敗）→ exit 2 |
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
| S-5 | `open_page` | 失敗 | 失敗 |
| S-6 | `verify_channel(N)` | 回 `False`（404，或標題不是 C） | 失敗；N 留在 backend，沒有註冊。N 來自待註冊紀錄時，同時刪掉那筆紀錄（N 確定不能用，下次重開） |
| S-6′ | `verify_channel(N)` | 丟 `BackendError`（網路、5xx、rate limit 等暫時性錯誤） | 失敗；**待註冊紀錄保留**，下次再驗同一頁（重審 L-2） |
| S-7 | `post_with_retries(rollover_comment(C, backend, N, P))` | 失敗 | 失敗（「opened a new page but could not register it」）；N 上 0 則訊息；待註冊紀錄保留，下一次 S-4 重試註冊同一頁 |
| S-8 | 貼成功 → **事後重算**：再讀一次註冊簿，把自己貼出的那則（`post_with_retries` 回傳的留言：`id`、`created_at`、`author_association`）併進去（同 id 去重） | 讀成功 | 刪掉待註冊紀錄；回傳重算實體：N 是現頁就是 N；N 成了旁頁就是贏家的實體，N 保持空的 |
| S-9 | 事後重算 | 讀失敗 | 刪掉待註冊紀錄；用 S-1 的留言加上自己那則重算，回傳（N 是現頁） |

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
  - 退役頁 r（含旁頁）在 `cursor.at is None` 或 `cursor.at < r.until + RETIRED_READ_GRACE` 時才讀（`cursor` 是主游標）；
  - 順序：退役頁（舊的在前）再現頁。
  - `--since`（只有 `at` 的游標）用同一條規則，所以 `--since` 早於換頁時會讀到舊頁。
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
  - 新游標＝`dataclasses.replace(advance_cursor(prev, 各頁 entries 的聯集), pages=pages_after_read(...))`。沒有新 entries 也要換上新的 `pages`（`advance_cursor` 在 entries 空時回傳 `previous`，帶的是舊的 `pages`）。
  - 一次 `read` 裡任何一頁往外丟錯（現頁的任何錯誤、退役頁的 rate limit／網路／5xx），整次 `read` 失敗、游標不存，`pages` 也不變（照現行「讀失敗不動游標」）。
  - 單頁頻道：`pages` 恆為 `{現頁: 與主游標相同的值}`，每一次讀的請求與回傳和 PR-2 之前相同（第一次讀時 `pages` 是 `None`，起點就是主游標；之後起點是 `pages[現頁]`，兩者一起前進、值相同）。
- **序列化**（裁定 A-11，§8.4；重審 M-2、nit-3）：
  - `pages` 是 `None` → 不寫 `pages` 鍵（`test_angareion_state.py` 那些只存 `Cursor()` 的測試照舊）；
  - 否則寫 `"pages": {"<issue>": {"position": …, "at": …, "seen": [...]}}`：鍵是十進位字串、依數值遞增；值與主游標同一種三欄格式（`None` 照主游標現行寫成 `null`）。
  - 讀檔的壞資料一律「當作沒有」：`pages` 不是物件 → `None`；某一筆的鍵不是正整數字串、或值不是物件 → 丟掉那一筆（那一頁從頭讀）；某一筆的 `seen` 照主游標現行的防禦（非字串項丟掉、不是 list 就空）。不報錯、不警告。
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

- 問題：`check` 不存游標，所以不更新 `pages`。從頭讀的頁（§2.1：起點是空游標的頁，包括沒讀過的頁與**新游標**的每一頁）每次都從頭掃；現行的 `_scan_since` 只在找到符合 scope 的留言時才設，頁上全是給別人的訊息時就一直沒有掃描窗。一張 2000 則的頁，每 15 秒 20 個請求，直到有人跑 `read`。新游標（從沒 `read` 過的身分做 `wait`）會對每一張退役頁這樣掃（`cursor.at is None` 時全部退役頁都在 `to_read`），請求數乘上頁數。
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
| **PR-2** | `retired` 的解析；`registry_rules.py`（純規則）；`rollover` 動作的解析與重建；回報去重；`pages_to_read`；游標 `pages` 與 `pages_after_read`；GitHub／Fake 的跨頁 `read`／`check`（含從頭讀模式的掃描窗）；退役頁失敗略過；`LogCache` v3；Fake 依頁存留言；`--capabilities`；`channel status` 的頁旗標；`channel delete` 的過期檢查 | 沒有 `retired` 時輸出不變（既有測試不改預期）；重建對既有註冊簿逐位元組相同 | C-6～C-15、C-22～C-26 |
| **PR-3** | `rollover.PageWriter`／`ensure_successor`；`open_page`；待註冊紀錄（`state.PendingPageStore`）；`post_with_retries` 回傳留言；`send`、`decision` 的寫入改走它；`channel rollover NAME` | 自動換頁上線；merge 前提見 §6.2 | C-16～C-21 |

### 6.1 PR-1 可以先實作（不等 PR-2／PR-3）

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
  - 現頁一定在最後；退役頁只在 `cursor.at is None` 或 `cursor.at < until + 48h` 時列入，舊的在前；`--since` 游標用同一規則。
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
  - `read` 與 `log` 在 stderr 印一行 `Warning: an older page of channel '<C>' could not be read (HTTP <碼>); skipped`（不含編號），`check` 不印；
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
  - `state.py` 存讀 `pages`（§2.1 的序列化）：`pages` 是 `None` → 檔案沒有 `pages` 鍵；有值 → 讀回相等（`Cursor` 的 `==` 含 `pages`）；舊檔沒有 `pages` → `None`；壞資料（`pages` 不是物件、鍵不是正整數字串、值不是物件、`seen` 型別不對）各一個案例，結果照 §2.1「當作沒有」，不丟例外（nit-3）。
  - 改壞驗證：
    - `page_base` 對不在 `pages` 的頁回主游標（改回比 position）→ 第一個情境失敗（漏）；
    - `page_base` 對在 `pages` 的頁也回主游標（等於第 3 版拿掉 `held`）→ C-22「讀過的頁」失敗（漏 r1、r2）；
    - `pages_after_read` 對被略過的頁不寫入結果（等於第 2 版「只留讀成功的頁」）→ C-22「讀過的頁」失敗（重送）；
    - `pages_after_read` 對被略過、沒讀過的頁存主游標 → C-22「沒讀過的頁」失敗（漏）；
    - `prev.pages=None` 時被略過存 `Cursor()` → C-22「舊游標」失敗（重送）。
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
  - W-9 的 S-2 不相容 → 呼叫 `refresh` 恰好 1 次再試；refresh 後相容 → 寫入成功；仍不相容 → exit 2（L-5）；W-8 的不相容不呼叫 `refresh`；
  - 24h 用 `page_size_at` 的 `Date`（Fake 用注入的時鐘）；
  - W-8 成功、P 在窗內且 <2490 → 寫 P；
  - I-8（本機設定檔過期，P 早已換頁）→ 寫新頁，`open_page` 0 次。
- **C-17** `ensure_successor(C, P, E_local)`：§1.3 的 S-1～S-9（含 S-6′）每一列至少一個測試，另外：
  - S-2 的四種不相容各一個，都是 `open_page` 0 次；
  - S-7 之後新頁上 0 則訊息；
  - **孤兒頁上界**（M-2）：S-7 失敗（註冊簿寫不進）之後，同一個身分再 `send` 3 次（P 在 2400～2499，都走 W-8；註冊簿仍寫不進）→ `open_page` 總共 1 次，`post_comment` 每次都對同一頁重試；註冊簿恢復後的下一次 → `open_page` 仍是 1 次，貼出的 `rollover` 的 `issue` 是那一頁，待註冊紀錄被刪掉。
  - 待註冊紀錄的其他列：S-6 對紀錄裡的頁回 `False` → 紀錄刪掉，下一次 `open_page` 1 次；S-6′（`verify_channel` 丟 `BackendError`）→ 紀錄保留，下一次 `open_page` 0 次、`verify_channel` 再驗同一頁；S-3 → 紀錄刪掉；紀錄的 `from` 與 P 不同 → 當作沒有，`open_page` 1 次並覆寫；紀錄檔壞掉 → 當作沒有，不報錯。
  - `post_with_retries` 回傳最後一次成功的留言物件；S-8 用它的 `id`／`created_at`（L-3）；
  - S-8 的「N 是旁頁」→ 回傳贏家實體，N 上 0 則；
  - S-8 的列表裡沒有自己那則 → 結果與有時相同；
  - 失敗訊息不含編號。
- **C-18** 競態（兩個寫者以 gate 交錯：兩邊都過了 S-1「沒有下一頁」之後才放行貼留言）：
  - 註冊簿出現兩則 `rollover from P`，重建出一個現頁加一個 `side`，旁頁上 0 則；
  - 兩人寫的訊息，PR-2 的讀者各讀到恰好一次；
  - 另一組：B 在 A 的 `rollover` 貼出之後才開始 → B 的 `open_page` 呼叫 0 次；
  - 另一組（I-2）：`FakeRegistryClient` 在 B 的 S-8 隱藏 A 那則 → B 寫進旁頁；讀者先拿到只有現頁的設定檔並前進，再拿到含旁頁的設定檔 → B 的訊息恰好送達一次。
- **C-19** `channel rollover NAME`：
  - 先強制 `store.fetch()`（M-4），再不看則數、強制 `ensure_successor(C, 現頁)`；已有待落地的下一頁（S-3）→ 不開新頁；S-2 不相容 → exit 2，不開頁。
  - stdout 只印 NAME，exit 0；之後照 `channel create` 輪詢落地（上限 2 分鐘，逾時只在 stderr 提示）。
  - 未註冊的名字 → exit 1；S-1／S-2／S-5～S-7 → exit 2。
- **C-20** `cli send` 與 `decision_cli` 的每一個寫入都經 `PageWriter`：
  - 測試：Fake 現頁設成 2500，`send` 與 `decision ask` 都寫到下一頁；`ChannelBackend.send` 仍是單頁的原語。
  - 警告（C-4 在 PR-3 後的語意）：實際寫入的那一頁，若是寫入所用實體的**現頁**，而且寫入前 ≥2000，就印 C-4 那一行；寫退役頁（W-1）時不印。
- **C-21** PR-1～PR-3 的所有新訊息（stdout、stderr、例外字串）都不含 issue 編號與 repo 名。掃描式測試：跑過上面各情境，對輸出做 `#\d+` 與 repo 名的比對。

## 8. 結構（模組、介面、依賴方向）

### 8.1 模組切割

| 模組 | 新增或改動 | 不做什麼 |
|---|---|---|
| `backend.py` | `RetiredPage(issue, until, side)`、`Entity.retired`（預設 `()`，frozen 不變）、`Cursor.pages`（頁號→那一頁的 `Cursor`）、頁的常數、`pages_to_read`、`page_base`、`pages_after_read`、`page_entity`、例外 `ChannelFullError`／`RateLimitError`、協定 `PageBackend`（`page_size`、`page_size_at`，`runtime_checkable`）與 `PageOpener`（`open_page`）、Fake 的頁大小覆寫表（PR-1）與依頁存留言（PR-2） | 不知道註冊簿 |
| `github.py` | §4 的分辨；`page_size`；`open_page`；`read`／`check` 逐頁合併、沒讀過的頁從頭讀、退役頁失敗略過 | 不選寫哪一頁 |
| `state.py` | 存讀 `Cursor.pages`（§2.1 的序列化與壞資料處理）；`PendingPageStore`（待註冊紀錄，PR-3） | 不解讀 `pages`；不決定何時用待註冊紀錄 |
| `channel_config.py` | 解析 `retired` | 不決定讀哪幾頁 |
| `registry.py` | `rollover` 的動作與 `from`、`rollover_comment`、協定 `RegistryCounter` 與 `comment_count`（GET issue；Fake 也有）、`post_comment` 與 `post_with_retries` 回傳留言 | 仍只用標準庫 |
| `registry_rules.py`（新） | `rebuild_channels`、`render_channels`（從 `registry_rebuild` 搬來，加上 `rollover`） | 純函式，不做 I/O；只用標準庫與 `registry` |
| `registry_rebuild.py` | workflow 殼：`Rebuilder`（含回報去重）、`main`、環境變數 | 不放規則 |
| `rollover.py`（新） | `PageWriter`、`ensure_successor`、協定 `PendingPages`；`refresh` 與 `PendingPages` 由呼叫方注入 | 不讀游標、不列訊息；不 import `channel_config`、`state` |
| `client.py` | PR-1 不動；PR-3 起 `Angareion.send` 經 `PageWriter`；讀的部分不變（跨頁在 backend 裡） | — |
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
- ＋`rollover → {backend, registry, registry_rules}`
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

## 10. 測試策略

- 全部在 `tests/`，用 `uv run pytest`。模組第一行 `import _guard`。
  - 不連真 GitHub：GitHub 的分支用 `transport` 假物件，可以設定 403 body 與標頭，並記錄請求 URL；註冊簿用 `FakeRegistryClient`，加 `before_post` 鉤子，以及「列表時隱藏某幾則」的設定（C-18 的 I-2）。
  - 時間用注入的時鐘與固定的 `Date` 標頭。
- **層級**：
  - 純函式（`pages_to_read`、`rebuild_channels`、`parse_comment`、§4 的分辨）用單元測試；
  - 跨頁讀寫用 Fake 與 GitHub 假 transport 各跑一組同樣的情境（C-10、C-11、C-12、C-22、C-23）；
  - CLI 用 `main(..., backend=, store=, registry=, transport=)` 的既有接縫。
- **競態**（C-18）：兩個執行緒，用 `threading.Event` 當 gate。A 與 B 都在「重算完、要貼留言」前停住，兩邊都到了才一起放行；不用 sleep。要跨行程時，改用暫存目錄裡的 gate 檔：子行程輪詢檔案存在，以短間隔檢查並設總逾時，不靠睡固定秒數猜時序。
- **golden**：用一份與現行 channels.json 同形狀的合成註冊簿夾具，比對 PR-2 前後的重建輸出，要求逐位元組相同（C-8）。
- **改壞驗證**（每個 PR 至少下列幾種，要能讓對應測試失敗）：
  - PR-1：C-1 拿掉 `retry-after` 判斷；C-1 訊息拿掉 `not retrying`；C-2 不做 GET 探測；C-2 把 2400 門檻拿掉；
  - PR-2：C-9 寬限比較改成 `<=`，或拿掉 `cursor.at is None`；C-10 只對現頁 `advance_cursor`；C-11 ETag key 不含頁；C-14 不讀退役頁；C-22／C-23 的五種 `page_base`／`pages_after_read` 改壞（列在 C-23）；C-11 拿掉從頭讀模式的 `_scan_since`、從頭讀模式不認新游標；C-10 每頁各取一個 `floor`；C-25 拿掉去重；C-8 旁頁改成取代現頁；
  - PR-3：C-16 拿掉「先寫舊頁」；C-16 拿掉 W-9 的 `refresh`；C-17 先寫新頁再貼註冊；C-17 拿掉 S-8；C-17 拿掉 S-2 的相容檢查；C-17 拿掉待註冊紀錄（`open_page` 次數要變多）；C-17 S-6′ 也刪紀錄（`open_page` 次數要變多）。
- **沒驗到、也不在測試裡驗的**（真 GitHub）：
  1. 達上限時的實際狀態碼與 body（issue 本文只記錄到 403 和我們自己的訊息）；
  2. 上限是否恰好是 `comments ≥ 2500`，有沒有把時間軸事件算進去；
  3. GET issue 的 `comments` 會不會延遲；
  4. comment id **跨 issue** 是否全站遞增。這是這份設計新增的依賴；D4 只依賴同一實體內遞增。審查者對 MyAiStorage 的 71 則留言抽樣，結果單調，但那不是 AiCompany-channel，也不是證明。第 4 版起每一頁用自己的游標讀，送達（不漏、不重送）不依賴它；若不成立，只有同一次讀取回傳的訊息跨頁的相對順序可能與到達順序不同，主游標的 `position`（跨頁最大 id）只用在 `pages` 是 `None` 的起點；
  5. 交互參照會不會吃留言額度；
  6. 鎖住的 issue 是否擋得住擁有者留言（文件說擋不住）；
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

其他風險：

- **§2.3 的三種漏**：設定檔過期的舊版寫者、退役頁整個寬限期讀不到、舊版讀者。前兩種有 `channel status` 的旗標或警告，第三種由盤點擋住；`log` 都查得到。
- **註冊簿的上限**：#47 也是 2500 則，現在 52 則；每次換頁多 1～2 則，回報去重後每種多 1 則。短期沒事，`channel status` 會顯示。真的要處理時，註冊簿換頁是另一張單。
- **舊版讀者**：channels.json 換頁後，舊版讀者只讀新頁，看不到舊頁剩下的未讀訊息。手動換頁（§6）與 PR-3 都要等盤點（§6.2）。
- **寫入多一個 GET**：用的是 core rate limit，不是 secondary；`send` 的量遠小於輪詢。
- **`page_size` 回應的 `Date` 當作「現在」**：沒有 `Date` 時，跳過 W-1，直接看現頁（保守，等於 Q-2 的 A）。
- **新頁從頭讀的成本**：一般是剛開的新頁，幾則；離線很久的讀者最多把一頁讀完（≤25 個請求，一次性）。那些訊息本來就是它沒讀過的。

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

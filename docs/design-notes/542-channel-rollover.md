# #542 Angareion 頻道換頁（rollover）：留言上限、403 分辨、告警

- 單：#542（MAS，層級 c；Angareion 讀寫路徑，架構師必經）。
- 作者：專案架構師（作者模式，myaistorage-arch-w1，Claude Opus 5.5），2026-11-25。基準：main `512f58e`。
- 範圍：只設計。動到的模組：`backend`、`github`、`channel_config`、`registry`、`registry_rebuild`、`client`、`log_cache`、`decision_cli`、`cli`，新增 `rollover`。主 spec（`openspec/specs/angareion/spec.md`、`angareion/channel-config/spec.md`）要補的地方列在 §9，由 PM 決定與撰寫。
- 實測：只讀。`gh api` 讀得到註冊簿 MyAiStorage#47（2026-11-25 有 52 則留言）。AiCompany-channel 是私有 repo，這台 worker 的 `gh` 讀不到（404），所以各頁的留言數照 issue 本文引用，沒有自己量。沒有對真 GitHub 寫入。
- 名詞：
  - **頁**：一個頻道名對應的一張 backend issue。
  - **現頁**：channels.json 的 `issue`，新訊息原則上寫這裡。
  - **退役頁**：`retired`，換頁前的舊頁。

## 0. 結論

1. **資料模型**：一個頻道名對應一串頁。channels.json 每一筆照舊有 `backend`、`issue`（＝現頁）。換過頁的頻道多一個 `retired`，內容是 `[{issue, until, side?}]`，舊的在前。
   - 舊讀者只讀 `backend`／`issue`，看不到 `retired`；格式 `version` 不升，沒換頁的頻道重建結果逐位元組不變（§1）。
2. **序化靠註冊簿留言的全序，不靠鎖**。GitHub 的 issue 沒有交易，channel token 也只有 Issues 權限，拿不到 ref 這類 compare-and-swap。
   - 換頁是註冊簿上的一則 `rollover` 動作，帶 `from`（從哪一頁換出）。
   - 重建依 `(created_at, id)` 依序套用：
     - 第一則 `from`＝現頁的換頁成立；
     - 同時開出的第二頁記成「旁頁」（`side: true`），一樣會被讀，不丟訊息，只是多一張空 issue。
   - 寫入端開新頁前，先在本機用**同一個純函式** `rebuild_channels` 重算註冊簿，所以「已經有人換頁、只是還沒落地」不會再開一頁。剩下的競態窗是兩個寫者各自在讀註冊簿與留言之間交錯（秒級）。
3. **讀者游標不用改格式**。游標的 key 本來就是頻道名（`channel:`／`to:`／`mention:` 加頻道），position 是 GitHub 的 comment id；comment id 全站遞增（推論，D4 已經依賴它）。
   - 所以一次讀取把「現頁＋還在寬限期內的退役頁」的列表合在一起，當成同一個實體處理：依 comment id 排序，`advance_cursor` 吃合併後的 entries。
   - 跨頁的接續、去重和既有單頁的語意相同（§2）。
4. **寫入端「舊頁先寫滿」**（Q-2，建議）：換頁發生在 2400 則。之後的 24 小時內，新寫者仍寫舊頁，直到 2490 則。
   - 好處：換頁剛落地時，config 還沒拉新的讀者也收得到，不會出現「被 poke 叫醒、讀到空的、又閒下來」。
   - 落地之後，新讀者兩頁都讀。
5. **403 分辨**：
   - 先看 rate limit 的標頭與訊息（primary：`x-ratelimit-remaining: 0`；secondary：`retry-after`，或訊息含 `secondary rate limit`）。
   - 不是 rate limit 的 POST 留言 403／422，再 GET 一次 issue 看 `comments`：達 2500 就是 `ChannelFullError`；否則是一般錯誤，並附上 GitHub 回應的 `message`（遮蔽 token、截短）。
   - 分辨靠數字，不靠猜訊息文字。真正的上限回應長什麼樣沒驗到（§5）。
6. **告警**：不能用 workflow 量，因為 AiCompany-channel 是私有 repo，MyAiStorage 的 `GITHUB_TOKEN` 讀不到。改由 channel token 這一側量：
   - `send` 本來就要 GET 現頁的留言數（換頁要用）。順便在 ≥2000 時在 stderr 警告寫的人。
   - 新增唯讀的 `angareion channel status`，給 PMO 看；建議由 `pmo tick` 定期呼叫並列在 tick 輸出（另開 d，Q-4）。不新增常駐程序。
7. **緊急緩解**：PR-1（403 分辨＋計數＋`channel status`）小而獨立。
   - PR-2（讀側跨頁＋重建認得 `rollover`）合上後，就可以**手動換頁**：PMO 在 AiCompany-channel 開新 issue，在註冊簿貼一行 `rollover` JSON。不必等 PR-3 的自動化。
   - 在那之前照現行做法用 `-b` 頂著。w3 已經有 `-b`；m1（約剩 260 則）、w4（約剩 550 則）請 PMO 用 PR-1 的 `channel status` 盯，必要時再開 `-b`（§6）。
8. **`-b` 頻道不併回**：訊息本文的 `channel` 不同、游標 key 不同，併成某頻道的頁會讓已讀過的再送一次。換頁上線後改回原名發訊，`-b` 留 7 天再 `channel delete`（Q-3）。
9. **切成 3 個 PR**：PR-1 → PR-2 → PR-3。每一步合上後 main 都不會更糟：
   - PR-2 在沒有 `retired` 時零行為變更；
   - PR-3 要等所有席位都跑 ≥PR-2 的版本才能 merge（Q-6）。
   - 契約 C-1～C-21。

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

- `issue`：現頁。舊讀者與舊寫者照舊只用它，換頁後就自動讀寫新頁；舊讀者看不到舊頁剩下的未讀訊息（§8 部署順序）。
- `retired`：退役頁，**依重建時套用的順序**排列。
  - `until`：讓它退役的那則註冊簿留言的 `created_at`，是後端時間（D4：只有後端決定新舊）。
  - `side: true`：競態下多開出來、從沒當過現頁的旁頁。
- 沒有換過頁的頻道不寫 `retired` 鍵。`render_channels` 用 `sort_keys`，所以既有頻道的輸出逐位元組不變。
- `ChannelStore.channels()` 現在只讀 `backend`／`issue`、忽略其他鍵（`channel_config.ChannelStore.channels`），所以舊版可以讀新檔。新版對 `retired` 嚴格：形狀不對＝`ChannelConfigError`（exit 2），與這個檔的其他欄位同一個規則。這個檔是機器產物。

### 1.2 註冊簿動作 `rollover`

```text
{"schema": 1, "action": "rollover", "channel": "team-x-w2", "backend": "aicompany-channel", "issue": 41, "from": 28}
```

- `schema` 不升：這是新增動作，不是改既有動作。`from` 只有 `rollover` 必填，`register`／`unregister` 帶 `from` 仍是「unknown fields」。
- 舊版重建碰到它會回報「unknown action」並略過。但重建只在 workflow 跑，workflow 跑的是 main 的程式；PR-2 把解析與套用一起帶進 main，客戶端要等 PR-3 才會寫出這種留言，所以不會有「舊重建看到新動作」的時窗。
- 套用規則（`rebuild_channels`，依 `(created_at, id)`）：

| 情況 | 結果 |
|---|---|
| 頻道不存在 | 回報，略過 |
| `backend` 與現頁不同 | 回報，略過 |
| `issue` 已是現頁或已在 `retired` | 重複，no-op（不回報） |
| `from`＝現頁 | `retired` 加 `{from, until}`；現頁＝`issue` |
| `from` 在 `retired` 裡（現頁已被別人換掉） | `retired` 加 `{issue, until, side: true}`；現頁不變 |
| 其他 `from` | 回報，略過 |

- `register` 判斷「同一個實體」只比 `backend`＋`issue`（現頁）。`unregister` 指向現頁時整筆移除（含 `retired`）；指向退役頁是 no-op，並回報。
- `channel delete` 只關現頁（照舊）。退役頁本來就不再寫入，不另外關（Q-5）。

### 1.3 寫入端何時換頁、怎麼避免開兩頁

常數（放 `backend.py`，與 `CURSOR_SEEN_MARGIN_SECONDS` 同一處；值由 PM 定，Q-7）：

| 名稱 | 值 | 用途 |
|---|---|---|
| `PAGE_COMMENT_LIMIT` | 2500 | GitHub 上限（觀測值，issue 本文） |
| `PAGE_WARN_AT` | 2000 | 告警 |
| `PAGE_ROLLOVER_AT` | 2400 | 寫者確保有下一頁 |
| `PAGE_FILL_LIMIT` | 2490 | 退役頁還可以寫到這裡（留 10 則給競態） |
| `PAGE_FILL_WINDOW` | 24 h | 退役頁在 `until` 之後還可以寫多久 |
| `RETIRED_READ_GRACE` | 48 h | 讀者讀退役頁的寬限，必須大於 `PAGE_FILL_WINDOW` |

寫一則訊息（`rollover.PageWriter.write(entity, message)`；`send` 與 `decision` 的寫入都走它）：

1. **先寫舊頁**：R＝`retired` 裡最後一個非旁頁。若 R 的 `until` 距今在 `PAGE_FILL_WINDOW` 內，而且 `page_size(R) < PAGE_FILL_LIMIT`，就寫 R。
   - 「距今」用 page_size 回應的 `Date` 標頭（後端時間），不是本機時鐘。
2. **否則看現頁** P，n＝`page_size(P)`：
   - n ≥ `PAGE_ROLLOVER_AT`：先 `ensure_successor(C, P)`。
   - n < `PAGE_COMMENT_LIMIT`：寫 P。
     - 回 `ChannelFullError` → 第 3 步。
     - 確保有下一頁之後，P 還沒滿就照樣寫 P；這是「舊頁先寫滿」在 config 尚未落地時的版本。
   - P 滿了：寫 `ensure_successor` 給的頁 S。對 S 再做一次第 2 步，最多 3 跳，超過就是錯誤。
3. `page_size` 失敗（網路等）：不擋寫入，直接寫 P（fail-open），403 照 §4 分辨。

`ensure_successor(C, P)`：

1. 讀註冊簿整串（`RegistryClient.all_comments`，現在 52 則、1 頁），加上 `backends.json` 的 backend 名，跑 `registry_rebuild.rebuild_channels`。這就是 workflow 下一次會寫出的 channels.json。
2. 重算結果裡 C 的現頁 ≠ P，而且 P 在它的 `retired` 裡 → 已經有人換過頁，回傳那個現頁，不開新頁。
3. 否則：
   - `open_page`：開 issue，標題＝C（`verify_channel` 照舊成立），本文寫「continued from #P」。交互參照會在 P 的時間軸出現；推論它不算留言數，沒驗到。
   - `verify_channel`。
   - `post_with_retries(rollover_comment(C, backend, N, P))`。
   - 回傳 N。
4. **註冊在第一次寫入之前**：新頁在 `rollover` 留言貼成功之前，一則訊息都不寫。
   - 貼失敗時：P 還有空間就寫 P，並在 stderr 警告；P 滿了就 exit 2，訊息說「開了新頁但沒能註冊」。
   - 沒註冊的新頁留在 backend，訊息裡不印編號（spec：介面不出現 issue 概念），PMO 用標題找。

競態：

- 兩個寫者 A、B 同時在第 1 步讀到「沒有下一頁」，各開 N_A、N_B 並各貼 `rollover from P`。重建後先到者是現頁，後到者是旁頁。
- 兩頁都在 `retired`／`issue` 裡，讀者都讀（§2），所以不丟訊息。
- 依第 2 步，P 沒滿時兩人都還是寫 P，旁頁通常是空的；只有 P 已滿時，各自寫自己的新頁。
- 能縮小競態窗的就是第 1 步的重算。窗的大小＝兩邊「讀註冊簿 → 貼留言」交錯的時間（秒級），外加註冊簿列表的複寫延遲（推論）。
- 不在舊頁貼「搶頁」標記：那要再讀一次舊頁、還有複寫延遲的問題，窗只會縮小、不會消失，又多吃舊頁的額度。

### 1.4 舊頁封存

建議**不鎖、不關、不加 label**（Q-5）：

- 鎖：channel token 是擁有者的，GitHub 允許有 push 權限的人在鎖住的 issue 留言，所以擋不住我們自己的寫者，只會誤導人。
- 關：`channel delete` 用「關閉」表示刪除，關退役頁會讓人誤會頻道被刪了。
- 封存靠兩件事：channels.json 的 `retired`，以及新頁本文的交互參照。

## 2. 讀者：跨頁接續

### 2.1 游標

- `state.py` 的游標以「目標 key」為準：`channel:<C>`、`to:<我>:<C>`、`mention:<我>:<C>`。值是 `{position, at, seen}`，position＝comment id。
  - key 裡沒有 issue 編號，所以**換頁不必搬游標**。
  - comment id 全站遞增（推論，未文件化；D4 的 `comment_is_new` 本來就靠它），新頁的留言 id 都大於換頁前的 position。
- 要讀哪幾頁：`backend.pages_to_read(entity, cursor) -> list[int]`，純函式：
  - 現頁一定讀；
  - 退役頁 r 在 `cursor.at is None` 或 `cursor.at < r.until + RETIRED_READ_GRACE` 時才讀；
  - 順序：退役頁（舊的在前）再現頁。
  - `--since`（只有 `at` 的游標）用同一條規則，所以 `--since` 早於換頁時會讀到舊頁。
- 為什麼寬限期用游標而不用本機時鐘：D4 只讓後端時間決定新舊。讀者的游標時間過了 `until+48h`，就表示它已經讀到換頁兩天之後的訊息；而退役頁的寫入都在 `until+24h` 之前（§1.3 第 1 步），早已被讀過。
- 沒有新訊息的冷頻道，游標停在換頁前，會一直多讀一頁。代價是每次讀多一個請求，直到下一則訊息出現；可以接受。

### 2.2 各介面

| 介面 | 跨頁行為 |
|---|---|
| `read`（`backend.read`） | 對 `pages_to_read` 的每一頁照現在的方式列表，把 entries 與符合的訊息合併。訊息依 comment id 排序（同一頻道內＝到達順序）；游標＝`advance_cursor(prev, 合併的 entries)`；`--since` 照舊不動游標；新身分的廣播下限取第一個回應的 `Date`（#224） |
| `wait`／`check` | 任一頁有新訊息就是 True。ETag 與 `scan_since` 的 key 改成每頁一份（`repo#issue|scope`，就是現在的 `_etag_key` 套在頁上），單頁的條件請求規則不變。退役頁在 `since=at-60s` 下通常是空的、不滿一頁，所以 ETag 可以存，之後是 304。`wait` 輪詢中仍不 fetch（spec） |
| 「給我」／「@ 我」跨頻道合併 | `client.Angareion.read` 照舊依 `(messages.received_at_key(received_at), channel, id)` 排序（D-31）。同一頻道內跨頁依 comment id，兩者一致：同一頻道的時間與 id 同向，同秒內由 id 決勝，與單頁相同 |
| `log` | `LogCache` v3：同一個頻道一個檔，`pages: {"<issue>": {last_created_at, etag}}`，**所有頁**（現頁＋全部退役頁，不看寬限期），訊息以 comment id 合併。v2 檔整份重建（照 #468 升版的先例）；`repo` 變了也重建；不在 entity 裡的頁，它的訊息丟掉。`enumerate_conditional` 的介面不變，由 `LogCache` 逐頁呼叫（每頁一個「單頁 entity」） |
| `decision list` | 經 `LogCache.sync`，自動看到全部的頁 |
| `decision ask`／`update`／`submit`… | 寫入走 `PageWriter`（§1.3） |

### 2.3 去重與順序（為什麼不會重送、不會漏）

- 同一則留言只在一頁上，comment id 全域唯一，所以合併後不會重複。
- 跨頁的「晚到」（舊頁上 id 較小、較晚才看得到的留言）由 `at - 60s` 的視窗與 `seen` 接住，與單頁的複寫延遲同一套。前提是一次讀取同時列了兩頁，所以 entries 是合併的。
- 會漏的情況只剩：退役頁在讀者的游標過了 `until+48h` 之後才寫入。依 §1.3，新版寫者不會這樣寫。舊版寫者也只寫 channels.json 的 `issue`，換頁後就寫新頁。

## 3. `-b` 臨時頻道

- 現況：`team-mylinuxpool-w2-b`（#36）、`w3-b`（#37）已註冊。PMO 用它們發訊；收件不指定頻道，所以照收。
- **不併回成頁**：
  - 那些訊息本文的 `channel` 是 `…-b`；
  - 讀者在 `…-b` 的游標是另一個 key（`to:<我>:…-b`），併成 w2 的頁之後，w2 的游標停在 #28 的最後位置，#36 的每一則都會被當成新的再送一次。
  - 要避免就得改游標或重寫歷史，兩者都不做。
- **收斂**（Q-3，建議）：
  1. PR-2 合上、所有席位都升級之後，PMO 手動換頁（§6）或用 PR-3 的 `channel rollover` 把 w2、w3 換頁。
  2. 發訊改回原名（teams.yml 本來就是原名）。
  3. `-b` 再留 7 天，讓還沒讀的人讀完，之後 `channel delete`。`delete` 會關 issue 並註銷，`log` 之後就查不到 `-b` 的歷史；要保留歷史就不刪（每次讀「給我」多一個請求）。

## 4. 403 分辨

`github.GitHubIssuesBackend._request` 依序判斷：

1. 403 或 429，而且 `x-ratelimit-remaining: 0` → `RateLimitError`（primary），訊息附 `x-ratelimit-reset` 換成的 UTC 時刻。
2. 403 或 429，而且有 `retry-after`，或 body 的 `message` 含 `secondary rate limit`（不分大小寫）→ `RateLimitError`（secondary），有 `retry-after` 就附秒數。
3. 只在 POST `…/comments`：403 或 422，不是上面兩種 → 用同一個 token GET 一次 issue，看 `comments`：
   - ≥ `PAGE_COMMENT_LIMIT` → `ChannelFullError(channel)`；
   - 否則，或這個 GET 失敗 → 一般的 `BackendError`。
4. 其他 403：`BackendError`。訊息有 HTTP 狀態碼與 GitHub 的 `message`（一行、≤200 字、遮蔽 token）。不再寫「secondary rate limit?」這個猜測。

說明：

- 所有新例外都是 `BackendError` 的子類，exit 照舊是 2，照舊不自動重試（spec「後端錯誤」情境）。
- 訊息不出現 repo 與 issue 編號（spec「介面不出現 GitHub repo／issue 的概念」）。`ChannelFullError` 的訊息：`channel '<C>' is full: its current page reached the backend's message limit (2500)`；PR-3 之後補上 `run 'angareion channel rollover <C>'`。
- 為什麼不看訊息文字：上限的回應長什麼樣，我們手上沒有樣本。現行程式只印自己的猜測，不印 GitHub 的訊息。所以判斷用可以重查的數字（`comments`），並把 GitHub 的 `message` 帶進錯誤，下一次就有樣本。
- 不確定時的保守處置：沒判成「滿」就不自動換頁；PR-3 的自動換頁只看 `page_size ≥ 2400`，不看 403。

## 5. 告警

- **誰量**：channel token 這一側。
  - workflow 量不到：AiCompany-channel 是私有 repo，MyAiStorage 的 `GITHUB_TOKEN` 讀不到；要量得加一把 PAT secret，這不做。
  - `send` 本來就要 `page_size`（§1.3），**不多花請求**。PR-1 先加上這個 GET：每次 send 多 1 個請求；送出量遠小於輪詢量，可以接受。
  - `angareion channel status [NAME…] [--json]` 是唯讀的計數：每個頻道 1 個請求，不動游標。
- **怎麼告警**：
  - 寫的人：`send` 在 ≥2000 時在 stderr 印一行 `Warning`（stdout、exit 都不變）。
  - PMO：建議 `pmo tick` 每 6 小時（以 pmo 的 `due` 排）跑一次 `angareion channel status --json`，把 ≥2000 的頻道列在 tick 輸出（Q-4；這是 pmo 的改動，另開 d）。在那之前由 PMO 手動跑。
  - 不新增常駐程序，也不從 workflow 發訊息。
- 註冊簿 MyAiStorage#47 本身也有 2500 則的上限，現在 52 則；每次換頁加 1 則。`channel status` 一併列出註冊簿的留言數（`registry` 一列）。

## 6. 緊急緩解與 PR 切分

時程上的事實：

- w2 已滿，已經有 `-b`；w3 約剩 65 則，已經有 `-b`；
- m1 約剩 260 則；w4 約剩 550 則。

| PR | 內容 | 合上後 main | 契約 |
|---|---|---|---|
| **PR-1**（急） | 403 分辨（`RateLimitError`、`ChannelFullError`）；`page_size`（GitHub＋Fake）；`send` 的 ≥2000 警告；`channel status` | 只有錯誤訊息變清楚、多一個唯讀指令；寫入多 1 個 GET（失敗不擋） | C-1～C-5 |
| **PR-2** | `retired` 的解析（`ChannelRef`／`Entity`）；`rollover` 動作的解析與重建；`pages_to_read`；GitHub／Fake 的跨頁 `read`／`check`；`LogCache` v3；Fake 依頁存留言 | 沒有 `retired` 時零行為變更（既有測試不改預期）；重建對既有註冊簿逐位元組相同 | C-6～C-15 |
| **PR-3** | `rollover.PageWriter`／`ensure_successor`；`open_page`；`send`、`decision` 的寫入改走它；`channel rollover NAME` | 自動換頁上線；merge 前提：所有席位已跑 ≥PR-2（Q-6） | C-16～C-21 |

- **PR-2 之後就能手動換頁**（不等 PR-3）：
  1. PMO 用本人的 gh 在 AiCompany-channel 開 issue，標題＝頻道名。
  2. 在註冊簿貼 `{"schema": 1, "action": "rollover", "channel": "<C>", "backend": "aicompany-channel", "issue": <新>, "from": <舊>}`，等 workflow 落地。
  3. 寫者（≥PR-2 或舊版）都寫新頁；≥PR-2 的讀者兩頁都讀。
  - 這一步要先確認讀者都升級了，否則舊讀者看不到舊頁剩下的未讀訊息。
- **PR-1 能不能只加一個手動換頁指令？** 不行。換頁的另一半是讀者要跟著讀兩頁，那就是 PR-2；只有 PR-1 時，換頁等同開一個 `-b`。
- **PR-1 之前與 PR-2 之前**：照 PMO 現行做法，頻道剩不到約 50 則時開 `-b`。m1、w4 由 PMO 用 `channel status` 或 PMO 的 gh 盯。

## 7. 契約（每條都有測試對應；實作報告逐條對到測試名稱）

### PR-1

- **C-1** `_request` 收到 403／429，而且 `x-ratelimit-remaining: "0"` → `RateLimitError`，`kind == "primary"`，訊息含 reset 時刻（UTC，RFC 3339）；有 `retry-after`，或 body 的 `message` 含 `secondary rate limit` → `RateLimitError`，`kind == "secondary"`，訊息含秒數（有的話）。兩者都是 `BackendError` 的子類，CLI 都 exit 2，請求只送一次。
- **C-2** POST 留言回 403／422，而且不符 C-1 → 接著 GET 一次 issue：
  - `comments ≥ 2500` → `ChannelFullError`，訊息含頻道名與 2500，不含 issue 編號與 repo；
  - 否則（或 GET 失敗）→ `BackendError`，訊息含 `HTTP 403`（或 422）與 GitHub `message` 的前 200 字（單行、已遮蔽 token），不含 `secondary rate limit?`。
- **C-3** `page_size(entity) -> int`：GitHub＝GET issue 的 `comments`，缺漏或不是整數 → `BackendError`。Fake＝該頁的留言數，另有測試輔助 `set_page_size(issue, n, repo=…)`。Fake 的 `send` 在該頁 ≥ `comment_limit`（預設 2500）時丟 `ChannelFullError`。新協定 `PageBackend`，`ChannelBackend` 不加方法。
- **C-4** `send`：
  - 寫入前 `page_size`：≥2000 → stderr 恰好一行 `Warning: channel '<C>' has <n> of 2500 messages on its current page`；<2000 → 沒有這一行。
  - `page_size` 失敗 → 沒有警告，照常寫入，stdout 與 exit 不變。
- **C-5** `channel status [NAME…] [--json]`：
  - 每個頻道一列，包含名稱、現頁則數、上限、等級（`ok` <2000、`warn`、`rollover` ≥2400、`full` ≥2500）、頁數；最後一列是 `registry`（註冊簿的留言數）。`--json` 是同樣欄位的陣列。
  - 未註冊的名字 → exit 1。某個頻道查詢失敗 → 那一列等級 `unknown`，stderr 說明，其他列照印，最後 exit 2。
  - 輸出不含 issue 編號，不動游標，不 fetch 以外的寫入。

### PR-2

- **C-6** channels.json 的 `retired`：
  - 合法：`[{issue: int>0, until: 帶時區的 RFC 3339, side?: true}]` → `Entity.retired: tuple[RetiredPage, ...]`，依檔案順序。
  - 沒有這個鍵 → `()`。
  - 形狀不合（不是 list、`issue` 不是正整數、`until` 讀不出、`side` 不是 `true`、與現頁重號）→ `ChannelConfigError`（exit 2）。
  - 既有的 channels.json 解析結果與 PR-2 之前相同。
- **C-7** `parse_comment`：`rollover` 必須恰好有 `schema/action/channel/backend/issue/from` 六鍵，`from` 是正整數、且不等於 `issue`；`register`／`unregister` 帶 `from` → `InvalidAction`（unknown fields）。`rollover_comment(channel, backend, issue, from_issue)` 產生一行 JSON，`parse_comment` 讀回得到相同的值。
- **C-8** `rebuild_channels` 套用 `rollover` 時照 §1.2 的表：每一列一個測試，含兩則 `from` 相同的競態，結果是一個現頁加一個 `side`。
  - `register` 判斷同一實體只比 `backend`／`issue`；`unregister` 現頁 → 整筆移除；`unregister` 退役頁 → no-op 並回報。
  - `render_channels`：沒有退役頁的頻道沒有 `retired` 鍵；現有註冊簿的夾具重建結果與 PR-2 之前**逐位元組相同**（golden）。
- **C-9** `pages_to_read(entity, cursor)`：現頁一定在最後；退役頁只在 `cursor.at is None` 或 `cursor.at < until + 48h` 時列入，舊的在前；`--since` 游標用同一規則。純函式，不碰網路。
- **C-10** `read`（GitHub、Fake）：
  - 回傳的訊息是 `pages_to_read` 各頁中符合 scope 的新訊息的聯集，依 comment id 遞增。
  - 游標＝`advance_cursor(prev, 各頁 entries 的聯集)`；`--since` 不動游標；新身分廣播的下限取第一個列表回應的 `Date`。
  - 只有一頁時，請求與結果和 PR-2 之前相同（既有測試不改）。
- **C-11** `check`：任一頁有新訊息 → True。ETag 紀錄與 `scan_since` 以頁為 key（`repo#issue|scope`）；某頁的 304 不替另一頁回答；單頁時與現行相同。
- **C-12** 接續（情境測試，Fake 與 GitHub 假 transport 各一）：
  - 讀者讀到舊頁第 k 則 → 換頁 → 舊頁再寫 2 則、新頁寫 3 則 → 下一次 `read` 恰好得到這 5 則，依到達順序；再讀一次得到 0 則。
  - 換頁前已讀過的不再出現。
  - 讀者游標超過 `until+48h` 之後，就不再對舊頁發請求（GitHub 假 transport 記錄 URL）。
- **C-13** 「給我」跨頻道合併的排序鍵不變（`received_at_key, channel, id`）；同一頻道跨頁的訊息相對順序＝comment id 順序。
- **C-14** `LogCache`：
  - v3 對**所有頁**逐頁 `enumerate_conditional`（單頁 entity），各頁有自己的 `last_created_at` 與 etag，訊息以 comment id 合併，依 id 排序。
  - v2 檔 → 整份重建；`repo` 不同 → 重建；entity 已沒有的頁 → 它的訊息移除。
  - `log --channel C` 與 `decision list` 看得到舊頁與新頁的訊息。
- **C-15** Fake 依 `(repo, issue)` 存留言：`deliver(entity, …)` 存在 `entity.issue`（可用 `page=` 指定別頁）；`read`／`check`／`enumerate_conditional`／`page_size` 只看所指的頁。既有測試的預期不變。

### PR-3

- **C-16** `PageWriter.write(entity, message) -> str` 照 §1.3 選頁（每個分支一個測試）：
  - 最後一個非旁頁的退役頁在 24h 內且 <2490 → 寫它；
  - 現頁 <2400 → 寫現頁，不讀註冊簿；
  - 現頁 2400～2499 → `ensure_successor` 之後仍寫現頁；
  - 現頁滿（≥2500 或 `ChannelFullError`）→ 寫下一頁；
  - 連 3 跳都滿 → `BackendError`；
  - `page_size` 失敗 → 寫現頁。
  - 24h 用 `page_size` 回應的 `Date`（Fake 用注入的時鐘）。
- **C-17** `ensure_successor(C, P)`：
  - 本機重算的現頁 ≠ P 而且 P 在它的退役頁 → 回傳它，`open_page` 呼叫 0 次。
  - 否則依序 `open_page`（標題＝C、本文含 `#P`）→ `verify_channel` → 貼 `rollover_comment(C, backend, N, P)`，回傳 N。
  - 貼留言失敗 → 新頁上 0 則訊息；P 還沒滿 → 寫 P 並在 stderr 警告；P 滿 → `BackendError`（exit 2），訊息不含編號。
- **C-18** 競態（兩個寫者以 gate 交錯：兩邊都過了「重算＝沒有下一頁」之後才放行貼留言）：
  - 註冊簿出現兩則 `rollover from P`，重建出一個現頁加一個 `side`；
  - 兩人寫的訊息，PR-2 的讀者各讀到恰好一次。
  - 另一組：B 在 A 的 `rollover` 貼出之後才開始 → B 的 `open_page` 呼叫 0 次。
- **C-19** `channel rollover NAME`：
  - 不看則數，強制 `ensure_successor(C, 現頁)`；已有待落地的下一頁 → 不開新頁。
  - stdout 只印 NAME，exit 0；之後照 `channel create` 輪詢落地（上限 2 分鐘，逾時只在 stderr 提示）。
  - 未註冊的名字 → exit 1。
- **C-20** `cli send` 與 `decision_cli` 的每一個寫入都經 `PageWriter`（測試：Fake 現頁設成 2500，`send` 與 `decision ask` 都寫到下一頁）；`ChannelBackend.send` 仍是單頁的原語。
- **C-21** PR-1～PR-3 的所有新訊息（stdout、stderr、例外字串）都不含 issue 編號與 repo 名（掃描式測試：跑過上面各情境，對輸出做 `#\d+` 與 repo 名的比對）。

## 8. 結構（模組、介面、依賴方向）

### 8.1 模組切割

| 模組 | 新增或改動 | 不做什麼 |
|---|---|---|
| `backend.py` | `RetiredPage(issue, until, side)`、`Entity.retired`（預設 `()`，frozen 不變）、頁的常數、`pages_to_read`、`page_entity(entity, issue)`、例外 `ChannelFullError`／`RateLimitError`、協定 `PageBackend`（`page_size`、`open_page`）、Fake 依頁存留言 | 不知道註冊簿 |
| `github.py` | §4 的分辨；`page_size`；`open_page`；`read`／`check` 逐頁合併 | 不選寫哪一頁 |
| `channel_config.py` | 解析 `retired` | 不決定讀哪幾頁 |
| `registry.py` | `rollover` 的動作與 `from`、`rollover_comment` | 仍只用標準庫 |
| `registry_rebuild.py` | 套用 `rollover`、`render` 寫 `retired` | 仍只用標準庫；不碰 backend |
| `rollover.py`（新） | `PageWriter`、`ensure_successor` | 不讀游標、不列訊息 |
| `client.py` | `Angareion.send` 經 `PageWriter`；讀的部分不變（跨頁在 backend 裡） | — |
| `log_cache.py` | v3 逐頁 | — |
| `decision_cli.py` | 寫入改經 app 的 `PageWriter` | — |
| `cli.py` | `channel status`、`channel rollover`、注入 | — |

### 8.2 依賴方向（新增的邊以 ＋ 標示；沒有環）

- `backend → messages`
- `github → {backend, messages, transport, config}`
- `channel_config → backend`
- `registry → transport`
- `registry_rebuild → registry`
- ＋`rollover → {backend, registry, registry_rebuild}`
- `client → {backend, messages, state}`；＋`client → rollover`
- `log_cache → {backend, messages}`
- `decision_cli → {decision, log_cache, backend, messages}`
- `cli → *`

說明：

- `rollover` 用 `registry_rebuild.rebuild_channels` 在本機重算。它是純函式、只用標準庫，寫入端與 workflow 用的是同一份規則，沒有第二份。
- `rollover` 不 import `channel_config`：backend 名由呼叫方（cli）從 `ChannelStore.backends()` 傳進來。

### 8.3 SOLID

- **S**：讀哪幾頁（`pages_to_read`，純）、寫哪一頁（`PageWriter`）、頁怎麼存（channels.json 與重建）是三個不同的模組。`github` 只負責「對一頁做 HTTP」再合併。
- **O**：`ChannelBackend` 不加方法。頁相關的寫入能力另立 `PageBackend`，同 #468 的 `EnumerableBackend` 先例；`LogCache` 逐頁呼叫既有的 `enumerate_conditional`。
- **L**：Fake 與 GitHub 都實作 `PageBackend`，同一組契約測試（C-3、C-10、C-11、C-15）兩邊都跑。
- **I**：讀側只要 `ChannelBackend`；只有寫入路徑與 `channel status` 要 `PageBackend`。
- **D**：`PageWriter` 依賴協定（`PageBackend`、`CommentPoster` 加上 `all_comments`），不依賴具體的 GitHub client。
- **符合但要說明的一處**：`Entity` 多了 `retired`，所有讀者都會拿到它。這是資料，不是行為；`pages_to_read` 是唯一解讀它的地方。

### 8.4 被捨棄的方案

| 方案 | 捨棄理由 |
|---|---|
| 只由 workflow 開新頁（單一寫者） | workflow 的 `GITHUB_TOKEN` 不能寫私有的 AiCompany-channel，要加 PAT secret；`cancel-in-progress` 下開 issue 不是冪等 |
| 在舊頁貼「搶頁」標記、最小 id 的贏 | 複寫延遲下仍會誤判，多吃舊頁額度，多一輪讀取；全序已經由註冊簿給了 |
| `rollover` 帶 CAS，輸的人被拒 | 輸的人可能已經寫進自己的新頁，被拒等於訊息讀不到；改成旁頁，不丟訊息 |
| 讀者讀所有頁 | 頁數會一直累積，每次讀都要多 N 個請求；寬限期之後的舊頁不會再有新訊息 |
| 換頁後立刻改寫新頁（Q-2 的 A） | config 還沒拉新的讀者會在換頁後最多 900 秒（`wait` 甚至更久）看不到新訊息，aicomm 的 poke 叫醒會讀到空的；作為 Q-2 的選項保留 |
| 舊頁貼「換頁了」的廣播讓讀者 fetch | 要等落地才有意義；`send` 若等落地（最多 2 分鐘）會超過 pmo 呼叫 angareion 的 60 秒逾時（`pmo.adapters._TIMEOUT`），pmo 會當失敗而重送；不等又沒用 |
| 把 `-b` 併成頁 | 游標 key 與訊息本文的 `channel` 不同，會重送（§3） |
| 鎖住舊頁 | 擋不住擁有者的 token（§1.4） |

## 9. 主 spec 要補的地方（PM 撰寫；架構師只看介面能否一致表達）

- `angareion/spec.md`「六個介面」：`channel` 家族多 `status`（唯讀）與 `rollover`，要寫進允許的指令清單（Q-1）；`channel rollover` 的 stdout 只印頻道名。
- `angareion/spec.md`「輸出與結束碼」的「後端錯誤」情境：分成 rate limit 與「頻道這一頁滿了」，exit 照舊是 2。
- `angareion/channel-config/spec.md`「channel 設定檔」：每筆可以有 `retired`（§1.1）。「channel 對應的更新」：多 `rollover` 動作（§1.2 的表）。「讀取與 pull」不變（`wait` 輪詢中仍不 fetch）。
- 「目標與游標」：游標以頻道為單位、跨頁接續（§2）。

## 10. 測試策略

- 全部在 `tests/`，用 `uv run pytest`。模組第一行 `import _guard`。
  - 不連真 GitHub：GitHub 的分支用 `transport` 假物件，可以設定 403 body 與標頭，並記錄請求 URL；註冊簿用 `FakeRegistryClient`，加 `before_post` 鉤子。
  - 時間用注入的時鐘與固定的 `Date` 標頭。
- **層級**：
  - 純函式（`pages_to_read`、`rebuild_channels`、`parse_comment`、§4 的分辨）用單元測試；
  - 跨頁讀寫用 Fake 與 GitHub 假 transport 各跑一組同樣的情境（C-10、C-11、C-12）；
  - CLI 用 `main(..., backend=, store=, registry=, transport=)` 的既有接縫。
- **競態**（C-18）：兩個執行緒，用 `threading.Event` 當 gate。A 與 B 都在「重算完、要貼留言」前停住，兩邊都到了才一起放行；不用 sleep。要跨行程時，改用暫存目錄裡的 gate 檔：子行程輪詢檔案存在，以短間隔檢查並設總逾時，不靠睡固定秒數猜時序。
- **golden**：用一份與現行 channels.json 同形狀的合成註冊簿夾具，比對 PR-2 前後的重建輸出，要求逐位元組相同（C-8）。
- **改壞驗證**（每個 PR 至少下列幾種，要能讓對應測試失敗）：
  - C-1 拿掉 `retry-after` 判斷；C-2 不做 GET 探測；
  - C-9 寬限比較改成 `<=`，或拿掉 `cursor.at is None`；
  - C-10 只對現頁 `advance_cursor`；C-11 ETag key 不含頁；
  - C-14 不讀退役頁；
  - C-16 拿掉「先寫舊頁」；C-17 先寫新頁再貼註冊；C-8 旁頁改成取代現頁。
- **沒驗到、也不在測試裡驗的**（真 GitHub）：
  1. 達上限時的實際狀態碼與 body（issue 本文只記錄到 403 和我們自己的訊息）；
  2. 上限是否恰好是 `comments ≥ 2500`，有沒有把時間軸事件算進去；
  3. GET issue 的 `comments` 會不會延遲；
  4. comment id 跨 issue 是否全站遞增（D4 已依賴，未文件化）；
  5. 交互參照會不會吃留言額度；
  6. 鎖住的 issue 是否擋得住擁有者留言（文件說擋不住）；
  7. 註冊簿列表的複寫延遲。
  - 第 1 項會在 PR-1 上線後，靠錯誤訊息裡的 GitHub `message` 取得樣本。

## 11. 風險與未決

| # | 問題 | 選項 | 建議 | 誰決定 |
|---|---|---|---|---|
| Q-1 | 新指令 `channel status`、`channel rollover` 進 spec | 都加／只加 status、換頁只走自動與手動註冊 | 都加：PMO 需要手動換頁 w2、w3 | PM |
| Q-2 | 換頁後寫哪一頁 | A 立刻寫新頁（簡單；config 舊的讀者最多延遲 900 秒）／B 舊頁先寫滿到 2490（24h 內） | B | PM（行為） |
| Q-3 | `-b` 頻道 | 換頁後停用並在 7 天後 delete／永久保留／併回（不可行） | 停用＋7 天後 delete；要保留歷史就不 delete | PMO |
| Q-4 | ≥2000 告警怎麼到 PMO | `pmo tick` 定期跑 `channel status`（另開 d）／PMO 手動跑／寫者發 Angareion 訊息（要新設定與去重） | `pmo tick`；在那之前手動 | PMO |
| Q-5 | 舊頁封存 | 不動（建議）／鎖／關／加 label | 不動 | PMO（經本人） |
| Q-6 | PR-3 merge 的前提 | 所有席位 `angareion --version` ≥PR-2 的 commit 之後才 merge／直接 merge | 先確認，由 PMO 盤點 | PMO |
| Q-7 | 門檻值 | 2000／2400／2490／24h／48h（§1.3） | 照表 | PM |
| Q-8 | 換頁前的營運 | m1、w4 先開 `-b`／等 PR-2 之後手動換頁 | 用 PR-1 的 `channel status` 盯；剩不到 50 則還沒有 PR-2 就開 `-b` | PMO |

其他風險：

- **註冊簿的上限**：#47 也是 2500 則，現在 52 則；每次換頁多 1 則，重建的回報也算。短期沒事，`channel status` 會顯示。真的要處理時，註冊簿換頁是另一張單。
- **舊版讀者**：channels.json 換頁後，舊版讀者只讀新頁，看不到舊頁剩下的未讀訊息。手動換頁（§6）與 PR-3 都要等升級（Q-6）。
- **寫入多一個 GET**：用的是 core rate limit，不是 secondary；`send` 的量遠小於輪詢。
- **`page_size` 回應的 `Date` 當作「現在」**：沒有 `Date` 時，跳過 §1.3 第 1 步，直接看現頁（保守，等於 Q-2 的 A）。

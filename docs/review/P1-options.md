# P1 的三個選項：在這台寫出來的 Session，本機沒有原始檔

2026-10-03，review。只讀程式（HEAD `65ef23a`，在 `git archive` 取出的副本裡看），沒有改任何東西。給使用者做決定用。來源：`docs/tickets/T1-pm-run.md` P1。

## 先回答：continue 寫回的新原始檔，本機會不會留著？

**不會。** 草稿 v2 的說法是對的。路徑是這樣的：

1. `cmd_continue` 開始的時候呼叫 `fetch_raw`，把**舊的**原始檔下載到 `mirror/<ulid>/raw-<舊>.json`；
2. agent 結束時，`_finish` → `_save` → `store.stage` 把**新的**原始檔寫進 `outbox/<ulid>/`；
3. `store.remember` **只**把 `session.md` 複製到鏡像；
4. `push_one` 上傳成功之後，整個 `outbox/<ulid>/` 就被 `rmtree` 掉了。

結果：鏡像裡的 `session.md` 指向 `raw-<新>.json`，但鏡像裡只有 `raw-<舊>.json`。我在副本裡實測過（fake rclone、fake agent）：continue 之後，標頭指的是 `raw-45f266ef53f5.json`，鏡像裡只有 `raw-34d98e9fb075.json`；把它從 Drive 上刪掉之後，`push --not-exist-upload` 是 exit 2、「標頭指到的 raw-45f266ef53f5.json 本機沒有，不傳半套」。

**而且舊的那一個會一直留著**：程式裡沒有任何地方會清掉鏡像裡的舊 `raw-*`（`push_one` 只清 **Drive 上**的舊原始檔）。所以現在每接續一次、或者每次換原始檔之後再 `fetch_raw` 一次，鏡像裡就會多一個用不到的舊檔。這在今天就已經是這樣，和下面選哪一個選項無關。

## 現況：哪些 Session 救得回來

`push --not-exist-upload` 需要鏡像裡有**標頭指到的那一個**原始檔（`cache._push_mirrored(need_raw=True)` 只檢查檔案在不在）。

| 這台機器上的 Session 是怎麼來的 | 鏡像裡有沒有那個原始檔 | 別台刪掉之後救得回來嗎 |
|---|---|---|
| 這台 import 的 | 沒有（只有 session.md） | ❌ |
| 這台 continue 寫回的 | 沒有（只有舊的那一個） | ❌ |
| 這台 merge 出來的（原始檔是 `sections.json`） | 沒有 | ❌ |
| 這台 edit 過的（原始檔沒變） | 只有在之前 pull 過或接續過的時候才有 | 不一定 |
| 別台寫的、這台 pull 過的（而且之後沒有再變） | 有 | ✅ |

也就是說，「保留並標記」對**這台自己寫出來的** Session，幾乎都只是保留了閱讀版，救不回來。

---

## (a) 寫完之後，把剛上傳的原始檔也留在本機鏡像

**要改的地方**

| 檔案 | 改什麼 | 大約多少程式碼行 |
|---|---|---:|
| `store.remember` | 除了 `session.md`，也把 `folder/<raw.file>` 複製到 `mirror/<ulid>/`。用**原子寫入**：先寫暫存檔，再 `os.replace`（`write_atomic` 現在只收文字，要多一個收 bytes 的版本，或者直接用 `shutil.copyfile` 到暫存檔再 replace）。同時刪掉鏡像裡其他的 `raw-*`（和 `push_one` 在 Drive 上做的事一樣） | +6～8 |
| `store.remember` 被 `_index_outbox` 呼叫的那一條路 | sync **每一次**都會對 outbox 裡的每一筆呼叫 `remember`，所以要「同名、同大小就不複製」，不然每次同步都會重新複製好幾 MB | +1～2 |
| `store.fetch_raw`（選擇性） | 下載新的原始檔之後，順便清掉同一個資料夾裡的舊 `raw-*`，把上面那個「舊檔一直留著」也修掉 | +2 |
| `cache._push_mirrored(need_raw=True)`（選擇性） | 除了檔案在不在，也比對 md5，避免一個不完整的檔案被傳上去 | +1 |
| **合計** | | **≈ 8～13 行** |

`_save` 是 import、continue（`_finish`）、merge、edit、`recover_pending` 共用的出口，所以只要改 `remember`，這幾條路就都包含進去了。另外需要 3～4 個單元測試（不算在 `src` 的行數裡）：import 之後鏡像裡有原始檔；continue 之後鏡像裡只有**新的**那一個；別台刪掉之後 `--not-exist-upload` 會成功；outbox 裡的那一筆每次同步不會重新複製。

**風險**

| 面向 | 影響 |
|---|---|
| **磁碟用量** | 這台寫出來的每一個 Session，都會在 `~/.cache/agora/sessions/<ulid>/` 多一份原始檔，大小大約等於 agent 那邊的 session（Claude 的 jsonl 加上 sidecar、opencode 的 export JSON）。我不能讀真實的 Session，所以沒有實際量過；使用者可以自己看一下 `~/.claude/projects`、opencode 資料庫的大小來估。這只是一個快取目錄：清掉之後，只會回到現在的狀態（又救不回來了），不會丟掉 Drive 上的任何東西 |
| pull 的「本機已有就略過」 | 變得更快：`_pull_agora` → `fetch_raw` 發現本機已經有正確 md5 的原始檔，就不會下載。沒有衝突 |
| continue 開始時的下載 | 也省掉了：`fetch_raw` 直接用本機的（這是 PM 說的附帶好處） |
| 索引重建（`rebuild_from_mirror`） | 不受影響：它只讀 `*/session.md` |
| sync 的 G3（Drive 上的原始檔還沒齊，就把本機的 session.md 刪掉） | 不受影響：它只刪 session.md，原始檔留在資料夾裡，等之後被覆蓋或清掉 |
| 別台又更新了這個 Session | sync 只會換 session.md；本機的原始檔變成舊的，下一次 `fetch_raw` 時 md5 不符，就會重新下載。不會用錯（如果做了上面那個選擇性的清理，舊的那一個也會被清掉） |
| 舊原始檔的清理 | 一定要在 `remember` 裡清掉其他的 `raw-*`，不然每接續一次就會多累積一個 |
| `push`（不帶 flag） | 現在，本機沒有原始檔時，`push` 只傳 session.md。改成 (a) 之後，`push` **每一次**都會把原始檔重新傳一次（同名覆蓋，結果一樣，只是多花頻寬）。可以在 `_push_mirrored` 裡，Drive 列檔出來的 md5 已經和標頭一樣時就跳過（+1 行） |
| 寫到一半當掉 | 如果不用原子寫入，鏡像裡可能留下一個名字對、內容不完整的原始檔，而 `need_raw` 只檢查檔案在不在。用暫存檔再 replace（或在 `need_raw` 那裡比對 md5）就可以避免 |
| delete、`--not-exist-delete` | `forget_local`／`delete_session` 都是整個資料夾一起刪，原始檔也會一起不見，沒有問題 |

**spec 和文件要改的句子**
- `docs/design.md` 5.10 第 393 行：「`session.md` 就是閱讀版，**原始檔用到才下載**」→「這台寫出來的 Session，原始檔也會留在鏡像裡；別台的，用到才下載」。
- `specs/session-sync/spec.md`：**MUST 不用改**。「本機沒有那個原始檔時拒絕這一個」照舊成立，只是變得很少遇到。可以在「雲端沒有的 Session 只在明確要求時刪除或復活」後面補一句說明：「這台機器寫出來的 Session，原始檔會留在本機，所以傳得回去」。
- README 的雲端沒有那一段：補一句同樣的說明。

## (b) 只改拒絕的訊息

**要改的地方**：`cache._push_mirrored` 在 `need_raw` 時丟出的訊息，**依照 relation 分開說**，因為「從 agent 重新 import」不是每一種都能用：

| relation | 能不能救 | 訊息應該寫 |
|---|---|---|
| import、continue | agent 那邊的 session 如果還在，可以重新 import（會是一個**新的** Session，舊的 id 回不來） | 「…本機沒有原始檔，傳不回去；可以從 agent 重新匯入：`agora import session --external-session-id <source.session_id> --agent <source.agent>`（會是新的 Session）」 |
| merge | 原始檔是 `sections.json`，是 agora 自己產生的，沒有 agent 可以重新匯入 | 「…可以重新合併：`agora merge session <parents…>`」（如果來源都還在） |

`source.session_id`／`agent` 和 `parents` 都在標頭裡，所以訊息可以把**整個指令**直接寫出來。

- **行數**：大約 **+4～6 行**（讀出 relation 和來源，組出訊息）。
- **風險**：幾乎沒有。行為不變，只是訊息不同；要補 1～2 個測試來斷言訊息的內容。
- **spec 要改的句子**：`specs/session-sync/spec.md` 第 49 行「本機沒有那個原始檔時拒絕這一個」→ 後面加上「，並提示能救回來的方法（從 agent 重新匯入成新的 Session，或者重新合併）」。design 5.10 第 405 行同一句也一起改；README 也一樣。
- **限制**：救回來的會是**新的** id，所以原本的 id、勾選、別的 Session 的 `parents` 指向的，都還是那個被刪掉的。對使用者來說，「保留並標記」的意義就從「可以撤銷別台的刪除」變成「在本機還留著一份可以看的閱讀版」。

## (c) 維持現狀

- **行數**：0。
- **風險**：功能上沒有新的風險；但「保留並標記 → `--not-exist-upload` 傳回去」這條路，對這台自己寫出來的 Session 幾乎都走不通（見上面的表），只有遇到的時候才會知道。訊息「標頭指到的 raw-….json 本機沒有，不傳半套」沒有告訴使用者接下來可以怎麼做。
- **spec／文件**：spec 不用改。建議至少在 README 和 design 5.10 寫明「只有本機拿過原始檔的 Session 傳得回去」，並把它列在驗收草稿的已知問題裡（現在已經列了）。

---

## 比較

| | (a) 留原始檔 | (b) 改訊息 | (c) 維持 |
|---|---|---|---|
| 這台自己寫的 Session 被別台刪掉，救得回來嗎 | ✅ 用原本的 id | ⚠️ 只能變成新的 id（merge 要重新合併） | ❌（使用者也不知道該怎麼做） |
| `src` 的行數 | +8～13 | +4～6 | 0 |
| 磁碟 | 每個在這台寫的 Session 多一份原始檔（可以清掉的快取） | 不變 | 不變 |
| 附帶的好處 | continue、pull 不必再下載；順便修掉舊原始檔累積的問題 | 訊息講得出下一步 | — |
| 主要的風險 | 原子寫入、清掉舊檔、`push` 重傳原始檔，這三個都要做對 | 沒有 | 沒有 |
| spec 的 MUST | 不用改（補說明就好） | 改一句 | 不用改 |

**review 的意見**（決定權在使用者）：**(a) 再加上 (b) 裡 import 那一種的訊息**。

- (a) 才真的符合「保留並標記，讓使用者還能選擇傳回去」的用意，改的地方也集中在 `remember` 這一個出口；
- 但即使做了 (a)，還是有救不回來的情況：快取被清掉了、或者是別台寫的又沒有在這台 pull 過。這時候，(b) 的訊息可以告訴使用者下一步怎麼做，只要多 2～3 行。
- 如果行數的額度很緊，就只做 (b)。

不管選哪一個，「鏡像裡的舊 `raw-*` 一直累積」是今天就存在的問題，建議順便在 `fetch_raw`（或 `remember`）裡清掉，只要 +2 行。

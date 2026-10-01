# Review：docs/design.md（Agora lite 基本設計）

2026-10-02，review。只提意見，不改 design.md，也不推翻 D1–D5。對象是 commit `7109c4d` 的 design.md。

嚴重度的意思：
- **High**：照現在的寫法做，會掉資料、讀到半份，或六個指令中有一個做不出來。
- **Medium**：做得出來，但會讓行數超標、等待變長，或之後四個實體互相參照時要改 header。
- **Low**：寫清楚就好，不用改設計。

## 摘要

| # | 嚴重度 | 項目 | 一句話建議 |
|---|---|---|---|
| S1 | High | 兩個檔案的寫入不是原子的 | 先傳 raw，最後傳 session.md；header 記 raw 的 md5，比對不上就不建索引 |
| S2 | High | 上傳失敗沒有重送的地方 | 加 outbox（不放在 `~/.cache`），`sync` 先推再拉 |
| S3 | High | continue-session 中途被打斷，結果就沒存 | 啟動 agent 前先寫 pending 記錄，下一次執行任何指令時補存 |
| S4 | High | `opencode import` 沒有「換新 id」的選項 | 要把 session／message／part 的 id 全部改寫，否則可能蓋掉或混進原本的 session |
| H1 | High | header 少了工作目錄 | `source` 加上 `dir`（還有 `host`），continue 的 `--dir` 預設用它 |
| S5 | Medium | 增量 sync 用「比較新」判斷，刪除也不會同步 | 改成 `--checksum`，並且清掉 Drive 上已經不存在的鏡像資料夾 |
| S6 | Medium | 重新匯入時，靠本機索引找對應的 agora id | import 前先做增量 sync；查詢結果依來源去重 |
| S7 | Medium | 重新匯入會蓋掉已經有子 Session 的父 Session | `parents` 記下父的版本（raw md5）；蓋掉之前先比對 |
| S8 | Medium | Drive 允許同名資料夾；`drive.file` 看不到其他 client 建的檔 | 把 `agora/` 的 folder ID 寫進設定；寫明只能透過 agora 寫入，以及 OAuth client 的注意事項 |
| S9 | Medium | Claude 的 Session 不只有一個 jsonl | V2 要確認 subagent／tool-results 這些附屬檔；raw 要把它們包進去 |
| L1 | Medium | 閱讀版轉換器是行數最可能爆掉的地方 | 先定一個最小的閱讀版規格 |
| L2 | Medium | 每次 search 都先 sync，而且整份鏡像含 raw | 鏡像只抓 session.md，raw 用到才抓；sync 加節流與離線退路 |
| L3 | Low | merge 的 raw.json 與 5.4 的載入方式表互相矛盾 | merge 的 raw 只記父 id 與 md5，不複製內容 |
| L4 | Low | 2,000 行沒有說含不含測試 | 寫清楚 |
| H2 | Medium | `case` 沒有實體前綴；ref 的語法沒有定義 | `case` 也用 `mybrain:...`；定義 `<entity>:<locator>[@<rev>][#<frag>]` |
| H3 | Medium | 共通欄位和 agora 專用欄位混在同一層 | 把共通欄位列成清單；實體專用欄位放進以實體為名的區塊（或在 design 裡列出名單） |
| H4 | Medium | 缺少 header 的版本欄位 | 加 `header: 1` |
| H5 | Medium | 沒辦法放 YAML 的項目（Google Docs、二進位檔）header 要放哪裡 | 規定可以用 sidecar `<name>.agora-header.md`（或 Drive appProperties） |
| H6 | Low | `--header` 在 import（`key: value`）與 search（`key=value`）寫法不一樣；`agent` 是巢狀欄位 | 統一成一種寫法；列出 search 可以用的扁平別名 |
| H7 | Low | `created_at` 指的是來源建立的時間還是匯入的時間 | 寫清楚，另外加 `source.created_at` |

下面依 PM 指定的三件事展開。

---

## (1) 2,000 行做得完嗎？哪裡會意外變大

**結論：有條件可行。** 前提是閱讀版先定成最小規格（L1）、sync 只做必要的部分（L2）、merge 不另外存 raw（L3）。照這個前提粗估如下（不含測試）：

| 模組 | 估計行數 | 備註 |
|---|---|---|
| `header` | 150 | 用 yaml.safe_dump／safe_load、`--header` 解析、驗證 |
| `store` | 350 | 包 rclone 呼叫（copy、lsjson、mkdir）、outbox、鏡像、FTS5 重建與增量 |
| `agents/opencode` | 300 | export、改寫 id 後 import、閱讀版轉換、找出結束後的 session |
| `agents/claude` | 350 | 找 jsonl、編碼專案路徑、改寫 sessionId／cwd、閱讀版轉換、附屬檔 |
| `cli` | 400 | 六個指令、continue 的流程與 pending 補存、merge |
| **合計** | **約 1,550** | 留 ~450 行的餘裕 |

### L1【Medium】閱讀版轉換器是最可能爆掉的地方
兩個 agent 的匯出格式都很細，而且一直在變。opencode 的 part 有 text、reasoning、tool、file、patch、step 等型態；Claude 的 jsonl 有 user／assistant、tool_use／tool_result、thinking、summary、compact boundary、sidechain、meta 等行。如果要每一種都「好看地」轉出來，單一個 agent 就可能超過 500 行，兩個 agent 合起來就把預算吃光了。
**建議**：在 design 第 4 節加一段「閱讀版規格」：
- 只輸出 user／assistant 的文字；
- 工具呼叫只寫一行 `[tool] <名稱> <參數摘要，截斷 200 字>`，不輸出工具的結果（或只留前 N 行）；
- reasoning／thinking 不輸出；
- 認不得的型態輸出 `[skip <type>]`，不要讓程式失敗。

這樣兩個轉換器各 80–120 行就夠。搜尋只靠閱讀版和 header，所以這也等於決定了「搜得到什麼」，需要使用者確認。

### L2【Medium】search 的前置 sync 會讓等待變長
- 5.1 寫的是「每次 search 先做增量 sync」。`sessions/<ULID>/` 是一個 Session 一個資料夾，沒有加 `--fast-list` 時，rclone 會每個資料夾各列一次。幾百個 Session 時，每次 search 就要等好幾十秒，違反第 1 節的「等待短」。
- 第 4 節寫的鏡像是「Drive 的本機鏡像」。raw.json（尤其是長的 Claude Session）可能有幾 MB 到幾十 MB，每台機器都抓全部並不必要。

**建議**：
- 鏡像只抓 `session.md`（`--include '*/session.md'`），raw 在 continue／show --raw 用到時才抓；
- 列檔一律用 `rclone lsjson -R --fast-list --hash`，一次拿到所有檔案的 md5，和本機比對後只抓有變的；
- search 的前置 sync 要節流（例如距離上次成功 sync 不到 5 分鐘就跳過），網路不通時退回只查本機，印一行警告就好，不要失敗；
- 加 `--no-sync` 旗標。

### L3【Low】merge 的 raw 跟載入方式表互相矛盾
5.3 寫「raw.json 記錄各來源的 raw，接續時才決定怎麼載入」，但 5.4 的表已經寫死「merge 出來的 → 閱讀版注入」。複製一份父 Session 的 raw 不但會讓 Drive 用量加倍，還要多寫一種 raw 格式。
**建議**：merge 的 raw.json 只寫 `{"parents": [{"id": ..., "raw_md5": ...}]}`，或者乾脆不建 raw.json。原生載入 merge 結果的需求等真的出現時再加。

### L4【Low】行數的範圍沒寫
**建議**：第 8 節寫清楚「2,000 行是 `src/` 的程式碼，不含測試與 fixture」（或者兩者都算）。這樣 impl 才不會把測試砍掉來達標。

### 其他會讓行數變大、但已經算進上面估計的
- S3 的 pending 補存（約 60 行）、S2 的 outbox（約 60 行）、S4 的 opencode id 改寫（約 40 行）、Claude 的專案路徑編碼與 sessionId 改寫（約 40 行）。這幾項都是 High 等級的正確性問題，不能為了行數省掉。

---

## (2) 第 3 節的共通 header 夠不夠用

**結論：骨架夠用**（`entity`、帶前綴的 `id`、`refs` 都對）。但缺了幾個 continue 會用到的欄位，ref 的語法也沒定義，之後 Foundry／Atelier 加入時會撞到。

### H1【High】缺工作目錄（以及產生的機器）
opencode 的 session 屬於某個「專案目錄」（依 git 判定），Claude 的 session 屬於 `~/.claude/projects/<編碼過的路徑>/`。原生接續時要在正確的目錄裡啟動，否則 `opencode --session` 或 `claude --resume` 會找不到那個 session，或者把它放進錯的專案。header 現在沒有記這個資訊，所以 `--dir` 只能靠使用者自己記得。
**建議**：
```yaml
source:
  agent: opencode
  session_id: ses_xxxx
  dir: /Users/.../proj      # 來源 session 所屬的專案目錄（絕對路徑）
  host: mbp-2024            # 產生這份的機器（只用來顯示與判斷 dir 在這台機器上存不存在）
  agent_version: 1.18.34    # 匯出時的 agent 版本，格式變動時用來判斷
```
`continue-session` 的 `--dir` 預設值：如果 `source.dir` 在這台機器上存在就用它，否則用目前目錄，並印出實際用的目錄。

### H2【Medium】`case` 沒有前綴；ref 的語法沒有定義
- 所有跨實體的指標都帶前綴，只有 `case` 不帶。等 MyBrain 有不變 id 之後，`case` 會變成全 header 唯一一個要靠上下文猜是哪個實體的值。
- `atelier:<職務>@<版本>` 已經在 id 後面接了版本，但第 3 節的 id 規則是 `<實體>:<不變 id>`，所以 ref 其實不等於 id。MyBrain 的路徑可能含有 `:`、`#`、空白，如果沒有規定怎麼切，之後各實體的解析會各寫各的。

**建議**：在第 3 節定義 ref 的語法：
```
ref     = entity ":" locator [ "@" rev ] [ "#" fragment ]
entity  = "mybrain" | "agora" | "foundry" | "atelier"
```
- 只用**第一個** `:` 切開；locator 裡如果出現 `@` 或 `#`，要做百分比編碼；
- `case` 的值也是一個 ref（例如 `case: "mybrain:案件/xyz"`），或者是 `null`；
- YAML 裡的 ref 一律加引號輸出（由 safe_dump 處理），避免路徑裡的字元被 YAML 誤判。

### H3【Medium】共通欄位和 agora 專用欄位混在同一層
第 3 節說共通的是 `entity`、`id`、`refs`、`case`，但範例的頂層也放了 `source`、`relation`、`parents`。Foundry 之後很可能也需要「來源」與「衍生自誰」，而它的意思不同（例如來源是 GitHub 的 commit）。如果都放在頂層，同名不同義的欄位一定會撞。
**建議**（二選一，寫進 design）：
- (a) 列出頂層的保留欄位：`header, entity, type, id, title, created_at, updated_at, refs, case, note, tags`。其他欄位都視為實體專用，只有該實體自己的程式會解讀；或
- (b) 把實體專用的欄位收進以實體為名的區塊：`agora: {source, relation, parents}`。

(a) 改動最小。另外也要對照 MyBrain 的 OKF frontmatter（這次 review 沒有讀 MyBrain，只是提醒）：如果 OKF 也有 `type`、`tags`、`title`、`created`／`updated` 這類欄位，要寫清楚 MyBrain 那邊是「不改、由 Agora 做對應」，還是之後要對齊。

### H4【Medium】缺少 header 的版本欄位
四個實體各自演進，以後一定會改 header。沒有版本欄位的話，讀的一方只能靠猜。
**建議**：加 `header: 1`。讀取時遇到不認得的版本，警告後只讀共通欄位就好。

### H5【Medium】放不了 YAML 的項目
D3 說 header 仿 frontmatter，但不是每個項目都是 markdown。例如放在 Drive 上的 Google Docs，或者二進位檔，就沒地方放 front matter。
**建議**：寫一條規則：項目本身放不了 front matter 時，旁邊放一個 sidecar `<檔名>.header.md`（只有 header），或者用 Drive 的 `appProperties`。sidecar 比較單純，也不用另外寫程式。

### H6【Low】`--header` 的兩種寫法
- import／merge 用 `--header 'key: value'`，search 用 `--header key=value`。同一個旗標兩種寫法，使用者一定會打錯。另外，`note` 的自由文字如果剛好含有「`注意: ...`」這種「字＋冒號＋空白」，會被當成鍵值對。現在的規則是「不在白名單裡的 key 一律當 note」，結果雖然安全，但使用者看不懂發生了什麼。
- search 範例的 `--header agent=claude` 指向的是巢狀欄位 `source.agent`。

**建議**：
- 兩邊都用 `key=value`，自由文字改用 `--note '...'`（或者寫成 `note=...`）；
- 列出 search 可以用的扁平別名（`agent` 對應 `source.agent`、`relation`、`case`、`tag`、`ref`），其他 key 直接報錯。

### H7【Low】`created_at` 的意思
**建議**：寫清楚 `created_at` 是 agora 建立這個項目的時間；來源 session 的建立時間另外放 `source.created_at`。search 輸出的日期用哪一個也要寫清楚（建議用來源的時間）。

### 其他（不用改設計，實作時注意）
- header 一律由 `yaml.safe_dump` 產生，不要用字串拼接。使用者的 note 或 title 裡可能有換行，或者一整行 `---`。
- 解析時只把**第一個** `---` 區塊當 header。閱讀版的內文裡很可能也有 `---`。
- `refs` 是單向的，反向連結（「哪些 Session 參照了這篇 MyBrain」）要靠各實體自己建索引。這點寫一句在 design 裡就好，現在不用做。

---

## (3) Drive（沒有條件寫入）與 drive.file 下的資料遺失／讀到半份

**結論：有條件可行。** 每個 Session 用一個 ULID 資料夾，這個方向是對的：新增不會互相覆寫。但現在一個 Session 由兩個檔案組成，寫入的順序、上傳失敗、中途被打斷這三種情況都沒有處理。以下 S1–S4 補上之後，在 D2（信任自己的機器）的範圍內就沒有已知的資料遺失路徑。

### S1【High】兩個檔案的寫入不是原子的，讀的一方會讀到半份
Drive 的單一檔案上傳是原子的（上傳完成才出現新內容），但 `session.md` 和 `raw.json` 是兩次上傳。另一台機器在中間 sync 的話，會發生：
- 新 Session：看得到 `session.md`，`raw.json` 還沒出現。這時 continue 會失敗，或者只能用閱讀版；
- 重新匯入：拿到新的 `session.md` 配舊的 `raw.json`（或者反過來）。閱讀版和原生載入的內容對不起來，而且不會有任何錯誤訊息。

**建議**：
1. 上傳順序固定：**先 raw，最後 session.md**。`session.md` 就是「這個版本完成了」的標記；
2. header 加 `raw: {file: raw-<md5前12碼>.json, md5: ..., size: ...}`。raw 的檔名帶內容的雜湊，重新匯入時寫成**新檔名**，等 `session.md` 換過去之後，才刪掉舊的 raw。這樣任何時候 Drive 上的 `session.md` 都指向一份完整存在的 raw；
3. sync／continue 拿到 raw 時，用 Drive 提供的 md5（`rclone lsjson --hash`）比對 header。對不上就當作「還沒寫完」，跳過這個 Session、不建索引，下次 sync 再試。

這一共大約 30 行，卻能同時解決 S1、S7 的比對，以及 L2 的增量判斷。

### S2【High】上傳失敗沒有地方重送；鏡像不能拿來暫存
第 6 節說「只有 import 與 continue-session 結束時寫入」，而 `sync` 只拉不推。如果 continue 結束的時候網路斷了、token 過期（見 S8），或者 Drive 回傳 rate limit，這一次的結果就只存在 agent 自己的資料庫裡，Agora 不會記得它的 `parents`。另外，第 4 節說 `~/.cache/agora/` 是「壞了刪掉重建」的快取。如果實作把還沒上傳的 Session 先寫進鏡像，使用者一刪快取，這些 Session 就沒了。如果之後把 sync 改成 `rclone sync`（會刪掉目的地多出來的檔案），也會把它們刪掉。
**建議**：
- 加一個 outbox：`~/.local/state/agora/outbox/<ULID>/`。它**不在** `~/.cache` 底下，design 裡也要寫明「不能刪」；
- import／continue 先把完整的 Session 寫進 outbox，再依 S1 的順序上傳。用 md5 確認 Drive 上的內容一致之後，才移出 outbox；
- `sync` 改成「先推 outbox，再拉」。每個指令開始時，如果 outbox 不是空的，就印一行提示；
- 第 6 節「不做背景 daemon」可以維持不變。

### S3【High】continue-session 中途被打斷，結果就沒存
5.4 的第 3 步是在 agent 結束後，由 agora 這個父行程去收尾。下面這些情況都會讓第 3 步沒有執行：終端機視窗被關掉（SIGHUP 會一起殺掉父行程）、筆電睡眠後 SSH 斷線、agora 本身的例外、agent 崩潰後 agora 也跟著出錯。這時新的 session 留在 agent 裡，但 Agora 不知道它存在，來源關係也不見了。
**建議**：
1. 啟動 agent **之前**，先寫一份 `~/.local/state/agora/pending/<新 ULID>.json`，內容是 `{parent, agent, agent_session_id, dir, started_at}`。Claude 可以先用 `--session-id <uuid>` 決定好 id；opencode 在 `opencode import` 之後就知道新 id；
2. agent 結束後照常收尾，成功時刪掉 pending；
3. **任何** agora 指令開始時，先檢查 pending。如果對應的 agent session 已經不在執行，就補做第 3 步（export → outbox → 上傳）；
4. 父行程在 agent 執行期間忽略 SIGINT（`signal.SIG_IGN`），只把 Ctrl-C 交給 agent，避免使用者在 agent 裡按 Ctrl-C 時，agora 先被殺掉。

### S4【High】`opencode import` 沒有「換新 id」的選項
`opencode import --help`（1.18.34）只有 `<file>` 這個參數，沒有任何指定新 id 的選項。5.4 寫的「`opencode import` 成新 session（新 id）」，實作上只能靠 agora 自己去改 export 的 JSON。如果只改 session id，而 message／part 的 id 沿用原本的，在**原本那台機器**上 import 的結果可能會是蓋掉或混進原本的 session，或是 message 主鍵衝突。這等於直接改到使用者真實的 session。
**建議**：
- 在 design 寫明：原生載入 opencode 時，要把 export 裡**所有** id 欄位（session id，以及每個 message 與 part 的 id 和它們的 `sessionID`）全部換成新產生的 id，然後才 import；
- 請 V1 的 spike 實際驗證：(a) 在原本那台機器上 import 改寫過的 export 之後，原本的 session 沒有任何變化（比對 export 前後的 message 數與 md5）；(b) 沒改寫 message id 時會發生什麼。

### S5【Medium】增量 sync 的判斷方式，以及刪除
- 5.1 寫的是「只下載 Drive 上比鏡像新的檔案」。如果照字面用 `--update`（依時間判斷），兩台機器時間不一致，或者上傳時保留了舊的 mtime，新內容就會被跳過。
- 第 6 節說刪除就是在 Drive 刪掉那個資料夾，但增量 copy 不會把刪除同步到鏡像。結果是已經刪掉的 Session 還搜得到，還能被 continue。

**建議**：
- 比對用 `--checksum`（Drive 有 md5），或者直接用 S1 的 md5 清單自己比，不要用 `--update`；
- sync 之後，列出 Drive 上的 `sessions/*/` 清單，把鏡像裡多出來的資料夾連同索引一起刪掉。只清鏡像，不碰 outbox／pending；
- 不要加 `--inplace`（rclone 1.69 預設會先寫到 `.partial` 再改名，本機不會看到半個檔案）。

### S6【Medium】重新匯入時找對應 id 靠的是本機索引
5.2 說重新匯入時，用 `source.agent` 加 `source.session_id` 找到同一個 agora id。但這要靠本機索引。另一台機器的鏡像如果還沒 sync，或是 search 被節流（L2）跳過了 sync，就會產生新的 ULID，於是同一個來源出現兩個 agora Session。
**建議**：import 一律先做一次（不節流的）增量 sync 再查對應。查詢與 show 遇到同一個來源有多個 agora id 時，顯示最新的那個，並且印出警告。不需要做衝突處理。

### S7【Medium】重新匯入會靜默地改掉父 Session
「同一個來源再匯入一次，更新內容」，但這個 Session 可能已經有 continue／merge 的子 Session。子 Session 是基於舊內容做的，父 Session 卻被換掉了，之後回頭追來源的時候看到的不是當時的內容。舊內容也只剩 Drive 本身的修訂紀錄（有期限）。
**建議**：
- `parents` 的每一項記下當時的 raw md5：`parents: [{id: "agora:01K6...", raw_md5: "..."}]`；或者維持字串，另外加一個對應的 `parents_rev`；
- 重新匯入時，如果內容變了，而且已經有子 Session（本機索引查得到），就不要覆寫，改成建一個新的 Session，`relation: import`、`parents: [舊 id]`，然後印出新 id。這樣仍然「最後寫的贏」，只是不會吃掉舊版本。

### S8【Medium】Drive 同名、drive.file 的可見範圍、token
- **同名資料夾**：Drive 允許同一層有兩個同名資料夾。兩台機器第一次執行時如果都去建 `agora/`（或 `sessions/`），就會有兩個。rclone 遇到時只會警告 duplicate，然後看起來就像一部分 Session 不見了。**建議**：`agora` 第一次執行時建好資料夾，把它的 folder ID 寫進 `config.toml`（或 rclone remote 的 `root_folder_id`）。之後所有機器都用 ID 存取，不再用名字找。`sync` 時用 `rclone lsjson` 檢查有沒有同名的資料夾，有的話就警告。
- **drive.file 的可見範圍**：使用者從 Drive 網頁或其他 app 拖進 `agora/` 的檔案，agora 看不到。如果 worker OAuth client 所在的 GCP 專案被刪掉或重建（換了 client ID），以前建的檔案**全部**會看不到。資料沒有不見，但 agora 讀不到。**建議**：在 design 的 D5 底下寫明：(a) 只能透過 agora 寫入；(b) 不要刪除或重建那個 OAuth client；(c) 萬一換了 client，復原的方法是什麼（例如用一次性的較大 scope 把檔案複製成新 client 擁有的檔，或用 Drive Picker 重新授權）。
- **token 過期**：如果 OAuth consent screen 還在「Testing」狀態，而且使用者類型是 external，Google 發的 refresh token 7 天就會失效，失效之後上傳會失敗（這時就需要 S2 的 outbox）。**建議**：在 design 寫明 client 要改成「In production」狀態，或者寫明 token 每 7 天要重新授權一次。這條請 V4 的 spike 順便確認一下現在的狀態。

### S9【Medium】Claude 的 Session 不只有一個 jsonl（讀到半份）
5.2 只找 `~/.claude/projects/*/<id>.jsonl`。但較新版的 Claude Code（這台機器是 2.1.286）可能會把 subagent 的對話、太大的 tool 結果另外存在 `<id>/` 子資料夾裡。只包主檔的話，raw 是半份，原生載入之後 `--resume` 可能找不到被引用的內容。另外，每一行都帶有 `sessionId` 與 `cwd`。如果只是複製成新檔名、沒有改寫這兩個欄位，`--resume` 之後實際寫入的可能是另一個檔案，收尾時讀到的就是沒有接續內容的半份。
**建議**：
- V2 加三個檢查：(a) `<id>/` 附屬資料夾存不存在、裡面有什麼（只記檔名形狀）；(b) 複製時要改寫哪些欄位，`--resume` 才會接著寫**同一個**檔案；(c) `--resume <新 id>` 能不能和 `--session-id` 一起用（`--help` 只寫 `--session-id` 必須是 UUID，`--fork-session` 要搭配 `--resume`）；
- design 寫明 raw.json 的 Claude 格式是 `{"main": [...jsonl lines], "aux": {"<相對路徑>": ...}}`；
- 收尾時檢查「行數比啟動前多」，沒有變多就不要存，把它留在 pending，並印出警告。

### S10【Low】匯入正在執行中的 session
如果 import 的時候那個 session 還在使用中，Claude 的 jsonl 最後一行可能只寫了一半，opencode 的 export 也可能少了最後一則訊息。**建議**：解析 jsonl 時，最後一行不完整就丟掉，並印出警告；import 前先檢查 opencode export 出來的 JSON 能不能解析、message 數是否大於 0，不行就不要上傳。另外寫明 export 一律輸出到檔案（5.2 已經寫了），stderr 另外導走。

---

## 對 docs/design.md 的修改建議（依優先順序）

1. 第 4 節：加入 S1 的寫入順序與 `raw` 欄位（帶雜湊的檔名與 md5），以及 S2 的 outbox 與 S3 的 pending 這兩個位置（`~/.local/state/agora/`，不能刪），並寫明 `sync` 是先推再拉。
2. 5.4：寫明 opencode 載入前要改寫哪些 id（S4）、SIGINT 的處理、pending 補存，以及 `--dir` 的預設值（H1）。
3. 第 3 節：`source` 加 `dir`、`host`、`agent_version`、`created_at`，再加上 `header: 1`、ref 的語法、讓 `case` 也成為 ref，並列出頂層保留欄位（H1–H4、H7），以及 sidecar 規則（H5）。
4. 第 4 節：加一段閱讀版的最小規格（L1，需要使用者確認搜得到什麼）。
5. 5.1：sync 的節流、`--no-sync`、離線退路，鏡像只抓 session.md，`--checksum`，清除已刪除的 Session（L2、S5）。
6. 5.2：import 前先 sync（S6），內容變了而且已經有子 Session 時改成建新 Session（S7）。
7. D5 底下：folder ID、只能透過 agora 寫入、不要刪除或重建 client、consent screen 要在 production 狀態（S8）。
8. 第 7 節：V1 加上 S4 的驗證；V2 加上 S9 的三個檢查；V4 加上 token 狀態與同名資料夾的檢查。
9. 第 8 節：寫明行數的範圍（L4）。merge 不另外存 raw（L3）。

## 這次實際跑過的指令

只讀了 design.md，看了幾個 CLI 的 `--help`。沒有碰 Drive，沒有讀或列出任何 Session、MyBrain 或 opencode 的資料庫，也沒有讀 rclone.conf。

| 指令 | 看到的結果（只記形狀） |
|---|---|
| `git status --short`、`git log --oneline -3`、`ls docs` | 工作樹是乾淨的，只有 `docs/design.md` |
| `rclone version`、`rclone help flags \| grep ...` | rclone v1.69.3；有 `--checksum`、`--fast-list`、`--update`、`--inplace`（預設先寫 `.partial` 再改名） |
| `claude --version`、`claude --help \| grep ...` | 2.1.286；`--session-id <uuid>`（必須是 UUID）、`--resume [id]`、`--fork-session`（要搭配 `--resume`／`--continue`） |
| `opencode --version`、`opencode import --help`、`opencode export --help` | 1.18.34；`import <file>` 沒有指定 id 的選項；`export [sessionID]` 有 `--sanitize` |

上面所有和實際行為有關的推論（opencode id 衝突、Claude 附屬檔、`--resume` 會寫到哪個檔、token 7 天過期），都還需要 V1、V2、V4 的 spike 確認。impl1／impl2 的報告出來之後，我會對照這份 review 再看一次。

## ADDED Requirements

### Requirement: raw 分段儲存（`chunks/1`）

`agora.raw` MAY 用分段的方式存一份 raw。分段版本 MUST 有下列欄位：

- `layout` 恰為 `chunks/1`；
- `segments` 是一個非空的清單，依順序排列，每一項是 `{file, size, md5}`；
- `file`／`md5`／`size` 仍指**整份** raw（不是某一段）：`md5` 是接起來之後的 md5，`size` 是所有 `size` 的總和。

整份 raw 的位元組 MUST 等於 `segments` 依序串接的位元組；**最後一段（tail） MAY 每一版不同，其餘的段 MUST 不變**。段檔 MUST 是 `sessions/<ULID>/` 下面的扁平檔名（`seg-<gen>-<seq>-<md5[:12]>.part`），MUST NOT 放在子目錄，MUST NOT 被當成一份可以單獨解析的 raw。段檔名 MUST 只是安全的 basename（不含 `/`、`\`、`..`、不以 `.` 開頭）。

除了 tail 之外，每個封存段 MUST NOT 大於 `AGORA_RAW_CHUNK`（預設 4 MiB）；tail 不受這個上限限制（它含 trailer 與 aux）。**整份（full）建立的第一版 MUST 把 `[0, main_end)` 切成多段**（`ceil(main_end / C)`），MUST NOT 只切成一個大段。

`session.md` 仍是**唯一**的「這個版本完成了」標記，也是分段的清單（沒有另一個 manifest 檔）。寫入的一方 MUST 在上傳 `session.md` 之前，讓清單裡的每一個段「已在 Drive 上或在本輪送出」，並在送出後用 listing 核對每一個段的 md5。**核對不通過的版本 MUST NOT 離開 outbox**，MUST 在下一輪補回缺的段（或整份重來）。一個已經在 Drive 上的版本 MAY 在稍後因為另一台機器的清理而變成不完整（見下面「兩台同時寫」）；那是允許的狀態，不是成功，也不是資料損壞。

讀的一方 MUST 只有在「清單非空、所有段都在 listing 裡且 md5 相符、`size` 加起來等於 `raw.size`」時，才把這個版本當成完整；否則 MUST 當成「還沒寫完」跳過（不建索引、不從鏡像提供）。**同一個版本若同時有 `raw.file` 且它在 listing 裡的 md5 等於 `raw.md5`（降級後舊版寫回時上傳的整份），MAY 改用整份；兩者都不完整才跳過。** 要接回整份時 MUST 逐段驗 md5、依序串接、再驗整份的 `md5` 與 `size`；任何一個不符 MUST NOT 把結果當成 raw 使用。同一次接回操作內，已經下載且驗過 md5 的段 MUST NOT 重抓（重試時只補缺的段）。

舊的單檔 `raw-<md5[:12]>.json` 版本 MUST 照常可讀（`layout` 不在時走 `raw.file`）。未知的 `layout` MUST NOT 猜測段格式、MUST NOT 半讀：同一份 header 的整份 raw（`raw.file`）在 listing 且 md5 等於 `raw.md5` 時 MAY 當成完整並讀整份（header 的 `md5` 就是整份）；整份不可用（沒有 `raw.file`、或 md5 不符）時 MUST 拒絕（當成未完成或 `StoreError`）。

#### Scenario: 分段版本完整
- **WHEN** 某個 Session 的 `session.md` 有 `layout: chunks/1` 與三段，三段的 md5 都與 listing 相符
- **THEN** sync 把它建進索引；`agora show session <id> --raw` 印出的位元組等於整份 raw 的 md5

#### Scenario: 第一次切段（814 MB 的舊 session）
- **WHEN** 一個 814 MB 的 session 第一次被切成段（新 import 或第一次寫回）
- **THEN** Drive 上有約 195 個封存段＋1 個 tail，**沒有一個 814 MB 的段**；缺任何一段時補回只花那一段的大小

#### Scenario: 少一段
- **WHEN** 清單列了三段，Drive 上只有兩段
- **THEN** 這個版本被當成還沒寫完：不建索引、鏡像的 `session.md` 拿掉、下次同步再試；沒有下載半份 raw

#### Scenario: 段被改壞
- **WHEN** 某一段的 Drive md5 與清單不符
- **THEN** 接回時 `StoreError`；暫存檔刪掉；鏡像沒有留下半份 raw

#### Scenario: 舊的單檔版本
- **WHEN** 一個 Session 沒有 `layout`，只有 `raw.file` 與 `raw.md5`
- **THEN** 照舊下載、驗 md5；行為與這個 change 之前相同

#### Scenario: 舊版寫回後兩種都在
- **WHEN** 一個分段版本同時有完整的段與一份 `raw.file`（舊版寫回時傳上去的），兩者的 md5 都與標頭相符
- **THEN** 優先用段；段不完整時改用整份；兩者都不完整才當未完成跳過

#### Scenario: 同時有段與整份、段完整但整份缺
- **WHEN** 標頭有 `layout: chunks/1`、三段的 md5 都對、但 `raw.file` 在 listing 裡沒有（或 md5 不符）
- **THEN** 用段；**不要**因為整份缺而把版本當成未完成

#### Scenario: 段不完整但整份完整
- **WHEN** 標頭有 `layout: chunks/1`、清單有一段不在 listing、而 `raw.file` 在 listing 且 md5 等於 `raw.md5`
- **THEN** 用整份；不建索引以外不報錯，也不下載任何段

#### Scenario: 未知的 layout，但整份可用
- **WHEN** `agora.raw.layout` 是 `chunks/9`（這個版本不認得），而同一個 header 有 `raw.file`，它在 listing 的 md5 等於 `raw.md5`
- **THEN** 這個版本被當成完整（header 的 md5 就是整份）；`show --raw`／pull 讀整份；**不**猜 `chunks/9` 的段格式、也不下載任何段

#### Scenario: 未知的 layout，整份也不可用
- **WHEN** `agora.raw.layout` 是 `chunks/9`，而且沒有可用的整份（`raw.file` 不在 listing，或 md5 與 `raw.md5` 不符）
- **THEN** 不猜格式；當成未完成跳過，`sync` 印一行原因；必要時 `show --raw` 明確報錯；MUST NOT 讀半份

#### Scenario: 清單檔名不安全
- **WHEN** `segments[].file` 含有 `/`、`\`、`..` 或以 `.` 開頭
- **THEN** 這個版本被當成壞的（未完成／`StoreError`），MUST NOT 用那個名字去下載或刪除任何東西

### Requirement: 只上傳新增的段；不是單純追加就退回整份

一個分段版本 MAY 只上傳新增的段（差分上傳），但 MUST 同時滿足：

- 來源是同一個 agent session、同一個 raw 格式，而且包含上一版的封存前綴（可以比上一版短，只要封存前綴還在、位元組相同）；
- 這一版 raw 的封存前綴與上一版逐位元組相同（以記錄的前綴 md5 驗證）；
- 上一版是 `chunks/1`；
- 本機有可用的 cursor（換機器、清 cache、cursor 壞掉時可以沒有，但那就是退回整份）。

成立時 MUST 只送「上一個封存點之後到檔尾」的位元組，按 `AGORA_RAW_CHUNK` 切成新的封存段與 tail；MUST NOT 重傳已封存且已在 Drive 上的段；缺的基準段依下一段補回。新的 tail 與新的 `session.md` 仍照「段先、`session.md` 後」的順序。

**基準段不在 Drive 上不是退回整份的理由**：上傳的一方 MUST 從本機的整份 raw 按清單的 `size` 切出缺的段、驗證 md5 後補上；補不回（切出來的 md5 與清單不符）MUST NOT 發布這個版本，留在 outbox 並警告。這樣跨機器把別人的段清掉時，補回只花缺的 bytes，不必重傳整個 session。

下列任一成立時 MUST 退回整份（新的世代、整份按 C 切成多段、上傳所有段、發布 `session.md`、確認後才清舊段）：

- 封存前綴被改寫，或來源短到不足以包含封存的行數；
- 上一版不是 `chunks/1`，或沒有可用的 cursor；
- 上一版的整份 md5 與 cursor 記錄的基準不同；
- 上一版的封存段加起來不等於這次的 `append_from`；
- 代理的 agent session 換了（新的 `continue`；cursor 的 agent session id 與這次不同）；
- 寫端被關閉（`AGORA_RAW_LAYOUT=legacy`）；
- adapter 沒有提供增量（opencode、agy、merge、fake）。

只砍掉封存點之後的內容（封存前綴仍相同）不算改寫：這一版 MAY 走 append，新的 tail 較短。

退回整份時 MUST NOT 在確認前刪掉任何舊段；舊的 `session.md` 與它指的段 MUST 在新版本確認前保持完整。清理失敗 MUST 只警告、下一次再試，MUST NOT 讓一個失敗的清理擋住後續版本。

同時有兩台機器寫同一個 Session 時，最後上傳的 `session.md` 贏。發現基準段不見的那一台 MUST 從自己的整份 raw 補回缺的段再發布；補不回 MUST NOT 發布，留在 outbox 並警告。**這一段時間內，Drive 上的版本 MAY 指到缺段**：讀者 MUST 把它當未完成（見「raw 分段儲存」），MUST NOT 把它當成壞資料或刪掉它。

#### Scenario: 長 session 的下一輪
- **WHEN** 一個 814 MB 的 claude Session 已經有一個分段版本在 Drive，**同一個 `continue`** 在 5 分鐘後多了 1 MB 的對話
- **THEN** 這一輪只上傳新的段與 tail（≤ 4 MiB＋1 MB），不重傳已經封存的 814 MB；`session.md` 更新，讀者接回來仍是整份

#### Scenario: 新開一個 continue 的第一次寫回
- **WHEN** 上一次寫回之後使用者結束了 agent，再 `agora continue` 同一個 Session（新的 agent session，`start_native` 換掉每一行的 session id）
- **THEN** 第一次寫回是整份（新的世代、約 195 個段）；之後同一個 continue 的定期寫回才只送新的段。這是固定成本，不是失敗

#### Scenario: 來源被改寫
- **WHEN** agent 的 jsonl 被 `/compact` 或別的原因改寫，封存前綴的 md5 與上一版不同
- **THEN** 這一輪走整份：新的世代、所有段重傳、`session.md` 最後上傳；結果與整份路徑逐位元組相同

#### Scenario: 只砍掉未封存的部分
- **WHEN** 來源變短，但封存的行數還在、位元組也相同（只砍到封存點之後）
- **THEN** 這一輪仍可 append：只送較短的新 tail；封存段不重傳；接回來等於新的整份 raw

#### Scenario: 基準段被別台清掉（單寫者看到的版本）
- **WHEN** 這一版要沿用的一段在 Drive 上已經沒有了（別台寫了更新的版本，且已過寬限期）
- **THEN** 上傳的一方從本機的整份 raw 切出缺的段、驗 md5 後補上，再發布 `session.md`；補不回才留在 outbox 並警告

#### Scenario: 兩台同時寫，一台先發布
- **WHEN** A 傳完自己的新段、還沒傳 `session.md`，B 在這時確認了自己的版本並清理
- **THEN** B 的清單與 A 的段無關；A 的段在寬限期內不會被刪；A 發布的版本完整。若 A 的段超過寬限期才被當孤兒清掉，A 的版本在 Drive 上**可能指到缺段**——讀者當未完成跳過，A 下一輪補回；MUST NOT 有讀者讀到半份或混合版本

#### Scenario: 清理失敗
- **WHEN** 新版本確認成功，但清舊段時 rclone 失敗
- **THEN** 印一行警告；舊段留著；下一次確認成功再試；新版本照常可讀

### Requirement: 舊段只刪「被取代」與「過期」的

一個版本確認成功、離開 outbox 之後，寫入的一方 MAY 清掉 Drive 上不再需要的 `seg-*`，但 MUST 只刪同時滿足這三條的檔：

1. 不在**目前**清單裡；
2. 不在**前一版**清單裡（這一版取代掉的那一份 `session.md` 的 `segments`）。這份清單 MUST 來自**上一次確認成功時留下的記錄**（`state/published/<ULID>.json`：那一版 `session.md` 的 md5 與清單），而且只有當記錄的 md5 等於**送出前** Drive 上那份 `session.md` 的 md5 時才算數——那才是真的被這一版取代的那一版。MUST NOT 用 `stage` 當下看到的上一版：離線時連續 stage，那個「上一版」是還沒上傳的本機版本，Drive 上公開的那一版反而沒被保護。對不上（別台在這期間寫過）就當沒有前一版，只剩第 3 條；
3. Drive 的 `ModTime` 超過寬限期（`AGORA_RAW_ORPHAN_GRACE`，預設 3600 秒）。寫入的一方 MUST 在送出段檔之前把它們的 mtime 設成現在，讓這個時鐘是「上傳那一刻」而不是「切出那一刻」。

MUST NOT 刪「本輪 listing 有、但不在目前清單」的所有段——那會刪掉另一台剛傳上來、還沒發布 `session.md` 的段。`raw-*.json` 的清理照舊（只刪不是 `raw.file` 的）。**單檔版本的確認 MUST 跑同一條**：`repack`、`AGORA_RAW_LAYOUT=legacy` 的寫回、R1 機器上的 `edit` 都會把一個分段 Session 寫成單檔，之後沒有「分段項目」可以觸發清理；沒有這一條，那些 `seg-*` 永遠沒人指到、也永遠不會被刪（舊的 `_replaced_raws` 只認 `raw-*`）。那時的「目前清單」是空集合。確認成功之後 MUST 把這一版寫成新的記錄（同一個檔），下一輪才知道誰是被取代的版本；寫記錄失敗只影響下一次的前一版保護（那一輪只剩寬限期），不影響正確性。清理 MUST 用一次批次刪除；失敗 MUST 只警告。

#### Scenario: 別台剛傳的段不被刪
- **WHEN** A 把新段傳上 Drive、還沒傳 `session.md`；B 在同一個資料夾確認成功並清理
- **THEN** A 的段還在（不在 B 的前一版清單，但在寬限期內）

#### Scenario: 慢速讀者握著前一版
- **WHEN** 讀者拿到 v1 的清單，v2、v3 相繼發布（同一台機器確認的）
- **THEN** v2 發布時 v1 的段還留著（記錄裡的前一版清單＋寬限期）；v3 發布後 v1 的段才會被清掉。讀者最壞是重抓一次 header，不是讀到缺段。多寫者時前一版清單可能是空的（那個版本不是這台確認的），那一版只靠寬限期——一樣不會讀到缺段，只是可能要重抓幾段

#### Scenario: 孤兒段
- **WHEN** 一次上傳留下沒人指到的 `seg-*`（崩在 `session.md` 之前）
- **THEN** 它在寬限期內不被刪；之後任何一次成功的清理會刪掉它

#### Scenario: 單檔版本確認後清孤兒段
- **WHEN** 一個分段 Session 被 `repack`（或 `AGORA_RAW_LAYOUT=legacy` 的寫回、R1 機器上的 `edit`）寫成單檔，資料夾裡留著沒人指到的舊 `seg-*`
- **THEN** 這次單檔版本的確認也跑同一條清理：超過寬限期的孤兒段一次批次刪掉；沒超過的留著；刪不掉只警告

### Requirement: 由段轉回單檔（`repack`）

`agora repack session <id>…`（`--all` 表示本機索引裡所有分段 Session，**加上 `unfinished.json` 記的不完整分段 Session**；先做一次完整 sync）MUST 把指定的分段 Session 轉回單檔：上傳完整的 `raw-<md5>.json`、發布一份 `agora.raw` 只有 `{file, md5, size}`、沒有 `layout`／`segments` 的 `session.md`，並用 Drive 的 md5 核對兩者。它 MUST 先取得整份 raw（outbox → 鏡像 → 從段接回），拿不到或驗不過時 MUST 拒絕那一個並以非 0 結束，MUST NOT 發布半套。

確認成功後 MAY 刪掉舊的 `seg-*`（一次批次刪除，失敗只警告；規則同「舊段只刪被取代與過期的」，也會清掉同一個資料夾裡超過寬限期的孤兒段）；MUST NOT 在確認前刪。`--all` MUST 用本機索引的 header 決定要處理哪些 Session（先一次完整 sync，處理完再一次完整 sync 重新確認），MUST NOT 為了判斷而把 Drive 上每一個 `session.md` 都下載一次；**只掃索引會漏掉不完整的分段 Session**（`sync` 會把它們從索引拿掉，記在 `unfinished.json`），所以那些也要納入：`unfinished.json` v2 記著那一份 header 的 `layout`，只挑 `chunks/1` 的來轉，轉不了（本機沒有整份 raw、段也接不回）就逐個列出。結束時還有 `chunks/1` 就列出並以非 0 結束。**exit 0 才代表可以降級到讀不懂分段的舊版**；這個判定只反映那個時刻（另一台之後仍可能寫出新的分段版本）。

#### Scenario: 降級前轉回單檔
- **WHEN** Drive 上有一個 `layout: chunks/1` 的 Session，執行 `agora repack session agora:X`
- **THEN** Drive 上有完整的 `raw-<md5>.json` 與一份沒有 `layout` 的 `session.md`；舊版的兩個 predicate（`raw.file` 在 listing 且 md5 相符）都通過；舊段被清掉（或留著也不影響）

#### Scenario: 本機沒有整份 raw
- **WHEN** repack 一個分段 Session，本機沒有整份 raw，且從段接回失敗（離線、段缺）
- **THEN** 這一個失敗、exit 非 0；Drive 上的版本維持原樣（仍是可讀的分段版本）

#### Scenario: --all 還有殘留
- **WHEN** `agora repack session --all` 之後 Drive 上還有一個 `chunks/1` 的 Session（例如那台機器離線）
- **THEN** 列出它、exit 非 0；使用者不可以降級

#### Scenario: --all 遇到不完整的分段 Session
- **WHEN** 一個 `chunks/1` 的 Session 缺段（`sync` 已經把它從索引拿掉，記在 `unfinished.json`），執行 `agora repack session --all`
- **THEN** 它也被納入（`unfinished.json` 的 `layout` 是 `chunks/1`）；本機接不回整份 raw 就列出它、exit 非 0——MUST NOT 因為它不在索引裡就當成「已經沒有分段版本」

## MODIFIED Requirements

### Requirement: pull 只處理給的 id

`agora pull session <id>…` MUST 只處理給的 id，不給 id 時 MUST 報錯（exit 1）。沒有前綴或 `agora:` 前綴的 id 是 agora 的 Session：拿下它的 `session.md` 與標頭指到的原始檔；**分段儲存的（`agora.raw.layout: chunks/1`）預設是下載清單裡所有的段、逐一核對 md5 後接回整份原始檔；段不完整或下載失敗、而 `raw.file` 在 listing 裡且 md5 等於 `raw.md5` 時，MAY 改下載整份（「兩種都在」的退路，見「raw 分段儲存」）**。`opencode:<id>`、`claude:<id>` 是這台機器上的 agent session：把它的全文（閱讀版）寫進本機快取。`ses_…` 或 uuid 沒有寫 agent 前綴時 MUST 報錯，不從形狀猜。已經在本機、沒有過時的 MUST 略過。

#### Scenario: 拿下一個 Session 的原始檔
- **WHEN** 本機只有某個 Session 的 `session.md`，執行 `agora pull session agora:X`
- **THEN** 它的原始檔被下載到本機，exit 0

#### Scenario: 拿下一個分段儲存的 Session
- **WHEN** 本機只有某個分段 Session 的 `session.md`，執行 `agora pull session agora:X`
- **THEN** 所有段被下載、逐段驗 md5、接回整份；本機鏡像有一份 `raw-<md5>.json`，md5 與標頭相符，exit 0

#### Scenario: 寫進 agent session 的全文快取
- **WHEN** 執行 `agora pull session claude:<uuid>`
- **THEN** 那個 session 的閱讀版寫進本機快取；再執行一次時，因為沒有過時而略過

#### Scenario: 沒給 id
- **WHEN** 執行 `agora pull session`（型態有寫，但沒給任何 id）
- **THEN** exit 1，提示要給 session id

### Requirement: push 只寫回該寫的檔案

`agora push session <agora id>…` MUST 先送出 outbox，再把每個給的、雲端還在的 Session 的 `session.md` 與它的原始檔寫回 Drive，同名覆蓋。**分段儲存的 Session，原始檔是 `agora.raw.segments` 列出的每一個段**：本機缺的段 MUST 用本機的整份原始檔按清單的 `size` 切出來再傳；MUST NOT 上傳不在這份清單裡的分段。**本機沒有整份原始檔、而且 Drive 上缺段時 MUST 拒絕這一個（exit 非 0），MUST NOT 只傳 `session.md` 讓清單指到不存在的段。** **從鏡像送出時**，Drive 上多出來的檔不刪；**從 outbox 送出時**（批次上傳），被取代的舊 `raw-*` 與依「舊段只刪被取代與過期」規則該清的 `seg-*` 會被刪掉（換過原始檔的 Session 需要這樣，N8），其餘多出來的檔仍然不刪。本機沒有那個原始檔、但 Drive 上已經有完整的原始檔（或完整的段）時，只傳 `session.md`；Drive 上不完整時拒絕這一個。舊的原始檔、下載到一半的檔、以 `.` 開頭的檔 MUST NOT 被上傳。

#### Scenario: 覆蓋雲端的版本
- **WHEN** 本機改過某個 Session 的 `session.md`，執行 `agora push session agora:X`
- **THEN** Drive 上的 `session.md` 變成本機的版本

#### Scenario: 不傳多餘的檔
- **WHEN** 本機的鏡像裡有舊的原始檔、`*.partial`、`.DS_Store`
- **THEN** push 之後 Drive 上沒有這些檔

#### Scenario: 分段 Session 補回缺的段
- **WHEN** 一個分段 Session 在 Drive 上少了一段，本機鏡像有整份 raw，執行 `agora push session agora:X`
- **THEN** 缺的段由整份 raw 按清單的 size 切出、上傳；`session.md` 與所有段的 md5 都相符；沒有上傳不在清單裡的分段

# session-sync Specification

## Purpose
本機與 Google Drive 之間明確地搬 Session：pull 把給的 Session 拿到本機，push 把給的 Session 寫回 Drive；別台機器刪掉的 Session 不會被悄悄傳回去。
## Requirements
### Requirement: pull 只處理給的 id

`agora pull session <id>…` MUST 只處理給的 id，不給 id 時 MUST 報錯（exit 1）。沒有前綴或 `agora:` 前綴的 id 是 agora 的 Session：拿下它的 `session.md` 與 manifest 指到的段；**分段儲存的（`agora.raw.layout: chunks/1`）預設是下載 manifest 裡缺少的段、逐一核對 md5 後接回整份原始檔**。不是 `chunks/1` 的 Session（舊格式、未知格式）MUST 只建索引與閱讀版、MUST NOT 下載 raw、MUST 給明確的「raw 不可用，請先遷移」。`opencode:<id>`、`claude:<id>` 是這台機器上的 agent session：把它的全文（閱讀版）寫進本機快取。`ses_…` 或 uuid 沒有寫 agent 前綴時 MUST 報錯，不從形狀猜。已經在本機、沒有過時的 MUST 略過。

#### Scenario: 拿下一個 Session 的原始檔
- **WHEN** 本機只有某個 Session 的 `session.md`，執行 `agora pull session agora:X`
- **THEN** 它的原始檔被下載到本機，exit 0

#### Scenario: 拿下一個分段儲存的 Session
- **WHEN** 本機只有某個分段 Session 的 `session.md`，執行 `agora pull session agora:X`
- **THEN** 缺少的段被下載、逐段驗 md5、接回整份；本機鏡像有一份 `raw-<md5>.json`，md5 與標頭相符，exit 0

#### Scenario: 舊格式的 Session
- **WHEN** 執行 `agora pull session agora:X`，而 X 的 header 沒有 `layout`
- **THEN** 它的 `session.md` 進鏡像與索引（閱讀版可讀）；不下載 raw；印一行說明 raw 不可用、要遷移；exit 0

#### Scenario: 寫進 agent session 的全文快取
- **WHEN** 執行 `agora pull session claude:<uuid>`
- **THEN** 那個 session 的閱讀版寫進本機快取；再執行一次時，因為沒有過時而略過

#### Scenario: 沒給 id
- **WHEN** 執行 `agora pull session`（型態有寫，但沒給任何 id）
- **THEN** exit 1，提示要給 session id

### Requirement: push 只寫回該寫的檔案

`agora push session <agora id>…` MUST 先送出 outbox，再把每個給的、雲端還在的 Session 的 `session.md` 與它的原始檔寫回 Drive，同名覆蓋。**分段儲存的 Session，原始檔是 `agora.raw.segments` 列出的每一個段**：本機缺的段 MUST 用本機的整份原始檔按 manifest 的 `size` 切出來再傳；MUST NOT 上傳不在這份 manifest 裡的分段、MUST NOT 上傳整份 raw。**本機沒有整份原始檔、而且 Drive 上缺段時 MUST 拒絕這一個（exit 非 0），MUST NOT 只傳 `session.md` 讓 manifest 指到不存在的段。** 從鏡像送出時，Drive 上多出來的檔不刪；從 outbox 送出時（批次上傳），依「舊段只刪被取代與過期」規則該清的 `seg-*` 會被刪掉，其餘多出來的檔仍然不刪。本機沒有那個原始檔、但 Drive 上已經有完整的原始檔（或完整的段）時，只傳 `session.md`；Drive 上不完整時拒絕這一個。舊的原始檔、下載到一半的檔、以 `.` 開頭的檔 MUST NOT 被上傳。不是 `chunks/1` 的 Session MUST 拒絕 push 它的 raw（只傳 `session.md` 也不行：那會讓 manifest 不存在），並說明沒有原地轉存、要重匯請用 `agora import … --as-new`。

#### Scenario: 覆蓋雲端的版本
- **WHEN** 本機改過某個 Session 的 `session.md`，執行 `agora push session agora:X`
- **THEN** Drive 上的 `session.md` 變成本機的版本

#### Scenario: 不傳多餘的檔
- **WHEN** 本機的鏡像裡有舊的原始檔、`*.partial`、`.DS_Store`
- **THEN** push 之後 Drive 上沒有這些檔

#### Scenario: 分段 Session 補回缺的段
- **WHEN** 一個分段 Session 在 Drive 上少了一段，本機鏡像有整份 raw，執行 `agora push session agora:X`
- **THEN** 缺的段由整份 raw 按 manifest 的 size 切出、上傳；`session.md` 與所有段的 md5 都相符；沒有上傳整份 raw、也沒有上傳不在 manifest 裡的分段

#### Scenario: 舊格式的 Session 不能 push
- **WHEN** 對一個沒有 `layout` 的 Session 執行 `agora push session agora:X`
- **THEN** 拒絕這一個、exit 非 0，說明沒有原地轉存、要重匯請用 `agora import … --as-new`；Drive 原樣不動

### Requirement: 雲端沒有的 Session 保留在本機並標記
同步時，**只有在這次列檔完整成功**時，本機有、雲端沒有、而且不在 outbox、也不是**正在接續中**（`pending/<ULID>.json` 的鎖有人拿著；只是留下來、沒有人拿鎖的記錄不算）的 Session MUST 被標成「雲端沒有」，鏡像與索引 MUST 保留。列檔失敗、離線或 Drive 上沒有 `sessions/` 時，MUST NOT 新增或清除任何標記。雲端又出現時，下一次同步 MUST 清除標記。還在 outbox 的 MUST 顯示為「未上傳」，不算雲端沒有。

#### Scenario: 別台機器刪掉了
- **WHEN** 另一台機器刪掉 X，這台機器同步
- **THEN** X 還在這台的索引與鏡像裡，標成雲端沒有

#### Scenario: 列檔失敗
- **WHEN** 同步時列檔失敗或離線
- **THEN** 沒有任何 Session 被標成雲端沒有，原本的標記也不變

#### Scenario: 又出現了
- **WHEN** X 被另一台機器傳回 Drive，這台再同步
- **THEN** X 的「雲端沒有」標記被清掉

### Requirement: 雲端沒有的 Session 只在明確要求時刪除或復活
`pull --not-exist-delete` MUST 刪掉給的 id 中雲端沒有的本機副本；在 outbox 或接續中的 MUST NOT 被刪，並印出原因。`push --not-exist-upload` MUST 把給的 id 中雲端沒有的傳回 Drive（傳 `session.md` 與它指到的原始檔；本機沒有那個原始檔時拒絕這一個）。都沒加 flag 時，遇到雲端沒有的 MUST 只印一行提醒、不動。flag 只對 agora 的 id 有作用；對 agent 的 id，`--not-exist-delete` 的意思是「agent 那邊已經沒有這個 session 了，就刪掉它的全文快取」。

#### Scenario: 刪掉本機副本
- **WHEN** X 雲端沒有，執行 `agora pull session X --not-exist-delete`
- **THEN** X 從這台機器的鏡像與索引消失

#### Scenario: 不刪還沒上傳的
- **WHEN** Y 還在 outbox，執行 `agora pull session Y --not-exist-delete`
- **THEN** Y 沒有被刪，訊息說它還沒上傳

#### Scenario: 復活
- **WHEN** X 雲端沒有，執行 `agora push session X --not-exist-upload`
- **THEN** X 回到 Drive，下一次同步後標記被清掉

### Requirement: 其他指令遇到雲端沒有的 Session
會寫回既有 id 的指令（continue、edit，以及互動模式裡的對應動作）遇到雲端沒有的 Session MUST 拒絕（exit 1），並提示 `agora push session X --not-exist-upload` 或 `agora pull session X --not-exist-delete`。search 與 show MUST 標出「雲端沒有」，search MUST 支援 `--filter cloud=no` 與 `cloud=yes`。merge MUST NOT 接受雲端沒有的 Session 當來源。delete 一個雲端沒有的 Session MUST 只刪本機副本（exit 0）。雲端沒有的 Session MUST NOT 算成別人的子 Session（不擋刪除、不觸發 import 的分岔）。import 的來源對到雲端沒有的那一筆時 MUST NOT 寫回它，而是 MUST 建一個新的 Session（使用者接受 review T1 Q3 的建議；review T1-final 指出原本和上一句矛盾）。

#### Scenario: 接續被別台刪掉的
- **WHEN** X 雲端沒有，執行 `agora continue session X --agent opencode`
- **THEN** exit 1，提示兩個選擇，agent 沒有被打開

#### Scenario: 接續到一半被別台刪掉的
- **WHEN** 執行 `agora continue session X` 之前 X 還在雲端，agent 工作的這段時間裡另一台機器刪掉了 X
- **THEN** 這次的工作不丟掉：另存成一個新的 Session Y（relation continue、parents 指向 X），並說明 X 已被刪；X 維持被刪的狀態，沒有被寫回去

#### Scenario: 用管線清掉
- **WHEN** 執行 `agora search session --filter cloud=no`
- **THEN** 只列出雲端沒有的 Session，每行最後標示 `(雲端沒有)`

### Requirement: search 每一行顯示最後更新時間
`agora search session` 的每一行 MUST 顯示該 Session 的最後更新時間（標頭的 `agora.updated_at`），不再只有建立日／對話日期。行的形狀：

`agora:<ULID>  <對話日期>[（更新：YYYY-MM-DD）]  <agent 或 relation>  <標題或摘要>  <標記>…`

- 對話日期是現行的 `store.sort_date`（來源對話、`generated.at`、匯入時間）；**清單的排序 MUST 維持現行**（對話日期新到舊，`updated_at` 已是次鍵），不因這個改動改變。
- `agora.updated_at` 與對話日期**不同**時，MUST 緊接在對話日期後面補上 `（更新：YYYY-MM-DD）`（只到日、不含空白，維持每行的空格分欄）；相同時 MUST 省略。`agora.updated_at` 沒有時（舊標頭）MUST 只顯示對話日期。

#### Scenario: 很久以前對話、今天被寫回
- **WHEN** 一個 Session 的對話日期是 2026-08-01、今天（2026-10-18）被寫回
- **THEN** 那一行是 `agora:…  2026-08-01（更新：2026-10-18）  …`，不會看起來像 8 月就沒動過

#### Scenario: 剛匯入、來源對話是更早的
- **WHEN** 今天匯入一個來源對話日期是 2026-10-01 的 Session
- **THEN** 那一行是 `agora:…  2026-10-01（更新：2026-10-18）  …`（兩個日期不同就都看得到）

#### Scenario: 舊標頭沒有 updated_at
- **WHEN** 一個 Session 的標頭沒有 `agora.updated_at`
- **THEN** 那一行只顯示對話日期，沒有「（更新：…）」；指令不因此失敗

### Requirement: search 標出本機使用中
本機正在接續中的 Session（`pending/<ULID>.json` 的鎖有人拿著；`store.continuing`，見 `session-continue`）MUST 在該行標成 `(使用中)`，與 `(未上傳)`、`(雲端沒有)` 並存。只是留下來的記錄（鎖沒人拿）MUST NOT 算使用中。

「使用中」只判斷**這台機器**的鎖：別台機器上的接續從這裡看不到（沒有共用的鎖），MUST NOT 假裝看得到；這個限制 MUST 寫在使用者文件（README）裡。

#### Scenario: 本機正在接續
- **WHEN** A 正在這台機器上被接續，`agora search session`
- **THEN** A 那一行有 `(使用中)`

#### Scenario: 收不了尾的記錄
- **WHEN** `pending/<ULID>.json` 留著但沒有任何人拿它的鎖
- **THEN** 那一行不標 `(使用中)`（而且下一次執行指令會把它補存或警告）

#### Scenario: 別台機器上的接續
- **WHEN** B 正在別台機器上被接續，這台執行 `agora search session`
- **THEN** 這台的清單不標 B 使用中；這是已知限制，README 寫明

### Requirement: raw 一律分段儲存（`chunks/1`）

一個**新寫入**的 Session MUST 用分段的方式存它的 raw，而且**只有這一種**（保留原樣、不是 `chunks/1` 的舊 Session 見「不是 `chunks/1` 的 Session 不猜、不刪」）：

- `agora.raw.layout` 恰為 `chunks/1`；
- `agora.raw.segments` 是一個非空的清單，依順序排列，每一項是 `{file, size, md5}`；
- `agora.raw.md5`／`size` 指**整份** raw（不是某一段）：`md5` 是接起來之後的 md5，`size` 是所有 `size` 的總和；
- **MUST NOT 寫 `agora.raw.file`**：新寫入的 Session 在 Drive 上沒有整份 raw 這個檔案（保留原樣的舊 Session 不在這條的範圍，見「不是 `chunks/1` 的 Session 不猜、不刪」）。本機鏡像的整份 raw 檔名由 `raw.md5` 推得（`raw-<md5[:12]>.json`）。

整份 raw 的位元組 MUST 等於 `segments` 依序串接的位元組；**最後一段（tail） MAY 每一版不同，其餘的段 MUST 不變**。段檔 MUST 是 `sessions/<ULID>/` 下面的扁平檔名（`seg-<gen>-<seq>-<md5[:12]>.part`），MUST NOT 放在子目錄，MUST NOT 被當成一份可以單獨解析的 raw。段檔名 MUST 只是安全的 basename（不含 `/`、`\`、`..`、不以 `.` 開頭）。

除了 tail 之外，每個封存段 MUST NOT 大於 `AGORA_RAW_CHUNK`（預設 4 MiB）；**claude 的** tail 不受這個上限限制（它含 trailer 與 aux）。**整份（full）建立的第一版 MUST 把 `[0, main_end)` 切成多段**（`ceil(main_end / C)`），MUST NOT 只切成一個大段；**但 `main_end ≤ C`（claude）或 `size ≤ C`（其他 agent）時 MUST 什麼都不封存、只寫一個 tail（`raw_stable = 0`）**——小 session 的檔案數與每輪上傳量因此和以前一樣，而它仍然是 `chunks/1`，MUST NOT 改回單檔。

`session.md` 仍是**唯一**的「這個版本完成了」標記，也是 manifest（沒有另一個 manifest 檔）。寫入的一方 MUST 在上傳 `session.md` 之前，讓 manifest 裡的每一個段「已在 Drive 上或在本輪送出」，並在送出後用 listing 核對每一個段的 md5。**核對不通過的版本 MUST NOT 離開 outbox**，MUST 在下一輪補回缺的段（或整份重來）。一個已經在 Drive 上的版本 MAY 在稍後因為另一台機器的清理而變成不完整（見「只上傳缺的段」）；那是允許的狀態，不是成功，也不是資料損壞。

讀的一方 MUST 只有在「manifest 非空、所有段都在 listing 裡且 md5 相符、`size` 加起來等於 `raw.size`」時，才把這個版本當成完整；否則 MUST 當成「還沒寫完」跳過（不建索引、不從鏡像提供）。要接回整份時 MUST 逐段驗 md5、依序串接、再驗整份的 `md5` 與 `size`；任何一個不符 MUST NOT 把結果當成 raw 使用。同一次接回操作內，已經下載且驗過 md5 的段 MUST NOT 重抓（重試時只補缺的段）。

#### Scenario: 分段版本完整
- **WHEN** 某個 Session 的 `session.md` 有 `layout: chunks/1` 與三段，三段的 md5 都與 listing 相符
- **THEN** sync 把它建進索引；`agora show session <id> --raw` 印出的位元組等於整份 raw 的 md5

#### Scenario: 第一次切段（814 MB 的舊 session）
- **WHEN** 一個 814 MB 的 session 第一次被切成段（新 import 或來源被改寫後的新世代）
- **THEN** Drive 上有約 195 個封存段＋1 個 tail，**沒有一個 814 MB 的段**；缺任何一段時補回只花那一段的大小

#### Scenario: 小 session 也是一個段
- **WHEN** 一個 3 KiB 的 Session（例如 merge 的要約）被寫入
- **THEN** 它有一個 `segments` 項（tail）、`layout: chunks/1`；Drive 上的檔案數和以前一樣是 1，只是檔名從 `raw-*.json` 變成 `seg-*.part`

#### Scenario: 少一段
- **WHEN** manifest 列了三段，Drive 上只有兩段
- **THEN** 這個版本被當成還沒寫完：不建索引、鏡像的 `session.md` 拿掉、下次同步再試；沒有下載半份 raw

#### Scenario: 段被改壞
- **WHEN** 某一段的 Drive md5 與 manifest 不符
- **THEN** 接回時 `StoreError`；暫存檔刪掉；鏡像沒有留下半份 raw

#### Scenario: 清單檔名不安全
- **WHEN** `segments[].file` 含有 `/`、`\`、`..` 或以 `.` 開頭
- **THEN** 這個版本被當成壞的（未完成／`StoreError`），MUST NOT 用那個名字去下載或刪除任何東西

### Requirement: 只上傳缺的段；不是單純追加就退回整份

一個分段版本 MAY 只上傳 Drive 還沒有的段（差分上傳），但 MUST 同時滿足：

- 來源是同一個 agent session、同一個 raw 格式，而且包含上一版的封存前綴（可以比上一版短，只要封存前綴還在、位元組相同）；
- 這一版 raw 的封存前綴與上一版逐位元組相同（以記錄的前綴 md5 驗證）；
- 上一版是 `chunks/1`；
- 本機有可用的 cursor（換機器、清 cache、cursor 壞掉時可以沒有，但那就是退回整份）。

成立時 MUST 只送「上一個封存點之後到檔尾」的位元組，按 `AGORA_RAW_CHUNK` 切成新的封存段與 tail；MUST NOT 重傳已封存且已在 Drive 上的段；缺的基準段依下一段補回。新的 tail 與新的 `session.md` 仍照「段先、`session.md` 後」的順序。

**段的檔案 MUST 在上傳那一輪才從本機的整份 raw 切出來**（切進該輪的 batch dir）；outbox MUST NOT 長期保存第二份段檔。切出來的段 md5 與 manifest 不符時 MUST NOT 發布這個版本，留在 outbox 並警告。

**基準段不在 Drive 上不是退回整份的理由**：上傳的一方 MUST 從本機的整份 raw 按 manifest 的 `size` 切出缺的段、驗證 md5 後補上；補不回（切出來的 md5 與 manifest 不符）MUST NOT 發布這個版本，留在 outbox 並警告。這樣跨機器把別人的段清掉時，補回只花缺的 bytes，不必重傳整個 session。

下列任一成立時 MUST 退回整份（新的世代、整份按 C 切成多段、上傳所有段、發布 `session.md`、確認後才清舊段）：

- 封存前綴被改寫，或來源短到不足以包含封存的行數；
- 上一版不是 `chunks/1`、或沒有可用的 cursor（**不是 `chunks/1` 的 Session MUST 拒絕寫入，見「不是 `chunks/1` 的 Session」**）；
- 上一版的整份 md5 與 cursor 記錄的基準不同；
- 上一版的封存段加起來不等於這次的 `append_from`；
- 代理的 agent session 換了（新的 `continue`；cursor 的 agent session id 與這次不同）；
- adapter 沒有提供增量（opencode、agy、merge、fake）。

只砍掉封存點之後的內容（封存前綴仍相同）不算改寫：這一版 MAY 走 append，新的 tail 較短。

退回整份時 MUST NOT 在確認前刪掉任何舊段；舊的 `session.md` 與它指的段 MUST 在新版本確認前保持完整。清理失敗 MUST 只警告、下一次再試，MUST NOT 讓一個失敗的清理擋住後續版本。

同時有兩台機器寫同一個 Session 時，最後上傳的 `session.md` 贏。發現基準段不見的那一台 MUST 從自己的整份 raw 補回缺的段再發布；補不回 MUST NOT 發布，留在 outbox 並警告。**這一段時間內，Drive 上的版本 MAY 指到缺段**：讀者 MUST 把它當未完成（見「raw 一律分段儲存」），MUST NOT 把它當成壞資料或刪掉它。

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

#### Scenario: 基準段被別台清掉
- **WHEN** 這一版要沿用的一段在 Drive 上已經沒有了（別台寫了更新的版本，且已過寬限期）
- **THEN** 上傳的一方從本機的整份 raw 切出缺的段、驗 md5 後補上，再發布 `session.md`；補不回才留在 outbox 並警告

#### Scenario: 兩台同時寫，一台先發布
- **WHEN** A 傳完自己的新段、還沒傳 `session.md`，B 在這時確認了自己的版本並清理
- **THEN** B 的 manifest 與 A 的段無關；A 的段在寬限期內不會被刪；A 發布的版本完整。若 A 的段超過寬限期才被當孤兒清掉，A 的版本在 Drive 上**可能指到缺段**——讀者當未完成跳過，A 下一輪補回；MUST NOT 有讀者讀到半份或混合版本

#### Scenario: 清理失敗
- **WHEN** 新版本確認成功，但清舊段時 rclone 失敗
- **THEN** 印一行警告；舊段留著；下一次確認成功再試；新版本照常可讀

### Requirement: 舊段只刪「被取代」與「過期」的

一個版本確認成功、離開 outbox 之後，寫入的一方 MAY 清掉 Drive 上不再需要的 `seg-*`，但 MUST 只刪同時滿足這三條的檔：

1. 不在**目前** manifest 裡；
2. 不在**前一版** manifest 裡（這一版取代掉的那一份 `session.md` 的 `segments`）。這份 manifest MUST 來自**上一次確認成功時留下的記錄**（`state/published/<ULID>.json`：那一版 `session.md` 的 md5 與 manifest），而且只有當記錄的 md5 等於**送出前** Drive 上那份 `session.md` 的 md5 時才算數——那才是真的被這一版取代的那一版。MUST NOT 用 `stage` 當下看到的上一版：離線時連續 stage，那個「上一版」是還沒上傳的本機版本，Drive 上公開的那一版反而沒被保護。對不上（別台在這期間寫過）就當沒有前一版，只剩第 3 條；
3. Drive 的 `ModTime` 超過寬限期（`AGORA_RAW_ORPHAN_GRACE`，預設 3600 秒）。寫入的一方 MUST 在送出段檔之前把它們的 mtime 設成現在，讓這個時鐘是「上傳那一刻」而不是「切出那一刻」。

MUST NOT 刪「本輪 listing 有、但不在目前 manifest」的所有段——那會刪掉另一台剛傳上來、還沒發布 `session.md` 的段。確認成功之後 MUST 把這一版寫成新的記錄（同一個檔），下一輪才知道誰是被取代的版本；寫記錄失敗只影響下一次的前一版保護（那一輪只剩寬限期），不影響正確性。清理 MUST 用一次批次刪除；失敗 MUST 只警告。

Drive 上不再有新的整份 `raw-*`；新寫入 MUST NOT 產生它。**舊 Session 的 `raw-*.json` 不逐檔刪**：它們隨 `agora delete` 對 `sessions/<ULID>/` 的整個資料夾 purge 一起進垃圾桶（與 `session.md` 同進退）；本人決定保留的舊 Session 原樣留著（見「不是 `chunks/1` 的 Session 不猜、不刪」）。

#### Scenario: 別台剛傳的段不被刪
- **WHEN** A 把新段傳上 Drive、還沒傳 `session.md`；B 在同一個資料夾確認成功並清理
- **THEN** A 的段還在（不在 B 的前一版 manifest，但在寬限期內）

#### Scenario: 慢速讀者握著前一版
- **WHEN** 讀者拿到 v1 的 manifest，v2、v3 相繼發布（同一台機器確認的）
- **THEN** v2 發布時 v1 的段還留著（記錄裡的前一版 manifest＋寬限期）；v3 發布後 v1 的段才會被清掉。讀者最壞是重抓一次 header，不是讀到缺段

#### Scenario: 孤兒段
- **WHEN** 一次上傳留下沒人指到的 `seg-*`（崩在 `session.md` 之前）
- **THEN** 它在寬限期內不被刪；之後任何一次成功的清理會刪掉它

### Requirement: 不是 `chunks/1` 的 Session 不猜、不刪

`agora.raw.layout` 不存在、或不是 `chunks/1` 的 Session（舊版留下的單檔 raw、別台未來的新格式），讀的一方：

- MUST NOT 猜它的 raw 格式、MUST NOT 半讀、MUST NOT 用它的 raw；
- MAY 把閱讀版建進索引（`show`／搜尋可用），但 raw MUST 標成不可用：`show --raw`、`continue`、`pull` 的下載、`edit` 都給一個明確錯誤（說明是舊格式或未知格式、要遷移）；
- MUST NOT 因為這樣就把它從索引或鏡像刪掉；MUST 在 `unfinished.json` 記成 `legacy`，下一次 sync 不再為它下載。

寫的一方 MUST 拒絕寫入這種 Session（`edit`／`continue`／`push` 都一樣），MUST NOT 自動把它升級成分段。**沒有原地轉存**：要它變成新格式只能從原檔重匯成一個新的 Session（`import … --as-new`）；舊的 MUST 原樣留著，或由本人明確 `delete`（整個資料夾進垃圾桶）。**升級前留在 outbox 的舊格式版本**（舊 header＋`raw-*.json`）MUST NOT 被上傳器發布（上傳前 MUST 檢查 `layout`），MUST 留在 outbox 並警告——否則會把舊格式寫回 Drive。**逐筆略過**：同一批的其他 entry MUST 照常上傳（MUST NOT 因為這一筆中止整批）；**警告 MUST 每個 entry 只印一次**（以 ULID 記在本機 `state/legacy-warned`，只有 id），重跑或下一輪 MUST NOT 再印。

**一般的 `agora import`（沒有 `--as-new`）也算寫的一方**：同一個來源最新的一筆（`Index.by_source` 的第一筆，就是會被更新的那一個）不是 `chunks/1` 時，MUST 拒絕（exit 非 0），訊息指向 `--as-new`（另存重匯）。MUST NOT 用「舊 raw 讀不到就不做『內容不能變少』比對」的 fail-open 更新它（那等於靜靜原地升級），MUST NOT 走分支路徑建一個 `parents` 指到它的新 Session（那會讓舊的因為「有子 Session」而永遠刪不掉）。同來源最新的一筆已經是 `chunks/1` 時，照常更新它。

#### Scenario: 舊的單檔 Session
- **WHEN** 一個 Session 的 header 沒有 `layout`，只有舊的 `raw.file`／`md5`／`size`
- **THEN** sync 建索引、`agora show session <id>` 讀得到閱讀版；`show --raw` 明確報錯說要遷移；鏡像與索引都還在；下一次 sync 不再下載

#### Scenario: 未知的 layout
- **WHEN** `agora.raw.layout` 是 `chunks/9`（這個版本不認得）
- **THEN** 同一個處理：不猜、不半讀、不刪；raw 不可用、閱讀版可讀

#### Scenario: 寫入一個舊格式的 Session
- **WHEN** 對一個沒有 `layout` 的 Session 執行 `agora continue` 或 `agora edit`
- **THEN** 在抓 raw 之前就明確拒絕（exit 非 0），訊息說明要用 `agora import … --as-new` 另存重匯（沒有原地轉存）；Drive 與本機都不動

#### Scenario: 一般 import 遇到舊格式
- **WHEN** 對一個沒有 `layout` 的 Session 執行 `agora import session …`（同來源、沒有 `--as-new`）
- **THEN** 拒絕（exit 非 0），訊息說明要用 `--as-new`；舊 Session 沒有被升級、也沒有產生指向它的子 Session；Drive 與本機都不動

#### Scenario: 升級前留在 outbox 的舊格式版本
- **WHEN** 升級到含 R3 的版本時，`outbox/` 裡還有一筆升級前 stage 的舊格式版本（沒有 `layout`、有舊 `raw.file`），同一批還有另一筆新格式 entry
- **THEN** 上傳器不上傳它、不發布它的 `session.md`（Drive 原樣不動），留在 outbox 並警告；**同一批的另一筆照常上傳**（逐筆略過，不是整批中止）；警告只印一次，重跑不再印；它留在 outbox 直到舊 Session 被 `delete`（本機 entry 清掉）或本人明確放棄——重匯成新 Session（`--as-new`）不會動它

### Requirement: 匯入大 Session 不把整份 raw 放進記憶體

`agora import` MUST 用串流的方式產生 raw，**記憶體峰值 MUST NOT 隨「不進閱讀版的部分」（例如工具輸出）成長**。閱讀版、sidecar、最大單行是固定形狀的界限（測試用固定閱讀版大小的假 session 量測），不是「與 raw 大小無關」：

- **claude**：MUST 單趟串流打包（讀一行、驗一行、寫一行；只緩衝最後一行），峰值只剩「最大單行×2＋sidecar＋閱讀版＋固定開銷」；
- **opencode**：MUST NOT 再保留一份與 raw 同大小的 `Exported.raw`（匯出檔就是 raw，事實摘要從檔案解析）；峰值仍受 `json.load` 的 O(匯出) 限制，這是已知界限；
- **agy**：raw MUST 維持串流打包（db 分塊讀、base64 以 3 的倍數寫）；剩下的 `scan_text` 是 O(文字)，這是已知界限。

**第一次上傳就是分段**：匯入的第一版 MUST 是 `chunks/1`（`ceil(main_end/C)` 段＋tail），MUST NOT 先傳一份整份 raw。上傳中斷時，下一次 MUST 只補 listing 缺的段（不必重讀原檔）；在 `stage` 之前中斷時，Drive MUST 什麼都沒有，重跑匯入即可。同一份內容重跑匯入 MUST 是 no-op（不重傳）。

#### Scenario: 匯入一個 814 MB 的 claude session
- **WHEN** 一個 814 MB 的 claude jsonl 被 `agora import session --agent claude --external-session-id <id>` 匯入
- **THEN** 峰值記憶體不隨工具輸出的量成長（800 MB 的峰值 ≤ 1.2 × 100 MB 的峰值，且 ≤ 256 MiB；三份假 session 的閱讀版大小固定）；Drive 上約 196 個段、沒有整份 raw；`show --raw` 接回來等於原檔
- **AND** 量測記錄 100／400／800 MB 的實際秒數（不設門檻，但要有數字）

#### Scenario: 匯入中途斷線
- **WHEN** 第一次上傳傳了 30 個段就斷了
- **THEN** entry 留在 outbox；下一輪只送 Drive 缺的段與 `session.md`，不重讀原檔、不重傳已上去的段

#### Scenario: 匯入前行程被殺
- **WHEN** `agora import` 在 `stage` 之前被 SIGKILL
- **THEN** Drive 上什麼都沒有；重跑同一個指令匯入成功；不會留下半個 Session

#### Scenario: opencode 的大匯出
- **WHEN** 一個很大的 opencode session 被匯入
- **THEN** 不再有第二份整份 bytes；峰值受 `json.load` 限制（已知）；第一次上傳仍是分段

#### Scenario: agy 的匯入
- **WHEN** 一個 agy conversation 被匯入
- **THEN** raw 仍是串流打包（與今天相同）；第一次上傳是分段；`scan_text` 的 O(文字) 是已知界限

### Requirement: 重匯既有來源時另存（`--as-new`）

`agora import` 的 `--as-new` 開關 MUST 讓重匯在「同一個來源已經有一個**舊格式** Session」時仍然建立一個**新的** Session，而不是更新舊的：

- 一般 import 會用 `agent`＋`session_id` 找同一個來源，找到就當成同一個 Session（內容沒變是 no-op、內容變了要比「不能變少」）；搬遷時舊 Session 還在本機索引裡，所以 MUST 用 `--as-new` 才不會靜靜地什麼都沒做。
- `--as-new` MUST 忽略同來源的舊格式 Session；若已經有同來源的 `chunks/1` Session，MUST 回它（重跑是 no-op；`agora migrate inventory` 的 `replacement` 用同一個查詢，但它另外要求那一版已在 Drive 上且內容相同，見「搬遷盤點」）；否則 MUST 建立新 Session（`relation: import`），MUST NOT 設 `parents`（否則舊 Session 會因為「有子 Session」而刪不掉）。
- `--as-new` MUST NOT 讀舊 Session 的 raw、MUST NOT 走「內容不能變少」的檢查；舊 Session 的 Drive 與本機內容 MUST 不被改動。

#### Scenario: 舊 Session 還在本機索引裡
- **WHEN** 一個 claude 來源已經匯入過（舊格式的 Session A 還在索引），執行 `agora import session --agent claude --external-session-id <id> --as-new`
- **THEN** 建立一個新的 Session B（`chunks/1`）；A 原樣不動；B 的 `relation` 是 `import`、沒有 `parents`

#### Scenario: 重跑
- **WHEN** 同一個來源再跑一次 `--as-new`（B 已經存在）
- **THEN** 回 B、不建立第二個；不重傳

#### Scenario: 一般 import 不變
- **WHEN** 不加 `--as-new` 重匯一個已經存在的來源，而**會被更新的那一個**（同來源最新的一筆）是 `chunks/1`
- **THEN** 行為與以前完全相同（內容沒變印出同一個 id、內容變了走原本的規則）；目標是舊格式時的拒絕見「不是 `chunks/1` 的 Session 不猜、不刪」

### Requirement: 搬遷盤點只讀 metadata

`agora migrate inventory` MUST 只讀 metadata，MUST NOT 讀或印對話內容：

- 讀本機索引的 `sessions` 表（`ulid`、`agent`、`source_id`、那一版 `session.md` 的 `md5`、header 的 `agora` 區塊；**MUST NOT SELECT `body`**，而且 `agora` 區塊 MUST 用 `json_extract` 只取需要的欄位（`$.agora.source`／`$.agora.relation`／`$.agora.raw.md5`／`.size`／`.layout`／`.segments`（只留檔名／md5／size），lineage 另用 `$.agora.parents`），**MUST NOT 把整個 `header`（含 `title`／`description`／`tags`）載進記憶體**；**MUST NOT 讀 `$.agora.raw.file`**——沒有任何程式需要舊 raw 的檔名）；
- 讀 Drive 的 listing（只有檔名、大小、md5）；
- 查本機 agent store 的**存在性**（claude 的 `find_jsonl`、opencode 的 `list_sessions` id 集合、agy 的 `db_path().is_file()`）；
- 讀**本機 outbox** 的 entry（唯讀的 `store.outbox_folders`：一般 entry、`.done-` 的 set-aside，以及下次指令會放回的 `.old-<ULID>`；與 `outbox_ulids`／`waiting_ulids` 同一套規則，但不 rename；不碰上傳鎖）與每一筆 `session.md` **header** 的 `$.agora.raw.layout`（判 `outbox_legacy`；不讀 `raw.file`、不輸出 body）；
- MUST NOT 下載任何 `session.md`（那是閱讀版）、MUST NOT 開 agent 的原始檔或 db 內容；**MUST NOT 觸發 sync**：MUST NOT 呼叫 `store.sync`（它會下載有變的 `session.md`、改索引與 `cloud_missing`、並啟動上傳器），MUST NOT 啟動上傳器；只列 Drive、不下載、不更新鏡像、不上傳。
- **前提是索引已經重建完成**：`Index` 開啟時若 `PRAGMA user_version` 不符，會清表並從鏡像的 `*/session.md` 重建（本機快取，但會讀 body）。所以升級後 MUST 先跑一次正常指令（例如 `agora sync`）讓重建完成，再跑 `inventory`；`inventory` 本身 MUST NOT 觸發或依賴這次重建——索引版本不符時它 MUST 明確報錯、要求先跑一次正常指令，而不是順手重建。這樣它才真的維持「只讀 metadata、不讀內容」。
- **必保清單**：`agora migrate inventory --must-keep <file>` MUST 接受一個 **repo 外**的本機檔（一行一個 `agora:` id 或裸 ULID，`#` 開頭是註解、空行忽略；檔案 MUST 只有本人可讀，`0600`，用完由本人刪除）；清單的內容 MUST NOT 寫進 repo、MUST NOT 上傳、MUST NOT 被複製進 `state`／log，輸出只有布林標記、算好的 `drop_list` 與 `must_keep_deleted`（只有 id）。**輸入端 MUST fail-closed**：（a）檔案開不了（不存在／權限／是目錄）或不是 UTF-8 → MUST 明確錯誤、exit 非 0、**不輸出任何候選**；（b）有給旗標但正規化、去註解與空行之後**解析出 0 個 id** → MUST 明確錯誤、exit 非 0、不給候選（空清單不等於沒有清單）；（c）**任何一行**不是可辨認的 id（`agora:<ULID>` 或裸 ULID；26 碼、Crockford 大寫字元）→ MUST 明確錯誤、exit 非 0、不給候選，而且**只列出行號、MUST NOT 重印那一行的內容**；（d）空白／CRLF／BOM MUST 先正規化再判斷，重複的 id（含 `agora:` 與裸寫法指到同一個）MUST 只算一次；大小寫 MUST NOT 自動轉換。清單裡的 id 在本機索引與 Drive listing 都找不到時：若在本機 `state/deleted` 墓碑裡（裸 ULID；比對前先正規化）MUST 標成已刪（不算錯；放在頂層 `must_keep_deleted`，只有 id，不在 `drop_list`）；否則 MUST 以明確錯誤結束（exit 非 0）、列出那些 id，而且那一輪 MUST NOT 給任何候選（打錯字不能靜靜地少保護一條）。**錯誤訊息 MUST NOT 含清單任何一行的內容**（只有路徑、行號、認得的 id 與原因）。

輸出 MUST 只有形狀：`host`、`drop_list`（只有 id）、`must_keep_deleted`（清單裡已在 `state/deleted` 墓碑的 id，只有 id；沒有給清單時是空陣列）、每一類的數量、以及每一筆的 `ulid`／`agent`／`external_id`／`host`／`relation`／`layout`／`raw_size`／`class`／`replacement`／`old_children`（`parents` 指到這一筆的其他舊 Session 的 id）／`lineage`（同一條 lineage 的連通集合，含自己；算的是索引內的 Session（含新格式；成員裡有新格式的，整條都不可丟），只有 id）／`must_keep`（這一筆本身在必保清單裡）／`lineage_protected`（同一條 lineage 有任何一筆在清單裡）／`continuing`（這台正在 `continue` 它；`store.continuing_details` 非 `None`，只讀 pending 記錄與它的鎖）／`outbox_legacy`（本機 outbox 有這一筆的舊格式 entry）／`droppable`（這一筆可不可以丟）／`not_droppable_reason`（不可以丟的原因）；MUST NOT 含 `title`／`description`／`tags`／`body`，MUST NOT 重印必保清單的內容。`old_children` MUST 用只讀 id 的查詢算（`json_each(header, '$.agora.parents')` 找 `agora:<ULID>` 再排除 `cloud_missing`），MUST NOT 用 `Index.children`——它會把每個 header 解析一次，違反上面「不把整個 header 載進來」；`lineage` MUST 由同一批 `$.agora.parents` 的 id 在記憶體算，不另外查、不解析 header。分類 MUST 至少包含：`local-present`、`local-missing`、`other-machine`、`host-unknown`、`no-source`、`agent-unavailable`、`drive-only`、`new-format`。

`outbox_legacy` MUST 是「這個 ULID 在**本機 outbox** 裡有一個 `layout` 不是 `chunks/1` 的 entry」（用唯讀的 `store.outbox_folders` 列 entry，含 `.done-` 與可放回的 `.old-<ULID>`；只讀那一份本機 `session.md` 的 header；不讀 `raw.file`、不下載、不輸出 body）。它只影響 `droppable`／`not_droppable_reason`：`droppable` MUST 是 `false`：那個未上傳的版本會隨 `delete` 一起清掉、永遠不會上線，只有本人明確放棄才丟。

`droppable` MUST 恰在以下全部成立時是 `true`，否則是 `false`；**同一條 lineage 的所有成員 MUST 同值**（丟掉的單位是整條）：(1) `class` 是 `local-present`；(2) `continuing` 是 `false`；(3) `replacement.relation_to_old` 是 `identical`；(4) `outbox_legacy` 是 `false`；(5) 同一條 lineage 的**每一筆**成員都成立 (1)–(4)；(6) 這一輪沒有未知 id 錯誤（有就整輪不輸出）。`not_droppable_reason` 在 `droppable: true` 時 MUST 是 `null`；否則 MUST 是恰一個，依這個順序取第一個成立的：`class-not-local-present`／`continuing`／`no-replacement`／`relation-null`／`relation-different`／`outbox-legacy`／`lineage-member-not-droppable`（自己 (1)–(4) 都成立，但同一條 lineage 有另一筆不成立）。

`drop_list` MUST 是 `sessions` 裡所有 `droppable: true` 的 `ulid`（升冪、不重複，只有 id），而且 MUST 是**完整 lineage 的聯集**——MUST NOT 出現同一條 lineage 只有一半在裡面。搬遷第 4 步 MUST 只把 `drop_list` 原樣交給 `delete`（不是人看 JSON 手算）；`different`／`outbox_legacy` 的人工例外 MUST NOT 進 `drop_list`。有未知 id 錯誤時，指令 MUST exit 非 0，而且 MUST NOT 輸出 `sessions`／`drop_list`（fail-closed，連同上面（a）–（c）的輸入錯誤）。

`replacement` 沒有替代版本時是 `null`，否則 MUST 是物件 `{ulid, relation_to_old, raw_size, reason}`：

- `ulid`：同一個 agent session id、`layout: chunks/1` 的 Session（`Index.by_source`）；多個時，任何一個 `identical` 就回報那一個，否則回報最新的一個。
- `raw_size`：替代版本整份 raw 的大小（外層那一筆的 `raw_size` 是舊的）。
- `relation_to_old` MUST 是 `identical`／`different`／`null`：**`identical` 恰在三條都成立時**——(a) 兩邊 header 的整份 `raw.md5` 相同（`raw.size` 也相同）；(b) **替代版本**就是 Drive 上那一份（索引裡那一版的 `session.md` md5 等於 listing 的 `session.md` md5），**而且那一版列的每一個段都在 listing**（`raw_complete`：檔名是 basename、`size` 加起來等於 `raw.size`、每個段的 md5 相符）；(c) **舊的那一筆**也還是 Drive 上那一份（索引裡舊 ULID 的 md5 等於 listing 的）。`md5` 不同、任一邊的索引比 Drive 舊、舊的已經不在 Drive、替代版本還沒上傳完、或替代版本少了任何一個段，都是 `different`；兩邊任何一邊沒有 `raw.md5`（例如沒有 raw 的 Session）是 `null`（無法比較，不是相同）。
- `reason`：`md5-equal`／`md5-differs`／`drive-version-elsewhere`（替代版本還沒上傳完或 Drive 上是別的版本）／`segments-missing`（替代版本的 `session.md` 在，但它列的段有缺；別台把它們當孤兒清掉了）／`old-drive-version-elsewhere`（本機對舊版的認知過期）／`old-not-on-drive`（別台已經刪了；本機鏡像可能是最後一份）／`old-has-no-raw`／`new-has-no-raw`／`no-old-md5`，給人看的診斷。
- **`droppable` 以 `identical` 為第一條件**；`different` 與 `null` MUST NOT 進 `drop_list`（要本人逐筆決定）。這個判準不讀內容：重匯同一份沒變的原檔時，新的分段版本接起來的位元組等於舊的單檔 raw，兩邊的 `raw.md5` 才會相同。

**必保清單的保護規則（全部 A 的搬遷）**：

- 清單內的 Session 及其**整條 lineage**（`lineage`）MUST 在重匯後**逐一**核對 `identical`（上面三條）才可進 `drop_list`；結果 MUST 由程式寫成每一筆的 `droppable`／`not_droppable_reason`，不是人看 JSON 推論。
- 任何一筆不是 `identical`（`different`／`null`／沒有原檔）、或那一筆沒有原始紀錄 → **整條 lineage MUST `droppable: false`、MUST NOT 進 `drop_list`**，MUST 列出來交本人；MUST NOT 只丟同一條裡剛好 `identical` 的其他成員。
- 清單內的 Session 正在被 `continue` 接續（`continuing: true`）→ MUST NOT 重匯、MUST NOT 丟（整條 lineage 都 `droppable: false`），MUST 列出來等它結束再跑。
- `identical` 只保證 raw 的位元組，不保證閱讀版 body 沒被手改（本指令不讀內容）。要保留手改過的 body，MUST 由本人決定整條不丟：把那一整條 lineage 從 `drop_list` **整條移除**（MUST NOT 只移除一半），記在搬遷紀錄；必保清單本身不是「永不丟」名單。
- 沒有原始紀錄的類別 MUST NOT 進 `drop_list`（`droppable: false`、`not_droppable_reason: class-not-local-present`）；由 `inventory` 列出、本人逐筆決定。

#### Scenario: 必保清單內一筆不同
- **WHEN** 必保清單裡的 X 重匯後是 `different`（原檔變短），同一條 lineage 的其他成員是 `identical`
- **THEN** `inventory` 標出 X 的 `must_keep` 與整條的 `lineage_protected`；X 是 `droppable: false`（`not_droppable_reason: relation-different`），其他成員也是 `droppable: false`（`lineage-member-not-droppable`）；**`drop_list` 不含這一整條的任何 id**；本人看過再決定

#### Scenario: 必保清單內正在接續
- **WHEN** 必保清單裡的 X 正在被 `agora continue` 接續
- **THEN** `inventory` 標 `continuing: true` 並把 X 列出來；X 與同一條 lineage 的每一筆都是 `droppable: false`、不在 `drop_list`；搬遷步驟 MUST 跳過 X（不重匯、不丟），等它結束再跑；其他 id 照做

#### Scenario: 必保清單的打字錯誤
- **WHEN** `--must-keep` 的檔案裡有一個 id 不在本機索引、不在 Drive listing，也不在本機的 `state/deleted`
- **THEN** `inventory` 以明確錯誤結束（exit 非 0）、列出那個 id，且**不輸出 `sessions`／`drop_list`**（沒有任何候選）；修正清單後重跑

#### Scenario: 必保清單的 id 已經刪掉
- **WHEN** 清單裡的 id 在 `state/deleted` 墓碑裡（別台或先前跑過已經刪了）
- **THEN** 它被標成已刪、不算錯：出現在 `must_keep_deleted`（只有 id）、不在 `sessions`／`drop_list`；指令正常輸出其他筆

#### Scenario: 必保清單的檔案讀不到
- **WHEN** `--must-keep` 指到的路徑不存在（或沒有讀取權限、是目錄、不是 UTF-8）
- **THEN** `inventory` MUST 以明確錯誤結束（exit 非 0）、訊息只有路徑與原因；MUST NOT 輸出 `sessions`／`drop_list`、MUST NOT 當成「沒有必保清單」照常給候選

#### Scenario: 必保清單是空的
- **WHEN** 有給 `--must-keep`，但檔案只有註解與空行（或整份是空白），解析出 0 個 id
- **THEN** `inventory` MUST 明確錯誤（exit 非 0）、說明「真的不要就不要給這個旗標」；MUST NOT 輸出任何候選

#### Scenario: 必保清單有一行不認得
- **WHEN** 清單檔的某一行不是 `agora:<ULID>` 也不是裸 ULID（小寫 ULID、全形字、`claude:`／`session:` 前綴、多餘字元）
- **THEN** `inventory` MUST 以明確錯誤結束（exit 非 0）、**只列出行號**（一次列完所有不認得的行）、**MUST NOT 重印那一行的內容**；MUST NOT 輸出任何候選

#### Scenario: 清單的正規化與重複
- **WHEN** 清單檔是 UTF-8 BOM、CRLF 換行，某個 id 前後有空白，同一筆同時以 `agora:<ULID>` 與裸 ULID 各寫一次
- **THEN** BOM／CRLF／空白先被正規化；兩行只算同一個 id；輸出的 `must_keep` 只標一次，清單內容不出現在 stdout／stderr／`state`

#### Scenario: 盤點的分類
- **WHEN** 本機索引有四個舊 Session：一個原檔還在、一個 `source.host` 是別台、一個是 merge（沒有 `source`）、一個只在 Drive 上
- **THEN** 輸出分成 `local-present`、`other-machine`、`no-source`、`drive-only`；每筆有 `replacement`（沒有就是 `null`）；沒有任何 title／body

#### Scenario: 卡在 outbox 的舊格式 entry
- **WHEN** 升級前有一筆舊格式版本還留在本機 outbox（沒有 `layout`、有舊 `raw.file`），它對應的舊 Session 也在盤點裡
- **THEN** 那一筆是 `outbox_legacy: true`、`droppable: false`（`not_droppable_reason: outbox-legacy`）、不在 `drop_list`；`inventory` 沒有上傳、沒有改 outbox、沒有啟動上傳器

#### Scenario: 不讀對話、也不 sync
- **WHEN** 執行 `agora migrate inventory --json`
- **THEN** 它沒有下載任何 `session.md`、沒有讀任何 raw、沒有啟動上傳器；fake rclone 的 `calls.log` 沒有任何 `copy`／`copyto`（只有 listing）；輸出的 key 集合不含 `title`／`description`／`body`

#### Scenario: 索引還沒從版本升級重建
- **WHEN** 升級到含 R3 的版本後，還沒跑過任何正常指令，就直接執行 `agora migrate inventory --json`（索引的 `PRAGMA user_version` 不符）
- **THEN** 它以明確的錯誤結束、要求先跑一次正常指令（例如 `agora sync`），**不**在本指令裡觸發重建（那會讀鏡像裡的 `session.md`）；`calls.log` 仍然沒有任何 `copy`／`copyto`，也沒有讀任何 `session.md`
- **AND** 先跑過一次正常指令、讓 `Index` 從鏡像重建完成之後，同一條指令才正常輸出

#### Scenario: 原檔變短後重匯
- **WHEN** 一個舊 Session 的原檔在匯入後被截短（或 `/compact` 改寫），用 `--as-new` 重匯得到一個較短的新 Session
- **THEN** `replacement.relation_to_old` 是 `different`（`reason: md5-differs`）、`replacement.raw_size` 比舊的 `raw_size` 小；這一筆是 `droppable: false`（`not_droppable_reason: relation-different`）、不在 `drop_list` 裡；舊 Session 要本人逐筆決定才能刪

#### Scenario: 替代版本還沒上傳完
- **WHEN** `--as-new` 剛建立新 Session，它還在 outbox，Drive 上還沒有它的 `session.md`
- **THEN** `replacement` 出現但 `relation_to_old` 是 `different`（`reason: drive-version-elsewhere`），`droppable: false`、不在 `drop_list` 裡；等上傳成功後再跑一次 `inventory` 才是 `identical`

#### Scenario: 替代版本少了一段
- **WHEN** 替代版本的 `session.md` 已在 Drive 上、md5 也與索引相同，但它列的某一個段被別台當孤兒清掉了（A.3）
- **THEN** `relation_to_old` 是 `different`（`reason: segments-missing`）——listing 裡少那一段，`raw_complete` 不成立；`droppable: false`、不在 `drop_list` 裡（`session.md` 在，不代表讀得回來）

#### Scenario: 本機對舊版的認知過期
- **WHEN** 另一台還在跑舊版，把舊 Session 改了並發布（Drive 上舊 ULID 的 `session.md` md5 與本機索引不同）
- **THEN** `relation_to_old` 是 `different`（`reason: old-drive-version-elsewhere`）；不拿本機的 `raw.md5` 當等價證明，這一筆 `droppable: false`（`not_droppable_reason: relation-different`）

#### Scenario: 舊的已經被別台刪了
- **WHEN** 舊 Session 已經不在 Drive 的 listing 裡（別台刪掉了），本機鏡像還在
- **THEN** `relation_to_old` 是 `different`（`reason: old-not-on-drive`）、`droppable: false`、不在 `drop_list`；本機鏡像可能是最後一份，要不要丟或 `push --not-exist-upload` 由本人決定

#### Scenario: 已經是新格式
- **WHEN** 一個 Session 已經是 `chunks/1`
- **THEN** 它被標成 `new-format`、`droppable: false`（`not_droppable_reason: class-not-local-present`），不會出現在 `drop_list` 裡


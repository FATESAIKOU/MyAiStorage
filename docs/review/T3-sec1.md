> **⚠ 會影響第 1 節（骨架，impl2 正在做）的有三件，請先看：**
> - **N1**：`AGORA_UPLOAD=inline` 時，上傳**失敗**要回 exit 3 嗎？刪除佇列要不要也在 inline 時處理？現在沒有寫，但既有的測試（例如 `test_upload_failure_exits_3_and_stays_searchable`）靠的就是「上傳失敗 → 3」。1.2 的 helper 要能把「失敗了」回報給呼叫端。
> - **N2**：`push` 等鎖「60 秒逾時」，可是 spec 寫的是「等到傳完才結束」。而被限流的時候，一次 rclone 就要 50 秒，一輪是 4 次。這兩邊矛盾，1.1 的鎖 API（阻塞、逾時）要先定下來。
> - **N3**：「背景上傳失敗之後，下一個指令再試」的那一次，是 sync **在前景**上傳（現在 design 的寫法），還是由那個指令**啟動背景**？如果是前景，背景失敗之後的第一個 search 又會慢下來。這決定 1.1、1.2 要不要讓「指令開頭的 sync」也透過 helper 啟動背景。

**沒有 High。** T3.md 的 H1、M1～M7、L1～L7 都有處理到。但改寫之後，有兩條新的規則彼此衝突（N4、N5，Medium，在第 2 節），另外還有幾個要寫清楚的地方。

# Review：local-first-writes 依 T3 改寫之後（規劃文件）

2026-10-03，review。對象：`f6064a2` 的 proposal、design、`specs/local-first-writes/spec.md`、`specs/batch-commands/spec.md`（MODIFIED delta）、tasks。只看規劃文件，沒有看程式，沒有跑任何東西。

## T3.md 的每一項

| 項目 | 處理 | 結果 |
|---|---|---|
| H1：只刪自己驗過的版本 | design「只刪自己驗過的版本」：這一輪的 md5、改名成 `.done-`、比對、相符才刪，否則改名回去；`stage` 遇到 `.done-` 時的處理；spec 新增「只有自己驗過的版本離開 outbox」，以及 Scenario「上傳中又改了同一個」；tasks 2.3、2.6 有測試 | ✅（細節見 N6、N7） |
| M1：一把鎖 | design「一把鎖，前景背景共用」：背景、sync、push 都拿同一把；sync 用非阻塞，push 用阻塞；spec 寫明「不論前景或背景」，以及 Scenario「push 等背景」 | ✅（但見 N2、N3） |
| M2：exit code | design「exit code」；spec「背景上傳」的開頭寫了；batch-commands 的 delta；tasks 4.1 不再用 `code == 3` 判斷 | ✅（inline 的情況見 N1） |
| M3：獨立入口 | `agora.background`，不經過 `main()` | ✅ |
| M4：原始檔那一次部分失敗 | 原始檔那一次不是 exit 0，整輪就不傳 session.md；spec 的 Scenario「原始檔那一次失敗」 | ✅ |
| M5：佇列裡的不能 pull／push | spec「delete 先在本機」；tasks 3.3 | ✅ |
| M6：MODIFIED delta | 新增了 `specs/batch-commands/spec.md`；proposal 的 Modified Capabilities 也寫上了 | ✅（措辭見 N9） |
| M7：分工 | 第 1 節是骨架（impl2 先做，留一個空的 `process_trash_queue`）；sync 跳過佇列（2.4）改給 impl2；impl1 等第 1 節做完才開始 | ✅（剩下的重疊見 N10） |
| L1：前景仍然會連 Drive 的 | design 有一節；spec「背景上傳」的最後一段 | ✅ |
| L2：edit、`recover_pending` | spec 和 tasks 2.5 都寫上了 | ✅ |
| L3：改完馬上刪 | delete 會拿掉 outbox 裡的那一筆；spec 的 Scenario | ✅ |
| L4：佇列的提醒 | spec、tasks 3.3 | ✅ |
| L5：「背景上傳中，N 筆」 | design、spec | ✅ |
| L6：雲端沒有的不進佇列 | design、spec | ✅ |
| L7：更新既有 id 前確認還在 | design 一節；spec 新增的那一段，以及 Scenario「別台在上傳前刪掉了」 | ⚠️ 見 **N4、N5** |

## 新的問題

### N1（影響第 1 節）：inline 模式失敗時的 exit code

design 說「`AGORA_UPLOAD=inline` 時，在前景同步跑同一個函式。既有的單元測試照常假設『指令結束時 Drive 上已經有了』」。但沒有說 inline 的時候上傳**失敗**要怎麼辦。既有的測試裡，有一些就是測這個的：`test_upload_failure_exits_3_and_stays_searchable`、`test_finalize_upload_failure_keeps_outbox_clears_pending`，都期待 exit 3 和「已存入 outbox」的訊息。

**建議**：在 design 裡寫明「inline 時上傳失敗，回 exit 3（和以前一樣），訊息照舊」；並且說清楚 inline 時，刪除佇列也在前景處理（不然 delete 的測試就看不到 Drive 上的變化）。1.2 的 helper 要回傳三種結果：「已啟動背景」「已在前景傳完」「失敗」，讓 `_emit` 能判斷。

### N2（影響第 1 節）：push 等鎖 60 秒逾時，和 spec 矛盾

- spec：「`push session <id>…` MUST 等正在跑的上傳結束，再自己送，**直到傳完才結束**」；
- design：「阻塞等鎖，60 秒逾時」，但沒有說逾時之後怎麼辦。

被限流的時候，一次 rclone 大約 50 秒，背景的一輪是 4 次，再加上刪除佇列裡每一個 purge，很容易超過 60 秒。

**建議**（二選一，寫進 spec 和 design）：
- (a) 不設逾時，一直等，每 10 秒在 stderr 說一次「背景上傳中，還在等…」；
- (b) 逾時之後，push 不自己送，而是說「X 已經在 outbox，背景會送出；要確定的話稍後再跑一次」，exit 3。spec 也要改成這個說法。

另外，push 其實要等的是「**X 離開 outbox**」，不是「背景**整個**結束」：背景可能還要處理一長串的刪除佇列，跟 X 一點關係都沒有。可以考慮讓 push 等到 X 不在 outbox（而且 Drive 上的 md5 對了）就結束。

### N3（影響第 1 節）：背景失敗之後，誰來補傳

design 的寫法是：sync 拿到鎖的時候，就在前景用批次上傳把 outbox 送掉。spec 也說：「背景上傳失敗時……下一個會連 Drive 的指令 MUST 再試」。

結果是：背景失敗（例如離線）之後，下一個 `search session` 只要沒有被節流擋下，就會在**前景**上傳整個 outbox，又是好幾次 rclone，T3 想解決的「站在那裡等」又回來了。

**建議**：指令開頭的 sync **不在前景上傳**，outbox 不是空的時候，改成透過 1.2 的 helper 啟動背景（拿不到鎖就代表已經有一個在跑，什麼都不做）。只有 `push` 會在前景送。這樣 spec 的那一句也要改成「下一個會連 Drive 的指令 MUST 啟動背景再試」。

### N4（Medium，第 2 節）：「更新既有 id」的判斷，會把**每一筆**都算進去

design：「標頭的 `agora.previous_sources` 有值，**或索引裡本來就有這個 ULID**，代表這是更新既有 id」。

可是 `_save` 會先 `stage`、再 `remember`，**所有**寫出的 Session，包含全新的 import，在背景看到它們的時候，**都已經在索引裡了**。所以照這條規則，全新的 Session 也會被當成「更新既有 id」。而它們本來就還不在 Drive 上，所以會被判成「別台刪掉了」，**永遠不會被傳上去**。

`previous_sources` 也不可靠：只有 continue 在 source 換掉的時候才會寫；edit、import 的原地更新都不會寫。

**建議**：「是不是更新既有 id」要在**前景寫入的那一刻**決定，並記在 outbox 項目裡，例如 `outbox/<ULID>/.update`（空檔）。`_save` 寫回一個**原本就存在**的 id 時（continue 寫回、edit、import 的原地更新、`recover_pending` 寫回），就寫上這個記號；全新的 id（import 新建、merge、continue 另存的 Y）不寫。背景只看這個記號。另外，`stage` 換掉整個資料夾的時候，要保留這個記號（新的版本也是更新）。

### N5（Medium，第 2 節）：L7 留下來的那一筆，兩個「選擇」都走不通

L7 的規則：更新既有 id，但 Drive 上已經沒有了 → 不傳、留在 outbox，「提醒時用 `cloud_lost` 的說法」，也就是提示 `push … --not-exist-upload` 或 `pull … --not-exist-delete`。可是：

- 這一筆在 **outbox** 裡，所以 `pull X --not-exist-delete` 會說「還沒上傳，不能刪」（session-sync 的規定：outbox 的不刪）；
- `push X --not-exist-upload` 會先送 outbox，而批次上傳又照 L7 拒絕它，結果是「還沒上傳成功，仍在 outbox」；
- 它在 outbox 裡，所以也**不會**被標成雲端沒有（outbox 的不算），雲端欄一直顯示「未上傳」。

也就是說，這一筆會**永遠**卡在 outbox，而提示的兩個選擇都沒有用。

**建議**：寫明這種情況下兩個選擇的意思。
- `push X --not-exist-upload`：**蓋過** L7，照常把 outbox 的這一筆傳上去（等於使用者決定撤銷別台的刪除）；
- `pull X --not-exist-delete`：丟掉 outbox 裡的這一筆和本機的副本（使用者決定接受刪除，放棄這次的修改）。

spec 的 L7 那一段，以及 session-sync 的「outbox 的不刪」，都要寫一個例外：「因為雲端沒有而被留在 outbox 的那一筆，`--not-exist-delete` MAY 刪掉它」。或者，另一種做法：把它當成 T1 的「接續到一半被刪掉」，**另存成一個新的 Session**，outbox 就不會卡住。這個要使用者決定。

### N6（Low）：`outbox_ulids` 還原 `.done-` 時，要先看鎖

design：「`outbox_ulids` 的啟動清理：沒有人在跑時，留下來的 `.done-<ULID>` 改名回去」。可是 `outbox_ulids` 被呼叫的地方很多（sync、互動模式的每一次重讀、每個指令開頭的提醒），所以「沒有人在跑」要用**非阻塞地試拿 `upload.lock`** 來判斷，拿得到才還原。不然，互動模式重讀的那一瞬間，剛好是背景正在比對 `.done-X` 的時候，就會互相搶（雖然結果只是多傳一次，不會掉資料）。

### N7（Low）：`.done-` 的其他交錯

- 背景要改名 `outbox/X` → `.done-X` 的時候，`X` 可能剛好不在：`stage` 正把舊的改名成 `.old-X`。這時 `rename` 會丟出 `FileNotFoundError`，要當成「被取代了」，跳過。
- 「改名回去」的時候，`X` 可能已經是新的了：在 macOS 上，把資料夾改名到一個已經存在、而且不是空的資料夾上，會失敗（ENOTEMPTY）。design 說這時「直接刪掉 `.done-`」，對；但要記得接住這個例外。
- 第 5 步「清掉被取代的舊 `raw-*`」，只能對**這一輪離開 outbox 的**那幾筆做。被新版本取代、留下來的那幾筆，Drive 上的 session.md 可能已經是新的了（第 3 步傳上去的），這時清理要等到下一輪。

### N8（Low）：「push 等背景」會連刪除佇列一起等

背景的迴圈是「處理到 outbox **與刪除佇列**都空了」才放開鎖。所以 push 等的鎖，也包含了佇列裡每一個 purge 的時間。這和 N2 一起處理（push 改成等 X 離開 outbox 就好）。

### N9（Low）：batch-commands delta 的措辭

delta 保留了原文的最後一句：「只給一個 id 時，行為與 exit code **和以前相同**」。可是以前單一個 id 上傳失敗時是 exit 3，現在（交給背景）是 0。建議改成「只給一個 id 時，行為與給多個時相同」，或者把「和以前相同」限定在「批次化之前」的意思。

### N10（Low）：tasks 剩下的重疊

| 位置 | 誰 |
|---|---|
| `cache.push`：2.3（impl2，改成用批次上傳、阻塞等鎖）和 3.3（impl1，拒絕佇列裡的） | 同一個函式，兩個人改 |
| `cli.main` 開頭的提醒：2.3（impl2，「背景上傳中，N 筆」）和 3.3（impl1，「有 N 個等著移到 Drive 垃圾桶」） | 相鄰的幾行 |

**建議**：3.3 的「push 拒絕佇列裡的」和「main 開頭的佇列提醒」，改給 impl2 在 2.3 一起做（它們用的是同一個佇列的判斷，只要一個 `store.queued_for_trash(paths)`）；impl1 只做 pull 的拒絕。

## 結論

T3.md 的每一項都有處理到，方向也對。實作第 1 節之前，請先定下 N1（inline 失敗時的 exit code）、N2（push 等多久、等什麼）、N3（補傳由誰做）這三件，因為它們決定了骨架的 API。第 2 節開始之前，要修 N4（「更新既有 id」的判斷方式）和 N5（L7 留下來的那一筆要有出路）。N6～N10 可以在實作時一起處理。

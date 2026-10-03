## 1. 批次指令（impl2）

- [x] 1.1 import：`--external-session-id` 可以多個（重複或逗號），先同步一次再逐一匯入，失敗照樣做下一個，exit code 為第一個非零（spec batch-commands「import 一次多個」）
- [x] 1.2 import、delete、merge 在 stderr 印 `k/N` 進度，stdout 只放 agora id（「進度」）
- [x] 1.3 delete：刪除成功時記到 `<state>/deleted`，重跑時只略過記錄裡的；打錯的 id 照樣找不到（「delete 重跑略過已刪除」）
- [x] 1.4 merge：要約存到 `<state>/merge-sections/`，鍵含 agent、提示詞版本、模型設定、來源 id、實際送出文字的雜湊；沿用前再驗 schema（「merge 沿用已寫好的要約」）
- [x] 1.5 單元測試：上面四項各自的情境（含中斷後重跑）
- [x] 1.6 review T1-sec1：S1-1 exit code 取第一個非零、S1-2 單一 id 的 InputError 維持 exit 1、S1-3 批次只同步一次（index 傳給 `_import_one`）；順手修 S1-6（訊息說重跑會接著做）、S1-9（快取暫存檔用唯一名）

## 2. pull／push（impl1）

- [x] 2.1 拿掉 `cache`、`sync`，新增 `pull session <id>…`、`push session <id>…`，只吃 id，id 前綴規則照 spec（session-sync「pull 只處理給的 id」）
- [x] 2.2 pull：agora id 拿 session.md 與原始檔，agent id 寫全文快取，沒過時的略過；每一個的例外都接住、照樣做下一個（review K5）
- [x] 2.3 push：先送 outbox，再逐一 copyto `session.md` 與它指到的原始檔（本機沒有就只傳 session.md）；不傳舊原始檔、`*.partial`、`.*`（「push 只寫回該寫的檔案」，K1、K2、Q7）
- [x] 2.4 全文快取的暫存檔名加唯一碼（review K4）
- [x] 2.5 單元測試：pull／push 的情境、k/N、K4、K5
- [x] 2.6 S2-2：outbox 上傳失敗的 id 不算成功（review T1-sec2）
- [x] 2.7 S2-3：只有 agent id 的 pull 不碰 Drive；Drive 的錯誤逐筆處理
- [x] 2.8 S2-4：raw 還沒齊的不建索引（G3）
- [x] 2.9 S2-5：本機和雲端都沒有的算找不到
- [x] 2.10 S2-6：補測試（沒給 id → exit 1、outbox 上傳失敗算失敗）
- [x] 2.11 S2-7：去重、push 後 md5 確認、不覆蓋 outbox 裡的

## 3. 雲端沒有（impl1，等 2.x 完成）

- [x] 3.1 索引記「雲端沒有」，與 outbox（未上傳）分開；只有列檔完整成功才更新標記；索引版本不同就重建（「雲端沒有的 Session 保留在本機並標記」，Q2、Q4）
- [x] 3.2 `pull --not-exist-delete`、`push --not-exist-upload`；都沒加時只提醒；outbox 與接續中的不刪（「只在明確要求時刪除或復活」）
- [x] 3.3 continue、edit、import 的更新路徑遇到雲端沒有的就拒絕並提示兩個選擇（「其他指令遇到雲端沒有的 Session」，Q1）
- [x] 3.4 search／show 標示、`--filter cloud=no|yes`；merge 拒絕雲端沒有的來源；delete 雲端沒有的只刪本機；`children()` 與 import 的對應不計入雲端沒有的（Q3）
- [x] 3.5 單元測試：列檔失敗不動標記、別台刪掉被標記、又出現被清除、outbox 不被標記也不被刪、continue 被拒絕、search 的 filter
- [x] 3.6 S1-4b：purge 失敗時再列檔確認，列檔也失敗就不算已刪（review T1-sec1）
- [x] 3.7 review T1-sec3 的 M1～M3 與 L4：M1 寫回前直接問 Drive（continue／edit 開始前、agent 結束後各一次；中途被刪就另存新 Session，parents 指向原本的，原本保持被刪）、M2 `--not-exist-delete` 對 agent id 先確認 agent 真的沒有了否則照常 pull、M3 索引版本不同一律從鏡像重建（不看是不是空的）、L4 補「原本的標記在列檔失敗或 sessions/ 不見時不變」與 outbox 不被標記的測試；spec 補「接續到一半被別台刪掉」的 scenario
- [x] 3.8 review T1-sec3 的 F1～F7：F1 continue／edit／`_finish` **同時**看標記與 Drive（離線不再放行已標記的 Session，補離線測試）、F2 沒有 `sessions/` 時當成不知道、F3 在 outbox 的不算雲端沒有、F4 delete 遇到接續中的拒絕、`_finish` 連「索引裡沒有」也另存新的（parents 用 pending 的 parent）、F5 agent 清單讀不到時不刪快取、F6 outbox 不被標記的測試補上「先進索引」、F7 design 5.4 第 6 步與 5.10 的句子對齊；順手補 F8（edit 存檔前再查一次）、F9（docstring 說明為什麼是完整列檔）
- [x] 3.9 review T1-sec3 第二次確認的 G1、G3、G4：G1 `continuing()` 改成真的拿 pending 記錄的 flock（拿不到才算正在接續），所以收不了尾的 pending 不再讓 delete 永遠被擋——拿得到鎖就照刪並提示「有一筆中斷的接續沒補存成功」；G3 `pull --not-exist-delete` 遇到 Drive 上沒有 `sessions/` 時拒絕那一個（那是 folder id／token 的問題，不是刪除的證據）；G4 把 F2、F3、F6、F8 的測試補到真的守得住那個修正（兩個都用 mutation 確認過：拿掉修正測試會紅）
- [x] 3.10 review T1-sec3 的 H1、H3 與 T1-final 的 (2)、(4)：H1 補「中斷的接續在**離線**時收尾」——那時只有標記能擋住 X 复活（mutation 確認）；H3 把 F8 測試的刪除動作搬進假的編輯器裡（原本在 edit 開始前就刪了，存檔前那次檢查根本沒走到）；(2) 補兩個 spec MUST 的測試「雲端沒有的不算別人的子 Session（不擋刪除）」與「**接續中**（拿著鎖）的不標記」，兩個都用 mutation 確認抓得到；(4) README 補上雲端沒有的行為、`--not-exist-delete`／`--not-exist-upload`、`--filter cloud=no\|yes`。順手修正 4.2b 裡 S1 的敘述（只有 pull 用 `mirror_one`，review T1-final D8）

## 4. 收尾（PM 驗收，review 審）

- [x] 4.1 `docs/design.md`（5.2、5.3、5.6、5.10）與 README 更新（5.10 的「雲端沒有」已照 specs/session-sync/spec.md 寫完：什麼時候更新標記、兩個 flag 的分界、其他指令遇到它怎麼拒絕、search／show 怎麼標）
- [x] 4.2 整合測試（只用 `agora-test`、自編短對話）：pull、push、import 多個，實跑通過（`tests/integration/test_import_batch.py`：import 多個與其中一個失敗；`tests/integration/test_pull_push.py`：pull 把原始檔拿回來、push 覆寫 Drive、pull agent id 進全文快取與再 pull 略過、不給 id 報錯、push 拒絕 agent id。「雲端沒有」：在 Drive 上直接 purge 模擬別台刪掉 → 保留並標記、`search --filter cloud=no|yes`、continue 被拒（exit 1、沒開 agent）、`push --not-exist-upload` 傳回去且標記清除、`pull --not-exist-delete` 刪本機、不加 flag 只提醒不動）
- [x] 4.2b `docs/review/T1-size.md` 第 2 節去重複：做了 S1（`store.mirror_one`，**目前只有 pull 用它**；sync 的迴圈仍是自己的一份，見 review T1-final D8）、S2（`_upload_checked`，兩條上傳路徑共用）、S3（=M3）、S4、S5、C1～C7、L1、L4、L5、L7，加做 L6 的一半（delete 只建一次 Drive）。**沒做**：L3（合併後行數沒省到，且會把 F1 的「標記＋Drive 兩個訊號」揉成一個）、L6 的 `delete_session(in_cloud=…)`（會動到剛被 review 過的刪除路徑）。以 review 的算法量：**cache.py 208 → 180（−28）、cli.py 725 → 726（+1）、store.py 455 → 464（+9）、三個檔案合計 1,388 → 1,370（−18 行）**；store 之所以變多，是因為 `warn`／`progress`／`write_atomic` 搬進來這裡，而 cache 與 cli 各自少了一份。沒到 review 估的 75 行，因為 F1～F8 那批新判斷就落在同一批檔案裡。全部 unit 過（387 passed）。
- [x] 4.2c review T1-size 的 D1：`push_one` 合併到 `_upload_checked` 時沒拿掉自己上傳的兩行，每次存檔每個檔都上傳兩次。改成全部交給 `_upload_checked`，兩個 `_fault`（`after-raw-upload`、`after-session-upload`）搬進它原本的位置；補「一次 `push_one` 的 copyto 數 = 檔案數」（有原始檔 2 次、沒有 1 次），拿掉修正兩個測試都會紅
- [x] 4.2d PM 用假資料實跑的 P2～P4（`docs/tickets/T1-pm-run.md`）：**P2** merge 在開始寫要約之前就把**全部**來源檢查完（第二個來源雲端沒有時，不會先替第一個花一次 AI）；**P3** pull 已經是新的算「略過」不算「拉下」，另外印「已經是新的，略過 N 個」（session.md 與它指到的原始檔都齊才算已經是新的）；**P4** merge 被中斷時提示「重跑同一個指令會沿用已寫好的要約」，continue 維持「沒存完的接續會在下一個 agora 指令自動補存」。三項都補測試，並用 mutation 確認抓得到（把檢查放回迴圈、把略過算成拉下、兩種動作用同一句提示，三個測試都紅）。**依 review T1-4.2d 精簡**：P3 原本用「事先推測」（另外寫 `_is_current` 重讀標頭比對 md5）多了 20 行，改成用 `Drive.download` 的計數器判斷「這次有沒有下載」——cache **+7**、store **+2**，合計 +9（review 的估計），而快取裡的原始檔過期時會算成「拉下」不是「略過」，比原來更精確。
- [x] 4.2e 補兩個「可以歸檔之後再處理」的測試（review T1-archive）：merge 的要約快取**鍵含 agent**（換 agent 不沿用，測試裡把模型設定固定住，讓 agent 是唯一的差異）與**鍵含提示詞版本**（改 `SUMMARY_PROMPT_VERSION` 就重寫）；以及「雲端沒有的不算子 Session」的另一半——**不觸發 import 的分岔**（唯一的子 Session 被別台刪掉之後，import 同一個來源是原地更新，不是另開一個新的）。三個都用 mutation 確認抓得到（把該項從鍵裡拿掉、把 children 的排除拿掉，測試都紅）。只加測試，沒有動 src。
- [x] 4.3 review 審程式（`docs/review/T1-archive.md`、`T1-4.2d.md`、`final-checks.md`，沒有 High，D3～D7 已改）；PM 驗收（用假資料實跑第 1～6 節，`docs/tickets/T1-pm-run.md`）。**移出這個 change、等使用者決定**：程式碼行數的額度（目標 2,900 行）與 P1（在這台寫的 Session 本機沒有原始檔），記在 `docs/tickets/README.md`；本人的真實驗收（1-5）也在那裡

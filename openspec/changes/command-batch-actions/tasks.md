## 1. 批次指令（impl2）

- [x] 1.1 import：`--external-session-id` 可以多個（重複或逗號），先同步一次再逐一匯入，失敗照樣做下一個，exit code 為第一個非零（spec batch-commands「import 一次多個」）
- [x] 1.2 import、delete、merge 在 stderr 印 `k/N` 進度，stdout 只放 agora id（「進度」）
- [x] 1.3 delete：刪除成功時記到 `<state>/deleted`，重跑時只略過記錄裡的；打錯的 id 照樣找不到（「delete 重跑略過已刪除」）
- [x] 1.4 merge：要約存到 `<state>/merge-sections/`，鍵含 agent、提示詞版本、模型設定、來源 id、實際送出文字的雜湊；沿用前再驗 schema（「merge 沿用已寫好的要約」）
- [x] 1.5 單元測試：上面四項各自的情境（含中斷後重跑）

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
- [ ] 3.2 `pull --not-exist-delete`、`push --not-exist-upload`；都沒加時只提醒；outbox 與接續中的不刪（「只在明確要求時刪除或復活」）
- [ ] 3.3 continue、edit、import 的更新路徑遇到雲端沒有的就拒絕並提示兩個選擇（「其他指令遇到雲端沒有的 Session」，Q1）
- [ ] 3.4 search／show 標示、`--filter cloud=no|yes`；merge 拒絕雲端沒有的來源；delete 雲端沒有的只刪本機；`children()` 與 import 的對應不計入雲端沒有的（Q3）
- [ ] 3.5 單元測試：列檔失敗不動標記、別台刪掉被標記、又出現被清除、outbox 不被標記也不被刪、continue 被拒絕、search 的 filter

## 4. 收尾（PM 驗收，review 審）

- [ ] 4.1 `docs/design.md`（5.2、5.3、5.6、5.10）與 README 更新
- [ ] 4.2 整合測試（只用 `agora-test`、自編短對話）：pull、push、import 多個，實跑通過
- [ ] 4.3 review 審程式；PM 驗收；程式碼行數在 2,900 行內

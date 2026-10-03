# 人工驗收結果（2026-10-03 晚上～10-04）

本人在自己的終端機，照 `docs/acceptance.md` 一步一步做，PM 用提問工具帶。HEAD `c5142ae` 之後的版本（`uv tool install --force --editable .`）。Drive 是本人自己的 OAuth client，只用 `agora-test` 與 `/tmp/agora-acc`。自編的 opencode 對話 s1～s10 由 PM 用 `opencode run` 在 `/tmp/agora-acc/proj` 造。大 Session 由 PM 用 `grow.py` 加長本機鏡像與全文快取。

## 結果

| 節 | 內容 | 結果 |
|---|---|---|
| 0～1 | 換成這一版、準備 | ✅ |
| 2 | import 一次多個、背景上傳、本機完整的一份、重跑同 id、其中一個失敗 exit 2 | ✅（2.4 因為接了管線看不到 exit code，略過） |
| 3 | pull／push 的各種情況 | ✅ |
| 4 | continue 寫回同一個、只留新的原始檔、打開就離開不存 | ✅ |
| 5 | import 完馬上 edit 不遺失 | ✅ |
| 6 | delete 先在本機消失、背景移到垃圾桶、重跑略過、打錯的 id | ✅ |
| 7 | merge 的結果、寫要約的 opencode 對話不留下 | ✅；7.2（中途 Ctrl-C）自編對話太短、來不及按，跳過（PM 10-03 用假資料實測過：exit 130、重跑沿用） |
| 8 | 被別台刪掉：保留並標記、continue／edit／merge 拒絕、不用先 pull 就救回、接受刪除 | ✅ |
| 9 | 改到一半被別台刪掉：edit 與 continue 都另存成新的 Session，relation 與 parents 正確 | ✅ |
| 10 | push 等背景、可以 Ctrl-C | ✅ |
| 11 | 刪除排隊時的提醒、不能 pull | ✅ |
| 12 | 互動模式：兩頁、按鍵列、匯入、`a`、篩選掉的勾選不算、delete 預設取消 | ✅（中文篩選見 F3） |
| 13 | 合併、Esc 中斷、勾選保留、沿用、ctrl+q 先中斷、只勾一列的提示 | ✅ |
| 14 | pull 的勾選框與說明、push 預設取消、✗、不能接續、兩種刪除的說法 | ✅ |
| 15 | 「未上傳」看得到，上傳後變 ✓ | ✅ |
| 16 | 大 Session 的預覽：移動很順、只讀最後一段、往上捲補前一段且不跳、全部載完不重複；未匯入頁也一樣 | ✅ |
| 第三段 | 換 claude 接續 | 選做，沒做 |

## 發現（都不擋使用）

| # | 內容 |
|---|---|
| F1 | `upload.log` 裡第二個背景程序寫「背景上傳結束，剩下 3 筆」，意思其實是「交給正在跑的那一個」，容易誤會 |
| F2 | 另存成新的 Session（Y 從 N 另存）之後，每個會同步的指令都印「agora:Y 與 agora:N 來自同一個來源 Session」；N 已經是雲端沒有，這個提醒沒有意義而且重複 |
| F3 | 互動模式的篩選框用中文輸入法打字時，Enter 被輸入法吃掉，沒辦法用中文篩選；目前可以用英數（id、目錄名，例如 `agora-acc`） |
| F4 | 整合測試在 `agora-test` 留下 3 個沒清的 Session（10-03 第八輪之後），同步時一直印「還沒寫完」 |
| F5 | 驗收文件本身：一大段 heredoc、要手打長 id 不好用；這次改由 PM 建好 `/tmp/agora-acc` 的小工具（`keep` 存 id、`lock` 佔鎖、`ids.sh` 變數），本人只打短指令。下次改寫 `docs/acceptance.md` 時照這個方式 |

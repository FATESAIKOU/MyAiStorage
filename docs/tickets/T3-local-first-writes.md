# T3：寫入先存本機、背景上傳、少連 Drive

2026-10-03，使用者決定（PM 整理）。狀態：待轉成 OpenSpec change。

## 為什麼

1. **救不回來**（T1 PM 驗收 P1，`docs/review/P1-options.md`）：這台 import、continue、merge 出來的 Session，本機只有 `session.md`，原始檔上傳後就不在本機了。別台刪掉之後，`push --not-exist-upload` 因為本機沒有原始檔而拒絕。
2. **奇慢無比**（使用者回報，PM 量測）：每次連 Drive 是一次 rclone；rclone 內建 client 的配額是全世界共用的，被擋時一次要等約 50 秒（沒被擋時 1～2 秒）。import 1 個約 6 次 rclone、3 個約 10 次，delete N 個 N＋1 次。換回自己的 client 先不做，開成 issue #11 觀察。

## 使用者的決定

- 原話：「import continue merge 全部都改成 1. 先在 local 建立 cache, 2. 把東西 sync 上去」
- 解法選 **B 減少連 Drive 的次數** 與 **C 上傳改在背景**；A（換回自己的 client）「先觀察好了 開issue, 如果還慢在考慮做」

## 要做的

| # | 內容 |
|---|---|
| 1 | import、continue、merge 寫出的 Session，**先在本機鏡像存完整的一份**（`session.md`＋標頭指到的原始檔），再上傳；上傳完本機的原始檔留著。鏡像裡被取代的舊原始檔清掉 |
| 2 | **背景上傳**：指令把東西存進本機（鏡像＋outbox）就結束，上傳交給背景程序；同時只能有一個背景上傳在跑（鎖）。背景失敗時，下一個指令照現在的 outbox 機制補傳，並提醒有幾個還沒上傳 |
| 3 | **delete 也在背景**：先在本機記下（現有的刪除墓碑）並從清單消失，再由背景移到 Drive 垃圾桶 |
| 4 | **少連 Drive**：一批的上傳用一次 rclone 傳完（仍然先傳全部 raw、再傳全部 `session.md`），只驗一次 md5；一批的刪除用一兩次 rclone；拿掉重複的列檔 |
| 5 | 互動模式：雲端欄在背景上傳完成前顯示「未上傳」；結果視窗說「已存在本機，背景上傳中」 |

## PM 先定的預設（使用者沒有另外說的部分）

- 背景程序就是 `python -m agora.cli` 的一個內部入口，用 `start_new_session` 脫離終端機，輸出寫到 `<state>/upload.log`；不常駐，傳完就結束。
- 「別台看得到」的時間點延後到背景上傳完成；`push session <id>…` 照舊是前景、會等到傳完（要立刻確定的時候用）。
- 雲端沒有的標記：outbox 裡的不算（現在的規則不變）。

## 不做

- 換回自己的 OAuth client（issue #11）。
- 常駐的 daemon。

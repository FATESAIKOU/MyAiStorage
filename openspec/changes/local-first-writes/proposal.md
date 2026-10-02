## Why

使用者回報 import、delete「奇慢無比」。PM 量到的原因是：每次連 Drive 都是一次 rclone，而 rclone 內建 client 的配額是全世界共用的，被擋時一次要等約 50 秒，沒被擋時 1～2 秒。現在 import 1 個約 6 次 rclone、3 個約 10 次，delete N 個要 N＋1 次，而且都在前景等。

另外，在這台 import、continue、merge 出來的 Session，本機只有 `session.md`，原始檔上傳之後就不在本機了（T1 PM 驗收 P1，`docs/review/P1-options.md`）。所以別台刪掉之後救不回來。

需求單是 `docs/tickets/T3-local-first-writes.md`。使用者的決定：先在本機存完整的一份，再上傳；選 B（少連 Drive）與 C（上傳改在背景）。A（換回自己的 OAuth client）開成 issue #11 觀察。

## What Changes

- import、continue、merge 寫出的 Session，**先在本機鏡像存完整的一份**（`session.md` 加上標頭指到的原始檔），上傳之後原始檔也留在本機。鏡像裡被取代的舊原始檔會被清掉。
- **背景上傳**：指令把 Session 存進本機（鏡像加 outbox）就結束，上傳交給一個脫離終端機的背景程序。同一時間只有一個背景上傳。
- **delete 也在背景**：先在本機消失（墓碑），再由背景移到 Drive 垃圾桶。等著背景刪除的 Session，同步時不會被加回來。
- **少連 Drive**：
  - 一批的上傳用固定幾次 rclone 傳完：先傳全部原始檔，再傳全部 `session.md`，最後列一次檔驗 md5。
  - import 開頭的同步改成受 5 分鐘節流。
- `push session <id>…` 維持前景，會等到傳完，要立刻確定的時候用。
- 互動模式：背景上傳完成前，雲端欄顯示「未上傳」；結果視窗說明已經存在本機、背景上傳中。

## Capabilities

### New Capabilities
- `local-first-writes`：寫入先存本機完整的一份、背景上傳與背景刪除、一批只連幾次 Drive，以及互動模式怎麼顯示。

### Modified Capabilities

（無。`session-sync` 與 `batch-commands` 的既有要求不變：「雲端沒有」的判斷照舊不把 outbox 裡的算進去；`push --not-exist-upload` 照舊需要本機有原始檔，只是現在本機都會有。）

## Impact

- 程式：
  - `src/agora/store.py`：stage／remember、上傳、sync 跳過等著刪除的、delete；
  - `src/agora/cli.py`：import／continue／merge／delete 改成背景上傳，加一個內部入口；
  - `src/agora/tui.py`：結果視窗的說明；
  - `tests/fakes/fake_rclone.py`：從本機批次 copy 到 Drive。
- 文件：`docs/design.md` 4.1（寫入順序）、5.6（delete）、5.10，以及 README。
- 測試：單元測試（假 rclone，數 rclone 的呼叫次數），整合測試（只用 `agora-test`，背景上傳要等它傳完）。
- 程式碼行數：預估多 50～90 行（T4 的精簡另外算）。

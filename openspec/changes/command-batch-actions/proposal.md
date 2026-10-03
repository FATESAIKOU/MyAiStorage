## Why

使用者要在指令模式（之後的互動模式也一樣）一次處理多個 Session，看得到進度，中斷之後重跑能接著做；本機與 Drive 之間搬東西的指令（`cache agora`、`cache local`、`sync`）名字看不出方向，也會把別台機器刪掉的 Session 傳回去（review `docs/review/cache.md` K1）。原本的需求單是 `docs/tickets/T1-command-batch.md`，review 意見在 `docs/review/T1.md`。

## What Changes

- **BREAKING** `agora cache agora`、`agora cache local` 合併成 **`agora pull session <id>…`**；`agora sync` 改名 **`agora push session <id>…`**。兩個都只作用在給的 id，沒有「全部」。
- `agora import session` 可以一次匯入多個 agent session。
- import、delete、merge、pull、push 逐一在 stderr 印出 `k/N` 進度。
- 被中斷（SIGINT）之後重跑同一個指令會接著做：delete 略過 agora 自己刪過的 id；merge 沿用已經寫好的來源要約。
- **BREAKING** 同步時**不再自動清掉**別台機器刪除的 Session：本機保留，標成「雲端沒有」。`pull --not-exist-delete` 刪掉本機副本，`push --not-exist-upload` 傳回去。其他會寫回既有 id 的指令（continue、edit、import 的更新）遇到雲端沒有的就拒絕。
- push 每個 Session 只傳 `session.md` 與它指到的那一個原始檔。

## Capabilities

### New Capabilities
- `session-sync`: 本機與 Drive 之間的 pull／push、雲端沒有的 Session 怎麼標記與處理，以及其他指令遇到它時的行為
- `batch-commands`: import、delete、merge 一次處理多個、`k/N` 進度、中斷後重跑接著做

### Modified Capabilities

（`openspec/specs/` 目前沒有任何 spec；既有行為記在 `docs/design.md`。）

## Impact

- 程式：`src/agora/cli.py`（parser、import／delete／merge／pull／push）、`src/agora/cache.py`、`src/agora/store.py`（同步、索引的「雲端沒有」標記、push 上傳）。互動模式的對應在下一個 change（原 `docs/tickets/T2-tui-batch.md`）。
- 文件：`docs/design.md` 5.2、5.3、5.6、5.10，README。
- 測試：單元測試（假 rclone）與整合測試（只用 `agora-test`）。
- 程式碼行數目標 2,900 行。

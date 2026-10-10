## Why

`agora continue` 會把接續的結果寫回原本那個 Session（design 5.4，2026-10-02 本人的決定），所以「不再能從同一點分岔」。本人想從某個 Session 的某個時點分出另一條路去試，又不想改動原本那個（issue #29）。

## What Changes

- `agora continue session <id> --agent opencode|claude --fork`：
  - 先複製出一個**新 Session**：使用者 header 照抄，title 後面加 `-fork`；`id` 與 `agora` 區塊照新 Session 產生（`relation: fork`，`parents` 指向原 Session）。
  - 在新 Session 對話的最後加一則 user 發言「從 <原 id>（<原本的 header>）fork」，再加一則固定的 assistant 回覆，讓 AI 不會把自己當成原本那個 Session。
  - 用新 Session 啟動 code agent；之後的寫回都寫到新 Session，原 Session 不動。
- 跨 agent（例如原本是 opencode、用 claude 接）與 merge 出來的 Session 都能 fork；fork 的 fork 照字面變成 `…-fork-fork`。
- 修一個既有的 bug（fork 會讓它變成常態）：寫回一個還沒被 Drive 確認過的 Session（例如離線 import 之後馬上 continue／edit）會被誤判成「被別台刪掉」而另存。

## Capabilities

### New Capabilities

- `session-continue`：continue 的行為。這張先放 fork；#28（接續期間定期寫回）之後也加在這裡。

### Modified Capabilities

- `local-first-writes`：「只有自己驗過的版本離開 outbox」補上「寫回還沒被 Drive 確認過的 Session 不算更新既有 id」。

## Impact

- 程式：`src/agora/cli.py`（`cmd_continue`、`_save`、`_import_one` 的 S7、參數解析、fork 用的說明文字常數）；`src/agora/agents/`（新增 `append_native`）；`src/agora/store.py`（還沒確認過的判斷）。
- 測試：`tests/unit/test_cli*.py`（用假 agent），整合測試補一個 fork（只用 `agora-test`）。
- 文件：`docs/design.md` 5.4、README 的 continue。
- 不新增本人看得到的 header 欄位；關係記在 `agora` 區塊（`relation: fork`）。

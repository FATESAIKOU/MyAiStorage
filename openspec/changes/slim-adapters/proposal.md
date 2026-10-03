## Why

程式碼約 3,450 行，超過 2,900 行的目標。使用者 10-03 決定「先精簡，再放寬一點」（需求單 `docs/tickets/T4-slim-adapters.md`）。review 估過，兩個轉接器可以精簡：`docs/review/adapters-size.md`。

## What Changes

- 照 `docs/review/adapters-size.md` 精簡 `src/agora/agents/opencode.py`、`claude.py`，共用的部分搬到 `base.py`，**行為不變**。
- 例外有兩項，都是 review 列出、順手修掉的：
  - B1：claude 讀到看不懂的 `AGORA_SUMMARIZE_TIMEOUT` 時會丟 ValueError，改成和 opencode 一樣退回預設值；
  - B3：opencode 的比對改用 `store.normalize`，和索引一致。
- **不做 O1**，保留「export 找不到時回到專案目錄重試」這個保險。這是 PM 的預設，使用者沒有另外說。
- 做完量行數，由 PM 定新的目標。

## Capabilities

### New Capabilities

（無）

### Modified Capabilities

（無；行為不變，所以沒有 spec 的改動，`.openspec.yaml` 設 `skip_specs: true`。）

## Impact

- 程式：`src/agora/agents/{opencode,claude,base}.py`；測試裡用到改名函式的地方要跟著改。
- 不碰 `store.py`、`cli.py`、`cache.py`、`tui.py`、`background.py`，因為同時有 change `local-first-writes` 在改它們。

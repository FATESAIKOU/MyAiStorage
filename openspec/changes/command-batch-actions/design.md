## Context

見 proposal.md 的 Why。既有行為記在 `docs/design.md`（5.2 import、5.3 merge、5.4 continue、5.6 delete、5.10 快取）。現在的 `store.sync` 會把雲端已經沒有的 Session 從本機索引與鏡像清掉；`continue` 會寫回原本的 id（2026-10-02 使用者決定）。review 的意見：`docs/review/cache.md`（K1～K11）、`docs/review/T1.md`（Q1～Q12）。

## Goals / Non-Goals

**Goals:**
- 指令模式的行為一次定好，互動模式（下一個 change）只是呼叫這些指令。
- 刪除與復活只在使用者明確要求時發生。

**Non-Goals:**
- 不做「全部」的開關（使用者決定：只吃 id；要全部在互動模式按 `a`，或從 search 用管線接）。
- 不做雙向合併或衝突比對：push 是覆蓋（使用者接受別台較新的版本會被蓋過）。

## Decisions

- **「雲端沒有」記在索引裡，與「未上傳」分開**：用一張獨立的表（例如 `cloud_missing(ulid)`）或 `sessions` 的新欄位；若改 `sessions` 表，要用 `PRAGMA user_version` 判斷版本，不同就重建索引（索引本來就能從鏡像重建）。只有列檔完整成功才更新標記（Q4）。替代方案「同步時直接刪」被使用者否決。
- **寫回既有 id 前檢查**：continue、edit、import 的更新路徑在寫之前看標記，雲端沒有就拒絕（Q1，使用者決定）。這個檢查放在共用的地方，互動模式自動得到同樣的行為。
- **push 只上傳兩個檔**：依 `session.md` 標頭找出 `agora.raw.file`，逐一 `copyto`，不用整個資料夾的 `copy`，所以不需要排除清單（K1、K2、Q7）。
- **delete 的「已刪除」記錄**：刪除成功時把 ULID 記到 `<state>/deleted`；重跑時只有記錄裡的才略過（Q8），打錯的 id 照樣找不到。
- **merge 要約的沿用鍵**：agent 名稱、提示詞版本、模型設定（例如 `AGORA_OPENCODE_MODEL`）、來源 id、實際送出的文字（截斷之後）的雜湊；存在 `<state>/merge-sections/`，沿用前用 `SECTION_SCHEMA` 再驗一次（Q6）。成功的 merge 之後不必清理（檔案很小）。
- **pull 的 id 前綴**：沒有前綴或 `agora:` 是 agora；`opencode:`／`claude:` 是 agent；`ses_…`／uuid 沒有前綴就報錯（Q5）。
- **進度一律 stderr**（Q11）。

## Risks / Trade-offs

- [索引格式改變] → 用版本號重建，不做就地遷移。
- [列檔是全量的，Session 多時同步較慢] → 維持現有的節流；判斷雲端沒有只用這次的列檔結果，不額外呼叫。
- [rclone 內建 client 會被限流] → 已知（使用者接受）；逐一 `copyto` 的次數是 2×N，N 通常很小。
- [push 覆蓋別台較新的版本] → 使用者接受，寫在說明裡。

## Migration Plan

舊的 `cache`、`sync` 指令直接拿掉（只有使用者自己在用）。README 與 design 同步更新。索引版本不同時自動重建，不需要手動步驟。

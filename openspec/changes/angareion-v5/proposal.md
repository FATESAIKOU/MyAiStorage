## Why

本人 2026-10-12 看完介面圖後把 Angareion 的設計改成 v5（`design-v5-changes.md`），並在同日對 v5 疑點做裁定（`v5-decisions.md`）：channel 可以自由建立、不走 PR——`channel create` 三步（後端開實體 → 送註冊 → 本體的 `register-channel` workflow 直接更新 main 的對應檔）、`channel delete`（關實體不刪＋`unregister-channel`）；`send` 不給 `--to`＝廣播（算進「寄給我的」）；設計必須處理兩個 create 的競爭、觸發 workflow 的權限、pull 週期與失敗。v0 的 `--member`、`{repo, role}` 位址與 channel＝issue 標題的對應都要跟著改。

## What Changes

- channel 是英文名（Slack 式的 channel）；介面（參數、輸出、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念；對外五個介面：`channel create`、`channel delete`、`send`、`wait`、`read`。
- 第一期 channel 對所有隊員公開：**拿掉 `--member`**（`channel create` 不再收成員；後端不再存成員）。
- `send`：`--channel` 必填維持，收件人 `--to` 改為**選填**（不給＝發到整個 channel；廣播算進「寄給我的」）。
- 收件人／寄件人／身分改用共通 agent 命名 `<repo 名>-<pm|arch|impl|test>[-<id>]`。
- **channel 設定檔**（`config/angareion/channels.json`：channel 名 → backend 實體）由 `register-channel`／`unregister-channel` workflow 直接更新 main（channel 自由建立，不走 PR）；**backend 設定檔**（`config/angareion/backends.json`：backend 本身的設定）走 PR（AiStorage PM 審查＋merge，d 等級）。
- `channel create`／`channel delete` 的三步流程、同名與併發的處理（見 design §6、§7、§9）；`channel create` 不等落地，對應最慢一個 pull 週期後可見。
- 觸發 workflow 的方式與權限（`workflow_dispatch`／`repository_dispatch`／issue 事件）與 token 範圍**待本人確認**（design §10 有比較與建議）。
- `docs/angareion.md` 與 `skills/angareion/SKILL.md` 的改寫點列在 design §13，不在這個 change 改。

## Capabilities

### New Capabilities

- `angareion/channel-config`：兩個設定檔的格式與位置、register／unregister workflow 的契約（驗證、同名不覆蓋、sha 樂觀鎖、冪等）、讀取（本機複本＋lazy pull、失敗沿用）。

### Modified Capabilities

- `angareion`：五個介面（`channel create` 去 `--member`、改三步流程；新增 `channel delete`；`send --to` 選填）、目標與游標（收件人命名；廣播的歸屬）、訊息格式（位址改 agent 名；`to` 選填）、後端可以替換（channel 名經設定檔指到 backend 實體；不再用標題掃描；後端不存成員）。

## Impact

- 程式：`src/angareion/cli.py`（五個介面、`--backend`）、`messages.py`（位址驗證）、`backend.py`（`ChannelBackend` 介面、`Target`、假後端）、`github.py`（設定檔讀取、實體開／關、成員存取拿掉）、`config.py`（身分形狀、`config_dir`／`registry_repo`／`config_pull_interval`）、`client.py`、`state.py`（游標檔名）、註冊請求的送出（design §10 的裁定後才定）。
- repo 資產（本 change 只規劃、不實作）：`config/angareion/channels.json`、`config/angareion/backends.json`、`.github/workflows/register-channel.yml`、`.github/workflows/unregister-channel.yml`、workflow 用的更新腳本。
- 測試：`tests/unit/test_angareion_*.py`（cli、messages、backend、github、config、state、skill；新增註冊／同名／pull 的案例）。
- 文件（不在此 change 改，只列改寫點）：`docs/angareion.md`、`skills/angareion/SKILL.md`。
- 流程：backends.json 的 PR（d 等級）；本人對 workflow 觸發方式與 token 的確認；PR #41 的處置（提案見 design）。

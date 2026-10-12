## Why

本人 2026-10-12 看完介面圖後，把 Angareion 的設計改成 v5（唯一依據：`design-v5-changes.md`）：channel 是通訊軟體式的英文名、介面不再出現 GitHub repo／issue 的概念；頻道對所有隊員公開、收件人用共通的 agent 命名。v0 的 `--member`、`{repo, role}` 位址與 channel＝issue 的對應都要跟著改。

## What Changes

- channel 是英文名（Slack 式的 channel）；介面（指令、輸出、錯誤訊息）MUST NOT 出現 GitHub repo／issue 的概念。
- 第一期 channel 對所有隊員公開：**拿掉 `--member`**（`channel create` 不再收成員；後端不再存成員）。
- `send`：`--channel` 必填維持，收件人 `--to` 改為**選填**（不給＝發到整個 channel）。
- 收件人／寄件人／身分改用共通 agent 命名 `<repo 名>-<pm|arch|impl|test>[-<id>]`，取代 `{repo, role, name?}` 位址。
- **新增 channel 設定檔**（channel 名 → backend 實體）與 **backend 設定檔**（backend 本身的設定）：放 MyAiStorage repo，用 PR 更新（AiStorage PM 審查＋merge，d 等級）；讀取端 clone＋定期 pull。
- `channel create` 是否保留成指令，待本人確認（見 design 的 Open Questions；spec 暫維持現狀）。
- `docs/angareion.md` 與 `skills/angareion/SKILL.md` 的改寫點列在 design，不在這個 change 改。

## Capabilities

### New Capabilities

- `angareion/channel-config`：channel 設定檔與 backend 設定檔的格式、位置、更新（PR）與讀取（clone＋定期 pull）流程。

### Modified Capabilities

- `angareion`：四個介面（`channel create` 去 `--member`；`send --to` 選填）、目標與游標（收件人命名；無 `to` 訊息的歸屬）、訊息格式（位址改 agent 名；`to` 選填）、後端可以替換（channel 名經設定檔對應到 backend 實體；後端不再存成員）。

## Impact

- 程式：`src/angareion/cli.py`（參數與 `channel create`）、`messages.py`（位址驗證）、`backend.py`（`ChannelBackend` 介面、`Target`、假後端）、`github.py`（設定檔讀取、成員存取拿掉）、`config.py`（身分形狀、`channel_repo` 被設定檔取代）、`client.py`、`state.py`（游標檔名）。
- 測試：`tests/unit/test_angareion_*.py`（cli、messages、backend、github、config、state、skill）。
- 文件（不在此 change 改，只列改寫點）：`docs/angareion.md`、`skills/angareion/SKILL.md`。
- 流程：MyAiStorage repo 新增兩個設定檔；AiStorage PM 的 PR 審查（d 等級）。

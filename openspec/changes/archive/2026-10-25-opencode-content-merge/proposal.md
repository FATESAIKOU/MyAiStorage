## Why

MyAiStorage#242。`agora continue … --harness-file` 對 opencode 交付 harness 時，現在把 `OPENCODE_CONFIG_CONTENT` 設成只含 `instructions` 的 JSON，呼叫方環境原有的同名變數被整份蓋掉：呼叫方放在裡面的設定（provider、permission 等）在這個 agent 裡消失，而且沒有任何提示。PMO 2026-10-25 定案：改成合併。

## What Changes

- 啟動 opencode 的環境裡，`OPENCODE_CONFIG_CONTENT` 改成**合併**：呼叫方環境（`os.environ`）原有的值若是 JSON 物件，與 harness 的 `{"instructions": [...]}` 合併；鍵衝突時 harness 的值優先。
- 合併在 cli 疊環境的地方做（`Launch` 仍是純資料、只帶 harness 的值，呼叫方的值不進 `Launch.env`，也不進子行程之間傳的 JSON）；interactive 與 `--headless` 走同一個地方。
- 呼叫方的值不是合法 JSON、或不是物件：視為沒有，只給 harness 的值；不印出原值（可能含憑證）。
- 原值已有 `instructions`：被 harness 的值取代（不串接）。
- 不帶 `--harness-file` 時不碰呼叫方的值。
- 文件（`docs/atelier/tool.md`）同步。

## Capabilities

### Modified Capabilities

- `session-continue`：「continue --harness-file」那一則的 opencode 條目與兩個 Scenario——delta 在 `specs/session-continue/spec.md`。

## Impact

- `src/agora/agents/opencode.py`（`merge_config_content`）、`src/agora/cli.py`（`_spawn_env`）、`tests/unit/test_continue_harness.py`、`docs/atelier/tool.md`、`openspec/specs/session-continue/spec.md`。

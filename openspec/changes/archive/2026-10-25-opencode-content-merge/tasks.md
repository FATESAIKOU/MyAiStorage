## 1. 實作

- [x] 1.1 `merge_config_content`（`src/agora/agents/opencode.py`）：物件合併、harness 優先、壞值／非物件只給 harness 的值
- [x] 1.2 `_spawn_env`（`src/agora/cli.py`）：Popen 的環境疊在這裡，帶 `OPENCODE_CONFIG_CONTENT` 時合併；interactive 與 headless 共用同一個 Popen
- [x] 1.3 測試（`tests/unit/test_continue_harness.py`）：沒有原值／物件不衝突／`instructions` 衝突／壞 JSON 與非物件／不帶 `--harness-file`；一律驗 Popen 實際收到的環境（來源：#241 審查 A）

## 2. 文件與 spec

- [x] 2.1 `docs/atelier/tool.md`：合併規則
- [x] 2.2 delta `specs/session-continue/spec.md`（MODIFIED，規範語；來源：#241 審查 B）
- [x] 2.3 `openspec validate opencode-content-merge --strict` → `openspec archive` → `openspec validate --all --strict`

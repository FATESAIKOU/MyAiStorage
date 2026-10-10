## 1. 本人裁定（先做）

- [ ] 1.1 把 design 第一節「交本人選的問題」的 A／B／C 交本人選定；記錄選擇與理由。
- [ ] 1.2 依選擇更新本 change 的 `specs/agent-sidecar/spec.md`：選 A 就把「啟動與終止 team」改寫成對應的 v0、選 B 維持現狀、選 C 再加記憶的 requirement；`openspec validate --strict` 要過。

## 2. 對齊外部依賴

- [ ] 2.1 跟 MyPMO 的 LLMGateway 設計對齊租用介面（申請／准許的欄位、agent 的模型存取怎麼交、回報時機），把協定寫回 design 的 D6；在此之前 sidecar 用 `ModelBroker` 的假 broker。
- [ ] 2.2 跟 Atelier（#32）對齊 harness 的格式、位置與載入點。
- [ ] 2.3 跟 MyLinuxPool 對齊：`shared-configs/agent-sidecar/` 的 unit 欄位、`--check` 的三態判準、tmux 執行檔由哪個單元保證。
- [ ] 2.4 spike：在 opencode 與 claude 的 TUI 上實測「執行中送字＋Enter」（排隊、丟掉、或要 Ctrl-C）；把結果寫回 design 的 D3 並定案機制。

## 3. 實作 v0（本人選定範圍之後）

- [ ] 3.1 sidecar 骨架：設定檔、registry（原子寫、重啟恢復）、持久佇列、`sidecar status`；單元測試。
- [ ] 3.2 agent 宿主：tmux（`-L sidecar`）啟動 `agora continue`、狀態對照、`send-keys` 送字、三段 escalate 終止；用假 agent（腳本）做單元測試。
- [ ] 3.3 Angareion 收信迴圈（用 angareion 的程式介面）＋佇列＋依 urgency 投遞＋ack；假後端測試（含 304、重啟恢復、去重）。
- [ ] 3.4 啟動與終止 team：home Session 的 `continue --fork --sync-period`、harness 載入、結束後存回／匯出；假 agora、假 agent 的單元測試。
- [ ] 3.5 回報中繼：unix socket ＋ `SO_PEERCRED` 驗身分、`sidecar say` 薄指令；測試確認 agent 環境沒有 A2A token、送出的 `from` 是 team 身分。
- [ ] 3.6 `ModelBroker` 介面與假 broker；真接 LLMGateway 等 2.1。

## 4. 分發（改動在 MyLinuxPool，照它的規則）

- [ ] 4.1 `shared-configs/agent-sidecar/`：`unit.json`、`install.sh`（自帶 venv、systemd user unit、`--check`：檔案、tmux／Python 依賴、設定、token 能讀信道）、`files/`（unit 與範例設定）、`tests/`。
- [ ] 4.2 在一個 worker profile 上安裝與 `--check`；改 profile 的 shared-configs 清單（不是新增 profile）；記錄結果。

## 5. 驗收與文件

- [ ] 5.1 端到端驗收（本人在一台 worker 上）：PMO 送一則 → sidecar 收 → 啟動 team → team 回報 → PMO 收到；另外各做一次終止與 sidecar 重啟。
- [ ] 5.2 文件：安裝、設定、憑證（token 放置與輪替）、狀態與 log 位置、與 LLMGateway／Atelier 的邊界。
- [ ] 5.3 review（arch）與本人驗收；spec 歸檔。

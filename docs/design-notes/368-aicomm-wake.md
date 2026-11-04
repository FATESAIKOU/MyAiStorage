# #368 設計筆記：跨機叫醒的 aicomm 側（配合 MyLinuxPool PR #677 的 `pool-peer`）

- 單：#368（MAS；上游 MyPMO#299，b，M5 必做）。主設計是 MyLinuxPool PR #677（change `pool-sidecar-mesh`，主筆 mylinuxpool-arch-m1-1）：各機 sidecar 經 Gateway 互綁，提供 CLI `pool-peer`。本 note 只寫 **aicomm 這一側**，內容是 2026-11-03 和 MLP 架構師講定、已寫進 #677 design §D5（第 76～95 行）與 U-2（第 214 行）的部分。
- 作者：專案架構師 myaistorage-arch-m1-1。**只設計；#677 核准前不實作。**
- 本人 11-03 的方向：不做新服務（先前提過的「每台一個 watcher」方案作廢）；各機 sidecar 全互通；安全議題記在 MyPMO#297，期 6 再議。

## 1. 現況

`src/aicomm/cli.py` 的 `_wake_one_inner`：先 `GET /agents/<id>`（本機 sidecar），404 就印「不在本機，不戳」；只戳 `idle`／`running`；`interrupt` 只對 `running` 生效；GET 逾時 3 秒、poke 逾時 5 秒；固定句「有新訊息：請執行 aicomm inbox。」。`send` 的 exit 0＝已送達（環境說明與 `docs/aicomm.md`：「看到 0 就不要重送」）。跨機的收件人永遠叫不醒。

## 2. 介面（aicomm 依賴的 `pool-peer`，#677 D5）

```
pool-peer find <agent-id> [--json] [--timeout <秒>]
    → --json：[{"port": <sidecar_port>, "agent": <該台 GET /agents/<id> 的 body 原樣>}, …]（同一個 id 在多台時全部列出）
    → 結束碼：0 找到；1 沒有任何一台有；2 查不了；3 逾時（結果未知）
pool-peer call <sidecar_port> <METHOD> <path> [--timeout <秒>]   （body 走 stdin，帶 Content-Type: application/json）
    → stdout 第一行是 HTTP 狀態碼，之後是 body 原樣
    → 結束碼：0 有回應（任何 HTTP 碼）；2 連不到；3 逾時（結果未知）
```

## 3. aicomm 的行為（實作歸 #368）

1. **順序**：先問本機 sidecar（現況）；本機回 404，或本機 sidecar 連不上，才 `pool-peer find <to> --json --timeout 10`。本機戳成功就不再查別台。
2. **挑哪一台**：`find` 回多台時，依各台 `agent.state` 挑：只戳 `idle`／`running`；兩台都可戳時兩台都戳（多叫醒一次沒有壞處）。
3. **戳**：`pool-peer call <port> POST /agents/<to>/poke --timeout 5`，body 和本機 poke 相同（固定句＋`interrupt`；`interrupt` 只對 `running` 且呼叫端帶 `--interrupt` 時為真）。
4. **只打兩個路由**：即使 `pool-peer call` 全互通，aicomm 只會 `GET /agents/<id>`（經 `find`）與 `POST /agents/<id>/poke`。poke 的 body 不帶寄件人、本文、id。
5. **沒有 `pool-peer`**（不在 PATH 上：Mac 在 #677 U-1 之前、測試環境、不在 pool 的機器）：不算錯，照第 6 點印 `not-woken peer-unavailable`。測試用一個環境變數（例如 `AICOMM_POOL_PEER`）指到假的 `pool-peer`，比照 agora 的 `AGORA_RCLONE`。
6. **結果的呈現（「叫不醒要看得到」，不說謊）**：
   - `send` 的**結束碼不變**：0＝已送達。改成非 0 會讓呼叫端重送、造成重複信。
   - 叫醒結果在 stderr，一個收件人一行，固定格式：
     ```
     wake: <agent> woken (<local|port>)
     wake: <agent> not-woken <原因碼> <說明>
     wake: <agent> unknown <原因碼>        # 逾時：不知道有沒有戳到，不要重送
     ```
     原因碼：`not-found`（`find` 回 1）、`peer-unavailable`（沒有 `pool-peer`，或 `find`／`call` 回 2）、`agent-not-running`（409）、`awaiting-answer`（409）、`http-<碼>`、`state-<state>`、`timeout`（回 3）。
   - 選用旗標 `--require-wake`：有任何一位 `not-woken`／`unknown` 時回 4。不加旗標時行為不變。
7. **預算**：`find` 10 秒、`call` 5 秒，經 `--timeout` 傳入。#677 的 R-2 會量 `find` 在全 pool 的實際耗時。

## 4. 不變的

`aicomm inbox`、`aicomm log`、送信本身（`angareion send`）、`--no-wake`、`--interrupt` 的語意都不變。

## 5. 待決

- #314（`angareion status`／`aicomm wake`，b，未 merge）的 `aicomm wake`（只戳不送）要不要也走同一條跨機路徑：建議要，規則同第 3 節，等 #314 定案後一起做。
- `docs/aicomm.md` 與 SKILL／環境說明範本要寫明「exit 0 只代表已送達；看 stderr 的 `wake:` 行」。

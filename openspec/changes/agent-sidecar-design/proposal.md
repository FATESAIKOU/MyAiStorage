## Why

worker（MyLinuxPool）上所有 AI agent（Claude Code、opencode，以及之後的其他 agent）都要受一個監督程式控制；它負責收 Angareion 的訊息、依緊急度送進 agent，也負責啟動與終止 team。現在 Mac 上靠 herdr 開 agent、看狀態，worker 上沒有等價物，期 1 的流程（PMO → Angareion → worker team → PR → 回報）因此跑不起來（MyAiStorage#31，本人 2026-10-10 指示）。

**這張先設計，經本人選定範圍之後才實作**（設計任務獨立保留）。

## What Changes

- 新增 **Agent sidecar** 的設計：worker 上的常駐監督程式，v0 做「收 Angareion → 依緊急度投遞」與「啟動、終止 team」。
  - **收信與投遞**：為它負責的身分輪詢 Angareion；`urgency` ≥ 8 直接送進執行中的 agent（成為它下一輪的輸入），< 8 進持久佇列、在下一次 hand-off 時依緊急度送出；投遞成功才 ack。
  - **啟動與終止 team**：用 `agora continue`（含 `--sync-period`，任務之間用 `--fork` 隔離）；終止對整個 process group 依序 SIGINT → SIGTERM → SIGKILL；agent 結束後對話存回 agora。
  - **登錄與狀態**：自己啟動的 agent 記在可持久化的 registry，sidecar 重啟後還找得到、終止得到；不是它啟動的 agent 不管（v0）。
  - **憑證邊界**：A2A token 由本人設定、只給 sidecar，agent 環境沒有；模型憑證一律走 LLMGateway，sidecar 不自己拿。
  - **分發**：做成 MyLinuxPool 的 shared-config 單元（像 pool-runtime），任何 profile 都可以帶，MUST NOT 要求新增 profile（profile ＝ 部門）。
- **交本人選的問題**：sidecar 的範圍三種切法（A 通訊＋心跳／B 再加啟動關閉／C 再加記憶），利弊與 arch 的建議寫在 design.md 第一節；由本人選。
- **和 LLMGateway 的分界**：誰啟動 agent、誰拿憑證、兩邊怎麼溝通，依三種切法各自寫清楚（MyPMO 的 LLMGateway 設計對齊後補上介面細節）。
- **不在這張**：實作（本人選定範圍後才開）、群組定址、多 worker 調度、不是它啟動的 agent、期 4 的全 worker 監視。

## Capabilities

### New Capabilities

- `agent-sidecar`: worker 上 AI agent 的監督程式——收 Angareion 訊息並依緊急度投遞、啟動與終止 team、登錄與狀態、憑證與 LLMGateway 的分界、以一個 shared-config 單元分發。

### Modified Capabilities

（無。）

## Impact

- 程式：MyAiStorage 新增 `src/sidecar/`（設定、registry、佇列、投遞、agent 宿主、LLMGateway 介面）與 console script；用 angareion 的程式介面收發信；用 `agora continue／import` 啟動與匯出。
- 跨 repo：MyLinuxPool 新增 `shared-configs/agent-sidecar/`（unit.json、install.sh、files、tests），照它的 unit 規則；不新增 profile。
- 外部：LLMGateway 的介面（設計在 MyPMO）；Atelier harness（#32）；本人設定的 A2A token；worker 上既有的 git 憑證（開 PR 用）。
- 測試：單元測試用假 agent（腳本型）與假 Angareion 後端、假 LLMGateway；worker 上的端到端由本人執行。

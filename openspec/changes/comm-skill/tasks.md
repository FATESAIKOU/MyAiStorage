## 0. 前提（不是 PR）

- 本 change 是設計；經非作者審查、PMO 向本人說明、本人同意之後才動工。本人 2026-10-24 已裁定 J1、J4～J7、J9、J10；J2 為 (a)（PMO 依本人字面判定）。
- 不動：`src/angareion/`、`src/atelier/`、Angareion 協定；sidecar 與 MyPMO 的程式（別的 repo，只列步驟）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。

## 1. 定案（要 PMO／本人）

- [ ] 1.1 判斷點已定（J2 為 (a)，PMO 2026-10-24 依本人字面判定，其餘本人裁定）；本人看過定稿設計即可動工
- [ ] 1.2 待驗（V1、V3、V5、V6、V8、V9）在拋棄式環境實測（sidecar 用自己起的測試 daemon、不碰真 agent、不碰真 Session），結果寫回 design
- [ ] 1.3 指令名（暫 `aicomm`，作者提議 P11；PM／PMO 可改）

## 2. MyAiStorage：指令與說明檔（PR 1）

**範圍**：`src/aicomm/`（新；只用標準函式庫）、`pyproject.toml`（console script）、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/`。

- [ ] 2.1 `send`：參數驗證（`--channel` 必填；`--interrupt` 與 `--no-wake` 互斥）、`--ticket` → `--ref`、呼叫 `angareion send`、印訊息 id
- [ ] 2.2 叫醒：固定用 sidecar 預設 loopback 位址（無設定檔；測試用覆寫 `AICOMM_SIDECAR` 為作者提議 P5，只在要叫醒時驗 loopback）、`GET /agents/{id}`、條件（`idle` 或 `running`；不看 tui／`attach`）、`--interrupt`（`interrupt: true`，預設 false）、固定文字、不重送、逾時、`--no-wake`、自己不戳自己、stderr 一行結果；不依角色分支
- [ ] 2.3 `inbox`：`read`（無 `--channel`）、`--wait`（exit 3 逾時、exit 2 原樣傳回不 read）、`--mentioned`（wait 與 read 都帶）、`--json`
- [ ] 2.4 `SKILL.md`：frontmatter `name`／`description`；用法、結束碼、「送達＝寫進 Angareion」、不要貼私人資訊、閒著用 `inbox --wait`；D2.9 三條軟規則（預設不中斷；隊員基本上不要中斷 PM／PMO；「收到」類回覆加 `--no-wake`）、「之後要硬控制走 sidecar open 白名單（MyLinuxPool#346）」、代價（任何同台 agent 都能叫醒 PMO）（沒有「每輪結束前 inbox」這條）；全角色同一份
- [ ] 2.5 測試（全部對自己起的假 sidecar 與假 `angareion`；測試一律設 `AICOMM_SIDECAR` 指到自己起的假 sidecar；沒設就讓測試失敗〔guard〕；若 P5 被拒絕則改以函式參數注入）：
  - 每個 spec scenario 一條（寫入失敗不叫醒、404、`running`、`idle`（tui 與 headless 各一）、`starting` 不戳、`running` 也戳（不打斷）、`running` 連送兩則排兩句、`--interrupt` 帶 `interrupt: true`、預設 `interrupt: false`、`--interrupt` 與 `--no-wake` 互斥、廣播、mention、逾時不重送、覆寫為非 loopback 且要叫醒、覆寫為非 loopback 但 `--no-wake`、不依角色分支、SKILL.md 含三條軟規則、`wait` exit 2、`--mentioned` 同時給 wait 與 read、戳的文字固定）
  - 回應壞掉（非 JSON、缺欄位）＝不戳、exit 0
  - 對每條測試做改壞驗證（拿掉條件，測試要紅）
  - 斷言對假 sidecar 的請求只有「查」與「戳」兩種
- [ ] 2.6 `tests/unit/` 的文件測試：SKILL.md 與 docs 不含私人資訊樣式（沿用 `test_angareion_docs.py` 的做法）

## 3. 分發（跨 repo；PMO 排）

- [ ] 3.1 MyLinuxPool：映像建置用 tool-pins 同一個 sha 多裝 `aicomm`、放 `skills/aicomm/SKILL.md`（照 `docs/angareion.md`「說明檔（skill）」）
- [ ] 3.2 Mac：PMO 與各 PM 的安裝步驟（`uv tool install` 同 sha＋說明檔位置）——現況【未知】，先查
- [ ] 3.3 各環境說明（`--append`）加一行：溝通用 `aicomm`

## 4. harness（下一次改版時；不單獨出版本，除非 V6 實測不可靠）

- [ ] 4.1 `KNOWN_CAPABILITIES` 加 `comm`（`tests/unit/test_atelier_harness.py`）
- [ ] 4.2 pm／impl／rev 的下一版 `capabilities` 加 `comm`；正文只寫能力

## 5. MyPMO（別的 repo；只列）

- [ ] 5.1 `dispatch.py reply` **對 Mac team** 改為呼叫 `aicomm send`，由 PMO 決定叫醒失敗要不要保留 exit 2；**worker team 保留現行**（經轉發埠）
- [ ] 5.2 觀察一個 PM 週期後 Mac 路徑的舊 `reply` 標 deprecated；`send`／`poke`／`watch`／`open` 保留
- [ ] 5.3 worker team 的叫醒長期歸屬（PMO 經轉發埠，或 worker 上的 aicomm 自己叫醒同台 PM）——另案

## 6. 驗收

- [ ] 6.1 拋棄式環境端到端：A 送給閒置的 tui B → B 被戳 → B 跑 `inbox` 讀到；閒置的 headless B 同樣被戳；B 在 `running` 時送 → → 也被戳（不打斷，排到這一輪之後），B 之後讀到；`--interrupt` → 中斷；`--no-wake` → 不戳、訊息照樣讀得到；收件人在別台 → 不戳、`inbox --wait` 讀到
- [ ] 6.2 實測不碰真 Session、不讀真 credential

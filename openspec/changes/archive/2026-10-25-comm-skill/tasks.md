## 0. 前提（不是 PR）

- 本 change 是設計；經非作者審查、PMO 向本人說明、本人同意之後才動工。本人 2026-10-24 已裁定 J1、J4～J7、J9、J10；J2 為 (a)（PMO 依本人字面判定）。
- 不動：`src/angareion/`、`src/atelier/`、Angareion 協定；sidecar 與 MyPMO 的程式（別的 repo，只列步驟）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。

## 1. 定案（要 PMO／本人）

- [x] 1.1 判斷點已定（J2 為 (a)，PMO 2026-10-24 依本人字面判定，其餘本人裁定）；本人看過定稿設計即可動工。本人 2026-10-24 同意定稿（設計 PR #213）
- [x] 1.2 待驗（V1、V3、V5、V6、V8、V9、V10、V11）在拋棄式環境實測（sidecar 用自己起的測試 daemon、不碰真 agent、不碰真 Session），結果寫回 design。已測並寫回：V1、V3、V9、V10、V11（2026-10-24）。**V5、V6、V8 沒測**：V5 只在 D3.1 選 B 時才需要（已裁定 A）；V6（skill description 是否被可靠使用）要等 skill 實作後在 6.1 端到端看；V8（有人 attach 時被戳）這次的實測計畫沒有涵蓋
- [x] 1.3 指令名（暫 `aicomm`，作者提議 P11；PM／PMO 可改）。定為 `aicomm`（#216）

## 2. MyAiStorage：指令與說明檔（PR 1）

**範圍**：`src/aicomm/`（新；只用標準函式庫）、`pyproject.toml`（console script）、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/`。

- [x] 2.1 `send`：參數驗證（`--channel` 必填；`--interrupt` 與 `--no-wake` 互斥）、`--ticket` → `--ref`、呼叫 `angareion send`、印訊息 id（PR #216）
- [x] 2.2 叫醒：固定用 sidecar 預設 loopback 位址（無設定檔；測試用覆寫 `AICOMM_SIDECAR` 為作者提議 P5，只在要叫醒時驗 loopback）、`GET /agents/{id}`、條件（`idle` 或 `running`；不看 tui／`attach`）、`--interrupt`（`interrupt: true`，預設 false）、固定文字、不重送、逾時、`--no-wake`、自己不戳自己、stderr 一行結果；不依角色分支（PR #216）
- [x] 2.3 `inbox`：`read`（無 `--channel`）、`--wait`（exit 3 逾時、exit 2 原樣傳回不 read）、`--mentioned`（wait 與 read 都帶）、`--json`（PR #216）
- [x] 2.4 `SKILL.md`：frontmatter `name`／`description`；用法、結束碼、「送達＝寫進 Angareion」、不要貼私人資訊、閒著用 `inbox --wait`；D2.9 三條軟規則（預設不中斷；隊員基本上不要中斷 PM／PMO；「收到」類回覆加 `--no-wake`）、「之後要硬控制走 sidecar open 白名單（MyLinuxPool#346）」、代價（任何同台 agent 都能叫醒 PMO）（沒有「每輪結束前 inbox」這條）；全角色同一份（PR #216）
- [x] 2.5 測試（全部對自己起的假 sidecar 與假 `angareion`；測試一律設 `AICOMM_SIDECAR` 指到自己起的假 sidecar；沒設就讓測試失敗〔guard〕；若 P5 被拒絕則改以函式參數注入）：
  - 每個 spec scenario 一條（寫入失敗不叫醒、404、`running`、`idle`（tui 與 headless 各一）、`starting` 不戳、`running` 也戳（不打斷）、`running` 連送兩則排兩句、`--interrupt` 帶 `interrupt: true`、預設 `interrupt: false`、`--interrupt` 與 `--no-wake` 互斥、廣播、mention、逾時不重送、覆寫為非 loopback 且要叫醒、覆寫為非 loopback 但 `--no-wake`、不依角色分支、SKILL.md 含三條軟規則、`wait` exit 2、`--mentioned` 同時給 wait 與 read、戳的文字固定）
  - 回應壞掉（非 JSON、缺欄位）＝不戳、exit 0
  - 對每條測試做改壞驗證（拿掉條件，測試要紅）——實際是抽樣（作者 47 處、審查者 9 處），見 PR #216
  - 斷言對假 sidecar 的請求只有「查」與「戳」兩種
  - （PR #216）
- [x] 2.6 `tests/unit/` 的文件測試：SKILL.md 與 docs 不含私人資訊樣式（沿用 `test_angareion_docs.py` 的做法）（PR #216）

## 3. 分發（跨 repo；PMO 排）

- [x] 3.1 MyLinuxPool：映像建置用 tool-pins 同一個 sha 多裝 `aicomm`、放 `skills/aicomm/SKILL.md`（照 `docs/angareion.md`「說明檔（skill）」）。MyLinuxPool #376 已做（PMO 2026-10-25 告知：四台 worker 已換映像）
- [x] 3.2 Mac：PMO 與各 PM 的安裝步驟（`uv tool install` 同 sha＋說明檔位置）——現況【未知】，先查。2026-10-25 已裝：Mac 改用每個 sha 一個 venv（不是 `uv tool install`），`angareion` 與 `aicomm` 同 sha、symlink 一起換；說明檔不放全域 skills，改在 Mac 環境說明加一節（那一節由 MyLinuxPool#376 加；#376 不做安裝）
- [x] 3.3 各環境說明（`--append`）加一行：溝通用 `aicomm`。已做（PMO 2026-10-25 告知：Mac 與 worker 的環境說明都有「溝通用 aicomm」一節）

## 4. harness（下一次改版時；不單獨出版本，除非 V6 實測不可靠）

**搬到 #236**（PMO 2026-10-25：等下一次 harness 改版一起做；本 change 歸檔不等它）。下面兩項留原文，不在這裡勾。

- [ ] 4.1 `KNOWN_CAPABILITIES` 加 `comm`（`tests/unit/test_atelier_harness.py`）
- [ ] 4.2 pm／impl／rev 的下一版 `capabilities` 加 `comm`；正文只寫能力

## 5. MyPMO（別的 repo；只列）

- [x] 5.1 `dispatch.py reply` **對 Mac team** 改為呼叫 `aicomm send`，由 PMO 決定叫醒失敗要不要保留 exit 2；**worker team 保留現行**（經轉發埠）。FATESAIKOU/MyPMO#183（PR #184）已做
- [ ] 5.2 觀察一個 PM 週期後 Mac 路徑的舊 `reply` 標 deprecated；`send`／`poke`／`watch`／`open` 保留。**搬到 FATESAIKOU/MyPMO#190**
- [ ] 5.3 worker team 的叫醒長期歸屬（PMO 經轉發埠，或 worker 上的 aicomm 自己叫醒同台 PM）——另案。**搬到 FATESAIKOU/MyPMO#191**

## 6. 驗收

- [x] 6.1 拋棄式環境端到端：A 送給閒置的 tui B → B 被戳 → B 跑 `inbox` 讀到；閒置的 headless B 同樣被戳；B 在 `running` 時送 → 也被戳（不打斷），B 之後讀到（「插進同一輪、在當下的 tool 呼叫之後處理」是對 tui B 的期望；headless、opencode 的 running B 另外記錄實際觀察到的結果）；`--interrupt` → 中斷；`--no-wake` → 不戳、訊息照樣讀得到；收件人在別台 → 不戳、`inbox --wait` 讀到。2026-10-25 全過（真 Angareion、拋棄式身分與測試 sidecar；結果見 #212 的留言）。**沒涵蓋**：`running` 的 headless／opencode 收件人、V6、V8；「收件人在別台」這項用 `inbox`（不帶 `--wait`）與另一組 `angareion wait`→`inbox` 驗證
- [x] 6.2 實測不碰真 Session、不讀真 credential。2026-10-25：測試本身不讀 credential、不碰真 Session（驗收過程中 PM 曾誤把正在用的 sidecar 的 hook 設定印到自己的畫面，未外送、未寫檔，已報 PMO）；測試 agent 用真 HOME，會在本機新增它自己的 session 紀錄檔（事前經 PMO 同意）

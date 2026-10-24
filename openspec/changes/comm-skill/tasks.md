## 0. 前提（不是 PR）

- 本 change 是設計；經非作者審查、PMO 向本人說明、本人同意（判斷點 J1、J2、J4～J7、J9、J10）之後才動工。
- 不動：`src/angareion/`、`src/atelier/`、Angareion 協定；sidecar 與 MyPMO 的程式（別的 repo，只列步驟）。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的 session id。

## 1. 定案（要 PMO／本人）

- [ ] 1.1 本人回答 design 的判斷點 J1、J2、J4～J7、J9、J10
- [ ] 1.2 待驗（V1、V3、V5、V6、V8、V9）在拋棄式環境實測（sidecar 用自己起的測試 daemon、不碰真 agent、不碰真 Session），結果寫回 design
- [ ] 1.3 指令名（暫 `aicomm`，作者提議 P11；PM／PMO 可改）

## 2. MyAiStorage：指令與說明檔（PR 1）

**範圍**：`src/aicomm/`（新；只用標準函式庫）、`pyproject.toml`（console script）、`skills/aicomm/SKILL.md`、`docs/aicomm.md`、`tests/unit/`。

- [ ] 2.1 `send`：參數驗證（`--channel` 必填）、`--ticket` → `--ref`、呼叫 `angareion send`、印訊息 id
- [ ] 2.2 叫醒：只在要叫醒時檢查單一 loopback 位址、設定檔（`nowake` 名單、`sidecar` 單一 loopback 位址；邊界驗證；壞掉且可能要叫醒＝exit 1；唯一環境變數 `AICOMM_CONFIG`）、`GET /agents/{id}`、條件（只有 `idle`，不看 tui／`attach`）、固定文字、不重送、逾時、`--no-wake`、自己不戳自己、stderr 一行結果
- [ ] 2.3 `inbox`：`read`（無 `--channel`）、`--wait`（exit 3 逾時、exit 2 原樣傳回不 read）、`--mentioned`（wait 與 read 都帶）、`--json`
- [ ] 2.4 `SKILL.md`：frontmatter `name`／`description`；用法、結束碼、「送達＝寫進 Angareion」、不要貼私人資訊、閒著用 `inbox --wait`；兩條軟規則：每輪結束前跑一次 `aicomm inbox`；「收到」類回覆加 `--no-wake`
- [ ] 2.5 測試（全部對自己起的假 sidecar 與假 `angareion`；測試一律設 `AICOMM_CONFIG` 指到暫存的設定檔；沒設就讓測試失敗〔guard〕）：
  - 每個 spec scenario 一條（寫入失敗不叫醒、404、`running`、`idle`（tui 與 headless 各一）、`starting` 不戳、廣播、mention、不叫醒名單、逾時不重送、非 loopback 且要叫醒、非 loopback 但 `--no-wake`、收件人在名單上且位址非 loopback、設定檔不存在／壞掉、`wait` exit 2、`--mentioned` 同時給 wait 與 read、戳的文字固定）
  - 回應壞掉（非 JSON、缺欄位）＝不戳、exit 0
  - 對每條測試做改壞驗證（拿掉條件，測試要紅）
  - 斷言對假 sidecar 的請求只有「查」與「戳」兩種
- [ ] 2.6 `tests/unit/` 的文件測試：SKILL.md 與 docs 不含私人資訊樣式（沿用 `test_angareion_docs.py` 的做法）

## 3. 分發（跨 repo；PMO 排）

- [ ] 3.1 MyLinuxPool：映像建置用 tool-pins 同一個 sha 多裝 `aicomm`、放 `skills/aicomm/SKILL.md`（照 `docs/angareion.md`「說明檔（skill）」）
- [ ] 3.2 Mac：PMO 與各 PM 的安裝步驟（`uv tool install` 同 sha＋說明檔位置）——現況【未知】，先查
- [ ] 3.3 Mac 安裝步驟放 aicomm 設定檔（`nowake` 放 PMO 的 AI 名）；**由 PMO／本人執行，agent 不自己寫這個檔**
- [ ] 3.4 各環境說明（`--append`）加一行：溝通用 `aicomm`

## 4. harness（下一次改版時；不單獨出版本，除非 V6 實測不可靠）

- [ ] 4.1 `KNOWN_CAPABILITIES` 加 `comm`（`tests/unit/test_atelier_harness.py`）
- [ ] 4.2 pm／impl／rev 的下一版 `capabilities` 加 `comm`；正文只寫能力

## 5. MyPMO（別的 repo；只列）

- [ ] 5.1 `dispatch.py reply` **對 Mac team** 改為呼叫 `aicomm send`，由 PMO 決定叫醒失敗要不要保留 exit 2；**worker team 保留現行**（經轉發埠）
- [ ] 5.2 觀察一個 PM 週期後 Mac 路徑的舊 `reply` 標 deprecated；`send`／`poke`／`watch`／`open` 保留
- [ ] 5.3 worker team 的叫醒長期歸屬（PMO 經轉發埠，或 worker 上的 aicomm 自己叫醒同台 PM）——另案

## 6. 驗收

- [ ] 6.1 拋棄式環境端到端：A 送給閒置的 tui B → B 被戳 → B 跑 `inbox` 讀到；閒置的 headless B 同樣被戳；B 在 `nowake` 名單上 → 不戳、訊息照樣讀得到；B 在 `running` 時送 → 不戳、B 之後讀到；收件人在別台 → 不戳、`inbox --wait` 讀到
- [ ] 6.2 實測不碰真 Session、不讀真 credential

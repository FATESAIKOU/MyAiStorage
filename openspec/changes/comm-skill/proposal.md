## Why

期 4 把 PMO 與各 PM 換進 sidecar 之後，PMO↔PM、PM↔隊員改用 Angareion 溝通（MyPMO#163）。現在每送一則訊息要做**兩步**：`angareion send` 寫進 channel，再用 sidecar 的 poke 叫醒閒置的 tui agent；收信的一方要記得用 `angareion read`／`wait` 且「收訊不加 `--channel`」。本人 2026-10-24（MyAiStorage#212）：要把 sidecar 的操作部分變成 skill，告訴 PMO／PM／隊員怎麼溝通（經 Atelier 分發），「不用隨時記得要發兩邊」。

出處分開寫：MyLinuxPool#346 的原文是「sidecar 只是控管 agent 的服務層，不能依賴團隊的定義」（以及 open 直通的三條理念）；「sidecar 與 Angareion 各自只守自己對外的介面、不互相依賴」是 MyAiStorage#212 的說法。本設計的分層符合兩者。所以「寫進 Angareion 之後要不要叫醒」的判斷不能放進任一邊，只能放在**兩者之上的一層**——這個 skill。

本 change 只做設計（proposal／design／spec／tasks），**不寫程式**。設計經非作者審查、PMO 向本人說明，本人同意才實作（b 級）。

## What Changes

- 新增一個 skill（暫名 `aicomm`，指令與說明檔同一個 distribution、同一個 sha 一起釘），對外**只有兩個指令**：
  - `aicomm send`：寫進 Angareion；寫成功後，對收件人（`--to`＋`--mention`）中「在本機 sidecar、`state` 為 `idle` 或 `running`（不分 tui／headless）」的，盡力戳一下（固定短句，不含訊息內容）。預設是不中斷的 poke（實測範圍與結果見 design；PMO 2026-10-24 依本人字面判定），`--interrupt` 才中斷。`--no-wake` 不叫醒。
  - `aicomm inbox`：讀寄給自己的信（固定不加 `--channel`），可選擇先阻塞等。
- 叫醒的判斷（誰在本機 sidecar、閒不閒、可不可以叫、失敗怎麼辦）全放在 skill；Angareion 與 sidecar **不改**。
- `--channel` 必填；**所有角色用同一份、不依角色差異化**。沒有 `nowake`、沒有設定檔：節制叫醒靠 SKILL.md 軟規則（預設不中斷；隊員基本上不要中斷 PM、PMO；「收到」類回覆 `--no-wake`）；要硬控制之後走 sidecar open 帶白名單（MyLinuxPool#346）。代價：任何同台 agent 都能叫醒 PMO，與 M2 D6 的 watch 規則並存（watch 管秘書派工急件、aicomm 管 AI 間一般溝通），本人 2026-10-24 接受。sidecar 位址固定用預設 loopback 位址。
- 工單號是選填（`--ticket`），不帶也能送一般訊息。
- 分發：指令與說明檔跟著 `angareion` 同一個 tool-pins sha 交到各 agent；harness 只在下一次改版時加一個中性能力名 `comm`（見 design D3）。
- 與 MyPMO 的 `dispatch.py`：`send`（派工）與 `poke` **並存**；`reply`（送＋戳）對 Mac team 換成 `aicomm send`，worker team 保留現行（見 design D4）。

## Capabilities

### New Capabilities

- `comm-skill`：AI 之間溝通的 skill 的行為（兩個指令、叫醒的判斷與失敗語意、與 Angareion／sidecar 的界線）。

### Modified Capabilities

（無。Angareion 與 Atelier 的現行規格不改；若 design D3 選了選項 B，會再開 Atelier 的 delta。）

## Impact

- 本 change 的產出（公開、無私人資訊）：`openspec/changes/comm-skill/`。
- 未來實作（不在本 change，見 tasks.md）：`src/aicomm/`（新，只用標準函式庫）、`skills/aicomm/SKILL.md`、`pyproject.toml` 的 console script、`tests/unit/`、`docs/aicomm.md`。
- 不動：`src/angareion/`、`src/atelier/`、Angareion 協定（`angareion: 1` 不升版）；sidecar 與 MyPMO 的程式（只讀）。
- 跨 repo（只列，不在本 change）：MyLinuxPool 的映像建置要多裝一個指令與說明檔；MyPMO 的 `dispatch.py reply` 改為包 `aicomm send`；各 PM／PMO 的環境說明加一行。
- 私人資訊：本 change 不寫帳號、機器位址、憑證、真的 session id、MyBrain 內容。

## Why

工單 MyAiStorage#311（設計先審、本人同意才實作）。發信方現在沒有辦法查「我送出的那則，對方讀了沒」：`angareion` 與 `aicomm` 都只有收信與叫醒，沒有任何介面回答已讀與否；發信方只能猜，或在對方沒回時重送、重戳，兩者都會多花一輪模型額度，還會製造重複訊息。

本人 2026-10-31 的方向（PMO 轉達，本人原文未見）：

- 開 openspec 變更；「已讀」＝收件人的游標越過那則訊息；狀態是三態（已讀／未讀／未知），查不到時答未知、不答未讀。
- 發信方只在**明確未讀**時才中斷 poke；未知一律不戳。
- 先做**同機唯讀查詢**（C-min，零寫入）；跨機之後再議。
- `aicomm` 補「只戳不送」的指令；`aicomm send` 的中斷／不中斷兩種模式在文件寫清楚。

repo 現行條款明文禁止 ack 與已讀回條（`openspec/specs/angareion/spec.md:224`、`docs/angareion.md:5`、`skills/aicomm/SKILL.md:78`），所以本變更的第一步是提案與裁定，不是寫程式；本 change 只設計，**不寫程式**。

## What Changes

- `angareion` 新增**唯讀查詢指令 `status`**（第七個介面）：給 `--channel`、`--message <訊息 id>`、`--to <AI 名>`，回答「這則訊息對 `<AI 名>` 是已讀、未讀、還是未知」。它 MUST NOT 寫入任何東西、MUST NOT 推進任何游標、MUST NOT 有回條。
- 修改「六個介面」條款為「七個介面」，並把「MUST NOT 有 ack、已讀回條或限流」改寫成：不新增寫入、不新增回條機制；唯讀查詢不違反此條（見 design D7）。`log` 的「不動游標」條款沿用到 `status`。
- 統一用詞：「已讀」在本 repo 只指「收件人的游標越過」；`spec.md:460` 的「已讀狀態」改稱「游標」。
- `aicomm` 新增**只戳不送**指令（作者提議名 `wake`，待本人／PMO 拍板，見 design D5）：只對本機 sidecar 的 agent 發出戳，不寫 Angareion、不產生訊息。
- `aicomm send` 的三種模式（預設不中斷／`--interrupt`／`--no-wake`）寫進 `docs/aicomm.md` 與 `skills/aicomm/SKILL.md`，與程式行為對齊（design D6）。
- `aicomm` 的指令清單從「三個」改成「四個」（send、inbox、log、wake）；`status` 不放進 aicomm（`aicomm` 不做查狀態，見 design D7）。

## Non-goals

- 不做跨機的已讀公布（游標或回條上公開）——跨機之後再議（方案 A）。
- 不做逐則 reaction（方案 B）：所有身分共用同一把 token，reaction 分不出是哪個 agent 讀的（推論，見 design D7）。
- 不做「慣例回覆」的機制化（方案 D）；它只是現行 `--no-wake` 規則的正式化，本 change 不新增規則（design D7 與判斷點）。
- 不改 sidecar（它的 `poke` API 與 `idle`／`running` 規則不變）。
- 不做叫醒延遲的量測。
- 不改 Angareion 協定（`angareion: 1`）與訊息格式；不改 `read`、`wait`、`log` 的行為與游標檔。

## Capabilities

### Modified Capabilities

- `angareion`：「六個介面」改名為「七個介面」（RENAMED＋MODIFIED，新增唯讀查詢 `status`）；「輸出與結束碼」補 `status` 的 exit 3／4 與它和 `wait` 的 3 並存的說明（MODIFIED）；`log` 的本機快取條款中「已讀狀態」用詞統一為「游標」（MODIFIED）；「給 agent 的說明」（MODIFIED，改七個指令並寫三態判讀）；「後端可以替換（設定檔指位）」（MODIFIED，改七個介面、列舉供 `status` 使用、列舉提前停止的向後相容擴充）。新增一條 requirement：唯讀查詢 `status`（ADDED）。
- `comm-skill`：「對外只有 send、inbox、log 三個指令」改名為「對外只有 send、inbox、log、wake 四個指令」（RENAMED＋MODIFIED；「單獨戳 agent」的禁令改寫為「只有 `wake` 可以單獨戳」）；「log 薄包」的 Scenario 與文字（MODIFIED，「三個指令」改「四個指令，含 `wake`」）；新增 requirement：`wake` 只戳不送（ADDED）與「送出的三種模式」文件要求（ADDED）。

（無新增 capability。）

## Impact

- 未來實作（本 change 只列，不在本 change 做）：
  - `src/angareion/`：`cli.py`（`status` 子指令、`EXIT_*` 增加未知碼；`EXIT_TIMEOUT` 仍是 3）、`client.py`（查詢函式，不呼叫 commit）、`backend.py`／`github.py`（重用既有「列舉」動作，見 spec:205；為了「找到即停止」需對列舉介面做向後相容的提前停止擴充，`log` 行為不變，見 design D4.3；這不是寫入動作，本 change 仍不新增任何寫入動作）、`state.py`（唯讀的游標檔解析，不沿用會把壞檔當空的 `_load`，見 design D4.1）。
  - `src/aicomm/cli.py`：`wake` 子指令（重用 `_wake_one`，`cli.py:169`）、用法錯訊息（`cli.py:319` 的「只有 send、inbox 與 log 三個指令」、`:83` 的「（aicomm 只有 send、inbox 與 log）」改成含 wake）、`send` 的 help 文字（`:94`、`:104`、`:105`）。
  - `tests/unit/`：`test_angareion_status*.py`、`test_aicomm_wake*.py`；既有 `test_aicomm_docs.py`、`test_angareion_docs.py` 的守護字串要跟著改。
- 文件與 skill（實作時一起改，避免 main 上的說明與行為不一致）：
  - `openspec/specs/angareion/spec.md:170-171`（結束碼）、`:223-226`（六個介面）、`:460`（「已讀狀態」）。
  - `docs/angareion.md:3`（「對外只有六個介面」）、`:5`（「沒有 ack、已讀回條」）、`:11`（「## 六個指令」）。
  - `skills/angareion/SKILL.md:8`（「對外只有六個指令」）、`:21`（「## 六個指令」）。
  - `skills/aicomm/SKILL.md:78`（「查狀態」列在「不做」裡）、`:70-72`（軟規則，與三種模式的說明對齊）、`:52-58`（「送達與叫醒」段）。
  - `docs/aicomm.md:33`（同一個身分只留一個讀者；補 status 不屬於這條）、`:50-51`（叫醒對象）。
  - `docs/angareion.md:29`（同一身分只留一個讀者，同款說明；補 status 不屬於這條）、`skills/aicomm/SKILL.md:31`（同款，補一句）、`skills/angareion/SKILL.md:59`（同款，補一句）。落點與 design D8 一致。
- 不動：Angareion 協定（`angareion: 1`）、TS 端 MyAiEntry、sidecar、MyPMO 的程式、`openspec/specs/atelier/` 的已發布版本檔（append-only）。
- 不動 `openspec/specs/` 的主 spec：本 change 只放 delta，archive 時才套用。
- 公開 repo：內容不寫帳號、機器位址、憑證、真的訊息內文與真的 session id；範例用假的 channel 名與假的 AI 名。

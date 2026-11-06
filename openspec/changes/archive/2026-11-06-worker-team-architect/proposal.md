## Why

本人 2026-10-31 的新規矩（PMO 轉述）：每個團隊設**架構師**，與 PM 同級。PM 管需求、行為與品質；架構師管模組切割、函式與介面設計、SOLID 與該層級的審查，**不寫實作**。

本人於 10-31 20:3x 補充：**每個專案只有一位架構師**（不是每張單開一個），固定名（例 `<專案>-arch-<team>`）、長期常駐，同時負責該專案所有單的架構設計與審查，並有自己的交接檔（同 PM 的 PM-HANDOFF）。架構師是「專案的常駐席位」，不是「按單開關的隊員」。

現況：架構師目前只是 PM harness（`atelier/worker-team/pm/v2.md`）裡一個派給隊員的職務（L31 把它列在「隊員」之中），沒有自己的 harness，也沒有交接與容量的規矩。這個 change 只做**設計**：新增 worker-team 架構師 harness 的定義、與 PM 的分工、產物歸屬、「必經」的觸發條件、交接與容量、命名（`src/ainame/__init__.py` 的 `SINGLE_ROLES`）的待裁定，以及連動的 PM 與 PMO 版本。依 b 級流程，設計 PR 交本人向本人說明後才 merge；實作另開單。

修正輪 4 依第三方的第三輪確認（`REPORT-313-rev.md`「第三輪」）修正 M-A、M-B、L-A～L-D 與 nit；修正輪 3 依第三方的第二輪確認（`REPORT-313-rev.md`「第二輪」）修正 M1–M6、L1–L8；修正輪 2 依架構師的架構面審查（`REPORT-arch-313`）整體採納其建議，寫成「作者提議」。需要本人決定的六點整理在 `design.md` §0 的決定表（第 6 點 D27 不阻擋落檔），**閘門：PR-A（arch v1）、PR-B／PR-C（pm v3、pmo v4）都要等第 1–5 點本人確認（D21 是第 2 點的前置）；PR-D（ainame）只看第 4 點；第 6 點不阻擋**。

## What Changes

- **新增 `atelier/worker-team/arch/v1.md`**（id `atelier:worker-team-arch@v1`，`role: arch`，`team: worker-team`）：架構師的職務檔，`know`／`do`／`judge`／`dont`／`capabilities`。草稿全文在 `design.md` §10；定稿在實作 PR。架構師有兩種模式，都算必經：核准者模式（預設：隊員或 PM 派的作者寫設計，架構師核准）與作者模式（跨模組或改總圖的 b，架構師親寫，結構面改由另一模型的審查者照清單審，PMO 向本人說明）。
- **pm 連動（新版本 `atelier/worker-team/pm/v3.md`，append-only，不改 v2）**：架構師從「隊員」改為與 PM 同級的常駐席位；b／c 的設計與介面 PR **必經專案架構師**，PM 不越過架構師定介面；「必經」的觸發條件與判定；產物歸屬（proposal 與 specs 歸 PM，design 的結構歸架構師）；實作 brief 引用已核准的設計；結構審查與合併的先後；排隊由 PM 定；專案還沒開架構師席位時照 v2 的方式做，並在工單上註明「架構師：未開席」。逐句對照見 `design.md` §11。
- **pmo 連動（新版本 `atelier/pmo/pm/v4.md`，append-only，不改 v3）**：本人 10-31 補充的範圍——`atelier/pmo/pm/v3.md` L33「權限只有五級」要加上與 PM 同級的**專案架構師**（仍寫五級，D17）；L18 與 L98 的「各 PM」措辭；L34、L78 的「設計」協調不越過架構師定介面（跨 repo 介面僵持由 PMO 裁）；L105 的 b 級設計 PR 要求先有架構師的設計與非作者審查；L155 的 dont 補「也不越過架構師定介面」；代理架構師與書面例外的 PMO 一方義務。裁決規矩全文只放 spec 與 arch v1，pm v3 與 pmo v4 不重述。`pmo` 的 v4 只由 harness 的關鍵句測試守住，主 spec 不加 requirement。逐句對照見 `design.md` §11.2。
- **docs 與測試連動**：`docs/atelier/harness.md`（role 表已列 `arch`，補一段說明 worker-team 的架構師）；`tests/unit/test_atelier_harness.py`（新增 `ARCH_KEY_SENTENCES`，併入 `findings_for` 與 `KEY_SENTENCE_CASES`；pm v3 的關鍵句沿用 `PM_KEY_SENTENCES`，並新增整句）。
- **spec**（delta 見 `specs/atelier/spec.md`）：
  - ADDED 一條 requirement「架構師職務（arch，worker-team）」，因為「每專案一位、必經、不兼任實作、交接、產物歸屬、代理」是 MUST 等級的規矩，主 spec 目前沒有。
  - MODIFIED 主 spec 的「PM 職務（`v1`：有隊員的 PM）」（`openspec/specs/atelier/spec.md:178`–`:207`）：v1／v2 的「b 與大的 c」保留並加限縮 `（v1、v2）`（M2），另加 `v>=3` 的「b 與全部 c 必經架構師」。用 MODIFIED 而非 ADDED，因為主 spec 已有這條 requirement，ADDED 會並存、互相矛盾（H3，見 design §11.3）。
  - 不改 `spec.md:7`（`arch` 已在 role 列表中）；`pmo` 的「五級」主 spec 沒有對應 requirement，不加。
- **命名（待裁定 D12，見 design §7）**：`src/ainame/__init__.py` 的 `SINGLE_ROLES` 目前是 `("pm", "secretary")`，`arch` 必須接序號（`ainame` 文件 L16-18）。本人的例名 `mylinuxpool-arch-m1` 沒有序號，與現行語法不合。架構師建議保留序號（B），不建議現在改成 A。這一步只列影響、不改程式。

## Non-goals

- 不開 arch 隊員、不實作（不寫 `arch/v1.md` 的檔案、不改測試、不改 `src/`）。
- 不改現有 harness：`pm/v2.md`、`impl/v1.md`、`rev/v1.md`、`pmo/pm/v3.md` 全部不動（append-only；pm 的連動是新版本 `pm/v3.md`，在實作 PR 才寫）。
- 不改模型輪換表（由 PMO 維護）；不寫死模型名。
- 不碰 sidecar／MLP 的啟動機制（`-arch-` id 的交付、sidecar 的 harness 對應，列為待確認，另開單）。
- 不決定 MLP 既有架構師的細節（本人說 MLP 的那個就是 MyLinuxPool 的專案架構師、#424 是它第一件；本 change 未查 MLP 的 repo，見 design §13 D20）。
- 不在本 change 裁定決定表的六點；那是本人（或 PMO）的事（design §0）。

## Impact

本 change 的產出（MyAiStorage，**公開 repo**：不含私人資訊、帳號、額度、機器位址、模型輪換表內容）：
- `openspec/changes/worker-team-architect/`：`proposal.md`、`design.md`、`tasks.md`、`specs/atelier/spec.md`（delta）、`.openspec.yaml`。

未來實作（不在本 change，見 `tasks.md`）與落點（行號於 2026-10-31 的 `main`＝worktree 起點 `9d6ce16` 核對）：
- `atelier/worker-team/pm/v2.md`：L31（把架構師列在「隊員」中）、L32（架構與設計交架構師）、L50（派給不同的人）、L77–79（收到方向／b 與大的 c 交架構師）、L84（開單拆單）、L89–92（派工 brief）、L106（審查核對清單）、L115（合併）、L121（收尾：歸檔設計）、L138（停下來問 PMO）。以上在 v3 中以新版本對應，v2 不改。
- 新增 `atelier/worker-team/pm/v3.md`、`atelier/worker-team/arch/v1.md`。
- `atelier/worker-team/impl/v1.md:57`（「發現工單與程式現況或規格不符」）：實作隊員要引用已核准的設計；結構問題回報架構師，不自己改規格。（連動句在 impl 的新版本才寫，本 change 只列。）
- `atelier/worker-team/rev/v1.md:34`（「不審自己的作品」）與 `:38`（壓力測試、設計缺陷）：審查分工改成「結構疑點回報並標交架構師、不裁決；正確性與測試仍由審查隊員」，列為新版本的連動。
- `docs/atelier/harness.md:19`（role 表）；`docs/atelier/harness.md:3`（一頁格式說明）。
- `tests/unit/test_atelier_harness.py:88`（`PM_KEY_SENTENCES`）、`:97`（`IMPL_KEY_SENTENCES`）、`:101`–`:111`（REV）、`:437`–`:441`（依版本套關鍵句的迴圈）、`:711`–`:716`（`KEY_SENTENCE_CASES`）。
- `openspec/specs/atelier/spec.md:7`（`role` 列出 `arch`，且說明「審查職務是 `rev`，不再借用 `arch`」，與本 change 一致）、`:178`–`:190`（PM 職務 requirement，`:181` 的「b 與大的 c 先交架構師」）。
- `src/ainame/__init__.py:17`–`:18`（命名語法：arch 必須接序號）、`:36`（`SINGLE_ROLES`）、`:114`（檢查分支）；`tests/unit/test_ainame.py:245`（斷言 `SINGLE_ROLES == ("pm", "secretary")`）。
- `atelier/pmo/pm/v3.md`（連動，新版本 `v4` 才寫）：L18（「各 PM」的統籌）、L31（組織）、L33（「權限只有五級」）、L34（PMO 不越過 PM 直接指揮隊員）、L78（跨 repo 的設計由 PMO 協調）、L98（「各 PM」的措辭）、L105（b 級設計 PR 向本人說明）、L155（dont：不越過 PM 直接指揮隊員）；L147 附近的「一個 PR 好了的標準」不動。`PMO_V3_KEY_SENTENCES` 的四句（`tests/unit/test_atelier_harness.py:139`）沿用，v4 不得刪。
- `docs/atelier/harness.md`：L5（現況：`pm/` 有 v0–v2，`impl/`、`rev/` 有 v1）與 L7（`pmo/pm/` 的現況：到 v3）在實作時補上 `arch/` 與新版本；L3 的 spec 指標不動。
- **注意**：本人的補充提到「『設計先審』那幾句」，現行 `pm/v2.md` 全文搜不到「設計先審」這四個字；與它最接近的是 L78「b 與大的 c：交架構師寫設計…設計經非作者審查、逐項修正後，交 PMO 向本人說明」。以 L78 為準對照（推論：本人指的是同一段）。
- sidecar（`-arch-` id 的 harness 對應）：不在本 repo 的 change 內，待確認。

## 0. 本 change（設計，b 級）

- [x] 0.1 寫 proposal、design（含決定表、harness 草稿、連動逐句表、決策清單）、specs delta。（修正輪 2 已更新，見 REPORT-313-impl「修正輪 2」）
- [x] 0.2 `openspec validate worker-team-architect` 與 `--strict` 的原樣輸出（修正輪 3 後重跑，貼進 design §16）。
- [x] 0.3 草稿 §10 用既有 `findings_for` 檢查與隔離副本上的 `pytest`：結果見 design §16（修正輪 3 重跑）；關鍵句逐句以 `_body()` 實測見 design §9。
- [ ] 0.4 設計 PR 交本人向本人說明（經 PMO）；**本人同意後才 merge**。未經同意不開 0.x 之後的實作。
- [ ] 0.5 **本人裁定決定表（design §0）第 1–5 點**：範圍單位（第 1 點，H-A3）、作者／核准者模式（第 2 點，H-A1；**前置 D21**：先裁定作者模式的審查者角色與架構清單，於第 2 點確認時一併裁定）、衝突依問題種類裁決（第 3 點，D9）、D12 `SINGLE_ROLES`（第 4 點，建議 B）、不可用時開代理或靠書面例外（第 5 點，M-A4）。**閘門統一規則**（與 design §0 相同）：PR-A、PR-B、PR-C 都要等第 1–5 點確認；PR-D（ainame）只看第 4 點；第 6 點（D27，commit 權）不阻擋任何一個，見 0.5a。
- [ ] 0.5a 本人裁定其餘待決項（D18、D27 的裁定人另註）：D5（觸發範圍：全部 c，定義見 design §2.1）、D14（輪換表能否寫推理強度）、D17（PMO 五級寫法）、D18（sidecar 另開單，裁定人：本人／PMO）、D20（MLP 的架構師關係，未驗）、D21（rev `v2` 是否審設計文件；第 2 點的前置，見 0.5）、D22（席位開關與派工歸屬）、D23（PMO 與架構師的溝通路徑）、D24（架構師不可用與爭議時限）、D27（架構師的 commit 權，決定表第 6 點，M-A7／M6；裁定人：PMO 先建議）。
- [ ] 0.6 把 Opus medium 的意圖交 PMO，在輪換表或開席位時採用（design §8.1；只交意圖，不寫進 harness 正文）。
- [ ] 0.7 `specs/atelier/spec.md` 的 MODIFIED 標頭與主 spec 逐字一致；內文允許為限縮而改（第一條 bullet 加 `（v1、v2）`，M2）。以 `sed -n '178,207p'` 的原文為基準，逐條核對「只多了限縮」；0.4 前再核對一次。

## 0.P 前置（不在本 change）

- [ ] P.1 環境說明補上架構師交接檔的位置（另單；現行 Mac 環境說明沒有這一條，design §4 與 D11）。**PR-A 落檔前必須完成**，否則 arch 檔的「交接檔位置看環境說明」指向空白。

## 1. 實作 PR 切分（每一步都要讓 main 不更糟）

每個 PR 的共同完成判準：`uv run pytest tests/unit/test_atelier_harness.py` 綠（貼原樣輸出與 exit code）；改壞驗證（在副本裡）至少一處轉紅；`git status` 只有該 PR 的檔案；背景行程收掉。

### PR-A：新增 `atelier/worker-team/arch/v1.md` ＋測試（c 級；架構師職務）

- 前置：0.5 決定表第 1–5 點已由本人確認（第 3 點 D9 的依問題種類裁決是 arch 草稿 `judge` 的前提；第 1、4 點＝範圍單位與 D12 的結果已寫入草稿與 spec；第 5 點的代理句寫在草稿 `know`）；D21 已裁定（第 2 點的前置；草稿 `do` 不寫作者模式的啟用條件，模式由 PM 在工單上指定）；D18 已開單（依賴順序見 §2，`-arch-` 的席位要能開）；P.1 完成。
- [ ] A.1 新增 `atelier/worker-team/arch/v1.md`（內容＝design §10 草稿，定稿時只改措辭，**不改關鍵句**）。
- [ ] A.2 `tests/unit/test_atelier_harness.py`：新增 `ARCH_KEY_SENTENCES`（design §9 的十二句**整句**，逐字，含作者模式例外句；檢查只讀 `_body()` 的四章，導言段不算）；併入 `findings_for` 的依版本迴圈（`role == "arch" and is_v1_or_later`）；併入 `KEY_SENTENCE_CASES`（每句刪掉要紅）。
- [ ] A.3 `docs/atelier/harness.md:5`（現況句補 `arch/` 有 v1）與 `:19`（role 表補一句「worker-team 的架構師，每專案一位」）。
- [ ] A.4 完成判準：格式檢查綠；`ARCH_KEY_SENTENCES` 的每一句刪掉都轉紅（貼其中三句的紅燈輸出）；`arch/v1.md` 的 `findings_for` 無 error；關鍵句以外的改壞（例如把 `models.floor` 改成寫死的模型名）轉紅。
- 拒絕條件：正文出現工具名、模型名（含 `Claude`、`Opus`）、其他專案名、`/Users/`、`~/` 必須紅。

### PR-B：worker-team PM 新版本 `pm/v3.md` ＋測試（c 級）

- [ ] B.1 新增 `atelier/worker-team/pm/v3.md`（依 design §11.1 逐句表，含 2a 的「架構師席位不可用」句與 2b 的過渡句；**不改 v2**）。
- [ ] B.2 測試：`PM_V3_KEY_SENTENCES`（`PM_KEY_SENTENCES` ＋整句 `PM 不越過架構師定介面。`、`工單寫明「架構師：必經」。`、`工單寫明「架構師：免（理由）」。`、`PMO 席位名單尚無該專案架構師時，照 v2 的方式做，在工單上註明「架構師：未開席」。`）併入依版本迴圈（`role == "pm" and version >= 3`）；`審查一律非作者` 仍必須在 judge 或 dont。
- [ ] B.3 完成判準：v3 格式檢查綠；v2 檔的 SHA 不變（append-only：`git diff --name-status` 只有 A）；每句關鍵句各刪一次轉紅。
- 依賴：A 合併後（arch 的名字要存在，pm v3 才能引用「專案的架構師」）；D12 與範圍單位已定（名字格式）；D18 已開單（`-arch-` 能交付）；impl v2 與 rev v2 同時或更早（避免 pm v3 先上線、實作與審查還不認「交架構師」）。若採 2b 過渡句，B 可提前到 A 之後、impl v2 之前。

### PR-C：PMO 新版本 `atelier/pmo/pm/v4.md` ＋測試（c 級）

- [ ] C.1 新增 `atelier/pmo/pm/v4.md`（依 design §11.2 逐句表，含 L18、L98、L155 三條，以及代理與書面例外的一方義務；**不改 v3**）。
- [ ] C.2 測試：`PMO_V4_KEY_SENTENCES` = `PMO_V3_KEY_SENTENCES`（全部保留，`tests/unit/test_atelier_harness.py:139`）＋整句 `不越過 PM 直接指揮隊員，也不越過架構師定介面。`＋整句過渡句 `PMO 席位名單尚無該專案架構師時，照 v3 的方式做，在工單上註明「架構師：未開席」。`（M3）；v4 的 `pmo_version_4_or_later` 分支加入迴圈。`PMO_V2_KEY_SENTENCES` 也必須仍在 v4 裡。
- [ ] C.3 完成判準：v4 格式檢查綠；`PMO_V2`／`PMO_V3` 的句子在 v4 都在；刪掉 `不越過 PM 直接指揮隊員，也不越過架構師定介面。` 轉紅。
- 依賴：A 合併後；與 PR-B 同步（M3）：兩者都可以在 A 之後、impl v2 之前附過渡句提前（pmo v4 的過渡句與 pm v3 的 2b 同義），或都等 impl v2、rev v2。兩者不得一個附過渡句、另一個不附。

### PR-D：ainame 命名（待 D12 裁定；c 級或 d 級，依 ainame 規矩）

**預告（H4）**：若選項 A，下列測試與向量**必定**要改，不只 `test_ainame.py:245`；若選 B 或 A′，只有 A′ 需要改 `_step5` 與 docstring，B 不改程式。

- `src/ainame/__init__.py:16–18`（docstring）、`:36`（`SINGLE_ROLES`）、`:110–125`（`_step5` 分支，L114）。
- `tests/unit/test_ainame.py:34`（`_REFERENCE_RE` 第二份文法副本）、`:67`（合法例 `myaistorage-arch-m1-2` 變非法）、`:203`（`format` 斷言）、`:245`（`SINGLE_ROLES` 斷言）。
- `tests/vectors/ainame-vectors.json:39–45`（`myaistorage-arch-m1-2` 的向量）。
- `tests/unit/test_angareion_address.py:28`、`tests/unit/test_angareion_messages.py:161`（合法名清單）。
- `docs/angareion.md:63`、`:66`（文法說明）。
- 其他 repo 的釘住副本：**只引用、不碰**，列為影響。
- 現開的 `myaistorage-arch-m1-1`：選 A 時變非法名，改名重開（sidecar 側，另單）；選 B 不受影響。

步驟：

- [ ] D.1 若選項 A：依上面清單改程式與測試；`SEQ_MAX` 不改（與角色無關）。若選項 A′：改 `_step5` 的序號規則與 docstring，並同步測試與向量。
- [ ] D.2 若選項 B（架構師建議）：不改程式；在 PMO 席位名單的命名說明寫明 `<專案>-arch-<team>-1`。
- [ ] D.3 完成判準：ainame 測試綠（貼原樣輸出與 exit code）；向量檔與兩份 angareion 測試同步改（A／A′）；`mylinuxpool-arch-m1`（選項 A）與 `…-arch-m1-1`（選項 B）各自的 parse 結果原樣貼出；改壞（把 `arch` 從 `SINGLE_ROLES` 拿掉）轉紅（選 A）。
- 依賴：D12 裁定（決定表第 4 點）；範圍單位（第 1 點）定案。

### 不在本 change、另開單（不排進上面的 PR）

- sidecar：`-arch-` 的 id 交付 arch harness（按 agent 交付；只有架構師拿到，不給 PM 與隊員）；席位開關與代理開席的實作。D18、D22、D24。
- impl／rev 新版本（design §11.3）：impl `v2` 加「介面與模組切割與設計不符時回報 PM 轉架構師」，並實作報告逐條對到契約編號（C-1…）；rev `v2` 加「結構疑點回報並標交架構師、不裁決」。各自一個 PR，依賴 A。
- rev `v2` 是否加「審設計文件」：D21，待裁定。
- PMO 與架構師的溝通路徑：D23，待裁定（只有裁定後才寫進 pmo v4 以外的 harness）。
- 架構師的 commit 權（只限設計文件、總圖、決策清單的路徑）：D27，待裁定；裁定後寫進專案 `AGENTS.md`，不進 harness。
- MLP 的架構師既有交接檔與本 harness 的對照（D20，未驗）。

## 2. 排序與各步的 main 不更糟（H-A4 重排）

依賴順序：**P.1 → 決定表第 1–5 點（含 D21，第 2 點的前置）與 D12 → D18（另單，列為依賴）→ A → impl v2、rev v2 → B、C**。D 獨立，只看第 4 點（D12）。

- P.1（環境說明）先；決定表第 1–5 點（含 D21）與 D12 定案；D18 開單（否則 `-arch-` 的席位開不起來）；A 其次（新檔＋測試；不影響既有 PM／PMO 檔）。
- impl v2、rev v2 在 B、C 之前：B、C 生效後，觸發單會交架構師，實作與審查端要已經認得「回報／交架構師」的通路。
- B、C 可以並行，都依賴 A；兩者同步：都附過渡句（pm v3 的 2b、pmo v4 的過渡句）提前，讓未開席的專案照 v2／v3 的方式做；或都不附，等 impl v2、rev v2。
- D 獨立，等第 4 點（D12）定案。
- 每個 PR 都要能單獨 merge 並讓 main 綠；任何一步紅就停下回報，不跳過。

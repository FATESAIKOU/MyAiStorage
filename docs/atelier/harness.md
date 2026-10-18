# harness 檔的格式（一頁）

harness ＝ 一個職務一個版本的 UTF-8 Markdown 檔，放 `atelier/<team>/<role>/v<N>.md`；識別 `atelier:<team>-<role>@v<N>`——識別、版本與路徑是同一件事的三個寫法。frontmatter 的 `team` 欄位說的是**系列**（欄位名沿用 `team`，程式與檔名都不改）。spec 全文：`openspec/specs/atelier/spec.md`（`v1` 起的變更在 `openspec/changes/atelier-pm-harness-v1/specs/atelier/spec.md`，歸檔後併進主 spec）；檢查在 `tests/unit/test_atelier_harness.py`（格式＋append-only；接 CI 是 tasks 2.6，未做）。

**現況**：`atelier/worker-team/pm/` 有 `v0.md`（期 1 單人暫定）與 `v1.md`（有隊員的 PM）。**`v0` 已被 `v1` 取代**——`atelier show atelier:worker-team-pm` 不給版本讀到 `v1`。`v0` 檔因為 append-only 不改（`status` 也不原地改），所以它不會自己標 `deprecated`。

## frontmatter（檔的第一個區塊，YAML；OKF v0.2 對齊，不認得的欄位保留）

| 欄位 | 必填 | 值 |
|---|---|---|
| `type` | ✓ | `Harness` |
| `id` | ✓ | `atelier:<team>-<role>@v<N>`；與 `team`／`role`／`version`、檔名一致 |
| `title` | ✓ | 一行標題 |
| `description` | — | 一行摘要 |
| `status` | ✓ | `draft`／`stable`／`deprecated` |
| `team` | ✓ | `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`）；frontmatter 的欄位名是 `team`，它說的是**系列** |
| `role` | ✓ | `pm`／`arch`／`impl`／`test`／`rev`（審查職務是 `rev` 自己的角色） |
| `version` | ✓ | `v` 加非負整數；與檔名、`id` 一致 |
| `models` | ✓ | `floor`（模型識別碼或 `null`）＋`preferred`（有順序的清單，可空）；`v0`／`v1` 的值留空（未指定，由環境決定） |
| `generated` 等 | — | 不認得的欄位保留（例：`generated: {by, at}` 產生者註記） |

## 本文：五個 H2 章節（缺一不可）

| 章節 | 寫什麼 |
|---|---|
| `know` | 背景與「為什麼」：在哪、harness 怎麼載入、怎麼收發（只寫能力，指令看環境說明）、工單與協作、模型 |
| `do` | 這個職務實際怎麼做：從收到指示到開 PR 與回報的步驟 |
| `judge` | 什麼算做完、什麼時候停下來問、品質怎麼判斷 |
| `dont` | 界線與禁止事項（憑證、真機、main、繞過、假裝完成…） |
| `capabilities` | 能力需求；一個能力一個 `### <能力名>` 子節 |

- **能力子節**：子節內用**一行 YAML 或清單**寫參數，沒有參數就留空；**不寫散文**（用途放 `know`／`do`）。子節名不重複；未知能力名只警告、不擋（能力清單要能長）。
- **已知能力名**（`KNOWN_CAPABILITIES`）：`github`／`angareion`／`agora`／`channel`／`team`／`monitor`。`channel`／`team`／`monitor` 是**中性名**——`v1` 起的職務由 Mac 與 worker **共用一份**，回報通道、隊員控制、監看在兩邊是不同的東西，所以只宣告「需要什麼能力」，誰用什麼提供寫在該環境的說明。`angareion` 只有單一環境有：`v>=1` 的檔案宣告它**只警告、不擋**。
- **v0 職務檔**（`v0.md`）MUST 有 `angareion` 子節；一般職務 SHOULD 宣告自己真正需要的能力（v0 的三個：`github`／`angareion`／`agora`）。
- **MUST NOT 有 `angareion` 章節**（Angareion 是能力子節，不是章節）。
- `know` MUST 明寫**「專案自己的指示檔與工單規則優先」**。
- **每一個 `pm` 版本**（不只 `v0`）的 `judge`／`dont` MUST 有**「審查一律非作者」**。
- 內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表、第二大腦內容等私人資訊。

## `v>=1` 的內容限制（只寫能力，不寫工具名）

`v0` 是既成事實、append-only 不能改，這一節只驗 `v>=1`。範圍＝**正文**（`know`／`do`／`judge`／`dont` 合起來）：`capabilities` 的子節名與 frontmatter（例如 `generated` 的產生者註記）不算正文。

- MUST NOT 出現**具體的工具名、模型名**（工具與模型的名稱寫在各環境的說明／PMO 的模型輪換表）、**其他專案的名字**、**機器位址**（IPv4 樣式）、**使用者家目錄路徑**（`~/…`、`/Users/…`）。
- **通用系統路徑不算**（`/tmp` 要能用——`v1` 的「工作檔不放 `/tmp`」那句要用它）。
- 清單放測試一處、不收**一般詞**（`session`／`輪換表`／`通道`／`CI`／`git`／`commit`／`repo`／`ssh`／`brief`……規矩的句子本身要用），也不收**本專案自己的元件名**（`atelier`／`github`／`agora`）。比對前先遮掉 `CLAUDE.md`／`AGENTS.md` 兩個檔名（`know` 的 MUST 句一定要寫它們，而 `CLAUDE.md` 含 `claude`）。誤殺就改清單，不要放寬原則。
- `pm` 的 `v>=1` MUST 還寫到四個關鍵句——`不是權限`、`照 PMO 的模型輪換表`、`不指定頻道`、`長時間的指令`——**放在哪一段由 harness 自己安排**，只要 `know`＋`do`＋`judge`＋`dont` 合起來每一句至少出現一次。

## 怎麼載入（`v0` 與 `v1` 不同）

- **`v0`**：由 `atelier install` 寫進**使用者層級**指示檔（claude：`~/.claude/rules/`；opencode：`~/.config/opencode/AGENTS.md` 的管理區塊），同一個使用者的**每個** agent 都會載入。
- **`v1` 起：按 agent 交付**——開 agent 的一方（啟動那一個 agent 時）依它的角色把 harness 交給它，**只有 PM 角色拿 PM harness，隊員不拿**；交付只對那一個 process 生效，使用者層級的指示檔不再用作交付管道，也不會被動到。`v1` 的 PM harness **不得**用 `install` 交付（唯一例外是明寫 `--global`，且會印警告）——**這道守門（含 `--global` 旗標）PR 3 才實作**，在那之前 `install` 仍會照舊把 v1 寫進使用者層級，不要用它交付 v1。交付的介面在 `atelier render`（已實作，指令見 `docs/atelier/tool.md`）：啟動方在起 agent 前跑一次，把 `--out` 那個檔交給那一個 process。

## 版本與 append-only

- 一個版本一個檔（`v0.md`、`v1.md`…）；**內容有變 MUST 新增下一個版本檔**。
- **既有版本檔 MUST NOT 被修改、MUST NOT 被刪除**（append-only）；**新增版本檔（`A`）是唯一允許的變化**。檢查＝與 `origin/main` 的 merge-base 比對，對 **HEAD 與工作樹**各做一次 `git diff --name-status`：凡不是「新增 `v<N>.md`」的變化——修改（含 `chmod` 等模式變更）、刪除、改名、型別變更（例如換成符號連結）——都是違規；`atelier/` 底下不是版本檔的變更也一樣。沒有 `origin/main`（或算不出 merge-base）的環境會 skip 並說明——不是默默通過。
- 版本判定**只比版本字串**（frontmatter 的 `version` 與檔名）——本文不用來判斷版本；`atelier show <id>` 不給版本＝同一職務最大的 N。
- 改規矩的唯一路徑：對 MyAiStorage 開 PR（審查＋本人同意）；**agent 在工作時 MUST NOT 改 harness**——要改就回報提議，由人或 PM 決定。

## 怎麼跑

```
uv run pytest -q tests/unit/test_atelier_harness.py
```

`atelier show atelier:worker-team-pm` 讀的就是這棵樹的檔案（工具文件：`docs/atelier/tool.md`）；`install` 會先驗 frontmatter 與 `type: Harness`。

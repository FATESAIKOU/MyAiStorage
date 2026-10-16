# harness 檔的格式（一頁）

harness ＝ 一個職務一個版本的 UTF-8 Markdown 檔，放 `atelier/<team>/<role>/v<N>.md`；識別 `atelier:<team>-<role>@v<N>`——識別、版本與路徑是同一件事的三個寫法。spec 全文：`openspec/changes/atelier-v0/specs/atelier/spec.md`；檢查在 `tests/unit/test_atelier_harness.py`（格式＋append-only；接 CI 是 tasks 2.6，未做）。

## frontmatter（檔的第一個區塊，YAML；OKF v0.2 對齊，不認得的欄位保留）

| 欄位 | 必填 | 值 |
|---|---|---|
| `type` | ✓ | `Harness` |
| `id` | ✓ | `atelier:<team>-<role>@v<N>`；與 `team`／`role`／`version`、檔名一致 |
| `title` | ✓ | 一行標題 |
| `description` | — | 一行摘要 |
| `status` | ✓ | `draft`／`stable`／`deprecated` |
| `team` | ✓ | `[a-z0-9][a-z0-9-]*`（英文小寫與 `-`） |
| `role` | ✓ | `pm`／`arch`／`impl`／`test`（期 3 的 reviewer 對應 `arch`） |
| `version` | ✓ | `v` 加非負整數；與檔名、`id` 一致 |
| `models` | ✓ | `floor`（模型識別碼或 `null`）＋`preferred`（有順序的清單，可空）；v0 的值留空 |
| `generated` 等 | — | 不認得的欄位保留（例：`generated: {by, at}` 產生者註記） |

## 本文：五個 H2 章節（缺一不可）

| 章節 | 寫什麼 |
|---|---|
| `know` | 背景與「為什麼」：在哪、harness 怎麼載入、怎麼收發（Angareion）、工單與協作、模型 |
| `do` | 這個職務實際怎麼做：從收到指示到開 PR 與回報的步驟 |
| `judge` | 什麼算做完、什麼時候停下來問、品質怎麼判斷 |
| `dont` | 界線與禁止事項（憑證、真機、main、繞過、假裝完成…） |
| `capabilities` | 能力需求；一個能力一個 `### <能力名>` 子節 |

- **能力子節**：子節內用**一行 YAML 或清單**寫參數，沒有參數就留空；**不寫散文**（用途放 `know`／`do`）。子節名不重複；未知能力名只警告、不擋（能力清單要能長）。
- **v0 職務檔**（`v0.md`）MUST 有 `angareion` 子節；一般職務 SHOULD 宣告自己真正需要的能力（v0 的三個：`github`／`angareion`／`agora`）。
- **MUST NOT 有 `angareion` 章節**（Angareion 是能力子節，不是章節）。
- `know` MUST 明寫**「專案自己的指示檔與工單規則優先」**。
- 內容 MUST NOT 含帳號、額度、憑證名稱、模型輪換表、第二大腦內容等私人資訊。

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

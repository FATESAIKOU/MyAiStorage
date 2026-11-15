## Why

要問本人、但本人不在時，提問者把問題打包成選擇題（背景、內文、選項、推薦選項、理由）累積起來；本人有空時一題一題作答，覺得告一段落再一次送出；答案回到當初提問的頻道並 mention 提問者。這是 AI 公司重要的溝通模式，所以放在**通訊層**（Angareion）；App 只是顯示與作答的其中一個畫面，App 的機制不能反過來影響 Angareion。提問者需要能新增、更新、撤回待決（放太久的待決要增刪更新）。（#468）

## What Changes

只做設計（b，本人核准前不實作）。要設計的是介面：

- **訊息格式**：一般訊息有自己的格式；待決是一般訊息的特殊格式；兩者靠 `datatype` 區分。待決綁定「頻道＋提出者」，格式保留「提問頻道＋提問者」欄位，之後放寬提問者不用改格式。
- **一切都是 append**：新增、更新、撤回、作答都是新增一則訊息，已送出的訊息永不改動。
- **指令**：`angareion <datatype> <action>` 的指令與動作清單；CRUD 由 Angareion 處理，後端（driver／adapter）只讀寫訊息、不得依賴任何後端特有功能（例如 GitHub issue 的標籤或關閉）。
- **匯總與 snapshot**：把待決的歷史匯總成目前狀態（新增／更新／撤回／已答的折疊規則）；歷史太長時寫一則 snapshot，之後從 snapshot 往後匯總。
- **相容**：與現有讀者、游標、舊格式訊息（沒有 datatype）相容。
- **接法**：MyAiEntry（App）與 MyPMO（出題端）怎麼接。

## Capabilities

### New Capabilities

- `angareion-pending-decision`：待決訊息格式、指令、匯總與 snapshot（設計核准後才寫 spec）。

### Modified Capabilities

- `angareion`：訊息格式新增 datatype（設計核准後才寫 delta spec）。

## Impact

- 本階段只新增設計文件（`design.md`）；不改 `src/`、`tests/`、`openspec/specs/`。
- 實作範圍（核准後）：`src/angareion/`；依賴 Angareion 訊息格式的讀者（`aicomm` 等）要確認不被新格式搞亂。

# MyLinuxPool 工單（AiStorage 需要的）

> 2026-09-28：已依使用者指示整合成 MyLinuxPool 的 GitHub issue [#5](https://github.com/FATESAIKOU/MyLinuxPool/issues/5)（worker 部門與職能＋本檔內容）。期 1 完成後的實際 secrets 名稱與格式，補在該 issue。

AiStorage 需要 MyLinuxPool 做的事。期 1 沒有相依：期 1 的 profile 憑證由使用者手動安裝在 Mac 上。這些工單等 MyLinuxPool 的 profile 重新設計時一起考慮。期 1 實作完成後，會依實際結果更新本檔（tasks 8.2），再經使用者同意交給 MyLinuxPool 那邊。

詞彙依本 repo 的 `CONTEXT.md`：profile＝一類執行體的配置（部門），身分＝屬於哪個 profile，職務不參與授權。

## 1. 證明 profile 歸屬（發放 profile 憑證）

- AiStorage 的身分模型是「持有哪個 profile 的憑證，就屬於哪個 profile」。憑證要有**唯一的發放來源**，目標是 MyLinuxPool。
- 需要：建 worker 時，把該 profile 的 AiStorage 憑證注入 worker（目前的機制是 profile `secrets`）；撤銷以 profile 為單位，輪替該 profile 的憑證後，新建的 worker 拿到新憑證。
- 每個 profile 要注入的憑證（期 1 的 Mac opencode profile 是同一個形狀）：
  - Drive 收件匣用的 OAuth client 與 refresh token（`drive.file` scope）：**所有 worker 共用同一個 client 與同一份 refresh token**（技術驗證 1.4 之後的決定；每個 worker 各自授權可能碰到 Google 對 refresh token 數量的上限；這一點未實測、出處待補）。以唯讀方式掛在 `/secrets/`。
  - 讀取用的 service account 金鑰：**所有 worker 共用一個**（使用者決定），對讀取視圖資料夾是 reader。conf 裡的金鑰路徑寫容器內的路徑。
  - **每個 profile 各自一把簽章金鑰**（新增）：同步器用它對收件匣項目簽章，提交流程驗章；產生者章與以 profile 為單位的撤銷都靠它。需要：發放、輪替、撤銷（撤銷＝通知 AiStorage 不再接受該公開金鑰）。
  - GitHub fine-grained token（MyAiStorage 只有 Actions 寫入權，用來觸發提交流程；員工另有 MyBrain 唯讀）。
  - 另外，worker 裡的 opencode 需要 LLM provider 金鑰；容器裡允許的憑證是一份白名單（AiStorage design D3）。
- 實際的 secrets 名稱與檔案格式，期 1 實作後補上。

## 2. profile 能力清單

- Atelier 的職務會宣告能力需求，只能派到能力齊全的 profile。MyLinuxPool 的主機已經有 `capabilities`（`key: value`、每個 key 都有驗證方式），profile 的能力清單最好沿用同一套格式與「未驗證不算通過」的原則。
- 需要哪些部門（profile），見 `docs/atelier/`（期 1 的需求設計產出）。

## 3. worker 裡的 opencode 與同步

- worker image 安裝 opencode 與 AiStorage 的 opencode 同步器（期 1 已在 Mac 的容器裡跑過同一套）。⚠️ 期 1 驗證的是 Apple Silicon 上的 arm64 容器；worker 若是 amd64，要確認同一套在 amd64 上也能跑（期 1 實作後在本檔註明驗證過的架構）。
- 同步器在 worker 內定期執行。
- 刪除 worker 前，先跑最後一次同步並提交（以讀取介面看到這次的快照為完成）；沒同步完就明確告知哪些 Session 未同步（AiStorage spec：worker 刪除前同步完成）。

## 4. 登記非 worker 的 profile

- 把手機 App、Mac opencode（以及之後同步 Mac 本機 Session 的同步程式）也登記成 profile，讓身分的發放來源只有一個。

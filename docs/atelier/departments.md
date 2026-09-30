# 部門與職能需求（tasks 8.1 產出）

> 需求設計。只列「需要哪些部門（profile）、需要哪些職能（職務）」，
> 不做基本設計、不建 repo（與 `role.md`、`external-copy.md` 相同定位）。
> 期 1 實際存在的部門只有 Mac opencode 與測試用 profile；
> 其他一律是需求候選，每一項都標明**待使用者決定**。

前提仍然有效：
- ADR 0003（授權依 profile，職務不參與授權）：本文件列的職能只決定
  「做什麼、需要什麼能力」，不決定「能碰什麼」。
- `role.md`：職務由 know／do／judge／dont＋能力需求組成；
  職務只能派到能力清單涵蓋其全部能力需求的部門；員工啟動時只啟用職務宣告的能力。
- `external-copy.md`：職務用到的第三方 skill 以外部副本收納，
  員工不去外部取；下面職能候選若需外部 skill，一併列為副本候選。
- 詞彙依 MyLinuxPool issue #5（兩邊共用）：部門＝worker 實體＝profile；
  成員＝code agent；在部門裡開的 herdr session＝團隊；職能＝職務。

## 1. 期 1 實際存在的部門（事實，非候選）

### mac-opencode（程式識別名 `mac-opencode`）

期 1 唯一實際使用的部門：Mac 上 Ubuntu 容器裡的 opencode 與它的同步器。
能力現況（供職務比對用）：
- arm64 容器（colima）、opencode、opencode 同步器（定期同步＋同步並提交）。
- worker 共用的 Drive 憑證（`drive.file`）與讀取身分。
- 本部門自己的簽章金鑰（Ed25519；收件匣簽章、產生者章、撤銷單位）。
- LLM provider 金鑰（以檔案引用掛載，不落進工作目錄）。

### 測試用 profile

只用於測試（單元測試與整合測試的假身分、pin-test 等測試資源）。
職能：無（不派職務、不上線）。

## 2. 部門需求候選（待使用者決定）

### worker 部門（MyLinuxPool 上的 worker 實體）

需求：worker 要能成為部門，部門裡的成員（code agent，例如 opencode）
要能載入職務、用 AiStorage 身分同步與被指派工作。
能力候選（沿用 MLP 主機 `capabilities` 格式 `key: value`，
每個 key 有驗證方式，未驗證不算通過；清單取自 issue #5）：
- GPU（有沒有、型號與記憶體）。
- git 操作（可讀／可 push 的 repo，例如 MyBrain 唯讀）。
- 瀏覽器（chrome-devtools）。
- 容器內的 LLM provider（可用模型）。
- CPU 架構（arm64／amd64；期 1 只在 arm64 驗證過同一套）。
- 其他（待使用者決定）。

待使用者決定：
- 部門怎麼切：一個共用 worker 部門，還是按用途／機器拆多個部門。
- code agent 的啟動方式：先土炮（issue #5 現狀），正式機制之後再定。
- worker 之間的溝通：先當作沒有；AiStorage 的接續（交接單與認領）與
  參考可當非同步管道，夠不夠用待驗證。

### 手機 App 部門

需求：手機 App（含秘書的執行體）登記成 profile，
讓身分的發放來源只有一個（issue #5 第 4 節）。
待使用者決定：
- 秘書的 harness 目前內建在 App 裡，是否移入 Atelier 作職務，
  之後再檢討（在此之前手機 App 部門不派職務）。

## 3. 職能（職務）需求候選（待使用者決定）

`role.md` 只定義職務的機制（組成、比對、版本、可改），不定義具體職務。
以下候選每一個落實時，都要寫成 know／do／judge／dont＋能力需求，
並走職務版本（可驗證、可回滾）。

- Mac 容器內開發（部門：mac-opencode）：期 1 的 opencode 實際在做的事
  （開發、除錯、同步並提交）。期 1 沒有實際載入的職務；
  是否補寫第一份職務，待使用者決定。
- 通用 worker 職務（部門：worker 部門候選）：容器內以 opencode 工作、
  定期同步、被交接單指派。code agent 啟動土炮期間，職務內容先求最小可用。
- 以上職能若需第三方 skill（例如 MyBrain 的 `mybrain-*`），
  以外部副本收納（`external-copy.md`），不直接依賴外部。

不列的：秘書職能（harness 仍內建，待檢討，見第 2 節）；
抹除等管理操作（只限本人，不經職務指派）。

## 4. 待使用者決定的事項（彙整）

1. worker 部門切幾個、怎麼切。
2. GPU／瀏覽器等能力哪些部門要有（對應職務的能力需求）。
3. 是否補寫第一份職務（Mac 容器內開發）及其內容。
4. 秘書 harness 是否移入 Atelier。
5. code agent 正式啟動機制與 worker 間溝通（目前：土炮、當作沒有）。

## MODIFIED Requirements

### Requirement: channel 對應的更新（註冊簿重建）
`channels.json` MUST 由 MyAiStorage 的**註冊簿 issue**（長期存在，label 見 design §10）上的留言重建：`channel create`／`channel delete` MUST 各在它下面留一則一行 JSON 留言——`{"schema": 1, "action": "register"|"unregister", "channel": "<名>", "backend": "<backend 名>", "issue": <編號>}`；angareion 送出留言後 MUST NOT 等重建完成（`channel create` 會輪詢 fetch 到對應出現，見 `angareion` 的六個介面）。

workflow MUST `on: issue_comment`（限註冊簿 issue；觸發層 MUST 用 `if` 擋掉 Bot——workflow 自己的回報留言不會再觸發新的 run，複審 H1）並有 `schedule`（每天一次）當保險（複審 M3）觸發；每次 run 用 `GET /repos/{owner}/{repo}/issues/{n}/comments?per_page=100` 讀整串留言（MUST 翻完所有頁——複審 M4），依時間序**重建** `channels.json` 並 commit 進 main（結果與 main 相同就不 commit）；MUST 用 `concurrency` 併發控制且 `cancel-in-progress`（新的取消舊的）——因為每次都從整串重建，被取消的 MUST NOT 遺失任何留言的效果。

**套用規則（先到者為準）**：`register`——名字沒有對應就加上；已對應同一個實體視為成功（冪等）；已對應不同實體 MUST NOT 覆蓋，MUST 在註冊簿上回報。`unregister`——只移除指向同一個實體的對應；已經沒有、或指到別的實體 MUST NOT 改動對應，MUST 回報。

**輸入（防注入；複審 H2 調整）**：留言內容 MUST 只經環境變數交給腳本，MUST NOT 用 `${{ }}` 插進 shell；JSON MUST 嚴格解析。重建時每個留言 MUST **只驗留言本身**：channel 名（`^[A-Za-z0-9_-]+$`）、backend 名存在（在 `backends.json` 裡）、`author_association` 在採計清單；**MUST NOT 查 backend 的 issue**——標題被改、issue 被刪、API 暫時失敗都不會讓對應下架（issue 的存在與標題在 channel create 的用戶端驗，見 `angareion`）。不符的留言 MUST 跳過並在註冊簿上回報（非授權作者不回報，見下），MUST NOT 影響其他留言的重建。

**寫入**：以 main 的最新版本（sha）為底；被拒 MUST 重讀重試（上限 5 次）。

**workflow 權限（複審 L1）**：`permissions:` MUST 至少 `contents: write`（commit 進 main）與 `issues: write`（在註冊簿回報留言）。

**授權**：workflow MUST 只採計 `author_association` 為 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言；其他留言 MUST 忽略（不採計、不影響重建、**也不回報**——不回報才不會產生新留言）。防注入與驗證不是授權，照上面執行；不是合法 action 形狀的留言（含 workflow 的回報）MUST 忽略。

**回報**：無法套用或驗證失敗的留言（授權作者）MUST 在註冊簿上用**純文字**回報（引用那則留言；MUST NOT 是可被解析成 action 的一行 JSON——複審 L4）。

#### Scenario: 註冊新 channel
- **WHEN** 註冊簿上有一則 `register` 留言（`X`、backend `B`、issue `N`；B 在 `backends.json`、X 不在對應表）
- **THEN** 重建後 main 的 `channels.json` 出現 `{X: {backend: B, issue: N}}`

#### Scenario: 留言格式與重建
- **WHEN** 註冊簿上有兩則合法留言（依時間序）：register `X`→B/N，之後 unregister `X`→B/N
- **THEN** 重建後 `channels.json` 沒有 `X`；兩則都被採計（每次從整串重建）

#### Scenario: 重建以先到者為準
- **WHEN** 兩則 `register` 留言都註冊 `X`、指向不同實體（先 B1/N1、後 B2/N2）
- **THEN** 重建後 `X` 指到 B1 的 N1（不覆蓋）；後到的留言被跳過，並在註冊簿上回報

#### Scenario: 兩則註冊都採計
- **WHEN** 兩則不同名字的 `register` 留言幾乎同時留下
- **THEN** 兩筆對應都在 `channels.json`（沒有因為併發遺失）

#### Scenario: 註銷
- **WHEN** 一則 `unregister` 留言指到 `X` 目前的同一個實體
- **THEN** 重建後 `X` 從 `channels.json` 移除

#### Scenario: 註銷已經沒有或指到別人
- **WHEN** 一則 `unregister` 留言的 `X` 已經不在，或（依先前留言）指到別的實體
- **THEN** 對應不變；留言在註冊簿上被回報（no-op）

#### Scenario: 留言超過一頁仍全部重建
- **WHEN** 註冊簿的留言超過 100 則（要翻頁）
- **THEN** 重建翻完所有頁；早期的 register 仍在（沒有因為只讀第 1 頁被下架）

#### Scenario: 非 OWNER／MEMBER／COLLABORATOR 的留言被忽略且不回報
- **WHEN** 一則格式合法的留言，作者 `author_association` 不是 `OWNER`／`MEMBER`／`COLLABORATOR`
- **THEN** 該留言被忽略（不進重建、不影響其他留言、也不回報——沒有新留言產生）

#### Scenario: 回報留言不會再觸發 run
- **WHEN** workflow 對一則不合格的留言回了純文字回報（作者是 `github-actions[bot]`）
- **THEN** 那則回報不會觸發新的 run（觸發層的 `if` 擋 Bot）；不會無限空轉

#### Scenario: 取消舊 run 不遺失
- **WHEN** 一次重建 run 進行中，新留言觸發的 run 把它取消（`cancel-in-progress`）
- **THEN** 新 run 從整串留言完整重建；沒有任何留言的效果遺失

#### Scenario: 被取消後下一次 run 仍正確
- **WHEN** 一次 run 被取消（還沒寫回 main），之後又一次 run（下一則留言或手動重跑）
- **THEN** 重建結果仍正確包含全部合法留言（`channels.json` 完整）

#### Scenario: 手動重跑安全
- **WHEN** 對同一串留言 `workflow_dispatch` 重跑
- **THEN** 重建結果相同（相同就不 commit）；檔案不變

#### Scenario: 樂觀鎖重試用盡
- **WHEN** 寫回 main 連續被拒 5 次
- **THEN** 不動 main；在註冊簿上回報；下一次 run 重建再試

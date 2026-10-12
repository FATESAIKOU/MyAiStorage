# Angareion v5 設計的複審（非作者）

- 對象：`angareion-v5` worktree，`git diff angareion-v0..angareion-v5` 的 `openspec/changes/angareion-v5/`（`design.md`、`proposal.md`、`tasks.md`、`specs/angareion/spec.md`、`specs/angareion/channel-config/spec.md`），頂為 `aa40a3b`。
- 依據：本人裁定全文 `v5-decisions.md`（14:3x §10 與中午那份）＋ `design-v5-changes.md`；前一輪報告 `sidecar-v5/docs/review/v5-design-review.md`。
- 審查者：非作者。`openspec validate angareion-v5 --strict` 通過。只讀，未改任何設計文件。
- 範圍：只有 Angareion（sidecar 那半搬到 MyLinuxPool，不看）。

## 結論

**暫時還不能轉成可審（可實作）。** 前一輪的 Angareion 項目基本上都處理掉了，但 `aa40a3b` 換成「註冊簿 issue＋整串留言重建」之後長出兩個新的 High，兩者都是「照文字寫就會做出壞掉或空轉的 workflow」，而且都在定案的條文裡（§9／§10）。修完 H1、H2，順手把 M1 一起修（一個字元），這份設計就可以轉可審。Medium 其餘可在同一輪處理。

## 1. 前一輪 Angareion 項目的處理狀況

| 項目 | 結果 |
|---|---|
| H2 授權與輸入（sender 白名單） | **已換掉，方向正確**：依 §10 裁定改成 `author_association` 為 OWNER／MEMBER／COLLABORATOR；留言只經環境變數、不插 `${{ }}`、嚴格 JSON、驗 channel 名／backend 名／issue 存在且標題相符、不符就跳過並回報——§9 與 channel-config spec 都寫到了。（舊的「授權帳號名單放 repo 變數」在 `c825164`，`aa40a3b` 依裁定換掉，OK。） |
| M4 建完馬上送會擋 15 分鐘 | 已處理：查無 channel 時忽略間隔先 fetch（最短 30 秒）、`channel create` 輪詢 fetch 到對應出現（上限 2 分鐘、逾時提示 exit 0）；design §11、spec、tasks 1.3／3.2 都有，Scenario 也補了。 |
| M5 註冊請求被取消沒人重跑 | **一半**：`c825164` 的 drain＋`schedule` sweep 在 `aa40a3b` 因「重建已涵蓋」被刪掉。被取消的情況確實涵蓋了（見下），但 run 本身失敗的情況沒有（→ M3）。同名輸家不加 `--wait`、只有註冊簿留言看得到，已寫進 §6／Risks。 |
| M7 命名文法 | 已給正規式與由右而左的解析，並補了接受／拒絕 Scenario。**但正規式本身有錯**（→ M1）。 |
| M8 缺的 Scenario | 大部分補上（`--backend` 缺給、backend 不存在、第 1 步開實體失敗、關實體失敗、404 視同已關、註銷送出失敗、重試 3 次、pull 鎖、逾時 20 秒、`interval=0`、wait 輪詢不 fetch、sha 重試用盡）。仍缺的是回報留言不參與重建、讀留言分頁（→ M4）。 |
| L3 spec 引用未定案的 design | 已改成寫死 §10 的 token 範圍。 |
| L4 用 `git fetch`＋`git show origin/main:` | 已採用，`FETCH_HEAD` mtime 當新鮮度、flock、逾時 20 秒都寫了。（但和 §10 的 token 範圍有衝突，→ M2。） |
| L5 寄給我的掃所有 channel | 進 Risks。 |
| L6 delete 後的過期複本 | 進 Risks，「送註銷失敗」Scenario 也補了。 |

## 2. §10 新設計的可行性

逐項看過，**留言格式與解析、被 cancel 的 run、只採計三種 `author_association`、防注入、token 權限範圍**都有寫進 design §9／§10、兩個 spec 與 tasks，方向都對，細節也夠實作者動手：

- 留言格式 `{"schema":1,"action":"register"|"unregister","channel":…,"backend":…,"issue":N}` 一行 JSON、嚴格解析、非此形狀（含 workflow 自己的回報）忽略：清楚、可測。
- 被 cancel 的 run 不遺失：整串重建 ＋ `cancel-in-progress`，spec 兩個 Scenario（取消舊 run、被取消後下一次 run 仍正確）可以寫成腳本單元測試。
- 授權：只採計三種 `author_association`，其他忽略；§9、§10、channel-config spec、tasks 3.5／5.1 一致。
- 防注入：只經環境變數、不插 `${{ }}`、嚴格 JSON、驗 channel／backend／issue 存在且標題相符、不符就跳過且回報。夠。
- token 權限：§10 寫了細粒度 PAT、勾哪幾個 repo、`Issues: Read and write`，也把 workflow 用的 `GITHUB_TOKEN` 與 channel token 分開講。方向對，但有一個缺口（M2）。

問題出在**兩條規則的組合**上。

## 3. 問題

### High

**H1. 回報留言會觸發新的 run，被拒絕的留言會讓 workflow 無限空轉。**

- §10 的觸發條件是 `on: issue_comment`「限這張 issue」——只限 issue 編號，沒有任何作者／型別過濾。
- §9 的回報規則是「無法套用或驗證失敗的留言，workflow MUST 在註冊簿上回報」。回報留言本身也是那張 issue 的一則 `issue_comment`。
- 所以：一則留言驗證失敗 → run 1 回報 → 回報觸發 run 2 → run 2 讀到同一則失敗的留言 → 再回報 → run 3 …… **不會收斂**。「回報留言不參與重建」只擋住內容效果，擋不住觸發。疊加 `cancel-in-progress: true` 就是一條永遠被取消、永遠重生的流水線。
- 觸發條件很寬：一則留言指向的 issue 標題被改、issue 被刪掉、或留言者的權限被降級，都會讓它變成「永遠驗證失敗」，於是只要發生過一次，這個 repo 的 Actions 就一直在跑。
- 設計也沒說「非授權作者的留言不回報」——若會回報，任何外部使用者都能用一則垃圾留言啟動這個循環。
- 建議（擇一，都很小）：
  1. 觸發層加守門：`if: github.event.comment.user.type != 'Bot'`（或排除 `github-actions[bot]`），並明寫「非授權作者的留言不回報」（不回報就不會產生新留言）。推論：`author_association` 的過濾也可以搬到 `if:`，但重建腳本仍要再驗一次（手動重跑與日後重建都會重讀整串）。
  2. 回報去重：同一則留言在同一個結果下不重複回報（要寫得出「已經回報過什麼」的狀態，成本較高，不建議第一期）。
- spec 加兩個 Scenario：「被拒絕的留言觸發的回報留言不會再觸發 run」與「非授權作者的留言被忽略且不回報」。前者用 workflow 的觸發條件測（可測）。

**H2. 重建是純函式、沒有「已經套用過」的狀態，所以任何讓留言不再通過驗證的事故都會靜默下架還在用的 channel。**

- §9：「每次 run 讀整串留言、依時間序套用（重建）成最終對應」——最終對應＝f(整串留言)，**不看 main 上現在有什麼**。「同名不覆蓋」是在留言之間先到者為準，不是「main 優先」。
- 於是下面每一項都會把一個正在使用的 channel 從 `channels.json` 拿掉，而且沒有任何人收到通知（`channel delete` 不會被觸發、create 端不會知道）：
  1. **有人改了 backend repo 裡 issue 的標題**（很平常：加前綴、改寫）。§9 要求「標題等於 channel 名」，那次留言在之後每次重建都不合格 → 對應消失。
  2. **issue 被刪掉或被轉移**。
  3. **驗證時的暫時性 API 失敗**（5xx、逾時）如果被當成「issue 不存在」，同樣下架。設計沒寫怎麼區分「查不到」與「查失敗」。
  4. **留言者的權限被降級**（COLLABORATOR → NONE，例如被移出協作者）。授權在每次重建都重新評估，那則留言從此不採計 → 對應消失。
  5. **讀留言沒翻頁**（→ M4），超過一頁之後早期的註冊全被當成不存在 → 批次下架。
- 建議：在 §9 加一條權威順序，例如「**main 上已有的對應，重建時保留**；驗證（channel 名／backend／issue 存在／標題／授權）只用來決定**新的**對應能不能加進來；移除只能由 `unregister` 留言造成」。這一條同時解掉上面 1～5，也讓「人工改 `channels.json` 不會被下一次重建還原」變成明確行為（目前是會被還原，設計沒寫）。另外把「查不到的分類」寫清楚（404＝不存在；5xx／逾時＝保留現狀並回報，不動檔）。
- spec 加 Scenario：channel 已存在於 main、而它的 register 留言現在驗證不過 → 對應仍在（並舉授權降級與標題被改兩個例子）。

### Medium

**M1. agent 名的正規式不含 `-`，會拒掉設計自己舉的合法例子。**

§1 與 spec 的 Requirement 寫的文法是 `^[A-Za-z0-9][A-Za-z0-9._]*-(pm|arch|impl|test)(-[A-Za-z0-9_.]+)?$`，同一段又說「左邊剩下的都是 repo 名（repo 名裡的 `-` 靠右側的角色字解析，例 `my-repo-impl-2` → repo `my-repo`）」，spec 的「位址文法（接受）」Scenario 也列了 `my-repo-impl-2`。實際用這個正規式測：`my-repo-impl-2` **不通過**（repo 段的字元類 `[A-Za-z0-9._]` 沒有 `-`，regex 無法跨越 `-` 去比對角色字）。其餘三個接受案例與五個拒絕案例都正確。

主人的命名是 `<GitHub repo 名>-<role>[-<id>]`，含 `-` 的 repo 名很常見。建議把 repo 段改成 `[A-Za-z0-9][A-Za-z0-9._-]*`，並在 §1 講清楚「這是驗證用的文法；由右而左取出 id 與角色字是**另外寫的解析步驟**（取最右邊符合角色字的位置），不是正規式做的事」——現在兩句寫在一起容易被實作者誤以為 regex 會自動由右而左。

**M2. 「token 不需要 Contents」與 §11 的 `git fetch` 讀 `origin/main` 之間沒交代認證方式。**

§10 寫信道 token「不需要 Actions／Contents 等其他權限」，但 §11 的本機複本靠 `git fetch` + `git show origin/main:<路徑>`。如果 fetch 用的是這支 token，就需要 `Contents: Read`；MyAiStorage 是 public 的話應該明寫「clone／fetch 走匿名 HTTPS、不用 token」，才和「不需要 Contents」對得上。本人特別要求把 token 的權限範圍寫清楚，這是唯一還沒對上的地方。順便建議寫明：`Issues: Read and write` 在 backend repo 是留言與讀 issue、在 MyAiStorage 只用在註冊簿留言；workflow 那支是 repo 的 `GITHUB_TOKEN`，與信道 token 無關。

**M3. 硬失敗沒有補救。**

§10 寫「drain／`schedule` sweep 不需要（重建已涵蓋）」——「重建已涵蓋」只對**被取消的 run** 成立（設計也正確地這樣說了）。run 本身失敗的情況沒有：腳本出錯、讀留言 API 失敗、被自己人手动 cancel 而沒有下一則留言，此時那則留言永遠不會被套用，而 `channel create` 已經 exit 0（只有逾時時 stderr 有一行提示）。建議二選一：留一個低頻 `schedule`（例如每天一次，成本幾乎為零），或明寫「靠人手動 `workflow_dispatch`」並讓 create 的逾時提示指名要去看註冊簿 issue。至少要寫進 Risks——目前寫的是「下一次 run 補上」，只在真的有下一則留言時成立。

**M4. 讀整串留言沒有分頁的規定。**

§9 與 spec 都只寫「讀整串留言」，沒有寫 API 與 `per_page`。GitHub 的留言列表預設每頁 30 則。配合 H2，超過一頁之後早期的註冊會被當成不存在而**批次下架**。請寫明用 `GET /repos/{owner}/{repo}/issues/{n}/comments?per_page=100` 並翻完所有頁（或改用 GraphQL 一次拿回），spec 加一個「留言超過一頁仍全部重建」的 Scenario（腳本層可用假 API 做）。

### Low

- **L1** workflow 沒寫 `permissions:` 區塊。依 §10 的行為至少需要 `contents: write`（commit 進 main）與 `issues: write`（在註冊簿回報）。tasks 5.3 只講分支保護／bypass。
- **L2** 註冊簿 issue 是「長期存在、不關閉」的工單，而本 repo 的 AGENTS.md 說 issue 是工單、做完以 completed 關閉。§10 只寫「帶正確的 `層級:`／`狀態:` label（實際值開 issue 時定）」，沒有寫這張永不關閉的 issue 對 MyPMO 每日檢查的影響（前一輪 M6 問過，這裡換成單一 issue 後反而更需要寫一句）。建議把選定的 label 寫死並在 Risks 加一行。
- **L3** design 末段「本人未指示、作者提議」的清單沒有補 `aa40a3b` 新增的作者選擇：`workflow_dispatch` 手動重跑、回報留言要「驗 issue 標題等於 channel 名」、2 分鐘等待上限與 30 秒最短 fetch 間隔、註冊簿的置頂留言、`issue_comment` 限單一 issue 的觸發條件。依 AGENTS.md 與本輪任務的要求，這些要在向本人說明時一起提出。
- **L4** 回報留言「引用那則留言」的內容格式沒定義。建議明寫回報是純文字（不要把被拒絕的留言原文包成可被嚴格解析的單行 JSON），並配合 H1 在觸發層擋掉 Bot。
- **L5** 人工編輯 `config/angareion/channels.json` 會在下次重建被還原（目前行為），設計沒寫；要嘛寫明「這檔是機器專屬、人工改會被還原」，要嘛照 H2 的建議讓 main 優先。
- **L6** `registry_issue` 是每台機器各自填的編號；註冊簿 issue 若被關閉或換號要怎麼通知、怎麼遷移，沒寫。第一期可接受，但值得列進 Open Questions。

## 4. Scenario 可測性

- **可直接寫成單元測試**：兩個 spec 裡的絕大多數——重建的套用規則（先到者為準、冪等、不覆蓋）、留言格式與嚴格解析、授權過濾（OWNER／MEMBER／COLLABORATOR 採計、其他忽略）、手動重跑不變更、sha 樂觀鎖重試與用盡、`git fetch`＋`git show origin/main:` 的讀取（可注入 git 介面）、30 秒最短間隔、`interval=0`、wait 輪詢不 fetch、fetch 逾時與失敗沿用、channel 五個介面的 exit 1／exit 2 各種分支、送註冊重試 3 次。tasks 3.5／4.1 有寫「腳本以可注入的檔案 API 單元測試」，方向對。
- **目前寫不成測試**：H1（workflow 觸發條件——H1 修好並補 Scenario 後可以寫成對 `if:` 條件的測試或一組 workflow 觸發的整合測試）、H2（缺 Scenario；補上後可測）、M1（Scenario 與正規式互相矛盾，測會紅）、M4（沒有分頁的 Scenario）。

## 5. 與本人裁定、本人變更的核對

逐條核對過，**沒有漏、沒有寫反**：

| 本人的條目 | 落點 |
|---|---|
| `channel create <name>`：後端開 issue → 組成 {backend, issue-id} → workflow 產出對應檔直接更新 main；對應持久化 | §6、§8、§9、§10、tasks 3.2／3.5 |
| `channel delete`：關 issue 不刪（留紀錄）＋拿掉對應 | §7、spec「刪 channel」、tasks 3.3 |
| `send` 沒有 `--to`＝廣播，算進每個人的「寄給我的」 | §2、spec「目標與游標」 |
| 兩個 create 同時跑的競爭（workflow concurrency） | §9 `concurrency`＋`cancel-in-progress`、Risks |
| 觸發 workflow 需要的權限（信道 token 目前只有 Issues） | §10 token 權限、tasks 5.2 |
| 讀取端多久 pull、失敗怎麼辦 | §11、spec「讀取與 pull」 |
| §10：註冊簿 issue（MyAiStorage、帶正確 label）＋create／delete 各留一則留言、`issue_comment` 觸發 | §10、spec channel-config「channel 對應的更新」、tasks 1.4 |
| §10：整串留言重建、commit 進 main | §9、spec「留言格式與重建」 |
| §10：`concurrency`＋`cancel-in-progress` | §9、spec「取消舊 run 不遺失」 |
| §10：授權只採計 OWNER／MEMBER／COLLABORATOR | §9、§10、spec「非 OWNER／MEMBER／COLLABORATOR 的留言被忽略」、tasks 3.5／5.1 |
| §10：信道 token 的權限範圍要寫清楚 | §10（有一個缺口，見 M2） |
| v5 變更：channel 是英文名、介面不出 repo／issue、拿掉 `--member`、`--to` 選填 | §3、§4、§5 |
| v5 變更：channel 設定檔與 backend 設定檔分開、放 MyAiStorage、backend 走 PR | §8 |
| v5 變更：更新走 PR（這條被當天稍晚的裁定取代為「channels.json 由 workflow 直推、backends.json 走 PR」） | §8 分得很清楚 ✓ |
| v5 變更：讀取＝clone 後定期 pull | §11 |
| v5 變更的待確認（`channel create` 還要不要留） | 已定案保留（§5／§6），Open Questions 寫「沒有未定項目」✓ |
| PR #41 堆疊提案 | design 末節：#41 已用 merge commit `95a7641` 合併、v0 的 commit 已在 main、`angareion-v5` 以 `angareion-v0` 為 base 堆疊、**不需要** `rebase --onto`（那是 squash 的情形）、push 由 PM 指派。與前一輪 L9 的判斷一致 ✓ |

`design-v5-changes.md` 裡「更新＝對 MyAiStorage 開 PR」與裁定的「workflow 直接更新 main」不一致，設計跟的是後者（同一天、較晚的裁定），這個取捨正確。

**作者自己加、本人沒說的**（design 末段有列一部分，見 L3 的補充）：`--backend`、pull 間隔預設 900 秒、送註冊重試 2 次、sha 重試上限 5 次、`config_dir`／`registry_repo`／`registry_issue`、同名 create exit 1，加上 L3 列的那些。全部合理，沒有任何一項被寫成「本人裁定」。

## 6. 建議順序

1. **H1**：觸發層擋 Bot＋明寫非授權留言不回報；spec 補兩個 Scenario。
2. **H2**：§9 加「main 既有對應優先、驗證只擋新的對應」＋「查不到 vs 查失敗」的分類；spec 補標題被改、權限降級兩個 Scenario。
3. **M1**（一個字元）＋**M4**（分頁寫進 §9、補 Scenario）同一輪改。
4. **M2**（寫明 clone 走匿名）、**M3**（schedule 或 Risks 說明）、L1～L3 同一輪。
5. 之後請非作者再看一次 H1／H2／M1 的改動，再轉可審。
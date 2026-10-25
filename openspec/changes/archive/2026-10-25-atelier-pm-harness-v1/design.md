## Context

見 `proposal.md`「Why」。這一版設計的來源、現況與約束：

- **來源（唯讀、不進 repo）**：PMO 整理的「PM 共通的工作方式」草稿（四個 repo PM 加上 PMO 自己的職責→做法的整理；本設計只用它的第 1、2 節＝任何 repo 的 PM 都適用的部分）＋**本人 2026-10-18 對該草稿附錄 B 的 17 條決定**（以下記為 B-1～B-17）。草稿的附錄 A（各專案特有的做法）**不進 harness**，留在各 repo 的 `AGENTS.md`（B-17）。
- **現行 v0**：`atelier/worker-team/pm/v0.md`（`atelier:worker-team-pm@v0`）＝期 1 的**單人暫定** PM（自己實作、沒有非作者審查），已在 worker 上由開機單位 `start.d/20-atelier` 安裝、實機載入驗收通過（MyAiStorage#32）。
- **v1＝有隊員的 PM**（B-17）：Mac 上的 repo PM 與 worker 上的 team PM **共用一份**；「單人暫定」結束——PM 不自己寫程式、一律非作者審查、派工與監看是主要動作。
- **載入改成按 agent**（D4）：v0 是使用者層級指示檔（`~/.claude/rules/`、`~/.config/opencode/AGENTS.md`）——同一台機器、同一個使用者的**每個** agent 都會載入；v1 起由**開 agent 的一方**在啟動那一個 agent 時交付（只有 PM 拿 PM harness、隊員不拿），使用者層級不再是 v1 的交付管道。
- **約束**：MyAiStorage 是 public repo——harness 與本 change 都**不寫**帳號、額度、憑證名稱、機器位址、其他專案的名字、模型輪換表的內容、第二大腦的內容；模型只寫「照 PMO 的模型輪換表」（B-15）、通道與隊員管理只寫**能力**不寫工具名（B-16）。本 change 只做設計、不改 `src/`／`tests/`、不碰真機。

## Goals / Non-Goals

**Goals**

- 定 v1 職務檔的內容，並**以 v0 為底逐章說明差異**（刪／改／新增＋理由）（D1）。
- 把 17 條決定**逐條落點**到：harness（know／do／judge／dont）、環境說明（Mac／worker 各一份）、各 repo 的 `AGENTS.md`（D2）。
- 回答識別與路徑（D3，已定）、**載入改成按 agent 交付**（D4：機制選項＋實測／推測＋成本＋建議）、環境說明（D5，已定＋交付方式）、能力名（D6，已定）。
- 說清楚格式檢查要補什麼（D7）、spec delta 為什麼要寫與寫哪些（D8）、**要 MyLinuxPool 配合的介面變更**（誰改什麼、欄位／旗標、錯誤情形）。

**Non-Goals**

- 實作：v1 檔進 `atelier/` 樹、格式檢查、`docs/`、`atelier render`、sidecar／pane 的交付、開機單位的清理都只列步驟（tasks.md）。
- 各 repo 的 `AGENTS.md` 內容與附錄 A 的搬遷（各 repo 自己的事）。
- 模型輪換表與工單規則的修訂（PMO 的 repo）。
- 期 3 的 Atelier 本體、三職務、多職務載入（每個 agent 一份設定目錄）。
- v0 檔的修改（append-only：**不可改、不刪**）。

## Decisions

| # | 決策 | 狀態 |
|---|---|---|
| D1 | v1 職務檔的內容：以 v0 為底改成「有隊員的 PM」；逐章差異見下 | 定（草案 `harness/pm-v1.md`） |
| D2 | 17 條決定（B-1～B-17）各落在哪一層 | 定 |
| D3 | 識別與路徑：沿用 `worker-team`、新增 `atelier/worker-team/pm/v1.md` | **已定：選項 1**（PMO 2026-10-18） |
| D4 | 載入：**按 agent 交付**（不是使用者層級）；機制選項、實測／推測、v0 的過渡 | 定（建議選項 A；見下） |
| D5 | 環境說明：放環境那一側（已定）＋**也按 agent 交付** | **已定：選項 A**（PMO 2026-10-18）＋交付方式定 |
| D6 | 能力名：`github`／`channel`／`agora`／`team`／`monitor` | **已定：選項 I**（PMO 2026-10-18） |
| D7 | 格式檢查要補的項目 | 定（清單） |
| D8 | spec delta 的範圍與理由 | 定 |

### D1 v1 的內容——以 v0 為底的逐章差異

`harness/pm-v1.md`（本 change 內的草稿）＝ v0 的五章重寫。下表逐段對照 v0；「動作」是 刪／改／新增／保留。

**標題與前言**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| 「# worker team PM（v0）」「你是 worker 上 AI 團隊的 PM……任務從信道（Angareion）來。這是期 1 的 v0，期 3 會由正式版取代」 | 「# PM（v1）」「你是這個大專案（repo）的 PM：用隊員把工單做完、審完、合進去……任務從環境提供的通道來（看環境說明）」；「期 1／期 3」的字樣刪掉 | 改 | B-17 共用一份（不再綁 worker）；工具名（Angareion）不進 harness（B-16）；「暫定」由版本與 spec 記，不由內文記 |

**know**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| 「你在 worker（容器）上；你的工作目錄是啟動你的人指定的那個專案目錄」 | 「你在哪台機器、工作目錄怎麼決定、你的 agent 名怎麼取、通道是什麼、能力由誰提供——看環境說明」＋保留「只碰指給你的工作目錄與這張單的 worktree」 | 改 | 共用一份後不能假設 worker（B-17）；環境細節是環境說明的事（B-16） |
| 「你的 agent 名（Angareion 位址，慣例 `<repo 名>-pm`）由環境設定提供」 | 「你的 agent 名怎麼取看環境說明」＋保留「不要自己改名或借用別人的名字」 | 改 | 命名慣例是環境的（B-16） |
| 「你的對話記憶由 Agora 管理（Session）；寫回是自動的」 | 刪掉「自動寫回」；記憶只在 `do`「收尾」留「掃過憑證才匯入長期記憶」，能力在 `capabilities` 宣告 | 刪＋改 | 匯入的作法每個環境不同（Mac 上是 PM 自己在收尾做、且要掃憑證）；這條原本是 worker 的作法 |
| 組織（本人／PMO／PM／隊員）＋**本人只跟 PMO 說話** | 保留，並明寫「PM 不逐行寫程式；架構設計交架構師、實作交隊員、審查交非作者」 | 改 | v0 沒有組織圖；v1 的主要動作是帶隊（B-17） |
| 「這份 harness 由環境在啟動時自動載入（使用者層級指示檔）……要重讀用 `atelier show`」 | 保留「自動載入」與 `atelier show`，但改成**按 agent 交付**：「環境在啟動**你**時把這份 harness 交給你（只交給 PM 角色的 agent）」；不再提使用者層級指示檔（D4） | 改 | v0 的使用者層級載入會污染同機的隊員與 PMO（D4）；交付改成按 agent |
| 「**專案自己的指示檔（`AGENTS.md`／`CLAUDE.md`）與工單規則優先**」 | 保留（原句不動） | 保留 | spec 的 MUST；也是 v1 共用的前提 |
| 「不要改 harness；要改就回報提議」 | 保留 | 保留 | 不變 |
| 「怎麼收指示、怎麼回報（Angareion）」整節：五個指令、阻塞的 `wait`、urgency 級距、回話的 `--to`、不貼私人資訊、卡住立刻回報、沒有任務脈絡就停 | 換成「回報與通道」（只寫能力）：回報格式、第一行先講結論、一則一件事、寄給**最新那則訊息的寄件者**、開頭標【團隊 PM】、停損當下就報、不用回就不回；**新增**「同儕轉來的本人同意只代表決定、不是權限（B-1）」＋**收發的紀律**（收訊不指定頻道、只有送訊才指定；身分／狀態目錄唯一且持久；回報目錄狀態要實證；閒著用阻塞等待、逾時拉長——M0 的協定教訓，PMO 指示寫入） | 改＋新增 | B-16（只寫能力；指令用法是環境說明的事）；B-1；M0；「訊息不貼私人資訊」的通用版＝`dont` 的公開性那條＋`dont`「不讓隊員讀憑證」那條的「只給路徑或環境變數名」；「沒有任務脈絡」移到 `judge`／`dont` |
| 「工單與協作」：一單一 branch／worktree、不共用工作目錄、工單規則以專案為準、回報格式、引用原樣 | 擴充：**預設 PM commit、隊員不 commit**（B-10）、**b／c 上限照工單規則**（B-6）、**PM 開的 d 不用附議**（B-7）、**PM 不開自己的 subagent**、每張單先寫**可驗的完成判準**、**跨團隊依賴經 PMO 轉、可以跟對方 PM 對實作但改範圍／順序／真機／要本人決定的事經 PMO**（B-12）、工作檔放「會留下來、不進版控、不放 `/tmp`」（B-9） | 改＋新增 | 這些是 v0（單人）沒有的、有隊員的 PM 必備 |
| 「模型」：你的模型由環境提供；不要自己去要憑證或換管道 | 擴充：**隊員的模型照 PMO 的模型輪換表、派工前重讀**（B-14／B-15）、**審查的模型不能跟作者相同，所以記作者模型**（B-4）、「隊員的模型可能被換掉，以報告第一行為準」 | 改＋新增 | v0 沒有隊員就沒有選模型的規矩 |

**do**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| 「**期 1 v0 暫定**：隊上只有 PM 一個職務；你收到的工作由你自己執行到 PR——包含實作與測試——並在回報標明」 | 刪（`judge`／`dont` 改寫成「非作者審查、PM 不自己寫程式」） | 刪 | B-17：v1 起是有隊員的 PM |
| 四步：收到指示後確認範圍與驗收／執行（照工單做、寫檔用絕對路徑、測試前景跑、不為變綠改弱測試）／收尾（commit→push→開 PR）／回報 | 八節：**收到方向或需求／開單、拆單／派工／監看／核對與審查／開 PR、交合併／收尾／回報** | 改 | 有隊員的 PM 的工作是派工、盯、驗、審、合；自己動手的部分只剩「重跑測試、核對、commit」 |
| （無） | 新增：**b 與大的 c 交架構師寫設計**（列本人的判斷點與「作者提議、本人未指示」；經非作者審查、逐項修正、PMO 向本人說明、本人同意才實作）；方向變了先停下改設計 | 新增 | 需求收斂與設計先行是共通職責（草稿 1.3）；也是本 change 自己的流程 |
| （無） | 新增：**派工**（一單一 worktree、brief 檔的固定內容與第一行、訊息只指向 brief、換工作前清脈絡、實作／審查／修正／確認派給不同人） | 新增 | 草稿 1.4；B-10 的預設寫在 brief 契約裡 |
| （無） | 新增：**監看**（有在跑就掛著、到期重掛、狀態變化才處理、全部待命可停（B-8）；「做完」＝中斷提示消失＋報告檔存在；掉線接回、限流換下一個；長時間的跑自己看 log；本人說停下就在安全點停） | 新增 | 草稿 1.6；B-8 |
| （無） | 新增：**核對與審查**（同一個 worktree 自己重跑、只寫自己跑過的數字；給審查者 diff／設計／清單、要求在副本裡改壞實作確認會轉紅；**高、中修完才開 PR**、低可後續、專案可更嚴（B-3）；**同模型不同 session 的退路**（B-4）） | 新增 | 草稿 1.7／1.8；B-3／B-4 |
| v0 收尾：commit→push→開 PR（PR 說明寫範圍、測試、風險）→回報 | 擴充：**PR 說明**（為什麼／改了什麼／驗證原樣輸出／限制與沒驗到的／作者與審查模型／`Closes #N`）、「PR 好了」＝審過且 CI 綠、**合併的分工**（c／d 交 PMO 代 merge、b 等本人、私人筆記類留本人）、互相依存的 PR 先後與 rebase、收尾（worktree remove、歸檔、掃憑證才匯入、更新交接檔）＋**關隊員前的確認**（先中斷、確認沒有長時間的指令還活著；只關畫面不會停掉 session，共用服務重啟會叫醒——M0） | 改＋新增 | 草稿 1.9；v0 只有單人的收尾；M0 的實際事故 |

**judge**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| 「什麼算做完」：驗收條件都滿足、測試真的跑過、PR 開好、回報送出 | 加：**PM 自己重跑過**、**非作者審查通過（高、中修完）**、**CI 綠或附原樣輸出**、PMO 收到通知 | 改 | 有隊員後，「做完」要含核對與審查（草稿 1.7／1.8） |
| （無） | 新增：**隊員什麼算做完**（中斷提示消失**而且**報告檔存在；工具顯示的狀態不算數）、**隊員的回報可不可信**（自己重跑一致才算）、**審查報告可不可信**（第一行有模型、結論明確、證據） | 新增 | 草稿 1.6／1.7／1.8 的判斷基準；B-8 |
| 「什麼時候停下來問」6 條（矛盾、要本人裁定、被擋、真機／別的 repo／憑證、沒有任務脈絡、測試改不動） | 擴充成 10 條（跨 repo 相依與順序、設計取捨、懷疑審查或隊員、要例外、判斷不了誰決定）＋**什麼要等本人**清單＋**兩個直接問本人的例外**（B-11） | 改＋新增 | 草稿 1.11；B-11 |
| 品質判斷：引用真的存在嗎、有沒有證據、有沒有偷改弱測試 | 併進：「引用真的存在嗎、有沒有證據」→ `judge`「審查報告可不可信」（第一行有模型、附路徑行號與原樣引用、抽查）；「有沒有偷改弱測試」→ `judge`「隊員的回報可不可信」（動過的測試檔與 diff 對不對得上）；「不為了變綠改弱測試」→ `judge`「測試失敗你改不動、或『修好』會弱化測試就停下問」＋ `do`「核對與審查」的改壞驗證（比原句更強） | 改 | 保留原有的判斷語，去向寫在實際的位置 |
| 「**審查一律非作者**：你不審、也不合併自己的 PR（期 3 起由隊上 reviewer 審）」 | 保留原句，去掉「期 3 起」的但書 | 保留 | spec 已有這條檢查；v1 起就是這樣 |
| 「不確定就說不確定，把選項與建議一起回報」 | 保留 | 保留 | 不變 |
| （無） | 新增：層級判定（d／c／b）、真機的那一次授權（計畫含停損條件經 PMO 問過本人、本人授權那次，PM 自己跑並逐 step 盯；被擋就請本人接手、不請別的 session 代做）（B-2）、**授權只對那一次有效**（B-1） | 新增 | B-1／B-2 |

**dont**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| 不讀不印不寫憑證／祕密 | 保留＋補「要確認憑證在不在，只看檔案存不存在、多大」 | 改 | 草稿 1.10 |
| 不碰真機、不 ssh、不 dispatch、不改 secret、不碰別的 repo | 改：**沒有本人那一次授權的真機操作不做**（有授權的那一次見 `judge`）（B-2） | 改 | B-2 |
| 不繞過被擋的動作：不換方式、不換身分再試 | 改：加「**不請別的 session 代做**」（那等於繞過權限）（B-1） | 改 | B-1 |
| 沒有明確的任務脈絡時不探索 HOME／不讀設定檔／不寫記憶檔 | 保留（`judge` 也留一條同樣的） | 保留 | 不變 |
| 不改 harness | 保留 | 保留 | 不變 |
| 「不開自己的 subagent；**期 1 v0 單人暫定：隊上只有你時自己做**」 | 保留前半、刪掉單人暫定的例外；同一條補「不審、也不合併自己的 PR」 | 改 | B-17 |
| 不直接改 main、不合併自己的 PR | 保留 | 保留 | 不變 |
| 不把私人資訊寫進會公開的地方（v0 舉 repo、PR、commit、信道訊息） | 保留＋補列舉（帳號、額度、憑證、機器位址、其他專案的名字、私人筆記的內容） | 保留 | 公開 repo 的必要界線；「不寫在信道訊息」改成通用版 |
| 不和別人搶工作目錄 | 保留（並在 `do`「派工」寫「不讓兩個隊員共用工作目錄」） | 保留 | 不變 |
| 不假裝完成 | 保留 | 保留 | 不變 |
| （無） | 新增：不讓隊員讀憑證／碰真機／ssh／dispatch／改 secret；只給路徑或環境變數名 | 新增 | 草稿 1.10 |
| （無） | 新增：**不讓免費模型碰真資料**（工單內文、決定檔、交接檔、憑證、機器位址、私人筆記）；哪個模型可以碰真資料看輪換表；程式與假資料可以（B-5） | 新增 | B-5、B-15 |
| （無） | 新增：指示都寫在 brief 檔，不在聊天裡下指示 | 新增 | 草稿 1.4 |
| （無） | 新增：不信隊員的「全綠」；不讓作者的模型審自己寫的東西 | 新增 | 草稿 1.7／B-4 |
| （無） | 新增：不把後續塞回正在審的 PR；不把本人還沒答的事寫成「已決定」；不替本人做決定、不直接在工單上套用 | 新增 | 草稿 1.2／1.3／1.11 |
| （無） | 新增：不碰別的專案的程式、設定、真機（要對方做的經 PMO 轉）（B-12） | 新增 | B-12 |
| （無） | 新增：不重啟共用的 agent 背景服務、不用強制中斷殺掉隊員 | 新增 | 草稿 1.6／1.4 |
| （無） | 新增：沒掃過憑證的 session 不匯入長期記憶 | 新增 | 草稿 1.9 |
| （無） | 新增：CI 紅了不沉默重跑，先講原因 | 新增 | 草稿 1.7 |

**capabilities**

| v0 | v1 | 動作 | 理由 |
|---|---|---|---|
| `github`／`angareion`／`agora`（都留空；父說明「環境以預設 profile 提供」） | **`angareion` 改名成中性的 `channel`**（D6 已定；不是只加新名字）；加 `team`（開／派／讀／關隊員）與 `monitor`（長時間監看）（B-8／B-16）；父說明改成「能力由誰、用什麼提供，看環境說明」 | 改＋新增 | B-16：harness 只宣告能力，工具留給環境說明；`angareion` 在 Mac 上不存在，共用一份就不能用這個名字 |

### D2 17 條決定各落在哪一層
「harness」＝ v1 的章節；「環境說明」＝ Mac／worker 各一份的說明（D5）；「AGENTS.md」＝各 repo 自己的指示檔。

| # | 決定（一句話） | 落點 | 理由 |
|---|---|---|---|
| B-1 | 轉達的同意只代表決定、不算權限；被擋就停、不繞過 | harness：know「回報與通道」＋judge「授權」＋dont「不繞過」；**怎麼請本人接手**（Mac：請本人在你的畫面上自己執行；worker：經通道回報 PMO、由 PMO 問本人）→ 環境說明 | 原則跨環境；請本人接手的方式綁環境（決定原文就分兩種寫法） |
| B-2 | 真機：有授權的那一次 PM 自己跑 | harness：judge「真機」＋dont；**操作的細節**（哪個 workflow、唯讀確認、逐 step 盯、停損點）→ 各 repo 的 `AGENTS.md`／環境說明 | 原則（誰動、要什麼授權）跨環境；怎麼動是專案與環境的 |
| B-3 | 開 PR 前高、中修完；低可後續；各專案可更嚴 | harness：do「核對與審查」3＋judge「什麼算做完」；**加嚴的門檻**→ 各 repo 的 `AGENTS.md` | 預設跨專案；加嚴是專案的 |
| B-4 | 不同模型的審查者都用不了時，可同模型、不同 session（PR 第一行寫明） | harness：judge「退路」＋know「模型」 | 跨環境的規則；「記作者模型」是它的前提 |
| B-5 | private repo 的程式可給免費模型；真資料不行 | harness：dont＋know「模型」；**哪個模型可以碰真資料**→輪換表；**哪些內容算真資料（工單內文／決定檔／交接檔／位址）**→ 各 repo 的 `AGENTS.md` | 原則跨環境；名單會變（輪換表）與綁專案（AGENTS.md） |
| B-6 | b＋c 上限只寫「照工單規則」 | harness：know「工單與協作」＋do「開單、拆單」；**數字**→ 工單規則（PMO 的 repo） | 數字只放一個地方，改規則是改那一處（本 change 也不寫數字） |
| B-7 | PM 開的 d 不用另一位附議 | harness：know「工單與協作」一句；（收尾）工單規則與各 repo 摘要的措辭一起更正 | 一句話消歧義；規則本體在 PMO |
| B-8 | 有在跑就掛監看、全部待命可停 | harness：do「監看」1＋capabilities 的 `monitor` | 跨環境的行為與能力需求 |
| B-9 | 工作檔放會留下來、不進版控的地方（不放 `/tmp`） | harness：know「接手」＋do「派工」；**具體位置**→各 repo 的 `AGENTS.md`／環境說明 | 原則通用、路徑是專案的 |
| B-10 | 預設一單一 worktree、PM commit、隊員不 commit；工單明寫時照工單 | harness：know「工單與協作」＋do「派工」1／「開 PR」1 | 一個預設讓 PM 好帶 |
| B-11 | 照舊經 PMO；兩個例外（防冒用、本人在你的畫面上操作時） | harness：judge「兩個例外」 | 跨環境的原則 |
| B-12 | PM 之間可直接談實作；改範圍／順序／真機／要本人決定的事經 PMO | harness：know「工單與協作」 | 跨環境的原則；工具不重要 |
| B-13 | 某團隊的隊員工具例外不特別對待 | harness：**不寫**（不留個別例外）；（收尾）該團隊的文件與輪換表由 PMO 帶 | 共用一份就不該有單一團隊的例外 |
| B-14 | 隊員選模型的順序 | harness：know「模型」只寫「照 PMO 的模型輪換表」；**順序**→輪換表（PMO 的 repo） | 便宜先用；表會變 |
| B-15 | harness 只寫「照輪換表」；細節放表 | 同上；**細節**（某些模型不能接續某些 session、只給短任務的模型、看供應商）→輪換表 | 模型會變，harness 不跟著改 |
| B-16 | 只寫能力，不寫工具名 | harness：know「你是誰、你在哪」「回報與通道」＋capabilities（D6）；**工具名**→環境說明 | 換工具只改說明 |
| B-17 | Mac 與 worker 共用一份；差異放環境說明；專案特有留 `AGENTS.md` | 整份的結構（D1）＋D5；**附錄 A 的搬遷**＝跨 repo 收尾（各 repo 的 PM 帶） | 期二後兩邊都是有隊員的 PM；專案特有跟著 repo 走最好維護 |

**另外兩條不是 17 條決定，是協定與操作層的教訓**（PMO 2026-10-18 在 #94 留言指示寫入）：

- **收發的紀律**（收訊不指定頻道、只有送訊才指定；身分在每個工作單位唯一、狀態目錄持久；回報目錄狀態要實證；閒著用阻塞等待、逾時拉長）→ harness `know`「回報與通道」（能力層級、不寫工具名）＋環境說明（具體指令與秒數）；能用工具擋的列在「要 Angareion 配合」。**來源：M0 報告 §3／§5**（M0 的 4 個 B 回合有 2 個因協定錯誤中止）。
- **關隊員前的確認**（先中斷、確認沒有長時間的指令還活著；只關畫面不會停掉 session；共用服務重啟會把中斷的長指令重新叫醒、繼續吃額度）→ harness `know`「工單與協作」一句＋`do`「收尾」＋環境說明（操作步驟）；D7 用關鍵句釘住。**來源：PM-HANDOFF 2026-10-18 下午的教訓**（M0 的 `wait` 迴圈因此多吃了額度；M0 報告 §3／§5 沒有這段）。

### D3 識別與路徑（已定：選項 1）

現行：`atelier:worker-team-pm@v0` ↔ `atelier/worker-team/pm/v0.md`（`team: worker-team`）。`atelier install` 的目標檔名與管理區塊標記**都只用 `<team>-<role>`（不含版本）**：claude 是 `~/.claude/rules/atelier-worker-team-pm.md`（獨立檔，整個檔就是 harness），opencode 是 `AGENTS.md` 裡 `<!-- atelier:worker-team-pm -->`…`<!-- /atelier:worker-team-pm -->` 的管理區塊（依 `src/atelier/install.py`）。**v1 不再用 install 交付（D4）**，所以下面「升級」的說法是 `atelier show`／`render` 的世界。

- **選項 1（已定）：沿用 `worker-team`，新增 `atelier/worker-team/pm/v1.md`（`atelier:worker-team-pm@v1`）。**
  - 交付方拿的是 id（不指定版本＝latest）→ 下一次啟動 agent 就是 v1；**同一台機器不會有兩份 PM harness 被交付**（交付是 per-agent 的一份檔，不是檔案疊加）；`atelier show`／`--section`／`render`／格式檢查的 id↔檔名↔路徑規則都不動。
  - 缺點：名字的字面還綁 worker（Mac 也在吃同一份）；期 3 若要正式命名，那時再處理。
- **選項 2（未選）：換一個名符其實的 team 名**（例如 `shared`、`pm`、`company`……）＝`atelier:<新名>-pm@v1`。
  - 代價：交付的 id 要改（sidecar／pane／開機單位的參數）；舊的使用者層級殘留還是要清（過渡本來就要清）；`atelier show @v0` 仍要可讀（append-only），樹上會有同一職務的另一個 team 名。
- **選項 3（未選、這一版不做）：不分團隊的 `atelier:pm@v1`。** 要動 id 與路徑的文法：`src/atelier/harness.py` 的 `_ID`／`_PATH` 都要求 `<team>-<role>`（實測 `atelier:pm@v1` → 用法錯、`atelier/pm/v1.md` 不被接受），連帶 spec「識別、版本與路徑」「讀取工具」與 install 的檔名；影響面最大。

**已定（PMO 2026-10-18）：選項 1。** 本 change 的草稿（`harness/pm-v1.md`）照這個寫（`team: worker-team`、`id: atelier:worker-team-pm@v1`）。

### D4 載入：按 agent 交付（不是使用者層級）

**為什麼改（PMO 抓到的高）**：v0 把 harness 寫進**使用者層級**指示檔（`~/.claude/rules/`、`~/.config/opencode/AGENTS.md`）——同一台機器、同一個使用者的**每一個** agent 都會載入它：

- **worker**：team PM 與 M2 的隊員（opencode／Claude Code）同一台、同一個使用者 → 隊員也會載入 PM harness、以為自己是 PM。
- **Mac**：同一個使用者有 PMO、多個 PM、Claude Code 隊員、本人其他專案的 Claude Code、opencode 隊員 → 裝一次就全部被套上 PM 規矩。

**方向（v1 起）**：**按 agent 載入**——開 agent 的那一方（worker：隊員控制服務的 `open`／headless continue；Mac：開 pane 的指令／`agora continue`）在啟動**那一個** agent 時，依它的角色把 harness 交給它；**只有 PM 角色拿 PM harness，隊員不拿**。使用者層級指示檔不再是 v1 的交付管道。

**機制選項**（本機版本：opencode **v2.0.32**、claude **2.1.302**；「實測」＝這次在隔離環境真的跑過，「推測」＝只驗了旗標／文件）：

| 選項 | claude 這邊 | opencode 這邊 | 成本與風險 | 標記 |
|---|---|---|---|---|
| **A（建議）啟動參數／每 process 設定** | `--append-system-prompt-file <檔>` ＋ `--system-prompt-snapshot off` | `OPENCODE_CONFIG_CONTENT` 的 `instructions: [<絕對路徑>]`（或 `OPENCODE_CONFIG=<檔>`） | 要改啟動方（sidecar／pane／`agora continue`）；harness 檔要留在 agent 讀得到的地方；共用背景服務會破壞隔離（見下） | opencode 實測；claude 旗標存在（help／binary）、行為推測 |
| **B 每個 agent 一份設定目錄** | `CLAUDE_CONFIG_DIR=<dir>` | `OPENCODE_CONFIG_DIR=<dir>`（`opencode.json` 直接放在那個目錄） | sidecar 多設環境變數；每份目錄要**自己帶齊**設定（實測：設了 `OPENCODE_CONFIG_DIR` 之後，使用者的 `$XDG_CONFIG_HOME/opencode/AGENTS.md` 不再載入）；憑證／設定的複製是風險 | opencode 實測；claude 推測 |
| **C 第一則輸入／訊息** | `open` 的 `input` 帶全文 | 同 | 最省事，但只在新 session、會被歷史與壓縮沖淡；v0 已裁定不靠它（E11） | 推測（不建議當主機制） |
| **D 自訂 agent 的 prompt** | `--agents <json-or-file>`（定義一個 `pm` agent） | 設定 `agents.<name>.prompt` ＋ `--agent <name>` | 看起來最貼角色；但 opencode 這條**實測不生效**，claude 只驗了 help | opencode 實測（不生效）；claude 推測 |

**實測（隔離環境：`HOME` 與四個 XDG 指到 `/tmp`、專案自建、模型是本機 `tests/fakes/fake_upstream.py`、每次 `--standalone`、受管理的服務埠設成自由埠；沒有碰 49374 與真資料）**：

- **opencode `instructions`（A）**：帶 `OPENCODE_CONFIG_CONTENT='{"instructions":["<絕對路徑>"]}'` 的那一次 `opencode run`，假 upstream 收到的請求裡**有** harness 的 marker；**同一個隔離使用者、不帶設定的下一次 `opencode run` 沒有**（前後各一次對照）→ **只有那個 process 吃得到**。
  - `OPENCODE_CONFIG=<檔>` 同樣有效（實測）。
  - **路徑要絕對**：相對路徑實測不生效（marker 沒出現）。
  - 指向**不存在的檔**：`opencode run` 照常 exit 0、instruction 被忽略、**沒有錯誤訊息**（交付方要自己先驗檔案存在）。
  - 對照：使用者層級 `$XDG_CONFIG_HOME/opencode/AGENTS.md` 的 marker 在**每一次** run 都出現——現況的問題在隔離環境裡重現。
- **opencode `OPENCODE_CONFIG_DIR`（B）**：`opencode.json`（含 provider 與 `instructions`）直接放在那個目錄 → harness 有進模型（實測）；**但同一次 run 裡使用者層級的 `AGENTS.md` 沒有被載入**（實測）→ B 要自己帶齊所有設定。
- **opencode `agents.<name>.prompt`（D）**：v2 的設定鍵是 `agents`（不是 v1 的 `agent`）。作者的一次實測裡 `agents` 定義的 agent 曾出現在 `debug agents`，但**另一次審查重現不到**（回 `[]`）；不管列不列得出來，`opencode run --agent <name>` 的請求裡**看不到**那段 prompt——**inline 文字與檔案路徑都試過，都不生效**（實測）。不要押這條。
- **opencode 的共用背景服務會破壞隔離**：per-process 設定只對「自己起的 process／server」有效；若 agent 走同一台機器共用的 managed 服務，設定不是 per-agent 的。所以交付要在**per-agent 的 process** 上做（sidecar 的 headless 已經是每個 continue 一個 `opencode serve`）。
- **claude 的旗標（只到「存在」這一級）**：`--system-prompt-snapshot <on|off>`、`--agents`、`--resume` 在 2.1.302 的 `--help` 裡；`--append-system-prompt` 也在 help。**`--append-system-prompt-file` 不是 help 的獨立選項**（只在描述裡以 `--append-system-prompt[-file]` 出現），但 binary strings 有 `--append-system-prompt-file <file>` → **旗標存在**。`CLAUDE_CONFIG_DIR` 在 binary 裡（官方文件也列）。**`--help` 的旗標探測不具區分力**（給一個不存在的旗標加 `--help` 也 exit 0）。
- **claude 的行為（推測）**：help 明寫 `--system-prompt-snapshot`：**on（預設）**＝第一次請求時把系統提示（含 `--append-system-prompt`）記下來，之後的請求與 **resume** 都沿用那一份，「即使後來的啟動傳了不同的文字」，直到壓縮；**off**＝不記錄、每次請求重新渲染。→ 要讓「每次啟動拿到當時的 harness」就**必須帶 `off`**，否則 resume 的 session 會沿用舊的（甚至沒有 harness 的）提示。**旗標真的會把內容放進系統提示、`off` 在 resume 真的會重讀、缺檔錯誤的時機——都沒有行為證據**（推測）。
- **⚠️ 揭露：作者誤跑過一次 `claude -p`**（`--system-prompt-snapshot off -p hi`，隔離 HOME），它印出像模型回覆的文字、疑似真的呼叫了模型；**那次輸出沒有被採用**，本節的行為結論只來自 help 文字、binary strings 與 `src/agora/agents/claude.py` 的 argv。之後沒有再跑任何 `claude -p`。
- **啟動 argv（實查 `src/agora/agents/claude.py`）**：互動＝`claude --resume <id>`；headless＝`claude -p --input-format stream-json --output-format stream-json --verbose --resume <id> --settings '{"autoContinueAtUsageLimit": false}'`。交付要加在**這兩個** argv 上。opencode 的兩條（`opencode --session <id>`／`opencode serve --hostname … --port …`）同理。

**建議：選項 A。** opencode 的 `instructions` 已**實測**是 per-process（同一使用者、另一個 process 吃不到），不動使用者的檔案、也不動 agent 的設定目錄；claude 用啟動旗標是同一條思路（旗標存在、語意由官方 help 寫明，**行為待實作時驗**）。成本集中在**啟動方**：sidecar 的 `open`／headless continue、Mac 的開 pane 指令與 `agora continue`（還需要 `Launch` 的 env 通道，見介面清單第 2 條）各帶一個參數；`atelier` 要提供「產生這份檔」的子指令（見下面「要 MyLinuxPool 配合的介面變更」）。

**v0→v1 的過渡（**現在**的問題要明講）**：

- **現在**：worker 上以使用者層級安裝的 **v0** 已經會被 **M2 的隊員**載入（同一台、同一個使用者的 `~/.claude/rules/atelier-worker-team-pm.md` 與 opencode `AGENTS.md` 的區塊）——隊員會看到「你是 PM」的規矩。
- **緩解（依序）**：(1) 先做 per-agent 交付；(2) 開機單位**停止** `atelier install`（不再把 PM harness 放進使用者層級）；(3) **清掉**使用者層級的殘留（claude：刪 `~/.claude/rules/atelier-worker-team-pm.md`；opencode：刪 `AGENTS.md` 裡那個管理區塊——沒有 `uninstall`，人工做，經 PM 授權）；(4) v1 上線（PM 由啟動方交付）。在 (2)(3) 之前，隊員被 v0 污染的風險**還在**；暫時只能靠 brief 明寫角色與範圍、PM 盯隊員行為——這是暫時的，不是解法。
- v0 檔**不動**（append-only）；`atelier show @v0` 永遠讀得到；只是不再被交付。`status: draft` 也不能原地改 `stable`／標 `deprecated`（提議：`docs/atelier/harness.md` 寫一段「v0 已被 v1 取代」）。
- **生效時機**：per-agent 交付是**每次啟動 agent 都重新交**——沒有「重啟才重裝」的限制；已開著的 agent 要重開才拿到新版（claude 還要 `--system-prompt-snapshot off` 才會在 resume 時重讀）。
- **Rollback**：啟動方不帶交付參數（回到「沒有 harness」），或把交付指回 `@v0`；harness 樹不動。
- **`install` 還要不要留？** 建議**留著**（v0 的機制、以及「整台機器只有一個 agent」的全域用途），但 **v1 的 PM harness MUST NOT 用 install 交付——唯一例外是明寫 `--global` 的全域安裝**（單一 agent 的機器；安裝時印警告）；worker 的開機單位不再呼叫它。**而且光有規矩不夠（M0 的教訓：不靠自覺），工具要擋**——三個選項：
  1. **（建議）`install` 對 `v>=1` 的 `pm` harness 預設拒絕**（exit 1，訊息說「改用 `render`／啟動方交付」）；真的要全域安裝時，必須明寫 `--global`，並在 stderr 印一行警告「同一個使用者的每個 agent 都會拿到這份 PM harness」。測試釘住兩件事：沒有 `--global` → exit 1、什麼都沒寫；有 `--global` → 安裝成功且警告有印。
  2. 一律拒絕（沒有 `--global` 的後門）：最安全，但「只有一個 agent 的機器」就沒有合法路徑。
  3. 只加警告、照舊安裝：最省事，但擋不住舊習慣／舊腳本，等於沒修。
  → 選 1；主 spec 的「安裝」requirement 要跟著改（原本說它不動，現在有 `--global` 與拒絕的 MUST）。

### D5 環境說明（已定：選項 A；交付也按 agent）

「環境說明」＝ 每個環境一份的短文件：你在哪、通道怎麼收發（用什麼）、能力由誰提供、工作檔放哪、真機怎麼動、怎麼請本人接手（Mac：「在你的畫面上自己執行」／worker：「經通道回報 PMO」）。**已定（PMO 2026-10-18）：選項 A——放環境那一側，由環境的主人維護**（worker＝MyLinuxPool；Mac＝本人／PM）；不進 harness 樹、不套 harness 格式（它是環境的文件，不是職務）。

**環境說明要寫的具體項目**（harness 只寫能力，實際指令與秒數在這裡）：

1. 通道的指令用法：**收訊不加頻道、送訊才指定頻道**；等待的指令與**逾時秒數**（數分鐘級）；身分名怎麼取、狀態目錄在哪（要持久）。
2. 關隊員的步驟：先中斷它正在跑的工作、確認沒有長時間的指令還活著、再收掉畫面／session；共用服務重啟會把 session 裡中斷的長指令重新叫醒（M0 實際發生過）。
3. **開或接回多個 agent／session 時一個一個來、中間隔幾秒**；不要同時起多個。原因：共用的 opencode 服務一旦有多個 client 同時起它，搶到的那個開很大的 session 資料庫時會「database is locked」，之後所有 client 都被拒，要人工重啟服務（2026-10-18 實際發生）。
4. 真機怎麼動、被擋時怎麼請本人接手、工作檔放哪、能力由誰提供。

**交付：和 harness 一樣按 agent 交付。** 啟動 PM 時，由啟動方把「harness ＋ 該環境的說明」一起交給那一個 agent，不是寫在使用者層級。建議做法：`atelier render` 把 harness 與環境說明**合成一份**「這個 agent 的指示檔」（`render <id> --append <env-note> --out <path>`），啟動方再交給 claude 的 `--append-system-prompt-file`／opencode 的 `instructions`。這樣 harness 樹保持公開、可審、與機器無關；環境說明留在環境；交付路徑只有一條。

（未選的紀錄保留：**B**＝環境說明也做成 harness 檔——借名文法可行（審查實測 `atelier/mac/pm/v1.md` → `('mac','pm',1)`），但它會被當成一份 pm 職務 v1，格式檢查的 MUST 句與 D7 的黑名單都會套上去，而環境說明正是要寫工具名的地方；**C**＝只放 `docs/`，agent 看不到。兩者都不選。）

### D6 能力名（已定：選項 I）

現行環境（worker profile）實際提供的能力名是 `angareion`／`github`／`agora`／`llmgateway`／`sidecar`／`atelier`。共用一份之後，v0 的三個名字只有兩個可以照用（`github`、`agora`）；通道與隊員管理在 Mac 與 worker 是不同的東西（工具名不能寫，B-16），所以需要**跨環境的中性名**。

- **選項 I（建議）**：`github`、`channel`（跟 PMO 收發）、`agora`、`team`（開／派／讀／關隊員）、`monitor`（長時間監看）。環境說明負責對應（worker：`channel` 由環境的 `angareion` 提供、`team`／`monitor` 由隊員控制服務提供；Mac：由 Mac 上的工具提供）。
- **選項 II（保守）**：保留 `angareion` 當通道的名字，只加 `team`／`monitor`。好處：與 worker 的既有能力名一致（v0 也是這樣）；壞處：Mac 上沒有這個能力，名不符實。
- **選項 III**：`github`、`agora` 之外全用中性名（含把隊員管理再拆細，例如 `dispatch`／`watch`）。好處：最貼 B-16；壞處：名字多、環境要對更多次。

**已定（PMO 2026-10-18）：選項 I。** 五個能力的參數都留空（跟 v0 一樣）；未知名稱在格式檢查只警告、不擋。草稿照這個定案寫（`github`／`channel`／`agora`／`team`／`monitor`）。

### D7 可驗收的格式檢查（沿用 `tests/unit/test_atelier_harness.py`）

| 檢查 | 現況 | v1 要補什麼 |
|---|---|---|
| frontmatter 必填、`id`↔`version`↔檔名一致、五章齊 | 已有 | 不動（v1 照同一套） |
| `capabilities` H3 子節、未知只警告、子節名不重複、參數一行 YAML | 已有（`KNOWN_CAPABILITIES = github／angareion／agora`） | 更新已知清單（加 D6 定的名字） |
| v0 職務檔 `MUST` 有 `angareion` 子節 | 已有（只在 `v==0`） | 不動 |
| append-only（與 `origin/main` 的 merge-base 比對） | 已有 | 不動（v1 是「新增版本檔」這條唯一允許的變化） |
| `know` 明寫「專案自己的指示檔與工單規則優先」 | 已有 | 不動（v1 必須有） |
| 「審查一律非作者」 | 已有（只驗 v0） | **擴到所有 pm 版本**（v1 的 judge／dont 也要有） |
| **關鍵句**：`不是權限`（B-1）、`照 PMO 的模型輪換表`（B-15）、`不指定頻道`（收發紀律）、`長時間的指令`（關隊員） | 無 | 新增：把 `know`＋`do`＋`judge`＋`dont` 的正文**合起來**（聯集）比對，每一句至少出現一次。**範圍只限 `role: pm` 且 `v>=1`**（這四句是 PM 職務的義務，不是所有 `v>=1` 職務的義務——spec delta 把它們放在 ADDED「PM 職務（`v1`）」，MODIFIED「harness 檔的格式」沒有它們；`v0` 是 append-only、也豁免）。**不採「每一段都要有」**——照字面會紅（`不是權限` 只在 know／judge，`照 PMO 的模型輪換表` 只在 know；實測見審查 A-1）。後兩句是 M0 的協定教訓（PMO 指示寫入）；**工具擋得住的部分另外列在「要 Angareion 配合」**，這裡只釘住 harness 有沒有寫 |
| **工具名／模型名黑名單**（B-16／B-15） | 無 | 新增：`know`／`do`／`judge`／`dont` 的**正文**不得出現具體工具名與模型名（清單放測試一處）；**只驗 `v>=1`**（v0 是既成事實，append-only 不能改）。比對前要先遮掉 `CLAUDE.md`／`AGENTS.md` 這兩個**專案指示檔的檔名**——`know` 的 MUST 句本來就要寫它們，字串比對會誤殺（`CLAUDE.md` 含 `claude`）。清單不要收**一般詞**（`session`／`輪換表`／`通道`／`CI`／`ssh`／`commit`／`repo`……規矩的句子本身要用）。frontmatter 的 `generated` 不算正文（spec 已明寫）（B-16／B-15） |
| **機器位址與家目錄路徑**（公開 repo） | 無 | 新增：不得出現 IPv4 樣式、`/Users/`、`~/`（`v>=1`）。**通用系統路徑不算**——`/tmp` 是 `v1` 的 B-9 句要用的字（spec 已明寫） |
| **共用一份**：同一職務的版本不該綁單一環境 | 無 | 新增（**只警告、不擋**——未知能力名照 spec 只警告）：v1 的 `team`／`role` 與 v0 相同（同一職務的下一版）；`capabilities` 不含只有單一環境才有的名字（D6 已定的 `channel`／`team`／`monitor` 是中性名，`github`／`agora` 兩邊都有） |
| 改壞驗證 | 已有 12 項 | 每一條新檢查各一個注入（拿掉關鍵句、塞一個工具名、塞一個模型名、塞一個 IP、塞一個 `~/` 路徑）都要紅；**共用一份那條是警告**：注入後 assert 有 warning、不是 error |

`KNOWN_CAPABILITIES` 這一類「清單」只影響警告還是錯誤：**未知能力名只警告、不擋**（spec），所以清單更新是為了讓已知的能力名有人顧，不是把它變成硬門檻。

### D8 要不要寫 spec delta（為什麼要）

**要。** 六個理由：

1. **格式有變**（B-15／B-16）：內容限制多了「只寫能力、不寫工具名」「模型只寫照輪換表」——這是 harness 檔的規格（「harness 檔的格式」）。→ **MODIFIED**。
2. **能力需求的子節名規則要放寬**：原則是「子節名 SHOULD 用環境的 capability 名（v0：`github`／`angareion`／`agora`）」——共用一份的職務有一半的能力在兩個環境是不同的東西，要允許中性名（D6 已定）。→ **MODIFIED**。
3. **v1 職務的內容是新的要求**（有隊員的 PM；Mac 與 worker 共用；單人暫定結束）。→ **ADDED**；同時把「v0 職務」的最後一句（「此暫定由期 3 的三職務正式版取代」）改成「由 v1 起的 PM 職務取代，期 3 再由 Atelier 本體接手」→ **MODIFIED**（其餘 scenario 逐字保留）。
4. **載入範圍變了**（D4）：v1 起**按 agent 交付**、不是使用者層級；PM harness MUST NOT 被非 PM 角色載入。這是可觀察的行為（同一台機器上隊員拿不到、使用者層級檔案不變），不是實作細節。→ **ADDED**「按 agent 交付 harness」。
5. **多了一個寫檔的子指令**（`atelier render`）：主 spec 的「讀取工具」寫「對外 MUST 只有讀的操作：`atelier ls` 與 `atelier show`」——不加 delta，PR 2 就與主 spec 打架。→ **MODIFIED**「讀取工具」（把「只有讀」限定在 `ls`／`show`）＋ **ADDED**「產生 agent 指示檔（`render`）」。
6. **`install` 要加守門**（M4）：v1 的 pm harness 預設拒絕、要 `--global` 才行——這是 install 的行為改變。→ **MODIFIED**「安裝（`atelier install`）」。

**不用改**的：識別／版本／路徑（`atelier:<team>-<role>@v<N>` 的通則沒變）、harness 樹、「與 Agora Session 的關係」。**讀取工具與安裝要改**（理由 5／6）；`v0` 職務的 scenario 逐字保留；`安裝` 的既有 scenario **pin `@v0` 後保留**（不 pin 的話 v1 之後會被新守門拒絕——M2），只加新的。

**順帶在「能力需求與模型欄位」裡拿掉主 spec 的兩處環境細節**：一個是 `SHOULD` 句裡的**別 repo 名**（原文寫出環境所在的那個 repo），一個是「`github` 的單位是某個環境單位」（原文寫出單位路徑）。理由是公開 repo 不寫別的 repo 的名字（本 change 的規矩），而環境的單位名正是 B-16 要留給環境說明的細節。**這是刻意的刪除**，不是順手刪——所以列在這裡、delta 的開頭與「作者提議」也各記一筆。

## 要 MyLinuxPool 配合的介面變更（本單不實作；PMO 排程，MyPMO#86 會用到）

**共同前提**：交付＝「啟動那一個 agent 時，把『harness ＋ 該環境的說明』交給它」。harness 內容由本 repo 的 `atelier` 產生；**怎麼交給 process** 由啟動方做（opencode：`OPENCODE_CONFIG_CONTENT` 的 `instructions`／`OPENCODE_CONFIG` 檔；claude：`--append-system-prompt-file` ＋ `--system-prompt-snapshot off`）。啟動方**MUST 先確認檔案存在**（opencode 對不存在的 instruction 是靜默忽略，實測）。

**本 repo（MyAiStorage）的 `atelier` 要加的東西**

1. **新增 `atelier render <id>[@v<N>] [--append <檔>] --out <檔>`**：讀 harness（不指定版本＝latest）、可選附加一份環境說明、**原子寫入** `--out`（同 install 的寫法）。啟動方用它產生「這個 agent 的指示檔」。
   - 退出碼沿用：0 成功／1 用法錯、查無 id（含版本）、**`--append` 的檔不存在**／2 讀取或寫入失敗；錯誤訊息不含內文。
   - `render` 是**只寫 `--out` 的寫入操作**：不碰 harness 樹、不碰使用者層級檔案、不碰工作目錄。
   - **spec delta 有它自己的 requirement**（「產生 agent 指示檔」）；主 spec 的「讀取工具」也一起改成「讀的操作只有 `ls`／`show`」——不然 PR 2 會與主 spec 的「對外 MUST 只有讀的操作」打架。
1b. **`install` 的守門**（M4：規矩要有工具擋）：`install` 對 `v>=1` 的 `pm` harness **預設拒絕**（exit 1，訊息指向 `render`／啟動方交付）；要全域安裝必須明寫 `--global`，並在 stderr 印警告「同一個使用者的每個 agent 都會拿到這份 PM harness」。測試：沒有 `--global` → exit 1、什麼都沒寫；有 `--global` → 成功且警告有印。主 spec 的「安裝」requirement 要跟著改（加上這條 MUST）。
2. **`agora continue`（`src/agora/agents/claude.py`／`opencode.py` 的 `start_native`）**：新增**每次呼叫**的選項 `--harness-file <檔>`（不要用 ambient 環境變數——PM 的 shell 帶著它去接續隊員的 session 時，隊員會拿到 PM harness）。
   - **`Launch` 要加 `env` 通道**（現在只有 `argv`／`cwd`／`agent_session_id`／`before_count`）：`Launch.env: dict`（預設空），`src/agora/cli.py` spawn 時用 `{**os.environ, **launch.env, "PWD": …}`；**不要**在 `start_native` 改 `os.environ`（會污染父 process 與之後的 child）。
   - claude：`--harness-file` → argv 加 `--append-system-prompt-file <檔>` ＋ `--system-prompt-snapshot off`。
   - opencode：`--harness-file` → `Launch.env` 加 `OPENCODE_CONFIG_CONTENT={"instructions":[<絕對路徑>]}`（**實測與檔案設定合併**，只帶 `instructions`；**不要**把整份設定（含 provider 的 `apiKey`）重新序列化進環境變數）。
   - **測試**：只有帶 `--harness-file` 的那個 process 吃得到；同一個使用者、不帶的下一個 session 吃不到；檔案設定（provider、檔案自己的 `instructions`）都還在。

**worker（MyLinuxPool）**

3. **sidecar 的 `open`（新 session）與 `continue --headless`**：欄位形狀（**已定：PMO 2026-10-18 在 PR #95 留言的介面調和，與 MyPMO#80 的命名定案對齊；實作時以那一條為準，細節見 MyLinuxPool#216**；原本的作者提議見「作者提議」15，已被取代）——
   - **不收 `role` 欄位**：角色一律**由 AI 名字推出**（例如 `mylinuxpool-pm-w1` → 角色 pm；本人 Q9「由角色推」）。
   - **預設 harness＝`atelier:<harness_series>-<角色>`**；**選填 `harness` 可以覆寫，形狀 `{id, env_note}`**（`env_note` 可省略），例如 `harness: {"id": "atelier:worker-team-pm", "env_note": "<worker 環境說明的路徑>"}`。
   - **推出來的 harness 在 Atelier 不存在（目前隊員的角色都是這樣）→ 不注入、不算錯**；隊員本來就不拿 PM harness。
   - **秘書不由 sidecar 開**，照 MyPMO#80 回 **400**。
   - sidecar 在起 agent **之前**：跑 `atelier render <id> --append <env_note> --out <agent 自己的 state 目錄>/instructions.md`。
   - 注入：opencode＝`OPENCODE_CONFIG_CONTENT` 只帶 `instructions`（**實測與檔案設定合併**：provider 與檔案自己的 instructions 都保留；不要重貼整份設定，會把憑證帶進環境變數）；claude＝driver 的 argv 加 `--append-system-prompt-file <path>` ＋ `--system-prompt-snapshot off`（headless 與互動兩條都要）。
   - **錯誤情形**：`render` 回 1／2 → **不開 agent**，`open` 回 4xx／5xx 並附原因（不要開一個沒有規矩的 PM）；推出的 harness 不存在 → 不注入、不算錯（隊員）；**`continue` 的 harness 來源＝`open` 時記在 sidecar 的 session metadata**，`continue` 沿用；metadata 沒有（舊 session）→ 不注入＋記一行警告（安靜地讓 PM 沒有規矩是不行的，要讓 PMO 看得見）。
   - **生命週期**：`--out` 放 agent 自己的 state 目錄（不是 `/tmp`、不是 repo）；每次啟動重寫（版本跟著 main 走）。
4. **開機單位（`start.d/20-atelier`）**：**移除 `atelier install`**（改成只驗證：`atelier show --section capabilities` 之類，或整個單位刪掉）；不再把 PM harness 寫進使用者層級。**它遇到 `atelier install` exit 1 時 MUST 忽略並記一行警告、不擋開機**（照 MyLinuxPool#130 的契約；訊息要說「這一輪沒有裝 harness」）。
   - **順序（H1）**：開機單位跑的是**不指定版本**的 install（＝latest）——PR1 一合併，latest 就是 v1，下一次 worker 重啟就會把 v1 寫進使用者層級、所有 agent 都拿到。所以**停 install／pin 版本 MUST 在 PR1 之前或同一個部署批次**（見 Migration Plan）。
4b. **sidecar 的 `close`／`status`**：`close` 要真的結束 agent 的 process 與它 session 裡還在跑的長指令（不只關 pane）；`status` 要能看出「還有指令在跑」（M0：只關畫面不會停 session，共用服務重啟會叫醒中斷的長指令）。
5. **清理一次使用者層級殘留**（過渡，人工、經 PM 授權）：刪 `~/.claude/rules/atelier-worker-team-pm.md`；刪 opencode `AGENTS.md` 裡的 `<!-- atelier:worker-team-pm -->` 區塊（沒有 `uninstall`）。

**Mac（本人／PM）**

6. **開 pane 的指令**：PM 的啟動帶交付（claude：`--append-system-prompt-file` ＋ `--system-prompt-snapshot off`；opencode：`OPENCODE_CONFIG_CONTENT`＋`--standalone`）；PMO 與隊員的啟動**不帶**。
7. **驗收**（PMO／本人）：同一台機器同時開 PM 與一個隊員，隊員的請求裡**沒有** harness；PM 的有；`~/.claude/rules/` 與 `~/.config/opencode/AGENTS.md` **逐字不變**。

**要 Angareion 配合（工具能擋的部分）**（本 repo 的 tool；#96 已涵蓋前兩項）

1. `read`／`wait` 帶 `--channel` 時**印警告**：這會包含寄給別人的訊息（#96）。
2. `wait` 之間**保留 ETag**（同一個等待不重複抓完整清單；#96）。
3. **新的狀態目錄／新的身分**第一次 `read` 時警告：沒有游標，會從所有頻道的第一則開始讀（M0 的 r2b 事故）。
4. `wait` 的**預設逾時**拉到數分鐘級（例如 600 秒；文件寫明為什麼），逾時時印一行「可以再等一次」，不要讓 agent 以為要改成輪詢。
5. （擋不住）「隊員回報目錄狀態要實證」——工具不知道 agent 有沒有看過；留在 brief 與 PM 的核對（harness 的 `judge`）。

## 作者提議、本人未指示

0. **三題已定（PMO 2026-10-18）**：D3＝選項 1、D5＝選項 A、D6＝選項 I；設計照定案寫，不再列為待決。
1. **per-agent 交付的機制**（D4）：建議選項 A（opencode 的 `instructions` 實測 per-process；claude 的啟動旗標存在）。未選：B（每 agent 一份設定目錄——實測會失去使用者層級的 `AGENTS.md`）、C（第一則輸入——只在新 session）、D（自訂 agent 的 prompt——opencode 實測不生效）。**要 PM／PMO 確認**，因為會動 MyLinuxPool 的 sidecar。
2. **Mac 的 PM 由誰交付**：開 pane 的指令（本人／PM 的腳本）＋`agora continue`；腳本誰維護未定。
3. **`atelier render`（新子指令）與「harness＋環境說明合成一份」**：作者提議；`install` 留著（v0 遺留＋全域用途），但**要加守門**——`v1` 的 pm harness 預設拒絕、要 `--global`（M4 的三個選項，選 1）；`--global` 這個名字與警告文字要 PM 確認。
4. **`Launch` 加 `env` 通道**（`agora continue` 用得到；opencode 的 per-process 設定只能走環境變數）＋每次呼叫的 `--harness-file`（不用 ambient 環境變數）——作者提議，會動 `src/agora/`。
5. **claude 的交付要帶 `--system-prompt-snapshot off`**（help 明寫：on 會讓 resume 沿用舊的系統提示；行為推測）。
6. **格式檢查的黑名單範圍**：工具名與模型名（B-15／B-16）之外，我把它擴到「其他專案的名字、機器位址、使用者家目錄路徑（`/Users/`、`~/`）」（公開 repo）；`github`／`agora` 這種**本專案自己的元件名**與 `/tmp` 這種通用系統路徑不算，清單也不收一般詞（`session`／`輪換表`／`通道`／`CI`／`ssh`……）。範圍要 PM／審查者核一下。
7. **v1 的 `models` 照 v0 留空**（欄位在、值之後由本人決定）（E4 已答覆的延伸）。
8. **v0 不標 `deprecated`**（append-only 不能改檔）；「v0 已被 v1 取代」寫在 `docs/atelier/harness.md`。
9. **環境說明不套 harness 格式**（它是環境的文件，不是職務）。
10. **`harness/pm-v1.md` 先放 change 內**，定稿後才放進 `atelier/worker-team/pm/v1.md`（同 v0 的做法）。
11. **`docs/atelier/harness.md` 與 `src/atelier/harness.py` 的已知能力名**跟著 v1 更新（文件與警告清單）。
12. **spec delta 的範圍**（D8）：動「格式」「能力需求與模型欄位」「v0 職務」「讀取工具」「安裝」＋新增「PM 職務 v1」「按 agent 交付 harness」「產生 agent 指示檔（`render`）」；其中「能力需求與模型欄位」**刻意**順帶拿掉兩處環境細節（`github` 的單位、環境的 repo 名），理由見 D8。
13. **M0 的收發紀律與關隊員規矩**（PMO 2026-10-18 在 #94 留言指示寫入）：收發紀律的來源＝M0 報告 §3／§5；**關隊員那條的來源＝PM-HANDOFF 2026-10-18 下午的教訓**（M0 的 `wait` 迴圈因此多吃額度；M0 報告 §3／§5 沒有這段）。作者提議的是措辭、D7 的關鍵句，以及「哪些能用工具擋」的清單（`--channel` 警告與 ETag 由 #96 涵蓋）。
14. **一次只起一個 agent／session**（PMO 2026-10-18 指示，寫進 D5 的環境說明；harness 不寫——屬環境、也不寫工具名）：共用的 opencode 服務被多個 client 同時起時，搶到的那個開大 session 資料庫會 `database is locked`，之後所有 client 被拒、要人工重啟（2026-10-18 實際發生）。
15. **sidecar 的 `harness`／`role` 欄位形狀**——**已定（PMO 2026-10-18，PR #95 留言；與 MyPMO#80 命名定案對齊，細節 MyLinuxPool#216）**。原本的三個選項：(a) 只收 `harness: {id, env_note}`；(b) 只收 `role`（由 sidecar 的設定表解析成 id／env note）；(c) 兩者都收（`harness` 優先、`role` 是別名；衝突拒絕）。**定案不是 a／b／c 任何一個**：`open` **不收 `role` 欄位**，角色**由 AI 名字推出**（`<前綴>-<角色>-<序號>`，例如 `mylinuxpool-pm-w1` → pm）；**預設 harness＝`atelier:<harness_series>-<角色>`**；**選填 `harness`（形狀 `{id, env_note}`）可覆寫**；推出來的 harness 在 Atelier 不存在（目前隊員的角色都是這樣）→ **不注入、不算錯**（隊員不拿 PM harness）；**秘書不由 sidecar 開，照 MyPMO#80 回 400**。

## Risks / Trade-offs

- [共用一份 → 內文容易愈寫愈抽象] → 每條規則都要有落點（D2 的表）：原則留 harness、具體做法放環境說明、專案特有放 `AGENTS.md`；寫不出落點的就不要進 harness。
- [交付依賴啟動方] → 啟動方沒帶參數＝PM 沒有規矩（而且**不會報錯**）。介面清單把「`render` 失敗就不開 agent」寫成 MUST；驗收要看「PM 的請求裡有 harness、隊員的沒有」。
- [使用者層級殘留沒清] → 過渡期隊員仍會被 v0 污染；遷移把「停 install」與「清殘留」列成獨立步驟，順序在 v1 上線前。
- [opencode 的共用背景服務] → per-process 設定只對自己起的 process／server 有效；sidecar 的 headless 已是每個 continue 一個 serve；若有人把 agent 接到共用的 managed 服務，交付會失效。
- [版本差異] → `instructions` 在 2.0.29 實測**不生效**、2.0.32 生效：worker 的 opencode 釘的版本比本機舊；worker 的 PM 目前是 claude，所以 opencode 這條主要用在本機（2.0.32 實測）；**若 worker 上要跑 opencode PM，要先升級 pin 並重驗**。claude 的旗標在 worker 釘的 2.1.296 上沒驗（本機 2.1.302 有）。
- [claude 的 resume 語意] → `--system-prompt-snapshot` 預設 on 會沿用第一次記錄的提示（help 明寫）→ 交付要帶 `off`；不然換版／換 harness 後 resume 還吃舊的。
- [「一定會載入」≠「一定會遵守」] → 沿 v0：行為驗收看（PM 起來後收指示、派工、開 PR、回報的格式）；不遵守就改寫法。
- [append-only 的副作用] → v0 不能標 `deprecated`、`status` 不能原地改 `stable`；要改就新增版本檔（提議：先留 `draft`，本人驗收後再決定要不要發 v2）。
- [公開 repo 的內容風險] → 黑名單檢查（D7）＋非作者審查；本 change 的來源（決定的原文、輪換表）**不進 repo**。
- [黑名單可能誤殺] → 清單放測試一處、範圍寫清楚（只驗 `v>=1` 的正文，不含 `capabilities` 子節名與 frontmatter）；`CLAUDE.md`／`AGENTS.md` 這兩個檔名先遮掉再比對（`know` 的 MUST 句一定要寫它們，實測會撞到 `claude`）；清單不收一般詞（`session`／`輪換表`／`通道`／`CI`／`ssh`／`commit`／`repo`——規矩的句子本身要用）；誤殺就改清單，不要放寬原則。
- [M0 的教訓：規矩靠自覺就會出事] → 4 個 B 回合有 2 個因協定錯誤中止 → harness 寫明（`know`／`do`）＋D7 的關鍵句檢查＋Angareion 的警告（能擋的先擋），不要只靠隊員自覺（M0 報告 §3／§5）。
- [M0 的教訓：關隊員只關畫面] → 只關畫面不會停掉 session，共用服務重啟會把中斷的長指令叫醒、繼續吃額度 → `do`「收尾」寫明「先中斷、確認長指令不在」＋環境說明的操作步驟；sidecar 的 `close` 也要真的結束 process（介面清單）。
- [交接的檔案沒人清] → `render --out` 放 agent 的 state 目錄、每次啟動重寫；agent 關掉後留著（下次覆蓋），不堆積。
- [install 仍可手動把 v1 裝回使用者層級] → 光寫規矩不夠（M0 的教訓）→ `install` 對 `v>=1` 的 pm harness 預設拒絕、要 `--global`（D4 的三個選項，選 1）＋測試釘住；開機單位的 install 一併移除。
- [交付用 ambient 環境變數會漏給隊員] → `agora continue` 用**每次呼叫的 `--harness-file`**（不是 shell 環境變數）；sidecar 的 `continue` 用 `open` 時記下的 session metadata。

## Migration Plan

**部署順序是這個 change 的一部分（H1）**：worker 的開機單位跑的是**不指定版本**的 `atelier install`（＝latest）。PR 1 一合併，latest 就是 v1；在那之後、開機單位移除 install 之前，任何一次 worker 重啟都會把 v1 的 PM harness 寫進使用者層級，**所有 agent（含隊員）都拿到**——正是這個 change 要修的事。所以「停 install／pin 版本」**MUST 在 PR 1 之前或同一個部署批次**。

### 階段 A：部署前置（PR 1 之前或同一批次；跨 repo，PMO 排）

1. **（建議）先把開機單位 pin 到 `@v0`**：`atelier install atelier:worker-team-pm@v0 --agent claude --agent opencode`。一行改動，重啟也只會裝 v0（**不會**把 v1 裝進使用者層級），PM 在過渡期還有（v0 的）規矩；隊員被 v0 污染的問題仍在（已知風險，用 brief＋PM 盯緩解，不是解法）。
2. **（交付機制就緒後）移除 install**：開機單位只做驗證（或整個刪掉）；遇到 `install` exit 1 **忽略＋記一行警告、不擋開機**。
3. **備案**：如果 1 做不到，PR 1 **不得合併**；PR 1 合併後到 1 完成之前 **worker 不得重啟**（不建議依賴人記——M0 的教訓：不靠自覺）。

**為什麼建議 pin `@v0` 而不是直接移除 install**：直接移除後，PM 在交付機制就緒前完全沒有 harness（會用舊習慣做事），而交付是跨 repo、排程不在這個 change；pin 把「v1 外洩」這個新風險關掉，代價只是維持已知的 v0 污染到清理為止。
**為什麼不用「同一個批次」**：批次要跨 repo 同時上，實務上很難保證原子性；pin 是一個 repo 的一行，先做最穩。

### 階段 B：程式與規格（MyAiStorage）

4. **PR 1**：`atelier/worker-team/pm/v1.md` 定稿＋格式檢查補強（D7）＋`docs/atelier/harness.md` 更新；`uv run pytest -q tests/unit/test_atelier_harness.py` 綠、`openspec validate atelier-pm-harness-v1 --strict`。**只動 `atelier/` 樹、`tests/`、`docs/`（不動 `src/`）。**
5. **PR 2**：`atelier render`（介面清單 1）＋測試（查無 id → 1、`--append` 檔不存在 → 1、寫不進去 → 2、原子寫入、不碰 `--out` 以外的檔案）。
6. **PR 3**：`install` 的守門（`v1` 的 pm harness 預設 exit 1、`--global` 才裝並警告）＋`Launch.env`＋`agora continue --harness-file`＋測試（**會動 `src/atelier/install.py`、`src/agora/cli.py`、`src/agora/agents/*.py`**）。

### 階段 C：上線與收尾

7. **交付機制（跨 repo）**：worker 的 sidecar `open`／headless continue 注入（介面清單 3）＋Mac 的開 pane 指令（6）；PMO 排。開機單位在這個階段轉成「只驗證」（階段 A 的 2）。
8. **過渡清理**：使用者層級的 v0 殘留（介面清單 5）——**在交付可用之後**才做，否則 PM 也失去 harness。
9. **歸檔**：openspec archive（把 delta 併進主 spec）。
10. **行為驗收**：**在殘留清掉之後**，同一台機器同時開 PM 與隊員 → 只有 PM 的請求有 harness、`~/.claude/rules/` 與 `~/.config/opencode/AGENTS.md` 逐字不變；PM 起來後的行為照 harness（收指示、派工、非作者審查、回報格式）。
11. **Rollback**：交付方不帶參數（回到「沒有 harness」）或指回 `@v0`；harness 樹不動。

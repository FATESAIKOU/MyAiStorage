# AiStorage

個人 AI 基礎建設裡「有狀態的資料」那一層。收著 AiEntry 與 AiContainer 裡的 AI 需要記住的一切，讓 AI 的工作能跨 AI、跨裝置、跨時間被接續或參考。

## Language

`_Avoid_` 列的是文件與對話裡不要用的詞。程式、目錄、檔名需要英文識別名時，一律用各詞條「程式識別名」那一行的寫法。

### 儲存要素

**MyBrain**:
使用者的第二大腦；存進去的東西只用於決策參考（我是誰、我在哪裡、我要去哪裡）。
_Avoid_: memory、知識庫

**Atelier**:
住民的 harness 資料（prompt、skill、tool），依 know / do / judge / dont 分類，以某個 AiContainer profile 為前提。目前的服務對象是員工；秘書的 harness 仍內建在 App 裡，是否移入 Atelier 之後再檢討。
_Avoid_: harness 模組、設定檔

**Agora**:
所有來源應用的 Session 的集中存放處，讓之後啟動的 AI 能從它們出發。由儲存實體與一個可擴充的搜尋介面組成。
_Avoid_: Session Pool、聊天紀錄

**Foundry**:
所有產出的集中管理處。管理集中、儲存分散：每件產出都登錄在產出目錄裡，但只有收容產出的真本放在 Foundry。
_Avoid_: Artifact store、成果物庫

### Foundry

**產出**:
AI 做出來、要能被人與其他 AI 找回來的東西：文件、程式碼、圖片、影片、簡報。
_Avoid_: artifact、成果物

**產出目錄**:
Foundry 裡每件產出一筆的登錄，記著出處、型態、產出它的 Session 與所屬案件。已知的產出位置會定期自動收錄，死連結會被檢查出來。
_Avoid_: index、registry

**原處產出**:
真本留在自己專案 repo 的產出（程式碼、專案文件、跟著 PR 走的報告），Foundry 只登錄它的出處。

**收容產出**:
沒有自己的家、真本放在 Foundry 儲存實體裡的產出，例如簡報、圖片、影片、一次性報告。

### 共通

**住民**:
住在某個 AI 應用或 AiContainer 裡、使用 AiStorage 的 AI。手機 App 的住民是秘書，AiContainer 的住民是員工；期 1 在 Mac 容器裡執行的 opencode 也是住民。
_Avoid_: agent、bot

**共通約定**:
四個儲存要素之間共用的規則：項目的形狀與共通 metadata、怎麼互相參照、用什麼身分存取。每個要素仍各有自己的儲存實體與介面，AiStorage 沒有統一的存取入口。
_Avoid_: AiStorage API、統一介面

**profile**:
一類執行體的配置，決定它有哪些能力、能拿到哪些授權；比喻上是部門。AiContainer 的 worker 各屬於一個 profile，手機 App 與同步程式也各算一種 profile。期 1 唯一實際使用的是 Mac opencode（Mac 容器裡的 opencode 與它的同步器），程式識別名 `mac-opencode`。
_Avoid_: 機器設定、環境

**身分**:
一個執行體屬於哪個 profile，由它持有的該 profile 憑證證明；憑證只有一個發放來源（目標是 MyLinuxPool，期 1 由你本人手動安裝）。存取儲存實體的憑證可以由多個 profile 共用（期 1 的 worker 共用 Drive 憑證與讀取身分），這時由每個 profile 各自的簽章金鑰證明 profile。AI 自己載入的職務或自己的宣稱不構成身分。
_Avoid_: 帳號、user

**授權**:
儲存要素依身分決定允許哪些操作；規則由各要素自己持有。執行體只負責證明自己是誰，不知道自己被允許做什麼。
_Avoid_: permission、權限設定

**收件匣**:
每個 profile 專屬的投遞處，由 worker 共用的 client 建立，以資料夾 id 辨識。寫入者把原始紀錄、metadata、交接單、認領、改寫提案、產出登錄（之後還有職務修改提案）放進自己的收件匣，改不了也刪不了任何要素的真本。收件匣的位置**不證明**產生者（別的 client 也放得進來），產生者靠簽章。
_Avoid_: inbox、queue、暫存區

**提交流程**:
唯一能把收件匣內容寫進 Agora、Foundry（之後還有 Atelier）真本的角色。它負責確認真本沒被動過（核對釘選值、清掃注入物）、驗章、蓋產生者章、轉出閱讀版、處理交接單與認領、更新讀取視圖；同一時間只有一個在跑，管理操作也要跟它錯開。
_Avoid_: ingest job、後端
程式識別名：`committer`

**釘選值**:
提交流程記在住民碰不到的地方（獨立的 pin repo `MyAiStorage-pin`，只有提交流程的 deploy key 寫得進去）的真本指紋：全部 ref、manifest 的內容雜湊、bundle 清單、annex key 集合。每一輪都以它判斷 Drive 上的真本有沒有被動過；它是可信內容的唯一來源（ADR 0008）。
_Avoid_: pin、checkpoint
程式識別名：`pin`

**簽章金鑰**:
每個 profile 各自一把、用來對放進收件匣的項目簽章的金鑰。提交流程驗章通過才收，產生者章依它而蓋；profile 的撤銷也靠它。
_Avoid_: signing key（程式識別名可用 `signing_key`）

**讀取視圖**:
提交流程每次提交後，以一般檔案發佈的唯讀內容：metadata、閱讀版、Session Link 與交接單、快照時間、產出目錄、搜尋索引。讀取介面背後讀的就是它，屬於衍生物，可以重建。
_Avoid_: read model、快取、鏡像

**讀取介面**:
一個儲存要素對讀者提供的唯一讀取手段。讀的時候可以指定新鮮度；形狀不隨寫入機制或儲存後端改變。每個要素各有自己的讀取介面，沒有統一的入口。
_Avoid_: API、read endpoint、查詢服務

**同步器**:
來源應用這一側，把 Session 的原始紀錄與 metadata 放進自己 profile 收件匣的程式；每個來源應用一個。格式轉換不在這裡，在提交流程的轉換器。
_Avoid_: 上傳器、uploader、sync client
程式識別名：`syncer`（目錄 `syncers/`）

**同步並提交**:
寫入者主動做的一次同步（可以連同交接單、認領等項目一起放進收件匣），接著觸發提交流程，並等到讀取介面看得到這次放進去的每一個項目、或它的拒絕原因為止。接續、認領時一定要做；想讓並行的 Session 讀到進度時也可以做。
_Avoid_: flush、push、publish

**寫入機制**:
寫入端用來維持新鮮度的做法，例如同步的頻率、提交流程的觸發方式。依成本選擇、逐步追加，改變寫入機制不改變讀取介面。
_Avoid_: sync strategy、更新策略

**新鮮度**:
讀到的內容最多落後來源應用多久，以快照時間衡量。讀者在讀取時指定要求；達不到時照樣拿到內容，附上警告。一個 Session 能維持多新，由寫入端的機制（同步與提交的頻率）決定，讀取不會觸發寫入。
_Avoid_: freshness、即時性、延遲

**快照時間**:
同步器擷取某份原始紀錄的時間。每筆讀取結果都附上它；停止中的 Session，只要最新快照在它停止之後，就永遠算最新。
_Avoid_: timestamp、更新時間

**項目**:
儲存要素裡的一個單位，例如 MyBrain 的主題檔、Agora 的 Session、Atelier 的職務、Foundry 的產出。每個項目都由 metadata 與本體組成。
_Avoid_: 檔案、record、entry

**metadata**:
描述項目的欄位。共通最小欄位有六個：id（不變的身分）、型態、產生者、時間、所屬案件、出處；各要素可以再加自己的欄位。

**本體**:
項目的內容本身。如果項目只是指向別處的連結，本體裡放的就是那個對外連結。
_Avoid_: payload、內文

### Atelier

**職務**:
Atelier 的組織單位：一組 know / do / judge / dont，加上它的能力需求。秘書交接時指定職務，員工啟動時載入它；多個職務可以共用同一個 profile。比喻上 profile 是部門、職務是部門裡的工作說明。職務不參與授權。
_Avoid_: 部門、agent 定義
程式識別名：`role`（目錄 `roles/`）

**能力**:
profile 安裝好、並列在能力清單上的東西，例如某個 MCP server 或瀏覽器。
_Avoid_: capability、工具

**能力需求**:
職務宣告它需要、也只啟用的那些能力。職務只能派到能力齊全的 profile 上；這就是「職務以 profile 為前提」的具體形式。

**know**:
Atelier 裡給 AI 看的背景與規則：角色、流程、格式。回答「他能看到什麼」。

**do**:
Atelier 裡的能力：skill 與 tool 的定義。回答「他能做到什麼」。

**judge**:
Atelier 裡的驗收標準：審查觀點與驗證腳本。回答「他怎麼知道自己做對了」。

**dont**:
Atelier 裡的行為約定，回答「他不能做什麼」。它不是安全邊界；硬邊界放在 profile、憑證與網路規則。
_Avoid_: guardrail、權限

「他記得什麼」不屬於 Atelier，由 Agora 與 MyBrain 負責。

**職務版本**:
職務的每一次變更都是一個新版本。員工可以改職務，但改動要過 judge 的驗證才生效，舊版本都留著可以退回。每個 Session 記下它載入的是哪個職務的哪個版本。

**外部副本**:
不是你寫、或屬於別的系統的 skill，複製進 Atelier 的那一份。它記著出處與版本，定期跟上游比對內容：有落差就提出更新，上游消失就提出汰換。員工只依賴 Atelier，不在執行時去外部取。
_Avoid_: fork
程式識別名：`vendor`（目錄 `vendor/`）

### Session

**Session**:
一次人與 AI 應用、或 AI 與 AI 之間的對話，由它的來源應用從頭到尾記錄下來。
_Avoid_: chat、thread、對話紀錄

**來源應用**（Origin App）:
實際執行某個 Session 的 AI 應用，例如手機 App、Claude Code、opencode、agy。Session 的原始紀錄在這裡產生，再定期同步進 Agora。
_Avoid_: client、source

**原始紀錄**:
Session 以來源應用自己的匯出單位存下的內容，是這個 Session 在 Agora 的真本。只能透過改寫或抹除變更。
_Avoid_: raw log、dump

**改寫**:
（期 1 不提供，列在待辦）經由 Agora 提出的、對原始紀錄內容的修改。一律留下舊版本與變更紀錄、可以回滾；不能改變內容的位置。來源應用自己的編輯（例如 /undo 後重新輸入）不是改寫，而是原始紀錄的新版本。
_Avoid_: 編輯、覆寫

**抹除**:
真正刪掉原始紀錄的某一段或整個 Session，不留舊內容，只留下誰、何時、為什麼、抹了哪一段的紀錄。用於憑證外洩、私事這類不該留存的內容。只有你本人能執行；AI 只能改寫遮蔽並提醒你。
_Avoid_: 刪除、遮蔽

**閱讀版**:
由原始紀錄導出、所有來源應用共用同一種格式的 Session 內容，供其他 AI 接續與參考時讀取；壞了或格式變了都能從原始紀錄重建。
_Avoid_: transcript、摘要

**運作中**:
Session 的狀態之一：來源應用仍可能往它追加內容。

**停止中**:
Session 的狀態之一：來源應用目前不會再往它追加內容。由來源應用的可信訊號判定；沒有可信訊號的來源應用（例如 opencode），只能由你或 AI 明確宣告（opencode 用它自己的封存，`time.archived`），不從閒置時間推測。停止中的 Session 又被追加內容時，回到運作中，同步器會立刻同步並提交一次。

**Session Link**:
從一個 Session 指向另一個 Session 的有向關係，類型是接續或參考。接續的目標一定是較早的 Session；參考的目標可以是任何其他 Session，包括仍在並行的 Session。一個 Session 可以有多條 Session Link。

**接續**（Continuation）:
一種 Session Link：新 Session 從較早 Session 的某個接續點開始承接它的工作。由較早 Session 的持有者發起（同步並寫交接單，一起提交），新 Session 認領那張交接單時才形成這條 Link。較早的 Session 不受影響，可以照常繼續。
_Avoid_: handoff、resume、移交

**參考**（Reference）:
一種 Session Link：一個 Session 自己去讀另一個 Session 在 Agora 裡的紀錄（或它的摘要）取得資訊，並記下讀到的快照時間；不喚醒任何 AI，也不承接它的工作。並行的 Session 可以互相參考。同一對 Session 之間的參考 Link 只保留一條，記最新讀到的快照時間。
_Avoid_: 引用、lookup、詢問

**接續點**:
較早 Session 被接續時所到的位置：持有端寫交接單時已經提交、或與交接單同一批放進收件匣的那份原始紀錄快照，以及快照裡最後一則已完成的訊息（快照識別＋message id），記在交接單上。新 Session 只承接這份快照裡、這個位置之前的內容；來源端之後的編輯不影響它。

**同步**:
來源應用經由同步器把 Session 的最新內容送進 Agora。平時定期進行；接續由持有該 Session 的一方發起，同步並連同交接單一起提交。參考讀的是 Agora 當下最新已提交的版本，可以比來源應用落後，落後多少看快照時間。

**交接單**:
被接續 Session 的持有者（發起接續的 AI）寫給接手者的說明。它本身是 Agora 的一個項目（有 id），記著被接續的 Session、接續點與交接內容。還沒有人認領時可以經由讀取介面找到；接手者先讀交接單，需要時才深入閱讀版。
_Avoid_: ticket、handoff note

**認領**:
新 Session 宣告由自己接手某張交接單；提交流程收到認領時，才建立從新 Session 指向被接續 Session 的接續 Link。一張交接單只能被認領一次，所以認領之後要同步並提交，等讀取介面確認這條 Link 屬於自己才開工。
_Avoid_: claim、accept、領取

**摘要**:
由 AI 為單一 Session 產生、供參考時先讀的精簡內容，屬於可重建的衍生物。
_Avoid_: summary、交接單

**分岔**:
同一個 Session 在接續點之後出現兩條以上各自前進的後續：它自己繼續，或被多個 Session 接續。

**收斂**:
開一個 Session 同時接續多個分岔的末端，把它們合回一條。

**分裂**（1→n）:
刻意造出分岔，把一件工作切給多個 Session 分頭做：持有者同步，為每一份工作各寫一張交接單，一起提交，由多個新 Session 各自認領。對應的工作場景是「切分工作」。
_Avoid_: split、fan-out、派工

**統合**（n→1）:
刻意造出收斂，把分頭做完的結果合回來：每個分岔的持有者各自同步、寫一張交接單交出自己的末端並提交，由同一個新 Session 認領全部。對應的工作場景是「聚合成果」。
_Avoid_: merge、fan-in、合併

**相互參照**（n↔m）:
並行的 Session 彼此建立參考 Link、讀對方最新已提交的內容，互相支持推進。不承接對方的工作，也不讓對方被鎖住。
_Avoid_: 互相引用、sync、協作

### 案件

**案件**:
一件持續進行中的工作。案件只在 MyBrain 裡定義與維護，就是一個主題檔（Project Log、行動計劃這類），以它 metadata 裡不變的 id 辨識；Agora 與 Foundry 不另存案件清單。
_Avoid_: project、thread、工作線

**所屬案件**:
項目 metadata 裡指向某個案件 id 的欄位；不確定時可以空著。

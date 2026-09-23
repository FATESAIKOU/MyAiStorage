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
住在某個 AI 應用或 AiContainer 裡、使用 AiStorage 的 AI。手機 App 的住民是秘書，AiContainer 的住民是員工。
_Avoid_: agent、bot

**共通約定**:
四個儲存要素之間共用的規則：項目的形狀與共通 metadata、怎麼互相參照、用什麼身分存取。每個要素仍各有自己的儲存實體與介面，AiStorage 沒有統一的存取入口。
_Avoid_: AiStorage API、統一介面

**profile**:
一類執行體的配置，決定它有哪些能力、能拿到哪些授權；比喻上是部門。AiContainer 的 worker 各屬於一個 profile，手機 App 與同步程式也各算一種 profile。
_Avoid_: 機器設定、環境

**身分**:
一個執行體屬於哪個 profile，由 MyLinuxPool 證明。AI 自己載入的職務不構成身分。
_Avoid_: 帳號、user

**授權**:
儲存要素依身分決定允許哪些操作；規則由各要素自己持有。執行體只負責證明自己是誰，不知道自己被允許做什麼。
_Avoid_: permission、權限設定

**收件匣**:
每個 profile 專屬的投遞處。寫入者只能把原始紀錄、metadata、改寫提案、職務修改提案放進自己的收件匣，碰不到任何要素的真本。
_Avoid_: inbox、queue、暫存區

**提交流程**:
唯一能把收件匣內容寫進 Agora、Foundry、Atelier 真本的角色。它負責驗證、蓋產生者章、轉出閱讀版、跑 judge、更新讀取視圖；同一時間只有一個在跑。
_Avoid_: ingest job、後端
程式識別名：`committer`

**讀取視圖**:
提交流程每次提交後，以一般檔案發佈的唯讀內容：metadata、閱讀版、產出目錄、搜尋索引。給不能跑 git 的讀者讀，屬於衍生物，可以重建。
_Avoid_: read model、快取、鏡像

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
對原始紀錄內容的修改，你與 AI 都可以做。一律留下舊版本與變更紀錄、可以回滾；不能改變內容的位置，所以接續點永遠有效。
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
Session 的狀態之一：來源應用目前不會再往它追加內容。

**Session Link**:
從一個新 Session 指向一個較早 Session 的有向關係。一個 Session 可以有多條 Session Link，指向多個較早的 Session。

**接續**（Continuation）:
一種 Session Link：新 Session 從較早 Session 的某個接續點開始承接它的工作。較早的 Session 不受影響，可以照常繼續。
_Avoid_: handoff、resume、移交

**參考**（Reference）:
一種 Session Link：新 Session 自己去讀較早 Session 在 Agora 裡的紀錄（或它的摘要）取得資訊；不喚醒任何 AI，也不承接它的工作。
_Avoid_: 引用、lookup、詢問

**接續點**:
較早 Session 被接續時所到的位置：持有端發起接續前最後一次同步進 Agora 的位置。新 Session 只承接這個位置之前的內容。

**同步**:
來源應用把 Session 的最新內容送進 Agora。平時定期進行；接續由持有該 Session 的一方發起，發起前先同步一次。參考讀的是 Agora 當下最新的版本，可以比來源應用落後。

**交接單**:
發起接續的 AI 寫給接手者的說明，跟著那條接續的 Session Link 一起存進 Agora。接手者先讀交接單，需要時才深入閱讀版。
_Avoid_: ticket、handoff note

**摘要**:
由 AI 為單一 Session 產生、供參考時先讀的精簡內容，屬於可重建的衍生物。
_Avoid_: summary、交接單

**分岔**:
同一個 Session 在接續點之後出現兩條以上各自前進的後續：它自己繼續，或被多個 Session 接續。

**收斂**:
開一個 Session 同時接續多個分岔的末端，把它們合回一條。

### 案件

**案件**:
一件持續進行中的工作。案件只在 MyBrain 裡定義與維護，就是一個主題檔（Project Log、行動計劃這類），以它 metadata 裡不變的 id 辨識；Agora 與 Foundry 不另存案件清單。
_Avoid_: project、thread、工作線

**所屬案件**:
項目 metadata 裡指向某個案件 id 的欄位；不確定時可以空著。

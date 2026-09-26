# 住民寫得進 repo 資料夾：以偵測、隔離、釘選保證真本，不再靠 Drive 權限禁止

REQ 的原則是「禁止的能力要做成根本不存在」。技術驗證 1.4 證實，在我們的限制下這一條對 Drive 的「建檔」做不到：`drive.file` 的 client（不論在哪個 GCP project）只要知道資料夾 id，就能在任何資料夾裡建檔，包括 Agora、Foundry 的 repo 資料夾與讀取視圖資料夾；而資料夾與檔案 id 對住民是公開資訊（讀取身分本來就列得到）。攻擊方能建同名的 GITMANIFEST、GITBUNDLE，讓 clone 失敗，甚至讓全新 clone 拿到偽造的歷史。唯一能從根源解決的做法是每個 profile 各用一個 Google 帳號（它的 token 不是擁有者，建不進沒被分享的資料夾），使用者判定帳號管理不可行。

所以我們接受「寫得進去」這個能力依然存在，改由提交流程保證真本的完整性：每一輪以存在 MyAiStorage repo（住民寫不到）的**釘選值**為唯一的信任來源（全部 ref 的 hash、manifest 的內容雜湊、active 與已移除的 bundle 清單、annex key 集合），以 Drive 的 `sha256Checksum` 做**只看 metadata 的清掃**，把不符的檔移到隔離資料夾，再 clone 並核對釘選值；push 前寫待定、驗證後轉正（兩階段）。技術驗證 1.4f2〜1.4f5 用 spike 的腳本，在實際的注入形狀（換掉 main、保留真 main 只偽造 `git-annex` 分支、多餘 ref、同名 DoS、注入窗口、多層同名資料夾）下實測：偽造的歷史一律被擋下，中斷後可以自動恢復，清掃在 1.8 等級的 repo 上約 3 秒。

使用者在 2026-09-26 明示接受四項殘餘風險：(1) 住民能持續注入、取消 run 或停用 workflow，讓提交暫停（偵測得到，不會遺失或竄改內容）；(2)「寫進 repo 資料夾」的能力依然存在，是 REQ 原則的有意識例外；(3) 注入可以消耗家庭共用的 5TB 配額；(4) 共用同一個 client 的 worker 能刪除彼此的收件匣項目（同步器以 Agora 比對補傳）。

## Considered Options

- 每個 profile 各自一個 Google 帳號：從根源解決，但帳號與 OAuth 同意的管理成本太高，使用者不採用。
- 每個 profile 各自一個 GCP project：擋不住跨 project 的建檔，對注入沒有幫助。
- 自製以 id 定位的 git-remote-annex helper：等於重寫 ADR 0005 的核心，接近重新選型。
- git 歷史放 GitHub、只把 annex 物件放 Drive：物件由可信的 git 驗證，偽造會被拒；但違反「儲存只放 Drive」的限制（ADR 0005 已經否決過）。
- 共用雲端硬碟設權限：消費者版 Google One 不能建共用雲端硬碟。
- 住民完全不碰 Drive、改用其他寫入通道（Actions artifact、repository_dispatch、workflow_dispatch 的輸入）：外部上不去、會打破權限邊界、或容量太小且誰都讀得到。

## Consequences

- 提交流程的每一輪都有固定順序（design D2），任何一步讀不到或不符就中止、不做移動；中止本身就是對使用者可見的訊號，6.3 監控連續中止的輪數、隔離資料夾的增長、workflow 是否被停用、距離上一次成功提交的時間與家庭共用的整體配額。
- 釘選值只能由提交流程的 job 依自己觀測到的遠端狀態寫入，不得有可觸發、可輸入的寫入路徑；釘選值放在獨立的 pin repo（不放 workflow），提交流程以只對它有寫入權的 deploy key 寫入，維持單一 job。抹除之後由管理者在 Mac 上重建釘選值。
- 產生者章改靠每個 profile 的簽章金鑰（ADR 0006 修訂），MyLinuxPool 的工單要加上簽章金鑰的發放與輪替。
- 所有讀者與工具一律以 id 定位，讀取視圖資料夾也每一輪清掃。
- 以上結果的前提是 3.2 修正 spike 查核找到的實作缺陷（暫時性錯誤造成誤隔離、上一版 manifest 造成清不掉的 DoS、annex 物件被漂白等），並通過 9.4 的反向測試。
- 如果之後改成每個 profile 各自一個 Google 帳號，這份 ADR 就可以撤回，偵測機制保留為縱深防禦。

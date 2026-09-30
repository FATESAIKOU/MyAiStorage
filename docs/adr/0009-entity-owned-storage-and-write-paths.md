# 每個實體自己的儲存與寫入路徑；Foundry 回到 Google Drive 共享資料夾＋GitHub，Atelier 放 GitHub

> 2026-09-28 本人確認。取代 ADR 0005 裡關於 Foundry 的部分、ADR 0006 的適用範圍，以及 design D1、D7。

期 1 做到一半，四個實體在圖上變成了一條共用的寫入管線：Agora 與 Foundry 共用同一個收件匣、同一個提交流程、同一個釘選值 repo。這偏離了 ADR 0001「四個要素各自獨立，AiStorage 只定共通約定」。實際的代價也出現了：Foundry 的錯誤會讓 Agora 的收件匣清不掉，多 repo 的分派本身又帶出好幾個高度缺陷。所以回到 ADR 0001：每個實體自己決定儲存實體、寫入方式與頻率，彼此只靠 metadata 裡的 id 互相參照。

| 實體 | 儲存實體 | 寫入 | 讀取 | 寫入閘門 |
|---|---|---|---|---|
| MyBrain | GitHub private repo（不變） | AI 開 PR，本人 merge | clone | PR review 就是閘門 |
| Agora | Drive 上的 git-annex repo（不變） | 同步器 → Agora 的收件匣 → Agora 的提交流程 | 讀取視圖＋讀取介面 | 有，而且**只屬於 Agora** |
| Foundry | Google Drive 的共享資料夾（文件類）＋GitHub repo（程式碼，以 repo 為單位） | 文件：AI 直接放進共享資料夾，可以修改。程式碼：照一般方式開 PR | Drive 與 GitHub 各自的介面 | 無 |
| Atelier | GitHub private repo | 開 PR | clone | PR review |

Foundry 指的就是「那個共享資料夾＋那些 repo」，不是管理它們的模組。產出的出處（哪個 Session、哪個 profile、所屬案件）記在產出本身的 metadata：Drive 檔案用 `appProperties`，GitHub 用 commit 或 PR 裡的 Session id。不另外維護一份中央目錄。

## Considered Options

- **維持共用的寫入閘門**：少一個 workflow 要顧，但兩個實體互相拖累，而且閘門要理解每個實體的型態，實體之間就不獨立了。
- **Foundry 維持 Drive 上的 git-annex**：登錄過的產出改不了、有版本，但對「公司共享資料夾」這個用途太重，程式碼也不該離開自己的 repo（ADR 0002 已經這樣判斷）。
- **Foundry 的文件也禁止修改**：保證最強，但用起來不順。不想被改的東西改放 GitHub。

## Consequences

- 住民對 Foundry 的共享資料夾有寫入與修改權限，所以 Foundry 的文件**沒有**「AI 改不了」的保證，只有 Drive 自己的版本歷史。需要保證的產出放 GitHub。
- **隔離靠帳號，不靠 root 資料夾。** rclone 的 `root_folder_id` 只決定工具從哪裡開始看，Google 不會據此限制憑證；能改別人文件的憑證若屬於 Agora 的帳號，就刪得到 Agora 的真本。所以 Foundry 資料夾留在 AiStorage 的帳號，以「可編輯」分享給另一個**住民專用帳號**；住民只拿那個帳號的權限，Google 會強制它只看得到被分享的 Foundry 資料夾。住民專用帳號可以加入家庭方案取得空間。
- 期 1 已經做好的 git-annex 版 Foundry（Foundry repo、多 repo 分派、Foundry 讀取視圖與讀取介面、`admin create-repo`）全部移除；提交流程回到只處理 Agora。
- Atelier 期 1 仍然只做需求設計，儲存方向改記為 GitHub。

## 已定（2026-09-28，本人）

- 寫入閘門拆成每個實體各一套；實際上只有 Agora 需要。
- Foundry 的文件允許 AI 修改（先求簡單）；不想被改的放 GitHub。程式碼：Foundry 只記 repo 的出處，AI 照一般方式開 PR。
- 共享資料夾以「分享給住民專用帳號」隔離。
- Foundry 的讀取介面期 1 最小：依出處 metadata 查 Drive 檔案、依 Session id 查 GitHub，不做統一搜尋。
- 分裂、統合、相互參照是 **Agora** 的功能，不需要 Foundry（見 ADR 0010）。期 1 的驗收不再要求產出登錄進 Foundry。

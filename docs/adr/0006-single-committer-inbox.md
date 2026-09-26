# 所有寫入先進收件匣，由單一提交者收進真本

> 2026-09-26 re-scope：決定本身不變。文中的分期（期 1／期 2／期 3）、角色（員工、秘書）與範圍（大檔、Atelier）說法，以 `openspec/changes/establish-aistorage-phase1/proposal.md` 與 `docs/backlog.md` 為準；profile 名稱以 `CONTEXT.md` 為準（期 1 是 Mac opencode）。「接續前要等 1〜2 分鐘」改為至少一次提交流程的耗時（design D10）。

> 2026-09-27 技術驗證後修訂：「住民手上沒有任何能改寫真本的憑證」仍然成立（讀、改、刪別人的檔都被擋），但技術驗證 1.4 證實 `drive.file` 的 client 只要知道資料夾 id，就能在別人的資料夾（包括其他收件匣與 repo 資料夾）**建檔**。所以：(1) 收件匣的位置不再能證明產生者，**產生者章改靠每個 profile 的簽章金鑰**，提交流程驗章，沒有簽章或驗章失敗一律拒收；(2)「刪不掉歷史」仍靠憑證，但「偽造的歷史不會被當成真本」改靠提交流程的偵測＋隔離＋釘選（ADR 0008）；(3) 住民的 token 除了觸發任何 workflow，也能用任何既有分支觸發、rerun 舊的 run、停用或啟用 workflow、讀 log（1.6），所以每個 workflow 都要檢查 `github.sha` 與 `github.ref`，main 以外不留帶 workflow 的分支。詳見 design D2、D3。

Agora 與 Foundry 的 git repo 放在 Google Drive 上，而 Drive 沒有「同時寫入時只讓一個成功」的機制，兩次同時 push 會有一次悄悄消失。再加上手機跑不了 git，本來就需要有人代它 commit。所以所有寫入者（手機 App、worker、Mac 同步程式）都只能把內容放進 Drive 上自己的收件匣（`drive.file` scope，只碰得到自己建的檔案），再由唯一的提交流程（在 GitHub Actions 上按需或定時執行，同一時間只跑一個）驗證、蓋產生者章、轉出閱讀版，然後 push。Atelier 的修改提案也走同一條路：judge 通過才推進 Atelier repo。住民手上因此沒有任何能改寫真本的憑證：「刪不掉歷史」和「產生者章可信」是靠憑證本身做到的，不靠 AI 守規矩。

## Considered Options

- 每個寫入者各自一個 repo：寫入即時，但手機仍需要有人代為 commit，AI 也能毀掉自己 repo 的歷史。
- 大家直接 push 同一個 repo，再加鎖檔：Drive 上的鎖檔本身就不可靠，同時 push 時會悄悄丟資料。
- 在 Gateway VPS 跑提交程式：Gateway 設計成可以隨時丟棄，也違反「VPS 不是拿來開服務」的立場。

## Consequences

- 寫入是非同步的：接續前要等提交流程跑完，預估 1〜2 分鐘。
- 會用到 GitHub Actions 的分鐘數（免費方案每月 2,000 分鐘，跟 MyLinuxPool 共用）。不夠時改用自架 runner。
- 員工的 GitHub token 帶有 MyAiStorage repo 的 Actions 寫入權（只用來觸發提交流程），所以這個 repo 的每個 workflow 都必須設計成被任意觸發也安全。

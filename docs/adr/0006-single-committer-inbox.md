# 所有寫入先進收件匣，由單一提交者收進真本

Agora 與 Foundry 的 git repo 放在 Google Drive 上，而 Drive 沒有「同時寫入時只讓一個成功」的機制，兩次同時 push 會有一次悄悄消失。再加上手機跑不了 git，本來就需要有人代它 commit。所以所有寫入者（手機 App、worker、Mac 同步程式）都只能把內容放進 Drive 上自己的收件匣（`drive.file` scope，只碰得到自己建的檔案），再由唯一的提交流程（在 GitHub Actions 上按需或定時執行，同一時間只跑一個）驗證、蓋產生者章、轉出閱讀版，然後 push。Atelier 的修改提案也走同一條路：judge 通過才推進 Atelier repo。住民手上因此沒有任何能改寫真本的憑證：「刪不掉歷史」和「產生者章可信」是靠憑證本身做到的，不靠 AI 守規矩。

## Considered Options

- 每個寫入者各自一個 repo：寫入即時，但手機仍需要有人代為 commit，AI 也能毀掉自己 repo 的歷史。
- 大家直接 push 同一個 repo，再加鎖檔：Drive 上的鎖檔本身就不可靠，同時 push 時會悄悄丟資料。
- 在 Gateway VPS 跑提交程式：Gateway 設計成可以隨時丟棄，也違反「VPS 不是拿來開服務」的立場。

## Consequences

- 寫入是非同步的：接續前要等提交流程跑完，預估 1〜2 分鐘。
- 會用到 GitHub Actions 的分鐘數（免費方案每月 2,000 分鐘，跟 MyLinuxPool 共用）。不夠時改用自架 runner。
- 員工的 GitHub token 帶有 MyAiStorage repo 的 Actions 寫入權（只用來觸發提交流程），所以這個 repo 的每個 workflow 都必須設計成被任意觸發也安全。

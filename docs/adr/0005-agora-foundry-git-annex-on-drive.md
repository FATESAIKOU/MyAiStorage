# Agora 與 Foundry 以 git 為介面、Google Drive 為唯一儲存實體

使用者要求 Agora 與 Foundry 用 git 形式操作（看重版本與回滾、熟悉的指令、跟 MyBrain / Atelier 一致），而儲存實體只放已經付費的 Google Drive，不增加月費，也不保留第二份副本。現存「以 Drive 當 git remote」的做法裡，唯一還在維護、能把整個 repo（歷史與大檔）存進 Drive 的，是 git-annex 內建的 `git-remote-annex` 加上 rclone special remote。所以採用它，並把「在使用者的 Drive 上實測 clone、revert、抹除、大檔」列為期 1 的第一個任務。

## Considered Options

- AWS S3：用 IAM 就能直接做到「刪不掉舊版本」，但不是 git 形式，而且每月多一筆費用。
- git 放 GitHub、大檔放 Drive 的混合做法：GitHub 的 ref 更新是原子的，可以多人同時寫；但使用者要求儲存實體只放 Drive。
- git-remote-gcrypt、DataLad 的 git-remote-rclone、git-remote-gdrive 類專案：前兩者會強制覆蓋或已經停止維護，最後一類只是玩具等級。
- DVC：預設的 Google app 被封鎖，依賴的函式庫也停滯。

## Consequences

- Drive 沒有條件寫入，同時 push 會悄悄互相覆蓋。所以寫入必須序列化，見 ADR 0006。
- Drive 的垃圾桶與舊版本只保留 30 天，持有完整權限的憑證可以永久刪除。使用者明示接受這個風險；之後要加第二份副本時，git-annex 的 numcopies 就是擴張點。

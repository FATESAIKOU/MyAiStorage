# syncers

寫入端的同步器（程式識別名 `syncer`）：只負責把原始紀錄與 metadata sidecar（含快照時間）放進自己 profile 的收件匣，需要時同步並提交（design D4、D9）。期 1 只有 opencode（在 Mac 的容器裡執行）。手機 App 的同步器之後在 MyAiEntry repo 實作，不在這裡。

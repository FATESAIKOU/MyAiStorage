## ADDED Requirements

### Requirement: 接續的定期寫回只上傳新增的 raw 段

`agora continue` 的定期寫回與最後一次寫回，在目標是 claude Session、而且上一個版本是分段儲存（`agora.raw.layout: chunks/1`）時，MUST 只上傳這一輪新增的 raw 位元組（新的封存段與 tail，按 `AGORA_RAW_CHUNK` 切成多段），MUST NOT 重傳已封存的段、MUST NOT 上傳整份 raw。使用者看到的結果不變：每一輪寫回之後，`session.md` 仍是包含目前整份對話的版本，讀者接回來仍得到整份 raw。

- 這條只改**上傳的位元組**，不改「什麼時候寫回」「寫回哪一個 Session」「讀者看到什麼」的規則（本 spec 的其他 Requirement 不變）。
- 封存前綴被改寫、來源短到封存點之前、沒有 cursor 時，那一輪 MUST 退回整份（新的世代、整份按 C 切成多段、`session.md` 最後上傳）；前一版在新版確認前 MUST 保持完整。只砍掉封存點之後的內容不算改寫（新的 tail 較短，仍可 append）。基準段不在 Drive 上不是退回整份的理由：上傳的一方 MUST 從本機的整份 raw 補回缺的段；補不回才不上傳。
- **每次新開一個 `continue` 的第一次寫回是整份**：`start_native` 會把每一行換成新的 agent session id（所以封存前綴必然不同），來源 jsonl 也是新檔案；那一輪 MUST 走整份，之後同一個 continue 的定期寫回才差分。這是固定成本，不是失敗。
- 目標是**不是 `chunks/1`** 的 Session（舊格式、未知格式）時，MUST 在啟動 agent 之前就拒絕，訊息說明要用 `agora migrate adopt`；MUST NOT 自動把它升級、MUST NOT 開 agent 之後才失敗。
- 差分上傳失敗（離線、段沒傳完、Drive md5 不符）MUST NOT 中斷 agent；MUST 照「定期寫回失敗」的既有規則下一輪再試，entry 留在 outbox。
- 閱讀版 `session.md` 仍整份上傳（這個 change 不做它的差分）；`--sync-period 0`、沒有新內容不寫、最後一次寫回、被刪另存等行為都不變。
- 換機器或清掉本機快取之後的第一次寫回 MAY 是整份（沒有 cursor）；之後才差分。

#### Scenario: 長 claude Session 的下一輪
- **WHEN** **在同一個 `continue` 內**接續一個 814 MB 的 claude Session，5 分鐘後多了 1 MB 的對話
- **THEN** 這一輪只上傳新的段與 tail（≤ 4 MiB＋1 MB）與整份 `session.md`；Drive 上的版本仍包含整份對話；agent 畫面沒有任何 agora 的輸出

#### Scenario: 新開 continue 的第一次寫回
- **WHEN** 使用者結束 agent 後再對同一個 814 MB 的 Session 執行 `agora continue`（新的 agent session）
- **THEN** 第一次寫回是整份（約 195 個段、814 MB）；之後才只送新增的段；這一輪沒有壞版本，失敗也照「定期寫回失敗」處理

#### Scenario: 來源被改寫
- **WHEN** 接續期間 agent 的 jsonl 被壓縮或改寫，前綴與上一版不同
- **THEN** 這一輪走整份（新的世代、整份按 C 切成多段、`session.md` 最後）；前一版在新版確認前仍完整；agent 不受影響

#### Scenario: 差分上傳失敗
- **WHEN** 某一輪的段上傳失敗（離線或 rclone 錯誤）
- **THEN** entry 留在 outbox；agent 繼續跑；下一輪再試；結束時照既有規則說明失敗次數與 log 位置

#### Scenario: 換機器後第一次寫回
- **WHEN** 另一台機器接續同一個分段 Session，本機沒有 cursor
- **THEN** 第一次寫回是整份（一次）；之後的輪次只上傳新增的段；讀者看到的版本仍完整

#### Scenario: 舊格式的 Session 不能接續
- **WHEN** 對一個 header 沒有 `layout` 的 Session 執行 `agora continue`
- **THEN** 在開 agent 之前拒絕（exit 非 0），訊息說明要先用 `agora migrate adopt`；不寫任何東西

#### Scenario: 沒有新內容
- **WHEN** 一個定期寫回的間隔內沒有新的對話
- **THEN** 不寫、不上傳（與現行相同）

## ADDED Requirements

### Requirement: 接續期間定期寫回
`agora continue`（含 `--fork`）啟動 agent 之後，agent 執行期間 MUST 每 5 分鐘把 agent 目前的整份對話寫回接續的那個 Session，並和其他寫入一樣先存在本機、再由背景上傳到 Google Drive。

- 和上一次寫回（或啟動時載入的內容）相比沒有新內容時，MUST NOT 寫。
- agent 結束時 MUST 照原本的規則做最後一次寫回；和最後一次定期寫回相同時不必再寫。
- 定期寫回期間 agora MUST NOT 在終端機輸出任何東西；訊息記到 log 檔。
- 定期寫回失敗（讀不到、上傳失敗等）MUST NOT 中斷 agent，下一輪再試。
- 讀到 agent 寫到一半的內容時，MUST NOT 把 Session 寫壞。
- 接續途中 Session 被刪掉時，MUST 照接續的規則另存成一個新的 Session；之後的寫回（含最後一次與補存）MUST 寫到這個新的 Session，不再另存。

#### Scenario: 執行中看得到進度
- **WHEN** 用 `agora continue` 接續 Session A，在 agent 裡持續對話超過 5 分鐘
- **THEN** 不等 agent 結束，A 在本機與 Drive 上就已經是包含新對話的版本

#### Scenario: 沒有新內容不寫
- **WHEN** agent 開著但 5 分鐘內沒有新的對話
- **THEN** A 沒有被重寫，outbox 沒有新的一筆

#### Scenario: 不弄亂畫面
- **WHEN** 定期寫回發生時（包括讀到寫到一半的最後一行、上傳失敗）
- **THEN** 終端機上沒有任何 agora 的輸出；log 檔有記錄

#### Scenario: 途中被刪
- **WHEN** 接續 A 的途中，A 被別台機器刪掉，之後又過了兩個 5 分鐘、agent 才結束
- **THEN** 只多出一個另存的新 Session B（`parents` 指向 A），之後的寫回都寫到 B；結束時印出另存的說明

#### Scenario: agora 被強制結束
- **WHEN** 定期寫回過一次之後，agora 與 agent 都被強制結束
- **THEN** Drive 上已經有最後一次定期寫回的內容；下一個指令的補存會寫到同一個 Session（途中被刪過的話寫到另存的那個）

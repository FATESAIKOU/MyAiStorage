## ADDED Requirements

### Requirement: 接續期間定期寫回
`agora continue`（含 `--fork`）啟動 agent 之後，agent 執行期間 MUST 每 5 分鐘把 agent 目前的整份對話寫回接續的那個 Session，並和其他寫入一樣先存在本機、再由背景上傳到 Google Drive。

- 和上一次寫回（或啟動時載入的內容）相比沒有新內容時，MUST NOT 寫。
- agent 結束時 MUST 照原本的規則做最後一次寫回。和最後一次定期寫回相同時不必再寫，但 MUST 照樣確認那個 Session 還在；被刪了就照接續的規則另存。
- 定期寫回期間 agora MUST NOT 在終端機輸出任何東西，也 MUST NOT 讀終端機的輸入；訊息記到 log 檔。最後一次寫回之後，有定期寫回失敗過時 MUST 說明失敗次數與 log 的位置。
- 定期寫回失敗（讀不到、上傳失敗等）MUST NOT 中斷 agent，下一輪再試。
- 讀到 agent 寫到一半的內容時，MUST NOT 把 Session 寫壞。
- 接續途中 Session 被刪掉時（不論是這次接續發現、還是背景上傳發現），同一段工作 MUST 只另存成一個新的 Session；之後的寫回（含最後一次與補存）MUST 寫到這個新的 Session。

### Requirement: 同一台機器上不能同時接續同一個 Session
一個 Session 正在接續中（這台機器上有另一個 `agora continue` 正在寫回它）時，再對它 `agora continue`（不含 `--fork`）MUST 被拒絕（exit 1），說明它正在接續中。對它 `--fork` 不受影響。

#### Scenario: 執行中看得到進度
- **WHEN** 用 `agora continue` 接續 Session A，在 agent 裡持續對話，經過一次定期寫回並上傳完成
- **THEN** 不等 agent 結束，A 在本機與 Drive 上就已經是包含新對話的版本

#### Scenario: 沒有新內容不寫
- **WHEN** agent 開著，但一次定期寫回的間隔內沒有新的對話
- **THEN** A 的 session.md 沒有被重寫，Drive 沒有新的上傳

#### Scenario: 不弄亂畫面
- **WHEN** 定期寫回發生時（包括讀到寫到一半的最後一行、上傳失敗）
- **THEN** agent 開始到結束之間，終端機上沒有任何 agora 的輸出；log 檔有記錄；結束時說明失敗過幾次

#### Scenario: 途中被刪
- **WHEN** 接續 A 的途中，A 被別台機器刪掉，之後又經過兩次定期寫回、agent 才結束
- **THEN** 只多出一個另存的新 Session B（`parents` 含 A），之後的寫回都寫到 B；結束時印出另存的說明

#### Scenario: 離線期間被刪
- **WHEN** 接續 A 時 Drive 連不上，期間 A 被別台刪掉；恢復連線後，背景上傳把這次的修改另存成 Y；agent 之後才結束
- **THEN** 只有 Y 一個另存的 Session，之後的寫回與最後一次都寫到 Y

#### Scenario: 最後一次沒有新內容但已被刪
- **WHEN** 最後一次定期寫回把 A 傳上 Drive 之後，A 被別台刪掉，之後 agent 結束時沒有新的對話
- **THEN** 這段對話另存成新的 Session，不會只留在被刪的 A 裡

#### Scenario: agora 被強制結束
- **WHEN** 定期寫回並上傳完成之後，agora 與 agent 都被強制結束
- **THEN** Drive 上已經有那一次定期寫回的內容；下一個指令的補存寫到同一個 Session（途中另存過的話寫到另存的那個）

#### Scenario: 同一台機器再接續一次
- **WHEN** A 正在接續中，在另一個終端機 `agora continue session A --agent claude`
- **THEN** 被拒絕（exit 1），說明 A 正在接續中；第一個接續不受影響

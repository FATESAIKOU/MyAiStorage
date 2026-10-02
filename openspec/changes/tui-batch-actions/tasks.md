## 1. 動作改用子程序（負責：impl3 Opus 恢復後；在那之前 impl2）

- [ ] 1.1 新的等待視窗：子程序跑 `agora` 指令、逐行讀輸出、進度條讀 `k/N`、Esc 對 process group 送 SIGINT；`spawn` 可替換以便測試（spec「進度與中斷」）
- [ ] 1.2 import、merge、delete、pull、push 改用它；import 同一個 agent 的列合成一個指令
- [ ] 1.3 拿掉執行緒裡的 `redirect_stdout`（review K3）；首次設定與同步若仍用執行緒，只收自己的輸出

## 2. 選取與按鍵

- [ ] 2.1 動作作用在勾選的列，沒有勾選時游標那一列（spec「動作作用在勾選的列」）
- [ ] 2.2 `a` 全選切換，只算篩選後看得到的列（spec「全選切換」）
- [ ] 2.3 按鍵改成 spec「按鍵」：拿掉 `r`、`s`，新增 `p`、`P`；按鍵列只顯示能用的

## 3. 雲端欄

- [ ] 3.1 Agora 頁加「雲端」欄（✓、✗、未上傳）（spec「雲端欄與 pull／push 的選項」）
- [ ] 3.2 pull、push 的確認視窗加預設不勾的選項，對應 `--not-exist-delete`、`--not-exist-upload`
- [ ] 3.3 對雲端沒有的列接續、改標頭時，顯示指令模式的拒絕訊息

## 4. 測試與收尾

- [ ] 4.1 `run_test` 測：勾選的列被送進指令、`a` 切換、進度條跟著 k/N、Esc 送中斷、雲端欄
- [ ] 4.2 `docs/design.md` 5.9 更新
- [ ] 4.3 PM 用假資料在 pane 操作一遍；review 審程式

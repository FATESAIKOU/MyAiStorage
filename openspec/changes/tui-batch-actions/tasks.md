## 1. 動作改用子程序（負責：impl3 Opus 恢復後；在那之前 impl2）

- [x] 1.1 新的等待視窗：子程序跑 `agora` 指令、逐行讀輸出、進度條讀 `k/N`、Esc 對 process group 送 SIGINT；`spawn` 可替換以便測試（spec「進度與中斷」）
- [x] 1.2 import、merge、delete、pull、push 改用它；import 同一個 agent 的列合成一個指令
- [x] 1.3 拿掉執行緒裡的 `redirect_stdout`（review K3）；首次設定與同步若仍用執行緒，只收自己的輸出
- [x] 1.4 opencode 的 summarize 一從事件串流讀到 session id 就寫 `pending-<id>`（review V5；改 `opencode.py`，負責：impl1）
- [x] 1.5 Esc 的升級：SIGINT → 5 秒 SIGTERM → 5 秒 SIGKILL；`stdin=DEVNULL`；進度只讀 `[agora] … k/N` 的行（review V1、V2、V8）
- [x] 1.6 review T2-sec1 M1～M3：升級看整個 process group、中斷不再開始下一段、離開前停掉執行中的 group（順手修 L1～L7 與 M5）

## 2. 選取與按鍵

- [x] 2.1 動作作用在勾選的列（只算看得到的；被篩選掉的提示數量），沒有勾選時游標那一列；merge 照畫面順序；成功後清掉勾選、失敗或中斷時保留（spec「動作作用在勾選的列」，review V4、V6）
- [x] 2.2 `a` 全選切換，只算篩選後看得到的列（spec「全選切換」）
- [x] 2.5 review T2-sec2 的 2.2 兩點：`a` 之後游標留在原位、補「全部取消時隱藏的勾選仍在」的測試
- [x] 2.4 review T2-sec2 M1：只清掉這次真的成功送出的那些列的勾選
- [ ] 2.3 按鍵改成 spec「按鍵」：拿掉 `r`、`s`，新增 `p`、`P`；按鍵列只顯示能用的

## 3. 雲端欄

- [ ] 3.1 Agora 頁加「雲端」欄（✓、✗、未上傳）（spec「雲端欄與 pull／push 的選項」）
- [ ] 3.2 pull、push 的確認視窗加預設不勾的選項，對應 `--not-exist-delete`、`--not-exist-upload`
- [ ] 3.3 對雲端沒有的列接續、改標頭時，顯示指令模式的拒絕訊息

## 4. 測試與收尾

- [ ] 4.1 `run_test` 測：勾選的列被送進指令、篩選掉的不算、`a` 切換、進度條跟著 k/N、Esc 送中斷、雲端欄
- [ ] 4.1b 用**真的子程序**測一次 Esc：整個 process group 都停、沒有殘留的子程序（review V7）
- [x] 4.2 `docs/design.md` 5.9 更新（子程序與進度條、Esc 中斷的階梯、勾選規則與 `a` 全選、雲端欄與 pull／push 的兩個選項、按鍵表；順手刪掉 5.10 重複的一句 import 敘述）
- [ ] 4.3 PM 用假資料在 pane 操作一遍；review 審程式

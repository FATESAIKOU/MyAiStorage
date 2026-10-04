## 1. 實作（負責：impl1）

- [x] 1.1 `src/agora/cli.py` 的互動模式分支，在 `from agora import tui` 之前：`os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")`，加一行註解說明原因（issue #23）
- [x] 1.2 單元測試（子程序、乾淨的環境、隔離變數）：沒設時 import 互動模式之後 `textual.constants.DISABLE_KITTY_KEY` 為真；設成 `0` 時為假；指令模式不設
- [x] 1.3 README 互動模式那一段加一句：沒有設定時 agora 會自己設成 1（關掉）；自己設了就照你的設定：只有 1 會關掉，其他任何值（包括空字串）都會打開

## 2. 收尾

- [ ] 2.1 review 審
- [ ] 2.2 本人在 herdr 的 pane 裡用中文輸入法在篩選框選字（PM 帶）
- [ ] 2.3 和 `preview-search-keys` 一起開 PR 合進 main（使用者合）；本人試過後 issue #23 照規則關閉

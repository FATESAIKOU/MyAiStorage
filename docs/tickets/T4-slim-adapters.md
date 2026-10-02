# T4：精簡兩個轉接器、放寬行數目標

2026-10-03，使用者決定。狀態：待轉成 OpenSpec change（排在 T3 之後）。

## 使用者的決定

程式碼約 3,400 行，超過 2,900 行的目標。使用者選「先精簡，再放寬一點」。

## 要做的

- 照 `docs/review/adapters-size.md` 精簡 `agents/opencode.py` 與 `agents/claude.py`（review 估約 85 行；不拿掉 export 的重試是 65 行），**行為不變**，測試全過。
- 做完量一次行數（`docs/review/T1-size.md` 的算法），新的目標＝做完後的行數往上取到整百，加上 T3 預估的增加量；由 PM 寫進任務單與 `docs/design.md`。

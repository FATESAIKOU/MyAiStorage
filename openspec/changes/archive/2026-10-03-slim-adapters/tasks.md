## 1. 精簡（負責：impl1）

- [x] 1.1 `docs/review/adapters-size.md` 第 1 節 B1～B5：共用的部分搬到 `base.py`。B5 的 `warn` 只放在 base，不動 store／cache
- [x] 1.2 第 2 節 opencode 的 O2～O6（**不做 O1**）
- [x] 1.3 第 3 節 claude 的各項
- [x] 1.4 每一項跑一次相關測試；全部做完跑完整 unit；用 T1-size 的算法量 opencode、claude、base 的行數，前後各記一次

## 2. 收尾

- [x] 2.1 review 確認行為不變（`docs/review/T4.md`、`T4-sec1.md`；K1～K4 在 af1d869 修好，兩個小差異寫進 proposal）
- [x] 2.2 量行數：轉接器 1,043 → 1,007（−36）。HEAD（ce7bfb6，含 T3 第 1、2 節）全部 3,676 行。T3 的增加比預估多（約 +260，預估 70～120），放寬到哪裡**移到任務單等使用者決定**

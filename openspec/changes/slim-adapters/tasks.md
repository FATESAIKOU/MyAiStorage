## 1. 精簡（負責：impl1）

- [ ] 1.1 `docs/review/adapters-size.md` 第 1 節 B1～B5：共用的部分搬到 `base.py`。B5 的 `warn` 只放在 base，不動 store／cache
- [ ] 1.2 第 2 節 opencode 的 O2～O6（**不做 O1**）
- [ ] 1.3 第 3 節 claude 的各項
- [ ] 1.4 每一項跑一次相關測試；全部做完跑完整 unit；用 T1-size 的算法量 opencode、claude、base 的行數，前後各記一次

## 2. 收尾

- [ ] 2.1 review 確認行為不變（mutation 在副本裡做）
- [ ] 2.2 PM 依做完的行數定新的目標，寫進 `docs/design.md` 與任務單

## 1. 實作

- [ ] 1.1 `cmd_continue` 加 `--fork`：組 raw → 接上 fork 的兩則（同 agent 時用目標 agent 的格式接在原 raw 後面，必要時補轉接器方法）→ `start_native` → 建新 Session 的 header（照抄使用者欄位、title 加 `-fork`、`FORK_DROP_KEYS`、`relation: fork`、`parents`、`source` 是這次的 agent session）→ `_save(update=False)` → pending 寫新 Session → 跑 agent → `_finish` 寫回新 Session（design「Decisions」）
- [ ] 1.2 `FORK_NOTE`／`FORK_READY` 常數；`{header}` 是使用者欄位的一行 YAML flow，不含 `agora`
- [ ] 1.3 確認 `relation: fork` 對 search、delete（有子 Session 拒絕）、`_rescued_pair`、對 fork 再 continue／merge 的影響，照 design 補測試
- [ ] 1.4 單元測試（假 agent、隔離環境）：spec 的五個 Scenario；同 agent 時原 raw 的工具呼叫保留（不經轉換）；原 Session 的 session.md 與索引列 byte 不變；打開就離開不會存第二次
- [ ] 1.5 整合測試補一個 fork（只用 `agora-test`，真的 claude 或 opencode）

## 2. 收尾

- [ ] 2.1 `docs/design.md` 5.4 補 fork、README 的 continue 加 `--fork`
- [ ] 2.2 review 審
- [ ] 2.3 PM 用假資料實跑一次 fork（同 agent、跨 agent、打開就離開）
- [ ] 2.4 開 PR（本人 merge）

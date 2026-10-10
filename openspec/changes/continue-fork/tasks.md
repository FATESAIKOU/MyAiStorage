## 1. 實作（負責：impl1）

- [ ] 1.1 store／`_save`：寫回一個還沒被 Drive 確認過的 Session 不標 `.update`（design「還沒被 Drive 確認過的 Session」）；補測試：離線 fork 之後上傳、既有的離線 import → continue／edit 不再另存
- [ ] 1.2 `Agent` Protocol 新增 `append_native(raw, turns)`；claude（接在最後一個有 uuid、非 sidechain 的行之後；結尾是 user 或 tool_use 先補 `NO_REPLY`；`sessionId`／`cwd` 照 start_native 的規則）與 opencode（先套用 revert、抄 model／agent／providerID／modelID）各自實作；所有 FakeAgent 補上。轉接器層級測試（cl-basic、oc-basic）：鏈接得上、`turns(append_native(raw, t)) == turns(raw) + t`、原本的行一個 byte 不變、`before_count` 多 2
- [ ] 1.3 `cmd_continue --fork`：照 design「流程」的 8 步（先驗 header → 組 raw 接 fork 兩則 → start_native → export → 建 B 並 `_save(update=False)` → pending → agent → `_finish`）；`FORK_NOTE`／`FORK_READY`、`FORK_DROP_KEYS`；title 規則；`sources` 含 A；A 雲端沒有就拒絕；`--fork` 只給 continue
- [ ] 1.4 打開就離開：印 B 的 id、訊息、exit 依步驟 6、不寫 `unsaved-launches`；有新內容時 exit 取較差的；pending 記錄的欄位（`fork`、`saved_md5` 等，design「pending 記錄」）
- [ ] 1.5 import 的 S7：fork 子 Session 不觸發另建；continue／merge 子 Session 照舊
- [ ] 1.6 單元測試：spec 的九個 Scenario；A 的 session.md 與索引列 byte 不變；`FORK_NOTE` 的格式；對 B 再 continue／merge
- [ ] 1.7 用真的 claude 實測：A 以 tool_use 結尾時 fork 之後 `--resume` 是否被拒絕（結果記在這裡）；用真的 opencode 實測 revert 與 model
- [ ] 1.8 整合測試：claude 與 opencode 各補一個 fork（只用 `agora-test`）

## 2. 收尾

- [ ] 2.1 `docs/design.md` 3.4（relation 加 fork）、5.4 第 2 點與第 6 點、5.6；README 的 continue 加 `--fork`
- [ ] 2.2 review 審程式
- [ ] 2.3 PM 用假資料實跑一次 fork（同 agent、跨 agent、打開就離開、離線），觀察排序日期與「↻」（F17）
- [ ] 2.4 和 `continue-periodic-sync` 一起開 PR（本人 merge）

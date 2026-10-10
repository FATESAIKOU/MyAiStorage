## 1. 實作（負責：impl1）

- [x] 1.1 store／`_save`：寫回一個還沒被 Drive 確認過的 Session 不標 `.update`（design「還沒被 Drive 確認過的 Session」）；補測試：離線 fork 之後上傳、既有的離線 import → continue／edit 不再另存（`store.never_confirmed`，測試在 test_writes_final.py；離線 fork 之後上傳隨 1.3 的 fork 一起補）
- [x] 1.2 `Agent` Protocol 新增 `append_native(raw, turns)`；claude（接在最後一個有 uuid、非 sidechain 的行之後；結尾是 user 或 tool_use 先補 `NO_REPLY`；`sessionId`／`cwd` 照 start_native 的規則）與 opencode（先套用 revert、抄 model／agent／providerID／modelID）各自實作；所有 FakeAgent 補上。轉接器層級測試（cl-basic、oc-basic）：鏈接得上、`turns(append_native(raw, t)) == turns(raw) + t`、原本的行一個 byte 不變、`before_count` 多 2（`agents/base.py` 的 `NO_REPLY` 常數、`claude._leaf_uuid`／`_asked_without_an_answer`、`opencode._apply_revert`／`_role_fields`；測試在 test_agent_claude.py／test_agent_opencode.py。C12：test_cli.py／test_cli_more.py／test_tui.py 的 FakeAgent 已補 `append_native`，cli 改用 `base.NO_REPLY`，測試在 test_cli_fork.py）
- [x] 1.3 `cmd_continue --fork`：照 design「流程」的 8 步（先驗 header → 組 raw 接 fork 兩則 → start_native → export → 建 B 並 `_save(update=False)` → pending → agent → `_finish`）；`FORK_NOTE`／`FORK_READY`、`FORK_DROP_KEYS`；title 規則；`sources` 含 A；A 雲端沒有就拒絕；`--fork` 只給 continue
- [x] 1.4 打開就離開：印 B 的 id、訊息、exit 依步驟 6、不寫 `unsaved-launches`；有新內容時 exit 取較差的；pending 記錄的欄位（`fork`、`saved_md5` 等，design「pending 記錄」）
- [x] 1.5 import 的 S7：fork 子 Session 不觸發另建；continue／merge 子 Session 照舊
- [x] 1.6 單元測試：spec 的九個 Scenario；A 的 session.md 與索引列 byte 不變；`FORK_NOTE` 的格式；對 B 再 continue／merge
- [x] 1.7（claude 部分：PM 10-10 跑 `tests/integration/test_fork_real.py`，A 以沒回覆的 tool_use 結尾、以及平行 tool_use 第一個沒結果時，fork 之後 `claude --resume -p` 都 rc=0 正常回覆，所以只補 NO_REPLY 文字就夠，不需要補 tool_result；最後一個 uuid 行不是對話行時看得到 A 的第一則。opencode 部分：測試自編的 export 是 1.18.34 的形狀，1.18.35 的 import 要求更多欄位，修好測試（9e60c9b，不是程式的 bug）後重跑通過：先套用 revert、fork 兩則在最後；revert 指標指到的那個 part，opencode 自己清理時會刪掉，`append_native` 也刪掉，一致（C7）用真的 claude 實測：A 以 tool_use 結尾時 fork 之後 `--resume` 是否被拒絕（結果記在這裡）；用真的 opencode 實測 revert 與 model
- [x] 1.8 整合測試：claude 與 opencode 各補一個 fork（只用 `agora-test`）——`tests/integration/test_e2e_fork.py`（impl3，efe63a8），PM 10-10 跑：同 agent（claude）、opencode、跨 agent、打開就離開，4 passed（12 分 45 秒）

## 2. 收尾

- [x] 2.1 `docs/design.md` 3.4（relation 加 fork）、5.4 第 2 點與第 6 點、5.6；README 的 continue 加 `--fork`（impl3：51a9863；verified 照抄的字面 241ebb9）
- [x] 2.2 review 審程式（動手前審設計 F1～F18；程式審查 C1～C12、M1～M9（Claude 子代理）；修正複查 arch：continue-fork-code-3，沒有未修的 High／Medium；Low 的兩個測試缺口已補 322c2ec）
- [x] 2.3 PM 用假資料實跑一次 fork（同 agent、跨 agent、打開就離開、離線），觀察排序日期與「↻」（F17）（PM 10-10 用 /tmp/agora-trial2 假資料＋假 agent：同 agent fork 的 B 標題加 -fork、relation fork、parents／sources 含 A、對話最後是 fork 說明與新的一句，A 的 md5 不變；打開就離開印 B 的 id、不寫 unsaved-launches；跨 agent 照樣建 B；離線 fork 恢復後 push，B 本身上 Drive、沒有另存。排序日期與「↻」在假資料上看不出差異，留待本人實用時觀察）
- [ ] 2.4 和 `continue-periodic-sync` 一起開 PR（本人 merge）

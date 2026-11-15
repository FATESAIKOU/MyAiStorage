# Tasks：angareion 待決（#468）

依 `design.md` §19 切 PR，合併順序 PR-1 → PR-2 → PR-3 → PR-4。勾選＝這個 PR 的內容已完成；「合併」由 PM 決定。

## 1. PR-1 讀取端（已 merge：90004ad）

- [x] 1.1 `Message.datatype`（讀取寬鬆、預設 `message`）、`message_to_dict` 的 `datatype` 鍵、log 快取升到 v2、`_format_message` 標出 datatype、`SELF_SILENT_DATATYPES` 與遞送規則、`EnumerableBackend` 協定（C-1～C-3、C-20(a)～(c)、C-21、C-24）

## 2. PR-2 寫入端（已 merge：39cf797）

- [x] 2.1 `send` 寫 `datatype: message`，front matter 鍵順序照 §4.1（C-5）

## 3. PR-3 純函式（已 merge：463b27a）

- [x] 3.1 `decision.py`：payload 的寫入嚴格與讀取寬鬆、`fold`、`snapshot_for`、`plan_submit`（格式、匯總、去重、snapshot、submit 的預檢與分組分塊）
- [x] 3.2 單元測試：C-6（匯總部分）、C-8、C-9（匯總部分）、C-10、C-11（單元部分）、C-15～C-17、C-19（through 的選法與一般訊息推進 cutoff）、C-20(d)、C-26、C-27（單元部分）、C-28（`plan_submit` 的部分）、C-29（`plan_submit` 的部分）

## 4. PR-4 CLI 與文件（本分支，未 merge）

- [x] 4.1 `src/angareion/decision_cli.py` 與 `cli.py` 的 `decision` 子指令：6 個 action（`ask`、`update`、`withdraw`、`submit`、`list`、`snapshot`），寫入前預檢，寫入後的自動 snapshot（`dec.SNAPSHOT_EVERY`）
- [x] 4.2 PR-3 結構審的小修：`FoldResult`、`SubmitPlan` 加 `frozen=True`，`FoldResult(...)` 改關鍵字建構，`decisions`／`ignored`／`submitted` 的 `TypedDict`（只給型別，執行期仍是 dict），`decision_cli` 的後端參數是 `send` 與 `enumerate_conditional` 的協定
- [x] 4.3 CLI 測試 `tests/unit/test_angareion_decision_cli.py`：C-4、C-6、C-7、C-9、C-11～C-15、C-18、C-19、C-20(e)、C-22、C-23、C-25、C-27（`list`）、C-28（CLI 與 `plan_submit` 的單元部分）、C-29（CLI 部分）；寫入的 C-4 與 C-13 另含經 `serialize_message` 讀回的假後端
- [x] 4.4 `skills/angareion/SKILL.md` 與 `docs/angareion.md`：`decision` 各 action 的用法與輸出、`read`／`log` 的 `--json` 形狀（含 `datatype`；`decision list --json` 的形狀照 §5，不含 `datatype`）、App 寫 submit 的格式要點（R-A3）、給 PMO 的用法
- [x] 4.5 spec delta：`specs/angareion/spec.md`（「六個介面」改成七個介面家族、「訊息格式」加 datatype），新增 `specs/angareion-pending-decision/spec.md`（每條 Requirement 附 Scenario，對應 C-編號）
- [x] 4.6 合併（#474，d9e967c）

## 5. 不做

- [ ] 5.1 PR-5：`pmo` 的事件標籤（Q-7 照建議不做，維持印成 `[msg]`）
- [ ] 5.2 將來的 `ask` 放寬（提問頻道＋提問者的等式，Q-6）、複選（payload 版本 2，X-6）、過期（X-4）：本單不做，另開單

# 非作者審查報告：agent-sidecar 第二批 (tasks 3.3, 3.4, 3.5 ＋ task 5.2 文件)

- **審查對象**：commits `110b6e2`（3.4 啟動／終止 team、harness 載入）、`d6b697b`（3.5 回報中繼 socket、`SO_PEERCRED`／`LOCAL_PEERPID`、`sidecar say`）、`eee49cc`（3.3 收信迴圈：先寫佇列再 ack、去重、重啟恢復、urgency 投遞）、`e84e51a`（`docs/sidecar.md`）；複查 `22fcfed`（第一批 6 項修正）。（作者：agy4）
- **審查者**：Muse Spark（本來排 agy3，額度用完改由本人做）
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/agent-sidecar-design/design.md`（D1～D9；D3 含 10-11 spike：執行中 prompt 會排隊、緊急先 esc，claude 1 次、opencode 2 次）
  - `openspec/changes/agent-sidecar-design/specs/agent-sidecar/spec.md`
  - `openspec/changes/agent-sidecar-design/tasks.md`
  - 第一批審查 `docs/review/agent-sidecar-batch1.md`
- **方法**：worktree 只讀（`src/angareion/cli.py` 有 agy1 未 commit 的改動，未碰）；全部改壞驗證與探針都在副本 `/tmp/sidecar-rv2`（`git archive HEAD`）做，`PYTHONPATH` 指副本 `src`、探針只放副本 `tests/unit/test_rv2_probe.py`；不連網、不呼叫真的 herdr（全用 `FakeHerdrClient`／`FakeBackend`）；憑證檔一律不讀。worktree 側跑 sidecar 六個測試檔：36 passed。

---

## 結論

**暫緩合併：2 項 High 先修（都是 spec MUST 在主要路徑上不成立，其中 H1 還被現有測試背書）；4 項 Medium 建議同批修或開後續工單；Low 與文件項請 PM 裁量。**

- **合規／寫得好的部分**：
  - 中繼認證方向正確：`from` 取驗證過的 `agent.identity`（`relay.py:330-337`），呼叫方不能冒充別的 team；認證失敗 fail-closed（`herdr` 壞掉或無紀錄時拒絕）；`get_peer_pid` 在本機 socketpair 實測回傳自己 pid（Linux `SO_PEERCRED`／macOS `LOCAL_PEERPID` 雙路）。
  - 先寫佇列再 ack 的順序、`seen.json` 去重、queue 內 id 去重、重啟後佇列仍在，都有測試守住（`test_sidecar_receiver.py` 5 條全綠）。
  - 高緊急度打斷走 spike 結論（claude esc×1、opencode esc×2、不用 ctrl+c、不等 `--wait`），`--fork` 隔離與 harness 原文第一則送入，都有測試斷言。
  - 第一批 6 項修正全部「改壞會紅」（見 (a) 表），不是只改心安的。

---

## (a) 第一批 6 項修正複查（`22fcfed`）：全部改壞會紅

在副本把每一項改回修前形狀，只跑它對應的釘住測試：

| 項 | 改壞方式 | 釘住測試 | 結果 |
|---|---|---|---|
| H1 終止等待 | `sleep_fn(timeout_stage)` → `time.sleep(min(timeout_stage, 0.05))`（2 處） | `test_termination_waits_full_timeout_stage_without_clamping` | **紅**（`sleep_calls` 全空，不再是 `[5.0, 5.0]`） |
| H2 佇列檔名編碼 | `_queue_file`／tmp 回到 `f"{identity}.json"` | `test_queue_identity_with_special_characters_encoded` | **紅**（`FileNotFoundError: .../.tmp-owner-org/worker-team.json`，修前原形） |
| M1 只看狀態列 | 四處 `_has_indicator_in_tail` 回到 `in screen` 全文比對 | `test_is_working_only_checks_tail_lines_ignoring_historical_dialogue` | **紅** |
| M2 壞檔備份 | registry／queue 拿掉改名備份、靜默重設 | `test_registry_corrupt_file_backup`、`test_queue_corrupt_file_backup` | **雙紅** |
| L1 打斷失敗回報 | `interrupt` 讀錯回 `True`、`send_prompt` 不檢查回傳 | `test_interrupt_reports_failure_on_exception_or_timeout` | **紅** |
| L2 recover 用 process-info | `recover_agents` 回到只比名字 | `test_recover_agents_verifies_foreground_process_group` 紅，舊的 `test_recover_agents_against_herdr_agent_list` 照舊綠 | **新測紅、舊測綠**（行為差異正好被兩代測試夾住） |

附帶缺口（Low，見 L7）：M2 修了三個檔（registry、queue、**seen**），但釘住測試只有 registry 與 queue；`SeenStore` 的 `.corrupt-` 備份沒有直接測試（receiver 測試只走正常路徑）。

---

## (b) 新審查

### High (2 項)

#### H1. `sync_period` 單位錯：sidecar 以為是秒，agora 讀的是分鐘——預設 300 變成 5 小時才寫回一次

- **問題**（`src/sidecar/host.py:79,171`、`tests/unit/test_sidecar_team_lifecycle.py:54-56`、`docs/sidecar.md:112,241`）：
  - `start_team` 預設 `sync_period=300`，`start_agent` 預設也是 `300`，直接拼成 `--sync-period 300`。
  - agora 的 `--sync-period` 單位是**分鐘**（`src/agora/cli.py:51` `SYNC_PERIOD_MINUTES = 5.0`、`615-629` 明寫 "is minutes"）。300 → **300 分鐘＝5 小時**。
  - design D4 寫的是「預設 5 分鐘」；spec 要求「啟動 agent MUST 用 `agora continue session <id> --agent … --sync-period …`」。意圖是 5 分鐘，實際是 5 小時：執行中進度上不了 Drive；agent 中途被殺時最多丟 5 小時而不是 5 分鐘。
  - `docs/sidecar.md:112` 把 `teams.<id>.sync_period` 寫成「週期秒數（預設 `300`）」、`:241` 寫「定期背景寫回秒數（預設 300 秒）」——文件和程式抱著同一個誤會。
  - 最糟的是**測試把錯的釘住了**：`test_start_team_reads_config_and_loads_harness` 斷言 config `sync_period: 600` → `"--sync-period 600"`（600 分鐘＝10 小時），修好後這條要跟著改。
- **實測方法與結果**（副本探針 `test_probe_sync_period_unit_seconds_vs_agora_minutes`）：
  - 預設 config（不給 `sync_period`）跑 `start_team`，抓 `pane_run` 的指令：`agora continue session agora:01JHOME --agent opencode --fork --sync-period 300`。
  - 對照 agora 本體：300 分鐘。與 D4「預設 5 分鐘」差 60 倍。
- **修復建議**（二選一，請 PM 定）：(A) config 維持秒，組命令時除以 60（300→`--sync-period 5`）；(B) config 語意改成分鐘、預設改 5，文件與測試同步改。无论哪個，`--fork` 保留；釘住測試要改成斷言換算後的正確值。

#### H2. `deliver_hand_off` 在 `src/` 沒有任何呼叫者：低緊急度「下一次啟動先送」不會發生

- **問題**（`src/sidecar/receiver.py:144`）：
  - spec：「`urgency` < 8：MUST 進佇列，在下一次 hand-off 送出」；Scenario「低緊急度等下一次」：「下一次啟動 agent 時這則訊息先送進去」。D3：「agent 一啟動（herdr 偵測到、prompt 出現）就用 `herdr agent prompt` 依序送出」。
  - `poll_once` 在「沒有 running agent」時只 enqueue 就返回（`:138-140`）；`start_team`／`start_agent` 全程沒呼叫 `deliver_hand_off`（repo-wide grep：`src/` 只有定義，呼叫只出現在兩個測試裡）。
  - 現有測試 `test_receiver_low_urgency_queues_and_hand_off_delivers` 之所以綠，是**測試自己扮演了缺掉的那條接線**（手動呼叫 `deliver_hand_off`）。生產路徑上，idle 時進佇列的訊息會永遠躺著，直到某次 `poll_once` 遇到 running agent 才順手送出——而那正是 D3 說要等 hand-off 的情境。
- **實測方法與結果**（副本探針 `test_probe_handoff_delivery_has_no_caller`）：
  - urgency 3、無 agent → `poll_once`（queue 剩 1）→ `start_team` → 新 agent 收到 prompts `[]`，queue 還是 1。
- **修復建議**：`start_team` 在 harness 送入之後（或 `start_agent` 尾端）呼叫 `receiver.deliver_hand_off(identity, agent_name)`；注意和 M3 的等待順序（先等 prompt 出現再送，否則 hand-off 的第一批一樣可能打空）。要決定 `Receiver` 從哪裡取得（`start_team` 目前拿不到 receiver——接線方式一併定）。

### Medium (4 項)

#### M1. `seen.add` 在 `enqueue` 之前：死在中間的訊息永久遺失（連重送都被擋掉）

- **問題**（`src/sidecar/receiver.py:95` 在 `:108` 之前）：
  - `poll_once` 先 `self.seen.add(msg.id)`（立刻落檔），再 `self.queue.enqueue(...)`。死在這兩步之間（crash、磁碟錯誤、`enqueue` 丟例外）→ 重啟後 seen 有、queue 沒有 → 之後每次 poll 都被 seen 擋掉 → **這則訊息永遠不投遞、也不 ack**（spec：「sidecar 自己重啟 MUST NOT 讓任何還沒投遞的訊息遺失」）。
  - 反過來排（先 enqueue、再 seen）才是安全的：`enqueue` 本來就有 id 去重（`queue.py:78`），crash 在中間只會造成重複進佇列一次，下次照常投遞，at-least-once 語意不變。
- **實測方法與結果**（副本探針 `test_probe_seen_marked_before_enqueue_loses_message_on_crash`）：
  - `enqueue` 換成丟錯的版本跑一次 `poll_once`（模擬死在窗口），換回來、新 `Receiver` 同目錄重讀 → 第二次 `poll_once() == 0`、`queue.count == 0`：訊息蒸發。
- **修復建議**：把 `:95` 的 `seen.add` 搬到 `:108` 的 `enqueue` 之後；補一條「enqueue 失敗時 seen 沒有該 id」的測試。

#### M2. `start_agent(env=None)` 把 sidecar 整個行程環境（含 `A2A_TOKEN`）傳給 pane

- **問題**（`src/sidecar/host.py:95`、`src/sidecar/herdr.py:37-42`）：
  - `sanitize_herdr_env(None)` 回傳的是 `os.environ.copy()` 只扣掉 4 個 `HERDR_*`——`env=None` 時 `clean_env` 就是 sidecar 自己的全環境，照單 `tab_create --env` 送進 agent 的 pane。
  - 探針：行程有 `A2A_TOKEN=super-secret-token` 時 `start_agent(..., env=None)`，pane 拿到 76 個變數，**含 `A2A_TOKEN`**。spec：「被 sidecar 啟動的 agent MUST NOT 在環境變數或檔案裡拿到 A2A token」；Scenario 就是「檢查 agent 行程的環境」。
  - 緩解現況：`start_team` 永遠傳 dict（空的也沒事），生產路徑踩不到；現有測試 `test_agent_environment_isolation_no_a2a_token` 只測了 `env={...}`，沒測預設 `None`。但這是設計的核心憑證不變量，預設值不該是個陷阱。
- **實測方法與結果**（副本探針 `test_probe_start_agent_env_none_leaks_process_env`）：如上，`passed_env.get("A2A_TOKEN") == "super-secret-token"`。
- **修復建議**：`start_agent` 開頭 `env = dict(env or {})`（`None` 視同空，不再繼承行程環境）；補 `env=None` 版的隔離測試。另見文件項 D5：若 systemd unit 或登入環境本身 export 了 token，herdr server 繼承後每個 pane 都有——文件要加一句「token 只放檔案、不 export」。

#### M3. harness 沒等 agent 就緒就送（D4 明寫要等，程式沒等）

- **問題**（`src/sidecar/host.py:119-121`）：
  - D4：「agent 起來（用 `pane wait-output`／`agent wait` 判斷 prompt 出現）**後**，sidecar 把 harness 全文當第一則輸入送進去」；`docs/sidecar.md:29,244` 也寫「啟動並就緒後」。
  - 程式：`agent_rename` 下一行就是 `agent_prompt(harness)`，中間沒有任何等待。D3 spike 只證明「跑起來之後 prompt 會排隊」，沒證明「TUI 還沒起來時的 prompt 不會丟」。現有測試只斷言 prompt 被呼叫過，時序測不出來。
  - H2 的 hand-off 也有同一條：`deliver_hand_off` 同樣不等 prompt 出現。
- **實測方法與結果**（副本探針 `test_probe_harness_sent_without_waiting_for_prompt`）：`start_team` 後的 calls 順序是 `['tab_create', 'pane_run', 'agent_rename', 'agent_prompt']`，全程無 `pane_wait_output`。
- **修復建議**：harness 送出前先 `pane_wait_output(pane_id, ...)` 等 prompt（超時則報錯並記 registry，不靜默送空）；`deliver_hand_off` 同理。FakeClient 的等待永遠成功，另補一條「等不到就失敗」的測試。

#### M4. `recover_agents` 遇到 herdr 一時連不上，把活著的全標 exited

- **問題**（`src/sidecar/host.py:322-353`）：
  - `agent_list` 丟 `HerdrError` → `live_names = set()` → 全部 running 變 "not found … exited"；`pane_process_info` 丟錯 → 單隻變 "no foreground process … exited"。
  - 機器重開時兩邊一起死、pane 本來就沒了，標 exited 是對的；但「只重啟 sidecar、herdr 剛好在 flap／升級」時，活著的 agent 會被集體除名——之後 `start_team` 再開一批變分身，原來那批變無主孤兒繼續跑（design Risks 擔心的殭屍正是這樣來的）。
  - D5 只說「用 list 與 process-info 對照」，沒說連不上時怎麼辦；fail-closed（報錯、不改 registry）比「全標死」安全。
- **實測方法與結果**（副本探針 `test_probe_recover_marks_all_exited_when_herdr_down`）：`agent_list` 永遠丟錯的 fake 下，`recover_agents() == []` 且 running 變 `exited`。
- **修復建議**：`agent_list` 失敗時直接回空（不動 registry）並記 warning；`pane_process_info` 失敗時該隻保持原狀並記 warning，下次 recover 再判。

### Low (8 項，附修法方向)

#### L1. `authenticate_peer` 的 pid-1 交集：`foreground_process_group_id == 1` 時任何本機行程都通過

- `relay.py:194-214`：`ancestors` 永遠含 pid 1（`get_process_ancestors` 走到 1 才停）；`target_pids` 對 pgid 只擋掉 falsy（0），不擋 1。探針：fake `process_info` 回 `fg pgid=1` 時，**無關的 pytest 行程**直接通過認證（`test_probe_auth_accepts_any_pid_when_fg_pgid_is_1`）。
- 實務上 fg pgid 不該是 1，但認證函數不該有「某個數字一出現就全開」的形狀；附帶 PID 重用＋registry 陳舊的同類風險。
- 修法：交集前兩邊都扣掉 `<= 1`（一行）；或只接受 `shell_pid`／`foreground_processes` 的 pid、不用 pgid 比。

#### L2. relay 不驗 urgency 範圍，錯的靜默丟掉（CLI 還寫 0-10）

- `relay.py:324` 的 `int(...)`、負數、`10` 都直通 `sender.send`；`Message` 只允許 0-9（`messages.py:137-139`），`AngareionRelaySender` 丟 `MessageValidationError` 後被 `_handle_conn` 的 `except Exception: pass`（`:340-341`）吞掉——client 只拿到空回應。探針：`send_relay_message(urgency=10)` → `RuntimeError("Empty response")`，信道空無一物。
- 修法：`_handle_conn` 先驗 `0 <= urgency <= 9`，不過就回 `{"ok": False, ...}`（別走 exception）；`cli.py:94` 的 help `0-10` 改 `0-9`（Angareion 的上限）。

#### L3. `export_agent_session` 回的是 home，不是 fork 出來真正寫話的那個

- `host.py:200-209` 回 `rec.agora_session`；`--fork` 下任務對話寫進新 Session，home 不動（spec Scenario 明寫）。探針：`start_team`（帶 `--fork`）後 `export_agent_session(...) == "agora:01JHOME"`。
- 目前 `src/` 無呼叫者；要留就改成追 fork id（啟動時還拿不到，得另想），否則刪掉別留錯的。

#### L4. `terminate_agent` SIGKILL 後不驗證就回 True

- `host.py:415-430`：SIGKILL＋`pane_close` 後直接 `return True`、記 "SIGKILL escalation"，沒再問一次 `is_alive`。殺不死的（D-state 等）會被記成 terminated。
- 修法：照前兩段的形狀再等一次，還活著就回 False 並如實記。

#### L5. `RelayServer.start()` 先 unlink 再 bind：第二個 server 靜默搶走 socket

- `relay.py:242-244`。探針：同路徑起 A 再起 B，之後的 `say` 全進 B，A 還以為自己活著（`a._running is True`）。
- 單 worker 單 sidecar（D1）時踩不到；修法：bind 失敗（`EADDRINUSE`）就報錯不 unlink，或先試連舊 socket 有活人才讓路。

#### L6. socket `0o666` 與 fallback 目錄權限

- `relay.py:251` 註解說「同 worker user」，但 666 是同機所有 user 可連（認證是唯一門，有 L1 的形狀更該收緊）；`get_default_sock_path` 的 fallback `~/.local/state/sidecar/` 不保證 0700，只有 `XDG_RUNTIME_DIR` 那條是 0700。
- 修法：socket 改 0600（註解寫明靠目錄權限＋peer 認證），或文件寫死只用 `XDG_RUNTIME_DIR` 那條。

#### L7. 測試缺口：`SeenStore` 壞檔備份、`last_since`、靜默 except

- M2 的 seen 備份（`seen.py:34-42`）沒有直接測試；`receiver.py:46` 的 `last_since` 建了但永遠是 None（每次非 304 都是全量掃，靠 seen 擋，量大會慢）；`receiver.py:125-126,136-137,162` 與 `relay.py:340-341` 的 `except: pass` 連 log 都沒有，維運時只能猜。
- 修法：各補一條小測試；`last_since` 要嘛用（記 `max(at)`）要嘛刪；except 補 `logger.warning`。

#### L8. `AngareionRelaySender.send` 順手建 channel＋寫死 repo

- `relay.py:151-153`：送回報時「找不到 channel 就建一個」——打錯 channel 名會靜默開新戰場而不是報錯；`from={"repo": "sidecar", ...}`／`to={"repo": "pmo", ...}` 寫死（receiver 的 ack 也是 `repo: sidecar`）。
- `docs/sidecar.md:310` 卻寫 PMO 會收到「來自 `owner-org/worker-team`」——到底是 role 對上就行，還是 repo 也要對（`worker`？），5.1 端到端之前要定下來並寫進文件；否則建 channel 與 repo 映射改可設定。

---

## (c) `docs/sidecar.md` 與程式／design 一致性（`e84e51a`）

逐條都是文件問題，程式不動；嚴重度跟著它描述的程式項走：

- **D-sync（跟 H1）**：`:112`「週期秒數（預設 `300`）」、`:241`「定期背景寫回秒數（預設 300 秒）」、`:27,238` 的 `--sync-period <N>`／`<sync_period>` 沒寫單位。agora 讀的是分鐘：三處都要改（單位＋預設值方向等 H1 定案）。
- **D-harness（跟 M3）**：`:29`「在 agent 啟動並就緒後」、`:244`「偵測 agent 啟動並就緒後」——文件寫的是對的（D4），程式還沒跟上。修完前這兩句會誤導：建議程式修好再說，或先標 TODO。
- **D-env（跟 M2）**：`:150`「傳遞的環境變數清單嚴格受到白名單保護」——程式只是扣掉 4 個 `HERDR_*`（denylist），`teams.<id>.env` 照單全收，`env=None` 還是全量。改成照實寫，並加 M2 建議的那句「token 只放檔案、不 export 到登入環境」（herdr server 繼承來的 pane 擋不住）。
- **D-unreadable**：`:151`「Agent 在其工作目錄與檔案系統中無法讀取 A2A Token」——和 design（「技術上讀得到，但設計的邊界是 agent 不碰；v0 不做硬隔離」）矛盾。照 design 改成誠實版。
- **D-run（缺件）**：`:74` 的 systemd unit 寫 `ExecStart=... -m sidecar.cli run`，但 `run` 子命令**不存在**（`cli.py` 只有 `status`／`say`）；且全 repo 沒有把 `poll_once`＋`deliver_hand_off`＋`RelayServer` 接起來跑的 daemon 入口（跟 H2 同一條線）。文件要嘛先拿掉這行，要嘛等接線任務（4.x／5.1 之前要有）。
- **D-corrupt-example**：`:197` 範例 `registry.json.corrupt-20261011T160000Z`，程式實際是 `strftime("%Y%m%d%H%M%S")` → `.corrupt-20261011160000`（無 T 無 Z）。照程式改。
- **D-queue-layout**：`:184` `queue/<identity>.json`——H2 之後檔名是 percent-encoded（`owner-org%2Fworker-team.json`），加半句。
- **D-m2-note**：`:198`「已在審查（M2）中定案由 agy4 修復，寫完後請與 PM 確認」——`22fcfed` 已經修了，這句 stale，改成已完成。
- **D-python**：`:50`「Python 3.10+」vs `pyproject.toml` `requires-python = ">=3.12"`。照 pyproject 改。
- **D-from（問 PM）**：`:310` 寫 PMO 收到「來自 `owner-org/worker-team`」，但程式 from 是 `{"repo": "sidecar", "role": <identity>}`（L8）。role 對上是否就夠、repo 該不該是 `worker`、要不要可設定——5.1 之前定一下，文件寫死 exact shape。

---

## 實測記錄

- **Baseline（worktree，未改）**：`uv run pytest tests/unit/test_sidecar_host.py test_sidecar_skeleton.py test_sidecar_team_lifecycle.py test_sidecar_receiver.py test_sidecar_relay.py test_sidecar_model_broker.py` → **36 passed**。
- **(a) 6 個反向 mutation（副本）**：上表，每個都只紅它該紅的那條（L2 還示範了新舊測試一紅一綠）。
- **(b) 10 條探針（副本 `tests/unit/test_rv2_probe.py`，全綠＝現象成立）**：

| 探針 | 現象 |
|---|---|
| `test_probe_sync_period_unit_seconds_vs_agora_minutes` | 預設 config 產出 `--sync-period 300`（agora 讀成 300 分鐘） |
| `test_probe_handoff_delivery_has_no_caller` | `start_team` 後 queue 剩 1、新 agent 收到 prompts `[]` |
| `test_probe_seen_marked_before_enqueue_loses_message_on_crash` | 窗口 crash 後重 poll `== 0`、queue 空：遺失 |
| `test_probe_start_agent_env_none_leaks_process_env` | pane 拿到 76 個變數、含 `A2A_TOKEN` |
| `test_probe_harness_sent_without_waiting_for_prompt` | calls 全程無 `pane_wait_output` |
| `test_probe_recover_marks_all_exited_when_herdr_down` | `recovered == []`、running 變 `exited` |
| `test_probe_auth_accepts_any_pid_when_fg_pgid_is_1` | 無關 pid 通過認證 |
| `test_probe_export_returns_home_not_fork_session` | `--fork` 下 export 回 home |
| `test_probe_second_relay_server_steals_socket` | B 起來後訊息全進 B、A 不知情 |
| `test_probe_relay_urgency_out_of_range_silently_dropped` | `-u 10` → 空回應、信道無文 |

---

## 給 PM 的清單

1. **定 H1 的修法方向**：config `sync_period` 維持秒（組命令除以 60）還是改成分鐘（預設改 5）？定了之後 code＋`test_start_team_reads_config_and_loads_harness`（現在釘著錯的值）＋`docs/sidecar.md` 三處同步改。
2. **定 H2 的接線位置**：`deliver_hand_off` 由 `start_team` 呼叫（harness 之後、先等 prompt，順便解 M3 的一半），還是另起 daemon loop（那 `sidecar.cli run` 不存在也要一併做，見 D-run）？`Receiver` 實例從哪裡來一併定。
3. **轉交 agy4**：H1、H2（方向定了就動）；M1（seen/queue 順序對調＋測試）、M2（`env=None` 視同空＋測試＋文件加不 export）、M3（等 prompt 再送 harness／hand-off）、M4（herdr 連不上不改 registry＋log）；L1～L8 與 (c) D-系列擇期（D-run、D-from 建議 5.1 之前定）。
4. **確認事項**：L8 的 from/repo 映射（`repo: sidecar` 是否就是 PMO 要認的形狀）、`urgency` 上限到底是 9 還是 10（CLI 寫 0-10、Angareion 只到 9）。
5. 合併條件建議：H1、H2 修完＋測試跟上後合入；M/L 可開後續工單（#31 的子 issue）追。

---

## 複查（2026-10-11：`53b71b6` H1/H2/M1-M4＋run loop、`7846144` L1-L8；以 D10 為準）

- **複查者**：Muse Spark。方法同上：worktree 只讀；每項在副本 `/tmp/sidecar-rc2`（`git archive HEAD`）改回修前形狀跑對應測試；探針只放副本 `tests/unit/test_rc2_probe.py`、`test_rc2b_probe.py`；不連網、不碰真 herdr。Baseline（worktree）：sidecar 七檔 **69 passed**。
- **結論：H1/H2/M1-M4 與 L1-L6/L8 全部改壞會紅（約 20 個紅點，見下表）；L7 的 sender-failure 回錯誤一小片沒被釘住（附可直接收的探針）；新判一個 Medium（L3 的 fork 擷取在真實時序下幾乎一定抓空）與若干 Low。impl1「約 20 個 mutation 都紅」基本屬實。**

### 複查改壞結果表

| 項 | 改壞方式 | 結果 |
|---|---|---|
| H1 預設改 5 分鐘 | `start_agent`／`start_team` 預設改回 300 | `test_start_agent_default_sync_period_is_5_minutes`、`test_start_team_default_sync_period_is_5_minutes` **雙紅** |
| H2 hand-off 接線 | 刪掉 `start_team` 的 `deliver_hand_off` 呼叫 | `test_start_team_waits_for_prompt_then_harness_then_hand_off` **紅** |
| M1 先 enqueue 再記 seen | 順序調回 | `test_receiver_enqueue_failure_does_not_mark_seen` **紅** |
| M2 `env=None` 給空 | 改回 `sanitize_herdr_env(env)` | `test_start_agent_env_none_does_not_inherit_process_env` **紅** |
| M3 等 prompt | 整段 wait 拿掉 | 3 條（host×2＋team hand-off）**三紅** |
| M4 herdr 壞掉不動 registry | 回到空集合／`alive=False` | 2 條 **雙紅** |
| last_since 游標 | 拿掉追蹤 | `test_receiver_last_since_cursor_filters_old_messages` **紅** |
| L1 認證擋 `<=1` | 六處過濾拿掉 | `test_authenticate_peer_rejects_foreground_pgid_1` **紅** |
| L2 urgency 0-9 三處 | server／client／help 改回 | 3 條 **三紅** |
| L3 fork id 擷取 | 整段拿掉 | `test_export_returns_forked_session_not_home` **紅** |
| L4 SIGKILL 後驗證 | 拿掉 | survivor 那條紅、正常路徑照舊綠（正確分流） |
| L5 拒搶 socket | 回到先 unlink | takeover 紅、stale 替換照舊綠（正確分流） |
| L6 0600＋目錄 0700 | 回到 0666 | `test_relay_socket_and_dir_permissions` **紅** |
| L8 完整位址＋不自建頻道 | 回到寫死 repo＋自建 | 2 條 **雙紅** |
| L7 sender 失敗回錯誤 | 拿掉 try／error JSON | **13 條全綠**——這片沒被釘住（見下） |

### 新判 Medium 1：L3 的 fork 擷取在真實時序下幾乎一定抓空（測試看不出來）

- `start_agent` 的順序是 `pane_run` → `agent_rename` → M3 等待（`\S`）→ L3 讀屏（`agent_read lines=50`）→ 送 harness。
- 但 agora 本體的輸出順序是：`[agora] 工作目錄`（`src/agora/cli.py:1302`，fetch／export／fork-save 之前）→ `[agora] fork 已存成`（`:1352`，export＋存檔之後、開 agent 之前）→ agent TUI 慢慢起來。且 `pane run` 的 shell echo 本身就含非空白字。
- 所以 `\S` 等待幾乎立刻成立（早則 shell echo，晚則工作目錄行），而 L3 的讀屏緊接在後——**一定跑在 fork 行印出之前**（中間隔著 Drive 同步、export、存檔，全是秒級 I/O）。生產環境下 `forked_session` 幾乎永遠是 None，export 照舊回 home：L3 修了等於沒修。現有測試看不出來，因為 fake 的輸出是預先塞好的。
- 今天 `export_agent_session` 在 `src/` 無呼叫者，爆炸半徑是零；但「修了個不會動的」比沒修更糟（以為好了）。
- 建議：fork 啟動時改成**輪詢**讀屏找 fork 正則（沿用 `wait_timeout_ms` 額度，找到就停）——既證明 agora 走過 fork-save，又讓擷取可靠；順便把這段時序寫一條 fake 版測試（輸出分兩段出現：先工作目錄行、後 fork 行）。

### impl1 自定的三件事是否恰當

- **(a) `pane wait-output --regex '\S'` 當「prompt 出現」：不恰當，只能算 liveness，不算 readiness。** 證據如上：agora 自己的啟動輸出（乃至 shell echo）先滿足它，TUI 是否起來、輸入能不能收，它一概不保證；D3 spike 只證明「跑起來後 prompt 會排隊」，沒測過「TUI 還沒起來时的 prompt 去向」。較可靠的作法（都用現有接縫）：`\S` 留著當第一道，後面加一道——fork 啟動就輪詢 fork 行（如上）；一般情況可再等 `agent get` 進入已知狀態或 kind-specific 的就緒字樣（字樣要先在 worker herdr 上 spike 量，opencode／claude 開機畫面是什麼，repo 裡沒有）。在那之前，文件應誠實寫「`\S` 只保證畫面非空」，別寫「prompt 出現」。
- **(b) `teams.<id>.identity` 完整位址：恰當。** D10 明定；`cmd_run` 從 config 組 `team_addresses`，缺了在送出當下明確拒絕（有測試）；`from` 實測就是配好的 `{repo, role, name}`。小 nit：`_from_address` 的錯誤字寫 `{repo, role, name}`，實際只要求 repo＋role（name 可無），字樣對一下。
- **(c) run 預設 30s 輪詢／300s 找回：恰當。** 30s 配 ETag／304＋超限退避，對 GitHub 次級限制很鬆；300s 的找回頻率下，死掉的 agent 最多以 running 留 5 分鐘，期間投遞失敗是 per-message 接住並留佇列、認證看的是 live process-info 而不是 registry，所以陳舊 registry 既送不進去也認不過。心跳 5 分鐘粒度符合 D5「心跳是 `updated_at`」（沒定頻率）。單 worker 單 sidecar，不需 jitter。

### run 迴圈（任務 3）：例外、SIGTERM、節流

- **例外隔離：基本成立，漏一處。** `poll_once` 的後端錯（RateLimit／BackendError／Exception）都接住記 log 下輪再試；單則投遞失敗 per-message 接住留佇列；recover 包住；壞一個身分的佇列檔只影響它自己（讀壞就備份清空）。漏的是**心跳那兩行在 try 之外**（`run.py` recover 區段）：`registry.touch`（落檔 I/O）一丟錯 daemon 直接死——探針證實（`test_rc2b_probe.py`，`touch` 丟 `OSError` 穿透 `run_loop`）。修法：那兩行也包進 try（Low，一行）。
- **SIGTERM 收不乾淨。** `run.py` 無 signal handler，`cmd_run` 只接 `KeyboardInterrupt`；systemd／supervisor 送的 SIGTERM 讓行程直接死，`finally` 跑不到，**socket 檔留在原地**——探針證實（子行程跑 `run_loop`，SIGTERM 後 exit=-15、socket 檔還在）。重啟靠 L5 認 live／換 stale 檔接得上，飛行中的投遞靠 at-least-once 下次重送，所以是整潔問題不是安全問題。修法：裝 SIGTERM handler 轉成優雅退出（走現有 finally 關 relay＋unlink），Low。
- **節流：有照 Angareion 的節流走。** 靜默期靠 ETag／304（`Receiver` 的 etag＋本次補上的 `last_since`）；被 429／403 時 `run_loop` 睡滿 `retry_after` 再試（有測試）。小字：單次退避不封頂——server 若回幾千秒，心跳／找回跟著停擺；可考慮單次上限（例如 300s），不夠下輪再退（Low）。

### 安全（任務 4）：M2 白名單、L1 認證、L6 socket

- **M2：對它要擋的威脅成立。** `env=None`→`{}`（有測試）、team env 只過 operator 配的鍵並扣掉 `HERDR_*`；pane 側 `if env:` 空就不送 `--env`。真正的白名單語意（D10 租約 env 是「唯一的附加來源」）要等租約 plumbing，那是後續工作；現階段「行程環境（含 A2A token）永不進 pane」已守住。附帶：`worktree=True` 那條路根本不傳 env（`worktree_create` 無 env 參數），team env 會被靜默丟掉——舊 behavior，順手面提一句（Low）。
- **L1：修法正確，殘留是 pid 認證固有的。** `<=1` 兩邊都擋（改壞會紅，已驗）；剩下 TOCTOU／PID 重用是此類機制的原生成本，接受。
- **L6：修法正確，收尾建議把目錄 chmod 收斂一點。** socket 0600＋目錄 0700 都有測試釘住；但目錄 chmod 是無條件的——`SIDECAR_SOCK` 若指到共享目錄直下（如 `/tmp/s.sock`，非 root 跑會 EPERM 沒事；指到自家目錄則會把該目錄改成 0700）。建議只對預設 state dir 底下做，或文件寫明「socket 不要直放共享目錄」（Low）。

### 給 PM 的清單

1. **先修再合**：Medium 的 L3 時序（fork 行輪詢＋一條分段輸出測試）；Low 的心跳 try、SIGTERM handler 可同批（一眼大小）。
2. **impl1 的 L7 缺口**：sender 失敗回錯誤那片沒測試釘——探針（副本 `test_rc2_probe.py::test_probe_relay_sender_failure_gets_error_response`，修法在紅、拿掉紅）可直接收進 repo。
3. **要 PM 定的**：(a) 的 TUI-ready 字樣 spike（worker herdr 上量 opencode／claude 開機畫面）做不做、何時做；daemon 起 team 的 orchestrator（run loop 裡沒人呼叫 `start_team`——H2 接的是「被叫時」的線，5.1 的「收到→啟動 team」還缺一段）歸哪個任務。
4. **確認事項**：L8 的 `_from_address` 錯誤字 `{repo, role, name}` vs 實際只驗 repo＋role；L6 目錄 chmod 的範圍；退避睡眠要不要封頂。

## 1. v2 的讀取與指令層（含閱讀版與 server 上下文；一步一個 PR）

**範圍**：`src/agora/agents/opencode.py` 的讀取層與指令層、`tests/fakes/`（fake upstream、`fake_opencode.py`、`opencode_noninteractive.py`）、`tests/conftest.py` 的 opencode 護欄、`tests/fixtures/opencode/` 的 v2 session fixture、`docs/spike/opencode.md`。合併後：opencode 2.x 上的匯入、同 agent 接續、閱讀版都正確——**不再有靜默空閱讀版**（審查 H1）；native／append 與舊 raw 的拒絕留給第 2 步。
**PR 說明記一句**（報告 §H 末段）：sidecar `new` 模式 fallback 的寫回只需要讀取側的修（§F'-1、3、5、6），落在第 1 步與第 3 步。
**PR 說明也要寫（N4／N5）**：(1) PR 1 的 `--fork` 對 v2 的 Session 會報錯（`append_native` 底下還是 v1 形狀的 helper），第 2 步才通——這是響亮的錯誤，不是靜默；(2) 1.1（fake upstream）或 1.5（`--server` 量測）若先各自成一個小 PR 再接 PR 1，不算違反「一步一個 PR」。

- [ ] 1.1 **fake upstream（前置交付物；審查 M6）**：放 `tests/fakes/`，本機、不連外、不用憑證，能回一句話與一次工具呼叫（讀暫存目錄裡自己寫的檔案這種無害動作）。做不到時的出口：先收純文字 fixture、工具形狀記成 open、閱讀版對工具先 `[skip tool]`，並在 PR 說明標明（視為未完成，不是預設路線）
- [ ] 1.2 在隔離環境產生 v2 fixture（用 1.1）：四個 XDG＋`HOME` 指到暫存（`opencode debug paths` 確認）；**在隔離的 XDG_CONFIG 下把 managed 服務的埠改成空閒埠**（審查 H2）；清掉／控制繼承的 `OPENCODE_*`，pytest 外自己設 `AGORA_CONFIG`／`AGORA_CACHE_DIR`／`AGORA_STATE_DIR`／`AGORA_RCLONE`／`RCLONE_CONFIG`（L3）；工作目錄用暫存專案；自編多輪＋一次工具呼叫（第二輪 `-s <id>` 讓跨訊息參照出現）；`opencode session export <id> --standalone` 匯出；**去識別化清單**（`providerState`、`projectID`、HOME 路徑、主機名、email、時間戳、id；由 fake upstream 產生，L2）；**斷言 fixture 至少有一個跨訊息參照**（L6），否則補一輪；放 `tests/fixtures/opencode/oc-v2-*.json`
- [ ] 1.3 嗅探：**每一列同質**——每列有 `type` → v2、每列有 `info` → v1、混合 → `AgentError`（L1）；`messages` 空／不存在時看 `info.location`（v2）／`info.directory`（v1）；兩者都不是 → `AgentError`（明確訊息）
- [ ] 1.4 指令與 server 上下文（**全部在同一步**，審查 M7）：`_server_args()` 一處組；`_export_bytes` → `session export <id> --standalone`；`_import` → `session import <path> --standalone --directory <workdir>`（cwd 與 `PWD` 都給 workdir）；`_delete` → `session delete <id> --standalone`（`_discard`、`_drop_summary_session`、pending 補刪都走它）；summarize → `run --standalone <訊息> -f <材料> --format json`；`opencode_noninteractive.py` 的透傳（`export`／`import`／`session delete`）一起改
- [ ] 1.5 量 `--server`（L4）：隔離環境起一個私有 serve，對它跑 `session export／import --server …`，記語法與行為進 `docs/spike/opencode.md`；量到 `--standalone` 與 live serve 相衝就在第 1 步內改 `_server_args()` 的形狀
- [ ] 1.6 `export()` 的欄位讀 v2：`dir` 讀 `info.location.directory`、`version` 沒有就是 `None`、`model` 讀最後一則 assistant 的 `model.id`、`created_at` 照舊；**`message_count` 只算 user／assistant 列**（`idle` 不算，與 `turns()` 一致；N4）；`_import_verified` 在第 1 步先只比訊息數（內容項目數第 2 步才對 v2 生效）
- [ ] 1.7 `reidentify()` 認 v2：依真 fixture 決定要改的結構位置（訊息 id、assistant 列的參照、`content` 內部的 id 若有）；fixture 上不存在的欄位一律不動；v1 行為不變
- [ ] 1.8 `turns()`／`_lines_of()` 讀 v2（**審查 H1**）：user 的 `text`、assistant 的 `content`（text 一行、tool 一行 `[tool] …`）、`idle` 列略過、附件不算；不認得的型別照舊 `[skip <type>]`
- [ ] 1.9 `fake_opencode.py`：新舊語法都收（`export` 與 `session export`、`import` 與 `session import`、`--standalone`、`--directory` 決定 session 目錄）；v2 session 以扁平存；`session import` 收到 v1 巢狀 payload MUST 以 `SchemaError` 拒絕（模擬 v2）
- [ ] 1.10 conftest 的 opencode 護欄（審查 M5）：`session list --standalone`／`session delete <id> --standalone`；指令非 0 時**大聲失敗**（pytest.fail／raise），不是回空集合
- [ ] 1.11 整合測試的語法／版本探測（L5）：探測 `opencode session --help` 認不認得 `export`；v2 上讓現有的真 opencode 整合測試 skip 到第 5 步（否則它們在 2.x 上會真的跑並撞 49374）
- [ ] 1.12 單元測試：嗅探五種情形（v1／v2／空＋info／混合列／認不得）；指令 argv 帶 `--standalone`／`--directory`（fake 的 `opencode-calls.log`）；v2 的 export 欄位；reidentify v2 round trip（id 全換、參照指對、其餘 byte-for-byte）；**匯入 v2 fixture 後 body 有 user／assistant 段落**（H1）；v1 的既有測試全綠
- [ ] 1.13 改壞驗證：拿掉 `--standalone`／`--directory`、嗅探只認一版／不看同質、export 讀 `info.directory`、reidentify 少改一個 fixture 上的參照、turns 漏掉工具行——**「拿掉 --standalone」只在 fake 上做**（真 opencode 不刻意去走 managed 服務，H2）
- [ ] 1.14 真 opencode 的跑動（1.2；第 2、5 步的整合測試照做）**前後各確認預設埠 49374 的服務沒被動**：只看有沒有在聽／PID／etime，不讀任何資料（H2，被動到就是失敗）
- [ ] 1.15 `docs/spike/opencode.md` 補 v2 一節：fixture 產生步驟（含埠與環境變數）、v2 的指令與旗標、`--server` 的量測結果、格式差異、觀察到的陷阱

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py`（＋受影響的 fake／conftest 測試）；不需要真 opencode。
**改壞驗證**：1.13 逐項記在 PR 說明（哪一項、改壞什麼、哪個測試紅）。

## 2. v2 的寫入與計數（PR 2）

**範圍**：`src/agora/agents/opencode.py` 的寫入層（native／append 與底下的 helper）、`src/agora/cli.py` 的原地更新檢查、fake 的必填欄位驗證、真 opencode 的整合測試。合併後：v2 的接續／fork／merge 後接續、計數語意、舊 raw 的誠實拒絕。

- [ ] 2.1 必填欄位全集：拿 1.2 的 fixture 當模板，在隔離 XDG（＋空閒埠）逐欄餵 `session import` 看 `SchemaError: Missing key at [...]` 補齊（`info`、user 列、assistant 列的 `content`）；結果記進 `docs/spike/opencode.md`，`fake_opencode.py` 依同一份拒絕缺欄位的 payload；**順便量（N1 (d)）**：遷移過的舊 Session（v1 時代進來的）用 v2 匯出時，turn 數和 store 裡那版是否一致——落差要決定「只算有內容的 turn」的比對能不能放行
- [ ] 2.2 `native()` 產 v2：`info` 照模板（含 `location` 等）；訊息列照 v2 的 user／assistant 列，一則一行一個 text；不使用 synthetic／attachment；placeholder id 用 `ses`／`msg`／`prt` 前綴（以 fixture 複驗）；`_session_info` 的欄位集照模板換
- [ ] 2.3 `append_native()` 產 v2（**審查 M1**）：複製原 raw、接上新訊息列；原 raw 其餘部分 byte-for-byte 不動。**逐個點名底下的 v1-only helper 並寫 v2 版**：`_apply_revert`（讀 `info.revert`／`messages[].parts`）、`_role_fields`（讀 `info.role`／`providerID`／`modelID`）、`_unused_stem`（讀 `info`／`parts` 的 id）、`said`／`last_question`／`_created_ms(m.get("info"))`、`_last_model`（讀 `info.modelID`）——v2 扁平列沒有 `info`，不處理會回空／None，輕則被 `SchemaError` 拒絕、重則匯入鏈不對的 Session（靜默）
- [ ] 2.4 回讀驗證的第 2 層（**審查 M2；M8 的 `message_count` 已在 1.6，N4**）：該格式可數的內容項目數——v1 是 `parts` 數、v2 是 assistant 列 `content` 的項目數，沒有這一層時只比訊息數；spec 的說法照這個改
- [ ] 2.5 舊 raw（v1）的同 agent 接續／fork：在開 agent 前拒絕（agent session id 取自 payload 的 `info.id`），**訊息誠實**（審查 M5）：重新匯入不一定原地更新（被分支過的 Session 會另存），來源不在本機時只能跨 agent 接續
- [ ] 2.6 重新匯入的保護（**審查 M5／N1**；動 `cli._import_one`）：原地更新既有 Session 前，比對新匯出與 store 那版的 user／assistant turn 數，**只算至少有一行內容的 turn**（合法的 undo／revert／rewind 讓空 turn 消失，不算變少）；新的**有內容的 turn** 比較少就不覆蓋——**拒絕訊息要寫明下一步**（改用跨 agent 接續，或先讓這個 Session 分支再重新匯入、依分支規則另存），不能只說少了多少；不變／變多照舊更新，被分支過的照現行規則另存
- [ ] 2.7 單元測試：`native → import（fake）→ export → turns` 的 round trip；閱讀版對 fixture 的段落數與工具行；`append_native` 保留工具呼叫與原 raw，**fork v2 raw 的新列 model／鏈指對**（M1）；回讀驗證的 content 項目數（fake 掉一個 content 要紅，M2）；`collect` 只有 idle 列時回 None（沒說話不寫回，M8）；舊 raw 拒絕的訊息內容；**有內容的 turn 變少的重新匯入被拒、拒絕訊息含下一步**，**只有空 turn 消失（undo 的合法變少）時照舊更新**（N1）
- [ ] 2.8 真 opencode 整合測試（隔離 XDG＋空閒埠、fake upstream）：`native()` → 真的 `session import` → `run -s` 一輪 → `collect`；閱讀版含前文關鍵字；至少驗一次 `message_count` 與 `idle` 的關係（M8）
- [ ] 2.9 改壞驗證：native 少一個必填欄位；`idle` 當段落；工具行拿掉；`append_native` 改到原 raw 的其他欄位；v1 helper 的 v2 版拆掉（新列 model／鏈錯）；content 項目數不比對；**「有內容的 turn 少於原本」的檢查拿掉；把空 turn 也算進比較（合法的 undo 被誤擋）**（N1）

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py tests/unit/test_cli.py`；`uv run pytest -q -m integration tests/integration/test_opencode_real.py`（需真 opencode）。
**改壞驗證**：2.9 逐項記在 PR 說明。

## 3. 互動模式 DB（PR 3；第 1 步之後可與第 4 步並行）

**範圍**：`src/agora/agents/opencode.py` 的 DB 讀取（`list_sessions`／`last_message`／`search_text`／`_session_directory`）、v2 DB fixture。合併後：TUI 的列表／預覽／搜尋在 v2 的 store 上照常，舊 Session 不會被藏掉。
**PR 說明記一句**：sidecar `new` 模式 fallback 的讀取修包含這一步（報告 §H 末段）。

- [ ] 3.1 **先量（審查 M4；Open Q 原本的 3、4）**：隔離環境量「v2 升級有沒有把舊 session 遷進 `session_v2`」「舊表（`session` 等）還在不在」「同一顆 Session 在兩表的 id 是否相同」；記進 `docs/spike/opencode.md`。沒遷移時，fixture 與測試要涵蓋「只出現在舊表的 Session」
- [ ] 3.2 選表：`session_v2` 與所需欄位齊全就用它，否則讀 v1 的 `session`；**兩者都在時以 v2 為準，但只出現在 v1 表的 Session 仍要列出**（id 聯集、v2 的資料優先，M4）；都沒有 → `_warn_schema()` 一次＋空清單
- [ ] 3.3 預覽與搜尋改讀 `session_message`：欄位（`type`、`data` 形狀、排序用 `time_created` 或 `seq`）以 3.1 的真 DB fixture 定案；v1 的路徑照舊
- [ ] 3.4 `_session_directory()`：用選中的表；`row` 先設 None（表不存在、查不到、目錄不在都回 None），不再 `UnboundLocalError`
- [ ] 3.5 單元測試：v2 DB 的列表（跨專案、新到舊、目錄＋標題）、**id 聯集（同 id 用 v2 的列；只在舊表的 Session 也在清單）**、預覽（只算 user／assistant、截長度）、搜尋（命中、附件不算、正規化、同一個 Session 一次）；v1 DB 的既有測試全綠；認不得的結構只警告一次；`_session_directory` 的四種情形；`list_sessions` 的 memoisation 照舊
- [ ] 3.6 改壞驗證：表名改回 `session`；聯集改成只看 v2；預覽讀錯欄位；搜尋不比對；`_session_directory` 拿掉 `row = None`

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py`；不需要真 opencode。
**改壞驗證**：3.6 逐項記在 PR 說明。

## 4. summarize／delete 的 v2 行為（PR 4；第 1 步之後可與第 3 步並行）

**範圍**：`_model_of()` 與 summarize／delete 的 v2 行為驗證（server 上下文已在第 1 步）。合併後：寫要約、刪除與 merge 的收尾在 v2 上照常。

- [ ] 4.1 `_model_of()` 讀 v2 的模型（assistant 列的 `model.id`；形狀以 fixture 複驗）
- [ ] 4.2 summarize 的行為測試：argv（已在第 1 步加的 `--standalone`）、成功／空回覆／失敗／逾時；`_drop_summary_session` 失敗時保留記錄
- [ ] 4.3 刪除路徑的行為測試：`_discard`（匯入驗證失敗時的清理）、`_sweep_pending` 補刪、merge 流程的刪除警告（fake 的 `FAKE_OPENCODE_FAIL=delete` 對照）
- [ ] 4.4 真 opencode 的 summarize 整合測試（隔離 XDG＋空閒埠、fake upstream）：寫要約後那個 session 真的被刪掉
- [ ] 4.5 改壞驗證：`_model_of` 讀錯欄位；`_drop_summary_session` 失敗卻刪記錄；`_sweep_pending` 不刪

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py tests/unit/test_continue_headless.py tests/unit/test_cli.py`（受影響的）。
**改壞驗證**：4.5 逐項記在 PR 說明。

## 5. 驗收：隔離 XDG 的整條 v2 流程（PR 5）

**範圍**：整合測試與驗收紀錄（issue #51 的驗收條件）。合併後：一條測試證明整條路徑在真的 opencode 2.x 上會動，而且不碰使用者的 managed 服務。

- [ ] 5.1 整合測試（隔離 XDG＋**空閒埠的 managed 服務**＋fake upstream＋假 Drive 用 `tests/fakes/fake_rclone.py`、自編多輪含工具呼叫的 v2 session）：`agora import` → `continue --headless`（三行 stderr；`--sync-period` > 0，真的做一次定期寫回；SIGTERM 後溫和停下、寫回）→ `agora show`（閱讀版完整）→ `merge` 兩顆 → 接續 merged Session
- [ ] 5.2 非 headless 的路徑（審查 M3）：隔離環境＋運行中的 managed 服務（空閒埠），`session import --standalone` 之後用 `opencode --session <id>`（或 attach）確認看得到；另列手動驗收，替身（`opencode_noninteractive.py`）只證明參數
- [ ] 5.3 更新 `tests/integration/test_opencode_real.py` 與 `test_e2e_opencode.py` 的 v1 語法與格式假設（`session export／import`、扁平格式）；拔掉 1.11 的跳過探測；沒有 opencode 或模型不可用時照舊 skip（**驗 v2 的項目不用真模型**）
- [ ] 5.4 §G 三項複驗：工具呼叫的 parts、跨訊息 reference、import 必填欄位全集——結果（欄位清單、參照位置、fixture 名稱）記進 `docs/spike/opencode.md`；fixture 有漏就補
- [ ] 5.5 跑動前後確認預設埠 49374 的服務沒被動（只看在不在聽／PID／etime，不讀資料，H2）
- [ ] 5.6 改壞驗證：在副本把 v2 適配關掉（native 產 v1、turns 讀 v1、DB 讀 `session`）→ 5.1 必須紅；**「不帶 `--standalone`」這一項只對 fake 做**，真 opencode 不做（H2）；要的是紅不是 skip（用 fake upstream，M6）
- [ ] 5.7 驗收紀錄（PR 說明或 `docs/spike/opencode.md`）：指令與輸出形狀、三行 stderr、定期寫回、SIGTERM 後的寫回、清理（刪掉測試自己的 session id、暫存 XDG 全刪、不碰共用服務與真 store）

**測試**：`uv run pytest -q -m integration tests/integration/`（需真 opencode；模型相關的照舊可 skip，驗 v2 的不 skip）。
**改壞驗證**：5.6 照 H2 的規則記在 PR 說明。

## 6. 報告 §G「未測」項目的落點（對照）

- **工具呼叫的 parts**：1.2（fixture）＋1.8／1.12（閱讀版與 body 段落）＋2.3／2.7（native／append）＋5.4（整條流程）。
- **跨訊息 reference**：1.2（fixture 要有多輪＋工具的參照）＋1.7（reidentify）＋5.4（import→export round trip）。
- **import 必填欄位全集**：2.1（模板＋逐欄探測）＋2.7／2.8（fake 與真 import）＋5.4（記錄）。
- 其餘（互動模式 SQL、merge 的 sidecar HTTP 鏈、managed 服務的生產行為、非 headless TUI、真模型的格式細節）見 design 的同名表。

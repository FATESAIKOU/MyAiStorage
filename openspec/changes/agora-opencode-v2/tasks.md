## 1. 格式嗅探＋v2 fixture＋fake（一步一個 PR）

**範圍**：`src/agora/agents/opencode.py` 的讀取層與指令層、`tests/fakes/fake_opencode.py`、`tests/fixtures/opencode/` 的 v2 session fixture、`docs/spike/opencode.md`。合併後：adapter 認得出 v1／v2、對 v2 用對的指令匯出／匯入，`agora import` 的 header 正確；閱讀版與 native 還是舊的（第 2 步補），但不會更糟。

- [ ] 1.1 在隔離環境產生 v2 fixture：四個 XDG＋HOME 指到暫存目錄（`opencode debug paths` 確認）、工作目錄用暫存專案、自編多輪對話＋一次工具呼叫（自建 fake provider 或免費模型；第二輪用 `-s <id>` 讓跨訊息參照出現）；`opencode session export <id> --standalone` 匯出，去識別化（一般化路徑）後放 `tests/fixtures/opencode/oc-v2-*.json`
- [ ] 1.2 嗅探：`messages[0]` 有 `type` → v2、有 `info` → v1；`messages` 空／不存在時看 `info.location`（v2）／`info.directory`（v1）；兩者都不是 → `AgentError`（明確訊息）
- [ ] 1.3 指令與旗標：`_export_bytes` → `session export <id> --standalone`；`_import` → `session import <path> --standalone --directory <workdir>`（cwd 與 `PWD` 都給 workdir）；server 上下文集中一處組
- [ ] 1.4 `export()` 的欄位讀 v2：`dir` 讀 `info.location.directory`、`version` 沒有就是 `None`、`model` 讀最後一則 assistant 的 `model.id`、`created_at` 照舊
- [ ] 1.5 `reidentify()` 認 v2：依真 fixture 決定要改的結構位置（訊息 id、assistant 列的參照、`content` 內部的 id 若有）；fixture 上不存在的欄位一律不動；v1 行為不變
- [ ] 1.6 `fake_opencode.py`：新舊語法都收（`export` 與 `session export`、`import` 與 `session import`、`--standalone`、`--directory` 決定 session 目錄）；v2 session 以扁平存；`session import` 收到 v1 巢狀 payload MUST 以 `SchemaError` 拒絕（模擬 v2）
- [ ] 1.7 單元測試：嗅探四種情形（v1／v2／空＋info／認不得）；指令 argv 帶 `--standalone`／`--directory`（fake 的 `opencode-calls.log`）；v2 的 export 欄位；reidentify v2 round trip（id 全換、參照指對、其餘 byte-for-byte）；v1 的既有測試全綠
- [ ] 1.8 改壞驗證（在副本逐項改壞，確認對應測試變紅）：拿掉 `--standalone`／`--directory`；嗅探只認一版；export 讀 `info.directory`；reidentify 少改一個 fixture 上的參照
- [ ] 1.9 `docs/spike/opencode.md` 補 v2 一節：fixture 產生步驟（1.1 的做法）、v2 的指令與旗標、格式差異、觀察到的陷阱

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py`（＋受影響的 fake 相關測試）；不需要真 opencode。
**改壞驗證**：1.8 的每一項都在 PR 說明裡記一條（哪一項、改壞什麼、哪個測試紅）。

## 2. native()／append_native＋閱讀版（PR 2）

**範圍**：`src/agora/agents/opencode.py` 的寫入層與閱讀版、fake 的必填欄位驗證、真 opencode 的整合測試骨架。合併後：v2 的接續／fork／merge 後接續、閱讀版完整、舊 raw 明確拒絕。

- [ ] 2.1 必填欄位全集：拿 1.1 的 fixture 當模板，在隔離 XDG 逐欄餵 `session import` 看 `SchemaError: Missing key at [...]` 補齊（`info`、user 列、assistant 列的 `content`）；結果記進 `docs/spike/opencode.md`，`fake_opencode.py` 依同一份拒絕缺欄位的 payload
- [ ] 2.2 `native()` 產 v2：`info` 照模板（含 `location` 等）；訊息列照 v2 的 user／assistant 列，一則一行一個 text；不使用 synthetic／attachment；placeholder id 用 `ses`／`msg`／`prt` 前綴（以 fixture 複驗）
- [ ] 2.3 `append_native()` 產 v2：複製原 raw（`info.revert` 若存在照舊套用），接上新訊息列；原 raw 其餘部分 MUST byte-for-byte 不動
- [ ] 2.4 `turns()`／`_lines_of()` 讀 v2：user 的 `text`、assistant 的 `content`（text 一行、tool 一行 `[tool] …`）、`idle` 列略過、附件不算；不認得的型別照舊 `[skip <type>]`
- [ ] 2.5 舊 raw（v1）的同 agent 接續／fork：在開 agent 前以明確訊息拒絕（說明重新匯入的指令；agent session id 取自 payload 的 `info.id`）；跨 agent 接續照常
- [ ] 2.6 單元測試：`native → import（fake）→ export → turns` 的 round trip；閱讀版對 fixture 的段落數與工具行；`append_native` 保留工具呼叫與原 raw；回讀驗證（訊息數＋part 數）仍成立；v1 拒絕與跨 agent 的案例
- [ ] 2.7 整合測試（有真 opencode 才跑，隔離 XDG、免費模型不可用就 skip）：`native()` → 真的 `session import` → `run -s` 一輪 → `collect`，閱讀版含前文關鍵字
- [ ] 2.8 改壞驗證：native 少一個必填欄位（fake 與真 import 都要紅）；`idle` 當段落；工具行拿掉；`append_native` 改到原 raw 的其他欄位；拒絕路徑拿掉

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py`；`uv run pytest -q -m integration tests/integration/test_opencode_real.py`（可選，需真 opencode）。
**改壞驗證**：2.8 逐項記在 PR 說明。

## 3. 互動模式 DB（PR 3）

**範圍**：`src/agora/agents/opencode.py` 的 DB 讀取（`list_sessions`／`last_message`／`search_text`／`_session_directory`）、v2 DB fixture。合併後：TUI 的列表／預覽／搜尋在 v2 的 store 上照常。

- [ ] 3.1 v2 DB fixture：在隔離 XDG 用 1.1 的 session 產出 DB，記下 `session_v2`／`session_message` 的 schema 與需要的列（文字 dump，放 `tests/fixtures/opencode/`）；記進 `docs/spike/opencode.md`
- [ ] 3.2 選表：`session_v2` 與所需欄位齊全就用它，否則退回 v1 的 `session`；都沒有 → `_warn_schema()` 一次＋空清單
- [ ] 3.3 預覽與搜尋改讀 `session_message`：欄位（`type`、`data` 形狀、排序用 `time_created` 或 `seq`）以 3.1 的 fixture 定案；v1 的路徑照舊
- [ ] 3.4 `_session_directory()`：用選中的表；`row` 先設 None（表不存在、查不到、目錄不在都回 None），不再 `UnboundLocalError`
- [ ] 3.5 單元測試：v2 DB 的列表（跨專案、新到舊、目錄＋標題）、預覽（只算 user／assistant、截長度）、搜尋（命中、附件不算、正規化、同一個 Session 一次）；v1 DB 的既有測試全綠；認不得的結構只警告一次；`_session_directory` 的四種情形；`list_sessions` 的 memoisation 照舊
- [ ] 3.6 改壞驗證：表名改回 `session`；預覽讀錯欄位；搜尋不比對；`_session_directory` 拿掉 `row = None`

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py`；不需要真 opencode。
**改壞驗證**：3.6 逐項記在 PR 說明。

## 4. summarize／delete 的 server 上下文（PR 4）

**範圍**：`summarize()` 的 run 旗標、`_delete()` 與所有刪除路徑、`_model_of()` 的 v2 讀法。合併後：寫要約、刪除與 merge 的收尾在 v2 上照常。

- [ ] 4.1 `summarize()` 的 argv 加 `--standalone`（插在 `run` 後；「訊息在 `-f` 前」的順序照舊）
- [ ] 4.2 `_delete()` 加 `--standalone`：`_discard`、`_drop_summary_session`、`_sweep_pending` 全部走它
- [ ] 4.3 `_model_of()` 確認 v2 的模型讀法（assistant 的 `model.id`）與 export 的 server 上下文
- [ ] 4.4 單元測試：argv 斷言（summarize 與 delete）；summarize 的成功／空回覆／失敗／逾時；pending 補刪；`_drop_summary_session` 失敗時保留記錄；merge 流程的刪除警告（fake 的 `FAKE_OPENCODE_FAIL=delete` 對照）
- [ ] 4.5 改壞驗證：兩個旗標各拿掉一次；`_model_of` 讀錯欄位；`_drop_summary_session` 失敗卻刪記錄

**測試**：`uv run pytest -q tests/unit/test_agent_opencode.py tests/unit/test_continue_headless.py tests/unit/test_cli.py`（受影響的）。
**改壞驗證**：4.5 逐項記在 PR 說明。

## 5. 驗收：隔離 XDG 的整條 v2 流程（PR 5）

**範圍**：整合測試與驗收紀錄（issue #51 的驗收條件）。合併後：一條測試證明整條路徑在真的 opencode 2.x 上會動。

- [ ] 5.1 新增整合測試（`tests/integration/`；隔離 XDG＋HOME、假 Drive 用 `tests/fakes/fake_rclone.py`、自編多輪含工具呼叫的 v2 session）：`agora import` → `continue --headless`（三行 stderr；SIGTERM 後溫和停下、寫回）→ `agora show`（閱讀版完整）→ `merge` 兩顆 → 接續 merged Session
- [ ] 5.2 更新 `tests/integration/test_opencode_real.py` 與 `test_e2e_opencode.py` 的 v1 語法與格式假設（`session export／import`、扁平格式）；`tests/fakes/opencode_noninteractive.py` 跟著改；沒有 opencode 或模型不可用時照舊 skip
- [ ] 5.3 §G 三項複驗：工具呼叫的 parts、跨訊息 reference、import 必填欄位全集——結果（欄位清單、參照位置、fixture 名稱）記進 `docs/spike/opencode.md`；fixture 有漏就補
- [ ] 5.4 驗收紀錄（PR 說明或 `docs/spike/opencode.md`）：指令與輸出形狀、三行 stderr、SIGTERM 後的寫回、清理（刪掉測試自己的 session id、暫存 XDG 全刪、不碰共用服務與真 store）
- [ ] 5.5 改壞驗證：在副本把 v2 適配關掉（native 產 v1、turns 讀 v1、DB 讀 `session`、指令不帶 `--standalone`）→ 5.1 必須紅；確認驗收真的在驗 v2，不是只驗流程

**測試**：`uv run pytest -q -m integration tests/integration/`（需真 opencode 與模型；沒有的機器上 skip）。
**改壞驗證**：5.5 的「關掉 v2 適配」要看到 5.1 紅，記在 PR 說明。

## 6. 報告 §G「未測」項目的落點（對照）

- **工具呼叫的 parts**：1.1（fixture）＋2.4／2.6（閱讀版與 native／append）＋5.3（整條流程）。
- **跨訊息 reference**：1.1（fixture 要有多輪＋工具的參照）＋1.5（reidentify）＋5.3（import→export round trip）。
- **import 必填欄位全集**：2.1（模板＋逐欄探測）＋2.6／2.7（fake 與真 import）＋5.3（記錄）。
- 其餘（互動模式 SQL、merge 的 sidecar HTTP 鏈、managed 服務的生產行為、真模型的格式細節）見 design 的同名表。

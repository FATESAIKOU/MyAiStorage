# v5 影響分析（Angareion＋sidecar）

依據：本人 2026-10-12 中午的 `design-v5-changes.md`（唯一依據）與本人同日對 v5 疑點的裁定（`v5-decisions.md`）。程式證據用 git grep 實際找：
sidecar 側讀分支 `sidecar-v0-agy4`（`git -C <本 worktree> show sidecar-v0-agy4:<路徑>`），
Angareion 側讀本 worktree 群組的 A worktree（`angareion-v5`，即 angareion-v0 程式）。
本文件只分析，不改程式。第 3 節的疑點已逐條標出結果（2026-10-12 更新）。

## 1. 受影響的程式與測試

### 1.1 Angareion（A worktree：`src/angareion/`、`tests/unit/test_angareion_*.py`）

- `src/angareion/cli.py`（`--member` 2 處：`build_parser` 的 `p_create.add_argument("--member")`、`cmd_channel_create` 的解析迴圈；`--to` 在 parser `required=True` 與 `cmd_send` 必解析；`_format_message` 的 `<repo×role>` 顯示）
  → 拿掉 `--member`（未知參數自然 exit 1）；`--to` 改選填（不給就 `Message.to=None`）；顯示改 agent 名；`channel create` 留、改三步流程（輸出只印名）；新增 `channel delete`；`--backend`（多個 backend 時必給；見 angareion-v5 design §5～§7）。
- `src/angareion/messages.py`（`validate_address` 認 `{repo, role[, name]}`／`{group}`；`Message.to` 必填 dict；`serialize_message`／`parse_message` 的 front matter `to` 必填）
  → 位址只認 agent 名字串（`group=`、`repo=` 形狀都算格式錯誤）；`to` 選填（front matter 省略）；`urgency`／`id`／`at` 不動。
- `src/angareion/backend.py`（`ChannelBackend.create_channel(name, members)`；`Channel.members`；`FakeBackend` 的 members 儲存；`address_matches_identity` 逐欄比對）
  → 簽名去 members；`Channel` 去 members 欄；`address_matches_identity` 改字串相等；實體要能開（create）與關（delete，不刪）；留言列表的多頁邏輯不動。
- `src/angareion/github.py`（`serialize_channel_body`／`parse_channel_members`；`create_channel` 寫 members；`from_config` 讀 `channel_repo`；`channel_repo` 共 6 處）
  → channel 解析改走兩個設定檔（名→backend＋issue）——同名標題取最舊的掃描整套拿掉（MUST NOT 用標題掃描）；issue 本文不再寫／讀 members（舊殘留忽略）；`from_config` 改讀 backend 設定檔；多一個關實體（`state=closed`，404 視同已關）的動作。
- `src/angareion/config.py`（`Config.channel_repo`、`identity` dict、`validate`、`load_config`；`channel_repo` 共 6 處）
  → `identity` 改 agent 名字串；`channel_repo` 換成 `config_dir`／`registry_repo`／`config_pull_interval`；新增兩個 JSON 的讀取與驗證、lazy pull（`--ff-only`、flock、逾時、失敗沿用舊複本＋警告）。
- `src/angareion/client.py`（`create_channel(name, members)`；`send(channel, message)` 不動簽名）
  → `create_channel` 去 members；`send` 透傳無 `to` 訊息；註冊／註銷請求的送出（方式依 design §10 的裁定，待本人確認後才開工）。
- `src/angareion/state.py`（`identity_slug` 用 repo／role 組檔名）
  → 改用 agent 名組檔名；舊游標檔自然廢棄（新身分從頭讀一次）。
- repo 資產（本 change 規劃、實作放 MyAiStorage repo）：`config/angareion/channels.json`（workflow 寫）與 `backends.json`（PR）、`.github/workflows/register-channel.yml`／`unregister-channel.yml`＋更新腳本（concurrency、sha 樂觀鎖、同名不覆蓋、冪等；用可注入的檔案 API 單元測試）。
- tests：`test_angareion_cli.py`、`test_angareion_backend.py`、`test_angareion_github.py`（`--member`／members 相關）、`test_angareion_config.py`＋`test_angareion_token.py`（`channel_repo` 相關）、`test_angareion_messages.py`（位址形狀）、`test_angareion_state.py`＋`test_angareion_library.py`（`--to`／`Target` 相關）、`test_angareion_skill.py`（SKILL 改寫後同步）；新增：廣播、channel 英文名驗證、設定檔缺失／壞檔、pull（含失敗沿用）、create／delete 的三步與同名、註冊／註銷 workflow 腳本。
- 文件（本期不改，只列點）：`docs/angareion.md`（五個指令的指令表、位址節、訊息範例、游標檔名、設定節、`--member` 字樣）、`skills/angareion/SKILL.md`（同上；見 angareion-v5 design §13）。

### 1.2 sidecar（分支 `sidecar-v0-agy4`：`src/sidecar/`、`tests/unit/test_sidecar_*.py`）

- `open` 的 session 參數（`core.py: parse_session`、`open`、`_swap_new_mode` 等 new／fork／merge 路；`protocols.py: SESSION_MODES`；`http.py` 只透傳）：`session` 改為選填的 agora id（接續）／不給（新開）；`parse_session` 只認 id 或省略；fork／merge 的入口拿掉（core 的對外路徑收斂；store 的能力保留；`merge_not_allowed_in_job` 與 job 的 merge 規則刪除）。測試：`test_sidecar_core.py`、`test_sidecar_http.py`、`test_sidecar_jobs.py`、`test_sidecar_new_mode.py` 裡帶 `mode` 的案例（`merge` 相關見 `git grep merge` 的 4 個測試檔）。
- agent id 共通命名（`protocols.py: valid_id`）：**只是慣例，sidecar 不驗證格式**（`valid_id` 的基本字元／長度檢查不變；審查 M7 定案）；不做身分認證。
- 拿掉 `env_for`（`protocols.py: AgentDriver.env_for` 協定＋docstring、`core.py` 的 open／換把／merge 三處呼叫、`drivers/claude_code.py`、`drivers/opencode.py`、`drivers/common.py: pick_model` 搬家、`subproc.py` docstring；測試：`test_sidecar_driver_{claude,opencode,masking}.py`、`test_sidecar_core.py`、`_sidecar_fakes.py` 的 `env_for`、`tests/integration/test_sidecar_driver_env_real.py`）：`launch(session, workdir, credentials, env)` 直接拿憑證與環境變數，兩 driver 把模型選擇、`no_model`、開關變數、遮罩規則搬進 `launch`（opencode 的部分併入 v2 改寫）。
- **opencode driver 改寫成 v2**（`drivers/opencode.py`；本人 2026-10-12 裁定）：先做 v2 server API 的 spike（tasks 1.4；Mac 上已是 2.0.26，用 `--standalone` 的私有 server，不干擾共用的背景服務），再照 v2 重寫開 server／送訊息／中斷／狀態來源／額度偵測／session id 的取得，並在 v2 重驗 `launch` 的 key 與 baseURL（v1 的 `OPENCODE_CONFIG_CONTENT` 形狀作廢，沿用與否由 spike 定）；測試：`test_sidecar_driver_opencode.py` 與相關（形狀以 spike 為準）。
- `poke` 立刻讀（`core.py: poke` 已是 interrupt-then-send）：行為不動，補測試鎖住。
- 「現在＋1 小時」（`core.py: _swap` 的 `cooldown` 呼叫處、`_wait_quota`／`_earliest_lift`）：`resets_at` 缺失時報 now+1h；登錄只延長（LLMGateway 取較晚的 `until`）、agent 已停下後不會再自動刷新（審查 M1）；測試 `test_sidecar_{core,jobs,new_mode}.py` 的相關案例。
- `stop` 語意（`drivers/*/stop`、`subproc.terminate_process`、`core.py` close／shutdown 路）：close 三段＝等閒（`close_idle_wait`）→ SIGTERM → SIGKILL；shutdown 跳過等閒、換把不等閒；強制＝立刻；退出結束整個 agent 程序；agora 的 SIGTERM 轉送機制不動。
- `cooldown` 只送 `until`（`protocols.py: WINDOWS`、`Subscription.cooldown` 形狀、`SubscriptionSource.cooldown` 簽名、`sources.py` 兩實作＋`invalid_window`、`core.py` 的 swap／`_earliest_lift`／`_wait_quota`、兩 driver 的 window 對應；測試：`test_sidecar_{core,jobs,new_mode,sources}.py` 帶 `"5h"` 的案例、`test_sidecar_driver_{claude,opencode}.py` 的 window 案例）：`cooldown` 形狀改單一 `until`，`_earliest_lift` 取最小 `until`。
- curl 來源（`sources.py` 整個 `LLMGatewaySource`＋`_import_lgw`＋connect 流程、`wiring.py`、`config.py: gateway_connect_file`、`pyproject.toml` 的 gateway extra 註解；測試：`test_sidecar_sources.py`、`test_sidecar_cli.py` 的 wiring 案例、`test_sidecar_lgw_loopback.py` 改打本地 loopback、`_sidecar_fakes.py` 的 lgw doubles）：重寫為直連 `base_url`（標準函式庫），`FileSubscriptionSource` 不動。
- `/jobs`：程式完全不動（本人 2026-10-12 定案保留、不加功能）。
- 文件：`docs/sidecar.md`（session 參數、命名、`until`、curl 來源；kill 公式不變）。

## 2. 估工（沿用「每項一個 commit」，規模：小＝半天內／中＝1～2 天／大＝3 天以上含測試；2026-10-12 照裁定修正）

Angareion（合計約 9～11 commits）：
- 設定檔（兩個 JSON＋schema＋範例）＋本地讀取＋lazy pull：2 commits，中。
- 位址／訊息格式（messages＋cli＋state＋client）：2 commits，中。
- 後端（protocol＋GitHub＋fake；設定檔解析、實體開／關、去 members）：1～2 commits，大。
- `channel create` 三步＋`channel delete`＋註冊／註銷請求的送出：2 commits，大（請求送出待 design §10 的裁定）。
- register／unregister workflow＋更新腳本（concurrency、sha 樂觀鎖、同名不覆蓋、冪等）：1～2 commits，大。
- docs／SKILL 改寫：1 commit，小。
- 合計：大 ×3、中 ×2、小 ×1。

sidecar（合計約 9～11 commits；worker 映像不算在內）：
- session 參數＋命名＋入口收斂：1～2 commits，中（open 只收 id 或不給；fork／merge 的入口拿掉＝刪掉舊對外路徑與 `merge_not_allowed_in_job`；store 的能力保留，不另加工作）。
- 拿掉 `env_for`：2 commits，大（兩 driver＋core＋全部 driver 測試；opencode 的部分併入 v2 改寫）。
- now＋1h＋`until`（core＋sources＋drivers）：2 commits，大。
- curl 來源＋config／wiring／extra：1～2 commits，大。
- opencode v2：spike（tasks 1.4）1 commit，小～中；driver 改寫（tasks 3.2）1 commit，大（形狀全依 spike）。
- stop 語意＋poke 測試鎖定＋docs：1 commit，中。
- `/jobs`：0（定案保留、程式完全不動）。
- worker 映像：opencode 升 v2 由 MyLinuxPool 開單（tasks 5.8），不算這裡的 commit。
- 合計：大 ×4、中 ×2～3。

## 3. 疑點與結果（2026-10-12 更新：逐條標出）

1. **`channel create` 留不留**：**已裁定（本人 2026-10-12，`v5-decisions.md`）**——留，channel 可以自由建立、不走 PR：`channel create` 改三步（backend 開實體 → 送註冊 → 本體的 `register-channel` workflow 直接更新 main）；新增 `channel delete`（關實體不刪＋`unregister-channel`）。同名 exit 1；create 不等落地。另有一題待本人確認：觸發 workflow 的方式與 token 範圍（A／B／C；建議 C＝issue 事件，見 angareion-v5 design §10）。
2. **設定檔格式與位置**：**已裁定（本人 2026-10-12）**——channel 對應持久化在 MyAiStorage：`config/angareion/channels.json`（`version`＋`channels`，每筆 `{backend, issue}`；只有 workflow 寫）；backend 設定 `config/angareion/backends.json`（`version`＋`backends`，`{type, repo}`）走 PR（d 等級）；angareion 只讀本機複本。
3. **讀取端多久 pull、pull 失敗怎麼辦**：**已裁定（本人 2026-10-12；設計要處理）**——定案見 angareion-v5 design §11：lazy pull（`config_pull_interval` 預設 900 秒、`--ff-only`、鎖檔、逾時；`wait` 輪詢中不 pull）；失敗沿用目前複本＋stderr 警告（exit code 不變）；複本缺失／壞 JSON 才 exit 2；外部 cron／systemd 也可以另外拉。
4. **`--to` 選填時 wait／read 怎麼過濾**：**已裁定（本人 2026-10-12）**——無 `to`＝廣播，算進「寄給我的」：不給 `--channel` 的 `wait`／`read` 收「`to` 是自己或沒有 `to`」的訊息。
5. **「現在＋1 小時」由誰報**：**已定案（v5 設計；design D4 已照建議採 core；審查 M1 補充）**——driver 只報測到的 `resets_at`（拿不到就不帶），core 在 `cooldown` 補「現在＋1 小時」；登錄只會延長、沒有「下次刷新更正」（額度用完後 agent 已停）。
6. **不用 window 之後 claude 的 5h／1w 資訊要不要丟**：**已定案（v5 設計；design D4／D6）**——不丟：driver 照常解析（log 與內部判斷用），只是不進 `cooldown`（core 只送 `until`）。
7. **curl 本機 port 的位址從哪裡設定**：**已定案（v5 規格）**——`subscription_source` 用 `{"kind": "llmgateway", "base_url": …}`（無預設值、必填）；worker 由 MyLinuxPool 的 capability／supervisor 把實際 port 寫進設定，Mac／手機用既有轉發。spec、design 與 tasks 5.5 已照此寫（design D7 的「待 PM 確認」字樣已同步拿掉）。
8. **／jobs 待定期間程式怎麼辦**：**已裁定（本人 2026-10-12）**——`/jobs` 保留（at／every／until＋`GET`／`POST`／`DELETE`），越簡單越好、不加功能；程式完全不動（「若拿掉」案不成立）。
9. **fork／merge／new 入口去留**：**已裁定（本人 2026-10-12）**——`open` 只收 agora id（接續）或不給（新開）；fork／merge 的入口拿掉（由 PMO／PM 先在 agora 做好 Session 再交 id）；`SessionStore` 的 fork／merge 能力保留、本期沒有對外入口。估工不另加（入口拿掉＝刪舊對外路徑；`new` 已含在 D5 的工作裡）。

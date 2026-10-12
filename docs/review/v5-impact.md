# v5 影響分析（Angareion＋sidecar）

依據：本人 2026-10-12 中午的 `design-v5-changes.md`（唯一依據）。程式證據用 git grep 實際找：
sidecar 側讀分支 `sidecar-v0-agy4`（`git -C <本 worktree> show sidecar-v0-agy4:<路徑>`），
Angareion 側讀本 worktree 群組的 A worktree（`angareion-v5`，即 angareion-v0 程式）。
本文件只分析，不改程式。

## 1. 受影響的程式與測試

### 1.1 Angareion（A worktree：`src/angareion/`、`tests/unit/test_angareion_*.py`）

- `src/angareion/cli.py`（`--member` 2 處：`build_parser` 的 `p_create.add_argument("--member")`、`cmd_channel_create` 的解析迴圈；`--to` 在 parser `required=True` 與 `cmd_send` 必解析；`_format_message` 的 `<repo×role>` 顯示）
  → 拿掉 `--member`（未知參數自然 exit 1）；`--to` 改選填（不給就 `Message.to=None`）；顯示改 agent 名；`channel create` 照本人決定留（輸出只印名）或刪。
- `src/angareion/messages.py`（`validate_address` 認 `{repo, role[, name]}`／`{group}`；`Message.to` 必填 dict；`serialize_message`／`parse_message` 的 front matter `to` 必填）
  → 位址只認 agent 名字串（`group=`、`repo=` 形狀都算格式錯誤）；`to` 選填（front matter 省略）；`urgency`／`id`／`at` 不動。
- `src/angareion/backend.py`（`ChannelBackend.create_channel(name, members)`；`Channel.members`；`FakeBackend` 的 members 儲存；`address_matches_identity` 逐欄比對）
  → 簽名去 members；`Channel` 去 members 欄；`address_matches_identity` 改字串相等；同名取最舊／分頁邏輯不動。
- `src/angareion/github.py`（`serialize_channel_body`／`parse_channel_members`；`create_channel` 寫 members；`from_config` 讀 `channel_repo`；`channel_repo` 共 6 處）
  → channel 解析改走 channel 設定檔（名→backend＋issue）；issue 本文不再寫／讀 members（舊殘留忽略）；`from_config` 改讀 backend 設定檔。
- `src/angareion/config.py`（`Config.channel_repo`、`identity` dict、`validate`、`load_config`；`channel_repo` 共 6 處）
  → `identity` 改 agent 名字串；`channel_repo` 改為設定檔複本位置（clone 路徑）；新增兩個 JSON 的讀取與驗證。
- `src/angareion/client.py`（`create_channel(name, members)`；`send(channel, message)` 不動簽名）
  → `create_channel` 去 members；`send` 透傳無 `to` 訊息。
- `src/angareion/state.py`（`identity_slug` 用 repo／role 組檔名）
  → 改用 agent 名組檔名；舊游標檔自然廢棄（新身分從頭讀一次）。
- tests：`test_angareion_cli.py`、`test_angareion_backend.py`、`test_angareion_github.py`（`--member`／members 相關）、`test_angareion_config.py`＋`test_angareion_token.py`（`channel_repo` 相關）、`test_angareion_messages.py`（位址形狀）、`test_angareion_state.py`＋`test_angareion_library.py`（`--to`／`Target` 相關）、`test_angareion_skill.py`（SKILL 改寫後同步）；新增：廣播、channel 英文名驗證、設定檔缺失／壞檔、PR→pull 流程。
- 文件（本期不改，只列點）：`docs/angareion.md`（指令表、位址節、訊息範例、游標檔名、設定節、`--member` 字樣）、`skills/angareion/SKILL.md`（同上；見 angareion-v5 design §7）。

### 1.2 sidecar（分支 `sidecar-v0-agy4`：`src/sidecar/`、`tests/unit/test_sidecar_*.py`）

- `open` 的 session 參數（`core.py: parse_session`、`open`、`_swap_new_mode` 等 new／fork／merge 路；`protocols.py: SESSION_MODES`；`http.py` 只透傳）：`session` 改為選填的 agora id（接續）／不給（新開）；`parse_session` 重寫；`merge_not_allowed_in_job` 的檢查要跟著改（`session` 不再是 dict）。測試：`test_sidecar_core.py`、`test_sidecar_http.py`、`test_sidecar_jobs.py`、`test_sidecar_new_mode.py` 裡帶 `mode` 的案例（`merge` 相關見 `git grep merge` 的 4 個測試檔）。
- agent id 共通命名（`protocols.py: valid_id`）：命名規則寫進驗證（`<>-<>[-<>]` 形狀仍在既有字元集內，_qty_主要是規則文字＋測試）；不做身分認證。
- 拿掉 `env_for`（`protocols.py: AgentDriver.env_for` 協定＋docstring、`core.py` 的 open／換把／merge 三處呼叫、`drivers/claude_code.py`、`drivers/opencode.py`、`drivers/common.py: pick_model` 搬家、`subproc.py` docstring；測試：`test_sidecar_driver_{claude,opencode,masking}.py`、`test_sidecar_core.py`、`_sidecar_fakes.py` 的 `env_for`、`tests/integration/test_sidecar_driver_env_real.py`）：`launch(session, workdir, credentials, env)` 直接拿憑證與環境變數，兩 driver 把模型選擇、`no_model`、開關變數、遮罩規則搬進 `launch`。
- `poke` 立刻讀（`core.py: poke` 已是 interrupt-then-send）：行為不動，補測試鎖住。
- 「現在＋1 小時」（`core.py: _swap` 的 `cooldown` 呼叫處、`_wait_quota`／`_earliest_lift`）：`resets_at` 缺失時報 now+1h，下次刷新更正；測試 `test_sidecar_{core,jobs,new_mode}.py` 的相關案例。
- `stop` 語意（`drivers/*/stop`、`subproc.terminate_process`、`core.py` close／shutdown 路）：溫和＝等閒才退、強制＝立刻、退出結束整個 agent 程序；agora 的 SIGTERM 轉送機制不動。
- `cooldown` 只送 `until`（`protocols.py: WINDOWS`、`Subscription.cooldown` 形狀、`SubscriptionSource.cooldown` 簽名、`sources.py` 兩實作＋`invalid_window`、`core.py` 的 swap／`_earliest_lift`／`_wait_quota`、兩 driver 的 window 對應；測試：`test_sidecar_{core,jobs,new_mode,sources}.py` 帶 `"5h"` 的案例、`test_sidecar_driver_{claude,opencode}.py` 的 window 案例）：`cooldown` 形狀改單一 `until`，`_earliest_lift` 取最小 `until`。
- curl 來源（`sources.py` 整個 `LLMGatewaySource`＋`_import_lgw`＋connect 流程、`wiring.py`、`config.py: gateway_connect_file`、`pyproject.toml` 的 gateway extra 註解；測試：`test_sidecar_sources.py`、`test_sidecar_cli.py` 的 wiring 案例、`test_sidecar_lgw_loopback.py` 改打本地 loopback、`_sidecar_fakes.py` 的 lgw doubles）：重寫為直連 `base_url`（標準函式庫），`FileSubscriptionSource` 不動。
- `/jobs`：程式完全不動（design 已標待本人決定）。
- 文件：`docs/sidecar.md`（session 參數、命名、`until`、curl 來源；kill 公式不變）。

## 2. 估工（沿用「每項一個 commit」，規模：小＝半天內／中＝1～2 天／大＝3 天以上含測試）

Angareion（合計約 6 commits）：
- 設定檔讀取＋schema＋範例：1 commit，中。
- 位址／訊息格式（messages＋cli＋state＋client）：2 commits，中。
- 後端（protocol＋GitHub＋fake）：1～2 commits，大。
- `channel create` 留刪＋測試全改：1 commit，中（刪案另加小半個）。
- docs／SKILL 改寫：1 commit，小。
- 合計：中 ×3、大 ×1、小 ×2。

sidecar（合計約 8 commits）：
- session 參數＋命名：1～2 commits，中～大（fork／merge／new 去留若要刪，另加大）。
- 拿掉 `env_for`：2 commits，大（兩 driver＋core＋全部 driver 測試）。
- now＋1h＋`until`（core＋sources＋drivers）：2 commits，大。
- curl 來源＋config／wiring／extra：1～2 commits，大。
- stop 語意＋poke 測試鎖定＋docs：1 commit，中。
- `/jobs`：0（維持現狀）。
- 合計：大 ×3、中 ×2、小 ×1（不含 fork／merge／new 存廢的額外工作）。

## 3. 疑點（附建議）

1. **`channel create` 留不留**（本人決定）：建議先留（功能限縮為「確保 backend 實體存在」，不碰設定檔），等第一個設定檔 PR 流程跑順再刪；spec 已暫維持現狀，tasks 3.3 兩案各留一測。
2. **設定檔格式與位置**：建議 `config/angareion/channels.json`＋`config/angareion/backends.json`，JSON 帶 `version`（channel 條目 `{backend, issue}`，backend 條目 `{type, repo}`）；替代：repo 根檔案。請本人定。
3. **讀取端多久 pull、pull 失敗怎麼辦**：建議外部排程（cron／systemd）至少每小時 pull，失敗沿用上次可用複本並告警；angareion 只讀本地，缺失／壞 JSON 才 exit 2。請 PM 確認排程落在哪。
4. **`--to` 選填時 wait／read 怎麼過濾**：建議無 `to`＝廣播，算進「寄給我的」（已寫進 spec）；替代：廣播只用 `--channel` 看。請本人確認。
5. **「現在＋1 小時」由誰報（driver 還是 core）**：建議 core（driver 只報測到的 `resets_at`，拿不到就不帶；core 補 now+1h，下次刷新有時間再更正）。理由：重試語意統一，driver 保持誠實。
6. **不用 window 之後 claude 的 5h／1w 資訊要不要丟**：建議不丟（driver 照常解析、記 log、內部判斷用，只是不送進 `cooldown`）。
7. **curl 本機 port 的位址從哪裡設定**：建議 `subscription_source` 加 `base_url`（`{"kind": "llmgateway", "base_url": …}`），無預設值、必填；worker 由 capability／supervisor 把實際 port 寫進設定，Mac／手機用既有轉發。請 PM 確認欄位名。
8. **／jobs 待定期間程式怎麼辦**：建議完全不動（design 已標「待本人決定」）；若決定拿掉，另開小 change（刪 http／core／jobs 引用＋docs），估 1～2 commits、小。
9. **fork／merge／new 入口去留**（v5 只說 session＝id，沒說三種開法）：建議 open 只收 id（接續）／不給（新開走 import 路），store 的 fork／merge 能力保留但失去入口，等本人確認去留；若全刪，sidecar 估工另加約 2 commits（大）、agora 側不受影響。請本人決定。

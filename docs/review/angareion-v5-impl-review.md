# Angareion v5 實作的非作者審查（PR #42）

- 對象：`angareion-v5` worktree，`git diff 41b4f50..angareion-v5`（實作 commit `3ae79af..6b0cb5a`；HEAD `6b0cb5a`）。
- 設計：`openspec/changes/angareion-v5/`（`design.md`、兩個 spec、已全勾的 `tasks.md` 1.1–4.2）。
- 改壞紀錄：`docs/review/angareion-v5-mutation-checks.md`（作者自做，12 項全紅）。
- 審查者：非作者（沒用過作者那四個模型）。只讀程式、假資料與文件；未讀 `~/.config`、未讀憑證、未連網、未碰真的 GitHub；未改任何程式。
- 方法：逐條比對介面與設計；把每個 spec Scenario 對到測試名；workflow 六項逐項看碼；token 與私密掃一遍；在 `git archive HEAD` 解到 `/tmp/angareion-mut` 的副本裡自己挑 5 處改壞並跑測試（不只信作者的紀錄）。

## 結論

**可以合併。** 沒有 High、沒有 Medium。對外介面與已核准的設計逐項一致（介面無變動，不需要等本人）；每個 spec Scenario 都有測試；我的 5 處改壞全紅；workflow 六項全對；安全掃過。剩下 5 項 Low（死掉的 env var、排序假設、delete 無重試、docstring 小錯、fake 的 repo 忽略），都不擋合併。合併前請 PM 確認 tasks 5.1–5.3 的三個部署項（token 加選、分支保護 bypass、註冊簿 issue＋`ANGAREION_REGISTRY_ISSUE` 變數）有人認領——那是環境，不是程式。

## 1. 對外介面 vs 已核准的設計：一致，無差異

| 設計條目 | 實作 | 結果 |
|---|---|---|
| 五個介面 `channel create/delete`、`send`、`wait`、`read`；`--member` 拿掉（未知參數 exit 1） | `src/angareion/cli.py:355-394`（`build_parser`：create 只有 `name`＋`--backend`；無 `--member`）；`test_channel_create_no_longer_takes_member` 綠 | ✅ |
| `send --channel` 必填、`--to` 選填（不給＝廣播，front matter 省略 `to`） | `cli.py:373-375`、`messages.py:231-232`（`if msg.to is not None` 才寫）；`test_send_to_is_optional_in_the_parser`、`test_broadcast_omits_to_and_parses_back_as_none` 綠 | ✅ |
| `wait`／`read --channel` 選填（不給＝寄給我的，含廣播） | `cli.py:385-393`、`backend.py:232-236`（`recipient_matches`）；to-me 單 channel 一個游標（`client.py:142-144`） | ✅ |
| exit code 0／1／2／3；stdout 只放結果（create／delete 印名、`send` 印 id）；錯誤與說明走 stderr | `cli.py:54-57`、`cmd_channel_create:224`、`cmd_channel_delete:261`、`cmd_send:297-298`；`test_send_prints_only_the_message_id` 等 | ✅ |
| channel 名 `^[A-Za-z0-9_-]+$`，不合 exit 1 | `messages.py:28`（`CHANNEL_NAME_RE`）、`cli.py:172-176,238-241,279` | ✅ |
| agent 名文法（含 `-` 的 repo 段）＋驗證與解析兩步 | `messages.py:27`（`AGENT_NAME_RE`）、`parse_identity:74-94`；實測 stays 綠（接受 4／拒絕 5 见第三輪報告） | ✅ |
| create 三步：開實體→用戶端驗（存在且標題相符，否則 exit 2 不留言）→送註冊（重試 2 次共 3 次）→輪詢落地（上限 2 分鐘，逾時提示 exit 0）；同名 exit 1；`--backend` 多 backend 必給 | `cli.py:194-225`（`create_channel`→`verify_channel`→`post_with_retries`→`poll_for_channel`）；`channel_config.py:205-218`（`timeout` 預設 `LANDING_POLL_TIMEOUT_SECONDS = 120.0`）；delete 不等待（`cli.py:228-262`） | ✅ |
| delete：查無 exit 1→關實體（404 視同已關）→送註銷；關失敗 exit 2 不送註銷；重跑安全 | `cli.py:243-259`、`github.py:192-203`（404 return）、`backend.py:349-358`（fake 同理） | ✅ |
| 設定檔格式 `channels.json`／`backends.json`（`version`＋對應表；未知 type exit 2） | `channel_config.py:261-307`；空範例檔與 `config/angareion/README.md`；版本不符 exit 2（`_load_json:254-258`） | ✅ |
| 註冊簿留言一行 JSON（5 欄、無多餘欄、`schema: 1`、issue 正整數） | `registry.py:45-54`（`action_comment`）、`parse_comment:82-122` | ✅ |
| 讀取：`git fetch`＋`git show origin/main:`、不依賴工作樹；`FETCH_HEAD` mtime 新鮮度；`interval` 預設 900、0＝只關週期拉；flock；20 秒逾時；失敗沿用＋stderr 警告；`wait` 輪詢不 fetch；查無先拉（30 秒最短間隔） | `channel_config.py:168-218`（`GIT_TIMEOUT_SECONDS = 20.0`、`MIN_EVENT_FETCH_INTERVAL_SECONDS = 30.0`）；匿名 HTTPS（`design §10/§11`，clone 側無 token 邏輯可寫死——實作面是「不帶 token」，見 §4） | ✅ |
| `channels.json` 是機器產物、人工改會被還原 | `registry_rebuild.py:195-208`（整檔重建寫回）；spec＋docs＋skill 三處一致 | ✅ |

失敗訊息抽查（設計 §5：只說 channel 沒建立／沒刪掉，不吐後端細節）：開實體失敗→`Error: channel 'X' was not created.`（`cli.py:198`）；驗不過→`could not be verified in the backend`（`cli.py:205`，`backend` 是 angareion 自己的詞，非 repo／issue 概念，可接受）；註冊送不出→`was not created: the registration could not be posted.`（`cli.py:214`）；關不掉→`was not deleted: the backend entity could not be closed.`（`cli.py:249`）。都無 repo／issue 編號外洩。

## 2. spec Scenario 覆蓋＋我自己的 5 處改壞

作者紀錄（12 項，每項一處改壞、全紅，基線 234 綠）抽查合理：每個 item 的 mutation 都打在該 item 的核心邏輯上（`_load_json` 版本檢查、`ensure_fresh`、`parse_identity` 方向、`parse_address` 放行、`recipient_cursor_key`、`recipient_matches`、用戶端驗證、`close_channel`、缺欄接受、後到覆蓋、to-me 過濾、skill 刪行），catch 測試與命名相符。

我把全套 Scenario 對過測試名（70＋ 個，`grep ^def test_` 共 200＋）：`angareion` 的 5 介面／游標／格式／token／輸出、`channel-config` 的設定檔／重建 13 條／讀取 8 條，全部找得到對應測試。最容易漏的幾條也都在：`wait` 不動游標（`test_wait_polls_until_a_message_arrives` 末行 `assert not app.cursors.path.exists()`）、取消後下一次 run 仍正確（`test_a_cancelled_run_loses_nothing` 跑了兩輪）、回報不可解析（`test_first_come_first_served…` 內 `assert parse_comment(reports[0]) is None`）、fetch 逾時（`test_…:135` 用 `raise_on_fetch=GitError("git fetch timed out after 20s")`）、回報不觸發 run（workflow 三測＋`test_plain_comments_and_bot_reports_are_ignored_silently`）。

我在 `/tmp/angareion-mut`（`git archive HEAD` 新鮮解包）自做的 5 處改壞（刻意避開作者做過的位置，挑最可能漏的）：

| # | 改壞 | 跑的測試 | 結果 |
|---|---|---|---|
| 1 | `registry_rebuild.py:131` 授權檢查改 `if False:`（全採計） | `test_angareion_rebuild.py` | **4 failed**（`test_non_authorized_comments_are_ignored_without_a_report` 全參數） |
| 2 | `report_text` 改回傳可解析的 action JSON（破 L4） | 同上 | **4 failed**（含 optimistic-lock 回報那條） |
| 3 | `channel_config.py` `resolve()` 拿掉 `refresh_for_lookup()`（查無不再先拉） | channel_config＋pull＋cli | **4 failed**（`test_lookup_miss_fetches_and_then_finds_the_channel`、`test_send_pulls_once_when_the_local_copy_is_behind` 等） |
| 4 | `github.py` `verify_channel` 改永遠 `True`（跳過標題檢查） | github＋cli | **1 failed**（`test_verify_channel_checks_existence_and_the_exact_title`；CLI 側用 fake 故不受影響，符合預期） |
| 5 | `messages.py` `validate_channel_name` 改直接 `return None`（全放行） | messages＋cli＋channel_config | **3 failed** |

5 處全紅。基線：worktree 跑 angareion 15 個測試檔 **295 passed**；`/tmp` 副本子集基線 214 passed（每次改壞後還原，全程確認 `git status` 乾淨、worktree 未被動到）。註：全 repo 套件在這台機器的 system python 下跑不完（`test_tui*` 缺 `textual`；venv 跑全量逾時）——與本 diff 無關（diff 只碰 angareion＋docs＋skill＋config＋workflow），angareion 子集綠即足夠。

## 3. workflow 六項：全對

檔案：`.github/workflows/angareion-registry.yml`（43 行）＋ `test_angareion_workflow.py`（6 測，全綠）。

- **if 擋 Bot**：`jobs.rebuild.if` 含 `github.event.comment.user.type != 'Bot'` 且明確不用 `== 'User'`（`schedule` 無 comment，`null != 'Bot'` 照跑——複審 g 照抄正確）。
- **授權三種**：觸發層只擋 Bot，`author_association` 由腳本再驗（`registry_rebuild.py:131`，`AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})`，`registry.py:28`）；非授權不採計、不回報（`rebuild_channels:131-132`）。
- **留言只經環境變數**：唯一 `run:` 步是 `python3 -m angareion.registry_rebuild`，無 `${{ }}`（測試斷言）；`GITHUB_TOKEN`／repo／issue 號經 `env:` 傳。腳本實際重讀整串、不消費觸發留言（見 L1）。
- **分頁**：`list_comments` 用 `per_page=100`（`registry.py:32,232`），`all_comments` 翻完（`registry.py:242-251`）；`test_pagination_keeps_early_registers`、`test_client_lists_comments_and_follows_pagination` 綠。
- **`concurrency`＋`cancel-in-progress`**：`group: angareion-channels`、`cancel-in-progress: true`（yml:23-25），與設計字面一致；取消不遺失由整串重建保證（`test_a_cancelled_run_loses_nothing`）。
- **`permissions`**：`contents: write`＋`issues: write`（yml:19-21），spec 有 Requirement（複審 L1 的追補已落到 spec——第三輪報告裡記的是「只補一半」，本次確認 spec channel-config 已有「**workflow 權限（複審 L1）**」段，閉環）。

另：`on:` 為 `issue_comment types: [created]`＋每天 `schedule`（`23 3 * * *`）＋`workflow_dispatch`；`created` 限定比設計只嚴不鬆（編輯不觸發空跑，重建反正讀整串），非偏離。

## 4. 安全

- **token 不進輸出／log／例外**：CLI 所有 stderr 出口經 `mask_token(str(e), token)`（`cli.py:175,188,191,271,293,444,458,485,488,493,496,499,514` 全覆蓋）；`github.py:87-88` `_mask` 包住 transport 例外；`registry.py:207-216` 錯誤只帶 HTTP 狀態碼；`transport.py` 無任何 log。測試：`test_token_never_leaks_in_output_exceptions_or_logs`、`test_the_token_never_leaks_into_errors`、`test_backend_error_masks_the_token`、`test_token_permissions_too_open_exits_2_and_never_leaks` 全綠。
- **權限位**：只收 0600／0400（`config.py:200-207`，`mode & 0o077 or not mode & 0o400` 即拒）；0644 exit 2。
- **public repo 私密**：掃過 `src/`、`docs/`、`skills/`、`config/`、`tests/`——`ghp_` 字樣只出現在測試假 token（`ghp_FAKE_*`、`ghp_TEST_*`、`ghp_SECRET_TOKEN_AABBCC` 等，明示假值）；`docs/angareion.md`、`SKILL.md`、`registry-issue.md` 無秘密；註冊簿置頂留言模板明寫「不要把任何秘密寫進來」。`config/angareion/*.json` 為空骨架（部署時填）。
- **跳轉**：transport 不跟隨 redirect（`transport.py:44-48`），`_next_page_url` 拒絕跨 host 分頁連結（`github.py:140-151`）——token 不會被帶到別台。

## 5. Low（不擋合併，建議在 PR 回合或部署時順手處理）

- **L1. `ANGAREION_TRIGGER_COMMENT_BODY` 是死掉的 env var。** workflow 設了它（`.github/workflows/angareion-registry.yml:42`：`ANGAREION_TRIGGER_COMMENT_BODY: ${{ github.event.comment.body }}`），但 `registry_rebuild.py:main()`（`registry_rebuild.py:247-286`）根本不讀這個變數——腳本重讀整串，觸發留言不作為輸入。這正是 H1 想要的最安全形狀（連 env 都不消費），但「設計 §9 說留言內容經環境變數交給腳本」對不上現狀，且 `test_angareion_workflow.py:83` 把它釘住了。建議二選一：刪掉這行 env（腳本不讀，留著只會讓後人以為有在用）或在 design §9 補一句「觸發留言僅經 env 傳遞、腳本實際不消費」。原樣：`ANGAREION_TRIGGER_COMMENT_BODY: ${{ github.event.comment.body }}`。
- **L2. 重建依賴 API 回傳順序＝時間序，未顯式排序。** `rebuild_channels(comments, …)`（`registry_rebuild.py:123-131`）照 list 順序套用先到者為準；`all_comments`（`registry.py:242-251`）照 page 順序串接。GitHub 預設 oldest-first，目前成立，但「先到者為準（依留言時間序）」的權威應是 `(created_at, id)` 而非傳輸順序。建議重建前按 `(created_at, id)` 排一次（fake 與真實 API 行為也順便對齊）。撞上同名競爭＋亂序才會錯判，機率極低，故 Low。
- **L3. `channel delete` 送註銷無重試，create 有 3 次。** `cmd_channel_create` 用 `post_with_retries(registry, body, sleeper=sleeper)`（`cli.py:209-211`），`cmd_channel_delete` 用裸 `registry.post_comment(body)`（`cli.py:252-254`）。設計 §6 只給 register 定了重試次數，§7 沒寫，故非偏離；但暫態失敗在 delete 會留下「已關閉、未註銷」（需手動重跑——重跑安全已測）。建議給 unregister 同樣的 `post_with_retries`（一行）。
- **L4. `RegistryClient` docstring 寫 token 是信道 token，workflow 實際用 `GITHUB_TOKEN`。** 原樣（`registry.py:154-160`）：`"""The real registry issue on GitHub: comments plus the channels.json file. … The token is the channel token (`Issues: Read and write` on MyAiStorage); …"""`。類本身兩邊共用無錯，只是 docstring 以偏概全；建議加一句「workflow 側傳的是 `GITHUB_TOKEN`」。
- **L5. `FakeBackend` 按 issue 編號全域找實體，忽略 repo。** 原樣（`backend.py:355-357`）：`entry = self._entities.get(entity.issue)`；`send` 亦然（`backend.py:362-364`：`if entity.issue not in self._entities`）。真實後端按 `repo#issue` 定位（`github.py:225-226` 有 `_entity_key`）。多 backend＋同編號的測試會假綠。測試品質問題，不影響線上；建議 fake 以 `(repo, issue)` 為 key。

## 附：合併前檢查（非程式）

- tasks 5.1–5.3 未勾（預期內，部署項）：信道 token 加選 MyAiStorage、main 分支保護／bypass、`registry-issue.md` 建 issue＋`ANGAREION_REGISTRY_ISSUE` 變數＋各機器 `registry_issue`。程式側都已就緒（workflow 讀 `vars.ANGAREION_REGISTRY_ISSUE`；變數未設時 job 靜默跳過——部署時別漏）。
- `docs/angareion.md`、`skills/angareion/SKILL.md` 已按 design §13 改寫（含三步、2 分鐘落地、同名、`--backend`、廣播、游標、匿名 fetch、機器產物警告）；`--member` 零殘留；SKILL 無秘密（有測）。

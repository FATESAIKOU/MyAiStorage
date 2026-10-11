# 非作者審查報告：agent-sidecar 第三批 3.7～3.9（D11 worker 接法）

- **審查對象**：commit `d7b2321`（作者：impl1）：`sidecar --version`、`sidecar check [--json]` 三態、`herdr_session` 全域選項、fork 標頭 `sidecar.identity`／`sidecar.message`＋`status` 列 fork。
- **審查者**：Muse Spark
- **審查日期**：2026-10-11
- **對照規格**：
  - `openspec/changes/agent-sidecar-design/design.md` 的 D11（MyLinuxPool#52 對齊：`--version` 印 sha、`check` 三態、herdr session 名、容器重建後以 agora 為準）
  - `openspec/changes/agent-sidecar-design/tasks.md` 3.7～3.9（spec 本次無對應新增條文，見結尾第 5 點）
- **方法**：worktree 只讀；注意 worktree 上另有 impl1 未 commit 的 3.12 WIP（`wait_for_ready` 等，本次審查範圍以已 commit 的 `d7b2321` 為準，不含它）。全部改壞驗證在副本 `/tmp/sidecar-rc3`（`git archive d7b2321`）做，探針只放副本 `tests/unit/test_rc3_probe.py`；`FakeBackend`／`FakeHerdrClient`，不連網、不讀真 token。另為核對 `check` 讀的鍵是否真實存在，做了一次唯讀的 `herdr status server --json`（有子命令、非測試；只取鍵形狀，不記值）。Baseline（副本，純 `d7b2321`）：sidecar 六檔 **79 passed**。

---

## 結論

**建議可合併：無 High、無 Medium。** 三態語意與實作一致、每個分支都有測試釘住（約 20 個改壞紅點全紅）；`--session` 位置與真實 `herdr --help` 的全域形式一致；`status server --json` 的鍵形狀已用真實 herdr（0.8.0）核對過，`check` 讀的四個鍵都存在。附 6 個 Low（含 1 個潛伏的安全 seam）與文件 nits，擇期處理即可。

---

## 1. `sidecar --version`：正確；拿不到時的行為寫在輸出裡，文件沒寫

- `installed_sha()` 讀 PEP 610 `direct_url.json` 的 `vcs_info.commit_id`（`cli.py`）；`pyproject` 的 dist 名正是 `agora`，對得上。MyLinuxPool 的 `uv tool install git+https://…@<sha>` 會有 `vcs_info`；本地 `-e .`／非 git 安裝沒有 → 回 `"sidecar <pkgver> (install sha unknown)"`，exit 0（argparse 內建行為）。
- 改壞驗證：拿掉 sha 分支 → `test_version_prints_installed_sha` 紅、fallback 那條照舊綠（正確分流）。
- Nit（文件，tester 區）：「拿不到就印 `(install sha unknown)`」只寫在輸出字串裡，`docs/sidecar.md` 與 D11 都沒寫；補一句即可。

## 2. `sidecar check [--json]`：三態正確；1 優先於 2 合理但沒寫明

- **逐項語意都對**：config 缺／壞→1；token 缺／權限不對→1、0400 照 D11 接受（與 angareion `load_token` 同一規則，改壞會紅）；`channel_repo` 缺→1；herdr `compatible is False`→1、`running` 非真→1、連不上／回非 dict→2；channel 401／403→1、5xx／連線錯→2；四項全 ok→0。JSON 形狀 `{ok, exit, checks: [{name, status, reason}]}`、文字版標記都有測試。
- ** precedence（1 優先於 2）合理且行為已釘住，但沒寫明**：install.sh 閘門語意下「確定的壞」蓋過「不確定」是對的（Nagios 同理）；改壞驗證——對調順序紅的正好是 token/config 壞的那三條（因為 token／config 壞時 channel 連帶 unknown）。但規則只存在於程式碼：`check --help` 只寫「exit 0 ok, 1 fail, 2 unknown」，D11 與 `check.py` docstring 都沒寫組合規則。建議 help 或 D11 補一行，並收一條「token 壞＋herdr 掛→exit 1」的命名測試（探針已寫好，見下）。
- **一次 GET**：channel 探測就是一次 `list_channels()` 呼叫；GitHub 端 `per_page=100` 翻頁，channel repo 的 issue 數現實上不會翻頁，D11 的「一次 GET」成立（真到 100+ 頻道那天再說）。
- **秘密**：reason 只放路徑／版本／遮罩後錯誤；`mask_token` 拿掉會紅（`test_check_report_contains_no_secrets`）；`token` 變數只用於遮罩，不進輸出。
- **覆蓋缺口（Low，不擋）**：herdr 回非 dict→unknown、channel 404→fail、`backend_factory` 本身丟錯→unknown，這三條分支無命名測試（行為是對的，探針證實 404→1；non-dict 只是 code reading）。`load_config_for_check` 對「檔案存在但 JSON 壞掉」回 exit 1（探針證實），也無命名測試。

## 3. `herdr_session`：位置正確，每個呼叫都帶

- `RealHerdrClient._run` 在 subcommand 之前插 `--session <name>`；AST 驗過 13 個方法全走 `_run`（無旁路）；pin 測試斷言完整 argv `["herdr", "--session", "worker", "status", "server", "--json"]`，拿掉 prepend 會紅。
- 對照真實 `herdr --help`：全域形式正是 `herdr --session <name> [options]`。✓
- `FakeHerdrClient` 直接忽略 session（測試用，無妨）。
- 小缺口（Low）：除 `status_server` 外，其他 12 個方法的 `--session` 沒有逐一斷言——靠 `_run` 單一 choke point 保證，結構上穩；要寫也只要再加一條（例如 `tab_create`）。

## 4. fork 標頭與 `status` 列 fork：寫讀鏈條都通；「還沒結束」只是標註，不是過濾

- **寫**：`start_team` 組 `--header sidecar.identity=<role>`（role 取 `teams.<id>.identity.role`，沒有取 team key）與已知的 `sidecar.message=<msg id>`；三條命令形狀測試拿掉會紅。agora 側收得到：`--header` 支援點路徑（`header.py:146`）、`check_user_fields` 只擋 `id`／`agora`、`validate` 不清自訂鍵；`index.search` 的 `_matches` 走任意路徑全等比對，`sidecar.identity=<role>` 查得到（`store.py:1080`）。
- **讀**：`status` 對每個 `config.identities` 下 `sidecar.identity=<ident>` filter（Fake 與真實 `_matches` 在 `=` 純量形狀等價）；agora 掛掉整段 try 掉、`agora_forks_error` 照印，exit 照舊 0（兩條都拿掉會紅）。
- **「還沒結束」的判定（D11 用字 vs 實作）**：實作是**全列＋標 `[active]`／`[idle]`**，`active`＝`store.continuing()`（pending 鎖有人拿＝此刻正在被接續）。D11 寫「列出…還沒結束的 fork Session」——但「已結束」從標頭看不出來（跑完的和被打斷的長一樣），所以過濾做不到，標註＋讓人決定是誠實作法。建議 D11 改字為「列出並標註 active」，或定義 ended（目前無此訊號）。Low，問 PM 一句。
- **附帶一致性要求**：`status` 查的是 `config.identities`，寫的是 role（無 `teams.<id>.identity` 時退回 team key）。兩邊要對上，identities 放的必須是 role——測資都是 key＝role 的巧合，文件加一句即可（Low）。
- **`trigger_message_id` 目前無呼叫者**（`src/` 內只有定義；daemon 不叫 `start_team` 是已知缺段）：寫了也送不出去，`sidecar.message` 今天永遠缺席。等接線時一併驗。
- **潛伏的安全 seam（Low，現在無呼叫者所以不爆）**：`pane_run` 的命令用 `" ".join` 直拼，`trigger_message_id` 來自 wire（Angareion `Message.id` 只驗非空，空格／分號都過得了 `validate`）。接線那天若不處理，就是 shell injection。建議接線時對所有插值上 `shlex.quote`（會改命令字串形狀，屆時測試同步改）。

## 改壞驗證記錄（副本，WORKTREE 未動）

| 項 | 改壞方式 | 結果 |
|---|---|---|
| version sha | 永遠走 fallback | sha 那條紅、fallback 綠 |
| token 0400 | angareion 規則改回嚴格 0600 | 0400 那條紅、0644 照舊綠 |
| herdr compatible | 拿掉分支 | incompatible 紅 |
| precedence 對調 | unknown 優先 | token/config 壞的三條紅（channel 連帶 unknown）→ 規則行為已釘 |
| mask_token | 拿掉 | no-secrets 紅 |
| `--session` | 拿掉 prepend | argv 測試紅 |
| fork headers | 兩段拿掉 | 3 條命令形狀紅 |
| status forks | 不查 agora | 列表＋失敗兩條紅 |
| 探針（4 條，全綠＝行為如上） | — | fail+unknown→1、running=false→1、404→1、壞 JSON config→1 |

## 給 PM 的清單

1. **可合併**（無 High／Medium）；Low 擇期：precedence 與 `--version` fallback 寫進 help/D11（探針的 fail+unknown 命名測試可收）、non-dict／404／factory 丟錯三條命名測試、`tab_create` 的 `--session` argv 一條、D11「還沒結束」改字、identities 放 role 的文件一句。
2. **接線時處理**（已有缺段，不新開）：`trigger_message_id` 上線前先上 `shlex.quote`（shell injection 潛伏）；`start_team` 被 daemon 呼叫時 `sidecar.message` 才會出現，屆時重驗 3.9 端到端。
3. **spec 歸檔前（5.3）**：`--version`／`check` 三態／`herdr_session`／fork 標頭四組 D11 行為在 sidecar spec 目前無對應條文，要補 delta 還是維持「只在 design」請定一下。
4. 副本與探針留置 `/tmp/sidecar-rc3`（含 `tests/unit/test_rc3_probe.py`）；worktree 只新增本報告檔。

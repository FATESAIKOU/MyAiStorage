# 復原 runbook（6.4，只限本人）

Drive 上的 repo 被刪除時，從任一個 git clone 重建。全部在 AdminLock 內執行。

## 前置記錄（遺失時先補齊）

- 每個 repo 的完整 clone URL（`annex::<uuid>?type=rclone&…&rcloneprefix=…`）。
- pin repo 的位置、`config/committer.json` 的備份。
- manifest 的 file id。
- **隔離區的資料夾 id 與日期子資料夾**（`quarantine_folder_id`／`quarantine/<YYYY-MM-DD>/`）。
  真本被隔離時要從這裡搬回來，沒有 id 就只能走 from-clone。

> CLI 狀態：`recover` 目前只做**唯讀偵測與列步驟**（`--config config/committer.json
> --new-prefix <id>`）；實際的「刪檔重建＋重推＋重建 pin」是整合測試與
> `swap-finish` 的範圍，沒有管理憑證時會報 `not_wired`。
> **模式 0（從隔離區搬回）沒有 CLI**，照下面「模式 0」自己寫一支腳本。

## 四種模式（`python -m aistorage.admin recover --check …` 先判定）

**模式 0 最先問**：主 manifest 與它引用的 bundle／annex 物件是不是只是被**隔離**
到 `quarantine/<日期>/` 了？隔離是「搬走」不是「刪除」（`apply_sweep` 用 Drive 的
addParents/removeParents，檔案本身還在，file id 也還在）。是 → 照模式 0 搬回，
**資料一件都不會少**。先列隔離區確認，不要直接跳到模式 3。

0. **真本被隔離，但還在隔離區**（`lsf` 前綴看不到 manifest，但隔離區看得到）：
   從隔離區搬回前綴 → 重建釘選值。步驟見下。
1. **主 manifest 還在**：不需要復原，先跑健康檢查找真正的原因。
2. **只剩 `.bak`**：比對 `.bak` 內容雜湊等於正式 pin 的 manifest；
   相符就放著，下一輪提交流程走 BAK_RECOVERY（丟棄待定、照常往下）。
   不相符就改走 from-clone。
3. **主 manifest 與 `.bak` 都不在**（隔離區也沒有）：只能 from-clone：
   用任一個 clone 推到新前綴（git push＋上傳 bundle／manifest／annex 物件）；
   以觀測到的遠端狀態重建正式 pin（`init-pin --confirm`，只在 Mac；它會照
   6.5 自己上鎖——沒有維護旗標時停用 workflow、等沒有執行中的 run、重讀遠端
   manifest，已經有旗標時就是沿用中止處理中既有的鎖）；
   更新 `config/committer.json`；`readview_rebuild_epoch` 加 1
   完整重建讀取視圖（等全部上傳完才切換 manifest）；
   跑一輪完整提交流程（含清掃）確認正常，把步驟與耗時記在下面。

## 模式 0：從隔離區把真本搬回來

**什麼時候走這條**：提交流程某一輪把 manifest／bundle／annex 物件隔離了，但
隔離區裡還在。`git clone` 會直接失敗：

```
git-remote-annex: No git repository found in this remote.
```

**為什麼會發生**：釘選值落後遠端是完全正常的中間狀態（某一輪 push 成功、第 11 步
驗證沒過就沒有 promote）。舊版清掃規則是「內容雜湊不在釘選值 → 隔離」，於是
下一輪把剛 push 上去、還沒轉正的 manifest 連同它引用的新 bundle 一起搬走——真本
被提流通程自己消滅。**新版不會再這樣**（`HOLD` 取代 `QUARANTINE`），所以這一步
只會救到舊版（或人工製造）的狀況。

### 步驟

1. **先看清楚要搬什麼**（唯讀）。列出隔離區與前綴，記下每個檔案的 **file id**
   （搬移要用 id，不用名字）：

   ```bash
   export RCLONE_CONFIG=<rclone conf>
   rclone lsf "gdrive:" --drive-root-folder-id <quarantine_folder_id> "gdrive:<YYYY-MM-DD>"
   rclone lsf "gdrive:<rcloneprefix>/agora/"
   ```

   必搬／選搬：

   | 檔 | 必要性 | 不搬會怎樣 |
   |---|---|---|
   | `GITMANIFEST--<uuid>` | **必需** | `init-pin` 直接 `AbortRun`（它要求前綴裡**只有一種內容**的主 manifest） |
   | manifest 的 active 列表裡、但前綴沒有的 `GITBUNDLE-*` | **必需** | manifest 解析得出來但 `_download_and_replay` 找不到 bundle → 中止 |
   | 那一輪新寫入的 `SHA256E-*`（manifest 引用不到，但樹狀指得到） | **必需** | `init-pin` 的 key 集合不含它 → 之後讀不到那份 Session |
   | `GITMANIFEST--<uuid>.bak` | 選搬 | `init-pin` 只看主 manifest；搬回去只是讓前綴回到 push 完的形狀 |

   > **前綴裡有兩份同名主 manifest 是正常狀態，不要當成注入物**（2026-09-30 實測，
   > 見 `docs/decision-log.md` 同一節）：rclone 每輪 push 都重寫 manifest、file id
   > 會變，而同一輪 push 內因為 Drive 的列表落後，有時會留下**兩份位元組相同**的
   > 主 manifest。`git push` 與 `git clone` 在那種狀態下都正常，下一輪 push 會由
   > rclone 自己清掉多餘的那份。清掃的規則是「**位元組相同就都不搬**、列入健康檢查」
   > ——因為分不出哪一份才是真的，搬錯就是消滅真本。
   >
   > 所以要判斷的時候：**看 `createdTime` 與 `modifiedTime`**（健康檢查的
   > `held_files` 與提交流程報告的 `need_admin_files` 都附上了 `@<createdTime>`）。
   > 真正在用的是**最近被改寫（`modifiedTime` 最新）的那一份**；建立時間較早但已經
   > 很久沒被改的那幾份通常是被 rclone 留下的舊世代或住民的副本。**內容不同於釘選值
   > 的同名 manifest 才是注入物**，可以直接隔離。

2. **在 AdminLock 內搬回**。`AdminLock` 的真正互斥是 **pin repo 裡的維護旗標**
   （提交流程第 1b 步與 write_pending／push／promote 三個重查點都讀它）；
   停用 workflow 只是輔助措施。腳本要：先用 `drive.list_children` 確認 4 個檔都
   在 → `AdminLock(...)` → 逐檔 `drive.move(id, from_parent=<日期資料夾 id>,
   to_parent=<prefix_folder_id>)`（已存在於前綴就跳過）→ 驗證 → 解除。

3. **驗證（離開鎖之前就要做完）**：

   - 前綴裡 `GITMANIFEST--<uuid>` **恰好一個**；
   - `parse_manifest` 通過、active 數量符合預期；
   - `aistorage.integrity.settle._download_and_replay`（唯讀）重放出預期的 refs；
   - `git ls-remote` **等於**重放結果。

   三者不一致就**留在鎖內不要解除**（`AdminLock.__exit__` 會把旗標標成 `aborted`
   等人處理），先查清楚再來。

4. **重建釘選值**：

   ```bash
   uv run python -m aistorage.committer init-pin \
     --config config/committer.e2e.json --i-am-admin --confirm
   ```

   印出 `INIT_PIN_PROMOTED: repo=… manifest=<前 8 碼> active=<n> keys=<n>`。
   `manifest` 要等於隔離區那份 manifest 的雜湊，`keys` 要等於 pending 記的
   `annex_keys_count`（pending 還在的話，這是最強的交叉確認）。

5. **跑一輪提交流程**確認恢復（要有 `SUCCESS`）：

   ```bash
   uv run python -m aistorage.committer run --config config/committer.e2e.json
   ```

### 陷阱：`github_repository` 裡沒有 workflow 的環境（e2e／測試 pin repo）

`AdminLock.__enter__` 會 `gh workflow disable <committer_workflow>`。設定檔的
`github_repository` 若指向一個**沒有任何 workflow** 的 repo（例如 e2e 用的
`MyAiStorage-pin-test`，提交流程是在本機跑、不是 CI），`gh` 會 404 → rc=1 →
`AdminLock` 把旗標標成 `aborted` 然後拋出，**`init-pin` 完全沒跑**。

這是可預期的中止，照既有的中止處理路徑走就好：

1. **再跑一次同一個 `init-pin --confirm`**。旗標此時是 `aborted`，
   `admin_lock_if_needed` 會直接放行（不再上一次鎖、也不再呼叫 `gh`），
   `init_pin_cli(..., maintenance_ok=True)` 就會執行。→ `INIT_PIN_PROMOTED`。
2. **清掉旗標**（`admin_lock_if_needed` 的中止路徑不會自己清）：

   ```bash
   uv run python -m aistorage.admin unlock \
     --pin-repo <pin repo url> --key <deploy key> --repo <CommitterConfig.repo> \
     --gh-repo <github_repository> --confirm
   ```

   注意 `unlock` **不吃 `--config`**（它要的是上面那五個參數）。
   它會先刪旗標、再去 `gh workflow enable`，所以在沒有 workflow 的 repo 上
   **刪完旗標仍然回 `admin_error`**——那是預期的，旗標已經清掉了。
   用 `git ls-tree origin/main .pin/ | grep <repo>` 確認 `<repo>.maintenance`
   與 `<repo>.pending.*` 都不在。

（若要根治，可以讓 `AdminLock` 在「該 repo 沒有這個 workflow」時跳過停用步驟並
記錄下來，而不是標 aborted。目前的行為是安全的——只是要多跑一次。）

## 演練紀錄

### 2026-09-29　e2e `agora-e2e-01M3M1YGWMYS6C83J6NQ5TJ64J`（模式 0 實跑）

起因：impl3 實跑 9.1 時，提交流程在 00:30:06 那一輪把 `GITMANIFEST--<uuid>`、
它引用的 `GITBUNDLE-s3418-…` 與那一輪新寫的 `SHA256E-s42436-…` 隔離了，
`git clone` 之後每輪都 `ReadError (rc=128)`。釘選值停在 `manifest=83eb33b8`、
`main=d0caacf3`，而 pending（`refs main=f18a3cf6`）在 22 秒前被另一輪 settle 當成
「遠端沒動」丟掉了。**資料沒有遺失**：被丟掉那份 pending 記的 refs，用隔離區的 7
個 bundle 重放得出來完全一致。

| # | 動作 | 結果 |
|---|---|---|
| 1 | 列隔離區（唯讀） | 6 個檔；其中 3 個是真本（manifest、`.bak`、`GITBUNDLE-s3418`）＋ 1 個新 annex 物件、2 個讀取視圖的 reading 檔 |
| 2 | AdminLock（`reason=restore-from-quarantine`）內搬 4 個檔回前綴 | 前綴 40 → 44 個檔 |
| 3 | 鎖內驗證 | 主 manifest 恰好 1 個、`sha256=9ff0a559`、active 7／removed 10；重放 refs ＝ `ls-remote` ＝ `{git-annex: 4766309b, main: f18a3cf6}` ✓ |
| 4 | `init-pin --i-am-admin --confirm` | 第一次：`gh workflow disable committer.yml` 404 → 旗標標 aborted（陷阱一節） |
| 5 | 再跑一次 `init-pin --i-am-admin --confirm` | `INIT_PIN_PROMOTED: manifest=9ff0a559 active=7 keys=35`；`annex_keys_sha256=af4d0716…` 與被丟掉那份 pending 相同 ✓ |
| 6 | `admin unlock --confirm` | 刪掉旗標；`gh workflow enable` 404 → `admin_error`（預期），旗標已清 ✓ |
| 7 | 跑一輪 committer | `SUCCESS`（`accepted=3, already=1, inbox_deleted=12, quarantined_files=0, held_files=0, annex_keys_checked=35`）；釘選值推進到 `main=a1f9a370`、8 active、37 keys；收件匣清空 |

耗時（step 7）：clone 38s、push 29s、verify 26s、publish 126s，整輪約 4.5 分鐘。

# K.1 Drive 探針（#109 / MyAiStorage#152）

`experiments/agora-chunks/probe_drive.py` 是 `openspec/changes/agora-diff-upload/design.md`
「K.1 探針腳本規格」的可執行版本。它只回答 Drive 的行為，不改 repo、不 import `agora`，
也不讀任何真實 session／快取／state。

## 它量什麼

| # | 量測 | 預設次數 |
|---|---|---|
| 1 | `copy --files-from` 送 1／100／500／1000 個 4 KiB 檔 | 5 |
| 2 | `lsjson -R --fast-list --hash --files-only` 列 1k／2k／5k 檔；同一個資料夾不加 `--files-only` 再列一次，看目錄項有沒有 `ID` | 5 |
| 3 | `delete --files-from` 刪掉第 1 項剛送出的 1000 個檔 | 5 |
| 4 | 上傳後立刻 `lsjson`：md5 出現的延遲與失敗次數（K2）；`ModTime` 與本機 `stat` 的差（K13） | 20 輪 |
| 5 | 100 MiB 單檔 vs 25 個 4 MiB 段的下載＋接回 | 3 |
| 6 | `mkdir` → 記資料夾 `ID` → `purge` → 同名重建 → 再記 `ID`，比較兩者（K17）；重建用 `mkdir` 3 輪＋`copy` 1 輪 | 3＋1 |
| 7 | 對存在／不存在的資料夾各跑 `lsjson`（含 `--dirs-only`），記退出碼與 stderr；再注入一次斷網 | 1 |

## 安全邊界

- **只把 `~/.config/agora/rclone.conf` 的路徑交給 `rclone --config`**，腳本本身不開啟、
  不複製、不印出它的內容。remote 名稱與旗標形狀照 `src/agora/store.py` 的 `Drive`：
  `gdrive`、`--config <path> --ask-password=false`，root-scoped 呼叫才多
  `--drive-root-folder-id`；探針**不用** agora 的 root folder id。
- 在 remote 的**根**建 `gdrive:agora-probe-k1-<8 碼>`，所有東西只在它底下
  （`sessions/<假 ULID>/`、`send/…`、`listing5k/`、`big/`、`probe/`）。不碰
  `agora/`。如果根不能建資料夾，腳本印錯就停，不會改去 `agora/` 底下建。
- 所有檔案都是程式產生的假內容；不讀真檔、不讀 token、不讀 `~/.gemini`／
  `~/.claude`／`~/.cache/agora`／`~/.local/state/agora`。
- 交給 rclone 的子行程環境會**剔除所有 `RCLONE_*` 變數**（例如
  `RCLONE_DRIVE_ROOT_FOLDER_ID` 會把「根」換成 agora 的資料夾）；建好 throwaway
  後再列一次 remote 根目錄，若看到 `sessions/` 就停止（代表根不是 My Drive 根）。
- 每個 rclone 呼叫都有逾時。任何一步失敗都跳到清理。`mkdir` 的 `created` 標記在
  **呼叫之前**就設好，因為客戶端逾時時 Drive 上的資料夾可能已經建好。
- 清理（`finally`，含 Ctrl-C／SIGTERM）一定 `purge` 那個 throwaway 資料夾，並再
  `lsjson` 確認它不在；清理期間 SIGINT／SIGTERM 會被忽略，第二次中斷不會打斷
  purge。清不掉時大聲印出資料夾名稱與手動清理指令。
- `purge` 走 Drive 垃圾桶（rclone 的 `drive.use_trash` 預設為 true）：資料夾會從
  列表消失，但內容在垃圾桶保留約 30 天。這次只有假內容；要知道「刪乾淨」與
  「從列表消失」的差別。

## 怎麼跑

三個模式**必選一個**；完全沒給旗標時 argparse 會印用法、exit 2，而且在任何
rclone 呼叫之前就結束（不會建立資料夾或檔案）。

```bash
# 只回答 R2／R3 要的四題（②④⑥⑦；約 2–4 分鐘、<1 MiB）
python3 experiments/agora-chunks/probe_drive.py --answers

# 快速檢查（每項 1 次、只做 ①–④ 與小檔；約 1–2 分鐘）
python3 experiments/agora-chunks/probe_drive.py --quick

# 完整探針（約 30–60 分鐘；不含 K13 的一小時等待）
# 未經本人同意不要跑；它會建 ~15k 個檔、~270 MiB。
python3 experiments/agora-chunks/probe_drive.py --full

# K13 要求「隔一小時再列一次」：加這個參數（總時間再多 60 分鐘；只給 --full）
python3 experiments/agora-chunks/probe_drive.py --full --recheck-minutes 60
```

### `--answers`：R2／R3 的四個答案

只跑 ②（遞迴 `lsjson` 不加 `--files-only`，目錄項有沒有 `ID`）、④（三個不同
本機 mtime 的小檔，判斷 `ModTime` 是本機檔案時間還是上傳時間）、⑥（purge 後同名
重建的 `ID` 是否改變；三輪 `mkdir` 重建＋一輪 `copy` 重建）、⑦（存在／不存在／
存在但空／斷網的退出碼與 stderr 形狀）。不建 1000 個以上的檔、不跑 100 MiB、
不等一小時；②④各 3 次，⑥ 3＋1 輪，⑦一次。資料只有 5 個 session 小檔＋
9 個 ModTime 小檔（3 輪×3）＋1 個 copy 重建小檔＋幾個空資料夾，峰值不到 1 MiB、
約 15 個檔，跑完一樣 purge。

前提：`rclone` 已安裝，且 `gdrive` remote 已授權（就是 agora 平常在用的那個
`~/.config/agora/rclone.conf`）。探針不會幫你授權，也不會建立新的 rclone 設定。

### `--quick` 與完整模式的差別

`--quick` 只驗證接線與 ①–④ 的小檔版本（1 次、100 檔），**不回答 K.1 的
⑤⑥⑦**。R2／R3 要的四個答案可以只跑 `--answers`；完整的 K.1（含①③⑤與
小時後的 `ModTime` 重看）仍要 `--full`（未經本人同意不要跑）。

## 它會在 Drive 上建什麼、用多少

- `--answers`：5 個 session 小檔＋9 個 `ModTime` 小檔＋1 個 copy 重建小檔＋
  幾個空資料夾，峰值不到 1 MiB、約 15 個檔。
- 完整模式：約 15.1k 個 4 KiB 小檔（`sessions/` 2000、`listing5k/` 5000、① 的
  5×(1+100+500+1000)、④ 的 100 個）＋約 200 MiB 的大檔（100 MiB 單檔＋
  25×4 MiB 段）。
- 峰值約 270 MiB、約 15k 個檔，全部在 `agora-probe-k1-<8 碼>/` 底下。
- ⑤ 會下載 600 MiB（3 次 × 兩種形狀），本機同時留下每一輪的下載與接回檔，
  暫存最高約 1.2 GB。全部跑完（或失敗）就 purge，本機暫存也會自動刪；
  `--keep-local` 可留著除錯。

## 輸出

- stdout 只印時間、次數、bytes、檔案數、`ID` 有無／是否相同、`ModTime` 與本機
  `stat` 的差、退出碼與 stderr 前 200 字元。不印檔名內容、不印 config、不印 token。
- JSON：預設 `/tmp/agora-probe-k1-<時間>.json`（`--json-out` 可指定）。內含每一輪
  的秒數、中位數、`ID`、`ModTime`、清理結果與沒驗的項目。
- stderr 在進報告前會過濾：含 `token`／`client_secret`／`refresh`／`password`／
  `secret` 的整行遮掉，config 路徑換成 `<config>`。

## 怎麼確認清乾淨

腳本最後會印：

```text
[cleanup] purged and verified gone: agora-probe-k1-xxxxxxxx
```

想自己再看一次（只列名字，不讀內容）：

```bash
rclone --config ~/.config/agora/rclone.conf lsf gdrive: --dirs-only | grep agora-probe-k1
```

沒有輸出就是清乾淨了。如果腳本印出 `THROWAWAY FOLDER STILL EXISTS`，照它印的
`rclone … purge gdrive:agora-probe-k1-xxxxxxxx` 手動清。

## 不連 Drive 的自測

用 repo 裡的假 rclone 跑 `--quick`（假 rclone 沒有 `version`、`ModTime`、目錄下載、
換 ID 與斷網，所以只能驗證程式流程，不能當 K.1 答案）：

```bash
T=$(mktemp -d)
mkdir -p "$T/remote"
FAKE_REMOTE="$T/remote" FAKE_RCLONE_NOT_FOUND_RC=3 \
  python3 experiments/agora-chunks/probe_drive.py --quick \
  --rclone "python3 tests/fakes/fake_rclone.py" \
  --config "$T/rclone.conf" --json-out "$T/result.json"
```

把 `--quick` 換成 `--answers` 就是②④⑥⑦的最小自測；同一個假 rclone 沒有
`ModTime`、換 `ID` 與斷網，所以答案會是 `neither`／同 `ID`／網路注入仍成功，
只能驗流程。

`--rclone` 接受含空白的指令（用 `shlex` 切），所以不必把假 rclone 改成可執行檔。

# Agora lite 人工驗收清單（草稿，給 PM 看過後換掉 docs/acceptance.md）

> review 起草，2026-10-03。依 HEAD 的行為寫：continue **寫回同一個 id**；`cache`／`sync` 已經拿掉，改成 `pull`／`push`；雲端沒有的 Session 會保留並標記。**這份草稿沒有實際跑過**，指令與訊息是照程式和測試寫的，跑的時候如果有不一樣的地方，以實際畫面為準，再回報給 review。

全程只用 Drive 上的 `agora-test`，以及 `/tmp/agora-acc` 底下的目錄。對話一律用自編短句（例如「把 CSV 轉成 Markdown 表格，先列三個步驟」），不要貼真實內容。做完照最後一節清掉。

每一步都是一行指令或一個按鍵；「算過」寫的是要看到什麼。`echo $?` 用來看 exit code。印出來的 `agora:<ULID>` 都記下來（A、B、C…）。

---

## 0. 準備（只做一次）

```bash
cd ~/.herdr/worktrees/MyAiStorage/phase1-spike && uv tool install --editable .
mkdir -p /tmp/agora-acc/proj
cat > /tmp/agora-acc/env.sh <<'EOF'
export AGORA_FOLDER_NAME=agora-test
export AGORA_CACHE_DIR=/tmp/agora-acc/cache
export AGORA_STATE_DIR=/tmp/agora-acc/state
cd /tmp/agora-acc/proj
echo "[acc] 機器 1：agora-test、/tmp/agora-acc ✓"
EOF
cat > /tmp/agora-acc/env2.sh <<'EOF'
export AGORA_FOLDER_NAME=agora-test
export AGORA_CACHE_DIR=/tmp/agora-acc/cache2
export AGORA_STATE_DIR=/tmp/agora-acc/state2
cd /tmp/agora-acc/proj
echo "[acc] 機器 2（模擬另一台）：agora-test、/tmp/agora-acc/*2 ✓"
EOF
source /tmp/agora-acc/env.sh
git init -q && git commit -q --allow-empty -m init
```

⚠️ **每一節的第一行都是 `source /tmp/agora-acc/env.sh`，看到 `[acc] 機器 1 … ✓` 才往下做。** 沒有 source 的話，agora 會寫進正式的 `agora/` 和平常的快取。

⚠️ **「機器 2」只是另一組快取和狀態目錄**，用來模擬「另一台機器刪掉了」。用完要記得回到機器 1：`source /tmp/agora-acc/env.sh`。

⚠️ **每一個 `continue` 都明寫 `--dir /tmp/agora-acc/proj`。**

準備三段自編的 opencode 對話（之後的匯入要用）：

```bash
opencode    # 說一句自編的話，離開；重複三次，得到三個 session
opencode session list   # 只列出這個專案的；記下三個 ses_…（s1、s2、s3）
```

---

# 第一段：T1 指令模式

## 1. import 一次多個

| # | 指令 | 算過 |
|---|---|---|
| 1.1 | `agora import session --external-session-id s1,s2 --external-session-id s3 --agent opencode` | stdout 有三行 `agora:<ULID>`（記為 A、B、C）；stderr 有 `匯入 1/3`、`2/3`、`3/3` |
| 1.2 | `echo $?` | `0` |
| 1.3 | 同 1.1 再跑一次 | 印出**同樣的**三個 id（內容沒變就略過），不會多出新的 Session |
| 1.4 | `agora import session --external-session-id s1,ses_nope --agent opencode; echo $?` | 印出 A；stderr 說 `ses_nope` 匯入失敗；exit 不是 0 |
| 1.5 | `agora import session --external-session-id s1,s2 --agent opencode 2>/dev/null \| wc -l` | `2`（進度只在 stderr） |

## 2. pull／push

| # | 指令 | 算過 |
|---|---|---|
| 2.1 | `agora pull session; echo $?` | 提示要給 session id，exit `1` |
| 2.2 | `agora pull session <A>` | stderr 有 `pull 1/1`；`ls /tmp/agora-acc/cache/sessions/<A的ULID>/` 有 `session.md` 和一個 `raw-….json` |
| 2.3 | 同 2.2 再跑一次 | 很快結束（已經是新的，所以略過） |
| 2.4 | `agora pull session opencode:s1` | `ls /tmp/agora-acc/cache/reading/opencode/` 有 `s1.md` |
| 2.5 | `agora pull session s1; echo $?` | 提示 `s1` 是 agent 的 id、要寫前綴，exit 不是 0 |
| 2.6 | `agora edit session <A> --header 'title=驗收改名'` 之後 `agora push session <A>` | stderr 有 `push 1/1`、「寫回 1 個」 |
| 2.7 | `agora push session opencode:s1; echo $?` | 拒絕：push 只吃 agora 的 id |

## 3. 接續會寫回原本那一個

| # | 指令 | 算過 |
|---|---|---|
| 3.1 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj` | opencode 打開時，畫面上已經有 A 的對話；說一句自編的話（例如「把第二步寫詳細一點」），離開 |
| 3.2 | （3.1 結束時） | 印出的是**同一個 A**，不是新的 id |
| 3.3 | `agora show session <A> \| tail -5` | 看得到 3.1 說的那句話 |
| 3.4 | 同 3.1，但打開之後什麼都不說就離開 | 印「這次沒有新內容，沒有存」 |

## 4. delete 多個與重跑

| # | 指令 | 算過 |
|---|---|---|
| 4.1 | `agora delete session <B> <C>; echo $?` | 列出會刪的兩個，叫你加 `--yes`；exit `1` |
| 4.2 | `agora delete session <B> <C> --yes \| wc -l` | `2`；stderr 有 `刪除 1/2`、`2/2`，以及「移到 Drive 垃圾桶」 |
| 4.3 | 同 4.2 再跑一次（等同中斷後重跑），然後 `echo $?` | stderr 說「已經不在了，略過 2 個」；exit `0` |
| 4.4 | `agora delete session agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ --yes; echo $?` | 「找不到」，exit `1`（打錯的 id 不會被當成已刪） |

## 5. merge 被中斷之後沿用

先做兩個來源：`agora import session --external-session-id s2,s3 --agent opencode`（B、C 已經刪了，這次會印出新的 id，記為 B2、C2）。

| # | 指令 | 算過 |
|---|---|---|
| 5.1 | `agora merge session <B2>, <C2> --agent opencode` | stderr 先出現 `來源 1/2`，然後「請 opencode 寫 <B2> 的要約…」 |
| 5.2 | 看到 `來源 2/2` 和「請 opencode 寫 <C2> 的要約…」時，按 **Ctrl-C** | 中斷；`echo $?` 是 `130` |
| 5.3 | 同 5.1 再跑一次 | stderr 有「<B2> 的要約沿用上次寫好的」，**只**為 C2 叫一次 opencode；最後印出新的 id（記為 G） |
| 5.4 | `agora show session <G>` | 「## 要約」底下 B2、C2 各一節；標頭有 `status: draft` |
| 5.5 | `opencode session list` | **沒有**多出寫要約用的 session |

## 6. 雲端沒有的 Session（模擬別台刪掉）

| # | 指令 | 算過 |
|---|---|---|
| 6.1 | `source /tmp/agora-acc/env2.sh` 之後 `agora delete session <A> --yes` | 機器 2 把 A 移到 Drive 垃圾桶 |
| 6.2 | `source /tmp/agora-acc/env.sh` 之後 `rm -f /tmp/agora-acc/state/last-sync` | 回到機器 1，並且讓下一次同步不被節流 |
| 6.3 | `agora search session` | A 那一行的最後是 `(雲端沒有)`；A 還在機器 1 上，沒有被清掉 |
| 6.4 | `agora search session --filter cloud=no` | 只列出 A |
| 6.5 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj; echo $?` | **不會**打開 opencode；訊息提示兩個選擇（`push … --not-exist-upload`、`pull … --not-exist-delete`）；exit `1` |
| 6.6 | `agora edit session <A> --header 'title=x'; echo $?` | 一樣拒絕，exit `1` |
| 6.7 | `agora merge session <A>, <G> --agent opencode; echo $?` | 拒絕，exit `1` |
| 6.8 | `agora pull session <A>` | 只印一行「雲端沒有，本機的不動」；A 還在 |
| 6.9 | `agora push session <A>` | 只印一行「雲端沒有，沒有傳」；Drive 上還是沒有 A |
| 6.10 | `agora push session <A> --not-exist-upload` | A 傳回 Drive；`rm -f /tmp/agora-acc/state/last-sync && agora search session` 之後，A 那一行**沒有** `(雲端沒有)` |
| 6.11 | 再做一次 6.1、6.2，然後 `agora search session --filter cloud=no \| awk '{print $1}' \| xargs agora pull session --not-exist-delete` | 「雲端沒有，本機的副本已刪」；`agora search session` 裡沒有 A 了 |
| 6.12 | `agora show session <A>; echo $?` | 「找不到」，exit `1` |

---

# 第二段：T2 互動模式

準備：`source /tmp/agora-acc/env.sh`，再用 opencode 多說兩段自編的短對話（s4、s5，**不要**匯入）。然後在終端機裡執行 `agora`（不帶參數就是互動模式；這一步由使用者自己做）。

| # | 按鍵 | 算過 |
|---|---|---|
| 7.1 | （打開時） | 上面是「Agora」「未匯入」兩頁；Agora 頁有「雲端」欄，目前的 Session 都是 ✓ |
| 7.2 | 看最下面的按鍵列 | 有 `空白`、`a`、`enter 接續`、`m`、`e`、`d`、`p`、`P`、`/`、`ctrl+t`、`q`；**沒有** `r`、`s` |
| 7.3 | `Tab` | 換到未匯入頁；按鍵列**沒有** `m`、`e`、`d`、`P`，Enter 寫的是「匯入」 |
| 7.4 | `空白` 勾 s4，`↓`，`空白` 勾 s5 | 兩列有 ✓，游標沒有因為勾選而跳走 |
| 7.5 | `Enter` | 等待視窗出現，進度條走到 `2/2`，結束後顯示結果；回到清單時兩列不見了（已匯入），勾選清掉 |
| 7.6 | `Tab` 回 Agora 頁 | 剛匯入的兩個在最上面（記為 H、I），雲端欄是 ✓ |
| 7.7 | `a` | 看得到的列全部打勾；游標**還在原本那一列** |
| 7.8 | `a` | 全部取消 |
| 7.9 | 勾 H、I 兩列，然後 `/`，輸入 H 的標題裡的一個字，`Enter` | 只剩看得到的列；標題列寫著「另有 1 個勾選被篩選掉」 |
| 7.10 | `d` | 確認視窗**只列出看得到的那一個**；選「取消」（預設就是取消）。`/` → 清空 → `Enter` 取消篩選 |
| 7.11 | 勾 H、I，`m`，選 opencode | 等待視窗出現進度；在進度還沒走完之前按 **`Esc`** |
| 7.12 | （7.11 之後） | 視窗顯示「已中斷」，回到清單，提示「重跑同一個動作會接著做」；H、I **還是勾著的** |
| 7.13 | 另開一個終端機：`pgrep -fl "opencode run"` | 沒有殘留的 opencode（寫要約的那個也停了） |
| 7.14 | 回到互動模式，再按一次 `m`，選 opencode | 這次會沿用已經寫好的那一節；完成之後，最上面多一個合併出來的 Session，勾選清掉 |
| 7.15 | 游標放在一列上，`p` | 確認視窗「把 1 個拉到本機？」，有一個**沒有勾**的「雲端沒有的就刪掉本機的」；選「確定」→ 等待視窗 → 完成 |
| 7.16 | 同一列，`P` | 確認視窗「把 1 個寫回 Drive？」，有一個**沒有勾**的「雲端沒有的就傳回去」；選「取消」 |
| 7.17 | `q` 離開；在另一個終端機做 6.1、6.2，把 H 用機器 2 刪掉，再回機器 1 執行 `agora` | H 那一列的雲端欄是 **✗** |
| 7.18 | 游標放在 H 上，`Enter`（接續） | 不會打開 agent；畫面顯示指令模式的拒絕訊息和兩個選擇 |
| 7.19 | 游標放在 H 上，`P`，用 `Tab` 移到「雲端沒有的就傳回去」、按空白鍵勾起來，然後「確定」（`Enter` 一律是確定，不會勾選） | 完成；**下一次同步之後**，H 的雲端欄變回 ✓ |
| 7.20 | 對任何一列 `m`（只勾一列） | 提示「合併要先用空白鍵勾選至少兩個」，什麼都不做 |
| 7.21 | 勾兩列，`m`，等待視窗出現後按 `ctrl+q` | 不會直接離開，而是先中斷，等它停下來；`pgrep -fl "opencode run"` 沒有殘留 |
| 7.22 | `q` | 離開互動模式 |

**已知問題，驗收時請留意**（review 已回報，修好之後就把這幾行拿掉；勾選框只能用滑鼠點（S1）、未匯入頁的 `P`（Q1）已修好，那兩行已拿掉）：
- 7.19 傳回去之後，✗ 要等下一次完整同步才會變回 ✓（T2-sec3 R3／S4）。可以 `q` 離開，`rm -f /tmp/agora-acc/state/last-sync`，再執行 `agora`。

---

## 舊版還有效、這次沒有改的部分

舊版 `docs/acceptance.md` 的第 3～5 節（換 claude 接續、Claude 回覆到一半按 Ctrl-C、`/clear`）行為沒有變，可以照舊做，**只差一點**：continue 現在印出的是**原本那一個 id**，不是新的 id。做了 claude 的部分，清理時要加做下面的 Claude 那一行。

---

## 清理

⚠️ **先確認沒有整合測試或 e2e 正在跑**（它們也用 `agora-test`）。下面第一段會把 `agora-test/sessions/` 底下的東西一個一個移到 Drive 垃圾桶。

全部用寫死的路徑，沒有任何會變空的變數或佔位字（和舊版一樣）。

```bash
# Drive：只清 agora-test 底下的 sessions/
FID=$(rclone --config ~/.config/agora/rclone.conf lsjson gdrive: --dirs-only \
  | python3 -c "import json,sys; print(next(e['ID'] for e in json.load(sys.stdin) if e['Name']=='agora-test'))")
test -n "$FID" && for d in $(rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" lsf gdrive:sessions --dirs-only); do
  rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" purge "gdrive:sessions/${d%/}"
done
```

opencode 的驗收 session（s1～s5，加上接續時建的），在專案目錄裡**一個一個**依 id 刪，不要批次刪：

```bash
cd /tmp/agora-acc/proj
opencode session list            # 只會有這次驗收建的 ses_…
opencode session delete <ses_id> # 一個一個刪
```

有做 claude 的部分才需要（這個資料夾只有這次驗收建的檔）：

```bash
rm -rf "$HOME/.claude/projects/-private-tmp-agora-acc-proj"
```

最後（兩台「機器」的快取和狀態都在這底下）：

```bash
cd ~ && rm -rf /tmp/agora-acc
```

---

## 給 PM：這份草稿和舊版的差別

- 拿掉了 `cache`／`sync`，改成第 2 節的 `pull`／`push`。
- 第 3 節：continue 寫回同一個 id（舊版是「印出新的 id，記為 B」）。
- 新增第 1、4、5、6 節（批次、重跑、merge 沿用、雲端沒有），還有第二段的 T2。
- 用 `env2.sh`（另一組快取和狀態目錄）模擬「另一台機器刪掉」，不需要真的第二台機器；用 `rm -f …/last-sync` 讓同步不被 5 分鐘的節流擋住。
- 舊版第 6～9 節（merge 再接續、edit、delete、search）的重點，分別併進了第 5、2、4、6 節；舊版的「merge 再接續」那一步我沒有放進來，需要的話，可以把舊版第 6 節的後半段照舊附上。
- 「已知問題」那一段是根據目前還沒修的 review 意見寫的，修好之後就拿掉對應的那一行。

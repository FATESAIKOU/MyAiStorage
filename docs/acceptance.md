# Agora lite 人工驗收清單

給使用者在自己的終端機，一步一步做。涵蓋 T1（指令模式）、T2（互動模式）、T3（先存本機、背景上傳與刪除）、T6（大 Session 的預覽）。

依 HEAD `e3863f0` 之後的行為寫；第 16 節（T6，大 Session 的預覽）依 `9c74220`（T6 和它的 Y1、Y2、Y6 修正）和 ticket `docs/tickets/T6-lazy-preview.md`。「應該看到」的字，是 review 在副本裡用假 Drive、假 agent 實際跑出來的。真的 Drive 上，時間會不一樣，字是一樣的。

**規則**
- 只用 Drive 上的 `agora-test`，以及 `/tmp/agora-acc` 底下的目錄。
- 對話一律用自編的短句（例如「把 CSV 轉成 Markdown 表格，先列三個步驟」），不要貼真實的內容。
- 每一步只做一件事。做完看「應該看到」，對了再做下一步。
- 印出來的 `agora:<ULID>` 都記下來（A、B、C…）。表格裡的 `<A>` 就是換成 A 的完整 id。
- `echo $?` 是看上一個指令的 exit code。
- 不對的地方，記下步驟編號和畫面上的字，告訴 PM。

---

## 0. 換成這一版

你平常用的是穩定版（`9baa0e5`）。驗收要用這個工作目錄的版本。

| # | 做 | 應該看到 |
|---|---|---|
| 0.1 | `cd ~/.herdr/worktrees/MyAiStorage/phase1-spike && git status --short src` | **什麼都沒印**。有印東西，就是有人還沒 commit，先停下來問 PM |
| 0.2 | `uv tool install --force --editable .` | 安裝完成 |
| 0.3 | `cat "$(uv tool dir)/agora/uv-receipt.toml"` | `requirements` 那一行有 `editable = "…/phase1-spike"` |

**Drive**：已經是你自己的 OAuth client。它看不到舊 client 建的 `agora-test/`，所以第一次用 `agora-test` 時，agora 會建一個新的。這是正常的。

## 1. 準備（只做一次）

```bash
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
# 看 Drive 的 agora-test/sessions：不給參數列出 ULID；給 ULID 列那個資料夾；再給檔名就印那個檔
cat > /tmp/agora-acc/drive.sh <<'EOF'
#!/bin/sh
FID=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.config/agora/config.json')))['folders']['agora-test'])") || exit 1
R="rclone --config $HOME/.config/agora/rclone.conf --drive-root-folder-id $FID"
if [ -n "$2" ]; then $R cat "gdrive:sessions/$1/$2"
elif [ -n "$1" ]; then $R lsf "gdrive:sessions/$1"
else $R lsf gdrive:sessions --dirs-only; fi
EOF
source /tmp/agora-acc/env.sh
git init -q && git commit -q --allow-empty -m init
```

⚠️ **每一節的第一步都是 `source /tmp/agora-acc/env.sh`，看到 `[acc] 機器 1 … ✓` 才往下做。** 沒有 source 的話，agora 會寫進正式的 `agora/` 和平常的資料夾。

⚠️ **「機器 2」只是另一組資料夾**，用來模擬另一台機器。用完一定要 `source /tmp/agora-acc/env.sh` 回到機器 1。

⚠️ **每一個 `continue` 都寫 `--dir /tmp/agora-acc/proj`。**

⚠️ **「拿住上傳鎖」**：有幾步要假裝背景正在上傳。做法是在**第二個終端機**跑下面這一行，它會佔住上傳鎖 N 秒（把 `60` 換成要的秒數）。它不是 agora 的程式，只是佔住那把鎖：

```bash
python3 -c 'import fcntl,time; f=open("/tmp/agora-acc/state/upload.lock","a"); fcntl.flock(f, fcntl.LOCK_EX); print("鎖住"); time.sleep(60)'
```

準備自編的 opencode 對話（**不要匯入**）。在 `/tmp/agora-acc/proj` 跑 `opencode`，說一句話、離開；重複做出下面這些，然後用 `opencode session list` 記下每一個 `ses_…`：
- s1、s2、s3（第 2 節）
- s4（第 5 節）
- s5（第 10 節）
- s6（第 12 節，互動模式）
- s7、s8、s9、s10（第 16 節，大 Session 的預覽）

背景上傳通常幾秒就做完（自己的 client 每次約 0.7 秒）。表格裡寫「等 10 秒」就夠了。背景的訊息不會出現在終端機，而是寫在 `/tmp/agora-acc/state/upload.log`。

---

# 第一段：指令模式

## 2. import 一次多個，背景上傳

| # | 做 | 應該看到 |
|---|---|---|
| 2.1 | `source /tmp/agora-acc/env.sh` | `[acc] 機器 1 … ✓` |
| 2.2 | `rm -f /tmp/agora-acc/state/last-sync` | 沒有輸出（讓開頭的同步一定會跑，量的是最慢的情況） |
| 2.3 | `time agora import session --external-session-id s1,s2 --external-session-id s3 --agent opencode` | stdout 三行 `agora:<ULID>`（記為 A、B、C）。stderr 有 `[agora] 匯入 1/3  s1`、`2/3`、`3/3`。**沒有**「上傳失敗」。**記下 `real` 的秒數** |
| 2.4 | `echo $?` | `0` |
| 2.5 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox` | 什麼都沒印（都傳完了） |
| 2.6 | `sh /tmp/agora-acc/drive.sh` | 列出 A、B、C 的 ULID |
| 2.7 | `sh /tmp/agora-acc/drive.sh <A的ULID>` | `session.md` 和一個 `raw-….json` |
| 2.8 | `ls /tmp/agora-acc/cache/sessions/<A的ULID>/` | **本機也有** `session.md` 和同一個 `raw-….json` |
| 2.9 | 再跑一次 2.3 | 印出**同樣的**三個 id，沒有多出新的 Session |
| 2.10 | `agora import session --external-session-id s1,ses_nope --agent opencode; echo $?` | stdout 印出 A；stderr 有 `[agora] ses_nope 匯入失敗：…`；exit `2` |

## 3. pull、push

| # | 做 | 應該看到 |
|---|---|---|
| 3.1 | `agora pull session; echo $?` | `[agora] pull 要給 session id；要全部就在互動模式按 a，或從 agora search session 用管線接過來`，exit `1` |
| 3.2 | `agora pull session <A>` | stderr `[agora] 已經是新的，略過 1 個`；stdout `[agora] 拉下 0 個`。在這台匯入的，本機本來就有完整的一份 |
| 3.3 | `agora pull session <s1 的 ses_…>; echo $?`（不寫前綴） | stderr `… 拉不到：… 是 agent 的 session id，請寫前綴（opencode: 或 claude:）`；stdout `[agora] 拉下 0 個，1 個失敗`；exit `2` |
| 3.4 | `agora pull session opencode:<s1 的 ses_…>` | `ls /tmp/agora-acc/cache/reading/opencode/` 有 `<s1>.md` |
| 3.5 | `agora edit session <A> --header 'title=驗收改名'` | 印出同一個 A |
| 3.6 | `agora push session <A>` | stderr `[agora] push 1/1`；stdout `[agora] 寫回 1 個` |
| 3.7 | `agora push session opencode:<s1 的 ses_…>; echo $?` | `… 傳不上去：push 只吃 agora 的 session id，收到 opencode:…`；stdout `寫回 0 個，1 個失敗`；exit `2` |

## 4. 接續寫回原本那一個

| # | 做 | 應該看到 |
|---|---|---|
| 4.1 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj` | opencode 打開，畫面上已經有 A 的對話 |
| 4.2 | 在 opencode 說一句自編的話（例如「把第二步寫詳細一點」），離開 | 印出的是**同一個 A** |
| 4.3 | `agora show session <A> \| tail -5` | 看得到 4.2 說的那句話 |
| 4.4 | 等 10 秒，`ls /tmp/agora-acc/cache/sessions/<A的ULID>/ \| grep -c '^raw-'` | `1`（只留新的原始檔） |
| 4.5 | 再做一次 4.1，打開之後**什麼都不說**就離開 | stderr `[agora] 這次沒有新內容，沒有存` |

## 5. import 完馬上 edit，不會遺失

| # | 做 | 應該看到 |
|---|---|---|
| 5.1 | `N=$(agora import session --external-session-id s4 --agent opencode) && agora edit session $N --header 'title=馬上改'; echo $N` | 印出 N 的 id。edit 緊接在 import 後面，背景多半還在傳第一版 |
| 5.2 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox` | 什麼都沒印 |
| 5.3 | `sh /tmp/agora-acc/drive.sh <N的ULID> session.md \| grep title` | `title: 馬上改`（Drive 上是新的版本） |
| 5.4 | `agora show session $N \| grep title` | 本機也是 `title: 馬上改` |

## 6. delete：馬上從本機消失，背景移到垃圾桶

| # | 做 | 應該看到 |
|---|---|---|
| 6.1 | `agora delete session <B> <C>; echo $?` | `[agora] 會把這 2 個移到 Drive 垃圾桶：`，下面列出兩個，最後是 `確定的話加 --yes`；exit `1` |
| 6.2 | `agora delete session <B> <C> --yes; echo $?` | stdout 印出 B、C；stderr `[agora] 刪除 1/2`、`2/2`、`[agora] 已從本機刪除 2 個，背景移到 Drive 垃圾桶`；exit `0`；指令馬上結束 |
| 6.3 | 馬上 `agora search session --no-sync \| grep -c -e <B的ULID> -e <C的ULID>` | `0`（已經不在清單裡） |
| 6.4 | 等 10 秒，`ls -A /tmp/agora-acc/state/trash-queue` | 什麼都沒印 |
| 6.5 | `sh /tmp/agora-acc/drive.sh` | 沒有 B、C。Drive 網頁的垃圾桶裡有這兩個資料夾 |
| 6.6 | 再跑一次 6.2，`echo $?` | stderr `[agora] 已經不在了，略過 2 個：agora:…、agora:…`；exit `0` |
| 6.7 | `agora delete session agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ --yes; echo $?` | `[agora] 找不到：agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ`；exit `1` |

## 7. merge 被中斷之後沿用

先做兩個來源：`agora import session --external-session-id s2,s3 --agent opencode`（B、C 刪掉了，這次會是新的 id，記為 B2、C2）。

| # | 做 | 應該看到 |
|---|---|---|
| 7.1 | `agora merge session <B2>, <C2> --agent opencode` | stderr 先是 `來源 1/2`，然後 `請 opencode 寫 <B2> 的要約（… 字，不開畫面，可能要幾分鐘）…` |
| 7.2 | 看到 `來源 2/2` 時按 **Ctrl-C**，然後 `echo $?` | `[agora] 中斷了；重跑同一個指令會沿用已寫好的要約`；exit `130` |
| 7.3 | 再跑一次 7.1 | `[agora] <B2> 的要約沿用上次寫好的`，**只**為 C2 叫一次 opencode；最後印出新的 id（記為 G） |
| 7.4 | `agora show session <G>` | 「## 要約」底下 B2、C2 各一節；標頭有 `status: draft` |
| 7.5 | `opencode session list` | **沒有**多出寫要約用的 session |

## 8. 被別台刪掉之後：保留、拒絕、救回、刪掉

這一節用「機器 2」刪掉 A。**不用先 pull**：在這台寫的，本機本來就有完整的一份。

| # | 做 | 應該看到 |
|---|---|---|
| 8.1 | `source /tmp/agora-acc/env2.sh` | `[acc] 機器 2 … ✓` |
| 8.2 | `agora delete session <A> --yes` | 機器 2 刪掉了 A（它會先同步，所以認得 A） |
| 8.3 | 等 10 秒，`ls -A /tmp/agora-acc/state2/trash-queue` | 什麼都沒印 |
| 8.4 | `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync` | 回到機器 1 |
| 8.5 | `agora search session` | A 那一行的最後是 `(雲端沒有)`。A 還在，沒有被清掉 |
| 8.6 | `agora search session --filter cloud=no` | 只列出 A |
| 8.7 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj; echo $?` | **不會**打開 opencode。`[agora] agora:<A> 雲端沒有（別台機器刪掉了），不再寫回去；要傳回去用 agora push session … --not-exist-upload，要刪掉本機這份用 agora pull session … --not-exist-delete`；exit `1` |
| 8.8 | `agora edit session <A> --header 'title=x'; echo $?` | 同一句；exit `1` |
| 8.9 | `agora merge session <A>, <G> --agent opencode; echo $?` | 拒絕；exit `1` |
| 8.10 | `agora pull session <A>` | stderr `[agora] <A的ULID> 雲端沒有，本機的不動`；A 還在（stdout 是 `拉下 1 個`，見已知問題） |
| 8.11 | `agora push session <A>` | stderr `[agora] <A的ULID> 雲端沒有，沒有傳`；stdout `[agora] 寫回 0 個` |
| 8.12 | `agora push session <A> --not-exist-upload; echo $?` | stdout `[agora] 寫回 1 個`；exit `0` |
| 8.13 | `sh /tmp/agora-acc/drive.sh <A的ULID>` | `session.md` 和**一個** `raw-….json` |
| 8.14 | `rm -f /tmp/agora-acc/state/last-sync && agora search session \| grep <A的ULID>` | 沒有 `(雲端沒有)` 了 |
| 8.15 | 再做一次 8.1～8.4（機器 2 再刪一次 A），然後 `agora search session` | A 又是 `(雲端沒有)` |
| 8.16 | `agora pull session <A> --not-exist-delete` | stderr `[agora] <A的ULID> 雲端沒有，本機的副本已刪` |
| 8.17 | `agora show session <A>; echo $?` | `[agora] 找不到 agora:<A>`；exit `1` |

## 9. 改到一半被別台刪掉，另存成新的 Session

用「拿住上傳鎖」讓這台的修改先留在本機，機器 2 刪掉之後再放開。

**9a：edit**（另存的 Y 沿用原本的 relation）

| # | 做 | 應該看到 |
|---|---|---|
| 9.1 | **終端機 B**：「拿住上傳鎖」那一行，`60` 改成 `120` | `鎖住` |
| 9.2 | **終端機 A**：`source /tmp/agora-acc/env.sh && agora edit session <N> --header 'title=改到一半'` | 印出 N |
| 9.3 | `ls -A /tmp/agora-acc/state/outbox/<N的ULID>/` | 有 `.update`、`session.md`、`raw-….json`（還沒傳） |
| 9.4 | `source /tmp/agora-acc/env2.sh && agora delete session <N> --yes` | 機器 2 刪掉 N |
| 9.5 | 等 10 秒，`ls -A /tmp/agora-acc/state2/trash-queue` | 什麼都沒印 |
| 9.6 | 等終端機 B 結束。然後 `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync && agora search session > /dev/null` | stderr 有 `[agora] outbox 有 1 筆未上傳`。這個指令會啟動背景 |
| 9.7 | 等 10 秒，`agora search session --no-sync > /dev/null` | stderr 第一行：`[agora] <N的ULID> 已被別台刪除，這次的修改存成了 <新的ULID>`（記為 Y）。這一句只說一次 |
| 9.8 | `sh /tmp/agora-acc/drive.sh` | 有 Y，**沒有** N（N 沒有被傳回去） |
| 9.9 | `agora show session <Y> \| head -40` | `title: 改到一半`；`parents` 裡最後一個是 `agora:<N>`；`relation` 和 N 原本的一樣（`import`） |

**9b：continue**（另存的 Y 是 `continue`）

| # | 做 | 應該看到 |
|---|---|---|
| 9.10 | 終端機 B：再拿住上傳鎖 120 秒 | `鎖住` |
| 9.11 | 終端機 A：`agora continue session <G> --agent opencode --dir /tmp/agora-acc/proj`，說一句自編的話，離開 | 印出 G |
| 9.12 | `source /tmp/agora-acc/env2.sh && agora delete session <G> --yes` | 機器 2 刪掉 G |
| 9.13 | 等終端機 B 結束，然後照 9.6、9.7 做 | 提醒 `<G的ULID> 已被別台刪除，這次的修改存成了 <新的ULID>`（記為 Z） |
| 9.14 | `agora show session <Z> \| head -40` | `relation: continue`；`parents` 裡最後一個是 `agora:<G>`；內文看得到 9.11 說的那句話 |

## 10. push 會等背景傳完

| # | 做 | 應該看到 |
|---|---|---|
| 10.1 | 終端機 B：拿住上傳鎖，`60` 改成 `30` | `鎖住` |
| 10.2 | 終端機 A（30 秒內）：`source /tmp/agora-acc/env.sh && P=$(agora import session --external-session-id s5 --agent opencode) && time agora push session $P` | import 馬上結束。push **一直等**，大約每 10 秒說一次 `[agora] 背景上傳中，還在等…`；終端機 B 結束後，stdout `[agora] 寫回 1 個` |
| 10.3 | `sh /tmp/agora-acc/drive.sh <P的ULID>` | 已經有 `session.md` 和 `raw-….json` |
| 10.4 | 再做一次 10.1；終端機 A：`agora push session $P`，等 3 秒按 **Ctrl-C**，`echo $?` | 中斷，exit `130` |

## 11. 刪除排隊時的提醒

| # | 做 | 應該看到 |
|---|---|---|
| 11.1 | 終端機 B：拿住上傳鎖，`60` | `鎖住` |
| 11.2 | 終端機 A：`agora delete session <P> --yes` | `已從本機刪除 1 個，背景移到 Drive 垃圾桶`（背景拿不到鎖，還沒移） |
| 11.3 | `agora pull session <P>; echo $?` | stderr `[agora] <P> 拉不到：<P的ULID> 正在刪除，不能 pull`；stdout `[agora] 拉下 0 個，1 個失敗`；exit `2` |
| 11.4 | 等終端機 B 結束，然後 `agora search session --no-sync > /dev/null` | stderr `[agora] 有 1 個等著移到 Drive 垃圾桶`（`--no-sync` 不會啟動背景，所以還在等） |
| 11.5 | `rm -f /tmp/agora-acc/state/last-sync && agora search session > /dev/null`，等 10 秒 | 這次會啟動背景。`ls -A /tmp/agora-acc/state/trash-queue` 什麼都沒印 |
| 11.6 | `agora search session --no-sync > /dev/null` | 沒有提醒了 |

---

# 第二段：互動模式

在終端機執行 `agora`（不帶參數）。先 `source /tmp/agora-acc/env.sh`。

## 12. 匯入、勾選、篩選

| # | 按鍵 | 應該看到 |
|---|---|---|
| 12.1 | （打開時） | 上面是「Agora」「未匯入」兩頁；Agora 頁有「雲端」欄 |
| 12.2 | 看最下面的按鍵列 | 有 `空白`、`a`、`enter 接續`、`m`、`e`、`d`、`p`、`P`、`/`、`ctrl+t`、`q` |
| 12.3 | `Tab` | 換到未匯入頁；按鍵列**沒有** `m`、`e`、`d`、`P`；Enter 寫的是「匯入」 |
| 12.4 | 游標放在 s6，`Enter` | 等待視窗很快結束（不等上傳）；結果視窗寫 **「已經存在本機，背景上傳中」** |
| 12.5 | 關掉結果視窗，`Tab` 回 Agora 頁 | 多了剛匯入的那一列（記為 Q）。雲端欄是「未上傳」或 ✓（背景很快，看不到「未上傳」也正常） |
| 12.6 | `a` | 看得到的列全部打勾；游標還在原本那一列 |
| 12.7 | `a` | 全部取消 |
| 12.8 | 勾兩列，`/`，打其中一列標題裡的一個字，`Enter` | 只剩看得到的列；標題列寫「另有 1 個勾選被篩選掉」 |
| 12.9 | `d` | 確認視窗只列出看得到的那一個，停在「取消」。直接 `Enter` → 什麼都不做。`/` → 清空 → `Enter` 取消篩選 |

## 13. 合併、中斷、沿用

| # | 按鍵 | 應該看到 |
|---|---|---|
| 13.1 | 勾兩列，`m`，選 opencode | 等待視窗；寫第 2 個來源的要約時按 **`Esc`** |
| 13.2 | （13.1 之後） | 結果視窗標題「合併（已中斷）」，第一行「已中斷；重跑同一個動作會接著做」；關掉之後兩列**還勾著** |
| 13.3 | 另開終端機：`pgrep -fl "opencode run"` | 沒有殘留 |
| 13.4 | 再按一次 `m`，選 opencode | 沿用寫好的那一節；結果視窗「完成」；最上面多一個合併出來的 Session（記為 J） |
| 13.5 | 勾兩列，`m`，等待視窗出現後按 `ctrl+q` | 先中斷、等它停下來，不會直接離開；`pgrep -fl "opencode run"` 沒有殘留 |
| 13.6 | 只勾一列，`m` | 提示「合併要先用空白鍵勾選至少兩個」 |

## 14. pull、push、刪除的說法

| # | 按鍵 | 應該看到 |
|---|---|---|
| 14.1 | 游標在 J，`p` | 確認視窗「把 1 個拉到本機？」，勾選框 `[ ] 雲端沒有的就刪掉本機的（等同 --not-exist-delete）`，下面一行「雲端沒有的：印一行提醒，本機的不動」。`Tab` 到勾選框、`空白` → 變成 `[x]`，下面一行變成「⚠ 勾了：雲端沒有的，本機這份會被刪掉」；再按 `空白` 取消。`shift+tab`、`↓` 到「確定」、`Enter` → 完成 |
| 14.2 | 游標在 J，`P` | 「把 1 個寫回 Drive？」；直接 `Enter`（停在取消）→ 什麼都不做 |
| 14.3 | `q` 離開。用機器 2 刪掉 J（`source /tmp/agora-acc/env2.sh && agora delete session <J> --yes`，再 `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync`），然後 `agora` | J 那一列的雲端欄是 **✗** |
| 14.4 | 游標在 J，`Enter`（接續） | 不會打開 agent；視窗「不能接續」，內容是 8.7 那一句 |
| 14.5 | 游標在 J，`d`，`↓` 到「確定」，`Enter` | 結果視窗只寫 **「已從本機刪除」**（雲端本來就沒有，不說背景移到垃圾桶） |
| 14.6 | 游標在 Q（✓ 的那一列），`d`，`↓` 到「確定」，`Enter` | 結果視窗寫 **「已從本機刪除，背景移到 Drive 垃圾桶」** |
| 14.7 | `q` | 離開 |

## 15. 「未上傳」看得到

| # | 做 | 應該看到 |
|---|---|---|
| 15.1 | 終端機 B：拿住上傳鎖，`60` | `鎖住` |
| 15.2 | 終端機 A：`agora`，`Tab` 到未匯入頁，隨便匯入一列（如果沒有了，先用 opencode 再說一段話） | 結果視窗「已經存在本機，背景上傳中」 |
| 15.3 | `Tab` 回 Agora 頁 | 那一列的雲端欄是 **「未上傳」** |
| 15.4 | `q` 離開，等終端機 B 結束。`rm -f /tmp/agora-acc/state/last-sync && agora search session > /dev/null`，等 10 秒，再 `agora` | 那一列變成 **✓** |

## 16. 大 Session 的預覽（T6）

要看的是：很大的 Session，預覽只讀、只排最後一段，所以游標移動很順；往上捲才補前面的。

驗收不能用真實的內容，所以用一支小程式，把**自編的文字**接在 Session 的本機鏡像後面，做出幾 MB 的大 Session。這只改 `/tmp/agora-acc` 裡的檔案，不會傳到 Drive（只要不對它們 push）。

往上捲時，每到頂一次補一段（約 30 KB）。數字和 `第 N 則` 接得上、畫面不跳，就是對的。

先建那支小程式（只做一次）：

```bash
cat > /tmp/agora-acc/grow.py <<'EOF'
import sys, pathlib
# 用法：grow.py <檔案> <KB> [new]　把自編的對話接在檔案後面（new：整個檔案重寫）
path, kb = pathlib.Path(sys.argv[1]), int(sys.argv[2])
fresh = len(sys.argv) > 3 and sys.argv[3] == "new"
parts, size, n = [], 0, 0
while size < kb * 1024:
    n += 1
    one = (f"## user\n第 {n} 則：把 CSV 轉成 Markdown 表格，這是驗收用的自編文字。" + "表格" * 150
           + f"\n\n## assistant\n第 {n} 則的回覆：" + "好的" * 150 + "\n\n")
    parts.append(one)
    size += len(one.encode())
parts.append("## user\n（最後一則）\n")
path.parent.mkdir(parents=True, exist_ok=True)
with open(path, "w" if fresh else "a", encoding="utf-8") as f:
    if not fresh:
        f.write("\n")
    f.write("".join(parts))
print(f"{path.name}：加了 {n} 則，約 {size // 1024} KB")
EOF
```

| # | 做 | 應該看到 |
|---|---|---|
| 16.1 | `source /tmp/agora-acc/env.sh && agora import session --external-session-id s7,s8,s9 --agent opencode` | 印出三個 id（記為 T1、T2、T3） |
| 16.2 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox` | 什麼都沒印（都傳上去了） |
| 16.3 | `python3 /tmp/agora-acc/grow.py /tmp/agora-acc/cache/sessions/<T1的ULID>/session.md 2000` | `session.md：加了 … 則，約 2000 KB` |
| 16.4 | `python3 /tmp/agora-acc/grow.py /tmp/agora-acc/cache/sessions/<T2的ULID>/session.md 2000` | 同上 |
| 16.5 | `python3 /tmp/agora-acc/grow.py /tmp/agora-acc/cache/sessions/<T3的ULID>/session.md 100` | `… 約 100 KB`（小一點，才捲得完） |
| 16.6 | `python3 /tmp/agora-acc/grow.py /tmp/agora-acc/cache/reading/opencode/<s10 的 ses_…>.md 2000 new` | `… 約 2000 KB`（這是未匯入分頁用的全文快取；s10 **不要**匯入） |

**Agora 分頁**（`agora`，不帶參數）

| # | 做 | 應該看到 |
|---|---|---|
| 16.7 | 游標移到 T1，按住 `↓` `↑` 在 T1、T2、T3 和旁邊幾列之間快速移動 | 游標跟得上按鍵，**不卡**。移動時預覽不換；停下來約 0.15 秒後才換成游標那一列 |
| 16.8 | 停在 T1 | 預覽最下面是 `（最後一則）`；往上一點是 `第 N 則` 這種自編的句子。預覽區最上面有一行 **`↑ 往上捲載入更早的內容（還有約 2000 KB）`**（數字大約就好） |
| 16.9 | 記下預覽區最上面看得到的 `第 N 則` 是幾號 | 例如 `第 1503 則` |
| 16.10 | 滑鼠移到右邊的預覽區，用滾輪**往上**捲到頂 | 補上了更早的一段：那一行的 KB 變少（約少 30）。**畫面不跳**：16.9 記下的那一則，還在剛才的位置附近，它上面多了號碼比較小的幾則，號碼是接著的 |
| 16.11 | 游標移到 T3（約 100 KB），在預覽區一直往上捲到頂，重複幾次 | 每到頂一次，KB 就少一些；最後那一行**消失**。最上面是 T3 原本的第一則（你在 opencode 說的那句話），**沒有** `---`、`title:` 這種標頭 |
| 16.12 | T3 全部載完後，往下捲到底，再往上捲到頂 | 內容**沒有重複**：`（最後一則）` 只出現一次，最上面還是原本的第一則 |

**未匯入分頁**

| # | 做 | 應該看到 |
|---|---|---|
| 16.13 | `Tab` 到未匯入頁，游標移到 s10 | 先顯示 `最後一則（…），整份對話載入中…`，很快換成 `整份對話（閱讀版）`。最下面是 `（最後一則）`；最上面有 `↑ 往上捲載入更早的內容（還有約 2000 KB）` |
| 16.14 | 在預覽區往上捲到頂 | 補上前一段，KB 變少，畫面不跳 |
| 16.15 | 按住 `↓` `↑` 在 s10 和旁邊幾列之間快速移動 | 不卡；停下來才換預覽 |
| 16.16 | `q` | 離開 |

⚠️ T1、T2、T3 的本機鏡像被加長了，之後**不要**對它們 `push`（會把加長的內容傳到 `agora-test`；傳了也只是測試資料夾，清理時會一起清掉）。

---

# 第三段：換 claude 接續（選做，有裝 claude 才做）

| # | 做 | 應該看到 |
|---|---|---|
| 17.1 | `agora continue session <Q2> --agent claude --dir /tmp/agora-acc/proj`（Q2 是任何一個雲端欄 ✓ 的 Session） | Claude 打開時，畫面上已經有之前的對話（開頭多一則說明：這是轉過來的紀錄） |
| 17.2 | 說一句自編的話，用 `/exit` 離開 | 印出**同一個** Q2 |
| 17.3 | 再接一次；送一句話，**在它回覆到一半時按一次 `Ctrl-C`**（只中斷這一輪回覆），再 `/exit` | agora 沒有跟著被中斷，印出 Q2；`agora show session <Q2>` 看得到那一段 |
| 17.4 | 再接一次；說一句話，`/clear`，再說一句話，`/exit` | `/clear` 之前的照常存回 Q2；另外印一行 `這次接續中用過 /clear，之後的對話在 Claude session <uuid>；要存進 Agora 請另外執行：agora import session --external-session-id <uuid> --agent claude` |
| 17.5 | 照那一行執行 | 印出新的 `agora:<ULID>` |

---

## 給 PM 填的數字

| 項目 | 數字 |
|---|---|
| 2.3：import 3 個的前景時間（`real`） | ___ 秒 |
| 10.2：push 等了多久（`real`） | ___ 秒 |
| 16.7、16.15：大 Session 上移動游標的感覺 | 順／卡 |

## 已知問題（Low，不算驗收失敗）

- 16.13：未匯入分頁「整份對話載入中…」的那一瞬間，以及 Agora 分頁沒有任何列的時候，預覽最上面會多一行 `↑ 往上捲載入更早的內容（還有約 1 KB）`（review T6 Z1）。
- 8.10：pull 遇到「雲端沒有，本機的不動」，什麼都沒拉，stdout 還是寫 `拉下 1 個`（review T1-4.2d D2）。
- 救回之後（8.14、14.x），互動模式的雲端欄要等下一次完整同步，才從 ✗ 變回 ✓。可以 `q` 離開、`rm -f /tmp/agora-acc/state/last-sync`、`agora search session`，再打開。
- 9.7 之後的 `search`，可能多一行 `agora:<Y> 與 agora:<N> 來自同一個來源 Session`：Y 是 N 另存出來的，所以來源相同。
- 背景程序**啟動失敗**時，指令會 exit 3。delete 的訊息是 `背景上傳啟動失敗，移到 Drive 垃圾桶要等之後的指令`，其他寫入是 `上傳失敗，已存入 outbox，之後的指令會自動再送`。驗收時做不出這種情況，單元測試有測。互動模式裡 delete 遇到 exit 3 時，結果視窗用的是「已存進 outbox…」這句通用的說法（review T3-final2 G4）。
- 列不出 Drive（例如連線一瞬間斷掉）的那一輪，「更新既有 Session」的修改不會送出，留到下一輪。新建的照常送。這是刻意的（不把別台剛刪掉的傳回去）。
- 背景上傳在比對到一半當掉的話，那一版會留在 `outbox/.done-<ULID>`；下一個指令會把它算成「未上傳」，並啟動背景把它送出去。驗收時不用特別做。

---

## 清理

⚠️ **先確認沒有整合測試在跑**（它們也用 `agora-test`）。

Drive：只清 `agora-test/sessions/` 底下的東西，一個一個移到垃圾桶：

```bash
FID=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.config/agora/config.json')))['folders']['agora-test'])")
test -n "$FID" && for d in $(rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" lsf gdrive:sessions --dirs-only); do
  rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" purge "gdrive:sessions/${d%/}"
done
```

opencode 的驗收 session，在專案目錄裡**一個一個**依 id 刪，不要批次刪：

```bash
cd /tmp/agora-acc/proj
opencode session list            # 只會有這次驗收建的 ses_…
opencode session delete <ses_id> # 一個一個刪
```

有做第三段（claude）才需要：

```bash
rm -rf "$HOME/.claude/projects/-private-tmp-agora-acc-proj"
```

最後：

```bash
cd ~ && rm -rf /tmp/agora-acc
```

換 client 之前建的舊 `agora-test/`，新的 client 看不到，上面的清理也碰不到。要不要從 Drive 網頁丟掉，你決定。

---

## 驗收之後：要用哪一版

先確認：`pgrep -fl agora.background` 什麼都沒印（沒有背景還在跑）。

**驗收通過**：這一版就是新的穩定版。現在的 editable 安裝會跟著工作目錄變（隊員之後的 commit 也會跑進來），所以建議換成固定的一份：

```bash
cd ~/.herdr/worktrees/MyAiStorage/phase1-spike
V=$(git rev-parse --short HEAD)
mkdir -p ~/.local/share/agora-stable/$V && git archive HEAD | tar -x -C ~/.local/share/agora-stable/$V
uv tool install --force ~/.local/share/agora-stable/$V
cat "$(uv tool dir)/agora/uv-receipt.toml"    # requirements 那一行是 directory = "…/agora-stable/<V>"
```

**驗收沒通過**，要回到原本的穩定版：

```bash
uv tool install --force ~/.local/share/agora-stable/9baa0e5
cat "$(uv tool dir)/agora/uv-receipt.toml"    # directory = "…/agora-stable/9baa0e5"
```

兩種情況都一樣：平常的資料夾（`~/.cache/agora`、`~/.local/state/agora`）不受驗收影響，因為驗收全部用 `/tmp/agora-acc`。

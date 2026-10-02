# Agora lite 人工驗收清單（草稿，給 PM 看過後換掉 docs/acceptance.md）

> review 起草，2026-10-03。依 HEAD 的行為寫：continue **寫回同一個 id**；`cache`／`sync` 已經拿掉，改成 `pull`／`push`；雲端沒有的 Session 會保留並標記。
>
> **第 2 版**（2026-10-03）：PM 用假資料實跑過兩段（`docs/tickets/T1-pm-run.md`、`docs/tickets/T2-pm-run.md`）。訊息改成 HEAD `a29fd0d` 實際印出的字（review 在副本裡用 fake rclone／fake agent 逐條印出來對過）；impl2 正在改 P3／P4 的訊息，那兩處標了「**改到一半**」，以改完之後的 HEAD 為準。實跑時發現兩個步驟照原本的順序會失敗（6.10、7.17），已經改了順序，見各步旁的說明。
>
> **第 3 版**（2026-10-03）：照 HEAD `428b0b3` 的程式更新：T1 的 P2～P4（`bbc7a4a`）、T2 的 Q1～Q4（`5ae265b`）都已經修好了，相關的步驟改成修好之後的畫面和訊息，已知問題也拿掉了這幾項。進度條在正常結束時會不會到 N/N（final-checks E2），impl1 正在改，那一處標了「**改到一半**」。

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
| 1.4 | `agora import session --external-session-id s1,ses_nope --agent opencode; echo $?` | stdout 印出 A；stderr 有 `[agora] ses_nope 匯入失敗：…`；exit `2`（第一個非零的） |
| 1.5 | `agora import session --external-session-id s1,s2 --agent opencode 2>/dev/null \| wc -l` | `2`（進度只在 stderr） |

## 2. pull／push

| # | 指令 | 算過 |
|---|---|---|
| 2.1 | `agora pull session; echo $?` | `[agora] pull 要給 session id；要全部就在互動模式按 a，或從 agora search session 用管線接過來`，exit `1` |
| 2.2 | `agora pull session <A>` | stderr 有 `[agora] pull 1/1`，**stdout** 有 `[agora] 拉下 1 個`；`ls /tmp/agora-acc/cache/sessions/<A的ULID>/` 有 `session.md` 和一個 `raw-….json` |
| 2.3 | 同 2.2 再跑一次 | 很快結束；stderr 有 `[agora] 已經是新的，略過 1 個`，stdout 是 `[agora] 拉下 0 個`（略過的不算拉下，T1 P3） |
| 2.4 | `agora pull session opencode:s1` | `ls /tmp/agora-acc/cache/reading/opencode/` 有 `s1.md` |
| 2.5 | `agora pull session <s1 的 ses_…>; echo $?`（不寫前綴） | stderr：`… 拉不到：ses_… 是 agent 的 session id，請寫前綴（opencode: 或 claude:）`；stdout：`[agora] 拉下 0 個，1 個失敗`；exit `2` |
| 2.6 | `agora edit session <A> --header 'title=驗收改名'` 之後 `agora push session <A>` | stderr 有 `[agora] push 1/1`，**stdout** 有 `[agora] 寫回 1 個` |
| 2.7 | `agora push session opencode:<s1 的 ses_…>; echo $?` | stderr：`… 傳不上去：push 只吃 agora 的 session id，收到 opencode:…`；exit `2` |

## 3. 接續會寫回原本那一個

| # | 指令 | 算過 |
|---|---|---|
| 3.1 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj` | opencode 打開時，畫面上已經有 A 的對話；說一句自編的話（例如「把第二步寫詳細一點」），離開 |
| 3.2 | （3.1 結束時） | 印出的是**同一個 A**，不是新的 id |
| 3.3 | `agora show session <A> \| tail -5` | 看得到 3.1 說的那句話 |
| 3.4 | 同 3.1，但打開之後什麼都不說就離開 | stderr：`[agora] 這次沒有新內容，沒有存` |

## 4. delete 多個與重跑

| # | 指令 | 算過 |
|---|---|---|
| 4.1 | `agora delete session <B> <C>; echo $?` | `[agora] 會把這 2 個移到 Drive 垃圾桶：`，下面列出兩個，最後是 `確定的話加 --yes`；exit `1` |
| 4.2 | `agora delete session <B> <C> --yes \| wc -l` | `2`；stderr 有 `刪除 1/2`、`2/2`，以及 `[agora] 已把 2 個移到 Drive 垃圾桶，30 天內可以在 Drive 網頁還原` |
| 4.3 | 同 4.2 再跑一次（等同中斷後重跑），然後 `echo $?` | stderr：`[agora] 已經不在了，略過 2 個：agora:…、agora:…`；exit `0` |
| 4.4 | `agora delete session agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ --yes; echo $?` | `[agora] 找不到：agora:01ZZZZZZZZZZZZZZZZZZZZZZZZ`，exit `1`（打錯的 id 不會被當成已刪） |

## 5. merge 被中斷之後沿用

先做兩個來源：`agora import session --external-session-id s2,s3 --agent opencode`（B、C 已經刪了，這次會印出新的 id，記為 B2、C2）。

| # | 指令 | 算過 |
|---|---|---|
| 5.1 | `agora merge session <B2>, <C2> --agent opencode` | stderr 先出現 `來源 1/2`，然後「請 opencode 寫 <B2> 的要約…」 |
| 5.2 | 看到 `來源 2/2` 和「請 opencode 寫 <C2> 的要約…」時，按 **Ctrl-C** | 中斷；stderr：`[agora] 中斷了；重跑同一個指令會沿用已寫好的要約`；`echo $?` 是 `130` |
| 5.3 | 同 5.1 再跑一次 | stderr 有 `[agora] agora:<B2> 的要約沿用上次寫好的`，**只**為 C2 叫一次 opencode；最後印出新的 id（記為 G） |
| 5.4 | `agora show session <G>` | 「## 要約」底下 B2、C2 各一節；標頭有 `status: draft` |
| 5.5 | `opencode session list` | **沒有**多出寫要約用的 session |

## 6. 雲端沒有的 Session（模擬別台刪掉）

| # | 指令 | 算過 |
|---|---|---|
| 6.0 | `agora pull session <A>` | **先把 A 現在的原始檔拿下來**。3.1 的接續把 A 寫回時換了一個新的原始檔，那個新檔只上傳到 Drive，不會留在本機；2.2 拿下來的是**舊的**那一個。不做這一步的話，6.10 會被拒絕（見 6.10 旁的說明、已知問題 T1 P1） |
| 6.1 | `source /tmp/agora-acc/env2.sh` 之後 `agora delete session <A> --yes` | 機器 2 把 A 移到 Drive 垃圾桶 |
| 6.2 | `source /tmp/agora-acc/env.sh` 之後 `rm -f /tmp/agora-acc/state/last-sync` | 回到機器 1，並且讓下一次同步不被節流 |
| 6.3 | `agora search session` | A 那一行的最後是 `(雲端沒有)`；A 還在機器 1 上，沒有被清掉 |
| 6.4 | `agora search session --filter cloud=no` | 只列出 A |
| 6.5 | `agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj; echo $?` | **不會**打開 opencode；訊息提示兩個選擇（`push … --not-exist-upload`、`pull … --not-exist-delete`）；exit `1` |
| 6.6 | `agora edit session <A> --header 'title=x'; echo $?` | 一樣拒絕，exit `1` |
| 6.7 | `agora merge session <A>, <G> --agent opencode; echo $?` | 拒絕，exit `1` |
| 6.8 | `agora pull session <A>` | stderr 只多一行 `[agora] <A的ULID> 雲端沒有，本機的不動`（印的是 ULID，沒有 `agora:`）；A 還在。stdout 是 `[agora] 拉下 1 個`（什麼都沒拉，卻還是算進「拉下」；這一種不在 P3 的範圍內，review T1-4.2d D2 記為 Low） |
| 6.9 | `agora push session <A>` | stderr：`[agora] <A的ULID> 雲端沒有，沒有傳`；stdout：`[agora] 寫回 0 個`；Drive 上還是沒有 A |
| 6.10 | `agora push session <A> --not-exist-upload` | stdout：`[agora] 寫回 1 個`；A 傳回 Drive；`rm -f /tmp/agora-acc/state/last-sync && agora search session` 之後，A 那一行**沒有** `(雲端沒有)`。⚠️ **這一步會過，只是因為 6.0 把 A 現在的原始檔拿下來了**。在這台機器上 import 或接續的 Session，本機只有 `session.md`，沒有原始檔（已知問題 T1 P1）；少了 6.0 的話，這一步會是 exit `2`、`… 傳不上去：標頭指到的 raw-….json 本機沒有，不傳半套`。review 在副本裡照原本的順序（2.2 → 3.1 → 6.10）跑過，確認會被拒絕，所以只靠 2.2 不夠 |
| 6.11 | 再做一次 6.1、6.2，然後 `agora search session --filter cloud=no \| awk '{print $1}' \| xargs agora pull session --not-exist-delete` | stderr：`[agora] <A的ULID> 雲端沒有，本機的副本已刪`；`agora search session` 裡沒有 A 了 |
| 6.12 | `agora show session <A>; echo $?` | `[agora] 找不到 agora:…`，exit `1` |

---

# 第二段：T2 互動模式

準備：`source /tmp/agora-acc/env.sh`，再用 opencode 多說兩段自編的短對話（s4、s5，**不要**匯入）。然後在終端機裡執行 `agora`（不帶參數就是互動模式；這一步由使用者自己做）。

| # | 按鍵 | 算過 |
|---|---|---|
| 7.1 | （打開時） | 上面是「Agora」「未匯入」兩頁；Agora 頁有「雲端」欄，目前的 Session 都是 ✓ |
| 7.2 | 看最下面的按鍵列 | 有 `空白`、`a`、`enter 接續`、`m`、`e`、`d`、`p`、`P`、`/`、`ctrl+t`、`q`；**沒有** `r`、`s` |
| 7.3 | `Tab` | 換到未匯入頁；按鍵列**沒有** `m`、`e`、`d`、`P`，Enter 寫的是「匯入」 |
| 7.4 | `空白` 勾 s4，`↓`，`空白` 勾 s5 | 兩列有 ✓，游標沒有因為勾選而跳走 |
| 7.5 | `Enter` | 等待視窗出現。進度條顯示的是**做完的**個數：收到 `匯入 1/2` 時是 0/2，收到 `2/2` 時是 1/2（T2 Q3）；正常結束時會不會先到 2/2 再關，**改到一半**（final-checks E2：目前視窗多半在畫出 2/2 之前就關了）。結果視窗寫「完成」；關掉之後回到清單，兩列不見了（已匯入），勾選清掉，狀態列是空的 |
| 7.6 | `Tab` 回 Agora 頁 | 剛匯入的兩個在最上面（記為 H、I），雲端欄是 ✓ |
| 7.7 | `a` | 看得到的列全部打勾；游標**還在原本那一列** |
| 7.8 | `a` | 全部取消 |
| 7.9 | 勾 H、I 兩列，然後 `/`，輸入 H 的標題裡的一個字，`Enter` | 只剩看得到的列；標題列寫著「另有 1 個勾選被篩選掉」 |
| 7.10 | `d` | 確認視窗「把 1 個移到 Drive 垃圾桶？」**只列出看得到的那一個**，停在「取消」，提示寫「Enter 選擇　Esc 取消」；直接 `Enter`（選的是停著的「取消」）→ 什麼都不做。`/` → 清空 → `Enter` 取消篩選 |
| 7.11 | 勾 H、I，`m`，選 opencode | 等待視窗出現；寫第 1 個來源的要約時，進度條是 0/2，寫第 2 個時是 1/2（顯示做完的個數）。在寫第 2 個的時候按 **`Esc`** |
| 7.12 | （7.11 之後） | 結果視窗的標題是「合併（已中斷）」，第一行「已中斷；重跑同一個動作會接著做」。**說明只在結果視窗裡**：關掉之後回到清單，狀態列是**空的**（T2 Q4）；H、I **還是勾著的** |
| 7.13 | 另開一個終端機：`pgrep -fl "opencode run"` | 沒有殘留的 opencode（寫要約的那個也停了） |
| 7.14 | 回到互動模式，再按一次 `m`，選 opencode | 這次會沿用已經寫好的那一節；結果視窗寫「完成」；關掉之後，最上面多一個合併出來的 Session（記為 **J**），H、I 的勾選清掉，狀態列是空的 |
| 7.15 | 游標放在 **J** 上，`p` | 確認視窗「把 1 個拉到本機？」，勾選框的標籤是 `[ ] 雲端沒有的就刪掉本機的（等同 --not-exist-delete）`（**沒有勾**），下面一行寫「雲端沒有的：印一行提醒，本機的不動」。（可以順便試：`Tab` 到勾選框、`空白` → 標籤變成 `[x] …`，下面那一行變成「⚠ 勾了：雲端沒有的，本機這份會被刪掉」；再按一次 `空白` 取消，變回 `[ ]`，T2 Q1。）`shift+tab` 回到選項、`↓` 到「確定」、`Enter` → 等待視窗 → 完成。這一步也把 J 的原始檔拿下來了，7.19 要用（已知問題 T1 P1） |
| 7.16 | 同一列（J），`P` | 確認視窗「把 1 個寫回 Drive？」，標籤是 `[ ] 雲端沒有的就傳回去（等同 --not-exist-upload）`，下面一行寫「雲端沒有的：印一行提醒，不傳；同名的檔案直接覆蓋」；直接 `Enter`（停在取消）→ 什麼都不做 |
| 7.17 | `q` 離開；在另一個終端機做 6.1、6.2，把 **J** 用機器 2 刪掉（`agora delete session <J> --yes`），再回機器 1 執行 `agora` | J 那一列的雲端欄是 **✗**。⚠️ 用的是 J，不是 H：7.14 之後 H、I 都有子 Session（J），機器 2 刪 H 會被拒絕（「有子 Session，不能刪」） |
| 7.18 | 游標放在 J 上，`Enter`（接續） | 不會打開 agent；視窗「不能接續」，內容是指令模式的拒絕訊息：`agora:<J> 雲端沒有（別台機器刪掉了），不再寫回去；要傳回去用 agora push session … --not-exist-upload，要刪掉本機這份用 agora pull session … --not-exist-delete` |
| 7.19 | 游標放在 J 上，`P`，`Tab` 移到勾選框、`空白` 勾起來，`shift+tab` 回到選項、`↓` 到「確定」、`Enter`（`Enter` 一律是「選目前停的那一個」，不會勾選） | 勾起來之後，標籤是 `[x] 雲端沒有的就傳回去…`，下面那一行是「⚠ 勾了：別台機器刪掉的 Session 會被傳回 Drive」；完成；**下一次同步之後**，J 的雲端欄變回 ✓（見已知問題 R3／S4） |
| 7.20 | 對任何一列 `m`（只勾一列） | 提示「合併要先用空白鍵勾選至少兩個」，什麼都不做 |
| 7.21 | 勾兩列，`m`，等待視窗出現後按 `ctrl+q` | 不會直接離開，而是先中斷，等它停下來；`pgrep -fl "opencode run"` 沒有殘留 |
| 7.22 | `q` | 離開互動模式 |

**已知問題，驗收時請留意**（修好之後就把對應的那一行拿掉；勾選框只能用滑鼠點（S1）、未匯入頁的 `P`（Q1）已修好，那兩行已拿掉）：

T1（`docs/tickets/T1-pm-run.md`）：
- **P1（要使用者決定）**：在這台機器上 import、接續或合併的 Session，本機只有 `session.md`，**沒有原始檔**（原始檔只有在 pull 的時候才拿下來）。所以別台刪掉之後，`push --not-exist-upload` 會拒絕（`標頭指到的 raw-….json 本機沒有，不傳半套`），pull 也拿不到了——實際上救不回來。驗收步驟裡用 6.0、7.15 先 pull 一次來避開這個問題。
- P2～P4 已經修好（`bbc7a4a`），這幾行拿掉了。剩下一個 Low：pull 遇到「雲端沒有，本機的不動」這種什麼都沒拉的情況，還是算進「拉下 N 個」（review T1-4.2d D2）。

T2（`docs/tickets/T2-pm-run.md`；Q1～Q4 已經修好（`5ae265b`），那幾行拿掉了）：
- **E2**（**改到一半**）：進度條顯示做完的個數，但正常結束時，視窗多半在畫出 N/N 之前就關了，所以看不到填滿（review final-checks E2；impl1 正在改）。
- **E3**：輸入工作目錄的那個視窗，提示也寫成了「Enter 選擇」，那裡其實是送出打好的文字（review final-checks E3，impl1 正在改）。
- **Q6**：雲端欄是 ✗ 的 Session，它原本的 agent session 會重新出現在未匯入頁（因為之後的 import 會建一個新的）；這符合 spec。
- **R3／S4**：7.19 傳回去之後，✗ 要等到下一次完整同步才會變回 ✓。可以 `q` 離開，`rm -f /tmp/agora-acc/state/last-sync`，再執行 `agora`。

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
- 「已知問題」那一段是根據目前還沒修的 review 意見，以及 PM 實跑的發現寫的，修好之後就拿掉對應的那一行。
- **第 2 版改了什麼**：
  - 各步的「算過」改成 HEAD 實際印出的字（1.4、2.1～2.7、3.4、4.x、5.2、5.3、6.8～6.12）；pull 和 push 的總結（「拉下 N 個」「寫回 N 個」）是印在 **stdout** 的。
  - 新增 6.0：3.1 的接續換掉了 A 的原始檔，2.2 拿下來的是舊的那一個，照原本的順序 6.10 會被拒絕。
  - 7.15～7.19 改用 7.14 合併出來的 J：H、I 在 7.14 之後都有子 Session，機器 2 刪 H 會被拒絕；而且 7.15 的 pull 正好把 J 的原始檔拿下來，給 7.19 用。
  - 7.10、7.12、7.14、7.16 加上了 PM 試用時看到的畫面（Q1～Q4）。
  - 已知問題加上了 T1 P1～P4、T2 Q1～Q4、Q6。
- **第 3 版改了什麼**：2.3、5.2、6.8 改成 P3／P4 修好之後的訊息；7.5、7.10、7.11、7.12、7.14、7.15、7.16、7.19 改成 Q1～Q4 修好之後的畫面（勾選框的 `[ ]`／`[x]` 和會跟著變的說明、「Enter 選擇」、進度條顯示做完的個數、結果視窗關掉之後狀態列是空的）；已知問題拿掉了 P2～P4、Q1～Q4，加上 E2（改到一半）、E3。

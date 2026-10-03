# Agora lite 人工驗收清單（草稿，給 PM 看過後換掉 docs/acceptance.md）

> review 起草，2026-10-03。依 HEAD 的行為寫：continue **寫回同一個 id**；`cache`／`sync` 已經拿掉，改成 `pull`／`push`；雲端沒有的 Session 會保留並標記。
>
> **第 2 版**（2026-10-03）：PM 用假資料實跑過兩段（`docs/tickets/T1-pm-run.md`、`docs/tickets/T2-pm-run.md`）。訊息改成 HEAD `a29fd0d` 實際印出的字（review 在副本裡用 fake rclone／fake agent 逐條印出來對過）；impl2 正在改 P3／P4 的訊息，那兩處標了「**改到一半**」，以改完之後的 HEAD 為準。實跑時發現兩個步驟照原本的順序會失敗（6.10、7.17），已經改了順序，見各步旁的說明。
>
> **第 3 版**（2026-10-03）：照 HEAD `428b0b3` 的程式更新：T1 的 P2～P4（`bbc7a4a`）、T2 的 Q1～Q4（`5ae265b`）都已經修好了，相關的步驟改成修好之後的畫面和訊息，已知問題也拿掉了這幾項。進度條在正常結束時會不會到 N/N（final-checks E2），impl1 正在改，那一處標了「**改到一半**」。
>
> **第 4 版**（2026-10-03）：加上**第三段：T3 先存本機、背景上傳與刪除**（第 8～15 節），照 `openspec/changes/local-first-writes` 的 spec 寫。T3 第 3、4 節（背景刪除、互動模式的說法）在寫這一版時還在 impl1 的工作目錄裡、沒有 commit，訊息是照 spec 和工作目錄目前的字寫的，以 commit 之後的 HEAD 為準。第 0 節的安裝改成 `--force`（使用者現在裝的是穩定版），清理的最後加上「換回穩定版」。

全程只用 Drive 上的 `agora-test`，以及 `/tmp/agora-acc` 底下的目錄。對話一律用自編短句（例如「把 CSV 轉成 Markdown 表格，先列三個步驟」），不要貼真實內容。做完照最後一節清掉。

每一步都是一行指令或一個按鍵；「算過」寫的是要看到什麼。`echo $?` 用來看 exit code。印出來的 `agora:<ULID>` 都記下來（A、B、C…）。

---

## 0. 準備（只做一次）

先照第 8.1～8.4 步（在第三段的開頭），把 agora 換成這個工作目錄的版本，並確認 Drive 的資料夾（使用者平常用的是穩定版 `9baa0e5`，不換的話驗的是舊程式）。然後：

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

# 第三段：T3 先存本機、背景上傳與刪除

對照 `openspec/changes/local-first-writes/specs/local-first-writes/spec.md`。這一段可以單獨做：沒做前兩段的話，先做 8.1～8.4，再做第 0 節的 `env.sh`、`env2.sh`、`git init`（第 0 節的三段 opencode 對話這一段用不到），然後回來做第 8 節表格下面的兩個小工具（它們會改 `env.sh`，所以要在 `env.sh` 建好之後做）。

這一段要看的，多半是「指令馬上結束，**不久之後** Drive 上才有／才沒有」。換成自己的 OAuth client 之後，每次 rclone 約 0.6～0.8 秒，所以背景通常幾秒內就做完了；「等 10 秒」就夠。背景的輸出不會出現在終端機，而是寫在 `/tmp/agora-acc/state/upload.log`。

## 8. 準備：換成這個工作目錄的版本

| # | 指令 | 算過 |
|---|---|---|
| 8.1 | `cd ~/.herdr/worktrees/MyAiStorage/phase1-spike && git status --short src` | **什麼都沒印**。editable 安裝跑的是工作目錄裡**現在**的程式，包括隊員還沒 commit 的改動；有印出東西，就是還有人沒 commit，先停下來問 PM（寫這一版時 impl1 的第 3、4 節還沒 commit） |
| 8.2 | `uv tool install --force --editable ~/.herdr/worktrees/MyAiStorage/phase1-spike` | 裝好；`--force` 是因為現在裝的是穩定版 |
| 8.3 | `cat "$(uv tool dir)/agora/uv-receipt.toml"` | `requirements` 那一行是 `editable = "/Users/…/phase1-spike"`（換回穩定版之後會是 `directory = "…/agora-stable/9baa0e5"`） |
| 8.4 | `python3 -c "import json,os; print('agora-test' in json.load(open(os.path.expanduser('~/.config/agora/config.json'))).get('folders', {}))"` | 只印 `True` 或 `False`，不印 ID。Drive 已經換成使用者自己的 OAuth client（`docs/tickets/T5-own-oauth-client.md`）。scope 是 `drive.file`，新的 client 看不到舊 client 建的 `agora-test/`。**`False`**：第一個連 Drive 的指令會用新的 client 重建一個 `agora-test/`，正常。**`True`**：如果第 9 節的第一個指令出現 rclone 找不到資料夾（not found／404）的錯誤，停下來問 PM，**不要自己改 `config.json`** |

再建兩個小工具。都只用 `agora-test`，只用路徑引用 rclone 的設定檔，不印出它的內容：

```bash
# 數 rclone 被叫了幾次：fg = 前景的指令，bg = 背景上傳。只記子指令的名字（copy、lsjson…）
cat > /tmp/agora-acc/rclone-count.sh <<'EOF'
#!/bin/sh
who=fg; ps -o command= -p "$PPID" | grep -q agora.background && who=bg
sub=; skip=
for a in "$@"; do
  if [ -n "$skip" ]; then skip=; continue; fi
  case "$a" in
    --config|--drive-root-folder-id) skip=1 ;;
    -*) ;;
    *) sub=$a; break ;;
  esac
done
echo "$(date +%T) $who $sub" >> /tmp/agora-acc/rclone-calls.log
exec rclone "$@"
EOF
chmod +x /tmp/agora-acc/rclone-count.sh
grep -q AGORA_RCLONE /tmp/agora-acc/env.sh || sed -i '' '1a\
export AGORA_RCLONE=/tmp/agora-acc/rclone-count.sh
' /tmp/agora-acc/env.sh

# 看 Drive 的 agora-test/sessions：不給參數就列出 ULID；給 ULID 就列那個資料夾；再給檔名就印那個檔
cat > /tmp/agora-acc/drive.sh <<'EOF'
#!/bin/sh
FID=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser('~/.config/agora/config.json')))['folders']['agora-test'])") || exit 1
R="rclone --config $HOME/.config/agora/rclone.conf --drive-root-folder-id $FID"
if [ -n "$2" ]; then $R cat "gdrive:sessions/$1/$2"
elif [ -n "$1" ]; then $R lsf "gdrive:sessions/$1"
else $R lsf gdrive:sessions --dirs-only; fi
EOF
source /tmp/agora-acc/env.sh && echo "$AGORA_RCLONE"
```

最後一行要印出 `/tmp/agora-acc/rclone-count.sh`。

再用 opencode 在 `/tmp/agora-acc/proj` 說幾段自編的短對話，**不要匯入**，記下 `ses_…`：t1、t2、t3（第 9 節）、t4（第 10 節）、t5（第 14 節）、t6（第 15 節）。

⚠️ 第 14、15 節要用**第二個終端機**。那個終端機也要先 `source /tmp/agora-acc/env.sh`。

## 9. import 多個：指令馬上結束，不久 Drive 上就有了

| # | 指令 | 算過 |
|---|---|---|
| 9.1 | `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync /tmp/agora-acc/rclone-calls.log` | 讓開頭的同步不被節流（量的是「最慢」的那一種） |
| 9.2 | `time agora import session --external-session-id t1,t2,t3 --agent opencode; echo $?` | stdout 有三行 `agora:<ULID>`（記為 K、L、M）；stderr **沒有**「上傳失敗」；exit `0`。**記下 `real` 的秒數**（給 PM：前景時間）。指令不等上傳，所以應該只比「一次同步加上讀三段對話」多一點點 |
| 9.3 | 馬上執行 `agora search session --no-sync \| grep -c '(未上傳)'` | 0～3 都算對：背景很快，看到 `(未上傳)` 的話，就是還沒傳完 |
| 9.4 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox; pgrep -fl agora.background` | 兩個都**什麼都沒印**：outbox 空了，背景也結束了 |
| 9.5 | `sh /tmp/agora-acc/drive.sh` | 列出 K、L、M 的 ULID（後面有 `/`） |
| 9.6 | `sh /tmp/agora-acc/drive.sh <K的ULID>` | `session.md` 和一個 `raw-….json` |
| 9.7 | `ls /tmp/agora-acc/cache/sessions/<K的ULID>/` | **本機也有** `session.md` 和**同一個** `raw-….json`（spec「本機保留完整的一份」，以前只有 `session.md`） |
| 9.8 | `cat /tmp/agora-acc/rclone-calls.log` | `bg` 的行**不超過 4 行**（spec「一批只連固定幾次 Drive」：原始檔一次 `copy`、`session.md` 一次 `copy`、驗 md5 一次 `lsjson`）。`fg` 的行只有開頭同步用的（記下幾行給 PM）；這一次是第一次用 `agora-test` 的話，還會多 `mkdir`、`lsjson` 各一行（建資料夾） |
| 9.9 | `grep -i error /tmp/agora-acc/state/upload.log` | 什麼都沒印 |

## 10. import 完馬上 edit，不會遺失

| # | 指令 | 算過 |
|---|---|---|
| 10.1 | `N=$(agora import session --external-session-id t4 --agent opencode) && agora edit session $N --header 'title=馬上改'; echo $N $?` | 印出 N 的 id，exit `0`。edit 緊接在 import 後面，背景多半正在傳第一版（spec「上傳中又改了同一個」） |
| 10.2 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox` | 什麼都沒印 |
| 10.3 | `sh /tmp/agora-acc/drive.sh <N的ULID> session.md \| grep title` | `title: 馬上改`（Drive 上是**新的**版本，不是匯入時的標題） |
| 10.4 | `agora show session $N \| grep title` | 本機也是 `title: 馬上改` |

如果 10.1 的 edit 失敗、出現 `FileNotFoundError` 之類的錯誤，請記下來：這是 review T3-sec5 V5（edit 剛好碰上背景改名）。

## 11. delete：馬上從清單消失，不久 Drive 上就沒有了

| # | 指令 | 算過 |
|---|---|---|
| 11.1 | `agora delete session <L> <M> --yes; echo $?` | stdout 印出 L、M；stderr 有 `[agora] 已從本機刪除 2 個，背景移到 Drive 垃圾桶`；exit `0`；指令馬上結束 |
| 11.2 | 馬上執行 `agora search session --no-sync \| grep -c -e <L的ULID> -e <M的ULID>` | `0`：指令結束時就已經從清單消失了 |
| 11.3 | 等 10 秒，`ls -A /tmp/agora-acc/state/trash-queue` | 什麼都沒印（背景移完了） |
| 11.4 | `sh /tmp/agora-acc/drive.sh` | 沒有 L、M（K、N 還在）；Drive 網頁的垃圾桶裡看得到這兩個資料夾 |

## 12. 在這台寫的，被機器 2 刪掉之後救回來

這一節**不先 pull**。以前要用第 6.0 步先 pull 才救得回來（已知問題 T1 P1）；T3 之後不用了。

| # | 指令 | 算過 |
|---|---|---|
| 12.1 | `agora continue session <K> --agent opencode --dir /tmp/agora-acc/proj`，說一句自編的話，離開 | 印出的是同一個 K |
| 12.2 | 等 10 秒，`ls /tmp/agora-acc/cache/sessions/<K的ULID>/ \| grep -c '^raw-'` | `1`：只留新的原始檔（spec「接續之後只留新的原始檔」） |
| 12.3 | `source /tmp/agora-acc/env2.sh && rm -f /tmp/agora-acc/state2/last-sync && agora search session \| grep <K的ULID>` | 機器 2 看得到 K |
| 12.4 | `agora delete session <K> --yes`，等 10 秒，`ls -A /tmp/agora-acc/state2/trash-queue` | 機器 2 刪掉了 K；佇列是空的 |
| 12.5 | `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync && agora search session \| grep <K的ULID>` | 回到機器 1：K 那一行的最後是 `(雲端沒有)` |
| 12.6 | `agora push session <K> --not-exist-upload; echo $?` | stdout：`[agora] 寫回 1 個`；exit `0`；**沒有**「標頭指到的 raw-….json 本機沒有，不傳半套」 |
| 12.7 | `sh /tmp/agora-acc/drive.sh <K的ULID>` | `session.md` 和**一個** `raw-….json`（12.1 之後的那一個） |
| 12.8 | `rm -f /tmp/agora-acc/state/last-sync && agora search session \| grep <K的ULID>` | 沒有 `(雲端沒有)` 了 |

## 13. 改到一半被機器 2 刪掉 → 另存成新的 Session

要讓「這台的修改還沒傳上去，機器 2 就刪掉了」，先**關掉 Wi-Fi**，讓修改留在 outbox。

| # | 指令 | 算過 |
|---|---|---|
| 13.1 | **關掉 Wi-Fi**。`source /tmp/agora-acc/env.sh && agora edit session <N> --header 'title=改到一半'; echo $?` | exit `0`（存進本機就算成功；背景連不上 Drive，失敗了，只寫進記錄檔）。離線時 rclone 會重試，這一步可能要等十幾秒 |
| 13.2 | `ls -A /tmp/agora-acc/state/outbox/<N的ULID>/`，然後 `pgrep -fl agora.background` | outbox 裡有 `.update`（「更新既有的 id」的記號）、`session.md`、`raw-….json`。⚠️ **要等到 `pgrep` 什麼都沒印（背景已經放棄了）才打開 Wi-Fi**；不然還在重試的背景會在 Wi-Fi 一回來時就把修改傳上去，13.4 就變成普通的刪除 |
| 13.3 | **打開 Wi-Fi**。`source /tmp/agora-acc/env2.sh && rm -f /tmp/agora-acc/state2/last-sync && agora search session \| grep <N的ULID>` | 機器 2 看得到 N，標題是第 10 節的 `馬上改` |
| 13.4 | `agora delete session <N> --yes`，等 10 秒，`ls -A /tmp/agora-acc/state2/trash-queue` | 機器 2 刪掉了 N；佇列是空的 |
| 13.5 | `source /tmp/agora-acc/env.sh && rm -f /tmp/agora-acc/state/last-sync && agora search session > /dev/null` | 回到機器 1。這個指令開頭的同步，看到 outbox 不是空的，會啟動背景；stderr 有 `outbox 有 1 筆未上傳`，或者 `背景上傳中，1 筆` |
| 13.6 | 等 10 秒，`grep 存成了 /tmp/agora-acc/state/upload.log` | `<N的ULID> 已被別台刪除，這次的修改存成了 <新的ULID>`（記為 Y）。spec 說這一句要提醒使用者；目前**只寫在記錄檔**，見已知問題 V4 |
| 13.7 | `ls -A /tmp/agora-acc/state/outbox; sh /tmp/agora-acc/drive.sh` | outbox 空了；Drive 上有 **Y**，**沒有** N（N 沒有被傳回去） |
| 13.8 | `sh /tmp/agora-acc/drive.sh <Y的ULID> session.md \| grep -A3 -e title -e parents` | `title: 改到一半`；`parents` 底下有 `agora:<N的ULID>` |
| 13.9 | `rm -f /tmp/agora-acc/state/last-sync && agora search session \| grep -e <N的ULID> -e <Y的ULID>` | 有 Y（`改到一半`）；N 也還在，最後是 `(雲端沒有)`（N 留在本機，照同步的規則標記） |

## 14. push 會等自己的 id 傳完

要讓「push 的時候背景正在傳」，用第二個終端機**拿住上傳的鎖 30 秒**，假裝背景正在跑。這不是 agora 的程式，只是佔住那把鎖。

| # | 指令 | 算過 |
|---|---|---|
| 14.1 | **終端機 B**：`python3 -c 'import fcntl,time; f=open("/tmp/agora-acc/state/upload.lock","a"); fcntl.flock(f, fcntl.LOCK_EX); print("鎖住 30 秒"); time.sleep(30)'` | 印出 `鎖住 30 秒` |
| 14.2 | **終端機 A**（30 秒內）：`P=$(agora import session --external-session-id t5 --agent opencode) && echo $P && time agora push session $P` | import 馬上結束，印出 P；push **一直等到**終端機 B 結束，才印出 stdout `[agora] 寫回 1 個`；`real` 接近終端機 B 剩下的秒數。spec 說等的時候每 10 秒會在 stderr 說一次 `背景上傳中，還在等…`；目前**不會說**，見已知問題 R7 |
| 14.3 | push 一結束就執行 `sh /tmp/agora-acc/drive.sh <P的ULID>` | 已經有 `session.md` 和 `raw-….json`（push 結束時，P 就已經在 Drive 上了） |
| 14.4 | 再做一次 14.1；終端機 A：`agora push session $P`，等 3 秒後按 **Ctrl-C**，`echo $?` | 中斷，exit `130`（等待可以用 Ctrl-C 中斷） |

## 15. 互動模式：「未上傳」與結果視窗的說法

| # | 操作 | 算過 |
|---|---|---|
| 15.1 | **終端機 B**：14.1 那一行，把 `30` 改成 `60` | 鎖住 60 秒，讓剛匯入的來不及傳上去 |
| 15.2 | **終端機 A**：`agora`；`Tab` 到未匯入頁，游標放在 t6，`Enter` | 等待視窗**很快就結束**，不等上傳（spec「互動模式等的不是背景上傳」）；結果視窗寫 **「已經存在本機，背景上傳中」** |
| 15.3 | 關掉結果視窗，`Tab` 回 Agora 頁 | 剛匯入的那一列（記為 Q），雲端欄是 **「未上傳」** |
| 15.4 | 游標放在 K 上，`d`，`↓` 到「確定」，`Enter` | 結果視窗寫 **「已從本機刪除，背景移到 Drive 垃圾桶」**；關掉之後，K 那一列不見了 |
| 15.5 | `q` 離開。等終端機 B 結束，然後 `rm -f /tmp/agora-acc/state/last-sync && agora search session > /dev/null` | 這個指令會啟動背景，把 Q 傳上去、把 K 移到垃圾桶（spec「背景失敗之後補傳」也是同一條路：下一個連 Drive 的指令啟動背景） |
| 15.6 | 等 10 秒，`ls -A /tmp/agora-acc/state/outbox /tmp/agora-acc/state/trash-queue` | 兩個都是空的 |
| 15.7 | `sh /tmp/agora-acc/drive.sh` | 有 Q，沒有 K |
| 15.8 | `agora`，看 Q 那一列 | 雲端欄是 **✓**；`q` 離開 |

結果視窗只有在背景程序**啟動失敗**（exit 3）時，才會說「已存進 outbox，之後的指令會自動再送」。這種情況在驗收時做不出來，單元測試有測。

## 給 PM 填的數字

| 項目 | 數字 |
|---|---|
| 9.2：import 3 個的前景時間（`real`） | ___ 秒 |
| 9.8：前景的 rclone 次數（`fg`）／背景的（`bg`） | ___ ／ ___ |
| 14.2：push 等了多久（`real`） | ___ 秒 |

**第三段的已知問題**（出自 review `T3-sec3.md`、`T3-sec4.md`、`T3-sec5.md`，修好之後就拿掉那一行）：
- **V2**：另存出來的 Y，**本機的清單要等下一次同步才看得到**（Y 沒有被放進本機的索引和鏡像）。所以 13.9 要先 `rm -f …/last-sync`。修好之後，13.7 一結束，`agora search session --no-sync` 就應該有 Y 了。
- **V3**：Y 的標頭裡，relation 是 `import`（spec 說應該是原本的那一種，這裡是 `edit`）；`parents` 只有 N，N 原本的 parents 不見了。
- **V4**：「N 已被別台刪除，這次的修改存成了 Y」只寫進 `upload.log`，使用者在終端機看不到（13.6）。
- **R7**：push 在等的時候，不會每 10 秒說一次「背景上傳中，還在等…」（14.2）。
- **V1**：背景正在把 N 另存成 Y 的那一瞬間又 edit N 的話，第二次的修改會留在本機、不會傳上去。驗收步驟碰不到這個情況，只是先記著。
- **改到一半**：spec 說「有 N 個等著移到 Drive 垃圾桶」的提醒，以及 pull 拒絕刪除佇列裡的 Session 時算成失敗（T3-sec4 S3），寫這一版時還沒做完。

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

最後（兩台「機器」的快取和狀態，以及第三段的兩個小工具，都在這底下）：

```bash
cd ~ && rm -rf /tmp/agora-acc
```

舊的 `agora-test/`：換 client 之前建的那一個，新的 client 看不到（`drive.file`），所以上面的清理碰不到它。要不要從 Drive 網頁把它移到垃圾桶，由使用者決定。

### 換回穩定版（做完第三段之後）

| # | 指令 | 算過 |
|---|---|---|
| R.1 | `pgrep -fl agora.background` | 什麼都沒印（沒有背景還在跑） |
| R.2 | `ls -A ~/.local/state/agora/trash-queue 2>/dev/null \| wc -l` | `0`。只列名字，不打開任何檔案。不是 0 的話，就是驗收期間有指令**沒有** source `env.sh`，用新版本在正式的目錄刪了東西；穩定版不會處理這個佇列，先停下來告訴 PM，**不要自己清** |
| R.3 | `uv tool install --force ~/.local/share/agora-stable/9baa0e5` | 裝回穩定版（不是 editable） |
| R.4 | `cat "$(uv tool dir)/agora/uv-receipt.toml"` | `requirements` 那一行是 `directory = "/Users/…/.local/share/agora-stable/9baa0e5"` |

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
- **第 4 版改了什麼**：加上第三段（第 8～15 節），也就是 T3 的「先存本機、背景上傳與刪除」；第 0 節的安裝改到第 8 節（`--force --editable`，並且先確認工作目錄沒有還沒 commit 的程式）；清理的最後加上「換回穩定版」，以及換了 OAuth client 之後舊的 `agora-test/` 碰不到的說明。第 12 節的救回**不再先 pull**（T1 P1 由 T3 解決）；第一段的 6.0、7.15 還是照舊先 pull，等第三段驗收過了，再決定拿不拿掉。

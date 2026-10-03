# Agora lite 人工驗收清單

給使用者自己跑的互動驗收，只涵蓋自動測試做不到的部分（TUI、`Ctrl-C`、`/clear`）。
全程只用 Drive 上的 `agora-test`，以及 `/tmp/agora-acc` 底下自己的快取與狀態目錄。
對話一律用自編短句（例如「把 CSV 轉成 Markdown 表格，先列三個步驟」），不要貼真實內容。
做完照最後一節清掉。

## 0. 準備（只做一次）

```bash
# 安裝 agora 指令
cd ~/.herdr/worktrees/MyAiStorage/phase1-spike && uv tool install --editable .

# 驗收專用的環境寫成一個檔，每一節開頭都 source 它
mkdir -p /tmp/agora-acc/proj
cat > /tmp/agora-acc/env.sh <<'EOF'
export AGORA_FOLDER_NAME=agora-test
export AGORA_CACHE_DIR=/tmp/agora-acc/cache
export AGORA_STATE_DIR=/tmp/agora-acc/state
cd /tmp/agora-acc/proj
echo "[acc] agora-test、快取 /tmp/agora-acc、專案 /tmp/agora-acc/proj ✓"
EOF
source /tmp/agora-acc/env.sh
git init -q && git commit -q --allow-empty -m init
```

⚠️ **每一節的第一行都是 `source /tmp/agora-acc/env.sh`，看到 `[acc] … ✓` 才往下做。** 換了終端機或分頁而沒有 source 的話，`agora` 會寫進正式的 `agora/` 與平常的快取。

⚠️ **每一個 `continue` 都明寫 `--dir /tmp/agora-acc/proj`。** merge 出來的 Session 沒有
`source.dir`，不加 `--dir` 的話 agora 會開在「你當下所在的目錄」，會在別的專案（例如 repo）
留下 session。`import` 之前也先 `cd /tmp/agora-acc/proj`，opencode 的 session 才是這個專案的。

以下每一步印出的 `agora:<ULID>` 都記下來（A、B、C…）。

## 1. opencode TUI 對話＋import

```bash
source /tmp/agora-acc/env.sh
opencode                  # TUI，用免費模型說一句自編的話，然後正常離開
opencode session list     # 只會列出這個專案的 session，找到剛才那個 ses_…
agora import session --external-session-id <ses_id> --agent opencode --header 'title=驗收表格'
```

算過：印出 `agora:<ULID>`（記為 A）。

## 2. opencode 接續

```bash
source /tmp/agora-acc/env.sh
agora continue session <A> --agent opencode --dir /tmp/agora-acc/proj
```

在 TUI 裡再說一句自編的話（例如「把第二步寫詳細一點」），正常離開。
算過：印出新的 `agora:<ULID>`（記為 B）；`agora show session <B>` 看得到剛才那句話。

## 3. 同一個 Session 換 claude 接（轉成 Claude 的格式載入）

```bash
source /tmp/agora-acc/env.sh
agora continue session <A> --agent claude --dir /tmp/agora-acc/proj
```

算過：Claude 一打開，畫面上就有步驟 1 的對話（開頭多一則說明：這是轉過來的紀錄，
`[tool]` 行只是摘要），和 opencode 接續時看到的一樣；你再說一句自編的話，用 `/exit` 離開；
印出新的 `agora:<ULID>`（記為 C）。

再做一次，但**打開後什麼都不說就 `/exit`**：應該印「這次沒有新內容」，不存新 Session。

## 4. claude 接續中按 Ctrl-C

```bash
source /tmp/agora-acc/env.sh
agora continue session <A> --agent claude --dir /tmp/agora-acc/proj
```

1. 確認畫面上有之前的對話。
2. 送一句自編的話，**在它回覆到一半時按一次 `Ctrl-C`**——這只會中斷這一輪回覆，Claude 不會結束。
3. 用 `/exit` 離開。

算過：agora 沒有跟著被中斷，最後印出新的 `agora:<ULID>`（記為 D）；
`agora show session <D>` 看得到第 2 點那段。
（如果你在 Claude 說出任何話之前就按了 Ctrl-C，agora 會印「這次沒有新內容」而不存——這也是正確的。）

## 5. claude 接續中用一次 /clear

```bash
source /tmp/agora-acc/env.sh
agora continue session <A> --agent claude --dir /tmp/agora-acc/proj
```

先說一句話，執行 `/clear`，再說一句話，然後 `/exit` 離開。
算過：`/clear` 之前的部分照常存成新 Session（記為 E）；agora 另外印出一行，
告訴你 `/clear` 之後的對話在某個 Claude session，要用
`agora import session --external-session-id <uuid> --agent claude` 另外存。
照那行提示跑完，印出 `agora:<ULID>`（記為 F）。

## 6. 合併再接續

```bash
source /tmp/agora-acc/env.sh
agora merge session <B>, <C> --agent opencode     # 會在背景叫 opencode 寫一次要約，要等一下
agora show session <merge 印出的 id>
agora continue session <merge 印出的 id> --agent opencode --dir /tmp/agora-acc/proj
```

算過：merge 對 B、C 各叫一次 opencode，印出新 id（記為 G）；`show` 的「## 要約」底下 B、C 各一節，
格式固定（標題、agora id 與取原版的指令、目的、決定、進度、未解決），沒有跨來源的內容；最後是「## 來源」清單。
標頭有 `status: draft`、`generated.by: opencode/<模型>`；`show --raw` 是 `sections.json`。
`opencode session list` 裡**沒有**多出寫要約用的 session。
opencode 一打開，畫面上只有一則「自動寫成的要約與來源清單，是參考資料…」和各節要約，沒有 B、C 的全文。
問它「B 裡最後一句說了什麼？」，它應該會用 `agora show session <B 的 id>` 去取原版再回答。
說一句話後離開，continue 印出新的 id（記為 H）。同一個 G 換 `--agent claude` 接，畫面上看到的內容應該一樣。

## 7. 改標頭

```bash
source /tmp/agora-acc/env.sh
agora edit session <G> --header 'title=合併後改名' --header 'tags=[驗收]'
agora show session <G>
```

算過：`edit` 印回**同一個** id（G），`show` 的 `title` 變成「合併後改名」；
閱讀版沒變（只有標頭被改）。

## 8. 刪掉一個 Session

```bash
source /tmp/agora-acc/env.sh
agora delete session <D>            # 沒加 --yes：應該被拒絕，exit code 1，訊息叫你加 --yes
agora delete session <D> --yes      # 印出 id，並說已移到 Drive 垃圾桶
agora show session <D>              # 應該說「找不到」，exit code 1
agora delete session <A> --yes      # A 有子 Session：應該被拒絕，exit code 1，並列出它的子 Session
```

算過：沒有 `--yes` 會被拒；有 `--yes` 之後 Drive 上那個資料夾不見了（30 天內可從 Drive 網頁還原），
`show` 找不到它。有子 Session 的（例如 A）不能刪，會提示是哪幾個。

## 9. 搜尋中文二字詞

```bash
source /tmp/agora-acc/env.sh
agora search session --filter 'text~=表格'          # 全文包含
agora search session --filter agent=claude          # 標頭欄位全等
agora search session --filter 'title~=驗收'         # 標頭欄位包含
agora search session                                 # 省略 filter＝列出全部
```

算過：每一行第一欄都是 `agora:<ULID>`，而且第一個搜尋包含 A、B、C。
（`search` 會順便同步 Drive，所以沒有 `sync` 指令。）

## 清理

⚠️ **先確認沒有整合測試或 e2e 正在跑**（它們也用 `agora-test`）。下面第一段會把 `agora-test/sessions/` 整個移到 Drive 垃圾桶。

全部用**寫死的路徑**，沒有任何會變空的變數或佔位字。

```bash
# Drive：只清 agora-test 底下的 sessions/（這個資料夾只放測試資料）
FID=$(rclone --config ~/.config/agora/rclone.conf lsjson gdrive: --dirs-only \
  | python3 -c "import json,sys; print(next(e['ID'] for e in json.load(sys.stdin) if e['Name']=='agora-test'))")
# 一個資料夾一個資料夾地移到垃圾桶：整個 sessions/ 一次 purge 會被 drive.file 權限擋下（403 appNotAuthorizedToChild）
test -n "$FID" && for d in $(rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" lsf gdrive:sessions --dirs-only); do
  rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" purge "gdrive:sessions/${d%/}"
done

# Claude：驗收專案的資料夾只有這次驗收建的檔，整個刪
rm -rf "$HOME/.claude/projects/-private-tmp-agora-acc-proj"
```

opencode 的測試 session，在專案目錄裡**一個一個**依 id 刪（不要批次刪）：

```bash
cd /tmp/agora-acc/proj
opencode session list            # 只會有這次驗收建的 ses_…
opencode session delete <ses_id> # 一個一個刪
```

最後：

```bash
cd ~ && rm -rf /tmp/agora-acc
```

`~/.claude.json` 可能留下 `/private/tmp/agora-acc/proj` 的專案設定，那是本機設定檔，不影響 Drive，不用處理。

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
echo "[acc] agora-test、快取 /tmp/agora-acc ✓"
EOF
source /tmp/agora-acc/env.sh
git init -q && git commit -q --allow-empty -m init
```

⚠️ **每一節的第一行都是 `source /tmp/agora-acc/env.sh`，看到 `[acc] … ✓` 才往下做。** 換了終端機或分頁而沒有 source 的話，`agora` 會寫進正式的 `agora/` 與平常的快取。

以下每一步印出的 `agora:<ULID>` 都記下來（A、B、C…）。

## 1. opencode TUI 對話＋import

```bash
source /tmp/agora-acc/env.sh
opencode                  # TUI，用免費模型說一句自編的話，然後正常離開
opencode session list     # 只會列出這個專案的 session，找到剛才那個 ses_…
agora import --format opencode --session-id <ses_id> --header 'title=驗收表格'
```

算過：印出 `agora:<ULID>`（記為 A）。

## 2. opencode 接續

```bash
source /tmp/agora-acc/env.sh
agora continue-session <A> --agent opencode
```

在 TUI 裡再說一句自編的話（例如「把第二步寫詳細一點」），正常離開。
算過：印出新的 `agora:<ULID>`（記為 B）；`agora show <B>` 看得到剛才那句話。

## 3. 同一個 Session 換 claude 接（閱讀版注入）

```bash
source /tmp/agora-acc/env.sh
agora continue-session <A> --agent claude
```

Claude 的第一則訊息會先讀 A 的閱讀版。算過：它先用兩三句話說明理解的進度
（內容對得上步驟 1 的對話）；你再說一句自編的話，用 `/exit` 離開；
印出新的 `agora:<ULID>`（記為 C）。

## 4. claude 接續中按 Ctrl-C

```bash
source /tmp/agora-acc/env.sh
agora continue-session <A> --agent claude
```

1. 等 Claude 說完第一段（讀完閱讀版的那段）。
2. 再送一句自編的話，**在它回覆到一半時按一次 `Ctrl-C`**——這只會中斷這一輪回覆，Claude 不會結束。
3. 用 `/exit` 離開。

算過：agora 沒有跟著被中斷，最後印出新的 `agora:<ULID>`（記為 D）；`agora show <D>` 看得到第 1 點那段。
（如果你在 Claude 說出任何話之前就按了 Ctrl-C，agora 會印「這次沒有新內容」而不存——這也是正確的。）

## 5. claude 接續中用一次 /clear

```bash
source /tmp/agora-acc/env.sh
agora continue-session <A> --agent claude
```

先說一句話，執行 `/clear`，再說一句話，然後 `/exit` 離開。
算過：`/clear` 之前的部分照常存成新 Session（記為 E）；agora 另外印出一行，
告訴你 `/clear` 之後的對話在某個 Claude session，要用
`agora import --format claude --session-id <uuid>` 另外存。照那行提示跑完，印出 `agora:<ULID>`（記為 F）。

## 6. 合併再接續

```bash
source /tmp/agora-acc/env.sh
agora merge-session <B>, <C>
agora continue-session <merge 印出的 id> --agent opencode
```

在 TUI 裡說一句話後離開。算過：merge 印出新 id（記為 G），continue 又印出新的 id（記為 H）。

## 7. 搜尋中文二字詞

```bash
source /tmp/agora-acc/env.sh
agora search session '表格'
```

算過：每一行第一欄都是 `agora:<ULID>`，而且包含 A、B、C、D。

## 清理

全部用**寫死的路徑**，沒有任何會變空的變數或佔位字。

```bash
# Drive：只清 agora-test 底下的 sessions/（這個資料夾只放測試資料）
FID=$(rclone --config ~/.config/agora/rclone.conf lsjson gdrive: --dirs-only \
  | python3 -c "import json,sys; print(next(e['ID'] for e in json.load(sys.stdin) if e['Name']=='agora-test'))")
test -n "$FID" && rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" purge gdrive:sessions

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

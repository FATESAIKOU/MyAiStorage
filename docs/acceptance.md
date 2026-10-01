# Agora lite 人工驗收清單

給使用者早上自己跑的互動驗收，只涵蓋自動測試做不到的部分（TUI、`Ctrl-C`、`/clear`）。
每一行都只用 Drive 上的 `agora-test`（全程先 `export AGORA_FOLDER_NAME=agora-test`）。
對話一律用自編短句（例如「把 CSV 轉成 Markdown 表格，先列三個步驟」），不要貼真實內容。
做完請照最後一節把這次的 Session 清掉。

```bash
export AGORA_FOLDER_NAME=agora-test
mkdir -p /tmp/agora-acc/proj && cd /tmp/agora-acc/proj
git init -q && git commit -q --allow-empty -m init
```

以下每一步印出的 `agora:<ULID>` 都記下來，merge 與清理時要用。

## 1. opencode TUI 對話＋import

```bash
cd /tmp/agora-acc/proj
opencode            # TUI，用免費模型說一句自編的話（例如上面那句），然後正常離開
opencode session list     # 找到剛才那個 session 的 id（ses_…）
agora import --format opencode --session-id <ses_id> --header 'title=驗收表格'
```

算過：印出 `agora:<ULID>`（記為 A）。

## 2. opencode 接續

```bash
agora continue-session <A> --agent opencode --dir /tmp/agora-acc/proj
```

在 TUI 裡再說一句自編的話（例如「把第二步寫詳細一點」），正常離開。
算過：印出新的 `agora:<ULID>`（記為 B）；`agora show <B>` 看得到剛才那句話。

## 3. 同一個 Session 換 claude 接（閱讀版注入）

```bash
agora continue-session <A> --agent claude --dir /tmp/agora-acc/proj
```

Claude 的第一則訊息會先讀 A 的閱讀版。算過：它先用兩三句話說明理解的進度
（內容對得上步驟 1 的對話）；你再說一句自編的話，用 `/exit` 離開；
印出新的 `agora:<ULID>`（記為 C）。

## 4. claude 接續中按一次 Ctrl-C

```bash
agora continue-session <A> --agent claude --dir /tmp/agora-acc/proj
```

等它開始回覆後按一次 `Ctrl-C`。
算過：agent 停下來，agora 沒死，仍然印出新的 `agora:<ULID>`（記為 D），
`agora show <D>` 看得到 Ctrl-C 之前已說的內容。

## 5. claude 接續中用一次 /clear

```bash
agora continue-session <A> --agent claude --dir /tmp/agora-acc/proj
```

先說一句話，執行 `/clear`，再說一句話，然後 `/exit` 離開。
算過：`/clear` 之前的部分照常存成新 Session；agora 另外印出一行，
告訴你 `/clear` 之後的對話在某個 Claude session，要用
`agora import --format claude --session-id <uuid>` 另外存。
照那行提示把 import 跑完，算過：印出新的 `agora:<ULID>`（記為 E）。

## 6. 合併再接續

```bash
agora merge-session <B>, <C>
agora continue-session <上一步印出的id> --agent opencode --dir /tmp/agora-acc/proj
```

在 TUI 裡說一句話後離開。算過：merge 印出新 id，continue 又印出更新的 id（記為 F）。

## 7. 搜尋中文二字詞

```bash
agora search session '表格'
```

算過：每一行第一欄都是 `agora:<ULID>`，包含上面記下的 id。

## 清理

把上面記下的每個 `agora:<ULID>` 都 purge 掉（一次一個；`<ULID>` 是冒號後面的部分）。
先查 `agora-test` 資料夾的 ID（只列名字，不讀內容）：

```bash
FID=$(rclone --config ~/.config/agora/rclone.conf lsjson gdrive: --dirs-only \
  | python3 -c "import json,sys; print([e['ID'] for e in json.load(sys.stdin) if e['Name']=='agora-test'][0])")
for U in <ULID1> <ULID2> <ULID3>; do
  rclone --config ~/.config/agora/rclone.conf --drive-root-folder-id "$FID" purge "gdrive:sessions/$U"
done
```

opencode 的測試 session，在專案目錄裡一個一個刪（不要一次全刪）：

```bash
cd /tmp/agora-acc/proj
opencode session list        # 確認只有這次驗收建的 ses_…
opencode session delete <ses_id>     # 一個一個刪
```

Claude 的測試 jsonl，按 uuid 一個一個刪（uuid 從 `agora show <id>` 的
`source.session_id` 查，或步驟 5 提示的那個）。先算出專案目錄的編碼名字：

```bash
ENC=$(python3 -c "import re; print(re.sub(r'[^A-Za-z0-9]', '-', '/private/tmp/agora-acc/proj'))")
rm ~/.claude/projects/$ENC/<uuid>.jsonl
rm -rf ~/.claude/projects/$ENC/<uuid>   # 只有用過 subagent 才會有這個目錄
```

`/tmp/agora-acc/proj` 整個刪掉即可。`~/.claude.json` 可能留下 `/private/tmp/agora-acc`
的專案設定，那是本機設定檔，不影響 Drive，不用處理。

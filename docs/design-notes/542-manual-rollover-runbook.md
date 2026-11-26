# 手動換頁操作說明（給 PMO）

一個頻道的現頁快到留言上限時，PMO 用這份說明手動開一張新頁，在註冊簿貼一則 `rollover` 留言，讓頻道換到新頁。讀者不用做任何事。

- 對象：PR-2（2a／2b／2c）已經在 main、自動換頁（PR-3）還沒上線的這段期間。PR-3 上線之後改用 `angareion channel rollover`，這份說明只留作備援。
- 依據：設計 `docs/design-notes/542-channel-rollover.md`（§1.1、§1.2、§2、§3、§6.2；決定 D-34）與現行程式（`registry.rollover_comment`、`registry_rules.rebuild_channels`、`channel_cli` 的 `status`、`cli.CAPABILITIES`）。
- 這份文件在公開 repo，所以不寫任何 repo 名、issue 編號與機器位址，一律用占位符：

| 占位符 | 意思 |
|---|---|
| `<頻道名>` | 要換頁的頻道名（例如 `team-x-w2`） |
| `<舊頁>` | 頻道現在的頁（channels.json 裡這個頻道的 `issue`） |
| `<新頁>` | 這次新開的 issue 編號 |
| `<backend 名>` | channels.json 裡這個頻道的 `backend` |
| `<頻道 repo>` | `<backend 名>` 在 backends.json 指到的 repo |
| `<註冊簿 repo>`、`<註冊簿編號>` | 註冊簿 issue 所在的 repo 與編號（angareion 設定檔的 `registry_repo`／`registry_issue`） |
| `<config_dir>` | angareion 設定檔的 `config_dir`（本機的 MyAiStorage clone） |
| `<換頁時刻>` | 那則 `rollover` 留言的 `created_at`（後端時刻，UTC） |

- 開 issue 與貼留言都是對 GitHub 的寫入。照 PMO 的規矩先取得本人同意，再用本人的 `gh` 做。不要用 bot 或 GitHub App 的 token：註冊簿 workflow 只在非 Bot 的留言上觸發，而且只採計 `OWNER`／`MEMBER`／`COLLABORATOR` 的留言。
- 一個頻道一次只由一個人操作（見第 8 節「旁頁」）。

## 1. 前置盤點（沒過就不能貼 `rollover`）

盤點以「安裝」為單位：每台機器、每個 worker 映像，各有一份 angareion。名單包括 Mac（PMO 與 Mac 上的 PM）、每一台 worker（PM 與隊員共用映像裡的那一份），以及其他會呼叫 `angareion` 的程式（`aicomm`、`pmo`）所在的機器。worker 上由該機的 PM 跑指令回報，PMO 收齊。

每一份安裝都要符合下面三條：

1. **能力標記**：`angareion --capabilities` 的 exit 是 0，而且輸出含 `paged-read`。

   ```bash
   type -a angareion                         # PATH 上的每一個都要驗，不只第一個
   angareion --capabilities; echo "exit=$?"
   # 期待：
   # paged-read
   # exit=0
   ```

   舊版不認得這個旗標，會印用法錯誤、exit 不是 0。只認「exit 0 而且含 `paged-read`」，不看非 0 的值是多少。

2. **設定拉取間隔**：angareion 設定檔的 `config_pull_interval` 在 (0, 21600] 秒之間（大於 0、不超過 6 小時）。設定檔沒有這個鍵時是預設值 900。

   ```bash
   python3 -c 'import json, os, pathlib
   e = os.environ.get("ANGAREION_CONFIG")
   p = pathlib.Path(os.path.expanduser(e)) if e else pathlib.Path.home() / ".config/angareion/config.json"
   p = p / "config.json" if p.is_dir() else p
   print(json.load(open(p)).get("config_pull_interval", 900))'
   ```

   0 表示不定期拉取，這個席位會一直寫舊頁、讀不到新頁，不合格。

3. **同一個身分不得有未升級的安裝**：舊版存游標時會把整個游標檔的每頁游標（`pages`）洗掉，已換頁頻道的「沒讀過的頁從頭讀」保護就失效（只會漏，不會重送）。所以每個身分（`ANGAREION_IDENTITY`）用到的狀態目錄（預設 `~/.local/state/angareion`，或 `ANGAREION_STATE_DIR`），只能被已升級的安裝寫入。同一台機器上有兩份 angareion（例如映像裡一份、某個虛擬環境裡又一份）而它們共用狀態目錄時，兩份都要過第 1 條。

- 盤點不到的一份當作沒升級。
- 結果逐份記下（機器或映像、`angareion` 的路徑、`--capabilities` 的輸出與 exit、`config_pull_interval`），貼在這次換頁的工單上。
- **有任何一份不合格，就停在這裡**，不要開新頁，也不要貼 `rollover`。在那之前照舊用 `-b` 頂著（第 10 節）。
- 第一次手動換頁之前要做一次完整盤點。之後每次換頁之前，至少確認名單沒有新增機器或映像，有新增的就要補驗。

## 2. 換頁前確認現況

1. 取得最新的 channels.json，記下 `<頻道名>` 的 `backend` 與 `issue`（＝`<舊頁>`）：

   ```bash
   git -C <config_dir> fetch --quiet origin main
   git -C <config_dir> show origin/main:config/angareion/channels.json \
     | python3 -c 'import json, sys; print(json.load(sys.stdin)["channels"]["<頻道名>"])'
   # 例：{'backend': '<backend 名>', 'issue': <舊頁>}            （沒換過頁）
   #     {'backend': ..., 'issue': <舊頁>, 'retired': [...]}      （換過頁；<舊頁> 仍是 `issue` 那一個）
   ```

   `from` 一定要填這裡看到的 `issue`。填成 `retired` 裡的頁，重建會把新頁記成旁頁（第 8 節），頻道不會換頁。

2. 看現頁的則數：

   ```bash
   angareion channel status <頻道名>
   # name        count  limit  level  pages  flags
   # <頻道名>     2130   2500   warn   1      -
   # registry    60     2500   ok     1      -
   ```

   在 `warn`（≥2000）就可以換，最晚在 `rollover`（≥2400）之前換。真正碰到上限時的回應沒有驗過（第 11 節），不要等到 `full`。

3. 確認註冊簿上沒有別人正在換這個頻道：看註冊簿最近的留言，沒有 `"channel": "<頻道名>"` 的 `rollover` 還沒落地。

## 3. 開新頁並驗證

1. 在 `<頻道 repo>` 開一張 issue，標題就是頻道名：

   ```bash
   printf 'continued from #%s\n' '<舊頁>' > /path/to/body.md     # 本文只是交互參照，可以省略
   gh issue create -R <頻道 repo> --title '<頻道名>' --body-file /path/to/body.md
   # 印出的 URL 最後一段就是 <新頁>
   ```

   本文用 `--body-file`，不要用 `--body -`。

2. 驗證新頁：

   ```bash
   gh issue view <新頁> -R <頻道 repo> --json title,state --jq '[.title, .state]'
   # 期待：["<頻道名>","OPEN"]
   gh api repos/<頻道 repo>/issues/<新頁> --jq .comments
   # 期待：0
   git -C <config_dir> show origin/main:config/angareion/channels.json | grep -cE '"issue": <新頁>(,|$)'
   # 期待：0（沒有任何頻道、任何退役頁已經用它）
   ```

   - 重建不會查 issue 的標題或是否存在（它只驗留言本身），所以這一步只有你能擋。尤其不能把 `<新頁>` 填成別的頻道的頁：GitHub 後端讀一頁時不依訊息的 `channel` 篩選，填錯會讓這個頻道的讀者收到別的頻道的訊息。
   - 標題不對就改標題，或關掉這張再開一張。不要貼 `rollover`。

## 4. 貼 `rollover` 留言

留言本文恰好是一行 JSON，格式與 `registry.rollover_comment(channel, backend, issue, from_issue)` 的輸出相同（鍵的順序是 `schema, action, channel, backend, issue, from`；分隔是 `", "` 與 `": "`）：

```text
{"schema": 1, "action": "rollover", "channel": "<頻道名>", "backend": "<backend 名>", "issue": <新頁>, "from": <舊頁>}
```

- `schema` 是 `1`；`action` 是 `"rollover"`。
- `issue` 與 `from` 是**正整數，不加引號**，兩者不能相同。`from` 是第 2 節看到的現頁，`issue` 是第 3 節開的新頁。
- 恰好這六個鍵。少一個、多一個（例如多一個 `note`）、`register`／`unregister` 帶 `from`，都會被重建回報並略過。
- 留言裡不要再寫別的字：本文去掉前後空白後必須以 `{` 開頭，而且整段是一個 JSON 物件。

有可以 import `angareion` 的 Python 時，用程式產生，避免手打錯：

```bash
python3 -c 'from angareion.registry import rollover_comment
print(rollover_comment("<頻道名>", "<backend 名>", <新頁>, <舊頁>))' > /path/to/rollover.txt
```

沒有的話，照上面的格式手寫到 `/path/to/rollover.txt`，再貼：

```bash
cat /path/to/rollover.txt          # 再看一次：一行、六個鍵、兩個數字沒有引號
gh issue comment <註冊簿編號> -R <註冊簿 repo> --body-file /path/to/rollover.txt
```

同一則 JSON 不小心貼了兩次沒有關係：第二則是「`issue` 已經是現頁」的 no-op，不回報。

## 5. 等落地

1. **workflow 重建**：註冊簿的留言會觸發 `Angareion registry rebuild`（`.github/workflows/angareion-registry.yml`）。

   ```bash
   gh run list -R <註冊簿 repo> --workflow angareion-registry.yml --limit 3
   # 最新一筆是 completed / success
   ```

   - 沒有觸發時，可以在 Actions 頁面手動 `workflow_dispatch`；每天也會排程跑一次。重建每次都從整串留言重算，重跑是安全的。
   - 看註冊簿上有沒有 workflow 的回報留言。回報的開頭是 `Angareion registry: this comment could not be applied.`，下一行引用你的留言、再下一行是 `Reason: …`。出現了就表示這則 `rollover` 沒有套用：照 `Reason` 修正，再貼一則正確的（舊的那則留著，不要刪）。

2. **main 的 channels.json**：

   ```bash
   git -C <config_dir> fetch --quiet origin main
   git -C <config_dir> show origin/main:config/angareion/channels.json \
     | python3 -c 'import json, sys; print(json.load(sys.stdin)["channels"]["<頻道名>"])'
   # 期待：{'backend': '<backend 名>', 'issue': <新頁>, 'retired': [..., {'issue': <舊頁>, 'until': '<換頁時刻>'}]}
   ```

   `retired` 的最後一筆是 `<舊頁>`、沒有 `side`，`until` 是你那則留言的 `created_at`。

3. **各席位拉到新設定**：每份安裝在下一次執行 angareion 指令、而且距上次拉取超過 `config_pull_interval` 時才會拉。所以從 main 更新起，要等盤點裡**最大的** `config_pull_interval`（預設 900 秒，最長 6 小時），所有席位才都看得到新頁。這段期間是正常的過渡（第 7 節），不必催。

## 6. 驗證

1. `channel status`：

   ```bash
   angareion channel status <頻道名>; echo "exit=$?"
   # name        count  limit  level  pages  flags
   # <頻道名>     0      2500   ok     2      -
   # registry    61     2500   ok     1      -
   # exit=0
   ```

   - `count` 是新頁的則數（剛換時是 0）；`pages` 是現頁加退役頁的數目，比換頁前多 1；`flags` 是 `-`。
   - `channel status` 只在距上次拉取超過 `config_pull_interval` 時才拉設定。剛落地就要看結果，先跑一次第 5 節第 2 步的 `git fetch`。
   - 有人寫進新頁之後，`count` 會從 0 往上加：表示寫者已經寫到新頁。
   - 舊頁的則數直接問 GitHub：`gh api repos/<頻道 repo>/issues/<舊頁> --jq .comments`。過了第 5 節第 3 步的等待之後，它應該不再增加。

2. `read`（用 `--since`，不動游標）：

   ```bash
   angareion read --channel <頻道名> --since <換頁時刻前約 10 分鐘的 RFC 3339 時刻>; echo "exit=$?"
   ```

   - stderr 有一行 `--channel` 會讀到別人訊息的警告，這是正常的。
   - stdout 是這段時間內兩頁上的訊息，依到達順序；exit 0。
   - stderr **不應該**有 `Warning: an older page of channel '<頻道名>' could not be read (HTTP <碼>); skipped`。有的話是舊頁讀不到（第 11 節第 5 項）。
   - 只用 `--since`。PMO 的身分不要跑不帶 `--since` 的 `read`：那會推進游標，而 PMO 的信由 `pmo tick` 讀。

3. （選做）`log`：`angareion log --channel <頻道名> --since <換頁時刻前約 10 分鐘>` 看得到兩頁的訊息。`log` 讀所有的頁、不看寬限期，也不動游標。

## 7. 新舊頁上的訊息的預期行為

手動換頁沒有「舊頁先寫滿」：每個寫者都只寫它本機設定檔裡的現頁。

| 誰、什麼時候 | 會發生什麼 |
|---|---|
| 寫者，本機設定還沒拉到新的 | 照舊寫舊頁。換頁後到拉取之前寫的訊息在舊頁上 |
| 寫者，拉到新的之後 | 寫新頁 |
| 讀者（已升級），設定還沒拉到新的 | 只讀舊頁。新頁上的訊息要等它拉到新設定才讀得到，晚最多一個拉取間隔，不會漏 |
| 讀者（已升級），拉到新的之後 | 讀新頁（第一次從頭讀，因為游標沒讀過這一頁）＋舊頁。舊頁一直讀到讀者游標的時刻超過 `<換頁時刻>`＋48 小時 |
| 新身分、或游標被刪掉的讀者 | 讀現頁，以及最近一次換頁 48 小時內退役的頁；更早退役的頁不讀（要看就用 `log`）。「新身分只收開之前寄給自己的、不收舊廣播」的規則照舊 |
| `wait` | 每一頁各查一次，任一頁有新訊息就回來。舊頁讀不到時安靜地略過 |
| `log`、`decision list` | 讀這個頻道的**所有**頁（不看寬限期），依到達順序合併 |
| 舊頁讀不到（404、410、不是 rate limit 的 403） | `read`／`log` 略過那一頁並在 stderr 印一行警告，其他頁照讀；那一頁的游標不動，恢復後從原處接續，不重送也不漏 |
| 現頁讀不到、rate limit、網路錯 | 照舊 exit 2 |

- 舊頁**不鎖、不關、不加 label**（設計 §1.5 的建議）。鎖擋不住有 push 權限的人，關掉會被誤認成頻道已刪。
- **不要用 `channel delete` 清舊頁**：`channel delete` 關的是現頁，並且把整個頻道（含退役頁）註銷。

## 8. 旁頁（競態）怎麼辨認

旁頁是競態多開的頁：兩則 `rollover` 都寫 `from: <舊頁>`，例如兩個人同時換、或一個人開了兩張新頁各貼一次。重建依留言的 `(created_at, id)` 排序，第一則成立；第二則的頁記成旁頁，照樣被讀，不會被拒。

- **辨認**：
  - channels.json 的 `retired` 裡有一筆 `"side": true`；
  - `channel status` 的 `pages` 比預期多 1；
  - 旁頁上有留言時（7 天內退役的頁才查），`channel status` 的 `flags` 是 `side`。
- **處理**：
  - 讀者照常讀旁頁（從頭讀，寬限期從它自己那則 `rollover` 的時刻算），訊息不會漏，不用補救。
  - 不要從旁頁再貼 `rollover`（`from` 不是現頁，會被回報並略過）。也不要 `unregister` 旁頁：指向退役頁的 `unregister` 是 no-op，只會多一則回報。
  - 空的旁頁就留著，不關、不刪。有 `side` 旗標時告知該頻道的 PM：上面的訊息會送到，只是寫者寫錯了頁。
- **預防**：一個頻道一次只由一個人換（見開頭）；貼之前照第 2 節第 3 步看一次註冊簿。

## 9. 回退

回退是另貼留言，**不要刪或改註冊簿的留言**：重建每次都從整串留言重算，刪改留言沒有設計、也沒有測試；workflow 只在新增留言時觸發，刪除的效果要等下一次排程才出現，而且不留任何紀錄。

1. **`rollover` 還沒套用**（重建回報了，或 channels.json 沒變）：不必回退。照回報的 `Reason` 修正後重貼，或乾脆不換。
2. **新頁的標題錯了**：直接改 issue 標題即可，對應不受影響（重建與讀寫都不看標題）。
3. **往前修**：新頁之後不能用了（例如被刪、被轉移，讀寫都失敗），再開一張正確的新頁，從現在的現頁換過去（`from` 填現在的現頁，也就是剛才的 `<新頁>`）。剛才那頁變成退役頁，上面已有的訊息在寬限期內照樣會讀（讀不到時略過並警告），舊的退役頁也都保留。
4. **回到舊頁**：`<新頁>` 填錯了（例如填成別的頻道的頁，讀者會收到別的頻道的訊息），或決定這次不換。它要讓頻道完全不再讀 `<新頁>`，所以不能用往前修（往前修會讓它成為退役頁、繼續被讀）。連著貼兩則，順序不能反：

   ```text
   {"schema": 1, "action": "unregister", "channel": "<頻道名>", "backend": "<backend 名>", "issue": <新頁>}
   {"schema": 1, "action": "register", "channel": "<頻道名>", "backend": "<backend 名>", "issue": <舊頁>}
   ```

   - 結果是頻道只剩 `<舊頁>`，**`retired` 整個不見**。
   - 換頁之後寫進 `<新頁>` 的訊息，`read`／`wait` 讀不到，`log`／`decision list` 的鏡射也會丟掉它們（它們還在 GitHub 上）。貼之前先用 `log --channel <頻道名> --since <換頁時刻>` 看一遍，必要時請寫的人重送。
   - 兩則之間要是剛好跑完一次重建，頻道會短暫不存在：這段期間 `send` 回「channel 不存在」（exit 1）。兩則連著貼可以讓這個時窗很短（workflow 的新 run 會取消舊 run，最後一次看到兩則）。
   - 不要用 `angareion channel delete` 做第一則：它會把 `<新頁>` 這張 issue 關掉。

**不能回退的**：

- 從 `<新頁>` 再 `rollover` 回 `<舊頁>` 不行：`<舊頁>` 已經在 `retired` 裡，重建當它是 no-op，不回報、不改。
- 「回到舊頁」之後不會留下 `retired`，舊的換頁紀錄只剩註冊簿的留言串。
- 已經寫進 `<新頁>` 的訊息不會自動搬回舊頁；讀者已經讀過的也不會「取消送達」。
- 讀者游標裡已經記下的 `<新頁>` 不會被清掉（沒有害處：頻道不再有那一頁，就不會讀它）。

## 10. `-b` 臨時頻道換頁之後

`-b` 是換頁上線之前，原頻道滿了時另外註冊的臨時頻道（例如 `<頻道名>-b`）。

- **不要把 `-b` 併成原頻道的一頁**（不要用 `rollover` 把原頻道換到 `-b` 的 issue）：`-b` 上的訊息本文寫的是 `-b` 這個頻道，讀者在原頻道的游標也沒讀過那一頁，會把那一頁從頭讀，每一則都再送一次。
- 步驟（設計 §3 的建議，Q-3）：
  1. 原頻道照第 1～6 節換頁、驗證完。
  2. 發訊改回原頻道名。之後誰都不再寫 `-b`（停用）。
  3. `-b` 再留 7 天，讓還沒讀的人讀完（「寄給我的」不指定頻道，所以 `-b` 上的信照收）。這 7 天用 `angareion channel status <頻道名>-b` 確認 `count` 沒有再增加。
  4. 7 天後 `angareion channel delete <頻道名>-b`：關掉它的 issue 並註銷。之後 `log` 就查不到 `-b` 的歷史；要保留歷史就不刪（代價是每次讀「寄給我的」多一個請求）。

## 11. 已知風險

1. **真正的上限回應沒有驗過**：GitHub 在 2500 則時回的狀態碼與內容（只觀察到 403）、上限是不是恰好 `comments ≥ 2500`、`comments` 會不會延遲，都沒驗過。angareion 在 2400 以上被拒就當作「看起來滿了」（`looks full (or the token was refused)`），2500 以上是 `is full`，兩者都是 exit 2、不重試。所以照第 2 節在 `warn` 時就換，不要等到 `full`。
2. **設定沒拉到新版的寫者會繼續寫舊頁**：手動換頁沒有「舊頁先寫滿」的上限，一個寫者在拉到新設定之前寫的訊息都在舊頁上。換頁後 24 小時內這是正常的過渡，已升級的讀者照樣讀得到（舊頁要讀到讀者游標超過 `<換頁時刻>`＋48 小時）。超過 24 小時還有人寫舊頁，`channel status` 會出現 `late` 旗標（只看 7 天內退役的頁）；超過 48 小時才寫進舊頁的訊息，游標已經越過寬限期的讀者就讀不到了（`log` 查得到）。看到 `late` 就找出那個寫者：通常是 `config_pull_interval` 是 0，或它的 `git fetch` 一直失敗。
3. **未升級的讀者**：只讀現頁，舊頁上還沒讀的訊息它讀不到。第 1 節的盤點就是擋這個。
4. **同一身分混用兩版**：舊版會洗掉每頁游標，換過頁的頻道又會在設定晚到時漏訊息（不會重送）。第 1 節第 3 條擋這個。
5. **舊頁在整個寬限期都讀不到**（404、410、不是 rate limit 的 403）：每次 `read`／`log` 都會印跳過的警告。頁恢復之後，只要讀者游標還在寬限期內就會補回；超過了就讀不到（`log` 恢復後查得到）。看到這個警告就去查那張 issue（被刪、被轉移、權限變了）。
6. **註冊簿自己也有 2500 則的上限**：每次換頁加 1 則，競態加 2 則，每一種不同的回報各 1 則。`channel status` 最後一列 `registry` 是它的則數。
7. **跨 issue 的 comment id 是否遞增沒驗過**：送達（不漏、不重送）不依賴它；只有同一次 `read` 回傳的跨頁訊息的相對順序依賴它。
8. **註冊簿列表的複寫延遲**沒有量過：剛貼完就跑重建，理論上可能沒看到最新那則；每天的排程與手動重跑會補上。

# 技術驗證 impl2-S1：opencode 匯入 session，開頭與原 session 位元組相同

- 執行時間：2026-09-28 UTC；執行者：impl2（本機隔離環境，未動 e2e 容器）。
- opencode 版本：**1.18.32**（`opencode --version`；`debug info`：Darwin arm64）。
- **只用自己造的測試 session**，沒讀任何真實 Session 或 MyBrain。隔離方式：
  `XDG_DATA_HOME / XDG_CONFIG_HOME / XDG_STATE_HOME / XDG_CACHE_HOME` 指向 `/tmp/impl2-s1-*`，
  工作目錄 `/tmp/impl2-s1-work`（全新），模型用免費的 `opencode/space-bunny-free`（免金鑰）
  與自建本機 stub（`@ai-sdk/openai-compatible`，見下）。
- 測試 session（SEED，4 則）：user 提示（含 codeword `ALFA-123`，要求讀檔＋跑 echo）
  → assistant（文字＋`read` 工具呼叫）→ assistant（`bash echo TOOL-CHECK-789` 工具呼叫）
  → assistant（總結文字）。另有 2 則的 SMOKE（供 n→1 串接）。

## 結論（先講）

ADR 0010 的「原封不動重建開頭」在 opencode 上**做得到**，做法固定為：

> export JSON 截斷到接續點 → **session／message／part id 全部重編** →
> 在目標專案目錄執行 `opencode import`。

- 1→n：同一截斷點匯入 n 次（每次 id 各編一套），n 個新 session 送模型的請求**位元組完全相同**
  （實測 31538 bytes 全同）。
- 原 session 接續 vs 匯入 session 接續：共同前綴（system＋前 k 則重播）**位元組完全相同**
  （實測前 10469 bytes 全同，差異恰好從歷史分岔處開始）。
- n→1：兩段串接＋重編 id＋手動鏈 parent，import 收、export 正常、接續可跑。
- 限制只有五條（見 Q1、Q3、Q6）：id 必須重編（沿用會被靜默丟棄，且**自編的 id 互相
  重複也一樣被靜默丟棄**——9.1／9.2 的失敗就是這個）、重編後的 id 字典序要跟匯出
  順序一致、匯出順序由 `time_created` 決定（n→1 要把後段的時間往後排）、
  import 強制改寫 directory／project 為當下目錄、skill 重名會讓 system prompt
  在不同 run 翻轉（resident 容器 skill 固定則無此問題）。
- 2026-09-30 補：Q6 是本來沒測到的部分（重編 id 的長度與順序、n→1 的時間與
  parent），後來在 e2e 9.1／9.2 上炸開；證據與修法見
  `docs/spike/evidence/impl2-import-id-collision.md`。
- n→1：送模型的上下文照**時間順序**排（Q6.1），所以後段的時間要整體往後排到第一段
  之後（Q6.2）；parent 鏈只有 assistant 接得起來。

## Q1：官方匯入方式與限制（1.18.32）

| 方式 | 結果 |
|---|---|
| `opencode import <file>`（export JSON，`{info, messages}`） | **可用**（本驗證全部經此路徑；`Imported session: <id>`，rc=0） |
| `opencode import <share URL>` | 有此參數（讀 share 取回扁平資料再組裝，原始碼見 sst/opencode `packages/opencode/src/cli/cmd/import.ts`）；**未實測**（需先分享） |
| `POST /api/session/import`（v2 API 文件有載） | 文件有，**本版未實測** |
| fork：`POST /session/{id}/fork {"messageID"?}`／CLI `run -s <id> --fork` | 1.7h 已驗證：全量或截到指定訊息複製，**訊息 id 全部重編**；CLI 版會多加一輪新訊息 |
| 直接寫 SQLite（`$DATA/opencode.db` 的 `session`／`message`／`part` 表） | 非官方。表結構實測如 pysql 所示；`message.id`、`part.id` 是全域主鍵——自己寫一樣要遵守 id 唯一，不建議 |

實測到的 `import` 行為（與上述原始碼一致）：

1. **message／part id 全域唯一**：與已存在 id 相同的訊息／段落會被 `onConflictDoNothing` **靜默丟棄**。
   同一份 export 原樣改個 session id 再匯入，新 session 是**空的**（0 則，實測）。
   同 id 的 session 再匯入一次則是 no-op（只更新 project／directory／path）。
   **自己編的 id 若互相重複，丟棄的也是同一批**（Q6）——這是 9.1／9.2 的失敗。
2. **directory／project／path 強制改寫為當下 context**：實測把 `info.directory` 改成
   `/no/such/dir` 再匯入，DB 裡仍是執行 import 時的目錄（`global`／`/private/tmp/impl2-s1-work`）。
   → `agora init session` 必須在**目標專案目錄**執行 import（或事後搬）。
3. **parent 不驗證**：指到不存在訊息的 parent 也照收（n→1 實測，見 Q4）。
4. id 格式：`ses_`／`msg_`／`prt_` **前綴一定要有**；前綴之外寬鬆（長度不設限、
   連 `.` `-` `/` 都收）。以數字開頭、沒有前綴的 id 會被**整份拒絕**（Q6 補測，
   這格當時沒測）。
5. `opencode export` 無參數會進互動選單（1.7a 已知）；同步器一律明示 session id。
6. **形狀錯會大聲報錯**：每則訊息與每個 part 都過 `decodeUnknownSync`，缺
   `slug`／`agent`／`model`／`step-start` 之類的欄位就整份拒絕，**不會**靜默丟棄
   （Q6 補測）。所以「匯入之後少了東西」只可能來自 id，不是來自形狀。


## Q2：截斷匯入後接著送新訊息，開頭是否位元組相同

方法：自建 OpenAI-compatible stub provider（`scripts/spike/session_import_stub.py`，
`baseURL http://127.0.0.1:18080/v1`，模型 `stub/stub-echo`），把 `opencode run -s <id>`
每次送模型的完整 request body 存檔，再用 `scripts/spike/session_import_compare.py` 比對。
`--print-logs --log-level DEBUG` 經實測**不印請求內容**（只有 `process`／`stream`／`loop`
生命週期事件），故不用它。**全程沒印任何金鑰**（免費模型＋本機 stub，本來就沒有金鑰）。

抓到的請求形狀（OpenAI-compatible chat completions）：

```
messages[0]  system（約 9.5〜34 KB，視 skill 而定；內含一行模型名）
messages[1..] 歷史重播：user → assistant（文字＋tool_calls）→ tool（工具結果）→ …
最後一則      本次新 user 訊息
tools[]      10 個內建工具定義（bash、edit、glob、grep、read、skill、task、todowrite、webfetch、write）
```

逐項回答：

- **前 k 則位元組相同嗎？相同。** 原 session（4 則全量）接續 vs
  截斷到第 2 則的匯入 session 接續：兩個請求前 **10469 bytes 完全相同**，
  第一個差異位元組恰好是歷史分岔處（匯入側出現新 user 訊息，原側出現第 3 則的
  bash `tool_calls`）。歷史重播的 `tool_call id`（`call_function_…`）與工具結果
  都保留原值、未重編。
- **system prompt 混進開頭嗎？混在歷史之前，但同環境同模型下相同。**
  三次 run 的 system 全同（長度 9556）。注意它內有一行模型名
  （`powered by the model named …`），同模型接續無影響。
- **工具定義混進開頭嗎？在獨立 `tools` 欄位，不插進 messages；三次全同。**
- **時間戳混進開頭嗎？沒有。** payload 內 `ses_`／`msg_`／`prt_` 出現 0 次；
  已知的 `time.created` 值（`1790589…`）出現 0 次。message 的 `time.start/end`
  只留 DB，不送模型。
- **session id 混進開頭嗎？沒有**（同上，0 次）。

環境噪音（已定位，非匯入造成）：本機有 `~/.agents/skills` 與 `~/.claude/skills`
兩套重名 skill，opencode 每次 run 隨機挑一邊的 `<location>` 寫進 system prompt，
導致**開頭第一個位元組起就不相同**。把 skill 路徑正規化後三次請求除分岔外全同；
把 `HOME` 指到空目錄後三次 system 全同。**resident 容器的 skill 集合固定**，
不會有此翻轉；但建議鏡像內去掉重名 skill，並以 `tokens.cache.read` 監控
cache 實際命中（見「給 agora init 的建議」）。

## Q3：1→n——同一截斷點匯入兩次

同一截斷點（SEED 前 2 則）各編一套 id 匯入成 IMP-A／IMP-B，再以**同一句探針**
分別接續，抓到的兩個請求 **FULL 位元組相同（31538／31538）**。
→ n 個 session 開頭完全相同，符合 ADR 0010 對 1→n 共用 cache 的要求。
前提：每次匯入的 msg／part id 必須不同（見 Q1-1；沿用原 id 的第二次匯入是空 session）。

## Q4：n→1——兩段串成一個匯入

取 SEED 前 2 則＋SMOKE 2 則，id 全重編，第二段首則 parent 手工鏈到第一段末則
（註：有兩則 assistant 的 parent 忘記重編、指向原 session 的舊 id——
import **照收不報錯**，正好證明它不驗 parent），匯入結果：

- `import` rc=0；`export` 4 則正常；`session list` 可見新 id。
- stub 接續可跑（1 個請求，31534 bytes）；wire 歷史照**陣列順序**重播
  （seg1 → seg2），工具呼叫重播正常，最後回固定探針答覆。
- 限制：id 衝突規則同 Q1（兩段若來自同源、id 撞了會靜默丟）；parent 鏈、
  時序都由建檔方（`agora init session`）自己保證，opencode 不幫忙檢查。

## Q5：匯入後能不能再 export（給同步器當新 session）

可以。所有匯入的 session（IMP-A／B、ORIG2、MERGE、DIRTEST、TRUNC）都：
`session list --format json` 可見新 id、`opencode export <新id>` rc=0、
頂層形狀與原生 export 相同（`{info, messages}`，`messages[i]={info, parts}`）。
同步器照既有路徑（列舉→export）即可把它當**新 session**送進 Agora；
接續 Link 的建立照 ADR 0010 由提交流程依認領處理。

## Q6：重編 id 的長度與順序（本來沒測，9.1／9.2 上炸開的那格）

2026-09-30 補測，證據在 `docs/spike/evidence/impl2-import-id-collision.md`。這裡的
spike 用 `--tag IMPA`（**4 碼**），`f"msg_{tag}{i:020d}"[:30]` 剛好 28 個字元、
沒有被截斷，所以一直正常；`agora-opencode` 的實作改用**預留 session id 的尾
10 碼**再加段落標記（12 碼），總長 36 就被 `[:30]` 截掉序號，**整批訊息拿到同一個
id**，`import` 靜默丟棄（8 則進、1 則出；重跑時是 0 則）。

實測（容器內、乾淨的資料庫、每個形狀各匯入一次）：

| 匯入的 id | 結果 |
|---|---|
| `msg_01a0ee3e449d0000009479fc`（現在的形狀） | 收；9／9 則、31／31 個 part，內容與順序與來源相同 |
| `00000000000000000000`（以數字開頭、沒前綴） | **拒絕**：`Expected a string starting with "msg"` |
| 66 個字元的 id | 收（沒有長度上限） |
| 含 `.` `-` `_` `/` `?` 的 id | 收（字元集不限制） |
| 全部訊息同一個 id | 收，**靜默丟棄**：9 則進、1 則出 |
| part id 遞減（前綴正確） | 收，但**同一則訊息內 part 的順序被翻轉** |

兩條要記的規則：

- **前綴一定要有**（`ses_`／`msg_／`prt_`）：沒有前綴是整份拒絕，不是靜默丟棄。
  「以數字開頭的 id 沒測過」這格現在補上了：不行。
- **id 的字典序要跟匯出順序一致**。匯出排序是 `ORDER BY time_created, id`（訊息）
  與 `ORDER BY message_id, id`（同一則訊息內的 part）；`time_created` 一樣時
  （合成資料、同毫秒產生）順序由 id 決定——實測把 id 弄成遞減，匯出順序整個翻轉。

### Q6.1 送模型的上下文靠什麼？（parent 樹 vs 時間順序）

補測（同一天，證據與細節在 evidence 第 6.1 節）。四個 session、內容一樣、只改
parent 鏈，用本檔 Q2 的 stub 抓 request body：

| 情境 | 結果 |
|---|---|
| 正常（兩個 user root，assistant 掛在下面） | 歷史全在，照時間順序 |
| 把 user 訊息的 `parentID` 設成前一則 | **匯入後被清掉**（zod 去掉未宣告的欄位），歷史一樣全在 |
| assistant 的 `parentID` 指向不存在的訊息 | 照收，歷史一樣全在 |
| assistant **沒有** `parentID`／`parentID` 是 `null` | **整份匯入被拒絕** |

結論兩條：

1. **上下文是照 `(time_created, id)` 排出來的，不是照 parent 樹**——parent 鏈斷掉、
   清掉、指向不存在，送模型的歷史一模一樣。parent 只負責分支結構。
2. **`parentID` 是 assistant 的必填非 null 欄位，user 訊息不能有它**（真實匯出檔的
   user 訊息就沒有這個 key，`/undo` 之後長的就是多個 root）。

所以 n→1 的兩件事的機制是同一件：**順序由時間承載，parent 鏈能接就接**。

### Q6.2 n→1：後段的時間要整體往後排

兩段的時間交錯時，匯出（以及送模型的上下文）會交錯排列（實測段 1 五則、段 2 三則
變成 `1,2,1,2,1,2,1,1`），ADR 0010 的「最長的一段放最前面」在匯入後就不成立。
轉接器的做法（`loader.py` 的 `shift_plan`／`shift_times`／`merge_offsets`）：

- 第一段位移固定 0（**原封不動**）；
- 後面各段整體往後排到「上一段最後一則的下一毫秒」，**段內相對順序不變**；已經排好
  的段落位移 0；
- 起點包 metadata 記 `time_shift`（`rule: later_segments_after_first`）——宣告規則，
  實際位移由轉接器算（閱讀版的時間只有秒精度）；兩段以上卻沒有宣告就**明確拒絕**；
- 套用完再驗一次（第一段逐欄位沒變、合併後時間非遞減），違了就報錯。

串接則改成「首則是 assistant 才手工鏈到前一段末則；首則是 user 就讓它當 root」，
`reidentify` 也不再把指不到的 parent 清成 null（那會讓 assistant 匯不進去）。

所以 `loader.py` 的重編 id 是三段**固定寬度**、共 28 個字元、時間取自原始紀錄的
`time.created`、序號跨段遞增、鹽由預留 session id 推導（不是隨機：重跑同一個起點包
因此是 no-op，1→n 因為預留 id 不同而不同），**沒有任何可以被截斷的地方**，超出
長度上限時明確報錯。守門是
`tests/integration/test_opencode_load_roundtrip.py`（容器裡真的 load → export →
stub 抓包，不需要模型）。

## 給 `agora init session` 的建議

1. 固定流程：export → 截到接續點（接續點定義沿用 1.7c：快照識別＋message id，
   取到最後一則已完成訊息）→ session／message／part id 全重編（前綴保留，後綴
   「時間＋序號＋鹽」，**不要截斷**；鹽由預留 session id 推導）→ 在目標專案目錄
   `opencode import`。
2. n→1：最長的一段放最前面（ADR 0010 已定），其餘接後；**後段的時間整體往後排到
   第一段之後**（段內相對順序不變，Q6.2——順序是由時間決定的）；parent 只在首則
   是 assistant 時手工鏈（Q6.1）；超 context 上限先明確拒絕（ADR 0010 已定，
   不默默截斷）。
3. resident 鏡像去掉重名 skill（或固定解析順序），否則同批建出的 session
   可能因 system prompt 差幾個位元組而斷 cache。
4. 上線後以 `step-finish.tokens.cache.read` 監控：接續步若 cache 命中，
   其值應接近共同前綴的 token 數；長期偏低表示開頭在漂。
5. fork（`POST /session/{id}/fork`）只適用**同機截斷**；跨機與 n→1 仍走
   import＋重編 id。

## 再現步驟

```bash
# 0. 隔離（不要碰真實 Session；秘密只用路徑，本驗證無需任何金鑰）
export XDG_DATA_HOME=/tmp/s1-data XDG_CONFIG_HOME=/tmp/s1-config \
  XDG_STATE_HOME=/tmp/s1-state XDG_CACHE_HOME=/tmp/s1-cache
mkdir -p /tmp/s1-work && cd /tmp/s1-work

# 1. 造 session（含一次工具呼叫），免費模型即可
#    不想真的呼叫模型時，用 fixtures 腳本造一份形狀真實的匯出檔（Q6 的說明）：
#      python3 scripts/spike/session_import_fixture.py seed-src.json ses_SEED01
#      opencode import seed-src.json && opencode export ses_SEED01 > seed.json
echo "fixture alpha" > fixture.txt
opencode run --auto -m opencode/space-bunny-free --title SEED \
  "Remember codeword ALFA-123. Read fixture.txt, run 'echo TOOL-CHECK-789', reply one sentence."
opencode export $(opencode session list --format json | python3 -c \
  "import json,sys; print(json.load(sys.stdin)[0]['id'])") > seed.json

# 2. 截斷＋重編 id＋匯入（1→n 就跑兩次，--tag 不同）
python3 scripts/spike/session_import_make.py seed.json impA.json \
  --session-id ses_IMPA01 --title IMP-A --take 2 --tag IMPA
opencode import impA.json

# 3. 起 stub 抓請求（另開終端）
python3 scripts/spike/session_import_stub.py --port 18080 --dir caps
# opencode.json 加 custom provider stub（baseURL http://127.0.0.1:18080/v1，見本檔 Q2）
opencode run -m stub/stub-echo -s ses_IMPA01 "PROBE"
opencode run -m stub/stub-echo -s ses_IMPB01 "PROBE"

# 4. 比對
python3 scripts/spike/session_import_compare.py caps/req-001.json caps/req-002.json
```

匯入之後**一定要再 export 一次數訊息**（Q6 的教訓）：`import` 對重複 id 是靜默
丟棄，rc=0、沒有任何訊息，只看匯入檔看不出來。

## 腳本

- `scripts/spike/session_import_make.py`：截斷＋重編 id（**這支的 `--tag` 只有 4 碼，
  所以不會觸發 Q6 的截斷**；正式實作在 `loader.py`，id 是固定寬度、不截斷）。
- `scripts/spike/session_import_fixture.py`：造形狀真實的匯出檔（測試資料，不碰真實
  Session）。`import` 會驗每一個欄位，所以樣本的形狀必須真的對得上。
- `scripts/spike/session_import_stub.py`：本機 stub provider，存 request body。
- `scripts/spike/session_import_compare.py`：兩次請求的位元組比對。
- `tests/integration/test_opencode_load_roundtrip.py`：Q6 的守門——在真的 resident
  容器裡 load → export → stub 抓包，**不需要模型、Drive 或網路**。

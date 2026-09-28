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
- 限制只有三條（見 Q1、Q3）：id 必須重編（沿用會被靜默丟棄）、import 強制改寫
  directory／project 為當下目錄、skill 重名會讓 system prompt 在不同 run 翻轉
  （resident 容器 skill 固定則無此問題）。

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
2. **directory／project／path 強制改寫為當下 context**：實測把 `info.directory` 改成
   `/no/such/dir` 再匯入，DB 裡仍是執行 import 時的目錄（`global`／`/private/tmp/impl2-s1-work`）。
   → `agora init session` 必須在**目標專案目錄**執行 import（或事後搬）。
3. **parent 不驗證**：指到不存在訊息的 parent 也照收（n→1 實測，見 Q4）。
4. id 格式寬鬆：`ses_`／`msg_`／`prt_` 前綴＋自定字串可收（未測極端格式）。
5. `opencode export` 無參數會進互動選單（1.7a 已知）；同步器一律明示 session id。

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

## 給 `agora init session` 的建議

1. 固定流程：export → 截到接續點（接續點定義沿用 1.7c：快照識別＋message id，
   取到最後一則已完成訊息）→ session／message／part id 全重編（前綴保留，
   後綴隨機；同一次 1→n 的 n 個各編一套）→ 在目標專案目錄 `opencode import`。
2. n→1：最長的一段放最前面（ADR 0010 已定），其餘接後；parent 手工鏈成一串；
   超 context 上限先明確拒絕（ADR 0010 已定，不默默截斷）。
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

## 腳本

- `scripts/spike/session_import_make.py`：截斷＋重編 id。
- `scripts/spike/session_import_stub.py`：本機 stub provider，存 request body。
- `scripts/spike/session_import_compare.py`：兩次請求的位元組比對。

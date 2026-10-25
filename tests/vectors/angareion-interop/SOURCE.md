# angareion-interop 來源與用法

Python↔TS 互讀測試資料（MyPMO#165 2.3；M2a §3 的 a～d）。測試：`test/unit/infra/angareion/messages.test.ts`（(a)(b)(c)，describe「angareion interop fixtures (a)(b)(c)」）與 `githubAngareionClient.test.ts`（(d)，describe「angareion interop fixtures (d)」）。

## 檔案

| 檔 | 內容 | 誰產生 |
|---|---|---|
| `inputs.json` | 九則假訊息的欄位（假 AI 名、固定 id／at） | 手寫 |
| `python-written/*.md` | MyAiStorage `serialize_message` 實際輸出 | `generate_python_written.py` |
| `expected.json` | 每則的期望欄位＋五位假讀者的 recipient／mention 過濾結果 | 同上（過濾結果來自 Python 真的 `recipient_matches`／`mention_matches`） |
| `ts-written/*.md` | 同一組欄位由 TS `serializeMessage` 輸出 | TS 測試（見下） |
| `generate_python_written.py` | 重產 python-written 與 expected.json | — |

九則：01 含 mentions＋非空 attachments（ref 與 inline 各一）＋in_reply_to；02 只有 mentions 的廣播；03 `to` 與 mentions 都有；04 舊樣子（有 `to`、無新欄位）；05 舊樣子的廣播；06 多行 inline（含空行、`: `、`- ` 開頭的行）；07 超過 80 欄的 inline（PyYAML 折行）與雙引號折行；08 含中文的 inline；09 含單引號、`#`、雙引號、反斜線與 tab 的 inline。

## 來源

- 來源 repo：FATESAIKOU/MyAiStorage
- 來源 commit：`8d97ebc`（#209 的 `1e69b31`、`8d97ebc`；`src/angareion/messages.py` 的 `serialize_message`、`src/angareion/backend.py` 的 `recipient_matches`／`mention_matches`）
- 重產 python-written／expected.json：把 MyAiStorage `8d97ebc` 匯出到某目錄（`git archive 8d97ebc src | tar -x -C <dir>`），再
  `python3 -I test/fixtures/angareion-interop/generate_python_written.py <dir>/src test/fixtures/angareion-interop`
- 重產 ts-written：`UPDATE_INTEROP_FIXTURES=1 npx vitest run --maxWorkers=2 test/unit/infra/angareion/messages.test.ts`（平常不設這個變數，檔案與序列化器不符就轉紅）。**重產 ts-written 不等於驗過**：之後必須在 MyAiStorage 端重跑 Python 讀取（下面待辦 2、3），重產會把序列化器的任何改動直接寫進金檔。

## TS 測試做什麼

- (a) 讀 python-written，欄位＝expected.json
- (b) ts-written＝目前 TS 序列化器輸出；ts-written 與 python-written 每則解析結果相同
- (c) 不認得的頂層鍵不影響其他欄位（舊版讀者另在 scratch 以 9bb132b 的 messages.ts 驗過，結果在 #207 報告）
- (d) TS `recipient_matches`＝expected.json 的 recipient；mention 過濾 TS 沒有實作（本單不加讀取模式），測試內用參考函式對照 expected.json 的 mention

## MyAiStorage 那邊要補什麼

1. 把**整個目錄**複製進 MyAiStorage（例如 `tests/vectors/angareion-interop/`）：包含 `inputs.json`、`generate_python_written.py`、`SOURCE.md`、`expected.json` 與 `python-written/`、`ts-written/` 兩個目錄，不只 md 檔；並在複製處釘 **MyAiEntry 的 commit**（#207 合併後的 sha；複製時填入）。
2. Python 測試：`parse_message` 讀 `ts-written/*.md`，欄位＝expected.json 的 messages（M2a §3 的 b）。
3. Python 測試：對 expected.json 的每位 reader，`recipient_matches`／`mention_matches` 對 `python-written` 與 `ts-written` 兩個目錄的每則，結果都＝expected.json（d 的 Python 端）。
4. Python 讀 `python-written/*.md` 與 expected.json 一致（防 generate 後忘記提交）。
5. generate_python_written.py 的 `Message` 欄位若變，兩邊一起更新。
6. M2a §3 (c) 的 Python 半（舊版 Python 讀者讀 TS 新訊息）：請 MyAiStorage 確認 #209 是否已涵蓋；本單沒有驗，也沒有對應測試。

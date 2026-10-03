**超過了：HEAD 的 `src/` 程式碼行是 3,121 行，比 2,900 多 221 行。** 把 cli／store／cache 裡下面列的重複全部合併，大約可以省 75 行，會降到約 3,045 行，**還是超過 2,900 約 145 行**。所以光靠去重複做不到，剩下的差距要另外決定（見最後一節）。

# Review：T1 之後的程式碼行數，以及 cli／store／cache 的重複

2026-10-03，review。對象是 HEAD `55a5f0c`（用 `git archive HEAD src` 取出來量，不含工作目錄裡別人還沒 commit 的改動）。只提意見，沒有改程式；沒有跑整合測試，沒有碰 Drive。

## 1. 行數

算法同 design 的 L4：不含空行、註解、docstring（模組、類別、函式的第一個字串）。用 `tokenize` 找出有 token 的行，再扣掉 `ast` 找到的 docstring 行（腳本附在最後）。

| 檔案 | 程式碼行 | 檔案總行數 | T1 之前（`9535ca3^`） | 差 |
|---|---:|---:|---:|---:|
| cli.py | 707 | 920 | 585 | +122 |
| agents/opencode.py | 530 | 858 | 490 | +40 |
| tui.py | 563 | 709 | 556 | +7 |
| agents/claude.py | 442 | 632 | 442 | 0 |
| store.py | 440 | 657 | 390 | +50 |
| cache.py | 207 | 310 | 71 | +136 |
| header.py | 169 | 264 | 169 | 0 |
| agents/base.py | 63 | 131 | 63 | 0 |
| **合計** | **3,121** | **4,483** | **2,766** | **+355** |

T1 之前就已經是 2,766 行，只剩 134 行的空間；T1 加了 355 行（cli +122、cache +136、store +50），opencode 的 +40 是 T2 加的。

## 2. 可以合併或刪掉的重複

「省」是估計可以少掉的程式碼行。風險：**低** = 純粹搬動，行為不變，現有測試就蓋得到；**中** = 碰到安全相關的路徑，要加一個測試或仔細對照。

### store.py

| # | 位置 | 重複的內容 | 建議 | 省 | 風險 |
|---|---|---|---|---:|---|
| S1 | `sync` 的逐筆迴圈（566–584）和 `cache._pull_agora`（227–241） | 兩邊都做同一件事：md5 不同就下載 session.md → 解析 → raw 還沒齊就 `index.drop` 並刪掉本機那份（G3）→ 放進索引 | 抽出一個 `store.mirror_one(paths, drive, index, ulid, files) -> hdr \| None`，sync 用 try 包起來，pull 直接呼叫。順便讓 G3 的邏輯只有一份，以後不會一邊改了另一邊忘了改 | ~10 | 中：G3 的路徑。現有的 `raw 還沒齊` 測試兩邊都有，可以撐住 |
| S2 | `push_one`（267–285）和 `push_mirror`（288–311） | 都是先傳 raw、再傳 session.md，然後 `list_one`、比對兩個 md5 | 抽出一個 `_upload_checked(drive, folder, ulid, raw) -> remote`。`push_mirror` 在 raw 不在本機時先 warn，再傳 `raw=None` | ~7 | 中：`_fault` 的兩個點會跟著出現在 mirror 路徑上（沒有害處，但要確認故障測試只在 outbox 情境設定）；錯誤訊息要用參數帶上「留在 outbox」 |
| S3 | `Index.__init__` 的版本檢查，加上 `sync` 和 `cmd_search --no-sync` 各呼叫一次 `rebuild_from_mirror` | 有三個地方在處理「索引空了，要從鏡像重建」 | 在 `__init__` 裡 drop 表之後就**直接**從鏡像重建（新建的 db 的 `user_version` 是 0，所以「db 被刪掉」也會走到這條路），然後拿掉 `not self.known()` 的判斷和另外兩個呼叫。**這同時也是 T1-sec3 M3 的修法** | ~3 | 低～中：要加一個 M3 的測試 |
| S4 | `push_outbox` 的 `if not paths.outbox.exists(): return failed` | `outbox_ulids` 已經處理了 outbox 不存在的情況 | 刪掉 | 2 | 低 |
| S5 | `index_mirror`（只有 cache 用一次）和 `_put_file` | 只是多包了一層 | cache 直接呼叫（把 `_put_file` 改名成 public 的 `index_file`） | 2 | 低 |

`outbox_count` 看起來多餘，但測試有 6 個地方在用，所以留著。

### cache.py

| # | 位置 | 重複的內容 | 建議 | 省 | 風險 |
|---|---|---|---|---:|---|
| C1 | `_unique`（7 行），而且 `pull` 裡呼叫了兩次 | 和 `dict.fromkeys` 做的事一樣 | `wanted = list(dict.fromkeys(ids))`，在 pull 和 push 開頭各算一次 | 6 | 低 |
| C2 | `_plan_one`，加上 `pull` 開頭的預先掃描與 `offline` 變數 | 只是為了知道「這批裡有沒有 agora 的 id」才先解析一輪 | 改成在迴圈裡遇到**第一個** agora id 時才列 Drive（lazy）。這樣「整批都是 agent 的 id 就完全不碰 Drive」（S2-3）的行為不變，壞掉的 id 也還是只有它自己失敗 | ~8 | 低～中：S2-3 已經有測試 |
| C3 | `_line` 和 `store._warn` 一字不差；cache 的 `_progress` 和 cli 的 `_progress` 幾乎一樣 | 一樣的東西寫了兩份 | 在 store 留一個 `warn` 和一個 `progress(word, k, total, item="")`，cache 和 cli 都 import 它 | 4 | 低 |
| C4 | `local_reading` 的暫存檔加上 `os.replace`（54–61），和 `cli._cache_section`（490–498） | 同一套原子寫入寫了兩次（K4、S1-9） | 抽成 `store.write_atomic(path, text)` | ~7 | 低 |
| C5 | `_drop_reading` 的兩個分支、`_listed`（只用一次）、`_looks_like_uuid`（6 行） | 太囉嗦 | 一行三元運算；`listed.setdefault(kind, {…})`；用 `re.fullmatch` 搭配 uuid 的 pattern | ~6 | 低 |
| C6 | `_pull_agora` 和 `push` 裡「本機和雲端都沒有這個 Session／雲端沒有，本機的不動」那段 | 同一個判斷寫了兩次 | 抽成 `_absent(paths, ulid, said)` | ~2 | 低 |
| C7 | `pull` 和 `push` 各自寫了一次「列 Drive，失敗就記下 offline」 | 一樣的 try/except | 和 C2 一起做：共用一個 `_listing(drive)` | ~2 | 低 |

### cli.py

| # | 位置 | 重複的內容 | 建議 | 省 | 風險 |
|---|---|---|---|---:|---|
| L1 | `cmd_pull`／`cmd_push`（各 7 行） | 只差了函式、旗標和一個字 | 合併成一個 `cmd_pull_push`，用一張小表帶入差異 | ~5 | 低 |
| L2 | `_need_in_cloud` 的錯誤訊息和 `cmd_show` 的提示 | 兩段同樣的「怎麼傳回去／怎麼刪掉」文字 | 抽成 `_cloud_lost(agora_id) -> str`，兩邊共用，也能保證兩邊的說法一致 | ~3 | 低 |
| L3 | merge、continue、edit 三個地方都先 `_need_in_cloud`、再 `_header_for` | 每次都成對呼叫 | 改成 `_header_for(index, id, writing=True)` | ~2 | 低 |
| L4 | `[i.strip() for raw in … for i in raw.split(",") if i.strip()]` 出現 4 次（import、merge、delete、`_ids_of`） | 同一個切 id 的寫法 | 抽成 `_split_ids(values)`。**順便修一個小 bug**：`_ids_of` 沒有 strip，所以 `agora pull session "a, b"` 的第二個 id 會帶著前面的空白，被報成「本機和雲端都沒有」 | ~2 | 低 |
| L5 | actor 字串 `f"{ACTOR[…]}/{model}" if model else ACTOR[…]` 出現 3 次；sources 裡 agent 那一筆在 `_auto_header` 和 `_finish` 各寫了一次；說明文字截到 80 字寫了兩次 | 小段重複 | 抽成 `_actor(name, model)`、`_agent_source(agent, exported, actor)`、`_short(text)` | ~4 | 低 |
| L6 | `cmd_delete` 的「雲端沒有就只刪本機」分支（727–728），和 `store.delete_session` 結尾的 `forget_local` 加上刪 outbox | 一樣的兩行 | `delete_session(paths, drive, ulid, in_cloud=…)` 自己處理；Drive 也只建一次，不要每個 id 都 `store.Drive(paths)` 一次 | ~2 | 低～中：**不要**把刪 outbox 放進 `forget_local`，因為 pull 那條路靠的就是「outbox 不刪」（Q2） |
| L7 | `main` 裡的 `outbox_count`／`bad_count` 各呼叫兩次 | 每次都會掃一次資料夾，`outbox_ulids` 還會順便還原 `.old-*` | 用 walrus 只算一次 | 0 | 低（行數不變，只是少做一次） |

### 合計

store ~24、cache ~35、cli ~18，**大約 75 行**（加起來是 77；有些項目合併時會互相抵掉）。做完是 **≈3,045 行**。建議的順序是先做 S1、S3、C1～C4、L1、L2：這些省得最多，而且 S1 和 S3 本身就在修 T1-sec3 的 M3，也讓 G3 的邏輯只剩一份。

## 3. 剩下的約 145 行

不在這次指定的範圍，只是列出來給 PM 判斷：

- **需要使用者決定（建議先問）**：額度再放寬一次（從 2,000 放寬到 2,900，前幾次都是使用者決定的），或者接受「T1 和 T2 做完之後約 3,050 行」。
- **再找**：`agents/opencode.py`（530 行，T2 +40）和 `agents/claude.py`（442 行）的 simplify。兩個 adapter 加起來有 972 行，是剩下最大的一塊，但能省多少要另外開一輪 review 才知道。
- **不建議**：為了行數把 cli 裡的 `SECTION_SCHEMA`／`STORED_SECTION_SCHEMA` 和提示文字搬到資料檔（它們算程式碼行，約 30 行）。搬了只是把行數藏起來，而且會多一個讀檔失敗的路徑。

## 這次做了什麼

讀了 HEAD 的 `cli.py`、`store.py`、`cache.py` 全文；用 `grep` 查了候選 helper 在 `src/` 和 `tests/` 裡的使用處（確認 `outbox_count`、`push_mirror` 有被測試直接使用，所以不刪）；用下面的腳本量了 HEAD 和 `9535ca3^` 的行數，以及每個函式的行數。沒有改程式，沒有跑整合測試，沒有碰 Drive。

```python
# 程式碼行 = 有 token 的行 − docstring 行（不含空行、註解）
import ast, io, sys, tokenize, pathlib
def sloc(path):
    src = path.read_text(); tree = ast.parse(src); doc = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                doc.update(range(b[0].lineno, b[0].end_lineno + 1))
    code = set()
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
            code.update(range(t.start[0], t.end[0] + 1))
    return len(code - doc)
print(sum(sloc(p) for p in pathlib.Path(sys.argv[1]).rglob("*.py")))
```

## 去重複確認（2026-10-03）

對象：`8cc6838`（第 2 節的建議）。PM 特別點名的上傳路徑（`_upload_checked`）和鏡像（`mirror_one`）是更早的 `9e44286`、`7029b68` 合併的，所以也照 HEAD 的樣子一起看了。在 `git archive HEAD` 取出的副本跑單元測試：**387 passed**。call 的次數是在副本裡加探測測試，用 fake rclone 的 `calls.log` 數出來的，repo 沒有動。沒有跑整合測試，沒有碰 Drive，也沒有讀任何真實的 Session。

### D1（Medium）：outbox 的每一筆都上傳**兩次**

`push_one` 合併到 `_upload_checked` 的時候，只拿掉了原本的 md5 檢查，**自己上傳的那兩行沒有拿掉**。現在的流程是：先傳 raw → `_fault("after-raw-upload")` → 傳 session.md → `_fault("after-session-upload")` → 然後 `_upload_checked` **再傳一次 raw 和 session.md**，接著才列檔、比對 md5。實測一次 `push_one`（含 raw）的 rclone 呼叫：

| | copyto | lsjson |
|---|---:|---:|
| `5eab055`（合併之前） | 2 | 2 |
| HEAD | **4** | 2 |

結果是對的，但 import、merge、continue、edit 每一次存檔（`_save` → `push_one`），以及每一次推 outbox，網路傳輸都變成兩倍，raw 可能有好幾 MB；每一次 copyto 本身也要好幾秒（docs/perf.md）。這是 `9e44286` 帶進來的，`8cc6838` 沒有動到。**沒有任何測試抓得到**：沒有測試在數 push 的 copyto 次數。

**修法**：`push_one` 不要自己上傳，全部交給 `_upload_checked`，並且把兩個 `_fault` 搬進 `_upload_checked` 裡原本的位置（raw 之後、session.md 之後）。這兩個點在 mirror 那條路徑上也會出現，但不設 `AGORA_TEST_FAULT` 的時候什麼都不做，沒有影響。**要小心的是**：如果只是刪掉 `push_one` 裡那兩行，`_fault` 也會跟著消失，test-plan 的 I-08（`after-raw-upload`）就測不到了。目前單元測試裡沒有任何一個用到這兩個 fault 點，只有整合測試會用。另外補一個測試：一次 push_one 的 copyto 數 = 原始檔數 + 1。

### 其他確認

| 項目 | 結果 |
|---|---|
| 上傳順序（R7、S2-7） | ✅ 都是 raw 先、session.md 後，然後用 Drive 的 md5 決定；只送標頭指到的那兩個檔案。mirror 路徑的「本機沒有那個 raw 就只傳 session.md 並警告」保留下來了；`--not-exist-upload` 的 `need_raw` 拒絕還在 cache 那一層 |
| `_fault` 的位置 | ✅ 相對於**第一次**上傳的位置沒有變（raw 之後、session.md 之後）；但修 D1 的時候要一起搬過去（見上面） |
| 舊 raw 的清理 | ✅ `push_one` 還是只刪 `raw-*` 裡不是現在那個的；mirror 路徑一樣不刪 |
| `mirror_one`（G3：半套的 Session 不進鏡像也不進索引） | ✅ pull 改用它，判斷和原本一樣（標頭指到的 raw 不在，或 md5 不符 → 刪掉本機的 session.md、從索引拿掉）。差別：pull 現在會在 `fetch_raw` **之前**就把新的標頭放進索引，所以 fetch_raw 失敗的時候，索引已經是新的版本、原始檔則等要用的時候再拿；之前是舊的那一列留著。我認為這樣比較一致，不算回歸。sync 還是保留自己那一份（有 `h.validate` 的警告），**G3 的邏輯還是有兩份**，T1-size 的 S1 只做了一半 |
| `write_atomic` | ✅ 兩個地方都是 mkstemp → 寫入 → `os.replace` → 在 finally 裡清掉暫存檔，行為一樣。唯一的差別：全文快取的暫存檔名稱從 `.s.md.XXXX.tmp`（隱藏檔）變成 `s.mdXXXX.tmp`，`*.md` 的 glob（搜尋、`_cached`）不會比對到它，沒有影響。K4 的測試（`test_two_threads_caching_two_sessions_do_not_collide`）還在，也照樣通過 |
| `warn`／`progress` | ✅ 輸出的格式一字不差（cache 的 `pull k/N` 沒有 item，`.rstrip()` 之後一樣）。`sync(warn=warn)` 和 `push_outbox(warn=warn)` 的參數名稱會蓋過模組裡的 `warn`；現在沒有人傳 `warn=None`，所以沒有問題，但以後要是有人傳 None，就會 TypeError。`quarantine` 還是直接用模組的 `warn`，互動模式的等待視窗看不到它（T2-sec1 L5 剩下的那一半） |
| `_pull_or_push` | ✅ 訊息、flag、exit code 都和原本一樣 |
| delete 只建一個 Drive | ✅ `Drive()` 只讀 config.json，不碰網路；唯一的差別是：全部都是「雲端沒有、只刪本機」的情況下，也會先讀一次 config.json |
| `push_outbox` 拿掉 `outbox.exists()` | ✅ `outbox_ulids` 遇到不存在的目錄會回傳空的集合 |

### 行數（HEAD `68fafee`，算法同第 1 節）

| | cache | cli | store | tui | 合計 |
|---|---:|---:|---:|---:|---:|
| `55a5f0c`（第 1 節量的時候） | 207 | 707 | 440 | 563 | **3,121** |
| `9e44286`（T2 第 1 節） | 208 | 725 | 455 | 692 | 3,292 |
| `7029b68`（F1～F7） | 192 | 735 | 455 | 692 | 3,286 |
| `8cc6838`（這次的去重複） | 180 | 726 | 464 | 692 | **3,274** |

（opencode 538、claude 442、header 169、base 63，這段期間都沒有變；opencode 比第 1 節的時候多了 8 行，是 T2 1.6 加的。）

`8cc6838` 本身只淨省了 **12 行**（cache −12、cli −9、store +9：多出來的是共用函式）。從第 1 節到現在的總變化是 **+153**，主要是 T2 的互動模式（tui +129）和 T1-sec3 的修正（cli +19、store +24）。第 2 節估計的「約 75 行」，到目前為止做到的有：C1、C3、C4、C6、C7、L1、L4、L5 的一部分、L6、S2（不過造成了 D1）、S3、S4、S5，以及 S1 的一半；還沒做的有 S1 的 sync 那一半、C5、L2、L3。照現在的速度，HEAD 離 2,900 有 **374 行**，比第 1 節的時候（221）差得更多了，額度的決定更需要使用者來做。

**結論**：`8cc6838` 本身沒有改變行為，原本的情況也都還有測試在測。但上傳路徑有 **D1（Medium）**：每一筆 outbox 都會上傳兩次，修的時候要記得把 `_fault` 一起搬過去，並且補一個數 copyto 次數的測試。

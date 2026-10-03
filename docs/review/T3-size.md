# T3 的行數：精簡的估算（給使用者做決定）

2026-10-03，review。只讀程式，沒有改任何東西。算法同 `docs/review/T1-size.md`：不含空行、註解、docstring。

**結論先講**：
- T3 第 1、2 節，在這四個檔案一共加了 **+267 行**（HEAD `c10013c`／`943d44d`，src 合計 3,676）；
- 下面列的精簡全部做完，大約可以省 **60～66 行**，而且其中一項（E1）同時補上了一個正確性的漏洞；
- T3 第 3、4 節加上 T3-sec3 的修正都做完之後，預估是 **3,780 行左右**，做了精簡則大約是 **3,720**。

不論做不做精簡，都比 2,900 多出 800 行以上，額度要使用者決定。

## T3 加了多少

| 檔案 | T3 之前（`6933935^`） | HEAD | 差 |
|---|---:|---:|---:|
| background.py（新的） | 0 | 87 | +87 |
| store.py | 478 | 634 | +156 |
| cache.py | 189 | 204 | +15 |
| cli.py | 730 | 739 | +9 |
| **小計** | | | **+267** |

這段期間 T4 讓 adapters 少了 36 行，tui 沒有變。proposal 估的是 70～120 行（`6046d62` 的版本）；多出來的，主要是 H1（`.done-` 改名再比對，約 30 行）、一把鎖的 helper（約 34 行），以及批次上傳本身（`upload_batch` 56 行，加上周邊的 helper 約 35 行）。

工作目錄（impl1、impl2 還沒 commit 的第 3 節和修正，**只讀**量的）目前是 **3,730**（store 668、background 94、cli 747、tui 842、cache 203）。

## 可以精簡的地方

「省」是估計可以少掉的程式碼行；**低** = 純粹搬動或合併，行為不變；**中** = 會碰到行為，或者要先做一個設計上的決定。

| # | 位置 | 現在 | 建議 | 省 | 風險 |
|---|---|---|---|---:|---|
| E1 | `store.push_one`（9 行）、`cache.push` 裡的 `elif (paths.outbox / ulid).is_dir(): store.push_one(...)` | 舊的單筆上傳路徑還留著，只剩 `push` 那一個分支在用（快照之後才進 outbox 的那一筆）。**而且它傳完、驗完就直接 `rmtree(folder)`，沒有 H1 的 `.done-` 比對**，所以 push 這條路少了 H1 的保護 | 那個分支改成再跑一次 `upload_batch`（或者把那一個 ULID 算進 `staged`），刪掉 `push_one` | 9 | **低**，而且同時**補上正確性**：push 也只會刪掉自己驗過的版本 |
| E2 | 鎖：`background._take`（9 行）、`store.hold_upload_lock`（12 行）、`_Held`（7 行）、`uploader_is_running`（6 行） | 同一把鎖有兩套拿的方式；`_Held` 只是為了 `with` 和放開鎖 | 只留 `store.hold_upload_lock`，直接回傳打開的檔案（關掉 fd 的時候 flock 自己就會放開，所以 `with open(...)` 就夠了，不需要 `_Held`）；`background` 也改用它 | ~15 | 低 |
| E3 | `outbox_ulids` 每次都呼叫 `uploader_is_running` → `_restore_done` | 為了還原當掉之後留下來的 `.done-`，每一次列 outbox 都要短暫地去拿一次鎖。T3-sec3 R6 的「剛啟動的背景以為已經有人在跑」，就是這個造成的 | `_restore_done` 改成**只在背景拿到鎖之後**做一次（`background.run` 的開頭），`outbox_ulids` 就不用再碰鎖了 | ~3 | 低～中：同時修好 R6 |
| E4 | `Drive.download_many`、`_copy_batch`、`_delete_batch` | 三份「寫一個 `--files-from` 清單、跑 rclone、刪掉清單」 | 抽成一個 `_with_files_from(drive, paths, names, *args)` | ~10 | 低 |
| E5 | `store._listing_with_md5` 和 `cache._listing` | 兩份「列檔，失敗就回傳一個代表失敗的值」（一個回傳 None，一個回傳例外） | 只留一份（例如 store 裡的，回傳例外），cache 也用它 | ~4 | 低 |
| E6 | `_entry_md5s`、`_raw_of`、`_raw_names`、`_replaced_raws` | `_raw_of` 為了拿原始檔的名字，又讀了一次標頭 | `_entry_md5s` 一起回傳原始檔的名字，拿掉 `_raw_of` | ~4 | 低 |
| E7 | `_rename_back`（8 行）和 `_restore_done`（11 行） | 都是「`outbox/X` 已經有新的就丟掉 `.done-X`，不然就改名回去」 | 共用一個 `_put_back(done, folder)` | ~4 | 低 |
| E8 | `cache.push` 等鎖的迴圈 | 用的是阻塞的 flock，所以「每 10 秒提醒」那一段永遠不會執行到（T3-sec3 R7） | **二選一**（要決定）：(a) 接受阻塞地等，換成一行 `with store.hold_upload_lock(paths, blocking=True):`，省 6 行，但和 design N2 的「等自己的 id、每 1 秒看一次」不同；(b) 照 design 做，**多** 3～5 行 | −6 或 +4 | 中：要決定 |
| E9 | `background._trim_log`（11 行） | 超過 1 MB 時，只保留最後 256 KB | 超過 1 MB 就整個清掉重寫（記錄檔只是給人除錯用的） | ~7 | 低（只是少了舊的記錄） |
| E10 | `background` 裡的 `log_path`、`lock_path`、`upload_once` | 只被呼叫一兩次的小函式 | 直接寫在用到的地方（`lock_path` 如果照 E2 搬到 store，就留在 store） | ~4 | 低 |

**建議做的（E1～E7、E9、E10）合計：約 60 行**；E8 選 (a) 的話，再省 6 行，總共約 66 行。

### 不建議拿掉的「保險」

| 保險 | 大約多少行 | 為什麼不建議拿掉 |
|---|---:|---|
| H1：`.done-` 改名再比對 | ~30（含 `_restore_done`、`_rename_back`） | 這是「上傳的時候又 edit 同一個 Session」不會掉資料的唯一保護（T3-sec3 R4 實測過：拿掉比對，edit 就不見了） |
| L7、N4、N5：`.update` 記號，以及「Drive 上沒有就不傳回去、另存成新的」 | 現在約 14，N5 做完再多約 15 | 拿掉的話，別台刪掉的 Session，會被這台還沒傳上去的 edit 悄悄傳回去，這違反 T1 的 Q1（刪除和復活只能由使用者明確決定）。**要拿掉，就要使用者推翻 Q1** |
| `AGORA_UPLOAD=inline`（`background.start` 裡約 5 行） | ~5 | 這是給測試用的開關，放在正式的程式裡。可以搬到測試那邊（由 conftest monkeypatch `background.start`），省 5 行，但既有的幾百個測試都靠它，改了要動 conftest；划不來 |

## T3 第 3、4 節做完之後，大約會到多少

| 項目 | 估計 |
|---|---:|
| 工作目錄目前（第 3 節的一部分，還沒 commit） | 3,730 |
| 第 3 節剩下的：`process_trash_queue`（purge 加上 S1-4／S1-4b，失敗的共用一次列檔）、佇列的提醒、pull 改成算失敗 | +20～30 |
| T3-sec3 的修正：R1（刪一行，加上每一筆接住例外）、R2（N5 另存成新的 Session）、R5～R10 | +20～30 |
| 第 4 節（互動模式的說明；雲端欄的「未上傳」本來就有） | +5～10 |
| **第 3、4 節做完** | **≈ 3,775～3,800** |
| 減去上面建議的精簡（約 60～66） | **≈ 3,710～3,740** |

（量的是 `git archive` 和工作目錄的 `src`，用 T1-size 的那支腳本。工作目錄的數字裡，包含別人還沒 commit 的改動，只是拿來估算，不代表最後的樣子。）

## 給使用者的選項

1. **做 E1～E7、E9、E10**（低風險，約 −60）。E1 和 E3 也同時修了 push 路徑少了 H1 的保護，以及 T3-sec3 R6。
2. **E8**：決定 push 是「阻塞地等鎖」（簡單，−6），還是照 design「等自己的 id、定時提醒」（+4）。
3. **保險**：建議全部保留；要拿掉 L7、N4、N5 的話，要推翻 T1 的 Q1。
4. **額度**：不論怎麼選，T3 做完都會在 3,700 行左右，要使用者決定新的目標（T4 歸檔時，PM 記的是「新的目標等使用者決定」）。

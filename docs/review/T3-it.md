**隔離和清理沒有問題**：只用 `agora-test`；`AGORA_CONFIG`／`CACHE_DIR`／`STATE_DIR` 都指到 pytest 的暫存目錄；清理只 purge 這次印出的 ULID，opencode 的 session 也是按自己建的 id 一個一個刪。`wait_uploaded()` 的判斷是對的，也有逾時。

**但是四個測試照現在的寫法，全部都會失敗（I1，High：5.2 還不能算完成）**：
- `import_sessions` 第一行就會丟 `ValueError`，所以四個測試都走不到要驗的地方；
- 第 3、4 個情境，切到「機器 2」之後沒有切回來；
- 第 4 個另外還有找新 Session 的方式錯了兩處、前後順序靠運氣。

# Review：`beb04b4`（T3 tasks 5.2 的整合測試）

2026-10-03，review。**只讀**：impl1 正在跑整合測試，所以我沒有跑任何整合測試，也沒有碰 Drive。下面兩個 `ValueError`，是用純 Python（不跑 agora 的程式）重現的；其他是讀程式（`beb04b4` 的 `store.py`、`cli.py`）推出來的。

## 安全：照 PM 的四個重點

| 重點 | 結果 |
|---|---|
| 只用 `agora-test` | ✅ `AGORA_FOLDER_NAME=agora-test`，`AGORA_RCLONE` 拿掉（用真的 rclone）。folder ID 記在暫存的 `config.json`（`AGORA_CONFIG` 指到 `tmp_path/config`），所以不會改到使用者的 `config.json`，也不會用到正式的 `agora/` |
| 不碰使用者的鏡像與狀態目錄 | ✅ 三個 `AGORA_*` 都指到 `tmp_path`。「機器 2」是 `tmp_path/other`。背景程序用 `os.environ` 啟動，所以也帶著這些變數 |
| `rclone.conf` | ✅ 用 symlink 指到真的那一個，和 `test_store_drive.py` 一樣（rclone 換新的 token 時會寫回真的檔案）。沒有印出它的內容 |
| `HOME` 換回真的 | ⚠️ 是為了讓 opencode adapter 讀得到測試自己建的 session，既有的 e2e 也是這樣做的。因為三個 `AGORA_*` 都有設，agora 本身不會用到 `~/.cache/agora`、`~/.local/state/agora`。測試建的 opencode session 會寫進使用者真的 opencode 資料庫（專案 `/tmp/agora-it-background/p_專案.v2`），最後按 id 一個一個刪掉 |
| 清理只刪自己印出的 id | ✅ purge 的是 `created["ulids"]`，路徑是 `gdrive:sessions/<ULID>`，加上 `--drive-root-folder-id <agora-test>`。⚠️ 有兩個漏網的情況，見 I3 |
| `wait_uploaded()` 的判斷正確、有逾時 | ✅ 鎖沒人拿、outbox 空、刪除佇列空，三個都成立才回傳。逾時 300 秒，訊息會說是哪一個還沒好。小地方見 I4 |

## I1（High，就 5.2 而言）：四個測試照現在的寫法，全部會失敗

| # | 位置 | 問題 | 影響 |
|---|---|---|---|
| a | `import_sessions`：`_, sid = make_session(...)` | `make_session` 回傳的是**一個字串**（`ses_…`），不是 tuple。純 Python 重現：`_, sid = "ses_3f2a…"` → `ValueError: too many values to unpack (expected 2)` | **四個都在 import 的第一步就失敗**。commit 訊息說只做了 `--collect-only`，所以沒有看到 |
| b | 情境 3、4：`theirs = other_machine(env, monkeypatch)` 之後 | `other_machine` 用 `monkeypatch.setenv` 把 `AGORA_CACHE_DIR`／`AGORA_STATE_DIR` 換成機器 2 的，**之後沒有換回來**。所以後面的 `search`、`push --not-exist-upload`（情境 3）、`search`（情境 4）都是以**機器 2** 的身分跑的 | 情境 3：機器 2 剛刪掉，已經把它從本機忘掉了，所以 `search --filter cloud=no` 找不到，`push` 也沒有本機的那一份 → 失敗。情境 4：機器 1 的上傳不會被這個 search 啟動 |
| c | 情境 3：「this machine finds out on its next sync」 | `search` 的同步有 5 分鐘的節流（`store.sync(paths, throttle=True)`）。機器 1 在幾十秒前 import 時剛同步過，所以這一次**不會**列 Drive，標記也不會變。而且節流的時候，`sync` 在 `kick_uploader` **之前**就 return 了 | 就算換回機器 1，`cloud=no` 也還是找不到。要先 `(paths.state / "last-sync").unlink(missing_ok=True)`。情境 4 想靠 search「再啟動一次上傳」，也一樣要先刪掉它 |
| d | 情境 4：edit 之後才讓機器 2 刪 | edit 會**馬上**啟動機器 1 真的背景程序（`AGORA_UPLOAD` 已經拿掉）。用自己的 client，一次 rclone 才 0.6～0.8 秒，所以背景很可能在機器 2 刪掉**之前**，就把修改傳上去了。那麼結果就是：沒有另存，`new_ids == []`。下一行的 `assert store.outbox_ulids(paths) == {ulid}` 也是在和背景賽跑 | 結果看運氣，多半是失敗。要讓它一定成立，機器 1 要先**拿住自己的 `upload.lock`**（`store.hold_upload_lock(paths)`），等 edit、機器 2 刪完之後才放開，再刪掉 `last-sync`，跑一個指令（或直接呼叫 `store.kick_uploader(paths)`） |
| e | 情境 4：`index.header(u).get("parents")` | parents 放在 **`agora.parents`**（`_rescue_deleted` 寫的是 `new_hdr["agora"]["parents"]`），不在最上層 | 一定是 `None`，就算另存成功了，`new_ids` 也是空的。可以直接用 `index.children(ulid)`：它已經做了「parents 指向這個 ULID、而且雲端有」的判斷 |
| f | `_ulids`：`for hdr, _snippet in index.search([])` | `Index.search` 回傳的是 `(ulid, header, snippet)` **三個**。純 Python 重現：一樣是 `ValueError: too many values to unpack` | 用了 (e) 的 `index.children` 之後，這個函式就不需要了 |

修好之後，建議先在 PM 看得到的地方跑一次，把結果寫進 5.2。

## I2（Medium）：四個情境，有幾個地方只看了「有沒有」，沒看「對不對」

| 情境 | spec 要的 | 測試現在看的 | 建議補上 |
|---|---|---|---|
| 1 import 多個 | 指令一結束本機就完整、不久之後 Drive 上有 | Drive 上有那個資料夾；本機有 `session.md` 和原始檔 ✅。`assert store.outbox_ulids(paths)`（「還沒傳上去」）是在和背景賽跑：第一個 import 的背景，可能在第二個 import 跑完之前就傳完了 | 拿掉「outbox 不是空的」這一句，或者先拿住鎖；比對 Drive 上 `session.md`、原始檔的 md5，和本機的是不是一樣（`list_sessions()` 有 md5） |
| 2 delete 多個 | 指令結束時清單就沒有了，不久之後 Drive 上也沒有 | 只看了 `ids[0]` 的 `header` 是 None；`queued_for_trash == 兩個` 是在和背景賽跑（背景可能一下子就 purge 完了） | 兩個都要看，也看 `search session` 沒有它們；佇列那一句改成「`⊆` 兩個」，或者先拿住鎖 |
| 3 救回 | 在這台寫的、**沒有先 pull**，被刪之後 `push --not-exist-upload` 救得回來 | Drive 上有那個資料夾、沒有標記 ✅（修好 I1 b、c 之後） | 再看 Drive 上有**標頭指到的那個原始檔**，md5 也對：這正是 P1（「本機沒有原始檔，不傳半套」）要確認的 |
| 4 另存 | X 沒有被傳回去；修改存成 Y（parents 指向 X）；提醒說明 | X 不在 Drive ✅、剛好一個 Y、Y 在 Drive 上 ✅（修好 I1 之後） | **Y 裡面是那次修改**：`index.header(Y)["title"] == f"{MARK} 改過"`。不然「修改沒有掉」這件事其實沒有驗到。也可以看 X 在機器 1 還在、被標成雲端沒有。提醒的部分（T3-sec5 V4）還沒做，先不驗 |

## I3（Low）：清理漏掉的兩種情況

1. **另存出來的 Y**：要等到 `len(new_ids) == 1` 之後才加進 `created["ulids"]`。在那之前就失敗的話（例如 I1 的 e，或者存成了兩個），Y 就會留在 `agora-test/` 上。建議清理時再找一次：在機器 1 的暫存索引裡，用 `index.children(ulid)` 找出 parents 指向這次建的 ULID 的那幾個。**不要**把暫存索引裡的全部都刪掉：`agora-test` 是共用的，同步下來的可能是別人（例如使用者的驗收）的 Session。
2. **背景還在跑的時候就清理**：測試在 `wait_uploaded` 之前就失敗的話，真的背景程序還在跑，可能在 purge **之後**才把東西傳上去，留在 `agora-test/`。建議清理一開始，先對兩台機器各做一次 `wait_uploaded(..., timeout=60)`（失敗也繼續），再 purge。

另外，`PROJ` 是寫死的路徑（`/tmp/agora-it-background/…`），而且一開始會 `rmtree`。同時跑兩份的話會互相刪掉。現在只有 impl1 在跑，先記著就好。

## I4（Low）：`wait_uploaded()` 的小地方

- 這個 helper 每 0.5 秒呼叫一次 `uploader_is_running`；`outbox_ulids` 裡面也會再呼叫一次。它是靠**真的去拿一下鎖**來判斷的。背景程序剛啟動、去拿鎖的那一瞬間，如果剛好撞上，背景會以為「已經有人在跑」，就直接結束（T3-sec3 R6），然後就沒有人上傳了，`wait_uploaded` 等到 300 秒逾時。機率很小，但這是產品程式的 R6（T3-size 的 E3 建議過修法），不是這個測試的錯；逾時的訊息會把它講出來。
- outbox 空了，不代表都傳上去了：被移進 `.bad` 的那一筆也不在 `outbox_ulids` 裡。建議再加一句 `assert store.bad_count(paths) == 0`。
- 背景上傳失敗之後（例如被限流），鎖空著、outbox 不是空的，沒有人會再啟動上傳，所以要等滿 300 秒，訊息才會說 outbox 還有東西。這樣是對的（訊息清楚）；如果想快一點知道，可以在等的時候順便看 `upload.log` 有沒有新的錯誤。

## tasks.md

`beb04b4` 把 5.2 打了勾（`[x]`），旁邊註明「已寫、未跑」。照 I1 的情況，建議先改回 `[ ]`，等照 I1 修好、真的跑過之後再打勾，不然歸檔檢查會把它當成做完了。

## 結論

隔離、清理、`wait_uploaded()` 的設計都是對的，不會碰正式的 `agora/`，也不會碰使用者的目錄。但是測試本身還沒有跑通過：
- (a) 讓四個都在第一步失敗；
- (b)～(f) 讓第 3、4 個情境就算走到了，也驗不到 spec 要的東西。

建議 impl1 依 I1 修好之後再跑，並且照 I2 補上「內容對不對」的斷言（尤其是情境 4 的「Y 裡面是那次修改」、情境 3 的「Drive 上有原始檔」）。

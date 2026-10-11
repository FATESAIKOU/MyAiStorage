# 複查報告：Angareion 修正（part1 `ffdcf57`、part2 `bbbd4c2`、token `db74b55`）

- **審查對象**：`ffdcf57`（part1 H1、M1、M2、M3、L1）、`bbbd4c2`（part2 H1、M1、M2、L1、L2）、`db74b55`（token 接受 0400／0600 owner-only）。（作者：agy1／impl3）
- **審查者**：Muse Spark
- **審查日期**：2026-10-11
- **對照規格**：
  - `docs/review/angareion-part1.md`、`docs/review/angareion-part2.md`（agy3）
  - `openspec/changes/angareion-a2a-v0/` 的 design／spec／tasks
- **方法**：worktree 只讀（impl1、tester 另有未 commit 改動，未碰）；每一項都在副本 `/tmp/angareion-rc`（`git archive HEAD`）改回修前形狀，只跑它對應的釘住測試；不連網（全用 `FakeBackend`／`FakeTransport`）、不讀真 token、不改程式。worktree 側 angareion 七個測試檔 baseline：73 passed。

---

## 結論

**11 項修正全部「改壞會紅」，可以結案；另有 4 個 Low（3 個修正帶來的殘留／小毛病＋1 個 spec 文件滯後），不擋合併。**

- 無 High、無 Medium。403 重試只重試一次（第二次失敗 exit 2，不會無限重試）；`fcntl.flock` 在 macOS／Linux 皆可用（本機即 darwin，全綠），且與 repo 既有用法（agora `store.py`、sidecar）一致。

---

## 每項改壞結果表

### `ffdcf57`（part1：H1、M1、M2、M3、L1）

| 項 | 改壞方式（副本） | 釘住測試 | 結果 |
|---|---|---|---|
| H1 用 `Link rel="last"` 定位最後一頁 | 拿掉 last-page 抓取，只查第 1 頁 | `test_post_message_duplicate_detection_locates_last_page_when_over_30_comments` | **紅** |
| M1 用 `parse_message` 比 front matter id | 回到 `f"id: {id}" in body` 子字串比對 | `test_post_message_body_mentioning_id_does_not_swallow_new_message` | **紅** |
| M2 後續分頁失敗拋 `BackendError` | 回到靜默 `break` | `test_list_messages_pagination_subsequent_page_failure_raises_backend_error` | **紅** |
| M3 `list_channels` 遇 4xx/5xx 拋錯 | 回到靜默 `break` | `test_list_channels_raises_backend_error_on_http_error` | **紅** |
| L1 `Backend` Protocol 補 `list_channels` | 從 Protocol 刪掉該簽章 | `test_backend_protocol_requires_list_channels` | **紅** |

### `bbbd4c2`（part2：H1、M1、M2、L1、L2）

| 項 | 改壞方式（副本） | 釘住測試 | 結果 |
|---|---|---|---|
| H1 快取空時 etag 設 None | 回到 `etag = state.etags.get("messages")` | `test_inbox_rebuilds_after_cache_file_deleted` | **紅**（重現 part2 的空信箱） |
| M1 快取名用頂層 `identity_slug` | 回到 `hasattr(state, 'identity_slug')` fallback | `test_inbox_cache_file_is_per_identity` | **紅** |
| M2 403 等 `retry_after` 重試一次（`<=10s` 門檻拿掉） | 加回 `if e.retry_after <= 10` 門檻 | `test_send_retries_backend_403_with_injected_sleep`、`test_send_403_retry_still_failing_exits_2` | **雙紅** |
| L1 重複 ack 斷言（碼＋stderr＋無多餘留言） | 刪掉 `cmd_ack` 的 `if target_id in state.acked: continue` | `test_ack_workflow` | **紅**（part2 報告的盲區已補上） |
| L2 `rate_limit.json` 加 `flock` | 拿掉 `LOCK_EX`／`LOCK_UN`（保留 try/finally 形狀） | `test_rate_limit_file_uses_flock` | **紅** |

### `db74b55`（token：接受 0400／0600 owner-only，0440 等拒絕）

| 項 | 改壞方式（副本） | 釘住測試 | 結果 |
|---|---|---|---|
| 規則 `mode & 0o077 or not mode & 0o400` | 回到嚴格 `mode != 0o600` | `test_load_token_accepts_readonly_0400` | **紅**（0400 被拒）；`test_load_token_accepts_0600`、`test_load_token_rejects_group_readable_0440` 照舊綠 |
| （附帶） | 同上 | `test_angareion_token.py` 全檔 | 全綠——舊訊息斷言已同步改成 `"owner-only"`，訊息文字本身也被釘住 |

---

## 新發現（Low 4 項；修正本身沒引入 High／Medium 問題）

#### L1. H1（part1）的 last-page 抓取失敗時靜默退回只查第 1 頁

- `src/angareion/github.py`：`resp_last.status != 200` 時直接略過（連 log 都沒有），只拿第 1 頁比對。若那一瞬最後一頁正好有要重試的留言，就會重複貼。
- 前半 H1 要修的本來就是「逾時重試」情境；last-page 那次 GET 自己遇到 500 的機率很低，且舊行為（只查第 1 頁）更糟，所以是改善不是退步。
- 建議：失敗時記一行 warning（哪一頁、什麼 status），讓維運看得到為什麼重複貼了。

#### L2. H1（part1）只查第 1 頁＋最後一頁：留言超過 60 則時中間頁的同 id 重送會漏

- 61+ 則（3 頁以上）時，第 2..n-1 頁從不抓取。同 id 重送只有「剛貼上、還在最後一頁」的逾時重試會命中；一個很久以前的 id（已被擠到中間頁）被手動重送時會再貼一次，嚴格說違反 spec「id 已在信道上就當成功」。
- 機率極低（要 60+ 則新留言＋舊 id 重送），且修前是 30 則就穿，修後是 60 則＋特定形狀才穿。
- 建議：不修沒關係，但把「只保證最後一頁」寫進註解，免得以後有人以為全查了。

#### L3. M2（part2）：`RateLimiter.acquire` 抱著 `LOCK_EX` 睡覺＋空信箱永遠全量抓

- `src/angareion/cli.py` 的 `acquire` 在 `self.sleeper(wait_needed)`（預設上限 10s）時還拿著排他鎖：併發的第二個 `send` 會在 `flock` 上空等（不會死鎖，kernel 會放，但會卡住）。把 sleep 搬出鎖即可， nhưng非必要。
- 同一檔案的 `cmd_inbox`：空信道會把 `{}` 寫進 cache（探針證實內容就是 `'{}'`），`{}` 是 falsy → 下次 etag／since 照樣是 None → 永遠全量 200、不會 304。正確性沒問題（空的，重建也是空的），只是空信道每次都全抓。探針：`test_rc_probe.py::test_probe_empty_inbox_never_uses_304`（副本，全綠＝現象成立）。
- 建議：兩個都是「有空再順手」等級；sleep 出鎖一行，空 `{}` 可考慮不落檔（或視為有效快取）。

#### L4. `db74b55` 之後 spec 文件滯後：openspec 還寫 0600

- 程式現行：`mode & 0o077 == 0` 且 owner 可讀（0600、0400 過，另有 0500 這種 owner-only 也會過，符合規則本意；0440／0640／0644／0444 拒絕）。
- `openspec/changes/angareion-a2a-v0/specs/angareion/spec.md:113`「（檔案權限 0600）」、tasks 1.6、design 的 0600 字樣還沒跟上；`docs/angareion.md:112-113` tester 已經改好（未 commit）。part1 L2 當初說「保持 0600 實作」，現在是 PM／impl3 決定放寬（唯讀掛載 `/run/mlp-secrets` 的 0400），要有人把決定寫回 openspec（spec 一句＋design 註記）。
- 我沒動 openspec（不在本次任務範圍），列在給 PM 的清單。

---

## 確認沒問題的部分（本次任務點名的兩題）

- **403 重試不會無限重試**：`cmd_send` 的重試包在 `except RateLimitError` 裡只做一次（`cli.py` step 8）；第二次 `post_message` 再丟（含再次 403——`RateLimitError` 是 `BackendError` 子類）都進 `except BackendError → return 2`。`test_send_403_retry_still_failing_exits_2` 釘住（post 恰 2 次、exit 2、信道空無一物）。重試成功則正常落到 step 9 印 `<id> <channel>`、exit 0。
- **`flock` 跨平台**：`fcntl.flock` 是 POSIX 語意，macOS／Linux 皆可；本次就在 darwin 全綠。`a+` 開檔＋`LOCK_EX`＋讀改寫＋`truncate`＋`write`＋`finally` 解鎖形狀正確；crash 由 kernel 放鎖。Windows 本來就不在支援範圍（repo 內 agora、sidecar 早已頂層 `import fcntl`）。

---

## 給 PM 的清單

1. **結案建議**：11 項修正的釘住測試全部「改壞會紅」，無新 High／Medium，可以結案（合併與否照 #30 原定流程）。
2. **請轉交（擇期，不擋）**：L1 加 warning log；L2 補註解；L3 sleep 移出鎖、空 `{}` cache 可不落檔；三個都是一眼能改的大小。
3. **請決定文件歸屬**：L4 的 openspec 0600→0400/0600（spec.md:113、tasks 1.6、design 相關字樣）由誰改——tester 正在改 `docs/angareion.md`（未 commit），openspec 那側建議一併交代（impl3 或 tester），並把「PM 接受 0400（唯讀掛載）」的決定留一句記錄。
4. 副本與探針留置 `/tmp/angareion-rc`（含 `tests/unit/test_rc_probe.py`）；worktree 只新增本報告檔，無其他改動。

# 架構決策清單（agora）

維護者：專案架構師。每條寫決定、理由、誰決定、出處。引用程式時寫**符號名**，不寫行號（行號會漂）。和引起它的設計文件放在同一個 PR 更新。

## 已決

| # | 決定 | 理由 | 誰決定 | 出處 |
|---|---|---|---|---|
| D-1 | `upload_batch` 只上傳，不寫索引；索引只由寫入者（`store.remember`）和 `store.sync` 寫 | 上傳和索引的責任分開，避免兩條寫索引的路徑互相競爭 | 架構師（PM 同意） | #316（REPORT-arch-316b） |
| D-2 | `store.sync` 是 Drive／outbox → 索引的唯一入口；不為了測試加模式 | 同上；測試模式會讓正式路徑分岔 | 架構師 | #316（316d） |
| D-3 | `store.sync` 有只限關鍵字的 `strict=False`，**只在列表失敗時** raise；`sessions/` 不存在不算失敗；sync 做的事不因 strict 而變 | 最後一次寫回需要分辨「確定不在」和「不知道」，而永遠卡住比偶爾多一份更糟 | 架構師提介面、PM 定行為 | #316（316d、316g） |
| D-4 | 最後一次寫回在另存前先 `store.sync(strict=True)` 再解析一次；判斷「已被刪」用 sync **之前**讀到的本機標記 | strict sync 失敗前就會寫 `cloud_missing`，sync 之後讀到的標記不可信 | 架構師 | #316（316b、316f） |
| D-5 | 讀索引的公開方法要擋得住來自 Drive 的格式錯（例如 `Index.children` 略過不是 dict 的 parent） | 索引內容來自外部檔案 | 架構師 | #316（316d） |
| D-6 | 寫回目標的解析器（`cli._write_target`）不從 outbox 讀 header 判斷目標是否活著；定期寫回遇到本機沒有索引的目標，這一輪就跳過 | 「活著」只由索引和 sync 回答，不在解析器裡另開第二個來源；定期寫回不連 Drive（P7） | 架構師提、PM 同意 | #326（REPORT-arch-326b） |
| D-7 | config.json 的寫入一律在 `config.json.lock`（放在 config 目錄）之內：進鎖、重讀、合併、`write_atomic`；`upload.lock` 只給上傳用，不拿來保護別的資源 | 同機並行時，第一次建 Drive 資料夾才不會建出同名資料夾、也不會互蓋；上傳器拿著 upload.lock 時會呼叫 `folder_id`，共用同一把鎖會自己卡死 | 架構師 | #330（design-notes/330-folder-id-race.md） |
| D-8 | claude raw 的兩個 writer（`_pack_raw`、`_stream_raw`）不合併，由互比測試鎖住位元組相同；格式頭由 `FORMAT` 組出，測試另外保留一份字面值當 oracle | 串流 writer 要產 offset 和 digest，合併會把它拖回整份放在記憶體 | 架構師 | #338（121-A） |
| D-9 | 子行程的逾時或取消收尾只有一份（`_reap_group`），取消路徑的 `catch=BaseException` 不能收窄 | 收尾要有界，而且取消時原本的例外要優先 | 架構師 | #339（121-B） |
| D-10 | continue 的子行程協議外殼放在 `continue_shell`，它不 import cli、round_child、start_child；狀態編解碼（`state_of`、`decode_fingerprint`）也放在這裡，round_child 重新匯出 | 外殼和子行程共同依賴協議層，而不是彼此依賴 | 架構師（Sonnet 審過 design note 與實作：REPORT-341-design-rev、rev-m1-11） | #341（design-notes/121-continue-shell.md） |
| D-11 | `InputError` 放在 `agora/errors.py`，cli 重新匯出 | 它是全 CLI 共用的概念，低層模組要用時不必 import cli | 架構師（Sonnet 審過） | #341 |
| D-12 | 模組搬家時，被測試當接縫替換的名字，用「cli 重新匯出，cli 內部用裸名呼叫」保住；**不留舊名 alias**（替換 alias 不會影響實際呼叫） | 不讓測試悄悄地沒有驗到東西 | 架構師（Sonnet 審過） | #341 |
| D-13 | raw 載體的「有檔用檔、否則 bytes」只由 `Exported.raw_digest()`／`raw_source()` 回答，turns 的「有就用、否則解析」只由 `turns_of` 回答；呼叫方不再自己寫判斷梯子 | 收掉 cli 的四處重複梯子（121-C） | 架構師（PM 同意） | #340 |
| D-14 | claude raw 的封包格式（常數、writer、reader、aux 串流編碼、`PackedRaw`）放在 `agents/claude_raw`，它只依賴 `agents.base`；一趟式匯出 `_stream_raw` 和 `_Facts`／`_accumulate` 留在 claude.py，當作「解析加格式」的組合者 | `_stream_raw` 的折疊會用到整套閱讀版解析，搬過去會讓格式模組帶著解析；改成注入又不是純搬移 | 架構師（Sonnet 審過 design note；實作 #342） | #342（design-notes/121-claude-raw.md） |
| D-15 | PMO skill ＝ `pmo` 指令（套件 `src/pmo`，本 distribution）＋`skills/pmo/SKILL.md`；PMO harness 只寫能力，環境說明對應到指令 | 沿用 angareion／aicomm 的「指令＋SKILL.md」先例；atelier 指令維持只讀 harness | 本人（經 PMO；#110 留言、MyPMO 決定檔 PR #270），2026-11-03（Q-1 (a)、Q-5 (a)） | #110（design-notes/110-pmo-skill.md） note D-1 |
| D-16 | `pmo` 的核心是純函式，只依賴 `ainame`；外部只經四個埠（Angareion、aicomm、sidecar、GitHub），不 import agora／atelier／aicomm／angareion 的模組，也不依賴 MyPMO 的程式；印出去的字串過 `sanitize` | 測試用同名方法的假物件取代埠；public repo 不洩漏內文與憑證 | 架構師（Sonnet 審過）；本人 2026-11-03 隨整份設計定案 | #110（design-notes/110-pmo-skill.md） note D-2、D-3、D-12 |
| D-17 | 設定與狀態的格式（teams 設定、team 狀態、收件帳本、`tick.json`）公開在本 repo，資料由 `PMO_SKILL_CONFIG`／`PMO_SKILL_STATE_DIR` 指到本機，沒設就拒絕；收件帳本以 Angareion 為準、本機是快取、可 `rebuild`（`team` 與急件叫醒紀錄不可重建） | public repo 不放私人資料；帳本不再是去重的單點 | 本人（經 PMO；#110 留言、MyPMO 決定檔 PR #270），2026-11-03（Q-2 (a)） | #110（design-notes/110-pmo-skill.md） note D-4、D-5 |
| D-18 | `pmo tick` 是一輪、也是 PMO 身分唯一的讀信者；讀到的每一則都要有去處（自己處理、`[msg]` 交給 PMO、或交接給過渡工具）；指令只做機械步驟，建工單、派工、重開、換 provider 由 PMO 判斷 | 同一個身分只留一個讀者；職務分界 | 本人（經 PMO；#110 留言、MyPMO 決定檔 PR #270），2026-11-03（Q-9 (a)） | #110（design-notes/110-pmo-skill.md） note D-6、D-7、D-13 |
| D-19 | 叫醒＝寄件人立刻 poke＋PMO 用 `pmo wake set\|off\|show` 自己排的 sidecar 定時 poke job（頻率由 PMO 定）；到期判斷全在 tick 裡，不設外部監看程式 | 叫醒來源只需要「叫」，到期狀態只在唯一的讀者裡 | 本人（經 PMO；#110 留言、MyPMO 決定檔 PR #270），2026-11-02（Q-8 (a)） | #110（design-notes/110-pmo-skill.md） note D-14、§3.6 |
| D-20 | 第一階段只搬本機 sidecar 的派工；群請求、worker team 的動作、挑模型、MyPMO 的工單維護工具留在 MyPMO，和 tick 用 id 交接；切換後 MyPMO 的 dispatch.py 只留 worker 動作和群請求 | 群請求和 worker 傳輸搬移風險高、和其他 repo 綁在一起；讀者仍只有一個。之後的搬移與退役時機是架構師的後續提議，本人沒有決定 | 本人（經 PMO；#110 留言、MyPMO 決定檔 PR #270），2026-11-03（Q-3 (a)、Q-4 不搬、Q-7 (a)、Q-9 (a)） | #110（design-notes/110-pmo-skill.md） note D-8～D-11、D-15 |
| D-21 | sync 的大量下載（`need` ≥ `BULK_DOWNLOAD_MIN`）用一次列表式 `copy --include */session.md --fast-list --checksum` 下載到 mirror **外**的持久影子 `<cache>/download-staging`（`download-staging.lock` 排隊），只把 `changed` 裡、md5 等於這次列表的檔**複製**進 mirror（`os.replace`），影子保留供續傳；失敗時同樣先複製驗過的，其餘逐一 `copyto`；影子的清理只在列表完整時、非阻塞拿到鎖時做。門檻與並行度是模組常數，不開環境變數 | 逐檔解析路徑在冷快取時是 O(session 數 × 往返)（Mac 量到每份 0.64 秒），而 sidecar 的逾時會把一次做不完的下載整個丟掉；影子在 mirror 外，所以 G3／#119／垃圾桶佇列的不變式不受影響；正確性仍由 sync 之後的逐一 md5 核對兜底 | 架構師（核准者模式；作者 ollama-cloud/deepseek-v4.1-flash） | #364（design-notes/364-bulk-download.md §10） |

#110 的 note 有自己的 D- 編號，上表 D-15～D-20 的出處欄寫成「note D-x」。

**流程上的已決**（本人／PMO，10-31～11-01）：每個專案一位常駐的架構師；核准者模式和作者模式並存，預設是核准者模式；衝突依問題種類裁決（形狀和一致性歸架構師，語意、需求、行為、品質歸 PM）；架構師可以 commit 設計路徑，由 PM merge（AGENTS.md）。

## 待決

| # | 問題 | 誰決定 | 出處 |
|---|---|---|---|
| P-1 | 跨 repo 的介面由哪一邊的架構師提供、哪一邊對齊 | PMO（arch v2 時補） | REPORT-arch-321 L2 |
| P-2 | 開代理架構師的門檻 | PMO | D24 |
| P-3 | `Exported.raw` 沒有任何載體時要回 `b""` 還是 raise | PM | #121 報告第 5 節 |
| P-4 | 要不要刪 `Exported.raw_size`（src 沒有讀者） | PM | #121 報告第 5 節 |
| P-5 | fork 路徑讀整份 raw（`cli._load_for_launch` 的 fork 分支）要不要改用 `raw_file` | PM | PSTACK G 節；#121 報告 |
| P-6 | start_child 錯誤回答的形狀，在寫的一邊和讀的一邊（`continue_shell._raise_child_error`）之間沒有共同常數 | 架構師（#341 之後） | design-notes/121-continue-shell.md §7 |
| P-7 | `claude.py` 在 #342 之後仍有 1222 行：「Claude session 解析」（turns／標題／noise／`_Facts`）和「adapter 本體」（找檔、啟動、還原）要不要再切；`opencode.py` 1648 行的切割 | 架構師設計、PM 排程 | #121 報告；design-notes/121-claude-raw.md §7 |
| P-8 | #341 留下的相容層：cli 重新匯出 `RoundChildDead`、`StartChildDead`、`_reap_group`、`_round_timeout`、`_start_payload`、`CONTINUE_LOG`，round_child 重新匯出 `state_of`。日後把測試改讀 continue_shell，就可以刪掉；刪 `CONTINUE_LOG` 的重新匯出時，cli 自用的三處（`_LogWriter` 等）要一起改成 `continue_shell.CONTINUE_LOG`，或改由別處持有 | 架構師提、PM 排程 | REPORT-341-design-rev 發現 7 |
| P-9 | 寫進 header 的 `source.host` 直接取 `socket.gethostname()`，沒有可覆寫的來源；golden 測試只能用 monkeypatch 加子行程的 sitecustomize 釘住。要不要給它一個可覆寫的來源（例如環境變數） | 架構師提、PM 排程（不急） | #334 審查的低項 |
| P-10 | #342 留下的相容層：claude.py 重新匯出 `FORMAT`，claude.py 自己不用，只為了 6 處測試讀 `C.FORMAT`。日後把測試改讀 `claude_raw.FORMAT`，就可以刪掉 | 架構師提、PM 排程 | REPORT-342-design-rev 低 2 |

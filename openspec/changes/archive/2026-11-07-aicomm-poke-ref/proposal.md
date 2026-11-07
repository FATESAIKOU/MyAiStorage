## Why

期 6 的 M1（通訊看得見）要讓每一則派工、交件都查得到「有沒有送達」。巡檢現在手上只有訊息 id，卻沒有辦法用它找到 sidecar 那一筆叫醒紀錄：#406 讓 `wake:` 行附上 `record=…`，但那只有送信的當下看得到；事後巡檢要從訊息 id 出發查。

MyLinuxPool 的 sidecar 已定稿並上線（MyLinuxPool#786 poke-records-v1 的 D3，PR #796）：poke 的 body 除了 `text`、`interrupt`，還可以帶選用的 `via`（誰戳的）與 `ref`（對應的訊息 id），寫進叫醒紀錄。aicomm 這一側要開始帶（MyAiStorage#413）。

## What Changes

- **每一次戳的 body 一律帶 `via`**：本機 `"aicomm"`、跨機 `"aicomm-peer"`。
- **訊息 id 合格時另外帶 `ref`**：id 取 Angareion `send` 的 stdout 第一行、去掉頭尾空白，且完全符合 `^[A-Za-z0-9_-]{1,64}$`（fullmatch）才帶；**不合格就不帶 `ref` 這個鍵，照樣戳一次**，不加 stderr 行、不加原因碼（PM 決定 Q-2）。
- **同一次 `send` 的所有戳帶同一個 `ref`**（多收件人、`--mention`、同一個收件人在好幾台）。
- **本機與跨機共用一個純函式組 body**（`_poke_body`），只差 `via` 的值。
- **戳的 `text` 與 `interrupt` 的規則一個字都不變**；`wake:` 行的文法、原因碼清單、結束碼、`--no-wake`、戳的次數規則都不變。沒有新的 wake 種類、旗標或子指令。
- **文件**：`docs/aicomm.md` 的 body 欄與說明、`src/aicomm/cli.py` 的註解改寫；`skills/aicomm/SKILL.md` 不需要改（使用者看得到的行為沒有變）。
- **spec delta**（`specs/comm-skill/spec.md`）：MODIFIED 四條——「戳的內容固定且不含訊息」、「不改 Angareion 與 sidecar，不攜帶私人資訊」、「跨機叫醒只經 `pool-peer` 的兩個用法」、「`find` 找到的每一台怎麼戳」。
- **決策清單**：`docs/architecture/decisions.md` 新增 D-26，並在 D-25 註明「由 D-26 擴充」；**本設計 PR 已含**（AGENTS.md：決策清單的改動和引起它的設計文件放同一個 PR）。

### 撤回既有原則的一部分（主 spec「戳的內容固定且不含訊息」）

主 spec 原本的意圖是：收件人的輸入框不會看到訊息內容或寄件者，sidecar 的紀錄也不留私人資訊；字面寫成「戳的內容固定且不含訊息」，body 只有 `text` 與 `interrupt`。本 change 把字面改成：

> 戳的**文字**固定且不含訊息；body 可多帶選用的 `via`／`ref`，`ref` 只是 sidecar 紀錄用的中繼資料，不貼進對方的輸入。

意圖不變：`text` 仍是固定短句，對方輸入框看到的字一個都不變。（推論：MyLinuxPool `modules/sidecar/docs/sidecar.md` §2.2 只說 `via`／`ref` 是對照紀錄用，沒有寫「會送進 tui」。）訊息 id（ULID）本身不是私人資訊：它已印在寄件者的 stdout，Angareion 的紀錄本來就公開。

### 與 MyLinuxPool 的關係

- sidecar 端已定稿並上線（MyLinuxPool PR #796；`modules/sidecar/docs/sidecar.md` §2.2；`openspec/changes/sidecar-poke-records/design.md` D3）。不需再向 MLP 確認欄位名或格式。
- 舊版 sidecar（從 8b1acdd 到 c2b945f）的 poke 路由只讀 `text`／`interrupt`，不認得的鍵會忽略，所以不需要能力標記，也不需要重試。
- 開放問題 Q-MLP-1（不擋本單）：`GET /pokes` 的列表參數沒有 `ref`，巡檢現在只能先用 `agent`＋時間區間列出，再在本地用紀錄的 `ref` 欄篩選。已用 `--no-wake` 問 MLP PM。

### 風險

- **`via` 是自己宣告的**：sidecar 分不出真假。巡檢只能把 `via` 當線索，不能當證據，也不能拿來做授權（docs 要寫這一句）。
- **`ref` 格式不對，sidecar 會回 400 `invalid_request` 且不送出**：所以 aicomm 一定要先驗（D-2），否則「angareion 的輸出格式變了」這種與叫醒無關的事，會讓每一次叫醒都靜靜變成 `not-woken http-400`。
- ref 不合格時不出聲是有意的（Q-2，PM 已接受）：實際上只有 angareion 改了 id 格式才會發生，這時 sidecar 紀錄的 `ref: null` 就看得出來。

### 決定記錄

- **Q-1（PM）**：設計文件用 openspec change（本 change），不用 design-notes。理由：要改 4 條主 spec，只寫 design-notes 會讓主 spec 與行為不一致。
- **Q-2（PM）**：ref 不合格時不出聲（不加 stderr、不加原因碼），接受。

## Non-goals

- 不改 `wake:` 行、原因碼、結束碼，不加旗標、子指令。
- sidecar 端的實作（紀錄、`GET /pokes`、是否加 `ref=` 篩選參數）歸 MyLinuxPool。
- 巡檢如何使用 `ref` 不在本 change。
- 真機驗收不是 PR，由 PM／PMO 在 M1 收束時排。

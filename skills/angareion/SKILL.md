---
name: angareion
description: Leave and read messages on an Angareion channel, between agents, programs or people. Use when you need to send a message, read messages addressed to you, wait for new ones, or create/delete a channel. When idle, block in `angareion wait --timeout <seconds>` instead of polling the model.
---

# Angareion

Angareion 是留言工具。你用它「送訊息給某個 agent 或整個 channel」與「讀寄給自己的訊息」；channel 是英文名，訊息由設定檔指到後端的實體（實作是 GitHub Issues 的留言）。對外只有五個指令；沒有 ack、沒有已讀回條。

## 私人資訊：不要貼進訊息

channel 的訊息存在 GitHub（channel 對應的 repo）上，AI 之間的對話無法完全避免被看見。**不要在訊息裡放私人資訊**；需要傳遞私人資訊時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。

## 什麼時候用

- 有人要你留話給別的 agent、程式或人；
- 你要知道「有沒有寄給我的新訊息」；
- 你手上沒事，想等訊息；
- 要開一個新的 channel（或把不用的關掉）。

## 五個指令

```bash
angareion channel create <名稱> [--backend <名>]   # 建 channel；stdout 只印名稱
angareion channel delete <名稱>                    # 刪 channel（後端關掉、留紀錄）；stdout 只印名稱
angareion send --channel <ch> [--to <agent 名>] --body '訊息文字'   # 送；stdout 印訊息 id
angareion wait [--channel <ch>] [--timeout <秒>]   # 確認有沒有新訊息
angareion read [--channel <ch>] [--json]           # 讀新訊息，由舊到新
```

- channel 名只能是非空的 ASCII 字母、數字、`-`、`_`（例如 `general`、`phase-1`）。
- **位址（`--to`）是 agent 名**：`<repo 名>-<pm|arch|impl|test>[-<id>]`，例如 `LLMGateway-pm`、`MyLinuxPool-impl-2`。角色字只有小寫；`repo=...`、`owner/team×pm`、`group=...` 這些舊寫法都是錯的（exit 1）。不確定對方位址時先問，不要猜。
- **不給 `--to`＝廣播**：發到整個 channel，所有人都會在「寄給我的」裡讀到它。
- 多行內文用 `--body -` 從 stdin 餵；`--body` 一定要給（沒給會 exit 1，不會自動讀管線）。
- 不給 `--channel` 時，`wait`／`read` 的目標是「所有 channel 裡寄給我的訊息」——`to` 是自己 agent 名**或沒有 `to`（廣播）**的訊息。**收訊一律不加 `--channel`**：加了會讀到寄給別人的訊息（`read`／`wait` 會在 stderr 印一行警告）；只有送訊（`send --channel`）才指定 channel。
- `read` 會把你的本機游標往前推，同一則不會讀第二次；`read --json` 給程式解析用。程式要自己記進度時，`--since` 收的是後端時刻（每則輸出裡的 `received_at`），含邊界、同一秒可能重複，用訊息 `id` 去重。
- `send` 附加檔案：`--attach NAME=PATH`（小檔內容直接進訊息）或 `--ref NAME=LOCATOR`（連結）。

結束碼：0 成功（`wait` 是「有新訊息」）；1 用法或輸入錯；2 後端、網路或設定錯；3 `wait` 逾時沒有新訊息。錯誤看 stderr；stdout 只有結果。`wait` 若 exit 2（後端或網路錯）稍等一下再試，不要當成「沒有新訊息」。

## 建與刪 channel：有落地時間差

`channel create <名>` 會：在後端開實體 → 在註冊簿留一則註冊留言 → 等對應檔更新（上限 2 分鐘）。它成功（exit 0）只代表**留言已經送出**；對應可能還沒更新到大家看得到的設定檔。所以：

- 剛建好 channel 的前兩分鐘，別的 agent／別的機器可能還看不到它（`send` 會 exit 1「channel 不存在」是正常的，等一下再試）。
- 逾時（stderr 有一行提示）不是失敗：對應會在重建 workflow 跑完後出現，不用重試 `create`。
- 同名的 channel 已經存在 → exit 1；要換名字請先 `channel delete` 再建。
- 多個 backend 的環境要加 `--backend <名>`（沒給或名字不存在 → exit 1）。
- `channel delete <名>`：後端實體**關掉（不刪，留紀錄）**＋留註銷留言；不等待、重跑安全。

## 閒著的時候：阻塞地等，不要一直問模型

沒事做的時候，跑：

```bash
angareion wait --timeout 600
```

它會在那裡阻塞、每 15 秒向後端問一次，直到有新訊息（exit 0）或逾時（exit 3）。這不耗模型額度——你不用反覆醒來查信；把 `--timeout` 設在你工具（terminal）的單次執行上限以內，逾時就再下一次。收到 exit 0 之後跑 `angareion read` 拿訊息。

`wait` 會記住上次查到哪（本機的 ETag 狀態檔）：重新開始 `wait` 時對每個 channel 只發條件式查詢，閒置重啟幾乎不吃 GitHub 額度——所以逾時後直接再下一次就好。新的 channel、游標前進、或上次真的叫到訊息之後，才會有一次完整查詢。

- `--timeout 0`：只查一次就回，適合「順便看看」。
- 不給 `--timeout`：一直等，適合確定接下來只需要收信的時候。

`wait` 不會動你的游標：它 exit 0 只代表「有新訊息」，訊息還在，`read` 讀得到。新舊是看後端的到達順序，不是寄件者填的時間——別人的機器時鐘不準或時區不同都不會漏。**廣播也會叫醒你。**

## 收到訊息之後

- 每次 `wait` exit 0 或有事回報前，先 `angareion read`（不加 `--channel` 就是收自己的）。
- 看 `urgency`（0～9）：**7 以上先處理**——手上做到一半也先讀，需要人知道就轉告或回報；5～6 一般處理；4 以下有空再看。
- 被某件事卡住、需要對方動作時，用 `send` 回一則，把卡在哪、需要什麼寫清楚；不要只在心裡等。
- 回覆同一件事時，`--channel` 用對方那則訊息的 channel，`--to` 用對方的 agent 名；訊息標頭的 `<agent 名>` 就是對方的位址，可以直接貼給 `--to`。要回給整個 channel 就不給 `--to`。

## 不要做的事

- 不要把 token 或任何秘密寫進訊息、log 或回報裡（Angareion 的 token 由環境變數指到檔案，直接讀就好，不要印出來）。
- 不要在等待迴圈裡反覆問模型「有沒有新訊息」——那是 `wait` 的工作。
- 不要用 `read --since` 去重複讀已處理過的訊息；本機游標會自己記。
- 不要手改 `config/angareion/channels.json`（那是重建 workflow 的產物）。

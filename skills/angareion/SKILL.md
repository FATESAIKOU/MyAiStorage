---
name: angareion
description: Leave and read messages on an Angareion channel, between agents, programs or people. Use when you need to send a message, read messages addressed to you, or wait for new ones. When idle, block in `angareion wait --timeout <seconds>` instead of polling the model.
---

# Angareion

Angareion 是留言工具。你用它「送訊息給某個位址」與「讀寄給自己的訊息」，訊息放在一個 channel repo 的 Issues 上。對外只有四個指令；沒有 ack、沒有已讀回條。

## 什麼時候用

- 有人要你留話給別的 agent、程式或人；
- 你要知道「有沒有寄給我的新訊息」；
- 你手上沒事，想等訊息。

## 四個指令

```bash
angareion channel create <名稱> [--member <位址>]…   # 建 channel；stdout 印「<名稱> #<編號>」
angareion send --channel <ch> --to <位址> --body '訊息文字'   # 送；stdout 印訊息 id
angareion wait [--channel <ch>] [--timeout <秒>]     # 確認有沒有新訊息
angareion read [--channel <ch>] [--json]             # 讀新訊息，由舊到新
```

- 多行內文用 `--body -` 從 stdin 餵；`--body` 一定要給（沒給會 exit 1，不會自動讀管線）。
- 位址寫成 `repo=owner/team,role=pm`（要顯示名字再加 `,name=小明`）。不確定對方位址時先問，不要猜。
- 不給 `--channel` 時，`wait`／`read` 的目標是「所有 channel 裡寄給我的訊息」。
- `read` 會把你的本機游標往前推，同一則不會讀第二次；`read --json` 給程式解析用。
- `send` 附加檔案：`--attach NAME=PATH`（小檔內容直接進訊息）或 `--ref NAME=LOCATOR`（連結）。

結束碼：0 成功（`wait` 是「有新訊息」）；1 用法或輸入錯；2 後端、網路或設定錯；3 `wait` 逾時沒有新訊息。錯誤看 stderr；stdout 只有結果。

## 閒著的時候：阻塞地等，不要一直問模型

沒事做的時候，跑：

```bash
angareion wait --timeout 600
```

它會在那裡阻塞、每 15 秒向後端問一次，直到有新訊息（exit 0）或逾時（exit 3）。這不耗模型額度——你不用反覆醒來查信；把 `--timeout` 設在你工具（terminal）的單次執行上限以內，逾時就再下一次。收到 exit 0 之後跑 `angareion read` 拿訊息。

- `--timeout 0`：只查一次就回，適合「順便看看」。
- 不給 `--timeout`：一直等，適合確定接下來只需要收信的時候。

`wait` 不會動你的游標：它 exit 0 只代表「有新訊息」，訊息還在，`read` 讀得到。

## 收到訊息之後

- 每次 `wait` exit 0 或有事回報前，先 `angareion read`（不加 `--channel` 就是收自己的）。
- 看 `urgency`（0～9）：**7 以上先處理**——手上做到一半也先讀，需要人知道就轉告或回報；5～6 一般處理；4 以下有空再看。
- 被某件事卡住、需要對方動作時，用 `send` 回一則，把卡在哪、需要什麼寫清楚；不要只在心裡等。
- 回覆同一件事時，channel 用對方那則訊息的 channel，`--to` 用對方的位址。

## 不要做的事

- 不要把 token 或任何秘密寫進訊息、log 或回報裡（Angareion 的 token 由環境變數指到檔案，直接讀就好，不要印出來）。
- 不要在等待迴圈裡反覆問模型「有沒有新訊息」——那是 `wait` 的工作。
- 不要用 `read --since` 去重複讀已處理過的訊息；本機游標會自己記。

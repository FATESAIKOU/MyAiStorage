---
name: aicomm
description: Talk to another AI (PMO, PM, teammates) - send a message and wake the recipient, or read what was sent to you. Use `aicomm send` to message another AI and `aicomm inbox` to read your own mail. When idle, block in `aicomm inbox --wait <seconds>` instead of polling the model.
---

# aicomm

aicomm 是 AI 之間的溝通指令：**送一次、收一次**。它在 Angareion（留言）上再加一步——送出後，如果收件人在這台機器上、正好閒著或在跑，就戳它一下，叫它來讀信。對外只有兩個指令；所有角色（PMO、PM、隊員）用同一份說明。

## 私人資訊：不要貼進訊息

訊息存在 GitHub 上，無法完全避免被看見。**不要在訊息裡放私人資訊**；需要傳遞時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。

## 兩個指令

```bash
aicomm send --channel <ch> [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>] [--urgency 0-9] \
            [--ticket <repo>#<n>] [--no-wake | --interrupt] --body '訊息文字'   # stdout 第一行是訊息 id
aicomm inbox [--wait <秒>] [--mentioned] [--json]                               # 讀寄給我的信
```

- `--channel` 一定要給（channel 名不能從 AI 名推出來；派工信會寫）。`--body` 一定要給，`-` 從 stdin 讀（多行內文）。
- `--to` 是收件人的 AI 名（`<專案>-<角色>-<team>[-<序號>]`）；不給＝廣播，**廣播不叫醒任何人**。`--mention` 可重複，被點名的人和收件人一樣會被叫醒。
- `--ticket MyAiStorage#212` 只是附上一個 `ticket` 參照，不改內文、不去讀工單；不帶也能送。
- `inbox` 固定讀「寄給我的」，沒有 `--channel`。`--mentioned` 只看寄給我或點名我的（廣播不算）；`--json` 原樣印 Angareion 的輸出。
- 閒著沒事時用 `aicomm inbox --wait 600`：阻塞等，不花模型額度；不要在迴圈裡反覆問模型「有沒有新訊息」。

## 同一個身分只留一個讀者

`angareion read` 讀走信就推進游標；兩個讀者讀同一個身分，先讀的那個拿走信，另一個看到空的。`aicomm inbox --wait` 在 wait 之後會接著 read，放到背景一樣會把信讀走，所以**不能**當成「背景只等、不讀」。

- 被 sidecar 管的 agent（會被 poke 叫醒）：**不要跑背景讀信迴圈**（`wait → read`，或背景的 `aicomm inbox --wait`）；被叫醒就在**前景**跑 `aicomm inbox`。
- 不在 sidecar 底下、沒有人會 poke 的：才用 `aicomm inbox --wait` 當**唯一**的讀者。
- 補救：被叫醒但 inbox 是空的 → 先看自己有沒有背景讀信任務，它的輸出裡可能已經有那一則；要補讀用 `angareion read --channel <ch> --since <時刻>`（不移動游標）。
- `aicomm inbox` 讀不到信時會在 stderr 提示同一段補救；exit 0 與 stdout 不變。

## 送達與叫醒

- **送達＝訊息寫進 Angareion**。寫入成功，`send` 就是 exit 0，不管叫醒成不成功；叫醒失敗只在 stderr 印一行（「已送達，未叫醒…」）。看到 exit 0 就不要重送。
- 叫醒是盡力而為：只有收件人在**這台機器的 sidecar** 上、而且狀態是 `idle` 或 `running` 才戳；不在本機、`starting`、`waiting-quota`、`exited` 都不戳（訊息還是在 Angareion，對方之後 `inbox` 一定讀得到）。
- 戳的內容固定（請對方執行 `aicomm inbox`），不含你的訊息。對方在 `running` 時，戳會排到它這一輪之後，不打斷。
- 連送兩則會戳兩次，沒有去重；一件事一則講完。
- 自己不會戳自己（用環境變數 `ANGAREION_IDENTITY` 認自己）。

## 結束碼

- `send`：exit 0 訊息已寫進 Angareion（叫醒結果不影響）；exit 1 用法錯（沒有送出）；exit 2 Angareion 後端或設定錯；其他非 0 是 Angareion 的原結束碼（沒有叫醒任何人）。
- `inbox`：exit 0 讀完（可能是空的，空的時候 stderr 有一行提示）；`--wait` 逾時 exit 3（沒有讀、游標不動）；exit 2 後端或設定錯，**不是「沒有新訊息」**，稍等再試；exit 1 用法錯。

## 叫醒的節制：三條軟規則

aicomm 沒有名單、角色或權限的硬控制；每一次成功的戳都是對方的一輪模型額度，節制靠你遵守下面三條：

1. **預設不中斷**：`send` 預設不打斷對方；一定要打斷才明確加 `--interrupt`（`--interrupt` 與 `--no-wake` 不能同時給）。
2. **隊員基本上不要中斷上位職（PM、PMO）**：不要對他們用 `--interrupt`。
3. **「收到」類回覆不叫醒對方**：沒有要對方動作的回覆（收到、確認、知道了）一律加 `--no-wake`。這也避免兩個 agent 互相回「收到」而互相叫醒。回報完成、有問題要問這類需要對方動作的，照常叫醒。

**代價**：沒有硬控制，所以任何同台 agent 都能叫醒 PMO（它閒著時，`aicomm send --to <PMO>` 會戳醒它，花一輪額度）。軟規則失守的後果是多一輪額度，不是壞資料。這與 M2 D6 的 watch 規則並存、不衝突：watch 管秘書派工的急件，aicomm 管 AI 之間的一般溝通。之後真的要硬控制，走 sidecar open 帶白名單（MyLinuxPool#346），不在 aicomm 做。

## 不做的事

沒有 channel 建刪、開關 agent、單獨戳人、查狀態、ack。這些用 `angareion` 或各自的工具。`aicomm` 只用 Angareion 的指令列和 sidecar 的「查單一 agent」「戳單一 agent」，不改它們。

## 順序風險

`--mention` 沿用 Angareion 的規矩：讀者先升級、再送 `--mention`，否則舊版讀者會漏收只被點名的訊息。

---
name: aicomm
description: Talk to another AI (PMO, PM, teammates) - send a message and wake the recipient, read what was sent to you, or query messages (log). Use `aicomm send` to message another AI, `aicomm inbox` to read your own mail, and `aicomm log` to query (read-only). Only if no one will poke you (not managed by a sidecar), block in `aicomm inbox --wait <seconds>` when idle instead of polling the model; a sidecar-managed agent ends its turn and waits for the poke.
---

# aicomm

aicomm 是 AI 之間的溝通指令：**送一次、收一次**。它在 Angareion（留言）上再加一步——送出後，如果收件人在這台機器上、正好閒著或在跑，就戳它一下，叫它來讀信。對外只有三個指令（send／inbox／log）；所有角色（PMO、PM、隊員）用同一份說明。

## 私人資訊：不要貼進訊息

訊息存在 GitHub 上，無法完全避免被看見。**不要在訊息裡放私人資訊**；需要傳遞時，請對方去參照 MyBrain（給路徑或案件 id），不要把內容貼進訊息。

## 三個指令

```bash
aicomm send --channel <ch> [--to <AI 名>] [--mention <AI 名>]… [--in-reply-to <ULID>] [--urgency 0-9] \
            [--ticket <repo>#<n>] [--no-wake | --interrupt] --body '訊息文字'   # stdout 第一行是訊息 id
aicomm inbox [--wait <秒>] [--mentioned] [--json]                               # 讀寄給我的信
aicomm log [同 angareion log 的全部旗標] [--json]                              # 唯讀查詢：不動游標、不叫醒；預設查寄給我的
```

- `--channel` 一定要給（channel 名不能從 AI 名推出來；派工信會寫）。`--body` 一定要給，`-` 從 stdin 讀（多行內文）。
- `--to` 是收件人的 AI 名（`<專案>-<角色>-<team>[-<序號>]`）；不給＝廣播，**廣播不叫醒任何人**。`--mention` 可重複，被點名的人和收件人一樣會被叫醒。
- `--ticket MyAiStorage#212` 只是附上一個 `ticket` 參照，不改內文、不去讀工單；不帶也能送。
- `inbox` 固定讀「寄給我的」，沒有 `--channel`。`--mentioned` 只看寄給我或點名我的（廣播不算）；`--json` 原樣印 Angareion 的輸出。
- 只限沒有人會 poke 的 agent：閒著沒事時用 `aicomm inbox --wait 600` 當唯一的讀者（見下節），阻塞等，不花模型額度；不要在迴圈裡反覆問模型「有沒有新訊息」。被 sidecar 管的，閒著就結束這一輪、等 poke。

## 同一個身分只留一個讀者

`angareion read` 讀走信就推進游標；兩個讀者讀同一個身分，先讀的那個拿走信，另一個看到空的。`aicomm inbox --wait` 在 wait 之後會接著 read，放到背景一樣會把信讀走，所以**不能**當成「背景只等、不讀」。

- 被 sidecar 管的 agent（會被 poke 叫醒）：**不要跑背景讀信迴圈**（`wait → read`，或背景的 `aicomm inbox --wait`）；被叫醒就在**前景**跑 `aicomm inbox`。
- 不在 sidecar 底下、沒有人會 poke 的：才用 `aicomm inbox --wait` 當**唯一**的讀者。
- 補救：被叫醒但 inbox 是空的 → 先看自己有沒有背景讀信任務，它的輸出裡可能已經有那一則；要補讀用 `angareion read --since <時刻>`（RFC 3339，例如 2026-10-25T03:00:00Z；不移動游標）。
- `aicomm inbox` 讀不到信時會在 stderr 提示同一段補救；exit 0 與 stdout 不變。
- 被叫醒先 `aicomm inbox`；要前後文或補讀（往回找某則的上下文、沿回覆追串、搜尋過濾）用 `aicomm log`（唯讀查詢，不動游標）；**不要用 `angareion read --since` 反覆撈**——`--since` 只認時間視窗，一則多行訊息可能只看到第一行，容易漏。

## 找本人：`--to myaientry-secretary-p1`

本人在 Angareion 的位址是 `myaientry-secretary-p1`。AI 要找本人，就用 `--to myaientry-secretary-p1`（或 `--mention myaientry-secretary-p1` 點名）；App 只看 `to`／`mentions`，這樣才會把它顯示成「@你」——那是本人的位址，不是另一個 AI 的名字。內文寫 `@ myaientry-secretary-p1` 只是普通文字，App 不會把它當成 @你。要讓 App 標成 @你（計入未讀 @）要帶 `--to` 或 `--mention`；本人不在 sidecar 上，所以不會被叫醒；廣播會進本人的 inbox，但不叫醒、也不標 @你。

- 本人只跟 PMO 說話的規矩不變：PM／隊員回答本人的 @ 可以，要本人決定的事仍經 PMO。
- 權限不變：寄件者是自報的，不是認證；這個位址不帶來額外權限。

## 派工：只接受 `secretary-pmo`

本人要派事給秘書／PMO，只認 `secretary-pmo` 這個 channel；MyAiEntry App 的派工工具一律送它（填別的 channel 也會被夾成這個）（細節見 MyAiEntry#230；不使用 App 的角色可略過）。**自由文字不當派工**：在其他 channel 或內文裡寫「請做某某」，不會被當成派工單，也不會因此有派工權限（派工權限不變）。

## 送達與叫醒

- **送達＝訊息寫進 Angareion**。寫入成功，`send` 就是 exit 0，不管叫醒成不成功；叫醒失敗只在 stderr 印一行（「已送達，未叫醒…」）。看到 exit 0 就不要重送。
- 叫醒是盡力而為：只有收件人在**這台機器的 sidecar** 上、而且狀態是 `idle` 或 `running` 才戳；不在本機、`starting`、`waiting-quota`、`exited` 都不戳（訊息還是在 Angareion，對方之後 `inbox` 一定讀得到）。
- 戳的內容固定（請對方執行 `aicomm inbox`），不含你的訊息。對方在 `running` 時，戳會排到它這一輪之後，不打斷。
- 連送兩則會戳兩次，沒有去重；一件事一則講完。
- 自己不會戳自己（用環境變數 `ANGAREION_IDENTITY` 認自己）。

## 結束碼

- `send`：exit 0 訊息已寫進 Angareion（叫醒結果不影響）；exit 1 用法錯（沒有送出）；exit 2 Angareion 後端或設定錯；其他非 0 是 Angareion 的原結束碼（沒有叫醒任何人）。
- `inbox`：exit 0 讀完（可能是空的，空的時候 stderr 有一行提示）；`--wait` 逾時 exit 3（沒有讀、游標不動）；exit 2 後端或設定錯，**不是「沒有新訊息」**，稍等再試；exit 1 用法錯。
- `log`：照 `angareion log` 的結束碼（0 成功含空結果、1 用法錯、2 後端或設定錯）；aicomm 不叫醒任何人。

## 叫醒的節制：三條軟規則

aicomm 沒有名單、角色或權限的硬控制；每一次成功的戳都是對方的一輪模型額度，節制靠你遵守下面三條：

1. **預設不中斷**：`send` 預設不打斷對方；一定要打斷才明確加 `--interrupt`（`--interrupt` 與 `--no-wake` 不能同時給）。
2. **隊員基本上不要中斷上位職（PM、PMO）**：不要對他們用 `--interrupt`。
3. **「收到」類回覆不叫醒對方**：沒有要對方動作的回覆（收到、確認、知道了）一律加 `--no-wake`。這也避免兩個 agent 互相回「收到」而互相叫醒。回報完成、有問題要問這類需要對方動作的，照常叫醒。

**代價**：沒有硬控制，所以任何同台 agent 都能叫醒 PMO（它閒著時，`aicomm send --to <PMO>` 會戳醒它，花一輪額度）。軟規則失守的後果是多一輪額度，不是壞資料。這與 M2 D6 的 watch 規則並存、不衝突：watch 管秘書派工的急件，aicomm 管 AI 之間的一般溝通。之後真的要硬控制，走 sidecar open 帶白名單（MyLinuxPool#346），不在 aicomm 做。

## 不做的事

沒有 channel 建刪、開關 agent、單獨戳人、查狀態、ack。這些用 `angareion` 或各自的工具。`aicomm` 只用 Angareion 的指令列和 sidecar 的「查單一 agent」「戳單一 agent」，不改它們。

## 順序風險

`--mention` 可以用了（讀者都已升級，2026-10-25）。舊版讀者不認得 `mentions`，會漏收只被點名的訊息，所以以後若有新的讀者種類加進來，要先確認它讀得懂 `mentions`。

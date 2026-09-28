# Agora 的 session 操作：從場景推出的指令

> 2026-09-28 本人確認（v2，含轉接器命名 `agora-<coding agent 名稱>`）。取代 ADR 0010 裡暫定的 `agora init session`。ADR 0010 的原則不變：新 session 帶著前面的內容開始、開頭原樣保留以命中 KV cache、AI 不再自己 claim。

## 誰會用

- **你**：在終端機裡開新的 AI agent。
- **正在工作的 AI**：工作中要分工、交出成果、查別的 session，或自己開新的 agent。

能不能開 agent、能開幾個，由那台機器決定（例如 MyLinuxPool 的一個 worker 就是一個團隊，裡面可以自由開）。這不歸 Agora 管，所以 AI 與你用的是同一組指令。

## 場景

| # | 場景 | 心裡想的 | 關係 |
|---|---|---|---|
| A | 接著做 | 「開一個新 agent，接著 S1 做下去」 | 1→1 |
| B | 分工 | 「S1 規劃好了，拆成 n 份，各開一個 agent 做」 | 1→n |
| C | 匯總 | 「S2、S3 做完了，開一個 agent 把兩邊的成果整合起來」 | n→1 |
| D | 互相參照 | 「S2、S3 同時在做，要能看對方做到哪」 | n↔m |
| E | 找 | 「上週那個 session 在哪？有哪些工作在等人接？」 | — |

## 基本概念

- **起點**：一個 session 的某個位置（快照＋那一則訊息）。不指定位置，就是最新已提交的那一則。
- **交接單**：持有者寫下的「起點＋要交代的任務」。只能被接一次，接之前會出現在「等人接的工作」清單裡。
- **起點包**：`agora checkout` 產出的一個目錄，內含起點之前的**原始紀錄（原封不動）**、要交代的任務，以及來源 session 的 id。它不屬於任何一個 coding agent。
- **轉接器**：每個 coding agent 一個，命名為 `agora-<coding agent 名稱>`（`agora-opencode`、之後的 `agora-claude-code`…），和同步器放在一起。它負責把起點包載入成那個 agent 的原生 session。

Agora 本身不依賴任何 coding agent：它只管讀、寫交接單、產出起點包。載入與開 agent 是轉接器與呼叫者的事。

## 指令

```
agora find [關鍵字] [--case <案件>] [--waiting]      # E：找 session；--waiting 列出等人接的交接單
agora show <session>                                  # E：看內容與前後關係
agora read <session>                                  # D：讀最新已提交的內容，並記下一條參考關係
agora handoff <session> [--at <訊息>] --task "…" [--task "…" …]
                                                      # B、C：每個 --task 寫一張交接單
agora checkout <起點>… [--task "…"] -o <目錄>          # A、B、C：產出起點包
```

`<起點>` 可以是 `handoff:<id>`（接某張交接單），也可以是 `<session>[@<訊息>]`（直接從任何 session 的任何位置開始）。

`agora checkout` 做的事：
1. 從讀取介面取得起點與被釘住的快照，把原始紀錄原封不動放進起點包；
2. 起點是交接單時，登記「由我接手」，等確認沒有人先接走；被拒就不產出；
3. 多個起點（n→1）時，最長的一段放最前面，並檢查總長度沒有超過目標模型的上限，超過就明確拒絕。

**轉接器（以 opencode 為例）：**

```
agora-opencode load <起點包>        # 截斷、重編 id、opencode import → 印出新 session id
opencode --session <新 session id>      # 誰要開、在哪開，由呼叫者決定
```

同一個 agent 之間接續時，轉接器直接使用起點包裡的原始紀錄，新 session 送給模型的開頭會與原 session 位元組相同（技術驗證 `docs/spike/session-import.md`）。跨 agent（例如 opencode → Claude Code）時，改走共通閱讀版轉換，開頭會改寫，並在 metadata 標記。

| 場景 | 怎麼下 |
|---|---|
| A 接著做 | `agora checkout S1 -o p/` → `agora-opencode load p/` |
| B 分工 | `agora handoff S1 --task "做前端" --task "做後端"` → 每張各跑一次 `checkout handoff:Hx` 加 `load` |
| C 匯總 | `agora checkout handoff:H2 handoff:H3 --task "整合兩邊的成果" -o p/` → `load p/` |
| D 互相參照 | 工作中的 AI 用 `agora read S3` |

AI 在 session 裡用的是同一組指令，透過 skill 包成工具：`agora_find`、`agora_read`、`agora_handoff`、`agora_checkout`。

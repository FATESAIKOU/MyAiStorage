# Agora 直接建出 session，AI 載入就能開工

> 2026-09-28 本人確認。取代 design D3 中「AI 自己呼叫 claim 工具認領」的部分。
>
> 同日更新：指令的名稱與形狀改以 `docs/design/agora-session-operations.md` 為準。`agora checkout` 產出起點包，由各 coding agent 的轉接器 `agora-<名稱>` 載入成原生 session；Agora 本身不依賴任何 coding agent，開 agent 由呼叫者決定（AI 也可以）。下方的 `agora init session` 範例僅為當時的暫定形狀。
>
> 2026-09-30 補充（實作面的補充，決策不變）：下方「n→1：最長的一段原封不動放最前面」在
> opencode 上**只排陣列不夠**——`import` 之後它照 `time.created` 重新排序，送給模型的
> 上下文也是照時間排出來的（`docs/spike/session-import.md` Q6.1／Q6.2）。所以轉接器會把
> 後面各段的時間整體往後排到第一段之後（段內相對順序不動、第一段一個位元組都不動），
> 起點包 metadata 記一筆 `time_shift` 宣告這件事。證據與實作見
> `docs/spike/evidence/impl2-import-id-collision.md` 第 6 節。

分裂（1→n）、統合（n→1）、相互參照（n↔m）是 Agora 要支援的事。原本的做法是：新的 AI session 一開始是空的，由 AI 自己呼叫 `claim` 工具，工具再把前一個 session 到接續點為止的內容當成回覆交給它。本人要的是另一種形狀：由 Agora 依交接單直接**建出**一個 opencode 或 Claude Code 原生格式的 session，AI 一載入就帶著前面的內容，直接接著做。

```
agora init session --handoff <交接單>… [--reference <session>…] --app opencode|claude-code
  → 產生原生 session，匯入來源應用
  → 同時把一筆簽章過的「認領」放進 Agora 的收件匣
```

- **1→n**：同一個接續點建出 n 個 session。
- **n→1**：多張交接單合成一個 session。
- **n↔m**：工作中仍用 find／read 工具讀對方，留下參考 Link。
- AI 工作中照樣用 `split` 寫交接單；**AI 不再自己 claim**，認領一律由 `init session` 記下。
- 新 session 的上傳不變：同步器照常把它送進 Agora 的收件匣，提交流程收進 Agora 時建立接續 Link。

## KV cache

供應商的 prompt cache 只在「開頭的 token 完全相同」時命中，所以建 session 的方式直接決定成本與速度：

- **同應用接續**（opencode→opencode）：用原始紀錄**原封不動**重建到接續點，不經過閱讀版轉換，讓新 session 的開頭與原 session 位元組相同。
- **1→n**：n 個 session 的開頭完全相同，只在接續點之後分岔；同一個模型、在 cache 有效期內可以共用同一份 cache。
- **n→1**：最長的一段原封不動放最前面，其餘接在後面。
- **跨應用**（opencode→Claude Code）或**需要壓縮**時，cache 一定斷，所以只在必要時才走閱讀版轉換或壓縮，並在建出的 session 的 metadata 記下「開頭已改寫」。

## Considered Options

- **維持 AI 自己 claim**：不需要碰來源應用的內部格式，但新 session 的開頭是一段工具回覆，永遠不會命中原 session 的 cache；AI 也可能不照指示呼叫工具（期 1 的免費模型就發生過）。
- **兩種都保留**：彈性大，但要驗的路徑多一倍。

## Consequences

- Agora 要能產生來源應用的原生 session 格式，並依賴它們的匯入方式（opencode 的匯入指令、Claude Code 的 session 檔與 `--resume`）。這兩條路徑要先做技術驗證。
- 跨應用建 session 允許，經共通閱讀版轉換：文字完整，工具呼叫只留摘要。期 1 先做 opencode→opencode，跨應用放之後。
- n→1 合併兩個長 session 可能超過模型的 context 上限，需要壓縮策略；期 1 先偵測並明確拒絕，不默默截斷。
- 期 1 的 9.1〜9.3 驗收改成以 `init session` 建出 S2、S3、S4。

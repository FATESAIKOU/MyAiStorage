# 每輪只送新增的段（#170，#109 R3b-2）

和 [#169 的分段寫入](169-segmented-raw-write.md) 是**同一個 release**。

## 行為

- **claude 的增量**：`agora continue` 的定期寫回與 `agora import` 的更新，會讀這個 Session 的 **cursor**（`<state>/cursors/<ULID>.json`，本機狀態，不上傳），只把「上一次封存之後」新增的 raw 按 `AGORA_RAW_CHUNK`（預設 4 MiB）切成新段；之前封存的段原樣沿用、不重傳。每輪上傳量的上限是 `C＋I＋S`（C＝一段、I＝新增、S＝閱讀版 `session.md`），不再隨 Session 的總大小成長。
- **第一次匯入就分段**：一個 814 MB 的 claude Session 第一次 `import` 就是約 195 個封存段＋1 個 tail，不是一個巨段。小 Session（main ≤ 一段）仍是一個檔。opencode／agy／merge／fork 沒有增量，每輪仍是整份（新的一組段）。
- **退回整份重切（fail-safe）**：不是單純追加時——`/compact`、resume 後內容被改寫、來源檔變短、換了 agent session、cursor 不見或壞掉、cursor 與上一版的清單對不上——這一版整份重切成新的一代（`gen+1`），**不會送出錯的增量**。sidecar（aux）變動**不**在此列：aux 在 raw 的 main 之後，只會讓這一版的 tail 改變，其餘照常 append。換機器或清掉 `~/.cache/agora`、`state/` 後，第一次寫回也是整份（一次），之後差分。
- cursor 只在 `stage` 成功（版本已換進 outbox）**之後**才寫；`stage` 失敗就留著舊的。`agora delete` 一併刪掉它。

## 舊格式 session 不能用一般 `import` 更新

Drive 上沒有 `layout` 的 session（舊版上傳的）：`agora import` 遇到來源相同的舊格式 Session 會**拒絕**（不原地更新、不分支），訊息指向 `agora import … --as-new`。**`--as-new` 本身還沒做**（#109 R4，task 4.3）；在那之前想保留內容只能留著閱讀版，或自己 `agora delete` 之後重新 import。

## 其他

- 量測（814 MB 等效、多輪）見 `docs/perf.md`。
- `GOLDEN_MD5_SESSION_MD`：本 slice 沒有再變（header 的形狀 #169 已定）；兩個 raw 的指紋沒變。

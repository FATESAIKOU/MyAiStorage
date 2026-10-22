# raw 改成分段寫入（#169，#109 R3b-1）

**這是格式變更。** 這個 release 起，agora 寫上 Drive 的 session 不再有整份 `raw-<md5>.json`，而是一串 `seg-<世代>-<序>-<md5[:12]>.part`；`session.md` 的 header 就是清單：

```yaml
agora:
  raw: {md5: …, size: …, layout: chunks/1, segments: [{file: seg-0001-0000-….part, size: …, md5: …}, …]}
```

沒有 `file` 欄位。本機的 outbox 與鏡像仍留**整份** raw（`raw-<md5[:12]>.json`），`show --raw`、`continue`、`push` 都靠它。

## 行為

- **上傳**：每一輪只送 Drive 上缺的段（更新既有 id 時比對列表；新 session 全送），再送 `session.md`；確認時 `session.md` 與清單裡的**每一個段**都要在 Drive 上、md5 對，才離開 outbox。送出前每個檔都 `os.utime` 成現在（Drive 的 `ModTime` 是上傳那一刻，孤兒寬限期從它算）。
- **清舊段**（確認之後）：保留目前清單、`state/published/<ULID>.json` 記的前一版（只有它等於送出前 Drive 的 `session.md` 才算）、以及 `ModTime` 在 `AGORA_RAW_ORPHAN_GRACE`（預設 3600 秒）內的段；其餘 `seg-*` 刪。別台剛傳、還沒被 `session.md` 指到的段在寬限期內不會被刪。Drive 上的 `raw-*` 不碰。
- **`AGORA_RAW_CHUNK`**（預設 4194304 bytes）決定一段多大；壞的設定值是明確錯誤，不會靜默回預設。
- **刪除偵測**（R2）沿用：資料夾 ID 變了才算複製期間被別台刪掉；本輪送出的 `session.md` 與所有段都算「我們的」檔。

## 升級要注意

- **升級前留在 outbox 的舊格式版本**（沒有 `layout`）不會被上傳：上傳器逐筆略過、每個 entry 只警告一次，它留在 outbox 直到那個 Session 被 `agora delete`。**升級前請先讓 outbox 排空**（在舊版上跑到不再印「outbox 有 N 筆未上傳」）。
- Drive 上沒有 `layout` 的 session（舊版上傳的）：閱讀版照常可讀，raw 不可用（`show --raw`／`continue`／`edit` 給明確錯誤），**不會被刪**；要變成新格式用 `agora import … --as-new` 重匯。
- **回不去**：舊版 agora 讀不懂分段的 session（它找不到 `raw.file`），不要在同一個 Drive 上混用新舊版。
- `GOLDEN_MD5_SESSION_MD`（測試的標準答案指紋）因 header 多了 `layout`／`segments` 而重算，本人已同意；兩個 raw 的指紋沒變。

## 舊格式 session 不能 push／revive

沒有 `layout`（或 `layout` 不認得）的 session——舊版上傳的——**不能 `agora push`、也不能 `push --not-exist-upload` 傳回去**：寫回去等於把舊格式再寫上 Drive。想保留內容，請用 `agora import … --as-new` 從原始紀錄重匯成新 Session；想丟掉本機那份用 `pull --not-exist-delete`。「雲端沒有」的提示對這類 session 已改成這個說法。

## R3b-2

增量（每輪只送新增的段）與 `import` 對舊格式 session 的拒絕在 #170，同一個 release：見 [170-incremental-upload.md](170-incremental-upload.md)。

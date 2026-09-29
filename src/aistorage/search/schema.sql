-- AiStorage 搜尋索引兼讀取視圖目錄的 schema（規格的一部分）。
-- TS 用戶端照抄此檔。查詢語意見 schemas/searchindex.md。
-- 時間一律存成 UTC 到毫秒的固定寬度字串（形如 2026-09-27T08:00:00.000Z），
-- 所以字串比較與時間比較的順序一致。

CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- key: format='aistorage.searchindex/v1'、generation、built_at、
--      agora_main_sha、converter_versions(JSON)

CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  producer TEXT NOT NULL,
  case_id TEXT,
  -- running／stopped／reserved。`reserved`＝`agora checkout` 預留好、還沒有人
  -- 真正開工的新 Session（零則訊息的空紀錄）。它與「已經在跑」是兩件事，
  -- 讀取端要分得出來（`agora find --status reserved`）。
  status TEXT NOT NULL,
  stopped_at TEXT,
  in_progress INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  snapshot_at TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL,
  raw_size INTEGER NOT NULL,
  parent_id TEXT,
  reading_status TEXT NOT NULL,
  reading_error_code TEXT,
  committed_at TEXT NOT NULL,
  -- 預留的期限（RFC 3339 UTC Z）。只有 `status='reserved'` 才有值：期限到了只
  -- 是**顯示與管理用的訊號**（`agora show` 標 `expired`），提交流程不會自動刪。
  -- 放在最後一欄：舊世代的 index 沒有它，讀取端讀到時當作 NULL（見 query.py）。
  reserved_until TEXT
);

CREATE TABLE snapshots (
  session_id TEXT,
  snapshot_sha256 TEXT,
  snapshot_at TEXT,
  committed_at TEXT,
  via TEXT,
  -- 該快照原始紀錄的 git-annex key（`SHA256E-s<size>--<sha256>`）。
  -- `agora checkout` 依 key 直接去 Agora 的物件資料夾取物件（唯讀身分有分享
  -- 權限），用 **key 內嵌的 sha256** 驗證，所以讀取視圖不必另外發佈一份 raw：
  -- 內容定址的 key 本身就是位址。`via` 不是 sync/rewrite 的路徑（真本用
  -- git blob 而非 annex）時為 NULL。
  annex_key TEXT,
  PRIMARY KEY (session_id, snapshot_sha256)
);

CREATE TABLE readings (
  session_id TEXT,
  snapshot_sha256 TEXT,
  file_id TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  is_latest INTEGER NOT NULL,
  PRIMARY KEY (session_id, snapshot_sha256)
);

CREATE TABLE links (
  kind TEXT NOT NULL,
  from_session_id TEXT NOT NULL,
  to_session_id TEXT NOT NULL,
  handoff_id TEXT,
  claim_id TEXT,
  snapshot_sha256 TEXT,
  message_id TEXT,
  reference_id TEXT,
  read_snapshot_at TEXT
);
CREATE INDEX links_to ON links(to_session_id);

CREATE TABLE handoffs (
  handoff_id TEXT PRIMARY KEY,
  target_session_id TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  message_id TEXT NOT NULL,
  producer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  case_id TEXT,
  body_json TEXT NOT NULL,
  claimed_by_claim_id TEXT,
  claimed_by_session_id TEXT,
  claimed_at TEXT,
  -- 作者 Session（寫這張交接單的那個 Session 的 id）。讀取端列出待認領的
  -- 交接單時只看主 Session 寫的（PM 決定 9）；拿不到作者（舊資料）時為 NULL，
  -- 視為作者不明而排除。
  author_session_id TEXT
);

CREATE TABLE rejections (
  item_key TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  at TEXT NOT NULL,
  item_id TEXT,
  authenticated INTEGER NOT NULL
);

-- 全文只放最新快照的閱讀版，以訊息為單位（spec 要標出命中的位置）。
CREATE VIRTUAL TABLE message_fts USING fts5(
  text, session_id UNINDEXED, message_id UNINDEXED, idx UNINDEXED,
  tokenize='trigram');
CREATE VIRTUAL TABLE title_fts USING fts5(
  title, session_id UNINDEXED, tokenize='trigram');

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  content_type TEXT,
  name TEXT NOT NULL,
  producer TEXT NOT NULL,
  case_id TEXT,
  produced_by_session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  size INTEGER,
  sha256 TEXT,
  annex_key TEXT,
  repo TEXT,
  path TEXT,
  link TEXT,
  object_file_id TEXT
);
CREATE INDEX artifacts_producer ON artifacts(producer);
CREATE INDEX artifacts_session ON artifacts(produced_by_session_id);
CREATE INDEX artifacts_case ON artifacts(case_id);


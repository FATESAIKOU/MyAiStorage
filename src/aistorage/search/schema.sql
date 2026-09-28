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
  committed_at TEXT NOT NULL
);

CREATE TABLE snapshots (
  session_id TEXT,
  snapshot_sha256 TEXT,
  snapshot_at TEXT,
  committed_at TEXT,
  via TEXT,
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


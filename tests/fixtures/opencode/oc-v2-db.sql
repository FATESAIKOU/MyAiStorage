-- opencode 2.0.26 互動模式的 DB：`session_v2` 與 `session_message` 的文字 dump。
-- schema 是 2.0.26 寫出的原文（sqlite_master.sql）；列都是隔離環境裡的假 session：
-- AAA (ses_ocv2dbAAA) = fake upstream（tests/fakes/fake_upstream.py）一次 run 的 v2 原生列：
--   user → assistant(content 是 read 工具) → assistant(一句話) → idle。
-- BBB／CCC = 同一顆 DB 先預置 v1 表（自編假資料），再由 2.x 自己跑「Migrating sessions」遷移出來的列：
--   BBB: user → assistant(content: reasoning→text→tool→) → system（opencode 加的「工具參數已變更」通知）；
--   CCC: user → synthetic（v1 的附件 part 被拆成獨立的 synthetic 列）。
-- 遷移出來的 session 在 v1 `session` 表也是同一個 id（id 不變，審查 M4）；v1 表的形狀列在 spike 文件。
-- 內容全是自編假資料；id／時間戳／專案目錄固定化，無真實資訊。id 格式：ses_/msg_/prt_ 前綴自定義假碼。

CREATE TABLE `session_message` (
          `id` text PRIMARY KEY,
          `session_id` text NOT NULL,
          `type` text NOT NULL,
          `seq` integer NOT NULL,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          `data` text NOT NULL,
          CONSTRAINT `fk_session_message_session_id_session_v2_id_fk` FOREIGN KEY (`session_id`) REFERENCES `session_v2`(`id`) ON DELETE CASCADE
        );
CREATE TABLE `session_v2` (
          `id` text PRIMARY KEY,
          `project_id` text NOT NULL,
          `workspace_id` text,
          `parent_id` text,
          `fork_session_id` text,
          `fork_boundary` text,
          `slug` text NOT NULL,
          `directory` text NOT NULL,
          `path` text,
          `title` text,
          `version` text NOT NULL,
          `share_url` text,
          `summary_additions` integer,
          `summary_deletions` integer,
          `summary_files` integer,
          `summary_diffs` text,
          `metadata` text,
          `cost` real DEFAULT 0 NOT NULL,
          `tokens_input` integer DEFAULT 0 NOT NULL,
          `tokens_output` integer DEFAULT 0 NOT NULL,
          `tokens_reasoning` integer DEFAULT 0 NOT NULL,
          `tokens_cache_read` integer DEFAULT 0 NOT NULL,
          `tokens_cache_write` integer DEFAULT 0 NOT NULL,
          `revert` text,
          `permission` text,
          `agent` text,
          `model` text,
          `time_created` integer NOT NULL,
          `time_updated` integer NOT NULL,
          `time_idle` integer,
          `time_viewed` integer,
          `idle_outcome` text,
          `time_compacting` integer,
          `time_archived` integer,
          `time_suspended` integer,
          `resume_attempts` integer DEFAULT 0 NOT NULL,
          CONSTRAINT `fk_session_v2_project_id_project_id_fk` FOREIGN KEY (`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE
        );

insert into session_v2 (id, project_id, slug, directory, title, version, time_created, time_updated, time_idle, idle_outcome) values
  ('ses_ocv2dbBBB0000000002', '0123456789abcdef0123456789abcdef01234567', 'probe-one', '/tmp/agora-it-opencode/proj-2', '遷移的舊座談一', '2.0.26', 1790214209000, 1790214211600, NULL, NULL),
  ('ses_ocv2dbCCC0000000003', '0123456789abcdef0123456789abcdef01234567', 'probe-two', '/tmp/agora-it-opencode/proj-2', '遷移的舊座談二', '1.18.34', 1790214219000, 1790214221000, NULL, NULL),
  ('ses_ocv2dbAAA0000000001', '0123456789abcdef0123456789abcdef01234567', 'oc-v2db-fixture', '/tmp/agora-it-opencode/proj', 'oc-v2db fixture', '2.0.26', 1790214199000, 1790214201730, NULL, NULL);

insert into session_message (id, session_id, type, seq, time_created, time_updated, data) values
  ('msg_OCV2DB00000000000000000001', 'ses_ocv2dbBBB0000000002', 'user', 0, 1790214210000, 1790214210000, '{"text":"舊的問題一：這是假資料。","time":{"created":1790214210000}}'),
  ('msg_OCV2DB00000000000000000002', 'ses_ocv2dbBBB0000000002', 'assistant', 1, 1790214210100, 1790214210600, '{"agent":"build","model":{"providerID":"opencode","id":"space-bunny-free","variant":"default"},"content":[{"type":"reasoning","text":"ZZTHINK 假的思路。","time":{"created":1790214210110,"completed":1790214210120}},{"type":"text","text":"舊的回答一：同樣是假資料。"},{"type":"tool","id":"call_V1OLD0001","name":"read","state":{"status":"completed","input":{"path":"fake-note.txt"},"content":[{"type":"text","text":"ZZNOTE 這是一張紙條。"}],"metadata":{"truncated":false}},"time":{"created":1790214210140,"completed":1790214210150}}],"cost":0,"tokens":{"input":1,"output":2,"reasoning":0,"cache":{"read":0,"write":0}},"time":{"created":1790214210100,"completed":1790214210600}}'),
  ('msg_OCV2DB00000000000000000003', 'ses_ocv2dbBBB0000000002', 'system', 2, 1790214210100, 1790214210600, '{"text":"The available tools have changed.\n\nThe `read` tool now takes `path` instead of `filePath`.","time":{"created":1790214210100}}'),
  ('msg_OCV2DB00000000000000000004', 'ses_ocv2dbCCC0000000003', 'user', 0, 1790214220000, 1790214220000, '{"text":"舊的問題二","time":{"created":1790214220000}}'),
  ('msg_OCV2DB00000000000000000005', 'ses_ocv2dbCCC0000000003', 'synthetic', 1, 1790214220000, 1790214220000, '{"text":"舊的附件內容","time":{"created":1790214220000}}'),
  ('msg_OCV2DB00000000000000000006', 'ses_ocv2dbAAA0000000001', 'user', 5, 1790214200000, 1790214200029, '{"time":{"created":1790214200000},"text":"\"第一輪：先讀 fake-note.txt 再回答。\"","files":[]}'),
  ('msg_OCV2DB00000000000000000007', 'ses_ocv2dbAAA0000000001', 'assistant', 6, 1790214200077, 1790214200593, '{"time":{"created":1790214200077,"streamed":1790214200535,"completed":1790214200591},"agent":"build","model":{"id":"fake-model","providerID":"fakeup","variant":"default"},"content":[{"type":"tool","id":"call_fakeup_1","name":"read","executed":false,"state":{"status":"completed","input":{"path":"fake-note.txt"},"content":[{"type":"text","text":"Read file fake-note.txt, lines 1-1\n1: ZZNOTE 這是一張紙條。"}],"metadata":{"truncated":false}},"time":{"created":1790214200521,"ran":1790214200530,"completed":1790214200555}}],"snapshot":{"start":"0123456789abcdef0123456789abcdef0123456789ab","end":"0123456789abcdef0123456789abcdef0123456789ab","files":[]},"finish":"tool-calls","rawFinish":"tool_calls","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}'),
  ('msg_OCV2DB00000000000000000008', 'ses_ocv2dbAAA0000000001', 'assistant', 13, 1790214200613, 1790214200726, '{"time":{"created":1790214200613,"streamed":1790214200699,"completed":1790214200725},"agent":"build","model":{"id":"fake-model","providerID":"fakeup","variant":"default"},"content":[{"type":"text","text":"ZZFAKEUP 這是一句話的回答。"}],"snapshot":{"start":"0123456789abcdef0123456789abcdef0123456789ab","end":"0123456789abcdef0123456789abcdef0123456789ab","files":[]},"finish":"stop","rawFinish":"stop","cost":0,"tokens":{"input":0,"output":0,"reasoning":0,"cache":{"read":0,"write":0}}}'),
  ('msg_OCV2DB00000000000000000009', 'ses_ocv2dbAAA0000000001', 'idle', 18, 1790214200729, 1790214200730, '{"time":{"created":1790214200729},"outcome":"succeeded"}');

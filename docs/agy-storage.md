# agy (Antigravity CLI) conversation storage — PR0 probe notes

Public notes on how the **agy** CLI (Antigravity, version **1.4.3** at the time of
this probe; the earlier interface note assumed 1.4.2) stores conversations on
disk, and how `tests/fixtures/agy/make_agy_db.py` reproduces that shape so the
read-only adapter (PR1) has deterministic test data. Everything written here was
learned from **fake conversations generated only from synthetic text**
(`hello probe`-style prompts, number lists, a single `echo hi` tool call); no
real session content, no credential files, no paths containing real user names
appear in this document.

Provenance: three fake conversations opened with the real agy CLI inside a
temp cwd (the findings below are global to the CLI version, not to that cwd);
schema, timestamps and uuids are structural facts, safe to publish.

## 1. Files and layout

Under the agy home (default `~/.gemini/antigravity-cli/`, overridable - that is
what `AGORA_AGY_HOME` should be for):

| path | what it is |
|---|---|
| `conversations/<conversation-uuid>.db` | one SQLite 3 database per conversation (WAL mode, page size 32768) |
| `conversations/<uuid>.db-wal`, `.db-shm` | sidecars, **only while a write turn is in progress / a connection is open**; absent when idle or after exit |
| `conversation_summaries.db` | SQLite (WAL): list/titles/preview/step-count index over all conversations (*not deciphered here - it is shared across all sessions and holds real titles; treat its content as private*) |
| `brain/<conversation-uuid>/` | scratch area, empty-ish for fresh CLI conversations: `scratch/`, `.user_uploaded/`, and `.system_generated/{logs/transcript.jsonl, logs/transcript_full.jsonl, logs/chunks/, steps/<idx>/}` *(names and sizes only were inspected; contents were not read)* |

The `.db` file name equals the conversation id. Note the ids stored *inside* are
not that uuid - see §5.

## 2. Database shape

Standard SQLite 3, `page_size = 32768`, journal mode **WAL** (the persisted
write/read format bytes in the main header stay `2/2` even after agy drops the
sidecars - so an idle file is still "a WAL database without its -wal"),
`user_version = 1`, `auto_vacuum = 0`, UTF-8. Full schema (verbatim):

```sql
CREATE TABLE `trajectory_meta` (`trajectory_id` text,`cascade_id` text,`trajectory_type` integer,`source` integer,PRIMARY KEY (`trajectory_id`));
CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,`status` integer NOT NULL DEFAULT 0,`has_subtrajectory` numeric NOT NULL DEFAULT false,`metadata` blob,`error_details` blob,`permissions` blob,`task_details` blob,`render_info` blob,`step_payload` blob,`step_format` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE INDEX `idx_steps_status` ON `steps`(`status`);
CREATE INDEX `idx_steps_step_type` ON `steps`(`step_type`);
CREATE TABLE `gen_metadata` (`idx` integer,`data` blob,`size` integer NOT NULL DEFAULT 0,PRIMARY KEY (`idx`));
CREATE TABLE `executor_metadata` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `parent_references` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
CREATE TABLE `trajectory_metadata_blob` (`id` text DEFAULT "main",`data` blob,PRIMARY KEY (`id`));
CREATE TABLE `battle_mode_infos` (`idx` integer,`data` blob,PRIMARY KEY (`idx`));
```

Row semantics observed on fake conversations:

- `trajectory_meta`: exactly one row; `cascade_id` **is the file-name uuid**,
  `trajectory_id` is a separate inner uuid; `trajectory_type=4`, `source=17`
  (CLI conversations; other enum values not observed).
- `steps`: one row per step; `step_payload` carries everything interesting as
  protobuf (next section). `metadata` duplicates one sub-message of the payload
  (§4). `error_details`/`permissions`/`task_details` were empty in every
  observed step. `render_info` is only non-NULL on tool steps.
- `gen_metadata`: 1-2 rows (a small "bootstrap" row plus one large "main
  context" row); the mapping to turns is not fully determined (one of the fake
  conversations ended with only the large row after a single turn). Sizes
  observed on real agy: bootstrap ≈1.2 KB, main ≈52 KB (real observations; the
  fixture is shape-only and its synthetic blobs are smaller).
- `executor_metadata`: **exactly one row per user turn**, a ≈16.9 KB template
  (byte-length was identical across conversations; real observation - the
  fixture keeps the same field classes but a different size).
- `trajectory_metadata_blob`: one row `id='main'` containing workspace state:
  a nested title-ish proto, the two uuids of §5, `file://` workspace URI,
  project name (`default-cli-project` for CLI conversations), plus one 492-byte
  opaque blob that also recurs inside gen/executor rows.
- `parent_references`/`battle_mode_infos`: empty for fresh CLI conversations.

## 3. Wire format and resolved field paths

Blobs are plain protobuf wire format (no compression, no encryption at the
container level; some interior values are opaque blobs that could not be parsed
- they are listed as such). Every `step_payload` starts with:

| field | meaning |
|---|---|
| `1` | step_type again (same as the column) |
| `4` | final status (same as the column) |
| `5` | per-step context envelope (repeated structure, `5.12` below) |

Context envelope (payload field 5), observed on all steps:

| path | meaning (as observed) |
|---|---|
| `5.1` | start timestamp {1: unix seconds, 2: nanos} |
| `5.3` | actor int: **4 = user-written step, 2 = assistant/tool side** |
| `5.6` `5.7` `5.8` | timestamp triplets (assistant steps only) |
| `5.6/.7/.8.1.2` | nanos parts |
| `5.9` | generation stats (model steps): token counts (1/2/3/6), `5.9.7` = `bot-<uuid>` agent id, `5.9.8` = {1: "sessionID", 2: numeric id}, `5.9.11` = short id |
| `5.11` | context token count (int) |
| `5.12` | **conversation-scoped execution uuid, same value in every step of the conversation** (and equal to a field inside `gen_metadata`) |
| `5.20` | ids: {1: trajectory_id, 4: cascade_id}; on user steps it arrives as the equivalent pre-serialized bytes, on assistant steps as a nested message |
| `5.26` | status history: repeated {1: status code, 2: timestamp} - codes seen: 1, 2, 3, 8 |
| `5.32` | another timestamp pair (assistant steps) |
| `5.34` | permission decision: {4: "allow"} (tool steps) |
| `5.35` | 32-hex per-step session-ish id |
| `5.38` | int (1) on tool steps |

User steps (`step_type = 14`) - the user text lives at:

- `19.2` = the typed prompt (UTF-8)
- `19.3` = wrapped twin: embedded message whose `1` repeats the same text
- `19.4` = empty message (constant)
- `19.12` (and `19.13` on later user turns) = ≈1.3 KB generation-context blob;
  contains, at deep paths, the tool-permission strings class
  (`read_file(...)`, `write_file(...)` with workspace paths) and other config
  info - settings, not dialogue.

Model steps (`step_type = 15`):

- text turns: `20.1` = the reply text; `20.8` = same text again (display copy);
  `20.6` = per-step `bot-<uuid>`; `20.14.2.1` = opaque ≈150-350 B blob.
- tool-call turns instead carry a parts list: `20.7` = one part with
  {1: call id (`call_...`), 2: tool name (`run_command`), 3: args as a JSON
  string, 7.2.1: opaque 264 B blob}. The JSON keys mirror the executed command
  (e.g. `{"CommandLine":"echo hi",...}`).

Tool steps (`step_type = 132`):

- `5.4` = the call echo: {1: call id, 2: tool name, 3: args JSON, 7.2.1: the
  same opaque 264 B blob as in the model part above}
- `payload.140` = the result envelope:
  - `140.1` = repeated key/value pairs (e.g. `toolSummary`, `CommandLine`,
    `IsDaemon`, `WaitMsBeforeAsync`)
  - `140.2.1` = **the tool output text as the agent saw it**
    (observed shape: `Command exited with code N.\n\nCommand output:\n<out>` with
    CRLF tail on the captured output)
  - `140.2.3` = display tree proto {5: "Ran", 6: "terminal", 9.1: command,
    10.3.1: command, 10.3.2: cwd, 10.3.3: int, 10.3.4: captured output with CRLF}
  - `140.2.6` = typed detail: {1: `type.googleapis.com/exa.cortex.core.tools.RunCommand`,
    2: {5: shell name, 6: command, 7: cwd}}
  - `140.7` = `file:///.../brain/<conversation-uuid>/.system_generated/steps/<idx>/...`
    URL (embeds the conversation uuid again)
- `render_info` column (small display summary):
  {5: "Ran", 6: "terminal", 9.1: command, 10: {1: command, 2: cwd, 3: 0, 4: output CRLF}}

So a PR1 `turns()` decoder can extract, per row:

| step_type | what to extract | path from `step_payload` |
|---|---|---|
| 14 | user text | `19.2` |
| 15 | model text (only when a text part exists) | `20.1`; skip if absent |
| 15 (tool-call part) | tool name + args preview | `20.7.2`, `20.7.3` |
| 132 | tool name + output text | `5.4.2`, `140.2.1` |

Unknown step types should be passed through raw/unhandled (interface note §2),
not dropped.

## 4. Column semantics (observed)

- `step_type`: 14 = user input, 15 = model reply (text or tool-call part),
  132 = tool call execution step. No other values appeared in the fake
  conversations (sub-steps for other tool kinds, IDE interactions, etc.: unknown).
- `status` (column) = 3 for every finished step; inside `5.26` the transitions
  recorded were 1 → 2 → 3 on tool steps and 8 → 3 on model steps (code
  semantics beyond "3 = done" unknown).
- `has_subtrajectory`: 0 everywhere in the observed data (including tool steps,
  which do carry nested opaque blobs - those did not flip this flag).
- `step_format`: 0 everywhere (plain wire protobuf; other formats: not
  observed - when/if they appear, the decoder must treat them like unknown
  step types).
- `metadata` column: **byte-equal to `step_payload` field 5** (the context
  envelope) on every observed step - a cheap consistency check, not a separate
  message family.

## 5. Where the conversation uuid lives (fork/rename implications)

- file name = conversation (cascade) uuid;
- `trajectory_meta` row: `trajectory_id` (inner uuid) + `cascade_id`;
- every step's `5.20` repeats **both** ids;
- `140.7` URLs and workspace references embed the conversation uuid;
- `trajectory_metadata_blob` row carries both ids again.

Conclusion for PR2: renaming/forking to a new id is **not** a
`trajectory_meta`-only rewrite; step payloads must be rewritten too (or the
restore plan reuses the original id).

## 6. gen_metadata / executor classes (by class, no content)

- `gen_metadata.data` top-level: {1: main proto with a ≈15 KB opaque sub-blob,
  repeated 30-2000 B config messages (workspace paths, skill names, IDE state
  entries by class), token counts}, {3: ≈4.4 KB proto}, {4: the per-conversation
  execution uuid of `5.12`}, {8: the shared 492-byte opaque blob}.
  Plus tool-permission strings (`read_file(...)`, ...) of the same class PR0's
  interface note already recorded.
- `executor_metadata.data`: {1: int, 2/3: small ints, 9: uuid, 10: ≈7.3 KB
  opaque blob, 11: the 492-byte blob, 16: 14 B, 17: int, 18: ≈7.7 KB blob}.

The opaque blobs are the only content classes this probe could not decode; the
fixture generator fills them with deterministic synthetic bytes.

## 7. WAL behaviour (measured on fake conversations)

- **Turn start:** agy opens the db writable → `-shm` (32768 B) and `-wal`
  appear; the main `.db` mtime/size does not change until turn end.
- **During the turn:** the wal briefly grows on the order of **1.0-1.9 MB per
  turn** (measured 1,082,168 B and 1,344,504 B at the first samples ~1-6 s into
  two different turns; writes are batched, sampling was coarse). This dwarfs
  the visible new content (steps + one ≈17 KB executor row + one ≈52 KB
  gen row) - it reflects page-level churn (32 KB pages, re-written context
  blobs), not 1 MB of user text.
- **Turn end:** within ≤1 s of the turn finishing, the wal checkpoints into the
  main file (main grows by a few pages, e.g. 13→15 pages) and the sidecars are
  deleted (`lsof` showed no open conversation handles between turns). So an
  agy process left idle between turns holds **no** `.db-wal`/`.db-shm`.
- **Exit:** after `/exit` the conversation db stays WAL-mode-flagged in its
  header but has no sidecars; `file:...?immutable=1` opens it fine.
- **Kill during a turn:** not tested (would be required to prove
  crash-recovery semantics).

## 8. Snapshot / export practice (evidence)

- Idle conversations (no sidecars): copy `.db` alone; open with
  `file:...?immutable=1` → `PRAGMA quick_check` = ok. (Matches the
  interface note §1.2①; `sqlite3 -readonly` on the *original* still fails
  because it cannot create `-shm` next to a live-running agy - but that is a
  live-dir behavior, snapshots don't hit it.)
- **Mid-turn copy validated once more (this PR):** during a running turn the
  triple `.db` + `.db-wal` + `.db-shm` was copied, and the copy - opened with a
  *normal* connection (which replays the wal) - gave `quick_check` = ok and
  showed the in-progress state (2 steps where the turn later finished with the
  same 2 steps + the completed turn). Consistent with the interface note
  §1.2②; second confirming data point, still not stress-tested.
- **PR1 copies two files, not three.** `-shm` is a wal-index cache that SQLite
  rebuilds on the first connection, so copying it adds a file to check without
  adding consistency; the adapter copies `.db` + `.db-wal` only (review #26
  finding 9). This is a choice made after the probe, not a contradiction of it:
  the triple copy above is what was measured in PR0; PR1's snapshot is
  `.db`+`-wal` → replay in a temp dir → `VACUUM INTO`.
- **`VACUUM INTO` determinism:** three consecutive runs of
  `VACUUM INTO` from the same agy-format db produced **byte-identical** output
  (same md5), and another export of the same source unchanged it again. The
  PR1 golden standard "export twice → equal md5" is safe *as long as the
  snapshot itself does not get rewritten between exports* (it had earlier been
  feared that header change-counter fields might churn; they did not).
- Snapshot hygiene: a plain (read-write) open on a sidecar-less copy leaves
  0-byte `-wal`/`-shm` behind on Python's stdlib; treat "no -wal or a 0-byte
  -wal after clean close" as idle. agy's own cleanup removes both.

## 9. The fixture generator

`tests/fixtures/agy/make_agy_db.py` builds N-turn agy-shaped dbs purely
programmatically:

- same schema (the exact `CREATE` strings above), page size 32768,
  `user_version = 1`, `journal_mode = WAL`;
- encodes user/model/tool steps with the field numbers of §3 (a ~40-line
  protobuf encoder, no protobuf dependency);
- forms `metadata` = payload-field-5 bytes and a tool `render_info`, matching
  the real rule;
- fills every opaque blob with deterministic `sha256` filler (seeded uuids via
  `uuid5`, fixed synthetic epoch) so the logical rows and the `VACUUM INTO`
  export are reproducible. Schema and rows are written in a single
  transaction, so two independently generated files are byte-identical (with
  several transactions the SQLite header change counter, offset 24-27/92-95,
  could differ under load); the tests check the whole-file md5, the per-row
  content and the `VACUUM INTO` output;
- **contains no data derived from a real agy db** - texts are caller-supplied
  fake strings (`hello probe`-style), paths are `/tmp/...` placeholders, and a
  unit test scans every generated artifact and the fixture sources for
  home-shaped/credential-shaped strings (`/Users/`, `/home/`, `C:\Users\`,
  `.gemini/`, `google_accounts`, `oauth`);
- `make_db(path, turns, with_wal=False)` returns a clean file without sidecars
  (idle shape); `with_wal=True` returns with the connection left open on
  committed-but-uncheckpointed rows - a **mid-turn / live-writer** shape, not
  agy's idle between-turns shape. The caller copies `db`+`-wal` only (no
  `-shm`; SQLite rebuilds it), runs `VACUUM INTO`, and then closes.

Turn spec: `{"user": ..., "model": ..., "tool": {"name": ..., "args": ...,
"command": ..., "output": ...}}` - the tool key adds the model tool-call part
and the 132 step that echoes it with its result. `raw_steps=[{idx, step_type,
status, step_format, has_subtrajectory, payload}]` appends arbitrary rows so
PR1 can test unknown step types/statuses/formats passing through unchanged;
this is deliberately a minimum shape, not a full agy emulator.

Generated dbs currently only cover the voices this probe saw (no streaming
partial steps, no IDE sub-steps, no crash states).

## 10. Unknowns

- `conversation_summaries.db` cross-checks (title/preview/`step_count`,
  `last_user_input_step_index`) were **not** verified in this PR (the table is
  shared across real sessions; reading its rows was out of scope). PR1 should
  verify against the fake conversations under the PM's direction.
  **PR1 update:** the adapter now reads this file, but only against a
  *synthetic* table built by `tests/unit/test_agent_agy.py` (columns
  `conversation_id, title, preview, step_count, last_modified_time,
  workspace_uris`, a plausible table name). The real table's name, column
  types, timestamp unit and `workspace_uris` encoding are still unverified;
  the adapter discovers the table by columns and accepts seconds,
  milliseconds or ISO timestamps rather than assuming one (`agy_summaries.py`
  `summary_table` / `_iso` / `_workspace`).
- `gen_metadata` row-count semantics (and when the small bootstrap row
  appears) not determined.
- Status codes other than 3, `step_format` other than 0, and
  `has_subtrajectory=1`: never observed - semantics unknown.
- Tool steps other than `run_command`, and a *denied* permission flow: not
  observed (`echo hi` ran without a visible permission prompt in interactive
  mode, and the tool step stored `5.34.4 = "allow"`; how denial is stored is
  unknown).
- brain/<id> file contents, and WAL checkpoint behavior when agy is killed
  mid-turn: untested.
- agy version drift: probe ran on **1.4.3** vs the 1.4.2 note - field paths
  should be re-checked whenever the adapter starts failing, and the raw blobs
  are kept untouched on export precisely because of this risk (interface R1).
- Where agy would record its own version (and a conversation's creation time
  or model name) was not found; the PR1 envelope writes `agy_version: null`
  and `Exported.created_at` / `model` stay empty rather than invented.

## 11. PR1 verification (fixture only)

What `tests/unit/test_agent_agy.py` pins against the programmatic fixture -
these are facts about the fixture and the adapter's rules, not new claims
about a real agy:

- **Idle snapshot:** only the `.db` is copied (no `-wal`, no `-shm`), opened
  `immutable=1`, `quick_check` = ok; the source file's `(size, mtime_ns)` is
  unchanged afterwards.
- **Live snapshot:** `.db` + `.db-wal` copied, `-shm` never; the replay in the
  temp dir sees the committed-but-uncheckpointed rows; `VACUUM INTO` gives a
  single file.
- **Races:** a stat change between the before/after pair (main file or `-wal`
  only) triggers a second copy; a file that vanishes mid-copy (agy's last
  connection removing the wal) is retried on the idle path; three consecutive
  changes/deletions raise `AgentError`, and the temp directory is gone without
  relying on the garbage collector.
- **Golden standard:** exporting the same fake db twice gives the same
  `raw_md5` and `raw_size`; the envelope's `db_b64` decodes to the source file
  byte for byte (idle path) and `turns(raw)` equals the turns carried on
  `Exported`.
- **Turns:** user `19.2`, model text `20.1` and tool call `20.7` come out as
  text / one `[tool] …` line per call; the tool execution step (`132`) is
  recognised and silent - it is not `[skip]`, and its `5.4`/`140.2.1` content
  (including the full output) is only in `scan_text`, not in the reading
  version (design 4.4). An unobserved `step_type` becomes
  `[skip agy-step-<idx>/<type>]` with one `warn` line; a broken protobuf, or a
  field whose declared length runs past the message, skips just that step and
  does not fail the export. `message_count` counts only 14/15 turns that
  produced reading content.
- **scan_text:** every UTF-8 string of ≥ 4 bytes in any length-delimited field
  of `steps.step_payload` / `render_info` / `metadata` is collected,
  untruncated, deduplicated and in order (a 300-byte tool output with a
  `sk-…` string is in `scan_text` and not in `turns()`). `gen_metadata`,
  `executor_metadata` and `trajectory_metadata_blob` are not walked: those
  settings/system-prompt strings stay only inside `db_b64`. The repo has no
  credential-scan implementation; this is the plain-text surface for the
  PM/MyPMO scan, and this gap is known.
- **Listing / search:** the synthetic summaries rows give `Listed` title,
  dir, `updated_at` and the `(mtime, size)` cache avoids a second copy;
  `search_text` matches title/preview only (conversation body text is not
  searched), as the interface note's narrow first version says. Timestamps
  are accepted as seconds/milliseconds/microseconds/ISO inside 2020-2100 and
  are `None` (with one warning) outside it; an unrecognised summaries table is
  an `AgentError`, not an empty list; column names match case-insensitively.
- **Read-only boundary:** the five unsupported adapter methods raise
  `AgentError`; `agora merge` / `continue` (including `--fork`) with
  `--agent agy`, and any merge/continue/fork whose **source** is an
  agy-imported Session, exit 1 before syncing or reading a source.
- **Privacy:** `google_accounts.json` and `antigravity-oauth-token` placed in
  the fake `AGORA_AGY_HOME` are never opened by listing, exporting,
  `last_message` or an import.

## 12. PR2b spike: what agy loads a conversation by (2026-10-21)

The PM ran a real agy once, outside this repository's tests, against a
hand-made conversation db placed at `conversations/<new-uuid>.db` (the file
name rule of §1 holds). This is what the spike found, and what it did **not**
settle:

- **`agy --conversation <new-uuid>` answered `conversation "<id>" not found`
  and opened an empty new conversation.** The conversation db file alone is
  not what agy loads a conversation from.
- The load is keyed by a row in `conversation_summaries.db`:
  `conversation_id = ?`. With no row, agy does not open the file that is
  clearly there.
- The agy executable also carries the strings `jetbox_summaries.pb` and
  `agyhub_summaries.pb`. So there may be **other indexes** (a hub or a
  protobuf side list) involved in loading as well. This has not been
  deciphered and is **unverified**.
- Therefore the continue implemented in PR2b is **restore the db + insert the
  summaries row**: `agy_restore.restore` (content adjudication by
  `(count(*), max(idx))` and the summaries stamp, never mtime; the temp file is
  `quick_check`ed **before** `os.replace`; a Session that records no
  `agora.source.session_id`, or a raw naming another conversation than it, is
  refused - the guard is against **accidents and conflicts** (two writers on
  one Drive, a raw pasted into the wrong conversation), **not against a
  malicious writer**, since the header and the raw sit in the same Drive; an
  overwrite first backs the live db up to agora's own state, atomically and
  privately: the first backup as `<id>.db` (the user's original agy version,
  never replaced) and later ones as `<id>.<UTC stamp>.db`, only the newest
  three of those kept) and `agy_summaries.ensure_summary_row` (only a
  recognised table, only a missing id, one `BEGIN IMMEDIATE` transaction; it
  never touches the list's `-wal`/`-shm`).
  The row's five read columns are `conversation_id`, `title`, `preview`,
  `step_count`, `last_modified_time`; PR2b also wrote `workspace_uris` and
  every other column by its declared type (strings `''`, numbers `0`, blobs
  `NULL`), with `raw_summary` left `NULL`. The real table's constraints and
  the shapes its own rows use were found on 2026-10-21 - see the subsection
  below.

### 2026-10-21 真機驗收發現 (#136)

The first hand acceptance run of §18 (`agora continue --agent agy`) against
the real agy 1.4.4 failed at the summaries insert:
`NOT NULL constraint failed: conversation_summaries.last_user_input_time`.
The real table (`MyAiStorage-team/fixtures-real/agy-summaries-schema.sql`,
schema only, no rows) has three columns `NOT NULL` **without a default**:
`last_modified_time` (datetime), `workspace_uris` (text) and
`last_user_input_time` (datetime). PR2b filled the first two; the third went
through the declared-type rule, which answers NULL for `datetime`.

A row agy itself wrote (test conversation, fake text) looks like this:

| column | value |
| --- | --- |
| `last_modified_time`, `last_user_input_time` | text `2026-10-21 08:41:06.65668+00:00` - a space, five fraction digits here, `+00:00` (not ISO `T…Z`) |
| `workspace_uris` | JSON array text `["file:///private/tmp/agora-acc/proj"]` (not a bare string) |
| `status` | `CASCADE_RUN_STATUS_IDLE` |
| `project_id` / `app_data_dir` | `default-cli-project` / `antigravity-cli` |
| `last_user_input_step_index` | `-1` |
| `source`, `agent_name`, `parent_conversation_id`, `battle_id`, `winning_conversation_id`, `group_id` | empty string |
| `not_fully_idle`, `killed`, `nesting_depth` | `0` |
| `raw_summary` | protobuf blob (nullable) |

Worse than the refused insert: `restore` had already written the conversation
db into `conversations/`, so the run left a conversation file with **no**
summaries row (that one test file was deleted by hand; nothing else was
touched).

What this revision does:

- **Fill from samples first, defaults second.** Both datetimes carry the same
  moment; its text copies the separator, fraction width and zone form of the
  table's own datetime rows (`_stamp_text`), and only an empty table falls back
  to the old ISO `Z`. `workspace_uris` becomes `["file://<workdir>"]` when the
  rows use a JSON array and a bare `file://<workdir>` when they do not;
  `status`, `project_id`, `app_data_dir` take the most common value among the
  rows. None of agy 1.4.4's values are hardcoded: they are copied, so a later
  agy that changes them changes the fresh row with them.
- **Check the row before touching `conversations/`.** `prepare_continue`
  builds the full row from the schema and up to 50 sample rows and refuses
  (`AgentError`) when a `NOT NULL` column is still NULL. No dry
  INSERT/`ROLLBACK`: that would take agy's write lock for a row we were not
  going to keep, and a rollback still bumps the file's change counter; the
  static check is the simpler option, and the failures it cannot see (a
  trigger, a `CHECK`) are covered by the next point.
- **Undo the restore when the insert still fails.** Any failure of
  `ensure_summary_row` (lock timeout, constraint, disk) withdraws the file
  this restore wrote: the missing-target case unlinks it, and an overwritten
  live copy is put back from `agy-backups/` through a temp file and
  `os.replace`. A file the restore only read (`used_live`, same fingerprint)
  is left alone. No 「有對話檔、沒有 summaries 列」 state is left behind.

### 2026-10-21 真機驗收：#26 的接續（#141）

The hand acceptance run of §18 (`agora continue --agent agy`, real agy 1.4.4,
real `~/.gemini/antigravity-cli`; before the run the conversation file and its summaries row were absent)
settled §12's open question:

- **The row is enough to load the restored conversation.** agy opened the
  restored conversation and the sentence from the original one was visible.
  Whether `jetbox_summaries.pb` / `agyhub_summaries.pb` hold an entry for it was
  not checked.
- **A normally exited agy leaves no sidecars.** After the continue's agy
  exited normally, `conversations/` held only `<id>.db` - no `-wal`, no
  `-shm`. The restore's refusal of *any* sidecar therefore does not get in the
  way of a normal continue; the refusal and its size-based message (an empty
  `-wal` may be deleted by hand while agy is not running, one with bytes may
  not) stay as the guard against a live agy.
- **agy rewrites the summaries row when it uses it.** The row agora inserts and
  the row agy leaves behind differ:

  | column | agora's row | after agy used it |
  | --- | --- | --- |
  | `status` | `''` (empty string) | `CASCADE_RUN_STATUS_IDLE` |
  | `raw_summary` | `NULL` | a protobuf blob |
  | `last_user_input_step_index` | `-1` (the column default) | `2` (agy's own value) |
  | `step_count`, `last_modified_time`, `last_user_input_time` | computed by agora from the steps and the table's samples | rewritten by agy |

  (In that run agora's row carried `project_id` `default-cli-project` and
  `app_data_dir` `antigravity-cli`; only `status` stayed empty. Where those
  values came from was not traced.)

  So the row is an entry ticket, not a format agy keeps: it accepts ours and
  then writes its own. The adapter never has to imitate agy's protobuf.

**Still unverified after this run:**

- **The shapes an empty table has no sample for.** The real row's datetime
  text and JSON-array `workspace_uris` are now known (above), but a brand-new
  list has no row to copy: the adapter then writes ISO 8601 UTC `Z` and a bare
  `file://<workdir>`, still a guess rather than an observation. How agy parses
  those two fallbacks is unverified; that agy rewrites the row once it uses the
  conversation is now known (above).
- **Whether agy renames or rekeys a conversation when it is continued**
  (review #26 finding 7). `collect` treats a vanished db as an out-loud
  error saying the id may have changed; it does not guess a new id.
- **The `jetbox_summaries.pb` / `agyhub_summaries.pb` indexes** - what they
  contain is still unknown, and so is whether the restored conversation has an
  entry there; the 2026-10-21 run did not look.

**Known limit, not fixed: TOCTOU between the `-wal`/`-shm` check and the
replace.** `restore` checks the sidecars and then `os.replace`s the file; agy
starting in that window would hold a connection to the inode that was just
replaced, while a new `-wal` keeps the path. Detecting the agy process
(`pgrep`/`lsof`) is left for later (cross-platform and false-positive work),
and no lock exists that agy also takes, so the window can only be narrowed -
it is not eliminated and this document says so rather than implying safety.

Everything this repository's tests do is against a temporary fake
`AGORA_AGY_HOME` and the fake CLI in `tests/fakes/fake_agy.py`: no real agy is
started, no real `conversation_summaries.db` is written, and the user's agy
home is never read or written by the test suite.

## 13. The opt-in gate (history: removed 2026-10-21)

`agora continue --agent agy` was **off by default** until 2026-10-21: it writes the user's real
`~/.gemini/antigravity-cli` (the restored conversation file and a `conversation_summaries.db` row),
and whether the real agy loaded what we write was not verified (§12). Without `AGORA_AGY_CONTINUE=1`
the command refused before a sync or any write, and `AgyAgent.start_native` refused too.

The hand acceptance run of 2026-10-21 (§12, §18 of `docs/acceptance.md`) verified the load, the
sidecars and agy's rewrite of the row, so the gate and its environment variable were removed in
#141: the continue runs like any other agent's now. Import, listing, search and preview were never
gated.

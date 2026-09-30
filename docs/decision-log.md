# AiStorage grill log (Step 2, 2026-09-23)

> This log is kept in English, but **canonical terms are the ones in `CONTEXT.md`**. Mapping: Session (not ChatSession), 接續 / 交接單 (not handoff / handoff note), 原始紀錄 (not raw record), 收件匣 (not inbox), 提交流程 (code name `committer`). Where this log and `docs/adr/` or `openspec/` disagree, those win; later decisions are recorded there (e.g. ADR 0005/0006, erase = owner only, revocation per profile, Claude Code manual import in phase 1).

Decisions confirmed by the user, in order. Terms are defined in CONTEXT.md.

## Step 1 carry-over
- 2026-09-06 MyBrain rule ("six modules abolished; Harness/LLMGateway not modules") is deliberately overturned. Harness prompt/skill/tool → Atelier; MCP and environment install → MLP profile; LLMGateway fully independent (sub2api).
- AiEntry and AiContainer still must not depend on each other. Handoff = the phone AI hands a Session (via Agora) to an AI in AiContainer.
- Privacy / model routing (tags) belongs to the future LLMGateway design.
- After the discussion, open a MyBrain PR updating related notes.

## Agora
- Q1: Two relation kinds between Sessions: 接續 and 參考. A new Session may continue or reference multiple earlier Sessions.
- Q2: 參考 = passive read of the other Session's record in Agora. Sessions sync to the central store periodically. Session status: 運作中 / 停止中.
- Q3: 接續 forks (the earlier Session is unaffected); maintenance under heavy branching must be considered.
- Q5: 案件 lives only in MyBrain; Sessions carry 所屬案件 (may be empty). The branch-tidying AI (收斂 AI) is phase 2.
- Q6: Store 原始紀錄 (true copy, the origin app's own export unit) plus a derived common-format 閱讀版.
- Q7: Everything in this infra is personal; the user's day job never runs here → collect all Sessions.
- Q8: Periodic sync, plus a sync right before a 接續 (initiated by the side holding the Session). 接續點 = the last synced position. 參考 reads the latest synced version.
- Q9: The AI that initiates a 接續 writes a 交接單, stored with the Session Link. General per-Session 摘要 is phase 2.
- Q10: Agora = storage entity + an extensible search interface. MVP: filter (所屬案件, origin app, time, title, status) + full-text search. Semantic search is phase 2 (with the QMD trial).
- Q12/Q13: The AI may also rewrite raw records. Every rewrite keeps old versions and a change log (rollback possible) and must not change positions (接續點 stays valid). Exception: 抹除 truly deletes content and keeps only who/when/why/which part.

## Cross-cutting
- Q11: The four elements are independent (each has its own storage entity and interface). AiStorage only defines 共通約定. Storage entity selection is deferred to basic design.
- Q23/Q24: Every 項目 = metadata + 本體 (the 本體 holds the external link when the item is a link). Minimal common metadata: id (immutable), 型態, 產生者, 時間, 所屬案件, 出處. Each element may add fields. A MyBrain 案件 = a topic file; add an immutable id to its frontmatter (part of the final MyBrain PR).
- Q25/Q26 (ideal model): Identity = profile ("department"). MLP attests which profile an executor belongs to. Each element authorizes by profile and holds its own rules. Executors only prove who they are. 職務 never participates in authorization. The phone App and sync programs are also profiles registered in MLP. 產生者 is stamped by the interface from the authenticated identity.

## Atelier
- Q14: know = background and rules; do = skill + tool definitions; judge = acceptance criteria and validation scripts; dont = behavioral conventions (not a security boundary). memory is Agora + MyBrain's job.
- Q15/Q16: Atelier serves AiContainer employees. The secretary's harness stays built into the app (to be re-examined later). Mac-local and LearnGhAgent consumers are expansion.
- Q17: The unit is the 職務 (know/do/judge/dont + 能力需求). The secretary picks the 職務 at handoff; the employee loads it at start. Several 職務 may share one profile.
- Q18: The profile installs things and lists 能力; the 職務 declares 能力需求 and enables only those; a 職務 may only be dispatched to a profile that has all of them.
- Q19/Q20: External skills (third-party, or owned by another system such as mybrain-*) are copied into Atelier as 外部副本 with source + version. They are periodically compared with upstream by content; drift → propose update; upstream gone → propose retirement; updates must pass judge.
- Q21: Employees may modify Atelier directly; changes take effect only after judge validation; every version is kept for rollback; each Session records the 職務 version it loaded.

## Foundry
- Q22: Centralized management, distributed storage. A Foundry 產出目錄 registers every 產出, with known output locations auto-collected and dead links checked. 原處產出 stay in their project repo (Foundry records the source); 收容產出 (slides, images, video, one-off reports) are stored in Foundry. Phase 2 may add snapshots of important outputs.

## Non-functional
- Q27: Agora and Foundry keep everything permanently by default; 抹除 is the only way to remove.
- Q28: AiStorage must not live on AiContainer machines. Storage entities sit somewhere in the cloud that is always reachable (Drive / bucket class), so the secretary can still sync and reference when home is offline.
- Q29: No bulk backfill; only Sessions created after launch are collected. Special old Sessions are imported one by one by an AI agent on request, so single-Session manual import must be supported. No stopgap for Claude Code's 30-day cleanup (the user declined).

## Phasing (Q30, approved)
> Superseded by the Step 3 re-scope (2026-09-26) at the end of this log: phase 1 is now the Session topology verification, and there is no phase 2/3 planning (flat backlog).
Phase 1 = the minimum set to make the handoff scenario work, plus extension points.
- Common: item = metadata + body; six common fields; immutable id. Identity = profile; authorize by profile; interface stamps the producer. Cloud, always reachable, permanent, not living on AiContainer.
- Agora: sync for the phone App + the single agent type employees use (periodic + before a 接續). 原始紀錄 + 閱讀版. Status. Session Link (接續/參考), 接續點, 交接單. Search: filter + full-text. Rewrite with rollback and 抹除 (prefer storage-native versioning). Manual single-Session import.
- Atelier: 職務 (know/do/judge/dont + 能力需求), at least one usable 職務. Check 能力需求 against the profile's 能力 list. 職務 versions; each Session records the version it used; AI may modify, gated by judge, with rollback. 外部副本 with source/version; upstream comparison triggered manually.
- Foundry: 產出目錄 + storage for 收容產出. Employees register 原處產出 explicitly.
- MyBrain: add immutable ids to case files (in the final MyBrain PR). Employees get read-only access (a fine-grained read-only token is possible on the free plan). Writes keep going through the existing /mybrain-write flow.

Phase 2:
- Mac-local Claude Code / opencode / agy / codex sync.
- Summaries, the convergence AI, semantic search.
- Automatic periodic comparison of external copies.
- Foundry auto-collection and dead-link checks.
- Employees writing MyBrain (needs a solution for the hard ban without paid GitHub).

Phase 3:
- Moving the secretary's harness into Atelier.
- Mac-local and LearnGhAgent as Atelier consumers.
- Snapshots of important outputs.

Extension points reserved in phase 1:
1. metadata accepts extra fields (LLMGateway privacy tags go here later).
2. Session Link types are extensible.
3. The search interface backend is swappable or stackable (full-text → semantic).
4. Derived layers stack (reading copy → summary → …) and all rebuild from the raw record.
5. One sync adapter per source app.
6. Identity granularity is the profile from day one.

External prerequisites (other projects):
- AiContainer: install one agent in the worker image. Workers are disposable, so an employee's Sessions must finish syncing before the worker is deleted.
- AiEntry: sync, plus handoff (write the handoff note, pick the 職務).

## Q31
- Phase-1 employee agent: opencode (so the phase-1 Agora sync adapters are phone App + opencode).
- ADRs 0001–0004 written in docs/adr/.

## Constraints stated by the user
- No paid GitHub plan.
- Existing storage subscriptions: Google Drive (5 TB, monthly flat fee); AWS / Linode buckets (pay per use).
- Worried the initial scope is too big. Clarify all requirements first, then implement in phases, but always leave extension points so complex mechanisms can grow later.
- What was given is the ideal model; overall implementation complexity is still unknown.

## Open for basic design
- Storage entity per element (Drive / bucket / GitHub / ...).
- How the ideal identity model is realized without a paid GitHub plan. The MyBrain hard ban (no push to main, no merge) cannot come from GitHub-native permissions on a free private repo.
- Whether a resident-facing interface needs a running service (the user's stance: "a VPS is not for running services"; exceptions only for services exposed to themselves).

## Step 3 re-scope (2026-09-26)

Confirmed by the user during the phase-1 spike handoff. Canonical records: proposal / design (D3, D5, D6, D7, D9, D10) / ADR 0007 / `docs/backlog.md`. Some points below were refined by the next section (e.g. the container mounts a credential whitelist, not only the profile's credentials).

- Purpose restated: AiStorage **externalizes the system's state** (what the AI system should remember to work across AIs, devices and time), not a backup of the state that apps already hold. Executors are stateless places where AIs work.
- Phase 1 goal redefined: verify that the work system supports Session **splitting (1→n, 分岔)**, **merging (n→1, 收斂)** and **mutual reference (n↔m, 參考)**, mapping to splitting work, aggregating results, and supporting each other's progress.
- The AI in phase 1 is **opencode on the Mac** (not the secretary, not an employee). Workers cannot run an agent yet. It runs in a container that mounts only its profile's credentials (the Mac holds the user's own credentials).
- Phase 1 scope: Agora + common conventions + minimal Foundry (catalog, files up to 100 MB through the committer; multi-GB path deferred) + 所屬案件 as an opaque MyBrain id string.
- Atelier: requirement design only (which departments/profiles and job functions/職務 are needed). No basic design; implementation waits for the MyLinuxPool profile redesign (another line of work).
- Read/write separation (ADR 0007): one read interface per element; readers may specify freshness; if unmet, return the latest committed content with a warning and the snapshot time; reads never trigger writes. Write-side mechanisms are chosen by cost and added over time; phase 1 ships the cheapest set (periodic sync, scheduled commit, writer-initiated sync-and-commit).
- Identity in phase 1: profile credentials installed manually by the user; "MyLinuxPool attests the profile" moves to the backlog, recorded as tickets in `docs/tickets/mylinuxpool.md` for the MLP side.
- Manual single-Session import in phase 1: opencode + Claude Code.
- Spike changes: drop the large-file item (old 1.7, moved to backlog); add an `opencode export` feasibility / position-stability item; 1.4 also tests that a client cannot write into another client's inbox.
- After phase 1 there is no further phase planning; everything else is a flat backlog (`docs/backlog.md`).

## Step 3 review adoption (2026-09-26)

After an architect review of the re-scoped documents, the user confirmed:
- 接續 is created through a 交接單 item that a new Session 認領s (claims); a 交接單 can be claimed once. 統合 = each branch holder hands off its end with its own 交接單, and the new Session claims all of them. No reader ever triggers someone else's write (design D10).
- The committer's full-Drive credential belongs to a dedicated Google account (family-sharing member, same 5 TB quota), already created by the user, so it cannot touch the user's personal Drive.
- Terms: 住民 includes the phase-1 opencode; the phase-1 profile is "Mac opencode" (`mac-opencode`); 同步器 is the canonical term (上傳器 avoided); new terms 分裂, 統合, 相互參照, 同步並提交, 寫入機制, 認領.
- All other review findings adopted: one container per Session in E2E; spike items widened (drive.file folder/same-name behavior, Drive revisions and trash in erase, concurrency cancellation, synthetic-volume timing, opencode session id / stop signal / sub-sessions, annex object reads); admin ops serialized with the committer; stop status only by explicit declaration; per-profile read identities; incremental read-view publishing; reserved role fields; arm64/amd64.

## Spike 1.4f decision (2026-09-26)

Spike 1.4 showed that a `drive.file` client from another project can create files in any folder whose id it knows (including the repo prefix), so same-name GITMANIFEST/GITBUNDLE injection can break clones or substitute forged history. The user decided:
- One Google account per profile is not feasible. Workers share one GCP project and one OAuth client; the committer uses a separate project.
- The user accepts the residual risks of countermeasure set A (detection-based, with a content-hash tip pin, a two-phase pin, a full-ref check, recursive sweep and quarantine, and inbox signing): (1) residents can inject files or cancel runs to make commits fail (detectable; no loss or silent tampering); (2) the ability to write into the repo folder still exists — a deliberate exception to "forbidden capabilities must not exist", to be recorded in a new ADR 0008; (3) Drive quota (shared family 5 TB) can be exhausted, caught only by monitoring; (4) workers under the shared client can delete each other's inbox items; the syncer re-uploads anything not committed.
- Inbox signing with per-profile keys is mandatory (the producer stamp no longer relies on inbox isolation; ADR 0006 to be revised).
- 1.4f counts as go only after review-1.4f2 H1/H2 are fixed and re-tested within the spike.

## Spike 1.9 go decision (2026-09-27)

After reviewing `docs/spike/report.md`, the user decided:
- **go-with-design-changes.** The design changes in report section 3 are written back into design, ADRs, and tasks and reviewed before group 2 starts; spike resources are cleaned up at this point.
- Q2: all workers share one read identity (one service account). Revoking read access means rotating that shared key, which affects every worker.
- Q3: the syncer decides whether to upload by comparing against Agora (the read view), not only its own local record. It uploads when Agora lacks the Session or has an older version. Items already uploaded but not yet committed count as pending and are not re-uploaded; if they are still missing after the next commit, the syncer uploads them again. This protects against other workers under the shared client deleting inbox items, without adding Actions minutes.
- Q5: the commit schedule interval is configurable, defaulting to every 6 hours, and the commit pipeline can also be triggered manually. Actions minutes come from the user's GitHub account (2,000/month free, shared with MyLinuxPool); estimated 276–372 minutes/month, so monitor actual usage.
- 1.7j (clock after sleep/wake) stays open and does not block go; it will be re-tested with the user later.

## Implementation language (2026-09-27)

The user chose **Python 3.12** for the committer, syncers, the read interface (library and CLI), and admin scripts. The opencode plugin (session id, claim, stop declaration) is TypeScript because opencode requires it. The phone client (MyAiEntry) will later get its own TypeScript client that follows the same index format and query spec. Reasons: all four REQ criteria are met (git-annex/rclone via subprocess, preinstalled on Actions runners and available in Ubuntu containers, FTS5 trigram in the standard library with Ubuntu 24.04's SQLite 3.45, arm64 and amd64), and the spike's Drive probe and sweep scripts were already Python.

## Group 3 decisions (2026-09-27)

- **Rewrite dropped from phase 1.** The user has no use case for editing Session content inside Agora. Source-side edits (/rewind, /undo) already arrive as new raw versions and keep history; content removal uses erase. Rewrite proposals in the inbox are rejected (`rewrite_not_supported`); the feature is in `docs/backlog.md`.
- Converters: Claude Code sidechain records stay out of the parent reading (the raw record keeps them); `compact_boundary` maps to a compaction marker; unknown record types become visible text instead of failing the reading. Non-`data:` attachments become text notes; hashes are never fabricated.
- Manual import: provenance defaults to `manual-import` (no local paths), no `--role` option.

## Overnight run (2026-09-27 night)

- Scope: finish groups 3–9 using test resources only (test Drive folder `aistorage-test`, test pin repo, test GitHub repo). Production deployment (production folders, `RCLONE_CONF` in MyAiStorage, enabling the production workflow) waits for the user's review.
- The resident AI in E2E containers uses whatever model is available in the teammate priority order, never Claude.
- Commits stay on `phase1-spike`; no push or merge. After the user's morning review and OK, the PM merges to main and pushes.
- The MyLinuxPool tickets (8.2) are filed as a GitHub issue in the MyLinuxPool repo for the user to judge.

## Raw record storage (2026-09-28, tasks 2.6)

The PM chose **raw records as git-annex objects** (`annex.largefiles` includes the raw exports) with `annex.max-git-bundles=10`, per `docs/spike/evidence/2.6-raw-storage.md`: push 26 s vs 39 s, fresh clone 1.3 s vs 14.8 s, active bundles 0.4 MB vs 10 MB. The cost is about 4.5× remote storage (every synced version is a full object), which counts against the shared 5 TB quota and is watched by the health check. Listed for the user's morning review.

## Entity-first redesign (2026-09-28 evening)

The user found the pipeline-centred design hard to follow and restated AiStorage as four independent entities (MyBrain, Agora, Foundry, Atelier), each with its own storage and read/write interface, referencing each other only through ids in metadata. This matches ADR 0001; D1 and D2 had drifted from it. Decisions:
- **ADR 0009**: each entity owns its storage and write path. Only Agora keeps an inbox, committer and pin (its write gate). MyBrain and Atelier are GitHub repos written through PRs. Foundry becomes a Google Drive shared folder (AI may edit; anything that must not change goes to GitHub) plus GitHub repos for code (Foundry only records provenance). The shared folder is isolated by sharing it, editable, with a separate resident-only Google account; a root folder id alone is not enforced by Google. The git-annex Foundry built earlier in phase 1 was removed.
- **Phase 1 narrowed** to Agora, the common conventions and MyBrain references. New Foundry, the resident account and the Atelier repo are in the backlog.
- **ADR 0010 and `docs/design/agora-session-operations.md`**: Agora builds the new session instead of the AI claiming a handoff. `agora` offers find, show, read, handoff and checkout and depends on no coding agent. Checkout accepts a handoff or any `session@message` as a starting point and writes a context package holding the original records verbatim; per-agent adapters named `agora-<name>` load it as a native session. Whether an agent may be launched is decided by the machine, not by Agora, so AI may launch agents too. Same-agent continuation keeps the request prefix byte-identical so provider prompt caches hit (verified in `docs/spike/session-import.md`).
- **Raw records for checkout** are read straight from Agora's annex objects (the read-only identity gets read access to the object folder and verifies each object against the hash in its key), not published a second time into the read view.

## Reader access to Agora raw records (2026-09-28 night)

For `agora checkout` to rebuild prefixes verbatim, the read-only identity (whose key is in every resident container) can read the whole Agora prefix folder, including raw records and git bundles. Raw records carry the model's reasoning and full tool output, which the reading version trims, so an accidentally printed secret in a tool output becomes readable by every resident. The user accepted this boundary (review 73dbf2c-8e3be02 M4). The deploy runbook and resident docs state it, and "do not print secrets into tool output" stays a resident rule.

## Night decisions (2026-09-30, confirmed by the user the same morning)

Made under the user's night rule (pick the conservative, reversible option; record for morning review).
- **Pinning others' snapshots (review 2bc0785 M2):** any resident may start from any completed message in any existing snapshot of any session, which pins that snapshot and publishes its reading. This is accepted because it widens nothing beyond the reader boundary already accepted (readers can read the whole Agora prefix). Cost is bounded instead: each profile may file at most a fixed number of reservations/continuations per committer round (configurable), and a reserved session that never gets a follow-up snapshot shows as `reserved` and expires. *Confirmed 2026-09-30.*
- **Continuation targets:** like handoffs, only main sessions can be continued; sub-sessions are rejected. *Confirmed 2026-09-30.*
- **One link per (new session, source session):** the same rule for claims and continuations; checkout rejects a start-point set that names the same source twice. *Confirmed 2026-09-30.*

## Night decisions on how a reservation is shown (2026-09-30, review 2bc0785 follow-up)

Follow-up to the night decisions above, also made under the night rule.
- **`reserved_until` is a display and administration signal, not a deletion trigger.** Phase 1 does **not** delete expired reservations automatically: deleting an item from the true copy is an administration operation (`admin erase` / rollback) and belongs to the user. `agora show` reports `expired` so an abandoned reservation is visible; the deadline sits in the search index (`sessions.reserved_until`, nullable, appended last so older index generations read it as NULL) and in `agora find --status reserved`.
- **The per-round link cap is counted in the writer, not in the true copy.** "One round" is one `AgoraStore` instance, which is one committer round (ADR 0009), so the count resets every round and a profile is never permanently locked out. It is deliberately *not* written to the true copy: a write-mechanism limit is not content, and recording it in the true copy would add a manifest/pin rule (ADR 0008) whose own shape is still unconfirmed. A mid-round crash under-counts, which is the conservative direction (those items are already in the true copy and get blocked as idempotent retries). *If this cap is ever wanted as a durable, verifiable limit, that is a separate decision about writing it into the true copy.*
- **A reserved session is a third status, not "running".** A reservation is a zero-message empty record nobody has picked up; `agora find --status reserved` lists them separately from real work, and the reservation markers disappear as soon as the session's first real snapshot arrives.
- **Reserved id format rule (review L):** a profile may only reserve an id it will actually create — the one `agora checkout` generates (`<source>:ses_` + 16 ULID characters) or one the caller passes with `--new-session-id` for an agent that will really use it. Reserving someone else's id is possible in principle (a reserved id collides forever once the real holder first uploads it) and phase 1 does not clean it up, so retries must reuse the same reservation rather than minting a new id.
- **Link cap is cumulative (review 1926cd3-142fd04 M3):** the per-round cap only rate-limits, since residents trigger rounds. It is replaced by a cap on each profile's *open* reservations (status `reserved` with no follow-up snapshot), counted from the true store, default 20; the config accepts positive integers only. *Confirmed 2026-09-30.*

## Night decisions on what the sweep may leave in the canonical prefix (2026-09-30, review 1926cd3-142fd04 H1/M1)

Follow-up to the review of 1926cd3 (night rule: pick the conservative, reversible option and record it for morning review).
- **A second same-named manifest is an injection when the official one is present (H1).** ~~rclone rewrites `GITMANIFEST--<uuid>` in place, so a healthy prefix never has two files with that name.~~ *(2026-09-30 更正：這個前提是錯的——rclone 每輪 push 都重寫 manifest 且 file id 會變，而且同一輪 push 內可能留下兩份位元組相同的主 manifest，實測見下一節。位元組**相同**的那一份因此改成不搬移、列入健康檢查；位元組**不同**的才照下面隔離。)* Once a file whose content hash equals the pin's official value is in the prefix, every other same-named main manifest with *different* content is quarantined without reading its content, and the same rule is applied to `.bak`. The old rule read the content, saw a manifest that parsed and whose bundles were all present, and held it — and because `verify_clone` required *exactly one* main manifest, the committer then aborted on every round forever. Any resident could do it, because the read identity can read the whole prefix.
- **Content evidence is bound to the pending pin (H1/M1).** With no KEEP candidate, a candidate manifest is only *held* when replaying its active bundles reproduces `pending.refs` — the record written before the push, which says what the remote is about to become. That is the same replay `settle` uses, so the two cannot disagree. Its active bundles inherit that backing.
- **No backing is not "hold" (H1/M1).** A manifest that parses, whose bundles are present, but that no pending explains is `NEED_ADMIN`: not moved (there is no evidence it is an injection, and moving it is what destroyed the canonical copy in impl1) and **reported by name** in the run report and by the health check, instead of an indefinite silent hold. Annex objects are held only when `pending.annex_keys` lists them.
- **HOLD has an age limit (M1).** It expires after `quarantine_retention_days` (7) and is then escalated to `NEED_ADMIN` and reported, so a stuck pending cannot turn the canonical prefix into a place residents park content without it being visible. *(2026-09-30 更正：逾齡不再自動隔離，改為升級成 NEED_ADMIN 並標成「可能是真本」——見下一節。)* Age is measured from Drive's `created_time`, which a writer cannot set.
- **The "self-consistent is not backed, therefore quarantine" rule only applies while the pin is in sync with the prefix** (deviation from a literal reading of the review, recorded deliberately). If no main manifest in the prefix has the official content hash, the pin has no authority over that prefix at all, and "not in the pin" is not evidence of injection — in impl1's end state (pending lost, remote ahead) those files *are* the new generation, and quarantining the new raw objects makes `init-pin` refuse to build a pin. Those files become `NEED_ADMIN` instead: nothing moves, the health check names them, and once the pin catches up (settle promotion or `init-pin`) the next round quarantines them normally. Files whose name lies about their own hash are quarantined either way: git-annex derives those names from the content, so a mismatch is fabricated regardless of where the pin is.
- **Settle refuses to drop a pending it could not confirm (M2).** Running out of re-checks while the prefix is still changing now aborts the round and keeps the pending, and the re-check fingerprint only covers the files the conclusion depends on (main manifest, `.bak`, referenced bundles) so residents cannot force re-check exhaustion by writing junk.
- **`verify_new_keys_on_drive` waits about 32 seconds** across five attempts (was 6): the observed Drive listing lag is 30 seconds, and a misjudgement leaves a pending nobody is responsible for. *(Superseded 2026-09-30: the implementation is 4 attempts of 10 s, i.e. checks at 0/10/30/60 s and about 60 seconds of waiting in total — see the next section.)*
- **Health check names the files.** `held_files` / `need_admin_files` are collected with the same `plan_sweep` the committer uses (no downloads), and anything older than one round is reported with its file names.
- **The erase runbook now records the `snapshots.jsonl` glob fix** (`sessions/*/` was missing a level, so past erases never rewrote the snapshot list). Verified as no-ops: the only erases that ran were the 1.3 spike on a throwaway prefix, which had no Agora layout; `python -m aistorage.admin erase` postdates them and has only run against test resources. Production has never been erased.

## Night decisions on which of two identical manifests is the real one (2026-09-30, review 903d7e2)

Follow-up to the review of 903d7e2 (night rule: pick the conservative, reversible option and record it for morning review). The review's H1 rest on a premise that turns out to be **wrong for this repo**, so the fix follows the measurements instead of the review text. The user was asked and chose this direction.
- **Measured first, on the integration sandbox (never the e2e prefix), with both rclone versions in play** (`tests/integration/test_committer_dup_manifest.py` now pins the result). rclone **rewrites `GITMANIFEST--<uuid>` on every push and its file id changes** (delete + recreate) — with rclone 1.75.1 (the version `committer.yml` pins) and 1.69.3 (what the Mac uses for e2e), and it matches what spike 1.2 §1 關鍵發現 3 and `1.3-verify/trash-all.json` recorded. So the official manifest's `createdTime` is **the last push's time, not the repo-init time**, and a byte-identical resident copy can easily be *older* than the official file. Both tie-breaks ("keep the earliest" and "keep the latest") are therefore winnable by a resident, and winning means the genuine file gets quarantined — after which the resident deletes their copy, the prefix has no manifest, `verify_clone` fails *before* the push, and the committer can never push again (mode 0 by hand). Also measured: a healthy prefix routinely holds **two byte-identical same-named manifests** (rclone writes the manifest twice within one push because Drive's listing lags), `git push` and `git clone` both succeed in that state, and the next push has rclone clean up the extra one itself.
- **Byte-identical duplicates of the official manifest are left alone** (`plan_sweep`): the earliest-created keeps KEEP, the rest become `NEED_ADMIN` with `created`/`modified` stamps in the reason and in the health check. Nothing is moved, because we cannot tell which one is genuine and moving the wrong one destroys the canonical copy. The pin's official value stays in the prefix either way, so the deviation a resident could trigger is gone.
- **`createdTime` decides duplicates where it is actually decidable** (`dedup_rank`, earliest wins, file id breaks ties): git bundles and annex objects (uploaded once per generation, so any copy is later) and a "new generation" same-named manifest (written by the last push, so any copy is later). A *fabricated* same-named manifest is still quarantined on content evidence alone. Known trade-off: if rclone ever leaves two identical copies of the same bundle, the newer one is quarantined every round (harmless — the survivor is byte-identical and content-addressed — but visible in `quarantined_files`); the measurement above only ever saw this for the manifest, not for bundles.
- **`verify_clone`, `precheck`, `verify_after_push` and `init-pin`'s remote re-read now require "one distinct content", not "exactly one file"** (`unique_manifest_sha`). A second same-named manifest with *different* content is still an injection and still aborts. This also removes a latent bug: the sweep used to have to quarantine rclone's own duplicate before these checks could pass.
- **Deviation gets an injection witness (M1).** A bundle or object whose `createdTime` is later than the prefix main manifest's last write is not part of any remote generation (the normal order is objects and bundles first, manifest last) and is quarantined even while the pin has no authority over the prefix. Ten minutes of slack (`INJECTION_SKEW_GRACE`) covers Drive's listing lag. Genuine new-generation files are created before the manifest write, so impl1's accident (quarantining the new raw objects) cannot happen this way.
- **Settle is no longer steerable during a pending (M2).** Main-manifest and `.bak` candidates, and the re-check fingerprint, ignore files created after `pending.written_at + 10 min` (`push_cutoff`), so a resident adding same-named files during the re-check window can no longer exhaust the re-check limit and stall every round at step 3 (before the sweep). Candidates that replay to `pending.refs` with different bytes are no longer a `MismatchError`: `createdTime` picks the genuine one (the resident's copy can only be made after the push), and the leftover is quarantined by the sweep once the pin is promoted. Fail-closed is kept where the ordering genuinely cannot be established. *(2026-09-30 更正：`createdTime` 決勝本身被證明是住民可以贏的——他只要早於 rclone 寫出 manifest 就能更早建立，而且他可以自己製造 pending 狀態。改成只認 pending 記載的 `expected_manifest_sha256`，缺欄位時 fail-closed，見下一節 H1。)*
- **An aged-out HOLD is escalated, not quarantined (L).** Past `quarantine_retention_days` the file becomes `NEED_ADMIN` marked "可能是真本" instead of being moved, because a pending stuck for a week means the file may well be the real new generation — auto-quarantining is exactly the step that broke the canonical copy in impl1. It is still reported by name.
- **`list_children` asks Drive for `orderBy=createdTime`** and `createdTime`/`modifiedTime` are already in the requested fields, so listing order is reproducible even though the decisions never rely on it.
- **`verify_new_keys_on_drive` waits about 60 seconds** across 4 attempts (checks at 0/10/30/60 s). The previous entry said "32 seconds / five attempts", which never matched the code; the code was right.
- **Not done, pending the user's decision:** binding the manifest's Drive file id into the pin (a post-push observation record, `PinState.manifest_file_id`). That is the only answer a resident cannot forge, and it is written up in `docs/backlog.md`. *(Decided by the next section: option 3 is not needed — the pin records a content hash instead.)*
- **Not done, needs a measurement first (M3):** whether rclone's `checkpresent` skips the upload when a same-named annex object already exists. A resident who knows their own raw's key could pre-plant it before step 8, and if rclone skips, the only copy on Drive is theirs to delete. This needs the integration sandbox, not the e2e prefix, and was left alone this round. *(Resolved by the next section: the step-8 upload-window check makes the question moot — a skipped upload aborts the round instead of quietly succeeding.)*

## Night decisions on which file is genuine when two of them are (2026-09-30, review 5d4dd52)

Follow-up to the review of 5d4dd52. That review confirmed the direction of the previous round (byte-identical copies are never moved, verify looks at the number of distinct contents) and closed the "copy identical content → steal KEEP → force a deviation" path, but found **two resident-triggerable stalls that both came from deciding the genuine file by "earliest `createdTime`"**. Both are fixed. Option 3 (binding the manifest's Drive file id) turned out to be unnecessary — see the last bullet.
- **`createdTime` decides duplicates only where it is actually decidable; a content hash decides the manifest (H1).** After a successful push the committer reads **its own** `.git/annex/git-remote-annex/<uuid>/manifest` — the copy git-remote-annex leaves behind — and that file's sha256 is byte-for-byte the manifest it just wrote to the remote (measured on both 1.75.1 and 1.69.3). It records that hash in the pending pin as **`expected_manifest_sha256`** (a second `write_pending` after the push), and `verify_after_push`, `settle` and `plan_sweep` then accept **only** that content. Two consequences: (a) a resident's byte-different variant can no longer block the promotion of a round (the old "all same-named manifests must have one content" rule let one variant stall *every* round — pending deferred a round, inbox items waited a round, and it could be repeated), and (b) a resident can no longer get their variant *promoted*: "earliest wins" was something they could reach by uploading before rclone writes the manifest, and winning meant their bytes became the pin's `manifest_sha256` while the real manifest was quarantined as a "differing same-named manifest"; deleting their own copy then left the prefix with no usable manifest and `git clone` failing permanently.
- **When the hash is missing, stop — do not guess (H1, fail-closed).** If the pending pin has no `expected_manifest_sha256` (that round was interrupted between the push and the second write, or the pin repo holds an old-format pending) and several candidates with different content all replay to `pending.refs`, settle aborts and keeps both the pending and the remote untouched. A stall is the residual risk already accepted in ADR 0008; a takeover is not. The run report and the health check both say when the hash could not be recorded (`expected_manifest=UNRECORDED`).
- **The injection witness in a deviation is taken from the pinned hash, and from `createdTime` (M1).** "The prefix main manifest's last write" now means the file whose content equals `expected_manifest_sha256`, read through its `createdTime` (rclone deletes and recreates the manifest on every push, so that is the write time and it cannot be edited afterwards). The old rule took the `modifiedTime` of the "earliest created" file — which residents control, since they can rewrite their own copy at will, so the witness was forgeable and every junk file became `NEED_ADMIN`.
- **The genuine annex object is the one inside this round's upload window, not the earliest (H2).** A resident knows their own raw's key the moment they submit a session, so they can pre-plant a byte-identical same-named object — and it is always the *earlier* one, so "earliest wins" kept the resident's copy, quarantined the one the committer actually uploaded, and deleting their own copy removed a key the pin had already recorded (`verify_pin_keys_on_drive` aborts every round, permanently; that raw is by then the canonical copy). The pending pin now records the **upload window** of step 8 (`upload_window_start/end`), and for keys that are new this round (`pending.annex_keys - state.annex_keys`) the keeper is the file whose `createdTime` falls inside it. Before step 8 the committer re-lists the prefix for exactly those keys and quarantines the same-named files that already exist (they cannot be legitimate: the key was created this round), and after step 8 it requires every new key to have a file inside the window. Out-of-window same-named files are quarantined; if **no** copy is inside the window, nothing is moved and the file becomes `NEED_ADMIN` with the window in the reason. *(2026-09-30 更正：窗內要的是「**恰好一份**」，不是「窗內最早的那一份」——住民在第 8 步之後補放一份就能贏，見下一節 M1 a。)*
- **That step-8 window check is the answer to M3.** review 903d7e2 left "does rclone's `checkpresent` skip the upload when a same-named object already exists" unmeasured, because if it skips, the only copy on Drive is the resident's. Isolating the pre-existing copies first means rclone has nothing to skip; and if it skips anyway (or Drive's listing has not caught up), there is no file inside the window and the round aborts at step 8 — before any pending is written. So a skipped upload can no longer be mistaken for a successful one.
- **Persistent injection is now a named warning with a single remedy (M2).** A resident who re-injects a differing same-named manifest between step 4 and step 5 can stall every round, and no pin field can prevent it: `git clone` finds files **by name**, and neither git-remote-annex nor rclone can clone by file id. That is ADR 0008's accepted residual risk (1) — detected, no loss or silent tampering — but a stall has to be visible. The health check gained `manifest_conflict`: warn while such a file is present, **fail** once it has survived a whole round (the sweep quarantines it every round, so surviving means somebody keeps putting it back). The remedy is written into the check, the health runbook and recovery runbook mode 4: stop the injection first (revoke that profile's write access to the prefix — its inbox permission or its signing key), and only then delete the file. Doing it in the other order just grows it again next round.
- **Option 3 is not needed; the pin records a content hash instead.** Binding the manifest's Drive file id into the pin was left "pending the user's decision" above. The review's experiment settled it: the id changes on **every** push (rclone deletes and recreates the manifest), so a healthy prefix legitimately holds two files with that name, and recording "which id was mine" still needs the content hash to tell them apart — the hash is the only thing that matters and it is the thing the committer can compute about itself. `docs/backlog.md` now marks option 3 as superseded by `expected_manifest_sha256`.

## Morning confirmations (2026-09-30)

- The night decisions above (pinning others' snapshots with a cumulative open-reservation cap of 20, reservations that expire only as a label, main-session-only continuation targets, one link per source) are confirmed. Main-session-only stays because it is already implemented; the user would relax it if it ever became costly.
- The sweep rules explained in the "Agora 清掃規則" page are confirmed: identical duplicate manifests are left in place and reported, unbacked files are not quarantined while the pin is out of sync with Drive, and continuous injection that pauses every round is accepted as ADR 0008 residual risk (health check alerts; revoke the profile's key).
- Integration tests against the test Drive run locally with one command; moving them to GitHub Actions later should only need a thin workflow that calls the same command.

## The last resident-winnable hole in the upload window, and self-healing from quarantine (2026-09-30, review-final)

The merge-gate review of d5869d7 + 2a9138e + f28dfc9 closed H1/H2 and confirmed the
committer can be merged, with M1 ("a resident can still win the upload-window keeper")
as a condition for enabling the workflow in production. Both halves of M1 are now done;
no trust-model change was needed for either.
- **The upload window must hold exactly one copy, not the earliest one (M1 a).** The H2
  rule was "the keeper is the copy created inside the window, earliest wins inside it",
  which a resident wins by uploading **after** step 8: the prefix has no same-named file
  when `plan_upload_exclusive` runs, and `verify_upload_window` only requires "at least
  one inside the window" — so a copy added between the upload and the promotion lands
  inside the window too, and being earlier it wins. Two copies inside the window now
  select **neither**: every one of them becomes `NEED_ADMIN` and `apply_sweep` moves
  nothing. The cost is that a resident can keep his own session from ever landing (it
  only affects him, and the canonical copy is not lost — both copies are still in the
  prefix); "the remote stalls" stays the accepted residual risk, "the pin loses an
  object" does not.
- **A pinned object that vanished is restored from the quarantine automatically (M1 b).**
  `verify_pin_keys_on_drive` now searches the quarantine for a file whose name, Drive
  `sha256Checksum` and `size` all match the key, moves it back to the prefix and
  continues the round. Content addressing is what makes this safe: all three agreeing
  means the bytes *are* the object the pin recorded, so the restore cannot put anything
  else into the canonical copy. Quarantine is a move, not a delete, and it is retained
  for 7 days, which covers the window in which a resident can keep re-running this. It
  is the automatic version of recovery runbook mode 0, so the whole class of "the real
  one was quarantined by mistake" — including the impl1 accident — now heals itself.
  Scope stays narrow: only keys the pin records, only from the quarantine, so an
  injection is never brought back. If the quarantine has nothing, the round still aborts
  and the message says so, because the quarantine is purged after 7 days and after that
  there is nothing left to recover. `dry_run` records what it would restore and moves
  nothing.
- **Self-healing is never silent.** The run report carries `restored_from_quarantine`
  (plus a `restored=[...]` log line and a count), and the health check gained
  `quarantined_pinned_keys`: a pinned key sitting in the quarantine with nothing in the
  prefix is normally impossible (the restore moves it), so seeing one means the restore
  has not run yet, or the **same** key is being quarantined over and over — which is
  the thing a human has to look at. Repeatable mis-quarantining is the impl1 shape.
- **Pinned (L).** `local_manifest_sha256` reads git-remote-annex's local cache path
  `.git/annex/git-remote-annex/<uuid>/manifest`, which is an implementation detail, not
  an interface. It is now pinned by an integration assertion that the local copy's sha256
  equals the remote copy's after a real push (`tests/integration/test_committer_dup_manifest.py`),
  and the note next to `GIT_ANNEX_VERSION` in `committer.yml` says to re-run that test on
  an upgrade instead of trusting the workflow to look green.

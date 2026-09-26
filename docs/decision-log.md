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

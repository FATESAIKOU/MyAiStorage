# Git-style versioned storage on consumer Google Drive: research notes

Researched 2026-09-23. Scope: the AiStorage "Agora" (raw chat JSON/JSONL, single files up to ~150 MB, a few GB, growing) and "Foundry" (outputs, multi-GB videos, catalog) stores. Both stores need git-style commit, rollback and clone, with the bytes living on consumer Google Drive (Google One 5 TB). Writers are Linux Docker workers, a Mac, and an Android Capacitor app. They may write concurrently.

Legend: **[doc]** = primary doc or README, **[code]** = source, **[issue]** = issue tracker, **[inferred]** = my conclusion from the cited facts. Please verify [inferred] claims before relying on them.

---

## 0. Drive facts that decide everything

| Fact | Source |
|---|---|
| Drive API v3 `files.update` has **no precondition parameter** (no If-Match, no generation match). The v3 File resource has **no `etag`**. It only exposes read-only `version`, `headRevisionId`, `md5Checksum`, `sha256Checksum`. So compare-and-swap is impossible: read, compare, then write is always racy. | [doc] https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update , https://developers.google.com/workspace/drive/api/reference/rest/v3/files |
| Drive **allows duplicate file names in one folder**. rclone: "Sometimes ... drive will duplicate a file that rclone uploads. Drive unlike all the other remotes can have duplicated files." Two writers creating `manifest` at the same moment can end up with two files. | [doc] https://rclone.org/drive/ (Duplicated files) |
| Trash is auto-purged after 30 days. "Delete forever" or "Empty trash" cannot be undone. rclone trashes by default, but `--drive-use-trash=false` and `rclone cleanup` delete permanently. | [doc] https://support.google.com/drive/answer/2375102 , https://rclone.org/drive/ |
| Revisions are kept "30 days or 100 revisions (whatever comes first)". At most 200 revisions per file can be marked `keepForever` (blob files only). rclone has `--drive-keep-revision-forever`. | [doc] https://developers.google.com/workspace/drive/api/guides/manage-revisions , https://rclone.org/drive/ |
| Rate limit: rclone reaches "about 2 files per second only". There is also an undocumented upload cap of 750 GiB per day. | [doc] https://rclone.org/drive/ (Limitations, `--drive-stop-on-upload-limit`) |
| **rclone's shared client_id "is being retired and will stop working during 2026"**. You must create your own. Google now requires a homepage URL and a privacy-policy URL to publish even a single-user app. | [doc] https://rclone.org/drive/ (Making your own client_id) |
| Apps in the OAuth "Testing" status get refresh tokens that **expire in 7 days**. Each client ID is limited to 100 refresh tokens per account, and the oldest token is silently revoked when a new one is issued. | [doc] https://developers.google.com/identity/protocols/oauth2 |
| `drive` is a *restricted* scope. `drive.file` is *non-sensitive*: it only reaches files the app created or files the user explicitly opened with it. | [doc] https://developers.google.com/workspace/drive/api/guides/api-specific-auth |
| Service accounts cannot own files on consumer My Drive ("Service Accounts do not have storage quota"). Consumer accounts have no shared drives, so every writer needs a user OAuth token. **Flag:** I found this only in third-party issue reports (n8n #26050, kopia #2656), not on a Google page. | [issue] https://github.com/n8n-io/n8n/issues/26050 |

**Conclusion [inferred]:** No tool can make Drive a safe *shared ref store* for concurrent writers. The mutable pointer (refs, manifest, HEAD) must either live on a server with atomic ref updates (a git host) or be written by exactly one process. Only immutable, content-addressed blobs are safe to put on Drive concurrently.

---

## 1. Candidates

### 1.1 git-remote-gcrypt with `gcrypt::rclone://remote:subdir`
- **Drive support:** Yes, through rclone. The README says "an experimental `rclone://` backend for early adoptors only (you have been warned)". The script prints "WARNING: rclone support is experimental." The backend was added in 2018 and has been touched once since (2021). [doc][code] https://github.com/spwhitton/git-remote-gcrypt (README.rst, `git-remote-gcrypt` lines 239-316, 890)
- **Storage model:** Encrypted packfiles plus a GPG-signed manifest holding the branch list and pack keys. rclone uploads with `rclone copyto` and deletes with `rclone delete`. [code]
- **Concurrency:** Unsafe. Known issue: "Every git push effectively has `--force`. Be sure to pull before pushing." Debian bug #877464 (open since 2017-10) documents that a concurrent push "silently clobbers the most recent commit" of the other repo. Joey Hess proposed a fix, but it is not implemented. The manifest is overwritten with no lock, so the last writer wins. [doc][issue] https://bugs.debian.org/877464
- **Large files:** It "can decide to repack the remote without warning ... your whole history has to be reuploaded. This push might fail over a poor link." Every file is a plain git blob, so 150 MB JSONL files get packed and encrypted whole. [doc]
- **Maintenance:** Last commit 2026-08-15. Last tagged release 1.5 (debian/1.5-1, 2022-08-21). GitHub issues are disabled; bugs go to the Debian BTS, where 2 are open.
- **Verdict:** Deal-breaker for concurrent writers (implicit force push, experimental backend).

### 1.2 datalad/git-remote-rclone (`rclone://<remote>/<path>`)
- **How it works:** The remote holds exactly two files, `refs` and `repo.7z`, a 7-Zip archive of a bare repo. Each push re-compacts the local mirror (`.git/rclone/<remote>`) and uploads the **whole archive**. It was "tested with rclone 1.50.2, Google Drive, DropBox". [doc] https://github.com/datalad/git-remote-rclone
- **Concurrency:** "At the moment no locking is performed that would prevent simultaneous (conflicting) updates". Issue #2 (locking) is still open. [doc][issue]
- **Large files:** Re-uploading a multi-GB 7z on every push is impractical for Foundry. [inferred]
- **Maintenance:** Abandoned. The only release is 0.1 and the last commit was 2020-02-24. Issue #7 (a crash when cloning an existing remote) and issue #9 ("Archive repo ...") are open.
- **Verdict:** Deal-breaker (abandoned, whole-repo upload, no locking).

### 1.3 git-annex with an rclone special remote (**recommended content layer**)
- **Two ways to connect:** (a) the external bash helper `git-annex-remote-rclone`: v0.8 released 2023-11-18, last commit 2026-06-20, 26 open issues. (b) The **built-in** `type=rclone` / `externaltype=rclone-builtin` remote, added in git-annex 10.20240430. It needs rclone ≥ 1.67.0 (`rclone gitannex`), and rclone's docs warn: "This command is very new and has not been tested on many rclone backends", so run `git annex testremote` first. [doc] https://git-annex.branchable.com/special_remotes/rclone/ , https://rclone.org/commands/rclone_gitannex/ , https://github.com/git-annex-remote-rclone/git-annex-remote-rclone , changelog https://hackage.haskell.org/package/git-annex-10.20260525/changelog
- **Model:** The git repo holds pointer files plus a `git-annex` branch of location logs. The branch is "designed to be auto-merged by simply concatenating" timestamped lines, so concurrent writers merge without conflicts. Content lives on Drive as **immutable, hash-named keys**. `annex.largefiles` (e.g. `largerthan=...`) decides per path whether a file goes to git or to the annex. [doc] https://git-annex.branchable.com/internals/ , https://git-annex.branchable.com/tips/largefiles/
- **Where git lives:** Anywhere, for example a GitHub private repo, since only pointers go there (see §1.7).
- **Large files:** Designed for them. Chunking is optional; smaller chunks resume better, larger chunks need fewer round trips. [doc]
- **Concurrency on Drive:** Uploads are write-once keys, so concurrent writers never overwrite each other. Duplicate uploads of the same key can create Drive duplicates; clean them with `rclone dedupe` [inferred from the Drive duplicate behavior]. For special remotes "that do not support locking", concurrent *drops* "may violate the numcopies setting. It still guarantees at least 1 copy is preserved". 10.20240430 fixed a bug with redundant concurrent transfers to the same repository. [doc] https://git-annex.branchable.com/git-annex-numcopies/
- **Drop and erase:** `git annex drop --from=gdrive` refuses unless numcopies can be verified elsewhere. `--force` bypasses that ("Data loss can result"). `git annex unused` / `dropunused` handle orphans. `git annex forget` rewrites the git-annex branch to discard location history. [doc] https://git-annex.branchable.com/git-annex-drop/ , https://git-annex.branchable.com/git-annex-forget/
  - A true erase takes all of these steps [inferred]: (1) drop the content from every remote and clone, (2) empty the Drive trash or use `--drive-use-trash=false`, (3) rewrite main-branch history with filter-repo if the file name or key (which contains the SHA-256 and size) must disappear too, (4) force-push as owner, (5) run `git annex forget`.
- **Known Drive problems:** Queries-per-minute 403 rate limiting made drops take about 5 minutes when using the shared client ID (git-annex-remote-rclone issue #70). Issue #78 reports a hang on `git annex push`. [issue]
- **Android:** git-annex runs only inside Termux (`pkg install git-annex`) or Nix-on-Droid, not inside a Capacitor WebView. [doc] https://git-annex.branchable.com/Android/ , https://git-annex.branchable.com/install/termux/
- **Maintenance:** Very active. Latest release is 10.20260901 (2026-09-01), with roughly monthly releases. [doc] https://git-annex.branchable.com/news/

### 1.3b git-remote-annex (`annex::`): the whole git repo *inside* a special remote on Drive
- New in git-annex 10.20240531 and "based on Michael Hanke's git-remote-datalad-annex". It stores `GITMANIFEST` plus incremental `GITBUNDLE` objects. It fully re-uploads on force push, on ref deletion, or when `annex-max-git-bundles` is exceeded. Old bundles are kept, so deleted or overwritten refs stay recoverable until someone pushes a deletion of all refs. [doc] https://git-annex.branchable.com/git-remote-annex/ , https://git-annex.branchable.com/internals/git-remote-annex/
- **Concurrency:** "when conflicting pushes are being done at the same time, for one of the pushes to be overwritten by the other one ... the overwritten push will appear to have succeeded". Encrypted special remotes cannot be cloned from. [doc] https://git-annex.branchable.com/tips/storing_a_git_repository_on_any_special_remote/
- **Verdict:** The best-engineered "everything on Drive" option. It is safe only with **one** pusher (for example a scheduled mirror job), so use it as a Drive-side backup of the git repo, not as the shared hub. [inferred]

### 1.4 Git LFS with a standalone custom transfer agent to Drive or rclone
- The mechanism is solid. `lfs.standalonetransferagent` makes git-lfs skip the LFS API server and speak JSON over stdin/stdout to an agent. git-lfs v3.8.0 was released 2026-08-28. [doc] https://github.com/git-lfs/git-lfs/blob/main/docs/custom-transfers.md
- **The agents are the problem:**
  - `sinbad/lfs-folderstore` is **archived** ("No longer maintained"). Last release v1.0.1 (2021-02-24).
  - `ffunatsu/git-lfs-agent-rclone` is **archived** (2025-01-14, "stopping development"). Its fork `yaito6502/git-lfs-agent-rclone` was last pushed 2025-02-09 and has 4 stars.
  - `regen100/lfs-dal` (OpenDAL, lists Google Drive) was last pushed 2024-09-16 and has 36 stars.
  - [doc] READMEs on GitHub, dates from the GitHub API.
- Deleting from these folder or rclone stores is manual; LFS has no drop or numcopies concept [inferred]. If git lives on GitHub, the free plan's LFS allowance is only 10 GiB storage plus 10 GiB bandwidth, which matters only if you fall back to GitHub LFS. [doc] https://docs.github.com/en/billing/concepts/product-billing/git-lfs
- **Verdict:** Immature or abandoned agents. It would work for write-once blobs, but you would be maintaining the agent yourself.

### 1.5 DVC with a Google Drive remote
- The storage is content-addressed (`files/md5/xx/...`) and immutable, so concurrent pushes of different objects don't conflict. Git metadata goes to the git host. [doc] https://doc.dvc.org/user-guide/project-structure/internal-files
- **OAuth:** Google **blocked DVC's default app** ("This app is blocked"). The DVC team says it cannot pass restricted-scope verification, and the workaround is your own GCP project (issue #10516, opened 2024-08, closed 2026-01-30). [doc][issue] https://doc.dvc.org/user-guide/data-management/remote-storage/google-drive , https://github.com/treeverse/dvc/issues/10516
- **Reliability:** Issue #10525, "corrupted cache with GDrive" (non-reproducible across machines), is open. [issue] https://github.com/treeverse/dvc/issues/10525
- **Erase:** `dvc gc --cloud` "is irreversible", and any writer holding the credentials can run it. [doc] https://doc.dvc.org/command-reference/gc
- **Maintenance:** DVC 3.67.1 (2026-03-31). The repo now redirects `iterative/dvc` to `treeverse/dvc`. `dvc-gdrive` was last committed 2025-12-11. It depends on PyDrive2, whose last release is 1.21.3 (2024-11-29) and last commit 2024-11-30. The GDrive path is therefore the weakest-maintained link. [doc][GitHub API]
- **Verdict:** Workable but second choice. It is ML-pipeline oriented, its Drive stack is stale, and its erase is all-or-nothing gc.

### 1.6 "git-remote-gdrive"-style helpers
- `cakemanny/git-remote-drive` (1★, last pushed 2024-12), `RB14/gitdrive` (1★, created 2026-02), `darkharasho/drive-git-remote` (0★, 2026-08), `jr-dragon/git-remote-gdrive` (0★, 2026-09), `mavilef/git-remote-gdrive` (2★). `Lykos153/git-annex-remote-gdrive` is archived (2018). Adjacent new tools `Red-Eyed/git-sfs` (4★) and `david-hoze/bit` (1★) take a "git metadata + rclone content" approach. [GitHub API, 2026-09-23]
- **Verdict:** Toys or weekend projects, and all of them run into the same missing-CAS problem. Do not use.

### 1.7 Hybrid: small git repo on GitHub plus large blobs on Drive (**recommended shape**)
- GitHub gives an **atomic, fast-forward-checked ref update**, which is the compare-and-swap Drive lacks. The REST "Update a reference" call has `force` default `false`, which ensures "you're not overwriting work". [doc] https://docs.github.com/en/rest/git/refs#update-a-reference
- **Limits:** A 50 MiB warning, a **100 MiB hard block** per file, and repos should stay under 1 GB (under 5 GB "strongly recommended"). Agora's 150 MB files therefore *must* go through annex, DVC or LFS. [doc] https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github
- **History protection:** **GitHub Free private repos get neither branch protection nor rulesets.** They need GitHub Pro, Team or Enterprise. Without them, any push token can force-push or delete branches. [doc] https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches , https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets
- **Erase on GitHub:** After filter-repo and a force push, old commits stay reachable "via their SHA-1 hashes in cached views" and through PRs until GitHub Support purges them. Support only helps with *sensitive* data. Deleting and recreating the private repo is the blunt alternative. [doc] https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
- **Search:** GitHub code search covers only the default branch and excludes files over 350 KiB. Keep normalized text copies chunked, or search locally with `git grep` or your own index. [doc] https://docs.github.com/en/search-github/github-code-search/about-github-code-search

### 1.8 Other Drive-backed options worth knowing (not git, but useful for the backup copy)
- `rclone serve restic --append-only` ("Disallow deletion of repository data") lets restic snapshots flow to Drive through a gateway that alone holds the Drive token. [doc] https://rclone.org/commands/rclone_serve_restic/
- `rclone sync|copy --backup-dir` moves files that would be overwritten or deleted into a side directory instead of losing them. `--immutable` refuses to update existing files. [doc] https://rclone.org/docs/

---

## 2. Comparison table

Ratings: ✅ good fit · ⚠️ possible with caveats · ❌ blocker.

| Requirement | gcrypt+rclone | datalad git-remote-rclone | **git-annex + rclone (git on GitHub)** | git-remote-annex (git on Drive) | LFS standalone agent | DVC gdrive |
|---|---|---|---|---|---|---|
| Every rewrite versioned, rollback | ✅ git | ✅ git | ✅ git + immutable keys | ✅ git (old bundles kept) | ✅ git + immutable objects | ✅ git + immutable objects |
| Concurrent writers | ❌ implicit `--force`, last writer wins | ❌ no lock, whole-archive overwrite | ✅ refs on GitHub (fast-forward check); blobs write-once | ❌ concurrent pushes overwrite silently | ✅ if git is on GitHub | ✅ if git is on GitHub |
| 150 MB files / multi-GB video | ❌ packed into git; full repacks | ❌ whole repo per push | ✅ designed for it; chunking | ⚠️ content OK; git bundles on Drive | ✅ | ✅ |
| Writers cannot destroy history | ❌ | ❌ | ⚠️ needs GitHub Pro protection **and** a second copy (numcopies ≥ 2) | ⚠️ single pusher only | ⚠️ same as hybrid, no numcopies | ⚠️ `gc --cloud` open to all writers |
| Owner "erase" | ⚠️ rewrite + repack | ⚠️ rewrite + re-upload | ✅ drop --force + forget (+ filter-repo) | ⚠️ must delete all refs to purge bundles | ⚠️ manual deletion on Drive | ⚠️ gc (coarse) |
| Android (no native CLI) | ❌ | ❌ | ⚠️ Termux only; otherwise inbox pattern (§3) | ❌ | ❌ | ❌ |
| Full-text search on normalized text | text in git | text in git | ✅ text in git (small files), annex for blobs | text in git | text in git | text in git |
| Maintenance | ⚠️ active core, rclone "experimental" since 2018, last release 2022 | ❌ dead since 2020 | ✅ git-annex monthly (10.20260901); helper v0.8 / built-in since 10.20240430 | ✅ same project, since 10.20240531 | ❌ agents archived or stale | ⚠️ DVC active; PyDrive2 stale since 2024-11 |
| Main risk | silent loss on concurrent push | abandonment, corruption | complexity; Drive rate limits (about 2 files/s) | silent overwrite | unmaintained glue | blocked OAuth app, open corruption issue |

---

## 3. Cross-cutting answers

**Concurrent writers without CAS.**
- gcrypt and datalad's helper use no lock at all: last writer wins, and gcrypt even ignores non-fast-forward.
- git-remote-annex also has no lock. It documents silent overwrite and keeps old bundles as a partial safety net.
- git-annex content transfers are safe because keys are immutable. Its numcopies logic admits that non-locking remotes can violate numcopies on concurrent drops, while still guaranteeing ≥ 1 copy.
- None of these tools implements Drive lock files. A lock file on Drive would itself be racy, because Drive has no If-Match and allows duplicate names [inferred].
- The only robust pattern is to keep refs on a real git server, or to serialize every ref write through one process.

**Android writing without git.** Yes, with an inbox pattern [inferred design, built on the facts below]:
1. The app authenticates with a non-sensitive `drive.file` OAuth client, which needs only basic verification and can reach only files the app created.
2. It uploads write-once, content-named files (for example `inbox/<device>/<sha256>.jsonl`) with resumable upload.
3. A Linux worker with full `drive` scope ingests them later: it verifies Drive's `sha256Checksum` / `md5Checksum`, runs `git annex add`, commits, pushes, and then moves or deletes the inbox item.

Supporting details:
- Alternatively, the app can make small commits directly through the GitHub Git Data API (refs update with `force=false` gives CAS), or through isomorphic-git (v1.42.2, 2026-09-11). isomorphic-git needs a CORS proxy for GitHub in a browser. Capacitor's native HTTP patch likely avoids that, but this is not stated in Capacitor's docs. Capacitor also warns that CapacitorHttp handles only string or JSON bodies on native and points to `@capacitor/file-transfer` for large files. [doc] https://github.com/isomorphic-git/isomorphic-git , https://capacitorjs.com/docs/apis/http
- **Flag:** It is unclear whether `drive.file` access is scoped per OAuth client or per GCP project. Test that the phone's client cannot see worker-created files.

**History protection when any Drive token can delete.**
- A consumer account has one owner and no append-only role. Any `drive`-scope token can trash, permanently delete, or empty trash, and revisions and trash last only 30 days.
- Mitigations:
  - (a) Give full-scope Drive tokens only to one or two trusted workers or a gateway. Give phones and less-trusted writers a `drive.file` client, or have them write only through the gateway.
  - (b) Keep a second copy outside what Drive writers can reach, enforced by git-annex numcopies ≥ 2. For example: a local disk or NAS on the Mac, a second provider, or restic via `rclone serve restic --append-only`.
  - (c) Keep several git clones (every clone is a full history backup) and add a scheduled single-writer mirror of the git repo, for example to a git-remote-annex remote on Drive or to another host.
  - (d) Pay for GitHub Pro to block force pushes and branch deletion on the private repo.
  - (e) Use rclone `--backup-dir` / `--immutable` in any sync job.
- Deletion of annexed content is detectable (`git annex fsck --from=gdrive`), and with numcopies ≥ 2 it is recoverable.

---

## 4. Recommendation and deal-breakers

**Recommended: hybrid (§1.7) with git-annex as the content layer (§1.3).**
- **Metadata repo:** A private git repo holds the catalog, pointer files, the `git-annex` branch, and the normalized text copies (small, chunked). Host it on GitHub, which needs Pro for force-push protection, or on a self-hosted bare repo with `receive.denyNonFastForwards` and `receive.denyDeletes` [inferred alternative].
- **Content on Drive:** Use git-annex's rclone special remote. Prefer the mature `git-annex-remote-rclone` helper, or the built-in one after `git annex testremote`. Use your **own published OAuth client**.
- **Backup copy:** Keep numcopies = 2 with a second, non-Drive copy.
- **Phone:** It writes to a Drive inbox via REST, and a worker commits.
- **Optional:** A single-writer git-remote-annex mirror of the git repo onto Drive.

DVC is the fallback if you would rather not use git-annex.

**Deal-breakers found:**
1. Drive v3 has no conditional writes and no etag, and it allows duplicate names. This rules out every "git remote on Drive" as a multi-writer hub: gcrypt, datalad git-remote-rclone, git-remote-annex, and the git-remote-gdrive toys.
2. gcrypt: every push is an implicit `--force` (Debian #877464, open since 2017), and the rclone backend is "experimental".
3. datalad git-remote-rclone: abandoned since 2020-02, re-uploads the whole repo as 7z on every push, has no locking, and has a clone crash bug.
4. LFS→Drive agents are archived or stale (lfs-folderstore, git-lfs-agent-rclone, lfs-dal).
5. DVC's default Google app is blocked, its PyDrive2 dependency is stale, and GDrive has an open corruption issue.
6. GitHub Free private repos have **no branch protection**, and GitHub blocks files over 100 MiB, so Agora's 150 MB files must be annexed.
7. rclone's shared Drive client_id stops working during 2026. Apps left in Testing status have 7-day refresh tokens.
8. Nothing on Drive alone meets "writers cannot destroy history": trash and revisions last 30 days, and any full-scope token can hard-delete. An off-Drive second copy is required.

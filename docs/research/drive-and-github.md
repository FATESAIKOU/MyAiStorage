# Google Drive (consumer Google One) and GitHub Free for AiStorage

Researched 2026-09-23. Sources are primary: Google for Developers (Drive API, Identity), Google Drive / Google One Help, Android Developers, GitHub Docs (including the `github/docs` source repo, read to resolve plan-gating text) and the rclone docs. **Unverified** means the docs are silent or ambiguous, so test before relying on it. **Inference** marks my reasoning, not something a doc states.

---

## TL;DR

| Question | Answer |
|---|---|
| Can a service account (SA) own files or use quota in consumer Drive? | **No.** "Service accounts don't have storage quota and can't own files." Consumer accounts can't create shared drives, which is the documented way around this. |
| Can an SA **create** files in a folder that a consumer user shared with it? | **No** (inference from the doc above: whoever creates a file owns it, and SAs can't own files). It **can** read, and as `writer` it can overwrite the content of files the user owns. **Unverified** for the SA case specifically. |
| Can a credential be restricted to one folder? | **Only by sharing.** Share the folder with a separate identity (an SA, or a second Google account). No OAuth scope means "this folder only". `drive.file` means "files this app created or the user picked", not a folder. |
| Can a `writer` in My Drive trash or permanently delete the owner's files? | **No.** "Only the file owner can trash a file." Permanent delete and empty trash are owner-only. A writer *can* remove items from a folder, overwrite content, and share (unless `writersCanShare=false`). |
| Can we stop an identity from deleting? | **Yes, if it isn't the owner.** Give it `writer` or `reader`. Any token acting **as the owner** (the user's OAuth refresh token) can permanently delete, empty trash and delete revisions. |
| Revision retention for non-Google files (JSON/JSONL count) | Kept 30 days, or purged earlier once there are 100 newer ones. `keepForever` protects **up to 200 revisions per file**, and those count toward quota. |
| Can revisions be deleted? | **Yes**, for binary/blob files, but not the last remaining revision. Which roles may delete one is **not documented** (probably writer, but unverified). |
| Does `fullText contains` search inside plain-text files? | **Yes** for recognized types ("text documents"). It matches whole tokens. **How much of a large (~150 MB) file gets indexed is undocumented**, and so is whether JSON counts as a "recognized" type. `indexableText` hints max out at 128 KB. |
| Upload limits | 5 TB max per file. 750 GB/day (the API doc words this for Workspace users; rclone says consumer accounts see it too). Resumable upload sessions last 1 week. |
| API quotas (projects created on or after 2026-05-01) | 1,000,000 quota units per minute per project, 325,000 per minute per user. list = 100 units, get = 5, update = 50, download = 200. Billing above daily thresholds is announced for "later in 2026" with 90 days' notice. |
| Android (Capacitor WebView) | OAuth **must not** run in the WebView. Use native `AuthorizationClient` (package name plus SHA-1 client) or the system browser. Access tokens last 1 hour. Google advises against storing refresh tokens on the device. |
| GitHub Free: branch protection or rulesets on **private** repos | **No.** Both are public-repo-only on Free. Private repos need Pro, Team or Enterprise. |
| Fine-grained PAT scoped to one repo with Contents read-only | **Yes.** Expiry can be 1–366 days, or none. |
| Can a Contents:write PAT push to main, force-push and merge? | **Yes, all three.** Merging a PR needs Contents:write, the same permission as push. Without branch protection nothing separates "create branch and open PR" from "push to main". |
| Can a GitHub App be denied merge? | **Only by denying Contents:write**, and then it can't push branches either. Installation tokens can be narrowed per request (repositories and permissions) and last 1 hour, but whoever holds the App's private key can mint the App's full permissions. |
| Actions (Free, private repos) | 2,000 min/month, 500 MB artifact storage, 10 GB cache per repo. Self-hosted runners are free. Environments are **not** available for private repos on Free. |
| Packages / GHCR | 500 MB storage, 1 GB/month transfer. Container registry storage and bandwidth are "currently free". |
| Git LFS (Free) | 10 GiB storage and 10 GiB bandwidth per month. Max file size 2 GB. |
| Normal git file limits | Warning at 50 MiB, **blocked above 100 MiB**. Repos ideally under 1 GB, and under 5 GB is strongly recommended. **Agora's ~150 MB JSONL files can't go in plain git.** |
| Tamper-proof history on a Free private repo | **No native prevention.** You can only detect: the activity view and API (`force_push`), `push` webhooks and events (`forced`). Immutable releases are tamper-*evident* only, because a Contents:write holder can delete the release and then its tag. |

**Bottom line.**

- **Drive.** The real least-privilege tool on consumer Drive is **"not the owner"**. Any credential that acts as your user (an OAuth refresh token, whatever the scope) can permanently destroy what it can see. A second identity with `writer` or `reader` on one folder (an SA or a second Google account) can't trash or delete your files, but it can overwrite them and unparent them. Revisions (30 days / 100 versions, `keepForever` up to 200) are the undo mechanism.
- **GitHub Free private.** Anything with Contents:write is effectively root over history. You can only get "PR-only" access indirectly, for example with a workflow_dispatch broker (§B.2), and you can only *detect* force-pushes, not prevent them.

---

## Part A: Google Drive on a consumer Google One account

### A1. Headless access (Docker worker, Mac job) and token scoping

**Service accounts**
- "Service accounts don't have storage quota and can't own files. Instead, they must upload files and folders into shared drives, or use OAuth 2.0 to upload items on behalf of a human user." ([Shared drives overview](https://developers.google.com/workspace/drive/api/guides/about-shareddrives); the same note appears under `storageQuotaExceeded` in [Resolve errors](https://developers.google.com/workspace/drive/api/guides/handle-errors))
- "Ownership transfers to service accounts will fail." ([Transfer file ownership](https://developers.google.com/workspace/drive/api/guides/transfer-file))
- Shared drives are "only available for work or school accounts", so a consumer account can't use the shared-drive escape hatch. ([Troubleshoot shared drives](https://support.google.com/a/users/answer/12382709?hl=en))
- Domain-wide delegation (an SA impersonating the user) is a Workspace Admin Console feature. rclone's SA instructions all go through the Workspace Admin Console, so it isn't available to consumer accounts. ([rclone Drive](https://rclone.org/drive/))
- For quota purposes, "API calls by a service account are considered to be using a single account." ([Usage limits](https://developers.google.com/workspace/drive/api/guides/limits))

**Sharing a folder to an SA (or to any second identity)**
- Storage: "Files in shared folders only consume the cloud storage quota of the original file owner, never the recipient. However, if you … add your own files into a folder someone shares with you, those new files use your storage." ([How your Google storage works](https://support.google.com/googleone/answer/9312312?hl=en))
- "You own the files that you create or upload on Google Drive." ([Transfer file ownership](https://developers.google.com/workspace/drive/api/guides/transfer-file))
  - **Inference:** an SA can't create new files in your folder, because it would own them and it has no quota.
  - It **can** upload a new version of an existing file you own. The Drive Help page says "If you upload a new version of a file owned by someone else, the original owner remains the same" ([Check activity & file versions](https://support.google.com/drive/answer/2409045?hl=en)), so that storage counts against *you*. **Unverified for SAs specifically.**
- An SA shared as **`reader`** on a folder gives a genuinely **read-only credential limited to that folder subtree**, since permissions inherit to children ([Share files](https://developers.google.com/workspace/drive/api/guides/manage-sharing)). This is the cleanest "read-only, one folder" option on consumer Drive. The SA key is a long-lived JSON credential.
- Option: expiring permissions. `expirationTime` can be up to 1 year, but "for folders, temporary access is only supported with the reader role." ([Share files](https://developers.google.com/workspace/drive/api/guides/manage-sharing))
- Alternative writer identity: a **second consumer Google account**.
  - Every account includes up to 15 GB free ([Google One storage](https://support.google.com/googleone/answer/9004014?hl=en)).
  - Files it creates in your folder are owned by it and use *its* quota. A Google One plan can be shared with up to 5 family members, whose files spill into the shared pool once their own 15 GB is full ([Share with family](https://support.google.com/googleone/answer/9004015?hl=en)).
  - Consumer-to-consumer ownership transfer works, but only after the new owner accepts (`pendingOwner=true`, then `role=owner` + `transferOwnership=true`), and it can be scripted from the Mac with the owner's token ([Transfer file ownership](https://developers.google.com/workspace/drive/api/guides/transfer-file)).

**OAuth refresh token for your own user** (the usual rclone setup)
- Refresh tokens stop working if you revoke them, if they go **unused for 6 months**, if the account exceeds its token limit (**100 refresh tokens per account per client ID**; the oldest is silently invalidated), or if you granted time-limited access that expired. ([OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2))
- **Testing-mode trap.** A project with an External consent screen in "Testing" status gets refresh tokens that **expire in 7 days** (unless it only requests profile/email scopes). ([OAuth 2.0 overview](https://developers.google.com/identity/protocols/oauth2))
- The `drive` and `drive.readonly` scopes are **restricted**. The "personal use" exception ("fewer than 100 users") lets you skip verification if you click through the unverified-app screen. The 100-user cap covers the project's lifetime. ([Verification exceptions](https://support.google.com/cloud/answer/13464323?hl=en))
  - rclone's docs recommend publishing the app to "In production", unverified, to avoid the weekly expiry. ([rclone Drive](https://rclone.org/drive/))
- Device flow (for a TV or headless box) supports **only** `drive.file` and `drive.appdata` among Drive scopes ([Limited-input device flow](https://developers.google.com/identity/protocols/oauth2/limited-input-device)). For full `drive` scope on a headless worker, authorize on a machine with a browser and copy the token over (`rclone authorize`).

**Scopes** ([Choose Drive API scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth))

| Scope | Class | Meaning |
|---|---|---|
| `drive.file` | non-sensitive | "Create new Drive files, or modify existing files, that you open with an app or that the user shares with an app while using the Google Picker API or the app's file picker." |
| `drive.appdata` | non-sensitive | The app's hidden config folder, which the user can't see in the Drive UI |
| `drive`, `drive.readonly`, `drive.metadata(.readonly)`, `drive.activity(.readonly)` | restricted | Everything |

- There is **no folder-scoped OAuth scope**, and no read-only variant of `drive.file`.
- A `drive.file` token can still `files.delete` (permanently delete) and `revisions.delete` the files it can see. Both methods accept `drive.file`. `files.emptyTrash` needs full `drive`. ([files.delete](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete), [revisions.delete](https://developers.google.com/workspace/drive/api/reference/rest/v3/revisions/delete), [files.emptyTrash](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/emptyTrash))
- rclone describes `drive.file` as "read/view/modify only those files and folders it creates". ([rclone Drive](https://rclone.org/drive/))
- **Unverified:**
  - Whether picking a *folder* in the Picker (`allow_folder_selection`) grants `drive.file` access to that folder's existing children. The docs only say "per-file".
  - Whether `drive.file` grants are shared across OAuth clients in the same Cloud project.

### A2. Roles and deletion in My Drive

From the [Roles and permissions](https://developers.google.com/workspace/drive/api/guides/ref-roles) table (My Drive-relevant rows):

| Operation | owner | writer | commenter | reader |
|---|---|---|---|---|
| Read content and metadata, list folder | Y | Y | Y | Y |
| Modify content and metadata | Y | Y | – | – |
| Access historical revisions | Y | Y | – | – |
| Add items to folder | Y | Y | – | – |
| Remove items from a My Drive folder | Y | Y | – | – |
| Share items from a My Drive folder | Y | Y (unless `writersCanShare=false`) | – | – |
| Move to trash / recover from trash | Y | **–** | – | – |
| Empty trash / delete a file or folder | Y | **–** | – | – |
| Add a content restriction (My Drive) | Y | Y | – | – |

- `fileOrganizer` and `organizer` are **shared-drive roles**, not useful for consumer My Drive.
- "Only the file owner can trash a file … If you attempt to trash a file you don't own, you receive an `insufficientFilePermissions` error." Trash auto-deletes after 30 days. ([Trash or delete files](https://developers.google.com/workspace/drive/api/guides/delete))
- `files.delete` "Permanently deletes a file owned by the user without moving it to the trash." ([files.delete](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/delete))
- **What a writer can still damage:**
  - It can overwrite content, which creates a new revision; older revisions survive the 30-day / 100-version window.
  - It can rename files and remove them from a folder. The file then still exists in the owner's Drive, but outside the tree.
  - It can re-share files.
- **Content restrictions** ([Protect file content](https://developers.google.com/workspace/drive/api/guides/content-restrictions)):
  - `contentRestrictions.readOnly` blocks title and content edits and "Revisions of a binary file" uploads.
  - With `ownerRestricted=true`, only the owner can lift it. This is a per-file "freeze" that a writer can't undo.
  - Google warns it "isn't a way to create an immutable record", and a writer can still move or re-share the file.

### A3. Revisions (non-Google "blob" files, including JSON/JSONL)

From [Manage file revisions](https://developers.google.com/workspace/drive/api/guides/manage-revisions) and [revisions resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/revisions):
- "Purgeable revisions are typically preserved for 30 days, but can be purged earlier if a file has 100 revisions that aren't designated as 'Keep Forever' and a new revision is uploaded." The head revision is never auto-purged.
- "Up to 200 revisions can be set to 'Keep Forever' and they count towards your storage limit." You can set this with `keepForever` on the revision, or with `keepRevisionForever` on upload or update. rclone has `--drive-keep-revision-forever`.
- `quotaBytesUsed` = head revision plus keep-forever revisions ([files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)). rclone says ordinary purgeable revisions "do not count towards a user storage quota" ([rclone Drive](https://rclone.org/drive/)).
- To read revision history, the user needs owner or writer (plus the shared-drive roles). Readers and commenters can't see revisions.
- Deletion: "You can only delete revisions for blob files … Revisions for other files, such as Google Docs or Sheets, and the last remaining revision of the binary file, can't be deleted." The docs **don't say which role can delete a revision.** The Help page's "Manage versions → Delete" is available in the UI. Assume a writer can, and **verify.**
- Oddity in the guide: "You can only download blob file content revisions marked as 'Keep Forever'." (**Verify.** This matters if you plan restore-by-download of an older revision.)

### A4. Full-text search

- `fullText contains 'x'` matches "the name, description, indexableText properties, or text in the file's content or metadata". It matches whole tokens only ("HelloWorld" won't match "Hello"). Double quotes give a phrase match, and `_` is treated as a space. ([Search query terms](https://developers.google.com/workspace/drive/api/guides/ref-search-terms))
- "Drive automatically indexes documents for search when it recognizes the file type, including text documents, PDFs, images with text…" ([Manage file metadata](https://developers.google.com/workspace/drive/api/guides/file-metadata))
- **Undocumented:**
  - Whether `application/json` or `.jsonl` is treated as a text document.
  - How many bytes of a large text file get indexed.
  - Indexing latency.
  - Don't design Agora search around Drive FTS. **Inference:** keep your own index (SQLite FTS or similar) or put a summary into `indexableText`.
- `contentHints.indexableText` is limited to **128 KB** and is indexed as HTML ([files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)).
- Custom properties give you catalog-like metadata you can filter on with `properties has {key='k' and value='v'}` ([Custom file properties](https://developers.google.com/workspace/drive/api/guides/properties)):
  - Max 100 per file, 30 public, 30 private per app.
  - **124 bytes per key+value.**
- Plain-text size note from Drive Help: text documents converted to Docs are capped at 1.02M characters or 50 MB, but that applies only if you *convert*. Uploaded files can be up to 5 TB. ([Files you can store](https://support.google.com/drive/answer/37603?hl=en))

### A5. Quotas and limits

From [Usage limits](https://developers.google.com/workspace/drive/api/guides/limits). **This page changed on 2026-05-01. Projects created on or after that date get the new quotas.**
- 1,000,000 quota units per minute per project. 325,000 per minute per user per project.
- Method costs: read (`files.get`) 5, list (`files.list`) 100, download 200, edit (`files.update`) 50, other 5.
- Errors: `403 User rate limit exceeded` or `429`. Use exponential backoff.
- A daily billing threshold of 400,000,000 units per project and "1 TB" per project per day of egress "before charges apply". Billing is "planned … later in 2026" with ≥90 days' notice. ([Workspace standardized model](https://developers.google.com/workspace/tools-safety), [announcement](http://workspaceupdates.googleblog.com/2026/05/agent-tools-and-security-updates-for-workspace-developers.html)) A personal AiStorage is orders of magnitude below these numbers.
- 750 GB/day of uploads and copies. 5 TB max file size. 750 GB max copy size. The page words the 750 GB rule for "Google Workspace users". rclone calls it an "undocumented limit" that consumer accounts hit too. Irrelevant at a few GB.
- Uploads ([Upload file data](https://developers.google.com/workspace/drive/api/guides/manage-uploads)):
  - Simple upload is for files ≤5 MB. Use resumable upload for anything larger, for example a 150 MB JSONL.
  - Chunks must be multiples of 256 KB.
  - A session URI expires after one week.

### A6. rclone Drive backend ([rclone.org/drive](https://rclone.org/drive/), source `docs/content/drive.md`)

- **"The shared client_id is being retired and will stop working during 2026."** You must create your own OAuth client, and doing so also avoids shared rate limits (default of about 10 transactions per second).
- Headless setup: run `rclone authorize drive` on a machine with a browser, then paste the token into the worker config. Scope options are `drive`, `drive.readonly`, `drive.file`, `drive.appfolder` and `drive.metadata.readonly`, and a comma list is allowed.
- `root_folder_id` makes rclone treat a folder as the root. **It is a convenience, not a security boundary**: the token still reaches the whole Drive.
- Deletion sends files to trash by default (`--drive-use-trash=true`). Hashes: MD5, SHA1 and SHA256 (a few files may lack SHA1 or SHA256).
- Drive allows **duplicate file names** in one folder, which confuses sync.
- Throughput is "about 2 files per second". `--drive-stop-on-upload-limit` handles the 750 GB/day limit.
- SA support in rclone assumes Workspace domain-wide delegation (`--drive-impersonate`).

### A7. Android app (Capacitor/WebView)

- Google's OAuth policy says: "A developer must not direct a Google OAuth 2.0 authorization request to an embedded user-agent under the developer's control." A Capacitor WebView counts, so use a native plugin or the system browser. ([OAuth 2.0 Policies](https://developers.google.com/identity/protocols/oauth2/policies))
- Recommended native API ([Android: Authorize access to Google user data](https://developer.android.com/identity/authorization)):
  - `Identity.getAuthorizationClient(activity).authorize(AuthorizationRequest…setRequestedScopes(DRIVE_FILE))`.
  - If consent is needed, it returns a `PendingIntent`. Otherwise "access was previously granted" and it returns an access token directly. Tokens have a "lifespan of one hour", so call `authorize()` again to refresh silently.
  - The OAuth client is the **Android** type, bound to package name + SHA-1 signing certificate.
  - Offline access (`requestOfflineAccess(serverClientId)`) returns a server auth code. "It is strongly discouraged to store refresh tokens on the device."
- Scope choice matters: `drive.file` is non-sensitive (no unverified-app screen). Full `drive` triggers the restricted-scope unverified-app screen (the personal-use exception applies).

---

## Part B: GitHub Free plan

### B1. Branch protection and rulesets on private repos

From the `github/docs` source, `data/reusables/gated-features/protected-branches.md` and `repo-rules.md`; rendered at [About protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) and [About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets).
- "Protected branches are available in public repositories with GitHub Free and GitHub Free for organizations. Protected branches are also available in public and private repositories with GitHub Pro, GitHub Team, GitHub Enterprise Cloud, and GitHub Enterprise Server."
- "Rulesets are available in public repositories with GitHub Free and GitHub Free for organizations, and in public and private repositories with GitHub Pro, GitHub Team, and GitHub Enterprise Cloud." Push rulesets are Team plan only.
- **So on a Free private repo there is no branch protection and no rulesets. A GitHub Free *organization* doesn't help either.**
- Also Pro-gated for private repos ([GitHub's plans](https://docs.github.com/en/get-started/learning-about-github/githubs-plans)): required reviewers, CODEOWNERS enforcement, wikis, Pages.
- Environments and deployment branches in private repos need Pro or higher (`gated-features/environments.md`).
- Personal-account repos have only owner and collaborator roles, and "Collaborators can't have read-only access to repositories owned by a personal account". Collaborators can push and merge. ([Permission levels for a personal account repository](https://docs.github.com/en/account-and-profile/reference/permission-levels-for-a-personal-account-repository))
- "You cannot fork a private repository to an organization using GitHub Free." ([Permissions and visibility of forks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/about-permissions-and-visibility-of-forks))

### B2. Fine-grained PATs

From [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens):
- Each token is limited to one resource owner and can be further limited to "Only select repositories", **so a single repo works.**
- Contents offers `read` or `write`. Metadata:read is implicit.
- Expiry: "Infinite lifetimes are allowed but may be blocked by a maximum lifetime policy set by your organization or enterprise owner." The pre-fill URL parameter accepts `expires_in` = 1–366 days or `none`. A personal account has no policy, so **non-expiring is allowed.**
- Limitation: fine-grained PATs can't be used "to contribute to repositories where the user is an outside or repository collaborator". So the "second GitHub account as collaborator" idea would need a classic PAT, which is broad.

Required permissions ([Permissions required for fine-grained PATs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)):

| Action | Permission |
|---|---|
| Create, update (incl. force) or delete a ref | Contents: write |
| Create or update file contents | Contents: write |
| **Merge a PR** (`PUT …/pulls/{n}/merge`) | **Contents: write** |
| Merge a branch (`POST …/merges`) | Contents: write |
| Delete a release | Contents: write |
| `repository_dispatch` | Contents: write |
| Create a PR | Pull requests: write |
| `workflow_dispatch` | **Actions: write** |
| Disable a workflow | **Actions: write** |
| Enable immutable releases; Actions workflow-permission settings | Administration: write |

- GitHub's own example link is titled "Write code and push it to main" and uses `contents=write`. Git-over-HTTPS push also requires Contents ([Choosing permissions for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)).
- **Answer: no native way on Free private repos to allow "create branches + open PRs" while denying "push to main" and "merge".** Contents:write grants all of push, force-push, branch deletion and merge. Without Contents:write you can't push a head branch at all.
- Workflow files: editing `.github/workflows/*` additionally requires the **Workflows** permission (or the classic `workflow` scope). The classic scope doc says a workflow file "can be committed without this scope if the same file (with both the same path and contents) exists on another branch" ([OAuth scopes](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)). **Unverified:** whether *deleting* a workflow file, or force-pushing over a commit that contains one, is also blocked without Workflows.
- **Workaround pattern, a PR broker via Actions** (inference; each piece is documented, the combination is **unverified**):
  1. Give the worker a fine-grained PAT with **Contents: read + Actions: write** (and optionally Pull requests: write), so it can call `workflow_dispatch` (inputs max 25 properties; the [REST doc](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event) gives no size cap) but can't push, merge or edit workflows.
  2. A workflow you wrote, which the worker can't change, uses `GITHUB_TOKEN` to commit onto `proposals/*` branches and open a PR. This needs the repo setting "Allow GitHub Actions to create and approve pull requests" ([Managing Actions settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)).
  3. You merge from the phone or Mac.
  - Costs Actions minutes. The broker must reject paths under `.github/`.
  - Actions:write also lets the holder cancel or re-run runs and disable workflows.

### B3. GitHub Apps owned by a personal account

- Installation tokens "expire after 1 hour". At mint time you can narrow them with `repositories` / `repository_ids` (≤500) and `permissions`. They "cannot be granted permissions that the app was not granted." ([Generating an installation access token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app))
- A private App "can only be installed on the account that owns the app". You can register up to 100 Apps. "The private key … grants access to every account that the app is installed on." ([Public or private](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private), [Best practices](https://docs.github.com/en/apps/creating-github-apps/about-creating-github-apps/best-practices-for-creating-a-github-app))
- **Per-profile separation** (inference):
  - Separate identities work well: one App per profile, each commits as its own `app-name[bot]`, and permissions are fixed per App.
  - Down-scoping *within* one App only protects you if the private key stays **outside** the worker, for example with the Mac minting short-lived tokens. A worker that holds the key can mint the App's full permission set.
- **Denying merge:** only by withholding Contents:write (see B2). On Free private repos, rulesets' "bypass list for Apps" isn't available.
- Deploy keys: SSH, one repo each, "read-only by default", not tied to a user, and they **never expire**. A good read-only clone credential for the worker. ([Managing deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys))

### B4. Actions, Packages and GHCR on Free

- Actions, private repos ([Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)):
  - 2,000 min/month, 500 MB artifact storage, 10 GB cache per repo.
  - Public repos and self-hosted runners are free.
  - A planned $0.002/min "cloud platform charge" on self-hosted runners (announced for 2026-03-01) was **postponed** ([changelog](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/)).
  - Hosted-runner prices were cut up to 39% on 2026-01-01.
- Packages ([Packages billing](https://docs.github.com/en/billing/concepts/product-billing/github-packages)):
  - 500 MB storage, 1 GB/month data transfer. Free for public packages. Inbound transfer is free.
  - "Container image storage and bandwidth for the Container registry is currently free."
  - Fine-grained PATs **can't access Packages**, so GHCR needs a classic PAT or `GITHUB_TOKEN`.

### B5. Git LFS and large files

- LFS on Free ([Git LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs); `data/variables/large_files.yml`):
  - **10 GiB storage + 10 GiB bandwidth per month.** Max 2 GB per file on Free.
  - With no payment method, pushes of new LFS files are blocked, and after the bandwidth runs out "Git LFS support is disabled on your account until the next month" (clones get pointer files only).
- Plain git ([About large files](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)):
  - Warning at 50 MiB, block at 100 MiB, 25 MiB through the browser.
  - Repos ideally under 1 GB, and under 5 GB strongly recommended.
- Release assets: up to 1,000 per release, each under the LFS max file size (2 GB on Free). "There is no limit on the total size of a release, nor bandwidth usage." ([About releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases))

### B6. Tamper resistance on a Free private repo

- **Prevention:** none. Force-push and branch-deletion protection come *only* from branch protection or rulesets ("By default, each branch protection rule disables force pushes…"), and Free private repos don't have them.
- **Detection (native, free):**
  - The repo **Activity view** shows pushes, merges, **force pushes** and branch creations and deletions, tied to the authenticated user ([Activity view](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/using-the-activity-view-to-see-changes-to-a-repository)).
  - The API is `GET /repos/{o}/{r}/activity?activity_type=force_push`, which needs Contents:read ([REST: list repository activities](https://docs.github.com/en/rest/repos/repos#list-repository-activities)). **Retention isn't documented.**
  - `push` webhook and Actions event payloads carry `forced`, `before`, `after` and `deleted` ([push event](https://docs.github.com/en/webhooks/webhook-events-and-payloads#push)).
  - An `on: push` watchdog workflow can't be edited by a token without the Workflows permission, and can't be disabled without Actions:write.
- **Immutable releases** ([Immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)):
  - The tag "cannot be changed, and cannot be deleted while the release exists" and assets are locked.
  - But you can delete the release (Contents:write) and then the tag. The tag name can never be reused, so this is **tamper-evident checkpoints, not prevention.**
  - Enabling needs Administration:write.
  - Availability on Free private repos isn't stated explicitly (**unverified**).
- **Inference:** real tamper-proofing needs an out-of-band copy the token holder can't reach. Examples:
  - The Mac job mirroring `git bundle`s to Drive with `keepForever`.
  - Object storage with Object Lock (see `object-storage.md`).

---

## Open items to test empirically

1. An SA with `writer` on a consumer folder: does `files.create` fail with the no-quota error, and does `files.update` (new content) on a user-owned file succeed?
2. Can a `writer` (not owner) call `revisions.delete` on a user-owned JSON file? Does `ownerRestricted` `readOnly` block that?
3. Does `fullText contains` find a token deep inside a 150 MB `.jsonl` (for example past 10 MB)? Try both the `application/json` and `text/plain` MIME types.
4. Picker folder selection with `drive.file`: does it grant access to the folder's existing children, or only the right to create in it?
5. Are immutable releases available on a Free private repo? Does pushing a deletion of `.github/workflows/x.yml` without Workflows permission get rejected?
6. How far back does the repo Activity API go?

# Object storage for AiStorage: Linode (Akamai) Object Storage vs AWS S3

Researched 2026-09-23. Sources are primary (Akamai TechDocs, Linode API reference, AWS docs and pricing, AWS price-list JSON) unless marked otherwise. Doc dates are given where the page shows one. AWS user-guide pages don't show an "updated" date. **Unverified** means the docs are silent and you should test it before relying on it.

---

## TL;DR

| Question | Linode / Akamai | AWS S3 |
|---|---|---|
| Versioning | Yes, through the S3 API only. Not in Cloud Manager, the Linode CLI or the Linode API. | Yes |
| A key that can Put/Get but **not** DeleteObjectVersion | **No, not on its own.** Limited keys are `read_only` or `read_write` per bucket. `read_write` can delete versions. The only protection is Object Lock (see below). | **Yes.** Use an IAM `Deny` on `s3:DeleteObjectVersion`. |
| A key that cannot suspend versioning or change lifecycle | **Unverified.** Docs are silent on what `read_write` allows at the bucket-config level. On an Object Lock bucket, versioning can't be suspended (AWS semantics; unverified on Linode). | **Yes.** Deny `s3:PutBucketVersioning` and `s3:PutLifecycleConfiguration`. |
| Object Lock / WORM | Yes, on all endpoint types E0–E3. It **must be enabled when the bucket is created.** Governance and Compliance modes. | Yes. Can be enabled on existing buckets. Governance, Compliance and legal hold. |
| "Privileged erase still possible" | **Yes, with Governance mode.** Limited `read_write` keys can't delete locked versions. Unlimited keys can, with the `x-amz-bypass-governance-retention` header. | **Yes, with Governance mode.** Only principals with `s3:BypassGovernanceRetention` plus the header can erase. |
| MFA delete | Not documented (shows as "Disabled" in CLI output). | Yes, but root user only, CLI/API only, and **incompatible with lifecycle configurations.** |
| Per-prefix key scoping (`agora/raw/*`) | **No.** Scoping is per bucket only. Use one bucket per scope. | **Yes.** IAM `Resource` `arn:aws:s3:::bkt/agora/raw/*` plus `s3:prefix` on ListBucket. |
| Monthly minimum | **$5/mo flat per account**, which includes 250 GB storage and adds 1 TB to the transfer pool. | None |
| Event notifications | **None.** Only management (control-plane) audit logs. | Yes: S3 → Lambda / SQS / SNS / EventBridge. |
| Native full-text search | None | No native FTS. S3 Select is closed to new customers. Options: Athena (scan), OpenSearch Serverless NextGen (scale-to-zero), or DIY SQLite FTS5. |
| CORS (for a WebView calling the bucket directly) | **Not on E2/E3** (current endpoints). E0/E1 legacy only. | Yes |
| Temporary / scoped credentials | **None.** Only long-lived access keys. | STS, Cognito identity pools, IAM Roles Anywhere |
| rclone | Yes (built-in Linode provider) | Yes |

**Bottom line for the stated requirements.** Both providers can meet "AI rewrites, never destroys; privileged erase exists", but in different ways:

- **Linode** needs Object Lock in Governance mode on a new bucket. Its limited keys are coarse, and several bucket-config operations are undocumented.
- **AWS** does it with plain IAM denies. Governance-mode Object Lock is optional extra protection. AWS also has what Linode lacks: events, prefix scoping, temporary credentials and CORS.

---

## 1. Versioning and restricting keys

### Linode / Akamai

- **Versioning.** When enabled, "objects are not overwritten or deleted", and every version is billed as storage. Versioning can only be managed through third-party S3 tools (AWS CLI, Cyberduck), not Cloud Manager, the Linode CLI or the Linode API. [Versioning, updated 2026-09-14](https://techdocs.akamai.com/cloud-computing/docs/versioning-retain-object-version-history)
- **Access key types** [(Access keys, updated 2026-09-08)](https://techdocs.akamai.com/cloud-computing/docs/manage-access-keys):
  - **Unlimited** (the default): "access to all APIs within the selected regions".
  - **Limited:** per-bucket permission of `None`, `Read` (`read_only`) or `Read/Write` (`read_write`). Read/Write "can list, retrieve, add, delete, and modify most information and objects". The API enum is only `read_write` or `read_only`. [API: create key](https://techdocs.akamai.com/linode-api/reference/post-object-storage-keys)
  - **No per-action or per-prefix control is documented.**
  - "All access keys can create new buckets", but a limited key that creates a bucket can do nothing else on it.
  - Keys are regional. There is no expiry or TTL field. The limit is 100 keys per account. [Limits](https://techdocs.akamai.com/cloud-computing/docs/object-storage-product-limits)
- **Bucket policies** exist (Ceph-based) [(Bucket policies, updated 2026-09-08)](https://techdocs.akamai.com/cloud-computing/docs/define-access-and-permissions-using-bucket-policies):
  - Allow and Deny, resource prefixes (`bucket/folder/*`), and IP conditions are supported.
  - **Only unlimited keys can set bucket policies.**
  - **Blocker for per-key rules:** "all users and Object Storage API keys on an account share the same canonical ID" [(Canonical ID, 2026-09-08)](https://techdocs.akamai.com/cloud-computing/docs/find-the-canonical-user-id-for-an-account). So a policy `Principal` can't single out one key. Linode staff confirmed in a 2020 community thread that separate principals need separate accounts. [community Q20239 (secondary, dated)](https://www.linode.com/community/questions/20239/write-only-object-storage-access-key)
  - Limited keys "are enforced by provisioning a bucket policy on the selected buckets". If you put your own policy, you must merge it with that one or the limited key starts getting 403s. The principal format used inside that provisioned policy is **undocumented**. Editing it to add per-key Deny/prefix rules might work, but it is unsupported and unverified.
- **So the answer to "Put/Get but no DeleteObjectVersion / no suspend versioning / no lifecycle change":**
  - DeleteObjectVersion: not achievable with keys or policies alone. The Object Lock matrix shows limited `read_write` **can** delete versions when lock is disabled.
  - Suspend versioning and lifecycle change: **unverified**. The docs say only that limited keys can't set bucket policies. Test `put-bucket-versioning`, `put-bucket-lifecycle-configuration` and `put-object-lock-configuration` with a limited key.

### AWS S3

- **Permanent deletes need a separate action.** Deleting a specific version requires `s3:DeleteObjectVersion`. A plain `s3:DeleteObject` (no versionId) only adds a delete marker. Deleting a governance-locked version also needs `s3:BypassGovernanceRetention`. [Required permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html)
- **Bucket config has separate actions too:** `s3:PutBucketVersioning`, `s3:PutLifecycleConfiguration` (which also covers DeleteBucketLifecycle), and `s3:PutBucketObjectLockConfiguration`. (Same source.)
- **AWS's own guidance:** to block deletion, deny `s3:DeleteObject`, `s3:DeleteObjectVersion` **and** `s3:PutLifecycleConfiguration`, "because you can delete objects ... by configuring their lifecycle". [Bucket policy examples](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-bucket-policies.html)
- **Rollback without delete rights:** copy an old version onto the key (`copy-object --copy-source bkt/key?versionId=...`). "All object versions are preserved." This needs only Get(Version) and Put. [Restoring previous versions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/RestoringPreviousVersions.html) Removing a delete marker requires DeleteObjectVersion, so keep that privileged.

Suggested AI-agent policy (identity policy, sketch):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
     "Resource": "arn:aws:s3:::aistorage-agora/agora/*"},
    {"Effect": "Allow",
     "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
     "Resource": "arn:aws:s3:::aistorage-agora",
     "Condition": {"StringLike": {"s3:prefix": ["agora/*"]}}},
    {"Effect": "Deny",
     "Action": ["s3:DeleteObjectVersion", "s3:BypassGovernanceRetention",
                "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration",
                "s3:PutBucketPolicy", "s3:DeleteBucketPolicy",
                "s3:PutBucketObjectLockConfiguration", "s3:PutObjectRetention",
                "s3:PutObjectLegalHold", "s3:PutReplicationConfiguration",
                "s3:DeleteBucket"],
     "Resource": ["arn:aws:s3:::aistorage-agora", "arn:aws:s3:::aistorage-agora/*"]}
  ]
}
```

Also give agent credentials **no `iam:*` permissions**, so they can't rewrite their own policy. (`s3:prefix` on `ListBucketVersions` is per the Service Authorization Reference; I did not re-verify it this session because the page is rendered by JavaScript.)

---

## 2. Object Lock / WORM / MFA delete, and the "privileged erase" requirement

### Linode / Akamai
[Object Lock, updated 2026-09-14](https://techdocs.akamai.com/cloud-computing/docs/protect-data-with-object-lock)

- **Availability:** supported on all endpoint types (E0–E3).
- **Setup limits:**
  - Must be enabled at bucket creation; it can't be added to an existing bucket.
  - Enabling it also turns on versioning.
  - It is managed through the S3 API only; Cloud Manager has no support.
- **The documented permission matrix:**

  | Key | Mode | Add version | Add delete marker | Delete version during retention |
  |---|---|---|---|---|
  | Unlimited | Governance | yes | yes | **yes, with `x-amz-bypass-governance-retention`** |
  | Unlimited | Compliance | yes | yes | no |
  | Limited `read_write` | Governance | yes | yes | **no** |
  | Limited `read_write` | Compliance | yes | yes | no |
  | Limited `read_only` | any | no | no | no |

- **Compliance mode is not usable here:** versions "cannot be deleted or modified by any user, or Akamai, until (1) the retention period expires (2) the account is deleted". That conflicts with the privileged erase.
- **Fit: good.** Use Governance mode with a long default retention. AI and sync jobs get limited `read_write` keys. "Erase" uses an unlimited key held offline and sends the bypass header.
- **Lifecycle vs locks:** upstream Ceph says "Object Lock takes precedence. If a Lifecycle rule attempts to delete a locked object version, the deletion is blocked." [Ceph blog, 2025-12-11](https://ceph.io/en/news/blog/2025/rgw-deep-dive-3/) Linode's deployment isn't explicitly documented to behave the same. **Test it.**
- **Gaps (unverified):**
  - Whether a limited `read_write` key can call `PutObjectLockConfiguration` to shorten the default retention. That would affect **future** versions only, because "configuration changes apply only to new versions".
  - Whether it can call `PutObjectRetention` or legal hold.
  - The maximum retention value isn't documented.
  - Mitigation: a periodic audit job that checks the lock config.
- **MFA delete:** not documented.

### AWS S3
- **Modes** [(Object Lock)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html):
  - **Compliance:** can't be deleted "by any user, including the root user". The only escape is deleting the account.
  - **Governance:** override requires `s3:BypassGovernanceRetention` **and** the `x-amz-bypass-governance-retention:true` header.
  - **Legal hold:** no expiry, but "freely placed and removed by any user who has `s3:PutObjectLegalHold`", so deny it to agents.
  - **Variable retention / event hold** is a newer option (duration 1–36,500 days).
- **Lock and bucket setup** [(Configure)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html):
  - Object Lock can be enabled on **existing** buckets.
  - Once enabled, "you can't disable Object Lock or suspend versioning".
- **Lifecycle, delete markers and limits** [(Considerations)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html):
  - "A locked version of an object cannot be deleted by a S3 Lifecycle expiration policy."
  - Delete markers are not WORM-protected. That's fine: markers don't destroy data.
  - Maximum retention is 100 years.
  - To stop changes to bucket defaults, deny `s3:PutBucketObjectLockConfiguration`.
  - Uploads that carry retention need a `Content-MD5` or checksum header.
- **MFA delete** [(MFA delete)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/MultiFactorAuthenticationDelete.html):
  - Only the bucket owner **root account** can enable it, and only through CLI/API.
  - It covers version deletes and versioning-state changes.
  - "You cannot use MFA delete with lifecycle configurations."
  - It's poorly suited to automation. A better option: an "erase" IAM role that requires MFA through a policy condition.
- **Fit: good.** IAM denies alone meet the requirement. Governance mode protects against a policy mistake. The erase role gets `s3:DeleteObjectVersion` + `s3:BypassGovernanceRetention`, ideally gated by MFA.

---

## 3. Per-prefix scoping

- **Linode: not available.** Limited keys are bucket-granular. Bucket policies can express prefixes, but they can't target an individual key (shared canonical ID, see §1).
  - Workaround: **one bucket per trust scope**, e.g. `agora-raw`, `agora-reading`, `foundry`, `catalog`, each with its own limited key.
  - Room for this: 1,000 buckets per endpoint and 100 keys per account. [Limits](https://techdocs.akamai.com/cloud-computing/docs/object-storage-product-limits)
- **AWS: available.**
  - Put `Resource: arn:aws:s3:::bucket/agora/raw/*` on object actions.
  - Add an `s3:prefix` condition on `ListBucket` (AWS home-folder example in [bucket policy examples](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-bucket-policies.html)).

---

## 4. Pricing at 10–100 GB

### Linode / Akamai
[Pricing, updated 2026-09-08](https://techdocs.akamai.com/cloud-computing/docs/object-storage-pricing)

- **Base fee:** "Object Storage costs a flat rate of $5 a month, and includes 250 GB of storage." It is billed "regardless of whether or not there are active buckets" and is prorated. You must cancel Object Storage to stop billing.
- **Transfer:** enabling it adds **1 TB** to the account's monthly global transfer pool. Jakarta and São Paulo get no extra allowance.
- **Overage:**
  - Storage: $0.02/GB (Jakarta $0.024, São Paulo $0.028).
  - Egress: $0.005/GB (Jakarta $0.015, São Paulo $0.007).
- **Requests:**
  - "No charges for API requests for any Object Storage endpoint types."
  - Akamai is evaluating per-request charges for E3, which would not appear on invoices "any earlier than October 1, 2027".
  - Deletes are free.
- **Does your existing subscription cover it?** The $5 is **once per account**. If Object Storage is already enabled and your total account usage stays under 250 GB, adding 10–100 GB costs about **$0 extra**. Egress to phones and containers should also fit inside the 1 TB pool.
- **Regions near you** [(Endpoint types, 2026-09-08)](https://techdocs.akamai.com/cloud-computing/docs/endpoint-types):
  - E3: Tokyo 3 (`jp-tyo-3`, GA 2026-03-16 per [changelog](https://techdocs.akamai.com/cloud-computing/changelog/mar-16-2026-object-storage-e3-endpoint-now-generally-available-tokyo)) and Singapore 2 (`sg-sin-2`).
  - E1 legacy only: Osaka.

### AWS S3 (Standard)
Rates from the AWS price-list JSON published 2026-09-18 (S3) and 2026-09-16 (data transfer), plus [S3 pricing](https://aws.amazon.com/s3/pricing/).

| | us-east-1 | ap-northeast-1 Tokyo | ap-east-2 Taipei |
|---|---|---|---|
| Storage, first 50 TB | $0.023/GB-mo | $0.025 | $0.0225 |
| PUT/COPY/POST/LIST | $0.005 per 1k | $0.0047 per 1k | $0.00423 per 1k |
| GET | $0.0004 per 1k | $0.00037 per 1k | $0.000333 per 1k |
| Egress to internet, after free 100 GB | $0.09/GB | $0.114/GB | $0.1083/GB |

- **Free egress:** the first 100 GB/month out to the internet is free, "aggregated across all AWS Services and Regions (except China and GovCloud)".
- **Other terms:** ingress is free, DELETE is free, and S3 Standard has no minimum. Every noncurrent version is billed as storage.
- **Worked example.** Assumptions: 100 GB stored, 50 GB egress, 100k PUTs and 1M GETs per month.
  - us-east-1 comes to about **$2.30 + $0 + $0.50 + $0.40 ≈ $3.20/mo**.
  - At 300 GB egress, add about $18 (us-east-1) or $22.8 (Tokyo). Linode would still be $0 marginal.
- **Heavy egress favors Linode.** For large Foundry video downloads, Linode is much cheaper.

---

## 5. Event triggers for regenerating derived copies

- **Linode: no bucket notifications.**
  - Linode staff answer: "While supported by Ceph, bucket notifications are not currently a feature of Linode's Object Storage." [community Q21051 (~2021, secondary)](https://www.linode.com/community/questions/21051/object-storage-bucket-notification)
  - Current docs still show only **management (control-plane) events** in Cloud Pulse logs; object PUT/DELETE aren't logged. [Logs, 2026-09-08](https://techdocs.akamai.com/cloud-computing/docs/logs-for-object-storage)
  - Options: polling (ListObjectVersions, or diff ETags/LastModified) from a scheduled job, or have the writer call a regenerate hook after it writes.
- **AWS: S3 Event Notifications → Lambda / SQS / SNS / EventBridge.**
  - Delivery is "designed to be delivered at least once", typically within seconds.
  - Event types include ObjectCreated, ObjectRemoved (including delete-marker), lifecycle expiration and ObjectRetention.
  - AWS warns about loops, so write derived copies to a different prefix or bucket. [Event notifications](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html)
  - Lambda free tier: "one million requests and 400,000 GB-seconds per month". After that, $0.20 per 1M requests and $0.0000166667/GB-s on x86. [Lambda pricing](https://aws.amazon.com/lambda/pricing/)
- **Cross-cloud option:** a Lambda can also regenerate copies for a Linode bucket, but only on a schedule (EventBridge Scheduler), since Linode can't push events.

---

## 6. Full-text search over a few GB of JSON

| Option | Status and cost | Fit |
|---|---|---|
| **S3 Select** | "No longer available to new customers." Max record size 1 MB. [doc](https://docs.aws.amazon.com/AmazonS3/latest/userguide/selecting-content-from-objects.html) | Not viable |
| **Athena** | $5 per TB scanned, so a 5 GB full scan costs about $0.025 per query, plus S3 requests. [pricing](https://aws.amazon.com/athena/pricing/) Hard limits: 32 MB per row and 200 MB per text line. [limits](https://docs.aws.amazon.com/athena/latest/ug/other-notable-limitations.html) | Brute-force `LIKE` / `regexp_like` scan with seconds of latency and no ranking. **Raw records up to 150 MB exceed the 32 MB row limit**, so query only the reading copies. |
| **OpenSearch Serverless NextGen** (GA 2026-05-28, [announcement](https://aws.amazon.com/about-aws/whats-new/2026/05/amazon-opensearch-serverless-next-generation-generally-available/)) | $0.24 per OCU-hour and $0.02 per GB-month storage (us-east-1). **Scales to zero** after 10 minutes idle (not configurable), with 10–30 s cold start. Waking search starts "two search workers". [scale-to-zero doc](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-scale-to-zero.html), [pricing](https://aws.amazon.com/opensearch-service/pricing/) | Real FTS without an always-on bill. **Uncertain:** the OCU cost of one wake window (how workers map to OCUs isn't stated) and CJK analyzer support; I didn't verify either. Classic collections bill a minimum of 2 OCUs (about $350/mo), so avoid them. |
| **SQLite FTS5 index file in the bucket** | Near-zero cost. A Lambda (AWS) or scheduled worker (Linode) rebuilds `index.sqlite` from the reading copies and uploads it. | **Cheapest.** Clients download it (tens of MB) or query it over HTTP Range requests with [sql.js-httpvfs](https://github.com/phiresky/sql.js-httpvfs). That library is third-party, "mainly written for small personal projects", and needs CORS and Range support. |

Notes on the SQLite pattern:

- **CJK text:** the FTS5 `trigram` tokenizer supports substring matching and indexed LIKE/GLOB. Queries shorter than 3 Unicode characters match nothing. [SQLite FTS5](https://www.sqlite.org/fts5.html)
- **Single writer:** only one job should write the index. On AWS, use conditional writes (`If-Match` ETag / `If-None-Match`) for optimistic concurrency on the index or catalog object. [Conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html) Linode support for conditional writes is **unverified**.
- **Linode and browsers:** E2/E3 have **no CORS**, so range-querying from a WebView needs native HTTP (see §7).

---

## 7. Client access (Android Capacitor, Docker, Mac)

- **SDKs:** both are S3-compatible. AWS SDK for JavaScript v3 works in a browser or WebView (and in Node) against either; for Linode, override the endpoint.
- **rclone:**
  - Has a built-in `Linode` provider.
  - Versions: `--s3-versions` and `--s3-version-at`.
  - Object Lock: `--s3-object-lock-mode`, retain-until and legal-hold flags.
  - `--s3-no-check-bucket` for keys that can't list or create buckets. [rclone S3](https://rclone.org/s3/)
- **Presigned URLs on AWS:** valid up to **7 days** with IAM-user SigV4 credentials, and no longer than the session when signed with temporary credentials. A presigned URL is a bearer token. You can cap its age with the `s3:signatureAge` bucket-policy condition. [Presigned URLs](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)
- **Presigned URLs on Linode:**
  - Standard SigV4 presigning through an SDK should work because the endpoints are S3-compatible, but Akamai doesn't document it explicitly. **Test it.**
  - The Linode API's [Create a URL for an object](https://techdocs.akamai.com/linode-api/reference/post-object-storage-object-url) is limited to 360–3600 s and needs a token with scope `object_storage:read_write`.
  - That **same scope can create access keys** ([create key](https://techdocs.akamai.com/linode-api/reference/post-object-storage-keys)), so **never put a Linode API token on the phone or in agent containers.**
- **CORS:**
  - AWS supports bucket CORS.
  - Linode: "CORS is not available for E2 and E3 endpoints." [ACL/CORS doc, 2026-09-08](https://techdocs.akamai.com/cloud-computing/docs/define-access-and-permissions-using-acls-access-control-lists)
  - On Linode E2/E3 the Capacitor app has to use native HTTP. `CapacitorHttp` patches fetch/XHR to native libraries and is off by default. For large files, the docs point to `@capacitor/file-transfer`. [CapacitorHttp](https://capacitorjs.com/docs/apis/http) My inference, not stated in the doc: native requests aren't subject to WebView CORS.

---

## 8. Temporary or scoped credentials without an always-on server

**Linode:** no STS or temporary credentials are documented. The only mechanism is long-lived access keys (no expiry field) that you revoke through the API. Give each identity its own limited key on its own buckets.

**AWS options:**

| Identity | Best-fit mechanism | Notes |
|---|---|---|
| **Docker worker** (Linux, no GitHub token) | **IAM Roles Anywhere** with a **self-managed CA**, or simply a scoped IAM-user key | Roles Anywhere issues temporary credentials in exchange for an X.509 certificate. The trust anchor can be an external CA bundle (PEM), so no AWS Private CA is needed. Private CA would cost $400/mo general-purpose or $50/mo short-lived. [Private CA pricing](https://aws.amazon.com/private-ca/pricing/) Roles Anywhere itself is "no additional cost" [(2022 launch)](https://aws.amazon.com/about-aws/whats-new/2022/07/aws-identity-access-management-iam-roles-anywhere-workloads-outside-aws/). The helper `aws_signing_helper` (v1.8.5, 2026-08-24) ships for Linux x86-64/aarch64, macOS and Windows. It has `credential-process`, `serve` (an IMDSv2-compatible local endpoint) and `update` modes, with sessions of 900–43,200 s. [Roles Anywhere intro](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html), [credential helper](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/credential-helper.html) The certificate's private key is still a long-lived secret, but it can expire and be revoked, and the IAM denies cap the damage if it leaks. |
| **Mac sync job** | Roles Anywhere; the key can live in the macOS Keychain (`--cert-selector`) | Or an IAM-user key limited to `agora/raw/*`. |
| **Phone app** (Capacitor) | **Cognito identity pool** plus a login (Google, Sign in with Apple, or a Cognito user pool) | Identity pools are free: "provided at no charge". User pools Lite/Essentials include 10,000 MAU free per month, indefinitely. [Cognito pricing](https://aws.amazon.com/cognito/pricing/) In the JS SDK, use `fromCognitoIdentityPool` from `@aws-sdk/credential-providers`. [SDK doc](https://docs.aws.amazon.com/sdk-for-javascript/v3/developer-guide/loading-browser-credentials-cognito.html) Roles Anywhere isn't practical on Android: there is no official helper, so you'd have to implement CreateSession signing yourself. |
| **Alternative for the phone** | A Lambda function URL that checks a token and returns presigned URLs | Serverless and pay-per-request, but it's code you own. |
| Plain **STS AssumeRole** | Needs a base credential (an IAM user) | Useful for scoping down, but doesn't remove the base secret. |

AWS explicitly recommends against long-term credentials in apps outside AWS. It suggests OIDC federation, or Cognito for human users. [OIDC federation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_oidc.html)

---

## Open items to test before committing

1. **Linode, limited `read_write` key on an Object Lock (Governance) bucket.** Try:
   - `put-bucket-versioning Status=Suspended`
   - `put-bucket-lifecycle-configuration` with `NoncurrentVersionExpiration`
   - `put-object-lock-configuration` with a shorter default
   - `put-object-retention`
   - `delete-object --version-id` (expected to be denied)
2. **Linode:** check whether lifecycle skips locked versions, and what maximum Governance retention is accepted.
3. **Linode:** check SigV4 presigned GET/PUT through the SDK (7-day expiry) and conditional writes (`If-Match`).
4. **AWS:** check that Object Lock, event notifications and OpenSearch Serverless NextGen are available in the region you pick (Taipei `ap-east-2` is the cheapest for storage; I didn't verify feature availability there).
5. **OpenSearch NextGen:** measure the real cost of one "search wake" (10+ minutes at 2 workers) and check CJK analyzer support.

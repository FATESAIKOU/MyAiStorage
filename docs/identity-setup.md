# AiStorage 身分與簽章金鑰設定指南 (Phase 1)

本文說明 AiStorage 第 1 期（Phase 1）的身分與簽章金鑰設定程序。

依據設計原則（D2「產生者章」、D3「身分的實現」）與規格（`specs/common/identity`）：
- **身分即 Profile**：Mac 容器內的 opencode、同步器或測試 agent 皆以所屬的 profile 作為身分。
- **簽章金鑰證明 Profile**：由於所有 worker 共用限權的 Google Drive 憑證，Drive 收件匣位置無法作為身分依據；每個 profile 持有專屬的 Ed25519 簽章金鑰，在收件匣 sidecar 上簽章，由提交流程驗章並蓋上產生者章。
- **公開資訊與私鑰隔離**：登錄檔 `config/identity.json` 僅包含公開金鑰與授權設定，納入 Git 版本控管；私鑰絕不進入 repo，存放於 Mac 宿主機並唯讀掛載進容器的 `/secrets/`。
- **單一來源原則 (Single Source of Truth)**：期 1 由系統管理員手動生成並安裝，登錄檔即為發放紀錄。之後改由 MyLinuxPool (MLP) 發放時，`config/identity.json` 將由 MLP 的發放結果自動產生與同步，儲存要素不另行維護可能與發放端分歧的身分清單。

---

## 流程概覽

```
[Mac 宿主機]
  1. keygen 產生 Ed25519 金鑰對
     ├── 私鑰 (Raw 32 bytes) ──> ~/.config/aistorage/keys/<profile>.key (chmod 0600, dir 0700)
     │                                └── (唯讀掛載) ──> 容器內 /secrets/<profile>.key
     └── 公鑰 (Base64) ────────> 登記至 config/identity.json (納入 repo)
                                      └── 提交流程 (GitHub Actions) 驗章與授權
```

---

## 步驟一：產生簽章金鑰對

使用 `aistorage.identity` CLI 的 `keygen` 子命令產生標準 Ed25519 金鑰對。
正式私鑰建議儲放於 `~/.config/aistorage/keys/`（目錄權限 700，與 spike 測試憑證隔離）：

```bash
uv run python -m aistorage.identity keygen \
  --profile mac-opencode \
  --out ~/.config/aistorage/keys/mac-opencode.key
```

### 行為說明：
1. **目錄與私鑰權限**：父目錄權限自動確保為 `0700`，私鑰以原始 32 位元組（raw bytes）寫入 `--out` 指定路徑，權限嚴格設為 `0600`（僅擁有者可讀寫）。
2. **Profile 格式檢驗**：`--profile` 必須符合 `^[a-z][a-z0-9-]*$` 格式，否則中止執行。
3. **防覆寫保護**：若目標檔案已存在，工具會拒絕覆寫並中止。
4. **金鑰識別碼指紋化**：`key_id` 格式為 `<profile>-<sha256(公鑰raw32)前8個小寫hex>`，例如 `mac-opencode-a6ehf92d`，保證同月多次輪替不撞號，且可由公鑰內容驗證。
5. **終端輸出**：標準輸出僅印出 `key_id` 與 Base64 編碼的公開金鑰（絕不印出私鑰內容）：
   ```text
   key_id: mac-opencode-a6ehf92d
   public_key: A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=
   ```

---

## 步驟二：登記公開金鑰至身分登錄檔

開啟專案中的身分登錄檔 `config/identity.json`（初次設定可參考 `config/identity.example.json`），將公鑰與授權資訊填入對應的 profile：

```json
{
  "format": "aistorage.identity/v1",
  "profiles": {
    "mac-opencode": {
      "allowed_types": [
        "session",
        "handoff",
        "claim",
        "reference",
        "rewrite",
        "artifact"
      ],
      "signing_keys": [
        {
          "key_id": "mac-opencode-a6ehf92d",
          "public_key": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
          "status": "active",
          "added_at": "2026-09-27T08:00:00Z",
          "revoked_at": null
        }
      ],
      "inbox_folder_ids": [
        "1Obn3Rj1Quyg1l_2YW0GhXE39FpETeyLj"
      ]
    }
  }
}
```

### 欄位說明：
- `allowed_types`：該 profile 獲授權寫入的項目型態清單（限定 2.1 定義之 `session`, `handoff`, `claim`, `reference`, `rewrite`, `artifact`）。**注意（ADR 0009）**：`allowed_types` 請勿包含 `artifact`——產出登錄不再經過 Agora 的收件匣（Foundry 改成 Drive 共享資料夾＋GitHub），放了只會讓 artifact 進了收件匣卻被明確拒收（`artifact_not_supported`），而且每一輪都被計入 shaped 項目、讓每輪提交時間變長。
- `signing_keys`：
  - `key_id`：金鑰識別碼，格式為 `<profile>-<公鑰前8hex>`，整份登錄檔中全域唯一。
  - `public_key`：Base64 編碼的 32 位元組 Ed25519 公鑰（正式登錄檔禁止使用全 0 範例公鑰）。
  - `status`：狀態必須為 `"active"`（有效）或 `"revoked"`（已撤銷）。
  - `added_at`：啟用時間（RFC 3339 UTC 格式，帶 `Z`）。
  - `revoked_at`：撤銷時間；若狀態為 `active` 則**必須為 `null`**。
- `inbox_folder_ids`：該 profile 在 Google Drive 上的收件匣資料夾 ID 清單（僅供提交流程掃描定位，不代表產生者證明）。

### 驗證登錄檔有效性：
完成修改後，使用 `check` 子命令進行格式與領域規則檢查（包含指紋、金鑰長度與狀態檢查）：

```bash
uv run python -m aistorage.identity check config/identity.json
```

---

## 步驟三：Mac 宿主機與容器掛載配置

為落實身分隔離與安全性邊界，私鑰僅能由容器以唯讀方式存取：

1. **宿主機目錄與權限確認**：
   確保私鑰檔案位於 Mac 宿主機之安全路徑，權限設定為 `0600`，目錄權限為 `0700`：
   ```bash
   chmod 700 ~/.config/aistorage/keys
   chmod 600 ~/.config/aistorage/keys/mac-opencode.key
   ```
2. **容器掛載（Colima / Docker）**：
   啟動 opencode / worker 容器時，以唯讀掛載（`:ro`）掛入容器內的 `/secrets/` 目錄：
   ```bash
   docker run -d \
     --name opencode-worker \
     -v ~/.config/aistorage/keys/mac-opencode.key:/secrets/mac-opencode.key:ro \
     -v ~/.config/aistorage/worker-credentials.json:/secrets/worker-credentials.json:ro \
     ...
   ```
3. **資源清冊記錄**：
   在 `docs/resources.md` 中登記私鑰路徑與金鑰用途（**切勿記錄金鑰內容與秘密值**）。

---

## 步驟四：金鑰撤銷程序 (Key Revocation)

當懷疑金鑰外洩、或進行定期金鑰輪替時，依下列程序撤銷舊金鑰：

1. **更新登錄檔狀態**：
   在 `config/identity.json` 中將欲撤銷金鑰的 `status` 修改為 `"revoked"`，並填寫 RFC 3339 UTC 撤銷時間戳 `revoked_at`：
   ```json
   {
     "key_id": "mac-opencode-a6ehf92d",
     "public_key": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
     "status": "revoked",
     "added_at": "2026-09-27T08:00:00Z",
     "revoked_at": "2026-10-01T12:00:00Z"
   }
   ```
2. **驗證登錄檔**：
   ```bash
   uv run python -m aistorage.identity check config/identity.json
   ```
3. **提交並推送登錄檔**：
   將更新後的 `config/identity.json` 提交推送到 repo 的 `main` 分支。
4. **生效與防重放相依性**：
   提交流程（GitHub Actions）在每次執行時均以 `main` 分支的 HEAD 作為基準（D2 的 `github.sha == main HEAD` 保證）。重跑（rerun）舊的 GitHub Actions run 不會以舊登錄檔放行被撤銷的金鑰。一旦更新推至 main，提交流程隨即使用最新登錄檔，任何使用已撤銷金鑰簽章的項目一律被拒收並記錄原因。
5. **換發新金鑰**：
   若該 profile 需繼續寫入，依步驟一產生新金鑰對，並將新公鑰以 `status: "active"` 與新的指紋 key_id 加入 `signing_keys` 陣列。

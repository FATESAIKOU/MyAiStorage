# e2e

端到端驗收（tasks 9.1〜9.5）：分裂（1→n）、統合（n→1）、相互參照（n↔m）、反向測試、持久性。

每個 Session 都在各自的住民容器（`resident/run.sh`）裡執行；接手或參照的一方
唯一的資訊來源是讀取介面（`AgoraReader`）。提交流程由測試在本機以 committer-test
身分執行（`run_committer`），不觸發 GitHub Actions。

## 前置

1. `scripts/e2e_setup.py`（在 `TEST_FOLDER_ID` 底下建 Agora／Foundry repo、
   pin-test、讀取視圖、測試收件匣、測試 profile 的金鑰與身分登錄檔），產出
   `config/committer.e2e.json` 與 `config/reader.e2e.json`；
2. `resident/build.sh` 建好住民 image；
3. 測試用 profile 的祕密目錄（**與正式目錄分開**）：
   `~/.config/aistorage/resident-e2e/<profile>/` 內有
   `rclone-worker.conf`、`sa-reader.json`、`signing.key`、`reader.json`。
   profile 名稱預設從 `reader.e2e.json` 的 `inbox_folder_ids` 唯一條目取得
   （`scripts/e2e_setup.py` 寫的是 `mac-opencode-test`，`AISTORAGE_E2E_PROFILE`
   可覆寫）；正式的 `resident/` 根目錄一律拒絕（E-P4）。
   `llm-<provider>.key` **只在該 provider 需要金鑰時**才需要——預設模型
   `opencode/space-bunny-free` 屬於免金鑰的 provider（見下一節），所以
   `setup` 不會放這個檔案，放了反而會讓 opencode 認證失敗。

`scripts/e2e_setup.py` 目前尚未完成的部分（**A 線**，已列給 PM）：

- 把測試 profile 命名為 `mac-opencode-test`（或可由環境變數覆寫），
  不要再叫 `mac-opencode`（spec 要求期 1 另有測試用 profile）；
- 佈置測試 profile 的祕密目錄（`~/.config/aistorage/resident-e2e/…`）：
  至少放進 `rclone-worker.conf`、`sa-reader.json`、`signing.key`、`reader.json`；
- `reader.e2e.json` 的 `inbox_folder_ids` 只放測試 profile 的收件匣 id；
- 測試用的 LLM 金鑰（`llm-<provider>.key`）由使用者提供，setup 不產生。

Foundry 的端到端（9.2／9.5 的 artifact 部分）另外等提交流程接上 Foundry
（F-H1〜F-H3）：註冊工具 `aistorage_register_artifact` 已經完成，但收件匣的
artifact 目前只會被 DEFER、發佈端也還沒寫 `object_file_id`。那兩個測試以
`xfail(strict=True)` 鎖住，修好會 XPASS 提醒。

設定缺少時一律 FAIL，不用 `pytest.skip` 掩蓋（PM 規範）。

### 已實測（2026-09-28）

`resident/run.sh` 起測試 profile 的容器（免金鑰）後，
`POST /session/{id}/message` 問「回覆 OK」→ 回 `OK`、
`finish=stop`、`cost=0`。所以第 9 組的容器互動測試**不需要**任何 LLM 金鑰。

## 已知的範圍限制（E-M1）

9.x 的提交流程由測試在本機扮演（不花 Actions 分鐘，也不需 PAT）。這驗證了
「寫入者以讀取介面判斷完成」（ADR 0007）與整個提交路徑；但**沒有**覆蓋
「skill 觸發 GitHub workflow → runner 上的提交流程」那一段。要補的時候：
把 `gh-pat-actions.txt`（測試 repo 的 workflow_dispatch token）放進測試
profile，讓工具自己觸發；`run_committer` 只保留補送的角色。**待 PM 決定**
（需要一個測試用完可撤銷的 PAT）。

E-M2（每個情境記錄讀取視圖 generation 與收件匣檔案數，結束時驗證沒有殘留）
尚未全面加上：目前只有 `test_reference.py` 斷言讀取不增加收件匣檔案，
`test_adversarial.py` 自己清掉測試放入的孤兒。其餘情境的收尾檢查留到
A 線的 e2e_setup 完成、真的能跑之後再補（需要先知道正常的殘留形狀）。

執行：`uv run pytest tests/e2e -m e2e`（模型以 `AISTORAGE_E2E_MODEL` 指定，
預設 `opencode/space-bunny-free`，不使用 Claude）。

## 每個工具呼叫都要確認

「要求 AI 呼叫工具」的步驟之後一律呼叫
`assert_tool_called(container, session_id, tool)`：從容器內的 `opencode export`
確認 tool part 存在且成功。模型沒照做時 raise `ModelDidNotComply`（環境問題，
可換模型重跑），工具失敗時才是 AssertionError（系統錯誤）。

## 9.4 反向測試項目 → 測試檔案對照表

task 9.4 的項目分佈在 e2e 與整合測試；每一項都查得到在哪裡驗證。

| 9.4 項目 | 在哪裡驗證 |
|---|---|
| Mac opencode 的憑證推不了、改不了、刪不了真本與舊版本 | `tests/e2e/test_adversarial.py::test_9_4_worker_cannot_delete_or_modify_true_store`（rclone／file id 兩種）；`tests/integration/test_admin_erase_integration.py`（抹除必須 403） |
| 沒有簽章或簽章不符的收件匣項目被拒收、自填產生者被忽略 | `tests/e2e/test_adversarial.py::test_9_4_unsigned_item_rejected`；單元：`tests/unit/test_intake.py`、`tests/unit/test_accept_committer.py` |
| 重複認領被拒絕（already_claimed） | `tests/e2e/test_adversarial.py::test_9_4_duplicate_claim_rejected`；單元：`tests/unit/test_accept_apply.py` |
| 撤銷簽章金鑰後該 profile 一律被拒收、其他 profile 不受影響 | `tests/e2e/test_adversarial.py::test_9_4_revoked_key_unauthorized`；單元：`tests/unit/test_intake.py::test_evaluate_revoked_key_rejects_as_unauthorized`、`tests/unit/test_identity.py` |
| 換掉 main、真 main＋偽造 `git-annex` 分支、多餘 ref | `tests/integration/test_committer_injection.py` |
| 同名 DoS、多層同名資料夾 | `tests/integration/test_committer_injection.py` |
| 上一版 manifest 冒充目前版本 | `tests/integration/test_committer_injection.py` |
| 名稱與內容相符但沒被引用的 annex 物件 | `tests/integration/test_committer_injection.py`、`tests/unit/test_integrity.py` |
| prepare 之後 push 之前的注入 | `tests/integration/test_committer_injection.py` |
| push 後、轉正前被取消，下一輪自動恢復 | `tests/integration/test_committer_recovery.py` |
| GITMANIFEST 替換空檔 kill，下一輪自動恢復 | `tests/integration/test_committer_recovery.py` |
| consolidate 之後的 prepare 與 push 正常 | `tests/integration/test_committer_round_smoke.py`、`tests/integration/test_committer_bundle_gc.py` |
| 暫時性的讀取錯誤不會移動任何真檔 | `tests/integration/test_committer_transient_error.py` |
| 被刪掉的收件匣項目由同步器補傳 | `tests/unit/test_accept_syncer.py`（補傳條件） |

## 檔案

- `conftest.py`：住民容器 fixture（測試 profile、`AISTORAGE_WORK_ROOT`）、
  本機提交流程、讀取介面、`assert_tool_called`、輪詢工具。
- `test_split.py`（9.1）、`test_consolidation.py`（9.2）、`test_reference.py`（9.3）、
  `test_adversarial.py`（9.4）、`test_persistence.py`（9.5）。

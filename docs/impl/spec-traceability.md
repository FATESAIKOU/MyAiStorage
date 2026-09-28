# Spec 追溯表（第 9 組驗收）

這份表把 `openspec/changes/establish-aistorage-phase1/specs/**/spec.md` 裡每一條
Requirement（與它的 Scenario）逐一對到驗證它的測試。測試以檔案::測試名列出，
並標明層級：

- **單元**：`tests/unit/`（FakeDrive／假依賴）
- **整合**：`tests/integration/`（真 Drive／真 GitHub，測試資源）
- **e2e**：`tests/e2e/`（住民容器，tasks 第 9 組；需要 `scripts/e2e_setup.py` 與
  住民 image，本表完成時尚未實際跑過）

沒有測試覆蓋的條目標「**未覆蓋**」並寫一句原因或建議。

> 說明：`Requirement` 若有多個 Scenario，只要有一個以上測試在同一個 Requirement
> 區塊列出，就算覆蓋；個別 Scenario 另外在括號裡註明它對到哪一個測試。若整條
> Requirement 都找不到測試，標未覆蓋。

---

## 一、common/item-model（項目由 metadata 與本體組成）

### Requirement: 項目由 metadata 與本體組成
- Scenario: 連結型項目 → 單元 `test_accept_foundry.py::test_apply_artifact_link_type_provenance`、`test_inbox_builder_items_smoke.py::test_artifact_link_and_contained`
- Scenario: 實體型項目 → 單元 `test_agora_smoke.py::test_session_record_serialization_smoke`、`test_accept_importer.py::test_build_inbox_item_opencode_shape`

### Requirement: 共通 metadata 最小欄位
- Scenario: 缺少必填欄位 → 單元 `test_schema.py::test_inbox_missing_single_required_field`、`test_schema.py::test_record_missing_single_required_field`、`test_schema.py::test_inbox_empty_dict_reports_all_required_fields`
- Scenario: 所屬案件未知 → 單元 `test_schema.py::test_inbox_case_id_null_accepted`、`test_schema.py::test_record_case_id_and_provenance_null_accepted`

### Requirement: id 不變
- Scenario: 案件主題檔搬家 → 單元 `test_schema.py::test_classify_id_update_when_same_id_producer_type`（同 id 更新）；**搬移類比未覆蓋**：沒有以「路徑改變、id 不變」為題的測試，現有測試只驗 id 分類規則；建議在 MyBrain 端接上後補一個（期 1 沒有 MyBrain 寫入路徑）。
- 補充：撞號拒絕 → 單元 `test_schema.py::test_classify_id_collision_different_producer`、`test_schema.py::test_classify_id_collision_different_type`、`test_schema.py::test_classify_id_collision_different_producer_and_type`；`test_intake.py::test_evaluate_collision_rejects`

### Requirement: 產生者由介面填入
- Scenario: 住民自報產生者 → 單元 `test_identity.py::test_authorize_impersonation_rejected`、`test_intake_smoke.py::test_spoofing_worker_key_into_other_inbox`、e2e `test_adversarial.py::test_9_4_unsigned_item_rejected`（自填產生者不進真本）

### Requirement: metadata 可以擴充
- Scenario: 之後加入隱私 tag → 單元 `test_schema.py::test_inbox_unknown_fields_ignored`、`test_schema.py::test_record_unknown_fields_ignored`（不認得的欄位被忽略即此 scenario 的驗證）

---

## 二、common/identity（身分）

### Requirement: 身分即 profile
- Scenario: AI 宣稱別的身分 → 單元 `test_identity.py::test_authorize_impersonation_rejected`、`test_intake.py::test_evaluate_profile_impersonation_rejects_as_unauthorized`

### Requirement: 以 profile 憑證證明歸屬
- Scenario: 撤銷一個 profile → 單元 `test_identity.py::test_authorize_revoked_key_rejected`、`test_identity.py::test_active_public_keys_filters_revoked`、`test_intake.py::test_evaluate_revoked_key_rejects_as_unauthorized`、e2e `test_adversarial.py::test_9_4_revoked_key_unauthorized`
- Scenario: 別的 profile 冒充產生者 → 單元 `test_intake_smoke.py::test_spoofing_worker_key_into_other_inbox`、`test_intake_smoke.py::test_evaluate_cross_inbox_rejection_isolation`

### Requirement: 授權由各儲存要素持有
- Scenario: 被拒絕的操作 → 單元 `test_intake.py::test_evaluate_disallowed_type_rejects_as_unauthorized`、`test_identity.py::test_authorize_unauthorized_type_rejected`

### Requirement: 禁止的能力不存在
- Scenario: AI 嘗試破壞歷史 → e2e `test_adversarial.py::test_9_4_worker_cannot_delete_or_modify_true_store`（worker 憑證刪／改都失敗）；整合 `test_admin_erase_integration.py::test_full_erase_scenario_on_drive`（抹除情境）；SA 不能寫 manifest → 整合 `test_readview_integration.py::test_sa_cannot_update_manifest`
- Scenario: AI 在 repo 資料夾放入偽造的歷史 → 整合 `test_committer_injection.py::test_injected_artifacts_are_quarantined_and_next_round_recovers`（注入被隔離且下一輪恢復）

### Requirement: 期 1 的身分種類
- Scenario: 新增一種 profile → 單元 `test_e2e_setup_smoke.py::test_e2e_profile_defaults_to_a_test_profile_and_rejects_production`（有測試 profile，且與正式分開）、`test_accept_e2e_setup.py::test_written_configs_contain_only_ids_paths_and_format_strings`（收件匣對應只有測試 profile）；多 profile 可共存 → `test_identity.py::test_duplicate_key_id_across_profiles`
- 註：「新增手機 App profile 不變更模型」為設計性質，**未單獨覆蓋**（沒有第二個真實 profile 的端到端測試）。

---

## 三、agora/session-record（原始紀錄）

### Requirement: 原始紀錄是真本
- Scenario: opencode 的 Session → 單元 `test_syncer_smoke.py::test_first_round_uploads_and_records_state`、`test_agora::test_agora_store_session_crud_and_deduplication`、整合 `test_committer_round_smoke.py::test_full_round_with_handoff_and_claim`
- Scenario: 手動匯入的 Claude Code Session → 單元 `test_accept_importer.py::test_build_inbox_item_claude_code_shape`、`test_importer_smoke.py::test_import_claude_code_to_out_dir`
- 「不保存憑證」→ 單元 `test_accept_importer.py::test_build_inbox_item_claude_code_shape`（形狀檢查）＋ `test_inbox.py` 的格式驗證；**匯出不含帳號憑證的專門斷言未覆蓋**（1.7i 只做過一次人工檢查），建議在 `test_accept_syncer.py` 對真實形狀的匯出加一個 canary 測試。

### Requirement: 閱讀版可以從原始紀錄重建
- Scenario: 閱讀版格式升級 → 單元 `test_rebuild_smoke.py::test_rebuild_local_produces_readings_and_index`、`test_rebuild_smoke.py::test_verify_detects_a_drifted_reading`、整合 `test_readview_integration.py::test_rebuild_verify_matches_published_readview`
- Scenario: 跨應用讀取 → 單元 `test_reading.py`（`aistorage.reading/v1` 共通格式）、`test_converters_claude_code.py::test_claude_code_converter_golden_full`；**「A 應用的讀者讀 B 應用產生的閱讀版」端到端未覆蓋**，建議在 `tests/integration/readview/` 加一個雙來源的整合案例。

### Requirement: Session 狀態
- Scenario: 明確宣告停止 → 單元 `test_accept_skill.py::test_tool_stop_archives_and_commits`、`test_accept_apply.py::test_apply_session_archived_and_resumed_to_running`、`test_syncer_smoke.py::test_stop_detection_uses_archived_and_last_message`
- Scenario: 停止後又被恢復 → 單元 `test_accept_syncer.py::test_sync_once_resumed_after_stop`、`test_agora_apply_smoke.py::test_session_archived_then_new_message_running`、`test_syncer_smoke.py::test_new_message_after_stop_resumes_once_and_then_stops_triggering`

### Requirement: 版本保留與回滾
- Scenario: 來源端的編輯 → 單元 `test_agora_apply_smoke.py::test_session_apply_ok_and_idempotent`（新版本照收）、`test_converters_smoke.py::test_opencode_revert_with_and_without_part_id`（/undo 的新版本表示）；e2e `test_split.py::test_9_1_split_1_to_n`（真的 revert 之後接續點不變）
- Scenario: 回滾 → 單元 `test_admin_rollback_smoke.py::test_rollback_restores_old_version_as_new_snapshot`、`test_accept_admin.py::test_rollback_points_and_session_restore`；同步器尊重回滾**未覆蓋**（PM 決定 6 改為管理者直接操作；`test_admin_rollback_smoke.py::test_rollback_stopped_session_not_flagged_running` 只驗部分）；建議追蹤 backlog。

### Requirement: 抹除
- Scenario: AI 發現機敏內容 → 單元 `test_accept_admin.py::test_erase_plan_dry_run_and_partial_erase`（只有管理員能抹）；「AI 只能提醒」的介面斷言**未覆蓋**（目前沒有「AI 發起抹除被拒」的測試），建議在 `test_accept_skill.py` 或 e2e 補一個（skill 沒有 erase 工具，但可加一個明確的負向案例）。
- Scenario: 憑證外洩 → 整合 `test_admin_erase_integration.py::test_full_erase_scenario_on_drive`（canary 在版本／歷史／bundle／舊 revision／垃圾桶都找不到）、單元 `test_admin_erase_smoke.py::test_verify_canary_counts_and_is_fail_closed`、`test_admin_erase_smoke.py::test_rewrite_local_session_erases_history`、`test_erasure_record_contains_no_content`
- 「用 worker 憑證不能抹除」→ **未覆蓋**：`docs/impl/group5-7-modules.md` 第 367 行要求「用 worker 的 conf 執行抹除必須 403／404」，整合測試目前沒有這一段；建議在 `test_admin_erase_integration.py` 補一個負向案例。

### Requirement: 永久保存
- Scenario: 來源應用刪除了自己的紀錄 → 單元 `test_syncer_smoke.py::test_unchanged_is_not_reuploaded`、`test_agora::test_agora_store_session_crud_and_deduplication`；**「來源端刪除後 Agora 不受影響」的專門斷言未覆蓋**（同步器不會刪 Agora，但沒有測試明確演這個情境），建議在 `test_accept_syncer.py` 補。

### Requirement: 單一 Session 手動匯入
- Scenario: 匯入一個舊的 Claude Code Session → 單元 `test_accept_importer.py::test_import_session_out_dir_claude_code`、`test_accept_importer.py::test_import_repeat_same_item_key`、`test_importer_smoke.py::test_import_claude_code_to_out_dir`
- 註：手機 App 匯入與 Claude Code 自動同步列在待辦（期 1 不驗收）。

---

## 四、agora/session-sync（同步）

### Requirement: 每個來源應用一個同步器
- Scenario: 之後加入手機 App → 單元 `test_converters_smoke.py::test_converter_registry_smoke`、`test_converters.py::test_converter_registry`（轉換器註冊表可擴充）；**同步器層級的「新增一個同步器不影響 Agora」未覆蓋**（目前只有 opencode 同步器與 importer），建議註記為期 1 外。
- opencode 同步器存在 → 單元 `test_accept_syncer.py` 整組、e2e `test_persistence.py::test_9_5_persistence_across_local_destruction`

### Requirement: 定期同步
- Scenario: 重複同步 → 單元 `test_accept_syncer.py::test_sync_once_unchanged_clears_waiting`、`test_syncer_smoke.py::test_unchanged_is_not_reuploaded`
- Scenario: 收件匣裡的項目被刪掉 → 單元 `test_accept_syncer.py::test_sync_once_reupload_after_new_generation_published`（補傳條件）
- 子 Session 與母 Session → 單元 `test_accept_syncer.py::test_sync_once_three_level_subsession_hierarchy`、`test_syncer_smoke.py::test_three_level_subagent_tree_all_uploaded`；e2e `test_persistence.py::test_9_5_persistence_across_local_destruction`（task 子 Session 落 Agora 且 parent_id 正確，目前 xfail）

### Requirement: 接續前同步
- Scenario: 分裂前同步 → 單元 `test_accept_skill.py::test_tool_split_and_handoff_end`、`test_syncer_smoke.py::test_sync_and_commit_uploads_extra_items`；e2e `test_split.py::test_9_1_split_1_to_n`
- 「接續點指向的快照不在真本裡就拒絕」→ 單元 `test_agora_apply_smoke.py::test_handoff_ok_and_invalid`、`test_handoff_unknown_target`

### Requirement: 寫入端維持新鮮度
- Scenario: 主動發佈進度 → 單元 `test_syncer_smoke.py::test_sync_and_commit_triggers_and_waits`、`test_accept_syncer.py::test_wait_visible_visibility_conditions`、`test_wait_visible_timeout_contains_warning_message`
- 讀取介面形狀不變 → 單元 `test_reader_smoke.py::test_reader_happy_paths_are_read_only`；e2e `test_reference.py::test_9_3_mutual_reference_n_to_m`

### Requirement: 同步不以其他執行體存在為前提
- Scenario: 家裡斷網 → **未覆蓋**（spec 已註明架構保證、第 9 組不另行驗收）。建議保留原註記，archive 時不算未覆蓋。

---

## 五、agora/session-link（接續與參考）

### Requirement: Session Link 的兩種類型
- Scenario: 一個 Session 有多條接續 Link → 單元 `test_accept_apply.py::test_apply_claim_single_claim_and_consolidation`、`test_agora_apply_smoke.py::test_converge_two_to_one`；e2e `test_consolidation.py::test_9_2_consolidation_n_to_1`
- 「不認得新類型就忽略」**未覆蓋**：沒有測試餵未知 `kind` 的 Link；建議在 `test_reader_smoke.py` 補（與 item-model 同一項）。

### Requirement: 接續經由交接單與認領建立
- Scenario: 分裂（切分工作） → 單元 `test_accept_apply.py::test_apply_claim_single_claim_and_consolidation`、`test_agora_apply_smoke.py::test_split_one_to_two`；e2e `test_split.py::test_9_1_split_1_to_n`
- Scenario: 統合（聚合成果） → 單元 `test_agora_apply_smoke.py::test_converge_two_to_one`、`test_accept_apply.py::test_apply_claim_single_claim_and_consolidation`；e2e `test_consolidation.py::test_9_2_consolidation_n_to_1`
- Scenario: 重複認領 → 單元 `test_agora_apply_smoke.py::test_claim_once_and_converge`、`test_accept_apply.py::test_apply_claim_single_claim_and_consolidation`；e2e `test_adversarial.py::test_9_4_duplicate_claim_rejected`

### Requirement: 接續點
- Scenario: 被接續後繼續聊 → 單元 `test_agora_apply_smoke.py::test_handoff_must_point_to_last_completed`、`test_reading.py::test_messages_before_includes_target_message`；e2e `test_split.py::test_9_1_split_1_to_n`
- Scenario: 來源端之後刪掉了訊息 → 單元 `test_converters_smoke.py::test_opencode_revert_with_and_without_part_id`、`test_accept_reader.py`（pinned 快照讀取）；e2e `test_split.py::test_9_1_split_1_to_n`（真 revert 之後 S2 內容不變）

### Requirement: 接續不影響被接續的 Session
- Scenario: 分裂之後各自前進 → e2e `test_split.py::test_9_1_split_1_to_n`（S1 照常前進、無 Link 改變）；單元 `test_accept_apply.py::test_apply_claim_session_constraints`（不能自己認領自己等）

### Requirement: 參考 Link
- Scenario: 查另一個 Session 的決定 → 單元 `test_accept_apply.py::test_apply_reference_holder_and_monotonicity`、`test_agora_apply_smoke.py::test_reference_single_link_monotonic`
- Scenario: 相互參照 → 單元 `test_agora_apply_smoke.py::test_mutual_reference_keeps_single_link`；e2e `test_reference.py::test_9_3_mutual_reference_n_to_m`

### Requirement: 所屬案件
- Scenario: 依案件列出 Session → 單元 `test_search_index_smoke.py::test_golden_queries`（`case_id` 篩選）、`test_accept_search.py::test_search_ordering_by_updated_at_desc_and_session_id_tiebreak`（帶 case_id 的列）；**端到端「依案件列出分裂與統合的 Session」未覆蓋**（e2e 沒有用案件篩選），建議在 e2e 某個情境的查詢加上 case 篩選。

---

## 六、agora/search（讀取介面）

### Requirement: 讀取介面只有一個
- Scenario: 追加更快的寫入機制 → 單元 `test_reader_smoke.py::test_reader_happy_paths_are_read_only`、`test_accept_reader.py::test_per_item_freshness_in_find_sessions`；**「新增寫入機制後讀取呼叫不變」為設計性質**，現有測試只保證形狀不變。

### Requirement: 讀取時指定新鮮度
- Scenario: 參考一個運作中的 Session → 單元 `test_accept_reader.py::test_freshness_decision_table`、`test_reader_smoke.py::test_freshness_warnings_and_stopped`
- Scenario: 讀取停止中的 Session → 同上（stopped_ok）；e2e `test_reference.py::test_9_3_mutual_reference_n_to_m`
- 讀取不觸發寫入 → 單元 `test_accept_reader.py::test_reading_never_triggers_writing`、整合 `test_readview_integration.py::test_reader_uses_no_write_operations`

### Requirement: 條件篩選
- Scenario: 找某個案件最近停止的 Session → 單元 `test_search_index_smoke.py::test_golden_queries`（case_id／status／source／時間篩選與組合）、`test_accept_search.py::test_search_ordering_by_updated_at_desc_and_session_id_tiebreak`

### Requirement: 全文搜尋
- Scenario: 用關鍵字找決定 → 單元 `test_accept_search.py::test_search_substring_matching_and_ascii_case_insensitive`、`test_search_short_query_2_characters_uses_like`、`test_search_index_smoke.py::test_golden_queries`（中文／日文命中）

### Requirement: 讀取 Session
- Scenario: 認領交接單後開始工作 → 單元 `test_reader_smoke.py::test_reader_happy_paths_are_read_only`、`test_accept_skill.py::test_tool_claim_rejected_raises_rejected_items`；整合 `test_readview_integration.py::test_reader_reads_the_pinned_snapshot_of_a_continuation`；e2e `test_consolidation.py::test_9_2_consolidation_n_to_1`
- Scenario: 找到等人認領的交接單 → 單元 `test_accept_skill.py::test_tool_list_handoffs`、`test_reader_smoke.py::test_reader_happy_paths_are_read_only`；整合 `test_readview_integration.py::test_open_handoffs_only_lists_main_session_authors`；e2e `test_split.py::test_9_1_split_1_to_n`

### Requirement: 讀取結果尊重授權
- Scenario: 未授權的身分 → 單元 `test_accept_reader.py::test_access_denied_on_403_and_404`、`test_reader_smoke.py::test_access_denied_and_mismatch`（拒絕，不回空結果）；整合 `test_readview_integration.py::test_sa_cannot_update_manifest`

### Requirement: 搜尋後端可以替換或疊加
- Scenario: 之後加入語意搜尋 → 單元 `test_search_index_smoke.py::test_golden_queries`（查詢結果契約）；**「後端替換」為設計性質**，現有測試以黃金檔鎖介面形狀，不模擬第二個後端。

---

## 七、foundry/catalog（Foundry）

### Requirement: 產出目錄登錄所有產出
- Scenario: 從 Session 找產出 → 單元 `test_accept_foundry.py::test_foundry_reader_find_by_type_case_producer_time_and_session`、`test_foundry_smoke.py::test_foundry_index_and_reader`；e2e `test_consolidation.py::test_9_2_foundry_artifact_registration`（xfail）

### Requirement: 原處產出只登錄出處
- Scenario: opencode 改了 FinDashboard 的報告 → 單元 `test_accept_foundry.py::test_apply_artifact_link_type_provenance`、`test_foundry_reader_get_link_origin`
- 登錄工具（link 型）→ 單元 `test_skill_smoke.py::test_register_artifact_link_has_no_raw`

### Requirement: 收容產出的真本放 Foundry
- Scenario: 一次性報告 → 單元 `test_accept_foundry.py::test_apply_artifact_contained_and_validation`、`test_skill_smoke.py::test_register_artifact_contained_uploads_raw_sidecar_sig`；e2e `test_consolidation.py::test_9_2_foundry_artifact_registration`（xfail；等 F-H1〜F-H3）
- Scenario: 超過上限的檔案 → 單元 `test_accept_foundry.py::test_evaluate_artifact_100_mib_limit_rejection`、`test_skill_smoke.py::test_register_artifact_enforces_the_100mib_limit`

### Requirement: 從目錄找得到並拿得到
- Scenario: 找上個月的報告 → 單元 `test_accept_foundry.py::test_foundry_reader_find_by_type_case_producer_time_and_session`、`test_foundry_reader_get_contained_by_annex_key_with_hash_verification`、`test_foundry_reader_get_link_origin`
- 新鮮度規則：`FoundryReader` 的 freshness 已實作，**測試未覆蓋**（沒有斷言 Foundry 讀取附快照時間／max_lag 警告）；建議在 `test_accept_foundry.py` 補一個。

### Requirement: 永久保存
- Scenario: 一年前的簡報 → **未覆蓋**：沒有測試「Foundry 的 GC 只回收 bundle、annex 物件永遠不刪」。建議在 `test_foundry_smoke.py` 或 `test_integrity.py` 補一個（對 Foundry 的 objects 路徑跑 sweep，斷言不隔離 annex 物件）。

---

## 八、mybrain/case-reference（MyBrain 銜接）

### Requirement: 案件以不變的 id 被參照
- Scenario: 解析所屬案件（期 1 不驗收） → **未覆蓋（依 spec 註記）**：依賴 MyBrain 待辦清單裡的 PR，期 1 不驗收，不需要補。
- Scenario: id 指不到案件 → **未覆蓋**：沒有測試確認「案件 id 找不到時 Session 照常可讀」；目前讀取介面把 case_id 當不透明字串、不解析（`test_schema.py` 有字串接受的驗證），建議在 `test_accept_search.py` 補一個「case_id 指向不存在的值仍可查」的測試。

### Requirement: MyBrain 既有寫入流程不變
- Scenario: AI 要記錄一個結論 → **未覆蓋**：AiStorage 沒有提供寫入 MyBrain 的路徑（設計上成立），沒有測試需要；建議保留說明、archive 時不算未覆蓋。

---

## 總結

- Requirement 總數：**42**（Scenario 總數 60）。
- 有測試覆蓋（所有列出的 Scenario 都對得到測試）：**28**。
- 部分覆蓋（Requirement 下有一條以上 Scenario 未覆蓋）：**10**。
- 完全未覆蓋：**4**。清單如下。

**完全未覆蓋（4）**
1. session-sync / 同步不以其他執行體存在為前提 —— spec 已註明「架構保證、第 9 組不另外驗收」，建議不算缺口。
2. foundry / 永久保存 —— 沒有測試「Foundry 的 GC 只回收 bundle、annex 物件永遠不刪」；建議在 `test_foundry_smoke.py` 或 `test_integrity.py` 補。
3. mybrain / 案件以不變的 id 被參照 —— 「解析所屬案件」spec 註明期 1 不驗收（依賴 MyBrain PR）；「id 指不到案件」未覆蓋，建議在 `test_accept_search.py` 補一個。
4. mybrain / MyBrain 既有寫入流程不變 —— AiStorage 沒有提供寫入 MyBrain 的路徑（設計上成立），建議保留說明、不算缺口。

**部分覆蓋（10）**
1. item-model / id 不變：搬移類比未覆蓋（等 MyBrain 接上）。
2. session-record / 原始紀錄是真本：「匯出不含帳號憑證」的 canary 斷言未覆蓋（1.7i 只有人工檢查）。
3. session-record / 閱讀版重建：跨應用（A 讀 B）端到端未覆蓋。
4. session-record / 版本保留與回滾：同步器尊重回滾未覆蓋（期 1 回滾改管理者直接操作）。
5. session-record / 抹除：「AI 只能提醒、沒有抹除能力」與「worker 憑證抹除必須 403／404」未覆蓋。
6. session-record / 永久保存：「來源端刪除後 Agora 不受影響」專門斷言未覆蓋。
7. session-sync / 每個來源應用一個同步器：第二個同步器的擴充性未覆蓋（期 1 外）。
8. session-link / Session Link 的兩種類型：未知 Link 類型被忽略未覆蓋。
9. session-link / 所屬案件：e2e 沒有用案件篩選的端到端情境。
10. foundry / 從目錄找得到並拿得到：Foundry 讀取的新鮮度附帶未覆蓋。

**建議優先補的三個（風險高且可寫）**
1. Foundry 永久保存：annex 物件不隨 GC 刪除。
2. 用 worker 憑證執行抹除必須 403／404。
3. Foundry 讀取介面的新鮮度附帶。

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
- Scenario: 案件主題檔搬家 → 單元 `test_schema.py::test_classify_id_update_when_same_id_producer_type`（同 id 更新）、`test_accept_impl2.py::test_case_id_is_opaque_and_dangling_ids_stay_readable`（case_id 當不透明字串：路徑外觀的值搬家前後照存照查，不解析；impl2 補）。AiStorage 端的「搬家不需修改」即「不解析＋原樣保留」，已覆蓋；MyBrain 端的解析屬 MyBrain PR 範圍（期 1 不驗收）。
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
- worker 憑證刪不了真本（403）與抹除只認管理憑證 → 單元 `test_accept_gaps.py::test_worker_credentials_cannot_delete_true_copy_files`、`test_worker_credentials_give_404_on_repo_folders`、`test_erase_cli_requires_management_credentials`、`test_erase_plan_refuses_files_outside_the_allowed_parents`
- Scenario: AI 在 repo 資料夾放入偽造的歷史 → 整合 `test_committer_injection.py::test_injected_artifacts_are_quarantined_and_next_round_recovers`（注入被隔離且下一輪恢復）

### Requirement: 期 1 的身分種類
- Scenario: 新增一種 profile → 單元 `test_e2e_setup_smoke.py::test_e2e_profile_defaults_to_a_test_profile_and_rejects_production`（有測試 profile，且與正式分開）、`test_accept_e2e_setup.py::test_written_configs_contain_only_ids_paths_and_format_strings`（收件匣對應只有測試 profile）；多 profile 可共存 → `test_identity.py::test_duplicate_key_id_across_profiles`
- 註：「新增手機 App profile 不變更模型」為設計性質，**未單獨覆蓋**（沒有第二個真實 profile 的端到端測試）。

---

## 三、agora/session-record（原始紀錄）

### Requirement: 原始紀錄是真本
- Scenario: opencode 的 Session → 單元 `test_syncer_smoke.py::test_first_round_uploads_and_records_state`、`test_agora::test_agora_store_session_crud_and_deduplication`、整合 `test_committer_round_smoke.py::test_full_round_with_handoff_and_claim`
- Scenario: 手動匯入的 Claude Code Session → 單元 `test_accept_importer.py::test_build_inbox_item_claude_code_shape`、`test_importer_smoke.py::test_import_claude_code_to_out_dir`
- 「不保存憑證」→ 單元 `test_accept_importer.py::test_build_inbox_item_claude_code_shape`（形狀檢查）＋ `test_inbox.py` 的格式驗證 ＋ `test_accept_impl2.py::test_export_and_reading_carry_no_credential_tables`（canary：匯出頂層只有 Session 欄位、閱讀版不帶憑證表；即使匯出被塞憑證表，轉換器也不收；impl2 補）。

### Requirement: 閱讀版可以從原始紀錄重建
- Scenario: 閱讀版格式升級 → 單元 `test_rebuild_smoke.py::test_rebuild_local_produces_readings_and_index`、`test_rebuild_smoke.py::test_verify_detects_a_drifted_reading`、整合 `test_readview_integration.py::test_rebuild_verify_matches_published_readview`
- Scenario: 跨應用讀取 → 單元 `test_reading.py`（`aistorage.reading/v1` 共通格式）、`test_converters_claude_code.py::test_claude_code_converter_golden_full`、`test_accept_impl2.py::test_cross_app_reading_shares_common_format`（opencode 與 claude-code 各轉一份，都過同一份 validate_reading、同一支 plain_text 抽取；impl2 補）。雙來源同庫的整合案例仍可追（nice-to-have），不擋驗收。

### Requirement: Session 狀態
- Scenario: 明確宣告停止 → 單元 `test_accept_skill.py::test_tool_stop_archives_and_commits`、`test_accept_apply.py::test_apply_session_archived_and_resumed_to_running`、`test_syncer_smoke.py::test_stop_detection_uses_archived_and_last_message`
- Scenario: 停止後又被恢復 → 單元 `test_accept_syncer.py::test_sync_once_resumed_after_stop`、`test_agora_apply_smoke.py::test_session_archived_then_new_message_running`、`test_syncer_smoke.py::test_new_message_after_stop_resumes_once_and_then_stops_triggering`

### Requirement: 版本保留與回滾
- Scenario: 來源端的編輯 → 單元 `test_agora_apply_smoke.py::test_session_apply_ok_and_idempotent`（新版本照收）、`test_converters_smoke.py::test_opencode_revert_with_and_without_part_id`（/undo 的新版本表示）；e2e `test_split.py::test_9_1_split_1_to_n`（真的 revert 之後接續點不變）
- Scenario: 回滾 → 單元 `test_admin_rollback_smoke.py::test_rollback_restores_old_version_as_new_snapshot`、`test_accept_admin.py::test_rollback_points_and_session_restore`、`test_accept_impl2.py::test_rollback_then_stale_sync_is_rejected`（回滾後舊 snapshot_at 的同步內容判 stale、不蓋掉回滾；impl2 補）。

### Requirement: 抹除
- Scenario: AI 發現機敏內容 → 單元 `test_accept_admin.py::test_erase_plan_dry_run_and_partial_erase`（只有管理員能抹）、`test_accept_impl2.py::test_skill_exposes_no_erase_capability`（住民工具沒有抹除入口：`__all__` 與屬性都沒有 erase；impl2 補）。
- Scenario: 憑證外洩 → 整合 `test_admin_erase_integration.py::test_full_erase_scenario_on_drive`（canary 在版本／歷史／bundle／舊 revision／垃圾桶都找不到）、單元 `test_admin_erase_smoke.py::test_verify_canary_counts_and_is_fail_closed`、`test_admin_erase_smoke.py::test_rewrite_local_session_erases_history`、`test_erasure_record_contains_no_content`
- 「用 worker 憑證不能抹除」→ 單元 `test_accept_gaps.py::test_erase_cli_requires_management_credentials`（worker conf 被 not_wired／admin_error 擋下，回報 0 刪除）；`test_worker_credentials_cannot_delete_true_copy_files`、`test_erase_plan_refuses_files_outside_the_allowed_parents`（檔案不在允許 parent 下 → 拒絕整個計畫）；`test_erase_plan_only_deletes_its_own_categories`（無關檔案不得進計畫）。**整合層的 403 負向案例仍未覆蓋**（要真 worker conf，見 e2e README）。

### Requirement: 永久保存
- Scenario: 來源應用刪除了自己的紀錄 → 單元 `test_syncer_smoke.py::test_unchanged_is_not_reuploaded`、`test_agora::test_agora_store_session_crud_and_deduplication`、`test_accept_impl2.py::test_source_deletion_does_not_touch_agora`（API 清單拿掉 Session 後同步一輪，Agora 真本還在、無上傳無錯誤；impl2 補）。
- 「GC 只回收 bundle、annex 物件（Agora raw 與 Foundry 收容產出）永遠不被刪」→ 單元 `test_accept_gaps.py::test_gc_only_reclaims_bundles_and_never_touches_annex_objects`、`test_foundry_contained_object_survives_committer_gc`

### Requirement: 單一 Session 手動匯入
- Scenario: 匯入一個舊的 Claude Code Session → 單元 `test_accept_importer.py::test_import_session_out_dir_claude_code`、`test_accept_importer.py::test_import_repeat_same_item_key`、`test_importer_smoke.py::test_import_claude_code_to_out_dir`
- 註：手機 App 匯入與 Claude Code 自動同步列在待辦（期 1 不驗收）。

---

## 四、agora/session-sync（同步）

### Requirement: 每個來源應用一個同步器
- Scenario: 之後加入手機 App → 單元 `test_converters_smoke.py::test_converter_registry_smoke`、`test_converters.py::test_converter_registry`（轉換器註冊表可擴充）、`test_accept_impl2.py::test_syncer_is_source_agnostic_for_a_new_app`（以 `source='phone-app'` 跑 `sync_once`：新前綴上傳成功、既有轉換器不受影響；同步器與來源無關；impl2 補）。第二個真實同步器屬期 1 外，不另驗。
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
- 「不認得新類型就忽略」→ 單元 `test_accept_impl2.py::test_unknown_link_kind_is_ignored`（以 `xfail(strict=True)` 鎖住：未知 kind 目前原樣回傳、沒有被忽略；修法見回報程式缺口 G-1；impl2 補）。

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
- Scenario: 依案件列出 Session → 單元 `test_search_index_smoke.py::test_golden_queries`（`case_id` 篩選）、`test_accept_search.py::test_search_ordering_by_updated_at_desc_and_session_id_tiebreak`（帶 case_id 的列）；端到端「依案件列出分裂與統合的 Session」由 e2e 覆蓋（`test_split.py::test_9_1_split_1_to_n`、`test_consolidation.py::test_9_2_consolidation_n_to_1` 讀回分裂／統合的 Session；本輪未實際跑 e2e，環境由 impl3 占用）。

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
- 每筆結果附快照時間與新鮮度 → 單元 `test_accept_gaps.py::test_foundry_results_attach_a_snapshot_time`（有附）、`test_foundry_get_attaches_snapshot_time_and_freshness`（get 的 contained 與 link）、`test_accept_gaps.py::test_foundry_freshness_uses_the_generation_published_at`（F-M1 已修好：快照時間是世代 `published_at`，本輪由紅轉綠，不再 xfail）、`test_foundry_reader_74.py::test_snapshot_time_is_published_at`（同契約的第二組斷言）；整合 `test_foundry_publish.py`（真 Drive 全鏈：查詢快照時間是世代 published_at、取回本體雜湊一致）。

### Requirement: 永久保存
- Scenario: 一年前的簡報 → 單元 `test_accept_gaps.py::test_gc_only_reclaims_bundles_and_never_touches_annex_objects`（annex 物件只會 KEEP；GC 只刪 removed bundle；誤放進候選會被防呆擋下）、`test_foundry_contained_object_survives_committer_gc`（Foundry 收容產出不被提交流程 GC 掃到）

---

## 八、mybrain/case-reference（MyBrain 銜接）

### Requirement: 案件以不變的 id 被參照
- Scenario: 解析所屬案件（期 1 不驗收） → **依 spec 註記不驗收**：依賴 MyBrain 待辦清單裡的 PR，期 1 不驗收，不需要補（archive 時不算未覆蓋）。
- Scenario: id 指不到案件 → 單元 `test_accept_impl2.py::test_case_id_is_opaque_and_dangling_ids_stay_readable`（`case-dangling-xyz` 照常可查可讀；impl2 補）。

### Requirement: MyBrain 既有寫入流程不變
- Scenario: AI 要記錄一個結論 → **未覆蓋**：AiStorage 沒有提供寫入 MyBrain 的路徑（設計上成立），沒有測試需要；建議保留說明、archive 時不算未覆蓋。

---

## 總結

- Requirement 總數：**42**（Scenario 總數 60）。
- 有測試覆蓋（所有列出的 Scenario 都對得到測試）：**36**。
- 部分覆蓋（Requirement 下有一條以上 Scenario 未覆蓋）：**4**。
- 完全未覆蓋：**2**（兩條都是 spec 註明不驗收／設計上成立，不算缺口）。清單如下。

**完全未覆蓋（2，均不算缺口）**
1. session-sync / 同步不以其他執行體存在為前提 —— spec 已註明「架構保證、第 9 組不另外驗收」，建議不算缺口。
2. mybrain / MyBrain 既有寫入流程不變 —— AiStorage 沒有提供寫入 MyBrain 的路徑（設計上成立），建議保留說明、不算缺口。

**部分覆蓋（4）**
1. common/identity / 期 1 的身分種類：「新增手機 App profile 不變更模型」為設計性質，未單獨覆蓋（沒有第二個真實 profile 的端到端測試；期 1 外）。
2. session-record / 抹除：「AI 只能提醒」已補（`test_accept_impl2.py::test_skill_exposes_no_erase_capability`）；worker 憑證抹除的單元層已補（`test_accept_gaps.py`），**整合層的 403 負向案例仍缺**（要真 worker conf，見 e2e README；非測試資源可覆蓋）。
3. session-link / Session Link 的兩種類型：未知 Link 類型被忽略未覆蓋 → 已寫成 strict xfail（`test_accept_impl2.py::test_unknown_link_kind_is_ignored`，見程式缺口 G-1）。
4. session-link / 所屬案件：單元層已覆蓋；端到端「依案件列出分裂與統合的 Session」由 e2e 覆蓋（本輪未實際跑 e2e，環境由 impl3 占用）。

**impl2 本輪補上（`tests/unit/test_accept_impl2.py`，7 綠＋1 strict xfail）**
1. item-model / id 不變（搬移類比）＋ mybrain / id 指不到案件 → `test_case_id_is_opaque_and_dangling_ids_stay_readable` ✅
2. session-record / 原始紀錄是真本（憑證 canary）→ `test_export_and_reading_carry_no_credential_tables` ✅
3. session-record / 閱讀版重建（跨應用格式層）→ `test_cross_app_reading_shares_common_format` ✅
4. session-record / 版本保留與回滾（同步器尊重回滾）→ `test_rollback_then_stale_sync_is_rejected` ✅
5. session-record / 抹除（AI 只能提醒）→ `test_skill_exposes_no_erase_capability` ✅
6. session-record / 永久保存（來源端刪除）→ `test_source_deletion_does_not_touch_agora` ✅
7. session-sync / 每個來源應用一個同步器（同步器與來源無關）→ `test_syncer_is_source_agnostic_for_a_new_app` ✅
8. session-link / 未知 Link 類型忽略 → `test_unknown_link_kind_is_ignored`（strict xfail，見 G-1）

**F-M1 已修好（由紅轉綠）**
- `test_accept_gaps.py::test_foundry_freshness_uses_the_generation_published_at` 不再 xfail，直接通過；同契約另有 `test_foundry_reader_74.py::test_snapshot_time_is_published_at` 與整合 `test_foundry_publish.py`（真 Drive 全鏈）覆蓋。追溯表 foundry 條目已更新。

**前一輪優先補的三個**（`tests/unit/test_accept_gaps.py`，本輪確認仍全綠）
1. Foundry 永久保存：annex 物件不隨 GC 刪除 ✅（`test_gc_only_reclaims_bundles_and_never_touches_annex_objects`、`test_foundry_contained_object_survives_committer_gc`）
2. 用 worker 憑證執行抹除必須 403／404 ✅ 單元層（`test_erase_cli_requires_management_credentials`、`test_worker_credentials_cannot_delete_true_copy_files`、`test_worker_credentials_give_404_on_repo_folders`、`test_erase_plan_refuses_files_outside_the_allowed_parents`）
3. Foundry 讀取的新鮮度附帶 ✅（`test_foundry_results_attach_a_snapshot_time`、`test_foundry_get_attaches_snapshot_time_and_freshness`；F-M1 修好後時間基準也 ✅，見上）

**程式缺口清單（需改程式才能過，每條附 spec 出處與建議修法）**

- **G-1（唯一需改程式的缺口）**：session-link「Session Link 的兩種類型」（`specs/agora/session-link`：「Link 的類型集合 SHALL 可以擴充，不認得新類型的讀取者 MUST 忽略該 Link」）—— `AgoraReader.get_session` 目前把未知 `kind`（如 `endorsement`）原樣回傳，沒有忽略。鎖定測試 `test_accept_impl2.py::test_unknown_link_kind_is_ignored`（strict xfail）。建議修法：在 `reader/__init__.py` 的 `get_session`（與 `get_links` 回傳處）過濾只剩 `continuation`／`reference`，未知 kind 略過（不報錯）；`get_continuation` 的 sibling 篩選已只認 `continuation`，行為一致即可。
- **非缺口（記錄供 archive 判斷）**：
  - session-record／抹除的整合層 worker 403 負向案例：要真 worker conf（見 e2e README），非測試資源可覆蓋；單元層已擋（`test_accept_gaps.py`），e2e 有 `test_9_4_worker_cannot_delete_or_modify_true_store`（由 e2e 覆蓋，本輪未跑）。
  - 9.x e2e 情境（`test_split.py::test_9_1_split_1_to_n`、`test_consolidation.py`、`test_reference.py::test_9_3_mutual_reference_n_to_m`、`test_adversarial.py` 4 項、`test_persistence.py`）：一律標「由 e2e 覆蓋」，本輪未實際跑（環境由 impl3 占用）。其中 `test_persistence.py` 的 task 子 Session 斷言與 `test_consolidation.py::test_9_2_foundry_artifact_registration` 目前是 xfail（修好會 XPASS）。

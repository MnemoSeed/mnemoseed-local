# Method -> contract test mapping (prd-08 FR-8.5 / AC-3)

Generated automatically by `tests/contract/test_contract_coverage.py`. Every
public method of the four storage Protocols has at least one contract test
that runs against the embedded driver family.

Driver family covered by the `stack` fixture:

- embedded: lancedb_embedded + sqlite_graph + sqlite_meta + synthetic embedder

## VectorStore (12 methods)

| Method | Contract test |
|---|---|
| capabilities | `test_contract_vector.py::test_capabilities` |
| delete_chunk | `test_contract_vector.py::test_delete_chunk` |
| get_chunk | `test_contract_vector.py::test_upsert_get_roundtrip` |
| list_chunks | `test_contract_vector.py::test_list_chunks_filter_pagination` |
| mark_consolidated | `test_contract_vector.py::test_mark_consolidated` |
| near_duplicate | `test_contract_vector.py::test_near_duplicate_thresholds` |
| purge_range | `test_contract_vector.py::test_purge_range_disjoint_safe` |
| search | `test_contract_vector.py::test_search_profile_isolation_and_metadata_filters` |
| snapshot_read | `test_contract_vector.py::test_snapshot_read_consistent_set` |
| update_chunk_state | `test_contract_vector.py::test_update_chunk_state_usage_counts` |
| update_weights | `test_contract_vector.py::test_update_weights` |
| upsert_chunk | `test_contract_vector.py::test_upsert_get_roundtrip` |

## GraphStore (20 methods)

| Method | Contract test |
|---|---|
| add_edge | `test_contract_graph.py::test_add_edge_weight_overwrite` |
| append_version | `test_contract_graph.py::test_append_version_supersedes_previous` |
| as_of | `test_contract_graph.py::test_as_of_bi_temporal_replay` |
| batch_update_weights | `test_contract_graph.py::test_batch_update_weights` |
| bump_cooccurrence | `test_contract_graph.py::test_bump_cooccurrence_symmetric_and_increments` |
| capabilities | `test_contract_graph.py::test_capabilities` |
| clear_flags | `test_contract_graph.py::test_set_and_clear_flags` |
| diff | `test_contract_graph.py::test_diff_reports_payload_change` |
| find_same_predicate | `test_contract_graph.py::test_find_same_predicate` |
| get_node | `test_contract_graph.py::test_upsert_get_roundtrip` |
| invalidate | `test_contract_graph.py::test_invalidate_closes_current_revision` |
| list_edges | `test_contract_graph.py::test_list_edges_kinds_filters_and_stable_pagination` |
| list_nodes | `test_contract_graph.py::test_list_nodes_filter_pagination` |
| query_intentions | `test_contract_graph.py::test_query_intentions_status_and_due` |
| set_flags | `test_contract_graph.py::test_set_and_clear_flags` |
| timeline | `test_contract_graph.py::test_timeline_replays_versions` |
| tombstone | `test_contract_graph.py::test_tombstone_tombstoned_node_via_port` |
| traverse | `test_contract_graph.py::test_traverse_profile_scoped` |
| upsert_node | `test_contract_graph.py::test_upsert_get_roundtrip` |
| versions | `test_contract_graph.py::test_versions_chain` |

## MetaStore (32 methods)

| Method | Contract test |
|---|---|
| add_token_usage | `test_contract_meta.py::test_dream_token_ledger_atomic_increment` |
| advance_watermark | `test_contract_meta.py::test_pool_watermark_gap_raises` |
| archive_profile | `test_contract_meta.py::test_profile_archive_flag` |
| audit_append | `test_contract_meta.py::test_audit_append_and_query` |
| audit_query | `test_contract_meta.py::test_audit_append_and_query` |
| authenticate_token | `test_contract_meta.py::test_token_secret_hashed_at_rest_and_authenticates` |
| capabilities | `test_contract_meta.py::test_capabilities` |
| count_users | `test_contract_meta.py::test_users_crud_and_password_rotation` |
| create_owner | `test_contract_meta.py::test_create_owner_atomic_and_conflict` |
| create_user | `test_contract_meta.py::test_users_crud_and_password_rotation` |
| delete_profile | `test_contract_meta.py::test_profile_crud_and_token_cascade` |
| get_config | `test_contract_meta.py::test_config_versioned_get_set_rollback` |
| get_profile | `test_contract_meta.py::test_profile_crud_and_token_cascade` |
| get_user_by_username | `test_contract_meta.py::test_users_crud_and_password_rotation` |
| issue_token | `test_contract_meta.py::test_issue_token_and_revoke` |
| list_dream_runs | `test_contract_meta.py::test_dream_runs_roundtrip` |
| list_profiles | `test_contract_meta.py::test_profile_crud_and_token_cascade` |
| list_users | `test_contract_meta.py::test_users_crud_and_password_rotation` |
| migrate | `test_contract_meta.py::test_schema_version_and_migrate_forward_only` |
| pool_add | `test_contract_meta.py::test_pool_add_state_advance_watermark` |
| pool_credit | `test_contract_meta.py::test_pool_credit_upserts_row` |
| pool_state | `test_contract_meta.py::test_pool_add_state_advance_watermark` |
| pool_states | `test_contract_meta.py::test_pool_states_returns_all_rows` |
| record_dream_run | `test_contract_meta.py::test_dream_runs_roundtrip` |
| revoke_token | `test_contract_meta.py::test_issue_token_and_revoke` |
| rollback_config | `test_contract_meta.py::test_config_versioned_get_set_rollback` |
| schema_version | `test_contract_meta.py::test_schema_version_and_migrate_forward_only` |
| set_config | `test_contract_meta.py::test_config_versioned_get_set_rollback` |
| token_usage | `test_contract_meta.py::test_dream_token_ledger_atomic_increment` |
| update_dream_run_model | `test_contract_meta.py::test_dream_run_model_update_records_resolved_model` |
| update_user_password | `test_contract_meta.py::test_users_crud_and_password_rotation` |
| upsert_profile | `test_contract_meta.py::test_profile_crud_and_token_cascade` |

## Embedder (3 methods)

| Method | Contract test |
|---|---|
| capabilities | `test_contract_embed.py::test_capabilities` |
| embed | `test_contract_embed.py::test_embed_dense_deterministic_and_normalized` |
| embed_batch | `test_contract_embed.py::test_embed_batch_preserves_order_and_matches_single` |

"""Canonical appendix B method -> contract test mapping (prd-08 FR-8.5 / AC-3).

One entry per protocol method; ``test_contract_coverage`` introspects the port
Protocols and hard-fails on any method that has no contract test (or any mapped
name that is not a protocol method). The same table produces the checked-in
REPORT-method-mapping.md artifact.
"""

from __future__ import annotations

PORT_ORDER: tuple[str, ...] = ("VectorStore", "GraphStore", "MetaStore", "Embedder")

# Every value is "test_module.py::test_function" in this directory.
COVERAGE: dict[str, dict[str, str]] = {
    "VectorStore": {
        "capabilities": "test_contract_vector.py::test_capabilities",
        "upsert_chunk": "test_contract_vector.py::test_upsert_get_roundtrip",
        "get_chunk": "test_contract_vector.py::test_upsert_get_roundtrip",
        "delete_chunk": "test_contract_vector.py::test_delete_chunk",
        "search": "test_contract_vector.py::test_search_profile_isolation_and_metadata_filters",
        "near_duplicate": "test_contract_vector.py::test_near_duplicate_thresholds",
        "snapshot_read": "test_contract_vector.py::test_snapshot_read_consistent_set",
        "mark_consolidated": "test_contract_vector.py::test_mark_consolidated",
        "purge_range": "test_contract_vector.py::test_purge_range_disjoint_safe",
        "update_weights": "test_contract_vector.py::test_update_weights",
        "update_chunk_state": "test_contract_vector.py::test_update_chunk_state_usage_counts",
        "list_chunks": "test_contract_vector.py::test_list_chunks_filter_pagination",
    },
    "GraphStore": {
        "capabilities": "test_contract_graph.py::test_capabilities",
        "upsert_node": "test_contract_graph.py::test_upsert_get_roundtrip",
        "get_node": "test_contract_graph.py::test_upsert_get_roundtrip",
        "list_nodes": "test_contract_graph.py::test_list_nodes_filter_pagination",
        "add_edge": "test_contract_graph.py::test_add_edge_weight_overwrite",
        "bump_cooccurrence": "test_contract_graph.py::test_bump_cooccurrence_symmetric_and_increments",
        "traverse": "test_contract_graph.py::test_traverse_profile_scoped",
        "find_same_predicate": "test_contract_graph.py::test_find_same_predicate",
        "set_flags": "test_contract_graph.py::test_set_and_clear_flags",
        "clear_flags": "test_contract_graph.py::test_set_and_clear_flags",
        "invalidate": "test_contract_graph.py::test_invalidate_closes_current_revision",
        "append_version": "test_contract_graph.py::test_append_version_supersedes_previous",
        "versions": "test_contract_graph.py::test_versions_chain",
        "diff": "test_contract_graph.py::test_diff_reports_payload_change",
        "timeline": "test_contract_graph.py::test_timeline_replays_versions",
        "as_of": "test_contract_graph.py::test_as_of_bi_temporal_replay",
        "batch_update_weights": "test_contract_graph.py::test_batch_update_weights",
        "query_intentions": "test_contract_graph.py::test_query_intentions_status_and_due",
        "tombstone": "test_contract_graph.py::test_tombstone_tombstoned_node_via_port",
        "list_edges": "test_contract_graph.py::test_list_edges_kinds_filters_and_stable_pagination",
    },
    "MetaStore": {
        "capabilities": "test_contract_meta.py::test_capabilities",
        "pool_add": "test_contract_meta.py::test_pool_add_state_advance_watermark",
        "pool_state": "test_contract_meta.py::test_pool_add_state_advance_watermark",
        "pool_credit": "test_contract_meta.py::test_pool_credit_upserts_row",
        "pool_states": "test_contract_meta.py::test_pool_states_returns_all_rows",
        "advance_watermark": "test_contract_meta.py::test_pool_watermark_gap_raises",
        "upsert_profile": "test_contract_meta.py::test_profile_crud_and_token_cascade",
        "get_profile": "test_contract_meta.py::test_profile_crud_and_token_cascade",
        "delete_profile": "test_contract_meta.py::test_profile_crud_and_token_cascade",
        "list_profiles": "test_contract_meta.py::test_profile_crud_and_token_cascade",
        "archive_profile": "test_contract_meta.py::test_profile_archive_flag",
        "issue_token": "test_contract_meta.py::test_issue_token_and_revoke",
        "revoke_token": "test_contract_meta.py::test_issue_token_and_revoke",
        "get_config": "test_contract_meta.py::test_config_versioned_get_set_rollback",
        "set_config": "test_contract_meta.py::test_config_versioned_get_set_rollback",
        "rollback_config": "test_contract_meta.py::test_config_versioned_get_set_rollback",
        "audit_append": "test_contract_meta.py::test_audit_append_and_query",
        "audit_query": "test_contract_meta.py::test_audit_append_and_query",
        "record_dream_run": "test_contract_meta.py::test_dream_runs_roundtrip",
        "list_dream_runs": "test_contract_meta.py::test_dream_runs_roundtrip",
        "update_dream_run_model": "test_contract_meta.py::test_dream_run_model_update_records_resolved_model",
        "add_token_usage": "test_contract_meta.py::test_dream_token_ledger_atomic_increment",
        "token_usage": "test_contract_meta.py::test_dream_token_ledger_atomic_increment",
        "schema_version": "test_contract_meta.py::test_schema_version_and_migrate_forward_only",
        "migrate": "test_contract_meta.py::test_schema_version_and_migrate_forward_only",
        # identity chain (issue #14): owner account + hashed bearer secrets
        "create_user": "test_contract_meta.py::test_users_crud_and_password_rotation",
        "create_owner": "test_contract_meta.py::test_create_owner_atomic_and_conflict",
        "get_user_by_username": "test_contract_meta.py::test_users_crud_and_password_rotation",
        "list_users": "test_contract_meta.py::test_users_crud_and_password_rotation",
        "count_users": "test_contract_meta.py::test_users_crud_and_password_rotation",
        "update_user_password": "test_contract_meta.py::test_users_crud_and_password_rotation",
        "authenticate_token": "test_contract_meta.py::test_token_secret_hashed_at_rest_and_authenticates",
    },
    "Embedder": {
        "capabilities": "test_contract_embed.py::test_capabilities",
        "embed": "test_contract_embed.py::test_embed_dense_deterministic_and_normalized",
        "embed_batch": "test_contract_embed.py::test_embed_batch_preserves_order_and_matches_single",
    },
}

# Appendix B reference method counts (prd-08 appendix B.1..B.4). MetaStore
# grew to 29 with the issue-#14 identity chain surface (create/get/list/count/
# update_password user + authenticate_token over hashed bearer digests) and to
# 30 with create_owner (the atomic single-transaction owner setup), and to 31
# with archive_profile (PRD-07 FR-7.3 console profile archive), and to 32 with
# update_dream_run_model (F2 per-run model pinning recorded on dream_runs).
# GraphStore is 20 with list_edges (PRD-08 appendix B.2 v1.1 amendment,
# 2026-08-13, the console Graph View bulk edge listing).
EXPECTED_METHOD_COUNTS: dict[str, int] = {
    "VectorStore": 12,
    "GraphStore": 20,
    "MetaStore": 32,
    "Embedder": 3,
}

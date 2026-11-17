// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # gateway-core DB-integration tests
//!
//! Tests that need a real Postgres — query paths, route handlers against
//! the DB — that the in-source `#[cfg(test)]` unit tests can't exercise
//! because they have no DB harness.
//!
//! Gated behind the `db-tests` Cargo feature so plain `cargo test` is
//! unaffected. Schema comes from `tests/migrations/00000000000000_schema.sql`
//! (a `pg_dump --schema-only` of an Alembic-applied DB). See README.md for
//! local setup, the regen procedure, and CI.

// `pub`: the Enterprise workspace includes this file as a module
// (`enterprise/tests/integration/main.rs`) and its own tests build on the
// modules marked so. In this crate, where this file is the root, it changes
// nothing.
mod app_assembly;
mod approvals_decide;
mod approvals_mount;
mod assemblyai_transcribe;
mod audit_spool;
mod budget_contention;
mod cache_invalidation_race;
pub mod cache_statistics;
pub mod common;
mod community_composition;
pub mod composition;
mod context_enrichment_unavailable;
pub mod credential_surface_matrix;
mod duplicate_model_names;
pub mod edition_guard_start;
mod extension_assembly;
mod llm_ambiguous_retry;
mod llm_hot_path_latency;
mod llm_row_group_attribution;
mod media_api_url_query;
mod media_filesystem_replicas;
mod provider_base_url;
mod refused_agent_credential;
mod routing_group_cache_invalidation;
mod skill_script_allowlist;
mod tenant_features;
pub mod tenant_resolution_matrix;
mod tenant_standing;
mod transcribe_refused_after_answer;
mod undecryptable_credential;
mod undecryptable_mcp_token;
mod upstream_error_bodies;
mod usage_spool;
mod user_group_choice;
mod vmcp_group_binding;

mod a2a_governance;
mod a2a_skill_settle;
mod a2a_stream_notify;
mod a2a_v1_run;
mod accepted_callers;
mod admission_redis;
mod agent_authz_a2a_inbound;
mod agent_authz_db;
mod agent_authz_enforcement;
mod agent_impl;
mod ai_system_attribution;
pub mod anthropic_native;
mod approval_hold_attribution;
mod approval_hold_decide_forward;
mod approval_integrity;
mod approval_two_holds;
mod approval_window;
mod argument_policy_bands;
mod assurance_checks;
mod assurance_inbox;
mod audit_verify;
mod autonomy;
mod autonomy_group_header;
mod azure_request_shape;
pub mod background_writer_chain;
mod bedrock_claude;
mod bedrock_runtime;
mod bug1_rg_llm_limits;
mod chain_propagation;
mod cimd_authorize_bridge;
mod cloud_identity;
mod data_path_surfaces;
mod data_retention;
mod data_scope;
mod data_scope_model_tool_calls;
mod data_scope_surfaces;
pub mod db_error_fail_closed;
mod declared_outcomes;
mod drift;
mod drift_dependency_cause;
mod drift_resolution;
mod embedding_governance;
mod evidence_api;
mod evidence_records;
pub mod evidence_runtime;
mod evidence_witness;
mod gemini_native;
mod gemini_surface;
mod governance_fail_closed;
mod governance_inheritance;
mod guardrail_config_identity;
mod guardrails_unavailable_recorded;
mod hold_identity;
mod image_provider_timeout;
mod image_token_pricing;
mod inherited_limits_tighten_only;
pub mod instance_lease;
mod internal_embedding;
mod limits_http;
mod liveness;
mod llm_actor_planes_every_route;
pub mod llm_block_attribution;
mod llm_compliance;
mod llm_governance_merge_db;
mod llm_model_family_rules;
mod llm_proxy_http;
pub mod llm_refusal_audit;
mod llm_residency;
pub mod mcp_approval_required;
mod mcp_batch;
mod mcp_era_bridge;
mod mcp_execution_limits;
mod mcp_group_resolution;
mod mcp_hot_path_latency;
mod mcp_proxy_http;
mod mcp_rate_limit;
mod mcp_residency;
mod mcp_session_store;
mod media_api_key;
mod media_budget_settle;
mod media_quota;
mod model_availability;
pub mod model_fallback;
pub mod mounted_routes;
mod multimodal_guardrails;
mod oauth_agent_attribution;
mod oauth_db_errors;
pub mod oauth_routes;
mod openai_transcribe;
pub mod openapi_coverage;
mod otel_ingest;
mod outbound_policy_e2e;
mod proxy_pipeline;
mod quota_counter_redis;
mod quota_enforcement;
mod reasoning_withheld;
mod record_reach;
mod replay;
mod routing_group_all_busy;
mod run_abort;
mod run_actions;
mod run_budget;
mod run_clock_holds;
mod run_controls_parity;
mod run_ledger;
mod run_segments;
mod run_starts;
mod search_server_tools;
mod semantic_judge_run;
mod semantic_policy_db;
mod service_baselines;
mod service_routes_auth;
mod skill_argument_policy;
mod skill_catalog_scope;
mod skill_execution_owner;
mod skill_guardrails;
mod stream_abort;
mod system_server;
mod tenant_aggregate_quota;
mod tool_provenance;
mod trusted_proxies;
mod unbound_keys;
pub mod workload_identity;
mod workload_model_access;
mod workload_other_surfaces;

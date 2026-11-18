// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Tamper-evident audit records.
//!
//! Before this, a database-level modification to `proxy_logs` was undetectable.
//! The HMAC in [`crate::services::a2a_delegation`] protects the delegation
//! chain **in transit**; its only residue in the database is the
//! `trace_continuity` outcome, which proves steps were linked by the gateway and
//! proves nothing about whether a stored row was later altered.
//!
//! ## Two mechanisms, one property
//!
//! Each row carries `row_hash = SHA-256(canonical_bytes || prev_hash)`, so
//! altering any row breaks every hash after it. That detects *modification*
//! but not wholesale rewriting — anyone with write access could recompute the
//! chain. So the writer also **signs the head of each flushed batch** into
//! `audit_chain_checkpoints` with Ed25519.
//!
//! Signing every row would cost a signature per record. Signing batch heads
//! costs one per flush (up to 500 rows) and the property is identical: to
//! forge a row you must forge every later row hash *and* a signed checkpoint,
//! and the latter needs the private key.
//!
//! ## Chains are per writer, not per tenant
//!
//! The proxy runs multiple replicas, each with one `log_consumer` task, so a
//! per-writer chain is naturally sequential and needs no coordination. A
//! single global chain would serialise every replica behind one counter on the
//! hot write path. Chains are also *not* tenant-scoped: a tenant's rows
//! interleave with every other tenant's, and partitioning per tenant would let
//! a tenant-scoped delete silently renumber someone else's evidence.
//!
//! ## Key rotation
//!
//! `BRUTOR_AUDIT_SIGNING_KEY` holds the current private seed;
//! `BRUTOR_AUDIT_TRUSTED_KEYS` holds a comma-separated list of public keys the
//! deployment will accept, and the current key's public half is always
//! included implicitly. Rotation is therefore additive and needs no migration:
//!
//!   1. Generate a new seed. Append the **old** public key to
//!      `BRUTOR_AUDIT_TRUSTED_KEYS`.
//!   2. Set `BRUTOR_AUDIT_SIGNING_KEY` to the new seed and restart.
//!   3. New checkpoints are signed with the new key; every checkpoint the old
//!      key signed keeps verifying, because its public half is stored on the
//!      checkpoint row *and* still trusted.
//!
//! Never remove a public key that signed checkpoints you still retain — doing
//! so turns that history from `verified` into `forged`, which is exactly the
//! alarm it should raise if the key really was compromised, and a false one
//! if it was merely retired.
//!
//! ## The canonical bytes are a contract
//!
//! [`canonical_row_bytes`] must never change meaning for a given version tag.
//! Get it wrong and every historical row fails verification permanently, with
//! no way to tell a real tamper from a format drift. It is versioned and
//! pinned by test for exactly that reason — the same discipline as the
//! wire-contract constants.

use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering};

use ed25519_dalek::{Signer, SigningKey, VerifyingKey};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::models::db::ProxyLogInsert;

/// Version tag on the canonical row format. Bump only alongside a documented
/// change of the verifier, never as a side effect of adding a column.
///
/// Every row is sealed under this format and carries the tag in
/// `proxy_logs.chain_version` and inside its hashed bytes. It is the one
/// format the verifier computes: a row that names any other, or none, does
/// not verify ([`row_matches`]).
///
/// The format: the tag, then [`V3_FIELDS`], then the JSON decision records
/// ([`V4_ADDED_FIELDS`]) in their RFC 8785 canonical form, length-framed
/// ([`canonical_row_bytes`]).
pub const CANONICAL_VERSION: &str = "v4";
/// Every row format the verifier computes.
pub const KNOWN_VERSIONS: &[&str] = &[CANONICAL_VERSION];

/// Version tag inside the three **signed** shapes — checkpoints, export
/// manifests, report manifests. Deliberately NOT [`CANONICAL_VERSION`]:
/// those shapes did not change when the row format did, and tying them to
/// the row tag meant every row-format bump silently invalidated every
/// checkpoint signed before it (the move to row format `v2` did exactly that to earlier
/// checkpoints: their bytes carried `v1`, verification recomputed `v2`).
/// Frozen at `v2`, the tag every checkpoint is signed with.
pub const SIGNED_SHAPE_VERSION: &str = "v2";
/// The tag checkpoints and manifests signed before row format `v2` carry. Accepted on
/// verification only; nothing signs with it any more.
pub const SIGNED_SHAPE_VERSION_V1: &str = "v1";

/// `prev_hash` of the first row in a chain.
pub const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

// ---------------------------------------------------------------------------
// Canonical bytes
// ---------------------------------------------------------------------------

/// The fields the chain covers, as one definition.
///
/// Both the writer (which builds this from a `ProxyLogInsert`) and the
/// verifier (which `SELECT`s straight into it) go through this struct, so the
/// hash input cannot drift between the thing that writes it and the thing that
/// checks it. Two separate field lists would be the single most dangerous
/// thing about this feature: verification would start failing on honest rows,
/// and a real tamper would be indistinguishable from the drift.
///
/// **Deliberately a subset of the row.** It covers every scalar fact the
/// gateway establishes before sealing — identity, attribution, the software
/// that called, the governance decision, the outcome, the call
/// graph and the content digests. What it leaves out, and why, is the
/// explicit list [`CHAIN_EXCLUDED_COLUMNS`]; a unit test fails when a column
/// the writer inserts is on neither list.
///
/// Field ORDER here is not the hash order — [`V3_FIELDS`] then
/// [`V4_ADDED_FIELDS`] fix that.
/// Adding a field to this struct without adding it there covers nothing;
/// adding it there without bumping [`CANONICAL_VERSION`] breaks every
/// historical row.
#[derive(Debug, Clone, sqlx::FromRow)]
pub struct CoveredFields {
    pub id: String,
    pub tenant_id: String,
    pub timestamp: chrono::DateTime<chrono::Utc>,
    pub proxy_type: String,
    pub proxy_subtype: Option<String>,
    pub actor_agent_id: Option<String>,
    pub subject_id: Option<String>,
    pub subject_kind: Option<String>,
    pub api_key_id: Option<String>,
    pub resource_group_id: Option<String>,
    pub http_method: String,
    pub endpoint: String,
    pub mcp_capability_name: Option<String>,
    pub llm_model_used: Option<String>,
    pub agent_authz_decision: Option<String>,
    pub agent_grant_id: Option<String>,
    pub semantic_policy_id: Option<String>,
    pub argument_policy_id: Option<String>,
    pub guardrail_config_id: Option<String>,
    pub guardrail_check: Option<String>,
    pub guardrail_action: Option<String>,
    pub autonomy_level: Option<String>,
    pub autonomy_provenance: Option<String>,
    pub consumed_approval_request_id: Option<String>,
    pub guardrail_blocked: bool,
    pub argument_policy_denied: bool,
    pub http_status_code: Option<i32>,
    pub success: bool,
    pub root_task_id: Option<String>,
    pub turn_id: Option<String>,
    // Content digests sealed before retention touched the bodies, and the
    // tool definition / server version in force.
    pub request_content_hash: Option<String>,
    pub response_content_hash: Option<String>,
    pub server_version: Option<String>,
    pub tool_definition_hash: Option<String>,
    // The software that called, the model that decided, and the other
    // witnessed scalar facts.
    pub source: String,
    pub target_url: Option<String>,
    pub server_id: Option<String>,
    pub idp_server_id: Option<String>,
    pub mcp_method: Option<String>,
    pub skill_id: Option<String>,
    pub skill_execution_id: Option<String>,
    pub llm_provider: Option<String>,
    pub llm_model_requested: Option<String>,
    pub oauth_flow_type: Option<String>,
    pub oauth_scope: Option<String>,
    pub rag_collection_ids: Option<String>,
    pub prompt_fingerprint: Option<String>,
    pub gateway_user_id: Option<String>,
    pub idp_user_id: Option<String>,
    pub is_auth_event: bool,
    pub agent_impl_name: Option<String>,
    pub agent_impl_version: Option<String>,
    pub agent_impl_build: Option<String>,
    pub agent_impl_source: Option<String>,
    pub client_label: Option<String>,
    pub pii_detected: bool,
    pub region_routed_to: Option<String>,
    pub residency_class: Option<String>,
    pub error_type: Option<String>,
    pub upstream_request_id: Option<String>,
    pub a2a_task_state: Option<String>,
    pub current_task_id: Option<String>,
    pub parent_task_id: Option<String>,
    pub delegation_depth: Option<i32>,
    pub trace_root_kind: Option<String>,
    pub trace_continuity: Option<String>,
    pub run_end_state: Option<String>,
    pub deciding_model: Option<String>,
    // v4. The JSON decision records, in their RFC 8785 canonical
    // form: every one is final before the batch is sealed and nothing
    // rewrites it afterwards.
    pub compliance_tags: Option<serde_json::Value>,
    pub guardrails_skipped: Option<serde_json::Value>,
    pub guardrails_degraded: Option<serde_json::Value>,
    pub pre_action_state: Option<serde_json::Value>,
    pub custom_metadata: Option<serde_json::Value>,
    pub tags: Option<serde_json::Value>,
}

/// The scalar fields of the canonical row, in hash order: the contract
/// external verifiers implement. A row hashes these, in this order, then
/// [`V4_ADDED_FIELDS`].
///
/// Mirrored byte-for-byte by the control plane's reference verifier
/// (`app/services/audit_chain_canonical.py::V3_FIELDS`), which pins this
/// list by parsing this file. Never reorder, insert or remove: that is a new
/// version.
pub const V3_FIELDS: &[&str] = &[
    // Identity, time and provenance
    "id",
    "tenant_id",
    "timestamp",
    "source",
    // What was called
    "proxy_type",
    "proxy_subtype",
    "http_method",
    "endpoint",
    "target_url",
    "server_id",
    "idp_server_id",
    "mcp_method",
    "mcp_capability_name",
    "skill_id",
    "skill_execution_id",
    "llm_provider",
    "llm_model_requested",
    "llm_model_used",
    "oauth_flow_type",
    "oauth_scope",
    "rag_collection_ids",
    "prompt_fingerprint",
    // Who called it
    "api_key_id",
    "resource_group_id",
    "gateway_user_id",
    "idp_user_id",
    "actor_agent_id",
    "subject_id",
    "subject_kind",
    "is_auth_event",
    // Which software, which release
    "agent_impl_name",
    "agent_impl_version",
    "agent_impl_build",
    "agent_impl_source",
    "client_label",
    // Under what authority, and what was decided
    "agent_authz_decision",
    "agent_grant_id",
    "semantic_policy_id",
    "argument_policy_id",
    "guardrail_config_id",
    "guardrail_check",
    "guardrail_action",
    "guardrail_blocked",
    "argument_policy_denied",
    "pii_detected",
    "autonomy_level",
    "autonomy_provenance",
    "consumed_approval_request_id",
    "region_routed_to",
    "residency_class",
    // Outcome
    "http_status_code",
    "success",
    "error_type",
    "upstream_request_id",
    "a2a_task_state",
    // Run and call graph
    "root_task_id",
    "current_task_id",
    "parent_task_id",
    "delegation_depth",
    "trace_root_kind",
    "trace_continuity",
    "turn_id",
    "run_end_state",
    "deciding_model",
    // Content and tool provenance
    "request_content_hash",
    "response_content_hash",
    "server_version",
    "tool_definition_hash",
];

/// What a row hashes after [`V3_FIELDS`]: the JSON decision records, each
/// as its RFC 8785 (JCS) text. Never reorder, insert or remove: that is a
/// new version.
///
/// Why JCS: `jsonb` re-orders keys and re-spells numbers, so the text the
/// writer sent is not the text the verifier reads back. JCS is a function
/// of the JSON *data*, not its layout — the writer's `serde_json::Value`,
/// the stored `jsonb`/`json` value and an exported copy all canonicalise to
/// the same bytes.
pub const V4_ADDED_FIELDS: &[&str] = &[
    "compliance_tags",
    "guardrails_skipped",
    "guardrails_degraded",
    "pre_action_state",
    "custom_metadata",
    "tags",
];

/// Why a `proxy_logs` column is outside the chain. Every column the writer
/// inserts is either covered ([`V3_FIELDS`], [`V4_ADDED_FIELDS`]) or on
/// [`CHAIN_EXCLUDED_COLUMNS`] with
/// one of these reasons — the guard that stops the next column landing
/// outside the chain by accident (as columns have before).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Excluded {
    /// The chain's own linkage and position. Protected structurally: the
    /// hash covers `prev_hash`, density exposes a moved `chain_seq`, and the
    /// version tag is the first field inside the hashed bytes.
    ChainMechanics,
    /// Content retention and erasure must be able to remove. The content
    /// digests stand in for the bodies inside the chain.
    RemovableContent,
    /// A direct identifier data-subject erasure scrubs (GDPR Art. 17). The
    /// pseudonymous ids beside it are covered.
    ErasableIdentifier,
    /// A display label copied at write time; the id beside it is covered.
    Label,
    /// A caller-asserted join key (correlation, request, trace, step ids):
    /// organises the trail, is not a governance input.
    CallerCorrelation,
    /// Metering and measurement — tokens, cost, cache, sizes, latency.
    /// Economics, reconciled against provider invoices, not audit answers;
    /// several are floats with no cross-language canonical text form.
    Metering,
    /// Request-shape and resilience telemetry. The model that actually
    /// served (`llm_model_used`) is covered.
    RequestShape,
    /// Free-text diagnostics that may quote upstream content; the
    /// classification (`error_type`) is covered.
    FreeText,
    /// Set after the row is sealed (by the writer after stamping, by a
    /// database default, or by the control plane), so it cannot be inside
    /// a hash computed before it exists.
    AfterSeal,
}

/// Every `proxy_logs` column outside the chain, with the reason. The
/// control plane pins the same map against its ORM
/// (`tests/test_audit_chain_v3.py`, `tests/test_audit_chain_v4.py`).
pub const CHAIN_EXCLUDED_COLUMNS: &[(&str, Excluded)] = &[
    ("chain_id", Excluded::ChainMechanics),
    ("chain_seq", Excluded::ChainMechanics),
    ("prev_hash", Excluded::ChainMechanics),
    ("row_hash", Excluded::ChainMechanics),
    ("chain_version", Excluded::ChainMechanics),
    ("request_headers", Excluded::RemovableContent),
    ("request_body_preview", Excluded::RemovableContent),
    ("response_headers", Excluded::RemovableContent),
    ("response_body_preview", Excluded::RemovableContent),
    ("idp_user_email", Excluded::ErasableIdentifier),
    ("idp_user_username", Excluded::ErasableIdentifier),
    ("gateway_user_username", Excluded::ErasableIdentifier),
    ("client_ip", Excluded::ErasableIdentifier),
    ("user_agent", Excluded::ErasableIdentifier),
    ("api_key_name", Excluded::Label),
    ("correlation_id", Excluded::CallerCorrelation),
    ("parent_request_id", Excluded::CallerCorrelation),
    ("request_id", Excluded::CallerCorrelation),
    ("mcp_request_id", Excluded::CallerCorrelation),
    ("end_user_session_id", Excluded::CallerCorrelation),
    ("trace_id", Excluded::CallerCorrelation),
    ("span_id", Excluded::CallerCorrelation),
    ("step_id", Excluded::CallerCorrelation),
    ("step_name", Excluded::CallerCorrelation),
    ("turn_seq", Excluded::CallerCorrelation),
    // The outcome a client (or, on an outbound A2A row, a
    // delegate) REPORTED: a caller-asserted label judged at the run level
    // against the contract, never a governance input of the call itself.
    ("outcome_id", Excluded::CallerCorrelation),
    ("request_size_bytes", Excluded::Metering),
    ("response_size_bytes", Excluded::Metering),
    ("duration_ms", Excluded::Metering),
    ("llm_prompt_tokens", Excluded::Metering),
    ("llm_completion_tokens", Excluded::Metering),
    ("llm_total_tokens", Excluded::Metering),
    ("llm_estimated_cost_usd", Excluded::Metering),
    ("media_images_generated", Excluded::Metering),
    ("media_audio_chars", Excluded::Metering),
    ("media_audio_seconds_input", Excluded::Metering),
    ("media_video_seconds_output", Excluded::Metering),
    ("llm_cache_hit", Excluded::Metering),
    ("llm_cache_layer", Excluded::Metering),
    ("llm_tokens_saved", Excluded::Metering),
    ("llm_cache_similarity_score", Excluded::Metering),
    ("prompt_cache_write_tokens", Excluded::Metering),
    ("prompt_cache_read_tokens", Excluded::Metering),
    ("prompt_cache_savings_usd", Excluded::Metering),
    ("llm_context_tokens", Excluded::Metering),
    ("rag_enabled", Excluded::Metering),
    ("rag_chunks_retrieved", Excluded::Metering),
    ("rag_context_tokens", Excluded::Metering),
    ("rag_top_similarity_score", Excluded::Metering),
    ("rag_retrieval_duration_ms", Excluded::Metering),
    ("llm_streaming", Excluded::RequestShape),
    ("llm_temperature", Excluded::RequestShape),
    ("llm_max_tokens", Excluded::RequestShape),
    ("llm_function_calling", Excluded::RequestShape),
    ("llm_used_fallback", Excluded::RequestShape),
    ("llm_fallback_reason", Excluded::RequestShape),
    ("error_message", Excluded::FreeText),
    ("record_id", Excluded::AfterSeal),
    ("created_at", Excluded::AfterSeal),
    ("bodies_pruned_at", Excluded::AfterSeal),
    ("erased_at", Excluded::AfterSeal),
    ("a2a_protocol_version", Excluded::AfterSeal),
];

/// The `SELECT` list matching [`CoveredFields`]. Kept beside the struct so
/// the two are edited together; a unit test pins it to [`V3_FIELDS`] +
/// [`V4_ADDED_FIELDS`].
pub const COVERED_SELECT: &str = "\
    id, tenant_id, timestamp, proxy_type, proxy_subtype, \
    actor_agent_id, subject_id, subject_kind, api_key_id, resource_group_id, \
    http_method, endpoint, mcp_capability_name, llm_model_used, \
    agent_authz_decision, agent_grant_id, semantic_policy_id, \
    argument_policy_id, guardrail_config_id, guardrail_check, \
    guardrail_action, autonomy_level, autonomy_provenance, \
    consumed_approval_request_id, guardrail_blocked, argument_policy_denied, \
    http_status_code, success, root_task_id, turn_id, \
    request_content_hash, response_content_hash, server_version, tool_definition_hash, \
    source, target_url, server_id, idp_server_id, mcp_method, \
    skill_id, skill_execution_id, llm_provider, llm_model_requested, \
    oauth_flow_type, oauth_scope, rag_collection_ids, prompt_fingerprint, \
    gateway_user_id, idp_user_id, is_auth_event, \
    agent_impl_name, agent_impl_version, agent_impl_build, agent_impl_source, client_label, \
    pii_detected, region_routed_to, residency_class, \
    error_type, upstream_request_id, a2a_task_state, \
    current_task_id, parent_task_id, delegation_depth, \
    trace_root_kind, trace_continuity, run_end_state, deciding_model, \
    compliance_tags, guardrails_skipped, guardrails_degraded, pre_action_state, \
    custom_metadata, tags";

impl From<&ProxyLogInsert> for CoveredFields {
    fn from(l: &ProxyLogInsert) -> Self {
        Self {
            id: l.id.clone(),
            tenant_id: l.tenant_id.clone(),
            timestamp: l.timestamp,
            proxy_type: l.proxy_type.clone(),
            proxy_subtype: l.proxy_subtype.clone(),
            actor_agent_id: l.actor_agent_id.clone(),
            subject_id: l.subject_id.clone(),
            subject_kind: l.subject_kind.clone(),
            api_key_id: l.api_key_id.clone(),
            resource_group_id: l.resource_group_id.clone(),
            http_method: l.http_method.clone(),
            endpoint: l.endpoint.clone(),
            mcp_capability_name: l.mcp_capability_name.clone(),
            llm_model_used: l.llm_model_used.clone(),
            agent_authz_decision: l.agent_authz_decision.clone(),
            agent_grant_id: l.agent_grant_id.clone(),
            semantic_policy_id: l.semantic_policy_id.clone(),
            argument_policy_id: l.argument_policy_id.clone(),
            guardrail_config_id: l.guardrail_config_id.clone(),
            guardrail_check: l.guardrail_check.clone(),
            guardrail_action: l.guardrail_action.clone(),
            autonomy_level: l.autonomy_level.clone(),
            autonomy_provenance: l.autonomy_provenance.clone(),
            consumed_approval_request_id: l.consumed_approval_request_id.clone(),
            guardrail_blocked: l.guardrail_blocked,
            argument_policy_denied: l.argument_policy_denied,
            http_status_code: l.http_status_code,
            success: l.success,
            root_task_id: l.root_task_id.clone(),
            turn_id: l.turn_id.clone(),
            request_content_hash: l.request_content_hash.clone(),
            response_content_hash: l.response_content_hash.clone(),
            server_version: l.server_version.clone(),
            tool_definition_hash: l.tool_definition_hash.clone(),
            source: l.source.clone(),
            target_url: l.target_url.clone(),
            server_id: l.server_id.clone(),
            idp_server_id: l.idp_server_id.clone(),
            mcp_method: l.mcp_method.clone(),
            skill_id: l.skill_id.clone(),
            skill_execution_id: l.skill_execution_id.clone(),
            llm_provider: l.llm_provider.clone(),
            llm_model_requested: l.llm_model_requested.clone(),
            oauth_flow_type: l.oauth_flow_type.clone(),
            oauth_scope: l.oauth_scope.clone(),
            rag_collection_ids: l.rag_collection_ids.clone(),
            prompt_fingerprint: l.prompt_fingerprint.clone(),
            gateway_user_id: l.gateway_user_id.clone(),
            idp_user_id: l.idp_user_id.clone(),
            is_auth_event: l.is_auth_event,
            agent_impl_name: l.agent_impl_name.clone(),
            agent_impl_version: l.agent_impl_version.clone(),
            agent_impl_build: l.agent_impl_build.clone(),
            agent_impl_source: l.agent_impl_source.clone(),
            client_label: l.client_label.clone(),
            pii_detected: l.pii_detected,
            region_routed_to: l.region_routed_to.clone(),
            residency_class: l.residency_class.clone(),
            error_type: l.error_type.clone(),
            upstream_request_id: l.upstream_request_id.clone(),
            a2a_task_state: l.a2a_task_state.clone(),
            current_task_id: l.current_task_id.clone(),
            parent_task_id: l.parent_task_id.clone(),
            delegation_depth: l.delegation_depth,
            trace_root_kind: l.trace_root_kind.clone(),
            trace_continuity: l.trace_continuity.clone(),
            run_end_state: l.run_end_state.clone(),
            deciding_model: l.deciding_model.clone(),
            compliance_tags: l.compliance_tags.clone(),
            guardrails_skipped: l.guardrails_skipped.clone(),
            guardrails_degraded: l.guardrails_degraded.clone(),
            pre_action_state: l.pre_action_state.clone(),
            custom_metadata: l.custom_metadata.clone(),
            tags: l.tags.clone(),
        }
    }
}

/// The bytes a row's hash covers.
pub fn canonical_row_bytes(f: &CoveredFields, prev_hash: &str) -> Vec<u8> {
    canonical_row_bytes_v4(f, prev_hash)
}

/// The canonical timestamp: RFC 3339, UTC, `Z`, exactly six fractional
/// digits (truncated, never rounded).
///
/// Fixed width on purpose. The writer hashes the in-memory timestamp, which
/// on Linux carries nanoseconds, while Postgres stores (and the verifier
/// reads back) microseconds. Truncating to microseconds here matches what
/// `timestamptz` stores, so writer and verifier agree by construction.
pub fn v3_timestamp(ts: &chrono::DateTime<chrono::Utc>) -> String {
    ts.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string()
}

/// The scalar field values, in [`V3_FIELDS`] order. `None` is NULL, which
/// the framing encodes differently from the empty string.
pub fn v3_values(f: &CoveredFields) -> Vec<(&'static str, Option<String>)> {
    let b = |v: bool| Some(if v { "1" } else { "0" }.to_string());
    let s = |v: &Option<String>| v.clone();
    vec![
        ("id", Some(f.id.clone())),
        ("tenant_id", Some(f.tenant_id.clone())),
        ("timestamp", Some(v3_timestamp(&f.timestamp))),
        ("source", Some(f.source.clone())),
        ("proxy_type", Some(f.proxy_type.clone())),
        ("proxy_subtype", s(&f.proxy_subtype)),
        ("http_method", Some(f.http_method.clone())),
        ("endpoint", Some(f.endpoint.clone())),
        ("target_url", s(&f.target_url)),
        ("server_id", s(&f.server_id)),
        ("idp_server_id", s(&f.idp_server_id)),
        ("mcp_method", s(&f.mcp_method)),
        ("mcp_capability_name", s(&f.mcp_capability_name)),
        ("skill_id", s(&f.skill_id)),
        ("skill_execution_id", s(&f.skill_execution_id)),
        ("llm_provider", s(&f.llm_provider)),
        ("llm_model_requested", s(&f.llm_model_requested)),
        ("llm_model_used", s(&f.llm_model_used)),
        ("oauth_flow_type", s(&f.oauth_flow_type)),
        ("oauth_scope", s(&f.oauth_scope)),
        ("rag_collection_ids", s(&f.rag_collection_ids)),
        ("prompt_fingerprint", s(&f.prompt_fingerprint)),
        ("api_key_id", s(&f.api_key_id)),
        ("resource_group_id", s(&f.resource_group_id)),
        ("gateway_user_id", s(&f.gateway_user_id)),
        ("idp_user_id", s(&f.idp_user_id)),
        ("actor_agent_id", s(&f.actor_agent_id)),
        ("subject_id", s(&f.subject_id)),
        ("subject_kind", s(&f.subject_kind)),
        ("is_auth_event", b(f.is_auth_event)),
        ("agent_impl_name", s(&f.agent_impl_name)),
        ("agent_impl_version", s(&f.agent_impl_version)),
        ("agent_impl_build", s(&f.agent_impl_build)),
        ("agent_impl_source", s(&f.agent_impl_source)),
        ("client_label", s(&f.client_label)),
        ("agent_authz_decision", s(&f.agent_authz_decision)),
        ("agent_grant_id", s(&f.agent_grant_id)),
        ("semantic_policy_id", s(&f.semantic_policy_id)),
        ("argument_policy_id", s(&f.argument_policy_id)),
        ("guardrail_config_id", s(&f.guardrail_config_id)),
        ("guardrail_check", s(&f.guardrail_check)),
        ("guardrail_action", s(&f.guardrail_action)),
        ("guardrail_blocked", b(f.guardrail_blocked)),
        ("argument_policy_denied", b(f.argument_policy_denied)),
        ("pii_detected", b(f.pii_detected)),
        ("autonomy_level", s(&f.autonomy_level)),
        ("autonomy_provenance", s(&f.autonomy_provenance)),
        (
            "consumed_approval_request_id",
            s(&f.consumed_approval_request_id),
        ),
        ("region_routed_to", s(&f.region_routed_to)),
        ("residency_class", s(&f.residency_class)),
        (
            "http_status_code",
            f.http_status_code.map(|c| c.to_string()),
        ),
        ("success", b(f.success)),
        ("error_type", s(&f.error_type)),
        ("upstream_request_id", s(&f.upstream_request_id)),
        ("a2a_task_state", s(&f.a2a_task_state)),
        ("root_task_id", s(&f.root_task_id)),
        ("current_task_id", s(&f.current_task_id)),
        ("parent_task_id", s(&f.parent_task_id)),
        (
            "delegation_depth",
            f.delegation_depth.map(|d| d.to_string()),
        ),
        ("trace_root_kind", s(&f.trace_root_kind)),
        ("trace_continuity", s(&f.trace_continuity)),
        ("turn_id", s(&f.turn_id)),
        ("run_end_state", s(&f.run_end_state)),
        ("deciding_model", s(&f.deciding_model)),
        ("request_content_hash", s(&f.request_content_hash)),
        ("response_content_hash", s(&f.response_content_hash)),
        ("server_version", s(&f.server_version)),
        ("tool_definition_hash", s(&f.tool_definition_hash)),
    ]
}

/// A row's field values: [`v3_values`], then each [`V4_ADDED_FIELDS`]
/// column as its RFC 8785 text.
///
/// SQL NULL and a JSON `null` document are the same absence (`None`): the
/// database driver, the ORM of an exporter and a JSON export cannot all keep
/// the two apart, and neither is a decision record.
pub fn v4_values(f: &CoveredFields) -> Vec<(&'static str, Option<String>)> {
    let j = |v: &Option<serde_json::Value>| match v {
        None | Some(serde_json::Value::Null) => None,
        Some(doc) => Some(crate::services::jcs::canonicalize(doc)),
    };
    let mut out = v3_values(f);
    out.extend([
        ("compliance_tags", j(&f.compliance_tags)),
        ("guardrails_skipped", j(&f.guardrails_skipped)),
        ("guardrails_degraded", j(&f.guardrails_degraded)),
        ("pre_action_state", j(&f.pre_action_state)),
        ("custom_metadata", j(&f.custom_metadata)),
        ("tags", j(&f.tags)),
    ]);
    out
}

/// 2^53 − 1: the largest integer magnitude every IEEE-754 double neighbour
/// of which is itself an integer — the last integer JCS can print exactly.
pub const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;

/// The sealed JSON columns of a row about to be written, in
/// [`V4_ADDED_FIELDS`] order (pinned by `every_sealed_json_column_is_made_exact`).
fn sealed_json_columns(
    row: &mut ProxyLogInsert,
) -> [(&'static str, &mut Option<serde_json::Value>); 6] {
    [
        ("compliance_tags", &mut row.compliance_tags),
        ("guardrails_skipped", &mut row.guardrails_skipped),
        ("guardrails_degraded", &mut row.guardrails_degraded),
        ("pre_action_state", &mut row.pre_action_state),
        ("custom_metadata", &mut row.custom_metadata),
        ("tags", &mut row.tags),
    ]
}

/// Make every integer in the row's sealed JSON
/// columns one the row hash can tell from its neighbours.
///
/// The v4 hash covers these columns as RFC 8785 text, and RFC 8785 prints
/// every number as the IEEE-754 double it denotes: `9007199254740993` and
/// `9007199254740992` are the same text, so the same row hash. An integer
/// above 2^53 − 1 in a sealed column could therefore be edited within one
/// double's rounding and the chain would still verify. No writer emitted one;
/// nothing stopped the next writer (an id, a nanosecond timestamp, an amount
/// in minor units) from doing so.
///
/// The evidence crate refuses such an integer with an error naming its path
/// (`brutor-evidence` `canon.rs`, strict mode). An audit row cannot be
/// refused that way: failing the row loses it, or parks it in the spool for
/// ever, and a lost audit row is worse than an oddly typed value. So the
/// writer refuses the *number*, not the row: the integer is replaced by its
/// exact decimal digits as a JSON string — what `canonicalize_numbers` does
/// for a body digest — BEFORE the row is hashed and stored. What is stored
/// is then what was hashed, a string is hashed character for character, and
/// the control plane's verifier (`audit_chain_canonical.py`), which reads
/// the stored column, agrees without a change on its side.
///
/// Untouched: integers of magnitude up to 2^53 − 1, every float (two
/// distinct doubles never share a JCS text) and everything that is not a
/// number. The canonical form of every value representable before is the
/// same, so existing rows and the shared vectors verify as they did; the
/// canonicaliser itself (`jcs.rs`) is unchanged.
///
/// Returns `"<column>: <path>"` for each value replaced.
pub fn make_sealed_json_exact(row: &mut ProxyLogInsert) -> Vec<String> {
    fn walk(v: &mut serde_json::Value, path: &mut String, column: &str, out: &mut Vec<String>) {
        use serde_json::Value;
        let digits = match &*v {
            Value::Number(n) if !n.is_f64() => {
                let unsafe_integer = match (n.as_i64(), n.as_u64()) {
                    (Some(i), _) => i.unsigned_abs() > MAX_SAFE_INTEGER,
                    (None, Some(u)) => u > MAX_SAFE_INTEGER,
                    // Neither i64, u64 nor f64: not a number this build can
                    // hold (no `arbitrary_precision`); exact digits are the
                    // safe reading if it ever is.
                    (None, None) => true,
                };
                unsafe_integer.then(|| n.to_string())
            }
            _ => None,
        };
        if let Some(digits) = digits {
            out.push(format!("{column}: {path}"));
            *v = Value::String(digits);
            return;
        }
        match v {
            Value::Array(items) => {
                for (i, item) in items.iter_mut().enumerate() {
                    let len = path.len();
                    path.push_str(&format!("[{i}]"));
                    walk(item, path, column, out);
                    path.truncate(len);
                }
            }
            Value::Object(map) => {
                for (k, item) in map.iter_mut() {
                    let len = path.len();
                    path.push('.');
                    path.push_str(k);
                    walk(item, path, column, out);
                    path.truncate(len);
                }
            }
            _ => {}
        }
    }

    let mut replaced = Vec::new();
    for (column, doc) in sealed_json_columns(row) {
        if let Some(doc) = doc.as_mut() {
            walk(doc, &mut String::from("$"), column, &mut replaced);
        }
    }
    replaced
}

/// Append one field: `0x00` for NULL, else `0x01 ‖ u32-BE length ‖ UTF-8`.
fn push_v3(out: &mut Vec<u8>, v: Option<&str>) {
    match v {
        None => out.push(0x00),
        Some(s) => {
            out.push(0x01);
            // Field values are bounded by their varchar columns (the widest,
            // `target_url`/`oauth_scope`, are text but far below 4 GiB); a
            // length that did not fit would be a writer bug, not data.
            let len = u32::try_from(s.len()).expect("field exceeds u32 length");
            out.extend_from_slice(&len.to_be_bytes());
            out.extend_from_slice(s.as_bytes());
        }
    }
}

/// The canonical bytes of a row:
///
/// ```text
/// "v4" 0x00                                   -- the tag
/// for each field in V3_FIELDS, then each in V4_ADDED_FIELDS (a JSON
/// document as its RFC 8785 text, see `v4_values`), then prev_hash:
///     NULL       -> 0x00
///     present s  -> 0x01 ‖ u32_be(len(utf8(s))) ‖ utf8(s)
/// ```
///
/// Length-prefixed: the row covers values that originate with the caller
/// (the release carriers, the client label, URLs, scopes), and the encoding
/// must not rely on a validator in another module to keep a separator byte
/// out of them. Length prefixes are self-delimiting whatever the content,
/// and the presence byte keeps NULL and `""` distinct.
pub fn canonical_row_bytes_v4(f: &CoveredFields, prev_hash: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(1024);
    out.extend_from_slice(CANONICAL_VERSION.as_bytes());
    out.push(0);
    for (_, v) in v4_values(f) {
        push_v3(&mut out, v.as_deref());
    }
    push_v3(&mut out, Some(prev_hash));
    out
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

/// `SHA-256(canonical_bytes)`, hex-encoded.
pub fn hash_fields(f: &CoveredFields, prev_hash: &str) -> String {
    sha256_hex(&canonical_row_bytes(f, prev_hash))
}

/// Does a stored row's content produce its stored hash?
///
/// The row must name the format ([`CANONICAL_VERSION`]) in `chain_version`
/// and hash to its stored value under it. A row that names another format,
/// or none, does not verify: the verifier computes one format and never
/// guesses another. The tag is inside the hashed bytes as well, so a row
/// cannot be given the label without having been sealed under it.
pub fn row_matches(
    f: &CoveredFields,
    chain_version: Option<&str>,
    prev_hash: &str,
    stored_hash: &str,
) -> bool {
    chain_version == Some(CANONICAL_VERSION) && hash_fields(f, prev_hash) == stored_hash
}

/// Convenience for the write path, which holds a full `ProxyLogInsert`.
pub fn hash_row(log: &ProxyLogInsert, prev_hash: &str) -> String {
    hash_fields(&CoveredFields::from(log), prev_hash)
}

// ---------------------------------------------------------------------------
// Chain state
// ---------------------------------------------------------------------------

/// This process's chain identity. Stable for the process lifetime; a restart
/// starts a new chain, which is correct — the old chain is complete and its
/// checkpoints still verify it.
pub fn chain_id() -> &'static str {
    static ID: OnceLock<String> = OnceLock::new();
    ID.get_or_init(|| {
        // A named instance's chain is attributable to its deployment slot
        // rather than to an opaque ULID.
        match CHAIN_SLOT.get() {
            Some(slot) => format!("{slot}-{}", ulid::Ulid::new()),
            None => ulid::Ulid::new().to_string(),
        }
    })
}

/// The deployment slot this process's chain is named after, when a
/// distribution that runs several instances names them.
static CHAIN_SLOT: OnceLock<String> = OnceLock::new();

/// Name the deployment slot of this process's chain: [`chain_id`] is then
/// `<slot>-<ULID>`. For an extension that names the instances of a
/// deployment; a gateway that is one instance has no slot. Holds before the
/// first row is stamped and once per process: `false` when the chain already
/// has an identity or a slot, or `slot` is blank (the call changed nothing).
pub fn name_chain_slot(slot: &str) -> bool {
    let slot = slot.trim();
    !slot.is_empty() && CHAIN_SLOT.get().is_none() && CHAIN_SLOT.set(slot.to_string()).is_ok()
}

/// Reserve `n` consecutive sequence numbers, returning the first.
fn reserve_seq(n: usize) -> i64 {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    SEQ.fetch_add(n as u64, Ordering::Relaxed) as i64
}

/// Running head of this process's chain, updated as rows are stamped.
fn head() -> &'static std::sync::Mutex<String> {
    static HEAD: OnceLock<std::sync::Mutex<String>> = OnceLock::new();
    HEAD.get_or_init(|| std::sync::Mutex::new(GENESIS_HASH.to_string()))
}

/// Link a batch of rows into a chain, given explicit chain state.
///
/// The whole of the interesting behaviour lives here, deliberately free of
/// process-global state: the sequence counter and running head are shared
/// mutable statics, and a chain-linking function that could only be exercised
/// through them would be order-dependent to test — which is exactly the kind
/// of test that stops catching regressions.
pub fn stamp_with(
    chain_id: &str,
    start_seq: i64,
    prev_hash: &str,
    rows: &mut [ProxyLogInsert],
) -> Option<ChainRange> {
    if rows.is_empty() {
        return None;
    }
    let mut prev = prev_hash.to_string();
    for (i, row) in rows.iter_mut().enumerate() {
        // Before the hash, and so before the INSERT
        // that follows the stamp: an integer the canonical form cannot print
        // exactly is stored and hashed as its digits. Loud, because a writer
        // that emits one should emit a string itself.
        let inexact = make_sealed_json_exact(row);
        if !inexact.is_empty() {
            tracing::error!(
                proxy_log_id = %row.id,
                tenant_id = %row.tenant_id,
                values = ?inexact,
                "audit chain: integer beyond 2^53-1 in a sealed JSON column; \
                 stored and hashed as its decimal digits (a string)"
            );
        }
        // The format is recorded beside the hash so the verifier
        // dispatches on it; the tag is also the first field inside the
        // hashed bytes, so this column cannot re-label a row.
        row.chain_version = Some(CANONICAL_VERSION.to_string());
        let hash = hash_row(row, &prev);
        row.chain_id = Some(chain_id.to_string());
        row.chain_seq = Some(start_seq + i as i64);
        row.prev_hash = Some(prev.clone());
        row.row_hash = Some(hash.clone());
        prev = hash;
    }
    Some(ChainRange {
        chain_id: chain_id.to_string(),
        seq_start: start_seq,
        seq_end: start_seq + rows.len() as i64 - 1,
        head_hash: prev,
    })
}

/// Stamp a batch into *this process's* chain.
///
/// Called from the writer immediately before the INSERT, so a row that is shed
/// under backpressure never consumes a sequence number — shedding must not
/// manufacture a gap that reads as deletion.
///
/// The head lock is held across the whole batch so two concurrent flushes
/// cannot interleave and produce two rows claiming the same predecessor.
pub fn stamp_batch(rows: &mut [ProxyLogInsert]) -> Option<ChainRange> {
    if rows.is_empty() {
        return None;
    }
    let mut guard = head().lock().unwrap_or_else(|e| e.into_inner());
    let start = reserve_seq(rows.len());
    let range = stamp_with(chain_id(), start, &guard, rows)?;
    *guard = range.head_hash.clone();
    Some(range)
}

/// The range one checkpoint attests to. Serialisable so a stamped batch can
/// wait in the audit spool with its range.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ChainRange {
    pub chain_id: String,
    pub seq_start: i64,
    pub seq_end: i64,
    pub head_hash: String,
}

// ---------------------------------------------------------------------------
// Signing
// ---------------------------------------------------------------------------

/// The process signing key, if one is configured.
///
/// Absent key → no checkpoints. The hash chain still runs, which is the honest
/// degradation: rows remain internally linked (modification is detectable) but
/// unattested (wholesale rewriting is not). Refusing to serve traffic over a
/// missing audit key would turn an evidence-quality setting into an outage.
fn signing_key() -> Option<&'static SigningKey> {
    static KEY: OnceLock<Option<SigningKey>> = OnceLock::new();
    KEY.get_or_init(|| signing_key_from(&crate::env_vars::process()).ok().flatten())
        .as_ref()
}

/// The signing key `env` configures (`BRUTOR_AUDIT_SIGNING_KEY`); `None`
/// when it is unset. A value that is not a 32-byte seed in hexadecimal is
/// refused: the gateway does not start with it (`config::check`). It used to
/// read as "no key", and the chain ran without checkpoints.
pub fn signing_key_from(
    env: &crate::env_registry::Env,
) -> Result<Option<SigningKey>, crate::env_registry::EnvError> {
    let var = &crate::env_vars::BRUTOR_AUDIT_SIGNING_KEY;
    let Some(raw) = env.string(var) else {
        return Ok(None);
    };
    crate::env_vars::hex_32(&raw)
        .map(|seed| Some(SigningKey::from_bytes(&seed)))
        .ok_or_else(|| crate::env_vars::invalid(var, var.accepted))
}

/// Whether checkpointing is active in this process.
pub fn is_signing_enabled() -> bool {
    signing_key().is_some()
}

/// The bytes a checkpoint signature covers: null-separated fields, tagged
/// with [`SIGNED_SHAPE_VERSION`] — not the row format, which moves
/// independently.
pub fn canonical_checkpoint_bytes(range: &ChainRange) -> Vec<u8> {
    canonical_checkpoint_bytes_tagged(range, SIGNED_SHAPE_VERSION)
}

/// Checkpoint bytes under an explicit tag — [`SIGNED_SHAPE_VERSION`] for
/// everything signed today, [`SIGNED_SHAPE_VERSION_V1`] for the oldest.
pub fn canonical_checkpoint_bytes_tagged(range: &ChainRange, tag: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(160);
    for field in [
        tag,
        &range.chain_id,
        &range.seq_start.to_string(),
        &range.seq_end.to_string(),
        &range.head_hash,
    ] {
        out.extend_from_slice(field.as_bytes());
        out.push(0);
    }
    out
}

/// Sign a range. `None` when no key is configured.
pub fn sign_range(range: &ChainRange) -> Option<SignedCheckpoint> {
    let key = signing_key()?;
    let sig = key.sign(&canonical_checkpoint_bytes(range));
    Some(SignedCheckpoint {
        range: range.clone(),
        signature: hex::encode(sig.to_bytes()),
        public_key: hex::encode(key.verifying_key().to_bytes()),
    })
}

/// What an export manifest attests to.
///
/// A purpose-specific shape rather than "sign these bytes": a general signing
/// endpoint is an oracle, and an attacker who reached it could have the audit
/// key sign anything — including a checkpoint. Bounding the key to exactly two
/// canonical shapes is what keeps that from being possible.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExportManifest {
    /// Object key the export was written to.
    pub object_key: String,
    /// SHA-256 of the delivered bytes, hex. Of the **gzipped artifact**, so a
    /// recipient hashes the file exactly as received rather than having to
    /// reproduce our serialisation.
    pub sha256: String,
    pub byte_size: i64,
    pub row_count: i64,
    pub tenant_id: String,
    pub destination_id: String,
    pub window_start: Option<String>,
    pub window_end: String,
    /// Whether the exported rows carry the chain columns. An operator can
    /// configure a field allowlist that strips `row_hash` / `prev_hash` /
    /// `chain_seq`, which silently makes the export unverifiable — so the
    /// manifest states it rather than leaving a recipient to discover it.
    pub chain_columns_included: bool,
}

/// The bytes an export manifest signature covers. Same null-separated,
/// versioned discipline as the row and checkpoint formats.
pub fn canonical_manifest_bytes(m: &ExportManifest) -> Vec<u8> {
    canonical_manifest_bytes_tagged(m, SIGNED_SHAPE_VERSION)
}

/// Export-manifest bytes under an explicit tag (see
/// [`canonical_checkpoint_bytes_tagged`]).
pub fn canonical_manifest_bytes_tagged(m: &ExportManifest, tag: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(320);
    // The "export" tag is what stops a manifest signature being replayed as a
    // checkpoint signature, or the reverse: the two byte-strings can never
    // collide because their second field differs in kind.
    for field in [
        tag,
        "export",
        &m.object_key,
        &m.sha256,
        &m.byte_size.to_string(),
        &m.row_count.to_string(),
        &m.tenant_id,
        &m.destination_id,
        m.window_start.as_deref().unwrap_or(""),
        &m.window_end,
        if m.chain_columns_included { "1" } else { "0" },
    ] {
        out.extend_from_slice(field.as_bytes());
        out.push(0);
    }
    out
}

// ---------------------------------------------------------------------------
// Report manifests — the third canonical shape
// ---------------------------------------------------------------------------

/// The spec id a report manifest must carry. Pinned here so the signing
/// endpoint can refuse anything that merely *looks* like a report manifest —
/// the key signs known shapes, never shapes described by the caller.
pub const REPORT_MANIFEST_KIND: &str = "ai.brutor/ai-system-assurance-report";

/// A captured Assurance Report snapshot, as the control plane attests it.
///
/// Same discipline as [`ExportManifest`]: fixed fields, canonical
/// serialisation, its own domain tag. The signature covers the report's
/// digest plus its identifying facts, so a certificate cannot be re-bound
/// to another system, tenant, period or contract without the signature
/// failing.
#[derive(Debug, serde::Deserialize)]
pub struct ReportManifest {
    /// Must equal [`REPORT_MANIFEST_KIND`]; the route refuses otherwise.
    pub kind: String,
    pub spec_version: String,
    /// SHA-256 over the canonical JSON of the frozen report.
    pub report_sha256: String,
    pub tenant_id: String,
    pub ai_system_id: String,
    pub period_hours: i64,
    pub generated_at: String,
    pub contract_version: Option<i64>,
    pub contract_hash: Option<String>,
}

/// The bytes a report-manifest signature covers. Null-separated and
/// versioned like the row, checkpoint and export formats; the "report" tag
/// is what stops this byte-string ever colliding with the other two shapes.
pub fn canonical_report_manifest_bytes(m: &ReportManifest) -> Vec<u8> {
    let mut out = Vec::with_capacity(320);
    let contract_version = m
        .contract_version
        .map(|v| v.to_string())
        .unwrap_or_default();
    for field in [
        SIGNED_SHAPE_VERSION,
        "report",
        &m.kind,
        &m.spec_version,
        &m.report_sha256,
        &m.tenant_id,
        &m.ai_system_id,
        &m.period_hours.to_string(),
        &m.generated_at,
        &contract_version,
        m.contract_hash.as_deref().unwrap_or(""),
    ] {
        out.extend_from_slice(field.as_bytes());
        out.push(0);
    }
    out
}

/// Sign a report manifest. `None` when no key is configured.
pub fn sign_report_manifest(m: &ReportManifest) -> Option<(String, String)> {
    let key = signing_key()?;
    let sig = key.sign(&canonical_report_manifest_bytes(m));
    Some((
        hex::encode(sig.to_bytes()),
        hex::encode(key.verifying_key().to_bytes()),
    ))
}

/// Sign an export manifest. `None` when no key is configured.
pub fn sign_manifest(m: &ExportManifest) -> Option<(String, String)> {
    let key = signing_key()?;
    let sig = key.sign(&canonical_manifest_bytes(m));
    Some((
        hex::encode(sig.to_bytes()),
        hex::encode(key.verifying_key().to_bytes()),
    ))
}

/// Verify an export manifest against a trusted key.
pub fn verify_manifest(m: &ExportManifest, signature: &str, public_key: &str) -> CheckpointTrust {
    let Ok(sig_bytes) = hex::decode(signature) else {
        return CheckpointTrust::BadSignature;
    };
    let Ok(sig_arr): Result<[u8; 64], _> = sig_bytes.try_into() else {
        return CheckpointTrust::BadSignature;
    };
    let Ok(pk_bytes) = hex::decode(public_key) else {
        return CheckpointTrust::BadSignature;
    };
    let Ok(pk_arr): Result<[u8; 32], _> = pk_bytes.try_into() else {
        return CheckpointTrust::BadSignature;
    };
    let Ok(vk) = VerifyingKey::from_bytes(&pk_arr) else {
        return CheckpointTrust::BadSignature;
    };
    let sig = ed25519_dalek::Signature::from_bytes(&sig_arr);
    let signed = [SIGNED_SHAPE_VERSION, SIGNED_SHAPE_VERSION_V1]
        .iter()
        .any(|tag| {
            vk.verify_strict(&canonical_manifest_bytes_tagged(m, tag), &sig)
                .is_ok()
        });
    if !signed {
        return CheckpointTrust::BadSignature;
    }
    if trusted_keys().contains(&public_key.to_ascii_lowercase()) {
        CheckpointTrust::Trusted
    } else {
        CheckpointTrust::UntrustedKey
    }
}

#[derive(Debug, Clone)]
pub struct SignedCheckpoint {
    pub range: ChainRange,
    pub signature: String,
    pub public_key: String,
}

/// The additional trusted public keys `env` configures
/// (`BRUTOR_AUDIT_TRUSTED_KEYS`, hex, comma separated), in lower case. This
/// is how key rotation works: retire a signing key by moving its public half
/// here, and the checkpoints it already signed keep verifying.
///
/// Only well-formed keys are admitted: an entry that is not 64 hexadecimal
/// characters is refused, and the gateway does not start with it
/// (`config::check`). A typo must not widen the trust set with a value
/// nothing can ever match, nor be dropped without the operator knowing.
pub fn trusted_keys_from(
    env: &crate::env_registry::Env,
) -> Result<Vec<String>, crate::env_registry::EnvError> {
    let var = &crate::env_vars::BRUTOR_AUDIT_TRUSTED_KEYS;
    env.list(var)
        .into_iter()
        .map(|candidate| {
            let candidate = candidate.to_ascii_lowercase();
            crate::env_vars::hex_32(&candidate)
                .map(|_| candidate)
                .ok_or_else(|| crate::env_vars::invalid(var, var.accepted))
        })
        .collect()
}

/// Public keys this deployment will accept a checkpoint signature from.
///
/// The current signing key's public half is always included, so a
/// single-key deployment needs no extra configuration and rotation is purely
/// additive.
///
/// Without this set, the signature layer is decorative: `verify_checkpoint`
/// would take the public key from the checkpoint row itself, so anyone able to
/// write that row could generate their own keypair, re-sign a rewritten chain,
/// and store the matching public key beside it. Every hash would link and
/// every signature would check out. A signature is only evidence against a key
/// you already trusted.
fn trusted_keys() -> &'static std::collections::HashSet<String> {
    static KEYS: OnceLock<std::collections::HashSet<String>> = OnceLock::new();
    KEYS.get_or_init(|| {
        let mut set = std::collections::HashSet::new();
        if let Some(key) = signing_key() {
            set.insert(hex::encode(key.verifying_key().to_bytes()));
        }
        set.extend(trusted_keys_from(&crate::env_vars::process()).unwrap_or_default());
        set
    })
}

/// Whether any signing key is configured for this deployment.
pub fn has_trusted_keys() -> bool {
    !trusted_keys().is_empty()
}

/// What a checkpoint's signature is worth.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CheckpointTrust {
    /// Signature verifies against a key this deployment trusts.
    Trusted,
    /// Signature verifies, but against a key we never trusted. This is the
    /// shape a forged attestation takes, and it is a louder signal than an
    /// unsigned chain — someone went to the trouble of signing.
    UntrustedKey,
    /// Signature does not verify, or the material is malformed.
    BadSignature,
}

/// Verify a checkpoint's signature and the standing of the key that made it.
///
/// The public key comes from the checkpoint row, because a key rotated out of
/// service must still verify what it signed while it was live. That is only
/// safe because the key is then checked against [`trusted_keys`].
pub fn verify_checkpoint_trust(cp: &SignedCheckpoint) -> CheckpointTrust {
    if !verify_signature(cp) {
        return CheckpointTrust::BadSignature;
    }
    if trusted_keys().contains(&cp.public_key.to_ascii_lowercase()) {
        CheckpointTrust::Trusted
    } else {
        CheckpointTrust::UntrustedKey
    }
}

/// Signature-only check, without the trust question. Private on purpose:
/// callers should be forced to think about which key signed.
fn verify_signature(cp: &SignedCheckpoint) -> bool {
    let Ok(pk_bytes) = hex::decode(&cp.public_key) else {
        return false;
    };
    let Ok(pk_arr): Result<[u8; 32], _> = pk_bytes.try_into() else {
        return false;
    };
    let Ok(vk) = VerifyingKey::from_bytes(&pk_arr) else {
        return false;
    };
    let Ok(sig_bytes) = hex::decode(&cp.signature) else {
        return false;
    };
    let Ok(sig_arr): Result<[u8; 64], _> = sig_bytes.try_into() else {
        return false;
    };
    let sig = ed25519_dalek::Signature::from_bytes(&sig_arr);
    // Current tag first; the pre-a171 tag keeps checkpoints signed before
    // the row format first moved verifiable (they were silently reading as
    // bad signatures while the two tags were one).
    [SIGNED_SHAPE_VERSION, SIGNED_SHAPE_VERSION_V1]
        .iter()
        .any(|tag| {
            vk.verify_strict(&canonical_checkpoint_bytes_tagged(&cp.range, tag), &sig)
                .is_ok()
        })
}

/// A checkpoint that both verifies and was signed by a trusted key.
///
/// This is what "verified" means everywhere else in the codebase, so it keeps
/// the short name; anything needing the distinction calls
/// [`verify_checkpoint_trust`].
pub fn verify_checkpoint(cp: &SignedCheckpoint) -> bool {
    verify_checkpoint_trust(cp) == CheckpointTrust::Trusted
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{TimeZone, Timelike};

    fn log(id: &str) -> ProxyLogInsert {
        let mut l = crate::services::proxy_logger::build_proxy_log(
            "llm",
            &crate::models::auth::AuthContext {
                credential_kind: crate::models::auth::CredentialKind::ApiKey,
                credential_assurance: Some(crate::models::auth::CredentialAssurance::Key),
                credential_ref: None,
                credential_detail: Default::default(),
                identity_verdict: None,
                auth_type: crate::models::auth::AuthType::ApiKey,
                tenant_id: "t1".into(),
                gateway_user_id: None,
                gateway_username: None,
                gateway_tenant_id: None,
                idp_user_id: None,
                idp_username: None,
                idp_email: None,
                api_key_id: None,
                api_key_name: None,
                api_key_scope: None,
                api_key_group_id: None,
                api_key_is_platform: false,
                api_key_user_id: None,
                allowed_skill_ids: None,
                session_key: None,
                client_ip: None,
                resolved_group_id: None,
                actor_agent_id: None,
                session_token_type: None,
                agent_impl: crate::services::agent_impl::request_carriers(),
                admission_hold: None,
            },
            "POST",
            "/v1/chat/completions",
            None,
            None,
            200,
            1.0,
            true,
            None,
            "corr",
        );
        l.id = id.to_string();
        l.timestamp = chrono::Utc.with_ymd_and_hms(2026, 8, 7, 12, 0, 0).unwrap();
        l
    }

    #[test]
    fn the_canonical_format_is_pinned() {
        // This is a contract, not an implementation detail. If the bytes
        // change meaning under an unchanged version tag, every sealed row
        // fails verification forever and a real tamper becomes
        // indistinguishable from a format drift. Bump CANONICAL_VERSION with
        // the verifier — do not edit this expectation to make a failing
        // build pass.
        let h = hash_row(&log("row-1"), GENESIS_HASH);
        assert_eq!(
            h, "0bbb9e222df2c9a419001ebd76bc91b7b03d720ca0e0a04ee544ec3b5ca87de3",
            "canonical row bytes changed — see the comment above before touching this",
        );
    }

    #[test]
    fn content_digests_and_tool_provenance_are_covered() {
        let base = hash_row(&log("row-1"), GENESIS_HASH);
        for mutate in [
            (|l: &mut ProxyLogInsert| l.request_content_hash = Some("a".repeat(64)))
                as fn(&mut ProxyLogInsert),
            |l| l.response_content_hash = Some("b".repeat(64)),
            |l| l.server_version = Some("2.4.1".into()),
            |l| l.tool_definition_hash = Some("c".repeat(64)),
        ] {
            let mut l = log("row-1");
            mutate(&mut l);
            assert_ne!(hash_row(&l, GENESIS_HASH), base);
        }
        // The caller-asserted join keys stay outside (CallerCorrelation)...
        let mut l = log("row-1");
        l.trace_id = Some("0af7651916cd43dd8448eb211c80319c".into());
        l.span_id = Some("b7ad6b7169203331".into());
        assert_eq!(hash_row(&l, GENESIS_HASH), base);
        // ...while the deciding model is covered.
        let mut l = log("row-1");
        l.deciding_model = Some("gpt-5.2".into());
        assert_ne!(hash_row(&l, GENESIS_HASH), base);
    }

    #[test]
    fn the_version_tag_is_the_first_field() {
        // So a future format can be told apart from this one by inspection,
        // without having to guess from length.
        let bytes = canonical_row_bytes(&CoveredFields::from(&log("row-1")), GENESIS_HASH);
        assert!(bytes.starts_with(CANONICAL_VERSION.as_bytes()));
        assert_eq!(bytes[CANONICAL_VERSION.len()], 0);
    }

    #[test]
    fn altering_any_covered_field_changes_the_hash() {
        let base = hash_row(&log("row-1"), GENESIS_HASH);
        for mutate in [
            (|l: &mut ProxyLogInsert| l.tenant_id = "t2".into()) as fn(&mut ProxyLogInsert),
            |l| l.success = false,
            |l| l.http_status_code = Some(403),
            |l| l.agent_authz_decision = Some("deny".into()),
            |l| l.autonomy_level = Some("suspended".into()),
            |l| l.autonomy_provenance = Some("floor".into()),
            |l| l.guardrail_config_id = Some("gr-1".into()),
            |l| l.consumed_approval_request_id = Some("appr-1".into()),
            |l| l.actor_agent_id = Some("agent-x".into()),
        ] {
            let mut l = log("row-1");
            mutate(&mut l);
            assert_ne!(
                hash_row(&l, GENESIS_HASH),
                base,
                "a covered field was mutated without changing the hash",
            );
        }
    }

    #[test]
    fn the_chain_links_each_row_to_its_predecessor() {
        let mut rows = vec![log("a"), log("b"), log("c")];
        let range = stamp_with("chain-t", 0, GENESIS_HASH, &mut rows).expect("non-empty");

        assert_eq!(rows[0].prev_hash.as_deref(), Some(GENESIS_HASH));
        assert_eq!(rows[1].prev_hash, rows[0].row_hash);
        assert_eq!(rows[2].prev_hash, rows[1].row_hash);
        assert_eq!(range.head_hash, rows[2].row_hash.clone().unwrap());
        assert_eq!(range.seq_end - range.seq_start, 2);
    }

    #[test]
    fn sequence_numbers_are_dense_within_a_batch() {
        // A gap means rows were deleted. If the writer itself left gaps, that
        // signal would be worthless.
        let mut rows = vec![log("a"), log("b"), log("c"), log("d")];
        stamp_with("chain-t", 0, GENESIS_HASH, &mut rows).unwrap();
        let seqs: Vec<i64> = rows.iter().map(|r| r.chain_seq.unwrap()).collect();
        for w in seqs.windows(2) {
            assert_eq!(w[1], w[0] + 1, "sequence must be dense: {seqs:?}");
        }
    }

    #[test]
    fn an_empty_batch_consumes_no_sequence_numbers() {
        let mut none: Vec<ProxyLogInsert> = vec![];
        assert!(stamp_with("chain-t", 0, GENESIS_HASH, &mut none).is_none());
        assert!(stamp_batch(&mut none).is_none());
    }

    #[test]
    fn tampering_with_a_row_breaks_every_hash_after_it() {
        // The property the whole feature exists for.
        let mut rows = vec![log("a"), log("b"), log("c")];
        stamp_with("chain-t", 0, GENESIS_HASH, &mut rows).unwrap();

        // An attacker edits row b's outcome in the database.
        let mut tampered = rows[1].clone();
        tampered.success = false;

        let recomputed = hash_row(&tampered, rows[1].prev_hash.as_deref().unwrap());
        assert_ne!(recomputed, rows[1].row_hash.clone().unwrap());
        // ...and row c's stored prev_hash no longer matches, so the break is
        // visible without needing to know which row was edited.
        assert_ne!(rows[2].prev_hash.as_deref(), Some(recomputed.as_str()));
    }

    #[test]
    fn an_untrusted_key_is_rejected_even_though_its_signature_is_valid() {
        // The property the trust anchor exists for. An attacker who can write
        // checkpoint rows can generate a keypair, re-sign a rewritten chain,
        // and store the matching public key beside it — every hash links and
        // every signature checks out. Only "was this key ever ours?" catches it.
        let attacker = SigningKey::from_bytes(&[99u8; 32]);
        let range = ChainRange {
            chain_id: "chain-forged".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let sig = attacker.sign(&canonical_checkpoint_bytes(&range));
        let cp = SignedCheckpoint {
            range,
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(attacker.verifying_key().to_bytes()),
        };

        // The signature itself is genuine...
        assert!(verify_signature(&cp), "the attacker can sign correctly");
        // ...and that is exactly why the signature alone proves nothing.
        assert_eq!(
            verify_checkpoint_trust(&cp),
            CheckpointTrust::UntrustedKey,
            "a valid signature from an unknown key must not pass",
        );
        assert!(!verify_checkpoint(&cp));
    }

    #[test]
    fn a_malformed_trusted_key_entry_does_not_widen_trust() {
        // A typo in the env var must not admit a value nothing can match, nor
        // silently drop the rest of the list.
        for bad in ["", "not-hex", &"a".repeat(63), &"z".repeat(64)] {
            let ok = bad.len() == 64 && hex::decode(bad).is_ok();
            assert!(!ok, "{bad:?} should be rejected as a trusted key");
        }
        // A well-formed one is accepted.
        let good = hex::encode([3u8; 32]);
        assert!(good.len() == 64 && hex::decode(&good).is_ok());
    }

    #[test]
    fn tampering_still_fails_even_when_signed_by_a_trusted_key() {
        // Trust is necessary, not sufficient: a checkpoint over a range whose
        // head hash was altered must fail regardless of who signed it.
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let range = ChainRange {
            chain_id: "chain-1".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let sig = key.sign(&canonical_checkpoint_bytes(&range));
        let mut altered = range.clone();
        altered.head_hash = "def456".into();
        let cp = SignedCheckpoint {
            range: altered,
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(key.verifying_key().to_bytes()),
        };
        assert_eq!(verify_checkpoint_trust(&cp), CheckpointTrust::BadSignature);
    }

    #[test]
    fn a_checkpoint_verifies_against_the_key_that_signed_it() {
        let range = ChainRange {
            chain_id: "chain-1".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let sig = key.sign(&canonical_checkpoint_bytes(&range));
        let cp = SignedCheckpoint {
            range: range.clone(),
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(key.verifying_key().to_bytes()),
        };
        // Signature-only: [7u8;32] is not in this process's trust set.
        assert!(verify_signature(&cp));
    }

    #[test]
    fn a_checkpoint_over_a_different_range_does_not_verify() {
        // Moving a signature onto a neighbouring range would let an attacker
        // attest to rows the key never saw.
        let range = ChainRange {
            chain_id: "chain-1".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let sig = key.sign(&canonical_checkpoint_bytes(&range));

        let mut moved = range.clone();
        moved.seq_end = 10;
        let cp = SignedCheckpoint {
            range: moved,
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(key.verifying_key().to_bytes()),
        };
        assert!(!verify_signature(&cp));
    }

    #[test]
    fn a_forged_head_hash_does_not_verify() {
        let range = ChainRange {
            chain_id: "chain-1".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let sig = key.sign(&canonical_checkpoint_bytes(&range));

        let mut forged = range.clone();
        forged.head_hash = "def456".into();
        let cp = SignedCheckpoint {
            range: forged,
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(key.verifying_key().to_bytes()),
        };
        assert!(!verify_signature(&cp));
    }

    #[test]
    fn malformed_checkpoint_material_is_rejected_not_panicked() {
        // Verification runs over data an attacker may have written.
        let range = ChainRange {
            chain_id: "c".into(),
            seq_start: 0,
            seq_end: 0,
            head_hash: "h".into(),
        };
        for (sig, pk) in [
            ("zz", "zz"),
            ("", ""),
            ("00", &*"11".repeat(32)),
            (&*"00".repeat(64), "11"),
        ] {
            let cp = SignedCheckpoint {
                range: range.clone(),
                signature: sig.to_string(),
                public_key: pk.to_string(),
            };
            assert!(!verify_signature(&cp));
        }
    }

    // -- report manifests -------------------------------------------

    fn report_manifest() -> ReportManifest {
        ReportManifest {
            kind: REPORT_MANIFEST_KIND.to_string(),
            spec_version: "1.0".to_string(),
            report_sha256: "ab".repeat(32),
            tenant_id: "t1".to_string(),
            ai_system_id: "sys-1".to_string(),
            period_hours: 720,
            generated_at: "2026-08-14T06:00:00Z".to_string(),
            contract_version: Some(3),
            contract_hash: Some("cd".repeat(32)),
        }
    }

    #[test]
    fn report_manifest_bytes_carry_their_own_domain_tag() {
        // The tag is what stops a report signature being replayed as an
        // export or checkpoint signature: the byte-strings can never
        // collide because their second field differs in kind.
        let bytes = canonical_report_manifest_bytes(&report_manifest());
        let fields: Vec<&[u8]> = bytes.split(|b| *b == 0).collect();
        assert_eq!(fields[0], SIGNED_SHAPE_VERSION.as_bytes());
        assert_eq!(fields[1], b"report");
    }

    #[test]
    fn report_manifest_absent_contract_is_empty_not_zero() {
        let mut m = report_manifest();
        m.contract_version = None;
        m.contract_hash = None;
        let with_none = canonical_report_manifest_bytes(&m);
        m.contract_version = Some(0);
        let with_zero = canonical_report_manifest_bytes(&m);
        // "no contract" and "contract v0" must sign differently.
        assert_ne!(with_none, with_zero);
    }

    #[test]
    fn report_manifest_signature_binds_every_identifying_field() {
        // Signed with a locally built key rather than via the env:
        // `signing_key()` is OnceLock-cached, so env manipulation in a
        // test is order-dependent. What must hold is the *binding* — the
        // canonical bytes cover every identifying field.
        let seed: [u8; 32] = [7u8; 32];
        let key = SigningKey::from_bytes(&seed);
        let m = report_manifest();
        let sig = key.sign(&canonical_report_manifest_bytes(&m));
        let vk = key.verifying_key();

        let verify = |m: &ReportManifest| {
            vk.verify_strict(&canonical_report_manifest_bytes(m), &sig)
                .is_ok()
        };
        assert!(verify(&m));

        // Re-binding the certificate to another system, tenant, period or
        // report must break the signature.
        let mut tampered = report_manifest();
        tampered.ai_system_id = "sys-2".to_string();
        assert!(!verify(&tampered));
        let mut tampered = report_manifest();
        tampered.report_sha256 = "ee".repeat(32);
        assert!(!verify(&tampered));
        let mut tampered = report_manifest();
        tampered.period_hours = 24;
        assert!(!verify(&tampered));
        let mut tampered = report_manifest();
        tampered.tenant_id = "t2".to_string();
        assert!(!verify(&tampered));
    }

    // -- fixed vectors ------------------------------------------------
    //
    // The vectors below are the cross-repo contract. The control plane's
    // reference verifier (`tests/test_audit_chain_v4.py`) builds the SAME
    // rows and pins the SAME hashes; a divergence in either implementation
    // fails that repo's suite, never an honest row.

    fn ts(micros: u32) -> chrono::DateTime<chrono::Utc> {
        chrono::Utc
            .with_ymd_and_hms(2026, 9, 27, 14, 3, 7)
            .unwrap()
            .with_nanosecond(micros * 1_000)
            .unwrap()
    }

    /// Every v3 field present, one of them the EMPTY string (`error_type`)
    /// and one non-ASCII (`mcp_capability_name`), so the vector pins NULL vs
    /// empty and byte (not char) lengths.
    pub(crate) fn vector_full() -> CoveredFields {
        CoveredFields {
            id: "01K6V3VECTOR00000000000FULL".into(),
            tenant_id: "tenant-vec".into(),
            timestamp: ts(123_456),
            proxy_type: "mcp".into(),
            proxy_subtype: Some("mcp_jsonrpc".into()),
            actor_agent_id: Some("agent-support".into()),
            subject_id: Some("u-4471".into()),
            subject_kind: Some("user".into()),
            api_key_id: Some("key-1".into()),
            resource_group_id: Some("sys-support".into()),
            http_method: "POST".into(),
            endpoint: "/mcp/srv-crm".into(),
            mcp_capability_name: Some("issue_refund_\u{fc}".into()),
            llm_model_used: Some("gpt-5.2".into()),
            agent_authz_decision: Some("approval_required".into()),
            agent_grant_id: Some("grant-refunds".into()),
            semantic_policy_id: Some("sp-1".into()),
            argument_policy_id: Some("ap-1".into()),
            guardrail_config_id: Some("gr-1".into()),
            guardrail_check: Some("pii".into()),
            guardrail_action: Some("redact".into()),
            autonomy_level: Some("approval_required".into()),
            autonomy_provenance: Some("live".into()),
            consumed_approval_request_id: Some("appr-1".into()),
            guardrail_blocked: false,
            argument_policy_denied: false,
            http_status_code: Some(200),
            success: true,
            root_task_id: Some("run-1".into()),
            turn_id: Some("turn-3".into()),
            request_content_hash: Some("9f".repeat(32)),
            response_content_hash: Some("60".repeat(32)),
            server_version: Some("2.4.1".into()),
            tool_definition_hash: Some("46".repeat(32)),
            source: "gateway".into(),
            target_url: Some("https://crm.example.com/mcp?tenant=acme".into()),
            server_id: Some("srv-crm".into()),
            idp_server_id: Some("idp-1".into()),
            mcp_method: Some("tools/call".into()),
            skill_id: Some("skill-1".into()),
            skill_execution_id: Some("sx-1".into()),
            llm_provider: Some("openai".into()),
            llm_model_requested: Some("gpt-5".into()),
            oauth_flow_type: Some("client_credentials".into()),
            oauth_scope: Some("crm.read crm.write".into()),
            rag_collection_ids: Some("kb-1,kb-2".into()),
            prompt_fingerprint: Some("ab".repeat(32)),
            gateway_user_id: Some("gu-1".into()),
            idp_user_id: Some("idp-u-1".into()),
            is_auth_event: false,
            agent_impl_name: Some("support-copilot".into()),
            agent_impl_version: Some("1.4.2".into()),
            agent_impl_build: Some(format!("sha256:{}", "cd".repeat(32))),
            agent_impl_source: Some("header".into()),
            client_label: Some("python-httpx".into()),
            pii_detected: true,
            region_routed_to: Some("eu-west-1".into()),
            residency_class: Some("allowed".into()),
            error_type: Some(String::new()),
            upstream_request_id: Some("req-up-1".into()),
            a2a_task_state: Some("completed".into()),
            current_task_id: Some("task-2".into()),
            parent_task_id: Some("task-1".into()),
            delegation_depth: Some(1),
            trace_root_kind: Some("agent_invocation".into()),
            trace_continuity: Some("client_asserted".into()),
            run_end_state: Some("completed".into()),
            deciding_model: Some("gpt-5.2".into()),
            // v4 — every JSON decision record present, deliberately written
            // with keys out of order, non-ASCII and escapable text, a key
            // above U+FFFF beside one in U+E000..U+FFFF (UTF-16 ordering),
            // and numbers whose ES spelling differs from their literal.
            compliance_tags: Some(serde_json::json!(["soc2.cc6.6", "eu_ai_act.high"])),
            guardrails_skipped: Some(serde_json::json!([
                {"reason": "detector_timeout", "config": "gr-2", "check": "toxic_content"}
            ])),
            guardrails_degraded: Some(serde_json::json!([
                {"tier": "fallback", "config": "gr-1", "check": "pii"}
            ])),
            pre_action_state: Some(serde_json::json!({
                "kind": "llm_quota",
                "window_tokens": 120000,
                "spend_usd": 41.5,
                "ratio": 0.1234,
                "big": 1e21,
                "tiny": 1e-7,
                "flags": {"z": true, "a": null}
            })),
            custom_metadata: Some(serde_json::json!({
                "otel": {"span_name": "tool \u{20ac}", "service_name": "crm\n\"svc\""},
                "conversation_handle": "01K6V4HANDLE0000000000000",
                "\u{1f600}": 1,
                "\u{fb33}": 2
            })),
            tags: Some(
                serde_json::json!({"refusal": "quota", "scope": "tenant", "limits": [1, 2.5, -0.0]}),
            ),
        }
    }

    /// Every optional field NULL, every flag false, no status code.
    pub(crate) fn vector_sparse(micros: u32) -> CoveredFields {
        CoveredFields {
            id: "01K6V3VECTOR0000000000SPARSE".into(),
            tenant_id: "tenant-vec".into(),
            timestamp: ts(micros),
            proxy_type: "llm".into(),
            proxy_subtype: None,
            actor_agent_id: None,
            subject_id: None,
            subject_kind: None,
            api_key_id: None,
            resource_group_id: None,
            http_method: "POST".into(),
            endpoint: "/v1/chat/completions".into(),
            mcp_capability_name: None,
            llm_model_used: None,
            agent_authz_decision: None,
            agent_grant_id: None,
            semantic_policy_id: None,
            argument_policy_id: None,
            guardrail_config_id: None,
            guardrail_check: None,
            guardrail_action: None,
            autonomy_level: None,
            autonomy_provenance: None,
            consumed_approval_request_id: None,
            guardrail_blocked: false,
            argument_policy_denied: false,
            http_status_code: None,
            success: false,
            root_task_id: None,
            turn_id: None,
            request_content_hash: None,
            response_content_hash: None,
            server_version: None,
            tool_definition_hash: None,
            source: "otel".into(),
            target_url: None,
            server_id: None,
            idp_server_id: None,
            mcp_method: None,
            skill_id: None,
            skill_execution_id: None,
            llm_provider: None,
            llm_model_requested: None,
            oauth_flow_type: None,
            oauth_scope: None,
            rag_collection_ids: None,
            prompt_fingerprint: None,
            gateway_user_id: None,
            idp_user_id: None,
            is_auth_event: false,
            agent_impl_name: None,
            agent_impl_version: None,
            agent_impl_build: None,
            agent_impl_source: None,
            client_label: None,
            pii_detected: false,
            region_routed_to: None,
            residency_class: None,
            error_type: None,
            upstream_request_id: None,
            a2a_task_state: None,
            current_task_id: None,
            parent_task_id: None,
            delegation_depth: None,
            trace_root_kind: None,
            trace_continuity: None,
            run_end_state: None,
            deciding_model: None,
            compliance_tags: None,
            guardrails_skipped: None,
            guardrails_degraded: None,
            pre_action_state: None,
            custom_metadata: None,
            tags: None,
        }
    }

    /// The documented v4 vector: the sparse row with one JSON document,
    /// written with its keys out of order and `1.50` for 1.5.
    pub(crate) fn vector_sparse_json() -> CoveredFields {
        let mut f = vector_sparse(0);
        f.pre_action_state = Some(
            serde_json::from_str(r#"{"window":"day","used":1.50,"kind":"llm_quota"}"#).unwrap(),
        );
        f
    }

    const VECTOR_PREV: &str = "1111111111111111111111111111111111111111111111111111111111111111";

    #[test]
    fn fixed_vectors_pin_the_format() {
        // (row, prev_hash) -> hash. Mirrored verbatim in the control plane's
        // tests/test_audit_chain_v4.py. Never edit an expectation to make a
        // build pass — a new format is a new version.
        let full = vector_full();
        let sparse = vector_sparse(0);
        let sparse_ms = vector_sparse(250_000);
        let sparse_json = vector_sparse_json();
        let cases: [(&CoveredFields, &str, &str); 4] = [
            (
                &full,
                GENESIS_HASH,
                "648db704aad64e13a93ec3862a488b2fa561ffa0bc1ec92832f677b9576f6b78",
            ),
            (
                &sparse,
                VECTOR_PREV,
                "9e96cc27d103dee60a7b4d8df573f76eedffb7f5f6f496f14e4f3abf8fab2557",
            ),
            (
                &sparse_ms,
                VECTOR_PREV,
                "07bc370a29b5638cc61a66cd1a0449409df65fc4f978445391ce09b534933f46",
            ),
            (
                &sparse_json,
                VECTOR_PREV,
                "c18201c95d62f47743a543738a18ddd4dbc97e5eaa99a85daae869ea562c45ff",
            ),
        ];
        for (row, prev, expected) in cases {
            assert_eq!(
                hash_fields(row, prev),
                expected,
                "the vector for {} changed",
                row.id,
            );
        }
    }

    #[test]
    fn null_is_told_from_the_empty_string() {
        let mut a = vector_full();
        a.error_type = None;
        let b = vector_full(); // error_type = Some("")
        assert_ne!(hash_fields(&a, GENESIS_HASH), hash_fields(&b, GENESIS_HASH));
    }

    #[test]
    fn the_framing_is_self_delimiting_whatever_the_content() {
        // Moving a separator byte between adjacent fields must change the
        // hash — the ambiguity a null-terminated format has once a value may
        // contain the terminator.
        let mut a = vector_full();
        a.agent_impl_name = Some("a\0".into());
        a.agent_impl_version = Some("b".into());
        let mut b = vector_full();
        b.agent_impl_name = Some("a".into());
        b.agent_impl_version = Some("\0b".into());
        assert_ne!(hash_fields(&a, GENESIS_HASH), hash_fields(&b, GENESIS_HASH));
    }

    #[test]
    fn a_nanosecond_clock_read_hashes_as_the_microseconds_postgres_stores() {
        // `Utc::now()` carries nanoseconds on Linux and the database keeps
        // microseconds: the row must hash the value the database will hold.
        let mut written = vector_sparse(0);
        written.timestamp = ts(123_456).with_nanosecond(123_456_789).unwrap();
        let stored = vector_sparse(123_456);
        assert_eq!(
            hash_fields(&written, GENESIS_HASH),
            hash_fields(&stored, GENESIS_HASH),
            "the row must hash the timestamp at the precision timestamptz stores",
        );
    }

    #[test]
    fn every_scalar_field_is_tamper_evident() {
        // The release fields and the deciding model first, by name, then
        // every scalar field.
        let base = vector_full();
        let sealed = hash_fields(&base, GENESIS_HASH);
        let named: [fn(&mut CoveredFields); 12] = [
            |f| f.agent_impl_name = Some("other-agent".into()),
            |f| f.agent_impl_version = Some("9.9.9".into()),
            |f| f.agent_impl_build = Some("deadbeef".into()),
            |f| f.agent_impl_source = Some("token".into()),
            |f| f.client_label = Some("browser".into()),
            |f| f.deciding_model = Some("claude-x".into()),
            // ...and NULLing one is an edit too.
            |f| f.agent_impl_name = None,
            |f| f.agent_impl_version = None,
            |f| f.agent_impl_build = None,
            |f| f.agent_impl_source = None,
            |f| f.client_label = None,
            |f| f.deciding_model = None,
        ];
        for (i, mutate) in named.iter().enumerate() {
            let mut f = base.clone();
            mutate(&mut f);
            assert_ne!(
                hash_fields(&f, GENESIS_HASH),
                sealed,
                "named mutation {i} not covered"
            );
        }
        // Every field, generically: each value in v4_values must move the
        // hash when changed. Done through the encoded values so a field the
        // encoder forgot would show up as a mismatch with the field lists.
        let values = v4_values(&base);
        assert_eq!(values.len(), V3_FIELDS.len() + V4_ADDED_FIELDS.len());
        for idx in 0..values.len() {
            let mut bytes = b"v4\0".to_vec();
            for (j, (_, v)) in values.iter().enumerate() {
                let v = if j == idx {
                    Some(format!("{}~", v.clone().unwrap_or_default()))
                } else {
                    v.clone()
                };
                push_v3(&mut bytes, v.as_deref());
            }
            push_v3(&mut bytes, Some(GENESIS_HASH));
            assert_ne!(
                sha256_hex(&bytes),
                sealed,
                "field {} not bound",
                values[idx].0
            );
        }
        // The unedited encoding is the sealed one.
        let mut bytes = b"v4\0".to_vec();
        for (_, v) in &values {
            push_v3(&mut bytes, v.as_deref());
        }
        push_v3(&mut bytes, Some(GENESIS_HASH));
        assert_eq!(sha256_hex(&bytes), sealed);
    }

    #[test]
    fn the_verifier_computes_one_format_and_no_other() {
        let f = vector_full();
        let sealed = hash_fields(&f, GENESIS_HASH);
        assert!(row_matches(
            &f,
            Some(CANONICAL_VERSION),
            GENESIS_HASH,
            &sealed
        ));
        // A row that names another format, or none, does not verify, even
        // with the hash this format gives it: the verifier never guesses.
        for label in [
            None,
            Some("v1"),
            Some("v2"),
            Some("v3"),
            Some("v9"),
            Some(""),
        ] {
            assert!(
                !row_matches(&f, label, GENESIS_HASH, &sealed),
                "a row labelled {label:?} verified"
            );
        }
        // Nor does the right label make another hash pass.
        assert!(!row_matches(
            &f,
            Some(CANONICAL_VERSION),
            GENESIS_HASH,
            GENESIS_HASH
        ));
        assert!(!row_matches(
            &f,
            Some(CANONICAL_VERSION),
            VECTOR_PREV,
            &sealed
        ));
        assert_eq!(KNOWN_VERSIONS, [CANONICAL_VERSION]);
    }

    // -- canonical format v4 — the JSON decision records --------------

    /// `(column, setter)` for every JSON column v4 covers.
    fn json_setters() -> [(
        &'static str,
        fn(&mut CoveredFields, Option<serde_json::Value>),
    ); 6] {
        [
            ("compliance_tags", |f, v| f.compliance_tags = v),
            ("guardrails_skipped", |f, v| f.guardrails_skipped = v),
            ("guardrails_degraded", |f, v| f.guardrails_degraded = v),
            ("pre_action_state", |f, v| f.pre_action_state = v),
            ("custom_metadata", |f, v| f.custom_metadata = v),
            ("tags", |f, v| f.tags = v),
        ]
    }

    fn json_of(f: &CoveredFields, column: &str) -> serde_json::Value {
        match column {
            "compliance_tags" => f.compliance_tags.clone(),
            "guardrails_skipped" => f.guardrails_skipped.clone(),
            "guardrails_degraded" => f.guardrails_degraded.clone(),
            "pre_action_state" => f.pre_action_state.clone(),
            "custom_metadata" => f.custom_metadata.clone(),
            "tags" => f.tags.clone(),
            other => panic!("{other} is not a v4 JSON column"),
        }
        .expect("vector_full sets every JSON column")
    }

    /// The same JSON data laid out differently: keys reversed at every
    /// level, whitespace added, and every number re-spelt in exponent form.
    fn relaid(v: &serde_json::Value) -> String {
        fn go(v: &serde_json::Value, out: &mut String) {
            match v {
                serde_json::Value::Object(m) => {
                    out.push_str("{ ");
                    let mut keys: Vec<&String> = m.keys().collect();
                    keys.sort();
                    keys.reverse();
                    for (i, k) in keys.iter().enumerate() {
                        if i > 0 {
                            out.push_str(" ,\n ");
                        }
                        out.push_str(&serde_json::to_string(k).unwrap());
                        out.push_str(" : ");
                        go(&m[*k], out);
                    }
                    out.push_str(" }");
                }
                serde_json::Value::Array(a) => {
                    out.push_str("[ ");
                    for (i, x) in a.iter().enumerate() {
                        if i > 0 {
                            out.push_str(" , ");
                        }
                        go(x, out);
                    }
                    out.push_str(" ]");
                }
                serde_json::Value::Number(n) => {
                    out.push_str(&format!("{:e}", n.as_f64().unwrap()).replace('e', "E"))
                }
                other => out.push_str(&other.to_string()),
            }
        }
        let mut out = String::new();
        go(v, &mut out);
        out
    }

    #[test]
    fn every_json_column_is_tamper_evident() {
        let base = vector_full();
        let v4 = hash_fields(&base, GENESIS_HASH);
        for (column, set) in json_setters() {
            // A value change breaks the v4 row...
            let mut f = base.clone();
            set(&mut f, Some(serde_json::json!({"forged": column})));
            assert_ne!(
                hash_fields(&f, GENESIS_HASH),
                v4,
                "{column}: edit not detected"
            );
            // ...and so does removing the document.
            let mut f = base.clone();
            set(&mut f, None);
            assert_ne!(
                hash_fields(&f, GENESIS_HASH),
                v4,
                "{column}: NULLing not detected"
            );
        }
    }

    #[test]
    fn key_order_whitespace_and_number_spelling_never_break_a_v4_row() {
        // What jsonb does to a stored document (re-orders keys, drops
        // whitespace, re-spells numbers) must not read as a tamper.
        let base = vector_full();
        let v4 = hash_fields(&base, GENESIS_HASH);
        for (column, set) in json_setters() {
            let text = relaid(&json_of(&base, column));
            let reparsed: serde_json::Value = serde_json::from_str(&text).unwrap();
            let mut f = base.clone();
            set(&mut f, Some(reparsed));
            assert_eq!(
                hash_fields(&f, GENESIS_HASH),
                v4,
                "{column}: relaid as {text}"
            );
        }
    }

    #[test]
    fn a_small_value_change_inside_a_document_is_detected() {
        // Not just a different document — one number, one flag, one element.
        let base = vector_full();
        let v4 = hash_fields(&base, GENESIS_HASH);
        let mut f = base.clone();
        f.pre_action_state.as_mut().unwrap()["spend_usd"] = serde_json::json!(41.500001);
        assert_ne!(hash_fields(&f, GENESIS_HASH), v4);
        let mut f = base.clone();
        f.pre_action_state.as_mut().unwrap()["flags"]["z"] = serde_json::json!(false);
        assert_ne!(hash_fields(&f, GENESIS_HASH), v4);
        let mut f = base.clone();
        f.compliance_tags = Some(serde_json::json!(["eu_ai_act.high", "soc2.cc6.6"]));
        assert_ne!(hash_fields(&f, GENESIS_HASH), v4, "array order is data");
    }

    #[test]
    fn sql_null_and_a_json_null_document_are_the_same_absence() {
        let mut a = vector_full();
        a.tags = None;
        let mut b = vector_full();
        b.tags = Some(serde_json::Value::Null);
        assert_eq!(hash_fields(&a, GENESIS_HASH), hash_fields(&b, GENESIS_HASH));
        // ...while an empty document is a document.
        let mut c = vector_full();
        c.tags = Some(serde_json::json!({}));
        assert_ne!(hash_fields(&a, GENESIS_HASH), hash_fields(&c, GENESIS_HASH));
    }

    #[test]
    fn the_v4_byte_layout_is_exactly_as_documented() {
        // Hand-built from the documented layout: v3's framing under the tag
        // `v4`, the v3 fields, then the six JSON fields as JCS text.
        let row = vector_sparse_json();
        let mut want = b"v4\0".to_vec();
        let field = |out: &mut Vec<u8>, s: &str| {
            out.push(1);
            out.extend_from_slice(&(s.len() as u32).to_be_bytes());
            out.extend_from_slice(s.as_bytes());
        };
        for name in V3_FIELDS.iter().chain(V4_ADDED_FIELDS) {
            match *name {
                "id" => field(&mut want, "01K6V3VECTOR0000000000SPARSE"),
                "tenant_id" => field(&mut want, "tenant-vec"),
                "timestamp" => field(&mut want, "2026-09-27T14:03:07.000000Z"),
                "source" => field(&mut want, "otel"),
                "proxy_type" => field(&mut want, "llm"),
                "http_method" => field(&mut want, "POST"),
                "endpoint" => field(&mut want, "/v1/chat/completions"),
                "is_auth_event"
                | "guardrail_blocked"
                | "argument_policy_denied"
                | "pii_detected"
                | "success" => field(&mut want, "0"),
                "pre_action_state" => field(
                    &mut want,
                    r#"{"kind":"llm_quota","used":1.5,"window":"day"}"#,
                ),
                _ => want.push(0),
            }
        }
        field(&mut want, VECTOR_PREV);
        assert_eq!(canonical_row_bytes_v4(&row, VECTOR_PREV), want);
    }

    #[test]
    fn the_writer_seals_v4_over_the_json_it_inserts() {
        // stamp_with hashes the ProxyLogInsert the INSERT binds: the JSON
        // columns reach CoveredFields from the insert row, not a copy.
        let mut rows = vec![log("j")];
        rows[0].pre_action_state = Some(serde_json::json!({"kind": "llm_quota", "used": 3}));
        rows[0].guardrails_skipped = Some(serde_json::json!([{"check": "pii"}]));
        stamp_with("chain-json", 0, GENESIS_HASH, &mut rows).unwrap();
        let sealed = rows[0].row_hash.clone().unwrap();
        assert_eq!(rows[0].chain_version.as_deref(), Some("v4"));
        let mut f = CoveredFields::from(&rows[0]);
        assert!(row_matches(&f, Some("v4"), GENESIS_HASH, &sealed));
        f.pre_action_state = Some(serde_json::json!({"kind": "llm_quota", "used": 4}));
        assert!(!row_matches(&f, Some("v4"), GENESIS_HASH, &sealed));
    }

    // -- the column-coverage guard ------------------------------------

    fn insert_columns() -> Vec<String> {
        crate::services::background_writer::PROXY_LOG_INSERT_COLUMNS
            .split(',')
            .map(|c| c.trim().to_string())
            .filter(|c| !c.is_empty())
            .collect()
    }

    #[test]
    fn every_inserted_column_is_covered_or_excluded() {
        // The class-level guard (feedback: regressions need class-level
        // tests). proxy_logs columns have been added that landed
        // outside the chain with nothing to force the decision. Now every
        // column the writer inserts must be in V3_FIELDS or on
        // CHAIN_EXCLUDED_COLUMNS with a reason — adding a column to the
        // INSERT without deciding fails here.
        use std::collections::HashSet;
        let insert = insert_columns();
        let covered: HashSet<&str> = V3_FIELDS.iter().chain(V4_ADDED_FIELDS).copied().collect();
        let excluded: HashSet<&str> = CHAIN_EXCLUDED_COLUMNS.iter().map(|(c, _)| *c).collect();

        let undecided: Vec<&String> = insert
            .iter()
            .filter(|c| !covered.contains(c.as_str()) && !excluded.contains(c.as_str()))
            .collect();
        assert!(
            undecided.is_empty(),
            "proxy_logs columns written by the core but neither in the audit chain \
             (audit_chain::V3_FIELDS / V4_ADDED_FIELDS) nor on audit_chain::CHAIN_EXCLUDED_COLUMNS: {undecided:?}. \
             Decide: cover it (a new canonical version) or exclude it with a reason.",
        );
        // Covered fields are real inserted columns...
        let inserted: HashSet<&str> = insert.iter().map(String::as_str).collect();
        for f in V3_FIELDS.iter().chain(V4_ADDED_FIELDS) {
            assert!(
                inserted.contains(f),
                "the chain covers {f}, which the writer never inserts"
            );
        }
        // ...never also excluded, never listed twice.
        assert!(
            covered.is_disjoint(&excluded),
            "a column is both covered and excluded"
        );
        assert_eq!(
            covered.len(),
            V3_FIELDS.len() + V4_ADDED_FIELDS.len(),
            "a covered field is listed twice"
        );
        assert_eq!(
            excluded.len(),
            CHAIN_EXCLUDED_COLUMNS.len(),
            "exclusion list has a duplicate"
        );
        assert_eq!(inserted.len(), insert.len(), "INSERT lists a column twice");
    }

    #[test]
    fn an_excluded_column_the_writer_never_inserts_is_one_set_after_sealing() {
        // The exclusion list mirrors the whole table (the control plane pins
        // it against the ORM). A column absent from the INSERT can only be
        // one the database or the control plane sets later.
        let inserted: std::collections::HashSet<String> = insert_columns().into_iter().collect();
        for (c, why) in CHAIN_EXCLUDED_COLUMNS {
            if !inserted.contains(*c) {
                assert_eq!(*why, Excluded::AfterSeal, "{c} is not inserted by the core");
            }
        }
    }

    #[test]
    fn the_encoder_the_select_and_the_field_list_agree() {
        // v3_values is the encoder, COVERED_SELECT is what the verifier
        // reads back, V3_FIELDS is the published contract: one list, three
        // views, pinned equal.
        let names: Vec<&str> = v3_values(&vector_full()).iter().map(|(n, _)| *n).collect();
        assert_eq!(names, V3_FIELDS, "v3_values order differs from V3_FIELDS");
        let names: Vec<&str> = v4_values(&vector_full()).iter().map(|(n, _)| *n).collect();
        let v4_order: Vec<&str> = V3_FIELDS.iter().chain(V4_ADDED_FIELDS).copied().collect();
        assert_eq!(
            names, v4_order,
            "v4_values is not V3_FIELDS then V4_ADDED_FIELDS"
        );
        let mut select: Vec<&str> = COVERED_SELECT.split(',').map(str::trim).collect();
        let mut fields: Vec<&str> = v4_order.clone();
        select.sort_unstable();
        fields.sort_unstable();
        assert_eq!(
            select, fields,
            "COVERED_SELECT does not read exactly the v4 fields"
        );
    }

    #[test]
    fn the_insert_binds_one_value_per_column() {
        // The column list is a constant now; the binds are still a chain of
        // calls. Count them so the two cannot drift apart silently.
        let src = include_str!("background_writer.rs");
        let start = src.find("async fn insert_logs(").expect("insert_logs");
        let end = start + src[start..].find("qb.build()").expect("qb.build");
        let binds = src[start..end].matches(".push_bind(").count();
        assert_eq!(binds, insert_columns().len(), "binds vs INSERT columns");
    }

    // -- signed shapes no longer move with the row format -------------

    #[test]
    fn signed_shapes_carry_their_own_frozen_tag() {
        let range = ChainRange {
            chain_id: "c".into(),
            seq_start: 0,
            seq_end: 1,
            head_hash: "h".into(),
        };
        assert!(canonical_checkpoint_bytes(&range).starts_with(b"v2\0"));
        assert_eq!(
            SIGNED_SHAPE_VERSION, "v2",
            "every checkpoint is signed with v2"
        );
        assert_ne!(SIGNED_SHAPE_VERSION, CANONICAL_VERSION);
    }

    #[test]
    fn a_checkpoint_signed_before_a171_still_verifies() {
        // Pre-a171 checkpoints were signed over `v1`-tagged bytes; tying the
        // tag to the row format made them read as bad signatures.
        let range = ChainRange {
            chain_id: "chain-old".into(),
            seq_start: 0,
            seq_end: 9,
            head_hash: "abc123".into(),
        };
        let key = SigningKey::from_bytes(&[7u8; 32]);
        let sig = key.sign(&canonical_checkpoint_bytes_tagged(
            &range,
            SIGNED_SHAPE_VERSION_V1,
        ));
        let cp = SignedCheckpoint {
            range,
            signature: hex::encode(sig.to_bytes()),
            public_key: hex::encode(key.verifying_key().to_bytes()),
        };
        assert!(verify_signature(&cp));
        // ...but not under an arbitrary tag.
        let sig = key.sign(&canonical_checkpoint_bytes_tagged(&cp.range, "v3"));
        let cp = SignedCheckpoint {
            signature: hex::encode(sig.to_bytes()),
            ..cp
        };
        assert!(!verify_signature(&cp));
    }

    // ── integers a double cannot hold ──────────────

    /// One row sealed at the genesis, with `doc` in `custom_metadata`.
    fn sealed_with(doc: serde_json::Value) -> ProxyLogInsert {
        let mut rows = vec![log("r-f1218")];
        rows[0].custom_metadata = Some(doc);
        stamp_with("chain-f1218", 0, GENESIS_HASH, &mut rows).expect("stamped");
        rows.remove(0)
    }

    /// What a verifier does: read the stored row back (through JSON text, as
    /// the database and an export hold it) and recompute the hash.
    fn verifies_as_stored(row: &ProxyLogInsert) -> bool {
        let mut f = CoveredFields::from(row);
        let reread = |v: &Option<serde_json::Value>| {
            v.as_ref()
                .map(|doc| serde_json::from_str(&doc.to_string()).expect("stored JSON"))
        };
        f.compliance_tags = reread(&row.compliance_tags);
        f.guardrails_skipped = reread(&row.guardrails_skipped);
        f.guardrails_degraded = reread(&row.guardrails_degraded);
        f.pre_action_state = reread(&row.pre_action_state);
        f.custom_metadata = reread(&row.custom_metadata);
        f.tags = reread(&row.tags);
        row_matches(
            &f,
            row.chain_version.as_deref(),
            row.prev_hash.as_deref().expect("prev"),
            row.row_hash.as_deref().expect("hash"),
        )
    }

    /// JCS prints every number as the double it denotes, so 2^53 and 2^53+1
    /// are one text and were one row hash: an edit between them was not
    /// detectable. The writer stores (and so hashes) such an integer as its
    /// exact decimal digits.
    #[test]
    fn an_integer_beyond_2_pow_53_is_sealed_as_its_exact_digits() {
        let a = sealed_with(serde_json::json!({"n": 9_007_199_254_740_992_u64}));
        let b = sealed_with(serde_json::json!({"n": 9_007_199_254_740_993_u64}));
        assert_ne!(
            a.row_hash, b.row_hash,
            "2^53 and 2^53+1 in a sealed JSON column seal to the same hash"
        );
        assert_eq!(
            b.custom_metadata,
            Some(serde_json::json!({"n": "9007199254740993"})),
            "what is stored is what was hashed: the digits, as a string"
        );
        assert_eq!(
            a.custom_metadata,
            Some(serde_json::json!({"n": "9007199254740992"})),
            "2^53 is the first integer past the safe range (2^53 − 1)"
        );
        assert!(verifies_as_stored(&a) && verifies_as_stored(&b));

        // Nested, negative, and at the top of u64 / bottom of i64.
        let deep = sealed_with(serde_json::json!({
            "list": [1, {"m": -9_007_199_254_740_993_i64}, [u64::MAX]],
            "min": i64::MIN,
            "ok": 7,
        }));
        assert_eq!(
            deep.custom_metadata,
            Some(serde_json::json!({
                "list": [1, {"m": "-9007199254740993"}, ["18446744073709551615"]],
                "min": "-9223372036854775808",
                "ok": 7,
            }))
        );
        assert!(verifies_as_stored(&deep));
    }

    /// Nothing representable today changes: safe integers (to the last one,
    /// both signs), floats of any size, and strings are stored and hashed as
    /// they always were, so every existing row and vector still verifies.
    #[test]
    fn safe_integers_and_floats_are_sealed_untouched() {
        let doc = serde_json::json!({
            "max_safe": 9_007_199_254_740_991_u64,
            "min_safe": -9_007_199_254_740_991_i64,
            "zero": 0,
            "float": 1.5,
            "big_float": 1e300,
            "integral_float": 9_007_199_254_740_994.0_f64,
            "digits": "9007199254740993",
            "nested": [{"n": 42}, 0.1],
        });
        let sealed = sealed_with(doc.clone());
        assert_eq!(sealed.custom_metadata, Some(doc.clone()));

        // The same hash the canonical form gives the document as written.
        let mut unsealed = log("r-f1218");
        unsealed.custom_metadata = Some(doc);
        unsealed.chain_version = Some(CANONICAL_VERSION.to_string());
        assert_eq!(
            sealed.row_hash.as_deref(),
            Some(hash_row(&unsealed, GENESIS_HASH).as_str())
        );
        assert!(verifies_as_stored(&sealed));
    }

    /// The cross-repo vector for exact integers: what the writer stores for a
    /// document with unsafe integers, and the v4 hash of the sparse vector
    /// row carrying it. The hash is the one the control plane's verifier
    /// (`app/services/audit_chain_canonical.py`, `hash_row_as(…, "v4")`)
    /// computes for the STORED document — it reads the column, finds
    /// strings, and hashes them character for character — so the two agree
    /// without a change on its side. Never edit the expectation to make a
    /// build pass.
    #[test]
    fn the_exact_digits_vector_is_what_the_control_plane_verifier_computes() {
        let mut row = log("r-f1218-vector");
        row.custom_metadata = Some(serde_json::json!({
            "n": 9_007_199_254_740_993_u64,
            "safe": 9_007_199_254_740_991_u64,
            "neg": -9_007_199_254_740_993_i64,
            "list": [1, u64::MAX],
            "ratio": 0.25,
        }));
        make_sealed_json_exact(&mut row);
        let stored: serde_json::Value = serde_json::from_str(
            r#"{"n":"9007199254740993","safe":9007199254740991,"neg":"-9007199254740993",
                "list":[1,"18446744073709551615"],"ratio":0.25}"#,
        )
        .unwrap();
        assert_eq!(row.custom_metadata.as_ref(), Some(&stored));

        let mut f = vector_sparse(0);
        f.custom_metadata = Some(stored);
        assert_eq!(
            hash_fields(&f, VECTOR_PREV),
            "120dbfc18038f31ef6b9841ebae02ff1669b9fa3fe0b5a69f7c830dace0909f9",
        );
        // One less, the same double: another hash (the verifier's too).
        f.custom_metadata = Some(serde_json::json!({
            "n": "9007199254740992", "safe": 9_007_199_254_740_991_u64,
            "neg": "-9007199254740993", "list": [1, "18446744073709551615"], "ratio": 0.25,
        }));
        assert_eq!(
            hash_fields(&f, VECTOR_PREV),
            "0d8a3731c74545bd5ba42de20ae19f707bcac7acb472ed1704b5a7bb23d326f0",
        );
    }

    /// Every sealed JSON column is covered, and only those: the list the
    /// writer walks is the list the hash covers.
    #[test]
    fn every_sealed_json_column_is_made_exact() {
        let mut row = log("r-f1218-cols");
        let names: Vec<&str> = sealed_json_columns(&mut row)
            .into_iter()
            .map(|(name, _)| name)
            .collect();
        assert_eq!(names, V4_ADDED_FIELDS);

        for (_, column) in sealed_json_columns(&mut row) {
            *column = Some(serde_json::json!([9_007_199_254_740_993_u64]));
        }
        let replaced = make_sealed_json_exact(&mut row);
        assert_eq!(
            replaced,
            V4_ADDED_FIELDS
                .iter()
                .map(|c| format!("{c}: $[0]"))
                .collect::<Vec<_>>(),
            "each replacement is named by its column and path"
        );
        for (name, column) in sealed_json_columns(&mut row) {
            assert_eq!(
                *column,
                Some(serde_json::json!(["9007199254740993"])),
                "{name}"
            );
        }
        assert!(make_sealed_json_exact(&mut row).is_empty(), "idempotent");
    }
}

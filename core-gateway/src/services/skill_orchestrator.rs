// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Agent Skill Orchestrator
//!
//! Transport-agnostic service for skill resolution, authorization, and execution.
//! Port of Python `app/services/agent_skill/agent_skill_orchestrator.py`.
//!
//! Both REST and MCP paths converge here:
//!
//! ```text
//! REST  →  POST /proxy/skills/execute   →  execute_skill()
//! MCP   →  tools/call (skill__*)        →  execute_skill()
//! ```
//!
//! ## Execution pipeline (`execute_skill`)
//!
//! ```text
//! 1. resolve_skill()            — Find skill by ID or name, verify executability
//! 2. resolve_group()            — Explicit group_id or auto-resolve from auth context
//! 3. check_skill_group_access() — Verify skill is assigned to group via
//!                                 resource_group_agent_skills association table
//! 4. check_permissions(),       — Group execution mode + API key restrictions, and
//!    execution_mode_gate()        the mode of the skill and of its assignments:
//!                                 `disabled` refuses
//! 5. run controls, data scope, argument policy on the input, agent grant
//! 6. one approval hold          — when the grant, an argument-policy band or the
//!                                 execution mode asks for a person: the call is
//!                                 held in the approval queue and nothing below
//!                                 happens until it is retried with the token
//! 7. enforce_skill_quota()      — skill_global_limits on the ResourceGroup
//! 8. insert_running_execution() — INSERT into agent_skill_executions (RUNNING)
//! ```
//!
//! ## Additional Operations
//!
//! - `get_catalog()` — Group-scoped skill catalog (Progressive Disclosure Level 1,
//!   ~50-100 tokens per entry) with category/tag filters
//! - `get_execution_status()` — Read an execution's status
//! - `cancel_execution()` — Cancel a running execution
//!
//! ## Design
//!
//! All methods return `Result<T, SkillError>`. Callers map these to HTTP status
//! codes (REST route) or MCP error content blocks (system server handler).
//! The `SkillExecutionResult` includes `warning_headers` for quota utilization
//! alerts that get attached to the HTTP response.

use chrono::Utc;
use sqlx::PgPool;

use crate::models::auth::AuthContext;
use crate::models::db::{AgentSkill, AgentSkillExecution, ResourceGroup};
use crate::models::proxy::SkillExecuteRequest;
use crate::services::usage;

// =============================================================================
// Domain Errors
// =============================================================================

#[derive(Debug)]
pub struct SkillError {
    pub status_code: u16,
    pub message: String,
    /// When set, the route renders this JSON body verbatim instead of the
    /// canonical error envelope. Used to surface the agent-approval envelope
    /// (428/202 + poll_url) through the normal `SkillError` return path.
    pub body: Option<serde_json::Value>,
    /// The refusal subtype the audit row carries when this
    /// error is a governance verdict (a gate said no) rather than a failure,
    /// so the refused execution is sealed `denied` / `awaiting_human` /
    /// `engine_failure` instead of reading as a failed run.
    pub refusal: Option<&'static str>,
    /// An argument policy on the skill's input refused this execution
    /// (`execute_skill`). The caller's audit row is flagged as an
    /// argument-policy denial (`proxy_logger::apply_policy_deny`) — the flag
    /// every other surface's argument-policy refusal carries — instead of
    /// being given a subtype of its own.
    pub argument_policy_denied: bool,
}

impl SkillError {
    /// Tag this error as a governance refusal (see [`SkillError::refusal`]).
    fn refused(mut self, subtype: &'static str) -> Self {
        self.refusal = Some(subtype);
        self
    }

    fn not_found(msg: impl Into<String>) -> Self {
        Self {
            status_code: 404,
            message: msg.into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
    }
    fn forbidden(msg: impl Into<String>) -> Self {
        Self {
            status_code: 403,
            message: msg.into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
    }
    fn bad_request(msg: impl Into<String>) -> Self {
        Self {
            status_code: 400,
            message: msg.into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
    }
    fn conflict(msg: impl Into<String>) -> Self {
        Self {
            status_code: 409,
            message: msg.into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
    }
    /// The group's skill governance cannot be read: refuse (503), never
    /// read it as "no governance" (fail closed).
    fn governance_unavailable(e: &sqlx::Error) -> Self {
        tracing::error!(error = %e, "skill governance unreadable; refusing (fail closed)");
        Self {
            status_code: 503,
            message: "Refused: this group's skill governance cannot be read right now".into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
        .refused(crate::constants::proxy_subtype::ACCESS_DENIED)
    }
    fn internal(msg: impl Into<String>) -> Self {
        Self {
            status_code: 500,
            message: msg.into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
    }
    /// Carries the agent-approval envelope (status 428 or 202) + JSON body.
    fn approval(status_code: u16, body: serde_json::Value) -> Self {
        Self {
            status_code,
            message: "approval_required".to_string(),
            body: Some(body),
            refusal: Some(crate::constants::proxy_subtype::APPROVAL_REQUIRED),
            argument_policy_denied: false,
        }
    }
    /// An argument policy on the skill's input said `deny`. The body is the
    /// refusal the REST route has always answered with; the message is the
    /// audit row's error text, the same on every surface.
    fn argument_policy_denied(v: &crate::services::argument_policy::evaluator::Violation) -> Self {
        Self {
            status_code: 403,
            message: format!(
                "Argument policy ({}/{}): {}",
                v.policy_name, v.rule, v.message
            ),
            body: Some(serde_json::json!({
                "error": {
                    "code": "argument_policy_violation",
                    "message": v.message,
                    "policy": v.policy_name,
                    "skill": v.tool,
                    "argument": v.argument_key,
                    "analyzer": v.analyzer,
                    "rule": v.rule,
                    "facts": v.facts,
                }
            })),
            refusal: None,
            argument_policy_denied: true,
        }
    }
    /// The caller's group could not be looked up: refuse (503). A lookup
    /// that failed is not "this caller has no group".
    fn group_unavailable(e: &sqlx::Error) -> Self {
        tracing::error!(error = %e, "target group lookup failed; refusing (fail closed)");
        Self {
            status_code: 503,
            message: "Refused: the caller's Resource Group cannot be read right now".into(),
            body: None,
            refusal: None,
            argument_policy_denied: false,
        }
        .refused(crate::constants::proxy_subtype::ENGINE_FAILURE)
    }
}

// =============================================================================
// Result Types
// =============================================================================

pub struct SkillExecutionResult {
    pub execution_id: String,
    pub skill_id: String,
    pub skill_name: String,
    pub skill_version: String,
    pub execution_status: String,
    pub correlation_id: String,
    pub group_id: String,
    pub group_name: String,
    /// Usage warning headers to attach to the HTTP response.
    pub warning_headers: Vec<(String, String)>,
    /// Pre-approved tools for this skill (from skill.allowed_tools).
    /// The calling agent should restrict tool use to this list during execution.
    pub allowed_tools: Option<Vec<String>>,
    /// Recommended model for this skill execution (from skill_metadata.required_llm_models).
    pub recommended_model: Option<String>,
    /// Expanded skill prompt (SKILL.md content with resolved references).
    /// The calling agent should inject this into the LLM conversation context.
    pub skill_prompt: Option<String>,
    /// Description of the skill for context.
    pub skill_description: Option<String>,
    /// Maximum tool calls allowed per execution (from group governance).
    /// The calling agent should enforce this limit during execution.
    pub max_tool_calls_per_execution: Option<i64>,
}

impl SkillExecutionResult {
    /// What happened to the execution, in a sentence, from its status.
    pub fn message(&self) -> String {
        let what = match self.execution_status.as_str() {
            "COMPLETED" => "completed",
            "FAILED" => "failed",
            _ => "execution started",
        };
        format!(
            "Skill '{}' v{} {what}.",
            self.skill_name, self.skill_version
        )
    }
}

pub struct ExecutionStatusResult {
    pub execution_id: String,
    pub skill_id: String,
    pub skill_name: String,
    pub execution_status: String,
    pub correlation_id: Option<String>,
    pub message: String,
}

pub struct CatalogResult {
    pub skills: Vec<serde_json::Value>,
    pub total: usize,
    pub group_id: Option<String>,
    pub group_name: Option<String>,
}

// =============================================================================
// Skill Resolution
// =============================================================================

/// Resolve a skill by ID or name within a tenant.
pub async fn resolve_skill(
    db: &PgPool,
    tenant_id: &str,
    skill_id: Option<&str>,
    skill_name: Option<&str>,
) -> Result<AgentSkill, SkillError> {
    if skill_id.is_none() && skill_name.is_none() {
        return Err(SkillError::bad_request(
            "Must provide either skill_id or skill_name",
        ));
    }

    let skill = if let Some(sid) = skill_id {
        sqlx::query_as::<_, AgentSkill>(
            r#"SELECT id, tenant_id, name, description, license, compatibility,
                      allowed_tools, version, status::text AS status, enabled,
                      metadata, skill_md_content, execution_count, last_executed_at,
                      published_at, created_at, source_repo_id
               FROM agent_skills WHERE id = $1 AND tenant_id = $2"#,
        )
        .bind(sid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| SkillError::internal(format!("DB error resolving skill: {e}")))?
        .ok_or_else(|| SkillError::not_found(format!("Skill with id '{sid}' not found")))?
    } else {
        let sname = skill_name.unwrap();
        sqlx::query_as::<_, AgentSkill>(
            r#"SELECT id, tenant_id, name, description, license, compatibility,
                      allowed_tools, version, status::text AS status, enabled,
                      metadata, skill_md_content, execution_count, last_executed_at,
                      published_at, created_at, source_repo_id
               FROM agent_skills
               WHERE name = $1 AND tenant_id = $2
                 AND status = 'PUBLISHED'::skillstatus AND enabled = true
               ORDER BY created_at DESC LIMIT 1"#,
        )
        .bind(sname)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| SkillError::internal(format!("DB error resolving skill: {e}")))?
        .ok_or_else(|| {
            SkillError::not_found(format!("No published skill found with name '{sname}'"))
        })?
    };

    // Verify executability
    if !skill.enabled {
        return Err(
            SkillError::forbidden(format!("Skill '{}' is disabled", skill.name))
                .refused(crate::constants::proxy_subtype::ACCESS_DENIED),
        );
    }
    if skill.status != "PUBLISHED" && skill.status != "DEPRECATED" {
        return Err(SkillError::forbidden(format!(
            "Skill '{}' is in '{}' status and cannot be executed. Only published (or deprecated) skills can be executed.",
            skill.name, skill.status
        ))
            .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
    }

    Ok(skill)
}

// =============================================================================
// Group Resolution
// =============================================================================

/// Resolve target group: explicit group_id or auto-resolve from auth context.
/// Re-uses the same logic as `usage::resolve_target_group` but returns a SkillError.
pub async fn resolve_group(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    explicit_group_id: Option<&str>,
) -> Result<ResourceGroup, SkillError> {
    let mut group = if let Some(gid) = explicit_group_id {
        let g = sqlx::query_as::<_, ResourceGroup>(
            r#"SELECT *, group_type::text AS group_type
               FROM resource_groups WHERE id = $1 AND tenant_id = $2 AND is_active = true"#,
        )
        .bind(gid)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| SkillError::internal(format!("DB error resolving group: {e}")))?
        .ok_or_else(|| SkillError::not_found(format!("Resource Group '{gid}' not found")))?;
        // SECURITY: a client-supplied group_id must be one the caller is actually
        // entitled to — otherwise a caller bound to group A could execute a skill
        // billed to (and governed by) group B just by naming its id. Mirror the
        // entitlement edges of usage::resolve_target_group.
        authorize_explicit_group(db, auth, tenant_id, gid).await?;
        g
    } else {
        // Auto-resolve from auth context (same priority as usage::resolve_target_group)
        // A lookup that FAILED refuses as
        // unavailable; only a caller that really has no group is told so.
        crate::services::usage::resolve_target_group_checked(db, auth, tenant_id)
            .await
            .map_err(|e| SkillError::group_unavailable(&e))?
            .ok_or_else(|| {
                SkillError::forbidden(
                    "Cannot determine target Resource Group. Provide group_id or authenticate with a group-scoped API key.",
                )
            .refused(crate::constants::proxy_subtype::ACCESS_DENIED)
            })?
    };

    // Bake in the ANCESTOR-MERGED skill governance so a parent group's
    // restrictions (execution_mode, max_concurrent_executions,
    // max_tool_calls_per_execution) inherit to children — the downstream
    // checks all read `group.skill_global_governance`, which previously
    // reflected only this single group (no inheritance).
    group.skill_global_governance = collect_effective_skill_governance(db, &group).await?;
    Ok(group)
}

/// Authorize a *client-supplied* `group_id` against the caller's identity.
///
/// The auto-resolve path (usage::resolve_target_group) already only ever
/// returns a group the caller is entitled to; this closes the equivalent hole
/// on the explicit path. Entitlement edges (matching resolve_target_group):
///   - API-key-bound caller → may only target its bound group.
///   - Gateway/portal user  → must be a member of the group, directly
///     (resource_group_memberships) or via an end-user group
///     (end_user_group_members ∘ resource_group_end_user_groups).
///   - No identity → cannot target an explicit group.
async fn authorize_explicit_group(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    group_id: &str,
) -> Result<(), SkillError> {
    // A caller bound to one group: an API key, or a workload-identity agent
    // (the group it acts in).
    if let Some(key_gid) = auth.bound_group_id() {
        if key_gid == group_id {
            return Ok(());
        }
        return Err(SkillError::forbidden(
            "API key is not authorized for the requested Resource Group",
        )
        .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
    }
    if let Some(ref uid) = auth.gateway_user_id {
        let member: Option<i32> = sqlx::query_scalar(
            r#"SELECT 1
               FROM (
                   SELECT group_id AS gid
                   FROM resource_group_memberships
                   WHERE user_id = $1::int AND tenant_id = $3
                   UNION
                   SELECT rgeug.resource_group_id AS gid
                   FROM resource_group_end_user_groups rgeug
                   JOIN end_user_group_members eugm
                     ON eugm.end_user_group_id = rgeug.end_user_group_id
                   WHERE eugm.end_user_id = $1::int
               ) m
               WHERE m.gid = $2
               LIMIT 1"#,
        )
        .bind(uid)
        .bind(group_id)
        .bind(tenant_id)
        .fetch_optional(db)
        .await
        .map_err(|e| SkillError::internal(format!("DB error checking group membership: {e}")))?;
        if member.is_some() {
            return Ok(());
        }
        return Err(
            SkillError::forbidden("Not a member of the requested Resource Group")
                .refused(crate::constants::proxy_subtype::ACCESS_DENIED),
        );
    }
    Err(
        SkillError::forbidden("Authentication required to target a specific Resource Group")
            .refused(crate::constants::proxy_subtype::ACCESS_DENIED),
    )
}

/// Every field the control plane's `validate_skill_governance` accepts, and
/// therefore every field this merge must restrict. A field in the schema
/// but missing from the merge is stored, displayed, and then ignored.
///
/// This list and the Python `SKILL_GOVERNANCE_FIELDS` had drifted apart:
/// Python merged `discovery_enabled` and `approval_timeout_seconds` while
/// this side merged the two execution ceilings, so the console and the
/// proxy disagreed about four of the five fields. The control plane's
/// `tests/test_python_rust_merge_parity.py` reads both source files and
/// fails if they diverge again.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) const SKILL_GOVERNANCE_FIELDS: [&str; 5] = [
    "execution_mode",
    "discovery_enabled",
    "approval_timeout_seconds",
    "max_concurrent_executions",
    "max_tool_calls_per_execution",
];

/// Strictness ordering for `execution_mode` when a parent's governance is
/// merged into a child's. The three known modes rank as in the Python
/// `_SKILL_EXECUTION_STRICTNESS` map (the Control Plane's parity test reads
/// both). A mode this build does not know ranks above them all, so the merge
/// keeps it wherever it was stated, and the gate refuses the call on it
/// ([`ModeGate::Unknown`]).
fn skill_execution_strictness(mode: Option<&str>) -> u8 {
    match mode {
        Some("disabled") => 2,
        Some("approval_required") => 1,
        Some("unrestricted") | None => 0,
        Some(_) => 3,
    }
}

/// [`skill_execution_strictness`] of an `execution_mode` as stated: absent
/// and null are unset; a value that is not text is no known mode.
fn stated_execution_strictness(mode: Option<&serde_json::Value>) -> u8 {
    match mode {
        None | Some(serde_json::Value::Null) => 0,
        Some(serde_json::Value::String(text)) => skill_execution_strictness(Some(text)),
        Some(_) => 3,
    }
}

/// Merge a parent `skill_global_governance` JSON into the accumulated child
/// view, MOST-RESTRICTIVE wins:
/// - `execution_mode`: strictest of {disabled > approval_required > unrestricted}.
/// - `discovery_enabled`: false wins (AND) — a parent that turned discovery
///   off cannot be re-enabled by a child.
/// - `approval_timeout_seconds`, `max_concurrent_executions`,
///   `max_tool_calls_per_execution`: the smallest stated value. The first
///   two are read only when positive, so a `0`, a negative or a non-number
///   is no value at its level and the other level's stays;
///   `max_tool_calls_per_execution` is handed to the agent as stated, so
///   any number is stated.
///
/// A child whose governance is not an object (a JSON `null`) states nothing:
/// it inherits the parent's.
fn merge_skill_governance(base: &mut serde_json::Value, parent: &serde_json::Value) {
    let Some(p) = parent.as_object() else {
        return;
    };
    let Some(b) = base.as_object_mut() else {
        *base = parent.clone();
        return;
    };

    let p_mode = p.get("execution_mode");
    let b_mode = b.get("execution_mode");
    // Take the parent's mode when it is stricter, and when the child never
    // stated one at all — an unset child inherits rather than defaulting to
    // the most permissive value. A mode the gateway does not know is the
    // strictest, so neither level's is lost and the gate refuses on it.
    let take_parent = b_mode.is_none_or(serde_json::Value::is_null)
        || stated_execution_strictness(p_mode) > stated_execution_strictness(b_mode);
    if take_parent {
        if let Some(m) = p.get("execution_mode") {
            b.insert("execution_mode".into(), m.clone());
        }
    }

    // Discovery is a gate, not a ceiling: false is the restrictive value,
    // so the merged view is the AND of every level that expressed one.
    if let Some(pv) = p.get("discovery_enabled").and_then(|v| v.as_bool()) {
        let merged = match b.get("discovery_enabled").and_then(|v| v.as_bool()) {
            Some(bv) => bv && pv,
            None => pv,
        };
        b.insert("discovery_enabled".into(), serde_json::json!(merged));
    }

    for (key, non_positive_is_stated) in [
        ("approval_timeout_seconds", false),
        ("max_concurrent_executions", false),
        ("max_tool_calls_per_execution", true),
    ] {
        let stated = |v: Option<&serde_json::Value>| {
            v.and_then(|v| v.as_i64())
                .filter(|n| non_positive_is_stated || *n > 0)
        };
        if let Some(pv) = stated(p.get(key)) {
            let merged = match stated(b.get(key)) {
                Some(bv) => bv.min(pv),
                None => pv,
            };
            b.insert(key.into(), serde_json::json!(merged));
        }
    }
}

/// Effective `skill_global_governance` = this group merged with every
/// ancestor (most-restrictive). Mirrors LLM/A2A governance inheritance.
/// `Err` when a hop is unreadable: it used to stop the walk there, dropping
/// every ancestor's restriction (fail closed).
async fn collect_effective_skill_governance(
    db: &PgPool,
    group: &ResourceGroup,
) -> Result<Option<serde_json::Value>, SkillError> {
    let mut effective = group.skill_global_governance.clone();
    let mut current_parent_id = group.parent_group_id.clone();
    for _ in 0..10 {
        let pid = match current_parent_id {
            Some(ref id) => id.clone(),
            None => break,
        };
        let row: Option<(Option<serde_json::Value>, Option<String>)> = sqlx::query_as(
            "SELECT skill_global_governance, parent_group_id FROM resource_groups WHERE id = $1",
        )
        .bind(&pid)
        .fetch_optional(db)
        .await
        .map_err(|e| SkillError::governance_unavailable(&e))?;
        match row {
            Some((gov, next_pid)) => {
                if let Some(parent_gov) = gov {
                    match effective {
                        Some(ref mut base) => merge_skill_governance(base, &parent_gov),
                        None => effective = Some(parent_gov),
                    }
                }
                current_parent_id = next_pid;
            }
            None => break,
        }
    }
    Ok(effective)
}

// =============================================================================
// Access & Permission Checks
// =============================================================================

/// Verify the skill is assigned to the group via resource_group_agent_skills.
/// Walks up the group hierarchy when `inherit_resources = true`, so a child
/// group inherits skill assignments from its ancestors.
///
/// Resolution order:
///   1. Direct assignment: skill ↔ group in resource_group_agent_skills
///   2. Inherited: skill assigned to any ancestor group, provided every group
///      in the chain from the child up to that ancestor has inherit_resources = true
pub async fn check_skill_group_access(
    db: &PgPool,
    skill_id: &str,
    group_id: &str,
) -> Result<(), SkillError> {
    // Check direct assignment first (fast path)
    let found: Option<(String,)> = sqlx::query_as(
        "SELECT id FROM resource_group_agent_skills WHERE agent_skill_id = $1 AND resource_group_id = $2",
    )
    .bind(skill_id)
    .bind(group_id)
    .fetch_optional(db)
    .await
    .map_err(|e| SkillError::internal(format!("DB error checking skill access: {e}")))?;

    if found.is_some() {
        return Ok(());
    }

    // Walk up the hierarchy: check if any ancestor group has the skill assigned.
    // We use the hierarchy_path to find ancestors and verify inherit_resources
    // is true for the requesting group.
    let inherited: Option<(String,)> = sqlx::query_as(
        r#"SELECT rgas.id
           FROM resource_group_agent_skills rgas
           JOIN resource_groups ancestor ON ancestor.id = rgas.resource_group_id
           JOIN resource_groups child ON child.id = $2
           WHERE rgas.agent_skill_id = $1
             AND child.inherit_from_parent = true
             AND child.hierarchy_path LIKE ancestor.hierarchy_path || '/%'"#,
    )
    .bind(skill_id)
    .bind(group_id)
    .fetch_optional(db)
    .await
    .map_err(|e| SkillError::internal(format!("DB error checking inherited skill access: {e}")))?;

    if inherited.is_none() {
        return Err(
            SkillError::forbidden("Skill is not assigned to the target group")
                .refused(crate::constants::proxy_subtype::ACCESS_DENIED),
        );
    }
    Ok(())
}

/// Check group-level execution mode and API key skill restrictions.
/// Note: concurrent execution limit is enforced atomically in `execute_skill()`
/// using `pg_advisory_xact_lock` to prevent TOCTOU races.
pub async fn check_permissions(
    _db: &PgPool,
    skill: &AgentSkill,
    group: &ResourceGroup,
    auth: &AuthContext,
) -> Result<(), SkillError> {
    // Group-level execution mode (from skill_global_governance JSONB)
    if let Some(ref gov) = group.skill_global_governance {
        if let Some(mode) = gov.get("execution_mode").and_then(|v| v.as_str()) {
            if mode == "disabled" {
                return Err(SkillError::forbidden(format!(
                    "Skill execution is disabled for group '{}'",
                    group.name
                ))
                .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
            }
        }
    }

    // NOTE: max concurrent execution check has been moved into execute_skill()
    // to be performed atomically (within a transaction + advisory lock) alongside
    // the execution INSERT, preventing TOCTOU race conditions in multi-pod deployments.

    // API key skill restrictions (allowed_skill_ids from AuthContext)
    if let Some(ref allowed_ids) = auth.allowed_skill_ids {
        if !allowed_ids.contains(&skill.id) {
            return Err(SkillError::forbidden(format!(
                "API key is not authorized to execute skill '{}'. Allowed skills: {:?}",
                skill.name, allowed_ids
            ))
            .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
        }
    }

    Ok(())
}

/// What the skill's execution mode says about one call. The mode has three
/// sources and the strictest wins; none relaxes another:
///
/// * the group's skill governance (`skill_global_governance.execution_mode`,
///   already merged with every ancestor's);
/// * the skill's own default (`approval_required: true` in its metadata);
/// * the governance on the skill's assignment to the group, and to every
///   ancestor group the group inherits the skill from
///   ([`fetch_assignment_governance`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModeGate {
    /// A source says `disabled`: the call is refused.
    Disabled,
    /// A source states a mode that is none of `unrestricted`,
    /// `approval_required` and `disabled`: the call is refused (fail
    /// closed). The value may have been meant as `disabled`.
    Unknown(String),
    /// A source asks for a person: the call is held in the approval queue.
    ApprovalRequired {
        /// The window the mode states for the hold: the nearest assignment's
        /// `approval_timeout_seconds`, else the group's. `None` when neither
        /// states one; the hold takes the group / tenant / platform default.
        window: Option<i32>,
        /// Which sources asked: `group`, `skill`, `assignment`. On the
        /// approval request, for the person who answers it.
        required_by: Vec<&'static str>,
    },
    Unrestricted,
}

/// The governance on one assignment of a skill to a group
/// (`resource_group_agent_skills.governance`): the object the Admin API
/// shows as the pair's own, apart from the group-wide
/// `resource_groups.skill_global_governance`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AssignmentGovernance {
    pub execution_mode: Option<String>,
    /// `approval_timeout_seconds`, when positive.
    pub approval_timeout_seconds: Option<i32>,
}

/// The assignments of `skill_id` that apply to `group_id`, nearest first:
/// the group's own, then those of the ancestor groups it inherits the skill
/// from (the rows [`check_skill_group_access`] accepts). `Err` when they
/// cannot be read: a mode that cannot be read may be `disabled`, so the
/// execution is refused (fail closed).
pub async fn fetch_assignment_governance(
    db: &PgPool,
    group_id: &str,
    skill_id: &str,
) -> Result<Vec<AssignmentGovernance>, SkillError> {
    let rows: Vec<(Option<String>, Option<serde_json::Value>)> = sqlx::query_as(
        r#"SELECT rgas.governance->>'execution_mode',
                  rgas.governance->'approval_timeout_seconds'
           FROM resource_group_agent_skills rgas
           JOIN resource_groups holder ON holder.id = rgas.resource_group_id
           JOIN resource_groups child ON child.id = $1
           WHERE rgas.agent_skill_id = $2
             AND (holder.id = child.id
                  OR (child.inherit_from_parent = true
                      AND child.hierarchy_path LIKE holder.hierarchy_path || '/%'))
           ORDER BY (holder.id = child.id) DESC,
                    length(holder.hierarchy_path) DESC NULLS LAST,
                    holder.id"#,
    )
    .bind(group_id)
    .bind(skill_id)
    .fetch_all(db)
    .await
    .map_err(|e| SkillError::governance_unavailable(&e))?;
    Ok(rows
        .into_iter()
        .map(|(execution_mode, window)| AssignmentGovernance {
            execution_mode,
            approval_timeout_seconds: window
                .and_then(|v| v.as_i64())
                .filter(|s| *s > 0)
                .map(|s| i32::try_from(s).unwrap_or(i32::MAX)),
        })
        .collect())
}

/// The execution modes there are. A stated mode outside this list refuses
/// the call ([`ModeGate::Unknown`]).
const EXECUTION_MODES: [&str; 3] = ["unrestricted", "approval_required", "disabled"];

/// The verdict of the three sources, given what was read
/// ([`execution_mode_gate`] reads it).
fn mode_gate(
    skill: &AgentSkill,
    group: &ResourceGroup,
    assignments: &[AssignmentGovernance],
) -> ModeGate {
    // The group's mode as stated: text, or (for a value that is not text)
    // its JSON, which is no known mode. Absent and null are unset.
    let group_stated = group
        .skill_global_governance
        .as_ref()
        .and_then(|g| g.get("execution_mode"))
        .filter(|v| !v.is_null())
        .map(|v| match v.as_str() {
            Some(text) => text.to_string(),
            None => v.to_string(),
        });
    let group_mode = group_stated.as_deref();
    let assignment_says = |mode: &str| {
        assignments
            .iter()
            .any(|a| a.execution_mode.as_deref() == Some(mode))
    };
    if group_mode == Some("disabled") || assignment_says("disabled") {
        return ModeGate::Disabled;
    }
    if let Some(unknown) = group_mode
        .into_iter()
        .chain(
            assignments
                .iter()
                .filter_map(|a| a.execution_mode.as_deref()),
        )
        .find(|mode| !EXECUTION_MODES.contains(mode))
    {
        return ModeGate::Unknown(unknown.to_string());
    }
    let mut required_by = Vec::new();
    if group_mode == Some("approval_required") {
        required_by.push("group");
    }
    if skill.is_approval_required() {
        required_by.push("skill");
    }
    if assignment_says("approval_required") {
        required_by.push("assignment");
    }
    if required_by.is_empty() {
        return ModeGate::Unrestricted;
    }
    ModeGate::ApprovalRequired {
        window: assignments
            .iter()
            .find_map(|a| a.approval_timeout_seconds)
            .or_else(|| execution_mode_policy_window(group)),
        required_by,
    }
}

/// What the execution mode says about running `skill` in `group`: the one
/// reading both [`execute_skill`] and its preview ([`SkillGates`]) make.
pub async fn execution_mode_gate(
    db: &PgPool,
    skill: &AgentSkill,
    group: &ResourceGroup,
) -> Result<ModeGate, SkillError> {
    let assignments = fetch_assignment_governance(db, &group.id, &skill.id).await?;
    Ok(mode_gate(skill, group, &assignments))
}

// =============================================================================
// Execution Record CRUD
// =============================================================================

/// The window the group's execution-mode policy states for a hold: the
/// (already chain-merged) group's
/// `skill_global_governance.approval_timeout_seconds`, if positive.
pub fn execution_mode_policy_window(group: &ResourceGroup) -> Option<i32> {
    group
        .skill_global_governance
        .as_ref()
        .and_then(|g| g.get("approval_timeout_seconds"))
        .and_then(|v| v.as_i64())
        .filter(|s| *s > 0)
        .map(|s| i32::try_from(s).unwrap_or(i32::MAX))
}

/// How long an execution may stay RUNNING before [`reap_abandoned_skill_executions`]
/// settles it FAILED (`abandoned`). Every execution runs synchronously inside
/// its request, bounded by the runner client's 180 s timeout, so a row RUNNING
/// five times that long has no request left to settle it (a crashed pod, or a
/// caller that dispatched without settling).
pub const ABANDONED_EXECUTION_AFTER_SECS: i64 = 900;

/// Settle executions RUNNING longer than `older_than_secs` as FAILED with
/// `error_type = 'abandoned'`. RUNNING rows count against the group's
/// `max_concurrent_executions`, so a leaked row would otherwise hold its slot
/// forever (a safety net; the rule is that every dispatch settles).
/// Returns how many executions were reaped.
pub async fn reap_abandoned_skill_executions(
    db: &PgPool,
    older_than_secs: i64,
) -> Result<u64, sqlx::Error> {
    let reaped = sqlx::query(
        r#"WITH abandoned AS (
            SELECT id FROM agent_skill_executions
            WHERE execution_status = 'RUNNING'::executionstatus
              AND COALESCE(running_at, started_at) < NOW() - make_interval(secs => $1)
            FOR UPDATE SKIP LOCKED
        )
        UPDATE agent_skill_executions e
        SET execution_status = 'FAILED'::executionstatus,
            completed_at = NOW(),
            duration_ms = (EXTRACT(EPOCH FROM (NOW() - COALESCE(e.running_at, e.started_at))) * 1000)::int,
            error_type = 'abandoned',
            error_message = 'Execution never settled; reaped after exceeding the skill runner timeout'
        WHERE e.id IN (SELECT id FROM abandoned)"#,
    )
    .bind(older_than_secs as f64)
    .execute(db)
    .await?;
    Ok(reaped.rows_affected())
}

/// Record a new execution, RUNNING, and count it on the skill. Returns the
/// execution id. Every gate has passed when this is called: an execution
/// row is a run that started.
async fn insert_running_execution(
    conn: &mut sqlx::PgConnection,
    skill: &AgentSkill,
    group: &ResourceGroup,
    auth: &AuthContext,
    tenant_id: &str,
    correlation_id: &str,
    request: &SkillExecuteRequest,
) -> Result<String, SkillError> {
    let execution_id = ulid::Ulid::new().to_string();
    let now = Utc::now();
    let req_correlation_id = request.correlation_id.as_deref().unwrap_or(correlation_id);
    let username = auth
        .gateway_username
        .as_deref()
        .or(auth.idp_username.as_deref())
        .unwrap_or("unknown");

    let execution_context = serde_json::json!({
        "input_params": request.input_params,
        "resolved_mcp_servers": skill.get_metadata_value("required_mcp_servers"),
        "resolved_llm_models": skill.get_metadata_value("required_llm_models"),
        "auth_type": auth.auth_type.to_string(),
        "client_ip": auth.client_ip,
    });

    sqlx::query(
        r#"INSERT INTO agent_skill_executions (
            id, tenant_id, skill_id, group_id, user_id,
            skill_name, skill_version, group_name, username,
            request_id, correlation_id, parent_execution_id,
            execution_status, approval_status,
            started_at, running_at, execution_context, created_at
        ) VALUES (
            $1, $2, $3, $4, $5,
            $6, $7, $8, $9,
            $10, $11, $12,
            'RUNNING'::executionstatus, 'NOT_REQUIRED'::approvalstatus,
            $13, $13, $14, $13
        )"#,
    )
    .bind(&execution_id)
    .bind(tenant_id)
    .bind(&skill.id)
    .bind(&group.id)
    .bind(end_user_of(auth))
    .bind(&skill.name)
    .bind(&skill.version)
    .bind(&group.name)
    .bind(username)
    .bind(correlation_id)
    .bind(req_correlation_id)
    .bind(&request.parent_execution_id)
    .bind(now)
    .bind(&execution_context)
    .execute(&mut *conn)
    .await
    .map_err(|e| SkillError::internal(format!("Failed to create execution record: {e}")))?;

    sqlx::query(
        "UPDATE agent_skills SET execution_count = execution_count + 1, last_executed_at = $1 WHERE id = $2",
    )
    .bind(now)
    .bind(&skill.id)
    .execute(&mut *conn)
    .await
    .map_err(|e| SkillError::internal(format!("Failed to update skill counter: {e}")))?;

    Ok(execution_id)
}

/// Transition execution from RUNNING to COMPLETED.
pub async fn complete_execution(
    db: &PgPool,
    execution_id: &str,
    tenant_id: &str,
) -> Result<(), SkillError> {
    let now = Utc::now();

    // Calculate duration_ms from running_at
    let row = sqlx::query_scalar::<_, Option<chrono::DateTime<Utc>>>(
        "SELECT running_at FROM agent_skill_executions WHERE id = $1 AND tenant_id = $2",
    )
    .bind(execution_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    .map_err(|e| SkillError::internal(format!("DB error: {e}")))?;

    let duration_ms = row
        .flatten()
        .map(|running_at| (now - running_at).num_milliseconds() as i32);

    sqlx::query(
        r#"UPDATE agent_skill_executions
           SET execution_status = 'COMPLETED'::executionstatus,
               completed_at = $1,
               duration_ms = $2
           WHERE id = $3 AND tenant_id = $4"#,
    )
    .bind(now)
    .bind(duration_ms)
    .bind(execution_id)
    .bind(tenant_id)
    .execute(db)
    .await
    .map_err(|e| SkillError::internal(format!("Failed to complete execution: {e}")))?;

    tracing::debug!(execution_id, duration_ms, "Skill execution completed");
    Ok(())
}

/// Transition execution from RUNNING to FAILED.
pub async fn fail_execution(
    db: &PgPool,
    execution_id: &str,
    tenant_id: &str,
    error_type: &str,
    error_message: &str,
) -> Result<(), SkillError> {
    let now = Utc::now();

    let row = sqlx::query_scalar::<_, Option<chrono::DateTime<Utc>>>(
        "SELECT running_at FROM agent_skill_executions WHERE id = $1 AND tenant_id = $2",
    )
    .bind(execution_id)
    .bind(tenant_id)
    .fetch_optional(db)
    .await
    .map_err(|e| SkillError::internal(format!("DB error: {e}")))?;

    let duration_ms = row
        .flatten()
        .map(|running_at| (now - running_at).num_milliseconds() as i32);

    sqlx::query(
        r#"UPDATE agent_skill_executions
           SET execution_status = 'FAILED'::executionstatus,
               completed_at = $1,
               duration_ms = $2,
               error_type = $3,
               error_message = $4
           WHERE id = $5 AND tenant_id = $6"#,
    )
    .bind(now)
    .bind(duration_ms)
    .bind(error_type)
    .bind(error_message)
    .bind(execution_id)
    .bind(tenant_id)
    .execute(db)
    .await
    .map_err(|e| SkillError::internal(format!("Failed to fail execution: {e}")))?;

    tracing::info!(
        execution_id,
        error_type,
        duration_ms,
        "Skill execution failed"
    );
    Ok(())
}

// =============================================================================
// Full Execute Pipeline
// =============================================================================

/// Argument policy on the skill's input (`skill_input`), for EVERY caller.
///
/// This check lived in the callers. The REST route
/// ran it on a group it looked up itself (and skipped it when that lookup
/// failed), `skills__run_script` ran it and acted on `deny` only,
/// and the A2A paths did not run it at all, so a skill could be
/// driven past its input policy by choosing the surface. Here it runs on the
/// group the execution is authorised against, and no caller can skip it.
///
/// `Ok(Some(_))` is a band that asks for a person (`approval_required`);
/// the caller holds the execution through the shared approval lifecycle.
/// `deny` is the error. `warn` is logged.
async fn check_input_policy(
    state: &crate::services::SharedState,
    group: &ResourceGroup,
    skill: &AgentSkill,
    input: Option<&serde_json::Value>,
    view: crate::models::proxy::SkillInputView,
) -> Result<Option<crate::services::argument_policy::evaluator::Violation>, SkillError> {
    use crate::models::proxy::SkillInputView;
    use crate::services::argument_policy::Arguments;
    use crate::services::argument_policy::policy::{Severity, Surface};
    // No input is an input with no fields: a policy that requires one
    // (`field_present`, `schema.required`) still decides, and a policy on a
    // named key is skipped as for any call that omits the key.
    let empty = serde_json::Value::Object(Default::default());
    let input = input.unwrap_or(&empty);
    // The same field has the same address on every surface: an input that
    // arrived as an A2A message is judged on its data parts' values.
    let arguments = match view {
        SkillInputView::Whole => Arguments::Whole(input),
        SkillInputView::A2aMessage => Arguments::of_a2a_message(input),
    };
    let Some(v) = crate::services::argument_policy::enforce_view(
        &state.argument_policy_cache,
        &state.db,
        &group.tenant_id,
        &group.id,
        Surface::SkillInput,
        &skill.name,
        &arguments,
    )
    .await
    else {
        return Ok(None);
    };
    match v.severity {
        Severity::Warn => {
            tracing::warn!(
                policy = %v.policy_name,
                skill = %v.tool,
                rule = %v.rule,
                "Skill argument policy warn (logged, not blocked)"
            );
            Ok(None)
        }
        // The band asks for a human rather than refusing.
        // Merged with the agent-grant plane below, so one call produces one
        // queue entry.
        Severity::ApprovalRequired => {
            tracing::info!(
                policy = %v.policy_name,
                skill = %v.tool,
                rule = %v.rule,
                field = ?v.field,
                observed = ?v.observed,
                threshold = ?v.threshold,
                "Skill argument policy requires approval"
            );
            Ok(Some(v))
        }
        Severity::Deny => {
            tracing::warn!(
                policy = %v.policy_name,
                skill = %v.tool,
                rule = %v.rule,
                "Skill argument policy deny — refused"
            );
            crate::services::proxy_metrics::record_guardrail_block("skill", "argument_policy");
            Err(SkillError::argument_policy_denied(&v))
        }
    }
}

/// What an approval of this execution covers: the call a person is asked
/// about, and the call a redeemed token must be.
///
/// For the caller's own input object that is the input. An A2A Message is
/// a new Message on every attempt (its `messageId`, and on a retry the
/// `taskId` of the task that waits), so what is bound is what the Message
/// asks for: all of its parts and its metadata. Another amount, another
/// instruction in a text part or another capability hint is another call.
fn approval_arguments(
    request: &SkillExecuteRequest,
) -> Option<std::borrow::Cow<'_, serde_json::Value>> {
    approval_view(request.input_params.as_ref()?, request.input_view)
}

/// [`approval_arguments`] of one input.
fn approval_view(
    input: &serde_json::Value,
    view: crate::models::proxy::SkillInputView,
) -> Option<std::borrow::Cow<'_, serde_json::Value>> {
    use crate::models::proxy::SkillInputView;
    use crate::services::argument_policy::Arguments;
    use std::borrow::Cow;
    match view {
        SkillInputView::A2aMessage
            if matches!(Arguments::of_a2a_message(input), Arguments::Parts(_)) =>
        {
            Some(Cow::Owned(serde_json::json!({
                "parts": input.get("parts"),
                "metadata": input.get("metadata"),
            })))
        }
        _ => Some(Cow::Borrowed(input)),
    }
}

/// What an approval of this execution is bound to, when the input passed an
/// input guardrail on its way here (`SkillExecuteRequest::input_as_sent`):
/// the caller's own input, in the view [`approval_arguments`] takes of it.
///
/// The approver is shown, and the skill is given, the input as the guardrail
/// left it, and a guardrail may leave the same input differently on the
/// retry (one that asks a model or a detector service). The token is for
/// the call the caller made: it runs that call and no other, whatever the
/// guardrail removes from it this time. `None` when no guardrail stands
/// between the caller and the skill: the arguments are the binding.
fn approval_binding(
    state: &crate::services::SharedState,
    request: &SkillExecuteRequest,
) -> Option<String> {
    let sent = request.input_as_sent.as_ref()?;
    Some(crate::services::agent_approval::content_binding(
        &state.config.jwt_secret,
        approval_view(sent, request.input_view).as_deref(),
    ))
}

/// The skill gates of a request that has not reached the skill yet: what
/// [`execute_skill`] would do with `request`, so the gate of the surface in
/// front of it (an A2A task, `skills__run_script`) can raise the skill's
/// hold with its own and take both tokens on the retry
/// (`agent_approval::RequestApprovals`), and refuses a request the skill
/// would refuse before anything is held.
///
/// The same reading [`execute_skill`] makes, by the same functions
/// ([`judge_execution`], [`person_gate`]): the refusal it would give, or the
/// hold it would raise. It admits nothing and raises nothing. The execution
/// behind another surface is part of that surface's action
/// (`StepKind::PartOfAction`), so the run's controls are read and no unit is
/// taken.
pub struct SkillGates<'a> {
    pub state: &'a crate::services::SharedState,
    pub auth: &'a AuthContext,
    pub request: &'a SkillExecuteRequest,
    pub correlation_id: &'a str,
    /// The run the execution would belong to (what the surface passes to
    /// [`execute_skill`]).
    pub run: &'a crate::services::run_controls::RunScope,
}

impl crate::services::agent_approval::Downstream for SkillGates<'_> {
    fn holds(
        &self,
    ) -> futures::future::BoxFuture<
        '_,
        Result<
            Vec<crate::services::agent_approval::DownstreamHold>,
            crate::services::agent_approval::DownstreamRefusal,
        >,
    > {
        Box::pin(async move {
            match skill_hold(self).await {
                Ok(hold) => Ok(hold.into_iter().collect()),
                Err(e) => Err(crate::services::agent_approval::DownstreamRefusal {
                    status: axum::http::StatusCode::from_u16(e.status_code)
                        .unwrap_or(axum::http::StatusCode::FORBIDDEN),
                    message: e.message,
                    body: e.body,
                    subtype: e.refusal,
                    argument_policy_denied: e.argument_policy_denied,
                }),
            }
        })
    }
}

async fn skill_hold(
    gates: &SkillGates<'_>,
) -> Result<Option<crate::services::agent_approval::DownstreamHold>, SkillError> {
    let SkillGates {
        state,
        auth,
        request,
        correlation_id,
        run,
    } = *gates;
    let judged = judge_execution(
        state,
        auth,
        request,
        correlation_id,
        run,
        crate::services::run_controls::StepKind::PartOfAction,
        crate::services::data_scope::Record::ARefusalOnly,
    )
    .await?;
    let Some(gate) = person_gate(judged.grant_holds, judged.band.as_ref(), &judged.mode) else {
        return Ok(None);
    };
    Ok(Some(crate::services::agent_approval::DownstreamHold {
        surface: SKILL_SURFACE,
        scope_ref: judged.skill.id.clone(),
        capability_type: SKILL_ACTION.to_string(),
        capability_name: judged.skill.id.clone(),
        groups: vec![judged.group],
        policy_window_seconds: gate.window,
        call_arguments: approval_arguments(request).map(std::borrow::Cow::into_owned),
        binding: approval_binding(state, request),
        extra_metadata: gate.metadata,
    }))
}

/// What the agent's `skill_exec` grants say about running `skill`.
enum GrantGate {
    /// Not an agent, a grant that allows, or an agent that is observed only
    /// (dry run): the grant plane does not stop the call.
    Clear,
    /// The grant asks for a person.
    Hold,
    Deny(String),
    /// The grants cannot be read: refused (fail closed).
    Unavailable,
}

async fn grant_gate(
    db: &PgPool,
    tenant_id: &str,
    auth: &AuthContext,
    skill: &AgentSkill,
) -> GrantGate {
    use crate::services::agent_authz::{Decision, decide_for_agent};
    let Some(agent_id) = auth.actor_agent_id.as_deref() else {
        return GrantGate::Clear;
    };
    match decide_for_agent(
        db,
        tenant_id,
        agent_id,
        SKILL_ACTION,
        &skill.id,
        None,
        None,
        None,
        &[],
    )
    .await
    {
        // Observed only: the would-block is logged and the call proceeds.
        Ok(o) if o.dry_run && o.decision != Decision::Allow => {
            tracing::warn!(
                agent = %agent_id, skill = %skill.id, decision = %o.decision.as_str(),
                reason = %o.reason, "skill agent authz: dry-run would-block (proceeding)",
            );
            GrantGate::Clear
        }
        Ok(o) if o.decision == Decision::Deny => GrantGate::Deny(o.reason),
        Ok(o) if o.decision == Decision::ApprovalRequired => GrantGate::Hold,
        Ok(_) => GrantGate::Clear,
        Err(e) => {
            tracing::error!(error = %e, agent = %agent_id, "skill agent authz failed; failing closed");
            GrantGate::Unavailable
        }
    }
}

/// The one hold a skill execution can be given, and what it carries.
struct PersonGate {
    /// The window the policy that asked states. Only the execution mode
    /// states one on this surface.
    window: Option<i32>,
    /// For the person who answers: the band's detail and which sources of
    /// the execution mode asked.
    metadata: Option<serde_json::Value>,
}

/// Whether a person is asked about this execution, and with what: `Some`
/// when the agent's grant, an argument-policy band on the input or the
/// execution mode asks. They ask about the same call, so they share one
/// approval request. [`execute_skill`] raises it and [`SkillGates`] previews
/// it, both from here, so the two cannot state different holds.
fn person_gate(
    grant_holds: bool,
    band: Option<&crate::services::argument_policy::evaluator::Violation>,
    mode: &ModeGate,
) -> Option<PersonGate> {
    let (window, required_by) = match mode {
        ModeGate::ApprovalRequired {
            window,
            required_by,
        } => (*window, Some(required_by)),
        ModeGate::Disabled | ModeGate::Unknown(_) | ModeGate::Unrestricted => (None, None),
    };
    if !grant_holds && band.is_none() && required_by.is_none() {
        return None;
    }
    let mut metadata = band.map(|v| v.approval_metadata());
    if let Some(required_by) = required_by {
        let mut detail = match metadata.take() {
            Some(serde_json::Value::Object(m)) => m,
            _ => serde_json::Map::new(),
        };
        detail.insert(
            "execution_mode".to_string(),
            serde_json::json!({ "required_by": required_by }),
        );
        metadata = Some(serde_json::Value::Object(detail));
    }
    Some(PersonGate { window, metadata })
}

/// The surface and the action a skill execution is on an approval: one
/// definition for the gates in [`execute_skill`] and for their preview
/// ([`SkillGates`]), so the two state the same question.
const SKILL_SURFACE: &str = "skill";
const SKILL_ACTION: &str = "skill_exec";

/// What the gates that can refuse a skill execution found, for a call none
/// of them refused: everything [`execute_skill`] and its preview
/// ([`SkillGates`]) need to decide whether a person is asked.
struct Judged {
    skill: AgentSkill,
    group: ResourceGroup,
    mode: ModeGate,
    /// An argument-policy band on the input that asks for a person.
    band: Option<crate::services::argument_policy::evaluator::Violation>,
    /// The agent's grant asks for a person.
    grant_holds: bool,
    /// The run's action unit, when the execution is an action of its own.
    step_admitted: crate::services::run_controls::StepAdmitted,
}

/// Every gate that can REFUSE a skill execution, in order: the skill and
/// group resolve, access, permissions, the execution mode (`disabled`), the
/// run's controls, its data scope, the argument policy on the input, the
/// agent's grant, the AI System's autonomy. `Err` is the refusal.
///
/// One function for the execution and for its preview, so a front gate
/// never holds a call the execution would refuse: what [`execute_skill`]
/// refuses, [`SkillGates`] refuses, with the same answer.
///
/// `scope_record` says what the data-scope gate writes: the execution
/// records every access; the preview records a refused one only (the
/// request ends there), and leaves a passing one for the execution.
async fn judge_execution(
    state: &crate::services::SharedState,
    auth: &AuthContext,
    request: &SkillExecuteRequest,
    correlation_id: &str,
    run: &crate::services::run_controls::RunScope,
    step: crate::services::run_controls::StepKind,
    scope_record: crate::services::data_scope::Record,
) -> Result<Judged, SkillError> {
    let db = &state.db;
    let tenant_id = &auth.tenant_id;

    // 0. The tenant's "Agent skills" switch (`tenant_features`): off refuses
    //    every execution, whichever surface asks.
    match crate::services::tenant_features::require(
        state,
        tenant_id,
        crate::services::tenant_features::Feature::AgentSkills,
    )
    .await
    {
        Ok(()) => {}
        Err(Some(msg)) => {
            return Err(
                SkillError::forbidden(msg).refused(crate::constants::proxy_subtype::ACCESS_DENIED)
            );
        }
        Err(None) => {
            return Err(SkillError {
                status_code: 503,
                message: "Refused: the tenant's features cannot be read right now".into(),
                body: None,
                refusal: None,
                argument_policy_denied: false,
            }
            .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
        }
    }

    // 1. Resolve skill
    let skill = resolve_skill(
        db,
        tenant_id,
        request.skill_id.as_deref(),
        request.skill_name.as_deref(),
    )
    .await?;

    // 2. Resolve group
    let group = resolve_group(db, auth, tenant_id, request.group_id.as_deref()).await?;

    // 3. Check access — the skill must be bound to the resolved
    //    resource group (directly or inherited via hierarchy_path).
    //    Resource Groups are the platform's single access-control
    //    primitive for every AI resource; skills are no exception.
    check_skill_group_access(db, &skill.id, &group.id).await?;

    // 4. Check permissions (execution mode + API key restrictions)
    check_permissions(db, &skill, &group, auth).await?;

    // 4a. The execution mode: the group's, the skill's own default, and the
    //     governance on the skill's assignment to this group and to the
    //     ancestors it is inherited from. `disabled` refuses here; a mode
    //     that asks for a person holds the call below, with the other gates
    //     that do.
    let mode = execution_mode_gate(db, &skill, &group).await?;
    if mode == ModeGate::Disabled {
        return Err(SkillError::forbidden(format!(
            "Skill execution is disabled for group '{}'",
            group.name
        ))
        .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
    }
    if let ModeGate::Unknown(value) = &mode {
        tracing::error!(
            tenant_id = %tenant_id, group = %group.id, skill = %skill.id, mode = %value,
            "skill execution mode is not one this gateway knows; refusing (fail closed)"
        );
        return Err(SkillError::forbidden(format!(
            "Skill execution is refused for group '{}': its execution mode '{}' is not a \
             mode this gateway knows (unrestricted, approval_required, disabled)",
            group.name, value
        ))
        .refused(crate::constants::proxy_subtype::ACCESS_DENIED));
    }

    // 4b. Per-agent runtime authorization ("actions, not access"). When the
    //     executor is an agent principal, decide whether it may run this skill
    //     from its grants (alongside the resource-group access above; either
    //     plane can block). Fails closed on a lookup error.
    // Run-scoped controls — the run budget and an operator's
    // abort — for EVERY caller, before either authz plane so a stopped
    // run cannot even queue an approval. The run is the caller's call-chain
    // root, the same value its audit rows carry. Skills had no run budget
    // at all before this; see `run_controls::check`.
    //
    // An execution that is an action of its own is admitted against the
    // run's action ceiling here and held: kept once the execution exists,
    // given back if a later step refuses it (as its quota hold is).
    let step_admitted = match crate::services::run_controls::check(
        state,
        crate::services::run_controls::Step {
            tenant_id,
            group_id: Some(group.id.as_str()),
            run_id: &run.run_id,
            depth: run.depth,
            kind: step,
            starter: run.starter.as_deref(),
        },
    )
    .await
    {
        Ok(admitted) => admitted,
        Err(refusal) => {
            let message = match &refusal {
                crate::services::run_controls::RunRefusal::Aborted(reason) => {
                    format!("run_aborted: {reason}")
                }
                other => other.message().to_string(),
            };
            return Err(SkillError {
                status_code: refusal.status().as_u16(),
                message,
                body: None,
                refusal: Some(refusal.subtype()),
                argument_policy_denied: false,
            });
        }
    };

    // Run data scope on skill input. Rule name `skill:<name>`;
    // the arguments are the input params. Every way a skill runs (REST, the
    // system server's run_script, an A2A skill handler) passes here, so one
    // call site covers them all. Before either authz plane, like the run
    // controls: a call for the wrong record must not even queue an approval.
    {
        let tool = format!(
            "{}{}",
            crate::services::data_scope::surface::SKILL,
            skill.name
        );
        let input = request
            .input_params
            .clone()
            .unwrap_or(serde_json::Value::Null);
        if let Some(r) = crate::services::data_scope::gate_recording(
            db,
            crate::services::data_scope::Call {
                tenant_id,
                ai_system_id: &group.id,
                tool: &tool,
                arguments: &[&input],
                root_task_id: &run.run_id,
                conflicts: &request.data_scope_conflicts,
                auth_ctx: auth,
                correlation_id,
            },
            scope_record,
        )
        .await
        {
            return Err(SkillError {
                status_code: 403,
                message: r.message.clone(),
                body: Some(r.body()),
                refusal: Some(r.subtype()),
                argument_policy_denied: false,
            });
        }
    }

    // Argument policy on the skill's input — like the data scope above, one
    // call site for every way a skill runs, and
    // before either authz plane: a denied input must not queue an approval.
    let band = check_input_policy(
        state,
        &group,
        &skill,
        request.input_params.as_ref(),
        request.input_view,
    )
    .await?;

    // The agent's grants ("actions, not access"), alongside the
    // resource-group access above; either plane can block.
    let grant_holds = match grant_gate(db, tenant_id, auth, &skill).await {
        GrantGate::Clear => false,
        GrantGate::Hold => true,
        GrantGate::Deny(reason) => {
            return Err(
                SkillError::forbidden(format!("agent_not_authorized: {reason}"))
                    .refused(crate::constants::proxy_subtype::AGENT_AUTHZ_BLOCK),
            );
        }
        GrantGate::Unavailable => {
            return Err(SkillError::forbidden("agent_authz_unavailable".to_string())
                .refused(crate::constants::proxy_subtype::ENGINE_FAILURE));
        }
    };
    if auth.actor_agent_id.is_none() {
        // Non-agent caller: no grant was consulted, so neither was the
        // autonomy gate the grant decision ends with. Running a skill
        // executes code, which is side-effecting by definition — a suspended
        // or read-only AI System must refuse it whoever asked.
        if let Some((crate::services::autonomy::Verdict::Deny(reason), resolution)) =
            crate::services::autonomy::enforce_for_group(
                db,
                tenant_id,
                Some(group.id.as_str()),
                SKILL_ACTION,
                &[],
            )
            .await
        {
            tracing::warn!(
                tenant_id = %tenant_id,
                group = %group.id,
                skill = %skill.id,
                level = %resolution.level.as_str(),
                provenance = %resolution.provenance.as_str(),
                "autonomy block on a non-agent caller — refusing skill execution",
            );
            return Err(SkillError::forbidden(format!("autonomy_denied: {reason}"))
                .refused(crate::constants::proxy_subtype::AUTONOMY_BLOCK));
        }
    }

    Ok(Judged {
        skill,
        group,
        mode,
        band,
        grant_holds,
        step_admitted,
    })
}

/// Full skill execution pipeline — the single entry point for REST, MCP
/// (`skills__run_script`) and A2A callers.
///
/// Authorize, gate and record one skill execution.
///
/// `run` is the run the execution belongs to — derived by the caller from its
/// call chain (REST route, MCP system server, A2A dispatch). It drives the
/// run-scoped controls and is carried into any approval hold raised here, so
/// the run ledger and the run clock see the time the run waits on a person.
/// It used to be absent: the abort check keyed on the caller's correlation
/// id (which is not the run), and holds carried no run at all.
///
/// `step` says what the execution is to its run's action ceiling: an action
/// of its own when the skill route received the request
/// (`StepKind::Action`), part of the caller's action when a system server's
/// tool or an agent-to-agent task runs it (`StepKind::PartOfAction`; that
/// surface has admitted the request).
pub async fn execute_skill(
    state: &crate::services::SharedState,
    auth: &AuthContext,
    request: &SkillExecuteRequest,
    correlation_id: &str,
    run: &crate::services::run_controls::RunScope,
    step: crate::services::run_controls::StepKind,
) -> Result<SkillExecutionResult, SkillError> {
    let db = &state.db;
    let tenant_id = &auth.tenant_id;

    // 1-5. Every gate that can refuse the execution (`judge_execution`).
    let Judged {
        skill,
        group,
        mode,
        band,
        grant_holds,
        step_admitted,
    } = judge_execution(
        state,
        auth,
        request,
        correlation_id,
        run,
        step,
        crate::services::data_scope::Record::EveryAccess,
    )
    .await?;
    let approved_call = approval_arguments(request);

    // 6. One hold for everything that asks for a person on this call: the
    //    agent's grant, an argument-policy band, the execution mode. A valid
    //    one-time approval token carried on the request is redeemed and the
    //    call proceeds; otherwise an approval request is enqueued and its
    //    envelope is the answer. The input is bound into the issued token,
    //    so an approval for `{period: Q2}` cannot execute `{period: ALL}`.
    //    A held call returns here: nothing is charged, the run's action unit
    //    goes back, and no execution exists.
    if let Some(gate) = person_gate(grant_holds, band.as_ref(), &mode) {
        let binding = approval_binding(state, request);
        let ctx = crate::services::agent_approval::ApprovalContext {
            db,
            tenant_id,
            surface: SKILL_SURFACE,
            scope_ref: &skill.id,
            capability_type: SKILL_ACTION,
            capability_name: &skill.id,
            auth_ctx: auth,
            groups: std::slice::from_ref(&group),
            policy_window_seconds: gate.window,
            call_arguments: approved_call.as_deref(),
            binding: binding.as_deref(),
            run: Some(run.clone()),
            extra_metadata: gate.metadata,
            run_clock: Some(&state.usage_state),
            approvals: &request.approvals,
            // The skill's gates are the last of any request.
            downstream: None,
        };
        match crate::services::agent_approval::require_or_enqueue(&ctx).await {
            crate::services::agent_approval::ApprovalOutcome::Proceed => {}
            crate::services::agent_approval::ApprovalOutcome::Envelope(status, body) => {
                return Err(SkillError::approval(status.as_u16(), body));
            }
            crate::services::agent_approval::ApprovalOutcome::Denied(msg) => {
                return Err(
                    SkillError::forbidden(format!("Approval token invalid: {}", msg))
                        .refused(crate::constants::proxy_subtype::APPROVAL_TOKEN_INVALID),
                );
            }
            crate::services::agent_approval::ApprovalOutcome::Failed(e) => {
                return Err(SkillError::internal(e));
            }
            // The skill's gates are the request's last: nothing is behind
            // them to refuse. Answered as the refusal it would be.
            crate::services::agent_approval::ApprovalOutcome::Refused(r) => {
                return Err(SkillError {
                    status_code: r.status.as_u16(),
                    message: r.message,
                    body: r.body,
                    refusal: r.subtype,
                    argument_policy_denied: r.argument_policy_denied,
                });
            }
        }
    }

    // 7. Enforce skill quota (execution counts, budget, tokens). Admitted
    // under a hold: committed once the execution exists, given back (the
    // guard's Drop) if a later step refuses it.
    let mut quota_hold = crate::services::limits::PendingHold::of(
        crate::services::limits::HoldId::new(tenant_id, "skill-quota"),
    );
    let quota = usage::enforce_skill_quota_counted(
        db,
        tenant_id,
        Some(&group),
        Some(&skill.id),
        Some(state.usage_state.as_ref()),
        Some(&mut quota_hold),
    )
    .await;
    // A guard for what was admitted, and only for that.
    let quota_hold = quota_hold.held().map(|h| state.usage_state.adopt_hold(h));
    if quota.blocked {
        return Err(SkillError::forbidden(
            quota
                .message
                .unwrap_or_else(|| "Skill quota exceeded".to_string()),
        )
        .refused(crate::constants::proxy_subtype::SKILL_QUOTA_EXCEEDED));
    }

    // 8. Record the execution, RUNNING. With a concurrency ceiling the count
    // and the INSERT are serialized per group under `pg_advisory_xact_lock`,
    // so replicas cannot both take the last slot.
    let execution_id = {
        let max_concurrent = group
            .skill_global_governance
            .as_ref()
            .and_then(|g| g.get("max_concurrent_executions"))
            .and_then(|v| v.as_i64())
            .filter(|&v| v > 0);

        let mut tx = db
            .begin()
            .await
            .map_err(|e| SkillError::internal(format!("Failed to begin transaction: {e}")))?;

        if let Some(max_concurrent) = max_concurrent {
            sqlx::query("SELECT pg_advisory_xact_lock(hashtext($1))")
                .bind(format!("skill_concurrent_{}", group.id))
                .execute(&mut *tx)
                .await
                .map_err(|e| {
                    SkillError::internal(format!("Failed to acquire advisory lock: {e}"))
                })?;

            // An unreadable count refuses: it is never read as zero.
            let running_count: (i64,) = sqlx::query_as(
                "SELECT COUNT(*) FROM agent_skill_executions WHERE tenant_id = $1 AND group_id = $2 AND execution_status = 'RUNNING'"
            )
            .bind(tenant_id)
            .bind(&group.id)
            .fetch_one(&mut *tx)
            .await
            .map_err(|e| {
                SkillError::internal(format!("Failed to count running executions: {e}"))
            })?;

            if running_count.0 >= max_concurrent {
                // The transaction rolls back, releasing the lock.
                return Err(SkillError::conflict(format!(
                    "Max concurrent skill executions reached ({}/{}) for group '{}'",
                    running_count.0, max_concurrent, group.name
                ))
                .refused(crate::constants::proxy_subtype::SKILL_CONCURRENCY_LIMIT));
            }
        }

        let execution_id = insert_running_execution(
            &mut tx,
            &skill,
            &group,
            auth,
            tenant_id,
            correlation_id,
            request,
        )
        .await?;

        tx.commit()
            .await
            .map_err(|e| SkillError::internal(format!("Failed to commit transaction: {e}")))?;

        execution_id
    };

    // The execution exists: it counts, toward its quotas and as its run's
    // action.
    if let Some(quota_hold) = quota_hold {
        quota_hold.commit().await;
    }
    step_admitted.commit().await;

    // Extract allowed_tools from skill
    let allowed_tools: Option<Vec<String>> = skill
        .allowed_tools
        .as_ref()
        .and_then(|v| serde_json::from_value::<Vec<String>>(v.clone()).ok());

    // Extract recommended model from skill_metadata.required_llm_models
    let recommended_model: Option<String> = skill
        .metadata
        .as_ref()
        .and_then(|m| m.get("required_llm_models"))
        .and_then(|v| v.as_array())
        .and_then(|arr| arr.first())
        .and_then(|v| v.as_str())
        .map(String::from);

    // Extract max_tool_calls_per_execution from group governance
    let max_tool_calls = group
        .skill_global_governance
        .as_ref()
        .and_then(|g| g.get("max_tool_calls_per_execution"))
        .and_then(|v| v.as_i64());

    // Skill prompt (SKILL.md content) — provided as-is for context injection.
    // The calling agent injects this into the LLM conversation.
    let skill_prompt = skill.skill_md_content.clone();

    Ok(SkillExecutionResult {
        execution_id,
        skill_id: skill.id.clone(),
        skill_name: skill.name.clone(),
        skill_version: skill.version.clone(),
        execution_status: "RUNNING".to_string(),
        correlation_id: request
            .correlation_id
            .clone()
            .unwrap_or_else(|| correlation_id.to_string()),
        group_id: group.id,
        group_name: group.name,
        warning_headers: quota.warning_headers,
        allowed_tools,
        recommended_model,
        skill_prompt,
        skill_description: Some(skill.description.clone()),
        max_tool_calls_per_execution: max_tool_calls,
    })
}

// =============================================================================
// Run + settle a dispatched execution
// =============================================================================

/// How a dispatched execution ended in [`run_and_settle`].
#[derive(Debug)]
pub enum Settlement {
    /// The runner was called and the execution is settled:
    /// `result.execution_status` is `COMPLETED` or `FAILED`. `output` is the
    /// governed runner output (`None` when the run failed).
    Ran {
        output: Option<String>,
        /// The skill-surface output guardrail redacted PII: the caller's
        /// audit row must be flagged `pii_detected`.
        pii_redacted: bool,
    },
    /// Settled, but the skill-surface output guardrail blocked the runner
    /// output. The output must not leave; `message` is the refusal text.
    OutputBlocked { check: String, message: String },
    /// Settled, but the guardrail configs could not be read: the caller
    /// refuses (fail closed) instead of returning an unchecked result.
    GuardrailsUnavailable(sqlx::Error),
}

/// Settle a dispatched (RUNNING) execution as FAILED without running it — the
/// caller refused it after `execute_skill` (e.g. a guardrail on the skill
/// prompt).
pub async fn settle_unrun(
    db: &PgPool,
    tenant_id: &str,
    result: &mut SkillExecutionResult,
    error_type: &str,
    error_message: &str,
) {
    if let Err(e) = fail_execution(
        db,
        &result.execution_id,
        tenant_id,
        error_type,
        error_message,
    )
    .await
    {
        tracing::warn!(execution_id = %result.execution_id, error = %e.message, "Failed to mark skill execution failed");
    }
    result.execution_status = "FAILED".to_string();
}

/// Run an execution `execute_skill` dispatched, settle it, and govern its
/// output — the one place a dispatched execution becomes COMPLETED / FAILED.
///
/// `execute_skill` only authorises, gates and records a RUNNING execution;
/// RUNNING rows count against the group's `max_concurrent_executions`, so a
/// caller that dispatches without settling leaks a slot for good (both
/// inbound A2A paths did, until every skill in the group was refused). Every
/// caller of `execute_skill` therefore runs the execution through here (or,
/// like the MCP `run_script` tool, settles it itself) —
/// `tests/skill_settle_guard.rs` pins that.
///
/// Steps: the skill runner executes the skill with `input_params`; the
/// execution is settled COMPLETED or FAILED (`skill_execution_error`), or
/// FAILED (`skill_resolve_error`) when the skill cannot be re-read to run.
/// The runner's `output` is the assembled SKILL.md prompt; the skill's real
/// result is each script's stdout, so the scripts' stdout (sorted by filename)
/// is returned, falling back to the assembled output when no script printed.
/// That output then passes the skill-surface OUTPUT guardrail.
pub async fn run_and_settle(
    state: &crate::services::SharedState,
    auth: &AuthContext,
    tenant_id: &str,
    result: &mut SkillExecutionResult,
    input_params: Option<&serde_json::Value>,
) -> Settlement {
    use crate::services::guardrails;

    let mut runner_output: Option<String> = None;
    match resolve_skill(&state.db, tenant_id, Some(&result.skill_id), None).await {
        Ok(full_skill) => {
            let exec_output =
                crate::services::skill_runner_client::execute(&state.db, &full_skill, input_params)
                    .await;
            if exec_output.success {
                if let Err(e) = complete_execution(&state.db, &result.execution_id, tenant_id).await
                {
                    tracing::warn!(execution_id = %result.execution_id, error = %e.message, "Failed to mark skill execution completed");
                }
                result.execution_status = "COMPLETED".to_string();
                let mut names: Vec<&String> = exec_output.script_results.keys().collect();
                names.sort();
                let script_out: String = names
                    .iter()
                    .filter_map(|n| exec_output.script_results.get(*n))
                    .map(|s| s.stdout.trim_end())
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join("\n");
                runner_output = Some(if script_out.is_empty() {
                    exec_output.output
                } else {
                    script_out
                });
            } else {
                let msg: String = exec_output
                    .error
                    .clone()
                    .unwrap_or_else(|| exec_output.output.clone());
                let msg: String = msg.chars().take(500).collect();
                settle_unrun(&state.db, tenant_id, result, "skill_execution_error", &msg).await;
            }
        }
        Err(e) => {
            // Was "execution left RUNNING" — a leaked concurrency slot.
            tracing::warn!(skill_id = %result.skill_id, error = %e.message, "Could not resolve skill for runner dispatch; execution settled FAILED");
            let msg: String = e.message.chars().take(500).collect();
            settle_unrun(&state.db, tenant_id, result, "skill_resolve_error", &msg).await;
        }
    }

    // Skill output guardrails over the runner's actual output — surface
    // "skill", direction Output. A redact still enforces (the output is
    // scrubbed) but must ALSO be audited, so PII redaction is reported.
    let Some(out) = runner_output else {
        return Settlement::Ran {
            output: None,
            pii_redacted: false,
        };
    };
    if out.is_empty() {
        return Settlement::Ran {
            output: Some(out),
            pii_redacted: false,
        };
    }
    let configs = match state
        .guardrails_cache
        .get_configs(
            tenant_id,
            auth.guardrail_user_id().as_deref(),
            auth.guardrail_group_id(),
            &state.db,
        )
        .await
    {
        Ok(c) => c,
        // Fail closed — an unchecked skill result is exactly the
        // content output guardrails exist to redact. Every caller refuses on
        // this variant (REST: `configs_unavailable`; A2A: a
        // `guardrails_unavailable` refusal); the output never leaves.
        Err(e) => return Settlement::GuardrailsUnavailable(e),
    };
    if configs.is_empty() {
        return Settlement::Ran {
            output: Some(out),
            pii_redacted: false,
        };
    }
    let surface = crate::constants::guardrail_surface::SKILL;
    match guardrails::check_output(&out, &configs, surface, &state.http_client).await {
        guardrails::CheckOutcome::Block(violation) => {
            tracing::warn!(
                check = %violation.check,
                config = %violation.config_name,
                "Skill runner-output guardrail violation — output withheld"
            );
            crate::services::proxy_metrics::record_guardrail_block(surface, &violation.check);
            Settlement::OutputBlocked {
                message: format!(
                    "Skill output blocked by guardrail '{}' (check={}): {}",
                    violation.config_name, violation.check, violation.message
                ),
                check: violation.check,
            }
        }
        guardrails::CheckOutcome::Redacted {
            content: redacted,
            warnings,
        } => {
            let mut pii_redacted = false;
            for w in &warnings {
                if w.check.to_ascii_lowercase().contains("pii") {
                    pii_redacted = true;
                }
                tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
            }
            Settlement::Ran {
                output: Some(redacted),
                pii_redacted,
            }
        }
        guardrails::CheckOutcome::Warn { warnings } => {
            for w in &warnings {
                tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
            }
            Settlement::Ran {
                output: Some(out),
                pii_redacted: false,
            }
        }
        guardrails::CheckOutcome::Pass => Settlement::Ran {
            output: Some(out),
            pii_redacted: false,
        },
    }
}

// =============================================================================
// Execution Status
// =============================================================================

/// Who a skill execution belongs to: the caller that started it. The rule a
/// file has (`routes::files`): the end user when the caller has one,
/// otherwise the resource group the caller acts in.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecutionOwner {
    /// An end user: a key issued to one, or a portal session.
    User(i32),
    /// A caller bound to one group with no end user: a group key, a
    /// workload agent.
    Group(String),
}

/// The end user `auth` is, if any: the user an API key was issued to, or the
/// user of a portal session. An administrator's or a service's session
/// names no end user. Recorded as the execution's `user_id`.
fn end_user_of(auth: &AuthContext) -> Option<i32> {
    auth.api_key_user_id.or_else(|| {
        (auth.session_token_type == Some(crate::services::credential_verifier::TOKEN_TYPE_PORTAL))
            .then(|| auth.gateway_user_id.as_deref()?.parse().ok())
            .flatten()
    })
}

impl ExecutionOwner {
    /// The owner `auth` is. `None` for a caller that is neither an end user
    /// nor bound to a group: it started no execution, and reads none.
    pub fn of(auth: &AuthContext) -> Option<Self> {
        match end_user_of(auth) {
            Some(user) => Some(Self::User(user)),
            None => auth.bound_group_id().map(|g| Self::Group(g.to_string())),
        }
    }
}

/// The execution `execution_id` of `tenant_id` when `auth` started it
/// ([`ExecutionOwner`]). Not found for an execution that does not exist and
/// for one that is another caller's: the same answer. An execution that
/// records no user is its group's; one that records neither is nobody's.
async fn load_own_execution(
    db: &PgPool,
    execution_id: &str,
    tenant_id: &str,
    auth: &AuthContext,
) -> Result<AgentSkillExecution, SkillError> {
    let not_found = || SkillError::not_found(format!("Execution {execution_id} not found"));
    let (user, group) = match ExecutionOwner::of(auth).ok_or_else(not_found)? {
        ExecutionOwner::User(user) => (Some(user), None),
        ExecutionOwner::Group(group) => (None, Some(group)),
    };
    sqlx::query_as::<_, AgentSkillExecution>(
        r#"SELECT id, tenant_id, skill_id, group_id, user_id,
                  skill_name, skill_version, group_name, username,
                  request_id, correlation_id, parent_execution_id,
                  execution_status::text AS execution_status,
                  started_at, running_at, completed_at, duration_ms,
                  error_type, error_message, execution_context
           FROM agent_skill_executions
           WHERE id = $1 AND tenant_id = $2
             AND (user_id = $3 OR (user_id IS NULL AND group_id = $4))"#,
    )
    .bind(execution_id)
    .bind(tenant_id)
    .bind(user)
    .bind(group)
    .fetch_optional(db)
    .await
    .map_err(|e| SkillError::internal(format!("DB error: {e}")))?
    .ok_or_else(not_found)
}

pub async fn get_execution_status(
    db: &PgPool,
    execution_id: &str,
    tenant_id: &str,
    auth: &AuthContext,
) -> Result<ExecutionStatusResult, SkillError> {
    let exec = load_own_execution(db, execution_id, tenant_id, auth).await?;

    let skill_name = exec.skill_name.as_deref().unwrap_or("");
    let message = build_status_message(
        skill_name,
        &exec.execution_status,
        exec.duration_ms,
        exec.error_message.as_deref(),
    );

    Ok(ExecutionStatusResult {
        execution_id: exec.id,
        skill_id: exec.skill_id.unwrap_or_default(),
        skill_name: exec.skill_name.unwrap_or_default(),
        execution_status: exec.execution_status,
        correlation_id: exec.correlation_id,
        message,
    })
}

// =============================================================================
// Execution Cancellation
// =============================================================================

pub async fn cancel_execution(
    db: &PgPool,
    execution_id: &str,
    tenant_id: &str,
    auth: &AuthContext,
    cancelled_by: &str,
) -> Result<ExecutionStatusResult, SkillError> {
    let exec = load_own_execution(db, execution_id, tenant_id, auth).await?;

    if exec.execution_status != "RUNNING" {
        return Err(SkillError::conflict(format!(
            "Cannot cancel execution in '{}' status. Only a running execution can be cancelled.",
            exec.execution_status
        )));
    }

    let now = Utc::now();
    let reason = format!("Cancelled by {cancelled_by}");
    sqlx::query(
        r#"UPDATE agent_skill_executions
           SET execution_status = 'CANCELLED'::executionstatus, completed_at = $1, error_message = $2
           WHERE id = $3 AND tenant_id = $4
             AND execution_status = 'RUNNING'::executionstatus"#,
    )
    .bind(now)
    .bind(&reason)
    .bind(execution_id)
    .bind(tenant_id)
    .execute(db)
    .await
    .map_err(|e| SkillError::internal(format!("Failed to cancel execution: {e}")))?;

    let skill_name = exec.skill_name.clone().unwrap_or_default();
    Ok(ExecutionStatusResult {
        execution_id: exec.id,
        skill_id: exec.skill_id.unwrap_or_default(),
        skill_name: skill_name.clone(),
        execution_status: "CANCELLED".to_string(),
        correlation_id: exec.correlation_id,
        message: format!("Skill '{skill_name}' execution cancelled."),
    })
}

// =============================================================================
// Catalog
// =============================================================================

/// Get the skill catalog for the authenticated user.
/// Progressive Disclosure Level 1: lightweight entries designed for agent context injection.
pub async fn get_catalog(
    db: &PgPool,
    auth: &AuthContext,
    tenant_id: &str,
    category: Option<&str>,
    tag: Option<&str>,
) -> Result<CatalogResult, SkillError> {
    // The catalog lists exactly what the Agent Skills server will let this
    // caller load: the same group scoping (`resolve_scoping_group_ids`, which
    // honours the portal's active workspace when the user really is a member)
    // and the same allowed set (direct assignments plus skills inherited from
    // ancestor groups). Before this, the catalog resolved its own group, the
    // user's DEEPEST membership, ignoring the workspace the portal sent; a
    // user whose primary workspace was a top-level AI System saw an empty
    // Skills tab while models and MCP servers showed.
    let scope = crate::services::system_server::resolve_scoping_group_ids(db, auth)
        .await
        .map_err(|e| SkillError::internal(format!("DB error resolving skill scope: {e}")))?;

    let (skills, target_group) = match scope {
        Some(group_ids) => {
            // The scoped groups, each with its ANCESTOR-MERGED skill
            // governance: reading `discovery_enabled` off the group's own
            // row meant a parent that turned discovery off did not reach a
            // child with no skill governance of its own.
            let mut groups = sqlx::query_as::<_, ResourceGroup>(
                r#"SELECT *, group_type::text AS group_type FROM resource_groups
                   WHERE tenant_id = $1 AND id = ANY($2) AND is_active = true
                   ORDER BY hierarchy_level DESC NULLS LAST, id"#,
            )
            .bind(tenant_id)
            .bind(&group_ids)
            .fetch_all(db)
            .await
            .map_err(|e| SkillError::internal(format!("DB error resolving catalog groups: {e}")))?;
            // Several groups (no active workspace): the user's own order —
            // primary first — so the catalog reports the group their calls
            // are billed to, not merely the deepest.
            if groups.len() > 1 {
                if let Some(uid) = auth.gateway_user_id.as_deref() {
                    let order: Vec<String> = crate::services::usage::user_groups_ordered(
                        db,
                        tenant_id,
                        uid,
                        auth.resolved_group_id.as_deref(),
                    )
                    .await
                    .map_err(|e| SkillError::internal(format!("DB error ordering groups: {e}")))?
                    .into_iter()
                    .map(|g| g.id)
                    .collect();
                    groups.sort_by_key(|g| {
                        order
                            .iter()
                            .position(|id| *id == g.id)
                            .unwrap_or(usize::MAX)
                    });
                }
            }
            for g in &mut groups {
                g.skill_global_governance = collect_effective_skill_governance(db, g).await?;
            }

            // Discovery turned off for a group (or an ancestor) closes that
            // group's catalog. Several groups (no active workspace) are
            // allow-wins, like every other multi-group check.
            let open: Vec<ResourceGroup> = groups
                .iter()
                .filter(|g| !discovery_disabled(g))
                .cloned()
                .collect();
            if open.is_empty() {
                if let Some(g) = groups.first() {
                    return Err(SkillError::forbidden(format!(
                        "Skill discovery turned off for group \"{}\"",
                        g.name
                    )));
                }
            }

            let open_ids: Vec<String> = open.iter().map(|g| g.id.clone()).collect();
            let allowed: Vec<String> =
                crate::services::system_server::allowed_skill_names_for_groups(
                    db, tenant_id, &open_ids,
                )
                .await
                .map_err(|e| {
                    SkillError::internal(format!("DB error resolving allowed skills: {e}"))
                })?
                .into_iter()
                .collect();
            let skills = sqlx::query_as::<_, AgentSkill>(
                r#"SELECT id, tenant_id, name, description, license, compatibility,
                          allowed_tools, version, status::text AS status, enabled,
                          metadata, skill_md_content, execution_count, last_executed_at,
                          published_at, created_at, source_repo_id
                   FROM agent_skills
                   WHERE tenant_id = $1 AND enabled = true AND status = 'PUBLISHED'::skillstatus
                     AND name = ANY($2)
                   ORDER BY name"#,
            )
            .bind(tenant_id)
            .bind(&allowed)
            .fetch_all(db)
            .await
            .map_err(|e| SkillError::internal(format!("DB error fetching catalog: {e}")))?;
            // One group in scope (the active workspace, or a group-scoped
            // key): report it. Several: the user's first (`user_groups_ordered`).
            (skills, open.into_iter().next())
        }
        None => {
            // Unrestricted caller (no group-scoped key, not a user): the
            // tenant's default group when it resolves, else every published
            // skill, as before.
            let target_group =
                match crate::services::usage::resolve_target_group(db, auth, tenant_id).await {
                    Some(mut group) => {
                        group.skill_global_governance =
                            collect_effective_skill_governance(db, &group).await?;
                        Some(group)
                    }
                    None => None,
                };
            if let Some(ref group) = target_group {
                if discovery_disabled(group) {
                    return Err(SkillError::forbidden(format!(
                        "Skill discovery turned off for group \"{}\"",
                        group.name
                    )));
                }
            }
            let skills = if let Some(ref group) = target_group {
                sqlx::query_as::<_, AgentSkill>(
                    r#"SELECT s.id, s.tenant_id, s.name, s.description, s.license, s.compatibility,
                              s.allowed_tools, s.version, s.status::text AS status, s.enabled,
                              s.metadata, s.skill_md_content, s.execution_count, s.last_executed_at,
                              s.published_at, s.created_at, s.source_repo_id
                       FROM agent_skills s
                       JOIN resource_group_agent_skills rgas ON rgas.agent_skill_id = s.id
                       WHERE s.tenant_id = $1 AND s.enabled = true AND s.status = 'PUBLISHED'::skillstatus
                         AND rgas.resource_group_id = $2
                       ORDER BY s.name"#,
                )
                .bind(tenant_id)
                .bind(&group.id)
                .fetch_all(db)
                .await
                .map_err(|e| SkillError::internal(format!("DB error fetching catalog: {e}")))?
            } else {
                sqlx::query_as::<_, AgentSkill>(
                    r#"SELECT id, tenant_id, name, description, license, compatibility,
                              allowed_tools, version, status::text AS status, enabled,
                              metadata, skill_md_content, execution_count, last_executed_at,
                              published_at, created_at, source_repo_id
                       FROM agent_skills
                       WHERE tenant_id = $1 AND enabled = true AND status = 'PUBLISHED'::skillstatus
                       ORDER BY name"#,
                )
                .bind(tenant_id)
                .fetch_all(db)
                .await
                .map_err(|e| SkillError::internal(format!("DB error fetching catalog: {e}")))?
            };
            (skills, target_group)
        }
    };

    // Apply metadata filters in memory (same as Python)
    let mut filtered: Vec<serde_json::Value> = Vec::with_capacity(skills.len());
    for s in &skills {
        if let Some(cat) = category {
            let skill_cat = s.get_metadata_value("category").and_then(|v| v.as_str());
            if skill_cat != Some(cat) {
                continue;
            }
        }
        if let Some(t) = tag {
            let tags = s.get_tags();
            if !tags.iter().any(|st| st == t) {
                continue;
            }
        }
        filtered.push(s.to_catalog_json());
    }

    let total = filtered.len();
    Ok(CatalogResult {
        skills: filtered,
        total,
        group_id: target_group.as_ref().map(|g| g.id.clone()),
        group_name: target_group.as_ref().map(|g| g.name.clone()),
    })
}

// =============================================================================
// Private Helpers
// =============================================================================

/// Whether skill discovery is turned off for `group` (its ancestor-merged
/// skill governance, as `collect_effective_skill_governance` builds it).
fn discovery_disabled(group: &ResourceGroup) -> bool {
    group
        .skill_global_governance
        .as_ref()
        .and_then(|g| g.get("discovery_enabled"))
        .and_then(|v| v.as_bool())
        .map(|b| !b)
        .unwrap_or(false)
}

fn build_status_message(
    skill_name: &str,
    exec_status: &str,
    duration_ms: Option<i32>,
    error_message: Option<&str>,
) -> String {
    match exec_status {
        "COMPLETED" => {
            let mut msg = format!("Skill '{skill_name}' completed successfully.");
            if let Some(d) = duration_ms {
                msg.push_str(&format!(" Duration: {d}ms."));
            }
            msg
        }
        "FAILED" => format!(
            "Skill '{skill_name}' failed: {}",
            error_message.unwrap_or("Unknown error")
        ),
        "RUNNING" => format!("Skill '{skill_name}' is currently executing."),
        "CANCELLED" => format!("Skill '{skill_name}' was cancelled."),
        other => format!("Skill '{skill_name}' status: {other}"),
    }
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::db::{AgentSkill, ResourceGroup};

    // -----------------------------------------------------------------------
    // Skill governance merge — every field the schema accepts
    //
    // This merge and the control plane's `_skill_governance_restrictive_merge`
    // drifted: Python covered `discovery_enabled` and
    // `approval_timeout_seconds` while this side covered the two execution
    // ceilings, so the console and the proxy disagreed about four of the
    // five fields. The control plane's `test_python_rust_merge_parity.py`
    // now reads both sources; these tests pin this side's behaviour.
    // -----------------------------------------------------------------------

    #[test]
    fn merge_skill_governance_takes_the_strictest_execution_mode() {
        let mut base = serde_json::json!({"execution_mode": "unrestricted"});
        merge_skill_governance(
            &mut base,
            &serde_json::json!({"execution_mode": "disabled"}),
        );
        assert_eq!(base["execution_mode"], "disabled");

        let mut base = serde_json::json!({"execution_mode": "disabled"});
        merge_skill_governance(
            &mut base,
            &serde_json::json!({"execution_mode": "unrestricted"}),
        );
        assert_eq!(
            base["execution_mode"], "disabled",
            "a parent cannot loosen a child"
        );
    }

    #[test]
    fn merge_skill_governance_adopts_a_mode_the_child_never_set() {
        let mut base = serde_json::json!({});
        merge_skill_governance(
            &mut base,
            &serde_json::json!({"execution_mode": "approval_required"}),
        );
        assert_eq!(base["execution_mode"], "approval_required");
    }

    #[test]
    fn merge_skill_governance_ands_discovery() {
        // The gap that let a child re-open discovery a parent had closed.
        let mut base = serde_json::json!({"discovery_enabled": true});
        merge_skill_governance(&mut base, &serde_json::json!({"discovery_enabled": false}));
        assert_eq!(base["discovery_enabled"], false);

        let mut base = serde_json::json!({"discovery_enabled": false});
        merge_skill_governance(&mut base, &serde_json::json!({"discovery_enabled": true}));
        assert_eq!(
            base["discovery_enabled"], false,
            "false is the restrictive value"
        );

        let mut base = serde_json::json!({});
        merge_skill_governance(&mut base, &serde_json::json!({"discovery_enabled": false}));
        assert_eq!(
            base["discovery_enabled"], false,
            "inherited when the child is silent"
        );
    }

    #[test]
    fn merge_skill_governance_takes_the_min_of_every_numeric_ceiling() {
        let mut base = serde_json::json!({
            "approval_timeout_seconds": 600,
            "max_concurrent_executions": 99,
            "max_tool_calls_per_execution": 99,
        });
        merge_skill_governance(
            &mut base,
            &serde_json::json!({
                "approval_timeout_seconds": 60,
                "max_concurrent_executions": 2,
                "max_tool_calls_per_execution": 5,
            }),
        );
        assert_eq!(base["approval_timeout_seconds"], 60);
        assert_eq!(base["max_concurrent_executions"], 2);
        assert_eq!(base["max_tool_calls_per_execution"], 5);
    }

    #[test]
    fn merge_skill_governance_keeps_the_childs_tighter_value() {
        let mut base = serde_json::json!({"max_concurrent_executions": 1});
        merge_skill_governance(
            &mut base,
            &serde_json::json!({"max_concurrent_executions": 8}),
        );
        assert_eq!(base["max_concurrent_executions"], 1);
    }

    #[test]
    fn merge_skill_governance_covers_every_declared_field() {
        // A field in SKILL_GOVERNANCE_FIELDS that the merge ignores would be
        // stored, displayed, and then silently discarded at runtime.
        for field in SKILL_GOVERNANCE_FIELDS {
            let parent = match field {
                "execution_mode" => serde_json::json!({field: "disabled"}),
                "discovery_enabled" => serde_json::json!({field: false}),
                _ => serde_json::json!({field: 1}),
            };
            let mut base = serde_json::json!({});
            merge_skill_governance(&mut base, &parent);
            assert!(
                base.get(field).is_some(),
                "merge_skill_governance ignores {field}"
            );
        }
    }

    /// A child whose governance is JSON `null` (or not an object)
    /// states nothing the gate can read, so the parent's governance applies.
    /// This test used to pin the opposite: the child's `null` dropped the
    /// parent's `disabled`.
    #[test]
    fn merge_skill_governance_a_childs_null_document_inherits_the_parents() {
        for child in [serde_json::Value::Null, serde_json::json!("not an object")] {
            let mut base = child.clone();
            merge_skill_governance(
                &mut base,
                &serde_json::json!({"execution_mode": "disabled"}),
            );
            assert_eq!(
                base,
                serde_json::json!({"execution_mode": "disabled"}),
                "{child}"
            );
        }
        // A parent that is not an object changes nothing.
        let mut base = serde_json::json!({"execution_mode": "approval_required"});
        merge_skill_governance(&mut base, &serde_json::Value::Null);
        assert_eq!(
            base,
            serde_json::json!({"execution_mode": "approval_required"})
        );
    }

    /// `approval_timeout_seconds` and `max_concurrent_executions` are
    /// read only when positive, so a child's `0`, negative, `null` or
    /// non-number is no value at its level and the parent's stays.
    /// `max_tool_calls_per_execution` is handed to the agent as stated, so a
    /// child's `0` is the strictest value and stays.
    #[test]
    fn merge_skill_governance_a_childs_unreadable_ceiling_keeps_the_parents() {
        let parent = serde_json::json!({
            "approval_timeout_seconds": 600,
            "max_concurrent_executions": 3,
            "max_tool_calls_per_execution": 5,
        });
        for bad in [
            serde_json::json!(0),
            serde_json::json!(-1),
            serde_json::Value::Null,
            serde_json::json!("x"),
        ] {
            let mut base = serde_json::json!({
                "approval_timeout_seconds": bad.clone(),
                "max_concurrent_executions": bad.clone(),
            });
            merge_skill_governance(&mut base, &parent);
            assert_eq!(base["approval_timeout_seconds"], 600, "{bad}");
            assert_eq!(base["max_concurrent_executions"], 3, "{bad}");
            assert_eq!(base["max_tool_calls_per_execution"], 5, "{bad}");
        }
        let mut base = serde_json::json!({"max_tool_calls_per_execution": 0});
        merge_skill_governance(&mut base, &parent);
        assert_eq!(base["max_tool_calls_per_execution"], 0);
        // A parent's unreadable ceiling leaves the child's.
        let mut base = serde_json::json!({"max_concurrent_executions": 2});
        merge_skill_governance(
            &mut base,
            &serde_json::json!({"max_concurrent_executions": 0}),
        );
        assert_eq!(base["max_concurrent_executions"], 2);
    }

    /// A mode this build does not know outranks every known one: the merge
    /// keeps it, and the gate refuses on it (`ModeGate::Unknown`).
    #[test]
    fn skill_execution_strictness_ranks_unknown_as_most_restrictive() {
        assert_eq!(skill_execution_strictness(Some("disabled")), 2);
        assert_eq!(skill_execution_strictness(Some("approval_required")), 1);
        assert_eq!(skill_execution_strictness(Some("unrestricted")), 0);
        assert_eq!(skill_execution_strictness(Some("typo")), 3);
        assert_eq!(skill_execution_strictness(None), 0);
    }

    /// A parent's mode the gateway does not know (another string, or not a
    /// string) is not lost under a child's known one, and a child's is not
    /// replaced by a parent's known one: the merged view keeps it and the
    /// gate refuses.
    #[test]
    fn merge_skill_governance_keeps_an_unknown_execution_mode() {
        for unknown in [
            serde_json::json!("typo"),
            serde_json::json!(5),
            serde_json::json!({}),
        ] {
            for known in ["unrestricted", "approval_required", "disabled"] {
                let mut base = serde_json::json!({"execution_mode": known});
                merge_skill_governance(
                    &mut base,
                    &serde_json::json!({"execution_mode": unknown.clone()}),
                );
                assert_eq!(
                    base["execution_mode"], unknown,
                    "parent {unknown} over child {known}"
                );

                let mut base = serde_json::json!({"execution_mode": unknown.clone()});
                merge_skill_governance(&mut base, &serde_json::json!({"execution_mode": known}));
                assert_eq!(
                    base["execution_mode"], unknown,
                    "child {unknown} under parent {known}"
                );
            }
        }
    }

    // -----------------------------------------------------------------------
    // Helpers: construct minimal test structs
    // -----------------------------------------------------------------------

    fn make_skill(metadata: Option<serde_json::Value>) -> AgentSkill {
        AgentSkill {
            id: "skill-1".into(),
            tenant_id: "tenant-1".into(),
            name: "test-skill".into(),
            description: "A test skill".into(),
            license: None,
            compatibility: None,
            allowed_tools: None,
            version: "1.0.0".into(),
            status: "PUBLISHED".into(),
            enabled: true,
            metadata,
            skill_md_content: None,
            execution_count: 0,
            last_executed_at: None,
            published_at: None,
            created_at: None,
            source_repo_id: None,
        }
    }

    fn make_group(governance: Option<serde_json::Value>) -> ResourceGroup {
        ResourceGroup {
            id: "group-1".into(),
            tenant_id: "tenant-1".into(),
            parent_group_id: None,
            hierarchy_level: Some(0),
            hierarchy_path: Some("/group-1".into()),
            inherit_resources: false,
            name: "test-group".into(),
            display_name: None,
            description: None,
            icon: None,
            color: None,
            group_type: None,
            system_kind: None,
            is_active: true,
            is_system: false,
            llm_global_limits: None,
            mcp_global_limits: None,
            llm_global_governance: None,
            mcp_global_governance: None,
            skill_global_governance: governance,
            skill_global_limits: None,
            a2a_global_limits: None,
            a2a_global_governance: None,
            media_global_limits: None,
            media_global_governance: None,
            intended_client_systems: None,
            enforce_accepted_callers: false,
            created_by: None,
            updated_by: None,
            created_at: None,
            updated_at: None,
        }
    }

    // -----------------------------------------------------------------------
    // SkillError constructor tests
    // -----------------------------------------------------------------------

    #[test]
    fn skill_error_not_found_returns_404() {
        let err = SkillError::not_found("missing");
        assert_eq!(err.status_code, 404);
        assert_eq!(err.message, "missing");
    }

    #[test]
    fn skill_error_forbidden_returns_403() {
        let err = SkillError::forbidden("denied");
        assert_eq!(err.status_code, 403);
        assert_eq!(err.message, "denied");
    }

    #[test]
    fn skill_error_conflict_returns_409() {
        let err = SkillError::conflict("already exists");
        assert_eq!(err.status_code, 409);
        assert_eq!(err.message, "already exists");
    }

    #[test]
    fn skill_error_internal_returns_500() {
        let err = SkillError::internal("oops");
        assert_eq!(err.status_code, 500);
        assert_eq!(err.message, "oops");
    }

    #[test]
    fn skill_error_bad_request_returns_400() {
        let err = SkillError::bad_request("invalid input");
        assert_eq!(err.status_code, 400);
        assert_eq!(err.message, "invalid input");
    }

    #[test]
    fn skill_error_accepts_string_owned() {
        let err = SkillError::not_found(String::from("gone"));
        assert_eq!(err.status_code, 404);
        assert_eq!(err.message, "gone");
    }

    // -----------------------------------------------------------------------
    // build_status_message tests
    // -----------------------------------------------------------------------

    #[test]
    fn status_message_completed_with_duration() {
        let msg = build_status_message("my-skill", "COMPLETED", Some(1234), None);
        assert_eq!(
            msg,
            "Skill 'my-skill' completed successfully. Duration: 1234ms."
        );
    }

    #[test]
    fn status_message_completed_without_duration() {
        let msg = build_status_message("my-skill", "COMPLETED", None, None);
        assert_eq!(msg, "Skill 'my-skill' completed successfully.");
    }

    #[test]
    fn status_message_failed_with_error() {
        let msg = build_status_message("my-skill", "FAILED", None, Some("timeout"));
        assert_eq!(msg, "Skill 'my-skill' failed: timeout");
    }

    #[test]
    fn status_message_failed_without_error() {
        let msg = build_status_message("my-skill", "FAILED", None, None);
        assert_eq!(msg, "Skill 'my-skill' failed: Unknown error");
    }

    #[test]
    fn status_message_running() {
        let msg = build_status_message("my-skill", "RUNNING", None, None);
        assert_eq!(msg, "Skill 'my-skill' is currently executing.");
    }

    #[test]
    fn status_message_cancelled() {
        let msg = build_status_message("my-skill", "CANCELLED", None, None);
        assert_eq!(msg, "Skill 'my-skill' was cancelled.");
    }

    /// The orchestrator writes `RUNNING`, `COMPLETED`, `FAILED` and
    /// `CANCELLED` and nothing else: a state nothing writes has no message
    /// of its own.
    #[test]
    fn a_status_nothing_writes_has_no_message_of_its_own() {
        for status in ["PENDING", "TIMED_OUT"] {
            assert_eq!(
                build_status_message("my-skill", status, None, None),
                format!("Skill 'my-skill' status: {status}")
            );
        }
    }

    #[test]
    fn status_message_unknown_status() {
        let msg = build_status_message("my-skill", "WEIRD", None, None);
        assert_eq!(msg, "Skill 'my-skill' status: WEIRD");
    }

    // -----------------------------------------------------------------------
    // SkillExecutionResult::message tests
    // -----------------------------------------------------------------------

    #[test]
    fn execution_result_message() {
        let result = SkillExecutionResult {
            execution_id: "exec-2".into(),
            skill_id: "skill-1".into(),
            skill_name: "fast-skill".into(),
            skill_version: "1.3.0".into(),
            execution_status: "RUNNING".into(),
            correlation_id: "corr-2".into(),
            group_id: "g1".into(),
            group_name: "g1-name".into(),
            warning_headers: vec![],
            allowed_tools: None,
            recommended_model: None,
            skill_prompt: None,
            skill_description: None,
            max_tool_calls_per_execution: None,
        };
        assert_eq!(
            result.message(),
            "Skill 'fast-skill' v1.3.0 execution started."
        );
    }

    // -----------------------------------------------------------------------
    // The execution mode: three sources, the strictest wins
    // -----------------------------------------------------------------------

    /// A mode this gateway does not know (a typo, a value of another
    /// version, a value that is not text) refuses the call: it may have
    /// been meant as `disabled`. Whichever source states it, and whatever
    /// the others say short of `disabled`.
    #[test]
    fn an_unknown_execution_mode_refuses_the_call() {
        let skill = make_skill(None);
        for value in [
            serde_json::json!("dissabled"),
            serde_json::json!("APPROVAL_REQUIRED"),
            serde_json::json!(""),
            serde_json::json!(2),
            serde_json::json!(true),
            serde_json::json!({"mode": "disabled"}),
        ] {
            let group = make_group(Some(serde_json::json!({"execution_mode": value})));
            assert!(
                matches!(mode_gate(&skill, &group, &[]), ModeGate::Unknown(_)),
                "group mode {value}: {:?}",
                mode_gate(&skill, &group, &[])
            );
        }
        let plain = make_group(Some(serde_json::json!({"execution_mode": "unrestricted"})));
        assert_eq!(
            mode_gate(&skill, &plain, &[assignment(Some("off"), None)]),
            ModeGate::Unknown("off".into())
        );
        // Not relaxed by a source that asks for a person, nor by one that
        // says `unrestricted`.
        let asking = make_group(Some(
            serde_json::json!({"execution_mode": "approval_required"}),
        ));
        assert_eq!(
            mode_gate(
                &skill,
                &asking,
                &[
                    assignment(Some("unrestricted"), None),
                    assignment(Some("bogus"), None)
                ]
            ),
            ModeGate::Unknown("bogus".into())
        );
        // `disabled` is still `disabled`.
        let disabled = make_group(Some(serde_json::json!({"execution_mode": "disabled"})));
        assert_eq!(
            mode_gate(&skill, &disabled, &[assignment(Some("bogus"), None)]),
            ModeGate::Disabled
        );
        // A mode that is absent or null is unset, as before.
        let unset = make_group(Some(serde_json::json!({"execution_mode": null})));
        assert_eq!(
            mode_gate(&skill, &unset, &[assignment(None, None)]),
            ModeGate::Unrestricted
        );
    }

    fn assignment(mode: Option<&str>, window: Option<i32>) -> AssignmentGovernance {
        AssignmentGovernance {
            execution_mode: mode.map(str::to_string),
            approval_timeout_seconds: window,
        }
    }

    fn asked_by(gate: &ModeGate) -> Vec<&'static str> {
        match gate {
            ModeGate::ApprovalRequired { required_by, .. } => required_by.clone(),
            other => panic!("not held: {other:?}"),
        }
    }

    #[test]
    fn nothing_stated_is_unrestricted() {
        assert_eq!(
            mode_gate(&make_skill(None), &make_group(None), &[]),
            ModeGate::Unrestricted
        );
        let group = make_group(Some(serde_json::json!({"execution_mode": "unrestricted"})));
        let skill = make_skill(Some(serde_json::json!({"approval_required": false})));
        assert_eq!(
            mode_gate(
                &skill,
                &group,
                &[assignment(Some("unrestricted"), Some(60))]
            ),
            ModeGate::Unrestricted
        );
    }

    #[test]
    fn each_source_alone_asks_for_a_person() {
        let plain_skill = make_skill(None);
        let plain_group = make_group(None);
        let group = make_group(Some(
            serde_json::json!({"execution_mode": "approval_required"}),
        ));
        assert_eq!(asked_by(&mode_gate(&plain_skill, &group, &[])), ["group"]);
        let skill = make_skill(Some(serde_json::json!({"approval_required": true})));
        assert_eq!(asked_by(&mode_gate(&skill, &plain_group, &[])), ["skill"]);
        assert_eq!(
            asked_by(&mode_gate(
                &plain_skill,
                &plain_group,
                &[assignment(Some("approval_required"), None)]
            )),
            ["assignment"]
        );
        assert_eq!(
            asked_by(&mode_gate(
                &skill,
                &group,
                &[assignment(Some("approval_required"), None)]
            )),
            ["group", "skill", "assignment"]
        );
    }

    #[test]
    fn no_source_relaxes_another() {
        let group = make_group(Some(
            serde_json::json!({"execution_mode": "approval_required"}),
        ));
        // The skill says no approval and its assignment says unrestricted:
        // the group still asks.
        let skill = make_skill(Some(serde_json::json!({"approval_required": false})));
        assert_eq!(
            asked_by(&mode_gate(
                &skill,
                &group,
                &[assignment(Some("unrestricted"), None)]
            )),
            ["group"]
        );
        // An inherited assignment asks although the group's own does not.
        assert_eq!(
            asked_by(&mode_gate(
                &make_skill(None),
                &make_group(None),
                &[
                    assignment(Some("unrestricted"), None),
                    assignment(Some("approval_required"), None)
                ]
            )),
            ["assignment"]
        );
    }

    #[test]
    fn disabled_from_any_source_refuses() {
        let asking_skill = make_skill(Some(serde_json::json!({"approval_required": true})));
        let disabled_group = make_group(Some(serde_json::json!({"execution_mode": "disabled"})));
        assert_eq!(
            mode_gate(&asking_skill, &disabled_group, &[]),
            ModeGate::Disabled
        );
        let asking_group = make_group(Some(
            serde_json::json!({"execution_mode": "approval_required"}),
        ));
        assert_eq!(
            mode_gate(
                &asking_skill,
                &asking_group,
                &[
                    assignment(Some("approval_required"), None),
                    assignment(Some("disabled"), None)
                ]
            ),
            ModeGate::Disabled
        );
    }

    #[test]
    fn the_window_is_the_nearest_assignments_else_the_groups() {
        let group = make_group(Some(serde_json::json!(
            {"execution_mode": "approval_required", "approval_timeout_seconds": 900}
        )));
        let skill = make_skill(None);
        let window =
            |assignments: &[AssignmentGovernance]| match mode_gate(&skill, &group, assignments) {
                ModeGate::ApprovalRequired { window, .. } => window,
                other => panic!("not held: {other:?}"),
            };
        assert_eq!(window(&[]), Some(900));
        assert_eq!(window(&[assignment(None, None)]), Some(900));
        // Longer or shorter than the group's: the assignment's own applies.
        assert_eq!(window(&[assignment(None, Some(1800))]), Some(1800));
        assert_eq!(
            window(&[assignment(None, None), assignment(None, Some(60))]),
            Some(60),
            "the nearest assignment that states one"
        );
        assert_eq!(
            window(&[assignment(None, Some(300)), assignment(None, Some(60))]),
            Some(300)
        );
        let silent = make_group(Some(
            serde_json::json!({"execution_mode": "approval_required"}),
        ));
        assert_eq!(
            mode_gate(&skill, &silent, &[]),
            ModeGate::ApprovalRequired {
                window: None,
                required_by: vec!["group"]
            }
        );
    }

    #[test]
    fn a_held_call_carries_what_each_gate_said() {
        let mode = ModeGate::ApprovalRequired {
            window: Some(120),
            required_by: vec!["group", "assignment"],
        };
        assert!(person_gate(false, None, &ModeGate::Unrestricted).is_none());
        let grant = person_gate(true, None, &ModeGate::Unrestricted).expect("the grant holds");
        assert_eq!((grant.window, grant.metadata), (None, None));
        let held = person_gate(false, None, &mode).expect("the mode holds");
        assert_eq!(held.window, Some(120));
        assert_eq!(
            held.metadata,
            Some(serde_json::json!({"execution_mode": {"required_by": ["group", "assignment"]}}))
        );
    }

    #[test]
    fn the_execution_mode_policy_states_its_window_or_none() {
        assert_eq!(execution_mode_policy_window(&make_group(None)), None);
        let g = make_group(Some(serde_json::json!({"approval_timeout_seconds": 90})));
        assert_eq!(execution_mode_policy_window(&g), Some(90));
        let bad = make_group(Some(serde_json::json!({"approval_timeout_seconds": -5})));
        assert_eq!(execution_mode_policy_window(&bad), None);
        // Clamping to 7 days is the resolver's job (`approval_window::choose`).
        let huge = make_group(Some(
            serde_json::json!({"approval_timeout_seconds": i64::MAX}),
        ));
        assert_eq!(
            crate::services::approval_window::choose(
                execution_mode_policy_window(&huge),
                None,
                None
            )
            .seconds,
            crate::services::approval_window::MAX_SECONDS
        );
    }
}

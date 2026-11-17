// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! System MCP Server Handler
//!
//! Virtual MCP server implementation for system-managed servers.
//! Instead of proxying to an external HTTP server, the proxy handles
//! JSON-RPC requests internally and returns responses as if they came
//! from a real MCP server.
//!
//! Port of Python `app/services/mcp/server/system_server_handler.py`.
//!
//! ## System Server Detection
//!
//! Servers with `base_url` starting with `http://system/` are virtual.
//! The MCP proxy route (`src/routes/mcp.rs`) intercepts these after the
//! server DB lookup and delegates here instead of forwarding upstream.
//!
//! ## Supported MCP Methods
//!
//! ```text
//! initialize                 → Server info + capabilities declaration
//! notifications/initialized  → Acknowledgement (no-op / 202)
//! tools/list                 → Returns tools from MCPServer.capabilities,
//!                              filtered by user's group memberships via
//!                              resource_group_agent_skills association
//! tools/call                 → Full execution pipeline:
//!                              1. skill_orchestrator::execute_skill() — authorize,
//!                                 create execution record, start
//!                              2. skill_runner_client::execute() — load scripts/
//!                                 assets/resources from DB, mint a per-execution JWT,
//!                                 POST to the brutor-skill-runner sandbox container
//!                              3. Return MCP content blocks + execution metadata
//! resources/list             → Empty list (placeholder for future use)
//! resources/templates/list   → Empty list (placeholder for future use)
//! prompts/list               → Empty list (placeholder for future use)
//! ping                       → Health check (empty result)
//! ```
//!
//! ## Architecture
//!
//! ```text
//! MCP Client → Rust Proxy → system_server::handle_request()
//!                                   ↓
//!                 ┌─────────────────────────────────────┐
//!                 │  skill_orchestrator::execute_skill() │
//!                 │  (authorize + create execution)      │
//!                 └─────────────┬───────────────────────┘
//!                               ↓
//!                 ┌─────────────────────────────────────┐
//!                 │  skill_runner_client::execute()      │
//!                 │  (load → JWT-mint → POST to runner)  │
//!                 └─────────────┬───────────────────────┘
//!                               ↓ HTTPS (over docker net / Cloud Map)
//!                 ┌─────────────────────────────────────┐
//!                 │  brutor-skill-runner (separate      │
//!                 │  container; hardened sandbox)        │
//!                 │  (SKILL.md → scripts → templates)   │
//!                 └─────────────────────────────────────┘
//! ```
//!
//! ## Two-Layer Access Control
//!
//! Layer 1 (MCP server access): Checked in `src/routes/mcp.rs` via
//!     `resource_group_server_configs` — does the user's group have access
//!     to the Agent Skills MCP server?
//!
//! Layer 2 (per-skill access): Checked in `tools/list` via
//!     `resource_group_agent_skills` — is this specific skill assigned
//!     to the user's group? Anonymous/system callers bypass this filter.
//!
//! ## Logging & Metrics
//!
//! The MCP route (`handle_system_server` in `mcp.rs`) writes:
//!     - Proxy log with `proxy_subtype: "system_server"`
//!     - Proxy metric with `system_server: true` in mcp_data
//!     - Usage log for quota tracking
//!     - Prometheus request/latency counters

use serde_json::{Value, json};
use sqlx::PgPool;
use std::time::Instant;

use crate::constants;
use crate::models::auth::AuthContext;
use crate::models::db::McpServer;
use crate::models::proxy::SkillExecuteRequest;
use crate::services::SharedState;
// The MCP path is progressive disclosure. L1/L2 discover+load; L3
// act tools (skills__run_script / read_resource / render_template) run the
// sandbox via `skill_runner_client`, each governed + audited.
use crate::services::{search_server, skill_orchestrator, skill_runner_client};

// =============================================================================
// Response type returned to the MCP proxy route
// =============================================================================

pub struct SystemServerResponse {
    pub status_code: u16,
    pub body: Vec<u8>,
    pub headers: Vec<(String, String)>,
    /// JSON-RPC method name (for logging)
    pub mcp_method: Option<String>,
    /// JSON-RPC request id (for logging)
    pub mcp_request_id: Option<String>,
    /// Tool/capability name (for logging)
    pub mcp_capability_name: Option<String>,
}

/// The governance outcome of a per-skill L2/L3 operation (load / run_script /
/// read_resource / render_template), captured so it can be STAMPED ONTO THE
/// SINGLE `mcp` proxy-log row instead of writing a second `skill` row.
///
/// A skill call over MCP is ONE operation; it must produce ONE audit row (the
/// mcp row already carries the full JSON-RPC request/response body). This struct
/// carries the skill-specific facts the mcp row otherwise lacks — the skill id,
/// the real governance verdict (403/202 vs the transport-200), and the
/// guardrail/PII/policy flags that stamp the compliance tags.
#[derive(Clone, Default)]
pub(crate) struct SkillVerdict {
    pub skill_id: Option<String>,
    pub group_id: Option<String>,
    /// skill_load / skill_run / skill_read / skill_render / approval_required
    pub subtype: Option<String>,
    /// The real verdict status (e.g. 403 block, 202 approval) — the MCP
    /// transport itself is always 200, so this is what the audit row shows.
    pub status: u16,
    pub error: Option<String>,
    pub guardrail_blocked: bool,
    pub pii_detected: bool,
    pub policy_deny: bool,
}

tokio::task_local! {
    static SKILL_VERDICT: std::cell::RefCell<Option<SkillVerdict>>;
}

/// Run a system-server request future inside a skill-verdict scope and return
/// its output together with any verdict the skill layer recorded. The mcp-proxy
/// handler uses this to fold the skill outcome onto its single audit row.
pub(crate) async fn run_with_verdict<F>(fut: F) -> (F::Output, Option<SkillVerdict>)
where
    F: std::future::Future,
{
    SKILL_VERDICT
        .scope(std::cell::RefCell::new(None), async move {
            let out = fut.await;
            let v = SKILL_VERDICT.with(|c| c.borrow_mut().take());
            (out, v)
        })
        .await
}

// =============================================================================
// Main handler
// =============================================================================

/// Handle an MCP JSON-RPC request for a system server.
///
/// Parses the JSON-RPC envelope, dispatches to the appropriate method
/// handler, and wraps the result in a JSON-RPC response.
pub async fn handle_request(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    method: &str,
    // The request as the MCP route read it (`jsonrpc_body::read`)
    // — the ONE reading every control judged. This handler
    // executes that reading and never parses the bytes itself, so what runs
    // is what was judged. `None`: the body was not JSON.
    rpc_request: Option<&Value>,
    correlation_id: &str,
    // The caller's auth headers, forwarded to the semantic
    // policy judge's self-call. The judge evaluates by calling the governed
    // LLM surface as this caller; without them the self-call is unauthenticated,
    // the evaluation errors, and a tier-B policy silently never blocks — an
    // enforcement that looks configured and does nothing.
    auth_headers: &[(String, String)],
    // The run this MCP call belongs to (the proxy's call chain). A skill
    // executed here is gated by the run's controls and any hold it raises
    // names the run.
    run: &crate::services::run_controls::RunScope,
    // The request's approvals (`agent_approval::RequestApprovals`), shared
    // with the route's own gate: a script run finds its token there, or its
    // question answered.
    approvals: &std::sync::Arc<crate::services::agent_approval::RequestApprovals>,
) -> SystemServerResponse {
    // L3 governance (guardrails / arg policy / quota / audit) needs
    // the whole SharedState; the search delegation + L1/L2 need only these.
    let db = &state.db;
    let http_client = &state.http_client;
    let encryption = &state.encryption;
    // MCP Streamable HTTP spec: only POST carries JSON-RPC messages.
    if method != "POST" {
        let body = serde_json::to_vec(&json!({
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": "Method not allowed."},
            "id": null
        }))
        .unwrap_or_default();

        let hdrs = standard_headers(body.len());
        return SystemServerResponse {
            status_code: 405,
            body,
            headers: hdrs,
            mcp_method: None,
            mcp_request_id: None,
            mcp_capability_name: None,
        };
    }

    let Some(rpc_request) = rpc_request else {
        return build_jsonrpc_error(None, -32700, "Parse error: invalid JSON");
    };

    let jsonrpc_id = rpc_request.get("id").cloned();
    let rpc_method = rpc_request
        .get("method")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let params = rpc_request.get("params").cloned().unwrap_or(json!({}));

    // Per-request system-server invocation. Fires on every
    // tools/list, resources/list, prompts/list, and every
    // tools/call from the portal — many requests per chat
    // session. Routine traffic, not an on-call signal, so per
    // the proxy log taxonomy this lives at `debug`.
    tracing::debug!(
        server_id = %server.id,
        rpc_method,
        ?jsonrpc_id,
        user = ?auth.gateway_username,
        "System server request"
    );

    let capability_name = params
        .get("name")
        .and_then(|v| v.as_str())
        .map(String::from);

    // Search server — delegate entirely to search_server module
    // Use starts_with for tenant-scoped IDs (e.g., "system-search-server-ten-01KP...")
    if server
        .id
        .starts_with(constants::system_server::search::SERVER_ID)
    {
        let result = search_server::handle_request(
            db,
            server,
            auth,
            http_client,
            rpc_method,
            &params,
            jsonrpc_id.as_ref(),
            correlation_id,
            encryption,
        )
        .await;

        let mcp_method = Some(rpc_method.to_string());
        let mcp_request_id = jsonrpc_id.as_ref().map(|v| {
            if v.is_string() {
                v.as_str().unwrap().to_string()
            } else {
                v.to_string()
            }
        });

        return match result {
            Ok(mut result_value) => {
                // Modern results carry resultType (+ cache
                // metadata on the cacheable list methods). The search
                // tool surface is static → public, long TTL.
                if crate::services::mcp_wire::is_stateless_body(rpc_request) {
                    let cacheable = match rpc_method {
                        "tools/list" => Some((
                            3_600_000,
                            crate::constants::mcp_protocol::cache_scope::PUBLIC,
                        )),
                        _ => None,
                    };
                    crate::services::mcp_wire::stamp_modern_result(&mut result_value, cacheable);
                }
                build_jsonrpc_response(
                    jsonrpc_id.as_ref(),
                    &result_value,
                    mcp_method,
                    mcp_request_id,
                    capability_name,
                )
            }
            Err(err) => {
                let mut resp = build_jsonrpc_error(jsonrpc_id.as_ref(), err.code, &err.message);
                resp.mcp_method = mcp_method;
                resp.mcp_request_id = mcp_request_id;
                resp.mcp_capability_name = capability_name;
                resp
            }
        };
    }

    // Agent Skills server — dispatch to method handler
    let result = match rpc_method {
        "initialize" => handle_initialize(db, server, auth).await,

        // Dual-era: answers `server/discover` (2026-07-28)
        // alongside the legacy `initialize` handshake above. The L1
        // protocol preamble + access-scoped skill catalog move to the
        // `instructions` field of DiscoverResult — their sanctioned
        // stateless home. The catalog is access-scoped, so the result
        // is cacheable only within one authorization context (private).
        "server/discover" => match scoped_skill_catalog(db, server, auth).await {
            Err(e) => Err(JsonRpcError::from(e)),
            Ok(catalog) => Ok(crate::services::mcp_wire::discover_result(
                &[
                    crate::constants::mcp_protocol::V2026_07_28,
                    crate::constants::mcp_protocol::V2024_11_05,
                ],
                json!({
                    "tools": {"listChanged": true},
                    "resources": {"listChanged": false},
                    "prompts": {"listChanged": false},
                }),
                Some(build_instructions_text(&catalog)),
                "brutor-agent-skills",
                "1.0.0",
                60_000,
                crate::constants::mcp_protocol::cache_scope::PRIVATE,
            )),
        },

        "notifications/initialized" => {
            if jsonrpc_id.is_some() {
                Ok(json!({}))
            } else {
                // Notification (no id) — return 202 accepted with empty body
                return SystemServerResponse {
                    status_code: 202,
                    body: Vec::new(),
                    headers: standard_headers(0),
                    mcp_method: Some("notifications/initialized".to_string()),
                    mcp_request_id: None,
                    mcp_capability_name: None,
                };
            }
        }

        "ping" => Ok(json!({})),

        "tools/list" => handle_tools_list(db, server, auth, &params).await,

        "tools/call" => {
            handle_tools_call(
                state,
                server,
                auth,
                &params,
                correlation_id,
                auth_headers,
                run,
                approvals,
            )
            .await
        }

        "resources/list" => Ok(json!({"resources": []})),

        "resources/templates/list" => Ok(json!({"resourceTemplates": []})),

        "prompts/list" => Ok(json!({"prompts": []})),

        _ => {
            return build_jsonrpc_error(
                jsonrpc_id.as_ref(),
                -32601,
                &format!("Method not found: {rpc_method}"),
            );
        }
    };

    let mcp_method = Some(rpc_method.to_string());
    let mcp_request_id = jsonrpc_id.as_ref().map(|v| {
        if v.is_string() {
            v.as_str().unwrap().to_string()
        } else {
            v.to_string()
        }
    });

    match result {
        Ok(mut result_value) => {
            // Modern results carry resultType; the skills
            // tool list is access-scoped → private, short TTL so
            // admin catalog edits stay visible. Shaped by the request
            // dialect (`is_stateless_body`), so legacy callers are
            // unaffected.
            if crate::services::mcp_wire::is_stateless_body(rpc_request) {
                let cacheable = match rpc_method {
                    "tools/list" => {
                        Some((60_000, crate::constants::mcp_protocol::cache_scope::PRIVATE))
                    }
                    _ => None,
                };
                crate::services::mcp_wire::stamp_modern_result(&mut result_value, cacheable);
            }
            build_jsonrpc_response(
                jsonrpc_id.as_ref(),
                &result_value,
                mcp_method,
                mcp_request_id,
                capability_name,
            )
        }
        Err(err) => {
            let mut resp = build_jsonrpc_error(jsonrpc_id.as_ref(), err.code, &err.message);
            resp.mcp_method = mcp_method;
            resp.mcp_request_id = mcp_request_id;
            resp.mcp_capability_name = capability_name;
            resp
        }
    }
}

// =============================================================================
// Method Handlers
// =============================================================================

/// Handle MCP 'initialize' — returns server info + the level-1 skill catalog.
///
/// Option B: skills are NOT tools. The protocol preamble AND the access-scoped
/// skill *catalog* (names + descriptions the caller may use) go in the
/// `instructions` field, which MCP clients fold into the model's context — this
/// is the faithful L1 (skills as procedural knowledge in the system prompt, not
/// as callable tools). The callable tools are only the four `skills__*` verbs.
async fn handle_initialize(
    db: &PgPool,
    server: &McpServer,
    auth: &AuthContext,
) -> Result<Value, JsonRpcError> {
    let catalog = scoped_skill_catalog(db, server, auth).await?;
    Ok(initialize_result(&build_instructions_text(&catalog)))
}

/// Build the MCP `initialize` result with a given `instructions` body. Split out
/// so it is unit-testable without a DB.
fn initialize_result(instructions: &str) -> Value {
    json!({
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {"listChanged": true},
            "resources": {"listChanged": false},
            "prompts": {"listChanged": false},
        },
        "serverInfo": {
            "name": "brutor-agent-skills",
            "version": "1.0.0",
        },
        "instructions": instructions,
    })
}

/// Level 1: the access-scoped skill catalog (name, description) the caller
/// may use. Same group scoping as skill access (`resolve_scoping_group_ids` +
/// `allowed_skill_names_for_groups`); unrestricted callers see all published
/// skills.
async fn scoped_skill_catalog(
    db: &PgPool,
    server: &McpServer,
    auth: &AuthContext,
) -> Result<Vec<(String, String)>, sqlx::Error> {
    let auth_ctx = with_tenant(auth, server);
    let tenant_id = auth_ctx.tenant_id.clone();

    let all: Vec<(String, String)> = sqlx::query_as(
        "SELECT name, description FROM agent_skills \
         WHERE tenant_id = $1 AND status = 'PUBLISHED' AND enabled = true \
         ORDER BY name",
    )
    .bind(&tenant_id)
    .fetch_all(db)
    .await?;

    Ok(match resolve_scoping_group_ids(db, &auth_ctx).await? {
        None => all, // unrestricted (system/anonymous) caller — back-compat
        Some(group_ids) => {
            if group_ids.is_empty() {
                return Ok(Vec::new());
            }
            let allowed = allowed_skill_names_for_groups(db, &tenant_id, &group_ids).await?;
            all.into_iter()
                .filter(|(n, _)| allowed.contains(n))
                .collect()
        }
    })
}

/// Format the L1 `instructions` text: protocol preamble + the scoped catalog.
/// Descriptions are trimmed to keep each skill line compact (~100 tokens).
fn build_instructions_text(catalog: &[(String, String)]) -> String {
    let mut s = String::from(constants::system_server::agent_skill::INITIALIZE_INSTRUCTIONS);
    if catalog.is_empty() {
        s.push_str("\n\nNo skills are currently available to you.");
        return s;
    }
    s.push_str("\n\nAvailable skills — call `skills__load` with the name to use one:\n");
    for (name, desc) in catalog {
        let d = desc.trim();
        let short: String = if d.chars().count() > 240 {
            format!("{}…", d.chars().take(240).collect::<String>())
        } else {
            d.to_string()
        };
        s.push_str(&format!("- {name}: {short}\n"));
    }
    s
}

/// Level 1, `skills__list`: return the caller's access-scoped skill catalog
/// (name + description) as a tool result. This is the client-independent
/// discovery path: `tools/list` is always surfaced by MCP clients, whereas the
/// `initialize.instructions` catalog is only an optional hint some clients drop.
async fn handle_skills_list(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
) -> Result<Value, JsonRpcError> {
    let catalog = scoped_skill_catalog(&state.db, server, auth).await?;
    let skills: Vec<Value> = catalog
        .into_iter()
        .map(|(name, description)| json!({"name": name, "description": description}))
        .collect();
    Ok(json!({
        "content": [{
            "type": "text",
            "text": serde_json::to_string_pretty(&json!({"skills": skills})).unwrap_or_default(),
        }],
        "isError": false,
    }))
}

/// Handle MCP 'tools/list'.
///
/// Skills are NOT tools. This advertises ONLY the generic `skills__*` verbs
/// (L2 `skills__load` + the L3 act tools). The access-scoped skill *catalog* is
/// delivered in the `initialize` `instructions` field, where the model
/// discovers skills and loads them by name. (A `skill__<name>` tools/call
/// loads that skill, as `skills__load` does — see `parse_load_target`. This
/// list does not carry per-skill entries; the Portal's server listing does,
/// through [`skill_tools_for_groups`].)
async fn handle_tools_list(
    _db: &PgPool,
    _server: &McpServer,
    _auth: &AuthContext,
    _params: &Value,
) -> Result<Value, JsonRpcError> {
    Ok(json!({ "tools": skills_generic_tool_definitions() }))
}

/// Resolve which resource group(s) a caller's skill access is scoped to
/// (shared by `tools/list` filtering and `skills__load` access).
///
/// `None`   → unrestricted caller (no group-scoped key AND anonymous / system /
///            non-numeric gateway user id): the historical back-compat path
///            that exposes every skill.
/// `Some(v)` → the scoped group ids; an empty vec means the caller belongs to
///            no group and can therefore access no skills.
pub async fn resolve_scoping_group_ids(
    db: &PgPool,
    auth: &AuthContext,
) -> Result<Option<Vec<String>>, sqlx::Error> {
    // Group-scoped API key — the key itself pins the workspace. A
    // workload-identity agent is pinned to the group it acts in the same way.
    if let Some(bound) = auth.bound_group_id() {
        return Ok(Some(vec![bound.to_string()]));
    }
    // A workload agent that is a member of no group is offered no skill:
    // it is not one of the unrestricted service callers below.
    if auth.is_workload_agent() {
        return Ok(Some(Vec::new()));
    }

    // JWT / user-scoped caller. Non-user callers (anonymous / non-numeric id)
    // are unrestricted, for back-compat with system/service callers.
    let user_id_int: i32 = match &auth.gateway_user_id {
        Some(uid) if uid != "anonymous" && uid != "n/a" => match uid.parse() {
            Ok(id) => id,
            Err(_) => return Ok(None),
        },
        _ => return Ok(None),
    };

    // Real group memberships — direct (resource_group_memberships) plus
    // EndUserGroup-mediated (resource_group_end_user_groups → members).
    let memberships: Vec<String> = sqlx::query_scalar(
        r#"SELECT group_id FROM resource_group_memberships
           WHERE tenant_id = $1 AND user_id = $2
           UNION
           SELECT rgeug.resource_group_id
           FROM resource_group_end_user_groups rgeug
           JOIN end_user_group_members eugm
             ON eugm.end_user_group_id = rgeug.end_user_group_id
           WHERE eugm.end_user_id = $2"#,
    )
    .bind(&auth.tenant_id)
    .bind(user_id_int)
    .fetch_all(db)
    .await?;

    if memberships.is_empty() {
        return Ok(Some(Vec::new()));
    }

    // Honour the portal's active-workspace hint only when the user is a real
    // member (a spoofed `x-resource-group` header must not widen access).
    Ok(match auth.resolved_group_id.as_deref() {
        Some(active) if memberships.iter().any(|m| m == active) => Some(vec![active.to_string()]),
        _ => Some(memberships),
    })
}

/// Level-2 access check — may this caller load/use the named skill?
/// Same group scoping as `tools/list`: unrestricted callers may load anything;
/// otherwise the skill must be in the caller's group-allowed set.
async fn caller_may_access_skill(
    db: &PgPool,
    auth: &AuthContext,
    skill_name: &str,
) -> Result<bool, sqlx::Error> {
    Ok(match resolve_scoping_group_ids(db, auth).await? {
        None => true,
        Some(group_ids) => {
            if group_ids.is_empty() {
                return Ok(false);
            }
            allowed_skill_names_for_groups(db, &auth.tenant_id, &group_ids)
                .await?
                .contains(skill_name)
        }
    })
}

/// Collect the set of skill names accessible to the given resource
/// groups — direct `resource_group_agent_skills` assignments plus
/// skills inherited from ancestor groups (where the child group has
/// `inherit_from_parent = true`). Only `enabled` skills are counted.
pub async fn allowed_skill_names_for_groups(
    db: &PgPool,
    tenant_id: &str,
    group_ids: &[String],
) -> Result<std::collections::HashSet<String>, sqlx::Error> {
    if group_ids.is_empty() {
        return Ok(std::collections::HashSet::new());
    }

    // Direct assignments.
    let direct: Vec<String> = sqlx::query_scalar(
        r#"SELECT DISTINCT s.name
           FROM agent_skills s
           JOIN resource_group_agent_skills rgas ON rgas.agent_skill_id = s.id
           WHERE s.tenant_id = $1 AND s.enabled = true
             AND rgas.resource_group_id = ANY($2)"#,
    )
    .bind(tenant_id)
    .bind(group_ids)
    .fetch_all(db)
    .await?;

    // Parent-group inheritance: skills assigned to an ancestor group
    // are visible to descendants that opt into inheritance.
    let inherited: Vec<String> = sqlx::query_scalar(
        r#"SELECT DISTINCT s.name
           FROM agent_skills s
           JOIN resource_group_agent_skills rgas ON rgas.agent_skill_id = s.id
           JOIN resource_groups g ON g.id = rgas.resource_group_id
           WHERE s.tenant_id = $1 AND s.enabled = true
             AND EXISTS (
               SELECT 1 FROM resource_groups child
               WHERE child.id = ANY($2)
                 AND child.inherit_from_parent = true
                 AND child.hierarchy_path LIKE g.hierarchy_path || '%'
             )"#,
    )
    .bind(tenant_id)
    .bind(group_ids)
    .fetch_all(db)
    .await?;

    let mut allowed: std::collections::HashSet<String> = direct.into_iter().collect();
    allowed.extend(inherited);
    Ok(allowed)
}

/// Keep only the tools whose `skill__<name>` suffix is in `allowed`.
/// Non-skill tools (no `skill__` prefix) always pass through.
fn retain_allowed_skill_tools(
    all_tools: &[Value],
    allowed: &std::collections::HashSet<String>,
) -> Vec<Value> {
    all_tools
        .iter()
        .filter(|tool| {
            let tool_name = tool.get("name").and_then(|v| v.as_str()).unwrap_or("");
            match constants::system_server::agent_skill::skill_name_from_tool(tool_name) {
                Some(skill_name) => allowed.contains(skill_name),
                None => true,
            }
        })
        .cloned()
        .collect()
}

/// Build the MCP tool definitions for the skills assigned to a set of
/// resource groups (direct assignments + parent inheritance).
///
/// This is the group-scoped equivalent of a live `tools/list` against
/// the Agent Skills system server. The portal `/v1/portal/servers`
/// REST endpoint uses it to populate the Agent Skills server's
/// `capabilities.tools` so a single request returns the correct
/// per-workspace skill surface — no separate `tools/list` round-trip,
/// and the same data is available to any third-party API client.
pub async fn skill_tools_for_groups(
    db: &PgPool,
    tenant_id: &str,
    group_ids: &[String],
) -> Result<Vec<Value>, sqlx::Error> {
    if group_ids.is_empty() {
        return Ok(Vec::new());
    }
    let all_tools = build_tools_from_skills(db, tenant_id).await?;
    let allowed = allowed_skill_names_for_groups(db, tenant_id, group_ids).await?;
    Ok(retain_allowed_skill_tools(&all_tools, &allowed))
}

// =============================================================================
// Dynamic Tool Builder — converts agent_skills rows into MCP tool definitions
// =============================================================================

/// Lightweight skill row for tool definition building.
struct SkillRow {
    name: String,
    description: String,
    metadata: Option<Value>,
}

impl SkillInfo for SkillRow {
    fn name(&self) -> &str {
        &self.name
    }
    fn description(&self) -> &str {
        &self.description
    }
    fn metadata(&self) -> Option<&Value> {
        self.metadata.as_ref()
    }
}

/// Build MCP tool definitions dynamically from published+enabled agent_skills.
///
/// This replaces the Python write-through sync pattern: instead of pre-caching
/// tools in `mcp_servers.capabilities`, we query the source of truth directly.
/// With < 100 skills per tenant this is fast and always fresh.
async fn build_tools_from_skills(db: &PgPool, tenant_id: &str) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(String, String, Option<Value>)> = sqlx::query_as(
        "SELECT name, description, metadata FROM agent_skills \
         WHERE tenant_id = $1 AND status = 'PUBLISHED' AND enabled = true \
         ORDER BY name",
    )
    .bind(tenant_id)
    .fetch_all(db)
    .await?;

    Ok(rows
        .into_iter()
        .map(|(name, description, metadata)| {
            let row = SkillRow {
                name,
                description,
                metadata,
            };
            skill_to_tool_definition(&row)
        })
        .collect())
}

/// Convert a single agent_skill row into an MCP tool definition.
fn skill_to_tool_definition(skill: &impl SkillInfo) -> Value {
    let tool_name = constants::system_server::agent_skill::tool_name(skill.name());

    // Build description with metadata enrichment
    let mut parts: Vec<String> = Vec::new();
    let desc = skill.description();
    if !desc.is_empty() {
        parts.push(desc.to_string());
    }
    if let Some(meta) = skill.metadata() {
        if let Some(cat) = meta.get("category").and_then(|v| v.as_str()) {
            parts.push(format!("Category: {cat}"));
        }
        if let Some(tags) = meta.get("tags").and_then(|v| v.as_array()) {
            let tag_strs: Vec<&str> = tags.iter().filter_map(|t| t.as_str()).collect();
            if !tag_strs.is_empty() {
                parts.push(format!("Tags: {}", tag_strs.join(", ")));
            }
        }
        if meta
            .get("approval_required")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            parts.push("This skill requires human approval before execution.".to_string());
        }
        if let Some(cost) = meta.get("cost_estimate_usd").and_then(|v| v.as_f64()) {
            if cost > 0.0 {
                parts.push(format!("Estimated cost: ${cost:.4} per execution."));
            }
        }
    }
    let description = if parts.is_empty() {
        tool_name.clone()
    } else {
        parts.join(" | ")
    };

    // Build input schema
    let input_schema = build_input_schema(skill.metadata());

    // `skill__<name>` is the per-skill equivalent of `skills__load`: a read.
    json!({
        "name": tool_name,
        "description": description,
        "inputSchema": input_schema,
        "annotations": skill_read_annotations(),
    })
}

/// Build JSON Schema for tool input parameters from skill metadata.
///
/// Priority: 1) explicit input_schema, 2) simplified parameters list, 3) fallback.
fn build_input_schema(metadata: Option<&Value>) -> Value {
    let envelope_fields = json!({
        "group_id": {
            "type": "string",
            "description": "Optional: Target Resource Group ID. Auto-resolved from auth context if omitted."
        },
        "correlation_id": {
            "type": "string",
            "description": "Optional: Correlation ID for request tracing and workflow chaining."
        }
    });

    if let Some(meta) = metadata {
        // 1) Explicit input_schema
        if let Some(schema) = meta.get("input_schema") {
            if schema.is_object() {
                let mut result = schema.clone();
                if let Some(props) = result.get_mut("properties").and_then(|p| p.as_object_mut()) {
                    for (k, v) in envelope_fields.as_object().unwrap() {
                        props.entry(k.clone()).or_insert(v.clone());
                    }
                }
                return result;
            }
        }

        // 2) Simplified parameters list
        if let Some(params) = meta.get("parameters").and_then(|v| v.as_array()) {
            let mut properties = serde_json::Map::new();
            let mut required = Vec::new();

            for param in params {
                let Some(name) = param.get("name").and_then(|v| v.as_str()) else {
                    continue;
                };
                let mut prop = serde_json::Map::new();
                prop.insert(
                    "type".into(),
                    json!(
                        param
                            .get("type")
                            .and_then(|v| v.as_str())
                            .unwrap_or("string")
                    ),
                );
                if let Some(desc) = param.get("description").and_then(|v| v.as_str()) {
                    prop.insert("description".into(), json!(desc));
                }
                if let Some(e) = param.get("enum") {
                    prop.insert("enum".into(), e.clone());
                }
                if let Some(d) = param.get("default") {
                    prop.insert("default".into(), d.clone());
                }
                if let Some(min) = param.get("minimum") {
                    prop.insert("minimum".into(), min.clone());
                }
                if let Some(max) = param.get("maximum") {
                    prop.insert("maximum".into(), max.clone());
                }
                properties.insert(name.to_string(), Value::Object(prop));
                if param
                    .get("required")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false)
                {
                    required.push(json!(name));
                }
            }

            // Merge envelope fields
            for (k, v) in envelope_fields.as_object().unwrap() {
                properties.entry(k.clone()).or_insert(v.clone());
            }

            return json!({
                "type": "object",
                "properties": properties,
                "required": required,
            });
        }
    }

    // 3) Fallback — generic schema
    json!({
        "type": "object",
        "properties": {
            "input_params": {
                "type": "object",
                "description": "Skill-specific input parameters",
                "additionalProperties": true
            },
            "group_id": {
                "type": "string",
                "description": "Optional: Target Resource Group ID (auto-resolved if omitted)"
            }
        },
        "required": []
    })
}

/// Trait to abstract over skill row types for tool definition building.
trait SkillInfo {
    fn name(&self) -> &str;
    fn description(&self) -> &str;
    fn metadata(&self) -> Option<&Value>;
}

/// Handle MCP 'tools/call' — full execution pipeline via orchestrator.
///
/// MCP tools/call params: `{"name": "skill__query-sales-data", "arguments": {"date_range": "30d"}}`
/// MCP tools/call result: `{"content": [{"type": "text", "text": "..."}], "isError": false}`
async fn handle_tools_call(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    params: &Value,
    correlation_id: &str,
    auth_headers: &[(String, String)],
    run: &crate::services::run_controls::RunScope,
    approvals: &std::sync::Arc<crate::services::agent_approval::RequestApprovals>,
) -> Result<Value, JsonRpcError> {
    use constants::system_server::agent_skill as ask;

    let tool_name = params.get("name").and_then(|v| v.as_str()).unwrap_or("");
    let arguments = params.get("arguments").cloned().unwrap_or(json!({}));

    // The run controls were asked before this
    // function was reached: once, in the MCP route (`handle_system_server`),
    // for every `tools/call` of every system server, ahead of the rate
    // limits and the argument policy. `tests/mcp_one_reading_guard.rs` pins
    // that the route is the only caller of `handle_request` and the order of
    // its gates. `skills__run_script` is asked again inside `execute_skill`,
    // for the group the script runs in, because the REST and A2A callers
    // reach that without passing the route.

    // Level 1: discovery as a TOOL (client-independent; `tools/list` is
    // always surfaced, `initialize.instructions` is only an optional hint).
    if tool_name == ask::TOOL_LIST || tool_name.ends_with(ask::TOOL_LIST) {
        return handle_skills_list(state, server, auth).await;
    }

    // Level 3: governed act tools. Each runs its own governance chain and
    // writes its own per-layer audit row (matched with a trailing-suffix check
    // so client-prefixed tool names — e.g. "brutor__skills__run_script" — route
    // correctly).
    if tool_name == ask::TOOL_RUN_SCRIPT || tool_name.ends_with(ask::TOOL_RUN_SCRIPT) {
        return handle_run_script(
            state,
            server,
            auth,
            &arguments,
            correlation_id,
            auth_headers,
            run,
            approvals,
        )
        .await;
    }
    if tool_name == ask::TOOL_READ_RESOURCE || tool_name.ends_with(ask::TOOL_READ_RESOURCE) {
        return handle_read_resource(state, server, auth, &arguments, correlation_id).await;
    }
    if tool_name == ask::TOOL_RENDER_TEMPLATE || tool_name.ends_with(ask::TOOL_RENDER_TEMPLATE) {
        return handle_render_template(state, server, auth, &arguments, correlation_id).await;
    }

    // Otherwise L1/L2 — discover/load. Both the generic `skills__load` loader
    // and a per-skill `skill__<name>` entry resolve to L2 "load": return
    // SKILL.md + protocol preamble + manifest. There is deliberately no
    // one-shot execute here (that would be a *tool*).
    let Some(skill_name) = parse_load_target(tool_name, &arguments) else {
        return Ok(json!({
            "content": [{
                "type": "text",
                "text": format!(
                    "Unknown or unsupported tool '{tool_name}'. To use a skill, call \
                     'skills__load' with a skill_name (or the skill's own skill__<name> tool) \
                     to read its instructions, then use the skills__* tools it lists."
                ),
            }],
            "isError": true,
        }));
    };

    handle_skill_load(state, server, auth, &skill_name, correlation_id).await
}

/// Resolve which skill a `tools/call` targets for L2 load, from either the
/// generic `skills__load` (skill name in `arguments.skill_name`) or a per-skill
/// `skill__<name>` entry. Returns `None` for anything else — unknown tools and
/// the generic L3 `skills__*` act tools (handled by the L3 layer).
///
/// Pure (no I/O) so the dispatch rule is unit-testable.
fn parse_load_target(tool_name: &str, arguments: &Value) -> Option<String> {
    use constants::system_server::agent_skill as ask;
    let skill_prefix = format!("{}__", ask::TOOL_PREFIX);

    if tool_name == ask::TOOL_LOAD || tool_name.ends_with(ask::TOOL_LOAD) {
        // Generic loader — the skill name is an argument.
        arguments
            .get("skill_name")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(String::from)
    } else if tool_name.starts_with("skills__") {
        // A generic `skills__*` tool other than load (an L3 act tool): not an
        // L2 load target.
        None
    } else if let Some(idx) = tool_name.find(&skill_prefix) {
        // Per-skill `skill__<name>` entry — equivalent to loading that skill.
        Some(tool_name[idx + skill_prefix.len()..].to_string())
    } else {
        None
    }
}

/// Level 2: load a skill's instructions + manifest (no execution).
///
/// Governed: the caller must have group access to the skill (same scoping as
/// `tools/list`); the returned SKILL.md is prefixed with the injected protocol
/// preamble so the model runs L3 actions through Brutor, and the manifest lists
/// exactly what it may read / run / render. Nothing is executed here.
async fn handle_skill_load(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    skill_name: &str,
    correlation_id: &str,
) -> Result<Value, JsonRpcError> {
    use constants::system_server::agent_skill as ask;
    let db = &state.db;
    let start = Instant::now();

    // MCP clients (e.g. Goose) may carry no gateway auth — inject the server's
    // tenant so resolution + access checks run against the right tenant.
    let mut auth_ctx = auth.clone();
    if auth.tenant_id == "n/a" || auth.tenant_id == "anonymous" || auth.tenant_id.is_empty() {
        auth_ctx.tenant_id = server.tenant_id.clone();
    }
    let tenant_id = auth_ctx.tenant_id.clone();
    let req_json = json!({ "skill_name": skill_name }).to_string();

    // L2 access check — only skills the caller's group(s) allow may be loaded.
    if !caller_may_access_skill(db, &auth_ctx, skill_name).await? {
        tracing::debug!(skill_name, tenant_id = %tenant_id, "skills__load access denied");
        let msg = format!("Access denied: skill '{skill_name}' is not available to your group.");
        // Resolve the skill id best-effort (it exists tenant-wide, just isn't
        // assigned to this group) so the denial row still names which skill.
        let denied_id = skill_orchestrator::resolve_skill(db, &tenant_id, None, Some(skill_name))
            .await
            .ok()
            .map(|s| s.id);
        write_skill_layer_log(
            state,
            &auth_ctx,
            correlation_id,
            ask::TOOL_LOAD,
            constants::proxy_subtype::SKILL_LOAD,
            denied_id.as_deref(),
            None,
            403,
            dur_ms(start),
            false,
            Some(&msg),
            false,
            false,
            true,
            Some(&req_json),
            Some(&msg),
        );
        return Ok(err_result(&msg));
    }

    // Resolve the published skill (with SKILL.md body).
    let skill =
        match skill_orchestrator::resolve_skill(db, &tenant_id, None, Some(skill_name)).await {
            Ok(s) => s,
            Err(e) => {
                return Ok(err_result(&format!(
                    "Skill '{skill_name}' could not be loaded: {}",
                    e.message
                )));
            }
        };

    let manifest = build_skill_manifest(db, &tenant_id, &skill.id).await?;
    let skill_md = skill.skill_md_content.clone().unwrap_or_default();
    let preamble = ask_preamble();

    // L2 audit row — the load is a governed, on-the-record event.
    let group = crate::services::usage::resolve_target_group(db, &auth_ctx, &tenant_id).await;
    write_skill_layer_log(
        state,
        &auth_ctx,
        correlation_id,
        ask::TOOL_LOAD,
        constants::proxy_subtype::SKILL_LOAD,
        Some(&skill.id),
        group.as_ref().map(|g| g.id.as_str()),
        200,
        dur_ms(start),
        true,
        None,
        false,
        false,
        false,
        Some(&req_json),
        None,
    );

    tracing::debug!(
        skill_name = %skill.name,
        skill_id = %skill.id,
        tenant_id = %tenant_id,
        "skills__load (L2 progressive disclosure)"
    );

    // Block 1 — injected protocol preamble + the unmodified SKILL.md body.
    let instructions_block = format!(
        "{preamble}\n\n----- SKILL.md ({}) -----\n\n{skill_md}",
        skill.name
    );

    // Block 2 — the manifest: the exact L3 surface (scripts/resources/templates).
    let manifest_block = json!({
        "skill": {
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
        },
        "manifest": manifest,
    });

    Ok(json!({
        "content": [
            {"type": "text", "text": instructions_block},
            {"type": "text", "text": serde_json::to_string_pretty(&manifest_block).unwrap_or_default()},
        ],
        "isError": false,
    }))
}

/// The L2 injected protocol preamble (small indirection so tests can pin it).
fn ask_preamble() -> &'static str {
    constants::system_server::agent_skill::LOAD_PROTOCOL_PREAMBLE
}

/// MCP ToolAnnotations for a read-only skill verb (discover, load, read, render).
/// Pinned to `tests/fixtures/system_tool_annotations.json`, shared with the
/// control plane's `VERB_TOOLS`: the gateway classifies risk (autonomy
/// levels, `deny_tool_risk`, evidence) from these hints, and an unannotated
/// tool is treated as side-effecting — so a read that forgets them is blocked
/// by `approval_required` / `restricted`.
fn skill_read_annotations() -> Value {
    json!({"readOnlyHint": true, "idempotentHint": true, "openWorldHint": false})
}

/// `skills__run_script` acts. It is NOT read-only, and it deliberately does not
/// claim `destructiveHint: false`: a skill script can do anything its sandbox
/// allows, so the fail-closed default (destructive) stands.
fn skill_run_script_annotations() -> Value {
    json!({"readOnlyHint": false})
}

/// The generic progressive-disclosure toolset: L1 discover
/// (`skills__list`) + L2 load + L3 act. Discovery is a TOOL (not just the
/// `initialize.instructions` hint) so it is reachable on every MCP client.
fn skills_generic_tool_definitions() -> Vec<Value> {
    use constants::system_server::agent_skill as ask;
    vec![
        json!({
            "name": ask::TOOL_LIST,
            "description": "List the Agent Skills available to you (name + what each does). Call \
                            this first to discover which skills you can use, then `skills__load` \
                            one to read its instructions.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "annotations": skill_read_annotations()
        }),
        skills_load_tool_definition(),
        json!({
            "name": ask::TOOL_READ_RESOURCE,
            "description": "Read a bundled reference file (from a skill's manifest) into context. \
                            Use after skills__load when the instructions reference a file.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the file."},
                    "path": {"type": "string", "description": "The resource filename, exactly as listed in the manifest."}
                },
                "required": ["skill_name", "path"]
            },
            "annotations": skill_read_annotations()
        }),
        json!({
            "name": ask::TOOL_RUN_SCRIPT,
            "description": "Run one of a skill's scripts (from its manifest) with arguments, in \
                            Brutor's governed sandbox, and return only its output. Use when the \
                            instructions tell you to run a script.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the script."},
                    "script": {"type": "string", "description": "The script filename, exactly as listed in the manifest."},
                    "args": {"type": "object", "description": "Arguments for the script (per its args_schema).", "additionalProperties": true},
                    "approval_token": {"type": "string", "description": "One-time approval token(s) to retry a run that was held for approval, for a client that cannot set the X-Approval-Token header. Several tokens are separated by commas."}
                },
                "required": ["skill_name", "script"]
            },
            "annotations": skill_run_script_annotations()
        }),
        json!({
            "name": ask::TOOL_RENDER_TEMPLATE,
            "description": "Render one of a skill's templates/assets (from its manifest) with \
                            variables and return the result.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "The skill that owns the template."},
                    "template": {"type": "string", "description": "The template filename, exactly as listed in the manifest."},
                    "vars": {"type": "object", "description": "Variables to substitute into the template.", "additionalProperties": true}
                },
                "required": ["skill_name", "template"]
            },
            "annotations": skill_read_annotations()
        }),
    ]
}

/// Level 1: the generic progressive-disclosure loader tool definition.
fn skills_load_tool_definition() -> Value {
    json!({
        "name": constants::system_server::agent_skill::TOOL_LOAD,
        "description": "Load an Agent Skill's full instructions (SKILL.md) and its manifest of \
                        available scripts, resources and templates. Call this before using a \
                        skill (progressive disclosure). Equivalent to calling the skill's own \
                        skill__<name> tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to load (as shown by its skill__<name> tool)."
                }
            },
            "required": ["skill_name"]
        },
        "annotations": skill_read_annotations()
    })
}

/// Derive a skill's L2 manifest from its child rows (mirrors the
/// control plane's `AgentSkill.build_manifest()`). Disabled scripts are
/// omitted; `args_schema` / `vars_schema` come from the script rows. Enum
/// columns are cast to text and lower-cased to match the control-plane values.
/// A failed read is returned: a manifest missing its scripts or
/// resources tells the model the skill has none.
async fn build_skill_manifest(
    db: &PgPool,
    tenant_id: &str,
    skill_id: &str,
) -> Result<Value, sqlx::Error> {
    let resources: Vec<(String, String, Option<String>, Option<i32>)> = sqlx::query_as(
        r#"SELECT filename, resource_type::text, mime_type, size_bytes
           FROM agent_skill_resources
           WHERE tenant_id = $1 AND skill_id = $2
           ORDER BY filename"#,
    )
    .bind(tenant_id)
    .bind(skill_id)
    .fetch_all(db)
    .await?;

    let scripts: Vec<(String, String, String, Option<Value>, Option<String>)> = sqlx::query_as(
        r#"SELECT filename, language::text, execution_mode::text, args_schema, description
           FROM agent_skill_scripts
           WHERE tenant_id = $1 AND skill_id = $2 AND execution_mode::text <> 'DISABLED'
           ORDER BY filename"#,
    )
    .bind(tenant_id)
    .bind(skill_id)
    .fetch_all(db)
    .await?;

    let templates: Vec<(
        String,
        String,
        Option<String>,
        Option<Value>,
        Option<String>,
    )> = sqlx::query_as(
        r#"SELECT filename, asset_type::text, mime_type, vars_schema, description
               FROM agent_skill_assets
               WHERE tenant_id = $1 AND skill_id = $2
               ORDER BY filename"#,
    )
    .bind(tenant_id)
    .bind(skill_id)
    .fetch_all(db)
    .await?;

    Ok(json!({
        "resources": resources
            .into_iter()
            .map(|(path, kind, mime, bytes)| json!({
                "path": path,
                "kind": kind.to_lowercase(),
                "mime": mime,
                "bytes": bytes,
            }))
            .collect::<Vec<_>>(),
        "scripts": scripts
            .into_iter()
            .map(|(name, language, mode, args_schema, description)| json!({
                "name": name,
                "language": language.to_lowercase(),
                "execution_mode": mode.to_lowercase(),
                "args_schema": args_schema,
                "description": description,
            }))
            .collect::<Vec<_>>(),
        "templates": templates
            .into_iter()
            .map(|(name, kind, mime, vars_schema, description)| json!({
                "name": name,
                "kind": kind.to_lowercase(),
                "mime": mime,
                "vars_schema": vars_schema,
                "description": description,
            }))
            .collect::<Vec<_>>(),
    }))
}

// =============================================================================
// Level 3: governed act tools (read_resource / run_script / render_template)
// =============================================================================

/// An MCP `isError` tool result carrying a human/model-readable message.
fn err_result(msg: &str) -> Value {
    json!({ "content": [{"type": "text", "text": msg}], "isError": true })
}

fn dur_ms(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

/// Record the governance outcome of a per-skill L2/L3 step into the task-local
/// [`SkillVerdict`] slot. It does NOT write a proxy_log row — a skill call over
/// MCP is ONE operation and gets ONE row (the mcp-proxy handler stamps this
/// verdict onto its single row via [`run_with_verdict`]). Keeping the full
/// arg-set means the 15 call sites stay unchanged; `_`-params are the pieces the
/// mcp row already carries (auth, correlation, the full request/response body).
#[allow(clippy::too_many_arguments)]
fn write_skill_layer_log(
    _state: &SharedState,
    _auth: &AuthContext,
    _correlation_id: &str,
    _tool_name: &str,
    subtype: &str,
    skill_id: Option<&str>,
    group_id: Option<&str>,
    status_code: u16,
    _duration_ms: f64,
    _success: bool,
    error: Option<&str>,
    guardrail_blocked: bool,
    pii_detected: bool,
    policy_deny: bool,
    _request_body: Option<&str>,
    _response_body: Option<&str>,
) {
    let _ = SKILL_VERDICT.try_with(|c| {
        *c.borrow_mut() = Some(SkillVerdict {
            skill_id: skill_id.map(String::from),
            group_id: group_id.map(String::from),
            subtype: Some(subtype.to_string()),
            status: status_code,
            error: error.map(String::from),
            guardrail_blocked,
            pii_detected,
            policy_deny,
        });
    });
}

/// Outcome of the L3 output guardrail over content leaving the sandbox.
enum OutGuard {
    Blocked { check: String, message: String },
    Ok { content: String, pii: bool },
}

/// Run the "skill" output guardrail over content the model is about to receive
/// (resource body, script output, rendered template). A block stops the return;
/// a redact scrubs and flags PII.
async fn guard_skill_output(
    state: &SharedState,
    auth: &AuthContext,
    tenant_id: &str,
    content: String,
) -> OutGuard {
    use crate::services::guardrails;
    if content.is_empty() {
        return OutGuard::Ok {
            content,
            pii: false,
        };
    }
    let configs = state
        .guardrails_cache
        .get_configs(
            tenant_id,
            auth.guardrail_user_id().as_deref(),
            auth.guardrail_group_id(),
            &state.db,
        )
        .await;
    let configs = match configs {
        Ok(c) => c,
        // Fail closed — content the gateway cannot check is not returned.
        Err(e) => {
            tracing::error!(error = %e, "guardrail configs unavailable for skill output; refusing (fail closed)");
            return OutGuard::Blocked {
                check: "guardrails_unavailable".to_string(),
                message: "refusing to return content the gateway cannot check".to_string(),
            };
        }
    };
    if configs.is_empty() {
        return OutGuard::Ok {
            content,
            pii: false,
        };
    }
    match guardrails::check_output(&content, &configs, "skill", &state.http_client).await {
        guardrails::CheckOutcome::Block(v) => OutGuard::Blocked {
            check: v.check.clone(),
            message: format!(
                "Skill output blocked by guardrail '{}' (check={}): {}",
                v.config_name, v.check, v.message
            ),
        },
        guardrails::CheckOutcome::Redacted {
            content: red,
            warnings,
        } => {
            let pii = warnings
                .iter()
                .any(|w| w.check.to_ascii_lowercase().contains("pii"));
            OutGuard::Ok { content: red, pii }
        }
        guardrails::CheckOutcome::Warn { .. } | guardrails::CheckOutcome::Pass => OutGuard::Ok {
            content,
            pii: false,
        },
    }
}

/// Inject the server tenant for MCP clients without gateway auth.
pub fn with_tenant(auth: &AuthContext, server: &McpServer) -> AuthContext {
    let mut a = auth.clone();
    if a.tenant_id == "n/a" || a.tenant_id == "anonymous" || a.tenant_id.is_empty() {
        a.tenant_id = server.tenant_id.clone();
    }
    a
}

/// Whether `tool_name` is `skills__run_script` (a client may prefix it).
pub fn is_run_script(tool_name: &str) -> bool {
    use constants::system_server::agent_skill::TOOL_RUN_SCRIPT;
    tool_name == TOOL_RUN_SCRIPT || tool_name.ends_with(TOOL_RUN_SCRIPT)
}

/// The approval tokens a `skills__run_script` call carries in its
/// `approval_token` argument, for a client that cannot set a header.
pub fn run_script_tokens(arguments: &Value) -> Option<&str> {
    arguments
        .get(RUN_SCRIPT_TOKEN_ARGUMENT)
        .and_then(Value::as_str)
}

/// What a `skills__run_script` call asks to do: its arguments without the
/// `approval_token` argument. The token is a credential of the retry, not
/// part of the call a person approved, so an approval is asked for, and
/// compared against, the call without it.
pub fn run_script_content(arguments: &Value) -> std::borrow::Cow<'_, Value> {
    match arguments.as_object() {
        Some(map) if map.contains_key(RUN_SCRIPT_TOKEN_ARGUMENT) => {
            let mut map = map.clone();
            map.remove(RUN_SCRIPT_TOKEN_ARGUMENT);
            std::borrow::Cow::Owned(Value::Object(map))
        }
        _ => std::borrow::Cow::Borrowed(arguments),
    }
}

const RUN_SCRIPT_TOKEN_ARGUMENT: &str = "approval_token";

/// The skill execution a `skills__run_script` call is: built the same way
/// for the gate of the MCP route (which names the skill's gates beside its
/// own) and for the run.
pub fn run_script_request(
    arguments: &Value,
    correlation_id: &str,
    approvals: &std::sync::Arc<crate::services::agent_approval::RequestApprovals>,
) -> SkillExecuteRequest {
    let text = |key: &str| {
        arguments
            .get(key)
            .and_then(Value::as_str)
            .map(|v| v.trim().to_string())
    };
    SkillExecuteRequest {
        skill_id: None,
        skill_name: text("skill_name"),
        input_params: arguments.get("args").cloned(),
        group_id: arguments
            .get("group_id")
            .and_then(Value::as_str)
            .map(String::from),
        correlation_id: Some(correlation_id.to_string()),
        parent_execution_id: None,
        // The route read the call's tokens (headers and the
        // `approval_token` argument) into `approvals`.
        approval_token: None,
        approvals: approvals.clone(),
        data_scope_conflicts: Vec::new(),
        // The arguments reach the skill as the caller sent them.
        input_as_sent: None,
        input_view: crate::models::proxy::SkillInputView::Whole,
    }
}

/// Level 3, `skills__run_script`: run a named script, fully governed.
/// input guardrail + argument policy on args → authz/quota/approval via the
/// orchestrator → sandbox run (session workspace) → output guardrail → audit +
/// skill-usage attribution.
async fn handle_run_script(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    arguments: &Value,
    correlation_id: &str,
    auth_headers: &[(String, String)],
    run: &crate::services::run_controls::RunScope,
    approvals: &std::sync::Arc<crate::services::agent_approval::RequestApprovals>,
) -> Result<Value, JsonRpcError> {
    use constants::proxy_subtype::SKILL_RUN;
    use constants::system_server::agent_skill::TOOL_RUN_SCRIPT;
    let db = &state.db;
    let start = Instant::now();

    let skill_name = arguments
        .get("skill_name")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    let script = arguments
        .get("script")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    let script_args = arguments.get("args").cloned();

    if skill_name.is_empty() || script.is_empty() {
        return Ok(err_result(
            "skills__run_script requires 'skill_name' and 'script' (the script filename from the manifest).",
        ));
    }

    let auth_ctx = with_tenant(auth, server);
    let tenant_id = auth_ctx.tenant_id.clone();
    // The exact tools/call arguments — logged as the request body on every skill
    // row so a 403 block/deny shows WHAT was attempted, not just the error.
    let req_json = arguments.to_string();

    let skill =
        match skill_orchestrator::resolve_skill(db, &tenant_id, None, Some(skill_name)).await {
            Ok(s) => s,
            Err(e) => {
                return Ok(err_result(&format!(
                    "Skill '{skill_name}' not found: {}",
                    e.message
                )));
            }
        };

    let group = crate::services::usage::resolve_target_group(db, &auth_ctx, &tenant_id).await;
    let group_id: Option<String> = group.as_ref().map(|g| g.id.clone());

    // Manifest allowlist first: a script the skill does not allow is the
    // caller's request refused (403), so it is refused before any gate
    // admits the call against the group's limits or records an execution.
    match skill_runner_client::runnable_script(db, &skill, script).await {
        Ok(Ok(_)) => {}
        Ok(Err(refusal)) => {
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_RUN_SCRIPT,
                SKILL_RUN,
                Some(&skill.id),
                group_id.as_deref(),
                403,
                dur_ms(start),
                false,
                Some(&refusal.message),
                false,
                false,
                true,
                Some(&req_json),
                Some(&refusal.message),
            );
            return Ok(err_result(&refusal.message));
        }
        // Fail closed — the allowlist could not be read.
        Err(e) => {
            tracing::error!(error = %e, "skill script allowlist unreadable; refusing (fail closed)");
            let msg = "skill_unavailable: the gateway cannot read the skill's scripts";
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_RUN_SCRIPT,
                SKILL_RUN,
                Some(&skill.id),
                group_id.as_deref(),
                503,
                dur_ms(start),
                false,
                Some(msg),
                false,
                false,
                false,
                Some(&req_json),
                Some(msg),
            );
            return Ok(err_result(msg));
        }
    }

    // Semantic Governance Policy on the skill_input surface.
    //
    // The deprecated REST route (`routes/skills.rs`) has always evaluated this;
    // the canonical progressive-disclosure path, which is the one
    // agents actually use, never did. An enforcing policy that one route honours
    // and the other ignores is not enforcement, it is a coin flip decided by
    // which entry point the caller picked.
    if let Some(args_val) = script_args.as_ref() {
        use crate::services::semantic_policy::{self, PolicyContext, PolicyDecision};
        let content = serde_json::to_string(args_val).unwrap_or_default();
        if !content.is_empty() {
            let group_id = group.as_ref().map(|g| g.id.clone());
            let sgp_ctx = PolicyContext {
                surface: "skill_input".to_string(),
                actor_agent_id: auth_ctx.actor_agent_id.clone(),
                action_type: Some("skill_exec".to_string()),
                target: Some(skill_name.to_string()),
                subject: auth_ctx.gateway_user_id.clone(),
            };
            let outcome = semantic_policy::load_and_evaluate(
                &state.semantic_policy_cache,
                state.semantic_policy_judge.as_ref(),
                &state.db,
                &tenant_id,
                group_id.as_deref(),
                &content,
                &sgp_ctx,
                auth_headers,
                state.config.judge_model.as_deref(),
            )
            .await;
            let dur = start.elapsed().as_secs_f64() * 1000.0;
            for (pid, reason) in &outcome.shadow_hits {
                let log = crate::services::proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "skill",
                    "skills__run_script",
                    Some(skill_name),
                    group_id.clone(),
                    correlation_id,
                    dur,
                    pid,
                    false,
                    reason,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
            }
            if outcome.decision == PolicyDecision::Block {
                let pid = outcome.matched_policy_id.clone().unwrap_or_default();
                let reason = outcome.reason.clone().unwrap_or_default();
                let log = crate::services::proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "skill",
                    "skills__run_script",
                    Some(skill_name),
                    group_id.clone(),
                    correlation_id,
                    dur,
                    &pid,
                    true,
                    &reason,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
                // -32602 (invalid params): the arguments are what the policy
                // refused, and the JSON-RPC envelope has no richer code for a
                // governance refusal.
                return Err(JsonRpcError {
                    code: -32602,
                    message: format!("semantic_policy_blocked: {reason}"),
                });
            }
        }
    }

    // ---- INPUT guardrail on args (surface "skill") ----
    if let Some(args_val) = script_args.as_ref() {
        let mut leaves = Vec::new();
        crate::services::guardrails::body_redact::collect_string_leaves(args_val, &mut leaves);
        if !leaves.is_empty() {
            let configs = state
                .guardrails_cache
                .get_configs(
                    &tenant_id,
                    auth_ctx.guardrail_user_id().as_deref(),
                    auth_ctx.guardrail_group_id(),
                    db,
                )
                .await;
            let configs = match configs {
                Ok(c) => c,
                // Fail closed. Logged like a block so the refusal is in
                // the audit trail, not only in the tool result.
                Err(e) => {
                    tracing::error!(error = %e, "guardrail configs unavailable for skill input; refusing (fail closed)");
                    let msg =
                        "guardrails_unavailable: refusing to run a skill the gateway cannot check";
                    write_skill_layer_log(
                        state,
                        &auth_ctx,
                        correlation_id,
                        TOOL_RUN_SCRIPT,
                        SKILL_RUN,
                        Some(&skill.id),
                        group_id.as_deref(),
                        503,
                        dur_ms(start),
                        false,
                        Some(msg),
                        true,
                        false,
                        false,
                        Some(&req_json),
                        Some(msg),
                    );
                    return Ok(err_result(msg));
                }
            };
            if !configs.is_empty() {
                let combined = crate::services::guardrails::body_redact::join_for_detector(&leaves);
                if let crate::services::guardrails::CheckOutcome::Block(v) =
                    crate::services::guardrails::check_input(
                        &combined,
                        &configs,
                        "skill",
                        &state.http_client,
                    )
                    .await
                {
                    crate::services::proxy_metrics::record_guardrail_block("skill", &v.check);
                    let msg = format!(
                        "Skill input blocked by guardrail '{}' (check={}): {}",
                        v.config_name, v.check, v.message
                    );
                    write_skill_layer_log(
                        state,
                        &auth_ctx,
                        correlation_id,
                        TOOL_RUN_SCRIPT,
                        SKILL_RUN,
                        Some(&skill.id),
                        group_id.as_deref(),
                        403,
                        dur_ms(start),
                        false,
                        Some(&msg),
                        true,
                        v.check.to_ascii_lowercase().contains("pii"),
                        false,
                        Some(&req_json),
                        Some(&msg),
                    );
                    return Ok(err_result(&msg));
                }
            }
        }
    }

    // ---- ARGUMENT POLICY on args ----
    // Evaluated by the orchestrator below (`execute_skill`), on the group
    // the execution is authorised against, like every other way a skill
    // runs. It used to run here, where a failed
    // group lookup skipped it and a rule that asks for a person was
    // dropped.

    // ---- AUTHZ + APPROVAL HOLD + QUOTA (the orchestrator pipeline) ----
    let exec_req = run_script_request(arguments, correlation_id, approvals);
    let orch =
        // The MCP route admitted this tool call as the run's action.
        match skill_orchestrator::execute_skill(
            state,
            &auth_ctx,
            &exec_req,
            correlation_id,
            run,
            crate::services::run_controls::StepKind::PartOfAction,
        )
        .await
        {
            Ok(r) => r,
            Err(e) => {
                write_skill_layer_log(
                    state,
                    &auth_ctx,
                    correlation_id,
                    TOOL_RUN_SCRIPT,
                    // A governance refusal carries its own
                    // subtype so it is sealed `denied`, not a failed run.
                    e.refusal.unwrap_or(SKILL_RUN),
                    Some(&skill.id),
                    group_id.as_deref(),
                    e.status_code,
                    dur_ms(start),
                    false,
                    Some(&e.message),
                    false,
                    false,
                    true,
                    Some(&req_json),
                    Some(&e.message),
                );
                // A hold (the approval envelope) tells the caller which
                // approval to wait for and where to poll; without it the
                // caller knows only that it was stopped.
                let detail = match (&e.body, e.refusal) {
                    (Some(envelope), Some(constants::proxy_subtype::APPROVAL_REQUIRED)) => {
                        format!("{} {envelope}", e.message)
                    }
                    _ => e.message.clone(),
                };
                return Ok(err_result(&format!(
                    "skill_error_{}: {}",
                    e.status_code, detail
                )));
            }
        };
    // Gates passed; the script runs now. For a two-phase
    // system the `planned` record precedes the MCP row that is its outcome.
    crate::services::evidence::planned::dispatching(
        db,
        &tenant_id,
        Some(orch.group_id.as_str()),
        constants::proxy::MCP,
        TOOL_RUN_SCRIPT,
        None,
    );

    // ---- RUN the one named script in the session workspace (session = correlation) ----
    let (mut output, success, run_err) = match skill_runner_client::run_script(
        db,
        &skill,
        script,
        script_args.as_ref(),
        correlation_id,
    )
    .await
    {
        Ok(r) => (r.output, r.success, r.error),
        Err(e) => {
            let _ = skill_orchestrator::fail_execution(
                db,
                &orch.execution_id,
                &tenant_id,
                "skill_runner_error",
                &e,
            )
            .await;
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_RUN_SCRIPT,
                SKILL_RUN,
                Some(&skill.id),
                Some(orch.group_id.as_str()),
                502,
                dur_ms(start),
                false,
                Some(&e),
                false,
                false,
                false,
                Some(&req_json),
                Some(&e),
            );
            return Ok(err_result(&format!("Skill runner error: {e}")));
        }
    };

    // Settle the execution record.
    if success {
        let _ = skill_orchestrator::complete_execution(db, &orch.execution_id, &tenant_id).await;
    } else {
        let _ = skill_orchestrator::fail_execution(
            db,
            &orch.execution_id,
            &tenant_id,
            "script_error",
            &output,
        )
        .await;
    }

    // ---- OUTPUT guardrail (surface "skill") ----
    let pii;
    match guard_skill_output(state, &auth_ctx, &tenant_id, output.clone()).await {
        OutGuard::Blocked { check, message } => {
            crate::services::proxy_metrics::record_guardrail_block("skill", &check);
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_RUN_SCRIPT,
                SKILL_RUN,
                Some(&skill.id),
                Some(orch.group_id.as_str()),
                403,
                dur_ms(start),
                false,
                Some(&message),
                true,
                check.to_ascii_lowercase().contains("pii"),
                false,
                Some(&req_json),
                Some(&message),
            );
            return Ok(err_result(&message));
        }
        OutGuard::Ok { content, pii: p } => {
            output = content;
            pii = p;
        }
    }

    // ---- AUDIT (success) + skill-usage attribution (resource_type='skill') ----
    write_skill_layer_log(
        state,
        &auth_ctx,
        correlation_id,
        TOOL_RUN_SCRIPT,
        SKILL_RUN,
        Some(&skill.id),
        Some(orch.group_id.as_str()),
        200,
        dur_ms(start),
        success,
        run_err.as_deref(),
        false,
        pii,
        false,
        Some(&req_json),
        Some(&output),
    );
    crate::services::usage::write_skill_usage_log(
        &state.background_writer,
        state.db.clone(),
        &tenant_id,
        &auth_ctx,
        group.as_ref(),
        &orch.skill_id,
        &orch.skill_name,
        TOOL_RUN_SCRIPT,
        correlation_id,
        200,
        dur_ms(start),
    );

    Ok(json!({
        "content": [{"type": "text", "text": output}],
        "isError": !success,
    }))
}

/// Level 3, `skills__read_resource`: return a bundled reference file's
/// content (governed by access + manifest allowlist + output guardrail + audit).
async fn handle_read_resource(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    arguments: &Value,
    correlation_id: &str,
) -> Result<Value, JsonRpcError> {
    use constants::proxy_subtype::SKILL_READ;
    use constants::system_server::agent_skill::TOOL_READ_RESOURCE;
    let db = &state.db;
    let start = Instant::now();

    let skill_name = arguments
        .get("skill_name")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    let path = arguments
        .get("path")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    if skill_name.is_empty() || path.is_empty() {
        return Ok(err_result(
            "skills__read_resource requires 'skill_name' and 'path'.",
        ));
    }

    let auth_ctx = with_tenant(auth, server);
    let tenant_id = auth_ctx.tenant_id.clone();
    let req_json = arguments.to_string();

    if !caller_may_access_skill(db, &auth_ctx, skill_name).await? {
        let msg = format!("Access denied: skill '{skill_name}' is not available to your group.");
        write_skill_layer_log(
            state,
            &auth_ctx,
            correlation_id,
            TOOL_READ_RESOURCE,
            SKILL_READ,
            None,
            None,
            403,
            dur_ms(start),
            false,
            Some(&msg),
            false,
            false,
            true,
            Some(&req_json),
            Some(&msg),
        );
        return Ok(err_result(&msg));
    }

    let skill =
        match skill_orchestrator::resolve_skill(db, &tenant_id, None, Some(skill_name)).await {
            Ok(s) => s,
            Err(e) => {
                return Ok(err_result(&format!(
                    "Skill '{skill_name}' not found: {}",
                    e.message
                )));
            }
        };

    // Manifest allowlist + fetch in one — the (tenant, skill, filename) row IS
    // the allowlist; a path not in the skill simply doesn't resolve.
    let row: Option<(String, Option<String>)> = sqlx::query_as(
        r#"SELECT content, mime_type FROM agent_skill_resources
           WHERE tenant_id = $1 AND skill_id = $2 AND filename = $3"#,
    )
    .bind(&tenant_id)
    .bind(&skill.id)
    .bind(path)
    .fetch_optional(db)
    .await?;

    let (content, mime) = match row {
        Some(c) => c,
        None => {
            return Ok(err_result(&format!(
                "resource '{path}' is not part of skill '{skill_name}'"
            )));
        }
    };

    let group = crate::services::usage::resolve_target_group(db, &auth_ctx, &tenant_id).await;
    let group_id = group.as_ref().map(|g| g.id.as_str());

    let (content, pii) = match guard_skill_output(state, &auth_ctx, &tenant_id, content).await {
        OutGuard::Blocked { check, message } => {
            crate::services::proxy_metrics::record_guardrail_block("skill", &check);
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_READ_RESOURCE,
                SKILL_READ,
                Some(&skill.id),
                group_id,
                403,
                dur_ms(start),
                false,
                Some(&message),
                true,
                check.to_ascii_lowercase().contains("pii"),
                false,
                Some(&req_json),
                Some(&message),
            );
            return Ok(err_result(&message));
        }
        OutGuard::Ok { content, pii } => (content, pii),
    };

    write_skill_layer_log(
        state,
        &auth_ctx,
        correlation_id,
        TOOL_READ_RESOURCE,
        SKILL_READ,
        Some(&skill.id),
        group_id,
        200,
        dur_ms(start),
        true,
        None,
        false,
        pii,
        false,
        Some(&req_json),
        Some(&content),
    );

    Ok(json!({
        "content": [{"type": "text", "text": content, "mimeType": mime}],
        "isError": false,
    }))
}

/// Level 3, `skills__render_template`: render a bundled template/asset with
/// caller vars (governed by access + allowlist + output guardrail + audit).
async fn handle_render_template(
    state: &SharedState,
    server: &McpServer,
    auth: &AuthContext,
    arguments: &Value,
    correlation_id: &str,
) -> Result<Value, JsonRpcError> {
    use constants::proxy_subtype::SKILL_RENDER;
    use constants::system_server::agent_skill::TOOL_RENDER_TEMPLATE;
    let db = &state.db;
    let start = Instant::now();

    let skill_name = arguments
        .get("skill_name")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    let template = arguments
        .get("template")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .unwrap_or("");
    let vars = arguments.get("vars").cloned().unwrap_or(json!({}));
    if skill_name.is_empty() || template.is_empty() {
        return Ok(err_result(
            "skills__render_template requires 'skill_name' and 'template'.",
        ));
    }

    let auth_ctx = with_tenant(auth, server);
    let tenant_id = auth_ctx.tenant_id.clone();
    let req_json = arguments.to_string();

    if !caller_may_access_skill(db, &auth_ctx, skill_name).await? {
        let msg = format!("Access denied: skill '{skill_name}' is not available to your group.");
        write_skill_layer_log(
            state,
            &auth_ctx,
            correlation_id,
            TOOL_RENDER_TEMPLATE,
            SKILL_RENDER,
            None,
            None,
            403,
            dur_ms(start),
            false,
            Some(&msg),
            false,
            false,
            true,
            Some(&req_json),
            Some(&msg),
        );
        return Ok(err_result(&msg));
    }

    let skill =
        match skill_orchestrator::resolve_skill(db, &tenant_id, None, Some(skill_name)).await {
            Ok(s) => s,
            Err(e) => {
                return Ok(err_result(&format!(
                    "Skill '{skill_name}' not found: {}",
                    e.message
                )));
            }
        };

    let row: Option<(String,)> = sqlx::query_as(
        r#"SELECT content FROM agent_skill_assets
           WHERE tenant_id = $1 AND skill_id = $2 AND filename = $3"#,
    )
    .bind(&tenant_id)
    .bind(&skill.id)
    .bind(template)
    .fetch_optional(db)
    .await?;

    let template_content = match row {
        Some((c,)) => c,
        None => {
            return Ok(err_result(&format!(
                "template '{template}' is not part of skill '{skill_name}'"
            )));
        }
    };

    let rendered = render_template_vars(&template_content, &vars);

    let group = crate::services::usage::resolve_target_group(db, &auth_ctx, &tenant_id).await;
    let group_id = group.as_ref().map(|g| g.id.as_str());

    let (rendered, pii) = match guard_skill_output(state, &auth_ctx, &tenant_id, rendered).await {
        OutGuard::Blocked { check, message } => {
            crate::services::proxy_metrics::record_guardrail_block("skill", &check);
            write_skill_layer_log(
                state,
                &auth_ctx,
                correlation_id,
                TOOL_RENDER_TEMPLATE,
                SKILL_RENDER,
                Some(&skill.id),
                group_id,
                403,
                dur_ms(start),
                false,
                Some(&message),
                true,
                check.to_ascii_lowercase().contains("pii"),
                false,
                Some(&req_json),
                Some(&message),
            );
            return Ok(err_result(&message));
        }
        OutGuard::Ok { content, pii } => (content, pii),
    };

    write_skill_layer_log(
        state,
        &auth_ctx,
        correlation_id,
        TOOL_RENDER_TEMPLATE,
        SKILL_RENDER,
        Some(&skill.id),
        group_id,
        200,
        dur_ms(start),
        true,
        None,
        false,
        pii,
        false,
        Some(&req_json),
        Some(&rendered),
    );

    Ok(json!({
        "content": [{"type": "text", "text": rendered}],
        "isError": false,
    }))
}

/// Minimal, dependency-free `{{var}}` substitution for template rendering.
/// Supports `{{key}}` and `{{ key }}`; string values inline verbatim, others
/// via their JSON form.
fn render_template_vars(template: &str, vars: &Value) -> String {
    let mut out = template.to_string();
    if let Some(obj) = vars.as_object() {
        for (k, v) in obj {
            let val = match v {
                Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            out = out.replace(&format!("{{{{{k}}}}}"), &val);
            out = out.replace(&format!("{{{{ {k} }}}}"), &val);
        }
    }
    out
}

// =============================================================================
// Response Builders
// =============================================================================

struct JsonRpcError {
    code: i32,
    message: String,
}

/// A database error is a JSON-RPC internal error: the skill catalog,
/// manifest and access checks used to answer "no skills" or "not part of
/// this skill" instead, which the model then believed.
impl From<sqlx::Error> for JsonRpcError {
    #[track_caller]
    fn from(e: sqlx::Error) -> Self {
        crate::services::db_errors::record(&e);
        JsonRpcError {
            code: -32603,
            message: "The skills store could not be read; retry".to_string(),
        }
    }
}

fn build_jsonrpc_response(
    jsonrpc_id: Option<&Value>,
    result: &Value,
    mcp_method: Option<String>,
    mcp_request_id: Option<String>,
    mcp_capability_name: Option<String>,
) -> SystemServerResponse {
    let body = serde_json::to_vec(&json!({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "result": result,
    }))
    .unwrap_or_default();

    SystemServerResponse {
        status_code: 200,
        body: body.clone(),
        headers: standard_headers(body.len()),
        mcp_method,
        mcp_request_id,
        mcp_capability_name,
    }
}

fn build_jsonrpc_error(
    jsonrpc_id: Option<&Value>,
    code: i32,
    message: &str,
) -> SystemServerResponse {
    let body = serde_json::to_vec(&json!({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "error": {"code": code, "message": message},
    }))
    .unwrap_or_default();

    // JSON-RPC errors still use HTTP 200
    SystemServerResponse {
        status_code: 200,
        body: body.clone(),
        headers: standard_headers(body.len()),
        mcp_method: None,
        mcp_request_id: None,
        mcp_capability_name: None,
    }
}

fn standard_headers(body_len: usize) -> Vec<(String, String)> {
    vec![
        ("content-type".to_string(), "application/json".to_string()),
        ("content-length".to_string(), body_len.to_string()),
        (
            constants::headers::X_SYSTEM_MCP_SERVER.to_string(),
            "true".to_string(),
        ),
    ]
}

#[cfg(test)]
mod skill_annotation_contract {
    //! The Agent Skills verbs' MCP annotations are pinned to a fixture the
    //! control plane's `VERB_TOOLS` test reads too, so the stored copy (which
    //! the gateway classifies risk from) and the live `tools/list` agree.
    use super::*;

    const FIXTURE: &str = include_str!("../../tests/fixtures/system_tool_annotations.json");

    struct FakeSkill;
    impl SkillInfo for FakeSkill {
        fn name(&self) -> &str {
            "affordability-check"
        }
        fn description(&self) -> &str {
            "demo"
        }
        fn metadata(&self) -> Option<&Value> {
            None
        }
    }

    fn fixture() -> serde_json::Map<String, Value> {
        let v: Value = serde_json::from_str(FIXTURE).expect("fixture parses");
        v["agent_skills"]
            .as_object()
            .expect("agent_skills section")
            .clone()
    }

    #[test]
    fn every_generic_verb_carries_the_pinned_annotations() {
        let want = fixture();
        let tools = skills_generic_tool_definitions();
        assert_eq!(tools.len(), 5, "update the fixture when a verb is added");
        for t in &tools {
            let name = t["name"].as_str().unwrap();
            assert_eq!(
                t.get("annotations"),
                want.get(name),
                "{name}: annotations drifted from tests/fixtures/system_tool_annotations.json"
            );
        }
    }

    #[test]
    fn a_per_skill_tool_is_a_read_like_skills_load() {
        let t = skill_to_tool_definition(&FakeSkill);
        assert_eq!(t.get("annotations"), fixture().get("skill__<name>"));
    }

    #[test]
    fn only_run_script_acts() {
        use crate::services::agent_authz::tool_risk_attributes;
        for t in skills_generic_tool_definitions() {
            let name = t["name"].as_str().unwrap().to_string();
            let risk = tool_risk_attributes(t.get("annotations"));
            let read_only = risk.iter().any(|r| r == "read_only");
            assert_eq!(
                read_only,
                name != constants::system_server::agent_skill::TOOL_RUN_SCRIPT,
                "{name}: {risk:?}"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn skill_tool(name: &str) -> Value {
        json!({ "name": name, "description": "", "inputSchema": {} })
    }

    fn allowed(names: &[&str]) -> std::collections::HashSet<String> {
        names.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn retain_keeps_only_assigned_skills() {
        let tools = vec![
            skill_tool("skill__sprint-planner"),
            skill_tool("skill__hello-world"),
            skill_tool("skill__skill-creator"),
        ];
        let kept = retain_allowed_skill_tools(&tools, &allowed(&["sprint-planner"]));
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0]["name"], "skill__sprint-planner");
    }

    #[test]
    fn retain_empty_allowed_drops_every_skill_tool() {
        let tools = vec![
            skill_tool("skill__sprint-planner"),
            skill_tool("skill__hello-world"),
        ];
        let kept = retain_allowed_skill_tools(&tools, &allowed(&[]));
        assert!(kept.is_empty());
    }

    #[test]
    fn retain_passes_through_non_skill_tools() {
        // Tools without the `skill__` prefix are not skills and must
        // never be dropped by skill-assignment filtering.
        let tools = vec![
            skill_tool("search__web"),
            skill_tool("skill__sprint-planner"),
        ];
        let kept = retain_allowed_skill_tools(&tools, &allowed(&[]));
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0]["name"], "search__web");
    }

    // -----------------------------------------------------------------------
    // Progressive disclosure (L1/L2)
    // -----------------------------------------------------------------------
    use constants::system_server::agent_skill as ask;

    #[test]
    fn skill_name_from_tool_ignores_generic_skills_tools() {
        // The plural generic tools must never parse as a per-skill entry.
        assert_eq!(ask::skill_name_from_tool("skills__load"), None);
        assert_eq!(ask::skill_name_from_tool("skills__run_script"), None);
        assert_eq!(ask::skill_name_from_tool("skills__read_resource"), None);
        // A real per-skill entry still parses.
        assert_eq!(
            ask::skill_name_from_tool("skill__hello-world"),
            Some("hello-world")
        );
    }

    #[test]
    fn parse_load_target_generic_loader_reads_skill_name_arg() {
        let args = json!({"skill_name": "revenue-report"});
        assert_eq!(
            parse_load_target(ask::TOOL_LOAD, &args).as_deref(),
            Some("revenue-report")
        );
        // Client-prefixed variant (e.g. Goose "brutor__skills__load").
        assert_eq!(
            parse_load_target("brutor__skills__load", &args).as_deref(),
            Some("revenue-report")
        );
    }

    #[test]
    fn parse_load_target_generic_loader_missing_name_is_none() {
        assert_eq!(parse_load_target(ask::TOOL_LOAD, &json!({})), None);
        // Blank / whitespace-only is rejected.
        assert_eq!(
            parse_load_target(ask::TOOL_LOAD, &json!({"skill_name": "   "})),
            None
        );
    }

    #[test]
    fn parse_load_target_per_skill_entry_is_a_loader() {
        // A per-skill tool is equivalent to loading that skill (the arguments
        // are irrelevant for load).
        assert_eq!(
            parse_load_target("skill__hello-world", &json!({})).as_deref(),
            Some("hello-world")
        );
        // Client-prefixed per-skill entry.
        assert_eq!(
            parse_load_target("brutorskills__skill__hello-world", &json!({})).as_deref(),
            Some("hello-world")
        );
    }

    #[test]
    fn parse_load_target_l3_and_unknown_tools_are_none() {
        // L3 act tools are NOT load targets (handled by the L3 layer).
        assert_eq!(parse_load_target(ask::TOOL_RUN_SCRIPT, &json!({})), None);
        assert_eq!(parse_load_target(ask::TOOL_READ_RESOURCE, &json!({})), None);
        assert_eq!(
            parse_load_target(ask::TOOL_RENDER_TEMPLATE, &json!({})),
            None
        );
        // Genuinely unknown tools.
        assert_eq!(parse_load_target("search__web", &json!({})), None);
        assert_eq!(parse_load_target("nonsense", &json!({})), None);
    }

    #[test]
    fn initialize_result_carries_instructions_body() {
        let init = initialize_result("HELLO-INSTRUCTIONS");
        assert_eq!(
            init.get("instructions").and_then(|v| v.as_str()),
            Some("HELLO-INSTRUCTIONS")
        );
    }

    #[test]
    fn instructions_text_is_preamble_plus_scoped_catalog() {
        // Option B: the L1 preamble points at skills__load, and the scoped
        // catalog (name: description) is appended — skills are NOT tools.
        let catalog = vec![
            ("hello-world".to_string(), "greets you back".to_string()),
            (
                "revenue-report".to_string(),
                "runs a read-only report".to_string(),
            ),
        ];
        let text = build_instructions_text(&catalog);
        assert!(text.starts_with(ask::INITIALIZE_INSTRUCTIONS));
        assert!(text.contains("skills__load"));
        assert!(text.contains("- hello-world: greets you back"));
        assert!(text.contains("- revenue-report: runs a read-only report"));
    }

    #[test]
    fn instructions_text_empty_catalog_says_none() {
        let text = build_instructions_text(&[]);
        assert!(text.starts_with(ask::INITIALIZE_INSTRUCTIONS));
        assert!(text.contains("No skills"));
    }

    #[test]
    fn tools_list_advertises_only_generic_verbs() {
        // Option B: skills are NOT tools — tools/list is exactly the 4 verbs,
        // no skill__<name> entries.
        let tools = skills_generic_tool_definitions();
        let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
        assert_eq!(names.len(), 5); // list + load + read_resource + run_script + render_template
        assert!(names.contains(&ask::TOOL_LIST));
        assert!(names.iter().all(|n| n.starts_with("skills__")));
        assert!(!names.iter().any(|n| n.starts_with("skill__")));
    }

    #[test]
    fn load_tool_definition_shape() {
        let t = skills_load_tool_definition();
        assert_eq!(t["name"], ask::TOOL_LOAD);
        assert_eq!(t["inputSchema"]["required"][0], "skill_name");
    }

    #[test]
    fn l2_preamble_directs_l3_through_brutor() {
        let p = ask_preamble();
        // Must instruct the model to run scripts / read resources / render
        // templates through the governed tools — not a local shell.
        assert!(p.contains("skills__run_script"));
        assert!(p.contains("skills__read_resource"));
        assert!(p.contains("skills__render_template"));
        assert!(p.contains("sandbox"));
    }

    // -----------------------------------------------------------------------
    // L3 tool surface
    // -----------------------------------------------------------------------

    #[test]
    fn generic_tools_advertise_full_l1_l2_l3_surface() {
        let tools = skills_generic_tool_definitions();
        let names: Vec<&str> = tools.iter().filter_map(|t| t["name"].as_str()).collect();
        assert!(names.contains(&ask::TOOL_LIST));
        assert!(names.contains(&ask::TOOL_LOAD));
        assert!(names.contains(&ask::TOOL_READ_RESOURCE));
        assert!(names.contains(&ask::TOOL_RUN_SCRIPT));
        assert!(names.contains(&ask::TOOL_RENDER_TEMPLATE));
        // run_script requires skill_name + script.
        let run = tools
            .iter()
            .find(|t| t["name"] == ask::TOOL_RUN_SCRIPT)
            .unwrap();
        let req = run["inputSchema"]["required"].as_array().unwrap();
        assert!(req.iter().any(|v| v == "skill_name"));
        assert!(req.iter().any(|v| v == "script"));
    }

    #[test]
    fn l3_tools_are_not_load_targets() {
        // The L3 act tools must be routed by handle_tools_call's own arms, never
        // parsed as an L2 load target.
        for t in [
            ask::TOOL_RUN_SCRIPT,
            ask::TOOL_READ_RESOURCE,
            ask::TOOL_RENDER_TEMPLATE,
        ] {
            assert_eq!(parse_load_target(t, &json!({"skill_name": "x"})), None);
        }
    }

    #[test]
    fn render_template_vars_substitutes_both_brace_styles() {
        let tmpl = "Q{{quarter}} report for {{ team }} — total {{count}}";
        let vars = json!({"quarter": "3", "team": "Sales", "count": 42});
        let out = render_template_vars(tmpl, &vars);
        assert_eq!(out, "Q3 report for Sales — total 42");
    }

    #[test]
    fn render_template_vars_leaves_unknown_placeholders() {
        let out = render_template_vars("{{a}} and {{missing}}", &json!({"a": "X"}));
        assert_eq!(out, "X and {{missing}}");
    }
}

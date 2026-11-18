// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # MCP Proxy Routes (Model Context Protocol)
//!
//! Acts as an intelligent gateway between LLMs and external tools/data sources.
//!
//! ## Key Features
//! - **Dual-Mode Proxying**: Detects and handles both JSON-RPC (stateful) and REST (stateless) calls.
//! - **Credential Injection**: Automatically attaches OAuth Bearer tokens to upstream tool calls.
//! - **Header Sanitization**: Strips step-by-step HTTP headers to prevent request smuggling.
//! - **Security Capping**: Enforces strict payload limits (10MB) to protect against OOM attacks.
//!
//! ## Route Layout
//!
//! Two client-facing paths share the same dispatch pipeline. The choice between
//! them is part of the public contract — clients should pick deliberately.
//!
//! ### `/v1/proxy/mcp/{server_id}` — canonical client URL
//!
//! The standard entrypoint for talking to an MCP server through the gateway:
//! JSON-RPC method calls (`initialize`, `tools/list`, `tools/call`, …) and
//! Streamable HTTP (SSE) responses. The proxy automatically appends the
//! server's configured `mcp_endpoint_path` (set at registration time) before
//! forwarding upstream — clients **must not** include it in the URL themselves,
//! or they will hit a doubled path on the upstream and typically receive 404/405.
//!
//! ### `/v1/proxy/mcp/{server_id}/{*path}` — sub-path
//!
//! Used for two specific cases, distinguished by the first character of the path:
//!
//! 1. **OAuth discovery (primary use case)** — paths starting with `.` (e.g.
//!    `.well-known/oauth-protected-resource`, `.well-known/oauth-authorization-server`)
//!    are rewritten to hit the server's **origin**, dropping `mcp_endpoint_path`
//!    entirely. Required by [RFC 8414] (Authorization Server Metadata) and
//!    [RFC 9728] (Protected Resource Metadata) — discovery metadata MUST live at
//!    the origin, not under a nested API path. An MCP server with
//!    `base_url=https://example.com/api/mcp` and `mcp_endpoint_path=/mcp`
//!    publishes its metadata at `https://example.com/.well-known/...`,
//!    never at `…/api/mcp/.well-known/...`.
//!
//! 2. **REST-style MCP servers** — any other sub-path is appended to `base_url`
//!    verbatim. Escape hatch for MCP implementations that expose plain HTTP
//!    endpoints rather than (or alongside) the standard JSON-RPC entrypoint.
//!    Rarely needed; canonical JSON-RPC clients should use `/{server_id}`.
//!
//! Exact dispatch rules live in [`build_target_url`].
//!
//! [RFC 8414]: https://www.rfc-editor.org/rfc/rfc8414
//! [RFC 9728]: https://www.rfc-editor.org/rfc/rfc9728

use std::time::Instant;

use axum::Router;
use axum::body::Body;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, Method, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{any, get};
use serde_json::{Value, json};

use crate::constants;
use crate::middleware::tenant::TenantContext;
use crate::models::auth::AuthType;
use crate::models::db::McpServer;
use crate::services::a2a_delegation::{
    ABSOLUTE_MAX_DEPTH, CallChain, DelegationError, TraceContinuity, derive_tenant_key,
    parse_or_mint,
};
use crate::services::proxy_logger::{self, body_preview, extract_user_agent};
use crate::services::run_controls::RunScope;
use crate::services::{
    SharedState, agent_approval, auth, guardrails, proxy_metrics, system_server, usage,
};

/// Header getter for `parse_or_mint`'s closure-shaped contract. Mirrors
/// the same helper in `routes/llm.rs` and `routes/a2a.rs`.
fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned)
}

/// Tag this row's chain root with what initiated it. MCP tools/call
/// from an LLM tool-call decision should INHERIT the LLM's chain (not
/// mint a new root) — so this returns `None` for non-root spans by
/// design; only the very first step carries the tag.
fn root_kind_for_auth(auth_type: &AuthType, is_root: bool) -> Option<&'static str> {
    if !is_root {
        return None;
    }
    match auth_type {
        AuthType::GatewayJwt => Some("llm_user"),
        AuthType::ApiKey => Some("agent_invocation"),
        _ => Some("external_api"),
    }
}

/// MCP proxy router.
///
/// Two client-facing paths share one dispatch pipeline:
///
/// * `/{server_id}` — canonical JSON-RPC + Streamable HTTP entrypoint.
///   The proxy appends the server's configured `mcp_endpoint_path`
///   before forwarding upstream.
/// * `/{server_id}/{*path}` — OAuth discovery (`.well-known/*`) and
///   REST-style sub-paths. See [`build_target_url`] for dispatch rules.
///
/// Both use `any()` so the same handler accepts the full MCP method
/// matrix (POST for JSON-RPC, GET for the optional SSE listener
/// channel, DELETE for session teardown).
///
/// Full contract is documented in the module-level docs at the top of
/// this file and on the [`proxy_root`] / [`proxy_with_path`]
/// `utoipa::path` blocks (rendered by Swagger UI).
pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/health", get(health))
        .route("/{server_id}", any(proxy_root))
        .route("/{server_id}/", any(proxy_root))
        .route("/{server_id}/{*path}", any(proxy_with_path))
}

#[utoipa::path(
    method(post, get, put, delete),
    path = "/v1/proxy/mcp/{server_id}/{path}",
    tag = "MCP",
    summary = "MCP server proxy (sub-path: OAuth discovery and REST passthrough)",
    description = r#"
Proxies requests to a **sub-path** on a registered MCP server.

> ⚠ For normal JSON-RPC + Streamable HTTP calls, use the canonical endpoint
> `POST /v1/proxy/mcp/{server_id}` — the gateway appends the server's configured
> `mcp_endpoint_path` itself, so clients **must not** include it in the URL.

This endpoint exists for **two specific use cases**, picked apart by the first
character of `{path}`:

### 1. OAuth discovery (primary use case)

Paths beginning with `.` (e.g. `.well-known/oauth-protected-resource`,
`.well-known/oauth-authorization-server`) are rewritten to hit the MCP
server's **origin**, *dropping* the configured `mcp_endpoint_path`.

This is mandated by:
- [RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) — OAuth 2.0 Authorization
  Server Metadata
- [RFC 9728](https://www.rfc-editor.org/rfc/rfc9728) — OAuth 2.0 Protected
  Resource Metadata

Discovery metadata is a **resource-level** concept and must live at the server
origin. For an MCP server with `base_url=https://example.com/api/mcp` and
`mcp_endpoint_path=/mcp`, the discovery document is fetched from
`https://example.com/.well-known/oauth-protected-resource` — never from
`…/api/mcp/.well-known/...`.

**Examples:**
- `GET /v1/proxy/mcp/{server_id}/.well-known/oauth-protected-resource`
- `GET /v1/proxy/mcp/{server_id}/.well-known/oauth-authorization-server`

### 2. REST-style MCP servers (escape hatch)

Any other sub-path is appended to `base_url` verbatim. Used by non-standard
MCP implementations that expose plain HTTP endpoints rather than (or in
addition to) the JSON-RPC entrypoint. Rarely needed — modern Streamable HTTP
clients should target `/v1/proxy/mcp/{server_id}` instead.

**Example:** `GET /v1/proxy/mcp/{server_id}/tools/list-rest`

### Required headers

| Header | Value | Notes |
|---|---|---|
| `Authorization` | `Bearer sk_brutor_api_…` | Group-scoped API key |
| `X-Tenant-ID` | `<tenant-slug>` | Required for tenant routing |
| `Accept` | `application/json, text/event-stream` | Streamable HTTP requires **both** |
| `Content-Type` | `application/json` | For JSON-RPC bodies |
"#,
    params(
        ("server_id" = String, Path, description = "MCP server ID (e.g. `mcp-01KR…`)"),
        ("path" = String, Path, description = "Sub-path. Starts with `.` → OAuth discovery (rewritten to server origin). Otherwise → appended to `base_url` verbatim."),
    ),
    responses(
        (status = 200, description = "MCP server response (JSON or SSE stream)"),
        (status = 401, description = "Authentication required"),
        (status = 403, description = "Access denied — user's group does not have access to this MCP server"),
        (status = 404, description = "MCP server not found or disabled"),
        (status = 413, description = "Request body over the MCP payload limit (`data.max_payload_size_mb`, never above 10 MiB)"),
        (status = 429, description = "MCP usage limit exceeded — a quota, a frequency limit or a concurrency allowance (JSON-RPC error, `error.data.limit_type`), or the limits cannot be read (`limits_unavailable`)"),
        (status = 502, description = "The MCP server's answer is over the payload limit (JSON-RPC `payload_too_large`) or broke off"),
        (status = 504, description = "The MCP server did not answer within the call's timeout"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn proxy_with_path(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    method: Method,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path((server_id, path)): Path<(String, String)>,
    body: Body,
) -> Response {
    proxy_request(
        state,
        method,
        headers,
        tenant,
        &server_id,
        Some(&path),
        body,
        client_addr,
    )
    .await
}

#[utoipa::path(
    method(post, get, put, delete),
    path = "/v1/proxy/mcp/{server_id}",
    tag = "MCP",
    summary = "MCP server proxy (canonical client URL)",
    description = r#"
**Canonical entrypoint** for talking to a registered MCP server through the
gateway. This is the URL that MCP clients should use for normal operation.

### What it does

- Proxies JSON-RPC methods (`initialize`, `tools/list`, `tools/call`,
  `prompts/list`, `resources/read`, …).
- Supports Streamable HTTP transport — the gateway negotiates between a one-shot
  JSON response and an SSE stream based on the `Accept` header.
- Injects credentials. If the MCP server is registered with `auth_type=oauth`
  and a refresh token, the gateway attaches a fresh Bearer token to the upstream
  call. Clients do **not** need to manage MCP-server credentials themselves.
- Enforces group access and usage quotas (call count, daily budget).
- Logs every call to `proxy_logs` with tenant, group, method, capability,
  upstream latency, and result.

### Endpoint-path handling

The gateway **automatically appends** the server's configured `mcp_endpoint_path`
(set at registration time) when forwarding to the upstream. Clients **must not**
include it in the URL — doing so produces a doubled path on the upstream and
typically returns 404/405.

Example: a server registered with `base_url=https://upstream.example.com`
and `mcp_endpoint_path=/mcp` is reached at:

| Client sends             | Gateway forwards to             |
|--------------------------|---------------------------------|
| `/v1/proxy/mcp/{id}`     | `https://upstream.example.com/mcp` |

### Required headers

| Header | Value | Notes |
|---|---|---|
| `Authorization` | `Bearer sk_brutor_api_…` | Group-scoped API key |
| `X-Tenant-ID` | `<tenant-slug>` | Required for tenant routing |
| `Accept` | `application/json, text/event-stream` | Streamable HTTP requires **both** — sending only `application/json` may produce 405/406 |
| `Content-Type` | `application/json` | For JSON-RPC bodies |

### Related endpoints

- For OAuth discovery (`.well-known/*`) or REST-style sub-paths, see
  `POST /v1/proxy/mcp/{server_id}/{path}`.

### Example: initialize handshake

```bash
curl -X POST "https://gateway.example.com/v1/proxy/mcp/{server_id}" \
  -H "Authorization: Bearer sk_brutor_api_…" \
  -H "X-Tenant-ID: my-tenant" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "clientInfo": {"name": "my-client", "version": "1.0"},
      "capabilities": {}
    }
  }'
```
"#,
    params(
        ("server_id" = String, Path, description = "MCP server ID (e.g. `mcp-01KR…`)"),
    ),
    responses(
        (status = 200, description = "MCP server response (JSON or SSE stream)"),
        (status = 401, description = "Authentication required"),
        (status = 403, description = "Access denied — user's group does not have access to this MCP server"),
        (status = 404, description = "MCP server not found or disabled"),
        (status = 413, description = "Request body over the MCP payload limit (`data.max_payload_size_mb`, never above 10 MiB)"),
        (status = 429, description = "MCP usage limit exceeded — a quota, a frequency limit or a concurrency allowance (JSON-RPC error, `error.data.limit_type`), or the limits cannot be read (`limits_unavailable`)"),
        (status = 502, description = "The MCP server's answer is over the payload limit (JSON-RPC `payload_too_large`) or broke off"),
        (status = 504, description = "The MCP server did not answer within the call's timeout"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn proxy_root(
    State(state): State<SharedState>,
    client_addr: crate::routes::request::ClientIp,
    method: Method,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(server_id): Path<String>,
    body: Body,
) -> Response {
    proxy_request(
        state,
        method,
        headers,
        tenant,
        &server_id,
        None,
        body,
        client_addr,
    )
    .await
}

async fn proxy_request(
    state: SharedState,
    method: Method,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    server_id: &str,
    path: Option<&str>,
    body: Body,
    client_addr: crate::routes::request::ClientIp,
) -> Response {
    let start = Instant::now();
    let correlation_id = headers
        .get(crate::constants::headers::X_CORRELATION_ID)
        .and_then(|v| v.to_str().ok())
        .map(String::from)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());

    tracing::debug!(
        server_id,
        method = %method,
        path,
        correlation_id = %correlation_id,
        "MCP proxy_request entered"
    );

    let tenant_id = match tenant {
        Some(t) => t.tenant_id.clone(),
        None => {
            tracing::debug!("MCP proxy_request: missing tenant context");
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "Missing tenant context"})),
            )
                .into_response();
        }
    };

    // Establish the call chain at MCP request
    // entry. When an LLM tool-call drove this MCP call, the four
    // `x-brutor-delegation-*` headers will be present on the inbound
    // and HMAC-verified here; otherwise we mint a fresh root (rare for
    // MCP traffic, since MCP calls are almost always agent-initiated).
    // The resulting trace_root_task_id ties this row to the upstream
    // LLM span and any subsequent MCP/skill steps.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, current_task_id, trace_continuity) =
        parse_or_mint(|n| header_value(&headers, n), &tenant_key);
    let current_task_id_str = current_task_id.to_string();

    // Extend the chain by one step before forwarding,
    // mirroring the A2A route (`a2a.rs`). Only a VERIFIED inbound chain is
    // forked; a minted / client-dropped / spoofed chain stays a fresh root at
    // depth 0. `call_chain` itself remains THIS step's chain for proxy-log
    // attribution; `outgoing_chain` is what we emit downstream. Forking also
    // enforces the absolute depth cap here — a chain already at the cap is
    // rejected before any upstream send, so the MCP surface can't be used to
    // bypass the delegation-depth limit that A2A enforces.
    let outgoing_chain = if trace_continuity == TraceContinuity::Verified {
        match call_chain.fork_child(current_task_id) {
            Ok(child) => child,
            Err(DelegationError::DepthOverflow { depth }) => {
                tracing::warn!(
                    depth,
                    cap = ABSOLUTE_MAX_DEPTH,
                    server_id,
                    "MCP delegation chain exceeds absolute depth cap — rejected"
                );
                return error_response(
                    StatusCode::FORBIDDEN,
                    &format!(
                        "Delegation chain exceeds absolute depth cap ({} > {})",
                        depth, ABSOLUTE_MAX_DEPTH
                    ),
                );
            }
            Err(e) => {
                tracing::error!(error = %e, server_id, "MCP: fork_child unexpectedly failed");
                return error_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Delegation chain construction failed",
                );
            }
        }
    } else {
        call_chain.clone()
    };

    // Auth (MCP endpoints support anonymous)
    let mut auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        client_addr.ip(),
        false, // MCP allows anonymous
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };
    if let Err(e) = crate::services::identity_policy::judge_call(&state, &mut auth_ctx).await {
        return error_response(e.status_code(), &e.to_string());
    }

    let trace_root_kind = root_kind_for_auth(&auth_ctx.auth_type, call_chain.depth == 0);

    // MCP server access check — resolves every group that grants the caller
    // access to this server, once.
    //
    // This used to call `resolve_mcp_server_access` here for the
    // billing group and `resolve_all_mcp_server_groups` again further down for
    // the capability-filter merge. The two run the SAME membership query and
    // the SAME per-group chain walk, so a user in four groups paid for both on
    // every tools/call. They are also exactly equivalent — the billing group is
    // the first granting group, in every branch including the
    // "nothing configured, so treat as open" fallback. That equivalence is
    // pinned by tests/integration/mcp_group_resolution.rs against a real
    // database, so it cannot quietly stop being true.
    let granting_groups =
        match usage::resolve_all_mcp_server_groups(&state.db, &auth_ctx, &tenant_id, server_id)
            .await
        {
            Ok(g) => g,
            // Not "you do not have access" (the 403 below).
            Err(e) => return crate::services::db_errors::unavailable("MCP server access", &e),
        };
    let target_group = granting_groups.first().cloned();
    tracing::debug!(
        server_id,
        has_access = target_group.is_some(),
        group = ?target_group.as_ref().map(|g| &g.name),
        "MCP server access check"
    );
    if target_group.is_none() {
        return error_response(
            StatusCode::FORBIDDEN,
            "Access denied: you do not have access to this MCP server",
        );
    }

    // Resolve server_config_id for per-server limit enforcement
    // A failed read is a 503: read as "no server config", the
    // per-server limits below were silently not enforced.
    let server_config_id: Option<String> = {
        let row: Result<Option<(String,)>, sqlx::Error> = sqlx::query_as(
            "SELECT id FROM server_configs WHERE mcp_server_id = $1 AND tenant_id = $2 AND enabled = true LIMIT 1",
        )
        .bind(server_id)
        .bind(&tenant_id)
        .fetch_optional(&state.db)
        .await;
        match row {
            Ok(r) => r.map(|(id,)| id),
            Err(e) => {
                return crate::services::db_errors::unavailable(
                    "the MCP server's configuration",
                    &e,
                );
            }
        }
    };

    // Usage quota enforcement — hierarchical ancestor budgets. Per-call
    // rate/quota limits (minute/hour/day/burst/cooldown, group/server/tool
    // scope) are enforced separately once the tool name is known.
    //
    // Admitted under a hold, as are the per-call rate limits below: both are
    // committed when the call is dispatched upstream, and given back (the
    // guards' Drop) if anything in between refuses it — a call that never
    // ran counts toward no limit.
    let mut mcp_holds: Vec<crate::services::cache::usage_state::HoldGuard> = Vec::new();
    let mut quota_hold = crate::services::limits::PendingHold::of(
        crate::services::limits::HoldId::new(&tenant_id, "mcp-quota"),
    );
    let enforcement = usage::enforce_mcp_quota_counted(
        &state.db,
        &tenant_id,
        target_group.as_ref(),
        Some(state.usage_state.as_ref()),
        Some(&mut quota_hold),
    )
    .await;
    // A guard for what was admitted, and only for that.
    mcp_holds.extend(quota_hold.held().map(|h| state.usage_state.adopt_hold(h)));

    if enforcement.blocked {
        let duration = start.elapsed().as_secs_f64() * 1000.0;
        tracing::warn!(
            group_id = ?enforcement.group_id,
            group_name = ?enforcement.group_name,
            limit_type = ?enforcement.limit_type,
            message = ?enforcement.message,
            "Usage limit exceeded — 429 returned"
        );
        proxy_metrics::record_request("mcp", method.as_str(), 429, "n/a");

        let mut log = proxy_logger::build_proxy_log(
            "mcp",
            &auth_ctx,
            method.as_str(),
            "mcp-proxy",
            None,
            None,
            429,
            duration,
            false,
            enforcement.message.as_deref(),
            &correlation_id,
        );
        log.user_agent = extract_user_agent(&headers);
        log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
        proxy_logger::apply_call_chain(
            &mut log,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
        );
        // Note: this audit row fires BEFORE server resolution
        // (rate-limit gate at request entry), so the residency
        // check hasn't run yet. residency_class stays NULL on
        // this row — distinct from "not_evaluated" which means
        // "checked but no profile was set".
        state
            .background_writer
            .write_proxy_log(state.db.clone(), log);

        return usage_limit_response(&enforcement);
    }

    // Lookup MCP server
    let server: Option<McpServer> = match sqlx::query_as(
        "SELECT * FROM mcp_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(server_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(s) => s,
        Err(e) => return crate::services::db_errors::unavailable("the MCP server", &e),
    };

    let server = match server {
        Some(s) => {
            tracing::debug!(
                server_id,
                server_name = %s.name,
                base_url = %s.base_url,
                "MCP server found"
            );
            // Tenant-configured destination: an IP-literal base URL is
            // judged here (hyper never resolves those); hostnames are
            // judged by the policy resolver on the shared client.
            if crate::services::outbound_policy::check_mcp_server_url(&s.base_url).is_err() {
                return error_response(
                    StatusCode::FORBIDDEN,
                    "MCP server destination is refused by the outbound policy",
                );
            }
            s
        }
        None => {
            tracing::debug!(server_id, tenant_id = %tenant_id, "MCP server not found or disabled");
            return error_response(StatusCode::NOT_FOUND, "MCP server not found or disabled");
        }
    };

    // The tenant's "MCP servers" switch (`tenant_features`) governs the
    // servers it registered; the gateway's own system servers answer to the
    // switches of what they serve (agent skills: `execute_skill`).
    if !constants::system_server::is_system_server(&server.base_url)
        && let Err(sentence) = crate::services::tenant_features::require(
            &state,
            &tenant_id,
            crate::services::tenant_features::Feature::McpServers,
        )
        .await
    {
        return match sentence {
            Some(msg) => error_response(StatusCode::FORBIDDEN, msg),
            None => error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "The tenant's features could not be read right now",
            ),
        };
    }

    // =========================================================================
    // Data residency check
    // =========================================================================
    //
    // Load the tenant's residency profile and intersect with this
    // server's region. Three outcomes:
    //
    //   * No profile               → `residency_decision = None`;
    //                                downstream audit rows stamp
    //                                `residency_class = not_evaluated`
    //                                and the request proceeds.
    //
    //   * Allowed                  → `residency_decision = Some(Allowed)`;
    //                                downstream audit rows stamp
    //                                `region_routed_to + class=allowed`.
    //
    //   * Blocked                  → write a single audit row with
    //                                `proxy_subtype=residency_violation`
    //                                + `residency_class=blocked`, then
    //                                return 403 region_not_allowed.
    //                                The request never touches the
    //                                upstream MCP server.
    //
    // Loading the profile is one indexed PK lookup per request. No
    // caching layer yet — promote to TenantCache if the audit
    // dashboard's RPS grows.
    let residency_profile =
        crate::services::residency::load_tenant_profile(&state.db, &tenant_id).await;
    let residency_decision =
        crate::services::residency::check(residency_profile.as_ref(), server.region.as_deref());
    let server_region_for_audit = server.region.clone();

    if let Some(ref d) = residency_decision {
        if let crate::services::residency::ResidencyDecision::Blocked { reason } = d {
            tracing::warn!(
                tenant_id = %tenant_id,
                server_id = %server.id,
                server_region = ?server.region,
                %reason,
                "MCP residency block — refusing 403",
            );
            let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&server.base_url),
                Some(server_id),
                403,
                duration_ms,
                false,
                Some(reason),
                &correlation_id,
            );
            log.proxy_subtype =
                Some(crate::constants::proxy_subtype::RESIDENCY_VIOLATION.to_string());
            log.user_agent = extract_user_agent(&headers);
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            proxy_logger::apply_residency_audit(&mut log, d);
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return error_response(
                StatusCode::FORBIDDEN,
                &format!("region_not_allowed: {reason}"),
            );
        }
    }

    // =========================================================================
    // Early gate: short-circuit non-MCP GETs and known-non-SSE servers
    // =========================================================================
    //
    // A POST with a JSON-RPC body is the only inbound shape that
    // counts as a real MCP request. The Streamable-HTTP transport
    // also defines an OPTIONAL listener stream (`GET` returning
    // `text/event-stream`) that the SDK opens to receive
    // server-initiated events. In practice we see two kinds of
    // junk arrive on this path:
    //
    //   1. A browser tab pasted with the proxy URL. Chrome
    //      revalidates aggressively (304/200 alternating) every
    //      few seconds. Each revalidation is a `GET` with
    //      `Accept: text/html,…` — never `text/event-stream`.
    //      It cannot be a legitimate MCP listener and must not
    //      be forwarded upstream (let alone counted as MCP
    //      usage).
    //
    //   2. An MCP SDK reopening the listener stream against a
    //      server that never speaks it (`Accept: text/event-stream`
    //      but the upstream returns HTML / 405 / etc.). The SDK
    //      retries forever; we cache the determination after the
    //      first probe and short-circuit subsequent attempts so
    //      the loop collapses.
    //
    // Both branches return 405 Method Not Allowed — the
    // documented signal for an SDK to stop reopening the listener
    // — and skip ALL proxy writes (no proxy_log, no proxy_metric,
    // no usage_log). The point is that under steady-state these
    // requests should produce zero gateway side-effects beyond
    // the 405 response itself.
    if method == axum::http::Method::GET {
        // (1) Pure browser noise — no event-stream Accept.
        if !accept_includes_event_stream(&headers) {
            tracing::debug!(
                server_id = %server.id,
                "MCP gate: GET without text/event-stream Accept — short-circuit 405"
            );
            return error_response(
                StatusCode::METHOD_NOT_ALLOWED,
                "MCP proxy: GET requires Accept: text/event-stream. Use POST for JSON-RPC requests.",
            );
        }

        // (2) Persistent flag from the DB — the
        //     `wants_listener` column on `mcp_servers` was set
        //     by the last capability discovery based on what
        //     the server's `initialize` response declared.
        //     Survives restarts; doesn't need a probe to warm.
        //     This is the canonical answer to "does this
        //     server speak the listener channel?".
        if !server.wants_listener_stream() {
            tracing::debug!(
                server_id = %server.id,
                server_name = %server.name,
                "MCP gate: server.wants_listener=false — short-circuit 405"
            );
            return error_response(
                StatusCode::METHOD_NOT_ALLOWED,
                "Upstream MCP server does not support the SSE listener channel (per its initialize-response capabilities). Use POST for JSON-RPC requests.",
            );
        }

        // (3) In-process negative cache — covers servers we
        //     haven't yet re-probed (still flagged
        //     `wants_listener=true` in the DB but observed
        //     non-event-stream on the wire). Honour the TTL so
        //     a server that *gains* listener support
        //     self-heals on the next probe.
        if let Some(entry) = state.non_sse_mcp_servers.get(&server.id) {
            if entry.elapsed() < NON_SSE_CACHE_TTL {
                tracing::debug!(
                    server_id = %server.id,
                    server_name = %server.name,
                    cached_for_secs = entry.elapsed().as_secs(),
                    "MCP gate: server cached as non-SSE — short-circuit 405"
                );
                return error_response(
                    StatusCode::METHOD_NOT_ALLOWED,
                    "Upstream MCP server does not support the SSE listener channel. Use POST for JSON-RPC requests.",
                );
            } else {
                // TTL expired — drop the entry so the next path
                // probes again. We don't drop inline above because
                // dashmap doesn't allow remove-while-holding-ref.
                drop(entry);
                state.non_sse_mcp_servers.remove(&server.id);
            }
        }
    }

    // =========================================================================
    // The call's MCP limits, read once
    // =========================================================================
    // The frequency limits (tools/call) and the execution limits (timeout,
    // payload, concurrency) all come from this one read. A limit that cannot
    // be read refuses the call (fail closed).
    let limits_row = || McpRowCtx {
        state: &state,
        auth_ctx: &auth_ctx,
        headers: &headers,
        method: method.as_str(),
        target_url: &server.base_url,
        server_id,
        group_id: target_group.as_ref().map(|g| g.id.clone()),
        correlation_id: &correlation_id,
        start,
        call_chain: &call_chain,
        current_task_id_str: &current_task_id_str,
        trace_continuity,
        trace_root_kind,
    };
    let mcp_limit_sources = match target_group.as_ref() {
        Some(group) => match usage::load_mcp_limit_sources(
            &state.db,
            group,
            server_id,
            server_config_id.as_deref(),
        )
        .await
        {
            Ok(sources) => sources,
            Err(e) => {
                return refuse_mcp_limit(
                    limits_row(),
                    &crate::services::limits::config_unavailable_block(&e),
                    None,
                    None,
                    None,
                );
            }
        },
        None => crate::services::mcp_limits::McpLimitSources::default(),
    };
    let exec_limits = match crate::services::mcp_limits::exec_limits(&mcp_limit_sources) {
        Ok(l) => l,
        Err(e) => {
            return refuse_mcp_limit(
                limits_row(),
                &crate::services::mcp_limits::unreadable_block(&e),
                None,
                None,
                None,
            );
        }
    };

    // =========================================================================
    // System server interception
    // =========================================================================
    // System servers (http://system/*) are virtual — handle internally instead
    // of forwarding to a real HTTP server.
    if constants::system_server::is_system_server(&server.base_url) {
        return handle_system_server(
            &state,
            &method,
            &headers,
            &auth_ctx,
            &server,
            &tenant_id,
            target_group.as_ref(),
            body,
            server_id,
            path,
            &correlation_id,
            &enforcement,
            start,
            &call_chain,
            &current_task_id_str,
            trace_continuity,
            trace_root_kind,
            &residency_decision,
            &server_region_for_audit,
            mcp_holds,
            &mcp_limit_sources,
            &exec_limits,
        )
        .await;
    }

    // Build target URL
    let target_url = build_target_url(&server, path);
    tracing::debug!(target_url = %target_url, "MCP target URL built");

    // Prepare headers for upstream
    let mut upstream_headers = reqwest::header::HeaderMap::new();
    for (key, value) in headers.iter() {
        let key_str = key.as_str().to_lowercase();

        // Strip step-by-step headers (RFC 9110) plus a few proxy-local
        // headers that must never cross the boundary:
        //   * `origin` — browser's notion of the requesting origin;
        //     upstream servers interpret it against *their* origin
        //     and can reject legitimate proxied calls.
        //   * `x-gateway-authorization` / `x-tenant-id` — our own
        //     internal-only envelope.
        //   * `accept-encoding` — reqwest handles compression
        //     negotiation; forwarding the browser's value can leave
        //     the upstream emitting encodings reqwest won't decode.
        if matches!(
            key_str.as_str(),
            "host"
                | "connection"
                | "keep-alive"
                | "transfer-encoding"
                | "te"
                | "trailer"
                | "upgrade"
                | "proxy-authorization"
                | "proxy-connection"
                | "origin"
                | "x-gateway-authorization"
                | "x-tenant-id"
                | "x-brutor-capability"
                // The CALLER's credentials, which must never
                // cross this boundary.
                //
                // This loop copies every inbound header except the ones named
                // here, and `authorization` was not among them. It is
                // overwritten further down only when the server has an OAuth
                // token or a static `auth_token` of its own; a server with
                // neither — any public or unauthenticated MCP server — was
                // sent the caller's `Authorization: Bearer sk_brutor_api_…`
                // verbatim. `x-api-key` was worse: never stripped and never
                // overwritten, so it went upstream on every single call.
                //
                // A Brutor API key is not a request detail, it is the caller's
                // whole governance scope: whoever holds it can spend that
                // resource group's budget and reach its models and tools. It
                // must not be handed to a third party we are merely proxying
                // to. Stripped unconditionally here, so the injection below is
                // the only thing that can set an upstream credential.
                | "authorization"
                | "x-api-key"
                // Gateway-internal routing and authorisation, meaningless
                // upstream and misleading if echoed back.
                | "x-resource-group"
                | "x-approval-token"
                | "accept-encoding"
                // `content-length` MUST be recomputed by reqwest from the
                // body we actually send: input guardrails (PII / banned-
                // pattern redaction) can rewrite the JSON-RPC body in place
                // (see `body_bytes = ...` below), changing its length. If we
                // forward the client's original Content-Length, the upstream
                // waits for byte-count that never arrives (redaction grew or
                // shrank the body) and the call hangs until the 30s timeout,
                // surfacing as a spurious 502. Dropping it lets reqwest set
                // the correct length for the (possibly redacted) body.
                | "content-length"
        ) {
            continue;
        }

        // Fetch Metadata headers (`Sec-Fetch-Site`, `Sec-Fetch-Mode`,
        // `Sec-Fetch-Dest`, `Sec-Fetch-User`, …) describe the browser's
        // fetch context from the *direct* request's perspective.
        // Forwarding them to a downstream server means that server
        // interprets `Sec-Fetch-Site: same-site` (which was true
        // between `portal.brutor.ai` and `api.brutor.ai`) against its
        // own origin — and rightly concludes the request is
        // cross-site. MCP servers with DNS-rebinding protection then
        // reject with "cross-origin request detected from Sec-Fetch-Site
        // header" and the user sees a spurious 403.
        //
        // These headers are strictly browser→direct-recipient metadata;
        // a proxy that forwards them is forwarding a lie. Prefix-match
        // so we also drop any future `Sec-Fetch-*` additions browsers
        // introduce.
        if key_str.starts_with("sec-fetch-") {
            continue;
        }

        // W3C `baggage` is caller-controlled context the
        // gateway read for the call's release; it is not ours to hand to a
        // third party.
        if crate::services::agent_impl::is_caller_only_header(&key_str) {
            continue;
        }

        if let Ok(name) = reqwest::header::HeaderName::from_bytes(key.as_ref()) {
            upstream_headers.insert(name, value.clone());
        }
    }

    enforce_mcp_outbound_accept(&mut upstream_headers);

    // Stitch the inbound MCP call into
    // the cross-surface chain by emitting the four HMAC-signed
    // delegation headers on the upstream send. No task-local needed
    // here — the reqwest builder is constructed in this function so
    // we can write the headers inline.
    for (k, v) in outgoing_chain.to_header_pairs(&tenant_key) {
        if let Ok(value) = reqwest::header::HeaderValue::from_str(&v) {
            upstream_headers.insert(reqwest::header::HeaderName::from_static(k), value);
        }
    }

    // Inject upstream OAuth token.
    // Priority: 1) Stored OAuth token (via MCP JWT) 2) Static auth_token from mcp_servers table
    let mut injected_token = false;

    // Try to decode MCP JWT from Authorization header and look up stored OAuth token
    if let Some(bearer) = headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
    {
        // An MCP JWT speaks for one tenant and one
        // server: the pair the OAuth facade minted it for. It was verified
        // here and then used for whichever server the route named, so a JWT
        // for server X sent to `/v1/proxy/mcp/{Y}` handed the user's upstream
        // token for X to Y's upstream (and a JWT of another tenant selected
        // this tenant's stored credential of the same subject string). A JWT
        // for anything but this tenant and this route's server is not a
        // credential here: nothing is injected and the audit row is not
        // stamped with its IdP user. The refresh, revoke and list routes
        // compare the same two claims (`routes/oauth.rs`).
        let claims = state
            .oauth_token_manager
            .verify_mcp_jwt(bearer)
            .ok()
            .filter(|claims| {
                let bound = claims.tenant_id == tenant_id && claims.mcp_server_id == server_id;
                if !bound {
                    tracing::warn!(
                        server_id,
                        jwt_mcp_server_id = %claims.mcp_server_id,
                        "MCP JWT was not issued for this tenant and server — ignored"
                    );
                }
                bound
            });
        if let Some(claims) = claims {
            // Enrich auth context with IdP user info from MCP JWT (for proxy logging)
            auth_ctx.idp_user_id = Some(claims.sub.clone());
            auth_ctx.idp_username = claims.username.clone();
            auth_ctx.idp_email = claims.email.clone();

            tracing::debug!(
                idp_user_id = %claims.sub,
                idp_username = ?claims.username,
                mcp_server_id = %claims.mcp_server_id,
                "MCP JWT decoded, looking up stored OAuth token"
            );
            if let Some(oauth_token) = state
                .oauth_token_manager
                .get_valid_token(
                    &state.db,
                    auth_ctx.gateway_user_id.as_deref(),
                    &claims.sub,
                    &claims.mcp_server_id,
                    &tenant_id,
                    // The credential of the IdP
                    // this JWT's subject belongs to, and of no other.
                    &claims.idp_id,
                    true, // auto_refresh
                )
                .await
            {
                tracing::debug!("Injecting stored OAuth token for upstream request");
                // A value read from the database must not be able to
                // panic the handler. A token carrying a newline or a non-ASCII
                // byte (a truncated write, a mis-keyed decryption) is not a
                // valid header value, and `.unwrap()` here took the request task
                // down. Refusing is the right direction: dropping the header
                // instead would forward the call UNAUTHENTICATED, which an
                // permissive upstream might well accept.
                let Ok(value) = format!("Bearer {oauth_token}").parse() else {
                    tracing::error!(
                        server_id = %server_id,
                        "stored OAuth token is not a valid HTTP header value — refusing to forward"
                    );
                    return error_response(
                        StatusCode::BAD_GATEWAY,
                        "Stored OAuth credential for this server is malformed; re-authorize it",
                    );
                };
                upstream_headers.insert(reqwest::header::AUTHORIZATION, value);
                injected_token = true;
                // Broker-side on-behalf-of evidence: if an agent principal drove
                // this brokered call, stamp it on the token row.
                if let Some(agent_id) = auth_ctx.actor_agent_id.as_deref() {
                    state
                        .oauth_token_manager
                        .record_agent_use(
                            &state.db,
                            &tenant_id,
                            &claims.sub,
                            &claims.mcp_server_id,
                            &claims.idp_id,
                            agent_id,
                        )
                        .await;
                }
            } else {
                // No usable stored OAuth token. The caller's MCP JWT
                // validated — they *did* OAuth at some point — but the
                // access token is now expired/revoked and either no
                // refresh token was stored or the refresh failed
                // (oauth_token_manager already raised a downstream_idp
                // alert in that case).
                //
                // Historical behaviour was to silently fall through to
                // the mcp_servers.auth_token static fallback, which
                // meant we'd forward the request with the wrong
                // credentials and the upstream would return a bare 401
                // or 403 that the portal had no structured way to
                // react to. The portal then surfaced GitHub's raw
                // "Forbidden" string to the user with no re-auth path.
                //
                // New behaviour: return 401 with a stable error code
                // the portal can detect and use to kick off the
                // OAuth re-auth flow for this specific MCP server.
                // `WWW-Authenticate` header is added per RFC 6750 so
                // non-portal clients (CLI, scripts) also get a
                // standards-compliant signal.
                tracing::warn!(
                    server_id,
                    idp_user_id = %claims.sub,
                    mcp_server_id = %claims.mcp_server_id,
                    idp_id = ?server.idp_id,
                    "OAuth token unusable — returning 401 oauth_reauth_required"
                );
                let body = json!({
                    "error": "oauth_reauth_required",
                    "error_description": "OAuth token expired or revoked — please re-authenticate with the Identity Provider",
                    "mcp_server_id": claims.mcp_server_id,
                    "mcp_server_name": server.name,
                    "idp_id": server.idp_id,
                });
                let mut resp = (StatusCode::UNAUTHORIZED, Json(body)).into_response();
                resp.headers_mut().insert(
                    reqwest::header::WWW_AUTHENTICATE,
                    "Bearer realm=\"brutor-mcp\", error=\"invalid_token\", \
                     error_description=\"OAuth token expired or revoked\""
                        .parse()
                        .unwrap(),
                );
                return resp;
            }
        }
    }

    // The server's static token, for a server whose `auth_type` is
    // `bearer` (`McpServer::static_token` yields none for any other: a
    // `none` or `oauth` server is never sent a stored static token). Only
    // reached when the request carried no valid MCP JWT. The
    // OAuth-with-unusable-token case returned 401 above.
    if !injected_token {
        match server.static_token(&state.encryption) {
            Ok(None) => {}
            Ok(Some(token)) => {
                // Same reasoning as the OAuth branch above.
                let Ok(value) = format!("Bearer {token}").parse() else {
                    tracing::error!(
                        server_id = %server_id,
                        "configured auth token is not a valid HTTP header value — refusing to forward"
                    );
                    return error_response(
                        StatusCode::BAD_GATEWAY,
                        "Configured auth token for this server is malformed; re-enter it",
                    );
                };
                upstream_headers.insert(reqwest::header::AUTHORIZATION, value);
            }
            // A stored value that is not a ciphertext the gateway can open
            // (one that does not decrypt, or clear text) is not a token.
            // Forwarding without it would call a server that expects a
            // credential with none; forwarding it would send a ciphertext
            // as the bearer. The call is refused and nothing is sent. The
            // log line names the row, never the value.
            Err(e) => {
                tracing::error!(
                    tenant_id = %tenant_id,
                    server_id = %server_id,
                    "stored MCP server token is not a ciphertext this gateway can open ({e}); \
                     the server is refused until its token is saved again under this \
                     ENCRYPTION_KEY"
                );
                return error_response(
                    StatusCode::BAD_GATEWAY,
                    "Configured auth token for this server cannot be read; re-enter it",
                );
            }
        }
    }

    // Background capability discovery for OAuth servers that haven't been discovered yet.
    // This catches cases where the token was stored in a previous session (before discovery
    // code existed) or via refresh_token, so the token exchange didn't trigger discovery.
    if injected_token
        && (!super::oauth::has_capabilities(&server) || super::oauth::capabilities_expired(&server))
    {
        let db = state.db.clone();
        let _http_client = state.http_client.clone();
        let server_clone = server.clone();
        // Extract the OAuth token we just injected
        let oauth_token = upstream_headers
            .get(reqwest::header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .map(String::from);

        if let Some(token) = oauth_token {
            let state_clone = state.clone();
            tokio::spawn(async move {
                tracing::debug!(
                    mcp_server_id = %server_clone.id,
                    mcp_server_name = %server_clone.name,
                    "Background: discovering capabilities for OAuth MCP server"
                );
                match super::oauth::discover_capabilities(&state_clone, &server_clone, &token).await {
                    Ok(discovery) => {
                        if let Err(e) = sqlx::query(
                            "UPDATE mcp_servers SET capabilities = $1, wants_listener = $2, \
                             protocol_version = $3, dialect = $4, discovered_at = NOW() WHERE id = $5",
                        )
                        .bind(&discovery.capabilities)
                        .bind(discovery.wants_listener)
                        .bind(&discovery.protocol_version)
                        .bind(&discovery.dialect)
                        .bind(&server_clone.id)
                        .execute(&db)
                        .await
                        {
                            tracing::warn!(
                                mcp_server_id = %server_clone.id,
                                error = %e,
                                "Background: failed to store discovered capabilities"
                            );
                        } else {
                            tracing::debug!(
                                mcp_server_id = %server_clone.id,
                                tools = discovery.capabilities.get("tools").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
                                resources = discovery.capabilities.get("resources").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
                                prompts = discovery.capabilities.get("prompts").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
                                resource_templates = discovery.capabilities.get("resource_templates").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0),
                                wants_listener = discovery.wants_listener,
                                "Background: capabilities discovered and stored"
                            );
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            mcp_server_id = %server_clone.id,
                            error = %e,
                            "Background: capability discovery failed"
                        );
                    }
                }
            });
        }
    }

    // Read request body. Mutable because the input-guardrail
    // Redacted path may replace it with a rewritten body before
    // forwarding upstream (PII / banned-pattern redaction on
    // MCP tool-call arguments).
    let body_limit = exec_limits.request_body_limit();
    let mut body_bytes = match axum::body::to_bytes(body, body_limit).await {
        Ok(b) => b,
        Err(_) => {
            return refuse_request_payload(
                McpRowCtx {
                    state: &state,
                    auth_ctx: &auth_ctx,
                    headers: &headers,
                    method: method.as_str(),
                    target_url: &target_url,
                    server_id,
                    group_id: target_group.as_ref().map(|g| g.id.clone()),
                    correlation_id: &correlation_id,
                    start,
                    call_chain: &call_chain,
                    current_task_id_str: &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                },
                body_limit,
            );
        }
    };

    // The ONE reading of the body: what it
    // is, the parsed message every control below reads, and the bytes that
    // are forwarded all come from `jsonrpc_body::read`. A body an upstream
    // might execute as JSON-RPC that cannot be read as one message is
    // refused here, never forwarded.
    let mut parsed_body: Option<Value>;
    let is_mcp;
    match crate::services::jsonrpc_body::read(&body_bytes) {
        crate::services::jsonrpc_body::RpcBody::Message { value, bytes } => {
            // What was judged is what is sent: without a BOM.
            if bytes.len() != body_bytes.len() {
                body_bytes = body_bytes.slice_ref(bytes);
            }
            parsed_body = Some(value);
            is_mcp = true;
        }
        crate::services::jsonrpc_body::RpcBody::Batch => {
            tracing::warn!(
                tenant_id = %tenant_id,
                server_id = %server_id,
                "refused a JSON-RPC batch on the MCP proxy"
            );
            return batch_refused();
        }
        crate::services::jsonrpc_body::RpcBody::Unreadable(reason) => {
            tracing::warn!(
                tenant_id = %tenant_id,
                server_id = %server_id,
                reason,
                "refused a body that cannot be read as one JSON-RPC message"
            );
            return unreadable_refused(reason);
        }
        crate::services::jsonrpc_body::RpcBody::Other(value) => {
            parsed_body = value;
            is_mcp = false;
        }
    }

    // Debug: log request body and MCP detection
    if tracing::enabled!(tracing::Level::DEBUG) {
        let body_str = String::from_utf8_lossy(&body_bytes);
        let preview = if body_str.len() > 500 {
            &body_str[..500]
        } else {
            &body_str
        };
        tracing::debug!(
            is_mcp,
            body_len = body_bytes.len(),
            body_preview = %preview,
            method = %method,
            "MCP request body"
        );
    }

    // Guardrails — check MCP input
    let guardrail_configs = match state
        .guardrails_cache
        .get_configs(
            &tenant_id,
            auth_ctx.guardrail_user_id().as_deref(),
            auth_ctx.guardrail_group_id(),
            &state.db,
        )
        .await
    {
        Ok(c) => c,
        Err(e) => {
            // Fail closed.
            //
            // The cache serves the last known good set on a load failure, so
            // reaching this arm means nothing was cached at all. We cannot
            // tell whether this tenant has guardrails configured, and "we do
            // not know" must not be served as "there are none" — that is how
            // a call gets a clean audit row saying it was checked when
            // nothing ran.
            tracing::error!(error = %e, "guardrail configs unavailable; refusing the call");
            return refuse_unchecked_mcp(
                McpRowCtx {
                    state: &state,
                    auth_ctx: &auth_ctx,
                    headers: &headers,
                    method: method.as_str(),
                    target_url: &target_url,
                    server_id,
                    group_id: target_group.as_ref().map(|g| g.id.clone()),
                    correlation_id: &correlation_id,
                    start,
                    call_chain: &call_chain,
                    current_task_id_str: &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                },
                "guardrails_unavailable: refusing to proxy a call we cannot check",
            );
        }
    };

    // Narrow the config set by per-call scope. Extract
    // the current tool name from `tools/call` requests so configs
    // pinned to `(server_id, tool_name)` only fire on the right tool.
    // For non-call methods (`tools/list`, `initialize`, etc.) the
    // tool_name is None and only server-level scope filters apply.
    //
    // Parse the JSON-RPC envelope once and pull BOTH the inner
    // method (`tools/call`, `tools/list`, …) and the params.name
    // (the tool name on a tools/call). The earlier version gated
    // on `method == "tools/call"` where `method` is the *HTTP*
    // method — that comparison is always false for a real request,
    // so `current_tool_name` was permanently None and per-tool
    // scope filters never fired. We also use `mcp_inner_method`
    // for the blocked-path audit row so the Capability column
    // reads "tools/call" instead of "POST".
    // (`parsed_body` is the one reading made above.)

    // SEP-2243 — reject requests whose Mcp-Method / Mcp-Name
    // headers contradict the body: 400 + JSON-RPC -32020. Absent headers
    // (legacy clients) pass untouched; a caller must never be able to
    // show header-routing infrastructure a different action than the
    // body actually performs.
    if let Some(pb) = parsed_body.as_ref() {
        if let Some(reason) = crate::services::mcp_wire::header_body_mismatch(&headers, pb) {
            let id = pb.get("id").cloned();
            return (
                StatusCode::BAD_REQUEST,
                axum::Json(crate::services::mcp_wire::header_mismatch_error(
                    id.as_ref(),
                    &reason,
                )),
            )
                .into_response();
        }
    }

    // Era bridge: legacy client → stateless-only upstream.
    // Discovery stamped this upstream `dialect = 'stateless'`; it has no
    // `initialize` and rejects any request missing the 2026-07-28 `_meta`
    // fields / MCP-Protocol-Version header. The gateway answers the
    // handshake chatter itself and upgrades every forwarded request to
    // the modern dialect (modern clients pass through untouched).
    if server.is_stateless_upstream() && is_mcp {
        if let Some(pb) = parsed_body.as_mut() {
            let inner = pb
                .get("method")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            let rpc_id = pb.get("id").cloned();
            match inner.as_str() {
                "initialize" => {
                    let client_pv = pb
                        .pointer("/params/protocolVersion")
                        .and_then(|v| v.as_str());
                    let result = crate::services::mcp_wire::legacy_initialize_result(
                        client_pv,
                        &server.name,
                        server.capabilities.as_ref(),
                    );
                    tracing::debug!(
                        server_id,
                        "era bridge: synthesized legacy initialize for stateless upstream"
                    );
                    return (
                        StatusCode::OK,
                        axum::Json(serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "result": result
                        })),
                    )
                        .into_response();
                }
                // Streamable-HTTP notifications get 202 Accepted; there is
                // no upstream session to notify.
                "notifications/initialized" => {
                    return StatusCode::ACCEPTED.into_response();
                }
                // `ping` was removed in the modern era — answer locally.
                "ping" => {
                    return (
                        StatusCode::OK,
                        axum::Json(serde_json::json!({
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "result": {}
                        })),
                    )
                        .into_response();
                }
                _ => {
                    if crate::services::mcp_wire::ensure_modern_meta(pb) {
                        if let Ok(bytes) = serde_json::to_vec(pb) {
                            body_bytes = bytes::Bytes::from(bytes);
                        }
                    }
                }
            }
        }
        // Required on every POST in the stateless dialect. Overwrites a
        // legacy client's absent/older value; a modern client's header is
        // the same constant.
        upstream_headers.insert(
            reqwest::header::HeaderName::from_static(
                crate::constants::mcp_protocol::headers::PROTOCOL_VERSION,
            ),
            reqwest::header::HeaderValue::from_static(crate::constants::mcp_protocol::V2026_07_28),
        );
    }

    // Mirror the verified body onto the SEP-2243 routing
    // headers for the upstream send (no-op when the client already sent
    // them; ignored by legacy upstreams).
    if let Some(pb) = parsed_body.as_ref() {
        crate::services::mcp_wire::inject_routing_headers(&mut upstream_headers, pb);

        // SEP-414: MCP 2026-07-28 carries W3C trace
        // context in `_meta` (unprefixed keys, an explicit spec
        // exception). Mirror onto the upstream HTTP headers so OTel
        // spans stitch across the gateway even for HTTP-level tracing.
        if let Some(meta) = pb.pointer("/params/_meta") {
            for key in ["traceparent", "tracestate"] {
                if let Some(v) = meta.get(key).and_then(|v| v.as_str()) {
                    if !upstream_headers.contains_key(key) {
                        if let (Ok(name), Ok(val)) = (
                            reqwest::header::HeaderName::from_bytes(key.as_bytes()),
                            reqwest::header::HeaderValue::from_str(v),
                        ) {
                            upstream_headers.insert(name, val);
                        }
                    }
                }
            }
        }
    }

    let mcp_inner_method: Option<String> = parsed_body
        .as_ref()
        .and_then(|p| p.get("method"))
        .and_then(|m| m.as_str())
        .map(String::from);
    let current_tool_name: Option<String> = if mcp_inner_method.as_deref() == Some("tools/call") {
        parsed_body
            .as_ref()
            .and_then(|p| p.get("params"))
            .and_then(|p| p.get("name"))
            .and_then(|n| n.as_str())
            .map(String::from)
    } else {
        None
    };

    // Which definition of the tool is in force, and which server
    // version announced it. Recorded once here; every audit row this
    // request produces (blocked, parked or forwarded) picks it up.
    if let Some(tool) = current_tool_name.as_deref() {
        let prov = crate::services::tool_provenance::lookup(
            &state.db,
            &auth_ctx.tenant_id,
            &server.id,
            tool,
        )
        .await;
        crate::middleware::run_end::set_tool_provenance(prov);
    }

    // ── Per-call MCP rate limiting ──────────────────────────────────────
    // Token bucket + fixed-window quota + cooldown at group / server /
    // tool scope, on the Redis-backed UsageState. Runs only for
    // tools/call — protocol chatter (initialize, tools/list, …) does not
    // consume rate budget. Checked before guardrails so a rate-limited
    // client never reaches the (more expensive) scanning machinery.
    // The `mcp-rate` hold when it carries concurrency slots: it outlives
    // dispatch (in `McpInFlight`) and ends when the call is over.
    let mut slot_hold: Option<crate::services::cache::usage_state::HoldGuard> = None;
    if mcp_inner_method.as_deref() == Some("tools/call") {
        if let (Some(tool), Some(group)) = (current_tool_name.as_deref(), target_group.as_ref()) {
            let mut rate_hold = crate::services::limits::PendingHold::of(
                crate::services::limits::HoldId::new(&tenant_id, "mcp-rate"),
            );
            // The concurrency allowances ride the same admission as the
            // frequency limits: one atomic step, no round trip when unset.
            let slots = exec_limits.slot_dims(
                &group.tenant_id,
                &group.id,
                server_id,
                &crate::services::mcp_limits::principal_key(&auth_ctx),
            );
            let carries_slots = !slots.is_empty();
            let block = usage::admit_mcp_rate_limits(
                &state.usage_state,
                group,
                server_id,
                tool,
                &mcp_limit_sources,
                slots,
                Some(&mut rate_hold),
            )
            .await;
            let held = rate_hold.held().map(|h| state.usage_state.adopt_hold(h));
            if carries_slots {
                slot_hold = held;
            } else {
                mcp_holds.extend(held);
            }
            if let Some(block) = block {
                let duration = start.elapsed().as_secs_f64() * 1000.0;
                tracing::warn!(
                    server_id,
                    tool,
                    scope = block.scope,
                    limit_type = block.limit_type,
                    retry_after_secs = block.retry_after_secs,
                    "MCP rate limit exceeded — 429 returned"
                );
                proxy_metrics::record_request("mcp", method.as_str(), 429, "n/a");

                let mut log = proxy_logger::build_proxy_log(
                    "mcp",
                    &auth_ctx,
                    method.as_str(),
                    "mcp-proxy",
                    Some(server.base_url.as_str()),
                    Some(server.id.as_str()),
                    429,
                    duration,
                    false,
                    Some(&block.message),
                    &correlation_id,
                );
                log.user_agent = extract_user_agent(&headers);
                log.resource_group_id = Some(group.id.clone());
                log.proxy_subtype = Some(format!("rate_limit_{}", block.scope));
                log.mcp_method = Some("tools/call".to_string());
                log.mcp_capability_name = Some(tool.to_string());
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        server_region_for_audit.as_deref(),
                    ),
                }
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                let rpc_id = parsed_body.as_ref().and_then(|v| v.get("id").cloned());
                return mcp_rate_limit_response(&block, rpc_id.as_ref());
            }
        }
    }

    let guardrail_configs = guardrails::GuardrailsCache::filter_by_call_scope(
        guardrail_configs.into_vec(),
        Some(server_id),
        current_tool_name.as_deref(),
    );

    // Set when an input guardrail
    // detected a PII category on a PASSED request (Warn or Redact
    // action, not Block). Stamped on the success audit row below so
    // the compliance tagger derives HIPAA PHI / GDPR PII even when
    // the call wasn't blocked — mirrors llm.rs `input_pii_redacted`.
    let mut input_pii_detected = false;

    if !guardrail_configs.is_empty() && !body_bytes.is_empty() {
        // Extract text content from MCP request for guardrail
        // checking. For MCP JSON-RPC we walk every string leaf
        // under `params` (recursive) so the detector sees only
        // operator-supplied content, not JSON syntax characters,
        // AND so a Redacted outcome can splice each leaf's
        // replacement back into its original path. For non-MCP
        // (raw HTTP) the body is one opaque text blob — no
        // leaves to track.
        let (input_text, input_leaves): (String, Vec<guardrails::body_redact::StringLeaf>) =
            if is_mcp {
                match guardrails::body_redact::extract_subtree_for_check(&body_bytes, "params") {
                    Some((text, leaves)) => (text, leaves),
                    None => (String::new(), Vec::new()),
                }
            } else {
                let text = String::from_utf8_lossy(&body_bytes).to_string();
                // Single-leaf fallback so Redacted on a raw-HTTP body
                // can still overwrite the whole body. The synthetic
                // leaf carries an empty path; the body-redact helper
                // won't splice that, so the Redacted branch falls
                // back to "log + forward original" — fine for the
                // non-MCP edge case.
                let leaves = if text.is_empty() {
                    Vec::new()
                } else {
                    vec![guardrails::body_redact::StringLeaf {
                        path: Vec::new(),
                        value: text.clone(),
                    }]
                };
                (text, leaves)
            };

        if !input_text.is_empty() {
            match guardrails::check_input(
                &input_text,
                &guardrail_configs,
                "mcp",
                &state.http_client,
            )
            .await
            {
                guardrails::CheckOutcome::Block(violation) => {
                    let duration = start.elapsed().as_secs_f64() * 1000.0;
                    tracing::warn!(
                        check = %violation.check,
                        config = %violation.config_name,
                        message = %violation.message,
                        "Guardrail violation — 403 returned"
                    );
                    proxy_metrics::record_guardrail_block("mcp", &violation.check);
                    proxy_metrics::record_request("mcp", method.as_str(), 403, "n/a");

                    // Build the JSON-RPC error envelope first so we
                    // can store its serialised form on the audit row
                    // as response_body_preview — otherwise the
                    // blocked path leaves a row with empty body
                    // previews and operators see "No preview
                    // available" in Mission Control / the proxy-log
                    // viewer even though we know exactly what we
                    // returned.
                    let rpc_id = extract_rpc_id(&body_bytes);
                    let rpc_body = guardrail_violation_body(&violation, rpc_id.as_ref(), "input");
                    let rejection_body_str = serde_json::to_string(&rpc_body).unwrap_or_default();

                    let mut log = proxy_logger::build_proxy_log(
                        "mcp",
                        &auth_ctx,
                        method.as_str(),
                        "mcp-proxy",
                        // target_url + server_id are in scope at
                        // this point (built / passed at the top of
                        // proxy_request). Without them the audit
                        // row reads "POST → N/A" and the operator
                        // can't tell which upstream the call would
                        // have hit. Forensic value matters more for
                        // a blocked request than a passed one — the
                        // happy path already captures these.
                        Some(&target_url),
                        Some(server_id),
                        403,
                        duration,
                        false,
                        Some(&format!(
                            "Guardrail violation ({}): {}",
                            violation.check, violation.message
                        )),
                        &correlation_id,
                    );
                    log.user_agent = extract_user_agent(&headers);
                    log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
                    // Populate the same forensic fields the happy
                    // path captures (mcp.rs:1539-1568) — a blocked
                    // request needs MORE audit detail than a passed
                    // one, not less.
                    let req_preview = String::from_utf8_lossy(&body_bytes);
                    log.request_body_preview = Some(proxy_logger::body_preview(&req_preview));
                    log.response_body_preview =
                        Some(proxy_logger::body_preview(&rejection_body_str));
                    log.request_size_bytes = Some(body_bytes.len() as i32);
                    log.response_size_bytes = Some(rejection_body_str.len() as i32);
                    // mcp_method is the JSON-RPC method (tools/call,
                    // tools/list, …) — NOT the HTTP method. Previous
                    // version set this to `method.to_string()` which
                    // gave "POST" and the admin's Capability column
                    // fell back to displaying that.
                    log.mcp_method = mcp_inner_method.clone();
                    log.mcp_capability_name = current_tool_name.clone();
                    if let Some(id) = rpc_id.as_ref() {
                        log.mcp_request_id = Some(if id.is_string() {
                            id.as_str().unwrap_or("").to_string()
                        } else {
                            id.to_string()
                        });
                    }
                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    match &residency_decision {
                        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                        None => proxy_logger::apply_residency_not_evaluated(
                            &mut log,
                            server_region_for_audit.as_deref(),
                        ),
                    }
                    // MCP guardrail
                    // block. SOC 2 CC7.2 + (when PII) HIPAA/GDPR.
                    proxy_logger::apply_guardrail_violation(&mut log, &violation);
                    if violation.check == "pii" {
                        proxy_logger::apply_pii_detected(&mut log);
                    }
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);

                    return (StatusCode::FORBIDDEN, Json(rpc_body)).into_response();
                }
                guardrails::CheckOutcome::Redacted {
                    content: redacted,
                    warnings,
                } => {
                    // Splice the redacted text back into the
                    // JSON-RPC params subtree using body_redact's
                    // walk + slot replacement. The detector was
                    // fed text-leaves joined with `\n`, so we
                    // split the redacted output the same way and
                    // map each part back to its original path.
                    //
                    // Fallback if the split count mismatches
                    // (detector did something we didn't predict):
                    // forward the ORIGINAL body and log a warning.
                    // Better to leak unredacted than to corrupt a
                    // live JSON-RPC envelope.
                    if let Some(new_body) = guardrails::body_redact::apply_redactions_to_subtree(
                        &body_bytes,
                        "params",
                        &input_leaves,
                        &redacted,
                    ) {
                        body_bytes = bytes::Bytes::from(new_body);
                        tracing::debug!(
                            target: "guardrails",
                            "MCP input body redacted in place ({} leaves)",
                            input_leaves.len(),
                        );
                    } else {
                        tracing::warn!(
                            target: "guardrails",
                            "MCP input redaction skipped — split-count mismatch, forwarding original body",
                        );
                    }
                    if warnings.iter().any(|w| w.check == "pii") {
                        input_pii_detected = true;
                    }
                    for w in &warnings {
                        tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                    }
                }
                guardrails::CheckOutcome::Warn { warnings } => {
                    if warnings.iter().any(|w| w.check == "pii") {
                        input_pii_detected = true;
                    }
                    for w in &warnings {
                        tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                    }
                }
                guardrails::CheckOutcome::Pass => {}
            }
        }
    }

    // Semantic Governance Policy on the mcp_input surface (independent of the
    // guardrail config gate). Scans the tool-call params against bound NL
    // policies; shadow logs a would-block, enforce returns a JSON-RPC error.
    {
        use crate::services::semantic_policy::{self, PolicyContext, PolicyDecision};
        let sgp_content = if is_mcp {
            guardrails::body_redact::extract_subtree_for_check(&body_bytes, "params")
                .map(|(t, _)| t)
                .unwrap_or_default()
        } else {
            String::from_utf8_lossy(&body_bytes).to_string()
        };
        if !sgp_content.is_empty() {
            let group_id = target_group
                .as_ref()
                .map(|g| g.id.clone())
                .or_else(|| auth_ctx.api_key_group_id.clone())
                .or_else(|| auth_ctx.resolved_group_id.clone());
            let ctx = PolicyContext {
                surface: "mcp_input".to_string(),
                actor_agent_id: auth_ctx.actor_agent_id.clone(),
                action_type: Some("mcp_tool".to_string()),
                target: current_tool_name.clone(),
                subject: auth_ctx.gateway_user_id.clone(),
            };
            // The judge's call is a step of this call's run.
            let auth_headers = crate::routes::llm_helpers::judge_self_call_headers(
                &headers,
                &call_chain,
                current_task_id,
                &tenant_key,
            );
            let outcome = semantic_policy::load_and_evaluate(
                &state.semantic_policy_cache,
                state.semantic_policy_judge.as_ref(),
                &state.db,
                &tenant_id,
                group_id.as_deref(),
                &sgp_content,
                &ctx,
                &auth_headers,
                state.config.judge_model.as_deref(),
            )
            .await;
            let rg = target_group.as_ref().map(|g| g.id.clone());
            for (pid, reason) in &outcome.shadow_hits {
                let log = proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "mcp",
                    "mcp-proxy",
                    current_tool_name.as_deref(),
                    rg.clone(),
                    &correlation_id,
                    start.elapsed().as_secs_f64() * 1000.0,
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
                let log = proxy_logger::build_semantic_policy_log(
                    &auth_ctx,
                    "mcp",
                    "mcp-proxy",
                    current_tool_name.as_deref(),
                    rg,
                    &correlation_id,
                    start.elapsed().as_secs_f64() * 1000.0,
                    &pid,
                    true,
                    &reason,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
                let rpc_id = extract_rpc_id(&body_bytes);
                let rpc_body = serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": JSONRPC_GUARDRAIL_BLOCKED,
                        "message": format!("semantic_policy_blocked: {reason}"),
                    },
                });
                return (StatusCode::FORBIDDEN, Json(rpc_body)).into_response();
            }
        }
    }

    // =========================================================================
    // POLICY-10 — AST-aware argument policies (MCP inbound surface)
    //
    // Runs only on tools/call. Picks the per-policy `argument_key`
    // out of params.arguments, applies the chosen analyzer, and
    // returns a JSON-RPC error envelope on a deny.
    // =========================================================================
    // The decision junction. Argument policy runs HERE
    // and capability access runs later in this handler, and both can now
    // demand approval. Enqueuing at both would put two entries in the
    // queue for one call, so the earlier site only RECORDS its verdict
    // and the later one merges the two with `Decision::strictest` and
    // acts once. `Deny` still short-circuits here, as it always has: a
    // denied call must not incur a capability lookup.
    let mut arg_policy_approval: Option<crate::services::argument_policy::evaluator::Violation> =
        None;

    if mcp_inner_method.as_deref() == Some("tools/call") {
        if let (Some(tool_name), Some(tg)) = (current_tool_name.as_deref(), target_group.as_ref()) {
            let arguments = parsed_body
                .as_ref()
                .and_then(|b| b.get("params"))
                .and_then(|p| p.get("arguments"))
                .cloned()
                .unwrap_or(serde_json::Value::Null);

            if let Some(v) = crate::services::argument_policy::enforce(
                &state.argument_policy_cache,
                &state.db,
                &tg.tenant_id,
                &tg.id,
                crate::services::argument_policy::policy::Surface::McpInput,
                tool_name,
                &arguments,
            )
            .await
            {
                match v.severity {
                    crate::services::argument_policy::policy::Severity::Warn => {
                        tracing::warn!(
                            policy = %v.policy_name,
                            tool = %v.tool,
                            rule = %v.rule,
                            "MCP argument policy warn (logged, not blocked)"
                        );
                    }
                    // The band: hold the call for a human rather than
                    // refusing it. Recorded, not enqueued — see the junction
                    // note above.
                    crate::services::argument_policy::policy::Severity::ApprovalRequired => {
                        tracing::info!(
                            policy = %v.policy_name,
                            tool = %v.tool,
                            rule = %v.rule,
                            field = ?v.field,
                            observed = ?v.observed,
                            threshold = ?v.threshold,
                            "MCP argument policy requires approval"
                        );
                        arg_policy_approval = Some(v);
                    }
                    crate::services::argument_policy::policy::Severity::Deny => {
                        tracing::warn!(
                            policy = %v.policy_name,
                            tool = %v.tool,
                            rule = %v.rule,
                            "MCP argument policy deny — 403 returned"
                        );
                        proxy_metrics::record_request("mcp", method.as_str(), 403, "n/a");

                        // Build the JSON-RPC response body first so the
                        // audit row's response_body_preview matches what
                        // the caller actually saw (same pattern the
                        // guardrail-blocked path uses at mcp.rs:935 — a
                        // denied request needs MORE forensic detail than
                        // a passed one, not less).
                        let rpc_id = extract_rpc_id(&body_bytes);
                        let body = json!({
                            "jsonrpc": "2.0",
                            "id": rpc_id.clone().unwrap_or(json!(null)),
                            "error": {
                                "code": JSONRPC_GUARDRAIL_BLOCKED,
                                "message": v.message,
                                "data": {
                                    "argument_policy": {
                                        "policy": v.policy_name,
                                        "tool": v.tool,
                                        "argument": v.argument_key,
                                        "analyzer": v.analyzer,
                                        "rule": v.rule,
                                        "facts": v.facts,
                                    }
                                }
                            }
                        });
                        let rejection_body_str = serde_json::to_string(&body).unwrap_or_default();

                        // Proxy-log row — operators see denied calls
                        // in Mission Control with full body + tool +
                        // rule, same level of detail as a guardrail
                        // block.
                        let duration = start.elapsed().as_secs_f64() * 1000.0;
                        let mut log = proxy_logger::build_proxy_log(
                            "mcp",
                            &auth_ctx,
                            method.as_str(),
                            "mcp-proxy",
                            Some(&target_url),
                            Some(server_id),
                            403,
                            duration,
                            false,
                            Some(&format!(
                                "Argument policy ({}/{}): {}",
                                v.policy_name, v.rule, v.message
                            )),
                            &correlation_id,
                        );
                        log.user_agent = extract_user_agent(&headers);
                        let req_preview = String::from_utf8_lossy(&body_bytes);
                        log.request_body_preview = Some(proxy_logger::body_preview(&req_preview));
                        log.response_body_preview =
                            Some(proxy_logger::body_preview(&rejection_body_str));
                        log.request_size_bytes = Some(body_bytes.len() as i32);
                        log.response_size_bytes = Some(rejection_body_str.len() as i32);
                        log.mcp_method = mcp_inner_method.clone();
                        log.mcp_capability_name = current_tool_name.clone();
                        if let Some(id) = rpc_id.as_ref() {
                            log.mcp_request_id = Some(if id.is_string() {
                                id.as_str().unwrap_or("").to_string()
                            } else {
                                id.to_string()
                            });
                        }
                        log.resource_group_id = Some(tg.id.clone());
                        proxy_logger::apply_call_chain(
                            &mut log,
                            &call_chain,
                            &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        );
                        match &residency_decision {
                            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                            None => proxy_logger::apply_residency_not_evaluated(
                                &mut log,
                                server_region_for_audit.as_deref(),
                            ),
                        }
                        // MCP argument-policy
                        // deny. Drives SOC 2 CC6.6.
                        proxy_logger::apply_policy_deny(&mut log);
                        state
                            .background_writer
                            .write_proxy_log(state.db.clone(), log);

                        return (StatusCode::FORBIDDEN, Json(body)).into_response();
                    }
                }
            }
        }
    }

    // =========================================================================
    // Run data scope
    // =========================================================================
    // A run declares the records it is for; a tool call that names a record
    // is compared with that declaration before it goes upstream. Recorded in
    // `audit`, refused in `enforce`. If the system's config cannot be read,
    // whether it enforces is unknowable, so the call is refused (fail closed,
    // like argument policies).
    {
        let root_task_id = call_chain.root_task_id.to_string();
        let conflicts = crate::services::data_scope::capture(
            &state.db,
            &tg_tenant_or(&target_group, &auth_ctx),
            &root_task_id,
            headers
                .get(crate::services::data_scope::SCOPE_HEADER)
                .and_then(|v| v.to_str().ok()),
        )
        .await;
        if let (Some("tools/call"), Some(tool_name), Some(tg)) = (
            mcp_inner_method.as_deref(),
            current_tool_name.as_deref(),
            target_group.as_ref(),
        ) {
            let arguments = parsed_body
                .as_ref()
                .and_then(|b| b.get("params"))
                .and_then(|p| p.get("arguments"))
                .unwrap_or(&Value::Null);
            let refusal = crate::services::data_scope::gate(
                &state.db,
                crate::services::data_scope::Call {
                    tenant_id: &tg.tenant_id,
                    ai_system_id: &tg.id,
                    tool: tool_name,
                    arguments: &[arguments],
                    root_task_id: &root_task_id,
                    conflicts: &conflicts,
                    auth_ctx: &auth_ctx,
                    correlation_id: &correlation_id,
                },
            )
            .await;
            if let Some(crate::services::data_scope::Refusal { message, detail }) = refusal {
                proxy_metrics::record_request("mcp", method.as_str(), 403, "n/a");
                let rpc_id = extract_rpc_id(&body_bytes);
                let body = json!({
                    "jsonrpc": "2.0",
                    "id": rpc_id.clone().unwrap_or(json!(null)),
                    "error": {
                        "code": JSONRPC_GUARDRAIL_BLOCKED,
                        "message": message,
                        "data": { "data_scope": detail }
                    }
                });
                let rejection_body_str = serde_json::to_string(&body).unwrap_or_default();
                let duration = start.elapsed().as_secs_f64() * 1000.0;
                let mut log = proxy_logger::build_proxy_log(
                    "mcp",
                    &auth_ctx,
                    method.as_str(),
                    "mcp-proxy",
                    Some(&target_url),
                    Some(server_id),
                    403,
                    duration,
                    false,
                    Some(&message),
                    &correlation_id,
                );
                log.user_agent = extract_user_agent(&headers);
                let req_preview = String::from_utf8_lossy(&body_bytes);
                log.request_body_preview = Some(proxy_logger::body_preview(&req_preview));
                log.response_body_preview = Some(proxy_logger::body_preview(&rejection_body_str));
                log.request_size_bytes = Some(body_bytes.len() as i32);
                log.response_size_bytes = Some(rejection_body_str.len() as i32);
                log.mcp_method = mcp_inner_method.clone();
                log.mcp_capability_name = current_tool_name.clone();
                if let Some(id) = rpc_id.as_ref() {
                    log.mcp_request_id = Some(if id.is_string() {
                        id.as_str().unwrap_or("").to_string()
                    } else {
                        id.to_string()
                    });
                }
                log.resource_group_id = Some(tg.id.clone());
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        server_region_for_audit.as_deref(),
                    ),
                }
                proxy_logger::apply_policy_deny(&mut log);
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
                return (StatusCode::FORBIDDEN, Json(body)).into_response();
            }
        }
    }

    // =========================================================================
    // Capability-level access control (multi-group allow-wins, three-state)
    // =========================================================================
    // Resolve the groups whose capability filters apply. We resolve
    // every group that grants the caller access, then — when the
    // request carries an active workspace (`x-resource-group` →
    // resolved_group_id, or a group-scoped API key) — narrow to just
    // that workspace. Without the narrowing the filter is allow-wins
    // merged across every group the user belongs to, so a less-
    // restricted sibling workspace re-enables tools the active
    // workspace disabled. A bogus/absent active group falls back to
    // the full set (original behaviour) so access is never wrongly
    // denied. Separate from target_group (used for billing/quota).
    let capability_groups = {
        // Reuses the resolution done at the top of the request
        // rather than repeating it. `granting_groups` is the same value this
        // call produced.
        let all = granting_groups;
        match auth_ctx.active_resource_group() {
            Some(active) => {
                let scoped: Vec<_> = all.iter().filter(|g| g.id == active).cloned().collect();
                if scoped.is_empty() { all } else { scoped }
            }
            None => all,
        }
    };

    // Three-state capability access: Allowed → forward, ApprovalRequired → 428/202, Denied → 403.
    // If an X-Approval-Token header is present, validate it first (bypasses approval_required).
    if is_mcp {
        // The reading every control uses; never a second parse of the bytes
        // (the second parse failed where the classifier's succeeded,
        // and this whole block was skipped).
        if let Some(parsed) = parsed_body.as_ref() {
            let rpc_method = parsed.get("method").and_then(|v| v.as_str()).unwrap_or("");

            // Determine which capability is being accessed
            let cap_check: Option<(&str, String)> = match rpc_method {
                "tools/call" => parsed
                    .get("params")
                    .and_then(|p| p.get("name"))
                    .and_then(|v| v.as_str())
                    .map(|n| ("tool", n.to_string())),
                "resources/read" => parsed
                    .get("params")
                    .and_then(|p| p.get("uri"))
                    .and_then(|v| v.as_str())
                    .map(|n| ("resource", n.to_string())),
                "prompts/get" => parsed
                    .get("params")
                    .and_then(|p| p.get("name"))
                    .and_then(|v| v.as_str())
                    .map(|n| ("prompt", n.to_string())),
                _ => None,
            };

            if let Some((cap_type, cap_name)) = cap_check {
                // The request's approvals: one object for the agent's grant
                // and the capability filter, which ask the same question
                // and are answered by one approval
                // (`agent_approval::RequestApprovals`).
                let approvals = agent_approval::RequestApprovals::from_headers(&headers);
                // Per-agent runtime authorization ("actions, not access"). When
                // the actor is an agent principal, decide whether it may invoke
                // this MCP capability from its grants. Runs alongside the
                // resource-group capability ACL below (either plane can block);
                // fails closed on a lookup error.
                // Resolve the tool's risk attributes from the server's
                // discovered MCP annotation hints (readOnly/destructive/…), so
                // a grant's `deny_tool_risk` can gate by risk class and so
                // autonomy can tell a read from a write. Only for tool calls,
                // and only when the tool is in the discovered capability set
                // (an unknown tool carries no risk metadata, and `classify`
                // treats that as side-effecting).
                //
                // Hoisted above the agent branch because the non-agent autonomy
                // gate below needs the same classification.
                let tool_risk: Vec<String> = if cap_type == "tool" {
                    server
                        .capabilities
                        .as_ref()
                        .and_then(|c| {
                            c.get("tools")
                                .and_then(|t| t.as_array())
                                .or_else(|| c.as_array())
                        })
                        .and_then(|tools| {
                            tools.iter().find(|t| {
                                t.get("name").and_then(|n| n.as_str()) == Some(cap_name.as_str())
                            })
                        })
                        .map(|t| {
                            crate::services::agent_authz::tool_risk_attributes(t.get("annotations"))
                        })
                        .unwrap_or_default()
                } else {
                    Vec::new()
                };

                // Run-scoped controls — the run budget and an
                // operator's abort — apply to EVERY caller, agent
                // principal or not, and before either authorization plane so a
                // run that is over its ceiling or aborted cannot even queue an
                // approval (the skill surface's order). They used to sit in
                // the non-agent branch beside the autonomy gate, which meant an
                // agent-keyed MCP call skipped both: the callers the run budget
                // exists for were the ones it did not reach. Nothing in the design of the run controls
                // asked for the exemption; the sites were chosen by where the
                // autonomy call happened to be, and for agents that call lives
                // inside `decide_for_agent`.
                //
                // Budget first, then abort — one verdict, see
                // `run_controls::check`. Abort stops the NEXT action; a call
                // already in flight completes.
                //
                // The capability call is one action of the run. Its unit is
                // held with the request's other holds: kept when the call is
                // dispatched upstream, given back if a later gate refuses or
                // holds it.
                let starter = crate::services::run_controls::run_starter(
                    &auth_ctx,
                    trace_continuity == TraceContinuity::Verified,
                );
                let step = crate::services::run_controls::check(
                    &state,
                    crate::services::run_controls::Step {
                        tenant_id: &tenant_id,
                        group_id: target_group.as_ref().map(|g| g.id.as_str()),
                        run_id: &call_chain.root_task_id.to_string(),
                        depth: call_chain.depth,
                        kind: crate::services::run_controls::StepKind::Action,
                        starter: starter.as_deref(),
                    },
                )
                .await;
                let refusal = match step {
                    Ok(admitted) => {
                        mcp_holds.extend(admitted.into_hold());
                        None
                    }
                    Err(refusal) => Some(refusal),
                };
                if let Some(refusal) = refusal {
                    let duration = start.elapsed().as_secs_f64() * 1000.0;
                    let mut log = proxy_logger::build_proxy_log(
                        "mcp",
                        &auth_ctx,
                        method.as_str(),
                        "mcp-proxy",
                        Some(server.base_url.as_str()),
                        Some(server.id.as_str()),
                        refusal.status().as_u16(),
                        duration,
                        false,
                        Some(refusal.message()),
                        &correlation_id,
                    );
                    log.user_agent = extract_user_agent(&headers);
                    log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
                    log.mcp_method = Some(rpc_method.to_string());
                    log.mcp_capability_name = Some(cap_name.clone());
                    // A run refusal is `denied`, not a
                    // failed call.
                    log.proxy_subtype = Some(refusal.subtype().to_string());
                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);
                    // One run too many for the credential is a rate refusal,
                    // answered as this route answers every rate limit.
                    return match refusal.rate_block() {
                        Some(block) => mcp_rate_limit_response(block, parsed.get("id")),
                        None => error_response(refusal.status(), refusal.message()),
                    };
                }

                if let Some(agent_id) = auth_ctx.actor_agent_id.clone() {
                    use crate::services::agent_authz::{Decision, decide_for_agent};
                    match decide_for_agent(
                        &state.db,
                        &tenant_id,
                        &agent_id,
                        "mcp_tool",
                        &cap_name,
                        None,
                        None,
                        Some(&state.usage_state),
                        &tool_risk,
                    )
                    .await
                    {
                        // dry_run → record a would-block (shadow) and proceed,
                        // for any non-Allow decision (deny or approval-required).
                        Ok(o) if o.dry_run && o.decision != Decision::Allow => {
                            let duration = start.elapsed().as_secs_f64() * 1000.0;
                            let mut log = proxy_logger::build_proxy_log(
                                "mcp",
                                &auth_ctx,
                                method.as_str(),
                                "mcp-proxy",
                                Some(server.base_url.as_str()),
                                Some(server.id.as_str()),
                                200,
                                duration,
                                true,
                                Some(&o.reason),
                                &correlation_id,
                            );
                            log.user_agent = extract_user_agent(&headers);
                            if let Some(g) = target_group.as_ref() {
                                log.resource_group_id = Some(g.id.clone());
                            }
                            log.mcp_method = Some(rpc_method.to_string());
                            log.mcp_capability_name = Some(cap_name.clone());
                            proxy_logger::apply_agent_authz_decision(&mut log, &o, false);
                            log.proxy_subtype = Some(
                                crate::constants::proxy_subtype::AGENT_AUTHZ_SHADOW.to_string(),
                            );
                            proxy_logger::apply_call_chain(
                                &mut log,
                                &call_chain,
                                &current_task_id_str,
                                trace_continuity,
                                trace_root_kind,
                            );
                            state
                                .background_writer
                                .write_proxy_log(state.db.clone(), log);
                            // fall through — proceed as if allowed.
                        }
                        Ok(o) if o.decision == Decision::Deny => {
                            let duration = start.elapsed().as_secs_f64() * 1000.0;
                            let mut log = proxy_logger::build_proxy_log(
                                "mcp",
                                &auth_ctx,
                                method.as_str(),
                                "mcp-proxy",
                                Some(server.base_url.as_str()),
                                Some(server.id.as_str()),
                                403,
                                duration,
                                false,
                                Some(&o.reason),
                                &correlation_id,
                            );
                            log.user_agent = extract_user_agent(&headers);
                            if let Some(g) = target_group.as_ref() {
                                log.resource_group_id = Some(g.id.clone());
                            }
                            log.mcp_method = Some(rpc_method.to_string());
                            log.mcp_capability_name = Some(cap_name.clone());
                            // Capture what was attempted so the deny row carries the
                            // same request/response evidence as an argument-policy block.
                            log.request_body_preview = Some(proxy_logger::body_preview(
                                &String::from_utf8_lossy(&body_bytes),
                            ));
                            log.response_body_preview = Some(proxy_logger::body_preview(&o.reason));
                            log.request_size_bytes = Some(body_bytes.len() as i32);
                            proxy_logger::apply_agent_authz_decision(&mut log, &o, true);
                            proxy_logger::apply_call_chain(
                                &mut log,
                                &call_chain,
                                &current_task_id_str,
                                trace_continuity,
                                trace_root_kind,
                            );
                            state
                                .background_writer
                                .write_proxy_log(state.db.clone(), log);
                            return error_response(
                                StatusCode::FORBIDDEN,
                                &format!("agent_not_authorized: {}", o.reason),
                            );
                        }
                        Ok(o) if o.decision == Decision::ApprovalRequired => {
                            // The agent's grant requires human approval for this
                            // action. Reuse the MCP approval queue verbatim: if a
                            // valid one-time X-Approval-Token is presented, consume
                            // it and fall through to the RG ACL; otherwise enqueue a
                            // tool_approval_request and return 428/202.
                            // One shared approval lifecycle for every
                            // surface — see `agent_approval::require_or_enqueue`.
                            // Agent grants carry no per-action timeout (unlike
                            // RG capability configs), so the window comes from
                            // the same capability-filter row the RG ACL below
                            // reads: its approval_required timeout if it has
                            // one, else the hold window an operator set on an
                            // `enabled` row, else none — the hold then takes the
                            // group / tenant / platform default. Looked up only
                            // on this (rare) hold path, so the common case
                            // still runs the filter query once.
                            let grant_hold_window = {
                                let resolution =
                                    usage::check_mcp_capability_access_with_hold_window(
                                        &state.db,
                                        &server.id,
                                        cap_type,
                                        &cap_name,
                                        &capability_groups,
                                    )
                                    .await;
                                policy_hold_window(
                                    &resolution.access,
                                    resolution.hold_window_seconds,
                                )
                            };
                            let ctx = agent_approval::ApprovalContext {
                                db: &state.db,
                                tenant_id: &tenant_id,
                                surface: "mcp",
                                scope_ref: &server.name,
                                capability_type: cap_type,
                                capability_name: &cap_name,
                                auth_ctx: &auth_ctx,
                                groups: &capability_groups,
                                policy_window_seconds: grant_hold_window,
                                // Bind the retried call to the
                                // arguments the approver saw.
                                call_arguments: parsed
                                    .get("params")
                                    .and_then(|p| p.get("arguments")),
                                binding: None,
                                run: Some(RunScope::from_chain(
                                    &call_chain,
                                    &current_task_id_str,
                                    trace_root_kind,
                                )),
                                extra_metadata: None,
                                run_clock: Some(&state.usage_state),
                                approvals: &approvals,
                                // An external server's tool: no later gate.
                                downstream: None,
                            };
                            let approval_response =
                                match agent_approval::require_or_enqueue(&ctx).await {
                                    agent_approval::ApprovalOutcome::Proceed => None,
                                    agent_approval::ApprovalOutcome::Envelope(st, body) => {
                                        Some(Ok((st, body)))
                                    }
                                    agent_approval::ApprovalOutcome::Denied(msg) => {
                                        return error_response(
                                            StatusCode::FORBIDDEN,
                                            &format!("Approval token invalid: {}", msg),
                                        );
                                    }
                                    agent_approval::ApprovalOutcome::Failed(e) => Some(Err(e)),
                                    // No gate is behind this one.
                                    agent_approval::ApprovalOutcome::Refused(r) => {
                                        return error_response(r.status, &r.message);
                                    }
                                };

                            if let Some(approval_response) = approval_response {
                                let (status_for_log, err_for_log, envelope_str) =
                                    match &approval_response {
                                        Ok((status, body)) => (
                                            status.as_u16(),
                                            None,
                                            serde_json::to_string(body).unwrap_or_default(),
                                        ),
                                        Err(e) => (500u16, Some(e.as_str()), String::new()),
                                    };
                                let req_preview = String::from_utf8_lossy(&body_bytes).to_string();
                                let duration = start.elapsed().as_secs_f64() * 1000.0;
                                let mut log = proxy_logger::build_proxy_log(
                                    "mcp",
                                    &auth_ctx,
                                    method.as_str(),
                                    "mcp-proxy",
                                    Some(server.base_url.as_str()),
                                    Some(server.id.as_str()),
                                    status_for_log,
                                    duration,
                                    false,
                                    err_for_log.or(Some(&o.reason)),
                                    &correlation_id,
                                );
                                log.user_agent = extract_user_agent(&headers);
                                if let Some(g) = target_group.as_ref() {
                                    log.resource_group_id = Some(g.id.clone());
                                }
                                log.mcp_method = Some(rpc_method.to_string());
                                log.mcp_capability_name = Some(cap_name.clone());
                                log.request_body_preview =
                                    Some(proxy_logger::body_preview(&req_preview));
                                if !envelope_str.is_empty() {
                                    log.response_body_preview =
                                        Some(proxy_logger::body_preview(&envelope_str));
                                    log.response_size_bytes = Some(envelope_str.len() as i32);
                                }
                                log.request_size_bytes = Some(body_bytes.len() as i32);
                                // Stamp the agent decision + matched grant (blocked
                                // = false: this is a pending-approval row, not a
                                // hard block), then mark the subtype.
                                proxy_logger::apply_agent_authz_decision(&mut log, &o, false);
                                log.proxy_subtype = Some(
                                    crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string(),
                                );
                                proxy_logger::apply_call_chain(
                                    &mut log,
                                    &call_chain,
                                    &current_task_id_str,
                                    trace_continuity,
                                    trace_root_kind,
                                );
                                state
                                    .background_writer
                                    .write_proxy_log(state.db.clone(), log);

                                match approval_response {
                                    Ok((status, body)) => {
                                        return (status, Json(body)).into_response();
                                    }
                                    Err(e) => {
                                        tracing::error!(
                                            error = %e,
                                            "Failed to create agent approval request"
                                        );
                                        return error_response(
                                            StatusCode::INTERNAL_SERVER_ERROR,
                                            "Failed to create approval request",
                                        );
                                    }
                                }
                            }
                            // approved == true → fall through to the RG ACL check.
                        }
                        Ok(_) => {}
                        Err(e) => {
                            tracing::error!(error = %e, agent = %agent_id, "agent authz lookup failed; failing closed");
                            return error_response(
                                StatusCode::FORBIDDEN,
                                "agent_authz_unavailable",
                            );
                        }
                    }
                } else {
                    // Non-agent caller: the agent-authz block above never ran,
                    // and neither did the autonomy gate at its end. A suspended
                    // or read-only AI System has to stop a plain API key too —
                    // otherwise the one control an operator reaches for in an
                    // incident exempts exactly the callers a script uses.
                    let group_id = target_group
                        .as_ref()
                        .map(|g| g.id.as_str())
                        .or(auth_ctx.api_key_group_id.as_deref())
                        .or(auth_ctx.resolved_group_id.as_deref());
                    if let Some((crate::services::autonomy::Verdict::Deny(reason), resolution)) =
                        crate::services::autonomy::enforce_for_group(
                            &state.db, &tenant_id, group_id, "mcp_tool", &tool_risk,
                        )
                        .await
                    {
                        tracing::warn!(
                            tenant_id = %tenant_id,
                            group = ?group_id,
                            capability = %cap_name,
                            level = %resolution.level.as_str(),
                            provenance = %resolution.provenance.as_str(),
                            "autonomy block on a non-agent caller — refusing 403",
                        );
                        let duration = start.elapsed().as_secs_f64() * 1000.0;
                        let mut log = proxy_logger::build_proxy_log(
                            "mcp",
                            &auth_ctx,
                            method.as_str(),
                            "mcp-proxy",
                            Some(server.base_url.as_str()),
                            Some(server.id.as_str()),
                            403,
                            duration,
                            false,
                            Some(reason),
                            &correlation_id,
                        );
                        log.user_agent = extract_user_agent(&headers);
                        if let Some(g) = target_group.as_ref() {
                            log.resource_group_id = Some(g.id.clone());
                        }
                        log.mcp_method = Some(rpc_method.to_string());
                        log.mcp_capability_name = Some(cap_name.clone());
                        proxy_logger::apply_autonomy(&mut log, &resolution, true);
                        // Same evidence as any other block row: what was
                        // attempted, and why it was refused.
                        log.request_body_preview = Some(proxy_logger::body_preview(
                            &String::from_utf8_lossy(&body_bytes),
                        ));
                        log.response_body_preview = Some(proxy_logger::body_preview(reason));
                        log.request_size_bytes = Some(body_bytes.len() as i32);
                        proxy_logger::apply_call_chain(
                            &mut log,
                            &call_chain,
                            &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        );
                        state
                            .background_writer
                            .write_proxy_log(state.db.clone(), log);
                        return error_response(
                            StatusCode::FORBIDDEN,
                            &format!("autonomy_denied: {reason}"),
                        );
                    }
                }

                let resolution = usage::check_mcp_capability_access_with_hold_window(
                    &state.db,
                    &server.id,
                    cap_type,
                    &cap_name,
                    &capability_groups,
                )
                .await;

                // Merge the two planes that can demand
                // approval, then act once. A band on an `enabled` tool holds
                // for the window the filter row carries (if the operator set
                // one) rather than a hard-coded default — see
                // `merge_band_with_capability_access`.
                let access =
                    merge_band_with_capability_access(arg_policy_approval.is_some(), &resolution);

                match access {
                    usage::CapabilityAccess::Allowed => {
                        // Proceed — no filter or explicitly enabled
                    }
                    usage::CapabilityAccess::Denied => {
                        tracing::debug!(
                            cap_type, cap_name, server_id,
                            groups = ?capability_groups.iter().map(|g| &g.name).collect::<Vec<_>>(),
                            "MCP capability blocked by filter (disabled)"
                        );
                        let msg = format!(
                            "Access denied: {} '{}' is disabled for your group",
                            cap_type, cap_name
                        );
                        // A per-group tool-filter refusal is a logical-access-control
                        // denial — write the audit row (this path previously returned
                        // with no proxy_log, so tool-filter blocks were invisible in
                        // Mission Control and the compliance export). Stamp it like the
                        // other access denials (soc2.cc6.6 via apply_policy_deny).
                        let duration = start.elapsed().as_secs_f64() * 1000.0;
                        let mut log = proxy_logger::build_proxy_log(
                            "mcp",
                            &auth_ctx,
                            method.as_str(),
                            "mcp-proxy",
                            Some(server.base_url.as_str()),
                            Some(server.id.as_str()),
                            403,
                            duration,
                            false,
                            Some(&msg),
                            &correlation_id,
                        );
                        log.user_agent = extract_user_agent(&headers);
                        if let Some(g) = target_group.as_ref() {
                            log.resource_group_id = Some(g.id.clone());
                        }
                        log.mcp_method = Some(rpc_method.to_string());
                        log.mcp_capability_name = Some(cap_name.clone());
                        // Capture what was attempted so a tool-filter deny carries the
                        // same request/response evidence as an argument-policy block.
                        log.request_body_preview = Some(proxy_logger::body_preview(
                            &String::from_utf8_lossy(&body_bytes),
                        ));
                        log.response_body_preview = Some(proxy_logger::body_preview(&msg));
                        log.request_size_bytes = Some(body_bytes.len() as i32);
                        proxy_logger::apply_policy_deny(&mut log);
                        proxy_logger::apply_call_chain(
                            &mut log,
                            &call_chain,
                            &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        );
                        state
                            .background_writer
                            .write_proxy_log(state.db.clone(), log);
                        return error_response(StatusCode::FORBIDDEN, &msg);
                    }
                    usage::CapabilityAccess::ApprovalRequired { window_seconds } => {
                        // One shared approval lifecycle for every surface —
                        // see `agent_approval::require_or_enqueue`. A valid
                        // one-time token consumes and falls through to
                        // forwarding; otherwise a request is enqueued and its
                        // 428/202 envelope returned.
                        //
                        // The proxy_log row goes in regardless of outcome: a
                        // block-pending-approval is just as audit-worthy as a
                        // deny, and the demo's "the whole exchange is logged"
                        // contract relies on it.
                        let ctx = agent_approval::ApprovalContext {
                            db: &state.db,
                            tenant_id: &tenant_id,
                            surface: "mcp",
                            scope_ref: &server.name,
                            capability_type: cap_type,
                            capability_name: &cap_name,
                            auth_ctx: &auth_ctx,
                            groups: &capability_groups,
                            policy_window_seconds: window_seconds,
                            // Bind the retried call to the
                            // arguments the approver saw.
                            call_arguments: parsed.get("params").and_then(|p| p.get("arguments")),
                            binding: None,
                            run: Some(RunScope::from_chain(
                                &call_chain,
                                &current_task_id_str,
                                trace_root_kind,
                            )),
                            // Tell the approver WHY. A bare
                            // tool name is a queue entry people rubber-stamp;
                            // "amount 8000 exceeds the 1000 threshold set by
                            // payments-threshold" is one they can act on.
                            extra_metadata: arg_policy_approval.as_ref().map(|v| {
                                json!({
                                    "policy_id": v.policy_id,
                                    "policy_name": v.policy_name,
                                    "rule": v.rule,
                                    "field": v.field,
                                    "observed": v.observed,
                                    "threshold": v.threshold,
                                    "reason": v.message,
                                })
                            }),
                            run_clock: Some(&state.usage_state),
                            approvals: &approvals,
                            downstream: None,
                        };
                        let enqueued = match agent_approval::require_or_enqueue(&ctx).await {
                            agent_approval::ApprovalOutcome::Proceed => {
                                tracing::debug!(
                                    cap_type,
                                    cap_name,
                                    server_id,
                                    "MCP capability approved via approval token"
                                );
                                None
                            }
                            agent_approval::ApprovalOutcome::Envelope(st, body) => {
                                Some(Ok((st, body)))
                            }
                            agent_approval::ApprovalOutcome::Denied(msg) => {
                                return error_response(
                                    StatusCode::FORBIDDEN,
                                    &format!("Approval token invalid: {}", msg),
                                );
                            }
                            agent_approval::ApprovalOutcome::Failed(e) => Some(Err(e)),
                            // No gate is behind this one.
                            agent_approval::ApprovalOutcome::Refused(r) => {
                                return error_response(r.status, &r.message);
                            }
                        };

                        if let Some(approval_response) = enqueued {
                            // Capture the response envelope so the proxy_log
                            // row's response_body_preview matches the bytes
                            // the caller actually received — same forensic
                            // contract as the guardrail-blocked path at
                            // mcp.rs:1029 and the argument-policy DENY path
                            // at mcp.rs:1196.
                            let (status_for_log, success_for_log, err_for_log, envelope_str) =
                                match &approval_response {
                                    Ok((status, body)) => (
                                        status.as_u16(),
                                        false,
                                        None,
                                        serde_json::to_string(body).unwrap_or_default(),
                                    ),
                                    Err(e) => (500u16, false, Some(e.as_str()), String::new()),
                                };
                            let req_preview = String::from_utf8_lossy(&body_bytes).to_string();
                            let duration = start.elapsed().as_secs_f64() * 1000.0;
                            let mut log = proxy_logger::build_proxy_log(
                                "mcp",
                                &auth_ctx,
                                method.as_str(),
                                "mcp-proxy",
                                Some(server.base_url.as_str()),
                                Some(server.id.as_str()),
                                status_for_log,
                                duration,
                                success_for_log,
                                err_for_log.or(Some("approval_required")),
                                &correlation_id,
                            );
                            log.user_agent = extract_user_agent(&headers);
                            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
                            log.proxy_subtype = Some(
                                crate::constants::proxy_subtype::APPROVAL_REQUIRED.to_string(),
                            );
                            log.mcp_method = Some(rpc_method.to_string());
                            log.mcp_capability_name = Some(cap_name.clone());
                            log.request_body_preview =
                                Some(proxy_logger::body_preview(&req_preview));
                            if !envelope_str.is_empty() {
                                log.response_body_preview =
                                    Some(proxy_logger::body_preview(&envelope_str));
                                log.response_size_bytes = Some(envelope_str.len() as i32);
                            }
                            log.request_size_bytes = Some(body_bytes.len() as i32);
                            proxy_logger::apply_call_chain(
                                &mut log,
                                &call_chain,
                                &current_task_id_str,
                                trace_continuity,
                                trace_root_kind,
                            );
                            match &residency_decision {
                                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                                None => proxy_logger::apply_residency_not_evaluated(
                                    &mut log,
                                    server_region_for_audit.as_deref(),
                                ),
                            }
                            state
                                .background_writer
                                .write_proxy_log(state.db.clone(), log);

                            match approval_response {
                                Ok((status, body)) => {
                                    return (status, Json(body)).into_response();
                                }
                                Err(e) => {
                                    tracing::error!(
                                        error = %e,
                                        "Failed to create approval request"
                                    );
                                    return error_response(
                                        StatusCode::INTERNAL_SERVER_ERROR,
                                        "Failed to create approval request",
                                    );
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // =========================================================================
    // Per-server concurrency gate (Feature 9)
    // =========================================================================
    let max_concurrent = server
        .max_concurrent_requests
        .unwrap_or(crate::services::mcp_limits::DEFAULT_MAX_CONCURRENT_REQUESTS);
    let concurrency_key = format!("{tenant_id}:{server_id}");
    let permit = if max_concurrent > 0 {
        let semaphore = crate::services::mcp_limits::server_permits(
            &state.mcp_concurrency,
            &concurrency_key,
            max_concurrent as usize,
        );
        match semaphore.try_acquire_owned() {
            Ok(permit) => Some(permit),
            Err(_) => {
                tracing::warn!(
                    server_id,
                    max_concurrent,
                    "MCP concurrency limit reached — 429 returned"
                );
                return refuse_mcp_limit(
                    McpRowCtx {
                        state: &state,
                        auth_ctx: &auth_ctx,
                        headers: &headers,
                        method: method.as_str(),
                        target_url: &target_url,
                        server_id,
                        group_id: target_group.as_ref().map(|g| g.id.clone()),
                        correlation_id: &correlation_id,
                        start,
                        call_chain: &call_chain,
                        current_task_id_str: &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    },
                    &crate::services::mcp_limits::server_instance_block(max_concurrent),
                    parsed_body.as_ref().and_then(|v| v.get("id")),
                    mcp_inner_method.as_deref(),
                    current_tool_name.as_deref(),
                );
            }
        }
    } else {
        None // unlimited
    };

    // Forward request — with retry + circuit breaker (Feature 10)
    let timeout_secs = server
        .timeout
        .map(|t| t as u64)
        .unwrap_or(state.config.default_mcp_timeout_secs);
    let timeout = std::time::Duration::from_secs(timeout_secs);

    let upstream_method = match method {
        Method::GET => reqwest::Method::GET,
        Method::POST => reqwest::Method::POST,
        Method::PUT => reqwest::Method::PUT,
        Method::DELETE => reqwest::Method::DELETE,
        Method::PATCH => reqwest::Method::PATCH,
        Method::HEAD => reqwest::Method::HEAD,
        Method::OPTIONS => reqwest::Method::OPTIONS,
        _ => reqwest::Method::GET,
    };

    let circuit_key = format!("{tenant_id}:{server_id}");

    // A `tools/call` may invoke a side-effecting tool (send an email, create
    // a ticket). Such calls are non-idempotent: an ambiguous failure
    // (timeout, 502/504) must NOT be auto-retried or the tool could run more
    // than once. Protocol/read methods (initialize, tools/list, resources/read,
    // session DELETE, the GET listener) are idempotent and retry normally.
    let is_tools_call = mcp_inner_method.as_deref() == Some("tools/call");

    // Use per-server resilience config if available, otherwise defaults
    let retry_config = crate::services::retry::RetryConfig {
        max_retries: server.retry_attempts.unwrap_or(2) as u32,
        base_delay: std::time::Duration::from_millis(server.retry_after_ms.unwrap_or(100) as u64),
        idempotent: !is_tools_call,
        ..Default::default()
    };

    // Configure circuit breaker thresholds from per-server config
    state.circuit_breakers.configure(
        &circuit_key,
        server.failure_threshold.unwrap_or(5) as u32,
        std::time::Duration::from_secs(server.recovery_timeout_secs.unwrap_or(30) as u64),
    );

    // Estimate in-flight requests for this server by reading how many
    // permits are available on its concurrency semaphore. A server
    // configured with `max_concurrent_requests = 10` and 4 free permits
    // is carrying 6 in-flight calls. We feed that number into the
    // retry budget so the permitted retry cap scales with traffic.
    //
    // When the semaphore isn't initialised yet (first request of the
    // process lifetime) or the server is unbounded, we fall through to
    // `0`, which still gets the `min_concurrent_retries` floor on the
    // budget — so a cold-start still gets a few retries without
    // amplifying load on an already-shaky upstream.
    // Look the semaphore up under the key it was STORED
    // under.
    //
    // The map is keyed `{tenant}:{server}` (see the concurrency gate above)
    // but this read passed the bare `server_id`, so it never matched. The
    // estimate was therefore always 0 and the retry budget sat permanently at
    // its cold-start floor of a few concurrent retries per server, no matter
    // how loaded the upstream actually was — which is precisely backwards,
    // since the budget exists to stop retries amplifying load on a struggling
    // server.
    let active_requests_estimate: i64 = state
        .mcp_concurrency
        .get(&concurrency_key)
        .map(|entry| {
            let permits = entry.value();
            (permits.size() as i64 - permits.available_permits() as i64).max(0)
        })
        .unwrap_or(0);

    // Every gate has passed and the tool call leaves for the
    // upstream now. For a two-phase system the `planned` record is sealed
    // before the outcome row (off the request path; see `evidence::planned`).
    if mcp_inner_method.as_deref() == Some("tools/call") {
        crate::services::evidence::planned::dispatching(
            &state.db,
            &tenant_id,
            target_group.as_ref().map(|g| g.id.as_str()),
            crate::constants::proxy::MCP,
            current_tool_name.clone().unwrap_or_default(),
            Some(call_chain.root_task_id.to_string()),
        );
    }

    // The call is dispatched: what it was admitted against stays spent.
    for hold in mcp_holds.drain(..) {
        hold.commit().await;
    }
    // What the call holds while in flight; it goes with the answer (the
    // relay task of a stream), and every way the call ends gives it back.
    let in_flight = crate::services::mcp_limits::McpInFlight::new(permit, slot_hold);

    // The call's own timeout (`execution.timeout_seconds`) bounds the
    // headers of every attempt together with the server's; for anything but
    // a GET listener it is also the call's total, one deadline from here
    // that the retries, the body and a stream all share.
    let first_byte = exec_limits.timeout.map_or(timeout, |t| t.min(timeout));
    let deadline = exec_limits
        .timeout
        .filter(|_| method != Method::GET)
        .map(|t| tokio::time::Instant::now() + t);
    // The shared client's own whole-request timeout must not cut a call
    // allowed longer.
    let request_timeout = exec_limits
        .timeout
        .filter(|t| *t > std::time::Duration::from_secs(120))
        .map(|t| t + std::time::Duration::from_secs(5));
    // Whether the last attempt ran out of time (a 504, not a 502).
    let timed_out = std::sync::atomic::AtomicBool::new(false);
    // No retry waits past the deadline.
    let retry_config = crate::services::retry::RetryConfig {
        deadline,
        ..retry_config
    };

    // Use retry with circuit breaker + budget. The budget caps the
    // global number of in-flight retries per server so a partial
    // outage can't amplify itself by fanning out N×(max_retries+1)
    // new requests on every call.
    let retry_result = {
        let http_client = &state.http_client;
        let target = &target_url;
        let method_clone = upstream_method.clone();
        let headers_clone = upstream_headers.clone();
        let body_clone = body_bytes.clone();

        crate::services::retry::execute_with_retry_and_budget(
            &retry_config,
            &state.circuit_breakers,
            &circuit_key,
            Some(&state.retry_budget),
            active_requests_estimate,
            || {
                let mut request = http_client
                    .request(method_clone.clone(), target.as_str())
                    .headers(headers_clone.clone())
                    .body(body_clone.clone());
                if let Some(t) = request_timeout {
                    request = request.timeout(t);
                }
                let fut = request.send();
                let attempt_timeout = match deadline {
                    Some(d) => {
                        first_byte.min(d.saturating_duration_since(tokio::time::Instant::now()))
                    }
                    None => first_byte,
                };
                let timed_out = &timed_out;
                async move {
                    let outcome = tokio::time::timeout(attempt_timeout, fut).await;
                    timed_out.store(outcome.is_err(), std::sync::atomic::Ordering::Relaxed);
                    match outcome {
                        Ok(result) => result,
                        Err(_) => {
                            // Our tokio timeout fired — materialise it as a
                            // reqwest *timeout* error so the retry layer
                            // classifies it as `is_timeout()` (and crucially
                            // NOT `is_connect()`): the non-idempotent
                            // `tools/call` path must treat a timeout as
                            // non-retryable. A zero-duration timeout against a
                            // non-routable raw IP (TEST-NET-1, RFC 5737) fires
                            // instantly with no DNS, so the only possible error
                            // kind is the timeout — no connect/DNS race. Reuses
                            // the shared client (no per-timeout Client alloc).
                            Err(http_client
                                .get("http://192.0.2.0/")
                                .timeout(std::time::Duration::ZERO)
                                .send()
                                .await
                                .unwrap_err())
                        }
                    }
                }
            },
        )
        .await
    };

    // Convert retry result to the same Result<Response, Error> shape as before
    let resp: Result<reqwest::Response, reqwest::Error> = match retry_result {
        Ok(response) => {
            // Record health success
            state
                .mcp_health
                .record_success(state.db.clone(), &tenant_id, server_id);
            Ok(response)
        }
        Err(crate::services::retry::RetryError::CircuitOpen(msg)) => {
            let duration = start.elapsed().as_millis() as f64;
            tracing::warn!(server_id, "MCP circuit breaker open — 503 returned");
            // Mark the server's health as unhealthy and (through the same
            // helper) raise a system alert so the on-call engineer sees
            // the circuit trip — even a zero-traffic server can't ride
            // past an open breaker silently.
            state
                .mcp_health
                .record_circuit_open(state.db.clone(), &tenant_id, server_id);
            proxy_metrics::record_request("mcp", method.as_str(), 503, "mcp");
            proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration / 1000.0);
            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&target_url),
                Some(server_id),
                503,
                duration,
                false,
                Some(&msg),
                &correlation_id,
            );
            log.user_agent = extract_user_agent(&headers);
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    server_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(json!({"error": "MCP server temporarily unavailable (circuit breaker open)"})),
            )
                .into_response();
        }
        Err(crate::services::retry::RetryError::AllRetriesExhausted {
            last_error,
            attempts,
        }) => {
            let duration = start.elapsed().as_millis() as f64;
            tracing::debug!(server_id, attempts, error = %last_error, "MCP all retries exhausted");
            state
                .mcp_health
                .record_failure(state.db.clone(), &tenant_id, server_id, &last_error);
            // No answer in time is a timeout (504), any other failure 502.
            let timed_out = timed_out.load(std::sync::atomic::Ordering::Relaxed);
            let err_status: u16 = if timed_out { 504 } else { 502 };
            proxy_metrics::record_request("mcp", method.as_str(), err_status, "mcp");
            proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration / 1000.0);
            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&target_url),
                Some(server_id),
                err_status,
                duration,
                false,
                Some(&last_error),
                &correlation_id,
            );
            log.user_agent = extract_user_agent(&headers);
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            log.request_size_bytes = Some(body_bytes.len() as i32);
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    server_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            if timed_out {
                return error_response(StatusCode::GATEWAY_TIMEOUT, "MCP server timeout");
            }
            return error_response(StatusCode::BAD_GATEWAY, &last_error);
        }
        Err(crate::services::retry::RetryError::BudgetExhausted {
            last_error,
            active_retries,
        }) => {
            // Retry-budget refusal — the first attempt failed but the
            // global cap on concurrent retries prevented another. Surface
            // as 503 so callers can fail over.
            //
            // This used to add "won't hit the MCP server today (we
            // pass None budget from this call site)". The call site passes
            // `Some(&state.retry_budget)` and has for some time, so the arm is
            // live and this is a real refusal an operator can see in the logs —
            // not, as the comment claimed, dead code kept for exhaustiveness.
            let duration = start.elapsed().as_millis() as f64;
            tracing::warn!(
                server_id,
                active_retries,
                error = %last_error,
                "MCP retry budget exhausted — 503 returned"
            );
            proxy_metrics::record_request("mcp", method.as_str(), 503, "mcp");
            proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration / 1000.0);
            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&target_url),
                Some(server_id),
                503,
                duration,
                false,
                Some(&format!("retry_budget_exhausted: {last_error}")),
                &correlation_id,
            );
            log.user_agent = extract_user_agent(&headers);
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    server_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);
            return error_response(StatusCode::SERVICE_UNAVAILABLE, "Retry budget exhausted");
        }
        Err(crate::services::retry::RetryError::NonRetryable(response)) => {
            // Non-retryable response (4xx etc) — pass through
            state
                .mcp_health
                .record_success(state.db.clone(), &tenant_id, server_id);
            Ok(response)
        }
    };

    let duration = start.elapsed().as_millis() as f64;

    match resp {
        Ok(upstream_resp) => {
            let status = upstream_resp.status().as_u16();
            let resp_headers = upstream_resp.headers().clone();

            // Check if this is an SSE stream response
            let is_sse = resp_headers
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .map(|ct| ct.contains("text/event-stream"))
                .unwrap_or(false);

            if is_sse {
                return handle_sse_stream(
                    upstream_resp,
                    status,
                    &resp_headers,
                    &correlation_id,
                    &auth_ctx,
                    is_mcp,
                    &method,
                    path,
                    &target_url,
                    server_id,
                    &server,
                    &body_bytes,
                    &headers,
                    guardrail_configs.clone(),
                    &state,
                    &tenant_id,
                    target_group.as_ref(),
                    start,
                    &capability_groups,
                    call_chain.clone(),
                    current_task_id_str.clone(),
                    trace_continuity,
                    trace_root_kind,
                    residency_decision.clone(),
                    server_region_for_audit.clone(),
                    in_flight,
                    exec_limits.max_payload_bytes,
                    deadline,
                )
                .await;
            }

            let mut resp_body = match crate::services::mcp_limits::read_capped(
                upstream_resp,
                exec_limits.max_payload_bytes,
                deadline,
            )
            .await
            {
                Ok(b) => b,
                Err(e) => {
                    return refuse_upstream_answer(
                        McpRowCtx {
                            state: &state,
                            auth_ctx: &auth_ctx,
                            headers: &headers,
                            method: method.as_str(),
                            target_url: &target_url,
                            server_id,
                            group_id: target_group.as_ref().map(|g| g.id.clone()),
                            correlation_id: &correlation_id,
                            start,
                            call_chain: &call_chain,
                            current_task_id_str: &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        },
                        e,
                        parsed_body.as_ref().and_then(|v| v.get("id")),
                    );
                }
            };

            tracing::debug!(
                status,
                resp_body_len = resp_body.len(),
                target_url = %target_url,
                duration_ms = duration,
                "MCP upstream response received"
            );

            // =========================================================================
            // Capability-level filtering on list responses (multi-group allow-wins)
            // =========================================================================
            if is_mcp && (200..300).contains(&status) {
                if let Ok(req_parsed) = serde_json::from_slice::<Value>(&body_bytes) {
                    let rpc_method = req_parsed
                        .get("method")
                        .and_then(|v| v.as_str())
                        .unwrap_or("");

                    // Helper: filter an array in the response JSON by capability name
                    // Works for both wrapped (result.key) and top-level (key) responses
                    macro_rules! filter_list_response {
                        ($resp_json:expr, $json_key:expr, $cap_type:expr, $name_field:expr, $enabled_set:expr) => {
                            let mut filtered = false;
                            if let Some(result) = $resp_json.get_mut("result") {
                                if let Some(items) =
                                    result.get_mut($json_key).and_then(|t| t.as_array_mut())
                                {
                                    items.retain(|item| {
                                        item.get($name_field)
                                            .and_then(|n| n.as_str())
                                            .map(|n| $enabled_set.contains(n))
                                            .unwrap_or(true)
                                    });
                                    filtered = true;
                                }
                            }
                            if !filtered {
                                if let Some(items) =
                                    $resp_json.get_mut($json_key).and_then(|t| t.as_array_mut())
                                {
                                    items.retain(|item| {
                                        item.get($name_field)
                                            .and_then(|n| n.as_str())
                                            .map(|n| $enabled_set.contains(n))
                                            .unwrap_or(true)
                                    });
                                }
                            }
                        };
                    }

                    // Remember what the upstream announced, before any
                    // group filter, so later tools/call rows can say which
                    // definition was in force.
                    if rpc_method == "initialize" || rpc_method == "tools/list" {
                        if let Ok(v) = serde_json::from_slice::<Value>(&resp_body) {
                            if rpc_method == "initialize" {
                                crate::services::tool_provenance::record_initialize(&server.id, &v);
                            } else {
                                crate::services::tool_provenance::record_tools_list(&server.id, &v);
                            }
                        }
                    }

                    // tools/list
                    if rpc_method == "tools/list" {
                        if let Some(enabled) = usage::get_enabled_capabilities(
                            &state.db,
                            &server.id,
                            "tool",
                            &capability_groups,
                        )
                        .await
                        {
                            if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                                filter_list_response!(resp_json, "tools", "tool", "name", enabled);
                                resp_body = bytes::Bytes::from(
                                    serde_json::to_vec(&resp_json).unwrap_or_default(),
                                );
                            }
                        }
                    }

                    // resources/list
                    if rpc_method == "resources/list" {
                        if let Some(enabled) = usage::get_enabled_capabilities(
                            &state.db,
                            &server.id,
                            "resource",
                            &capability_groups,
                        )
                        .await
                        {
                            if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                                filter_list_response!(
                                    resp_json,
                                    "resources",
                                    "resource",
                                    "uri",
                                    enabled
                                );
                                resp_body = bytes::Bytes::from(
                                    serde_json::to_vec(&resp_json).unwrap_or_default(),
                                );
                            }
                        }
                    }

                    // prompts/list
                    if rpc_method == "prompts/list" {
                        if let Some(enabled) = usage::get_enabled_capabilities(
                            &state.db,
                            &server.id,
                            "prompt",
                            &capability_groups,
                        )
                        .await
                        {
                            if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                                filter_list_response!(
                                    resp_json, "prompts", "prompt", "name", enabled
                                );
                                resp_body = bytes::Bytes::from(
                                    serde_json::to_vec(&resp_json).unwrap_or_default(),
                                );
                            }
                        }
                    }

                    // resources/templates/list
                    if rpc_method == "resources/templates/list" {
                        if let Some(enabled) = usage::get_enabled_capabilities(
                            &state.db,
                            &server.id,
                            "resource_template",
                            &capability_groups,
                        )
                        .await
                        {
                            if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                                filter_list_response!(
                                    resp_json,
                                    "resourceTemplates",
                                    "resource_template",
                                    "uriTemplate",
                                    enabled
                                );
                                resp_body = bytes::Bytes::from(
                                    serde_json::to_vec(&resp_json).unwrap_or_default(),
                                );
                            }
                        }
                    }
                }
            }

            // =========================================================================
            // Output-side guardrails (POLICY-13 — response redaction / blocking)
            //
            // Run check_output on the JSON-RPC `result.content[*].text`
            // fields if (a) the surface toggle `mcp_output_enabled` is
            // on for any active config, (b) the response is JSON (not
            // SSE — streaming output redaction needs per-chunk
            // rewriting which we don't do yet), and (c) the status is
            // 2xx (don't redact error bodies). On Block we return a
            // JSON-RPC error with `data.direction = "output"` so a
            // client can tell its outbound tool call was forbidden by
            // upstream response content. On Redacted we replace each
            // text field in place and re-serialise resp_body — the
            // existing logging block downstream captures the redacted
            // form in response_body_preview automatically.
            //
            // The surface gate inside `check_output` -> `evaluate` ->
            // `surface_enabled` does its own filter by direction +
            // mcp_output_enabled, so we don't need to pre-gate
            // configs here.
            if is_mcp
                && (200..300).contains(&status)
                && !guardrail_configs.is_empty()
                && !resp_body.is_empty()
                && !response_content_type_is_event_stream(&resp_headers)
            {
                if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                    // Collect each text-content slot's indexed path so
                    // we can rewrite in place after the check. MCP
                    // tool results use {result: {content: [{type:
                    // "text", text: "..."}]}}; we only touch text
                    // entries — image / resource refs are passed
                    // through unchanged.
                    let mut text_indices: Vec<usize> = Vec::new();
                    let mut combined = String::new();
                    if let Some(content) = resp_json
                        .get("result")
                        .and_then(|r| r.get("content"))
                        .and_then(|c| c.as_array())
                    {
                        for (i, item) in content.iter().enumerate() {
                            if item.get("type").and_then(|t| t.as_str()) == Some("text") {
                                if let Some(t) = item.get("text").and_then(|t| t.as_str()) {
                                    if !combined.is_empty() {
                                        combined.push('\n');
                                    }
                                    combined.push_str(t);
                                    text_indices.push(i);
                                }
                            }
                        }
                    }

                    if !combined.is_empty() {
                        let outcome = guardrails::check_output(
                            &combined,
                            &guardrail_configs,
                            "mcp",
                            &state.http_client,
                        )
                        .await;
                        match outcome {
                            guardrails::CheckOutcome::Block(violation) => {
                                let duration = start.elapsed().as_secs_f64() * 1000.0;
                                tracing::warn!(
                                    check = %violation.check,
                                    config = %violation.config_name,
                                    message = %violation.message,
                                    "Guardrail violation on MCP output — 403 returned"
                                );
                                proxy_metrics::record_guardrail_block("mcp", &violation.check);
                                proxy_metrics::record_request("mcp", method.as_str(), 403, "n/a");

                                let rpc_id = extract_rpc_id(&body_bytes);
                                let rpc_body =
                                    guardrail_violation_body(&violation, rpc_id.as_ref(), "output");
                                let rejection_body_str =
                                    serde_json::to_string(&rpc_body).unwrap_or_default();

                                let mut log = proxy_logger::build_proxy_log(
                                    "mcp",
                                    &auth_ctx,
                                    method.as_str(),
                                    path.unwrap_or("/"),
                                    Some(&target_url),
                                    Some(server_id),
                                    403,
                                    duration,
                                    false,
                                    Some(&format!(
                                        "Guardrail violation on output ({}): {}",
                                        violation.check, violation.message
                                    )),
                                    &correlation_id,
                                );
                                log.user_agent = extract_user_agent(&headers);
                                log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
                                log.request_size_bytes = Some(body_bytes.len() as i32);
                                log.response_size_bytes = Some(rejection_body_str.len() as i32);
                                let req_preview = String::from_utf8_lossy(&body_bytes);
                                log.request_body_preview =
                                    Some(proxy_logger::body_preview(&req_preview));
                                log.response_body_preview =
                                    Some(proxy_logger::body_preview(&rejection_body_str));
                                log.mcp_method = mcp_inner_method.clone();
                                log.mcp_capability_name = current_tool_name.clone();
                                if let Some(id) = rpc_id.as_ref() {
                                    log.mcp_request_id = Some(if id.is_string() {
                                        id.as_str().unwrap_or("").to_string()
                                    } else {
                                        id.to_string()
                                    });
                                }
                                proxy_logger::apply_call_chain(
                                    &mut log,
                                    &call_chain,
                                    &current_task_id_str,
                                    trace_continuity,
                                    trace_root_kind,
                                );
                                match &residency_decision {
                                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                                    None => proxy_logger::apply_residency_not_evaluated(
                                        &mut log,
                                        server_region_for_audit.as_deref(),
                                    ),
                                }
                                // MCP
                                // output-guardrail block. Same
                                // rationale as the twin input sites.
                                proxy_logger::apply_guardrail_violation(&mut log, &violation);
                                if violation.check == "pii" {
                                    proxy_logger::apply_pii_detected(&mut log);
                                }
                                state
                                    .background_writer
                                    .write_proxy_log(state.db.clone(), log);

                                return (StatusCode::FORBIDDEN, Json(rpc_body)).into_response();
                            }
                            guardrails::CheckOutcome::Redacted {
                                content: redacted,
                                warnings,
                            } => {
                                if warnings.iter().any(|w| w.check == "pii") {
                                    input_pii_detected = true;
                                }
                                for w in &warnings {
                                    tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, direction="output", "{}", w.message);
                                }
                                // Split the redacted blob back on the
                                // separator we joined with. Falls back
                                // to overwriting only the first text
                                // entry if the split count drifted
                                // (shouldn't happen — the detector
                                // doesn't add or drop newlines — but
                                // belt-and-braces beats panicking on
                                // a live response).
                                let parts: Vec<&str> = redacted.split('\n').collect();
                                if parts.len() == text_indices.len() {
                                    if let Some(content) = resp_json
                                        .get_mut("result")
                                        .and_then(|r| r.get_mut("content"))
                                        .and_then(|c| c.as_array_mut())
                                    {
                                        for (slot, part) in text_indices.iter().zip(parts.iter()) {
                                            if let Some(item) = content.get_mut(*slot) {
                                                if let Some(obj) = item.as_object_mut() {
                                                    obj.insert(
                                                        "text".into(),
                                                        Value::String((*part).to_string()),
                                                    );
                                                }
                                            }
                                        }
                                    }
                                } else if let Some(content) = resp_json
                                    .get_mut("result")
                                    .and_then(|r| r.get_mut("content"))
                                    .and_then(|c| c.as_array_mut())
                                {
                                    if let Some(first_idx) = text_indices.first() {
                                        if let Some(item) = content.get_mut(*first_idx) {
                                            if let Some(obj) = item.as_object_mut() {
                                                obj.insert(
                                                    "text".into(),
                                                    Value::String(redacted.clone()),
                                                );
                                            }
                                        }
                                    }
                                }
                                resp_body = bytes::Bytes::from(
                                    serde_json::to_vec(&resp_json).unwrap_or_default(),
                                );
                            }
                            guardrails::CheckOutcome::Warn { warnings } => {
                                if warnings.iter().any(|w| w.check == "pii") {
                                    input_pii_detected = true;
                                }
                                for w in &warnings {
                                    tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, direction="output", "{}", w.message);
                                }
                            }
                            guardrails::CheckOutcome::Pass => {}
                        }
                    }
                }
            }

            // =================================================================
            // MRTR governance (SEP-2322).
            //
            // An `input_required` result is the upstream asking the USER
            // for something mid-call — the first time elicitation is a
            // plain response a gateway can govern. Two controls:
            //   (a) requestState size cap — opaque, replayable blobs
            //       through a governance proxy get a stated ceiling
            //       (BRUTOR_MCP_REQUEST_STATE_MAX_BYTES, default 64 KiB);
            //   (b) the mcp-output guardrails run over each elicitation
            //       message before it reaches a user: Block → JSON-RPC
            //       error; Redacted → messages rewritten in place.
            // Run as a separate pass from the content guardrail above —
            // MRTR responses are rare, and a distinct pass keeps the
            // redaction path-mapping trivial (message keys, not mixed
            // content slots).
            let mut mrtr_input_required = false;
            let mut task_handle_returned = false;
            if is_mcp
                && (200..300).contains(&status)
                && !resp_body.is_empty()
                && !response_content_type_is_event_stream(&resp_headers)
            {
                if let Ok(mut resp_json) = serde_json::from_slice::<Value>(&resp_body) {
                    // Tasks extension: audit marker
                    // when a tools/call returns a task handle instead of
                    // a synchronous result; tasks/get / tasks/cancel
                    // themselves pass through and are logged normally.
                    task_handle_returned = resp_json.pointer("/result/task/taskId").is_some();
                    if resp_json
                        .pointer("/result/resultType")
                        .and_then(|v| v.as_str())
                        == Some(crate::constants::mcp_protocol::result_type::INPUT_REQUIRED)
                    {
                        mrtr_input_required = true;

                        // (a) requestState ceiling.
                        let state_len = resp_json
                            .pointer("/result/requestState")
                            .and_then(|v| v.as_str())
                            .map(|s| s.len())
                            .unwrap_or(0);
                        if state_len > state.config.mcp_request_state_max_bytes {
                            tracing::warn!(
                                mcp_server_id = %server_id,
                                request_state_bytes = state_len,
                                cap = state.config.mcp_request_state_max_bytes,
                                "MRTR requestState exceeds gateway ceiling — response rejected"
                            );
                            proxy_metrics::record_request("mcp", method.as_str(), 502, "mcp");
                            let rpc_id = extract_rpc_id(&body_bytes);
                            return (
                                StatusCode::BAD_GATEWAY,
                                Json(json!({
                                    "jsonrpc": "2.0",
                                    "id": rpc_id.unwrap_or(Value::Null),
                                    "error": {
                                        "code": -32000,
                                        "message": format!(
                                            "Upstream requestState ({state_len} bytes) exceeds the \
                                             gateway ceiling ({} bytes)",
                                            state.config.mcp_request_state_max_bytes
                                        )
                                    }
                                })),
                            )
                                .into_response();
                        }

                        // (b) guardrail the elicitation messages.
                        if !guardrail_configs.is_empty() {
                            let mut keys: Vec<String> = Vec::new();
                            let mut combined = String::new();
                            if let Some(reqs) = resp_json
                                .pointer("/result/inputRequests")
                                .and_then(|r| r.as_object())
                            {
                                for (key, req) in reqs {
                                    if let Some(msg) =
                                        req.pointer("/params/message").and_then(|m| m.as_str())
                                    {
                                        if !combined.is_empty() {
                                            combined.push('\n');
                                        }
                                        combined.push_str(msg);
                                        keys.push(key.clone());
                                    }
                                }
                            }
                            if !combined.is_empty() {
                                match guardrails::check_output(
                                    &combined,
                                    &guardrail_configs,
                                    "mcp",
                                    &state.http_client,
                                )
                                .await
                                {
                                    guardrails::CheckOutcome::Block(violation) => {
                                        tracing::warn!(
                                            check = %violation.check,
                                            config = %violation.config_name,
                                            "Guardrail violation on MRTR elicitation message — 403"
                                        );
                                        proxy_metrics::record_guardrail_block(
                                            "mcp",
                                            &violation.check,
                                        );
                                        let rpc_id = extract_rpc_id(&body_bytes);
                                        let rpc_body = guardrail_violation_body(
                                            &violation,
                                            rpc_id.as_ref(),
                                            "output",
                                        );
                                        return (StatusCode::FORBIDDEN, Json(rpc_body))
                                            .into_response();
                                    }
                                    guardrails::CheckOutcome::Redacted {
                                        content: redacted,
                                        warnings,
                                    } => {
                                        if warnings.iter().any(|w| w.check == "pii") {
                                            input_pii_detected = true;
                                        }
                                        let parts: Vec<&str> = redacted.split('\n').collect();
                                        if parts.len() == keys.len() {
                                            if let Some(reqs) = resp_json
                                                .pointer_mut("/result/inputRequests")
                                                .and_then(|r| r.as_object_mut())
                                            {
                                                for (key, part) in keys.iter().zip(parts.iter()) {
                                                    if let Some(msg) =
                                                        reqs.get_mut(key).and_then(|r| {
                                                            r.pointer_mut("/params/message")
                                                        })
                                                    {
                                                        *msg = Value::String((*part).to_string());
                                                    }
                                                }
                                            }
                                            resp_body = bytes::Bytes::from(
                                                serde_json::to_vec(&resp_json).unwrap_or_default(),
                                            );
                                        }
                                    }
                                    guardrails::CheckOutcome::Warn { warnings } => {
                                        for w in &warnings {
                                            tracing::warn!(target: "guardrails", check=%w.check, direction="output", "MRTR elicitation: {}", w.message);
                                        }
                                    }
                                    guardrails::CheckOutcome::Pass => {}
                                }
                            }
                        }
                    }
                }
            }

            // Background logging
            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&target_url),
                Some(server_id),
                status,
                duration,
                (200..300).contains(&status),
                None,
                &correlation_id,
            );

            let ua = extract_user_agent(&headers);
            log.proxy_subtype = Some(resolve_proxy_subtype(&headers, is_mcp));
            log.user_agent = ua.clone();
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            log.request_size_bytes = Some(body_bytes.len() as i32);
            log.response_size_bytes = Some(resp_body.len() as i32);
            log.request_headers = Some(proxy_logger::sanitize_headers(&headers));
            let resp_hdr_str = {
                let mut map = serde_json::Map::new();
                for (k, v) in resp_headers.iter() {
                    if let Ok(vs) = v.to_str() {
                        map.insert(k.as_str().to_string(), serde_json::json!(vs));
                    }
                }
                serde_json::to_string(&map).unwrap_or_default()
            };
            log.response_headers = Some(resp_hdr_str);

            // Request/response body previews
            let req_preview = String::from_utf8_lossy(&body_bytes);
            log.request_body_preview = Some(body_preview(&req_preview));
            let resp_preview = String::from_utf8_lossy(&resp_body);
            log.response_body_preview = Some(body_preview(&resp_preview));

            // Extract MCP method, request ID, and capability name if JSON-RPC
            let mut log_mcp_method: Option<String> = None;
            let mut log_mcp_capability: Option<String> = None;
            if is_mcp {
                if let Ok(parsed) = serde_json::from_slice::<Value>(&body_bytes) {
                    log_mcp_method = parsed
                        .get("method")
                        .and_then(|v| v.as_str())
                        .map(String::from);
                    log.mcp_method = log_mcp_method.clone();
                    log.mcp_request_id = parsed.get("id").map(|v| {
                        if v.is_string() {
                            v.as_str().unwrap().to_string()
                        } else {
                            v.to_string()
                        }
                    });
                    log_mcp_capability = parsed
                        .get("params")
                        .and_then(|p| p.get("name"))
                        .and_then(|v| v.as_str())
                        .map(String::from);
                    log.mcp_capability_name = log_mcp_capability.clone();
                }
            }

            // Detect a GET that came back without an
            // `text/event-stream` content type — i.e. the upstream
            // doesn't speak the optional MCP SSE listener channel
            // (AWS Knowledge MCP returns the GitHub README HTML on
            // GET, for example). Mark the server in the negative
            // cache so subsequent SSE-listen GETs short-circuit at
            // the gate, and rewrite this response to 405 so the
            // calling SDK stops reconnecting (405 is the
            // documented "stop reopening the listener" signal).
            //
            // Only fires for GETs that *did* advertise
            // `Accept: text/event-stream` — pure browser tabs are
            // already rejected at the early gate before we ever
            // forward upstream, so they don't reach this branch.
            let upstream_was_not_event_stream = method == axum::http::Method::GET
                && accept_includes_event_stream(&headers)
                && !response_content_type_is_event_stream(&resp_headers);
            if upstream_was_not_event_stream {
                state
                    .non_sse_mcp_servers
                    .insert(server.id.clone(), std::time::Instant::now());

                // Persist the determination so other replicas
                // (and restarts of this one) short-circuit at
                // the gate without paying the probe round-trip
                // again. Best-effort — the in-memory cache
                // above is enough to break the loop within this
                // replica even if the UPDATE fails.
                let db = state.db.clone();
                let server_id_for_update = server.id.clone();
                tokio::spawn(async move {
                    if let Err(e) =
                        sqlx::query("UPDATE mcp_servers SET wants_listener = false WHERE id = $1")
                            .bind(&server_id_for_update)
                            .execute(&db)
                            .await
                    {
                        tracing::warn!(
                            mcp_server_id = %server_id_for_update,
                            error = %e,
                            "Failed to persist wants_listener=false (in-memory cache still active)"
                        );
                    }
                });

                tracing::info!(
                    server_id = %server.id,
                    server_name = %server.name,
                    upstream_status = status,
                    "MCP server does not speak SSE listener; cached as non-SSE for {}s and persisted to DB",
                    NON_SSE_CACHE_TTL.as_secs()
                );

                // Write one proxy_log row tagged so admins can see
                // the probe in the audit UI; skip proxy_metric and
                // usage_log so the headline counts stay clean.
                log.proxy_subtype =
                    Some(crate::constants::proxy_subtype::NON_SSE_PROBE.to_string());
                proxy_logger::apply_call_chain(
                    &mut log,
                    &call_chain,
                    &current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                match &residency_decision {
                    Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                    None => proxy_logger::apply_residency_not_evaluated(
                        &mut log,
                        server_region_for_audit.as_deref(),
                    ),
                }
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);

                proxy_metrics::record_request("mcp", method.as_str(), 405, "mcp_non_sse_probe");
                proxy_metrics::record_latency(
                    "mcp",
                    method.as_str(),
                    "mcp_non_sse_probe",
                    duration / 1000.0,
                );

                return error_response(
                    StatusCode::METHOD_NOT_ALLOWED,
                    "Upstream MCP server does not support the SSE listener channel. Use POST for JSON-RPC requests.",
                );
            }

            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    server_region_for_audit.as_deref(),
                ),
            }
            // Input/output guardrail
            // redacted/warned on a PII category but didn't block;
            // stamp the success row so HIPAA PHI / GDPR PII fire.
            if input_pii_detected {
                proxy_logger::apply_pii_detected(&mut log);
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            let mcp_metric_data = serde_json::json!({
                "method": log_mcp_method,
                "server_name": server.name,
                "tool_name": log_mcp_capability,
                // MRTR audit marker: this response
                // asked the user for input mid-call (SEP-2322).
                "mrtr_input_required": mrtr_input_required,
                // Tasks extension: the call returned
                // a task handle (long-running work, driven by tasks/get).
                "task_handle_returned": task_handle_returned,
            });
            let req_hdr_count = headers.len() as i32;
            let resp_hdr_count = resp_headers.len() as i32;
            let metric = proxy_logger::build_proxy_metric(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                "mcp_proxy",
                Some(server_id),
                status,
                duration,
                (200..300).contains(&status),
                &correlation_id,
                Some(body_bytes.len() as i32),
                Some(resp_body.len() as i32),
                Some(req_hdr_count),
                Some(resp_hdr_count),
                ua.as_deref(),
                None,
                Some(mcp_metric_data),
            );
            state
                .background_writer
                .write_proxy_metric(state.db.clone(), metric);

            // Prometheus metrics
            proxy_metrics::record_request("mcp", method.as_str(), status, "mcp");
            proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration / 1000.0);

            // Background: usage log
            if (200..300).contains(&status) {
                // Use the actual capability name (e.g. "search_crm_objects") for tool calls,
                // falling back to the JSON-RPC method (e.g. "tools/call") for non-tool requests.
                let mcp_tool = log_mcp_capability.as_deref().or(log_mcp_method.as_deref());
                usage::write_mcp_usage_log(
                    &state.background_writer,
                    state.db.clone(),
                    &tenant_id,
                    &auth_ctx,
                    target_group.as_ref(),
                    &server.id,
                    &server.name,
                    mcp_tool,
                    &target_url,
                    method.as_str(),
                    &correlation_id,
                    status,
                    duration,
                    Some(body_bytes.len() as i32),
                    Some(resp_body.len() as i32),
                );
                if let Some(ref g) = target_group {
                    usage::spawn_alert_evaluation(
                        state.db.clone(),
                        tenant_id.clone(),
                        g.clone(),
                        "mcp".into(),
                        Some(correlation_id.clone()),
                    );
                }
            }

            // Build response
            let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
            let mut response = axum::response::Response::builder().status(axum_status);

            // Forward response headers (strip encoding headers since we decompress upstream)
            for (key, value) in resp_headers.iter() {
                let key_str = key.as_str().to_lowercase();
                if !matches!(
                    key_str.as_str(),
                    "transfer-encoding"
                        | "connection"
                        | "keep-alive"
                        | "content-encoding"
                        | "content-length"
                ) {
                    response = response.header(key.as_str(), value);
                }
            }

            response =
                response.header(crate::constants::headers::X_CORRELATION_ID, &correlation_id);
            response = response.header(
                crate::constants::headers::X_AUTH_TYPE,
                auth_ctx.auth_type.to_string(),
            );
            response = response.header(
                crate::constants::headers::X_REQUEST_TYPE,
                if is_mcp { "mcp" } else { "http" },
            );

            response.body(Body::from(resp_body)).unwrap()
        }
        Err(e) => {
            // This path is only reached if retry returned Ok() but somehow we got
            // an Err here — should not happen with the retry integration above,
            // but kept for safety.
            tracing::debug!(
                error = %e,
                target_url = %target_url,
                is_timeout = e.is_timeout(),
                is_connect = e.is_connect(),
                "MCP upstream request failed (post-retry)"
            );
            state.mcp_health.record_failure(
                state.db.clone(),
                &tenant_id,
                server_id,
                &e.to_string(),
            );

            let mut log = proxy_logger::build_proxy_log(
                "mcp",
                &auth_ctx,
                method.as_str(),
                path.unwrap_or("/"),
                Some(&target_url),
                Some(server_id),
                502,
                duration,
                false,
                Some(&e.to_string()),
                &correlation_id,
            );
            log.user_agent = extract_user_agent(&headers);
            log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
            log.request_size_bytes = Some(body_bytes.len() as i32);
            let req_preview = String::from_utf8_lossy(&body_bytes);
            log.request_body_preview = Some(body_preview(&req_preview));
            proxy_logger::apply_call_chain(
                &mut log,
                &call_chain,
                &current_task_id_str,
                trace_continuity,
                trace_root_kind,
            );
            match &residency_decision {
                Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                None => proxy_logger::apply_residency_not_evaluated(
                    &mut log,
                    server_region_for_audit.as_deref(),
                ),
            }
            state
                .background_writer
                .write_proxy_log(state.db.clone(), log);

            let err_status = if e.is_timeout() { 504u16 } else { 502 };
            proxy_metrics::record_request("mcp", method.as_str(), err_status, "mcp");
            proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration / 1000.0);

            if e.is_timeout() {
                error_response(StatusCode::GATEWAY_TIMEOUT, "MCP server timeout")
            } else if e.is_connect() {
                error_response(StatusCode::BAD_GATEWAY, "Cannot connect to MCP server")
            } else {
                error_response(StatusCode::BAD_GATEWAY, &e.to_string())
            }
        }
    }
}

/// Handle a request to a system-managed virtual MCP server.
///
/// Reads the request body, delegates to `system_server::handle_request()`,
/// then writes proxy logs/metrics/usage and returns the response.
#[allow(clippy::too_many_arguments)]
async fn handle_system_server(
    state: &SharedState,
    method: &Method,
    headers: &HeaderMap,
    auth_ctx: &crate::models::auth::AuthContext,
    server: &McpServer,
    tenant_id: &str,
    target_group: Option<&crate::models::db::ResourceGroup>,
    body: Body,
    server_id: &str,
    path: Option<&str>,
    correlation_id: &str,
    enforcement: &crate::models::proxy::UsageEnforcementResult,
    start: Instant,
    call_chain: &CallChain,
    current_task_id_str: &str,
    trace_continuity: TraceContinuity,
    trace_root_kind: Option<&'static str>,
    residency_decision: &Option<crate::services::residency::ResidencyDecision>,
    server_region_for_audit: &Option<String>,
    // The request's admission holds: committed when the system tool runs,
    // given back (Drop) if anything before it refuses.
    mut mcp_holds: Vec<crate::services::cache::usage_state::HoldGuard>,
    // The call's limit documents and execution limits (read once by the
    // route).
    mcp_limit_sources: &crate::services::mcp_limits::McpLimitSources,
    exec_limits: &crate::services::mcp_limits::McpExecLimits,
) -> Response {
    // The request body, under the same ceiling as external proxying.
    let body_limit = exec_limits.request_body_limit();
    let body_bytes = match axum::body::to_bytes(body, body_limit).await {
        Ok(b) => b,
        Err(_) => {
            return refuse_request_payload(
                McpRowCtx {
                    state,
                    auth_ctx,
                    headers,
                    method: method.as_str(),
                    target_url: &build_target_url(server, path),
                    server_id,
                    group_id: target_group.map(|g| g.id.clone()),
                    correlation_id,
                    start,
                    call_chain,
                    current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                },
                body_limit,
            );
        }
    };

    // The same one reading as the proxy path: a batch, a repeated
    // key or text that is not one JSON value is refused, so that one rule
    // holds on every MCP surface. `parsed_rpc` is that reading, and the only
    // one: the guardrail input, the run controls, the rate limits, the
    // argument policy, the approval hold and the system server itself all
    // read it, never the bytes again (`tests/mcp_one_reading_guard.rs`).
    // The bytes are kept for sizes and audit previews.
    let (body_bytes, parsed_rpc): (axum::body::Bytes, Option<Value>) =
        match crate::services::jsonrpc_body::read(&body_bytes) {
            crate::services::jsonrpc_body::RpcBody::Message { value, bytes } => {
                (body_bytes.slice_ref(bytes), Some(value))
            }
            crate::services::jsonrpc_body::RpcBody::Batch => return batch_refused(),
            crate::services::jsonrpc_body::RpcBody::Unreadable(reason) => {
                return unreadable_refused(reason);
            }
            crate::services::jsonrpc_body::RpcBody::Other(value) => (body_bytes, value),
        };
    let rpc_id: Option<Value> = parsed_rpc.as_ref().and_then(|v| v.get("id").cloned());

    // Guardrails — check MCP input (same as external path)
    let guardrail_configs = match state
        .guardrails_cache
        .get_configs(
            tenant_id,
            auth_ctx.guardrail_user_id().as_deref(),
            auth_ctx.guardrail_group_id(),
            &state.db,
        )
        .await
    {
        Ok(c) => c,
        // Fail closed — see `configs_unavailable`.
        Err(e) => {
            tracing::error!(error = %e, "guardrail configs unavailable; refusing the system-server call");
            return refuse_unchecked_mcp(
                McpRowCtx {
                    state,
                    auth_ctx,
                    headers,
                    method: method.as_str(),
                    target_url: &build_target_url(server, path),
                    server_id,
                    group_id: target_group.map(|g| g.id.clone()),
                    correlation_id,
                    start,
                    call_chain,
                    current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                },
                "guardrails_unavailable: refusing to proxy a call we cannot check",
            );
        }
    };

    // PII detected on a PASSED
    // (warn/redact) system-server request; stamped on the success
    // row below. Mirrors the external-MCP path.
    let mut input_pii_detected = false;
    // The `mcp-rate` hold when it carries concurrency slots: held until the
    // tool has run.
    let mut slot_hold: Option<crate::services::cache::usage_state::HoldGuard> = None;

    if !guardrail_configs.is_empty() && !body_bytes.is_empty() {
        let input_text = parsed_rpc
            .as_ref()
            .and_then(|parsed| {
                parsed.get("params").map(|p| {
                    if let Some(s) = p.as_str() {
                        s.to_string()
                    } else {
                        p.to_string()
                    }
                })
            })
            .unwrap_or_default();

        if !input_text.is_empty() {
            match guardrails::check_input(
                &input_text,
                &guardrail_configs,
                "mcp",
                &state.http_client,
            )
            .await
            {
                guardrails::CheckOutcome::Block(violation) => {
                    let duration = start.elapsed().as_secs_f64() * 1000.0;
                    tracing::warn!(
                        check = %violation.check,
                        config = %violation.config_name,
                        message = %violation.message,
                        "Guardrail violation — 403 returned"
                    );
                    proxy_metrics::record_guardrail_block("mcp", &violation.check);
                    proxy_metrics::record_request("mcp", "POST", 403, "n/a");

                    // Same JSON-RPC envelope + full audit-row
                    // population as the external MCP path above. The
                    // system-server route is also JSON-RPC so the
                    // spec rules are identical; the body capture
                    // matters even more for these calls since the
                    // system server hosts internal tools the
                    // operator has chosen to expose.
                    let inner_method = parsed_rpc
                        .as_ref()
                        .and_then(|p| p.get("method"))
                        .and_then(|m| m.as_str())
                        .map(String::from);
                    let inner_tool = parsed_rpc
                        .as_ref()
                        .and_then(|p| p.get("params"))
                        .and_then(|p| p.get("name"))
                        .and_then(|n| n.as_str())
                        .map(String::from);
                    let rpc_body = guardrail_violation_body(&violation, rpc_id.as_ref(), "input");
                    let rejection_body_str = serde_json::to_string(&rpc_body).unwrap_or_default();

                    // Same target_url / server_id / sizes population
                    // as the external MCP block above — the system-
                    // server path also has all of these in scope, no
                    // reason to leave the audit row half-empty.
                    let sys_target_url = build_target_url(server, path);
                    let mut log = proxy_logger::build_proxy_log(
                        "mcp",
                        &auth_ctx,
                        "POST",
                        "mcp-system-server",
                        Some(&sys_target_url),
                        Some(server_id),
                        403,
                        duration,
                        false,
                        Some(&format!(
                            "Guardrail violation ({}): {}",
                            violation.check, violation.message
                        )),
                        &correlation_id,
                    );
                    log.user_agent = extract_user_agent(&headers);
                    log.resource_group_id = target_group.map(|g| g.id.clone());
                    let req_preview = String::from_utf8_lossy(&body_bytes);
                    log.request_body_preview = Some(proxy_logger::body_preview(&req_preview));
                    log.response_body_preview =
                        Some(proxy_logger::body_preview(&rejection_body_str));
                    log.request_size_bytes = Some(body_bytes.len() as i32);
                    log.response_size_bytes = Some(rejection_body_str.len() as i32);
                    log.mcp_method = inner_method;
                    log.mcp_capability_name = inner_tool;
                    if let Some(id) = rpc_id.as_ref() {
                        log.mcp_request_id = Some(if id.is_string() {
                            id.as_str().unwrap_or("").to_string()
                        } else {
                            id.to_string()
                        });
                    }
                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    match &residency_decision {
                        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
                        None => proxy_logger::apply_residency_not_evaluated(
                            &mut log,
                            server_region_for_audit.as_deref(),
                        ),
                    }
                    // MCP guardrail
                    // block. SOC 2 CC7.2 + (when PII) HIPAA/GDPR.
                    proxy_logger::apply_guardrail_violation(&mut log, &violation);
                    if violation.check == "pii" {
                        proxy_logger::apply_pii_detected(&mut log);
                    }
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);

                    return (StatusCode::FORBIDDEN, Json(rpc_body)).into_response();
                }
                guardrails::CheckOutcome::Redacted {
                    content: _,
                    warnings,
                }
                | guardrails::CheckOutcome::Warn { warnings } => {
                    if warnings.iter().any(|w| w.check == "pii") {
                        input_pii_detected = true;
                    }
                    for w in &warnings {
                        tracing::warn!(target: "guardrails", check=%w.check, surface=%w.surface, "{}", w.message);
                    }
                }
                guardrails::CheckOutcome::Pass => {}
            }
        }
    }

    // Operator governance of the system server's TOOLS (the four
    // `skills__*` verbs, the `search__*` tools, …). The normal MCP per-tool
    // governance — per-tool rate limits + the `McpInput` argument policy — lives
    // on the external path AFTER the `is_system_server` early return, so it
    // never reached system-server tools. Wire the same two checks here, keyed on
    // the inner tool name (`params.name`), so an operator can rate-limit or
    // attach an argument policy to e.g. `skills__run_script` as a real MCP tool.
    // Skill-level governance (per-skill guardrails/quota/approval) still runs
    // inside the L3 handlers; this is the additional verb-level layer.
    //
    // The request's approvals: its `X-Approval-Token` headers and, for
    // `skills__run_script`, the tool's `approval_token` argument. One object
    // for the tool's gate here and the skill's gates behind it
    // (`agent_approval::RequestApprovals`).
    let run_script_arguments = parsed_rpc
        .as_ref()
        .filter(|p| p.get("method").and_then(|m| m.as_str()) == Some("tools/call"))
        .and_then(|p| p.get("params"))
        .filter(|p| {
            p.get("name")
                .and_then(|n| n.as_str())
                .is_some_and(system_server::is_run_script)
        })
        .and_then(|p| p.get("arguments"));
    let approvals = std::sync::Arc::new(
        agent_approval::RequestApprovals::from_headers(headers)
            .with(run_script_arguments.and_then(system_server::run_script_tokens)),
    );
    {
        let inner_method = parsed_rpc
            .as_ref()
            .and_then(|p| p.get("method"))
            .and_then(|m| m.as_str());
        if inner_method == Some("tools/call") {
            let inner_tool = parsed_rpc
                .as_ref()
                .and_then(|p| p.get("params"))
                .and_then(|p| p.get("name"))
                .and_then(|n| n.as_str())
                .map(String::from);

            // Run-scoped controls — the run budget and an operator's abort
            // — for EVERY tool of EVERY system server, before the rate
            // limits, the argument policy and anything a tool does.
            // Only `skills__run_script`
            // reached them (inside `execute_skill`); the other skill tools
            // and the whole Web Search server never did, so an aborted or
            // over-budget run went on loading skills and searching. Before
            // the argument policy, as on the proxy path: a stopped run must
            // not even queue an approval. One verdict, one shape — the same
            // refusal the proxy path answers for an external server.
            //
            // The tool call is one action of the run, whatever the tool
            // does (a script it runs is part of it). Its unit is held with
            // the request's other holds: kept when the tool is run, given
            // back if a later gate refuses or holds the call.
            let starter = crate::services::run_controls::run_starter(
                auth_ctx,
                trace_continuity == TraceContinuity::Verified,
            );
            let step = crate::services::run_controls::check(
                state,
                crate::services::run_controls::Step {
                    tenant_id,
                    group_id: target_group.map(|g| g.id.as_str()),
                    run_id: &call_chain.root_task_id.to_string(),
                    depth: call_chain.depth,
                    kind: crate::services::run_controls::StepKind::Action,
                    starter: starter.as_deref(),
                },
            )
            .await;
            let refusal = match step {
                Ok(admitted) => {
                    mcp_holds.extend(admitted.into_hold());
                    None
                }
                Err(refusal) => Some(refusal),
            };
            if let Some(refusal) = refusal {
                let duration = start.elapsed().as_secs_f64() * 1000.0;
                let sys_target_url = build_target_url(server, path);
                let mut log = proxy_logger::build_proxy_log(
                    "mcp",
                    auth_ctx,
                    method.as_str(),
                    "mcp-system-server",
                    Some(&sys_target_url),
                    Some(server_id),
                    refusal.status().as_u16(),
                    duration,
                    false,
                    Some(refusal.message()),
                    correlation_id,
                );
                log.user_agent = extract_user_agent(headers);
                log.resource_group_id = target_group.map(|g| g.id.clone());
                log.mcp_method = Some("tools/call".to_string());
                log.mcp_capability_name = inner_tool.clone();
                // A run refusal is `denied`, not a failed
                // call.
                log.proxy_subtype = Some(refusal.subtype().to_string());
                proxy_logger::apply_call_chain(
                    &mut log,
                    call_chain,
                    current_task_id_str,
                    trace_continuity,
                    trace_root_kind,
                );
                state
                    .background_writer
                    .write_proxy_log(state.db.clone(), log);
                // One run too many for the credential is a rate refusal,
                // answered as this branch answers a tool's rate limit.
                return match refusal.rate_block() {
                    Some(block) => (
                        StatusCode::TOO_MANY_REQUESTS,
                        Json(system_tool_rate_limit_body(block, rpc_id.as_ref())),
                    )
                        .into_response(),
                    None => error_response(refusal.status(), refusal.message()),
                };
            }

            // The run's data scope, as on the proxy path.
            // A system server's tools are MCP tools like any other:
            // a rule (or an `unscoped_tools` entry) that names `search__web`
            // or `skills__load` is judged here, under the tool's MCP name.
            // This branch left the route before the gate, so only a script
            // run was judged (in `execute_skill`, under the skill's name).
            let root_task_id = call_chain.root_task_id.to_string();
            let scope_tenant = target_group.map_or(tenant_id, |g| g.tenant_id.as_str());
            let conflicts = crate::services::data_scope::capture(
                &state.db,
                scope_tenant,
                &root_task_id,
                headers
                    .get(crate::services::data_scope::SCOPE_HEADER)
                    .and_then(|v| v.to_str().ok()),
            )
            .await;
            if let (Some(tool), Some(group)) = (inner_tool.as_deref(), target_group) {
                let arguments = parsed_rpc
                    .as_ref()
                    .and_then(|p| p.get("params"))
                    .and_then(|p| p.get("arguments"))
                    .unwrap_or(&Value::Null);
                let refusal = crate::services::data_scope::gate(
                    &state.db,
                    crate::services::data_scope::Call {
                        tenant_id: &group.tenant_id,
                        ai_system_id: &group.id,
                        tool,
                        arguments: &[arguments],
                        root_task_id: &root_task_id,
                        conflicts: &conflicts,
                        auth_ctx,
                        correlation_id,
                    },
                )
                .await;
                if let Some(crate::services::data_scope::Refusal { message, detail }) = refusal {
                    proxy_metrics::record_request("mcp", "POST", 403, "n/a");
                    let rpc_body = json!({
                        "jsonrpc": "2.0",
                        "id": rpc_id.clone().unwrap_or(json!(null)),
                        "error": {
                            "code": JSONRPC_GUARDRAIL_BLOCKED,
                            "message": message,
                            "data": { "data_scope": detail }
                        }
                    });
                    let body_str = serde_json::to_string(&rpc_body).unwrap_or_default();
                    let sys_target_url = build_target_url(server, path);
                    let mut log = proxy_logger::build_proxy_log(
                        "mcp",
                        auth_ctx,
                        "POST",
                        "mcp-system-server",
                        Some(&sys_target_url),
                        Some(server_id),
                        403,
                        start.elapsed().as_secs_f64() * 1000.0,
                        false,
                        Some(&message),
                        correlation_id,
                    );
                    log.user_agent = extract_user_agent(headers);
                    log.resource_group_id = Some(group.id.clone());
                    log.mcp_method = Some("tools/call".to_string());
                    log.mcp_capability_name = Some(tool.to_string());
                    log.request_body_preview = Some(proxy_logger::body_preview(
                        &String::from_utf8_lossy(&body_bytes),
                    ));
                    log.response_body_preview = Some(proxy_logger::body_preview(&body_str));
                    proxy_logger::apply_policy_deny(&mut log);
                    proxy_logger::apply_call_chain(
                        &mut log,
                        call_chain,
                        current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);
                    return (StatusCode::FORBIDDEN, Json(rpc_body)).into_response();
                }
            }

            let inner_args = parsed_rpc
                .as_ref()
                .and_then(|p| p.get("params"))
                .and_then(|p| p.get("arguments"))
                .cloned()
                .unwrap_or(Value::Null);

            if let (Some(tool), Some(group)) = (inner_tool.as_deref(), target_group) {
                // Per-tool rate limits (token bucket / fixed-window quota /
                // cooldown) and the concurrency allowances, same as the
                // external path, under a hold.
                let mut rate_hold = crate::services::limits::PendingHold::of(
                    crate::services::limits::HoldId::new(&tenant_id, "mcp-rate"),
                );
                let slots = exec_limits.slot_dims(
                    &group.tenant_id,
                    &group.id,
                    server_id,
                    &crate::services::mcp_limits::principal_key(auth_ctx),
                );
                let carries_slots = !slots.is_empty();
                let block = usage::admit_mcp_rate_limits(
                    &state.usage_state,
                    group,
                    server_id,
                    tool,
                    mcp_limit_sources,
                    slots,
                    Some(&mut rate_hold),
                )
                .await;
                let held = rate_hold.held().map(|h| state.usage_state.adopt_hold(h));
                if carries_slots {
                    slot_hold = held;
                } else {
                    mcp_holds.extend(held);
                }
                if let Some(block) = block {
                    let duration = start.elapsed().as_secs_f64() * 1000.0;
                    tracing::warn!(
                        server_id,
                        tool,
                        scope = block.scope,
                        "System-server tool rate limit exceeded — 429 returned"
                    );
                    proxy_metrics::record_request("mcp", "POST", 429, "n/a");
                    let rpc_body = system_tool_rate_limit_body(&block, rpc_id.as_ref());
                    let body_str = serde_json::to_string(&rpc_body).unwrap_or_default();
                    let sys_target_url = build_target_url(server, path);
                    let mut log = proxy_logger::build_proxy_log(
                        "mcp",
                        &auth_ctx,
                        "POST",
                        "mcp-system-server",
                        Some(&sys_target_url),
                        Some(server_id),
                        429,
                        duration,
                        false,
                        Some(&block.message),
                        &correlation_id,
                    );
                    log.user_agent = extract_user_agent(&headers);
                    log.resource_group_id = Some(group.id.clone());
                    log.proxy_subtype = Some(format!("rate_limit_{}", block.scope));
                    log.mcp_method = Some("tools/call".to_string());
                    log.mcp_capability_name = Some(tool.to_string());
                    log.response_body_preview = Some(proxy_logger::body_preview(&body_str));
                    proxy_logger::apply_call_chain(
                        &mut log,
                        &call_chain,
                        &current_task_id_str,
                        trace_continuity,
                        trace_root_kind,
                    );
                    state
                        .background_writer
                        .write_proxy_log(state.db.clone(), log);
                    return (StatusCode::TOO_MANY_REQUESTS, Json(rpc_body)).into_response();
                }

                // McpInput argument policy (SQL/URL/shell analyzers) on the args.
                if let Some(v) = crate::services::argument_policy::enforce(
                    &state.argument_policy_cache,
                    &state.db,
                    &group.tenant_id,
                    &group.id,
                    crate::services::argument_policy::policy::Surface::McpInput,
                    tool,
                    &inner_args,
                )
                .await
                {
                    use crate::services::argument_policy::policy::Severity;
                    // What stops the call, if anything does: the response,
                    // the bytes of it the audit row previews, the row's
                    // error text and subtype, and whether it is a policy
                    // denial.
                    let stopped: Option<(Response, String, String, Option<&'static str>, bool)> =
                        match v.severity {
                            Severity::Warn => {
                                tracing::warn!(
                                    server_id, tool, policy = %v.policy_name, rule = %v.rule,
                                    "System-server tool argument policy warn (logged, not blocked)"
                                );
                                None
                            }
                            Severity::Deny => {
                                tracing::warn!(
                                    server_id, tool, policy = %v.policy_name, rule = %v.rule,
                                    "System-server tool argument policy deny — 403 returned"
                                );
                                proxy_metrics::record_request("mcp", "POST", 403, "n/a");
                                let rpc_body = json!({
                                    "jsonrpc": "2.0",
                                    "id": rpc_id.clone().unwrap_or(json!(null)),
                                    "error": {
                                        "code": JSONRPC_GUARDRAIL_BLOCKED,
                                        "message": v.message,
                                        "data": {"argument_policy": {
                                            "policy": v.policy_name,
                                            "tool": v.tool,
                                            "argument": v.argument_key,
                                            "analyzer": v.analyzer,
                                            "rule": v.rule,
                                            "facts": v.facts,
                                        }}
                                    }
                                });
                                let preview = serde_json::to_string(&rpc_body).unwrap_or_default();
                                Some((
                                    (StatusCode::FORBIDDEN, Json(rpc_body)).into_response(),
                                    preview,
                                    format!(
                                        "Argument policy ({}/{}): {}",
                                        v.policy_name, v.rule, v.message
                                    ),
                                    None,
                                    true,
                                ))
                            }
                            // The band asks for a
                            // person. This arm did not exist: only `Deny` was
                            // acted on, so a rule that should hold the call
                            // let the tool run, with no log line. Same shared
                            // lifecycle as the MCP proxy: a valid one-time
                            // token (bound to these arguments) proceeds,
                            // otherwise the call is held and the envelope
                            // tells the caller where to poll.
                            Severity::ApprovalRequired => {
                                tracing::info!(
                                    server_id, tool, policy = %v.policy_name, rule = %v.rule,
                                    field = ?v.field, observed = ?v.observed,
                                    threshold = ?v.threshold,
                                    "System-server tool argument policy requires approval"
                                );
                                // What the call asks to do: its arguments
                                // (a script run's without its token
                                // argument). A script run passes the
                                // skill's own gates after this one; they
                                // are named here so both holds are raised
                                // in one answer.
                                let content = parsed_rpc
                                    .as_ref()
                                    .and_then(|p| p.get("params"))
                                    .and_then(|p| p.get("arguments"))
                                    .map(|a| match run_script_arguments {
                                        Some(_) => system_server::run_script_content(a),
                                        None => std::borrow::Cow::Borrowed(a),
                                    });
                                let skill_caller = system_server::with_tenant(auth_ctx, server);
                                let skill_run = RunScope::from_chain(
                                    call_chain,
                                    current_task_id_str,
                                    trace_root_kind,
                                );
                                let skill_request = run_script_arguments.map(|a| {
                                    system_server::run_script_request(a, correlation_id, &approvals)
                                });
                                let skill_gates = skill_request.as_ref().map(|request| {
                                    crate::services::skill_orchestrator::SkillGates {
                                        state,
                                        auth: &skill_caller,
                                        request,
                                        correlation_id,
                                        run: &skill_run,
                                    }
                                });
                                let ctx = agent_approval::ApprovalContext {
                                    db: &state.db,
                                    tenant_id,
                                    surface: "mcp",
                                    scope_ref: &server.name,
                                    capability_type: "tool",
                                    capability_name: tool,
                                    auth_ctx,
                                    groups: std::slice::from_ref(group),
                                    // The policy states no window: the hold
                                    // takes the group / tenant / platform
                                    // default (`approval_window`).
                                    policy_window_seconds: None,
                                    call_arguments: content.as_deref(),
                                    binding: None,
                                    run: Some(RunScope::from_chain(
                                        call_chain,
                                        current_task_id_str,
                                        trace_root_kind,
                                    )),
                                    extra_metadata: Some(v.approval_metadata()),
                                    run_clock: Some(&state.usage_state),
                                    approvals: &approvals,
                                    downstream: skill_gates
                                        .as_ref()
                                        .map(|g| g as &dyn agent_approval::Downstream),
                                };
                                match agent_approval::require_or_enqueue(&ctx).await {
                                    agent_approval::ApprovalOutcome::Proceed => None,
                                    agent_approval::ApprovalOutcome::Envelope(st, envelope) => {
                                        let preview =
                                            serde_json::to_string(&envelope).unwrap_or_default();
                                        Some((
                                            (st, Json(envelope)).into_response(),
                                            preview,
                                            "approval_required".to_string(),
                                            Some(constants::proxy_subtype::APPROVAL_REQUIRED),
                                            false,
                                        ))
                                    }
                                    agent_approval::ApprovalOutcome::Denied(msg) => {
                                        let msg = format!("Approval token invalid: {msg}");
                                        Some((
                                            error_response(StatusCode::FORBIDDEN, &msg),
                                            msg.clone(),
                                            msg,
                                            Some(constants::proxy_subtype::APPROVAL_TOKEN_INVALID),
                                            false,
                                        ))
                                    }
                                    agent_approval::ApprovalOutcome::Failed(e) => {
                                        tracing::error!(
                                            error = %e,
                                            "Failed to create approval request"
                                        );
                                        Some((
                                            error_response(
                                                StatusCode::INTERNAL_SERVER_ERROR,
                                                "Failed to create approval request",
                                            ),
                                            String::new(),
                                            e,
                                            Some(constants::proxy_subtype::ENGINE_FAILURE),
                                            false,
                                        ))
                                    }
                                    // The skill the script run reaches
                                    // would refuse it: refused here, with
                                    // nothing held and no token redeemed.
                                    agent_approval::ApprovalOutcome::Refused(r) => Some((
                                        match &r.body {
                                            Some(body) => {
                                                (r.status, Json(body.clone())).into_response()
                                            }
                                            None => error_response(r.status, &r.message),
                                        },
                                        r.message.clone(),
                                        r.message,
                                        r.subtype,
                                        r.argument_policy_denied,
                                    )),
                                }
                            }
                        };
                    if let Some((response, preview, error, subtype, policy_deny)) = stopped {
                        let duration = start.elapsed().as_secs_f64() * 1000.0;
                        let sys_target_url = build_target_url(server, path);
                        let mut log = proxy_logger::build_proxy_log(
                            "mcp",
                            &auth_ctx,
                            "POST",
                            "mcp-system-server",
                            Some(&sys_target_url),
                            Some(server_id),
                            response.status().as_u16(),
                            duration,
                            false,
                            Some(&error),
                            &correlation_id,
                        );
                        log.user_agent = extract_user_agent(&headers);
                        log.resource_group_id = Some(group.id.clone());
                        log.proxy_subtype = subtype.map(str::to_owned);
                        log.mcp_method = Some("tools/call".to_string());
                        log.mcp_capability_name = Some(tool.to_string());
                        log.request_body_preview = Some(proxy_logger::body_preview(
                            &String::from_utf8_lossy(&body_bytes),
                        ));
                        if !preview.is_empty() {
                            log.response_body_preview = Some(proxy_logger::body_preview(&preview));
                        }
                        if policy_deny {
                            proxy_logger::apply_policy_deny(&mut log);
                        }
                        proxy_logger::apply_call_chain(
                            &mut log,
                            &call_chain,
                            &current_task_id_str,
                            trace_continuity,
                            trace_root_kind,
                        );
                        state
                            .background_writer
                            .write_proxy_log(state.db.clone(), log);
                        return response;
                    }
                }
            }
        }
    }

    // Delegate to system server handler. Pass the whole SharedState
    // so the L3 skill tools can run guardrails / arg policy / quota / audit.
    // A skill call over MCP is ONE operation and must produce ONE
    // audit row (this mcp row, which already carries the full JSON-RPC body).
    // `run_with_verdict` folds the per-skill L2/L3 governance outcome up so we
    // stamp it here instead of writing a second `skill` row.
    // The system tool runs: what the call was admitted against stays spent.
    for hold in mcp_holds.drain(..) {
        hold.commit().await;
    }
    // The call's slots are held while the tool runs.
    let in_flight = crate::services::mcp_limits::McpInFlight::new(None, slot_hold);
    // The semantic-policy judge calls the governed LLM
    // surface as this caller, so it needs the caller's credentials. And the
    // run: a signed child of this step's chain, as
    // on every other surface that asks the judge, so the judge's call for a
    // script run over MCP is a step of the caller's run and not a run of
    // its own. A step id that does not parse cannot be forked from; the
    // call then carries the credential alone.
    let judge_headers = match ulid::Ulid::from_string(current_task_id_str) {
        Ok(step) => crate::routes::llm_helpers::judge_self_call_headers(
            headers,
            call_chain,
            step,
            &derive_tenant_key(state.config.encryption_key.as_bytes(), tenant_id),
        ),
        Err(_) => crate::routes::llm_helpers::extract_cache_auth_headers(headers),
    };
    let (sys_resp, skill_verdict) = system_server::run_with_verdict(system_server::handle_request(
        state,
        server,
        auth_ctx,
        method.as_str(),
        parsed_rpc.as_ref(),
        correlation_id,
        &judge_headers,
        &RunScope::from_chain(call_chain, current_task_id_str, trace_root_kind),
        &approvals,
    ))
    .await;
    in_flight.finish().await;

    let duration_ms = start.elapsed().as_secs_f64() * 1000.0;
    // The MCP transport is always 200; a skill governance verdict (403 block /
    // 202 approval) lives in the folded SkillVerdict — use it for the AUDIT row
    // so the denial is a filterable 403. The CLIENT still gets the 200 JSON-RPC
    // envelope (built from `sys_resp.status_code` further below).
    let log_status = skill_verdict
        .as_ref()
        .map(|v| v.status)
        .unwrap_or(sys_resp.status_code);
    let is_success = (200..300).contains(&log_status);
    // The CLIENT response + Prometheus/usage metrics keep the transport status
    // (MCP tools/call is always a 200 JSON-RPC envelope); only the audit row
    // above uses the governance verdict (`log_status`).
    let status = sys_resp.status_code;
    let resp_body_len = sys_resp.body.len();

    // Background: proxy log
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        auth_ctx,
        method.as_str(),
        path.unwrap_or("/"),
        Some(&server.base_url),
        Some(server_id),
        log_status,
        duration_ms,
        is_success,
        None,
        correlation_id,
    );
    log.proxy_subtype = Some(crate::constants::proxy_subtype::SYSTEM_SERVER.to_string());
    log.user_agent = extract_user_agent(headers);
    log.resource_group_id = target_group.map(|g| g.id.clone());
    log.request_size_bytes = Some(body_bytes.len() as i32);
    log.response_size_bytes = Some(resp_body_len as i32);
    log.request_headers = Some(proxy_logger::sanitize_headers(headers));
    let req_preview = String::from_utf8_lossy(&body_bytes);
    log.request_body_preview = Some(body_preview(&req_preview));
    let resp_preview = String::from_utf8_lossy(&sys_resp.body);
    log.response_body_preview = Some(body_preview(&resp_preview));
    log.mcp_method = sys_resp.mcp_method.clone();
    log.mcp_request_id = sys_resp.mcp_request_id.clone();
    log.mcp_capability_name = sys_resp.mcp_capability_name.clone();
    proxy_logger::apply_call_chain(
        &mut log,
        &call_chain,
        &current_task_id_str,
        trace_continuity,
        trace_root_kind,
    );
    match &residency_decision {
        Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
        None => proxy_logger::apply_residency_not_evaluated(
            &mut log,
            server_region_for_audit.as_deref(),
        ),
    }
    if input_pii_detected {
        proxy_logger::apply_pii_detected(&mut log);
    }
    // Fold the skill-layer verdict onto this single row: the skill id, the real
    // subtype (skill_load / skill_run / …), the denial reason, and the
    // guardrail/PII/policy flags that stamp the compliance tags — so no second
    // `skill` row is needed and none of that governance evidence is lost.
    if let Some(v) = &skill_verdict {
        log.skill_id = v.skill_id.clone();
        if let Some(sub) = &v.subtype {
            log.proxy_subtype = Some(sub.clone());
        }
        if let Some(e) = &v.error {
            log.error_message = Some(e.clone());
        }
        if log.resource_group_id.is_none() {
            log.resource_group_id = v.group_id.clone();
        }
        if v.guardrail_blocked {
            // This path receives only a boolean across a boundary
            // (skill verdict / stream flag / helper arg), so the config id and
            // action are not available here. Recorded as a known gap rather than
            // stamped with a guess.
            proxy_logger::apply_guardrail_block(&mut log);
        }
        if v.pii_detected {
            proxy_logger::apply_pii_detected(&mut log);
        }
        if v.policy_deny {
            proxy_logger::apply_policy_deny(&mut log);
        }
    }
    state
        .background_writer
        .write_proxy_log(state.db.clone(), log);

    // Background: proxy metric
    let ua = extract_user_agent(headers);
    let mcp_metric_data = json!({
        "method": sys_resp.mcp_method,
        "server_name": server.name,
        "tool_name": sys_resp.mcp_capability_name,
        "system_server": true,
    });
    let metric = proxy_logger::build_proxy_metric(
        "mcp",
        auth_ctx,
        method.as_str(),
        path.unwrap_or("/"),
        "mcp_proxy",
        Some(server_id),
        log_status,
        duration_ms,
        is_success,
        correlation_id,
        Some(body_bytes.len() as i32),
        Some(resp_body_len as i32),
        Some(headers.len() as i32),
        None,
        ua.as_deref(),
        None,
        Some(mcp_metric_data),
    );
    state
        .background_writer
        .write_proxy_metric(state.db.clone(), metric);

    // Prometheus metrics
    proxy_metrics::record_request("mcp", method.as_str(), log_status, "mcp");
    proxy_metrics::record_latency("mcp", method.as_str(), "mcp", duration_ms / 1000.0);

    // Background: usage log
    if is_success {
        let mcp_tool = sys_resp
            .mcp_capability_name
            .as_deref()
            .or(sys_resp.mcp_method.as_deref());
        usage::write_mcp_usage_log(
            &state.background_writer,
            state.db.clone(),
            tenant_id,
            auth_ctx,
            target_group,
            &server.id,
            &server.name,
            mcp_tool,
            &server.base_url,
            method.as_str(),
            correlation_id,
            status,
            duration_ms,
            Some(body_bytes.len() as i32),
            Some(resp_body_len as i32),
        );
        if let Some(g) = target_group {
            usage::spawn_alert_evaluation(
                state.db.clone(),
                tenant_id.to_string(),
                g.clone(),
                "mcp".into(),
                Some(correlation_id.to_string()),
            );
        }
    }

    // Build HTTP response
    let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
    let mut response = axum::response::Response::builder().status(axum_status);

    // Apply system server response headers
    for (name, value) in &sys_resp.headers {
        response = response.header(name.as_str(), value.as_str());
    }

    // Standard proxy headers
    response = response.header(constants::headers::X_CORRELATION_ID, correlation_id);
    response = response.header(
        constants::headers::X_AUTH_TYPE,
        auth_ctx.auth_type.to_string(),
    );
    response = response.header(constants::headers::X_REQUEST_TYPE, "mcp-system");

    // Attach usage warning headers if quota is approaching limits
    for (name, value) in &enforcement.warning_headers {
        if let (Ok(n), Ok(v)) = (
            axum::http::HeaderName::from_bytes(name.as_bytes()),
            axum::http::HeaderValue::from_str(value),
        ) {
            response = response.header(n, v);
        }
    }

    response.body(Body::from(sys_resp.body)).unwrap()
}

/// Handle SSE stream responses by streaming chunks directly to the client
/// instead of buffering the entire response body.
#[allow(clippy::too_many_arguments)]
async fn handle_sse_stream(
    upstream_resp: reqwest::Response,
    status: u16,
    resp_headers: &reqwest::header::HeaderMap,
    correlation_id: &str,
    auth_ctx: &crate::models::auth::AuthContext,
    is_mcp: bool,
    method: &Method,
    path: Option<&str>,
    target_url: &str,
    server_id: &str,
    server: &McpServer,
    body_bytes: &[u8],
    req_headers: &HeaderMap,
    // Resolved output-side guardrail configs — applied to an SSE-delivered
    // tools/call result (buffer-and-guard), closing the gap where SSE tool
    // results bypassed output redaction/blocking.
    guardrail_configs: Vec<crate::models::db::GuardrailConfig>,
    state: &SharedState,
    tenant_id: &str,
    target_group: Option<&crate::models::db::ResourceGroup>,
    start: Instant,
    capability_groups: &[crate::models::db::ResourceGroup],
    call_chain: CallChain,
    current_task_id_str: String,
    trace_continuity: TraceContinuity,
    trace_root_kind: Option<&'static str>,
    residency_decision: Option<crate::services::residency::ResidencyDecision>,
    server_region_for_audit: Option<String>,
    // What the call holds while in flight: the relay task owns it and ends
    // it when the stream is over. A GET listener gives its server permit
    // back at headers.
    mut in_flight: crate::services::mcp_limits::McpInFlight,
    // The payload limit of the answer; a GET listener is exempt.
    payload_limit: Option<usize>,
    // The call's deadline (none for a GET listener).
    deadline: Option<tokio::time::Instant>,
) -> Response {
    tracing::debug!(
        status,
        target_url,
        "MCP SSE stream started — streaming response to client"
    );
    let payload_limit = if *method == Method::GET {
        in_flight.release_permit();
        None
    } else {
        payload_limit
    };

    // Extract MCP method and capability from request body (same as non-SSE path)
    let mut log_mcp_method: Option<String> = None;
    let mut log_mcp_request_id: Option<String> = None;
    let mut log_mcp_capability: Option<String> = None;
    let mut rpc_id: Option<Value> = None;
    if is_mcp {
        if let Ok(parsed) = serde_json::from_slice::<Value>(body_bytes) {
            rpc_id = parsed.get("id").cloned();
            log_mcp_method = parsed
                .get("method")
                .and_then(|v| v.as_str())
                .map(String::from);
            log_mcp_request_id = parsed.get("id").map(|v| {
                if v.is_string() {
                    v.as_str().unwrap().to_string()
                } else {
                    v.to_string()
                }
            });
            log_mcp_capability = parsed
                .get("params")
                .and_then(|p| p.get("name"))
                .and_then(|v| v.as_str())
                .map(String::from);
        }
    }

    // Pre-compute capability filter for list responses (if applicable).
    // We resolve this BEFORE starting the stream so we don't need DB access inside the async stream task.
    let sse_cap_filter: Option<(String, String, std::collections::HashSet<String>)> = {
        let rpc_method = log_mcp_method.as_deref().unwrap_or("");
        let filter_spec: Option<(&str, &str, &str)> = match rpc_method {
            "tools/list" => Some(("tool", "tools", "name")),
            "resources/list" => Some(("resource", "resources", "uri")),
            "prompts/list" => Some(("prompt", "prompts", "name")),
            "resources/templates/list" => {
                Some(("resource_template", "resourceTemplates", "uriTemplate"))
            }
            _ => None,
        };
        if let Some((cap_type, json_key, name_field)) = filter_spec {
            if let Some(enabled_set) =
                usage::get_enabled_capabilities(&state.db, &server.id, cap_type, capability_groups)
                    .await
            {
                Some((json_key.to_string(), name_field.to_string(), enabled_set))
            } else {
                None
            }
        } else {
            None
        }
    };

    // Capture values needed for background logging after stream ends
    let ua = extract_user_agent(req_headers);
    let req_body_preview = {
        let s = String::from_utf8_lossy(body_bytes);
        body_preview(&s)
    };
    let sanitized_headers = proxy_logger::sanitize_headers(req_headers);
    let req_size = body_bytes.len() as i32;
    let req_hdr_count = req_headers.len() as i32;
    let resp_hdr_count = resp_headers.len() as i32;
    let is_success = (200..300).contains(&status);

    // Output-side guardrails on an SSE-delivered tool result. Gated to
    // `tools/call` only — its result is a single short event (like a
    // capability list), so buffer-and-guard is safe; long-lived listener GETs
    // and other methods must keep streaming unbuffered.
    let needs_output_guard = is_mcp
        && is_success
        && log_mcp_method.as_deref() == Some("tools/call")
        && !guardrail_configs.is_empty();
    let bg_guardrail_configs = guardrail_configs;

    // Clone values for the background task
    let ua_for_metric = ua.clone();
    let bg_state = state.clone();
    let bg_db = state.db.clone();
    let bg_auth = auth_ctx.clone();
    let bg_method = method.to_string();
    let bg_path = path.unwrap_or("/").to_string();
    let bg_target_url = target_url.to_string();
    let bg_server_id = server_id.to_string();
    let bg_server_name = server.name.clone();
    let bg_server_real_id = server.id.clone();
    let bg_tenant_id = tenant_id.to_string();
    let bg_correlation_id = correlation_id.to_string();
    let bg_mcp_method = log_mcp_method.clone();
    let bg_mcp_request_id = log_mcp_request_id;
    let bg_mcp_capability = log_mcp_capability;
    let bg_target_group = target_group.cloned();
    let bg_call_chain = call_chain;
    let bg_current_task_id_str = current_task_id_str;
    let bg_trace_continuity = trace_continuity;
    let bg_trace_root_kind = trace_root_kind;
    // Clone the residency decision and the
    // server's region into the background task so the SSE stream
    // closure's audit-row writes stamp the same outcome that the
    // gateway evaluated at request entry.
    let bg_residency_decision = residency_decision.clone();
    let bg_server_region_for_audit = server_region_for_audit.clone();

    // Prometheus metrics for the SSE connection
    proxy_metrics::record_request("mcp", method.as_str(), status, "mcp");
    proxy_metrics::stream_opened();

    // Build streaming response
    let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
    let mut response = axum::response::Response::builder().status(axum_status);

    // Forward response headers (including content-type: text/event-stream)
    for (key, value) in resp_headers.iter() {
        let key_str = key.as_str().to_lowercase();
        if !matches!(
            key_str.as_str(),
            "transfer-encoding"
                | "connection"
                | "keep-alive"
                | "content-encoding"
                | "content-length"
        ) {
            response = response.header(key.as_str(), value);
        }
    }

    response = response.header(crate::constants::headers::X_CORRELATION_ID, correlation_id);
    response = response.header(
        crate::constants::headers::X_AUTH_TYPE,
        auth_ctx.auth_type.to_string(),
    );
    response = response.header(crate::constants::headers::X_REQUEST_TYPE, "mcp-sse");

    // Stream the upstream response body directly to the client.
    // Use a channel-based approach to track stream end.
    let byte_stream = upstream_resp.bytes_stream();
    let mapped_stream = futures::StreamExt::map(byte_stream, |chunk| {
        chunk.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
    });

    // Spawn a task that forwards chunks, accumulates the response body,
    // and writes proxy log + metric after the stream ends.
    let shutdown_token = state.shutdown_token.clone();
    let (tx, rx) = tokio::sync::mpsc::channel::<Result<bytes::Bytes, std::io::Error>>(32);
    tokio::spawn(async move {
        use futures::StreamExt;
        let mut stream = mapped_stream;
        let mut accumulated_body = Vec::new();
        let mut total_bytes: usize = 0;

        // For capability-filtered list responses, we buffer all SSE data,
        // filter the JSON-RPC result, then send the modified SSE events.
        // List responses are short-lived (single event + close), so buffering is fine.
        let needs_filtering = sse_cap_filter.is_some();
        // Buffer for either capability filtering (list responses) or output
        // guardrails (tools/call results) — both rewrite the JSON-RPC body
        // post-stream before forwarding.
        let needs_buffering = needs_filtering || needs_output_guard;
        let mut filter_buffer: Vec<u8> = Vec::new();
        // Why the relay stopped before the upstream's end, if it did.
        let mut cut: Option<SseCut> = None;
        let mut output_blocked = false;
        // Set when the SSE output
        // guardrail matched a PII category (block or redact). Stamped
        // on the audit row below.
        let mut output_pii_detected = false;

        loop {
            tokio::select! {
                _ = shutdown_token.cancelled() => {
                    tracing::debug!("MCP SSE stream terminated by shutdown");
                    break;
                }
                // The client hung up. Seen here, not only when a chunk is
                // forwarded: while the relay buffers it forwards nothing.
                _ = tx.closed() => {
                    tracing::debug!("MCP SSE client hung up");
                    break;
                }
                // The call's time is up.
                _ = async {
                    match deadline {
                        Some(d) => tokio::time::sleep_until(d).await,
                        None => std::future::pending().await,
                    }
                } => {
                    cut = Some(SseCut::Timeout);
                    break;
                }
                item = stream.next() => {
                    match item {
                        Some(chunk) => {
                            match chunk {
                                Ok(ref data) => {
                                    // Over the payload limit: this chunk is not
                                    // forwarded and the upstream is dropped.
                                    if let Some(limit) = payload_limit {
                                        if total_bytes + data.len() > limit {
                                            cut = Some(SseCut::Payload(limit));
                                            break;
                                        }
                                    }
                                    // Cap accumulation at 10KB to avoid memory issues on long streams
                                    if accumulated_body.len() < 10 * 1024 {
                                        accumulated_body.extend_from_slice(data);
                                    }
                                    total_bytes += data.len();

                                    if needs_buffering {
                                        // Buffer data for post-stream filtering / guarding
                                        filter_buffer.extend_from_slice(data);
                                        continue; // Don't forward yet
                                    }
                                }
                                // A stream that breaks off while buffered is
                                // not answered with what was buffered so far.
                                Err(ref e) if needs_buffering => {
                                    cut = Some(SseCut::Upstream(e.to_string()));
                                    break;
                                }
                                Err(_) => {}
                            }
                            if !needs_buffering {
                                if tx.send(chunk).await.is_err() {
                                    // Client disconnected
                                    break;
                                }
                            }
                        }
                        None => break, // Stream ended naturally
                    }
                }
            }
        }

        // A cut stream ends with one error event answering the call (after
        // a blank line, which ends an event the cut left half-sent).
        if let Some(c) = &cut {
            let event = format!(
                "\n\nevent: message\ndata: {}\n\n",
                serde_json::to_string(&c.error(rpc_id.as_ref())).unwrap_or_default()
            );
            let _ = tx.send(Ok(bytes::Bytes::from(event))).await;
        // If we buffered SSE data for filtering, process and send it now
        } else if needs_filtering && !filter_buffer.is_empty() {
            if let Some((json_key, name_field, enabled_set)) = sse_cap_filter {
                let filtered_data =
                    filter_sse_data(&filter_buffer, &json_key, &name_field, &enabled_set);
                let _ = tx.send(Ok(bytes::Bytes::from(filtered_data))).await;
            }
        } else if needs_output_guard && !filter_buffer.is_empty() {
            // Output-side guardrails on the buffered tools/call result. Mirrors
            // the buffered (application/json) path: scan result.content[*].text,
            // then redact in place or block (JSON-RPC error). Closes the gap
            // where SSE tool results were forwarded ungoverned.
            let send_bytes: Vec<u8> = match sse_tool_result_combined_text(&filter_buffer) {
                Some(combined) if !combined.is_empty() => {
                    match guardrails::check_output(
                        &combined,
                        &bg_guardrail_configs,
                        "mcp",
                        &bg_state.http_client,
                    )
                    .await
                    {
                        guardrails::CheckOutcome::Block(violation) => {
                            tracing::warn!(
                                check = %violation.check,
                                config = %violation.config_name,
                                message = %violation.message,
                                "Guardrail violation on MCP SSE output — blocked"
                            );
                            proxy_metrics::record_guardrail_block("mcp", &violation.check);
                            output_blocked = true;
                            if violation.check == "pii" {
                                output_pii_detected = true;
                            }
                            let rpc_id =
                                bg_mcp_request_id.as_ref().map(|s| Value::String(s.clone()));
                            let rpc_body =
                                guardrail_violation_body(&violation, rpc_id.as_ref(), "output");
                            format!(
                                "event: message\ndata: {}\n\n",
                                serde_json::to_string(&rpc_body).unwrap_or_default()
                            )
                            .into_bytes()
                        }
                        guardrails::CheckOutcome::Redacted { content, warnings } => {
                            if warnings.iter().any(|w| w.check == "pii") {
                                output_pii_detected = true;
                            }
                            redact_sse_tool_result(&filter_buffer, &content)
                        }
                        // Pass / Warn (warn = log-only): forward unchanged.
                        guardrails::CheckOutcome::Pass => filter_buffer.clone(),
                        guardrails::CheckOutcome::Warn { warnings } => {
                            if warnings.iter().any(|w| w.check == "pii") {
                                output_pii_detected = true;
                            }
                            filter_buffer.clone()
                        }
                    }
                }
                // No text result to scan (e.g. image/resource-only) — forward.
                _ => filter_buffer.clone(),
            };
            let _ = tx.send(Ok(bytes::Bytes::from(send_bytes))).await;
        }
        // Drop tx to signal stream end
        drop(tx);
        // The upstream is dropped and the call's hold on the server ends.
        drop(stream);
        in_flight.finish().await;

        let duration = start.elapsed().as_millis() as f64;
        proxy_metrics::stream_closed();
        proxy_metrics::record_latency("mcp", &bg_method, "mcp", duration / 1000.0);
        tracing::debug!(total_bytes, duration_ms = duration, "MCP SSE stream ended");

        // Write proxy log with full response body preview. Reflect an output
        // guardrail block (403) rather than the upstream 200 so the audit row
        // records the real outcome.
        let resp_preview = String::from_utf8_lossy(&accumulated_body);
        let cut_message = cut.as_ref().map(SseCut::message);
        let (log_status, log_success, log_error): (u16, bool, Option<&str>) = if let Some(c) = &cut
        {
            (c.status(), false, cut_message.as_deref())
        } else if output_blocked {
            (403, false, Some("Guardrail violation on MCP SSE output"))
        } else {
            (status, is_success, None)
        };
        let mut log = proxy_logger::build_proxy_log(
            "mcp",
            &bg_auth,
            &bg_method,
            &bg_path,
            Some(&bg_target_url),
            Some(&bg_server_id),
            log_status,
            duration,
            log_success,
            log_error,
            &bg_correlation_id,
        );
        log.proxy_subtype = Some(match &cut {
            Some(SseCut::Payload(_)) => "payload_limit".to_string(),
            _ => crate::constants::proxy_subtype::SSE_STREAM.to_string(),
        });
        log.user_agent = ua;
        log.resource_group_id = bg_target_group.as_ref().map(|g| g.id.clone());
        log.request_size_bytes = Some(req_size);
        log.response_size_bytes = Some(total_bytes as i32);
        log.request_headers = Some(sanitized_headers);
        log.request_body_preview = Some(req_body_preview);
        log.response_body_preview = Some(body_preview(&resp_preview));
        log.mcp_method = bg_mcp_method.clone();
        log.mcp_request_id = bg_mcp_request_id;
        let bg_mcp_capability_for_metric = bg_mcp_capability.clone();
        log.mcp_capability_name = bg_mcp_capability;
        proxy_logger::apply_call_chain(
            &mut log,
            &bg_call_chain,
            &bg_current_task_id_str,
            bg_trace_continuity,
            bg_trace_root_kind,
        );
        match &bg_residency_decision {
            Some(d) => proxy_logger::apply_residency_audit(&mut log, d),
            None => proxy_logger::apply_residency_not_evaluated(
                &mut log,
                bg_server_region_for_audit.as_deref(),
            ),
        }
        // SSE output guardrail outcome.
        if output_blocked {
            // This path receives only a boolean across a boundary
            // (skill verdict / stream flag / helper arg), so the config id and
            // action are not available here. Recorded as a known gap rather than
            // stamped with a guess.
            proxy_logger::apply_guardrail_block(&mut log);
        }
        if output_pii_detected {
            proxy_logger::apply_pii_detected(&mut log);
        }
        bg_state
            .background_writer
            .write_proxy_log(bg_db.clone(), log);

        // Proxy metric
        let mcp_metric_data = serde_json::json!({
            "method": bg_mcp_method,
            "server_name": bg_server_name,
            "tool_name": bg_mcp_capability_for_metric,
        });
        let metric = proxy_logger::build_proxy_metric(
            "mcp",
            &bg_auth,
            &bg_method,
            &bg_path,
            "mcp_proxy",
            Some(&bg_server_id),
            status,
            duration,
            is_success,
            &bg_correlation_id,
            Some(req_size),
            Some(total_bytes as i32),
            Some(req_hdr_count),
            Some(resp_hdr_count),
            ua_for_metric.as_deref(),
            None,
            Some(mcp_metric_data),
        );
        bg_state
            .background_writer
            .write_proxy_metric(bg_db.clone(), metric);

        // Usage log
        if is_success {
            let mcp_tool = bg_mcp_capability_for_metric
                .as_deref()
                .or(bg_mcp_method.as_deref());
            usage::write_mcp_usage_log(
                &bg_state.background_writer,
                bg_db.clone(),
                &bg_tenant_id,
                &bg_auth,
                bg_target_group.as_ref(),
                &bg_server_real_id,
                &bg_server_name,
                mcp_tool,
                &bg_target_url,
                &bg_method,
                &bg_correlation_id,
                status,
                duration,
                Some(req_size),
                Some(total_bytes as i32),
            );
            if let Some(ref g) = bg_target_group {
                usage::spawn_alert_evaluation(
                    bg_db,
                    bg_tenant_id.clone(),
                    g.clone(),
                    "mcp".into(),
                    Some(bg_correlation_id.clone()),
                );
            }
        }
    });

    let body = Body::from_stream(tokio_stream::wrappers::ReceiverStream::new(rx));
    response.body(body).unwrap()
}

/// Resolve the upstream URL for an MCP proxy call from the (optional)
/// sub-path captured from the route.
///
/// This function is the single source of truth for how the gateway's
/// two MCP routes ([`proxy_root`] and [`proxy_with_path`]) map onto
/// upstream URLs. Three cases, distinguished by `path`:
///
/// | Inbound path                          | Upstream URL                                            | Used for                                  |
/// |---------------------------------------|---------------------------------------------------------|-------------------------------------------|
/// | `None` / `Some("")`                   | `base_url + mcp_endpoint_path`                          | Canonical JSON-RPC + Streamable HTTP      |
/// | `Some(p)` where `p.starts_with('.')`  | `<scheme>://<origin>/p` (drops `mcp_endpoint_path`)     | OAuth `.well-known/*` discovery           |
/// | `Some(p)` (any other)                 | `base_url + "/" + p`                                    | REST-style passthrough                    |
///
/// ## Why the origin rewrite for `.`-prefixed paths
///
/// OAuth protected-resource discovery ([RFC 9728]) and authorization-server
/// metadata ([RFC 8414]) are **resource-level** concepts: the metadata document
/// lives at the server origin, never under a nested API path. A server
/// registered with `base_url = https://example.com/api/mcp` and
/// `mcp_endpoint_path = /mcp` publishes its metadata at
/// `https://example.com/.well-known/...`, never at
/// `…/api/mcp/.well-known/...`. The `.`-prefix branch enforces this rewrite
/// regardless of how deeply the MCP endpoint itself is nested.
///
/// ## Why the canonical client URL omits `mcp_endpoint_path`
///
/// Clients pass only `/v1/proxy/mcp/{server_id}`; the proxy appends
/// `mcp_endpoint_path` itself. This keeps the client URL stable across
/// upstream reorganizations — operators can change the MCP endpoint path
/// in the admin UI without any client-side update.
///
/// Note that a client-supplied path on `/v1/proxy/mcp/{server_id}/<path>`
/// does **not** combine with `mcp_endpoint_path` — it **replaces** it.
/// The upstream URL becomes `base_url + "/" + path` verbatim. Whether
/// that works depends on what the upstream accepts:
///
/// - For a server with `mcp_endpoint_path=""` (e.g. AWS Knowledge MCP),
///   any client-supplied path hits a path-relative URL on the upstream.
///   Servers that route any path to their JSON-RPC handler will accept it.
/// - For a server with `mcp_endpoint_path=/mcp` (e.g. DeepWiki), a
///   client-supplied path like `/foo` would forward to `<base>/foo` and
///   miss the `/mcp` endpoint entirely.
///
/// Sticking to the canonical URL avoids having to reason about either
/// case.
///
/// [RFC 8414]: https://www.rfc-editor.org/rfc/rfc8414
/// [RFC 9728]: https://www.rfc-editor.org/rfc/rfc9728
fn build_target_url(server: &McpServer, path: Option<&str>) -> String {
    // Normalise base_url: drop trailing slash so the join with the
    // endpoint path can't produce `host//mcp`. Operators occasionally
    // copy URLs from a browser address bar that include a trailing
    // `/`, and that surfaces later as a duplicate-server check
    // misfiring on the malformed URL ("`host:8080//mcp`" vs
    // "`host:8080/mcp`"). Trim once, here, where the upstream URL is
    // actually assembled.
    let base = server.base_url.trim_end_matches('/');
    match path {
        // Canonical client URL: /v1/proxy/mcp/{server_id}
        // Append the configured MCP endpoint path so operators can
        // change the upstream layout without breaking clients.
        None | Some("") => {
            if let Some(ref mcp_path) = server.mcp_endpoint_path {
                if mcp_path.is_empty() {
                    base.to_string()
                } else if mcp_path.starts_with('/') {
                    // Already canonical: `<base>/<mcp_path>`.
                    format!("{base}{mcp_path}")
                } else {
                    // Operator stored `mcp` (no slash). Insert one so
                    // we don't accidentally collapse to `<base>mcp`.
                    format!("{base}/{mcp_path}")
                }
            } else {
                base.to_string()
            }
        }
        // OAuth discovery: /v1/proxy/mcp/{server_id}/.well-known/...
        // Rewrite to the server origin per RFC 8414 / 9728 — the
        // metadata document MUST live at the origin, not under
        // mcp_endpoint_path.
        Some(p) if p.starts_with('.') => {
            let url = url::Url::parse(&server.base_url)
                .unwrap_or_else(|_| url::Url::parse("http://localhost").unwrap());
            format!("{}://{}/{}", url.scheme(), url.authority(), p)
        }
        // REST-style passthrough: /v1/proxy/mcp/{server_id}/<anything>
        // Append verbatim to base_url. Escape hatch for non-Streamable-HTTP
        // MCP servers; rarely needed.
        Some(p) => {
            format!("{base}/{p}")
        }
    }
}

/// JSON-RPC 2.0 `Invalid Request` for a batch. Batching was removed from MCP
/// in 2025-06-18; governing each element separately would need a per-element
/// decision, hold and audit row, so the gateway refuses the envelope instead.
fn batch_refused() -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({
            "jsonrpc": "2.0",
            "id": null,
            "error": {
                "code": -32600,
                "message": "JSON-RPC batches are not accepted by the gateway: send one message per request",
            },
        })),
    )
        .into_response()
}

/// JSON-RPC 2.0 `Parse error` for a body that might be executed as JSON-RPC
/// and cannot be read as one message (`jsonrpc_body::RpcBody::Unreadable`).
fn unreadable_refused(reason: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(crate::services::jsonrpc_body::unreadable_error(reason)),
    )
        .into_response()
}

/// MCP Streamable HTTP spec compliance for the proxy's *outbound* leg.
///
/// The MCP Streamable HTTP transport spec (2025-03-26, "Sending Messages to the Server", and the
/// successor 2025-06-18) makes the following client requirement
/// normative:
///
/// > "The client MUST send an Accept header that lists both
/// > `application/json` and `text/event-stream` as supported content
/// > types."
///
/// The proxy *is* the client from the upstream MCP server's
/// perspective, so this requirement applies to every outbound request
/// the proxy makes. The reference Python MCP SDK
/// (`mcp.server.streamable_http_manager`) and the reference TypeScript
/// MCP SDK both enforce it strictly — a request advertising only
/// `application/json` is rejected with `406 Not Acceptable`. Lax
/// upstreams (AWS Knowledge MCP, our trial smoke target) ignore extra
/// Accept types, so widening the header is safe.
///
/// We overwrite the inbound `Accept` value rather than merge into it.
/// Inbound clients may legitimately send `Accept: */*`, `Accept:
/// application/json`, or a v0.x list that omits `text/event-stream`;
/// none of those are useful to forward to a strict upstream, and the
/// proxy already negotiates the response-side stream/JSON distinction
/// based on the upstream's `Content-Type`, not the client's Accept.
///
/// This helper is extracted from the build-upstream-headers section
/// so it can be unit-tested in isolation — see the
/// `outbound_accept_enforcement_tests` module below.
fn enforce_mcp_outbound_accept(headers: &mut reqwest::header::HeaderMap) {
    headers.insert(
        reqwest::header::ACCEPT,
        reqwest::header::HeaderValue::from_static("application/json, text/event-stream"),
    );
}

/// How long to remember that an MCP server doesn't speak the
/// optional Streamable-HTTP SSE listener channel. After expiry we
/// re-probe so an upstream that *gains* listener support
/// self-heals without an operator action.
///
/// 30 minutes balances:
///   * fast self-heal once an upstream is fixed,
///   * vs. the cost of re-probing too aggressively on a known-bad
///     server (one bad request per replica per TTL window).
const NON_SSE_CACHE_TTL: std::time::Duration = std::time::Duration::from_secs(30 * 60);

/// Does the inbound request advertise `text/event-stream` in its
/// `Accept` header? Real MCP clients using the Streamable HTTP
/// transport always send `Accept: application/json,
/// text/event-stream` (or include `text/event-stream` in the list)
/// when opening the SSE GET that listens for server-initiated
/// events. Browser tabs pasting the proxy URL send
/// `Accept: text/html,…` and never carry `text/event-stream`.
///
/// Used as the disambiguator between a legitimate stream-open and
/// a browser passthrough so we don't silently swallow real SSE
/// traffic from the metric counts.
fn accept_includes_event_stream(headers: &HeaderMap) -> bool {
    headers
        .get(axum::http::header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .map(|v| {
            // Comma-separated; tolerate parameters (e.g. `;q=0.9`)
            // and surrounding whitespace.
            v.split(',')
                .any(|part| part.split(';').next().unwrap_or("").trim() == "text/event-stream")
        })
        .unwrap_or(false)
}

/// Did the upstream actually return an SSE stream? We compare the
/// Content-Type prefix because some servers append parameters
/// (e.g. `text/event-stream; charset=utf-8`).
///
/// Returning `false` when the inbound was a GET that advertised
/// `text/event-stream` in `Accept` is the trigger for the
/// non-SSE cache: we know the server's listener channel is fake.
fn response_content_type_is_event_stream(headers: &reqwest::header::HeaderMap) -> bool {
    headers
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|v| {
            v.split(';')
                .next()
                .unwrap_or("")
                .trim()
                .eq_ignore_ascii_case("text/event-stream")
        })
        .unwrap_or(false)
}

/// Resolve the `proxy_subtype` for a proxy_logs row.
///
/// Audit hook: when the portal calls into MCP via the
/// McpAppFrame iframe loopback (`ui/tools/call`), it stamps
/// `X-Brutor-Capability: mcp_app_callback` so admins can distinguish
/// iframe-initiated calls from chat-initiated ones in the audit UI
/// without changing access-control semantics. Anything else falls
/// back to `json_rpc` (when the body is JSON-RPC) or `http`.
///
/// Pure function so it can be unit-tested without spinning up axum.
pub(crate) fn resolve_proxy_subtype(headers: &HeaderMap, is_mcp: bool) -> String {
    let capability = headers
        .get("x-brutor-capability")
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty());
    match capability {
        Some("mcp_app_callback") => "mcp_app_callback".into(),
        _ if is_mcp => "json_rpc".into(),
        _ => "http".into(),
    }
}

/// The window the POLICY states for a hold on this capability, if any.
///
/// If the filter itself is `approval_required`, the window it states (its
/// rows' smallest `approval_timeout_seconds`, or the group-wide default's)
/// is the operator's statement for this tool. Otherwise — an
/// argument-policy band or an agent grant demanded the
/// human, and neither carries a per-action window — the filter row's
/// `approval_timeout_seconds` (which the control plane accepts on an
/// `enabled` row too) lends its window. `None` = no policy states one: the
/// hold takes the Resource Group / tenant / platform default, which
/// `agent_approval::require_or_enqueue` resolves through
/// `approval_window::resolve` — never a literal here.
///
/// Pure function so it can be unit-tested without a database.
pub(crate) fn policy_hold_window(
    access: &usage::CapabilityAccess,
    filter_hold_window: Option<i32>,
) -> Option<i32> {
    let lent = filter_hold_window.filter(|t| *t > 0);
    match access {
        usage::CapabilityAccess::ApprovalRequired { window_seconds } => {
            window_seconds.filter(|t| *t > 0).or(lent)
        }
        _ => lent,
    }
}

/// Merge the two planes that can demand approval (an
/// argument-policy band and the capability filter) into one decision, so a
/// single call produces a single queue entry.
///
/// * The filter's `Denied` is the stricter statement; a band cannot soften a
///   disabled tool.
/// * Both want a human → one hold, on the filter's stated window.
/// * The band alone → one hold, on the filter row's window if the operator
///   set one, else no stated window (see [`policy_hold_window`]).
/// * No band → the filter's decision, unchanged.
pub(crate) fn merge_band_with_capability_access(
    band_requires_approval: bool,
    resolution: &usage::CapabilityAccessResolution,
) -> usage::CapabilityAccess {
    match (band_requires_approval, &resolution.access) {
        (_, usage::CapabilityAccess::Denied) => usage::CapabilityAccess::Denied,
        (true, access) => usage::CapabilityAccess::ApprovalRequired {
            window_seconds: policy_hold_window(access, resolution.hold_window_seconds),
        },
        (false, other) => other.clone(),
    }
}

#[cfg(test)]
mod band_hold_window_tests {
    use super::{merge_band_with_capability_access, policy_hold_window};
    use crate::services::usage::{CapabilityAccess, CapabilityAccessResolution};

    fn res(access: CapabilityAccess, window: Option<i32>) -> CapabilityAccessResolution {
        CapabilityAccessResolution {
            access,
            hold_window_seconds: window,
        }
    }

    #[test]
    fn enabled_filter_with_a_window_lends_it_to_the_band() {
        // The tool stays `enabled` (no hold on its own), but the operator
        // set approval_timeout_seconds so a band-only hold gives the
        // underwriter more than the default.
        assert_eq!(
            merge_band_with_capability_access(true, &res(CapabilityAccess::Allowed, Some(3600))),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(3600)
            }
        );
    }

    #[test]
    fn enabled_filter_without_a_window_states_none() {
        // No literal default here: the hold takes the group / tenant /
        // platform window from `approval_window::resolve`.
        assert_eq!(
            merge_band_with_capability_access(true, &res(CapabilityAccess::Allowed, None)),
            CapabilityAccess::ApprovalRequired {
                window_seconds: None
            }
        );
    }

    #[test]
    fn approval_required_filter_keeps_its_own_window() {
        assert_eq!(
            merge_band_with_capability_access(
                true,
                &res(
                    CapabilityAccess::ApprovalRequired {
                        window_seconds: Some(900)
                    },
                    Some(900)
                )
            ),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(900)
            }
        );
    }

    #[test]
    fn a_band_cannot_soften_a_disabled_tool() {
        assert_eq!(
            merge_band_with_capability_access(true, &res(CapabilityAccess::Denied, Some(3600))),
            CapabilityAccess::Denied
        );
    }

    #[test]
    fn no_band_leaves_the_filter_decision_untouched() {
        assert_eq!(
            merge_band_with_capability_access(false, &res(CapabilityAccess::Allowed, Some(3600))),
            CapabilityAccess::Allowed
        );
        assert_eq!(
            merge_band_with_capability_access(
                false,
                &res(
                    CapabilityAccess::ApprovalRequired {
                        window_seconds: Some(42)
                    },
                    None
                )
            ),
            CapabilityAccess::ApprovalRequired {
                window_seconds: Some(42)
            }
        );
    }

    #[test]
    fn a_non_positive_window_is_not_a_window() {
        assert_eq!(
            policy_hold_window(&CapabilityAccess::Allowed, Some(0)),
            None
        );
        assert_eq!(
            policy_hold_window(&CapabilityAccess::Allowed, Some(-5)),
            None
        );
        assert_eq!(
            policy_hold_window(&CapabilityAccess::Allowed, Some(86400)),
            Some(86400)
        );
        assert_eq!(
            policy_hold_window(
                &CapabilityAccess::ApprovalRequired {
                    window_seconds: None
                },
                None
            ),
            None
        );
    }
}

#[cfg(test)]
mod proxy_subtype_tests {
    use super::resolve_proxy_subtype;
    use axum::http::{HeaderMap, HeaderValue};

    fn hdrs(capability: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        if let Some(v) = capability {
            h.insert("x-brutor-capability", HeaderValue::from_str(v).unwrap());
        }
        h
    }

    #[test]
    fn capability_header_overrides_json_rpc() {
        let h = hdrs(Some("mcp_app_callback"));
        assert_eq!(resolve_proxy_subtype(&h, true), "mcp_app_callback");
    }

    #[test]
    fn capability_header_overrides_http() {
        let h = hdrs(Some("mcp_app_callback"));
        assert_eq!(resolve_proxy_subtype(&h, false), "mcp_app_callback");
    }

    #[test]
    fn unknown_capability_value_falls_through_to_default() {
        // Defence-in-depth: only the literal "mcp_app_callback"
        // promotes the subtype. A future taxonomy change must come
        // with a new case in resolve_proxy_subtype, not silent
        // pass-through of arbitrary header values.
        let h = hdrs(Some("not_a_real_capability"));
        assert_eq!(resolve_proxy_subtype(&h, true), "json_rpc");
    }

    #[test]
    fn empty_capability_header_uses_default() {
        let h = hdrs(Some(""));
        assert_eq!(resolve_proxy_subtype(&h, true), "json_rpc");
    }

    #[test]
    fn whitespace_around_capability_value_is_trimmed() {
        let h = hdrs(Some("  mcp_app_callback  "));
        assert_eq!(resolve_proxy_subtype(&h, true), "mcp_app_callback");
    }

    #[test]
    fn no_capability_header_json_rpc_body() {
        let h = hdrs(None);
        assert_eq!(resolve_proxy_subtype(&h, true), "json_rpc");
    }

    #[test]
    fn no_capability_header_http_body() {
        let h = hdrs(None);
        assert_eq!(resolve_proxy_subtype(&h, false), "http");
    }
}

#[cfg(test)]
mod accept_event_stream_tests {
    use super::accept_includes_event_stream;
    use axum::http::{HeaderMap, HeaderValue};

    fn hdrs(accept: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        if let Some(v) = accept {
            h.insert(
                axum::http::header::ACCEPT,
                HeaderValue::from_str(v).unwrap(),
            );
        }
        h
    }

    #[test]
    fn missing_accept_is_false() {
        // Browser GETs without an explicit Accept fall through here
        // and must NOT be treated as SSE — that's the whole point
        // of the disambiguation.
        assert!(!accept_includes_event_stream(&hdrs(None)));
    }

    #[test]
    fn browser_html_accept_is_false() {
        // The realistic Chrome Accept header for a tab pasted
        // with the proxy URL. Must NOT count as SSE.
        assert!(!accept_includes_event_stream(&hdrs(Some(
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ))));
    }

    #[test]
    fn mcp_sdk_dual_accept_is_true() {
        // Exact value the @modelcontextprotocol StreamableHTTP
        // transport sends. Must count as SSE so we don't drop
        // legitimate stream-opens from the portal's MCPClient.
        assert!(accept_includes_event_stream(&hdrs(Some(
            "application/json, text/event-stream"
        ))));
    }

    #[test]
    fn pure_event_stream_accept_is_true() {
        // Some clients send only text/event-stream — also valid.
        assert!(accept_includes_event_stream(&hdrs(Some(
            "text/event-stream"
        ))));
    }

    #[test]
    fn accept_with_qvalue_parameter_is_recognised() {
        // Quality-value parameter on the SSE entry must not break
        // detection. Real-world: Firefox sometimes appends q=… to
        // every entry.
        assert!(accept_includes_event_stream(&hdrs(Some(
            "application/json;q=0.9, text/event-stream;q=1.0"
        ))));
    }

    #[test]
    fn json_only_accept_is_false() {
        // POST JSON-RPC clients without SSE — those still go
        // through the json_rpc path, but if such a client somehow
        // issued a GET we'd want to flag it as passthrough.
        assert!(!accept_includes_event_stream(&hdrs(Some(
            "application/json"
        ))));
    }

    #[test]
    fn substring_match_does_not_false_positive() {
        // Hypothetical malformed header. The contract is *exact*
        // entry match (after trimming params), not substring,
        // because a substring search would also match
        // "text/event-stream-fake" or similar oddities.
        assert!(!accept_includes_event_stream(&hdrs(Some(
            "text/event-stream-not-really"
        ))));
    }
}

#[cfg(test)]
mod response_content_type_tests {
    use super::response_content_type_is_event_stream;
    use reqwest::header::{CONTENT_TYPE, HeaderMap, HeaderValue};

    fn hdrs(content_type: Option<&str>) -> HeaderMap {
        let mut h = HeaderMap::new();
        if let Some(v) = content_type {
            h.insert(CONTENT_TYPE, HeaderValue::from_str(v).unwrap());
        }
        h
    }

    #[test]
    fn missing_content_type_is_false() {
        // Defensive: an upstream that omitted Content-Type
        // can't claim it's SSE.
        assert!(!response_content_type_is_event_stream(&hdrs(None)));
    }

    #[test]
    fn html_response_is_false() {
        // The AWS Knowledge MCP behavior — HTML on GET. The
        // whole reason this helper exists.
        assert!(!response_content_type_is_event_stream(&hdrs(Some(
            "text/html; charset=utf-8"
        ))));
    }

    #[test]
    fn json_response_is_false() {
        // Some servers respond to GET with JSON-RPC error
        // payloads; not a stream.
        assert!(!response_content_type_is_event_stream(&hdrs(Some(
            "application/json"
        ))));
    }

    #[test]
    fn pure_event_stream_is_true() {
        assert!(response_content_type_is_event_stream(&hdrs(Some(
            "text/event-stream"
        ))));
    }

    #[test]
    fn event_stream_with_charset_param_is_true() {
        // Real upstreams append parameters; the check must
        // tolerate them.
        assert!(response_content_type_is_event_stream(&hdrs(Some(
            "text/event-stream; charset=utf-8"
        ))));
    }

    #[test]
    fn event_stream_uppercase_is_true() {
        // Header values aren't case-sensitive for the
        // media-type token; some upstreams capitalize. The
        // check must be case-insensitive.
        assert!(response_content_type_is_event_stream(&hdrs(Some(
            "Text/Event-Stream"
        ))));
    }

    #[test]
    fn event_stream_substring_does_not_false_positive() {
        // Same defence-in-depth rule as the Accept-side helper.
        assert!(!response_content_type_is_event_stream(&hdrs(Some(
            "text/event-stream-something-else"
        ))));
    }
}

#[cfg(test)]
mod build_target_url_tests {
    //! Unit tests for [`build_target_url`].
    //!
    //! The function is the single source of truth for the inbound→upstream
    //! URL mapping and is heavily relied on by the rest of mcp.rs. These
    //! tests pin down the three dispatch branches (root, .well-known,
    //! sub-path) plus the normalisation behaviour that fixed the v0.7.95
    //! bench finding about `host//mcp` from trailing slashes.
    //!
    //! Test fixtures construct an `McpServer` with the minimum required
    //! fields; the rest are defaulted via the `mk_server` helper. The
    //! macro means the field set can grow without churning every test.
    use super::build_target_url;
    use crate::models::db::McpServer;

    fn mk_server(base_url: &str, mcp_endpoint_path: Option<&str>) -> McpServer {
        McpServer {
            id: "mcp-test".to_string(),
            tenant_id: "default".to_string(),
            name: "test-server".to_string(),
            base_url: base_url.to_string(),
            mcp_endpoint_path: mcp_endpoint_path.map(String::from),
            auth_type: None,
            auth_token: None,
            timeout: None,
            enabled: true,
            mcp_apps_enabled: None,
            capabilities: None,
            health_status: None,
            idp_id: None,
            max_concurrent_requests: None,
            retry_attempts: None,
            retry_after_ms: None,
            failure_threshold: None,
            recovery_timeout_secs: None,
            wants_listener: None,
            region: None,
            protocol_version: None,
            dialect: None,
        }
    }

    // ──────────────────────────────────────────────────────────────
    // Canonical root path (None / Some(""))
    // ──────────────────────────────────────────────────────────────

    #[test]
    fn root_no_endpoint_path_returns_base_url() {
        let s = mk_server("https://knowledge-mcp.global.api.aws", None);
        assert_eq!(
            build_target_url(&s, None),
            "https://knowledge-mcp.global.api.aws"
        );
    }

    #[test]
    fn root_empty_endpoint_path_returns_base_url() {
        let s = mk_server("https://knowledge-mcp.global.api.aws", Some(""));
        assert_eq!(
            build_target_url(&s, None),
            "https://knowledge-mcp.global.api.aws"
        );
    }

    #[test]
    fn root_with_leading_slash_endpoint_path() {
        let s = mk_server("https://mcp.deepwiki.com", Some("/mcp"));
        assert_eq!(build_target_url(&s, None), "https://mcp.deepwiki.com/mcp");
    }

    #[test]
    fn root_trims_trailing_slash_on_base_url() {
        // The exact v0.7.95 bench finding: base_url="http://x:8080/"
        // + mcp_endpoint_path="/mcp" used to produce "http://x:8080//mcp".
        let s = mk_server("http://x:8080/", Some("/mcp"));
        assert_eq!(build_target_url(&s, None), "http://x:8080/mcp");
    }

    #[test]
    fn root_inserts_slash_when_endpoint_path_lacks_one() {
        // Operator typed `mcp` (no leading slash). Insert one rather
        // than collapse to `host8080mcp`.
        let s = mk_server("http://x:8080", Some("mcp"));
        assert_eq!(build_target_url(&s, None), "http://x:8080/mcp");
    }

    #[test]
    fn root_trims_trailing_slash_with_no_endpoint_path() {
        let s = mk_server("https://example.com/", None);
        assert_eq!(build_target_url(&s, None), "https://example.com");
    }

    // ──────────────────────────────────────────────────────────────
    // OAuth discovery branch (path starts with `.`)
    // ──────────────────────────────────────────────────────────────

    #[test]
    fn well_known_rewrites_to_origin_dropping_endpoint_path() {
        // The whole reason this branch exists: discovery metadata
        // lives at the server origin per RFC 8414 / 9728, never
        // under mcp_endpoint_path.
        let s = mk_server("https://example.com/api/mcp", Some("/mcp"));
        assert_eq!(
            build_target_url(&s, Some(".well-known/oauth-protected-resource")),
            "https://example.com/.well-known/oauth-protected-resource"
        );
    }

    #[test]
    fn well_known_uses_origin_for_authorization_server_metadata() {
        let s = mk_server("https://example.com/path", Some("/mcp"));
        assert_eq!(
            build_target_url(&s, Some(".well-known/oauth-authorization-server")),
            "https://example.com/.well-known/oauth-authorization-server"
        );
    }

    // ──────────────────────────────────────────────────────────────
    // REST-style passthrough (any other sub-path)
    // ──────────────────────────────────────────────────────────────

    #[test]
    fn rest_passthrough_appends_path_verbatim() {
        let s = mk_server("https://upstream.example.com", Some("/mcp"));
        assert_eq!(
            build_target_url(&s, Some("tools/list")),
            "https://upstream.example.com/tools/list"
        );
    }

    #[test]
    fn rest_passthrough_replaces_endpoint_path_with_client_path() {
        // This is the property the api-programmatic-access guide
        // and the rustdoc both document: client-supplied path
        // REPLACES mcp_endpoint_path rather than stacking on top.
        // So `/v1/proxy/mcp/{id}/foo` against a server with
        // mcp_endpoint_path=/mcp forwards to `<base>/foo`, NOT
        // `<base>/mcp/foo`.
        let s = mk_server("https://mcp.deepwiki.com", Some("/mcp"));
        assert_eq!(
            build_target_url(&s, Some("foo")),
            "https://mcp.deepwiki.com/foo"
        );
    }

    #[test]
    fn rest_passthrough_trims_base_trailing_slash() {
        // Same normalisation must apply to the passthrough branch.
        let s = mk_server("http://x:8080/", Some("/mcp"));
        assert_eq!(
            build_target_url(&s, Some("custom/path")),
            "http://x:8080/custom/path"
        );
    }
}

#[cfg(test)]
mod outbound_accept_enforcement_tests {
    //! Unit tests for [`enforce_mcp_outbound_accept`].
    //!
    //! These tests pin down the proxy's spec-compliance posture on the
    //! outbound MCP leg. The contract under test: regardless of what the
    //! inbound client sent in `Accept`, the outbound `HeaderMap` ends up
    //! with `Accept: application/json, text/event-stream` — which is
    //! exactly what the MCP Streamable HTTP spec (both 2025-03-26
    //! and 2025-06-18) requires the client to advertise.
    //!
    //! Regression context: a v0.7.95 bench against db-mcp (reference
    //! Python MCP SDK upstream) returned `406 Not Acceptable` because
    //! the proxy forwarded the client's `Accept: application/json` only.
    //! These tests prevent that regression class from recurring.

    use super::enforce_mcp_outbound_accept;
    use reqwest::header::{ACCEPT, HeaderMap, HeaderValue};

    const EXPECTED: &str = "application/json, text/event-stream";

    fn accept_value(headers: &HeaderMap) -> &str {
        headers
            .get(ACCEPT)
            .expect("Accept must be present after enforcement")
            .to_str()
            .expect("Accept must be UTF-8 after enforcement")
    }

    #[test]
    fn adds_accept_when_missing() {
        let mut h = HeaderMap::new();
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn overrides_json_only_accept() {
        // The exact bug from the bench report: client sent
        // `Accept: application/json` only and the proxy forwarded it
        // verbatim, triggering 406 from spec-compliant upstreams.
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("application/json"));
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn overrides_wildcard_accept() {
        // Many HTTP clients default to `Accept: */*`. Forwarding that
        // works against lax upstreams but is non-compliant per the
        // strict reading of the specification — overwrite it.
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("*/*"));
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn overrides_event_stream_only_accept() {
        // Some clients on the SSE listener path advertise only
        // text/event-stream; the spec still requires both.
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static("text/event-stream"));
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn overrides_html_accept_browser_passthrough() {
        // A browser tab pasted the proxy URL — Accept starts with
        // `text/html`. The earlier short-circuit gate rejects browser
        // GETs before this helper runs, but for any POST that slips
        // past with the same header, we still must enforce.
        let mut h = HeaderMap::new();
        h.insert(
            ACCEPT,
            HeaderValue::from_static(
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
            ),
        );
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn idempotent_when_already_correct() {
        // The reference SDKs send exactly the right value; calling
        // enforce on it should be a no-op (same string in, same string
        // out). Important for log volume: we shouldn't be mutating
        // headers that were already correct.
        let mut h = HeaderMap::new();
        h.insert(ACCEPT, HeaderValue::from_static(EXPECTED));
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
    }

    #[test]
    fn does_not_remove_unrelated_headers() {
        // Defensive: enforce must not accidentally clear other
        // headers (Authorization, X-Correlation-ID, etc.). This
        // catches a future regression where someone replaces
        // `.insert(ACCEPT, …)` with something that drains the map.
        let mut h = HeaderMap::new();
        h.insert(
            reqwest::header::AUTHORIZATION,
            HeaderValue::from_static("Bearer sk_brutor_api_test"),
        );
        h.insert(
            reqwest::header::HeaderName::from_static("x-correlation-id"),
            HeaderValue::from_static("abc-123"),
        );
        enforce_mcp_outbound_accept(&mut h);
        assert_eq!(accept_value(&h), EXPECTED);
        assert_eq!(
            h.get(reqwest::header::AUTHORIZATION).unwrap(),
            "Bearer sk_brutor_api_test"
        );
        assert_eq!(h.get("x-correlation-id").unwrap(), "abc-123");
    }
}

/// Filter SSE data by modifying JSON-RPC result arrays in `data:` lines.
///
/// SSE format: lines like `event: message\ndata: {...}\n\n`
/// We parse each `data:` line as JSON, filter the capability array, re-serialize.
fn filter_sse_data(
    raw: &[u8],
    json_key: &str,
    name_field: &str,
    enabled_set: &std::collections::HashSet<String>,
) -> Vec<u8> {
    let text = String::from_utf8_lossy(raw);
    let mut output = String::with_capacity(text.len());

    for line in text.split('\n') {
        if let Some(json_str) = line.strip_prefix("data: ") {
            // Try to parse and filter the JSON
            if let Ok(mut json_val) = serde_json::from_str::<Value>(json_str) {
                let mut filtered = false;
                // Try result.{json_key} first (standard JSON-RPC wrapper)
                if let Some(result) = json_val.get_mut("result") {
                    if let Some(items) = result.get_mut(json_key).and_then(|v| v.as_array_mut()) {
                        items.retain(|item| {
                            item.get(name_field)
                                .and_then(|n| n.as_str())
                                .map(|n| enabled_set.contains(n))
                                .unwrap_or(true)
                        });
                        filtered = true;
                    }
                }
                // Fallback: try top-level {json_key}
                if !filtered {
                    if let Some(items) = json_val.get_mut(json_key).and_then(|v| v.as_array_mut()) {
                        items.retain(|item| {
                            item.get(name_field)
                                .and_then(|n| n.as_str())
                                .map(|n| enabled_set.contains(n))
                                .unwrap_or(true)
                        });
                    }
                }
                output.push_str("data: ");
                output.push_str(
                    &serde_json::to_string(&json_val).unwrap_or_else(|_| json_str.to_string()),
                );
                output.push('\n');
            } else {
                // Not valid JSON, pass through unchanged
                output.push_str(line);
                output.push('\n');
            }
        } else {
            output.push_str(line);
            output.push('\n');
        }
    }

    output.into_bytes()
}

/// Extract the combined `result.content[*].text` from an SSE buffer carrying a
/// JSON-RPC tool result, so output guardrails can run on an SSE-delivered
/// `tools/call` response. The buffered (application/json) path already does
/// this; SSE tool results were previously forwarded ungoverned. Returns `None`
/// when the buffer has no text tool-result.
fn sse_tool_result_combined_text(raw: &[u8]) -> Option<String> {
    let text = String::from_utf8_lossy(raw);
    let mut combined = String::new();
    let mut found = false;
    for line in text.split('\n') {
        let json_str = match line.strip_prefix("data:") {
            Some(s) => s.strip_prefix(' ').unwrap_or(s),
            None => continue,
        };
        let Ok(json_val) = serde_json::from_str::<Value>(json_str) else {
            continue;
        };
        if let Some(content) = json_val
            .get("result")
            .and_then(|r| r.get("content"))
            .and_then(|c| c.as_array())
        {
            for item in content {
                if item.get("type").and_then(|t| t.as_str()) == Some("text") {
                    if let Some(t) = item.get("text").and_then(|t| t.as_str()) {
                        if found {
                            combined.push('\n');
                        }
                        combined.push_str(t);
                        found = true;
                    }
                }
            }
        }
    }
    if found { Some(combined) } else { None }
}

/// Re-splice redacted text into the `result.content[*].text` fields of an SSE
/// tool-result buffer, returning rewritten SSE bytes. The redacted blob is
/// split on `\n` across the text slots in order (mirrors the buffered path).
/// If the slot count drifts from the redacted segment count, the original
/// buffer is returned unchanged — better to forward than corrupt.
fn redact_sse_tool_result(raw: &[u8], redacted_combined: &str) -> Vec<u8> {
    let text = String::from_utf8_lossy(raw);
    let parts: Vec<&str> = redacted_combined.split('\n').collect();
    let mut idx = 0usize;
    let mut output = String::with_capacity(text.len());
    for line in text.split('\n') {
        if let Some(rest) = line.strip_prefix("data:") {
            let json_str = rest.strip_prefix(' ').unwrap_or(rest);
            if let Ok(mut json_val) = serde_json::from_str::<Value>(json_str) {
                if let Some(content) = json_val
                    .get_mut("result")
                    .and_then(|r| r.get_mut("content"))
                    .and_then(|c| c.as_array_mut())
                {
                    for item in content.iter_mut() {
                        if item.get("type").and_then(|t| t.as_str()) == Some("text")
                            && item.get("text").and_then(|t| t.as_str()).is_some()
                        {
                            if let Some(p) = parts.get(idx) {
                                if let Some(slot) = item.get_mut("text") {
                                    *slot = Value::String((*p).to_string());
                                }
                            }
                            idx += 1;
                        }
                    }
                }
                output.push_str("data: ");
                output.push_str(
                    &serde_json::to_string(&json_val).unwrap_or_else(|_| json_str.to_string()),
                );
                output.push('\n');
                continue;
            }
        }
        output.push_str(line);
        output.push('\n');
    }
    if idx != parts.len() {
        // Slot/segment mismatch — don't risk corrupting the body.
        return raw.to_vec();
    }
    output.into_bytes()
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

/// JSON-RPC 2.0 server-error code reserved for "server-defined"
/// errors (range -32000 to -32099). Policy denials by the gateway
/// fit cleanly here — they're neither "internal error" (-32603,
/// which implies a bug) nor parse / param errors. We pick -32000
/// as a fixed point so client SDKs can switch on it directly.
const JSONRPC_GUARDRAIL_BLOCKED: i32 = -32000;

/// Build a JSON-RPC 2.0 error response for a guardrail violation
/// on an inbound MCP request. The previous shape (`{"error": {…}}`)
/// was not spec-compliant: a JSON-RPC 2.0 response MUST carry
/// `jsonrpc: "2.0"` AND the request `id` (echoed back so the
/// client can correlate the failure to the call it made). The
/// detailed guardrail attribution (`check`, `guardrail`,
/// `direction`) moves into the spec-defined `data` slot inside
/// `error` — clients that don't care about the detail still see
/// a well-formed JSON-RPC error.
/// Build the JSON-RPC 2.0 body for a guardrail rejection. Pulled
/// out of any one call site so the caller can stash the
/// serialised form on the proxy_log row's `response_body_preview`
/// without re-serialising the same Value. `direction` is "input"
/// (block fired on the request body before forwarding upstream)
/// or "output" (block fired on the upstream's response before
/// returning to the client) — the value surfaces in
/// `error.data.direction` so SIEM-side dashboards can split block
/// volume by side.
fn guardrail_violation_body(
    violation: &guardrails::GuardrailViolation,
    request_id: Option<&Value>,
    direction: &str,
) -> Value {
    json!({
        "jsonrpc": "2.0",
        // `null` is the spec-mandated id for the case where we
        // couldn't parse one out of the request — better than
        // dropping the field entirely.
        "id": request_id.cloned().unwrap_or(Value::Null),
        "error": {
            "code": JSONRPC_GUARDRAIL_BLOCKED,
            "message": violation.message,
            "data": {
                "code": "guardrail_blocked",
                "check": violation.check,
                "guardrail": violation.config_name,
                "direction": direction,
            }
        }
    })
}

/// Extract a JSON-RPC `id` from a request body if it is parseable
/// JSON-RPC. Returns `None` for non-JSON-RPC bodies or bodies that
/// omit `id` (notifications). The id type is opaque per spec
/// (string | number | null) so we pass the raw Value through.
fn extract_rpc_id(body: &[u8]) -> Option<Value> {
    serde_json::from_slice::<Value>(body)
        .ok()
        .and_then(|v| v.get("id").cloned())
}

fn usage_limit_response(enforcement: &crate::models::proxy::UsageEnforcementResult) -> Response {
    let body = json!({
        "error": enforcement.message.as_deref().unwrap_or("Usage limit exceeded"),
        "usage_limit": {
            "group_id": enforcement.group_id,
            "group_name": enforcement.group_name,
            "limit_type": enforcement.limit_type,
            "current_usage": enforcement.current_usage,
            "max_limit": enforcement.max_limit,
        }
    });
    let retry_after = crate::services::usage::retry_after_seconds_for_limit_type(
        enforcement.limit_type.as_deref(),
    );
    (
        StatusCode::TOO_MANY_REQUESTS,
        [("Retry-After", retry_after.to_string())],
        Json(body),
    )
        .into_response()
}

/// JSON-RPC 2.0 server-defined error code for an MCP rate-limit rejection.
/// Same -32000..-32099 band as a guardrail block; `error.data.code`
/// distinguishes `rate_limit_exceeded` from `guardrail_blocked`.
const JSONRPC_RATE_LIMITED: i32 = -32000;

/// Build the HTTP 429 response for an MCP rate-limit block.
///
/// Carries both layers: HTTP 429 + RFC 6585 `Retry-After` (for proxies and
/// load balancers) and a spec-compliant JSON-RPC 2.0 error envelope in the
/// body (for MCP clients that parse it). The request `id` is echoed so the
/// client can correlate the rejection to the call it made.
/// The JSON-RPC error a system server's tool call is refused with by a rate
/// limit (the answer is a 429).
fn system_tool_rate_limit_body(
    block: &crate::services::usage::McpRateLimitBlock,
    request_id: Option<&Value>,
) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": request_id.cloned().unwrap_or(Value::Null),
        "error": {
            "code": -32000,
            "message": block.message,
            "data": {"rate_limit": {
                "scope": block.scope,
                "limit_type": block.limit_type,
                "retry_after_secs": block.retry_after_secs,
            }}
        }
    })
}

fn mcp_rate_limit_response(
    block: &crate::services::usage::McpRateLimitBlock,
    request_id: Option<&Value>,
) -> Response {
    let body = json!({
        "jsonrpc": "2.0",
        "id": request_id.cloned().unwrap_or(Value::Null),
        "error": {
            "code": JSONRPC_RATE_LIMITED,
            "message": block.message,
            "data": {
                "code": "rate_limit_exceeded",
                "scope": block.scope,
                "limit_type": block.limit_type,
                "retry_after_secs": block.retry_after_secs,
            }
        }
    });
    (
        StatusCode::TOO_MANY_REQUESTS,
        [("Retry-After", block.retry_after_secs.max(1).to_string())],
        Json(body),
    )
        .into_response()
}

#[cfg(test)]
mod sse_output_guard_tests {
    use super::{redact_sse_tool_result, sse_tool_result_combined_text};

    const SSE: &str = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"my ssn is 123-45-6789\"}]}}\n\n";

    #[test]
    fn extracts_tool_result_text() {
        let got = sse_tool_result_combined_text(SSE.as_bytes());
        assert_eq!(got.as_deref(), Some("my ssn is 123-45-6789"));
    }

    #[test]
    fn extracts_none_when_no_text_result() {
        // A notification / non-result event has no result.content text.
        let sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\",\"params\":{}}\n\n";
        assert!(sse_tool_result_combined_text(sse.as_bytes()).is_none());
    }

    #[test]
    fn redacts_text_in_place_preserving_sse_framing() {
        let out = redact_sse_tool_result(SSE.as_bytes(), "my ssn is [REDACTED-SSN]");
        let out_str = String::from_utf8(out).unwrap();
        assert!(
            out_str.contains("[REDACTED-SSN]"),
            "redacted text spliced: {out_str}"
        );
        assert!(!out_str.contains("123-45-6789"), "original PII removed");
        // Still parseable JSON-RPC with the redaction applied.
        let data_line = out_str
            .lines()
            .find_map(|l| l.strip_prefix("data: "))
            .unwrap();
        let v: serde_json::Value = serde_json::from_str(data_line).unwrap();
        assert_eq!(
            v["result"]["content"][0]["text"],
            "my ssn is [REDACTED-SSN]"
        );
    }

    #[test]
    fn redact_bails_to_original_on_slot_count_mismatch() {
        // Redacted blob has 2 segments but the result has 1 text slot →
        // return original unchanged rather than corrupt.
        let out = redact_sse_tool_result(SSE.as_bytes(), "a\nb");
        assert_eq!(out, SSE.as_bytes());
    }

    #[test]
    fn multi_text_slots_round_trip() {
        let sse = "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"content\":[{\"type\":\"text\",\"text\":\"one\"},{\"type\":\"image\",\"data\":\"x\"},{\"type\":\"text\",\"text\":\"two\"}]}}\n\n";
        assert_eq!(
            sse_tool_result_combined_text(sse.as_bytes()).as_deref(),
            Some("one\ntwo")
        );
        let out = String::from_utf8(redact_sse_tool_result(sse.as_bytes(), "ONE\nTWO")).unwrap();
        let data_line = out.lines().find_map(|l| l.strip_prefix("data: ")).unwrap();
        let v: serde_json::Value = serde_json::from_str(data_line).unwrap();
        assert_eq!(v["result"]["content"][0]["text"], "ONE");
        assert_eq!(v["result"]["content"][1]["type"], "image"); // untouched
        assert_eq!(v["result"]["content"][2]["text"], "TWO");
    }
}

#[cfg(test)]
mod rate_limit_response_tests {
    use super::{JSONRPC_RATE_LIMITED, mcp_rate_limit_response};
    use crate::services::usage::McpRateLimitBlock;
    use axum::body::to_bytes;
    use axum::http::StatusCode;
    use serde_json::{Value, json};

    fn sample_block() -> McpRateLimitBlock {
        McpRateLimitBlock {
            scope: "group-tool",
            limit_type: "rate",
            retry_after_secs: 12,
            message: "Rate limit exceeded for tool generate_report".to_string(),
        }
    }

    #[tokio::test]
    async fn response_status_is_429_with_retry_after_header() {
        let resp = mcp_rate_limit_response(&sample_block(), Some(&json!(7)));
        assert_eq!(resp.status(), StatusCode::TOO_MANY_REQUESTS);
        let retry = resp
            .headers()
            .get("Retry-After")
            .and_then(|v| v.to_str().ok())
            .expect("Retry-After header");
        assert_eq!(retry, "12", "Retry-After must echo block.retry_after_secs");
    }

    #[tokio::test]
    async fn retry_after_floors_at_one_second_even_if_block_says_zero() {
        let mut block = sample_block();
        block.retry_after_secs = 0;
        let resp = mcp_rate_limit_response(&block, None);
        let retry = resp.headers().get("Retry-After").unwrap().to_str().unwrap();
        assert_eq!(
            retry, "1",
            "Retry-After must be >= 1 even when block.retry_after_secs is 0"
        );
    }

    #[tokio::test]
    async fn body_is_jsonrpc_envelope_with_echoed_id() {
        let resp = mcp_rate_limit_response(&sample_block(), Some(&json!(42)));
        let body = to_bytes(resp.into_body(), 4096).await.expect("body bytes");
        let v: Value = serde_json::from_slice(&body).expect("JSON body");
        assert_eq!(v["jsonrpc"], "2.0");
        assert_eq!(
            v["id"], 42,
            "the request id must be echoed for client correlation"
        );
        assert_eq!(
            v["error"]["code"], JSONRPC_RATE_LIMITED,
            "error code must be the rate-limit slot in the JSON-RPC server-defined range",
        );
        assert_eq!(v["error"]["data"]["code"], "rate_limit_exceeded");
        assert_eq!(v["error"]["data"]["scope"], "group-tool");
        assert_eq!(v["error"]["data"]["limit_type"], "rate");
        assert_eq!(v["error"]["data"]["retry_after_secs"], 12);
    }

    #[tokio::test]
    async fn missing_request_id_is_null_in_envelope() {
        let resp = mcp_rate_limit_response(&sample_block(), None);
        let body = to_bytes(resp.into_body(), 4096).await.unwrap();
        let v: Value = serde_json::from_slice(&body).unwrap();
        assert!(
            v["id"].is_null(),
            "id must be JSON null when no request id supplied"
        );
    }
}

async fn health(State(state): State<SharedState>) -> Json<Value> {
    let db_ok = sqlx::query("SELECT 1").execute(&state.db).await.is_ok();
    Json(json!({
        "status": if db_ok { "healthy" } else { "degraded" },
        "service": "brutor-gateway-core-mcp",
    }))
}

/// The tenant a scope declaration belongs to: the target group's when the
/// call has one, otherwise the caller's.
fn tg_tenant_or(
    target_group: &Option<crate::models::db::ResourceGroup>,
    auth_ctx: &crate::models::auth::AuthContext,
) -> String {
    target_group
        .as_ref()
        .map(|g| g.tenant_id.clone())
        .unwrap_or_else(|| auth_ctx.tenant_id.clone())
}

/// What a refusal row of one MCP request carries.
struct McpRowCtx<'a> {
    state: &'a crate::services::SharedState,
    auth_ctx: &'a crate::models::auth::AuthContext,
    headers: &'a HeaderMap,
    method: &'a str,
    target_url: &'a str,
    server_id: &'a str,
    group_id: Option<String>,
    correlation_id: &'a str,
    start: Instant,
    call_chain: &'a CallChain,
    current_task_id_str: &'a str,
    trace_continuity: TraceContinuity,
    trace_root_kind: Option<&'a str>,
}

/// Why an SSE relay stopped before the upstream's end.
enum SseCut {
    /// The answer reached the payload limit (bytes).
    Payload(usize),
    /// The upstream broke off while the relay buffered.
    Upstream(String),
    /// The call's deadline passed.
    Timeout,
}

impl SseCut {
    fn status(&self) -> u16 {
        match self {
            SseCut::Timeout => 504,
            _ => 502,
        }
    }

    fn message(&self) -> String {
        match self {
            SseCut::Payload(limit) => {
                format!("The MCP server's answer exceeds the payload limit of {limit} bytes")
            }
            SseCut::Upstream(e) => format!("The MCP server's answer broke off: {e}"),
            SseCut::Timeout => "MCP server timeout".to_string(),
        }
    }

    /// The JSON-RPC error the stream ends with.
    fn error(&self, id: Option<&Value>) -> Value {
        match self {
            SseCut::Payload(limit) => {
                crate::services::mcp_limits::payload_too_large_error(id, *limit)
            }
            SseCut::Upstream(_) | SseCut::Timeout => json!({
                "jsonrpc": "2.0",
                "id": id.cloned().unwrap_or(Value::Null),
                "error": {"code": -32000, "message": self.message(),
                          "data": {"code": match self {
                              SseCut::Timeout => "timeout",
                              _ => "upstream_error",
                          }}}
            }),
        }
    }
}

/// A request body over the call's payload ceiling: 413, naming the limit,
/// on the record as `payload_limit`.
fn refuse_request_payload(ctx: McpRowCtx<'_>, limit: usize) -> Response {
    let message = format!("The request body exceeds the MCP payload limit of {limit} bytes");
    refusal_row(&ctx, 413, &message, Some("payload_limit"));
    error_response(StatusCode::PAYLOAD_TOO_LARGE, &message)
}

/// A buffered upstream answer that could not be taken: over the payload
/// limit (502, JSON-RPC `payload_too_large`), broken off (502) or past the
/// call's deadline (504). On the record, never an empty answer.
fn refuse_upstream_answer(
    ctx: McpRowCtx<'_>,
    e: crate::services::mcp_limits::BodyReadError,
    rpc_id: Option<&Value>,
) -> Response {
    use crate::services::mcp_limits::BodyReadError;
    ctx.state.mcp_health.record_failure(
        ctx.state.db.clone(),
        &ctx.auth_ctx.tenant_id,
        ctx.server_id,
        &format!("{e:?}"),
    );
    match e {
        BodyReadError::TooLarge(limit) => {
            let body = crate::services::mcp_limits::payload_too_large_error(rpc_id, limit);
            let message = body["error"]["message"]
                .as_str()
                .unwrap_or_default()
                .to_string();
            refusal_row(&ctx, 502, &message, Some("payload_limit"));
            (StatusCode::BAD_GATEWAY, Json(body)).into_response()
        }
        BodyReadError::Upstream(err) => {
            let message = format!("The MCP server's answer broke off: {err}");
            refusal_row(&ctx, 502, &message, None);
            error_response(StatusCode::BAD_GATEWAY, &message)
        }
        BodyReadError::Deadline => {
            refusal_row(&ctx, 504, "MCP server timeout", None);
            error_response(StatusCode::GATEWAY_TIMEOUT, "MCP server timeout")
        }
    }
}

/// The audit row of a call ended by the gateway with `status`.
fn refusal_row(ctx: &McpRowCtx<'_>, status: u16, message: &str, subtype: Option<&str>) {
    proxy_metrics::record_request("mcp", ctx.method, status, "mcp");
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        ctx.auth_ctx,
        ctx.method,
        "mcp-proxy",
        Some(ctx.target_url),
        Some(ctx.server_id),
        status,
        ctx.start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ctx.correlation_id,
    );
    log.user_agent = extract_user_agent(ctx.headers);
    log.resource_group_id = ctx.group_id.clone();
    log.proxy_subtype = subtype.map(String::from);
    proxy_logger::apply_call_chain(
        &mut log,
        ctx.call_chain,
        ctx.current_task_id_str,
        ctx.trace_continuity,
        ctx.trace_root_kind,
    );
    ctx.state
        .background_writer
        .write_proxy_log(ctx.state.db.clone(), log);
}

/// A call refused by an MCP limit (`block`): the JSON-RPC 429 of
/// [`mcp_rate_limit_response`], on the record as `rate_limit_{scope}`.
fn refuse_mcp_limit(
    ctx: McpRowCtx<'_>,
    block: &crate::services::usage::McpRateLimitBlock,
    rpc_id: Option<&Value>,
    mcp_method: Option<&str>,
    tool: Option<&str>,
) -> Response {
    proxy_metrics::record_request("mcp", ctx.method, 429, "n/a");
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        ctx.auth_ctx,
        ctx.method,
        "mcp-proxy",
        Some(ctx.target_url),
        Some(ctx.server_id),
        429,
        ctx.start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(&block.message),
        ctx.correlation_id,
    );
    log.user_agent = extract_user_agent(ctx.headers);
    log.resource_group_id = ctx.group_id;
    log.proxy_subtype = Some(format!("rate_limit_{}", block.scope));
    log.mcp_method = mcp_method.map(String::from);
    log.mcp_capability_name = tool.map(String::from);
    proxy_logger::apply_call_chain(
        &mut log,
        ctx.call_chain,
        ctx.current_task_id_str,
        ctx.trace_continuity,
        ctx.trace_root_kind,
    );
    ctx.state
        .background_writer
        .write_proxy_log(ctx.state.db.clone(), log);
    mcp_rate_limit_response(block, rpc_id)
}

/// The guardrail configs could not be read, so the call is refused (fail
/// closed). On the record like every other refusal, sealed
/// `engine_failure`: no policy decided it. A row is written for these 503s too.
fn refuse_unchecked_mcp(ctx: McpRowCtx<'_>, message: &str) -> Response {
    let response = error_response(StatusCode::SERVICE_UNAVAILABLE, message);
    let status = response.status().as_u16();
    proxy_metrics::record_request("mcp", ctx.method, status, "n/a");
    let mut log = proxy_logger::build_proxy_log(
        "mcp",
        ctx.auth_ctx,
        ctx.method,
        "mcp-proxy",
        Some(ctx.target_url),
        Some(ctx.server_id),
        status,
        ctx.start.elapsed().as_secs_f64() * 1000.0,
        false,
        Some(message),
        ctx.correlation_id,
    );
    log.user_agent = extract_user_agent(ctx.headers);
    log.resource_group_id = ctx.group_id;
    log.proxy_subtype = Some(crate::constants::proxy_subtype::ENGINE_FAILURE.to_string());
    proxy_logger::apply_call_chain(
        &mut log,
        ctx.call_chain,
        ctx.current_task_id_str,
        ctx.trace_continuity,
        ctx.trace_root_kind,
    );
    ctx.state
        .background_writer
        .write_proxy_log(ctx.state.db.clone(), log);
    response
}

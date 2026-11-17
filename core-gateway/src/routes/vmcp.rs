// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Virtual MCP Proxy — aggregates tools/resources from multiple MCP servers
// behind a single endpoint.
//
// Architecture:
//
//   tools/list:
//     Per upstream-server, call `usage::get_enabled_capabilities()` with
//     the caller's resource groups → only the upstream tools the caller
//     is allowed to use survive. The surviving entries are returned as
//     proper MCP tool objects built from the `tool_def` snapshots
//     stored on the VMCP's capabilities JSON. Per-entry `exposed_name`
//     and `description_override` are applied on the way out so the
//     client sees the operator's chosen names + descriptions.
//
//   tools/call:
//     The client calls a tool by its EXPOSED name. The VMCP looks up
//     the exposed name in `reverse_name_mapping` → original
//     `capability_name`, and in `source_mapping` → upstream server_id.
//     The body's `params.name` is rewritten to the original name and
//     the request is forwarded via internal HTTP loopback to
//     `/v1/proxy/mcp/{upstream_server_id}` — which runs the full
//     `proxy_request()` pipeline (OAuth injection, guardrails, logging).
//
// The capabilities JSON shape is the output of
// `app/services/mcp/server/vmcp_normalizer.py` — see that module for
// the canonical contract.

use std::collections::HashMap;
use std::time::Instant;

use axum::Router;
use axum::extract::Path;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Json, Response};
use axum::routing::{get, post};
use serde_json::{Map, Value, json};

use crate::middleware::tenant::TenantContext;
use crate::models::db::VirtualServer;
use crate::services::call_chain::{derive_tenant_key, parse_or_mint};
use crate::services::{SharedState, auth, usage};

pub fn router() -> Router<SharedState> {
    Router::new()
        .route("/health", get(health))
        .route("/{virtual_server_id}", post(proxy))
}

#[utoipa::path(
    post,
    path = "/v1/proxy/vmcp/{virtual_server_id}",
    tag = "Virtual MCP",
    summary = "Virtual MCP server proxy",
    description = "Routes MCP JSON-RPC requests to the appropriate source server based on tool/resource/prompt name.",
    params(
        ("virtual_server_id" = String, Path, description = "Virtual server ID"),
    ),
    responses(
        (status = 200, description = "Aggregated MCP response"),
        (status = 401, description = "Authentication required"),
        (status = 404, description = "Virtual server not found"),
    ),
    security(("api_key" = []), ("bearer" = []))
)]
pub(crate) async fn proxy(
    State(state): State<SharedState>,
    headers: HeaderMap,
    tenant: Option<axum::Extension<TenantContext>>,
    Path(virtual_server_id): Path<String>,
    Json(body): Json<Value>,
) -> Response {
    let _start = Instant::now();
    let correlation_id = headers
        .get(crate::constants::headers::X_CORRELATION_ID)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    let correlation_id = if correlation_id.is_empty() {
        uuid::Uuid::new_v4().to_string()
    } else {
        correlation_id
    };

    let tenant_id = match tenant {
        Some(t) => t.tenant_id.clone(),
        None => return error_response(StatusCode::BAD_REQUEST, "Missing tenant context"),
    };

    // Parse-or-mint the chain at the VMCP entry.
    // VMCP is a real governance step (capability filter, OAuth fanout,
    // poison-scan on tool description customization) — the trace UI
    // shows it as a node. The chain is forwarded to the inner MCP
    // proxy via OUTBOUND_CHAIN_HEADERS so the inner route inherits
    // continuity instead of seeing depth-0 / sig_failed.
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), &tenant_id);
    let (call_chain, _current_task_id, _trace_continuity) = parse_or_mint(
        |n| {
            headers
                .get(n)
                .and_then(|v| v.to_str().ok())
                .map(String::from)
        },
        &tenant_key,
    );
    let outbound_chain_owned: Vec<(&'static str, String)> = call_chain
        .to_header_pairs(&tenant_key)
        .into_iter()
        .collect();

    // We now USE the auth context (was `_auth_ctx`) for
    // resource-group resolution per upstream. Without this the
    // resource-group capability filter doesn't cascade into VMCP
    // tools/list responses.
    let auth_ctx = match auth::authenticate_request(
        &headers,
        &tenant_id,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    {
        Ok(ctx) => ctx,
        Err(e) => return error_response(e.status_code(), &e.to_string()),
    };

    // The tenant's "Virtual MCP servers" switch (`tenant_features`).
    if let Err(sentence) = crate::services::tenant_features::require(
        &state,
        &tenant_id,
        crate::services::tenant_features::Feature::VirtualMcpServers,
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

    // Load virtual server
    let vserver: Option<VirtualServer> = match sqlx::query_as(
        "SELECT * FROM virtual_servers WHERE id = $1 AND tenant_id = $2 AND enabled = true",
    )
    .bind(&virtual_server_id)
    .bind(&tenant_id)
    .fetch_optional(&state.db)
    .await
    {
        Ok(v) => v,
        Err(e) => return crate::services::db_errors::unavailable("the virtual server", &e),
    };

    let vserver = match vserver {
        Some(v) => v,
        None => return error_response(StatusCode::NOT_FOUND, "Virtual server not found"),
    };

    let method = body.get("method").and_then(|v| v.as_str()).unwrap_or("");
    let jsonrpc_id = body.get("id").cloned();
    let capabilities = vserver.capabilities.clone().unwrap_or(json!({}));

    // JSON-RPC 2.0, "Notification": notifications (no `id` field) MUST NOT
    // receive a response. The portal SDK sends `notifications/initialized`
    // right after the initialize handshake; if we 200 it back with a
    // JSON-RPC error envelope (the old `_` arm), the SDK logs it as
    // an error. Detect notifications up-front and short-circuit with
    // 204 — no body, no JSON-RPC envelope.
    let is_notification = jsonrpc_id.is_none() && method.starts_with("notifications/");
    if is_notification {
        return StatusCode::NO_CONTENT.into_response();
    }

    // SEP-2243 — reject requests whose Mcp-Method / Mcp-Name headers
    // contradict the body (400 + -32020). Absent headers (legacy
    // clients) pass; lying headers never do, from any dialect.
    if let Some(reason) = crate::services::mcp_wire::header_body_mismatch(&headers, &body) {
        return (
            StatusCode::BAD_REQUEST,
            axum::Json(crate::services::mcp_wire::header_mismatch_error(
                jsonrpc_id.as_ref(),
                &reason,
            )),
        )
            .into_response();
    }

    match method {
        // Dual-era: the VMCP answers `server/discover`
        // (2026-07-28) alongside the legacy `initialize` handshake below.
        "server/discover" => {
            let server_name = format!("Brutor VMCP — {}", vserver.name);
            let result = crate::services::mcp_wire::discover_result(
                &[
                    crate::constants::mcp_protocol::V2026_07_28,
                    crate::constants::mcp_protocol::V2025_06_18,
                    crate::constants::mcp_protocol::V2025_03_26,
                    crate::constants::mcp_protocol::V2024_11_05,
                ],
                json!({
                    "tools": {"listChanged": false},
                    "resources": {"listChanged": false, "subscribe": false},
                    "prompts": {"listChanged": false},
                }),
                None,
                &server_name,
                env!("CARGO_PKG_VERSION"),
                // Group-filtered surface → never shared across
                // authorization contexts.
                60_000,
                crate::constants::mcp_protocol::cache_scope::PRIVATE,
            );
            // ttl/scope note: the exposed tool surface is operator-
            // driven; 60s keeps admin edits visible without hammering.
            json_response(jsonrpc_id, result, &correlation_id)
        }
        // VMCPs are aggregators — there is no single upstream to
        // forward `initialize` to. Synthesize a response declaring
        // the three capability families the VMCP fans out to. The
        // actual lists are delivered later via tools/list etc.
        //
        // protocolVersion: echo the client's requested version when
        // it's one we know; fall back to 2024-11-05 (the most-
        // implemented today). The MCP spec lets servers negotiate
        // down, but in practice every modern client accepts any of
        // these three.
        "initialize" => {
            let requested_version =
                crate::services::mcp_wire::requested_protocol_version(&headers, &body);
            let protocol_version = select_protocol_version(requested_version.as_deref());
            let server_name = format!("Brutor VMCP — {}", vserver.name);
            let result = json!({
                "protocolVersion": protocol_version,
                "capabilities": {
                    // listChanged=false: the VMCP's exposed surface
                    // is operator-driven (admin UI), not push from
                    // the upstreams. Re-list on portal reload.
                    "tools": {"listChanged": false},
                    "resources": {"listChanged": false, "subscribe": false},
                    "prompts": {"listChanged": false},
                },
                "serverInfo": {
                    "name": server_name,
                    "version": env!("CARGO_PKG_VERSION"),
                },
            });
            json_response(jsonrpc_id, result, &correlation_id)
        }

        // Liveness check. Spec: empty-object result.
        "ping" => json_response(jsonrpc_id, json!({}), &correlation_id),

        // VMCPs don't model resource templates today — the
        // capability normaliser stores `resource_templates: []`
        // unconditionally. Return an empty list rather than
        // -32601, which some SDKs treat as a hard error mid-handshake.
        "resources/templates/list" => {
            let mut result = json!({"resourceTemplates": []});
            stamp_if_modern(&headers, &body, &mut result);
            json_response(jsonrpc_id, result, &correlation_id)
        }

        "tools/list" => {
            let tools = match build_filtered_tool_list(&state, &auth_ctx, &tenant_id, &capabilities)
                .await
            {
                Ok(t) => t,
                Err(e) => {
                    return crate::services::db_errors::unavailable("capability filters", &e);
                }
            };
            let mut result = json!({ "tools": tools });
            stamp_if_modern(&headers, &body, &mut result);
            json_response(jsonrpc_id, result, &correlation_id)
        }

        "resources/list" => {
            let items = build_filtered_basic_list(
                &state,
                &auth_ctx,
                &tenant_id,
                &capabilities,
                "resources",
                "resource",
                "uri",
            )
            .await;
            let items = match items {
                Ok(i) => i,
                Err(e) => return crate::services::db_errors::unavailable("capability filters", &e),
            };
            let mut result = json!({ "resources": items });
            stamp_if_modern(&headers, &body, &mut result);
            json_response(jsonrpc_id, result, &correlation_id)
        }

        "prompts/list" => {
            let items = build_filtered_basic_list(
                &state,
                &auth_ctx,
                &tenant_id,
                &capabilities,
                "prompts",
                "prompt",
                "name",
            )
            .await;
            let items = match items {
                Ok(i) => i,
                Err(e) => return crate::services::db_errors::unavailable("capability filters", &e),
            };
            let mut result = json!({ "prompts": items });
            stamp_if_modern(&headers, &body, &mut result);
            json_response(jsonrpc_id, result, &correlation_id)
        }

        "tools/call" => {
            // Propagate the VMCP chain
            // onto the internal forward to /v1/proxy/mcp/{id}. The
            // inner MCP route inherits trace continuity instead of
            // minting a fresh root for every VMCP-dispatched call.
            crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
                .scope(
                    outbound_chain_owned.clone(),
                    handle_tools_call(
                        &state,
                        &headers,
                        &correlation_id,
                        &body,
                        &capabilities,
                        jsonrpc_id,
                    ),
                )
                .await
        }

        "resources/read" | "prompts/get" => {
            // Resources/prompts don't have rename today — fall through
            // to the existing source_mapping lookup keyed by the
            // requested name. Chain propagated for the same reason as
            // tools/call above.
            crate::services::call_chain::OUTBOUND_CHAIN_HEADERS
                .scope(
                    outbound_chain_owned,
                    handle_basic_passthrough(
                        &state,
                        &headers,
                        &correlation_id,
                        &body,
                        &capabilities,
                        jsonrpc_id,
                    ),
                )
                .await
        }

        _ => {
            let error = json!({
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "error": {
                    "code": -32601,
                    "message": format!("Method not supported: {method}")
                }
            });
            Json(error).into_response()
        }
    }
}

// ---------------------------------------------------------------------------
// tools/list — applies per-upstream capability filtering + override mechanic
// ---------------------------------------------------------------------------

/// Build a filtered list of MCP tool objects from the normalized
/// VMCP capabilities JSON, applying:
///   - Per-upstream resource-group capability filtering
///   - exposed_name override (or VMCP-wide prefix via the normalizer)
///   - description_override
///   - tool_def snapshot for proper MCP tool object shape
async fn build_filtered_tool_list(
    state: &SharedState,
    auth_ctx: &crate::models::auth::AuthContext,
    tenant_id: &str,
    capabilities: &Value,
) -> Result<Vec<Value>, sqlx::Error> {
    let tools = match capabilities.get("tools").and_then(|v| v.as_array()) {
        Some(t) => t,
        None => return Ok(Vec::new()),
    };

    // Resolve enabled capability sets once per unique upstream server
    // — operating on a 50-tool VMCP with 5 upstreams hits the DB 5
    // times, not 50.
    let mut enabled_by_server: HashMap<String, Option<std::collections::HashSet<String>>> =
        HashMap::new();
    for entry in tools {
        let server_id = match entry.get("server_id").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        if !enabled_by_server.contains_key(server_id) {
            // `?`: an unreadable group list used to be an EMPTY one,
            // which the capability map reads as "no filters", exposing every
            // upstream capability, disabled ones included.
            let groups =
                usage::resolve_all_mcp_server_groups(&state.db, auth_ctx, tenant_id, server_id)
                    .await?;
            let enabled =
                usage::get_enabled_capabilities(&state.db, server_id, "tool", &groups).await;
            enabled_by_server.insert(server_id.to_string(), enabled);
        }
    }

    let mut out: Vec<Value> = Vec::with_capacity(tools.len());
    for entry in tools {
        let server_id = match entry.get("server_id").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        let capability_name = match entry.get("capability_name").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };

        // Apply per-server filter. None = allow all; Some(set) =
        // only the listed names are permitted.
        if let Some(Some(enabled_set)) = enabled_by_server.get(server_id) {
            if !enabled_set.contains(capability_name) {
                continue;
            }
        }

        let exposed_name = entry
            .get("exposed_name")
            .and_then(|v| v.as_str())
            .unwrap_or(capability_name);
        let description_override = entry.get("description_override").and_then(|v| v.as_str());

        // Build the outgoing tool object — prefer the upstream's
        // snapshot for shape; fall back to a minimal stub if it's
        // not in the snapshot (the normalizer logs a warning when
        // it can't snapshot, so an operator still has visibility).
        let mut obj: Map<String, Value> = match entry.get("tool_def") {
            Some(Value::Object(snapshot)) => snapshot.clone(),
            _ => {
                let mut m = Map::new();
                m.insert("name".to_string(), json!(capability_name));
                m
            }
        };

        // Override the upstream's name with our exposed_name.
        obj.insert("name".to_string(), json!(exposed_name));
        if let Some(desc) = description_override {
            obj.insert("description".to_string(), json!(desc));
        }
        out.push(Value::Object(obj));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// resources/list + prompts/list — filter pass without rename mechanic
// ---------------------------------------------------------------------------

async fn build_filtered_basic_list(
    state: &SharedState,
    auth_ctx: &crate::models::auth::AuthContext,
    tenant_id: &str,
    capabilities: &Value,
    items_key: &str,
    capability_type: &str,
    name_field: &str,
) -> Result<Vec<Value>, sqlx::Error> {
    let items = match capabilities.get(items_key).and_then(|v| v.as_array()) {
        Some(t) => t,
        None => return Ok(Vec::new()),
    };

    let mut enabled_by_server: HashMap<String, Option<std::collections::HashSet<String>>> =
        HashMap::new();
    for entry in items {
        let server_id = match entry.get("server_id").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        if !enabled_by_server.contains_key(server_id) {
            // `?`: an unreadable group list used to be an EMPTY one,
            // which the capability map reads as "no filters", exposing every
            // upstream capability, disabled ones included.
            let groups =
                usage::resolve_all_mcp_server_groups(&state.db, auth_ctx, tenant_id, server_id)
                    .await?;
            let enabled =
                usage::get_enabled_capabilities(&state.db, server_id, capability_type, &groups)
                    .await;
            enabled_by_server.insert(server_id.to_string(), enabled);
        }
    }

    let mut out: Vec<Value> = Vec::with_capacity(items.len());
    for entry in items {
        let server_id = match entry.get("server_id").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        let capability_name = match entry.get("capability_name").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        if let Some(Some(enabled_set)) = enabled_by_server.get(server_id) {
            if !enabled_set.contains(capability_name) {
                continue;
            }
        }
        // Prefer the snapshot `def` if present; fall back to minimal stub.
        let obj: Map<String, Value> = match entry.get("def") {
            Some(Value::Object(snapshot)) => snapshot.clone(),
            _ => {
                let mut m = Map::new();
                m.insert(name_field.to_string(), json!(capability_name));
                m
            }
        };
        out.push(Value::Object(obj));
    }
    Ok(out)
}

// ---------------------------------------------------------------------------
// tools/call — name rewrite + loopback delegation
// ---------------------------------------------------------------------------

async fn handle_tools_call(
    state: &SharedState,
    headers: &HeaderMap,
    correlation_id: &str,
    body: &Value,
    capabilities: &Value,
    jsonrpc_id: Option<Value>,
) -> Response {
    let exposed_name = body
        .get("params")
        .and_then(|p| p.get("name"))
        .and_then(|n| n.as_str())
        .unwrap_or("");

    if exposed_name.is_empty() {
        return error_response(StatusCode::BAD_REQUEST, "Missing params.name in tools/call");
    }

    // Resolve exposed_name → (upstream_server_id, original_capability_name).
    let source_mapping = capabilities.get("source_mapping");
    let reverse_mapping = capabilities.get("reverse_name_mapping");

    let upstream_server_id = source_mapping
        .and_then(|m| m.get(exposed_name))
        .and_then(|v| v.as_str());

    let original_name = reverse_mapping
        .and_then(|m| m.get(exposed_name))
        .and_then(|v| v.as_str())
        // Backward compatibility: a VMCP with no name mapping
        // (reverse_name_mapping is empty) just uses
        // the exposed_name verbatim — no rename took place.
        .unwrap_or(exposed_name);

    let upstream_server_id = match upstream_server_id {
        Some(s) => s,
        None => {
            return tool_not_found_response(jsonrpc_id, exposed_name);
        }
    };

    // Rewrite the body so params.name is the upstream's original
    // capability_name. The upstream MCP server doesn't know about
    // our rename — it only understands its own native tool name.
    let mut rewritten_body = body.clone();
    if let Some(params) = rewritten_body.get_mut("params") {
        if let Some(params_obj) = params.as_object_mut() {
            params_obj.insert("name".to_string(), json!(original_name));
        }
    }

    forward_to_upstream(
        state,
        headers,
        correlation_id,
        &rewritten_body,
        upstream_server_id,
    )
    .await
}

// ---------------------------------------------------------------------------
// resources/read + prompts/get — straight passthrough (no rename today)
// ---------------------------------------------------------------------------

async fn handle_basic_passthrough(
    state: &SharedState,
    headers: &HeaderMap,
    correlation_id: &str,
    body: &Value,
    capabilities: &Value,
    jsonrpc_id: Option<Value>,
) -> Response {
    let name = body
        .get("params")
        .and_then(|p| p.get("name").or_else(|| p.get("uri")))
        .and_then(|n| n.as_str())
        .unwrap_or("");

    let source_mapping = capabilities.get("source_mapping");
    let upstream_server_id = source_mapping
        .and_then(|m| m.get(name))
        .and_then(|v| v.as_str());

    let upstream_server_id = match upstream_server_id {
        Some(s) => s,
        None => return tool_not_found_response(jsonrpc_id, name),
    };

    forward_to_upstream(state, headers, correlation_id, body, upstream_server_id).await
}

// ---------------------------------------------------------------------------
// Shared loopback delegation — the architectural invariant.
// ---------------------------------------------------------------------------

async fn forward_to_upstream(
    state: &SharedState,
    headers: &HeaderMap,
    correlation_id: &str,
    body: &Value,
    upstream_server_id: &str,
) -> Response {
    let source_url = format!(
        "http://localhost:{}/v1/proxy/mcp/{}",
        state.config.listen_addr.split(':').last().unwrap_or("8100"),
        upstream_server_id
    );

    let mut req = state
        .http_client
        .post(&source_url)
        .headers(forward_headers(headers));
    // Emit the verified chain headers
    // when the parent VMCP route set them via task-local.
    if let Some(pairs) = crate::services::call_chain::current_outbound_headers() {
        for (k, v) in pairs {
            req = req.header(k, v);
        }
    }
    let resp = req.json(body).send().await;

    match resp {
        Ok(r) => {
            let status = r.status().as_u16();
            let body_bytes = r.bytes().await.unwrap_or_default();
            let axum_status = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
            axum::response::Response::builder()
                .status(axum_status)
                .header("content-type", "application/json")
                .header(crate::constants::headers::X_CORRELATION_ID, correlation_id)
                .body(axum::body::Body::from(body_bytes))
                .unwrap_or_else(|_| {
                    error_response(StatusCode::INTERNAL_SERVER_ERROR, "response build failed")
                })
        }
        Err(e) => error_response(StatusCode::BAD_GATEWAY, &e.to_string()),
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn json_response(jsonrpc_id: Option<Value>, result: Value, correlation_id: &str) -> Response {
    let response = json!({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "result": result,
    });
    let mut resp = Json(response).into_response();
    if let Ok(value) = correlation_id.parse() {
        resp.headers_mut()
            .insert(crate::constants::headers::X_CORRELATION_ID, value);
    }
    resp
}

fn tool_not_found_response(jsonrpc_id: Option<Value>, name: &str) -> Response {
    let error = json!({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "error": {
            "code": -32601,
            "message": format!("Capability '{name}' not found on this virtual server")
        }
    });
    Json(error).into_response()
}

fn forward_headers(headers: &HeaderMap) -> reqwest::header::HeaderMap {
    let mut out = reqwest::header::HeaderMap::new();
    for (key, value) in headers.iter() {
        let key_str = key.as_str().to_lowercase();
        if matches!(
            key_str.as_str(),
            "authorization"
                | "x-gateway-authorization"
                | "x-api-key"
                | "x-tenant-id"
                | "x-correlation-id"
                | "content-type"
                // Gateway-internal, and this hop is still
                // inside the gateway.
                //
                // Dropping `x-approval-token` meant an approval-required tool
                // could never be executed through a virtual server: the
                // retry's one-time token never reached `/v1/proxy/mcp`, so the
                // call re-enqueued forever and the human's approval bought
                // nothing. Dropping `x-resource-group` silently widened
                // capability filtering from the caller's active workspace to
                // the allow-wins union of every group they belong to — the
                // quieter of the two, and the worse.
                | "x-resource-group"
                | "x-approval-token"
                // The caller's implementation carriers. This hop
                // is still inside the gateway, so the inner MCP route must
                // see what the caller declared to record the same release;
                // it strips `baggage` before anything leaves the gateway.
                | "user-agent"
                | crate::constants::agent_impl::headers::BAGGAGE
                | crate::constants::agent_impl::headers::X_BRUTOR_AGENT_NAME
                | crate::constants::agent_impl::headers::X_BRUTOR_AGENT_VERSION
                | crate::constants::agent_impl::headers::X_BRUTOR_AGENT_BUILD
                | crate::constants::agent_impl::headers::X_BRUTOR_CLIENT
        ) {
            if let Ok(name) = reqwest::header::HeaderName::from_bytes(key.as_ref()) {
                out.insert(name, value.clone());
            }
        }
    }
    out
}

fn error_response(status: StatusCode, message: &str) -> Response {
    crate::services::error_envelope::canonical_error_response(status, message)
}

async fn health(State(_state): State<SharedState>) -> Json<Value> {
    Json(json!({
        "status": "healthy",
        "service": "brutor-gateway-core-vmcp",
    }))
}

/// Pick the protocol version the VMCP advertises on `initialize`.
///
/// Real MCP clients send their preferred version in
/// `params.protocolVersion`. If it's a value we know we can speak,
/// echo it (every modern client accepts the echo). Otherwise fall
/// back to `2024-11-05` — the most widely-implemented version
/// across real-world clients today.
///
/// Extracted as a pure helper so the dispatch arm stays tidy AND
/// the version-pinning policy has a dedicated unit-test surface.
/// Stamp `resultType` + cache metadata on a synthesized
/// list result when (and only when) the request speaks the modern
/// dialect. VMCP surfaces are group-
/// filtered → always `private`, short TTL so admin edits stay visible.
fn stamp_if_modern(headers: &HeaderMap, body: &Value, result: &mut Value) {
    if crate::services::mcp_wire::is_stateless_request(headers, body) {
        crate::services::mcp_wire::stamp_modern_result(
            result,
            Some((60_000, crate::constants::mcp_protocol::cache_scope::PRIVATE)),
        );
    }
}

fn select_protocol_version(requested: Option<&str>) -> &'static str {
    use crate::constants::mcp_protocol as v;
    match requested {
        Some(v::V2024_11_05) => v::V2024_11_05,
        Some(v::V2025_03_26) => v::V2025_03_26,
        Some(v::V2025_06_18) => v::V2025_06_18,
        // A client may send `initialize` while requesting the newest
        // version it knows; the VMCP is dual-era, so echo 2026-07-28.
        Some(v::V2026_07_28) => v::V2026_07_28,
        _ => v::V2024_11_05,
    }
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn select_protocol_version_echoes_known_versions() {
        assert_eq!(select_protocol_version(Some("2024-11-05")), "2024-11-05");
        assert_eq!(select_protocol_version(Some("2025-03-26")), "2025-03-26");
        assert_eq!(select_protocol_version(Some("2025-06-18")), "2025-06-18");
    }

    #[test]
    fn select_protocol_version_falls_back_for_unknown() {
        assert_eq!(select_protocol_version(Some("1999-01-01")), "2024-11-05");
        assert_eq!(select_protocol_version(Some("")), "2024-11-05");
    }

    #[test]
    fn select_protocol_version_falls_back_when_missing() {
        assert_eq!(select_protocol_version(None), "2024-11-05");
    }

    /// Dual-era: 2026-07-28 is echoed when requested (the flag
    /// that gated this is gone), and nothing else is widened.
    #[test]
    fn select_protocol_version_echoes_2026_07_28() {
        assert_eq!(select_protocol_version(Some("2026-07-28")), "2026-07-28");
        assert_eq!(select_protocol_version(Some("1999-01-01")), "2024-11-05");
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The credential × surface matrix.
//!
//! Every route of the assembled gateway (`common::full_app`: the application
//! the binary serves, assembled by the builder for the full composition) is asked with every kind of credential, and the status is
//! compared with `tests/fixtures/credential_surface_matrix/`. The fixture
//! was recorded on the code as it stood before the gateway-JWT branch of
//! `auth::authenticate_request` became a token-type registry
//! (`services::credential_verifier`), so it is the proof that an assembly
//! with the portal verifier registered answers as the gateway did before.
//! The cells that changed with the registry are intended tightenings (354
//! cells, six kinds of credential: [`REFUSED_SINCE_THE_REGISTRY`]). One
//! loosening was made on purpose afterwards and is the only one:
//! [`API_KEY_ACCEPTED_SINCE_P3_36`], 33 cells.
//! Both are in the recording itself: the fixture is what the recorder
//! writes, byte for byte, and nothing is applied to it in code.
//!
//! ## How the surfaces are enumerated
//!
//! [`ROUTES`] is every `.route(...)` of every `src/routes/*.rs` router with
//! the prefix `src/app.rs` mounts it at. `the_matrix_lists_every_route`
//! parses the route modules and fails when a route is not in the table, so a
//! new surface cannot be added without a row.
//!
//! ## What a cell is
//!
//! The status of one request: the route's method and path (path parameters
//! filled with values that name nothing), a body the route's extractors
//! accept, `X-Tenant-ID` of the test tenant, and the credential. No model,
//! server, skill or agent is seeded, so an accepted credential ends in the
//! route's own "not found" or validation answer, never in an upstream call.
//! That is enough: the matrix asks who gets past authentication, and a
//! refused credential answers 401 (or 403, 500, 503) where an accepted one
//! answers something else.
//!
//! Re-record (only on purpose, and say why in the change):
//! `CREDENTIAL_MATRIX_RECORD=1 cargo nextest run --features db-tests -E 'test(credential_surface_matrix)'`

use std::collections::BTreeMap;
use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::Request;
use base64::Engine;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use super::workload_identity as wl;
use crate::common;

pub(crate) const TENANT: &str = "tenant-matrix";
pub(crate) const OTHER_TENANT: &str = "tenant-matrix-other";
pub(crate) const GROUP: &str = "rg-matrix";
/// The end user the portal tokens name.
pub(crate) const USER: i32 = 1;
/// An end user whose sessions were ended after the token was issued.
pub(crate) const CUT_OFF_USER: i32 = 2;
/// An MCP server that exists, for the OAuth routes (they take the tenant from
/// it). Nothing is proxied to it: no route of the matrix names it as `server_id`.
pub(crate) const OAUTH_SERVER: &str = "oauth-srv";
/// The recording: one file per shard of [`ROUTES`].
pub(crate) const FIXTURE_DIR: &str = "tests/fixtures/credential_surface_matrix";
/// The matrix is asked in this many tests, each with its own database, so
/// that no one test runs for minutes.
pub(crate) const SHARDS: usize = 8;

// ---------------------------------------------------------------------------
// The surfaces
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Send {
    /// No body.
    Nothing,
    /// A JSON body.
    Json(&'static str),
    /// A multipart form with one small file part.
    Multipart,
}

pub(crate) struct Route {
    /// The route module, for the completeness check.
    module: &'static str,
    /// Where `src/app.rs` mounts the module's router.
    mount: &'static str,
    /// The path as the module's router declares it.
    path: &'static str,
    pub(crate) method: &'static str,
    send: Send,
}

pub(crate) const fn r(
    module: &'static str,
    mount: &'static str,
    path: &'static str,
    method: &'static str,
    send: Send,
) -> Route {
    Route {
        module,
        mount,
        path,
        method,
        send,
    }
}

pub(crate) const EMPTY: Send = Send::Json("{}");
pub(crate) const A2A_MESSAGE: Send =
    Send::Json(r#"{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}"#);
pub(crate) const A2A_INVOKE: Send =
    Send::Json(r#"{"target_card_id":"c","capability":"x","message":{}}"#);
pub(crate) const NONE: Send = Send::Nothing;

/// Every route of the gateway. `any(...)` routes are asked with GET and POST.
pub(crate) const ROUTES: &[Route] = &[
    // --- A2A ---------------------------------------------------------------
    r("a2a.rs", "/v1/proxy/a2a", "/outbound", "POST", A2A_INVOKE),
    r("a2a.rs", "/v1/proxy/a2a", "/inbound", "POST", A2A_INVOKE),
    r(
        "a2a.rs",
        "",
        "/.well-known/agent-card/{tenant_id}/{card_name}",
        "GET",
        NONE,
    ),
    r(
        "a2a.rs",
        "",
        "/.well-known/a2a-jwks/{tenant_id}",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/message:send",
        "POST",
        A2A_MESSAGE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/message:stream",
        "POST",
        A2A_MESSAGE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_ref}",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_ref}",
        "POST",
        EMPTY,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/extendedAgentCard",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs",
        "POST",
        Send::Json(r#"{"url":"https://example.invalid/hook"}"#),
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        "GET",
        NONE,
    ),
    r(
        "a2a_v1.rs",
        "/a2a",
        "/{tenant_id}/{card_name}/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        "DELETE",
        NONE,
    ),
    // --- LLM ---------------------------------------------------------------
    r(
        "anthropic.rs",
        "/v1",
        "/messages",
        "POST",
        Send::Json(r#"{"model":"m","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}"#),
    ),
    r(
        "anthropic.rs",
        "/v1",
        "/messages/count_tokens",
        "POST",
        Send::Json(r#"{"model":"m","messages":[{"role":"user","content":"hi"}]}"#),
    ),
    r(
        "gemini.rs",
        "",
        "/v1beta/models/{model_action}",
        "POST",
        Send::Json(r#"{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}"#),
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/chat/completions",
        "POST",
        Send::Json(r#"{"model":"m","messages":[{"role":"user","content":"hi"}]}"#),
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/responses",
        "POST",
        Send::Json(r#"{"model":"m","input":"hi"}"#),
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/completions",
        "POST",
        Send::Json(r#"{"model":"m","prompt":"hi"}"#),
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/embeddings",
        "POST",
        Send::Json(r#"{"model":"m","input":"hi"}"#),
    ),
    r("llm.rs", "/v1/proxy/llm", "/models", "GET", NONE),
    r("llm.rs", "/v1/proxy/llm", "/models/{model_id}", "GET", NONE),
    r("llm.rs", "/v1/proxy/llm", "/health", "GET", NONE),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/admin/{model_id}/reset-resilience",
        "POST",
        EMPTY,
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/admin/{model_id}/invalidate-cache",
        "POST",
        EMPTY,
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/admin/{model_id}/probe-catalog",
        "POST",
        EMPTY,
    ),
    r(
        "llm.rs",
        "/v1/proxy/llm",
        "/admin/{model_id}/record-probe-result",
        "POST",
        Send::Json(r#"{"success":true,"status_code":200,"latency_ms":1.0,"probe_type":"catalog"}"#),
    ),
    r(
        "images.rs",
        "/v1/proxy/llm/images/generations",
        "/",
        "POST",
        Send::Json(r#"{"model":"m","prompt":"a cat"}"#),
    ),
    r(
        "audio_speech.rs",
        "/v1/proxy/llm/audio/speech",
        "/",
        "POST",
        Send::Json(r#"{"model":"m","input":"hi","voice":"alloy"}"#),
    ),
    r(
        "audio_transcribe.rs",
        "/v1/proxy/llm/audio/transcriptions",
        "/",
        "POST",
        Send::Multipart,
    ),
    r(
        "video.rs",
        "/v1/proxy/llm/video/generations",
        "/",
        "POST",
        Send::Json(r#"{"model":"m","prompt":"a cat"}"#),
    ),
    r(
        "video.rs",
        "/v1/proxy/llm/video/jobs",
        "/{job_id}",
        "GET",
        NONE,
    ),
    r(
        "video.rs",
        "/v1/proxy/llm/video/jobs",
        "/{job_id}",
        "DELETE",
        NONE,
    ),
    r(
        "batch.rs",
        "/v1/proxy/batch",
        "/jobs",
        "POST",
        Send::Json(
            r#"{"model":"m","requests":[{"custom_id":"a","body":{"messages":[{"role":"user","content":"hi"}]}}]}"#,
        ),
    ),
    r("batch.rs", "/v1/proxy/batch", "/jobs", "GET", NONE),
    r(
        "batch.rs",
        "/v1/proxy/batch",
        "/jobs/{batch_job_id}",
        "GET",
        NONE,
    ),
    r(
        "batch.rs",
        "/v1/proxy/batch",
        "/jobs/{batch_job_id}/cancel",
        "POST",
        EMPTY,
    ),
    r(
        "batch.rs",
        "/v1/proxy/batch",
        "/jobs/{batch_job_id}/results",
        "GET",
        NONE,
    ),
    // --- MCP ---------------------------------------------------------------
    r("mcp.rs", "/v1/proxy/mcp", "/health", "GET", NONE),
    r("mcp.rs", "/v1/proxy/mcp", "/{server_id}", "GET", NONE),
    r(
        "mcp.rs",
        "/v1/proxy/mcp",
        "/{server_id}",
        "POST",
        Send::Json(r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#),
    ),
    r("mcp.rs", "/v1/proxy/mcp", "/{server_id}/", "GET", NONE),
    r(
        "mcp.rs",
        "/v1/proxy/mcp",
        "/{server_id}/",
        "POST",
        Send::Json(r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#),
    ),
    r(
        "mcp.rs",
        "/v1/proxy/mcp",
        "/{server_id}/{*path}",
        "GET",
        NONE,
    ),
    r(
        "mcp.rs",
        "/v1/proxy/mcp",
        "/{server_id}/{*path}",
        "POST",
        Send::Json(r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#),
    ),
    r("vmcp.rs", "/v1/proxy/vmcp", "/health", "GET", NONE),
    r(
        "vmcp.rs",
        "/v1/proxy/vmcp",
        "/{virtual_server_id}",
        "POST",
        Send::Json(r#"{"jsonrpc":"2.0","id":1,"method":"tools/list"}"#),
    ),
    // --- OAuth -------------------------------------------------------------
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/.well-known/oauth-protected-resource",
        "GET",
        NONE,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration",
        "GET",
        NONE,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/token",
        "POST",
        EMPTY,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/revoke",
        "POST",
        EMPTY,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/tokens",
        "GET",
        NONE,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/register",
        "POST",
        EMPTY,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/authorize",
        "GET",
        NONE,
    ),
    r(
        "oauth.rs",
        "/v1/proxy/oauth",
        "/{mcp_server_id}/{idp_server_id}/callback",
        "GET",
        NONE,
    ),
    // --- Skills, files -----------------------------------------------------
    r(
        "skills.rs",
        "/v1/proxy/skills",
        "/execute",
        "POST",
        Send::Json(r#"{"skill_id":"s","inputs":{}}"#),
    ),
    r(
        "skills.rs",
        "/v1/proxy/skills",
        "/executions/{execution_id}",
        "GET",
        NONE,
    ),
    r(
        "skills.rs",
        "/v1/proxy/skills",
        "/executions/{execution_id}/cancel",
        "POST",
        EMPTY,
    ),
    r("skills.rs", "/v1/proxy/skills", "/catalog", "GET", NONE),
    r("files.rs", "/v1/proxy/files", "/", "POST", Send::Multipart),
    r("files.rs", "/v1/proxy/files", "/{file_id}", "GET", NONE),
    r("files.rs", "/v1/proxy/files", "/{file_id}", "DELETE", NONE),
    r("files.rs", "/v1/proxy/files", "/{file_id}/url", "GET", NONE),
    r(
        "files.rs",
        "/v1/proxy/files",
        "/{file_id}/content",
        "GET",
        NONE,
    ),
    // --- Audit, evidence, runs, ingest, assurance ---------------------------
    r("audit.rs", "/v1/audit", "/chains", "GET", NONE),
    r(
        "audit.rs",
        "/v1/audit",
        "/chains/{chain_id}/verify",
        "POST",
        EMPTY,
    ),
    r(
        "audit.rs",
        "/v1/audit",
        "/export-manifest/sign",
        "POST",
        EMPTY,
    ),
    r("evidence.rs", "/v1/evidence", "/seal", "POST", EMPTY),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/logs/{tenant_id}/tree-head",
        "POST",
        EMPTY,
    ),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/verify/record",
        "POST",
        Send::Json(r#"{"record":{}}"#),
    ),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/verify/statement",
        "POST",
        Send::Json(r#"{"statement_b64":"AA=="}"#),
    ),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/verify/tree-head",
        "POST",
        Send::Json(r#"{"statement_b64":"AA=="}"#),
    ),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/verify/receipt",
        "POST",
        Send::Json(r#"{"receipt_b64":"AA==","statement_b64":"AA=="}"#),
    ),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/verify/bundle",
        "POST",
        Send::Json(r#"{"bundle":{}}"#),
    ),
    r("evidence.rs", "/v1/evidence", "/bundles", "POST", EMPTY),
    r("evidence.rs", "/v1/evidence", "/backfill", "POST", EMPTY),
    r(
        "evidence.rs",
        "/v1/evidence",
        "/keys/{tenant_id}/rotate",
        "POST",
        EMPTY,
    ),
    r(
        "evidence.rs",
        "/.well-known",
        "/brutor-evidence-keys.json",
        "GET",
        NONE,
    ),
    r(
        "evidence_aac.rs",
        "/v1/evidence",
        "/records/{record_id}/aac",
        "GET",
        NONE,
    ),
    r("runs.rs", "/v1/runs", "/{root_task_id}/end", "POST", EMPTY),
    r("runs.rs", "/v1/runs", "/{root_task_id}", "GET", NONE),
    r(
        "run_feedback.rs",
        "/v1/runs",
        "/{run_id}/feedback",
        "POST",
        Send::Json(r#"{"score":1}"#),
    ),
    r(
        "otel_ingest.rs",
        "/v1/ingest/otel",
        "/v1/traces",
        "POST",
        Send::Json(r#"{"resourceSpans":[]}"#),
    ),
    r(
        "assurance_checks.rs",
        "/v1/assurance",
        "/checks/compile",
        "POST",
        Send::Json(r#"{"expression":"run.turn_count > 0","output":"boolean"}"#),
    ),
    r(
        "assurance_checks.rs",
        "/v1/assurance",
        "/checks/backtest",
        "POST",
        Send::Json(r#"{"check_id":"c","version":1}"#),
    ),
    // --- Approvals ---------------------------------------------------------
    r("approvals.rs", "/v1/approvals", "/", "GET", NONE),
    r(
        "approvals.rs",
        "/v1/approvals",
        "/pending-count",
        "GET",
        NONE,
    ),
    r("approvals.rs", "/v1/approvals", "/{id}", "GET", NONE),
    r("approvals.rs", "/v1/approvals", "/{id}/poll", "GET", NONE),
    r(
        "approvals.rs",
        "/v1/approvals",
        "/{id}/approve",
        "POST",
        EMPTY,
    ),
    r(
        "approvals.rs",
        "/v1/approvals",
        "/{id}/reject",
        "POST",
        EMPTY,
    ),
    r(
        "approvals.rs",
        "/v1/internal/approvals",
        "/decide",
        "POST",
        EMPTY,
    ),
    // --- Portal ------------------------------------------------------------
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/register",
        "POST",
        EMPTY,
    ),
    r("portal_auth.rs", "/v1/portal/auth", "/login", "POST", EMPTY),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/login/2fa",
        "POST",
        EMPTY,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/login/2fa/enroll/setup",
        "POST",
        EMPTY,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/login/2fa/enroll/verify",
        "POST",
        EMPTY,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/validate",
        "POST",
        EMPTY,
    ),
    r("portal_auth.rs", "/v1/portal/auth", "/profile", "GET", NONE),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/profile",
        "PATCH",
        EMPTY,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/profile/photo",
        "POST",
        Send::Multipart,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/change-password",
        "POST",
        Send::Json(r#"{"current_password":"a","new_password":"b"}"#),
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/logout",
        "POST",
        EMPTY,
    ),
    r("portal_auth.rs", "/v1/portal/auth", "/groups", "GET", NONE),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/primary-group",
        "PATCH",
        Send::Json(r#"{"group_id":"none"}"#),
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/2fa/status",
        "GET",
        NONE,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/2fa/setup",
        "POST",
        EMPTY,
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/2fa/verify",
        "POST",
        Send::Json(r#"{"code":"000000"}"#),
    ),
    r(
        "portal_auth.rs",
        "/v1/portal/auth",
        "/2fa/disable",
        "POST",
        Send::Json(r#"{"password":"a","code":"000000"}"#),
    ),
    r("portal_alerts.rs", "/v1/portal/alerts", "/", "GET", NONE),
    r(
        "portal_alerts.rs",
        "/v1/portal/alerts",
        "/{alert_id}/acknowledge",
        "POST",
        EMPTY,
    ),
    r(
        "portal_alerts.rs",
        "/v1/portal/alerts",
        "/group/{group_id}",
        "GET",
        NONE,
    ),
    r(
        "portal_evidence.rs",
        "/v1/portal/evidence",
        "/notice-config",
        "GET",
        NONE,
    ),
    r(
        "portal_evidence.rs",
        "/v1/portal/evidence",
        "/notice",
        "POST",
        Send::Json(
            r#"{"conversation_handle":"c","locale":"en","method":"banner","text_sha256":"00"}"#,
        ),
    ),
    r(
        "portal_kb.rs",
        "/v1/portal/knowledgebases",
        "/",
        "GET",
        NONE,
    ),
    r(
        "portal_kb.rs",
        "/v1/portal/knowledgebases",
        "/{collection_id}",
        "GET",
        NONE,
    ),
    r(
        "portal_kb_identities.rs",
        "/v1/portal/kb-identities",
        "/connectors",
        "GET",
        NONE,
    ),
    r(
        "portal_kb_identities.rs",
        "/v1/portal/kb-identities",
        "/",
        "GET",
        NONE,
    ),
    r(
        "portal_kb_identities.rs",
        "/v1/portal/kb-identities",
        "/",
        "POST",
        EMPTY,
    ),
    r(
        "portal_kb_identities.rs",
        "/v1/portal/kb-identities",
        "/{mapping_id}",
        "DELETE",
        NONE,
    ),
    r(
        "portal_mcp_apps.rs",
        "/v1/portal/mcp-apps",
        "/{server_id}/resource",
        "GET",
        NONE,
    ),
    r("portal_mcps.rs", "/v1/portal/servers", "/", "GET", NONE),
    r(
        "portal_mcps.rs",
        "/v1/portal/servers",
        "/{config_id}",
        "GET",
        NONE,
    ),
    r(
        "portal_mcps.rs",
        "/v1/portal/servers",
        "/by-group/{group_id}",
        "GET",
        NONE,
    ),
    r("portal_models.rs", "/v1/portal/models", "/", "GET", NONE),
    r(
        "portal_models.rs",
        "/v1/portal/models",
        "/{model_id}",
        "GET",
        NONE,
    ),
    r(
        "portal_notifications.rs",
        "/v1/portal/notifications",
        "/",
        "GET",
        NONE,
    ),
    r(
        "portal_notifications.rs",
        "/v1/portal/notifications",
        "/unread-count",
        "GET",
        NONE,
    ),
    r(
        "portal_notifications.rs",
        "/v1/portal/notifications",
        "/mark-all-read",
        "POST",
        EMPTY,
    ),
    r(
        "portal_notifications.rs",
        "/v1/portal/notifications",
        "/{id}/read",
        "POST",
        EMPTY,
    ),
    r(
        "portal_settings.rs",
        "/v1/portal/settings",
        "/",
        "GET",
        NONE,
    ),
    r("portal_skills.rs", "/v1/portal/skills", "/", "GET", NONE),
    r(
        "portal_skills.rs",
        "/v1/portal/skills",
        "/{skill_id}/export",
        "GET",
        NONE,
    ),
    // --- Public, assets, gateway -------------------------------------------
    r(
        "public_tenant_theme.rs",
        "/v1/public/tenants",
        "/{tenant_id}/theme",
        "GET",
        NONE,
    ),
    r(
        "icons.rs",
        "/v1/assets",
        "/mcp-servers/{server_id}/icon",
        "GET",
        NONE,
    ),
    r(
        "icons.rs",
        "/v1/assets",
        "/llm-providers/{provider_id}/icon",
        "GET",
        NONE,
    ),
    r(
        "icons.rs",
        "/v1/assets",
        "/users/end-user/{user_id}/photo",
        "GET",
        NONE,
    ),
    r(
        "connector_icons.rs",
        "/v1/assets/connector-icons",
        "/{filename}",
        "GET",
        NONE,
    ),
    r("gateway.rs", "", "/v1/version", "GET", NONE),
    r("gateway.rs", "", "/health", "GET", NONE),
    r("gateway.rs", "", "/healthz", "GET", NONE),
    // Last, so that no other row moves to another shard. The link is the
    // credential: every kind of credential gets the 404 of a link that is
    // not valid (and the assembly here has no filesystem store).
    r("media.rs", "/v1/media", "/{*key}", "GET", NONE),
];

/// Route modules whose routes are not part of `app::router`: `/metrics` is
/// served on its own listener (`app::metrics_router`), behind no credential
/// of this matrix.
pub(crate) const NOT_IN_THE_APPLICATION: &[&str] = &["metrics.rs"];

impl Route {
    pub(crate) fn name(&self) -> String {
        format!("{} {}{}", self.method, self.mount, self.path)
    }

    /// The request path: parameters filled with values that name nothing.
    pub(crate) fn uri(&self) -> String {
        // A router's own "/" is the mount itself.
        let template = match self.path {
            "/" => self.mount.to_string(),
            path => format!("{}{path}", self.mount),
        };
        let mut uri = template
            .split('/')
            .map(|segment| match segment {
                "{tenant_id}" => TENANT,
                "{model_action}" => "m:generateContent",
                "{filename}" => "none.svg",
                "{*path}" => "mcp",
                // The OAuth routes resolve the tenant from the server.
                "{mcp_server_id}" => OAUTH_SERVER,
                // `POST /tasks/{id}` needs a verb.
                "{task_ref}" if self.method == "POST" => "1:cancel",
                s if s.starts_with('{') => "1",
                s => s,
            })
            .collect::<Vec<_>>()
            .join("/");
        if self.module == "portal_mcp_apps.rs" {
            uri.push_str("?uri=ui%3A%2F%2Fnone");
        }
        uri
    }
}

// ---------------------------------------------------------------------------
// The credentials
// ---------------------------------------------------------------------------

/// How a credential is put on the request.
pub(crate) enum Carry {
    Nothing,
    XApiKey(String),
    Authorization(String),
    XGatewayAuthorization(String),
}

pub(crate) struct World {
    pub(crate) api_key: String,
    pub(crate) revoked_api_key: String,
    /// A key bound to an agent identity that is disabled, and one bound to
    /// an identity past its `expires_at`. Neither
    /// agent is default-deny.
    pub(crate) disabled_agent_api_key: String,
    pub(crate) expired_agent_api_key: String,
    pub(crate) revoked_agent_api_key: String,
    pub(crate) tenant_admin_jti: String,
    pub(crate) system_admin_jti: String,
    pub(crate) revoked_admin_jti: String,
    pub(crate) tenant_admin_id: String,
    pub(crate) system_admin_id: String,
    pub(crate) workload: WorkloadWorld,
}

/// The identity provider the workload-identity credentials are minted
/// against: its key set is served by this mock, which
/// lives as long as the world.
pub(crate) struct WorkloadWorld {
    _jwks: wiremock::MockServer,
}

/// The key id of the matrix's identity providers.
const WL_KID: &str = "kid-matrix";
/// The identity provider of [`TENANT`] and the one of [`OTHER_TENANT`].
const WL_ISSUER: &str = "https://idp.matrix.example/";
const WL_OTHER_ISSUER: &str = "https://idp.matrix-other.example/";
/// An enabled agent of [`TENANT`], a member of [`GROUP`].
pub(crate) const WL_AGENT: &str = "agent-matrix";
const WL_SUB: &str = "spiffe://matrix/agent";
/// A disabled agent of [`TENANT`], a member of [`GROUP`].
const WL_DISABLED_SUB: &str = "spiffe://matrix/disabled";
/// An enabled agent of [`TENANT`], a member of [`GROUP`], whose identity
/// expired an hour ago (`agent_identities.expires_at`).
const WL_EXPIRED_IDENTITY_SUB: &str = "spiffe://matrix/expired-identity";
/// An enabled agent of [`TENANT`] that is a member of no resource group.
const WL_NO_GROUP_SUB: &str = "spiffe://matrix/no-group";
/// An enabled agent of [`OTHER_TENANT`], a member of a group of that tenant.
const WL_OTHER_SUB: &str = "spiffe://matrix-other/agent";

impl WorkloadWorld {
    /// A token of `sub` signed by the identity provider at `issuer`.
    pub(crate) fn token(issuer: &str, sub: &str, expires_in: i64) -> String {
        wl::mint_token(issuer, sub, WL_KID, (now() + expires_in) as usize)
    }

    /// The valid token of [`WL_AGENT`].
    pub(crate) fn valid() -> String {
        Self::token(WL_ISSUER, WL_SUB, 3600)
    }

    /// The valid token with another payload under the same signature.
    fn tampered() -> String {
        let token = Self::valid();
        let mut parts = token.split('.');
        let (header, _payload, signature) = (
            parts.next().expect("header"),
            parts.next().expect("payload"),
            parts.next().expect("signature"),
        );
        let payload = b64(json!({
            "iss": WL_ISSUER, "sub": WL_SUB, "aud": wl::WL_AUDIENCE, "exp": now() + 86_400,
        })
        .to_string()
        .as_bytes());
        format!("{header}.{payload}.{signature}")
    }
}

async fn seed_workload_world(pool: &PgPool) -> WorkloadWorld {
    let jwks = wl::mock_jwks(WL_KID).await;
    let jwks_uri = format!("{}/jwks", jwks.uri());
    wl::seed_idp(pool, "idp-matrix", TENANT, WL_ISSUER, &jwks_uri).await;
    wl::seed_idp(
        pool,
        "idp-matrix-other",
        OTHER_TENANT,
        WL_OTHER_ISSUER,
        &jwks_uri,
    )
    .await;
    common::seed_resource_group(pool, "rg-matrix-other", OTHER_TENANT).await;
    for (agent, tenant, idp, sub, group) in [
        (WL_AGENT, TENANT, "idp-matrix", WL_SUB, Some(GROUP)),
        (
            "agent-matrix-disabled",
            TENANT,
            "idp-matrix",
            WL_DISABLED_SUB,
            Some(GROUP),
        ),
        (
            "agent-matrix-expired-identity",
            TENANT,
            "idp-matrix",
            WL_EXPIRED_IDENTITY_SUB,
            Some(GROUP),
        ),
        (
            "agent-matrix-no-group",
            TENANT,
            "idp-matrix",
            WL_NO_GROUP_SUB,
            None,
        ),
        (
            "agent-matrix-other",
            OTHER_TENANT,
            "idp-matrix-other",
            WL_OTHER_SUB,
            Some("rg-matrix-other"),
        ),
    ] {
        wl::seed_agent_with_idp(pool, agent, tenant, idp, sub).await;
        if let Some(group) = group {
            sqlx::query(
                "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id) \
                 VALUES ($1, $2, $3)",
            )
            .bind(tenant)
            .bind(agent)
            .bind(group)
            .execute(pool)
            .await
            .expect("agent membership");
        }
    }
    sqlx::query("UPDATE agent_identities SET enabled = false WHERE id = 'agent-matrix-disabled'")
        .execute(pool)
        .await
        .expect("disable agent");
    sqlx::query(
        "UPDATE agent_identities SET expires_at = now() - interval '1 hour' \
         WHERE id = 'agent-matrix-expired-identity'",
    )
    .execute(pool)
    .await
    .expect("expire agent identity");
    WorkloadWorld { _jwks: jwks }
}

pub(crate) fn now() -> i64 {
    common::db_now().timestamp()
}

pub(crate) fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

pub(crate) fn sign_with(claims: &Value, secret: &str) -> String {
    use jsonwebtoken::{EncodingKey, Header, encode};
    encode(
        &Header::default(),
        claims,
        &EncodingKey::from_secret(secret.as_bytes()),
    )
    .expect("encode")
}

pub(crate) fn sign(claims: &Value) -> String {
    sign_with(claims, common::TEST_JWT_SECRET)
}

/// An unsigned token (`alg: none`) with the claims of a valid portal session.
pub(crate) fn alg_none(claims: &Value) -> String {
    format!(
        "{}.{}.",
        b64(br#"{"alg":"none","typ":"JWT"}"#),
        b64(claims.to_string().as_bytes())
    )
}

pub(crate) fn portal_claims(user: i32, tenant: &str) -> Value {
    json!({
        "typ": "portal", "jti": uuid::Uuid::new_v4().to_string(),
        "user_id": user.to_string(), "tenant_id": tenant, "username": format!("user-{user}"),
        "user_type": "end_user", "exp": now() + 3600, "iat": now(),
    })
}

pub(crate) fn tenant_admin_claims(world: &World, jti: &str) -> Value {
    json!({
        "typ": "admin", "jti": jti, "user_id": world.tenant_admin_id, "username": "admin",
        "admin_type": "tenant", "tenant_id": TENANT, "permissions": [],
        "exp": now() + 3600, "iat": now(),
    })
}

pub(crate) fn system_admin_claims(world: &World) -> Value {
    json!({
        "typ": "admin", "jti": world.system_admin_jti, "user_id": world.system_admin_id,
        "username": "sysadmin", "admin_type": "system", "permissions": [],
        "exp": now() + 3600, "iat": now(),
    })
}

pub(crate) fn gateway_claims(tenant: &str) -> Value {
    json!({
        "typ": "gateway", "user_id": "svc-1", "username": "service", "tenant_id": tenant,
        "exp": now() + 3600, "iat": now(),
    })
}

pub(crate) fn with(mut claims: Value, key: &str, value: Value) -> Value {
    claims[key] = value;
    claims
}

pub(crate) fn without(mut claims: Value, key: &str) -> Value {
    claims.as_object_mut().expect("object").remove(key);
    claims
}

/// The credential kinds, in fixture order. Tokens are minted for each route
/// anew (a route may end the session it is given: `/logout`).
pub(crate) const CREDENTIALS: &[&str] = &[
    "none",
    "api_key",
    "api_key_bearer",
    "api_key_revoked",
    "api_key_unknown",
    "portal",
    "portal_x_gateway",
    "portal_expired",
    "portal_revoked",
    "portal_before_cutoff",
    "portal_wrong_key",
    "portal_other_tenant",
    "portal_no_jti",
    "admin_tenant",
    "admin_system",
    "admin_expired",
    "admin_revoked",
    "admin_no_session",
    "admin_wrong_key",
    "admin_other_tenant",
    "admin_no_admin_type",
    "admin_unknown_admin_type",
    "gateway",
    "gateway_expired",
    "gateway_wrong_key",
    "gateway_other_tenant",
    "pre_session_2fa_challenge",
    "pre_session_2fa_enrollment",
    "pre_session_admin_2fa_challenge",
    "unknown_typ",
    "no_typ",
    "no_typ_end_user",
    "no_typ_admin_tenant",
    "skill_runner_token",
    "registry_service_token",
    "alg_none",
    "garbage_bearer",
    // Workload identity: an agent's own token from
    // an identity provider of the tenant.
    "workload",
    "workload_no_group",
    "workload_expired",
    "workload_tampered",
    "workload_other_tenant",
    "workload_disabled_agent",
    // A valid token of an agent whose identity is past its `expires_at`.
    "workload_expired_identity",
    // An API key that stands, bound to an agent identity that does not: the
    // agent is disabled, or its identity is past `expires_at`,
    // or it is revoked. Such a key is no credential:
    // it answers on every route what a revoked key answers there.
    "api_key_disabled_agent",
    "api_key_expired_agent",
    "api_key_revoked_agent",
];

pub(crate) fn credential(kind: &str, world: &World) -> Carry {
    use Carry::*;
    let wrong = "another-secret-that-is-long-enough-to-sign-with!!";
    match kind {
        "none" => Nothing,
        "api_key" => XApiKey(world.api_key.clone()),
        "api_key_bearer" => Authorization(world.api_key.clone()),
        "api_key_revoked" => XApiKey(world.revoked_api_key.clone()),
        "api_key_unknown" => XApiKey("sk_brutor_api_test_nobody_has_this_key".into()),
        "portal" => Authorization(sign(&portal_claims(USER, TENANT))),
        "portal_x_gateway" => XGatewayAuthorization(sign(&portal_claims(USER, TENANT))),
        "portal_expired" => Authorization(sign(&with(
            portal_claims(USER, TENANT),
            "exp",
            json!(now() - 3600),
        ))),
        "portal_revoked" => Authorization(sign(&with(
            portal_claims(USER, TENANT),
            "jti",
            json!("revoked-portal-jti"),
        ))),
        "portal_before_cutoff" => Authorization(sign(&with(
            portal_claims(CUT_OFF_USER, TENANT),
            "iat",
            json!(now() - 7200),
        ))),
        "portal_wrong_key" => Authorization(sign_with(&portal_claims(USER, TENANT), wrong)),
        "portal_other_tenant" => Authorization(sign(&portal_claims(USER, OTHER_TENANT))),
        "portal_no_jti" => Authorization(sign(&without(portal_claims(USER, TENANT), "jti"))),
        "admin_tenant" => Authorization(sign(&tenant_admin_claims(world, &world.tenant_admin_jti))),
        "admin_system" => Authorization(sign(&system_admin_claims(world))),
        "admin_expired" => Authorization(sign(&with(
            tenant_admin_claims(world, &world.tenant_admin_jti),
            "exp",
            json!(now() - 3600),
        ))),
        "admin_revoked" => {
            Authorization(sign(&tenant_admin_claims(world, &world.revoked_admin_jti)))
        }
        "admin_no_session" => Authorization(sign(&tenant_admin_claims(world, "no-such-session"))),
        "admin_wrong_key" => Authorization(sign_with(
            &tenant_admin_claims(world, &world.tenant_admin_jti),
            wrong,
        )),
        "admin_other_tenant" => Authorization(sign(&with(
            tenant_admin_claims(world, &world.tenant_admin_jti),
            "tenant_id",
            json!(OTHER_TENANT),
        ))),
        "admin_no_admin_type" => Authorization(sign(&without(
            tenant_admin_claims(world, &world.tenant_admin_jti),
            "admin_type",
        ))),
        "admin_unknown_admin_type" => Authorization(sign(&with(
            tenant_admin_claims(world, &world.tenant_admin_jti),
            "admin_type",
            json!("super"),
        ))),
        "gateway" => Authorization(sign(&gateway_claims(TENANT))),
        "gateway_expired" => Authorization(sign(&with(
            gateway_claims(TENANT),
            "exp",
            json!(now() - 3600),
        ))),
        "gateway_wrong_key" => Authorization(sign_with(&gateway_claims(TENANT), wrong)),
        "gateway_other_tenant" => Authorization(sign(&gateway_claims(OTHER_TENANT))),
        // What `/v1/portal/auth/login` hands out before the second factor.
        "pre_session_2fa_challenge" => Authorization(sign(&json!({
            "type": "portal_2fa_challenge", "user_id": USER, "tenant_id": TENANT,
            "iat": now(), "exp": now() + 300,
        }))),
        "pre_session_2fa_enrollment" => Authorization(sign(&json!({
            "type": "portal_2fa_enrollment", "user_id": USER, "tenant_id": TENANT,
            "iat": now(), "exp": now() + 600,
        }))),
        // The control plane's, for an administrator.
        "pre_session_admin_2fa_challenge" => Authorization(sign(&json!({
            "type": "2fa_challenge", "admin_id": world.tenant_admin_id, "tenant_id": TENANT,
            "admin_type": "tenant", "iat": now(), "exp": now() + 300,
        }))),
        "unknown_typ" => Authorization(sign(&with(
            portal_claims(USER, TENANT),
            "typ",
            json!("registry"),
        ))),
        // Session-shaped claims with no `typ` and nothing that marks them as
        // an end user's or an administrator's.
        "no_typ" => Authorization(sign(&without(gateway_claims(TENANT), "typ"))),
        // A portal session as it was minted before `typ` existed.
        "no_typ_end_user" => Authorization(sign(&without(portal_claims(USER, TENANT), "typ"))),
        // A tenant administrator's session as the control plane's password
        // sign-in once minted it: with no `typ`.
        "no_typ_admin_tenant" => Authorization(sign(&without(
            tenant_admin_claims(world, &world.tenant_admin_jti),
            "typ",
        ))),
        // The claims `services::skill_runner_client` signs for the skill
        // runner. That token is signed with `BRUTOR_SKILL_RUNNER_JWT_SECRET`; this
        // one, with the gateway's secret, is what it is where an operator
        // set the two alike.
        "skill_runner_token" => Authorization(sign(&json!({
            "exec": "exec-1", "tnt": TENANT, "skl": "skill-1", "exp": now() + 300,
        }))),
        // What the control plane signs for the MCP registry with the
        // gateway's `JWT_SECRET` (`mcp_registry_instance._service_token`).
        // The registry's own token for the control plane has the same
        // shape (`sub`, `iss`, `aud`, `scope`; no `typ`).
        "registry_service_token" => Authorization(sign(&json!({
            "sub": "admin:someone", "iss": "brutor-control-plane",
            "iat": now(), "exp": now() + 60,
        }))),
        "alg_none" => Authorization(alg_none(&portal_claims(USER, TENANT))),
        "garbage_bearer" => Authorization("eyJhbGciOiJIUzI1NiJ9.garbage.garbage".into()),
        // An enabled agent of the tenant, a member of the fixture's group.
        "workload" => Authorization(WorkloadWorld::valid()),
        // An enabled agent of the tenant that is a member of no group: it
        // authenticates, and no group's models or servers are its.
        "workload_no_group" => {
            Authorization(WorkloadWorld::token(WL_ISSUER, WL_NO_GROUP_SUB, 3600))
        }
        "workload_expired" => Authorization(WorkloadWorld::token(WL_ISSUER, WL_SUB, -3600)),
        "workload_tampered" => Authorization(WorkloadWorld::tampered()),
        // A token that stands in the other tenant (its provider, its agent,
        // its group), presented for this one.
        "workload_other_tenant" => {
            Authorization(WorkloadWorld::token(WL_OTHER_ISSUER, WL_OTHER_SUB, 3600))
        }
        "workload_disabled_agent" => {
            Authorization(WorkloadWorld::token(WL_ISSUER, WL_DISABLED_SUB, 3600))
        }
        "workload_expired_identity" => Authorization(WorkloadWorld::token(
            WL_ISSUER,
            WL_EXPIRED_IDENTITY_SUB,
            3600,
        )),
        "api_key_disabled_agent" => XApiKey(world.disabled_agent_api_key.clone()),
        "api_key_expired_agent" => XApiKey(world.expired_agent_api_key.clone()),
        "api_key_revoked_agent" => XApiKey(world.revoked_agent_api_key.clone()),
        other => panic!("no credential named {other}"),
    }
}

// ---------------------------------------------------------------------------
// The world
// ---------------------------------------------------------------------------

pub(crate) async fn seed_admin(pool: &PgPool, kind: &str, tenant: Option<&str>) -> String {
    let id = format!("{kind}-{}", uuid::Uuid::new_v4().simple());
    match tenant {
        None => {
            sqlx::query(
                "INSERT INTO system_admins (id, username, email, password_hash, is_active, \
                 is_locked, failed_login_attempts, permissions, totp_enabled) \
                 VALUES ($1, $1, $1 || '@test.local', 'x', true, false, 0, '[]'::json, false)",
            )
            .bind(&id)
            .execute(pool)
            .await
            .expect("system admin");
        }
        Some(tenant) => {
            let role = format!("role-{id}");
            sqlx::query(
                "INSERT INTO tenant_roles (id, tenant_id, name, is_system) \
                 VALUES ($1, $2, $1, false)",
            )
            .bind(&role)
            .bind(tenant)
            .execute(pool)
            .await
            .expect("role");
            sqlx::query(
                "INSERT INTO tenant_admins (id, tenant_id, role_id, username, email, \
                 password_hash, is_active, is_locked, failed_login_attempts, totp_enabled) \
                 VALUES ($1, $2, $3, $1, $1 || '@test.local', 'x', true, false, 0, false)",
            )
            .bind(&id)
            .bind(tenant)
            .bind(&role)
            .execute(pool)
            .await
            .expect("tenant admin");
        }
    }
    id
}

pub(crate) async fn seed_tenant_admin_session(pool: &PgPool, admin: &str, active: bool) -> String {
    let jti = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO tenant_admin_sessions (id, tenant_id, admin_id, token_hash, risk_score, \
         expires_at, is_active) VALUES ($1, $2, $3, 'h', 0.0, NOW() + interval '1 hour', $4)",
    )
    .bind(&jti)
    .bind(TENANT)
    .bind(admin)
    .bind(active)
    .execute(pool)
    .await
    .expect("tenant admin session");
    jti
}

pub(crate) async fn seed_world(pool: &PgPool) -> World {
    common::seed_tenant(pool, TENANT).await;
    common::seed_tenant(pool, OTHER_TENANT).await;
    common::seed_resource_group(pool, GROUP, TENANT).await;
    common::seed_end_user(pool, USER, TENANT).await;
    common::seed_end_user(pool, CUT_OFF_USER, TENANT).await;
    common::seed_mcp_server(pool, OAUTH_SERVER, TENANT, OAUTH_SERVER).await;
    // The model the `/v1/proxy/llm/admin/{model_id}/...` routes name (they
    // look the model up before anything else). Its provider has no catalogue
    // address, so the catalogue probe has nowhere to call. No request of the
    // matrix asks for it by name.
    common::seed_llm_provider(pool, "prov-matrix", TENANT, "matrix-provider").await;
    common::seed_llm_model(pool, "1", TENANT, "prov-matrix", "matrix-model").await;
    sqlx::query("UPDATE end_users SET sessions_valid_after = NOW() WHERE id = $1")
        .bind(CUT_OFF_USER)
        .execute(pool)
        .await
        .expect("cutoff");
    sqlx::query(
        "INSERT INTO end_user_revoked_tokens (jti, tenant_id, end_user_id, expires_at, reason) \
         VALUES ('revoked-portal-jti', $1, $2, NOW() + interval '1 day', 'logout')",
    )
    .bind(TENANT)
    .bind(USER)
    .execute(pool)
    .await
    .expect("revoked token");

    let api_key = common::seed_api_key(pool, TENANT, GROUP, None, "matrix").await;
    let revoked_api_key = common::seed_api_key(pool, TENANT, GROUP, None, "matrix-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'matrix-revoked'")
        .execute(pool)
        .await
        .expect("revoke key");

    let mut agent_keys = Vec::new();
    for (agent, enabled, expires, revoked) in [
        ("agent-matrix-key-disabled", false, None, false),
        ("agent-matrix-key-expired", true, Some("-1 hour"), false),
        ("agent-matrix-key-revoked", true, None, true),
    ] {
        sqlx::query(
            "INSERT INTO agent_identities \
             (id, tenant_id, name, source, enabled, status, default_deny, expires_at, \
              revoked_at) \
             VALUES ($1, $2, $1, 'manual', $3, 'active', false, now() + $4::interval, \
                     CASE WHEN $5 THEN now() END)",
        )
        .bind(agent)
        .bind(TENANT)
        .bind(enabled)
        .bind(expires)
        .bind(revoked)
        .execute(pool)
        .await
        .expect("agent identity of a key");
        let name = format!("matrix-{agent}");
        let key = common::seed_api_key(pool, TENANT, GROUP, None, &name).await;
        sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE name = $2")
            .bind(agent)
            .bind(&name)
            .execute(pool)
            .await
            .expect("bind the key to the agent");
        agent_keys.push(key);
    }
    let revoked_agent_api_key = agent_keys.pop().expect("three keys");
    let expired_agent_api_key = agent_keys.pop().expect("three keys");
    let disabled_agent_api_key = agent_keys.pop().expect("three keys");

    let tenant_admin_id = seed_admin(pool, "tenadm", Some(TENANT)).await;
    let system_admin_id = seed_admin(pool, "sysadm", None).await;
    let tenant_admin_jti = seed_tenant_admin_session(pool, &tenant_admin_id, true).await;
    let revoked_admin_jti = seed_tenant_admin_session(pool, &tenant_admin_id, false).await;
    let system_admin_jti = uuid::Uuid::new_v4().to_string();
    sqlx::query(
        "INSERT INTO system_admin_sessions (id, admin_id, token_hash, risk_score, expires_at, \
         is_active) VALUES ($1, $2, 'h', 0.0, NOW() + interval '1 hour', true)",
    )
    .bind(&system_admin_jti)
    .bind(&system_admin_id)
    .execute(pool)
    .await
    .expect("system admin session");

    let workload = seed_workload_world(pool).await;

    World {
        api_key,
        revoked_api_key,
        disabled_agent_api_key,
        expired_agent_api_key,
        revoked_agent_api_key,
        tenant_admin_jti,
        system_admin_jti,
        revoked_admin_jti,
        tenant_admin_id,
        system_admin_id,
        workload,
    }
}

// ---------------------------------------------------------------------------
// Asking
// ---------------------------------------------------------------------------

pub(crate) fn request(route: &Route, carry: Carry) -> Request<Body> {
    let mut headers = vec![("x-tenant-id", TENANT.to_string())];
    match carry {
        Carry::Nothing => {}
        Carry::XApiKey(key) => headers.push(("x-api-key", key)),
        Carry::Authorization(token) => headers.push(("authorization", format!("Bearer {token}"))),
        Carry::XGatewayAuthorization(token) => {
            headers.push(("x-gateway-authorization", format!("Bearer {token}")))
        }
    }
    request_with(route, &headers)
}

/// The route's request (method, path, a body its extractors accept) with
/// these headers and no others. Shared with the tenant-resolution matrix
/// (`tenant_resolution_matrix.rs`), which varies how the tenant is named.
pub(crate) fn request_with(route: &Route, headers: &[(&'static str, String)]) -> Request<Body> {
    let mut builder = Request::builder().method(route.method).uri(route.uri());
    for (name, value) in headers {
        builder = builder.header(*name, value);
    }
    let mut request = match route.send {
        Send::Nothing => builder.body(Body::empty()),
        Send::Json(body) => builder
            .header("content-type", "application/json")
            .body(Body::from(body)),
        Send::Multipart => builder
            .header("content-type", "multipart/form-data; boundary=matrix")
            .body(Body::from(
                "--matrix\r\ncontent-disposition: form-data; name=\"file\"; \
                 filename=\"a.txt\"\r\ncontent-type: text/plain\r\n\r\nhello\r\n\
                 --matrix\r\ncontent-disposition: form-data; name=\"model\"\r\n\r\nm\r\n\
                 --matrix--\r\n",
            )),
    }
    .expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    request.extensions_mut().insert(ConnectInfo(peer));
    request
}

pub(crate) type Matrix = BTreeMap<String, BTreeMap<String, u16>>;

/// Ask every route with every credential of `kinds`. One application per
/// route, so that nothing a route keeps in memory (a login throttle, a
/// circuit) carries from one row to the next.
pub(crate) async fn ask(
    make_app: &dyn Fn() -> Router,
    world: &World,
    kinds: &[&str],
    shard: usize,
    into: &mut Matrix,
) {
    for (_, route) in ROUTES
        .iter()
        .enumerate()
        .filter(|(i, _)| i % SHARDS == shard)
    {
        let app: Router = make_app();
        let row = into.entry(route.name()).or_default();
        for kind in kinds {
            let response = tokio::time::timeout(
                std::time::Duration::from_secs(20),
                app.clone().oneshot(request(route, credential(kind, world))),
            )
            .await
            .unwrap_or_else(|_| panic!("{} with {kind} did not answer", route.name()))
            .expect("router");
            let status = response.status().as_u16();
            // Why a route answered as it did: `CREDENTIAL_MATRIX_SHOW=<part of
            // the route's name>` prints each answer's body.
            if std::env::var("CREDENTIAL_MATRIX_SHOW").is_ok_and(|w| route.name().contains(&w)) {
                let body = axum::body::to_bytes(response.into_body(), 4096)
                    .await
                    .unwrap_or_default();
                eprintln!(
                    "{} {kind}: {status} {}",
                    route.name(),
                    String::from_utf8_lossy(&body)
                );
            }
            row.insert((*kind).to_string(), status);
        }
    }
}

pub(crate) fn fixture_path(shard: usize) -> std::path::PathBuf {
    crate::common::community_root()
        .join(FIXTURE_DIR)
        .join(format!("part_{shard}.json"))
}

pub(crate) fn read_fixture_part(shard: usize) -> Matrix {
    let text = std::fs::read_to_string(fixture_path(shard))
        .expect("the recorded matrix (record it with CREDENTIAL_MATRIX_RECORD=1)");
    serde_json::from_str(&text).expect("fixture JSON")
}

/// The whole recording.
pub(crate) fn read_fixture() -> Matrix {
    (0..SHARDS).flat_map(read_fixture_part).collect()
}

pub(crate) fn differences(expected: &Matrix, actual: &Matrix) -> Vec<String> {
    let mut out = Vec::new();
    for (route, row) in expected {
        let Some(got) = actual.get(route) else {
            out.push(format!("{route}: not asked"));
            continue;
        };
        for (kind, status) in row {
            match got.get(kind) {
                Some(s) if s == status => {}
                Some(s) => out.push(format!("{route} with {kind}: recorded {status}, now {s}")),
                None => out.push(format!("{route} with {kind}: not asked")),
            }
        }
        for kind in got.keys() {
            if !row.contains_key(kind) {
                out.push(format!("{route} with {kind}: not in the recording"));
            }
        }
    }
    for route in actual.keys() {
        if !expected.contains_key(route) {
            out.push(format!("{route}: not in the recording"));
        }
    }
    out
}

/// Credentials the gateway accepted before the token-type registry and
/// refuses since. Each answers, on every route, what a bearer that is no
/// credential at all answers there (`holes_in` checks it in the recording).
pub(crate) const REFUSED_SINCE_THE_REGISTRY: &[(&str, &str)] = &[
    (
        "no_typ",
        "a token with no `typ` is of no registered type; every issuer mints one",
    ),
    (
        "no_typ_end_user",
        "a portal session minted before `typ` existed; none can be alive (sessions last \
         hours, `typ` has been minted for longer), and there is no backward compatibility to keep",
    ),
    (
        "no_typ_admin_tenant",
        "a tenant administrator's session with no `typ`, as the control plane once minted it; the \
         control plane refuses it too, and its holder signs in again",
    ),
    (
        "skill_runner_token",
        "the claims of the skill runner's execution token; where an operator gives the \
         runner the gateway's secret, that token was a gateway session with no user",
    ),
    (
        "registry_service_token",
        "the service token the control plane signs for the MCP registry with JWT_SECRET \
         (and the registry's for the control plane) was accepted as a gateway session with no \
         user and no tenant of its own, for whichever tenant `X-Tenant-ID` named",
    ),
    (
        "admin_no_admin_type",
        "`typ: admin` without `admin_type` passed with no session check, so it could not \
         be revoked; no issuer mints one",
    ),
];

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// The step itself: a Community assembly has no verifier for portal sessions
// ---------------------------------------------------------------------------

/// What a Community assembly answers: the recording, except that a portal
/// session is no credential. Whatever its state (valid, expired, revoked,
/// for another tenant) and whichever header carries it, it answers what a
/// bearer that verifies as nothing answers on that route.
pub(crate) fn expected_of_a_community_assembly(mut expected: Matrix) -> Matrix {
    for (route, row) in expected.iter_mut() {
        let refused = row["garbage_bearer"];
        let unread = row["none"];
        for (kind, cell) in row.iter_mut() {
            if kind.starts_with("portal") {
                *cell = if header_not_read(route, kind) {
                    unread
                } else {
                    refused
                };
            }
            // A system administrator is the Enterprise
            // Edition's. A Community assembly has no verifier for that kind
            // of administrator, so the token answers what a bearer that is
            // no credential answers on that route. Tightened on purpose.
            if SYSTEM_ADMIN_KINDS.contains(&kind.as_str()) {
                *cell = refused;
            }
        }
        row.retain(|kind, _| !kind.contains('@'));
    }
    expected
}

/// The credentials of a system administrator in this matrix.
pub(crate) const SYSTEM_ADMIN_KINDS: &[&str] = &["admin_system"];

/// Routes on which a portal session is not a 401 on a Community assembly,
/// and why it is still not a credential there. `kind` is a credential of
/// the matrix, or `*` for every portal one.
pub(crate) const NOT_A_401_AND_NOT_A_CREDENTIAL: &[(&str, &str, &str)] = &[
    (
        "POST /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/token",
        "*",
        "the OAuth token endpoint: a gateway credential is optional by the protocol, and one \
         that does not verify is ignored (the grant itself is then refused, 400). A portal \
         session is ignored like any other bearer that verifies as nothing; the token that \
         results is not attributed to the end user",
    ),
    (
        "POST /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/revoke",
        "portal_x_gateway",
        "the route reads an MCP token from `Authorization` or the body and never looks at \
         `X-Gateway-Authorization`: the request is one with no token (400)",
    ),
];

pub(crate) fn listed_as_not_a_401(route: &str, kind: &str) -> bool {
    NOT_A_401_AND_NOT_A_CREDENTIAL
        .iter()
        .any(|(r, k, _)| *r == route && (*k == "*" || *k == kind))
}

pub(crate) fn header_not_read(route: &str, kind: &str) -> bool {
    NOT_A_401_AND_NOT_A_CREDENTIAL
        .iter()
        .any(|(r, k, _)| *r == route && *k == kind)
}

/// The mounts an extension makes (batch jobs, the
/// Portal; the knowledge base's connector icons), each with the Community
/// prefix it sits under. A Community
/// assembly serves no route under them: the mounted-route recordings differ
/// by exactly these (`batch_extension.rs`, `portal_extension.rs`).
pub(crate) const MOUNTED_BY_AN_EXTENSION: &[(&str, &str)] = &[
    ("/v1/proxy/batch", "/v1/proxy"),
    ("/v1/public/tenants", "/v1/public"),
    ("/v1/portal/auth", "/v1/portal"),
    ("/v1/portal/alerts", "/v1/portal"),
    ("/v1/portal/evidence", "/v1/portal"),
    ("/v1/portal/models", "/v1/portal"),
    ("/v1/portal/skills", "/v1/portal"),
    ("/v1/portal/knowledgebases", "/v1/portal"),
    ("/v1/portal/kb-identities", "/v1/portal"),
    ("/v1/portal/notifications", "/v1/portal"),
    ("/v1/portal/servers", "/v1/portal"),
    ("/v1/portal/mcp-apps", "/v1/portal"),
    ("/v1/portal/settings", "/v1/portal"),
    ("/v1/assets/connector-icons", "/v1/assets"),
];

/// The prefix an extension-mounted route sits under, when it is one.
pub(crate) fn extension_mount_parent(route: &str) -> Option<&'static str> {
    ROUTES.iter().find(|r| r.name() == route).and_then(|r| {
        MOUNTED_BY_AN_EXTENSION
            .iter()
            .find(|(mount, _)| *mount == r.mount)
            .map(|(_, parent)| *parent)
    })
}

pub(crate) fn mounted_by_an_extension(route: &str) -> bool {
    extension_mount_parent(route).is_some()
}

/// What a Community assembly answers, with each credential, on a path under
/// the same prefix (`/v1/proxy/`, `/v1/portal/`, `/v1/public/`) that no
/// assembly serves: the same method, body and headers as `route`'s request. This is what "the route is not there" looks like
/// for that credential, whatever the layers in front of the router make of
/// it; nothing about it is assumed here.
pub(crate) async fn answers_where_no_route_is(
    make_app: &dyn Fn() -> Router,
    world: &World,
    route: &Route,
) -> BTreeMap<String, u16> {
    let nowhere = Route {
        module: route.module,
        mount: extension_mount_parent(&route.name()).expect("a route an extension mounts"),
        path: "/zz-no-assembly-serves-this/jobs",
        method: route.method,
        send: route.send,
    };
    let app = make_app();
    let mut row = BTreeMap::new();
    for kind in CREDENTIALS {
        let response = app
            .clone()
            .oneshot(request(&nowhere, credential(kind, world)))
            .await
            .expect("router");
        row.insert((*kind).to_string(), response.status().as_u16());
    }
    row
}

/// The Community assembly this tree can build: every route of the one
/// application that no extension mounts (not batch jobs, not the
/// portal's own routes) over a state nothing was installed on.
pub(crate) async fn the_community_shard_refuses_portal_sessions(pool: PgPool, shard: usize) {
    let world = seed_world(&pool).await;
    let state_pool = pool.clone();
    let make_app = move || common::community_app(state_pool.clone());
    let mut matrix = Matrix::new();
    ask(&make_app, &world, CREDENTIALS, shard, &mut matrix).await;

    let mut expected = expected_of_a_community_assembly(read_fixture_part(shard));
    // A route an extension mounts is not there: with every credential it
    // answers what a path no assembly serves answers, and with a credential
    // the full assembly accepts that is the router's 404.
    for (_, route) in ROUTES
        .iter()
        .enumerate()
        .filter(|(i, r)| i % SHARDS == shard && mounted_by_an_extension(&r.name()))
    {
        let nowhere = answers_where_no_route_is(&make_app, &world, route).await;
        assert_eq!(
            nowhere["api_key"], 404,
            "the control: a path no assembly serves is a 404 for a valid key"
        );
        assert_eq!(
            matrix[&route.name()],
            nowhere,
            "{}: a Community assembly serves this route",
            route.name()
        );
        expected.insert(route.name(), nowhere);
    }
    // The rule in its own words, before the cell-by-cell comparison: on a
    // route that asks for a credential at all, a portal session is a 401.
    let mut accepted = Vec::new();
    for (route, row) in &matrix {
        if OPEN_WITHOUT_A_CREDENTIAL.iter().any(|(r, _)| r == route) {
            continue;
        }
        if mounted_by_an_extension(route) {
            // No surface here to accept or refuse a credential: checked
            // above against a path no assembly serves.
            continue;
        }
        for (kind, status) in row {
            if kind.starts_with("portal") && (*status != 401) != listed_as_not_a_401(route, kind) {
                accepted.push(format!("{route} with {kind}: {status}"));
            }
        }
    }
    assert!(
        accepted.is_empty(),
        "a Community assembly must answer 401 to a portal session on every surface:\n{}",
        accepted.join("\n")
    );
    // A system administrator's token is never a
    // credential of a Community assembly. On every route it answers what a
    // bearer that verifies as nothing answers; where the full assembly
    // answered something else, the cell changed, and every such change is
    // a tightening (the full assembly accepted the token there).
    let recording = read_fixture_part(shard);
    for (route, row) in &matrix {
        if mounted_by_an_extension(route) {
            continue;
        }
        for kind in SYSTEM_ADMIN_KINDS {
            assert_eq!(
                row[*kind], row["garbage_bearer"],
                "{route}: a Community assembly tells a system administrator's token from a \
                 bearer that is no credential"
            );
            let before = recording[route][*kind];
            if before != row[*kind] {
                assert!(
                    matches!(row[*kind], 401 | 403),
                    "{route} with {kind}: {before} -> {}: not a refusal",
                    row[*kind]
                );
                if std::env::var("CREDENTIAL_MATRIX_PRINT_SYSTEM_ADMIN").is_ok() {
                    println!("system admin {route} | {kind} | {before} -> {}", row[*kind]);
                }
            }
        }
    }
    let diff = differences(&expected, &matrix);
    assert!(
        diff.is_empty(),
        "{} cells of a Community assembly differ from the recording:\n{}",
        diff.len(),
        diff.join("\n")
    );
}

macro_rules! community_shards {
    ($($name:ident = $shard:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            the_community_shard_refuses_portal_sessions(pool, $shard).await;
        }
    )+};
}

community_shards!(
    a_community_assembly_refuses_portal_sessions_everywhere_0 = 0,
    a_community_assembly_refuses_portal_sessions_everywhere_1 = 1,
    a_community_assembly_refuses_portal_sessions_everywhere_2 = 2,
    a_community_assembly_refuses_portal_sessions_everywhere_3 = 3,
    a_community_assembly_refuses_portal_sessions_everywhere_4 = 4,
    a_community_assembly_refuses_portal_sessions_everywhere_5 = 5,
    a_community_assembly_refuses_portal_sessions_everywhere_6 = 6,
    a_community_assembly_refuses_portal_sessions_everywhere_7 = 7,
);

/// The request reached the route's authentication: with no credential a
/// route either refuses (401/403) or is one of the surfaces listed in
/// `OPEN_WITHOUT_A_CREDENTIAL`. A row that answers 400/404/422 for *every*
/// credential would say nothing about authentication.
#[test]
fn every_recorded_row_tells_credentials_apart_or_is_listed_as_open() {
    let matrix = read_fixture();
    let mut silent = Vec::new();
    for (route, row) in &matrix {
        let none = row["none"];
        let all_same = row.values().all(|s| *s == none);
        let listed = OPEN_WITHOUT_A_CREDENTIAL.iter().any(|(r, _)| r == route);
        if all_same != listed {
            silent.push(format!(
                "{route}: none={none}, every cell the same: {all_same}, listed open: {listed}"
            ));
        }
    }
    assert!(silent.is_empty(), "{}", silent.join("\n"));
    assert_eq!(matrix.len(), ROUTES.len(), "one row per route");
}

/// Fail closed, in the recording's own cells: where a route asks for a
/// credential, a session whose store cannot be read is refused (503 from the
/// portal's verifier, 500 from the administrator's, 502 where the A2A
/// surface words it, 401 where a route answers every authentication failure
/// alike), and so is every credential that does not stand. A recording
/// that held a pass in one of these cells would be a recording of a hole.
#[test]
fn the_recording_refuses_what_cannot_be_verified_on_every_credentialed_route() {
    let recording = read_fixture();
    let holes = holes_in(&recording);
    assert!(holes.is_empty(), "{}", holes.join("\n"));

    // The check, against the recordings it must not accept.
    let route = "POST /v1/proxy/llm/chat/completions";
    for (kind, pass) in [
        ("portal@session_store_down", 200),
        ("admin_tenant@session_store_down", 404),
        ("portal_revoked", 404),
        ("unknown_typ", 200),
        ("no_typ", 404),
        ("gateway_other_tenant", 404),
    ] {
        let mut holed = recording.clone();
        holed
            .get_mut(route)
            .expect("the chat route")
            .insert(kind.to_string(), pass);
        assert_eq!(
            holes_in(&holed).len(),
            1,
            "{kind} answered {pass} was not seen"
        );
    }
}

/// An API key bound to an agent identity is the
/// agent's credential. When the identity does not stand (disabled, past its
/// `expires_at`, revoked) the key is no credential, on every route: it
/// answers exactly what a revoked key answers there. Before, it
/// authenticated and was refused only where a route reached the agent's
/// gate, so routes with no target (files, job status, model listing, token
/// counting, trace ingest) served it.
#[test]
fn a_key_of_an_agent_that_does_not_stand_answers_as_a_revoked_key_on_every_route() {
    let recording = read_fixture();
    let served = keys_of_fallen_agents_served(&recording);
    assert!(
        served.is_empty(),
        "{} cells:\n{}",
        served.len(),
        served.join("\n")
    );

    // The check, against a recording it must not accept.
    let mut holed = recording.clone();
    holed
        .get_mut("GET /v1/proxy/llm/models")
        .expect("the model listing")
        .insert("api_key_revoked_agent".to_string(), 200);
    assert_eq!(keys_of_fallen_agents_served(&holed).len(), 1);
}

pub(crate) fn keys_of_fallen_agents_served(recording: &Matrix) -> Vec<String> {
    let mut served = Vec::new();
    for (route, row) in recording {
        let revoked = row["api_key_revoked"];
        for kind in [
            "api_key_disabled_agent",
            "api_key_expired_agent",
            "api_key_revoked_agent",
        ] {
            match row.get(kind) {
                Some(status) if *status == revoked => {}
                other => served.push(format!(
                    "{route} with {kind}: {other:?}, a revoked key answers {revoked}"
                )),
            }
        }
    }
    served
}

pub(crate) fn holes_in(recording: &Matrix) -> Vec<String> {
    const UNREADABLE: [&str; 3] = [
        "portal@session_store_down",
        "admin_tenant@session_store_down",
        "admin_system@session_store_down",
    ];
    /// Not credentials, by signature, expiry, type or session.
    const NEVER: [&str; 22] = [
        "portal_expired",
        "portal_revoked",
        "portal_before_cutoff",
        "portal_wrong_key",
        "portal_no_jti",
        "admin_expired",
        "admin_revoked",
        "admin_no_session",
        "admin_wrong_key",
        "admin_unknown_admin_type",
        "gateway_expired",
        "gateway_wrong_key",
        "pre_session_2fa_challenge",
        "pre_session_2fa_enrollment",
        "pre_session_admin_2fa_challenge",
        "unknown_typ",
        "alg_none",
        "workload_expired",
        "workload_tampered",
        "workload_other_tenant",
        "workload_disabled_agent",
        "workload_expired_identity",
    ];
    let mut holes = Vec::new();
    for (route, row) in recording {
        if OPEN_WITHOUT_A_CREDENTIAL.iter().any(|(r, _)| r == route) {
            continue;
        }
        for kind in UNREADABLE {
            if ![401, 500, 502, 503].contains(&row[kind]) {
                holes.push(format!("{route} with {kind}: {}", row[kind]));
            }
        }
        // Each answers what a bearer that is no credential answers there.
        for kind in NEVER
            .iter()
            .chain(REFUSED_SINCE_THE_REGISTRY.iter().map(|(kind, _)| kind))
        {
            if row[*kind] != row["garbage_bearer"] {
                holes.push(format!(
                    "{route} with {kind}: {}, a bearer that is nothing: {}",
                    row[*kind], row["garbage_bearer"]
                ));
            }
        }
        // A token for another tenant is a 403, or whatever the route says
        // to every refused credential.
        for kind in [
            "portal_other_tenant",
            "admin_other_tenant",
            "gateway_other_tenant",
        ] {
            if row[kind] != 403 && row[kind] != row["garbage_bearer"] {
                holes.push(format!("{route} with {kind}: {}", row[kind]));
            }
        }
    }
    holes
}

/// The file, image, audio and video
/// routes accept an API key, governed as on the chat routes. Before it they
/// asked for a Portal session only, and an API key answered 401. The one
/// loosening of this matrix: on each of these routes the cells `api_key`,
/// `api_key_bearer` and `api_key@session_store_down` went from 401 to the
/// route's own answer for a caller it serves (no model, file or job is
/// seeded, so that is 404, or 400 for an upload with no `kind`, or the
/// idempotent 204 of a delete). Nothing else changed.
pub(crate) const API_KEY_ACCEPTED_SINCE_P3_36: &[&str] = &[
    "POST /v1/proxy/files/",
    "GET /v1/proxy/files/{file_id}",
    "GET /v1/proxy/files/{file_id}/url",
    "GET /v1/proxy/files/{file_id}/content",
    "DELETE /v1/proxy/files/{file_id}",
    "POST /v1/proxy/llm/images/generations/",
    "POST /v1/proxy/llm/audio/speech/",
    "POST /v1/proxy/llm/audio/transcriptions/",
    "POST /v1/proxy/llm/video/generations/",
    "GET /v1/proxy/llm/video/jobs/{job_id}",
    "DELETE /v1/proxy/llm/video/jobs/{job_id}",
];

/// In the recording's own cells: on the media routes a key that stands
/// is served, and a key that does not (revoked, unknown) answers what no
/// credential answers. And the file, media and video routes are exactly the
/// listed ones, so a new route under them cannot go unlisted.
#[test]
fn the_recording_accepts_an_api_key_on_the_media_routes_and_only_one_that_stands() {
    let recording = read_fixture();
    for route in API_KEY_ACCEPTED_SINCE_P3_36 {
        let row = recording
            .get(*route)
            .unwrap_or_else(|| panic!("{route} is not in the recording"));
        assert_eq!(row["none"], 401, "{route}: asks for a credential");
        for kind in ["api_key", "api_key_bearer", "api_key@session_store_down"] {
            assert!(
                !matches!(row[kind], 401 | 403 | 500 | 502 | 503),
                "{route} with {kind}: {} is a refusal",
                row[kind]
            );
        }
        for kind in ["api_key_revoked", "api_key_unknown"] {
            assert_eq!(row[kind], 401, "{route} with {kind}");
        }
    }
    let media: Vec<&String> = recording
        .keys()
        .filter(|r| {
            let path = r.split(' ').nth(1).unwrap_or_default();
            path.starts_with("/v1/proxy/files")
                || [
                    "/v1/proxy/llm/images/",
                    "/v1/proxy/llm/audio/",
                    "/v1/proxy/llm/video/",
                ]
                .iter()
                .any(|p| path.starts_with(p))
        })
        .collect();
    assert_eq!(
        media.len(),
        API_KEY_ACCEPTED_SINCE_P3_36.len(),
        "the file, image, audio and video routes of the recording: {media:?}"
    );
}

/// Routes that answer every credential of the matrix alike, and why.
pub(crate) const OPEN_WITHOUT_A_CREDENTIAL: &[(&str, &str)] = &[
    ("GET /health", "liveness"),
    ("GET /healthz", "liveness"),
    ("GET /v1/version", "the build's version"),
    ("GET /v1/proxy/llm/health", "liveness"),
    ("GET /v1/proxy/mcp/health", "liveness"),
    ("GET /v1/proxy/vmcp/health", "liveness"),
    (
        "GET /.well-known/brutor-evidence-keys.json",
        "public evidence keys, pinned out of band",
    ),
    (
        "GET /.well-known/agent-card/{tenant_id}/{card_name}",
        "A2A discovery (404: no such card)",
    ),
    (
        "GET /.well-known/a2a-jwks/{tenant_id}",
        "A2A discovery (404: no keys)",
    ),
    (
        "GET /v1/public/tenants/{tenant_id}/theme",
        "the sign-in page's theme",
    ),
    (
        "GET /v1/assets/mcp-servers/{server_id}/icon",
        "an image (404: none stored)",
    ),
    (
        "GET /v1/assets/llm-providers/{provider_id}/icon",
        "an image (404: none stored)",
    ),
    (
        "GET /v1/assets/users/end-user/{user_id}/photo",
        "an image (404: none stored)",
    ),
    (
        "GET /v1/assets/connector-icons/{filename}",
        "an image (404: no such file)",
    ),
    (
        "GET /v1/media/{*key}",
        "a signed media download link: the link is the credential (404: not a valid link)",
    ),
    (
        "GET /v1/portal/kb-identities/connectors",
        "a constant catalogue of connector kinds",
    ),
    (
        "POST /v1/portal/auth/register",
        "sign-up; takes no session (422: the body is refused)",
    ),
    (
        "POST /v1/portal/auth/login",
        "sign-in; takes no session (422: the body is refused)",
    ),
    (
        "POST /v1/portal/auth/login/2fa",
        "sign-in, second factor; takes its own token in the body",
    ),
    (
        "POST /v1/portal/auth/login/2fa/enroll/setup",
        "sign-in, enrolment; takes its own token in the body",
    ),
    (
        "POST /v1/portal/auth/login/2fa/enroll/verify",
        "sign-in, enrolment; takes its own token in the body",
    ),
    (
        "GET /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/oauth-protected-resource",
        "OAuth discovery document (404: no identity provider is bound)",
    ),
    (
        "GET /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/.well-known/openid-configuration",
        "OAuth discovery document (404: no identity provider is bound)",
    ),
    (
        "GET /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/authorize",
        "the OAuth authorization redirect; the browser carries no gateway credential",
    ),
    (
        "GET /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/callback",
        "the identity provider's redirect back; carries a state, not a credential",
    ),
    (
        "POST /v1/proxy/oauth/{mcp_server_id}/{idp_server_id}/register",
        "OAuth dynamic client registration, open by the protocol",
    ),
    (
        "GET /v1/evidence/records/{record_id}/aac",
        "404 for everyone unless BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED=1 (then a service principal)",
    ),
];

#[test]
fn the_matrix_lists_every_route() {
    let modules = route_modules();
    let mut missing = Vec::new();
    let mut declared = 0;
    for (name, file) in &modules {
        if NOT_IN_THE_APPLICATION.contains(&name.as_str()) {
            continue;
        }
        let text = std::fs::read_to_string(file).expect("read");
        let text = match text.find("#[cfg(test)]\nmod ") {
            Some(at) => &text[..at],
            None => &text[..],
        };
        for (path, methods) in declared_routes(text) {
            for method in methods {
                declared += 1;
                let asked: &[&str] = if method == "ANY" {
                    &["GET", "POST"]
                } else {
                    &[method]
                };
                for m in asked {
                    if !ROUTES
                        .iter()
                        .any(|r| r.module == name && r.path == path && r.method == *m)
                    {
                        missing.push(format!("{name}: {m} {path}"));
                    }
                }
            }
        }
    }
    assert!(
        missing.is_empty(),
        "routes with no row in the credential matrix:\n{}",
        missing.join("\n")
    );
    // The routes the extensions mount are declared in the Enterprise
    // workspace: counted, and compared below, where this run has it.
    let enterprise = crate::common::enterprise_root().is_some();
    let least = if enterprise { 130 } else { 90 };
    assert!(declared >= least, "the scan found only {declared} routes");
    // and nothing in the table that the source does not declare
    for route in ROUTES {
        if !enterprise && mounted_by_an_extension(&route.name()) {
            continue;
        }
        let (_, file) = modules
            .iter()
            .find(|(name, _)| name == route.module)
            .unwrap_or_else(|| panic!("no route module is named {}", route.module));
        let text = std::fs::read_to_string(file).expect("module");
        assert!(
            declared_routes(&text)
                .iter()
                .any(|(path, _)| path == route.path),
            "{} is not declared in {}",
            route.path,
            route.module
        );
    }
}

/// The route modules of the Enterprise extensions, under the names the
/// matrix rows carry (the file names they had in `src/routes/`), where this
/// repository carries the Enterprise workspace. The recording is of the full
/// composition, so its rows name these too (the Community suite derives
/// what a Community assembly answers from the same record).
const ENTERPRISE_ROUTE_MODULES: &[(&str, &str)] = &[("batch.rs", "src/batch/routes.rs")];

/// `(row name, file)` of every route module: the Community crate's
/// `src/routes/*.rs`, then the Enterprise ones that exist.
fn route_modules() -> Vec<(String, std::path::PathBuf)> {
    let root = crate::common::community_root();
    let mut modules = Vec::new();
    for entry in std::fs::read_dir(root.join("src/routes"))
        .expect("src/routes")
        .flatten()
    {
        let name = entry.file_name().to_string_lossy().to_string();
        if name.ends_with(".rs") {
            modules.push((name, entry.path()));
        }
    }
    // The Enterprise ones, where this run has the Enterprise workspace
    // (`tests/support/roots.rs`): batch jobs' module, and the route modules
    // of the Portal's and the knowledge base's pages, which are not
    // in `src/routes`.
    if let Some(enterprise) = crate::common::enterprise_root() {
        for (name, file) in ENTERPRISE_ROUTE_MODULES {
            modules.push((name.to_string(), enterprise.join(file)));
        }
        for entry in std::fs::read_dir(enterprise.join("src/routes"))
            .expect("the Enterprise route modules")
            .flatten()
        {
            let name = entry.file_name().to_string_lossy().to_string();
            if name.ends_with(".rs") {
                modules.push((name, entry.path()));
            }
        }
    }
    modules
}

/// `(path, methods)` of every `.route("path", get(..).post(..))` in `text`.
pub(crate) fn declared_routes(text: &str) -> Vec<(String, Vec<&'static str>)> {
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(i) = text[from..].find(".route(") {
        let at = from + i + ".route(".len();
        from = at;
        let rest = text[at..].trim_start();
        if !rest.starts_with('"') {
            continue; // prose, or a route built from a variable (there is none)
        }
        let end = rest[1..].find('"').expect("closing quote") + 1;
        let path = rest[1..end].to_string();
        // The handler expression runs to the parenthesis that closes
        // `.route(`.
        let tail = &rest[end..];
        let mut depth = 1;
        let mut stop = tail.len();
        for (i, c) in tail.char_indices() {
            match c {
                '(' => depth += 1,
                ')' => depth -= 1,
                _ => {}
            }
            if depth == 0 {
                stop = i;
                break;
            }
        }
        let handler = &tail[..stop];
        let mut methods = Vec::new();
        for (word, method) in [
            ("get(", "GET"),
            ("post(", "POST"),
            ("put(", "PUT"),
            ("patch(", "PATCH"),
            ("delete(", "DELETE"),
            ("any(", "ANY"),
        ] {
            let found = handler.match_indices(word).any(|(i, _)| {
                // a method router call, not the tail of another identifier
                !handler[..i]
                    .chars()
                    .next_back()
                    .is_some_and(|c| c.is_alphanumeric() || c == '_')
            });
            if found {
                methods.push(method);
            }
        }
        if !methods.is_empty() {
            out.push((path, methods));
        }
    }
    out
}

#[test]
fn the_route_scan_reads_methods() {
    let text = r#"
        Router::new()
            .route("/a", get(list))
            .route("/{id}", get(read).delete(remove))
            .route(
                "/b",
                post(create).layer(axum::middleware::from_fn(mark)),
            )
            .route("/{server_id}/{*path}", any(proxy))
    "#;
    assert_eq!(
        declared_routes(text),
        vec![
            ("/a".to_string(), vec!["GET"]),
            ("/{id}".to_string(), vec!["GET", "DELETE"]),
            ("/b".to_string(), vec!["POST"]),
            ("/{server_id}/{*path}".to_string(), vec!["ANY"]),
        ]
    );
}

// ---------------------------------------------------------------------------
// A key of a deactivated end user
// ---------------------------------------------------------------------------

/// A key bound to an end user stops being a credential when the user is
/// deactivated, as it does when the key is revoked: on every route of the
/// edition's application the two answer alike. Asked live, both of them, so
/// the comparison holds whatever a route answers a dead key. The control is
/// the matrix's key that stands: the shard has routes that tell it from a
/// revoked one, so "alike" is not "every key answers alike".
pub(crate) async fn a_deactivated_users_key_answers_as_a_revoked_key(pool: PgPool, shard: usize) {
    const BOUND_USER: i32 = 3;
    let world = seed_world(&pool).await;
    common::seed_end_user(&pool, BOUND_USER, TENANT).await;
    common::add_direct_membership(&pool, BOUND_USER, GROUP, TENANT).await;
    let bound = common::seed_api_key(&pool, TENANT, GROUP, Some(BOUND_USER), "matrix-user").await;
    sqlx::query("UPDATE end_users SET is_active = false WHERE id = $1")
        .bind(BOUND_USER)
        .execute(&pool)
        .await
        .expect("deactivate the end user");
    async fn status(app: &Router, route: &Route, key: &str) -> u16 {
        tokio::time::timeout(
            std::time::Duration::from_secs(20),
            app.clone()
                .oneshot(request(route, Carry::XApiKey(key.to_string()))),
        )
        .await
        .unwrap_or_else(|_| panic!("{} did not answer", route.name()))
        .expect("router")
        .status()
        .as_u16()
    }

    let mut differs = Vec::new();
    let mut told_apart = 0;
    for (_, route) in ROUTES
        .iter()
        .enumerate()
        .filter(|(i, _)| i % SHARDS == shard)
    {
        let app = common::edition_app(pool.clone());
        let revoked = status(&app, route, &world.revoked_api_key).await;
        let deactivated = status(&app, route, &bound).await;
        if deactivated != revoked {
            differs.push(format!(
                "{}: a revoked key {revoked}, a deactivated user's key {deactivated}",
                route.name()
            ));
        }
        if status(&app, route, &world.api_key).await != revoked {
            told_apart += 1;
        }
    }
    assert!(
        differs.is_empty(),
        "a key of a deactivated end user is served where a revoked key is refused:\n{}",
        differs.join("\n")
    );
    assert!(
        told_apart >= 3,
        "only {told_apart} routes of shard {shard} tell a key that stands from a revoked one: \
         the comparison proves nothing"
    );
}

macro_rules! deactivated_user_shards {
    ($($name:ident = $shard:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_deactivated_users_key_answers_as_a_revoked_key(pool, $shard).await;
        }
    )+};
}

deactivated_user_shards!(
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_0 = 0,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_1 = 1,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_2 = 2,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_3 = 3,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_4 = 4,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_5 = 5,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_6 = 6,
    a_deactivated_users_key_answers_as_a_revoked_key_everywhere_7 = 7,
);

// ---------------------------------------------------------------------------
// A shared key acts for its own tenant
// ---------------------------------------------------------------------------
//
// A shared (`global`-scope) API key is a tenant's own: a tenant administrator
// mints it, bound to one of the tenant's resource groups. It used to
// authenticate for whichever tenant `X-Tenant-ID` named unless the operator
// had listed the tenants a shared key may act for. Now, with no list, it acts for
// its own tenant only; with a list, for its own tenant and the tenants on
// it. The rule is the key lookup's (`auth::authenticate_request`), so it
// holds on every route: asked here on all of them.

/// Another tenant's shared key, bound to a group of that tenant.
async fn seed_foreign_shared_key(pool: &PgPool) -> String {
    common::seed_resource_group(pool, "rg-matrix-foreign", OTHER_TENANT).await;
    common::seed_shared_group_api_key(pool, OTHER_TENANT, "rg-matrix-foreign", "matrix-foreign")
        .await
}

/// Routes that act for no one tenant (`service_auth::require_service_principal`
/// with no tenant: the audit chains, the evidence service, the control
/// plane's approval decision). The request's `X-Tenant-ID` is not what a key
/// is held to there; the route admits the platform service key and an
/// administrator, and answers every tenant's key 403.
const SYSTEM_SCOPED_MOUNTS: &[&str] = &["/v1/audit/", "/v1/evidence/", "/v1/internal/approvals/"];

fn system_scoped(route: &str) -> bool {
    SYSTEM_SCOPED_MOUNTS.iter().any(|mount| {
        route
            .split(' ')
            .nth(1)
            .is_some_and(|p| p.starts_with(mount))
    })
}

async fn key_status(app: &Router, route: &Route, key: &str) -> u16 {
    tokio::time::timeout(
        std::time::Duration::from_secs(20),
        app.clone()
            .oneshot(request(route, Carry::XApiKey(key.to_string()))),
    )
    .await
    .unwrap_or_else(|_| panic!("{} did not answer", route.name()))
    .expect("router")
    .status()
    .as_u16()
}

/// With no allow-list (and with a list that names some other tenant), a
/// shared key of tenant A that names tenant B is no credential of B: on
/// every route it answers what a revoked key answers. The control is the
/// matrix's own key: the shard has routes that tell it from a revoked one.
pub(crate) async fn a_foreign_shared_key_answers_as_a_revoked_key(
    pool: PgPool,
    shard: usize,
    allow_list: Option<Vec<String>>,
) {
    assert!(
        brutor_gateway_core::services::auth::configure_global_key_allowed_targets(
            allow_list.clone()
        ),
        "the allow-list was already fixed in this process"
    );
    let world = seed_world(&pool).await;
    let foreign = seed_foreign_shared_key(&pool).await;

    let mut differs = Vec::new();
    let mut told_apart = 0;
    for (_, route) in ROUTES
        .iter()
        .enumerate()
        .filter(|(i, _)| i % SHARDS == shard)
    {
        let app = common::edition_app(pool.clone());
        let revoked = key_status(&app, route, &world.revoked_api_key).await;
        let got = key_status(&app, route, &foreign).await;
        let own = key_status(&app, route, &world.api_key).await;
        if system_scoped(&route.name()) {
            // No tenant is named to the key lookup here, so the key is its
            // own tenant's key, and it is refused as every key that is not
            // the platform's is: this tenant's own key gets the same 403.
            // (Or the route answers everyone alike: the AAC profile is off.)
            if (got, own) != (403, 403) && got != revoked {
                differs.push(format!(
                    "{}: a system-scoped route answers another tenant's shared key {got} and \
                     this tenant's key {own} (403 for both)",
                    route.name()
                ));
            }
        } else if got != revoked {
            differs.push(format!(
                "{}: a revoked key {revoked}, another tenant's shared key {got}",
                route.name()
            ));
        }
        if own != revoked {
            told_apart += 1;
        }
    }
    assert!(
        differs.is_empty(),
        "allow-list {allow_list:?}: another tenant's shared key is a credential of this tenant \
         on {} routes:\n{}",
        differs.len(),
        differs.join("\n")
    );
    assert!(
        told_apart >= 3,
        "only {told_apart} routes of shard {shard} tell a key that stands from a revoked one: \
         the comparison proves nothing"
    );
}

macro_rules! foreign_shared_key_shards {
    ($($none:ident, $other:ident = $shard:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $none(pool: PgPool) {
            a_foreign_shared_key_answers_as_a_revoked_key(pool, $shard, None).await;
        }
        /// A list that does not name the tenant asked for changes nothing.
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $other(pool: PgPool) {
            a_foreign_shared_key_answers_as_a_revoked_key(
                pool,
                $shard,
                Some(vec!["some-third-tenant".to_string()]),
            )
            .await;
        }
    )+};
}

foreign_shared_key_shards!(
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_0,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_0 = 0,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_1,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_1 = 1,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_2,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_2 = 2,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_3,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_3 = 3,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_4,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_4 = 4,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_5,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_5 = 5,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_6,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_6 = 6,
    with_no_allow_list_a_foreign_shared_key_is_refused_everywhere_7,
    with_another_tenant_listed_a_foreign_shared_key_is_refused_everywhere_7 = 7,
);

/// With the tenant on the allow-list the key is a credential there: it is
/// authenticated as acting for that tenant (the context names the tenant
/// acted for, and the key), and the routes stop answering it as a revoked
/// key. It is still its own tenant's key: where a route holds a credential
/// to the tenant's own resources (model access through the key's group, the
/// media routes' owner check) it is refused there, as before.
pub(crate) async fn a_listed_tenant_accepts_the_foreign_shared_key(pool: PgPool, shard: usize) {
    use brutor_gateway_core::models::auth::AuthType;
    use brutor_gateway_core::services::auth::{
        authenticate_request, configure_global_key_allowed_targets,
    };
    assert!(configure_global_key_allowed_targets(Some(vec![
        TENANT.to_string()
    ])));
    let world = seed_world(&pool).await;
    let foreign = seed_foreign_shared_key(&pool).await;

    let state = common::build_test_state(pool.clone());
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-api-key", foreign.parse().expect("header"));
    let ctx = authenticate_request(
        &headers,
        TENANT,
        &state.config.jwt_secret,
        &pool,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    .expect("a listed tenant accepts the key");
    assert_eq!(ctx.auth_type, AuthType::ApiKey);
    assert_eq!(ctx.tenant_id, TENANT, "the tenant acted for");
    assert_eq!(ctx.api_key_name.as_deref(), Some("matrix-foreign"));
    // A tenant that is not listed is still refused.
    common::seed_tenant(&pool, "tenant-matrix-unlisted").await;
    assert!(
        authenticate_request(
            &headers,
            "tenant-matrix-unlisted",
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            true,
        )
        .await
        .is_err(),
        "the key acted for a tenant that is not on the list"
    );

    let mut accepted = 0;
    for (_, route) in ROUTES
        .iter()
        .enumerate()
        .filter(|(i, _)| i % SHARDS == shard)
    {
        let app = common::edition_app(pool.clone());
        let revoked = key_status(&app, route, &world.revoked_api_key).await;
        if key_status(&app, route, &foreign).await != revoked {
            accepted += 1;
        }
    }
    assert!(
        accepted >= 3,
        "only {accepted} routes of shard {shard} answer a listed tenant's foreign shared key \
         differently from a revoked key"
    );
}

macro_rules! listed_tenant_shards {
    ($($name:ident = $shard:expr),+ $(,)?) => {$(
        #[sqlx::test(migrations = "./tests/migrations")]
        async fn $name(pool: PgPool) {
            a_listed_tenant_accepts_the_foreign_shared_key(pool, $shard).await;
        }
    )+};
}

listed_tenant_shards!(
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_0 = 0,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_1 = 1,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_2 = 2,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_3 = 3,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_4 = 4,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_5 = 5,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_6 = 6,
    with_the_tenant_listed_a_foreign_shared_key_is_accepted_7 = 7,
);

/// The platform service key is not a tenant's shared key: the control plane
/// seeds it and no tenant administrator can mint one. It serves every tenant
/// (health checks, the KB uploader, the control plane's routes) with no list,
/// as before; a configured list bounds it as it bounds every global key.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_platform_service_key_acts_for_any_tenant_with_no_list(pool: PgPool) {
    use brutor_gateway_core::services::auth::{
        authenticate_request, configure_global_key_allowed_targets,
    };
    assert!(configure_global_key_allowed_targets(None));
    seed_world(&pool).await;
    let platform = common::seed_key(&pool, OTHER_TENANT, None, true, "matrix-platform").await;
    let state = common::build_test_state(pool.clone());
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-api-key", platform.parse().expect("header"));
    let ctx = authenticate_request(
        &headers,
        TENANT,
        &state.config.jwt_secret,
        &pool,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    .expect("the platform key acts for the tenant named");
    assert!(ctx.api_key_is_platform);
    assert_eq!(ctx.tenant_id, TENANT);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_configured_list_bounds_the_platform_service_key_too(pool: PgPool) {
    use brutor_gateway_core::services::auth::{
        authenticate_request, configure_global_key_allowed_targets,
    };
    assert!(configure_global_key_allowed_targets(Some(vec![
        "some-third-tenant".to_string()
    ])));
    seed_world(&pool).await;
    let platform = common::seed_key(&pool, OTHER_TENANT, None, true, "matrix-platform").await;
    let state = common::build_test_state(pool.clone());
    let mut headers = axum::http::HeaderMap::new();
    headers.insert("x-api-key", platform.parse().expect("header"));
    for (tenant, stands) in [(TENANT, false), (OTHER_TENANT, true)] {
        let got = authenticate_request(
            &headers,
            tenant,
            &state.config.jwt_secret,
            &pool,
            &state.credential_verifiers,
            None,
            true,
        )
        .await;
        assert_eq!(got.is_ok(), stands, "tenant {tenant}");
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The tenant-resolution matrix.
//!
//! The sibling of the credential × surface matrix
//! (`credential_surface_matrix.rs`), over the same route table: every route
//! of the assembled gateway is asked with every way a request can name a
//! tenant, and what the tenant layer (`middleware::tenant`) did is compared
//! with `tests/fixtures/tenant_resolution_matrix/`. The fixture was recorded
//! on the code as it stood before the layer's path rules became a registry
//! (`services::tenant_paths`), when the layer decided from three hard-coded
//! prefix lists. It is the proof that the full assembly, with the portal's
//! prefixes registered by the builder, resolves tenants as the gateway did
//! before. What changed on purpose is listed in [`TIGHTENED`].
//!
//! ## What a cell is
//!
//! `"<status> <who answered>"`. The layer's own refusals are recognised by
//! their bodies: `layer:no_tenant` (401), `layer:tenant_not_found` (403),
//! `layer:blocked` (403, with the error code and message: `tenant_suspended`
//! for a suspended tenant, `trial_expired` for one whose trial is over where
//! the composition gates trials — the Enterprise Edition; a Community
//! assembly ignores the trial columns. The message of a trial that is over
//! is the edition's (`composition::trial_expired_message`), and the
//! recording names it [`TRIAL_MESSAGE`]), `layer:status_unavailable` (503),
//! `layer:key_lookup_failed` (500). Anything else is `inner`: the request
//! passed the layer and the route (or the router's 404/405) answered.
//!
//! ## The cells
//!
//! [`CELLS`]: no tenant named at all; `X-Tenant-ID` of a tenant that is
//! valid, unknown, suspended, on an expired trial, on a running trial; a
//! tenant named only by an API key (valid, bearer form, unknown, of a
//! suspended tenant); a tenant in a verified token (`X-Gateway-Authorization`)
//! alone, and disagreeing with the header in each direction; an expired
//! token beside a header; a token in `Authorization` only (the layer does
//! not read it); a header beside a key of another tenant. And, with the
//! table gone, a tenant whose status cannot be read and a key that cannot
//! be looked up.
//!
//! ## Paths no route serves
//!
//! [`UNROUTED`] are asked too. Before the registry a path outside the three
//! lists was "no tenant required", routed or not; with it, a path nobody
//! registered gets the most restrictive rule.
//!
//! Re-record (only on purpose, and say why in the change):
//! `TENANT_MATRIX_RECORD=1 cargo nextest run --features db-tests -E 'test(tenant_resolution_matrix)'`

use std::collections::BTreeMap;

use axum::Router;
use axum::http::Request;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;
use crate::credential_surface_matrix::{
    self as surfaces, ROUTES, Route, TENANT as VALID, request_with,
};

pub(crate) const SUSPENDED: &str = "tenant-tr-suspended";
pub(crate) const TRIAL_OVER: &str = "tenant-tr-trial-over";
pub(crate) const TRIAL_RUNNING: &str = "tenant-tr-trial";
/// No such tenant.
pub(crate) const GHOST: &str = "tenant-tr-ghost";
pub(crate) const SUSPENSION_REASON: &str = "Suspended for the matrix";

pub(crate) const FIXTURE_DIR: &str = "tests/fixtures/tenant_resolution_matrix";

/// How a cell names the message of a trial that is over: the edition's own
/// message (`composition::trial_expired_message`), which the Community tree
/// does not carry. A layer block with any other message is recorded as it
/// is, so a wrong message differs from the recording.
pub(crate) const TRIAL_MESSAGE: &str = "<the edition's trial message>";
pub(crate) const SHARDS: usize = 8;

/// The tenant a cell's request resolves to, by the layer's order of
/// sources (verified token, then header, then API key).
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Resolves {
    Nothing,
    Valid,
    Ghost,
    Suspended,
    TrialOver,
    TrialRunning,
}

pub(crate) struct Cell {
    name: &'static str,
    resolves: Resolves,
}

pub(crate) const fn cell(name: &'static str, resolves: Resolves) -> Cell {
    Cell { name, resolves }
}

pub(crate) const CELLS: &[Cell] = &[
    cell("none", Resolves::Nothing),
    cell("header", Resolves::Valid),
    cell("header_unknown", Resolves::Ghost),
    cell("header_suspended", Resolves::Suspended),
    cell("header_trial_expired", Resolves::TrialOver),
    cell("header_trial_running", Resolves::TrialRunning),
    cell("api_key", Resolves::Valid),
    cell("api_key_bearer", Resolves::Valid),
    cell("api_key_unknown", Resolves::Nothing),
    cell("api_key_of_suspended", Resolves::Suspended),
    cell("token", Resolves::Valid),
    cell("token_valid_header_suspended", Resolves::Valid),
    cell("token_suspended_header_valid", Resolves::Suspended),
    cell("token_unknown_header_valid", Resolves::Ghost),
    cell("token_expired_header_valid", Resolves::Valid),
    cell("token_in_authorization_only", Resolves::Nothing),
    cell("header_suspended_key_valid", Resolves::Suspended),
    // Workload identity: an agent's own token in
    // `Authorization`. The layer does not read it: the tenant is the
    // header's, and without a header no tenant is named.
    cell("workload_token_header_valid", Resolves::Valid),
    cell("workload_token_only", Resolves::Nothing),
    cell("workload_token_header_suspended", Resolves::Suspended),
    cell("workload_token_header_unknown", Resolves::Ghost),
];

/// Asked after the table is renamed away.
pub(crate) const TENANTS_UNREADABLE: &str = "header@tenants_unreadable";
pub(crate) const KEYS_UNREADABLE: &str = "api_key@api_keys_unreadable";

pub(crate) struct World {
    api_key: String,
    suspended_api_key: String,
    /// Kept alive: the workload cells' identity provider.
    _workload: surfaces::WorkloadWorld,
}

pub(crate) fn token(tenant: &str, expires_in: i64) -> String {
    let now = common::db_now().timestamp();
    surfaces::sign(&json!({
        "typ": "gateway", "user_id": "svc-1", "username": "service", "tenant_id": tenant,
        "exp": now + expires_in, "iat": now - 7200,
    }))
}

pub(crate) fn headers(cell: &str, world: &World) -> Vec<(&'static str, String)> {
    let tenant = |id: &str| ("x-tenant-id", id.to_string());
    let gateway = |t: String| ("x-gateway-authorization", format!("Bearer {t}"));
    // The valid token of the surfaces matrix's agent (tenant `VALID`).
    let workload = || {
        (
            "authorization",
            format!("Bearer {}", surfaces::WorkloadWorld::valid()),
        )
    };
    match cell {
        "none" => vec![],
        "header" => vec![tenant(VALID)],
        "header_unknown" => vec![tenant(GHOST)],
        "header_suspended" => vec![tenant(SUSPENDED)],
        "header_trial_expired" => vec![tenant(TRIAL_OVER)],
        "header_trial_running" => vec![tenant(TRIAL_RUNNING)],
        "api_key" => vec![("x-api-key", world.api_key.clone())],
        "api_key_bearer" => vec![("authorization", format!("Bearer {}", world.api_key))],
        "api_key_unknown" => vec![(
            "x-api-key",
            format!("sk_brutor_api_test_{}", uuid::Uuid::new_v4().simple()),
        )],
        "api_key_of_suspended" => vec![("x-api-key", world.suspended_api_key.clone())],
        "token" => vec![gateway(token(VALID, 3600))],
        "token_valid_header_suspended" => vec![gateway(token(VALID, 3600)), tenant(SUSPENDED)],
        "token_suspended_header_valid" => vec![gateway(token(SUSPENDED, 3600)), tenant(VALID)],
        "token_unknown_header_valid" => vec![gateway(token(GHOST, 3600)), tenant(VALID)],
        "token_expired_header_valid" => vec![gateway(token(SUSPENDED, -3600)), tenant(VALID)],
        "token_in_authorization_only" => {
            vec![("authorization", format!("Bearer {}", token(VALID, 3600)))]
        }
        "header_suspended_key_valid" => {
            vec![tenant(SUSPENDED), ("x-api-key", world.api_key.clone())]
        }
        "workload_token_header_valid" => vec![workload(), tenant(VALID)],
        "workload_token_only" => vec![workload()],
        "workload_token_header_suspended" => vec![workload(), tenant(SUSPENDED)],
        "workload_token_header_unknown" => vec![workload(), tenant(GHOST)],
        TENANTS_UNREADABLE => vec![tenant(VALID)],
        KEYS_UNREADABLE => vec![(
            "x-api-key",
            format!("sk_brutor_api_test_{}", uuid::Uuid::new_v4().simple()),
        )],
        other => panic!("no such cell: {other}"),
    }
}

pub(crate) async fn seed_world(pool: &PgPool) -> World {
    let surfaces_world = surfaces::seed_world(pool).await;
    for tenant in [SUSPENDED, TRIAL_OVER, TRIAL_RUNNING] {
        common::seed_tenant(pool, tenant).await;
    }
    sqlx::query("UPDATE tenants SET suspended = true, suspension_reason = $2 WHERE id = $1")
        .bind(SUSPENDED)
        .bind(SUSPENSION_REASON)
        .execute(pool)
        .await
        .expect("suspend");
    sqlx::query(
        "UPDATE tenants SET is_trial = true, trial_ends_at = NOW() - interval '1 day' WHERE id = $1",
    )
    .bind(TRIAL_OVER)
    .execute(pool)
    .await
    .expect("trial over");
    sqlx::query(
        "UPDATE tenants SET is_trial = true, trial_ends_at = NOW() + interval '7 days' WHERE id = $1",
    )
    .bind(TRIAL_RUNNING)
    .execute(pool)
    .await
    .expect("trial running");
    common::seed_resource_group(pool, "rg-tr-suspended", SUSPENDED).await;
    let suspended_api_key =
        common::seed_api_key(pool, SUSPENDED, "rg-tr-suspended", None, "tr-suspended").await;
    World {
        api_key: surfaces_world.api_key,
        suspended_api_key,
        _workload: surfaces_world.workload,
    }
}

// ---------------------------------------------------------------------------
// The surfaces
// ---------------------------------------------------------------------------

/// The API document and its viewer: routes of the application that no
/// route module declares (`openapi::docs_router`).
pub(crate) const DOCS: &[&str] = &["/docs", "/docs/", "/api-docs/openapi.json"];

/// Paths no route serves: a prefix nobody knows, and near misses of the
/// prefixes the layer does know (a prefix is a prefix, not a word).
pub(crate) const UNROUTED: &[&str] = &[
    "/",
    "/favicon.ico",
    "/v1",
    "/v1/unknown",
    "/v1/ext/reports",
    "/v2/anything",
    "/v1/proxy",
    "/v1/proxy/nothing-here",
    "/v1/messages-other",
    "/v1/approvals-report",
    "/v1/approvals/",
    "/v1/runs",
    "/v1/runs/feedback",
    "/v1/runs/1/feedback/more",
    "/v1beta",
    "/v1beta/nothing",
    "/v1/portal",
    "/v1/portal/nothing-here",
    "/v1/portalx/y",
    "/v1/public",
    "/v1/public/nothing-here",
    "/v1/publicity",
    "/v1/auth/info",
    "/a2a",
    "/.well-known/nothing",
    "/health/deep",
    "/v1/proxy/oauth",
    "/v1/proxy/oauth/nothing-here",
];

pub(crate) struct Surface {
    pub(crate) name: String,
    pub(crate) uri: String,
    pub(crate) route: Option<&'static Route>,
}

pub(crate) fn surfaces_of(shard: usize) -> Vec<Surface> {
    let mut all: Vec<Surface> = ROUTES
        .iter()
        .map(|route| Surface {
            name: route.name(),
            uri: route.uri(),
            route: Some(route),
        })
        .collect();
    for path in DOCS {
        all.push(Surface {
            name: format!("GET {path}"),
            uri: (*path).to_string(),
            route: None,
        });
    }
    for path in UNROUTED {
        all.push(Surface {
            name: format!("GET (no route) {path}"),
            uri: (*path).to_string(),
            route: None,
        });
    }
    all.into_iter()
        .enumerate()
        .filter(|(i, _)| i % SHARDS == shard)
        .map(|(_, s)| s)
        .collect()
}

pub(crate) fn request(
    surface: &Surface,
    headers: &[(&'static str, String)],
) -> Request<axum::body::Body> {
    match surface.route {
        Some(route) => request_with(route, headers),
        None => {
            let mut builder = Request::builder().method("GET").uri(&surface.uri);
            for (name, value) in headers {
                builder = builder.header(*name, value);
            }
            let mut request = builder.body(axum::body::Body::empty()).expect("request");
            let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().expect("peer");
            request
                .extensions_mut()
                .insert(axum::extract::ConnectInfo(peer));
            request
        }
    }
}

// ---------------------------------------------------------------------------
// Asking
// ---------------------------------------------------------------------------

pub(crate) type Matrix = BTreeMap<String, BTreeMap<String, String>>;

pub(crate) const NO_TENANT: &str = "401 layer:no_tenant";
pub(crate) const TENANT_NOT_FOUND: &str = "403 layer:tenant_not_found";

pub(crate) fn blocked(code: &str, message: &str) -> String {
    format!("403 layer:blocked {code}: {message}")
}

/// Who answered, from the status and the body.
pub(crate) fn outcome(status: u16, body: &[u8]) -> String {
    let text = String::from_utf8_lossy(body);
    let json: Option<Value> = serde_json::from_slice(body).ok();
    let error = json.as_ref().and_then(|j| j.get("error"));
    // The layer's bodies, byte for byte: some routes refuse in similar words.
    if status == 401
        && text
            == r#"{"error":{"message":"Missing or invalid credentials. Provide X-Tenant-ID header or a valid bearer token / API key."}}"#
    {
        return NO_TENANT.to_string();
    }
    if status == 403 && text == r#"{"error":"Tenant not found."}"# {
        return TENANT_NOT_FOUND.to_string();
    }
    if status == 503
        && text == r#"{"error":"Tenant status temporarily unavailable. Retry shortly."}"#
    {
        return "503 layer:status_unavailable".to_string();
    }
    if status == 500 && text == r#"{"error":"Auth lookup failed"}"# {
        return "500 layer:key_lookup_failed".to_string();
    }
    let message = json
        .as_ref()
        .and_then(|j| j.get("message"))
        .and_then(|m| m.as_str());
    let fields = json.as_ref().and_then(|j| j.as_object()).map(|o| o.len());
    // The layer's block: its two-field body, with the code it gives — a
    // suspension, or (where the composition gates trials) a trial that is
    // over.
    if let (403, Some(code @ ("tenant_suspended" | "trial_expired")), Some(message), Some(2)) =
        (status, error.and_then(|e| e.as_str()), message, fields)
    {
        let edition_trial_message = crate::composition::trial_expired_message();
        if code == "trial_expired" && edition_trial_message == Some(message) {
            return blocked(code, TRIAL_MESSAGE);
        }
        return blocked(code, message);
    }
    format!("{status} inner")
}

pub(crate) async fn ask(
    make_app: &dyn Fn(PgPool) -> Router,
    pool: &PgPool,
    world: &World,
    cells: &[&str],
    shard: usize,
    into: &mut Matrix,
) {
    for surface in surfaces_of(shard) {
        // One application per surface: the layer caches a tenant's status
        // and a key's tenant for seconds.
        let app: Router = make_app(pool.clone());
        let row = into.entry(surface.name.clone()).or_default();
        for name in cells {
            let response = tokio::time::timeout(
                std::time::Duration::from_secs(20),
                app.clone()
                    .oneshot(request(&surface, &headers(name, world))),
            )
            .await
            .unwrap_or_else(|_| panic!("{} with {name} did not answer", surface.name))
            .expect("router");
            let status = response.status().as_u16();
            let body = tokio::time::timeout(
                std::time::Duration::from_secs(5),
                axum::body::to_bytes(response.into_body(), 16 * 1024),
            )
            .await
            .ok()
            .and_then(Result::ok)
            .unwrap_or_default();
            let cell = outcome(status, &body);
            if std::env::var("TENANT_MATRIX_SHOW").is_ok_and(|w| surface.name.contains(&w)) {
                eprintln!(
                    "{} {name}: {cell} {}",
                    surface.name,
                    String::from_utf8_lossy(&body)
                );
            }
            row.insert((*name).to_string(), cell);
        }
    }
}

/// Connections that have run nothing yet. A connection that already
/// prepared the layer's statement keeps its plan when the table is renamed
/// (the plan holds the table, not its name) and would go on reading it; so
/// the "table gone" cells are asked over connections opened after the
/// rename. Without this the cell depends on which pooled connection a
/// request happens to get, and the matrix fails now and then under load.
pub(crate) async fn fresh_pool(pool: &PgPool) -> PgPool {
    sqlx::postgres::PgPoolOptions::new()
        .max_connections(4)
        .connect_with((*pool.connect_options()).clone())
        .await
        .expect("a pool of new connections")
}

pub(crate) async fn record(
    pool: &PgPool,
    make_app: &dyn Fn(PgPool) -> Router,
    shard: usize,
) -> Matrix {
    let world = seed_world(pool).await;
    let names: Vec<&str> = CELLS.iter().map(|c| c.name).collect();
    let mut matrix = Matrix::new();
    ask(make_app, pool, &world, &names, shard, &mut matrix).await;

    // The key cannot be looked up; then the tenant's status cannot be read.
    sqlx::query("ALTER TABLE api_keys RENAME TO api_keys_gone")
        .execute(pool)
        .await
        .expect("rename api_keys");
    let after = fresh_pool(pool).await;
    ask(
        make_app,
        &after,
        &world,
        &[KEYS_UNREADABLE],
        shard,
        &mut matrix,
    )
    .await;
    after.close().await;
    sqlx::query("ALTER TABLE tenants RENAME TO tenants_gone")
        .execute(pool)
        .await
        .expect("rename tenants");
    let after = fresh_pool(pool).await;
    ask(
        make_app,
        &after,
        &world,
        &[TENANTS_UNREADABLE],
        shard,
        &mut matrix,
    )
    .await;
    after.close().await;
    matrix
}

pub(crate) fn fixture_path(shard: usize) -> std::path::PathBuf {
    crate::common::community_root()
        .join(FIXTURE_DIR)
        .join(format!("part_{shard}.json"))
}

pub(crate) fn read_fixture_part(shard: usize) -> Matrix {
    let text = std::fs::read_to_string(fixture_path(shard))
        .expect("the recorded matrix (record it with TENANT_MATRIX_RECORD=1)");
    serde_json::from_str(&text).expect("fixture JSON")
}

pub(crate) fn read_fixture() -> Matrix {
    (0..SHARDS).flat_map(read_fixture_part).collect()
}

pub(crate) fn differences(expected: &Matrix, actual: &Matrix) -> Vec<String> {
    let mut out = Vec::new();
    for (surface, row) in expected {
        let Some(got) = actual.get(surface) else {
            out.push(format!("{surface}: not asked"));
            continue;
        };
        for (cell, outcome) in row {
            match got.get(cell) {
                Some(o) if o == outcome => {}
                Some(o) => out.push(format!(
                    "{surface} with {cell}: recorded {outcome:?}, now {o:?}"
                )),
                None => out.push(format!("{surface} with {cell}: not asked")),
            }
        }
        for cell in got.keys() {
            if !row.contains_key(cell) {
                out.push(format!("{surface} with {cell}: not in the recording"));
            }
        }
    }
    for surface in actual.keys() {
        if !expected.contains_key(surface) {
            out.push(format!("{surface}: not in the recording"));
        }
    }
    out
}

// ---------------------------------------------------------------------------
// The most restrictive rule
// ---------------------------------------------------------------------------

/// Whether the composition refuses a tenant whose trial is over.
///
/// The trial is the Enterprise Edition's. Its gate is
/// installed by the `multi_tenant` extension; a Community assembly reads no
/// trial column, so a tenant whose trial is over is a tenant like any other
/// there.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Trials {
    /// The full (Enterprise) assembly: the recording's.
    Gated,
    /// A Community assembly.
    Ignored,
}

/// What the layer answers under its most restrictive rule, or `None` when
/// the request passes it: a tenant is required (401 without one), an
/// unknown tenant is refused (403), a suspended tenant is blocked (403,
/// `tenant_suspended`) and, where trials are gated, so is one whose trial
/// is over (403, `trial_expired`); a status that cannot be read is 503.
pub(crate) fn most_restrictive(cell: &str, trials: Trials) -> Option<String> {
    if cell == TENANTS_UNREADABLE {
        return Some("503 layer:status_unavailable".to_string());
    }
    if cell == KEYS_UNREADABLE {
        return Some("500 layer:key_lookup_failed".to_string());
    }
    let resolves = CELLS
        .iter()
        .find(|c| c.name == cell)
        .unwrap_or_else(|| panic!("no such cell {cell}"))
        .resolves;
    match resolves {
        Resolves::Nothing => Some(NO_TENANT.to_string()),
        Resolves::Ghost => Some(TENANT_NOT_FOUND.to_string()),
        // A suspension is `tenant_suspended`; it was `trial_expired`.
        Resolves::Suspended => Some(blocked("tenant_suspended", SUSPENSION_REASON)),
        Resolves::TrialOver if trials == Trials::Gated => {
            Some(blocked("trial_expired", TRIAL_MESSAGE))
        }
        Resolves::Valid | Resolves::TrialRunning | Resolves::TrialOver => None,
    }
}

/// Paths no route serves and no rule was registered for: before the
/// registry they were "no tenant required" (the router's 404 answered
/// whatever the tenant); now they get the most restrictive rule. Tightened
/// on purpose: a path nobody declared is never "no tenant required".
/// Each is listed with the list it fell outside of.
pub(crate) const TIGHTENED: &[(&str, &str)] = &[
    ("/", "no rule: not a path of the gateway"),
    ("/favicon.ico", "no rule: not a path of the gateway"),
    ("/v1", "no rule: not a path of the gateway"),
    ("/v1/unknown", "no rule: an unknown prefix"),
    (
        "/v1/ext/reports",
        "no rule: where an extension would mount without registering",
    ),
    ("/v2/anything", "no rule: an unknown prefix"),
    ("/v1/proxy", "no rule: the proxy prefix is `/v1/proxy/`"),
    (
        "/v1/approvals-report",
        "no rule: the approvals prefix is `/v1/approvals/`",
    ),
    ("/v1/runs", "no rule: the runs prefix is `/v1/runs/`"),
    ("/v1beta", "no rule: the Gemini prefix is `/v1beta/`"),
    ("/v1/portal", "no rule: the portal prefix is `/v1/portal/`"),
    (
        "/v1/portalx/y",
        "no rule: the portal prefix is `/v1/portal/`",
    ),
    ("/v1/public", "no rule: the public prefix is `/v1/public/`"),
    (
        "/v1/publicity",
        "no rule: the public prefix is `/v1/public/`",
    ),
    ("/a2a", "no rule: the A2A prefix is `/a2a/`"),
    (
        "/.well-known/nothing",
        "no rule: not a document the gateway publishes",
    ),
    ("/health/deep", "no rule: `/health` is an exact path"),
];

/// The recording with the intended tightenings applied, for a composition
/// that gates trials (the recording's: the full assembly) or one that does
/// not (a Community assembly).
pub(crate) fn expected_with_tightenings(mut recorded: Matrix, trials: Trials) -> Matrix {
    for (surface, row) in recorded.iter_mut() {
        let Some(path) = surface.strip_prefix("GET (no route) ") else {
            continue;
        };
        if !TIGHTENED.iter().any(|(p, _)| *p == path) {
            continue;
        }
        for (cell, outcome) in row.iter_mut() {
            if let Some(refusal) = most_restrictive(cell, trials) {
                *outcome = refusal;
            } else if cell == "header_trial_expired" && trials == Trials::Ignored {
                // Not refused by the layer: the router's 404, as for a
                // tenant on a running trial.
                *outcome = "404 inner".to_string();
            }
        }
    }
    recorded
}

// ---------------------------------------------------------------------------
// What the recording itself says (no database)
// ---------------------------------------------------------------------------

/// The path a surface is asked at (its name shows the route's template; a
/// router's own `/` is asked at the mount itself, without the slash).
pub(crate) fn path_of(surface: &str) -> String {
    static PATHS: std::sync::OnceLock<BTreeMap<String, String>> = std::sync::OnceLock::new();
    let paths = PATHS.get_or_init(|| {
        (0..SHARDS)
            .flat_map(surfaces_of)
            .map(|s| {
                let path = s.uri.split('?').next().unwrap_or("").to_string();
                (s.name, path)
            })
            .collect()
    });
    paths
        .get(surface)
        .unwrap_or_else(|| panic!("{surface} is not a surface of the matrix"))
        .clone()
}

/// The layer's classes as the recording shows them, so that the fixture is
/// read by a person as well as compared by a machine: which surfaces the
/// layer never refuses, which need a tenant, which are blocked for a
/// suspended tenant.
#[test]
fn the_recording_shows_the_layers_classes() {
    let matrix = read_fixture();
    assert!(matrix.len() > 150, "{} surfaces", matrix.len());
    for (surface, row) in &matrix {
        let path = path_of(surface);
        let path = path.as_str();
        assert_eq!(row.len(), CELLS.len() + 2, "{surface}");
        let refuses_no_tenant = row["none"] == NO_TENANT;
        let blocks_suspended = row["header_suspended"].starts_with("403 layer:blocked");
        let refuses_unknown = row["header_unknown"] == TENANT_NOT_FOUND;
        // Before the registry: the three lists.
        let open = ["/health", "/healthz", "/v1/version", "/v1/auth/info"].contains(&path)
            || [
                "/v1/proxy/oauth/",
                "/.well-known/agent-card/",
                "/.well-known/a2a-jwks/",
                "/a2a/",
                "/v1/public/",
                // Added with the filesystem media store (decision 11): a
                // signed download link is its own credential and names its
                // tenant.
                "/v1/media/",
            ]
            .iter()
            .any(|p| path.starts_with(p));
        let required = path.starts_with("/v1/proxy/") || path.starts_with("/v1/messages");
        let runtime = required
            || path.starts_with("/v1/portal/")
            || path.starts_with("/v1/approvals/")
            || (path.starts_with("/v1/runs/") && path.ends_with("/feedback"))
            || path.starts_with("/v1beta/");
        assert_eq!(refuses_no_tenant, !open && required, "{surface}: none");
        assert_eq!(blocks_suspended, !open && runtime, "{surface}: suspended");
        assert_eq!(refuses_unknown, !open, "{surface}: unknown tenant");
        // The same tenant, however it is named, is treated the same.
        assert_eq!(
            row["header_suspended"].starts_with("403 layer:blocked"),
            row["api_key_of_suspended"].starts_with("403 layer:blocked"),
            "{surface}"
        );
        // A verified token's tenant wins over the header's, both ways.
        assert_eq!(
            row["token_suspended_header_valid"].starts_with("403 layer:blocked"),
            blocks_suspended,
            "{surface}"
        );
        assert!(
            !row["token_valid_header_suspended"].starts_with("403 layer:blocked"),
            "{surface}: the header overrode a verified token"
        );
        // A trial that is over blocks where a suspension does, with its own
        // message; a running trial never does.
        assert_eq!(
            row["header_trial_expired"] == blocked("trial_expired", TRIAL_MESSAGE),
            blocks_suspended,
            "{surface}"
        );
        if blocks_suspended {
            // A suspension is `tenant_suspended`: the
            // recording said `trial_expired` for it.
            for cell in [
                "header_suspended",
                "api_key_of_suspended",
                "header_suspended_key_valid",
                "token_suspended_header_valid",
            ] {
                assert_eq!(
                    row[cell],
                    blocked("tenant_suspended", SUSPENSION_REASON),
                    "{surface}: {cell}"
                );
            }
        }
        assert!(!row["header_trial_running"].contains("layer:"), "{surface}");
        // What cannot be read is never "no tenant" or "not suspended".
        if !open {
            assert_eq!(
                row[TENANTS_UNREADABLE], "503 layer:status_unavailable",
                "{surface}"
            );
            assert_eq!(
                row[KEYS_UNREADABLE], "500 layer:key_lookup_failed",
                "{surface}"
            );
        }
    }
}
// ---------------------------------------------------------------------------
// The registry
// ---------------------------------------------------------------------------

/// The prefixes an extension mounts under a Community prefix (batch
/// jobs; the knowledge base's connector icons): a Community assembly serves no route under them (the mounted-route
/// recordings differ by these and the portal's, `batch_extension.rs`,
/// `portal_extension.rs`).
pub(crate) const MOUNTED_BY_AN_EXTENSION: &[&str] =
    &["/v1/proxy/batch", "/v1/assets/connector-icons"];

pub(crate) fn mounted_by_an_extension(path: &str) -> bool {
    MOUNTED_BY_AN_EXTENSION
        .iter()
        .any(|prefix| path == *prefix || path.starts_with(&format!("{prefix}/")))
}

/// Is this a path of the Enterprise portal (its API, and the public theme
/// its sign-in screen reads)? The Portal extension mounts them and
/// registers their rules; a Community assembly has neither.
pub(crate) fn portal_path(path: &str) -> bool {
    path.starts_with("/v1/portal/") || path.starts_with("/v1/public/")
}

/// What a Community assembly answers: the recording, except on the portal's
/// paths. Nothing registered them, so they are not special: they get the
/// most restrictive rule like any path nobody declared; and no route is
/// mounted there, so a request that passes the layer is answered 404 by
/// the router.
///
/// And except under a prefix an extension mounts
/// (batch jobs). The layer's rule for such a path is unchanged, since
/// `/v1/proxy/` is a Community prefix, so every refusal by the layer is as
/// recorded; a request the layer lets through finds no route there and is
/// answered 404 by the router, where the full assembly's handler answered.
pub(crate) fn expected_of_a_community_assembly(recorded: Matrix) -> Matrix {
    // A Community assembly ignores the trial columns: a
    // tenant whose trial is over is answered as one whose trial runs (the
    // two tenants of the matrix are alike in everything else). On every
    // surface, before the portal's and the extensions' paths are derived.
    let mut recorded = recorded;
    for row in recorded.values_mut() {
        let running = row["header_trial_running"].clone();
        row.insert("header_trial_expired".to_string(), running);
    }
    let mut expected = expected_with_tightenings(recorded, Trials::Ignored);
    for (surface, row) in expected.iter_mut() {
        if mounted_by_an_extension(&path_of(surface)) {
            for outcome in row.values_mut() {
                if outcome.ends_with(" inner") {
                    *outcome = "404 inner".to_string();
                }
            }
            continue;
        }
        if !portal_path(&path_of(surface)) {
            continue;
        }
        for (cell, outcome) in row.iter_mut() {
            if let Some(refusal) = most_restrictive(cell, Trials::Ignored) {
                *outcome = refusal;
            } else {
                *outcome = "404 inner".to_string();
            }
        }
    }
    expected
}

pub(crate) async fn the_community_shard(pool: PgPool, shard: usize) {
    let matrix = record(&pool, &common::community_app, shard).await;
    let recorded = read_fixture_part(shard);

    // The rule in its own words first: on a Community assembly a portal
    // path without a tenant is refused by the layer, an unknown tenant is
    // refused, a suspended one is blocked — also where the full assembly
    // skips tenant resolution (the public theme).
    let mut special = Vec::new();
    for (surface, row) in &matrix {
        if !portal_path(&path_of(surface)) {
            continue;
        }
        for (cell, outcome) in row {
            if let Some(refusal) = most_restrictive(cell, Trials::Ignored) {
                if *outcome != refusal {
                    special.push(format!("{surface} with {cell}: {outcome}"));
                }
            } else if outcome.contains("layer:") {
                special.push(format!("{surface} with {cell}: {outcome}"));
            }
        }
    }
    assert!(
        special.is_empty(),
        "a Community assembly treats a portal path specially:\n{}",
        special.join("\n")
    );
    let diff = differences(&expected_of_a_community_assembly(recorded), &matrix);
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
            the_community_shard(pool, $shard).await;
        }
    )+};
}

community_shards!(
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_0 = 0,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_1 = 1,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_2 = 2,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_3 = 3,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_4 = 4,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_5 = 5,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_6 = 6,
    a_community_assembly_gives_portal_paths_the_most_restrictive_rule_7 = 7,
);

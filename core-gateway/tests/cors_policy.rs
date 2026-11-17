// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What a browser on another origin may read, and where
//! `/metrics` lives.
//!
//! Behaviour tests drive `middleware::cors::layer` on a small router (the
//! real router is assembled in `main.rs`); the guards scan `src/` so the
//! permissive layer and a public `/metrics` route cannot come back.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

use axum::Router;
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use axum::routing::get;
use tower::ServiceExt;

use brutor_gateway_core::middleware::cors;

const EVIL: &str = "https://evil.example";
const PORTAL: &str = "https://portal.example.com";

fn app() -> Router {
    let ok = || async { "ok" };
    Router::new()
        .route("/v1/portal/models", get(ok))
        .route("/health", get(ok))
        .route("/v1/proxy/mcp/srv", get(ok).post(ok))
        .route("/.well-known/oauth-protected-resource", get(ok))
        .layer(cors::layer(&cors::parse_origins(Some(PORTAL))))
}

async fn preflight(app: Router, path: &str, origin: &str) -> axum::http::Response<Body> {
    app.oneshot(
        Request::builder()
            .method(Method::OPTIONS)
            .uri(path)
            .header("origin", origin)
            .header("access-control-request-method", "GET")
            .header("access-control-request-headers", "authorization")
            .body(Body::empty())
            .unwrap(),
    )
    .await
    .unwrap()
}

async fn simple_get(app: Router, path: &str, origin: &str) -> axum::http::Response<Body> {
    app.oneshot(
        Request::builder()
            .uri(path)
            .header("origin", origin)
            .body(Body::empty())
            .unwrap(),
    )
    .await
    .unwrap()
}

fn acao(r: &axum::http::Response<Body>) -> Option<String> {
    r.headers()
        .get("access-control-allow-origin")
        .map(|v| v.to_str().unwrap().to_string())
}

#[tokio::test]
async fn another_origin_cannot_read_the_portal_api_or_health() {
    for path in ["/v1/portal/models", "/health"] {
        assert_eq!(
            acao(&preflight(app(), path, EVIL).await),
            None,
            "preflight {path}"
        );
        let r = simple_get(app(), path, EVIL).await;
        assert_eq!(
            r.status(),
            StatusCode::OK,
            "the request itself is not blocked server-side"
        );
        assert_eq!(
            acao(&r),
            None,
            "but the browser is not allowed to read {path}"
        );
    }
}

#[tokio::test]
async fn a_configured_origin_can() {
    let r = preflight(app(), "/v1/portal/models", PORTAL).await;
    assert_eq!(acao(&r).as_deref(), Some(PORTAL));
    let vary = r
        .headers()
        .get_all("vary")
        .iter()
        .map(|v| v.to_str().unwrap().to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join(",");
    assert!(
        vary.contains("origin"),
        "a per-origin answer must vary on Origin: {vary}"
    );
}

#[tokio::test]
async fn protocol_routes_stay_open_to_any_origin() {
    for path in ["/v1/proxy/mcp/srv", "/.well-known/oauth-protected-resource"] {
        let r = preflight(app(), path, EVIL).await;
        assert_eq!(acao(&r).as_deref(), Some(EVIL), "{path}");
    }
}

#[tokio::test]
async fn no_route_allows_credentials() {
    for path in ["/v1/portal/models", "/v1/proxy/mcp/srv", "/health"] {
        let r = preflight(app(), path, PORTAL).await;
        assert!(
            r.headers()
                .get("access-control-allow-credentials")
                .is_none(),
            "{path}"
        );
    }
}

/// There is no switch that opens every route to every origin: an origin
/// not listed reads no route that is not a protocol route.
#[tokio::test]
async fn an_unlisted_origin_reads_no_console_route() {
    let r = preflight(app(), "/v1/portal/models", EVIL).await;
    assert_eq!(acao(&r), None);
}

// ---------------------------------------------------------------- guards

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    for e in fs::read_dir(dir).unwrap().flatten() {
        let p = e.path();
        if p.is_dir() {
            rust_files(&p, out);
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            out.push(p);
        }
    }
}

/// `(path, line)` of every `needle` in `src/`, outside `allowed` files.
fn occurrences(needles: &[&str], allowed: &[&str]) -> Vec<String> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let mut found = Vec::new();
    for f in files {
        let path = f.to_string_lossy().replace('\\', "/");
        if allowed.contains(&path.as_str()) {
            continue;
        }
        for (i, line) in fs::read_to_string(&f).unwrap().lines().enumerate() {
            let code = line.split("//").next().unwrap_or("");
            if needles.iter().any(|n| code.contains(n)) {
                found.push(format!("{path}:{}: {}", i + 1, line.trim()));
            }
        }
    }
    found
}

/// No permissive CORS anywhere in product code.
#[test]
fn nothing_installs_a_permissive_cors_layer() {
    let found = occurrences(
        &["CorsLayer::permissive", "CorsLayer::very_permissive"],
        &[],
    );
    assert!(
        found.is_empty(),
        "use middleware::cors::layer:\n{}",
        found.join("\n")
    );
}

/// `"/metrics"` passed to a `.route(` call (the call may span lines).
fn routes_metrics(src: &str) -> bool {
    src.match_indices("\"/metrics\"").any(|(at, _)| {
        let before = src[..at].trim_end();
        before.ends_with(".route(")
    })
}

/// `/metrics` is routed only by the metrics listener's router.
#[test]
fn metrics_are_routed_only_on_their_own_listener() {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    // The Enterprise workspace's sources too, where this repository carries
    // them (the source-scan guards are root-aware).
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    let routed: Vec<String> = files
        .iter()
        .map(|f| f.to_string_lossy().replace('\\', "/"))
        .filter(|p| p != "src/routes/metrics.rs")
        .filter(|p| routes_metrics(&fs::read_to_string(p).unwrap()))
        .collect();
    assert!(
        routed.is_empty(),
        "/metrics must not be on the public router: {routed:?}"
    );
}

/// The scanners find what they look for.
#[test]
fn the_guards_see_the_shapes_they_forbid() {
    assert!(routes_metrics(
        &fs::read_to_string("src/routes/metrics.rs").unwrap()
    ));
    // The shape main.rs must not have.
    assert!(routes_metrics(
        ".route(\n            \"/metrics\",\n            axum::routing::get(h),\n        )"
    ));
    let line = "        .layer(CorsLayer::permissive())";
    assert!(
        line.split("//")
            .next()
            .unwrap()
            .contains("CorsLayer::permissive")
    );
}

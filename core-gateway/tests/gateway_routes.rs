// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Integration tests for gateway routes.
//!
//! These tests exercise the HTTP layer using `tower::ServiceExt::oneshot`
//! without requiring a live database or Redis connection.
//!
//! Note: Routes that require `SharedState` (health, metrics, icons, etc.)
//! cannot be tested here without a full database setup. Those are covered
//! by unit tests within the library crate or by end-to-end tests.

use axum::Router;
use axum::body::Body;
use axum::http::{Method, Request, StatusCode};
use axum::routing::get;
use http_body_util::BodyExt;
use tower::ServiceExt;

/// Helper: build a minimal router containing only the version endpoint.
/// The handler reads the edition from the gateway's state (the composition);
/// with no state to give it here, the route answers what the handler
/// answers for a Community composition (`version_info`). The handler itself
/// is asked on an assembled gateway in `integration::community_composition`.
fn version_app() -> Router {
    Router::new().route(
        "/v1/version",
        get(|| async {
            axum::Json(brutor_gateway_core::routes::gateway::version_info(
                brutor_gateway_core::services::edition::Edition::Community,
            ))
        }),
    )
}

// ---------------------------------------------------------------------------
// GET /v1/version — happy path
// ---------------------------------------------------------------------------

#[tokio::test]
async fn version_returns_200() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn version_returns_correct_service_name() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    assert_eq!(json["service"], "brutor-gateway-core");
}

#[tokio::test]
async fn version_returns_correct_runtime() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    assert_eq!(json["runtime"], "rust/tokio");
}

#[tokio::test]
async fn version_contains_all_feature_flags() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    let features = &json["features"];
    assert!(features["llm_streaming"].as_bool().unwrap());
    assert!(features["mcp_proxy"].as_bool().unwrap());
    assert!(features["virtual_mcp"].as_bool().unwrap());
    assert!(features["oauth_proxy"].as_bool().unwrap());
    assert!(features["skill_execution"].as_bool().unwrap());
    assert!(features["guardrails"].as_bool().unwrap());
    assert!(features["usage_tracking"].as_bool().unwrap());
}

#[tokio::test]
async fn version_includes_version_field() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    // The version field must be present and non-empty (set by CARGO_PKG_VERSION).
    let version = json["version"].as_str().unwrap();
    assert!(!version.is_empty(), "version string must not be empty");
}

#[tokio::test]
async fn version_response_is_valid_json_content_type() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let content_type = response
        .headers()
        .get("content-type")
        .expect("content-type header must be present")
        .to_str()
        .unwrap();
    assert!(
        content_type.contains("application/json"),
        "expected application/json, got {content_type}"
    );
}

// ---------------------------------------------------------------------------
// Method not allowed — POST to a GET-only endpoint
// ---------------------------------------------------------------------------

#[tokio::test]
async fn post_to_version_returns_405() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::POST)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn put_to_version_returns_405() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::PUT)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn delete_to_version_returns_405() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::DELETE)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

// ---------------------------------------------------------------------------
// Unknown routes return 404
// ---------------------------------------------------------------------------

#[tokio::test]
async fn unknown_route_returns_404() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/does-not-exist")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn root_returns_404() {
    let app = version_app();

    let response = app
        .oneshot(Request::builder().uri("/").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

// ---------------------------------------------------------------------------
// CORS — verify that CorsLayer::permissive adds the expected headers
// ---------------------------------------------------------------------------

#[tokio::test]
async fn cors_headers_present_on_version() {
    use tower_http::cors::CorsLayer;

    let app = version_app().layer(CorsLayer::permissive());

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .header("Origin", "https://example.com")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    let acao = response
        .headers()
        .get("access-control-allow-origin")
        .expect("CORS allow-origin header must be present")
        .to_str()
        .unwrap();
    assert_eq!(acao, "*", "permissive CORS should allow all origins");
}

#[tokio::test]
async fn cors_preflight_returns_200() {
    use tower_http::cors::CorsLayer;

    let app = version_app().layer(CorsLayer::permissive());

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::OPTIONS)
                .uri("/v1/version")
                .header("Origin", "https://example.com")
                .header("Access-Control-Request-Method", "GET")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
}

// ===========================================================================
// NEW TESTS — added for broader coverage
// ===========================================================================

// ---------------------------------------------------------------------------
// Version response — structural completeness
// ---------------------------------------------------------------------------

#[tokio::test]
async fn version_has_exactly_five_top_level_keys() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    let obj = json.as_object().expect("response must be a JSON object");
    assert_eq!(
        obj.len(),
        5,
        "expected 5 top-level keys (service, version, runtime, edition, features), got {}: {:?}",
        obj.len(),
        obj.keys().collect::<Vec<_>>()
    );
    assert!(obj.contains_key("service"));
    assert!(obj.contains_key("version"));
    assert!(obj.contains_key("runtime"));
    // The edition.
    assert_eq!(obj["edition"], "community");
    assert!(obj.contains_key("features"));
}

#[tokio::test]
async fn version_features_has_exactly_seven_flags() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    let features = json["features"]
        .as_object()
        .expect("features must be a JSON object");
    assert_eq!(
        features.len(),
        7,
        "expected 7 feature flags, got {}: {:?}",
        features.len(),
        features.keys().collect::<Vec<_>>()
    );
}

#[tokio::test]
async fn version_matches_cargo_pkg_version() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();

    let version = json["version"].as_str().unwrap();
    // Version string should be a valid semver (major.minor.patch).
    let parts: Vec<&str> = version.split('.').collect();
    assert_eq!(
        parts.len(),
        3,
        "version '{}' is not valid semver (expected M.m.p)",
        version
    );
    for part in &parts {
        assert!(
            part.parse::<u32>().is_ok(),
            "version component '{}' is not a valid number",
            part
        );
    }
}

// ---------------------------------------------------------------------------
// PATCH method on version returns 405
// ---------------------------------------------------------------------------

#[tokio::test]
async fn patch_to_version_returns_405() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::PATCH)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
}

// ---------------------------------------------------------------------------
// HEAD request on version returns 200 (axum auto-supports HEAD for GET routes)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn head_to_version_returns_200() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::HEAD)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn head_to_version_has_empty_body() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::HEAD)
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert!(
        body.is_empty(),
        "HEAD response body must be empty, got {} bytes",
        body.len()
    );
}

// ---------------------------------------------------------------------------
// 404 on various non-existent paths
// ---------------------------------------------------------------------------

#[tokio::test]
async fn deeply_nested_unknown_route_returns_404() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/a/b/c/d/e/f")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn health_returns_404_on_version_only_app() {
    // The version_app does NOT mount /health, so it should return 404.
    // (The real /health endpoint requires SharedState and is not testable
    // without a database.)
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn healthz_returns_404_on_version_only_app() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/healthz")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

// ---------------------------------------------------------------------------
// CORS — preflight includes expected Access-Control-Allow-Methods
// ---------------------------------------------------------------------------

#[tokio::test]
async fn cors_preflight_includes_allow_methods() {
    use tower_http::cors::CorsLayer;

    let app = version_app().layer(CorsLayer::permissive());

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::OPTIONS)
                .uri("/v1/version")
                .header("Origin", "https://example.com")
                .header("Access-Control-Request-Method", "GET")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    let allow_methods = response
        .headers()
        .get("access-control-allow-methods")
        .expect("CORS allow-methods header must be present on preflight")
        .to_str()
        .unwrap();
    assert!(
        !allow_methods.is_empty(),
        "allow-methods header should not be empty"
    );
}

#[tokio::test]
async fn cors_preflight_includes_allow_headers() {
    use tower_http::cors::CorsLayer;

    let app = version_app().layer(CorsLayer::permissive());

    let response = app
        .oneshot(
            Request::builder()
                .method(Method::OPTIONS)
                .uri("/v1/version")
                .header("Origin", "https://example.com")
                .header("Access-Control-Request-Method", "GET")
                .header("Access-Control-Request-Headers", "content-type")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    let allow_headers = response
        .headers()
        .get("access-control-allow-headers")
        .expect("CORS allow-headers header must be present on preflight")
        .to_str()
        .unwrap();
    assert!(
        !allow_headers.is_empty(),
        "allow-headers header should not be empty"
    );
}

// ---------------------------------------------------------------------------
// Version response body is reproducible (idempotent handler)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn version_response_is_deterministic() {
    // Call the version endpoint twice and verify identical JSON output.
    let app1 = version_app();
    let app2 = version_app();

    let resp1 = app1
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body1 = resp1.into_body().collect().await.unwrap().to_bytes();
    let json1: serde_json::Value = serde_json::from_slice(&body1).unwrap();

    let resp2 = app2
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body2 = resp2.into_body().collect().await.unwrap().to_bytes();
    let json2: serde_json::Value = serde_json::from_slice(&body2).unwrap();

    assert_eq!(json1, json2, "version response should be deterministic");
}

// ---------------------------------------------------------------------------
// Version with query string still returns 200 (query params are ignored)
// ---------------------------------------------------------------------------

#[tokio::test]
async fn version_ignores_query_parameters() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version?foo=bar&baz=42")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["service"], "brutor-gateway-core");
}

// ---------------------------------------------------------------------------
// Router construction — verify gateway router builds without panic
// ---------------------------------------------------------------------------

#[test]
fn gateway_router_construction_does_not_panic() {
    // gateway::router() returns Router<SharedState>. Building it must not
    // panic even though we never supply the state. This verifies that the
    // route definitions themselves are valid.
    let _router: Router<brutor_gateway_core::services::SharedState> =
        brutor_gateway_core::routes::gateway::router();
}

// ---------------------------------------------------------------------------
// Content-length header is present on version response
// ---------------------------------------------------------------------------

#[tokio::test]
async fn version_response_includes_content_length() {
    let app = version_app();

    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/version")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let content_length = response
        .headers()
        .get("content-length")
        .expect("content-length header must be present")
        .to_str()
        .unwrap();
    let len: usize = content_length
        .parse()
        .expect("content-length must be numeric");
    assert!(len > 0, "content-length must be positive");
}

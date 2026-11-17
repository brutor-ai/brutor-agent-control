// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The gateway's HTTP application as the builder assembles it for the full
//! composition (`common::full_app`; `app::router` and
//! the extensions' routes): the one definition the binary serves. These tests build that application,
//! not a copy of it, so a route that is documented but not mounted, or
//! mounted under the wrong prefix, fails here.
//!
//! How a mount is checked without running a handler: the router answers
//! `405 Method Not Allowed` when the path matches a route but the method does
//! not, and `404` when no route matches the path. So every path in the API
//! document is probed with a method it does not serve. `405` proves the path
//! is mounted, and no handler runs.
//!
//! The MCP proxy routes accept every method (`any(...)`), so they have no
//! method to probe with: the handler itself answers. For those the test asserts
//! the handler's own refusal of a server the caller has no access to, which is
//! just as much proof that the path reached it. They are listed by name, so a
//! new any-method route has to be added here on purpose.

use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tower::ServiceExt;
use utoipa::OpenApi;

use brutor_gateway_core::openapi::ApiDoc;

use crate::common;

const TENANT: &str = "tenant-assembly";
const GROUP: &str = "rg-assembly";
const METHODS: [&str; 5] = ["GET", "POST", "PUT", "DELETE", "PATCH"];

/// Documented paths whose route accepts every method, with what the handler
/// answers for a server id this caller cannot use.
const ANY_METHOD: [(&str, StatusCode); 2] = [
    ("/v1/proxy/mcp/{server_id}", StatusCode::FORBIDDEN),
    ("/v1/proxy/mcp/{server_id}/{path}", StatusCode::FORBIDDEN),
];

/// A caller the tenant layer lets through on every surface: an API key for
/// the proxy routes, a portal session for the portal routes.
struct Caller {
    api_key: String,
    portal_jwt: String,
}

async fn caller(pool: &PgPool) -> Caller {
    common::seed_tenant(pool, TENANT).await;
    common::seed_resource_group(pool, GROUP, TENANT).await;
    common::seed_end_user(pool, 1, TENANT).await;
    Caller {
        api_key: common::seed_api_key(pool, TENANT, GROUP, None, "assembly").await,
        portal_jwt: common::make_portal_jwt(1, "assembly-user", TENANT),
    }
}

async fn status(app: &Router, method: &str, path: &str, caller: &Caller) -> StatusCode {
    let token = if path.starts_with("/v1/portal/") {
        &caller.portal_jwt
    } else {
        &caller.api_key
    };
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("authorization", format!("Bearer {token}"))
        .header("x-tenant-id", TENANT)
        .body(Body::empty())
        .expect("request");
    let peer: SocketAddr = "127.0.0.1:55555".parse().expect("peer");
    request.extensions_mut().insert(ConnectInfo(peer));
    app.clone().oneshot(request).await.expect("router").status()
}

/// `{param}` segments filled with a value no route uses as a literal.
fn concrete(path: &str) -> String {
    let mut out = String::new();
    let mut in_param = false;
    for ch in path.chars() {
        match ch {
            '{' => {
                in_param = true;
                out.push_str("zz-probe");
            }
            '}' => in_param = false,
            _ if in_param => {}
            _ => out.push(ch),
        }
    }
    out
}

/// path -> the methods the API document lists for it.
fn documented() -> BTreeMap<String, BTreeSet<String>> {
    let doc = serde_json::to_value(ApiDoc::openapi()).expect("document");
    let mut out = BTreeMap::new();
    for (path, item) in doc["paths"].as_object().expect("paths") {
        let methods: BTreeSet<String> = item
            .as_object()
            .expect("path item")
            .keys()
            .map(|m| m.to_uppercase())
            .filter(|m| METHODS.contains(&m.as_str()))
            .collect();
        out.insert(path.clone(), methods);
    }
    out
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_path_no_route_serves_is_404(pool: PgPool) {
    let caller = caller(&pool).await;
    let app = common::edition_app(pool);
    // The control for the probe below: this is what "not mounted" looks like
    // on each surface, for a caller the tenant layer admits.
    for path in [
        "/v1/proxy/zz-not-a-route",
        "/v1/portal/zz-not-a-route",
        "/v1/zz-not-a-route",
        "/zz-not-a-route",
    ] {
        assert_eq!(
            status(&app, "GET", path, &caller).await,
            StatusCode::NOT_FOUND,
            "{path}"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn health_is_served_without_credentials(pool: PgPool) {
    let app = common::edition_app(pool);
    let request = Request::builder()
        .uri("/health")
        .body(Body::empty())
        .expect("request");
    let response = app.oneshot(request).await.expect("router");
    assert_eq!(response.status(), StatusCode::OK);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn every_documented_path_is_mounted(pool: PgPool) {
    let caller = caller(&pool).await;
    let app = common::edition_app(pool);

    let documented = documented();
    // The document's content is pinned by the OpenAPI snapshot test; here it
    // only has to be the real one, not an empty stand-in.
    assert!(
        documented.len() >= 20,
        "the document lists {} paths",
        documented.len()
    );

    let mut not_mounted = Vec::new();
    let mut probed = 0;
    for (path, methods) in &documented {
        let Some(other) = METHODS.iter().find(|m| !methods.contains(**m)) else {
            continue; // serves all five; nothing to probe with
        };
        probed += 1;
        let expected = ANY_METHOD
            .iter()
            .find(|(any_path, _)| any_path == path)
            .map_or(StatusCode::METHOD_NOT_ALLOWED, |(_, handler_answer)| {
                *handler_answer
            });
        let got = status(&app, other, &concrete(path), &caller).await;
        if got != expected {
            not_mounted.push(format!("{other} {path} -> {got}, expected {expected}"));
        }
    }
    assert!(
        not_mounted.is_empty(),
        "documented paths the application does not mount (a mounted path answers 405 to a \
         method it does not serve; an any-method route answers from its handler):\n{}",
        not_mounted.join("\n")
    );
    for (path, _) in ANY_METHOD {
        assert!(
            documented.contains_key(path),
            "{path} is listed as any-method but is not in the document"
        );
    }
    assert_eq!(probed, documented.len(), "every documented path was probed");
}

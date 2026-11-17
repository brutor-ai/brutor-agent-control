// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A failed database read on a request path is a 503, never
//! the answer an empty result would have produced.
//!
//! Each test makes one table unreadable (renamed, so every query that
//! touches it fails exactly as it would during an outage or a bad
//! migration) and checks the request is refused as unavailable. Before
//! the fix each of these errors became `None` / an empty list and the
//! request went on to a confident, wrong answer: a 404 for an existing
//! user, a public key document with no keys, a run scoped to nobody and
//! therefore open to everybody.

use std::net::SocketAddr;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::{middleware, routes};

use crate::common;

pub(crate) async fn make_unreadable(pool: &PgPool, table: &str) {
    sqlx::query(&format!("ALTER TABLE {table} RENAME TO {table}_unreadable"))
        .execute(pool)
        .await
        .expect("rename table");
}

pub(crate) async fn send(app: Router, mut req: Request<Body>) -> (StatusCode, String) {
    let peer: SocketAddr = "127.0.0.1:55555".parse().unwrap();
    req.extensions_mut().insert(ConnectInfo(peer));
    let resp = app.oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

pub(crate) fn get(path: &str, bearer: &str, tenant: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header("authorization", format!("Bearer {bearer}"))
        .header("x-tenant-id", tenant)
        .body(Body::empty())
        .unwrap()
}

/// Was: `run_group_ids` read the error as "the run touched no group", and
/// an ungrouped run is open to every caller in the tenant (the
/// permissive branch), so a group-scoped key passed the scope check.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_run_scope_is_refused_not_opened(pool: PgPool) {
    let t = "dbe-runs";
    common::seed_tenant(&pool, t).await;
    common::seed_resource_group(&pool, "rg-dbe-runs", t).await;
    let key = common::seed_shared_group_api_key(&pool, t, "rg-dbe-runs", "svc").await;
    let state = common::build_test_state(pool.clone());
    let app = Router::new()
        .nest("/v1/runs", routes::runs::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .with_state(state);

    make_unreadable(&pool, "proxy_logs").await;
    let (st, body) = send(app, get("/v1/runs/rt-dbe-1", &key, t)).await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE, "{body}");
}

/// Was: `stored_keys` read the error as "no keys", and the public key
/// document was served (with a public cache header) without them, so
/// every offline verifier that fetched it failed every record.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_key_store_does_not_publish_an_empty_key_document(pool: PgPool) {
    let state = common::build_test_state(pool.clone());
    let app = Router::new()
        .nest("/.well-known", routes::evidence::well_known_router())
        .with_state(state);
    let req = || {
        Request::builder()
            .uri("/.well-known/brutor-evidence-keys.json")
            .body(Body::empty())
            .unwrap()
    };

    let (st, body) = send(app.clone(), req()).await;
    assert_eq!(st, StatusCode::OK, "control: {body}");

    make_unreadable(&pool, "evidence_signing_keys").await;
    let resp = app.oneshot(req()).await.unwrap();
    assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert!(
        resp.headers()
            .get("cache-control")
            .and_then(|v| v.to_str().ok())
            .is_none_or(|v| !v.contains("public")),
        "a refusal must not be cached by intermediaries"
    );
}

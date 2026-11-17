// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The OAuth routes' configuration lookups fail closed with a 503 when the
//! database errors, instead of answering "not found".
//!
//! Before, every `fetch_optional` in `routes/oauth.rs` ended in
//! `.unwrap_or(None)`: a database outage told an MCP client its server or
//! identity provider did not exist (404), which sends it down the wrong
//! recovery path (re-registration, user-visible "server removed"), and the
//! token exchange silently dropped a gateway user whose credential could not
//! be checked. A table that cannot be read stands in for the outage.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

const T: &str = "t-oauth-db";
const SRV: &str = "srv-oauth-db";
const IDP: &str = "idp-oauth-db";

async fn seeded(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    common::seed_mcp_server(pool, SRV, T, "oauth-db").await;
    common::seed_idp(pool, IDP, T, None, Some("https://idp.test.local/register")).await;
}

async fn unreadable(pool: &PgPool, table: &str) {
    sqlx::query(&format!("ALTER TABLE {table} RENAME TO {table}_unreadable"))
        .execute(pool)
        .await
        .expect("rename");
}

async fn status(pool: &PgPool, req: Request<Body>) -> StatusCode {
    let app = common::build_oauth_app(common::build_test_state(pool.clone()));
    app.oneshot(req).await.unwrap().status()
}

fn metadata(srv: &str, idp: &str) -> Request<Body> {
    common::build_get_request(
        &format!("/v1/proxy/oauth/{srv}/{idp}/.well-known/oauth-protected-resource"),
        None,
        Some(T),
    )
}

fn register() -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(format!("/v1/proxy/oauth/{SRV}/{IDP}/register"))
        .header("X-Tenant-ID", T)
        .header("content-type", "application/json")
        .body(Body::from(
            r#"{"redirect_uris":["https://client.example/cb"]}"#,
        ))
        .unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_missing_server_or_idp_still_answers_as_before(pool: PgPool) {
    seeded(&pool).await;
    // Control: these are the answers the outage used to masquerade as. The
    // OAuth routes resolve the tenant from the server record, so an unknown
    // server is "missing tenant context" (400); an unknown IdP is 404.
    assert_eq!(
        status(&pool, metadata("srv-none", IDP)).await,
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        status(&pool, metadata(SRV, "idp-none")).await,
        StatusCode::NOT_FOUND
    );
    assert_ne!(
        status(&pool, metadata(SRV, IDP)).await,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_mcp_server_table_is_unavailable_not_missing(pool: PgPool) {
    seeded(&pool).await;
    unreadable(&pool, "mcp_servers").await;
    assert_eq!(
        status(&pool, metadata(SRV, IDP)).await,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_idp_table_is_unavailable_not_missing(pool: PgPool) {
    seeded(&pool).await;
    unreadable(&pool, "idp_servers").await;
    assert_eq!(
        status(&pool, metadata(SRV, IDP)).await,
        StatusCode::SERVICE_UNAVAILABLE
    );
    assert_eq!(
        status(&pool, register()).await,
        StatusCode::SERVICE_UNAVAILABLE
    );
}

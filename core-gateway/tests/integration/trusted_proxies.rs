// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The `ClientIp` extractor every handler takes, over a real state: the
//! peer the connection names and `BRUTOR_TRUSTED_PROXIES` decide the
//! client's address, never a header from an untrusted peer.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::Request;
use axum::routing::get;
use http_body_util::BodyExt;
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::routes::request::ClientIp;
use brutor_gateway_core::services::trusted_proxies::TrustedProxies;

use crate::common;

fn app(pool: PgPool, trusted: &str) -> Router {
    let mut state = common::new_state(pool);
    state.config.trusted_proxies = TrustedProxies::parse(trusted).unwrap();
    Router::new()
        .route(
            "/ip",
            get(|client: ClientIp| async move { client.ip().unwrap_or_else(|| "none".into()) }),
        )
        .with_state(Arc::new(state))
}

async fn ip(app: &Router, peer: Option<&str>, forwarded_for: &[&str]) -> String {
    let mut req = Request::builder().uri("/ip");
    for value in forwarded_for {
        req = req.header("x-forwarded-for", *value);
    }
    let mut req = req
        .header("x-real-ip", "192.0.2.99")
        .header("forwarded", "for=192.0.2.98")
        .body(Body::empty())
        .unwrap();
    if let Some(peer) = peer {
        let peer: SocketAddr = peer.parse().unwrap();
        req.extensions_mut().insert(ConnectInfo(peer));
    }
    let resp = app.clone().oneshot(req).await.unwrap();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    String::from_utf8(bytes.to_vec()).unwrap()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn without_trusted_proxies_the_peer_is_the_client(pool: PgPool) {
    let app = app(pool, "");
    assert_eq!(
        ip(&app, Some("203.0.113.7:51000"), &["10.9.9.9"]).await,
        "203.0.113.7"
    );
    assert_eq!(
        ip(&app, Some("[::ffff:203.0.113.7]:1"), &[]).await,
        "203.0.113.7"
    );
    // no connection: no address, never a header's
    assert_eq!(ip(&app, None, &["10.9.9.9"]).await, "none");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_trusted_proxy_names_the_right_most_untrusted_hop(pool: PgPool) {
    let app = app(pool, "10.0.0.0/8, fd00::/8");
    // an untrusted peer: its header is ignored
    assert_eq!(
        ip(&app, Some("198.51.100.1:443"), &["6.6.6.6"]).await,
        "198.51.100.1"
    );
    // a trusted peer: the forged left-most entry is not believed
    assert_eq!(
        ip(
            &app,
            Some("10.0.0.2:443"),
            &["6.6.6.6, 198.51.100.4:5678", "10.0.0.3"]
        )
        .await,
        "198.51.100.4"
    );
    assert_eq!(
        ip(&app, Some("[fd00::2]:443"), &["[2001:db8::1]:80"]).await,
        "2001:db8::1"
    );
    // a non-address stops the walk; all trusted: the left-most
    assert_eq!(
        ip(&app, Some("10.0.0.2:443"), &["198.51.100.4, unknown"]).await,
        "10.0.0.2"
    );
    assert_eq!(
        ip(&app, Some("10.0.0.2:443"), &["10.1.1.1, 10.2.2.2"]).await,
        "10.1.1.1"
    );
    // X-Real-IP and Forwarded are never read
    assert_eq!(ip(&app, Some("10.0.0.2:443"), &[]).await, "10.0.0.2");
}

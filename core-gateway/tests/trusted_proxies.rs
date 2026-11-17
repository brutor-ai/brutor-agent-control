// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The caller's address helper (`routes::request::extract_client_ip`): a
//! peer that is not a trusted proxy cannot name the client with
//! `X-Forwarded-For` (it could, from any peer, before
//! `BRUTOR_TRUSTED_PROXIES`). The rules in full are pinned by the unit tests
//! of `services::trusted_proxies` and `tests/integration/trusted_proxies.rs`.

use std::net::SocketAddr;

use axum::http::{HeaderMap, HeaderValue};
use brutor_gateway_core::routes::request::extract_client_ip;
use brutor_gateway_core::services::trusted_proxies::TrustedProxies;

fn headers() -> HeaderMap {
    let mut h = HeaderMap::new();
    h.insert("x-forwarded-for", HeaderValue::from_static("10.9.9.9"));
    h.insert("x-real-ip", HeaderValue::from_static("10.8.8.8"));
    h
}

#[test]
fn an_untrusted_peer_cannot_forge_the_client_ip() {
    let peer: SocketAddr = "203.0.113.7:51000".parse().unwrap();
    for list in ["", "10.0.0.0/8"] {
        let trusted = TrustedProxies::parse(list).unwrap();
        assert_eq!(
            extract_client_ip(&headers(), Some(peer), &trusted).as_deref(),
            Some("203.0.113.7"),
            "trusted: {list:?}"
        );
    }
}

#[test]
fn a_trusted_peer_names_the_client_and_no_peer_names_nobody() {
    let peer: SocketAddr = "10.0.0.2:443".parse().unwrap();
    let trusted = TrustedProxies::parse("10.0.0.2").unwrap();
    assert_eq!(
        extract_client_ip(&headers(), Some(peer), &trusted).as_deref(),
        Some("10.9.9.9")
    );
    assert_eq!(extract_client_ip(&headers(), None, &trusted), None);
}

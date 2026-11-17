// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A non-idempotent upstream call whose outcome is unknown is recorded as
//! ambiguous, not retried. The retry layer is what every
//! side-effecting caller (MCP `tools/call`) goes through.
use std::time::Duration;

use brutor_gateway_core::services::circuit_breaker::CircuitBreakerRegistry;
use brutor_gateway_core::services::evidence::request::{ambiguous_upstream, scope};
use brutor_gateway_core::services::retry::{RetryConfig, execute_with_retry};
use wiremock::matchers::any;
use wiremock::{Mock, MockServer, ResponseTemplate};

fn config(idempotent: bool) -> RetryConfig {
    RetryConfig {
        max_retries: 2,
        base_delay: Duration::from_millis(1),
        backoff_factor: 1,
        idempotent,
        deadline: None,
    }
}

/// Run one call through the retry layer inside a request scope; return what
/// the scope recorded and how many requests the upstream saw.
async fn call(
    server: &MockServer,
    url: String,
    idempotent: bool,
    timeout: Duration,
) -> (Vec<String>, usize) {
    let client = reqwest::Client::new();
    let recorded = scope(None, async {
        let _ = execute_with_retry(
            &config(idempotent),
            &CircuitBreakerRegistry::new(),
            "k",
            || client.post(url.as_str()).timeout(timeout).send(),
        )
        .await;
        ambiguous_upstream()
    })
    .await;
    (
        recorded,
        server.received_requests().await.unwrap_or_default().len(),
    )
}

async fn answering(status: u16, delay: Duration) -> MockServer {
    let server = MockServer::start().await;
    Mock::given(any())
        .respond_with(ResponseTemplate::new(status).set_delay(delay))
        .mount(&server)
        .await;
    server
}

#[tokio::test]
async fn a_gateway_timeout_on_a_side_effecting_call_is_recorded_and_not_retried() {
    let server = answering(504, Duration::ZERO).await;
    let (recorded, sent) = call(&server, server.uri(), false, Duration::from_secs(5)).await;
    assert_eq!(sent, 1, "not retried");
    assert_eq!(recorded, ["HTTP 504 after the request was sent"]);
}

#[tokio::test]
async fn a_timeout_after_sending_is_recorded() {
    let server = answering(200, Duration::from_secs(3)).await;
    let (recorded, sent) = call(&server, server.uri(), false, Duration::from_millis(200)).await;
    assert_eq!(sent, 1, "not retried");
    assert_eq!(recorded, ["timeout after the request was sent"]);
}

#[tokio::test]
async fn a_refused_connection_is_not_ambiguous() {
    // Control: a request that never reached anything has a known outcome.
    let server = answering(200, Duration::ZERO).await;
    let dead = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let (recorded, _) = call(
        &server,
        format!("http://127.0.0.1:{dead}/"),
        false,
        Duration::from_secs(2),
    )
    .await;
    assert!(recorded.is_empty(), "{recorded:?}");
}

#[tokio::test]
async fn an_idempotent_call_still_retries_and_is_not_flagged() {
    let server = answering(504, Duration::ZERO).await;
    let (recorded, sent) = call(&server, server.uri(), true, Duration::from_secs(5)).await;
    assert_eq!(sent, 3, "first attempt plus two retries");
    assert!(recorded.is_empty(), "{recorded:?}");
}

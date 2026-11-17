// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The control plane's cache-statistics request, answered by a Community
//! gateway.
//!
//! A Community gateway is one instance, so it answers one request with one
//! reply: the request's correlation id and its own counters, and nothing
//! that names an instance. The request and the reply travel as the control
//! plane sends and reads them: `NOTIFY` on the request channel, `LISTEN` on
//! the reply channel, through the gateway's real listener.
//!
//! The helpers are shared with the Enterprise suite, whose clustering
//! extension answers per instance (`enterprise/tests/integration/clustering.rs`).

use std::collections::BTreeMap;
use std::time::Duration;

use serde_json::{Value, json};
use sqlx::PgPool;
use sqlx::postgres::PgListener;
use tokio_util::sync::CancellationToken;

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::system_notify::{
    CHANNEL_CACHE_STATS_REPLY, CHANNEL_CACHE_STATS_REQUEST, SystemNotifyListener,
};

use crate::common;

fn database_url(pool: &PgPool) -> String {
    use sqlx::ConnectOptions;
    pool.connect_options().to_url_lossy().to_string()
}

/// The replies to one statistics request, from gateways with `states`
/// listening on the test's database: the control plane's side of the
/// exchange (LISTEN on the reply channel, NOTIFY the request, collect what
/// carries the request's correlation id).
pub async fn replies_to_one_request(pool: &PgPool, states: Vec<SharedState>) -> Vec<Value> {
    let url = database_url(pool);
    let shutdown = CancellationToken::new();
    let listening = states.len();
    for state in states {
        SystemNotifyListener {
            database_url: url.clone(),
            state,
        }
        .spawn(shutdown.clone());
    }
    let mut collector = PgListener::connect(&url).await.expect("collector");
    collector
        .listen(CHANNEL_CACHE_STATS_REPLY)
        .await
        .expect("LISTEN on the reply channel");

    // A request published before a gateway's listener is subscribed is lost
    // (NOTIFY is not replayed), so ask until every gateway has answered one
    // request, then ask the request that is measured.
    let mut attempt = 0;
    let replies = loop {
        attempt += 1;
        assert!(attempt <= 100, "the gateways never answered a request");
        let correlation = format!("stats-test-{attempt:04}-{}", uuid::Uuid::new_v4().simple());
        sqlx::query("SELECT pg_notify($1, $2)")
            .bind(CHANNEL_CACHE_STATS_REQUEST)
            .bind(json!({ "correlation_id": correlation }).to_string())
            .execute(pool)
            .await
            .expect("publish the request");
        // The collection window. Everything that arrives in it is kept:
        // more replies than gateways must be seen, not cut off.
        let mut replies = Vec::new();
        let window = tokio::time::Instant::now() + Duration::from_millis(400);
        while let Ok(Ok(notification)) = tokio::time::timeout_at(window, collector.recv()).await {
            let reply: Value = serde_json::from_str(notification.payload()).expect("reply JSON");
            if reply["correlation_id"] == json!(correlation) {
                replies.push(reply);
            }
        }
        if replies.len() >= listening {
            // Every listener is subscribed. One more request, measured
            // whole: nothing of an earlier attempt can be in its window.
            let correlation = format!("stats-test-final-{}", uuid::Uuid::new_v4().simple());
            sqlx::query("SELECT pg_notify($1, $2)")
                .bind(CHANNEL_CACHE_STATS_REQUEST)
                .bind(json!({ "correlation_id": correlation }).to_string())
                .execute(pool)
                .await
                .expect("publish the request");
            let mut replies = Vec::new();
            let window = tokio::time::Instant::now() + Duration::from_millis(800);
            while let Ok(Ok(notification)) = tokio::time::timeout_at(window, collector.recv()).await
            {
                let reply: Value =
                    serde_json::from_str(notification.payload()).expect("reply JSON");
                if reply["correlation_id"] == json!(correlation) {
                    replies.push(reply);
                }
            }
            break replies;
        }
    };
    shutdown.cancel();
    replies
}

/// A reply with what differs from run to run taken out: the correlation id,
/// the replica's own name (a non-empty string stays visible as such) and
/// the counters' values (a non-negative integer stays visible as such).
pub fn shape_of(reply: &Value) -> Value {
    let object = reply.as_object().expect("a reply is an object");
    let mut shape = BTreeMap::new();
    for (key, value) in object {
        let described = match (key.as_str(), value) {
            ("correlation_id", Value::String(s)) if !s.is_empty() => json!("<the request's>"),
            ("replica_id", Value::String(s)) if !s.is_empty() => json!("<a non-empty string>"),
            ("layers", Value::Object(layers)) => Value::Object(
                layers
                    .iter()
                    .map(|(layer, counters)| {
                        let counters = counters.as_object().expect("counters");
                        let described: serde_json::Map<String, Value> = counters
                            .iter()
                            .map(|(name, n)| {
                                (
                                    name.clone(),
                                    if n.is_u64() {
                                        json!("<u64>")
                                    } else {
                                        n.clone()
                                    },
                                )
                            })
                            .collect();
                        (layer.clone(), Value::Object(described))
                    })
                    .collect(),
            ),
            (_, other) => other.clone(),
        };
        shape.insert(key.clone(), described);
    }
    json!(shape)
}

/// One Community gateway, one request, one reply: the correlation id and
/// the counters of the three Community layers. No field names an instance.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_community_gateway_answers_with_one_reply_that_names_no_instance(pool: PgPool) {
    let state = common::build_community_test_state(pool.clone());
    assert!(state.cache_stats_responder.feature().is_none());
    let replies = replies_to_one_request(&pool, vec![state]).await;
    assert_eq!(replies.len(), 1, "one instance, one reply: {replies:?}");
    assert_eq!(
        shape_of(&replies[0]),
        json!({
            "correlation_id": "<the request's>",
            "layers": {
                "group": {"entries": "<u64>", "hits": "<u64>", "misses": "<u64>"},
                "policy": {"entries": "<u64>", "hits": "<u64>", "misses": "<u64>"},
                "tool": {"entries": "<u64>", "hits": "<u64>", "misses": "<u64>"},
            },
        }),
        "the Community reply: {}",
        replies[0]
    );
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! An operator abort that lands while a stream is open stops the stream and
//! is recorded as the abort it is.
//!
//! Only the chat stream polled for an abort mid-stream, and it recorded the
//! stop as a plain 403 outcome row: no `run_aborted` subtype, so the evidence
//! writer did not seal it as the `denied` step it was and the run's timeline
//! could not say why it ended. The other streams did not poll at all. Every
//! stream now asks `StreamGuard::run_aborted` per upstream event and stamps
//! `StreamGuard::stamp_outcome` on its outcome row — the static guard in
//! `tests/llm_refusal_guard.rs` holds every streaming surface to that.
//!
//! The upstream here is gated: it sends a few chunks, waits until the test
//! has aborted the run, then sends the rest — so the abort lands mid-stream
//! deterministically, not by racing the wall clock.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tokio::sync::Notify;
use tower::ServiceExt;

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::call_chain::{asserted_root, derive_tenant_key};

use crate::common;

const RUN: &str = "run-stream-abort";
const SEED: [u8; 32] = [0x42; 32];
/// Chunks after the gate — comfortably more than two abort-poll periods.
const AFTER_GATE: usize = 80;

fn enable_evidence() {
    unsafe {
        std::env::set_var("BRUTOR_EVIDENCE_SIGNING_KEY", hex::encode(SEED));
        std::env::set_var("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2");
        std::env::set_var("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off");
    }
    brutor_gateway_core::env_vars::reload_for_tests();
}

fn chunk(i: usize) -> String {
    let v = json!({"id": "c", "object": "chat.completion.chunk", "model": "test-gpt",
                   "choices": [{"index": 0, "delta": {"content": format!("tok{i} ")},
                                "finish_reason": null}]});
    format!("data: {v}\n\n")
}

/// An OpenAI-shaped streaming upstream that sends 3 chunks, waits on `gate`,
/// then sends [`AFTER_GATE`] more and `[DONE]`. Returns its base URL.
async fn gated_upstream(gate: Arc<Notify>) -> String {
    let app = axum::Router::new().route(
        "/chat/completions",
        axum::routing::post(move || {
            let gate = gate.clone();
            async move {
                let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, std::io::Error>>(8);
                tokio::spawn(async move {
                    for i in 0..3 {
                        if tx.send(Ok(chunk(i))).await.is_err() {
                            return;
                        }
                    }
                    gate.notified().await;
                    for i in 3..3 + AFTER_GATE {
                        if tx.send(Ok(chunk(i))).await.is_err() {
                            // The gateway dropped the connection — the point.
                            return;
                        }
                    }
                    let _ = tx.send(Ok("data: [DONE]\n\n".to_string())).await;
                });
                axum::response::Response::builder()
                    .header("content-type", "text/event-stream")
                    .body(Body::from_stream(
                        tokio_stream::wrappers::ReceiverStream::new(rx),
                    ))
                    .expect("stream response")
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = listener.local_addr().expect("addr");
    tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });
    format!("http://{addr}")
}

async fn topology(pool: &PgPool, t: &str, upstream: &str) -> String {
    let rg = format!("rg-{t}");
    common::seed_tenant(pool, t).await;
    common::seed_resource_group(pool, &rg, t).await;
    common::seed_end_user(pool, 9881, t).await;
    common::add_direct_membership(pool, 9881, &rg, t).await;
    common::seed_llm_provider(pool, &format!("prov-{t}"), t, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        &format!("m-{t}"),
        t,
        &format!("prov-{t}"),
        "test-gpt",
        upstream,
    )
    .await;
    common::set_model_api_key(pool, &format!("m-{t}"), "sk-test").await;
    common::bind_llm_model_to_rg(pool, &format!("m-{t}"), &rg).await;
    common::seed_api_key(pool, t, &rg, Some(9881), &format!("key-{t}")).await
}

fn run_root(state: &SharedState, t: &str, key: &str) -> String {
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), t);
    let auth = format!("Bearer {key}");
    let get = |n: &str| match n {
        "authorization" => Some(auth.clone()),
        "x-brutor-run-id" => Some(RUN.to_string()),
        _ => None,
    };
    asserted_root(&get, &tenant_key)
        .expect("an authenticated caller may assert its run")
        .to_string()
}

#[derive(Clone, Copy, Debug)]
enum Surface {
    Chat,
    Messages,
}

async fn a_mid_stream_abort_is_recorded(pool: PgPool, surface: Surface) {
    enable_evidence();
    let t = match surface {
        Surface::Chat => "t-sab-chat",
        Surface::Messages => "t-sab-msgs",
    };
    let gate = Arc::new(Notify::new());
    let upstream = gated_upstream(gate.clone()).await;
    let key = topology(&pool, t, &upstream).await;
    let state = common::build_test_state(pool.clone());
    let root = run_root(&state, t, &key);

    let (app, path, body) = match surface {
        Surface::Chat => (
            common::build_llm_app(state.clone()),
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": format!("m-{t}"), "stream": true,
                   "messages": [{"role": "user", "content": "count"}]}),
        ),
        Surface::Messages => (
            common::build_anthropic_app(state.clone()),
            "/v1/messages",
            json!({"model": format!("m-{t}"), "stream": true, "max_tokens": 64,
                   "messages": [{"role": "user", "content": "count"}]}),
        ),
    };
    let mut req = common::build_post_request(path, body, Some(&key), Some(t));
    req.headers_mut()
        .insert("x-brutor-run-id", RUN.parse().expect("run header"));
    let resp = app.oneshot(req).await.expect("router call");
    assert_eq!(
        resp.status(),
        StatusCode::OK,
        "{surface:?}: the stream opened"
    );
    let mut body = resp.into_body();

    // The stream is flowing: the first frame reached the client.
    let first = tokio::time::timeout(Duration::from_secs(20), body.frame())
        .await
        .expect("first frame")
        .expect("a frame")
        .expect("frame ok");
    assert!(first.is_data(), "{surface:?}");

    // Now the operator stops the run — and only then does the upstream go on.
    sqlx::query(
        "INSERT INTO run_interventions \
         (id, tenant_id, root_task_id, action, reason, status, requested_at, \
          steps_blocked, created_at, updated_at) \
         VALUES ($1, $2, $3, 'abort', 'runaway', 'requested', now(), 0, now(), now())",
    )
    .bind(format!("ri-{t}"))
    .bind(t)
    .bind(&root)
    .execute(&pool)
    .await
    .expect("abort the run");
    // And propagate it the way the `brutor_run_abort` listener does in
    // production (path 1: the per-pod map). The durable row alone is path 3,
    // which fails OPEN on a lookup error by design — under a
    // loaded test pool that made this test flaky, not the gateway wrong.
    state.run_abort_cache.insert(
        t,
        &root,
        brutor_gateway_core::services::run_intervention::AbortRecord {
            intervention_id: format!("ri-{t}"),
            reason: "runaway".into(),
        },
    );
    gate.notify_one();

    let rest = tokio::time::timeout(Duration::from_secs(30), body.collect())
        .await
        .expect("the aborted stream must end")
        .expect("collect")
        .to_bytes();
    let rest = String::from_utf8_lossy(&rest);
    assert!(
        !rest.contains(&format!("tok{} ", 2 + AFTER_GATE)),
        "{surface:?}: the stream must stop, not run to its end: {rest}"
    );

    // The outcome row is the abort: run_aborted, on the run, sealed denied.
    let mut row = None;
    for _ in 0..160 {
        row = sqlx::query_as::<_, (Option<i32>, Option<String>, Option<String>)>(
            "SELECT http_status_code, root_task_id, record_id FROM proxy_logs \
              WHERE tenant_id = $1 AND proxy_subtype = 'run_aborted' AND record_id IS NOT NULL",
        )
        .bind(t)
        .fetch_optional(&pool)
        .await
        .expect("proxy_logs query");
        if row.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let (status, row_root, record) =
        row.unwrap_or_else(|| panic!("{surface:?}: no sealed run_aborted outcome row"));
    assert_eq!(status, Some(403), "{surface:?}");
    assert_eq!(row_root.as_deref(), Some(root.as_str()), "{surface:?}");
    let (verdict,): (String,) =
        sqlx::query_as("SELECT verdict FROM action_records WHERE record_id = $1")
            .bind(record.expect("sealed"))
            .fetch_one(&pool)
            .await
            .expect("action record");
    assert_eq!(
        verdict, "denied",
        "{surface:?}: sealed like a pre-flight abort"
    );
    let (blocked,): (i32,) =
        sqlx::query_as("SELECT steps_blocked FROM run_interventions WHERE id = $1")
            .bind(format!("ri-{t}"))
            .fetch_one(&pool)
            .await
            .expect("intervention");
    assert!(
        blocked >= 1,
        "{surface:?}: the intervention records that it bit"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_mid_stream_abort_on_chat_is_recorded_as_the_abort(pool: PgPool) {
    a_mid_stream_abort_is_recorded(pool, Surface::Chat).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_mid_stream_abort_on_messages_is_recorded_as_the_abort(pool: PgPool) {
    a_mid_stream_abort_is_recorded(pool, Surface::Messages).await;
}

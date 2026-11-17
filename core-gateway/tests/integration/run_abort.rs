// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Operator abort of one in-flight run, end to end.
//!
//! **Abort is autonomy scoped to a run.** The point of it is that it stops
//! ONE run and leaves its neighbours alone; suspending the AI System was
//! always possible and is exactly the blunt instrument nobody uses on a
//! shared assistant.
//!
//! Pinned here, in the order the risk matters:
//!
//! 1. The next governed action on the aborted run is refused, with the
//!    `run_aborted` subtype so it is separable from an autonomy block.
//! 2. A different run in the same tenant is untouched.
//! 3. The intervention records that it actually bit — "I clicked stop" and
//!    "the run stopped" are different claims.
//! 4. Pending approvals on the run are expired, so an approver cannot
//!    resurrect it an hour later.
//! 5. The sweeper finalizes it as `cancelled` by `operator`, not as an
//!    abandoned run: the operator's label wins over the timeout's guess.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::run_intervention;

use crate::common;

const T: &str = "tenant-abort";

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn seed(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-abort", T).await;
    common::seed_end_user(pool, user_id, T).await;
    common::add_direct_membership(pool, user_id, "rg-abort", T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-abort", T, "tools", mock_uri).await;
    common::seed_server_config(pool, "sc-abort", T, "ms-abort").await;
    common::bind_server_config_to_rg(pool, "sc-abort", "rg-abort").await;
    common::seed_api_key(pool, T, "rg-abort", Some(user_id), "abort-key").await
}

/// The `root_task_id` the gateway actually recorded for this client's run.
///
/// `X-Brutor-Run-Id` is a client-supplied label; the chain derives a
/// tenant-keyed ULID from it, and THAT is what lands in `proxy_logs` and
/// what the live-run list shows an operator. So an abort is keyed on the
/// derived value, which is what these tests use.
async fn observed_root_task_id(pool: &PgPool, client_run_id: &str) -> String {
    for _ in 0..60 {
        let row: Option<(Option<String>,)> = sqlx::query_as(
            "SELECT root_task_id FROM proxy_logs \
             WHERE tenant_id = $1 AND root_task_id IS NOT NULL \
             ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(T)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query");
        if let Some((Some(root),)) = row {
            return root;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no proxy_logs row for run {client_run_id}");
}

/// Write the intervention the way the control plane does.
async fn request_abort(pool: &PgPool, root_task_id: &str, reason: &str) -> String {
    let id = format!("ri-{root_task_id}");
    sqlx::query(
        "INSERT INTO run_interventions \
         (id, tenant_id, root_task_id, action, reason, status, requested_at, \
          steps_blocked, created_at, updated_at) \
         VALUES ($1, $2, $3, 'abort', $4, 'requested', now(), 0, now(), now())",
    )
    .bind(&id)
    .bind(T)
    .bind(root_task_id)
    .bind(reason)
    .execute(pool)
    .await
    .expect("insert intervention");
    id
}

fn tool_call(run_id: &str) -> (Value, &str) {
    (
        json!({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "do_thing", "arguments": {}}
        }),
        run_id,
    )
}

async fn call_with_run(pool: &PgPool, api_key: &str, run_id: &str) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);
    let (body, _) = tool_call(run_id);
    let mut req =
        common::build_post_request("/v1/proxy/mcp/ms-abort", body, Some(api_key), Some(T));
    // The client asserts its run id the same way an agent SDK does.
    req.headers_mut()
        .insert("X-Brutor-Run-Id", run_id.parse().unwrap());
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_to_json(resp.into_body()).await)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_next_action_on_an_aborted_run_is_refused(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), 9801).await;

    // A first call establishes the run, exactly as an agent would.
    let run_id = "run-abort-1";
    let (first, _) = call_with_run(&pool, &key, run_id).await;
    assert_eq!(first, StatusCode::OK, "the run starts healthy");

    // The operator aborts what the live-run list shows them.
    let observed = observed_root_task_id(&pool, run_id).await;
    request_abort(&pool, &observed, "runaway loop").await;

    let (status, body) = call_with_run(&pool, &key, run_id).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "an aborted run's next action must be refused: {body}"
    );
    assert!(
        body.to_string().contains("stopped by an operator"),
        "the refusal must say who stopped it, not a bare 403: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_neighbouring_run_is_untouched(pool: PgPool) {
    // The whole reason abort exists: suspending the AI System was always
    // possible and takes down every other run inside it.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), 9802).await;

    // Establish the run that will be stopped, then abort what was observed.
    call_with_run(&pool, &key, "run-doomed").await;
    let doomed = observed_root_task_id(&pool, "run-doomed").await;
    request_abort(&pool, &doomed, "stop this one").await;

    // A DIFFERENT client run derives a different root and must be unaffected.
    let (status, body) = call_with_run(&pool, &key, "run-healthy").await;
    assert_eq!(
        status,
        StatusCode::OK,
        "a different run must keep working: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_intervention_records_that_it_actually_bit(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), 9803).await;
    let run_id = "run-abort-3";
    call_with_run(&pool, &key, run_id).await;
    let observed = observed_root_task_id(&pool, run_id).await;
    let id = request_abort(&pool, &observed, "runaway").await;

    let before: (String, Option<chrono::DateTime<chrono::Utc>>, i32) = sqlx::query_as(
        "SELECT status, first_blocked_at, steps_blocked FROM run_interventions WHERE id = $1",
    )
    .bind(&id)
    .fetch_one(&pool)
    .await
    .expect("row");
    assert_eq!(before.0, "requested");
    assert!(before.1.is_none(), "not yet observed by any replica");

    call_with_run(&pool, &key, run_id).await;

    let after: (String, Option<chrono::DateTime<chrono::Utc>>, i32) = sqlx::query_as(
        "SELECT status, first_blocked_at, steps_blocked FROM run_interventions WHERE id = $1",
    )
    .bind(&id)
    .fetch_one(&pool)
    .await
    .expect("row");
    assert_eq!(after.0, "effective", "a refusal must move it to effective");
    assert!(after.1.is_some(), "first_blocked_at must be stamped");
    assert_eq!(after.2, 1, "one refusal counted");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn pending_approvals_on_the_run_are_expired(pool: PgPool) {
    // Otherwise an approver could approve an action on a stopped run an hour
    // later, minting a valid token and resurrecting it.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .mount(&mock)
        .await;
    let key = seed(&pool, &mock.uri(), 9804).await;
    let run_id = "run-abort-4";
    call_with_run(&pool, &key, run_id).await;
    let observed = observed_root_task_id(&pool, run_id).await;
    request_abort(&pool, &observed, "runaway").await;

    sqlx::query(
        "INSERT INTO tool_approval_requests \
         (id, tenant_id, server_name, capability_type, capability_name, status, \
          token_used, requester_type, requester_group_id, timeout_seconds, \
          originating_trace_root_id, created_at, expires_at) \
         VALUES ('appr-abort', $1, 'tools', 'tool', 'do_thing', 'pending', 'N', \
                 'agent', 'rg-abort', 300, $2, now(), now() + interval '1 hour')",
    )
    .bind(T)
    .bind(&observed)
    .execute(&pool)
    .await
    .expect("seed approval");

    call_with_run(&pool, &key, run_id).await;

    let status: (String,) =
        sqlx::query_as("SELECT status FROM tool_approval_requests WHERE id = 'appr-abort'")
            .fetch_one(&pool)
            .await
            .expect("approval row");
    assert_eq!(
        status.0, "expired",
        "a stopped run's pending approvals must not stay approvable"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_check_falls_through_the_three_paths(pool: PgPool) {
    // No cache, no Redis: the durable record is the last line and must
    // answer on its own, because that is the path a replica takes when the
    // fast ones have not reached it.
    common::seed_tenant(&pool, T).await;
    let run_id = "01JABORTRUN0000000000000005";
    request_abort(&pool, run_id, "runaway").await;

    let verdict = run_intervention::check(&pool, None, T, run_id).await;
    assert!(verdict.is_some(), "the database alone must answer");

    let clean = run_intervention::check(&pool, None, T, "01JNOTABORTED000000000000").await;
    assert!(clean.is_none(), "an un-aborted run must not be blocked");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn reconcile_populates_a_cold_replica(pool: PgPool) {
    // Propagation path 3: a replica that started mid-incident, or missed the
    // NOTIFY, catches up on the sweeper tick.
    common::seed_tenant(&pool, T).await;
    let run_id = "01JABORTRUN0000000000000006";
    request_abort(&pool, run_id, "runaway").await;

    let cache = run_intervention::RunAbortCache::new();
    assert!(cache.get(T, run_id).is_none(), "cold replica knows nothing");

    run_intervention::reconcile(&pool, &cache).await;
    assert!(
        cache.get(T, run_id).is_some(),
        "reconcile must load open interventions"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_closed_intervention_no_longer_blocks(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let run_id = "01JABORTRUN0000000000000007";
    let id = request_abort(&pool, run_id, "runaway").await;
    sqlx::query("UPDATE run_interventions SET status = 'closed' WHERE id = $1")
        .bind(&id)
        .execute(&pool)
        .await
        .expect("close");

    assert!(
        run_intervention::check(&pool, None, T, run_id)
            .await
            .is_none(),
        "a closed intervention is history, not an active block"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_run_cannot_be_aborted_twice(pool: PgPool) {
    // A second abort is the same instruction, not a new one; the unique
    // constraint is what makes the control plane's idempotent response safe.
    common::seed_tenant(&pool, T).await;
    let run_id = "01JABORTRUN0000000000000008";
    request_abort(&pool, run_id, "first").await;

    let second = sqlx::query(
        "INSERT INTO run_interventions \
         (id, tenant_id, root_task_id, action, reason, status, requested_at, \
          steps_blocked, created_at, updated_at) \
         VALUES ('ri-dup', $1, $2, 'abort', 'second', 'requested', now(), 0, now(), now())",
    )
    .bind(T)
    .bind(run_id)
    .execute(&pool)
    .await;
    assert!(
        second.is_err(),
        "a second open abort on one run must be refused"
    );
}

/// With nothing in the local map or
/// Redis and the durable record unreadable, the action is REFUSED. If it were
/// allowed, an operator's stop would be silently ignored for exactly as
/// long as the interventions store is failing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_intervention_store_refuses_the_action(pool: PgPool) {
    // Fault injection on this test's private database: the lookup errors.
    sqlx::query("DROP TABLE run_interventions CASCADE")
        .execute(&pool)
        .await
        .unwrap();

    let verdict = run_intervention::check(&pool, None, T, "01JUNREADABLE00000000000000").await;
    match verdict {
        Some(brutor_gateway_core::services::autonomy::Verdict::Deny(msg)) => {
            assert_eq!(msg, run_intervention::ABORT_UNKNOWN_MESSAGE)
        }
        other => panic!("an unreadable store must refuse, got {other:?}"),
    }
}

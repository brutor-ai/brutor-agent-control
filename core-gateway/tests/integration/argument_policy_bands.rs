// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The banded policy, end to end.
//!
//! The canonical demonstration of agent control is a threshold: allow small
//! transfers, hold large ones for a human, refuse the outrageous ones. Before
//! this, Brutor could only answer that with a hard block, which kills the
//! agent above the threshold rather than supervising it.
//!
//! What these tests pin, in the order the risk matters:
//!
//! 1. **The band works.** Below → through. Middle → 202 with an approval
//!    entry. Above → 403. One policy, ordered rules.
//! 2. **The bypass is closed.** Omitting the compared field must NOT slip
//!    past the threshold. The unit tests assert the raw predicate fails open;
//!    this asserts the *shipped shape* — with a presence guard — refuses.
//! 3. **The approval binds the arguments.** Approving £8,000 and retrying at
//!    £80,000 is refused, now exercised through the argument-policy
//!    path rather than only the capability-filter one.
//! 4. **One call, one queue entry.** An argument-policy band and a
//!    capability filter that both want a human collapse into a single
//!    approval, not two.
//! 5. **The approver sees the number.** A queue entry saying "amount 8000
//!    exceeds the 1000 threshold" is one an operator can act on.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-band";

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

/// The banded policy: deny over 10,000, approve over 1,000,
/// allow below — with the presence rule first, which is what makes the
/// threshold un-bypassable.
async fn seed_band_policy(pool: &PgPool, rg_id: &str) {
    let rules = json!([
        {"deny_if": "schema_invalid", "severity": "deny",
         "message": "transfer_funds called without an amount"},
        {"deny_if": "field_gt", "field": "amount", "value": 10000, "severity": "deny",
         "message": "Transfers over 10,000 are not permitted from this system"},
        {"deny_if": "field_gt", "field": "amount", "value": 1000,
         "severity": "approval_required",
         "message": "Transfers over 1,000 require approval"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-band', $1, 'payments-threshold', 'mcp_input', 'transfer_funds', \
                 '*', 'json', $2::jsonb, $3::jsonb, true, 'warn', now(), now()) \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(T)
    .bind(json!({"schema": {"required": ["amount"]}}).to_string())
    .bind(rules.to_string())
    .execute(pool)
    .await
    .expect("seed policy");

    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, 'ap-band') ON CONFLICT DO NOTHING",
    )
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind policy to group");
}

async fn seed_topology(pool: &PgPool, mock_uri: &str, user_id: i32) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, "rg-band", T).await;
    common::seed_end_user(pool, user_id, T).await;
    common::add_direct_membership(pool, user_id, "rg-band", T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-band", T, "bank", mock_uri).await;
    common::seed_server_config(pool, "sc-band", T, "ms-band").await;
    common::bind_server_config_to_rg(pool, "sc-band", "rg-band").await;
    common::seed_api_key(pool, T, "rg-band", Some(user_id), "band-key").await
}

fn transfer(amount: Value) -> Value {
    json!({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {"amount": amount}}
    })
}

async fn call(pool: &PgPool, mock_uri: &str, api_key: &str, body: Value) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);
    let _ = mock_uri;
    let req = common::build_post_request("/v1/proxy/mcp/ms-band", body, Some(api_key), Some(T));
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    (status, body_to_json(resp.into_body()).await)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_transfer_below_the_band_goes_through(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9701).await;
    seed_band_policy(&pool, "rg-band").await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(500))).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "below the band must proceed: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_transfer_inside_the_band_asks_for_a_human(pool: PgPool) {
    let mock = MockServer::start().await;
    // The upstream must NOT be called — the point of the band is that the
    // action is held, not performed and then reviewed.
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9702).await;
    seed_band_policy(&pool, "rg-band").await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "inside the band must enqueue an approval: {body}"
    );
    assert_eq!(body["approval_required"], json!(true));

    let id = body["approval_request_id"].as_str().expect("approval id");
    let meta: (Option<Value>,) =
        sqlx::query_as("SELECT request_metadata FROM tool_approval_requests WHERE id = $1")
            .bind(id)
            .fetch_one(&pool)
            .await
            .expect("approval row");
    let meta = meta.0.expect("request_metadata");

    // The approver must see WHY, or the queue gets
    // rubber-stamped.
    assert_eq!(meta["policy_name"], json!("payments-threshold"));
    assert_eq!(meta["field"], json!("amount"));
    assert_eq!(meta["observed"], json!(8000.0));
    assert_eq!(meta["threshold"], json!(1000.0));
    assert_eq!(meta["rule"], json!("field_gt"));
    // The fields the decision resolver depends on are never displaced.
    assert_eq!(meta["surface"], json!("mcp"));
}

/// A policy whose OWN severity is `severity`, with a presence guard that
/// denies and a threshold rule that states no severity of its own, so it
/// takes the policy's.
async fn seed_inheriting_policy(pool: &PgPool, rg_id: &str, severity: &str) {
    let rules = json!([
        {"allow_only_if": "field_present", "values": ["amount"], "severity": "deny",
         "message": "transfer_funds called without an amount"},
        {"deny_if": "field_gt", "field": "amount", "value": 1000,
         "message": "Transfers over 1,000 need a person"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-inherit', $1, 'payments-supervised', 'mcp_input', 'transfer_funds', \
                 '*', 'json', NULL, $2::jsonb, true, $3, now(), now())",
    )
    .bind(T)
    .bind(rules.to_string())
    .bind(severity)
    .execute(pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ($1, 'ap-inherit')",
    )
    .bind(rg_id)
    .execute(pool)
    .await
    .expect("bind policy to group");
}

/// A rule that states no severity takes the policy's. When the policy's is
/// `approval_required` the call is held for a person, as it is for a rule
/// that says so itself; it is not refused, and it is not performed.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_rule_that_inherits_approval_required_holds_the_call(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9712).await;
    seed_inheriting_policy(&pool, "rg-band", "approval_required").await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "a policy-level approval_required holds the call: {body}"
    );
    assert_eq!(body["approval_required"], json!(true), "{body}");
    assert!(
        mock.received_requests().await.unwrap().is_empty(),
        "a held call is not performed"
    );
    let id = body["approval_request_id"].as_str().expect("approval id");
    let meta: (Option<Value>,) =
        sqlx::query_as("SELECT request_metadata FROM tool_approval_requests WHERE id = $1")
            .bind(id)
            .fetch_one(&pool)
            .await
            .expect("approval row");
    let meta = meta.0.expect("request_metadata");
    assert_eq!(meta["policy_name"], json!("payments-supervised"));
    assert_eq!(meta["observed"], json!(8000.0));
    assert_eq!(meta["threshold"], json!(1000.0));

    // A rule's own severity still overrides the policy's: the guard denies.
    let no_amount = json!({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {}}
    });
    let (status, body) = call(&pool, &mock.uri(), &key, no_amount).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");

    // Below the threshold nothing trips and the call is performed.
    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(500))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 1);
}

/// The other two policy-level severities, for contrast: `deny` refuses and
/// `warn` lets the call through.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_rule_that_inherits_deny_refuses_and_one_that_inherits_warn_passes(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9713).await;
    seed_inheriting_policy(&pool, "rg-band", "deny").await;
    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert!(mock.received_requests().await.unwrap().is_empty());

    sqlx::query("UPDATE argument_policies SET severity = 'warn' WHERE id = 'ap-inherit'")
        .execute(&pool)
        .await
        .expect("loosen the policy");
    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let held: (i64,) = sqlx::query_as("SELECT count(*) FROM tool_approval_requests")
        .fetch_one(&pool)
        .await
        .expect("count");
    assert_eq!(held.0, 0, "neither severity raises a hold");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_transfer_above_the_band_is_refused_outright(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9703).await;
    seed_band_policy(&pool, "rg-band").await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(50000))).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "above the band must deny: {body}"
    );
    let rendered = body.to_string();
    assert!(
        rendered.contains("payments-threshold"),
        "the denial must name the policy: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn omitting_the_amount_does_not_slip_past_the_threshold(pool: PgPool) {
    // THE BYPASS. A comparison predicate reading a missing field does not
    // hold, so a policy carrying only a threshold would let this through.
    // The shipped shape carries a presence rule first, and the control-plane
    // validator refuses to save one that does not.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9704).await;
    seed_band_policy(&pool, "rg-band").await;

    let body = json!({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {}}
    });
    let (status, resp) = call(&pool, &mock.uri(), &key, body).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "omitting the compared field must be refused, not allowed: {resp}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_quoted_amount_is_still_compared(pool: PgPool) {
    // LLM tool calls routinely emit `{"amount": "8000.00"}`. A policy that
    // silently stops applying because the model quoted the number is worse
    // than one that compares a coerced value.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9705).await;
    seed_band_policy(&pool, "rg-band").await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!("8000.00"))).await;
    assert_eq!(
        status,
        StatusCode::ACCEPTED,
        "a quoted number must still trip the band: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approving_one_amount_does_not_authorise_a_larger_one(pool: PgPool) {
    // Through the argument-policy path: the issued token is
    // bound to the arguments the approver saw, so approving 8,000 and
    // retrying at 80,000 is refused.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9706).await;
    seed_band_policy(&pool, "rg-band").await;

    let (_, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    let id = body["approval_request_id"].as_str().expect("approval id");

    // Approve it directly — the resolver path has its own tests.
    sqlx::query(
        "UPDATE tool_approval_requests \
         SET status = 'approved', approval_token = 'tok-band-1' WHERE id = $1",
    )
    .bind(id)
    .execute(&pool)
    .await
    .expect("approve");

    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-band",
        transfer(json!(80000)),
        Some(&key),
        Some(T),
    );
    req.headers_mut()
        .insert("X-Approval-Token", "tok-band-1".parse().unwrap());
    let resp = app.oneshot(req).await.expect("retry");
    let status = resp.status();
    let retry_body = body_to_json(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a token approved for 8000 must not authorise 80000: {retry_body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_band_and_a_capability_filter_produce_one_approval_not_two(pool: PgPool) {
    // The two planes that can demand approval must merge and
    // act once, or one call puts two entries in the operator's queue.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9707).await;
    seed_band_policy(&pool, "rg-band").await;
    sqlx::query(
        "INSERT INTO resource_group_mcp_capabilities \
         (id, tenant_id, resource_group_id, server_config_id, capability_type, \
          capability_name, access, approval_timeout_seconds, created_at, updated_at) \
         VALUES ('cap-band', $1, 'rg-band', 'sc-band', 'tool', 'transfer_funds', \
                 'approval_required', 600, now(), now()) ON CONFLICT DO NOTHING",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("seed capability filter");

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");

    let count: (i64,) = sqlx::query_as(
        "SELECT COUNT(*) FROM tool_approval_requests WHERE tenant_id = $1 \
         AND capability_name = 'transfer_funds'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("count approvals");
    assert_eq!(
        count.0, 1,
        "one call must produce exactly one approval entry"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_band_on_an_enabled_tool_holds_for_the_filter_rows_window(pool: PgPool) {
    // The tool stays `enabled` — no hold of its own — but the operator set
    // approval_timeout_seconds on the row so a band-only hold gives the
    // approver a day, not five minutes. Before this, the enabled row's
    // window was discarded and every band hold expired at 300 s.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9709).await;
    seed_band_policy(&pool, "rg-band").await;
    common::set_mcp_capability_filter_with_timeout(
        &pool,
        T,
        "rg-band",
        "sc-band",
        "tool",
        "transfer_funds",
        "enabled",
        Some(86_400),
    )
    .await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(
        body["timeout_seconds"],
        json!(86_400),
        "the hold must use the enabled row's window: {body}"
    );

    // The persisted request carries the same window, so expiry sweeps and
    // the portal inbox agree with the envelope.
    let id = body["approval_request_id"].as_str().expect("approval id");
    let stored: (i32,) =
        sqlx::query_as("SELECT timeout_seconds FROM tool_approval_requests WHERE id = $1")
            .bind(id)
            .fetch_one(&pool)
            .await
            .expect("approval row");
    assert_eq!(stored.0, 86_400);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_band_on_an_enabled_tool_without_a_window_uses_the_default(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9710).await;
    seed_band_policy(&pool, "rg-band").await;
    common::set_mcp_capability_filter(
        &pool,
        T,
        "rg-band",
        "sc-band",
        "tool",
        "transfer_funds",
        "enabled",
    )
    .await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["timeout_seconds"], json!(300), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_band_on_an_enabled_tool_without_a_window_takes_the_groups_window(pool: PgPool) {
    // No filter row states a window, so the hold takes the group's
    // default instead of the old literal 300 s.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9711).await;
    seed_band_policy(&pool, "rg-band").await;
    common::set_mcp_capability_filter(
        &pool,
        T,
        "rg-band",
        "sc-band",
        "tool",
        "transfer_funds",
        "enabled",
    )
    .await;
    common::set_rg_approval_window(&pool, "rg-band", Some(86_400)).await;

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["timeout_seconds"], json!(86_400), "{body}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_capability_filter_alone_still_uses_its_own_timeout(pool: PgPool) {
    // The merge must not change what the capability plane does on its own.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9708).await;
    sqlx::query(
        "INSERT INTO resource_group_mcp_capabilities \
         (id, tenant_id, resource_group_id, server_config_id, capability_type, \
          capability_name, access, approval_timeout_seconds, created_at, updated_at) \
         VALUES ('cap-solo', $1, 'rg-band', 'sc-band', 'tool', 'transfer_funds', \
                 'approval_required', 600, now(), now()) ON CONFLICT DO NOTHING",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("seed capability filter");

    let (status, body) = call(&pool, &mock.uri(), &key, transfer(json!(8000))).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let id = body["approval_request_id"].as_str().expect("approval id");
    let meta: (Option<Value>,) =
        sqlx::query_as("SELECT request_metadata FROM tool_approval_requests WHERE id = $1")
            .bind(id)
            .fetch_one(&pool)
            .await
            .expect("row");
    assert!(
        meta.0.expect("metadata").get("policy_name").is_none(),
        "no argument policy fired, so no band metadata should be attached"
    );
}

// ===========================================================================
// Undecidable input, the analysis caps and the `policy_unavailable` arm
// ===========================================================================

/// An upstream that must never be reached, and the seeded band.
async fn refused_world(pool: &PgPool, user_id: i32) -> (MockServer, String) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"unused": true})))
        .expect(0)
        .mount(&mock)
        .await;
    let key = seed_topology(pool, &mock.uri(), user_id).await;
    (mock, key)
}

fn refusal_rule(body: &Value) -> &Value {
    &body["error"]["data"]["argument_policy"]["rule"]
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn padding_before_the_amount_is_refused_not_allowed(pool: PgPool) {
    // 512 padding leaves under a key that sorts
    // before `amount` exhaust the flattener's leaf cap, so `amount` is never
    // recorded as a number — while it is still a top-level key, so the
    // presence guard passes. The threshold then compared nothing and a
    // 20,000 transfer went through.
    let (mock, key) = refused_world(&pool, 9720).await;
    seed_band_policy(&pool, "rg-band").await;

    let mut pad = serde_json::Map::new();
    for i in 0..512 {
        pad.insert(format!("k{i:03}"), json!(0));
    }
    let body = json!({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {"_": pad, "amount": 20000}}
    });
    let (status, resp) = call(&pool, &mock.uri(), &key, body).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "an argument too large to analyze completely must be refused: {resp}"
    );
    assert_eq!(refusal_rule(&resp), &json!("arguments_truncated"), "{resp}");
}

/// A guarded threshold on `transfer_funds` with `config` as its
/// `analyzer_config`.
async fn seed_policy_with_config(pool: &PgPool, id: &str, config: Value) {
    let rules = json!([
        {"allow_only_if": "field_present", "values": ["amount"], "severity": "deny"},
        {"deny_if": "field_gt", "field": "amount", "value": 10000, "severity": "deny",
         "message": "Transfers over 10,000 are not permitted from this system"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ($1, $2, $1, 'mcp_input', 'transfer_funds', \
                 '*', 'json', $3::jsonb, $4::jsonb, true, 'deny', now(), now())",
    )
    .bind(id)
    .bind(T)
    .bind(config.to_string())
    .bind(rules.to_string())
    .execute(pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ('rg-band', $1)",
    )
    .bind(id)
    .execute(pool)
    .await
    .expect("bind policy to group");
}

/// A `transfer_funds` call whose `amount` comes after `padding` leaves.
fn padded_transfer(padding: usize, amount: i64) -> Value {
    let pad: serde_json::Map<String, Value> = (0..padding)
        .map(|i| (format!("k{i:05}"), json!(0)))
        .collect();
    json!({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "transfer_funds", "arguments": {"_": pad, "amount": amount}}
    })
}

/// A policy that states `max_leaves` judges an argument larger than the
/// default limit: a small transfer is performed, a large one is refused by
/// the threshold (not as too large), and beyond the stated limit the call
/// is refused as too large.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_that_states_its_limits_judges_a_larger_argument(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"ok": true}})),
        )
        .mount(&mock)
        .await;
    let key = seed_topology(&pool, &mock.uri(), 9725).await;
    seed_policy_with_config(&pool, "ap-roomy", json!({"max_leaves": 1500})).await;

    let (status, resp) = call(&pool, &mock.uri(), &key, padded_transfer(1000, 500)).await;
    assert_eq!(
        status,
        StatusCode::OK,
        "1,001 leaves under a limit of 1,500: {resp}"
    );
    assert_eq!(mock.received_requests().await.unwrap().len(), 1);

    let (status, resp) = call(&pool, &mock.uri(), &key, padded_transfer(1000, 20000)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{resp}");
    assert_eq!(refusal_rule(&resp), &json!("field_gt"), "{resp}");

    let (status, resp) = call(&pool, &mock.uri(), &key, padded_transfer(1500, 500)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{resp}");
    assert_eq!(refusal_rule(&resp), &json!("arguments_truncated"), "{resp}");
    assert_eq!(mock.received_requests().await.unwrap().len(), 1);
}

/// Two policies on one tool analyse under their own limits each: the call
/// has to fit both, so the policy with the default limits refuses what its
/// neighbour has room for.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_second_policy_with_default_limits_still_refuses_the_larger_argument(pool: PgPool) {
    let (mock, key) = refused_world(&pool, 9726).await;
    seed_policy_with_config(&pool, "ap-roomy", json!({"max_leaves": 1500})).await;
    seed_policy_with_config(&pool, "ap-default", json!({})).await;

    let (status, resp) = call(&pool, &mock.uri(), &key, padded_transfer(1000, 500)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{resp}");
    assert_eq!(refusal_rule(&resp), &json!("arguments_truncated"), "{resp}");
    assert_eq!(
        resp["error"]["data"]["argument_policy"]["policy"],
        json!("ap-default"),
        "{resp}"
    );
}

/// A limit over its ceiling, zero, negative or not an integer makes the
/// policy one that cannot be loaded: the call is refused, whatever its
/// arguments. It is not analysed under the nearest acceptable limit.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_with_an_unacceptable_limit_cannot_be_loaded(pool: PgPool) {
    let (mock, key) = refused_world(&pool, 9727).await;
    seed_policy_with_config(&pool, "ap-odd", json!({"max_leaves": 1})).await;
    for config in [
        json!({"max_leaves": 1_000_000}),
        json!({"max_depth": 0}),
        json!({"max_array_elements": -5}),
        json!({"max_depth": 12.5}),
        json!({"max_leaves": "2000"}),
    ] {
        sqlx::query("UPDATE argument_policies SET analyzer_config = $1::jsonb WHERE id = 'ap-odd'")
            .bind(config.to_string())
            .execute(&pool)
            .await
            .expect("set the config");
        let (status, resp) = call(&pool, &mock.uri(), &key, transfer(json!(5))).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{config}: {resp}");
        assert_eq!(
            refusal_rule(&resp),
            &json!("policy_unavailable"),
            "{config}: {resp}"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_amount_that_is_present_but_not_a_number_is_refused(pool: PgPool) {
    // `{"amount": null}` (or a bool, an array, an
    // object, "20,000") satisfies the presence guard and never entered the
    // number map, so no threshold fired.
    let (mock, key) = refused_world(&pool, 9721).await;
    seed_band_policy(&pool, "rg-band").await;

    for amount in [json!(null), json!([20000]), json!("20,000")] {
        let (status, resp) = call(&pool, &mock.uri(), &key, transfer(amount.clone())).await;
        assert_eq!(
            status,
            StatusCode::FORBIDDEN,
            "amount {amount} must be refused, not allowed: {resp}"
        );
        assert_eq!(refusal_rule(&resp), &json!("field_gt"), "{resp}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_policy_whose_rules_cannot_be_decoded_refuses_the_call(pool: PgPool) {
    // One rule this build cannot decode (here a
    // severity it does not know) used to turn the whole policy into ZERO
    // rules, silently: it matched nothing and the 50,000 transfer passed.
    let (mock, key) = refused_world(&pool, 9722).await;
    let rules = json!([
        {"deny_if": "field_gt", "field": "amount", "value": 1000, "severity": "block",
         "message": "Transfers over 1,000 are not permitted"}
    ]);
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, analyzer, \
          analyzer_config, rules, enabled, severity, created_at, updated_at) \
         VALUES ('ap-undecodable', $1, 'undecodable', 'mcp_input', 'transfer_funds', \
                 '*', 'json', NULL, $2::jsonb, true, 'deny', now(), now())",
    )
    .bind(T)
    .bind(rules.to_string())
    .execute(&pool)
    .await
    .expect("seed policy");
    sqlx::query(
        "INSERT INTO resource_group_argument_policies (resource_group_id, argument_policy_id) \
         VALUES ('rg-band', 'ap-undecodable')",
    )
    .execute(&pool)
    .await
    .expect("bind policy to group");

    let (status, resp) = call(&pool, &mock.uri(), &key, transfer(json!(50000))).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "a policy that cannot be decoded is not a policy with no rules: {resp}"
    );
    assert_eq!(refusal_rule(&resp), &json!("policy_unavailable"), "{resp}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn unreadable_argument_policies_refuse_the_call(pool: PgPool) {
    // Pins the existing fail-closed arm: with the policy tables
    // unreadable the call cannot be checked, so it is refused — even a
    // transfer below every band — and the refusal says why.
    let (mock, key) = refused_world(&pool, 9723).await;
    seed_band_policy(&pool, "rg-band").await;
    crate::db_error_fail_closed::make_unreadable(&pool, "argument_policies").await;

    let (status, resp) = call(&pool, &mock.uri(), &key, transfer(json!(500))).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{resp}");
    assert_eq!(refusal_rule(&resp), &json!("policy_unavailable"), "{resp}");
}

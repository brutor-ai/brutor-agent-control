// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Run data scope, through the MCP route against real Postgres.
//!
//! The case: a support agent's run reads another
//! customer's record with the same tools, in the same order, as a good run.
//! These tests pin that the gateway tells them apart by the argument, and
//! that `enforce` stops the call before it reaches the tool.

use axum::http::{HeaderValue, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::data_scope;

use crate::common;

const T: &str = "tenant-scope";
const RG: &str = "rg-scope";

async fn topology(pool: &PgPool, mock_uri: &str) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9801, T).await;
    common::add_direct_membership(pool, 9801, RG, T).await;
    common::seed_mcp_server_with_base_url(pool, "ms-scope", T, "crm", mock_uri).await;
    common::seed_server_config(pool, "sc-scope", T, "ms-scope").await;
    common::bind_server_config_to_rg(pool, "sc-scope", RG).await;
    common::seed_api_key(pool, T, RG, Some(9801), "scope-key").await
}

async fn configure(pool: &PgPool, mode: &str, rules: Value) {
    data_scope::invalidate_all();
    sqlx::query(
        "INSERT INTO ai_system_data_scopes \
            (id, tenant_id, ai_system_id, mode, on_unbound, keys, rules, unscoped_tools) \
         VALUES ('ds-1', $1, $2, $3, 'deny', $4, $5, $6) \
         ON CONFLICT (tenant_id, ai_system_id) DO UPDATE SET mode = EXCLUDED.mode, \
            rules = EXCLUDED.rules",
    )
    .bind(T)
    .bind(RG)
    .bind(mode)
    .bind(json!([{"key": "customer", "source": "declared"}]))
    .bind(rules)
    .bind(json!(["crm_list_customers"]))
    .execute(pool)
    .await
    .expect("seed data scope");
}

fn customer_rule() -> Value {
    json!([{"tool": "crm_get_customer", "argument": "customer_id", "key": "customer"}])
}

fn get_customer(id: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "crm_get_customer", "arguments": {"customer_id": id}}})
}

async fn call(
    pool: &PgPool,
    key: &str,
    run: &str,
    scope: Option<&str>,
    body: Value,
) -> (StatusCode, Value) {
    call_at(pool, key, run, scope, "ms-scope", body).await
}

async fn call_at(
    pool: &PgPool,
    key: &str,
    run: &str,
    scope: Option<&str>,
    server: &str,
    body: Value,
) -> (StatusCode, Value) {
    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);
    let mut req =
        common::build_post_request(&format!("/v1/proxy/mcp/{server}"), body, Some(key), Some(T));
    req.headers_mut()
        .insert("x-brutor-run-id", HeaderValue::from_str(run).unwrap());
    if let Some(s) = scope {
        req.headers_mut()
            .insert(data_scope::SCOPE_HEADER, HeaderValue::from_str(s).unwrap());
    }
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.unwrap().to_bytes();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn upstream(expected_calls: u64) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})),
        )
        .expect(expected_calls)
        .mount(&mock)
        .await;
    mock
}

/// Verdicts recorded in this test's tenant. The stored run id is derived from
/// the credential and the `x-brutor-run-id` header (so a caller cannot steer
/// its calls into someone else's run), so rows are found by tenant: each test
/// makes one run.
async fn verdicts(pool: &PgPool, _run: &str) -> Vec<String> {
    sqlx::query_as::<_, (String,)>(
        "SELECT verdict FROM ai_system_record_access WHERE tenant_id = $1 \
          ORDER BY accessed_at, id",
    )
    .bind(T)
    .fetch_all(pool)
    .await
    .unwrap()
    .into_iter()
    .map(|(v,)| v)
    .collect()
}

fn run_id() -> String {
    ulid::Ulid::new().to_string()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn enforce_lets_the_runs_own_record_through_and_stops_another(pool: PgPool) {
    let mock = upstream(1).await;
    let key = topology(&pool, &mock.uri()).await;
    configure(&pool, "enforce", customer_rule()).await;
    let run = run_id();

    let (ok, body) = call(
        &pool,
        &key,
        &run,
        Some("customer=C0001"),
        get_customer("C0001"),
    )
    .await;
    assert_eq!(ok, StatusCode::OK, "{body}");

    // Same tool, same argument name, same run: someone else's record.
    let (status, body) = call(&pool, &key, &run, None, get_customer("C0002")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body["error"]["data"]["data_scope"]["verdict"],
        "out_of_scope"
    );
    assert_eq!(
        verdicts(&pool, &run).await,
        vec!["in_scope", "out_of_scope"]
    );

    let refs: Vec<(Option<String>,)> =
        sqlx::query_as("SELECT record_ref FROM ai_system_record_access WHERE tenant_id = $1")
            .bind(T)
            .fetch_all(&pool)
            .await
            .unwrap();
    assert!(
        refs.iter().all(|(r,)| r
            .as_deref()
            .is_some_and(|r| r.len() == 64 && !r.contains("C000"))),
        "references, never the value: {refs:?}"
    );

    let (sev,): (String,) = sqlx::query_as(
        "SELECT severity FROM assurance_inbox_items WHERE tenant_id = $1 AND category = 'scope_violation'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(sev, "critical");

    let (root,): (String,) = sqlx::query_as(
        "SELECT DISTINCT root_task_id FROM ai_system_record_access WHERE tenant_id = $1",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("both calls are one run");
    let mut conn = pool.acquire().await.unwrap();
    let facts = data_scope::load_run_facts(&mut conn, T, &root)
        .await
        .unwrap();
    assert!(facts.scope_declared);
    assert_eq!(facts.records_touched, 2);
    assert_eq!(facts.scope_violation_count, 1);
    mock.verify().await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn audit_records_the_same_call_and_lets_it_through(pool: PgPool) {
    let mock = upstream(1).await;
    let key = topology(&pool, &mock.uri()).await;
    configure(&pool, "audit", customer_rule()).await;
    let run = run_id();
    let (status, _) = call(
        &pool,
        &key,
        &run,
        Some("customer=C0001"),
        get_customer("C0002"),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(verdicts(&pool, &run).await, vec!["out_of_scope"]);
    let (sev,): (String,) = sqlx::query_as(
        "SELECT severity FROM assurance_inbox_items WHERE tenant_id = $1 AND category = 'scope_violation'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(sev, "warning");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_that_never_says_whom_it_serves_is_refused_in_enforce(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri()).await;
    configure(&pool, "enforce", customer_rule()).await;
    let run = run_id();
    let (status, body) = call(&pool, &key, &run, None, get_customer("C0001")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(body["error"]["data"]["data_scope"]["verdict"], "unbound");
    mock.verify().await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_first_declaration_binds_and_a_later_one_cannot_widen_it(pool: PgPool) {
    let mock = upstream(1).await;
    let key = topology(&pool, &mock.uri()).await;
    configure(&pool, "enforce", customer_rule()).await;
    let run = run_id();
    let (ok, _) = call(
        &pool,
        &key,
        &run,
        Some("customer=C0001"),
        get_customer("C0001"),
    )
    .await;
    assert_eq!(ok, StatusCode::OK);
    // A model persuaded to have its runtime re-declare the scope.
    let (status, body) = call(
        &pool,
        &key,
        &run,
        Some("customer=C0002"),
        get_customer("C0002"),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    let (values,): (Value,) = sqlx::query_as(
        "SELECT scope_values FROM ai_system_run_scopes WHERE tenant_id = $1 AND scope_key = 'customer'",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(values, json!(["C0001"]), "the binding is unchanged");
    assert!(
        verdicts(&pool, &run)
            .await
            .contains(&"conflict".to_string())
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_list_tool_inside_a_scoped_run_is_refused(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri()).await;
    configure(&pool, "enforce", customer_rule()).await;
    let run = run_id();
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "crm_list_customers", "arguments": {"limit": 5}}});
    let (status, resp) = call(&pool, &key, &run, Some("customer=C0001"), body).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{resp}");
    assert_eq!(
        resp["error"]["data"]["data_scope"]["verdict"],
        "unscoped_tool"
    );
    mock.verify().await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_unreadable_config_refuses_rather_than_passing(pool: PgPool) {
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri()).await;
    // A config this build cannot read is not "no config".
    configure(&pool, "enforce", json!({"not": "a list"})).await;
    let run = run_id();
    let (status, body) = call(
        &pool,
        &key,
        &run,
        Some("customer=C0001"),
        get_customer("C0001"),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body["error"]["data"]["data_scope"]["verdict"],
        "config_unavailable"
    );
    mock.verify().await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_without_a_config_is_untouched(pool: PgPool) {
    let mock = upstream(1).await;
    let key = topology(&pool, &mock.uri()).await;
    data_scope::invalidate_all();
    let run = run_id();
    let (status, _) = call(&pool, &key, &run, None, get_customer("C0002")).await;
    assert_eq!(status, StatusCode::OK);
    assert!(verdicts(&pool, &run).await.is_empty());
}

/// A system server's tools are MCP tools like any other: a
/// run bound to one record is judged there too. The system-server branch
/// left the route before the data-scope gate, so a rule on `search__web`,
/// `skills__load` or any other system tool was never applied (only a
/// script run was judged, inside `execute_skill`, under the skill's name).
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_system_server_tool_is_judged_against_the_runs_data_scope(pool: PgPool) {
    use brutor_gateway_core::constants::system_server::agent_skill as ask;
    let mock = upstream(0).await;
    let key = topology(&pool, &mock.uri()).await;
    common::seed_mcp_server_with_base_url(&pool, "ms-scope-skills", T, "skills", ask::BASE_URL)
        .await;
    common::seed_server_config(&pool, "sc-scope-skills", T, "ms-scope-skills").await;
    common::bind_server_config_to_rg(&pool, "sc-scope-skills", RG).await;
    configure(
        &pool,
        "enforce",
        json!([{"tool": ask::TOOL_LOAD, "argument": "skill_name", "key": "customer"}]),
    )
    .await;
    let run = run_id();
    let load = |name: &str| {
        json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": ask::TOOL_LOAD, "arguments": {"skill_name": name}}})
    };

    let (status, body) = call_at(
        &pool,
        &key,
        &run,
        Some("customer=C0001"),
        "ms-scope-skills",
        load("C0001"),
    )
    .await;
    assert_eq!(
        status,
        StatusCode::OK,
        "the run's own record passes: {body}"
    );

    let (status, body) = call_at(&pool, &key, &run, None, "ms-scope-skills", load("C0002")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body["error"]["data"]["data_scope"]["verdict"], "out_of_scope",
        "{body}"
    );
    assert_eq!(
        verdicts(&pool, &run).await,
        vec!["in_scope", "out_of_scope"]
    );

    // A tool the system lists as unscoped is refused inside a scoped run.
    sqlx::query("UPDATE ai_system_data_scopes SET unscoped_tools = $1 WHERE tenant_id = $2")
        .bind(json!([ask::TOOL_LIST]))
        .bind(T)
        .execute(&pool)
        .await
        .unwrap();
    data_scope::invalidate_all();
    let list = json!({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": ask::TOOL_LIST, "arguments": {}}});
    let (status, body) = call_at(&pool, &key, &run, None, "ms-scope-skills", list).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
    assert_eq!(
        body["error"]["data"]["data_scope"]["verdict"], "unscoped_tool",
        "{body}"
    );
}

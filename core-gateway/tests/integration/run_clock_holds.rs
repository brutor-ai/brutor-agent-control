// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The live run clock, end to end on the MCP surface.
//!
//! `max_duration_seconds_per_run` bounds the AGENT's own execution. Two
//! defects, one per test group:
//!
//! 1. **Time parked on a human counted as runtime.** A call held for approval
//!    is retried on the SAME run once a person decides. The run budget gate
//!    measured `now − t0`, so an approval slower than the ceiling refused the
//!    very retry it approved. The retry reaches the gate BEFORE its token is
//!    redeemed, so the gate reconciles the pause from `tool_approval_requests`
//!    itself.
//! 2. **Agent-keyed MCP calls skipped the run caps entirely.** The run budget
//!    and run-abort checks sat in the non-agent branch of `routes/mcp.rs`,
//!    beside the autonomy gate. An agent principal — the caller the run budget
//!    exists for — was never checked. Every test here runs on both paths, so
//!    either path skipping the cap fails a test.
//!
//! No test races the wall clock. The ceiling is 60 s, and a run is put past
//! it by construction: its clock start is moved two minutes into the past
//! (`UsageState::run_backdate_start`) and, for a held call, so is the hold's
//! `created_at` — the person "took two minutes". What each test proves is
//! unchanged (runaway refused; approved retry allowed; the pause stays off
//! the clock), but the margin between "inside" and "past" the ceiling is
//! ~a minute of execution, not the ~300 ms a loaded CI box would eat. The
//! previous 1 s ceiling / 1.3 s sleep failed under parallel load.

use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::method;
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::services::SharedState;

use crate::common;

const TENANT: &str = "t-rclock";
const RG: &str = "rg-rclock";
const TOOL: &str = "send_email";
/// The run's duration ceiling.
const CEILING_SECS: i64 = 60;
/// How far past the ceiling a test puts the run (by construction, not sleep).
const AGED_MS: i64 = 120_000;

#[derive(Clone, Copy, Debug)]
enum Caller {
    /// A plain API key — the non-agent branch.
    ApiKey,
    /// An agent-bound API key — the agent-authz branch.
    Agent,
}

/// Tenant, group, MCP server bound to the group, a key, and a 1 s duration
/// ceiling. `hold` makes the tool `approval_required` on the plane that
/// governs this caller: the RG capability filter for an API key, the agent
/// grant for an agent (whose grant is otherwise `allow`).
async fn topology(pool: &PgPool, mock_uri: &str, caller: Caller, hold: bool) -> String {
    common::seed_tenant(pool, TENANT).await;
    common::seed_resource_group(pool, RG, TENANT).await;
    common::seed_end_user(pool, 9701, TENANT).await;
    common::add_direct_membership(pool, 9701, RG, TENANT).await;
    common::seed_mcp_server_with_base_url(pool, "ms-rclock", TENANT, "rclocksrv", mock_uri).await;
    common::seed_server_config(pool, "sc-rclock", TENANT, "ms-rclock").await;
    common::bind_server_config_to_rg(pool, "sc-rclock", RG).await;
    let key = common::seed_api_key(pool, TENANT, RG, Some(9701), "rclock-key").await;
    common::set_rg_operating_envelope(
        pool,
        RG,
        json!({"max_duration_seconds_per_run": CEILING_SECS}),
    )
    .await;

    match caller {
        Caller::ApiKey => {
            if hold {
                common::set_mcp_capability_filter(
                    pool,
                    TENANT,
                    RG,
                    "sc-rclock",
                    "tool",
                    TOOL,
                    "approval_required",
                )
                .await;
            }
        }
        Caller::Agent => {
            sqlx::query(
                "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
                 VALUES ('agent-rc', $1, 'agent-rc', 'manual', true, 'active', true)",
            )
            .bind(TENANT)
            .execute(pool)
            .await
            .expect("agent identity");
            sqlx::query(
                "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
                 VALUES ('g-rc', $1, 'agent-rc', 'mcp_tool', $2, $3, true)",
            )
            .bind(TENANT)
            .bind(TOOL)
            .bind(if hold { "approval_required" } else { "allow" })
            .execute(pool)
            .await
            .expect("agent grant");
            sqlx::query("UPDATE api_keys SET agent_id = 'agent-rc' WHERE tenant_id = $1 AND name = 'rclock-key'")
                .bind(TENANT)
                .execute(pool)
                .await
                .expect("bind key to agent");
        }
    }
    key
}

async fn mock_upstream() -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "sent"}]}
        })))
        .mount(&mock)
        .await;
    mock
}

/// One `tools/call` on `run`, optionally redeeming an approval token.
fn call(key: &str, run: &str, token: Option<&str>) -> Request<Body> {
    let mut req = common::build_post_request(
        "/v1/proxy/mcp/ms-rclock",
        json!({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": TOOL, "arguments": {"to": "cfo@example.com"}}
        }),
        Some(key),
        Some(TENANT),
    );
    req.headers_mut()
        .insert("x-brutor-run-id", run.parse().expect("run header"));
    if let Some(t) = token {
        req.headers_mut()
            .insert("x-approval-token", t.parse().expect("token header"));
    }
    req
}

/// The `root_task_id` the gateway derives for this key's asserted run — the
/// id the run clock is keyed on.
fn run_root(state: &SharedState, key: &str, run: &str) -> String {
    let tenant_key = brutor_gateway_core::services::call_chain::derive_tenant_key(
        state.config.encryption_key.as_bytes(),
        TENANT,
    );
    let auth = format!("Bearer {key}");
    let get = |n: &str| match n {
        "authorization" => Some(auth.clone()),
        "x-brutor-run-id" => Some(run.to_string()),
        _ => None,
    };
    brutor_gateway_core::services::call_chain::asserted_root(&get, &tenant_key)
        .expect("an authenticated caller may assert its run")
        .to_string()
}

/// The run has been open `AGED_MS` longer than it really has.
async fn age_the_run(state: &SharedState, key: &str, run: &str) {
    state
        .usage_state
        .run_backdate_start(TENANT, &run_root(state, key, run), AGED_MS)
        .await;
}

async fn send(state: &SharedState, req: Request<Body>) -> StatusCode {
    common::build_mcp_proxy_app(state.clone())
        .oneshot(req)
        .await
        .expect("router call")
        .status()
}

/// A run that genuinely ran past its ceiling, with no hold, is refused — on
/// the path under test. The agent path answers the same.
async fn runaway_is_refused(pool: PgPool, caller: Caller) {
    let mock = mock_upstream().await;
    let key = topology(&pool, &mock.uri(), caller, false).await;
    let state = common::build_test_state(pool.clone());
    let run = ulid::Ulid::new().to_string();

    assert_eq!(
        send(&state, call(&key, &run, None)).await,
        StatusCode::OK,
        "{caller:?}: first call is inside the ceiling"
    );
    // The run has now been executing two minutes — past its 60 s ceiling.
    age_the_run(&state, &key, &run).await;
    assert_eq!(
        send(&state, call(&key, &run, None)).await,
        StatusCode::TOO_MANY_REQUESTS,
        "{caller:?}: the run has executed past max_duration_seconds_per_run with no hold — \
         a 200 means this path skips the run budget"
    );

    // The refusal is audited as a run-cap refusal, not a failed call.
    let mut subtype: Option<String> = None;
    for _ in 0..40 {
        subtype = sqlx::query_scalar(
            "SELECT proxy_subtype FROM proxy_logs WHERE tenant_id = $1 \
             AND proxy_subtype = 'run_cap_reached' LIMIT 1",
        )
        .bind(TENANT)
        .fetch_optional(&pool)
        .await
        .unwrap();
        if subtype.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(subtype.as_deref(), Some("run_cap_reached"), "{caller:?}");
}

/// The approved retry of a call held past the ceiling is ALLOWED: the time
/// awaiting the approver is not the run's runtime.
async fn approved_retry_is_allowed(pool: PgPool, caller: Caller) {
    let mock = mock_upstream().await;
    let key = topology(&pool, &mock.uri(), caller, true).await;
    let state = common::build_test_state(pool.clone());
    let run = ulid::Ulid::new().to_string();

    let held = send(&state, call(&key, &run, None)).await;
    assert_eq!(
        held,
        StatusCode::ACCEPTED,
        "{caller:?}: the call is held for a human"
    );
    let approval_id: String = sqlx::query_scalar(
        "SELECT id FROM tool_approval_requests WHERE tenant_id = $1 AND status = 'pending'",
    )
    .bind(TENANT)
    .fetch_one(&pool)
    .await
    .expect("the hold was enqueued");

    // The person takes two minutes — longer than the whole run is allowed to
    // execute: the run's clock AND the hold both started two minutes ago.
    age_the_run(&state, &key, &run).await;
    sqlx::query(
        "UPDATE tool_approval_requests \
            SET created_at = created_at - make_interval(secs => $2::double precision / 1000) \
          WHERE id = $1",
    )
    .bind(&approval_id)
    .bind(AGED_MS as f64)
    .execute(&pool)
    .await
    .expect("the hold began two minutes ago");
    let token = format!("tok-{}", ulid::Ulid::new());
    sqlx::query(
        "UPDATE tool_approval_requests SET status = 'approved', approval_token = $2, \
         resolved_at = now(), resolved_by = 'approver' WHERE id = $1",
    )
    .bind(&approval_id)
    .bind(&token)
    .execute(&pool)
    .await
    .expect("approve");

    assert_eq!(
        send(&state, call(&key, &run, Some(&token))).await,
        StatusCode::OK,
        "{caller:?}: the approved retry must be served — the run spent the \
         ceiling waiting on a person, not executing"
    );
    let (consumed, token_used): (String, String) =
        sqlx::query_as("SELECT status, token_used FROM tool_approval_requests WHERE id = $1")
            .bind(&approval_id)
            .fetch_one(&pool)
            .await
            .unwrap();
    assert_eq!(
        consumed, "consumed",
        "{caller:?}: the token was actually redeemed"
    );
    assert_eq!(
        token_used, "Y",
        "{caller:?}: redemption records token_used in the same write as the status"
    );

    // The pause stays on the run clock: the next call on the run, immediately
    // after, is still inside the ceiling.
    assert_eq!(
        send(&state, call(&key, &run, None)).await,
        StatusCode::ACCEPTED,
        "{caller:?}: a fresh call on the same run is held again (not refused by the run cap)"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn runaway_past_the_ceiling_is_refused_for_an_api_key(pool: PgPool) {
    runaway_is_refused(pool, Caller::ApiKey).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn runaway_past_the_ceiling_is_refused_for_an_agent(pool: PgPool) {
    runaway_is_refused(pool, Caller::Agent).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approved_retry_after_the_ceiling_is_allowed_for_an_api_key(pool: PgPool) {
    approved_retry_is_allowed(pool, Caller::ApiKey).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn approved_retry_after_the_ceiling_is_allowed_for_an_agent(pool: PgPool) {
    approved_retry_is_allowed(pool, Caller::Agent).await;
}

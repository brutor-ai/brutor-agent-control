// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The limit on new runs per credential (`max_new_runs_per_window`), through
//! the HTTP surfaces, on gateway states whose counters live in one REAL
//! Redis.
//!
//! Every per-run ceiling is a ceiling of ONE run, and a caller chooses its
//! run: it asserts a run id, or sends none and the gateway mints a root per
//! request. A caller that never repeats a run id is under every per-run
//! ceiling for ever. The limit counts the distinct runs one credential (an
//! agent identity, else an API key) STARTS in a window:
//!
//! * every surface refuses the run that is one too many, as it refuses a
//!   rate limit (429, the surface's rate-limit body, `rate_limit_credential`
//!   in the audit row), naming the term; a run already counted continues;
//! * a root the gateway mints counts; a step on a verified delegation chain
//!   continues its run and does not; a chain that does not verify is a
//!   minted root;
//! * the count is the credential's: another key has its own, the keys of
//!   one agent identity share one;
//! * a burst of new runs on two gateway states admits exactly the limit;
//! * the window rolls over; no limit, or a malformed one, counts nothing;
//!   a set that cannot be operated refuses.
//!
//! Needs `TEST_REDIS_URL` (CI provides a service container).

use axum::body::Body;
use axum::http::{HeaderMap, Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::call_chain::{CallChain, derive_tenant_key};
use brutor_gateway_core::services::limits;

use super::run_actions::{actions_key, redis_conn, replica, shape, upstream, world};
use super::run_controls_parity::{self as rc, Caller, Surface, T};
use crate::common;

const TERM: &str = "max_new_runs_per_window";
const SUBTYPE: &str = "rate_limit_credential";

/// How the caller names its run.
#[derive(Clone)]
enum Run {
    /// `x-brutor-run-id`.
    Asserted(String),
    /// No run id: the gateway mints a root for the request.
    None,
    /// A delegation chain of the run `root`, signed with `key` (the tenant's
    /// key verifies; any other does not).
    Chain { root: ulid::Ulid, key: Vec<u8> },
}

fn asserted(run: &str) -> Run {
    Run::Asserted(run.to_string())
}

fn request(surface: Surface, key: &str, run: &Run) -> Request<Body> {
    let mut req = rc::request(surface, key);
    let h = req.headers_mut();
    h.remove("x-brutor-run-id");
    match run {
        Run::Asserted(id) => {
            h.insert("x-brutor-run-id", id.parse().expect("run header"));
        }
        Run::None => {}
        Run::Chain { root, key } => {
            let chain = CallChain::root(*root)
                .fork_child(ulid::Ulid::new())
                .expect("child");
            for (name, value) in chain.to_header_pairs(key) {
                h.insert(name, value.parse().expect("chain header"));
            }
        }
    }
    req
}

async fn send(
    state: &SharedState,
    surface: Surface,
    key: &str,
    run: &Run,
) -> (StatusCode, HeaderMap, Value) {
    let resp = rc::app(state, surface)
        .oneshot(request(surface, key, run))
        .await
        .expect("router call");
    let status = resp.status();
    let headers = resp.headers().clone();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    let body = serde_json::from_slice(&bytes)
        .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).to_string()));
    (status, headers, body)
}

fn refused_as_new(status: StatusCode, body: &Value) -> bool {
    status == StatusCode::TOO_MANY_REQUESTS && body.to_string().contains(TERM)
}

/// The principal an API key's runs are counted for.
async fn key_principal(pool: &PgPool, name: &str) -> String {
    let id: String =
        sqlx::query_scalar("SELECT id FROM api_keys WHERE tenant_id = $1 AND name = $2")
            .bind(T)
            .bind(name)
            .fetch_one(pool)
            .await
            .expect("the key");
    format!("key:{id}")
}

/// How many runs `principal` has started in the current window.
async fn runs_started(principal: &str, window_secs: u64) -> i64 {
    let mut c = redis_conn().await;
    redis::cmd("SCARD")
        .arg(limits::new_runs_key(T, principal, window_secs))
        .query_async(&mut c)
        .await
        .expect("SCARD")
}

/// Every set of started runs of the tenant's principals named like `like`.
async fn run_sets(like: &str) -> Vec<String> {
    let mut c = redis_conn().await;
    redis::cmd("KEYS")
        .arg(format!("brutor:runs:{T}:{like}:*"))
        .query_async(&mut c)
        .await
        .expect("KEYS")
}

/// The refusal rows of a new run: (root, error message).
async fn refusal_rows(pool: &PgPool, want: usize) -> Vec<(Option<String>, Option<String>)> {
    let mut rows = Vec::new();
    for _ in 0..80 {
        rows = sqlx::query_as(
            "SELECT root_task_id, error_message FROM proxy_logs \
             WHERE tenant_id = $1 AND proxy_subtype = $2 ORDER BY timestamp",
        )
        .bind(T)
        .bind(SUBTYPE)
        .fetch_all(pool)
        .await
        .expect("proxy_logs query");
        if rows.len() >= want {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    rows
}

// ---------------------------------------------------------------------------
// Every surface refuses the run that is one too many, as a rate limit
// ---------------------------------------------------------------------------

/// The surface's own rate limit, so that its refusal can be compared: the
/// group allows one call an hour.
async fn arm_the_sibling_rate_limit(pool: &PgPool, rg: &str, surface: Surface) -> bool {
    let limit = json!({"frequency": {"max_calls_per_hour": 1}});
    let column = match surface {
        Surface::Mcp
        | Surface::SkillServerList
        | Surface::SkillServerRunScript
        | Surface::SearchServerWeb => "mcp_global_limits",
        Surface::LlmChat | Surface::LlmMessages | Surface::Embeddings => "llm_global_limits",
        Surface::A2aOutbound | Surface::A2aInbound | Surface::A2aV1 => "a2a_global_limits",
        // The skill route has no rate limit of its own.
        _ => return false,
    };
    sqlx::query(&format!(
        "UPDATE resource_groups SET {column} = $2::json WHERE id = $1"
    ))
    .bind(rg)
    .bind(limit.to_string())
    .execute(pool)
    .await
    .expect("set the sibling limit");
    true
}

async fn refuses_the_run_that_is_one_too_many(pool: PgPool, surface: Surface) {
    let up = upstream(0).await;
    let rg = format!("rg-runs-{surface:?}").to_lowercase();
    let key = world(&pool, &up, Caller::ApiKey, &rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, &rg, json!({"max_new_runs_per_window": 2})).await;
    let principal = key_principal(&pool, "rcpar-key").await;

    // Two runs, and the first of them again: two runs started.
    for (run, started) in [("a", 1), ("a", 1), ("b", 2), ("a", 2), ("b", 2)] {
        let (status, _, body) = send(&state, surface, &key, &asserted(run)).await;
        assert!(
            !refused_as_new(status, &body),
            "{surface:?}: run {run} is within the limit: {status} {body}"
        );
        assert_eq!(
            runs_started(&principal, 3600).await,
            started,
            "{surface:?}: after run {run} ({status}: {body})"
        );
    }
    assert!(refusal_rows(&pool, 0).await.is_empty());

    // The third run is one too many.
    let sent_before = up.received_requests().await.map_or(0, |r| r.len());
    let (status, headers, body) = send(&state, surface, &key, &asserted("c")).await;
    assert!(
        refused_as_new(status, &body),
        "{surface:?}: the third new run of a credential limited to two: {status} {body}"
    );
    assert_eq!(runs_started(&principal, 3600).await, 2);
    assert_eq!(
        up.received_requests().await.map_or(0, |r| r.len()),
        sent_before,
        "{surface:?}: a refused run reached the upstream"
    );
    let root = rc::run_root_of(&state, &key, "c");
    let rows = refusal_rows(&pool, 1).await;
    assert_eq!(rows.len(), 1, "{surface:?}: {rows:?}");
    assert_eq!(
        rows[0].0.as_deref(),
        Some(root.as_str()),
        "{surface:?}: the refusal lands on the run it refused"
    );
    assert!(
        rows[0].1.as_deref().unwrap_or("").contains(TERM),
        "{surface:?}: {rows:?}"
    );

    // The runs already started go on.
    for run in ["a", "b"] {
        let (status, _, body) = send(&state, surface, &key, &asserted(run)).await;
        assert!(
            !refused_as_new(status, &body),
            "{surface:?}: {status} {body}"
        );
    }

    // The same answer as the surface's own rate limit gives.
    common::set_rg_operating_envelope(&pool, &rg, json!({})).await;
    if !arm_the_sibling_rate_limit(&pool, &rg, surface).await {
        return;
    }
    let state = replica(&pool).await;
    let mut sibling = None;
    for _ in 0..3 {
        let (s, h, b) = send(&state, surface, &key, &asserted("a")).await;
        if s == StatusCode::TOO_MANY_REQUESTS {
            sibling = Some((s, h, b));
            break;
        }
    }
    let (sibling_status, sibling_headers, sibling_body) =
        sibling.unwrap_or_else(|| panic!("{surface:?}: the premise — its rate limit refuses"));
    assert!(!sibling_body.to_string().contains(TERM), "{sibling_body}");
    assert_eq!(status, sibling_status);
    assert_eq!(
        shape(&body),
        shape(&sibling_body),
        "{surface:?}: another body shape than the surface's rate limit.\n new run: {body}\n \
         sibling: {sibling_body}"
    );
    assert_eq!(
        headers.contains_key("retry-after"),
        sibling_headers.contains_key("retry-after"),
        "{surface:?}: Retry-After, as the surface's rate limit sends it"
    );
}

macro_rules! one_too_many {
    ($($name:ident => $s:ident),* $(,)?) => {
        $(
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn $name(pool: PgPool) {
                refuses_the_run_that_is_one_too_many(pool, Surface::$s).await;
            }
        )*
    };
}

one_too_many! {
    llm_chat_refuses_the_run_that_is_one_too_many => LlmChat,
    llm_messages_refuses_the_run_that_is_one_too_many => LlmMessages,
    embeddings_refuse_the_run_that_is_one_too_many => Embeddings,
    mcp_refuses_the_run_that_is_one_too_many => Mcp,
    skill_server_refuses_the_run_that_is_one_too_many => SkillServerList,
    skill_server_script_refuses_the_run_that_is_one_too_many => SkillServerRunScript,
    search_server_refuses_the_run_that_is_one_too_many => SearchServerWeb,
    skill_refuses_the_run_that_is_one_too_many => Skill,
    a2a_outbound_refuses_the_run_that_is_one_too_many => A2aOutbound,
    a2a_inbound_refuses_the_run_that_is_one_too_many => A2aInbound,
    a2a_v1_refuses_the_run_that_is_one_too_many => A2aV1,
}

// ---------------------------------------------------------------------------
// What starts a run
// ---------------------------------------------------------------------------

/// A caller that sends no run id gets a root minted per request: each is a
/// run it started. The caller that never repeats a run id, or never sends
/// one, is the one the limit is for.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_root_the_gateway_mints_is_a_run_the_credential_started(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-minted";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 4})).await;
    let principal = key_principal(&pool, "rcpar-key").await;

    // Four requests with no run id, across surfaces: four runs.
    for (n, surface) in [
        Surface::Mcp,
        Surface::LlmChat,
        Surface::A2aInbound,
        Surface::Skill,
    ]
    .into_iter()
    .enumerate()
    {
        let (status, _, body) = send(&state, surface, &key, &Run::None).await;
        assert!(
            !refused_as_new(status, &body),
            "{surface:?}: {status} {body}"
        );
        assert_eq!(
            runs_started(&principal, 3600).await,
            n as i64 + 1,
            "{surface:?}"
        );
    }
    // The fifth, on any surface, is refused.
    for surface in [
        Surface::Mcp,
        Surface::LlmChat,
        Surface::A2aOutbound,
        Surface::A2aV1,
        Surface::SkillServerList,
    ] {
        let (status, _, body) = send(&state, surface, &key, &Run::None).await;
        assert!(
            refused_as_new(status, &body),
            "{surface:?}: {status} {body}"
        );
    }
    assert_eq!(runs_started(&principal, 3600).await, 4);
    assert_eq!(refusal_rows(&pool, 5).await.len(), 5);
}

/// A step that arrives on a delegation chain the gateway signed continues
/// the chain's run: it starts nothing and is not counted, however many
/// different runs such steps belong to. A chain that does not verify is not
/// a chain: the gateway mints a root for the request, which counts.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_step_on_a_verified_chain_starts_no_run(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-chain";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 1})).await;
    let principal = key_principal(&pool, "rcpar-key").await;
    let tenant_key = derive_tenant_key(state.config.encryption_key.as_bytes(), T);

    // The credential's one run.
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("mine")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    assert_eq!(runs_started(&principal, 3600).await, 1);

    // Steps of six other runs, each on a verified chain: none is counted.
    for surface in [Surface::Mcp, Surface::LlmChat, Surface::A2aOutbound] {
        for _ in 0..2 {
            let run = Run::Chain {
                root: ulid::Ulid::new(),
                key: tenant_key.clone(),
            };
            let (status, _, body) = send(&state, surface, &key, &run).await;
            assert!(
                !refused_as_new(status, &body),
                "{surface:?}: a step of a run the gateway signed for: {status} {body}"
            );
        }
    }
    assert_eq!(runs_started(&principal, 3600).await, 1);
    assert!(refusal_rows(&pool, 0).await.is_empty());

    // A chain signed with another key does not verify: a minted root, the
    // credential's second run.
    for surface in [Surface::Mcp, Surface::LlmChat, Surface::A2aOutbound] {
        let run = Run::Chain {
            root: ulid::Ulid::new(),
            key: b"not the tenant's key".to_vec(),
        };
        let (status, _, body) = send(&state, surface, &key, &run).await;
        assert!(
            refused_as_new(status, &body),
            "{surface:?}: an unverified chain is a new run: {status} {body}"
        );
    }
    assert_eq!(runs_started(&principal, 3600).await, 1);
}

/// The count belongs to the credential. Another API key has its own; the
/// keys of one agent identity share the agent's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_count_is_the_credentials_own(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-principal";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let other = common::seed_api_key(&pool, T, rg, Some(9901), "rcpar-key-2").await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 1})).await;
    let (p1, p2) = (
        key_principal(&pool, "rcpar-key").await,
        key_principal(&pool, "rcpar-key-2").await,
    );

    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("a")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("b")).await;
    assert!(refused_as_new(status, &body), "{status} {body}");
    // The other key is not held to the first key's count, and a run id is
    // each credential's own: the other key's "a" is another run.
    let (status, _, body) = send(&state, Surface::Mcp, &other, &asserted("a")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    let (status, _, body) = send(&state, Surface::Mcp, &other, &asserted("b")).await;
    assert!(refused_as_new(status, &body), "{status} {body}");
    assert_eq!(
        (runs_started(&p1, 3600).await, runs_started(&p2, 3600).await),
        (1, 1)
    );

    // Both keys become one agent's: their runs are the agent's.
    let agent = format!("agent-runs-{}", ulid::Ulid::new());
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ($1, $2, $1, 'manual', true, 'active', false)",
    )
    .bind(&agent)
    .bind(T)
    .execute(&pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = $1 WHERE tenant_id = $2")
        .bind(&agent)
        .bind(T)
        .execute(&pool)
        .await
        .expect("bind the keys to the agent");
    let state = replica(&pool).await;
    let principal = format!("agent:{agent}");
    let (status, _, body) = send(&state, Surface::LlmChat, &key, &asserted("x")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    assert_eq!(runs_started(&principal, 3600).await, 1);
    // The agent's second run, through its other key and on another surface.
    let (status, _, body) = send(&state, Surface::A2aInbound, &other, &asserted("y")).await;
    assert!(
        refused_as_new(status, &body),
        "the agent's other key started a second run: {status} {body}"
    );
    assert_eq!(runs_started(&principal, 3600).await, 1);
    assert_eq!(
        (runs_started(&p1, 3600).await, runs_started(&p2, 3600).await),
        (1, 1),
        "an agent's runs are not its keys'"
    );
}

// ---------------------------------------------------------------------------
// A burst on two gateway states
// ---------------------------------------------------------------------------

/// Forty requests, each of a run of its own, arrive together on two gateway
/// states that share the counters. Whether a run is new is decided by the
/// admission that counts it, so exactly the limit are let through.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_burst_of_new_runs_on_two_gateway_states_admits_exactly_the_limit(pool: PgPool) {
    let up = upstream(300).await;
    let rg = "rg-runs-burst";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let (a, b) = (replica(&pool).await, replica(&pool).await);
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 6})).await;
    let principal = key_principal(&pool, "rcpar-key").await;

    let surfaces = [Surface::Mcp, Surface::LlmChat, Surface::A2aInbound];
    let tasks: Vec<_> = (0..40)
        .map(|i| {
            let state = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            let surface = surfaces[i % surfaces.len()];
            tokio::spawn(async move {
                let run = if i % 4 == 0 {
                    Run::None
                } else {
                    asserted(&format!("burst-{i}"))
                };
                let (status, _, body) = send(&state, surface, &key, &run).await;
                refused_as_new(status, &body)
            })
        })
        .collect();
    let mut refused = 0;
    for t in tasks {
        if t.await.unwrap() {
            refused += 1;
        }
    }
    assert_eq!(refused, 34, "40 new runs, a limit of 6");
    assert_eq!(runs_started(&principal, 3600).await, 6);
    assert_eq!(refusal_rows(&pool, 34).await.len(), 34);
}

/// Thirty requests of ONE new run arrive together on two states, with a
/// limit of one: the run is counted once and every request of it passes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn one_new_run_arriving_many_times_at_once_is_one_run(pool: PgPool) {
    let up = upstream(200).await;
    let rg = "rg-runs-one";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let (a, b) = (replica(&pool).await, replica(&pool).await);
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 1})).await;
    let principal = key_principal(&pool, "rcpar-key").await;

    let tasks: Vec<_> = (0..30)
        .map(|i| {
            let state = if i % 2 == 0 { a.clone() } else { b.clone() };
            let key = key.clone();
            tokio::spawn(async move {
                let (status, _, body) =
                    send(&state, Surface::Mcp, &key, &asserted("the-run")).await;
                (status, body)
            })
        })
        .collect();
    for t in tasks {
        // (The upstream's own concurrency limit may refuse some of thirty
        // calls at once; none is refused as a new run.)
        let (status, body) = t.await.unwrap();
        assert!(!refused_as_new(status, &body), "{status} {body}");
    }
    assert_eq!(runs_started(&principal, 3600).await, 1);
    assert!(refusal_rows(&pool, 0).await.is_empty());
    assert!(!up.received_requests().await.unwrap().is_empty());
}

// ---------------------------------------------------------------------------
// The window, no limit, a set that cannot be operated
// ---------------------------------------------------------------------------

/// The window is the group's (`new_runs_window_seconds`), epoch-aligned: a
/// credential at its limit starts runs again when it rolls over.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_window_rolls_over(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-window";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(
        &pool,
        rg,
        json!({"max_new_runs_per_window": 1, "new_runs_window_seconds": 3}),
    )
    .await;
    let now = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64
    };
    // Start early in a window, so that both requests fall in it.
    while now() % 3_000 > 1_000 {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let window = now() / 3_000;

    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("a")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    let (status, headers, body) = send(&state, Surface::Mcp, &key, &asserted("b")).await;
    assert_eq!(now() / 3_000, window, "the test ran slower than its window");
    assert!(refused_as_new(status, &body), "{status} {body}");
    let retry: u64 = headers
        .get("retry-after")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse().ok())
        .expect("Retry-After");
    assert!(
        (1..=3).contains(&retry),
        "retry after {retry}s of a 3s window"
    );

    while now() / 3_000 == window {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("b")).await;
    assert!(
        !refused_as_new(status, &body),
        "a new window, a new allowance: {status} {body}"
    );
}

/// Without the term nothing is counted: no set is written, so the default
/// costs no round trip. A malformed maximum is no limit, like every other
/// envelope term; a malformed window is the default one.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_without_the_limit_counts_nothing(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-none";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    let principal = key_principal(&pool, "rcpar-key").await;

    for envelope in [
        None,
        Some(json!({"max_actions_per_run": 50, "max_llm_calls_per_run": 50})),
        Some(json!({"max_new_runs_per_window": 0})),
        Some(json!({"max_new_runs_per_window": -3})),
        Some(json!({"max_new_runs_per_window": "ten"})),
        Some(json!({"max_new_runs_per_window": null, "new_runs_window_seconds": 60})),
    ] {
        if let Some(e) = &envelope {
            common::set_rg_operating_envelope(&pool, rg, e.clone()).await;
        }
        for surface in [Surface::Mcp, Surface::LlmChat, Surface::A2aInbound] {
            for _ in 0..3 {
                let (status, _, body) = send(&state, surface, &key, &Run::None).await;
                assert!(
                    status.is_success(),
                    "{envelope:?} × {surface:?}: no limit refuses nothing: {status} {body}"
                );
            }
        }
        assert!(
            run_sets(&principal).await.is_empty(),
            "{envelope:?}: runs were counted without a limit"
        );
    }
    assert!(refusal_rows(&pool, 0).await.is_empty());

    // A malformed window is the default one (an hour): the maximum the
    // operator declared still applies.
    common::set_rg_operating_envelope(
        &pool,
        rg,
        json!({"max_new_runs_per_window": 1, "new_runs_window_seconds": "soon"}),
    )
    .await;
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("a")).await;
    assert!(!refused_as_new(status, &body), "{status} {body}");
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("b")).await;
    assert!(refused_as_new(status, &body), "{status} {body}");
    assert_eq!(runs_started(&principal, 3600).await, 1);
}

/// A set the admission cannot operate on refuses the step (fail closed), on
/// an action surface and on a model call.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_set_that_cannot_be_operated_refuses_the_step(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-broken";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(&pool, rg, json!({"max_new_runs_per_window": 100})).await;
    let principal = key_principal(&pool, "rcpar-key").await;
    let mut c = redis_conn().await;
    let _: () = redis::cmd("SET")
        .arg(limits::new_runs_key(T, &principal, 3600))
        .arg("not a set")
        .arg("EX")
        .arg(120)
        .query_async(&mut c)
        .await
        .expect("SET");

    for surface in [
        Surface::Mcp,
        Surface::LlmChat,
        Surface::A2aInbound,
        Surface::Skill,
    ] {
        let (status, _, body) = send(&state, surface, &key, &asserted("a")).await;
        assert!(
            status.is_client_error(),
            "{surface:?}: a step whose limit cannot be checked went through: {status} {body}"
        );
    }
    assert_eq!(up.received_requests().await.unwrap().len(), 0);
}

// ---------------------------------------------------------------------------
// With the action ceiling
// ---------------------------------------------------------------------------

/// The new run and the run's action are admitted in one step: a step
/// refused as one run too many takes no action, and a step refused by its
/// run's action ceiling starts no run.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_new_run_and_its_action_are_admitted_together_or_not_at_all(pool: PgPool) {
    let up = upstream(0).await;
    let rg = "rg-runs-and-actions";
    let key = world(&pool, &up, Caller::ApiKey, rg).await;
    let state = replica(&pool).await;
    common::set_rg_operating_envelope(
        &pool,
        rg,
        json!({"max_new_runs_per_window": 2, "max_actions_per_run": 1}),
    )
    .await;
    let principal = key_principal(&pool, "rcpar-key").await;
    let actions = |run: &str| {
        let root = rc::run_root_of(&state, &key, run);
        let state = state.clone();
        async move {
            state
                .usage_state
                .counter_value(&actions_key(&root))
                .await
                .unwrap()
        }
    };

    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("a")).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(
        (runs_started(&principal, 3600).await, actions("a").await),
        (1, 1)
    );

    // Run a is at its action ceiling: its next step is refused by that, and
    // is still one run.
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("a")).await;
    assert!(
        status == StatusCode::TOO_MANY_REQUESTS && body.to_string().contains("max_actions_per_run"),
        "{status} {body}"
    );
    assert_eq!(runs_started(&principal, 3600).await, 1);

    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("b")).await;
    assert!(status.is_success(), "{status} {body}");
    assert_eq!(runs_started(&principal, 3600).await, 2);

    // Run c is one too many: refused as that, and it took no action.
    let (status, _, body) = send(&state, Surface::Mcp, &key, &asserted("c")).await;
    assert!(refused_as_new(status, &body), "{status} {body}");
    assert_eq!(actions("c").await, 0, "a refused run took an action");
    let mut c = redis_conn().await;
    let exists: i64 = redis::cmd("EXISTS")
        .arg(actions_key(&rc::run_root_of(&state, &key, "c")))
        .query_async(&mut c)
        .await
        .unwrap();
    assert_eq!(exists, 0);
    assert_eq!(runs_started(&principal, 3600).await, 2);
}

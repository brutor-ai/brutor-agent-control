// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The identity judge reads the policy store: the acting group's ancestor
//! chain (each group's explicit `identity_policy`, the lifecycle stage and
//! risk tier of each AI System) from the group cache, and the tenant's
//! `identity_policy_default` from the tenant cache. A change of the tenant
//! default reaches the core through the invalidation the control plane
//! publishes for it, not after the cache's lifetime.

use axum::http::HeaderMap;
use brutor_gateway_core::models::auth::{AuthContext, CredentialAssurance};
use brutor_gateway_core::services::SharedState;
use brutor_gateway_core::services::auth::{AuthError, authenticate_request};
use brutor_gateway_core::services::cache::{CacheInvalidator, InvalidationMessage};
use brutor_gateway_core::services::identity_policy::{Outcome, Reason, judge_call};
use sqlx::PgPool;

use crate::common;

const T: &str = "tenant-id-policy-store";

fn bearer(token: &str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert("authorization", format!("Bearer {token}").parse().unwrap());
    headers
}

async fn authenticate(state: &SharedState, raw_key: &str) -> AuthContext {
    authenticate_request(
        &bearer(raw_key),
        T,
        &state.config.jwt_secret,
        &state.db,
        &state.credential_verifiers,
        None,
        true,
    )
    .await
    .expect("the key authenticates")
}

async fn make_ai_system(pool: &PgPool, id: &str, stage: &str) {
    sqlx::query(
        "UPDATE resource_groups SET group_type = 'AI_SYSTEM', lifecycle_stage = $2, \
         display_name = 'Loan Pre-screening' WHERE id = $1",
    )
    .bind(id)
    .bind(stage)
    .execute(pool)
    .await
    .expect("make_ai_system");
}

async fn set_group_policy(pool: &PgPool, id: &str, policy: Option<serde_json::Value>) {
    sqlx::query("UPDATE resource_groups SET identity_policy = $2 WHERE id = $1")
        .bind(id)
        .bind(policy)
        .execute(pool)
        .await
        .expect("set_group_policy");
}

async fn set_tenant_default(pool: &PgPool, default: Option<serde_json::Value>) {
    sqlx::query("UPDATE tenants SET identity_policy_default = $2 WHERE id = $1")
        .bind(T)
        .bind(default)
        .execute(pool)
        .await
        .expect("set_tenant_default");
}

fn policy(mode: &str, min: &str) -> serde_json::Value {
    serde_json::json!({
        "mode": mode,
        "min_assurance": min,
        "allow_user_keys": true,
        "allowed_issuers": null,
        "max_token_lifetime_s": 3600,
        "max_key_ttl_days": null,
    })
}

/// A fresh key of `group`, authenticated and judged.
async fn judged(
    state: &SharedState,
    group: &str,
) -> Result<brutor_gateway_core::services::identity_policy::IdentityVerdict, AuthError> {
    let raw = common::seed_shared_group_api_key(&state.db, T, group, "k").await;
    let mut auth = authenticate(state, &raw).await;
    judge_call(state, &mut auth).await
}

fn refusal(
    result: Result<impl std::fmt::Debug, AuthError>,
) -> Box<brutor_gateway_core::services::identity_policy::CredentialRequirement> {
    match result {
        Err(AuthError::CredentialAssuranceInsufficient(requirement)) => requirement,
        other => panic!("expected a credential refusal, got {other:?}"),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_active_ai_system_refuses_a_key_and_its_sub_group_inherits_the_floor(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-sys", T).await;
    common::seed_resource_group(&pool, "rg-child", T).await;
    common::set_rg_parent(&pool, "rg-child", "rg-sys").await;
    make_ai_system(&pool, "rg-sys", "active").await;
    let state = common::build_community_test_state(pool.clone());

    let refused = refusal(judged(&state, "rg-sys").await);
    assert_eq!(refused.required, CredentialAssurance::Workload);
    assert_eq!(refused.presented, Some(CredentialAssurance::Key));
    assert_eq!(refused.reason, Reason::BelowMinimum);
    assert_eq!(refused.ai_system_id.as_deref(), Some("rg-sys"));
    assert_eq!(
        refused.ai_system_name.as_deref(),
        Some("Loan Pre-screening")
    );
    assert_eq!(refused.group_id.as_deref(), Some("rg-sys"));

    // The production floor of the system governs its sub-groups too.
    let refused = refusal(judged(&state, "rg-child").await);
    assert_eq!(refused.group_id.as_deref(), Some("rg-child"));
    assert_eq!(refused.ai_system_id.as_deref(), Some("rg-sys"));
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_development_system_serves_a_key_and_an_explicit_policy_is_read(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-dev", T).await;
    make_ai_system(&pool, "rg-dev", "approved").await;
    let state = common::build_community_test_state(pool.clone());
    let verdict = judged(&state, "rg-dev").await.expect("development default");
    assert_eq!(verdict.outcome, Outcome::Met);

    // An explicit audit policy above `key`: served, recorded below policy.
    common::seed_resource_group(&pool, "rg-audit", T).await;
    set_group_policy(&pool, "rg-audit", Some(policy("audit", "workload"))).await;
    let verdict = judged(&state, "rg-audit").await.expect("audit serves");
    assert_eq!(verdict.outcome, Outcome::BelowPolicy);
    assert_eq!(verdict.required, CredentialAssurance::Workload);

    // An explicit enforce policy on a plain group refuses.
    common::seed_resource_group(&pool, "rg-enforce", T).await;
    set_group_policy(&pool, "rg-enforce", Some(policy("enforce", "workload"))).await;
    let refused = refusal(judged(&state, "rg-enforce").await);
    assert_eq!(refused.ai_system_id, None, "not an AI System");
    assert_eq!(refused.group_id.as_deref(), Some("rg-enforce"));

    // A stored value that is no policy is read as the strictest one.
    common::seed_resource_group(&pool, "rg-broken", T).await;
    set_group_policy(
        &pool,
        "rg-broken",
        Some(serde_json::json!({"mode": "monitor"})),
    )
    .await;
    let refused = refusal(judged(&state, "rg-broken").await);
    assert_eq!(refused.required, CredentialAssurance::WorkloadPop);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_default_change_reaches_the_core_on_its_invalidation(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-plain", T).await;
    let state = common::build_community_test_state(pool.clone());

    // No tenant default: the development default serves a key.
    assert_eq!(
        judged(&state, "rg-plain").await.expect("met").outcome,
        Outcome::Met
    );

    // The tenant default is set to enforce `workload`. The control plane
    // publishes a `resource_group` message for the tenant; answered, it
    // drops the tenant's cached status, so the next call reads the new
    // default at once (not after the cache lifetime).
    let mut default = policy("enforce", "workload");
    default["max_user_key_ttl_days"] = serde_json::json!(30);
    set_tenant_default(&pool, Some(default)).await;
    invalidate_tenant_default(&state).await;
    let refused = refusal(judged(&state, "rg-plain").await);
    assert_eq!(refused.required, CredentialAssurance::Workload);

    // Cleared again: served.
    set_tenant_default(&pool, None).await;
    invalidate_tenant_default(&state).await;
    assert_eq!(
        judged(&state, "rg-plain").await.expect("met").outcome,
        Outcome::Met
    );
}

/// What the listener does with the message the control plane publishes on
/// a change of the tenant default (`type = resource_group`, `id` = the
/// tenant).
async fn invalidate_tenant_default(state: &SharedState) {
    let inv = CacheInvalidator {
        model_cache: state.model_cache.clone(),
        group_cache: state.group_cache.clone(),
        mcp_cache: state.mcp_cache.clone(),
        skill_cache: state.skill_cache.clone(),
        provider_cache: state.provider_cache.clone(),
        guardrails_cache: state.guardrails_cache.clone(),
        argument_policy_cache: state.argument_policy_cache.clone(),
        agent_card_cache: state.agent_card_cache.clone(),
        state: Some(state.clone()),
        redis: None,
        handlers: state.invalidation.clone(),
    };
    let msg = InvalidationMessage {
        resource_type: "resource_group".into(),
        action: "update".into(),
        tenant_id: T.into(),
        id: T.into(),
    };
    let mut answered = false;
    for handler in state.invalidation.handlers_for(&msg.resource_type) {
        (handler.handle)(&inv, &msg).await;
        answered = true;
    }
    assert!(answered, "a resource_group message has a handler");
}

/// A refused call writes one `proxy_logs` row, however often its context
/// is judged: 403, `access_denied`, `credential_policy`, the sentence, the
/// acting group, and the credential with the `refused` verdict, sealed.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_refused_call_writes_one_sealed_row(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-sys", T).await;
    make_ai_system(&pool, "rg-sys", "active").await;
    let state = common::build_community_test_state(pool.clone());

    let raw = common::seed_shared_group_api_key(&pool, T, "rg-sys", "k").await;
    let mut auth = authenticate(&state, &raw).await;
    let first = refusal(judge_call(&state, &mut auth).await);
    let second = refusal(judge_call(&state, &mut auth).await);
    assert_eq!(first, second, "a context keeps its first verdict");

    type Row = (
        i32,
        bool,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    );
    let mut rows: Vec<Row> = Vec::new();
    for _ in 0..80 {
        rows = sqlx::query_as(
            "SELECT http_status_code, success, proxy_subtype, error_type, error_message, \
                    resource_group_id, credential_kind, credential_assurance, credential_ref, \
                    credential_policy_outcome, chain_version \
               FROM proxy_logs WHERE tenant_id = $1",
        )
        .bind(T)
        .fetch_all(&pool)
        .await
        .expect("rows");
        if !rows.is_empty() && rows[0].10.is_some() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    // Give a second row, if one were written, the time to land.
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(&pool)
        .await
        .expect("count");
    assert_eq!(count, 1, "one refusal, one row");
    let row = &rows[0];
    assert_eq!(row.0, 403);
    assert!(!row.1);
    assert_eq!(row.2.as_deref(), Some("access_denied"));
    assert_eq!(row.3.as_deref(), Some("credential_policy"));
    assert_eq!(row.4.as_deref(), Some(first.sentence().as_str()));
    assert_eq!(row.5.as_deref(), Some("rg-sys"));
    assert_eq!(row.6.as_deref(), Some("api_key"));
    assert_eq!(row.7.as_deref(), Some("key"));
    assert_eq!(row.8, auth.credential_ref);
    assert_eq!(row.9.as_deref(), Some("refused"));
    assert_eq!(row.10.as_deref(), Some("v5"), "sealed under v5");
}

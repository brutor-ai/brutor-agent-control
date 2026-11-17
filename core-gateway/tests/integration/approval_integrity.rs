// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What a human approval actually authorises.
//!
//! A one-time approval token is the only thing standing between an agent and
//! an action a person said yes to. Three properties have to hold, and none of
//! them did:
//!
//!   * The token authorises *these arguments*, not merely this tool.
//!     Validation compared tenant, scope and capability name and never looked
//!     at `call_arguments`, so a token granted for one call could be replayed
//!     with different arguments.
//!   * The token is spent exactly once. Validation was a SELECT that
//!     checked the status, then an unconditional UPDATE, with nothing between
//!     them, so two concurrent retries both passed.
//!   * An approved token expires. The sweeper only expired rows
//!     still `pending`, so an approved-but-unused token stayed valid forever.
//!
//! These drive `validate_approval_token` directly against a real row, because
//! the properties are about the row transition, not the HTTP surface.

use chrono::Duration;
use serde_json::json;
use sqlx::PgPool;

use brutor_gateway_core::services::agent_approval::validate_approval_token;

use crate::common;

const T: &str = "t-approval";

/// Insert an approved request holding `args`, and return its token.
async fn seed_approved(
    pool: &PgPool,
    id: &str,
    args: Option<serde_json::Value>,
    resolved_ago_secs: i64,
) -> String {
    let token = format!("apt_{id}");
    sqlx::query(
        "INSERT INTO tool_approval_requests \
         (id, tenant_id, server_name, capability_type, capability_name, requester_type, \
          requester_group_id, call_arguments, request_metadata, status, approval_token, \
          token_used, timeout_seconds, expires_at, resolved_at, created_at) \
         VALUES ($1, $2, 'crm', 'tool', 'issue_refund', 'agent', 'g1', $3::json, \
                 '{\"surface\": \"mcp\", \"agent_id\": \"agent-appr\"}'::json, 'approved', \
                 $4, 'N', 300, now() + interval '1 hour', $5, now())",
    )
    .bind(id)
    .bind(T)
    .bind(args.map(|a| a.to_string()))
    .bind(&token)
    .bind(crate::common::db_now() - Duration::seconds(resolved_ago_secs))
    .execute(pool)
    .await
    .expect("seed approval");
    token
}

/// The agent that raised the seeded holds.
fn caller() -> brutor_gateway_core::models::auth::AuthContext {
    let mut c = crate::background_writer_chain::test_auth(T);
    c.actor_agent_id = Some("agent-appr".into());
    c
}

async fn status_of(pool: &PgPool, id: &str) -> String {
    sqlx::query_as::<_, (String,)>("SELECT status FROM tool_approval_requests WHERE id = $1")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("status")
        .0
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_token_approved_for_one_argument_set_is_refused_for_another(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let approved = json!({"order_id": "4711", "amount": 20});
    let token = seed_approved(&pool, "appr-args", Some(approved), 0).await;

    // The human approved a $20 refund on order 4711; the retry asks for $9000.
    let replay = json!({"order_id": "4711", "amount": 9000});
    let err = validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&replay),
    )
    .await
    .expect_err("a different call must not be authorised by this token");
    assert!(err.contains("argument mismatch"), "got: {err}");

    // Refused, and NOT consumed — a legitimate retry with the right arguments
    // must still work after someone else's mistake or probe.
    assert_eq!(status_of(&pool, "appr-args").await, "approved");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_approved_arguments_authorise_the_matching_call(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let args = json!({"order_id": "4711", "amount": 20});
    let token = seed_approved(&pool, "appr-ok", Some(args.clone()), 0).await;

    validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&args),
    )
    .await
    .expect("the approved call must be authorised");
    assert_eq!(status_of(&pool, "appr-ok").await, "consumed");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn key_order_does_not_change_whether_a_call_is_authorised(pool: PgPool) {
    // Clients serialise JSON in whatever order they like; that must not be
    // the difference between an authorised and a refused call.
    common::seed_tenant(&pool, T).await;
    let stored = json!({"a": 1, "b": {"x": true, "y": [1, 2]}});
    let token = seed_approved(&pool, "appr-order", Some(stored), 0).await;
    let reordered = json!({"b": {"y": [1, 2], "x": true}, "a": 1});

    validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&reordered),
    )
    .await
    .expect("the same arguments in a different key order are the same call");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_one_time_token_cannot_be_spent_twice(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let args = json!({"order_id": "4711"});
    let token = seed_approved(&pool, "appr-once", Some(args.clone()), 0).await;

    validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&args),
    )
    .await
    .expect("first use is the authorised one");
    let err = validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&args),
    )
    .await
    .expect_err("the second use must be refused");
    // The row is consumed, so the token no longer resolves at all.
    assert!(
        err.contains("not found") || err.contains("already consumed"),
        "got: {err}",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_approval_nobody_spent_stops_being_valid(pool: PgPool) {
    // A person answering "yes, refund this" is answering a question asked
    // now. Two hours later that answer is stale, and the sweeper never
    // expired approved rows.
    common::seed_tenant(&pool, T).await;
    let args = json!({"order_id": "4711"});
    let token = seed_approved(&pool, "appr-stale", Some(args.clone()), 7200).await;

    let err = validate_approval_token(
        &pool,
        &token,
        T,
        &caller(),
        "crm",
        "tool",
        "issue_refund",
        Some(&args),
    )
    .await
    .expect_err("an hours-old approval must not still authorise the call");
    assert!(err.contains("expired"), "got: {err}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn scope_and_capability_are_still_checked(pool: PgPool) {
    // The pre-existing guarantees must survive the argument binding.
    common::seed_tenant(&pool, T).await;
    let args = json!({"order_id": "4711"});
    let token = seed_approved(&pool, "appr-scope", Some(args.clone()), 0).await;

    assert!(
        validate_approval_token(
            &pool,
            &token,
            T,
            &caller(),
            "other-server",
            "tool",
            "issue_refund",
            Some(&args)
        )
        .await
        .is_err(),
        "a token must not cross to another server",
    );
    assert!(
        validate_approval_token(
            &pool,
            &token,
            T,
            &caller(),
            "crm",
            "tool",
            "delete_account",
            Some(&args)
        )
        .await
        .is_err(),
        "a token must not cross to another capability",
    );
}

// --------------------------------------------------------------------

/// An approval answers the requester's question; another principal in the
/// same tenant holding the token cannot spend it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn only_the_requester_can_redeem_an_approval(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    let args = json!({"order_id": "4711"});
    let token = seed_approved(&pool, "appr-who", Some(args.clone()), 0).await;
    let redeem = |who| {
        let (pool, token, args) = (pool.clone(), token.clone(), args.clone());
        async move {
            validate_approval_token(
                &pool,
                &token,
                T,
                &who,
                "crm",
                "tool",
                "issue_refund",
                Some(&args),
            )
            .await
        }
    };

    let mut other_agent = caller();
    other_agent.actor_agent_id = Some("agent-other".into());
    let mut plain_key = crate::background_writer_chain::test_auth(T);
    plain_key.actor_agent_id = None;
    plain_key.api_key_id = Some("key-any".into());
    let mut portal_user = crate::background_writer_chain::test_auth(T);
    portal_user.actor_agent_id = None;
    portal_user.api_key_id = None;
    portal_user.gateway_user_id = Some("7".into());

    for (who, ctx) in [
        ("another agent", other_agent),
        ("an API key", plain_key),
        ("a portal user", portal_user),
    ] {
        let err = redeem(ctx).await.expect_err(who);
        assert!(err.contains("different requester"), "{who}: {err}");
        // Not consumed: the requester can still use it.
        assert_eq!(status_of(&pool, "appr-who").await, "approved", "{who}");
    }
    redeem(caller()).await.expect("the requester redeems it");
    assert_eq!(status_of(&pool, "appr-who").await, "consumed");
}

/// The same binding for an API-key requester and a portal-user requester,
/// through the row a real hold writes.
#[sqlx::test(migrations = "./tests/migrations")]
async fn key_and_user_holds_bind_to_their_own_principal(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    for (id, kind, user, key) in [
        ("appr-key", "api_key", None, Some("key-a")),
        ("appr-user", "portal_user", Some("41"), None),
    ] {
        sqlx::query(
            "INSERT INTO tool_approval_requests \
             (id, tenant_id, server_name, capability_type, capability_name, requester_type, \
              requester_user_id, requester_api_key_id, requester_group_id, status, \
              approval_token, token_used, timeout_seconds, expires_at, resolved_at, created_at) \
             VALUES ($1, $2, 'crm', 'tool', 'issue_refund', $3, $4, $5, 'g1', 'approved', \
                     $6, 'N', 300, now() + interval '1 hour', now(), now())",
        )
        .bind(id)
        .bind(T)
        .bind(kind)
        .bind(user)
        .bind(key)
        .bind(format!("apt_{id}"))
        .execute(&pool)
        .await
        .unwrap();
    }
    let ctx = |user: Option<&str>, key: Option<&str>| {
        let mut c = crate::background_writer_chain::test_auth(T);
        c.actor_agent_id = None;
        c.gateway_user_id = user.map(String::from);
        c.api_key_id = key.map(String::from);
        c
    };
    let v = |token: &'static str, c| {
        let pool = pool.clone();
        async move {
            validate_approval_token(&pool, token, T, &c, "crm", "tool", "issue_refund", None).await
        }
    };
    assert!(
        v("apt_appr-key", ctx(None, Some("key-b"))).await.is_err(),
        "another key"
    );
    v("apt_appr-key", ctx(None, Some("key-a")))
        .await
        .expect("same key");
    assert!(
        v("apt_appr-user", ctx(Some("42"), None)).await.is_err(),
        "another user"
    );
    assert!(
        v("apt_appr-user", ctx(Some("41"), Some("key-x")))
            .await
            .is_err(),
        "the user's id through an API key is not the user's session"
    );
    v("apt_appr-user", ctx(Some("41"), None))
        .await
        .expect("same user");
}

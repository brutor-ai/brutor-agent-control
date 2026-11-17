// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A tenant-wide API key
//! gets its tenant's guardrails.
//!
//! A key with no user and no group carries the identity `api_key_<id>`. The
//! guardrail loader bound that to `$1::int` to look up memberships, so the
//! load failed on every request made with such a key. Failing open, the LLM,
//! A2A, Anthropic, Gemini and system-server surfaces then ran with no
//! guardrails at all, and the fail-closed MCP and skills surfaces refused
//! every call. A non-numeric identity has no memberships: it takes the
//! group / tenant-default path.

use sqlx::PgPool;

use crate::common;

const T: &str = "tenant-gr-identity";

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tenant_wide_api_key_gets_the_tenant_default_guardrails(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-default",
        T,
        &["forbidden"],
        &["chat_input"],
    )
    .await;
    sqlx::query("UPDATE guardrail_configs SET is_default = true WHERE id = 'gr-default'")
        .execute(&pool)
        .await
        .unwrap();
    let state = common::build_test_state(pool.clone());

    let configs = state
        .guardrails_cache
        .get_configs(T, Some("api_key_key-123"), None, &pool)
        .await
        .expect("a tenant-wide key's guardrails must load, not error");
    assert_eq!(
        configs.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(),
        vec!["gr-default"],
        "a tenant-wide key gets the tenant's default guardrails"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_numeric_user_still_resolves_through_memberships(pool: PgPool) {
    common::seed_tenant(&pool, T).await;
    common::seed_resource_group(&pool, "rg-gr", T).await;
    common::seed_end_user(&pool, 9811, T).await;
    common::add_direct_membership(&pool, 9811, "rg-gr", T).await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-group",
        T,
        &["forbidden"],
        &["chat_input"],
    )
    .await;
    sqlx::query(
        "INSERT INTO resource_group_guardrails (resource_group_id, guardrail_config_id) \
         VALUES ('rg-gr', 'gr-group')",
    )
    .execute(&pool)
    .await
    .unwrap();
    let state = common::build_test_state(pool.clone());

    let configs = state
        .guardrails_cache
        .get_configs(T, Some("9811"), None, &pool)
        .await
        .expect("load");
    assert_eq!(
        configs.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(),
        vec!["gr-group"]
    );
}

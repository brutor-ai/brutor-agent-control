// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// OAuth broker on-behalf-of attribution: when an agent exercises a user-held
// brokered token, the broker stamps the acting agent on the oauth_tokens row
// (last_actor_agent_id + last_used_by_agent_at), complementing the per-request
// proxy_logs.actor_agent_id.

use sqlx::PgPool;

use crate::common;

async fn seed_oauth_token(pool: &PgPool, tenant: &str, idp_user: &str, mcp_server: &str) {
    sqlx::query(
        "INSERT INTO oauth_tokens \
         (tenant_id, idp_user_id, mcp_server_id, idp_id, encrypted_access_token, token_type, \
          expires_at, refresh_count, created_at, updated_at) \
         VALUES ($1, $2, $3, 'idp-x', 'enc', 'bearer', now() + interval '1 hour', 0, now(), now())",
    )
    .bind(tenant)
    .bind(idp_user)
    .bind(mcp_server)
    .execute(pool)
    .await
    .expect("seed_oauth_token");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn record_agent_use_stamps_actor_on_token(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-oa").await;
    seed_oauth_token(&pool, "tenant-oa", "user-sub-1", "mcp-srv-1").await;

    let state = common::build_test_state(pool.clone());
    state
        .oauth_token_manager
        .record_agent_use(
            &pool,
            "tenant-oa",
            "user-sub-1",
            "mcp-srv-1",
            "idp-x",
            "agent-oa",
        )
        .await;

    let row: (Option<String>, Option<chrono::DateTime<chrono::Utc>>) = sqlx::query_as(
        "SELECT last_actor_agent_id, last_used_by_agent_at FROM oauth_tokens \
         WHERE tenant_id = 'tenant-oa' AND idp_user_id = 'user-sub-1' AND mcp_server_id = 'mcp-srv-1'",
    )
    .fetch_one(&pool)
    .await
    .expect("token row");
    assert_eq!(row.0.as_deref(), Some("agent-oa"));
    assert!(row.1.is_some(), "last_used_by_agent_at must be set");
}

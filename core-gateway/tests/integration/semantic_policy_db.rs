// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// Semantic Governance Policy — DB integration. Exercises the real policy
// loading (group-bound + tenant-default), judge-model resolution, and the
// evaluate() enforce/shadow/block decision logic against live Postgres, with a
// fake Judge (the production judge self-calls the chat endpoint, which a
// service-level test can't host). The route glue + recursion guard are covered
// by unit tests + compile.

use async_trait::async_trait;
use sqlx::PgPool;

use brutor_gateway_core::services::semantic_policy::{
    Judge, JudgeVerdict, PolicyContext, PolicyDecision, SemanticPolicyCache, evaluate,
};

use crate::common;

const TENANT: &str = "t-sgp";
/// The chat model `seed_chat_model` creates, also passed to `evaluate` as the
/// judge fallback (the `JUDGE_MODEL_NAME` env var's stand-in). Shared so the seed and
/// the fallback can't drift apart.
///
/// These policies pin no judge model and the tenant sets no default, so the
/// fallback is the only resolution tier left. Pass `None` and every policy
/// fails open — which would silently turn the `Block` assertions below into
/// false passes rather than failures.
const JUDGE_MODEL_NAME: &str = "judge-gpt";

/// Verdict driven by whether the content contains a marker word.
struct WordJudge {
    trigger: &'static str,
}
#[async_trait]
impl Judge for WordJudge {
    async fn evaluate(
        &self,
        _model: &str,
        _constraint: &str,
        content: &str,
        _ctx: &PolicyContext,
        _h: &[(String, String)],
    ) -> Result<JudgeVerdict, String> {
        Ok(JudgeVerdict {
            violated: content.contains(self.trigger),
            reason: format!("matched {}", self.trigger),
        })
    }
}

async fn seed_policy(
    pool: &PgPool,
    id: &str,
    mode: &str,
    action: &str,
    surfaces: Option<serde_json::Value>,
) {
    sqlx::query(
        "INSERT INTO semantic_policies \
         (id, tenant_id, name, constraint_text, action, mode, surfaces, enabled, created_at, updated_at) \
         VALUES ($1, $2, $3, 'no PII to external peers', $4, $5, $6, true, now(), now())",
    )
    .bind(id)
    .bind(TENANT)
    .bind(id)
    .bind(action)
    .bind(mode)
    .bind(surfaces)
    .execute(pool)
    .await
    .expect("seed_policy");
}

async fn bind_policy(pool: &PgPool, policy_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_semantic_policies (resource_group_id, semantic_policy_id) \
         VALUES ($1, $2)",
    )
    .bind(rg_id)
    .bind(policy_id)
    .execute(pool)
    .await
    .expect("bind_policy");
}

async fn seed_chat_model(pool: &PgPool) {
    common::seed_llm_provider(pool, "prov-sgp", TENANT, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-sgp",
        TENANT,
        "prov-sgp",
        JUDGE_MODEL_NAME,
        "http://127.0.0.1:1",
    )
    .await;
}

fn ctx(surface: &str) -> PolicyContext {
    PolicyContext {
        surface: surface.to_string(),
        actor_agent_id: Some("agent-x".to_string()),
        action_type: Some("a2a_call".to_string()),
        target: Some("card-peer".to_string()),
        subject: Some("user:42".to_string()),
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn enforce_block_policy_blocks_violation(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, "rg-sgp", TENANT).await;
    seed_chat_model(&pool).await;
    seed_policy(&pool, "sgp-block", "enforce", "block", None).await;
    bind_policy(&pool, "sgp-block", "rg-sgp").await;

    let cache = SemanticPolicyCache::new(60);
    let policies = cache
        .get_policies(TENANT, Some("rg-sgp"), &pool)
        .await
        .expect("load policies");
    assert_eq!(policies.len(), 1, "the group-bound policy must load");

    let judge = WordJudge { trigger: "ssn" };
    // Violating content → enforce+block → Block.
    let blocked = evaluate(
        &pool,
        TENANT,
        &policies,
        "here is my ssn 123",
        &ctx("a2a_outbound"),
        &judge,
        &[],
        Some(JUDGE_MODEL_NAME),
    )
    .await;
    assert_eq!(blocked.decision, PolicyDecision::Block);
    assert_eq!(blocked.matched_policy_id.as_deref(), Some("sgp-block"));

    // Clean content → Allow.
    let allowed = evaluate(
        &pool,
        TENANT,
        &policies,
        "totally clean",
        &ctx("a2a_outbound"),
        &judge,
        &[],
        Some(JUDGE_MODEL_NAME),
    )
    .await;
    assert_eq!(allowed.decision, PolicyDecision::Allow);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn shadow_policy_records_but_does_not_block(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, "rg-sgp", TENANT).await;
    seed_chat_model(&pool).await;
    seed_policy(&pool, "sgp-shadow", "shadow", "block", None).await;
    bind_policy(&pool, "sgp-shadow", "rg-sgp").await;

    let cache = SemanticPolicyCache::new(60);
    let policies = cache
        .get_policies(TENANT, Some("rg-sgp"), &pool)
        .await
        .unwrap();
    let judge = WordJudge { trigger: "ssn" };
    let out = evaluate(
        &pool,
        TENANT,
        &policies,
        "my ssn is 1",
        &ctx("a2a_outbound"),
        &judge,
        &[],
        Some(JUDGE_MODEL_NAME),
    )
    .await;
    assert_eq!(out.decision, PolicyDecision::Allow, "shadow never blocks");
    assert_eq!(out.shadow_hits.len(), 1, "but records a would-block");
    assert_eq!(out.shadow_hits[0].0, "sgp-shadow");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn surface_scoping_and_tenant_default(pool: PgPool) {
    common::seed_tenant(&pool, TENANT).await;
    common::seed_resource_group(&pool, "rg-sgp", TENANT).await;
    seed_chat_model(&pool).await;
    // A tenant-default policy (no RG binding), scoped to chat_input only.
    seed_policy(
        &pool,
        "sgp-default",
        "enforce",
        "block",
        Some(serde_json::json!(["chat_input"])),
    )
    .await;

    let cache = SemanticPolicyCache::new(60);
    // Tenant-default policies load even for a specific group.
    let policies = cache
        .get_policies(TENANT, Some("rg-sgp"), &pool)
        .await
        .unwrap();
    assert_eq!(policies.len(), 1);

    let judge = WordJudge { trigger: "ssn" };
    // Wrong surface (a2a_outbound) → policy does not apply → Allow.
    let other = evaluate(
        &pool,
        TENANT,
        &policies,
        "my ssn",
        &ctx("a2a_outbound"),
        &judge,
        &[],
        Some(JUDGE_MODEL_NAME),
    )
    .await;
    assert_eq!(other.decision, PolicyDecision::Allow);
    // Matching surface (chat_input) → blocks.
    let hit = evaluate(
        &pool,
        TENANT,
        &policies,
        "my ssn",
        &ctx("chat_input"),
        &judge,
        &[],
        Some(JUDGE_MODEL_NAME),
    )
    .await;
    assert_eq!(hit.decision, PolicyDecision::Block);
}

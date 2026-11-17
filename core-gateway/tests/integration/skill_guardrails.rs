// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Agent Skill guardrail surfaces (`skill_input`, `skill_output`) via
//! `POST /v1/proxy/skills/execute`.
//!
//! Pins that guardrails actually fire on the skill surfaces (the "is the
//! handler wired to invoke the guardrail on surface Y?" gap). The REST skill
//! path resolves the skill from the DB and returns its `skill_md_content` /
//! `description` as the result — no sandbox runs — so `skill_output` is
//! testable by putting the banned word in the seeded skill row.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::json;
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

async fn body_to_string(body: Body) -> String {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    String::from_utf8_lossy(&bytes).to_string()
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_input_guardrail_blocks_banned_args(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-sk").await;
    common::seed_resource_group(&pool, "rg-sk", "tenant-sk").await;
    common::seed_end_user(&pool, 7701, "tenant-sk").await;
    common::add_direct_membership(&pool, 7701, "rg-sk", "tenant-sk").await;

    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-sk-in",
        "tenant-sk",
        &["forbidden-skill-arg"],
        &["skill_input"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-sk-in", "rg-sk").await;

    let api_key = common::seed_api_key(&pool, "tenant-sk", "rg-sk", Some(7701), "k-sk").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    // The input guardrail runs BEFORE skill resolution, so the skill need not
    // exist — the banned word in input_params is enough to 403.
    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({
            "skill_id": "sk-does-not-matter",
            "input_params": {"q": "please forbidden-skill-arg now"}
        }),
        Some(&api_key),
        Some("tenant-sk"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "skill_input guardrail must block banned args; got {status}, body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_output_guardrail_blocks_banned_result(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-sk2").await;
    common::seed_resource_group(&pool, "rg-sk2", "tenant-sk2").await;
    common::seed_end_user(&pool, 7702, "tenant-sk2").await;
    common::add_direct_membership(&pool, 7702, "rg-sk2", "tenant-sk2").await;

    // The skill's md_content becomes the scanned "output" on the REST path.
    common::seed_agent_skill(
        &pool,
        "sk-out",
        "tenant-sk2",
        "report-builder",
        "# Report\nThis result contains forbidden-skill-secret in the body.",
        "Builds a report",
    )
    .await;
    common::bind_skill_to_rg(&pool, "sk-out", "rg-sk2").await;

    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-sk-out",
        "tenant-sk2",
        &["forbidden-skill-secret"],
        &["skill_output"],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-sk-out", "rg-sk2").await;

    let api_key = common::seed_api_key(&pool, "tenant-sk2", "rg-sk2", Some(7702), "k-sk2").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({
            "skill_id": "sk-out",
            "input_params": {"q": "clean input"}
        }),
        Some(&api_key),
        Some("tenant-sk2"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "skill_output guardrail must block a banned skill result; got {status}, body={body}"
    );
    assert!(
        !body.contains("forbidden-skill-secret in the body"),
        "banned skill output leaked to the client: {body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_governance_inherited_from_ancestor(pool: PgPool) {
    // `execution_mode: disabled` set on the PARENT group must be inherited by
    // the child (skill governance previously read only the resolved group).
    common::seed_tenant(&pool, "tenant-sg").await;
    common::seed_resource_group(&pool, "rg-sg-parent", "tenant-sg").await;
    common::seed_resource_group(&pool, "rg-sg-child", "tenant-sg").await;
    common::set_group_parent(
        &pool,
        "rg-sg-child",
        "rg-sg-parent",
        "/rg-sg-parent/rg-sg-child",
        1,
    )
    .await;
    common::seed_end_user(&pool, 7703, "tenant-sg").await;
    common::add_direct_membership(&pool, 7703, "rg-sg-child", "tenant-sg").await;
    common::seed_agent_skill(&pool, "sk-sg", "tenant-sg", "report-builder", "# r", "desc").await;
    common::bind_skill_to_rg(&pool, "sk-sg", "rg-sg-child").await;
    common::set_rg_skill_governance(&pool, "rg-sg-parent", json!({"execution_mode": "disabled"}))
        .await;

    let api_key = common::seed_api_key(&pool, "tenant-sg", "rg-sg-child", Some(7703), "k-sg").await;
    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);
    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({"skill_id": "sk-sg", "input_params": {"q": "clean"}}),
        Some(&api_key),
        Some("tenant-sg"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "skill execution_mode=disabled on the PARENT must be inherited by the child; got {status}, body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_execute_denies_cross_group_group_id(pool: PgPool) {
    // SECURITY regression: a caller bound to rg-a must NOT be able to execute a
    // skill (and bill/govern) against rg-b by passing an explicit group_id it
    // isn't entitled to. Previously resolve_group() honored any tenant group id.
    common::seed_tenant(&pool, "tenant-xg").await;
    common::seed_resource_group(&pool, "rg-a", "tenant-xg").await;
    common::seed_resource_group(&pool, "rg-b", "tenant-xg").await;
    common::seed_end_user(&pool, 7810, "tenant-xg").await;
    common::add_direct_membership(&pool, 7810, "rg-a", "tenant-xg").await; // member of A only

    // Skill lives in rg-b (so skill↔group access alone would pass for rg-b).
    common::seed_agent_skill(&pool, "sk-b", "tenant-xg", "b-skill", "# ok", "desc").await;
    common::bind_skill_to_rg(&pool, "sk-b", "rg-b").await;

    // API key is bound to rg-a.
    let api_key = common::seed_api_key(&pool, "tenant-xg", "rg-a", Some(7810), "k-xg").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_multimodal_app(state);

    let req = common::build_post_request(
        "/v1/proxy/skills/execute",
        json!({"skill_id": "sk-b", "group_id": "rg-b", "input_params": {"q": "clean"}}),
        Some(&api_key),
        Some("tenant-xg"),
    );
    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let body = body_to_string(response.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "cross-group explicit group_id must be forbidden; got {status}, body={body}"
    );
    assert!(
        body.to_lowercase().contains("resource group"),
        "expected an RG-authorization error, body={body}"
    );
}

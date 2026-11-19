// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! LLM compliance tagging end-to-end.
//!
//! Pins the audit-row write path stamps `compliance_tags` for
//! tenants that declared a profile, and leaves NULL for tenants
//! that didn't. The wiring lives in
//! `services::background_writer::write_proxy_log` so any audit row
//! from any route picks it up; this suite uses the LLM chat
//! completion path as the concrete vehicle.
//!
//! Four scenarios:
//!
//!   * **No profile** — pre-a070 tenant. compliance_tags stays NULL.
//!   * **Empty frameworks** — explicit `{frameworks: []}`. NULL.
//!   * **ISO 42001 declared** — quiet happy-path call. `["iso_42001"]`.
//!   * **EU AI Act + tier** — RG carries `high`, tenant lists
//!     `eu_ai_act`. Tag is `eu_ai_act.high`.

use std::time::Duration;

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

fn openai_chat_completion_fixture() -> Value {
    json!({
        "id": "chatcmpl-compliance-fixture",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    })
}

async fn seed_topology(pool: &PgPool, mock_uri: &str) -> String {
    common::seed_tenant(pool, "tenant-comp").await;
    common::seed_resource_group(pool, "rg-comp", "tenant-comp").await;
    common::seed_end_user(pool, 7001, "tenant-comp").await;
    common::add_direct_membership(pool, 7001, "rg-comp", "tenant-comp").await;
    common::seed_llm_provider(pool, "prov-comp", "tenant-comp", "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        "m-comp",
        "tenant-comp",
        "prov-comp",
        "test-gpt",
        mock_uri,
    )
    .await;
    common::bind_llm_model_to_rg(pool, "m-comp", "rg-comp").await;
    common::seed_api_key(pool, "tenant-comp", "rg-comp", Some(7001), "comp-key").await
}

async fn issue_chat_completion(pool: &PgPool, api_key: &str) -> StatusCode {
    issue_chat_completion_content(pool, api_key, "hi").await
}

async fn issue_chat_completion_content(pool: &PgPool, api_key: &str, content: &str) -> StatusCode {
    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-comp",
            "messages": [{"role": "user", "content": content}]
        }),
        Some(api_key),
        Some("tenant-comp"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let _ = body_to_json(resp.into_body()).await;
    status
}

/// background_writer is async — poll until our row shows up,
/// returning the audit row's compliance_tags JSONB value.
async fn await_compliance_tags(pool: &PgPool, tenant_id: &str) -> Option<Value> {
    for _ in 0..60 {
        let row = sqlx::query_as::<_, (Option<sqlx::types::Json<Value>>,)>(
            "SELECT compliance_tags FROM proxy_logs \
             WHERE tenant_id = $1 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(tenant_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        if let Some((tags_opt,)) = row {
            return tags_opt.map(|j| j.0);
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("background_writer never persisted a proxy_logs row for {tenant_id}");
}

/// Poll for the latest audit row's `resource_group_id` (which group the
/// request was attributed to). Same async-write caveat as
/// [`await_compliance_tags`].
async fn await_proxy_log_resource_group(pool: &PgPool, tenant_id: &str) -> Option<String> {
    for _ in 0..60 {
        let row = sqlx::query_as::<_, (Option<String>,)>(
            "SELECT resource_group_id FROM proxy_logs \
             WHERE tenant_id = $1 ORDER BY timestamp DESC LIMIT 1",
        )
        .bind(tenant_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        if let Some((rg_id,)) = row {
            return rg_id;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("background_writer never persisted a proxy_logs row for {tenant_id}");
}

// ---------------------------------------------------------------------------
// No profile — pre-a070 tenant. compliance_tags stays NULL.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_profile_leaves_compliance_tags_null(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_topology(&pool, &mock.uri()).await;
    // Note: seed_tenant materialises compliance_profile via the
    // schema default `{"frameworks": []}` — so this is the
    // "frameworks empty" case at the schema-default level.
    assert_eq!(issue_chat_completion(&pool, &api_key).await, StatusCode::OK,);

    let tags = await_compliance_tags(&pool, "tenant-comp").await;
    assert!(
        tags.is_none(),
        "tenant without frameworks must leave compliance_tags NULL — \
         got {tags:?}",
    );
}

// ---------------------------------------------------------------------------
// ISO 42001 declared — always-on tag fires on every call.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn iso_42001_profile_stamps_iso_42001_tag(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_topology(&pool, &mock.uri()).await;
    common::set_tenant_compliance_profile(
        &pool,
        "tenant-comp",
        json!({"frameworks": ["iso_42001"]}),
    )
    .await;

    assert_eq!(issue_chat_completion(&pool, &api_key).await, StatusCode::OK,);

    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "iso_42001"),
        "expected iso_42001 in {arr:?}",
    );
}

// ---------------------------------------------------------------------------
// EU AI Act tier — `eu_ai_act.<tier>` lands on every call through
// a tier-classified RG.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn eu_ai_act_tier_stamps_tier_suffix(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    let personal_key = seed_topology(&pool, &mock.uri()).await;
    common::set_tenant_compliance_profile(
        &pool,
        "tenant-comp",
        json!({"frameworks": ["eu_ai_act"]}),
    )
    .await;
    common::set_rg_eu_ai_act_tier(&pool, "rg-comp", "high").await;

    // A high-risk group refuses a person's own key (its identity floor);
    // the call is made with the group's shared key.
    assert_eq!(
        issue_chat_completion(&pool, &personal_key).await,
        StatusCode::FORBIDDEN,
    );
    let api_key =
        common::seed_shared_group_api_key(&pool, "tenant-comp", "rg-comp", "comp-shared").await;
    assert_eq!(issue_chat_completion(&pool, &api_key).await, StatusCode::OK,);

    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "eu_ai_act.high"),
        "expected eu_ai_act.high in {arr:?}",
    );
}

// ---------------------------------------------------------------------------
// Shared (`scope='global'`) group key — must attribute to the key's bound
// group, NOT an arbitrary group that merely happens to have the model.
//
// Regression for the "works in the portal, no compliance tag via curl" bug:
// a `scope='global'` key is a *shared* key bound to a group (the only shape
// the admin UI's `shared` access mode produces), not an unbound service key.
// `run_preflight` used to treat ANY global key as a binding-bypass and resolve
// the target group via "find any group with this model `LIMIT 1`" — so the
// audit row's resource_group_id (and thus cost/governance/compliance) landed
// on whichever group the scan happened to return. Here two groups bind the
// model; only the key's bound group (`rg-comp`) carries the EU AI Act tier, so
// the tag is the canary for correct attribution.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn shared_global_key_attributes_to_bound_group(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-comp").await;
    // Two groups both bind the model. `rg-other` is seeded + bound FIRST so a
    // regressed `LIMIT 1` scan is biased toward the wrong (tier-less) group.
    common::seed_resource_group(&pool, "rg-other", "tenant-comp").await;
    common::seed_resource_group(&pool, "rg-comp", "tenant-comp").await;
    common::seed_llm_provider(&pool, "prov-comp", "tenant-comp", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-comp",
        "tenant-comp",
        "prov-comp",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-comp", "rg-other").await;
    common::bind_llm_model_to_rg(&pool, "m-comp", "rg-comp").await;

    common::set_tenant_compliance_profile(
        &pool,
        "tenant-comp",
        json!({"frameworks": ["eu_ai_act"]}),
    )
    .await;
    // Only the key's bound group is tier-classified.
    common::set_rg_eu_ai_act_tier(&pool, "rg-comp", "high").await;

    // The real shared-key shape: scope='global', group_id=rg-comp, no user_id.
    let api_key =
        common::seed_shared_group_api_key(&pool, "tenant-comp", "rg-comp", "shared-key").await;

    assert_eq!(issue_chat_completion(&pool, &api_key).await, StatusCode::OK);

    // The audit row must be attributed to the bound group...
    let rg_id = await_proxy_log_resource_group(&pool, "tenant-comp").await;
    assert_eq!(
        rg_id.as_deref(),
        Some("rg-comp"),
        "shared global key must attribute to its bound group rg-comp, got {rg_id:?}",
    );

    // ...and therefore carry the EU AI Act tag, matching the portal path.
    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "eu_ai_act.high"),
        "expected eu_ai_act.high (bound group is tier 'high') in {arr:?}",
    );
}

// ---------------------------------------------------------------------------
// SOC 2 — a guardrail block stamps soc2.cc7.2 on the audit row.
// (See the note below: this is the end-to-end wire, complementing
// the `compliance.rs` unit tests of the derivation.)
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn soc2_guardrail_block_stamps_cc7_2(pool: PgPool) {
    // No upstream call — input block short-circuits.
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(0)
        .mount(&mock)
        .await;

    let api_key = seed_topology(&pool, &mock.uri()).await;
    common::set_tenant_compliance_profile(&pool, "tenant-comp", json!({"frameworks": ["soc2"]}))
        .await;
    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-soc2",
        "tenant-comp",
        &["forbidden-soc2-word"],
        true,  // chat_input
        false, // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-soc2", "rg-comp").await;

    let status =
        issue_chat_completion_content(&pool, &api_key, "please forbidden-soc2-word now").await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "banned input must be blocked"
    );

    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "soc2.cc7.2"),
        "expected soc2.cc7.2 on a guardrail-blocked row, got {arr:?}",
    );
}

// ---------------------------------------------------------------------------
// GDPR Art. 30 — PII detected for an identifiable subject stamps gdpr_art_30.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn gdpr_pii_for_identifiable_subject_stamps_art_30(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(0)
        .mount(&mock)
        .await;

    let api_key = seed_topology(&pool, &mock.uri()).await;
    common::set_tenant_compliance_profile(
        &pool,
        "tenant-comp",
        json!({"frameworks": ["gdpr_art_30"]}),
    )
    .await;
    // PII BLOCK on input → stamps pii_detected; the key is user-scoped
    // (user 7001) so the data subject is identifiable.
    common::seed_llm_pii_guardrail(
        &pool,
        "gr-gdpr",
        "tenant-comp",
        "block",
        true,  // chat_input
        false, // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-gdpr", "rg-comp").await;

    let status = issue_chat_completion_content(
        &pool,
        &api_key,
        "my email is jane.doe@example.com please help",
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "PII input must be blocked");

    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "gdpr_art_30"),
        "expected gdpr_art_30 (PII + identifiable subject), got {arr:?}",
    );
}

// ---------------------------------------------------------------------------
// Coverage note: end-to-end "guardrail block → soc2.cc7.2"
// coverage lives in the orchestrator-level test suite under
// `tests/integration/proxy_pipeline.rs::s1_guardrail_input_block_*`
// — that's where the guardrail config + bindings are seeded with
// the right depth. The compliance derivation itself is exercised
// by `src/services/compliance.rs::tests::from_log_state_*` (6
// unit tests that flip each flag and assert the expected tag).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// HIPAA rg_marked — RG flag → hipaa.rg_marked tag on every call.
// ---------------------------------------------------------------------------

#[sqlx::test(migrations = "./tests/migrations")]
async fn hipaa_rg_marked_stamps_rg_marked_tag(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture()))
        .expect(1)
        .mount(&mock)
        .await;

    let api_key = seed_topology(&pool, &mock.uri()).await;
    common::set_tenant_compliance_profile(&pool, "tenant-comp", json!({"frameworks": ["hipaa"]}))
        .await;
    common::set_rg_phi_resource(&pool, "rg-comp", true).await;

    assert_eq!(issue_chat_completion(&pool, &api_key).await, StatusCode::OK,);

    let tags = await_compliance_tags(&pool, "tenant-comp")
        .await
        .expect("Some");
    let arr = tags.as_array().expect("JSON array");
    assert!(
        arr.iter().any(|v| v == "hipaa.rg_marked"),
        "expected hipaa.rg_marked in {arr:?}",
    );
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Media governance inheritance + per-mode media quota enforcement.
//!
//! - `media_global_governance` now walks the ancestor chain (was single-group).
//! - `media_global_limits` (per-mode daily_requests/characters/budget) is now
//!   enforced (was dead config), aggregated from `proxy_logs`.
//!
//! The caller is an API key bound to the group: these run on
//! a Community gateway. The Portal-session variants are the Enterprise suite's
//! `media_quota.rs`. Both blocks fire BEFORE the upstream provider call, so no
//! wiremock is needed.

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

async fn seed_media_model(pool: &PgPool, tenant: &str, rg_id: &str, model_id: &str) {
    common::seed_llm_provider(pool, "prov-media", tenant, "openai").await;
    common::seed_llm_model_with_api_url(
        pool,
        model_id,
        tenant,
        "prov-media",
        "tts-1",
        "http://unused.invalid", // block precedes dispatch; never called
    )
    .await;
    common::set_model_mode(pool, model_id, "audio_speech").await;
    common::set_model_api_key(pool, model_id, "sk-test").await;
    common::bind_llm_model_to_rg(pool, model_id, rg_id).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn media_governance_inherited_from_ancestor(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-mg").await;
    common::seed_resource_group(&pool, "rg-mg-parent", "tenant-mg").await;
    common::seed_resource_group(&pool, "rg-mg-child", "tenant-mg").await;
    common::set_group_parent(
        &pool,
        "rg-mg-child",
        "rg-mg-parent",
        "/rg-mg-parent/rg-mg-child",
        1,
    )
    .await;
    seed_media_model(&pool, "tenant-mg", "rg-mg-child", "m-mg").await;

    // Governance ONLY on the parent: audio_speech allows only the "alloy" voice.
    common::set_rg_media_governance(
        &pool,
        "rg-mg-parent",
        json!({"audio_speech": {"voice_whitelist": ["alloy"]}}),
    )
    .await;

    let key = common::seed_api_key(&pool, "tenant-mg", "rg-mg-child", None, "k-mg").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/audio/speech",
        json!({"model": "m-mg", "voice": "nova", "input": "hello"}),
        Some(&key),
        Some("tenant-mg"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_string(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "voice whitelist set on the PARENT group must be inherited by the child and reject 'nova'; got {status}, body={body}"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn media_quota_blocks_on_daily_requests(pool: PgPool) {
    common::seed_tenant(&pool, "tenant-mq").await;
    common::seed_resource_group(&pool, "rg-mq", "tenant-mq").await;
    seed_media_model(&pool, "tenant-mq", "rg-mq", "m-mq").await;

    // daily_requests = 1, and one media request already logged today → the
    // next request must be blocked.
    common::set_rg_media_limits(
        &pool,
        "rg-mq",
        json!({"audio_speech": {"daily_requests": 1}}),
    )
    .await;
    common::seed_media_proxy_log(&pool, "tenant-mq", "rg-mq", 10, 0.001).await;

    let key = common::seed_api_key(&pool, "tenant-mq", "rg-mq", None, "k-mq").await;
    let app = common::build_multimodal_app(common::build_test_state(pool.clone()));
    let req = common::build_post_request(
        "/v1/proxy/llm/audio/speech",
        json!({"model": "m-mq", "voice": "alloy", "input": "hello"}),
        Some(&key),
        Some("tenant-mq"),
    );
    let resp = app.oneshot(req).await.expect("router call");
    let status = resp.status();
    let body = body_to_string(resp.into_body()).await;
    assert_eq!(
        status,
        StatusCode::TOO_MANY_REQUESTS,
        "media daily_requests quota must block the 2nd request; got {status}, body={body}"
    );
}

/// Each media mode's daily unit limit caps its own unit: TTS characters,
/// transcribed audio seconds, generated images, generated video seconds.
/// A unit field of another mode caps nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn each_modes_daily_unit_limit_caps_its_own_unit(pool: PgPool) {
    use brutor_gateway_core::services::usage::{MediaCharge, enforce_media_quota};
    let cases = [
        ("audio_speech", "daily_characters"),
        ("audio_transcription", "daily_audio_seconds"),
        ("image_generation", "daily_images"),
        ("video_generation", "daily_video_seconds"),
    ];
    common::seed_tenant(&pool, "tenant-mu").await;
    for (i, (mode, field)) in cases.iter().enumerate() {
        let rg = format!("rg-mu-{i}");
        common::seed_resource_group(&pool, &rg, "tenant-mu").await;
        common::set_rg_media_limits(&pool, &rg, json!({ *mode: { *field: 5 } })).await;
        let group = common::load_resource_group(&pool, &rg).await;
        let state = common::build_test_state(pool.clone());
        let charge = |units: i64| MediaCharge {
            mode,
            model_id: "m-mu",
            units,
            cost_usd: 0.0,
        };
        let refused =
            enforce_media_quota(&state.usage_state, &pool, "tenant-mu", &group, &charge(6)).await;
        assert!(
            refused.is_err(),
            "{mode}: {field} = 5 must refuse 6 units, got {:?}",
            refused.map(|r| r.is_some())
        );
        let admitted =
            enforce_media_quota(&state.usage_state, &pool, "tenant-mu", &group, &charge(5)).await;
        assert!(
            matches!(admitted, Ok(Some(_))),
            "{mode}: 5 units fit {field} = 5: {:?}",
            admitted.map(|r| r.is_some())
        );

        // Another mode's unit field is not this mode's limit.
        let other = format!("rg-mu-other-{i}");
        common::seed_resource_group(&pool, &other, "tenant-mu").await;
        let foreign = cases[(i + 1) % cases.len()].1;
        common::set_rg_media_limits(&pool, &other, json!({ *mode: { foreign: 5 } })).await;
        let group = common::load_resource_group(&pool, &other).await;
        let unlimited =
            enforce_media_quota(&state.usage_state, &pool, "tenant-mu", &group, &charge(6)).await;
        assert!(
            unlimited.is_ok(),
            "{mode}: {foreign} is not this mode's unit: {:?}",
            unlimited.map(|r| r.is_some())
        );
    }
}

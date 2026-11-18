// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The file, image, audio (speech,
//! transcription) and video routes accept an API key, governed as the chat
//! routes are.
//!
//! Before this the routes asked for a Portal session and nothing else, so a
//! Community gateway (which has no Portal) could not use surfaces the edition
//! boundary puts in Community. Every test here runs on the composition of
//! the edition running the suite (`common::build_test_state`): in the
//! Community suite that is a gateway with no extension, so no Portal.
//!
//! What is pinned, per route family:
//! * a valid API key bound to a resource group is served, and the call is
//!   recorded against the key and its group (and its end user, when the key
//!   has one);
//! * the same refusals as on the chat routes: the group does not hold the
//!   model (403), a quota is used up (429), a guardrail blocks (403), the
//!   tenant is suspended (403), each with its audit row where the chat
//!   routes write one;
//! * what must still be refused: another tenant's key, a revoked key, an
//!   expired key, a key no group governs, no credential, and every session
//!   token that is not a Portal session of a composed Portal.

use axum::Router;
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::common;

const T: &str = "tenant-mak";
const OTHER: &str = "tenant-mak-other";
const RG: &str = "rg-mak";
const RG_NO_MODEL: &str = "rg-mak-no-model";
const MODEL: &str = "m-mak";

/// The four route families that name a model.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Family {
    Speech,
    Images,
    Transcription,
    Video,
}

const FAMILIES: [Family; 4] = [
    Family::Speech,
    Family::Images,
    Family::Transcription,
    Family::Video,
];

impl Family {
    fn mode(self) -> &'static str {
        match self {
            Family::Speech => "audio_speech",
            Family::Images => "image_generation",
            Family::Transcription => "audio_transcription",
            Family::Video => "video_generation",
        }
    }
    fn path(self) -> &'static str {
        match self {
            Family::Speech => "/v1/proxy/llm/audio/speech",
            Family::Images => "/v1/proxy/llm/images/generations",
            Family::Transcription => "/v1/proxy/llm/audio/transcriptions",
            Family::Video => "/v1/proxy/llm/video/generations",
        }
    }
    /// The column of `proxy_logs` the mode's media quota counts.
    fn unit_column(self) -> &'static str {
        match self {
            Family::Speech => "media_audio_chars",
            Family::Images => "media_images_generated",
            Family::Transcription => "media_audio_seconds_input",
            Family::Video => "media_video_seconds_output",
        }
    }
    /// The guardrail surface a banned word is caught on.
    fn guardrail_surface(self) -> &'static str {
        match self {
            Family::Speech => "audio_tts_input",
            Family::Images => "image_gen_input",
            Family::Transcription => "audio_stt_output",
            Family::Video => "video_gen_input",
        }
    }
    /// What the route answers when it served the call.
    fn served(self) -> StatusCode {
        match self {
            Family::Video => StatusCode::ACCEPTED,
            _ => StatusCode::OK,
        }
    }
    /// The route's request, carrying `text` where the family carries text.
    fn request(self, key: Option<&str>, tenant: Option<&str>, text: &str) -> Request<Body> {
        match self {
            Family::Speech => common::build_post_request(
                self.path(),
                json!({"model": MODEL, "voice": "alloy", "input": text}),
                key,
                tenant,
            ),
            Family::Images => common::build_post_request(
                self.path(),
                json!({"model": MODEL, "prompt": text, "response_format": "b64_json"}),
                key,
                tenant,
            ),
            Family::Video => common::build_post_request(
                self.path(),
                json!({"model": MODEL, "prompt": text, "duration_seconds": 5}),
                key,
                tenant,
            ),
            Family::Transcription => {
                let b = "----brutormak";
                let body = format!(
                    "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n\
                     Content-Type: application/octet-stream\r\n\r\n\x00\r\n\
                     --{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{MODEL}\r\n\
                     --{b}--\r\n"
                );
                let mut builder = Request::builder()
                    .method("POST")
                    .uri(self.path())
                    .header("content-type", format!("multipart/form-data; boundary={b}"));
                if let Some(t) = tenant {
                    builder = builder.header("X-Tenant-ID", t);
                }
                if let Some(k) = key {
                    builder = builder.header("authorization", format!("Bearer {k}"));
                }
                let mut req = builder.body(Body::from(body)).expect("multipart request");
                req.extensions_mut().insert(ConnectInfo(
                    "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
                ));
                req
            }
        }
    }
}

/// The provider every family's model points at: it answers each media
/// endpoint as OpenAI does. The transcript carries `transcript`.
async fn provider(transcript: &str) -> MockServer {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/speech"))
        .respond_with(ResponseTemplate::new(200).set_body_bytes(vec![0x49, 0x44, 0x33, 0x04]))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/images/generations"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "created": 1_700_000_000, "data": [{"b64_json": "aGVsbG8="}]
        })))
        .mount(&mock)
        .await;
    Mock::given(method("POST"))
        .and(wm_path("/v1/audio/transcriptions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(json!({"text": transcript, "duration": 1.0})),
        )
        .mount(&mock)
        .await;
    mock
}

/// Tenant, two groups (one holds the model, one does not), the family's
/// model, and the other tenant. No end user and no Portal anywhere.
async fn seed(pool: &PgPool, family: Family, provider_uri: &str) {
    common::seed_tenant(pool, T).await;
    common::seed_tenant(pool, OTHER).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_resource_group(pool, RG_NO_MODEL, T).await;
    common::seed_resource_group(pool, "rg-mak-foreign", OTHER).await;
    common::seed_llm_provider(pool, "prov-mak", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-mak", MODEL, provider_uri).await;
    common::set_model_mode(pool, MODEL, family.mode()).await;
    common::set_model_api_key(pool, MODEL, "sk-test").await;
    common::bind_llm_model_to_rg(pool, MODEL, RG).await;
}

fn app(pool: &PgPool) -> Router {
    common::build_multimodal_app(common::build_test_state(pool.clone()))
}

async fn send(app: Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

/// The newest `proxy_logs` row of the tenant matching `filter`:
/// (status, subtype, group, key id, user, endpoint, evidence record).
type AuditRow = (
    Option<i32>,
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
    String,
    Option<String>,
);

async fn audit_row(pool: &PgPool, filter: &str) -> AuditRow {
    let sql = format!(
        "SELECT http_status_code, proxy_subtype, resource_group_id, api_key_id, \
                gateway_user_id, endpoint, record_id \
           FROM proxy_logs WHERE tenant_id = $1 AND {filter} ORDER BY timestamp DESC LIMIT 1"
    );
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as::<_, AuditRow>(&sql)
            .bind(T)
            .fetch_optional(pool)
            .await
            .expect("proxy_logs")
        {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("no proxy_logs row of {T} matching {filter}");
}

async fn key_id(pool: &PgPool, name: &str) -> String {
    sqlx::query_scalar("SELECT id FROM api_keys WHERE name = $1")
        .bind(name)
        .fetch_one(pool)
        .await
        .expect("key id")
}

// ---------------------------------------------------------------------------
// Accepted, and governed
// ---------------------------------------------------------------------------

async fn a_key_is_served_and_recorded(pool: PgPool, family: Family) {
    let mock = provider("a harmless transcript").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-group-key").await;

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, family.served(), "{family:?}: {body}");

    // The audit row: the key, and the key's group (no end user on this key,
    // so the call counts against the resource group).
    let (http, _, group, api_key, user, endpoint, _) =
        audit_row(&pool, "endpoint LIKE '/v1/proxy/llm/%' AND success").await;
    assert_eq!(http, Some(i32::from(family.served().as_u16())));
    assert_eq!(group.as_deref(), Some(RG), "{family:?}: the key's group");
    assert_eq!(
        api_key,
        Some(key_id(&pool, "mak-group-key").await),
        "{family:?}: the row names the key"
    );
    assert_eq!(
        user.as_deref(),
        Some(RG),
        "{family:?}: a key with no end user is counted as its group"
    );
    assert_eq!(endpoint, family.path());
}

async fn a_key_with_an_end_user_counts_against_that_user(pool: PgPool, family: Family) {
    let mock = provider("a harmless transcript").await;
    seed(&pool, family, &mock.uri()).await;
    common::seed_end_user(&pool, 9701, T).await;
    common::add_direct_membership(&pool, 9701, RG, T).await;
    let key = common::seed_api_key(&pool, T, RG, Some(9701), "mak-user-key").await;

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, family.served(), "{family:?}: {body}");
    let (_, _, group, api_key, user, _, _) =
        audit_row(&pool, "endpoint LIKE '/v1/proxy/llm/%' AND success").await;
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(api_key, Some(key_id(&pool, "mak-user-key").await));
    assert_eq!(
        user.as_deref(),
        Some("9701"),
        "{family:?}: the key's end user"
    );
}

// ---------------------------------------------------------------------------
// Refused as on the chat routes
// ---------------------------------------------------------------------------

async fn a_key_whose_group_does_not_hold_the_model_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG_NO_MODEL, None, "mak-unbound-model").await;

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{family:?}: {body}");
    assert!(body.contains("Access denied"), "{family:?}: {body}");
    let (http, subtype, _, api_key, _, endpoint, _) =
        audit_row(&pool, "proxy_subtype = 'access_denied'").await;
    assert_eq!(http, Some(403));
    assert_eq!(subtype.as_deref(), Some("access_denied"));
    assert_eq!(api_key, Some(key_id(&pool, "mak-unbound-model").await));
    assert_eq!(endpoint, "preflight_access_denied");
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: the provider was called for a refused key"
    );
}

async fn a_key_over_its_groups_quota_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-quota").await;
    // One call a day, and one already made today.
    common::set_rg_media_limits(&pool, RG, json!({family.mode(): {"daily_requests": 1}})).await;
    sqlx::query(&format!(
        "INSERT INTO proxy_logs (id, tenant_id, timestamp, proxy_type, end_user_session_id, \
         http_method, endpoint, success, resource_group_id, {}, llm_estimated_cost_usd, created_at) \
         VALUES ($1, $2, now(), 'llm', 'sess', 'POST', $3, true, $4, 1, 0.001, now())",
        family.unit_column()
    ))
    .bind(format!("pl-{}", uuid::Uuid::new_v4().simple()))
    .bind(T)
    .bind(family.path())
    .bind(RG)
    .execute(&pool)
    .await
    .expect("a media call made earlier today");

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{family:?}: {body}");
    let (http, subtype, group, api_key, _, _, _) =
        audit_row(&pool, "proxy_subtype LIKE 'rate_limit%'").await;
    assert_eq!(http, Some(429));
    assert_eq!(subtype.as_deref(), Some("rate_limit_media_quota"));
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(api_key, Some(key_id(&pool, "mak-quota").await));
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: the provider was called over quota"
    );
}

/// The chat routes' own quota (the group's daily LLM tokens) governs a media
/// call made with a key, as it governs a chat call: the one admission.
async fn a_key_over_its_groups_llm_quota_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-llm-quota").await;
    common::set_llm_global_limits(&pool, RG, json!({"tokens": {"daily_limit": 5}})).await;
    common::insert_usage_log(&pool, T, RG, None, "llm", MODEL, 50, 0.0, common::db_now()).await;

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "{family:?}: {body}");
    let (http, _, group, api_key, _, _, _) =
        audit_row(&pool, "proxy_subtype LIKE 'rate_limit%'").await;
    assert_eq!(http, Some(429));
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(api_key, Some(key_id(&pool, "mak-llm-quota").await));
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: the provider was called over quota"
    );
}

async fn a_guardrail_blocks_a_keys_call(pool: PgPool, family: Family) {
    let mock = provider("the transcript says forbidden-mak-word").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-guardrail").await;
    common::seed_banned_words_guardrail_surfaces(
        &pool,
        "gr-mak",
        T,
        &["forbidden-mak-word"],
        &[family.guardrail_surface()],
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-mak", RG).await;

    let (status, body) = send(
        app(&pool),
        family.request(Some(&key), Some(T), "say forbidden-mak-word aloud"),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{family:?}: {body}");
    let (http, _, _, api_key, _, _, _) = audit_row(&pool, "http_status_code = 403").await;
    assert_eq!(http, Some(403));
    assert_eq!(api_key, Some(key_id(&pool, "mak-guardrail").await));
    if family != Family::Transcription {
        // An input guardrail: nothing reached the provider.
        assert!(
            mock.received_requests()
                .await
                .unwrap_or_default()
                .is_empty(),
            "{family:?}: blocked input reached the provider"
        );
    } else {
        assert!(!body.contains("the transcript says"), "leaked: {body}");
    }
}

async fn a_suspended_tenants_key_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-suspended").await;
    sqlx::query("UPDATE tenants SET suspended = true WHERE id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .expect("suspend");

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{family:?}: {body}");
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: a suspended tenant reached the provider"
    );
}

// ---------------------------------------------------------------------------
// The actor planes of the chat routes: agent authorization and the AI
// System's autonomy gate
// ---------------------------------------------------------------------------

/// A key bound to an agent acts as that agent. A default-deny agent with no
/// grant for the model is refused on a media route as on a chat route, and
/// the refusal is recorded as the agent's.
async fn a_default_deny_agents_key_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-agent").await;
    sqlx::query(
        "INSERT INTO agent_identities (id, tenant_id, name, source, enabled, status, default_deny) \
         VALUES ('agent-mak', $1, 'agent-mak', 'manual', true, 'active', true)",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("agent identity");
    sqlx::query("UPDATE api_keys SET agent_id = 'agent-mak' WHERE name = 'mak-agent'")
        .execute(&pool)
        .await
        .expect("bind key to agent");

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{family:?}: {body}");
    assert!(body.contains("agent_not_authorized"), "{family:?}: {body}");
    let (http, subtype, group, api_key, _, _, _) =
        audit_row(&pool, "actor_agent_id = 'agent-mak'").await;
    assert_eq!(http, Some(403));
    assert_eq!(subtype.as_deref(), Some("agent_authz_block"));
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(api_key, Some(key_id(&pool, "mak-agent").await));
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: an agent with no grant reached the provider"
    );
}

/// The kill switch: a key of a suspended AI System is refused on a media
/// route as on a chat route.
async fn a_suspended_systems_key_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-paused").await;
    sqlx::query("UPDATE resource_groups SET autonomy_level = 'suspended' WHERE id = $1")
        .bind(RG)
        .execute(&pool)
        .await
        .expect("suspend the system");

    let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{family:?}: {body}");
    let (http, _, group, api_key, _, _, _) = audit_row(&pool, "http_status_code = 403").await;
    assert_eq!(http, Some(403));
    assert_eq!(group.as_deref(), Some(RG));
    assert_eq!(api_key, Some(key_id(&pool, "mak-paused").await));
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: a suspended system reached the provider"
    );
}

// ---------------------------------------------------------------------------
// Still refused
// ---------------------------------------------------------------------------

/// Keys that are not a credential on these routes, each asked on the tenant
/// that holds the model. Returns (name, key).
async fn keys_that_do_not_stand(pool: &PgPool) -> Vec<(&'static str, String)> {
    let foreign = common::seed_api_key(pool, OTHER, "rg-mak-foreign", None, "mak-foreign").await;
    let revoked = common::seed_api_key(pool, T, RG, None, "mak-revoked").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'mak-revoked'")
        .execute(pool)
        .await
        .expect("revoke");
    let disabled = common::seed_api_key(pool, T, RG, None, "mak-disabled").await;
    sqlx::query("UPDATE api_keys SET enabled = false WHERE name = 'mak-disabled'")
        .execute(pool)
        .await
        .expect("disable");
    let expired = common::seed_api_key(pool, T, RG, None, "mak-expired").await;
    sqlx::query(
        "UPDATE api_keys SET expires_at = now() - interval '1 hour' WHERE name = 'mak-expired'",
    )
    .execute(pool)
    .await
    .expect("expire");
    // No group governs it, and it is not the platform service key.
    let unbound = common::seed_key(pool, T, None, false, "mak-no-group").await;
    // Its group was switched off.
    common::seed_resource_group(pool, "rg-mak-off", T).await;
    let group_off = common::seed_api_key(pool, T, "rg-mak-off", None, "mak-group-off").await;
    sqlx::query("UPDATE resource_groups SET is_active = false WHERE id = 'rg-mak-off'")
        .execute(pool)
        .await
        .expect("group off");
    vec![
        ("another tenant's key", foreign),
        ("a revoked key", revoked),
        ("a disabled key", disabled),
        ("an expired key", expired),
        ("a key no group governs", unbound),
        ("a key whose group is switched off", group_off),
        (
            "a key nobody issued",
            "sk_brutor_api_test_nobody_has_this_key".to_string(),
        ),
    ]
}

async fn keys_that_do_not_stand_are_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let before = common::count_proxy_logs(&pool, T).await;
    for (what, key) in keys_that_do_not_stand(&pool).await {
        let (status, body) = send(app(&pool), family.request(Some(&key), Some(T), "hello")).await;
        assert!(
            matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
            "{family:?}: {what} answered {status}: {body}"
        );
    }
    let (status, _) = send(app(&pool), family.request(None, Some(T), "hello")).await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "{family:?}: no credential"
    );
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: a refused key reached the provider"
    );
    tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    assert_eq!(
        common::count_proxy_logs(&pool, T).await,
        before,
        "{family:?}: a refused key was recorded as a call of the tenant"
    );
}

/// Another tenant's key asked WITHOUT naming the victim tenant resolves to
/// its own tenant, where the model does not exist: never the victim's model.
async fn another_tenants_key_never_reaches_this_tenants_model(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    let foreign = common::seed_api_key(&pool, OTHER, "rg-mak-foreign", None, "mak-foreign").await;
    let (status, body) = send(app(&pool), family.request(Some(&foreign), None, "hello")).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{family:?}: {body}");
    assert!(
        mock.received_requests()
            .await
            .unwrap_or_default()
            .is_empty(),
        "{family:?}: another tenant's key reached this tenant's provider"
    );
}

// ---------------------------------------------------------------------------
// A key of a deactivated end user
// ---------------------------------------------------------------------------

/// A key bound to an end user acts as that user. Deactivating the user ends
/// the key, as revoking it does: before the first call, and between two
/// calls of one running gateway.
async fn a_deactivated_users_key_is_refused(pool: PgPool, family: Family) {
    let mock = provider("x").await;
    seed(&pool, family, &mock.uri()).await;
    common::seed_end_user(&pool, 9711, T).await;
    common::add_direct_membership(&pool, 9711, RG, T).await;
    let key = common::seed_api_key(&pool, T, RG, Some(9711), "mak-user-off").await;
    let app = app(&pool);

    let (status, body) = send(app.clone(), family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(status, family.served(), "{family:?}, while active: {body}");
    let served = mock.received_requests().await.unwrap_or_default().len();

    sqlx::query("UPDATE end_users SET is_active = false WHERE id = 9711")
        .execute(&pool)
        .await
        .expect("deactivate");
    let revoked = common::seed_api_key(&pool, T, RG, None, "mak-revoked-twin").await;
    sqlx::query("UPDATE api_keys SET revoked = true WHERE name = 'mak-revoked-twin'")
        .execute(&pool)
        .await
        .expect("revoke");
    let as_revoked = send(
        app.clone(),
        family.request(Some(&revoked), Some(T), "hello"),
    )
    .await;
    // The same running application: whatever it cached while serving the
    // first call does not keep the key alive.
    let got = send(app, family.request(Some(&key), Some(T), "hello")).await;
    assert_eq!(got.0, StatusCode::UNAUTHORIZED, "{family:?}: {}", got.1);
    assert_eq!(got, as_revoked, "{family:?}: the answer of a revoked key");
    assert_eq!(
        mock.received_requests().await.unwrap_or_default().len(),
        served,
        "{family:?}: served after the user was deactivated"
    );
}

macro_rules! per_family {
    ($($case:ident),+ $(,)?) => {$(
        mod $case {
            use super::*;
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn speech(pool: PgPool) { super::$case(pool, Family::Speech).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn images(pool: PgPool) { super::$case(pool, Family::Images).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn transcription(pool: PgPool) { super::$case(pool, Family::Transcription).await; }
            #[sqlx::test(migrations = "./tests/migrations")]
            async fn video(pool: PgPool) { super::$case(pool, Family::Video).await; }
        }
    )+};
}

per_family!(
    a_key_is_served_and_recorded,
    a_key_with_an_end_user_counts_against_that_user,
    a_key_whose_group_does_not_hold_the_model_is_refused,
    a_key_over_its_groups_quota_is_refused,
    a_key_over_its_groups_llm_quota_is_refused,
    a_guardrail_blocks_a_keys_call,
    a_suspended_tenants_key_is_refused,
    a_default_deny_agents_key_is_refused,
    a_suspended_systems_key_is_refused,
    keys_that_do_not_stand_are_refused,
    another_tenants_key_never_reaches_this_tenants_model,
    a_deactivated_users_key_is_refused,
);

#[test]
fn every_family_is_covered() {
    assert_eq!(FAMILIES.len(), 4);
}

// ---------------------------------------------------------------------------
// One call, counted once
// ---------------------------------------------------------------------------

/// A media route resolves its caller and then runs the chat routes'
/// preflight. The key's use is counted once per call, as on a chat route.
#[sqlx::test(migrations = "./tests/migrations")]
async fn one_media_call_is_one_use_of_the_key(pool: PgPool) {
    let mock = provider("x").await;
    seed(&pool, Family::Speech, &mock.uri()).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-counted").await;
    // Whatever other tests of this process left pending is flushed first.
    brutor_gateway_core::services::api_key_usage::flush_once(&pool)
        .await
        .expect("flush");
    let (status, body) = send(
        app(&pool),
        Family::Speech.request(Some(&key), Some(T), "hello"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    brutor_gateway_core::services::api_key_usage::flush_once(&pool)
        .await
        .expect("flush");
    let used: i32 =
        sqlx::query_scalar("SELECT usage_count FROM api_keys WHERE name = 'mak-counted'")
            .fetch_one(&pool)
            .await
            .expect("usage_count");
    assert_eq!(used, 1, "one call is one use of the key");
}

// ---------------------------------------------------------------------------
// Video jobs
// ---------------------------------------------------------------------------

fn job_request(method: &str, job: &str, key: &str, tenant: &str) -> Request<Body> {
    let mut req = Request::builder()
        .method(method)
        .uri(format!("/v1/proxy/llm/video/jobs/{job}"))
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {key}"))
        .body(Body::empty())
        .expect("request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_follows_and_cancels_its_tenants_video_job_and_nobody_elses(pool: PgPool) {
    seed(&pool, Family::Video, "http://unused.invalid").await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-video").await;
    let foreign = common::seed_api_key(&pool, OTHER, "rg-mak-foreign", None, "mak-foreign").await;

    let (status, body) = send(
        app(&pool),
        Family::Video.request(Some(&key), Some(T), "a red kite"),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    let job = serde_json::from_str::<Value>(&body).expect("json")["job_id"]
        .as_str()
        .expect("job_id")
        .to_string();
    let actor: Option<String> =
        sqlx::query_scalar("SELECT actor_id FROM video_generation_jobs WHERE id = $1")
            .bind(&job)
            .fetch_one(&pool)
            .await
            .expect("job row");
    assert_eq!(actor.as_deref(), Some(RG), "the job is the key's group's");

    let (status, body) = send(app(&pool), job_request("GET", &job, &key, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("pending"), "{body}");

    // Another tenant's key: refused when it names this tenant, and a 404
    // (no such job) on its own tenant. The job is untouched.
    let (status, _) = send(app(&pool), job_request("GET", &job, &foreign, T)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _) = send(app(&pool), job_request("DELETE", &job, &foreign, T)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _) = send(app(&pool), job_request("GET", &job, &foreign, OTHER)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _) = send(app(&pool), job_request("DELETE", &job, &foreign, OTHER)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // A shared key of another tenant, naming this tenant: not this tenant's
    // key, so the job is neither shown to it nor cancelled by it.
    let shared_foreign =
        common::seed_shared_group_api_key(&pool, OTHER, "rg-mak-foreign", "mak-shared-foreign")
            .await;
    for m in ["GET", "DELETE"] {
        let (status, body) = send(app(&pool), job_request(m, &job, &shared_foreign, T)).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{m}: {body}");
    }
    let state: String =
        sqlx::query_scalar("SELECT status FROM video_generation_jobs WHERE id = $1")
            .bind(&job)
            .fetch_one(&pool)
            .await
            .expect("job row");
    assert_eq!(state, "pending");

    let (status, body) = send(app(&pool), job_request("DELETE", &job, &key, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body.contains("cancelled"), "{body}");
}

/// Submits a video job as `key` and returns its id.
async fn submit_job_as(pool: &PgPool, key: &str) -> String {
    let (status, body) = send(
        app(pool),
        Family::Video.request(Some(key), Some(T), "a red kite"),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    serde_json::from_str::<Value>(&body).expect("json")["job_id"]
        .as_str()
        .expect("job_id")
        .to_string()
}

async fn job_status(pool: &PgPool, job: &str) -> String {
    sqlx::query_scalar("SELECT status FROM video_generation_jobs WHERE id = $1")
        .bind(job)
        .fetch_one(pool)
        .await
        .expect("job row")
}

/// A video job belongs to the caller that submitted it, as a file belongs to
/// its writer: another account of the same tenant that knows the job's id
/// gets the answer of a job that does not exist, and cancels nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn one_users_video_job_is_not_another_users_of_the_same_tenant(pool: PgPool) {
    seed(&pool, Family::Video, "http://unused.invalid").await;
    common::seed_resource_group(&pool, "rg-mak-second", T).await;
    for user in [9721, 9722] {
        common::seed_end_user(&pool, user, T).await;
        common::add_direct_membership(&pool, user, RG, T).await;
    }
    let alice = common::seed_api_key(&pool, T, RG, Some(9721), "mak-job-alice").await;
    let alice_again = common::seed_api_key(&pool, T, RG, Some(9721), "mak-job-alice-2").await;
    let bob = common::seed_api_key(&pool, T, RG, Some(9722), "mak-job-bob").await;
    let group_key = common::seed_api_key(&pool, T, RG, None, "mak-job-group").await;
    let group_peer = common::seed_api_key(&pool, T, RG, None, "mak-job-group-peer").await;
    let other_group = common::seed_api_key(&pool, T, "rg-mak-second", None, "mak-job-other").await;

    let job = submit_job_as(&pool, &alice).await;
    let (missing, missing_body) =
        send(app(&pool), job_request("GET", "vjob_no_such_job", &bob, T)).await;
    assert_eq!(missing, StatusCode::NOT_FOUND);
    for (who, key) in [("another user", &bob), ("a key of the group", &group_key)] {
        for m in ["GET", "DELETE"] {
            let (status, body) = send(app(&pool), job_request(m, &job, key, T)).await;
            assert_eq!(status, StatusCode::NOT_FOUND, "{who} {m}: {body}");
            assert_eq!(
                body.replace(&job, "vjob_no_such_job"),
                missing_body,
                "{who} {m}: the answer of a job that is not there"
            );
        }
        assert_eq!(
            job_status(&pool, &job).await,
            "pending",
            "{who} cancelled it"
        );
    }
    // The submitter follows it through any key of theirs, and cancels it.
    let (status, body) = send(app(&pool), job_request("GET", &job, &alice_again, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = send(app(&pool), job_request("DELETE", &job, &alice, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(job_status(&pool, &job).await, "cancelled");
    // A cancelled job is still nobody else's.
    let (status, _) = send(app(&pool), job_request("DELETE", &job, &bob, T)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    // A group's job: a key that acts as the same group follows it; a key of
    // another group and a key that acts as an end user do not.
    let job = submit_job_as(&pool, &group_key).await;
    let (status, body) = send(app(&pool), job_request("GET", &job, &group_peer, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    for (who, key) in [("another group", &other_group), ("an end user", &alice)] {
        for m in ["GET", "DELETE"] {
            let (status, body) = send(app(&pool), job_request(m, &job, key, T)).await;
            assert_eq!(status, StatusCode::NOT_FOUND, "{who} {m}: {body}");
        }
        assert_eq!(
            job_status(&pool, &job).await,
            "pending",
            "{who} cancelled it"
        );
    }

    // A row that records no submitter is nobody's.
    sqlx::query("UPDATE video_generation_jobs SET actor_id = NULL WHERE id = $1")
        .bind(&job)
        .execute(&pool)
        .await
        .expect("forget the submitter");
    for m in ["GET", "DELETE"] {
        let (status, _) = send(app(&pool), job_request(m, &job, &group_key, T)).await;
        assert_eq!(
            status,
            StatusCode::NOT_FOUND,
            "{m} of a job nobody submitted"
        );
    }
    assert_eq!(job_status(&pool, &job).await, "pending");
}

// ---------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------

fn upload(key: Option<&str>, tenant: &str) -> Request<Body> {
    let b = "----brutormakfile";
    let body = format!(
        "--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\
         Content-Type: text/plain\r\n\r\nhello file\r\n\
         --{b}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\ndocument\r\n\
         --{b}--\r\n"
    );
    let mut builder = Request::builder()
        .method("POST")
        .uri("/v1/proxy/files")
        .header("X-Tenant-ID", tenant)
        .header("content-type", format!("multipart/form-data; boundary={b}"));
    if let Some(k) = key {
        builder = builder.header("authorization", format!("Bearer {k}"));
    }
    let mut req = builder.body(Body::from(body)).expect("upload");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

fn file_request(method: &str, tail: &str, key: &str, tenant: &str) -> Request<Body> {
    let mut req = Request::builder()
        .method(method)
        .uri(format!("/v1/proxy/files/{tail}"))
        .header("X-Tenant-ID", tenant)
        .header("x-api-key", key)
        .body(Body::empty())
        .expect("request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

async fn seed_files(pool: &PgPool) {
    common::seed_tenant(pool, T).await;
    common::seed_tenant(pool, OTHER).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_resource_group(pool, "rg-mak-foreign", OTHER).await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_key_uploads_reads_and_deletes_its_tenants_files(pool: PgPool) {
    seed_files(&pool).await;
    common::seed_end_user(&pool, 9702, T).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-files").await;
    let user_key = common::seed_api_key(&pool, T, RG, Some(9702), "mak-files-user").await;
    let app = app(&pool);

    let (status, body) = send(app.clone(), upload(Some(&key), T)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let id = serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string();
    let (tenant, actor): (String, Option<String>) =
        sqlx::query_as("SELECT tenant_id, actor_id FROM media_files WHERE id = $1")
            .bind(&id)
            .fetch_one(&pool)
            .await
            .expect("file row");
    assert_eq!(tenant, T);
    assert_eq!(
        actor.as_deref(),
        Some(RG),
        "a key with no end user: its group"
    );

    // A key with an end user: the file is that user's.
    let (status, body) = send(app.clone(), upload(Some(&user_key), T)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let user_file = serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string();
    let actor: Option<String> =
        sqlx::query_scalar("SELECT actor_id FROM media_files WHERE id = $1")
            .bind(&user_file)
            .fetch_one(&pool)
            .await
            .expect("file row");
    assert_eq!(actor.as_deref(), Some("9702"));

    let (status, body) = send(app.clone(), file_request("GET", &id, &key, T)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = send(
        app.clone(),
        file_request("GET", &format!("{id}/url"), &key, T),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let (status, body) = send(
        app.clone(),
        file_request("GET", &format!("{id}/content"), &key, T),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body, "hello file");
    let (status, body) = send(app.clone(), file_request("DELETE", &id, &key, T)).await;
    assert!(status.is_success(), "{status}: {body}");
    // Soft-deleted: its bytes and its URL are gone.
    for tail in [format!("{id}/url"), format!("{id}/content")] {
        let (status, body) = send(app.clone(), file_request("GET", &tail, &key, T)).await;
        assert_eq!(status, StatusCode::GONE, "{tail} of a deleted file: {body}");
    }
}

/// A file belongs to the caller that wrote it: the end user, or the
/// resource group a key without an end user (or an agent) acts in. Another
/// account of the same tenant that knows the file's id gets the answer of a
/// file that does not exist, on every per-file route.
async fn upload_as(app: &Router, key: &str) -> String {
    let (status, body) = send(app.clone(), upload(Some(key), T)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string()
}

/// What each per-file route answers `key` for file `id`, in the order
/// metadata, url, content.
async fn reads(app: &Router, id: &str, key: &str) -> Vec<StatusCode> {
    let mut seen = Vec::new();
    for tail in [id.to_string(), format!("{id}/url"), format!("{id}/content")] {
        seen.push(
            send(app.clone(), file_request("GET", &tail, key, T))
                .await
                .0,
        );
    }
    seen
}

async fn deleted_at(pool: &PgPool, id: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    sqlx::query_scalar("SELECT deleted_at FROM media_files WHERE id = $1")
        .bind(id)
        .fetch_one(pool)
        .await
        .expect("file row")
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn one_users_file_is_not_another_users_of_the_same_tenant(pool: PgPool) {
    seed_files(&pool).await;
    common::seed_end_user(&pool, 9711, T).await;
    common::seed_end_user(&pool, 9712, T).await;
    let alice = common::seed_api_key(&pool, T, RG, Some(9711), "mak-own-alice").await;
    let alice_again = common::seed_api_key(&pool, T, RG, Some(9711), "mak-own-alice-2").await;
    let bob = common::seed_api_key(&pool, T, RG, Some(9712), "mak-own-bob").await;
    let group_key = common::seed_api_key(&pool, T, RG, None, "mak-own-group").await;
    let app = app(&pool);
    let file = upload_as(&app, &alice).await;

    // What a file that does not exist answers: the answer for a file that
    // is somebody else's.
    let missing = reads(&app, "no-such-file", &bob).await;
    assert_eq!(missing, [StatusCode::NOT_FOUND; 3]);
    for (who, key) in [
        ("another user", &bob),
        ("a key of the user's group", &group_key),
    ] {
        assert_eq!(reads(&app, &file, key).await, missing, "{who} reads it");
        let (status, _) = send(app.clone(), file_request("DELETE", &file, key, T)).await;
        let (unknown, _) = send(app.clone(), file_request("DELETE", "no-such-file", key, T)).await;
        assert_eq!(
            status, unknown,
            "{who}: the answer of a file that is not there"
        );
        // DELETE is idempotent: a file that is not there is already gone
        // (204), where the three reads answer 404. Another caller's file
        // gets that same 204, so the answer says nothing about whether the
        // id exists; the file itself is untouched (below).
        assert_eq!(
            status,
            StatusCode::NO_CONTENT,
            "{who}: DELETE of a file that is not the caller's"
        );
        assert!(deleted_at(&pool, &file).await.is_none(), "{who} deleted it");
    }
    let (reads_counted,): (i32,) =
        sqlx::query_as("SELECT access_count FROM media_files WHERE id = $1")
            .bind(&file)
            .fetch_one(&pool)
            .await
            .expect("file row");
    assert_eq!(reads_counted, 0, "a refused read was counted as an access");

    // The owner still can, through any key of theirs.
    assert_eq!(reads(&app, &file, &alice).await, [StatusCode::OK; 3]);
    assert_eq!(reads(&app, &file, &alice_again).await, [StatusCode::OK; 3]);
    let (status, body) = send(app.clone(), file_request("DELETE", &file, &alice_again, T)).await;
    assert!(status.is_success(), "{status}: {body}");
    assert!(deleted_at(&pool, &file).await.is_some());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_groups_file_is_its_groups_and_no_other_groups(pool: PgPool) {
    seed_files(&pool).await;
    common::seed_resource_group(&pool, "rg-mak-second", T).await;
    common::seed_end_user(&pool, 9713, T).await;
    let writer = common::seed_api_key(&pool, T, RG, None, "mak-grp-writer").await;
    let same_group = common::seed_api_key(&pool, T, RG, None, "mak-grp-peer").await;
    let other_group = common::seed_api_key(&pool, T, "rg-mak-second", None, "mak-grp-other").await;
    let member = common::seed_api_key(&pool, T, RG, Some(9713), "mak-grp-member").await;
    let app = app(&pool);
    let file = upload_as(&app, &writer).await;

    // Another key that acts as the same group reads the group's file.
    assert_eq!(reads(&app, &file, &same_group).await, [StatusCode::OK; 3]);
    // A key of another group, and a key that acts as an end user, do not.
    for (who, key) in [("another group", &other_group), ("an end user", &member)] {
        assert_eq!(
            reads(&app, &file, key).await,
            [StatusCode::NOT_FOUND; 3],
            "{who} reads it"
        );
        send(app.clone(), file_request("DELETE", &file, key, T)).await;
        assert!(deleted_at(&pool, &file).await.is_none(), "{who} deleted it");
    }
    let (status, body) = send(app.clone(), file_request("DELETE", &file, &same_group, T)).await;
    assert!(status.is_success(), "{status}: {body}");
    assert!(deleted_at(&pool, &file).await.is_some());
}

/// A row that records no writer is nobody's: no caller reads or deletes it.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_file_with_no_recorded_writer_is_read_by_nobody(pool: PgPool) {
    seed_files(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-nobody").await;
    let app = app(&pool);
    let file = upload_as(&app, &key).await;
    sqlx::query("UPDATE media_files SET actor_id = NULL WHERE id = $1")
        .bind(&file)
        .execute(&pool)
        .await
        .expect("forget the writer");
    assert_eq!(reads(&app, &file, &key).await, [StatusCode::NOT_FOUND; 3]);
    send(app.clone(), file_request("DELETE", &file, &key, T)).await;
    assert!(deleted_at(&pool, &file).await.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn keys_that_do_not_stand_never_reach_a_tenants_files(pool: PgPool) {
    seed_files(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-files").await;
    let app = app(&pool);
    let (status, body) = send(app.clone(), upload(Some(&key), T)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let id = serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string();

    for (what, bad) in keys_that_do_not_stand(&pool).await {
        for (m, tail) in [
            ("GET", id.clone()),
            ("GET", format!("{id}/url")),
            ("GET", format!("{id}/content")),
            ("DELETE", id.clone()),
        ] {
            let (status, body) = send(app.clone(), file_request(m, &tail, &bad, T)).await;
            assert!(
                matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
                "{what}: {m} {tail} answered {status}: {body}"
            );
        }
        let (status, body) = send(app.clone(), upload(Some(&bad), T)).await;
        assert!(
            matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
            "{what}: upload answered {status}: {body}"
        );
    }
    let (status, _) = send(app.clone(), upload(None, T)).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "no credential");

    // Another tenant's key on its own tenant: this tenant's file is not there.
    let foreign_key =
        common::seed_api_key(&pool, OTHER, "rg-mak-foreign", None, "mak-foreign-2").await;
    for (m, tail) in [
        ("GET", id.clone()),
        ("GET", format!("{id}/url")),
        ("GET", format!("{id}/content")),
    ] {
        let (status, body) = send(app.clone(), file_request(m, &tail, &foreign_key, OTHER)).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{m} {tail}: {body}");
    }
    // A delete is idempotent ("not there" is a 204): it deleted nothing.
    let (status, _) = send(
        app.clone(),
        file_request("DELETE", &id, &foreign_key, OTHER),
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    // Still there, still this tenant's, not deleted.
    let deleted: Option<chrono::DateTime<chrono::Utc>> =
        sqlx::query_scalar("SELECT deleted_at FROM media_files WHERE id = $1 AND tenant_id = $2")
            .bind(&id)
            .bind(T)
            .fetch_one(&pool)
            .await
            .expect("file row");
    assert!(deleted.is_none());
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_storage_quota_and_suspension_govern_a_keys_upload(pool: PgPool) {
    seed_files(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-files").await;
    sqlx::query("UPDATE tenants SET media_storage_quota_bytes = 4 WHERE id = $1")
        .bind(T)
        .execute(&pool)
        .await
        .expect("quota");
    let (status, body) = send(app(&pool), upload(Some(&key), T)).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
    let files: i64 = sqlx::query_scalar("SELECT count(*) FROM media_files WHERE tenant_id = $1")
        .bind(T)
        .fetch_one(&pool)
        .await
        .expect("count");
    assert_eq!(files, 0, "nothing was stored over the quota");

    sqlx::query(
        "UPDATE tenants SET media_storage_quota_bytes = NULL, suspended = true WHERE id = $1",
    )
    .bind(T)
    .execute(&pool)
    .await
    .expect("suspend");
    let (status, body) = send(app(&pool), upload(Some(&key), T)).await;
    assert_eq!(status, StatusCode::FORBIDDEN, "{body}");
}

/// A global-scope key of another tenant is not re-scoped to the tenant a
/// request names (unless the operator allow-listed it); it stays its own
/// tenant's key and is refused here, never served this tenant's data.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_shared_key_of_another_tenant_is_not_this_tenants_key(pool: PgPool) {
    seed_files(&pool).await;
    let key = common::seed_api_key(&pool, T, RG, None, "mak-files").await;
    let app = app(&pool);
    let (status, body) = send(app.clone(), upload(Some(&key), T)).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let id = serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string();

    let shared_foreign =
        common::seed_shared_group_api_key(&pool, OTHER, "rg-mak-foreign", "mak-shared-foreign")
            .await;
    for (m, tail) in [
        ("GET", id.clone()),
        ("GET", format!("{id}/url")),
        ("GET", format!("{id}/content")),
        ("DELETE", id.clone()),
    ] {
        let (status, body) = send(app.clone(), file_request(m, &tail, &shared_foreign, T)).await;
        assert!(
            matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
            "{m} {tail} answered {status}: {body}"
        );
    }
    let (status, body) = send(app.clone(), upload(Some(&shared_foreign), T)).await;
    assert!(
        matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN),
        "upload answered {status}: {body}"
    );
    let rows: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM media_files WHERE tenant_id = $1 AND deleted_at IS NULL",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("count");
    assert_eq!(
        rows, 1,
        "the other tenant's key wrote or deleted in this tenant"
    );
}

// ---------------------------------------------------------------------------
// Workload-identity tokens
// ---------------------------------------------------------------------------
//
// An agent that authenticates with its own IdP-issued token (`services::
// workload_identity`) is a caller of the chat routes. The media routes
// refused it with every credential that is not an API key or a Portal
// session. Decision: accepted, governed as on chat — the agent acts in its
// own resource group, its grants and its AI System's autonomy decide, the
// group's limits and guardrails apply. A token that does not verify for this
// tenant (expired, tampered, another tenant's IdP, an unknown or disabled
// agent) stays refused.

use super::workload_identity as wl;

const WL_ISSUER: &str = "https://idp.media.example/";
const WL_SUB: &str = "spiffe://media/agent";
const WL_AGENT: &str = "agent-mak-wl";

struct Workload {
    /// Kept alive: the IdP's key set is fetched from it.
    _jwks: MockServer,
    kid: String,
}

impl Workload {
    fn token(&self, exp_from_now: i64) -> String {
        let exp = (common::db_now().timestamp() + exp_from_now) as usize;
        wl::mint_token(WL_ISSUER, WL_SUB, &self.kid, exp)
    }
}

/// An IdP of tenant `T`, and an agent it authenticates, a member of `RG`
/// with an `allow` grant for the model.
async fn seed_workload(pool: &PgPool, tag: &str) -> Workload {
    // The key-set cache is per process and keyed by URI: a key id per test.
    let kid = format!("kid-mak-{tag}");
    let jwks = wl::mock_jwks(&kid).await;
    wl::seed_idp(
        pool,
        "idp-mak",
        T,
        WL_ISSUER,
        &format!("{}/jwks", jwks.uri()),
    )
    .await;
    wl::seed_agent_with_idp(pool, WL_AGENT, T, "idp-mak", WL_SUB).await;
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id) \
         VALUES ($1, $2, $3)",
    )
    .bind(T)
    .bind(WL_AGENT)
    .bind(RG)
    .execute(pool)
    .await
    .expect("membership");
    sqlx::query(
        "INSERT INTO agent_grants (id, tenant_id, agent_id, action_type, target, effect, enabled) \
         VALUES ('g-mak-wl', $1, $2, 'llm_call', $3, 'allow', true)",
    )
    .bind(T)
    .bind(WL_AGENT)
    .bind(MODEL)
    .execute(pool)
    .await
    .expect("grant");
    Workload { _jwks: jwks, kid }
}

/// A valid workload token is a caller on every model family, and the agent
/// is served there as on the chat routes:
/// it is a member of the group that holds the model and holds an `allow`
/// grant for it. The row names the agent and its group.
///
/// Model access knows a workload identity as it knows a key's
/// group and an end user's memberships: the agent is not refused 403
/// `access_denied` here, exactly as on chat.
async fn a_workload_identity_is_a_caller_and_gets_the_chat_routes_answer(
    pool: PgPool,
    family: Family,
) {
    let mock = provider("a harmless transcript").await;
    seed(&pool, family, &mock.uri()).await;
    let workload = seed_workload(&pool, &format!("ok-{family:?}")).await;
    let token = workload.token(3600);

    // Chat lets the same token through authentication, model access and the
    // actor planes for the same model (the media provider then answers
    // something chat cannot read: not a refusal of the caller).
    let chat = common::build_llm_app(common::build_test_state(pool.clone()));
    let (chat_status, chat_body) = send(
        chat,
        common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": MODEL, "messages": [{"role": "user", "content": "hello"}]}),
            Some(&token),
            Some(T),
        ),
    )
    .await;
    assert!(
        !matches!(
            chat_status,
            StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN
        ),
        "chat refuses the agent: {chat_status}: {chat_body}"
    );

    let (status, body) = send(app(&pool), family.request(Some(&token), Some(T), "hello")).await;
    assert_eq!(status, family.served(), "{family:?}: {body}");
    let served: (Option<String>, Option<String>, Option<String>) = {
        let mut found = None;
        for _ in 0..160 {
            found = sqlx::query_as(
                "SELECT actor_agent_id, resource_group_id, api_key_id FROM proxy_logs \
                  WHERE tenant_id = $1 AND endpoint = $2 AND success = true \
                  ORDER BY timestamp DESC LIMIT 1",
            )
            .bind(T)
            .bind(family.path())
            .fetch_optional(&pool)
            .await
            .expect("proxy_logs");
            if found.is_some() {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
        found.unwrap_or_else(|| panic!("{family:?}: no row of the served call"))
    };
    assert_eq!(served.0.as_deref(), Some(WL_AGENT), "{family:?}: the agent");
    assert_eq!(served.1.as_deref(), Some(RG), "{family:?}: its group");
    assert_eq!(served.2, None, "{family:?}: no key was presented");
}

/// Tokens that do not stand for this tenant are refused (401) on every
/// family and never reach the provider.
async fn workload_tokens_that_do_not_stand_are_refused(pool: PgPool, family: Family) {
    let mock = provider("a harmless transcript").await;
    seed(&pool, family, &mock.uri()).await;
    let workload = seed_workload(&pool, &format!("bad-{family:?}")).await;

    let valid = workload.token(3600);
    let mut tampered = valid.clone();
    tampered.push('x');
    let exp = (common::db_now().timestamp() + 3600) as usize;
    let cases = [
        ("expired", workload.token(-3600), T),
        ("tampered", tampered, T),
        (
            "an unknown subject",
            wl::mint_token(WL_ISSUER, "spiffe://media/nobody", &workload.kid, exp),
            T,
        ),
        (
            "an issuer the tenant does not trust",
            wl::mint_token("https://idp.elsewhere.example/", WL_SUB, &workload.kid, exp),
            T,
        ),
        // Valid for tenant T, presented to another tenant: that tenant has
        // no such IdP and no such agent.
        ("another tenant's token", valid.clone(), OTHER),
    ];
    for (what, token, tenant) in cases {
        let (status, body) = send(
            app(&pool),
            family.request(Some(&token), Some(tenant), "hello"),
        )
        .await;
        assert_eq!(
            status,
            StatusCode::UNAUTHORIZED,
            "{family:?}, {what}: {body}"
        );
    }

    // A disabled agent's token is no credential.
    sqlx::query("UPDATE agent_identities SET enabled = false WHERE id = $1")
        .bind(WL_AGENT)
        .execute(&pool)
        .await
        .expect("disable");
    let (status, body) = send(app(&pool), family.request(Some(&valid), Some(T), "hello")).await;
    assert_eq!(
        status,
        StatusCode::UNAUTHORIZED,
        "{family:?}, disabled: {body}"
    );
    assert_eq!(mock.received_requests().await.unwrap_or_default().len(), 0);
}

per_family!(
    a_workload_identity_is_a_caller_and_gets_the_chat_routes_answer,
    workload_tokens_that_do_not_stand_are_refused,
);

fn bearer_file_request(method: &str, tail: &str, token: &str, tenant: &str) -> Request<Body> {
    let uri = if tail.is_empty() {
        "/v1/proxy/files".to_string()
    } else {
        format!("/v1/proxy/files/{tail}")
    };
    let mut req = Request::builder()
        .method(method)
        .uri(uri)
        .header("X-Tenant-ID", tenant)
        .header("authorization", format!("Bearer {token}"))
        .body(Body::empty())
        .expect("request");
    req.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:55555".parse::<std::net::SocketAddr>().unwrap(),
    ));
    req
}

/// The routes that name no model (files, a video job's status and
/// cancellation) have no preflight: the token is the whole decision. A
/// workload identity that stands uploads, reads and deletes its tenant's
/// files and is answered about its tenant's jobs; one that does not stand,
/// one presented to another tenant, and one of an agent in no resource group
/// (whatever group a header names) get nothing.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_workload_identity_reaches_its_tenants_files_and_jobs_and_nobody_elses(pool: PgPool) {
    seed_files(&pool).await;
    let workload = seed_workload(&pool, "files").await;
    let token = workload.token(3600);
    let app = app(&pool);

    let (status, body) = send(app.clone(), upload(Some(&token), T)).await;
    assert_eq!(status, StatusCode::CREATED, "upload: {body}");
    let file_id = serde_json::from_str::<Value>(&body).expect("json")["id"]
        .as_str()
        .expect("id")
        .to_string();
    let (tenant, actor): (String, Option<String>) =
        sqlx::query_as("SELECT tenant_id, actor_id FROM media_files WHERE id = $1")
            .bind(&file_id)
            .fetch_one(&pool)
            .await
            .expect("file row");
    assert_eq!(tenant, T);
    assert_eq!(actor.as_deref(), Some(RG), "the agent's own group");

    for tail in [
        file_id.clone(),
        format!("{file_id}/url"),
        format!("{file_id}/content"),
    ] {
        let (status, body) = send(app.clone(), bearer_file_request("GET", &tail, &token, T)).await;
        assert_eq!(status, StatusCode::OK, "GET {tail}: {body}");
    }

    // A video job that does not exist: the route's own answer, not a 401.
    for method in ["GET", "DELETE"] {
        let (status, body) = send(app.clone(), job_request(method, "no-such-job", &token, T)).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{method} job: {body}");
    }

    // Refused: expired, and the valid token presented to another tenant.
    let expired = workload.token(-3600);
    for (what, t, tenant) in [
        ("expired", expired.as_str(), T),
        ("another tenant", token.as_str(), OTHER),
    ] {
        for req in [
            upload(Some(t), tenant),
            bearer_file_request("GET", &file_id, t, tenant),
            bearer_file_request("GET", &format!("{file_id}/url"), t, tenant),
            bearer_file_request("GET", &format!("{file_id}/content"), t, tenant),
            bearer_file_request("DELETE", &file_id, t, tenant),
            job_request("GET", "no-such-job", t, tenant),
            job_request("DELETE", "no-such-job", t, tenant),
        ] {
            let (status, body) = send(app.clone(), req).await;
            assert_eq!(status, StatusCode::UNAUTHORIZED, "{what}: {body}");
        }
    }

    // An agent in no resource group is governed by no group: refused, also
    // when the request names a group in a header.
    sqlx::query("DELETE FROM resource_group_agent_members WHERE agent_id = $1")
        .bind(WL_AGENT)
        .execute(&pool)
        .await
        .expect("leave the group");
    let mut named_group = bearer_file_request("GET", &file_id, &token, T);
    named_group
        .headers_mut()
        .insert("x-resource-group", RG.parse().unwrap());
    let (status, body) = send(app.clone(), named_group).await;
    // An agent in no group is refused at authentication (403
    // `AGENT_NOT_BOUND`), whether or not it names a group.
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "an agent in no group names one in a header: {body}"
    );
    assert!(body.contains("AGENT_NOT_BOUND"), "{body}");
    let (status, body) = send(app.clone(), bearer_file_request("GET", &file_id, &token, T)).await;
    assert_eq!(
        status,
        StatusCode::FORBIDDEN,
        "an agent in no group: {body}"
    );
    assert!(body.contains("AGENT_NOT_BOUND"), "{body}");

    // Back in its group, it deletes the file.
    sqlx::query(
        "INSERT INTO resource_group_agent_members (tenant_id, agent_id, group_id) \
         VALUES ($1, $2, $3)",
    )
    .bind(T)
    .bind(WL_AGENT)
    .bind(RG)
    .execute(&pool)
    .await
    .expect("membership");
    let (status, body) = send(
        app.clone(),
        bearer_file_request("DELETE", &file_id, &token, T),
    )
    .await;
    assert!(status.is_success(), "delete: {status}: {body}");
}

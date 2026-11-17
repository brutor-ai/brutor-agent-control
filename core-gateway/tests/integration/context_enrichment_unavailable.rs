// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A grounded group whose context cannot be retrieved.
//!
//! A resource group bound to knowledge-base collections is grounded: its
//! answers are to be built from the passages the asker may see. When the
//! retrieval could not be carried out, the chat route answered anyway, from
//! the model's own knowledge, and the audit row was that of a request
//! nothing was retrieved for. A control that cannot decide refuses: the
//! request is now refused (503), nothing is sent to the model, and the
//! refusal is recorded with its reason.
//!
//! The route is Community code and the knowledge base is not, so the hook
//! is filled here with an enricher that says what the test needs: the
//! retrieval failed, or it succeeded and found nothing (which is not a
//! failure). The Enterprise suite asks the same of the real knowledge base
//! (`context_enricher_surfaces`).

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{method, path as wm_path};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::models::auth::AuthContext;
use brutor_gateway_core::models::db::ResourceGroup;
use brutor_gateway_core::services::AppState;
use brutor_gateway_core::services::cache::group_cache::EffectiveGroupData;
use brutor_gateway_core::services::context_enrichment::{
    ContextEnricher, ContextEnrichmentSession, EnrichmentUnavailable, RetrievedContext,
};

use crate::common;

const T: &str = "tenant-ceu";
const RG: &str = "rg-ceu";
const RG_PLAIN: &str = "rg-ceu-plain";
const MODEL: &str = "m-ceu";

#[derive(Clone, Copy)]
enum Retrieval {
    Fails,
    FindsNothing,
}

/// An enricher for which `RG` is grounded and every other group is not.
struct Stub {
    retrieval: Retrieval,
    asked: Arc<AtomicUsize>,
}

struct StubSession {
    retrieval: Retrieval,
    asked: Arc<AtomicUsize>,
}

#[async_trait]
impl ContextEnrichmentSession for StubSession {
    fn grounded(&self) -> bool {
        true
    }
    fn context_bound_bytes(&self) -> usize {
        4096
    }
    async fn retrieve(
        &self,
        _query: &str,
        _asker: &AuthContext,
        _auth_headers: &[(String, String)],
    ) -> Result<Option<RetrievedContext>, EnrichmentUnavailable> {
        self.asked.fetch_add(1, Ordering::SeqCst);
        match self.retrieval {
            Retrieval::Fails => Err(EnrichmentUnavailable),
            Retrieval::FindsNothing => Ok(None),
        }
    }
}

#[async_trait]
impl ContextEnricher for Stub {
    fn reserve_bytes(&self, _group: &EffectiveGroupData) -> usize {
        0
    }
    async fn session(
        &self,
        _state: &AppState,
        _tenant_id: &str,
        group: Option<&ResourceGroup>,
    ) -> Arc<dyn ContextEnrichmentSession> {
        match group {
            Some(g) if g.id == RG => Arc::new(StubSession {
                retrieval: self.retrieval,
                asked: Arc::clone(&self.asked),
            }),
            _ => brutor_gateway_core::services::context_enrichment::no_session(),
        }
    }
}

struct World {
    app: Router,
    mock: MockServer,
    asked: Arc<AtomicUsize>,
    key: String,
    plain_key: String,
}

async fn world(pool: &PgPool, retrieval: Retrieval) -> World {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-ceu", "object": "chat.completion", "created": 1_700_000_000,
            "model": "test-gpt",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        })))
        .mount(&mock)
        .await;
    common::seed_tenant(pool, T).await;
    common::seed_llm_provider(pool, "prov-ceu", T, "openai").await;
    common::seed_llm_model_with_api_url(pool, MODEL, T, "prov-ceu", "test-gpt", &mock.uri()).await;
    for group in [RG, RG_PLAIN] {
        common::seed_resource_group(pool, group, T).await;
        common::bind_llm_model_to_rg(pool, MODEL, group).await;
    }
    let key = common::seed_api_key(pool, T, RG, None, "ceu-key").await;
    let plain_key = common::seed_api_key(pool, T, RG_PLAIN, None, "ceu-plain-key").await;

    let asked = Arc::new(AtomicUsize::new(0));
    let mut state = common::new_state(pool.clone());
    state.context_enricher = Arc::new(Stub {
        retrieval,
        asked: Arc::clone(&asked),
    });
    World {
        app: common::build_llm_app(Arc::new(state)),
        mock,
        asked,
        key,
        plain_key,
    }
}

fn chat(key: &str, stream: bool) -> Request<Body> {
    common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": MODEL,
            "stream": stream,
            "messages": [{"role": "user", "content": "what does the handbook say"}]
        }),
        Some(key),
        Some(T),
    )
}

async fn send(app: &Router, req: Request<Body>) -> (StatusCode, String) {
    let resp = app.clone().oneshot(req).await.expect("router");
    let status = resp.status();
    let bytes = resp.into_body().collect().await.expect("body").to_bytes();
    (status, String::from_utf8_lossy(&bytes).into_owned())
}

async fn provider_bodies(mock: &MockServer) -> Vec<Value> {
    mock.received_requests()
        .await
        .unwrap_or_default()
        .iter()
        .map(|r| serde_json::from_slice(&r.body).expect("json"))
        .collect()
}

/// A refusal row: (subtype, error type, group, message).
type RefusalRow = (
    Option<String>,
    Option<String>,
    Option<String>,
    Option<String>,
);

/// The retrieval failed: the request is refused, streamed or not, nothing
/// goes to the model, and the refusal is on the record with its reason, on
/// the caller's group and key.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grounded_request_whose_retrieval_fails_is_refused(pool: PgPool) {
    let w = world(&pool, Retrieval::Fails).await;
    for stream in [false, true] {
        let (status, body) = send(&w.app, chat(&w.key, stream)).await;
        assert_eq!(
            status,
            StatusCode::SERVICE_UNAVAILABLE,
            "stream={stream}: a grounded request was answered without its grounding: {body}"
        );
        assert!(
            body.contains("knowledge_base_unavailable"),
            "stream={stream}: {body}"
        );
    }
    assert_eq!(w.asked.load(Ordering::SeqCst), 2, "one retrieval each");
    assert!(
        provider_bodies(&w.mock).await.is_empty(),
        "an ungrounded request reached the model"
    );

    let mut rows: Vec<RefusalRow> = Vec::new();
    for _ in 0..120 {
        rows = sqlx::query_as(
            "SELECT proxy_subtype, error_type, resource_group_id, error_message FROM proxy_logs \
              WHERE tenant_id = $1 AND http_status_code = 503",
        )
        .bind(T)
        .fetch_all(&pool)
        .await
        .expect("proxy_logs");
        if rows.len() >= 2 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(rows.len(), 2, "one refusal row per request: {rows:?}");
    for (subtype, error_type, group, message) in rows {
        assert_eq!(subtype.as_deref(), Some("engine_failure"));
        assert_eq!(error_type.as_deref(), Some("knowledge_base_unavailable"));
        assert_eq!(group.as_deref(), Some(RG));
        assert!(
            message
                .as_deref()
                .unwrap_or_default()
                .contains("could not be retrieved"),
            "{message:?}"
        );
    }
    // Nothing of the tenant was answered.
    let answered: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM proxy_logs WHERE tenant_id = $1 AND http_status_code = 200",
    )
    .bind(T)
    .fetch_one(&pool)
    .await
    .expect("count");
    assert_eq!(answered, 0);
}

/// A retrieval that succeeds and finds nothing is not a failure: the
/// request is answered as it was written, streamed or not.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_grounded_request_with_no_hits_is_answered(pool: PgPool) {
    let w = world(&pool, Retrieval::FindsNothing).await;
    for stream in [false, true] {
        let (status, body) = send(&w.app, chat(&w.key, stream)).await;
        assert_eq!(status, StatusCode::OK, "stream={stream}: {body}");
    }
    assert_eq!(w.asked.load(Ordering::SeqCst), 2);
    let sent = provider_bodies(&w.mock).await;
    assert_eq!(sent.len(), 2);
    for body in sent {
        let roles: Vec<&str> = body["messages"]
            .as_array()
            .expect("messages")
            .iter()
            .map(|m| m["role"].as_str().unwrap_or_default())
            .collect();
        assert_eq!(roles, ["user"], "nothing was injected: {body}");
    }
}

/// A group that is not grounded is not asked about and not affected, also
/// while the knowledge base is failing for the group that is.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_group_with_no_knowledge_base_is_unaffected(pool: PgPool) {
    let w = world(&pool, Retrieval::Fails).await;
    for stream in [false, true] {
        let (status, body) = send(&w.app, chat(&w.plain_key, stream)).await;
        assert_eq!(status, StatusCode::OK, "stream={stream}: {body}");
    }
    assert_eq!(w.asked.load(Ordering::SeqCst), 0);
    assert_eq!(provider_bodies(&w.mock).await.len(), 2);
}

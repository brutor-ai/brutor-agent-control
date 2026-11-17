// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// A tenant-configured URL whose host is an IP literal never reaches
// the outbound DNS resolver, so before the fix an LLM model whose `api_url`
// pointed at the cloud metadata service was fetched with the proxy's network
// position. End to end through the real route: the call is refused as a
// policy decision (403) before anything is sent, on every LLM-shaped
// surface that resolves a model.

use axum::body::Body;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use crate::common;

async fn body_json(body: Body) -> Value {
    let bytes = body.collect().await.unwrap().to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::String(String::from_utf8_lossy(&bytes).into()))
}

/// One tenant with one model at `api_url`. Ids are per tenant (`n`): the
/// tables key on them globally, so a shared id would leave the second
/// tenant without its model.
async fn tenant_with_model(pool: &PgPool, n: i32, tenant: &str, api_url: &str) -> String {
    let (rg, prov, model) = (
        format!("rg-ssrf-{n}"),
        format!("prov-ssrf-{n}"),
        format!("m-ssrf-{n}"),
    );
    let user = 7300 + n;
    common::seed_tenant(pool, tenant).await;
    common::seed_resource_group(pool, &rg, tenant).await;
    common::seed_end_user(pool, user, tenant).await;
    common::add_direct_membership(pool, user, &rg, tenant).await;
    common::seed_llm_provider(pool, &prov, tenant, "openai").await;
    common::seed_llm_model_with_api_url(pool, &model, tenant, &prov, "ssrf-gpt", api_url).await;
    common::bind_llm_model_to_rg(pool, &model, &rg).await;
    common::seed_api_key(pool, tenant, &rg, Some(user), &format!("ssrf-key-{n}")).await
}

/// `(http_status_code, proxy_subtype, error_type)` of the tenant's refusal
/// row; the writer is asynchronous, so poll.
async fn refusal_row(pool: &PgPool, tenant: &str) -> (Option<i32>, Option<String>, Option<String>) {
    for _ in 0..120 {
        if let Some(r) = sqlx::query_as(
            "SELECT http_status_code, proxy_subtype, error_type FROM proxy_logs \
              WHERE tenant_id = $1 AND endpoint = 'preflight_outbound_denied'",
        )
        .bind(tenant)
        .fetch_optional(pool)
        .await
        .expect("proxy_logs query")
        {
            return r;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    panic!("{tenant}: the outbound-policy refusal wrote no audit row");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_llm_model_pointed_at_the_metadata_service_is_refused(pool: PgPool) {
    for (n, tenant, url) in [
        (1, "t-ssrf-v4", "http://169.254.169.254/latest/meta-data"),
        (2, "t-ssrf-v6", "http://[fd00:ec2::254]/v1"),
        (3, "t-ssrf-mapped", "http://[::ffff:169.254.169.254]/v1"),
    ] {
        let api_key = tenant_with_model(&pool, n, tenant, url).await;
        let app = common::build_llm_app(common::build_test_state(pool.clone()));
        let started = std::time::Instant::now();
        let resp = app
            .oneshot(common::build_post_request(
                "/v1/proxy/llm/chat/completions",
                json!({"model_id": format!("m-ssrf-{n}"), "messages": [{"role": "user", "content": "hi"}]}),
                Some(&api_key),
                Some(tenant),
            ))
            .await
            .unwrap();
        let status = resp.status();
        let body = body_json(resp.into_body()).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{url}: body={body}");
        assert!(
            body.to_string().contains("outbound policy"),
            "{url}: the refusal names the policy: {body}"
        );
        // Refused before any connect attempt: an attempted send to a
        // black-holed address would wait out the connect timeout.
        assert!(
            started.elapsed() < std::time::Duration::from_secs(5),
            "{url}: refused without trying to connect"
        );
        // Like every refusal, it is audited (llm_refusal::refuse).
        let (code, subtype, error_type) = refusal_row(&pool, tenant).await;
        assert_eq!(code, Some(403), "{url}");
        assert_eq!(subtype.as_deref(), Some("access_denied"), "{url}");
        assert_eq!(error_type.as_deref(), Some("outbound_denied"), "{url}");
    }
}

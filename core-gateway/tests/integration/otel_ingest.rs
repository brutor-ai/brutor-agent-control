// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The OpenTelemetry GenAI inlet, end to end.
//!
//! Reported evidence must land in the same ledger as witnessed evidence
//! and stay distinguishable at every step: the row (`source`,
//! `trace_continuity`), the rollup (`chain_integrity = observed_external`,
//! `source = otel | mixed`), and the run facts a Tier-A check reads.

use std::time::Duration;

use axum::{
    Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
};
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;

use brutor_gateway_core::middleware;
use brutor_gateway_core::routes;
use brutor_gateway_core::services::background_writer::BackgroundWriter;
use brutor_gateway_core::services::otel_ingest::{
    self, CONTINUITY_EXTERNAL, IngestContext, ROOT_KIND_EXTERNAL_TELEMETRY, SOURCE_OTEL,
};
use brutor_gateway_core::services::run_ledger::{chain, finalize_run, finalized_by, source};

use crate::background_writer_chain::test_auth;
use crate::common;

/// (source, trace_continuity, trace_root_kind, delegation_depth) per step row.
type StepRow = (String, Option<String>, Option<String>, Option<i32>);

const TRACE: &str = "cafe0000cafe0000cafe0000cafe0000";

async fn seed_ai_system(pool: &PgPool, id: &str, tenant: &str) {
    sqlx::query(
        "INSERT INTO resource_groups (id, tenant_id, parent_group_id, hierarchy_level, \
         inherit_from_parent, name, is_active, is_system, group_type, system_kind) \
         VALUES ($1, $2, NULL, 0, true, $1, true, false, \
                 CAST('AI_SYSTEM' AS public.grouptype), 'agent') \
         ON CONFLICT (id) DO NOTHING",
    )
    .bind(id)
    .bind(tenant)
    .execute(pool)
    .await
    .expect("seed ai system");
}

/// One finished trace: HTTP server span → chat → tool, plus a second chat.
fn export(trace: &str) -> Value {
    json!({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claims-agent"}}]},
        "scopeSpans": [{"scope": {"name": "test"}, "spans": [
            {"traceId": trace, "spanId": "0000000000000001", "name": "POST /chat",
             "startTimeUnixNano": "1725000000000000000", "endTimeUnixNano": "1725000003000000000"},
            {"traceId": trace, "spanId": "0000000000000002", "parentSpanId": "0000000000000001",
             "name": "chat gpt-4o", "startTimeUnixNano": "1725000000100000000", "endTimeUnixNano": "1725000001100000000",
             "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "20"}}]},
            {"traceId": trace, "spanId": "0000000000000003", "parentSpanId": "0000000000000002",
             "name": "execute_tool lookup", "startTimeUnixNano": "1725000000200000000", "endTimeUnixNano": "1725000000400000000",
             "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": "lookup_policy"}}]},
            {"traceId": trace, "spanId": "0000000000000004", "parentSpanId": "0000000000000001",
             "name": "chat gpt-4o", "startTimeUnixNano": "1725000001200000000", "endTimeUnixNano": "1725000002900000000",
             "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "300"}},
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "25"}}]}
        ]}]
    }]})
}

async fn wait_for_steps(pool: &PgPool, tenant: &str, root: &str, expected: i64) {
    for _ in 0..80 {
        let (n,): (i64,) = sqlx::query_as(
            "SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1 AND root_task_id = $2",
        )
        .bind(tenant)
        .bind(root)
        .fetch_one(pool)
        .await
        .unwrap();
        if n >= expected {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("ingested steps never landed for {root}");
}

async fn wait_for_run(pool: &PgPool, tenant: &str, root: &str) -> (String, String, String) {
    for _ in 0..80 {
        if let Ok(row) = sqlx::query_as::<_, (String, String, String)>(
            "SELECT terminal_state, chain_integrity, source FROM ai_system_runs \
              WHERE tenant_id = $1 AND root_task_id = $2",
        )
        .bind(tenant)
        .bind(root)
        .fetch_one(pool)
        .await
        {
            return row;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("run {root} was never finalized by the writer");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn ingested_spans_become_a_reported_run(pool: PgPool) {
    let t = "t-otel-run";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, "sys-otel", t).await;

    let parsed = otel_ingest::parse_export(&export(TRACE)).unwrap();
    let mut auth = test_auth(t);
    auth.api_key_group_id = Some("sys-otel".into());
    let ctx = IngestContext {
        auth: &auth,
        ai_system_id: Some("sys-otel"),
        correlation_id: "corr-otel",
    };

    let writer = BackgroundWriter::new();
    for span in &parsed.spans {
        writer.write_proxy_log(pool.clone(), otel_ingest::span_to_log(span, &parsed, &ctx));
    }
    let root = otel_ingest::run_id(TRACE);
    wait_for_steps(&pool, t, &root, 3).await;

    // Every row says where it came from.
    let rows: Vec<StepRow> = sqlx::query_as(
        "SELECT source, trace_continuity, trace_root_kind, delegation_depth FROM proxy_logs \
          WHERE tenant_id = $1 AND root_task_id = $2 ORDER BY timestamp",
    )
    .bind(t)
    .bind(&root)
    .fetch_all(&pool)
    .await
    .unwrap();
    assert_eq!(rows.len(), 3);
    assert!(rows.iter().all(|r| r.0 == SOURCE_OTEL));
    assert!(
        rows.iter()
            .all(|r| r.1.as_deref() == Some(CONTINUITY_EXTERNAL))
    );
    assert_eq!(rows[0].2.as_deref(), Some(ROOT_KIND_EXTERNAL_TELEMETRY));
    assert_eq!(rows[1].3, Some(1), "the tool step sits under the chat step");

    // The root step carried the end marker, so the writer closed the run
    // itself — no sweeper, no explicit call.
    let (state, integrity, src) = wait_for_run(&pool, t, &root).await;
    assert_eq!(state, "completed");
    assert_eq!(integrity, chain::OBSERVED_EXTERNAL);
    assert_eq!(src, source::OTEL);

    let (sys, llm, tools, tokens): (Option<String>, i32, i32, Option<i32>) = sqlx::query_as(
        "SELECT ai_system_id, llm_call_count, tool_call_count, total_tokens FROM ai_system_runs \
          WHERE tenant_id = $1 AND root_task_id = $2",
    )
    .bind(t)
    .bind(&root)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(
        sys.as_deref(),
        Some("sys-otel"),
        "attributed through the ordinary resolver"
    );
    assert_eq!((llm, tools), (2, 1));
    assert_eq!(tokens, Some(445));

    // Re-finalizing keeps the provenance; the facts a check reads carry it.
    let run = finalize_run(&pool, t, &root, finalized_by::EXPLICIT, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.chain_integrity, chain::OBSERVED_EXTERNAL);
    assert_eq!(run.source, source::OTEL);
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_run_with_proxied_and_ingested_steps_is_mixed(pool: PgPool) {
    let t = "t-otel-mixed";
    common::seed_tenant(&pool, t).await;

    // One proxied step, written the way the gateway writes it (source defaults
    // to gateway, continuity minted), timestamped just before the exporter's
    // spans so the run's duration stays a sane i32 of milliseconds.
    sqlx::query(
        "INSERT INTO proxy_logs (id, tenant_id, timestamp, proxy_type, end_user_session_id, \
         http_method, endpoint, success, root_task_id, delegation_depth, trace_continuity) \
         VALUES ('mx-1', $1, to_timestamp(1724999970), 'llm', 'sess', 'POST', '/v1/chat', \
                 true, $2, 0, 'minted')",
    )
    .bind(t)
    .bind(otel_ingest::run_id(TRACE))
    .execute(&pool)
    .await
    .unwrap();

    let parsed = otel_ingest::parse_export(&export(TRACE)).unwrap();
    let auth = test_auth(t);
    let ctx = IngestContext {
        auth: &auth,
        ai_system_id: None,
        correlation_id: "corr-mixed",
    };
    let writer = BackgroundWriter::new();
    // Only the tool step, so the gateway's chat and the exporter's tool share a run.
    writer.write_proxy_log(
        pool.clone(),
        otel_ingest::span_to_log(&parsed.spans[1], &parsed, &ctx),
    );
    let root = otel_ingest::run_id(TRACE);
    wait_for_steps(&pool, t, &root, 2).await;

    let run = finalize_run(&pool, t, &root, finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.source, source::MIXED);
    assert_eq!(
        run.chain_integrity,
        chain::OBSERVED_EXTERNAL,
        "one reported step is enough to withhold the signed claim"
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_purely_proxied_run_stays_gateway_sourced(pool: PgPool) {
    let t = "t-otel-none";
    common::seed_tenant(&pool, t).await;
    sqlx::query(
        "INSERT INTO proxy_logs (id, tenant_id, timestamp, proxy_type, end_user_session_id, \
         http_method, endpoint, success, root_task_id, delegation_depth, trace_continuity) \
         VALUES ('gw-1', $1, NOW() - interval '30 seconds', 'llm', 'sess', 'POST', '/v1/chat', \
                 true, 'r-gw', 0, 'minted')",
    )
    .bind(t)
    .execute(&pool)
    .await
    .unwrap();
    let run = finalize_run(&pool, t, "r-gw", finalized_by::IDLE, None, None)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(run.source, source::GATEWAY);
    assert_eq!(run.chain_integrity, chain::INTACT);
}

// ---------------------------------------------------------------------------
// HTTP surface
// ---------------------------------------------------------------------------

fn app(pool: PgPool) -> Router {
    let state = common::build_test_state(pool);
    Router::new()
        .nest("/v1/ingest/otel", routes::otel_ingest::router())
        .layer(middleware::tenant::TenantLayer::new(state.clone()))
        .with_state(state)
}

fn post(tenant: &str, key: Option<&str>, content_type: &str, body: Vec<u8>) -> Request<Body> {
    let mut b = Request::builder()
        .method("POST")
        .uri("/v1/ingest/otel/v1/traces")
        .header("X-Tenant-ID", tenant)
        .header("content-type", content_type);
    if let Some(k) = key {
        b = b.header("authorization", format!("Bearer {k}"));
    }
    let mut req = b.body(Body::from(body)).unwrap();
    let peer: std::net::SocketAddr = "127.0.0.1:55555".parse().unwrap();
    req.extensions_mut()
        .insert(axum::extract::ConnectInfo(peer));
    req
}

async fn body_json(resp: axum::response::Response) -> Value {
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn http_inlet_accepts_an_export_with_an_api_key(pool: PgPool) {
    let t = "t-otel-http";
    common::seed_tenant(&pool, t).await;
    seed_ai_system(&pool, "sys-http", t).await;
    let key = common::seed_api_key(&pool, t, "sys-http", None, "collector").await;

    let trace = "d00d0000d00d0000d00d0000d00d0000";
    let resp = app(pool.clone())
        .oneshot(post(
            t,
            Some(&key),
            "application/json",
            export(trace).to_string().into_bytes(),
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body = body_json(resp).await;
    assert_eq!(body["accepted"], 3);
    assert_eq!(body["skipped"], 1);
    assert_eq!(body["partialSuccess"]["rejectedSpans"], 0);
    assert_eq!(body["runs"], json!([otel_ingest::run_id(trace)]));
    assert_eq!(
        body["aiSystemId"], "sys-http",
        "attributed from the key's own group"
    );

    let root = otel_ingest::run_id(trace);
    wait_for_steps(&pool, t, &root, 3).await;
    let (state, integrity, src) = wait_for_run(&pool, t, &root).await;
    assert_eq!(
        (state.as_str(), integrity.as_str(), src.as_str()),
        ("completed", chain::OBSERVED_EXTERNAL, source::OTEL)
    );
}

/// The protobuf refusal is the ONLY check that precedes
/// authentication, and body shape is no longer among them.
///
/// This test previously asserted that a malformed or non-export body came back
/// `400` without a credential, which was true and was the defect: reaching
/// that answer meant parsing up to 8 MB and walking up to 5 000 spans for an
/// unauthenticated caller who supplied nothing but a tenant header. Work done
/// on behalf of an anonymous caller IS the attack, so the order was inverted
/// and an unauthenticated request now stops at `401` whatever its body.
///
/// Reading one request header stays free, so the protobuf message — the one an
/// exporter is most likely to need — still arrives before auth.
#[sqlx::test(migrations = "./tests/migrations")]
async fn http_inlet_refuses_protobuf_before_auth_but_never_parses_for_a_stranger(pool: PgPool) {
    let t = "t-otel-http-bad";
    common::seed_tenant(&pool, t).await;

    // Header-only check: precise, and still ahead of authentication.
    let resp = app(pool.clone())
        .oneshot(post(t, None, "application/x-protobuf", vec![0x0a, 0x00]))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNSUPPORTED_MEDIA_TYPE);
    let body = body_json(resp).await;
    assert!(body.to_string().contains("http/json"), "{body}");

    // A body-shaped complaint would prove we parsed it. We must not have.
    let resp = app(pool.clone())
        .oneshot(post(
            t,
            None,
            "application/json",
            b"{\"spans\": []}".to_vec(),
        ))
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "an unauthenticated caller must be refused before the body is parsed",
    );

    let resp = app(pool.clone())
        .oneshot(post(t, None, "application/json", b"not json".to_vec()))
        .await
        .unwrap();
    assert_eq!(
        resp.status(),
        StatusCode::UNAUTHORIZED,
        "not even 'is this JSON' is answered for a caller we have not authenticated",
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn http_inlet_requires_a_credential(pool: PgPool) {
    let t = "t-otel-http-auth";
    common::seed_tenant(&pool, t).await;
    let resp = app(pool.clone())
        .oneshot(post(
            t,
            None,
            "application/json",
            export(TRACE).to_string().into_bytes(),
        ))
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    let (n,): (i64,) = sqlx::query_as("SELECT COUNT(*) FROM proxy_logs WHERE tenant_id = $1")
        .bind(t)
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(n, 0, "nothing is written for an unauthenticated export");
}

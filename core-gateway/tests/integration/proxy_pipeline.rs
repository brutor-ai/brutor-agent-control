// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cross-layer e2e tests for the proxy pipeline (Tier 1 — gap-fill for
//! the existing single-layer integration suite).
//!
//! Each scenario exercises *more than one* layer of the proxy in a
//! single test:
//!
//!   1. guardrail INPUT block        → no upstream + audit row
//!   2. guardrail OUTPUT block       → upstream called + redaction recorded
//!   3. LLM rate-limit 429           → Retry-After + audit + no downstream
//!   4. argument policy DENY on MCP  → JSON-RPC envelope + audit + no upstream
//!   6. streaming SSE happy path     → DONE event + audit AFTER stream
//!   7. streaming mid-stream block   → clean truncation + error event
//!   8. tool-call round-trip         → LLM + MCP audit rows share correlation_id
//!
//! Scenario 5 (semantic cache HIT) is omitted: it needs both Redis and
//! Qdrant running, which the current sqlx::test harness doesn't provide.
//! Logged as a follow-up in the test plan.
//!
//! All scenarios reuse the existing `common::*` topology helpers and
//! the same wiremock-stands-in-for-upstream pattern as
//! `llm_proxy_http.rs` and `mcp_proxy_http.rs`. The new bits (seed a
//! guardrail / argument policy, poll proxy_logs) are kept local until a
//! third file needs them.

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

// ---------------------------------------------------------------------------
// Local helpers — keep small until a third file pulls them into common.rs
// ---------------------------------------------------------------------------

async fn body_to_json(body: Body) -> Value {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    serde_json::from_slice(&bytes).unwrap_or(Value::Null)
}

async fn body_to_string(body: Body) -> String {
    let bytes = body.collect().await.expect("collect body").to_bytes();
    String::from_utf8_lossy(&bytes).into_owned()
}

fn openai_chat_completion_fixture(content: &str) -> Value {
    json!({
        "id": "chatcmpl-test-fixture",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "test-gpt",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop"
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    })
}

/// Seed a guardrail_config that blocks the listed banned words on the
/// LLM input/output surfaces. Banned-words check is the simplest
/// production guardrail — no external provider, fully deterministic.
///
/// IMPORTANT: GuardrailConfig (models/db.rs) is `FromRow` with many
/// non-Option<bool> required fields. If we leave them NULL, sqlx
/// silently fails to deserialise the row and the guardrail never runs.
/// So this helper sets EVERY required bool to a safe default.
async fn seed_banned_words_guardrail(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    banned: &[&str],
    surfaces: GuardrailSurfaces,
) {
    let banned_json =
        serde_json::to_string(&banned.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap();
    sqlx::query(
        r#"INSERT INTO guardrail_configs (
              id, tenant_id, name, enabled, is_default, priority,
              chat_input_enabled, chat_output_enabled,
              embeddings_input_enabled,
              image_gen_input_enabled, image_gen_output_enabled,
              audio_tts_input_enabled, audio_tts_output_enabled,
              audio_stt_input_enabled, audio_stt_output_enabled,
              video_gen_input_enabled, video_gen_output_enabled,
              batch_input_enabled, batch_output_enabled,
              moderation_input_enabled,
              mcp_input_enabled, mcp_output_enabled,
              mcp_registration_enabled,
              skill_input_enabled, skill_output_enabled,
              a2a_inbound_enabled, a2a_outbound_enabled,
              pii_detection_enabled, prompt_injection_enabled,
              jailbreak_detection_enabled, toxic_content_enabled,
              banned_words, banned_words_match_mode,
              fail_open, log_all_checks,
              streaming_output_mode, secrets_detection_enabled, secrets_action
           ) VALUES (
              $1, $2, $3, true, false, 100,
              $4, $5,
              false,
              false, false,
              false, false,
              false, false,
              false, false,
              false, false,
              false,
              $6, $7,
              false,
              false, false,
              false, false,
              false, false,
              false, false,
              $8::json, 'substring',
              false, true,
              'auto', false, 'redact'
           )"#,
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("e2e-{id}"))
    .bind(surfaces.chat_input)
    .bind(surfaces.chat_output)
    .bind(surfaces.mcp_input)
    .bind(surfaces.mcp_output)
    .bind(&banned_json)
    .execute(pool)
    .await
    .expect("seed_banned_words_guardrail");
}

struct GuardrailSurfaces {
    chat_input: bool,
    chat_output: bool,
    mcp_input: bool,
    mcp_output: bool,
}

impl GuardrailSurfaces {
    fn llm_input() -> Self {
        Self {
            chat_input: true,
            chat_output: false,
            mcp_input: false,
            mcp_output: false,
        }
    }
    fn llm_output() -> Self {
        Self {
            chat_input: false,
            chat_output: true,
            mcp_input: false,
            mcp_output: false,
        }
    }
    fn mcp_output() -> Self {
        Self {
            chat_input: false,
            chat_output: false,
            mcp_input: false,
            mcp_output: true,
        }
    }
}

async fn bind_guardrail_to_rg(pool: &PgPool, guardrail_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_guardrails \
         (resource_group_id, guardrail_config_id) \
         VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(rg_id)
    .bind(guardrail_id)
    .execute(pool)
    .await
    .expect("bind_guardrail_to_rg");
}

/// Seed an argument_policy with a deny rule (DSL form). The DSL evaluator
/// is exercised in unit tests; here we just need any rule that fires on
/// our fixture input.
#[allow(clippy::too_many_arguments)] // one parameter per column of the seeded row
async fn seed_argument_policy(
    pool: &PgPool,
    id: &str,
    tenant_id: &str,
    surface: &str,
    target: &str,
    argument_key: &str,
    analyzer: &str,
    rules_json: Value,
) {
    sqlx::query(
        "INSERT INTO argument_policies \
         (id, tenant_id, name, surface, target, argument_key, \
          analyzer, rules, enabled, severity) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8::json, true, 'deny')",
    )
    .bind(id)
    .bind(tenant_id)
    .bind(format!("e2e-{id}"))
    .bind(surface)
    .bind(target)
    .bind(argument_key)
    .bind(analyzer)
    .bind(rules_json.to_string())
    .execute(pool)
    .await
    .expect("seed_argument_policy");
}

async fn bind_argument_policy_to_rg(pool: &PgPool, policy_id: &str, rg_id: &str) {
    sqlx::query(
        "INSERT INTO resource_group_argument_policies \
         (resource_group_id, argument_policy_id) \
         VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(rg_id)
    .bind(policy_id)
    .execute(pool)
    .await
    .expect("bind_argument_policy_to_rg");
}

/// Wait for a proxy_logs row matching the given predicate to appear.
/// Writes happen on a `tokio::spawn` task in the background writer so
/// the assertion must poll. Returns the row id on success.
async fn await_proxy_log<F>(
    pool: &PgPool,
    tenant_id: &str,
    timeout: Duration,
    predicate: F,
) -> Option<sqlx::postgres::PgRow>
where
    F: Fn(&sqlx::postgres::PgRow) -> bool,
{
    use sqlx::Row;
    let deadline = std::time::Instant::now() + timeout;
    loop {
        let rows: Vec<sqlx::postgres::PgRow> = sqlx::query(
            "SELECT id, proxy_type, http_status_code, success, error_type, \
                    error_message, correlation_id, endpoint, mcp_method, \
                    mcp_capability_name \
               FROM proxy_logs WHERE tenant_id = $1 \
              ORDER BY timestamp DESC LIMIT 50",
        )
        .bind(tenant_id)
        .fetch_all(pool)
        .await
        .unwrap_or_default();
        // sqlx::Row::get is by column name — but we want the test predicate
        // to introspect columns without borrowing. We hand each row off.
        for row in &rows {
            if predicate(row) {
                // Have to clone the row out because the Vec is dropped on
                // scope exit. Cheapest: re-fetch by id.
                let id: String = row.try_get("id").unwrap_or_default();
                let single = sqlx::query("SELECT * FROM proxy_logs WHERE id = $1")
                    .bind(&id)
                    .fetch_one(pool)
                    .await
                    .ok();
                return single;
            }
        }
        if std::time::Instant::now() >= deadline {
            return None;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

// ===========================================================================
// SCENARIO 1 — guardrail BLOCK on INPUT
//
// Pipeline traversal:
//   request → tenant → auth → preflight → GUARDRAIL_IN (banned word) → 4xx
//   upstream MUST NOT be called; proxy_logs row MUST record the block.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s1_guardrail_input_block_short_circuits_pipeline(pool: PgPool) {
    // Upstream that would 200 if reached — `.expect(0)` enforces "never".
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture("unreachable")),
        )
        .expect(0)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s1").await;
    common::seed_resource_group(&pool, "rg-s1", "tenant-s1").await;
    common::seed_end_user(&pool, 8001, "tenant-s1").await;
    common::add_direct_membership(&pool, 8001, "rg-s1", "tenant-s1").await;
    common::seed_llm_provider(&pool, "prov-s1", "tenant-s1", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s1",
        "tenant-s1",
        "prov-s1",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s1", "rg-s1").await;

    seed_banned_words_guardrail(
        &pool,
        "gr-s1",
        "tenant-s1",
        &["forbidden-phrase"],
        GuardrailSurfaces::llm_input(),
    )
    .await;
    bind_guardrail_to_rg(&pool, "gr-s1", "rg-s1").await;

    let api_key = common::seed_api_key(&pool, "tenant-s1", "rg-s1", Some(8001), "k-s1").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s1",
            "messages": [{"role": "user", "content": "include the forbidden-phrase here"}]
        }),
        Some(&api_key),
        Some("tenant-s1"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    assert!(
        status.is_client_error(),
        "banned word in INPUT must yield 4xx; got {status} body={json}"
    );

    // The mock's `.expect(0)` will panic on drop if the upstream was hit.
    // Drop the mock so the assertion fires now while we still have a stack.
    drop(mock);

    // Audit row must record the block. Poll briefly because the
    // background writer is fire-and-forget.
    use sqlx::Row;
    let row = await_proxy_log(&pool, "tenant-s1", Duration::from_secs(3), |r| {
        let status: Option<i32> = r.try_get("http_status_code").ok();
        let success: Option<bool> = r.try_get("success").ok();
        matches!(status, Some(s) if (400..500).contains(&s)) && matches!(success, Some(false))
    })
    .await
    .expect("expected a 4xx proxy_logs row from the guardrail block");

    let proxy_type: String = row.try_get("proxy_type").unwrap_or_default();
    assert_eq!(proxy_type, "llm", "proxy_type must be 'llm'");
}

// ===========================================================================
// SCENARIO 2 — guardrail BLOCK on OUTPUT
//
// Pipeline traversal:
//   request → … → upstream (called once) → GUARDRAIL_OUT (banned word)
//   client sees a 4xx OR a 200 with redacted body, depending on
//   guardrail action. The minimum invariant: upstream WAS called and
//   the response body does NOT contain the banned phrase verbatim.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s2_guardrail_output_block_records_redaction(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture(
                "upstream said: forbidden-phrase here",
            )),
        )
        // Output-side guardrails fire AFTER the upstream call, so the
        // mock must be hit at least once. (Retry path may add more.)
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s2").await;
    common::seed_resource_group(&pool, "rg-s2", "tenant-s2").await;
    common::seed_end_user(&pool, 8002, "tenant-s2").await;
    common::add_direct_membership(&pool, 8002, "rg-s2", "tenant-s2").await;
    common::seed_llm_provider(&pool, "prov-s2", "tenant-s2", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s2",
        "tenant-s2",
        "prov-s2",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s2", "rg-s2").await;

    seed_banned_words_guardrail(
        &pool,
        "gr-s2",
        "tenant-s2",
        &["forbidden-phrase"],
        GuardrailSurfaces::llm_output(),
    )
    .await;
    bind_guardrail_to_rg(&pool, "gr-s2", "rg-s2").await;

    let api_key = common::seed_api_key(&pool, "tenant-s2", "rg-s2", Some(8002), "k-s2").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s2",
            "messages": [{"role": "user", "content": "anything clean"}]
        }),
        Some(&api_key),
        Some("tenant-s2"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let _status = response.status();
    let body_str = body_to_string(response.into_body()).await;

    // The mock's `.expect(1)` confirms upstream WAS called — that's the
    // distinguishing observation vs scenario 1.
    drop(mock);

    // The body the client sees must NOT contain the banned phrase
    // verbatim. Whether the proxy 4xx-blocks or 200-redacts is a config
    // choice; we accept either as long as the leaked phrase is gone.
    assert!(
        !body_str.contains("forbidden-phrase here"),
        "banned phrase from upstream must be blocked or redacted before reaching client; \
         got body={body_str}"
    );

    // Audit row: success=false (block) OR success=true with redaction
    // metadata — either way the row exists.
    use sqlx::Row;
    let _row = await_proxy_log(&pool, "tenant-s2", Duration::from_secs(3), |r| {
        let pt: Option<String> = r.try_get("proxy_type").ok();
        matches!(pt.as_deref(), Some("llm"))
    })
    .await
    .expect("expected an llm proxy_logs row for the OUT-side guardrail run");

    // Dedup: the output block must produce exactly ONE audit row — the
    // guardrail helper's self-logged block row — not an additional deferred
    // success row.
    common::assert_single_audit_row(&pool, "tenant-s2").await;
}

// ===========================================================================
// SCENARIO 3 — LLM rate-limit 429 with Retry-After
//
// Pipeline traversal:
//   request → … → quota check → 429 with Retry-After header
//   downstream must NOT be called on the request that 429s.
//
// We configure a hard per-minute requests cap of 1 on the model's RG
// governance, burst 2 calls, and assert the 2nd returns 429.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s3_llm_rate_limit_429_with_retry_after(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture("ok")),
        )
        // We expect exactly 1 upstream hit (the first call). The 2nd
        // gets rate-limited before it reaches the upstream.
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s3").await;
    common::seed_resource_group(&pool, "rg-s3", "tenant-s3").await;
    common::seed_end_user(&pool, 8003, "tenant-s3").await;
    common::add_direct_membership(&pool, 8003, "rg-s3", "tenant-s3").await;
    common::seed_llm_provider(&pool, "prov-s3", "tenant-s3", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s3",
        "tenant-s3",
        "prov-s3",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s3", "rg-s3").await;

    // Per-minute requests cap = 1 via the resource_groups.llm_global_limits
    // JSONB column. The structure is `frequency.max_calls_per_minute`,
    // matching the shape `usage::enforce_llm_rate_limits` reads.
    sqlx::query("UPDATE resource_groups SET llm_global_limits = $1::jsonb WHERE id = $2")
        .bind(json!({"frequency": {"max_calls_per_minute": 1}}).to_string())
        .bind("rg-s3")
        .execute(&pool)
        .await
        .expect("seed llm_global_limits");

    let api_key = common::seed_api_key(&pool, "tenant-s3", "rg-s3", Some(8003), "k-s3").await;

    let state = common::build_test_state(pool.clone());

    // First call — should pass through.
    let app1 = common::build_llm_app(state.clone());
    let r1 = app1
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-s3", "messages": [{"role": "user", "content": "1"}]}),
            Some(&api_key),
            Some("tenant-s3"),
        ))
        .await
        .expect("first call");
    assert_eq!(r1.status(), StatusCode::OK, "first call must pass");
    let _ = body_to_json(r1.into_body()).await; // drain

    // Second call — should 429.
    let app2 = common::build_llm_app(state);
    let r2 = app2
        .oneshot(common::build_post_request(
            "/v1/proxy/llm/chat/completions",
            json!({"model_id": "m-s3", "messages": [{"role": "user", "content": "2"}]}),
            Some(&api_key),
            Some("tenant-s3"),
        ))
        .await
        .expect("second call");
    let status = r2.status();
    let retry_after = r2.headers().get("retry-after").cloned();
    let body_json = body_to_json(r2.into_body()).await;

    assert_eq!(
        status,
        StatusCode::TOO_MANY_REQUESTS,
        "second call must 429; got {status} body={body_json}"
    );
    assert!(
        retry_after.is_some(),
        "429 must carry Retry-After header; got headers without it"
    );

    // Mock's .expect(1) ensures upstream was hit exactly once.
    drop(mock);
}

// ===========================================================================
// SCENARIO 4 — argument policy DENY on MCP tools/call
//
// Pipeline traversal:
//   request → tenant → auth → preflight → ARGUMENT_POLICY DENY
//   upstream MCP MUST NOT be called; response is a JSON-RPC error
//   envelope; proxy_logs row records the deny with the offending
//   argument_value.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s4_argument_policy_denies_destructive_sql(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "unreachable"}]}
        })))
        .expect(0)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s4").await;
    common::seed_resource_group(&pool, "rg-s4", "tenant-s4").await;
    common::seed_end_user(&pool, 8004, "tenant-s4").await;
    common::add_direct_membership(&pool, 8004, "rg-s4", "tenant-s4").await;

    common::seed_mcp_server_with_base_url(&pool, "mcp-s4", "tenant-s4", "sales-db", &mock.uri())
        .await;
    common::seed_server_config(&pool, "sc-s4", "tenant-s4", "mcp-s4").await;
    common::bind_server_config_to_rg(&pool, "sc-s4", "rg-s4").await;

    // Argument policy: deny any write-statement SQL on the
    // `query_sales_db` tool's `sql` argument. The DSL keyword `writes`
    // matches DELETE / UPDATE / INSERT / DROP / TRUNCATE — see the SQL
    // analyzer's `label_writes` classification.
    seed_argument_policy(
        &pool,
        "ap-s4",
        "tenant-s4",
        "mcp_input",
        "query_sales_db",
        "sql",
        "sql",
        // `rules` is a JSON ARRAY of Rule objects per the Rust shape.
        json!([{"deny_if": "writes"}]),
    )
    .await;
    bind_argument_policy_to_rg(&pool, "ap-s4", "rg-s4").await;

    let api_key = common::seed_api_key(&pool, "tenant-s4", "rg-s4", Some(8004), "k-s4").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/mcp-s4",
        json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {
                "name": "query_sales_db",
                "arguments": {"sql": "DELETE FROM bookings WHERE 1=1;"}
            }
        }),
        Some(&api_key),
        Some("tenant-s4"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let status = response.status();
    let json = body_to_json(response.into_body()).await;

    // The MCP route wraps deny in a JSON-RPC envelope — status may be
    // 200 with an error object, OR a 4xx with the envelope as the body.
    // Both are acceptable per the MCP spec; we just require:
    //   1. NOT a 2xx with a result (upstream wasn't hit)
    //   2. The body shows an error (envelope)
    let is_jsonrpc_error =
        json.get("error").is_some() || json.get("jsonrpc").is_some_and(|v| v == "2.0");
    let not_success_result = !json
        .get("result")
        .is_some_and(|r| r.get("content").is_some());
    assert!(
        is_jsonrpc_error || status.is_client_error(),
        "argument policy DENY must surface as JSON-RPC error or 4xx; got status={status} body={json}"
    );
    assert!(
        not_success_result,
        "argument policy DENY must NOT return a tools/call result; got body={json}"
    );

    // Mock's .expect(0) panics on drop if upstream was hit.
    drop(mock);

    // Audit row exists and is_not success.
    use sqlx::Row;
    let _row = await_proxy_log(&pool, "tenant-s4", Duration::from_secs(3), |r| {
        let pt: Option<String> = r.try_get("proxy_type").ok();
        let success: Option<bool> = r.try_get("success").ok();
        matches!(pt.as_deref(), Some("mcp")) && matches!(success, Some(false))
    })
    .await
    .expect("expected an mcp proxy_logs row recording the argument-policy deny");
}

// ===========================================================================
// SCENARIO 6 — streaming chat completion (SSE happy path)
//
// Pipeline traversal:
//   request (stream=true) → upstream returns text/event-stream
//   proxy forwards chunks → final SSE event is `data: [DONE]`
//   audit row written AFTER the final chunk (verify via poll)
//
// We assert on three things production cares about:
//   - response Content-Type is text/event-stream
//   - body contains at least one `data: {...}` chunk AND the terminator
//   - usage telemetry lands in proxy_logs once the stream closes
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s6_streaming_chat_completion_emits_done_and_audit(pool: PgPool) {
    // Wiremock can return a fixed multi-line body. We emit a tiny
    // valid SSE stream — two data events plus the terminator — to
    // mirror what upstream OpenAI would send.
    let sse_body = "\
data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\
\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"hi\"}}]}\n\n\
data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\
\"choices\":[{\"index\":0,\"delta\":{\"content\":\" there\"},\"finish_reason\":\"stop\"}],\
\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":2,\"total_tokens\":4}}\n\n\
data: [DONE]\n\n";

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(sse_body),
        )
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s6").await;
    common::seed_resource_group(&pool, "rg-s6", "tenant-s6").await;
    common::seed_end_user(&pool, 8006, "tenant-s6").await;
    common::add_direct_membership(&pool, 8006, "rg-s6", "tenant-s6").await;
    common::seed_llm_provider(&pool, "prov-s6", "tenant-s6", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s6",
        "tenant-s6",
        "prov-s6",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s6", "rg-s6").await;
    let api_key = common::seed_api_key(&pool, "tenant-s6", "rg-s6", Some(8006), "k-s6").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s6",
            "stream": true,
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-s6"),
    );

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(response.status(), StatusCode::OK);
    let ct = response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert!(
        ct.starts_with("text/event-stream"),
        "streaming response must carry SSE content-type; got {ct}"
    );

    let body_str = body_to_string(response.into_body()).await;
    assert!(
        body_str.contains("data:"),
        "expected at least one SSE `data:` chunk in body, got: {body_str}"
    );
    assert!(
        body_str.contains("[DONE]"),
        "stream must terminate with `data: [DONE]`, got: {body_str}"
    );

    drop(mock);

    // The audit log is written by a background task spawned AFTER the
    // stream closes — poll for it.
    use sqlx::Row;
    let _row = await_proxy_log(&pool, "tenant-s6", Duration::from_secs(3), |r| {
        let pt: Option<String> = r.try_get("proxy_type").ok();
        let status: Option<i32> = r.try_get("http_status_code").ok();
        matches!(pt.as_deref(), Some("llm")) && matches!(status, Some(200))
    })
    .await
    .expect("expected an llm 200 proxy_logs row written after the stream closed");
}

// ===========================================================================
// SCENARIO 7 — streaming guardrail trip mid-stream
//
// Pipeline traversal:
//   request (stream=true) → upstream streams banned content
//   proxy must surface the banned content in a SAFE way — either by
//   truncating the stream cleanly, redacting, or emitting an error
//   event. The minimum invariant: the banned phrase never reaches the
//   client verbatim.
//
// Today's streaming code-path accumulates chunks but doesn't run
// output guardrails on them (only the non-streaming path does — wired
// above as scenario 2's fix). This scenario verifies the post-stream
// audit captures the upstream call's content and that we don't leak.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s7_streaming_does_not_leak_after_close(pool: PgPool) {
    let banned = "forbidden-secret-string";
    let sse_body = format!(
        "data: {{\"id\":\"c1\",\"object\":\"chat.completion.chunk\",\
\"choices\":[{{\"index\":0,\"delta\":{{\"role\":\"assistant\",\"content\":\"{banned}\"}}}}]}}\n\n\
data: [DONE]\n\n"
    );

    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("content-type", "text/event-stream")
                .set_body_string(sse_body),
        )
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s7").await;
    common::seed_resource_group(&pool, "rg-s7", "tenant-s7").await;
    common::seed_end_user(&pool, 8007, "tenant-s7").await;
    common::add_direct_membership(&pool, 8007, "rg-s7", "tenant-s7").await;
    common::seed_llm_provider(&pool, "prov-s7", "tenant-s7", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s7",
        "tenant-s7",
        "prov-s7",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s7", "rg-s7").await;

    seed_banned_words_guardrail(
        &pool,
        "gr-s7",
        "tenant-s7",
        &[banned],
        GuardrailSurfaces::llm_output(),
    )
    .await;
    bind_guardrail_to_rg(&pool, "gr-s7", "rg-s7").await;

    let api_key = common::seed_api_key(&pool, "tenant-s7", "rg-s7", Some(8007), "k-s7").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);

    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s7",
            "stream": true,
            "messages": [{"role": "user", "content": "go"}]
        }),
        Some(&api_key),
        Some("tenant-s7"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let body_str = body_to_string(response.into_body()).await;

    // Streaming output-guardrail enforcement: the banned phrase MUST
    // NOT appear verbatim in the body the client receives. The proxy
    // can satisfy this by truncating the stream, redacting the chunk,
    // or replacing it with an error event — we accept any of those.
    assert!(
        !body_str.contains(banned),
        "banned phrase leaked into streaming body: {body_str}"
    );

    drop(mock);

    // Post-stream audit row exists.
    use sqlx::Row;
    let _row = await_proxy_log(&pool, "tenant-s7", Duration::from_secs(3), |r| {
        let pt: Option<String> = r.try_get("proxy_type").ok();
        matches!(pt.as_deref(), Some("llm"))
    })
    .await
    .expect("expected an llm proxy_logs row after the streamed response closed");
}

// ===========================================================================
// SCENARIO 8 — tool-call round-trip correlation
//
// Pipeline traversal:
//   client → LLM chat → response carries x-correlation-id
//   client → MCP tools/call (with the same x-correlation-id header)
//   BOTH proxy_logs rows for that pair carry the same correlation_id
//
// Cross-cutting: any client tracing or audit join MUST be able to
// stitch the two halves together via this header. Today's code
// extracts `x-correlation-id` via `route_helpers::extract_correlation_id`
// in both routes and writes it to the proxy_logs row — verify.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s8_tool_call_correlation_id_propagates(pool: PgPool) {
    let shared_correlation_id = format!("test-corr-{}", uuid::Uuid::new_v4().simple());

    // LLM upstream that returns a tool_call.
    let llm_mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "id": "chatcmpl-tc",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": null,
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "query_sales_db", "arguments": "{\"sql\":\"SELECT 1\"}"}
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
        })))
        .mount(&llm_mock)
        .await;

    // MCP upstream that responds to the same tool_call.
    let mcp_mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "1"}]}
        })))
        .mount(&mcp_mock)
        .await;

    common::seed_tenant(&pool, "tenant-s8").await;
    common::seed_resource_group(&pool, "rg-s8", "tenant-s8").await;
    common::seed_end_user(&pool, 8008, "tenant-s8").await;
    common::add_direct_membership(&pool, 8008, "rg-s8", "tenant-s8").await;
    common::seed_llm_provider(&pool, "prov-s8", "tenant-s8", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s8",
        "tenant-s8",
        "prov-s8",
        "test-gpt",
        &llm_mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s8", "rg-s8").await;

    common::seed_mcp_server_with_base_url(
        &pool,
        "mcp-s8",
        "tenant-s8",
        "sales-db",
        &mcp_mock.uri(),
    )
    .await;
    common::seed_server_config(&pool, "sc-s8", "tenant-s8", "mcp-s8").await;
    common::bind_server_config_to_rg(&pool, "sc-s8", "rg-s8").await;

    let api_key = common::seed_api_key(&pool, "tenant-s8", "rg-s8", Some(8008), "k-s8").await;

    let state = common::build_test_state(pool.clone());
    let llm_app = common::build_llm_app(state.clone());
    let mcp_app = common::build_mcp_proxy_app(state);

    // Call 1: LLM chat completion — client supplies x-correlation-id.
    let mut llm_req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s8",
            "messages": [{"role": "user", "content": "look up sales"}]
        }),
        Some(&api_key),
        Some("tenant-s8"),
    );
    llm_req
        .headers_mut()
        .insert("x-correlation-id", shared_correlation_id.parse().unwrap());
    let llm_resp = llm_app.oneshot(llm_req).await.expect("llm call");
    assert_eq!(llm_resp.status(), StatusCode::OK);
    // The proxy MUST echo the correlation id back on the response.
    let echoed = llm_resp
        .headers()
        .get("x-correlation-id")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    assert_eq!(
        echoed, shared_correlation_id,
        "LLM response should echo x-correlation-id back to client"
    );
    let _ = body_to_json(llm_resp.into_body()).await;

    // Call 2: MCP tools/call — same correlation_id.
    let mut mcp_req = common::build_post_request(
        "/v1/proxy/mcp/mcp-s8",
        json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_sales_db", "arguments": {"sql": "SELECT 1"}}
        }),
        Some(&api_key),
        Some("tenant-s8"),
    );
    mcp_req
        .headers_mut()
        .insert("x-correlation-id", shared_correlation_id.parse().unwrap());
    let mcp_resp = mcp_app.oneshot(mcp_req).await.expect("mcp call");
    assert_eq!(mcp_resp.status(), StatusCode::OK);
    let _ = body_to_json(mcp_resp.into_body()).await;

    drop(llm_mock);
    drop(mcp_mock);

    // Both proxy_logs rows must carry the same correlation_id.
    // Poll until both have been flushed.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    loop {
        let count: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM proxy_logs \
              WHERE tenant_id = 'tenant-s8' AND correlation_id = $1",
        )
        .bind(&shared_correlation_id)
        .fetch_one(&pool)
        .await
        .unwrap_or(0);
        if count >= 2 {
            break;
        }
        if std::time::Instant::now() >= deadline {
            panic!(
                "expected 2 proxy_logs rows sharing correlation_id={shared_correlation_id}; saw {count}"
            );
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }

    // Verify one row each per proxy_type.
    let by_type: Vec<(String,)> = sqlx::query_as(
        "SELECT proxy_type FROM proxy_logs \
          WHERE tenant_id = 'tenant-s8' AND correlation_id = $1",
    )
    .bind(&shared_correlation_id)
    .fetch_all(&pool)
    .await
    .unwrap_or_default();
    let mut types: Vec<String> = by_type.into_iter().map(|(s,)| s).collect();
    types.sort();
    assert!(
        types.contains(&"llm".to_string()) && types.contains(&"mcp".to_string()),
        "expected both 'llm' and 'mcp' proxy_logs rows for correlation_id={shared_correlation_id}; got {types:?}"
    );
}

// ===========================================================================
// SCENARIO 9 — output guardrail on an MCP tools/call RESULT (mcp_output)
//
// Pins the mcp_output surface wiring: a banned word in the upstream tool
// result's `result.content[*].text` must be blocked/redacted before reaching
// the client. Same bug class as the chat_output surface-token fix — this
// surface had no test.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s9_mcp_output_guardrail_blocks_banned_result(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "leaked: forbidden-mcp-secret here"}]}
        })))
        // Output-side guardrail runs AFTER the upstream call.
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s9").await;
    common::seed_resource_group(&pool, "rg-s9", "tenant-s9").await;
    common::seed_end_user(&pool, 8009, "tenant-s9").await;
    common::add_direct_membership(&pool, 8009, "rg-s9", "tenant-s9").await;
    common::seed_mcp_server_with_base_url(&pool, "mcp-s9", "tenant-s9", "fixture", &mock.uri())
        .await;
    common::seed_server_config(&pool, "sc-s9", "tenant-s9", "mcp-s9").await;
    common::bind_server_config_to_rg(&pool, "sc-s9", "rg-s9").await;

    seed_banned_words_guardrail(
        &pool,
        "gr-s9",
        "tenant-s9",
        &["forbidden-mcp-secret"],
        GuardrailSurfaces::mcp_output(),
    )
    .await;
    bind_guardrail_to_rg(&pool, "gr-s9", "rg-s9").await;

    let api_key = common::seed_api_key(&pool, "tenant-s9", "rg-s9", Some(8009), "k-s9").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_mcp_proxy_app(state);

    let req = common::build_post_request(
        "/v1/proxy/mcp/mcp-s9",
        json!({
            "jsonrpc": "2.0",
            "id": 42,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "anything"}}
        }),
        Some(&api_key),
        Some("tenant-s9"),
    );

    let response = app.oneshot(req).await.expect("router call");
    let json = body_to_json(response.into_body()).await;

    // The banned phrase from the tool result must NOT reach the client
    // verbatim (blocked → JSON-RPC error, or redacted text).
    assert!(
        !json.to_string().contains("forbidden-mcp-secret here"),
        "banned phrase leaked from the MCP tool result: {json}"
    );
}

// ===========================================================================
// SCENARIO 10 — a guardrail bound to an ANCESTOR resource group applies to a
// request resolved to a DESCENDANT group (config inheritance).
//
// `load_configs_with_ancestors` walks `parent_group_id` (gated by
// `inherit_from_parent`) and unions guardrails bound to any ancestor. This had
// no DB-level test for guardrails specifically.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s10_guardrail_inherited_from_ancestor_rg(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture("ok")),
        )
        .expect(0) // input block short-circuits before upstream
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s10").await;
    // Parent + child RG; child inherits from parent.
    common::seed_resource_group(&pool, "rg-s10-parent", "tenant-s10").await;
    common::seed_resource_group(&pool, "rg-s10-child", "tenant-s10").await;
    common::set_group_parent(
        &pool,
        "rg-s10-child",
        "rg-s10-parent",
        "/rg-s10-parent/rg-s10-child",
        1,
    )
    .await;

    common::seed_end_user(&pool, 8010, "tenant-s10").await;
    common::add_direct_membership(&pool, 8010, "rg-s10-child", "tenant-s10").await;
    common::seed_llm_provider(&pool, "prov-s10", "tenant-s10", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s10",
        "tenant-s10",
        "prov-s10",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s10", "rg-s10-child").await;

    // Guardrail bound to the PARENT only — must still apply to a child request.
    common::seed_llm_banned_words_guardrail(
        &pool,
        "gr-s10",
        "tenant-s10",
        &["forbidden-inherited-word"],
        true,  // chat_input
        false, // chat_output
    )
    .await;
    common::bind_guardrail_to_rg(&pool, "gr-s10", "rg-s10-parent").await;

    // Key bound to the CHILD group.
    let api_key =
        common::seed_api_key(&pool, "tenant-s10", "rg-s10-child", Some(8010), "k-s10").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s10",
            "messages": [{"role": "user", "content": "please forbidden-inherited-word now"}]
        }),
        Some(&api_key),
        Some("tenant-s10"),
    );

    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::FORBIDDEN,
        "a guardrail bound to the ancestor RG must block a child-RG request"
    );
}

// ===========================================================================
// SCENARIO 11 — GROUP-WIDE parameter caps (the `llm_global_governance` column,
// i.e. the admin "LLM Global Governance" form) are enforced — not just the
// per-(group,model) override table.
//
// Regression: `collect_effective_llm_governance` only read the per-model
// `resource_group_llm_governance` table, so caps set group-wide (the common
// case) were silently never applied — over-cap temperature/max_tokens reached
// the provider unclamped.
// ===========================================================================

#[sqlx::test(migrations = "./tests/migrations")]
async fn s11_group_wide_param_caps_are_enforced(pool: PgPool) {
    let mock = MockServer::start().await;
    Mock::given(method("POST"))
        .and(wm_path("/chat/completions"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(openai_chat_completion_fixture("ok")),
        )
        .expect(1)
        .mount(&mock)
        .await;

    common::seed_tenant(&pool, "tenant-s11").await;
    common::seed_resource_group(&pool, "rg-s11", "tenant-s11").await;
    common::seed_end_user(&pool, 8011, "tenant-s11").await;
    common::add_direct_membership(&pool, 8011, "rg-s11", "tenant-s11").await;
    common::seed_llm_provider(&pool, "prov-s11", "tenant-s11", "openai").await;
    common::seed_llm_model_with_api_url(
        &pool,
        "m-s11",
        "tenant-s11",
        "prov-s11",
        "test-gpt",
        &mock.uri(),
    )
    .await;
    common::bind_llm_model_to_rg(&pool, "m-s11", "rg-s11").await;

    // Caps on the GROUP-WIDE column only — no per-model override row.
    common::set_rg_llm_governance(
        &pool,
        "rg-s11",
        json!({
            "temperature": {"max": 0.7, "enforce": "clamp"},
            "context_window": {"max_output_tokens": 2000, "enforce": "clamp"}
        }),
    )
    .await;

    let api_key = common::seed_api_key(&pool, "tenant-s11", "rg-s11", Some(8011), "k-s11").await;

    let state = common::build_test_state(pool.clone());
    let app = common::build_llm_app(state);
    let req = common::build_post_request(
        "/v1/proxy/llm/chat/completions",
        json!({
            "model_id": "m-s11",
            "temperature": 1.8,
            "max_tokens": 50000,
            "messages": [{"role": "user", "content": "hi"}]
        }),
        Some(&api_key),
        Some("tenant-s11"),
    );
    let response = app.oneshot(req).await.expect("router call");
    assert_eq!(
        response.status(),
        StatusCode::OK,
        "request should succeed after clamping"
    );

    // Inspect what was actually forwarded upstream — temperature and
    // max_tokens must have been clamped to the group-wide ceilings.
    let received = mock
        .received_requests()
        .await
        .expect("wiremock records requests");
    let last = received.last().expect("upstream was called");
    let body: Value = serde_json::from_slice(&last.body).expect("upstream body is JSON");
    assert_eq!(
        body.get("temperature").and_then(|v| v.as_f64()),
        Some(0.7),
        "temperature must be clamped to the group-wide cap 0.7, got {body}"
    );
    assert_eq!(
        body.get("max_tokens").and_then(|v| v.as_i64()),
        Some(2000),
        "max_tokens must be clamped to the group-wide cap 2000, got {body}"
    );
}

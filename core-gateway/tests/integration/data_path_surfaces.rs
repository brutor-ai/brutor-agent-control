// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Data paths through the real MCP route that had no end-to-end test: the Agent
//! Skills system server, and an upstream that answers MCP in SSE.

use axum::Router;
use axum::http::StatusCode;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use sqlx::PgPool;
use tower::ServiceExt;
use wiremock::matchers::{body_string_contains, method};
use wiremock::{Mock, MockServer, ResponseTemplate};

use brutor_gateway_core::constants::system_server::agent_skill as ask;

use crate::common;

const T: &str = "t-dpath";
const RG: &str = "rg-dpath";
const BANNED: &str = "forbidden-phrase";

async fn send(app: &Router, uri: &str, key: Option<&str>, body: Value) -> (StatusCode, String) {
    let req = common::build_post_request(uri, body, key, Some(T));
    let resp = app.clone().oneshot(req).await.unwrap();
    let s = resp.status();
    let b = resp.into_body().collect().await.unwrap().to_bytes();
    (s, String::from_utf8_lossy(&b).to_string())
}

fn rpc(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
}

async fn base(pool: &PgPool) -> String {
    common::seed_tenant(pool, T).await;
    common::seed_resource_group(pool, RG, T).await;
    common::seed_end_user(pool, 9701, T).await;
    common::add_direct_membership(pool, 9701, RG, T).await;
    common::seed_api_key(pool, T, RG, Some(9701), "k-dpath").await
}

// ── Agent Skills system server ─────────────────────────────────────────

const SKILL_URI: &str = "/v1/proxy/mcp/system-agent-skill-server-t-dpath";

async fn skills_world(pool: &PgPool) -> (Router, String) {
    let key = base(pool).await;
    common::seed_mcp_server_with_base_url(
        pool,
        "system-agent-skill-server-t-dpath",
        T,
        "skills",
        ask::BASE_URL,
    )
    .await;
    common::seed_server_config(pool, "sc-sk", T, "system-agent-skill-server-t-dpath").await;
    common::bind_server_config_to_rg(pool, "sc-sk", RG).await;
    common::seed_agent_skill(pool, "sk-mine", T, "mine", "# Mine\nDo {{x}}", "My skill").await;
    common::bind_skill_to_rg(pool, "sk-mine", RG).await;
    common::seed_resource_group(pool, "rg-other", T).await;
    common::seed_agent_skill(
        pool,
        "sk-theirs",
        T,
        "theirs",
        "# Theirs\nSECRET-BODY",
        "Other",
    )
    .await;
    common::bind_skill_to_rg(pool, "sk-theirs", "rg-other").await;
    (
        common::build_mcp_app(common::build_test_state(pool.clone())),
        key,
    )
}

fn call(name: &str, arguments: Value) -> Value {
    rpc("tools/call", json!({"name": name, "arguments": arguments}))
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_skill_server_handshake_and_list_are_scoped(pool: PgPool) {
    let (app, key) = skills_world(&pool).await;
    let (s, b) = send(&app, SKILL_URI, Some(&key), rpc("initialize", json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("protocolVersion"), "{b}");
    let (s, b) = send(&app, SKILL_URI, Some(&key), rpc("ping", json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    let (_, b) = send(
        &app,
        SKILL_URI,
        Some(&key),
        rpc("no/such/method", json!({})),
    )
    .await;
    assert!(b.contains("-32601") || b.contains("not found"), "{b}");

    let (s, b) = send(&app, SKILL_URI, Some(&key), rpc("tools/list", json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains(ask::TOOL_LIST), "{b}");
    assert!(!b.contains("theirs"), "another group's skill: {b}");

    let (s, b) = send(&app, SKILL_URI, Some(&key), call(ask::TOOL_LIST, json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("mine") && !b.contains("theirs"), "{b}");
}

/// Skill tools refuse a skill outside the caller's groups: its body, its
/// resources and its templates are the group's, not the tenant's.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_skill_of_another_group_is_not_readable_through_any_tool(pool: PgPool) {
    let (app, key) = skills_world(&pool).await;
    let cases = [
        call("skills__load", json!({"skill_name": "theirs"})),
        call(
            ask::TOOL_READ_RESOURCE,
            json!({"skill_name": "theirs", "path": "SKILL.md"}),
        ),
        call(
            ask::TOOL_RENDER_TEMPLATE,
            json!({"skill_name": "theirs", "template": "t.md", "vars": {}}),
        ),
        call(
            ask::TOOL_RUN_SCRIPT,
            json!({"skill_name": "theirs", "script": "x.py"}),
        ),
    ];
    for c in cases {
        let (_, b) = send(&app, SKILL_URI, Some(&key), c.clone()).await;
        assert!(!b.contains("SECRET-BODY"), "{c}: {b}");
        assert!(
            b.contains("error") || b.contains("isError") || b.contains("not found"),
            "{c}: {b}"
        );
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_tools_refuse_malformed_arguments(pool: PgPool) {
    let (app, key) = skills_world(&pool).await;
    for c in [
        rpc("tools/call", json!({})),
        call(ask::TOOL_READ_RESOURCE, json!({})),
        call(ask::TOOL_READ_RESOURCE, json!({"skill_name": "mine"})),
        call(ask::TOOL_RENDER_TEMPLATE, json!({"skill_name": "mine"})),
        call(ask::TOOL_RUN_SCRIPT, json!({})),
        call("skills__load", json!({"skill_name": "no-such-skill"})),
        call("skills__load", json!({})),
    ] {
        let (_, b) = send(&app, SKILL_URI, Some(&key), c.clone()).await;
        assert!(b.contains("error") || b.contains("isError"), "{c}: {b}");
    }
}

/// A path out of the skill's own files is refused, not read.
#[sqlx::test(migrations = "./tests/migrations")]
async fn skill_resource_paths_cannot_escape(pool: PgPool) {
    let (app, key) = skills_world(&pool).await;
    for path in ["../../etc/passwd", "/etc/passwd", "no-such-file.md"] {
        let (_, b) = send(
            &app,
            SKILL_URI,
            Some(&key),
            call(
                ask::TOOL_READ_RESOURCE,
                json!({"skill_name": "mine", "path": path}),
            ),
        )
        .await;
        assert!(!b.contains("root:"), "{path}: {b}");
        assert!(b.contains("error") || b.contains("isError"), "{path}: {b}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn loading_your_own_skill_returns_its_body(pool: PgPool) {
    let (app, key) = skills_world(&pool).await;
    let (s, b) = send(
        &app,
        SKILL_URI,
        Some(&key),
        call("skills__load", json!({"skill_name": "mine"})),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("Do {{x}}"), "{b}");
}

// ── An MCP upstream that answers in SSE ────────────────────────────────

const MCP_URI: &str = "/v1/proxy/mcp/ms-sse";

async fn sse_world(pool: &PgPool) -> (Router, String, MockServer) {
    let key = base(pool).await;
    let mock = MockServer::start().await;
    common::seed_mcp_server_with_base_url(pool, "ms-sse", T, "sse", &mock.uri()).await;
    common::seed_server_config(pool, "sc-sse", T, "ms-sse").await;
    common::bind_server_config_to_rg(pool, "sc-sse", RG).await;
    let app = common::build_mcp_app(common::build_test_state(pool.clone()));
    (app, key, mock)
}

async fn sse_answer(mock: &MockServer, rpc_method: &str, result: Value) {
    let event = json!({"jsonrpc": "2.0", "id": 1, "result": result});
    Mock::given(method("POST"))
        .and(body_string_contains(rpc_method))
        .respond_with(ResponseTemplate::new(200).set_body_raw(
            format!("event: message\ndata: {event}\n\n"),
            "text/event-stream",
        ))
        .mount(mock)
        .await;
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_sse_answer_is_relayed(pool: PgPool) {
    let (app, key, mock) = sse_world(&pool).await;
    sse_answer(
        &mock,
        "tools/call",
        json!({"content": [{"type": "text", "text": "tool output"}]}),
    )
    .await;
    let (s, b) = send(&app, MCP_URI, Some(&key), call("echo", json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("tool output"), "{b}");
}

/// A disabled capability is removed from a streamed list too.
#[sqlx::test(migrations = "./tests/migrations")]
async fn capability_filters_apply_to_a_streamed_tools_list(pool: PgPool) {
    let (app, key, mock) = sse_world(&pool).await;
    common::set_mcp_capability_filter(&pool, T, RG, "sc-sse", "tool", "secret-tool", "disabled")
        .await;
    common::set_mcp_capability_filter(&pool, T, RG, "sc-sse", "tool", "open-tool", "enabled").await;
    sse_answer(
        &mock,
        "tools/list",
        json!({"tools": [{"name": "open-tool"}, {"name": "secret-tool"}]}),
    )
    .await;
    let (s, b) = send(&app, MCP_URI, Some(&key), rpc("tools/list", json!({}))).await;
    assert_eq!(s, StatusCode::OK, "{b}");
    assert!(b.contains("open-tool"), "{b}");
    assert!(!b.contains("secret-tool"), "{b}");
}

/// The output guardrail sees a tool result that arrives as SSE.
#[sqlx::test(migrations = "./tests/migrations")]
async fn the_output_guardrail_covers_a_streamed_tool_result(pool: PgPool) {
    let (app, key, mock) = sse_world(&pool).await;
    common::seed_banned_words_guardrail_surfaces(&pool, "gr-sse", T, &[BANNED], &["mcp_output"])
        .await;
    common::bind_guardrail_to_rg(&pool, "gr-sse", RG).await;
    sse_answer(
        &mock,
        "tools/call",
        json!({"content": [{"type": "text", "text": format!("leaks {BANNED} here")}]}),
    )
    .await;
    let (_, b) = send(&app, MCP_URI, Some(&key), call("echo", json!({}))).await;
    assert!(!b.contains("leaks"), "{b}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn an_sse_answer_with_a_session_keeps_it_for_the_client(pool: PgPool) {
    let (app, key, mock) = sse_world(&pool).await;
    Mock::given(method("POST"))
        .respond_with(
            ResponseTemplate::new(200)
                .insert_header("mcp-session-id", "up-session")
                .set_body_raw(
                    "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":\"2025-03-26\",\"capabilities\":{}}}\n\n",
                    "text/event-stream",
                ),
        )
        .mount(&mock)
        .await;
    let req =
        common::build_post_request(MCP_URI, rpc("initialize", json!({})), Some(&key), Some(T));
    let resp = app.clone().oneshot(req).await.unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    assert!(
        resp.headers().contains_key("mcp-session-id"),
        "{:?}",
        resp.headers()
    );
}

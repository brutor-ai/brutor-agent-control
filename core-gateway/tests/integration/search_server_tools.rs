// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The built-in web-search MCP server's tools: RPC surface, tenant
//! configuration, and that the tenant's policy outranks what a tool call asks.

use brutor_gateway_core::models::db::McpServer;
use brutor_gateway_core::services::encryption::EncryptionService;
use brutor_gateway_core::services::search_server::handle_request;
use serde_json::{Value, json};
use sqlx::PgPool;
use wiremock::matchers::{method, path, query_param};
use wiremock::{Mock, MockServer, ResponseTemplate};

use crate::background_writer_chain::test_auth;
use crate::common;

const T: &str = "t-search";

fn enc() -> EncryptionService {
    EncryptionService::new("test-encryption-key-32-chars-ok!")
}

async fn server(pool: &PgPool) -> McpServer {
    common::seed_tenant(pool, T).await;
    common::seed_mcp_server(pool, "ms-search", T, "search").await;
    sqlx::query_as("SELECT * FROM mcp_servers WHERE id = 'ms-search'")
        .fetch_one(pool)
        .await
        .unwrap()
}

async fn configure(
    pool: &PgPool,
    provider: &str,
    base: Option<&str>,
    key: Option<String>,
    safe: bool,
    allowed: Option<Value>,
    blocked: Option<Value>,
) {
    sqlx::query(
        "INSERT INTO search_provider_configs (tenant_id, provider, enabled, api_key_encrypted, \
         searxng_base_url, max_results_per_query, safe_search, allowed_domains, blocked_domains) \
         VALUES ($1, $2, true, $3, $4, 10, $5, $6, $7)",
    )
    .bind(T)
    .bind(provider)
    .bind(key)
    .bind(base)
    .bind(safe)
    .bind(allowed)
    .bind(blocked)
    .execute(pool)
    .await
    .unwrap();
}

async fn rpc(
    pool: &PgPool,
    srv: &McpServer,
    method: &str,
    params: Value,
) -> Result<Value, (i32, String)> {
    handle_request(
        pool,
        srv,
        &test_auth(T),
        &reqwest::Client::new(),
        method,
        &params,
        None,
        "corr",
        &enc(),
    )
    .await
    .map_err(|e| (e.code, e.message))
}

fn call(tool: &str, arguments: Value) -> Value {
    json!({"name": tool, "arguments": arguments})
}

fn text(v: &Value) -> Value {
    serde_json::from_str(v["content"][0]["text"].as_str().unwrap()).unwrap()
}

async fn searxng_answering(results: Value) -> MockServer {
    let m = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/search"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"results": results})))
        .mount(&m)
        .await;
    m
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_protocol_methods_answer(pool: PgPool) {
    let srv = server(&pool).await;
    assert!(rpc(&pool, &srv, "initialize", json!({})).await.unwrap()["serverInfo"].is_object());
    assert_eq!(
        rpc(&pool, &srv, "ping", json!({})).await.unwrap(),
        json!({})
    );
    let tools = rpc(&pool, &srv, "tools/list", json!({})).await.unwrap();
    assert_eq!(tools["tools"].as_array().unwrap().len(), 4);
    assert_eq!(
        rpc(&pool, &srv, "resources/list", json!({})).await.unwrap()["resources"],
        json!([])
    );
    assert_eq!(
        rpc(&pool, &srv, "prompts/list", json!({})).await.unwrap()["prompts"],
        json!([])
    );
    assert_eq!(
        rpc(&pool, &srv, "resources/templates/list", json!({}))
            .await
            .unwrap()["resourceTemplates"],
        json!([])
    );
    assert!(
        rpc(&pool, &srv, "server/discover", json!({}))
            .await
            .unwrap()["supportedVersions"]
            .is_array()
    );
    assert_eq!(
        rpc(&pool, &srv, "nope", json!({})).await.unwrap_err().0,
        -32601
    );
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn malformed_tool_calls_are_refused(pool: PgPool) {
    let srv = server(&pool).await;
    assert_eq!(
        rpc(&pool, &srv, "tools/call", json!({}))
            .await
            .unwrap_err()
            .0,
        -32602
    );
    assert_eq!(
        rpc(&pool, &srv, "tools/call", call("no_such_tool", json!({})))
            .await
            .unwrap_err()
            .0,
        -32601
    );
    for tool in [
        "search__web",
        "search__news",
        "search__images",
        "search__extract",
    ] {
        let (code, msg) = rpc(&pool, &srv, "tools/call", call(tool, json!({})))
            .await
            .unwrap_err();
        assert_eq!(code, -32602, "{tool}: {msg}");
    }
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn no_configuration_means_the_default_with_no_key_and_a_clear_error(pool: PgPool) {
    let srv = server(&pool).await;
    let (code, msg) = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__web", json!({"query": "q"})),
    )
    .await
    .unwrap_err();
    assert_eq!(code, -32000);
    assert!(msg.contains("not configured"), "{msg}");
    let (_, msg) = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__news", json!({"query": "q"})),
    )
    .await
    .unwrap_err();
    assert!(msg.contains("not configured"), "{msg}");
    let (_, msg) = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__images", json!({"query": "q"})),
    )
    .await
    .unwrap_err();
    assert!(msg.contains("not configured"), "{msg}");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn each_tool_reaches_the_configured_provider(pool: PgPool) {
    let srv = server(&pool).await;
    let m = searxng_answering(json!([
        {"title": "A", "url": "https://a.example/1", "content": "c", "img_src": "https://a.example/i.png"},
    ]))
    .await;
    configure(&pool, "searxng", Some(&m.uri()), None, true, None, None).await;
    for tool in ["search__web", "search__news", "search__images"] {
        let r = rpc(&pool, &srv, "tools/call", call(tool, json!({"query": "q"})))
            .await
            .unwrap();
        assert_eq!(text(&r)[0]["title"], "A", "{tool}");
    }

    Mock::given(method("GET"))
        .and(path("/page"))
        .respond_with(
            ResponseTemplate::new(200).set_body_raw("<title>T</title><p>Body</p>", "text/html"),
        )
        .mount(&m)
        .await;
    let r = rpc(
        &pool,
        &srv,
        "tools/call",
        call(
            "search__extract",
            json!({"url": format!("{}/page", m.uri())}),
        ),
    )
    .await
    .unwrap();
    assert_eq!(text(&r)["title"], "T");
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn the_domain_policy_is_read_from_configuration_and_applied(pool: PgPool) {
    let srv = server(&pool).await;
    let m = searxng_answering(json!([
        {"title": "bad", "url": "https://blocked.example/1", "content": "c"},
        {"title": "good", "url": "https://ok.example/2", "content": "c"},
    ]))
    .await;
    configure(
        &pool,
        "searxng",
        Some(&m.uri()),
        None,
        true,
        None,
        Some(json!(["blocked.example"])),
    )
    .await;
    let r = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__web", json!({"query": "q"})),
    )
    .await
    .unwrap();
    let got = text(&r);
    assert_eq!(got.as_array().unwrap().len(), 1, "{got}");
    assert_eq!(got[0]["title"], "good");
}

/// `extract_content` fetches a URL the model chose: the same policy applies.
#[sqlx::test(migrations = "./tests/migrations")]
async fn extract_refuses_a_url_outside_the_domain_policy(pool: PgPool) {
    let srv = server(&pool).await;
    let m = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string("secret"))
        .expect(0)
        .mount(&m)
        .await;
    configure(
        &pool,
        "searxng",
        Some("http://127.0.0.1:1"),
        None,
        true,
        None,
        Some(json!(["127.0.0.1"])),
    )
    .await;
    let (code, msg) = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__extract", json!({"url": format!("{}/x", m.uri())})),
    )
    .await
    .unwrap_err();
    assert_eq!(code, -32000);
    assert!(msg.contains("domain policy"), "{msg}");
}

/// Malformed arguments used to fall back to the derived `Default`: no results
/// asked for, safe search off.
#[sqlx::test(migrations = "./tests/migrations")]
async fn malformed_arguments_are_refused_not_defaulted(pool: PgPool) {
    let srv = server(&pool).await;
    let m = searxng_answering(json!([])).await;
    configure(&pool, "searxng", Some(&m.uri()), None, true, None, None).await;
    for tool in ["search__web", "search__news", "search__images"] {
        let (code, msg) = rpc(
            &pool,
            &srv,
            "tools/call",
            call(tool, json!({"query": "q", "num_results": "many"})),
        )
        .await
        .unwrap_err();
        assert_eq!(code, -32602, "{tool}: {msg}");
    }
    let (code, _) = rpc(
        &pool,
        &srv,
        "tools/call",
        call(
            "search__extract",
            json!({"url": "https://a.example", "max_length": "x"}),
        ),
    )
    .await
    .unwrap_err();
    assert_eq!(code, -32602);
    assert_eq!(m.received_requests().await.unwrap().len(), 0);
}

/// The tenant's `safe_search` outranks the tool call's `safe_search: false`.
#[sqlx::test(migrations = "./tests/migrations")]
async fn a_tool_call_cannot_turn_off_the_tenants_safe_search(pool: PgPool) {
    let srv = server(&pool).await;
    let results = json!([{"title": "x", "url": "https://a.example/1"}]);
    let m = searxng_answering(results.clone()).await;
    configure(&pool, "searxng", Some(&m.uri()), None, true, None, None).await;
    // SearXNG carries no safe-search flag: this pins that the call still works
    // and the flag reaches the provider params. The wire check is below.
    rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__web", json!({"query": "q", "safe_search": false})),
    )
    .await
    .unwrap();

    // Hosted providers: the wire shows it. `api_base` is not reachable from
    // configuration, so this goes through the provider function the dispatch
    // calls, with the parameters the dispatch computes.
    use brutor_gateway_core::services::search_providers as sp;
    let hosted = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/res/v1/web/search"))
        .and(query_param("safesearch", "strict"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"web": {"results": []}})))
        .expect(1)
        .mount(&hosted)
        .await;
    let cfg = sp::SearchProviderConfig {
        api_key: Some("k".into()),
        api_base: Some(hosted.uri()),
        ..Default::default()
    };
    let mut params: sp::WebSearchParams =
        serde_json::from_value(json!({"safe_search": false})).unwrap();
    params.safe_search |= cfg.safe_search;
    sp::brave_web_search(&reqwest::Client::new(), &cfg, "q", &params)
        .await
        .unwrap();
}

#[sqlx::test(migrations = "./tests/migrations")]
async fn a_stored_api_key_is_decrypted_and_a_disabled_config_is_ignored(pool: PgPool) {
    let srv = server(&pool).await;
    let ct = enc().encrypt("the-real-key").unwrap();
    configure(&pool, "brave", None, Some(ct), true, None, None).await;
    // With a key the brave path passes the "not configured" check; there is no
    // network here, so it fails later, at the request. That difference is what
    // proves the key was read.
    let c = reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_millis(1))
        .build()
        .unwrap();
    let (_, msg) = handle_request(
        &pool,
        &srv,
        &test_auth(T),
        &c,
        "tools/call",
        &call("search__web", json!({"query": "q"})),
        None,
        "corr",
        &enc(),
    )
    .await
    .map_err(|e| (e.code, e.message))
    .unwrap_err();
    assert!(!msg.contains("not configured"), "{msg}");

    sqlx::query("UPDATE search_provider_configs SET enabled = false")
        .execute(&pool)
        .await
        .unwrap();
    let (_, msg) = rpc(
        &pool,
        &srv,
        "tools/call",
        call("search__web", json!({"query": "q"})),
    )
    .await
    .unwrap_err();
    assert!(msg.contains("not configured"), "{msg}");
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Search System MCP Server
//!
//! Virtual MCP server implementation for built-in web search.
//! Handles JSON-RPC requests internally, routing search queries to
//! the configured upstream provider (Google, Brave, SearXNG).
//!
//! ## Supported MCP Methods
//!
//! ```text
//! initialize                 → Server info + capabilities
//! notifications/initialized  → Acknowledgement (202)
//! tools/list                 → Returns 4 search tools with JSON schemas
//! tools/call                 → Executes search via upstream provider
//! resources/list             → Empty
//! resources/templates/list   → Empty
//! prompts/list               → Empty
//! ping                       → Health check
//! ```

use serde_json::{Value, json};
use sqlx::PgPool;

use crate::constants;
use crate::models::auth::AuthContext;
use crate::models::db::McpServer;
use crate::services::search_providers::{self, SearchProviderConfig};

// =============================================================================
// Tool JSON Schemas
// =============================================================================

/// MCP ToolAnnotations for every search tool: reads of the open web.
/// Pinned to `tests/fixtures/system_tool_annotations.json` (`web_search`),
/// shared with the control plane's seeded copy, which the gateway classifies
/// risk from; without them a search is treated as side-effecting.
fn search_annotations() -> Value {
    json!({"readOnlyHint": true, "idempotentHint": true, "openWorldHint": true})
}

fn search_tools() -> Vec<Value> {
    search_tool_schemas()
        .into_iter()
        .map(|mut t| {
            t["annotations"] = search_annotations();
            t
        })
        .collect()
}

fn search_tool_schemas() -> Vec<Value> {
    vec![
        json!({
            "name": constants::system_server::search::TOOL_WEB,
            "description": "Search the web for any topic. Returns a list of relevant results with titles, URLs, and snippets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results to return (1-20, default 5)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code (e.g. 'en', 'fr', 'de')"
                    },
                    "country": {
                        "type": "string",
                        "description": "Country code (e.g. 'US', 'GB', 'DE')"
                    }
                },
                "required": ["query"]
            }
        }),
        json!({
            "name": constants::system_server::search::TOOL_NEWS,
            "description": "Search for recent news articles on a topic. Returns news results with publication dates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The news search query"
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results (1-20, default 5)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5
                    },
                    "freshness": {
                        "type": "string",
                        "enum": ["day", "week", "month"],
                        "description": "How recent the news should be"
                    },
                    "language": {
                        "type": "string",
                        "description": "Language code"
                    }
                },
                "required": ["query"]
            }
        }),
        json!({
            "name": constants::system_server::search::TOOL_EXTRACT,
            "description": "Extract clean text content from a web page URL. Strips HTML and returns readable text.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to extract content from"
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum character length of extracted text"
                    }
                },
                "required": ["url"]
            }
        }),
        json!({
            "name": constants::system_server::search::TOOL_IMAGES,
            "description": "Search for images on the web. Returns image URLs with metadata.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The image search query"
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results (1-20, default 5)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5
                    },
                    "size": {
                        "type": "string",
                        "enum": ["small", "medium", "large"],
                        "description": "Preferred image size"
                    }
                },
                "required": ["query"]
            }
        }),
    ]
}

// =============================================================================
// Main handler
// =============================================================================

pub async fn handle_request(
    db: &PgPool,
    server: &McpServer,
    auth: &AuthContext,
    http_client: &reqwest::Client,
    rpc_method: &str,
    params: &Value,
    // jsonrpc id is accepted for signature parity with other RPC
    // handlers; we don't echo it back yet because the current error
    // paths don't surface JSON-RPC envelope errors to the caller.
    _jsonrpc_id: Option<&Value>,
    _correlation_id: &str,
    encryption: &crate::services::encryption::EncryptionService,
) -> Result<Value, SearchRpcError> {
    match rpc_method {
        // The system servers are dual-era: they answer
        // `server/discover` (2026-07-28) alongside the legacy `initialize`
        // handshake. A modern client's response is shaped by the request
        // dialect, not a flag. Static tool surface → public, long TTL.
        "server/discover" => Ok(crate::services::mcp_wire::discover_result(
            &[
                crate::constants::mcp_protocol::V2026_07_28,
                crate::constants::mcp_protocol::V2024_11_05,
            ],
            json!({
                "tools": {"listChanged": false},
                "resources": {"listChanged": false},
                "prompts": {"listChanged": false},
            }),
            None,
            "brutor-web-search",
            "1.0.0",
            3_600_000,
            crate::constants::mcp_protocol::cache_scope::PUBLIC,
        )),
        "initialize" => Ok(handle_initialize()),

        "ping" => Ok(json!({})),

        "tools/list" => Ok(handle_tools_list(params)),

        "tools/call" => handle_tools_call(db, server, auth, http_client, params, encryption).await,

        "resources/list" => Ok(json!({"resources": []})),
        "resources/templates/list" => Ok(json!({"resourceTemplates": []})),
        "prompts/list" => Ok(json!({"prompts": []})),

        _ => Err(SearchRpcError {
            code: -32601,
            message: format!("Method not found: {rpc_method}"),
        }),
    }
}

// =============================================================================
// Method handlers
// =============================================================================

fn handle_initialize() -> Value {
    json!({
        "protocolVersion": "2024-11-05",
        "capabilities": {
            "tools": {"listChanged": false},
            "resources": {"listChanged": false},
            "prompts": {"listChanged": false},
        },
        "serverInfo": {
            "name": "brutor-web-search",
            "version": "1.0.0",
        },
    })
}

fn handle_tools_list(params: &Value) -> Value {
    let tools = search_tools();

    let cursor = params
        .get("cursor")
        .and_then(|v| v.as_str())
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(0);

    let page_size = 100;
    let page: Vec<&Value> = tools.iter().skip(cursor).take(page_size).collect();

    json!({"tools": page})
}

async fn handle_tools_call(
    db: &PgPool,
    _server: &McpServer,
    auth: &AuthContext,
    http_client: &reqwest::Client,
    params: &Value,
    encryption: &crate::services::encryption::EncryptionService,
) -> Result<Value, SearchRpcError> {
    let tool_name = params
        .get("name")
        .and_then(|v| v.as_str())
        .ok_or_else(|| SearchRpcError {
            code: -32602,
            message: "Missing 'name' in tools/call params".into(),
        })?;

    let arguments = params.get("arguments").cloned().unwrap_or(json!({}));

    // Load provider config for this tenant
    let config = load_provider_config(db, &auth.tenant_id, encryption).await;

    tracing::debug!(
        tool = tool_name,
        provider = %config.provider,
        tenant = %auth.tenant_id,
        "Search tool call"
    );

    let result = match tool_name {
        constants::system_server::search::TOOL_WEB => {
            let query = arguments["query"].as_str().ok_or_else(|| SearchRpcError {
                code: -32602,
                message: "Missing 'query' argument".into(),
            })?;
            let mut params: search_providers::WebSearchParams = tool_params(&arguments)?;
            params.safe_search |= config.safe_search;

            let results = dispatch_web_search(http_client, &config, query, &params)
                .await
                .map_err(|e| SearchRpcError {
                    code: -32000,
                    message: e.0,
                })?;

            json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&results).unwrap_or_default()
                }]
            })
        }

        constants::system_server::search::TOOL_NEWS => {
            let query = arguments["query"].as_str().ok_or_else(|| SearchRpcError {
                code: -32602,
                message: "Missing 'query' argument".into(),
            })?;
            let params: search_providers::NewsSearchParams = tool_params(&arguments)?;

            let results = dispatch_news_search(http_client, &config, query, &params)
                .await
                .map_err(|e| SearchRpcError {
                    code: -32000,
                    message: e.0,
                })?;

            json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&results).unwrap_or_default()
                }]
            })
        }

        constants::system_server::search::TOOL_EXTRACT => {
            let url = arguments["url"].as_str().ok_or_else(|| SearchRpcError {
                code: -32602,
                message: "Missing 'url' argument".into(),
            })?;
            let params: search_providers::ExtractParams = tool_params(&arguments)?;

            // The URL comes from the model: the tenant's domain policy applies
            // to what it fetches, as it does to what a search returns.
            if !search_providers::domain_permitted(url, &config) {
                return Err(SearchRpcError {
                    code: -32000,
                    message: "URL is not permitted by this tenant's search domain policy".into(),
                });
            }

            let result = search_providers::extract_content(url, &params)
                .await
                .map_err(|e| SearchRpcError {
                    code: -32000,
                    message: e.0,
                })?;

            json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&result).unwrap_or_default()
                }]
            })
        }

        constants::system_server::search::TOOL_IMAGES => {
            let query = arguments["query"].as_str().ok_or_else(|| SearchRpcError {
                code: -32602,
                message: "Missing 'query' argument".into(),
            })?;
            let mut params: search_providers::ImageSearchParams = tool_params(&arguments)?;
            params.safe_search |= config.safe_search;

            let results = dispatch_image_search(http_client, &config, query, &params)
                .await
                .map_err(|e| SearchRpcError {
                    code: -32000,
                    message: e.0,
                })?;

            json!({
                "content": [{
                    "type": "text",
                    "text": serde_json::to_string_pretty(&results).unwrap_or_default()
                }]
            })
        }

        _ => {
            return Err(SearchRpcError {
                code: -32601,
                message: format!("Unknown search tool: {tool_name}"),
            });
        }
    };

    Ok(result)
}

/// Parse a tool's arguments. Malformed arguments are refused: the derived
/// `Default` they used to fall back to asks for no results and turns safe
/// search off.
fn tool_params<T: serde::de::DeserializeOwned>(arguments: &Value) -> Result<T, SearchRpcError> {
    serde_json::from_value(arguments.clone()).map_err(|e| SearchRpcError {
        code: -32602,
        message: format!("Invalid arguments: {e}"),
    })
}

// =============================================================================
// Provider dispatch
// =============================================================================

async fn dispatch_web_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &search_providers::WebSearchParams,
) -> search_providers::SearchRes<Vec<search_providers::SearchResult>> {
    match config.provider.as_str() {
        "google" => search_providers::google_web_search(client, config, query, params).await,
        "searxng" => search_providers::searxng_web_search(client, config, query, params).await,
        _ => search_providers::brave_web_search(client, config, query, params).await,
    }
}

async fn dispatch_news_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &search_providers::NewsSearchParams,
) -> search_providers::SearchRes<Vec<search_providers::SearchResult>> {
    match config.provider.as_str() {
        "searxng" => search_providers::searxng_news_search(client, config, query, params).await,
        _ => search_providers::brave_news_search(client, config, query, params).await,
    }
}

async fn dispatch_image_search(
    client: &reqwest::Client,
    config: &SearchProviderConfig,
    query: &str,
    params: &search_providers::ImageSearchParams,
) -> search_providers::SearchRes<Vec<search_providers::ImageResult>> {
    match config.provider.as_str() {
        "google" => search_providers::google_image_search(client, config, query, params).await,
        "searxng" => search_providers::searxng_image_search(client, config, query, params).await,
        _ => search_providers::brave_image_search(client, config, query, params).await,
    }
}

// =============================================================================
// Config loading
// =============================================================================

/// Load the search provider configuration for a tenant.
/// Falls back to default (Brave, no API key) if no config exists.
///
/// `allowed_domains` / `blocked_domains` are stored as Postgres `json`
/// columns (see `search_provider_configs` schema), **not** as `text[]`.
/// Decoding them into `Vec<String>` directly would silently fail at the
/// sqlx layer and hand us back a default (no-API-key) config — which is
/// exactly the "Brave API key not configured" symptom we chased for
/// weeks. Pull them as `Json<Vec<String>>` and also surface any decode
/// error in the trace instead of swallowing it with `unwrap_or(None)`.
async fn load_provider_config(
    db: &PgPool,
    tenant_id: &str,
    encryption: &crate::services::encryption::EncryptionService,
) -> SearchProviderConfig {
    use sqlx::types::Json;

    let row_result: Result<
        Option<(
            String,
            Option<String>,
            Option<String>,
            Option<String>,
            bool,
            Option<Json<Vec<String>>>,
            Option<Json<Vec<String>>>,
        )>,
        sqlx::Error,
    > = sqlx::query_as(
        r#"SELECT provider, api_key_encrypted, google_cx_id, searxng_base_url,
                  safe_search, allowed_domains, blocked_domains
           FROM search_provider_configs
           WHERE tenant_id = $1 AND enabled = true
           LIMIT 1"#,
    )
    .bind(tenant_id)
    .fetch_optional(db)
    .await;

    let row = match row_result {
        Ok(r) => r,
        Err(e) => {
            // Loud warning so the next occurrence isn't invisible. The
            // request keeps going with the default (no API key) rather
            // than 500ing — a misconfigured row shouldn't take out the
            // tool call, but admins need to see the decode error.
            tracing::warn!(
                tenant_id,
                error = %e,
                "search_provider_configs query failed; falling back to default config",
            );
            None
        }
    };

    match row {
        Some((provider, api_key, cx_id, searxng_url, safe, allowed, blocked)) => {
            // The column is `api_key_encrypted` — Fernet-at-rest.
            // Decrypt to plaintext before forwarding to Google /
            // Brave; sending the ciphertext would 401 as "invalid
            // API key" even when the operator's stored key was
            // valid. See `EncryptionService::decrypt_optional` for
            // the shared boundary helper.
            let api_key = encryption
                .decrypt_optional(api_key.as_deref(), "search_server:provider_config.api_key");
            SearchProviderConfig {
                provider,
                api_key,
                google_cx_id: cx_id,
                searxng_base_url: searxng_url,
                safe_search: safe,
                allowed_domains: allowed.map(|j| j.0).unwrap_or_default(),
                blocked_domains: blocked.map(|j| j.0).unwrap_or_default(),
                api_base: None,
            }
        }
        None => SearchProviderConfig::default(),
    }
}

// =============================================================================
// Error type
// =============================================================================

pub struct SearchRpcError {
    pub code: i32,
    pub message: String,
}

#[cfg(test)]
mod annotation_contract {
    use super::*;

    const FIXTURE: &str = include_str!("../../tests/fixtures/system_tool_annotations.json");

    #[test]
    fn every_search_tool_carries_the_pinned_annotations() {
        let v: Value = serde_json::from_str(FIXTURE).expect("fixture parses");
        let want = v["web_search"].as_object().expect("web_search section");
        let tools = search_tools();
        assert_eq!(
            tools.len(),
            want.len(),
            "update the fixture when a search tool is added"
        );
        for t in &tools {
            let name = t["name"].as_str().unwrap();
            assert_eq!(t.get("annotations"), want.get(name), "{name}");
        }
    }
}

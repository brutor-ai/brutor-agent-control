// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! OpenAPI specification and Swagger UI for the Brutor Agent Control Core Gateway.

use utoipa::openapi::security::{ApiKey, ApiKeyValue, HttpAuthScheme, HttpBuilder, SecurityScheme};
use utoipa::{Modify, OpenApi};

#[derive(OpenApi)]
#[openapi(
    info(
        title = "Brutor Agent Control Core Gateway",
        version = env!("CARGO_PKG_VERSION"),
        description = "The Core Gateway of Brutor Agent Control: LLM routing, MCP proxying, OAuth, and skill execution.",
        contact(name = "Brutor AI", url = "https://brutor.ai"),
    ),
    paths(
        // Gateway
        crate::routes::gateway::version,
        crate::routes::gateway::health,
        // Anthropic Messages API
        crate::routes::anthropic::messages,
        crate::routes::anthropic::count_tokens,
        // LLM
        crate::routes::llm::chat_completions,
        crate::routes::llm::responses,
        crate::routes::llm::completions,
        crate::routes::llm::embeddings,
        crate::routes::llm::list_models,
        crate::routes::llm::get_model,
        crate::routes::llm::reset_resilience,
        crate::routes::llm::invalidate_model_cache,
        crate::routes::llm::probe_catalog,
        crate::routes::llm::record_probe_result,
        // Gemini
        crate::routes::gemini::generate_content,
        // MCP
        crate::routes::mcp::proxy_with_path,
        crate::routes::mcp::proxy_root,
        // Virtual MCP
        crate::routes::vmcp::proxy,
        // OAuth
        crate::routes::oauth::oauth_protected_resource,
        crate::routes::oauth::openid_configuration,
        crate::routes::oauth::token_exchange,
        crate::routes::oauth::revoke_token,
        crate::routes::oauth::list_tokens,
        // Skills
        crate::routes::skills::execute_skill,
        crate::routes::skills::get_execution,
        crate::routes::skills::cancel_execution,
        crate::routes::skills::get_catalog,
        // A2A
        crate::routes::a2a::outbound,
        crate::routes::a2a::inbound,
        crate::routes::a2a::discover_card,
        crate::routes::a2a::discover_jwks,
        // Runs
        crate::routes::runs::end_run,
        crate::routes::runs::get_run,
        // OTel inlet
        crate::routes::otel_ingest::export_traces,
        // Media
        crate::routes::media::download,
    ),
    components(schemas(
        crate::models::proxy::ChatCompletionRequest,
        crate::models::proxy::ChatMessage,
        crate::models::proxy::ChatCompletionResponse,
        crate::models::proxy::ChatChoice,
        crate::models::proxy::UsageInfo,
        crate::models::proxy::CompletionRequest,
        crate::models::proxy::EmbeddingRequest,
        crate::models::proxy::EmbeddingResponse,
        crate::models::proxy::EmbeddingData,
        crate::models::proxy::ResponsesRequest,
        crate::models::proxy::SkillExecuteRequest,
        crate::models::proxy::SkillExecuteResponse,
        crate::models::proxy::GatewayInfo,
        crate::models::proxy::HealthResponse,
        crate::routes::a2a::A2AOutboundRequest,
        crate::routes::a2a::A2AInboundRequest,
        crate::routes::a2a::A2ACardDiscoveryResponse,
        crate::routes::otel_ingest::ExportResponse,
        crate::routes::otel_ingest::PartialSuccess,
        crate::routes::llm::RecordProbeResultBody,
        crate::routes::runs::EndRunRequest,
        crate::routes::runs::RunResponse,
    )),
    modifiers(&SecurityAddon),
    tags(
        (name = "Anthropic", description = "Anthropic Messages API — native endpoint for Claude Code and other Anthropic-native clients"),
        (name = "Gateway", description = "Health checks and service info"),
        (name = "LLM", description = "LLM proxy — chat completions, embeddings, responses API"),
        (name = "Gemini", description = "Gemini generateContent API — native endpoint for Gemini-native clients"),
        (name = "Runs", description = "Runs — end a run and read its rollup"),
        (name = "MCP", description = "MCP server proxy — JSON-RPC and SSE streaming"),
        (name = "Virtual MCP", description = "Virtual MCP — aggregates multiple MCP servers"),
        (name = "OAuth", description = "OAuth 2.0 / OIDC discovery and token exchange"),
        (name = "Skills", description = "Agent skill execution and catalog"),
        (name = "A2A", description = "Agent-to-Agent proxy — outbound invocation with signed delegation chains"),
        (name = "Media", description = "Signed download links of the filesystem media store"),
    )
)]
pub struct ApiDoc;

struct SecurityAddon;

impl Modify for SecurityAddon {
    fn modify(&self, openapi: &mut utoipa::openapi::OpenApi) {
        let components = openapi.components.get_or_insert_with(Default::default);
        components.add_security_scheme(
            "api_key",
            SecurityScheme::ApiKey(ApiKey::Header(ApiKeyValue::new("X-API-Key"))),
        );
        components.add_security_scheme(
            "bearer",
            SecurityScheme::Http(
                HttpBuilder::new()
                    .scheme(HttpAuthScheme::Bearer)
                    .bearer_format("JWT")
                    .build(),
            ),
        );
    }
}

/// Where the API document is served (JSON) and browsed (Swagger UI).
pub const OPENAPI_JSON_PATH: &str = "/api-docs/openapi.json";
pub const SWAGGER_UI_PATH: &str = "/docs";

/// Swagger UI at [`SWAGGER_UI_PATH`] plus the document at
/// [`OPENAPI_JSON_PATH`].
///
/// The document is handed over as JSON, not as a `utoipa::openapi::OpenApi`:
/// utoipa-swagger-ui 10 (the release free of RUSTSEC-2026-0367..0369 in its
/// build-time `zip`) is built on utoipa 6, whose `OpenApi` is a different
/// type from the utoipa 5 document the routes derive. Same URL, same
/// document — `tests/openapi_docs.rs` pins both.
/// The gateway's OpenAPI document as JSON: the derived `ApiDoc`. An
/// extension's fragments are merged into this by the builder.
pub fn document() -> anyhow::Result<serde_json::Value> {
    serde_json::to_value(ApiDoc::openapi())
        .map_err(|e| anyhow::anyhow!("OpenAPI document does not serialize: {e}"))
}

pub fn docs_router<S>(document: serde_json::Value) -> axum::Router<S>
where
    S: Clone + Send + Sync + 'static,
{
    utoipa_swagger_ui::SwaggerUi::new(SWAGGER_UI_PATH)
        .external_url_unchecked(OPENAPI_JSON_PATH, document)
        .into()
}

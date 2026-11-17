// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Provider-specific configuration: URL discovery, parameter mapping,
// header generation, and parameter dropping.
//
// Delegates provider-specific behavior to the LlmAdapter trait
// (see llm_adapter.rs). This module provides the orchestration
// pipeline (prepare_request, prepare_embedding, etc.) and URL discovery.

use serde_json::Value;

use crate::models::db::LlmModel;
use crate::services::llm_adapter::{self, StreamNormalizer};

// ---------------------------------------------------------------------------
// Adapter-delegating public API
// ---------------------------------------------------------------------------

/// Returns true if this provider needs stream normalization (non-OpenAI format).
pub fn needs_stream_normalization(provider: &str) -> bool {
    llm_adapter::get_adapter(provider).needs_stream_normalization()
}

/// Create a stream normalizer for the given provider.
pub fn create_stream_normalizer(provider: &str) -> Box<dyn StreamNormalizer> {
    llm_adapter::get_adapter(provider).create_stream_normalizer()
}

/// Normalize a non-streaming response body to OpenAI chat completion format.
pub fn normalize_response(provider: &str, body: &mut Value) {
    llm_adapter::get_adapter(provider).normalize_response(body);
}

/// Well-known provider base URLs for auto-discovery.
pub fn discover_base_url(provider: &str) -> Option<&'static str> {
    match provider {
        "openai" => Some("https://api.openai.com/v1"),
        "anthropic" => Some("https://api.anthropic.com/v1"),
        // Native Gemini REST surface. The OpenAI-compat shortcut
        // (`/v1beta/openai`) blocked everything Gemini-specific:
        // `cachedContents`, code execution, Google Search grounding,
        // safety settings, native multimodal `Part` objects. The
        // GeminiAdapter now translates OpenAI shape → native at the
        // wire edge (same pattern AnthropicAdapter uses), so going
        // native here unlocks all of those without changing what
        // clients of Brutor's own surface see.
        "gemini" | "google_ai_studio" => Some("https://generativelanguage.googleapis.com"),
        "mistral" => Some("https://api.mistral.ai/v1"),
        "cohere" => Some("https://api.cohere.com/v2"),
        "xai" => Some("https://api.x.ai/v1"),
        "deepseek" => Some("https://api.deepseek.com"),
        "moonshot" => Some("https://api.moonshot.ai/v1"),
        "minimax" => Some("https://api.minimax.io/v1"),
        "groq" => Some("https://api.groq.com/openai/v1"),
        "together_ai" => Some("https://api.together.xyz/v1"),
        "fireworks_ai" => Some("https://api.fireworks.ai/inference/v1"),
        "perplexity" => Some("https://api.perplexity.ai"),
        "voyage" => Some("https://api.voyageai.com/v1"),
        "jina_ai" => Some("https://api.jina.ai/v1"),
        "elevenlabs" => Some("https://api.elevenlabs.io/v1"),
        "deepgram" => Some("https://api.deepgram.com/v1"),
        "stability" => Some("https://api.stability.ai/v2beta"),
        "fal_ai" => Some("https://fal.run"),
        "sambanova" => Some("https://api.sambanova.ai/v1"),
        "cerebras" => Some("https://api.cerebras.ai/v1"),
        "ai21" => Some("https://api.ai21.com/studio/v1"),
        "deepinfra" => Some("https://api.deepinfra.com/v1/openai"),
        "huggingface" => Some("https://api-inference.huggingface.co/models"),
        "openrouter" => Some("https://openrouter.ai/api/v1"),
        _ => None,
    }
}

/// Build the upstream URL for a model + endpoint path.
///
/// `stream=true` requests the streaming URL variant — only honored by
/// adapters whose provider exposes distinct streaming endpoints
/// (Gemini's `:generateContent` vs `:streamGenerateContent`). Most
/// providers use the same URL for both modes and the flag is ignored.
pub fn build_url(model: &LlmModel, endpoint: &str, stream: bool) -> String {
    with_azure_api_version(base_url_and_endpoint(model, endpoint, stream), model)
}

/// Azure OpenAI versions its dated API with the `api-version` **query
/// parameter** ("in: query, required: yes" on the deployment-based endpoint;
/// <https://learn.microsoft.com/en-us/azure/ai-foundry/openai/reference>).
/// The gateway used to send the row's `api_version` as an HTTP header, which
/// Azure does not read. A row with no `api_version`
/// gets none: the v1 endpoint (`…/openai/v1/…`) needs none. Neither does a
/// row whose `api_url` already carries `api-version` in its query.
fn with_azure_api_version(url: String, model: &LlmModel) -> String {
    if !model.provider.starts_with("azure") {
        return url;
    }
    let Some(version) = model
        .api_version
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
    else {
        return url;
    };
    // The endpoint already states a version in its own query: that one is
    // sent, once.
    if let Some((_, query)) = url.split_once('?') {
        if url::form_urlencoded::parse(query.as_bytes()).any(|(k, _)| k == "api-version") {
            return url;
        }
    }
    let version: String = url::form_urlencoded::byte_serialize(version.as_bytes()).collect();
    let separator = if url.contains('?') { '&' } else { '?' };
    format!("{url}{separator}api-version={version}")
}

/// The model's name as its provider knows it: what goes on the wire as
/// `model`. The catalog seeds Azure rows under a prefix of its own
/// (`azure/gpt-5.2`, `azure_ai/DeepSeek-V3.2`) that is part of no Azure
/// name: on the v1 endpoint `model` is the deployment's name, which cannot
/// contain `/`. Every other provider's name is sent
/// as the row has it (the Gemini embedding prefix is `prepare_embedding`'s).
pub fn wire_model_name(model: &LlmModel) -> &str {
    let name = model.model_name.as_str();
    match model.provider.as_str() {
        "azure" => name.strip_prefix("azure/").unwrap_or(name),
        "azure_ai" => name.strip_prefix("azure_ai/").unwrap_or(name),
        _ => name,
    }
}

/// The provider API a request is for. The base URL of a provider differs
/// by API (OpenAI's chat base ends in `/v1`, its media base does not), and
/// so does the set of providers the gateway can reach on it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProviderApi {
    /// The generic forwarder (`build_url`): responses, text completions,
    /// embeddings, and every chat fallback or routing-group member.
    Llm,
    /// The chat completions route: as `Llm`, and the rows it bridges to a
    /// native backend (Claude on Bedrock or Vertex, Gemini on Vertex) get
    /// their URL from the row's region / project. So do the Bedrock rows
    /// served through Converse (`bedrock_runtime`).
    Chat,
    /// The chat forwarder (`llm_router::forward_chat_completion{,_streaming}`),
    /// where a chat fallback or a routing-group member is sent: as `Llm`,
    /// and the Bedrock rows served through Converse.
    ChatForward,
    /// The embeddings route: as `Llm`, and the Bedrock embedding rows served
    /// through InvokeModel.
    Embeddings,
    /// The Anthropic Messages route: as `Llm`, and a Bedrock or Vertex row's
    /// URL is built from its region / project by the native backend.
    AnthropicNative,
    /// The Gemini route: as `Llm`, and a Vertex row's URL is built from its
    /// region / project.
    GeminiNative,
    Images,
    Speech,
    Transcription,
    Video,
    /// The provider's batch API (Enterprise).
    Batch,
}

impl ProviderApi {
    /// The API a gateway route dispatches on, by the route's endpoint label
    /// (`enforce_actor_planes`'s `endpoint`). The label is the route's own
    /// constant, never request data.
    pub fn of_route(endpoint: &str) -> Self {
        if endpoint.starts_with("/v1/proxy/llm/images") {
            Self::Images
        } else if endpoint.starts_with("/v1/proxy/llm/audio/speech") {
            Self::Speech
        } else if endpoint.starts_with("/v1/proxy/llm/audio/transcriptions") {
            Self::Transcription
        } else if endpoint.starts_with("/v1/proxy/llm/video") {
            Self::Video
        } else if endpoint.starts_with("/v1beta/models/") {
            Self::GeminiNative
        } else if endpoint == "/v1/messages" {
            Self::AnthropicNative
        } else if endpoint == "/jobs" {
            Self::Batch
        } else if endpoint == "/v1/chat/completions" {
            Self::Chat
        } else if endpoint == "/v1/embeddings" {
            Self::Embeddings
        } else {
            Self::Llm
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Llm
            | Self::Chat
            | Self::ChatForward
            | Self::Embeddings
            | Self::AnthropicNative
            | Self::GeminiNative => "LLM",
            Self::Images => "image generation",
            Self::Speech => "speech",
            Self::Transcription => "transcription",
            Self::Video => "video generation",
            Self::Batch => "batch",
        }
    }
}

/// The one known base URL of `provider` for `api`; `None` when the gateway
/// knows none. A provider's own base only: no entry names another
/// provider's host.
fn known_base(provider: &str, api: ProviderApi) -> Option<&'static str> {
    use ProviderApi::*;
    match api {
        Llm | Chat | ChatForward | Embeddings | AnthropicNative | GeminiNative => {
            discover_base_url(provider)
        }
        // The media routes match a provider's name without regard to case
        // and with its spellings.
        Images => match provider.to_lowercase().as_str() {
            "openai" => Some("https://api.openai.com"),
            "fal" | "fal_ai" | "fal-ai" => Some("https://fal.run"),
            "replicate" => Some("https://api.replicate.com"),
            "stability" | "stability_ai" | "stability-ai" => Some("https://api.stability.ai"),
            // Not `vertex_ai`: the Gemini API is not Vertex's endpoint.
            "gemini" | "google" | "google_ai" => Some("https://generativelanguage.googleapis.com"),
            _ => None,
        },
        Speech => match provider.to_lowercase().as_str() {
            "openai" => Some("https://api.openai.com"),
            "elevenlabs" | "eleven_labs" | "eleven-labs" => Some("https://api.elevenlabs.io"),
            "google" | "google_cloud" | "google-cloud" => {
                Some("https://texttospeech.googleapis.com")
            }
            _ => None,
        },
        Transcription => match provider.to_lowercase().as_str() {
            "openai" => Some("https://api.openai.com"),
            "deepgram" => Some("https://api.deepgram.com"),
            "assemblyai" | "assembly_ai" | "assembly-ai" => Some("https://api.assemblyai.com"),
            "elevenlabs" | "eleven_labs" | "eleven-labs" => Some("https://api.elevenlabs.io"),
            "google" | "google_cloud" | "google-cloud" => Some("https://speech.googleapis.com"),
            _ => None,
        },
        Video => match provider.to_lowercase().as_str() {
            "openai" => Some("https://api.openai.com/v1"),
            "fal" | "fal_ai" => Some("https://queue.fal.run"),
            _ => None,
        },
        Batch => match provider {
            "openai" => Some("https://api.openai.com"),
            "anthropic" => Some("https://api.anthropic.com"),
            "xai" => Some("https://api.x.ai"),
            "mistral" => Some("https://api.mistral.ai"),
            "google_ai_studio" => Some("https://generativelanguage.googleapis.com"),
            _ => None,
        },
    }
}

/// The error type of a row the gateway has no destination for.
pub const PROVIDER_API_URL_MISSING: &str = "provider_api_url_missing";

/// The base URL a request for `provider` goes to: THE one place a base is
/// chosen. It is the row's own `api_url`, or the
/// one known base of the row's own provider for this API, and nothing
/// else: a row with neither has no destination and the answer is an error
/// naming the provider and the missing `api_url`. There is no default
/// provider. The fallback this replaces sent such a row's prompt and
/// credential to `api.openai.com`.
pub fn upstream_base_for(
    provider: &str,
    model_name: &str,
    api_url: Option<&str>,
    api: ProviderApi,
) -> Result<String, String> {
    if let Some(url) = api_url.map(str::trim).filter(|u| !u.is_empty()) {
        return Ok(url.to_string());
    }
    known_base(provider, api).map(str::to_string).ok_or_else(|| {
        format!(
            "{PROVIDER_API_URL_MISSING}: model '{model_name}' of provider '{provider}' has no              api_url, and the gateway knows no {} base URL for provider '{provider}'. Nothing              was sent. Set api_url on the model.",
            api.label()
        )
    })
}

/// [`upstream_base_for`] a model row.
pub fn upstream_base(model: &LlmModel, api: ProviderApi) -> Result<String, String> {
    upstream_base_for(
        &model.provider,
        &model.model_name,
        model.api_url.as_deref(),
        api,
    )
}

/// Why a model row is refused at dispatch, before anything is sent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DispatchRefusal {
    /// `unsupported_provider_api`, [`PROVIDER_API_URL_MISSING`],
    /// [`PROVIDER_AUTH_UNSUPPORTED`], or for a Bedrock or Vertex AI row
    /// `provider_credential_missing` / `provider_endpoint_invalid`.
    pub error_type: &'static str,
    /// The message, starting with the error type.
    pub reason: String,
}

/// True for the two names of Amazon Bedrock.
pub fn is_bedrock(provider: &str) -> bool {
    matches!(provider, "bedrock" | "bedrock_converse")
}

/// True for the two names of Google Vertex AI. `vertex` is `vertex_ai`.
pub fn is_vertex(provider: &str) -> bool {
    matches!(provider, "vertex_ai" | "vertex")
}

/// True for a provider that is a cloud: its requests are authenticated
/// with a cloud identity (SigV4, a Google access token) by the provider's
/// own backend, never with an API key.
pub fn is_cloud_provider(provider: &str) -> bool {
    is_bedrock(provider) || is_vertex(provider)
}

/// The backend of the gateway that serves a cloud row on an API.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CloudBackend {
    /// Claude on Bedrock: InvokeModel with the Anthropic body
    /// (`anthropic_adapter::backend::bedrock`).
    BedrockClaude,
    /// Converse, ConverseStream and the embeddings through InvokeModel
    /// (`bedrock_runtime`).
    BedrockRuntime,
    /// Claude and Gemini on Vertex AI (`anthropic_adapter::backend::vertex`,
    /// `routes::gemini`).
    Vertex,
}

/// The backend that serves a row of a cloud provider on `api`; `None` where
/// the gateway has none for it. The whole table:
///
/// | Row | Served on |
/// |---|---|
/// | Claude on Bedrock (`bedrock`, `bedrock_converse`) | chat, Messages |
/// | Nova, Mistral, DeepSeek on Bedrock (Converse) | chat, and as a chat fallback or routing-group member |
/// | Titan Text and Cohere Embed v3 on Bedrock | embeddings |
/// | Claude on Vertex AI (`vertex_ai`, `vertex`) | chat, Messages |
/// | Gemini on Vertex AI | chat, the Gemini route |
/// | any other Vertex AI publisher model of Google | the Gemini route |
///
/// Everything else has no backend: responses, text completions, a Claude or
/// Gemini row as a chat fallback, the media routes, the batch API.
fn cloud_backend(model: &LlmModel, api: ProviderApi) -> Option<CloudBackend> {
    use ProviderApi::*;
    let claude = crate::services::anthropic_adapter::should_route_via_openai_surface(model);
    if is_bedrock(&model.provider) {
        use crate::services::bedrock_runtime::Family;
        if claude {
            return matches!(api, Chat | AnthropicNative).then_some(CloudBackend::BedrockClaude);
        }
        return match (crate::services::bedrock_runtime::family(model), api) {
            (Some(Family::ConverseChat), Chat | ChatForward)
            | (Some(Family::TitanEmbed | Family::CohereEmbed), Embeddings) => {
                Some(CloudBackend::BedrockRuntime)
            }
            _ => None,
        };
    }
    if is_vertex(&model.provider) {
        let served = if claude {
            matches!(api, Chat | AnthropicNative)
        } else {
            match api {
                Chat => model.model_name.to_ascii_lowercase().contains("gemini"),
                GeminiNative => true,
                _ => false,
            }
        };
        return served.then_some(CloudBackend::Vertex);
    }
    None
}

/// The one dispatch check of a model row: `None` for a
/// row the gateway can send, the refusal otherwise. Run on the requested
/// model by `llm_preflight::enforce_actor_planes` and on every fallback and
/// routing-group member where it is about to be sent
/// (`llm_router::forward_chat_completion{,_streaming}`,
/// `llm_router::resolve_fallback_chain`).
///
/// A row of a cloud provider (Amazon Bedrock, Vertex AI) is decided here
/// and nowhere below: it is served by that provider's own backend on this
/// API, with the row's own cloud credential and an endpoint that can be
/// built, or it is refused. It never reaches a forwarder that authenticates
/// with an API key, with or without an `api_url`
/// (such a row with a legacy credential in `api_key` went out as
/// an unsigned OpenAI-shaped request, the AWS secret access key or the
/// service-account key in an API-key header).
pub fn dispatch_refusal(model: &LlmModel, api: ProviderApi) -> Option<DispatchRefusal> {
    if let Some(refusal) = unreadable_credential_refusal(model) {
        return Some(refusal);
    }
    if is_cloud_provider(&model.provider) {
        use crate::services::anthropic_adapter::backend::{bedrock, vertex};
        let problem = match cloud_backend(model, api) {
            None => {
                return Some(DispatchRefusal {
                    error_type: "unsupported_provider_api",
                    reason: unsupported_cloud_api(model),
                });
            }
            // Sent signed with the row's own credential or not at all,
            // to an endpoint built from a region that is one.
            Some(CloudBackend::BedrockClaude) => bedrock::BedrockBackend::row_problem(model),
            Some(CloudBackend::BedrockRuntime) => {
                crate::services::bedrock_runtime::row_problem(model)
            }
            // Called with the row's own service account or not at all,
            // in a project the row names.
            Some(CloudBackend::Vertex) => vertex::VertexBackend::row_problem(model),
        };
        return problem.map(|(error_type, reason)| DispatchRefusal { error_type, reason });
    }
    // From here the row is sent by a forwarder that authenticates with the
    // row's API key and knows no cloud identity. A row whose `auth_type`
    // says its credential is one (that credential was copied into
    // `x-brutor-*` request headers, which the upstream received) is not
    // sent: the gateway has no way to authenticate it here.
    if let Some(refusal) = cloud_auth_refusal(model) {
        return Some(refusal);
    }
    // The common case, without building the base (this runs on every
    // request): the row has an `api_url` or its provider a known base.
    if model
        .api_url
        .as_deref()
        .is_some_and(|u| !u.trim().is_empty())
        || known_base(&model.provider, api).is_some()
    {
        return None;
    }
    upstream_base(model, api)
        .err()
        .map(|reason| DispatchRefusal {
            error_type: PROVIDER_API_URL_MISSING,
            reason,
        })
}

/// The refusal of a row whose stored credential is a ciphertext the gateway
/// could not decrypt (`llm_router::open_model_credentials`);
/// `None` for every other row. Such a row has no credential the
/// gateway may use: it is not sent without one, and never with the stored
/// value.
pub fn unreadable_credential_refusal(model: &LlmModel) -> Option<DispatchRefusal> {
    model.credential_unreadable.then(|| DispatchRefusal {
        error_type: "provider_credential_missing",
        reason: format!(
            "provider_credential_missing: the stored credential of model '{}' (provider \
             '{}') cannot be decrypted with this gateway's ENCRYPTION_KEY, so the model has \
             no credential the gateway can use. Save the model's credential again in the \
             Control Plane, or give the gateway the ENCRYPTION_KEY it was stored under.",
            model.model_name, model.provider
        ),
    })
}

/// The error type of a row whose cloud `auth_type` the forwarder it would go
/// to cannot authenticate with.
pub const PROVIDER_AUTH_UNSUPPORTED: &str = "provider_auth_unsupported";

/// Why a row with a cloud `auth_type` cannot be sent by a forwarder that
/// authenticates with an API key; `None` for every other row.
///
/// * `aws_iam`: SigV4 is implemented for Amazon Bedrock only (Claude,
///   Converse, embeddings), on the routes that serve it.
/// * `gcp_service_account`: the Google access token is implemented for
///   Vertex AI only (Claude and Gemini on the chat, Messages and Gemini
///   routes).
/// * `azure_ad`: the client-credentials flow is not implemented. A row that
///   also carries the resource's API key is sent with that key.
fn cloud_auth_refusal(model: &LlmModel) -> Option<DispatchRefusal> {
    let auth_type = model.auth_type.as_deref()?;
    let what = match auth_type {
        "aws_iam" => {
            "AWS IAM authentication (SigV4) is implemented for Amazon Bedrock models on the \
             routes that serve them, not here"
        }
        "gcp_service_account" => {
            "Google service-account authentication is implemented for Vertex AI models on the \
             chat, Messages and Gemini routes, not here"
        }
        "azure_ad" => {
            if model
                .api_key
                .as_deref()
                .is_some_and(|k| !k.trim().is_empty())
            {
                return None;
            }
            "Azure AD (client credentials) authentication is not implemented; give the model \
             the resource's API key"
        }
        _ => return None,
    };
    Some(DispatchRefusal {
        error_type: PROVIDER_AUTH_UNSUPPORTED,
        reason: format!(
            "{PROVIDER_AUTH_UNSUPPORTED}: model '{}' of provider '{}' has auth type \
             '{auth_type}', which the gateway cannot authenticate with on this route: {what}. \
             Nothing was sent, and the model's cloud credential is never sent to a provider.",
            model.model_name, model.provider
        ),
    })
}

/// Why the gateway cannot dispatch to `model` on `api`, when its provider is
/// a cloud and the gateway has no backend of that provider for the row on
/// that API; `None` for a row of
/// another provider and for a cloud row that is served there
/// ([`dispatch_refusal`] then checks its credential and endpoint).
pub fn unsupported_provider_api(model: &LlmModel, api: ProviderApi) -> Option<String> {
    (is_cloud_provider(&model.provider) && cloud_backend(model, api).is_none())
        .then(|| unsupported_cloud_api(model))
}

/// The message of a cloud row on an API the gateway has no backend of its
/// provider for: what the gateway does serve the row on.
fn unsupported_cloud_api(model: &LlmModel) -> String {
    use crate::services::bedrock_runtime::Family;
    let claude = crate::services::anthropic_adapter::should_route_via_openai_surface(model);
    let (name, provider) = (&model.model_name, &model.provider);
    if is_vertex(provider) {
        return if claude {
            format!(
                "unsupported_provider_api: the gateway serves model '{name}' (provider \
                 '{provider}') on Vertex AI through rawPredict with the Anthropic Messages \
                 body, on the chat completions and Messages routes only. This route is not \
                 implemented for it, and a Vertex AI model is never sent by a forwarder that \
                 authenticates with an API key."
            )
        } else {
            format!(
                "unsupported_provider_api: the gateway serves model '{name}' (provider \
                 '{provider}') on Vertex AI through generateContent, on the Gemini route (and \
                 a Gemini model on the chat completions route too). This route is not \
                 implemented for it, and a Vertex AI model is never sent by a forwarder that \
                 authenticates with an API key."
            )
        };
    }
    if claude {
        return format!(
            "unsupported_provider_api: the gateway serves model '{name}' (provider \
             '{provider}') on Amazon Bedrock through InvokeModel with the Anthropic Messages \
             body, on the chat completions and Messages routes only. This route is not \
             implemented for it, and a Bedrock request is never sent unsigned."
        );
    }
    match crate::services::bedrock_runtime::family(model) {
        Some(Family::ConverseChat) => format!(
            "unsupported_provider_api: the gateway serves model '{name}' (provider \
             '{provider}') on Amazon Bedrock through the Converse API, on the chat completions \
             route only. This route is not implemented for it."
        ),
        Some(Family::TitanEmbed | Family::CohereEmbed) => format!(
            "unsupported_provider_api: the gateway serves model '{name}' (provider \
             '{provider}') on Amazon Bedrock through InvokeModel, on the embeddings route \
             only. This route is not implemented for it."
        ),
        None => format!(
            "unsupported_provider_api: the gateway does not implement the Amazon Bedrock \
             API for model '{name}' (provider '{provider}'). On Bedrock it serves Anthropic \
             Claude models (InvokeModel), the Amazon Nova, Mistral and DeepSeek chat models \
             (Converse) and the Amazon Titan Text and Cohere Embed v3 embedding models \
             (InvokeModel); the request bodies of other model families are not implemented."
        ),
    }
}

/// An operator's `api_url` with a provider path appended: the ONE place a
/// provider URL is built from a base.
/// The path goes after the base's path and the base's query stays the
/// query, after the path's own if it has one. `path` starts with `/` (or
/// is empty); nothing is inserted between the two. Appending to the string
/// put the path inside the query of an `api_url` that has one
/// (`https://host/base?api-version=1` + `/v1/audio/speech`).
pub fn join_api_url(api_url: &str, path: &str) -> String {
    let (base, base_query) = match api_url.split_once('?') {
        Some((base, query)) => (base, Some(query)),
        None => (api_url, None),
    };
    let (path, path_query) = match path.split_once('?') {
        Some((path, query)) => (path, Some(query)),
        None => (path, None),
    };
    let mut url = format!("{}{path}", base.trim_end_matches('/'));
    let mut separator = '?';
    for query in [path_query, base_query]
        .into_iter()
        .flatten()
        .filter(|q| !q.is_empty())
    {
        url.push(separator);
        url.push_str(query);
        separator = '&';
    }
    url
}

fn base_url_and_endpoint(model: &LlmModel, endpoint: &str, stream: bool) -> String {
    // A Bedrock row served through Converse or InvokeModel: the operation's
    // URL on the Bedrock Runtime host of the row's region (or the row's
    // `api_url`), never a generic `…/chat/completions`.
    if let Some(operation) =
        crate::services::bedrock_runtime::operation_for(model, endpoint, stream)
    {
        return crate::services::bedrock_runtime::endpoint_url(model, operation)
            .unwrap_or_else(|_| format!("{NO_UPSTREAM_SCHEME}://{}/{operation}", model.provider));
    }
    let adapter = llm_adapter::get_adapter(&model.provider);
    let resolved_endpoint = adapter.chat_endpoint(model, endpoint, stream);
    // Any other row of a cloud provider has no URL here, whatever its
    // `api_url`: this is the URL of the forwarder that authenticates
    // with an API key, and `dispatch_refusal` refuses such a row before any
    // caller builds it. The backstop is a URL no HTTP client sends.
    if is_cloud_provider(&model.provider) {
        return format!(
            "{NO_UPSTREAM_SCHEME}://{}{resolved_endpoint}",
            model.provider
        );
    }
    match upstream_base(model, ProviderApi::Llm) {
        // The query of an `api_url` stays the query:
        // an endpoint pasted with one, as Azure shows it
        // (`…/deployments/d?api-version=…`), got the path appended
        // after it.
        Ok(base) => join_api_url(&base, &resolved_endpoint),
        // No destination. `dispatch_refusal` refuses such a row
        // before any caller builds a URL; a caller that skipped it gets a
        // URL no HTTP client sends (not an `http(s)` scheme), never another
        // provider's host.
        Err(_) => format!(
            "{NO_UPSTREAM_SCHEME}://{}{resolved_endpoint}",
            model.provider
        ),
    }
}

/// The scheme of the URL `build_url` answers for a row with no destination.
/// `reqwest` refuses to send it (not `http` or `https`): nothing is resolved
/// or connected.
pub const NO_UPSTREAM_SCHEME: &str = "no-upstream-base";

/// Build authorization and provider-specific headers.
pub fn build_headers(model: &LlmModel) -> Vec<(String, String)> {
    build_headers_for(model, "")
}

/// Headers for one endpoint: a provider may authenticate an endpoint
/// differently (Gemini embeddings).
pub fn build_headers_for(model: &LlmModel, endpoint: &str) -> Vec<(String, String)> {
    // A row of a cloud provider is authenticated by its provider's own
    // backend (SigV4, a Google access token), never with an API-key header:
    // whatever is in its `api_key` is put in no header.
    if is_cloud_provider(&model.provider) {
        return Vec::new();
    }
    let adapter = llm_adapter::get_adapter(&model.provider);
    let mut headers = adapter.auth_headers_for_endpoint(model, endpoint);

    // (Azure's `api-version` is a query parameter, not a header: `build_url`.)

    // Extra headers from model config
    if let Some(ref extra) = model.extra_headers {
        if let Some(obj) = extra.as_object() {
            for (k, v) in obj {
                if let Some(s) = v.as_str() {
                    headers.push((k.clone(), s.to_string()));
                }
            }
        }
    }

    headers
}

/// Apply parameter dropping to the request body.
///
/// Drop list is the union of:
///   * the provider row's `additional_drop_params` (inherited at
///     runtime by every model under the provider — applied only
///     when `model.drop_params` is true so operators can opt a
///     specific model out for debugging upstream);
///   * the model row's own `additional_drop_params` (ALWAYS
///     applied — operators put params here when they're certain
///     the upstream rejects them, the legacy contract is preserved).
///
/// Both lists are operator-editable from the admin UI; provider-wide
/// defaults are seeded by `init_llm_providers.PROVIDER_SEED_CATALOG`.
///
/// Logs at DEBUG — every drop is configured-and-known behaviour,
/// not a runtime warning. Operators can flip `RUST_LOG=debug` if
/// they need to confirm what the proxy stripped.
/// Anthropic retired the `temperature` and `top_p` sampling knobs on its
/// newer models: the API answers `400 "\`temperature\` is deprecated for
/// this model."` (likewise for `top_p`) — observed on Claude Opus 4.8 and
/// every Claude 5 (Opus/Sonnet/Fable), while Opus 4.7, Sonnet 4.6 and
/// Haiku 4.5 still accept both. The gateway injects the model row's
/// `default_temperature` (0.7 from the seed) into every request that
/// does not set one, so without this rule *every* chat through those
/// models failed — and the resulting 400s were then counted by the
/// breaker, which gated the model for everybody.
///
/// Version rule: `claude-<family>-<major>[-<minor>[-<date>]]`, retired
/// from 4.8 upwards. Old-style names (`claude-3-5-sonnet-20241022`) put
/// the numbers before the family and are all < 4.8.
pub fn anthropic_sampling_params_retired(model_name: &str) -> bool {
    match claude_version(model_name).as_slice() {
        [major, ..] if *major >= 5 => true,
        [4, minor] => *minor >= 8,
        _ => false,
    }
}

/// Claude 4.6 and later take `thinking: {type: "adaptive"}`. A fixed
/// `budget_tokens` is deprecated on 4.6 and answered with a 400 from 4.7 on.
pub fn anthropic_uses_adaptive_thinking(model_name: &str) -> bool {
    match claude_version(model_name).as_slice() {
        [major, ..] if *major >= 5 => true,
        [4, minor] => *minor >= 6,
        _ => false,
    }
}

/// Request-body note from a route to the Anthropic adapter: an output policy
/// that can block or redact applies to this call, and those checks read the
/// answer only, so the model's thinking must come back unreadable
/// ([`withhold_anthropic_thinking`]). Removed before any send
/// (`prepare_request` step 4a, the Bedrock / Vertex bridge's translation).
pub const OMIT_REASONING_MARKER: &str = "brutor_omit_reasoning";

/// Ask a Claude model for thinking that comes back unreadable, where the
/// model can. Leaves a request without thinking, or with thinking disabled,
/// as it is.
///
/// - 4.7 and later (`anthropic_thinking_summary_is_opt_in`): ask for
///   `display: "omitted"`. The model still thinks; no readable text is made.
/// - Older Claude cannot omit. Its request is left as it is: the model keeps
///   its thinking, and the response side clears the readable text before the
///   client or the log sees it (`blank_native_thinking`, the native stream's
///   `thinking_delta` drop, `withhold_unscanned_reasoning`). This is done
///   instead of turning thinking off, because the check below
///   shows a cleared block still replays.
///
/// Either way a tool loop keeps working: Claude validates a replayed thinking
/// block by its signature only. Checked against the provider (Sonnet / Haiku
/// 4.5, Sonnet 4.6, Opus 4.7, Opus / Sonnet 5.5, Fable 5.1): cleared text,
/// changed text and a removed block all replay with 200; a changed
/// signature is a 400.
pub fn withhold_anthropic_thinking(obj: &mut serde_json::Map<String, Value>, model_name: &str) {
    if !anthropic_thinking_summary_is_opt_in(model_name) {
        return;
    }
    let Some(thinking) = obj.get_mut("thinking").and_then(Value::as_object_mut) else {
        return;
    };
    if thinking.get("type").and_then(Value::as_str) == Some("disabled") {
        return;
    }
    thinking.insert("display".into(), Value::String("omitted".into()));
}

/// Whether the model id names a Claude model, on any platform.
pub fn is_claude_model(model_name: &str) -> bool {
    claude_base_name(model_name).is_some()
}

/// The first-party form of a Claude model id, lower-cased, or `None` when it
/// is not one. Platform ids carry extras around the same name:
/// Bedrock `anthropic.claude-opus-4-7`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
/// and inference-profile ARNs (`arn:aws:bedrock:…:inference-profile/eu.anthropic.claude-…`);
/// Vertex `claude-opus-4-7@20260101` and resource paths
/// (`publishers/anthropic/models/claude-…`). Without this, those ids were not
/// recognised as Claude at all, so no version rule applied to them.
pub fn claude_base_name(model_name: &str) -> Option<String> {
    let lower = model_name.trim().to_lowercase();
    let mut base = lower.rsplit('/').next().unwrap_or("");
    if let Some(at) = base.rfind("anthropic.") {
        base = &base[at + "anthropic.".len()..];
    }
    let base = base.split('@').next().unwrap_or("");
    base.starts_with("claude").then(|| base.to_string())
}

/// From Claude 4.7 on, thinking text comes back empty unless the request asks
/// for `display: "summarized"`.
pub fn anthropic_thinking_summary_is_opt_in(model_name: &str) -> bool {
    match claude_version(model_name).as_slice() {
        [major, ..] if *major >= 5 => true,
        [4, minor] => *minor >= 7,
        _ => false,
    }
}

/// Numeric path segments of a Claude model name, in order; a date-like segment
/// (>= 1000) ends the version. `claude-opus-4-5-20251101` -> [4, 5];
/// `claude-opus-5` -> [5]. Empty for anything that is not a Claude model.
fn claude_version(model_name: &str) -> Vec<u32> {
    let Some(lower) = claude_base_name(model_name) else {
        return Vec::new();
    };
    let mut nums: Vec<u32> = Vec::new();
    for seg in lower.split('-') {
        if let Ok(n) = seg.parse::<u32>() {
            if n >= 1000 {
                break;
            }
            nums.push(n);
            if nums.len() == 2 {
                break;
            }
        }
    }
    nums
}

fn is_anthropic(model: &LlmModel) -> bool {
    model.provider.eq_ignore_ascii_case("anthropic")
}

pub fn drop_unsupported_params(body: &mut Value, model: &LlmModel) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    let should_drop_provider = model.drop_params.unwrap_or(true);

    let mut dropped: Vec<String> = Vec::new();
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();

    let mut apply = |source: Option<&Value>,
                     dropped: &mut Vec<String>,
                     seen: &mut std::collections::HashSet<String>| {
        let Some(arr) = source.and_then(|v| v.as_array()) else {
            return;
        };
        for item in arr {
            if let Some(name) = item.as_str() {
                if !seen.insert(name.to_string()) {
                    continue;
                }
                if obj.remove(name).is_some() {
                    dropped.push(name.to_string());
                }
            }
        }
    };

    if should_drop_provider {
        apply(
            model.provider_additional_drop_params.as_ref(),
            &mut dropped,
            &mut seen,
        );
    }
    apply(
        model.additional_drop_params.as_ref(),
        &mut dropped,
        &mut seen,
    );

    if !dropped.is_empty() {
        tracing::debug!(
            provider = %model.provider,
            model = %model.model_name,
            params = ?dropped,
            "Dropped unsupported params from request (provider + model additional_drop_params)"
        );
    }

    // Anthropic models that retired sampling parameters (see
    // `anthropic_sampling_params_retired`): drop `temperature` / `top_p`
    // whether the caller or a default put them there. Same shape as the
    // `temperature_locked` capability below — deterministic, so `debug!`.
    if is_anthropic(model) && anthropic_sampling_params_retired(&model.model_name) {
        for key in ["temperature", "top_p"] {
            if obj.remove(key).is_some() {
                tracing::debug!(
                    provider = %model.provider,
                    model = %model.model_name,
                    param = key,
                    "Dropped sampling param retired by Anthropic for this model"
                );
            }
        }
    }

    // Capability flag: ``temperature_locked``. Some models (notably
    // Moonshot's Kimi K2.x family) hard-pin temperature server-side
    // and reject any other value with a 400. The seed flags these so
    // we can drop the parameter before forwarding rather than letting
    // the request 400 every time.
    if let Some(caps) = model.capabilities.as_ref().and_then(|v| v.as_object()) {
        if caps
            .get("temperature_locked")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            if obj.remove("temperature").is_some() {
                // `debug!`, not `warn!` — deterministic capability-based
                // scrub on every request to a temperature-locked model.
                // Operators don't need to react; they just need to know
                // it's correctly happening when they go looking.
                tracing::debug!(
                    provider = %model.provider,
                    model = %model.model_name,
                    "Dropped `temperature` (model has capabilities.temperature_locked=true)"
                );
            }
        }

        // Capability flag: ``supports_function_calling=false``. Some
        // models (Groq Compound family is the canonical case) are
        // closed agentic systems that run their own internal tools
        // and reject any caller-supplied ``tools`` / ``tool_choice``
        // / ``functions`` / ``function_call`` with a 400. Strip them
        // here so the request goes through cleanly — the model just
        // won't see the tools it can't honour anyway.
        let supports_fc = caps
            .get("supports_function_calling")
            .and_then(|v| v.as_bool())
            // ``None`` means "not declared" — preserve today's behaviour
            // (assume yes) so we don't accidentally strip from models
            // whose seed predates the flag.
            .unwrap_or(true);
        if !supports_fc {
            let mut dropped: Vec<&'static str> = Vec::new();
            for key in &["tools", "tool_choice", "functions", "function_call"] {
                if obj.remove(*key).is_some() {
                    dropped.push(*key);
                }
            }
            if !dropped.is_empty() {
                // `debug!`, not `warn!` — same reasoning as
                // temperature_locked: deterministic capability-based
                // scrub, not an operator signal.
                tracing::debug!(
                    provider = %model.provider,
                    model = %model.model_name,
                    params = ?dropped,
                    "Dropped tool-calling params (model has supports_function_calling=false)"
                );
            }
        }
    }
}

/// Inject model-level default parameters into the request body.
/// Only sets values that aren't already provided by the caller.
pub fn inject_model_defaults(body: &mut Value, model: &LlmModel) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    // Treat client-supplied top-level `null`s as "unset" BEFORE deciding
    // which model defaults to inject. OpenAI-style SDKs and our own portal
    // serialize omitted params as explicit `null` (`temperature: null`,
    // `top_p: null`, `n: null`, `stop: null`, …) — and the proxy's own
    // `ChatCompletionRequest` struct re-serializes its unset `Option`
    // fields the same way. Without this strip, every `contains_key` guard
    // below sees the key as *present* and silently skips the model's
    // configured default, so only `max_tokens` ever made it through (it has
    // a second fallback inside the Anthropic adapter; the generic params
    // have none). Removing the nulls here lets the guards fire uniformly on
    // absent-OR-null. Nested nulls inside `messages` are untouched (not
    // top-level keys); `prepare_request` step 6 still strips any residual.
    let null_keys: Vec<String> = obj
        .iter()
        .filter(|(_, v)| v.is_null())
        .map(|(k, _)| k.clone())
        .collect();
    for k in null_keys {
        obj.remove(&k);
    }

    let sampling_retired =
        is_anthropic(model) && anthropic_sampling_params_retired(&model.model_name);

    // Temperature
    if !sampling_retired && !obj.contains_key("temperature") {
        if let Some(temp) = model.default_temperature {
            obj.insert("temperature".into(), Value::from(temp));
        }
    }

    // Max tokens
    if !obj.contains_key("max_tokens") && !obj.contains_key("max_completion_tokens") {
        if let Some(mt) = model.default_max_tokens {
            obj.insert("max_tokens".into(), Value::from(mt));
        }
    }

    // Top-p
    if !sampling_retired && !obj.contains_key("top_p") {
        if let Some(tp) = model.top_p {
            obj.insert("top_p".into(), Value::from(tp));
        }
    }

    // Penalties
    if !obj.contains_key("presence_penalty") {
        if let Some(pp) = model.presence_penalty {
            obj.insert("presence_penalty".into(), Value::from(pp));
        }
    }
    if !obj.contains_key("frequency_penalty") {
        if let Some(fp) = model.frequency_penalty {
            obj.insert("frequency_penalty".into(), Value::from(fp));
        }
    }

    // Seed
    if !obj.contains_key("seed") {
        if let Some(s) = model.seed {
            obj.insert("seed".into(), Value::from(s));
        }
    }

    // Response format
    if !obj.contains_key("response_format") {
        if let Some(ref rf) = model.response_format {
            obj.insert("response_format".into(), rf.clone());
        }
    }

    // N (number of completions) — only if > 1
    if !obj.contains_key("n") {
        if let Some(n) = model.n {
            if n > 1 {
                obj.insert("n".into(), Value::from(n));
            }
        }
    }

    // Stop sequences
    if !obj.contains_key("stop") {
        if let Some(ref ss) = model.stop_sequences {
            obj.insert("stop".into(), ss.clone());
        }
    }

    // Logit bias
    if !obj.contains_key("logit_bias") {
        if let Some(ref lb) = model.logit_bias {
            obj.insert("logit_bias".into(), lb.clone());
        }
    }

    // Logprobs
    if !obj.contains_key("logprobs") {
        if let Some(lp) = model.logprobs {
            if lp {
                obj.insert("logprobs".into(), Value::Bool(true));
            }
        }
    }

    // Top logprobs
    if !obj.contains_key("top_logprobs") {
        if let Some(tlp) = model.top_logprobs {
            obj.insert("top_logprobs".into(), Value::from(tlp));
        }
    }

    // Stream options
    if !obj.contains_key("stream_options") {
        if let Some(ref so) = model.stream_options {
            obj.insert("stream_options".into(), so.clone());
        }
    }

    // Tools (model-level default tools)
    if !obj.contains_key("tools") {
        if let Some(ref tools) = model.tools {
            obj.insert("tools".into(), tools.clone());
        }
    }

    // Tool choice
    if !obj.contains_key("tool_choice") {
        if let Some(ref tc) = model.tool_choice {
            obj.insert("tool_choice".into(), Value::String(tc.clone()));
        }
    }

    // Parallel tool calls — only inject if explicitly disabled
    if !obj.contains_key("parallel_tool_calls") {
        if let Some(ptc) = model.parallel_tool_calls {
            if !ptc {
                obj.insert("parallel_tool_calls".into(), Value::Bool(false));
            }
        }
    }

    // User identifier for abuse monitoring
    if !obj.contains_key("user") {
        if let Some(ref uid) = model.user_identifier {
            obj.insert("user".into(), Value::String(uid.clone()));
        }
    }
}

/// Inject the model's `system_prompt` as the FIRST system message of
/// the request, MERGING cleanly with any caller-supplied system
/// messages rather than skipping when one is already present.
///
/// ## Merge semantics
///
/// * Server's `system_prompt` becomes the first `role:"system"`
///   message at index 0 of the messages array.
/// * Caller-supplied system messages keep their original order; they
///   sit AFTER the server's prompt.
/// * Non-system messages are unaffected.
/// * Empty or missing `system_prompt` on the model is a no-op — we
///   never inject an empty system block.
///
/// ## Why merge instead of skip
///
/// Two reasons:
///
/// 1. **Policy enforcement.** Operators configure
///    `LLModel.system_prompt` on the catalogue row expecting it to
///    apply on every call through that model. Silently dropping it
///    when a direct-API caller happens to pass their own system
///    message breaks that expectation.
///
/// 2. **Prompt-cache hit rate.** Provider-side caches key on stable
///    prefix bytes. The server's prompt is stable across all calls
///    in the same tenant/model — putting it FIRST maximises the
///    cacheable prefix. If the caller's variable system block lands
///    first, the shared prefix collapses to whatever the two have
///    in common (usually zero), and the cache never warms.
pub fn inject_system_prompt(body: &mut Value, model: &LlmModel) {
    let system_prompt = match &model.system_prompt {
        Some(s) if !s.is_empty() => s,
        _ => return,
    };

    let messages = match body.get_mut("messages").and_then(|m| m.as_array_mut()) {
        Some(m) => m,
        None => return,
    };

    let sys_msg = serde_json::json!({
        "role": "system",
        "content": system_prompt,
    });
    messages.insert(0, sys_msg);
}

/// Apply provider-specific parameter transformations via the adapter.
/// Maps OpenAI-format parameters to each provider's native format.
pub fn transform_for_provider(body: &mut Value, model: &LlmModel) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    let adapter = llm_adapter::get_adapter(&model.provider);
    adapter.transform_request(obj, model);
}

/// The output-limit spelling `model`'s provider takes, as its adapter
/// decides it: a bounded probe is translated by the adapter and the limit it
/// comes out as is the answer. There is no second rule to drift from the
/// adapter. `max_tokens` when the adapter nests the limit elsewhere
/// (Gemini's `generationConfig`, which the ceiling handles by body shape).
pub fn native_output_limit_key(model: &LlmModel) -> &'static str {
    let mut probe = serde_json::Map::new();
    probe.insert(
        "model".into(),
        Value::String(wire_model_name(model).to_string()),
    );
    probe.insert("max_tokens".into(), serde_json::json!(1));
    llm_adapter::get_adapter(&model.provider).transform_request(&mut probe, model);
    crate::services::llm_governance::OUTPUT_LIMIT_KEYS
        .into_iter()
        .find(|k| probe.contains_key(*k))
        .unwrap_or("max_tokens")
}

/// Bound a provider-shaped body for `model` to `ceiling` output tokens,
/// inserting the limit in the provider's own spelling when it carries none.
pub fn apply_output_ceiling_for_model(body: &mut Value, model: &LlmModel, ceiling: i64) {
    crate::services::llm_governance::apply_output_ceiling_as(
        body,
        ceiling,
        native_output_limit_key(model),
    );
}

/// Full request preparation pipeline for chat completions:
/// inject defaults, system prompt, transform, drop params, strip nulls.
pub fn prepare_request(body: &mut Value, model: &LlmModel) {
    // 1. Override model name with provider's expected name
    if let Some(obj) = body.as_object_mut() {
        obj.insert(
            "model".into(),
            Value::String(wire_model_name(model).to_string()),
        );
        obj.remove("model_id");
        obj.remove("extra");
    }

    // 2. Inject model defaults for missing params
    inject_model_defaults(body, model);

    // 3. Inject system prompt
    inject_system_prompt(body, model);

    // 4. Provider-specific transformations
    transform_for_provider(body, model);

    // 4a. The route's "omit reasoning" note is read by the Anthropic adapter
    // in step 4 and goes to no provider.
    if let Some(obj) = body.as_object_mut() {
        obj.remove(OMIT_REASONING_MARKER);
    }

    // 4b. `thinking_blocks` is how Anthropic's signed thinking round-trips
    // through the OpenAI shape. The Anthropic adapter has already moved them
    // into content; any still here are bound for a provider that would reject
    // the field (a client replaying history after a routing fallback).
    if let Some(msgs) = body.get_mut("messages").and_then(|m| m.as_array_mut()) {
        for msg in msgs.iter_mut().filter_map(|m| m.as_object_mut()) {
            msg.remove("thinking_blocks");
        }
    }

    // 5. Drop unsupported params
    drop_unsupported_params(body, model);

    // 5b. Clamp model-injected output params against the group's
    // governance ceilings. `inject_model_defaults` (step 2) and the
    // Anthropic adapter's `max_tokens` fallback (run during step 4)
    // both fill params the caller omitted — *after* the first governance pass
    // already clamped/rejected the caller-supplied values. Without this
    // pass a model whose `default_max_tokens` (or default temperature)
    // exceeds the group ceiling would escape the cap. The route handler
    // scopes `EFFECTIVE_PARAM_CAPS` around its forward call; we read it
    // here at the injection chokepoint so the clamp also covers the
    // routing case (the member model resolves inside the router, after
    // governance ran on the virtual routing model). Clamp-only — see
    // `llm_governance::clamp_injected_params`.
    if let Some(caps) = crate::services::llm_governance::current_param_caps() {
        crate::services::llm_governance::clamp_injected_params(
            body,
            &caps,
            native_output_limit_key(model),
        );
    }

    // 6. Strip top-level null values.
    //
    // The Rust route handlers deserialize incoming requests into
    // structs full of ``Option<T>`` fields (temperature, top_p, n,
    // stop, presence_penalty, frequency_penalty, tools, tool_choice,
    // stream, …). When we re-serialize to JSON to send upstream,
    // every unset Option becomes a literal ``null``. Most providers
    // tolerate that, but some (notably Google's OpenAI-compat
    // endpoint) reject the entire request with
    // ``INVALID_ARGUMENT: Value is not a string: null`` on the first
    // null they see — the health check then fails through no fault
    // of the operator's config.
    //
    // The same hygiene step has lived in the Anthropic adapter for
    // ages, but it ran only on that one provider. Hoisting it here
    // makes every chat-completion call defensive by default while
    // still honouring intentional ``null`` values inside `messages`
    // (those are nested objects, not top-level keys, so they're
    // unaffected).
    if let Some(obj) = body.as_object_mut() {
        let null_keys: Vec<String> = obj
            .iter()
            .filter(|(_, v)| v.is_null())
            .map(|(k, _)| k.clone())
            .collect();
        for k in null_keys {
            obj.remove(&k);
        }
    }
}

/// Prepare a text completion request.
/// Text completions are converted to chat format for non-OpenAI providers and
/// converted back: for non-OpenAI, convert prompt → messages
/// and route through the chat completion pipeline.
pub fn prepare_text_completion(body: &mut Value, model: &LlmModel) -> bool {
    if let Some(obj) = body.as_object_mut() {
        obj.insert(
            "model".into(),
            Value::String(wire_model_name(model).to_string()),
        );
        obj.remove("model_id");
        obj.remove("extra");
    }

    // For OpenAI-compatible providers, keep as text completion
    let use_chat_format = !matches!(
        model.provider.as_str(),
        "openai" | "azure" | "azure_ai" | "text-completion-openai"
    );

    if use_chat_format {
        // Convert prompt → messages for chat completion format
        if let Some(obj) = body.as_object_mut() {
            if let Some(prompt) = obj.remove("prompt") {
                let prompt_str = match &prompt {
                    Value::String(s) => s.clone(),
                    Value::Array(arr) => arr
                        .iter()
                        .filter_map(|v| v.as_str())
                        .collect::<Vec<_>>()
                        .join(""),
                    _ => prompt.to_string(),
                };
                let messages = serde_json::json!([
                    {"role": "user", "content": prompt_str}
                ]);
                obj.insert("messages".into(), messages);
            }
            // Remove text-completion-only params
            obj.remove("echo");
            obj.remove("best_of");
            obj.remove("suffix");
            obj.remove("logprobs");
        }

        // Run through the full chat preparation pipeline
        inject_model_defaults(body, model);
        inject_system_prompt(body, model);
        transform_for_provider(body, model);
        drop_unsupported_params(body, model);
    } else {
        // Text completion mode — just drop unsupported params
        drop_unsupported_params(body, model);
    }

    use_chat_format
}

/// The provider's reply as JSON, never `null`.
///
/// `resp.json().await.unwrap_or_default()` turned any reply that is not JSON
/// (a plain-text or HTML error page, an empty body) into `null`, so the caller
/// and the audit row lost what the provider said. A non-JSON reply is kept as
/// an OpenAI-shaped error carrying its status and (truncated) text.
pub async fn upstream_json(resp: reqwest::Response) -> Value {
    let status = resp.status().as_u16();
    let text = match resp.text().await {
        Ok(t) => t,
        Err(e) => {
            return serde_json::json!({"error": {
                "message": format!("could not read the provider's reply (HTTP {status}): {e}"),
                "type": "upstream_error",
            }});
        }
    };
    upstream_json_from_text(status, &text)
}

/// Pure half of [`upstream_json`], for tests.
pub fn upstream_json_from_text(status: u16, text: &str) -> Value {
    if let Ok(v) = serde_json::from_str::<Value>(text) {
        if !v.is_null() {
            return v;
        }
    }
    let snippet: String = text.trim().chars().take(1000).collect();
    let message = if snippet.is_empty() {
        format!("provider returned HTTP {status} with an empty body")
    } else {
        format!("provider returned HTTP {status}: {snippet}")
    };
    serde_json::json!({"error": {"message": message, "type": "upstream_error"}})
}

/// Prepare an embedding request with provider-specific parameter handling.
pub fn prepare_embedding(body: &mut Value, model: &LlmModel) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    // The provider's own id: a `gemini/` catalog prefix is not part of it
    // (Google's OpenAI-compatible endpoint 404s on it).
    let wire_name = if model.provider == "gemini" || model.provider == "google_ai_studio" {
        model.model_name.trim_start_matches("gemini/").to_string()
    } else {
        wire_model_name(model).to_string()
    };
    obj.insert("model".into(), Value::String(wire_name));
    obj.remove("model_id");
    obj.remove("extra");

    let provider = model.provider.as_str();

    // dimensions: only supported by certain providers/models
    // Supported by: OpenAI text-embedding-3-*, Cohere, Vertex, Voyage, etc.
    if let Some(_dims) = obj.get("dimensions").cloned() {
        match provider {
            "openai" => {
                // Only text-embedding-3-* supports dimensions
                if !model.model_name.contains("text-embedding-3") {
                    obj.remove("dimensions");
                }
            }
            "cohere" => {
                // Cohere supports dimensions via input_type → maps differently
                // Keep dimensions as-is; Cohere v2 API supports it
            }
            "voyage" | "jina_ai" => {
                // These support dimensions natively
            }
            _ => {
                // Most providers don't support dimensions — drop it
                obj.remove("dimensions");
            }
        }
    }

    // encoding_format: OpenAI supports "float" and "base64"
    // Most other providers don't support it
    match provider {
        "openai" | "azure" | "azure_ai" => {
            // Keep encoding_format as-is
        }
        "cohere" => {
            // Cohere uses embedding_types instead
            if let Some(ef) = obj.remove("encoding_format") {
                if let Some(fmt) = ef.as_str() {
                    let cohere_types = match fmt {
                        "float" => serde_json::json!(["float"]),
                        "base64" => serde_json::json!(["float"]), // Cohere doesn't support base64
                        _ => serde_json::json!(["float"]),
                    };
                    obj.insert("embedding_types".into(), cohere_types);
                }
            }
        }
        _ => {
            obj.remove("encoding_format");
        }
    }

    // Remove null values
    let keys_to_remove: Vec<String> = obj
        .iter()
        .filter(|(_, v)| v.is_null())
        .map(|(k, _)| k.clone())
        .collect();
    for k in keys_to_remove {
        obj.remove(&k);
    }
}

/// Prepare a Responses API request.
/// The Responses API uses a different parameter structure from chat completions:
/// - `input` instead of `messages`
/// - `instructions` for system-level guidance
/// - `text_format` for response formatting
/// - `reasoning` for chain-of-thought control
/// - `store`, `truncation`, `previous_response_id` for session management
pub fn prepare_responses(body: &mut Value, model: &LlmModel) {
    let obj = match body.as_object_mut() {
        Some(o) => o,
        None => return,
    };

    obj.insert(
        "model".into(),
        Value::String(wire_model_name(model).to_string()),
    );
    obj.remove("model_id");
    obj.remove("extra");

    // Inject system prompt as instructions if not already set
    if !obj.contains_key("instructions") {
        if let Some(ref sp) = model.system_prompt {
            if !sp.is_empty() {
                obj.insert("instructions".into(), Value::String(sp.clone()));
            }
        }
    }

    // Reasoning-mode models reject `temperature` and `top_p` upstream
    // — this is true across providers (OpenAI o-series + gpt-5*, xAI
    // grok reasoning models, etc.) The chat-completions path handles
    // this in each provider's adapter, but `prepare_responses` is the
    // only request-prep step on /v1/proxy/llm/responses, so without a
    // guard here we inject 0.7 (the `default_temperature` column
    // default) and the upstream 400s with "Unsupported parameter:
    // 'temperature' is not supported with this model".
    //
    // Detection: any of (a) `thinking_enabled` flag on the row, (b)
    // `capabilities.supports_reasoning=true` (the cross-provider
    // signal the seed already sets), or (c) a fallback name prefix
    // for OpenAI's hard-coded reasoning families (kept as a belt-and-
    // braces in case a row is mis-seeded with the cap flag missing).
    // (c) is the same definition the Chat Completions adapters use
    // (`llm_adapter::openai_family`), not a second list.
    let is_native_openai_reasoning = llm_adapter::openai_family::chat_rules(model).reasoning;
    let supports_reasoning_cap = model
        .capabilities
        .as_ref()
        .and_then(|v| v.get("supports_reasoning"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let thinking_on = model.thinking_enabled.unwrap_or(false)
        || supports_reasoning_cap
        || is_native_openai_reasoning;

    let sampling_retired =
        is_anthropic(model) && anthropic_sampling_params_retired(&model.model_name);

    if !thinking_on && !sampling_retired {
        if !obj.contains_key("temperature") {
            if let Some(temp) = model.default_temperature {
                obj.insert("temperature".into(), Value::from(temp));
            }
        }
        if !obj.contains_key("top_p") {
            if let Some(tp) = model.top_p {
                obj.insert("top_p".into(), Value::from(tp));
            }
        }
    } else {
        // Strip if a caller (or the seed) snuck them in.
        obj.remove("temperature");
        obj.remove("top_p");
    }

    if !obj.contains_key("max_output_tokens") {
        if let Some(mt) = model.default_max_tokens {
            obj.insert("max_output_tokens".into(), Value::from(mt));
        }
    }

    // Default tools from model config
    if !obj.contains_key("tools") {
        if let Some(ref tools) = model.tools {
            obj.insert("tools".into(), tools.clone());
        }
    }

    // Normalise function-tool shape. The Responses API expects
    // a flat layout — `{type: "function", name, description,
    // parameters}` — whereas Chat Completions uses a nested
    // wrapper — `{type: "function", function: {name, description,
    // parameters}}`. Clients (and our portal) ship the wrapper
    // form because that's what every OpenAI-compatible SDK builds
    // by default; without translation here the upstream rejects
    // the request with `Missing required parameter:
    // 'tools[0].name'`. Non-function tool types (`file_search`,
    // `web_search`, `code_interpreter`, …) pass through verbatim.
    //
    // `tool_choice` mirrors the same shape divergence when the
    // caller specifies a particular function — flatten there too.
    if let Some(tools_val) = obj.get_mut("tools") {
        if let Some(arr) = tools_val.as_array_mut() {
            for tool in arr.iter_mut() {
                if let Some(t_obj) = tool.as_object_mut() {
                    let is_function = t_obj
                        .get("type")
                        .and_then(|v| v.as_str())
                        .map(|s| s == "function")
                        .unwrap_or(false);
                    if !is_function {
                        continue;
                    }
                    if let Some(Value::Object(inner_obj)) = t_obj.remove("function") {
                        for (k, v) in inner_obj {
                            t_obj.entry(k).or_insert(v);
                        }
                    }
                }
            }
        }
    }
    if let Some(choice) = obj.get_mut("tool_choice") {
        if let Some(c_obj) = choice.as_object_mut() {
            let is_function = c_obj
                .get("type")
                .and_then(|v| v.as_str())
                .map(|s| s == "function")
                .unwrap_or(false);
            if is_function {
                if let Some(Value::Object(inner_obj)) = c_obj.remove("function") {
                    for (k, v) in inner_obj {
                        c_obj.entry(k).or_insert(v);
                    }
                }
            }
        }
    }

    // Remove null values
    let keys_to_remove: Vec<String> = obj
        .iter()
        .filter(|(_, v)| v.is_null())
        .map(|(k, _)| k.clone())
        .collect();
    for k in keys_to_remove {
        obj.remove(&k);
    }
}

// ===========================================================================
// Tests
// ===========================================================================
#[cfg(test)]
mod tests {
    /// A Bedrock row the gateway does not implement has no
    /// generic destination and is refused on every API; a row of a family
    /// it serves (Converse chat, InvokeModel embeddings) is refused on every
    /// API but its own, where its URL is the Bedrock Runtime operation.
    #[test]
    fn a_bedrock_row_is_served_on_its_own_api_and_refused_elsewhere() {
        use ProviderApi::*;
        // Rows that name their region (a row that names none is refused,
        // `anthropic_adapter::backend::bedrock`).
        let row = |provider: &str, name: &str| {
            let mut m = crate::services::llm_adapter::make_test_model(provider, name);
            m.api_url = None;
            m.model_metadata = Some(serde_json::json!({"aws_region": "us-east-1"}));
            m
        };
        // Families the gateway does not implement.
        for (provider, name) in [
            ("bedrock", "bedrock/meta.llama3-3-70b-instruct-v1:0"),
            ("bedrock", "bedrock/amazon.titan-text-express-v1"),
            ("bedrock", "bedrock/amazon.titan-embed-image-v1"),
            (
                "bedrock_converse",
                "bedrock/converse/cohere.command-r-plus-v1:0",
            ),
        ] {
            let m = row(provider, name);
            // The generic forwarder has no destination for it: the
            // URL is one no HTTP client sends, not OpenAI's.
            assert_eq!(
                build_url(&m, "/chat/completions", false),
                format!("no-upstream-base://{provider}/chat/completions")
            );
            for api in [
                Llm,
                Chat,
                ChatForward,
                Embeddings,
                AnthropicNative,
                Batch,
                Images,
            ] {
                let reason = unsupported_provider_api(&m, api).expect("refused");
                assert!(
                    reason.contains(provider) && reason.contains(name),
                    "{reason}"
                );
            }
        }
        // The chat families: Converse, on the chat route and its forwarder.
        for (provider, name, id) in [
            (
                "bedrock",
                "bedrock/amazon.nova-pro-v1:0",
                "amazon.nova-pro-v1%3A0",
            ),
            (
                "bedrock_converse",
                "bedrock/converse/deepseek.v3.2",
                "deepseek.v3.2",
            ),
            (
                "bedrock",
                "bedrock/mistral.mistral-large-3-675b-instruct",
                "mistral.mistral-large-3-675b-instruct",
            ),
        ] {
            let m = row(provider, name);
            let host = "https://bedrock-runtime.us-east-1.amazonaws.com";
            assert_eq!(
                build_url(&m, "/chat/completions", false),
                format!("{host}/model/{id}/converse")
            );
            assert_eq!(
                build_url(&m, "/chat/completions", true),
                format!("{host}/model/{id}/converse-stream")
            );
            for api in [Chat, ChatForward] {
                assert_eq!(unsupported_provider_api(&m, api), None, "{name} {api:?}");
            }
            for api in [
                Llm,
                Embeddings,
                AnthropicNative,
                GeminiNative,
                Batch,
                Images,
                Speech,
            ] {
                let reason = unsupported_provider_api(&m, api).expect("refused");
                assert!(
                    reason.contains(name) && reason.contains("Converse"),
                    "{reason}"
                );
                assert_eq!(
                    dispatch_refusal(&m, api).map(|r| r.error_type),
                    Some("unsupported_provider_api"),
                    "{name} {api:?}"
                );
            }
            // On no other endpoint does it get a Bedrock URL.
            assert_eq!(
                build_url(&m, "/embeddings", false),
                format!("no-upstream-base://{provider}/embeddings")
            );
        }
        // The embedding families: InvokeModel, on the embeddings route.
        for (name, id) in [
            (
                "bedrock/amazon.titan-embed-text-v2:0",
                "amazon.titan-embed-text-v2%3A0",
            ),
            ("bedrock/cohere.embed-english-v3", "cohere.embed-english-v3"),
        ] {
            let m = row("bedrock", name);
            assert_eq!(
                build_url(&m, "/embeddings", false),
                format!("https://bedrock-runtime.us-east-1.amazonaws.com/model/{id}/invoke")
            );
            assert_eq!(unsupported_provider_api(&m, Embeddings), None, "{name}");
            for api in [Llm, Chat, ChatForward, AnthropicNative, Batch, Images] {
                let reason = unsupported_provider_api(&m, api).expect("refused");
                assert!(
                    reason.contains(name) && reason.contains("InvokeModel"),
                    "{reason}"
                );
            }
            assert_eq!(
                build_url(&m, "/chat/completions", false),
                "no-upstream-base://bedrock/chat/completions"
            );
        }
        // A served row with no credential of its own is refused all the
        // same: nothing is sent unsigned (`make_test_model`'s key is not an
        // AWS credential).
        let nova = row("bedrock", "bedrock/amazon.nova-pro-v1:0");
        assert_eq!(
            dispatch_refusal(&nova, Chat).map(|r| r.error_type),
            Some("provider_credential_missing")
        );
        // A credential in `api_key` is not one.
        let mut legacy = nova.clone();
        legacy.api_key = Some("AKIAUNITTEST:unit-secret".into());
        assert_eq!(
            dispatch_refusal(&legacy, Chat).map(|r| r.error_type),
            Some("provider_credential_missing")
        );
        let mut signed = nova.clone();
        signed.provider_config = Some(
            serde_json::json!({"aws_access_key_id": "AKIAUNITTEST",
                               "aws_secret_access_key": "unit-secret"})
            .to_string(),
        );
        for api in [Chat, ChatForward] {
            assert_eq!(dispatch_refusal(&signed, api), None, "{api:?}");
        }
        assert!(dispatch_refusal(&signed, Llm).is_some());
        assert_eq!(ProviderApi::of_route("/v1/embeddings"), Embeddings);
        // Claude on Bedrock is the Claude backend's: the chat and Messages
        // routes, and nowhere else (on the other APIs it went to the
        // generic forwarder).
        for (provider, name) in [
            ("bedrock", "bedrock/global.anthropic.claude-sonnet-5-5"),
            (
                "bedrock_converse",
                "bedrock/converse/us.anthropic.claude-haiku-5-5",
            ),
        ] {
            let m = crate::services::llm_adapter::make_test_model(provider, name);
            for api in [Chat, AnthropicNative] {
                assert_eq!(unsupported_provider_api(&m, api), None, "{provider}/{name}");
            }
            for api in [Llm, ChatForward, Embeddings, GeminiNative, Images, Batch] {
                assert!(
                    unsupported_provider_api(&m, api).is_some(),
                    "{provider}/{name} {api:?}"
                );
            }
        }
        // Not a cloud's row: not refused here.
        for (provider, name) in [("openai", "gpt-5.2"), ("mistral", "mistral-large-3")] {
            let m = crate::services::llm_adapter::make_test_model(provider, name);
            for api in [Llm, Chat, ChatForward, Embeddings] {
                assert_eq!(unsupported_provider_api(&m, api), None, "{provider}/{name}");
            }
        }
    }

    /// A row of a cloud provider is served by that provider's
    /// own backend on the API in question or refused. Whatever else the row
    /// carries (an `api_url`, a credential in `api_key`, a cloud
    /// `auth_type`, nothing), it is never passed on to the checks of the
    /// forwarder that authenticates with an API key.
    #[test]
    fn a_cloud_row_is_served_by_its_own_backend_or_refused() {
        use ProviderApi::*;
        const APIS: [ProviderApi; 11] = [
            Llm,
            Chat,
            ChatForward,
            Embeddings,
            AnthropicNative,
            GeminiNative,
            Images,
            Speech,
            Transcription,
            Video,
            Batch,
        ];
        // (provider, model name, the APIs its backend serves)
        let rows: [(&str, &str, &[ProviderApi]); 9] = [
            (
                "bedrock",
                "bedrock/global.anthropic.claude-sonnet-5-5",
                &[Chat, AnthropicNative],
            ),
            (
                "bedrock_converse",
                "bedrock/converse/us.anthropic.claude-haiku-5-5",
                &[Chat, AnthropicNative],
            ),
            (
                "bedrock",
                "bedrock/amazon.nova-pro-v1:0",
                &[Chat, ChatForward],
            ),
            (
                "bedrock",
                "bedrock/amazon.titan-embed-text-v2:0",
                &[Embeddings],
            ),
            ("bedrock", "bedrock/meta.llama3-3-70b-instruct-v1:0", &[]),
            (
                "vertex_ai",
                "vertex_ai/claude-sonnet-5-5",
                &[Chat, AnthropicNative],
            ),
            (
                "vertex",
                "vertex/claude-sonnet-5-5",
                &[Chat, AnthropicNative],
            ),
            (
                "vertex_ai",
                "vertex_ai/gemini-3.5-flash-lite",
                &[Chat, GeminiNative],
            ),
            ("vertex_ai", "vertex_ai/text-embedding-005", &[GeminiNative]),
        ];
        for (provider, name, served) in rows {
            assert!(is_cloud_provider(provider));
            for api_url in [None, Some("http://127.0.0.1:9/v1")] {
                for (auth_type, api_key) in [
                    (None, None),
                    (None, Some("AKIAUNITTEST:unit-secret")),
                    (None, Some("sk-unit-test-made-up")),
                    (Some("aws_iam"), None),
                    (Some("gcp_service_account"), None),
                ] {
                    let mut m = crate::services::llm_adapter::make_test_model(provider, name);
                    m.api_url = api_url.map(String::from);
                    m.auth_type = auth_type.map(String::from);
                    m.api_key = api_key.map(String::from);
                    m.provider_config = None;
                    for api in APIS {
                        let refusal = dispatch_refusal(&m, api).map(|r| r.error_type);
                        // No credential in `provider_config`: a served API
                        // refuses for that, every other API for itself.
                        let expected = if served.contains(&api) {
                            "provider_credential_missing"
                        } else {
                            "unsupported_provider_api"
                        };
                        assert_eq!(
                            refusal,
                            Some(expected),
                            "{provider} {name} on {api:?} (api_url {api_url:?}, auth_type \
                             {auth_type:?}, api_key {})",
                            api_key.is_some()
                        );
                    }
                }
            }
        }
        // With its own AWS credential a Bedrock row is served where its
        // backend is, and only there.
        for (provider, name, served) in rows {
            if !is_bedrock(provider) {
                continue;
            }
            let mut m = crate::services::llm_adapter::make_test_model(provider, name);
            m.api_url = None;
            m.api_key = None;
            m.auth_type = Some("aws_iam".into());
            m.provider_config = Some(
                serde_json::json!({"aws_access_key_id": "AKIAUNITTEST",
                                   "aws_secret_access_key": "unit-secret",
                                   "aws_region": "eu-west-1"})
                .to_string(),
            );
            for api in APIS {
                assert_eq!(
                    dispatch_refusal(&m, api).map(|r| r.error_type),
                    (!served.contains(&api)).then_some("unsupported_provider_api"),
                    "{provider} {name} on {api:?}"
                );
            }
        }
        // Not a cloud's row.
        for provider in ["openai", "anthropic", "gemini", "azure", "mistral"] {
            assert!(!is_cloud_provider(provider), "{provider}");
        }
        // The backstop, for a caller that skipped the check: the forwarder
        // that authenticates with an API key gets no URL it can send to and
        // no header, whatever the row's `api_url` and `api_key` are.
        for (provider, name, _) in rows {
            let mut m = crate::services::llm_adapter::make_test_model(provider, name);
            m.api_url = Some("http://127.0.0.1:9/v1".into());
            m.api_key = Some("AKIAUNITTEST:unit-secret".into());
            m.extra_headers = Some(serde_json::json!({"x-extra": "AKIAUNITTEST:unit-secret"}));
            for endpoint in ["/responses", "/completions"] {
                assert!(
                    build_url(&m, endpoint, false).starts_with(NO_UPSTREAM_SCHEME),
                    "{provider} {name} {endpoint}: {}",
                    build_url(&m, endpoint, false)
                );
                assert!(
                    build_headers_for(&m, endpoint).is_empty(),
                    "{provider} {name}"
                );
            }
        }
    }

    /// The one URL join.
    #[test]
    fn join_api_url_puts_the_path_before_the_query() {
        for (base, path, expected) in [
            ("https://h", "/v1/x", "https://h/v1/x"),
            ("https://h/", "/v1/x", "https://h/v1/x"),
            ("https://h/gw?a=1", "/v1/x", "https://h/gw/v1/x?a=1"),
            (
                "https://h/gw/?a=1&b=2",
                "/v1/x",
                "https://h/gw/v1/x?a=1&b=2",
            ),
            ("https://h/gw?", "/v1/x", "https://h/gw/v1/x"),
            (
                "https://h/gw?a=1",
                "/v1/x?o=mp3",
                "https://h/gw/v1/x?o=mp3&a=1",
            ),
            ("https://h/gw", "/v1/x?o=mp3", "https://h/gw/v1/x?o=mp3"),
            ("https://h/gw?a=1", "", "https://h/gw?a=1"),
        ] {
            assert_eq!(join_api_url(base, path), expected, "{base} + {path}");
        }
    }

    use super::*;
    use serde_json::json;

    // -----------------------------------------------------------------------
    // The admitted output ceiling, in every provider's
    // own spelling
    // -----------------------------------------------------------------------

    /// Every output-limit spelling a provider body can carry.
    const OUTPUT_LIMIT_KEYS: [&str; 6] = [
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "num_predict",
        "max_new_tokens",
        "max_length",
    ];

    /// Every provider `get_adapter` maps (one name per adapter is enough, but
    /// all of them are cheap).
    const ALL_PROVIDERS: [&str; 30] = [
        "openai",
        "anthropic",
        "gemini",
        "google_ai_studio",
        "vertex_ai",
        "cohere",
        "perplexity",
        "deepseek",
        "groq",
        "together_ai",
        "fireworks_ai",
        "deepinfra",
        "ollama",
        "lm_studio",
        "bedrock",
        "azure",
        "azure_ai",
        "mistral",
        "codestral",
        "xai",
        "moonshot",
        "minimax",
        "ai21",
        "openrouter",
        "cloudflare",
        "replicate",
        "databricks",
        "unknown_openai_compatible",
        "huggingface",
        "sagemaker",
    ];

    const MODEL_FAMILIES: [&str; 9] = [
        "gpt-4o",
        "gpt-5.5",
        "gpt-6-luna",
        "gpt-6-sol",
        "azure/gpt-5.2",
        "o3",
        "llama-3-70b",
        "claude-sonnet-4-5",
        "vicuna-13b",
    ];

    fn output_limits(body: &Value) -> Vec<(&'static str, i64)> {
        let mut out: Vec<(&'static str, i64)> = OUTPUT_LIMIT_KEYS
            .iter()
            .filter_map(|k| body.get(*k).and_then(|v| v.as_i64()).map(|v| (*k, v)))
            .collect();
        if let Some(v) = body
            .get("generationConfig")
            .and_then(|g| g.get("maxOutputTokens"))
            .and_then(|v| v.as_i64())
        {
            out.push(("generationConfig.maxOutputTokens", v));
        }
        out
    }

    fn scoped(ceiling: i64, f: impl FnOnce()) {
        let caps = crate::services::llm_governance::ParamCaps {
            output_ceiling: Some(ceiling),
            ..Default::default()
        };
        crate::services::llm_governance::EFFECTIVE_PARAM_CAPS.sync_scope(caps, f);
    }

    /// Was: the ceiling pass after translation knew three spellings. A
    /// provider whose adapter renames the limit (Ollama `num_predict`,
    /// Replicate `max_new_tokens` / `max_length`) kept a model default above
    /// the ceiling — the cap was not enforced — and got a second, stray
    /// limit beside it. Whatever path the body takes (the chat route bounds
    /// it before translation; the Responses-to-chat and text paths, and a
    /// default injected after, only after), the provider must receive
    /// exactly ONE output limit, in the spelling its adapter uses, within
    /// the ceiling.
    #[test]
    fn the_output_ceiling_reaches_every_provider_in_its_own_spelling() {
        let ceiling = 700;
        for provider in ALL_PROVIDERS {
            for name in MODEL_FAMILIES {
                // The adapter's own spelling: what it sends for a bounded
                // request when no ceiling is in force.
                let model = crate::services::llm_adapter::make_test_model(provider, name);
                let mut oracle = json!({"model": name, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 500});
                prepare_request(&mut oracle, &model);
                let native: Vec<&str> = output_limits(&oracle).iter().map(|(k, _)| *k).collect();
                assert_eq!(native.len(), 1, "{provider}/{name}: oracle {oracle}");

                // (caller limit, model default, bounded before translation)
                let cases = [
                    (None, None, true),
                    (Some(100_000), None, true),
                    (Some(50), None, true),
                    (None, None, false),
                    (None, Some(100_000), false),
                    (Some(100_000), None, false),
                ];
                for (caller, default, pre) in cases {
                    let mut model = model.clone();
                    model.default_max_tokens = default;
                    let mut req: crate::models::proxy::ChatCompletionRequest =
                        serde_json::from_value(json!({
                            "model": name,
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": caller,
                        }))
                        .unwrap();
                    if pre {
                        crate::services::llm_governance::apply_output_ceiling_to_chat(
                            &mut req, ceiling,
                        );
                    }
                    let mut body = serde_json::to_value(&req).unwrap();
                    scoped(ceiling, || prepare_request(&mut body, &model));
                    let got = output_limits(&body);
                    let case = format!(
                        "{provider}/{name} caller={caller:?} default={default:?} pre={pre}"
                    );
                    assert_eq!(got.len(), 1, "{case}: exactly one output limit: {got:?}");
                    let (key, value) = got[0];
                    assert_eq!(vec![key], native, "{case}: the adapter's spelling");
                    let want = caller.unwrap_or(ceiling).min(ceiling);
                    assert!(value <= want, "{case}: {key}={value} exceeds {want}");
                }
            }
        }
    }

    /// Create a minimal LlmModel with sensible defaults for testing.
    fn test_model() -> LlmModel {
        LlmModel {
            id: "test-id".into(),
            tenant_id: "tenant-1".into(),
            name: "Test Model".into(),
            description: None,
            usage: None,
            provider_id: "prov-openai".into(),
            provider: "openai".into(),
            provider_additional_drop_params: None,
            model_name: "gpt-4".into(),
            mode: None,
            parameters: None,
            tags: None,
            api_url_auto_discovered: None,
            api_url: None,
            auth_type: None,
            api_key: None,
            default_temperature: None,
            default_max_tokens: None,
            system_prompt: None,
            top_p: None,
            n: None,
            stop_sequences: None,
            seed: None,
            presence_penalty: None,
            frequency_penalty: None,
            logit_bias: None,
            response_format: None,
            logprobs: None,
            top_logprobs: None,
            stream_options: None,
            supports_streaming: None,
            supports_function_calling: None,
            supports_vision: None,
            tools: None,
            tool_choice: None,
            parallel_tool_calls: None,
            user_identifier: None,
            api_version: None,
            extra_headers: None,
            model_metadata: None,
            drop_params: None,
            additional_drop_params: None,
            thinking_enabled: None,
            routing_group_id: None,
            routed_via_group_id: None,
            routed_stream_timeout: None,
            routed_via_tier: None,
            routed_tier_signals: None,
            routing_weight: None,
            fallback_models: None,
            rate_limit_rpm: None,
            rate_limit_tpm: None,
            cost_per_input_token: None,
            cost_per_output_token: None,
            cost_per_image: None,
            supported_image_sizes: None,
            supported_image_qualities: None,
            max_images_per_request: None,
            cost_per_audio_char: None,
            supported_voices: None,
            supported_audio_formats: None,
            cost_per_audio_minute: None,
            max_audio_duration_seconds: None,
            supported_input_formats: None,
            supported_response_formats: None,
            cost_per_video_second: None,
            supported_video_resolutions: None,
            supported_video_durations: None,
            max_video_duration_seconds: None,
            timeout: None,
            max_concurrent_requests: None,
            allowed_fails: None,
            cooldown_time: None,
            enabled: true,
            capabilities: None,
            health_status: None,
            last_health_check: None,
            last_health_success: None,
            consecutive_health_failures: None,
            health_check_error: None,
            total_requests: None,
            failed_requests: None,
            total_tokens_consumed: None,
            avg_latency_ms: None,
            supports_batch: None,
            provider_config: None,
            credential_unreadable: false,
            credentials_opened: false,
            created_at: None,
            updated_at: None,
        }
    }

    // -----------------------------------------------------------------------
    // 1. discover_base_url
    // -----------------------------------------------------------------------

    #[test]
    fn discover_base_url_openai() {
        assert_eq!(
            discover_base_url("openai"),
            Some("https://api.openai.com/v1")
        );
    }

    #[test]
    fn discover_base_url_anthropic() {
        assert_eq!(
            discover_base_url("anthropic"),
            Some("https://api.anthropic.com/v1")
        );
    }

    /// Gemini embeddings reach Google's OpenAI-compatible endpoint
    /// with a Bearer key. The adapter always built the native
    /// `:generateContent` URL (`/v1beta/models/gemini/text-embedding-004:
    /// generateContent` — an HTML 404; a plain id answered 400 "Unknown
    /// name input"), so no Gemini embedding model could work.
    #[test]
    fn gemini_embeddings_use_the_openai_compatible_endpoint() {
        let mut m = crate::services::llm_adapter::make_test_model("gemini", "gemini-embedding-2");
        m.api_key = Some("k".into());
        m.api_url = None;
        assert_eq!(
            build_url(&m, "/embeddings", false),
            "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
        );
        let h = build_headers_for(&m, "/embeddings");
        assert!(
            h.contains(&("Authorization".into(), "Bearer k".into())),
            "{h:?}"
        );
        assert!(!h.iter().any(|(k, _)| k == "x-goog-api-key"), "{h:?}");
        // Native chat is unchanged: model-in-path URL and the native key header.
        assert!(build_url(&m, "/chat/completions", false).ends_with(":generateContent"));
        assert!(
            build_headers_for(&m, "/chat/completions")
                .contains(&("x-goog-api-key".into(), "k".into()))
        );
        // The body names the model without a provider prefix.
        let legacy =
            crate::services::llm_adapter::make_test_model("gemini", "gemini/text-embedding-004");
        let mut body = serde_json::json!({"model": "x", "input": "hi"});
        prepare_embedding(&mut body, &legacy);
        assert_eq!(body["model"], "text-embedding-004");
    }

    #[test]
    fn discover_base_url_gemini() {
        // Native Gemini REST surface. The adapter's `chat_endpoint`
        // appends the model-in-path `/v1beta/models/{name}:generate*`
        // suffix; the base URL is JUST the host.
        let url = discover_base_url("gemini");
        assert_eq!(url, Some("https://generativelanguage.googleapis.com"));
        // google_ai_studio should resolve to the same URL
        assert_eq!(discover_base_url("google_ai_studio"), url);
    }

    #[test]
    fn discover_base_url_mistral() {
        assert_eq!(
            discover_base_url("mistral"),
            Some("https://api.mistral.ai/v1")
        );
    }

    #[test]
    fn discover_base_url_unknown_returns_none() {
        assert_eq!(discover_base_url("unknown_provider"), None);
        assert_eq!(discover_base_url(""), None);
    }

    #[test]
    fn discover_base_url_all_known_providers() {
        let known = vec![
            "openai",
            "anthropic",
            "gemini",
            "google_ai_studio",
            "mistral",
            "cohere",
            "xai",
            "deepseek",
            "groq",
            "together_ai",
            "fireworks_ai",
            "perplexity",
            "voyage",
            "jina_ai",
            "elevenlabs",
            "deepgram",
            "stability",
            "fal_ai",
            "sambanova",
            "cerebras",
            "ai21",
            "deepinfra",
            "huggingface",
        ];
        for p in known {
            assert!(
                discover_base_url(p).is_some(),
                "Expected Some for provider '{p}'"
            );
        }
    }

    // -----------------------------------------------------------------------
    // 2. build_url
    // -----------------------------------------------------------------------

    #[test]
    fn build_url_with_explicit_api_url() {
        let mut m = test_model();
        m.api_url = Some("https://custom.example.com/v1".into());
        let url = build_url(&m, "/chat/completions", false);
        assert_eq!(url, "https://custom.example.com/v1/chat/completions");
    }

    #[test]
    fn build_url_explicit_api_url_trailing_slash() {
        let mut m = test_model();
        m.api_url = Some("https://custom.example.com/v1/".into());
        let url = build_url(&m, "/chat/completions", false);
        assert_eq!(url, "https://custom.example.com/v1/chat/completions");
    }

    #[test]
    fn build_url_auto_discovery() {
        let m = test_model(); // provider = "openai", api_url = None
        let url = build_url(&m, "/chat/completions", false);
        assert_eq!(url, "https://api.openai.com/v1/chat/completions");
    }

    #[tokio::test]
    async fn build_url_unknown_provider_has_no_destination() {
        // This asserted `https://api.openai.com/v1/chat/completions`,
        // the fallback that sent such a row to OpenAI.
        let mut m = test_model();
        m.provider = "totally_unknown".into();
        m.api_url = None;
        let url = build_url(&m, "/chat/completions", false);
        assert_eq!(url, "no-upstream-base://totally_unknown/chat/completions");
        // No client sends it: refused for its scheme, before any lookup.
        let sent = reqwest::Client::new().get(&url).send().await;
        assert!(sent.is_err_and(|e| e.is_builder()));
        let refusal = dispatch_refusal(&m, ProviderApi::Llm).expect("refused");
        assert_eq!(refusal.error_type, PROVIDER_API_URL_MISSING);
        assert!(
            refusal.reason.contains("totally_unknown") && refusal.reason.contains("api_url"),
            "{}",
            refusal.reason
        );
        // Its own `api_url` is its destination.
        m.api_url = Some("http://10.0.0.5:8000/v1".into());
        assert_eq!(
            build_url(&m, "/chat/completions", false),
            "http://10.0.0.5:8000/v1/chat/completions"
        );
        assert_eq!(dispatch_refusal(&m, ProviderApi::Llm), None);
    }

    /// The rows whose URL a native backend builds are exempt on the routes
    /// that backend serves, and only there.
    #[test]
    fn rows_with_a_derived_base_are_exempt_where_their_backend_serves() {
        let row = |provider: &str, name: &str| {
            let mut m = crate::services::llm_adapter::make_test_model(provider, name);
            m.api_url = None;
            m
        };
        // A Claude row on Bedrock is sent signed or not at all: the
        // row carries a (made-up) AWS credential.
        let mut claude = row("bedrock", "bedrock/global.anthropic.claude-sonnet-5-5");
        claude.provider_config = Some(
            serde_json::json!({"aws_access_key_id": "AKIAUNITTESTMADEUP01",
                               "aws_secret_access_key": "unit-test-made-up-secret"})
            .to_string(),
        );
        claude.model_metadata = Some(serde_json::json!({"aws_region": "eu-west-1"}));
        let unsigned = row("bedrock", "bedrock/global.anthropic.claude-sonnet-5-5");
        for api in [ProviderApi::Chat, ProviderApi::AnthropicNative] {
            assert_eq!(
                dispatch_refusal(&unsigned, api).map(|r| r.error_type),
                Some("provider_credential_missing"),
                "{api:?}"
            );
            let mut elsewhere = claude.clone();
            elsewhere.model_metadata =
                Some(serde_json::json!({"aws_region": "x.attacker.example/"}));
            assert_eq!(
                dispatch_refusal(&elsewhere, api).map(|r| r.error_type),
                Some("provider_endpoint_invalid"),
                "{api:?}"
            );
            // A row that names no region is not given one.
            let mut nowhere = claude.clone();
            nowhere.model_metadata = None;
            assert_eq!(
                dispatch_refusal(&nowhere, api).map(|r| r.error_type),
                Some("provider_endpoint_invalid"),
                "{api:?}"
            );
        }
        // A Vertex AI row is called with its own service account or not
        // at all: these rows carry none, so where their backend
        // serves them the refusal is the missing credential, and everywhere
        // else it is the API (the served case, with a service account, is
        // `tests/integration/cloud_identity.rs`).
        let vertex_gemini = row("vertex_ai", "gemini-3-pro");
        let vertex_claude = row("vertex_ai", "claude-sonnet-5-5");
        use ProviderApi::*;
        const SERVED: Option<&str> = None;
        const NO_CREDENTIAL: Option<&str> = Some("provider_credential_missing");
        const NOT_HERE: Option<&str> = Some("unsupported_provider_api");
        for (m, api, refusal) in [
            (&claude, Chat, SERVED),
            (&claude, AnthropicNative, SERVED),
            (&claude, Llm, NOT_HERE),
            (&claude, Batch, NOT_HERE),
            (&vertex_gemini, Chat, NO_CREDENTIAL),
            (&vertex_gemini, GeminiNative, NO_CREDENTIAL),
            (&vertex_gemini, Llm, NOT_HERE),
            (&vertex_gemini, Images, NOT_HERE),
            (&vertex_gemini, Batch, NOT_HERE),
            (&vertex_claude, Chat, NO_CREDENTIAL),
            (&vertex_claude, AnthropicNative, NO_CREDENTIAL),
            (&vertex_claude, Llm, NOT_HERE),
        ] {
            assert_eq!(
                dispatch_refusal(m, api).map(|r| r.error_type),
                refusal,
                "{} {} on {api:?}",
                m.provider,
                m.model_name
            );
        }
        // Each media API knows its own providers and no other.
        for (provider, api, known) in [
            ("openai", Images, true),
            ("openai", Speech, true),
            ("openai", Transcription, true),
            ("openai", Video, true),
            ("openai", Batch, true),
            ("deepgram", Transcription, true),
            ("deepgram", Speech, false),
            ("elevenlabs", Images, false),
            ("mistral", Images, false),
            ("anthropic", Video, false),
        ] {
            assert_eq!(
                dispatch_refusal(&row(provider, "m"), api).is_none(),
                known,
                "{provider} on {api:?}"
            );
        }
        assert_eq!(ProviderApi::of_route("/v1/chat/completions"), Chat);
        assert_eq!(ProviderApi::of_route("/v1/responses"), Llm);
        assert_eq!(ProviderApi::of_route("/completions"), Llm);
        assert_eq!(ProviderApi::of_route("/v1/embeddings"), Embeddings);
        assert_eq!(ProviderApi::of_route("/v1/messages"), AnthropicNative);
        assert_eq!(ProviderApi::of_route("/v1beta/models/g"), GeminiNative);
        assert_eq!(ProviderApi::of_route("/jobs"), Batch);
        assert_eq!(ProviderApi::of_route("/v1/proxy/llm/audio/speech"), Speech);
    }

    #[test]
    fn build_url_empty_api_url_uses_discovery() {
        let mut m = test_model();
        m.api_url = Some("".into());
        let url = build_url(&m, "/chat/completions", false);
        // Empty string should fall through to auto-discovery
        assert_eq!(url, "https://api.openai.com/v1/chat/completions");
    }

    // -----------------------------------------------------------------------
    // 3. build_headers
    // -----------------------------------------------------------------------

    #[test]
    fn build_headers_with_api_key() {
        let mut m = test_model();
        m.api_key = Some("sk-test-key".into());
        let headers = build_headers(&m);
        assert!(
            headers
                .iter()
                .any(|(k, v)| k == "Authorization" && v == "Bearer sk-test-key")
        );
    }

    #[test]
    fn build_headers_no_api_key() {
        let m = test_model();
        let headers = build_headers(&m);
        assert!(!headers.iter().any(|(k, _)| k == "Authorization"));
    }

    /// Azure reads `api-version` from the query string. (This test
    /// asserted an `api-version` header, which is what the gateway sent.)
    #[test]
    fn azure_api_version_is_a_query_parameter_not_a_header() {
        let mut m = test_model();
        m.provider = "azure".into();
        m.api_version = Some("2024-02-01".into());
        m.api_key = Some("key".into());
        m.api_url = Some("https://r.openai.azure.com/openai/deployments/d".into());
        let headers = build_headers(&m);
        assert!(!headers.iter().any(|(k, _)| k == "api-version"));
        assert_eq!(
            build_url(&m, "/chat/completions", false),
            "https://r.openai.azure.com/openai/deployments/d/chat/completions?api-version=2024-02-01"
        );
        assert_eq!(
            build_url(&m, "/embeddings", false),
            "https://r.openai.azure.com/openai/deployments/d/embeddings?api-version=2024-02-01"
        );
        // No version on the row (the v1 endpoint): no parameter.
        m.api_version = None;
        m.api_url = Some("https://r.openai.azure.com/openai/v1".into());
        assert_eq!(
            build_url(&m, "/chat/completions", false),
            "https://r.openai.azure.com/openai/v1/chat/completions"
        );
        m.api_version = Some("  ".into());
        assert!(!build_url(&m, "/chat/completions", false).contains('?'));
        // Another provider's row is left alone whatever it carries.
        m.provider = "openai".into();
        m.api_version = Some("2024-02-01".into());
        assert!(!build_url(&m, "/chat/completions", false).contains("api-version"));
    }

    #[test]
    fn the_wire_name_of_an_azure_row_has_no_catalog_prefix() {
        let mut m = test_model();
        for (provider, name, wire) in [
            ("azure", "azure/gpt-5.2", "gpt-5.2"),
            ("azure", "my-deployment", "my-deployment"),
            (
                "azure_ai",
                "azure_ai/claude-sonnet-5-5",
                "claude-sonnet-5-5",
            ),
            // Only the provider's own prefix.
            ("azure_ai", "azure/gpt-5.2", "azure/gpt-5.2"),
            ("openai", "azure/gpt-5.2", "azure/gpt-5.2"),
            (
                "openrouter",
                "anthropic/claude-haiku-5.5",
                "anthropic/claude-haiku-5.5",
            ),
        ] {
            m.provider = provider.into();
            m.model_name = name.into();
            assert_eq!(wire_model_name(&m), wire, "{provider} {name}");
            let mut body = serde_json::json!({"model_id": "x", "input": "hi"});
            prepare_responses(&mut body, &m);
            assert_eq!(body["model"], wire, "responses: {provider} {name}");
            let mut body = serde_json::json!({"model_id": "x", "input": "hi"});
            prepare_embedding(&mut body, &m);
            assert_eq!(body["model"], wire, "embeddings: {provider} {name}");
        }
    }

    #[test]
    fn build_headers_non_azure_ignores_api_version() {
        let mut m = test_model();
        m.provider = "openai".into();
        m.api_version = Some("2024-02-01".into());
        let headers = build_headers(&m);
        assert!(!headers.iter().any(|(k, _)| k == "api-version"));
    }

    #[test]
    fn build_headers_extra_headers() {
        let mut m = test_model();
        m.extra_headers = Some(json!({
            "X-Custom-Header": "custom-value",
            "X-Another": "another-value"
        }));
        let headers = build_headers(&m);
        assert!(
            headers
                .iter()
                .any(|(k, v)| k == "X-Custom-Header" && v == "custom-value")
        );
        assert!(
            headers
                .iter()
                .any(|(k, v)| k == "X-Another" && v == "another-value")
        );
    }

    // -----------------------------------------------------------------------
    // 4. drop_unsupported_params
    // -----------------------------------------------------------------------

    #[test]
    fn drop_unsupported_params_default_drop_true_strips_inherited_provider_list() {
        // drop_params=None (defaults to true). Provider-inherited list
        // is applied even when the model has no own additional_drop_params.
        let mut m = test_model();
        m.provider = "xai".into();
        m.provider_additional_drop_params = Some(json!(["stop", "frequency_penalty"]));
        let mut body = json!({
            "model": "grok-4",
            "messages": [],
            "stop": ["END"],
            "frequency_penalty": 0.5,
            "temperature": 0.7,
        });
        drop_unsupported_params(&mut body, &m);
        assert!(body.get("stop").is_none());
        assert!(body.get("frequency_penalty").is_none());
        assert_eq!(body.get("temperature"), Some(&json!(0.7)));
    }

    #[test]
    fn drop_unsupported_params_drop_false_skips_provider_keeps_model() {
        // drop_params=false is the operator opt-out for provider drops
        // (so they can probe upstream with a normally-stripped param).
        // The model row's OWN additional_drop_params is still applied
        // — that's the per-model "definitely drop this" override.
        let mut m = test_model();
        m.drop_params = Some(false);
        m.provider_additional_drop_params = Some(json!(["stop", "frequency_penalty"]));
        m.additional_drop_params = Some(json!(["debug_only"]));
        let mut body = json!({
            "stop": ["END"],
            "frequency_penalty": 0.5,
            "debug_only": true,
            "keep_me": "ok",
        });
        drop_unsupported_params(&mut body, &m);
        // Provider drops skipped:
        assert_eq!(body.get("stop"), Some(&json!(["END"])));
        assert_eq!(body.get("frequency_penalty"), Some(&json!(0.5)));
        // Model own list still applied:
        assert!(body.get("debug_only").is_none());
        assert_eq!(body.get("keep_me"), Some(&json!("ok")));
    }

    #[test]
    fn drop_unsupported_params_unions_provider_and_model_lists() {
        // Both lists union; overlapping entries are deduped (only one
        // remove() per param) — verifies the seen-set guard.
        let mut m = test_model();
        m.provider_additional_drop_params =
            Some(json!(["logit_bias", "stream_options", "logprobs"]));
        m.additional_drop_params = Some(json!(["logprobs", "presence_penalty"]));
        let mut body = json!({
            "logit_bias": {"a": 1},
            "stream_options": {"include_usage": true},
            "logprobs": true,
            "presence_penalty": 0.2,
            "keep": "yes",
        });
        drop_unsupported_params(&mut body, &m);
        assert!(body.get("logit_bias").is_none());
        assert!(body.get("stream_options").is_none());
        assert!(body.get("logprobs").is_none());
        assert!(body.get("presence_penalty").is_none());
        assert_eq!(body.get("keep"), Some(&json!("yes")));
    }

    #[test]
    fn drop_unsupported_params_provider_only_list_when_model_list_absent() {
        let mut m = test_model();
        m.provider_additional_drop_params = Some(json!(["logit_bias"]));
        m.additional_drop_params = None;
        let mut body = json!({"logit_bias": {}, "temperature": 0.5});
        drop_unsupported_params(&mut body, &m);
        assert!(body.get("logit_bias").is_none());
        assert_eq!(body.get("temperature"), Some(&json!(0.5)));
    }

    #[test]
    fn drop_unsupported_params_model_only_list_when_provider_list_absent() {
        let mut m = test_model();
        m.provider_additional_drop_params = None;
        m.additional_drop_params = Some(json!(["custom_param"]));
        let mut body = json!({"custom_param": 1, "keep": 2});
        drop_unsupported_params(&mut body, &m);
        assert!(body.get("custom_param").is_none());
        assert_eq!(body.get("keep"), Some(&json!(2)));
    }

    #[test]
    fn drop_unsupported_params_empty_arrays_are_noop() {
        let mut m = test_model();
        m.provider_additional_drop_params = Some(json!([]));
        m.additional_drop_params = Some(json!([]));
        let mut body = json!({"a": 1, "b": 2});
        let body_before = body.clone();
        drop_unsupported_params(&mut body, &m);
        assert_eq!(body, body_before);
    }

    #[test]
    fn drop_unsupported_params_skips_non_string_array_items() {
        // Defensive: a list that somehow contains a number / object
        // shouldn't panic — non-string entries are silently skipped.
        let mut m = test_model();
        m.provider_additional_drop_params = Some(json!(["stop", 42, {"x": 1}, "user"]));
        let mut body = json!({"stop": "x", "user": "u", "keep": "k"});
        drop_unsupported_params(&mut body, &m);
        assert!(body.get("stop").is_none());
        assert!(body.get("user").is_none());
        assert_eq!(body.get("keep"), Some(&json!("k")));
    }

    #[test]
    fn drop_unsupported_params_non_object_body_is_noop() {
        let m = test_model();
        let mut body = json!("not an object");
        drop_unsupported_params(&mut body, &m);
        assert_eq!(body, json!("not an object"));
    }

    #[test]
    fn drop_unsupported_params_non_array_drop_lists_are_noop() {
        // If somebody sets the column to a non-array JSON value
        // (operator typo), we treat it as no-op rather than panicking.
        let mut m = test_model();
        m.provider_additional_drop_params = Some(json!("oops not an array"));
        m.additional_drop_params = Some(json!({"oops": "not an array"}));
        let mut body = json!({"a": 1});
        let body_before = body.clone();
        drop_unsupported_params(&mut body, &m);
        assert_eq!(body, body_before);
    }

    // -----------------------------------------------------------------------
    // 5. inject_model_defaults
    // -----------------------------------------------------------------------

    /// 2026-11-08: Bedrock and Vertex ids were not recognised as Claude, so
    /// no version rule (adaptive thinking, display, sampling) applied to them.
    #[test]
    fn platform_claude_ids_are_recognised_by_their_first_party_name() {
        for (id, base) in [
            ("claude-opus-4-7", "claude-opus-4-7"),
            ("anthropic.claude-opus-4-7", "claude-opus-4-7"),
            (
                "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "claude-sonnet-4-5-20250929-v1:0",
            ),
            (
                "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                "claude-haiku-4-5-20251001-v1:0",
            ),
            (
                "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/eu.anthropic.claude-opus-4-8",
                "claude-opus-4-8",
            ),
            ("claude-opus-4-7@20260416", "claude-opus-4-7"),
            (
                "publishers/anthropic/models/claude-sonnet-4-6@20260101",
                "claude-sonnet-4-6",
            ),
            ("Claude-Opus-5", "claude-opus-5"),
        ] {
            assert_eq!(claude_base_name(id).as_deref(), Some(base), "{id}");
        }
        for id in [
            "gpt-5",
            "amazon.nova-pro-v1:0",
            "meta.llama3-70b",
            "gemini-2.5-pro",
            "",
        ] {
            assert_eq!(claude_base_name(id), None, "{id}");
        }
        assert!(anthropic_thinking_summary_is_opt_in(
            "us.anthropic.claude-opus-4-7"
        ));
        assert!(anthropic_thinking_summary_is_opt_in(
            "claude-opus-4-7@20260416"
        ));
        assert!(!anthropic_thinking_summary_is_opt_in(
            "anthropic.claude-sonnet-4-5-20250929-v1:0"
        ));
        assert!(anthropic_uses_adaptive_thinking(
            "anthropic.claude-sonnet-4-6"
        ));
        assert!(anthropic_sampling_params_retired(
            "eu.anthropic.claude-opus-4-8"
        ));
    }

    #[test]
    fn anthropic_sampling_retirement_follows_the_version_rule() {
        for name in [
            "claude-fable-5",
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-5",
            "claude-opus-5-1",
            "claude-sonnet-4-9-20270101",
        ] {
            assert!(
                anthropic_sampling_params_retired(name),
                "{name} should be retired"
            );
        }
        for name in [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-opus-4-5-20251101",
            "claude-3-5-sonnet-20241022",
            "claude-2.1",
            "gpt-5",
            "claude",
        ] {
            assert!(
                !anthropic_sampling_params_retired(name),
                "{name} should still sample"
            );
        }
    }

    #[test]
    fn the_catalogs_claude_55_ids_meet_the_version_rules_on_every_platform() {
        // The current catalog: no list to extend; the ids
        // parse. First-party, Bedrock (`global.` / `us.` profiles, the
        // Converse prefix), Vertex and Foundry forms.
        for id in [
            "claude-haiku-5-5",
            "claude-sonnet-5-5",
            "claude-opus-5-5",
            "claude-fable-5-2",
            "claude-fable-5-1",
            "bedrock/global.anthropic.claude-sonnet-5-5",
            "bedrock/global.anthropic.claude-haiku-5-5",
            "bedrock/converse/us.anthropic.claude-opus-5-5",
            "vertex_ai/claude-haiku-5-5",
            "azure_ai/claude-opus-5-5",
        ] {
            assert!(is_claude_model(id), "{id}");
            assert!(anthropic_sampling_params_retired(id), "{id}");
            assert!(anthropic_uses_adaptive_thinking(id), "{id}");
            assert!(anthropic_thinking_summary_is_opt_in(id), "{id}");
        }
    }

    #[test]
    fn responses_requests_to_gpt6_carry_no_sampling_parameters() {
        // Core item C4: the name is the fallback for a row without
        // `supports_reasoning`; the same definition as Chat Completions.
        for (provider, name) in [
            ("openai", "gpt-6-sol"),
            ("openai", "gpt-6-astra"),
            ("openai", "gpt-6.1-sol"),
            ("azure", "azure/gpt-5.2"),
        ] {
            let mut m = test_model();
            m.provider = provider.into();
            m.model_name = name.into();
            m.default_temperature = Some(0.7);
            m.top_p = Some(0.9);
            let mut body = json!({"input": "hi", "temperature": 0.2});
            prepare_responses(&mut body, &m);
            assert!(body.get("temperature").is_none(), "{name}: {body}");
            assert!(body.get("top_p").is_none(), "{name}: {body}");
        }
        // The negative: a plain model keeps its defaults.
        let mut m = test_model();
        m.model_name = "plain-chat-1".into();
        m.default_temperature = Some(0.7);
        let mut body = json!({"input": "hi"});
        prepare_responses(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.7));
    }

    #[test]
    fn retired_anthropic_model_gets_no_injected_sampling_and_loses_explicit_ones() {
        let mut m = test_model();
        m.provider = "anthropic".into();
        m.model_name = "claude-opus-5".into();
        m.default_temperature = Some(0.7);
        m.top_p = Some(0.9);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert!(
            body.get("temperature").is_none(),
            "must not inject the seed default"
        );
        assert!(body.get("top_p").is_none());

        let mut body = json!({"messages": [], "temperature": 0.2, "top_p": 0.5, "max_tokens": 8});
        drop_unsupported_params(&mut body, &m);
        assert!(
            body.get("temperature").is_none(),
            "explicit temperature is dropped"
        );
        assert!(body.get("top_p").is_none(), "explicit top_p is dropped");
        assert_eq!(body["max_tokens"], json!(8));

        // A model that still samples is untouched.
        m.model_name = "claude-haiku-4-5-20251001".into();
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.7));
    }

    #[test]
    fn inject_defaults_temperature() {
        let mut m = test_model();
        m.default_temperature = Some(0.5);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.5));
    }

    #[test]
    fn inject_defaults_does_not_override_caller_temperature() {
        let mut m = test_model();
        m.default_temperature = Some(0.5);
        let mut body = json!({"messages": [], "temperature": 0.9});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.9));
    }

    #[test]
    fn inject_defaults_treats_null_temperature_as_unset() {
        // OpenAI SDKs / the portal / our own ChatCompletionRequest
        // re-serialization send omitted params as explicit `null`. A
        // null-valued key must NOT block the model default.
        let mut m = test_model();
        m.default_temperature = Some(0.5);
        let mut body = json!({"messages": [], "temperature": null});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.5));
    }

    #[test]
    fn inject_defaults_treats_null_params_as_unset_across_fields() {
        // The full set of params an OpenAI client nulls out — every one
        // must fall back to the model default, not be skipped.
        let mut m = test_model();
        m.default_temperature = Some(0.3);
        m.top_p = Some(0.8);
        m.n = Some(2);
        m.default_max_tokens = Some(256);
        let mut body = json!({
            "messages": [],
            "temperature": null,
            "top_p": null,
            "n": null,
            "max_tokens": null,
            "stop": null,
        });
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.3));
        assert_eq!(body["top_p"], json!(0.8));
        assert_eq!(body["n"], json!(2));
        assert_eq!(body["max_tokens"], json!(256));
    }

    #[test]
    fn inject_defaults_max_tokens() {
        let mut m = test_model();
        m.default_max_tokens = Some(1024);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["max_tokens"], json!(1024));
    }

    #[test]
    fn inject_defaults_max_tokens_skipped_if_max_completion_tokens_present() {
        let mut m = test_model();
        m.default_max_tokens = Some(1024);
        let mut body = json!({"messages": [], "max_completion_tokens": 512});
        inject_model_defaults(&mut body, &m);
        assert!(body.get("max_tokens").is_none());
    }

    #[test]
    fn inject_defaults_top_p_and_penalties() {
        let mut m = test_model();
        m.top_p = Some(0.95);
        m.presence_penalty = Some(0.1);
        m.frequency_penalty = Some(0.2);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["top_p"], json!(0.95));
        assert_eq!(body["presence_penalty"], json!(0.1));
        assert_eq!(body["frequency_penalty"], json!(0.2));
    }

    #[test]
    fn inject_defaults_seed() {
        let mut m = test_model();
        m.seed = Some(42);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["seed"], json!(42));
    }

    #[test]
    fn inject_defaults_logprobs_true_only() {
        let mut m = test_model();
        m.logprobs = Some(true);
        m.top_logprobs = Some(5);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["logprobs"], json!(true));
        assert_eq!(body["top_logprobs"], json!(5));
    }

    #[test]
    fn inject_defaults_logprobs_false_not_injected() {
        let mut m = test_model();
        m.logprobs = Some(false);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert!(body.get("logprobs").is_none());
    }

    #[test]
    fn inject_defaults_tools_and_tool_choice() {
        let mut m = test_model();
        m.tools = Some(json!([{"type": "function", "function": {"name": "get_weather"}}]));
        m.tool_choice = Some("auto".into());
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert!(body["tools"].is_array());
        assert_eq!(body["tool_choice"], json!("auto"));
    }

    #[test]
    fn inject_defaults_parallel_tool_calls_only_when_false() {
        let mut m = test_model();
        m.parallel_tool_calls = Some(false);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["parallel_tool_calls"], json!(false));
    }

    #[test]
    fn inject_defaults_parallel_tool_calls_true_not_injected() {
        let mut m = test_model();
        m.parallel_tool_calls = Some(true);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert!(body.get("parallel_tool_calls").is_none());
    }

    #[test]
    fn inject_defaults_n_only_when_greater_than_1() {
        let mut m = test_model();
        m.n = Some(1);
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert!(body.get("n").is_none());

        m.n = Some(3);
        let mut body2 = json!({"messages": []});
        inject_model_defaults(&mut body2, &m);
        assert_eq!(body2["n"], json!(3));
    }

    #[test]
    fn inject_defaults_stop_sequences() {
        let mut m = test_model();
        m.stop_sequences = Some(json!(["END", "STOP"]));
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["stop"], json!(["END", "STOP"]));
    }

    #[test]
    fn inject_defaults_response_format() {
        let mut m = test_model();
        m.response_format = Some(json!({"type": "json_object"}));
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["response_format"], json!({"type": "json_object"}));
    }

    #[test]
    fn inject_defaults_user_identifier() {
        let mut m = test_model();
        m.user_identifier = Some("user-123".into());
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["user"], json!("user-123"));
    }

    #[test]
    fn inject_defaults_stream_options() {
        let mut m = test_model();
        m.stream_options = Some(json!({"include_usage": true}));
        let mut body = json!({"messages": []});
        inject_model_defaults(&mut body, &m);
        assert_eq!(body["stream_options"], json!({"include_usage": true}));
    }

    #[test]
    fn inject_defaults_no_model_defaults_leaves_body_unchanged() {
        let m = test_model(); // all defaults are None
        let mut body = json!({"messages": [{"role": "user", "content": "hi"}]});
        let body_before = body.clone();
        inject_model_defaults(&mut body, &m);
        assert_eq!(body, body_before);
    }

    #[test]
    fn inject_defaults_non_object_is_noop() {
        let m = test_model();
        let mut body = json!(42);
        inject_model_defaults(&mut body, &m);
        assert_eq!(body, json!(42));
    }

    // -----------------------------------------------------------------------
    // 6. inject_system_prompt
    // -----------------------------------------------------------------------

    #[test]
    fn inject_system_prompt_prepends_when_no_system() {
        let mut m = test_model();
        m.system_prompt = Some("You are helpful.".into());
        let mut body = json!({
            "messages": [
                {"role": "user", "content": "Hello"}
            ]
        });
        inject_system_prompt(&mut body, &m);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[0]["content"], "You are helpful.");
        assert_eq!(msgs[1]["role"], "user");
    }

    #[test]
    fn inject_system_prompt_merges_with_caller_system() {
        // server's `system_prompt` must MERGE with any
        // caller-supplied system message, not skip. Server's prompt
        // goes first (stable, cacheable prefix); caller's follows.
        let mut m = test_model();
        m.system_prompt = Some("You are helpful.".into());
        let mut body = json!({
            "messages": [
                {"role": "system", "content": "Caller-supplied system prompt"},
                {"role": "user", "content": "Hello"}
            ]
        });
        inject_system_prompt(&mut body, &m);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(
            msgs.len(),
            3,
            "merge must produce server-system + caller-system + user"
        );
        // Server's prompt at index 0 — the stable, cacheable prefix.
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[0]["content"], "You are helpful.");
        // Caller's system at index 1 — preserved verbatim, NOT dropped.
        assert_eq!(msgs[1]["role"], "system");
        assert_eq!(msgs[1]["content"], "Caller-supplied system prompt");
        // User message at index 2.
        assert_eq!(msgs[2]["role"], "user");
    }

    #[test]
    fn inject_system_prompt_merges_with_multiple_caller_systems() {
        // Direct API callers occasionally pass multiple system
        // messages (e.g. role + persona + constraints). Each must
        // survive the merge unchanged, still in their original order,
        // all positioned AFTER the server's prompt.
        let mut m = test_model();
        m.system_prompt = Some("[SERVER] core policy".into());
        let mut body = json!({
            "messages": [
                {"role": "system", "content": "[A] persona"},
                {"role": "system", "content": "[B] constraints"},
                {"role": "user", "content": "Hello"}
            ]
        });
        inject_system_prompt(&mut body, &m);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 4);
        assert_eq!(msgs[0]["content"], "[SERVER] core policy");
        assert_eq!(msgs[1]["content"], "[A] persona");
        assert_eq!(msgs[2]["content"], "[B] constraints");
        assert_eq!(msgs[3]["role"], "user");
    }

    #[test]
    fn inject_system_prompt_skips_when_no_system_prompt_configured() {
        let m = test_model(); // system_prompt = None
        let mut body = json!({
            "messages": [{"role": "user", "content": "Hello"}]
        });
        let body_before = body.clone();
        inject_system_prompt(&mut body, &m);
        assert_eq!(body, body_before);
    }

    #[test]
    fn inject_system_prompt_skips_empty_system_prompt() {
        let mut m = test_model();
        m.system_prompt = Some("".into());
        let mut body = json!({
            "messages": [{"role": "user", "content": "Hello"}]
        });
        let body_before = body.clone();
        inject_system_prompt(&mut body, &m);
        assert_eq!(body, body_before);
    }

    #[test]
    fn inject_system_prompt_no_messages_key_is_noop() {
        let mut m = test_model();
        m.system_prompt = Some("You are helpful.".into());
        let mut body = json!({"prompt": "Hello"});
        let body_before = body.clone();
        inject_system_prompt(&mut body, &m);
        assert_eq!(body, body_before);
    }

    #[test]
    fn inject_system_prompt_empty_messages_array() {
        let mut m = test_model();
        m.system_prompt = Some("You are helpful.".into());
        let mut body = json!({"messages": []});
        inject_system_prompt(&mut body, &m);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["role"], "system");
    }

    // -----------------------------------------------------------------------
    // 7. prepare_request
    // -----------------------------------------------------------------------

    #[test]
    fn prepare_request_sets_model_name() {
        let m = test_model();
        let mut body = json!({
            "model": "some-alias",
            "model_id": "id-to-remove",
            "extra": "also-remove",
            "messages": [{"role": "user", "content": "hi"}]
        });
        prepare_request(&mut body, &m);
        assert_eq!(body["model"], "gpt-4");
        assert!(body.get("model_id").is_none());
        assert!(body.get("extra").is_none());
    }

    /// The route's omit-reasoning note is for the Anthropic adapter; a body
    /// bound anywhere else (a fallback to another provider) never carries it.
    #[test]
    fn prepare_request_never_sends_the_omit_reasoning_note() {
        let m = test_model();
        let mut body = json!({
            OMIT_REASONING_MARKER: true,
            "messages": [{"role": "user", "content": "hi"}]
        });
        prepare_request(&mut body, &m);
        assert!(body.get(OMIT_REASONING_MARKER).is_none());
    }

    #[test]
    fn prepare_request_injects_defaults_and_system_prompt() {
        let mut m = test_model();
        m.default_temperature = Some(0.3);
        m.system_prompt = Some("Be concise.".into());
        let mut body = json!({
            "messages": [{"role": "user", "content": "hi"}]
        });
        prepare_request(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.3));
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs[0]["role"], "system");
        assert_eq!(msgs[0]["content"], "Be concise.");
    }

    #[test]
    fn prepare_request_full_pipeline() {
        let mut m = test_model();
        m.default_temperature = Some(0.7);
        m.default_max_tokens = Some(500);
        m.system_prompt = Some("You are a test bot.".into());
        m.drop_params = Some(false);
        m.additional_drop_params = Some(json!(["custom_field"]));

        let mut body = json!({
            "model": "alias",
            "model_id": "xxx",
            "messages": [{"role": "user", "content": "test"}],
            "custom_field": "should be dropped"
        });
        prepare_request(&mut body, &m);

        assert_eq!(body["model"], "gpt-4");
        assert!(body.get("model_id").is_none());
        assert!(body.get("custom_field").is_none());
        assert_eq!(body["temperature"], json!(0.7));
        assert_eq!(body["max_tokens"], json!(500));
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs[0]["role"], "system");
    }

    #[test]
    fn prepare_request_strips_top_level_nulls() {
        // Regression for the Gemini health-check bug:
        // Google's OpenAI-compat endpoint rejects a request with
        // ``INVALID_ARGUMENT: Value is not a string: null`` the moment
        // it sees any top-level ``null`` value (temperature: null,
        // tools: null, top_p: null, …). The Rust route handler
        // serializes every unset Option<T> field as ``null`` when
        // re-encoding the body before forwarding upstream, so plain
        // health-check requests routinely contained 8+ nulls. The
        // common-cleanup step in prepare_request must strip them so
        // every provider gets a clean body.
        let m = test_model(); // provider = "openai"
        let mut body = json!({
            "model": "alias",
            "messages": [{"role": "user", "content": "ping"}],
            "temperature": null,
            "max_tokens": 5,
            "top_p": null,
            "n": null,
            "stop": null,
            "presence_penalty": null,
            "frequency_penalty": null,
            "tools": null,
            "tool_choice": null,
            "stream": null,
        });
        prepare_request(&mut body, &m);
        let obj = body.as_object().expect("body must remain an object");
        for null_key in [
            "top_p",
            "n",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "tools",
            "tool_choice",
            "stream",
        ] {
            assert!(
                !obj.contains_key(null_key),
                "top-level null `{null_key}` must be stripped before upstream call",
            );
        }
        // Non-null values survive untouched.
        assert_eq!(obj.get("max_tokens"), Some(&json!(5)));
        // The model row's default_temperature isn't set in this test,
        // so the original null temperature should still get stripped
        // (no inject would have replaced it).
        assert!(!obj.contains_key("temperature"));
    }

    #[test]
    fn prepare_request_does_not_strip_nested_nulls() {
        // Stripping must operate at the top level only — a null
        // inside `messages[i].name` (Anthropic-style optional field)
        // or `tools[i].function.parameters.properties.x.default` is
        // legitimate and should not be touched. We don't recursively
        // descend.
        let m = test_model();
        let mut body = json!({
            "model": "alias",
            "messages": [
                {"role": "user", "content": "ping", "name": null},
            ],
        });
        prepare_request(&mut body, &m);
        let msg = &body["messages"][0];
        assert_eq!(msg["name"], json!(null), "nested null must survive");
    }

    // -----------------------------------------------------------------------
    // 8. prepare_text_completion
    // -----------------------------------------------------------------------

    #[test]
    fn prepare_text_completion_openai_keeps_text_format() {
        let m = test_model(); // provider = "openai"
        let mut body = json!({
            "model": "alias",
            "prompt": "Once upon a time",
            "echo": true,
            "best_of": 2,
        });
        let use_chat = prepare_text_completion(&mut body, &m);
        assert!(!use_chat, "OpenAI should keep text format");
        assert_eq!(body["model"], "gpt-4");
        // prompt should still be present for OpenAI
        assert!(body.get("prompt").is_some());
    }

    #[test]
    fn prepare_text_completion_non_openai_converts_to_chat() {
        let mut m = test_model();
        m.provider = "anthropic".into();
        m.model_name = "claude-3-sonnet".into();
        let mut body = json!({
            "model": "alias",
            "prompt": "Once upon a time",
            "echo": true,
            "best_of": 2,
            "suffix": ".end",
        });
        let use_chat = prepare_text_completion(&mut body, &m);
        assert!(use_chat, "Non-OpenAI should convert to chat format");
        assert_eq!(body["model"], "claude-3-sonnet");
        // prompt should be converted to messages
        assert!(body.get("prompt").is_none());
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs[0]["role"], "user");
        assert_eq!(msgs[0]["content"], "Once upon a time");
        // Text-completion-only params should be removed
        assert!(body.get("echo").is_none());
        assert!(body.get("best_of").is_none());
        assert!(body.get("suffix").is_none());
    }

    #[test]
    fn prepare_text_completion_prompt_array_join() {
        let mut m = test_model();
        m.provider = "mistral".into();
        m.model_name = "mistral-large".into();
        let mut body = json!({
            "prompt": ["Hello ", "world"],
        });
        let use_chat = prepare_text_completion(&mut body, &m);
        assert!(use_chat);
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs[0]["content"], "Hello world");
    }

    #[test]
    fn prepare_text_completion_azure_keeps_text_format() {
        let mut m = test_model();
        m.provider = "azure".into();
        let mut body = json!({"prompt": "test"});
        let use_chat = prepare_text_completion(&mut body, &m);
        assert!(!use_chat);
    }

    // -----------------------------------------------------------------------
    // 9. prepare_embedding
    // -----------------------------------------------------------------------

    #[test]
    fn prepare_embedding_sets_model_and_cleans_nulls() {
        let m = test_model();
        let mut body = json!({
            "model": "alias",
            "model_id": "xxx",
            "input": "hello",
            "unused": null,
        });
        prepare_embedding(&mut body, &m);
        assert_eq!(body["model"], "gpt-4");
        assert!(body.get("model_id").is_none());
        assert!(body.get("unused").is_none());
    }

    #[test]
    fn prepare_embedding_openai_text_embedding_3_keeps_dimensions() {
        let mut m = test_model();
        m.model_name = "text-embedding-3-small".into();
        let mut body = json!({
            "input": "hello",
            "dimensions": 256,
        });
        prepare_embedding(&mut body, &m);
        assert_eq!(body["dimensions"], json!(256));
    }

    #[test]
    fn prepare_embedding_openai_non_embedding3_drops_dimensions() {
        let mut m = test_model();
        m.model_name = "text-embedding-ada-002".into();
        let mut body = json!({
            "input": "hello",
            "dimensions": 256,
        });
        prepare_embedding(&mut body, &m);
        assert!(body.get("dimensions").is_none());
    }

    #[test]
    fn prepare_embedding_unknown_provider_drops_dimensions() {
        let mut m = test_model();
        m.provider = "groq".into();
        m.model_name = "some-embed".into();
        let mut body = json!({"input": "hello", "dimensions": 512});
        prepare_embedding(&mut body, &m);
        assert!(body.get("dimensions").is_none());
    }

    #[test]
    fn prepare_embedding_voyage_keeps_dimensions() {
        let mut m = test_model();
        m.provider = "voyage".into();
        m.model_name = "voyage-large-2".into();
        let mut body = json!({"input": "hello", "dimensions": 1024});
        prepare_embedding(&mut body, &m);
        assert_eq!(body["dimensions"], json!(1024));
    }

    #[test]
    fn prepare_embedding_cohere_encoding_format_to_embedding_types() {
        let mut m = test_model();
        m.provider = "cohere".into();
        m.model_name = "embed-english-v3.0".into();
        let mut body = json!({
            "input": "hello",
            "encoding_format": "float",
        });
        prepare_embedding(&mut body, &m);
        assert!(body.get("encoding_format").is_none());
        assert_eq!(body["embedding_types"], json!(["float"]));
    }

    #[test]
    fn prepare_embedding_openai_keeps_encoding_format() {
        let m = test_model();
        let mut body = json!({
            "input": "hello",
            "encoding_format": "base64",
        });
        prepare_embedding(&mut body, &m);
        assert_eq!(body["encoding_format"], json!("base64"));
    }

    #[test]
    fn prepare_embedding_other_provider_drops_encoding_format() {
        let mut m = test_model();
        m.provider = "mistral".into();
        let mut body = json!({
            "input": "hello",
            "encoding_format": "float",
        });
        prepare_embedding(&mut body, &m);
        assert!(body.get("encoding_format").is_none());
    }

    #[test]
    fn prepare_embedding_jina_keeps_dimensions() {
        let mut m = test_model();
        m.provider = "jina_ai".into();
        m.model_name = "jina-embeddings-v3".into();
        let mut body = json!({"input": "hello", "dimensions": 768});
        prepare_embedding(&mut body, &m);
        assert_eq!(body["dimensions"], json!(768));
    }

    // -----------------------------------------------------------------------
    // 10. prepare_responses
    // -----------------------------------------------------------------------

    #[test]
    fn prepare_responses_sets_model_name() {
        let m = test_model();
        let mut body = json!({
            "model": "alias",
            "model_id": "xxx",
            "extra": "remove",
            "input": "Hello",
        });
        prepare_responses(&mut body, &m);
        assert_eq!(body["model"], "gpt-4");
        assert!(body.get("model_id").is_none());
        assert!(body.get("extra").is_none());
    }

    #[test]
    fn prepare_responses_injects_instructions_from_system_prompt() {
        let mut m = test_model();
        m.system_prompt = Some("Be brief.".into());
        let mut body = json!({"input": "Hello"});
        prepare_responses(&mut body, &m);
        assert_eq!(body["instructions"], "Be brief.");
    }

    #[test]
    fn prepare_responses_does_not_override_existing_instructions() {
        let mut m = test_model();
        m.system_prompt = Some("Be brief.".into());
        let mut body = json!({
            "input": "Hello",
            "instructions": "Already set"
        });
        prepare_responses(&mut body, &m);
        assert_eq!(body["instructions"], "Already set");
    }

    #[test]
    fn prepare_responses_injects_defaults() {
        let mut m = test_model();
        m.default_temperature = Some(0.4);
        m.default_max_tokens = Some(2000);
        m.top_p = Some(0.9);
        let mut body = json!({"input": "Hello"});
        prepare_responses(&mut body, &m);
        assert_eq!(body["temperature"], json!(0.4));
        assert_eq!(body["max_output_tokens"], json!(2000));
        assert_eq!(body["top_p"], json!(0.9));
    }

    #[test]
    fn prepare_responses_does_not_override_caller_values() {
        let mut m = test_model();
        m.default_temperature = Some(0.4);
        m.default_max_tokens = Some(2000);
        let mut body = json!({
            "input": "Hello",
            "temperature": 1.0,
            "max_output_tokens": 100
        });
        prepare_responses(&mut body, &m);
        assert_eq!(body["temperature"], json!(1.0));
        assert_eq!(body["max_output_tokens"], json!(100));
    }

    #[test]
    fn prepare_responses_injects_tools() {
        let mut m = test_model();
        m.tools = Some(json!([{"type": "web_search"}]));
        let mut body = json!({"input": "Hello"});
        prepare_responses(&mut body, &m);
        assert_eq!(body["tools"], json!([{"type": "web_search"}]));
    }

    #[test]
    fn prepare_responses_removes_nulls() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "temperature": null,
            "metadata": null,
        });
        prepare_responses(&mut body, &m);
        assert!(body.get("temperature").is_none());
        assert!(body.get("metadata").is_none());
        assert!(body.get("input").is_some());
    }

    #[test]
    fn prepare_responses_empty_system_prompt_not_injected() {
        let mut m = test_model();
        m.system_prompt = Some("".into());
        let mut body = json!({"input": "Hello"});
        prepare_responses(&mut body, &m);
        assert!(body.get("instructions").is_none());
    }

    #[test]
    fn prepare_responses_flattens_chat_completions_function_tools() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "tools": [{
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search the docs",
                    "parameters": {"type": "object", "properties": {}}
                }
            }]
        });
        prepare_responses(&mut body, &m);
        let tool = &body["tools"][0];
        assert_eq!(tool["type"], "function");
        assert_eq!(tool["name"], "search");
        assert_eq!(tool["description"], "Search the docs");
        assert!(tool["parameters"].is_object());
        assert!(tool.get("function").is_none(), "wrapper must be removed");
    }

    #[test]
    fn prepare_responses_passes_through_already_flat_function_tools() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "tools": [{
                "type": "function",
                "name": "search",
                "description": "Search the docs",
                "parameters": {"type": "object", "properties": {}}
            }]
        });
        prepare_responses(&mut body, &m);
        let tool = &body["tools"][0];
        assert_eq!(tool["name"], "search");
        assert!(tool.get("function").is_none());
    }

    #[test]
    fn prepare_responses_leaves_non_function_tool_types_untouched() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "tools": [
                {"type": "file_search"},
                {"type": "web_search"},
                {"type": "code_interpreter", "container": "auto"}
            ]
        });
        prepare_responses(&mut body, &m);
        let arr = body["tools"].as_array().unwrap();
        assert_eq!(arr.len(), 3);
        assert_eq!(arr[0]["type"], "file_search");
        assert_eq!(arr[1]["type"], "web_search");
        assert_eq!(arr[2]["type"], "code_interpreter");
        assert_eq!(arr[2]["container"], "auto");
    }

    #[test]
    fn prepare_responses_flattens_function_tool_choice() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "tool_choice": {
                "type": "function",
                "function": {"name": "search"}
            }
        });
        prepare_responses(&mut body, &m);
        let choice = &body["tool_choice"];
        assert_eq!(choice["type"], "function");
        assert_eq!(choice["name"], "search");
        assert!(choice.get("function").is_none());
    }

    #[test]
    fn prepare_responses_string_tool_choice_passes_through() {
        let m = test_model();
        let mut body = json!({
            "input": "Hello",
            "tool_choice": "auto"
        });
        prepare_responses(&mut body, &m);
        assert_eq!(body["tool_choice"], "auto");
    }

    #[test]
    fn prepare_responses_function_wrapper_does_not_overwrite_top_level_keys() {
        let m = test_model();
        // Defensive: if a caller hands us BOTH a top-level `name`
        // AND a wrapper (malformed but plausible from naive
        // serialisers), the explicit top-level value wins.
        let mut body = json!({
            "input": "Hello",
            "tools": [{
                "type": "function",
                "name": "outer",
                "function": {
                    "name": "inner",
                    "description": "should not clobber outer"
                }
            }]
        });
        prepare_responses(&mut body, &m);
        let tool = &body["tools"][0];
        assert_eq!(tool["name"], "outer");
        assert_eq!(tool["description"], "should not clobber outer");
        assert!(tool.get("function").is_none());
    }

    // -----------------------------------------------------------------------
    // prepare_request — governance caps bind model-injected defaults
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn prepare_request_clamps_injected_max_tokens_default_to_cap() {
        // A caller who omits max_tokens gets the model's default (4096)
        // injected; with a 2000 output ceiling scoped, prepare_request
        // must lower it to 2000.
        let mut m = test_model();
        m.default_max_tokens = Some(4096);
        m.default_temperature = Some(1.0);

        let caps = crate::services::llm_governance::ParamCaps {
            max_output_tokens: Some(2000),
            max_temperature: Some(0.7),
            output_ceiling: None,
        };
        let mut body = json!({ "messages": [], "model": "gpt-4" });
        crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
            .scope(caps, async {
                prepare_request(&mut body, &m);
            })
            .await;
        assert_eq!(body["max_tokens"], json!(2000));
        assert_eq!(body["temperature"], json!(0.7));
    }

    #[tokio::test]
    async fn prepare_request_clamps_anthropic_adapter_fallback_max_tokens() {
        // Anthropic's adapter fills max_tokens from
        // `default_max_tokens.unwrap_or(4096)` during transform — AFTER
        // inject_model_defaults — so the clamp must run at the end of
        // prepare_request to bind it. Model has NO default_max_tokens,
        // so the 4096 fallback is what we expect the cap to lower.
        let mut m = test_model();
        m.provider = "anthropic".into();
        m.model_name = "claude-haiku".into();
        m.default_max_tokens = None;

        let caps = crate::services::llm_governance::ParamCaps {
            max_output_tokens: Some(512),
            max_temperature: None,
            output_ceiling: None,
        };
        let mut body = json!({
            "messages": [{"role": "user", "content": "hi"}],
            "model": "claude-haiku",
        });
        crate::services::llm_governance::EFFECTIVE_PARAM_CAPS
            .scope(caps, async {
                prepare_request(&mut body, &m);
            })
            .await;
        // Anthropic keeps the `max_tokens` key; it must be clamped to 512.
        assert_eq!(
            body["max_tokens"],
            json!(512),
            "anthropic adapter fallback max_tokens escaped the cap"
        );
    }

    #[tokio::test]
    async fn prepare_request_without_scope_leaves_defaults_unclamped() {
        // No governance scope (route that doesn't run LLM governance) →
        // the model default flows through untouched. Proves the clamp is
        // opt-in via the task-local and can't regress un-governed paths.
        let mut m = test_model();
        m.default_max_tokens = Some(4096);
        let mut body = json!({ "messages": [], "model": "gpt-4" });
        prepare_request(&mut body, &m);
        assert_eq!(body["max_tokens"], json!(4096));
    }
}

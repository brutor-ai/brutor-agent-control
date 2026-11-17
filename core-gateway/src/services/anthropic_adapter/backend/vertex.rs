// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Google Vertex AI Backend
//!
//! Routes Anthropic Messages format requests to Google Vertex AI.
//! Handles:
//! - `:rawPredict` / `:streamRawPredict` endpoint construction
//! - Google OAuth Bearer token authentication via `gcp_auth` crate
//!   (the row's service account key, token caching)
//! - Beta header stripping (Vertex rejects unknown beta flags)
//! - Model name mapping for Vertex endpoint format
//!
//! ## Authentication
//!
//! A Vertex AI row is called with the service account the row carries, and
//! with no other identity. The gateway process's
//! own Google identity (`GOOGLE_APPLICATION_CREDENTIALS`, the gcloud
//! configuration, the metadata server of the node it runs on: Application
//! Default Credentials) is the operator's; a row without a credential used
//! it, so any tenant able to create a model row acted as the operator in
//! Google Cloud. Such a row is refused (`provider_credential_missing`) and
//! this module never asks `gcp_auth` for the ambient identity.
//!
//! The `gcp_auth` crate caches and refreshes the token of each service
//! account.

use std::sync::Arc;

use serde_json::{Value, json};

use super::{BackendAdapter, BetaFilter};
use crate::models::db::LlmModel;

// ---------------------------------------------------------------------------
// Vertex AI token management
// ---------------------------------------------------------------------------

/// What the gateway keeps per service account, keyed by the SHA-256 of the
/// service-account JSON a row carries.
#[derive(Default)]
struct KnownAccounts {
    /// The token providers, one per credential a token was asked for.
    /// `gcp_auth` caches and refreshes the token inside each provider.
    /// There is no entry for the process's own identity.
    ///
    /// There was one provider for the whole process, the first one built:
    /// every later Vertex row, of any tenant, was
    /// called with the first row's Google access token.
    providers: std::collections::HashMap<[u8; 32], Arc<dyn gcp_auth::TokenProvider>>,
    /// The accounts the dispatch check has parsed and found to be service
    /// accounts: their hashes, and nothing of the accounts themselves. A
    /// parsed account is kept only once a token is asked for, as before.
    parsed: std::collections::HashSet<[u8; 32]>,
}

/// Parsing a service account is not cheap (`gcp_auth` builds an HTTPS
/// client for each: 73 ms measured), and a row's account was parsed at
/// dispatch, to see that it is one, and again at token time, on every
/// request. Both ask here first: an account is
/// parsed once for the dispatch check and once for its token provider, per
/// credential. An account that does not parse is not remembered. The lock
/// is never held across an `await`.
static KNOWN_ACCOUNTS: std::sync::LazyLock<std::sync::RwLock<KnownAccounts>> =
    std::sync::LazyLock::new(Default::default);

/// More entries than this, of either kind, and both are emptied: an account
/// is parsed again from the row on its next call.
const MAX_KNOWN_ACCOUNTS: usize = 1024;

impl KnownAccounts {
    fn read() -> std::sync::RwLockReadGuard<'static, KnownAccounts> {
        KNOWN_ACCOUNTS
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// The map to add an entry to, emptied first when it is full.
    fn write() -> std::sync::RwLockWriteGuard<'static, KnownAccounts> {
        let mut known = KNOWN_ACCOUNTS
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if known.providers.len().max(known.parsed.len()) >= MAX_KNOWN_ACCOUNTS {
            *known = KnownAccounts::default();
        }
        known
    }
}

/// The service account a row carries, as JSON, with its hash:
/// `provider_config`'s `gcp_credentials_json`, and nothing else. `None` for
/// a row that carries none. A row's `api_key` is not read: the base64
/// service account it once carried is not a credential of a Vertex AI row
/// (the same string was sent in an API-key header on
/// the routes the generic forwarder served).
fn row_account(model: &LlmModel) -> Option<([u8; 32], String)> {
    use sha2::{Digest, Sha256};
    let account = model
        .provider_config
        .as_deref()
        .and_then(|json| {
            serde_json::from_str::<crate::services::cloud_auth::ProviderCredentials>(json).ok()
        })
        .and_then(|creds| creds.gcp_credentials_json)?;
    Some((Sha256::digest(account.as_bytes()).into(), account))
}

/// A service account parsed into its token provider.
fn parse_account(account: &str) -> Option<Arc<dyn gcp_auth::TokenProvider>> {
    // `gcp_auth` builds its HTTPS client when it parses a service account.
    ensure_tls_crypto_provider();
    let provider = gcp_auth::CustomServiceAccount::from_json(account).ok()?;
    Some(Arc::new(provider))
}

/// True for a row that carries a service account of its own (the dispatch
/// check). Nothing of the account is kept.
fn row_has_a_service_account(model: &LlmModel) -> bool {
    let Some((key, account)) = row_account(model) else {
        return false;
    };
    {
        let known = KnownAccounts::read();
        if known.parsed.contains(&key) || known.providers.contains_key(&key) {
            return true;
        }
    }
    if parse_account(&account).is_none() {
        return false;
    }
    KnownAccounts::write().parsed.insert(key);
    true
}

/// The token provider of the service account a row carries, kept per
/// credential; `None` for a row that carries no service account.
fn row_token_provider(model: &LlmModel) -> Option<Arc<dyn gcp_auth::TokenProvider>> {
    let (key, account) = row_account(model)?;
    let cached = KnownAccounts::read().providers.get(&key).cloned();
    if cached.is_some() {
        return cached;
    }
    let provider = parse_account(&account)?;
    KnownAccounts::write()
        .providers
        .insert(key, provider.clone());
    Some(provider)
}

/// The message of a Vertex AI row that carries no service account.
pub(crate) fn missing_credential(model: &LlmModel) -> String {
    format!(
        "provider_credential_missing: model '{}' (provider '{}') has no Google service account \
         of its own. A Vertex AI request is authenticated with the model's own service account \
         (the model's cloud credentials, `gcp_credentials_json`), never with the gateway's own \
         Google identity. Nothing was sent.",
        model.model_name, model.provider
    )
}

/// Install the process's rustls crypto provider, once, before `gcp_auth`
/// builds its HTTPS client. The dependency graph
/// enables both of rustls's providers (`ring` through reqwest and sqlx,
/// `aws-lc-rs` through hyper-rustls and the AWS SDK), so rustls cannot choose
/// one by itself, and `gcp_auth`, which names none, panicked on its first
/// token request: no Claude model on Vertex AI could be called. Every other
/// TLS client of the gateway names its provider. A provider that is already
/// installed stays.
fn ensure_tls_crypto_provider() {
    static INSTALL: std::sync::Once = std::sync::Once::new();
    INSTALL.call_once(|| {
        // Err: another part of the process installed one first. That one stands.
        let _installed = rustls::crypto::aws_lc_rs::default_provider().install_default();
    });
}

/// A Google access token for a Vertex AI row: the token of the service
/// account the row carries. A row that carries none gets an error and no
/// token; there is no other identity to fall back to. Such a row is
/// refused at dispatch (`providers::dispatch_refusal`); this is the same
/// rule where the token is made.
pub async fn get_access_token(model: &LlmModel) -> Result<String, String> {
    let Some(provider) = row_token_provider(model) else {
        return Err(missing_credential(model));
    };

    // Get token with Vertex AI scope
    let scopes = &["https://www.googleapis.com/auth/cloud-platform"];
    let token = provider
        .token(scopes)
        .await
        .map_err(|e| format!("Failed to get GCP access token: {e}"))?;

    Ok(token.as_str().to_string())
}

// ---------------------------------------------------------------------------
// VertexBackend
// ---------------------------------------------------------------------------

pub struct VertexBackend;

impl VertexBackend {
    /// The location, the project and the Vertex model id of a row:
    /// `provider_config` first, then `model_metadata`. Neither the location
    /// nor the project has a default (a
    /// row that named no location was sent to `us-central1`, and one that
    /// named no project to the project `my-project`, with the row's own
    /// access token).
    fn endpoint_parts(model: &LlmModel) -> (Option<String>, Option<String>, String) {
        // Try provider_config (new UI-based credentials)
        let (pc_project, pc_location) = model
            .provider_config
            .as_deref()
            .and_then(|json| {
                serde_json::from_str::<crate::services::cloud_auth::ProviderCredentials>(json).ok()
            })
            .map(|c| (c.gcp_project_id, c.gcp_location))
            .unwrap_or((None, None));

        let metadata = model.model_metadata.as_ref().and_then(|m| m.as_object());

        let region = pc_location
            .or_else(|| {
                metadata
                    .and_then(|m| m.get("region"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
            })
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty());

        let project = pc_project
            .or_else(|| {
                metadata
                    .and_then(|m| m.get("project"))
                    .and_then(|v| v.as_str())
                    .map(String::from)
            })
            .map(|p| p.trim().to_string())
            .filter(|p| !p.is_empty());

        let vertex_model = metadata
            .and_then(|m| m.get("vertex_model"))
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_else(|| model.model_name.clone());

        (region, project, vertex_model)
    }

    /// The location and the project of a Vertex AI row, as configured
    /// (`provider_config`, then `model_metadata`); `None` for what the row
    /// does not name.
    pub(crate) fn location_and_project(model: &LlmModel) -> (Option<String>, Option<String>) {
        let (location, project, _) = Self::endpoint_parts(model);
        (location, project)
    }

    /// The URL of a publisher model of a Vertex AI row, without the
    /// `:<method>` suffix, built by the one builder of a Vertex host
    /// (`cloud_hosts::vertex_publisher_model_url`) from the row's checked
    /// location and project (a location such as
    /// `x.attacker.example/` put the request, with the Google access token,
    /// on another host). `model_name` is the model as the row names it; the
    /// catalog's `vertex_ai/` prefix is part of no Vertex model id.
    pub(crate) fn publisher_model_url(
        model: &LlmModel,
        publisher: &'static str,
        model_name: &str,
    ) -> Result<String, String> {
        let (location, project) = Self::location_and_project(model);
        let model_name = model_name.strip_prefix("models/").unwrap_or(model_name);
        let model_name = model_name
            .strip_prefix("vertex_ai/")
            .or_else(|| model_name.strip_prefix("vertex/"))
            .unwrap_or(model_name);
        project
            .ok_or_else(|| {
                "the model names no Google Cloud project (set the project in the model's cloud \
                 credentials)"
                    .to_string()
            })
            .and_then(|project| {
                let location = location.ok_or_else(|| {
                    "the model names no Vertex AI location (set gcp_location in the model's \
                     cloud credentials)"
                        .to_string()
                })?;
                crate::services::cloud_hosts::vertex_publisher_model_url(
                    &location, &project, publisher, model_name,
                )
            })
            .map_err(|reason| {
                format!(
                    "provider_endpoint_invalid: the Vertex AI endpoint of model '{}' (provider \
                 '{}') cannot be built: {reason}. Nothing was sent.",
                    model.model_name, model.provider
                )
            })
    }

    /// Why a Vertex AI row cannot be sent: it carries no service account of
    /// its own (it was called with the gateway's own Google identity),
    /// or its URL, built from its location and project, cannot be built: one
    /// of them is not a name, or there is no project. `None`
    /// for a row that can be authenticated and sent. A Claude row with an
    /// `api_url` is sent there and builds no host; a Gemini row is always
    /// built.
    pub(crate) fn row_problem(model: &LlmModel) -> Option<(&'static str, String)> {
        if !row_has_a_service_account(model) {
            return Some(("provider_credential_missing", missing_credential(model)));
        }
        let claude = model.model_name.to_ascii_lowercase().contains("claude");
        let built = if claude {
            if model.api_url.as_deref().is_some_and(|u| !u.is_empty()) {
                return None;
            }
            let (_, _, vertex_model) = Self::endpoint_parts(model);
            Self::publisher_model_url(model, "anthropic", &vertex_model)
        } else {
            Self::publisher_model_url(model, "google", &model.model_name)
        };
        built
            .err()
            .map(|reason| ("provider_endpoint_invalid", reason))
    }
}

impl BackendAdapter for VertexBackend {
    fn backend_name(&self) -> &str {
        "vertex"
    }

    fn messages_url(&self, model: &LlmModel, streaming: bool) -> String {
        if let Some(ref url) = model.api_url {
            if !url.is_empty() {
                let base = url.trim_end_matches('/');
                return if streaming {
                    format!("{base}:streamRawPredict")
                } else {
                    format!("{base}:rawPredict")
                };
            }
        }

        let (_, _, vertex_model) = Self::endpoint_parts(model);
        // A row whose location or project is not a name is refused at
        // dispatch (`providers::dispatch_refusal`). Should one get here, it
        // gets a URL nothing can be sent to.
        let base =
            Self::publisher_model_url(model, "anthropic", &vertex_model).unwrap_or_else(|_| {
                format!(
                    "{}://vertex_ai/endpoint-invalid",
                    crate::services::providers::NO_UPSTREAM_SCHEME
                )
            });
        if streaming {
            format!("{base}:streamRawPredict")
        } else {
            format!("{base}:rawPredict")
        }
    }

    fn count_tokens_url(&self, _model: &LlmModel) -> Option<String> {
        None
    }

    fn auth_headers(&self, _model: &LlmModel) -> Vec<(String, String)> {
        // Auth headers are computed asynchronously via get_access_token().
        // The orchestrator calls get_access_token() and adds the Bearer header.
        // We can't do async work in a sync trait method, so return empty here.
        //
        // The orchestrator must check if provider == "vertex_ai" and call
        // get_access_token() to add the Authorization header.
        vec![]
    }

    fn filter_beta_headers(&self, beta_value: &str) -> Option<String> {
        BetaFilter::strip_all().filter(beta_value)
    }

    fn transform_request(&self, body: &mut Value, model: &LlmModel) {
        let obj = match body.as_object_mut() {
            Some(o) => o,
            None => return,
        };

        // Set the model name as Vertex expects it
        let (_, _, vertex_model) = Self::endpoint_parts(model);
        obj.insert("model".into(), Value::String(vertex_model));

        // Add anthropic_version to body
        if !obj.contains_key("anthropic_version") {
            obj.insert(
                "anthropic_version".into(),
                Value::String("2023-06-01".into()),
            );
        }

        // Map effort → thinking (same logic as Bedrock)
        if let Some(effort) = obj.remove("effort") {
            if let Some(effort_str) = effort.as_str() {
                let budget = match effort_str {
                    "low" | "minimal" => 1024,
                    "medium" => 10000,
                    "high" => 15000,
                    _ => 10000,
                };
                if !obj.contains_key("thinking") {
                    obj.insert(
                        "thinking".into(),
                        json!({
                            "type": "enabled",
                            "budget_tokens": budget
                        }),
                    );
                }
            }
        }
    }

    fn native_anthropic_streaming(&self) -> bool {
        // Vertex :streamRawPredict returns SSE-like format.
        // Based on documentation, Vertex wraps Anthropic SSE events similarly to
        // Anthropic Direct, so we treat it as native with minimal conversion needed.
        // If issues arise in testing, switch to false and implement a converter.
        true
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_model(model_name: &str, api_url: Option<&str>, metadata: Option<Value>) -> LlmModel {
        let mut m = crate::services::llm_adapter::make_test_model("vertex_ai", model_name);
        m.api_url = api_url.map(String::from);
        m.model_metadata = metadata;
        m
    }

    #[test]
    fn backend_name() {
        assert_eq!(VertexBackend.backend_name(), "vertex");
    }

    #[test]
    fn messages_url_from_metadata() {
        let model = test_model(
            "claude-sonnet-4-6-20250514",
            None,
            Some(json!({
                "region": "europe-west4",
                "project": "my-gcp-project",
                "vertex_model": "claude-sonnet-4-6@20250514"
            })),
        );
        let url = VertexBackend.messages_url(&model, false);
        assert!(url.contains("europe-west4-aiplatform.googleapis.com"));
        assert!(url.contains("my-gcp-project"));
        assert!(url.contains("claude-sonnet-4-6@20250514"));
        assert!(url.ends_with(":rawPredict"));
    }

    #[test]
    fn messages_url_streaming() {
        let model = test_model(
            "claude-sonnet-4-6-20250514",
            None,
            Some(json!({"region": "us-central1", "project": "p"})),
        );
        let url = VertexBackend.messages_url(&model, true);
        assert!(url.ends_with(":streamRawPredict"));
    }

    #[test]
    fn messages_url_custom_api_url() {
        let model = test_model(
            "claude-sonnet-4-6-20250514",
            Some("https://custom-vertex-endpoint.example.com/v1/model"),
            None,
        );
        let url = VertexBackend.messages_url(&model, false);
        assert_eq!(
            url,
            "https://custom-vertex-endpoint.example.com/v1/model:rawPredict"
        );
    }

    #[test]
    fn count_tokens_not_supported() {
        let model = test_model("claude-sonnet-4-6-20250514", None, None);
        assert!(VertexBackend.count_tokens_url(&model).is_none());
    }

    #[test]
    fn beta_headers_stripped() {
        let result =
            VertexBackend.filter_beta_headers("claude-code-20250219,extended-thinking-2025-01-24");
        assert!(result.is_none());
    }

    #[test]
    fn transform_adds_anthropic_version() {
        let model = test_model("claude-sonnet-4-6-20250514", None, None);
        let mut body = json!({"model": "claude-sonnet-4-6", "messages": []});

        VertexBackend.transform_request(&mut body, &model);
        assert_eq!(body["anthropic_version"], "2023-06-01");
    }

    #[test]
    fn transform_maps_effort() {
        let model = test_model("claude-sonnet-4-6-20250514", None, None);
        let mut body = json!({"model": "claude-sonnet-4-6", "messages": [], "effort": "medium"});

        VertexBackend.transform_request(&mut body, &model);

        assert!(body.get("effort").is_none());
        let thinking = body.get("thinking").unwrap();
        assert_eq!(thinking["type"], "enabled");
        assert_eq!(thinking["budget_tokens"], 10000);
    }

    #[test]
    fn native_streaming_enabled() {
        // Vertex :streamRawPredict is treated as native Anthropic SSE
        assert!(VertexBackend.native_anthropic_streaming());
    }

    // ── provider_config tests ─────────────────────────────────────────

    #[test]
    fn endpoint_parts_from_provider_config() {
        let mut model = test_model("claude-sonnet-4-6-20250514", None, None);
        model.provider_config = Some(
            serde_json::json!({
                "gcp_project_id": "pc-project",
                "gcp_location": "europe-west4"
            })
            .to_string(),
        );

        let (region, project, _vertex_model) = VertexBackend::endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("europe-west4"));
        assert_eq!(project.as_deref(), Some("pc-project"));
    }

    #[test]
    fn endpoint_parts_provider_config_takes_priority_over_metadata() {
        let mut model = test_model(
            "claude-sonnet-4-6-20250514",
            None,
            Some(json!({
                "region": "us-central1",
                "project": "metadata-project"
            })),
        );
        model.provider_config = Some(
            serde_json::json!({
                "gcp_project_id": "pc-project",
                "gcp_location": "europe-west4"
            })
            .to_string(),
        );

        let (region, project, _) = VertexBackend::endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("europe-west4"));
        assert_eq!(project.as_deref(), Some("pc-project"));
    }

    #[test]
    fn endpoint_parts_falls_back_to_metadata() {
        let model = test_model(
            "claude-sonnet-4-6-20250514",
            None,
            Some(json!({
                "region": "asia-southeast1",
                "project": "meta-project",
                "vertex_model": "claude-sonnet-4-6@20250514"
            })),
        );
        // No provider_config

        let (region, project, vertex_model) = VertexBackend::endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("asia-southeast1"));
        assert_eq!(project.as_deref(), Some("meta-project"));
        assert_eq!(vertex_model, "claude-sonnet-4-6@20250514");
    }

    /// A row that names no project has none. It had `my-project`.
    #[test]
    fn endpoint_parts_have_no_default_location_and_no_default_project() {
        let model = test_model("claude-sonnet-4-6-20250514", None, None);

        let (region, project, vertex_model) = VertexBackend::endpoint_parts(&model);
        assert_eq!(region, None, "no default location");
        assert_eq!(project, None);
        assert_eq!(vertex_model, "claude-sonnet-4-6-20250514");
        for blank in ["", "  "] {
            let mut model = test_model("claude-sonnet-4-6-20250514", None, None);
            model.provider_config = Some(json!({"gcp_project_id": blank}).to_string());
            assert_eq!(VertexBackend::endpoint_parts(&model).1, None, "{blank:?}");
        }
        assert!(
            VertexBackend::publisher_model_url(&model, "anthropic", "claude-sonnet-4-6")
                .unwrap_err()
                .starts_with("provider_endpoint_invalid"),
        );
        assert!(
            VertexBackend
                .messages_url(&model, false)
                .starts_with(crate::services::providers::NO_UPSTREAM_SCHEME)
        );
    }

    /// A row with no service account of its own has a problem and
    /// gets no token; nothing here asks for the process's own identity.
    #[tokio::test]
    async fn a_row_without_a_service_account_gets_no_token() {
        let mut model = test_model(
            "claude-sonnet-4-6-20250514",
            Some("http://127.0.0.1:9/v1/model"),
            None,
        );
        model.api_key = None;
        model.provider_config =
            Some(json!({"gcp_project_id": "p", "gcp_location": "global"}).to_string());
        assert_eq!(
            VertexBackend::row_problem(&model).map(|p| p.0),
            Some("provider_credential_missing")
        );
        let refused = get_access_token(&model).await.unwrap_err();
        assert!(
            refused.starts_with("provider_credential_missing"),
            "{refused}"
        );
        // A service account that is not one is none.
        model.provider_config = Some(
            json!({"gcp_project_id": "p", "gcp_credentials_json": "{\"type\": \"nothing\"}"})
                .to_string(),
        );
        assert_eq!(
            VertexBackend::row_problem(&model).map(|p| p.0),
            Some("provider_credential_missing")
        );
    }

    #[test]
    fn endpoint_parts_partial_provider_config() {
        let mut model = test_model(
            "claude-sonnet-4-6-20250514",
            None,
            Some(json!({"region": "us-central1", "project": "meta-proj"})),
        );
        // provider_config with only location, no project
        model.provider_config = Some(serde_json::json!({"gcp_location": "me-west1"}).to_string());

        let (region, project, _) = VertexBackend::endpoint_parts(&model);
        assert_eq!(region.as_deref(), Some("me-west1")); // from provider_config
        assert_eq!(project.as_deref(), Some("meta-proj")); // falls back to metadata
    }

    #[test]
    fn messages_url_from_provider_config() {
        let mut model = test_model("claude-sonnet-4-6-20250514", None, None);
        model.provider_config = Some(
            serde_json::json!({
                "gcp_project_id": "my-pc-project",
                "gcp_location": "europe-west1"
            })
            .to_string(),
        );

        let url = VertexBackend.messages_url(&model, false);
        assert!(url.contains("europe-west1-aiplatform.googleapis.com"));
        assert!(url.contains("my-pc-project"));
        assert!(url.ends_with(":rawPredict"));
    }
}

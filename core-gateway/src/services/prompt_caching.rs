// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The prompt-cache hook
//!
//! Providers keep the computed state of a prompt prefix and bill a repeat of
//! it at a fraction. Prompt caching is the gateway using that on purpose,
//! under a policy bound to a resource group: it marks the stable part of an
//! outbound request for the provider's cache (Anthropic `cache_control`
//! breakpoints, a Gemini `cachedContents` resource the gateway creates and
//! references; OpenAI caches by itself and gets no mark), and it pins a
//! conversation to the model whose cache it warmed, so that a routing group
//! does not send the next turn to a sibling with a cold cache.
//!
//! That is an Enterprise feature. The router, the LLM routes and the native
//! Anthropic and Gemini pipelines are Community, and before this hook each of
//! them loaded the group's policy and called the feature's functions itself
//! (six places that mark, one that follows a pin, one that drops it, two that
//! set it). They now ask this hook, and which requests are marked, how, and
//! which model a conversation is pinned to is the feature's business.
//!
//! ## Shape
//!
//! * [`PromptCachePolicy::session`] is asked once per request, where the
//!   request's group and model are known. It returns a
//!   [`PromptCacheSession`] that already holds what is the same for every
//!   question about that request: the policy as it stood when the request
//!   arrived (or the fact that there is none), the tenant, and the stores
//!   the feature keeps its state in. The session is shared (`Arc`), so the
//!   end-of-stream task of a streamed answer carries it.
//! * [`PromptCacheSession::mark`] where the outbound body is final and in
//!   the provider's own shape — after translation and system-prompt
//!   injection, so the marks travel with the content they mark.
//! * [`PromptCacheSession::pinned_model`] before the router picks its
//!   primary; [`PromptCacheSession::clear_pin`] when that primary failed;
//!   [`PromptCacheSession::record_warm`] when an answer reported tokens
//!   written to the provider's cache.
//!
//! The router takes the session as `Option<&dyn PromptCacheSession>`: a
//! caller that passes `None` is not asking for prompt caching at all (the
//! fallback candidates, `/v1/messages` in front of a model that is not
//! Anthropic's), exactly as before.
//!
//! ## What stays with the caller, and why
//!
//! * **Cached-token accounting.** What the provider *reports* about its
//!   cache — the audit row's `prompt_cache_read_tokens` /
//!   `prompt_cache_write_tokens` (part of the hash-chained evidence record),
//!   the saving, the metrics, the size of the context — is read by Community
//!   code in [`crate::services::prompt_cache_usage`], with or without this
//!   feature: OpenAI caches without being asked, and a caller may mark its
//!   own request. The saving is computed there from the provider's discount
//!   and the model's price; no policy is involved. The route hands
//!   `record_warm` the one figure the pin needs (tokens written).
//! * **Whether a call succeeded.** Only an answer pins; the route knows
//!   the status, the session does not.
//! * **Making a pinned model safe to dispatch to.** A pin names a sibling in
//!   the routing group. Decrypting its credentials and applying the tenant's
//!   residency profile is the router's rule for *any* model that becomes a
//!   primary, not the feature's: the session returns the
//!   model as stored and the router declines it when it cannot make it safe.
//! * **Counting the decision.** `mark` returns what was decided
//!   ([`PromptCacheDecision`]) and the surfaces that counted it before count
//!   it still ([`record_decision`]); the OpenAI-surface bridge to Gemini
//!   never did.
//!
//! ## What is not behind the hook yet
//!
//! The feature's in-process caches are invalidated, flushed and counted by
//! the invalidation dispatcher and the cache-statistics fan-out through the
//! service handle (`AppState.prompt_cache`), as the response cache's are
//! through `AppState.semantic_cache`. Those handlers are already tagged with
//! the feature they belong to (`services::cache::invalidation`) .
//!
//! ## The default
//!
//! [`NoPromptCaching`]: nothing is marked, no model is pinned, nothing is
//! recorded. The request goes to the provider as the caller and the adapters
//! wrote it. That is the Community behaviour.

use std::sync::Arc;

use async_trait::async_trait;
use serde_json::Value;

use crate::models::db::{LlmModel, ResourceGroup};
use crate::models::proxy::ChatCompletionRequest;
use crate::services::AppState;
use crate::services::prompt_cache_usage::PromptCacheMechanism;

/// What was decided about marking one outbound request. Telemetry only: the
/// request has already been changed (or left alone) when this is returned.
#[derive(Debug, Clone)]
pub struct PromptCacheDecision {
    /// Prompt caching is in effect for the request (for OpenAI that means
    /// "the provider will do it", with no mark).
    pub applied: bool,
    pub mechanism: PromptCacheMechanism,
    pub breakpoints_inserted: u8,
    /// Why, as a metric label: `applied`, or the reason it was skipped.
    pub reason: &'static str,
}

impl PromptCacheDecision {
    pub fn skipped(mechanism: PromptCacheMechanism, reason: &'static str) -> Self {
        Self {
            applied: false,
            mechanism,
            breakpoints_inserted: 0,
            reason,
        }
    }

    pub fn applied(mechanism: PromptCacheMechanism, breakpoints: u8) -> Self {
        Self {
            applied: true,
            mechanism,
            breakpoints_inserted: breakpoints,
            reason: "applied",
        }
    }
}

/// Count a marking decision (`brutor_prompt_cache_decisions_total`).
pub fn record_decision(decision: &PromptCacheDecision) {
    crate::services::proxy_metrics::record_prompt_cache_decision(
        crate::services::proxy_metrics::mechanism_label(decision.mechanism),
        decision.applied,
        decision.reason,
    );
}

/// One request's view of prompt caching.
#[async_trait]
pub trait PromptCacheSession: Send + Sync {
    /// Mark `body` — the request as it is about to be sent to `model`, in
    /// the provider's own shape — for the provider's cache. `None` when
    /// there was nothing to decide (no policy applies to the request).
    async fn mark(&self, body: &mut Value, model: &LlmModel) -> Option<PromptCacheDecision>;

    /// The model this request's prefix is pinned to, when that is a sibling
    /// of `primary` and not `primary` itself. Returned as stored: the caller
    /// makes it safe to dispatch to, or declines it.
    async fn pinned_model(
        &self,
        primary: &LlmModel,
        request: &ChatCompletionRequest,
    ) -> Option<LlmModel>;

    /// `model` failed for this request: a pin to it must not send the next
    /// request there.
    async fn clear_pin(&self, request: &ChatCompletionRequest, model: &LlmModel);

    /// `model` answered this request and reported `write_tokens` written to
    /// its cache. Call it only for an answer, never for a failed call.
    async fn record_warm(
        &self,
        request: &ChatCompletionRequest,
        model: &LlmModel,
        write_tokens: i32,
    );
}

/// The prompt-cache policy of an installation.
#[async_trait]
pub trait PromptCachePolicy: Send + Sync {
    /// Open the session for one request. `group` is the resource group the
    /// request resolved to and `model` the model it asked for; a narrower
    /// policy bound to that model in the group wins over the group's.
    async fn session(
        &self,
        state: &AppState,
        tenant_id: &str,
        group: Option<&ResourceGroup>,
        model: &LlmModel,
    ) -> Arc<dyn PromptCacheSession>;
}

/// No prompt caching: the Community default.
pub struct NoPromptCaching;

struct NoSession;

#[async_trait]
impl PromptCacheSession for NoSession {
    async fn mark(&self, _body: &mut Value, _model: &LlmModel) -> Option<PromptCacheDecision> {
        None
    }

    async fn pinned_model(
        &self,
        _primary: &LlmModel,
        _request: &ChatCompletionRequest,
    ) -> Option<LlmModel> {
        None
    }

    async fn clear_pin(&self, _request: &ChatCompletionRequest, _model: &LlmModel) {}

    async fn record_warm(
        &self,
        _request: &ChatCompletionRequest,
        _model: &LlmModel,
        _write_tokens: i32,
    ) {
    }
}

/// The session of a request no prompt-cache policy applies to: nothing is
/// marked, pinned or recorded. One shared value; taking it allocates nothing.
pub fn no_session() -> Arc<dyn PromptCacheSession> {
    static NONE: std::sync::OnceLock<Arc<NoSession>> = std::sync::OnceLock::new();
    NONE.get_or_init(|| Arc::new(NoSession)).clone()
}

#[async_trait]
impl PromptCachePolicy for NoPromptCaching {
    async fn session(
        &self,
        _state: &AppState,
        _tenant_id: &str,
        _group: Option<&ResourceGroup>,
        _model: &LlmModel,
    ) -> Arc<dyn PromptCacheSession> {
        no_session()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;
    use serde_json::json;

    fn request() -> ChatCompletionRequest {
        serde_json::from_value(json!({
            "model": "m",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "hi"}
            ]
        }))
        .expect("a chat request")
    }

    #[tokio::test]
    async fn the_default_session_marks_nothing_and_pins_nothing() {
        let session = no_session();
        let model = make_test_model("anthropic", "claude-test");
        let mut body = json!({
            "system": "Be terse.",
            "tools": [{"name": "t", "input_schema": {"type": "object"}}],
            "messages": [{"role": "user", "content": "hi"}]
        });
        let sent = body.clone();
        assert!(session.mark(&mut body, &model).await.is_none());
        assert_eq!(body, sent, "the request goes out as it was written");

        let request = request();
        assert!(session.pinned_model(&model, &request).await.is_none());
        // Neither has anything to do, and neither needs a store to do it.
        session.record_warm(&request, &model, 900).await;
        session.clear_pin(&request, &model).await;
    }

    #[test]
    fn a_decision_says_whether_it_applied_and_why() {
        let skipped = PromptCacheDecision::skipped(PromptCacheMechanism::None, "below_min_tokens");
        assert!(!skipped.applied);
        assert_eq!(skipped.breakpoints_inserted, 0);
        assert_eq!(skipped.reason, "below_min_tokens");

        let applied =
            PromptCacheDecision::applied(PromptCacheMechanism::CacheControlBreakpoints, 2);
        assert!(applied.applied);
        assert_eq!(applied.breakpoints_inserted, 2);
        assert_eq!(applied.reason, "applied");
    }
}

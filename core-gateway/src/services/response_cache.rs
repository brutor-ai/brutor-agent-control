// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The response cache hook
//!
//! A response cache answers a request from an earlier answer instead of
//! calling the model. It is an Enterprise feature; the LLM routes are
//! Community. Before this hook each of the five LLM surfaces (chat, streamed
//! chat, `/responses`, streamed `/responses`, `/completions`) held its own
//! copy of the cache's gating — the opt-out header, the group's cache policy,
//! tool round trips, continuations — around direct calls into the cache
//! service, and the copies had drifted. The routes now ask this
//! hook, and what the cache's rules are is the cache's business.
//!
//! ## Shape
//!
//! * [`ResponseCache::session`] is asked once per request. It returns a
//!   [`ResponseCacheSession`] that already knows everything about the request
//!   that is the same for its lookup and its store: whether the caller opted
//!   out, the group's cache policy as it stood when the request arrived, and
//!   the credentials for the cache's own embedding calls. The session is
//!   shared (`Arc`), so a streamed answer's end-of-stream task carries it.
//! * [`ResponseCacheSession::lookup`] before the model is called.
//! * [`ResponseCacheSession::plan_store`] where the facts about the answer
//!   are in scope, and [`StorePlan::commit`] with the body that was actually
//!   returned.
//!
//! ## Why the store has two steps
//!
//! A lookup returns a cached body with no guardrail pass, so what goes in
//! must already be what a caller may see. The decision to
//! store needs inputs that are in scope early; the write must wait for the
//! output guardrail's verdict and take the redacted body. A single `store`
//! call is one a caller can make too early — that was the defect. With a
//! plan, *having* something to commit and *committing* it are two acts, and
//! the route performs the second only after the verdict, with the body it
//! returns.
//!
//! ## What stays with the route
//!
//! * **Never committing a refused answer.** The route knows the verdict; the
//!   plan does not.
//! * **Grounded answers.** A group whose answers are built from a knowledge
//!   base neither reads nor writes any response cache: the
//!   answer depends on the asker's document permissions, which are not in
//!   the key. That is a rule about grounded answers, not about whichever
//!   cache is installed, so the route does not ask the session at all.
//!
//! ## The dispatch layer (R10b)
//!
//! A cache may also answer further down, where the router sends a request to
//! one model: an exact repeat of a non-streamed chat request, for the same
//! caller, answered from memory. The router is Community and deep in the
//! call graph, so the route asks [`ResponseCache::dispatch_cache`] once and
//! runs the forward inside [`with_dispatch_cache`]; the router reads it back
//! with [`current_dispatch_cache`]. No dispatch cache in scope — none
//! installed, the caller opted out, or a caller of the router that is not a
//! route — means the router neither reads nor writes one. What such a hit
//! returns is the upstream's answer as it arrived, so the output guardrail
//! runs on it as on a fresh answer; that is why this layer needs no plan
//! and commit.
//!
//! ## The default
//!
//! [`NoResponseCache`]: every lookup misses, nothing is ever planned, and
//! there is no dispatch layer.
//! That is the Community behaviour, and it is the most restrictive answer a
//! cache can give.

use std::sync::Arc;

use async_trait::async_trait;
use axum::http::HeaderMap;
use serde_json::Value;

use crate::models::auth::AuthContext;
use crate::models::db::{LlmModel, ResourceGroup};
use crate::models::proxy::ChatCompletionRequest;
use crate::services::AppState;
use crate::services::router_state::RouterState;

/// Which layer of a response cache served a hit. Recorded on the audit row
/// (`llm_cache_layer`), whoever the cache is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CacheHitLayer {
    Exact,
    Semantic,
    SharedPool,
}

impl CacheHitLayer {
    pub fn as_str(&self) -> &'static str {
        match self {
            CacheHitLayer::Exact => "exact",
            CacheHitLayer::Semantic => "semantic",
            CacheHitLayer::SharedPool => "shared",
        }
    }
}

impl std::fmt::Display for CacheHitLayer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A cache hit: the cached response and what the audit row says of it. The
/// routes build their audit row and their reply from it. Plain data,
/// defined here so that this module and
/// the routes name no implementation; the Enterprise cache service knows
/// it as `CacheResult`.
#[derive(Debug, Clone)]
pub struct CacheHit {
    pub layer: CacheHitLayer,
    pub response: Value,
    pub tokens_saved: i32,
    pub similarity_score: Option<f64>,
}

/// The request a cached answer is keyed on.
#[derive(Clone, Copy)]
pub enum CacheBody<'a> {
    /// `/chat/completions`.
    Chat(&'a ChatCompletionRequest),
    /// `/responses` and `/completions`, as the JSON body.
    Value(&'a Value),
}

/// What identifies one cacheable exchange within a session.
#[derive(Clone, Copy)]
pub struct CacheKey<'a> {
    pub tenant_id: &'a str,
    pub group_id: Option<&'a str>,
    pub model_name: &'a str,
    pub body: CacheBody<'a>,
}

/// What the route knows about the answer when it asks for a store plan.
pub struct StoreFacts {
    /// `(server, tool)` for each tool the answer asked to call; one empty
    /// pair when a tool was called and its name is not known. Whether the
    /// *request* is the second half of a tool round trip is the session's
    /// to work out from the key.
    pub invoked_tools: Vec<(String, String)>,
    /// Tokens the stored answer cost, reported as saved on a later hit.
    pub token_count: i32,
}

/// A store that has been decided and not yet made.
///
/// Dropping a plan stores nothing. That is the point: the route drops it
/// when the output guardrail refuses the answer.
#[must_use = "a store plan does nothing until it is committed with the body that was returned"]
pub struct StorePlan {
    commit: Box<dyn FnOnce(Value) + Send>,
}

impl StorePlan {
    pub fn new(commit: impl FnOnce(Value) + Send + 'static) -> Self {
        Self {
            commit: Box::new(commit),
        }
    }

    /// Store `body`. Call this only after the output guardrail's verdict,
    /// with the body the caller was given (redactions applied).
    pub fn commit(self, body: Value) {
        (self.commit)(body);
    }
}

impl std::fmt::Debug for StorePlan {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("StorePlan")
    }
}

/// One request's view of the response cache.
#[async_trait]
pub trait ResponseCacheSession: Send + Sync {
    /// The cached answer for `key`, if the cache has one this caller may have.
    async fn lookup(&self, key: &CacheKey<'_>) -> Option<CacheHit>;

    /// Whether an answer with these facts is to be stored, and how.
    fn plan_store(&self, key: &CacheKey<'_>, facts: StoreFacts) -> Option<StorePlan>;
}

/// One caller's view of the cache at the point a request is dispatched to a
/// model. See "The dispatch layer" in the module documentation.
pub trait DispatchCache: Send + Sync {
    /// The answer and upstream status stored for this exact request, if any.
    fn lookup(
        &self,
        router_state: &RouterState,
        model: &LlmModel,
        request: &ChatCompletionRequest,
    ) -> Option<(Value, u16)>;

    /// Offer a successful upstream answer for storing.
    fn store(
        &self,
        router_state: &RouterState,
        model: &LlmModel,
        request: &ChatCompletionRequest,
        body: &Value,
        status: u16,
    );
}

tokio::task_local! {
    /// The dispatch cache of the request being forwarded. A task-local for
    /// the same reason as the router's other per-request values: it is set
    /// once around the forward and read once at the dispatch, without a
    /// parameter through every function between them.
    static DISPATCH_CACHE: Arc<dyn DispatchCache>;
}

/// Run `forward` with `cache` as the request's dispatch cache; with `None`,
/// just run it.
pub async fn with_dispatch_cache<F: Future>(
    cache: Option<Arc<dyn DispatchCache>>,
    forward: F,
) -> F::Output {
    match cache {
        Some(cache) => DISPATCH_CACHE.scope(cache, forward).await,
        None => forward.await,
    }
}

/// The dispatch cache the current request was forwarded with, if any.
///
/// `None` is "do not cache", never "share one": an earlier version keyed an
/// unscoped call on tenant, model and body alone, and two users were served
/// each other's answers.
pub fn current_dispatch_cache() -> Option<Arc<dyn DispatchCache>> {
    DISPATCH_CACHE.try_with(Arc::clone).ok()
}

/// The response cache of an installation.
#[async_trait]
pub trait ResponseCache: Send + Sync {
    /// The dispatch layer for one request of `caller`, or `None` when there
    /// is none for it (not installed, or the caller opted out by header).
    fn dispatch_cache(
        &self,
        headers: &HeaderMap,
        caller: &AuthContext,
    ) -> Option<Arc<dyn DispatchCache>>;

    /// Open the session for one request. `group` is the resource group the
    /// request resolved to; `headers` are the request's, as the cache's own
    /// self-calls are to see them (`routes::llm_helpers::self_call_headers`
    /// where [`ResponseCache::makes_self_calls`]).
    async fn session(
        &self,
        state: &AppState,
        tenant_id: &str,
        group: Option<&ResourceGroup>,
        headers: &HeaderMap,
    ) -> Arc<dyn ResponseCacheSession>;

    /// Whether a session of this cache calls the gateway's own routes for
    /// the request (the semantic layer's embedding calls). The route then
    /// hands the session headers that carry the request's run, so those
    /// calls are recorded as steps of it. `false` costs the route nothing.
    fn makes_self_calls(&self) -> bool {
        false
    }
}

/// No response cache: the Community default.
pub struct NoResponseCache;

struct NoSession;

#[async_trait]
impl ResponseCacheSession for NoSession {
    async fn lookup(&self, _key: &CacheKey<'_>) -> Option<CacheHit> {
        None
    }

    fn plan_store(&self, _key: &CacheKey<'_>, _facts: StoreFacts) -> Option<StorePlan> {
        None
    }
}

/// The session of an installation with no response cache: every lookup
/// misses, nothing is planned. One shared value; taking it allocates nothing.
pub fn no_session() -> Arc<dyn ResponseCacheSession> {
    static NONE: std::sync::OnceLock<Arc<NoSession>> = std::sync::OnceLock::new();
    NONE.get_or_init(|| Arc::new(NoSession)).clone()
}

#[async_trait]
impl ResponseCache for NoResponseCache {
    fn dispatch_cache(
        &self,
        _headers: &HeaderMap,
        _caller: &AuthContext,
    ) -> Option<Arc<dyn DispatchCache>> {
        None
    }

    async fn session(
        &self,
        _state: &AppState,
        _tenant_id: &str,
        _group: Option<&ResourceGroup>,
        _headers: &HeaderMap,
    ) -> Arc<dyn ResponseCacheSession> {
        no_session()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_plan_stores_only_when_committed_and_with_the_body_given() {
        let (tx, rx) = std::sync::mpsc::channel::<Value>();
        let plan = StorePlan::new(move |body| tx.send(body).expect("send"));
        assert!(rx.try_recv().is_err(), "planning is not storing");
        plan.commit(serde_json::json!({"answer": "redacted"}));
        assert_eq!(
            rx.try_recv().expect("committed"),
            serde_json::json!({"answer": "redacted"})
        );

        let (tx, rx) = std::sync::mpsc::channel::<Value>();
        drop(StorePlan::new(move |body| tx.send(body).expect("send")));
        assert!(rx.try_recv().is_err(), "a dropped plan stores nothing");
    }

    struct Marked;

    impl DispatchCache for Marked {
        fn lookup(
            &self,
            _router_state: &RouterState,
            _model: &LlmModel,
            _request: &ChatCompletionRequest,
        ) -> Option<(Value, u16)> {
            None
        }

        fn store(
            &self,
            _router_state: &RouterState,
            _model: &LlmModel,
            _request: &ChatCompletionRequest,
            _body: &Value,
            _status: u16,
        ) {
        }
    }

    #[tokio::test]
    async fn the_dispatch_cache_is_seen_only_inside_the_forward_it_was_given_to() {
        // Unscoped (a caller of the router that is not a route): none. The
        // router reads that as "do not cache", not as "share one bucket".
        assert!(current_dispatch_cache().is_none());

        let given: Arc<dyn DispatchCache> = Arc::new(Marked);
        let seen =
            with_dispatch_cache(Some(Arc::clone(&given)), async { current_dispatch_cache() })
                .await
                .expect("visible inside the forward");
        assert!(
            Arc::ptr_eq(&seen, &given),
            "and it is the one that was given"
        );

        // Declined by the cache: the forward runs with none.
        let seen = with_dispatch_cache(None, async { current_dispatch_cache() }).await;
        assert!(seen.is_none());
        assert!(current_dispatch_cache().is_none(), "nothing leaks out");
    }
}

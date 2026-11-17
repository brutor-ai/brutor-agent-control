// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The context-enricher hook
//!
//! A resource group can be *grounded*: bound to knowledge-base collections,
//! so that the gateway retrieves the passages the asker is permitted to see
//! and puts them in the request before the model is called. The knowledge
//! base is an Enterprise feature; the chat route and the request's admission
//! are Community. Before this hook the route read the group's knowledge-base
//! settings, resolved the asker's document permissions, called the retrieval
//! service and formatted its result, and the admission read the same
//! settings to reserve room for it. They now ask this hook, and whether a
//! group is grounded, what is retrieved for whom and how it is worded is the
//! feature's business.
//!
//! ## Shape
//!
//! * [`ContextEnricher::session`] is asked once per request, where the
//!   request's group is known. It returns a [`ContextEnrichmentSession`]
//!   that holds what is the same for every question about that request: the
//!   group's grounding as it stood when the request arrived.
//! * [`ContextEnrichmentSession::grounded`] — whether this group's answers
//!   are built from documents retrieved for the asker.
//! * [`ContextEnrichmentSession::retrieve`] — the context for one query and
//!   one asker, already worded, with what the audit row records about it.
//! * [`ContextEnricher::reserve_bytes`] — the most context a request of a
//!   group can be given, asked by the admission, which already holds the
//!   group; [`ContextEnrichmentSession::context_bound_bytes`] is the same
//!   figure for the session's group.
//! * [`enrich_chat`] is the route's one call. It is Community code and it is
//!   the only place that changes a request: it takes the query from the
//!   request, asks the session, cuts the context to the bound and places it.
//!
//! ## What crosses the boundary
//!
//! In: the query text, the caller's [`AuthContext`] (the enricher resolves
//! the caller's document permissions from it; the route does not know what a
//! permission is), and the credentials for the enricher's own embedding
//! call. Out: [`RetrievedContext`] — the text to inject and an
//! [`EnrichmentRecord`] — or nothing, or [`EnrichmentUnavailable`].
//!
//! ## What stays with the caller, and why
//!
//! * **The request's shape.** Which message is the query and where a system
//!   message goes are facts about a chat request, and the request is
//!   Community's. A session never sees the request.
//! * **The admission bound.** What the gateway adds after admission is part
//!   of the request's worst case, so the admission reserves
//!   `reserve_bytes` and [`enrich_chat`] cuts the context to
//!   `context_bound_bytes` — in Community code, so that a retrieval that
//!   returns more than it said it would cannot take a request past its token
//!   and spend caps.
//! * **The audit row.** `rag_collection_ids` and its sibling columns are
//!   part of the hash-chained evidence record in both editions (null where
//!   nothing was retrieved). The route writes them from the
//!   [`EnrichmentRecord`]; the enricher never touches the row.
//! * **The response cache.** A grounded group neither reads nor writes any
//!   response cache: the answer depends on the asker's
//!   document permissions, which are not in the cache key. The route asks
//!   `grounded()` and then does not ask the cache at all. The rule follows
//!   the *group*, not the outcome of one retrieval: a grounded request for
//!   which nothing was retrieved, or retrieval failed, is still not shared.
//! * **What an unavailable retrieval means for the request.**
//!   [`EnrichmentUnavailable`] is returned to the route, not turned into
//!   "nothing matched" behind the hook, and the route refuses the request
//!   (503, recorded as `engine_failure` / `knowledge_base_unavailable`):
//!   a grounded group's request is not answered
//!   without its grounding. "Nothing matched" is `Ok(None)` and is answered.
//! * **Guardrails.** They run on the caller's request before enrichment and
//!   on the answer after it, as before.
//!
//! ## Failing closed
//!
//! An implementation that cannot tell whether a group is grounded says it
//! *is* (nothing is shared through the response cache) and reports every
//! retrieval for it as unavailable. An asker whose permissions cannot be
//! resolved is the asker with none, never the asker with all.
//!
//! ## Shared data that is not behind the hook yet
//!
//! The group's knowledge-base settings (`EffectiveGroupData::kb_config`, of
//! the feature's own type) are still loaded by the group cache with the rest
//! of the group, from the feature's tables, and evicted by the invalidation
//! handler tagged `knowledge_base`. No request-path code reads the field;
//! the installed enricher does. The loader belongs to the feature,
//! as do the portal's knowledge-base routes.
//!
//! ## The default
//!
//! [`NoContextEnrichment`]: no group is grounded, nothing is retrieved,
//! nothing is injected and zero bytes are reserved. A group that has
//! collections bound is then an ordinary group, and its answers are ordinary
//! answers: nothing in them was retrieved for one asker, so the reason a
//! grounded answer is kept out of a response cache does not apply to them.
//! (Community has no response cache either; the statement matters for an
//! installation that composes one without the knowledge base.) That is the
//! Community behaviour.

use std::sync::Arc;

use async_trait::async_trait;

use crate::models::auth::AuthContext;
use crate::models::db::ResourceGroup;
use crate::models::proxy::{ChatCompletionRequest, ChatMessage};
use crate::services::AppState;
use crate::services::cache::group_cache::EffectiveGroupData;

/// What the audit row records about the context a request was given
/// (`rag_*` in `proxy_logs`).
#[derive(Debug, Clone, PartialEq)]
pub struct EnrichmentRecord {
    /// The collections the retrieval searched (`rag_collection_ids`).
    pub collection_ids: Vec<String>,
    pub chunks_retrieved: usize,
    pub context_tokens: usize,
    pub top_similarity_score: f64,
    pub retrieval_ms: f64,
}

/// Context retrieved for one query and one asker.
#[derive(Debug, Clone)]
pub struct RetrievedContext {
    /// The text to put in front of the model, already worded.
    pub text: String,
    pub record: EnrichmentRecord,
}

/// The group is grounded and its context could not be retrieved (the
/// implementation has logged why). Not the same as "nothing matched".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EnrichmentUnavailable;

impl std::fmt::Display for EnrichmentUnavailable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("the group's knowledge-base context could not be retrieved")
    }
}

impl std::error::Error for EnrichmentUnavailable {}

/// One request's view of context enrichment.
#[async_trait]
pub trait ContextEnrichmentSession: Send + Sync {
    /// Whether the request's group is grounded. `true` also when that could
    /// not be established.
    fn grounded(&self) -> bool;

    /// The most bytes of context a request of this group is given; what its
    /// admission reserved.
    fn context_bound_bytes(&self) -> usize;

    /// The context for `query`, limited to what `asker` is permitted to see.
    /// `Ok(None)` when the group is not grounded or nothing relevant was
    /// found. `auth_headers` are the caller's credentials, for the
    /// enricher's own (governed) embedding call.
    async fn retrieve(
        &self,
        query: &str,
        asker: &AuthContext,
        auth_headers: &[(String, String)],
    ) -> Result<Option<RetrievedContext>, EnrichmentUnavailable>;
}

/// The context enricher of an installation.
#[async_trait]
pub trait ContextEnricher: Send + Sync {
    /// The most bytes of context a request of `group` can be given after its
    /// admission. Zero for a group that is not grounded.
    fn reserve_bytes(&self, group: &EffectiveGroupData) -> usize;

    /// Open the session for one request. `group` is the resource group the
    /// request resolved to.
    async fn session(
        &self,
        state: &AppState,
        tenant_id: &str,
        group: Option<&ResourceGroup>,
    ) -> Arc<dyn ContextEnrichmentSession>;

    /// Whether a retrieval calls the gateway's own routes for the request
    /// (the embedding of the query). The route then forwards headers that
    /// carry the request's run. `false` costs the route nothing.
    fn makes_self_calls(&self) -> bool {
        false
    }
}

/// Ground a chat request: retrieve the context for its last user message and
/// place it as a system message after the caller's own system messages.
///
/// Returns what the audit row records, `Ok(None)` when the request was left
/// as it was (the group is not grounded, there is no user text to search
/// for, or nothing relevant was found), and `Err` when the group is grounded
/// and its context could not be retrieved — the request is unchanged then.
pub async fn enrich_chat(
    session: &dyn ContextEnrichmentSession,
    request: &mut ChatCompletionRequest,
    asker: &AuthContext,
    auth_headers: &[(String, String)],
) -> Result<Option<EnrichmentRecord>, EnrichmentUnavailable> {
    if !session.grounded() {
        return Ok(None);
    }
    // The last user message is the query. Array content is flattened, so a
    // text-and-image message still drives retrieval.
    let Some(query) = request
        .messages
        .iter()
        .rev()
        .find(|m| m.role == "user")
        .and_then(|m| m.content.as_ref())
        .map(crate::services::llm_preflight::flatten_input_text)
        .filter(|s| !s.is_empty())
    else {
        return Ok(None);
    };
    let Some(context) = session.retrieve(&query, asker, auth_headers).await? else {
        return Ok(None);
    };
    let text = cut_to(context.text, session.context_bound_bytes());
    let at = request
        .messages
        .iter()
        .position(|m| m.role != "system")
        .unwrap_or(0);
    request.messages.insert(
        at,
        ChatMessage {
            role: "system".to_string(),
            content: Some(serde_json::Value::String(text)),
            name: None,
            tool_calls: None,
            tool_call_id: None,
        },
    );
    Ok(Some(context.record))
}

/// `text` cut to at most `bound` bytes, on a character boundary.
fn cut_to(mut text: String, bound: usize) -> String {
    if text.len() > bound {
        let mut cut = bound;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
    }
    text
}

/// No context enrichment: the Community default.
pub struct NoContextEnrichment;

struct NoSession;

#[async_trait]
impl ContextEnrichmentSession for NoSession {
    fn grounded(&self) -> bool {
        false
    }

    fn context_bound_bytes(&self) -> usize {
        0
    }

    async fn retrieve(
        &self,
        _query: &str,
        _asker: &AuthContext,
        _auth_headers: &[(String, String)],
    ) -> Result<Option<RetrievedContext>, EnrichmentUnavailable> {
        Ok(None)
    }
}

/// The session of a request whose group is not grounded: nothing is
/// retrieved, nothing is reserved. One shared value; taking it allocates
/// nothing.
pub fn no_session() -> Arc<dyn ContextEnrichmentSession> {
    static NONE: std::sync::OnceLock<Arc<NoSession>> = std::sync::OnceLock::new();
    NONE.get_or_init(|| Arc::new(NoSession)).clone()
}

#[async_trait]
impl ContextEnricher for NoContextEnrichment {
    fn reserve_bytes(&self, _group: &EffectiveGroupData) -> usize {
        0
    }

    async fn session(
        &self,
        _state: &AppState,
        _tenant_id: &str,
        _group: Option<&ResourceGroup>,
    ) -> Arc<dyn ContextEnrichmentSession> {
        no_session()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    fn request(messages: serde_json::Value) -> ChatCompletionRequest {
        serde_json::from_value(json!({"model": "m", "messages": messages})).expect("a chat request")
    }

    fn asker() -> AuthContext {
        AuthContext {
            auth_type: crate::models::auth::AuthType::Anonymous,
            tenant_id: "t1".into(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: None,
            api_key_name: None,
            api_key_scope: None,
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    /// A session that answers every query with `text` and remembers what it
    /// was asked.
    struct Fixed {
        grounded: bool,
        bound: usize,
        answer: Result<Option<&'static str>, EnrichmentUnavailable>,
        asked: Mutex<Vec<String>>,
    }

    impl Fixed {
        fn grounded(answer: Result<Option<&'static str>, EnrichmentUnavailable>) -> Self {
            Self {
                grounded: true,
                bound: 1_000,
                answer,
                asked: Mutex::new(Vec::new()),
            }
        }
    }

    #[async_trait]
    impl ContextEnrichmentSession for Fixed {
        fn grounded(&self) -> bool {
            self.grounded
        }

        fn context_bound_bytes(&self) -> usize {
            self.bound
        }

        async fn retrieve(
            &self,
            query: &str,
            _asker: &AuthContext,
            _auth_headers: &[(String, String)],
        ) -> Result<Option<RetrievedContext>, EnrichmentUnavailable> {
            self.asked.lock().expect("lock").push(query.to_string());
            self.answer.map(|found| {
                found.map(|text| RetrievedContext {
                    text: text.to_string(),
                    record: EnrichmentRecord {
                        collection_ids: vec!["c1".to_string()],
                        chunks_retrieved: 1,
                        context_tokens: text.len() / 4,
                        top_similarity_score: 0.9,
                        retrieval_ms: 1.0,
                    },
                })
            })
        }
    }

    fn roles(request: &ChatCompletionRequest) -> Vec<&str> {
        request.messages.iter().map(|m| m.role.as_str()).collect()
    }

    #[tokio::test]
    async fn the_default_grounds_nothing_and_reserves_nothing() {
        let session = no_session();
        assert!(!session.grounded());
        assert_eq!(session.context_bound_bytes(), 0);
        assert_eq!(
            session
                .retrieve("q", &asker(), &[])
                .await
                .map(|c| c.is_some()),
            Ok(false)
        );

        let mut req = request(json!([{"role": "user", "content": "what is our leave policy?"}]));
        let sent = serde_json::to_value(&req).expect("json");
        assert_eq!(
            enrich_chat(session.as_ref(), &mut req, &asker(), &[]).await,
            Ok(None)
        );
        assert_eq!(
            serde_json::to_value(&req).expect("json"),
            sent,
            "the request goes out as it was written"
        );
    }

    #[tokio::test]
    async fn context_goes_after_the_callers_system_messages_and_the_query_is_the_last_user_turn() {
        let session = Fixed::grounded(Ok(Some("CONTEXT")));
        let mut req = request(json!([
            {"role": "system", "content": "one"},
            {"role": "system", "content": "two"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": [{"type": "text", "text": "second question"}]}
        ]));
        let record = enrich_chat(&session, &mut req, &asker(), &[])
            .await
            .expect("retrieval worked")
            .expect("and found something");
        assert_eq!(record.collection_ids, ["c1"]);
        assert_eq!(
            roles(&req),
            ["system", "system", "system", "user", "assistant", "user"]
        );
        assert_eq!(req.messages[2].content, Some(json!("CONTEXT")));
        assert_eq!(*session.asked.lock().expect("lock"), ["second question"]);

        // No system message of the caller's: the context comes first.
        let mut req = request(json!([{"role": "user", "content": "q"}]));
        enrich_chat(&session, &mut req, &asker(), &[])
            .await
            .expect("retrieval worked");
        assert_eq!(roles(&req), ["system", "user"]);
    }

    #[tokio::test]
    async fn the_context_is_cut_to_the_bound_on_a_character_boundary() {
        let mut session = Fixed::grounded(Ok(Some("ééééé")));
        session.bound = 5;
        let mut req = request(json!([{"role": "user", "content": "q"}]));
        enrich_chat(&session, &mut req, &asker(), &[])
            .await
            .expect("retrieval worked");
        assert_eq!(
            req.messages[0].content,
            Some(json!("éé")),
            "four bytes: the fifth would split a character"
        );

        // A session that reserved nothing injects nothing but an empty
        // message is not a way to say so: the bound is the session's own.
        assert_eq!(cut_to("abc".to_string(), 3), "abc");
        assert_eq!(cut_to("abc".to_string(), 0), "");
    }

    #[tokio::test]
    async fn a_request_is_unchanged_when_nothing_is_found_or_retrieval_is_unavailable() {
        for answer in [Ok(None), Err(EnrichmentUnavailable)] {
            let session = Fixed::grounded(answer);
            let mut req = request(json!([{"role": "user", "content": "q"}]));
            let sent = serde_json::to_value(&req).expect("json");
            let outcome = enrich_chat(&session, &mut req, &asker(), &[]).await;
            assert_eq!(
                outcome,
                answer.map(|_| None),
                "the outcome is the session's"
            );
            assert_eq!(serde_json::to_value(&req).expect("json"), sent);
        }
    }

    #[tokio::test]
    async fn nothing_is_asked_without_a_grounded_group_or_without_user_text() {
        let mut ungrounded = Fixed::grounded(Ok(Some("CONTEXT")));
        ungrounded.grounded = false;
        let mut req = request(json!([{"role": "user", "content": "q"}]));
        assert_eq!(
            enrich_chat(&ungrounded, &mut req, &asker(), &[]).await,
            Ok(None)
        );
        assert!(ungrounded.asked.lock().expect("lock").is_empty());
        assert_eq!(roles(&req), ["user"]);

        let session = Fixed::grounded(Ok(Some("CONTEXT")));
        for messages in [
            json!([{"role": "system", "content": "only a system message"}]),
            json!([{"role": "user", "content": ""}]),
        ] {
            let mut req = request(messages);
            assert_eq!(
                enrich_chat(&session, &mut req, &asker(), &[]).await,
                Ok(None)
            );
        }
        assert!(session.asked.lock().expect("lock").is_empty());
    }
}

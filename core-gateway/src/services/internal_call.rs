// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Proof that a request originated from THIS gateway process.
//!
//! Some internal self-calls need to relax governance that would otherwise
//! be self-defeating — most importantly the semantic-policy judge, which
//! must be able to read the very content a guardrail would block in order
//! to rule on it, and must not recursively evaluate policies against
//! itself.
//!
//! Those relaxations used to key off the mere *presence* of the
//! `X-Brutor-Semantic-Policy` header. That header arrives on the inbound
//! `HeaderMap` and was never stripped or validated, so any caller holding
//! a valid API key could set it and thereby skip input guardrails (PII,
//! prompt-injection, secrets, toxic-content), skip semantic-policy
//! evaluation, and force a cache bypass — a governance bypass available
//! to the very traffic the gateway exists to govern.
//!
//! The self-call is loopback (`self_call::BASE_URL` → 127.0.0.1), so it
//! always re-enters the same process. That makes a process-local random
//! token both sufficient and zero-config: the gateway mints one at first
//! use and sends it as the header value; an external client cannot guess
//! it, and there is nothing to distribute or rotate across instances.

use std::sync::OnceLock;

use axum::http::HeaderMap;
use base64::Engine as _;
use rand::RngCore as _;

static TOKEN: OnceLock<String> = OnceLock::new();

/// The value this process stamps on (and expects from) its own self-calls.
pub fn token() -> &'static str {
    TOKEN.get_or_init(|| {
        let mut buf = [0u8; 32];
        rand::rng().fill_bytes(&mut buf);
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(buf)
    })
}

/// True when `header_name` carries this process's internal token.
///
/// Compared in constant time: the token is a bearer secret for the
/// governance relaxations above, so a byte-by-byte early exit would leak
/// it to an attacker able to time repeated requests.
pub fn is_internal(headers: &HeaderMap, header_name: &str) -> bool {
    let Some(value) = headers.get(header_name).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    constant_time_eq(value.as_bytes(), token().as_bytes())
}

/// An embedding call the platform's own code makes for a caller: which
/// feature makes it and why.
///
/// The semantic cache and the Knowledge Base embed text through the
/// gateway's own governed `/v1/proxy/llm/embeddings`, with the caller's
/// credential, so the call is the caller's in every respect: tenant, model
/// access through the caller's group, limits and quotas, residency, the
/// autonomy kill switch, guardrails, its audit row and its cost. One thing is
/// not asked of it: an agent's GRANT for the embedding model. The agent did
/// not choose to call that model; the platform did, to serve a lookup the
/// agent's resource group is configured for.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum InternalEmbedding {
    /// The semantic cache embeds the prompt to search for a cached answer.
    SemanticCacheLookup,
    /// The semantic cache embeds the prompt to key a new entry.
    SemanticCacheStore,
    /// The Knowledge Base embeds the query to retrieve passages.
    KnowledgeBase,
}

impl InternalEmbedding {
    /// The feature the call is made for, as the audit row names it
    /// (`proxy_logs.tags.internal_embedding`).
    pub fn feature(self) -> &'static str {
        match self {
            Self::SemanticCacheLookup | Self::SemanticCacheStore => "semantic_cache",
            Self::KnowledgeBase => "knowledge_base",
        }
    }

    /// The semantic cache's operation (`proxy_logs.llm_cache_layer`), which
    /// tells the request-side lookup embed from the response-side store.
    pub fn cache_op(self) -> Option<&'static str> {
        match self {
            Self::SemanticCacheLookup => Some("lookup"),
            Self::SemanticCacheStore => Some("store"),
            Self::KnowledgeBase => None,
        }
    }

    fn wire(self) -> &'static str {
        match self {
            Self::SemanticCacheLookup => "semantic_cache.lookup",
            Self::SemanticCacheStore => "semantic_cache.store",
            Self::KnowledgeBase => "knowledge_base",
        }
    }

    fn from_wire(wire: &str) -> Option<Self> {
        [
            Self::SemanticCacheLookup,
            Self::SemanticCacheStore,
            Self::KnowledgeBase,
        ]
        .into_iter()
        .find(|p| p.wire() == wire)
    }
}

/// The value of `X-Brutor-Internal-Embedding` for `purpose`: the purpose and
/// this process's token. Only code running in this process can produce it.
pub fn embedding_assertion(purpose: InternalEmbedding) -> String {
    format!("{}:{}", purpose.wire(), token())
}

/// The internal embedding call this request is, if it carries this process's
/// assertion. Anything else — no header, a purpose without the token, a
/// wrong token, an unknown purpose — is an ordinary call.
pub fn internal_embedding(headers: &HeaderMap) -> Option<InternalEmbedding> {
    let value = headers
        .get(crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING)
        .and_then(|v| v.to_str().ok())?;
    let (wire, presented) = value.split_once(':')?;
    if !constant_time_eq(presented.as_bytes(), token().as_bytes()) {
        return None;
    }
    InternalEmbedding::from_wire(wire)
}

/// The platform's embedding self-call: `body` posted to this gateway's own
/// `/v1/proxy/llm/embeddings` with the caller's forwarded headers
/// (`routes::llm_helpers::embedding_self_call_headers`: the credential, the
/// tenant and the run) and the assertion for `purpose`. The one way such a
/// call is built, so that it is always attributed to the caller and always
/// marked.
pub fn embedding_self_call(
    http_client: &reqwest::Client,
    purpose: InternalEmbedding,
    forwarded: &[(String, String)],
    body: &serde_json::Value,
) -> reqwest::RequestBuilder {
    let mut req = http_client
        .post(format!(
            "{}{}",
            crate::constants::self_call::BASE_URL,
            crate::constants::routes::LLM_EMBEDDINGS
        ))
        .json(body);
    for (name, value) in forwarded {
        // The assertion is this function's to set, never a forwarded value.
        if name.eq_ignore_ascii_case(crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING) {
            continue;
        }
        req = req.header(name.as_str(), value.as_str());
    }
    req.header(
        crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING,
        embedding_assertion(purpose),
    )
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    const H: &str = "x-brutor-semantic-policy";

    fn headers_with(value: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(H, HeaderValue::from_str(value).unwrap());
        h
    }

    #[test]
    fn token_is_stable_and_unguessable() {
        assert_eq!(token(), token(), "token must be stable within a process");
        assert!(
            token().len() >= 40,
            "expect ~43 chars of base64 for 32 bytes"
        );
    }

    #[test]
    fn accepts_only_the_real_token() {
        assert!(is_internal(&headers_with(token()), H));
    }

    /// The regression this module exists for: the old check was
    /// `headers.get(...).is_some()`, so ANY value opened the gate.
    #[test]
    fn rejects_forged_values() {
        for forged in ["1", "true", "0", "", "yes", "x"] {
            assert!(
                !is_internal(&headers_with(forged), H),
                "forged value {forged:?} must not pass as internal"
            );
        }
    }

    #[test]
    fn rejects_missing_header() {
        assert!(!is_internal(&HeaderMap::new(), H));
    }

    const E: &str = crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING;

    fn embedding_headers(value: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(E, HeaderValue::from_str(value).unwrap());
        h
    }

    #[test]
    fn the_embedding_assertion_names_its_purpose_and_is_recognised() {
        for purpose in [
            InternalEmbedding::SemanticCacheLookup,
            InternalEmbedding::SemanticCacheStore,
            InternalEmbedding::KnowledgeBase,
        ] {
            let h = embedding_headers(&embedding_assertion(purpose));
            assert_eq!(internal_embedding(&h), Some(purpose));
        }
        assert_eq!(
            InternalEmbedding::SemanticCacheLookup.feature(),
            "semantic_cache"
        );
        assert_eq!(InternalEmbedding::KnowledgeBase.feature(), "knowledge_base");
        assert_eq!(
            InternalEmbedding::SemanticCacheStore.cache_op(),
            Some("store")
        );
        assert_eq!(InternalEmbedding::KnowledgeBase.cache_op(), None);
    }

    /// What a caller can write is never the assertion: a purpose alone, a
    /// purpose with a wrong or truncated token, the bare token, an unknown
    /// purpose with the right token.
    #[test]
    fn a_caller_cannot_write_the_embedding_assertion() {
        let t = token();
        for forged in [
            "1".to_string(),
            "true".to_string(),
            "semantic_cache.lookup".to_string(),
            "knowledge_base".to_string(),
            "knowledge_base:".to_string(),
            "knowledge_base:x".to_string(),
            format!("knowledge_base:{}", &t[..t.len() - 1]),
            format!("knowledge_base:{t}x"),
            t.to_string(),
            format!(":{t}"),
            format!("judge:{t}"),
        ] {
            assert_eq!(
                internal_embedding(&embedding_headers(&forged)),
                None,
                "{forged:?} passed as an internal embedding call"
            );
        }
        assert_eq!(internal_embedding(&HeaderMap::new()), None);
        // The judge's header carrying the token is not this assertion.
        assert_eq!(internal_embedding(&headers_with(t)), None);
    }

    /// The self-call carries the caller's forwarded headers and this
    /// process's assertion, and a forwarded value never stands in for it.
    #[test]
    fn the_embedding_self_call_is_marked_and_carries_what_was_forwarded() {
        let forwarded = vec![
            ("x-api-key".to_string(), "sk-caller".to_string()),
            ("x-tenant-id".to_string(), "t1".to_string()),
            (E.to_string(), "knowledge_base:forged".to_string()),
        ];
        let req = embedding_self_call(
            &reqwest::Client::new(),
            InternalEmbedding::KnowledgeBase,
            &forwarded,
            &serde_json::json!({"model": "m", "input": "q"}),
        )
        .build()
        .expect("request");
        assert_eq!(
            req.url().as_str(),
            "http://127.0.0.1:8100/v1/proxy/llm/embeddings"
        );
        assert_eq!(req.headers()["x-api-key"], "sk-caller");
        assert_eq!(req.headers()["x-tenant-id"], "t1");
        let marks: Vec<_> = req.headers().get_all(E).iter().collect();
        assert_eq!(marks.len(), 1, "one assertion, ours");
        let mut inbound = HeaderMap::new();
        inbound.insert(E, marks[0].clone());
        assert_eq!(
            internal_embedding(&inbound),
            Some(InternalEmbedding::KnowledgeBase)
        );
    }

    #[test]
    fn constant_time_eq_matches_semantics() {
        assert!(constant_time_eq(b"abc", b"abc"));
        assert!(!constant_time_eq(b"abc", b"abd"));
        assert!(!constant_time_eq(b"abc", b"ab"));
    }
}

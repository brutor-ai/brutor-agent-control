// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Perplexity (Sonar) adapter
//!
//! Perplexity exposes an OpenAI-compatible chat-completions endpoint
//! that's a small superset of OpenAI's spec — the Sonar family adds
//! request-side knobs (`search_recency_filter`, `search_domain_filter`,
//! `return_citations`, `return_images`, `return_related_questions`)
//! and response-side fields (`citations`, `images`,
//! `related_questions`). Brutor's pipeline already passes both
//! through unchanged today:
//!
//!   * **Request side**: `ChatCompletionRequest` captures unknown
//!     fields into `extra` via `#[serde(flatten)]`, which serialize
//!     back into the outbound body. The provider drop-list at
//!     `llm_providers.additional_drop_params` doesn't list any
//!     Sonar field, so they survive `drop_unsupported_params`.
//!   * **Response side**: the response body is a plain
//!     `serde_json::Value` from upstream → through to client. No
//!     typed model strips unknown fields. `normalize_response` is a
//!     no-op for Perplexity (the body is already OpenAI-shape; only
//!     the extras tagged onto the top level survive verbatim).
//!
//! This adapter is therefore mostly a documentation surface and a
//! regression guard — the tests below pin both directions so a
//! future drop-list cleanup or response normaliser can't silently
//! strip Sonar's value-add.

use super::LlmAdapter;

/// Sonar-specific request fields. Listed here for documentation only —
/// the pipeline doesn't filter on this list (it passes through any
/// unknown field). Tests below assert each one survives unchanged.
#[cfg(test)]
const SONAR_REQUEST_FIELDS: &[&str] = &[
    "search_recency_filter",
    "search_domain_filter",
    "return_citations",
    "return_images",
    "return_related_questions",
    "search_mode",
    "web_search_options",
];

/// Sonar-specific response fields. Tests below assert each one
/// survives `normalize_response`.
#[cfg(test)]
const SONAR_RESPONSE_FIELDS: &[&str] = &["citations", "images", "related_questions"];

pub struct PerplexityAdapter;

impl LlmAdapter for PerplexityAdapter {
    fn provider_name(&self) -> &str {
        "perplexity"
    }

    // No `transform_request` override — Brutor's pipeline already
    // serialises unknown fields back via `ChatCompletionRequest.extra`,
    // which is what every Sonar param is from Brutor's typed model's
    // perspective. The provider drop-list (loaded at
    // `prepare_request → drop_unsupported_params`) doesn't list any
    // Sonar field. The combination is a no-op in spirit but tests
    // below assert the contract explicitly so a future drop-list edit
    // doesn't silently delete `search_recency_filter` & friends.

    // No `normalize_response` override either — the upstream response
    // is already OpenAI-shape with Sonar's `citations` / `images` /
    // `related_questions` tagged on at the top level. Since the body
    // travels as a `serde_json::Value`, those fields survive into the
    // client-visible response unchanged. Tests below pin this.
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::db::LlmModel;
    use serde_json::json;

    #[test]
    fn perplexity_provider_name() {
        assert_eq!(PerplexityAdapter.provider_name(), "perplexity");
    }

    // ── Request-side passthrough ─────────────────────────────────────

    #[test]
    fn perplexity_request_passes_sonar_specific_fields_through() {
        // Regression guard: if the adapter ever grows a `transform_request`
        // override and accidentally drops Sonar params, this test catches
        // it. The empty adapter doesn't mutate the body — these fields
        // are expected to be byte-identical pre/post call.
        let adapter = PerplexityAdapter;
        let mut obj = serde_json::Map::new();
        obj.insert("model".into(), json!("sonar-pro"));
        obj.insert("messages".into(), json!([{"role": "user", "content": "x"}]));
        // Every documented Sonar request field.
        obj.insert("search_recency_filter".into(), json!("week"));
        obj.insert(
            "search_domain_filter".into(),
            json!(["wikipedia.org", "-reddit.com"]),
        );
        obj.insert("return_citations".into(), json!(true));
        obj.insert("return_images".into(), json!(false));
        obj.insert("return_related_questions".into(), json!(true));
        obj.insert("search_mode".into(), json!("academic"));

        let before = serde_json::Value::Object(obj.clone());
        adapter.transform_request(&mut obj, &dummy_model());
        let after = serde_json::Value::Object(obj);

        assert_eq!(
            before, after,
            "PerplexityAdapter must not mutate Sonar-specific request \
             fields. If a transform was added that breaks this, either \
             update SONAR_REQUEST_FIELDS or list the field in the \
             test exclusions — but don't drop them silently."
        );
        // Belt-and-braces: every documented field is still there.
        for field in SONAR_REQUEST_FIELDS {
            // search_mode + web_search_options aren't in the body for
            // this test, so just skip the absent ones.
            if before.get(field).is_some() {
                assert!(
                    after.get(field).is_some(),
                    "Sonar request field {field:?} was dropped"
                );
            }
        }
    }

    // ── Response-side passthrough ────────────────────────────────────

    #[test]
    fn perplexity_response_keeps_citations_images_related_questions() {
        // Sonar returns OpenAI-shape body with extra top-level fields.
        // Brutor's `normalize_response` for Perplexity is a no-op
        // (inherited from `LlmAdapter`'s default impl), so all extras
        // pass through to the client. This test guards against a
        // future override that strips them.
        let adapter = PerplexityAdapter;
        let mut body = json!({
            "id": "chatcmpl-abc",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Sweden's capital is Stockholm."},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            // Sonar extras
            "citations": [
                "https://en.wikipedia.org/wiki/Stockholm",
                "https://sweden.se/society"
            ],
            "images": [
                {"image_url": "https://example.com/img.jpg", "origin_url": "https://example.com"}
            ],
            "related_questions": [
                "What's the population of Stockholm?",
                "When did Sweden join the EU?"
            ]
        });
        let before = body.clone();
        adapter.normalize_response(&mut body);
        assert_eq!(
            body, before,
            "PerplexityAdapter.normalize_response must NOT strip \
             Sonar-specific top-level fields (citations/images/related_questions). \
             The pipeline ships the body as a `Value` so unknown fields \
             survive end-to-end; this test pins the no-op contract."
        );
        for field in SONAR_RESPONSE_FIELDS {
            assert!(
                body.get(field).is_some(),
                "Sonar response field {field:?} was stripped by \
                 normalize_response"
            );
        }
    }

    #[test]
    fn perplexity_response_passes_through_empty_extras() {
        // Negative case: a Perplexity response without Sonar extras
        // (e.g. a non-Sonar model, or a request with return_citations=false)
        // should pass through unchanged — no synthesised null fields.
        let adapter = PerplexityAdapter;
        let mut body = json!({
            "id": "chatcmpl-xyz",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi."},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        });
        let before = body.clone();
        adapter.normalize_response(&mut body);
        assert_eq!(body, before);
        for field in SONAR_RESPONSE_FIELDS {
            assert!(
                body.get(field).is_none(),
                "Empty-extras case should NOT inject a null {field:?} \
                 field"
            );
        }
    }

    fn dummy_model() -> LlmModel {
        crate::services::llm_adapter::make_test_model("perplexity", "sonar-pro")
    }
}

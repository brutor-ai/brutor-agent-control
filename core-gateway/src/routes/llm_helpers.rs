// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # LLM Helper Functions
//!
//! Shared utilities extracted from the LLM route handlers to reduce duplication
//! across chat completions, responses, and completions endpoints.

use axum::http::HeaderMap;

use crate::models::db::ProxyLogInsert;
use crate::services::context_enrichment::EnrichmentRecord;

/// True when the caller has explicitly opted out of cache via
/// header. Recognises:
///
///   * `X-Brutor-Cache: off|no|false|0|disabled` — platform-specific
///     opt-out for one-off requests and audit replays.
///   * `Cache-Control: no-cache | no-store` — HTTP-standard.
///   * `X-Brutor-Internal-Health-Check: 1` — the gateway-control
///     health-check service stamps this on every probe. Without
///     skipping the cache here, repeated probes against the same
///     model would write into the semantic cache (the probe text
///     "test" / "ping" / "health check") and then read back as
///     hits — polluting cache stats and the cache itself with
///     synthetic traffic operators didn't ask for. The skip-on-
///     probe rule applies symmetrically: don't lookup, don't store.
///
/// When any of the above is present we skip BOTH lookup and store —
/// the caller gets a fresh model invocation and the cache stays
/// clean.
pub fn cache_disabled_by_header(headers: &HeaderMap) -> bool {
    if let Some(v) = headers.get("x-brutor-cache").and_then(|h| h.to_str().ok()) {
        let v = v.trim().to_ascii_lowercase();
        if matches!(v.as_str(), "off" | "no" | "false" | "0" | "disabled") {
            return true;
        }
    }
    if let Some(v) = headers.get("cache-control").and_then(|h| h.to_str().ok()) {
        let v = v.to_ascii_lowercase();
        if v.contains("no-cache") || v.contains("no-store") {
            return true;
        }
    }
    if headers
        .get("x-brutor-internal-health-check")
        .and_then(|h| h.to_str().ok())
        .map(|v| !v.trim().is_empty() && v.trim() != "0")
        .unwrap_or(false)
    {
        return true;
    }
    // Semantic-policy JUDGE self-calls (marked `X-Brutor-Semantic-Policy`) must
    // NEVER touch the semantic response cache — neither lookup nor store. The
    // judge verdict is a per-content decision, but the cache matches by vector
    // SIMILARITY: caching it lets one prompt's block/allow verdict be served to
    // a *different* prompt the judge never saw. That is both a false-positive
    // source (a benign call inherits a malicious call's block) AND a governance
    // BYPASS (a malicious call inherits a benign call's allow). Routing every
    // judge self-call through the cache-disabled path (all lookup + store gates
    // read this helper) forces a fresh judgment on every governed request.
    // Verified internal origin — see services/internal_call.rs. (Header
    // presence alone was client-forgeable, which also made cache-bypass
    // a free lever for any caller.)
    if crate::services::internal_call::is_internal(
        headers,
        crate::constants::headers::X_BRUTOR_SEMANTIC_POLICY,
    ) {
        return true;
    }
    false
}

/// Extract authorization-related headers for forwarding to the gateway's
/// self-calls (the semantic-policy judge's chat call): the caller's
/// credential and the tenant, and nothing else.
pub fn extract_cache_auth_headers(headers: &HeaderMap) -> Vec<(String, String)> {
    forwarded(headers, &[])
}

/// What the platform's embedding self-calls forward (the semantic cache's
/// and the Knowledge Base's): the caller's credential and tenant, as
/// [`extract_cache_auth_headers`], and the run — the signed delegation chain
/// [`self_call_headers`] put in `headers`, so the embedding is recorded as a
/// step of the caller's run.
pub fn embedding_self_call_headers(headers: &HeaderMap) -> Vec<(String, String)> {
    use crate::services::a2a_delegation as chain;
    forwarded(
        headers,
        &[
            chain::HEADER_ROOT,
            chain::HEADER_PARENT,
            chain::HEADER_DEPTH,
            chain::HEADER_SIG,
            chain::HEADER_ACTOR,
            chain::HEADER_SUBJECT,
        ],
    )
}

/// What the semantic-policy judge's self-call forwards for the request it
/// rules on: the caller's credential and tenant and a signed child of THIS
/// step's delegation chain ([`self_call_headers`]), so the judge's chat call
/// is recorded as a step of the caller's run and not as a run of its own.
/// At the depth ceiling the chain cannot be
/// extended and the call carries none.
pub fn judge_self_call_headers(
    headers: &HeaderMap,
    call_chain: &crate::services::a2a_delegation::CallChain,
    current_task_id: ulid::Ulid,
    tenant_key: &[u8],
) -> Vec<(String, String)> {
    embedding_self_call_headers(&self_call_headers(
        headers,
        call_chain,
        current_task_id,
        tenant_key,
    ))
}

/// [`judge_self_call_headers`] for a surface that holds the child chain of
/// its own step instead of the chain it arrived with:
/// A2A outbound forks the chain it sends the peer before it asks the
/// judge, and an A2A v1 step is a task, not a parsed chain. `None` (the
/// step cannot be extended, or has no id to fork from) forwards the
/// credential alone.
pub fn judge_self_call_headers_for_child(
    headers: &HeaderMap,
    child: Option<&crate::services::a2a_delegation::CallChain>,
    tenant_key: &[u8],
) -> Vec<(String, String)> {
    embedding_self_call_headers(&with_child_chain(headers, child, tenant_key))
}

fn forwarded(headers: &HeaderMap, also: &[&str]) -> Vec<(String, String)> {
    let names = [
        "authorization",
        "x-api-key",
        "x-gateway-authorization",
        "x-tenant-id",
    ];
    headers
        .iter()
        .filter(|(name, _)| {
            let name = name.as_str();
            names.contains(&name) || also.contains(&name)
        })
        .filter_map(|(name, value)| {
            value
                .to_str()
                .ok()
                .map(|v| (name.to_string(), v.to_string()))
        })
        .collect()
}

/// The request's headers as the platform's self-calls made for it see them:
/// the caller's own, with the delegation chain of THIS request's step in
/// place of whatever chain or run assertion arrived. A self-call that
/// forwards them ([`embedding_self_call_headers`]) is verified by the
/// embeddings route as a child of this step, so its audit row names the
/// caller's run (`root_task_id`), this step as its parent and one more level
/// of depth ("attributed").
///
/// A chain that is already at the depth ceiling cannot be extended; the
/// headers then carry no chain and the self-call is recorded as its own run.
pub fn self_call_headers(
    headers: &HeaderMap,
    call_chain: &crate::services::a2a_delegation::CallChain,
    current_task_id: ulid::Ulid,
    tenant_key: &[u8],
) -> HeaderMap {
    with_child_chain(
        headers,
        call_chain.fork_child(current_task_id).ok().as_ref(),
        tenant_key,
    )
}

/// `headers` with whatever chain or run assertion arrived replaced by
/// `child`, signed; by none when there is no child.
fn with_child_chain(
    headers: &HeaderMap,
    child: Option<&crate::services::a2a_delegation::CallChain>,
    tenant_key: &[u8],
) -> HeaderMap {
    use crate::services::a2a_delegation as chain;
    let mut out = headers.clone();
    for name in [
        chain::HEADER_ROOT,
        chain::HEADER_PARENT,
        chain::HEADER_DEPTH,
        chain::HEADER_SIG,
        chain::HEADER_ACTOR,
        chain::HEADER_SUBJECT,
        chain::HEADER_RUN_ID,
    ] {
        out.remove(name);
    }
    if let Some(child) = child {
        for (name, value) in child.to_header_pairs(tenant_key) {
            if let Ok(value) = axum::http::HeaderValue::from_str(&value) {
                out.insert(name, value);
            }
        }
    }
    out
}

/// Extract `(server_config_id, tool_name)` pairs from a
/// `/chat/completions` response body's `choices[0].message.tool_calls`.
///
/// Returns an empty vec when the response did not invoke tools. The
/// `server_config_id` slot is intentionally left blank for now —
/// resolving tool-name → MCP-server requires a catalog lookup we
/// haven't wired here yet, so per-server cache rules that target a
/// *specific* server_config_id won't match responses from this path.
/// Per-tool wildcards (`server_config_id = None`) still work.
pub fn extract_chat_tool_calls(response: &serde_json::Value) -> Vec<(String, String)> {
    let calls = response
        .get("choices")
        .and_then(|c| c.get(0))
        .and_then(|c| c.get("message"))
        .and_then(|m| m.get("tool_calls"))
        .and_then(|tc| tc.as_array());
    let Some(calls) = calls else {
        return Vec::new();
    };
    calls
        .iter()
        .filter_map(|tc| {
            let name = tc
                .get("function")
                .and_then(|f| f.get("name"))
                .and_then(|n| n.as_str())?;
            Some((String::new(), name.to_string()))
        })
        .collect()
}

/// Extract `(server_config_id, tool_name)` pairs from a `/responses`
/// response body's `output` array (items of `type == "function_call"`).
pub fn extract_responses_tool_calls(response: &serde_json::Value) -> Vec<(String, String)> {
    let items = response.get("output").and_then(|o| o.as_array());
    let Some(items) = items else {
        return Vec::new();
    };
    items
        .iter()
        .filter(|i| i.get("type").and_then(|t| t.as_str()) == Some("function_call"))
        .filter_map(|i| {
            i.get("name")
                .and_then(|n| n.as_str())
                .map(|s| s.to_string())
        })
        .map(|name| (String::new(), name))
        .collect()
}

/// True when `finish_reason` indicates the model invoked tools. Works
/// for both OpenAI (`tool_calls`) and legacy OpenAI (`function_call`).
pub fn finish_reason_is_tool_call(finish_reason: Option<&str>) -> bool {
    matches!(finish_reason, Some("tool_calls") | Some("function_call"))
}

/// True when the chat request shows a tool roundtrip already in
/// progress — i.e. the `messages[]` history contains evidence that
/// the LLM previously invoked a tool and the client is now sending
/// the result back for synthesis.
///
/// **Why we need this for the cache gate:** the standard OpenAI
/// tool-calling pattern uses two chat-completion requests:
///   1. LLM responds with `tool_calls=[…]`, no content. Gate fires
///      on the response and store is correctly skipped.
///   2. Client appends a `role: "tool"` message with the result and
///      sends a SECOND request. The LLM's final response has
///      `tool_calls=null` and pure text. `extract_chat_tool_calls`
///      sees an empty list and `should_cache_tool_response` thinks
///      "no tool invoked → cache it" — but the response is in fact a
///      narration of the tool's output (skill output, MCP tool
///      output, etc.) and IS contaminated by side-effecting
///      computation.
///
/// This helper looks at the request side instead: if any prior
/// assistant message has `tool_calls`, OR any message has `role:
/// "tool"`, the response we're about to cache is the back-end of a
/// tool roundtrip. Treat it as if a tool was invoked, regardless of
/// the final response shape.
pub fn chat_request_has_tool_history(
    request: &crate::models::proxy::ChatCompletionRequest,
) -> bool {
    for msg in &request.messages {
        if msg.role == "tool" {
            return true;
        }
        if msg.role == "assistant" {
            // Check the structured `tool_calls` field on the message.
            // ChatMessage's `tool_calls` is `Option<Value>` so that
            // both OpenAI shapes (array of {id, type, function:{...}})
            // and Anthropic-style shapes pass through unchanged.
            if let Some(tc) = msg.tool_calls.as_ref() {
                let is_present = match tc {
                    serde_json::Value::Array(arr) => !arr.is_empty(),
                    serde_json::Value::Null => false,
                    _ => true,
                };
                if is_present {
                    return true;
                }
            }
        }
    }
    false
}

/// True when the chat request already has prior conversation
/// context — i.e. anything other than a fresh single-user-turn
/// request (with an optional system prompt).
///
/// **Why we need this:** the semantic cache stores a response
/// keyed on the entire `messages[]` embedding. For a continuation
/// turn, *most* of the embedding text is the prior conversation
/// — only the latest user line differs. Cosine similarity between
/// turn N and turn N+1 routinely exceeds 90% because they share
/// 90% of the input. Result: a stale cached response from an
/// earlier turn gets returned for an entirely different new
/// question, e.g. the "weekend trip recommendation" answer comes
/// back when the user asks "is there a better time to visit?".
///
/// Per the proxy-error-taxonomy memory we discussed — the right
/// fix is to keep the cache as a single-turn FAQ front door: only
/// store responses where there's no prior context to bias the
/// next continuation's lookup. Multi-turn requests can still
/// LOOK UP (and miss); they just don't STORE. The hit rate on
/// continuations is intentionally zero, which is the correct
/// outcome — context-dependent answers shouldn't be served from
/// cache to a different conversation that happens to share
/// vocabulary.
pub fn chat_request_is_continuation(request: &crate::models::proxy::ChatCompletionRequest) -> bool {
    let mut user_count = 0;
    for msg in &request.messages {
        match msg.role.as_str() {
            "assistant" | "tool" => return true,
            "user" => {
                user_count += 1;
                if user_count > 1 {
                    return true;
                }
            }
            _ => {} // system / unknown — don't count
        }
    }
    false
}

/// Same question as [`chat_request_is_continuation`] for the generic JSON
/// body used by `/responses` and `/completions`.
///
/// The conversation of such a body is in one of three places:
///
/// * `messages` — the chat shape;
/// * `input` — the `/responses` shape: a string (one question), or a list
///   of items: messages with a `role`, and typed items that are earlier
///   model output or tool traffic (`function_call`, `function_call_output`,
///   `reasoning`, …);
/// * at the provider — `previous_response_id` or `conversation` names
///   earlier turns this request does not carry at all.
///
/// Any of them makes the request a continuation (only `messages` was
/// read, so the answer to a multi-turn `/responses` request was stored). An
/// `input` item this function does not recognise counts as conversation: the
/// consequence of this answer is "do not store", the safe side.
pub fn chat_body_is_continuation(body: &serde_json::Value) -> bool {
    if names_a_provider_side_conversation(body) {
        return true;
    }
    let mut user_count = 0;
    if let Some(messages) = body.get("messages").and_then(|m| m.as_array()) {
        for msg in messages {
            let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
            match role {
                "assistant" | "tool" => return true,
                "user" => user_count += 1,
                _ => {}
            }
        }
    }
    if let Some(items) = body.get("input").and_then(|i| i.as_array()) {
        for item in items {
            match input_item_kind(item) {
                InputItem::Message("user") => user_count += 1,
                // Instructions are not a turn.
                InputItem::Message("system" | "developer") => {}
                // An assistant message, any role this does not know, tool
                // traffic, earlier output, or an item of no known shape.
                InputItem::Message(_) | InputItem::ToolTraffic | InputItem::Other => return true,
            }
        }
    }
    // A bare `prompt` (`/completions`) or a string `input` is one question.
    user_count > 1
}

/// Same question as [`chat_request_has_tool_history`] for the generic JSON
/// body: a `tool` message or an assistant message with tool calls in
/// `messages`, or tool traffic among the `/responses` `input` items.
pub fn chat_body_has_tool_history(body: &serde_json::Value) -> bool {
    if let Some(messages) = body.get("messages").and_then(|m| m.as_array()) {
        for msg in messages {
            let role = msg.get("role").and_then(|r| r.as_str()).unwrap_or("");
            if role == "tool" {
                return true;
            }
            if role == "assistant"
                && let Some(tc) = msg.get("tool_calls")
            {
                let is_present = match tc {
                    serde_json::Value::Array(arr) => !arr.is_empty(),
                    serde_json::Value::Null => false,
                    _ => true,
                };
                if is_present {
                    return true;
                }
            }
        }
    }
    if let Some(items) = body.get("input").and_then(|i| i.as_array()) {
        return items
            .iter()
            .any(|item| matches!(input_item_kind(item), InputItem::ToolTraffic));
    }
    false
}

/// What one item of a `/responses` `input` list is, for the cache's purposes.
enum InputItem<'a> {
    /// A message, with its role.
    Message(&'a str),
    /// A tool call or a tool's output (`function_call`,
    /// `function_call_output`, `mcp_call`, `computer_call_output`, …).
    ToolTraffic,
    /// Anything else: reasoning, an item reference, a shape not known here.
    Other,
}

fn input_item_kind(item: &serde_json::Value) -> InputItem<'_> {
    let kind = item.get("type").and_then(|t| t.as_str());
    let role = item.get("role").and_then(|r| r.as_str());
    match (kind, role) {
        // `type` is optional on a message ("easy input message").
        (None | Some("message"), Some("tool")) => InputItem::ToolTraffic,
        (None | Some("message"), Some(role)) => InputItem::Message(role),
        (Some(kind), _) if kind.ends_with("_call") || kind.ends_with("_call_output") => {
            InputItem::ToolTraffic
        }
        _ => InputItem::Other,
    }
}

/// True when the body points at earlier turns kept by the provider.
fn names_a_provider_side_conversation(body: &serde_json::Value) -> bool {
    ["previous_response_id", "conversation"]
        .iter()
        .any(|key| body.get(key).is_some_and(|v| !v.is_null()))
}

/// Extract prompt and completion token counts from an LLM response body.
///
/// Handles both OpenAI-style (`prompt_tokens`/`completion_tokens`) and
/// Anthropic-style (`input_tokens`/`output_tokens`) field names.
pub fn extract_token_counts(response: &serde_json::Value) -> (Option<i32>, Option<i32>) {
    let usage_obj = response.get("usage");
    let prompt_tokens = usage_obj
        .and_then(|u| u.get("prompt_tokens").or_else(|| u.get("input_tokens")))
        .and_then(|v| v.as_i64())
        .map(|v| v as i32);
    let completion_tokens = usage_obj
        .and_then(|u| {
            u.get("completion_tokens")
                .or_else(|| u.get("output_tokens"))
        })
        .and_then(|v| v.as_i64())
        .map(|v| v as i32);
    (prompt_tokens, completion_tokens)
}

/// Estimate the cost of an LLM call given token counts and per-token pricing.
///
/// `cost_per_input` / `cost_per_output` are USD per **one million** tokens,
/// matching `llm_model.cost_per_input_token` in the catalog.
///
/// Returns `None` only when we know NOTHING about the call's token usage.
/// A missing *side* is priced at zero rather than voiding the whole
/// estimate, because a `None` here is not "unknown, ask later" — it is
/// written to `proxy_logs.llm_estimated_cost_usd` and charged to every
/// budget counter as **$0**. So the old `zip` semantics (None if either
/// side is absent) silently under-BILLED exactly the cases where a side is
/// legitimately absent: embeddings have no completion tokens at all, and
/// some providers close an SSE stream having reported only one of the two.
/// Pricing the known side and treating the unknown one as zero
/// under-counts one term instead of discarding the whole charge — the
/// smaller and more honest of the two errors.
///
/// This is the single definition of the formula. Every surface (chat,
/// responses, embeddings, native Anthropic, native Gemini, streaming and
/// non-streaming) must route through it, so that a partially-reported usage
/// block costs the same everywhere.
pub fn estimate_llm_cost(
    prompt_tokens: Option<i32>,
    completion_tokens: Option<i32>,
    cost_per_input: f64,
    cost_per_output: f64,
) -> Option<f64> {
    if prompt_tokens.is_none() && completion_tokens.is_none() {
        return None;
    }
    let pt = prompt_tokens.unwrap_or(0) as f64;
    let ct = completion_tokens.unwrap_or(0) as f64;
    Some(pt * cost_per_input / 1_000_000.0 + ct * cost_per_output / 1_000_000.0)
}

/// Estimate the cost savings from a cache hit based on tokens saved.
///
/// Uses the average of input and output cost per token as an approximation.
pub fn estimate_cache_savings_cost(
    tokens_saved: i32,
    cost_per_input: f64,
    cost_per_output: f64,
) -> f64 {
    let avg = (cost_per_input + cost_per_output) / 2.0;
    tokens_saved as f64 * avg / 1_000_000.0
}

/// Token usage as reported by an OpenAI Responses API body:
/// `usage.input_tokens` / `output_tokens` / `total_tokens`, with
/// `input_tokens_details.cached_tokens` and
/// `output_tokens_details.reasoning_tokens`.
///
/// The `/v1/proxy/llm/responses` surface can also be answered in chat
/// shape — providers that don't speak Responses are bridged to
/// `/chat/completions` upstream and their body comes back untranslated —
/// so the chat names (`prompt_tokens` / `completion_tokens` /
/// `prompt_tokens_details.cached_tokens`) are accepted as fallbacks. One
/// parser for both shapes, used by the streaming and non-streaming
/// branches alike, is what keeps `proxy_logs`, `usage_logs` and the
/// budget counters reporting the same numbers for a call.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ResponsesUsage {
    pub input_tokens: Option<i32>,
    pub output_tokens: Option<i32>,
    /// The provider's `total_tokens` when present, else the sum of the
    /// sides that were reported (a lone side counts as the total rather
    /// than voiding it — see `estimate_llm_cost` for why a partial usage
    /// block must never collapse to nothing).
    pub total_tokens: Option<i32>,
    /// Input tokens served from the provider's prompt cache (0 when the
    /// provider reported none).
    pub cached_input_tokens: i32,
    /// Output tokens spent on hidden reasoning (0 when not reported).
    pub reasoning_output_tokens: i32,
}

impl ResponsesUsage {
    /// Parse the `usage` block of a Responses (or chat-shaped) body. A
    /// body without an object-valued `usage` — an error envelope, or a
    /// `response.created` stream event where `usage` is `null` — yields
    /// the empty value, which `is_empty` reports.
    pub fn from_body(body: &serde_json::Value) -> Self {
        let Some(usage) = body.get("usage").filter(|u| u.is_object()) else {
            return Self::default();
        };
        let int = |v: Option<&serde_json::Value>| v.and_then(|v| v.as_i64()).map(|v| v as i32);
        let input_tokens = int(usage
            .get("input_tokens")
            .or_else(|| usage.get("prompt_tokens")));
        let output_tokens = int(usage
            .get("output_tokens")
            .or_else(|| usage.get("completion_tokens")));
        let total_tokens = int(usage.get("total_tokens")).or(match (input_tokens, output_tokens) {
            (Some(p), Some(c)) => Some(p + c),
            (p, c) => p.or(c),
        });
        let cached_input_tokens = int(usage
            .pointer("/input_tokens_details/cached_tokens")
            .or_else(|| usage.pointer("/prompt_tokens_details/cached_tokens")))
        .unwrap_or(0);
        let reasoning_output_tokens = int(usage
            .pointer("/output_tokens_details/reasoning_tokens")
            .or_else(|| usage.pointer("/completion_tokens_details/reasoning_tokens")))
        .unwrap_or(0);
        Self {
            input_tokens,
            output_tokens,
            total_tokens,
            cached_input_tokens,
            reasoning_output_tokens,
        }
    }

    /// True when the provider reported no token count at all.
    pub fn is_empty(&self) -> bool {
        self.input_tokens.is_none() && self.output_tokens.is_none()
    }
}

/// Stamp token counts and the shared cost estimate on a proxy log row.
///
/// Returns the estimate so the caller charges the SAME figure to
/// `usage_logs` and the budget counters. The run ledger
/// (`ai_system_runs.total_cost_usd`), the assurance cost signals and
/// Mission Control's run cost all read `proxy_logs`; the budget gate
/// reads `usage_logs`. A surface that fills one and not the other
/// produces a 429 "budget exhausted" against runs whose ledger says $0 —
/// which is exactly what the `/responses` surface did before this
/// helper existed.
pub fn populate_llm_usage_fields(
    log: &mut ProxyLogInsert,
    usage: &ResponsesUsage,
    model: &crate::models::db::LlmModel,
) -> Option<f64> {
    log.llm_prompt_tokens = usage.input_tokens;
    log.llm_completion_tokens = usage.output_tokens;
    log.llm_total_tokens = usage.total_tokens;
    log.llm_estimated_cost_usd = estimate_llm_cost(
        usage.input_tokens,
        usage.output_tokens,
        model.cost_per_input_token.unwrap_or(0.0),
        model.cost_per_output_token.unwrap_or(0.0),
    );
    log.llm_estimated_cost_usd
}

/// Stamp provider prompt-cache counters on a proxy log row and record the
/// Prometheus series, when the provider reported any.
///
/// Zero counters skip the fields so Mission Control's aggregates aren't
/// filled with rows that look like "cache supported but not hitting" for
/// providers that never report cache usage at all. Returns true when the
/// fields were written.
pub fn apply_prompt_cache_usage(
    log: &mut ProxyLogInsert,
    pc_usage: &crate::services::prompt_cache_usage::PromptCacheUsage,
    mechanism: crate::services::prompt_cache_usage::PromptCacheMechanism,
    provider: &str,
) -> bool {
    if pc_usage.write_tokens <= 0 && pc_usage.read_tokens <= 0 {
        return false;
    }
    use crate::services::proxy_metrics;
    log.prompt_cache_write_tokens = Some(pc_usage.write_tokens);
    log.prompt_cache_read_tokens = Some(pc_usage.read_tokens);
    log.prompt_cache_savings_usd = Some(pc_usage.savings_usd);
    let mech_label = proxy_metrics::mechanism_label(mechanism);
    proxy_metrics::record_prompt_cache_tokens("read", mech_label, provider, pc_usage.read_tokens);
    proxy_metrics::record_prompt_cache_tokens("write", mech_label, provider, pc_usage.write_tokens);
    proxy_metrics::record_prompt_cache_savings(mech_label, provider, pc_usage.savings_usd);
    true
}

/// Build the audit row for a semantic-cache hit that short-circuited an
/// LLM call. Shared by the chat-completions and Responses surfaces so a
/// hit on either records the same evidence: the layer that answered,
/// the tokens it saved, the cost that would otherwise have been paid,
/// the transport the client asked for, and body previews of what was
/// asked and what was served. Callers still stamp call chain, residency
/// and PII on the returned row, as they do for every other row.
#[allow(clippy::too_many_arguments)]
pub fn build_semantic_cache_hit_log(
    endpoint: &str,
    proxy_subtype: &str,
    auth: &crate::models::auth::AuthContext,
    target_group: &Option<crate::models::db::ResourceGroup>,
    headers: &HeaderMap,
    model: &crate::models::db::LlmModel,
    model_requested: Option<String>,
    request_value: &serde_json::Value,
    result: &crate::services::response_cache::CacheHit,
    streaming: bool,
    duration_ms: f64,
    correlation_id: &str,
) -> ProxyLogInsert {
    use crate::services::proxy_logger;
    let mut log = proxy_logger::build_proxy_log(
        "llm",
        auth,
        "POST",
        endpoint,
        None,
        Some(&model.id),
        200,
        duration_ms,
        true,
        None,
        correlation_id,
    );
    log.proxy_subtype = Some(proxy_subtype.to_string());
    populate_group_id(&mut log, target_group);
    log.user_agent = proxy_logger::extract_user_agent(headers);
    log.llm_model_requested = model_requested;
    log.llm_model_used = Some(model.model_name.clone());
    log.llm_provider = Some(model.provider.clone());
    log.llm_cache_hit = Some(true);
    log.llm_cache_layer = Some(result.layer.as_str().to_string());
    log.llm_tokens_saved = Some(result.tokens_saved);
    log.llm_cache_similarity_score = result.similarity_score;
    // The cache short-circuits before the streaming/non-streaming branch,
    // so the request's intended transport is recorded here explicitly —
    // `populate_llm_request_fields` doesn't set it, and without it every
    // cache-hit row shows `llm_streaming = NULL`, hiding the streaming
    // path when telemetry is sliced by transport.
    log.llm_streaming = Some(streaming);
    // Cost that would have been incurred without the cache.
    log.llm_estimated_cost_usd = Some(estimate_cache_savings_cost(
        result.tokens_saved,
        model.cost_per_input_token.unwrap_or(0.0),
        model.cost_per_output_token.unwrap_or(0.0),
    ));
    proxy_logger::populate_llm_request_fields(&mut log, request_value);
    // Body previews — the normal forward path that sets these is skipped
    // on a hit, and a row reading "No preview available" for both bodies
    // is not inspectable. Record the request as asked and the cached
    // response as served.
    let req_str = serde_json::to_string(request_value).unwrap_or_default();
    log.request_size_bytes = Some(req_str.len() as i32);
    log.request_body_preview = Some(proxy_logger::body_preview(&req_str));
    let resp_str = serde_json::to_string(&result.response).unwrap_or_default();
    log.response_size_bytes = Some(resp_str.len() as i32);
    log.response_body_preview = Some(proxy_logger::body_preview(&resp_str));
    log
}

/// Populate RAG-related fields on a proxy log entry.
///
/// If the request was given retrieved context, sets the enabled flag, chunk
/// count, context tokens, collection IDs, top similarity score, and retrieval
/// duration. These columns are part of the evidence record in both editions;
/// they stay null for a request nothing was retrieved for.
pub fn populate_rag_log_fields(log: &mut ProxyLogInsert, enrichment: &Option<EnrichmentRecord>) {
    if let Some(rag) = enrichment {
        log.rag_enabled = Some(true);
        log.rag_chunks_retrieved = Some(rag.chunks_retrieved as i32);
        log.rag_context_tokens = Some(rag.context_tokens as i32);
        log.rag_collection_ids = Some(rag.collection_ids.join(","));
        log.rag_top_similarity_score = Some(rag.top_similarity_score);
        log.rag_retrieval_duration_ms = Some(rag.retrieval_ms);
    }
}

/// Set the resource group ID on a proxy log from the target group.
pub fn populate_group_id(
    log: &mut crate::models::db::ProxyLogInsert,
    target_group: &Option<crate::models::db::ResourceGroup>,
) {
    log.resource_group_id = target_group.as_ref().map(|g| g.id.clone());
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The platform's self-call for a request is a step of that request's
    /// run: the headers it forwards verify as a child of the request's step,
    /// whatever chain or run assertion the caller sent, and carry the
    /// caller's credential and tenant and nothing else of the caller's.
    #[test]
    fn a_self_call_forwards_the_callers_credential_and_the_requests_run() {
        use crate::services::a2a_delegation::{
            CallChain, DelegationChain, HEADER_PARENT, HEADER_ROOT, HEADER_RUN_ID,
        };
        let key = b"tenant-key";
        let mut inbound = HeaderMap::new();
        inbound.insert("x-api-key", "sk-caller".parse().unwrap());
        inbound.insert("x-tenant-id", "t1".parse().unwrap());
        inbound.insert("x-custom", "not forwarded".parse().unwrap());
        // What the caller sent about runs is not what the self-call carries.
        inbound.insert(HEADER_RUN_ID, "the-callers-label".parse().unwrap());
        inbound.insert(HEADER_ROOT, "forged".parse().unwrap());
        inbound.insert(
            crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING,
            "knowledge_base:forged".parse().unwrap(),
        );

        let run = ulid::Ulid::new();
        let step = ulid::Ulid::new();
        let headers = self_call_headers(&inbound, &CallChain::root(run), step, key);
        // The credential alone, for a surface that has no step to hang a
        // self-call on.
        let bare = extract_cache_auth_headers(&headers);
        assert_eq!(bare.len(), 2, "{bare:?}");
        // The judge's self-call carries the credential and the run, as the
        // embedding self-calls do.
        let judge = judge_self_call_headers(&inbound, &CallChain::root(run), step, key);
        assert_eq!(judge, embedding_self_call_headers(&headers));
        assert!(judge.len() > 2, "{judge:?}");
        let forwarded = embedding_self_call_headers(&headers);
        let get = |name: &str| {
            forwarded
                .iter()
                .find(|(n, _)| n.eq_ignore_ascii_case(name))
                .map(|(_, v)| v.clone())
        };
        assert_eq!(get("x-api-key").as_deref(), Some("sk-caller"));
        assert_eq!(get("x-tenant-id").as_deref(), Some("t1"));
        assert_eq!(get("x-custom"), None);
        assert_eq!(get(HEADER_RUN_ID), None);
        assert_eq!(
            get(crate::constants::headers::X_BRUTOR_INTERNAL_EMBEDDING),
            None,
            "a caller's claim to be the platform is never forwarded"
        );
        assert_eq!(get(HEADER_ROOT), Some(run.to_string()));
        assert_eq!(get(HEADER_PARENT), Some(step.to_string()));

        // The receiving route verifies it as a child of the step.
        let verified = DelegationChain::parse_and_verify(get, key).expect("verifies");
        assert_eq!(verified.root_task_id, run);
        assert_eq!(verified.parent_task_id, step);
        assert_eq!(verified.depth, 1);
        // Another tenant's key does not verify it.
        assert!(DelegationChain::parse_and_verify(get, b"other-tenant-key").is_err());
    }
    use crate::models::proxy::{ChatCompletionRequest, ChatMessage};
    use serde_json::json;

    fn msg(
        role: &str,
        content: Option<&str>,
        tool_calls: Option<serde_json::Value>,
    ) -> ChatMessage {
        ChatMessage {
            role: role.to_string(),
            content: content.map(|c| serde_json::json!(c)),
            name: None,
            tool_calls,
            tool_call_id: None,
        }
    }

    fn req(messages: Vec<ChatMessage>) -> ChatCompletionRequest {
        ChatCompletionRequest {
            model: Some("gpt-4o".to_string()),
            model_id: None,
            messages,
            temperature: None,
            max_tokens: None,
            stream: None,
            top_p: None,
            n: None,
            stop: None,
            presence_penalty: None,
            frequency_penalty: None,
            tools: None,
            tool_choice: None,
            extra: serde_json::Map::new(),
        }
    }

    // ── estimate_llm_cost ─────────────────────────────────────────────
    //
    // One formula. Four hand-rolled copies had
    // disagreed about what a partially-reported usage block costs. These
    // pin the one answer: a missing side is worth zero, not "the call was
    // free".

    #[test]
    fn both_token_counts_are_priced_per_million() {
        // 1M in @ $3/M + 1M out @ $15/M = $18.
        let cost = estimate_llm_cost(Some(1_000_000), Some(1_000_000), 3.0, 15.0);
        assert_eq!(cost, Some(18.0));
    }

    #[test]
    fn a_missing_completion_side_still_bills_the_prompt() {
        // Embeddings never report completion tokens. Under the old `zip`
        // this was `None`, which reached the budget counters as $0 — every
        // embedding call in the platform was billed as free.
        let cost = estimate_llm_cost(Some(1_000_000), None, 3.0, 15.0);
        assert_eq!(cost, Some(3.0));
    }

    #[test]
    fn a_missing_prompt_side_still_bills_the_completion() {
        // The mirror case: some providers close an SSE stream having
        // reported only the output side.
        let cost = estimate_llm_cost(None, Some(1_000_000), 3.0, 15.0);
        assert_eq!(cost, Some(15.0));
    }

    #[test]
    fn no_usage_at_all_is_unknown_not_zero() {
        // `None` here means "we were told nothing", which is a different
        // claim from "this cost nothing" and must stay distinguishable in
        // the audit row.
        assert_eq!(estimate_llm_cost(None, None, 3.0, 15.0), None);
    }

    // ── chat_request_has_tool_history ─────────────────────────────────
    //
    // The cache gate previously only inspected the response for
    // `tool_calls`. The skill / MCP tool roundtrip pattern uses TWO
    // chat-completion requests; the second request's response has no
    // `tool_calls` field but the request's `messages[]` contains the
    // tool roundtrip evidence. These tests pin the new gate.

    #[test]
    fn no_tool_history_when_messages_are_plain() {
        let r = req(vec![
            msg("system", Some("you are helpful"), None),
            msg("user", Some("hello"), None),
        ]);
        assert!(!chat_request_has_tool_history(&r));
    }

    #[test]
    fn tool_history_detected_via_tool_role_message() {
        // The portal injects the skill output as a `role: "tool"`
        // message before sending the second turn. Detect it.
        let r = req(vec![
            msg("user", Some("Hello World!"), None),
            msg(
                "assistant",
                None,
                Some(json!([
                    {"id": "call_1", "type": "function",
                     "function": {"name": "skill__hello-world", "arguments": "{}"}}
                ])),
            ),
            msg("tool", Some("Hello from Brutor AI!"), None),
        ]);
        assert!(chat_request_has_tool_history(&r));
    }

    #[test]
    fn tool_history_detected_via_assistant_tool_calls() {
        // Even without a follow-up `role: "tool"` message, an
        // assistant turn carrying `tool_calls` is evidence of a
        // roundtrip in progress.
        let r = req(vec![
            msg("user", Some("call something"), None),
            msg(
                "assistant",
                None,
                Some(json!([
                    {"id": "call_1", "type": "function",
                     "function": {"name": "do_thing", "arguments": "{}"}}
                ])),
            ),
        ]);
        assert!(chat_request_has_tool_history(&r));
    }

    #[test]
    fn empty_tool_calls_array_is_not_tool_history() {
        // Some providers serialise an empty array rather than null.
        // Treat that as "no tools called" — otherwise legitimate
        // responses get gated.
        let r = req(vec![
            msg("user", Some("hi"), None),
            msg("assistant", Some("hello"), Some(json!([]))),
        ]);
        assert!(!chat_request_has_tool_history(&r));
    }

    #[test]
    fn null_tool_calls_is_not_tool_history() {
        let r = req(vec![
            msg("user", Some("hi"), None),
            msg("assistant", Some("hello"), Some(json!(null))),
        ]);
        assert!(!chat_request_has_tool_history(&r));
    }

    #[test]
    fn skill_roundtrip_followup_is_blocked() {
        // The exact screenshot regression: turn 2 lands here with
        // skill roundtrip in `messages[]`. Old gate said "no tool
        // calls in response → cache it"; new gate says "request
        // shows a tool roundtrip → don't cache it".
        let r = req(vec![
            msg("user", Some("Hello World!"), None),
            msg(
                "assistant",
                None,
                Some(json!([
                    {"id": "call_1", "type": "function",
                     "function": {"name": "skill__hello-world", "arguments": "{}"}}
                ])),
            ),
            msg("tool", Some("ASCII art + hostname + timestamp"), None),
        ]);
        assert!(chat_request_has_tool_history(&r));
    }

    // ── chat_body_has_tool_history (generic JSON shape) ───────────────

    #[test]
    fn body_no_tool_history_when_plain() {
        let body = json!({"messages": [
            {"role": "user", "content": "hello"},
        ]});
        assert!(!chat_body_has_tool_history(&body));
    }

    #[test]
    fn body_tool_history_detected_via_tool_role() {
        let body = json!({"messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "x", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "result"},
        ]});
        assert!(chat_body_has_tool_history(&body));
    }

    #[test]
    fn body_tool_history_detected_via_assistant_tool_calls() {
        let body = json!({"messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "tool_calls": [
                {"id": "c", "type": "function",
                 "function": {"name": "x", "arguments": "{}"}}
            ]},
        ]});
        assert!(chat_body_has_tool_history(&body));
    }

    #[test]
    fn body_missing_messages_is_not_tool_history() {
        // /completions uses `prompt`, /responses uses `input` — no
        // messages array means the helper has nothing to scan and
        // should return false rather than panic.
        let body = json!({"prompt": "hello"});
        assert!(!chat_body_has_tool_history(&body));
    }

    // ── chat_request_is_continuation ──────────────────────────────────
    //
    // Multi-turn requests must be excluded from the cache STORE path
    // because their embedding is dominated by the shared prior
    // context — a 4-turn conversation's embedding differs from a
    // 5-turn version of itself by a single user line, producing
    // 90%+ cosine similarity that defeats the threshold even when
    // the actual new question is unrelated. Real-world failure: a
    // user asked "Is there a better time to visit?" mid-conversation
    // and got the cached "weekend trip recommendation" answer back.

    #[test]
    fn fresh_user_only_request_is_not_a_continuation() {
        // Single user message with optional system prompt — the
        // canonical "FAQ-front-door" shape we DO want to cache.
        let r = req(vec![
            msg("system", Some("you are helpful"), None),
            msg("user", Some("what is the capital of Norway?"), None),
        ]);
        assert!(!chat_request_is_continuation(&r));
    }

    #[test]
    fn second_user_turn_is_a_continuation() {
        let r = req(vec![
            msg("user", Some("first question"), None),
            msg("user", Some("second question"), None),
        ]);
        assert!(chat_request_is_continuation(&r));
    }

    #[test]
    fn assistant_in_history_means_continuation() {
        let r = req(vec![
            msg("user", Some("first question"), None),
            msg("assistant", Some("first answer"), None),
            msg("user", Some("follow-up"), None),
        ]);
        assert!(chat_request_is_continuation(&r));
    }

    #[test]
    fn tool_role_in_history_means_continuation() {
        let r = req(vec![
            msg("user", Some("hi"), None),
            msg("tool", Some("tool result"), None),
        ]);
        assert!(chat_request_is_continuation(&r));
    }

    #[test]
    fn empty_messages_is_not_a_continuation() {
        let r = req(vec![]);
        assert!(!chat_request_is_continuation(&r));
    }

    // ── chat_body_is_continuation (generic JSON) ──────────────────────

    #[test]
    fn body_single_user_turn_is_not_a_continuation() {
        let body = json!({"messages": [
            {"role": "user", "content": "hello"},
        ]});
        assert!(!chat_body_is_continuation(&body));
    }

    #[test]
    fn body_multiple_user_turns_is_a_continuation() {
        let body = json!({"messages": [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]});
        assert!(chat_body_is_continuation(&body));
    }

    #[test]
    fn body_assistant_in_history_is_a_continuation() {
        let body = json!({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "follow-up"},
        ]});
        assert!(chat_body_is_continuation(&body));
    }

    // The `/responses` shape.

    #[test]
    fn a_responses_question_is_not_a_continuation() {
        for body in [
            json!({"model": "m", "input": "What is the capital of France?"}),
            json!({"model": "m", "input": [{"role": "user", "content": "capital of France?"}]}),
            json!({"model": "m", "input": [
                {"type": "message", "role": "developer", "content": "be brief"},
                {"type": "message", "role": "system", "content": "be kind"},
                {"type": "message", "role": "user", "content": "capital of France?"}]}),
            json!({"model": "m", "input": "x", "previous_response_id": null}),
            json!({"model": "m", "prompt": "Once upon a time"}),
        ] {
            assert!(!chat_body_is_continuation(&body), "{body}");
            assert!(!chat_body_has_tool_history(&body), "{body}");
        }
    }

    #[test]
    fn a_responses_conversation_is_a_continuation() {
        for body in [
            json!({"input": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "c"}]}),
            json!({"input": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}),
            json!({"input": [{"role": "assistant", "content": "b"}]}),
            json!({"input": "c", "previous_response_id": "resp_1"}),
            json!({"input": "c", "conversation": "conv_1"}),
            json!({"input": "c", "conversation": {"id": "conv_1"}}),
            json!({"input": [
                {"role": "user", "content": "a"},
                {"type": "reasoning", "id": "rs_1", "summary": []}]}),
            json!({"input": [
                {"role": "user", "content": "a"},
                {"type": "item_reference", "id": "msg_1"}]}),
            // A role or an item nobody knows is conversation, not nothing.
            json!({"input": [{"role": "critic", "content": "a"}]}),
            json!({"input": [{"type": "something_new"}]}),
            json!({"input": ["a bare string in the list"]}),
            // One user turn in each of the two places is two turns.
            json!({"messages": [{"role": "user", "content": "a"}],
                   "input": [{"role": "user", "content": "b"}]}),
        ] {
            assert!(chat_body_is_continuation(&body), "{body}");
        }
    }

    #[test]
    fn responses_tool_traffic_is_tool_history_and_a_continuation() {
        for body in [
            json!({"input": [
                {"role": "user", "content": "weather?"},
                {"type": "function_call", "call_id": "c1", "name": "weather", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "sunny"}]}),
            json!({"input": [{"type": "function_call_output", "call_id": "c1", "output": "sunny"}]}),
            json!({"input": [{"type": "mcp_call", "id": "m1"}]}),
            json!({"input": [{"type": "computer_call_output", "call_id": "c1"}]}),
            json!({"input": [{"type": "custom_tool_call", "call_id": "c1"}]}),
            json!({"input": [{"role": "tool", "content": "sunny"}]}),
        ] {
            assert!(chat_body_has_tool_history(&body), "{body}");
            // The implication `plan_store` relies on holds for this shape too.
            assert!(chat_body_is_continuation(&body), "{body}");
        }
        // Earlier output that is not tool traffic is not tool history.
        let reasoning = json!({"input": [
            {"role": "user", "content": "a"}, {"type": "reasoning", "id": "rs_1"}]});
        assert!(!chat_body_has_tool_history(&reasoning));
    }

    #[test]
    fn body_with_no_messages_array_is_not_a_continuation() {
        // /completions uses `prompt`, /responses uses `input` —
        // bare-prompt requests have no conversation context.
        let body = json!({"prompt": "hello"});
        assert!(!chat_body_is_continuation(&body));
    }

    // ── cache_disabled_by_header ──────────────────────────────────────
    //
    // Per-request opt-out — operators can pass `X-Brutor-Cache: off`
    // (or the standard `Cache-Control: no-cache`) to bypass the
    // cache for one specific request. Matches the convention used
    // by Portkey (`X-Cache: false`), Helicone (`Helicone-Cache-
    // Enabled: false`), and OpenAI's standard HTTP semantics.

    fn header_map(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(
                axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                axum::http::HeaderValue::from_str(v).unwrap(),
            );
        }
        h
    }

    #[test]
    fn cache_enabled_by_default_with_no_headers() {
        assert!(!cache_disabled_by_header(&HeaderMap::new()));
    }

    #[test]
    fn cache_disabled_via_x_brutor_cache_off() {
        for v in ["off", "no", "false", "0", "disabled", "OFF", "NO"] {
            let h = header_map(&[("x-brutor-cache", v)]);
            assert!(
                cache_disabled_by_header(&h),
                "X-Brutor-Cache: {v} should disable",
            );
        }
    }

    #[test]
    fn cache_enabled_when_x_brutor_cache_is_on_or_unrecognised() {
        for v in ["on", "yes", "true", "1", "enabled", ""] {
            let h = header_map(&[("x-brutor-cache", v)]);
            assert!(
                !cache_disabled_by_header(&h),
                "X-Brutor-Cache: {v:?} should NOT disable",
            );
        }
    }

    #[test]
    fn cache_disabled_via_cache_control_no_cache() {
        let h = header_map(&[("cache-control", "no-cache")]);
        assert!(cache_disabled_by_header(&h));
    }

    #[test]
    fn cache_disabled_via_cache_control_no_store() {
        let h = header_map(&[("cache-control", "no-store, max-age=0")]);
        assert!(cache_disabled_by_header(&h));
    }

    #[test]
    fn cache_enabled_when_cache_control_is_max_age_only() {
        // `max-age=N` alone doesn't trigger our opt-out — it's a
        // freshness directive, not a bypass.
        let h = header_map(&[("cache-control", "max-age=300")]);
        assert!(!cache_disabled_by_header(&h));
    }

    #[test]
    fn cache_disabled_for_internal_health_check_probe() {
        // Pin the contract that motivated this: gateway-control
        // health-check probes carry `X-Brutor-Internal-Health-Check`
        // and the cache layer must skip both lookup and store on
        // those, otherwise repeated probes against the same model
        // populate the semantic cache with synthetic "ping" /
        // "test" / "health check" entries that then read back as
        // hits — polluting cache stats and the cache itself.
        for v in ["1", "true", "yes", "on"] {
            let h = header_map(&[("x-brutor-internal-health-check", v)]);
            assert!(
                cache_disabled_by_header(&h),
                "X-Brutor-Internal-Health-Check: {v} should disable",
            );
        }
    }

    #[test]
    fn cache_enabled_when_health_check_header_is_blank_or_zero() {
        // Defensive: an empty / "0" value should be treated as the
        // header absent, not as a true health check. Otherwise a
        // misconfigured upstream sending the header explicitly off
        // would still skip the cache, which is the wrong default.
        for v in ["", "0", "   "] {
            let h = header_map(&[("x-brutor-internal-health-check", v)]);
            assert!(
                !cache_disabled_by_header(&h),
                "X-Brutor-Internal-Health-Check: {v:?} should NOT disable",
            );
        }
    }
    // ── ResponsesUsage — the /responses usage parser ─────────────────
    //
    // 0.10.94 wrote every /responses proxy_logs row with NULL tokens and
    // cost while usage_logs carried both; the row was written before the
    // usage was read. These pin the parser both surfaces now share.

    #[test]
    fn responses_usage_reads_input_output_total() {
        let body = json!({
            "id": "resp_1",
            "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
        });
        let u = ResponsesUsage::from_body(&body);
        assert_eq!(u.input_tokens, Some(120));
        assert_eq!(u.output_tokens, Some(30));
        assert_eq!(u.total_tokens, Some(150));
        assert_eq!(u.cached_input_tokens, 0);
        assert_eq!(u.reasoning_output_tokens, 0);
        assert!(!u.is_empty());
    }

    #[test]
    fn responses_usage_reads_cached_and_reasoning_details() {
        let body = json!({
            "usage": {
                "input_tokens": 2048,
                "input_tokens_details": {"cached_tokens": 1024},
                "output_tokens": 500,
                "output_tokens_details": {"reasoning_tokens": 400},
                "total_tokens": 2548
            }
        });
        let u = ResponsesUsage::from_body(&body);
        assert_eq!(u.cached_input_tokens, 1024);
        assert_eq!(u.reasoning_output_tokens, 400);
        assert_eq!(u.total_tokens, Some(2548));
    }

    #[test]
    fn responses_usage_sums_sides_when_total_is_absent() {
        let body = json!({"usage": {"input_tokens": 10, "output_tokens": 5}});
        assert_eq!(ResponsesUsage::from_body(&body).total_tokens, Some(15));
        // A lone side is the total, not nothing — a partially reported
        // usage block must not collapse to NULL (same rule as the cost).
        let body = json!({"usage": {"input_tokens": 10}});
        let u = ResponsesUsage::from_body(&body);
        assert_eq!(u.total_tokens, Some(10));
        assert_eq!(u.output_tokens, None);
        assert!(!u.is_empty());
    }

    #[test]
    fn responses_usage_accepts_chat_shape_for_bridged_providers() {
        // Non-Responses providers are bridged to /chat/completions and the
        // body comes back in chat shape on the /responses surface.
        let body = json!({
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 6},
                "completion_tokens_details": {"reasoning_tokens": 2}
            }
        });
        let u = ResponsesUsage::from_body(&body);
        assert_eq!(u.input_tokens, Some(7));
        assert_eq!(u.output_tokens, Some(3));
        assert_eq!(u.total_tokens, Some(10));
        assert_eq!(u.cached_input_tokens, 6);
        assert_eq!(u.reasoning_output_tokens, 2);
    }

    #[test]
    fn responses_usage_is_empty_without_an_object_usage() {
        // `response.created` / `in_progress` stream events carry
        // `"usage": null`; an error envelope has none at all.
        for body in [
            json!({"usage": null}),
            json!({"error": {"message": "boom"}}),
            json!({"usage": "n/a"}),
        ] {
            let u = ResponsesUsage::from_body(&body);
            assert!(u.is_empty(), "{body} should parse as empty");
            assert_eq!(u.total_tokens, None);
            assert_eq!(u, ResponsesUsage::default());
        }
    }

    #[test]
    fn populate_llm_usage_fields_prices_with_the_shared_formula() {
        let mut model = crate::services::llm_adapter::make_test_model("openai", "gpt-5.5");
        model.cost_per_input_token = Some(2.0);
        model.cost_per_output_token = Some(8.0);
        let usage = ResponsesUsage::from_body(&json!({
            "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000, "total_tokens": 1_500_000}
        }));
        let mut log = crate::services::proxy_logger::build_proxy_log(
            "llm",
            &crate::models::auth::make_auth_context(
                crate::models::auth::AuthType::ApiKey,
                "tenant-1",
            ),
            "POST",
            "/responses",
            None,
            None,
            200,
            1.0,
            true,
            None,
            "corr",
        );
        let cost = populate_llm_usage_fields(&mut log, &usage, &model);
        assert_eq!(log.llm_prompt_tokens, Some(1_000_000));
        assert_eq!(log.llm_completion_tokens, Some(500_000));
        assert_eq!(log.llm_total_tokens, Some(1_500_000));
        // Same figure on the row and returned for usage_logs.
        assert_eq!(cost, log.llm_estimated_cost_usd);
        assert_eq!(
            cost,
            estimate_llm_cost(Some(1_000_000), Some(500_000), 2.0, 8.0)
        );
        assert!((cost.unwrap() - 6.0).abs() < 1e-9);
    }

    #[test]
    fn populate_llm_usage_fields_leaves_cost_null_when_nothing_reported() {
        let model = crate::services::llm_adapter::make_test_model("openai", "gpt-5.5");
        let mut log = crate::services::proxy_logger::build_proxy_log(
            "llm",
            &crate::models::auth::make_auth_context(
                crate::models::auth::AuthType::ApiKey,
                "tenant-1",
            ),
            "POST",
            "/responses",
            None,
            None,
            502,
            1.0,
            false,
            None,
            "corr",
        );
        let cost = populate_llm_usage_fields(&mut log, &ResponsesUsage::default(), &model);
        assert_eq!(cost, None);
        assert_eq!(log.llm_prompt_tokens, None);
        assert_eq!(log.llm_total_tokens, None);
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Chat Completions parameter rules of the OpenAI model families
//!
//! One definition for the OpenAI adapter and the Azure OpenAI adapter (they
//! serve the same models and had two copies of one prefix list).
//!
//! **The catalog row decides where it says something; a name prefix is the
//! fallback.** The gateway receives each model's `capabilities` JSON from the
//! database. A model the catalog adds tomorrow is then handled by its row,
//! not by a release of the gateway:
//!
//! | Rule | From the row | Fallback by name |
//! |---|---|---|
//! | Reasoning model: `temperature` / `top_p` are refused, the output limit is `max_completion_tokens` | `capabilities.supports_reasoning = true` (providers `openai` and `azure` only) | [`REASONING_PREFIXES`] |
//! | `max_completion_tokens` instead of `max_tokens` | a reasoning model, as above | [`COMPLETION_TOKENS_PREFIXES`] |
//! | Function tools are accepted only with `reasoning_effort: "none"` | `capabilities.tools_require_reasoning_effort_none` (either value is final) | [`tools_need_effort_none_by_name`] |
//!
//! The row can only switch the first two rules *on*: a row with no
//! `supports_reasoning` (an operator's hand-made model, a row older than the
//! flag) must not send `temperature` to a model the provider refuses it on,
//! so a name the gateway knows still counts. The row's flag is read only for
//! the providers `openai` and `azure`: this adapter is also the default for
//! every OpenAI-compatible server, where "reasoning" says nothing about
//! which parameters the server takes.

use serde_json::{Value, json};
use tracing::debug;

use crate::models::db::LlmModel;

/// Names (after any `azure/` prefix, lower-cased) of the families that
/// refuse `temperature` and `top_p` and take `max_completion_tokens`.
const REASONING_PREFIXES: [&str; 5] = ["o1", "o3", "o4", "gpt-5", "gpt-6"];

/// Families that take `max_completion_tokens` without being reasoning
/// models.
const COMPLETION_TOKENS_PREFIXES: [&str; 1] = ["gpt-4o"];

/// The capability a catalog row sets to state (or lift) the tool rule.
pub(crate) const TOOLS_NEED_EFFORT_NONE_CAPABILITY: &str = "tools_require_reasoning_effort_none";

/// What Chat Completions accepts for one model.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ChatRules {
    /// The output limit is spelled `max_completion_tokens`.
    pub completion_tokens: bool,
    /// `temperature` and `top_p` are refused.
    pub reasoning: bool,
    /// Function tools are accepted only with `reasoning_effort: "none"`.
    pub tools_need_effort_none: bool,
}

/// The model's name as the provider knows the family: lower-cased, without
/// the gateway's `azure/` catalog prefix (the Azure ids are seeded as
/// `azure/gpt-5.2`; the rules below were written against `gpt-5.2`).
pub(crate) fn family_name(model: &LlmModel) -> String {
    let lower = model.model_name.trim().to_lowercase();
    match lower.strip_prefix("azure/") {
        Some(rest) if is_azure(model) => rest.to_string(),
        _ => lower,
    }
}

fn is_azure(model: &LlmModel) -> bool {
    model.provider == "azure" || model.provider == "azure_ai"
}

fn capability(model: &LlmModel, key: &str) -> Option<bool> {
    model
        .capabilities
        .as_ref()
        .and_then(|caps| caps.get(key))
        .and_then(Value::as_bool)
}

/// `gpt-6-sol` and `gpt-6-luna` (and their dated snapshots) everywhere;
/// `gpt-5.6-*` on Azure, where Microsoft states the same restriction.
/// `gpt-6.1-sol` and `gpt-6-astra` have no `none` level and take tools on the
/// Responses API only: they are not in this rule.
fn tools_need_effort_none_by_name(name: &str, azure: bool) -> bool {
    let is = |id: &str| name == id || name.strip_prefix(id).is_some_and(|r| r.starts_with('-'));
    is("gpt-6-sol") || is("gpt-6-luna") || (azure && name.starts_with("gpt-5.6-"))
}

pub(crate) fn chat_rules(model: &LlmModel) -> ChatRules {
    let name = family_name(model);
    let row_says_reasoning = matches!(model.provider.as_str(), "openai" | "azure")
        && capability(model, "supports_reasoning") == Some(true);
    let reasoning = row_says_reasoning || REASONING_PREFIXES.iter().any(|p| name.starts_with(p));
    ChatRules {
        completion_tokens: reasoning
            || COMPLETION_TOKENS_PREFIXES
                .iter()
                .any(|p| name.starts_with(p)),
        reasoning,
        tools_need_effort_none: capability(model, TOOLS_NEED_EFFORT_NONE_CAPABILITY)
            .unwrap_or_else(|| tools_need_effort_none_by_name(&name, is_azure(model))),
    }
}

fn has_tools(tools: Option<&Value>) -> bool {
    tools
        .and_then(Value::as_array)
        .is_some_and(|a| !a.is_empty())
}

/// Why this chat request cannot be sent to `model`, when the caller's own
/// choices contradict what the model accepts. The gateway fills what the
/// caller left open ([`apply`]); it does not replace what the caller chose,
/// and it does not send a request the provider is known to refuse.
pub fn chat_request_conflict(
    model: &LlmModel,
    tools: Option<&Value>,
    reasoning_effort: Option<&Value>,
) -> Option<String> {
    if !has_tools(tools) || !chat_rules(model).tools_need_effort_none {
        return None;
    }
    let effort = reasoning_effort.filter(|v| !v.is_null())?;
    if effort.as_str() == Some("none") {
        return None;
    }
    Some(format!(
        "Model '{}' accepts tools on Chat Completions only with reasoning_effort \"none\"; \
         this request asks for reasoning_effort {effort}. Send \"none\" (or leave \
         reasoning_effort out and the gateway sets it), or call the model without tools.",
        model.model_name
    ))
}

/// Shape a Chat Completions body for an OpenAI-family model.
pub(crate) fn apply(obj: &mut serde_json::Map<String, Value>, model: &LlmModel) {
    let rules = chat_rules(model);
    let name = &model.model_name;

    if rules.completion_tokens
        && let Some(max_tokens) = obj.remove("max_tokens")
        && !obj.contains_key("max_completion_tokens")
    {
        // DEBUG, not WARN: a routine rename (these models no longer accept
        // `max_tokens`; the caller's value moves to the new name). WARN is
        // for what an on-call should look at.
        debug!(
            model = %name,
            "Adapter rewriting max_tokens → max_completion_tokens (model does not support max_tokens)"
        );
        obj.insert("max_completion_tokens".into(), max_tokens);
    }

    // Tools need `reasoning_effort: "none"` on these models, whose own
    // default is `medium`: a request with tools and no effort would be
    // refused. Only the absent value is filled; a caller's other choice was
    // refused before this point where the route can answer
    // (`chat_request_conflict`), and is otherwise left as the caller sent it.
    if rules.tools_need_effort_none
        && has_tools(obj.get("tools"))
        && !obj.contains_key("reasoning_effort")
    {
        debug!(model = %name, "Adapter set reasoning_effort=none (the model takes tools only without reasoning)");
        obj.insert("reasoning_effort".into(), json!("none"));
    }

    // ── Extended thinking ──────────────────────────────────────
    // The OpenAI side of the generic `thinking_enabled` flag. With thinking
    // on for the row, or on a reasoning model, the adapter (a) fills
    // `reasoning_effort` if the operator asked for thinking and nobody
    // chose one and (b) strips temperature / top_p so the upstream does not
    // answer 400 "unsupported parameter".
    let thinking_flag = model.thinking_enabled.unwrap_or(false);
    if !(thinking_flag || rules.reasoning) {
        return;
    }
    // `reasoning_effort` only when the operator turned thinking on for this
    // model; a caller's own value always wins. A reasoning model with
    // the flag off keeps the provider's default.
    if thinking_flag {
        obj.entry("reasoning_effort".to_string())
            .or_insert_with(|| json!("medium"));
    }
    // `debug!`: deterministic on every reasoning-mode request, not an
    // on-call signal.
    for key in ["temperature", "top_p"] {
        if obj.remove(key).is_some() {
            debug!(
                model = %name,
                param = key,
                "Adapter dropped a sampling parameter (thinking/reasoning enabled — provider rejects)"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::llm_adapter::make_test_model;

    fn rules(provider: &str, name: &str, caps: Option<Value>) -> ChatRules {
        let mut model = make_test_model(provider, name);
        model.capabilities = caps;
        chat_rules(&model)
    }

    #[test]
    fn every_family_of_the_catalog_gets_its_rules_by_name() {
        for name in [
            "gpt-6-sol",
            "gpt-6-luna",
            "gpt-6.1-sol",
            "gpt-6-astra",
            "gpt-5.2",
            "gpt-5.6-terra",
            "o3-mini",
            "GPT-6-Luna",
        ] {
            let r = rules("openai", name, None);
            assert!(r.reasoning && r.completion_tokens, "{name}: {r:?}");
        }
        let r = rules("openai", "gpt-4o", None);
        assert!(r.completion_tokens && !r.reasoning, "{r:?}");
        let r = rules("openai", "gpt-3.5-turbo", None);
        assert!(!r.completion_tokens && !r.reasoning, "{r:?}");
    }

    #[test]
    fn the_row_switches_reasoning_on_for_openai_and_azure_only() {
        let caps = Some(json!({"supports_reasoning": true}));
        assert!(rules("openai", "unlisted-1", caps.clone()).reasoning);
        assert!(rules("azure", "azure/unlisted-1", caps.clone()).reasoning);
        // An OpenAI-compatible server: "reasoning" says nothing about the
        // parameters it takes.
        for provider in ["nvidia_nim", "vllm", "azure_ai", "huggingface"] {
            let r = rules(provider, "unlisted-1", caps.clone());
            assert!(!r.reasoning && !r.completion_tokens, "{provider}: {r:?}");
        }
        // A row cannot switch a known family off.
        let off = Some(json!({"supports_reasoning": false}));
        assert!(rules("openai", "gpt-6-luna", off).reasoning);
    }

    #[test]
    fn the_azure_catalog_prefix_is_not_part_of_the_family_name() {
        let r = rules("azure", "azure/gpt-5.2", None);
        assert!(r.reasoning && r.completion_tokens, "{r:?}");
        // Only Azure ids carry it: on another provider it is part of the id.
        assert!(!rules("openai", "azure/gpt-5.2", None).reasoning);
    }

    #[test]
    fn the_tool_rule_names_exactly_the_models_that_have_it() {
        for (provider, name, expected) in [
            ("openai", "gpt-6-sol", true),
            ("openai", "gpt-6-luna", true),
            ("openai", "gpt-6-luna-2026-10-01", true),
            ("openai", "gpt-6-solar", false),
            ("openai", "gpt-6.1-sol", false),
            ("openai", "gpt-6-astra", false),
            ("openai", "gpt-5.6-terra", false),
            ("azure", "azure/gpt-5.6-terra", true),
            ("azure", "azure/gpt-5.6-sol", true),
            ("azure", "azure/gpt-5.2", false),
        ] {
            assert_eq!(
                rules(provider, name, None).tools_need_effort_none,
                expected,
                "{provider} {name}"
            );
        }
        // The row is final in both directions.
        let on = Some(json!({TOOLS_NEED_EFFORT_NONE_CAPABILITY: true}));
        assert!(rules("openai", "unlisted-1", on).tools_need_effort_none);
        let off = Some(json!({TOOLS_NEED_EFFORT_NONE_CAPABILITY: false}));
        assert!(!rules("openai", "gpt-6-sol", off).tools_need_effort_none);
    }

    #[test]
    fn a_conflict_is_only_the_callers_own_other_effort_with_tools() {
        let model = make_test_model("openai", "gpt-6-sol");
        let tools = json!([{"type": "function", "function": {"name": "f"}}]);
        let conflict = |tools: Option<&Value>, effort: Option<Value>| {
            chat_request_conflict(&model, tools, effort.as_ref())
        };
        assert!(conflict(Some(&tools), Some(json!("high"))).is_some());
        assert!(conflict(Some(&tools), Some(json!("none"))).is_none());
        assert!(conflict(Some(&tools), None).is_none());
        assert!(conflict(Some(&tools), Some(Value::Null)).is_none());
        assert!(conflict(None, Some(json!("high"))).is_none());
        assert!(conflict(Some(&json!([])), Some(json!("high"))).is_none());
        // Not a string: not "none", so not what the model accepts.
        assert!(conflict(Some(&tools), Some(json!(3))).is_some());
        let other = make_test_model("openai", "gpt-6.1-sol");
        assert!(chat_request_conflict(&other, Some(&tools), Some(&json!("high"))).is_none());
    }

    #[test]
    fn tools_get_effort_none_before_the_operators_thinking_default() {
        // `thinking_enabled` fills `medium` when nobody chose; with tools on
        // these models that would be refused, so `none` is filled first.
        let mut model = make_test_model("openai", "gpt-6-luna");
        model.thinking_enabled = Some(true);
        let mut obj = serde_json::Map::new();
        obj.insert("tools".into(), json!([{"type": "function"}]));
        apply(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("none")));

        let mut obj = serde_json::Map::new();
        apply(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("medium")));
    }

    #[test]
    fn a_callers_effort_is_never_replaced() {
        let model = make_test_model("openai", "gpt-6-luna");
        let mut obj = serde_json::Map::new();
        obj.insert("tools".into(), json!([{"type": "function"}]));
        obj.insert("reasoning_effort".into(), json!("high"));
        apply(&mut obj, &model);
        assert_eq!(obj.get("reasoning_effort"), Some(&json!("high")));
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Typed LLM governance structs and merge logic.
//!
//! These mirror the `governance` JSON column on `resource_group_llm_governance`
//! and implement governance-specific restrictive merge for inheritance.

use serde::Deserialize;
use std::collections::HashSet;

// ---------------------------------------------------------------------------
// Governance structs
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize, Default)]
pub struct LlmGovernance {
    pub temperature: Option<TemperatureGovernance>,
    pub context_window: Option<ContextWindowGovernance>,
    pub tool_use: Option<ToolUseGovernance>,
    pub system_prompt: Option<SystemPromptGovernance>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct TemperatureGovernance {
    pub max: Option<f64>,
    pub enforce: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct ContextWindowGovernance {
    pub max_input_tokens: Option<i64>,
    pub max_output_tokens: Option<i64>,
    pub enforce: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct ToolUseGovernance {
    pub access: Option<String>,
    pub approval_timeout_seconds: Option<i32>,
    pub allowed_tools: Option<Vec<String>>,
    pub blocked_tools: Option<Vec<String>>,
    pub max_tool_calls_per_request: Option<i32>,
    pub max_tool_calls_per_hour: Option<i32>,
    pub max_tool_calls_per_day: Option<i32>,
    pub banned_argument_patterns: Option<Vec<String>>,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct MandatoryFragment {
    pub position: String,
    pub text: String,
}

#[derive(Debug, Clone, Deserialize, Default)]
pub struct SystemPromptGovernance {
    pub mandatory_fragments: Option<Vec<MandatoryFragment>>,
    pub banned_patterns: Option<Vec<String>>,
}

// ---------------------------------------------------------------------------
// Strictness helpers
// ---------------------------------------------------------------------------

fn access_strictness(access: &str) -> u8 {
    match access {
        "enabled" => 0,
        "approval_required" => 1,
        "disabled" => 2,
        // An access the gateway does not know is the most restrictive: it
        // outranks every known one, so the merge keeps it and the request
        // path refuses on it (`llm_governance::enforce_request_governance`).
        _ => 3,
    }
}

fn enforce_strictness(enforce: &str) -> u8 {
    match enforce {
        "clamp" => 0,
        "reject" => 1,
        // An enforce the gateway does not know is the most restrictive: it
        // outranks both known ones, so the merge keeps it and the request
        // path rejects on it (`llm_governance::rejects`).
        _ => 2,
    }
}

// ---------------------------------------------------------------------------
// Merge logic
// ---------------------------------------------------------------------------

/// Merge `child` governance into `base` with restrictive semantics.
///
/// This is the governance-specific merge — NOT a simple numeric min.
/// Rules per field are documented in the plan.
pub fn merge_governance(base: &mut LlmGovernance, child: &LlmGovernance) {
    // Temperature
    if let Some(ref child_temp) = child.temperature {
        let base_temp = base.temperature.get_or_insert_with(Default::default);
        merge_numeric_min_f64(&mut base_temp.max, &child_temp.max);
        merge_enforce_strict(&mut base_temp.enforce, &child_temp.enforce);
    }

    // Context window
    if let Some(ref child_ctx) = child.context_window {
        let base_ctx = base.context_window.get_or_insert_with(Default::default);
        merge_numeric_min_i64(&mut base_ctx.max_input_tokens, &child_ctx.max_input_tokens);
        merge_numeric_min_i64(
            &mut base_ctx.max_output_tokens,
            &child_ctx.max_output_tokens,
        );
        merge_enforce_strict(&mut base_ctx.enforce, &child_ctx.enforce);
    }

    // Tool use
    if let Some(ref child_tu) = child.tool_use {
        let base_tu = base.tool_use.get_or_insert_with(Default::default);

        // Three-state access: max strictness wins
        if let Some(ref child_access) = child_tu.access {
            match &base_tu.access {
                Some(base_access) => {
                    if access_strictness(child_access) > access_strictness(base_access) {
                        base_tu.access = Some(child_access.clone());
                    }
                }
                None => base_tu.access = Some(child_access.clone()),
            }
        }

        // Timeout: min wins
        merge_numeric_min_i32(
            &mut base_tu.approval_timeout_seconds,
            &child_tu.approval_timeout_seconds,
        );

        // Allowed tools: intersection
        merge_list_intersection(&mut base_tu.allowed_tools, &child_tu.allowed_tools);

        // Blocked tools: union
        merge_list_union(&mut base_tu.blocked_tools, &child_tu.blocked_tools);

        // Tool call rate limits: min wins
        merge_numeric_min_i32(
            &mut base_tu.max_tool_calls_per_request,
            &child_tu.max_tool_calls_per_request,
        );
        merge_numeric_min_i32(
            &mut base_tu.max_tool_calls_per_hour,
            &child_tu.max_tool_calls_per_hour,
        );
        merge_numeric_min_i32(
            &mut base_tu.max_tool_calls_per_day,
            &child_tu.max_tool_calls_per_day,
        );

        // Banned argument patterns: union
        merge_list_union(
            &mut base_tu.banned_argument_patterns,
            &child_tu.banned_argument_patterns,
        );
    }

    // System prompt
    if let Some(ref child_sp) = child.system_prompt {
        let base_sp = base.system_prompt.get_or_insert_with(Default::default);

        // Mandatory fragments: union (parent first, child appended)
        if let Some(ref child_frags) = child_sp.mandatory_fragments {
            let base_frags = base_sp.mandatory_fragments.get_or_insert_with(Vec::new);
            let existing_texts: HashSet<String> =
                base_frags.iter().map(|f| f.text.clone()).collect();
            for frag in child_frags {
                if !existing_texts.contains(&frag.text) {
                    base_frags.push(frag.clone());
                }
            }
        }

        // Banned patterns: union
        merge_list_union(&mut base_sp.banned_patterns, &child_sp.banned_patterns);
    }
}

// ---------------------------------------------------------------------------
// Merge primitives
// ---------------------------------------------------------------------------

fn merge_numeric_min_f64(base: &mut Option<f64>, child: &Option<f64>) {
    if let Some(c) = child {
        match base {
            Some(b) => *b = b.min(*c),
            None => *base = Some(*c),
        }
    }
}

fn merge_numeric_min_i64(base: &mut Option<i64>, child: &Option<i64>) {
    if let Some(c) = child {
        match base {
            Some(b) => *b = (*b).min(*c),
            None => *base = Some(*c),
        }
    }
}

fn merge_numeric_min_i32(base: &mut Option<i32>, child: &Option<i32>) {
    if let Some(c) = child {
        match base {
            Some(b) => *b = (*b).min(*c),
            None => *base = Some(*c),
        }
    }
}

fn merge_enforce_strict(base: &mut Option<String>, child: &Option<String>) {
    if let Some(c) = child {
        let should_replace = match base.as_ref() {
            Some(b) => enforce_strictness(c) > enforce_strictness(b),
            None => true,
        };
        if should_replace {
            *base = Some(c.clone());
        }
    }
}

fn merge_list_intersection(base: &mut Option<Vec<String>>, child: &Option<Vec<String>>) {
    if let Some(child_list) = child {
        match base {
            Some(base_list) => {
                let child_set: HashSet<&str> = child_list.iter().map(|s| s.as_str()).collect();
                base_list.retain(|s| child_set.contains(s.as_str()));
            }
            None => *base = Some(child_list.clone()),
        }
    }
}

fn merge_list_union(base: &mut Option<Vec<String>>, child: &Option<Vec<String>>) {
    if let Some(child_list) = child {
        let base_list = base.get_or_insert_with(Vec::new);
        let existing: HashSet<String> = base_list.iter().cloned().collect();
        for item in child_list {
            if !existing.contains(item) {
                base_list.push(item.clone());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // -----------------------------------------------------------------------
    // access_strictness
    // -----------------------------------------------------------------------

    #[test]
    fn access_strictness_enabled() {
        assert_eq!(access_strictness("enabled"), 0);
    }

    #[test]
    fn access_strictness_approval_required() {
        assert_eq!(access_strictness("approval_required"), 1);
    }

    #[test]
    fn access_strictness_disabled() {
        assert_eq!(access_strictness("disabled"), 2);
    }

    /// An access the gateway does not know is stricter than every access it
    /// knows, so no level of the chain can merge it away.
    #[test]
    fn access_strictness_unknown_is_the_strictest() {
        for unknown in ["bogus", "", "Disabled"] {
            assert!(access_strictness(unknown) > access_strictness("disabled"));
        }
    }

    // -----------------------------------------------------------------------
    // enforce_strictness
    // -----------------------------------------------------------------------

    #[test]
    fn enforce_strictness_clamp() {
        assert_eq!(enforce_strictness("clamp"), 0);
    }

    #[test]
    fn enforce_strictness_reject() {
        assert_eq!(enforce_strictness("reject"), 1);
    }

    #[test]
    fn enforce_strictness_unknown_outranks_both_known() {
        assert_eq!(enforce_strictness("unknown"), 2);
        assert_eq!(enforce_strictness(""), 2);
        assert!(enforce_strictness("unknown") > enforce_strictness("reject"));
    }

    // -----------------------------------------------------------------------
    // merge_numeric_min_f64
    // -----------------------------------------------------------------------

    #[test]
    fn merge_f64_none_none() {
        let mut base = None;
        merge_numeric_min_f64(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_f64_some_none() {
        let mut base = Some(1.5);
        merge_numeric_min_f64(&mut base, &None);
        assert_eq!(base, Some(1.5));
    }

    #[test]
    fn merge_f64_none_some() {
        let mut base = None;
        merge_numeric_min_f64(&mut base, &Some(0.8));
        assert_eq!(base, Some(0.8));
    }

    #[test]
    fn merge_f64_child_lower() {
        let mut base = Some(1.0);
        merge_numeric_min_f64(&mut base, &Some(0.5));
        assert_eq!(base, Some(0.5));
    }

    #[test]
    fn merge_f64_parent_lower() {
        let mut base = Some(0.3);
        merge_numeric_min_f64(&mut base, &Some(0.9));
        assert_eq!(base, Some(0.3));
    }

    #[test]
    fn merge_f64_equal() {
        let mut base = Some(0.7);
        merge_numeric_min_f64(&mut base, &Some(0.7));
        assert_eq!(base, Some(0.7));
    }

    // -----------------------------------------------------------------------
    // merge_numeric_min_i64
    // -----------------------------------------------------------------------

    #[test]
    fn merge_i64_none_none() {
        let mut base = None;
        merge_numeric_min_i64(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_i64_some_none() {
        let mut base = Some(1000);
        merge_numeric_min_i64(&mut base, &None);
        assert_eq!(base, Some(1000));
    }

    #[test]
    fn merge_i64_none_some() {
        let mut base = None;
        merge_numeric_min_i64(&mut base, &Some(500));
        assert_eq!(base, Some(500));
    }

    #[test]
    fn merge_i64_child_lower() {
        let mut base = Some(1000);
        merge_numeric_min_i64(&mut base, &Some(200));
        assert_eq!(base, Some(200));
    }

    #[test]
    fn merge_i64_parent_lower() {
        let mut base = Some(100);
        merge_numeric_min_i64(&mut base, &Some(500));
        assert_eq!(base, Some(100));
    }

    #[test]
    fn merge_i64_equal() {
        let mut base = Some(300);
        merge_numeric_min_i64(&mut base, &Some(300));
        assert_eq!(base, Some(300));
    }

    // -----------------------------------------------------------------------
    // merge_numeric_min_i32
    // -----------------------------------------------------------------------

    #[test]
    fn merge_i32_none_none() {
        let mut base = None;
        merge_numeric_min_i32(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_i32_some_none() {
        let mut base = Some(50);
        merge_numeric_min_i32(&mut base, &None);
        assert_eq!(base, Some(50));
    }

    #[test]
    fn merge_i32_none_some() {
        let mut base = None;
        merge_numeric_min_i32(&mut base, &Some(25));
        assert_eq!(base, Some(25));
    }

    #[test]
    fn merge_i32_child_lower() {
        let mut base = Some(100);
        merge_numeric_min_i32(&mut base, &Some(10));
        assert_eq!(base, Some(10));
    }

    #[test]
    fn merge_i32_parent_lower() {
        let mut base = Some(5);
        merge_numeric_min_i32(&mut base, &Some(50));
        assert_eq!(base, Some(5));
    }

    #[test]
    fn merge_i32_equal() {
        let mut base = Some(42);
        merge_numeric_min_i32(&mut base, &Some(42));
        assert_eq!(base, Some(42));
    }

    // -----------------------------------------------------------------------
    // merge_enforce_strict
    // -----------------------------------------------------------------------

    #[test]
    fn merge_enforce_none_none() {
        let mut base = None;
        merge_enforce_strict(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_enforce_clamp_reject_picks_reject() {
        let mut base = Some("clamp".to_string());
        merge_enforce_strict(&mut base, &Some("reject".to_string()));
        assert_eq!(base.as_deref(), Some("reject"));
    }

    #[test]
    fn merge_enforce_reject_clamp_keeps_reject() {
        let mut base = Some("reject".to_string());
        merge_enforce_strict(&mut base, &Some("clamp".to_string()));
        assert_eq!(base.as_deref(), Some("reject"));
    }

    #[test]
    fn merge_enforce_same_values() {
        let mut base = Some("reject".to_string());
        merge_enforce_strict(&mut base, &Some("reject".to_string()));
        assert_eq!(base.as_deref(), Some("reject"));
    }

    #[test]
    fn merge_enforce_none_some() {
        let mut base = None;
        merge_enforce_strict(&mut base, &Some("clamp".to_string()));
        assert_eq!(base.as_deref(), Some("clamp"));
    }

    #[test]
    fn merge_enforce_some_none() {
        let mut base = Some("reject".to_string());
        merge_enforce_strict(&mut base, &None);
        assert_eq!(base.as_deref(), Some("reject"));
    }

    // -----------------------------------------------------------------------
    // merge_list_intersection
    // -----------------------------------------------------------------------

    #[test]
    fn merge_intersection_none_none() {
        let mut base = None;
        merge_list_intersection(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_intersection_some_none() {
        let mut base = Some(vec!["a".to_string(), "b".to_string()]);
        merge_list_intersection(&mut base, &None);
        assert_eq!(base, Some(vec!["a".to_string(), "b".to_string()]));
    }

    #[test]
    fn merge_intersection_none_some() {
        let mut base = None;
        merge_list_intersection(&mut base, &Some(vec!["x".to_string()]));
        assert_eq!(base, Some(vec!["x".to_string()]));
    }

    #[test]
    fn merge_intersection_overlapping() {
        let mut base = Some(vec!["a".to_string(), "b".to_string(), "c".to_string()]);
        merge_list_intersection(
            &mut base,
            &Some(vec!["b".to_string(), "c".to_string(), "d".to_string()]),
        );
        assert_eq!(base, Some(vec!["b".to_string(), "c".to_string()]));
    }

    #[test]
    fn merge_intersection_disjoint() {
        let mut base = Some(vec!["a".to_string(), "b".to_string()]);
        merge_list_intersection(&mut base, &Some(vec!["x".to_string(), "y".to_string()]));
        assert_eq!(base, Some(vec![]));
    }

    #[test]
    fn merge_intersection_identical() {
        let mut base = Some(vec!["a".to_string(), "b".to_string()]);
        merge_list_intersection(&mut base, &Some(vec!["a".to_string(), "b".to_string()]));
        assert_eq!(base, Some(vec!["a".to_string(), "b".to_string()]));
    }

    // -----------------------------------------------------------------------
    // merge_list_union
    // -----------------------------------------------------------------------

    #[test]
    fn merge_union_none_none() {
        let mut base = None;
        merge_list_union(&mut base, &None);
        assert!(base.is_none());
    }

    #[test]
    fn merge_union_some_none() {
        let mut base = Some(vec!["a".to_string()]);
        merge_list_union(&mut base, &None);
        assert_eq!(base, Some(vec!["a".to_string()]));
    }

    #[test]
    fn merge_union_none_some() {
        let mut base = None;
        merge_list_union(&mut base, &Some(vec!["x".to_string()]));
        assert_eq!(base, Some(vec!["x".to_string()]));
    }

    #[test]
    fn merge_union_overlapping() {
        let mut base = Some(vec!["a".to_string(), "b".to_string()]);
        merge_list_union(&mut base, &Some(vec!["b".to_string(), "c".to_string()]));
        let result = base.unwrap();
        assert_eq!(result.len(), 3);
        assert!(result.contains(&"a".to_string()));
        assert!(result.contains(&"b".to_string()));
        assert!(result.contains(&"c".to_string()));
    }

    #[test]
    fn merge_union_disjoint() {
        let mut base = Some(vec!["a".to_string()]);
        merge_list_union(&mut base, &Some(vec!["x".to_string(), "y".to_string()]));
        let result = base.unwrap();
        assert_eq!(result.len(), 3);
        assert!(result.contains(&"a".to_string()));
        assert!(result.contains(&"x".to_string()));
        assert!(result.contains(&"y".to_string()));
    }

    // -----------------------------------------------------------------------
    // merge_governance (full integration)
    // -----------------------------------------------------------------------

    #[test]
    fn merge_governance_temperature() {
        let mut base = LlmGovernance {
            temperature: Some(TemperatureGovernance {
                max: Some(1.0),
                enforce: Some("clamp".to_string()),
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            temperature: Some(TemperatureGovernance {
                max: Some(0.5),
                enforce: Some("reject".to_string()),
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        let temp = base.temperature.unwrap();
        assert_eq!(temp.max, Some(0.5));
        assert_eq!(temp.enforce.as_deref(), Some("reject"));
    }

    /// An `enforce` the gateway does not know outranks both known ones in
    /// the merge, so the request path sees it and rejects on it.
    #[test]
    fn merge_governance_keeps_an_unknown_enforce() {
        let temp = |enforce: &str| LlmGovernance {
            temperature: Some(TemperatureGovernance {
                max: Some(1.0),
                enforce: Some(enforce.to_string()),
            }),
            ..Default::default()
        };
        for base in ["clamp", "reject"] {
            let mut merged = temp(base);
            merge_governance(&mut merged, &temp("block"));
            assert_eq!(
                merged.temperature.unwrap().enforce.as_deref(),
                Some("block"),
                "over {base}"
            );
            let mut merged = temp("block");
            merge_governance(&mut merged, &temp(base));
            assert_eq!(
                merged.temperature.unwrap().enforce.as_deref(),
                Some("block"),
                "under {base}"
            );
        }
    }

    #[test]
    fn merge_governance_context_window() {
        let mut base = LlmGovernance {
            context_window: Some(ContextWindowGovernance {
                max_input_tokens: Some(8000),
                max_output_tokens: Some(4000),
                enforce: Some("clamp".to_string()),
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            context_window: Some(ContextWindowGovernance {
                max_input_tokens: Some(4000),
                max_output_tokens: Some(2000),
                enforce: Some("reject".to_string()),
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        let ctx = base.context_window.unwrap();
        assert_eq!(ctx.max_input_tokens, Some(4000));
        assert_eq!(ctx.max_output_tokens, Some(2000));
        assert_eq!(ctx.enforce.as_deref(), Some("reject"));
    }

    #[test]
    fn merge_governance_tool_use_access_strictest_wins() {
        let mut base = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                access: Some("enabled".to_string()),
                ..Default::default()
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                access: Some("disabled".to_string()),
                ..Default::default()
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        assert_eq!(base.tool_use.unwrap().access.as_deref(), Some("disabled"));
    }

    #[test]
    fn merge_governance_tool_use_lists() {
        let mut base = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                allowed_tools: Some(vec!["a".into(), "b".into(), "c".into()]),
                blocked_tools: Some(vec!["x".into()]),
                ..Default::default()
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                allowed_tools: Some(vec!["b".into(), "c".into(), "d".into()]),
                blocked_tools: Some(vec!["y".into()]),
                ..Default::default()
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        let tu = base.tool_use.unwrap();
        // allowed = intersection
        assert_eq!(tu.allowed_tools, Some(vec!["b".into(), "c".into()]));
        // blocked = union
        let blocked = tu.blocked_tools.unwrap();
        assert!(blocked.contains(&"x".to_string()));
        assert!(blocked.contains(&"y".to_string()));
    }

    #[test]
    fn merge_governance_system_prompt() {
        let mut base = LlmGovernance {
            system_prompt: Some(SystemPromptGovernance {
                mandatory_fragments: Some(vec![MandatoryFragment {
                    position: "prepend".to_string(),
                    text: "You are a helpful assistant.".to_string(),
                }]),
                banned_patterns: Some(vec!["ignore instructions".to_string()]),
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            system_prompt: Some(SystemPromptGovernance {
                mandatory_fragments: Some(vec![
                    MandatoryFragment {
                        position: "append".to_string(),
                        text: "Be concise.".to_string(),
                    },
                    // Duplicate text should be skipped
                    MandatoryFragment {
                        position: "prepend".to_string(),
                        text: "You are a helpful assistant.".to_string(),
                    },
                ]),
                banned_patterns: Some(vec!["bypass safety".to_string()]),
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        let sp = base.system_prompt.unwrap();
        let frags = sp.mandatory_fragments.unwrap();
        assert_eq!(frags.len(), 2); // duplicate skipped
        assert_eq!(frags[0].text, "You are a helpful assistant.");
        assert_eq!(frags[1].text, "Be concise.");
        let banned = sp.banned_patterns.unwrap();
        assert!(banned.contains(&"ignore instructions".to_string()));
        assert!(banned.contains(&"bypass safety".to_string()));
    }

    #[test]
    fn merge_governance_empty_child_leaves_base_unchanged() {
        let mut base = LlmGovernance {
            temperature: Some(TemperatureGovernance {
                max: Some(0.8),
                enforce: Some("clamp".to_string()),
            }),
            ..Default::default()
        };
        let child = LlmGovernance::default();
        merge_governance(&mut base, &child);
        assert_eq!(base.temperature.as_ref().unwrap().max, Some(0.8));
    }

    #[test]
    fn merge_governance_tool_rate_limits() {
        let mut base = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                max_tool_calls_per_request: Some(20),
                max_tool_calls_per_hour: Some(500),
                max_tool_calls_per_day: Some(5000),
                ..Default::default()
            }),
            ..Default::default()
        };
        let child = LlmGovernance {
            tool_use: Some(ToolUseGovernance {
                max_tool_calls_per_request: Some(10),
                max_tool_calls_per_hour: Some(1000),
                max_tool_calls_per_day: Some(3000),
                ..Default::default()
            }),
            ..Default::default()
        };
        merge_governance(&mut base, &child);
        let tu = base.tool_use.unwrap();
        assert_eq!(tu.max_tool_calls_per_request, Some(10)); // min
        assert_eq!(tu.max_tool_calls_per_hour, Some(500)); // min
        assert_eq!(tu.max_tool_calls_per_day, Some(3000)); // min
    }

    /// The typed LLM governance merge reads a
    /// child's `null` as "not stated", so the parent's value stays; a value
    /// of the wrong type does not parse and the collector refuses.
    #[test]
    fn a_childs_null_keeps_the_parents_governance() {
        let mut base: LlmGovernance = serde_json::from_value(serde_json::json!({
            "temperature": {"max": 0.5, "enforce": "reject"},
            "context_window": {"max_input_tokens": 100, "max_output_tokens": 50},
            "tool_use": {"access": "disabled", "max_tool_calls_per_request": 2,
                         "allowed_tools": ["a"], "blocked_tools": ["b"]}
        }))
        .unwrap();
        let child: LlmGovernance = serde_json::from_value(serde_json::json!({
            "temperature": {"max": null, "enforce": null},
            "context_window": {"max_input_tokens": null, "max_output_tokens": null},
            "tool_use": {"access": null, "max_tool_calls_per_request": null,
                         "allowed_tools": null, "blocked_tools": null}
        }))
        .unwrap();
        merge_governance(&mut base, &child);
        let t = base.temperature.unwrap();
        assert_eq!((t.max, t.enforce.as_deref()), (Some(0.5), Some("reject")));
        let c = base.context_window.unwrap();
        assert_eq!(
            (c.max_input_tokens, c.max_output_tokens),
            (Some(100), Some(50))
        );
        let tu = base.tool_use.unwrap();
        assert_eq!(tu.access.as_deref(), Some("disabled"));
        assert_eq!(tu.max_tool_calls_per_request, Some(2));
        assert_eq!(tu.allowed_tools, Some(vec!["a".to_string()]));
        assert_eq!(tu.blocked_tools, Some(vec!["b".to_string()]));
        assert!(
            serde_json::from_value::<LlmGovernance>(serde_json::json!({
                "tool_use": {"max_tool_calls_per_request": "two"}
            }))
            .is_err(),
            "a malformed value does not parse (the collector refuses)"
        );
    }
}

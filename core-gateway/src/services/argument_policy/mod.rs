// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # AST-aware Argument Policy
//!
//! POLICY-10 follow-up. Where guardrails ask "does this *text*
//! contain something bad", argument policies ask "does this
//! *structured tool argument* do something bad" — e.g. is the SQL
//! a write, is the URL a private IP, is the shell command outside
//! the allowlist.
//!
//! ## Layers
//!
//! 1. **Analyzers** — domain-specific parsers (`sql`, `url`,
//!    `shell`, `path`, `json`). Each implements [`Analyzer`] and
//!    returns a normalized [`ArgFacts`] struct.
//! 2. **Evaluator** — [`evaluator::evaluate`] runs a per-policy
//!    rule list against the produced facts. Predicates are a
//!    fixed registry; rules are declarative `{deny_if, ...}` /
//!    `{allow_only_if, ...}` records.
//! 3. **Surface wires** — call sites in `llm_governance.rs`,
//!    `routes/mcp.rs`, `routes/skills.rs` that pick the right
//!    analyzer based on the policy's `analyzer` field and feed
//!    the matching `argument_key` value through.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub mod cache;
pub mod enforce;
pub mod evaluator;
pub mod path_analyzer;
pub mod policy;
pub mod shell;
pub mod sql;
pub mod url_analyzer;

pub use cache::ArgumentPolicyCache;
pub use enforce::{Arguments, enforce, enforce_view};

pub use evaluator::{Violation, evaluate};
pub use policy::{ArgumentPolicy, Rule, Severity, Surface};

/// What kind of argument we just parsed. The evaluator dispatches
/// predicates against the matching variant's facts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ArgKind {
    Sql,
    Url,
    Shell,
    Path,
    Json,
}

/// Normalized facts extracted by an analyzer. Each predicate in
/// the evaluator reads from this struct rather than re-parsing
/// the raw value, so adding a new predicate is a single match arm.
///
/// Variant choice is driven by [`ArgKind`]; only the matching
/// field is populated per parse.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ArgFacts {
    pub kind: ArgKindTag,
    /// SQL statement category (SELECT/INSERT/UPDATE/DELETE/DDL/UNKNOWN),
    /// one entry per parsed statement (multi-statement payloads).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sql_statement_types: Vec<String>,
    /// SQL tables read by the query.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sql_tables_read: Vec<String>,
    /// SQL tables written by the query.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub sql_tables_written: Vec<String>,
    /// Maximum subquery nesting depth seen.
    #[serde(default, skip_serializing_if = "is_zero_u32")]
    pub sql_subquery_depth: u32,
    /// True when the payload contained more than one SQL statement.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub sql_multi_statement: bool,
    /// True when sqlparser failed to parse — handled as a separate
    /// signal so the evaluator can choose to fail-closed.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub sql_parse_error: bool,
    /// True when `shellwords::split` failed (unbalanced quoting).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub shell_parse_error: bool,
    /// True when the input is not an absolute URL.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub url_parse_error: bool,

    /// URL scheme (lowercased, e.g. "https").
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url_scheme: Option<String>,
    /// URL host (lowercased).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url_host: Option<String>,
    /// Coarse classification: public, loopback, private, link_local,
    /// cgnat, multicast, unspecified, metadata, unknown.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url_ip_class: Option<String>,

    /// Shell argv after `shellwords::split`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub shell_argv: Vec<String>,
    /// First argv token (the command).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shell_command: Option<String>,
    /// Tokens starting with `-` or `--`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub shell_flags: Vec<String>,

    /// True when the path contains `..` segments.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub path_traverses_parent: bool,
    /// True when the path is absolute.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub path_is_absolute: bool,
    /// Normalized path (no `.`/`..` segments).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_normalized: Option<String>,

    /// JSON Schema validation errors (jsonschema crate output).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub json_schema_errors: Vec<String>,
    /// JSON top-level keys observed.
    ///
    /// Retained unchanged so the existing `field_present` /
    /// `field_not_present` predicates keep their current semantics. The
    /// nested form is opt-in: a path containing a dot is looked up in
    /// `json_leaf_paths` instead.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub json_top_keys: Vec<String>,
    /// Numeric leaves by dotted path — `amount`, `transfer.amount`,
    /// `items.0.qty`. Read by the comparison predicates.
    ///
    /// `NaN` and infinities never enter this map: they compare false against
    /// every threshold, which would silently disable a band.
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub json_numbers: std::collections::BTreeMap<String, f64>,
    /// String leaves by dotted path, for the equality and membership
    /// predicates.
    #[serde(default, skip_serializing_if = "std::collections::BTreeMap::is_empty")]
    pub json_strings: std::collections::BTreeMap<String, String>,
    /// Every leaf path seen, so `field_present` can answer for nested paths.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub json_leaf_paths: Vec<String>,
    /// Every path that holds an object or an array.
    ///
    /// A container has no leaf of its own (`{"amount": {}}` records
    /// nothing in `json_leaf_paths`), so without this a comparison could not
    /// tell "the field is absent" from "the field is there and is not a
    /// scalar". Read by the evaluator only; not
    /// part of the facts echoed in a refusal.
    #[serde(skip)]
    pub json_container_paths: std::collections::BTreeSet<String>,
    /// Boolean leaves by dotted path, for the equality and membership
    /// predicates with a boolean operand. Read by the
    /// evaluator only; not part of the facts echoed in a refusal.
    #[serde(skip)]
    pub json_bools: std::collections::BTreeMap<String, bool>,
    /// Paths whose number arrived as a quoted string.
    ///
    /// LLM tool calls routinely emit `{"amount": "8000.00"}`. Coercing is the
    /// default because a policy that silently stops applying when the model
    /// quotes the number is worse than one that compares a coerced value;
    /// under `analyzer_config: {"strict_numbers": true}` the quoted number is
    /// not coerced, and a comparison rule over it refuses the call.
    #[serde(default, skip_serializing_if = "std::collections::BTreeSet::is_empty")]
    pub json_coerced_paths: std::collections::BTreeSet<String>,
    /// Paths that are present in more than one part of an argument that
    /// arrives in parts (the data parts of an A2A message).
    ///
    /// Such a path has no one value: which part the receiver acts on is its
    /// own choice. A comparison over it is undecidable; presence is not in
    /// doubt, so the path stays in `json_leaf_paths` / `json_container_paths`
    /// and no value is recorded for it. Read by the evaluator only.
    #[serde(skip)]
    pub json_ambiguous_paths: std::collections::BTreeSet<String>,
    /// The value a non-JSON analyzer was to read is given by more than one
    /// part, so there is no one value to analyze: every predicate over
    /// these facts is undecidable. Read by the evaluator only.
    #[serde(skip)]
    pub ambiguous_input: bool,
    /// A traversal cap was hit, so the fact set is incomplete.
    ///
    /// A policy evaluated against a truncated fact set cannot be trusted to
    /// have seen the field it compares, so `evaluator::evaluate` refuses the
    /// call (rule `arguments_truncated`) rather than quietly comparing what
    /// it managed to reach.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub json_truncated: bool,

    /// Raw input echoed for audit purposes — truncated to 1KB.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw_excerpt: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum ArgKindTag {
    #[default]
    Unknown,
    Sql,
    Url,
    Shell,
    Path,
    Json,
}

fn is_zero_u32(n: &u32) -> bool {
    *n == 0
}

/// Dispatch to the right analyzer. Returns the produced facts;
/// on parse error returns facts with the kind-specific `parse_error`
/// flag set so the evaluator can fail-closed if the operator wants.
pub fn analyze(value: &Value, kind: ArgKind, config: Option<&Value>) -> ArgFacts {
    // All analyzers accept either a JSON string or a JSON value that
    // happens to already be the target type. Most tools serialize args
    // as strings, but JSON-Schema-validated tools might use objects.
    let as_string = match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    };

    match kind {
        ArgKind::Sql => sql::analyze(&as_string, config),
        ArgKind::Url => url_analyzer::analyze(&as_string),
        ArgKind::Shell => shell::analyze(&as_string),
        ArgKind::Path => path_analyzer::analyze(&as_string, config),
        ArgKind::Json => {
            // For JSON we want the parsed value, not the stringified form.
            match value {
                Value::String(_) => serde_json::from_str::<Value>(&as_string)
                    .map(|v| analyze_json(&v, config))
                    .unwrap_or_else(|e| ArgFacts {
                        kind: ArgKindTag::Json,
                        json_schema_errors: vec![format!("invalid JSON input: {e}")],
                        raw_excerpt: truncate_excerpt(&as_string),
                        ..Default::default()
                    }),
                other => analyze_json(other, config),
            }
        }
    }
}

/// How much of an argument the JSON analyzer walks before it gives up.
///
/// `analyze_json` runs on every call a policy targets, so without bounds a
/// hostile argument turns a hot-path parse into an unbounded tree walk.
/// Reaching any limit sets `json_truncated`, and a truncated analysis is a
/// refusal (`evaluator::RULE_ARGUMENTS_TRUNCATED`), never a comparison of
/// what was reached.
///
/// A policy states its own limits in `analyzer_config` (`max_leaves`,
/// `max_depth`, `max_array_elements`) when the tool it governs legitimately
/// takes a larger argument than the defaults allow; each is bounded by a
/// hard ceiling. Each policy analyses under its own limits, so a call that
/// several policies target passes only when it fits every one of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AnalysisLimits {
    /// Scalar leaves recorded (and, separately, containers recorded).
    pub max_leaves: usize,
    /// Nesting below the argument itself: a top-level field is at depth 1.
    pub max_depth: usize,
    /// Elements read of any one array.
    pub max_array_elements: usize,
}

impl AnalysisLimits {
    /// What a policy that states no limit analyses under.
    pub const DEFAULT: Self = Self {
        max_leaves: 512,
        max_depth: 8,
        max_array_elements: 64,
    };

    /// The most a policy may ask for.
    ///
    /// The cost of one policy on one call is linear in the leaves it
    /// records, and independent of how they are nested. Measured with the
    /// `argument_analysis` bench (`benches/argument_analysis.rs`, release
    /// build): about 0.2 µs per leaf, so 3 µs for a 16-leaf argument, 90 µs
    /// at the default limit and 0.8 ms at this ceiling; 32,768 leaves cost
    /// 7 ms and 131,072 cost 28 ms. The ceiling is where one policy still
    /// costs under a millisecond: beyond it a caller buys gateway time, per
    /// call and per policy, for an argument no tool schema asks for.
    ///
    /// `max_array_elements` cannot usefully exceed a quarter of the leaves
    /// (an argument is rarely one array), and `max_depth` stays far inside
    /// what the recursive walk and the JSON reader (which refuses nesting
    /// beyond 128) can take.
    pub const CEILING: Self = Self {
        max_leaves: 4096,
        max_depth: 32,
        max_array_elements: 1024,
    };

    /// The limits `config` states, each defaulting when absent.
    ///
    /// A stated limit must be an integer from 1 to its ceiling. Anything
    /// else (zero, negative, fractional, a string, `null`, over the ceiling)
    /// is an error naming the key: the policy is not one this gateway can
    /// apply, and the caller refuses. It is never clamped: a clamped limit
    /// analyses under a bound the operator did not write.
    pub fn from_config(config: Option<&Value>) -> Result<Self, InvalidAnalysisLimit> {
        let read = |key: &'static str, default: usize, ceiling: usize| match config
            .and_then(|c| c.get(key))
        {
            None => Ok(default),
            Some(stated) => stated
                .as_u64()
                .and_then(|n| usize::try_from(n).ok())
                .filter(|n| (1..=ceiling).contains(n))
                .ok_or(InvalidAnalysisLimit { key, ceiling }),
        };
        Ok(Self {
            max_leaves: read(
                "max_leaves",
                Self::DEFAULT.max_leaves,
                Self::CEILING.max_leaves,
            )?,
            max_depth: read(
                "max_depth",
                Self::DEFAULT.max_depth,
                Self::CEILING.max_depth,
            )?,
            max_array_elements: read(
                "max_array_elements",
                Self::DEFAULT.max_array_elements,
                Self::CEILING.max_array_elements,
            )?,
        })
    }
}

/// An analysis limit in `analyzer_config` that is not an integer from 1 to
/// its ceiling. Names the key, never the value.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidAnalysisLimit {
    pub key: &'static str,
    pub ceiling: usize,
}

impl std::fmt::Display for InvalidAnalysisLimit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "analyzer_config.{} must be an integer from 1 to {}",
            self.key, self.ceiling
        )
    }
}

/// Flatten a JSON value to dotted paths, recording numeric and string leaves.
///
/// Object keys and array indices both become path segments, so
/// `{"items": [{"qty": 3}]}` yields `items.0.qty`. Keys containing a dot are
/// recorded as-is; a policy author targeting such a key gets an unambiguous
/// path only if the key itself has no dot, which is stated in the docs
/// rather than worked around here.
fn flatten_json(
    value: &Value,
    prefix: &str,
    depth: usize,
    facts: &mut ArgFacts,
    strict_numbers: bool,
    limits: &AnalysisLimits,
) {
    if depth > limits.max_depth {
        facts.json_truncated = true;
        return;
    }
    if facts.json_leaf_paths.len() >= limits.max_leaves {
        facts.json_truncated = true;
        return;
    }

    if !prefix.is_empty() && matches!(value, Value::Object(_) | Value::Array(_)) {
        // Bounded like the leaves: an argument of nothing but empty
        // containers must not grow this set without limit.
        if facts.json_container_paths.len() >= limits.max_leaves {
            facts.json_truncated = true;
            return;
        }
        facts.json_container_paths.insert(prefix.to_string());
    }

    match value {
        Value::Object(map) => {
            for (k, v) in map {
                let path = if prefix.is_empty() {
                    k.clone()
                } else {
                    format!("{prefix}.{k}")
                };
                flatten_json(v, &path, depth + 1, facts, strict_numbers, limits);
            }
        }
        Value::Array(items) => {
            for (i, v) in items.iter().enumerate() {
                if i >= limits.max_array_elements {
                    facts.json_truncated = true;
                    break;
                }
                let path = if prefix.is_empty() {
                    i.to_string()
                } else {
                    format!("{prefix}.{i}")
                };
                flatten_json(v, &path, depth + 1, facts, strict_numbers, limits);
            }
        }
        leaf => {
            if prefix.is_empty() {
                return; // a bare scalar has no path to record
            }
            facts.json_leaf_paths.push(prefix.to_string());
            match leaf {
                Value::Number(n) => {
                    if let Some(f) = n.as_f64() {
                        // NaN/inf compare false against every threshold,
                        // which would silently disable a band.
                        if f.is_finite() {
                            facts.json_numbers.insert(prefix.to_string(), f);
                        }
                    }
                }
                Value::String(text) => {
                    facts.json_strings.insert(prefix.to_string(), text.clone());
                    if !strict_numbers {
                        if let Ok(f) = text.trim().parse::<f64>() {
                            if f.is_finite() {
                                facts.json_numbers.insert(prefix.to_string(), f);
                                facts.json_coerced_paths.insert(prefix.to_string());
                            }
                        }
                    }
                }
                Value::Bool(b) => {
                    facts.json_bools.insert(prefix.to_string(), *b);
                }
                _ => {}
            }
        }
    }
}

fn strict_numbers(config: Option<&Value>) -> bool {
    config
        .and_then(|c| c.get("strict_numbers"))
        .and_then(|v| v.as_bool())
        .unwrap_or(false)
}

fn analyze_json(value: &Value, config: Option<&Value>) -> ArgFacts {
    let mut facts = ArgFacts {
        kind: ArgKindTag::Json,
        raw_excerpt: truncate_excerpt(&value.to_string()),
        ..Default::default()
    };
    if let Value::Object(map) = value {
        facts.json_top_keys = map.keys().cloned().collect();
    }

    // Limits this gateway does not accept are refused where the policy is
    // loaded and again where it is enforced. Reached with them anyway, the
    // analysis is one that did not complete: nothing is walked and the
    // facts are marked truncated, which every evaluation refuses.
    match AnalysisLimits::from_config(config) {
        Ok(limits) => flatten_json(value, "", 0, &mut facts, strict_numbers(config), &limits),
        Err(_) => facts.json_truncated = true,
    }
    check_required(&mut facts, config, value.is_object());
    facts
}

/// `analyzer_config.schema.required`: each listed field that the analyzed
/// facts do not hold is a schema error (`schema_invalid`). A bare name is a
/// top-level key; a dotted one is a leaf path, so `transfer.amount` can be
/// required. `is_object` is whether the argument is an object at all.
fn check_required(facts: &mut ArgFacts, config: Option<&Value>, is_object: bool) {
    let Some(required) = config
        .and_then(|c| c.get("schema"))
        .and_then(|s| s.get("required"))
        .and_then(|v| v.as_array())
    else {
        return;
    };
    if !is_object {
        facts.json_schema_errors.push("expected object".into());
        return;
    }
    for field in required.iter().filter_map(Value::as_str) {
        let present = if field.contains('.') {
            facts.json_leaf_paths.iter().any(|p| p == field)
        } else {
            facts.json_top_keys.iter().any(|k| k == field)
        };
        if !present {
            facts
                .json_schema_errors
                .push(format!("required field missing: {field}"));
        }
    }
}

/// Analyze an argument that arrives in PARTS as one argument: the data
/// parts of an A2A message, each a value of its own.
///
/// A policy author addresses a field by its name (`amount`), not by which
/// part carries it, because the position of a part is the caller's choice.
/// So the parts are read as one set of fields:
///
/// * a path present in exactly one part has that part's value;
/// * a path present in more than one part has no one value
///   (`json_ambiguous_paths`): a comparison over it is undecidable, which
///   is a violation in both directions, while a presence guard still sees
///   it;
/// * no parts is an argument with no fields, so a presence guard or a
///   required field refuses it.
///
/// One part is that part, analyzed exactly as [`analyze`] would. The
/// policy's limits bound the whole: the parts together may record no more
/// leaves than `max_leaves`, and there may be no more of them than
/// `max_array_elements` (they are the elements of the message's `parts`).
///
/// For the analyzers that read ONE value (SQL, URL, shell, path), more than
/// one part offering it is `ambiguous_input`.
pub fn analyze_parts(parts: &[&Value], kind: ArgKind, config: Option<&Value>) -> ArgFacts {
    match (parts, kind) {
        ([], _) => analyze(&Value::Object(Default::default()), kind, config),
        ([one], _) => analyze(one, kind, config),
        (_, ArgKind::Json) => merge_json_parts(parts, config),
        (first, _) => {
            let mut facts = analyze(first[0], kind, config);
            facts.ambiguous_input = true;
            facts
        }
    }
}

fn merge_json_parts(parts: &[&Value], config: Option<&Value>) -> ArgFacts {
    use std::collections::{BTreeMap, BTreeSet};
    let mut merged = ArgFacts {
        kind: ArgKindTag::Json,
        raw_excerpt: truncate_excerpt(
            &Value::Array(parts.iter().map(|p| (*p).clone()).collect()).to_string(),
        ),
        ..Default::default()
    };
    let Ok(limits) = AnalysisLimits::from_config(config) else {
        merged.json_truncated = true;
        return merged;
    };
    if parts.len() > limits.max_array_elements {
        merged.json_truncated = true;
        return merged;
    }
    // In how many parts each path is present, as a leaf or as a container.
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    let mut top_keys: BTreeSet<String> = BTreeSet::new();
    let mut leaves = 0usize;
    for part in parts {
        // A JSON string holding JSON is read as `analyze` reads it.
        let facts = analyze(part, ArgKind::Json, config);
        leaves += facts.json_leaf_paths.len();
        if facts.json_truncated || leaves > limits.max_leaves {
            merged.json_truncated = true;
            return merged;
        }
        // A part that is not an object has no fields to merge by name; an
        // unparsable one carries its own error.
        merged.json_schema_errors.extend(
            facts
                .json_schema_errors
                .into_iter()
                .filter(|e| e.starts_with("invalid JSON input")),
        );
        for path in facts
            .json_leaf_paths
            .iter()
            .chain(facts.json_container_paths.iter())
        {
            *seen.entry(path.clone()).or_default() += 1;
        }
        top_keys.extend(facts.json_top_keys);
        merged.json_leaf_paths.extend(facts.json_leaf_paths);
        merged
            .json_container_paths
            .extend(facts.json_container_paths);
        merged.json_numbers.extend(facts.json_numbers);
        merged.json_strings.extend(facts.json_strings);
        merged.json_bools.extend(facts.json_bools);
        merged.json_coerced_paths.extend(facts.json_coerced_paths);
    }
    merged.json_leaf_paths.sort();
    merged.json_leaf_paths.dedup();
    merged.json_top_keys = top_keys.into_iter().collect();
    for (path, _) in seen.into_iter().filter(|(_, n)| *n > 1) {
        merged.json_numbers.remove(&path);
        merged.json_strings.remove(&path);
        merged.json_bools.remove(&path);
        merged.json_coerced_paths.remove(&path);
        merged.json_ambiguous_paths.insert(path);
    }
    check_required(&mut merged, config, parts.iter().any(|p| p.is_object()));
    merged
}

pub(crate) fn truncate_excerpt(s: &str) -> Option<String> {
    if s.is_empty() {
        None
    } else if s.len() > 1024 {
        Some(format!("{}…", &s[..1024]))
    } else {
        Some(s.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn truncate_excerpt_empty_returns_none() {
        assert!(truncate_excerpt("").is_none());
    }

    #[test]
    fn truncate_excerpt_short_string_passes_through() {
        assert_eq!(truncate_excerpt("hello").as_deref(), Some("hello"));
    }

    #[test]
    fn truncate_excerpt_long_string_truncates_with_ellipsis() {
        let s = "a".repeat(2000);
        let out = truncate_excerpt(&s).unwrap();
        assert_eq!(out.chars().count(), 1025); // 1024 'a' + '…'
        assert!(out.ends_with('…'));
    }

    #[test]
    fn analyze_dispatches_to_correct_analyzer_kind() {
        let f = analyze(&json!("SELECT 1"), ArgKind::Sql, None);
        assert_eq!(f.kind, ArgKindTag::Sql);

        let f = analyze(&json!("https://example.com"), ArgKind::Url, None);
        assert_eq!(f.kind, ArgKindTag::Url);

        let f = analyze(&json!("ls -la"), ArgKind::Shell, None);
        assert_eq!(f.kind, ArgKindTag::Shell);

        let f = analyze(&json!("/tmp/file"), ArgKind::Path, None);
        assert_eq!(f.kind, ArgKindTag::Path);

        let f = analyze(&json!({"a": 1}), ArgKind::Json, None);
        assert_eq!(f.kind, ArgKindTag::Json);
    }

    #[test]
    fn json_analyzer_required_field_check() {
        let facts = analyze(
            &json!({"a": 1}),
            ArgKind::Json,
            Some(&json!({"schema":{"required":["a","b"]}})),
        );
        assert_eq!(facts.json_schema_errors.len(), 1);
        assert!(facts.json_schema_errors[0].contains("b"));
    }

    #[test]
    fn limits_default_when_the_policy_states_none() {
        for config in [None, Some(json!({})), Some(json!({"strict_numbers": true}))] {
            assert_eq!(
                AnalysisLimits::from_config(config.as_ref()),
                Ok(AnalysisLimits::DEFAULT)
            );
        }
        assert_eq!(
            AnalysisLimits::DEFAULT,
            AnalysisLimits {
                max_leaves: 512,
                max_depth: 8,
                max_array_elements: 64
            }
        );
    }

    #[test]
    fn a_stated_limit_is_taken_up_to_its_ceiling() {
        let c = AnalysisLimits::CEILING;
        assert_eq!(
            AnalysisLimits::from_config(Some(&json!({"max_leaves": 2000}))),
            Ok(AnalysisLimits {
                max_leaves: 2000,
                ..AnalysisLimits::DEFAULT
            })
        );
        // Below the default is a limit too: a policy may be stricter.
        assert_eq!(
            AnalysisLimits::from_config(Some(
                &json!({"max_leaves": 1, "max_depth": 1, "max_array_elements": 1})
            )),
            Ok(AnalysisLimits {
                max_leaves: 1,
                max_depth: 1,
                max_array_elements: 1
            })
        );
        assert_eq!(
            AnalysisLimits::from_config(Some(&json!({
                "max_leaves": c.max_leaves,
                "max_depth": c.max_depth,
                "max_array_elements": c.max_array_elements
            }))),
            Ok(c)
        );
    }

    /// Over the ceiling, zero, negative, fractional, not a number: an error
    /// naming the key. Never the nearest acceptable value.
    #[test]
    fn a_limit_that_is_not_an_integer_from_one_to_its_ceiling_is_an_error() {
        let c = AnalysisLimits::CEILING;
        for (key, ceiling) in [
            ("max_leaves", c.max_leaves),
            ("max_depth", c.max_depth),
            ("max_array_elements", c.max_array_elements),
        ] {
            for bad in [
                json!(ceiling + 1),
                json!(u64::MAX),
                json!(0),
                json!(-1),
                json!(16.0),
                json!(16.5),
                json!("16"),
                json!(null),
                json!(true),
                json!([16]),
                json!({"value": 16}),
            ] {
                let config = json!({ key: bad });
                assert_eq!(
                    AnalysisLimits::from_config(Some(&config)),
                    Err(InvalidAnalysisLimit { key, ceiling }),
                    "{config}"
                );
            }
        }
    }

    fn nested(depth: usize) -> Value {
        let mut v = json!(1);
        for _ in 0..depth {
            v = json!({ "n": v });
        }
        v
    }

    fn wide(leaves: usize) -> Value {
        Value::Object(
            (0..leaves)
                .map(|i| (format!("k{i:05}"), json!(i)))
                .collect(),
        )
    }

    /// Each limit is the boundary it says it is: at the limit the analysis
    /// is complete, one beyond it is truncated. Under the defaults and
    /// under limits a policy states.
    #[test]
    fn each_limit_is_exact_under_the_defaults_and_when_stated() {
        let truncated = |v: &Value, config: Option<Value>| {
            analyze(v, ArgKind::Json, config.as_ref()).json_truncated
        };
        // Depth: a leaf `depth` levels down.
        assert!(!truncated(&nested(8), None));
        assert!(truncated(&nested(9), None));
        let deeper = Some(json!({"max_depth": 20}));
        assert!(!truncated(&nested(20), deeper.clone()));
        assert!(truncated(&nested(21), deeper));
        // Leaves.
        assert!(!truncated(&wide(512), None));
        assert!(truncated(&wide(513), None));
        let more = Some(json!({"max_leaves": 3000}));
        assert!(!truncated(&wide(3000), more.clone()));
        assert!(truncated(&wide(3001), more.clone()));
        let facts = analyze(&wide(3000), ArgKind::Json, more.as_ref());
        assert_eq!(facts.json_numbers.len(), 3000);
        assert_eq!(facts.json_numbers.get("k02999"), Some(&2999.0));
        // Array elements.
        let array = |n: usize| json!({"xs": (0..n).collect::<Vec<_>>()});
        assert!(!truncated(&array(64), None));
        assert!(truncated(&array(65), None));
        let wider = Some(json!({"max_array_elements": 300}));
        assert!(!truncated(&array(300), wider.clone()));
        assert!(truncated(&array(301), wider));
        // A stricter policy truncates what the defaults would walk.
        assert!(truncated(&wide(11), Some(json!({"max_leaves": 10}))));
    }

    /// The ceilings themselves are usable: an argument that reaches all
    /// three is analysed completely.
    #[test]
    fn an_argument_at_every_ceiling_is_analysed_completely() {
        let c = AnalysisLimits::CEILING;
        let config = json!({
            "max_leaves": c.max_leaves,
            "max_depth": c.max_depth,
            "max_array_elements": c.max_array_elements
        });
        // Four arrays of the widest length, under a chain as deep as allowed.
        let mut v = Value::Object(
            (0..c.max_leaves / c.max_array_elements)
                .map(|i| {
                    (
                        format!("a{i}"),
                        json!((0..c.max_array_elements).collect::<Vec<_>>()),
                    )
                })
                .collect(),
        );
        for _ in 0..c.max_depth - 2 {
            v = json!({ "n": v });
        }
        let facts = analyze(&v, ArgKind::Json, Some(&config));
        assert!(!facts.json_truncated);
        assert_eq!(facts.json_leaf_paths.len(), c.max_leaves);
    }

    /// Limits this gateway does not accept never analyse under some other
    /// bound: the facts are marked truncated and nothing is recorded.
    #[test]
    fn analysis_under_limits_it_cannot_accept_is_truncated_not_clamped() {
        let over = json!({"max_leaves": AnalysisLimits::CEILING.max_leaves + 1});
        let facts = analyze(&json!({"amount": 5}), ArgKind::Json, Some(&over));
        assert!(facts.json_truncated);
        assert!(facts.json_numbers.is_empty());
    }

    #[test]
    fn one_part_is_analyzed_as_the_value_it_is_and_none_as_no_fields() {
        let part = json!({"amount": "8000", "to": {"account": "A-1"}});
        let config = json!({"strict_numbers": true, "schema": {"required": ["amount", "memo"]}});
        let (one, whole) = (
            analyze_parts(&[&part], ArgKind::Json, Some(&config)),
            analyze(&part, ArgKind::Json, Some(&config)),
        );
        assert_eq!(
            serde_json::to_value(&one).unwrap(),
            serde_json::to_value(&whole).unwrap()
        );
        assert_eq!(one.json_container_paths, whole.json_container_paths);
        let none = analyze_parts(&[], ArgKind::Json, Some(&config));
        assert!(none.json_top_keys.is_empty() && none.json_leaf_paths.is_empty());
        assert_eq!(none.json_schema_errors.len(), 2);
    }

    #[test]
    fn parts_merge_into_one_set_of_fields() {
        let (a, b) = (
            json!({"amount": 5, "to": {"account": "A-1"}, "ok": true}),
            json!({"amount": 7, "memo": "m", "to": {"name": "n"}}),
        );
        let config = json!({"schema": {"required": ["memo", "ok", "to.account", "missing"]}});
        let f = analyze_parts(&[&a, &b], ArgKind::Json, Some(&config));
        assert_eq!(f.json_top_keys, ["amount", "memo", "ok", "to"]);
        // Given once: its value. Given twice: present, ambiguous, no value.
        assert_eq!(f.json_strings.get("memo").map(String::as_str), Some("m"));
        assert_eq!(
            f.json_strings.get("to.account").map(String::as_str),
            Some("A-1")
        );
        assert_eq!(f.json_bools.get("ok"), Some(&true));
        assert!(!f.json_numbers.contains_key("amount"));
        assert!(f.json_leaf_paths.iter().any(|p| p == "amount"));
        assert_eq!(
            f.json_ambiguous_paths.iter().collect::<Vec<_>>(),
            ["amount", "to"]
        );
        // A field is required of the message, not of each part.
        assert_eq!(f.json_schema_errors, ["required field missing: missing"]);
        assert!(!f.json_truncated);
    }

    #[test]
    fn json_analyzer_top_keys_extracted() {
        let facts = analyze(&json!({"x": 1, "y": 2}), ArgKind::Json, None);
        assert_eq!(facts.json_top_keys.len(), 2);
        assert!(facts.json_top_keys.contains(&"x".to_string()));
    }
}

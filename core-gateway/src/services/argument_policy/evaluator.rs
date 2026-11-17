// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Predicate evaluator.
//!
//! Given a parsed [`ArgFacts`] and a list of [`Rule`]s, return
//! the first violation (or None if all rules pass). The predicate
//! registry is fixed — adding a predicate is one match arm here +
//! the same name in the control-plane Pydantic catalogue.

use serde_json::Value;

use super::ArgFacts;
use super::policy::{ArgumentPolicy, Rule, Severity};
use super::sql;

/// One denied rule, ready to surface in the canonical violation envelope.
#[derive(Debug, Clone)]
pub struct Violation {
    pub policy_id: String,
    pub policy_name: String,
    pub tool: String,
    pub argument_key: String,
    pub analyzer: String,
    pub rule: String,
    pub message: String,
    /// The rule's own severity when it set one, otherwise the policy's.
    pub severity: Severity,
    /// The compared path, when a comparison predicate tripped. Carried so
    /// the approver and the audit row can state the number rather than a
    /// bare tool name — the difference between a queue an operator acts on
    /// and one they rubber-stamp.
    pub field: Option<String>,
    /// The value observed at `field`.
    pub observed: Option<f64>,
    /// The threshold it was compared against.
    pub threshold: Option<f64>,
    pub facts: ArgFacts,
}

impl Violation {
    /// The approver-facing detail for a banded call, merged
    /// into the approval request's metadata.
    ///
    /// A bare tool name is a queue entry people rubber-stamp; "amount 8000
    /// exceeds the 1000 threshold set by payments-threshold" is one they can
    /// act on. One definition, so no two surfaces describe the same band
    /// differently.
    pub fn approval_metadata(&self) -> Value {
        serde_json::json!({
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
            "rule": self.rule,
            "field": self.field,
            "observed": self.observed,
            "threshold": self.threshold,
            "reason": self.message,
        })
    }
}

/// The rule name of the refusal for an argument too large or too deep to
/// analyze completely.
pub const RULE_ARGUMENTS_TRUNCATED: &str = "arguments_truncated";

/// What a predicate says about the analyzed argument.
///
/// The third answer is the point. A
/// `bool` forces "could not be decided" into one of the other two, and
/// whichever is chosen, one of `deny_if` / `allow_only_if` inverts it into
/// "allowed": unparsable input that "holds" passes every `allow_only_if`, a
/// field that is present but not a number "does not hold" and passes every
/// `deny_if`. Undecidable is a violation in both directions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Verdict {
    Holds,
    DoesNotHold,
    Undecidable,
}

impl From<bool> for Verdict {
    fn from(holds: bool) -> Self {
        if holds {
            Verdict::Holds
        } else {
            Verdict::DoesNotHold
        }
    }
}

/// A rule that tripped: the predicate it names and whether it tripped
/// because the predicate could not be decided.
struct Tripped {
    predicate: String,
    undecidable: bool,
}

/// Run all rules in `policy` against `facts`. Returns the first
/// violation, or None if nothing tripped.
pub fn evaluate(policy: &ArgumentPolicy, target_name: &str, facts: &ArgFacts) -> Option<Violation> {
    if !policy.enabled {
        return None;
    }
    if !policy.matches_target(target_name) {
        return None;
    }
    // A traversal cap was hit, so the facts are
    // incomplete: a field a rule compares may never have been reached while
    // its presence guard is still satisfied (512 padding leaves sorted
    // before `amount`). No rule can be trusted over such facts, so the call
    // is refused before any of them runs, whatever their severities.
    if facts.json_truncated {
        return Some(Violation {
            policy_id: policy.id.clone(),
            policy_name: policy.name.clone(),
            tool: target_name.to_string(),
            argument_key: policy.argument_key.clone(),
            analyzer: policy.analyzer.clone(),
            rule: RULE_ARGUMENTS_TRUNCATED.to_string(),
            message: "Refused: the arguments are too large or too deeply nested to be \
                      checked against this policy"
                .to_string(),
            severity: Severity::Deny,
            field: None,
            observed: None,
            threshold: None,
            facts: facts.clone(),
        });
    }
    for rule in &policy.rules {
        if let Some(tripped) = check_rule(rule, facts) {
            let field = rule.field.clone();
            let observed = field
                .as_deref()
                .and_then(|p| facts.json_numbers.get(p))
                .copied();
            let mut message = rule
                .message
                .clone()
                .unwrap_or_else(|| "argument policy denied".to_string());
            // The rule's own message describes the comparison
            // ("Transfers over 10,000 …"), which is not what happened when
            // the field could not be compared at all. Say so.
            if tripped.undecidable {
                if let Some(path) = field.as_deref().filter(|p| ambiguous(p, facts)) {
                    message.push_str(&format!(
                        " (refused: field '{path}' is given by more than one data part, so \
                         it has no one value this rule can compare)"
                    ));
                } else if let Some(path) = field.as_deref().filter(|p| field_present(p, facts)) {
                    message.push_str(&format!(
                        " (refused: field '{path}' is present but is not a value this rule \
                         can compare)"
                    ));
                }
            }
            return Some(Violation {
                policy_id: policy.id.clone(),
                policy_name: policy.name.clone(),
                tool: target_name.to_string(),
                argument_key: policy.argument_key.clone(),
                analyzer: policy.analyzer.clone(),
                rule: tripped.predicate,
                message,
                // Per-rule severity is what makes a BAND expressible in one
                // policy: rules run in order and the first match wins, so
                // `> 10000 → deny` above `> 1000 → approval_required` reads
                // as deny / hold / allow. Falling back to the policy's value
                // keeps every existing policy behaving exactly as before.
                severity: rule.severity.unwrap_or(policy.severity),
                field,
                observed,
                threshold: rule.value.as_ref().and_then(|v| v.as_f64()),
                facts: facts.clone(),
            });
        }
    }
    None
}

fn check_rule(rule: &Rule, facts: &ArgFacts) -> Option<Tripped> {
    if let Some(predicate) = &rule.deny_if {
        let verdict = predicate_verdict(predicate, rule.values.as_deref(), facts, rule);
        if verdict != Verdict::DoesNotHold {
            return Some(Tripped {
                predicate: predicate.clone(),
                undecidable: verdict == Verdict::Undecidable,
            });
        }
    }
    if let Some(predicate) = &rule.allow_only_if {
        let verdict = predicate_verdict(predicate, rule.values.as_deref(), facts, rule);
        if verdict != Verdict::Holds {
            return Some(Tripped {
                predicate: predicate.clone(),
                undecidable: verdict == Verdict::Undecidable,
            });
        }
    }
    None
}

/// Whether a target path exists in the argument.
///
/// A bare key keeps its original meaning — a top-level key — so every
/// existing `field_present` policy behaves exactly as before. A dotted
/// target is answered from the flattened leaves, which is what lets a
/// nested threshold be guarded.
fn path_present(target: &str, facts: &ArgFacts) -> bool {
    if target.contains('.') {
        facts.json_leaf_paths.iter().any(|p| p == target)
    } else {
        facts.json_top_keys.iter().any(|k| k == target)
    }
}

/// Whether a compared path exists in the argument at all — as a scalar
/// leaf, or as an object or array (which has no leaf of its own).
fn field_present(path: &str, facts: &ArgFacts) -> bool {
    facts.json_container_paths.contains(path) || facts.json_leaf_paths.iter().any(|p| p == path)
}

/// Whether a compared path is given by more than one part of an argument
/// that arrives in parts (`argument_policy::analyze_parts`). It is present,
/// and it has no one value: a comparison over it is undecidable.
fn ambiguous(path: &str, facts: &ArgFacts) -> bool {
    facts.json_ambiguous_paths.contains(path)
}

/// Numeric comparison against a flattened leaf.
///
/// Does not hold when the field is ABSENT — consistent with every other
/// predicate's per-rule fail-open. That is exactly the omit-the-field
/// bypass, and it is closed at policy level: the control-plane validator
/// refuses a comparison rule whose path is not covered by a presence guard
/// (`validate_comparison_rules_have_a_presence_guard`). Closing it here
/// instead would make a missing field DENY, which would break every
/// optional-argument policy that already relies on fail-open.
///
/// A field that is PRESENT but is not a number the policy compares — `null`,
/// a bool, an array, an object, `"20,000"`, or a quoted `"20000"` under
/// `strict_numbers` — is undecidable. It
/// satisfies the presence guard, so reading it as "does not hold" let the
/// call past the threshold with a value the upstream may well act on.
fn compare_number(rule: &Rule, facts: &ArgFacts, op: fn(f64, f64) -> bool) -> Verdict {
    let Some(path) = rule.field.as_deref() else {
        return Verdict::DoesNotHold;
    };
    let Some(threshold) = rule.value.as_ref().and_then(|v| v.as_f64()) else {
        return Verdict::DoesNotHold;
    };
    if !threshold.is_finite() {
        return Verdict::DoesNotHold;
    }
    if ambiguous(path, facts) {
        return Verdict::Undecidable;
    }
    match facts.json_numbers.get(path) {
        Some(observed) => op(*observed, threshold).into(),
        None if field_present(path, facts) => Verdict::Undecidable,
        None => Verdict::DoesNotHold,
    }
}

/// What a leaf comparison found at the rule's path.
enum Leaf {
    /// No such path (or the rule names no path or operand).
    Absent,
    /// The path holds a value of the operand's type; this is the answer.
    Compared(bool),
    /// The path is present with a value the operand cannot be compared
    /// with: another type, `null`, an object or an array.
    Incomparable,
}

impl Leaf {
    /// `field_eq` / `field_in`.
    fn holds(self) -> Verdict {
        match self {
            Leaf::Absent => Verdict::DoesNotHold,
            Leaf::Compared(matched) => matched.into(),
            Leaf::Incomparable => Verdict::Undecidable,
        }
    }

    /// `field_ne` / `field_not_in`. Absent is not "not equal": a missing
    /// field would otherwise make `deny_if: field_ne` fire on every call
    /// that omits it.
    fn does_not_hold(self) -> Verdict {
        match self {
            Leaf::Absent => Verdict::DoesNotHold,
            Leaf::Compared(matched) => (!matched).into(),
            Leaf::Incomparable => Verdict::Undecidable,
        }
    }
}

/// Equality against a leaf, numeric or string.
fn leaf_equals(rule: &Rule, facts: &ArgFacts) -> Leaf {
    let (Some(path), Some(operand)) = (rule.field.as_deref(), rule.value.as_ref()) else {
        return Leaf::Absent;
    };
    if ambiguous(path, facts) {
        return Leaf::Incomparable;
    }
    if let Some(want) = operand.as_f64() {
        if let Some(observed) = facts.json_numbers.get(path) {
            return Leaf::Compared(*observed == want);
        }
    }
    if let Some(want) = operand.as_str() {
        if let Some(observed) = facts.json_strings.get(path) {
            return Leaf::Compared(observed == want);
        }
    }
    if let Some(want) = operand.as_bool() {
        if let Some(observed) = facts.json_bools.get(path) {
            return Leaf::Compared(*observed == want);
        }
    }
    if field_present(path, facts) {
        Leaf::Incomparable
    } else {
        Leaf::Absent
    }
}

/// Membership of a leaf in `rule.values`.
fn leaf_in_values(rule: &Rule, facts: &ArgFacts) -> Leaf {
    let (Some(path), Some(options)) = (rule.field.as_deref(), rule.values.as_deref()) else {
        return Leaf::Absent;
    };
    if ambiguous(path, facts) {
        return Leaf::Incomparable;
    }
    if let Some(observed) = facts.json_strings.get(path) {
        return Leaf::Compared(
            options
                .iter()
                .any(|v| v.as_str() == Some(observed.as_str())),
        );
    }
    if let Some(observed) = facts.json_numbers.get(path) {
        return Leaf::Compared(
            options
                .iter()
                .any(|v| v.as_f64().map(|n| n == *observed).unwrap_or(false)),
        );
    }
    // A boolean is judged against a list that offers a boolean; against a
    // list of strings or numbers it is a reshaped value.
    if let Some(observed) = facts.json_bools.get(path) {
        if options.iter().any(Value::is_boolean) {
            return Leaf::Compared(options.iter().any(|v| v.as_bool() == Some(*observed)));
        }
    }
    if field_present(path, facts) {
        Leaf::Incomparable
    } else {
        Leaf::Absent
    }
}

fn predicate_verdict(
    predicate: &str,
    values: Option<&[Value]>,
    facts: &ArgFacts,
    rule: &Rule,
) -> Verdict {
    // Fail closed: input the analyzer could not parse cannot be shown
    // to match or not to match, so no predicate over it can be decided.
    // (It never "holds", which `allow_only_if` would invert into "allowed".)
    // Each flag is set only by its own analyzer,
    // and a policy runs exactly one analyzer.
    if facts.sql_parse_error || facts.shell_parse_error || facts.url_parse_error {
        return Verdict::Undecidable;
    }
    // More than one part offers the one value this analyzer reads: there is
    // no one value to have matched or not matched.
    if facts.ambiguous_input {
        return Verdict::Undecidable;
    }
    let holds: bool = match predicate {
        // ---- JSON comparisons -------------------------------
        // A closed set of eight. The general form is the CEL profile of the assurance checks
        // and stays there; this is deliberately not a back door into it.
        "field_gt" => return compare_number(rule, facts, |a, b| a > b),
        "field_gte" => return compare_number(rule, facts, |a, b| a >= b),
        "field_lt" => return compare_number(rule, facts, |a, b| a < b),
        "field_lte" => return compare_number(rule, facts, |a, b| a <= b),
        "field_eq" => return leaf_equals(rule, facts).holds(),
        "field_ne" => return leaf_equals(rule, facts).does_not_hold(),
        "field_in" => return leaf_in_values(rule, facts).holds(),
        "field_not_in" => return leaf_in_values(rule, facts).does_not_hold(),

        // ---- SQL -----------------------------------------------------------
        "writes" => facts
            .sql_statement_types
            .iter()
            .any(|s| sql::label_writes(s)),
        "statement_type_in" => {
            let allowed = string_values(values);
            facts
                .sql_statement_types
                .iter()
                .any(|s| allowed.contains(s))
        }
        "statement_type_not_in" => {
            let allowed = string_values(values);
            facts
                .sql_statement_types
                .iter()
                .any(|s| !allowed.contains(s))
        }
        "touches_any" => {
            let targets = string_values(values);
            let all: Vec<&String> = facts
                .sql_tables_read
                .iter()
                .chain(facts.sql_tables_written.iter())
                .collect();
            all.iter().any(|t| targets.contains(*t))
        }
        "touches_none_of" => {
            let targets = string_values(values);
            let all: Vec<&String> = facts
                .sql_tables_read
                .iter()
                .chain(facts.sql_tables_written.iter())
                .collect();
            !all.iter().any(|t| targets.contains(*t))
        }
        "subquery_depth_gt" => {
            let limit = values
                .and_then(|v| v.first())
                .and_then(|v| v.as_u64())
                .unwrap_or(0) as u32;
            facts.sql_subquery_depth > limit
        }
        "multi_statement" => facts.sql_multi_statement,

        // ---- URL -----------------------------------------------------------
        "host_in_private_range" => matches!(
            facts.url_ip_class.as_deref(),
            Some("private")
                | Some("loopback")
                | Some("link_local")
                | Some("cgnat")
                | Some("metadata")
                | Some("multicast")
                | Some("unspecified")
        ),
        "scheme_in" => {
            let allowed = string_values(values);
            facts
                .url_scheme
                .as_ref()
                .map(|s| allowed.contains(s))
                .unwrap_or(false)
        }
        "scheme_not_in" => {
            let allowed = string_values(values);
            facts
                .url_scheme
                .as_ref()
                .map(|s| !allowed.contains(s))
                .unwrap_or(false)
        }
        "host_in" => {
            let allowed = string_values(values);
            facts
                .url_host
                .as_ref()
                .map(|h| allowed.contains(h))
                .unwrap_or(false)
        }
        "host_not_in" => {
            let allowed = string_values(values);
            facts
                .url_host
                .as_ref()
                .map(|h| !allowed.contains(h))
                .unwrap_or(false)
        }

        // ---- SHELL ---------------------------------------------------------
        "command_in" => {
            let allowed = string_values(values);
            facts
                .shell_command
                .as_ref()
                .map(|c| allowed.contains(c))
                .unwrap_or(false)
        }
        "command_not_in" => {
            let allowed = string_values(values);
            facts
                .shell_command
                .as_ref()
                .map(|c| !allowed.contains(c))
                .unwrap_or(false)
        }
        "flag_present" => {
            let targets = string_values(values);
            facts.shell_flags.iter().any(|f| targets.contains(f))
        }
        "flag_not_present" => {
            let targets = string_values(values);
            !facts.shell_flags.iter().any(|f| targets.contains(f))
        }

        // ---- PATH ----------------------------------------------------------
        "traverses_parent" => facts.path_traverses_parent,
        "absolute_required" => facts.path_is_absolute,
        "inside_root" => {
            let roots = string_values(values);
            let root_refs: Vec<&str> = roots.iter().map(String::as_str).collect();
            facts
                .path_normalized
                .as_ref()
                .map(|p| super::path_analyzer::is_inside_any_root(p, &root_refs))
                .unwrap_or(false)
        }
        "outside_root" => {
            let roots = string_values(values);
            let root_refs: Vec<&str> = roots.iter().map(String::as_str).collect();
            facts
                .path_normalized
                .as_ref()
                .map(|p| !super::path_analyzer::is_inside_any_root(p, &root_refs))
                .unwrap_or(true)
        }

        // ---- JSON ----------------------------------------------------------
        "schema_invalid" => !facts.json_schema_errors.is_empty(),
        "field_present" => {
            let targets = string_values(values);
            // Top-level keys keep their original semantics; a dotted target
            // is answered from the flattened leaves so a nested path can be
            // required too.
            targets.iter().any(|t| path_present(t, facts))
        }
        "field_not_present" => {
            let targets = string_values(values);
            !targets.iter().any(|t| path_present(t, facts))
        }

        // Unknown predicate: one this build cannot evaluate is undecidable
        // (fail closed) — a violation under `deny_if` and
        // under `allow_only_if` too. The control-plane validator
        // should reject it at write time; a newer control plane must not
        // open an older proxy.
        other => {
            tracing::error!(
                predicate = other,
                "unknown argument-policy predicate — refusing"
            );
            return Verdict::Undecidable;
        }
    };
    holds.into()
}

fn string_values(values: Option<&[Value]>) -> Vec<String> {
    values
        .map(|v| {
            v.iter()
                .filter_map(|x| x.as_str().map(String::from))
                .collect()
        })
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::argument_policy::policy::Surface;
    use serde_json::json;

    fn policy(analyzer: &str, rules: Vec<Rule>) -> ArgumentPolicy {
        ArgumentPolicy {
            id: "p1".into(),
            tenant_id: "t".into(),
            name: "test-policy".into(),
            surface: Surface::LlmOutputToolCalls,
            target: "*".into(),
            argument_key: "x".into(),
            analyzer: analyzer.into(),
            analyzer_config: None,
            rules,
            enabled: true,
            severity: Severity::Deny,
        }
    }

    fn rule_deny(name: &str, values: Option<Vec<Value>>) -> Rule {
        Rule {
            deny_if: Some(name.into()),
            allow_only_if: None,
            values,
            field: None,
            value: None,
            severity: None,
            message: Some(format!("denied via {name}")),
        }
    }

    fn rule_allow_only(name: &str, values: Option<Vec<Value>>) -> Rule {
        Rule {
            deny_if: None,
            allow_only_if: Some(name.into()),
            values,
            field: None,
            value: None,
            severity: None,
            message: Some(format!("only allow when {name}")),
        }
    }

    /// A comparison rule: `deny_if <predicate> <field> <value>`.
    fn rule_cmp(predicate: &str, field: &str, value: Value) -> Rule {
        Rule {
            deny_if: Some(predicate.into()),
            allow_only_if: None,
            values: None,
            field: Some(field.into()),
            value: Some(value),
            severity: None,
            message: Some(format!("{field} {predicate} threshold")),
        }
    }

    fn json_facts(v: Value) -> ArgFacts {
        crate::services::argument_policy::analyze(
            &v,
            crate::services::argument_policy::ArgKind::Json,
            None,
        )
    }

    fn json_facts_cfg(v: Value, cfg: Value) -> ArgFacts {
        crate::services::argument_policy::analyze(
            &v,
            crate::services::argument_policy::ArgKind::Json,
            Some(&cfg),
        )
    }

    // ----- SQL ------------------------------------------------------------

    #[test]
    fn writes_predicate_denies_delete() {
        let facts = super::super::sql::analyze("DELETE FROM x WHERE id = 1", None);
        let pol = policy("sql", vec![rule_deny("writes", None)]);
        let v = evaluate(&pol, "some_tool", &facts);
        assert!(v.is_some());
        assert_eq!(v.unwrap().rule, "writes");
    }

    #[test]
    fn writes_predicate_allows_select() {
        let facts = super::super::sql::analyze("SELECT * FROM x", None);
        let pol = policy("sql", vec![rule_deny("writes", None)]);
        assert!(evaluate(&pol, "some_tool", &facts).is_none());
    }

    #[test]
    fn touches_any_predicate_denies_sensitive_table() {
        let facts = super::super::sql::analyze("SELECT * FROM users WHERE id = 1", None);
        let pol = policy(
            "sql",
            vec![rule_deny(
                "touches_any",
                Some(vec![json!("users"), json!("api_keys")]),
            )],
        );
        assert!(evaluate(&pol, "x", &facts).is_some());
    }

    #[test]
    fn touches_any_predicate_allows_safe_table() {
        let facts = super::super::sql::analyze("SELECT * FROM products", None);
        let pol = policy(
            "sql",
            vec![rule_deny("touches_any", Some(vec![json!("users")]))],
        );
        assert!(evaluate(&pol, "x", &facts).is_none());
    }

    #[test]
    fn multi_statement_predicate_fires_on_semicolons() {
        let facts = super::super::sql::analyze("SELECT 1; SELECT 2", None);
        let pol = policy("sql", vec![rule_deny("multi_statement", None)]);
        assert!(evaluate(&pol, "x", &facts).is_some());
    }

    #[test]
    fn subquery_depth_gt_threshold() {
        let facts = super::super::sql::analyze(
            "SELECT * FROM (SELECT id FROM (SELECT id FROM t) AS a) AS b",
            None,
        );
        let pol = policy(
            "sql",
            vec![rule_deny("subquery_depth_gt", Some(vec![json!(1)]))],
        );
        assert!(evaluate(&pol, "x", &facts).is_some());
    }

    // ----- URL ------------------------------------------------------------

    #[test]
    fn host_in_private_range_denies_metadata_ip() {
        let facts = super::super::url_analyzer::analyze("http://169.254.169.254/");
        let pol = policy("url", vec![rule_deny("host_in_private_range", None)]);
        assert!(evaluate(&pol, "fetch", &facts).is_some());
    }

    #[test]
    fn host_in_private_range_allows_public_https() {
        let facts = super::super::url_analyzer::analyze("https://example.com");
        let pol = policy("url", vec![rule_deny("host_in_private_range", None)]);
        assert!(evaluate(&pol, "fetch", &facts).is_none());
    }

    #[test]
    fn scheme_not_in_denies_http() {
        let facts = super::super::url_analyzer::analyze("http://example.com");
        let pol = policy(
            "url",
            vec![rule_deny("scheme_not_in", Some(vec![json!("https")]))],
        );
        assert!(evaluate(&pol, "fetch", &facts).is_some());
    }

    // ----- Shell ----------------------------------------------------------

    #[test]
    fn allow_only_if_command_in_denies_unlisted() {
        let facts = super::super::shell::analyze("curl https://evil.example.com");
        let pol = policy(
            "shell",
            vec![rule_allow_only(
                "command_in",
                Some(vec![json!("ls"), json!("cat"), json!("git")]),
            )],
        );
        assert!(evaluate(&pol, "exec", &facts).is_some());
    }

    #[test]
    fn allow_only_if_command_in_allows_listed() {
        let facts = super::super::shell::analyze("git status");
        let pol = policy(
            "shell",
            vec![rule_allow_only(
                "command_in",
                Some(vec![json!("ls"), json!("git")]),
            )],
        );
        assert!(evaluate(&pol, "exec", &facts).is_none());
    }

    #[test]
    fn flag_present_denies_destructive_flag() {
        let facts = super::super::shell::analyze("rm -rf /tmp/x");
        let pol = policy(
            "shell",
            vec![rule_deny(
                "flag_present",
                Some(vec![json!("-rf"), json!("--force")]),
            )],
        );
        assert!(evaluate(&pol, "exec", &facts).is_some());
    }

    // ----- Path -----------------------------------------------------------

    #[test]
    fn traverses_parent_denies_dotdot() {
        let facts = super::super::path_analyzer::analyze("../../etc/passwd", None);
        let pol = policy("path", vec![rule_deny("traverses_parent", None)]);
        assert!(evaluate(&pol, "read", &facts).is_some());
    }

    #[test]
    fn inside_root_allows_within_allowlist() {
        let facts = super::super::path_analyzer::analyze("/data/foo.txt", None);
        let pol = policy(
            "path",
            vec![rule_allow_only("inside_root", Some(vec![json!("/data")]))],
        );
        assert!(evaluate(&pol, "read", &facts).is_none());
    }

    #[test]
    fn inside_root_denies_outside_allowlist() {
        let facts = super::super::path_analyzer::analyze("/etc/passwd", None);
        let pol = policy(
            "path",
            vec![rule_allow_only("inside_root", Some(vec![json!("/data")]))],
        );
        assert!(evaluate(&pol, "read", &facts).is_some());
    }

    // ----- Policy gating --------------------------------------------------

    #[test]
    fn disabled_policy_never_fires() {
        let facts = super::super::sql::analyze("DELETE FROM x WHERE id = 1", None);
        let mut pol = policy("sql", vec![rule_deny("writes", None)]);
        pol.enabled = false;
        assert!(evaluate(&pol, "x", &facts).is_none());
    }

    #[test]
    fn target_mismatch_never_fires() {
        let facts = super::super::sql::analyze("DELETE FROM x WHERE id = 1", None);
        let mut pol = policy("sql", vec![rule_deny("writes", None)]);
        pol.target = "specific_tool".into();
        assert!(evaluate(&pol, "different_tool", &facts).is_none());
    }

    #[test]
    fn target_wildcard_matches_anything() {
        let facts = super::super::sql::analyze("DELETE FROM x WHERE id = 1", None);
        let pol = policy("sql", vec![rule_deny("writes", None)]);
        assert!(evaluate(&pol, "any_tool", &facts).is_some());
    }

    /// A predicate this build cannot evaluate holds (fail closed).
    /// This test used to pin the opposite — that it never fires.
    #[test]
    fn unknown_predicate_fails_closed() {
        let facts = super::super::sql::analyze("SELECT 1", None);
        let pol = policy("sql", vec![rule_deny("totally_made_up_predicate", None)]);
        let v = evaluate(&pol, "x", &facts).expect("an unknown predicate must hold");
        assert_eq!(v.rule, "totally_made_up_predicate");
    }

    /// Input the analyzer could not parse cannot pass a policy over it.
    #[test]
    fn unparseable_input_trips_every_predicate_of_its_analyzer() {
        // SQL: garbage cannot be shown not to write.
        let sql = super::super::sql::analyze("DELETE FROM WHERE ((", None);
        assert!(sql.sql_parse_error);
        let pol = policy("sql", vec![rule_deny("writes", None)]);
        assert!(
            evaluate(&pol, "x", &sql).is_some(),
            "unparseable SQL must not pass `writes`"
        );

        // Shell: an allow-list must not read "no command" as "allowed".
        let sh = super::super::shell::analyze(r#"rm -rf / "unterminated"#);
        assert!(sh.shell_parse_error);
        let pol = policy(
            "shell",
            vec![rule_deny("command_not_in", Some(vec![json!("ls")]))],
        );
        assert!(
            evaluate(&pol, "x", &sh).is_some(),
            "unsplittable shell must not pass an allow-list"
        );

        // URL: dropping the scheme must not pass a host allow-list.
        let url = super::super::url_analyzer::analyze("evil.example/x");
        assert!(url.url_parse_error);
        let pol = policy(
            "url",
            vec![rule_deny("host_not_in", Some(vec![json!("good.example")]))],
        );
        assert!(
            evaluate(&pol, "x", &url).is_some(),
            "a scheme-less URL must not pass a host allow-list"
        );

        // And well-formed input still evaluates normally.
        let ok = super::super::url_analyzer::analyze("https://good.example/x");
        assert!(!ok.url_parse_error);
        assert!(evaluate(&pol, "x", &ok).is_none());
    }

    #[test]
    fn first_rule_violation_returned() {
        let facts = super::super::sql::analyze("DELETE FROM users WHERE id = 1", None);
        let pol = policy(
            "sql",
            vec![
                rule_deny("writes", None),
                rule_deny("touches_any", Some(vec![json!("users")])),
            ],
        );
        let v = evaluate(&pol, "x", &facts).unwrap();
        assert_eq!(v.rule, "writes");
    }

    #[test]
    fn severity_carried_through_to_violation() {
        let facts = super::super::sql::analyze("DELETE FROM x WHERE id = 1", None);
        let mut pol = policy("sql", vec![rule_deny("writes", None)]);
        pol.severity = Severity::Warn;
        let v = evaluate(&pol, "x", &facts).unwrap();
        assert!(matches!(v.severity, Severity::Warn));
    }

    // ===== Banded comparison predicates =================

    #[test]
    fn flattening_records_numbers_by_dotted_path() {
        let f = json_facts(json!({"amount": 8000, "transfer": {"amount": 25}}));
        assert_eq!(f.json_numbers.get("amount"), Some(&8000.0));
        assert_eq!(f.json_numbers.get("transfer.amount"), Some(&25.0));
        assert!(f.json_leaf_paths.contains(&"transfer.amount".to_string()));
    }

    #[test]
    fn flattening_indexes_array_elements() {
        let f = json_facts(json!({"items": [{"qty": 3}, {"qty": 9}]}));
        assert_eq!(f.json_numbers.get("items.0.qty"), Some(&3.0));
        assert_eq!(f.json_numbers.get("items.1.qty"), Some(&9.0));
    }

    #[test]
    fn a_quoted_number_is_coerced_and_flagged() {
        // LLM tool calls routinely emit `{"amount": "8000.00"}`; a policy
        // that silently stops applying because the model quoted the number
        // is worse than one that compares a coerced value.
        let f = json_facts(json!({"amount": "8000.00"}));
        assert_eq!(f.json_numbers.get("amount"), Some(&8000.0));
        assert!(f.json_coerced_paths.contains("amount"));
        assert_eq!(
            f.json_strings.get("amount").map(String::as_str),
            Some("8000.00")
        );
    }

    #[test]
    fn strict_numbers_refuses_the_coercion() {
        let f = json_facts_cfg(json!({"amount": "8000"}), json!({"strict_numbers": true}));
        assert!(
            f.json_numbers.is_empty(),
            "a quoted number must not compare"
        );
        assert!(f.json_coerced_paths.is_empty());
    }

    #[test]
    fn non_finite_values_never_enter_the_number_map() {
        // NaN and infinities compare false against every threshold, which
        // would silently disable a band.
        let f = json_facts(json!({"a": "NaN", "b": "inf", "c": "-Infinity"}));
        assert!(f.json_numbers.is_empty(), "got {:?}", f.json_numbers);
    }

    #[test]
    fn depth_cap_sets_truncated() {
        let mut v = json!(1);
        for _ in 0..12 {
            v = json!({ "n": v });
        }
        let f = json_facts(v);
        assert!(
            f.json_truncated,
            "a deeper-than-cap object must flag truncation"
        );
    }

    #[test]
    fn array_width_cap_sets_truncated() {
        let wide: Vec<Value> = (0..200).map(|i| json!(i)).collect();
        let f = json_facts(json!({ "xs": wide }));
        assert!(f.json_truncated);
        assert!(f.json_numbers.contains_key("xs.0"));
        assert!(
            !f.json_numbers.contains_key("xs.100"),
            "capped at 64 per level"
        );
    }

    #[test]
    fn leaf_count_cap_sets_truncated() {
        let mut map = serde_json::Map::new();
        for i in 0..900 {
            map.insert(format!("k{i}"), json!(i));
        }
        let f = json_facts(Value::Object(map));
        assert!(f.json_truncated);
    }

    #[test]
    fn every_ordering_predicate_compares_the_right_way() {
        let facts = json_facts(json!({"amount": 1000}));
        for (predicate, expect) in [
            ("field_gt", false),
            ("field_gte", true),
            ("field_lt", false),
            ("field_lte", true),
        ] {
            let r = rule_cmp(predicate, "amount", json!(1000));
            assert_eq!(
                check_rule(&r, &facts).is_some(),
                expect,
                "{predicate} at the boundary"
            );
        }
    }

    #[test]
    fn equality_works_for_numbers_and_strings() {
        let facts = json_facts(json!({"currency": "GBP", "amount": 10}));
        assert!(check_rule(&rule_cmp("field_eq", "currency", json!("GBP")), &facts).is_some());
        assert!(check_rule(&rule_cmp("field_eq", "currency", json!("USD")), &facts).is_none());
        assert!(check_rule(&rule_cmp("field_eq", "amount", json!(10)), &facts).is_some());
        assert!(check_rule(&rule_cmp("field_ne", "currency", json!("USD")), &facts).is_some());
    }

    #[test]
    fn field_ne_does_not_fire_on_a_missing_field() {
        // Absent is not "not equal" — otherwise `deny_if: field_ne` would
        // block every call that simply omits the field.
        let facts = json_facts(json!({}));
        assert!(check_rule(&rule_cmp("field_ne", "currency", json!("GBP")), &facts).is_none());
    }

    #[test]
    fn membership_reads_the_values_list() {
        let facts = json_facts(json!({"currency": "XXX"}));
        let mut r = rule_cmp("field_in", "currency", json!(null));
        r.value = None;
        r.values = Some(vec![json!("GBP"), json!("EUR")]);
        assert!(check_rule(&r, &facts).is_none(), "XXX is not in the list");
        let mut r2 = r.clone();
        r2.deny_if = Some("field_not_in".into());
        assert!(check_rule(&r2, &facts).is_some(), "XXX must trip not_in");
    }

    #[test]
    fn a_comparison_rule_without_a_field_or_value_never_fires() {
        let facts = json_facts(json!({"amount": 9999}));
        let mut r = rule_cmp("field_gt", "amount", json!(1));
        r.field = None;
        assert!(check_rule(&r, &facts).is_none());
        let mut r2 = rule_cmp("field_gt", "amount", json!(1));
        r2.value = None;
        assert!(check_rule(&r2, &facts).is_none());
    }

    #[test]
    fn nested_and_indexed_paths_compare() {
        let facts = json_facts(json!({"transfer": {"amount": 5000}, "items": [{"qty": 99}]}));
        assert!(
            check_rule(
                &rule_cmp("field_gt", "transfer.amount", json!(1000)),
                &facts
            )
            .is_some()
        );
        assert!(check_rule(&rule_cmp("field_gt", "items.0.qty", json!(50)), &facts).is_some());
    }

    #[test]
    fn field_present_still_answers_top_level_keys_unchanged() {
        let facts = json_facts(json!({"amount": 1}));
        let r = rule_allow_only("field_present", Some(vec![json!("amount")]));
        assert!(check_rule(&r, &facts).is_none());
        let missing = rule_allow_only("field_present", Some(vec![json!("nope")]));
        assert!(check_rule(&missing, &facts).is_some());
    }

    #[test]
    fn field_present_answers_a_nested_path() {
        let facts = json_facts(json!({"transfer": {"amount": 1}}));
        let r = rule_allow_only("field_present", Some(vec![json!("transfer.amount")]));
        assert!(check_rule(&r, &facts).is_none());
    }

    // ---- The bypass, documented as a test rather than as a comment ------

    #[test]
    fn omitting_the_compared_field_walks_past_a_bare_threshold() {
        // THIS IS THE BYPASS. A comparison predicate reading a missing field
        // does not hold, so a policy carrying ONLY a threshold lets
        // `transfer_funds({})` straight through. It is asserted here so the
        // behaviour is impossible to forget, and it is closed one level up:
        // the control-plane validator refuses to save such a policy
        // (`validate_comparison_rules_have_a_presence_guard`).
        let p = policy("json", vec![rule_cmp("field_gt", "amount", json!(1000))]);
        let facts = json_facts(json!({}));
        assert!(
            evaluate(&p, "transfer_funds", &facts).is_none(),
            "a bare threshold cannot see an omitted field — this is why the \
             validator refuses to save one without a presence guard"
        );
    }

    #[test]
    fn a_presence_guard_closes_the_bypass() {
        let p = policy(
            "json",
            vec![
                rule_allow_only("field_present", Some(vec![json!("amount")])),
                rule_cmp("field_gt", "amount", json!(1000)),
            ],
        );
        let facts = json_facts(json!({}));
        let v = evaluate(&p, "transfer_funds", &facts).expect("presence rule must deny");
        assert_eq!(v.rule, "field_present");
    }

    #[test]
    fn a_schema_required_guard_closes_it_too() {
        let mut p = policy(
            "json",
            vec![
                rule_deny("schema_invalid", None),
                rule_cmp("field_gt", "amount", json!(1000)),
            ],
        );
        p.analyzer_config = Some(json!({"schema": {"required": ["amount"]}}));
        let facts = json_facts_cfg(json!({}), json!({"schema": {"required": ["amount"]}}));
        let v = evaluate(&p, "transfer_funds", &facts).expect("schema rule must deny");
        assert_eq!(v.rule, "schema_invalid");
    }

    // ---- Ordering is the band -------------------------------------------

    #[test]
    fn rule_order_expresses_the_band() {
        // deny over 10000, approval over 1000, allow below.
        let mut deny_high = rule_cmp("field_gt", "amount", json!(10000));
        deny_high.severity = Some(Severity::Deny);
        let mut approve_mid = rule_cmp("field_gt", "amount", json!(1000));
        approve_mid.severity = Some(Severity::ApprovalRequired);
        let p = policy(
            "json",
            vec![
                rule_allow_only("field_present", Some(vec![json!("amount")])),
                deny_high,
                approve_mid,
            ],
        );

        let low = evaluate(&p, "transfer_funds", &json_facts(json!({"amount": 500})));
        assert!(low.is_none(), "below the band is allowed");

        let mid = evaluate(&p, "transfer_funds", &json_facts(json!({"amount": 8000})))
            .expect("mid band must trip");
        assert!(matches!(mid.severity, Severity::ApprovalRequired));

        let high = evaluate(&p, "transfer_funds", &json_facts(json!({"amount": 50000})))
            .expect("top band must trip");
        assert!(matches!(high.severity, Severity::Deny));
    }

    #[test]
    fn a_looser_band_placed_first_shadows_the_tighter_one() {
        // The reason the form must warn about ordering: with the approval
        // rule first, the deny rule is unreachable and a 50,000 transfer
        // only ever asks for approval.
        let mut approve_mid = rule_cmp("field_gt", "amount", json!(1000));
        approve_mid.severity = Some(Severity::ApprovalRequired);
        let mut deny_high = rule_cmp("field_gt", "amount", json!(10000));
        deny_high.severity = Some(Severity::Deny);
        let p = policy("json", vec![approve_mid, deny_high]);
        let v = evaluate(&p, "transfer_funds", &json_facts(json!({"amount": 50000})))
            .expect("something must trip");
        assert!(
            matches!(v.severity, Severity::ApprovalRequired),
            "the shadowed deny never runs — this is what the UI warning is for"
        );
    }

    #[test]
    fn a_rule_without_its_own_severity_inherits_the_policys() {
        let p = policy(
            "json",
            vec![
                rule_allow_only("field_present", Some(vec![json!("amount")])),
                rule_cmp("field_gt", "amount", json!(1000)),
            ],
        );
        let v = evaluate(&p, "transfer_funds", &json_facts(json!({"amount": 9000})))
            .expect("must trip");
        assert!(
            matches!(v.severity, Severity::Deny),
            "policy default is Deny"
        );
    }

    #[test]
    fn severity_ranks_strictest_last() {
        assert!(Severity::Deny.rank() > Severity::ApprovalRequired.rank());
        assert!(Severity::ApprovalRequired.rank() > Severity::Warn.rank());
    }

    #[test]
    fn severity_round_trips_over_the_wire_as_snake_case() {
        // The control plane stores `approval_required`; `deny` and `warn`
        // are unchanged, so existing rows deserialize exactly as before.
        assert_eq!(
            serde_json::to_string(&Severity::ApprovalRequired).unwrap(),
            "\"approval_required\""
        );
        assert_eq!(serde_json::to_string(&Severity::Deny).unwrap(), "\"deny\"");
        let parsed: Severity = serde_json::from_str("\"approval_required\"").unwrap();
        assert!(matches!(parsed, Severity::ApprovalRequired));
    }

    // ===== Undecidable input, analysis caps, numbers as text ================

    /// `allow_only_if <predicate> <field> <value>`.
    fn rule_cmp_allow(predicate: &str, field: &str, value: Value) -> Rule {
        let mut r = rule_cmp(predicate, field, value);
        r.allow_only_if = r.deny_if.take();
        r
    }

    /// The shipped shape of a threshold: presence guard, then the band.
    fn guarded_threshold() -> ArgumentPolicy {
        policy(
            "json",
            vec![
                rule_allow_only("field_present", Some(vec![json!("amount")])),
                rule_cmp("field_gt", "amount", json!(10000)),
            ],
        )
    }

    /// 512 padding leaves sort before `amount`, the
    /// flattener stops at its cap and never records `amount` as a number,
    /// yet `amount` is still a top-level key, so the presence guard passes
    /// and the threshold compares nothing.
    #[test]
    fn padding_past_the_leaf_cap_is_refused_not_allowed() {
        let mut pad = serde_json::Map::new();
        for i in 0..512 {
            pad.insert(format!("k{i:03}"), json!(0));
        }
        let facts = json_facts(json!({"_": pad, "amount": 20000}));
        assert!(facts.json_truncated, "the padding must hit the leaf cap");
        assert!(
            !facts.json_numbers.contains_key("amount"),
            "the premise: the compared field was never reached"
        );

        let v = evaluate(&guarded_threshold(), "transfer_funds", &facts)
            .expect("a truncated analysis must be refused");
        assert_eq!(v.rule, "arguments_truncated");
        assert!(matches!(v.severity, Severity::Deny));

        // A denial whatever the policy's own severity: nothing was compared,
        // so no rule's severity describes what happened.
        let mut warn_only = guarded_threshold();
        warn_only.severity = Severity::Warn;
        let v = evaluate(&warn_only, "transfer_funds", &facts).expect("still refused");
        assert!(matches!(v.severity, Severity::Deny));

        // The depth and array-width caps are the same condition.
        let mut deep = json!(20000);
        for _ in 0..12 {
            deep = json!({ "n": deep });
        }
        let facts = json_facts(json!({"amount": 1, "pad": deep}));
        assert!(evaluate(&guarded_threshold(), "transfer_funds", &facts).is_some());

        // A policy that does not apply to the call still does not fire.
        let mut other_tool = guarded_threshold();
        other_tool.target = "another_tool".into();
        let facts = json_facts(json!({"xs": (0..200).collect::<Vec<i32>>(), "amount": 1}));
        assert!(facts.json_truncated);
        assert!(evaluate(&other_tool, "transfer_funds", &facts).is_none());
        let mut disabled = guarded_threshold();
        disabled.enabled = false;
        assert!(evaluate(&disabled, "transfer_funds", &facts).is_none());
    }

    /// A compared field that is PRESENT but is not a
    /// number satisfies the presence guard and never enters the number map,
    /// so the threshold used to compare nothing and the call passed.
    #[test]
    fn a_present_field_that_is_not_a_number_trips_every_ordering_predicate() {
        let not_numbers = [
            json!(null),
            json!(true),
            json!([20000]),
            json!({"v": 20000}),
            json!("20,000"),
            json!([]),
            json!({}),
            json!("NaN"),
        ];
        for value in &not_numbers {
            let top = json_facts(json!({ "amount": value }));
            let nested = json_facts(json!({ "transfer": { "amount": value } }));
            for predicate in ["field_gt", "field_gte", "field_lt", "field_lte"] {
                for (facts, path) in [(&top, "amount"), (&nested, "transfer.amount")] {
                    assert!(
                        check_rule(&rule_cmp(predicate, path, json!(10000)), facts).is_some(),
                        "deny_if {predicate} {path} must trip on {value}"
                    );
                    assert!(
                        check_rule(&rule_cmp_allow(predicate, path, json!(10000)), facts).is_some(),
                        "allow_only_if {predicate} {path} must trip on {value}"
                    );
                }
            }
            // Through the shipped shape: guard satisfied, threshold refuses.
            let v = evaluate(&guarded_threshold(), "transfer_funds", &top)
                .unwrap_or_else(|| panic!("{value} must be refused"));
            assert_eq!(v.rule, "field_gt", "{value}");
            assert!(
                v.message.contains("amount"),
                "the refusal says which field could not be compared: {}",
                v.message
            );
        }
    }

    /// An ABSENT field keeps its semantics: `deny_if` does not fire (the
    /// presence guard is what refuses it), `allow_only_if` does.
    #[test]
    fn an_absent_field_keeps_its_semantics_under_every_comparison() {
        let facts = json_facts(json!({"other": 1}));
        for predicate in ["field_gt", "field_gte", "field_lt", "field_lte"] {
            assert!(check_rule(&rule_cmp(predicate, "amount", json!(1)), &facts).is_none());
            assert!(check_rule(&rule_cmp_allow(predicate, "amount", json!(1)), &facts).is_some());
        }
        for predicate in ["field_eq", "field_ne"] {
            assert!(check_rule(&rule_cmp(predicate, "amount", json!(1)), &facts).is_none());
            assert!(check_rule(&rule_cmp_allow(predicate, "amount", json!(1)), &facts).is_some());
        }
        for predicate in ["field_in", "field_not_in"] {
            let mut deny = rule_cmp(predicate, "amount", json!(null));
            deny.value = None;
            deny.values = Some(vec![json!(1)]);
            assert!(check_rule(&deny, &facts).is_none(), "{predicate}");
            let mut allow = deny.clone();
            allow.allow_only_if = allow.deny_if.take();
            assert!(check_rule(&allow, &facts).is_some(), "{predicate}");
        }
    }

    /// `strict_numbers` promised to refuse a quoted
    /// number and only stopped comparing it.
    #[test]
    fn a_quoted_number_is_compared_by_default_and_refused_under_strict_numbers() {
        // Default: coerced and compared like the number it spells.
        let coerced = json_facts(json!({"amount": "20000"}));
        assert!(check_rule(&rule_cmp("field_gt", "amount", json!(10000)), &coerced).is_some());
        assert!(check_rule(&rule_cmp("field_gt", "amount", json!(30000)), &coerced).is_none());
        assert!(
            check_rule(
                &rule_cmp_allow("field_lt", "amount", json!(30000)),
                &coerced
            )
            .is_none()
        );

        // Strict: present, and not a number this policy will compare.
        let strict = json_facts_cfg(json!({"amount": "20000"}), json!({"strict_numbers": true}));
        for predicate in ["field_gt", "field_gte", "field_lt", "field_lte"] {
            for threshold in [json!(10000), json!(30000)] {
                assert!(
                    check_rule(&rule_cmp(predicate, "amount", threshold.clone()), &strict)
                        .is_some(),
                    "deny_if {predicate} {threshold} under strict_numbers"
                );
                assert!(
                    check_rule(&rule_cmp_allow(predicate, "amount", threshold), &strict).is_some(),
                    "allow_only_if {predicate} under strict_numbers"
                );
            }
        }
        // Equality against a number is the same comparison.
        assert!(check_rule(&rule_cmp("field_eq", "amount", json!(5)), &strict).is_some());
        assert!(check_rule(&rule_cmp("field_ne", "amount", json!(20000)), &strict).is_some());
        // A real number still compares under strict_numbers.
        let real = json_facts_cfg(json!({"amount": 500}), json!({"strict_numbers": true}));
        assert!(check_rule(&rule_cmp("field_gt", "amount", json!(10000)), &real).is_none());
    }

    /// The equality and membership predicates had the same hole: a
    /// present field of a type the operand cannot be compared with read as
    /// "does not hold", so `deny_if field_ne currency GBP` — "only GBP" —
    /// passed `{"currency": ["USD"]}`, and `deny_if field_in` passed a
    /// reshaped value.
    #[test]
    fn a_present_field_of_an_incomparable_type_trips_equality_and_membership() {
        let reshaped = [
            json!(null),
            json!(true),
            json!(["USD"]),
            json!({"code": "USD"}),
            json!(840),
        ];
        for value in &reshaped {
            let facts = json_facts(json!({ "currency": value }));
            for predicate in ["field_eq", "field_ne"] {
                assert!(
                    check_rule(&rule_cmp(predicate, "currency", json!("GBP")), &facts).is_some(),
                    "deny_if {predicate} must trip on {value}"
                );
                assert!(
                    check_rule(&rule_cmp_allow(predicate, "currency", json!("GBP")), &facts)
                        .is_some(),
                    "allow_only_if {predicate} must trip on {value}"
                );
            }
        }
        // Membership compares strings with strings and numbers with numbers;
        // anything else that is present cannot be judged.
        for value in [json!(null), json!(true), json!(["USD"]), json!({"c": 1})] {
            let facts = json_facts(json!({ "currency": value }));
            for predicate in ["field_in", "field_not_in"] {
                let mut deny = rule_cmp(predicate, "currency", json!(null));
                deny.value = None;
                deny.values = Some(vec![json!("GBP"), json!("EUR")]);
                assert!(
                    check_rule(&deny, &facts).is_some(),
                    "deny_if {predicate} on {value}"
                );
                let mut allow = deny.clone();
                allow.allow_only_if = allow.deny_if.take();
                assert!(
                    check_rule(&allow, &facts).is_some(),
                    "allow_only_if {predicate} on {value}"
                );
            }
        }
        // Comparable values still decide normally.
        let gbp = json_facts(json!({"currency": "GBP"}));
        assert!(check_rule(&rule_cmp("field_ne", "currency", json!("GBP")), &gbp).is_none());
        assert!(check_rule(&rule_cmp_allow("field_eq", "currency", json!("GBP")), &gbp).is_none());
    }

    /// Unparsable input made every predicate "hold",
    /// which `allow_only_if` inverted into "allowed". An allow-list must not
    /// be passed by input the analyzer could not read.
    #[test]
    fn unparseable_input_is_refused_under_allow_only_if_too() {
        let url = super::super::url_analyzer::analyze("evil.example/x");
        assert!(url.url_parse_error);
        let pol = policy(
            "url",
            vec![rule_allow_only(
                "host_in",
                Some(vec![json!("good.example")]),
            )],
        );
        let v = evaluate(&pol, "fetch", &url)
            .expect("a scheme-less URL must not pass an allow_only_if host allow-list");
        assert_eq!(v.rule, "host_in");

        let sh = super::super::shell::analyze(r#"rm -rf / "unterminated"#);
        assert!(sh.shell_parse_error);
        let pol = policy(
            "shell",
            vec![rule_allow_only("command_in", Some(vec![json!("ls")]))],
        );
        assert!(evaluate(&pol, "exec", &sh).is_some());

        let sql = super::super::sql::analyze("DELETE FROM WHERE ((", None);
        assert!(sql.sql_parse_error);
        let pol = policy(
            "sql",
            vec![rule_allow_only(
                "statement_type_in",
                Some(vec![json!("SELECT")]),
            )],
        );
        assert!(evaluate(&pol, "query", &sql).is_some());

        // Well-formed input on the allow-list still passes.
        let ok = super::super::url_analyzer::analyze("https://good.example/x");
        let pol = policy(
            "url",
            vec![rule_allow_only(
                "host_in",
                Some(vec![json!("good.example")]),
            )],
        );
        assert!(evaluate(&pol, "fetch", &ok).is_none());
    }

    /// Same inversion: a predicate this build does not know "held",
    /// so `allow_only_if <unknown>` allowed everything.
    #[test]
    fn an_unknown_predicate_is_refused_under_allow_only_if_too() {
        let facts = super::super::sql::analyze("SELECT 1", None);
        let pol = policy(
            "sql",
            vec![rule_allow_only("totally_made_up_predicate", None)],
        );
        assert!(evaluate(&pol, "x", &facts).is_some());
    }

    /// The control plane accepts a boolean operand for the
    /// equality and membership predicates (`deny_if field_eq confirm false`),
    /// and the gateway compared numbers and strings only. Such a rule never
    /// fired; read as undecidable it would fire on EVERY present value, `true`
    /// and `false` alike. A boolean is compared with a boolean.
    #[test]
    fn a_boolean_field_is_compared_with_a_boolean_operand() {
        let yes = json_facts(json!({"confirm": true, "nested": {"dry_run": true}}));
        let no = json_facts(json!({"confirm": false, "nested": {"dry_run": false}}));

        // deny_if field_eq confirm false: fires on false, not on true.
        let deny_unconfirmed = rule_cmp("field_eq", "confirm", json!(false));
        assert!(check_rule(&deny_unconfirmed, &no).is_some());
        assert!(check_rule(&deny_unconfirmed, &yes).is_none());
        // allow_only_if field_eq confirm true: passes true, refuses false.
        let only_confirmed = rule_cmp_allow("field_eq", "confirm", json!(true));
        assert!(check_rule(&only_confirmed, &yes).is_none());
        assert!(check_rule(&only_confirmed, &no).is_some());
        // field_ne, nested path.
        let deny_real_runs = rule_cmp("field_ne", "nested.dry_run", json!(true));
        assert!(check_rule(&deny_real_runs, &no).is_some());
        assert!(check_rule(&deny_real_runs, &yes).is_none());

        // Membership with a boolean among the options.
        let mut in_false = rule_cmp("field_in", "confirm", json!(null));
        in_false.value = None;
        in_false.values = Some(vec![json!(false)]);
        assert!(check_rule(&in_false, &no).is_some());
        assert!(check_rule(&in_false, &yes).is_none());
        let mut not_in_true = in_false.clone();
        not_in_true.deny_if = Some("field_not_in".into());
        not_in_true.values = Some(vec![json!(true)]);
        assert!(check_rule(&not_in_true, &no).is_some());
        assert!(check_rule(&not_in_true, &yes).is_none());

        // A boolean against an operand of another type is still undecidable,
        // and so is another type against a boolean operand.
        assert!(check_rule(&rule_cmp("field_eq", "confirm", json!("true")), &yes).is_some());
        let quoted = json_facts(json!({"confirm": "false"}));
        assert!(check_rule(&deny_unconfirmed, &quoted).is_some());
        assert!(check_rule(&only_confirmed, &quoted).is_some());
        // Absent keeps its semantics.
        let absent = json_facts(json!({"other": 1}));
        assert!(check_rule(&deny_unconfirmed, &absent).is_none());
        assert!(check_rule(&only_confirmed, &absent).is_some());
    }

    /// The analysis caps, pinned with their numbers: they
    /// are constants, not settings, and exceeding one REFUSES
    /// the call whenever a JSON policy targets the tool. A list of 64
    /// elements is analyzed; one of 65 (or 100) is refused. 512 leaves are
    /// analyzed; 513 are refused. Eight levels of nesting are analyzed; a
    /// ninth is refused.
    #[test]
    fn the_analysis_caps_are_64_elements_512_leaves_and_depth_8() {
        let pol = guarded_threshold();
        let refused = |args: Value| {
            evaluate(&pol, "transfer_funds", &json_facts(args)).map(|v| v.rule)
                == Some(RULE_ARGUMENTS_TRUNCATED.to_string())
        };
        let list = |n: usize| json!({"amount": 1, "ids": (0..n).collect::<Vec<usize>>()});
        assert!(!refused(list(64)));
        assert!(refused(list(65)));
        assert!(
            refused(list(100)),
            "a legitimate 100-element list is refused"
        );

        let leaves = |n: usize| {
            let mut m = serde_json::Map::new();
            m.insert("amount".into(), json!(1));
            for i in 1..n {
                m.insert(format!("k{i:04}"), json!(0));
            }
            Value::Object(m)
        };
        assert!(!refused(leaves(512)));
        assert!(refused(leaves(513)));

        // `amount` at nesting level n (1 = top-level key).
        let nested = |levels: usize| {
            let mut v = json!(1);
            for _ in 1..levels {
                v = json!({ "n": v });
            }
            json!({ "amount": 1, "deep": v })
        };
        assert!(!refused(nested(8)));
        assert!(refused(nested(9)));
    }

    #[test]
    fn an_old_rule_without_the_new_fields_still_parses() {
        let r: Rule = serde_json::from_str(r#"{"deny_if":"writes","message":"no"}"#).unwrap();
        assert!(r.field.is_none() && r.value.is_none() && r.severity.is_none());
    }
}

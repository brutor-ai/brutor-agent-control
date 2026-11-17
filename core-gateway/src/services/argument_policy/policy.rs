// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Argument policy schema (mirrors the control plane's Pydantic shape).

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Surface {
    LlmOutputToolCalls,
    McpInput,
    SkillInput,
}

/// How hard a tripped rule bites.
///
/// `ApprovalRequired` is the band between the two: the call is not refused,
/// it is held for a human through the existing approval queue. Without it a
/// threshold policy has to be a hard block, which kills the agent above the
/// threshold rather than supervising it.
///
/// `#[serde(rename_all = "snake_case")]` so the wire value is
/// `approval_required`, matching the control plane's
/// `ALLOWED_SEVERITIES`. `deny` and `warn` are unchanged under snake_case,
/// so existing stored policies deserialize exactly as before.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    Deny,
    ApprovalRequired,
    Warn,
}

impl Severity {
    /// Strictness rank — higher is stricter. `Deny` outranks
    /// `ApprovalRequired` outranks `Warn`, matching
    /// `agent_authz::Decision::rank` so the two planes merge consistently.
    pub fn rank(self) -> u8 {
        match self {
            Severity::Warn => 1,
            Severity::ApprovalRequired => 2,
            Severity::Deny => 3,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArgumentPolicy {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub surface: Surface,
    /// Tool / skill name. `"*"` matches any.
    pub target: String,
    pub argument_key: String,
    /// One of "sql" / "url" / "shell" / "path" / "json".
    pub analyzer: String,
    #[serde(default)]
    pub analyzer_config: Option<Value>,
    #[serde(default)]
    pub rules: Vec<Rule>,
    #[serde(default = "default_true")]
    pub enabled: bool,
    #[serde(default = "default_deny")]
    pub severity: Severity,
}

fn default_true() -> bool {
    true
}

fn default_deny() -> Severity {
    Severity::Deny
}

/// One predicate. Exactly one of `deny_if` / `allow_only_if` is
/// set per the control plane's Pydantic validator.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Rule {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deny_if: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub allow_only_if: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub values: Option<Vec<Value>>,
    /// Dotted path into the flattened JSON argument, e.g. `amount` or
    /// `transfer.amount` or `items.0.qty`. Read by the comparison
    /// predicates; ignored by every other predicate.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    /// The operand a comparison predicate tests `field` against.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value: Option<Value>,
    /// Overrides the policy-level severity for this rule only.
    ///
    /// This is what makes a *band* expressible in one policy: rules are
    /// evaluated in order and the first match wins, so
    /// `> 10000 → deny` above `> 1000 → approval_required` reads as
    /// "deny over ten thousand, hold for a human over one thousand,
    /// allow below". Ordering is the band, which is why the form must keep
    /// it explicit and warn when a looser rule shadows a tighter one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub severity: Option<Severity>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

impl ArgumentPolicy {
    /// Whether this policy applies to a given tool/skill name.
    pub fn matches_target(&self, name: &str) -> bool {
        self.target == "*" || self.target == name
    }

    pub fn kind(&self) -> Option<super::ArgKind> {
        match self.analyzer.as_str() {
            "sql" => Some(super::ArgKind::Sql),
            "url" => Some(super::ArgKind::Url),
            "shell" => Some(super::ArgKind::Shell),
            "path" => Some(super::ArgKind::Path),
            "json" => Some(super::ArgKind::Json),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(target: &str) -> ArgumentPolicy {
        ArgumentPolicy {
            id: "id".into(),
            tenant_id: "t".into(),
            name: "n".into(),
            surface: Surface::LlmOutputToolCalls,
            target: target.into(),
            argument_key: "query".into(),
            analyzer: "sql".into(),
            analyzer_config: None,
            rules: vec![],
            enabled: true,
            severity: Severity::Deny,
        }
    }

    #[test]
    fn wildcard_matches_any_target() {
        assert!(p("*").matches_target("anything"));
        assert!(p("*").matches_target(""));
    }

    #[test]
    fn exact_target_only_matches_itself() {
        assert!(p("postgres_query").matches_target("postgres_query"));
        assert!(!p("postgres_query").matches_target("other_query"));
    }

    #[test]
    fn analyzer_field_maps_to_arg_kind() {
        let mut pol = p("*");
        pol.analyzer = "url".into();
        assert_eq!(pol.kind(), Some(super::super::ArgKind::Url));
        pol.analyzer = "shell".into();
        assert_eq!(pol.kind(), Some(super::super::ArgKind::Shell));
        pol.analyzer = "unknown".into();
        assert_eq!(pol.kind(), None);
    }
}

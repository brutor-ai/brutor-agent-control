// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Surface entry point — the function each route calls to enforce
//! argument policies. Returns the first violation across all
//! applicable policies for the given (tenant, group, surface,
//! target_tool, arguments).

use serde_json::Value;
use sqlx::PgPool;

use super::cache::ArgumentPolicyCache;
use super::evaluator::{Violation, evaluate};
use super::policy::Surface;

/// The refusal when argument policies exist but cannot be applied
/// (fail closed): a `deny` violation naming no particular rule.
fn unenforceable(target_tool: &str, why: &str) -> Violation {
    Violation {
        policy_id: "argument_policy_unavailable".to_string(),
        policy_name: "argument policy unavailable".to_string(),
        tool: target_tool.to_string(),
        argument_key: "*".to_string(),
        analyzer: String::new(),
        rule: "policy_unavailable".to_string(),
        message: format!("Refused: {why}"),
        severity: super::policy::Severity::Deny,
        field: None,
        observed: None,
        threshold: None,
        facts: Default::default(),
    }
}

/// The arguments a policy judges.
#[derive(Debug, Clone)]
pub enum Arguments<'a> {
    /// One value: a tool call's arguments, a skill's input.
    Whole(&'a Value),
    /// An argument that arrives in parts, judged as one set of fields: the
    /// data parts of an A2A message (`argument_policy::analyze_parts`).
    Parts(Vec<&'a Value>),
}

/// The members of an A2A Message other than its parts. A `message` whose
/// members are all among these and that has a `parts` array is a Message;
/// anything else is an opaque body.
const A2A_MESSAGE_MEMBERS: [&str; 9] = [
    "parts",
    "kind",
    "messageId",
    "role",
    "contextId",
    "taskId",
    "metadata",
    "extensions",
    "referenceTaskIds",
];

impl<'a> Arguments<'a> {
    /// What an argument policy judges of a skill input that arrived as an
    /// A2A message.
    ///
    /// A Message carries structured input in its data parts, and which
    /// part carries a field is the caller's choice. So the policy judges
    /// the data parts' values (`data_scope::a2a_arguments`, the view the
    /// run's data scope judges) as one set of fields: an author addresses
    /// `amount`, on every A2A version, as on every other surface.
    ///
    /// A `message` that is not a Message is an opaque body and is judged
    /// whole. That includes a body that has a `parts` member beside members
    /// a Message does not have: reading it as a Message would hide those
    /// members from the policy while the skill still receives them.
    pub fn of_a2a_message(message: &'a Value) -> Self {
        let is_message = message.as_object().is_some_and(|m| {
            m.get("parts").is_some_and(Value::is_array)
                && m.keys().all(|k| A2A_MESSAGE_MEMBERS.contains(&k.as_str()))
        });
        if is_message {
            Arguments::Parts(crate::services::data_scope::a2a_arguments(message))
        } else {
            Arguments::Whole(message)
        }
    }
}

/// [`enforce_view`] for arguments that are one value.
pub async fn enforce(
    cache: &ArgumentPolicyCache,
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    surface: Surface,
    target_tool: &str,
    arguments: &Value,
) -> Option<Violation> {
    enforce_view(
        cache,
        db,
        tenant_id,
        group_id,
        surface,
        target_tool,
        &Arguments::Whole(arguments),
    )
    .await
}

/// What `policy` analyzes of one value: the whole of it for
/// `argument_key: "*"`, the named member of an object, the value itself
/// when it is not an object. `None` when the object has no such member.
fn target_of<'a>(policy: &super::policy::ArgumentPolicy, value: &'a Value) -> Option<&'a Value> {
    if policy.argument_key == "*" {
        return Some(value);
    }
    match value.as_object() {
        Some(obj) => obj.get(&policy.argument_key),
        None => Some(value),
    }
}

/// Run every argument policy bound to the given resource group on
/// the given (surface, target_tool, arguments) tuple. Returns the
/// first violation, or None if everything passed.
///
/// Arguments are passed as a JSON object — each policy's
/// `argument_key` looks up its target inside that object.
/// Policies that name a missing key are skipped (a missing arg
/// can't violate a rule that asks "is this SQL a write").
///
/// Cheap on the hot path: cache lookup is O(1) once warm, analyzers
/// run only on the args they target, and the evaluator short-circuits
/// at the first match.
pub async fn enforce_view(
    cache: &ArgumentPolicyCache,
    db: &PgPool,
    tenant_id: &str,
    group_id: &str,
    surface: Surface,
    target_tool: &str,
    arguments: &Arguments<'_>,
) -> Option<Violation> {
    let policies = match cache.get_for_group(tenant_id, group_id, surface, db).await {
        Ok(p) if !p.is_empty() => p,
        Ok(_) => return None,
        Err(e) => {
            tracing::error!(
                tenant_id,
                group_id,
                surface = ?surface,
                error = %e,
                "argument-policy cache load failed — refusing (fail closed)"
            );
            return Some(unenforceable(
                target_tool,
                "argument policies could not be loaded, so the call cannot be checked",
            ));
        }
    };

    for policy in &policies {
        // A disabled policy refuses nothing, whatever is wrong with it;
        // `evaluate` skips it too.
        if !policy.enabled || !policy.matches_target(target_tool) {
            continue;
        }
        let kind = match policy.kind() {
            Some(k) => k,
            None => {
                // A policy this build cannot run is not a passed one.
                tracing::error!(
                    policy_id = %policy.id,
                    analyzer = %policy.analyzer,
                    "unknown analyzer in policy — refusing (should have been rejected at admin write)"
                );
                let mut v = unenforceable(
                    target_tool,
                    "an argument policy uses an analyzer this gateway does not support",
                );
                v.policy_id = policy.id.clone();
                v.policy_name = policy.name.clone();
                v.analyzer = policy.analyzer.clone();
                return Some(v);
            }
        };

        // The value to analyze.
        //
        // `argument_key: "*"` means the WHOLE argument object, mirroring the
        // `target: "*"` convention. Banded policies need this: a
        // threshold on `amount` is a field INSIDE the tool's arguments, and
        // the comparison predicates address it by dotted path, so the
        // analyzer has to see the object rather than one extracted member.
        // Naming a single key still works, and still means "analyze that
        // one argument" — which is what the SQL, URL, shell and path
        // analyzers want. A policy on a named key that the arguments do not
        // have is skipped.
        //
        // Arguments in parts are judged as one set of fields: every part's
        // target, merged (`analyze_parts`).
        let targets: Vec<&Value> = match arguments {
            Arguments::Whole(value) => match target_of(policy, value) {
                Some(v) => vec![v],
                None => continue,
            },
            Arguments::Parts(parts) => {
                let targets: Vec<&Value> =
                    parts.iter().filter_map(|p| target_of(policy, p)).collect();
                if targets.is_empty() && policy.argument_key != "*" {
                    continue;
                }
                targets
            }
        };
        // The limits were checked when the policy was loaded. A policy that
        // reached here another way with limits this gateway does not accept
        // is not one it can apply.
        if kind == super::ArgKind::Json {
            if let Err(invalid) =
                super::AnalysisLimits::from_config(policy.analyzer_config.as_ref())
            {
                tracing::error!(
                    policy_id = %policy.id,
                    key = invalid.key,
                    "argument policy states an analysis limit this gateway does not accept — refusing"
                );
                let mut v = unenforceable(
                    target_tool,
                    "an argument policy states an analysis limit this gateway does not accept",
                );
                v.policy_id = policy.id.clone();
                v.policy_name = policy.name.clone();
                v.analyzer = policy.analyzer.clone();
                return Some(v);
            }
        }
        let facts = match arguments {
            Arguments::Whole(_) => {
                super::analyze(targets[0], kind, policy.analyzer_config.as_ref())
            }
            Arguments::Parts(_) => {
                super::analyze_parts(&targets, kind, policy.analyzer_config.as_ref())
            }
        };

        if let Some(v) = evaluate(policy, target_tool, &facts) {
            return Some(v);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::argument_policy::policy::{ArgumentPolicy, Rule, Severity};
    use serde_json::json;

    // Construct a small in-memory cache and pre-seed it via
    // direct access (test-only) so we can drive `enforce` without
    // a real DB.
    fn cache_with(
        policies: Vec<ArgumentPolicy>,
        tenant: &str,
        group: &str,
        surface: Surface,
    ) -> ArgumentPolicyCache {
        let c = ArgumentPolicyCache::new(300);
        let surface_s = match surface {
            Surface::LlmOutputToolCalls => "llm_output_tool_calls",
            Surface::McpInput => "mcp_input",
            Surface::SkillInput => "skill_input",
        };
        let key = format!("{tenant}:{group}:{surface_s}");
        // Reach in via cache.inner; only legal because we're in
        // the same crate as `cache.rs` — production code uses
        // `get_for_group` which goes through the DB.
        let mut guard = futures::executor::block_on(c.inner_write_for_test());
        guard.insert(
            key,
            crate::services::argument_policy::cache::test_only_make_entry(policies),
        );
        drop(guard);
        c
    }

    fn pol(target: &str, analyzer: &str, arg_key: &str, rule: Rule) -> ArgumentPolicy {
        ArgumentPolicy {
            id: "p".into(),
            tenant_id: "t".into(),
            name: "n".into(),
            surface: Surface::LlmOutputToolCalls,
            target: target.into(),
            argument_key: arg_key.into(),
            analyzer: analyzer.into(),
            analyzer_config: None,
            rules: vec![rule],
            enabled: true,
            severity: Severity::Deny,
        }
    }

    #[tokio::test]
    async fn enforce_returns_none_when_no_policies() {
        let c = ArgumentPolicyCache::new(300);
        // Empty cache → the load_for_group path would try the DB,
        // which we can't reach in unit tests; we exercise only the
        // "policies empty" branch by skipping DB entirely.
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        // Pre-populate empty entry so cache hit returns []
        let mut g = c.inner_write_for_test().await;
        g.insert(
            "t:g:llm_output_tool_calls".into(),
            crate::services::argument_policy::cache::test_only_make_entry(vec![]),
        );
        drop(g);
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::LlmOutputToolCalls,
            "anything",
            &json!({}),
        )
        .await;
        assert!(v.is_none());
    }

    /// A disabled policy refuses nothing, whatever is wrong
    /// with it: its analyzer was looked up (and an unknown one refused the
    /// call) before anything asked whether the policy was enabled.
    #[tokio::test]
    async fn a_disabled_policy_with_an_unknown_analyzer_refuses_nothing() {
        let mut p = pol(
            "transfer_funds",
            "analyzer-from-a-newer-build",
            "*",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: None,
            },
        );
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        let call = |policy: ArgumentPolicy| {
            let pool = pool.clone();
            async move {
                let c = ArgumentPolicyCache::new(300);
                c.inner_write_for_test().await.insert(
                    "t:g:llm_output_tool_calls".into(),
                    crate::services::argument_policy::cache::test_only_make_entry(vec![policy]),
                );
                enforce(
                    &c,
                    &pool,
                    "t",
                    "g",
                    Surface::LlmOutputToolCalls,
                    "transfer_funds",
                    &json!({"amount": 1}),
                )
                .await
            }
        };
        // Enabled: refused, the premise.
        assert_eq!(
            call(p.clone()).await.map(|v| v.rule),
            Some("policy_unavailable".to_string())
        );
        p.enabled = false;
        assert!(call(p).await.is_none());
    }

    /// Pins the fail-closed arm: a policy load that fails is a
    /// refusal carrying `policy_unavailable`, never "no policies".
    #[tokio::test]
    async fn a_failed_policy_load_refuses_the_call() {
        let c = ArgumentPolicyCache::new(300);
        // A closed pool: every query fails at once, as in an outage.
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        pool.close().await;
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::McpInput,
            "transfer_funds",
            &json!({"amount": 1}),
        )
        .await
        .expect("a policy load that failed must refuse the call");
        assert_eq!(v.rule, "policy_unavailable");
        assert_eq!(v.policy_id, "argument_policy_unavailable");
        assert_eq!(v.tool, "transfer_funds");
        assert!(matches!(v.severity, Severity::Deny));
        // And the failure is not cached as an answer: the next call asks
        // again (and is refused again).
        assert!(c.inner_write_for_test().await.is_empty());
    }

    #[tokio::test]
    async fn enforce_skips_when_argument_key_missing_from_args() {
        let p = pol(
            "*",
            "sql",
            "missing_key",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: Some("nope".into()),
            },
        );
        let c = cache_with(vec![p], "t", "g", Surface::LlmOutputToolCalls);
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::LlmOutputToolCalls,
            "tool",
            &json!({"other_key": "DELETE FROM users WHERE 1"}),
        )
        .await;
        assert!(v.is_none(), "policy's argument_key absent → skip, not deny");
    }

    #[tokio::test]
    async fn enforce_denies_write_sql_in_named_arg() {
        let p = pol(
            "*",
            "sql",
            "query",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: Some("no writes".into()),
            },
        );
        let c = cache_with(vec![p], "t", "g", Surface::LlmOutputToolCalls);
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::LlmOutputToolCalls,
            "postgres_query",
            &json!({"query": "DELETE FROM orders WHERE id = 1"}),
        )
        .await;
        assert!(v.is_some());
        assert_eq!(v.as_ref().unwrap().rule, "writes");
        assert_eq!(v.unwrap().message, "no writes");
    }

    #[tokio::test]
    async fn enforce_passes_through_select() {
        let p = pol(
            "*",
            "sql",
            "query",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: Some("no writes".into()),
            },
        );
        let c = cache_with(vec![p], "t", "g", Surface::LlmOutputToolCalls);
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::LlmOutputToolCalls,
            "postgres_query",
            &json!({"query": "SELECT * FROM products WHERE active = true"}),
        )
        .await;
        assert!(v.is_none());
    }

    #[tokio::test]
    async fn enforce_respects_target_narrowing() {
        let mut p = pol(
            "only_this_tool",
            "sql",
            "query",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: Some("no".into()),
            },
        );
        p.target = "only_this_tool".into();
        let c = cache_with(vec![p], "t", "g", Surface::LlmOutputToolCalls);
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        let v = enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::LlmOutputToolCalls,
            "different_tool",
            &json!({"query": "DELETE FROM x"}),
        )
        .await;
        assert!(v.is_none(), "policy targeting different tool must not fire");
    }

    fn threshold(id: &str, config: Option<serde_json::Value>) -> ArgumentPolicy {
        let mut p = pol(
            "transfer_funds",
            "json",
            "*",
            Rule {
                deny_if: Some("field_gt".into()),
                allow_only_if: None,
                values: None,
                field: Some("amount".into()),
                value: Some(json!(1000)),
                severity: None,
                message: None,
            },
        );
        p.id = id.into();
        p.name = id.into();
        p.surface = Surface::McpInput;
        p.analyzer_config = config;
        p
    }

    /// `amount` after `padding` other leaves (keys sort before `amount`).
    fn padded(padding: usize, amount: i64) -> serde_json::Value {
        let mut map: serde_json::Map<String, serde_json::Value> = (0..padding)
            .map(|i| (format!("_{i:05}"), json!(i)))
            .collect();
        map.insert("amount".into(), json!(amount));
        serde_json::Value::Object(map)
    }

    async fn judged(
        policies: Vec<ArgumentPolicy>,
        arguments: serde_json::Value,
    ) -> Option<Violation> {
        let c = ArgumentPolicyCache::new(300);
        c.inner_write_for_test().await.insert(
            "t:g:mcp_input".into(),
            crate::services::argument_policy::cache::test_only_make_entry(policies),
        );
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        enforce(
            &c,
            &pool,
            "t",
            "g",
            Surface::McpInput,
            "transfer_funds",
            &arguments,
        )
        .await
    }

    /// A policy that states a larger `max_leaves` judges an argument the
    /// defaults would refuse as too large: the comparison is made, and it
    /// is made on the field the padding sorted behind.
    #[tokio::test]
    async fn a_policy_analyses_under_the_limits_it_states() {
        use crate::services::argument_policy::evaluator::RULE_ARGUMENTS_TRUNCATED;
        // Under the defaults 600 leaves are too many.
        let v = judged(vec![threshold("default", None)], padded(600, 5)).await;
        assert_eq!(v.map(|v| v.rule).as_deref(), Some(RULE_ARGUMENTS_TRUNCATED));

        let roomy = || threshold("roomy", Some(json!({"max_leaves": 2000})));
        assert!(judged(vec![roomy()], padded(600, 5)).await.is_none());
        let v = judged(vec![roomy()], padded(600, 5000))
            .await
            .expect("the amount behind the padding is compared");
        assert_eq!(v.rule, "field_gt");
        assert_eq!(v.observed, Some(5000.0));
        // Beyond the stated limit it is still a refusal, and an
        // unconditional one: the policy's own severity does not soften it.
        let mut warn_only = roomy();
        warn_only.severity = Severity::Warn;
        let v = judged(vec![warn_only], padded(2000, 5))
            .await
            .expect("2001 leaves under a limit of 2000");
        assert_eq!(v.rule, RULE_ARGUMENTS_TRUNCATED);
        assert!(matches!(v.severity, Severity::Deny));
    }

    /// Several policies on one tool: each analyses under its OWN limits, so
    /// the call passes only when it fits every one of them. A policy with
    /// room does not lend it to its neighbour, in either order.
    #[tokio::test]
    async fn each_policy_on_a_tool_analyses_under_its_own_limits() {
        use crate::services::argument_policy::evaluator::RULE_ARGUMENTS_TRUNCATED;
        let roomy = || threshold("roomy", Some(json!({"max_leaves": 2000})));
        let default = || threshold("default", None);
        for policies in [vec![roomy(), default()], vec![default(), roomy()]] {
            let v = judged(policies, padded(600, 5))
                .await
                .expect("the policy with default limits cannot analyse 601 leaves");
            assert_eq!(v.rule, RULE_ARGUMENTS_TRUNCATED);
            assert_eq!(v.policy_id, "default");
        }
        // An argument that fits both is judged by both.
        assert!(
            judged(vec![roomy(), default()], padded(100, 5))
                .await
                .is_none()
        );
        // A stricter neighbour refuses what the defaults would analyse.
        let strict = threshold("strict", Some(json!({"max_leaves": 50})));
        let v = judged(vec![default(), strict], padded(100, 5))
            .await
            .expect("101 leaves under a limit of 50");
        assert_eq!(
            (v.rule.as_str(), v.policy_id.as_str()),
            (RULE_ARGUMENTS_TRUNCATED, "strict")
        );
    }

    /// A policy that reaches enforcement with a limit this gateway does not
    /// accept refuses the call as a policy it cannot apply. It is not
    /// analysed under the nearest acceptable limit.
    #[tokio::test]
    async fn a_policy_with_an_unacceptable_limit_refuses_the_call() {
        use crate::services::argument_policy::AnalysisLimits;
        for config in [
            json!({"max_leaves": AnalysisLimits::CEILING.max_leaves + 1}),
            json!({"max_depth": 0}),
            json!({"max_array_elements": -1}),
            json!({"max_leaves": "many"}),
        ] {
            let v = judged(
                vec![threshold("odd", Some(config.clone()))],
                json!({"amount": 5}),
            )
            .await
            .unwrap_or_else(|| panic!("{config} must refuse"));
            assert_eq!(v.rule, "policy_unavailable", "{config}");
            assert_eq!(v.policy_id, "odd");
            assert!(matches!(v.severity, Severity::Deny));
        }
    }

    // ---- arguments that arrive in parts ----------------------------------

    fn rule(kind: &str, predicate: &str, field: &str, value: serde_json::Value) -> Rule {
        let list = value
            .is_array()
            .then(|| value.as_array().cloned().unwrap_or_default());
        Rule {
            deny_if: (kind == "deny_if").then(|| predicate.to_string()),
            allow_only_if: (kind == "allow_only_if").then(|| predicate.to_string()),
            values: list.clone(),
            field: Some(field.to_string()),
            value: list.is_none().then_some(value),
            severity: None,
            message: None,
        }
    }

    async fn judged_view(
        policies: Vec<ArgumentPolicy>,
        arguments: Arguments<'_>,
    ) -> Option<Violation> {
        let c = ArgumentPolicyCache::new(300);
        c.inner_write_for_test().await.insert(
            "t:g:skill_input".into(),
            crate::services::argument_policy::cache::test_only_make_entry(policies),
        );
        let pool = PgPool::connect_lazy("postgres://nobody@localhost/nope").unwrap();
        enforce_view(
            &c,
            &pool,
            "t",
            "g",
            Surface::SkillInput,
            "transfer_funds",
            &arguments,
        )
        .await
    }

    fn guarded_band() -> ArgumentPolicy {
        let mut p = threshold("band", None);
        p.surface = Surface::SkillInput;
        p.rules = vec![
            Rule {
                allow_only_if: Some("field_present".into()),
                deny_if: None,
                values: Some(vec![json!("amount")]),
                field: None,
                value: None,
                severity: Some(Severity::Deny),
                message: None,
            },
            Rule {
                severity: Some(Severity::ApprovalRequired),
                ..rule("deny_if", "field_gt", "amount", json!(1000))
            },
        ];
        p
    }

    /// The data parts of a message are one set of fields: `amount` is
    /// addressed by its name wherever its part sits, and beside whatever
    /// other parts the message has.
    #[tokio::test]
    async fn a_field_in_a_data_part_is_addressed_by_its_name_not_its_position() {
        let (small, big) = (json!({"amount": 500}), json!({"amount": 5000}));
        let other = json!({"memo": "invoice", "to": {"account": "A-1"}});
        for parts in [vec![&big], vec![&other, &big], vec![&big, &other]] {
            let v = judged_view(vec![guarded_band()], Arguments::Parts(parts))
                .await
                .expect("the threshold sees the amount");
            assert_eq!(v.rule, "field_gt");
            assert_eq!(v.observed, Some(5000.0));
            assert!(matches!(v.severity, Severity::ApprovalRequired));
        }
        for parts in [vec![&small], vec![&other, &small], vec![&small, &other]] {
            assert!(
                judged_view(vec![guarded_band()], Arguments::Parts(parts))
                    .await
                    .is_none()
            );
        }
        // No data part, or none with the field: the presence guard refuses.
        for parts in [vec![], vec![&other]] {
            let v = judged_view(vec![guarded_band()], Arguments::Parts(parts))
                .await
                .expect("the guard refuses a message that states no amount");
            assert_eq!(v.rule, "field_present");
            assert!(matches!(v.severity, Severity::Deny));
        }
    }

    /// A compared path that more than one data part gives has no one
    /// value: the comparison is undecidable, a violation at the rule's
    /// severity under `deny_if` and under `allow_only_if`, for every
    /// comparison predicate, whatever the values are. The presence guard
    /// is satisfied: the field is there.
    #[tokio::test]
    async fn a_compared_path_in_more_than_one_data_part_is_undecidable() {
        let (a, b) = (
            json!({"amount": 5, "currency": "GBP"}),
            json!({"amount": 5}),
        );
        let v = judged_view(vec![guarded_band()], Arguments::Parts(vec![&a, &b]))
            .await
            .expect("an amount given twice cannot be compared");
        assert_eq!(v.rule, "field_gt");
        assert!(matches!(v.severity, Severity::ApprovalRequired));
        assert_eq!(v.observed, None);
        assert!(
            v.message.contains("more than one data part"),
            "{}",
            v.message
        );

        for (kind, predicate, value) in [
            ("deny_if", "field_gt", json!(1000)),
            ("deny_if", "field_gte", json!(1000)),
            ("deny_if", "field_lt", json!(0)),
            ("deny_if", "field_lte", json!(0)),
            ("deny_if", "field_eq", json!(99)),
            ("deny_if", "field_ne", json!(5)),
            ("deny_if", "field_in", json!([99])),
            ("deny_if", "field_not_in", json!([5])),
            ("allow_only_if", "field_lt", json!(1000)),
            ("allow_only_if", "field_eq", json!(5)),
            ("allow_only_if", "field_in", json!([5])),
            ("allow_only_if", "field_not_in", json!([99])),
        ] {
            let mut p = threshold("p", None);
            p.surface = Surface::SkillInput;
            p.rules = vec![rule(kind, predicate, "amount", value)];
            // One part: decided, and it does not trip.
            assert!(
                judged_view(vec![p.clone()], Arguments::Parts(vec![&a]))
                    .await
                    .is_none(),
                "{kind} {predicate}: premise"
            );
            let v = judged_view(vec![p], Arguments::Parts(vec![&a, &b])).await;
            assert_eq!(
                v.map(|v| v.rule).as_deref(),
                Some(predicate),
                "{kind} {predicate}: undecidable"
            );
        }

        // A path only one part gives is compared, beside an ambiguous one.
        let mut p = threshold("p", None);
        p.surface = Surface::SkillInput;
        p.rules = vec![rule("deny_if", "field_eq", "currency", json!("RUB"))];
        assert!(
            judged_view(vec![p], Arguments::Parts(vec![&a, &b]))
                .await
                .is_none()
        );
        // A scalar in one part and an object in another is the same path twice.
        let nested = json!({"amount": {"value": 50_000}});
        let v = judged_view(vec![guarded_band()], Arguments::Parts(vec![&b, &nested])).await;
        assert_eq!(v.map(|v| v.rule).as_deref(), Some("field_gt"));
    }

    /// The policy's analysis limits bound the parts together, and the
    /// number of parts.
    #[tokio::test]
    async fn the_analysis_limits_bound_the_parts_together() {
        use crate::services::argument_policy::evaluator::RULE_ARGUMENTS_TRUNCATED;
        let part = |from: usize, n: usize| -> serde_json::Value {
            serde_json::Value::Object(
                (from..from + n)
                    .map(|i| (format!("k{i:05}"), json!(i)))
                    .collect(),
            )
        };
        let (a, b, amount) = (part(0, 300), part(300, 300), json!({"amount": 5}));
        let mut p = threshold("p", None);
        p.surface = Surface::SkillInput;
        // 300 + 1 leaves fit the default limit; 300 + 300 + 1 do not.
        assert!(
            judged_view(vec![p.clone()], Arguments::Parts(vec![&a, &amount]))
                .await
                .is_none()
        );
        let v = judged_view(vec![p.clone()], Arguments::Parts(vec![&a, &b, &amount])).await;
        assert_eq!(v.map(|v| v.rule).as_deref(), Some(RULE_ARGUMENTS_TRUNCATED));
        // More parts than an array may have elements.
        let many: Vec<serde_json::Value> = (0..65).map(|i| json!({ format!("f{i}"): i })).collect();
        let v = judged_view(vec![p.clone()], Arguments::Parts(many.iter().collect())).await;
        assert_eq!(v.map(|v| v.rule).as_deref(), Some(RULE_ARGUMENTS_TRUNCATED));
        assert!(
            judged_view(vec![p], Arguments::Parts(many.iter().take(64).collect()))
                .await
                .is_none()
        );
    }

    /// An analyzer that reads one value (here SQL on `query`): one part
    /// offering it is analyzed, none skips the policy, two are undecidable.
    #[tokio::test]
    async fn a_named_argument_given_by_two_parts_is_undecidable() {
        let mut p = pol(
            "transfer_funds",
            "sql",
            "query",
            Rule {
                deny_if: Some("writes".into()),
                allow_only_if: None,
                values: None,
                field: None,
                value: None,
                severity: None,
                message: None,
            },
        );
        p.surface = Surface::SkillInput;
        let (read, write, other) = (
            json!({"query": "SELECT 1"}),
            json!({"query": "DELETE FROM t"}),
            json!({"memo": "x"}),
        );
        let judge = |parts: Vec<&serde_json::Value>| {
            let p = p.clone();
            let parts: Vec<serde_json::Value> = parts.into_iter().cloned().collect();
            async move { judged_view(vec![p], Arguments::Parts(parts.iter().collect())).await }
        };
        assert!(judge(vec![&other, &read]).await.is_none());
        assert!(judge(vec![&other]).await.is_none());
        assert!(judge(vec![&write, &other]).await.is_some());
        assert_eq!(
            judge(vec![&read, &read]).await.map(|v| v.rule).as_deref(),
            Some("writes"),
            "two parts offering the query: undecidable"
        );
    }

    /// What is a Message and what is an opaque body.
    #[test]
    fn an_a2a_message_is_judged_on_its_data_parts_and_an_opaque_body_whole() {
        let parts_of = |m: &serde_json::Value| match Arguments::of_a2a_message(m) {
            Arguments::Parts(p) => Some(p.into_iter().cloned().collect::<Vec<_>>()),
            Arguments::Whole(_) => None,
        };
        let v1 = json!({"messageId": "m", "role": "ROLE_USER", "metadata": {"capability": "x"},
                        "parts": [{"text": "please"}, {"data": {"amount": 5}}]});
        assert_eq!(parts_of(&v1), Some(vec![json!({"amount": 5})]));
        let v03 = json!({"kind": "message", "messageId": "m", "role": "user",
                         "parts": [{"kind": "data", "data": {"amount": 5}},
                                   {"kind": "text", "text": "hi"}]});
        assert_eq!(parts_of(&v03), Some(vec![json!({"amount": 5})]));
        assert_eq!(parts_of(&json!({"parts": []})), Some(vec![]));
        // Opaque bodies, the last two with a `parts` member of their own.
        for opaque in [
            json!({"amount": 5}),
            json!("text"),
            json!({"amount": 50_000, "parts": []}),
            json!({"parts": "three", "role": "user"}),
        ] {
            assert_eq!(parts_of(&opaque), None, "{opaque}");
        }
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! What one argument policy costs per call: the JSON analysis of the
//! arguments and the evaluation of the policy's rules over the facts.
//!
//! The analysis limits (`AnalysisLimits`) bound that cost. This bench is
//! where the hard ceilings come from: it times an argument that reaches
//! the default limits, arguments between the defaults and the ceilings,
//! and an argument that reaches every ceiling at once, each under a policy
//! with a presence guard and a two-step band (the shape of a threshold
//! policy). Sizes above the leaf ceiling are skipped: to see the curve
//! beyond it, raise `AnalysisLimits::CEILING` locally and run again.
//!
//! * `analyze/<leaves>`: `argument_policy::analyze` alone, on a flat
//!   object of that many numeric leaves.
//! * `analyze_and_evaluate/<leaves>`: the analysis and `evaluate`, as
//!   `enforce` runs them for one policy.
//! * `shape/…`: the same total for arguments that reach the depth and the
//!   array-width limits instead of the leaf count.

use brutor_gateway_core::services::argument_policy::policy::{
    ArgumentPolicy, Rule, Severity, Surface,
};
use brutor_gateway_core::services::argument_policy::{AnalysisLimits, ArgKind, analyze, evaluate};
use criterion::{BenchmarkId, Criterion, criterion_group, criterion_main};
use serde_json::{Value, json};
use std::hint::black_box;

fn rule(kind: &str, predicate: &str, field: Option<&str>, value: Option<Value>) -> Rule {
    Rule {
        deny_if: (kind == "deny").then(|| predicate.to_string()),
        allow_only_if: (kind == "allow").then(|| predicate.to_string()),
        values: (predicate == "field_present").then(|| vec![json!("amount")]),
        field: field.map(str::to_string),
        value,
        severity: None,
        message: None,
    }
}

/// A presence guard and a band on `amount`, analysing under `limits`.
fn policy(limits: AnalysisLimits) -> ArgumentPolicy {
    ArgumentPolicy {
        id: "ap-bench".into(),
        tenant_id: "t".into(),
        name: "bench".into(),
        surface: Surface::McpInput,
        target: "*".into(),
        argument_key: "*".into(),
        analyzer: "json".into(),
        analyzer_config: Some(json!({
            "max_leaves": limits.max_leaves,
            "max_depth": limits.max_depth,
            "max_array_elements": limits.max_array_elements,
        })),
        rules: vec![
            rule("allow", "field_present", None, None),
            rule("deny", "field_gt", Some("amount"), Some(json!(10000))),
            rule("deny", "field_gt", Some("amount"), Some(json!(1000))),
        ],
        enabled: true,
        severity: Severity::Deny,
    }
}

/// `amount` and `leaves - 1` other numeric leaves, flat.
fn flat(leaves: usize) -> Value {
    let mut map: serde_json::Map<String, Value> = (1..leaves)
        .map(|i| (format!("field_{i:05}"), json!(i)))
        .collect();
    map.insert("amount".into(), json!(500));
    Value::Object(map)
}

/// `leaves` leaves in all: `amount`, and numbers in arrays of at most
/// `width` under a chain of objects that puts the elements at `depth`.
fn shaped(leaves: usize, width: usize, depth: usize) -> Value {
    let mut v = Value::Object(
        (0..leaves / width)
            .map(|i| {
                // One element short in the first array: `amount` is a leaf.
                let n = if i == 0 { width - 1 } else { width };
                (format!("a{i}"), json!((0..n).collect::<Vec<_>>()))
            })
            .collect(),
    );
    for _ in 0..depth.saturating_sub(3) {
        v = json!({ "n": v });
    }
    json!({ "amount": 500, "n": v })
}

fn run(policy: &ArgumentPolicy, arguments: &Value) {
    let facts = analyze(arguments, ArgKind::Json, policy.analyzer_config.as_ref());
    assert!(
        !facts.json_truncated,
        "the bench argument must fit its limits"
    );
    black_box(evaluate(policy, "transfer_funds", &facts));
}

fn bench_argument_analysis(c: &mut Criterion) {
    let ceiling = AnalysisLimits::CEILING;
    let roomy = policy(ceiling);

    let mut group = c.benchmark_group("argument_analysis");
    let sizes = [16, 512, 2048, 4096, 32768, 131072];
    for leaves in sizes.into_iter().filter(|n| *n <= ceiling.max_leaves) {
        let arguments = flat(leaves);
        let config = json!({ "max_leaves": leaves });
        group.bench_with_input(BenchmarkId::new("analyze", leaves), &arguments, |b, a| {
            b.iter(|| black_box(analyze(a, ArgKind::Json, Some(&config))))
        });
        group.bench_with_input(
            BenchmarkId::new("analyze_and_evaluate", leaves),
            &arguments,
            |b, a| b.iter(|| run(&roomy, a)),
        );
    }

    let d = AnalysisLimits::DEFAULT;
    for (name, leaves, width, depth) in [
        (
            "default_limits",
            d.max_leaves,
            d.max_array_elements,
            d.max_depth,
        ),
        (
            "every_ceiling",
            ceiling.max_leaves,
            ceiling.max_array_elements,
            ceiling.max_depth,
        ),
    ] {
        let arguments = shaped(leaves, width, depth);
        group.bench_with_input(BenchmarkId::new("shape", name), &arguments, |b, a| {
            b.iter(|| run(&roomy, a))
        });
    }
    group.finish();
}

criterion_group!(benches, bench_argument_analysis);
criterion_main!(benches);

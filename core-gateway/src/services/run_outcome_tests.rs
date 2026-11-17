// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The classification, grammar, catalogue lookup and error-cause
//! precedence, one case per cell of the spec's tables (hand-computed).

use super::*;
use crate::services::run_ledger::terminal;
use crate::services::run_segments::outcome as seg;
use serde_json::json;

// -- wire names ---------------------------------------------------------------

#[test]
fn wire_names_match_the_spec() {
    assert_eq!(OUTCOME_ID_HEADER, "X-Brutor-Outcome-Id");
    assert_eq!(OUTCOME_CONFORMANCE_HEADER, "X-Brutor-Outcome-Conformance");
    assert_eq!(METADATA_OUTCOME_ID, "brutor.outcome.id");
    assert_eq!(METADATA_OUTCOME_CONFORMANCE, "brutor.outcome.conformance");
    assert_eq!(OTEL_OUTCOME_ID_ATTRIBUTE, "brutor.outcome.id");
    assert_eq!(conformance::ALL, ["declared", "undeclared", "unreported"]);
    assert_eq!(
        run_class::ALL,
        ["success", "no_work", "deviation", "failed", "not_decisive"]
    );
    // The precedence order IS the vocabulary order.
    assert_eq!(
        error_cause::ALL,
        [
            "blocked_by_policy",
            "rate_limited",
            "model_unavailable",
            "model_rejected",
            "tool_failed",
            "skill_failed",
            "agent_unreachable",
            "delegate_deviated",
            "iteration_limit",
            "client_reported",
        ]
    );
    for v in conformance::ALL.iter().chain(run_class::ALL.iter()) {
        assert!(v.len() <= 16, "{v} does not fit VARCHAR(16)");
    }
    for v in error_cause::ALL {
        assert!(v.len() <= 32, "{v} does not fit VARCHAR(32)");
    }
}

// -- grammar ------------------------------------------------------------------

#[test]
fn the_grammar_accepts_outcome_and_outcome_dot_variant() {
    for ok in [
        "task_completed",
        "nothing_to_do",
        "application_processed.loan_approved",
        "a",
        "a.b",
        "x-1_2.y-3_4",
        &"a".repeat(48),
        &format!("{}.{}", "a".repeat(48), "b".repeat(48)),
    ] {
        assert!(is_well_formed(ok), "{ok:?} should be well-formed");
    }
}

#[test]
fn the_grammar_rejects_everything_else() {
    for bad in [
        "",
        ".",
        "a.",
        ".a",
        "a.b.c",
        "A",
        "Task_Completed",
        "task completed",
        "task/completed",
        "tâche",
        "a..b",
        &"a".repeat(49),
        &format!("a.{}", "b".repeat(49)),
        " a",
    ] {
        assert!(!is_well_formed(bad), "{bad:?} should be malformed");
    }
}

#[test]
fn a_reported_value_is_stored_verbatim_and_truncated_never_dropped() {
    assert_eq!(stored("  task_completed "), Some("task_completed".into()));
    assert_eq!(stored(""), None, "blank is nothing reported");
    assert_eq!(stored("   "), None);
    // Malformed is kept: it is evidence of a deviation.
    assert_eq!(stored("Not Valid!"), Some("Not Valid!".into()));
    let long = "x".repeat(300);
    assert_eq!(stored(&long).unwrap().chars().count(), MAX_STORED_LEN);
    // Truncation is on characters, never mid-codepoint.
    let wide = "é".repeat(200);
    let s = stored(&wide).unwrap();
    assert_eq!(s.chars().count(), MAX_STORED_LEN);
    let hv = axum::http::HeaderValue::from_bytes(&[b'a', 0xff, b'b']).unwrap();
    let got = stored_from_header(&hv).unwrap();
    assert!(got.starts_with('a') && got.ends_with('b'), "{got:?}");
}

// -- the classification, every cell -------------------------------------------

#[test]
fn every_cell_of_the_classification() {
    use conformance::*;
    use run_class::*;
    let cases: &[(&str, Option<&str>, Option<bool>, &str)] = &[
        // errored / exhausted -> failed, whatever the outcome.
        (terminal::ERRORED, None, None, FAILED),
        (terminal::ERRORED, Some(DECLARED), Some(true), FAILED),
        (terminal::ERRORED, Some(UNDECLARED), None, FAILED),
        (terminal::EXHAUSTED, Some(DECLARED), Some(true), FAILED),
        (terminal::EXHAUSTED, Some(UNREPORTED), None, FAILED),
        // abandoned / cancelled -> not decisive.
        (
            terminal::ABANDONED,
            Some(DECLARED),
            Some(true),
            NOT_DECISIVE,
        ),
        (terminal::ABANDONED, None, None, NOT_DECISIVE),
        (terminal::CANCELLED, Some(UNDECLARED), None, NOT_DECISIVE),
        // completed + declared + counts -> success.
        (terminal::COMPLETED, Some(DECLARED), Some(true), SUCCESS),
        (
            terminal::COMPLETED_DEGRADED,
            Some(DECLARED),
            Some(true),
            SUCCESS,
        ),
        // completed + declared + does not count -> no work.
        (terminal::COMPLETED, Some(DECLARED), Some(false), NO_WORK),
        (
            terminal::COMPLETED_DEGRADED,
            Some(DECLARED),
            Some(false),
            NO_WORK,
        ),
        // declared but the flag unknown is not success.
        (terminal::COMPLETED, Some(DECLARED), None, NO_WORK),
        // completed + undeclared / unreported / unjudged -> deviation.
        (terminal::COMPLETED, Some(UNDECLARED), None, DEVIATION),
        (terminal::COMPLETED, Some(UNREPORTED), None, DEVIATION),
        (terminal::COMPLETED, None, None, DEVIATION),
        (
            terminal::COMPLETED_DEGRADED,
            Some(UNDECLARED),
            Some(true),
            DEVIATION,
        ),
        // Unknown terminal state counts against, never for.
        ("blocked_policy", Some(DECLARED), Some(true), FAILED),
        ("finished-ish", Some(DECLARED), Some(true), FAILED),
    ];
    for (ts, conf, counts, want) in cases {
        assert_eq!(
            run_class(ts, *conf, *counts),
            *want,
            "{ts} / {conf:?} / {counts:?}"
        );
    }
}

#[test]
fn only_completed_declared_counting_runs_are_success() {
    // Exhaustive over the vocabularies: success has exactly one shape.
    for ts in terminal::ALL {
        for conf in conformance::ALL.iter().map(|c| Some(*c)).chain([None]) {
            for counts in [Some(true), Some(false), None] {
                let class = run_class(ts, conf, counts);
                let expect_success = matches!(ts, "completed" | "completed_degraded")
                    && conf == Some(conformance::DECLARED)
                    && counts == Some(true);
                assert_eq!(
                    class == run_class::SUCCESS,
                    expect_success,
                    "{ts}/{conf:?}/{counts:?}"
                );
            }
        }
    }
}

// -- the catalogue --------------------------------------------------------------

fn demo_catalogue() -> Catalogue {
    Catalogue::from_section(Some(&json!([
        {"id": "application_processed", "label": "Application processed",
         "counts_toward_baseline": true,
         "variants": [{"id": "loan_approved", "label": "a"},
                      {"id": "loan_rejected", "label": "r"}]},
        {"id": "no_applications_available", "label": "Nothing",
         "counts_toward_baseline": false,
         "expect": {"max_actions": 2}},
        {"id": "task_completed", "label": "Done", "counts_toward_baseline": true,
         "variants": []}
    ])))
    .expect("a list is a catalogue")
}

#[test]
fn no_contract_or_no_section_judges_every_reported_id_undeclared() {
    let none = judge(Some("task_completed"), None);
    assert_eq!(none.conformance, conformance::UNDECLARED);
    assert_eq!(none.counts_toward_baseline, None);
    assert_eq!(none.outcome_id.as_deref(), Some("task_completed"));
    for section in [None, Some(json!(null)), Some(json!({"not": "a list"}))] {
        let c = Catalogue::from_section(section.as_ref());
        assert!(c.is_none(), "{section:?} is no section");
        assert_eq!(
            judge(Some("task_completed"), c.as_ref()).conformance,
            conformance::UNDECLARED
        );
    }
    // An empty catalogue declares nothing.
    let empty = Catalogue::from_section(Some(&json!([]))).unwrap();
    assert_eq!(
        judge(Some("task_completed"), Some(&empty)).conformance,
        conformance::UNDECLARED
    );
}

#[test]
fn nothing_reported_is_unreported_with_or_without_a_catalogue() {
    let c = demo_catalogue();
    for reported in [None, Some(""), Some("   ")] {
        let j = judge(reported, Some(&c));
        assert_eq!(j.conformance, conformance::UNREPORTED);
        assert_eq!(j.outcome_id, None);
        assert_eq!(j.counts_toward_baseline, None);
        assert_eq!(judge(reported, None).conformance, conformance::UNREPORTED);
    }
}

#[test]
fn declared_ids_carry_their_baseline_flag() {
    let c = demo_catalogue();
    let j = judge(Some("application_processed.loan_approved"), Some(&c));
    assert_eq!(j.conformance, conformance::DECLARED);
    assert_eq!(j.counts_toward_baseline, Some(true));
    let j = judge(Some("no_applications_available"), Some(&c));
    assert_eq!(j.conformance, conformance::DECLARED);
    assert_eq!(j.counts_toward_baseline, Some(false));
    let j = judge(Some("task_completed"), Some(&c));
    assert_eq!(
        j.conformance,
        conformance::DECLARED,
        "empty variants = bare"
    );
}

#[test]
fn an_outcome_with_variants_must_be_reported_with_one() {
    let c = demo_catalogue();
    let j = judge(Some("application_processed"), Some(&c));
    assert_eq!(j.conformance, conformance::UNDECLARED);
    assert_eq!(j.counts_toward_baseline, None);
}

#[test]
fn unknown_ids_and_variants_are_undeclared() {
    let c = demo_catalogue();
    for id in [
        "application_processed.loan_maybe",
        "screening_completed.hit",
        "no_applications_available.today",
        "task_completed.x",
        "something_else",
    ] {
        let j = judge(Some(id), Some(&c));
        assert_eq!(j.conformance, conformance::UNDECLARED, "{id}");
        assert_eq!(j.counts_toward_baseline, None, "{id}");
        assert_eq!(j.outcome_id.as_deref(), Some(id), "stored verbatim");
    }
}

#[test]
fn a_malformed_report_is_undeclared_even_if_it_resembles_a_declared_id() {
    let c = demo_catalogue();
    for id in [
        "Task_Completed",
        "task_completed.",
        "task completed",
        "a.b.c",
    ] {
        assert_eq!(
            judge(Some(id), Some(&c)).conformance,
            conformance::UNDECLARED,
            "{id}"
        );
    }
    // Truncated to the column, verbatim.
    let long = format!("task_completed{}", "x".repeat(200));
    let j = judge(Some(&long), Some(&c));
    assert_eq!(j.conformance, conformance::UNDECLARED);
    assert_eq!(j.outcome_id.unwrap().len(), MAX_STORED_LEN);
}

#[test]
fn unreadable_catalogue_entries_declare_nothing() {
    let c = Catalogue::from_section(Some(&json!([
        {"id": "no_flag", "label": "x"},
        {"id": "string_flag", "label": "x", "counts_toward_baseline": "true"},
        {"id": "Bad Id", "label": "x", "counts_toward_baseline": true},
        {"id": "bad_variants", "label": "x", "counts_toward_baseline": true,
         "variants": "loan_approved"},
        {"id": "bad_variant_id", "label": "x", "counts_toward_baseline": true,
         "variants": [{"id": "OK"}]},
        {"label": "no id", "counts_toward_baseline": true},
        "not an object",
        {"id": "fine", "label": "x", "counts_toward_baseline": true}
    ])))
    .unwrap();
    for id in [
        "no_flag",
        "string_flag",
        "bad_variants",
        "bad_variants.loan_approved",
        "bad_variant_id",
    ] {
        assert_eq!(
            judge(Some(id), Some(&c)).conformance,
            conformance::UNDECLARED,
            "{id}"
        );
    }
    assert_eq!(
        judge(Some("fine"), Some(&c)).conformance,
        conformance::DECLARED
    );
}

// -- error causes: every row of the precedence table ------------------------------

fn call(proxy_type: &str, subtype: Option<&str>, status: Option<i32>) -> CallFacts {
    CallFacts {
        id: "pl-1".into(),
        proxy_type: proxy_type.into(),
        proxy_subtype: subtype.map(str::to_string),
        http_status_code: status,
        ..Default::default()
    }
}

#[test]
fn blocked_by_policy_from_every_refusal_shape() {
    let mut g = call("llm", None, Some(403));
    g.guardrail_blocked = true;
    assert_eq!(cause_of_call(&g), Some(error_cause::BLOCKED_BY_POLICY));
    let mut p = call("mcp", None, Some(403));
    p.argument_policy_denied = true;
    assert_eq!(cause_of_call(&p), Some(error_cause::BLOCKED_BY_POLICY));
    for s in REFUSAL_SUBTYPES {
        assert_eq!(
            cause_of_call(&call("a2a", Some(s), Some(403))),
            Some(error_cause::BLOCKED_BY_POLICY),
            "{s}"
        );
    }
    // Approval denied and expired are refusals.
    assert!(REFUSAL_SUBTYPES.contains(&"approval_rejected"));
    assert!(REFUSAL_SUBTYPES.contains(&"approval_token_invalid"));
    // Precedence: a refusal answered with 429 is still the policy.
    let q = call("skill", Some("skill_quota_exceeded"), Some(429));
    assert_eq!(cause_of_call(&q), Some(error_cause::BLOCKED_BY_POLICY));
}

#[test]
fn a_429_is_rate_limited_on_any_call_type() {
    for t in ["llm", "mcp", "virtual-mcp", "skill", "a2a", "oauth"] {
        assert_eq!(
            cause_of_call(&call(t, None, Some(429))),
            Some(error_cause::RATE_LIMITED),
            "{t}"
        );
    }
}

#[test]
fn model_calls_split_unavailable_from_rejected() {
    for s in [Some(500), Some(502), Some(503), Some(504), None, Some(200)] {
        assert_eq!(
            cause_of_call(&call("llm", None, s)),
            Some(error_cause::MODEL_UNAVAILABLE),
            "{s:?}"
        );
    }
    for s in [400, 401, 404, 413, 422] {
        assert_eq!(
            cause_of_call(&call("llm", Some("embeddings"), Some(s))),
            Some(error_cause::MODEL_REJECTED),
            "{s}"
        );
    }
}

#[test]
fn tool_skill_and_agent_rows() {
    assert_eq!(
        cause_of_call(&call("mcp", None, Some(500))),
        Some(error_cause::TOOL_FAILED)
    );
    assert_eq!(
        cause_of_call(&call("virtual-mcp", None, Some(200))),
        Some(error_cause::TOOL_FAILED)
    );
    assert_eq!(
        cause_of_call(&call("skill", None, Some(500))),
        Some(error_cause::SKILL_FAILED)
    );
    // A skill run over MCP is a skill, never a tool.
    assert_eq!(
        cause_of_call(&call("mcp", Some("skill_run"), Some(500))),
        Some(error_cause::SKILL_FAILED)
    );
    // A delegated call: its segment's cause, else unreachable.
    assert_eq!(
        cause_of_call(&call("a2a", Some("outbound"), Some(502))),
        Some(error_cause::AGENT_UNREACHABLE)
    );
    let mut dev = call("a2a", Some("outbound"), Some(200));
    dev.segment_cause = Some(error_cause::DELEGATE_DEVIATED);
    assert_eq!(cause_of_call(&dev), Some(error_cause::DELEGATE_DEVIATED));
    // A kind with no row explains nothing.
    assert_eq!(cause_of_call(&call("oauth", None, Some(500))), None);
    assert_eq!(cause_of_call(&call("portal", None, Some(500))), None);
}

#[test]
fn iteration_limit_and_client_reported_close_the_table() {
    // exhausted with no failing call, or one the run recovered from.
    assert_eq!(
        derive_error_cause(terminal::EXHAUSTED, None, false),
        Some(error_cause::ITERATION_LIMIT)
    );
    let rl = call("llm", None, Some(429));
    assert_eq!(
        derive_error_cause(terminal::EXHAUSTED, Some(&rl), false),
        Some(error_cause::ITERATION_LIMIT),
        "a recovered 429 does not explain an iteration limit"
    );
    assert_eq!(
        derive_error_cause(terminal::EXHAUSTED, Some(&rl), true),
        Some(error_cause::RATE_LIMITED),
        "a run that ENDED on the 429 hit the limit"
    );
    // errored with no failing call, or one that explains nothing.
    assert_eq!(
        derive_error_cause(terminal::ERRORED, None, false),
        Some(error_cause::CLIENT_REPORTED)
    );
    assert_eq!(
        derive_error_cause(
            terminal::ERRORED,
            Some(&call("oauth", None, Some(500))),
            true
        ),
        Some(error_cause::CLIENT_REPORTED)
    );
    // errored explained by its failing call, last step or not.
    assert_eq!(
        derive_error_cause(
            terminal::ERRORED,
            Some(&call("llm", None, Some(502))),
            false
        ),
        Some(error_cause::MODEL_UNAVAILABLE)
    );
}

#[test]
fn only_errored_and_exhausted_carry_a_cause() {
    let c = call("llm", None, Some(502));
    for ts in terminal::ALL {
        let got = derive_error_cause(ts, Some(&c), true);
        assert_eq!(
            got.is_some(),
            matches!(ts, "errored" | "exhausted"),
            "{ts}: {got:?}"
        );
    }
}

#[test]
fn step_kinds_mirror_the_control_plane() {
    assert_eq!(step_kind_of("llm", None), step_kind::MODEL);
    assert_eq!(step_kind_of("llm", Some("skill_run")), step_kind::MODEL);
    assert_eq!(step_kind_of("skill", None), step_kind::SKILL);
    assert_eq!(step_kind_of("mcp", Some("skill_run")), step_kind::SKILL);
    assert_eq!(step_kind_of("mcp", None), step_kind::TOOL);
    assert_eq!(step_kind_of("virtual-mcp", None), step_kind::TOOL);
    assert_eq!(step_kind_of("a2a", None), step_kind::AGENT);
    assert_eq!(step_kind_of("oauth", None), step_kind::OTHER);
}

// -- segment error causes -----------------------------------------------------------

#[test]
fn segment_error_causes() {
    use conformance::*;
    // A refusal is the gateway's, whatever else is true.
    assert_eq!(
        segment_error_cause(seg::FAILED, true, true, Some(DECLARED)),
        Some(error_cause::BLOCKED_BY_POLICY)
    );
    assert_eq!(
        segment_error_cause(seg::FAILED, true, false, None),
        Some(error_cause::BLOCKED_BY_POLICY)
    );
    for o in [seg::FAILED, seg::TIMED_OUT] {
        assert_eq!(
            segment_error_cause(o, false, true, None),
            Some(error_cause::AGENT_UNREACHABLE),
            "{o}"
        );
    }
    for o in [seg::ANSWERED, seg::REJECTED] {
        assert_eq!(segment_error_cause(o, false, true, Some(DECLARED)), None);
        for c in [Some(UNDECLARED), Some(UNREPORTED), None] {
            assert_eq!(
                segment_error_cause(o, false, true, c),
                Some(error_cause::DELEGATE_DEVIATED),
                "{o}/{c:?}"
            );
        }
    }
    // Not entered over A2A: no delegate to deviate or be unreachable.
    assert_eq!(segment_error_cause(seg::FAILED, false, false, None), None);
    assert_eq!(segment_error_cause(seg::ANSWERED, false, false, None), None);
}

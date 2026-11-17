// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Unit + property tests for `run_segments`.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use chrono::{DateTime, Duration, TimeZone, Utc};
use proptest::collection::vec;
use proptest::prelude::*;

use super::*;
use crate::services::run_ledger;

fn t0() -> DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 9, 26, 12, 0, 0).unwrap()
}

fn part(group: Option<&str>, actions: i64, tokens: i64, cost_units: i64, at: i64) -> GroupPart {
    GroupPart {
        group_id: group.map(str::to_string),
        action_count: actions,
        llm_call_count: actions,
        total_tokens: tokens,
        cost_units,
        first_at: t0() + Duration::seconds(at),
        last_at: t0() + Duration::seconds(at + actions),
        // Its first call ran for half a second before it was stamped.
        active_from: t0() + Duration::seconds(at) - Duration::milliseconds(500),
        last_id: format!("last-{at}"),
        last_success: true,
        ..Default::default()
    }
}

fn outbound(id: &str, group: Option<&str>, at: i64) -> DelegationCall {
    DelegationCall {
        id: id.to_string(),
        at: t0() + Duration::seconds(at),
        group_id: group.map(str::to_string),
        server_id: Some("card-fraud".into()),
        proxy_type: "a2a".into(),
        proxy_subtype: Some("outbound".into()),
        actor_agent_id: Some("agent-caller".into()),
        success: true,
        http_status_code: Some(200),
        ..Default::default()
    }
}

fn entering(id: &str, group: Option<&str>, at: i64, child: &str) -> DelegationCall {
    DelegationCall {
        child_groups: vec![child.to_string()],
        has_children: true,
        ..outbound(id, group, at)
    }
}

fn systems(pairs: &[(&str, &str)]) -> HashMap<String, String> {
    pairs
        .iter()
        .map(|(g, s)| (g.to_string(), s.to_string()))
        .collect()
}

// ── The two-system delegation ──────────────────────────────────────────

#[test]
fn a_two_system_delegation_yields_one_segment_and_conserves() {
    let parts = vec![
        part(Some("g-root"), 7, 900, 12_000, 0),
        part(Some("g-fraud"), 2, 300, 4_000, 3),
    ];
    let calls = vec![entering("call-1", Some("g-root"), 2, "g-fraud")];
    let map = systems(&[("g-root", "sys-root"), ("g-fraud", "sys-fraud")]);
    let p = partition(&parts, &calls, Some("sys-root"), &map, &HashMap::new());

    assert_eq!(p.segments.len(), 1);
    let s = &p.segments[0];
    assert_eq!(s.system_id, "sys-fraud");
    assert_eq!(s.tally.action_count, 2);
    assert_eq!(s.tally.total_tokens, 300);
    assert_eq!(s.tally.cost_units, 4_000);
    assert_eq!(s.caller_agent_id.as_deref(), Some("agent-caller"));
    assert_eq!(s.outcome, outcome::ANSWERED);
    assert_eq!(s.entered_by, vec!["call-1".to_string()]);
    assert_eq!(p.root_own.action_count, 7);
    assert_eq!(p.totals(), Tally::of_parts(&parts));
    assert_eq!(p.via(), vec!["sys-fraud".to_string()]);
}

#[test]
fn a_delegated_step_is_never_missing_from_both_shares() {
    // The delegate's rows sit at depth 1 under a group of ITS system; the
    // root has two groups and an unattributed (NULL-group) step.
    let parts = vec![
        part(Some("g-root"), 3, 100, 10, 0),
        part(Some("g-root-2"), 1, 50, 5, 1),
        part(None, 1, 0, 0, 2),
        part(Some("g-fraud"), 4, 400, 40, 3),
        part(Some("g-orphan"), 2, 20, 2, 4),
    ];
    let map = systems(&[
        ("g-root", "sys-root"),
        ("g-root-2", "sys-root"),
        ("g-fraud", "sys-fraud"),
    ]);
    let p = partition(&parts, &[], Some("sys-root"), &map, &HashMap::new());
    // Exactly one home per part: at most one segment holds it, and the ones
    // no segment holds are precisely the root's own share.
    let mut unheld = 0;
    for part in &parts {
        let homes = p
            .segments
            .iter()
            .filter(|s| {
                part.group_id
                    .as_ref()
                    .is_some_and(|g| s.group_ids.contains(g))
            })
            .count();
        assert!(homes <= 1, "{:?} is in {homes} segments", part.group_id);
        if homes == 0 {
            unheld += part.action_count;
        }
    }
    assert_eq!(p.root_own.action_count, unheld);
    assert_eq!(p.root_own.action_count, 3 + 1 + 1 + 2);
    assert_eq!(p.segments.len(), 1);
    assert_eq!(p.segments[0].tally.action_count, 4);
    assert_eq!(p.totals().action_count, 11);
}

#[test]
fn an_unattributed_run_segments_every_ai_system() {
    let parts = vec![
        part(Some("g-free"), 2, 10, 1, 0),
        part(Some("g-a"), 1, 5, 1, 1),
    ];
    let map = systems(&[("g-a", "sys-a")]);
    let p = partition(&parts, &[], None, &map, &HashMap::new());
    assert_eq!(p.via(), vec!["sys-a".to_string()]);
    assert_eq!(p.root_own.action_count, 2);
    // Judged by its own last step when no call entered it.
    assert_eq!(p.segments[0].outcome, outcome::ANSWERED);
    assert!(p.segments[0].caller_agent_id.is_none());
}

#[test]
fn a_system_split_across_groups_is_one_segment() {
    let mut a = part(Some("svc-a"), 2, 2, 2, 5);
    a.models = vec!["m1".into(), "m1".into()];
    a.tools = vec!["lookup".into()];
    let mut b = part(Some("svc-b"), 3, 3, 3, 2);
    b.models = vec!["m1".into(), "m2".into()];
    b.tools = vec!["lookup".into(), "score".into()];
    b.min_depth = 1;
    a.min_depth = 2;
    let parts = vec![part(Some("g-root"), 1, 1, 1, 0), a, b];
    let map = systems(&[("g-root", "root"), ("svc-a", "svc"), ("svc-b", "svc")]);
    let p = partition(&parts, &[], Some("root"), &map, &HashMap::new());
    assert_eq!(p.segments.len(), 1);
    let s = &p.segments[0];
    assert_eq!(s.tally.action_count, 5);
    assert_eq!(s.min_depth, 1);
    assert_eq!(s.first_action_at, t0() + Duration::seconds(2));
    assert_eq!(s.last_action_at, t0() + Duration::seconds(7));
    assert_eq!(s.group_ids, vec!["svc-a".to_string(), "svc-b".to_string()]);
    assert_eq!(s.tools, vec!["lookup".to_string(), "score".to_string()]);
    assert_eq!(
        s.models_used,
        BTreeMap::from([("m1".to_string(), 3), ("m2".to_string(), 1)])
    );
}

// ── Active time starts when the first call started ─────────

#[test]
fn a_one_call_segment_is_active_for_its_call() {
    // One step: stamped when it finished, so first == last stamp.
    let mut svc = part(Some("g-svc"), 1, 1, 1, 5);
    svc.last_at = svc.first_at;
    svc.active_from = svc.first_at - Duration::milliseconds(1_243);
    let parts = vec![part(Some("g-root"), 1, 1, 1, 0), svc];
    let map = systems(&[("g-root", "root"), ("g-svc", "svc")]);
    let p = partition(&parts, &[], Some("root"), &map, &HashMap::new());
    let s = &p.segments[0];
    assert_eq!(s.first_action_at, s.last_action_at);
    assert_eq!(
        active_span_ms(s),
        1_243,
        "was 0 when measured stamp to stamp"
    );
}

#[test]
fn a_split_segment_starts_at_its_earliest_call() {
    // svc-b is stamped later but its call started first.
    let mut a = part(Some("svc-a"), 1, 1, 1, 2);
    a.active_from = a.first_at - Duration::seconds(1);
    let mut b = part(Some("svc-b"), 1, 1, 1, 4);
    b.active_from = t0();
    let parts = vec![part(Some("g-root"), 1, 1, 1, 0), a, b];
    let map = systems(&[("g-root", "root"), ("svc-a", "svc"), ("svc-b", "svc")]);
    let p = partition(&parts, &[], Some("root"), &map, &HashMap::new());
    let s = &p.segments[0];
    assert_eq!(s.first_action_at, t0() + Duration::seconds(2));
    assert_eq!(s.active_from, t0());
    assert_eq!(active_span_ms(s), 5_000);
}

#[test]
fn a_start_after_the_first_stamp_is_clamped() {
    // A part can never have started after it finished; if one says so the
    // segment keeps the first stamp (the old measure), never a later one.
    let mut svc = part(Some("g-svc"), 2, 1, 1, 5);
    svc.active_from = svc.first_at + Duration::seconds(1);
    let parts = vec![part(Some("g-root"), 1, 1, 1, 0), svc];
    let map = systems(&[("g-root", "root"), ("g-svc", "svc")]);
    let p = partition(&parts, &[], Some("root"), &map, &HashMap::new());
    let s = &p.segments[0];
    assert_eq!(s.active_from, s.first_action_at);
    assert_eq!(active_span_ms(s), 2_000);
}

// ── Calls that never reached the delegate ───────────────────────────────

#[test]
fn an_unanswered_call_is_attributed_through_the_declared_edge() {
    let parts = vec![part(Some("g-root"), 3, 10, 1, 0)];
    let mut call = outbound("call-x", Some("g-root"), 1);
    call.success = false;
    call.http_status_code = Some(502);
    call.error_message = Some("Remote agent request failed: operation timed out".into());
    let map = systems(&[("g-root", "sys-root")]);
    let edges = HashMap::from([(
        ("sys-root".to_string(), "card-fraud".to_string()),
        "sys-fraud".to_string(),
    )]);
    let p = partition(&parts, &[call], Some("sys-root"), &map, &edges);
    assert_eq!(p.segments.len(), 1);
    let s = &p.segments[0];
    assert_eq!(s.system_id, "sys-fraud");
    assert_eq!(s.tally, Tally::default(), "it did no work — zero actions");
    assert_eq!(s.outcome, outcome::TIMED_OUT);
    assert_eq!(s.min_depth, 1);
    assert_eq!(s.first_action_at, t0() + Duration::seconds(1));
    // It did no work of its own, so it has no active time.
    assert_eq!(s.active_from, s.first_action_at);
    assert_eq!(active_span_ms(s), 0);
    // Its step (the failed call) stays in the caller's share: conserved.
    assert_eq!(p.totals(), Tally::of_parts(&parts));
}

#[test]
fn an_undeclared_card_that_never_answered_makes_no_segment() {
    let parts = vec![part(Some("g-root"), 3, 10, 1, 0)];
    let mut call = outbound("call-x", Some("g-root"), 1);
    call.success = false;
    let map = systems(&[("g-root", "sys-root")]);
    let p = partition(&parts, &[call], Some("sys-root"), &map, &HashMap::new());
    assert!(p.segments.is_empty());
}

#[test]
fn a_declared_edge_back_to_the_root_is_not_a_segment() {
    let parts = vec![part(Some("g-svc"), 1, 1, 1, 0)];
    let mut call = outbound("c", Some("g-svc"), 1);
    call.success = false;
    let map = systems(&[("g-svc", "svc")]);
    let edges = HashMap::from([(
        ("svc".to_string(), "card-fraud".to_string()),
        "root".to_string(),
    )]);
    let p = partition(&parts, &[call], Some("root"), &map, &edges);
    assert_eq!(p.via(), vec!["svc".to_string()]);
}

#[test]
fn a_call_inside_one_system_enters_nothing() {
    let parts = vec![
        part(Some("g-a1"), 1, 1, 1, 0),
        part(Some("g-a2"), 1, 1, 1, 1),
    ];
    let calls = vec![entering("c", Some("g-a1"), 0, "g-a2")];
    let map = systems(&[("g-a1", "root"), ("g-a2", "root")]);
    let p = partition(&parts, &calls, Some("root"), &map, &HashMap::new());
    assert!(p.segments.is_empty());
}

// ── Outcome mapping ─────────────────────────────────────────────────────

fn verdict_of(f: impl FnOnce(&mut DelegationCall)) -> CallVerdict {
    let mut c = outbound("c", Some("g"), 0);
    f(&mut c);
    classify_call(&c)
}

#[test]
fn outcome_table() {
    use CallVerdict::{Held, Outcome, Refused};
    assert_eq!(verdict_of(|_| {}), Outcome(outcome::ANSWERED));
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("completed".into())),
        Outcome(outcome::ANSWERED)
    );
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("input_required".into())),
        Outcome(outcome::ANSWERED),
        "asking the caller a question is an answer"
    );
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("failed".into())),
        Outcome(outcome::FAILED)
    );
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("canceled".into())),
        Outcome(outcome::FAILED)
    );
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("rejected".into())),
        Outcome(outcome::REJECTED)
    );
    assert_eq!(
        verdict_of(|c| c.a2a_task_state = Some("auth_required".into())),
        Outcome(outcome::REJECTED)
    );
    assert_eq!(
        verdict_of(|c| c.response_body =
            Some(r#"{"jsonrpc":"2.0","error":{"code":-32001,"message":"x"}}"#.into())),
        Outcome(outcome::FAILED),
        "a 200 carrying a JSON-RPC error is not an answer"
    );
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.http_status_code = Some(502);
        }),
        Outcome(outcome::FAILED)
    );
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.http_status_code = Some(504);
        }),
        Outcome(outcome::TIMED_OUT)
    );
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.http_status_code = Some(502);
            c.error_message = Some("error sending request: operation timed out".into());
        }),
        Outcome(outcome::TIMED_OUT)
    );
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.guardrail_blocked = true;
        }),
        Refused
    );
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.argument_policy_denied = true;
        }),
        Refused
    );
    for refusal in GATEWAY_REFUSAL_SUBTYPES {
        assert_eq!(
            verdict_of(|c| {
                c.success = false;
                c.http_status_code = Some(504);
                c.proxy_subtype = Some(refusal.into());
            }),
            Refused,
            "{refusal} is the gateway's refusal, not the service failing"
        );
    }
    assert_eq!(
        verdict_of(|c| {
            c.success = false;
            c.proxy_subtype = Some("approval_required".into());
        }),
        Held
    );
}

#[test]
fn the_latest_call_decides_as_the_control_plane_reads_it() {
    let ok = outbound("a", Some("g"), 0);
    let mut timeout = outbound("b", Some("g"), 1);
    timeout.success = false;
    timeout.http_status_code = Some(504);
    let mut rejected = outbound("c", Some("g"), 2);
    rejected.a2a_task_state = Some("rejected".into());
    let mut failed = outbound("d", Some("g"), 3);
    failed.success = false;

    // A retry that answered after a timeout is answered, and vice versa.
    assert_eq!(segment_outcome(&[&timeout, &ok]).0, outcome::ANSWERED);
    assert_eq!(segment_outcome(&[&ok, &timeout]).0, outcome::TIMED_OUT);
    assert_eq!(segment_outcome(&[&failed, &rejected]).0, outcome::REJECTED);
    assert_eq!(segment_outcome(&[&rejected, &failed]).0, outcome::FAILED);
}

#[test]
fn unanswered_outcomes_match_the_control_plane() {
    // SEGMENT_UNANSWERED_OUTCOMES: rejected/blocked are answers.
    let mut u = outcome::UNANSWERED.to_vec();
    u.sort_unstable();
    assert_eq!(u, vec!["failed", "timed_out"]);
}

#[test]
fn a_hold_then_an_answer_is_answered_and_a_hold_alone_is_refused() {
    let mut held = outbound("a", Some("g"), 0);
    held.success = false;
    held.proxy_subtype = Some("approval_required".into());
    let ok = outbound("b", Some("g"), 1);
    let (o, _, refused) = segment_outcome(&[&held, &ok]);
    assert_eq!((o, refused), (outcome::ANSWERED, false));
    // Governance parked it — `failed`, refused by the gateway
    // (error_cause blocked_by_policy), never the delegate's failure.
    let (o, _, refused) = segment_outcome(&[&held]);
    assert_eq!((o, refused), (outcome::FAILED, true));
}

#[test]
fn a_segment_stopped_only_by_a_hold_records_the_hold() {
    // A hold is counted apart from a refusal, so the
    // segment names the approval request the control plane judges it by
    // (decided or inside its timeout = all good; lapsed = a warning). The
    // stored outcome stays `failed` + refused, so every older reader still
    // reads a gateway stop, never a delegate failure.
    let mut first = outbound("a", Some("g"), 0);
    first.success = false;
    first.proxy_subtype = Some("approval_required".into());
    first.approval_request_id = Some("apr-first".into());
    let mut second = first.clone();
    second.id = "b".into();
    second.at = first.at + chrono::Duration::seconds(1);
    second.approval_request_id = Some("apr-latest".into());
    assert_eq!(
        held_approval_request_id(&[&first, &second]).as_deref(),
        Some("apr-latest")
    );

    // An answer after the hold decides: not held.
    let ok = outbound("c", Some("g"), 2);
    assert_eq!(held_approval_request_id(&[&first, &ok]), None);
    // A refusal is not a hold, even after one.
    let mut refused = outbound("r", Some("g"), 3);
    refused.success = false;
    refused.proxy_subtype = Some(proxy_subtype::ACCESS_DENIED.into());
    assert_eq!(held_approval_request_id(&[&first, &refused]), None);
    assert_eq!(held_approval_request_id(&[&refused]), None);
}

#[test]
fn a_failed_call_answered_by_the_delegates_inbound_hold_is_a_hold() {
    // The service's gateway held the caller's call on its inbound side and
    // returned a refusal: the caller's row failed, the service's last act
    // was the hold. A hold — refused by governance, never the service
    // failing to answer.
    let mut call = outbound("c", Some("g-root"), 2);
    call.success = false;
    call.http_status_code = Some(403);
    let mut p = part(Some("g-svc"), 1, 0, 0, 1);
    p.last_subtype = Some("approval_required".into());
    p.last_approval_request_id = Some("apr-in".into());
    assert_eq!(inbound_hold(&[&call], Some(&p)).as_deref(), Some("apr-in"));
    // A call that answered is not a hold, whatever the service did after.
    let ok = outbound("ok", Some("g-root"), 2);
    assert_eq!(inbound_hold(&[&ok], Some(&p)), None);
    // A service whose last act was not a hold failed on its own.
    p.last_subtype = None;
    assert_eq!(inbound_hold(&[&call], Some(&p)), None);
    // A hold that came AFTER the failed call is not what the caller got.
    let mut late = part(Some("g-svc"), 1, 0, 0, 5);
    late.last_subtype = Some("approval_required".into());
    late.last_approval_request_id = Some("apr-late".into());
    assert_eq!(inbound_hold(&[&call], Some(&late)), None);
}

#[test]
fn a_segment_whose_last_act_was_a_hold_records_the_hold() {
    let mut p = part(Some("g"), 1, 0, 0, 0);
    p.last_subtype = Some("approval_required".into());
    p.last_approval_request_id = Some("apr-own".into());
    assert_eq!(own_held_approval_request_id(&p).as_deref(), Some("apr-own"));
    p.last_subtype = Some("run_aborted".into());
    assert_eq!(own_held_approval_request_id(&p), None);
}

#[test]
fn the_partition_carries_the_hold_onto_the_segment() {
    let parts = vec![
        part(Some("g-root"), 2, 1, 1, 0),
        part(Some("g-svc"), 0, 0, 0, 0),
    ];
    let mut held = entering("c1", Some("g-root"), 1, "g-svc");
    held.success = false;
    held.proxy_subtype = Some("approval_required".into());
    held.approval_request_id = Some("apr-1".into());
    let map = systems(&[("g-root", "root"), ("g-svc", "svc")]);
    let p = partition(&parts, &[held], Some("root"), &map, &HashMap::new());
    let s = &p.segments[0];
    assert_eq!((s.outcome, s.refused), (outcome::FAILED, true));
    assert_eq!(s.held_approval_request_id.as_deref(), Some("apr-1"));
}

#[test]
fn a_refused_call_is_failed_and_refused_never_a_delegate_failure() {
    let mut r = outbound("r", Some("g"), 0);
    r.success = false;
    r.http_status_code = Some(403);
    r.proxy_subtype = Some(proxy_subtype::ACCESS_DENIED.into());
    assert_eq!(segment_outcome(&[&r]), (outcome::FAILED, None, true));
    // A real failure after the refusal decides: not refused.
    let mut f = outbound("f", Some("g"), 1);
    f.success = false;
    f.http_status_code = Some(502);
    assert_eq!(segment_outcome(&[&r, &f]), (outcome::FAILED, None, false));
}

#[test]
fn the_state_is_the_last_one_the_caller_received_and_the_caller_the_first() {
    let parts = vec![
        part(Some("g-root"), 2, 1, 1, 0),
        part(Some("g-svc"), 2, 1, 1, 5),
    ];
    let mut first = entering("c1", Some("g-root"), 1, "g-svc");
    first.actor_agent_id = Some("agent-first".into());
    first.a2a_task_state = Some("working".into());
    let mut second = entering("c2", Some("g-root"), 6, "g-svc");
    second.actor_agent_id = Some("agent-second".into());
    second.a2a_task_state = Some("completed".into());
    let mut third = entering("c3", Some("g-root"), 7, "g-svc");
    // A row whose reply named no state (a plain message, or pruned).
    third.response_body = Some("<pruned>".into());
    let map = systems(&[("g-root", "root"), ("g-svc", "svc")]);
    // Out of order on purpose: the partition sorts by time.
    let p = partition(
        &parts,
        &[third, second, first],
        Some("root"),
        &map,
        &HashMap::new(),
    );
    let s = &p.segments[0];
    assert_eq!(s.caller_agent_id.as_deref(), Some("agent-first"));
    assert_eq!(s.a2a_task_state.as_deref(), Some("completed"));
    assert_eq!(s.entered_by, vec!["c1", "c2", "c3"]);
}

#[test]
fn a_segment_no_call_entered_is_judged_by_its_last_step() {
    let mut p = part(Some("g"), 1, 0, 0, 0);
    assert_eq!(own_outcome(&p), (outcome::ANSWERED, false));
    p.last_success = false;
    assert_eq!(own_outcome(&p), (outcome::FAILED, false));
    p.last_blocked = true;
    assert_eq!(own_outcome(&p), (outcome::FAILED, true));
    p.last_blocked = false;
    p.last_subtype = Some("run_aborted".into());
    assert_eq!(own_outcome(&p), (outcome::FAILED, true));
    p.last_subtype = Some("approval_required".into());
    assert_eq!(own_outcome(&p), (outcome::FAILED, true));
}

// ── Task state: one source ──────────────────────────

#[test]
fn the_task_state_is_read_from_the_column_never_from_the_body() {
    use CallVerdict::Outcome;
    // The body says failed, the column (what the gateway decided from the
    // bytes the caller received) says nothing: the ledger believes the
    // column. A ledger that re-parsed the body would read `failed`.
    assert_eq!(
        verdict_of(|c| {
            c.response_body = Some(r#"{"task":{"status":{"state":"TASK_STATE_FAILED"}}}"#.into());
            c.a2a_task_state = None;
        }),
        Outcome(outcome::ANSWERED)
    );
    // And the other way round: a pruned body cannot hide a stored state.
    assert_eq!(
        verdict_of(|c| {
            c.response_body = None;
            c.a2a_task_state = Some("rejected".into());
        }),
        Outcome(outcome::REJECTED)
    );
    let mut pruned = outbound("p", Some("g"), 0);
    pruned.response_body = None;
    pruned.a2a_task_state = Some("completed".into());
    let mut body_only = outbound("q", Some("g"), 1);
    body_only.response_body = Some(r#"{"task":{"status":{"state":"TASK_STATE_WORKING"}}}"#.into());
    assert_eq!(
        segment_outcome(&[&pruned, &body_only]).1.as_deref(),
        Some("completed"),
        "the segment's state is the last STORED state, not a re-parse"
    );
}

#[test]
fn the_ledger_does_not_parse_replies_for_a_state() {
    // Class guard: the reply parser lives in `a2a_task_state` and is called
    // by the gateway at write time. The ledger reading replies again is the
    // duplicate this phase removed.
    const SRC: &str = include_str!("run_segments.rs");
    let body = SRC.split("#[cfg(test)]").next().unwrap();
    for forbidden in ["from_reply", "of_document", "\"state\"", "normalize("] {
        assert!(
            !body.contains(forbidden),
            "run_segments.rs mentions `{forbidden}` — the task state has one source, \
             proxy_logs.a2a_task_state"
        );
    }
    assert!(
        CALLS_SQL.contains("a2a_task_state"),
        "the entering calls must carry the stored state"
    );
}

// ── Accepted-caller refusals ──────────────────────────

#[test]
fn an_unaccepted_caller_refusal_is_a_refused_failed_segment() {
    assert!(GATEWAY_REFUSAL_SUBTYPES.contains(&proxy_subtype::UNACCEPTED_CALLER));
    // The refusal row the outbound route writes: its own subtype, no
    // children (the service was never contacted), 403.
    let parts = vec![part(Some("g-root"), 2, 10, 1, 0)];
    let mut refused = outbound("call-r", Some("g-root"), 1);
    refused.proxy_subtype = Some(proxy_subtype::UNACCEPTED_CALLER.into());
    refused.success = false;
    refused.http_status_code = Some(403);
    let map = systems(&[("g-root", "sys-root")]);
    let edges = HashMap::from([(
        ("sys-root".to_string(), "card-fraud".to_string()),
        "sys-fraud".to_string(),
    )]);
    let p = partition(&parts, &[refused], Some("sys-root"), &map, &edges);
    assert_eq!(
        p.segments.len(),
        1,
        "the refused delegation names its service"
    );
    let s = &p.segments[0];
    assert_eq!(s.system_id, "sys-fraud");
    assert_eq!(s.outcome, outcome::FAILED);
    assert!(s.refused, "the gateway's refusal, not the service failing");
    assert!(s.entered_by_a2a);
    assert_eq!(s.tally, Tally::default());
    assert_eq!(p.totals(), Tally::of_parts(&parts));
}

#[test]
fn every_gateway_refusal_on_the_outbound_route_is_an_attempted_call() {
    assert_eq!(UNANSWERED_CALL_SUBTYPES[0], proxy_subtype::OUTBOUND);
    for r in GATEWAY_REFUSAL_SUBTYPES {
        assert!(UNANSWERED_CALL_SUBTYPES.contains(&r), "{r}");
    }
    // A delegation held for a person before it left is attempted too:
    // it is the delegate's segment, counted as a hold.
    assert!(UNANSWERED_CALL_SUBTYPES.contains(&proxy_subtype::APPROVAL_REQUIRED));
    assert_eq!(
        UNANSWERED_CALL_SUBTYPES.len(),
        GATEWAY_REFUSAL_SUBTYPES.len() + 2
    );
    // A shadow row is not a call attempt.
    assert!(!UNANSWERED_CALL_SUBTYPES.contains(&proxy_subtype::SEMANTIC_POLICY_SHADOW));
    assert!(!UNANSWERED_CALL_SUBTYPES.contains(&proxy_subtype::AGENT_AUTHZ_SHADOW));
}

// ── Caller system: the IMMEDIATE calling AI System ───────────────

#[test]
fn the_caller_system_is_the_immediate_caller_not_the_root() {
    // A → B → C, rooted at A.
    let parts = vec![
        part(Some("g-a"), 2, 1, 1, 0),
        part(Some("g-b"), 2, 1, 1, 2),
        part(Some("g-c"), 2, 1, 1, 5),
    ];
    let ab = entering("ab", Some("g-a"), 1, "g-b");
    let bc = entering("bc", Some("g-b"), 4, "g-c");
    let map = systems(&[("g-a", "sys-a"), ("g-b", "sys-b"), ("g-c", "sys-c")]);
    let p = partition(&parts, &[ab, bc], Some("sys-a"), &map, &HashMap::new());
    let by: HashMap<&str, &SegmentDraft> = p
        .segments
        .iter()
        .map(|s| (s.system_id.as_str(), s))
        .collect();
    assert_eq!(by["sys-b"].caller_system_id.as_deref(), Some("sys-a"));
    assert_eq!(
        by["sys-c"].caller_system_id.as_deref(),
        Some("sys-b"),
        "C's caller is B — the run's root A is root_system_id, not the caller"
    );
}

#[test]
fn a_caller_in_no_ai_system_and_a_segment_no_call_entered_have_no_caller() {
    let parts = vec![
        part(Some("g-free"), 1, 1, 1, 0),
        part(Some("g-s"), 1, 1, 1, 2),
    ];
    let call = entering("c", Some("g-free"), 1, "g-s");
    let map = systems(&[("g-s", "sys-s")]);
    let p = partition(&parts, &[call], Some("sys-root"), &map, &HashMap::new());
    assert_eq!(p.segments.len(), 1);
    assert!(
        p.segments[0].caller_system_id.is_none(),
        "the calling step is in no AI System"
    );
    // A caller group that IS the root's resolves to the root.
    let parts = vec![part(Some("g-r"), 1, 1, 1, 0), part(Some("g-s"), 1, 1, 1, 2)];
    let call = entering("c", Some("g-r"), 1, "g-s");
    let map = systems(&[("g-r", "sys-root"), ("g-s", "sys-s")]);
    let p = partition(&parts, &[call], Some("sys-root"), &map, &HashMap::new());
    assert_eq!(p.segments[0].caller_system_id.as_deref(), Some("sys-root"));
    // No entering call: nothing delegated into it that the ledger can see.
    let p = partition(
        &[part(Some("g-s"), 1, 1, 1, 0)],
        &[],
        Some("sys-root"),
        &map,
        &HashMap::new(),
    );
    assert!(p.segments[0].caller_system_id.is_none());
}

// ── Wire contract + single-source guards ────────────────────────────────

#[test]
fn outcomes_match_the_control_plane() {
    // Mirrored in app/database/models/ai_system_run_segment.py::SEGMENT_OUTCOMES
    // and its CHECK constraint.
    let mut all = outcome::ALL.to_vec();
    all.sort_unstable();
    assert_eq!(
        all,
        vec!["answered", "failed", "rejected", "timed_out"],
        "`blocked` is gone — a refusal is `failed` + blocked_by_policy"
    );
}

#[test]
fn the_run_totals_have_one_source() {
    // The conserved three must come from the parts, never from a second
    // aggregate that could disagree with them.
    let agg = run_ledger::AGGREGATE_SQL;
    for col in ["AS action_count", "AS total_tokens", "AS total_cost_usd"] {
        assert!(
            !agg.contains(col),
            "the run aggregate computes {col} itself — conservation is no \
             longer by construction"
        );
    }
    for col in ["AS action_count", "AS total_tokens", "AS cost_units"] {
        assert!(PARTS_SQL.contains(col));
    }
    assert!(PARTS_SQL.contains("GROUP BY pl.resource_group_id"));
    // The shared tally is spliced into both.
    let tally = "AS tool_error_count";
    assert!(agg.contains(tally) && PARTS_SQL.contains(tally));
}

#[test]
fn segment_holds_are_the_segments_own() {
    assert!(FINISH_SQL.contains("tar.requester_group_id = ANY(CAST($5 AS TEXT[]))"));
    assert!(FINISH_SQL.contains("tar.originating_trace_root_id = $2 AND tar.requester_group_id"));
}

#[test]
fn cost_units_are_the_column_scale() {
    assert_eq!(COST_UNITS_PER_USD, 100_000_000);
    assert!(INSERT_SQL.contains("CAST($20 AS NUMERIC) / 100000000"));
    assert!(PARTS_SQL.contains("* 100000000"));
}

// ── Property: conservation over arbitrary delegation trees ──────────────

#[derive(Debug, Clone)]
struct RowSpec {
    tokens: i64,
    cost_units: i64,
    success: bool,
}

#[derive(Debug, Clone)]
struct NodeSpec {
    parent_pick: usize,
    group_pick: usize,
    rows: Vec<RowSpec>,
    /// Outbound calls this node made that nobody answered (card picks).
    unanswered: Vec<usize>,
}

#[derive(Debug, Clone)]
struct Scenario {
    n_systems: usize,
    nodes: Vec<NodeSpec>,
    /// (caller system, card, target system) declarations.
    edges: Vec<(usize, usize, usize)>,
}

fn scenario() -> impl Strategy<Value = Scenario> {
    let row =
        (0i64..5_000, 0i64..3_000_000, any::<bool>()).prop_map(|(tokens, cost_units, success)| {
            RowSpec {
                tokens,
                cost_units,
                success,
            }
        });
    let node = (
        any::<usize>(),
        any::<usize>(),
        vec(row, 1..4),
        vec(0usize..3, 0..2),
    )
        .prop_map(|(parent_pick, group_pick, rows, unanswered)| NodeSpec {
            parent_pick,
            group_pick,
            rows,
            unanswered,
        });
    (
        1usize..=4,
        vec(node, 1..14),
        vec((0usize..4, 0usize..3, 0usize..4), 0..6),
    )
        .prop_map(|(n_systems, nodes, edges)| Scenario {
            n_systems,
            nodes,
            edges,
        })
}

#[derive(Debug, Clone)]
struct Row {
    id: String,
    group: Option<String>,
    at: i64,
    depth: i32,
    tokens: i64,
    cost_units: i64,
    success: bool,
    current: String,
    parent: Option<String>,
    card: Option<String>,
}

struct Built {
    rows: Vec<Row>,
    parts: Vec<GroupPart>,
    calls: Vec<DelegationCall>,
    group_system: HashMap<String, String>,
    card_targets: HashMap<(String, String), String>,
    root: Option<String>,
}

/// Lay the tree out as proxy_logs rows, then aggregate them the way
/// PARTS_SQL / CALLS_SQL do.
fn build(sc: &Scenario) -> Built {
    // Group universe: two groups per system, one group under no system, and
    // the NULL group.
    let mut universe: Vec<Option<String>> = Vec::new();
    let mut group_system = HashMap::new();
    for s in 0..sc.n_systems {
        for g in ["a", "b"] {
            let gid = format!("g{s}{g}");
            group_system.insert(gid.clone(), format!("sys{s}"));
            universe.push(Some(gid));
        }
    }
    universe.push(Some("free".into()));
    universe.push(None);

    let mut rows: Vec<Row> = Vec::new();
    let mut depth_of: Vec<i32> = Vec::new();
    let mut seq = 0i64;
    for (i, n) in sc.nodes.iter().enumerate() {
        let group = universe[n.group_pick % universe.len()].clone();
        let (depth, parent) = if i == 0 {
            (0, None)
        } else {
            let p = n.parent_pick % i;
            (depth_of[p] + 1, Some(format!("t{p}-0")))
        };
        depth_of.push(depth);
        for (r, spec) in n.rows.iter().enumerate() {
            seq += 1;
            rows.push(Row {
                id: format!("r{seq:04}"),
                group: group.clone(),
                at: seq,
                depth,
                tokens: spec.tokens,
                cost_units: spec.cost_units,
                success: spec.success,
                current: format!("t{i}-{r}"),
                parent: parent.clone(),
                card: None,
            });
        }
        for (k, card) in n.unanswered.iter().enumerate() {
            seq += 1;
            rows.push(Row {
                id: format!("r{seq:04}"),
                group: group.clone(),
                at: seq,
                depth,
                tokens: 0,
                cost_units: 0,
                success: false,
                current: format!("u{i}-{k}"),
                parent: parent.clone(),
                card: Some(format!("card{card}")),
            });
        }
    }

    let mut by_group: BTreeMap<Option<String>, GroupPart> = BTreeMap::new();
    for r in &rows {
        let p = by_group
            .entry(r.group.clone())
            .or_insert_with(|| GroupPart {
                group_id: r.group.clone(),
                min_depth: i32::MAX,
                first_at: t0() + Duration::seconds(r.at),
                active_from: t0() + Duration::seconds(r.at) - Duration::milliseconds(250),
                ..Default::default()
            });
        p.action_count += 1;
        p.llm_call_count += 1;
        p.error_count += i64::from(!r.success);
        p.total_tokens += r.tokens;
        p.cost_units += r.cost_units;
        p.min_depth = p.min_depth.min(r.depth);
        p.last_at = t0() + Duration::seconds(r.at);
        p.last_id = r.id.clone();
        p.last_success = r.success;
    }
    let parts: Vec<GroupPart> = by_group.into_values().collect();

    let mut calls = Vec::new();
    for r in &rows {
        let children: Vec<&Row> = rows
            .iter()
            .filter(|c| c.parent.as_deref() == Some(r.current.as_str()))
            .collect();
        let child_groups: Vec<String> = children
            .iter()
            .filter_map(|c| c.group.clone())
            .filter(|g| Some(g) != r.group.as_ref())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let unanswered = r.card.is_some() && children.is_empty();
        if child_groups.is_empty() && !unanswered {
            continue;
        }
        calls.push(DelegationCall {
            id: r.id.clone(),
            at: t0() + Duration::seconds(r.at),
            group_id: r.group.clone(),
            server_id: r.card.clone(),
            proxy_type: "a2a".into(),
            proxy_subtype: Some("outbound".into()),
            success: r.success,
            depth: r.depth,
            child_groups,
            has_children: !children.is_empty(),
            ..Default::default()
        });
    }

    let mut card_targets = HashMap::new();
    for (from, card, to) in &sc.edges {
        let (from, to) = (from % sc.n_systems, to % sc.n_systems);
        if from != to {
            card_targets.insert(
                (format!("sys{from}"), format!("card{card}")),
                format!("sys{to}"),
            );
        }
    }
    let root = rows[0]
        .group
        .as_ref()
        .and_then(|g| group_system.get(g))
        .cloned();
    Built {
        rows,
        parts,
        calls,
        group_system,
        card_targets,
        root,
    }
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 512, ..ProptestConfig::default() })]

    #[test]
    fn root_own_plus_segments_is_the_run(sc in scenario()) {
        let b = build(&sc);
        let p = partition(&b.parts, &b.calls, b.root.as_deref(), &b.group_system, &b.card_targets);

        // Conservation, on every measure the partition splits.
        prop_assert_eq!(p.totals(), Tally::of_parts(&b.parts));
        let rows_tokens: i64 = b.rows.iter().map(|r| r.tokens).sum();
        let rows_cost: i64 = b.rows.iter().map(|r| r.cost_units).sum();
        prop_assert_eq!(p.totals().action_count, b.rows.len() as i64);
        prop_assert_eq!(p.totals().total_tokens, rows_tokens);
        prop_assert_eq!(p.totals().cost_units, rows_cost);

        // Row by row: every step — delegated or not — has exactly one home,
        // and it is the home the attribution rule names.
        let home = |r: &Row| -> Option<String> {
            r.group.as_ref()
                .and_then(|g| b.group_system.get(g))
                .filter(|s| Some(*s) != b.root.as_ref())
                .cloned()
        };
        let mut expected: BTreeMap<Option<String>, (i64, i64, i64)> = BTreeMap::new();
        for r in &b.rows {
            let e = expected.entry(home(r)).or_default();
            e.0 += 1;
            e.1 += r.tokens;
            e.2 += r.cost_units;
        }
        let root_expected = expected.remove(&None).unwrap_or_default();
        prop_assert_eq!(
            (p.root_own.action_count, p.root_own.total_tokens, p.root_own.cost_units),
            root_expected
        );
        for s in &p.segments {
            let got = (s.tally.action_count, s.tally.total_tokens, s.tally.cost_units);
            let want = expected.remove(&Some(s.system_id.clone())).unwrap_or_default();
            prop_assert_eq!(got, want, "segment {} holds the wrong steps", s.system_id);
        }
        prop_assert!(
            expected.is_empty(),
            "delegated steps with no segment: {:?}", expected
        );

        // One segment per system, never the root, via derived from them.
        let ids: Vec<String> = p.segments.iter().map(|s| s.system_id.clone()).collect();
        let unique: BTreeSet<&String> = ids.iter().collect();
        prop_assert_eq!(unique.len(), ids.len());
        prop_assert!(b.root.as_ref().is_none_or(|r| !ids.contains(r)));
        prop_assert_eq!(p.via(), ids);

        for s in &p.segments {
            prop_assert!(s.first_action_at <= s.last_action_at);
            prop_assert!(s.active_from <= s.first_action_at);
            prop_assert!(active_span_ms(s) >= 0);
            prop_assert!(outcome::ALL.contains(&s.outcome));
            if s.tally.action_count == 0 {
                // A work-less segment exists only because a declared edge
                // named it as the target of a call nobody answered.
                prop_assert!(!s.entered_by.is_empty());
                prop_assert!(b.card_targets.values().any(|t| t == &s.system_id));
            }
        }
    }
}

// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the gateway's background tasks are the list in `src/tasks.rs`,
//! and `main` starts them from that list and in no other way.
//!
//! Why it matters: the list is what another distribution extends,
//! and what an operator can be told is running. A task spawned
//! straight from `main` again would be in neither.

use std::collections::HashSet;
use std::fs;

use brutor_gateway_core::tasks::BACKGROUND_TASKS;

#[path = "support/task_scan.rs"]
mod task_scan;
use task_scan::{spawn_sites, task_functions};

/// The Enterprise features a task may be tagged with (`editions.yaml`,
/// `core_gateway.enterprise_modules`).
const KNOWN_FEATURES: &[&str] = &["batch_jobs", "licence", "portal", "response_cache"];

/// Recorded from `main` as it was when it spawned the tasks itself, in the
/// order it spawned them. Changing the order or the set is a behaviour
/// change; make it on purpose.
const RECORDED: &[(&str, Option<&str>)] = &[
    // New, and first on purpose. The lease was
    // taken while the gateway was built; its renewal starts before anything
    // else does.
    ("instance_lease_renewal", None),
    ("pool_sampler", None),
    ("model_metrics_flusher", None),
    ("pool_saturation_monitor", None),
    ("api_key_usage_flusher", None),
    // New. The audit rows of refused agent
    // credentials, at most one per credential per minute.
    ("refused_agent_credential_flusher", None),
    ("oauth_token_cleanup", None),
    // The pruning of the portal's revocation rows, which
    // `oauth_token_cleanup` used to do in the same loop, is the portal's.
    // `portal_revocation_prune` stood here. It is the portal
    // extension's task now ([`RECORDED_EXTENSION_TASKS`]).
    ("video_pipeline", None),
    ("media_gc_sweep", None),
    // New: the instances that use a filesystem media store find each
    // other in Redis, so that a second one is logged (decision 11).
    ("media_filesystem_presence", None),
    ("router_state_cleanup", None),
    // `licence_check` stood here. It is the licence
    // extension's task now ([`RECORDED_EXTENSION_TASKS`]) and starts after
    // the built-in ones.
    ("cache_invalidation_listener", None),
    ("system_notify_listener", None),
    ("run_abort_listener", None),
    ("a2a_stream_listener", None),
    ("quota_counter_seed", None),
    ("usage_alert_resolver", None),
    ("admission_lease_renewal", None),
    ("evidence_witness_retry", None),
    ("tool_approval_expiry", None),
    ("skill_execution_reaper", None),
    // `batch_submitter` and `batch_poller` stood here.
    // They are the batch extension's tasks now ([`RECORDED_EXTENSION_TASKS`]).
    ("system_alert_resolver", None),
    ("run_ledger_sweeper", None),
    ("ai_system_liveness", None),
    ("behavioural_drift", None),
    ("replay_runner", None),
];

#[test]
fn the_tasks_are_these_in_this_order() {
    let actual: Vec<(&str, Option<&str>)> = BACKGROUND_TASKS
        .iter()
        .map(|task| (task.name, task.feature))
        .collect();
    assert_eq!(actual, RECORDED);
}

#[test]
fn names_are_unique_and_features_are_known() {
    let names: HashSet<&str> = BACKGROUND_TASKS.iter().map(|task| task.name).collect();
    assert_eq!(names.len(), BACKGROUND_TASKS.len());
    for task in BACKGROUND_TASKS {
        if let Some(feature) = task.feature {
            assert!(
                KNOWN_FEATURES.contains(&feature),
                "task `{}` is tagged with an unknown feature `{feature}`",
                task.name
            );
        }
    }
}

#[test]
fn the_builder_starts_the_list_and_no_task_of_its_own() {
    // `main` is `GatewayBuilder::new(config).run()`; the
    // assembly that starts the tasks is the builder's.
    let main = fs::read_to_string("src/builder.rs").unwrap();
    assert_eq!(
        main.matches("tasks::start_all(").count(),
        1,
        "main starts the background tasks once, from the list"
    );
    assert!(
        main.contains("tasks::BACKGROUND_TASKS.to_vec()"),
        "the builder starts from the list"
    );
    // The one spawn left in main is the metrics listener: a second server,
    // not a background task.
    let sites = spawn_sites(&main);
    assert_eq!(
        sites,
        vec!["tokio::spawn(async move {"],
        "the builder spawns something besides the metrics listener; a background task belongs in \
         src/tasks.rs, in BACKGROUND_TASKS"
    );
    let after_spawn = main.split("tokio::spawn(async move {").nth(1).unwrap();
    assert!(after_spawn[..120].contains("axum::serve(metrics_listener, metrics_app)"));
}

#[test]
fn every_task_function_is_listed_and_spawns() {
    let src = fs::read_to_string("src/tasks.rs").unwrap();
    let functions = task_functions(&src);
    let names: Vec<&str> = functions.iter().map(|(name, _)| *name).collect();
    let listed: Vec<&str> = BACKGROUND_TASKS.iter().map(|task| task.name).collect();
    assert_eq!(
        names, listed,
        "a task function and its list entry carry the same name, in the same order"
    );
    for (name, body) in functions {
        assert!(
            !spawn_sites(body).is_empty(),
            "task `{name}` spawns nothing: a start function must hand its work to the runtime and return"
        );
    }
}

// ── the scanners are proven against the shapes they are for ──────────────

#[test]
fn the_spawn_scanner_sees_the_shapes_main_had() {
    let before = "
    services::diagnostics::spawn_pool_sampler(
        state.db.clone(),
    );
    tokio::spawn(async move {
        loop {}
    });
    listener.spawn(state.shutdown_token.clone());
    services::pool_saturation_monitor::spawn(
    // tokio::spawn(async move { in a comment is not a spawn
    let x = respawn_count;
";
    assert_eq!(
        spawn_sites(before),
        vec![
            "services::diagnostics::spawn_pool_sampler(",
            "tokio::spawn(async move {",
            "listener.spawn(state.shutdown_token.clone());",
            "services::pool_saturation_monitor::spawn(",
        ]
    );
}

#[test]
fn the_function_scanner_reads_task_functions_only() {
    let src = "
pub fn start_all(ctx: &TaskContext<'_>, tasks: &[BackgroundTask]) {
    for task in tasks {}
}

fn one(ctx: &TaskContext<'_>) {
    tokio::spawn(async move {});
}

fn two(_ctx: &TaskContext<'_>) {
    let x = 1;
}

fn helper(n: u32) {}
";
    let functions = task_functions(src);
    assert_eq!(
        functions.iter().map(|(name, _)| *name).collect::<Vec<_>>(),
        vec!["one", "two"]
    );
    assert_eq!(spawn_sites(functions[0].1).len(), 1);
    assert!(spawn_sites(functions[1].1).is_empty());
}

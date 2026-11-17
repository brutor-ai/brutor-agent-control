// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

// `serde_json::json!` expands recursively per key; the run-facts builders
// carry ~45 keys and hit the default 128 once the subject fields joined.
#![recursion_limit = "512"]
// No `unsafe` anywhere, tests included; the compiler enforces it.
#![forbid(unsafe_code)]
// ---------------------------------------------------------------------------
// Project-wide Clippy allowances — style lints that add noise without value
// ---------------------------------------------------------------------------
#![allow(clippy::collapsible_if)]
#![allow(clippy::too_many_arguments)]
#![allow(clippy::result_large_err)]
#![allow(clippy::redundant_closure)]
#![allow(clippy::manual_map)]
#![allow(clippy::needless_borrow)]
#![allow(clippy::new_without_default)]
#![allow(clippy::type_complexity)]
#![allow(clippy::unnecessary_map_or)]
#![allow(clippy::unnecessary_filter_map)]
#![allow(clippy::double_ended_iterator_last)]
#![allow(clippy::cloned_ref_to_slice_refs)]
#![allow(clippy::manual_repeat_n)]
#![allow(clippy::io_other_error)]

//! # Brutor Agent Control Core Gateway — Library Crate
//!
//! Re-exports all internal modules so they are available to both the
//! binary entry-point (`main.rs`) and the integration test suite (`tests/`).

/// The SQL condition under which a row that records its writer in an
/// `actor_id` column is the caller's: the request's tenant (`$2`) and the
/// caller's actor id (`$3`, what `routes::request::extract_media_caller`
/// names the caller: the end user, or the resource group a key without an
/// end user or an agent acts in). A row that records no writer matches no
/// caller. Every by-id read, update and delete of such a row (a file, a
/// video job) uses this one condition, so a row that is another caller's
/// and a row that does not exist are the same answer.
#[macro_export]
macro_rules! written_by_caller_sql {
    () => {
        "tenant_id = $2 AND actor_id = $3"
    };
}

/// The SQL predicate "this `llm_model` row may be called now", for a
/// `WHERE` clause. `$alias` is the row's table alias with its dot (`"m."`),
/// or `""` for an unaliased `FROM llm_model`.
///
/// A model is available when all of these hold:
///
/// * it is enabled;
/// * it is not retired: it has no `retire_date`, or that date is still in
///   the future (UTC). From its retire date on, a model is not called;
/// * its provider record is enabled. A routing group's virtual row
///   (`mode = 'routing'`) stands for no provider of its own: its members
///   are judged one by one.
///
/// EVERY query that loads a model in order to call it, to offer it or to
/// pick it (a direct call, a routing-group member, a fallback, a judge, a
/// model list) uses this predicate, so a model that is not available is
/// answered exactly as a disabled one is and is never a candidate.
/// `tests/model_availability_guard.rs` lists every `llm_model` query and
/// refuses one that filters on `enabled` by hand.
#[macro_export]
macro_rules! model_available_sql {
    ($alias:literal) => {
        concat!(
            $alias,
            "enabled = true AND (",
            $alias,
            "retire_date IS NULL OR ",
            $alias,
            "retire_date > (now() AT TIME ZONE 'UTC')::date) AND (",
            $alias,
            "mode = 'routing' OR EXISTS (SELECT 1 FROM llm_providers available_provider \
             WHERE available_provider.id = ",
            $alias,
            "provider_id AND available_provider.enabled = true))"
        )
    };
}

pub mod app;
pub mod builder;
pub mod config;
pub mod constants;
pub mod env_registry;
pub mod env_vars;
pub mod middleware;
pub mod models;
pub mod openapi;
pub mod routes;
pub mod services;
pub mod tasks;

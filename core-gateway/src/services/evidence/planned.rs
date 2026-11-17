// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Two-phase actions: a `planned` record at dispatch,
//! confirmed by the outcome record.
//!
//! For an AI System whose profile sets `two_phase_actions` (default: any
//! high-risk classification), every MCP tool call, A2A send and skill
//! execute produces two records under one `action_id`: `planned` when the
//! gates have accepted it and the upstream call is about to happen, and the
//! ordinary outcome record — chained `confirms` → planned — once the row
//! lands. A planned record with no outcome is the visible may/did gap a
//! crash mid-dispatch leaves behind; that is the point of sealing it first.
//!
//! # No database on the request path
//!
//! [`dispatching`] is synchronous and allocation-cheap: it reserves the
//! outcome row's id in the request scope (so both records share
//! `<surface>/<id>`), registers a slot in a process-wide map, and hands the
//! rest — options lookup (cached), build and [`store::seal`] — to a spawned
//! task that owns the slot's lock from the moment it exists.
//!
//! # Ordering
//!
//! The writer's [`confirmed_by`] waits on that lock, so by the time the
//! outcome is minted the planned record is either committed to the log
//! (and the outcome chains to it, landing strictly after it) or known to
//! have failed (and the outcome is sealed unchained, counted as a capture
//! gap). The two can never land in the wrong order.

use crate::services::evidence::mint::{self, PlannedAction};
use crate::services::evidence::{attribution, key, metrics, request, store};
use moka::sync::Cache;
use sqlx::PgPool;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use tokio::sync::Mutex;

/// Upper bound the writer waits for an in-flight planned seal before it
/// seals the outcome unchained. A seal is one short transaction.
const CLAIM_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq, Eq)]
enum Slot {
    /// The spawned task has not finished (it holds the lock meanwhile).
    Resolving,
    /// The system does not ask for two-phase records.
    NotPlanned,
    Sealed(String),
    Failed,
}

type Entry = Arc<Mutex<Slot>>;
type SlotKey = (String, String, String);

fn slots() -> &'static Cache<SlotKey, Entry> {
    static C: OnceLock<Cache<SlotKey, Entry>> = OnceLock::new();
    C.get_or_init(|| {
        Cache::builder()
            .max_capacity(200_000)
            // Outlives any sane tool call; a longer one falls back to the
            // indexed `(tenant_id, verdict)` lookup in `confirmed_by`.
            .time_to_live(Duration::from_secs(3600))
            .build()
    })
}

fn db_key(pool: &PgPool) -> String {
    pool.connect_options()
        .get_database()
        .unwrap_or("")
        .to_string()
}

/// Evidence surface of a proxy type (`mcp` / `a2a` / `skill`).
fn surface_for(proxy_type: &str) -> Option<&'static str> {
    use crate::constants::proxy;
    match proxy_type {
        proxy::MCP | proxy::VIRTUAL_MCP => Some("mcp"),
        proxy::A2A => Some("a2a"),
        proxy::SKILL => Some("skill"),
        _ => None,
    }
}

/// The gates accepted an action on `proxy_type` and it is about to be
/// dispatched. Call once, immediately before the upstream call, from inside
/// the request (the outcome row must be built in the same request scope).
/// A no-op when evidence is off or outside a request scope.
pub fn dispatching(
    pool: &PgPool,
    tenant_id: &str,
    rg_id: Option<&str>,
    proxy_type: &str,
    effect_type: impl Into<String>,
    root_task_id: Option<String>,
) {
    if !key::is_enabled() {
        return;
    }
    let Some(surface) = surface_for(proxy_type) else {
        return;
    };
    let Some(log_id) = request::reserve_log_id(proxy_type) else {
        return;
    };
    let action_id = format!("{surface}/{log_id}");
    let entry: Entry = Arc::new(Mutex::new(Slot::Resolving));
    // Taken before the slot is published, so a claimant can never observe
    // `Resolving` without waiting for the task that will resolve it.
    let Ok(mut guard) = entry.clone().try_lock_owned() else {
        return;
    };
    slots().insert(
        (db_key(pool), tenant_id.to_string(), action_id.clone()),
        entry,
    );
    let planned = PlannedAction {
        tenant_id: tenant_id.to_string(),
        surface,
        proxy_log_id: log_id,
        effect_type: effect_type.into(),
        root_task_id,
        consumed_approval_request_id: crate::middleware::run_end::consumed_approval(),
        timestamp: chrono::Utc::now(),
    };
    let pool = pool.clone();
    let rg = rg_id.map(str::to_owned);
    tokio::spawn(async move {
        *guard = seal_planned(&pool, rg.as_deref(), &planned).await;
    });
}

async fn seal_planned(pool: &PgPool, rg_id: Option<&str>, p: &PlannedAction) -> Slot {
    // A lookup error is not "this system does not plan": the slot
    // fails, so the outcome lands unconfirmed and is counted.
    let (opts, attr) = match (
        attribution::options(pool, &p.tenant_id, rg_id).await,
        attribution::resolve(pool, &p.tenant_id, rg_id).await,
    ) {
        (Ok(o), Ok(a)) => (o, a),
        (Err(e), _) | (_, Err(e)) => {
            tracing::error!(
                target = "brutor::evidence",
                tenant_id = %p.tenant_id,
                error = %e,
                "planned record lookups failed; the outcome will land unconfirmed",
            );
            return Slot::Failed;
        }
    };
    if !opts.two_phase_actions {
        return Slot::NotPlanned;
    }
    let minted = match mint::build_planned(p, attr.as_deref()) {
        Ok(m) => m,
        Err(e) => {
            tracing::error!(
                target = "brutor::evidence",
                error = %e,
                "planned record could not be built",
            );
            return Slot::Failed;
        }
    };
    match store::seal(pool, &p.tenant_id, std::slice::from_ref(&minted)).await {
        Ok(sealed) => sealed
            .into_iter()
            .next()
            .map(|s| Slot::Sealed(s.record_id))
            .unwrap_or(Slot::Failed),
        Err(e) => {
            tracing::warn!(
                target = "brutor::evidence",
                tenant_id = %p.tenant_id,
                error = %e,
                "planned record not sealed; the outcome will land unconfirmed",
            );
            Slot::Failed
        }
    }
}

/// The planned record the outcome of `action_id` confirms, if one was
/// sealed. Called by the writer; waits (bounded) for an in-flight seal.
/// `expect_planned` = the row's system asks for two-phase records, which
/// is what justifies the indexed fallback lookup on a slot miss (another
/// replica restarted, or a call that outlived the slot).
pub async fn confirmed_by(
    pool: &PgPool,
    tenant_id: &str,
    action_id: &str,
    expect_planned: bool,
) -> Option<String> {
    let key = (db_key(pool), tenant_id.to_string(), action_id.to_string());
    if let Some(entry) = slots().remove(&key) {
        return match tokio::time::timeout(CLAIM_TIMEOUT, entry.lock()).await {
            Ok(slot) => match &*slot {
                Slot::Sealed(id) => Some(id.clone()),
                Slot::NotPlanned => None,
                Slot::Resolving | Slot::Failed => {
                    metrics::capture_gap(metrics::gap_reason::PLANNED_UNCONFIRMED, 1);
                    None
                }
            },
            Err(_) => {
                metrics::capture_gap(metrics::gap_reason::PLANNED_UNCONFIRMED, 1);
                None
            }
        };
    }
    if !expect_planned {
        return None;
    }
    match sqlx::query_scalar(
        "SELECT record_id FROM action_records \
         WHERE tenant_id = $1 AND verdict = 'planned' AND record->>'action_id' = $2 \
         ORDER BY seq ASC LIMIT 1",
    )
    .bind(tenant_id)
    .bind(action_id)
    .fetch_optional(pool)
    .await
    {
        Ok(found) => found,
        Err(e) => {
            // Counted like any unconfirmed outcome, and logged.
            tracing::error!(target = "brutor::evidence", error = %e, "planned record lookup failed");
            metrics::capture_gap(metrics::gap_reason::PLANNED_UNCONFIRMED, 1);
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_action_surfaces_are_two_phase() {
        assert_eq!(surface_for("mcp"), Some("mcp"));
        assert_eq!(surface_for("virtual-mcp"), Some("mcp"));
        assert_eq!(surface_for("a2a"), Some("a2a"));
        assert_eq!(surface_for("skill"), Some("skill"));
        assert_eq!(surface_for("llm"), None);
        assert_eq!(surface_for("oauth"), None);
    }

    /// Same never-enters rule as `mint.rs`: this module builds a record and
    /// must never read identity off the request.
    #[test]
    fn planned_never_reads_a_never_enters_field() {
        const SRC: &str = include_str!("planned.rs");
        let body = SRC.split("#[cfg(test)]").next().unwrap();
        for forbidden in [
            "subject_id",
            "api_key_id",
            "end_user_session_id",
            "gateway_user_id",
            "idp_user_email",
            "client_ip",
            "user_agent",
            "request_body_preview",
            "response_body_preview",
            "actor_agent_id",
        ] {
            assert!(!body.contains(forbidden), "{forbidden} in planned.rs");
        }
    }
}

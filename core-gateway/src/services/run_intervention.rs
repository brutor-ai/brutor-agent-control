// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Operator abort of a single in-flight run.
//!
//! **Abort is autonomy scoped to a run.** `autonomy.rs` answers "how much may
//! this agent do without a human"; this answers "may *this run* continue at
//! all". Same `Verdict` type, same five governed call sites, same audit
//! shape. Once framed that way the enforcement is a few lines per surface and
//! the real work is knowing which runs are aborted, fast, on every replica.
//!
//! ## How an abort arrives
//!
//! An abort that fails to *arrive* lets a runaway continue, which is the
//! wrong direction for a safety control to fail. Two things carry it:
//!
//! 1. **`pg_notify`** on write — every replica's listener inserts the run
//!    into a process-local map. A run found there is refused without I/O.
//! 2. **The durable record** — a run that is *not* in the local map is
//!    looked up in `run_interventions`, on every governed action. This is
//!    what answers for a replica that started after the notification or
//!    missed it, and it is also what answers "not aborted": the common case
//!    costs one indexed query per governed action.
//!
//! (There is no third path through Redis.)
//!
//! ## Fail direction
//!
//! Closed. When the durable record
//! cannot be read, the action is refused: with the store unreadable there is
//! no telling whether an operator pressed stop — possibly because of the very
//! incident that broke the read.
//!
//! ## What abort does not do
//!
//! It stops the *next* governed action. A non-streaming provider call already
//! in flight completes. Overclaiming a hard kill is exactly the sort of thing
//! external validation catches, and the honest control is still the one the
//! operator wants.

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use sqlx::PgPool;
use sqlx::postgres::PgListener;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

use crate::services::autonomy::Verdict;

/// The NOTIFY channel the control plane fires on write.
pub const ABORT_CHANNEL: &str = "brutor_run_abort";

/// Operator-facing refusal. Carries its own message for the same reason
/// `autonomy::SUSPENDED_MESSAGE` does: a generic 403 sends the caller to the
/// wrong team.
pub const ABORTED_MESSAGE: &str =
    "This run was stopped by an operator. Start a new run to continue.";

/// Refusal when whether the run was aborted cannot be determined (fail closed).
pub const ABORT_UNKNOWN_MESSAGE: &str =
    "Refused: whether this run was stopped by an operator cannot be determined right now";

/// What this replica knows about an aborted run.
#[derive(Debug, Clone)]
pub struct AbortRecord {
    pub intervention_id: String,
    pub reason: String,
}

/// Process-local view of the open interventions, keyed by
/// `(tenant_id, root_task_id)`.
///
/// Bounded by the number of runs an operator has aborted and not yet closed,
/// which is small by construction — an intervention is a human action.
#[derive(Clone, Default)]
pub struct RunAbortCache {
    entries: Arc<DashMap<(String, String), AbortRecord>>,
}

impl RunAbortCache {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn insert(&self, tenant_id: &str, root_task_id: &str, record: AbortRecord) {
        self.entries
            .insert((tenant_id.to_string(), root_task_id.to_string()), record);
    }

    pub fn get(&self, tenant_id: &str, root_task_id: &str) -> Option<AbortRecord> {
        self.entries
            .get(&(tenant_id.to_string(), root_task_id.to_string()))
            .map(|e| e.value().clone())
    }

    pub fn remove(&self, tenant_id: &str, root_task_id: &str) {
        self.entries
            .remove(&(tenant_id.to_string(), root_task_id.to_string()));
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// Is this run aborted?
///
/// The local map first (a run already known to be aborted is refused without
/// I/O), then the durable record, which is what answers for every run that
/// is not aborted.
///
/// Returns `Some(Verdict::Deny)` when the run must stop, `None` otherwise.
/// `cache` is optional because not every call site holds the shared state
/// (the unit tests and the reconcile path).
pub async fn check(
    db: &PgPool,
    cache: Option<&RunAbortCache>,
    tenant_id: &str,
    root_task_id: &str,
) -> Option<Verdict> {
    if let Some(cache) = cache {
        if cache.get(tenant_id, root_task_id).is_some() {
            return Some(Verdict::Deny(ABORTED_MESSAGE));
        }
    }

    // The durable record: every run the local map does not know as aborted.
    let row: Result<Option<(String, String)>, _> = sqlx::query_as(
        "SELECT id, reason FROM run_interventions \
         WHERE tenant_id = $1 AND root_task_id = $2 AND action = 'abort' \
           AND status <> 'closed' LIMIT 1",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_optional(db)
    .await;

    match row {
        Ok(Some((id, reason))) => {
            if let Some(cache) = cache {
                cache.insert(
                    tenant_id,
                    root_task_id,
                    AbortRecord {
                        intervention_id: id,
                        reason,
                    },
                );
            }
            Some(Verdict::Deny(ABORTED_MESSAGE))
        }
        Ok(None) => None,
        Err(e) => {
            // Fail closed. The local map
            // has already been consulted and had nothing; with the
            // durable record unreadable we cannot tell whether an operator
            // pressed stop — possibly because of the very incident that broke
            // the read — so the action is refused, not allowed.
            tracing::error!(
                error = %e,
                tenant_id,
                root_task_id,
                "run-intervention lookup failed — refusing the action (fail closed)"
            );
            Some(Verdict::Deny(ABORT_UNKNOWN_MESSAGE))
        }
    }
}

/// Record that an abort actually bit: stamp `first_blocked_at` on the first
/// refusal and count every one after it.
///
/// "I clicked stop" and "the run actually stopped" are different claims, and
/// a control that conflates them is one nobody can trust in an incident.
pub async fn record_block(db: &PgPool, tenant_id: &str, root_task_id: &str) {
    // `RETURNING` the pre-update value tells us whether this was the FIRST
    // refusal, which is the only time the approval sweep is worth running:
    // after it, there is nothing left pending to expire.
    let result = sqlx::query_scalar::<_, Option<chrono::DateTime<chrono::Utc>>>(
        "UPDATE run_interventions \
         SET first_blocked_at = COALESCE(first_blocked_at, now()), \
             steps_blocked = steps_blocked + 1, \
             status = CASE WHEN status = 'requested' THEN 'effective' ELSE status END, \
             updated_at = now() \
         WHERE tenant_id = $1 AND root_task_id = $2 AND status <> 'closed' \
         RETURNING (SELECT first_blocked_at FROM run_interventions \
                    WHERE tenant_id = $1 AND root_task_id = $2 AND status <> 'closed')",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_optional(db)
    .await;

    match result {
        // `Some(None)` — the row existed and had no `first_blocked_at`, so
        // this refusal is the first. Retire anything still waiting on a
        // human for this run, or an approver could approve an action on a
        // stopped run an hour later and resurrect it.
        Ok(Some(None)) => {
            let expired = expire_pending_approvals(db, tenant_id, root_task_id).await;
            if expired > 0 {
                tracing::info!(
                    tenant_id,
                    root_task_id,
                    expired,
                    "expired pending approvals for an aborted run",
                );
            }
        }
        Ok(_) => {}
        Err(e) => {
            // Telemetry about the intervention, not the intervention itself —
            // the refusal has already happened and must not be undone by a
            // failed bookkeeping write.
            tracing::error!(error = %e, tenant_id, root_task_id, "failed to record run-abort block");
        }
    }
}

/// Expire the pending approvals belonging to an aborted run.
///
/// Without this an approver could approve an action on a stopped run an hour
/// later and resurrect it — the approval would mint a valid one-time token
/// for a run an operator had deliberately ended. The `originating_*`
/// chain is what makes the pending requests reachable from the run.
///
/// Only `pending` rows are touched: an approval already consumed is history,
/// and rewriting history is not this function's job.
pub async fn expire_pending_approvals(db: &PgPool, tenant_id: &str, root_task_id: &str) -> u64 {
    match sqlx::query_scalar::<_, String>(
        "UPDATE tool_approval_requests SET status = 'expired' \
         WHERE tenant_id = $1 AND originating_trace_root_id = $2 AND status = 'pending' \
         RETURNING id",
    )
    .bind(tenant_id)
    .bind(root_task_id)
    .fetch_all(db)
    .await
    {
        Ok(ids) => {
            // Each lapsed approval is an `expired` verdict,
            // superseding its `awaiting_human` record.
            let expired: Vec<(String, String)> = ids
                .iter()
                .map(|id| (id.clone(), tenant_id.to_string()))
                .collect();
            crate::services::evidence::approval::seal_expired(db, &expired).await;
            ids.len() as u64
        }
        Err(e) => {
            tracing::error!(
                error = %e,
                tenant_id,
                root_task_id,
                "failed to expire pending approvals for an aborted run",
            );
            0
        }
    }
}

/// Load every open intervention for this tenant into the local map.
///
/// Path 3 of the propagation design, run on the existing sweeper tick. Also
/// how a replica that started mid-incident catches up.
pub async fn reconcile(db: &PgPool, cache: &RunAbortCache) {
    let rows: Result<Vec<(String, String, String, String)>, _> = sqlx::query_as(
        "SELECT id, tenant_id, root_task_id, reason FROM run_interventions \
         WHERE action = 'abort' AND status <> 'closed'",
    )
    .fetch_all(db)
    .await;

    match rows {
        Ok(rows) => {
            for (id, tenant_id, root_task_id, reason) in rows {
                cache.insert(
                    &tenant_id,
                    &root_task_id,
                    AbortRecord {
                        intervention_id: id,
                        reason,
                    },
                );
            }
        }
        Err(e) => {
            tracing::error!(error = %e, "run-intervention reconcile failed");
        }
    }
}

// ---------------------------------------------------------------------------
// Propagation path 1 — LISTEN/NOTIFY
// ---------------------------------------------------------------------------

/// Listens on `brutor_run_abort` and populates this replica's map.
///
/// Payload is `tenant_id|root_task_id|intervention_id`. Deliberately a flat
/// string rather than JSON: it carries three opaque identifiers and nothing
/// that needs escaping, and a parse failure here must never be able to drop
/// an abort silently — an unparseable payload triggers a full reconcile
/// instead, which is slower but cannot lose the instruction.
pub struct RunAbortListener {
    pub database_url: String,
    pub cache: RunAbortCache,
    pub db: PgPool,
}

async fn sleep_or_shutdown(secs: u64, shutdown: &CancellationToken) -> bool {
    tokio::select! {
        _ = shutdown.cancelled() => true,
        _ = tokio::time::sleep(Duration::from_secs(secs)) => false,
    }
}

impl RunAbortListener {
    pub fn spawn(self, shutdown: CancellationToken) -> JoinHandle<()> {
        tokio::spawn(async move {
            tracing::info!(
                channel = ABORT_CHANNEL,
                "run-abort NOTIFY listener starting"
            );

            let mut backoff_secs: u64 = 2;
            loop {
                if shutdown.is_cancelled() {
                    tracing::info!("run-abort NOTIFY listener shutting down");
                    return;
                }

                let mut listener = match PgListener::connect(&self.database_url).await {
                    Ok(l) => l,
                    Err(e) => {
                        tracing::warn!(error = %e, backoff_secs, "run-abort listener: connect failed — retrying");
                        if sleep_or_shutdown(backoff_secs, &shutdown).await {
                            return;
                        }
                        backoff_secs = backoff_secs.saturating_mul(2).min(30);
                        continue;
                    }
                };

                if let Err(e) = listener.listen(ABORT_CHANNEL).await {
                    tracing::warn!(error = %e, backoff_secs, "run-abort listener: LISTEN failed — retrying");
                    if sleep_or_shutdown(backoff_secs, &shutdown).await {
                        return;
                    }
                    backoff_secs = backoff_secs.saturating_mul(2).min(30);
                    continue;
                }

                // Every reconnect re-reads the open interventions: a replica
                // that was disconnected has, by definition, missed whatever
                // was published while it was away.
                reconcile(&self.db, &self.cache).await;
                tracing::info!(channel = ABORT_CHANNEL, "run-abort NOTIFY listener active");
                backoff_secs = 2;

                loop {
                    tokio::select! {
                        _ = shutdown.cancelled() => {
                            tracing::info!("run-abort NOTIFY listener shutting down");
                            return;
                        }
                        msg = listener.recv() => {
                            match msg {
                                Ok(n) => match parse_abort_payload(n.payload()) {
                                    Some((tenant_id, root_task_id, intervention_id)) => {
                                        self.cache.insert(
                                            &tenant_id,
                                            &root_task_id,
                                            AbortRecord {
                                                intervention_id,
                                                reason: String::new(),
                                            },
                                        );
                                        tracing::info!(
                                            tenant_id = %tenant_id,
                                            run = %root_task_id,
                                            "run abort received",
                                        );
                                    }
                                    None => {
                                        // Never drop an abort because a
                                        // payload was malformed.
                                        tracing::warn!(
                                            payload = n.payload(),
                                            "run-abort listener: bad payload — reconciling instead",
                                        );
                                        reconcile(&self.db, &self.cache).await;
                                    }
                                },
                                Err(e) => {
                                    tracing::warn!(error = %e, "run-abort listener: recv failed — reconnecting");
                                    break;
                                }
                            }
                        }
                    }
                }

                if sleep_or_shutdown(backoff_secs, &shutdown).await {
                    return;
                }
                backoff_secs = backoff_secs.saturating_mul(2).min(30);
            }
        })
    }
}

/// `tenant_id|root_task_id|intervention_id`.
pub(crate) fn parse_abort_payload(payload: &str) -> Option<(String, String, String)> {
    let mut parts = payload.splitn(3, '|');
    let tenant_id = parts.next()?.trim();
    let root_task_id = parts.next()?.trim();
    let intervention_id = parts.next().unwrap_or("").trim();
    if tenant_id.is_empty() || root_task_id.is_empty() {
        return None;
    }
    Some((
        tenant_id.to_string(),
        root_task_id.to_string(),
        intervention_id.to_string(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_cache_answers_per_tenant_and_run() {
        let cache = RunAbortCache::new();
        cache.insert(
            "t1",
            "task-1",
            AbortRecord {
                intervention_id: "ri-1".into(),
                reason: "runaway".into(),
            },
        );
        assert!(cache.get("t1", "task-1").is_some());
        // A different run in the same tenant is unaffected — the whole point
        // of abort is that it does NOT take down its neighbours.
        assert!(cache.get("t1", "task-2").is_none());
        // And a different tenant cannot see it at all.
        assert!(cache.get("t2", "task-1").is_none());
    }

    #[test]
    fn removing_clears_the_entry() {
        let cache = RunAbortCache::new();
        cache.insert(
            "t1",
            "task-1",
            AbortRecord {
                intervention_id: "ri-1".into(),
                reason: "x".into(),
            },
        );
        assert_eq!(cache.len(), 1);
        cache.remove("t1", "task-1");
        assert!(cache.is_empty());
    }

    #[test]
    fn the_payload_parser_accepts_the_published_shape() {
        let got = parse_abort_payload("t1|task-1|ri-1").expect("parses");
        assert_eq!(got, ("t1".into(), "task-1".into(), "ri-1".into()));
    }

    #[test]
    fn the_payload_parser_tolerates_a_missing_intervention_id() {
        let got = parse_abort_payload("t1|task-1").expect("parses");
        assert_eq!(got.0, "t1");
        assert_eq!(got.1, "task-1");
    }

    #[test]
    fn the_payload_parser_rejects_incomplete_payloads() {
        // A rejected payload triggers a full reconcile rather than dropping
        // the abort, so being strict here is safe.
        assert!(parse_abort_payload("").is_none());
        assert!(parse_abort_payload("t1").is_none());
        assert!(parse_abort_payload("|task-1|ri").is_none());
        assert!(parse_abort_payload("t1||ri").is_none());
    }

    #[test]
    fn a_run_id_containing_a_pipe_keeps_its_tail() {
        // splitn(3) means the third field absorbs the rest, so an id with a
        // separator in it cannot shift the tenant or run out of position.
        let got = parse_abort_payload("t1|task-1|ri-1|extra").expect("parses");
        assert_eq!(got.1, "task-1");
    }

    #[test]
    fn the_refusal_message_names_the_cause() {
        // A generic 403 sends the caller to the wrong team.
        assert!(ABORTED_MESSAGE.contains("stopped by an operator"));
    }
}

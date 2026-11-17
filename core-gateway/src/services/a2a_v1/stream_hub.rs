// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # A2A task-update stream hub + NOTIFY listener
//!
//! Drives event-based SSE streaming for A2A tasks
//! (`message/stream`, `tasks/resubscribe`). Without this, `build_task_stream`
//! re-reads the task row every 500ms — adding up to half a second of latency
//! to every state transition and a steady DB read per open stream.
//!
//! ## How a wake propagates
//!
//! 1. Any task write goes through `task_store::write`, which fires
//!    `pg_notify('a2a_task_update', {tenant_id, task_id})` after the row lands.
//! 2. On **every** gateway-core replica, an [`A2aStreamListener`] owns a
//!    dedicated `PgListener` on that channel. When a NOTIFY arrives it calls
//!    [`A2aStreamHub::notify`].
//! 3. The hub wakes the in-process broadcast channel for that task; every
//!    `build_task_stream` subscribed to it re-reads the task and emits any new
//!    frames.
//!
//! Postgres LISTEN/NOTIFY is what makes this work across replicas: the pod
//! serving the SSE stream is usually NOT the pod that ran the task, so an
//! in-process signal alone wouldn't reach it. (This mirrors `system_notify.rs`.)
//!
//! ## Why the stream still polls (as a safety net)
//!
//! NOTIFY is best-effort: a notification fired while a replica's listener is
//! mid-reconnect is lost, and an SSE stream that subscribes a hair after a
//! write misses that edge. So `build_task_stream` keeps a *long-interval*
//! reconcile tick (seconds, not 500ms) alongside the event wake — correctness
//! never depends on a NOTIFY arriving, only latency does.

use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use serde::{Deserialize, Serialize};
use sqlx::postgres::PgListener;
use tokio::sync::broadcast;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

/// Postgres LISTEN/NOTIFY channel for A2A task updates. Wire protocol shared by
/// the publisher (`task_store::write`) and every replica's listener.
pub const A2A_TASK_UPDATE_CHANNEL: &str = "a2a_task_update";

/// Broadcast buffer per task. A stream only ever needs "something changed" —
/// it always re-reads the latest row — so a lagging receiver coalescing
/// multiple wakes into one re-read is harmless.
const WAKE_CHANNEL_CAPACITY: usize = 16;

/// Tiny NOTIFY payload. The stream re-reads the full task from the DB on wake,
/// so we ship only enough to route to the right subscribers.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TaskUpdateNotice {
    pub tenant_id: String,
    pub task_id: String,
}

/// In-process registry of SSE subscribers waiting on task updates.
///
/// Keyed by `"{tenant_id}:{task_id}"`; each key maps to a broadcast channel
/// every active stream for that task holds a `Receiver` on. Empty entries are
/// GC'd lazily on the next `notify` for that key.
#[derive(Default)]
pub struct A2aStreamHub {
    subscribers: DashMap<String, broadcast::Sender<()>>,
}

impl A2aStreamHub {
    pub fn new() -> Self {
        Self {
            subscribers: DashMap::new(),
        }
    }

    fn key(tenant_id: &str, task_id: &str) -> String {
        format!("{tenant_id}:{task_id}")
    }

    /// Subscribe a stream to wakeups for a task. The returned receiver yields
    /// `()` each time the task is written (locally or relayed via NOTIFY).
    pub fn subscribe(&self, tenant_id: &str, task_id: &str) -> broadcast::Receiver<()> {
        let key = Self::key(tenant_id, task_id);
        let entry = self
            .subscribers
            .entry(key)
            .or_insert_with(|| broadcast::channel(WAKE_CHANNEL_CAPACITY).0);
        entry.subscribe()
    }

    /// Drop the entry for a task if nobody is listening any more.
    ///
    /// The map only ever shed entries inside `notify`, when a
    /// send found zero receivers. A stream that ends without a further NOTIFY
    /// for that task — the normal case: it reached a terminal state, or hit
    /// the 300-second cap, or the client walked away — left its sender behind
    /// for the life of the process. One leaked entry per streamed task is a
    /// slow, quiet climb that only shows up as memory on a long-lived replica.
    ///
    /// Removal is conditional on the receiver count so a subscriber racing in
    /// between the check and the remove is not silently unhooked.
    pub fn release(&self, tenant_id: &str, task_id: &str) {
        let key = Self::key(tenant_id, task_id);
        self.subscribers
            .remove_if(&key, |_, tx| tx.receiver_count() == 0);
    }

    /// Subscribe, and get a guard that releases the entry when it drops.
    ///
    /// The stream task has several early returns (client
    /// gone, terminal state, time cap), so a release call at the end of the
    /// function would miss most of the ways a stream actually ends. Tying it
    /// to a drop is the only version that cannot be forgotten.
    pub fn subscribe_guarded(
        self: &Arc<Self>,
        tenant_id: &str,
        task_id: &str,
    ) -> (broadcast::Receiver<()>, StreamSubscription) {
        let rx = self.subscribe(tenant_id, task_id);
        (
            rx,
            StreamSubscription {
                hub: Arc::clone(self),
                tenant_id: tenant_id.to_string(),
                task_id: task_id.to_string(),
            },
        )
    }

    /// Wake every local subscriber for a task. No-op when nobody on this
    /// replica is streaming it; GCs the stale entry in that case.
    pub fn notify(&self, tenant_id: &str, task_id: &str) {
        let key = Self::key(tenant_id, task_id);
        // `send` errors only when there are zero receivers.
        let stale = match self.subscribers.get(&key) {
            Some(tx) => tx.send(()).is_err(),
            None => false,
        };
        if stale {
            // Remove only if STILL empty — guards a subscribe() racing in
            // between the send above and this remove.
            self.subscribers
                .remove_if(&key, |_, tx| tx.receiver_count() == 0);
        }
    }

    /// Number of distinct tasks with at least one tracked subscriber entry.
    /// Test/observability only.
    pub fn tracked_tasks(&self) -> usize {
        self.subscribers.len()
    }
}

/// Per-pod listener that relays `a2a_task_update` NOTIFYs into the local hub.
///
/// Mirrors [`crate::services::system_notify::SystemNotifyListener`]: owns a
/// dedicated `PgListener` (a listener connection can't be shared with the
/// query pool) and reconnects with 2s→30s exponential backoff so a transient
/// Postgres blip doesn't permanently sever event delivery — streams fall back
/// to their reconcile tick until it recovers.
pub struct A2aStreamListener {
    pub database_url: String,
    pub hub: Arc<A2aStreamHub>,
}

impl A2aStreamListener {
    pub fn spawn(self, shutdown: CancellationToken) -> JoinHandle<()> {
        tokio::spawn(async move {
            tracing::info!(
                channel = A2A_TASK_UPDATE_CHANNEL,
                "A2A task-update NOTIFY listener starting"
            );

            let mut backoff_secs: u64 = 2;
            loop {
                if shutdown.is_cancelled() {
                    tracing::info!("A2A task-update NOTIFY listener shutting down");
                    return;
                }

                let mut listener = match PgListener::connect(&self.database_url).await {
                    Ok(l) => l,
                    Err(e) => {
                        tracing::warn!(error = %e, backoff_secs, "A2A listener: PgListener connect failed — retrying");
                        if sleep_or_shutdown(backoff_secs, &shutdown).await {
                            return;
                        }
                        backoff_secs = backoff_secs.saturating_mul(2).min(30);
                        continue;
                    }
                };

                if let Err(e) = listener.listen(A2A_TASK_UPDATE_CHANNEL).await {
                    tracing::warn!(error = %e, backoff_secs, "A2A listener: LISTEN failed — retrying");
                    if sleep_or_shutdown(backoff_secs, &shutdown).await {
                        return;
                    }
                    backoff_secs = backoff_secs.saturating_mul(2).min(30);
                    continue;
                }

                tracing::info!(
                    channel = A2A_TASK_UPDATE_CHANNEL,
                    "A2A task-update NOTIFY listener active"
                );
                backoff_secs = 2;

                loop {
                    tokio::select! {
                        _ = shutdown.cancelled() => {
                            tracing::info!("A2A task-update NOTIFY listener shutting down");
                            return;
                        }
                        msg = listener.recv() => {
                            match msg {
                                Ok(n) => {
                                    match serde_json::from_str::<TaskUpdateNotice>(n.payload()) {
                                        Ok(notice) => self.hub.notify(&notice.tenant_id, &notice.task_id),
                                        Err(e) => tracing::warn!(error = %e, payload = n.payload(), "A2A listener: bad NOTIFY payload — ignoring"),
                                    }
                                }
                                Err(e) => {
                                    // Connection-level error — routine on a
                                    // restart-driven disconnect. Drop to the
                                    // reconnect loop.
                                    tracing::warn!(error = %e, "A2A listener: PgListener recv failed — reconnecting");
                                    break;
                                }
                            }
                        }
                    }
                }
            }
        })
    }
}

/// Sleep `secs`, returning `true` early if shutdown fires first.
async fn sleep_or_shutdown(secs: u64, shutdown: &CancellationToken) -> bool {
    tokio::select! {
        _ = shutdown.cancelled() => true,
        _ = tokio::time::sleep(Duration::from_secs(secs)) => false,
    }
}

/// Releases a task's hub entry when the stream that owned it ends.
///
/// See `subscribe_guarded`.
pub struct StreamSubscription {
    hub: Arc<A2aStreamHub>,
    tenant_id: String,
    task_id: String,
}

impl Drop for StreamSubscription {
    fn drop(&mut self) {
        self.hub.release(&self.tenant_id, &self.task_id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn subscribe_then_notify_wakes_receiver() {
        let hub = A2aStreamHub::new();
        let mut rx = hub.subscribe("t1", "task-a");
        hub.notify("t1", "task-a");
        assert!(rx.try_recv().is_ok(), "subscriber should have been woken");
    }

    #[tokio::test]
    async fn notify_other_task_does_not_wake() {
        let hub = A2aStreamHub::new();
        let mut rx = hub.subscribe("t1", "task-a");
        hub.notify("t1", "task-b");
        assert!(
            rx.try_recv().is_err(),
            "a wake for a different task must not fire this receiver"
        );
    }

    #[tokio::test]
    async fn tenant_scoping_isolates_same_task_id() {
        let hub = A2aStreamHub::new();
        let mut rx_a = hub.subscribe("tenant-a", "task-1");
        let mut rx_b = hub.subscribe("tenant-b", "task-1");
        hub.notify("tenant-a", "task-1");
        assert!(rx_a.try_recv().is_ok());
        assert!(
            rx_b.try_recv().is_err(),
            "same task_id in another tenant must not be woken"
        );
    }

    #[tokio::test]
    async fn empty_entry_is_gced_on_notify() {
        let hub = A2aStreamHub::new();
        let rx = hub.subscribe("t1", "task-a");
        assert_eq!(hub.tracked_tasks(), 1);
        drop(rx); // last receiver gone
        hub.notify("t1", "task-a"); // send fails → entry GC'd
        assert_eq!(
            hub.tracked_tasks(),
            0,
            "entry with no receivers should be removed on notify"
        );
    }

    #[test]
    fn notice_round_trips_json() {
        let n = TaskUpdateNotice {
            tenant_id: "t1".into(),
            task_id: "task-a".into(),
        };
        let s = serde_json::to_string(&n).unwrap();
        let back: TaskUpdateNotice = serde_json::from_str(&s).unwrap();
        assert_eq!(back.tenant_id, "t1");
        assert_eq!(back.task_id, "task-a");
    }
}

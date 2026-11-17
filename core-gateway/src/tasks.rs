// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The background tasks the gateway starts before it serves requests: an
//! ordered list of named tasks ([`BACKGROUND_TASKS`]) and the function that
//! starts them ([`start_all`]).
//!
//! They were spawned one after another in `main`. As a list they can be
//! named, counted and, later, extended by another distribution.
//! Each function below is the block that stood in `main`, unchanged; the
//! order of the list is the order they stood in.
//!
//! A task's `start` returns at once: it spawns onto the Tokio runtime, so
//! [`start_all`] must be called from inside one.

use crate::config::AppConfig;
use crate::services::SharedState;
use crate::{routes, services};

/// What a task is started with.
pub struct TaskContext<'a> {
    pub state: &'a SharedState,
    pub config: &'a AppConfig,
}

/// One background task.
#[derive(Clone, Copy)]
pub struct BackgroundTask {
    /// Unique, stable name.
    pub name: &'static str,
    /// The Enterprise feature the task belongs to, or `None` for a task of
    /// the Community Edition.
    pub feature: Option<&'static str>,
    /// Spawns the task and returns.
    pub start: fn(&TaskContext<'_>),
}

/// Every background task, in start order.
pub const BACKGROUND_TASKS: &[BackgroundTask] = &[
    BackgroundTask {
        name: "instance_lease_renewal",
        feature: None,
        start: instance_lease_renewal,
    },
    BackgroundTask {
        name: "pool_sampler",
        feature: None,
        start: pool_sampler,
    },
    BackgroundTask {
        name: "model_metrics_flusher",
        feature: None,
        start: model_metrics_flusher,
    },
    BackgroundTask {
        name: "pool_saturation_monitor",
        feature: None,
        start: pool_saturation_monitor,
    },
    BackgroundTask {
        name: "api_key_usage_flusher",
        feature: None,
        start: api_key_usage_flusher,
    },
    BackgroundTask {
        name: "refused_agent_credential_flusher",
        feature: None,
        start: refused_agent_credential_flusher,
    },
    BackgroundTask {
        name: "oauth_token_cleanup",
        feature: None,
        start: oauth_token_cleanup,
    },
    BackgroundTask {
        name: "video_pipeline",
        feature: None,
        start: video_pipeline,
    },
    BackgroundTask {
        name: "media_gc_sweep",
        feature: None,
        start: media_gc_sweep,
    },
    BackgroundTask {
        name: "media_filesystem_presence",
        feature: None,
        start: media_filesystem_presence,
    },
    BackgroundTask {
        name: "router_state_cleanup",
        feature: None,
        start: router_state_cleanup,
    },
    BackgroundTask {
        name: "cache_invalidation_listener",
        feature: None,
        start: cache_invalidation_listener,
    },
    BackgroundTask {
        name: "system_notify_listener",
        feature: None,
        start: system_notify_listener,
    },
    BackgroundTask {
        name: "run_abort_listener",
        feature: None,
        start: run_abort_listener,
    },
    BackgroundTask {
        name: "a2a_stream_listener",
        feature: None,
        start: a2a_stream_listener,
    },
    BackgroundTask {
        name: "quota_counter_seed",
        feature: None,
        start: quota_counter_seed,
    },
    BackgroundTask {
        name: "usage_alert_resolver",
        feature: None,
        start: usage_alert_resolver,
    },
    BackgroundTask {
        name: "admission_lease_renewal",
        feature: None,
        start: admission_lease_renewal,
    },
    BackgroundTask {
        name: "evidence_witness_retry",
        feature: None,
        start: evidence_witness_retry,
    },
    BackgroundTask {
        name: "tool_approval_expiry",
        feature: None,
        start: tool_approval_expiry,
    },
    BackgroundTask {
        name: "skill_execution_reaper",
        feature: None,
        start: skill_execution_reaper,
    },
    BackgroundTask {
        name: "system_alert_resolver",
        feature: None,
        start: system_alert_resolver,
    },
    BackgroundTask {
        name: "run_ledger_sweeper",
        feature: None,
        start: run_ledger_sweeper,
    },
    BackgroundTask {
        name: "ai_system_liveness",
        feature: None,
        start: ai_system_liveness,
    },
    BackgroundTask {
        name: "behavioural_drift",
        feature: None,
        start: behavioural_drift,
    },
    BackgroundTask {
        name: "replay_runner",
        feature: None,
        start: replay_runner,
    },
];

/// Start `tasks` in order. Must be called from inside a Tokio runtime.
pub fn start_all(ctx: &TaskContext<'_>, tasks: &[BackgroundTask]) {
    for task in tasks {
        (task.start)(ctx);
    }
}

// The instance lease of a Community gateway, renewed every
// five seconds for as long as the process runs (the first renewal at once).
// First in the list: the lease was taken while the gateway was built, and
// nothing that starts later should be able to delay its renewal. A
// composition that runs as several instances holds no lease and this starts
// nothing. A lease found held by another instance is logged and alerted;
// the gateway keeps serving.
fn instance_lease_renewal(ctx: &TaskContext<'_>) {
    let Some(lease) = ctx.state.instance_lease.clone() else {
        return;
    };
    services::instance_lease::InstanceLease::spawn_renewal(
        lease,
        ctx.state.db.clone(),
        ctx.state.shutdown_token.clone(),
    );
    tracing::info!("Instance lease renewal started");
}

// Pool sampler. Pushes `proxy_db_pool_size` and
// `proxy_db_pool_idle` gauges every N seconds so an operator
// can correlate slow-request bursts with main-pool saturation
// windows. Cancels cleanly on shutdown.
fn pool_sampler(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    services::diagnostics::spawn_pool_sampler(
        state.db.clone(),
        std::time::Duration::from_secs(config.pool_sampler_interval_secs),
        state.shutdown_token.clone(),
    );
}

// Model metrics flusher. Drains the in-process
// counter aggregator into a single bulk UPDATE every
// `BRUTOR_MODEL_METRICS_FLUSH_INTERVAL_SECS`. The aggregator is
// populated by request handlers via
// `state.model_metrics.record(...)` instead of per-request
// UPDATEs that previously contended with admin
// reset-resilience for the `llm_model` row lock. Final flush
// on shutdown so we don't drop counters on Pod recycle.
fn model_metrics_flusher(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    let flush_interval = if config.model_metrics_flush_interval_secs == 0 {
        // Test override — `BRUTOR_MODEL_METRICS_FLUSH_INTERVAL_SECS=0`
        // shrinks to 100 ms so integration tests don't have to
        // wait 5 s. Production should never hit this branch.
        std::time::Duration::from_millis(100)
    } else {
        std::time::Duration::from_secs(config.model_metrics_flush_interval_secs)
    };
    services::model_metrics_aggregator::spawn_flusher(
        state.model_metrics.clone(),
        state.db.clone(),
        flush_interval,
        state.shutdown_token.clone(),
    );
}

// Pool saturation monitor. Periodically checks the
// main pool and fires a system_alert when sustained idle==0
// crosses the configured window. Pairs with the
// pool_sampler (Prometheus gauges) — sampler shows utilisation
// continuously, monitor fires the page when it crosses the
// operator's tolerance.
fn pool_saturation_monitor(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    services::pool_saturation_monitor::spawn(
        state.db.clone(),
        state.db.clone(),
        std::time::Duration::from_secs(config.pool_sampler_interval_secs),
        std::time::Duration::from_secs(config.pool_saturation_sustained_secs),
        state.shutdown_token.clone(),
    );
}

// Batched api_keys.usage_count / last_used_at writer. Flushes
// every few seconds and once more on shutdown; replaces the per-request
// spawned UPDATE that serialised on one hot row per busy key.
fn api_key_usage_flusher(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    services::api_key_usage::spawn_flusher(
        state.db.clone(),
        std::time::Duration::from_secs(services::api_key_usage::FLUSH_INTERVAL_SECS),
        state.shutdown_token.clone(),
    );
}

// Audit rows for refused agent credentials: at most one per
// credential per minute, written through the audit writer; everything
// pending is written on shutdown.
fn refused_agent_credential_flusher(ctx: &TaskContext<'_>) {
    services::refused_agent_credential::spawn_flusher(
        ctx.state.clone(),
        ctx.state.shutdown_token.clone(),
    );
}

// Periodic cleanup of expired OAuth tokens (hourly, tokens expired > 30 days)
fn oauth_token_cleanup(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let token_cleanup_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(3600));
        loop {
            interval.tick().await;
            match token_cleanup_state
                .oauth_token_manager
                .cleanup_expired_tokens(&token_cleanup_state.db, None, 30)
                .await
            {
                Ok(count) => {
                    if count > 0 {
                        tracing::info!(deleted = count, "Cleaned up expired OAuth tokens");
                    }
                }
                Err(e) => tracing::warn!(error = %e, "Failed to clean up expired OAuth tokens"),
            }
        }
    });
}

// Video generation pipeline. Drives
// video_generation_jobs rows pending → submitted → terminal
// by submitting + polling provider queue APIs and persisting
// the output bytes via the Files API. Runs in-process — the
// workload is tiny per tick (≤ 25 rows × 2 phases) and the
// trial bundle doesn't ship a dedicated worker.
fn video_pipeline(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let video_pipeline_db = state.db.clone();
    let video_pipeline_storage = state.media_storage.clone();
    let video_pipeline_http = state.http_client.clone();
    let video_pipeline_enc = state.encryption.clone();
    services::video_pipeline::spawn_loop(
        video_pipeline_db,
        video_pipeline_storage,
        video_pipeline_http,
        video_pipeline_enc,
        services::video_pipeline::DEFAULT_TICK_SECS,
    );
}

// Periodic media-files GC sweep. Soft-deletes
// past-retention rows and hard-deletes both row + bytes after
// the recovery window. Default cadence 6 h; runs in-process
// because the work is small and the trial bundle doesn't ship
// a separate worker.
fn media_gc_sweep(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let media_gc_db = state.db.clone();
    let media_gc_storage = state.media_storage.clone();
    let media_gc_interval = state.config.media_gc_interval_secs;
    services::media_storage::spawn_sweep_loop(media_gc_db, media_gc_storage, media_gc_interval);
}

// The instances that use a filesystem media store, found in Redis
// (`services::media_storage::replicas`): only where the store is the
// filesystem one and the composition runs as several instances (no instance
// lease; a Community gateway holds one and is one instance by construction).
fn media_filesystem_presence(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    if state.media_downloads.is_none() || state.instance_lease.is_some() {
        return;
    }
    let Some(mut redis) = state.redis.clone() else {
        return;
    };
    let instance = services::instance_lease::mint_instance_id();
    let presence = services::media_storage::replicas::Presence::default();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(services::media_storage::replicas::BEAT);
        let mut last = 0usize;
        loop {
            interval.tick().await;
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0);
            match presence.beat(&mut redis, &instance, now_ms).await {
                Ok(count) => {
                    if count != last {
                        match services::media_storage::replicas::warning(count) {
                            Some(text) => tracing::error!(instances = count, "{text}"),
                            None if last > 1 => tracing::info!(
                                "One gateway instance uses the filesystem media store again"
                            ),
                            None => {}
                        }
                        last = count;
                    }
                }
                Err(e) => tracing::warn!(
                    error = %e,
                    "Could not report this instance's use of the filesystem media store"
                ),
            }
        }
    });
}

// Periodic cleanup of router state (cooldowns, stale rate counters, cache) — every 60s
fn router_state_cleanup(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let router_cleanup_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
        loop {
            interval.tick().await;
            router_cleanup_state.router_state.cleanup();
        }
    });
}

// Cache invalidation listener (Redis pub/sub).
//
// CRITICAL: every cache passed to `CacheInvalidator` MUST share
// state with the corresponding `state.*` instance — for moka-backed
// caches that's via `Clone` (cloning a moka::Cache returns a handle
// to the same `Arc`-shared inner store), and for the
// already-wrapped ones it's via `Arc::clone`. Constructing fresh
// cache instances here would silently produce throwaway caches —
// pub/sub invalidations would evict them while the request
// pipeline kept serving stale data from the live caches in
// `state.*` until their TTL expired.
fn cache_invalidation_listener(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    if let Some(ref redis_url) = config.redis_url {
        let invalidator = services::cache::CacheInvalidator {
            model_cache: state.model_cache.clone(),
            group_cache: state.group_cache.clone(),
            mcp_cache: state.mcp_cache.clone(),
            skill_cache: state.skill_cache.clone(),
            provider_cache: state.provider_cache.clone(),
            guardrails_cache: state.guardrails_cache.clone(),
            argument_policy_cache: state.argument_policy_cache.clone(),
            agent_card_cache: state.agent_card_cache.clone(),
            // Shared with the live request pipeline — the
            // listener uses it to refresh router-cache gates.
            state: Some(state.clone()),
            redis: state.redis.clone(),
            // The composed registry: built-in handlers and the extensions'.
            handlers: state.invalidation.clone(),
        };
        invalidator.spawn(redis_url.clone(), state.shutdown_token.clone());
        tracing::info!("Cache invalidation listener started");
    } else {
        tracing::info!(
            "Redis not configured — cache invalidation via pub/sub disabled (TTL-only mode)"
        );
    }
}

// Postgres LISTEN consumer for System
// Operations Console NOTIFY channels. The listener owns a PgListener
// subscribed to the 9 brutor:* channels and dispatches each
// notification to its handler.
//
// Not all of them mutate this process. Cache stats, cache invalidate,
// maintenance and (partially) counters reset do; scheduler
// trigger/pause/resume, session revoke and log level are ADVISORY here
// — the audit row is written by the control plane and the console
// renders it, but gateway-core has nothing to change. The per-channel
// table in `services::system_notify` is the authority; this comment
// used to claim all of them were wired, which is how an operator ended
// up trusting a pause that never happened.
//
// Mirrors the CacheInvalidator pattern above — reconnects with 2s
// → 30s exponential backoff so a transient Postgres blip doesn't
// permanently drop System Operations Console actions.
fn system_notify_listener(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    {
        let listener = services::system_notify::SystemNotifyListener {
            database_url: config.database_url.clone(),
            state: state.clone(),
        };
        listener.spawn(state.shutdown_token.clone());
        tracing::info!("System Operations NOTIFY listener started");
    }
}

// Run-abort NOTIFY listener. Path 1 of three: an
// operator's abort reaches every replica within a round trip, after
// which the hot-path check is a lock-free map lookup. The Redis mirror
// and the sweeper's reconcile cover a replica that misses it, because an
// abort that fails to arrive lets a runaway continue.
fn run_abort_listener(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    {
        let listener = services::run_intervention::RunAbortListener {
            database_url: config.database_url.clone(),
            cache: state.run_abort_cache.clone(),
            db: state.db.clone(),
        };
        listener.spawn(state.shutdown_token.clone());
        tracing::info!("Run-abort NOTIFY listener started");
    }
}

// A2A task-update NOTIFY listener — relays `pg_notify('a2a_task_update')`
// (fired by task_store::write on any replica) into this pod's in-process
// stream hub, so open SSE streams emit new frames event-driven instead of
// polling every 500ms. Same reconnect/backoff discipline as above.
fn a2a_stream_listener(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let config = ctx.config;
    {
        let listener = services::a2a_v1::stream_hub::A2aStreamListener {
            database_url: config.database_url.clone(),
            hub: state.a2a_stream_hub.clone(),
        };
        listener.spawn(state.shutdown_token.clone());
        tracing::info!("A2A task-update NOTIFY listener started");
    }
}

// Keep the Redis quota counters coherent with the usage_logs ledger: the
// heal only ever RAISES a counter to the ledger.
fn quota_counter_seed(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    {
        let reconcile_state = state.clone();
        tokio::spawn(async move {
            // One-time baseline when the proxy first comes up against a system
            // that already has ledger usage this window: FORCE counters = ledger
            // so a mid-window start doesn't under-count. Guarded by a Redis
            // sentinel → runs once per Redis lifetime.
            if reconcile_state
                .usage_state
                .try_claim_shadow_baseline()
                .await
            {
                let n = services::usage::backfill_llm_shadow_counters(
                    &reconcile_state.db,
                    &reconcile_state.usage_state,
                )
                .await;
                tracing::info!(counters = n, "LLM quota-counter baseline established");
            }

            // Periodic cold-start seed (every 2 minutes): heals a window
            // roll-over or a mid-window Redis flush by seeding cold counters
            // from the ledger. Never overwrites a live counter.
            let mut interval = tokio::time::interval(std::time::Duration::from_secs(120));
            loop {
                interval.tick().await;
                services::usage::reconcile_llm_shadow_counters(
                    &reconcile_state.db,
                    &reconcile_state.usage_state,
                )
                .await;
            }
        });
        tracing::info!("LLM quota-counter seed task started (120s)");
    }
}

// Periodic resolution of expired usage alerts (every 5 minutes)
fn usage_alert_resolver(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let alert_cleanup_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(300));
        loop {
            interval.tick().await;
            match services::usage::resolve_expired_alerts(&alert_cleanup_state.db).await {
                Ok(count) => {
                    if count > 0 {
                        tracing::info!(resolved = count, "Resolved expired usage alerts");
                    }
                }
                Err(e) => tracing::warn!(error = %e, "Failed to resolve expired usage alerts"),
            }
        }
    });
}

// Admission holds (see `usage_state::admission`): renew the leases of
// the calls this replica is serving, and end the holds of replicas that
// stopped renewing (a crash, a partition). Every replica runs both; the
// reaper ends each hold once. A reaped hold is kept as spent (its worst
// case) and frees its in-flight slots, so it is logged at WARN: a steady
// trickle means replicas are dying or a path is not settling.
fn admission_lease_renewal(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let hold_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(
            services::cache::usage_state::HOLD_RENEW_SECS,
        ));
        loop {
            interval.tick().await;
            if let Err(e) = hold_state.usage_state.renew_live_holds().await {
                tracing::error!(error = %e, "admission lease renewal failed — this replica's holds may be reaped");
            }
            match hold_state.usage_state.reap_expired_holds().await {
                Ok(0) => {}
                Ok(n) => tracing::warn!(
                    reaped = n,
                    "Ended admission holds whose lease lapsed (kept as spent)"
                ),
                Err(e) => tracing::error!(error = %e, "admission hold reaper failed"),
            }
        }
    });
    tracing::info!(
        renew_secs = services::cache::usage_state::HOLD_RENEW_SECS,
        lease_ms = services::cache::usage_state::HOLD_LEASE_MS,
        "Admission lease renewal + reaper started"
    );
}

// Re-register tree heads that lack a verified witness receipt.
fn evidence_witness_retry(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    tokio::spawn(services::evidence::witness::retry_loop(state.db.clone()));
}

// Periodic expiration of pending tool approvals (every 60 seconds)
fn tool_approval_expiry(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let approval_cleanup_state = state.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
        loop {
            interval.tick().await;
            match routes::approvals::expire_pending_approvals(&approval_cleanup_state.db).await {
                Ok(count) => {
                    if count > 0 {
                        tracing::info!(expired = count, "Expired pending tool approval requests");
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "Failed to expire pending tool approvals")
                }
            }
        }
    });
}

// Reaping of abandoned RUNNING skill executions (every 60 seconds)
fn skill_execution_reaper(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let reaper_db = state.db.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(60));
        loop {
            interval.tick().await;
            // Safety net: settle executions RUNNING far past the runner
            // timeout, so a leaked row cannot hold a concurrency slot forever.
            match services::skill_orchestrator::reap_abandoned_skill_executions(
                &reaper_db,
                services::skill_orchestrator::ABANDONED_EXECUTION_AFTER_SECS,
            )
            .await
            {
                Ok(count) => {
                    if count > 0 {
                        tracing::warn!(reaped = count, "Reaped abandoned RUNNING skill executions");
                    }
                }
                Err(e) => {
                    tracing::warn!(error = %e, "Failed to reap abandoned skill executions")
                }
            }
        }
    });
}

// System alert auto-resolver: closes recovered health alerts and
// stale downstream alerts every 60s. Honours the shutdown token so
// the loop exits promptly instead of waiting for the next tick.
fn system_alert_resolver(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let alert_db = state.db.clone();
    let alert_shutdown = state.shutdown_token.clone();
    tokio::spawn(async move {
        services::system_alerts_resolver::run_resolver_loop(alert_db, 60, alert_shutdown).await;
    });
}

// Run-ledger idle sweeper. Closes runs that went
// quiet without an end signal (the common case: no LLM or MCP protocol
// has one). Runs every 30s; each tick closes at most SWEEP_BATCH_SIZE
// so a backlog drains over several ticks rather than in one long
// transaction competing with request traffic.
fn run_ledger_sweeper(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let run_sweep_db = state.db.clone();
    let run_sweep_abort_cache = state.run_abort_cache.clone();
    let run_sweep_shutdown = state.shutdown_token.clone();
    tokio::spawn(async move {
        services::run_ledger::run_sweeper_loop(
            run_sweep_db,
            30,
            run_sweep_shutdown,
            Some(run_sweep_abort_cache),
        )
        .await;
    });
}

// AI System liveness. The only detector that fires
// on the ABSENCE of traffic: a system that stopped calling looks
// perfect on every other signal, because there is nothing to be wrong.
// Every 60s; findings are cooled down so one outage is one alert.
fn ai_system_liveness(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let liveness_db = state.db.clone();
    let liveness_shutdown = state.shutdown_token.clone();
    tokio::spawn(async move {
        services::liveness::run_liveness_loop(liveness_db, 60, liveness_shutdown).await;
    });
}

// Behavioural drift. Refreshes baselines and
// compares the recent window against them, attributing a cause rather
// than reporting a p-value. Every 15 minutes: drift is a trend, not an
// event, and a tighter loop would only recompute the same baselines.
fn behavioural_drift(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let drift_db = state.db.clone();
    let drift_shutdown = state.shutdown_token.clone();
    tokio::spawn(async move {
        services::drift::sweeper::run_drift_loop(drift_db, 900, drift_shutdown).await;
    });
}

// Executes replay runs the control plane requests.
// The two sides are joined by the row rather than a call: the admin UI
// talks to one backend, and neither service goes down with the other.
fn replay_runner(ctx: &TaskContext<'_>) {
    let state = ctx.state;
    let replay_db = state.db.clone();
    let replay_shutdown = state.shutdown_token.clone();
    tokio::spawn(async move {
        services::replay::runner::run_replay_loop(
            replay_db,
            services::replay::runner::SWEEP_INTERVAL_SECONDS,
            replay_shutdown,
        )
        .await;
    });
}
